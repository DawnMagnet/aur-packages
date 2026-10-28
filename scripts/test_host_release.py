#!/usr/bin/env python3
"""Offline tests for the host-computed version of release packages.

Network, downloads and docker are mocked. The tests also check that the
read-only path writes nothing into the checkout.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import publish_artifact as gate
import update_package as up

ROOT = Path(__file__).resolve().parent.parent
H1 = "ab" * 32


def tree_digest(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        rel = str(path.relative_to(root))
        if path.is_file():
            out[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
        else:
            out[rel] = "dir"
    return out


def no_nvchecker(name: str):
    raise AssertionError("read-only mode must not run nvchecker")


MEMPROCFS_INFO = {
    "pkgver": "5.19.1",
    "pkgrel": "20261101",
    "tag": "v5.19.1",
    "x86_64": "https://github.com/ufrisk/MemProcFS/releases/download/v5.19.1/MemProcFS_files_and_binaries_v5.19.1-linux_x64-20261101.tar.gz",
    "aarch64": "https://github.com/ufrisk/MemProcFS/releases/download/v5.19.1/MemProcFS_files_and_binaries_v5.19.1-linux_aarch64-20261101.tar.gz",
}


class FakeMoon:
    """Stands in for curl, tar and docker inside probe_moonbit."""

    def __init__(self, version: str = "0.1.20261101") -> None:
        self.version = version
        self.calls: list[list[str]] = []

    def check_call(self, args, **_kw) -> int:
        args = [str(a) for a in args]
        self.calls.append(args)
        if args[0] == "curl":
            Path(args[args.index("-o") + 1]).write_bytes(b"tarball")
        elif args[0] == "tar":
            bindir = Path(args[args.index("-C") + 1]) / "bin"
            bindir.mkdir()
            for name in ("moon", "moonc", "moonrun"):
                (bindir / name).write_bytes(b"#!/bin/false\n")
                (bindir / name).chmod(0o644)
        else:
            raise AssertionError(f"unexpected command {args}")
        return 0

    def check_output(self, args, **_kw) -> str:
        args = [str(a) for a in args]
        self.calls.append(args)
        return f"moon {self.version} (3778465 2026-11-01)\n\nFeature flags enabled: x\n"


class Base(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        # A copy of the checkout so the read-only check sees real files.
        self.root = self.tmp / "checkout"
        shutil.copytree(
            ROOT, self.root, ignore=shutil.ignore_patterns(".git", "__pycache__", "src", "pkg")
        )
        patcher = mock.patch.object(up, "ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name, value in (("NVCHECKER_TOML", self.root / "nvchecker.toml"), ("KEYFILE", self.root / "nvchecker-keyfile.toml")):
            p = mock.patch.object(up, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(up, "nvchecker_version", no_nvchecker)
        p.start()
        self.addCleanup(p.stop)
        self.out = self.tmp / "github_output"
        p = mock.patch.dict(os.environ, {"GITHUB_OUTPUT": str(self.out)})
        p.start()
        self.addCleanup(p.stop)

    def outputs(self) -> dict[str, str]:
        return dict(line.split("=", 1) for line in self.out.read_text(encoding="utf-8").splitlines())

    def artifact(self, pkgbuild: str | None) -> Path:
        art = self.tmp / "artifact"
        if pkgbuild is None:
            gate.stage_skip(art)
        else:
            art.mkdir()
            (art / "PKGBUILD").write_text(pkgbuild, encoding="utf-8")
            gate.write_manifest(art, "publish", {"PKGBUILD": (art / "PKGBUILD").read_bytes()})
        return art


class ExpectedReleaseTest(Base):
    def test_maple_same_version_keeps_main_pkgrel(self) -> None:
        main = "pkgname=maple-mono-nf-cn\npkgver=7.9\npkgrel=3\n"
        with mock.patch.object(up, "stable_github_version", lambda repo, prefix: "7.9"):
            self.assertEqual(up.expected_release("maple-mono-nf-cn", main), ("7.9", "3"))

    def test_maple_bump_resets_pkgrel_and_uses_nvchecker_toml_coordinates(self) -> None:
        seen = []

        def api(repo: str, prefix: str) -> str:
            seen.append((repo, prefix))
            return "7.10"

        main = "pkgname=maple-mono-nf-cn\npkgver=7.9\npkgrel=3\n"
        with mock.patch.object(up, "stable_github_version", api):
            self.assertEqual(up.expected_release("maple-mono-nf-cn", main), ("7.10", "1"))
        self.assertEqual(seen, [("subframe7536/maple-font", "v")])

    def test_upstream_older_than_main_fails(self) -> None:
        main = "pkgname=maple-mono-nf-cn\npkgver=8.0\npkgrel=1\n"
        with mock.patch.object(up, "stable_github_version", lambda repo, prefix: "7.9"):
            with self.assertRaises(SystemExit) as caught:
                up.expected_release("maple-mono-nf-cn", main)
        self.assertIn("older than main", str(caught.exception))

    def test_memprocfs_pkgrel_is_the_asset_date(self) -> None:
        main = (self.root / "memprocfs/PKGBUILD").read_text(encoding="utf-8")
        with mock.patch.object(up, "probe_memprocfs", lambda: MEMPROCFS_INFO):
            ver, rel, text = up.expected_release_pkgbuild("memprocfs", main)
        self.assertEqual((ver, rel), ("5.19.1", "20261101"))
        self.assertEqual(
            gate.parse_sources("memprocfs", text),
            {"source_x86_64": [MEMPROCFS_INFO["x86_64"]], "source_aarch64": [MEMPROCFS_INFO["aarch64"]]},
        )

    def test_moonbit_skips_core_and_runs_moon_sandboxed(self) -> None:
        fake = FakeMoon("0.1.20261023")
        main = "pkgname=moonbit-bin\npkgver=0.1.20261023\npkgrel=2\n"
        with mock.patch.object(up.subprocess, "check_call", fake.check_call), mock.patch.object(
            up.subprocess, "check_output", fake.check_output
        ):
            self.assertEqual(up.expected_release("moonbit-bin", main), ("0.1.20261023", "2"))
        curls = [c for c in fake.calls if c[0] == "curl"]
        self.assertEqual(len(curls), 1)
        self.assertTrue(curls[0][-1].endswith("/binaries/latest/moonbit-linux-x86_64.tar.gz"))
        docker = [c for c in fake.calls if c[0] == "docker"]
        self.assertEqual(len(docker), 1)
        args = docker[0]
        for flag in ("--network", "--read-only", "--cap-drop", "--user"):
            self.assertIn(flag, args)
        self.assertEqual(args[args.index("--network") + 1], "none")
        mounts = [args[i + 1] for i, a in enumerate(args) if a == "-v"]
        self.assertEqual(len(mounts), 1)
        self.assertTrue(mounts[0].endswith("/bin:/opt/moon/bin:ro"))
        self.assertNotIn(str(self.root), mounts[0])
        self.assertNotIn(str(ROOT), mounts[0])
        # The binary never runs on the host.
        self.assertFalse([c for c in fake.calls if c[0].endswith("/moon")])

    def test_moonbit_bump_resets_pkgrel(self) -> None:
        fake = FakeMoon("0.1.20261101")
        main = "pkgname=moonbit-bin\npkgver=0.1.20261023\npkgrel=2\n"
        with mock.patch.object(up.subprocess, "check_call", fake.check_call), mock.patch.object(
            up.subprocess, "check_output", fake.check_output
        ):
            self.assertEqual(up.expected_release("moonbit-bin", main), ("0.1.20261101", "1"))

    def test_updater_probe_still_downloads_core(self) -> None:
        fake = FakeMoon()
        with mock.patch.object(up.subprocess, "check_call", fake.check_call), mock.patch.object(
            up.subprocess, "check_output", fake.check_output
        ):
            self.assertEqual(up.probe_moonbit()["pkgver"], "0.1.20261101")
        self.assertEqual(len([c for c in fake.calls if c[0] == "curl"]), 2)
        self.assertFalse([c for c in fake.calls if c[0] == "docker"])

    def test_refuses_unsafe_or_unknown(self) -> None:
        main = "pkgname=maple-mono-nf-cn\npkgver=7.9\npkgrel=1\n"
        with mock.patch.object(up, "stable_github_version", lambda repo, prefix: "7.9;id"):
            with self.assertRaises(SystemExit):
                up.expected_release("maple-mono-nf-cn", main)
        with self.assertRaises(SystemExit):
            up.expected_release("reflectcpp-git", main)


class ExpectReleaseTest(Base):
    def test_outputs_and_writes_nothing_into_the_checkout(self) -> None:
        before = tree_digest(self.root)
        art = self.artifact((self.root / "maple-mono-nf-cn/PKGBUILD").read_text(encoding="utf-8"))
        with mock.patch.object(up, "stable_github_version", lambda repo, prefix: "7.10"):
            gate.main([
                "publish_artifact.py", "expect-release", "--package", "maple-mono-nf-cn",
                "--artifact", str(art), "--main-pkgbuild", str(self.root / "maple-mono-nf-cn/PKGBUILD"),
            ])
        outs = self.outputs()
        self.assertEqual((outs["pkgver"], outs["pkgrel"]), ("7.10", "1"))
        self.assertEqual(
            json.loads(outs["sources"]),
            {"source": ["https://github.com/subframe7536/maple-font/releases/download/v$pkgver/MapleMono-NF-CN-unhinted.zip"]},
        )
        self.assertEqual(tree_digest(self.root), before)

    def test_moonbit_writes_nothing_into_the_checkout(self) -> None:
        before = tree_digest(self.root)
        main = self.root / "moonbit-bin/PKGBUILD"
        art = self.artifact(main.read_text(encoding="utf-8"))
        fake = FakeMoon(up.read_var(main.read_text(encoding="utf-8"), "pkgver"))
        with mock.patch.object(up.subprocess, "check_call", fake.check_call), mock.patch.object(
            up.subprocess, "check_output", fake.check_output
        ):
            gate.expect_release("moonbit-bin", art, main)
        rel = up.read_var(main.read_text(encoding="utf-8"), "pkgrel")
        self.assertEqual(self.outputs()["pkgrel"], rel)
        self.assertEqual(tree_digest(self.root), before)
        for call in fake.calls:
            for arg in call:
                self.assertNotIn(str(self.root), arg)

    def test_skip_artifact_outputs_empty_without_probing(self) -> None:
        art = self.artifact(None)

        def boom(*_a, **_k):
            raise AssertionError("must not probe upstream for a skip artifact")

        with mock.patch.object(up, "expected_release", boom):
            gate.expect_release("moonbit-bin", art, self.root / "moonbit-bin/PKGBUILD")
        self.assertEqual(self.outputs(), {"pkgver": "", "pkgrel": "", "sources": ""})

    def test_refuses_git_packages_and_wrong_main(self) -> None:
        art = self.artifact(f"pkgname=reflectcpp-git\npkgver=1.r0.gabcdef1\npkgrel=1\nsha256sums=('{H1}')\n")
        with self.assertRaises(SystemExit):
            gate.expect_release("reflectcpp-git", art, self.root / "reflectcpp-git/PKGBUILD")
        shutil.rmtree(art)
        art = self.artifact((self.root / "memprocfs/PKGBUILD").read_text(encoding="utf-8"))
        with self.assertRaises(SystemExit) as caught:
            gate.expect_release("memprocfs", art, self.root / "moonbit-bin/PKGBUILD")
        self.assertIn("is not the PKGBUILD of memprocfs", str(caught.exception))

    def test_end_to_end_host_values_gate_import(self) -> None:
        main = self.root / "moonbit-bin/PKGBUILD"
        text = main.read_text(encoding="utf-8")
        ver = up.read_var(text, "pkgver")
        for artifact_text, ok in ((text, True), (text.replace(f"pkgver={ver}", "pkgver=0.1.29991231"), False)):
            with self.subTest(ok=ok):
                if (self.tmp / "artifact").exists():
                    shutil.rmtree(self.tmp / "artifact")
                self.out.write_text("", encoding="utf-8")
                art = self.artifact(artifact_text)
                fake = FakeMoon(ver)
                with mock.patch.object(up.subprocess, "check_call", fake.check_call), mock.patch.object(
                    up.subprocess, "check_output", fake.check_output
                ):
                    gate.expect_release("moonbit-bin", art, main)
                outs = self.outputs()
                dest = self.tmp / "dest" / "moonbit-bin"
                if dest.exists():
                    shutil.rmtree(dest)
                dest.mkdir(parents=True)
                (dest / "PKGBUILD").write_text(text, encoding="utf-8")
                if ok:
                    gate.import_artifact("moonbit-bin", art, dest, outs["pkgver"], None, outs["pkgrel"], outs["sources"])
                else:
                    with self.assertRaises(SystemExit):
                        gate.import_artifact("moonbit-bin", art, dest, outs["pkgver"], None, outs["pkgrel"], outs["sources"])
                    self.assertEqual((dest / "PKGBUILD").read_text(encoding="utf-8"), text)


class SourceLockTest(unittest.TestCase):
    """import must refuse any source change the host did not compute."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def run_import(self, package: str, current: str, updated: str, expected_sources: str | None):
        for leftover in (self.tmp / "a", self.tmp / package):
            if leftover.exists():
                shutil.rmtree(leftover)
        art = self.tmp / "a"
        art.mkdir()
        (art / "PKGBUILD").write_text(updated, encoding="utf-8")
        gate.write_manifest(art, "publish", {"PKGBUILD": (art / "PKGBUILD").read_bytes()})
        dest = self.tmp / package
        dest.mkdir()
        (dest / "PKGBUILD").write_text(current, encoding="utf-8")
        ver, rel = up.read_var(updated, "pkgver"), up.read_var(updated, "pkgrel")
        if package.endswith("-git"):
            commit = up.GIT_HASH_RE.search(ver).group(1).ljust(40, "0")
            gate.import_artifact(package, art, dest, ver, commit)
        else:
            gate.import_artifact(package, art, dest, ver, None, rel, expected_sources)
        return (dest / "PKGBUILD").read_text(encoding="utf-8")

    def real(self, package: str) -> str:
        return (ROOT / package / "PKGBUILD").read_text(encoding="utf-8")

    def mutations(self, package: str, text: str, name: str) -> list[tuple[str, str, str]]:
        """(label, mutated text, expected error fragment)."""
        items = gate.parse_sources(package, text)[name]
        start = text.index(f"{name}=(")
        end = text.index(")\n", start) + 1
        def render(values):
            return f"{name}=(" + "\n  ".join(f'"{v}"' for v in values) + ")"
        out = [
            ("other https", text[:start] + render(["https://evil.invalid/x.tar.gz", *items[1:]]) + text[end:],
             f"{name}[0] is 'https://evil.invalid/x.tar.gz'"),
            ("extra item", text[:start] + render([*items, "https://evil.invalid/extra"]) + text[end:],
             f"{name}[{len(items)}] 'https://evil.invalid/extra' is extra"),
        ]
        if len(items) > 1:
            out.append(("missing item", text[:start] + render(items[:-1]) + text[end:],
                        f"{name}[{len(items) - 1}] is missing"))
            out.append(("reordered", text[:start] + render([items[1], items[0], *items[2:]]) + text[end:],
                        f"{name}[0] is {items[1]!r}"))
        else:
            out.append(("missing item", text[:start] + f"{name}=()" + text[end:], f"{name}[0] is missing"))
        return out

    def check(self, package: str, name: str, host_text: str | None = None) -> None:
        current = self.real(package)
        good = host_text or current
        expected = json.dumps(gate.parse_sources(package, good))
        # unchanged passes
        self.assertEqual(self.run_import(package, current, good, expected), good)
        for label, bad, needle in self.mutations(package, good, name):
            with self.subTest(package=package, case=label):
                with self.assertRaises(SystemExit) as caught:
                    self.run_import(package, current, bad, expected)
                msg = str(caught.exception)
                self.assertIn(package, msg)
                self.assertIn(needle, msg)
                self.assertEqual((self.tmp / package / "PKGBUILD").read_text(encoding="utf-8"), current)

    def test_moonbit_multiline_source_is_locked(self) -> None:
        self.check("moonbit-bin", "source")

    def test_maple_source_is_locked(self) -> None:
        self.check("maple-mono-nf-cn", "source")

    def test_memprocfs_source_must_equal_host_urls(self) -> None:
        current = self.real("memprocfs")
        with mock.patch.object(up, "probe_memprocfs", lambda: MEMPROCFS_INFO):
            _v, _r, host_text = up.expected_release_pkgbuild("memprocfs", current)
        self.check("memprocfs", "source_x86_64", host_text)
        # The artifact keeping main's (stale) URLs is also refused.
        stale = up.set_var(up.set_var(current, "pkgver", "5.19.1"), "pkgrel", "20261101")
        with self.assertRaises(SystemExit) as caught:
            self.run_import("memprocfs", current, stale, json.dumps(gate.parse_sources("memprocfs", host_text)))
        self.assertIn("memprocfs: source_aarch64[0] is", str(caught.exception))

    def test_git_source_must_equal_main(self) -> None:
        for package in ("reflectcpp-git", "cuda-api-wrappers-git"):
            self.check(package, "source")

    def test_missing_or_bad_expected_sources_fail_closed(self) -> None:
        text = self.real("moonbit-bin")
        for raw, needle in ((None, "missing host-computed --expected-sources"), ("", "missing host-computed"),
                            ("not json", "invalid --expected-sources"), ("{}", "invalid --expected-sources"),
                            ('{"install": ["x"]}', "invalid --expected-sources")):
            with self.subTest(raw=raw):
                with self.assertRaises(SystemExit) as caught:
                    self.run_import("moonbit-bin", text, text, raw)
                self.assertIn(needle, str(caught.exception))

    def test_added_or_removed_arch_array_is_refused(self) -> None:
        text = self.real("memprocfs")
        expected = json.dumps(gate.parse_sources("memprocfs", text))
        added = text.replace("source_aarch64=", 'source_i686=("https://evil.invalid/x")\nsource_aarch64=')
        with self.assertRaises(SystemExit) as caught:
            self.run_import("memprocfs", text, added, expected)
        self.assertIn("artifact adds source_i686", str(caught.exception))
        removed = "\n".join(l for l in text.splitlines() if not l.startswith("source_aarch64=")) + "\n"
        with self.assertRaises(SystemExit) as caught:
            self.run_import("memprocfs", text, removed, expected)
        self.assertIn("missing source_aarch64", str(caught.exception))

    def test_parser_never_evaluates_and_refuses_shell(self) -> None:
        bad = [
            'source=("$(curl evil)")',
            'source=("`id`")',
            'source=("https://x") ; id',
            'source+=("https://x")',
            '  source=("https://x")',
            'source=(https://x\\ y)',
            'source=("https://x"',
            'source=("a")\nsource=("b")',
        ]
        for line in bad:
            with self.subTest(line=line):
                with self.assertRaises(SystemExit):
                    gate.parse_sources("p", f"pkgname=p\n{line}\n")
        with mock.patch.object(gate.subprocess if hasattr(gate, "subprocess") else up.subprocess, "run",
                               side_effect=AssertionError("must not run anything")):
            self.assertEqual(
                gate.parse_sources("p", "source=('lit $x' word \"https://a#b=${pkgver}\")\n"),
                {"source": ["lit $x", "word", "https://a#b=${pkgver}"]},
            )


