"""core.process: local programs start with posix_spawn, never fork (issue #371)."""
from __future__ import annotations

import errno
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from timecapsulesmb.core import process
from timecapsulesmb.core.process import SPAWN_LOCK, popen_process, run_process


# 3.9 calls through the extension module; newer CPython imports an alias.
FORK_TARGET = "subprocess._fork_exec" if hasattr(subprocess, "_fork_exec") else "_posixsubprocess.fork_exec"


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

    def test_the_spy_intercepts_cpythons_actual_fork_path(self) -> None:
        with mock.patch(FORK_TARGET, side_effect=AssertionError("fork intercepted")) as fork:
            with self.assertRaisesRegex(AssertionError, "fork intercepted"):
                subprocess.Popen(["/usr/bin/true"], cwd="/")
            fork.assert_called_once()

    def test_unsafe_options_never_reach_the_fork_fallback(self) -> None:
        # Independent cases: do not derive the expectations from the guard.
        cases = [
            {"cwd": "/"}, {"start_new_session": True}, {"process_group": 0},
            {"preexec_fn": lambda: None}, {"pass_fds": (3,)}, {"close_fds": True},
            {"umask": 0o077}, {"user": os.getuid()}, {"group": os.getgid()},
            {"extra_groups": []}, {"executable": "sh"}, {"shell": True},
            {"future_option": True}, {"stderr": subprocess.STDOUT},
        ]
        cases.extend({stream: fd} for stream in ("stdin", "stdout", "stderr") for fd in (0, 1, 2))
        cases.extend({stream: mock.Mock(fileno=lambda: 1)} for stream in ("stdin", "stdout", "stderr"))
        for kwargs in cases:
            with self.subTest(kwargs=kwargs), \
                 mock.patch(FORK_TARGET, side_effect=AssertionError("forked")) as fork, \
                 mock.patch.object(os, "posix_spawn", wraps=os.posix_spawn) as spawn:
                with self.assertRaises(TypeError):
                    run_process(["tc-test-tool"], env=self.env, **kwargs)
                fork.assert_not_called()
                spawn.assert_not_called()

    def test_supported_redirections_really_spawn(self) -> None:
        with tempfile.TemporaryFile() as file:
            cases = [
                {}, {"capture_output": True}, {"input": b"", "capture_output": True},
                {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL},
                {"stdin": file, "stdout": file, "stderr": file},
                {"stdin": file.fileno(), "stdout": file.fileno(), "stderr": file.fileno()},
                {"stdout": subprocess.PIPE, "stderr": subprocess.STDOUT, "text": True},
                {"stdout": file, "stderr": subprocess.STDOUT},
                {"stdout": subprocess.PIPE, "encoding": "utf-8", "errors": "strict", "bufsize": 1},
                {"capture_output": True, "universal_newlines": True, "restore_signals": False},
            ]
            for kwargs in cases:
                with self.subTest(kwargs=kwargs), \
                     mock.patch(FORK_TARGET, side_effect=AssertionError("forked")) as fork, \
                     mock.patch.object(os, "posix_spawn", wraps=os.posix_spawn) as spawn:
                    result = run_process(["/usr/bin/true"], **kwargs)
                    self.assertEqual(result.returncode, 0)
                    spawn.assert_called_once()
                    fork.assert_not_called()

    def test_invalid_commands_are_rejected(self) -> None:
        for cmd in ("/usr/bin/true", b"/usr/bin/true", Path("/usr/bin/true"), []):
            with self.subTest(cmd=cmd), self.assertRaisesRegex(TypeError, "nonempty sequence"):
                run_process(cmd)

    def test_a_python_without_spawn_support_does_not_fork(self) -> None:
        with mock.patch.object(subprocess, "_USE_POSIX_SPAWN", False), \
             mock.patch(FORK_TARGET, side_effect=AssertionError("forked")) as fork:
            with self.assertRaisesRegex(OSError, "without fork"):
                run_process(["/usr/bin/true"])
        fork.assert_not_called()

    def test_closed_standard_descriptors_fail_without_forking(self) -> None:
        # Isolate closing descriptors from pytest and its own capture pipes.
        for fd in (0, 1, 2):
            code = f"""
import os, subprocess
from unittest.mock import patch
from timecapsulesmb.core.process import run_process
os.close({fd})
target = 'subprocess._fork_exec' if hasattr(subprocess, '_fork_exec') else '_posixsubprocess.fork_exec'
with patch(target, side_effect=AssertionError('forked')) as fork:
    try:
        run_process(['/usr/bin/true'], stdin=subprocess.DEVNULL, capture_output=True)
    except OSError as exc:
        assert 'standard descriptor {fd} closed' in str(exc), str(exc)
    else:
        raise AssertionError('accepted a closed descriptor')
    fork.assert_not_called()
"""
            with self.subTest(fd=fd):
                result = run_process([sys.executable, "-c", code], capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)

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

    def test_conflicting_arguments_fail_before_starting_a_child(self) -> None:
        for kwargs in (
            {"input": b"data", "stdin": subprocess.DEVNULL},
            {"capture_output": True, "stdout": subprocess.PIPE},
            {"capture_output": True, "stderr": subprocess.STDOUT},
        ):
            with self.subTest(kwargs=kwargs), mock.patch.object(process, "popen_process") as spawn:
                with self.assertRaises(ValueError):
                    run_process(["/usr/bin/true"], **kwargs)
                spawn.assert_not_called()

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
        self.assertEqual(child.wait(timeout=5), -9)


if __name__ == "__main__":
    unittest.main()
