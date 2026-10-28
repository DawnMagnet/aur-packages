#!/usr/bin/env python3
"""Pass a sealed PKGBUILD from the build job to a clean publish checkout.

The test build runs in a second container that cannot see this file. The
publish job does not accept a .SRCINFO from the build; it regenerates one
with makepkg --printsrcinfo from the validated PKGBUILD.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from pathlib import Path

from alpmver import vercmp
from update_package import read_var, same_pkgver_commit, set_output

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
    relies on assert_not_downgrade instead.
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


def import_artifact(package: str, artifact: Path, dest: Path) -> None:
    validate_package(package)
    if dest.is_symlink() or not dest.is_dir():
        die("dest must be the package directory")
    dest = dest.resolve()
    if dest.name != package:
        die("dest directory name must match the package")
    root = locate_artifact(artifact)
    names = list_files(root)
    manifest = parse_manifest(read_text_file(root / "MANIFEST"))
    status = manifest.get("status")
    if status == "skip":
        if names != ["MANIFEST"]:
            die("skip artifact must contain only MANIFEST")
        set_output("publish", "false")
        return
    if status != "publish":
        die("MANIFEST status must be publish or skip")
    # .SRCINFO from the build is never accepted. The publish job regenerates it.
    if names != ["MANIFEST", "PKGBUILD"]:
        die(f"unexpected artifact files: {names}")
    pkg_bytes = read_bytes(root / "PKGBUILD")
    check_hash("PKGBUILD", pkg_bytes, manifest.get("pkgbuild"))
    pkgbuild = pkg_bytes.decode("utf-8")
    current_path = dest / "PKGBUILD"
    if current_path.is_symlink() or not current_path.is_file():
        die("checkout PKGBUILD must be a regular file")
    current = read_text_file(current_path)
    assert_pkgbuild_delta(package, current, pkgbuild, allow_source=True, require_same_git_commit=False)
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
    else:
        import_artifact(args.package, args.artifact, args.dest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
