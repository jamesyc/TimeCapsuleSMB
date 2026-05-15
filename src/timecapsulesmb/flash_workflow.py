from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import struct
import zlib

from timecapsulesmb.flash import (
    BankAnalysis,
    FlashAnalysis,
    FlashAnalysisError,
    FlashInspection,
    active_selection_error_message,
    analyze_bank,
    bank_inspection_status_line,
    sha256_hex,
)
from timecapsulesmb.flash_payloads import (
    AcpFlashPayload,
    AppleFirmwareMatch,
    build_patch_payload_for_bank,
    build_restore_payload_for_active_bank,
    find_apple_firmware_match,
)
from timecapsulesmb.integrations.acp import ACPError
from timecapsulesmb.transport.ssh import SshConnection


@dataclass(frozen=True)
class FlashPlan:
    mode: str
    target_bank: BankAnalysis | None
    payload: AcpFlashPayload | None
    apple_match: AppleFirmwareMatch | None
    already_satisfied: bool
    warnings: tuple[str, ...] = ()

    @property
    def write_requested(self) -> bool:
        return self.mode in {"patch", "restore"} and not self.already_satisfied

    def to_jsonable(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "target_bank": None if self.target_bank is None else self.target_bank.name,
            "write_requested": self.write_requested,
            "already_satisfied": self.already_satisfied,
            "warnings": list(self.warnings),
            "payload": None if self.payload is None else self.payload.to_jsonable(),
            "apple_match": None if self.apple_match is None else self.apple_match.to_jsonable(),
        }


def inactive_bank(analysis: FlashAnalysis) -> BankAnalysis | None:
    if analysis.active_bank == "primary":
        return analysis.secondary
    if analysis.active_bank == "secondary":
        return analysis.primary
    return None


def require_active_and_inactive_valid(analysis: FlashAnalysis) -> BankAnalysis:
    active = analysis.active
    inactive = inactive_bank(analysis)
    if active is None:
        raise FlashAnalysisError(active_selection_error_message(analysis, write=True))
    if inactive is None or not inactive.footer_valid or inactive.acp_checksum_matches is not True:
        raise FlashAnalysisError("refusing to write because inactive firmware bank backup did not validate")
    return active


def require_patch_ready(analysis: FlashAnalysis) -> BankAnalysis:
    active = require_active_and_inactive_valid(analysis)
    if active.login.classification == "already_patched":
        return active
    if active.login.classification != "stock":
        raise FlashAnalysisError(f"refusing to write active bank with LOGIN classification {active.login.classification}")
    if active.patch is None:
        detail = f": {active.patch_error}" if active.patch_error else ""
        raise FlashAnalysisError(f"refusing to write because active bank has no patch candidate{detail}")
    return active


def _patch_preflight_lines(reason: str, inspection: FlashInspection) -> list[str]:
    return [
        reason,
        bank_inspection_status_line(inspection.primary),
        bank_inspection_status_line(inspection.secondary),
        "Use --force to patch the primary bank anyway after reviewing the backup status.",
    ]


def _both_backup_banks_valid(inspection: FlashInspection) -> bool:
    return inspection.primary.backup_valid and inspection.secondary.backup_valid


def _force_warnings(inspection: FlashInspection) -> tuple[str, ...]:
    warnings: list[str] = []
    if not _both_backup_banks_valid(inspection):
        warnings.append("patch forced despite one or more invalid backup banks")
    if not inspection.primary.active_candidate:
        warnings.append("patch forced even though the primary bank did not pass active-candidate checks")
    return tuple(warnings)


def require_primary_patch_ready(inspection: FlashInspection, *, force: bool = False) -> BankAnalysis:
    primary = inspection.primary
    if primary.analysis is None:
        lines = [
            "refusing to patch primary because the primary firmware bank could not be analyzed",
            bank_inspection_status_line(inspection.primary),
            bank_inspection_status_line(inspection.secondary),
        ]
        raise FlashAnalysisError("\n".join(lines))

    if not force and not _both_backup_banks_valid(inspection):
        raise FlashAnalysisError(
            "\n".join(_patch_preflight_lines(
                "refusing to patch primary because both firmware banks must be valid backups",
                inspection,
            ))
        )

    if not force and not primary.active_candidate:
        raise FlashAnalysisError(
            "\n".join(_patch_preflight_lines(
                "refusing to patch primary because primary is not an active firmware candidate",
                inspection,
            ))
        )

    analysis = primary.analysis
    if analysis.login.classification == "already_patched":
        return analysis
    if analysis.login.classification != "stock":
        raise FlashAnalysisError(
            f"refusing to patch primary bank with LOGIN classification {analysis.login.classification}"
        )
    if analysis.patch is None:
        detail = f": {analysis.patch_error}" if analysis.patch_error else ""
        raise FlashAnalysisError(f"refusing to patch because primary bank has no patch candidate{detail}")
    return analysis


