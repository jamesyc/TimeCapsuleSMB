"""The shared resource lock file (AGENTS.md, "Shared Resource Locks").

~/tmp/semaphore.txt holds one Markdown table row per resource: the VM checkout,
the VM Samba lanes, and each LAN device. A row is free when its Holder column is
empty. This module claims and releases rows for one holder and never touches a
row another holder has: a row held by someone else is Busy, and a resource that
has no row at all is an error (AGENTS.md: ask the user).
"""
from __future__ import annotations

import datetime
from pathlib import Path
import re
import subprocess

LOCK_FILE = Path.home() / "tmp" / "semaphore.txt"
ROW = re.compile(r"^\| ([^|]+?)\s*\|([^|]*)\|([^|]*)\|([^|]*)\|$")

VM_CHECKOUT = "VM checkout"
VM_LANES = "VM Samba lanes"
DEVICE_ROWS = {"6": "NetBSD 6", "4le": "NetBSD 4"}


class Busy(Exception):
    """A row another holder has."""


class Unknown(Exception):
    """A resource the lock file has no row for."""


def holders(text: str) -> dict[str, str]:
    """Each resource's holder ("" when free)."""
    rows = {}
    for line in text.splitlines():
        match = ROW.match(line)
        if match and match.group(1).strip() not in ("Resource",) and not set(match.group(1).strip()) <= {"-"}:
            rows[match.group(1).strip()] = match.group(2).strip()
    return rows


def _row(name: str, holder: str = "", since: str = "", doing: str = "") -> str:
    if not holder:
        return f"| {name:<14} |        |                  |       |"
    return f"| {name:<14} | {holder} | {since} | {doing} |"


def claim(text: str, resources: list[str], holder: str, doing: str, now: str) -> str:
    """The lock file with every resource held by holder; all or nothing."""
    current = holders(text)
    for name in resources:
        if name not in current:
            raise Unknown(name)
        if current[name] not in ("", holder):
            raise Busy(f"{name}: {current[name]}")
    wanted = set(resources)
    lines = []
    for line in text.splitlines():
        match = ROW.match(line)
        if match and match.group(1).strip() in wanted:
            line = _row(match.group(1).strip(), holder, now, doing)
        lines.append(line)
    return "\n".join(lines) + "\n"


def release(text: str, resources: list[str], holder: str) -> str:
    """The lock file with holder's rows among resources freed; others untouched."""
    wanted = set(resources)
    lines = []
    for line in text.splitlines():
        match = ROW.match(line)
        if match and match.group(1).strip() in wanted and match.group(2).strip() == holder:
            line = _row(match.group(1).strip())
        lines.append(line)
    return "\n".join(lines) + "\n"


def default_holder(root: Path) -> str:
    """Branch or worktree name plus commit, as AGENTS.md asks."""
    def git(*args: str) -> str:
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True).stdout.strip()

    branch = git("rev-parse", "--abbrev-ref", "HEAD") or root.name
    return f"{branch} ({root} @ {git('rev-parse', '--short', 'HEAD')})"


class Locks:
    """Hold rows for a block: claim on entry (then re-read to confirm, as
    AGENTS.md asks), release on exit even when the block fails."""

    def __init__(self, resources: list[str], holder: str, doing: str, path: Path = LOCK_FILE) -> None:
        self.resources, self.holder, self.doing, self.path = resources, holder, doing, path

    def __enter__(self) -> "Locks":
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        self.path.write_text(claim(self.path.read_text(), self.resources, self.holder, self.doing, now))
        held = holders(self.path.read_text())
        lost = [name for name in self.resources if held.get(name) != self.holder]
        if lost:
            raise Busy(f"lost after claiming: {lost}")
        return self

    def __exit__(self, *exc) -> None:
        self.path.write_text(release(self.path.read_text(), self.resources, self.holder))
