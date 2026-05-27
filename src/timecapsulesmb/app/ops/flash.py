from __future__ import annotations

from pathlib import Path

from timecapsulesmb.app.context import AppOperationContext
from timecapsulesmb.app.confirmations import build_confirmation, require_confirmation
from timecapsulesmb.app.contracts import flash_backup_payload, flash_plan_payload, flash_write_payload
from timecapsulesmb.core.config import AppConfig
from timecapsulesmb.device.compat import is_netbsd4_payload_family, payload_family_description
from timecapsulesmb.device.errors import DeviceError
from timecapsulesmb.flash import FlashAnalysisError
from timecapsulesmb.services.app import (
    AppOperationError,
    OperationResult,
    bool_param,
    config_path,
    required_path_param,
    string_param,
)
from timecapsulesmb.services.credentials import overlay_request_credentials
from timecapsulesmb.services.flash import (
    WRITE_OPERATIONS,
    FlashTarget,
    backup_flash,
    flash_target_from_connection,
    plan_flash_from_backup,
    record_write_outcome,
    validate_live_target_matches_backup,
    write_flash_plan,
)
from timecapsulesmb.services.runtime import (
    load_env_config,
    require_connection_compatibility,
    resolve_validated_managed_target,
)
from timecapsulesmb.transport.errors import TransportError


FLASH_ACTIONS = {"backup", "plan", "write"}
PLAN_OPERATIONS = {"patch", "restore", "check_apple", "download_only"}


def flash_operation(params: dict[str, object], context: AppOperationContext) -> OperationResult:
    action = string_param(params, "action", "backup").strip() or "backup"
    if action not in FLASH_ACTIONS:
        raise AppOperationError(f"unsupported flash action: {action}", code="validation_failed")
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


def _firmware_template_param(params: dict[str, object]) -> Path | None:
    return _optional_path_param(params, "firmware_template")


def _firmware_version_param(params: dict[str, object]) -> str | None:
    value = string_param(params, "firmware_version").strip()
    return value or None


def _plan_operation_param(params: dict[str, object]) -> str:
    plan_operation = string_param(params, "mode", "patch").strip() or "patch"
    if plan_operation not in PLAN_OPERATIONS:
        raise AppOperationError(f"unsupported flash plan mode: {plan_operation}", code="validation_failed")
    return plan_operation


def _write_operation_param(params: dict[str, object]) -> str:
    plan_operation = _plan_operation_param(params)
    if plan_operation not in WRITE_OPERATIONS:
        raise AppOperationError(f"flash mode {plan_operation} does not write firmware", code="validation_failed")
    return plan_operation


def _load_flash_config(params: dict[str, object], context: AppOperationContext) -> AppConfig:
    context.stage("load_config")
    config = overlay_request_credentials(load_env_config(env_path=config_path(params)), params)
    context.config = config
    return config


def _resolve_flash_target(config: AppConfig, context: AppOperationContext) -> FlashTarget:
    context.stage("resolve_connection")
    target = resolve_validated_managed_target(
        config,
        command_name="flash",
        profile="flash",
        include_probe=False,
    )
    context.apply_managed_target(target)
    context.stage("check_compatibility")
    try:
        compatibility = require_connection_compatibility(target.connection)
    except DeviceError as exc:
        raise AppOperationError(str(exc), code="unsupported_device") from exc
    context.update_fields(device_family=compatibility.payload_family)
    if not is_netbsd4_payload_family(compatibility.payload_family):
        raise AppOperationError(
            "flash is only supported for NetBSD4 AirPort storage devices.",
            code="unsupported_device",
        )
    context.log(f"Using {payload_family_description(compatibility.payload_family)} payload family for flash work.")
    return flash_target_from_connection(target.connection, compatibility)


def _backup_operation(params: dict[str, object], context: AppOperationContext) -> OperationResult:
    config = _load_flash_config(params, context)
    target = _resolve_flash_target(config, context)
    backup_dir = _optional_path_param(params, "backup_dir")
    context.update_fields(backup_dir=str(backup_dir) if backup_dir is not None else None)
    try:
        bundle = backup_flash(
            target=target,
            backup_dir=backup_dir,
            operation="read_only",
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
    firmware_template = _firmware_template_param(params)
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
    except FlashAnalysisError as exc:
        raise AppOperationError(str(exc), code="validation_failed") from exc
    return OperationResult(True, flash_plan_payload(bundle.manifest))


def _confirmation_message(target: FlashTarget, mode: str, bank: str | None) -> str:
    if mode == "patch":
        return (
            f"Patch the primary firmware bank boot hook on {target.acp_host} "
            "and acknowledge that manual power cycle is required after a successful write?"
        )
    bank_text = f" {bank}" if bank else ""
    return f"Restore Apple stock firmware to the active{bank_text} bank on {target.acp_host}?"


def _write_operation(params: dict[str, object], context: AppOperationContext) -> OperationResult:
    plan_operation = _write_operation_param(params)
    force = bool_param(params, "force")
    backup_dir = required_path_param(params, "backup_dir")
    firmware_template = _firmware_template_param(params)
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
        bundle, plan = plan_flash_from_backup(
            backup_dir=backup_dir,
            operation=plan_operation,
            force=force,
            firmware_template=firmware_template,
            firmware_version=firmware_version,
        )
    except FlashAnalysisError as exc:
        raise AppOperationError(str(exc), code="validation_failed") from exc
    if plan is None:
        raise AppOperationError("flash write has no plan", code="validation_failed")
    if plan.already_satisfied:
        record_write_outcome(
            bundle=bundle,
            plan=plan,
            status="not_needed",
            write_validated=False,
            write_may_have_modified_device=False,
        )
        return OperationResult(True, flash_write_payload(bundle.manifest))

    config = _load_flash_config(params, context)
    target = _resolve_flash_target(config, context)
    bank = None if plan.target_bank is None else plan.target_bank.name
    context.update_fields(target_bank=bank)
    context.stage("confirm_write")
    require_confirmation(
        params,
        build_confirmation(
            operation="flash",
            params=params,
            title="Confirm firmware flash write",
            message=_confirmation_message(target, plan_operation, bank),
            action_title="Write Firmware",
            risk="destructive",
            summary=f"Flash {plan_operation} firmware write",
            context={
                "host": target.acp_host,
                "backup_dir": str(bundle.backup_dir),
                "mode": plan_operation,
                "target_bank": bank,
                "target_sha256": None if plan.target_bank is None else plan.target_bank.sha256,
            },
            presentation_id=f"flash.{plan_operation}_write",
            presentation_values={
                "host": target.acp_host,
                "backup_dir": str(bundle.backup_dir),
                "mode": plan_operation,
                "target_bank": bank,
            },
        ),
        legacy_names=("confirm_flash",),
    )

    try:
        context.stage("pre_write_validation")
        validate_live_target_matches_backup(
            connection=target.connection,
            plan=plan,
            log=context.log,
        )
        context.stage("write_primary_bank" if plan_operation == "patch" else "write_active_bank")
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
    return OperationResult(True, flash_write_payload(bundle.manifest))
