from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
import atexit
import hashlib
import shlex
import shutil
import subprocess
import os
import re
import tempfile
import threading
import time
from pathlib import Path

from timecapsulesmb.core.net import endpoint_host
from timecapsulesmb.transport.errors import (
    SshAlgorithmNegotiationError,
    SshAuthenticationError,
    SshClientConfigError,
    SshClientCrashedError as SshClientCrashedError,
    ssh_signal_error,
    SshCommandTimeout,
    SshError,
    SshLocalNetworkFilteredError,
    SshNetworkError,
    local_network_filtered_message,
)

from timecapsulesmb.core.process import popen_process, run_process

from .local import tcp_open
from .ssh_client import require_local_ssh


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

# See SshLocalNetworkFilteredError for why EBADF means this computer dropped it.
SSH_LOCAL_NETWORK_FILTERED_PATTERN = re.compile(r"^ssh: connect to host \S+ port \d+: Bad file descriptor$")
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
# ssh runs this with each prompt; it answers the password from SSH_PASSWORD_ENV.
SSH_ASKPASS_PATH = Path(__file__).with_name("ssh-askpass")
SSH_PASSWORD_ENV = "TCAPSULE_SSH_PASSWORD"
# ssh logs this when SSH_ASKPASS cannot run; it then sends an empty password.
SSH_ASKPASS_EXEC_FAILURE = "ssh_askpass: exec("
# Every command shares one authenticated SSH connection per device. A new
# login costs about a second against a Time Capsule (key exchange on its CPU,
# then password authentication), a shared session about 40 ms, and a deploy
# runs nearly 200 commands. The first command authenticates and its ssh
# becomes the background master (ControlMaster=auto); while it lives, later
# commands open sessions over it, and once it is gone ssh logs in afresh.
# A master quits after ControlPersist idle seconds, when this process exits,
# and before every reboot request; ServerAlive ends one whose device vanished
# some other way, so commands then reconnect instead of waiting on it.
SSH_CONTROL_DIR_PREFIX = "tcsmb-ssh-"
SSH_CONTROL_PERSIST_SECONDS = 180
SSH_SERVER_ALIVE_INTERVAL_SECONDS = 15
SSH_SERVER_ALIVE_COUNT_MAX = 3
SSH_CONTROL_EXIT_TIMEOUT_SECONDS = 5
# A session over a master never authenticates itself, so its log has no
# "Authenticated to" line; at DEBUG1 ssh logs this line for it instead.
SSH_MUX_SESSION_MARKER = "mux_client_request_session: master session id:"
REMOTE_COMMAND_SUMMARY_LIMIT = 500
SSH_ERROR_STDERR_LIMIT_BYTES = 65536
SSH_ERROR_STDOUT_PREFIX_BYTES = 8192
DEVICE_HOSTS_PATH = "/etc/hosts"


def client_hosts_line_command(hosts_path: str = DEVICE_HOSTS_PATH) -> str:
    """Return the shell prefix that maps this client's address in hosts_path.

    Apple's sshd looks up the client's hostname at every password login:
    NetBSD's allowed_user() calls get_canonical_hostname(1) for login.conf's
    host.allow and host.deny, whatever UseDNS says (OpenSSH 4.4 on NetBSD 4,
    5.9 on NetBSD 6). When the device's DNS server never answers, every login
    waits 15 seconds or more. The resolver reads /etc/hosts before DNS
    (hosts: files dns), so each command adds one line for the address sshd
    saw, named after it so the name maps back to it from the file too, and
    later logins from that address skip DNS.

    /etc/hosts is on the RAM root and Apple writes it fresh at boot, so the
    first command after any reboot waits once and adds the line again. Lines
    are only appended, never rewritten: Apple's lines and the manager's own
    mapping stay byte for byte, and a line lost to a concurrent rewrite comes
    back with the next command. Commands that start together before the line
    exists (doctor runs some in parallel) may each append it; the duplicates
    are harmless and go at reboot. The leading newline keeps a last line without
    one intact. Link-local addresses are skipped, since a hosts line cannot
    carry their scope. The prefix is silent and ends in ";", so the caller's
    command runs after it with its own stdin, output and exit status.
    """
    hosts = shlex.quote(hosts_path)
    return (
        "{ _tc=${SSH_CLIENT%% *}\n"
        "case $_tc in ''|*%*|[Ff][Ee]80:*) ;;\n"
        f'*) case "\n$(cat {hosts})" in *"\n$_tc tcsmb-"*) ;;\n'
        f"""*) printf '\\n%s tcsmb-%s\\n' "$_tc" "$(echo "$_tc" | sed 's/[.:]/-/g')" >> {hosts} ;;\n"""
        "esac ;;\n"
        "esac; unset _tc; } </dev/null >/dev/null 2>&1; "
    )


