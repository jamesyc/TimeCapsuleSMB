from __future__ import annotations

import argparse
import base64
from datetime import datetime
import re
from pathlib import Path
from typing import Callable, Optional

from timecapsulesmb.apple_firmware import (
    APPLE_FIRMWARE_CATALOG_URL,
    FirmwareTemplateCandidate,
    normalize_syap,
)
from timecapsulesmb.cli.context import CommandContext
from timecapsulesmb.cli.flows import observe_reboot_cycle
from timecapsulesmb.cli.runtime import (
    NonInteractivePromptError,
    add_config_argument,
    confirm,
    load_env_config,
    print_json,
    require_netbsd4_device_compatibility,
    write_json_file,
)
from timecapsulesmb.cli.util import color_green, color_red
from timecapsulesmb.core.config import AIRPORT_IDENTITIES_BY_SYAP, extract_host
from timecapsulesmb.core.paths import default_user_data_dir
from timecapsulesmb.deploy.executor import remote_request_reboot
from timecapsulesmb.flash import (
    FlashAnalysis,
    FlashAnalysisError,
    BankAnalysis,
    STOCK_LOGIN_NETBSD4_DUMMY,
    analysis_to_jsonable,
    analyze_flash_banks,
    require_zopfli_gzip_available,
    sha256_hex,
)
from timecapsulesmb.flash_payloads import build_patch_payload_for_active_bank as build_acp_flash_payload_for_active_bank
from timecapsulesmb.flash_workflow import (
    FlashPlan,
    plan_check_apple,
    plan_download_only,
    plan_patch_active,
    plan_restore_apple,
    require_patch_ready as require_write_ready,
    write_and_validate_plan,
)
from timecapsulesmb.identity import ensure_install_id
from timecapsulesmb.integrations.acp import ACPError, flash_firmware_bank, get_property_int
from timecapsulesmb.telemetry import TelemetryClient
from timecapsulesmb.transport.ssh import SshCommandTimeout, SshConnection, SshError, run_ssh_capture_bytes


FLASH_READ_TIMEOUT_SECONDS = 180
FLASH_WRITE_TIMEOUT_SECONDS = 300
MAX_LOGIN_ERROR_UPLOAD_BYTES = 8192
WRITE_OPERATIONS = {"patch", "restore"}
POWERCYCLE_REQUIRED_MESSAGE = (
    "POWER-CYCLE REQUIRED: unplug the Time Capsule, wait 10 seconds, then plug it back in."
)
ProgressLogger = Optional[Callable[[str], None]]


def _progress_logger(enabled: bool) -> ProgressLogger:
    if not enabled:
        return None

    def emit(message: str) -> None:
        print(f"[flash] {message}", flush=True)

    return emit


def _progress(log: ProgressLogger, message: str) -> None:
    if log is not None:
        log(message)


def _safe_path_part(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip())
    return safe.strip("-.") or "device"


def default_flash_backup_root() -> Path:
    return default_user_data_dir() / "flash-backups"


def build_flash_backup_dir(*, base_dir: Path | None, host: str, syap: str) -> Path:
    if base_dir is not None:
        return base_dir.expanduser().resolve()
    root = default_flash_backup_root()
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    return root / f"{timestamp}-{_safe_path_part(host)}-syAP{_safe_path_part(syap)}"


def dump_remote_bank(connection: SshConnection, device: str, *, log: ProgressLogger = None) -> bytes:
    _progress(log, f"SSH: /bin/dd if={device} bs=65536 2>/dev/null")
    return run_ssh_capture_bytes(
        connection,
        f"/bin/dd if={device} bs=65536 2>/dev/null",
        timeout=FLASH_READ_TIMEOUT_SECONDS,
    )


