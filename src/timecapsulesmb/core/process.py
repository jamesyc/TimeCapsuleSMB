"""Start local programs with posix_spawn, never fork.

A forked child on macOS runs Apple's fork handlers before exec, and
Network.framework's handler crashes once a Network Extension (a VPN, filter or
proxy) has set up state in this process; any name lookup installs that handler
(issue #371). CPython spawns with posix_spawn only for an absolute program path
with close_fds off and compatible redirections. Only the supported options
below are accepted; new subprocess options must be checked before adding them.

With close_fds off the child keeps every descriptor not marked close-on-exec.
macOS has no pipe2, so os.pipe() marks its pipes in a second step: a child
spawned by another thread in between holds the pipe, and its reader waits for
end-of-file until that child exits (for example, caffeinate or dns-sd).
SPAWN_LOCK covers creating a child's pipes and spawning it, so no spawn lands
in that gap. Socket creators need the same lock to prevent socket inheritance;
this helper cannot protect sockets created elsewhere. OpenSSH closes inherited
descriptors above 2 at startup.
"""
from __future__ import annotations

import errno
import os
import shutil
import subprocess
import threading
from collections.abc import Mapping, Sequence


SPAWN_LOCK = threading.Lock()
SPAWN_OPTIONS = frozenset({
    "stdin", "stdout", "stderr", "env", "bufsize", "text", "universal_newlines",
    "encoding", "errors", "restore_signals",
})


def _spawn_argv(cmd: Sequence[str], env: Mapping[str, str] | None) -> list[str]:
    if isinstance(cmd, (str, bytes, os.PathLike)) or not cmd:
        raise TypeError("cmd must be a nonempty sequence of arguments")
    path = shutil.which(cmd[0], path=(os.environ if env is None else env).get("PATH"))
    if path is None:
        raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), cmd[0])
    return [os.path.abspath(path), *cmd[1:]]


def popen_process(cmd: Sequence[str], **kwargs) -> subprocess.Popen:
    unsupported = kwargs.keys() - SPAWN_OPTIONS
    if unsupported:
        raise TypeError(f"unsupported spawn options: {', '.join(sorted(unsupported))}")
    if not subprocess._USE_POSIX_SPAWN:
        raise OSError("This Python cannot start local programs without fork; use a supported macOS or Linux Python")
    argv = _spawn_argv(cmd, kwargs.get("env"))
    with SPAWN_LOCK:
        # Closed standard descriptors can be reused for a pipe or /dev/null,
        # silently sending Popen down its fork path. Do not modify the parent.
        for fd in (0, 1, 2):
            try:
                os.fstat(fd)
            except OSError as exc:
                raise OSError(f"Cannot spawn with standard descriptor {fd} closed") from exc
        for name in ("stdin", "stdout", "stderr"):
            value = kwargs.get(name)
            if name == "stderr" and value == subprocess.STDOUT:
                value = kwargs.get("stdout")
                if value is None:
                    raise TypeError("stderr=STDOUT requires redirected stdout to avoid fork")
            if value is None or value in (subprocess.PIPE, subprocess.DEVNULL):
                continue
            fd = value if isinstance(value, int) else value.fileno()
            if fd <= 2:
                raise TypeError(f"{name} must use a descriptor above 2 to avoid fork")
            # Resolve file objects once so Popen uses the descriptor we checked.
            if kwargs.get(name) != subprocess.STDOUT:
                kwargs[name] = fd
        return subprocess.Popen(argv, close_fds=False, **kwargs)


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
        if kwargs.get("stdout") is not None or kwargs.get("stderr") is not None:
            raise ValueError("stdout and stderr arguments may not be used with capture_output")
        kwargs["stdout"] = kwargs["stderr"] = subprocess.PIPE
    if input is not None:
        if kwargs.get("stdin") is not None:
            raise ValueError("stdin and input arguments may not both be used")
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
