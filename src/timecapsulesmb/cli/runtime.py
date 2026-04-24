from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from timecapsulesmb.core.config import AppConfig, ENV_PATH, parse_env_values, require_valid_config
from timecapsulesmb.device.compat import DeviceCompatibility, compatibility_from_probe_result, require_compatibility
from timecapsulesmb.device.probe import (
    ProbedDeviceState,
    RemoteInterfaceProbeResult,
    probe_device_conn,
    probe_remote_interface_conn as probe_remote_interface,
)
from timecapsulesmb.transport.ssh import SshConnection


@dataclass(frozen=True)
class ManagedTargetState:
    connection: SshConnection
    interface_probe: RemoteInterfaceProbeResult
    probe_state: ProbedDeviceState | None


def load_env_values(*, env_path: Path = ENV_PATH, defaults: dict[str, str] | None = None) -> dict[str, str]:
    resolved_defaults = defaults if defaults is not None else None
    return parse_env_values(env_path, defaults=resolved_defaults)


def require_airport_syap(values: dict[str, str], *, command_name: str) -> None:
    AppConfig(values).require(
        "TC_AIRPORT_SYAP",
        messageafter=f"\nPlease run the `configure` command before running `{command_name}`.",
    )


def resolve_ssh_credentials(
    values: dict[str, str],
    *,
    allow_empty_password: bool = False,
) -> tuple[str, str]:
    config = AppConfig(values)
    host = config.require("TC_HOST")
    password = config.get("TC_PASSWORD")
    if not password and not allow_empty_password:
        import getpass
        password = getpass.getpass("Time Capsule root password: ")
    return host, password


def resolve_env_connection(
    values: dict[str, str],
    *,
    required_keys: tuple[str, ...] = (),
    allow_empty_password: bool = False,
) -> SshConnection:
    config = AppConfig(values)
    for key in required_keys:
        config.require(key)
    host, password = resolve_ssh_credentials(values, allow_empty_password=allow_empty_password)
    return SshConnection(host=host, password=password, ssh_opts=config.get("TC_SSH_OPTS"))


def inspect_managed_connection(
    connection: SshConnection,
    iface: str,
    *,
    include_probe: bool = False,
) -> ManagedTargetState:
    interface_probe = probe_remote_interface(connection, iface)
    probe_state = probe_connection_state(connection) if include_probe else None
    return ManagedTargetState(connection=connection, interface_probe=interface_probe, probe_state=probe_state)


def resolve_validated_managed_target(
    values: dict[str, str],
    *,
    command_name: str,
    profile: str,
    include_probe: bool = False,
) -> ManagedTargetState:
    require_airport_syap(values, command_name=command_name)
    require_valid_config(values, profile=profile)
    connection = resolve_env_connection(values)
    target = inspect_managed_connection(connection, values["TC_NET_IFACE"], include_probe=include_probe)
    if not target.interface_probe.exists:
        raise SystemExit(
            "TC_NET_IFACE is invalid. Run the `configure` command again.\n"
            f"{target.interface_probe.detail}."
        )
    return target


def probe_device_state(host: str, password: str, ssh_opts: str) -> ProbedDeviceState:
    probe_result = probe_device_conn(SshConnection(host=host, password=password, ssh_opts=ssh_opts))
    compatibility = compatibility_from_probe_result(probe_result)
    return ProbedDeviceState(probe_result=probe_result, compatibility=compatibility)


def probe_connection_state(connection: SshConnection) -> ProbedDeviceState:
    probe_result = probe_device_conn(connection)
    compatibility = compatibility_from_probe_result(probe_result)
    return ProbedDeviceState(probe_result=probe_result, compatibility=compatibility)


def require_connection_compatibility(connection: SshConnection) -> DeviceCompatibility:
    state = probe_connection_state(connection)
    return require_compatibility(
        state.compatibility,
        fallback_error=state.probe_result.error or "Failed to determine remote device OS compatibility.",
    )
