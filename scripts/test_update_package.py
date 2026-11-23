#!/usr/bin/env python3
"""Offline tests for version bumps and the CI guards around them."""

from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path
from unittest import mock

import update_package as up

ROOT = Path(__file__).resolve().parent.parent


class ArchImageTest(unittest.TestCase):
    def test_check_arch_digest_refuses_a_malformed_pin(self) -> None:
        import shutil, tempfile
        scripts = Path(up.__file__).resolve().parent
        for bad in ("archlinux:base-devel", "archlinux:base-devel@sha256:" + "A" * 64, "evil/archlinux:base-devel@sha256:" + "0" * 64):
            with self.subTest(bad=bad), tempfile.TemporaryDirectory() as tmp:
                (Path(tmp) / "scripts").mkdir()
                shutil.copy(scripts / "check_arch_digest.sh", Path(tmp) / "scripts")
                (Path(tmp) / "scripts" / "archlinux-image").write_text(bad + "\n", encoding="utf-8")
                env = {"PATH": "/usr/bin:/bin", "HOME": tmp}
                proc = subprocess.run(["/bin/bash", str(Path(tmp) / "scripts" / "check_arch_digest.sh")],
                                      capture_output=True, text=True, env=env)
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn("must be archlinux:base-devel@sha256", proc.stderr)

    def test_reminder_lookup_trusts_only_labelled_bot_issues(self) -> None:
        import json, tempfile
        scripts = Path(up.__file__).resolve().parent
        title, label = "Arch build image digest is outdated", "arch-digest-reminder"
        bot = {"login": "app/github-actions", "is_bot": True}
        lab = [{"name": label}]
        issues = [
            {"number": 9, "title": title, "author": bot, "labels": lab},
            {"number": 3, "title": title, "author": bot, "labels": lab},
            {"number": 4, "title": title, "author": {"login": "mallory", "is_bot": False}, "labels": lab},
            {"number": 5, "title": title, "author": {"login": "app/evil-bot", "is_bot": True}, "labels": lab},
            {"number": 6, "title": title, "author": {"login": "app/github-actions", "is_bot": False}, "labels": lab},
            {"number": 7, "title": title + " ", "author": bot, "labels": lab},
            {"number": 8, "title": title, "author": bot, "labels": [{"name": "bug"}]},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "issues.json").write_text(json.dumps(issues), encoding="utf-8")
            gh = Path(tmp) / "gh"
            gh.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$(dirname "$0")/args"\ncat "$(dirname "$0")/issues.json"\n',
                          encoding="utf-8")
            gh.chmod(0o755)
            env = {"PATH": f"{tmp}:/usr/bin:/bin", "HOME": tmp, "REPO": "o/r", "TITLE": title, "LABEL": label}
            proc = subprocess.run(["/bin/bash", str(scripts / "arch_digest_reminders.sh")],
                                  capture_output=True, text=True, env=env)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout.split(), ["3", "9"])
            args = (Path(tmp) / "args").read_text(encoding="utf-8").splitlines()
            for flag, value in (("--author", "github-actions[bot]"), ("--label", label), ("--state", "open")):
                self.assertEqual(args[args.index(flag) + 1], value)

    def test_reminder_workflow_uses_the_filtered_lookup(self) -> None:
        wf = (ROOT / ".github/workflows/arch-digest.yml").read_text(encoding="utf-8")
        self.assertNotIn("--search", wf)
        self.assertEqual(wf.count("bash scripts/arch_digest_reminders.sh"), 2)
        self.assertIn('--label "$LABEL"', wf)
        self.assertIn("gh issue close", wf)
        for block in wf.split("run: |")[1:]:
            self.assertNotIn("${{", block.split("\n      - ")[0])

    def test_image_is_digest_pinned_and_shared(self) -> None:
        self.assertRegex(up.MOON_SANDBOX_IMAGE, r"^archlinux:base-devel@sha256:[0-9a-f]{64}$")
        scripts = Path(up.__file__).resolve().parent
        for name in ("test_build.sh", "regen_srcinfo.sh"):
            text = (scripts / name).read_text(encoding="utf-8")
            self.assertIn("scripts/archlinux-image", text)
            for line in text.splitlines():
                if line.lstrip().startswith("#"):
                    continue
                self.assertNotRegex(line, r"archlinux:base-devel\s*(\\|$)", f"{name}: {line}")


