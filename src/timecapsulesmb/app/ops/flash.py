from __future__ import annotations

from pathlib import Path

from timecapsulesmb.app.context import AppOperationContext
from timecapsulesmb.app.confirmations import build_confirmation, require_confirmation
from timecapsulesmb.app.contracts import flash_backup_payload, flash_plan_payload, flash_write_payload
from timecapsulesmb.app.ops.common import (
    load_request_config,
    require_request_sshpass,
    resolve_request_target,
)
from timecapsulesmb.app.ops.deploy import device_operation_error
from timecapsulesmb.core.config import AppConfig
from timecapsulesmb.device.errors import DeviceError
from timecapsulesmb.flash import FlashAnalysisError
from timecapsulesmb.flash_workflow import FlashPlan, SecondaryBankInvalidError, SecondaryBankReadMismatchError
from timecapsulesmb.services.app import (
    AppOperationError,
    OperationResult,
    bool_param,
    optional_bool_param,
    required_path_param,
    string_param,
)
from timecapsulesmb.services.flash import (
    WRITE_OPERATIONS,
    FlashTarget,
    backup_flash,
    finish_validated_write,
    plan_flash_from_backup,
    record_write_outcome,
    require_netbsd4_flash_target,
    validate_live_target_matches_backup,
    write_flash_plan,
    write_stage_for_plan,
)
from timecapsulesmb.services.runtime import (
    require_connection_compatibility,
)
from timecapsulesmb.services.reboot import RebootFlowError
from timecapsulesmb.transport.errors import TransportError


FLASH_ACTIONS = {"backup", "plan", "write"}
PLAN_OPERATIONS = {"patch", "restore", "check_apple", "download_only"}


def flash_operation(params: dict[str, object], context: AppOperationContext) -> OperationResult:
    action = string_param(params, "action", "backup").strip() or "backup"
    if action not in FLASH_ACTIONS:
        raise AppOperationError(f"Unsupported flash action: {action}", code="validation_failed")
    context.update_fields(flash_action=action)
    if action == "backup":
        return _backup_operation(params, context)
    if action == "plan":
        return _plan_operation(params, context)
    return _write_operation(params, context)


def _optional_path_param(params: dict[str, object], name: str) -> Path | None:
    value = params.get(name)
    if value in (None, ""):
        return None
    return Path(str(value)).expanduser()


def _firmware_version_param(params: dict[str, object]) -> str | None:
    value = string_param(params, "firmware_version").strip()
    return value or None


def _plan_operation_param(params: dict[str, object]) -> str:
    plan_operation = string_param(params, "mode", "patch").strip() or "patch"
    if plan_operation not in PLAN_OPERATIONS:
        raise AppOperationError(f"Unsupported flash plan mode: {plan_operation}", code="validation_failed")
    return plan_operation


def _write_operation_param(params: dict[str, object]) -> str:
    plan_operation = _plan_operation_param(params)
    if plan_operation not in WRITE_OPERATIONS:
        raise AppOperationError(f"Flash mode {plan_operation} does not write firmware", code="validation_failed")
    return plan_operation


def _write_reboot_policy(params: dict[str, object], plan_operation: str) -> tuple[bool, bool]:
    explicit_reboot = optional_bool_param(params, "reboot_after_write")
    reboot_after_write = explicit_reboot if explicit_reboot is not None else plan_operation == "restore"
    if plan_operation == "patch" and reboot_after_write:
        raise AppOperationError(
            "Flash patch cannot request reboot; power cycle manually after the validated write",
            code="validation_failed",
        )
    wait_after_reboot = bool_param(params, "wait_after_reboot", True) if reboot_after_write else False
    return reboot_after_write, wait_after_reboot


def _resolve_flash_target(config: AppConfig, context: AppOperationContext) -> FlashTarget:
    require_request_sshpass()
    target = resolve_request_target(config, context, profile="flash", include_probe=False)
    context.stage("check_compatibility")
    try:
        compatibility = require_connection_compatibility(target.connection)
    except DeviceError as exc:
        # A probe that did not log in keeps its own code (SSH off, device
        # unreachable, password rejected); it says nothing about the model.
        raise device_operation_error(context, exc) from exc
    try:
        return require_netbsd4_flash_target(
            target.connection,
            compatibility,
            update_fields=context.update_fields,
            log=context.log,
        )
    except DeviceError as exc:
        raise AppOperationError(str(exc), code="unsupported_device") from exc


