from __future__ import annotations

import argparse
import base64
from datetime import datetime
import re
from pathlib import Path
from typing import Optional

from timecapsulesmb.cli.context import CommandContext
from timecapsulesmb.cli.runtime import (
    NonInteractivePromptError,
    add_config_argument,
    confirm,
    load_env_config,
    print_json,
    require_netbsd4_device_compatibility,
    write_json_file,
)
from timecapsulesmb.cli.flows import request_reboot_and_wait
from timecapsulesmb.core.config import extract_host
from timecapsulesmb.core.paths import default_user_data_dir
from timecapsulesmb.flash import (
    FlashAnalysis,
    FlashAnalysisError,
    BankAnalysis,
    STOCK_LOGIN_NETBSD4_DUMMY,
    analysis_to_jsonable,
    analyze_bank,
    analyze_flash_banks,
    require_zopfli_gzip_available,
    sha256_hex,
)
from timecapsulesmb.identity import ensure_install_id
from timecapsulesmb.integrations.acp import ACPError, flash_firmware_bank, get_property_int
from timecapsulesmb.telemetry import TelemetryClient
from timecapsulesmb.transport.ssh import SshConnection, run_ssh_capture_bytes


FLASH_READ_TIMEOUT_SECONDS = 180
FLASH_WRITE_TIMEOUT_SECONDS = 300
FIRMWARE_WRITE_ACKNOWLEDGEMENT = "I_UNDERSTAND_THIS_CAN_BRICK_MY_AIRPORT"
MAX_TELEMETRY_LOGIN_UPLOAD_BYTES = 8192
ACP_WRITE_PAYLOAD_UNSUPPORTED_MESSAGE = (
    "refusing ACP firmware write because command 0x03/0x05 expects an Apple "
    "basebinary firmware container, not a raw flash bank; encrypted container "
    "composition is not implemented yet"
)


def _safe_path_part(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip())
    return safe.strip("-") or "device"


def default_flash_backup_root() -> Path:
    return default_user_data_dir() / "flash-backups"


def build_flash_backup_dir(*, base_dir: Path | None, host: str, syap: str) -> Path:
    if base_dir is not None:
        return base_dir.expanduser().resolve()
    root = default_flash_backup_root()
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    return root / f"{timestamp}-{_safe_path_part(host)}-syAP{_safe_path_part(syap)}"


def dump_remote_bank(connection: SshConnection, device: str) -> bytes:
    return run_ssh_capture_bytes(
        connection,
        f"/bin/dd if={device} bs=65536 2>/dev/null",
        timeout=FLASH_READ_TIMEOUT_SECONDS,
    )


def read_live_login(connection: SshConnection) -> bytes:
    return run_ssh_capture_bytes(connection, "/bin/dd if=/etc/rc.d/LOGIN bs=4096 2>/dev/null", timeout=30)


def read_flash_inputs(connection: SshConnection, *, acp_host: str, password: str) -> tuple[bytes, bytes, int | None, int | None, int | None, bytes]:
    primary = dump_remote_bank(connection, "/dev/rflash0.raw")
    secondary = dump_remote_bank(connection, "/dev/rflash1.raw")
    cks1 = get_property_int(acp_host, password, "cks1")
    cks2 = get_property_int(acp_host, password, "cks2")
    syap = get_property_int(acp_host, password, "syAP")
    login = read_live_login(connection)
    return primary, secondary, cks1, cks2, syap, login


