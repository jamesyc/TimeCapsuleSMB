from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
import time

from timecapsulesmb.core.config import DEFAULTS, AppConfig, ConfigError, load_app_config, require_valid_app_config
from timecapsulesmb.core.net import (
    canonical_ssh_target,
    endpoint_host,
    ipv4_literal,
    is_link_local_ipv4,
    is_link_local_ipv6,
    resolve_host_ipv4s,
    resolve_host_ipv6s,
)
from timecapsulesmb.core.paths import AppPaths, resolve_app_paths
from timecapsulesmb.device.compat import DeviceCompatibility
from timecapsulesmb.device.errors import DeviceError
from timecapsulesmb.device.probe import (
    ProbeResult,
    ProbedDeviceState,
    SshAccessStatus,
    probe_connection_state,
)
from timecapsulesmb.integrations.acp import ACP_PORT
from timecapsulesmb.transport.ssh import SshConnection
from timecapsulesmb.transport.local import tcp_connect_error, tcp_open

PasswordProvider = Callable[[str], str]

# The error code for each way an SSH probe can stop short of a login session.
# Configure uses the same codes. An unsupported model is never one of these:
# that needs a successful login, and its callers raise `unsupported_device`.
PROBE_STATUS_ERROR_CODES: dict[SshAccessStatus, str] = {
    SshAccessStatus.AUTH_REJECTED: "auth_failed",
    SshAccessStatus.ALGORITHM_NEGOTIATION_FAILED: "ssh_compatibility_failed",
    SshAccessStatus.TRANSPORT_FAILED: "ssh_transport_failed",
    SshAccessStatus.DEVICE_PROBE_FAILED: "device_probe_failed",
}
ACP_PORT_CHECK_TIMEOUT_SECONDS = 2.0
# How long a probe of an already set up device keeps trying a closed SSH port.
# The probe tries port 22 once, for 2 s; in v3.1.2 telemetry several deploys
# stopped on that one timeout although SSH was on.
CLOSED_SSH_RECHECK_WINDOW_SECONDS = 6.0
CLOSED_SSH_RECHECK_INTERVAL_SECONDS = 1.0


