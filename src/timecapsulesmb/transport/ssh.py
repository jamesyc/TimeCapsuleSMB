from __future__ import annotations

import shlex
import shutil
import subprocess
import os
import re
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


def _verify_remote_size(host: str, password: str, ssh_opts: str, src: Path, dest: str, *, timeout: int) -> None:
    expected_size = src.stat().st_size
    quoted_dest = shlex.quote(dest)
    remote_script = (
        f"[ -f {quoted_dest} ] || exit 1; "
        f"if command -v wc >/dev/null 2>&1; then "
        f"wc -c < {quoted_dest}; "
        f"else set -- $(ls -l {quoted_dest}); echo \"$5\"; fi"
    )
    remote_cmd = f"/bin/sh -c {shlex.quote(remote_script)}"
    proc = run_ssh(host, password, ssh_opts, remote_cmd, check=False, timeout=timeout)
    matches = re.findall(r"^\s*([0-9]+)\s*$", proc.stdout, flags=re.MULTILINE)
    actual_size = int(matches[-1]) if matches else None
    if proc.returncode != 0 or actual_size != expected_size:
        raise SystemExit(
            f"upload verification failed for {dest}: expected {expected_size} bytes, "
            f"got {actual_size if actual_size is not None else 'unknown'} bytes"
        )


def run_scp(host: str, password: str, ssh_opts: str, src: Path, dest: str, *, timeout: int = 120) -> None:
    probe = run_ssh(
        host,
        password,
        ssh_opts,
        "/bin/sh -c 'command -v scp >/dev/null 2>&1'",
        check=False,
        timeout=30,
    )
    if probe.returncode == 0:
        cmd = ["scp", "-O", *shlex.split(ssh_opts), str(src), f"{host}:{dest}"]
        rc, stdout = _spawn_with_password(
            cmd,
            password,
            timeout=timeout,
            timeout_message=f"Timed out copying {src} to {dest}",
        )
        if rc != 0:
            raise SystemExit(stdout.strip() or f"scp failed with rc={rc}")
        _verify_remote_size(host, password, ssh_opts, src, dest, timeout=30)
        return

    if shutil.which("sshpass") is None:
        raise SystemExit("Remote scp is unavailable and local sshpass is required for streaming upload fallback.")

    remote_cmd = f"/bin/sh -c {shlex.quote('cat > ' + shlex.quote(dest))}"
    cmd = ["sshpass", "-e", "ssh", *shlex.split(ssh_opts), host, remote_cmd]
    env = dict(os.environ)
    env["SSHPASS"] = password
    try:
        proc = subprocess.run(
            cmd,
            input=src.read_bytes(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise SystemExit(f"Timed out copying {src} to {dest}") from e
    if proc.returncode != 0:
        stdout = proc.stdout.decode("utf-8", errors="replace").strip()
        raise SystemExit(stdout or f"ssh cat upload failed with rc={proc.returncode}")
    _verify_remote_size(host, password, ssh_opts, src, dest, timeout=30)
