from __future__ import annotations

import getpass
from dataclasses import dataclass
from pathlib import Path

from timecapsulesmb.core.config import AppConfig, ENV_PATH, parse_env_values, require_valid_config
from timecapsulesmb.device.compat import DeviceCompatibility, compatibility_from_probe_result, require_compatibility
from timecapsulesmb.device.probe import (
    ProbedDeviceState,
    RemoteInterfaceProbeResult,
    probe_device,
    probe_remote_interface,
)


@dataclass(frozen=True)
class ResolvedConnection:
    host: str
    password: str
    ssh_opts: str


@dataclass(frozen=True)
class ManagedTargetState:
    connection: ResolvedConnection
    interface_probe: RemoteInterfaceProbeResult
    probe_state: ProbedDeviceState | None


def load_env_values(*, env_path: Path = ENV_PATH, defaults: dict[str, str] | None = None) -> dict[str, str]:
    resolved_defaults = defaults if defaults is not None else None
    return parse_env_values(env_path, defaults=resolved_defaults)


def load_validated_env(*, profile: str, env_path: Path = ENV_PATH, defaults: dict[str, str] | None = None) -> dict[str, str]:
    values = load_env_values(env_path=env_path, defaults=defaults)
    require_valid_config(values, profile=profile)
    return values


def require_airport_syap(values: dict[str, str], *, command_name: str) -> None:
    AppConfig(values).require(
        "TC_AIRPORT_SYAP",
        messageafter=f"\nPlease run the `configure` command before running `{command_name}`.",
    )


def resolve_ssh_credentials(
    values: dict[str, str],
    *,
    password_prompt: str = "Time Capsule root password: ",
    allow_empty_password: bool = False,
) -> tuple[str, str]:
    config = AppConfig(values)
    host = config.require("TC_HOST")
    password = config.get("TC_PASSWORD")
    if not password and not allow_empty_password:
        password = getpass.getpass(password_prompt)
    return host, password


def resolve_env_connection(
    values: dict[str, str],
    *,
    required_keys: tuple[str, ...] = (),
    allow_empty_password: bool = False,
) -> ResolvedConnection:
    config = AppConfig(values)
    for key in required_keys:
        config.require(key)
    host, password = resolve_ssh_credentials(values, allow_empty_password=allow_empty_password)
    return ResolvedConnection(host=host, password=password, ssh_opts=config.get("TC_SSH_OPTS"))


def resolve_validated_managed_connection(
    values: dict[str, str],
    *,
    command_name: str,
    profile: str,
) -> ResolvedConnection:
    return resolve_validated_managed_target(
        values,
        command_name=command_name,
        profile=profile,
        include_probe=False,
    ).connection


def inspect_managed_connection(
    connection: ResolvedConnection,
    iface: str,
    *,
    include_probe: bool = False,
) -> ManagedTargetState:
    interface_probe = probe_remote_interface(connection.host, connection.password, connection.ssh_opts, iface)
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
    probe_result = probe_device(host, password, ssh_opts)
    compatibility = compatibility_from_probe_result(probe_result)
    return ProbedDeviceState(probe_result=probe_result, compatibility=compatibility)


def probe_connection_state(connection: ResolvedConnection) -> ProbedDeviceState:
    return probe_device_state(connection.host, connection.password, connection.ssh_opts)


def require_connection_compatibility(connection: ResolvedConnection) -> DeviceCompatibility:
    state = probe_connection_state(connection)
    return require_compatibility(
        state.compatibility,
        fallback_error=state.probe_result.error or "Failed to determine remote device OS compatibility.",
    )
