from __future__ import annotations

import argparse
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
from timecapsulesmb.core.config import extract_host
from timecapsulesmb.core.paths import default_user_data_dir
from timecapsulesmb.flash import (
    FlashAnalysis,
    FlashAnalysisError,
    analysis_to_jsonable,
    analyze_flash_banks,
    require_zopfli_gzip_available,
    sha256_hex,
)
from timecapsulesmb.identity import ensure_install_id
from timecapsulesmb.integrations.acp import get_property_int
from timecapsulesmb.telemetry import TelemetryClient
from timecapsulesmb.transport.ssh import SshConnection, run_ssh_capture_bytes


FLASH_READ_TIMEOUT_SECONDS = 180
UNSUPPORTED_WRITE_MESSAGE = "flash write is not supported yet"


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
    if analysis.active_bank == "primary" and analysis.primary.patch is not None:
        files["primary_patched"] = str(backup_dir / "primary.patched.raw")
    if analysis.secondary.patch is not None:
        files["secondary_patched"] = str(backup_dir / "secondary.patched.raw")
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


def save_secondary_patched_bank_if_ready(*, backup_dir: Path, analysis: FlashAnalysis) -> Path | None:
    if analysis.secondary.patch is None:
        return None
    path = backup_dir / "secondary.patched.raw"
    path.write_bytes(analysis.secondary.patch.target_bank)
    return path


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
        if bank["name"] == "primary" and bank.get("would_write"):
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
            raise SystemExit(str(exc)) from exc
        command_context.update_fields(
            active_bank=analysis.active_bank,
            primary_login=analysis.primary.login.classification,
            secondary_login=analysis.secondary.login.classification,
        )
        patched_active_path = save_active_patched_bank_if_ready(backup_dir=backup_dir, analysis=analysis)
        if patched_active_path is not None:
            command_context.update_fields(patched_active_path=str(patched_active_path))
        patched_secondary_path = save_secondary_patched_bank_if_ready(backup_dir=backup_dir, analysis=analysis)
        if patched_secondary_path is not None:
            command_context.update_fields(patched_secondary_path=str(patched_secondary_path))
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

        if not args.yes:
            command_context.set_stage("confirm_write")
            try:
                proceed = confirm(
                    "This will eventually write the active firmware bank. Continue?",
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

        command_context.set_stage("write_not_supported")
        print(UNSUPPORTED_WRITE_MESSAGE)
        command_context.fail_with_error(UNSUPPORTED_WRITE_MESSAGE)
        return 1
    return 1
