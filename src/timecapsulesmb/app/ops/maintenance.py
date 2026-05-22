from __future__ import annotations

import argparse
import shlex
import sys
from contextlib import redirect_stderr, redirect_stdout

from timecapsulesmb.app.contracts import (
    activation_plan_payload,
    activation_result_payload,
    fsck_plan_payload,
    fsck_result_payload,
    fsck_volume_list_payload,
    repair_xattrs_payload,
    uninstall_plan_payload,
    uninstall_result_payload,
)
from timecapsulesmb.app.confirmations import build_confirmation, require_confirmation
from timecapsulesmb.app.events import EventSink
from timecapsulesmb.app.ops.deploy import (
    request_reboot,
    request_reboot_and_wait,
    require_supported_payload,
    verify_runtime,
)
from timecapsulesmb.core.config import MANAGED_PAYLOAD_DIR_NAME
from timecapsulesmb.core.errors import system_exit_message
from timecapsulesmb.core.messages import NETBSD4_REBOOT_FOLLOWUP
from timecapsulesmb.deploy.dry_run import activation_plan_to_jsonable, uninstall_plan_to_jsonable
from timecapsulesmb.deploy.executor import remote_uninstall_payload, run_remote_actions
from timecapsulesmb.deploy.planner import (
    DEFAULT_APPLE_MOUNT_WAIT_SECONDS,
    build_netbsd4_activation_plan,
    build_uninstall_plan,
)
from timecapsulesmb.deploy.verify import render_post_uninstall_verification, verify_post_uninstall
from timecapsulesmb.device.compat import is_netbsd4_payload_family
from timecapsulesmb.device.probe import probe_managed_runtime_conn, wait_for_ssh_state_conn
from timecapsulesmb.device.storage import (
    UNINSTALL_DRY_RUN_VOLUME_ROOT_PLACEHOLDER,
    mounted_mast_volumes_conn,
    read_mast_volumes_conn,
)
from timecapsulesmb.services.app import (
    AppOperationError,
    OperationResult,
    bool_param,
    config_path,
    int_param,
    jsonable,
    optional_int_param,
    required_path_param,
    string_param,
)
from timecapsulesmb.services.credentials import overlay_request_credentials
from timecapsulesmb.services.deploy import DEPLOY_REBOOT_UP_TIMEOUT_MESSAGE
from timecapsulesmb.services.maintenance import (
    FSCK_REMOTE_COMMAND_TIMEOUT_SECONDS,
    FSCK_REBOOT_NO_DOWN_MESSAGE,
    UNINSTALL_REBOOT_NO_DOWN_MESSAGE,
    LineLogCapture,
    RepairExecutionContext,
    build_remote_fsck_script,
    format_fsck_plan,
    format_fsck_targets,
    fsck_plan_to_jsonable,
    fsck_target_from_volume,
    fsck_target_to_jsonable,
    select_fsck_target,
)
from timecapsulesmb.services import repair_xattrs as repair_xattrs_service
from timecapsulesmb.services.runtime import (
    load_env_config,
    load_optional_env_config,
    resolve_env_connection,
    resolve_validated_managed_target,
)
from timecapsulesmb.transport.ssh import SshConnection, run_ssh


def activate_operation(params: dict[str, object], sink: EventSink) -> OperationResult:
    operation = "activate"
    dry_run = bool_param(params, "dry_run")
    sink.stage(operation, "build_activation_plan")
    plan = build_netbsd4_activation_plan()
    if dry_run:
        return OperationResult(True, activation_plan_payload(activation_plan_to_jsonable(plan)))

    sink.stage(operation, "load_config")
    config = overlay_request_credentials(load_env_config(env_path=config_path(params)), params)
    confirmation_connection = resolve_env_connection(config, allow_empty_password=True)
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
        legacy_names=("confirm_netbsd4_activation",),
    )

    sink.stage(operation, "resolve_managed_target")
    target = resolve_validated_managed_target(
        config,
        command_name=operation,
        profile="activate",
        include_probe=True,
    )
    compatibility = require_supported_payload(target, allow_unsupported=False)
    if not is_netbsd4_payload_family(compatibility.payload_family):
        raise AppOperationError(
            "activate is only supported for NetBSD4 AirPort storage devices; use deploy for persistent NetBSD6 installs.",
            code="unsupported_device",
        )
    connection = target.connection
    sink.stage(operation, "probe_runtime")
    if probe_managed_runtime_conn(connection, timeout_seconds=20).ready:
        return OperationResult(True, activation_result_payload(already_active=True))

    sink.stage(operation, "run_activation")
    run_remote_actions(connection, plan.actions)
    verify_runtime(operation, sink, connection, stage="verify_runtime_activation", timeout_seconds=180)
    return OperationResult(True, activation_result_payload(
        already_active=False,
        message=f"NetBSD4 activation complete. {NETBSD4_REBOOT_FOLLOWUP}",
    ))


