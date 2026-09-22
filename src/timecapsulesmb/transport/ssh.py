from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
import shlex
import subprocess
import os
import re
import tempfile
import time
from pathlib import Path

from timecapsulesmb.core.errors import missing_dependency_message
from timecapsulesmb.transport.errors import (
    SshAlgorithmNegotiationError,
    SshAuthenticationError,
    SshClientConfigError,
    SshCommandTimeout,
    SshError,
    SshNetworkError,
)

from .local import find_command, tcp_open


@dataclass
class SshConnection:
    host: str
    password: str
    ssh_opts: str


@dataclass(frozen=True)
class SshClientDiagnostics:
    text: str
    authenticated: bool
    error: SshError | None


SSH_TRANSPORT_ERROR_PATTERNS = (
    "bind [",
    "channel_setup_fwd_listener_tcpip:",
    "could not resolve hostname",
    "connection refused",
    "connection timed out",
    "no route to host",
    "connection closed by remote host",
    "kex_exchange_identification:",
    "ssh: ",
)
LEGACY_AIRPORT_MACS = (
    "hmac-sha1",
    "hmac-md5-96",
    "hmac-md5",
    "hmac-sha1-96",
    "hmac-ripemd160",
)

SSH_AUTHENTICITY_PROMPT = r"Are you sure you want to continue connecting \(yes/no/\[fingerprint\]\)\?"
SSH_AUTH_FAILURE_PATTERNS = (
    re.compile(r"^Permission denied, please try again\.$", re.IGNORECASE),
    re.compile(r"^(?:.+:\s*)?Permission denied \([^\r\n()]+\)\.$", re.IGNORECASE),
)
SSH_STARTUP_CONFIG_ERROR_PATTERNS = (
    "bad configuration option",
    "couldn't open logfile",
    "illegal option -- e",
    "unknown option -- e",
)
SSH_CLIENT_LOG_PREFIX = "timecapsulesmb-ssh-"
REMOTE_COMMAND_SUMMARY_LIMIT = 500
SSH_ERROR_STDERR_LIMIT_BYTES = 65536
SSH_ERROR_STDOUT_PREFIX_BYTES = 8192


def _summarize_remote_command(remote_cmd: str) -> str:
    summary = " ".join(remote_cmd.split())
    if len(summary) <= REMOTE_COMMAND_SUMMARY_LIMIT:
        return summary
    return summary[: REMOTE_COMMAND_SUMMARY_LIMIT - 3] + "..."


def ssh_opts_use_proxy(ssh_opts: str) -> bool:
    try:
        tokens = shlex.split(ssh_opts)
    except ValueError:
        tokens = ssh_opts.split()

    for token in tokens:
        lowered = token.lower()
        if token == "-J":
            return True
        if token.startswith("-J"):
            return True
        if lowered in {"proxycommand", "proxyjump"}:
            return True
        if lowered.startswith("proxycommand=") or lowered.startswith("proxyjump="):
            return True
        if lowered.startswith("-oproxycommand=") or lowered.startswith("-oproxyjump="):
            return True

    return False


def _decode_remote_error_output(stderr: bytes, stdout: bytes = b"", *, include_stdout: bool = True) -> str:
    stderr_text = stderr[:SSH_ERROR_STDERR_LIMIT_BYTES].decode("utf-8", errors="replace")
    stdout_text = stdout[:SSH_ERROR_STDOUT_PREFIX_BYTES].decode("utf-8", errors="replace") if include_stdout else ""
    return stderr_text + stdout_text


def _parse_no_matching_algorithm(line: str) -> SshAlgorithmNegotiationError | None:
    match = re.search(
        r"no matching (?P<algorithm>MAC|key exchange method|host key type) found\. "
        r"Their offer: (?P<offered>.+)$",
        line,
        re.IGNORECASE,
    )
    if match is None:
        return None
    raw_algorithm = match.group("algorithm").lower()
    algorithm = {
        "mac": "mac",
        "key exchange method": "kex",
        "host key type": "host_key",
    }[raw_algorithm]
    offered = tuple(item.strip() for item in match.group("offered").split(",") if item.strip())
    return SshAlgorithmNegotiationError(line, algorithm=algorithm, offered=offered)


