from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
import tempfile

from timecapsulesmb.core.config import DEFAULTS, AppConfig, parse_bool, shell_quote
from timecapsulesmb.core.messages import NETBSD4_REBOOT_FOLLOWUP
from timecapsulesmb.core.release import CLI_VERSION_CODE, RELEASE_TAG
from timecapsulesmb.deploy.artifact_resolver import resolve_payload_artifacts
from timecapsulesmb.deploy.artifacts import validate_artifacts
from timecapsulesmb.deploy.auth import render_smbpasswd
from timecapsulesmb.deploy.boot_assets import boot_asset_path
from timecapsulesmb.deploy.executor import flush_remote_filesystem_writes, run_remote_actions, upload_deployment_payload
from timecapsulesmb.deploy.commands import RemoteAction
from timecapsulesmb.deploy.planner import (
    BINARY_MDNS_SOURCE,
    BINARY_NBNS_SOURCE,
    BINARY_SMBD_SOURCE,
    DeploymentPlan,
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
    build_deployment_plan,
)
from timecapsulesmb.device.compat import DeviceCompatibility, is_netbsd4_payload_family, render_compatibility_message
from timecapsulesmb.device.errors import DeviceError
from timecapsulesmb.device.storage import (
    MAST_DISCOVERY_ATTEMPTS,
    MAST_DISCOVERY_DELAY_SECONDS,
    MaStDiscoveryResult,
    PayloadHome,
    PayloadHomeSelection,
    PayloadVerificationResult,
    build_dry_run_payload_home,
    payload_candidate_checks_debug_summary,
    select_payload_home_with_diagnostics_conn,
    verify_payload_home_conn,
)
from timecapsulesmb.services import storage as storage_service
from timecapsulesmb.services.callbacks import OperationCallbacks
from timecapsulesmb.services.runtime import ManagedTargetState
from timecapsulesmb.transport.ssh import SshConnection


DEPLOY_REBOOT_UP_TIMEOUT_MESSAGE = (
    "Timed out waiting for SSH after reboot.\n\n"
    "The payload was uploaded and the reboot request succeeded, but the device did not accept SSH again "
    "before the 4 minute timeout. It may still be booting, or it may have come back with a different IP address.\n\n"
    "Next steps:\n"
    "  1. Wait a few more minutes.\n"
    "  2. If the device is reachable at a new IP, update TC_HOST or rerun configure.\n"
    "  3. Make sure you are connected to the same network/wifi as the device.\n"
    "  4. On NetBSD 4 devices, run `tcapsule activate` once SSH is reachable; "
    "deploy did not get far enough to activate Samba after reboot."
)
DEPLOY_REBOOT_NO_DOWN_MESSAGE = (
    "Reboot was requested but the device did not go down.\n"
    "The deploy stopped the managed runtime before reboot; power-cycle or rerun deploy."
)
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


@dataclass(frozen=True)
class DeployArtifactPaths:
    smbd: Path
    mdns_advertiser: Path
    nbns_advertiser: Path


@dataclass(frozen=True)
class PreparedDeployPlan:
    payload_context: DeployPayloadContext
    artifacts: DeployArtifactPaths
    payload_home: PayloadHome
    plan: DeploymentPlan


@dataclass(frozen=True)
class DeployRuntimeConfig:
    nbns_enabled: bool
    debug_logging: bool | None = None
    internal_share_use_disk_root: bool | None = None
    any_protocol: bool | None = None
    ata_idle_seconds: str | int | None = None
    ata_standby: str | int | None = None


def _best_effort_debug_summary(render, value: object) -> object | None:
    try:
        return render(value)
    except Exception:
        return None


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


def resolve_deploy_artifact_paths(
    distribution_root,
    payload_family: str,
    *,
    resolver=resolve_payload_artifacts,
) -> DeployArtifactPaths:
    resolved_artifacts = resolver(distribution_root, payload_family)
    return DeployArtifactPaths(
        smbd=resolved_artifacts["smbd"].absolute_path,
        mdns_advertiser=resolved_artifacts["mdns-advertiser"].absolute_path,
        nbns_advertiser=resolved_artifacts["nbns-advertiser"].absolute_path,
    )


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
    callbacks: OperationCallbacks | None = None,
    wait_for_mast_volumes: Callable[..., MaStDiscoveryResult] | None = None,
    select_payload_home: Callable[..., PayloadHomeSelection] | None = None,
) -> PayloadHome:
    callbacks = callbacks or OperationCallbacks()
    if dry_run:
        return build_dry_run_payload_home(payload_dir_name)

    mast_discovery = storage_service.wait_for_mast_volumes_with_diagnostics(
        connection,
        callbacks=callbacks,
        attempts=MAST_DISCOVERY_ATTEMPTS,
        delay_seconds=MAST_DISCOVERY_DELAY_SECONDS,
        wait_for_mast_volumes=wait_for_mast_volumes,
    )
    if not mast_discovery.volumes:
        raise DeviceError(
            no_mast_volumes_message(
                attempts=MAST_DISCOVERY_ATTEMPTS,
                delay_seconds=MAST_DISCOVERY_DELAY_SECONDS,
            )
        )

    callbacks.stage("select_payload_home")
    if select_payload_home is None:
        select_payload_home = select_payload_home_with_diagnostics_conn
    selection = select_payload_home(
        connection,
        mast_discovery.volumes,
        payload_dir_name,
        wait_seconds=mount_wait_seconds,
    )
    callbacks.debug(
        mast_candidate_checks=_best_effort_debug_summary(
            payload_candidate_checks_debug_summary,
            getattr(selection, "checks", ()),
        ),
    )
    if selection.payload_home is None:
        raise DeviceError(no_writable_mast_volumes_message(len(mast_discovery.volumes)))
    return selection.payload_home


