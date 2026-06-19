from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from timecapsulesmb.app.context import AppOperationContext
from timecapsulesmb.app.contracts import deploy_plan_payload, deploy_result_payload
from timecapsulesmb.app.confirmations import build_confirmation, require_confirmation
from timecapsulesmb.core.config import (
    DEFAULTS,
    airport_family_display_name_from_identity,
    parse_bool,
)
from timecapsulesmb.core.paths import resolve_app_paths
from timecapsulesmb.app.ops.common import (
    load_request_config,
    resolve_request_target,
)
from timecapsulesmb.device.errors import DeviceError
from timecapsulesmb.services.app import (
    AppOperationError,
    OperationResult,
    bool_param,
    config_path,
    int_param,
    optional_bool_param,
)
from timecapsulesmb.services.deploy import (
    DEFAULT_APPLE_MOUNT_WAIT_SECONDS,
    DEPLOY_STARTUP_ACTIVATE_NOW,
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
from timecapsulesmb.services.runtime_verification import verify_managed_runtime_ready

if TYPE_CHECKING:
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


def _deploy_completion_payload(result) -> object:
    return deploy_result_payload(
        payload_dir=result.payload_dir,
        netbsd4=result.is_netbsd4,
        rebooted=result.rebooted,
        reboot_requested=result.reboot_requested,
        waited=result.waited,
        verified=result.verified,
        message=result.message,
        payload_family=result.payload_family,
    )


def _verify_runtime_for_service(
    connection: SshConnection,
    *,
    callbacks,
    stage: str,
    timeout_seconds: int,
    heading: str,
    failure_message: str,
) -> object:
    return verify_managed_runtime_ready(
        connection,
        callbacks=callbacks,
        stage=stage,
        timeout_seconds=timeout_seconds,
        heading=heading,
        failure_message=failure_message,
    )


def deploy_operation(params: dict[str, object], context: AppOperationContext) -> OperationResult:
    operation = "deploy"
    nbns_enabled = bool_param(params, "nbns_enabled", True)
    dry_run = bool_param(params, "dry_run")
    no_reboot = bool_param(params, "no_reboot")
    no_wait = bool_param(params, "no_wait")
    mount_wait = int_param(params, "mount_wait", DEFAULT_APPLE_MOUNT_WAIT_SECONDS)
    allow_unsupported = bool_param(params, "allow_unsupported")
    deploy_options = DeployOptions(
        dry_run=dry_run,
        no_reboot=no_reboot,
        no_wait=no_wait,
        mount_wait_seconds=mount_wait,
        allow_unsupported=allow_unsupported,
    )
    no_wait = deploy_options.effective_no_wait
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
    smb_bind_lan_only = bool_param(
        params,
        "smb_bind_lan_only",
        parse_bool(config.get("TC_SMB_BIND_LAN_ONLY", DEFAULTS["TC_SMB_BIND_LAN_ONLY"])),
    )
    smb_browse_compatibility = bool_param(
        params,
        "smb_browse_compatibility",
        parse_bool(config.get("TC_SMB_BROWSE_COMPATIBILITY", DEFAULTS["TC_SMB_BROWSE_COMPATIBILITY"])),
    )
    mdns_advertise_afp = bool_param(
        params,
        "mdns_advertise_afp",
        parse_bool(config.get("TC_MDNS_ADVERTISE_AFP", DEFAULTS["TC_MDNS_ADVERTISE_AFP"])),
    )
    any_protocol = bool_param(
        params,
        "any_protocol",
        parse_bool(config.get("TC_ANY_PROTOCOL", DEFAULTS["TC_ANY_PROTOCOL"])),
    )
    fruit_metadata_netatalk = bool_param(
        params,
        "fruit_metadata_netatalk",
        parse_bool(config.get("TC_FRUIT_METADATA_NETATALK", DEFAULTS["TC_FRUIT_METADATA_NETATALK"])),
    )

    try:
        preflight = prepare_deploy_preflight(
            connection,
            target,
            app_paths.distribution_root,
            deploy_options,
            callbacks=context.to_operation_callbacks(),
        )
    except DeployArtifactValidationError as exc:
        raise AppOperationError(str(exc), code="validation_failed") from exc
    except DeviceError as exc:
        raise AppOperationError(str(exc), code="unsupported_device") from exc
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
        presentation_values = {
            "device_name": device_name,
            "netbsd4": is_netbsd4,
            "requires_reboot": preflight.requires_reboot,
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
                    "requires_reboot": preflight.requires_reboot,
                    "no_reboot": no_reboot,
                    "no_wait": no_wait,
                    "startup_mode": startup_mode,
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
        )
    except DeviceError as exc:
        raise AppOperationError(str(exc), code="remote_error") from exc
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

    def log_payload_verification(verification, _post_sync: bool) -> None:
        context.log(verification.detail)

    try:
        upload_and_verify_deployment_payload(
            config,
            connection=connection,
            prepared_plan=prepared_plan,
            runtime_config=DeployRuntimeConfig(
                nbns_enabled=nbns_enabled,
                debug_logging=debug_logging,
                internal_share_use_disk_root=internal_share_use_disk_root,
                smb_bind_lan_only=smb_bind_lan_only,
                smb_browse_compatibility=smb_browse_compatibility,
                mdns_advertise_afp=mdns_advertise_afp,
                any_protocol=any_protocol,
                fruit_metadata_netatalk=fruit_metadata_netatalk,
                ata_idle_seconds=ata_idle_seconds,
                ata_standby=ata_standby,
            ),
            callbacks=context.to_operation_callbacks(),
            initial_upload_stage=None,
            on_uploading=stage_upload,
            on_before_flush=lambda: context.log("Flushing deployed payload to disk..."),
            on_verified=log_payload_verification,
        )
    except ValueError as exc:
        raise AppOperationError(str(exc), code="validation_failed") from exc
    except DeviceError as exc:
        raise AppOperationError(str(exc), code="remote_error") from exc

    try:
        completion = complete_deployment_after_upload(
            connection,
            prepared_plan,
            no_wait=no_wait,
            callbacks=context.to_operation_callbacks(),
            messages=DeployCompletionMessages(),
            verify_runtime_func=_verify_runtime_for_service,
        )
    except RebootFlowError as exc:
        raise AppOperationError(str(exc), code="remote_error") from exc
    except DeviceError as exc:
        raise AppOperationError(str(exc), code="remote_error") from exc
    return OperationResult(True, _deploy_completion_payload(completion))


def verify_runtime(
    context: AppOperationContext,
    connection: SshConnection,
    *,
    stage: str,
    timeout_seconds: int,
    failure_message: str = "Managed runtime did not become ready.",
) -> None:
    try:
        _verify_runtime_for_service(
            connection,
            callbacks=context.to_operation_callbacks(),
            stage=stage,
            timeout_seconds=timeout_seconds,
            heading="Waiting for managed runtime to finish starting...",
            failure_message=failure_message,
        )
    except DeviceError as exc:
        raise AppOperationError(
            str(exc),
            code="remote_error",
        ) from exc