def _backup_operation(params: dict[str, object], context: AppOperationContext) -> OperationResult:
    config = load_request_config(params, context)
    target = _resolve_flash_target(config, context)
    backup_dir = _optional_path_param(params, "backup_dir")
    context.update_fields(backup_dir=str(backup_dir) if backup_dir is not None else None)
    try:
        bundle = backup_flash(
            target=target,
            backup_dir=backup_dir,
            log=context.log,
            stage=context.stage,
        )
    except FlashAnalysisError as exc:
        raise AppOperationError(str(exc), code="validation_failed") from exc
    except TransportError as exc:
        raise AppOperationError(f"SSH flash read failed: {exc}", code="remote_error") from exc
    return OperationResult(True, flash_backup_payload(bundle.manifest))


def _plan_operation(params: dict[str, object], context: AppOperationContext) -> OperationResult:
    plan_operation = _plan_operation_param(params)
    force = bool_param(params, "force")
    backup_dir = required_path_param(params, "backup_dir")
    firmware_template = _optional_path_param(params, "firmware_template")
    firmware_version = _firmware_version_param(params)
    context.update_fields(
        flash_mode=plan_operation,
        force=force,
        backup_dir=str(backup_dir),
        firmware_template=str(firmware_template) if firmware_template is not None else None,
        firmware_version=firmware_version,
    )
    try:
        context.stage("inspect_backup")
        context.stage("plan_flash")
        bundle, _plan = plan_flash_from_backup(
            backup_dir=backup_dir,
            operation=plan_operation,
            force=force,
            firmware_template=firmware_template,
            firmware_version=firmware_version,
        )
    except SecondaryBankInvalidError as exc:
        raise AppOperationError(str(exc), code="secondary_bank_invalid") from exc
    except SecondaryBankReadMismatchError as exc:
        raise AppOperationError(str(exc), code="secondary_bank_read_mismatch") from exc
    except FlashAnalysisError as exc:
        raise AppOperationError(str(exc), code="validation_failed") from exc
    return OperationResult(True, flash_plan_payload(bundle.manifest))


def _confirmation_message(
    target: FlashTarget,
    mode: str,
    bank: str | None,
    *,
    reboot_after_write: bool,
    secondary_refresh: bool = False,
) -> str:
    if secondary_refresh:
        return (
            f"The backup (secondary) firmware bank on {target.acp_host} is invalid. The primary bank is not "
            "changed and no reboot is needed; keep the device powered for a few minutes. "
            "Rewrite the backup bank with Apple stock firmware?"
        )
    if mode == "patch":
        return (
            f"Patch the primary firmware bank boot hook on {target.acp_host} "
            "and acknowledge that manual power cycle is required after a successful write?"
        )
    bank_text = bank or "target"
    if reboot_after_write:
        return (
            f"Restore Apple stock firmware to the {bank_text} firmware bank on {target.acp_host} "
            "and reboot after validation?"
        )
    return f"Restore Apple stock firmware to the {bank_text} firmware bank on {target.acp_host}?"


def _secondary_refresh_context(plan: FlashPlan) -> dict[str, object]:
    """A secondary rewrite has no saved bank to name: the confirmation approves its image."""
    refresh = plan.secondary_refresh
    if refresh is None or plan.payload is None:
        return {}
    return {"image_sha256": refresh.image_sha256, "firmware_version": plan.payload.template_version}


