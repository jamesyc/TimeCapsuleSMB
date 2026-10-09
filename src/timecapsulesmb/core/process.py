"""Start local programs with posix_spawn, never fork.

A forked child on macOS runs Apple's fork handlers before exec, and
Network.framework's handler crashes once a Network Extension (a VPN, filter or
proxy) has set up state in this process; any name lookup installs that handler
(issue #371). CPython spawns with posix_spawn only for an absolute program path
with close_fds off, and forks without saying so for the options in
FORKING_OPTIONS, which are refused here.

With close_fds off the child keeps every descriptor not marked close-on-exec.
macOS has no pipe2, so os.pipe() marks its pipes in a second step: a child
spawned by another thread in between holds the pipe, and its reader waits for
end-of-file until that child exits (an ssh master: three minutes). SPAWN_LOCK
covers creating a child's pipes and spawning it, so no spawn lands in that gap.
"""
from __future__ import annotations

import errno
import os
import shutil
import subprocess
import threading
from collections.abc import Mapping, Sequence


SPAWN_LOCK = threading.Lock()
FORKING_OPTIONS = frozenset({"cwd", "start_new_session", "process_group", "preexec_fn", "pass_fds", "close_fds"})


def _spawn_argv(cmd: Sequence[str], env: Mapping[str, str] | None) -> list[str]:
    path = shutil.which(cmd[0], path=(os.environ if env is None else env).get("PATH"))
    if path is None:
        raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), cmd[0])
    return [os.path.abspath(path), *cmd[1:]]


def popen_process(cmd: Sequence[str], **kwargs) -> subprocess.Popen:
    forking = FORKING_OPTIONS & kwargs.keys()
    if forking:
        raise TypeError(f"{', '.join(sorted(forking))} would make subprocess fork")
    with SPAWN_LOCK:
        return subprocess.Popen(_spawn_argv(cmd, kwargs.get("env")), close_fds=False, **kwargs)


def run_process(
    cmd: Sequence[str],
    *,
    input: bytes | str | None = None,
    capture_output: bool = False,
    timeout: float | None = None,
    check: bool = False,
    **kwargs,
) -> subprocess.CompletedProcess:
    """subprocess.run, started through popen_process."""
    if capture_output:
        kwargs["stdout"] = kwargs["stderr"] = subprocess.PIPE
    if input is not None:
        kwargs["stdin"] = subprocess.PIPE
    with popen_process(cmd, **kwargs) as process:
        try:
            stdout, stderr = process.communicate(input, timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            raise
        except BaseException:
            process.kill()
            raise
    if check and process.returncode:
        raise subprocess.CalledProcessError(process.returncode, process.args, stdout, stderr)
    return subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)
