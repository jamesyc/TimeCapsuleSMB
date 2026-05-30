from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
import tempfile

from timecapsulesmb.app.context import AppOperationContext
from timecapsulesmb.app.contracts import deploy_plan_payload, deploy_result_payload
from timecapsulesmb.app.confirmations import build_confirmation, require_confirmation
from timecapsulesmb.core.config import (
    DEFAULTS,
    MANAGED_PAYLOAD_DIR_NAME,
    AppConfig,
    airport_family_display_name_from_identity,
    parse_bool,
)
from timecapsulesmb.core.errors import system_exit_message
from timecapsulesmb.core.net import extract_host
from timecapsulesmb.core.paths import resolve_app_paths
from timecapsulesmb.deploy.artifact_resolver import resolve_payload_artifacts
from timecapsulesmb.deploy.artifacts import validate_artifacts
from timecapsulesmb.deploy.auth import render_smbpasswd
from timecapsulesmb.deploy.boot_assets import boot_asset_path
from timecapsulesmb.deploy.dry_run import deployment_plan_to_jsonable
from timecapsulesmb.deploy.executor import (
    flush_remote_filesystem_writes,
    remote_request_reboot,
    run_remote_actions,
    upload_deployment_payload,
)
from timecapsulesmb.deploy.planner import (
    BINARY_MDNS_SOURCE,
    BINARY_NBNS_SOURCE,
    BINARY_SMBD_SOURCE,
    DEFAULT_APPLE_MOUNT_WAIT_SECONDS,
    DEPLOY_STARTUP_ACTIVATE_NOW,
    DEPLOY_STARTUP_REBOOT_THEN_ACTIVATE,
    DEPLOY_STARTUP_REBOOT_THEN_VERIFY,
    DeploymentStartupMode,
    FileTransfer,
    GENERATED_FLASH_CONFIG_SOURCE,
    GENERATED_SMBPASSWD_SOURCE,
    GENERATED_USERNAME_MAP_SOURCE,
    PACKAGED_BOOT_SOURCE,
    PACKAGED_COMMON_SH_SOURCE,
    PACKAGED_DFREE_SH_SOURCE,
    PACKAGED_MANAGER_SOURCE,
    PACKAGED_RC_LOCAL_SOURCE,
    build_deployment_plan,
)
from timecapsulesmb.deploy.verify import (
    managed_runtime_ready,
    render_managed_runtime_verification,
    verify_managed_runtime,
)
from timecapsulesmb.device.compat import (
    DeviceCompatibility,
    is_netbsd4_payload_family,
    payload_family_description,
    render_compatibility_message,
    require_compatibility,
)
from timecapsulesmb.device.probe import (
    read_remote_network_diagnostics_conn,
    read_runtime_log_tails_conn,
    runtime_startup_failure_debug_fields,
    wait_for_ssh_state_conn,
)
from timecapsulesmb.device.storage import (
    MAST_DISCOVERY_ATTEMPTS,
    MAST_DISCOVERY_DELAY_SECONDS,
    build_dry_run_payload_home,
    mast_volumes_debug_summary,
    payload_candidate_checks_debug_summary,
    select_payload_home_with_diagnostics_conn,
    verify_payload_home_conn,
    wait_for_mast_volumes_conn,
)
from timecapsulesmb.integrations.acp import ACPError, reboot as acp_reboot
from timecapsulesmb.services.app import (
    AppOperationError,
    OperationResult,
    bool_param,
    config_path,
    int_param,
    optional_bool_param,
)
from timecapsulesmb.services.credentials import overlay_request_credentials
from timecapsulesmb.services.deploy import (
    DEPLOY_REBOOT_NO_DOWN_MESSAGE,
    DEPLOY_REBOOT_UP_TIMEOUT_MESSAGE,
    activation_complete_message,
    no_mast_volumes_message,
    no_writable_mast_volumes_message,
    payload_verification_error,
    render_flash_runtime_config,
    startup_mode_for_deploy,
)
from timecapsulesmb.services.activation import decide_netbsd4_post_reboot_activation
from timecapsulesmb.services.runtime import ManagedTargetState, load_env_config, resolve_validated_managed_target
from timecapsulesmb.transport.ssh import SshCommandTimeout, SshConnection, SshError


