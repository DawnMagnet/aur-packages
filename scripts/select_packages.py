#!/usr/bin/env python3
"""Choose which packages update-aur.yml may build and publish.

schedule publishes every allowlisted package.
workflow_dispatch publishes `all` or an explicit allowlisted list.
push publishes only allowlisted package directories that changed in that
push. A change to scripts, nvchecker.toml, or the workflow file does not
add any package. Invalid input fails closed and selects nothing.

The matrix entries (timeout, free_disk) come from this file, never from
the dispatch string.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

# Publish allowlist, in matrix order. Timeouts and free_disk are trusted
# constants. asus-fa401km-acpi-fix is intentionally absent.
PACKAGES: dict[str, dict[str, object]] = {
    "maple-mono-nf-cn": {"timeout": 45, "free_disk": False},
    "memprocfs": {"timeout": 45, "free_disk": False},
    "moonbit-bin": {"timeout": 60, "free_disk": False},
    "mfile-bin": {"timeout": 45, "free_disk": False},
    "hearthstone-linux-gui-bin": {"timeout": 60, "free_disk": False},
    "hearthstone-linux-gui-appimage": {"timeout": 45, "free_disk": False},
    "cuda-api-wrappers-git": {"timeout": 180, "free_disk": True},
    "reflectcpp-git": {"timeout": 120, "free_disk": False},
}

NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*")
SHA_RE = re.compile(r"[0-9a-f]{40}")
ZERO_SHA = "0" * 40
EVENTS = ("schedule", "workflow_dispatch", "push")


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


def set_output(key: str, value: str) -> None:
    print(f"{key}={value}")
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(f"{key}={value}\n")


def row(name: str) -> dict[str, object]:
    spec = PACKAGES[name]
    return {
        "package": name,
        "timeout": spec["timeout"],
        "free_disk": spec["free_disk"],
    }


def rows_for(names: list[str]) -> list[dict[str, object]]:
    return [row(name) for name in names]


def parse_manual(raw: str) -> list[str]:
    """Return allowlisted names in allowlist order. `all` means every package."""
    if raw is None or any(ord(ch) < 32 or ord(ch) == 127 for ch in raw):
        raise SystemExit("refusing packages input with control characters")
    if re.search(r"[^A-Za-z0-9._+\-, ]", raw):
        raise SystemExit("refusing packages input that is not names separated by spaces or commas")
    stripped = raw.strip()
    if not stripped:
        raise SystemExit("packages input is empty; pass all or allowlisted names")
    if ",," in stripped or "  " in stripped or stripped.startswith(",") or stripped.endswith(","):
        raise SystemExit("refusing empty package name")
    parts = [part for part in re.split(r"[ ,]+", stripped) if part]
    if any(not NAME_RE.fullmatch(part) for part in parts):
        raise SystemExit("refusing a package token that is not a single name")
    if "all" in parts:
        if parts != ["all"]:
            raise SystemExit("refusing to mix all with package names")
        return list(PACKAGES)
    if len(parts) != len(set(parts)):
        raise SystemExit("refusing duplicate package names")
    unknown = [part for part in parts if part not in PACKAGES]
    if unknown:
        raise SystemExit(f"unknown package: {unknown[0]}")
    chosen = set(parts)
    return [name for name in PACKAGES if name in chosen]


def packages_from_paths(paths: list[str]) -> list[str]:
    """Allowlisted package directories touched by a diff. Other paths add nothing."""
    found: set[str] = set()
    for path in paths:
        if not isinstance(path, str) or path.startswith("/") or "\\" in path:
            raise SystemExit("refusing an unsafe diff path")
        parts = path.split("/")
        if any(part in ("", ".", "..") for part in parts):
            raise SystemExit("refusing an unsafe diff path")
        # A package is selected only when a file inside its directory changed.
        if len(parts) >= 2 and parts[0] in PACKAGES:
            found.add(parts[0])
    return [name for name in PACKAGES if name in found]


def git_changed_paths(before: str, after: str) -> list[str]:
    if not SHA_RE.fullmatch(before) or not SHA_RE.fullmatch(after):
        raise SystemExit("refusing a push range that is not two commit ids")
    if before == ZERO_SHA:
        raise SystemExit("refusing a push with an unknown base; not selecting every package")
    proc = subprocess.run(
        ["git", "diff", "--name-only", "--no-renames", before, after],
        check=False,
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "git diff failed").strip()
        raise SystemExit(f"git diff failed; not selecting every package: {detail}")
    return proc.stdout.splitlines()


def select(event: str, packages_input: str | None = None, changed_paths: list[str] | None = None) -> list[str]:
    if event not in EVENTS:
        raise SystemExit(f"refusing event {event!r}")
    if event == "schedule":
        return list(PACKAGES)
    if event == "workflow_dispatch":
        return parse_manual(packages_input if packages_input is not None else "")
    if changed_paths is None:
        raise SystemExit("push selection requires the changed path list")
    return packages_from_paths(changed_paths)


def emit(names: list[str]) -> None:
    body = json.dumps(rows_for(names), separators=(",", ":"))
    # run is false only when the push changed no package directory.
    # An invalid request never gets here: it raises before emit.
    set_output("run", "true" if names else "false")
    set_output("matrix", body)
    shown = ", ".join(names) if names else "(none)"
    log(f"selected {len(names)} package(s): {shown}")


def main() -> int:
    event = os.environ.get("EVENT_NAME", "")
    if event == "push":
        names = select(event, changed_paths=git_changed_paths(
            os.environ.get("BEFORE", ""),
            os.environ.get("AFTER", ""),
        ))
    elif event == "workflow_dispatch":
        names = select(event, packages_input=os.environ.get("PACKAGES", ""))
    else:
        names = select(event)
    emit(names)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:
        log(f"error: {exc}")
        raise
