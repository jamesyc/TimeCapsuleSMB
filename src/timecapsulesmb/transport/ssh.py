from __future__ import annotations

import shlex
import subprocess
from pathlib import Path


def _spawn_with_password(cmd: list[str], password: str, *, timeout: int, timeout_message: str) -> tuple[int, str]:
    try:
        import pexpect
    except Exception as e:
        raise SystemExit(f"pexpect is required for SSH transport: {e}")

    child = pexpect.spawn(cmd[0], cmd[1:], encoding="utf-8", timeout=timeout)
    output: list[str] = []
    try:
        while True:
            idx = child.expect(["[Pp]assword:", pexpect.EOF, pexpect.TIMEOUT], timeout=timeout)
            if idx == 0:
                child.sendline(password)
            elif idx == 1:
                output.append(child.before or "")
                break
            else:
                output.append(child.before or "")
                raise SystemExit(timeout_message)
    finally:
        try:
            child.close()
        except Exception:
            pass

    rc = child.exitstatus if child.exitstatus is not None else (child.signalstatus or 1)
    return rc, "".join(output)


def run_ssh(host: str, password: str, ssh_opts: str, remote_cmd: str, *, check: bool = True, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    cmd = ["ssh", *shlex.split(ssh_opts), host, remote_cmd]
    rc, stdout = _spawn_with_password(
        cmd,
        password,
        timeout=timeout,
        timeout_message="Timed out waiting for ssh command to finish.",
    )
    if check and rc != 0:
        raise SystemExit(stdout.strip() or f"ssh command failed with rc={rc}")
    return subprocess.CompletedProcess(cmd, rc, stdout=stdout, stderr="")


def run_scp(host: str, password: str, ssh_opts: str, src: Path, dest: str, *, timeout: int = 120) -> None:
    cmd = ["scp", "-O", *shlex.split(ssh_opts), str(src), f"{host}:{dest}"]
    rc, stdout = _spawn_with_password(
        cmd,
        password,
        timeout=timeout,
        timeout_message=f"Timed out copying {src} to {dest}",
    )
    if rc != 0:
        raise SystemExit(stdout.strip() or f"scp failed with rc={rc}")
