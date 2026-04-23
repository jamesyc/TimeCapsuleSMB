from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
import shlex
import shutil
import subprocess
import os
import re
import time
from pathlib import Path

from .local import tcp_open


class SshTransportError(SystemExit):
    """Raised for SSH transport failures with a CLI-ready message.

    This intentionally remains a SystemExit subclass during the migration so
    existing command boundaries keep their current user-facing behavior.
    """


@dataclass
class SshConnection:
    host: str
    password: str
    ssh_opts: str
    remote_has_scp: bool | None = None


SSH_TRANSPORT_ERROR_PATTERNS = (
    "bind [",
    "channel_setup_fwd_listener_tcpip:",
    "bad configuration option",
    "could not resolve hostname",
    "connection refused",
    "connection timed out",
    "no route to host",
    "connection closed by remote host",
    "kex_exchange_identification:",
    "ssh: ",
)

SSH_CLIENT_NOISE_PATTERNS = (
    re.compile(r"^Warning: Permanently added .+ to the list of known hosts\.$"),
    re.compile(r"^\*\* WARNING: connection is not using a post-quantum key exchange algorithm\.$"),
    re.compile(r"^\*\* This session may be vulnerable to \"store now, decrypt later\" attacks\.$"),
    re.compile(r"^\*\* The server may need to be upgraded\. See https://openssh\.com/pq\.html$"),
)

SSH_AUTHENTICITY_PROMPT = r"Are you sure you want to continue connecting \(yes/no/\[fingerprint\]\)\?"


def ssh_opts_use_proxy(ssh_opts: str) -> bool:
    try:
        tokens = shlex.split(ssh_opts)
    except ValueError:
        tokens = ssh_opts.split()

    for token in tokens:
        if token == "-J":
            return True
        if token.startswith("-J"):
            return True
        if token in {"ProxyCommand", "ProxyJump"}:
            return True
        if token.startswith("ProxyCommand=") or token.startswith("ProxyJump="):
            return True
        if token.startswith("-oProxyCommand=") or token.startswith("-oProxyJump="):
            return True

    return False


def _looks_like_transient_ssh_auth_failure(output: str) -> bool:
    lowered = output.lower()
    return "permission denied" in lowered or "please try again" in lowered


def _extract_ssh_transport_error(output: str) -> str | None:
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        lowered = line.lower()
        if any(pattern in lowered for pattern in SSH_TRANSPORT_ERROR_PATTERNS):
            return line
    return None


def _strip_ssh_client_noise(output: str) -> str:
    kept: list[str] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if any(pattern.match(line) for pattern in SSH_CLIENT_NOISE_PATTERNS):
            continue
        kept.append(raw_line)
    if not kept:
        return ""
    suffix = "\n" if output.endswith(("\n", "\r\n")) else ""
    return "\n".join(kept) + suffix


def _spawn_with_password(cmd: list[str], password: str, *, timeout: int, timeout_message: str) -> tuple[int, str]:
    try:
        import pexpect
    except Exception as e:
        raise SystemExit(f"pexpect is required for SSH transport: {e}")

    child = pexpect.spawn(cmd[0], cmd[1:], encoding="utf-8", codec_errors="replace", timeout=timeout)
    output: list[str] = []
    try:
        while True:
            idx = child.expect([SSH_AUTHENTICITY_PROMPT, "[Pp]assword:", pexpect.EOF, pexpect.TIMEOUT], timeout=timeout)
            if idx == 0:
                child.sendline("yes")
            elif idx == 1:
                child.sendline(password)
            elif idx == 2:
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
    rc = 1
    stdout = ""
    for attempt in range(3):
        rc, stdout = _spawn_with_password(
            cmd,
            password,
            timeout=timeout,
            timeout_message="Timed out waiting for ssh command to finish.",
        )
        if rc == 0 or not _looks_like_transient_ssh_auth_failure(stdout) or attempt == 2:
            break
        time.sleep(1)
    transport_error = _extract_ssh_transport_error(stdout)
    if transport_error:
        raise SshTransportError(f"Connecting to the device failed, SSH error: {transport_error}")
    stdout = _strip_ssh_client_noise(stdout)
    if check and rc != 0:
        raise SystemExit(stdout.strip() or f"ssh command failed with rc={rc}")
    return subprocess.CompletedProcess(cmd, rc, stdout=stdout, stderr="")


