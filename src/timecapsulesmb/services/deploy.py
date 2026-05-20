from __future__ import annotations

from timecapsulesmb.core.config import DEFAULTS, AppConfig, parse_bool, shell_quote
from timecapsulesmb.core.release import CLI_VERSION_CODE, RELEASE_TAG
from timecapsulesmb.deploy.planner import DEFAULT_ATA_IDLE_SECONDS, DEFAULT_DISKD_USE_VOLUME_ATTEMPTS
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


def _render_flash_config_assignment(key: str, value: str | int) -> str:
    if isinstance(value, int):
        return f"{key}={value}"
    return f"{key}={shell_quote(value)}"


def render_flash_runtime_config(
    config: AppConfig,
    payload_home: PayloadHome,
    *,
    nbns_enabled: bool,
    debug_logging: bool,
    ata_idle_seconds: int = DEFAULT_ATA_IDLE_SECONDS,
    diskd_use_volume_attempts: int = DEFAULT_DISKD_USE_VOLUME_ATTEMPTS,
) -> str:
    internal_root_default = config.get("TC_INTERNAL_SHARE_USE_DISK_ROOT", DEFAULTS["TC_INTERNAL_SHARE_USE_DISK_ROOT"])
    any_protocol_default = config.get("TC_ANY_PROTOCOL", DEFAULTS["TC_ANY_PROTOCOL"])
    configured_debug_logging = config.get("TC_DEBUG_LOGGING", DEFAULTS["TC_DEBUG_LOGGING"])
    effective_debug_logging = debug_logging or parse_bool(configured_debug_logging)

    values: list[tuple[str, str | int]] = [
        ("TC_CONFIG_VERSION", 2),
        ("TC_DEPLOY_RELEASE_TAG", RELEASE_TAG),
        ("TC_DEPLOY_CLI_VERSION_CODE", CLI_VERSION_CODE),
        ("INTERNAL_SHARE_USE_DISK_ROOT", 1 if parse_bool(internal_root_default) else 0),
        ("ANY_PROTOCOL", 1 if parse_bool(any_protocol_default) else 0),
        ("DISKD_USE_VOLUME_ATTEMPTS", diskd_use_volume_attempts),
        ("ATA_IDLE_SECONDS", ata_idle_seconds),
        ("NBNS_ENABLED", 1 if nbns_enabled else 0),
        ("SMBD_DEBUG_LOGGING", 1 if effective_debug_logging else 0),
        ("MDNS_DEBUG_LOGGING", 1 if effective_debug_logging else 0),
    ]
    return "\n".join(_render_flash_config_assignment(key, value) for key, value in values) + "\n"
