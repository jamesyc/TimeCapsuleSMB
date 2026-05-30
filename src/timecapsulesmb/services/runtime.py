from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import time
from collections.abc import Callable

from timecapsulesmb.core.config import DEFAULTS, AppConfig, ConfigError, load_app_config, require_valid_app_config
from timecapsulesmb.core.errors import system_exit_message
from timecapsulesmb.core.net import (
    extract_host,
    ipv4_literal,
    is_link_local_ipv4,
    is_link_local_ipv6,
    resolve_host_ipv4s,
    resolve_host_ipv6s,
)
from timecapsulesmb.core.paths import AppPaths, resolve_app_paths
from timecapsulesmb.device.compat import DeviceCompatibility, require_compatibility
from timecapsulesmb.device.probe import (
    ProbedDeviceState,
    RemoteInterfaceProbeResult,
    probe_connection_state,
    probe_remote_interface_conn,
    wait_for_ssh_state_conn,
)
from timecapsulesmb.deploy.executor import remote_request_reboot
from timecapsulesmb.integrations.acp import ACPError, reboot as acp_reboot
from timecapsulesmb.transport.ssh import SshConnection, ssh_opts_use_proxy
from timecapsulesmb.transport.ssh import SshCommandTimeout, SshError
from timecapsulesmb.transport.local import tcp_open


@dataclass(frozen=True)
class ManagedTargetState:
    connection: SshConnection
    interface_probe: RemoteInterfaceProbeResult | None
    probe_state: ProbedDeviceState | None


@dataclass(frozen=True)
class RuntimeOperationCallbacks:
    set_stage: Callable[[str], None] | None = None
    log: Callable[[str], None] | None = None
    add_debug_fields: Callable[..., None] | None = None
    update_fields: Callable[..., None] | None = None


@dataclass(frozen=True)
class RebootCycleResult:
    went_down: bool
    came_back_up: bool

    @property
    def completed(self) -> bool:
        return self.went_down and self.came_back_up


ACP_REBOOT_REQUEST_TIMEOUT_SECONDS = 10
SSH_SHUTDOWN_REBOOT_PROGRESS_MESSAGE = "SSH: /bin/sync; /sbin/shutdown -r now (fallback /sbin/reboot)"


def _call_stage(callbacks: RuntimeOperationCallbacks, stage: str) -> None:
    if callbacks.set_stage is not None:
        callbacks.set_stage(stage)


def _call_log(callbacks: RuntimeOperationCallbacks, message: str) -> None:
    if callbacks.log is not None:
        callbacks.log(message)


def _call_debug(callbacks: RuntimeOperationCallbacks, **fields: object) -> None:
    if callbacks.add_debug_fields is not None:
        callbacks.add_debug_fields(**fields)


def _call_update(callbacks: RuntimeOperationCallbacks, **fields: object) -> None:
    if callbacks.update_fields is not None:
        callbacks.update_fields(**fields)


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
) -> tuple[str, str]:
    host = config.require("TC_HOST")
    password = config.get("TC_PASSWORD")
    if not password and not allow_empty_password:
        if not allow_password_prompt:
            raise ConfigError("TC_PASSWORD is required when --no-input is used.")
        import getpass
        password = getpass.getpass("Device root password: ")
    return host, password


def resolve_env_connection(
    config: AppConfig,
    *,
    required_keys: tuple[str, ...] = (),
    allow_empty_password: bool = False,
    allow_password_prompt: bool = True,
) -> SshConnection:
    for key in required_keys:
        config.require(key)
    host, password = resolve_ssh_credentials(
        config,
        allow_empty_password=allow_empty_password,
        allow_password_prompt=allow_password_prompt,
    )
    return SshConnection(host=host, password=password, ssh_opts=config.get("TC_SSH_OPTS", DEFAULTS["TC_SSH_OPTS"]))


def inspect_managed_connection(
    connection: SshConnection,
    iface: str,
    *,
    include_probe: bool = False,
) -> ManagedTargetState:
    interface_probe = probe_remote_interface_conn(connection, iface)
    probe_state = probe_connection_state(connection) if include_probe else None
    return ManagedTargetState(connection=connection, interface_probe=interface_probe, probe_state=probe_state)


def ssh_target_link_local_resolution_error(
    target: str,
    ssh_opts: str,
    *,
    field_name: str = "Device SSH target",
) -> str | None:
    if ssh_opts_use_proxy(ssh_opts):
        return None
    host = extract_host(target).strip()
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
) -> ManagedTargetState:
    require_valid_app_config(config, profile=profile, command_name=command_name)
    resolution_error = ssh_target_link_local_resolution_error(
        config.require("TC_HOST"),
        config.get("TC_SSH_OPTS", DEFAULTS["TC_SSH_OPTS"]),
        field_name="TC_HOST",
    )
    if resolution_error is not None:
        raise ConfigError(resolution_error)
    connection = resolve_env_connection(config, allow_password_prompt=allow_password_prompt)
    if profile == "flash":
        return ManagedTargetState(connection=connection, interface_probe=None, probe_state=None)
    probe_state = probe_connection_state(connection) if include_probe else None
    return ManagedTargetState(connection=connection, interface_probe=None, probe_state=probe_state)