def _manifest(
    *,
    analysis: FlashAnalysis,
    host: str,
    syap: str,
    live_login: bytes,
    backup_dir: Path,
    os_release: str,
) -> dict[str, object]:
    payload = analysis_to_jsonable(analysis)
    files: dict[str, str] = {
        "primary": str(backup_dir / "primary.raw"),
        "secondary": str(backup_dir / "secondary.raw"),
        "manifest": str(backup_dir / "manifest.json"),
    }
    active = analysis.active
    if active is not None and active.patch is not None:
        files[f"{active.name}_patched"] = str(backup_dir / f"{active.name}.patched.raw")
    payload.update({
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


def live_login_mismatch_telemetry(live_login: bytes) -> dict[str, object]:
    if live_login == STOCK_LOGIN_NETBSD4_DUMMY:
        return {}
    upload = live_login[:MAX_TELEMETRY_LOGIN_UPLOAD_BYTES]
    return {
        "flash_login_mismatch_file": "/etc/rc.d/LOGIN",
        "flash_login_mismatch_size": len(live_login),
        "flash_login_mismatch_sha256": sha256_hex(live_login),
        "flash_login_mismatch_truncated": len(live_login) > len(upload),
        "flash_login_mismatch_base64": base64.b64encode(upload).decode("ascii"),
    }


def record_flash_error(
    command_context: CommandContext,
    message: str,
    *,
    stage: str,
    live_login: bytes | None = None,
    include_login_mismatch: bool = False,
) -> None:
    fields: dict[str, object] = {
        "flash_error_stage": stage,
        "flash_error": message,
    }
    if include_login_mismatch and live_login is not None:
        fields.update(live_login_mismatch_telemetry(live_login))
    command_context.update_fields(**fields)


def active_checksum_property(bank_name: str) -> str:
    if bank_name == "primary":
        return "cks1"
    if bank_name == "secondary":
        return "cks2"
    raise FlashAnalysisError(f"unknown active bank: {bank_name}")


def inactive_bank(analysis: FlashAnalysis) -> BankAnalysis | None:
    if analysis.active_bank == "primary":
        return analysis.secondary
    if analysis.active_bank == "secondary":
        return analysis.primary
    return None


def require_write_ready(analysis: FlashAnalysis) -> BankAnalysis:
    active = analysis.active
    inactive = inactive_bank(analysis)
    if active is None:
        raise FlashAnalysisError("refusing to write because active firmware bank selection is ambiguous")
    if inactive is None or not inactive.footer_valid or inactive.acp_checksum_matches is not True:
        raise FlashAnalysisError("refusing to write because inactive firmware bank backup did not validate")
    if active.login.classification == "already_patched":
        raise FlashAnalysisError("active firmware bank is already patched; no write needed")
    if active.login.classification != "stock":
        raise FlashAnalysisError(f"refusing to write active bank with LOGIN classification {active.login.classification}")
    if active.patch is None:
        detail = f": {active.patch_error}" if active.patch_error else ""
        raise FlashAnalysisError(f"refusing to write because active bank has no patch candidate{detail}")
    return active


def build_acp_flash_payload_for_active_bank(active: BankAnalysis) -> bytes:
    assert active.patch is not None
    raise FlashAnalysisError(ACP_WRITE_PAYLOAD_UNSUPPORTED_MESSAGE)


def write_and_validate_active_bank(
    *,
    connection: SshConnection,
    acp_host: str,
    active: BankAnalysis,
    os_release: str,
) -> dict[str, object]:
    assert active.patch is not None
    payload = build_acp_flash_payload_for_active_bank(active)
    try:
        result = flash_firmware_bank(
            acp_host,
            connection.password,
            active.name,
            payload,
            timeout=FLASH_WRITE_TIMEOUT_SECONDS,
        )
    except ACPError as exc:
        raise FlashAnalysisError(f"ACP flash command failed: {exc}") from exc
    readback = dump_remote_bank(connection, active.device)
    readback_sha256 = sha256_hex(readback)
    if readback_sha256 != active.patch.target_bank_sha256:
        raise FlashAnalysisError(
            "read-back firmware bank SHA-256 mismatch after ACP write: "
            f"got {readback_sha256}, expected {active.patch.target_bank_sha256}"
        )
    checksum_property = active_checksum_property(active.name)
    acp_checksum = get_property_int(acp_host, connection.password, checksum_property)
    readback_analysis = analyze_bank(
        name=active.name,
        device=active.device,
        data=readback,
        acp_checksum=acp_checksum,
        os_release=os_release,
        build_patch_candidate=False,
    )
    if not readback_analysis.footer_valid:
        raise FlashAnalysisError("read-back firmware bank footer checksum is invalid after ACP write")
    if readback_analysis.acp_checksum_matches is not True:
        raise FlashAnalysisError(f"ACP {checksum_property} does not match read-back firmware footer after write")
    if readback_analysis.login.classification != "already_patched":
        raise FlashAnalysisError(
            "read-back firmware bank does not contain exactly one expected patched LOGIN script"
        )
    return {
        "bank": active.name,
        "device": active.device,
        "command": f"0x{result.command:02x}",
        "reply_body_size": len(result.reply_body),
        "reply_body_sha256": sha256_hex(result.reply_body),
        "readback_sha256": readback_sha256,
        "acp_checksum_property": checksum_property,
        "acp_checksum": f"0x{acp_checksum:08x}",
        "footer_checksum": f"0x{readback_analysis.footer.checksum:08x}",
        "login_classification": readback_analysis.login.classification,
    }


def print_flash_summary(manifest: dict[str, object]) -> None:
    print(f"Backed up firmware banks to: {manifest['backup_dir']}")
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


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Analyze and eventually patch the NetBSD4 firmware boot hook.")
    add_config_argument(parser)
    parser.add_argument("--read-only", action="store_true", help="Dump, back up, and analyze firmware banks without writing")
    parser.add_argument("--yes", action="store_true", help="Do not prompt before write mode")
    parser.add_argument(
        "--firmware-write-ack",
        default="",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--reboot", action="store_true", help="Reboot after a validated firmware write")
    parser.add_argument("--json", action="store_true", help="Output the read-only flash analysis as JSON")
    parser.add_argument("--backup-dir", type=Path, default=None, help="Directory where this run's firmware backup should be saved")
    args = parser.parse_args(argv)

    if args.read_only and args.yes:
        parser.error("--yes is only valid for write mode")
    if args.json and not args.read_only:
        parser.error("--json currently requires --read-only")

    try:
        require_zopfli_gzip_available()
    except FlashAnalysisError as exc:
        raise SystemExit(str(exc)) from exc

    if not args.json:
        print("Analyzing NetBSD4 flash firmware...")

    ensure_install_id()
    config = load_env_config(env_path=args.config)
    telemetry = TelemetryClient.from_config(config)
    with CommandContext(telemetry, "flash", "flash_started", "flash_finished", config=config, args=args) as command_context:
        command_context.update_fields(read_only=args.read_only, write_requested=not args.read_only)
        command_context.set_stage("resolve_managed_target")
        target = command_context.resolve_validated_managed_target(profile="flash", include_probe=True)
        connection = target.connection
        acp_host = extract_host(connection.host)

        command_context.set_stage("check_compatibility")
        compatibility, _compatibility_message = require_netbsd4_device_compatibility(
            command_context,
            command_name="flash",
            json_output=args.json,
            unsupported_message="flash is only supported for NetBSD4 AirPort storage devices.",
        )

        command_context.set_stage("read_flash")
        primary, secondary, cks1, cks2, acp_syap, live_login = read_flash_inputs(
            connection,
            acp_host=acp_host,
            password=connection.password,
        )
        syap = str(acp_syap or config.get("TC_AIRPORT_SYAP"))
        backup_dir = build_flash_backup_dir(base_dir=args.backup_dir, host=acp_host, syap=syap)
        command_context.set_stage("save_raw_backup")
        save_flash_banks(backup_dir=backup_dir, primary=primary, secondary=secondary)

        command_context.set_stage("analyze_flash")
        try:
            analysis = analyze_flash_banks(
                primary_data=primary,
                secondary_data=secondary,
                cks1=cks1,
                cks2=cks2,
                os_release=compatibility.os_release,
            )
        except FlashAnalysisError as exc:
            message = str(exc)
            record_flash_error(command_context, message, stage="analyze_flash", live_login=live_login)
            raise SystemExit(message) from exc
        command_context.update_fields(
            active_bank=analysis.active_bank,
            primary_login=analysis.primary.login.classification,
            secondary_login=analysis.secondary.login.classification,
        )
        patched_active_path = save_active_patched_bank_if_ready(backup_dir=backup_dir, analysis=analysis)
        if patched_active_path is not None:
            command_context.update_fields(patched_active_path=str(patched_active_path))
        manifest = _manifest(
            analysis=analysis,
            host=acp_host,
            syap=syap,
            live_login=live_login,
            backup_dir=backup_dir,
            os_release=compatibility.os_release,
        )

        command_context.set_stage("save_backup")
        save_flash_manifest(backup_dir=backup_dir, manifest=manifest)

        if args.json:
            print_json(manifest)
        else:
            print_flash_summary(manifest)

        if args.read_only:
            command_context.succeed()
            return 0

        if args.firmware_write_ack != FIRMWARE_WRITE_ACKNOWLEDGEMENT:
            message = (
                "Refusing firmware write without the exact firmware danger acknowledgement. "
                f"This can brick the device; pass --firmware-write-ack {FIRMWARE_WRITE_ACKNOWLEDGEMENT} "
                "only after reviewing the backup."
            )
            record_flash_error(command_context, message, stage="confirm_write", live_login=live_login)
            print(message)
            command_context.fail_with_error(message)
            return 1

        if not args.yes:
            command_context.set_stage("confirm_write")
            try:
                proceed = confirm(
                    f"This will write the active firmware bank. Type y only if you accept: {FIRMWARE_WRITE_ACKNOWLEDGEMENT}",
                    default=False,
                    noninteractive_message="Running `flash` write mode requires confirmation when stdin is not interactive. Use `flash --yes` to skip the prompt.",
                )
            except NonInteractivePromptError as exc:
                message = str(exc)
                print(message)
                command_context.fail_with_error(message)
                return 1
            if not proceed:
                print("Flash write cancelled.")
                command_context.cancel_with_error("Cancelled by user at flash write confirmation prompt.")
                return 0

        try:
            active = require_write_ready(analysis)
        except FlashAnalysisError as exc:
            message = str(exc)
            active_analysis = analysis.active
            include_login_mismatch = (
                active_analysis is not None
                and active_analysis.login.classification != "stock"
            )
            record_flash_error(
                command_context,
                message,
                stage="pre_write_validation",
                live_login=live_login,
                include_login_mismatch=include_login_mismatch,
            )
            print(message)
            command_context.fail_with_error(message)
            return 1

        command_context.set_stage("write_active_bank")
        try:
            write_result = write_and_validate_active_bank(
                connection=connection,
                acp_host=acp_host,
                active=active,
                os_release=compatibility.os_release,
            )
        except FlashAnalysisError as exc:
            message = str(exc)
            record_flash_error(command_context, message, stage="post_write_validation", live_login=live_login)
            print(message)
            command_context.fail_with_error(message)
            return 1
        manifest["write_result"] = write_result
        save_flash_manifest(backup_dir=backup_dir, manifest=manifest)
        command_context.update_fields(
            wrote_bank=write_result["bank"],
            readback_sha256=write_result["readback_sha256"],
            acp_reply_body_size=write_result["reply_body_size"],
        )
        print(
            f"Firmware write validated for {write_result['bank']} bank; "
            f"readback_sha256={write_result['readback_sha256']}"
        )

        if not args.reboot:
            print("Reboot not requested; stopping after validated write.")
            command_context.succeed()
            return 0

        if not request_reboot_and_wait(
            connection,
            command_context,
            reboot_no_down_message="Firmware write validated, but the device did not go down after reboot request.",
            down_timeout_seconds=60,
            up_timeout_seconds=240,
        ):
            return 1
        print("Device returned after reboot. Run `tcapsule doctor` to verify Samba startup.")
        command_context.succeed()
        return 0
    return 1
