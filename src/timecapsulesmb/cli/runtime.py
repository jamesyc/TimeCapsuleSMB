from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from timecapsulesmb.core.config import (
    DEFAULTS,
    AppConfig,
    load_app_config,
    require_valid_app_config,
)
from timecapsulesmb.core.paths import resolve_app_paths
from timecapsulesmb.device.compat import DeviceCompatibility, require_compatibility
from timecapsulesmb.device.probe import (
    ProbedDeviceState,
    RemoteInterfaceProbeResult,
    probe_connection_state,
    probe_remote_interface_conn,
)
from timecapsulesmb.transport.ssh import SshConnection


@dataclass(frozen=True)
class ManagedTargetState:
    connection: SshConnection
    interface_probe: RemoteInterfaceProbeResult
    probe_state: ProbedDeviceState | None


def load_env_config(*, env_path: Path | None = None, defaults: dict[str, str] | None = None) -> AppConfig:
    resolved_path = env_path or resolve_app_paths().env_path
    return load_app_config(resolved_path, defaults=defaults)


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


def resolve_validated_managed_target(
    config: AppConfig,
    *,
    command_name: str,
    profile: str,
    include_probe: bool = False,
) -> ManagedTargetState:
    require_valid_app_config(config, profile=profile, command_name=command_name)
    connection = resolve_env_connection(config)
    target = inspect_managed_connection(connection, config.require("TC_NET_IFACE"), include_probe=include_probe)
    if not target.interface_probe.exists:
        raise SystemExit(
            "TC_NET_IFACE is invalid. Run the `configure` command again.\n"
            f"{target.interface_probe.detail}."
        )
    return target


def require_connection_compatibility(connection: SshConnection) -> DeviceCompatibility:
    state = probe_connection_state(connection)
    return require_compatibility(
        state.compatibility,
        fallback_error=state.probe_result.error or "Failed to determine remote device OS compatibility.",
    )
