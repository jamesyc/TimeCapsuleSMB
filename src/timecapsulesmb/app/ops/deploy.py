from __future__ import annotations

from dataclasses import dataclass, replace

from timecapsulesmb.app.context import AppOperationContext
from timecapsulesmb.app.contracts import deploy_plan_payload, deploy_result_payload
from timecapsulesmb.app.confirmations import (
    build_confirmation,
    disk_failing_confirmation_fields,
    disk_failing_context,
    disk_failing_telemetry_fields,
    require_disk_failing_confirmation,
)
from timecapsulesmb.core.config import (
    airport_family_display_name_from_identity,
)
from timecapsulesmb.core.error_codes import ErrorCode
from timecapsulesmb.core.paths import resolve_app_paths
from timecapsulesmb.app.ops.common import (
    load_request_config,
    managed_setting_params,
    resolve_managed_settings,
    resolve_request_target,
)
from timecapsulesmb.device.disk_errors import read_disk_failing_evidence_conn
from timecapsulesmb.device.errors import DeviceError
from timecapsulesmb.identity import load_install_identity
from timecapsulesmb.services.app import (
    AppOperationError,
    OperationResult,
    bool_param,
    config_path,
    int_param,
)
from timecapsulesmb.services.context import exception_cause_detail, message_with_exception_cause
from timecapsulesmb.services.deploy import (
    DEFAULT_APPLE_MOUNT_WAIT_SECONDS,
    DEPLOY_STARTUP_REBOOT_THEN_ACTIVATE,
    DeployArtifactValidationError,
    DeployCompletionMessages,
    DeployOptions,
    DeployRuntimeConfig,
    DeploymentStartupMode,
    complete_deployment_after_upload,
    deployment_plan_to_jsonable,
    deploy_upload_stage,
    payload_family_description,
    prepare_deploy_preflight,
    prepare_deployment_plan,
    upload_and_verify_deployment_payload,
)
from timecapsulesmb.services.reboot import RebootFlowError


@dataclass(frozen=True)
class DeployConfirmationPresentation:
    title: str
    message: str
    action_title: str
    risk: str
    summary: str
    presentation_id: str


def _device_error_code(exc: DeviceError) -> ErrorCode:
    return exc.code or ErrorCode.REMOTE_ERROR


def device_operation_error(
    context: AppOperationContext,
    exc: DeviceError,
) -> AppOperationError:
    message = str(exc)
    diagnostic_message = message_with_exception_cause(message, exc)
    context.set_error(diagnostic_message)
    cause = exception_cause_detail(exc)
    code = _device_error_code(exc)
    return AppOperationError(
        message,
        code=code,
        debug={"cause": cause} if cause else None,
    )


