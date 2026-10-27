#!/usr/bin/env python3
"""Update one package directory to the current upstream version.

The script reads nvchecker.toml for GitHub coordinates, then rewrites pkgver,
pkgrel, and (for memprocfs) source URLs. Checksums for non-VCS packages are
filled afterwards by updpkgsums inside the Arch test build. A package is left
untouched when the upstream version is not strictly newer.
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


def choose_release_version(name: str) -> str:
    entry = load_nv_entry(name)
    if entry.get("source") != "github":
        raise SystemExit(f"{name} is not a github nvchecker entry")
    repo = entry["github"]
    prefix = entry.get("prefix") or ""
    api_version = stable_github_version(repo, prefix)
    log(f"github releases {repo} -> {api_version}")
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
    text = set_var(text, "pkgver", new_ver)
    text = set_var(text, "pkgrel", "1")
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


def probe_moonbit() -> dict:
    """Download the latest toolchain and read `moon version`.

    MoonBit's installer accepts a version segment, but dated paths such as
    binaries/0.1.20261023/ return S3 AccessDenied. The only published channels
    are `latest` and `nightly`, so the PKGBUILD keeps the latest URL and pins
    the checksum of the tarball that produced pkgver.
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
    text = set_var(text, "pkgver", new_ver)
    text = set_var(text, "pkgrel", "1")
    write_pkgbuild("moonbit-bin", text)
    mark_changed("moonbit-bin", old_full, new_full)


def git_pkgver(url: str, describe_args: list[str]) -> str:
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
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    # Same transform as the PKGBUILD pkgver() functions.
    pkgver = subprocess.check_output(
        ["sed", r"s/^v//;s/\([^-]*-g\)/r\1/;s/-/./g"],
        input=described + "\n",
        text=True,
    ).strip()
    if not pkgver or not re.fullmatch(r"[A-Za-z0-9._+]+", pkgver):
        raise SystemExit(f"refusing pkgver {pkgver!r} from git describe {described!r}")
    return pkgver


def update_git(pkg: str, url: str) -> None:
    nvchecker_version(pkg)
    new_ver = git_pkgver(url, ["--long", "--tags"])
    text = read_pkgbuild(pkg)
    old_ver = read_var(text, "pkgver")
    old_rel = read_var(text, "pkgrel")
    old_full = full_version(old_ver, old_rel)
    new_full = full_version(new_ver, "1")
    if new_ver == old_ver:
        mark_unchanged(pkg, old_full)
        return
    require_newer(pkg, old_full, new_full)
    text = set_var(text, "pkgver", new_ver)
    text = set_var(text, "pkgrel", "1")
    write_pkgbuild(pkg, text)
    mark_changed(pkg, old_full, new_full)


UPDATERS = {
    "maple-mono-nf-cn": update_maple,
    "memprocfs": update_memprocfs,
    "moonbit-bin": update_moonbit,
    "cuda-api-wrappers-git": lambda: update_git(
        "cuda-api-wrappers-git",
        "https://github.com/eyalroz/cuda-api-wrappers.git",
    ),
    "reflectcpp-git": lambda: update_git(
        "reflectcpp-git",
        "https://github.com/getml/reflect-cpp.git",
    ),
}


def main(argv: list[str]) -> int:
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