CLIENT_HOSTS_LINE_COMMAND = client_hosts_line_command()


def _with_client_hosts_line(remote_cmd: str) -> str:
    return CLIENT_HOSTS_LINE_COMMAND + remote_cmd


def _summarize_remote_command(remote_cmd: str) -> str:
    summary = " ".join(remote_cmd.split())
    if len(summary) <= REMOTE_COMMAND_SUMMARY_LIMIT:
        return summary
    return summary[: REMOTE_COMMAND_SUMMARY_LIMIT - 3] + "..."


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
    if SSH_ASKPASS_EXEC_FAILURE in line:
        return SshClientConfigError(f"The SSH password helper could not run; reinstall TimeCapsuleSMB. ({line})")
    if SSH_LOCAL_NETWORK_FILTERED_PATTERN.fullmatch(line):
        return SshLocalNetworkFilteredError(f"{local_network_filtered_message()} ({line})")
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
        if line.startswith("debug1:") and SSH_MUX_SESSION_MARKER in line:
            # The master authenticated this session's connection.
            authenticated = True
            continue
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


@lru_cache(maxsize=None)
def _ssh_option_supported(option_name: str) -> bool:
    try:
        proc = run_process(
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
        proc = run_process(
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
    "controlmaster",
    "controlpath",
    "controlpersist",
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


_control_lock = threading.Lock()
_control_dir: str | None = None
# Control socket path -> device host, for closing a device's master.
_control_hosts: dict[str, str] = {}


def _control_path(connection: SshConnection) -> str:
    """Return this process's master socket path for the connection.

    The name covers the password and options too, so a command never rides on
    a master that another password or option set authenticated: a password
    check must log in itself. The private directory is short because macOS
    limits socket paths to 104 bytes.
    """
    global _control_dir
    key = hashlib.sha256(
        "\0".join((connection.host, connection.ssh_opts, connection.password)).encode("utf-8")
    ).hexdigest()[:16]
    with _control_lock:
        if _control_dir is None:
            _control_dir = tempfile.mkdtemp(prefix=SSH_CONTROL_DIR_PREFIX, dir="/tmp")
            atexit.register(close_ssh_masters)
        path = os.path.join(_control_dir, key)
        _control_hosts[path] = endpoint_host(connection.host)
    return path


def close_ssh_masters(host: str | None = None) -> None:
    """Close this process's shared SSH connections, to `host` or to every device.

    Call before anything that drops the device's SSH connections, such as a
    reboot: a master left to a rebooting device would hold the next commands
    until ServerAlive gives up on it.
    """
    global _control_dir
    wanted = endpoint_host(host) if host is not None else None
    with _control_lock:
        paths = [path for path, path_host in _control_hosts.items() if wanted is None or path_host == wanted]
        for path in paths:
            del _control_hosts[path]
        directory = None
        if wanted is None:
            # The directory goes too; the next shared command makes a new one.
            directory, _control_dir = _control_dir, None
    for path in paths:
        if not os.path.exists(path):
            continue
        try:
            run_process(
                ["ssh", "-F", "/dev/null", "-o", f"ControlPath={path}", "-O", "exit", "tcsmb-master"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=SSH_CONTROL_EXIT_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    if directory is not None:
        shutil.rmtree(directory, ignore_errors=True)


def _connection_ssh_args(
    connection: SshConnection,
    *,
    client_log: Path,
    stdin_null: bool,
    extra_args: tuple[str, ...] = (),
    shared: bool = False,
) -> list[str]:
    """Return config-isolated SSH args with transport-owned session behavior.

    `shared` sends the command over the device's shared connection.
    """
    tokens = _normalize_ssh_tokens(connection.ssh_opts)

    if not connection.password:
        auth_args = ["-o", "BatchMode=yes"]
    elif _tokens_request_public_key_auth(tokens):
        auth_args = []
    else:
        # Avoid passphrase prompts from unintended default keys while preserving
        # explicit key configuration and keyboard-interactive password servers.
        auth_args = ["-o", "PubkeyAuthentication=no"]

    if shared:
        session_args = [
            "-o", "ControlMaster=auto",
            "-o", f"ControlPath={_control_path(connection)}",
            "-o", f"ControlPersist={SSH_CONTROL_PERSIST_SECONDS}",
        ]
        # After the caller's options, which may set their own keepalive.
        keepalive_args = [
            "-o", f"ServerAliveInterval={SSH_SERVER_ALIVE_INTERVAL_SECONDS}",
            "-o", f"ServerAliveCountMax={SSH_SERVER_ALIVE_COUNT_MAX}",
        ]
    else:
        session_args = ["-S", "none"]
        keepalive_args = []

    return [
        "-F", "/dev/null",
        "-o", f"LogLevel={'DEBUG1' if shared else 'VERBOSE'}",
        "-o", "NumberOfPasswordPrompts=1",
        "-o", "ExitOnForwardFailure=yes",
        *auth_args,
        *extra_args,
        *tokens,
        *keepalive_args,
        "-E", str(client_log),
        *session_args,
        "-T",
        "-a",
        "-x",
        *(["-n"] if stdin_null else []),
    ]


def _ssh_env(connection: SshConnection) -> dict[str, str] | None:
    """The environment that gives ssh the password, or None to inherit ours.

    ssh asks SSH_ASKPASS for every prompt when SSH_ASKPASS_REQUIRE is force
    (OpenSSH 8.4+), so it needs no terminal and starts like any other program.
    Only this child's copy holds the password.
    """
    if not connection.password:
        return None
    return {
        **os.environ,
        "SSH_ASKPASS": str(SSH_ASKPASS_PATH),
        "SSH_ASKPASS_REQUIRE": "force",
        SSH_PASSWORD_ENV: connection.password,
    }


def _run_ssh(
    connection: SshConnection,
    remote_cmd: str,
    *,
    input_bytes: bytes | None = None,
    merge_stderr: bool = False,
    timeout: int | None,
    timeout_message: str,
    extra_ssh_args: tuple[str, ...] = (),
) -> subprocess.CompletedProcess[bytes]:
    """Run one remote command over the device's shared SSH connection.

    ssh sends it over the live master, or logs in itself and becomes the
    master (ControlMaster=auto), so the command runs once either way. A
    rejected login is retried: the remote command never started, so it cannot
    run twice. NetBSD 4 devices sometimes refuse the right password while
    busy. Any other failure is returned or raised at once.
    """
    stdin_kwargs: dict[str, object] = (
        {"stdin": subprocess.DEVNULL} if input_bytes is None else {"input": input_bytes}
    )
    executable = require_local_ssh() if connection.password else "ssh"
    attempt = 0
    while True:
        with _ssh_client_log_path() as client_log:
            cmd = [
                executable,
                *_connection_ssh_args(
                    connection,
                    client_log=client_log,
                    stdin_null=input_bytes is None,
                    extra_args=extra_ssh_args,
                    shared=True,
                ),
                connection.host,
                _with_client_hosts_line(remote_cmd),
            ]
            try:
                proc = run_process(
                    cmd,
                    **stdin_kwargs,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT if merge_stderr else subprocess.PIPE,
                    env=_ssh_env(connection),
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
        if proc.returncode < 0:
            raise ssh_signal_error(-proc.returncode)
        client_error = _ssh_client_error_for_result(
            diagnostics,
            returncode=proc.returncode,
            startup_output=_decode_remote_error_output(
                proc.stdout if merge_stderr else proc.stderr, include_stdout=False
            ),
        )
        if client_error is None:
            return proc
        if not _should_retry_password_auth(connection, diagnostics, attempt):
            raise client_error
        attempt += 1
        time.sleep(1)


def run_ssh(connection: SshConnection, remote_cmd: str, *, check: bool = True, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    """Run a command and return its stdout and stderr joined, as text."""
    proc = _run_ssh(
        connection,
        remote_cmd,
        merge_stderr=True,
        timeout=timeout,
        timeout_message=f"Timed out waiting for ssh command to finish: {_summarize_remote_command(remote_cmd)}",
    )
    stdout = proc.stdout.decode("utf-8", errors="replace")
    if check and proc.returncode != 0:
        raise SshError(stdout.strip() or f"ssh command failed with rc={proc.returncode}")
    return subprocess.CompletedProcess(proc.args, proc.returncode, stdout=stdout, stderr="")


def run_ssh_input(
    connection: SshConnection,
    remote_cmd: str,
    *,
    input_bytes: bytes = b"",
    timeout: int | None = 120,
    raw_remote_status: bool = False,
    extra_ssh_args: tuple[str, ...] = (),
) -> subprocess.CompletedProcess[bytes]:
    """Send a bounded request, keeping stdout and logs separate.

    raw_remote_status returns a nonzero remote status instead of raising.
    """
    proc = _run_ssh(
        connection, remote_cmd, input_bytes=input_bytes, timeout=timeout,
        timeout_message=f"Timed out waiting for ssh command to finish: {_summarize_remote_command(remote_cmd)}",
        extra_ssh_args=extra_ssh_args,
    )
    if proc.returncode and not raw_remote_status:
        raise SshError(_decode_remote_error_output(proc.stderr, proc.stdout).strip()
                       or f"ssh command failed with rc={proc.returncode}")
    return proc


def run_ssh_capture_bytes(connection: SshConnection, remote_cmd: str, *, timeout: int = 120) -> bytes:
    """Run a remote command over SSH and return raw stdout bytes, such as a firmware bank."""
    proc = _run_ssh(
        connection,
        remote_cmd,
        timeout=timeout,
        timeout_message=f"Timed out waiting for ssh command to finish: {_summarize_remote_command(remote_cmd)}",
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
    with _ssh_client_log_path() as client_log:
        # A tunnel keeps its own connection: a forward opened through a
        # master belongs to the master and would outlive this tunnel.
        cmd = [
            require_local_ssh() if connection.password else "ssh",
            *_connection_ssh_args(connection, client_log=client_log, stdin_null=True),
            "-N",
            "-L",
            f"{local_port}:{remote_host}:{remote_port}",
            connection.host,
        ]
        try:
            child = popen_process(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                # ssh logs to client_log; only a startup error comes here.
                stderr=subprocess.PIPE,
                env=_ssh_env(connection),
            )
        except OSError as exc:
            raise SshClientConfigError(f"Could not start local SSH client: {exc}") from exc
        try:
            deadline = time.monotonic() + ready_timeout
            while True:
                diagnostics = _read_ssh_client_diagnostics(client_log)
                if diagnostics.error is not None:
                    raise diagnostics.error
                returncode = child.poll()
                if returncode is not None:
                    if returncode < 0:
                        raise ssh_signal_error(-returncode)
                    startup_output = child.stderr.read().decode("utf-8", errors="replace") if child.stderr else ""
                    startup_error = _classify_ssh_startup_error(startup_output) if not diagnostics.text else None
                    if startup_error is not None:
                        raise startup_error
                    raise SshError(
                        _client_failure_detail(diagnostics)
                        or startup_output.strip()
                        or "ssh tunnel exited before becoming ready"
                    )
                if tcp_open("127.0.0.1", local_port, timeout=0.2):
                    break
                if time.monotonic() >= deadline:
                    raise SshError(
                        "Timed out waiting for ssh tunnel to become ready: "
                        f"127.0.0.1:{local_port} -> {remote_host}:{remote_port} via {connection.host}"
                    )
                time.sleep(0.2)
            yield
        finally:
            child.terminate()
            try:
                child.wait(timeout=SSH_CONTROL_EXIT_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
            if child.stderr is not None:
                child.stderr.close()


def upload_file(connection: SshConnection, src: Path, dest: str, *, timeout: int = 120) -> None:
    """Write src to dest on the device and check the written size.

    dd gathers the pipe's short reads into 1 MiB writes: NetBSD 4 mounts
    /mnt/Flash synchronously, so every write is a flash write, and cat's small
    ones took the 333 KB service 124 s against 99 s. The same command prints
    the size the device then has, so a short file fails here.
    """
    quoted_dest = shlex.quote(dest)
    remote_script = f'dd of={quoted_dest} ibs=65536 obs=1048576 && set -- $(ls -l {quoted_dest}) && echo "$5"'
    expected_size = src.stat().st_size
    remote_cmd = f"/bin/sh -c {shlex.quote(remote_script)}"
    proc = _run_ssh(
        connection,
        remote_cmd,
        input_bytes=src.read_bytes(),
        timeout=timeout,
        timeout_message=f"Timed out copying {src.name} to remote path {dest} over SSH",
    )
    if proc.returncode != 0:
        stdout = _decode_remote_error_output(proc.stderr, proc.stdout).strip()
        raise SshError(stdout or f"SSH upload failed for {src.name} to remote path {dest} with rc={proc.returncode}")
    sizes = re.findall(rb"^\s*([0-9]+)\s*$", proc.stdout, flags=re.MULTILINE)
    actual_size = int(sizes[-1]) if sizes else None
    if actual_size != expected_size:
        raise SshError(
            f"upload verification failed for {src.name} -> {dest}: expected {expected_size} bytes, "
            f"got {actual_size if actual_size is not None else 'unknown'} bytes"
        )
