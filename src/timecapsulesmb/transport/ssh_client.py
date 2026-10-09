"""Validate the local OpenSSH required by the password helper."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from functools import lru_cache

from timecapsulesmb.core.process import run_process
from timecapsulesmb.transport.errors import SshClientConfigError, ssh_signal_error


def require_local_ssh() -> str:
    """Return the absolute executable whose version was checked."""
    path = shutil.which("ssh")
    if path is None:
        raise SshClientConfigError("Local tool ssh is missing; install OpenSSH 8.4 or newer on your computer.")
    path = os.path.abspath(path)
    _validate_ssh(path)
    return path


@lru_cache(maxsize=16)
def _validate_ssh(path: str) -> None:
    # Deploy runs hundreds of commands. Cache successful checks by executable,
    # but let failures be retried after bootstrap installs or repairs ssh.
    try:
        result = run_process([path, "-V"], capture_output=True, text=True, errors="replace", timeout=5)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SshClientConfigError(f"Could not check local SSH client {path}: {exc}") from exc
    if result.returncode < 0:
        raise ssh_signal_error(-result.returncode)
    version = re.search(r"OpenSSH_(\d+)\.(\d+)", (result.stderr or "") + (result.stdout or ""))
    if result.returncode or version is None:
        raise SshClientConfigError(f"Could not identify local SSH client {path}; OpenSSH 8.4 or newer is required.")
    if tuple(map(int, version.groups())) < (8, 4):
        raise SshClientConfigError(
            f"Local SSH client {path} is {version.group(0)}; OpenSSH 8.4 or newer is required. "
            "Upgrade OpenSSH on your computer and retry."
        )