def confirmation_presentation_for_startup_mode(
    *,
    startup_mode: DeploymentStartupMode,
    no_wait: bool,
    device_name: str,
) -> DeployConfirmationPresentation:
    if startup_mode == DEPLOY_STARTUP_REBOOT_THEN_ACTIVATE:
        if no_wait:
            return DeployConfirmationPresentation(
                title="Confirm NetBSD 4 deployment and reboot request",
                message=(
                    f"Deploy TimeCapsuleSMB to this {device_name}, request reboot, and return immediately "
                    "without running Samba activation after SSH returns?"
                ),
                action_title="Deploy and request reboot",
                risk="reboot",
                summary="NetBSD 4 deployment with reboot request and no post-reboot activation wait",
                presentation_id="deploy.netbsd4_no_wait",
            )
        return DeployConfirmationPresentation(
            title="Confirm NetBSD 4 deployment",
            message=f"Deploy TimeCapsuleSMB to this {device_name}, reboot it, then activate Samba after SSH returns?",
            action_title="Deploy, reboot, and activate",
            risk="reboot",
            summary="NetBSD 4 deployment with reboot and service activation",
            presentation_id="deploy.netbsd4",
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


def _deploy_completion_payload(result) -> object:
    return deploy_result_payload(
        payload_dir=result.payload_dir,
        netbsd4=result.is_netbsd4,
        rebooted=result.rebooted,
        reboot_requested=result.reboot_requested,
        waited=result.waited,
        verified=result.verified,
        message=result.message,
        summary=result.summary,
        payload_family=result.payload_family,
    )


def deploy_operation(params: dict[str, object], context: AppOperationContext) -> OperationResult:
    operation = "deploy"
    if "nbns_enabled" in params:
        raise AppOperationError(
            "nbns_enabled has been removed; Apple's native NBNS service is always enabled.",
            code=ErrorCode.VALIDATION_FAILED,
        )
    dry_run = bool_param(params, "dry_run")
    if bool_param(params, "no_reboot"):
        raise AppOperationError(
            "Deployment now requires a reboot. Remove no_reboot and retry; no device changes were made.",
            code=ErrorCode.VALIDATION_FAILED,
        )
    no_wait = bool_param(params, "no_wait")
    sent_settings = managed_setting_params(params)
    rsync_enabled = bool(sent_settings.rsync_enabled)
    mount_wait = int_param(params, "mount_wait", DEFAULT_APPLE_MOUNT_WAIT_SECONDS)
    allow_unsupported = bool_param(params, "allow_unsupported")
    deploy_options = DeployOptions(
        dry_run=dry_run,
        no_wait=no_wait,
        rsync_enabled=rsync_enabled,
        mount_wait_seconds=mount_wait,
        allow_unsupported=allow_unsupported,
    )
    no_wait = deploy_options.no_wait
    context.update_fields(
        rsync_enabled=rsync_enabled,
        reboot_was_attempted=False,
        device_came_back_after_reboot=False,
    )

    config = load_request_config(params, context)
    target = resolve_request_target(config, context, profile="deploy", include_probe=True)
    connection = target.connection
    app_paths = resolve_app_paths(config_path=config_path(params))
    telemetry_enabled = load_install_identity().telemetry_enabled
    settings = resolve_managed_settings(sent_settings, config)

    try:
        preflight = prepare_deploy_preflight(
            connection,
            target,
            app_paths.distribution_root,
            deploy_options,
            callbacks=context.to_operation_callbacks(),
            device_reading=context.reading_at(connection),
        )
    except DeployArtifactValidationError as exc:
        raise AppOperationError(str(exc), code=ErrorCode.VALIDATION_FAILED) from exc
    except DeviceError as exc:
        raise device_operation_error(context, exc) from exc
    payload_context = preflight.payload_context
    payload_family = preflight.payload_family
    is_netbsd4 = preflight.is_netbsd4
    startup_mode = preflight.startup_mode
    context.log(f"Using {payload_family_description(payload_family)} payload.")
    if not dry_run:
        device_name = airport_family_display_name_from_identity(
            model=target.probe_state.probe_result.airport_model if target.probe_state else None,
            syap=target.probe_state.probe_result.airport_syap if target.probe_state else None,
        )
        presentation = confirmation_presentation_for_startup_mode(
            startup_mode=startup_mode,
            no_wait=no_wait,
            device_name=device_name,
        )
        disk_failing = read_disk_failing_evidence_conn(connection, update_fields=context.update_fields)
        if disk_failing:
            context.update_fields(**disk_failing_telemetry_fields(operation, disk_failing))
            presentation = replace(presentation, **disk_failing_confirmation_fields(operation, disk_failing))
        presentation_values = {
            "device_name": device_name,
            "netbsd4": is_netbsd4,
            "requires_reboot": preflight.requires_reboot,
            "no_wait": no_wait,
            "startup_mode": startup_mode,
            "rsync_enabled": rsync_enabled,
            **({"disk_failing": True} if disk_failing else {}),
        }
        require_disk_failing_confirmation(
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
                    "requires_reboot": preflight.requires_reboot,
                    "no_wait": no_wait,
                    "startup_mode": startup_mode,
                    **disk_failing_context(disk_failing),
                },
                presentation_id=presentation.presentation_id,
                presentation_values=presentation_values,
            ),
        )
    try:
        prepared_plan = prepare_deployment_plan(
            connection,
            app_paths.distribution_root,
            payload_context,
            dry_run=dry_run,
            payload_dir_name=deploy_options.payload_dir_name,
            mount_wait_seconds=mount_wait,
            callbacks=context.to_operation_callbacks(),
            artifacts=preflight.artifacts,
            wait_after_reboot=not no_wait,
            rsync_enabled=rsync_enabled,
        )
    except DeviceError as exc:
        raise device_operation_error(context, exc) from exc
    plan = prepared_plan.plan
    if dry_run:
        return OperationResult(True, deploy_plan_payload(
            deployment_plan_to_jsonable(plan),
            payload_family=payload_family,
            netbsd4=is_netbsd4,
        ))

    current_upload_stage: str | None = None

    def stage_upload(transfer) -> None:
        nonlocal current_upload_stage
        stage = deploy_upload_stage(transfer)
        if stage == current_upload_stage:
            return
        current_upload_stage = stage
        context.stage(stage)

    try:
        upload_and_verify_deployment_payload(
            config,
            connection=connection,
            prepared_plan=prepared_plan,
            runtime_config=DeployRuntimeConfig.from_settings(settings, telemetry_enabled=telemetry_enabled),
            callbacks=context.to_operation_callbacks(),
            initial_upload_stage=None,
            on_uploading=stage_upload,
            on_before_flush=lambda: context.log("Flushing deployed payload to disk..."),
            on_verified=lambda verification: context.log(verification.detail),
        )
    except ValueError as exc:
        raise AppOperationError(str(exc), code=ErrorCode.VALIDATION_FAILED) from exc
    except DeviceError as exc:
        raise device_operation_error(context, exc) from exc

    try:
        completion = complete_deployment_after_upload(
            connection,
            prepared_plan,
            no_wait=no_wait,
            callbacks=context.to_operation_callbacks(),
            messages=DeployCompletionMessages(),
        )
    except RebootFlowError as exc:
        raise AppOperationError(str(exc), code=exc.code) from exc
    except DeviceError as exc:
        raise device_operation_error(context, exc) from exc
    return OperationResult(True, _deploy_completion_payload(completion))
