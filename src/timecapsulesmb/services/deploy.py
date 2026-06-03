from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
import tempfile

from timecapsulesmb.core.config import DEFAULTS, MANAGED_PAYLOAD_DIR_NAME, AppConfig, parse_bool, shell_quote
from timecapsulesmb.core.messages import NETBSD4_REBOOT_FOLLOWUP
from timecapsulesmb.core.release import CLI_VERSION_CODE, RELEASE_TAG
from timecapsulesmb.deploy.artifact_resolver import resolve_payload_artifacts
from timecapsulesmb.deploy.artifacts import validate_artifacts
from timecapsulesmb.deploy.auth import render_smbpasswd
from timecapsulesmb.deploy.boot_assets import boot_asset_path
from timecapsulesmb.deploy.dry_run import (
    deployment_plan_to_jsonable as _deployment_plan_to_jsonable,
    format_deployment_plan as _format_deployment_plan,
)
from timecapsulesmb.deploy.executor import flush_remote_filesystem_writes, run_remote_actions, upload_deployment_payload
from timecapsulesmb.deploy.commands import RemoteAction, StopProcessAction
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
    DEFAULT_APPLE_MOUNT_WAIT_SECONDS,
    DEFAULT_DISKD_USE_VOLUME_ATTEMPTS,
    DEPLOY_STARTUP_ACTIVATE_NOW,
    DEPLOY_STARTUP_REBOOT_THEN_ACTIVATE,
    DEPLOY_STARTUP_REBOOT_THEN_VERIFY,
    DeploymentStartupMode,
    build_deployment_plan,
)
from timecapsulesmb.device.compat import (
    DeviceCompatibility,
    is_netbsd4_payload_family,
    payload_family_description,
    render_compatibility_message,
)
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
from timecapsulesmb.services.activation import decide_netbsd4_post_reboot_activation
from timecapsulesmb.services.callbacks import OperationCallbacks
from timecapsulesmb.services.reboot import RebootFlowError, request_reboot, request_reboot_and_wait
from timecapsulesmb.services.runtime import ManagedTargetState
from timecapsulesmb.services.runtime_verification import verify_managed_runtime_ready
from timecapsulesmb.transport.ssh import (
    SshConnection,
    local_scp_path,
    local_scp_supports_legacy_option,
    scp_upload_transport,
)


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


@dataclass(frozen=True)
class DeployCompletionMessages:
    activate_now_message: str = "Starting deployed runtime without reboot."
    activate_now_heading: str = "Waiting for managed runtime to finish starting..."
    activate_now_failure: str = "Managed runtime activation failed."
    post_reboot_activation_message: str = "Activating deployed runtime after reboot."
    netbsd4_autostart_message: str = "NetBSD4 firmware autostart is enabled; waiting for managed runtime."
    netbsd4_heading: str = "Waiting for managed runtime to finish starting..."
    netbsd4_failure: str = "NetBSD4 activation failed."
    reboot_request_message: str | None = None
    reboot_runtime_wait_message: str | None = None
    reboot_heading: str = "Waiting for managed runtime to finish starting..."
    reboot_failure: str = "Managed runtime did not become ready after reboot."


@dataclass(frozen=True)
class DeployCompletionResult:
    payload_dir: str
    payload_family: str
    is_netbsd4: bool
    rebooted: bool
    reboot_requested: bool
    waited: bool
    verified: bool
    message: str | None = None


@dataclass(frozen=True)
class DeployOptions:
    dry_run: bool
    no_reboot: bool
    no_wait: bool
    mount_wait_seconds: int = DEFAULT_APPLE_MOUNT_WAIT_SECONDS
    allow_unsupported: bool = False
    payload_dir_name: str = MANAGED_PAYLOAD_DIR_NAME

    @property
    def effective_no_wait(self) -> bool:
        return effective_no_wait_for_deploy(requested=self.no_wait, no_reboot=self.no_reboot)


