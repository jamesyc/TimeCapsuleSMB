from __future__ import annotations

import argparse
import base64
from pathlib import Path
from typing import Optional

from timecapsulesmb.apple_firmware import (
    APPLE_FIRMWARE_CATALOG_URL,
    FirmwareTemplateCandidate,
)
from timecapsulesmb.cli.context import CommandContext
from timecapsulesmb.cli.runtime import (
    LogCallback,
    add_config_argument,
    add_no_input_argument,
    add_no_wait_argument,
    emit_progress,
    no_input_enabled,
    prefixed_logger,
    print_json,
    require_netbsd4_device_compatibility,
)
from timecapsulesmb.cli.util import color_green, color_red
from timecapsulesmb.core.config import AIRPORT_IDENTITIES_BY_SYAP
from timecapsulesmb.flash import (
    FlashAnalysisError,
    STOCK_LOGIN_NETBSD4_DUMMY,
    analyze_flash_banks,
    inspect_flash_banks,
    require_zopfli_gzip_available,
    sha256_hex,
)
from timecapsulesmb.flash_payloads import build_patch_payload_for_active_bank as build_acp_flash_payload_for_active_bank
from timecapsulesmb.flash_workflow import (
    FlashPlan,
    require_patch_ready as require_write_ready,
)
from timecapsulesmb.identity import ensure_install_id
from timecapsulesmb.services import flash as flash_service
from timecapsulesmb.services.flash import (
    FlashAnalysisBundle,
    FlashInputs,
    FlashTarget,
    FLASH_UNSUPPORTED_DEVICE_MESSAGE,
    apply_flash_plan_to_manifest,
    build_flash_backup_dir,
    default_flash_backup_root,
    manifest_from_inspection,
    plan_from_operation,
    record_write_outcome,
    require_netbsd4_flash_target,
    save_acp_flash_payload,
    save_flash_banks,
    save_flash_manifest,
    save_primary_patched_bank_if_ready,
    write_flash_plan,
)
from timecapsulesmb.services.reboot import RebootFlowError, observe_reboot_cycle, request_reboot
from timecapsulesmb.services.runtime import load_env_config
from timecapsulesmb.telemetry import TelemetryClient
from timecapsulesmb.transport.ssh import SshError


MAX_LOGIN_ERROR_UPLOAD_BYTES = 8192
WRITE_OPERATIONS = {"patch", "restore"}
POWERCYCLE_REQUIRED_MESSAGE = (
    "POWER-CYCLE REQUIRED: unplug the device, wait 10 seconds, then plug it back in."
)
ProgressLogger = LogCallback


def _manifest_banks(manifest: dict[str, object]) -> list[dict[str, object]]:
    banks = manifest.get("banks")
    assert isinstance(banks, list)
    typed_banks: list[dict[str, object]] = []
    for bank in banks:
        assert isinstance(bank, dict)
        typed_banks.append(bank)
    return typed_banks


def live_login_mismatch_error_lines(live_login: bytes) -> list[str]:
    if live_login == STOCK_LOGIN_NETBSD4_DUMMY:
        return []
    upload = live_login[:MAX_LOGIN_ERROR_UPLOAD_BYTES]
    return [
        "flash_login_mismatch_file=/etc/rc.d/LOGIN",
        f"flash_login_mismatch_size={len(live_login)}",
        f"flash_login_mismatch_sha256={sha256_hex(live_login)}",
        f"flash_login_mismatch_truncated={len(live_login) > len(upload)}",
        f"flash_login_mismatch_base64={base64.b64encode(upload).decode('ascii')}",
    ]


def record_flash_error(
    command_context: CommandContext,
    message: str,
    *,
    stage: str,
    live_login: bytes | None = None,
    include_login_mismatch: bool = False,
) -> None:
    lines = [
        message,
        f"flash_error_stage={stage}",
    ]
    if include_login_mismatch and live_login is not None:
        lines.extend(live_login_mismatch_error_lines(live_login))
    command_context.set_error("\n".join(lines))


