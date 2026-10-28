#!/usr/bin/env python3
"""Offline tests for the AUR RPC comparison. The RPC is always mocked."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import aur_rpc

ROOT = Path(__file__).resolve().parent.parent


class FakeResponse(io.BytesIO):
    def __init__(self, body: bytes, status: int = 200) -> None:
        super().__init__(body)
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def rpc_body(name: str | None, version: str = "") -> bytes:
    results = [] if name is None else [{"Name": name, "Version": version}]
    return json.dumps(
        {"resultcount": len(results), "results": results, "type": "multiinfo", "version": 5}
    ).encode()


class Opener:
    """Returns queued responses or raises queued exceptions, recording URLs."""

    def __init__(self, *items) -> None:
        self.items = list(items)
        self.calls: list[tuple[str, float]] = []

    def __call__(self, url: str, timeout: float):
        self.calls.append((url, timeout))
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        return FakeResponse(item)


def no_sleep(_seconds: float) -> None:
    return None


class FetchTest(unittest.TestCase):
    def test_reads_version_and_uses_v5_info_with_timeout(self) -> None:
        opener = Opener(rpc_body("moonbit-bin", "0.1.20261023-1"))
        got = aur_rpc.fetch_aur_version("moonbit-bin", opener=opener, sleep=no_sleep)
        self.assertEqual(got, "0.1.20261023-1")
        url, timeout = opener.calls[0]
        self.assertEqual(url, "https://aur.archlinux.org/rpc/v5/info?arg%5B%5D=moonbit-bin")
        self.assertGreater(timeout, 0)

    def test_missing_package_is_none(self) -> None:
        opener = Opener(rpc_body(None))
        self.assertIsNone(aur_rpc.fetch_aur_version("moonbit-bin", opener=opener, sleep=no_sleep))

    def test_retries_then_succeeds(self) -> None:
        sleeps: list[float] = []
        opener = Opener(
            urllib.error.URLError("boom"),
            TimeoutError("timed out"),
            rpc_body("memprocfs", "5.19.0-20261014"),
        )
        got = aur_rpc.fetch_aur_version("memprocfs", opener=opener, sleep=sleeps.append)
        self.assertEqual(got, "5.19.0-20261014")
        self.assertEqual(len(opener.calls), 3)
        self.assertEqual(len(sleeps), 2)

    def test_gives_up_after_retries(self) -> None:
        opener = Opener(*(urllib.error.URLError("down") for _ in range(3)))
        with self.assertRaises(aur_rpc.RpcError):
            aur_rpc.fetch_aur_version("memprocfs", opener=opener, sleep=no_sleep)
        self.assertEqual(len(opener.calls), 3)

    def test_bad_responses_are_errors(self) -> None:
        bad = [
            b"not json",
            json.dumps({"type": "error", "error": "Incorrect request type"}).encode(),
            json.dumps({"type": "search", "results": []}).encode(),
            rpc_body("other-package", "1.0-1"),
            rpc_body("memprocfs", "1.0; rm -rf /"),
            rpc_body("memprocfs", ""),
            b"[]",
        ]
        for body in bad:
            with self.subTest(body=body[:40]):
                opener = Opener(body, body, body)
                with self.assertRaises(aur_rpc.RpcError):
                    aur_rpc.fetch_aur_version("memprocfs", opener=opener, sleep=no_sleep)

    def test_http_error_is_an_error(self) -> None:
        err = urllib.error.HTTPError("u", 503, "unavailable", {}, None)  # type: ignore[arg-type]
        opener = Opener(err, err, err)
        with self.assertRaises(aur_rpc.RpcError):
            aur_rpc.fetch_aur_version("memprocfs", opener=opener, sleep=no_sleep)

    def test_invalid_package_name_is_refused_before_any_request(self) -> None:
        opener = Opener()
        with self.assertRaises(aur_rpc.RpcError):
            aur_rpc.fetch_aur_version("../x&arg[]=y", opener=opener, sleep=no_sleep)
        self.assertEqual(opener.calls, [])


class CompareTest(unittest.TestCase):
    MOONBIT = "pkgname=moonbit-bin\npkgver=0.1.20261023\npkgrel=2\n"
    REFLECT = "pkgname=reflectcpp-git\npkgver=0.26.2.r12.g5457ea3c\npkgrel=1\n"

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        out = self.root / "github_output"
        out.write_text("", encoding="utf-8")
        patcher = mock.patch.dict(os.environ, {"GITHUB_OUTPUT": str(out)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.output = out

    def pkgdir(self, name: str, pkgbuild: str) -> Path:
        d = self.root / name
        d.mkdir()
        (d / "PKGBUILD").write_text(pkgbuild, encoding="utf-8")
        return d

    def outputs(self) -> dict[str, str]:
        lines = self.output.read_text(encoding="utf-8").splitlines()
        return dict(line.split("=", 1) for line in lines)

    def run_compare(self, name: str, pkgbuild: str, changed: bool, aur) -> bool:
        d = self.pkgdir(name, pkgbuild)

        def fetch(package: str):
            self.assertEqual(package, name)
            if isinstance(aur, Exception):
                raise aur
            return aur

        return aur_rpc.compare(name, changed, d, fetch=fetch)

    def test_moonbit_pkgrel_bump_is_changed(self) -> None:
        # The motivating bug: repo -2, AUR -1, upstream unchanged.
        self.assertTrue(self.run_compare("moonbit-bin", self.MOONBIT, False, "0.1.20261023-1"))
        self.assertEqual(self.outputs(), {"changed": "true", "aur_version": "0.1.20261023-1"})
        old = (self.root / "moonbit-bin/.update-old-version").read_text(encoding="utf-8")
        self.assertEqual(old.strip(), "0.1.20261023-1")

    def test_equal_keeps_changed_as_is(self) -> None:
        self.assertFalse(self.run_compare("moonbit-bin", self.MOONBIT, False, "0.1.20261023-2"))
        self.assertEqual(self.outputs()["changed"], "false")
        self.assertFalse((self.root / "moonbit-bin/.update-old-version").exists())

    def test_upstream_change_stays_changed_when_equal(self) -> None:
        self.assertTrue(self.run_compare("moonbit-bin", self.MOONBIT, True, "0.1.20261023-2"))

    def test_missing_on_aur_is_changed(self) -> None:
        self.assertTrue(self.run_compare("moonbit-bin", self.MOONBIT, False, None))
        self.assertEqual(self.outputs(), {"changed": "true", "aur_version": ""})

    def test_aur_newer_fails_with_clear_message(self) -> None:
        for changed in (False, True):
            with self.subTest(changed=changed):
                d = self.root / "moonbit-bin"
                if d.exists():
                    (d / "PKGBUILD").unlink()
                    d.rmdir()
                with self.assertRaises(SystemExit) as caught:
                    self.run_compare("moonbit-bin", self.MOONBIT, changed, "0.1.20261101-1")
                msg = str(caught.exception)
                self.assertIn("AUR has 0.1.20261101-1", msg)
                self.assertIn("newer than the repo", msg)

    def test_rpc_failure_falls_back_and_never_triggers(self) -> None:
        for changed in (False, True):
            with self.subTest(changed=changed):
                self.output.write_text("", encoding="utf-8")
                d = self.root / "moonbit-bin"
                if d.exists():
                    (d / "PKGBUILD").unlink()
                    d.rmdir()
                got = self.run_compare("moonbit-bin", self.MOONBIT, changed, aur_rpc.RpcError("down"))
                self.assertEqual(got, changed)
                self.assertEqual(self.outputs()["changed"], "true" if changed else "false")
                self.assertFalse((d / ".update-old-version").exists())

    def test_git_same_commit_other_abbrev_is_equal(self) -> None:
        aur = "0.26.2.r12.g5457ea3-1"
        self.assertFalse(self.run_compare("reflectcpp-git", self.REFLECT, False, aur))

    def test_git_newer_commit_on_repo_is_changed(self) -> None:
        aur = "0.26.2.r10.g587b72ee-1"
        self.assertTrue(self.run_compare("reflectcpp-git", self.REFLECT, False, aur))

    def test_memprocfs_date_pkgrel(self) -> None:
        text = "pkgname=memprocfs\npkgver=5.19.0\npkgrel=20261014\n"
        self.assertTrue(self.run_compare("memprocfs", text, False, "5.19.0-20260901"))
        self.output.write_text("", encoding="utf-8")
        (self.root / "memprocfs/PKGBUILD").unlink()
        (self.root / "memprocfs/.update-old-version").unlink()
        (self.root / "memprocfs").rmdir()
        with self.assertRaises(SystemExit):
            self.run_compare("memprocfs", text, False, "5.19.0-20261101")

    def test_epoch_is_respected(self) -> None:
        text = "pkgname=maple-mono-nf-cn\nepoch=1\npkgver=7.9\npkgrel=1\n"
        self.assertTrue(self.run_compare("maple-mono-nf-cn", text, False, "8.0-1"))

    def test_disabled_package_is_not_compared(self) -> None:
        d = self.pkgdir("moonbit-bin", self.MOONBIT)
        (d / ".aur-disabled").write_text("", encoding="utf-8")

        def fetch(_package: str):
            raise AssertionError("must not query the RPC")

        self.assertFalse(aur_rpc.compare("moonbit-bin", True, d, fetch=fetch))
        self.assertEqual(self.outputs()["changed"], "false")

    def test_cli_with_mocked_urlopen(self) -> None:
        self.pkgdir("moonbit-bin", self.MOONBIT)
        opener = Opener(rpc_body("moonbit-bin", "0.1.20261023-1"))
        with mock.patch.object(aur_rpc, "_default_open", opener):
            aur_rpc.main(
                ["aur_rpc.py", "--package", "moonbit-bin", "--changed", "false", "--root", str(self.root)]
            )
        self.assertEqual(self.outputs()["changed"], "true")
        self.assertEqual(len(opener.calls), 1)

    def test_cli_rpc_down_keeps_changed_false(self) -> None:
        self.pkgdir("moonbit-bin", self.MOONBIT)
        opener = Opener(*(urllib.error.URLError("down") for _ in range(3)))
        with mock.patch.object(aur_rpc, "_default_open", opener), mock.patch.object(
            aur_rpc.time, "sleep", no_sleep
        ):
            aur_rpc.main(
                ["aur_rpc.py", "--package", "moonbit-bin", "--changed", "false", "--root", str(self.root)]
            )
        self.assertEqual(self.outputs()["changed"], "false")
        self.assertEqual(len(opener.calls), 3)


class WorkflowTest(unittest.TestCase):
    def test_build_gate_uses_the_aur_comparison(self) -> None:
        text = (ROOT / ".github/workflows/update-aur.yml").read_text(encoding="utf-8")
        build = text.split("\n  publish:\n", 1)[0]
        self.assertIn('python3 scripts/aur_rpc.py --package "$PACKAGE" --changed "$CHANGED"', build)
        self.assertIn("CHANGED: ${{ steps.aur.outputs.changed }}", build)
        self.assertLess(build.index("id: upd"), build.index("id: aur"))
        self.assertLess(build.index("id: aur"), build.index("id: gate"))
        self.assertNotIn("aur_rpc.py", text.split("\n  publish:\n", 1)[1])


if __name__ == "__main__":
    unittest.main()
