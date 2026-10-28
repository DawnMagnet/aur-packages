#!/usr/bin/env python3
"""Tests for the PKGBUILD/.SRCINFO artifact gate."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

import publish_artifact as gate

SRCINFO = """pkgbase = maple-mono-nf-cn
	pkgdesc = font
	pkgver = 7.9
	pkgrel = 1

pkgname = maple-mono-nf-cn
"""

BASELINE = """pkgname=maple-mono-nf-cn
pkgver=7.9
pkgrel=1
source=("https://example.invalid/MapleMono.zip")
sha256sums=('aaa'
            'bbb')
"""


def write_pair(directory: Path, pkgbuild: str, srcinfo: str = SRCINFO) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "PKGBUILD").write_text(pkgbuild, encoding="utf-8")
    (directory / ".SRCINFO").write_text(srcinfo, encoding="utf-8")


class StageTest(unittest.TestCase):
    def test_checksum_edit_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            baseline = root / "baseline"
            baseline.write_text(BASELINE, encoding="utf-8")
            out = root / "out"
            updated = BASELINE.replace("'aaa'", "'ccc'").replace("'bbb'", "'ddd'")
            write_pair(out, updated)
            stage = root / "stage"
            gate.stage("maple-mono-nf-cn", baseline, out, stage)
            manifest = (stage / "MANIFEST").read_text(encoding="utf-8")
            self.assertIn("status=publish", manifest)
            digest = hashlib.sha256(updated.encode()).hexdigest()
            self.assertIn(f"pkgbuild={digest}", manifest)
            self.assertEqual(sorted(path.name for path in stage.iterdir()), [".SRCINFO", "MANIFEST", "PKGBUILD"])

    def test_source_url_edit_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            baseline = root / "baseline"
            baseline.write_text(BASELINE, encoding="utf-8")
            out = root / "out"
            write_pair(out, BASELINE.replace("example.invalid", "evil.invalid"))
            with self.assertRaises(SystemExit) as caught:
                gate.stage("maple-mono-nf-cn", baseline, out, root / "stage")
            self.assertIn("beyond checksums", str(caught.exception))

    def test_pkgver_edit_is_rejected_for_release_packages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            baseline = root / "baseline"
            baseline.write_text(BASELINE, encoding="utf-8")
            out = root / "out"
            write_pair(out, BASELINE.replace("pkgver=7.9", "pkgver=9.9"))
            with self.assertRaises(SystemExit) as caught:
                gate.stage("maple-mono-nf-cn", baseline, out, root / "stage")
            self.assertIn("pkgver changed", str(caught.exception))

    def test_git_abbrev_growth_is_accepted_and_a_new_commit_is_not(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            baseline_text = (
                "pkgname=cuda-api-wrappers-git\n"
                "pkgver=0.8.2.r0.gc2f2a2b\n"
                "pkgrel=1\n"
                "sha256sums=('SKIP')\n"
            )
            srcinfo = SRCINFO.replace("maple-mono-nf-cn", "cuda-api-wrappers-git").replace(
                "pkgver = 7.9", "pkgver = 0.8.2.r0.gc2f2a2b1"
            )
            baseline = root / "baseline"
            baseline.write_text(baseline_text, encoding="utf-8")
            out = root / "out"
            write_pair(out, baseline_text.replace("gc2f2a2b", "gc2f2a2b1"), srcinfo)
            gate.stage("cuda-api-wrappers-git", baseline, out, root / "stage")
            bad = root / "bad"
            bad_src = srcinfo.replace("gc2f2a2b1", "deadbeef")
            write_pair(bad, baseline_text.replace("gc2f2a2b", "deadbeef"), bad_src)
            with self.assertRaises(SystemExit) as caught:
                gate.stage("cuda-api-wrappers-git", baseline, bad, root / "stage2")
            self.assertIn("not the same commit", str(caught.exception))

    def test_skip_checksum_is_rejected_for_non_git(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            baseline = root / "baseline"
            baseline.write_text(BASELINE, encoding="utf-8")
            out = root / "out"
            write_pair(out, BASELINE.replace("'aaa'", "'SKIP'"))
            with self.assertRaises(SystemExit) as caught:
                gate.stage("maple-mono-nf-cn", baseline, out, root / "stage")
            self.assertIn("SKIP", str(caught.exception))

    def test_symlink_and_extra_file_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            baseline = root / "baseline"
            baseline.write_text(BASELINE, encoding="utf-8")
            out = root / "out"
            write_pair(out, BASELINE)
            (out / "publish.sh").write_text("echo pwned\n", encoding="utf-8")
            with self.assertRaises(SystemExit):
                gate.stage("maple-mono-nf-cn", baseline, out, root / "stage")
            linked = root / "linked"
            linked.mkdir()
            (linked / "PKGBUILD").symlink_to(baseline)
            (linked / ".SRCINFO").write_text(SRCINFO, encoding="utf-8")
            with self.assertRaises(SystemExit):
                gate.stage("maple-mono-nf-cn", baseline, linked, root / "stage2")


class ImportTest(unittest.TestCase):
    def stage_of(self, root: Path, pkgbuild: str, srcinfo: str = SRCINFO) -> Path:
        baseline = root / "baseline"
        baseline.write_text(BASELINE, encoding="utf-8")
        out = root / "out"
        write_pair(out, pkgbuild, srcinfo)
        stage = root / "stage"
        gate.stage("maple-mono-nf-cn", baseline, out, stage)
        return stage

    def test_import_replaces_only_the_two_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            updated = BASELINE.replace("'aaa'", "'ccc'")
            stage = self.stage_of(root, updated)
            dest = root / "maple-mono-nf-cn"
            dest.mkdir()
            (dest / "PKGBUILD").write_text(BASELINE, encoding="utf-8")
            (dest / ".SRCINFO").write_text("old\n", encoding="utf-8")
            (dest / "extra.txt").write_text("keep\n", encoding="utf-8")
            gate.import_artifact("maple-mono-nf-cn", stage, dest)
            self.assertEqual((dest / "PKGBUILD").read_text(encoding="utf-8"), updated)
            self.assertEqual((dest / ".SRCINFO").read_text(encoding="utf-8"), SRCINFO)
            self.assertEqual((dest / "extra.txt").read_text(encoding="utf-8"), "keep\n")
            self.assertFalse((dest / "MANIFEST").exists())

    def test_import_refuses_a_downgrade(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stage = self.stage_of(root, BASELINE)
            dest = root / "maple-mono-nf-cn"
            dest.mkdir()
            current = BASELINE.replace("pkgver=7.9", "pkgver=8.0")
            (dest / "PKGBUILD").write_text(current, encoding="utf-8")
            with self.assertRaises(SystemExit) as caught:
                gate.import_artifact("maple-mono-nf-cn", stage, dest)
            self.assertIn("older", str(caught.exception))
            self.assertEqual((dest / "PKGBUILD").read_text(encoding="utf-8"), current)

    def test_import_allows_the_same_git_commit_with_a_shorter_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current = (
                "pkgname=cuda-api-wrappers-git\n"
                "pkgver=0.8.2.r0.gc2f2a2b1\n"
                "pkgrel=1\n"
                "sha256sums=('SKIP')\n"
            )
            updated = current.replace("gc2f2a2b1", "gc2f2a2b")
            srcinfo = (
                "pkgbase = cuda-api-wrappers-git\n"
                "\tpkgver = 0.8.2.r0.gc2f2a2b\n"
                "\tpkgrel = 1\n"
                "\n"
                "pkgname = cuda-api-wrappers-git\n"
            )
            artifact = root / "artifact"
            write_pair(artifact, updated, srcinfo)
            files = {
                "PKGBUILD": (artifact / "PKGBUILD").read_bytes(),
                ".SRCINFO": (artifact / ".SRCINFO").read_bytes(),
            }
            gate.write_manifest(artifact, "publish", files)
            dest = root / "cuda-api-wrappers-git"
            dest.mkdir()
            (dest / "PKGBUILD").write_text(current, encoding="utf-8")
            gate.import_artifact("cuda-api-wrappers-git", artifact, dest)
            self.assertIn("gc2f2a2b\n", (dest / "PKGBUILD").read_text(encoding="utf-8"))

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
            stage = self.stage_of(root, BASELINE)
            nested = root / "download"
            nested.mkdir()
            moved = nested / "aur-stage"
            stage.rename(moved)
            dest = root / "maple-mono-nf-cn"
            dest.mkdir()
            (dest / "PKGBUILD").write_text(BASELINE, encoding="utf-8")
            gate.import_artifact("maple-mono-nf-cn", nested, dest)
            self.assertTrue((dest / ".SRCINFO").is_file())

    def test_manifest_hash_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stage = self.stage_of(root, BASELINE)
            manifest = stage / "MANIFEST"
            manifest.write_text(manifest.read_text(encoding="utf-8").replace("status=publish", "status=publish"), encoding="utf-8")
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