ACP_REBOOT_REQUEST_TIMEOUT_SECONDS = 10
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


def _best_effort_debug_summary(render, value: object) -> object | None:
    try:
        return render(value)
    except Exception:
        return None


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


@dataclass(frozen=True)
class DeployConfirmationPresentation:
    title: str
    message: str
    action_title: str
    risk: str
    summary: str
    presentation_id: str


def effective_no_wait_for_deploy(*, requested: bool, no_reboot: bool) -> bool:
    return False if no_reboot else requested


def optional_unsigned_int_override_param(params: dict[str, object], name: str) -> int | str | None:
    if name not in params or params.get(name) is None:
        return None
    value = params.get(name)
    if isinstance(value, str) and value.strip() == "":
        return ""
    return int_param(params, name, 0)


def confirmation_presentation_for_startup_mode(
    *,
    startup_mode: DeploymentStartupMode,
    no_wait: bool,
    device_name: str,
) -> DeployConfirmationPresentation:
    if startup_mode == DEPLOY_STARTUP_REBOOT_THEN_ACTIVATE:
        if no_wait:
            return DeployConfirmationPresentation(
                title="Confirm NetBSD4 deployment and reboot request",
                message=(
                    f"Deploy TimeCapsuleSMB to this {device_name}, request reboot, and return immediately "
                    "without running Samba activation after SSH returns?"
                ),
                action_title="Deploy and request reboot",
                risk="reboot",
                summary="NetBSD4 deployment with reboot request and no post-reboot activation wait",
                presentation_id="deploy.netbsd4_no_wait",
            )
        return DeployConfirmationPresentation(
            title="Confirm NetBSD4 deployment",
            message=f"Deploy TimeCapsuleSMB to this {device_name}, reboot it, then activate Samba after SSH returns?",
            action_title="Deploy, reboot, and activate",
            risk="reboot",
            summary="NetBSD4 deployment with reboot and service activation",
            presentation_id="deploy.netbsd4",
        )
    if startup_mode == DEPLOY_STARTUP_ACTIVATE_NOW:
        return DeployConfirmationPresentation(
            title="Confirm deployment and runtime start",
            message=f"Deploy TimeCapsuleSMB to this {device_name} and start Samba without rebooting it?",
            action_title="Deploy and start SMB",
            risk="remote_write",
            summary="Deployment without reboot and runtime start",
            presentation_id="deploy.activate_now",
        )
    if no_wait:
        return DeployConfirmationPresentation(
            title="Confirm deployment and reboot request",
            message=f"Deploy TimeCapsuleSMB to this {device_name}, request reboot, and return immediately?",
            action_title="Deploy and request reboot",
            risk="reboot",
            summary="Deployment with reboot request and no post-reboot verification wait",
            presentation_id="deploy.reboot_no_wait",
        )
    return DeployConfirmationPresentation(
        title="Confirm deployment and reboot",
        message=f"Deploy TimeCapsuleSMB and reboot this {device_name}?",
        action_title="Deploy and reboot",
        risk="reboot",
        summary="Deployment with reboot request",
        presentation_id="deploy.reboot",
    )


def require_supported_payload(target: ManagedTargetState, *, allow_unsupported: bool) -> DeviceCompatibility:
    probe_state = target.probe_state
    if probe_state is None:
        raise AppOperationError("Failed to determine remote device OS compatibility.", code="remote_error")
    compatibility = require_compatibility(
        probe_state.compatibility,
        fallback_error=probe_state.probe_result.error or "Failed to determine remote device OS compatibility.",
    )
    if not compatibility.supported and not allow_unsupported:
        raise AppOperationError(render_compatibility_message(compatibility), code="unsupported_device")
    if not compatibility.payload_family:
        raise AppOperationError("No deployable payload is available for this detected device.", code="unsupported_device")
    return compatibility


