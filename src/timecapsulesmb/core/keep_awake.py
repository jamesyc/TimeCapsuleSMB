from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager


# Full path: the macOS app runs the helper with a narrowed PATH.
CAFFEINATE = "/usr/bin/caffeinate"
STOP_TIMEOUT_SECONDS = 2.0


@contextmanager
def keep_system_awake() -> Iterator[None]:
    """Keep macOS from idle-sleeping while a long device operation runs.

    A Mac that sleeps mid-deploy drops the SSH session and leaves the device
    half-installed. Elsewhere this does nothing: Linux hosts such as a
    Raspberry Pi do not idle-suspend, and none of this is required to work.
    """
    process = _start_caffeinate() if sys.platform == "darwin" else None
    try:
        yield
    finally:
        _stop(process)


def _start_caffeinate() -> subprocess.Popen[bytes] | None:
    try:
        return subprocess.Popen(
            # -i prevents idle sleep only, on battery too; the display may
            # still sleep. -w releases the assertion when this process exits,
            # even if it is killed before the finally block runs.
            [CAFFEINATE, "-i", "-w", str(os.getpid())],
            # Never inherit the helper's pipes: the app reads them until EOF.
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            # A terminal Ctrl-C must not end it while the command still runs.
            start_new_session=True,
        )
    except OSError:
        return None


def _stop(process: subprocess.Popen[bytes] | None) -> None:
    if process is None:
        return
    process.terminate()
    try:
        process.wait(timeout=STOP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
