#!/usr/bin/env python3
"""Offline tests for version bumps and the CI guards around them."""

from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path

import update_package as up

ROOT = Path(__file__).resolve().parent.parent


class ArchImageTest(unittest.TestCase):
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
        for name in (
            "hearthstone-linux-gui-bin",
            "hearthstone-linux-gui-appimage",
            "not-a-package",
        ):
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

    def test_disabled_package_is_refused_before_docker(self) -> None:
        proc = subprocess.run(
            ["bash", str(ROOT / "scripts/test_build.sh"), "hearthstone-linux-gui-bin"],
            check=False,
            text=True,
            capture_output=True,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("disabled", proc.stderr)
        proc = subprocess.run(
            ["bash", str(ROOT / "scripts/test_build.sh"), "hearthstone-linux-gui-appimage"],
            check=False,
            text=True,
            capture_output=True,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("disabled", proc.stderr)

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
        self.assertNotIn("package: hearthstone", text)
        for package in (
            "maple-mono-nf-cn",
            "memprocfs",
            "moonbit-bin",
            "cuda-api-wrappers-git",
            "reflectcpp-git",
        ):
            self.assertEqual(text.count(f"package: {package}"), 2)
        dependabot = (ROOT / ".github/dependabot.yml").read_text(encoding="utf-8")
        self.assertIn("package-ecosystem: github-actions", dependabot)
        self.assertIn("interval: weekly", dependabot)

    def test_moonbit_pkgrel_is_2_and_conflicts_moon_bin(self) -> None:
        pkgbuild = (ROOT / "moonbit-bin/PKGBUILD").read_text(encoding="utf-8")
        srcinfo = (ROOT / "moonbit-bin/.SRCINFO").read_text(encoding="utf-8")
        self.assertIn("pkgrel=2", pkgbuild)
        self.assertIn("conflicts=(\"moonbit\" \"moon-bin\")", pkgbuild)
        self.assertIn("\tpkgrel = 2\n", srcinfo)
        self.assertIn("\tconflicts = moon-bin\n", srcinfo)
        self.assertNotIn("pkgrel=1", pkgbuild)
        self.assertNotIn("pkgrel = 1", srcinfo)


if __name__ == "__main__":
    unittest.main()
