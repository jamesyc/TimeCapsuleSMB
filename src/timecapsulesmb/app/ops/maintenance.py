from __future__ import annotations

from timecapsulesmb.app.context import AppOperationContext
from timecapsulesmb.app.contracts import (
    activation_plan_payload,
    activation_result_payload,
    fsck_plan_payload,
    fsck_result_payload,
    fsck_volume_list_payload,
    uninstall_plan_payload,
    uninstall_result_payload,
)
from timecapsulesmb.app.confirmations import build_confirmation, disk_failing_confirmation_fields, require_confirmation
from timecapsulesmb.app.ops.common import (
    load_request_config,
    require_request_acpd_ready,
    resolve_request_connection,
    resolve_request_target,
)
from timecapsulesmb.app.ops.deploy import device_operation_error
from timecapsulesmb.core.error_codes import ErrorCode
from timecapsulesmb.core.messages import netbsd4_activation_summary
from timecapsulesmb.deploy.dry_run import activation_plan_to_jsonable, uninstall_plan_to_jsonable
from timecapsulesmb.deploy.executor import remote_uninstall_payload
from timecapsulesmb.deploy.planner import (
    DEFAULT_APPLE_MOUNT_WAIT_SECONDS,
    build_runtime_activation_plan,
)
from timecapsulesmb.device.compat import is_netbsd4_payload_family
from timecapsulesmb.device.disk_errors import read_disk_failing_evidence_conn
from timecapsulesmb.device.errors import DeviceError
from timecapsulesmb.services.app import (
    AppOperationError,
    OperationResult,
    bool_param,
    int_param,
    string_param,
)
from timecapsulesmb.services.reboot import RebootFlowError
from timecapsulesmb.services.activation import activate_runtime, installed_netbsd4_autostart
from timecapsulesmb.services.maintenance import (
    FSCK_DID_NOT_RUN_MESSAGE,
    format_fsck_plan,
    format_fsck_targets,
    fsck_plan_to_jsonable,
    fsck_target_from_volume,
    fsck_target_to_jsonable,
    prepare_uninstall,
    reboot_after_uninstall,
    run_fsck,
    select_fsck_target,
)
from timecapsulesmb.services.deploy import require_supported_payload
from timecapsulesmb.services import storage as storage_service
from timecapsulesmb.transport.ssh import SshConnection
from timecapsulesmb.services.runtime import (
    probe_failure_error,
    probe_managed_connection_state,
    resolve_env_connection,
)


def activate_operation(params: dict[str, object], context: AppOperationContext) -> OperationResult:
    operation = "activate"
    dry_run = bool_param(params, "dry_run")
    context.stage("build_activation_plan")
    plan = build_runtime_activation_plan()
    if dry_run:
        return OperationResult(True, activation_plan_payload(activation_plan_to_jsonable(plan)))

    config = load_request_config(params, context)
    confirmation_connection = resolve_request_connection(config, context, allow_empty_password=True)
    require_confirmation(
        params,
        build_confirmation(
            operation=operation,
            params=params,
            title="Confirm NetBSD4 activation",
            message="Activate the deployed NetBSD4 payload and restart managed services?",
            action_title="Activate",
            risk="destructive",
            summary="NetBSD4 service activation",
            context={
                "host": confirmation_connection.host,
                "netbsd4": True,
            },
            presentation_id="activate.netbsd4",
            presentation_values={"netbsd4": True},
        ),
    )

    target = resolve_request_target(config, context, profile="activate", include_probe=True)
    try:
        compatibility = require_supported_payload(target, allow_unsupported=False)
    except DeviceError as exc:
        # The error's own code: an AirPort Express gets the unsupported-device
        # guidance, and a device whose SSH is off or unreachable gets its own.
        raise device_operation_error(context, exc) from exc
    if not is_netbsd4_payload_family(compatibility.payload_family):
        raise AppOperationError(
            "activate is only supported for NetBSD4 AirPort storage devices; use deploy for persistent NetBSD6 installs.",
            code=ErrorCode.UNSUPPORTED_DEVICE,
        )
    try:
        decision = activate_runtime(target.connection, plan.actions, callbacks=context.to_operation_callbacks())
    except DeviceError as exc:
        raise device_operation_error(context, exc) from exc
    if not decision.run_actions:
        return OperationResult(True, activation_result_payload(already_active=True))
    return OperationResult(True, activation_result_payload(
        already_active=False,
        summary=netbsd4_activation_summary(),
    ))


