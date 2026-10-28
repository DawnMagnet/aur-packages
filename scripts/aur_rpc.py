#!/usr/bin/env python3
"""Compare the repo PKGBUILD with the version published on the AUR.

update_package.py only compares upstream with the repo PKGBUILD. A repo-only
bump (for example moonbit-bin pkgrel 1 -> 2) therefore reported changed=false
and was never pushed. The build job runs this right after update_package.py:

  repo newer than AUR (alpm vercmp)  -> changed=true
  package missing on AUR             -> exit 1 (create it on the AUR first)
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
    if data.get("type") == "error":
        raise RpcError(f"RPC error: {data.get('error')!r}")
    if data.get("type") != "multiinfo":
        raise RpcError(f"unexpected response type {data.get('type')!r}")
    results = data.get("results")
    if not isinstance(results, list):
        raise RpcError("response has no results list")
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
    """Return (changed, reason). Raises SystemExit when the AUR is newer."""
    if aur is None:
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


def main(argv: list[str]) -> int:
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
