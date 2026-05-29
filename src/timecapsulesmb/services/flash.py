from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from functools import partial
import json
import re
from pathlib import Path
from typing import Callable

from timecapsulesmb.apple_firmware import normalize_syap
from timecapsulesmb.core.config import AIRPORT_IDENTITIES_BY_SYAP
from timecapsulesmb.core.net import extract_host
from timecapsulesmb.core.paths import default_user_data_dir
from timecapsulesmb.device.compat import DeviceCompatibility
from timecapsulesmb.flash import (
    FlashAnalysis,
    FlashAnalysisError,
    FlashInspection,
    inspection_error_message,
    inspection_to_jsonable,
    inspect_flash_banks,
    require_zopfli_gzip_available,
    sha256_hex,
)
from timecapsulesmb.flash_workflow import (
    FlashPlan,
    plan_check_apple,
    plan_download_only,
    plan_patch_primary,
    plan_restore_apple,
    write_and_validate_plan,
)
from timecapsulesmb.integrations.acp import ACPError, flash_firmware_bank, get_property_int
from timecapsulesmb.transport.ssh import SshConnection, run_ssh_capture_bytes


FLASH_READ_TIMEOUT_SECONDS = 180
FLASH_WRITE_TIMEOUT_SECONDS = 300
WRITE_OPERATIONS = {"patch", "restore"}
READ_OPERATIONS = {"read_only", "patch", "restore", "check_apple", "download_only"}
POWERCYCLE_REQUIRED_MESSAGE = (
    "POWER-CYCLE REQUIRED: unplug the device, wait 10 seconds, then plug it back in."
)
STALE_BACKUP_AFTER_WRITE_MESSAGE = (
    "This flash backup was used for a firmware write. Back up and inspect again before planning another flash action."
)


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


def _emit(log: object | None, message: str) -> None:
    if log is None:
        return
    log(message)  # type: ignore[misc]


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


def flash_target_from_connection(connection: SshConnection, compatibility: DeviceCompatibility) -> FlashTarget:
    return FlashTarget(
        connection=connection,
        acp_host=extract_host(connection.host),
        compatibility=compatibility,
    )


def dump_remote_bank(connection: SshConnection, device: str, *, log: object | None = None) -> bytes:
    _emit(log, f"SSH: /bin/dd if={device} bs=65536 2>/dev/null")
    return run_ssh_capture_bytes(
        connection,
        f"/bin/dd if={device} bs=65536 2>/dev/null",
        timeout=FLASH_READ_TIMEOUT_SECONDS,
    )


def read_live_login(connection: SshConnection, *, log: object | None = None) -> bytes:
    _emit(log, "SSH: /bin/dd if=/etc/rc.d/LOGIN bs=4096 2>/dev/null")
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
    log: object | None = None,
) -> FlashInputs:
    _emit(log, "Reading primary firmware bank from /dev/rflash0.raw...")
    primary = dump_remote_bank(connection, "/dev/rflash0.raw", log=log)
    _emit(log, "Reading secondary firmware bank from /dev/rflash1.raw...")
    secondary = dump_remote_bank(connection, "/dev/rflash1.raw", log=log)
    _emit(log, "Reading ACP checksum properties cks1 and cks2...")
    cks1 = read_acp_property_int(acp_host, password, "cks1")
    cks2 = read_acp_property_int(acp_host, password, "cks2")
    _emit(log, "Reading ACP product property syAP...")
    syap = normalize_syap(read_acp_property_int(acp_host, password, "syAP"))
    _emit(log, "Reading live /etc/rc.d/LOGIN...")
    live_login = read_live_login(connection, log=log)
    return FlashInputs(primary=primary, secondary=secondary, cks1=cks1, cks2=cks2, syap=syap, live_login=live_login)


def dump_remote_bank_for_validation(connection: SshConnection, device: str, *, log: object | None = None) -> bytes:
    _emit(log, f"Reading back written firmware bank from {device}...")
    return dump_remote_bank(connection, device, log=log)


def get_property_int_for_validation(
    host: str,
    password: str,
    name: str,
    *,
    log: object | None = None,
    **kwargs: object,
) -> int:
    _emit(log, f"Reading ACP checksum property {name} after write...")
    return get_property_int(host, password, name, **kwargs)


def _mark_manifest_no_write(manifest: dict[str, object], decision: str) -> None:
    banks = manifest.get("banks")
    if not isinstance(banks, list):
        return
    for bank in banks:
        if isinstance(bank, dict):
            bank["would_write"] = False
            bank["write_decision"] = decision