def _classify_ssh_client_error_line(line: str) -> SshError | None:
    algorithm_error = _parse_no_matching_algorithm(line)
    if algorithm_error is not None:
        return algorithm_error

    lowered = line.lower()
    if "bad configuration option" in lowered:
        return SshClientConfigError(f"Connecting to the device failed, SSH error: {line}")
    if any(pattern in lowered for pattern in SSH_TRANSPORT_ERROR_PATTERNS):
        return SshNetworkError(f"Connecting to the device failed, SSH error: {line}")
    if any(pattern.fullmatch(line) for pattern in SSH_AUTH_FAILURE_PATTERNS):
        return SshAuthenticationError(line)
    return None


def _is_authenticated_log_line(line: str) -> bool:
    # Apple's bundled SSH emits this text on macOS 13+, covering our macOS 14+ baseline.
    return line.startswith("Authenticated to ") and ' using "' in line and line.endswith('".')


def parse_ssh_client_diagnostics(output: str) -> SshClientDiagnostics:
    authenticated = False
    auth_error: SshAuthenticationError | None = None
    other_error: SshError | None = None
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("debug1:", "debug2:", "debug3:")):
            continue
        if _is_authenticated_log_line(line):
            authenticated = True
        error = _classify_ssh_client_error_line(line)
        if isinstance(error, SshAuthenticationError):
            auth_error = error
        elif error is not None and other_error is None:
            other_error = error
    return SshClientDiagnostics(
        text=output,
        authenticated=authenticated,
        error=other_error or (None if authenticated else auth_error),
    )


def classify_ssh_client_error(output: str) -> SshError | None:
    """Classify text read from an OpenSSH -E client log, never remote output."""
    return parse_ssh_client_diagnostics(output).error


def _classify_ssh_startup_error(output: str) -> SshClientConfigError | None:
    for raw_line in output.splitlines():
        line = raw_line.strip()
        lowered = line.lower()
        if line and any(pattern in lowered for pattern in SSH_STARTUP_CONFIG_ERROR_PATTERNS):
            return SshClientConfigError(f"Connecting to the device failed, SSH error: {line}")
    return None


def _client_failure_detail(diagnostics: SshClientDiagnostics) -> str:
    ignored_prefixes = (
        "Authenticated to ",
        "Transferred: ",
        "Bytes per second: ",
        "Warning: Permanently added ",
    )
    lines = [
        line.strip()
        for line in diagnostics.text.splitlines()
        if line.strip()
        and not line.strip().startswith(("debug1:", "debug2:", "debug3:", *ignored_prefixes))
    ]
    return lines[-1] if lines else ""


@contextmanager
def _ssh_client_log_path():
    with tempfile.TemporaryDirectory(prefix=SSH_CLIENT_LOG_PREFIX, dir="/tmp") as directory:
        yield Path(directory) / "client.log"


def _read_ssh_client_diagnostics(path: Path) -> SshClientDiagnostics:
    try:
        text = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    except OSError as exc:
        raise SshClientConfigError(f"Could not read SSH client diagnostics: {exc}") from exc
    return parse_ssh_client_diagnostics(text)


def _should_retry_password_auth(
    connection: SshConnection,
    diagnostics: SshClientDiagnostics,
    attempt: int,
) -> bool:
    return (
        bool(connection.password)
        and attempt < 2
        and not diagnostics.authenticated
        and isinstance(diagnostics.error, SshAuthenticationError)
    )


def _ssh_client_error_for_result(
    diagnostics: SshClientDiagnostics,
    *,
    returncode: int,
    startup_output: str,
) -> SshError | None:
    if diagnostics.error is not None:
        return diagnostics.error
    if not diagnostics.text:
        startup_error = _classify_ssh_startup_error(startup_output)
        if startup_error is not None:
            return startup_error
    if returncode == 255 and not diagnostics.authenticated:
        detail = _client_failure_detail(diagnostics)
        return SshError(detail or startup_output.strip() or "ssh client failed with rc=255")
    return None