class UpdatePackageTest(unittest.TestCase):
    def test_self_check(self) -> None:
        proc = subprocess.run(
            ["python3", str(ROOT / "scripts/update_package.py"), "--self-check"],
            check=False,
            text=True,
            capture_output=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("self-check ok", proc.stdout)

    def test_bump_pkgver_resets_pkgrel(self) -> None:
        text = "pkgname=example\npkgver=1.0.0\npkgrel=4\n"
        bumped = up.bump_pkgver(text, "1.2.0")
        self.assertEqual(up.read_var(bumped, "pkgver"), "1.2.0")
        self.assertEqual(up.read_var(bumped, "pkgrel"), "1")
        self.assertEqual(up.read_var(text, "pkgrel"), "4")

    def _use_root(self, root: Path) -> None:
        self._root = up.ROOT
        up.ROOT = root
        self.addCleanup(setattr, up, "ROOT", self._root)

    def test_maple_bump_resets_pkgrel(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._use_root(root)
            pkg = root / "maple-mono-nf-cn"
            pkg.mkdir()
            (pkg / "PKGBUILD").write_text("pkgname=maple-mono-nf-cn\npkgver=1.0.0\npkgrel=4\n", encoding="utf-8")
            self._patch("choose_release_version", lambda name: "1.2.0")
            up.update_maple()
            text = (pkg / "PKGBUILD").read_text(encoding="utf-8")
            self.assertEqual(up.read_var(text, "pkgver"), "1.2.0")
            self.assertEqual(up.read_var(text, "pkgrel"), "1")
            self.assertEqual((pkg / ".update-old-version").read_text(encoding="utf-8").strip(), "1.0.0-4")

    def test_maple_same_version_keeps_pkgrel(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._use_root(root)
            pkg = root / "maple-mono-nf-cn"
            pkg.mkdir()
            (pkg / "PKGBUILD").write_text("pkgname=maple-mono-nf-cn\npkgver=1.0.0\npkgrel=4\n", encoding="utf-8")
            self._patch("choose_release_version", lambda name: "1.0.0")
            up.update_maple()
            text = (pkg / "PKGBUILD").read_text(encoding="utf-8")
            self.assertEqual(up.read_var(text, "pkgrel"), "4")
            self.assertFalse((pkg / ".update-old-version").exists())

    def test_maple_refuses_downgrade(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._use_root(root)
            pkg = root / "maple-mono-nf-cn"
            pkg.mkdir()
            original = "pkgname=maple-mono-nf-cn\npkgver=2.0.0\npkgrel=1\n"
            (pkg / "PKGBUILD").write_text(original, encoding="utf-8")
            self._patch("choose_release_version", lambda name: "1.0.0")
            with self.assertRaises(SystemExit):
                up.update_maple()
            self.assertEqual((pkg / "PKGBUILD").read_text(encoding="utf-8"), original)

    def test_git_bump_resets_pkgrel(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._use_root(root)
            pkg = root / "cuda-api-wrappers-git"
            pkg.mkdir()
            (pkg / "PKGBUILD").write_text(
                "pkgname=cuda-api-wrappers-git\npkgver=0.1.0.r0.gabcdef1\npkgrel=3\n",
                encoding="utf-8",
            )
            self._patch("nvchecker_version", lambda name: None)
            self._patch("git_pkgver", lambda url, args: ("0.2.0.r0.g1234567", "1234567890abcdef"))
            up.update_git("cuda-api-wrappers-git", "https://example.invalid/repo.git")
            text = (pkg / "PKGBUILD").read_text(encoding="utf-8")
            self.assertEqual(up.read_var(text, "pkgver"), "0.2.0.r0.g1234567")
            self.assertEqual(up.read_var(text, "pkgrel"), "1")

    def test_git_same_commit_does_not_reset_pkgrel(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._use_root(root)
            pkg = root / "cuda-api-wrappers-git"
            pkg.mkdir()
            original = "pkgname=cuda-api-wrappers-git\npkgver=1.0.0.r5.gabcdef1\npkgrel=3\n"
            (pkg / "PKGBUILD").write_text(original, encoding="utf-8")
            self._patch("nvchecker_version", lambda name: None)
            self._patch("git_pkgver", lambda url, args: ("1.0.0.r0.gabcdef12", "abcdef1234567890"))
            up.update_git("cuda-api-wrappers-git", "https://example.invalid/repo.git")
            self.assertEqual((pkg / "PKGBUILD").read_text(encoding="utf-8"), original)

    def test_memprocfs_pkgrel_is_the_asset_date(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._use_root(root)
            pkg = root / "memprocfs"
            pkg.mkdir()
            (pkg / "PKGBUILD").write_text(
                "\n".join(
                    [
                        "pkgname=memprocfs",
                        "pkgver=5.19.0",
                        "pkgrel=20261014",
                        'source_x86_64=("https://example.invalid/old-x64.tar.gz")',
                        'source_aarch64=("https://example.invalid/old-arm.tar.gz")',
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            self._patch(
                "probe_memprocfs",
                lambda: {
                    "pkgver": "5.20.0",
                    "pkgrel": "20261101",
                    "x86_64": "https://example.invalid/x64.tar.gz",
                    "aarch64": "https://example.invalid/arm.tar.gz",
                },
            )
            up.update_memprocfs()
            text = (pkg / "PKGBUILD").read_text(encoding="utf-8")
            self.assertEqual(up.read_var(text, "pkgver"), "5.20.0")
            self.assertEqual(up.read_var(text, "pkgrel"), "20261101")

    def test_disabled_package_is_not_updated(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._use_root(root)
            pkg = root / "maple-mono-nf-cn"
            pkg.mkdir()
            (pkg / "PKGBUILD").write_text("pkgver=1\npkgrel=1\n", encoding="utf-8")
            (pkg / ".aur-disabled").write_text("\n", encoding="utf-8")
            self.assertEqual(up.main(["update_package.py", "maple-mono-nf-cn"]), 0)
            self.assertEqual((pkg / "PKGBUILD").read_text(encoding="utf-8"), "pkgver=1\npkgrel=1\n")

    def test_same_pkgver_commit(self) -> None:
        self.assertTrue(up.same_pkgver_commit("0.8.2.r0.gc2f2a2b", "0.8.2.r0.gc2f2a2b1"))
        self.assertTrue(up.same_commit("0.8.2.r0.gc2f2a2b", "c2f2a2b999"))
        self.assertFalse(up.same_pkgver_commit("0.8.2.r0.gabcdef1", "0.8.2.r1.gabcdef12"))
        self.assertFalse(up.same_commit("0.8.2.r0.gc2f2a2b", "deadbeef"))

    def _patch(self, name: str, value: object) -> None:
        original = getattr(up, name)
        setattr(up, name, value)
        self.addCleanup(setattr, up, name, original)


MFILE_SHA256SUMS = """\
0022781691d94c0ceb1ea09df92ca5bce45c5404c2d487e3710276f0ece3d65e  mfile_0.1.0_linux_amd64.tar.gz
280bcb4d3d8f7fcdef2c4e97535ef7ebfe970095d5243f922e0758b13868988b  mfile_0.1.0_linux_arm64.tar.gz
01979c82a0959330b12acd4c84851097c141017110ff0f2e267968d6df97fc07  mfile_0.1.0_darwin_amd64.tar.gz
d6956a080e21193ceda2a991ce756f2a660bf3967f7ee1d638ca637f78463524  mfile_0.1.0_darwin_arm64.tar.gz
0f10bc1399621a4ac996a4b1bc597b131b5fd44e9fe261cdaaff019839031474  mfile_0.1.0_windows_amd64.zip
"""

MFILE_NEXT_SHA256SUMS = """\
aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa  mfile_0.2.0_linux_amd64.tar.gz
bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb  mfile_0.2.0_linux_arm64.tar.gz
"""


class MfileUpdateTest(unittest.TestCase):
    def test_sha256sums_parser_uses_linux_assets_only(self) -> None:
        got = up.parse_mfile_sha256sums(MFILE_SHA256SUMS, "0.1.0")
        self.assertEqual(
            got,
            {
                "x86_64": "0022781691d94c0ceb1ea09df92ca5bce45c5404c2d487e3710276f0ece3d65e",
                "aarch64": "280bcb4d3d8f7fcdef2c4e97535ef7ebfe970095d5243f922e0758b13868988b",
            },
        )
        pinned = up.mfile_pinned_sha256sums("0.1.0", MFILE_SHA256SUMS)
        self.assertEqual(pinned["sha256sums_x86_64"], [got["x86_64"]])
        self.assertEqual(pinned["sha256sums_aarch64"], [got["aarch64"]])

    def test_sha256sums_parser_rejects_a_missing_arch(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            up.parse_mfile_sha256sums(MFILE_SHA256SUMS, "9.9.9")
        self.assertIn("missing", str(caught.exception))

    def test_single_source_array_cannot_be_updated(self) -> None:
        text = "pkgname=mfile-bin\npkgver=0.1.0\npkgrel=1\nsource=(\"https://example.invalid/mfile.tar.gz\")\nsha256sums=('aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa')\n"
        with self.assertRaises(SystemExit) as caught:
            up.apply_mfile_sources_and_sums(text, "0.1.0", MFILE_SHA256SUMS)
        self.assertIn("could not set source_x86_64", str(caught.exception))

    def test_bump_rewrites_both_arch_sources_and_checksums(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            up.ROOT = root
            self.addCleanup(setattr, up, "ROOT", ROOT)
            pkg = root / "mfile-bin"
            pkg.mkdir()
            original = "\n".join(
                [
                    "pkgname=mfile-bin",
                    "pkgver=0.1.0",
                    "pkgrel=4",
                    'source_x86_64=("https://example.invalid/old-amd64.tar.gz")',
                    'source_aarch64=("https://example.invalid/old-arm64.tar.gz")',
                    "sha256sums_x86_64=('cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc')",
                    "sha256sums_aarch64=('dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd')",
                    "",
                ]
            )
            (pkg / "PKGBUILD").write_text(original, encoding="utf-8")
            self._patch("choose_release_version", lambda name: "0.2.0")
            self._patch("fetch_mfile_sha256sums", lambda pkgver: MFILE_NEXT_SHA256SUMS)
            up.update_mfile()
            text = (pkg / "PKGBUILD").read_text(encoding="utf-8")
            self.assertEqual(up.read_var(text, "pkgver"), "0.2.0")
            self.assertEqual(up.read_var(text, "pkgrel"), "1")
            self.assertIn(f'source_x86_64=("{up.mfile_source_item("x86_64")}")', text)
            self.assertIn(f'source_aarch64=("{up.mfile_source_item("aarch64")}")', text)
            self.assertNotIn("example.invalid", text)
            self.assertIn("sha256sums_x86_64=('aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa')", text)
            self.assertIn("sha256sums_aarch64=('bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb')", text)
            self.assertEqual((pkg / ".update-old-version").read_text(encoding="utf-8").strip(), "0.1.0-4")

    def test_current_release_does_not_rewrite_the_pkgbuild(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            up.ROOT = root
            self.addCleanup(setattr, up, "ROOT", ROOT)
            pkg = root / "mfile-bin"
            pkg.mkdir()
            text = (ROOT / "mfile-bin" / "PKGBUILD").read_text(encoding="utf-8")
            (pkg / "PKGBUILD").write_text(text, encoding="utf-8")
            self._patch("choose_release_version", lambda name: "0.1.0")
            self._patch("fetch_mfile_sha256sums", lambda pkgver: MFILE_SHA256SUMS)
            up.update_mfile()
            self.assertEqual((pkg / "PKGBUILD").read_text(encoding="utf-8"), text)
            self.assertFalse((pkg / ".update-old-version").exists())

    def _patch(self, name: str, value: object) -> None:
        original = getattr(up, name)
        setattr(up, name, value)
        self.addCleanup(setattr, up, name, original)


class CiShouldBuildTest(unittest.TestCase):
    def decide(self, changed: str, event: str, force: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(ROOT / "scripts/ci_should_build.sh"), changed, event, force],
            check=False,
            text=True,
            capture_output=True,
        )

    def test_changed_package_builds_on_schedule_and_push(self) -> None:
        for event in ("schedule", "push"):
            proc = self.decide("true", event, "false")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout.strip(), "true")

    def test_unchanged_package_skips_unless_manual_force(self) -> None:
        for event in ("schedule", "push", "workflow_dispatch"):
            proc = self.decide("false", event, "false")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout.strip(), "false")
        forced = self.decide("false", "workflow_dispatch", "true")
        self.assertEqual(forced.returncode, 0, forced.stderr)
        self.assertEqual(forced.stdout.strip(), "true")

    def test_force_is_rejected_off_workflow_dispatch(self) -> None:
        for event in ("schedule", "push"):
            proc = self.decide("false", event, "true")
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("workflow_dispatch", proc.stderr)

    def test_empty_force_is_false(self) -> None:
        proc = self.decide("false", "workflow_dispatch", "")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "false")


class PublishGuardTest(unittest.TestCase):
    def test_publish_refuses_packages_outside_the_allowlist(self) -> None:
        for name in ("not-a-package", "asus-fa401km-acpi-fix", "hearthstone-linux-gui"):
            env = os.environ.copy()
            env.pop("AUR_SSH_PRIVATE_KEY", None)
            proc = subprocess.run(
                ["bash", str(ROOT / "scripts/publish.sh"), name],
                check=False,
                text=True,
                capture_output=True,
                env=env,
            )
            self.assertNotEqual(proc.returncode, 0, name)
            self.assertIn("allowlist", proc.stderr)
            self.assertNotIn("PRIVATE KEY", proc.stderr + proc.stdout)

    def test_hearthstone_is_allowlisted_and_stops_before_using_a_key(self) -> None:
        env = os.environ.copy()
        env.pop("AUR_SSH_PRIVATE_KEY", None)
        for name in ("hearthstone-linux-gui-bin", "hearthstone-linux-gui-appimage"):
            self.assertFalse((ROOT / name / ".aur-disabled").is_file(), name)
            proc = subprocess.run(
                ["bash", str(ROOT / "scripts/publish.sh"), name],
                check=False,
                text=True,
                capture_output=True,
                env=env,
            )
            self.assertNotEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("AUR_SSH_PRIVATE_KEY is empty", proc.stderr)
            self.assertNotIn("allowlist", proc.stderr)
            self.assertNotIn("disabled", proc.stderr)
            self.assertNotIn("PRIVATE KEY", proc.stdout)
            self.assertNotIn("BEGIN ", proc.stderr + proc.stdout)

    def test_disabled_marker_still_blocks_build_and_publish(self) -> None:
        for rel in ("scripts/publish.sh", "scripts/test_build.sh", "scripts/build_in_arch.sh"):
            self.assertIn(".aur-disabled", (ROOT / rel).read_text(encoding="utf-8"))

    def test_test_build_seals_before_a_second_container(self) -> None:
        text = (ROOT / "scripts/test_build.sh").read_text(encoding="utf-8")
        seal_part, build_part = text.split("# Second container:", 1)
        self.assertEqual(seal_part.count("docker run"), 1)
        self.assertEqual(build_part.count("docker run"), 1)
        self.assertIn('"$pkgdir:/pkg:ro"', seal_part)
        self.assertIn(" seal ", seal_part)
        self.assertIn('"$work_build/pkg:/pkg"', build_part)
        self.assertNotIn('"$pkgdir:', build_part)
        self.assertNotIn('"$seal:', build_part)
        self.assertNotIn('"$repo:/', text)
        self.assertNotIn("AUR_SSH_PRIVATE_KEY", text)
        helper = (ROOT / "scripts/build_in_arch.sh").read_text(encoding="utf-8")
        seal_helper, build_helper = helper.split('echo "builder ALL=(ALL) NOPASSWD: ALL"', 1)
        self.assertNotIn("NOPASSWD", seal_helper)
        self.assertIn("makepkg -s", build_helper)

    def test_workflow_keeps_the_job_boundary(self) -> None:
        text = (ROOT / ".github/workflows/update-aur.yml").read_text(encoding="utf-8")
        jobs = text.split("\njobs:\n", 1)[1]
        build, publish = jobs.split("\n  publish:\n", 1)
        self.assertIn("persist-credentials: false", build)
        self.assertNotIn("persist-credentials: true", build)
        self.assertNotIn("AUR_SSH_PRIVATE_KEY", build)
        self.assertIn("contents: read", build)
        self.assertNotIn("actions: write", build)
        self.assertIn("regen_srcinfo.sh", publish)
        self.assertIn("persist-credentials: true", publish)
        self.assertIn("environment: aur", publish)
        self.assertIn("AUR_SSH_PRIVATE_KEY", publish)
        self.assertIn("contents: write", publish)
        self.assertIn("actions: read", publish)
        self.assertNotIn("actions/checkout@v", text)
        self.assertEqual(text.count("actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"), 2)
        self.assertIn("actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9", text)
        self.assertIn("actions/download-artifact@9000827ccba6bdab643e8b6fd33ac0654aef8333", text)
        self.assertEqual(text.count("fail-fast: false"), 2)
        self.assertEqual(text.count("cancel-in-progress: false"), 2)
        self.assertEqual(text.count("group: aur-update-${{ matrix.package }}"), 2)
        self.assertIn("force_publish", text)
        for package in (
            "maple-mono-nf-cn",
            "memprocfs",
            "moonbit-bin",
            "mfile-bin",
            "hearthstone-linux-gui-bin",
            "hearthstone-linux-gui-appimage",
            "cuda-api-wrappers-git",
            "reflectcpp-git",
        ):
            self.assertEqual(text.count(f"package: {package}"), 2)
        self.assertIn('"mfile-bin/**"', text)
        self.assertIn('"hearthstone-linux-gui-bin/**"', text)
        self.assertIn('"hearthstone-linux-gui-appimage/**"', text)
        ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        self.assertIn("python3 scripts/update_package.py mfile-bin", ci)
        self.assertIn("hearthstone-linux-gui-bin hearthstone-linux-gui-appimage", ci)
        self.assertNotIn("update_package.py maple", ci)
        self.assertNotIn("update_package.py moonbit", ci)
        self.assertNotIn("update_package.py memprocfs", ci)
        dependabot = (ROOT / ".github/dependabot.yml").read_text(encoding="utf-8")
        self.assertIn("package-ecosystem: github-actions", dependabot)
        self.assertIn("interval: weekly", dependabot)

    def test_moonbit_conflicts_moon_bin_and_srcinfo_matches_pkgrel(self) -> None:
        pkgbuild = (ROOT / "moonbit-bin/PKGBUILD").read_text(encoding="utf-8")
        srcinfo = (ROOT / "moonbit-bin/.SRCINFO").read_text(encoding="utf-8")
        self.assertIn("conflicts=(\"moonbit\" \"moon-bin\")", pkgbuild)
        self.assertIn("\tconflicts = moon-bin\n", srcinfo)
        rel = up.read_var(pkgbuild, "pkgrel")
        self.assertIn(f"\tpkgrel = {rel}\n", srcinfo)


V041_ASSETS = [
    "hearthstone-linux-gui-0.4.1-1-x86_64.pkg.tar.zst",
    "hearthstone-linux-gui-0.4.1-1.x86_64.rpm",
    "hearthstone-linux-gui-0.4.1-x86_64.AppImage",
    "hearthstone-linux-gui-0.4.1-x86_64.flatpak",
    "hearthstone-linux-gui_0.4.1_amd64.deb",
    "SHA256SUMS.txt",
]
V041_SUMS = """\
be31ec0b988a062481d47d33064005852ef6322516d3dcf523417539f8003543  hearthstone-linux-gui-0.4.1-1-x86_64.pkg.tar.zst
e00ef9061c1310515fc45d9d23cf63de70858800cde058e7119a08ea4a0ed904  hearthstone-linux-gui-0.4.1-1.x86_64.rpm
0db2b769530095ba7811888e03b10eb7581553ad31cc97ec3d495279e4cce43d  hearthstone-linux-gui-0.4.1-x86_64.AppImage
21108142cf6eea1cc61b5fc4487fdf27a9b8f2bac5ee8e99470f40e1dd354f96  hearthstone-linux-gui-0.4.1-x86_64.flatpak
6fa062256cbacab5fa7735a0ed94c53b3df550c82e617bc4d17b4f9e8d9f53a8  hearthstone-linux-gui_0.4.1_amd64.deb
4b19fc174b0e5778ab1dd5d697094fdec4c79af97010e44e362db9bc46a91392  provenance.json
"""
BIN_URL = (
    "https://github.com/DawnMagnet/hearthstone-linux-gui/releases/download/v0.4.1/"
    "hearthstone-linux-gui-0.4.1-1-x86_64.pkg.tar.zst"
)
APP_URL = (
    "https://github.com/DawnMagnet/hearthstone-linux-gui/releases/download/v0.4.1/"
    "hearthstone-linux-gui-0.4.1-x86_64.AppImage"
)


def _pkgbuild(name: str, ver: str = "0.3.2", rel: str = "4", source: str | None = None, digest: str | None = None) -> str:
    source = source or "https://example.invalid/old"
    digest = digest or "c" * 64
    return (
        f"pkgname={name}\n"
        f"pkgver={ver}\n"
        f"pkgrel={rel}\n"
        f'source=("{source}")\n'
        f"sha256sums=('{digest}')\n"
    )


class HearthstoneUpdateTest(unittest.TestCase):
    def test_v041_assets_ignore_rpm_and_use_real_checksums(self) -> None:
        info = up.resolve_hearthstone_assets("0.4.1", V041_ASSETS, V041_SUMS)
        self.assertEqual(info["bin_rel"], "1")
        self.assertEqual(info["bin_url"], BIN_URL)
        self.assertEqual(info["appimage_url"], APP_URL)
        self.assertEqual(info["bin_sha256"], "be31ec0b988a062481d47d33064005852ef6322516d3dcf523417539f8003543")
        self.assertEqual(info["appimage_sha256"], "0db2b769530095ba7811888e03b10eb7581553ad31cc97ec3d495279e4cce43d")
        self.assertNotIn("SKIP", info["bin_sha256"] + info["appimage_sha256"])

    def test_committed_pkgbuilds_match_the_v041_release(self) -> None:
        info = up.resolve_hearthstone_assets("0.4.1", V041_ASSETS, V041_SUMS)
        pairs = (
            ("hearthstone-linux-gui-bin", info["bin_url"], info["bin_sha256"], "hearthstone-linux-gui-appimage"),
            ("hearthstone-linux-gui-appimage", info["appimage_url"], info["appimage_sha256"], "hearthstone-linux-gui-bin"),
        )
        for name, url, digest, other in pairs:
            text = (ROOT / name / "PKGBUILD").read_text(encoding="utf-8")
            srcinfo = (ROOT / name / ".SRCINFO").read_text(encoding="utf-8")
            self.assertEqual(up.read_var(text, "pkgver"), "0.4.1")
            self.assertEqual(up.read_var(text, "pkgrel"), "1")
            self.assertIn(f'source=("{url}")', text)
            self.assertIn(f"sha256sums=('{digest}')", text)
            self.assertNotIn("SKIP", text)
            self.assertIn("provides=('hearthstone-linux-gui')", text)
            self.assertIn(f"conflicts=('hearthstone-linux-gui' '{other}')", text)
            self.assertNotIn(f"conflicts=('{name}'", text)
            self.assertIn(f"\tpkgver = 0.4.1\n", srcinfo)
            self.assertIn(f"\tsha256sums = {digest}\n", srcinfo)
            self.assertIn("\tprovides = hearthstone-linux-gui\n", srcinfo)
            self.assertIn(f"\tconflicts = {other}\n", srcinfo)
            self.assertFalse((ROOT / name / ".aur-disabled").exists())
        bin_text = (ROOT / "hearthstone-linux-gui-bin/PKGBUILD").read_text(encoding="utf-8")
        app_text = (ROOT / "hearthstone-linux-gui-appimage/PKGBUILD").read_text(encoding="utf-8")
        bin_depends = up.read_var(bin_text, "depends")
        app_depends = up.read_var(app_text, "depends")
        for dep in ("glibc", "gtk4", "libadwaita", "glib2", "pango", "xdg-utils", "gdk-pixbuf2", "xz", "hicolor-icon-theme"):
            self.assertIn(dep, bin_depends)
        self.assertNotIn("gtk4", app_depends)
        for dep in ("glibc", "fuse2", "hicolor-icon-theme", "xdg-utils"):
            self.assertIn(dep, app_depends)
        self.assertIn("/usr/bin/hearthstone-linux-gui", app_text)
        self.assertIn("io.github.hearthstone_linux_gui.desktop", app_text)
        self.assertIn('bsdtar -xf "${archives[0]}" -C "${pkgdir}"', bin_text)
        self.assertIn("/usr/share/licenses/${pkgname}/LICENSE", bin_text)
        self.assertIn("/usr/share/licenses/${pkgname}/LICENSE", app_text)
        allow = (ROOT / "scripts/aur_rpc.py").read_text(encoding="utf-8")
        self.assertIn('FIRST_PUBLISH_ALLOWLIST = ("mfile-bin",)', allow)
        self.assertNotIn("hearthstone", allow.split("FIRST_PUBLISH_ALLOWLIST", 1)[1].split("\n", 1)[0])

    def test_nvchecker_entries_match_the_asset_repo(self) -> None:
        for name in up.HEARTHSTONE_PACKAGES:
            entry = up.load_nv_entry(name)
            self.assertEqual(entry["source"], "github")
            self.assertEqual(entry["github"], up.HEARTHSTONE_REPO)
            self.assertTrue(entry["use_latest_release"])
            self.assertEqual(entry["prefix"], "v")
            self.assertIn(name, up.RELEASE_PACKAGES)
            self.assertIn(name, up.UPDATERS)

    def test_upstream_pkgrel_is_not_assumed_to_be_1(self) -> None:
        names = [
            "hearthstone-linux-gui-1.2.3-2-x86_64.pkg.tar.zst",
            "hearthstone-linux-gui-1.2.3-10-x86_64.pkg.tar.zst",
            "hearthstone-linux-gui-1.2.3-1.x86_64.rpm",
            "hearthstone-linux-gui-1.2.3-1-x86_64-native.pkg.tar.zst",
            "hearthstone-linux-gui-1.2.3-4-x86_64.AppImage",
            "SHA256SUMS.txt",
        ]
        sums = (
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa  hearthstone-linux-gui-1.2.3-2-x86_64.pkg.tar.zst\n"
            "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb  hearthstone-linux-gui-1.2.3-10-x86_64.pkg.tar.zst\n"
            "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc  hearthstone-linux-gui-1.2.3-4-x86_64.AppImage\n"
        )
        info = up.resolve_hearthstone_assets("1.2.3", names, sums)
        self.assertEqual(info["bin_rel"], "10")
        self.assertTrue(info["bin_url"].endswith("hearthstone-linux-gui-1.2.3-10-x86_64.pkg.tar.zst"))
        self.assertTrue(info["appimage_url"].endswith("hearthstone-linux-gui-1.2.3-4-x86_64.AppImage"))
        self.assertNotIn("-1-x86_64.pkg.tar.zst", info["bin_url"])

    def test_plain_and_qualified_appimage_together_is_refused(self) -> None:
        names = [
            "hearthstone-linux-gui-1.0.0-1-x86_64.pkg.tar.zst",
            "hearthstone-linux-gui-1.0.0-x86_64.AppImage",
            "hearthstone-linux-gui-1.0.0-2-x86_64.AppImage",
            "SHA256SUMS.txt",
        ]
        with self.assertRaises(SystemExit) as caught:
            up.resolve_hearthstone_assets("1.0.0", names, "aa" * 32 + "  hearthstone-linux-gui-1.0.0-1-x86_64.pkg.tar.zst\n")
        self.assertIn("refusing to guess", str(caught.exception))

    def test_missing_asset_checksum_or_sums_file_fails(self) -> None:
        names = [
            "hearthstone-linux-gui-1.0.0-3-x86_64.pkg.tar.zst",
            "hearthstone-linux-gui-1.0.0-x86_64.AppImage",
            "SHA256SUMS.txt",
        ]
        with self.assertRaises(SystemExit) as caught:
            up.resolve_hearthstone_assets("1.0.0", names, "not a checksum\n")
        self.assertIn("malformed", str(caught.exception))
        with self.assertRaises(SystemExit) as caught:
            up.resolve_hearthstone_assets(
                "1.0.0",
                names,
                "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd  hearthstone-linux-gui-1.0.0-3-x86_64.pkg.tar.zst\n",
            )
        self.assertIn("missing", str(caught.exception))
        with self.assertRaises(SystemExit) as caught:
            up.resolve_hearthstone_assets("1.0.0", names[:-1], "x\n")
        self.assertIn("no SHA256SUMS.txt", str(caught.exception))
        native_only = ["hearthstone-linux-gui-1.0.0-1-x86_64-native.pkg.tar.zst", "SHA256SUMS.txt"]
        with self.assertRaises(SystemExit) as caught:
            up.resolve_hearthstone_assets("1.0.0", native_only, "x\n")
        self.assertIn("no Arch package asset", str(caught.exception))
        with self.assertRaises(SystemExit) as caught:
            up.parse_hearthstone_sha256sums("SKIP  hearthstone-linux-gui-1.0.0-x86_64.AppImage\n")
        self.assertIn("malformed", str(caught.exception))

    def test_release_fetch_uses_the_canonical_sums_url(self) -> None:
        rel = {
            "tag_name": "v0.4.1",
            "draft": False,
            "prerelease": False,
            "assets": [{"name": name} for name in V041_ASSETS],
        }
        seen: dict[str, str] = {}

        def api(url: str):
            seen["api"] = url
            return rel

        def download(url: str, *, limit: int = 0) -> str:
            seen["sums"] = url
            seen["limit"] = str(limit)
            return V041_SUMS

        with mock.patch.object(up, "github_api", api), mock.patch.object(up, "download_release_text", download):
            info = up.hearthstone_release("0.4.1")
        self.assertEqual(seen["api"], "https://api.github.com/repos/DawnMagnet/hearthstone-linux-gui/releases/tags/v0.4.1")
        self.assertEqual(
            seen["sums"],
            "https://github.com/DawnMagnet/hearthstone-linux-gui/releases/download/v0.4.1/SHA256SUMS.txt",
        )
        self.assertEqual(info["bin_url"], BIN_URL)
        prerelease = dict(rel, prerelease=True)
        with mock.patch.object(up, "github_api", lambda url: prerelease):
            with self.assertRaises(SystemExit) as caught:
                up.github_release_by_tag(up.HEARTHSTONE_REPO, "0.4.1")
        self.assertIn("prerelease", str(caught.exception))

    def test_bump_resets_pkgrel_and_records_the_real_asset_rel(self) -> None:
        import tempfile

        info = up.resolve_hearthstone_assets(
            "0.4.1",
            [
                "hearthstone-linux-gui-0.4.1-7-x86_64.pkg.tar.zst",
                "hearthstone-linux-gui-0.4.1-x86_64.AppImage",
                "SHA256SUMS.txt",
            ],
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa  hearthstone-linux-gui-0.4.1-7-x86_64.pkg.tar.zst\n"
            "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb  hearthstone-linux-gui-0.4.1-x86_64.AppImage\n",
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            up.ROOT = root
            self.addCleanup(setattr, up, "ROOT", ROOT)
            for name, url_key, sum_key in (
                ("hearthstone-linux-gui-bin", "bin_url", "bin_sha256"),
                ("hearthstone-linux-gui-appimage", "appimage_url", "appimage_sha256"),
            ):
                pkg = root / name
                pkg.mkdir()
                (pkg / "PKGBUILD").write_text(_pkgbuild(name), encoding="utf-8")
            self._patch("choose_release_version", lambda package: "0.4.1")
            self._patch("hearthstone_release", lambda pkgver: info)
            up.update_hearthstone("hearthstone-linux-gui-bin")
            text = (root / "hearthstone-linux-gui-bin/PKGBUILD").read_text(encoding="utf-8")
            self.assertEqual(up.read_var(text, "pkgver"), "0.4.1")
            self.assertEqual(up.read_var(text, "pkgrel"), "1")
            self.assertIn(f'source=("{info["bin_url"]}")', text)
            self.assertIn("-7-x86_64.pkg.tar.zst", text)
            self.assertNotIn("-1-x86_64.pkg.tar.zst", text)
            self.assertIn(f"sha256sums=('{info['bin_sha256']}')", text)
            self.assertNotIn("SKIP", text)
            self.assertEqual((root / "hearthstone-linux-gui-bin/.update-old-version").read_text(encoding="utf-8").strip(), "0.3.2-4")

    def test_same_version_drift_does_not_rewrite(self) -> None:
        import tempfile

        info = {
            "pkgver": "0.4.1",
            "bin_url": BIN_URL.replace("-1-x86_64", "-2-x86_64"),
            "bin_sha256": "a" * 64,
            "appimage_url": APP_URL,
            "appimage_sha256": "b" * 64,
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            up.ROOT = root
            self.addCleanup(setattr, up, "ROOT", ROOT)
            pkg = root / "hearthstone-linux-gui-bin"
            pkg.mkdir()
            original = (ROOT / "hearthstone-linux-gui-bin/PKGBUILD").read_text(encoding="utf-8")
            (pkg / "PKGBUILD").write_text(original, encoding="utf-8")
            self._patch("choose_release_version", lambda package: "0.4.1")
            self._patch("hearthstone_release", lambda pkgver: info)
            with self.assertRaises(SystemExit) as caught:
                up.update_hearthstone("hearthstone-linux-gui-bin")
            self.assertIn("does not match", str(caught.exception))
            self.assertEqual((pkg / "PKGBUILD").read_text(encoding="utf-8"), original)
            self.assertFalse((pkg / ".update-old-version").exists())

    def test_current_release_does_not_rewrite_and_downgrade_is_refused(self) -> None:
        import tempfile

        info = up.resolve_hearthstone_assets("0.4.1", V041_ASSETS, V041_SUMS)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            up.ROOT = root
            self.addCleanup(setattr, up, "ROOT", ROOT)
            pkg = root / "hearthstone-linux-gui-appimage"
            pkg.mkdir()
            original = (ROOT / "hearthstone-linux-gui-appimage/PKGBUILD").read_text(encoding="utf-8")
            (pkg / "PKGBUILD").write_text(original, encoding="utf-8")
            self._patch("choose_release_version", lambda package: "0.4.1")
            self._patch("hearthstone_release", lambda pkgver: info)
            up.update_hearthstone("hearthstone-linux-gui-appimage")
            self.assertEqual((pkg / "PKGBUILD").read_text(encoding="utf-8"), original)
            (pkg / "PKGBUILD").write_text(
                _pkgbuild("hearthstone-linux-gui-appimage", ver="9.0.0", rel="1", source=APP_URL, digest=info["appimage_sha256"]),
                encoding="utf-8",
            )
            with self.assertRaises(SystemExit) as caught:
                up.update_hearthstone("hearthstone-linux-gui-appimage")
            self.assertIn("downgrade", str(caught.exception))

    def _patch(self, name: str, value: object) -> None:
        original = getattr(up, name)
        setattr(up, name, value)
        self.addCleanup(setattr, up, name, original)


if __name__ == "__main__":
    unittest.main()