def print_flash_summary(manifest: dict[str, object]) -> None:
    print(f"Backed up firmware banks to: {manifest['backup_dir']}")
    print(f"Operation: {manifest['operation']}")
    print(f"Active bank: {manifest['active_bank'] or 'unknown'}")
    selection = manifest.get("active_selection")
    if isinstance(selection, dict):
        candidates = selection.get("candidates")
        if isinstance(candidates, list) and candidates:
            candidate_text = ", ".join(str(candidate) for candidate in candidates)
        else:
            candidate_text = "none"
        selected_by = selection.get("selected_by") or "none"
        print(f"Active selection: {selection.get('status')} selected_by={selected_by} candidates={candidate_text}")
    for bank in manifest["banks"]:
        assert isinstance(bank, dict)
        footer = bank.get("footer")
        gzip_info = bank.get("gzip")
        login = bank.get("login")
        patch = bank.get("patch")
        if isinstance(footer, dict):
            footer_text = f"footer={footer['checksum']} acp_match={bank['acp_checksum_matches']}"
        else:
            footer_text = f"footer=unreadable acp_match={bank.get('acp_checksum_matches')}"
        print(f"{bank['name']}: size={bank['size']} sha256={bank['sha256']} {footer_text}")
        if isinstance(gzip_info, dict):
            print(
                f"  gzip offset={gzip_info['offset']} consumed={gzip_info['consumed_length']} "
                f"decompressed_sha256={gzip_info['decompressed_sha256']}"
            )
        else:
            print("  gzip unavailable")
        if isinstance(login, dict):
            print(f"  LOGIN={login['classification']} offset={login['offset']} length={login['length']}")
        else:
            print("  LOGIN=unavailable")
        print(f"  write decision={bank['write_decision']}")
        backup_failures = bank.get("backup_failures")
        if isinstance(backup_failures, list) and backup_failures:
            print(f"  backup failures={'; '.join(str(failure) for failure in backup_failures)}")
        failures = bank.get("active_selection_failures")
        if isinstance(failures, list) and failures:
            print(f"  active selection failures={'; '.join(str(failure) for failure in failures)}")
        if bank.get("would_write"):
            if isinstance(patch, dict):
                files = manifest.get("files", {})
                patch_file = files.get(f"{bank['name']}_patched") if isinstance(files, dict) else None
                print(
                    f"  patch target sha256={patch['target_bank_sha256']} "
                    f"method={patch['compression_method']}"
                )
                if patch_file:
                    print(f"  patch file={patch_file}")
            elif bank.get("patch_error"):
                print(f"  patch infeasible: {bank['patch_error']}")
    plan = manifest.get("flash_plan")
    if isinstance(plan, dict):
        warnings = plan.get("warnings")
        if isinstance(warnings, list):
            for warning in warnings:
                print(f"Warning: {warning}")
        payload = plan.get("payload")
        apple_match = plan.get("apple_match")
        if isinstance(payload, dict):
            print(
                f"Firmware payload: source={payload['template_source']} "
                f"version={payload['template_version']} sha256={payload['payload_sha256']}"
            )
        if isinstance(apple_match, dict):
            print(
                f"Apple firmware match: matched={apple_match['matched']} "
                f"source={apple_match['template_source']} version={apple_match['template_version']}"
            )


def _operation_from_args(args: argparse.Namespace) -> str:
    if args.patch:
        return "patch"
    if args.restore:
        return "restore"
    if args.check_apple:
        return "check_apple"
    if args.download_only:
        return "download_only"
    return "read_only"


def _confirmation_prompt(plan: FlashPlan) -> str:
    assert plan.target_bank is not None
    if plan.mode == "restore":
        payload = plan.payload
        version = "unknown" if payload is None else payload.template_version or f"0x{payload.inner_version:08x}"
        product = "unknown" if payload is None else payload.template_product_id or str(payload.inner_model)
        return (
            f"This will flash Apple stock firmware {version} for product {product} "
            f"to the active {plan.target_bank.name} bank. Continue?"
        )
    return "This will patch the primary firmware bank. Continue?"


