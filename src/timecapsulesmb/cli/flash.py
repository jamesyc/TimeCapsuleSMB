from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import partial
import re
from pathlib import Path
from typing import Optional

from timecapsulesmb.apple_firmware import (
    APPLE_FIRMWARE_CATALOG_URL,
    FirmwareTemplateCandidate,
    normalize_syap,
)
from timecapsulesmb.cli.context import CommandContext
from timecapsulesmb.cli.flows import observe_reboot_cycle, request_ssh_reboot
from timecapsulesmb.cli.runtime import (
    LogCallback,
    add_config_argument,
    emit_progress,
    load_env_config,
    prefixed_logger,
    print_json,
    require_netbsd4_device_compatibility,
    write_json_file,
)
from timecapsulesmb.cli.util import color_green, color_red
from timecapsulesmb.core.config import AIRPORT_IDENTITIES_BY_SYAP
from timecapsulesmb.core.net import extract_host
from timecapsulesmb.core.paths import default_user_data_dir
from timecapsulesmb.device.compat import DeviceCompatibility
from timecapsulesmb.flash import (
    FlashAnalysis,
    FlashAnalysisError,
    FlashInspection,
    STOCK_LOGIN_NETBSD4_DUMMY,
    analyze_flash_banks,
    inspection_error_message,
    inspection_to_jsonable,
    inspect_flash_banks,
    require_zopfli_gzip_available,
    sha256_hex,
)
from timecapsulesmb.flash_payloads import build_patch_payload_for_active_bank as build_acp_flash_payload_for_active_bank
from timecapsulesmb.flash_workflow import (
    FlashPlan,
    plan_check_apple,
    plan_download_only,
    plan_patch_primary,
    plan_restore_apple,
    require_patch_ready as require_write_ready,
    write_and_validate_plan,
)
from timecapsulesmb.identity import ensure_install_id
from timecapsulesmb.integrations.acp import ACPError, flash_firmware_bank, get_property_int
from timecapsulesmb.telemetry import TelemetryClient
from timecapsulesmb.transport.ssh import SshConnection, SshError, run_ssh_capture_bytes


FLASH_READ_TIMEOUT_SECONDS = 180
FLASH_WRITE_TIMEOUT_SECONDS = 300
MAX_LOGIN_ERROR_UPLOAD_BYTES = 8192
WRITE_OPERATIONS = {"patch", "restore"}
POWERCYCLE_REQUIRED_MESSAGE = (
    "POWER-CYCLE REQUIRED: unplug the Time Capsule, wait 10 seconds, then plug it back in."
)
ProgressLogger = LogCallback


@dataclass(frozen=True)
class FlashTarget:
    connection: SshConnection
    acp_host: str
    compatibility: DeviceCompatibility


@dataclass(frozen=True)
class FlashInputs:
    primary: bytes
    secondary: bytes
    cks1: int | None
    cks2: int | None
    syap: str
    live_login: bytes


@dataclass(frozen=True)
class FlashAnalysisBundle:
    inspection: FlashInspection
    analysis: FlashAnalysis | None
    backup_dir: Path
    manifest: dict[str, object]


def _safe_path_part(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip())
    return safe.strip("-.") or "device"


def default_flash_backup_root() -> Path:
    return default_user_data_dir() / "flash-backups"


def build_flash_backup_dir(*, base_dir: Path | None, host: str, syap: str) -> Path:
    if base_dir is not None:
        return base_dir.expanduser().resolve()
    root = default_flash_backup_root()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%fZ")
    return root / f"{timestamp}-{_safe_path_part(host)}-syAP{_safe_path_part(syap)}"


def dump_remote_bank(connection: SshConnection, device: str, *, log: ProgressLogger = None) -> bytes:
    emit_progress(log, f"SSH: /bin/dd if={device} bs=65536 2>/dev/null")
    return run_ssh_capture_bytes(
        connection,
        f"/bin/dd if={device} bs=65536 2>/dev/null",
        timeout=FLASH_READ_TIMEOUT_SECONDS,
    )


