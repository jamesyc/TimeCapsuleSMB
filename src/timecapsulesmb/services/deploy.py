from __future__ import annotations

from dataclasses import dataclass

from timecapsulesmb.core.config import DEFAULTS, AppConfig, parse_bool, shell_quote
from timecapsulesmb.core.messages import NETBSD4_REBOOT_FOLLOWUP
from timecapsulesmb.core.release import CLI_VERSION_CODE, RELEASE_TAG
from timecapsulesmb.deploy.artifacts import validate_artifacts
from timecapsulesmb.deploy.planner import (
    BINARY_MDNS_SOURCE,
    BINARY_NBNS_SOURCE,
    BINARY_SMBD_SOURCE,
    GENERATED_FLASH_CONFIG_SOURCE,
    GENERATED_SMBPASSWD_SOURCE,
    GENERATED_USERNAME_MAP_SOURCE,
    PACKAGED_BOOT_SOURCE,
    PACKAGED_COMMON_SH_SOURCE,
    PACKAGED_DFREE_SH_SOURCE,
    PACKAGED_MANAGER_SOURCE,
    PACKAGED_RC_LOCAL_SOURCE,
    FileTransfer,
)
from timecapsulesmb.deploy.planner import (
    DEFAULT_DISKD_USE_VOLUME_ATTEMPTS,
    DEPLOY_STARTUP_ACTIVATE_NOW,
    DEPLOY_STARTUP_REBOOT_THEN_ACTIVATE,
    DEPLOY_STARTUP_REBOOT_THEN_VERIFY,
    DeploymentStartupMode,
)
from timecapsulesmb.device.compat import DeviceCompatibility, is_netbsd4_payload_family, render_compatibility_message
from timecapsulesmb.device.errors import DeviceError
from timecapsulesmb.device.storage import (
    MAST_DISCOVERY_ATTEMPTS,
    MAST_DISCOVERY_DELAY_SECONDS,
    PayloadHome,
    PayloadVerificationResult,
    build_dry_run_payload_home,
    mast_volumes_debug_summary,
    payload_candidate_checks_debug_summary,
    select_payload_home_with_diagnostics_conn,
    wait_for_mast_volumes_conn,
)
from timecapsulesmb.services.runtime import ManagedTargetState, RuntimeOperationCallbacks
from timecapsulesmb.transport.ssh import SshConnection


DEPLOY_REBOOT_UP_TIMEOUT_MESSAGE = "Timed out waiting for SSH after reboot."
DEPLOY_REBOOT_NO_DOWN_MESSAGE = (
    "Reboot was requested but the device did not go down.\n"
    "The deploy stopped the managed runtime before reboot; power-cycle or rerun deploy."
)
MAST_ACP_OUTPUT_DEBUG_LIMIT = 8192
DEPLOY_UPLOAD_BOOT_SOURCES = frozenset({
    PACKAGED_RC_LOCAL_SOURCE,
    PACKAGED_COMMON_SH_SOURCE,
    PACKAGED_DFREE_SH_SOURCE,
    PACKAGED_BOOT_SOURCE,
    PACKAGED_MANAGER_SOURCE,
})
DEPLOY_UPLOAD_ACCOUNT_SOURCES = frozenset({
    GENERATED_SMBPASSWD_SOURCE,
    GENERATED_USERNAME_MAP_SOURCE,
})


@dataclass(frozen=True)
class DeployPayloadContext:
    compatibility: DeviceCompatibility
    payload_family: str
    is_netbsd4: bool
    startup_mode: DeploymentStartupMode


def _call_stage(callbacks: RuntimeOperationCallbacks | None, stage: str) -> None:
    if callbacks is not None and callbacks.set_stage is not None:
        callbacks.set_stage(stage)


def _call_debug(callbacks: RuntimeOperationCallbacks | None, **fields: object) -> None:
    if callbacks is not None and callbacks.add_debug_fields is not None:
        callbacks.add_debug_fields(**fields)


def _best_effort_debug_summary(render, value: object) -> object | None:
    try:
        return render(value)
    except Exception:
        return None


def _mast_acp_output_debug_text(raw_output: str) -> str:
    if not raw_output:
        return "<empty>"
    if len(raw_output) <= MAST_ACP_OUTPUT_DEBUG_LIMIT:
        return raw_output
    omitted = len(raw_output) - MAST_ACP_OUTPUT_DEBUG_LIMIT
    return f"{raw_output[:MAST_ACP_OUTPUT_DEBUG_LIMIT]}...<truncated {omitted} chars>"


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


def effective_no_wait_for_deploy(*, requested: bool, no_reboot: bool) -> bool:
    return False if no_reboot else requested