def _manifest_banks(manifest: dict[str, object]) -> list[dict[str, object]]:
    banks = manifest.get("banks")
    if not isinstance(banks, list):
        return []
    return [bank for bank in banks if isinstance(bank, dict)]


def apply_flash_plan_to_manifest(manifest: dict[str, object], plan: FlashPlan) -> None:
    target_name = None if plan.target_bank is None else plan.target_bank.name
    for bank in _manifest_banks(manifest):
        if bank.get("name") != target_name:
            bank["would_write"] = False
            if target_name is not None and plan.mode == "patch":
                bank["write_decision"] = "secondary backup left unmodified"
            elif target_name is not None:
                bank["write_decision"] = "inactive bank left unmodified"
            continue

        bank["would_write"] = plan.write_requested
        if plan.mode == "patch":
            if plan.already_satisfied:
                bank["write_decision"] = "primary bank already patched; no write needed"
            elif plan.write_requested:
                bank["write_decision"] = "primary bank patch planned"
        elif plan.mode == "restore":
            if plan.write_requested:
                bank["write_decision"] = "active bank restore from Apple firmware planned"
            else:
                bank["write_decision"] = "active bank already matches requested Apple stock firmware; no write needed"
        elif plan.mode == "check_apple":
            bank["write_decision"] = "check only; no firmware write planned"
        elif plan.mode == "download_only":
            bank["write_decision"] = "download only; no firmware write planned"


def manifest_from_inspection(
    *,
    operation: str,
    inspection: FlashInspection,
    target: FlashTarget,
    inputs: FlashInputs,
    backup_dir: Path,
) -> dict[str, object]:
    payload = inspection_to_jsonable(
        inspection,
        write_policy="primary_bank_patch" if operation == "patch" else "active_bank_only",
    )
    if operation != "patch":
        _mark_manifest_no_write(payload, "backup only; no patch candidate built")
    identity = AIRPORT_IDENTITIES_BY_SYAP.get(inputs.syap)
    files: dict[str, str] = {
        "primary": str(backup_dir / "primary.raw"),
        "secondary": str(backup_dir / "secondary.raw"),
        "manifest": str(backup_dir / "manifest.json"),
    }
    payload.update({
        "operation": operation,
        "host": target.acp_host,
        "syap": inputs.syap,
        "device_model": None if identity is None else identity.mdns_model,
        "os_release": target.compatibility.os_release,
        "backup_dir": str(backup_dir),
        "files": files,
        "acp_properties": {
            "cks1": inputs.cks1,
            "cks2": inputs.cks2,
        },
        "live_login": {
            "size": len(inputs.live_login),
            "sha256": sha256_hex(inputs.live_login),
        },
    })
    return payload


def save_flash_banks(*, backup_dir: Path, primary: bytes, secondary: bytes) -> None:
    backup_dir.mkdir(parents=True, exist_ok=True)
    (backup_dir / "primary.raw").write_bytes(primary)
    (backup_dir / "secondary.raw").write_bytes(secondary)