def read_live_login(connection: SshConnection, *, log: ProgressLogger = None) -> bytes:
    emit_progress(log, "SSH: /bin/dd if=/etc/rc.d/LOGIN bs=4096 2>/dev/null")
    return run_ssh_capture_bytes(connection, "/bin/dd if=/etc/rc.d/LOGIN bs=4096 2>/dev/null", timeout=30)


def read_acp_property_int(acp_host: str, password: str, name: str) -> int:
    try:
        return get_property_int(acp_host, password, name)
    except ACPError as exc:
        raise FlashAnalysisError(f"ACP property {name} read failed: {exc}") from exc


def read_flash_inputs(
    connection: SshConnection,
    *,
    acp_host: str,
    password: str,
    log: ProgressLogger = None,
) -> tuple[bytes, bytes, int | None, int | None, int | None, bytes]:
    emit_progress(log, "Reading primary firmware bank from /dev/rflash0.raw...")
    primary = dump_remote_bank(connection, "/dev/rflash0.raw", log=log)
    emit_progress(log, "Reading secondary firmware bank from /dev/rflash1.raw...")
    secondary = dump_remote_bank(connection, "/dev/rflash1.raw", log=log)
    emit_progress(log, "Reading ACP checksum properties cks1 and cks2...")
    cks1 = read_acp_property_int(acp_host, password, "cks1")
    cks2 = read_acp_property_int(acp_host, password, "cks2")
    emit_progress(log, "Reading ACP product property syAP...")
    syap = read_acp_property_int(acp_host, password, "syAP")
    emit_progress(log, "Reading live /etc/rc.d/LOGIN...")
    login = read_live_login(connection, log=log)
    return primary, secondary, cks1, cks2, syap, login


def dump_remote_bank_for_validation(
    connection: SshConnection,
    device: str,
    *,
    log: ProgressLogger = None,
) -> bytes:
    emit_progress(log, f"Reading back written firmware bank from {device}...")
    return dump_remote_bank(connection, device)


def get_property_int_for_validation(
    host: str,
    password: str,
    name: str,
    *,
    log: ProgressLogger = None,
    **kwargs: object,
) -> int:
    emit_progress(log, f"Reading ACP checksum property {name} after write...")
    return get_property_int(host, password, name, **kwargs)


def _manifest(
    *,
    operation: str,
    inspection: FlashInspection,
    host: str,
    syap: str,
    live_login: bytes,
    backup_dir: Path,
    os_release: str,
) -> dict[str, object]:
    payload = inspection_to_jsonable(
        inspection,
        write_policy="primary_bank_patch" if operation == "patch" else "active_bank_only",
    )
    if operation != "patch":
        _mark_manifest_no_write(payload, "backup only; no patch candidate built")
    files: dict[str, str] = {
        "primary": str(backup_dir / "primary.raw"),
        "secondary": str(backup_dir / "secondary.raw"),
        "manifest": str(backup_dir / "manifest.json"),
    }
    payload.update({
        "operation": operation,
        "host": host,
        "syap": syap,
        "os_release": os_release,
        "backup_dir": str(backup_dir),
        "files": files,
        "live_login": {
            "size": len(live_login),
            "sha256": sha256_hex(live_login),
        },
    })
    return payload


def _mark_manifest_no_write(manifest: dict[str, object], decision: str) -> None:
    for bank in manifest["banks"]:
        assert isinstance(bank, dict)
        bank["would_write"] = False
        bank["write_decision"] = decision


def _manifest_banks(manifest: dict[str, object]) -> list[dict[str, object]]:
    banks = manifest.get("banks")
    assert isinstance(banks, list)
    typed_banks: list[dict[str, object]] = []
    for bank in banks:
        assert isinstance(bank, dict)
        typed_banks.append(bank)
    return typed_banks