@dataclass(frozen=True)
class DeployPreflight:
    payload_context: DeployPayloadContext
    artifacts: DeployArtifactPaths
    plan: DeploymentPlan

    @property
    def payload_family(self) -> str:
        return self.payload_context.payload_family

    @property
    def is_netbsd4(self) -> bool:
        return self.payload_context.is_netbsd4

    @property
    def startup_mode(self) -> DeploymentStartupMode:
        return self.payload_context.startup_mode

    @property
    def requires_reboot(self) -> bool:
        return bool(self.plan.reboot_required)


@dataclass(frozen=True)
class DeployServiceDependencies:
    validate_artifacts: Callable[..., object]
    resolve_payload_artifacts: Callable[..., object]
    build_deployment_plan: Callable[..., DeploymentPlan]
    wait_for_mast_volumes: Callable[..., MaStDiscoveryResult]
    select_payload_home: Callable[..., PayloadHomeSelection]
    run_remote_actions: Callable[..., object]
    render_flash_config: Callable[..., str]
    render_smbpasswd: Callable[..., tuple[str, str]]
    boot_asset_path: Callable[..., object]
    upload_deployment_payload: Callable[..., object]
    verify_payload_home: Callable[..., PayloadVerificationResult]
    flush_remote_writes: Callable[..., object]
    request_reboot: Callable[..., object]
    request_reboot_and_wait: Callable[..., object]
    decide_post_reboot_activation: Callable[..., object]
    verify_runtime: Callable[..., object]


class DeployArtifactValidationError(ValueError):
    """Raised when local deploy artifacts fail validation."""


def default_deploy_service_dependencies() -> DeployServiceDependencies:
    return DeployServiceDependencies(
        validate_artifacts=validate_artifacts,
        resolve_payload_artifacts=resolve_payload_artifacts,
        build_deployment_plan=build_deployment_plan,
        wait_for_mast_volumes=storage_service.wait_for_mast_volumes_conn,
        select_payload_home=select_payload_home_with_diagnostics_conn,
        run_remote_actions=run_remote_actions,
        render_flash_config=render_flash_runtime_config,
        render_smbpasswd=render_smbpasswd,
        boot_asset_path=boot_asset_path,
        upload_deployment_payload=upload_deployment_payload,
        verify_payload_home=verify_payload_home_conn,
        flush_remote_writes=flush_remote_filesystem_writes,
        request_reboot=request_reboot,
        request_reboot_and_wait=request_reboot_and_wait,
        decide_post_reboot_activation=decide_netbsd4_post_reboot_activation,
        verify_runtime=verify_managed_runtime_ready,
    )


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


def deployment_plan_to_jsonable(plan: DeploymentPlan) -> dict[str, object]:
    return _deployment_plan_to_jsonable(plan)


def format_deployment_plan(plan: DeploymentPlan) -> str:
    return _format_deployment_plan(plan)


def uploaded_file_message(transfer: FileTransfer) -> str | None:
    if transfer.source_id == BINARY_SMBD_SOURCE:
        return "Uploaded smbd."
    if transfer.source_id == BINARY_MDNS_SOURCE and transfer.mode == "flash_atomic":
        return "Uploaded mdns-advertiser."
    if transfer.source_id == BINARY_NBNS_SOURCE:
        return "Uploaded nbns-advertiser."
    if transfer.source_id == PACKAGED_DFREE_SH_SOURCE:
        return "Uploaded boot files."
    if transfer.source_id == GENERATED_FLASH_CONFIG_SOURCE:
        return "Uploaded runtime config."
    if transfer.source_id == GENERATED_USERNAME_MAP_SOURCE:
        return "Uploaded Samba account files."
    return None


def pre_upload_action_message(action: RemoteAction) -> str | None:
    if isinstance(action, StopProcessAction) and action.name == "nbns-advertiser":
        return "Cleaning up previous deployment files..."
    return None


def resolve_deploy_artifact_paths(
    distribution_root,
    payload_family: str,
    *,
    resolver=None,
    dependencies: DeployServiceDependencies | None = None,
) -> DeployArtifactPaths:
    if resolver is None:
        resolver = (dependencies or default_deploy_service_dependencies()).resolve_payload_artifacts
    resolved_artifacts = resolver(distribution_root, payload_family)
    return DeployArtifactPaths(
        smbd=resolved_artifacts["smbd"].absolute_path,
        mdns_advertiser=resolved_artifacts["mdns-advertiser"].absolute_path,
        nbns_advertiser=resolved_artifacts["nbns-advertiser"].absolute_path,
    )


