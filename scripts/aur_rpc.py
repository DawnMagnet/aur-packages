#!/usr/bin/env python3
"""Compare the repo PKGBUILD with the version published on the AUR.

update_package.py only compares upstream with the repo PKGBUILD. A repo-only
bump (for example moonbit-bin pkgrel 1 -> 2) therefore reported changed=false
and was never pushed. The build job runs this right after update_package.py:

  repo newer than AUR (alpm vercmp)  -> changed=true
  package missing on AUR             -> exit 1, except the hard-coded
                                     first-publish allowlist (mfile-bin only)
                                     when the RPC definitively reports
                                     resultcount 0. That sets changed=true and
                                     logs "first publish: mfile-bin". Any RPC
                                     error still fails closed and does not
                                     create a package.
  repo equal to AUR                  -> changed unchanged
  AUR newer than repo                -> exit 1 with a clear message
  RPC failed after retries           -> changed unchanged (see below)

RPC failure policy: the AUR comparison fails closed. When the RPC cannot be
read (network error, HTTP error, timeout, malformed or error response, wrong
package name) the result is logged and changed= is exactly the value
update_package.py produced. The RPC can then neither trigger a publish nor
block one that the upstream check already wanted; the publish job keeps its
own import checks either way.

The request is a read-only GET of
https://aur.archlinux.org/rpc/v5/info?arg[]=<pkg> with a timeout and retries.
No credentials are sent.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

from alpmver import vercmp
from update_package import read_var, same_pkgver_commit, set_output

RPC_URL = "https://aur.archlinux.org/rpc/v5/info"
# Worst case per package when the AUR is down: 3 x 10 s + 2 s + 4 s = 36 s.
TIMEOUT = 10
ATTEMPTS = 3
PKG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
# [epoch:]pkgver-pkgrel as the AUR reports it.
AUR_VERSION_RE = re.compile(r"^(?:[0-9]+:)?[A-Za-z0-9._+]+-[0-9]+(?:\.[0-9]+)?$")
MAX_RESPONSE = 1024 * 1024
# Hard-coded. Nothing in the environment, a workflow input, or an artifact
# can add a name. The publish job checks out main; this tuple is the code
# on that checkout.
FIRST_PUBLISH_ALLOWLIST = ("mfile-bin",)
BOOTSTRAP_FILES = (".SRCINFO", "PKGBUILD")


class RpcError(Exception):
    """The AUR RPC could not be read or returned something unusable."""


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


def _default_open(url: str, timeout: float):
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "DawnMagnet-aur-packages"},
        method="GET",
    )
    return urllib.request.urlopen(req, timeout=timeout)


def rpc_url(package: str) -> str:
    return RPC_URL + "?" + urllib.parse.urlencode({"arg[]": package})


def parse_response(package: str, body: bytes) -> str | None:
    """Return the AUR version of package, or None when it is not on the AUR."""
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RpcError(f"malformed JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise RpcError("response is not an object")
    # Absent is only a successful multiinfo with resultcount 0. An error
    # field, any other type, or a count that does not match results is not
    # absence.
    if "error" in data and data.get("error") not in (None, ""):
        raise RpcError(f"RPC error: {data.get('error')!r}")
    if data.get("type") == "error":
        raise RpcError(f"RPC error: {data.get('error')!r}")
    if data.get("type") != "multiinfo":
        raise RpcError(f"unexpected response type {data.get('type')!r}")
    results = data.get("results")
    if not isinstance(results, list):
        raise RpcError("response has no results list")
    count = data.get("resultcount")
    if type(count) is not int or count != len(results):
        raise RpcError(f"resultcount {count!r} does not match {len(results)} results")
    matches = [r for r in results if isinstance(r, dict) and r.get("Name") == package]
    if len(results) != len(matches) or len(matches) > 1:
        raise RpcError(f"response does not describe exactly {package}")
    if not matches:
        return None
    version = matches[0].get("Version")
    if not isinstance(version, str) or not AUR_VERSION_RE.fullmatch(version):
        raise RpcError(f"unusable AUR version {version!r}")
    return version


def fetch_aur_version(
    package: str,
    *,
    opener: Callable | None = None,
    sleep: Callable[[float], None] | None = None,
    attempts: int = ATTEMPTS,
    timeout: float = TIMEOUT,
) -> str | None:
    """Query the AUR RPC. Retries transport and parse failures."""
    if not PKG_RE.fullmatch(package):
        raise RpcError(f"invalid package name {package!r}")
    opener = opener or _default_open
    sleep = sleep or time.sleep
    url = rpc_url(package)
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            with opener(url, timeout) as resp:
                status = getattr(resp, "status", 200)
                if status != 200:
                    raise RpcError(f"HTTP {status}")
                body = resp.read(MAX_RESPONSE + 1)
            if len(body) > MAX_RESPONSE:
                raise RpcError("response too large")
            return parse_response(package, body)
        except (OSError, urllib.error.URLError, RpcError, ValueError) as exc:
            last = exc
            log(f"AUR RPC attempt {attempt}/{attempts} for {package} failed: {exc}")
            if attempt < attempts:
                sleep(2 * attempt)
    raise RpcError(f"AUR RPC failed after {attempts} attempts: {last}")


def repo_version(pkgbuild: str) -> str:
    ver = read_var(pkgbuild, "pkgver")
    rel = read_var(pkgbuild, "pkgrel")
    epoch_match = re.search(r"^epoch=([0-9]+)\s*$", pkgbuild, re.M)
    full = f"{ver}-{rel}"
    if epoch_match and epoch_match.group(1) != "0":
        full = f"{epoch_match.group(1)}:{full}"
    return full


def same_version(package: str, repo: str, aur: str) -> bool:
    if vercmp(repo, aur) == 0:
        return True
    if not package.endswith("-git"):
        return False
    # git describe hashes may differ only in abbreviation length.
    repo_ver, _, repo_rel = repo.rpartition("-")
    aur_ver, _, aur_rel = aur.rpartition("-")
    return repo_rel == aur_rel and same_pkgver_commit(repo_ver, aur_ver)


def decide(package: str, changed: bool, repo: str, aur: str | None) -> tuple[bool, str]:
    """Return (changed, reason). Raises SystemExit when the AUR is newer.

    aur is None only when parse_response saw a definitive absence
    (HTTP 200, type multiinfo, resultcount 0, no error). RPC failures never
    arrive here as None; the caller keeps the upstream changed flag and does
    not create a package.
    """
    if aur is None:
        if FIRST_PUBLISH_ALLOWLIST != ("mfile-bin",):
            raise SystemExit("first-publish allowlist must be exactly ('mfile-bin',)")
        if package == "mfile-bin":
            # Exact log line. Not taken from an argument, env var, or artifact.
            log("first publish: mfile-bin")
            return True, "first publish: mfile-bin"
        raise SystemExit(
            f"{package}: the AUR RPC reports no such package. This workflow only "
            "updates packages that already exist on the AUR; create it there by "
            "hand (or fix the package name) before the workflow can publish it."
        )
    if same_version(package, repo, aur):
        return changed, f"{package} repo {repo} matches AUR {aur}"
    if vercmp(repo, aur) > 0:
        return True, f"{package} repo {repo} is newer than AUR {aur}; treating as changed"
    raise SystemExit(
        f"{package}: AUR has {aur}, which is newer than the repo PKGBUILD {repo}. "
        "Someone pushed to the AUR outside this repo; bring the repo PKGBUILD up "
        "to date (or bump pkgrel) before the workflow can publish again."
    )


def compare(
    package: str,
    changed: bool,
    pkgdir: Path,
    *,
    fetch: Callable[[str], str | None] | None = None,
) -> bool:
    if not PKG_RE.fullmatch(package):
        raise SystemExit(f"invalid package name: {package}")
    if (pkgdir / ".aur-disabled").is_file():
        log(f"{package} is .aur-disabled; not comparing with the AUR")
        set_output("changed", "false")
        set_output("aur_version", "")
        return False
    pkgbuild = (pkgdir / "PKGBUILD").read_text(encoding="utf-8")
    repo = repo_version(pkgbuild)
    fetch = fetch or fetch_aur_version
    try:
        aur = fetch(package)
    except RpcError as exc:
        log(
            f"warning: {exc}. Skipping the AUR comparison (fail closed); "
            f"keeping changed={'true' if changed else 'false'} from the upstream check."
        )
        set_output("changed", "true" if changed else "false")
        set_output("aur_version", "")
        return changed
    new_changed, reason = decide(package, changed, repo, aur)
    log(reason)
    if new_changed and not changed and aur is not None:
        # test_build.sh passes this to check-pkgbuild, which then requires the
        # built version to be newer than what the AUR has.
        old = pkgdir / ".update-old-version"
        if not old.exists():
            old.write_text(aur + "\n", encoding="utf-8")
    set_output("changed", "true" if new_changed else "false")
    set_output("aur_version", aur or "")
    return new_changed


def assert_bootstrap_files(names: list[str]) -> None:
    """Reject any bootstrap tree that is not exactly PKGBUILD and .SRCINFO."""
    if tuple(sorted(names)) != BOOTSTRAP_FILES:
        raise SystemExit(
            "bootstrap commit must contain exactly PKGBUILD and .SRCINFO, "
            f"found {sorted(names)}"
        )


def repo_is_empty(path: Path) -> bool:
    """True when the clone has no commits and no refs.

    `git rev-parse HEAD` fails on an unborn repository. Any ref means the
    AUR repo is not empty and bootstrap must stop.
    """
    head = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--verify", "HEAD"],
        capture_output=True,
        text=True,
    )
    refs = subprocess.run(
        ["git", "-C", str(path), "show-ref"],
        capture_output=True,
        text=True,
    )
    has_head = head.returncode == 0
    has_refs = refs.returncode == 0 and bool(refs.stdout.strip())
    if has_head or has_refs:
        return False
    if head.returncode not in (0, 128) or refs.returncode not in (0, 1):
        raise SystemExit(f"could not tell whether {path} is an empty git repository")
    return True


def require_empty_repo(path: Path) -> None:
    if not repo_is_empty(path):
        raise SystemExit(f"{path} is not an empty AUR repository; refusing first publish")


def require_bootstrap_absence(
    package: str,
    *,
    opener: Callable | None = None,
    sleep: Callable[[float], None] | None = None,
) -> None:
    """Raise unless package is mfile-bin and the RPC resultcount is 0.

    Timeout, non-200, bad JSON, an error field, and any other failure are
    SystemExit. They are not treated as absence.
    """
    try:
        absent = package_absent(package, opener=opener, sleep=sleep)
    except RpcError as exc:
        raise SystemExit(f"first publish denied: {exc}") from exc
    if not absent:
        raise SystemExit(
            f"first publish denied for {package}: "
            "not an allowlisted package with a definitive empty RPC result"
        )


def package_absent(package: str, *, opener: Callable | None = None, sleep: Callable[[float], None] | None = None) -> bool:
    """True only for a definitive resultcount 0 of an allowlisted package.

    RPC errors propagate as RpcError (the caller denies the bootstrap).
    A package that exists returns False. A name that is not mfile-bin is
    never absent-for-creation, even when the RPC resultcount is 0.
    """
    if FIRST_PUBLISH_ALLOWLIST != ("mfile-bin",) or package != "mfile-bin":
        return False
    version = fetch_aur_version(package, opener=opener, sleep=sleep)
    return version is None


def confirm_published(
    package: str,
    expected: str,
    *,
    fetch: Callable[[], str | None] | None = None,
    attempts: int = 5,
    sleep: Callable[[float], None] | None = None,
) -> None:
    """Fail the job unless the AUR RPC reports exactly expected (0.1.0-1 today)."""
    if package != "mfile-bin" or FIRST_PUBLISH_ALLOWLIST != ("mfile-bin",):
        raise SystemExit(f"refusing to confirm a first publish of {package}")
    if not AUR_VERSION_RE.fullmatch(expected):
        raise SystemExit(f"refusing expected version {expected!r}")
    sleep = sleep or time.sleep
    fetch = fetch or (lambda: fetch_aur_version(package))
    last: object = None
    for attempt in range(1, attempts + 1):
        try:
            got = fetch()
        except RpcError as exc:
            last = exc
            log(f"AUR confirm attempt {attempt}/{attempts} failed: {exc}")
            if attempt == attempts:
                raise SystemExit(
                    f"first publish of {package} failed confirmation: {exc}"
                ) from exc
            sleep(2)
            continue
        if got == expected:
            log(f"AUR {package} is {got}")
            return
        if got is None and attempt < attempts:
            log(f"AUR confirm attempt {attempt}/{attempts}: {package} is not visible yet")
            sleep(2)
            continue
        raise SystemExit(
            f"first publish of {package} failed confirmation: "
            f"AUR RPC returned {got!r}, expected {expected!r}"
        )
    raise SystemExit(f"first publish of {package} failed confirmation: {last!r}")


def _bootstrap_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    sub = parser.add_subparsers(dest="cmd")

    absent = sub.add_parser("package-absent")
    absent.add_argument("--package", required=True)

    check = sub.add_parser("bootstrap-check")
    check.add_argument("--package", required=True)
    check.add_argument("--repo", type=Path, required=True)

    files = sub.add_parser("assert-files")
    files.add_argument("names", nargs="*")

    confirm = sub.add_parser("confirm")
    confirm.add_argument("--package", required=True)
    confirm.add_argument("--version", required=True)
    return parser


def bootstrap_main(argv: list[str]) -> int:
    args = _bootstrap_parser().parse_args(argv)
    if args.cmd == "package-absent":
        try:
            require_bootstrap_absence(args.package)
        except SystemExit:
            return 1
        return 0
    if args.cmd == "bootstrap-check":
        require_empty_repo(args.repo)
        require_bootstrap_absence(args.package)
        log("first publish: mfile-bin")
        return 0
    if args.cmd == "assert-files":
        assert_bootstrap_files(list(args.names))
        return 0
    if args.cmd == "confirm":
        confirm_published(args.package, args.version)
        return 0
    raise SystemExit(f"unknown bootstrap command {args.cmd!r}")


def main(argv: list[str]) -> int:
    if len(argv) > 1 and argv[1] in {
        "package-absent",
        "bootstrap-check",
        "assert-files",
        "confirm",
    }:
        return bootstrap_main(argv[1:])
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--package", required=True)
    parser.add_argument("--changed", required=True, choices=["true", "false"])
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    args = parser.parse_args(argv[1:])
    if not PKG_RE.fullmatch(args.package):
        raise SystemExit(f"invalid package name: {args.package}")
    compare(args.package, args.changed == "true", args.root / args.package)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
