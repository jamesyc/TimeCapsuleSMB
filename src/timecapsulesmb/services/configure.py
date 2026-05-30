from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
from typing import Callable, Mapping

from timecapsulesmb.configure_defaults import valid_existing_config_value
from timecapsulesmb.core.config import DEFAULTS, parse_bool, preserved_env_file_values, write_env_file
from timecapsulesmb.core.net import extract_host
from timecapsulesmb.device.probe import ProbedDeviceState, probe_connection_state
from timecapsulesmb.integrations.acp import ACPAuthError, ACPError, enable_ssh
from timecapsulesmb.services.runtime import wait_for_tcp_port_state
from timecapsulesmb.transport.ssh import SshConnection


@dataclass(frozen=True)
class ConfigureEnableSshCallbacks:
    set_stage: Callable[[str], None] | None = None
    log: Callable[[str], None] | None = None
    add_debug_fields: Callable[..., None] | None = None
    update_fields: Callable[..., None] | None = None


def _call_fields(callback: Callable[..., None] | None, **fields: object) -> None:
    if callback is not None:
        callback(**fields)


def _call_stage(callback: Callable[[str], None] | None, stage: str) -> None:
    if callback is not None:
        callback(stage)


def _call_log(callback: Callable[[str], None] | None, message: str) -> None:
    if callback is not None:
        callback(message)


def enable_ssh_and_reprobe(
    connection: SshConnection,
    *,
    timeout_seconds: int = 180,
    verbose_wait: bool = True,
    callbacks: ConfigureEnableSshCallbacks | None = None,
) -> ProbedDeviceState | None:
    callbacks = callbacks or ConfigureEnableSshCallbacks()
    host = extract_host(connection.host)
    _call_fields(
        callbacks.add_debug_fields,
        configure_acp_enable_attempted=True,
        ssh_initially_reachable=False,
    )
    _call_log(callbacks.log, "\nSSH is not reachable. Attempting to enable SSH on the device...")
    _call_stage(callbacks.set_stage, "acp_enable_ssh")
    try:
        enable_ssh(host, connection.password, reboot_device=True, log=callbacks.log)
    except ACPAuthError:
        _call_fields(
            callbacks.add_debug_fields,
            configure_acp_enable_succeeded=False,
            configure_retry_reason="acp_authentication_failed",
        )
        raise
    except ACPError:
        _call_fields(callbacks.add_debug_fields, configure_acp_enable_succeeded=False)
        raise

    _call_fields(callbacks.add_debug_fields, configure_acp_enable_succeeded=True)
    _call_stage(callbacks.set_stage, "wait_for_ssh_after_acp")
    if not wait_for_tcp_port_state(
        host,
        22,
        expected_state=True,
        timeout_seconds=timeout_seconds,
        service_name="SSH port",
        log=callbacks.log if verbose_wait else None,
    ):
        _call_fields(callbacks.update_fields, ssh_final_reachable=False)
        return None

    _call_fields(callbacks.update_fields, ssh_final_reachable=True)
    _call_stage(callbacks.set_stage, "ssh_probe_after_acp")
    return probe_connection_state(connection)


def _optional_unsigned_config_value(value: object, key: str) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        raise ValueError(f"{key} must be a non-negative integer")
    if isinstance(value, int):
        if value < 0:
            raise ValueError(f"{key} must be a non-negative integer")
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer() or value < 0:
            raise ValueError(f"{key} must be a non-negative integer")
        return str(int(value))
    raw_value = str(value).strip()
    if raw_value == "":
        return ""
    if not raw_value.isdigit():
        raise ValueError(f"{key} must be a non-negative integer")
    return str(int(raw_value))


def _existing_unsigned_config_value_or_default(existing: dict[str, str], key: str, label: str) -> str:
    return valid_existing_config_value(existing, key, label) or DEFAULTS[key]


def build_configure_env_values(
    existing: dict[str, str],
    *,
    host: str,
    password: str,
    ssh_opts: str,
    configure_id: str,
    internal_share_use_disk_root: bool | None = None,
    any_protocol: bool | None = None,
    debug_logging: bool | None = None,
    ata_idle_seconds: object | None = None,
    ata_standby: object | None = None,
) -> dict[str, str]:
    values = preserved_env_file_values(existing)
    values.update({
        "TC_HOST": host,
        "TC_PASSWORD": password,
        "TC_SSH_OPTS": ssh_opts,
        "TC_INTERNAL_SHARE_USE_DISK_ROOT": "true" if (
            parse_bool(existing.get("TC_INTERNAL_SHARE_USE_DISK_ROOT", DEFAULTS["TC_INTERNAL_SHARE_USE_DISK_ROOT"]))
            if internal_share_use_disk_root is None
            else internal_share_use_disk_root
        ) else "false",
        "TC_ANY_PROTOCOL": "true" if (
            parse_bool(existing.get("TC_ANY_PROTOCOL", DEFAULTS["TC_ANY_PROTOCOL"]))
            if any_protocol is None
            else any_protocol
        ) else "false",
        "TC_DEBUG_LOGGING": "true" if (
            parse_bool(existing.get("TC_DEBUG_LOGGING", DEFAULTS["TC_DEBUG_LOGGING"]))
            if debug_logging is None
            else debug_logging
        ) else "false",
        "TC_ATA_IDLE_SECONDS": (
            _existing_unsigned_config_value_or_default(existing, "TC_ATA_IDLE_SECONDS", "ATA idle seconds")
            if ata_idle_seconds is None
            else _optional_unsigned_config_value(ata_idle_seconds, "TC_ATA_IDLE_SECONDS")
        ),
        "TC_ATA_STANDBY": (
            _existing_unsigned_config_value_or_default(existing, "TC_ATA_STANDBY", "ATA standby timer")
            if ata_standby is None
            else _optional_unsigned_config_value(ata_standby, "TC_ATA_STANDBY")
        ),
        "TC_CONFIGURE_ID": configure_id,
    })
    return values


def write_configure_env_file(path: Path, values: Mapping[str, str], *, persist_password: bool) -> None:
    output = dict(values)
    if not persist_password:
        output.pop("TC_PASSWORD", None)
    write_env_file(path, output)