def require_connection_compatibility(connection: SshConnection) -> DeviceCompatibility:
    state = probe_connection_state(connection)
    return require_compatibility(
        state.compatibility,
        fallback_error=state.probe_result.error or "Failed to determine remote device OS compatibility.",
    )


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


def request_runtime_reboot(
    connection: SshConnection,
    *,
    strategy: str,
    callbacks: RuntimeOperationCallbacks | None = None,
    progress_log: Callable[[str], None] | None = None,
    raise_on_request_error: bool = False,
    request_reboot: Callable[[SshConnection], None] = remote_request_reboot,
    request_acp_reboot: Callable[..., object] = acp_reboot,
) -> None:
    callbacks = callbacks or RuntimeOperationCallbacks()
    _call_stage(callbacks, "reboot")
    _call_update(callbacks, reboot_was_attempted=True)
    _call_debug(callbacks, reboot_request_strategy=strategy)
    if strategy == "acp_then_ssh":
        _request_reboot_acp_then_ssh(
            connection,
            callbacks=callbacks,
            progress_log=progress_log,
            raise_on_request_error=raise_on_request_error,
            request_reboot=request_reboot,
            request_acp_reboot=request_acp_reboot,
        )
        return
    _request_reboot_via_ssh(
        connection,
        callbacks=callbacks,
        progress_log=progress_log,
        request_reboot=request_reboot,
        raise_on_request_error=raise_on_request_error,
    )


def _request_reboot_acp_then_ssh(
    connection: SshConnection,
    *,
    callbacks: RuntimeOperationCallbacks,
    progress_log: Callable[[str], None] | None,
    raise_on_request_error: bool,
    request_reboot: Callable[[SshConnection], None],
    request_acp_reboot: Callable[..., object],
) -> None:
    _call_debug(callbacks, acp_reboot_attempted=True)
    try:
        request_acp_reboot(
            extract_host(connection.host),
            connection.password,
            timeout=ACP_REBOOT_REQUEST_TIMEOUT_SECONDS,
        )
    except ACPError as exc:
        _call_debug(
            callbacks,
            acp_reboot_succeeded=False,
            acp_reboot_error=system_exit_message(exc),
        )
        _call_log(callbacks, "ACP reboot request failed; trying SSH reboot request.")
        _request_reboot_via_ssh(
            connection,
            callbacks=callbacks,
            progress_log=progress_log,
            request_reboot=request_reboot,
            raise_on_request_error=raise_on_request_error,
        )
        return

    _call_debug(callbacks, acp_reboot_succeeded=True)
    _call_log(callbacks, "ACP reboot requested.")


def _request_reboot_via_ssh(
    connection: SshConnection,
    *,
    callbacks: RuntimeOperationCallbacks,
    progress_log: Callable[[str], None] | None,
    request_reboot: Callable[[SshConnection], None],
    progress_message: str = SSH_SHUTDOWN_REBOOT_PROGRESS_MESSAGE,
    raise_on_request_error: bool,
) -> None:
    _call_debug(callbacks, ssh_reboot_attempted=True)
    if progress_log is not None:
        progress_log(progress_message)
    try:
        request_reboot(connection)
    except SshCommandTimeout as exc:
        _call_debug(
            callbacks,
            ssh_reboot_succeeded=False,
            ssh_reboot_timed_out=True,
            ssh_reboot_error=system_exit_message(exc),
        )
        if raise_on_request_error:
            raise
        _call_log(callbacks, "SSH reboot request timed out; checking whether the device is rebooting...")
        return
    except SshError as exc:
        _call_debug(
            callbacks,
            ssh_reboot_succeeded=False,
            ssh_reboot_error=system_exit_message(exc),
        )
        if raise_on_request_error:
            raise
        _call_log(callbacks, "SSH reboot request failed; checking whether the device is rebooting anyway...")
        return

    _call_debug(callbacks, ssh_reboot_succeeded=True)
    _call_log(callbacks, "SSH reboot requested.")


def observe_runtime_reboot_cycle(
    connection: SshConnection,
    *,
    callbacks: RuntimeOperationCallbacks | None = None,
    down_timeout_seconds: int,
    up_timeout_seconds: int,
    wait_for_ssh_state: Callable[..., bool] = wait_for_ssh_state_conn,
) -> RebootCycleResult:
    callbacks = callbacks or RuntimeOperationCallbacks()
    _call_log(callbacks, "Waiting for the device to go down...")
    _call_stage(callbacks, "wait_for_reboot_down")
    if not wait_for_ssh_state(connection, expected_up=False, timeout_seconds=down_timeout_seconds):
        return RebootCycleResult(went_down=False, came_back_up=False)

    _call_log(callbacks, "Device went down; waiting for it to come back up...")
    _call_stage(callbacks, "wait_for_reboot_up")
    if not wait_for_ssh_state(connection, expected_up=True, timeout_seconds=up_timeout_seconds):
        return RebootCycleResult(went_down=True, came_back_up=False)

    _call_update(callbacks, device_came_back_after_reboot=True)
    _call_log(callbacks, "Device is back online.")
    return RebootCycleResult(went_down=True, came_back_up=True)
