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
    airport_family_display_name_from_identity,
    parse_bool,
)
from timecapsulesmb.core.errors import system_exit_message
from timecapsulesmb.core.paths import resolve_app_paths
from timecapsulesmb.deploy.artifact_resolver import resolve_payload_artifacts
from timecapsulesmb.deploy.artifacts import validate_artifacts
from timecapsulesmb.deploy.auth import render_smbpasswd
from timecapsulesmb.deploy.boot_assets import boot_asset_path
from timecapsulesmb.deploy.dry_run import deployment_plan_to_jsonable
from timecapsulesmb.deploy.executor import (
    flush_remote_filesystem_writes,
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
    render_managed_runtime_verification,
)
from timecapsulesmb.device.errors import DeviceError
from timecapsulesmb.device.probe import (
    probe_managed_runtime_conn,
    read_remote_network_diagnostics_conn,
    read_runtime_log_tails_conn,
    runtime_startup_failure_debug_fields,
)
from timecapsulesmb.device.compat import payload_family_description
from timecapsulesmb.device.storage import (
    select_payload_home_with_diagnostics_conn,
    verify_payload_home_conn,
    wait_for_mast_volumes_conn,
)
from timecapsulesmb.app.ops.common import (
    load_request_config,
    resolve_request_target,
)
from timecapsulesmb.services.app import (
    AppOperationError,
    OperationResult,
    bool_param,
    config_path,
    int_param,
    optional_bool_param,
)
from timecapsulesmb.services.deploy import (
    DEPLOY_REBOOT_NO_DOWN_MESSAGE,
    DEPLOY_REBOOT_UP_TIMEOUT_MESSAGE,
    activation_complete_message,
    deploy_artifact_failures,
    deploy_upload_stage,
    effective_no_wait_for_deploy,
    payload_verification_error,
    prepare_deploy_payload_context,
    require_supported_payload,
    render_flash_runtime_config,
    select_deploy_payload_home,
)
from timecapsulesmb.services.reboot import RebootFlowError, request_reboot, request_reboot_and_wait
from timecapsulesmb.services.activation import decide_netbsd4_post_reboot_activation
from timecapsulesmb.transport.ssh import SshConnection


@dataclass(frozen=True)
class DeployConfirmationPresentation:
    title: str
    message: str
    action_title: str
    risk: str
    summary: str
    presentation_id: str


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

    config = load_request_config(params, context)
    target = resolve_request_target(config, context, profile="deploy", include_probe=True)
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
    failures = deploy_artifact_failures(app_paths.distribution_root, validate=validate_artifacts)
    if failures:
        raise AppOperationError("; ".join(failures), code="validation_failed")

    context.stage("check_compatibility")
    try:
        compatibility = require_supported_payload(target, allow_unsupported=allow_unsupported)
        payload_context = prepare_deploy_payload_context(connection, compatibility, no_reboot=no_reboot)
    except DeviceError as exc:
        raise AppOperationError(str(exc), code="unsupported_device") from exc
    payload_family = payload_context.payload_family
    is_netbsd4 = payload_context.is_netbsd4
    startup_mode = payload_context.startup_mode
    no_wait = effective_no_wait_for_deploy(requested=no_wait, no_reboot=no_reboot)
    context.update_fields(deploy_startup_mode=startup_mode)
    context.log(f"Using {payload_family_description(payload_family)} payload.")
    resolved_artifacts = resolve_payload_artifacts(app_paths.distribution_root, payload_family)
    if not dry_run:
        confirmation_plan = build_deployment_plan(
            connection.host,
            select_deploy_payload_home(
                connection,
                dry_run=True,
                payload_dir_name=MANAGED_PAYLOAD_DIR_NAME,
                mount_wait_seconds=mount_wait,
            ),
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
    try:
        payload_home = select_deploy_payload_home(
            connection,
            dry_run=dry_run,
            payload_dir_name=MANAGED_PAYLOAD_DIR_NAME,
            mount_wait_seconds=mount_wait,
            callbacks=context.to_runtime_callbacks(),
            wait_for_mast_volumes=wait_for_mast_volumes_conn,
            select_payload_home=select_payload_home_with_diagnostics_conn,
        )
    except DeviceError as exc:
        raise AppOperationError(str(exc), code="remote_error") from exc

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
            verification_timeout_seconds=200,
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
        try:
            request_reboot(
                connection,
                strategy="ssh_shutdown_then_reboot",
                callbacks=context.to_runtime_callbacks(),
                raise_on_request_error=True,
            )
        except RebootFlowError as exc:
            raise AppOperationError(str(exc), code="remote_error") from exc
        return OperationResult(True, deploy_result_payload(
            payload_dir=plan.payload_dir,
            reboot_requested=True,
            waited=False,
            verified=False,
            payload_family=payload_family,
        ))

    try:
        request_reboot_and_wait(
            connection,
            strategy="ssh_shutdown_then_reboot",
            callbacks=context.to_runtime_callbacks(),
            down_timeout_seconds=60,
            up_timeout_seconds=240,
            reboot_no_down_message=DEPLOY_REBOOT_NO_DOWN_MESSAGE,
            reboot_up_timeout_message=DEPLOY_REBOOT_UP_TIMEOUT_MESSAGE,
        )
    except RebootFlowError as exc:
        raise AppOperationError(str(exc), code="remote_error") from exc

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
                verification_timeout_seconds=200,
                failure_message="NetBSD4 activation failed.",
            )
        else:
            context.log("NetBSD4 firmware autostart is enabled; waiting for managed runtime.")
            verify_runtime(
                context,
                connection,
                stage="verify_runtime_activation",
                timeout_seconds=200,
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
    verification = probe_managed_runtime_conn(connection, timeout_seconds=timeout_seconds)
    for line in render_managed_runtime_verification(
        verification,
        heading="Waiting for managed runtime to finish starting...",
    ):
        context.log(line)
    if not verification.ready:
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