def read_live_login(connection: SshConnection, *, log: ProgressLogger = None) -> bytes:
    _progress(log, "SSH: /bin/dd if=/etc/rc.d/LOGIN bs=4096 2>/dev/null")
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
    _progress(log, "Reading primary firmware bank from /dev/rflash0.raw...")
    primary = dump_remote_bank(connection, "/dev/rflash0.raw", log=log)
    _progress(log, "Reading secondary firmware bank from /dev/rflash1.raw...")
    secondary = dump_remote_bank(connection, "/dev/rflash1.raw", log=log)
    _progress(log, "Reading ACP checksum properties cks1 and cks2...")
    cks1 = read_acp_property_int(acp_host, password, "cks1")
    cks2 = read_acp_property_int(acp_host, password, "cks2")
    _progress(log, "Reading ACP product property syAP...")
    syap = read_acp_property_int(acp_host, password, "syAP")
    _progress(log, "Reading live /etc/rc.d/LOGIN...")
    login = read_live_login(connection, log=log)
    return primary, secondary, cks1, cks2, syap, login


def _manifest(
    *,
    operation: str,
    analysis: FlashAnalysis,
    host: str,
    syap: str,
    live_login: bytes,
    backup_dir: Path,
    os_release: str,
) -> dict[str, object]:
    payload = analysis_to_jsonable(analysis)
    if operation != "patch":
        for bank in payload["banks"]:
            assert isinstance(bank, dict)
            bank["would_write"] = False
            bank["write_decision"] = "backup only; no patch candidate built"
    files: dict[str, str] = {
        "primary": str(backup_dir / "primary.raw"),
        "secondary": str(backup_dir / "secondary.raw"),
        "manifest": str(backup_dir / "manifest.json"),
    }
    active = analysis.active
    if operation == "patch" and active is not None and active.patch is not None:
        files[f"{active.name}_patched"] = str(backup_dir / f"{active.name}.patched.raw")
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


def save_flash_banks(*, backup_dir: Path, primary: bytes, secondary: bytes) -> None:
    backup_dir.mkdir(parents=True, exist_ok=True)
    (backup_dir / "primary.raw").write_bytes(primary)
    (backup_dir / "secondary.raw").write_bytes(secondary)


def save_flash_manifest(*, backup_dir: Path, manifest: dict[str, object]) -> None:
    write_json_file(backup_dir / "manifest.json", manifest)


def save_active_patched_bank_if_ready(*, backup_dir: Path, analysis: FlashAnalysis) -> Path | None:
    active = analysis.active
    if active is None or active.patch is None:
        return None
    path = backup_dir / f"{active.name}.patched.raw"
    path.write_bytes(active.patch.target_bank)
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
    for bank in manifest["banks"]:
        assert isinstance(bank, dict)
        footer = bank["footer"]
        gzip_info = bank["gzip"]
        login = bank["login"]
        patch = bank["patch"]
        assert isinstance(footer, dict)
        assert isinstance(gzip_info, dict)
        assert isinstance(login, dict)
        print(
            f"{bank['name']}: size={bank['size']} sha256={bank['sha256']} "
            f"footer={footer['checksum']} acp_match={bank['acp_checksum_matches']}"
        )
        print(
            f"  gzip offset={gzip_info['offset']} consumed={gzip_info['consumed_length']} "
            f"decompressed_sha256={gzip_info['decompressed_sha256']}"
        )
        print(f"  LOGIN={login['classification']} offset={login['offset']} length={login['length']}")
        print(f"  write decision={bank['write_decision']}")
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
    analysis: FlashAnalysis,
    syap: str,
    firmware_template: Path | None,
    firmware_version: str | None,
) -> FlashPlan | None:
    if operation == "patch":
        return plan_patch_active(
            analysis,
            syap=syap,
            firmware_template=firmware_template,
            firmware_version=firmware_version,
        )
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
    return f"This will patch the active {plan.target_bank.name} firmware bank. Continue?"


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