def run_ssh_conn(connection: SshConnection, remote_cmd: str, *, check: bool = True, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return run_ssh(connection.host, connection.password, connection.ssh_opts, remote_cmd, check=check, timeout=timeout)


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
    child = pexpect.spawn(cmd[0], cmd[1:], encoding="utf-8", codec_errors="replace", timeout=ready_timeout)
    output: list[str] = []
    start_time = time.time()
    try:
        password_sent = False
        while True:
            idx = child.expect([SSH_AUTHENTICITY_PROMPT, "[Pp]assword:", pexpect.EOF, pexpect.TIMEOUT], timeout=1)
            if idx == 0:
                child.sendline("yes")
            elif idx == 1:
                child.sendline(password)
                password_sent = True
            elif idx == 2:
                output.append(child.before or "")
                text = "".join(output)
                transport_error = _extract_ssh_transport_error(text)
                if transport_error:
                    raise SshTransportError(f"Connecting to the device failed, SSH error: {transport_error}")
                raise SystemExit(text.strip() or "ssh tunnel exited before becoming ready")
            else:
                output.append(child.before or "")
                if tcp_open("127.0.0.1", local_port, timeout=0.2):
                    break
                if child.isalive() and not password_sent:
                    continue
                if time.time() - start_time < ready_timeout:
                    continue
                transport_error = _extract_ssh_transport_error("".join(output))
                if transport_error:
                    raise SshTransportError(f"Connecting to the device failed, SSH error: {transport_error}")
                raise SystemExit("Timed out waiting for ssh tunnel to become ready.")
        yield
    finally:
        try:
            child.close(force=True)
        except Exception:
            pass


def ssh_local_forward_conn(
    connection: SshConnection,
    *,
    local_port: int,
    remote_host: str,
    remote_port: int,
    ready_timeout: int = 20,
):
    return ssh_local_forward(
        connection.host,
        connection.password,
        connection.ssh_opts,
        local_port=local_port,
        remote_host=remote_host,
        remote_port=remote_port,
        ready_timeout=ready_timeout,
    )


def probe_remote_scp_available(connection: SshConnection) -> bool:
    probe = run_ssh_conn(
        connection,
        "/bin/sh -c 'command -v scp >/dev/null 2>&1'",
        check=False,
        timeout=30,
    )
    return probe.returncode == 0


def ensure_remote_scp_capability(connection: SshConnection) -> bool:
    if connection.remote_has_scp is None:
        connection.remote_has_scp = probe_remote_scp_available(connection)
    return connection.remote_has_scp


def _verify_remote_size_conn(connection: SshConnection, src: Path, dest: str, *, timeout: int) -> None:
    expected_size = src.stat().st_size
    quoted_dest = shlex.quote(dest)
    remote_script = (
        f"[ -f {quoted_dest} ] || exit 1; "
        f"if command -v wc >/dev/null 2>&1; then "
        f"wc -c < {quoted_dest}; "
        f"else set -- $(ls -l {quoted_dest}); echo \"$5\"; fi"
    )
    remote_cmd = f"/bin/sh -c {shlex.quote(remote_script)}"
    proc = None
    actual_size = None
    for attempt in range(3):
        proc = run_ssh_conn(connection, remote_cmd, check=False, timeout=timeout)
        matches = re.findall(r"^\s*([0-9]+)\s*$", proc.stdout, flags=re.MULTILINE)
        actual_size = int(matches[-1]) if matches else None
        if proc.returncode == 0 and actual_size == expected_size:
            return
        if attempt < 2:
            time.sleep(1)
    raise SystemExit(
        f"upload verification failed for {dest}: expected {expected_size} bytes, "
        f"got {actual_size if actual_size is not None else 'unknown'} bytes"
    )


def _verify_remote_size(host: str, password: str, ssh_opts: str, src: Path, dest: str, *, timeout: int) -> None:
    _verify_remote_size_conn(SshConnection(host=host, password=password, ssh_opts=ssh_opts), src, dest, timeout=timeout)


def run_scp(host: str, password: str, ssh_opts: str, src: Path, dest: str, *, timeout: int = 120) -> None:
    connection = SshConnection(host=host, password=password, ssh_opts=ssh_opts)
    connection.remote_has_scp = probe_remote_scp_available(connection)
    run_scp_conn(connection, src, dest, timeout=timeout)


def run_scp_conn(connection: SshConnection, src: Path, dest: str, *, timeout: int = 120) -> None:
    if ensure_remote_scp_capability(connection):
        cmd = ["scp", "-O", *_normalize_ssh_tokens(connection.ssh_opts), str(src), f"{connection.host}:{dest}"]
        rc = 1
        stdout = ""
        for attempt in range(3):
            rc, stdout = _spawn_with_password(
                cmd,
                connection.password,
                timeout=timeout,
                timeout_message=f"Timed out copying {src} to {dest}",
            )
            if rc == 0 or not _looks_like_transient_ssh_auth_failure(stdout) or attempt == 2:
                break
            time.sleep(1)
        if rc != 0:
            transport_error = _extract_ssh_transport_error(stdout)
            if transport_error:
                raise SshTransportError(f"Connecting to the device failed, SSH error: {transport_error}")
            raise SystemExit(stdout.strip() or f"scp failed with rc={rc}")
        _verify_remote_size_conn(connection, src, dest, timeout=30)
        return

    if shutil.which("sshpass") is None:
        raise SystemExit("Remote scp is unavailable and local sshpass is required for streaming upload fallback.")

    remote_cmd = f"/bin/sh -c {shlex.quote('cat > ' + shlex.quote(dest))}"
    cmd = ["sshpass", "-e", "ssh", *_normalize_ssh_tokens(connection.ssh_opts), connection.host, remote_cmd]
    env = dict(os.environ)
    env["SSHPASS"] = connection.password
    proc = None
    for attempt in range(3):
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
        stdout = proc.stdout.decode("utf-8", errors="replace").strip()
        if proc.returncode == 0 or not _looks_like_transient_ssh_auth_failure(stdout) or attempt == 2:
            break
        time.sleep(1)
    if proc is None:
        raise SystemExit(f"ssh cat upload failed for {dest}")
    if proc.returncode != 0:
        stdout = proc.stdout.decode("utf-8", errors="replace").strip()
        transport_error = _extract_ssh_transport_error(stdout)
        if transport_error:
            raise SshTransportError(f"Connecting to the device failed, SSH error: {transport_error}")
        raise SystemExit(stdout or f"ssh cat upload failed with rc={proc.returncode}")
    _verify_remote_size_conn(connection, src, dest, timeout=30)