ALL_PACKAGES = ("maple-mono-nf-cn", "memprocfs", "moonbit-bin", "cuda-api-wrappers-git", "reflectcpp-git")


def bump_ver(package: str, ver: str) -> str:
    if package.endswith("-git"):
        return ver.replace(".r", ".r1", 1)  # r0 -> r10, r12 -> r112: a newer describe
    return ver + ".1"


class VersionRuleTest(unittest.TestCase):
    """pkgver / pkgrel come from the host, never from the build."""

    def real(self, package: str) -> str:
        return (ROOT / package / "PKGBUILD").read_text(encoding="utf-8")

    def with_version(self, text: str, ver: str | None = None, rel: str | None = None) -> str:
        if ver is not None:
            text = up.set_var(text, "pkgver", ver)
        if rel is not None:
            text = up.set_var(text, "pkgrel", rel)
        return text

    def stage_and_check(self, package: str, baseline: str, candidate: str) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "base").write_text(baseline, encoding="utf-8")
            out = root / "out"
            out.mkdir()
            (out / "PKGBUILD").write_text(candidate, encoding="utf-8")
            errors = []
            for run in (
                lambda: gate.stage(package, root / "base", out, root / "stage"),
                lambda: gate.check_pkgbuild(package, root / "base", out / "PKGBUILD", None),
            ):
                try:
                    run()
                except SystemExit as exc:
                    errors.append(str(exc))
            if errors and len(errors) != 2:
                self.fail(f"stage and check-pkgbuild disagree: {errors}")
            if errors:
                raise SystemExit(errors[0])

    def test_stage_and_check_reject_build_chosen_versions(self) -> None:
        for package in ALL_PACKAGES:
            base = self.real(package)
            ver = up.read_var(base, "pkgver")
            cases = [("pkgrel=99999", self.with_version(base, rel="99999"))]
            if not package.endswith("-git"):
                cases += [
                    ("pkgver=99999", self.with_version(base, ver="99999")),
                    ("pkgver up, pkgrel=2", self.with_version(base, ver=bump_ver(package, ver), rel="2")),
                    ("pkgver up, pkgrel=1", self.with_version(base, ver=bump_ver(package, ver), rel="1")),
                ]
            else:
                cases.append(("pkgver=99999", self.with_version(base, ver="99999.r0.g" + ver.rsplit(".g", 1)[1])))
            for label, cand in cases:
                if cand == base:
                    continue
                with self.subTest(package=package, case=label):
                    with self.assertRaises(SystemExit) as caught:
                        self.stage_and_check(package, base, cand)
                    self.assertIn(package if "pkgrel" in label or not package.endswith("-git") else "pkgver", str(caught.exception))
            with self.subTest(package=package, case="unchanged"):
                self.stage_and_check(package, base, base)

    def test_moonbit_99999_is_refused_at_stage(self) -> None:
        base = self.real("moonbit-bin")
        with self.assertRaises(SystemExit) as caught:
            self.stage_and_check("moonbit-bin", base, self.with_version(base, ver="99999"))
        self.assertIn("moonbit-bin: build changed pkgver", str(caught.exception))

    def test_import_pkgrel_rule_all_packages(self) -> None:
        for package in ALL_PACKAGES:
            main = self.real(package)
            ver, rel = up.read_var(main, "pkgver"), up.read_var(main, "pkgrel")
            new_ver = bump_ver(package, ver)
            if package == "memprocfs":
                continue  # pkgrel is the asset date; pinned by the host value instead
            refused = [
                ("same pkgver, pkgrel=99999", self.with_version(main, rel="99999")),
                ("same pkgver, other pkgrel", self.with_version(main, rel=str(int(rel) + 1))),
                ("pkgver up, pkgrel=2", self.with_version(main, ver=new_ver, rel="2")),
                ("pkgver up, pkgrel=99999", self.with_version(main, ver=new_ver, rel="99999")),
            ]
            for label, cand in refused:
                with self.subTest(package=package, case=label):
                    with self.assertRaises(SystemExit) as caught:
                        gate.assert_pkgrel_rule(package, main, cand)
                    self.assertIn(f"{package}: artifact pkgrel", str(caught.exception))
            for label, cand in (("same", main), ("pkgver up, pkgrel=1", self.with_version(main, ver=new_ver, rel="1"))):
                with self.subTest(package=package, case=label):
                    gate.assert_pkgrel_rule(package, main, cand)
        # -git: a longer hash of the same commit is "same pkgver".
        main = self.real("cuda-api-wrappers-git")
        ver = up.read_var(main, "pkgver")
        gate.assert_pkgrel_rule("cuda-api-wrappers-git", main, self.with_version(main, ver=ver + "1"))

    def import_full(self, package: str, main: str, cand: str, host_ver: str, host_rel: str) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            art = root / "a"
            art.mkdir()
            (art / "PKGBUILD").write_text(cand, encoding="utf-8")
            gate.write_manifest(art, "publish", {"PKGBUILD": (art / "PKGBUILD").read_bytes()})
            dest = root / package
            dest.mkdir()
            (dest / "PKGBUILD").write_text(main, encoding="utf-8")
            if package.endswith("-git"):
                commit = up.GIT_HASH_RE.search(host_ver).group(1).ljust(40, "0")
                gate.import_artifact(package, art, dest, host_ver, commit)
            else:
                sources = json.dumps(gate.parse_sources(package, main))
                gate.import_artifact(package, art, dest, host_ver, None, host_rel, sources)

    def test_import_end_to_end_even_if_host_agrees(self) -> None:
        # The pkgrel rule holds even when the host-computed values agree with
        # a bad artifact (e.g. a -git package, where the host only pins pkgver).
        for package in ("moonbit-bin", "maple-mono-nf-cn", "reflectcpp-git", "cuda-api-wrappers-git"):
            main = self.real(package)
            ver = up.read_var(main, "pkgver")
            new_ver = bump_ver(package, ver)
            for label, cand_ver, cand_rel in (("pkgrel=99999", ver, "99999"), ("pkgver up, pkgrel=2", new_ver, "2")):
                cand = self.with_version(main, ver=cand_ver, rel=cand_rel)
                with self.subTest(package=package, case=label):
                    with self.assertRaises(SystemExit) as caught:
                        self.import_full(package, main, cand, cand_ver, cand_rel)
                    self.assertIn("artifact pkgrel", str(caught.exception))
            with self.subTest(package=package, case="normal bump"):
                self.import_full(package, main, self.with_version(main, ver=new_ver, rel="1"), new_ver, "1")
            with self.subTest(package=package, case="unchanged"):
                self.import_full(package, main, main, ver, up.read_var(main, "pkgrel"))

    def test_import_rejects_pkgver_99999_for_release_packages(self) -> None:
        for package in ("moonbit-bin", "maple-mono-nf-cn", "memprocfs"):
            main = self.real(package)
            ver, rel = up.read_var(main, "pkgver"), up.read_var(main, "pkgrel")
            with self.subTest(package=package):
                with self.assertRaises(SystemExit) as caught:
                    self.import_full(package, main, self.with_version(main, ver="99999"), ver, rel)
                self.assertIn("does not match the host-computed pkgver", str(caught.exception))