def _update_context_with_plan(command_context: CommandContext, plan: FlashPlan, payload_path: Path | None) -> None:
    fields: dict[str, object] = {
        "flash_plan_mode": plan.mode,
        "flash_plan_already_satisfied": plan.already_satisfied,
    }
    if plan.target_bank is not None:
        fields["flash_plan_target_bank"] = plan.target_bank.name
    if plan.payload is not None:
        fields.update({
            "firmware_template_source": plan.payload.template_source,
            "firmware_template_sha256": plan.payload.template_sha256,
            "firmware_payload_sha256": plan.payload.payload_sha256,
            "firmware_payload_size": len(plan.payload.data),
            "firmware_template_key_id": plan.payload.key_id,
        })
    if payload_path is not None:
        fields["firmware_payload_path"] = str(payload_path)
    command_context.update_fields(**fields)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Analyze, patch, or restore the NetBSD4 firmware boot hook.")
    add_config_argument(parser)
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--read-only", action="store_true", help="Dump and back up firmware banks without patch planning")
    mode_group.add_argument("--patch", action="store_true", help="Patch the primary firmware bank LOGIN hook")
    mode_group.add_argument("--restore", action="store_true", help="Restore the active firmware bank from Apple stock firmware")
    mode_group.add_argument("--check-apple", action="store_true", help="Check whether the active bank matches Apple stock firmware")
    mode_group.add_argument("--download-only", action="store_true", help="Download and validate Apple firmware without writing")
    parser.add_argument("--yes", action="store_true", help="Do not prompt before --patch or --restore writes")
    add_no_input_argument(parser)
    parser.add_argument("--reboot", action="store_true", help="Reboot after a validated --restore write")
    add_no_wait_argument(parser)
    parser.add_argument("--poweroff", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--json", action="store_true", help="Output the flash analysis and plan as JSON")
    parser.add_argument("--backup-dir", type=Path, default=None, help="Directory where this run's firmware backup should be saved")
    parser.add_argument(
        "--force",
        action="store_true",
        help="With --patch, bypass backup/active-candidate preflight and target the primary bank",
    )
    parser.add_argument(
        "--firmware-template",
        type=Path,
        default=None,
        help="Apple .basebinary firmware template to use; defaults to Apple catalog auto-selection",
    )
    parser.add_argument("--firmware-version", default=None, help="Apple firmware version to select, for example 7.8.1")
    return parser


def _parse_args(argv: Optional[list[str]]) -> tuple[argparse.Namespace, str]:
    parser = _build_parser()
    args = parser.parse_args(argv)
    operation = _operation_from_args(args)

    if args.yes and operation not in WRITE_OPERATIONS:
        parser.error("--yes is only valid with --patch or --restore")
    if args.force and operation != "patch":
        parser.error("--force is only valid with --patch")
    if operation == "patch" and args.reboot:
        parser.error("flash --patch cannot use --reboot; power cycle manually after the validated write")
    if args.reboot and operation != "restore":
        parser.error("--reboot is only valid with --restore")
    if args.no_wait and not (operation == "restore" and args.reboot):
        parser.error("--no-wait is only valid with --restore --reboot")
    if args.poweroff:
        parser.error("--poweroff is not supported; power cycle manually after a validated patch write")
    if args.json and operation in WRITE_OPERATIONS:
        parser.error("--json is only valid for read-only flash modes")
    return args, operation


def _require_operation_dependencies(operation: str) -> None:
    if operation == "patch":
        try:
            require_zopfli_gzip_available()
        except FlashAnalysisError as exc:
            raise SystemExit(str(exc)) from exc


def _resolve_flash_target(
    command_context: CommandContext,
    *,
    args: argparse.Namespace,
    log: ProgressLogger,
) -> FlashTarget:
    command_context.set_stage("resolve_connection")
    emit_progress(log, "Resolving SSH target...")
    target = command_context.resolve_validated_managed_target(profile="flash", include_probe=False)
    connection = target.connection

    command_context.set_stage("check_compatibility")
    emit_progress(log, "Checking NetBSD4 device compatibility...")
    compatibility, _compatibility_message = require_netbsd4_device_compatibility(
        command_context,
        command_name="flash",
        json_output=args.json,
        unsupported_message=FLASH_UNSUPPORTED_DEVICE_MESSAGE,
    )
    flash_target = require_netbsd4_flash_target(
        connection,
        compatibility,
        update_fields=command_context.update_fields,
    )
    emit_progress(log, f"Using ACP host {flash_target.acp_host}.")
    return flash_target


def _read_flash(
    command_context: CommandContext,
    target: FlashTarget,
    *,
    log: ProgressLogger,
) -> FlashInputs | None:
    command_context.set_stage("read_flash")
    try:
        inputs = flash_service.read_flash_inputs(
            target.connection,
            acp_host=target.acp_host,
            password=target.connection.password,
            log=log,
        )
    except FlashAnalysisError as exc:
        message = str(exc)
        record_flash_error(command_context, message, stage="read_flash")
        print(message)
        command_context.fail()
        return None
    except SshError as exc:
        message = f"SSH flash read failed: {exc}"
        record_flash_error(command_context, message, stage="read_flash")
        print(message)
        command_context.fail()
        return None

    identity = AIRPORT_IDENTITIES_BY_SYAP.get(inputs.syap)
    command_context.update_fields(
        device_syap=inputs.syap,
        device_model=None if identity is None else identity.mdns_model,
    )
    return inputs


def _analyze_flash(
    command_context: CommandContext,
    *,
    args: argparse.Namespace,
    operation: str,
    target: FlashTarget,
    inputs: FlashInputs,
    log: ProgressLogger,
) -> FlashAnalysisBundle | None:
    backup_dir = build_flash_backup_dir(base_dir=args.backup_dir, host=target.acp_host, syap=inputs.syap)
    command_context.set_stage("save_raw_backup")
    emit_progress(log, f"Saving raw flash backup to {backup_dir}...")
    save_flash_banks(backup_dir=backup_dir, primary=inputs.primary, secondary=inputs.secondary)

    command_context.set_stage("analyze_flash")
    if operation == "patch":
        emit_progress(log, "Analyzing flash banks and building patched gzip candidate...")
    else:
        emit_progress(log, "Analyzing flash banks...")
    inspection = inspect_flash_banks(
        primary_data=inputs.primary,
        secondary_data=inputs.secondary,
        cks1=inputs.cks1,
        cks2=inputs.cks2,
        os_release=target.compatibility.os_release,
        build_primary_patch_candidate=operation == "patch",
    )
    analysis = inspection.strict_analysis

    primary_analysis = inspection.primary.analysis
    secondary_analysis = inspection.secondary.analysis
    command_context.update_fields(
        active_bank=inspection.active_bank,
        active_selection_status=inspection.active_selection.status,
        active_selection_selected_by=inspection.active_selection.selected_by,
        primary_backup_valid=inspection.primary.backup_valid,
        secondary_backup_valid=inspection.secondary.backup_valid,
        primary_active_candidate=inspection.primary.active_candidate,
        secondary_active_candidate=inspection.secondary.active_candidate,
        primary_login=None if primary_analysis is None else primary_analysis.login.classification,
        secondary_login=None if secondary_analysis is None else secondary_analysis.login.classification,
    )
    manifest = manifest_from_inspection(
        operation=operation,
        inspection=inspection,
        target=target,
        inputs=inputs,
        backup_dir=backup_dir,
    )
    return FlashAnalysisBundle(inspection=inspection, analysis=analysis, backup_dir=backup_dir, manifest=manifest)


def _plan_flash(
    command_context: CommandContext,
    *,
    args: argparse.Namespace,
    operation: str,
    bundle: FlashAnalysisBundle,
    inputs: FlashInputs,
) -> tuple[bool, FlashPlan | None]:
    if operation == "read_only":
        return True, None

    command_context.set_stage("plan_flash")
    try:
        plan = plan_from_operation(
            operation=operation,
            inspection=bundle.inspection,
            analysis=bundle.analysis,
            force=args.force,
            syap=inputs.syap,
            firmware_template=args.firmware_template,
            firmware_version=args.firmware_version,
        )
    except FlashAnalysisError as exc:
        message = str(exc)
        bundle.manifest["flash_plan_error"] = {
            "stage": "plan_flash",
            "message": message,
        }
        active_analysis = None if bundle.analysis is None else bundle.analysis.active
        if operation == "patch":
            active_analysis = bundle.inspection.primary.analysis
        include_login_mismatch = (
            operation == "patch"
            and active_analysis is not None
            and active_analysis.login.classification != "stock"
            and active_analysis.login.classification != "already_patched"
        )
        record_flash_error(
            command_context,
            message,
            stage="plan_flash",
            live_login=inputs.live_login,
            include_login_mismatch=include_login_mismatch,
        )
        print(message)
        command_context.fail()
        return False, None
    assert plan is not None
    patched_primary_path = None
    if operation == "patch":
        patched_primary_path = save_primary_patched_bank_if_ready(backup_dir=bundle.backup_dir, inspection=bundle.inspection)
        if patched_primary_path is not None:
            files = bundle.manifest.get("files")
            if isinstance(files, dict):
                files["primary_patched"] = str(patched_primary_path)
            command_context.update_fields(patched_primary_path=str(patched_primary_path))
    payload_path = save_acp_flash_payload(backup_dir=bundle.backup_dir, plan=plan)
    files = bundle.manifest.get("files")
    if isinstance(files, dict) and payload_path is not None and plan.target_bank is not None:
        files[f"{plan.target_bank.name}_{plan.mode}_basebinary_payload"] = str(payload_path)
    bundle.manifest["flash_plan"] = plan.to_jsonable()
    apply_flash_plan_to_manifest(bundle.manifest, plan)
    _update_context_with_plan(command_context, plan, payload_path)
    return True, plan


def _save_and_report_manifest(
    command_context: CommandContext,
    *,
    args: argparse.Namespace,
    bundle: FlashAnalysisBundle,
    log: ProgressLogger,
) -> None:
    command_context.set_stage("save_backup")
    emit_progress(log, "Writing flash manifest...")
    save_flash_manifest(backup_dir=bundle.backup_dir, manifest=bundle.manifest)
    if args.json:
        print_json(bundle.manifest)
    else:
        print_flash_summary(bundle.manifest)


def _save_manifest_after_plan_failure(
    command_context: CommandContext,
    *,
    bundle: FlashAnalysisBundle,
    log: ProgressLogger,
) -> None:
    command_context.set_stage("save_backup")
    emit_progress(log, "Writing flash manifest...")
    save_flash_manifest(backup_dir=bundle.backup_dir, manifest=bundle.manifest)


def _prepare_write(
    command_context: CommandContext,
    *,
    args: argparse.Namespace,
    operation: str,
    bundle: FlashAnalysisBundle,
    plan: FlashPlan,
) -> tuple[bool, int]:
    if plan.already_satisfied:
        if operation == "patch":
            print("Primary firmware bank is already patched; no write needed.")
        else:
            print("Active firmware bank already matches the requested Apple stock firmware; no write needed.")
        record_write_outcome(
            bundle=bundle,
            plan=plan,
            status="not_needed",
            write_validated=False,
            write_may_have_modified_device=False,
        )
        command_context.succeed()
        return False, 0

    if not args.yes:
        command_context.set_stage("confirm_write")
        proceed = command_context.confirm_or_fail(
            _confirmation_prompt(plan),
            default=False,
            noninteractive_message=(
                f"Running `flash --{operation}` requires confirmation when stdin is not interactive. "
                f"Use `flash --{operation} --yes` to skip the prompt."
            ),
            allow_prompt=not no_input_enabled(args),
        )
        if proceed is None:
            return False, 1
        if not proceed:
            print("Flash write cancelled.", flush=True)
            record_write_outcome(
                bundle=bundle,
                plan=plan,
                status="cancelled",
                write_validated=False,
                write_may_have_modified_device=False,
                stage="confirm_write",
                message="Cancelled by user at flash write confirmation prompt.",
            )
            command_context.cancel_with_error("Cancelled by user at flash write confirmation prompt.")
            return False, 0
    return True, 0


def _write_flash(
    command_context: CommandContext,
    *,
    target: FlashTarget,
    plan: FlashPlan,
    bundle: FlashAnalysisBundle,
    live_login: bytes,
    log: ProgressLogger,
) -> dict[str, object] | None:
    assert plan.target_bank is not None
    stage = "write_primary_bank" if plan.mode == "patch" else "write_active_bank"
    target_text = "primary" if plan.mode == "patch" else f"active {plan.target_bank.name}"
    command_context.set_stage(stage)
    emit_progress(log, f"Sending ACP flash command for {target_text} bank...")
    try:
        write_result = write_flash_plan(target=target, bundle=bundle, plan=plan, log=log)
    except FlashAnalysisError as exc:
        message = str(exc)
        record_write_outcome(
            bundle=bundle,
            plan=plan,
            status="failed",
            write_validated=False,
            write_may_have_modified_device=True,
            stage="post_write_validation",
            message=message,
        )
        record_flash_error(command_context, message, stage="post_write_validation", live_login=live_login)
        print(message)
        command_context.fail()
        return None
    except SshError as exc:
        message = f"SSH post-write validation failed: {exc}"
        record_write_outcome(
            bundle=bundle,
            plan=plan,
            status="failed",
            write_validated=False,
            write_may_have_modified_device=True,
            stage="post_write_validation",
            message=message,
        )
        record_flash_error(command_context, message, stage="post_write_validation", live_login=live_login)
        print(message)
        command_context.fail()
        return None

    record_write_outcome(
        bundle=bundle,
        plan=plan,
        status="validated",
        write_validated=True,
        write_may_have_modified_device=True,
        write_result=write_result,
    )
    command_context.update_fields(
        wrote_bank=write_result["bank"],
        readback_sha256=write_result["readback_sha256"],
        readback_prefix_sha256=write_result["readback_prefix_sha256"],
        acp_reply_body_size=write_result["reply_body_size"],
    )
    print(
        f"Firmware write validated for {write_result['bank']} bank; "
        f"readback_prefix_sha256={write_result['readback_prefix_sha256']}"
    )
    return write_result


def _finish_write(
    command_context: CommandContext,
    *,
    args: argparse.Namespace,
    operation: str,
    target: FlashTarget,
    log: ProgressLogger,
) -> int:
    if operation == "patch":
        print(color_red(POWERCYCLE_REQUIRED_MESSAGE), flush=True)
        print(f"{color_green('Patch write successful.')} The device needs to be manually rebooted.", flush=True)
        command_context.succeed()
        return 0

    if not args.reboot:
        print(f"{color_green('Restore write successful.')} The device needs to be manually rebooted.", flush=True)
        command_context.succeed()
        return 0

    try:
        request_reboot(
            target.connection,
            strategy="ssh",
            callbacks=command_context.to_operation_callbacks(),
            progress_log=log,
            raise_on_request_error=args.no_wait,
        )
    except RebootFlowError as exc:
        print(str(exc))
        command_context.fail_with_error(str(exc))
        return 1
    if args.no_wait:
        print("Reboot requested; not waiting for the device to go down or come back.", flush=True)
        command_context.succeed()
        return 0
    try:
        observe_reboot_cycle(
            target.connection,
            callbacks=command_context.to_operation_callbacks(),
            reboot_no_down_message="Firmware write validated, but the device did not go down after reboot request.",
            reboot_up_timeout_message="Timed out waiting for SSH after reboot.",
            down_timeout_seconds=60,
            up_timeout_seconds=240,
        )
    except RebootFlowError as exc:
        print(str(exc))
        command_context.fail_with_error(str(exc))
        print(color_red(POWERCYCLE_REQUIRED_MESSAGE), flush=True)
        return 1
    print("Device returned after reboot. Run `tcapsule flash --check-apple` to verify Apple stock firmware.", flush=True)
    command_context.succeed()
    return 0


def _run_flash(
    command_context: CommandContext,
    *,
    args: argparse.Namespace,
    operation: str,
    log: ProgressLogger,
) -> int:
    command_context.update_fields(read_only=operation not in WRITE_OPERATIONS, write_requested=operation in WRITE_OPERATIONS, operation=operation)
    target = _resolve_flash_target(command_context, args=args, log=log)
    inputs = _read_flash(command_context, target, log=log)
    if inputs is None:
        return 1

    bundle = _analyze_flash(
        command_context,
        args=args,
        operation=operation,
        target=target,
        inputs=inputs,
        log=log,
    )
    if bundle is None:
        return 1

    plan_ok, plan = _plan_flash(
        command_context,
        args=args,
        operation=operation,
        bundle=bundle,
        inputs=inputs,
    )
    if not plan_ok:
        _save_manifest_after_plan_failure(command_context, bundle=bundle, log=log)
        return 1

    _save_and_report_manifest(command_context, args=args, bundle=bundle, log=log)
    if operation not in WRITE_OPERATIONS:
        command_context.succeed()
        return 0

    assert plan is not None
    should_write, rc = _prepare_write(command_context, args=args, operation=operation, bundle=bundle, plan=plan)
    if not should_write:
        return rc

    if _write_flash(
        command_context,
        target=target,
        plan=plan,
        bundle=bundle,
        live_login=inputs.live_login,
        log=log,
    ) is None:
        return 1

    return _finish_write(command_context, args=args, operation=operation, target=target, log=log)


def main(argv: Optional[list[str]] = None) -> int:
    args, operation = _parse_args(argv)
    if no_input_enabled(args) and operation in WRITE_OPERATIONS and not args.yes:
        print(
            f"Running `flash --{operation}` in non-interactive mode requires `--yes` "
            "to approve the firmware write."
        )
        return 1
    _require_operation_dependencies(operation)

    log = prefixed_logger("flash", enabled=not args.json)
    if not args.json:
        print("Analyzing NetBSD4 flash firmware...", flush=True)

    emit_progress(log, "Loading configuration and install identity...")
    ensure_install_id()
    config = load_env_config(env_path=args.config)
    telemetry = TelemetryClient.from_config(config, include_device_identity=False)
    with CommandContext(telemetry, "flash", "flash_started", "flash_finished", config=config, args=args) as command_context:
        return _run_flash(command_context, args=args, operation=operation, log=log)
    return 1