def prepare_deployment_plan(
    connection: SshConnection,
    distribution_root,
    payload_context: DeployPayloadContext,
    *,
    dry_run: bool,
    payload_dir_name: str,
    mount_wait_seconds: int,
    wait_after_reboot: bool = True,
    callbacks: OperationCallbacks | None = None,
    resolver=resolve_payload_artifacts,
    wait_for_mast_volumes: Callable[..., MaStDiscoveryResult] | None = None,
    select_payload_home: Callable[..., PayloadHomeSelection] | None = None,
    build_plan=build_deployment_plan,
) -> PreparedDeployPlan:
    artifacts = resolve_deploy_artifact_paths(
        distribution_root,
        payload_context.payload_family,
        resolver=resolver,
    )
    payload_home = select_deploy_payload_home(
        connection,
        dry_run=dry_run,
        payload_dir_name=payload_dir_name,
        mount_wait_seconds=mount_wait_seconds,
        callbacks=callbacks,
        wait_for_mast_volumes=wait_for_mast_volumes,
        select_payload_home=select_payload_home,
    )
    if callbacks is not None:
        callbacks.stage("build_deployment_plan")
    plan = build_plan(
        connection.host,
        payload_home,
        artifacts.smbd,
        artifacts.mdns_advertiser,
        artifacts.nbns_advertiser,
        startup_mode=payload_context.startup_mode,
        apple_mount_wait_seconds=mount_wait_seconds,
        wait_after_reboot=wait_after_reboot,
    )
    if callbacks is not None:
        callbacks.debug(
            payload_volume_root=plan.volume_root,
            payload_device_path=plan.device_path,
            payload_dir=plan.payload_dir,
        )
    return PreparedDeployPlan(
        payload_context=payload_context,
        artifacts=artifacts,
        payload_home=payload_home,
        plan=plan,
    )


def _deployment_upload_sources(
    plan: DeploymentPlan,
    password: str,
    flash_config_text: str,
    tmpdir: Path,
    boot_assets: ExitStack,
    *,
    render_smbpasswd_func=render_smbpasswd,
    boot_asset_path_func=boot_asset_path,
) -> Mapping[str, Path]:
    generated_flash_config = tmpdir / "tcapsulesmb.conf"
    generated_smbpasswd = tmpdir / "smbpasswd"
    generated_username_map = tmpdir / "username.map"
    generated_flash_config.write_text(flash_config_text)
    smbpasswd_text, username_map_text = render_smbpasswd_func(password)
    generated_smbpasswd.write_text(smbpasswd_text)
    generated_username_map.write_text(username_map_text)
    return {
        BINARY_SMBD_SOURCE: plan.smbd_path,
        BINARY_MDNS_SOURCE: plan.mdns_path,
        BINARY_NBNS_SOURCE: plan.nbns_path,
        GENERATED_SMBPASSWD_SOURCE: generated_smbpasswd,
        GENERATED_USERNAME_MAP_SOURCE: generated_username_map,
        GENERATED_FLASH_CONFIG_SOURCE: generated_flash_config,
        PACKAGED_RC_LOCAL_SOURCE: boot_assets.enter_context(boot_asset_path_func("rc.local")),
        PACKAGED_COMMON_SH_SOURCE: boot_assets.enter_context(boot_asset_path_func("common.sh")),
        PACKAGED_DFREE_SH_SOURCE: boot_assets.enter_context(boot_asset_path_func("dfree.sh")),
        PACKAGED_BOOT_SOURCE: boot_assets.enter_context(boot_asset_path_func("boot.sh")),
        PACKAGED_MANAGER_SOURCE: boot_assets.enter_context(boot_asset_path_func("manager.sh")),
    }