def _spawn_with_password(
    cmd: list[str],
    password: str,
    *,
    client_log: Path,
    timeout: int,
    timeout_message: str,
) -> tuple[int, str]:
    try:
        import pexpect
    except Exception as e:
        raise SshError(missing_dependency_message("pexpect", e)) from e

    try:
        child = pexpect.spawn(cmd[0], cmd[1:], encoding="utf-8", codec_errors="replace", timeout=timeout)
    except OSError as exc:
        raise SshClientConfigError(f"Could not start local SSH client: {exc}") from exc
    output: list[str] = []
    password_sent = False
    force_close = False
    try:
        while True:
            idx = child.expect([SSH_AUTHENTICITY_PROMPT, "[Pp]assword:", pexpect.EOF, pexpect.TIMEOUT], timeout=timeout)
            diagnostics = _read_ssh_client_diagnostics(client_log)
            if idx in {0, 1} and diagnostics.authenticated:
                output.extend((child.before or "", child.after or ""))
            elif idx == 0:
                child.sendline("yes")
            elif idx == 1:
                if password_sent:
                    force_close = True
                    break
                child.sendline(password)
                password_sent = True
            elif idx == 2:
                output.append(child.before or "")
                break
            else:
                output.append(child.before or "")
                raise SshCommandTimeout(timeout_message)
    finally:
        try:
            child.close(force=force_close)
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