def _apply_flash_plan_to_manifest(manifest: dict[str, object], plan: FlashPlan) -> None:
    target_name = None if plan.target_bank is None else plan.target_bank.name
    for bank in _manifest_banks(manifest):
        if bank.get("name") != target_name:
            bank["would_write"] = False
            if target_name is not None and plan.mode == "patch":
                bank["write_decision"] = "secondary backup left unmodified"
            elif target_name is not None:
                bank["write_decision"] = "inactive bank left unmodified"
            continue

        if plan.mode == "patch":
            bank["would_write"] = plan.write_requested
            if plan.already_satisfied:
                bank["write_decision"] = "primary bank already patched; no write needed"
            elif plan.write_requested:
                bank["write_decision"] = "primary bank patch planned"
        elif plan.mode == "restore":
            bank["would_write"] = plan.write_requested
            if plan.write_requested:
                bank["write_decision"] = "active bank restore from Apple firmware planned"
            else:
                bank["write_decision"] = "active bank already matches requested Apple stock firmware; no write needed"
        elif plan.mode == "check_apple":
            bank["would_write"] = False
            bank["write_decision"] = "check only; no firmware write planned"
        elif plan.mode == "download_only":
            bank["would_write"] = False
            bank["write_decision"] = "download only; no firmware write planned"


def save_flash_banks(*, backup_dir: Path, primary: bytes, secondary: bytes) -> None:
    backup_dir.mkdir(parents=True, exist_ok=True)
    (backup_dir / "primary.raw").write_bytes(primary)
    (backup_dir / "secondary.raw").write_bytes(secondary)


def save_flash_manifest(*, backup_dir: Path, manifest: dict[str, object]) -> None:
    write_json_file(backup_dir / "manifest.json", manifest)


def save_primary_patched_bank_if_ready(*, backup_dir: Path, inspection: FlashInspection) -> Path | None:
    primary = inspection.primary.analysis
    if primary is None or primary.patch is None:
        return None
    path = backup_dir / "primary.patched.raw"
    path.write_bytes(primary.patch.target_bank)
    return path


def save_acp_flash_payload(*, backup_dir: Path, plan: FlashPlan) -> Path | None:
    if plan.target_bank is None or plan.payload is None:
        return None
    suffix = "patched" if plan.mode == "patch" else plan.mode
    path = backup_dir / f"{plan.target_bank.name}.{suffix}.basebinary"
    path.write_bytes(plan.payload.data)
    return path


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


def _plan_from_operation(
    *,
    operation: str,
    inspection: FlashInspection,
    analysis: FlashAnalysis | None,
    force: bool,
    syap: str,
    firmware_template: Path | None,
    firmware_version: str | None,
) -> FlashPlan | None:
    if operation == "patch":
        return plan_patch_primary(
            inspection,
            force=force,
            syap=syap,
            firmware_template=firmware_template,
            firmware_version=firmware_version,
        )
    if analysis is None:
        raise FlashAnalysisError(inspection_error_message(inspection))
    if operation == "restore":
        return plan_restore_apple(
            analysis,
            syap=syap,
            firmware_template=firmware_template,
            firmware_version=firmware_version,
        )
    if operation == "check_apple":
        return plan_check_apple(
            analysis,
            syap=syap,
            firmware_template=firmware_template,
            firmware_version=firmware_version,
        )
    if operation == "download_only":
        return plan_download_only(
            analysis,
            syap=syap,
            firmware_template=firmware_template,
            firmware_version=firmware_version,
        )
    return None


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


def _write_outcome_payload(
    *,
    plan: FlashPlan,
    status: str,
    write_validated: bool,
    write_may_have_modified_device: bool,
    stage: str | None = None,
    message: str | None = None,
) -> dict[str, object]:
    outcome: dict[str, object] = {
        "status": status,
        "mode": plan.mode,
        "write_validated": write_validated,
        "write_may_have_modified_device": write_may_have_modified_device,
    }
    if plan.target_bank is not None:
        outcome.update({
            "bank": plan.target_bank.name,
            "device": plan.target_bank.device,
        })
    if plan.payload is not None:
        outcome.update({
            "firmware_payload_sha256": plan.payload.payload_sha256,
            "firmware_payload_size": len(plan.payload.data),
            "expected_prefix_sha256": plan.payload.expected_prefix_sha256,
            "expected_prefix_size": len(plan.payload.expected_prefix),
        })
    if stage is not None:
        outcome["stage"] = stage
    if message is not None:
        outcome["message"] = message
    return outcome


