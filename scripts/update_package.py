#!/usr/bin/env python3
"""Update one package directory to the current upstream version.

The script reads nvchecker.toml for GitHub coordinates, then rewrites pkgver,
pkgrel, and (for memprocfs) source URLs. A pkgver bump resets pkgrel to 1.
memprocfs does not: its pkgrel is the upstream asset date. Checksums for
non-VCS packages are filled afterwards by updpkgsums inside the Arch test
build. A package is left untouched when the upstream version is not strictly
newer.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import urllib.request
from pathlib import Path

from alpmver import vercmp

ROOT = Path(__file__).resolve().parent.parent
NVCHECKER_TOML = ROOT / "nvchecker.toml"
KEYFILE = ROOT / "nvchecker-keyfile.toml"

PRE_RE = re.compile(r"(?i)(?:alpha|beta|rc|pre|dev)")
MEMPROCFS_ASSET = re.compile(
    r"^MemProcFS_files_and_binaries_v(\d+\.\d+\.\d+)-linux_(x64|aarch64)-(\d{8})\.tar\.gz$"
)


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


def set_output(key: str, value: str) -> None:
    print(f"{key}={value}")
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{key}={value}\n")


def github_token() -> str:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
    if token:
        return token
    if KEYFILE.exists():
        data = tomllib.loads(KEYFILE.read_text(encoding="utf-8"))
        keys = data.get("keys", {})
        token = keys.get("github") or keys.get("github.com") or ""
        if token and "your_token_here" not in token:
            return token
    try:
        return subprocess.check_output(["gh", "auth", "token"], text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def ensure_keyfile() -> None:
    token = github_token()
    if KEYFILE.exists() and not token:
        return
    escaped = token.replace("\\", "\\\\").replace('"', '\\"')
    KEYFILE.write_text(f'[keys]\ngithub = "{escaped}"\n', encoding="utf-8")
    KEYFILE.chmod(0o600)


def github_api(url: str):
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "aur-packages",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    token = github_token()
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def load_nv_entry(name: str) -> dict:
    data = tomllib.loads(NVCHECKER_TOML.read_text(encoding="utf-8"))
    entry = data.get(name)
    if not isinstance(entry, dict):
        raise SystemExit(f"{name} is not declared in nvchecker.toml")
    return entry


def nvchecker_version(name: str) -> str | None:
    if shutil.which("nvchecker") is None:
        log("nvchecker is not installed; using the GitHub API only")
        return None
    ensure_keyfile()
    proc = subprocess.run(
        [
            "nvchecker",
            "-c",
            str(NVCHECKER_TOML),
            "-k",
            str(KEYFILE),
            "-e",
            name,
            "--logger",
            "json",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        log(proc.stderr.strip() or proc.stdout.strip() or "nvchecker failed")
        return None
    if proc.stderr.strip():
        log(proc.stderr.strip())
    newver = ROOT / "new_ver.json"
    if not newver.exists():
        return None
    payload = json.loads(newver.read_text(encoding="utf-8"))
    entry = payload.get("data", {}).get(name, {})
    version = entry.get("version") if isinstance(entry, dict) else None
    if version:
        log(f"nvchecker {name} -> {version}")
    return version


def stable_github_version(repo: str, prefix: str) -> str:
    releases = github_api(f"https://api.github.com/repos/{repo}/releases?per_page=50")
    best: str | None = None
    for rel in releases:
        if rel.get("draft") or rel.get("prerelease"):
            continue
        tag = rel.get("tag_name") or ""
        version = tag[len(prefix) :] if prefix and tag.startswith(prefix) else tag
        if not re.match(r"^\d", version) or PRE_RE.search(version):
            continue
        if best is None or vercmp(version, best) > 0:
            best = version
    if not best:
        raise SystemExit(f"no stable release found for {repo}")
    return best


def choose_release_version(name: str, *, use_nvchecker: bool = True) -> str:
    """Highest stable GitHub release, or nvchecker's value when it is newer.

    use_nvchecker=False is the read-only mode for the publish host: nvchecker
    writes nvchecker-keyfile.toml (with the token) and new_ver.json into the
    checkout, so it is skipped and only the GitHub releases API is read.
    """
    entry = load_nv_entry(name)
    if entry.get("source") != "github":
        raise SystemExit(f"{name} is not a github nvchecker entry")
    repo = entry["github"]
    prefix = entry.get("prefix") or ""
    api_version = stable_github_version(repo, prefix)
    log(f"github releases {repo} -> {api_version}")
    if not use_nvchecker:
        return api_version
    nv = nvchecker_version(name)
    chosen = api_version
    if nv and not PRE_RE.search(nv) and vercmp(nv, api_version) > 0:
        chosen = nv
    if nv and PRE_RE.search(nv):
        log(f"ignoring nvchecker prerelease {nv}")
    return chosen


def read_pkgbuild(pkg: str) -> str:
    return (ROOT / pkg / "PKGBUILD").read_text(encoding="utf-8")


def write_pkgbuild(pkg: str, text: str) -> None:
    (ROOT / pkg / "PKGBUILD").write_text(text, encoding="utf-8")


def read_var(text: str, name: str) -> str:
    match = re.search(rf"^{re.escape(name)}=(.+)$", text, re.M)
    if not match:
        raise SystemExit(f"PKGBUILD is missing {name}")
    return match.group(1).strip().strip("'\"")


def set_var(text: str, name: str, value: str) -> str:
    new, count = re.subn(
        rf"^{re.escape(name)}=.*$",
        f"{name}={value}",
        text,
        count=1,
        flags=re.M,
    )
    if count != 1:
        raise SystemExit(f"could not set {name}")
    return new


def set_quoted_array(text: str, name: str, url: str) -> str:
    pattern = rf'^{re.escape(name)}=\(".*?"\)$'
    new, count = re.subn(
        pattern,
        lambda _m: f'{name}=("{url}")',
        text,
        count=1,
        flags=re.M,
    )
    if count != 1:
        raise SystemExit(f"could not set {name}")
    return new


def set_sha256sum(text: str, name: str, digest: str) -> str:
    """Replace one single-line sha256sums or sha256sums_<arch> entry."""
    if not re.fullmatch(r"sha256sums(?:_[A-Za-z0-9_]+)?", name):
        raise SystemExit(f"refusing checksum array name {name!r}")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise SystemExit(f"refusing checksum for {name}")
    pattern = rf"^{re.escape(name)}=\('(?:[0-9a-f]+|SKIP)'\)$"
    new, count = re.subn(
        pattern,
        lambda _m: f"{name}=('{digest}')",
        text,
        count=1,
        flags=re.M,
    )
    if count != 1:
        raise SystemExit(f"could not set {name}")
    return new


def bump_pkgver(text: str, new_ver: str) -> str:
    """Set pkgver and reset pkgrel to 1.

    memprocfs does not use this helper. Its pkgrel is the upstream asset date.
    """
    text = set_var(text, "pkgver", new_ver)
    text = set_var(text, "pkgrel", "1")
    return text


def full_version(pkgver: str, pkgrel: str) -> str:
    return f"{pkgver}-{pkgrel}"


def mark_changed(pkg: str, old_full: str, new_full: str) -> None:
    (ROOT / pkg / ".update-old-version").write_text(old_full + "\n", encoding="utf-8")
    log(f"update {pkg}: {old_full} -> {new_full}")
    set_output("changed", "true")
    set_output("version", new_full)


def mark_unchanged(pkg: str, current: str) -> None:
    log(f"{pkg} is current at {current}")
    set_output("changed", "false")
    set_output("version", current)


def require_newer(pkg: str, old_full: str, new_full: str) -> None:
    cmp = vercmp(new_full, old_full)
    if cmp < 0:
        raise SystemExit(
            f"refusing to downgrade {pkg} from {old_full} to {new_full}"
        )
    if cmp == 0:
        mark_unchanged(pkg, old_full)
        raise SystemExit(0)


def update_maple() -> None:
    text = read_pkgbuild("maple-mono-nf-cn")
    old_ver = read_var(text, "pkgver")
    old_rel = read_var(text, "pkgrel")
    new_ver = choose_release_version("maple-mono-nf-cn")
    new_full = full_version(new_ver, "1")
    old_full = full_version(old_ver, old_rel)
    if new_ver == old_ver:
        mark_unchanged("maple-mono-nf-cn", old_full)
        return
    require_newer("maple-mono-nf-cn", old_full, new_full)
    text = bump_pkgver(text, new_ver)
    write_pkgbuild("maple-mono-nf-cn", text)
    mark_changed("maple-mono-nf-cn", old_full, new_full)


def probe_memprocfs() -> dict:
    release = github_api("https://api.github.com/repos/ufrisk/MemProcFS/releases/latest")
    tag = release.get("tag_name") or ""
    if release.get("prerelease") or release.get("draft") or PRE_RE.search(tag):
        raise SystemExit(f"MemProcFS latest release is not stable: {tag}")
    found: dict[str, dict] = {}
    for asset in release.get("assets", []):
        match = MEMPROCFS_ASSET.match(asset.get("name") or "")
        if not match:
            continue
        version, arch, date = match.group(1), match.group(2), match.group(3)
        found[arch] = {
            "version": version,
            "date": date,
            "url": asset["browser_download_url"],
            "name": asset["name"],
        }
    if "x64" not in found or "aarch64" not in found:
        raise SystemExit(f"MemProcFS release {tag} is missing linux assets: {list(found)}")
    if found["x64"]["version"] != found["aarch64"]["version"] or found["x64"]["date"] != found["aarch64"]["date"]:
        raise SystemExit(f"MemProcFS linux assets disagree: {found}")
    return {
        "tag": tag,
        "pkgver": found["x64"]["version"],
        "pkgrel": found["x64"]["date"],
        "x86_64": found["x64"]["url"],
        "aarch64": found["aarch64"]["url"],
    }


def update_memprocfs() -> None:
    # nvchecker.toml runs scripts/nvchecker_memprocfs.py, which calls this probe.
    info = probe_memprocfs()
    text = read_pkgbuild("memprocfs")
    old_full = full_version(read_var(text, "pkgver"), read_var(text, "pkgrel"))
    new_full = full_version(info["pkgver"], info["pkgrel"])
    if vercmp(new_full, old_full) == 0:
        mark_unchanged("memprocfs", old_full)
        return
    require_newer("memprocfs", old_full, new_full)
    text = set_var(text, "pkgver", info["pkgver"])
    text = set_var(text, "pkgrel", info["pkgrel"])
    text = set_quoted_array(text, "source_x86_64", info["x86_64"])
    text = set_quoted_array(text, "source_aarch64", info["aarch64"])
    write_pkgbuild("memprocfs", text)
    mark_changed("memprocfs", old_full, new_full)


# Digest-pinned archlinux:base-devel, shared with test_build.sh and
# regen_srcinfo.sh. Bump the digest in scripts/archlinux-image.
ARCH_IMAGE_FILE = Path(__file__).resolve().parent / "archlinux-image"
ARCH_IMAGE_RE = re.compile(r"^archlinux:base-devel@sha256:[0-9a-f]{64}$")


def arch_image() -> str:
    image = ARCH_IMAGE_FILE.read_text(encoding="utf-8").strip()
    if not ARCH_IMAGE_RE.fullmatch(image):
        raise SystemExit(f"{ARCH_IMAGE_FILE.name} must be archlinux:base-devel@sha256:<digest>, not {image!r}")
    return image


MOON_SANDBOX_IMAGE = arch_image()


def moon_version_sandboxed(bindir: Path) -> str:
    """Run `moon version` in a throwaway container.

    The publish job holds a write token in .git/config, so the upstream
    binary must not run on that host directly. The container gets only the
    extracted bin directory (read-only), no network, no capabilities, a
    read-only root and an unprivileged user.
    """
    for path in [bindir, *bindir.rglob("*")]:
        if path.is_symlink():
            continue
        mode = path.stat().st_mode
        path.chmod(mode | (0o755 if path.is_dir() else 0o444))
    return subprocess.check_output(
        [
            "docker", "run", "--rm",
            "--network", "none",
            "--read-only",
            "--tmpfs", "/tmp",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--user", "65534:65534",
            "-v", f"{bindir}:/opt/moon/bin:ro",
            "-e", "HOME=/tmp",
            "-e", "PATH=/opt/moon/bin:/usr/bin:/bin",
            "-e", "MOONC_OVERRIDE=/opt/moon/bin/moonc",
            "-e", "MOONRUN_OVERRIDE=/opt/moon/bin/moonrun",
            MOON_SANDBOX_IMAGE,
            "/opt/moon/bin/moon", "version",
        ],
        text=True,
    )


def probe_moonbit(*, with_core: bool = True, sandbox: bool = False) -> dict:
    """Download the latest toolchain and read `moon version`.

    MoonBit's installer accepts a version segment, but dated paths such as
    binaries/0.1.20261023/ return S3 AccessDenied. The only published channels
    are `latest` and `nightly`, so the PKGBUILD keeps the latest URL and pins
    the checksum of the tarball that produced pkgver.

    The publish host calls this with with_core=False (core-latest.tar.gz is
    not needed for the version) and sandbox=True (see moon_version_sandboxed).
    Everything is written under a temporary directory outside the checkout.
    """
    tmp = Path(tempfile.mkdtemp(prefix="moonbit-"))
    try:
        archive = tmp / "moonbit.tar.gz"
        core = tmp / "core.tar.gz"
        subprocess.check_call(
            [
                "curl",
                "-fL",
                "--retry",
                "3",
                "-o",
                str(archive),
                "https://cli.moonbitlang.com/binaries/latest/moonbit-linux-x86_64.tar.gz",
            ]
        )
        if with_core:
            subprocess.check_call(
                [
                    "curl",
                    "-fL",
                    "--retry",
                    "3",
                    "-o",
                    str(core),
                    "https://cli.moonbitlang.com/cores/core-latest.tar.gz",
                ]
            )
        subprocess.check_call(
            ["tar", "-xzf", str(archive), "-C", str(tmp), "./bin"]
        )
        for path in (tmp / "bin").rglob("*"):
            if path.is_file():
                path.chmod(path.stat().st_mode | 0o755)
        if sandbox:
            output = moon_version_sandboxed(tmp / "bin")
        else:
            moon = tmp / "bin" / "moon"
            env = os.environ.copy()
            env["PATH"] = str(tmp / "bin") + os.pathsep + env.get("PATH", "")
            env["MOONC_OVERRIDE"] = str(tmp / "bin" / "moonc")
            env["MOONRUN_OVERRIDE"] = str(tmp / "bin" / "moonrun")
            output = subprocess.check_output([str(moon), "version"], env=env, text=True)
        match = re.search(r"^moon\s+(\d+\.\d+\.\d+)", output, re.M)
        if not match:
            raise SystemExit(f"could not parse moon version from: {output!r}")
        return {"pkgver": match.group(1), "moon_output": output.strip().splitlines()[0]}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# mfile-bin publishes one tarball per Arch arch. The PKGBUILD names are
# source_<arch> / sha256sums_<arch>. Digests are the lines in the release
# SHA256SUMS file for the upstream asset names, not a re-hash invented here.
# The repository default branch is master; versions come from release tags.
MFILE_REPO = "DawnMagnet/mfile"
MFILE_ARCHES = ("x86_64", "aarch64")
MFILE_GOARCH = {"x86_64": "amd64", "aarch64": "arm64"}
SHA256SUMS_LINE = re.compile(r"^([0-9a-f]{64}) [ *](\S+)$")


def mfile_source_item(arch: str) -> str:
    """PKGBUILD source item for one arch. ${pkgver} stays literal."""
    if arch not in MFILE_GOARCH:
        raise SystemExit(f"unknown mfile arch {arch}")
    goarch = MFILE_GOARCH[arch]
    return (
        "${pkgname}-${pkgver}-"
        + arch
        + ".tar.gz::https://github.com/DawnMagnet/mfile/releases/download/v${pkgver}/mfile_${pkgver}_linux_"
        + goarch
        + ".tar.gz"
    )


def mfile_asset_name(pkgver: str, arch: str) -> str:
    return f"mfile_{pkgver}_linux_{MFILE_GOARCH[arch]}.tar.gz"


def parse_mfile_sha256sums(text: str, pkgver: str) -> dict[str, str]:
    """Map Arch arch -> sha256 from a sha256sum(1) SHA256SUMS file.

    Extra assets (darwin, windows) are ignored. Both linux assets are required.
    """
    if not re.fullmatch(r"[0-9A-Za-z.][0-9A-Za-z._]*", pkgver):
        raise SystemExit(f"refusing mfile pkgver {pkgver!r}")
    found: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        match = SHA256SUMS_LINE.fullmatch(line)
        if match is None:
            raise SystemExit(f"malformed SHA256SUMS line: {line!r}")
        digest, name = match.group(1), match.group(2)
        if name in found:
            raise SystemExit(f"duplicate SHA256SUMS entry {name}")
        found[name] = digest
    out: dict[str, str] = {}
    for arch in MFILE_ARCHES:
        asset = mfile_asset_name(pkgver, arch)
        digest = found.get(asset)
        if digest is None:
            raise SystemExit(f"SHA256SUMS for v{pkgver} is missing {asset}")
        out[arch] = digest
    return out


def download_release_text(url: str, *, limit: int = 1024 * 1024) -> str:
    """GET an https URL and return the body as text. Redirects must stay https."""
    if not re.fullmatch(r"https://[A-Za-z0-9._~:/?#@%+=&!,;-]{1,2000}", url):
        raise SystemExit(f"refusing URL {url!r}")

    class _HttpsRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            if not str(newurl).startswith("https://"):
                raise SystemExit(f"refusing redirect to {newurl!r}")
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    opener = urllib.request.build_opener(_HttpsRedirect)
    request = urllib.request.Request(
        url, headers={"User-Agent": "aur-packages", "Accept": "text/plain,*/*"}
    )
    try:
        with opener.open(request, timeout=60) as resp:
            final = resp.geturl()
            if not str(final).startswith("https://"):
                raise SystemExit(f"refusing final URL {final!r}")
            if getattr(resp, "status", 200) != 200:
                raise SystemExit(f"download of {url} returned HTTP {resp.status}")
            data = resp.read(limit + 1)
    except SystemExit:
        raise
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise SystemExit(f"download of {url} failed: {exc}") from exc
    if len(data) > limit:
        raise SystemExit(f"download of {url} is too large")
    return data.decode("utf-8")


def fetch_mfile_sha256sums(pkgver: str) -> str:
    """Download SHA256SUMS for one mfile release tag v<pkgver>."""
    if not re.fullmatch(r"[0-9A-Za-z.][0-9A-Za-z._]*", pkgver):
        raise SystemExit(f"refusing mfile pkgver {pkgver!r}")
    url = f"https://github.com/{MFILE_REPO}/releases/download/v{pkgver}/SHA256SUMS"
    return download_release_text(url, limit=64 * 1024)


def mfile_pinned_sha256sums(pkgver: str, sums_text: str | None = None) -> dict[str, list[str]]:
    """sha256sums_<arch> arrays taken from the release SHA256SUMS file."""
    text = fetch_mfile_sha256sums(pkgver) if sums_text is None else sums_text
    digests = parse_mfile_sha256sums(text, pkgver)
    return {f"sha256sums_{arch}": [digests[arch]] for arch in MFILE_ARCHES}


def apply_mfile_sources_and_sums(text: str, pkgver: str, sums_text: str) -> str:
    """Rewrite both per-arch source arrays and both sha256sums_* arrays.

    A PKGBUILD that only has a single source=() array cannot be updated:
    set_quoted_array / set_sha256sum require source_<arch> and sha256sums_<arch>.
    """
    digests = parse_mfile_sha256sums(sums_text, pkgver)
    for arch in MFILE_ARCHES:
        text = set_quoted_array(text, f"source_{arch}", mfile_source_item(arch))
        text = set_sha256sum(text, f"sha256sums_{arch}", digests[arch])
    return text


def update_mfile() -> None:
    # nvchecker.toml is a github release entry (DawnMagnet/mfile, prefix v).
    text = read_pkgbuild("mfile-bin")
    old_ver = read_var(text, "pkgver")
    old_rel = read_var(text, "pkgrel")
    new_ver = choose_release_version("mfile-bin")
    sums_text = fetch_mfile_sha256sums(new_ver)
    old_full = full_version(old_ver, old_rel)
    if new_ver == old_ver:
        refreshed = apply_mfile_sources_and_sums(text, new_ver, sums_text)
        if refreshed != text:
            raise SystemExit(
                "mfile-bin is at the upstream version but source_<arch> or "
                "sha256sums_<arch> does not match the release SHA256SUMS"
            )
        mark_unchanged("mfile-bin", old_full)
        return
    new_full = full_version(new_ver, "1")
    require_newer("mfile-bin", old_full, new_full)
    text = apply_mfile_sources_and_sums(text, new_ver, sums_text)
    text = bump_pkgver(text, new_ver)
    write_pkgbuild("mfile-bin", text)
    mark_changed("mfile-bin", old_full, new_full)


def update_moonbit() -> None:
    # nvchecker.toml runs scripts/nvchecker_moonbit.py, which calls this probe.
    info = probe_moonbit()
    log(info["moon_output"])
    text = read_pkgbuild("moonbit-bin")
    old_ver = read_var(text, "pkgver")
    old_rel = read_var(text, "pkgrel")
    old_full = full_version(old_ver, old_rel)
    new_ver = info["pkgver"]
    if new_ver == old_ver:
        mark_unchanged("moonbit-bin", old_full)
        return
    new_full = full_version(new_ver, "1")
    require_newer("moonbit-bin", old_full, new_full)
    text = bump_pkgver(text, new_ver)
    write_pkgbuild("moonbit-bin", text)
    mark_changed("moonbit-bin", old_full, new_full)


RELEASE_PACKAGES = ("maple-mono-nf-cn", "memprocfs", "moonbit-bin", "mfile-bin")
PKGREL_RE = re.compile(r"[0-9]+(?:\.[0-9]+)?")


def expected_release(pkg: str, main_pkgbuild: str) -> tuple[str, str]:
    pkgver, pkgrel, _text = expected_release_pkgbuild(pkg, main_pkgbuild)
    return pkgver, pkgrel


def expected_release_pkgbuild(pkg: str, main_pkgbuild: str) -> tuple[str, str, str]:
    """Read-only twin of update_maple / update_memprocfs / update_moonbit.

    Returns the (pkgver, pkgrel) the updater would leave in the PKGBUILD,
    given the PKGBUILD on main, without writing anything into the checkout
    and without nvchecker. pkgrel follows the updater: memprocfs uses the
    upstream asset date; the others keep main's pkgrel when pkgver is
    unchanged and reset it to 1 on a pkgver bump. An upstream version older
    than main fails, like the updater's downgrade check.

    The third value is the PKGBUILD text the updater would write, built in
    memory only (memprocfs gets its source URLs rewritten, as in
    update_memprocfs). publish_artifact.py takes the expected source arrays
    from it.
    """
    main_ver = read_var(main_pkgbuild, "pkgver")
    main_rel = read_var(main_pkgbuild, "pkgrel")
    text = main_pkgbuild
    if pkg == "memprocfs":
        info = probe_memprocfs()
        pkgver, pkgrel = info["pkgver"], info["pkgrel"]
        text = set_quoted_array(text, "source_x86_64", info["x86_64"])
        text = set_quoted_array(text, "source_aarch64", info["aarch64"])
    elif pkg == "maple-mono-nf-cn":
        pkgver = choose_release_version(pkg, use_nvchecker=False)
        pkgrel = main_rel if pkgver == main_ver else "1"
    elif pkg == "moonbit-bin":
        info = probe_moonbit(with_core=False, sandbox=True)
        log(info["moon_output"])
        pkgver = info["pkgver"]
        pkgrel = main_rel if pkgver == main_ver else "1"
    elif pkg == "mfile-bin":
        pkgver = choose_release_version(pkg, use_nvchecker=False)
        pkgrel = main_rel if pkgver == main_ver else "1"
        # Both source_<arch> arrays and both sha256sums_<arch> arrays. The
        # digests are the release SHA256SUMS lines. Source URLs keep ${pkgver}.
        text = apply_mfile_sources_and_sums(text, pkgver, fetch_mfile_sha256sums(pkgver))
    else:
        raise SystemExit(f"{pkg} is not a release package")
    if not PKGVER_RE.fullmatch(pkgver) or not PKGREL_RE.fullmatch(pkgrel):
        raise SystemExit(f"{pkg}: refusing host-computed version {pkgver!r}-{pkgrel!r}")
    if vercmp(full_version(pkgver, pkgrel), full_version(main_ver, main_rel)) < 0:
        raise SystemExit(
            f"{pkg}: upstream {pkgver}-{pkgrel} is older than main {main_ver}-{main_rel}"
        )
    text = set_var(set_var(text, "pkgver", pkgver), "pkgrel", pkgrel)
    return pkgver, pkgrel, text


PKGVER_RE = re.compile(r"[A-Za-z0-9._+]+")


def describe_to_pkgver(described: str) -> str:
    """Same transform as the PKGBUILD pkgver() functions."""
    pkgver = subprocess.check_output(
        ["sed", r"s/^v//;s/\([^-]*-g\)/r\1/;s/-/./g"],
        input=described + "\n",
        text=True,
    ).strip()
    if not pkgver or not PKGVER_RE.fullmatch(pkgver):
        raise SystemExit(f"refusing pkgver {pkgver!r} from git describe {described!r}")
    return pkgver


def git_pkgver(url: str, describe_args: list[str]) -> tuple[str, str]:
    """Return (pkgver, full commit hash) for the default branch of url."""
    tmp = Path(tempfile.mkdtemp(prefix="pkgver-"))
    try:
        subprocess.check_call(
            ["git", "clone", "--filter=blob:none", url, str(tmp / "src")],
            stdout=subprocess.DEVNULL,
        )
        described = subprocess.check_output(
            ["git", "describe", *describe_args],
            cwd=tmp / "src",
            text=True,
        ).strip()
        head = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=tmp / "src", text=True
        ).strip()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return describe_to_pkgver(described), head


def git_commit_pkgver(url: str, abbrev: str) -> tuple[str, str]:
    """Return (pkgver, full commit hash) for one commit of url's default branch.

    The publish job uses this to recompute a -git pkgver on the host. abbrev
    only selects the commit. The commit must exist upstream and be reachable
    from the default branch, and the pkgver comes from git describe of that
    commit with the same transform as update_git, abbreviated to the same
    length so the string can be compared exactly.
    """
    if not re.fullmatch(r"[0-9a-f]{4,40}", abbrev):
        raise SystemExit(f"refusing commit abbreviation {abbrev!r}")
    tmp = Path(tempfile.mkdtemp(prefix="pkgver-"))
    try:
        src = tmp / "src"
        subprocess.check_call(
            ["git", "clone", "--filter=blob:none", "--no-checkout", url, str(src)],
            stdout=subprocess.DEVNULL,
        )
        proc = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"{abbrev}^{{commit}}"],
            cwd=src,
            text=True,
            capture_output=True,
        )
        full = proc.stdout.strip()
        if proc.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}", full):
            raise SystemExit(f"commit {abbrev} does not exist upstream or is ambiguous")
        reachable = subprocess.run(
            ["git", "merge-base", "--is-ancestor", full, "HEAD"],
            cwd=src,
        )
        if reachable.returncode != 0:
            raise SystemExit(f"commit {full} is not on the upstream default branch")
        described = subprocess.check_output(
            ["git", "describe", "--long", "--tags", f"--abbrev={len(abbrev)}", full],
            cwd=src,
            text=True,
        ).strip()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return describe_to_pkgver(described), full


GIT_HASH_RE = re.compile(r"\.g([0-9a-f]{4,40})$")


def same_commit(old_ver: str, head: str) -> bool:
    """git describe abbreviates hashes to a length that can change between
    clones (7 vs 8 hex digits), so compare the commit, not the string."""
    match = GIT_HASH_RE.search(old_ver)
    return bool(match) and head.startswith(match.group(1))


def same_pkgver_commit(left: str, right: str) -> bool:
    """True when two git pkgver strings describe the same commit.

    The abbreviated hash may grow from 7 hex digits to 8 without the commit
    changing. The tag and revision prefix must still match.
    """
    if left == right:
        return True
    left_match = GIT_HASH_RE.search(left)
    right_match = GIT_HASH_RE.search(right)
    if not left_match or not right_match:
        return False
    if left[: left_match.start()] != right[: right_match.start()]:
        return False
    left_hash = left_match.group(1)
    right_hash = right_match.group(1)
    return left_hash.startswith(right_hash) or right_hash.startswith(left_hash)


def update_git(pkg: str, url: str) -> None:
    nvchecker_version(pkg)
    new_ver, head = git_pkgver(url, ["--long", "--tags"])
    text = read_pkgbuild(pkg)
    old_ver = read_var(text, "pkgver")
    old_rel = read_var(text, "pkgrel")
    old_full = full_version(old_ver, old_rel)
    new_full = full_version(new_ver, "1")
    if new_ver == old_ver or same_commit(old_ver, head):
        mark_unchanged(pkg, old_full)
        return
    require_newer(pkg, old_full, new_full)
    text = bump_pkgver(text, new_ver)
    write_pkgbuild(pkg, text)
    mark_changed(pkg, old_full, new_full)


GIT_REPOS = {
    "cuda-api-wrappers-git": "https://github.com/eyalroz/cuda-api-wrappers.git",
    "reflectcpp-git": "https://github.com/getml/reflect-cpp.git",
}

UPDATERS = {
    "maple-mono-nf-cn": update_maple,
    "memprocfs": update_memprocfs,
    "moonbit-bin": update_moonbit,
    "mfile-bin": update_mfile,
    "cuda-api-wrappers-git": lambda: update_git(
        "cuda-api-wrappers-git", GIT_REPOS["cuda-api-wrappers-git"]
    ),
    "reflectcpp-git": lambda: update_git("reflectcpp-git", GIT_REPOS["reflectcpp-git"]),
}


def self_check() -> None:
    """Offline checks for the pkgrel reset and git commit comparison."""
    sample = "pkgname=example\npkgver=1.0.0\npkgrel=4\n"
    bumped = bump_pkgver(sample, "1.2.0")
    if read_var(bumped, "pkgver") != "1.2.0" or read_var(bumped, "pkgrel") != "1":
        raise SystemExit("self-check: pkgrel was not reset to 1 on pkgver bump")
    if read_var(sample, "pkgrel") != "4":
        raise SystemExit("self-check: bump_pkgver mutated its input")
    if not same_commit("0.8.2.r0.gc2f2a2b", "c2f2a2b1234567890"):
        raise SystemExit("self-check: same_commit missed a full hash")
    if same_commit("0.8.2.r0.gc2f2a2b", "deadbeef1234567890"):
        raise SystemExit("self-check: same_commit matched a different commit")
    if not same_pkgver_commit("0.8.2.r0.gc2f2a2b", "0.8.2.r0.gc2f2a2b1"):
        raise SystemExit("self-check: abbreviated git pkgvers of one commit differed")
    if same_pkgver_commit("0.8.2.r0.gabcdef1", "0.8.2.r1.gabcdef12"):
        raise SystemExit("self-check: different git revisions compared equal")
    print("self-check ok")


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[1] == "--self-check":
        self_check()
        return 0
    if len(argv) != 2:
        print(f"usage: {argv[0]} <package>", file=sys.stderr)
        return 2
    pkg = argv[1]
    pkgdir = ROOT / pkg
    if not (pkgdir / "PKGBUILD").is_file():
        raise SystemExit(f"no PKGBUILD for {pkg}")
    if (pkgdir / ".aur-disabled").is_file():
        log(f"{pkg} is marked .aur-disabled; not updating or publishing")
        set_output("changed", "false")
        set_output("version", "")
        return 0
    updater = UPDATERS.get(pkg)
    if updater is None:
        raise SystemExit(f"{pkg} has no updater")
    updater()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv))
    except SystemExit:
        raise
    except Exception as exc:
        log(f"error: {exc}")
        raise