def _verify_deployed_payload(
    callbacks: OperationCallbacks,
    connection: SshConnection,
    payload_home: PayloadHome,
    *,
    wait_seconds: int,
    post_sync: bool,
    verify_payload_home=verify_payload_home_conn,
    on_verified: Callable[[PayloadVerificationResult, bool], None] | None = None,
) -> None:
    callbacks.stage("verify_payload_upload_after_sync" if post_sync else "verify_payload_upload")
    verification = verify_payload_home(connection, payload_home, wait_seconds=wait_seconds)
    callbacks.debug(
        **{"payload_post_sync_verification" if post_sync else "payload_upload_verification": verification.detail}
    )
    if on_verified is not None:
        on_verified(verification, post_sync)
    if not verification.ok:
        raise DeviceError(payload_verification_error(payload_home, verification))


def upload_and_verify_deployment_payload(
    config: AppConfig,
    connection: SshConnection,
    prepared_plan: PreparedDeployPlan,
    runtime_config: DeployRuntimeConfig,
    *,
    callbacks: OperationCallbacks | None = None,
    initial_upload_stage: str | None = "upload_payload",
    on_pre_upload_action_done: Callable[[RemoteAction, int, int], None] | None = None,
    on_before_upload: Callable[[], None] | None = None,
    on_after_upload: Callable[[], None] | None = None,
    on_uploaded: Callable[[FileTransfer], None] | None = None,
    on_uploading: Callable[[FileTransfer], None] | None = None,
    on_before_post_upload_actions: Callable[[], None] | None = None,
    on_before_verify: Callable[[bool], None] | None = None,
    on_before_flush: Callable[[], None] | None = None,
    on_verified: Callable[[PayloadVerificationResult, bool], None] | None = None,
    run_remote_actions_func=run_remote_actions,
    render_flash_config_func=None,
    render_smbpasswd_func=render_smbpasswd,
    boot_asset_path_func=boot_asset_path,
    upload_payload_func=upload_deployment_payload,
    verify_payload_home=verify_payload_home_conn,
    flush_remote_writes=flush_remote_filesystem_writes,
) -> None:
    callbacks = callbacks or OperationCallbacks()
    plan = prepared_plan.plan
    payload_home = prepared_plan.payload_home
    if render_flash_config_func is None:
        render_flash_config_func = render_flash_runtime_config

    callbacks.stage("pre_upload_actions")
    run_remote_actions_func(connection, plan.pre_upload_actions, on_action_done=on_pre_upload_action_done)
    callbacks.stage("prepare_deployment_files")
    flash_config_text = render_flash_config_func(
        config,
        payload_home,
        nbns_enabled=runtime_config.nbns_enabled,
        debug_logging=runtime_config.debug_logging,
        internal_share_use_disk_root=runtime_config.internal_share_use_disk_root,
        any_protocol=runtime_config.any_protocol,
        ata_idle_seconds=runtime_config.ata_idle_seconds,
        ata_standby=runtime_config.ata_standby,
    )
    with tempfile.TemporaryDirectory(prefix="tc-deploy-") as tmp, ExitStack() as boot_assets:
        upload_sources = _deployment_upload_sources(
            plan,
            connection.password,
            flash_config_text,
            Path(tmp),
            boot_assets,
            render_smbpasswd_func=render_smbpasswd_func,
            boot_asset_path_func=boot_asset_path_func,
        )
        if initial_upload_stage is not None:
            callbacks.stage(initial_upload_stage)
        if on_before_upload is not None:
            on_before_upload()
        upload_kwargs: dict[str, object] = {
            "connection": connection,
            "source_resolver": upload_sources,
        }
        if on_uploaded is not None:
            upload_kwargs["on_uploaded"] = on_uploaded
        if on_uploading is not None:
            upload_kwargs["on_uploading"] = on_uploading
        upload_payload_func(plan, **upload_kwargs)
        if on_after_upload is not None:
            on_after_upload()

    callbacks.stage("post_upload_actions")
    if on_before_post_upload_actions is not None:
        on_before_post_upload_actions()
    run_remote_actions_func(connection, plan.post_upload_actions)
    if on_before_verify is not None:
        on_before_verify(False)
    _verify_deployed_payload(
        callbacks,
        connection,
        payload_home,
        wait_seconds=plan.apple_mount_wait_seconds,
        post_sync=False,
        verify_payload_home=verify_payload_home,
        on_verified=on_verified,
    )
    callbacks.stage("flush_payload_upload")
    if on_before_flush is not None:
        on_before_flush()
    flush_remote_writes(connection)
    if on_before_verify is not None:
        on_before_verify(True)
    _verify_deployed_payload(
        callbacks,
        connection,
        payload_home,
        wait_seconds=plan.apple_mount_wait_seconds,
        post_sync=True,
        verify_payload_home=verify_payload_home,
        on_verified=on_verified,
    )


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