def uninstall_operation(params: dict[str, object], sink: EventSink) -> OperationResult:
    operation = "uninstall"
    dry_run = bool_param(params, "dry_run")
    no_reboot = bool_param(params, "no_reboot")
    no_wait = bool_param(params, "no_wait")
    mount_wait = int_param(params, "mount_wait", DEFAULT_APPLE_MOUNT_WAIT_SECONDS)
    sink.stage(operation, "load_config")
    config = overlay_request_credentials(load_env_config(env_path=config_path(params)), params)
    sink.stage(operation, "resolve_connection")
    connection = resolve_env_connection(config, allow_empty_password=True)
    if not dry_run:
        presentation_id = "uninstall.no_reboot" if no_reboot else "uninstall.reboot"
        presentation_values = {
            "requires_reboot": not no_reboot,
            "no_reboot": no_reboot,
            "no_wait": no_wait,
        }
        require_confirmation(
            params,
            build_confirmation(
                operation=operation,
                params=params,
                title="Confirm uninstall",
                message=(
                    "Remove managed TimeCapsuleSMB files from the device"
                    + (" and reboot it?" if not no_reboot else "?")
                ),
                action_title="Uninstall",
                risk="destructive" if not no_reboot else "remote_write",
                summary="Uninstall managed payload" + (" with reboot" if not no_reboot else " without reboot"),
                context={
                    "host": connection.host,
                    "requires_reboot": not no_reboot,
                    "no_reboot": no_reboot,
                    "no_wait": no_wait,
                },
                presentation_id=presentation_id,
                presentation_values=presentation_values,
            ),
            legacy_names=("confirm_uninstall",) if no_reboot else ("confirm_uninstall", "confirm_reboot"),
        )
    if dry_run:
        volume_roots = [UNINSTALL_DRY_RUN_VOLUME_ROOT_PLACEHOLDER]
        payload_dirs = [f"{UNINSTALL_DRY_RUN_VOLUME_ROOT_PLACEHOLDER}/{MANAGED_PAYLOAD_DIR_NAME}"]
    else:
        sink.stage(operation, "read_mast")
        mast_volumes = read_mast_volumes_conn(connection)
        sink.stage(operation, "mount_mast_volumes")
        mounted_volumes = mounted_mast_volumes_conn(
            connection,
            mast_volumes,
            wait_seconds=mount_wait,
        )
        volume_roots = [volume.volume_root for volume in mounted_volumes]
        payload_dirs = [f"{volume_root}/{MANAGED_PAYLOAD_DIR_NAME}" for volume_root in volume_roots]
    sink.stage(operation, "build_uninstall_plan")
    plan = build_uninstall_plan(connection.host, volume_roots, payload_dirs, reboot_after_uninstall=not no_reboot)
    if dry_run:
        return OperationResult(True, uninstall_plan_payload(uninstall_plan_to_jsonable(plan)))
    sink.stage(operation, "uninstall_payload")
    remote_uninstall_payload(connection, plan)
    if no_reboot:
        return OperationResult(True, uninstall_result_payload(
            rebooted=False,
            verified=False,
            reboot_requested=False,
            waited=False,
        ))
    if no_wait:
        request_reboot(
            operation,
            sink,
            connection,
            strategy="acp_then_ssh",
            raise_on_request_error=True,
        )
        return OperationResult(True, uninstall_result_payload(
            rebooted=False,
            verified=False,
            reboot_requested=True,
            waited=False,
        ))
    request_reboot_and_wait(
        operation,
        sink,
        connection,
        strategy="acp_then_ssh",
        reboot_no_down_message=UNINSTALL_REBOOT_NO_DOWN_MESSAGE,
    )
    sink.stage(operation, "verify_post_uninstall")
    verification = verify_post_uninstall(connection, plan)
    for line in render_post_uninstall_verification(verification):
        sink.log(operation, line)
    if not verification:
        raise AppOperationError("Managed TimeCapsuleSMB files are still present after reboot.", code="remote_error")
    return OperationResult(True, uninstall_result_payload(
        rebooted=True,
        verified=True,
        reboot_requested=True,
        waited=True,
    ))


