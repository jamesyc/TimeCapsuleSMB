from __future__ import annotations

from contextlib import contextmanager
from functools import lru_cache
import shlex
import shutil
import subprocess
import os
import re
import time
from pathlib import Path

from .local import tcp_open


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


@lru_cache(maxsize=None)
def _ssh_option_supported(option_name: str) -> bool:
    try:
        proc = subprocess.run(
            ["ssh", "-F", "/dev/null", "-G", "localhost", "-o", f"{option_name}=+ssh-rsa"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    except OSError:
        return False
    stderr = proc.stderr or ""
    return proc.returncode == 0 and "Bad configuration option" not in stderr


def _normalize_ssh_tokens(ssh_opts: str) -> list[str]:
    tokens = shlex.split(ssh_opts)
    rewritten = tokens
    if not _ssh_option_supported("PubkeyAcceptedAlgorithms") and _ssh_option_supported("PubkeyAcceptedKeyTypes"):
        rewritten = []
        i = 0
        while i < len(tokens):
            token = tokens[i]
            if token == "-o" and i + 1 < len(tokens):
                value = tokens[i + 1]
                if value.startswith("PubkeyAcceptedAlgorithms="):
                    value = value.replace("PubkeyAcceptedAlgorithms=", "PubkeyAcceptedKeyTypes=", 1)
                rewritten.extend([token, value])
                i += 2
                continue
            if token.startswith("-oPubkeyAcceptedAlgorithms="):
                rewritten.append(token.replace("-oPubkeyAcceptedAlgorithms=", "-oPubkeyAcceptedKeyTypes=", 1))
            else:
                rewritten.append(token)
            i += 1

    expanded: list[str] = []
    i = 0
    while i < len(rewritten):
        token = rewritten[i]
        if token == "-i" and i + 1 < len(rewritten):
            expanded.extend([token, os.path.expanduser(rewritten[i + 1])])
            i += 2
            continue
        if token.startswith("-oIdentityFile="):
            expanded.append("-oIdentityFile=" + os.path.expanduser(token.split("=", 1)[1]))
            i += 1
            continue
        if token == "-o" and i + 1 < len(rewritten) and rewritten[i + 1].startswith("IdentityFile="):
            expanded.extend([token, "IdentityFile=" + os.path.expanduser(rewritten[i + 1].split("=", 1)[1])])
            i += 2
            continue
        expanded.append(token)
        i += 1
    return expanded


def run_ssh(host: str, password: str, ssh_opts: str, remote_cmd: str, *, check: bool = True, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    cmd = ["ssh", *_normalize_ssh_tokens(ssh_opts), host, remote_cmd]
    rc, stdout = _spawn_with_password(
        cmd,
        password,
        timeout=timeout,
        timeout_message="Timed out waiting for ssh command to finish.",
    )
    if check and rc != 0:
        raise SystemExit(stdout.strip() or f"ssh command failed with rc={rc}")
    return subprocess.CompletedProcess(cmd, rc, stdout=stdout, stderr="")


@contextmanager
def ssh_local_forward(
    host: str,
    password: str,
    ssh_opts: str,
    *,
    local_port: int,
    remote_host: str,
    remote_port: int,
    ready_timeout: int = 20,
):
    try:
        import pexpect
    except Exception as e:
        raise SystemExit(f"pexpect is required for SSH transport: {e}")

    cmd = [
        "ssh",
        "-N",
        "-o",
        "ExitOnForwardFailure=yes",
        "-L",
        f"{local_port}:{remote_host}:{remote_port}",
        *_normalize_ssh_tokens(ssh_opts),
        host,
    ]
    child = pexpect.spawn(cmd[0], cmd[1:], encoding="utf-8", timeout=ready_timeout)
    output: list[str] = []
    start_time = time.time()
    try:
        password_sent = False
        while True:
            idx = child.expect(["[Pp]assword:", pexpect.EOF, pexpect.TIMEOUT], timeout=1)
            if idx == 0:
                child.sendline(password)
                password_sent = True
            elif idx == 1:
                output.append(child.before or "")
                raise SystemExit(output[-1].strip() or "ssh tunnel exited before becoming ready")
            else:
                output.append(child.before or "")
                if tcp_open("127.0.0.1", local_port, timeout=0.2):
                    break
                if child.isalive() and not password_sent:
                    continue
                if time.time() - start_time < ready_timeout:
                    continue
                raise SystemExit("Timed out waiting for ssh tunnel to become ready.")
        yield
    finally:
        try:
            child.close(force=True)
        except Exception:
            pass


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
        cmd = ["scp", "-O", *_normalize_ssh_tokens(ssh_opts), str(src), f"{host}:{dest}"]
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
    cmd = ["sshpass", "-e", "ssh", *_normalize_ssh_tokens(ssh_opts), host, remote_cmd]
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
