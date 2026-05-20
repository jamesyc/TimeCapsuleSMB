from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import time
from typing import Callable

from timecapsulesmb.core.config import DEFAULTS, AppConfig, ConfigError, load_app_config, require_valid_app_config
from timecapsulesmb.core.net import extract_host, ipv4_literal, is_link_local_ipv4, resolve_host_ipv4s
from timecapsulesmb.core.paths import resolve_app_paths
from timecapsulesmb.device.compat import require_compatibility
from timecapsulesmb.device.probe import (
    ProbedDeviceState,
    RemoteInterfaceProbeResult,
    probe_connection_state,
    probe_remote_interface_conn,
)
from timecapsulesmb.transport.ssh import SshConnection, ssh_opts_use_proxy
from timecapsulesmb.transport.local import tcp_open


@dataclass(frozen=True)
class ManagedTargetState:
    connection: SshConnection
    interface_probe: RemoteInterfaceProbeResult | None
    probe_state: ProbedDeviceState | None


def load_env_config(
    *,
    env_path: Path | None = None,
    defaults: dict[str, str] | None = None,
    resolve_paths=resolve_app_paths,
) -> AppConfig:
    resolved_path = resolve_paths(config_path=env_path).config_path
    return load_app_config(resolved_path, defaults=defaults)


def load_optional_env_config(
    *,
    env_path: Path | None = None,
    defaults: dict[str, str] | None = None,
    resolve_paths=resolve_app_paths,
) -> AppConfig:
    try:
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
) -> tuple[str, str]:
    host = config.require("TC_HOST")
    password = config.get("TC_PASSWORD")
    if not password and not allow_empty_password:
        import getpass
        password = getpass.getpass("Device root password: ")
    return host, password


def resolve_env_connection(
    config: AppConfig,
    *,
    required_keys: tuple[str, ...] = (),
    allow_empty_password: bool = False,
) -> SshConnection:
    for key in required_keys:
        config.require(key)
    host, password = resolve_ssh_credentials(config, allow_empty_password=allow_empty_password)
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
    if not link_local_ips:
        return None
    noun = "address" if len(link_local_ips) == 1 else "addresses"
    return (
        f"{field_name} host {host} resolves to 169.254.x.x link-local IPv4 {noun} "
        f"{', '.join(link_local_ips)}. Use the device's LAN IP or a hostname that resolves "
        "to its LAN IP; 169.254.x.x is only suitable for temporary SSH recovery."
    )


def resolve_validated_managed_target(
    config: AppConfig,
    *,
    command_name: str,
    profile: str,
    include_probe: bool = False,
) -> ManagedTargetState:
    require_valid_app_config(config, profile=profile, command_name=command_name)
    resolution_error = ssh_target_link_local_resolution_error(
        config.require("TC_HOST"),
        config.get("TC_SSH_OPTS", DEFAULTS["TC_SSH_OPTS"]),
        field_name="TC_HOST",
    )
    if resolution_error is not None:
        raise ConfigError(resolution_error)
    connection = resolve_env_connection(config)
    if profile == "flash":
        return ManagedTargetState(connection=connection, interface_probe=None, probe_state=None)
    probe_state = probe_connection_state(connection) if include_probe else None
    return ManagedTargetState(connection=connection, interface_probe=None, probe_state=probe_state)


def require_connection_compatibility(connection: SshConnection):
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
) -> bool:
    label = service_name or f"TCP port {port}"
    expected_state_string = "open" if expected_state else "closed"
    if log is not None:
        log(f"Waiting for {label} to be {expected_state_string}...")
    deadline = time.time() + timeout_seconds
    while True:
        is_open = tcp_open(host, port)
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
