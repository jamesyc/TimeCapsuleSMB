from __future__ import annotations

from timecapsulesmb.core.config import DEFAULTS, AppConfig, parse_bool, shell_quote
from timecapsulesmb.core.messages import NETBSD4_REBOOT_FOLLOWUP
from timecapsulesmb.core.release import CLI_VERSION_CODE, RELEASE_TAG
from timecapsulesmb.deploy.planner import (
    DEFAULT_DISKD_USE_VOLUME_ATTEMPTS,
    DEPLOY_STARTUP_ACTIVATE_NOW,
    DEPLOY_STARTUP_REBOOT_THEN_ACTIVATE,
    DEPLOY_STARTUP_REBOOT_THEN_VERIFY,
    DeploymentStartupMode,
)
from timecapsulesmb.device.storage import PayloadHome, PayloadVerificationResult


DEPLOY_REBOOT_UP_TIMEOUT_MESSAGE = "Timed out waiting for SSH after reboot."
DEPLOY_REBOOT_NO_DOWN_MESSAGE = (
    "Reboot was requested but the device did not go down.\n"
    "The deploy stopped the managed runtime before reboot; power-cycle or rerun deploy."
)


def no_mast_volumes_message(*, attempts: int, delay_seconds: int) -> str:
    return (
        f"No deployable HFS disk was found after {attempts} MaSt queries "
        f"spaced {delay_seconds} seconds apart."
    )


def no_writable_mast_volumes_message(volume_count: int) -> str:
    return f"MaSt found {volume_count} deployable HFS volume(s), but deploy could not write to any of them."


def payload_verification_error(payload_home: PayloadHome, result: PayloadVerificationResult) -> str:
    return f"managed payload verification failed at {payload_home.payload_dir}: {result.detail}"


def startup_mode_for_deploy(*, no_reboot: bool, is_netbsd4: bool) -> DeploymentStartupMode:
    if no_reboot:
        return DEPLOY_STARTUP_ACTIVATE_NOW
    if is_netbsd4:
        return DEPLOY_STARTUP_REBOOT_THEN_ACTIVATE
    return DEPLOY_STARTUP_REBOOT_THEN_VERIFY


def activation_complete_message(*, is_netbsd4: bool) -> str:
    if is_netbsd4:
        return f"NetBSD4 activation complete. {NETBSD4_REBOOT_FOLLOWUP}"
    return "Runtime activation complete."


def _render_flash_config_assignment(key: str, value: str | int) -> str:
    if isinstance(value, int):
        return f"{key}={value}"
    return f"{key}={shell_quote(value)}"


def _runtime_unsigned_config_value(config: AppConfig, key: str, default: str) -> str:
    raw_value = config.get(key, default).strip()
    if raw_value == "":
        raw_value = default
    if raw_value == "":
        return ""
    if not raw_value.isdigit():
        raise ValueError(f"{key} must be a non-negative integer")
    return str(int(raw_value))


def _runtime_unsigned_override_value(value: str | int) -> str | int:
    if isinstance(value, int):
        if value < 0:
            raise ValueError("runtime setting override must be a non-negative integer")
        return value
    raw_value = value.strip()
    if raw_value == "":
        return ""
    if not raw_value.isdigit():
        raise ValueError("runtime setting override must be a non-negative integer")
    return str(int(raw_value))


def render_flash_runtime_config(
    config: AppConfig,
    payload_home: PayloadHome,
    *,
    nbns_enabled: bool,
    debug_logging: bool | None = None,
    internal_share_use_disk_root: bool | None = None,
    any_protocol: bool | None = None,
    ata_idle_seconds: str | int | None = None,
    ata_standby: str | int | None = None,
    diskd_use_volume_attempts: int = DEFAULT_DISKD_USE_VOLUME_ATTEMPTS,
) -> str:
    internal_root_default = config.get("TC_INTERNAL_SHARE_USE_DISK_ROOT", DEFAULTS["TC_INTERNAL_SHARE_USE_DISK_ROOT"])
    any_protocol_default = config.get("TC_ANY_PROTOCOL", DEFAULTS["TC_ANY_PROTOCOL"])
    configured_debug_logging = config.get("TC_DEBUG_LOGGING", DEFAULTS["TC_DEBUG_LOGGING"])
    runtime_ata_idle_seconds = (
        _runtime_unsigned_config_value(config, "TC_ATA_IDLE_SECONDS", DEFAULTS["TC_ATA_IDLE_SECONDS"])
        if ata_idle_seconds is None
        else _runtime_unsigned_override_value(ata_idle_seconds)
    )
    runtime_ata_standby = (
        _runtime_unsigned_config_value(config, "TC_ATA_STANDBY", DEFAULTS["TC_ATA_STANDBY"])
        if ata_standby is None
        else _runtime_unsigned_override_value(ata_standby)
    )
    effective_internal_root = (
        parse_bool(internal_root_default)
        if internal_share_use_disk_root is None
        else internal_share_use_disk_root
    )
    effective_any_protocol = (
        parse_bool(any_protocol_default)
        if any_protocol is None
        else any_protocol
    )
    effective_debug_logging = parse_bool(configured_debug_logging) if debug_logging is None else debug_logging

    values: list[tuple[str, str | int]] = [
        ("TC_CONFIG_VERSION", 2),
        ("TC_DEPLOY_RELEASE_TAG", RELEASE_TAG),
        ("TC_DEPLOY_CLI_VERSION_CODE", CLI_VERSION_CODE),
        ("INTERNAL_SHARE_USE_DISK_ROOT", 1 if effective_internal_root else 0),
        ("ANY_PROTOCOL", 1 if effective_any_protocol else 0),
        ("DISKD_USE_VOLUME_ATTEMPTS", diskd_use_volume_attempts),
        ("ATA_IDLE_SECONDS", runtime_ata_idle_seconds),
        ("ATA_STANDBY", runtime_ata_standby),
        ("NBNS_ENABLED", 1 if nbns_enabled else 0),
        ("SMBD_DEBUG_LOGGING", 1 if effective_debug_logging else 0),
        ("MDNS_DEBUG_LOGGING", 1 if effective_debug_logging else 0),
    ]
    return "\n".join(_render_flash_config_assignment(key, value) for key, value in values) + "\n"
