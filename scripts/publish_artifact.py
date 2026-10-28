#!/usr/bin/env python3
"""Move PKGBUILD and .SRCINFO from a build job to a clean publish checkout.

The build container can rewrite its work directory. This script is the gate
that runs on the trusted runner:

* stage compares the container output with the PKGBUILD produced by
  update_package.py and keeps checksum edits (git packages may also change
  the abbreviated commit in pkgver).
* import checks the manifest, file types, pkgname, .SRCINFO, SKIP policy,
  and refuses a downgrade against the clean checkout.
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
SUM_RE = re.compile(
    r"^(md5|sha1|sha224|sha256|sha384|sha512|b2)sums(_[A-Za-z0-9_]+)?\+?="
)
SKIP_RE = re.compile(r"sums(_[A-Za-z0-9_]+)?=\(.*SKIP", re.S)
SRCINFO_SKIP_RE = re.compile(r"(?m)^[ \t]*[A-Za-z0-9_]*sums.*\bSKIP\b")
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


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
    text = read_text_file(path)
    return text.encode("utf-8")


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


def strip_pkgbuild(text: str, *, drop_pkgver: bool) -> str:
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if drop_pkgver and line.startswith("pkgver="):
            index += 1
            continue
        if SUM_RE.match(line):
            balance = line.count("(") - line.count(")")
            index += 1
            while balance > 0 and index < len(lines):
                balance += lines[index].count("(") - lines[index].count(")")
                index += 1
            continue
        out.append(line)
        index += 1
    return "".join(out)


def assert_pkgbuild_delta(package: str, baseline: str, updated: str) -> None:
    if read_var(baseline, "pkgname") != package:
        die(f"baseline pkgname is not {package}")
    base_ver = read_var(baseline, "pkgver")
    new_ver = read_var(updated, "pkgver")
    if package.endswith("-git"):
        if not same_pkgver_commit(base_ver, new_ver):
            die(
                f"{package} pkgver changed from {base_ver} to {new_ver}, "
                "which is not the same commit"
            )
        drop_pkgver = True
    else:
        if base_ver != new_ver:
            die(f"{package} pkgver changed from {base_ver} to {new_ver}")
        drop_pkgver = False
    if read_var(baseline, "pkgrel") != read_var(updated, "pkgrel"):
        die(f"{package} pkgrel changed during the build")
    left = strip_pkgbuild(baseline, drop_pkgver=drop_pkgver)
    right = strip_pkgbuild(updated, drop_pkgver=drop_pkgver)
    if left != right:
        die(f"PKGBUILD for {package} changed beyond checksums")


def srcinfo_values(text: str, key: str) -> list[str]:
    prefix = f"{key} = "
    values: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(prefix):
            values.append(stripped[len(prefix) :].strip())
    return values


def assert_srcinfo(package: str, pkgbuild: str, srcinfo: str) -> None:
    pkgver = read_var(pkgbuild, "pkgver")
    pkgrel = read_var(pkgbuild, "pkgrel")
    pkgname = read_var(pkgbuild, "pkgname")
    if pkgname != package:
        die(f"PKGBUILD pkgname {pkgname} != {package}")
    if srcinfo_values(srcinfo, "pkgbase") != [package]:
        die(f".SRCINFO pkgbase does not match {package}")
    if package not in srcinfo_values(srcinfo, "pkgname"):
        die(f".SRCINFO is missing pkgname {package}")
    if srcinfo_values(srcinfo, "pkgver") != [pkgver]:
        die(".SRCINFO pkgver does not match PKGBUILD")
    if srcinfo_values(srcinfo, "pkgrel") != [pkgrel]:
        die(".SRCINFO pkgrel does not match PKGBUILD")


def assert_no_skip(package: str, pkgbuild: str, srcinfo: str) -> None:
    if package.endswith("-git"):
        return
    if SKIP_RE.search(pkgbuild) or SRCINFO_SKIP_RE.search(srcinfo):
        die(f"refusing SKIP checksums for {package}")


def assert_not_downgrade(package: str, current: str, updated: str) -> None:
    old_ver = read_var(current, "pkgver")
    old_rel = read_var(current, "pkgrel")
    new_ver = read_var(updated, "pkgver")
    new_rel = read_var(updated, "pkgrel")
    if (
        package.endswith("-git")
        and old_rel == new_rel
        and same_pkgver_commit(old_ver, new_ver)
    ):
        return
    if vercmp(f"{new_ver}-{new_rel}", f"{old_ver}-{old_rel}") < 0:
        die(
            f"refusing to replace {package} {old_ver}-{old_rel} "
            f"with older {new_ver}-{new_rel}"
        )


def parse_manifest(text: str) -> dict[str, str]:
    data: dict[str, str] = {}
    if not text.endswith("\n"):
        die("MANIFEST must end with a newline")
    for line in text.splitlines():
        if "=" not in line:
            die("invalid MANIFEST")
        key, value = line.split("=", 1)
        if not re.fullmatch(r"[a-z]+", key) or key in data:
            die("invalid MANIFEST")
        data[key] = value
    extra = set(data) - {"status", "pkgbuild", "srcinfo"}
    if extra:
        die(f"unexpected MANIFEST keys: {sorted(extra)}")
    return data


def write_manifest(stage: Path, status: str, files: dict[str, bytes]) -> None:
    lines = [f"status={status}"]
    if "PKGBUILD" in files:
        lines.append("pkgbuild=" + hashlib.sha256(files["PKGBUILD"]).hexdigest())
    if ".SRCINFO" in files:
        lines.append("srcinfo=" + hashlib.sha256(files[".SRCINFO"]).hexdigest())
    path = stage / "MANIFEST"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(path, 0o644)


def empty_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if any(path.iterdir()):
        die("stage directory is not empty")


def stage(package: str, baseline: Path, build_out: Path, stage_dir: Path) -> None:
    validate_package(package)
    names = list_files(build_out)
    if names != [".SRCINFO", "PKGBUILD"]:
        die(f"build output must contain only PKGBUILD and .SRCINFO, found {names}")
    pkg_bytes = read_bytes(build_out / "PKGBUILD")
    src_bytes = read_bytes(build_out / ".SRCINFO")
    pkgbuild = pkg_bytes.decode("utf-8")
    srcinfo = src_bytes.decode("utf-8")
    base = read_text_file(baseline)
    assert_pkgbuild_delta(package, base, pkgbuild)
    assert_srcinfo(package, pkgbuild, srcinfo)
    assert_no_skip(package, pkgbuild, srcinfo)
    empty_dir(stage_dir)
    files = {"PKGBUILD": pkg_bytes, ".SRCINFO": src_bytes}
    for name, data in files.items():
        target = stage_dir / name
        target.write_bytes(data)
        os.chmod(target, 0o644)
    write_manifest(stage_dir, "publish", files)


def stage_skip(stage_dir: Path) -> None:
    empty_dir(stage_dir)
    write_manifest(stage_dir, "skip", {})


def check_hash(label: str, data: bytes, expected: str | None) -> None:
    if expected is None or not HEX64_RE.fullmatch(expected):
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
    if names != [".SRCINFO", "MANIFEST", "PKGBUILD"]:
        die(f"unexpected artifact files: {names}")
    pkg_bytes = read_bytes(root / "PKGBUILD")
    src_bytes = read_bytes(root / ".SRCINFO")
    check_hash("PKGBUILD", pkg_bytes, manifest.get("pkgbuild"))
    check_hash(".SRCINFO", src_bytes, manifest.get("srcinfo"))
    pkgbuild = pkg_bytes.decode("utf-8")
    srcinfo = src_bytes.decode("utf-8")
    assert_srcinfo(package, pkgbuild, srcinfo)
    assert_no_skip(package, pkgbuild, srcinfo)
    current_path = dest / "PKGBUILD"
    if current_path.is_symlink() or not current_path.is_file():
        die("checkout PKGBUILD must be a regular file")
    assert_not_downgrade(package, read_text_file(current_path), pkgbuild)
    write_into(dest, "PKGBUILD", pkg_bytes)
    write_into(dest, ".SRCINFO", src_bytes)
    set_output("publish", "true")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

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
    if args.cmd == "stage":
        stage(args.package, args.baseline, args.build_out, args.stage)
    elif args.cmd == "stage-skip":
        stage_skip(args.stage)
    else:
        import_artifact(args.package, args.artifact, args.dest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