def prepare_deploy_preflight(
    connection: SshConnection,
    target: ManagedTargetState,
    distribution_root,
    options: DeployOptions,
    *,
    callbacks: OperationCallbacks | None = None,
    dependencies: DeployServiceDependencies | None = None,
) -> DeployPreflight:
    callbacks = callbacks or OperationCallbacks()
    dependencies = dependencies or default_deploy_service_dependencies()

    callbacks.stage("validate_artifacts")
    failures = deploy_artifact_failures(distribution_root, validate=dependencies.validate_artifacts)
    if failures:
        raise DeployArtifactValidationError("; ".join(failures))

    callbacks.stage("check_compatibility")
    compatibility = require_supported_payload(target, allow_unsupported=options.allow_unsupported)
    payload_context = prepare_deploy_payload_context(
        connection,
        compatibility,
        no_reboot=options.no_reboot,
    )
    callbacks.update(deploy_startup_mode=payload_context.startup_mode)
    artifacts = resolve_deploy_artifact_paths(
        distribution_root,
        payload_context.payload_family,
        dependencies=dependencies,
    )
    plan = dependencies.build_deployment_plan(
        connection.host,
        build_dry_run_payload_home(options.payload_dir_name),
        artifacts.smbd,
        artifacts.mdns_advertiser,
        artifacts.nbns_advertiser,
        startup_mode=payload_context.startup_mode,
        apple_mount_wait_seconds=options.mount_wait_seconds,
        wait_after_reboot=not options.effective_no_wait,
    )
    return DeployPreflight(
        payload_context=payload_context,
        artifacts=artifacts,
        plan=plan,
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
        compatibility_message = render_compatibility_message(compatibility)
        if compatibility_message:
            raise DeviceError(
                f"{compatibility_message}\nNo deployable payload is available for this detected device."
            )
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
    resolver=None,
    wait_for_mast_volumes: Callable[..., MaStDiscoveryResult] | None = None,
    select_payload_home: Callable[..., PayloadHomeSelection] | None = None,
    build_plan=None,
    artifacts: DeployArtifactPaths | None = None,
    dependencies: DeployServiceDependencies | None = None,
) -> PreparedDeployPlan:
    dependencies = dependencies or default_deploy_service_dependencies()
    if artifacts is None:
        artifacts = resolve_deploy_artifact_paths(
            distribution_root,
            payload_context.payload_family,
            resolver=resolver,
            dependencies=dependencies,
        )
    payload_home = select_deploy_payload_home(
        connection,
        dry_run=dry_run,
        payload_dir_name=payload_dir_name,
        mount_wait_seconds=mount_wait_seconds,
        callbacks=callbacks,
        wait_for_mast_volumes=wait_for_mast_volumes or dependencies.wait_for_mast_volumes,
        select_payload_home=select_payload_home or dependencies.select_payload_home,
    )
    if callbacks is not None:
        callbacks.stage("build_deployment_plan")
    if build_plan is None:
        build_plan = dependencies.build_deployment_plan
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
    render_smbpasswd_func=None,
    boot_asset_path_func=None,
    dependencies: DeployServiceDependencies | None = None,
) -> Mapping[str, Path]:
    dependencies = dependencies or default_deploy_service_dependencies()
    if render_smbpasswd_func is None:
        render_smbpasswd_func = dependencies.render_smbpasswd
    if boot_asset_path_func is None:
        boot_asset_path_func = dependencies.boot_asset_path
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
    verify_payload_home=None,
    on_verified: Callable[[PayloadVerificationResult, bool], None] | None = None,
    dependencies: DeployServiceDependencies | None = None,
) -> None:
    if verify_payload_home is None:
        verify_payload_home = (dependencies or default_deploy_service_dependencies()).verify_payload_home
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
    run_remote_actions_func=None,
    render_flash_config_func=None,
    render_smbpasswd_func=None,
    boot_asset_path_func=None,
    upload_payload_func=None,
    verify_payload_home=None,
    flush_remote_writes=None,
    dependencies: DeployServiceDependencies | None = None,
) -> None:
    callbacks = callbacks or OperationCallbacks()
    dependencies = dependencies or default_deploy_service_dependencies()
    if run_remote_actions_func is None:
        run_remote_actions_func = dependencies.run_remote_actions
    if render_flash_config_func is None:
        render_flash_config_func = dependencies.render_flash_config
    if upload_payload_func is None:
        upload_payload_func = dependencies.upload_deployment_payload
    if flush_remote_writes is None:
        flush_remote_writes = dependencies.flush_remote_writes
    plan = prepared_plan.plan
    payload_home = prepared_plan.payload_home

    def update_scp_upload_telemetry() -> None:
        scp_path = local_scp_path()
        callbacks.update(
            local_scp_path=scp_path or "not_found",
            local_scp_legacy_option_supported=local_scp_supports_legacy_option(),
            remote_scp_available=connection.remote_has_scp if connection.remote_has_scp is not None else "unknown",
            upload_transport=scp_upload_transport(connection),
        )

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
            dependencies=dependencies,
        )
        if initial_upload_stage is not None:
            callbacks.stage(initial_upload_stage)
        update_scp_upload_telemetry()
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
        try:
            upload_payload_func(plan, **upload_kwargs)
        finally:
            update_scp_upload_telemetry()
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
        dependencies=dependencies,
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
        dependencies=dependencies,
    )