def load_config_and_target(
    context: AppOperationContext,
    params: dict[str, object],
    *,
    profile: str,
    include_probe: bool,
) -> tuple[AppConfig, ManagedTargetState]:
    context.stage("load_config")
    config = overlay_request_credentials(load_env_config(env_path=config_path(params)), params)
    context.config = config
    context.stage("resolve_managed_target")
    target = resolve_validated_managed_target(
        config,
        command_name=context.operation,
        profile=profile,
        include_probe=include_probe,
    )
    context.apply_managed_target(target)
    return config, target


def deploy_operation(params: dict[str, object], context: AppOperationContext) -> OperationResult:
    operation = "deploy"
    nbns_enabled = bool_param(params, "nbns_enabled", True)
    dry_run = bool_param(params, "dry_run")
    no_reboot = bool_param(params, "no_reboot")
    no_wait = bool_param(params, "no_wait")
    mount_wait = int_param(params, "mount_wait", DEFAULT_APPLE_MOUNT_WAIT_SECONDS)
    allow_unsupported = bool_param(params, "allow_unsupported")
    debug_logging = optional_bool_param(params, "debug_logging")
    ata_idle_seconds = (
        int_param(params, "ata_idle_seconds", int(DEFAULTS["TC_ATA_IDLE_SECONDS"]))
        if "ata_idle_seconds" in params and params.get("ata_idle_seconds") is not None
        else None
    )
    ata_standby = optional_unsigned_int_override_param(params, "ata_standby")
    context.update_fields(
        nbns_enabled=nbns_enabled,
        reboot_was_attempted=False,
        device_came_back_after_reboot=False,
    )

    config, target = load_config_and_target(context, params, profile="deploy", include_probe=True)
    connection = target.connection
    app_paths = resolve_app_paths(config_path=config_path(params))
    internal_share_use_disk_root = bool_param(
        params,
        "internal_share_use_disk_root",
        parse_bool(config.get("TC_INTERNAL_SHARE_USE_DISK_ROOT", DEFAULTS["TC_INTERNAL_SHARE_USE_DISK_ROOT"])),
    )
    any_protocol = bool_param(
        params,
        "any_protocol",
        parse_bool(config.get("TC_ANY_PROTOCOL", DEFAULTS["TC_ANY_PROTOCOL"])),
    )

    context.stage("validate_artifacts")
    failures = [message for _, ok, message in validate_artifacts(app_paths.distribution_root) if not ok]
    if failures:
        raise AppOperationError("; ".join(failures), code="validation_failed")

    context.stage("check_compatibility")
    compatibility = require_supported_payload(target, allow_unsupported=allow_unsupported)
    payload_family = compatibility.payload_family
    is_netbsd4 = is_netbsd4_payload_family(payload_family)
    if is_netbsd4:
        # Apple NetBSD 4 firmware can expose /usr/bin/scp but hang after
        # writing the file. Use the SSH pipe upload fallback consistently.
        connection.remote_has_scp = False
    startup_mode = startup_mode_for_deploy(no_reboot=no_reboot, is_netbsd4=is_netbsd4)
    no_wait = effective_no_wait_for_deploy(requested=no_wait, no_reboot=no_reboot)
    context.update_fields(deploy_startup_mode=startup_mode)
    context.log(f"Using {payload_family_description(payload_family)} payload.")
    resolved_artifacts = resolve_payload_artifacts(app_paths.distribution_root, payload_family)
    if not dry_run:
        confirmation_plan = build_deployment_plan(
            connection.host,
            build_dry_run_payload_home(MANAGED_PAYLOAD_DIR_NAME),
            resolved_artifacts["smbd"].absolute_path,
            resolved_artifacts["mdns-advertiser"].absolute_path,
            resolved_artifacts["nbns-advertiser"].absolute_path,
            startup_mode=startup_mode,
            apple_mount_wait_seconds=mount_wait,
            wait_after_reboot=not no_wait,
        )
        device_name = airport_family_display_name_from_identity(
            model=target.probe_state.probe_result.airport_model if target.probe_state else None,
            syap=target.probe_state.probe_result.airport_syap if target.probe_state else None,
        )
        presentation = confirmation_presentation_for_startup_mode(
            startup_mode=startup_mode,
            no_wait=no_wait,
            device_name=device_name,
        )
        presentation_values = {
            "device_name": device_name,
            "netbsd4": is_netbsd4,
            "requires_reboot": bool(confirmation_plan.reboot_required),
            "no_reboot": no_reboot,
            "no_wait": no_wait,
            "startup_mode": startup_mode,
        }
        require_confirmation(
            params,
            build_confirmation(
                operation=operation,
                params=params,
                title=presentation.title,
                message=presentation.message,
                action_title=presentation.action_title,
                risk=presentation.risk,
                summary=presentation.summary,
                context={
                    "host": connection.host,
                    "payload_family": payload_family,
                    "netbsd4": is_netbsd4,
                    "requires_reboot": bool(confirmation_plan.reboot_required),
                    "no_reboot": no_reboot,
                    "no_wait": no_wait,
                    "startup_mode": startup_mode,
                },
                presentation_id=presentation.presentation_id,
                presentation_values=presentation_values,
            ),
        )
    if dry_run:
        payload_home = build_dry_run_payload_home(MANAGED_PAYLOAD_DIR_NAME)
    else:
        context.stage("read_mast")
        mast_discovery = wait_for_mast_volumes_conn(
            connection,
            attempts=MAST_DISCOVERY_ATTEMPTS,
            delay_seconds=MAST_DISCOVERY_DELAY_SECONDS,
        )
        context.add_debug_fields(
            mast_read_attempts=mast_discovery.attempts,
            mast_volume_count=len(mast_discovery.volumes),
            mast_candidates=_best_effort_debug_summary(mast_volumes_debug_summary, mast_discovery.volumes),
        )
        if not mast_discovery.volumes:
            raise AppOperationError(
                no_mast_volumes_message(
                    attempts=MAST_DISCOVERY_ATTEMPTS,
                    delay_seconds=MAST_DISCOVERY_DELAY_SECONDS,
                ),
                code="remote_error",
            )
        context.stage("select_payload_home")
        selection = select_payload_home_with_diagnostics_conn(
            connection,
            mast_discovery.volumes,
            MANAGED_PAYLOAD_DIR_NAME,
            wait_seconds=mount_wait,
        )
        context.add_debug_fields(
            mast_candidate_checks=_best_effort_debug_summary(
                payload_candidate_checks_debug_summary,
                getattr(selection, "checks", ()),
            )
        )
        if selection.payload_home is None:
            raise AppOperationError(
                no_writable_mast_volumes_message(len(mast_discovery.volumes)),
                code="remote_error",
            )
        payload_home = selection.payload_home

    context.stage("build_deployment_plan")
    plan = build_deployment_plan(
        connection.host,
        payload_home,
        resolved_artifacts["smbd"].absolute_path,
        resolved_artifacts["mdns-advertiser"].absolute_path,
        resolved_artifacts["nbns-advertiser"].absolute_path,
        startup_mode=startup_mode,
        apple_mount_wait_seconds=mount_wait,
        wait_after_reboot=not no_wait,
    )
    context.add_debug_fields(
        payload_volume_root=plan.volume_root,
        payload_device_path=plan.device_path,
        payload_dir=plan.payload_dir,
    )
    if dry_run:
        return OperationResult(True, deploy_plan_payload(
            deployment_plan_to_jsonable(plan),
            payload_family=payload_family,
            netbsd4=is_netbsd4,
        ))

    context.stage("pre_upload_actions")
    run_remote_actions(connection, plan.pre_upload_actions)
    context.stage("prepare_deployment_files")
    try:
        flash_config_text = render_flash_runtime_config(
            config,
            payload_home,
            nbns_enabled=nbns_enabled,
            debug_logging=debug_logging,
            internal_share_use_disk_root=internal_share_use_disk_root,
            any_protocol=any_protocol,
            ata_idle_seconds=ata_idle_seconds,
            ata_standby=ata_standby,
        )
    except ValueError as exc:
        raise AppOperationError(str(exc), code="validation_failed") from exc
    with tempfile.TemporaryDirectory(prefix="tc-deploy-") as tmp, ExitStack() as boot_assets:
        tmpdir = Path(tmp)
        generated_flash_config = tmpdir / "tcapsulesmb.conf"
        generated_smbpasswd = tmpdir / "smbpasswd"
        generated_username_map = tmpdir / "username.map"
        generated_flash_config.write_text(flash_config_text)
        smbpasswd_text, username_map_text = render_smbpasswd(connection.password)
        generated_smbpasswd.write_text(smbpasswd_text)
        generated_username_map.write_text(username_map_text)
        upload_sources = {
            BINARY_SMBD_SOURCE: plan.smbd_path,
            BINARY_MDNS_SOURCE: plan.mdns_path,
            BINARY_NBNS_SOURCE: plan.nbns_path,
            GENERATED_SMBPASSWD_SOURCE: generated_smbpasswd,
            GENERATED_USERNAME_MAP_SOURCE: generated_username_map,
            GENERATED_FLASH_CONFIG_SOURCE: generated_flash_config,
            PACKAGED_RC_LOCAL_SOURCE: boot_assets.enter_context(boot_asset_path("rc.local")),
            PACKAGED_COMMON_SH_SOURCE: boot_assets.enter_context(boot_asset_path("common.sh")),
            PACKAGED_DFREE_SH_SOURCE: boot_assets.enter_context(boot_asset_path("dfree.sh")),
            PACKAGED_BOOT_SOURCE: boot_assets.enter_context(boot_asset_path("boot.sh")),
            PACKAGED_MANAGER_SOURCE: boot_assets.enter_context(boot_asset_path("manager.sh")),
        }
        current_upload_stage: str | None = None

        def stage_upload(transfer: FileTransfer) -> None:
            nonlocal current_upload_stage
            stage = deploy_upload_stage(transfer)
            if stage == current_upload_stage:
                return
            current_upload_stage = stage
            context.stage(stage)

        upload_deployment_payload(
            plan,
            connection=connection,
            source_resolver=upload_sources,
            on_uploading=stage_upload,
        )

    context.stage("post_upload_actions")
    run_remote_actions(connection, plan.post_upload_actions)
    verify_payload_upload(context, connection, payload_home, wait_seconds=mount_wait)
    context.stage("flush_payload_upload")
    context.log("Flushing deployed payload to disk...")
    flush_remote_filesystem_writes(connection)
    verify_payload_upload(context, connection, payload_home, wait_seconds=mount_wait, post_sync=True)

    if startup_mode == DEPLOY_STARTUP_ACTIVATE_NOW:
        run_activation_actions_and_verify(
            context,
            connection,
            plan.activation_actions,
            activation_message="Starting deployed runtime without reboot.",
            activation_stage="activate_runtime",
            verification_stage="verify_runtime_activation",
            verification_timeout_seconds=180,
            failure_message="Managed runtime activation failed.",
        )
        return OperationResult(True, deploy_result_payload(
            payload_dir=plan.payload_dir,
            netbsd4=is_netbsd4,
            rebooted=False,
            reboot_requested=False,
            waited=False,
            verified=True,
            message=activation_complete_message(is_netbsd4=is_netbsd4),
            payload_family=payload_family,
        ))

    if no_wait:
        request_reboot(
            context,
            connection,
            strategy="ssh_shutdown_then_reboot",
            raise_on_request_error=True,
        )
        return OperationResult(True, deploy_result_payload(
            payload_dir=plan.payload_dir,
            reboot_requested=True,
            waited=False,
            verified=False,
            payload_family=payload_family,
        ))

    request_reboot_and_wait(
        context,
        connection,
        strategy="ssh_shutdown_then_reboot",
        reboot_no_down_message=DEPLOY_REBOOT_NO_DOWN_MESSAGE,
    )

    if startup_mode == DEPLOY_STARTUP_REBOOT_THEN_ACTIVATE:
        context.stage("probe_runtime")
        decision = decide_netbsd4_post_reboot_activation(connection)
        context.add_debug_fields(
            activation_decision=decision.reason,
            manual_activation_required=decision.run_actions,
        )
        context.log(decision.detail)
        if decision.run_actions:
            run_activation_actions_and_verify(
                context,
                connection,
                plan.activation_actions,
                activation_message="Activating deployed runtime after reboot.",
                activation_stage="post_reboot_activation",
                verification_stage="verify_runtime_activation",
                verification_timeout_seconds=180,
                failure_message="NetBSD4 activation failed.",
            )
        else:
            context.log("NetBSD4 firmware autostart is enabled; waiting for managed runtime.")
            verify_runtime(
                context,
                connection,
                stage="verify_runtime_activation",
                timeout_seconds=180,
                failure_message="NetBSD4 activation failed.",
            )
        return OperationResult(True, deploy_result_payload(
            payload_dir=plan.payload_dir,
            netbsd4=True,
            rebooted=True,
            reboot_requested=True,
            waited=True,
            verified=True,
            message=activation_complete_message(is_netbsd4=is_netbsd4),
            payload_family=payload_family,
        ))

    verify_runtime(context, connection, stage="verify_runtime_reboot", timeout_seconds=240)
    return OperationResult(True, deploy_result_payload(
        payload_dir=plan.payload_dir,
        rebooted=True,
        reboot_requested=True,
        waited=True,
        verified=True,
        payload_family=payload_family,
    ))


