"""keep_system_awake: the caffeinate child that holds off macOS idle sleep."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import unittest
from unittest import mock

import pytest

from timecapsulesmb.core import keep_awake
from timecapsulesmb.core.keep_awake import CAFFEINATE, keep_system_awake

# These tests exercise the real _start_caffeinate, which conftest.py
# replaces with a no-op for every unmarked test.
pytestmark = pytest.mark.real_keep_awake


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _wait_until_gone(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _process_alive(pid):
            return True
        time.sleep(0.05)
    return not _process_alive(pid)


class KeepSystemAwakeTests(unittest.TestCase):
    PID = 4242

    def patched_spawn(self, *, waits: list[tuple[int, int]] | None = None, spawn_error: OSError | None = None):
        """Patch the spawn, kill and wait calls; return the mocks."""
        spawn = mock.patch.object(
            keep_awake.os, "posix_spawn", return_value=self.PID, side_effect=spawn_error,
        ).start()
        kill = mock.patch.object(keep_awake.os, "kill").start()
        waitpid = mock.patch.object(keep_awake.os, "waitpid", side_effect=waits or [(self.PID, 0)]).start()
        mock.patch.object(keep_awake.sys, "platform", "darwin").start()
        self.addCleanup(mock.patch.stopall)
        return spawn, kill, waitpid

    def test_darwin_starts_caffeinate_for_this_process_and_stops_it_after(self) -> None:
        spawn, kill, waitpid = self.patched_spawn()
        with keep_system_awake():
            spawn.assert_called_once_with(
                CAFFEINATE,
                [CAFFEINATE, "-i", "-w", str(os.getpid())],
                os.environ,
                file_actions=[(os.POSIX_SPAWN_OPEN, fd, os.devnull, os.O_RDWR, 0) for fd in (0, 1, 2)],
                setsid=True,
            )
            kill.assert_not_called()
        kill.assert_called_once_with(self.PID, signal.SIGTERM)
        waitpid.assert_called_once_with(self.PID, os.WNOHANG)

    def test_failing_operation_still_stops_caffeinate_and_keeps_its_error(self) -> None:
        _spawn, kill, _waitpid = self.patched_spawn()
        with self.assertRaisesRegex(RuntimeError, "deploy failed"):
            with keep_system_awake():
                raise RuntimeError("deploy failed")
        kill.assert_called_once_with(self.PID, signal.SIGTERM)

    def test_caffeinate_that_ignores_terminate_is_killed_and_reaped(self) -> None:
        _spawn, kill, waitpid = self.patched_spawn(waits=[(0, 0), (self.PID, 9)])
        with mock.patch.object(keep_awake, "STOP_TIMEOUT_SECONDS", 0):
            with keep_system_awake():
                pass
        self.assertEqual(kill.call_args_list, [mock.call(self.PID, signal.SIGTERM), mock.call(self.PID, signal.SIGKILL)])
        self.assertEqual(waitpid.call_args_list, [mock.call(self.PID, os.WNOHANG), mock.call(self.PID, 0)])

    def test_a_caffeinate_already_gone_does_not_fail_the_operation(self) -> None:
        for error in (ProcessLookupError(), ChildProcessError()):
            with self.subTest(error=type(error).__name__):
                _spawn, kill, waitpid = self.patched_spawn()
                waitpid.side_effect = error
                with keep_system_awake():
                    pass
                kill.assert_called_once_with(self.PID, signal.SIGTERM)
                mock.patch.stopall()

    def test_other_platforms_start_nothing(self) -> None:
        ran = []
        with mock.patch.object(keep_awake.sys, "platform", "linux"):
            with mock.patch.object(keep_awake.os, "posix_spawn") as spawn:
                with keep_system_awake():
                    ran.append(True)
        spawn.assert_not_called()
        self.assertEqual(ran, [True])

    def test_operation_runs_when_caffeinate_cannot_start(self) -> None:
        for error in (FileNotFoundError(CAFFEINATE), PermissionError(CAFFEINATE)):
            with self.subTest(error=type(error).__name__):
                ran = []
                _spawn, kill, _waitpid = self.patched_spawn(spawn_error=error)
                with keep_system_awake():
                    ran.append(True)
                self.assertEqual(ran, [True])
                kill.assert_not_called()
                mock.patch.stopall()


@unittest.skipUnless(sys.platform == "darwin" and os.path.exists(CAFFEINATE), "needs macOS caffeinate")
class RealCaffeinateTests(unittest.TestCase):
    def test_real_caffeinate_lives_only_inside_the_block(self) -> None:
        started = []
        real_start = keep_awake._start_caffeinate

        def recording_start():
            pid = real_start()
            started.append(pid)
            return pid

        with mock.patch.object(keep_awake, "_start_caffeinate", recording_start):
            with keep_system_awake():
                self.assertEqual(len(started), 1)
                self.assertIsNotNone(started[0])
                self.assertTrue(_process_alive(started[0]))
        # Stopped and reaped: no zombie keeps the pid.
        self.assertFalse(_process_alive(started[0]))

    def test_caffeinate_exits_by_itself_when_its_process_is_killed(self) -> None:
        # The helper can die without running its finally block (the app kills
        # it, or os._exit); -w must still release the assertion.
        script = (
            "import os, sys\n"
            "from timecapsulesmb.core import keep_awake\n"
            "print(keep_awake._start_caffeinate(), flush=True)\n"
            "os._exit(1)\n"
        )
        env = dict(os.environ, PYTHONPATH=os.pathsep.join(p for p in sys.path if p))
        child = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env, timeout=30)
        caffeinate_pid = int(child.stdout.strip())
        self.assertTrue(_wait_until_gone(caffeinate_pid), "caffeinate outlived the process it was watching")


if __name__ == "__main__":
    unittest.main()
