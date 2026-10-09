"""core.process: local programs start with posix_spawn, never fork (issue #371)."""
from __future__ import annotations

import errno
import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from timecapsulesmb.core import process
from timecapsulesmb.core.process import FORKING_OPTIONS, SPAWN_LOCK, popen_process, run_process


class SpawnTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.bin = Path(tmp.name)
        self.tool = self.bin / "tc-test-tool"
        self.tool.write_text('#!/bin/sh\necho "$0 $*"\n')
        self.tool.chmod(0o755)
        self.env = {"PATH": str(self.bin)}

    def test_a_program_is_found_on_the_childs_path_and_spawned_by_absolute_path(self) -> None:
        spawned: list[str] = []
        real_spawn = os.posix_spawn

        def spy(path, *args, **kwargs):
            spawned.append(path)
            return real_spawn(path, *args, **kwargs)

        with mock.patch.object(os, "posix_spawn", spy):
            proc = run_process(["tc-test-tool", "a b"], env=self.env, capture_output=True, text=True)
            child = popen_process(["tc-test-tool", "c"], env=self.env, stdout=subprocess.PIPE, text=True)
            output, _ = child.communicate(timeout=10)
        self.assertEqual(proc.stdout, f"{self.tool} a b\n")
        self.assertEqual(output, f"{self.tool} c\n")
        if subprocess._USE_POSIX_SPAWN:  # every macOS Python; glibc Linux
            self.assertEqual(spawned, [str(self.tool)] * 2)

    def test_a_missing_program_raises_file_not_found_naming_it(self) -> None:
        for start in (run_process, popen_process):
            with self.subTest(start=start.__name__):
                with self.assertRaises(FileNotFoundError) as raised:
                    start(["tc-no-such-tool"], env=self.env)
                self.assertEqual(raised.exception.filename, "tc-no-such-tool")
                self.assertEqual(raised.exception.errno, errno.ENOENT)

    def test_options_that_would_make_subprocess_fork_are_refused(self) -> None:
        for option in sorted(FORKING_OPTIONS):
            with self.subTest(option=option):
                with mock.patch.object(process.subprocess, "Popen") as popen:
                    with self.assertRaisesRegex(TypeError, f"{option} would make subprocess fork"):
                        run_process(["tc-test-tool"], env=self.env, **{option: True})
                popen.assert_not_called()

    def test_the_child_and_its_pipes_are_made_under_the_spawn_lock(self) -> None:
        # Another thread's spawn must not land between os.pipe() and the
        # close-on-exec flag it gets afterwards (macOS has no pipe2).
        held: list[bool] = []
        real_popen = subprocess.Popen

        def recording_popen(*args, **kwargs):
            held.append(SPAWN_LOCK.locked())
            return real_popen(*args, **kwargs)

        with mock.patch.object(process.subprocess, "Popen", recording_popen):
            run_process(["tc-test-tool"], env=self.env, capture_output=True)
        self.assertEqual(held, [True])
        self.assertFalse(SPAWN_LOCK.locked())

    def test_a_long_lived_child_never_holds_another_commands_pipe(self) -> None:
        # Children that outlive their spawn by seconds, started while other
        # threads run quick commands with captured output: a quick command
        # that inherited none of them never waits for one to exit.
        stop = time.monotonic() + 2
        slow: list[float] = []
        children: list[subprocess.Popen] = []

        def start_long_lived() -> None:
            while time.monotonic() < stop:
                children.append(popen_process(
                    ["/bin/sleep", "3"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                ))
                time.sleep(0.01)

        def run_quick() -> None:
            while time.monotonic() < stop:
                started = time.monotonic()
                run_process(["/usr/bin/true"], capture_output=True)
                if time.monotonic() - started > 2:
                    slow.append(time.monotonic() - started)

        threads = [threading.Thread(target=start_long_lived)] + [threading.Thread(target=run_quick) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        for child in children:
            child.kill()
            child.wait()
        self.assertEqual(slow, [])


class RunProcessTests(unittest.TestCase):
    """run_process keeps subprocess.run's behaviour."""

    def test_input_output_and_status(self) -> None:
        proc = run_process(["/bin/sh", "-c", "tr a-z A-Z; echo err >&2; exit 3"], input=b"hi\n", capture_output=True)
        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (3, b"HI\n", b"err\n"))

    def test_check_raises_with_the_output(self) -> None:
        with self.assertRaises(subprocess.CalledProcessError) as raised:
            run_process(["/bin/sh", "-c", "echo out; exit 4"], capture_output=True, text=True, check=True)
        self.assertEqual((raised.exception.returncode, raised.exception.stdout), (4, "out\n"))

    def test_a_timeout_kills_and_reaps_the_child(self) -> None:
        children: list[subprocess.Popen] = []
        real_popen = process.popen_process

        def recording(*args, **kwargs):
            children.append(real_popen(*args, **kwargs))
            return children[-1]

        with mock.patch.object(process, "popen_process", recording):
            with self.assertRaises(subprocess.TimeoutExpired):
                run_process(["/bin/sleep", "30"], timeout=0.2)
        [child] = children
        self.assertEqual(child.returncode, -9)

    def test_an_interrupt_kills_the_child_and_propagates(self) -> None:
        children: list[subprocess.Popen] = []
        real_popen = process.popen_process

        def recording(*args, **kwargs):
            children.append(real_popen(*args, **kwargs))
            return children[-1]

        def interrupted(self, *args, **kwargs):
            raise KeyboardInterrupt

        with mock.patch.object(process, "popen_process", recording), \
             mock.patch.object(subprocess.Popen, "communicate", interrupted):
            with self.assertRaises(KeyboardInterrupt):
                run_process(["/bin/sleep", "30"], stdout=subprocess.PIPE)
        [child] = children
        self.assertIsNotNone(child.returncode)


if __name__ == "__main__":
    unittest.main()
