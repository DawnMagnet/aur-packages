#!/usr/bin/env python3
"""Scope rules for update-aur.yml package selection."""

from __future__ import annotations

import json
import os
import subprocess
import unittest
from pathlib import Path
from unittest import mock

import select_packages as sel

ROOT = Path(__file__).resolve().parent.parent
ALL = tuple(sel.PACKAGES)
HEARTHSTONE = ("hearthstone-linux-gui-bin", "hearthstone-linux-gui-appimage")


class SelectTest(unittest.TestCase):
    def names(self, event: str, packages: str | None = None, paths: list[str] | None = None) -> list[str]:
        return sel.select(event, packages, paths)

    def test_schedule_is_every_allowlisted_package(self) -> None:
        self.assertEqual(self.names("schedule"), list(ALL))

    def test_manual_all_is_every_allowlisted_package(self) -> None:
        self.assertEqual(self.names("workflow_dispatch", "all"), list(ALL))
        self.assertEqual(self.names("workflow_dispatch", "  all  "), list(ALL))

    def test_manual_subset_keeps_allowlist_order_and_trusted_metadata(self) -> None:
        chosen = self.names(
            "workflow_dispatch",
            "hearthstone-linux-gui-appimage hearthstone-linux-gui-bin",
        )
        self.assertEqual(chosen, list(HEARTHSTONE))
        rows = sel.rows_for(chosen)
        self.assertEqual(
            rows,
            [
                {"package": "hearthstone-linux-gui-bin", "timeout": 60, "free_disk": False},
                {"package": "hearthstone-linux-gui-appimage", "timeout": 45, "free_disk": False},
            ],
        )
        # The dispatch string cannot smuggle timeout or free_disk.
        self.assertEqual(sel.rows_for(self.names("workflow_dispatch", "cuda-api-wrappers-git"))[0]["free_disk"], True)
        self.assertEqual(sel.rows_for(self.names("workflow_dispatch", "cuda-api-wrappers-git"))[0]["timeout"], 180)

    def test_manual_accepts_commas(self) -> None:
        self.assertEqual(
            self.names("workflow_dispatch", "mfile-bin, maple-mono-nf-cn"),
            ["maple-mono-nf-cn", "mfile-bin"],
        )

    def test_manual_input_fails_closed(self) -> None:
        rejected = [
            "",
            "   ",
            "all,reflectcpp-git",
            "all reflectcpp-git",
            "ALL",
            "reflectcpp-git reflectcpp-git",
            "asus-fa401km-acpi-fix",
            "hearthstone-linux-gui",
            "not-a-package",
            "maple-mono-nf-cn;rm -rf /",
            "maple-mono-nf-cn && true",
            "$(id)",
            "`id`",
            "maple-mono-nf-cn\nreflectcpp-git",
            "maple-mono-nf-cn\rreflectcpp-git",
            "../reflectcpp-git",
            "reflectcpp-git/PKGBUILD",
            "scripts/publish.sh",
            "*",
            "maple-mono-nf-cn*",
            "maple mono",
            "a,,b",
            "maple-mono-nf-cn,",
            ",maple-mono-nf-cn",
            "maple-mono-nf-cn,",
            "${{ secrets.AUR_SSH_PRIVATE_KEY }}",
        ]
        for raw in rejected:
            with self.subTest(raw=raw):
                with self.assertRaises(SystemExit):
                    self.names("workflow_dispatch", raw)

    def test_push_selects_only_changed_package_directories(self) -> None:
        paths = [
            "scripts/update_package.py",
            "scripts/select_packages.py",
            "scripts/publish.sh",
            "scripts/test_select_packages.py",
            "nvchecker.toml",
            "README.md",
            ".github/workflows/update-aur.yml",
            ".github/workflows/ci.yml",
            "hearthstone-linux-gui-bin/PKGBUILD",
            "hearthstone-linux-gui-bin/.SRCINFO",
            "hearthstone-linux-gui-bin/.aur-disabled",
            "hearthstone-linux-gui-appimage/PKGBUILD",
            "hearthstone-linux-gui-appimage/.SRCINFO",
        ]
        self.assertEqual(self.names("push", paths=paths), list(HEARTHSTONE))

    def test_code_only_push_selects_nothing(self) -> None:
        paths = [
            "scripts/publish.sh",
            "scripts/select_packages.py",
            "nvchecker.toml",
            ".github/workflows/update-aur.yml",
            "README.md",
        ]
        self.assertEqual(self.names("push", paths=paths), [])

    def test_push_does_not_add_an_unchanged_package(self) -> None:
        paths = ["hearthstone-linux-gui-bin/PKGBUILD", "scripts/update_package.py"]
        chosen = self.names("push", paths=paths)
        self.assertEqual(chosen, ["hearthstone-linux-gui-bin"])
        self.assertNotIn("reflectcpp-git", chosen)
        self.assertNotIn("maple-mono-nf-cn", chosen)

    def test_push_of_one_other_package_stays_that_package(self) -> None:
        self.assertEqual(
            self.names("push", paths=["reflectcpp-git/PKGBUILD", "scripts/foo.py"]),
            ["reflectcpp-git"],
        )

    def test_unsafe_diff_paths_fail_closed(self) -> None:
        for path in ("/etc/passwd", "../reflectcpp-git/PKGBUILD", "hearthstone-linux-gui-bin/../../x", "a\\b"):
            with self.subTest(path=path):
                with self.assertRaises(SystemExit):
                    self.names("push", paths=[path])

    def test_unknown_event_fails_closed(self) -> None:
        with self.assertRaises(SystemExit):
            self.names("pull_request")
        with self.assertRaises(SystemExit):
            self.names("")

    def test_schedule_ignores_a_manual_list(self) -> None:
        self.assertEqual(self.names("schedule", "hearthstone-linux-gui-bin"), list(ALL))

    def test_zero_base_and_bad_sha_do_not_run_git(self) -> None:
        with mock.patch.object(sel.subprocess, "run", side_effect=AssertionError("git must not run")):
            with self.assertRaises(SystemExit):
                sel.git_changed_paths("0" * 40, "a" * 40)
            with self.assertRaises(SystemExit):
                sel.git_changed_paths("main", "a" * 40)
            with self.assertRaises(SystemExit):
                sel.git_changed_paths("a" * 40, "b" * 39)

    def test_git_diff_failure_does_not_select_every_package(self) -> None:
        proc = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="bad revision")
        with mock.patch.object(sel.subprocess, "run", return_value=proc) as run:
            with self.assertRaises(SystemExit) as caught:
                sel.git_changed_paths("a" * 40, "b" * 40)
        self.assertIn("not selecting every package", str(caught.exception))
        argv = run.call_args.args[0]
        self.assertEqual(argv[:4], ["git", "diff", "--name-only", "--no-renames"])
        self.assertEqual(argv[4:], ["a" * 40, "b" * 40])

    def test_emit_marks_an_empty_push_as_not_running(self) -> None:
        out = Path(self.id().replace(".", "_") + ".out")
        self.addCleanup(lambda: out.unlink(missing_ok=True))
        with mock.patch.dict(os.environ, {"GITHUB_OUTPUT": str(out)}):
            sel.emit([])
        text = out.read_text(encoding="utf-8")
        self.assertIn("run=false\n", text)
        self.assertIn("matrix=[]\n", text)

    def test_allowlist_matches_publish_sh_and_excludes_staged_packages(self) -> None:
        text = (ROOT / "scripts/publish.sh").read_text(encoding="utf-8")
        case = text.split("case \"$pkg\" in\n", 1)[1].split(")", 1)[0]
        published = set(case.replace("\\\n", "").split("|"))
        published = {name.strip() for name in published if name.strip()}
        self.assertEqual(published, set(sel.PACKAGES))
        self.assertNotIn("asus-fa401km-acpi-fix", sel.PACKAGES)
        self.assertNotIn("hearthstone-linux-gui", sel.PACKAGES)