class DeviceAccessError(DeviceError):
    """The SSH probe did not reach a login session; `code` says why."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


def probe_failure_error(
    probe_result: ProbeResult,
    host: str,
    *,
    tcp_connect_error_func: Callable[[str, int, float], str | None] | None = None,
) -> DeviceAccessError:
    """The error for a probe that did not log in.

    A closed SSH port has two causes the user fixes differently: SSH is off
    (after a reset, or turned off in SSH Access) while the device still
    answers AirPort ACP, or the device is not answering at this address at
    all. One TCP connect to the ACP port tells them apart.
    """
    if probe_result.ssh_status == SshAccessStatus.CLOSED:
        target = endpoint_host(host)
        connect_error = tcp_connect_error_func or tcp_connect_error
        acp_error = connect_error(target, ACP_PORT, ACP_PORT_CHECK_TIMEOUT_SECONDS)
        if acp_error is None:
            return DeviceAccessError(
                f"SSH is turned off on {target}: AirPort ACP answers on port {ACP_PORT}, but SSH port 22 is closed.",
                code="ssh_disabled",
            )
        return DeviceAccessError(
            f"The device is not answering at {target}: neither SSH port 22 nor AirPort ACP port {ACP_PORT} "
            f"is reachable ({acp_error}).",
            code="device_unreachable",
        )
    return DeviceAccessError(
        probe_result.error or "Failed to probe device compatibility.",
        code=PROBE_STATUS_ERROR_CODES.get(probe_result.ssh_status, "remote_error"),
    )


def probe_managed_connection_state(
    connection: SshConnection,
    *,
    probe: Callable[[SshConnection], ProbedDeviceState] | None = None,
    tcp_open_func: Callable[[str, int], bool] | None = None,
    sleep_func: Callable[[float], None] = time.sleep,
    monotonic_func: Callable[[], float] = time.monotonic,
) -> ProbedDeviceState:
    """Probe a device that should already have SSH on.

    A closed port gets a few more seconds before the probe's answer stands,
    and the device is probed again as soon as port 22 opens. Configure does not
    use this: there SSH is usually off and ACP turns it on.
    """
    probe = probe or probe_connection_state
    tcp_open_func = tcp_open_func or tcp_open
    state = probe(connection)
    if state.probe_result.ssh_status != SshAccessStatus.CLOSED:
        return state
    host = endpoint_host(connection.host)
    deadline = monotonic_func() + CLOSED_SSH_RECHECK_WINDOW_SECONDS
    while True:
        remaining = deadline - monotonic_func()
        if remaining <= 0:
            return state
        sleep_func(min(CLOSED_SSH_RECHECK_INTERVAL_SECONDS, remaining))
        if tcp_open_func(host, 22):
            return probe(connection)


@dataclass(frozen=True)
class ManagedTargetState:
    connection: SshConnection
    probe_state: ProbedDeviceState | None


def load_env_config(
    *,
    env_path: Path | None = None,
    defaults: dict[str, str] | None = None,
    resolve_paths: Callable[..., AppPaths] | None = None,
) -> AppConfig:
    if resolve_paths is None:
        resolve_paths = resolve_app_paths
    resolved_path = resolve_paths(config_path=env_path).config_path
    return load_app_config(resolved_path, defaults=defaults)


def load_optional_env_config(
    *,
    env_path: Path | None = None,
    defaults: dict[str, str] | None = None,
    resolve_paths: Callable[..., AppPaths] | None = None,
) -> AppConfig:
    try:
        if resolve_paths is None:
            resolve_paths = resolve_app_paths
        resolved_path = resolve_paths(config_path=env_path).config_path
    except Exception:
        return AppConfig.missing(path=env_path or Path.cwd() / ".env")
    if not resolved_path.exists():
        return AppConfig.missing(path=resolved_path)
    try:
        return load_app_config(resolved_path, defaults=defaults)
    except OSError:
        return AppConfig.missing(path=resolved_path)


def resolve_ssh_credentials(
    config: AppConfig,
    *,
    allow_empty_password: bool = False,
    allow_password_prompt: bool = True,
    password_provider: PasswordProvider | None = None,
) -> tuple[str, str]:
    raw_host = config.require("TC_HOST")
    try:
        host = canonical_ssh_target(raw_host)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    password = config.get("TC_PASSWORD")
    if not password and not allow_empty_password:
        if not allow_password_prompt or password_provider is None:
            raise ConfigError("TC_PASSWORD is required when --no-input is used.")
        password = password_provider("Device root password: ")
    return host, password


def resolve_env_connection(
    config: AppConfig,
    *,
    required_keys: tuple[str, ...] = (),
    allow_empty_password: bool = False,
    allow_password_prompt: bool = True,
    password_provider: PasswordProvider | None = None,
) -> SshConnection:
    for key in required_keys:
        config.require(key)
    host, password = resolve_ssh_credentials(
        config,
        allow_empty_password=allow_empty_password,
        allow_password_prompt=allow_password_prompt,
        password_provider=password_provider,
    )
    return SshConnection(host=host, password=password, ssh_opts=config.get("TC_SSH_OPTS", DEFAULTS["TC_SSH_OPTS"]))


def ssh_target_link_local_resolution_error(
    target: str,
    *,
    field_name: str = "Device SSH target",
) -> str | None:
    host = endpoint_host(target).strip()
    if not host or ipv4_literal(host) is not None:
        return None
    link_local_ips = tuple(ip for ip in resolve_host_ipv4s(host) if is_link_local_ipv4(ip))
    link_local_ipv6s = tuple(ip for ip in resolve_host_ipv6s(host) if is_link_local_ipv6(ip))
    link_local_hosts = link_local_ips + link_local_ipv6s
    if not link_local_hosts:
        return None
    noun = "address" if len(link_local_hosts) == 1 else "addresses"
    return (
        f"{field_name} host {host} resolves to link-local {noun} "
        f"{', '.join(link_local_hosts)}. Use the device's LAN IP or a hostname that resolves "
        "to its LAN IP; link-local addresses are only suitable for temporary SSH recovery."
    )


def resolve_validated_managed_target(
    config: AppConfig,
    *,
    command_name: str,
    profile: str,
    include_probe: bool = False,
    allow_password_prompt: bool = True,
    password_provider: PasswordProvider | None = None,
) -> ManagedTargetState:
    require_valid_app_config(config, profile=profile, command_name=command_name)
    resolution_error = ssh_target_link_local_resolution_error(config.require("TC_HOST"), field_name="TC_HOST")
    if resolution_error is not None:
        raise ConfigError(resolution_error)
    connection = resolve_env_connection(
        config,
        allow_password_prompt=allow_password_prompt,
        password_provider=password_provider,
    )
    if profile == "flash":
        return ManagedTargetState(connection=connection, probe_state=None)
    probe_state = probe_managed_connection_state(connection) if include_probe else None
    return ManagedTargetState(connection=connection, probe_state=probe_state)


def require_connection_compatibility(connection: SshConnection) -> DeviceCompatibility:
    state = probe_managed_connection_state(connection)
    if state.compatibility is None:
        raise probe_failure_error(state.probe_result, connection.host)
    return state.compatibility


def wait_for_tcp_port_state(
    host: str,
    port: int,
    *,
    expected_state: bool,
    timeout_seconds: int = 120,
    interval_seconds: int = 5,
    log: Callable[[str], None] | None = None,
    service_name: str | None = None,
    tcp_open_func: Callable[[str, int], bool] = tcp_open,
) -> bool:
    label = service_name or f"TCP port {port}"
    expected_state_string = "open" if expected_state else "closed"
    if log is not None:
        log(f"Waiting for {label} to be {expected_state_string}...")
    deadline = time.time() + timeout_seconds
    while True:
        is_open = tcp_open_func(host, port)
        if is_open == expected_state:
            if log is not None:
                log(f"{label} is {expected_state_string}.")
            return True
        if time.time() >= deadline:
            break
        time.sleep(interval_seconds)
    if log is not None:
        log(f"{label} did not become {expected_state_string} within {timeout_seconds}s.")
    return False