def run_activation_actions_and_verify(
    context: AppOperationContext,
    connection: SshConnection,
    activation_actions,
    *,
    activation_message: str,
    activation_stage: str,
    verification_stage: str,
    verification_timeout_seconds: int = 180,
    failure_message: str = "Managed runtime activation failed.",
) -> None:
    context.stage(activation_stage)
    context.log(activation_message)
    run_remote_actions(connection, activation_actions)
    verify_runtime(
        context,
        connection,
        stage=verification_stage,
        timeout_seconds=verification_timeout_seconds,
        failure_message=failure_message,
    )


def verify_payload_upload(
    context: AppOperationContext,
    connection: SshConnection,
    payload_home,
    *,
    wait_seconds: int,
    post_sync: bool = False,
) -> None:
    context.stage("verify_payload_upload_after_sync" if post_sync else "verify_payload_upload")
    verification = verify_payload_home_conn(connection, payload_home, wait_seconds=wait_seconds)
    context.log(verification.detail)
    context.add_debug_fields(
        **{"payload_post_sync_verification" if post_sync else "payload_upload_verification": verification.detail}
    )
    if not verification.ok:
        raise AppOperationError(payload_verification_error(payload_home, verification), code="remote_error")


def verify_runtime(
    context: AppOperationContext,
    connection: SshConnection,
    *,
    stage: str,
    timeout_seconds: int,
    failure_message: str = "Managed runtime did not become ready.",
) -> None:
    context.stage(stage)
    verification = verify_managed_runtime(connection, timeout_seconds=timeout_seconds)
    for line in render_managed_runtime_verification(
        verification,
        heading="Waiting for managed runtime to finish starting...",
    ):
        context.log(line)
    if not managed_runtime_ready(verification):
        runtime_log_fields: dict[str, object] = {}
        try:
            runtime_log_fields = read_runtime_log_tails_conn(connection)
            context.add_debug_fields(**runtime_log_fields)
        except Exception as exc:
            context.add_debug_fields(remote_runtime_log_tail_error=system_exit_message(exc))
        startup_failure_fields = runtime_startup_failure_debug_fields(
            runtime_log_fields,
            verification_detail=verification.detail.strip(),
        )
        if startup_failure_fields:
            context.add_debug_fields(**startup_failure_fields)
            if startup_failure_fields.get("runtime_startup_failure") == "network_auto_ip_unavailable":
                try:
                    context.add_debug_fields(**read_remote_network_diagnostics_conn(connection))
                except Exception as exc:
                    context.add_debug_fields(remote_network_diagnostics_error=system_exit_message(exc))
        raise AppOperationError(
            f"{failure_message.rstrip()} {verification.detail.strip()}".strip(),
            code="remote_error",
        )