def _write_operation(params: dict[str, object], context: AppOperationContext) -> OperationResult:
    plan_operation = _write_operation_param(params)
    reboot_after_write, wait_after_reboot = _write_reboot_policy(params, plan_operation)
    force = bool_param(params, "force")
    backup_dir = required_path_param(params, "backup_dir")
    firmware_template = _optional_path_param(params, "firmware_template")
    firmware_version = _firmware_version_param(params)
    context.update_fields(
        flash_mode=plan_operation,
        force=force,
        backup_dir=str(backup_dir),
        firmware_template=str(firmware_template) if firmware_template is not None else None,
        firmware_version=firmware_version,
        reboot_after_write=reboot_after_write,
        wait_after_reboot=wait_after_reboot,
    )

    try:
        context.stage("inspect_backup")
        context.stage("plan_flash")
        bundle, plan = plan_flash_from_backup(
            backup_dir=backup_dir,
            operation=plan_operation,
            force=force,
            firmware_template=firmware_template,
            firmware_version=firmware_version,
        )
    except SecondaryBankInvalidError as exc:
        raise AppOperationError(str(exc), code="secondary_bank_invalid") from exc
    except SecondaryBankReadMismatchError as exc:
        raise AppOperationError(str(exc), code="secondary_bank_read_mismatch") from exc
    except FlashAnalysisError as exc:
        raise AppOperationError(str(exc), code="validation_failed") from exc
    if plan is None:
        raise AppOperationError("Flash write has no plan", code="validation_failed")
    if plan.already_satisfied:
        record_write_outcome(
            bundle=bundle,
            plan=plan,
            status="not_needed",
            write_validated=False,
            write_may_have_modified_device=False,
        )
        return OperationResult(True, flash_write_payload(bundle.manifest))

    secondary_refresh = plan.secondary_refresh is not None
    if secondary_refresh:
        # The device keeps running the primary it booted; a restart adds nothing.
        reboot_after_write = False
        wait_after_reboot = False
        context.update_fields(reboot_after_write=False, wait_after_reboot=False)
    config = load_request_config(params, context)
    target = _resolve_flash_target(config, context)
    bank = plan.target_name
    context.update_fields(target_bank=bank)
    presentation_id = "flash.restore_secondary_write" if secondary_refresh else f"flash.{plan_operation}_write"
    context.stage("confirm_write")
    require_confirmation(
        params,
        build_confirmation(
            operation="flash",
            params=params,
            title="Confirm firmware flash write",
            message=_confirmation_message(
                target,
                plan_operation,
                bank,
                reboot_after_write=reboot_after_write,
                secondary_refresh=secondary_refresh,
            ),
            action_title="Write Firmware",
            risk="destructive",
            summary=f"Flash {plan_operation} firmware write",
            context={
                "host": target.acp_host,
                "backup_dir": str(bundle.backup_dir),
                "mode": plan_operation,
                "target_bank": bank,
                "target_sha256": None if plan.target_bank is None else plan.target_bank.sha256,
                "reboot_after_write": reboot_after_write,
                "wait_after_reboot": wait_after_reboot,
                **_secondary_refresh_context(plan),
            },
            presentation_id=presentation_id,
            presentation_values={
                "host": target.acp_host,
                "backup_dir": str(bundle.backup_dir),
                "mode": plan_operation,
                "target_bank": bank,
                "reboot_after_write": reboot_after_write,
                "wait_after_reboot": wait_after_reboot,
            },
        ),
    )

    try:
        context.stage("pre_write_validation")
        validate_live_target_matches_backup(
            connection=target.connection,
            plan=plan,
            log=context.log,
        )
        context.stage(write_stage_for_plan(plan))
        if not secondary_refresh:
            # write_flash_plan logs the secondary bank write command itself.
            context.log("Sending ACP flash command...")
        write_flash_plan(
            target=target,
            bundle=bundle,
            plan=plan,
            log=context.log,
        )
        context.stage("post_write_validation")
    except FlashAnalysisError as exc:
        raise AppOperationError(str(exc), code="operation_failed") from exc
    except TransportError as exc:
        raise AppOperationError(f"SSH post-write validation failed: {exc}", code="remote_error") from exc
    try:
        finish_validated_write(
            target=target,
            bundle=bundle,
            plan=plan,
            reboot=reboot_after_write,
            wait=wait_after_reboot,
            callbacks=context.to_operation_callbacks(),
        )
    except RebootFlowError as exc:
        raise AppOperationError(str(exc), code=exc.code) from exc
    return OperationResult(True, flash_write_payload(bundle.manifest))
