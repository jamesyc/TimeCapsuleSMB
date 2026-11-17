"""Clean up the processes and process groups a test started.

Stdlib only. tests/samba/run.py keeps its own copy of the kill_group rule
because build/_samba4x.sh runs it as a plain script, where this module does
not import.
"""
from collections.abc import Iterable
from pathlib import Path
import os
import signal
import subprocess


def kill_group(pgid: int) -> None:
    """SIGKILL whatever is still alive in a process group the test started.

    A group with no live member is not an error: Linux answers ESRCH, and
    Darwin answers EPERM while the group still holds a zombie nobody has
    reaped yet.
    """
    if pgid <= 1 or pgid == os.getpgrp():
        raise ValueError(f'refusing to kill process group {pgid}')
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def kill_acp_fixture_groups(calls: Path) -> None:
    """Kill the groups of every ACP that tests/native/integration/acp_fixture.c
    recorded as a group leader ("key pid parent time" lines) in CALLS."""
    if not calls.exists():
        return
    for line in calls.read_text().splitlines():
        fields = line.split()
        if fields[2] == 'parent':
            kill_group(int(fields[1]))


def signal_if_ours(pids: Iterable[int], folder: Path, sig: int) -> None:
    """Send SIG to each of PIDS whose command line still runs from FOLDER, a
    directory unique to the test.

    Once the test's process is reaped, the system may give its pid to an
    unrelated process; that one is left alone, and so is a zombie, which
    needs no signal. The trailing separator keeps a folder ending in
    "manager1" from matching one ending in "manager10".
    """
    arguments = [argument for pid in pids for argument in ('-p', str(pid))]
    if not arguments:
        return
    listing = subprocess.run(['ps', '-ww', '-o', 'pid=,command=', *arguments],
                             capture_output=True, text=True).stdout
    marker = os.path.join(folder, '')
    for line in listing.splitlines():
        pid, _, command = line.strip().partition(' ')
        if marker not in command:
            continue
        try:
            os.kill(int(pid), sig)
        except ProcessLookupError:
            pass
