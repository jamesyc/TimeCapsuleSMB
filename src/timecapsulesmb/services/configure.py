from __future__ import annotations

import math

from timecapsulesmb.configure_defaults import valid_existing_config_value
from timecapsulesmb.core.config import DEFAULTS, parse_bool, preserved_env_file_values


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