def deploy_upload_stage(transfer: FileTransfer) -> str:
    if transfer.source_id == BINARY_SMBD_SOURCE:
        return "upload_smbd"
    if transfer.source_id == BINARY_MDNS_SOURCE:
        return "upload_mdns_advertiser"
    if transfer.source_id == BINARY_NBNS_SOURCE:
        return "upload_nbns_advertiser"
    if transfer.source_id in DEPLOY_UPLOAD_BOOT_SOURCES:
        return "upload_boot_files"
    if transfer.source_id == GENERATED_FLASH_CONFIG_SOURCE:
        return "upload_runtime_config"
    if transfer.source_id in DEPLOY_UPLOAD_ACCOUNT_SOURCES:
        return "upload_samba_accounts"
    return "upload_payload"


def deploy_artifact_failures(distribution_root, *, validate=validate_artifacts) -> list[str]:
    return [message for _, ok, message in validate(distribution_root) if not ok]


def require_supported_payload(target: ManagedTargetState, *, allow_unsupported: bool) -> DeviceCompatibility:
    probe_state = target.probe_state
    if probe_state is None:
        raise DeviceError("Failed to determine remote device OS compatibility.")
    compatibility = probe_state.compatibility
    if compatibility is None:
        raise DeviceError(probe_state.probe_result.error or "Failed to determine remote device OS compatibility.")
    if not compatibility.supported and not allow_unsupported:
        raise DeviceError(render_compatibility_message(compatibility))
    if not compatibility.payload_family:
        raise DeviceError("No deployable payload is available for this detected device.")
    return compatibility


def prepare_deploy_payload_context(
    connection: SshConnection,
    compatibility: DeviceCompatibility,
    *,
    no_reboot: bool,
) -> DeployPayloadContext:
    if not compatibility.payload_family:
        raise DeviceError("No deployable payload is available for this detected device.")
    payload_family = compatibility.payload_family
    is_netbsd4 = is_netbsd4_payload_family(payload_family)
    if is_netbsd4:
        # Apple NetBSD 4 firmware can expose /usr/bin/scp but hang after
        # writing the file. Use the SSH pipe upload fallback consistently.
        connection.remote_has_scp = False
    return DeployPayloadContext(
        compatibility=compatibility,
        payload_family=payload_family,
        is_netbsd4=is_netbsd4,
        startup_mode=startup_mode_for_deploy(no_reboot=no_reboot, is_netbsd4=is_netbsd4),
    )


def select_deploy_payload_home(
    connection: SshConnection,
    *,
    dry_run: bool,
    payload_dir_name: str,
    mount_wait_seconds: int,
    callbacks: RuntimeOperationCallbacks | None = None,
    wait_for_mast_volumes=wait_for_mast_volumes_conn,
    select_payload_home=select_payload_home_with_diagnostics_conn,
) -> PayloadHome:
    if dry_run:
        return build_dry_run_payload_home(payload_dir_name)

    _call_stage(callbacks, "read_mast")
    mast_discovery = wait_for_mast_volumes(
        connection,
        attempts=MAST_DISCOVERY_ATTEMPTS,
        delay_seconds=MAST_DISCOVERY_DELAY_SECONDS,
    )
    debug_fields: dict[str, object] = {
        "mast_read_attempts": mast_discovery.attempts,
        "mast_volume_count": len(mast_discovery.volumes),
        "mast_candidates": _best_effort_debug_summary(mast_volumes_debug_summary, mast_discovery.volumes),
    }
    if not mast_discovery.volumes:
        debug_fields["mast_acp_output_chars"] = len(mast_discovery.raw_output)
        debug_fields["mast_acp_output"] = _mast_acp_output_debug_text(mast_discovery.raw_output)
    _call_debug(callbacks, **debug_fields)
    if not mast_discovery.volumes:
        raise DeviceError(
            no_mast_volumes_message(
                attempts=MAST_DISCOVERY_ATTEMPTS,
                delay_seconds=MAST_DISCOVERY_DELAY_SECONDS,
            )
        )

    _call_stage(callbacks, "select_payload_home")
    selection = select_payload_home(
        connection,
        mast_discovery.volumes,
        payload_dir_name,
        wait_seconds=mount_wait_seconds,
    )
    _call_debug(
        callbacks,
        mast_candidate_checks=_best_effort_debug_summary(
            payload_candidate_checks_debug_summary,
            getattr(selection, "checks", ()),
        ),
    )
    if selection.payload_home is None:
        raise DeviceError(no_writable_mast_volumes_message(len(mast_discovery.volumes)))
    return selection.payload_home


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