def fsck_operation(params: dict[str, object], sink: EventSink) -> OperationResult:
    operation = "fsck"
    dry_run = bool_param(params, "dry_run")
    list_volumes = bool_param(params, "list_volumes")
    no_reboot = bool_param(params, "no_reboot")
    no_wait = bool_param(params, "no_wait")
    mount_wait = int_param(params, "mount_wait", DEFAULT_APPLE_MOUNT_WAIT_SECONDS)
    if dry_run and list_volumes:
        raise AppOperationError("dry_run and list_volumes are mutually exclusive.", code="validation_failed")
    if not dry_run and not list_volumes:
        presentation_id = "fsck.no_reboot" if no_reboot else "fsck.reboot"
        volume = string_param(params, "volume")
        require_confirmation(
            params,
            build_confirmation(
                operation=operation,
                params=params,
                title="Confirm fsck",
                message=(
                    "Run fsck on the selected HFS volume"
                    + (" and reboot the device?" if not no_reboot else "?")
                ),
                action_title="Run fsck",
                risk="destructive" if not no_reboot else "remote_write",
                summary="Filesystem check and repair",
                context={
                    "volume": volume,
                    "requires_reboot": not no_reboot,
                    "no_reboot": no_reboot,
                    "no_wait": no_wait,
                },
                presentation_id=presentation_id,
                presentation_values={
                    "volume": volume,
                    "requires_reboot": not no_reboot,
                    "no_reboot": no_reboot,
                    "no_wait": no_wait,
                },
            ),
            legacy_names=("confirm_fsck",),
        )
    sink.stage(operation, "load_config")
    config = overlay_request_credentials(load_env_config(env_path=config_path(params)), params)
    sink.stage(operation, "resolve_connection")
    connection = resolve_env_connection(config, allow_empty_password=True)
    sink.stage(operation, "read_mast")
    mast_volumes = read_mast_volumes_conn(connection)
    sink.stage(operation, "mount_hfs_volumes")
    mounted_volumes = mounted_mast_volumes_conn(
        connection,
        mast_volumes,
        wait_seconds=mount_wait,
    )
    targets = tuple(fsck_target_from_volume(volume) for volume in mounted_volumes)
    if list_volumes:
        sink.stage(operation, "list_fsck_volumes")
        sink.log(operation, format_fsck_targets(targets))
        return OperationResult(True, fsck_volume_list_payload({
            "targets": [fsck_target_to_jsonable(target) for target in targets],
        }))

    sink.stage(operation, "select_fsck_volume")
    try:
        target = select_fsck_target(
            targets,
            string_param(params, "volume") or None,
            prompt=False,
        )
    except RuntimeError as exc:
        raise AppOperationError(str(exc), code="validation_failed") from exc
    if dry_run:
        sink.log(operation, format_fsck_plan(target, reboot=not no_reboot, wait=not no_wait))
        return OperationResult(True, fsck_plan_payload(fsck_plan_to_jsonable(
            target,
            reboot=not no_reboot,
            wait=not no_wait,
        )))

    sink.stage(operation, "run_fsck")
    script = build_remote_fsck_script(target.device, target.mountpoint, reboot=not no_reboot)
    proc = run_ssh(
        connection,
        f"/bin/sh -c {shlex.quote(script)}",
        check=False,
        timeout=FSCK_REMOTE_COMMAND_TIMEOUT_SECONDS,
    )
    if proc.stdout:
        for line in proc.stdout.splitlines():
            sink.log(operation, line)
    if no_reboot:
        return OperationResult(proc.returncode == 0, fsck_result_payload(
            device=target.device,
            mountpoint=target.mountpoint,
            returncode=proc.returncode,
            reboot_requested=False,
            waited=False,
            verified=False,
        ))
    if no_wait:
        return OperationResult(True, fsck_result_payload(
            device=target.device,
            mountpoint=target.mountpoint,
            reboot_requested=True,
            waited=False,
            verified=False,
        ))
    observe_reboot_cycle(
        operation,
        sink,
        connection,
        reboot_no_down_message=FSCK_REBOOT_NO_DOWN_MESSAGE,
        down_timeout_seconds=90,
        up_timeout_seconds=420,
    )
    return OperationResult(True, fsck_result_payload(
        device=target.device,
        mountpoint=target.mountpoint,
        reboot_requested=True,
        waited=True,
        verified=True,
    ))