def _request_ssh_reboot(connection: SshConnection, command_context: CommandContext, *, log: ProgressLogger) -> None:
    command_context.set_stage("reboot")
    command_context.update_fields(reboot_was_attempted=True)
    command_context.add_debug_fields(reboot_request_strategy="ssh")
    _progress(log, "SSH: /sbin/reboot")
    try:
        remote_request_reboot(connection)
    except SshCommandTimeout as exc:
        command_context.add_debug_fields(
            ssh_reboot_succeeded=False,
            ssh_reboot_timed_out=True,
            ssh_reboot_error=str(exc),
        )
        print("SSH reboot request timed out; checking whether the device is rebooting...", flush=True)
        return
    except SshError as exc:
        command_context.add_debug_fields(
            ssh_reboot_succeeded=False,
            ssh_reboot_error=str(exc),
        )
        print("SSH reboot request failed; checking whether the device is rebooting anyway...", flush=True)
        return

    command_context.add_debug_fields(ssh_reboot_succeeded=True)
    print("SSH reboot requested.", flush=True)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Analyze, patch, or restore the NetBSD4 firmware boot hook.")
    add_config_argument(parser)
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--read-only", action="store_true", help="Dump and back up firmware banks without patch planning")
    mode_group.add_argument("--patch", action="store_true", help="Patch the active firmware bank LOGIN hook")
    mode_group.add_argument("--restore", action="store_true", help="Restore the active firmware bank from Apple stock firmware")
    mode_group.add_argument("--check-apple", action="store_true", help="Check whether the active bank matches Apple stock firmware")
    mode_group.add_argument("--download-only", action="store_true", help="Download and validate Apple firmware without writing")
    parser.add_argument("--yes", action="store_true", help="Do not prompt before --patch or --restore writes")
    parser.add_argument("--reboot", action="store_true", help="Reboot after a validated --restore write")
    parser.add_argument("--poweroff", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--json", action="store_true", help="Output the flash analysis and plan as JSON")
    parser.add_argument("--backup-dir", type=Path, default=None, help="Directory where this run's firmware backup should be saved")
    parser.add_argument(
        "--firmware-template",
        type=Path,
        default=None,
        help="Apple .basebinary firmware template to use; defaults to Apple catalog auto-selection",
    )
    parser.add_argument("--firmware-version", default=None, help="Apple firmware version to select, for example 7.8.1")
    args = parser.parse_args(argv)
    operation = _operation_from_args(args)

    if args.yes and operation not in WRITE_OPERATIONS:
        parser.error("--yes is only valid with --patch or --restore")
    if operation == "patch" and args.reboot:
        parser.error("flash --patch cannot use --reboot; power cycle manually after the validated write")
    if args.reboot and operation != "restore":
        parser.error("--reboot is only valid with --restore")
    if args.poweroff:
        parser.error("--poweroff is not supported; power cycle manually after a validated patch write")
    if args.json and operation in WRITE_OPERATIONS:
        parser.error("--json is only valid for read-only flash modes")

    if operation == "patch":
        try:
            require_zopfli_gzip_available()
        except FlashAnalysisError as exc:
            raise SystemExit(str(exc)) from exc

    log = _progress_logger(not args.json)
    if not args.json:
        print("Analyzing NetBSD4 flash firmware...", flush=True)

    _progress(log, "Loading configuration and install identity...")
    ensure_install_id()
    config = load_env_config(env_path=args.config)
    telemetry = TelemetryClient.from_config(config, include_device_identity=False)
    with CommandContext(telemetry, "flash", "flash_started", "flash_finished", config=config, args=args) as command_context:
        command_context.update_fields(read_only=operation not in WRITE_OPERATIONS, write_requested=operation in WRITE_OPERATIONS, operation=operation)
        command_context.set_stage("resolve_connection")
        _progress(log, "Resolving SSH target...")
        target = command_context.resolve_validated_managed_target(profile="flash", include_probe=False)
        connection = target.connection
        acp_host = extract_host(connection.host)
        _progress(log, f"Using ACP host {acp_host}.")

        command_context.set_stage("check_compatibility")
        _progress(log, "Checking NetBSD4 device compatibility...")
        compatibility, _compatibility_message = require_netbsd4_device_compatibility(
            command_context,
            command_name="flash",
            json_output=args.json,
            unsupported_message="flash is only supported for NetBSD4 AirPort storage devices.",
        )

        command_context.set_stage("read_flash")
        try:
            primary, secondary, cks1, cks2, acp_syap, live_login = read_flash_inputs(
                connection,
                acp_host=acp_host,
                password=connection.password,
                log=log,
            )
        except FlashAnalysisError as exc:
            message = str(exc)
            record_flash_error(command_context, message, stage="read_flash")
            print(message)
            command_context.fail()
            return 1
        except SshError as exc:
            message = f"SSH flash read failed: {exc}"
            record_flash_error(command_context, message, stage="read_flash")
            print(message)
            command_context.fail()
            return 1
        try:
            syap = normalize_syap(acp_syap)
        except FlashAnalysisError as exc:
            message = str(exc)
            record_flash_error(command_context, message, stage="read_flash", live_login=live_login)
            print(message)
            command_context.fail()
            return 1
        identity = AIRPORT_IDENTITIES_BY_SYAP.get(syap)
        command_context.update_fields(
            device_syap=syap,
            device_model=None if identity is None else identity.mdns_model,
        )
        backup_dir = build_flash_backup_dir(base_dir=args.backup_dir, host=acp_host, syap=syap)
        command_context.set_stage("save_raw_backup")
        _progress(log, f"Saving raw flash backup to {backup_dir}...")
        save_flash_banks(backup_dir=backup_dir, primary=primary, secondary=secondary)

        command_context.set_stage("analyze_flash")
        if operation == "patch":
            _progress(log, "Analyzing flash banks and building patched gzip candidate...")
        else:
            _progress(log, "Analyzing flash banks...")
        try:
            analysis = analyze_flash_banks(
                primary_data=primary,
                secondary_data=secondary,
                cks1=cks1,
                cks2=cks2,
                os_release=compatibility.os_release,
                build_patch_candidate=operation == "patch",
            )
        except FlashAnalysisError as exc:
            message = str(exc)
            record_flash_error(command_context, message, stage="analyze_flash", live_login=live_login)
            print(message)
            command_context.fail()
            return 1
        command_context.update_fields(
            active_bank=analysis.active_bank,
            primary_login=analysis.primary.login.classification,
            secondary_login=analysis.secondary.login.classification,
        )
        patched_active_path = None
        if operation == "patch":
            patched_active_path = save_active_patched_bank_if_ready(backup_dir=backup_dir, analysis=analysis)
        if patched_active_path is not None:
            command_context.update_fields(patched_active_path=str(patched_active_path))
        manifest = _manifest(
            operation=operation,
            analysis=analysis,
            host=acp_host,
            syap=syap,
            live_login=live_login,
            backup_dir=backup_dir,
            os_release=compatibility.os_release,
        )

        plan = None
        if operation != "read_only":
            command_context.set_stage("plan_flash")
            _progress(log, "Resolving Apple firmware template and composing flash plan...")
            try:
                plan = _plan_from_operation(
                    operation=operation,
                    analysis=analysis,
                    syap=syap,
                    firmware_template=args.firmware_template,
                    firmware_version=args.firmware_version,
                )
            except FlashAnalysisError as exc:
                message = str(exc)
                active_analysis = analysis.active
                include_login_mismatch = (
                    operation == "patch"
                    and active_analysis is not None
                    and active_analysis.login.classification != "stock"
                )
                record_flash_error(
                    command_context,
                    message,
                    stage="plan_flash",
                    live_login=live_login,
                    include_login_mismatch=include_login_mismatch,
                )
                print(message)
                command_context.fail()
                return 1
            assert plan is not None
            payload_path = save_acp_flash_payload(backup_dir=backup_dir, plan=plan)
            files = manifest.get("files")
            if isinstance(files, dict) and payload_path is not None and plan.target_bank is not None:
                files[f"{plan.target_bank.name}_{plan.mode}_basebinary_payload"] = str(payload_path)
            manifest["flash_plan"] = plan.to_jsonable()
            _update_context_with_plan(command_context, plan, payload_path)

        command_context.set_stage("save_backup")
        _progress(log, "Writing flash manifest...")
        save_flash_manifest(backup_dir=backup_dir, manifest=manifest)

        if args.json:
            print_json(manifest)
        else:
            print_flash_summary(manifest)

        if operation not in WRITE_OPERATIONS:
            command_context.succeed()
            return 0

        assert plan is not None
        if plan.already_satisfied:
            if operation == "patch":
                print("Active firmware bank is already patched; no write needed.")
            else:
                print("Active firmware bank already matches the requested Apple stock firmware; no write needed.")
            command_context.succeed()
            return 0

        if not args.yes:
            command_context.set_stage("confirm_write")
            try:
                proceed = confirm(
                    _confirmation_prompt(plan),
                    default=False,
                    noninteractive_message=(
                        f"Running `flash --{operation}` requires confirmation when stdin is not interactive. "
                        f"Use `flash --{operation} --yes` to skip the prompt."
                    ),
                )
            except NonInteractivePromptError as exc:
                message = str(exc)
                print(message)
                command_context.fail_with_error(message)
                return 1
            if not proceed:
                print("Flash write cancelled.", flush=True)
                command_context.cancel_with_error("Cancelled by user at flash write confirmation prompt.")
                return 0

        command_context.set_stage("write_active_bank")
        assert plan.target_bank is not None
        _progress(log, f"Sending ACP flash command for active {plan.target_bank.name} bank...")

        def dump_remote_bank_for_validation(validation_connection: SshConnection, device: str) -> bytes:
            _progress(log, f"Reading back written firmware bank from {device}...")
            _progress(log, f"SSH: /bin/dd if={device} bs=65536 2>/dev/null")
            return dump_remote_bank(validation_connection, device)

        def get_property_int_for_validation(host: str, password: str, name: str, **kwargs: object) -> int:
            _progress(log, f"Reading ACP checksum property {name} after write...")
            return get_property_int(host, password, name, **kwargs)

        try:
            write_result = write_and_validate_plan(
                connection=connection,
                acp_host=acp_host,
                plan=plan,
                os_release=compatibility.os_release,
                flash_firmware_bank_func=flash_firmware_bank,
                dump_remote_bank_func=dump_remote_bank_for_validation,
                get_property_int_func=get_property_int_for_validation,
                timeout=FLASH_WRITE_TIMEOUT_SECONDS,
            )
        except FlashAnalysisError as exc:
            message = str(exc)
            record_flash_error(command_context, message, stage="post_write_validation", live_login=live_login)
            print(message)
            command_context.fail()
            return 1
        except SshError as exc:
            message = f"SSH post-write validation failed: {exc}"
            record_flash_error(command_context, message, stage="post_write_validation", live_login=live_login)
            print(message)
            command_context.fail()
            return 1
        manifest["write_result"] = write_result
        save_flash_manifest(backup_dir=backup_dir, manifest=manifest)
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

        if operation == "patch":
            print(color_red(POWERCYCLE_REQUIRED_MESSAGE), flush=True)
            print(f"{color_green('Patch write successful.')} The device needs to be manually rebooted.", flush=True)
            command_context.succeed()
            return 0

        if not args.reboot:
            print(f"{color_green('Restore write successful.')} The device needs to be manually rebooted.", flush=True)
            command_context.succeed()
            return 0

        _request_ssh_reboot(connection, command_context, log=log)
        if not observe_reboot_cycle(
            connection,
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
    return 1