def save_flash_manifest(*, backup_dir: Path, manifest: dict[str, object]) -> None:
    backup_dir.mkdir(parents=True, exist_ok=True)
    (backup_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")


def load_flash_manifest(backup_dir: Path) -> dict[str, object]:
    manifest_path = backup_dir.expanduser().resolve() / "manifest.json"
    try:
        data = json.loads(manifest_path.read_text())
    except OSError as exc:
        raise FlashAnalysisError(f"flash manifest not found: {manifest_path}") from exc
    except json.JSONDecodeError as exc:
        raise FlashAnalysisError(f"flash manifest is not valid JSON: {manifest_path}") from exc
    if not isinstance(data, dict):
        raise FlashAnalysisError(f"flash manifest is not an object: {manifest_path}")
    return data


def _manifest_file_path(manifest: dict[str, object], backup_dir: Path, name: str) -> Path:
    files = manifest.get("files")
    if isinstance(files, dict) and isinstance(files.get(name), str):
        return Path(str(files[name])).expanduser().resolve()
    return backup_dir / f"{name}.raw"


def _parse_optional_int(value: object) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        return int(text, 16) if text.lower().startswith("0x") else int(text, 10)
    return None


def _manifest_acp_checksum(manifest: dict[str, object], name: str) -> int | None:
    properties = manifest.get("acp_properties")
    if isinstance(properties, dict):
        value = properties.get("cks1" if name == "primary" else "cks2")
        parsed = _parse_optional_int(value)
        if parsed is not None:
            return parsed
    for bank in _manifest_banks(manifest):
        if bank.get("name") == name:
            return _parse_optional_int(bank.get("acp_checksum"))
    return None


def _read_backup_raw(backup_dir: Path, manifest: dict[str, object]) -> tuple[bytes, bytes]:
    try:
        primary = _manifest_file_path(manifest, backup_dir, "primary").read_bytes()
        secondary = _manifest_file_path(manifest, backup_dir, "secondary").read_bytes()
    except OSError as exc:
        raise FlashAnalysisError(f"flash backup raw bank file could not be read: {exc}") from exc
    return primary, secondary


def _backup_syap(manifest: dict[str, object]) -> str:
    syap = str(manifest.get("syap") or "").strip()
    if not syap:
        raise FlashAnalysisError("flash manifest is missing syAP")
    return normalize_syap(syap)


def _backup_os_release(manifest: dict[str, object]) -> str:
    os_release = str(manifest.get("os_release") or "").strip()
    if not os_release:
        raise FlashAnalysisError("flash manifest is missing os_release")
    return os_release


def _backup_was_used_for_write(manifest: dict[str, object]) -> bool:
    outcome = manifest.get("write_outcome")
    if not isinstance(outcome, dict):
        return False
    return bool(outcome.get("write_may_have_modified_device"))


def require_backup_fresh_for_plan(manifest: dict[str, object]) -> None:
    if _backup_was_used_for_write(manifest):
        raise FlashAnalysisError(STALE_BACKUP_AFTER_WRITE_MESSAGE)


def inspect_backup(
    backup_dir: Path,
    *,
    operation: str,
) -> FlashAnalysisBundle:
    if operation not in READ_OPERATIONS:
        raise FlashAnalysisError(f"unsupported flash operation: {operation}")
    backup_dir = backup_dir.expanduser().resolve()
    manifest = load_flash_manifest(backup_dir)
    if operation != "read_only":
        require_backup_fresh_for_plan(manifest)
    primary, secondary = _read_backup_raw(backup_dir, manifest)
    inspection = inspect_flash_banks(
        primary_data=primary,
        secondary_data=secondary,
        cks1=_manifest_acp_checksum(manifest, "primary"),
        cks2=_manifest_acp_checksum(manifest, "secondary"),
        os_release=_backup_os_release(manifest),
        build_primary_patch_candidate=operation == "patch",
    )
    return FlashAnalysisBundle(
        inspection=inspection,
        analysis=inspection.strict_analysis,
        backup_dir=backup_dir,
        manifest=manifest,
    )


def backup_flash(
    *,
    target: FlashTarget,
    backup_dir: Path | None,
    operation: str = "read_only",
    log: object | None = None,
    stage: Callable[[str], None] | None = None,
) -> FlashAnalysisBundle:
    if stage is not None:
        stage("read_flash")
    inputs = read_flash_inputs(
        target.connection,
        acp_host=target.acp_host,
        password=target.connection.password,
        log=log,
    )
    resolved_backup_dir = build_flash_backup_dir(base_dir=backup_dir, host=target.acp_host, syap=inputs.syap)
    if stage is not None:
        stage("save_raw_backup")
    save_flash_banks(backup_dir=resolved_backup_dir, primary=inputs.primary, secondary=inputs.secondary)
    if stage is not None:
        stage("analyze_flash")
    inspection = inspect_flash_banks(
        primary_data=inputs.primary,
        secondary_data=inputs.secondary,
        cks1=inputs.cks1,
        cks2=inputs.cks2,
        os_release=target.compatibility.os_release,
        build_primary_patch_candidate=operation == "patch",
    )
    manifest = manifest_from_inspection(
        operation=operation,
        inspection=inspection,
        target=target,
        inputs=inputs,
        backup_dir=resolved_backup_dir,
    )
    if stage is not None:
        stage("save_backup")
    save_flash_manifest(backup_dir=resolved_backup_dir, manifest=manifest)
    return FlashAnalysisBundle(
        inspection=inspection,
        analysis=inspection.strict_analysis,
        backup_dir=resolved_backup_dir,
        manifest=manifest,
    )


def plan_from_operation(
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
    if operation == "read_only":
        return None
    raise FlashAnalysisError(f"unsupported flash plan operation: {operation}")


def _save_primary_patched_bank_if_ready(*, backup_dir: Path, inspection: FlashInspection) -> Path | None:
    primary = inspection.primary.analysis
    if primary is None or primary.patch is None:
        return None
    path = backup_dir / "primary.patched.raw"
    path.write_bytes(primary.patch.target_bank)
    return path


def _save_acp_flash_payload(*, backup_dir: Path, plan: FlashPlan) -> Path | None:
    if plan.target_bank is None or plan.payload is None:
        return None
    suffix = "patched" if plan.mode == "patch" else plan.mode
    path = backup_dir / f"{plan.target_bank.name}.{suffix}.basebinary"
    path.write_bytes(plan.payload.data)
    return path


def plan_flash_from_backup(
    *,
    backup_dir: Path,
    operation: str,
    force: bool,
    firmware_template: Path | None,
    firmware_version: str | None,
) -> tuple[FlashAnalysisBundle, FlashPlan | None]:
    if operation == "patch":
        require_zopfli_gzip_available()
    bundle = inspect_backup(backup_dir, operation=operation)
    syap = _backup_syap(bundle.manifest)
    plan = plan_from_operation(
        operation=operation,
        inspection=bundle.inspection,
        analysis=bundle.analysis,
        force=force,
        syap=syap,
        firmware_template=firmware_template,
        firmware_version=firmware_version,
    )
    bundle.manifest["operation"] = operation
    bundle.manifest["flash_plan_params"] = {
        "operation": operation,
        "force": force,
        "firmware_template": None if firmware_template is None else str(firmware_template),
        "firmware_version": firmware_version,
    }
    if plan is not None:
        if operation == "patch":
            patched_primary_path = _save_primary_patched_bank_if_ready(
                backup_dir=bundle.backup_dir,
                inspection=bundle.inspection,
            )
            if patched_primary_path is not None:
                files = bundle.manifest.get("files")
                if isinstance(files, dict):
                    files["primary_patched"] = str(patched_primary_path)
        payload_path = _save_acp_flash_payload(backup_dir=bundle.backup_dir, plan=plan)
        files = bundle.manifest.get("files")
        if isinstance(files, dict) and payload_path is not None and plan.target_bank is not None:
            files[f"{plan.target_bank.name}_{plan.mode}_basebinary_payload"] = str(payload_path)
        bundle.manifest["flash_plan"] = plan.to_jsonable()
        apply_flash_plan_to_manifest(bundle.manifest, plan)
    save_flash_manifest(backup_dir=bundle.backup_dir, manifest=bundle.manifest)
    return bundle, plan


def write_outcome_payload(
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


def record_write_outcome(
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
    bundle.manifest["write_outcome"] = write_outcome_payload(
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


def record_post_write_action(
    *,
    bundle: FlashAnalysisBundle,
    post_write_action: str,
    reboot_requested: bool,
    rebooted: bool,
    waited_after_reboot: bool,
) -> None:
    outcome = bundle.manifest.get("write_outcome")
    if not isinstance(outcome, dict):
        outcome = {}
        bundle.manifest["write_outcome"] = outcome
    outcome.update({
        "post_write_action": post_write_action,
        "reboot_requested": reboot_requested,
        "rebooted": rebooted,
        "waited_after_reboot": waited_after_reboot,
    })
    save_flash_manifest(backup_dir=bundle.backup_dir, manifest=bundle.manifest)


def validate_live_target_matches_backup(
    *,
    connection: SshConnection,
    plan: FlashPlan,
    log: object | None = None,
) -> None:
    if plan.target_bank is None:
        raise FlashAnalysisError("flash plan has no target bank")
    _emit(log, f"Verifying live {plan.target_bank.name} bank still matches the saved backup...")
    live = dump_remote_bank(connection, plan.target_bank.device, log=log)
    live_sha256 = sha256_hex(live)
    if live_sha256 != plan.target_bank.sha256:
        raise FlashAnalysisError(
            "refusing to write because the live firmware bank changed since the saved backup: "
            f"bank={plan.target_bank.name} live_sha256={live_sha256} backup_sha256={plan.target_bank.sha256}"
        )


def write_flash_plan(
    *,
    target: FlashTarget,
    bundle: FlashAnalysisBundle,
    plan: FlashPlan,
    log: object | None = None,
) -> dict[str, object]:
    if plan.target_bank is None or plan.payload is None:
        raise FlashAnalysisError("flash plan has no write payload")
    record_write_outcome(
        bundle=bundle,
        plan=plan,
        status="attempting",
        write_validated=False,
        write_may_have_modified_device=True,
        stage="write_primary_bank" if plan.mode == "patch" else "write_active_bank",
    )
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
    record_write_outcome(
        bundle=bundle,
        plan=plan,
        status="validated",
        write_validated=True,
        write_may_have_modified_device=True,
        write_result=write_result,
    )
    return write_result