class WorkflowScopeTest(unittest.TestCase):
    def text(self) -> str:
        return (ROOT / ".github/workflows/update-aur.yml").read_text(encoding="utf-8")

    def test_push_cannot_publish_a_package_whose_directory_did_not_change(self) -> None:
        text = self.text()
        select = text.split("\n  select:\n", 1)[1].split("\n  build:\n", 1)[0]
        build, publish = text.split("\n  build:\n", 1)[1].split("\n  publish:\n", 1)
        self.assertIn("python3 scripts/select_packages.py", select)
        run_lines = [line.strip() for line in select.splitlines() if line.startswith("        run:")]
        self.assertEqual(run_lines, ["run: python3 scripts/select_packages.py"])
        self.assertNotIn("${{", run_lines[0])
        # The select command itself is a fixed script. Inputs arrive as env data.
        self.assertIn('EVENT_NAME: ${{ github.event_name }}', select)
        self.assertIn('PACKAGES: ${{ github.event.inputs.packages }}', select)
        self.assertIn('BEFORE: ${{ github.event.before }}', select)
        self.assertIn('AFTER: ${{ github.sha }}', select)
        self.assertNotIn("AUR_SSH_PRIVATE_KEY", select)
        self.assertNotIn("environment:", select)
        self.assertIn("persist-credentials: false", select)
        self.assertNotIn("contents: write", select)
        self.assertEqual(text.count("fromJson(needs.select.outputs.matrix)"), 2)
        self.assertIn("needs.select.outputs.run == 'true'", build)
        self.assertIn("needs.select.outputs.run == 'true'", publish)
        self.assertIn("environment: aur", publish)
        self.assertNotIn("environment:", build)
        self.assertNotIn("AUR_SSH_PRIVATE_KEY", build)
        # A push of scripts or this workflow must not be enough to start publication.
        paths = text.split("paths:", 1)[1].split("permissions:", 1)[0]
        self.assertNotIn("scripts/**", paths)
        self.assertNotIn("nvchecker.toml", paths)
        self.assertNotIn("update-aur.yml", paths)
        for package in ALL:
            self.assertIn(f'"{package}/**"', paths)
            self.assertNotIn(f"package: {package}", text)

    def test_secrets_stay_on_the_publish_job(self) -> None:
        text = self.text()
        self.assertEqual(text.count("environment: aur"), 1)
        self.assertEqual(text.count("secrets.AUR_SSH_PRIVATE_KEY"), 1)
        publish = text.split("\n  publish:\n", 1)[1]
        self.assertIn("environment: aur", publish)
        self.assertIn("AUR_SSH_PRIVATE_KEY", publish)
        self.assertIn("contents: write", publish)
        self.assertEqual(text.count("actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"), 3)
        jobs = text.split("\njobs:\n", 1)[1]
        self.assertEqual(jobs.count("persist-credentials: false"), 2)
        self.assertEqual(jobs.count("persist-credentials: true"), 1)


if __name__ == "__main__":
    unittest.main()
