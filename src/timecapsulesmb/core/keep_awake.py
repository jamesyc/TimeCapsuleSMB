from __future__ import annotations

import os
import signal
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager

from timecapsulesmb.core.process import SPAWN_LOCK


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
    pid = _start_caffeinate() if sys.platform == "darwin" else None
    try:
        yield
    finally:
        _stop(pid)


def _start_caffeinate() -> int | None:
    """Start caffeinate; return its pid, or None when it cannot start.

    posix_spawn, never fork, under SPAWN_LOCK (see core.process): this is the
    one child that needs a session of its own, which subprocess would fork for.
    """
    try:
        with SPAWN_LOCK:
            return os.posix_spawn(
                CAFFEINATE,
                # -i prevents idle sleep only, on battery too; the display may
                # still sleep. -w releases the assertion when this process exits,
                # even if it is killed before the finally block runs.
                [CAFFEINATE, "-i", "-w", str(os.getpid())],
                os.environ,
                # Never inherit the helper's pipes: the app reads them until EOF.
                file_actions=[(os.POSIX_SPAWN_OPEN, fd, os.devnull, os.O_RDWR, 0) for fd in (0, 1, 2)],
                # A terminal Ctrl-C must not end it while the command still runs.
                setsid=True,
            )
    except Exception:
        # Sleep prevention is best effort, including unsupported spawn flags.
        return None


def _stop(pid: int | None) -> None:
    if pid is None:
        return
    try:
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + STOP_TIMEOUT_SECONDS
        while os.waitpid(pid, os.WNOHANG)[0] == 0:
            if time.monotonic() >= deadline:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
                return
            time.sleep(0.01)
    except (ProcessLookupError, ChildProcessError):
        # Already gone and reaped: nothing to stop.
        return