@lru_cache(maxsize=None)
def _local_ssh_macs() -> tuple[str, ...]:
    try:
        proc = subprocess.run(
            ["ssh", "-Q", "mac"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
    except OSError:
        return ()
    if proc.returncode != 0:
        return ()
    return tuple(line.strip() for line in proc.stdout.splitlines() if line.strip())


def _legacy_airport_macs_supported_locally() -> tuple[str, ...]:
    available = set(_local_ssh_macs())
    return tuple(mac for mac in LEGACY_AIRPORT_MACS if mac in available)


def _tokens_include_mac_option(tokens: list[str]) -> bool:
    i = 0
    while i < len(tokens):
        token = tokens[i]
        lowered = token.lower()
        if lowered == "-m" or (lowered.startswith("-m") and lowered != "-m"):
            return True
        if lowered == "-o" and i + 1 < len(tokens) and tokens[i + 1].lower().startswith("macs="):
            return True
        if lowered.startswith("-omacs="):
            return True
        i += 1
    return False


_TRANSPORT_OWNED_SSH_OPTIONS = {
    "controlpath",
    "exitonforwardfailure",
    "forwardagent",
    "forwardx11",
    "forwardx11trusted",
    "loglevel",
    "logverbose",
    "numberofpasswordprompts",
    "requesttty",
    "stdinnull",
}
_TRANSPORT_OWNED_SHORT_FLAGS = set("qvytTnAaXYx")
_SSH_NO_ARGUMENT_SHORT_FLAGS = set("46AaCfGgKkMNnqsTtVvXxYyZ")


def _ssh_option_name(option: str) -> str:
    return option.casefold().replace("=", " ", 1).split(None, 1)[0]


def _without_transport_owned_tokens(tokens: list[str]) -> list[str]:
    kept: list[str] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token in {"-E", "-F", "-S"}:
            i += 2
            continue
        if token.startswith(("-E", "-F", "-S")) and len(token) > 2:
            i += 1
            continue
        if token == "-o":
            option = tokens[i + 1] if i + 1 < len(tokens) else ""
            if _ssh_option_name(option) in _TRANSPORT_OWNED_SSH_OPTIONS:
                i += 2
                continue
            kept.extend((token, option))
            i += 2
            continue
        if token.startswith("-o") and _ssh_option_name(token[2:]) in _TRANSPORT_OWNED_SSH_OPTIONS:
            i += 1
            continue
        if token.startswith("-") and len(token) > 1 and set(token[1:]) <= _SSH_NO_ARGUMENT_SHORT_FLAGS:
            remaining = "".join(char for char in token[1:] if char not in _TRANSPORT_OWNED_SHORT_FLAGS)
            if remaining:
                kept.append("-" + remaining)
            i += 1
            continue
        kept.append(token)
        i += 1
    return kept


def _normalize_ssh_tokens(ssh_opts: str) -> list[str]:
    tokens = _without_transport_owned_tokens(shlex.split(ssh_opts))
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
    if not _tokens_include_mac_option(expanded):
        legacy_macs = _legacy_airport_macs_supported_locally()
        if legacy_macs:
            expanded.extend(["-o", f"MACs=+{','.join(legacy_macs)}"])
    return expanded


_KEY_SOURCE_OPTIONS = {
    "identityfile",
    "identityagent",
    "certificatefile",
    "pkcs11provider",
    "securitykeyprovider",
}
_PUBKEY_ENABLED_VALUES = {"yes", "unbound", "host-bound"}


def _ssh_option_assignments(tokens: list[str]) -> Iterator[tuple[str, str]]:
    tokens = iter(tokens)
    for token in tokens:
        if token == "-o":
            option = next(tokens, "")
        elif token.startswith("-o"):
            option = token[2:]
        else:
            continue

        parts = option.casefold().replace("=", " ", 1).split(None, 1)
        if len(parts) == 2:
            yield parts[0], parts[1]


def _tokens_request_public_key_auth(tokens: list[str]) -> bool:
    for index, token in enumerate(tokens):
        if token in {"-i", "-I"}:
            value = tokens[index + 1] if index + 1 < len(tokens) else ""
        elif token[:2] in {"-i", "-I"}:
            value = token[2:]
        else:
            continue

        if value and value.casefold() != "none":
            return True

    return any(
        (name in _KEY_SOURCE_OPTIONS and value != "none")
        or (name == "pubkeyauthentication" and value in _PUBKEY_ENABLED_VALUES)
        or (
            name == "preferredauthentications"
            and "publickey" in re.split(r"\s*,\s*", value)
        )
        or (name == "batchmode" and value == "yes")
        for name, value in _ssh_option_assignments(tokens)
    )


def _connection_ssh_args(
    connection: SshConnection,
    *,
    client_log: Path,
    stdin_null: bool,
    extra_args: tuple[str, ...] = (),
) -> list[str]:
    """Return config-isolated SSH args with transport-owned session behavior."""
    tokens = _normalize_ssh_tokens(connection.ssh_opts)

    if not connection.password:
        auth_args = ["-o", "BatchMode=yes"]
    elif _tokens_request_public_key_auth(tokens):
        auth_args = []
    else:
        # Avoid passphrase prompts from unintended default keys while preserving
        # explicit key configuration and keyboard-interactive password servers.
        auth_args = ["-o", "PubkeyAuthentication=no"]

    return [
        "-F", "/dev/null",
        "-o", "LogLevel=VERBOSE",
        "-o", "NumberOfPasswordPrompts=1",
        "-o", "ExitOnForwardFailure=yes",
        *auth_args,
        *extra_args,
        *tokens,
        "-E", str(client_log),
        "-S", "none",
        "-T",
        "-a",
        "-x",
        *(["-n"] if stdin_null else []),
    ]


def run_ssh(connection: SshConnection, remote_cmd: str, *, check: bool = True, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    timeout_message = (
        "Timed out waiting for ssh command to finish: "
        f"{_summarize_remote_command(remote_cmd)}"
    )
    rc = 1
    stdout = ""
    cmd: list[str] = []
    for attempt in range(3):
        with _ssh_client_log_path() as client_log:
            cmd = [
                "ssh",
                *_connection_ssh_args(connection, client_log=client_log, stdin_null=True),
                connection.host,
                remote_cmd,
            ]
            try:
                rc, stdout = _spawn_with_password(
                    cmd,
                    connection.password,
                    client_log=client_log,
                    timeout=timeout,
                    timeout_message=timeout_message,
                )
            except SshCommandTimeout:
                diagnostics = _read_ssh_client_diagnostics(client_log)
                if diagnostics.error is not None:
                    raise diagnostics.error
                raise
            diagnostics = _read_ssh_client_diagnostics(client_log)
        client_error = _ssh_client_error_for_result(
            diagnostics,
            returncode=rc,
            startup_output=stdout,
        )
        if client_error is not None:
            if _should_retry_password_auth(connection, diagnostics, attempt):
                time.sleep(1)
                continue
            raise client_error
        break
    if check and rc != 0:
        raise SshError(stdout.strip() or f"ssh command failed with rc={rc}")
    return subprocess.CompletedProcess(cmd, rc, stdout=stdout, stderr="")


def _run_piped_ssh(
    connection: SshConnection,
    remote_cmd: str,
    *,
    input_bytes: bytes | None = None,
    timeout: int | None,
    missing_tool_message: str,
    timeout_message: str,
    raw_remote_status: bool = False,
    extra_ssh_args: tuple[str, ...] = (),
) -> subprocess.CompletedProcess[bytes]:
    env = dict(os.environ)
    if connection.password:
        if find_command("sshpass") is None:
            raise SshError(missing_tool_message)
        env["SSHPASS"] = connection.password
        command_prefix = ["sshpass", "-e", "ssh"]
    else:
        command_prefix = ["ssh"]
    proc: subprocess.CompletedProcess[bytes] | None = None
    cmd: list[str] = []
    attempts = 1 if raw_remote_status else 3
    for attempt in range(attempts):
        with _ssh_client_log_path() as client_log:
            cmd = [
                *command_prefix,
                *_connection_ssh_args(
                    connection,
                    client_log=client_log,
                    stdin_null=input_bytes is None,
                    extra_args=extra_ssh_args,
                ),
                connection.host,
                remote_cmd,
            ]
            try:
                proc = subprocess.run(
                    cmd,
                    input=input_bytes,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=env,
                    timeout=timeout,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                diagnostics = _read_ssh_client_diagnostics(client_log)
                if diagnostics.error is not None:
                    raise diagnostics.error
                raise SshCommandTimeout(timeout_message) from exc
            except OSError as exc:
                raise SshClientConfigError(f"Could not start local SSH client: {exc}") from exc
            diagnostics = _read_ssh_client_diagnostics(client_log)
        startup_output = _decode_remote_error_output(proc.stderr, include_stdout=False)
        client_error = _ssh_client_error_for_result(
            diagnostics,
            returncode=proc.returncode,
            startup_output=startup_output,
        )
        if client_error is not None:
            if not raw_remote_status and _should_retry_password_auth(connection, diagnostics, attempt):
                time.sleep(1)
                continue
            raise client_error
        if (
            connection.password
            and proc.returncode in {5, 6, 7}
            and not diagnostics.authenticated
        ):
            raise SshError(startup_output.strip() or f"sshpass failed with rc={proc.returncode}")
        break
    if proc is None:
        raise SshError("piped ssh command did not run")
    return proc


def run_ssh_input(
    connection: SshConnection,
    remote_cmd: str,
    *,
    input_bytes: bytes = b"",
    timeout: int | None = 120,
    raw_remote_status: bool = False,
    extra_ssh_args: tuple[str, ...] = (),
) -> subprocess.CompletedProcess[bytes]:
    """Send a bounded request without PTY echo, keeping stdout and logs separate."""
    proc = _run_piped_ssh(
        connection, remote_cmd, input_bytes=input_bytes, timeout=timeout,
        missing_tool_message="Piped SSH requires local sshpass; run `./tcapsule bootstrap`.",
        timeout_message=f"Timed out waiting for ssh command to finish: {_summarize_remote_command(remote_cmd)}",
        raw_remote_status=raw_remote_status,
        extra_ssh_args=extra_ssh_args,
    )
    if proc.returncode and not raw_remote_status:
        raise SshError(_decode_remote_error_output(proc.stderr, proc.stdout).strip()
                       or f"ssh command failed with rc={proc.returncode}")
    return proc


def run_ssh_capture_bytes(
    connection: SshConnection,
    remote_cmd: str,
    *,
    timeout: int = 120,
    missing_tool_message: str | None = None,
) -> bytes:
    """Run a remote command over SSH and return raw stdout bytes.

    This intentionally uses a pipe instead of the pexpect PTY path because
    firmware bank reads are binary and a PTY can transform byte streams.
    """
    proc = _run_piped_ssh(
        connection,
        remote_cmd,
        timeout=timeout,
        missing_tool_message=missing_tool_message or (
            "Reading raw firmware banks requires local sshpass. "
            "Run `./tcapsule bootstrap` to install sshpass, then rerun `tcapsule flash`."
        ),
        timeout_message=(
            "Timed out waiting for ssh command to finish: "
            f"{_summarize_remote_command(remote_cmd)}"
        ),
    )
    if proc.returncode != 0:
        detail = _decode_remote_error_output(proc.stderr, include_stdout=False).strip() or f"ssh command failed with rc={proc.returncode}"
        raise SshError(detail)
    return proc.stdout


@contextmanager
def ssh_local_forward(
    connection: SshConnection,
    *,
    local_port: int,
    remote_host: str,
    remote_port: int,
    ready_timeout: int = 20,
):
    try:
        import pexpect
    except Exception as e:
        raise SshError(missing_dependency_message("pexpect", e)) from e

    with _ssh_client_log_path() as client_log:
        cmd = [
            "ssh",
            *_connection_ssh_args(connection, client_log=client_log, stdin_null=True),
            "-N",
            "-L",
            f"{local_port}:{remote_host}:{remote_port}",
            connection.host,
        ]
        try:
            child = pexpect.spawn(cmd[0], cmd[1:], encoding="utf-8", codec_errors="replace", timeout=ready_timeout)
        except OSError as exc:
            raise SshClientConfigError(f"Could not start local SSH client: {exc}") from exc
        output: list[str] = []
        start_time = time.time()
        password_sent = False
        try:
            while True:
                idx = child.expect([SSH_AUTHENTICITY_PROMPT, "[Pp]assword:", pexpect.EOF, pexpect.TIMEOUT], timeout=1)
                diagnostics = _read_ssh_client_diagnostics(client_log)
                if idx in {0, 1} and diagnostics.authenticated:
                    output.extend((child.before or "", child.after or ""))
                elif idx == 0:
                    child.sendline("yes")
                elif idx == 1:
                    if password_sent:
                        raise diagnostics.error or SshAuthenticationError("SSH requested the password more than once")
                    child.sendline(connection.password)
                    password_sent = True
                elif idx == 2:
                    output.append(child.before or "")
                    if diagnostics.error is not None:
                        raise diagnostics.error
                    startup_error = _classify_ssh_startup_error("".join(output)) if not diagnostics.text else None
                    if startup_error is not None:
                        raise startup_error
                    raise SshError(_client_failure_detail(diagnostics) or "".join(output).strip() or "ssh tunnel exited before becoming ready")
                else:
                    output.append(child.before or "")
                    if diagnostics.error is not None:
                        raise diagnostics.error
                    if tcp_open("127.0.0.1", local_port, timeout=0.2):
                        break
                    if child.isalive() and time.time() - start_time < ready_timeout:
                        continue
                    raise SshError(
                        "Timed out waiting for ssh tunnel to become ready: "
                        f"127.0.0.1:{local_port} -> {remote_host}:{remote_port} via {connection.host}"
                    )
            yield
        finally:
            try:
                child.close(force=True)
            except Exception:
                pass


def _verify_uploaded_size(connection: SshConnection, src: Path, dest: str, *, timeout: int) -> None:
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
        proc = run_ssh(connection, remote_cmd, check=False, timeout=timeout)
        matches = re.findall(r"^\s*([0-9]+)\s*$", proc.stdout, flags=re.MULTILINE)
        actual_size = int(matches[-1]) if matches else None
        if proc.returncode == 0 and actual_size == expected_size:
            return
        if attempt < 2:
            time.sleep(1)
    raise SshError(
        f"upload verification failed for {src.name} -> {dest}: expected {expected_size} bytes, "
        f"got {actual_size if actual_size is not None else 'unknown'} bytes"
    )


def upload_file(connection: SshConnection, src: Path, dest: str, *, timeout: int = 120) -> None:
    remote_cmd = f"/bin/sh -c {shlex.quote('cat > ' + shlex.quote(dest))}"
    proc = _run_piped_ssh(
        connection,
        remote_cmd,
        input_bytes=src.read_bytes(),
        timeout=timeout,
        missing_tool_message=(
            "SSH uploads with a password require local sshpass. "
            "Run `./tcapsule bootstrap` to install sshpass, then rerun `tcapsule deploy`."
        ),
        timeout_message=f"Timed out copying {src.name} to remote path {dest} over SSH",
    )
    if proc.returncode != 0:
        stdout = _decode_remote_error_output(proc.stderr, proc.stdout).strip()
        raise SshError(stdout or f"SSH upload failed for {src.name} to remote path {dest} with rc={proc.returncode}")
    _verify_uploaded_size(connection, src, dest, timeout=30)