def require_active_for_read_plan(analysis: FlashAnalysis) -> BankAnalysis:
    active = analysis.active
    if active is None:
        raise FlashAnalysisError(active_selection_error_message(analysis, write=False))
    return active


def plan_patch_primary(
    inspection: FlashInspection,
    *,
    force: bool = False,
    syap: str | int | None,
    firmware_template: Path | None,
    firmware_version: str | None = None,
    cache_dir: Path | None = None,
) -> FlashPlan:
    primary = require_primary_patch_ready(inspection, force=force)
    warnings = _force_warnings(inspection) if force else ()
    if primary.login.classification == "already_patched":
        return FlashPlan(
            mode="patch",
            target_bank=primary,
            payload=None,
            apple_match=None,
            already_satisfied=True,
            warnings=warnings,
        )
    payload = build_patch_payload_for_bank(
        primary,
        syap=syap,
        firmware_template=firmware_template,
        firmware_version=firmware_version,
        cache_dir=cache_dir,
    )
    return FlashPlan(
        mode="patch",
        target_bank=primary,
        payload=payload,
        apple_match=None,
        already_satisfied=False,
        warnings=warnings,
    )


def plan_restore_apple(
    analysis: FlashAnalysis,
    *,
    syap: str | int | None,
    firmware_template: Path | None,
    firmware_version: str | None = None,
    cache_dir: Path | None = None,
) -> FlashPlan:
    active = require_active_and_inactive_valid(analysis)
    payload = build_restore_payload_for_active_bank(
        active,
        syap=syap,
        firmware_template=firmware_template,
        firmware_version=firmware_version,
        cache_dir=cache_dir,
    )
    already_satisfied = active.data[: len(payload.expected_prefix)] == payload.expected_prefix
    match = apple_match_from_restore_payload(payload=payload, matched=already_satisfied)
    return FlashPlan(
        mode="restore",
        target_bank=active,
        payload=payload,
        apple_match=match,
        already_satisfied=already_satisfied,
        warnings=(),
    )


def plan_check_apple(
    analysis: FlashAnalysis,
    *,
    syap: str | int | None,
    firmware_template: Path | None,
    firmware_version: str | None = None,
    cache_dir: Path | None = None,
) -> FlashPlan:
    active = require_active_for_read_plan(analysis)
    match = find_apple_firmware_match(
        active,
        syap=syap,
        firmware_template=firmware_template,
        firmware_version=firmware_version,
        cache_dir=cache_dir,
    )
    return FlashPlan(
        mode="check_apple",
        target_bank=active,
        payload=None,
        apple_match=match,
        already_satisfied=match.matched,
        warnings=(),
    )


def plan_download_only(
    analysis: FlashAnalysis,
    *,
    syap: str | int | None,
    firmware_template: Path | None,
    firmware_version: str | None = None,
    cache_dir: Path | None = None,
) -> FlashPlan:
    active = require_active_for_read_plan(analysis)
    payload = build_restore_payload_for_active_bank(
        active,
        syap=syap,
        firmware_template=firmware_template,
        firmware_version=firmware_version,
        cache_dir=cache_dir,
    )
    already_satisfied = active.data[: len(payload.expected_prefix)] == payload.expected_prefix
    match = apple_match_from_restore_payload(payload=payload, matched=already_satisfied)
    return FlashPlan(
        mode="download_only",
        target_bank=active,
        payload=payload,
        apple_match=match,
        already_satisfied=already_satisfied,
        warnings=(),
    )


def apple_match_from_restore_payload(*, payload: AcpFlashPayload, matched: bool) -> AppleFirmwareMatch:
    return AppleFirmwareMatch(
        matched=matched,
        template_source=payload.template_source,
        template_path=payload.template_path,
        template_product_id=payload.template_product_id,
        template_version=payload.template_version,
        template_sha256=payload.template_sha256,
        inner_sha256=payload.expected_prefix_sha256,
        inner_size=len(payload.expected_prefix),
        key_id=payload.key_id,
        inner_model=payload.inner_model,
        inner_version=payload.inner_version,
    )


