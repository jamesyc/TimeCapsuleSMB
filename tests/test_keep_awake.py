"""keep_system_awake: the caffeinate child that holds off macOS idle sleep."""
from __future__ import annotations

import os
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
    def test_darwin_starts_caffeinate_for_this_process_and_stops_it_after(self) -> None:
        process = mock.Mock()
        with mock.patch.object(keep_awake.sys, "platform", "darwin"):
            with mock.patch.object(keep_awake.subprocess, "Popen", return_value=process) as popen:
                with keep_system_awake():
                    popen.assert_called_once_with(
                        [CAFFEINATE, "-i", "-w", str(os.getpid())],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        start_new_session=True,
                    )
                    process.terminate.assert_not_called()
        process.terminate.assert_called_once_with()
        process.wait.assert_called_once_with(timeout=keep_awake.STOP_TIMEOUT_SECONDS)
        process.kill.assert_not_called()

    def test_failing_operation_still_stops_caffeinate_and_keeps_its_error(self) -> None:
        process = mock.Mock()
        with mock.patch.object(keep_awake.sys, "platform", "darwin"):
            with mock.patch.object(keep_awake.subprocess, "Popen", return_value=process):
                with self.assertRaisesRegex(RuntimeError, "deploy failed"):
                    with keep_system_awake():
                        raise RuntimeError("deploy failed")
        process.terminate.assert_called_once_with()

    def test_caffeinate_that_ignores_terminate_is_killed(self) -> None:
        process = mock.Mock()
        process.wait.side_effect = [subprocess.TimeoutExpired(CAFFEINATE, keep_awake.STOP_TIMEOUT_SECONDS), 0]
        with mock.patch.object(keep_awake.sys, "platform", "darwin"):
            with mock.patch.object(keep_awake.subprocess, "Popen", return_value=process):
                with keep_system_awake():
                    pass
        process.kill.assert_called_once_with()
        self.assertEqual(process.wait.call_count, 2)

    def test_other_platforms_start_nothing(self) -> None:
        ran = []
        with mock.patch.object(keep_awake.sys, "platform", "linux"):
            with mock.patch.object(keep_awake.subprocess, "Popen") as popen:
                with keep_system_awake():
                    ran.append(True)
        popen.assert_not_called()
        self.assertEqual(ran, [True])

    def test_operation_runs_when_caffeinate_cannot_start(self) -> None:
        for error in (FileNotFoundError(CAFFEINATE), PermissionError(CAFFEINATE)):
            with self.subTest(error=type(error).__name__):
                ran = []
                with mock.patch.object(keep_awake.sys, "platform", "darwin"):
                    with mock.patch.object(keep_awake.subprocess, "Popen", side_effect=error):
                        with keep_system_awake():
                            ran.append(True)
                self.assertEqual(ran, [True])


@unittest.skipUnless(sys.platform == "darwin" and os.path.exists(CAFFEINATE), "needs macOS caffeinate")
class RealCaffeinateTests(unittest.TestCase):
    def test_real_caffeinate_lives_only_inside_the_block(self) -> None:
        started = []
        real_start = keep_awake._start_caffeinate

        def recording_start():
            process = real_start()
            started.append(process)
            return process

        with mock.patch.object(keep_awake, "_start_caffeinate", recording_start):
            with keep_system_awake():
                self.assertEqual(len(started), 1)
                self.assertIsNotNone(started[0])
                self.assertIsNone(started[0].poll())
        self.assertIsNotNone(started[0].poll())

    def test_caffeinate_exits_by_itself_when_its_process_is_killed(self) -> None:
        # The helper can die without running its finally block (the app kills
        # it, or os._exit); -w must still release the assertion.
        script = (
            "import os, sys\n"
            "from timecapsulesmb.core import keep_awake\n"
            "process = keep_awake._start_caffeinate()\n"
            "print(process.pid, flush=True)\n"
            "os._exit(1)\n"
        )
        env = dict(os.environ, PYTHONPATH=os.pathsep.join(p for p in sys.path if p))
        child = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env, timeout=30)
        caffeinate_pid = int(child.stdout.strip())
        self.assertTrue(_wait_until_gone(caffeinate_pid), "caffeinate outlived the process it was watching")


if __name__ == "__main__":
    unittest.main()