def uninstall_operation(params: dict[str, object], context: AppOperationContext) -> OperationResult:
    operation = "uninstall"
    dry_run = bool_param(params, "dry_run")
    no_reboot = bool_param(params, "no_reboot")
    no_wait = bool_param(params, "no_wait")
    mount_wait = int_param(params, "mount_wait", DEFAULT_APPLE_MOUNT_WAIT_SECONDS)
    config = load_request_config(params, context)
    # The reboot goes through AirPort ACP, which needs the password.
    connection = resolve_request_connection(config, context, allow_empty_password=no_reboot or dry_run)
    if not dry_run and not no_reboot:
        require_request_acpd_ready(context, connection)
    if not dry_run:
        disk_failing = read_disk_failing_evidence_conn(connection)
        presentation = {
            "title": "Confirm uninstall",
            "message": "Remove managed TimeCapsuleSMB files from the device" + (" and reboot it?" if not no_reboot else "?"),
            "action_title": "Uninstall",
            "presentation_id": "uninstall.no_reboot" if no_reboot else "uninstall.reboot",
        }
        if disk_failing:
            context.update_fields(disk_failing_warned=True)
            presentation.update(disk_failing_confirmation_fields(operation, disk_failing))
        presentation_values = {
            "requires_reboot": not no_reboot,
            "no_reboot": no_reboot,
            "no_wait": no_wait,
            **({"disk_failing": True} if disk_failing else {}),
        }
        require_confirmation(
            params,
            build_confirmation(
                operation=operation,
                params=params,
                risk="destructive" if not no_reboot else "remote_write",
                summary="Uninstall managed payload" + (" with reboot" if not no_reboot else " without reboot"),
                context={
                    "host": connection.host,
                    "requires_reboot": not no_reboot,
                    "no_reboot": no_reboot,
                    "no_wait": no_wait,
                    **({"disk_failing": True} if disk_failing else {}),
                },
                presentation_values=presentation_values,
                **presentation,
            ),
        )
    plan = prepare_uninstall(
        connection,
        dry_run=dry_run,
        reboot=not no_reboot,
        wait=not no_wait,
        mount_wait=mount_wait,
        callbacks=context.to_operation_callbacks(),
    )
    if dry_run:
        return OperationResult(True, uninstall_plan_payload(uninstall_plan_to_jsonable(plan)))
    context.stage("uninstall_payload")
    remote_uninstall_payload(connection, plan)
    try:
        verified = reboot_after_uninstall(connection, plan, callbacks=context.to_operation_callbacks())
    except RebootFlowError as exc:
        raise AppOperationError(str(exc), code=exc.code) from exc
    except DeviceError as exc:
        raise AppOperationError(str(exc), code=ErrorCode.REMOTE_ERROR) from exc
    return OperationResult(True, uninstall_result_payload(
        rebooted=verified,
        verified=verified,
        reboot_requested=plan.reboot_required,
        waited=verified,
    ))


def _fsck_connection(params: dict[str, object], context: AppOperationContext, *, rebooting: bool) -> SshConnection:
    config = load_request_config(params, context)
    context.stage("resolve_connection")
    # The reboot goes through AirPort ACP, which needs the password.
    connection = resolve_env_connection(config, allow_empty_password=not rebooting)
    context.connection = connection
    return connection