def _run_activation_actions_and_verify(
    connection: SshConnection,
    activation_actions: list[RemoteAction],
    *,
    callbacks: OperationCallbacks,
    activation_message: str,
    activation_stage: str,
    verification_stage: str,
    verification_timeout_seconds: int,
    verification_heading: str,
    failure_message: str,
    run_remote_actions_func=None,
    verify_runtime_func=None,
    dependencies: DeployServiceDependencies | None = None,
) -> None:
    dependencies = dependencies or default_deploy_service_dependencies()
    if run_remote_actions_func is None:
        run_remote_actions_func = dependencies.run_remote_actions
    if verify_runtime_func is None:
        verify_runtime_func = dependencies.verify_runtime
    callbacks.stage(activation_stage)
    callbacks.message(activation_message)
    run_remote_actions_func(connection, activation_actions)
    verify_runtime_func(
        connection,
        callbacks=callbacks,
        stage=verification_stage,
        timeout_seconds=verification_timeout_seconds,
        heading=verification_heading,
        failure_message=failure_message,
    )


def complete_deployment_after_upload(
    connection: SshConnection,
    prepared_plan: PreparedDeployPlan,
    *,
    no_wait: bool,
    callbacks: OperationCallbacks | None = None,
    messages: DeployCompletionMessages | None = None,
    run_remote_actions_func=None,
    request_reboot_func=None,
    request_reboot_and_wait_func=None,
    decide_post_reboot_activation=None,
    verify_runtime_func=None,
    dependencies: DeployServiceDependencies | None = None,
) -> DeployCompletionResult:
    callbacks = callbacks or OperationCallbacks()
    messages = messages or DeployCompletionMessages()
    dependencies = dependencies or default_deploy_service_dependencies()
    if run_remote_actions_func is None:
        run_remote_actions_func = dependencies.run_remote_actions
    if request_reboot_func is None:
        request_reboot_func = dependencies.request_reboot
    if request_reboot_and_wait_func is None:
        request_reboot_and_wait_func = dependencies.request_reboot_and_wait
    if decide_post_reboot_activation is None:
        decide_post_reboot_activation = dependencies.decide_post_reboot_activation
    if verify_runtime_func is None:
        verify_runtime_func = dependencies.verify_runtime
    plan = prepared_plan.plan
    payload_context = prepared_plan.payload_context
    payload_family = payload_context.payload_family
    is_netbsd4 = payload_context.is_netbsd4
    startup_mode = payload_context.startup_mode

    if startup_mode == DEPLOY_STARTUP_ACTIVATE_NOW:
        _run_activation_actions_and_verify(
            connection,
            plan.activation_actions,
            callbacks=callbacks,
            activation_message=messages.activate_now_message,
            activation_stage="activate_runtime",
            verification_stage="verify_runtime_activation",
            verification_timeout_seconds=200,
            verification_heading=messages.activate_now_heading,
            failure_message=messages.activate_now_failure,
            run_remote_actions_func=run_remote_actions_func,
            verify_runtime_func=verify_runtime_func,
            dependencies=dependencies,
        )
        return DeployCompletionResult(
            payload_dir=plan.payload_dir,
            payload_family=payload_family,
            is_netbsd4=is_netbsd4,
            rebooted=False,
            reboot_requested=False,
            waited=False,
            verified=True,
            message=activation_complete_message(is_netbsd4=is_netbsd4),
        )

    if no_wait:
        if messages.reboot_request_message:
            callbacks.message(messages.reboot_request_message)
        request_reboot_func(
            connection,
            strategy="ssh_shutdown_then_reboot",
            callbacks=callbacks,
            raise_on_request_error=True,
        )
        return DeployCompletionResult(
            payload_dir=plan.payload_dir,
            payload_family=payload_family,
            is_netbsd4=is_netbsd4,
            rebooted=False,
            reboot_requested=True,
            waited=False,
            verified=False,
        )

    if messages.reboot_request_message:
        callbacks.message(messages.reboot_request_message)
    request_reboot_and_wait_func(
        connection,
        strategy="ssh_shutdown_then_reboot",
        callbacks=callbacks,
        down_timeout_seconds=60,
        up_timeout_seconds=240,
        reboot_no_down_message=DEPLOY_REBOOT_NO_DOWN_MESSAGE,
        reboot_up_timeout_message=DEPLOY_REBOOT_UP_TIMEOUT_MESSAGE,
    )

    if startup_mode == DEPLOY_STARTUP_REBOOT_THEN_ACTIVATE:
        callbacks.stage("probe_runtime")
        decision = decide_post_reboot_activation(connection)
        callbacks.debug(
            activation_decision=decision.reason,
            manual_activation_required=decision.run_actions,
        )
        callbacks.message(decision.detail)
        if decision.run_actions:
            _run_activation_actions_and_verify(
                connection,
                plan.activation_actions,
                callbacks=callbacks,
                activation_message=messages.post_reboot_activation_message,
                activation_stage="post_reboot_activation",
                verification_stage="verify_runtime_activation",
                verification_timeout_seconds=200,
                verification_heading=messages.netbsd4_heading,
                failure_message=messages.netbsd4_failure,
                run_remote_actions_func=run_remote_actions_func,
                verify_runtime_func=verify_runtime_func,
                dependencies=dependencies,
            )
        else:
            callbacks.message(messages.netbsd4_autostart_message)
            verify_runtime_func(
                connection,
                callbacks=callbacks,
                stage="verify_runtime_activation",
                timeout_seconds=200,
                heading=messages.netbsd4_heading,
                failure_message=messages.netbsd4_failure,
            )
        return DeployCompletionResult(
            payload_dir=plan.payload_dir,
            payload_family=payload_family,
            is_netbsd4=True,
            rebooted=True,
            reboot_requested=True,
            waited=True,
            verified=True,
            message=activation_complete_message(is_netbsd4=is_netbsd4),
        )

    if messages.reboot_runtime_wait_message:
        callbacks.message(messages.reboot_runtime_wait_message)
    verify_runtime_func(
        connection,
        callbacks=callbacks,
        stage="verify_runtime_reboot",
        timeout_seconds=240,
        heading=messages.reboot_heading,
        failure_message=messages.reboot_failure,
    )
    return DeployCompletionResult(
        payload_dir=plan.payload_dir,
        payload_family=payload_family,
        is_netbsd4=is_netbsd4,
        rebooted=True,
        reboot_requested=True,
        waited=True,
        verified=True,
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