def observe_reboot_cycle(
    operation: str,
    sink: EventSink,
    connection: SshConnection,
    *,
    reboot_no_down_message: str,
    down_timeout_seconds: int,
    up_timeout_seconds: int,
) -> None:
    sink.stage(operation, "wait_for_reboot_down")
    if not wait_for_ssh_state_conn(connection, expected_up=False, timeout_seconds=down_timeout_seconds):
        raise AppOperationError(reboot_no_down_message, code="remote_error")
    sink.stage(operation, "wait_for_reboot_up")
    if not wait_for_ssh_state_conn(connection, expected_up=True, timeout_seconds=up_timeout_seconds):
        raise AppOperationError(DEPLOY_REBOOT_UP_TIMEOUT_MESSAGE, code="remote_error")


def repair_xattrs_operation(params: dict[str, object], sink: EventSink) -> OperationResult:
    operation = "repair-xattrs"
    sink.stage(operation, "validate_params")
    dry_run = bool_param(params, "dry_run")
    path = required_path_param(params, "path")
    recursive = bool_param(params, "recursive", True)
    max_depth = optional_int_param(params, "max_depth")
    include_hidden = bool_param(params, "include_hidden")
    include_time_machine = bool_param(params, "include_time_machine")
    fix_permissions = bool_param(params, "fix_permissions")
    verbose = bool_param(params, "verbose")
    if not dry_run:
        require_confirmation(
            params,
            build_confirmation(
                operation=operation,
                params=params,
                title="Confirm xattr repair",
                message=f"Repair known-safe macOS metadata issues under {path}?",
                action_title="Repair xattrs",
                risk="local_write",
                summary="Repair local mounted-share metadata",
                context={"path": str(path)},
                presentation_id="repair_xattrs",
                presentation_values={"path": str(path)},
            ),
            legacy_names=("confirm_repair",),
        )
    sink.stage(operation, "platform_check")
    if sys.platform != "darwin":
        raise AppOperationError(
            "repair-xattrs must be run on macOS because it uses xattr/chflags on the mounted SMB share.",
            code="validation_failed",
        )
    config = load_optional_env_config(env_path=config_path(params))
    args = argparse.Namespace(
        path=path,
        dry_run=dry_run,
        yes=not dry_run,
        recursive=recursive,
        max_depth=max_depth,
        include_hidden=include_hidden,
        include_time_machine=include_time_machine,
        fix_permissions=fix_permissions,
        verbose=verbose,
    )
    context = RepairExecutionContext(lambda stage: sink.stage(operation, stage))
    stdout_capture = LineLogCapture(lambda message: sink.log(operation, message, level="info"))
    stderr_capture = LineLogCapture(lambda message: sink.log(operation, message, level="warning"))
    try:
        with redirect_stdout(stdout_capture), redirect_stderr(stderr_capture):
            result = repair_xattrs_service.run_repair_structured(
                args,
                context,
                config,
                emit_log=lambda message: sink.log(operation, message),
            )
    except SystemExit as exc:
        message = system_exit_message(exc) or "repair-xattrs failed"
        raise AppOperationError(message, code="operation_failed") from exc
    finally:
        stdout_capture.flush()
        stderr_capture.flush()
    return OperationResult(result.returncode == 0, repair_xattrs_payload({
        "returncode": result.returncode,
        "root": str(result.root),
        "finding_count": len(result.findings),
        "repairable_count": len(result.candidates),
        "stats": jsonable(result.summary),
        "report": result.report,
        "telemetry_result": context.result,
        "error": context.error,
    }))
