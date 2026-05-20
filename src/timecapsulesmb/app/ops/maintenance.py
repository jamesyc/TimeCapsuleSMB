from __future__ import annotations

import argparse
import shlex
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from timecapsulesmb.app.contracts import (
    activation_plan_payload,
    activation_result_payload,
    fsck_result_payload,
    repair_xattrs_payload,
    uninstall_plan_payload,
    uninstall_result_payload,
)
from timecapsulesmb.app.events import EventSink
from timecapsulesmb.app.ops.deploy import (
    load_config_and_target,
    request_reboot_and_wait,
    require_supported_payload,
    verify_runtime,
)
from timecapsulesmb.cli import repair_xattrs as repair_xattrs_cli
from timecapsulesmb.cli.fsck import (
    FSCK_REMOTE_COMMAND_TIMEOUT_SECONDS,
    build_remote_fsck_script,
    select_fsck_target,
    _target_from_volume,
)
from timecapsulesmb.cli.runtime import load_env_config, load_optional_env_config, resolve_env_connection
from timecapsulesmb.core.config import MANAGED_PAYLOAD_DIR_NAME
from timecapsulesmb.core.errors import system_exit_message
from timecapsulesmb.core.messages import NETBSD4_REBOOT_FOLLOWUP
from timecapsulesmb.deploy.dry_run import uninstall_plan_to_jsonable
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
    confirm_param,
    jsonable,
    optional_int_param,
    string_param,
)
from timecapsulesmb.services.deploy import DEPLOY_REBOOT_UP_TIMEOUT_MESSAGE
from timecapsulesmb.services.maintenance import FSCK_REBOOT_NO_DOWN_MESSAGE, UNINSTALL_REBOOT_NO_DOWN_MESSAGE
from timecapsulesmb.transport.ssh import SshConnection, run_ssh


def activate_operation(params: dict[str, object], sink: EventSink) -> OperationResult:
    operation = "activate"
    confirm_activation = confirm_param(params, "confirm_netbsd4_activation")
    dry_run = bool_param(params, "dry_run")
    _, target = load_config_and_target(operation, params, sink, profile="activate", include_probe=True)
    compatibility = require_supported_payload(target, allow_unsupported=False)
    if not is_netbsd4_payload_family(compatibility.payload_family):
        raise AppOperationError(
            "activate is only supported for NetBSD4 AirPort storage devices; use deploy for persistent NetBSD6 installs.",
            code="unsupported_device",
        )
    sink.stage(operation, "build_activation_plan")
    plan = build_netbsd4_activation_plan()
    if dry_run:
        return OperationResult(True, activation_plan_payload(jsonable(plan)))
    if not confirm_activation:
        raise AppOperationError("NetBSD4 activation requires explicit confirmation.", code="confirmation_required")
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
    confirm_uninstall = confirm_param(params, "confirm_uninstall")
    confirm_reboot = confirm_param(params, "confirm_reboot")
    if not dry_run and not confirm_uninstall:
        raise AppOperationError("Uninstall requires explicit confirmation.", code="confirmation_required")
    if not dry_run and not no_reboot and not confirm_reboot:
        raise AppOperationError("Uninstall requires confirmation to reboot the device.", code="confirmation_required")
    sink.stage(operation, "load_config")
    config = load_env_config(env_path=config_path(params))
    sink.stage(operation, "resolve_connection")
    connection = resolve_env_connection(config, allow_empty_password=True)
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
            wait_seconds=DEFAULT_APPLE_MOUNT_WAIT_SECONDS,
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
        return OperationResult(True, uninstall_result_payload(rebooted=False, verified=False))
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
    return OperationResult(True, uninstall_result_payload(rebooted=True, verified=True))


