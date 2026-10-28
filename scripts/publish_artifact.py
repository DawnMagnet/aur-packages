#!/usr/bin/env python3
"""Pass a sealed PKGBUILD from the build job to a clean publish checkout.

The test build runs in a second container that cannot see this file. The
publish job does not accept a .SRCINFO from the build; it regenerates one
with makepkg --printsrcinfo from the validated PKGBUILD.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

from alpmver import vercmp
import update_package
from update_package import GIT_HASH_RE, read_var, same_pkgver_commit, set_output

MAX_BYTES = 256 * 1024
PKG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
ASSIGN_RE = re.compile(r"^(pkgver|pkgrel)=([A-Za-z0-9._+]+)$")
SUM_START_RE = re.compile(
    r"^(?P<algo>md5|sha1|sha224|sha256|sha384|sha512|b2)sums(?:_[A-Za-z0-9_]+)?=\("
)
SUM_INSIDE_RE = re.compile(r"\A(?:\s*'(?:[0-9a-f]+|SKIP)'\s*,?\s*)+\Z")
# Single-line source arrays the updater rewrites (memprocfs). Multi-line
# source arrays must be unchanged; they are not masked.
SOURCE_RE = re.compile(
    r'^source(?:_[A-Za-z0-9_]+)?=\("https://[A-Za-z0-9._~:/?#@%+=-]{1,2000}"\)$'
)
HASH_LEN = {
    "md5": 32,
    "sha1": 40,
    "sha224": 56,
    "sha256": 64,
    "sha384": 96,
    "sha512": 128,
    "b2": 128,
}


def die(msg: str) -> None:
    raise SystemExit(msg)


def validate_package(package: str) -> None:
    if not PKG_RE.fullmatch(package):
        die(f"invalid package name: {package}")


def read_text_file(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        die(f"{path.name} must be a regular file")
    size = path.stat().st_size
    if size > MAX_BYTES:
        die(f"{path.name} is too large")
    data = path.read_bytes()
    if len(data) != size:
        die(f"{path.name} changed while it was being read")
    if b"\0" in data:
        die(f"{path.name} contains NUL")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        die(f"{path.name} is not utf-8: {exc}")
    raise AssertionError("unreachable")


def read_bytes(path: Path) -> bytes:
    return read_text_file(path).encode("utf-8")


def list_files(directory: Path) -> list[str]:
    if not directory.is_dir() or directory.is_symlink():
        die(f"{directory} is not a directory")
    names: list[str] = []
    for path in directory.iterdir():
        if path.is_symlink():
            die(f"refusing symlink {path.name}")
        if not path.is_file():
            die(f"refusing non-file {path.name}")
        names.append(path.name)
    return sorted(names)


def locate_artifact(artifact: Path) -> Path:
    manifest = artifact / "MANIFEST"
    if manifest.is_file() and not manifest.is_symlink():
        return artifact
    if not artifact.is_dir() or artifact.is_symlink():
        die(f"{artifact} is not a directory")
    subdirs = [path for path in artifact.iterdir() if path.is_dir() and not path.is_symlink()]
    files = [path for path in artifact.iterdir() if not path.is_dir()]
    if files or len(subdirs) != 1:
        die("unrecognized artifact layout")
    nested = subdirs[0] / "MANIFEST"
    if nested.is_file() and not nested.is_symlink():
        return subdirs[0]
    die("unrecognized artifact layout")


def consume_sum_block(lines: list[str], start: int) -> tuple[re.Match[str], str, int]:
    match = SUM_START_RE.match(lines[start])
    if match is None:
        die("internal error: not a checksum line")
    inside = lines[start].split("=(", 1)[1]
    if ")" in inside:
        body, after = inside.split(")", 1)
        if after.strip():
            die(f"trailing text after checksum array: {lines[start]!r}")
        if inside.count(")") != 1:
            die(f"malformed checksum array: {lines[start]!r}")
        return match, body, start + 1
    parts = [inside]
    index = start + 1
    while index < len(lines):
        line = lines[index]
        if ")" in line:
            body, after = line.split(")", 1)
            if after.strip():
                die(f"trailing text after checksum array: {line!r}")
            if line.count(")") != 1:
                die(f"malformed checksum array: {line!r}")
            parts.append(body)
            return match, "\n".join(parts), index + 1
        parts.append(line)
        index += 1
    die("unclosed checksum array")


def validate_sum_body(package: str, match: re.Match[str], body: str) -> None:
    if not SUM_INSIDE_RE.fullmatch(body):
        die(f"illegal checksum entry in {match.group(0)}")
    algo = match.group("algo")
    expected = HASH_LEN[algo]
    tokens = re.findall(r"'([^']*)'", body)
    if not tokens:
        die("empty checksum array")
    for token in tokens:
        if token == "SKIP":
            if not package.endswith("-git"):
                die(f"SKIP checksums are only allowed for -git packages, not {package}")
            continue
        if len(token) != expected:
            die(f"{algo} checksum length {len(token)} is not {expected}")


def normalize_pkgbuild(package: str, text: str) -> list[str]:
    """Mask pkgver, pkgrel, and validated checksum arrays."""
    lines = text.splitlines()
    out: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if SUM_START_RE.match(line):
            match, body, index = consume_sum_block(lines, index)
            validate_sum_body(package, match, body)
            out.append("@@SUMS@@")
            continue
        assign = ASSIGN_RE.fullmatch(line)
        if line.startswith("pkgver=") or line.startswith("pkgrel="):
            if assign is None:
                die(f"unsafe pkgver/pkgrel line: {line!r}")
            out.append(f"@@{assign.group(1)}@@")
            index += 1
            continue
        out.append(line)
        index += 1
    return out


def assert_pkgbuild_delta(
    package: str,
    baseline: str,
    updated: str,
    *,
    allow_source: bool,
    require_same_git_commit: bool = True,
) -> None:
    """Allow only pkgver, pkgrel, checksum (and optionally source URL) edits.

    require_same_git_commit is for stage and check-pkgbuild, where the baseline is
    the host-generated PKGBUILD of this run and makepkg's pkgver() may only change
    the hash length. import compares against main, which is the previous release,
    so a -git package moving to a newer upstream commit is expected there; import
    relies on assert_host_pkgver (exact match with the host-computed pkgver) and
    assert_not_downgrade instead.
    """
    if read_var(baseline, "pkgname") != package or read_var(updated, "pkgname") != package:
        die(f"PKGBUILD pkgname is not {package}")
    if require_same_git_commit and package.endswith("-git") and not same_pkgver_commit(
        read_var(baseline, "pkgver"), read_var(updated, "pkgver")
    ):
        die(
            f"{package} pkgver changed from {read_var(baseline, 'pkgver')} "
            f"to {read_var(updated, 'pkgver')}, which is not the same commit"
        )
    left = normalize_pkgbuild(package, baseline)
    right = normalize_pkgbuild(package, updated)
    if len(left) != len(right):
        die("PKGBUILD changed beyond pkgver, pkgrel, and checksums")
    for old, new in zip(left, right):
        if old == new:
            continue
        if allow_source and SOURCE_RE.fullmatch(old) and SOURCE_RE.fullmatch(new):
            continue
        die(f"PKGBUILD changed beyond pkgver, pkgrel, and checksums: {old!r} -> {new!r}")


SOURCE_ASSIGN_RE = re.compile(r"^(source(?:_[A-Za-z0-9_]+)?)=\((.*)$")
SOURCE_ANY_RE = re.compile(r"^\s*(?:declare\s+-a\s+|local\s+)?source(?:_[A-Za-z0-9_]*)?\+?=")
# One array item: a double-quoted string without quotes, backslashes,
# backticks or $( ; a single-quoted string; or a plain word.
SOURCE_ITEM_RE = re.compile(
    r"""\s*(?:"((?:[^"\\`$]|\$(?!\())*)"|'([^']*)'|([A-Za-z0-9._/:#=+@%~-]+))\s*"""
)


def parse_sources(package: str, text: str) -> dict[str, list[str]]:
    """Return the top-level source arrays of a PKGBUILD, items unexpanded.

    Pure text parsing; nothing is sourced or evaluated. Anything that is not
    a plain top-level source=( ... ) / source_<arch>=( ... ) array of quoted
    strings or words is refused.
    """
    lines = text.splitlines()
    arrays: dict[str, list[str]] = {}
    index = 0
    while index < len(lines):
        line = lines[index]
        match = SOURCE_ASSIGN_RE.match(line)
        if match is None:
            if SOURCE_ANY_RE.match(line):
                die(f"{package}: unsupported source assignment: {line!r}")
            index += 1
            continue
        name, rest = match.group(1), match.group(2)
        if name in arrays:
            die(f"{package}: {name} is assigned twice")
        body_parts = []
        while True:
            # A ) outside quotes closes the array.
            close = _closing_paren(rest)
            if close is not None:
                body_parts.append(rest[:close])
                if rest[close + 1 :].strip():
                    die(f"{package}: trailing text after {name} array: {lines[index]!r}")
                break
            body_parts.append(rest)
            index += 1
            if index >= len(lines):
                die(f"{package}: unclosed {name} array")
            rest = lines[index]
        arrays[name] = _source_items(package, name, "\n".join(body_parts))
        index += 1
    return arrays


def _closing_paren(fragment: str) -> int | None:
    quote = None
    for pos, char in enumerate(fragment):
        if quote:
            if char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char == ")":
            return pos
    return None


def _source_items(package: str, name: str, body: str) -> list[str]:
    items: list[str] = []
    pos = 0
    body = body.strip()
    while pos < len(body):
        match = SOURCE_ITEM_RE.match(body, pos)
        if match is None or match.end() == pos:
            die(f"{package}: unsupported item in {name}: {body[pos:pos + 60]!r}")
        value = next(g for g in match.groups() if g is not None)
        items.append(value)
        pos = match.end()
    return items


def assert_sources(package: str, expected: dict[str, list[str]], updated: str, origin: str) -> None:
    """Require the artifact source arrays to equal expected exactly."""
    actual = parse_sources(package, updated)
    for name in sorted(set(expected) | set(actual)):
        if name not in actual:
            die(f"{package}: artifact is missing {name} (expected from {origin})")
        if name not in expected:
            die(f"{package}: artifact adds {name}, which {origin} does not have")
        want, got = expected[name], actual[name]
        for pos in range(max(len(want), len(got))):
            if pos >= len(got):
                die(f"{package}: {name}[{pos}] is missing; {origin} has {want[pos]!r}")
            if pos >= len(want):
                die(f"{package}: {name}[{pos}] {got[pos]!r} is extra; {origin} has {len(want)} items")
            if want[pos] != got[pos]:
                die(f"{package}: {name}[{pos}] is {got[pos]!r}, {origin} has {want[pos]!r}")


def decode_expected_sources(package: str, raw: str | None) -> dict[str, list[str]]:
    if not raw:
        die(f"{package}: missing host-computed --expected-sources; refusing to import")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        die(f"{package}: invalid --expected-sources: {exc}")
    if not isinstance(data, dict) or not data or not all(
        isinstance(k, str) and re.fullmatch(r"source(?:_[A-Za-z0-9_]+)?", k)
        and isinstance(v, list) and all(isinstance(i, str) for i in v)
        for k, v in data.items()
    ):
        die(f"{package}: invalid --expected-sources")
    return data


def assert_baseline_version(package: str, baseline: str, candidate: str) -> None:
    """stage / check-pkgbuild: the build may not choose its own version.

    The baseline is the PKGBUILD update_package.py wrote on the host in this
    run. Non -git packages must keep its pkgver and pkgrel exactly (makepkg
    has no pkgver() for them). -git packages keep pkgrel exactly; their pkgver
    may only grow the hash (same_pkgver_commit, checked in the delta).
    """
    base_ver, base_rel = read_var(baseline, "pkgver"), read_var(baseline, "pkgrel")
    cand_ver, cand_rel = read_var(candidate, "pkgver"), read_var(candidate, "pkgrel")
    if not package.endswith("-git") and cand_ver != base_ver:
        die(f"{package}: build changed pkgver from the host value {base_ver} to {cand_ver}")
    if cand_rel != base_rel:
        die(f"{package}: build changed pkgrel from the host value {base_rel} to {cand_rel}")


def assert_pkgrel_rule(package: str, current: str, updated: str) -> None:
    """import: pkgrel follows the updater, never the artifact.

    Same pkgver as main -> main's pkgrel; new pkgver -> 1. A manual pkgrel
    bump has to land on main through a PR. memprocfs is the exception: its
    pkgrel is the upstream asset date, which assert_host_pkgver already pins
    to the host-computed value. For -git packages "same pkgver" means the
    same commit (the abbreviated hash may grow).
    """
    if package == "memprocfs":
        return
    old_ver, old_rel = read_var(current, "pkgver"), read_var(current, "pkgrel")
    new_ver, new_rel = read_var(updated, "pkgver"), read_var(updated, "pkgrel")
    if package.endswith("-git"):
        same = same_pkgver_commit(old_ver, new_ver)
    else:
        same = old_ver == new_ver
    want = old_rel if same else "1"
    if new_rel != want:
        if same:
            reason = f"pkgver {new_ver} is unchanged, so pkgrel must stay main's {old_rel}"
        else:
            reason = f"pkgver changed {old_ver} -> {new_ver}, so pkgrel must be 1"
        die(f"{package}: artifact pkgrel {new_rel} refused: {reason}; bump pkgrel on main through a PR")


def assert_not_downgrade(package: str, current: str, updated: str) -> None:
    old_ver = read_var(current, "pkgver")
    old_rel = read_var(current, "pkgrel")
    new_ver = read_var(updated, "pkgver")
    new_rel = read_var(updated, "pkgrel")
    if package.endswith("-git") and old_rel == new_rel and same_pkgver_commit(old_ver, new_ver):
        return
    if vercmp(f"{new_ver}-{new_rel}", f"{old_ver}-{old_rel}") < 0:
        die(
            f"refusing to replace {package} {old_ver}-{old_rel} "
            f"with older {new_ver}-{new_rel}"
        )


def parse_manifest(text: str) -> dict[str, str]:
    if not text.endswith("\n"):
        die("MANIFEST must end with a newline")
    data: dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line:
            die("invalid MANIFEST")
        key, value = line.split("=", 1)
        if not re.fullmatch(r"[a-z]+", key) or key in data:
            die("invalid MANIFEST")
        data[key] = value
    extra = set(data) - {"status", "pkgbuild"}
    if extra:
        die(f"unexpected MANIFEST keys: {sorted(extra)}")
    return data


def write_manifest(stage: Path, status: str, files: dict[str, bytes]) -> None:
    lines = [f"status={status}"]
    if "PKGBUILD" in files:
        lines.append("pkgbuild=" + hashlib.sha256(files["PKGBUILD"]).hexdigest())
    path = stage / "MANIFEST"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(path, 0o644)


def empty_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if any(path.iterdir()):
        die("stage directory is not empty")


def check_pkgbuild(
    package: str,
    baseline: Path,
    candidate: Path,
    old_version_file: Path | None,
) -> None:
    validate_package(package)
    assert_pkgbuild_delta(
        package,
        read_text_file(baseline),
        read_text_file(candidate),
        allow_source=False,
    )
    assert_baseline_version(package, read_text_file(baseline), read_text_file(candidate))
    if old_version_file is None:
        return
    old = read_text_file(old_version_file).strip()
    if not re.fullmatch(r"[A-Za-z0-9._+-]+", old):
        die("refusing old version value")
    updated = read_text_file(candidate)
    new = f"{read_var(updated, 'pkgver')}-{read_var(updated, 'pkgrel')}"
    if vercmp(new, old) <= 0:
        die(f"refusing to publish {package}: {new} is not newer than {old}")


def stage(package: str, baseline: Path, build_out: Path, stage_dir: Path) -> None:
    validate_package(package)
    names = list_files(build_out)
    if names != ["PKGBUILD"]:
        die(f"build output must contain only PKGBUILD, found {names}")
    pkg_bytes = read_bytes(build_out / "PKGBUILD")
    assert_pkgbuild_delta(
        package,
        read_text_file(baseline),
        pkg_bytes.decode("utf-8"),
        allow_source=False,
    )
    assert_baseline_version(package, read_text_file(baseline), pkg_bytes.decode("utf-8"))
    empty_dir(stage_dir)
    target = stage_dir / "PKGBUILD"
    target.write_bytes(pkg_bytes)
    os.chmod(target, 0o644)
    write_manifest(stage_dir, "publish", {"PKGBUILD": pkg_bytes})


def stage_skip(stage_dir: Path) -> None:
    empty_dir(stage_dir)
    write_manifest(stage_dir, "skip", {})


def check_hash(label: str, data: bytes, expected: str | None) -> None:
    if expected is None or not re.fullmatch(r"[0-9a-f]{64}", expected or ""):
        die(f"MANIFEST is missing a sha256 for {label}")
    if hashlib.sha256(data).hexdigest() != expected:
        die(f"{label} hash does not match MANIFEST")


def write_into(dest: Path, name: str, data: bytes) -> None:
    target = dest / name
    if target.is_symlink():
        die(f"{name} in the checkout is a symlink")
    if name == "PKGBUILD" and not target.is_file():
        die("checkout is missing PKGBUILD")
    tmp = dest / f".{name}.import"
    tmp.write_bytes(data)
    os.chmod(tmp, 0o644)
    os.replace(tmp, target)


def assert_host_pkgver(
    package: str,
    updated: str,
    expected_pkgver: str | None,
    expected_commit: str | None,
    expected_pkgrel: str | None = None,
) -> None:
    """Require the sealed pkgver to equal the pkgver the publish host computed.

    import masks pkgver when it compares PKGBUILDs, and vercmp alone cannot
    tell two hashes of the same rN apart or stop a huge rN that would block
    every later update. For -git packages the publish job recomputes the
    pkgver from upstream (publish_artifact.py expect) and passes it in here.
    For the release packages (maple-mono-nf-cn, memprocfs, moonbit-bin) it
    recomputes pkgver and pkgrel (publish_artifact.py expect-release) and
    both must match exactly. A missing value fails closed for all of them.
    Other packages are unchanged unless a value is passed.
    """
    is_git = package.endswith("-git")
    is_release = package in update_package.RELEASE_PACKAGES
    if not expected_pkgver:
        if is_git or is_release:
            die(f"{package}: missing host-computed --expected-pkgver; refusing to import")
        return
    if not update_package.PKGVER_RE.fullmatch(expected_pkgver):
        die(f"{package}: refusing expected pkgver {expected_pkgver!r}")
    actual = read_var(updated, "pkgver")
    if actual != expected_pkgver:
        die(
            f"{package}: artifact pkgver {actual} does not match the host-computed "
            f"pkgver {expected_pkgver}"
        )
    if is_release:
        if not expected_pkgrel or not update_package.PKGREL_RE.fullmatch(expected_pkgrel):
            die(f"{package}: missing or invalid host-computed --expected-pkgrel; refusing to import")
        actual_rel = read_var(updated, "pkgrel")
        if actual_rel != expected_pkgrel:
            die(
                f"{package}: artifact pkgrel {actual_rel} does not match the host-computed "
                f"pkgrel {expected_pkgrel}"
            )
        return
    if not is_git:
        return
    if not expected_commit or not re.fullmatch(r"[0-9a-f]{40}", expected_commit):
        die(f"{package}: missing or invalid host-computed --expected-commit; refusing to import")
    match = GIT_HASH_RE.search(actual)
    if match is None or not expected_commit.startswith(match.group(1)):
        die(f"{package}: artifact pkgver {actual} is not commit {expected_commit}")


def read_artifact(package: str, artifact: Path) -> bytes | None:
    """Validate the artifact layout. Return the PKGBUILD bytes, or None for skip."""
    validate_package(package)
    root = locate_artifact(artifact)
    names = list_files(root)
    manifest = parse_manifest(read_text_file(root / "MANIFEST"))
    status = manifest.get("status")
    if status == "skip":
        if names != ["MANIFEST"]:
            die("skip artifact must contain only MANIFEST")
        return None
    if status != "publish":
        die("MANIFEST status must be publish or skip")
    # .SRCINFO from the build is never accepted. The publish job regenerates it.
    if names != ["MANIFEST", "PKGBUILD"]:
        die(f"unexpected artifact files: {names}")
    pkg_bytes = read_bytes(root / "PKGBUILD")
    check_hash("PKGBUILD", pkg_bytes, manifest.get("pkgbuild"))
    return pkg_bytes


def expect(package: str, artifact: Path) -> None:
    """Compute the expected pkgver for a -git artifact on the publish host.

    The artifact pkgver only names a commit. That commit must exist on the
    upstream default branch, and the expected pkgver is git describe of it,
    computed here. Writes the outputs pkgver and commit (empty for skip).
    """
    validate_package(package)
    url = update_package.GIT_REPOS.get(package)
    if url is None:
        die(f"{package} is not a -git package with a known upstream")
    pkg_bytes = read_artifact(package, artifact)
    if pkg_bytes is None:
        set_output("pkgver", "")
        set_output("commit", "")
        return
    for line in pkg_bytes.decode("utf-8").splitlines():
        if line.startswith("pkgver=") and ASSIGN_RE.fullmatch(line) is None:
            die(f"unsafe pkgver/pkgrel line: {line!r}")
    candidate = read_var(pkg_bytes.decode("utf-8"), "pkgver")
    match = GIT_HASH_RE.search(candidate)
    if match is None:
        die(f"{package}: artifact pkgver {candidate!r} does not end in a git hash")
    pkgver, commit = update_package.git_commit_pkgver(url, match.group(1))
    set_output("pkgver", pkgver)
    set_output("commit", commit)


def expect_release(package: str, artifact: Path, main_pkgbuild: Path) -> None:
    """Compute the expected pkgver and pkgrel of a release package on the host.

    Runs the updater's version logic read-only (update_package.expected_release)
    against the PKGBUILD on main. The artifact is only read to see whether it is
    a skip marker, in which case nothing is downloaded and both outputs are
    empty. Writes the outputs pkgver and pkgrel.
    """
    validate_package(package)
    if package not in update_package.RELEASE_PACKAGES:
        die(f"{package} is not a release package")
    pkg_bytes = read_artifact(package, artifact)
    if pkg_bytes is None:
        set_output("pkgver", "")
        set_output("pkgrel", "")
        set_output("sources", "")
        return
    main_text = read_text_file(main_pkgbuild)
    if read_var(main_text, "pkgname") != package:
        die(f"{main_pkgbuild} is not the PKGBUILD of {package}")
    pkgver, pkgrel, expected_text = update_package.expected_release_pkgbuild(package, main_text)
    sources = parse_sources(package, expected_text)
    if not sources:
        die(f"{package}: the expected PKGBUILD has no source array")
    set_output("pkgver", pkgver)
    set_output("pkgrel", pkgrel)
    set_output("sources", json.dumps(sources, separators=(",", ":"), sort_keys=True))


def require_registered(package: str) -> None:
    """Refuse a package this repo does not know how to check.

    Import pins pkgver, pkgrel and source arrays only for -git packages with a
    known upstream (update_package.GIT_REPOS) and for update_package's
    RELEASE_PACKAGES. Anything else would skip those checks and publish
    unverified, so a new package that was never registered fails loudly here,
    even when its artifact is only a skip marker.
    """
    if package.endswith("-git"):
        if package not in update_package.GIT_REPOS:
            die(
                f"{package}: -git package has no upstream in update_package.GIT_REPOS; "
                "register it before it can be imported"
            )
        return
    if package not in update_package.RELEASE_PACKAGES:
        die(
            f"{package}: not a -git package and not in update_package.RELEASE_PACKAGES; "
            "register it before it can be imported"
        )


def import_artifact(
    package: str,
    artifact: Path,
    dest: Path,
    expected_pkgver: str | None = None,
    expected_commit: str | None = None,
    expected_pkgrel: str | None = None,
    expected_sources: str | None = None,
) -> None:
    validate_package(package)
    require_registered(package)
    if dest.is_symlink() or not dest.is_dir():
        die("dest must be the package directory")
    dest = dest.resolve()
    if dest.name != package:
        die("dest directory name must match the package")
    pkg_bytes = read_artifact(package, artifact)
    if pkg_bytes is None:
        set_output("publish", "false")
        return
    pkgbuild = pkg_bytes.decode("utf-8")
    current_path = dest / "PKGBUILD"
    if current_path.is_symlink() or not current_path.is_file():
        die("checkout PKGBUILD must be a regular file")
    current = read_text_file(current_path)
    assert_host_pkgver(package, pkgbuild, expected_pkgver, expected_commit, expected_pkgrel)
    # Lock the source arrays. -git: exactly main's. Release packages: exactly
    # what the host-side updater produced. Parsed only, never evaluated.
    if package.endswith("-git"):
        assert_sources(package, parse_sources(package, current), pkgbuild, "main")
    elif package in update_package.RELEASE_PACKAGES:
        assert_sources(
            package, decode_expected_sources(package, expected_sources), pkgbuild, "the host"
        )
    assert_pkgbuild_delta(package, current, pkgbuild, allow_source=True, require_same_git_commit=False)
    if package.endswith("-git") or package in update_package.RELEASE_PACKAGES:
        assert_pkgrel_rule(package, current, pkgbuild)
    assert_not_downgrade(package, current, pkgbuild)
    write_into(dest, "PKGBUILD", pkg_bytes)
    set_output("publish", "true")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    check_cmd = sub.add_parser("check-pkgbuild")
    check_cmd.add_argument("--package", required=True)
    check_cmd.add_argument("--baseline", required=True, type=Path)
    check_cmd.add_argument("--candidate", required=True, type=Path)
    check_cmd.add_argument("--old-version-file", type=Path)

    stage_cmd = sub.add_parser("stage")
    stage_cmd.add_argument("--package", required=True)
    stage_cmd.add_argument("--baseline", required=True, type=Path)
    stage_cmd.add_argument("--build-out", required=True, type=Path)
    stage_cmd.add_argument("--stage", required=True, type=Path)

    skip_cmd = sub.add_parser("stage-skip")
    skip_cmd.add_argument("--stage", required=True, type=Path)

    import_cmd = sub.add_parser("import")
    import_cmd.add_argument("--package", required=True)
    import_cmd.add_argument("--artifact", required=True, type=Path)
    import_cmd.add_argument("--dest", required=True, type=Path)
    import_cmd.add_argument("--expected-pkgver", default="")
    import_cmd.add_argument("--expected-commit", default="")
    import_cmd.add_argument("--expected-pkgrel", default="")
    import_cmd.add_argument("--expected-sources", default="")

    expect_cmd = sub.add_parser("expect")
    expect_cmd.add_argument("--package", required=True)
    expect_cmd.add_argument("--artifact", required=True, type=Path)

    release_cmd = sub.add_parser("expect-release")
    release_cmd.add_argument("--package", required=True)
    release_cmd.add_argument("--artifact", required=True, type=Path)
    release_cmd.add_argument("--main-pkgbuild", required=True, type=Path)

    args = parser.parse_args(argv[1:])
    if args.cmd == "check-pkgbuild":
        old = args.old_version_file
        if old is not None and not old.is_file():
            old = None
        check_pkgbuild(args.package, args.baseline, args.candidate, old)
    elif args.cmd == "stage":
        stage(args.package, args.baseline, args.build_out, args.stage)
    elif args.cmd == "stage-skip":
        stage_skip(args.stage)
    elif args.cmd == "expect":
        expect(args.package, args.artifact)
    elif args.cmd == "expect-release":
        expect_release(args.package, args.artifact, args.main_pkgbuild)
    else:
        import_artifact(
            args.package,
            args.artifact,
            args.dest,
            args.expected_pkgver or None,
            args.expected_commit or None,
            args.expected_pkgrel or None,
            args.expected_sources or None,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