def fsck_operation(params: dict[str, object], context: AppOperationContext) -> OperationResult:
    operation = "fsck"
    dry_run = bool_param(params, "dry_run")
    list_volumes = bool_param(params, "list_volumes")
    no_reboot = bool_param(params, "no_reboot")
    no_wait = bool_param(params, "no_wait")
    mount_wait = int_param(params, "mount_wait", DEFAULT_APPLE_MOUNT_WAIT_SECONDS)
    if dry_run and list_volumes:
        raise AppOperationError("dry_run and list_volumes are mutually exclusive.", code=ErrorCode.VALIDATION_FAILED)
    rebooting = not (no_reboot or dry_run or list_volumes)
    connection = None
    if rebooting:
        # The reboot goes through ACP: check ACPd and the password before the
        # confirmation and before the disk is touched.
        connection = _fsck_connection(params, context, rebooting=True)
        require_request_acpd_ready(context, connection)
    if not dry_run and not list_volumes:
        volume = string_param(params, "volume")
        # The preflight reads the device before the confirmation.
        if connection is None:
            connection = _fsck_connection(params, context, rebooting=rebooting)
        disk_failing = read_disk_failing_evidence_conn(connection)
        presentation = {
            "title": "Confirm fsck",
            "message": "Run fsck on the selected HFS volume" + (" and reboot the device?" if not no_reboot else "?"),
            "action_title": "Run fsck",
            "presentation_id": "fsck.no_reboot" if no_reboot else "fsck.reboot",
        }
        if disk_failing:
            context.update_fields(disk_failing_warned=True)
            presentation.update(disk_failing_confirmation_fields(operation, disk_failing))
        require_confirmation(
            params,
            build_confirmation(
                operation=operation,
                params=params,
                risk="destructive" if not no_reboot else "remote_write",
                summary="Filesystem check and repair",
                context={
                    "volume": volume,
                    "requires_reboot": not no_reboot,
                    "no_reboot": no_reboot,
                    "no_wait": no_wait,
                    **({"disk_failing": True} if disk_failing else {}),
                },
                presentation_values={
                    "volume": volume,
                    "requires_reboot": not no_reboot,
                    "no_reboot": no_reboot,
                    "no_wait": no_wait,
                    **({"disk_failing": True} if disk_failing else {}),
                },
                **presentation,
            ),
        )
    if connection is None:
        connection = _fsck_connection(params, context, rebooting=rebooting)
    mounted_volumes = storage_service.mount_mast_volumes_with_diagnostics(
        connection,
        callbacks=context.to_operation_callbacks(),
        wait_seconds=mount_wait,
        mount_stage="mount_hfs_volumes",
    )
    targets = tuple(fsck_target_from_volume(volume) for volume in mounted_volumes)
    if list_volumes:
        context.stage("list_fsck_volumes")
        context.log(format_fsck_targets(targets))
        return OperationResult(True, fsck_volume_list_payload({
            "targets": [fsck_target_to_jsonable(target) for target in targets],
        }))

    context.stage("select_fsck_volume")
    try:
        target = select_fsck_target(
            targets,
            string_param(params, "volume") or None,
        )
    except RuntimeError as exc:
        raise AppOperationError(str(exc), code=ErrorCode.VALIDATION_FAILED) from exc
    context.update_fields(fsck_device=target.device, fsck_mountpoint=target.mountpoint)
    if dry_run:
        context.log(format_fsck_plan(target, reboot=not no_reboot, wait=not no_wait))
        return OperationResult(True, fsck_plan_payload(fsck_plan_to_jsonable(
            target,
            reboot=not no_reboot,
            wait=not no_wait,
        )))

    netbsd4_autostart = None
    if not no_reboot:
        # Whether file sharing must be started after the reboot, read before
        # the reboot as deploy does.
        probe_state = probe_managed_connection_state(connection)
        context.apply_probe_state(probe_state)
        if probe_state.compatibility is None:
            raise device_operation_error(context, probe_failure_error(probe_state.probe_result, connection.host))
        netbsd4_autostart = installed_netbsd4_autostart(connection, probe_state, context.to_operation_callbacks())
    try:
        outcome = run_fsck(
            connection,
            target,
            reboot=not no_reboot,
            wait=not no_wait,
            callbacks=context.to_operation_callbacks(),
            netbsd4_autostart=netbsd4_autostart,
        )
    except RebootFlowError as exc:
        raise AppOperationError(str(exc), code=exc.code) from exc
    if outcome.status is None:
        raise AppOperationError(outcome.failure or FSCK_DID_NOT_RUN_MESSAGE, code=outcome.failure_code or ErrorCode.REMOTE_ERROR)
    if outcome.runtime_restart_error is not None:
        raise AppOperationError(
            outcome.failure or outcome.runtime_restart_error,
            code=outcome.runtime_restart_code or ErrorCode.RUNTIME_NOT_RESTARTED,
        )
    if outcome.failure is not None:
        context.set_error(outcome.failure)
    return OperationResult(outcome.failure is None, fsck_result_payload(
        device=target.device,
        mountpoint=target.mountpoint,
        returncode=outcome.status,
        reboot_requested=outcome.reboot_requested,
        waited=outcome.waited,
        verified=outcome.waited,
        error=outcome.failure,
    ))