def _record_write_outcome(
    *,
    bundle: FlashAnalysisBundle,
    plan: FlashPlan,
    status: str,
    write_validated: bool,
    write_may_have_modified_device: bool,
    stage: str | None = None,
    message: str | None = None,
    write_result: dict[str, object] | None = None,
) -> None:
    bundle.manifest["write_outcome"] = _write_outcome_payload(
        plan=plan,
        status=status,
        write_validated=write_validated,
        write_may_have_modified_device=write_may_have_modified_device,
        stage=stage,
        message=message,
    )
    if write_result is not None:
        bundle.manifest["write_result"] = write_result
    save_flash_manifest(backup_dir=bundle.backup_dir, manifest=bundle.manifest)


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
    parser.add_argument("--reboot", action="store_true", help="Reboot after a validated --restore write")
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
    acp_host = extract_host(connection.host)
    emit_progress(log, f"Using ACP host {acp_host}.")

    command_context.set_stage("check_compatibility")
    emit_progress(log, "Checking NetBSD4 device compatibility...")
    compatibility, _compatibility_message = require_netbsd4_device_compatibility(
        command_context,
        command_name="flash",
        json_output=args.json,
        unsupported_message="flash is only supported for NetBSD4 AirPort storage devices.",
    )
    return FlashTarget(connection=connection, acp_host=acp_host, compatibility=compatibility)


def _read_flash(
    command_context: CommandContext,
    target: FlashTarget,
    *,
    log: ProgressLogger,
) -> FlashInputs | None:
    command_context.set_stage("read_flash")
    try:
        primary, secondary, cks1, cks2, acp_syap, live_login = read_flash_inputs(
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

    try:
        syap = normalize_syap(acp_syap)
    except FlashAnalysisError as exc:
        message = str(exc)
        record_flash_error(command_context, message, stage="read_flash", live_login=live_login)
        print(message)
        command_context.fail()
        return None

    identity = AIRPORT_IDENTITIES_BY_SYAP.get(syap)
    command_context.update_fields(
        device_syap=syap,
        device_model=None if identity is None else identity.mdns_model,
    )
    return FlashInputs(
        primary=primary,
        secondary=secondary,
        cks1=cks1,
        cks2=cks2,
        syap=syap,
        live_login=live_login,
    )


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
    manifest = _manifest(
        operation=operation,
        inspection=inspection,
        host=target.acp_host,
        syap=inputs.syap,
        live_login=inputs.live_login,
        backup_dir=backup_dir,
        os_release=target.compatibility.os_release,
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
        plan = _plan_from_operation(
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
    _apply_flash_plan_to_manifest(bundle.manifest, plan)
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
        _record_write_outcome(
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
            )
        )
        if proceed is None:
            return False, 1
        if not proceed:
            print("Flash write cancelled.", flush=True)
            _record_write_outcome(
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
    _record_write_outcome(
        bundle=bundle,
        plan=plan,
        status="attempting",
        write_validated=False,
        write_may_have_modified_device=True,
        stage=stage,
    )
    try:
        write_result = write_and_validate_plan(
            connection=target.connection,
            acp_host=target.acp_host,
            plan=plan,
            os_release=target.compatibility.os_release,
            flash_firmware_bank_func=flash_firmware_bank,
            dump_remote_bank_func=partial(dump_remote_bank_for_validation, log=log),
            get_property_int_func=partial(get_property_int_for_validation, log=log),
            timeout=FLASH_WRITE_TIMEOUT_SECONDS,
        )
    except FlashAnalysisError as exc:
        message = str(exc)
        _record_write_outcome(
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
        _record_write_outcome(
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

    _record_write_outcome(
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

    request_ssh_reboot(target.connection, command_context, log=log)
    if not observe_reboot_cycle(
        target.connection,
        command_context,
        reboot_no_down_message="Firmware write validated, but the device did not go down after reboot request.",
        down_timeout_seconds=60,
        up_timeout_seconds=240,
    ):
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
