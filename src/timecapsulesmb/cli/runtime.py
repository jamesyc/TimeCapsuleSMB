from __future__ import annotations

import getpass
from dataclasses import dataclass
from pathlib import Path

from timecapsulesmb.core.config import AppConfig, ENV_PATH, parse_env_values, require_valid_config
from timecapsulesmb.device.compat import DeviceCompatibility, probe_device_compatibility
from timecapsulesmb.device.probe import remote_interface_exists


@dataclass(frozen=True)
class ResolvedConnection:
    host: str
    password: str
    ssh_opts: str


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
    require_airport_syap(values, command_name=command_name)
    require_valid_config(values, profile=profile)
    connection = resolve_env_connection(values)
    if not remote_interface_exists(connection.host, connection.password, connection.ssh_opts, values["TC_NET_IFACE"]):
        raise SystemExit(
            "TC_NET_IFACE is invalid. Run the `configure` command again.\n"
            "The configured network interface was not found on the device."
        )
    return connection


def probe_compatibility(connection: ResolvedConnection) -> DeviceCompatibility:
    return probe_device_compatibility(connection.host, connection.password, connection.ssh_opts)