def fsck_operation(params: dict[str, object], sink: EventSink) -> OperationResult:
    operation = "fsck"
    confirm_fsck = confirm_param(params, "confirm_fsck")
    no_reboot = bool_param(params, "no_reboot")
    no_wait = bool_param(params, "no_wait")
    if not confirm_fsck:
        raise AppOperationError("fsck requires explicit confirmation.", code="confirmation_required")
    sink.stage(operation, "load_config")
    config = load_env_config(env_path=config_path(params))
    sink.stage(operation, "resolve_connection")
    connection = resolve_env_connection(config, allow_empty_password=True)
    sink.stage(operation, "read_mast")
    mast_volumes = read_mast_volumes_conn(connection)
    sink.stage(operation, "mount_hfs_volumes")
    mounted_volumes = mounted_mast_volumes_conn(
        connection,
        mast_volumes,
        wait_seconds=DEFAULT_APPLE_MOUNT_WAIT_SECONDS,
    )
    sink.stage(operation, "select_fsck_volume")
    try:
        target = select_fsck_target(
            tuple(_target_from_volume(volume) for volume in mounted_volumes),
            string_param(params, "volume") or None,
            prompt=False,
        )
    except RuntimeError as exc:
        raise AppOperationError(str(exc), code="validation_failed") from exc
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
        ))
    if no_wait:
        return OperationResult(True, fsck_result_payload(
            device=target.device,
            mountpoint=target.mountpoint,
            waited=False,
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
        waited=True,
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


class RepairContext:
    def __init__(self, operation: str, sink: EventSink) -> None:
        self.operation = operation
        self.sink = sink
        self.result = "failure"
        self.error: str | None = None

    def set_stage(self, stage: str) -> None:
        self.sink.stage(self.operation, stage)

    def update_fields(self, **_fields: object) -> None:
        pass

    def succeed(self) -> None:
        self.result = "success"

    def fail_with_error(self, message: str) -> None:
        self.result = "failure"
        self.error = message


class StreamLogCapture:
    def __init__(self, operation: str, sink: EventSink, *, level: str) -> None:
        self.operation = operation
        self.sink = sink
        self.level = level
        self._buffer = ""

    def write(self, text: str) -> int:
        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            self._emit(line)
        return len(text)

    def flush(self) -> None:
        if self._buffer:
            self._emit(self._buffer)
            self._buffer = ""

    def _emit(self, line: str) -> None:
        message = line.rstrip("\r")
        if message:
            self.sink.log(self.operation, message, level=self.level)


def repair_xattrs_operation(params: dict[str, object], sink: EventSink) -> OperationResult:
    operation = "repair-xattrs"
    dry_run = bool_param(params, "dry_run")
    confirm_repair = confirm_param(params, "confirm_repair")
    if not dry_run and not confirm_repair:
        raise AppOperationError(
            "repair-xattrs requires dry_run or explicit confirmation.",
            code="confirmation_required",
        )
    sink.stage(operation, "platform_check")
    if sys.platform != "darwin":
        raise AppOperationError(
            "repair-xattrs must be run on macOS because it uses xattr/chflags on the mounted SMB share.",
            code="validation_failed",
        )
    sink.stage(operation, "validate_params")
    config = load_optional_env_config(env_path=config_path(params))
    args = argparse.Namespace(
        path=Path(str(params["path"])) if params.get("path") else None,
        dry_run=dry_run,
        yes=confirm_repair,
        recursive=bool_param(params, "recursive", True),
        max_depth=optional_int_param(params, "max_depth"),
        include_hidden=bool_param(params, "include_hidden"),
        include_time_machine=bool_param(params, "include_time_machine"),
        fix_permissions=bool_param(params, "fix_permissions"),
        verbose=bool_param(params, "verbose"),
    )
    context = RepairContext(operation, sink)
    stdout_capture = StreamLogCapture(operation, sink, level="info")
    stderr_capture = StreamLogCapture(operation, sink, level="warning")
    try:
        with redirect_stdout(stdout_capture), redirect_stderr(stderr_capture):
            result = repair_xattrs_cli.run_repair_structured(
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
        "summary": jsonable(result.summary),
        "report": result.report,
        "telemetry_result": context.result,
        "error": context.error,
    }))