class WorkflowTest(unittest.TestCase):
    def test_publish_wires_release_expectation_into_import(self) -> None:
        text = (ROOT / ".github/workflows/update-aur.yml").read_text(encoding="utf-8")
        publish = text.split("\n  publish:\n", 1)[1]
        step = publish.split("id: expect_release\n", 1)[1].split("\n      - name:", 1)[0]
        self.assertIn("!endsWith(matrix.package, '-git')", step)
        self.assertIn("publish_artifact.py expect-release", step)
        self.assertNotIn("AUR_SSH_PRIVATE_KEY", step)
        run = step.split("run: |", 1)[1]
        self.assertNotIn("${{", run)
        imp = publish.split("id: import\n", 1)[1].split("\n      - name:", 1)[0]
        self.assertIn(
            "EXPECTED_PKGVER: ${{ steps.expect.outputs.pkgver || steps.expect_release.outputs.pkgver }}", imp
        )
        self.assertIn("EXPECTED_PKGREL: ${{ steps.expect_release.outputs.pkgrel }}", imp)
        self.assertIn('--expected-pkgrel "$EXPECTED_PKGREL"', imp)
        self.assertIn("EXPECTED_SOURCES: ${{ steps.expect_release.outputs.sources }}", imp)
        self.assertIn('--expected-sources "$EXPECTED_SOURCES"', imp)
        self.assertNotIn("${{", imp.split("run: |", 1)[1])
        self.assertLess(publish.index("id: expect_release"), publish.index("id: import"))


if __name__ == "__main__":
    unittest.main()