def request_reboot_and_wait(
    context: AppOperationContext,
    connection: SshConnection,
    *,
    strategy: str,
    reboot_no_down_message: str,
    reboot_up_timeout_message: str = DEPLOY_REBOOT_UP_TIMEOUT_MESSAGE,
    down_timeout_seconds: int = 60,
    up_timeout_seconds: int = 240,
) -> None:
    request_reboot(context, connection, strategy=strategy)

    context.stage("wait_for_reboot_down")
    context.log("Waiting for the device to go down...")
    if not wait_for_ssh_state_conn(connection, expected_up=False, timeout_seconds=down_timeout_seconds):
        raise AppOperationError(reboot_no_down_message, code="remote_error")
    context.stage("wait_for_reboot_up")
    context.log("Waiting for the device to come back up...")
    if not wait_for_ssh_state_conn(connection, expected_up=True, timeout_seconds=up_timeout_seconds):
        raise AppOperationError(reboot_up_timeout_message, code="remote_error")
    context.update_fields(device_came_back_after_reboot=True)
    context.log("Device is back online.")


def request_reboot(
    context: AppOperationContext,
    connection: SshConnection,
    *,
    strategy: str,
    raise_on_request_error: bool = False,
) -> None:
    context.stage("reboot")
    context.update_fields(reboot_was_attempted=True)
    context.add_debug_fields(reboot_request_strategy=strategy)
    if strategy == "acp_then_ssh":
        context.add_debug_fields(acp_reboot_attempted=True)
        try:
            acp_reboot(extract_host(connection.host), connection.password, timeout=ACP_REBOOT_REQUEST_TIMEOUT_SECONDS)
            context.log("ACP reboot requested.")
            context.add_debug_fields(acp_reboot_succeeded=True)
        except ACPError as exc:
            context.add_debug_fields(
                acp_reboot_succeeded=False,
                acp_reboot_error=system_exit_message(exc),
            )
            context.log(f"ACP reboot request failed; trying SSH reboot request: {exc}", level="warning")
            request_ssh_reboot(
                context,
                connection,
                raise_on_request_error=raise_on_request_error,
            )
    else:
        request_ssh_reboot(
            context,
            connection,
            raise_on_request_error=raise_on_request_error,
        )


def request_ssh_reboot(
    context: AppOperationContext,
    connection: SshConnection,
    *,
    raise_on_request_error: bool = False,
) -> None:
    context.add_debug_fields(ssh_reboot_attempted=True)
    try:
        remote_request_reboot(connection)
    except SshCommandTimeout as exc:
        context.add_debug_fields(
            ssh_reboot_succeeded=False,
            ssh_reboot_timed_out=True,
            ssh_reboot_error=system_exit_message(exc),
        )
        if raise_on_request_error:
            raise AppOperationError(f"SSH reboot request timed out: {exc}", code="remote_error") from exc
        context.log(f"SSH reboot request timed out; checking whether the device is rebooting: {exc}", level="warning")
        return
    except SshError as exc:
        context.add_debug_fields(
            ssh_reboot_succeeded=False,
            ssh_reboot_error=system_exit_message(exc),
        )
        if raise_on_request_error:
            raise AppOperationError(f"SSH reboot request failed: {exc}", code="remote_error") from exc
        context.log(f"SSH reboot request failed; checking whether the device is rebooting anyway: {exc}", level="warning")
        return
    context.add_debug_fields(ssh_reboot_succeeded=True)
    context.log("SSH reboot requested.")