def active_checksum_property(bank_name: str) -> str:
    if bank_name == "primary":
        return "cks1"
    if bank_name == "secondary":
        return "cks2"
    raise FlashAnalysisError(f"unknown active bank: {bank_name}")


def expected_bank_after_write(active: BankAnalysis, payload: AcpFlashPayload) -> tuple[bytes, int]:
    if len(payload.expected_prefix) != active.footer.end_offset:
        raise FlashAnalysisError(
            "flash payload expected prefix length does not match active bank footer end_offset: "
            f"payload={len(payload.expected_prefix)}, active_end_offset={active.footer.end_offset}"
        )
    expected = bytearray(active.data)
    expected[: active.footer.end_offset] = payload.expected_prefix
    checksum = zlib.adler32(memoryview(expected)[: active.footer.end_offset]) & 0xFFFFFFFF
    expected[active.footer.offset : active.footer.offset + 4] = struct.pack(">I", checksum)
    return bytes(expected), checksum


def write_and_validate_plan(
    *,
    connection: SshConnection,
    acp_host: str,
    plan: FlashPlan,
    os_release: str,
    flash_firmware_bank_func: object,
    dump_remote_bank_func: object,
    get_property_int_func: object,
    timeout: int,
) -> dict[str, object]:
    if plan.target_bank is None or plan.payload is None:
        raise FlashAnalysisError("flash plan has no write payload")
    active = plan.target_bank
    payload = plan.payload
    try:
        result = flash_firmware_bank_func(
            acp_host,
            connection.password,
            active.name,
            payload.data,
            timeout=timeout,
        )
    except ACPError as exc:
        raise FlashAnalysisError(f"ACP flash command failed: {exc}") from exc

    readback = dump_remote_bank_func(connection, active.device)
    readback_sha256 = sha256_hex(readback)
    expected_prefix = payload.expected_prefix
    actual_prefix = readback[: len(expected_prefix)]
    actual_prefix_sha256 = sha256_hex(actual_prefix)
    if actual_prefix != expected_prefix:
        raise FlashAnalysisError(
            "read-back firmware bank prefix SHA-256 mismatch after ACP write: "
            f"got {actual_prefix_sha256}, expected {payload.expected_prefix_sha256}"
        )
    expected_bank, expected_footer_checksum = expected_bank_after_write(active, payload)
    expected_bank_sha256 = sha256_hex(expected_bank)
    if readback != expected_bank:
        raise FlashAnalysisError(
            "read-back firmware bank SHA-256 mismatch after ACP write: "
            f"got {readback_sha256}, expected {expected_bank_sha256}"
        )

    checksum_property = active_checksum_property(active.name)
    try:
        acp_checksum = get_property_int_func(acp_host, connection.password, checksum_property)
    except ACPError as exc:
        raise FlashAnalysisError(f"ACP checksum property {checksum_property} read failed after write: {exc}") from exc
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
    if payload.expected_login_classification is not None and readback_analysis.login.classification != payload.expected_login_classification:
        raise FlashAnalysisError(
            f"read-back firmware bank LOGIN classification is {readback_analysis.login.classification}; "
            f"expected {payload.expected_login_classification}"
        )

    return {
        "mode": plan.mode,
        "bank": active.name,
        "device": active.device,
        "command": f"0x{result.command:02x}",
        "reply_body_size": len(result.reply_body),
        "reply_body_sha256": sha256_hex(result.reply_body),
        "firmware_payload_sha256": payload.payload_sha256,
        "firmware_payload_size": len(payload.data),
        "expected_prefix_sha256": payload.expected_prefix_sha256,
        "expected_prefix_size": len(payload.expected_prefix),
        "expected_bank_sha256": expected_bank_sha256,
        "readback_sha256": readback_sha256,
        "readback_prefix_sha256": actual_prefix_sha256,
        "acp_checksum_property": checksum_property,
        "acp_checksum": f"0x{acp_checksum:08x}",
        "footer_checksum": f"0x{readback_analysis.footer.checksum:08x}",
        "expected_footer_checksum": f"0x{expected_footer_checksum:08x}",
        "login_classification": readback_analysis.login.classification,
    }
