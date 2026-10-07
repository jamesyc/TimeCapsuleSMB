from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import struct
import zlib

from timecapsulesmb.apple_firmware import normalize_syap
from timecapsulesmb.flash import (
    BankAnalysis,
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
    build_download_payload_for_syap,
    build_patch_payload_for_bank,
    build_restore_payload_for_bank,
    find_apple_firmware_match,
)
from timecapsulesmb.integrations.acp import ACPError
from timecapsulesmb.transport.ssh import SshConnection, SshError


# Rewriting the secondary bank (plan/issue177-acpd/flash-secondary-20261007).
# ACPd cannot write it on shipping units: its write-secondary command needs the
# factory `diag` unlock, which has no ACP setter. Apple's own flashctl erases
# the bank and dd writes it through the same raw device ACPd programs a bank
# through. On both kernels below an erase of this unit cannot reach any other
# unit, and a raw write is cut off at the end of the bank. The image's footer
# is written last, so an interrupted write leaves an erased footer, which the
# bootloader and ACPd both skip; the primary is never touched.
SECONDARY_BANK_DEVICE = "/dev/rflash1.raw"
# flashctl reads no input, but </dev/null keeps it from ever taking image
# bytes off the stdin that dd must get whole.
SECONDARY_BANK_WRITE_COMMAND = (
    f"/sbin/flashctl {SECONDARY_BANK_DEVICE} erase </dev/null && /bin/dd of={SECONDARY_BANK_DEVICE} ibs=4096 obs=65536"
)
FIRMWARE_BANK_SIZE = 0x700000
FIRMWARE_BANK_FOOTER_OFFSET = FIRMWARE_BANK_SIZE - 32
# ACPd refuses bank images under 3 MiB.
MIN_FIRMWARE_BANK_IMAGE_SIZE = 3 * 1024 * 1024
# Kirkwood (113, 116: SPI NOR, no lock bits) and Orion (106, 109: CFI NOR) are
# the kernels whose flash driver was checked for this write. Never run
# `flashctl unlock`: on Orion it erases every sector's protection bit chip-wide,
# the bootloader's included.
SECONDARY_REFRESH_SYAPS = frozenset({"106", "109", "113", "116"})
SECONDARY_BANK_WRITE_PROTECTED_MESSAGE = (
    "The secondary (backup) firmware bank may be write-protected: after the write it still read back "
    "its previous contents. The primary firmware is unchanged."
)
SECONDARY_BANK_WRITE_FAILED_MESSAGE = (
    "The secondary (backup) firmware bank could not be written ({detail}). "
    "The primary firmware is unchanged; you can retry."
)
SECONDARY_BANK_UNVERIFIED_MESSAGE = (
    "The secondary (backup) firmware bank was written but could not be verified ({detail}). "
    "The primary firmware is unchanged; you can retry. "
    "If it keeps failing, this Time Capsule's flash may be failing."
)


class SecondaryBankInvalidError(FlashAnalysisError):
    """Patching was refused only because the secondary bank is not a valid backup."""


class SecondaryBankReadMismatchError(FlashAnalysisError):
    """This backup's read of the secondary bank and ACPd's own read disagree about its footer."""


@dataclass(frozen=True)
class SecondaryBankRefresh:
    image: bytes
    footer_checksum: int
    end_offset: int
    primary_sha256: str
    previous_secondary_sha256: str
    # The primary's LOGIN state decides the next step: patch a stock primary,
    # or restore again to put stock firmware back on a patched one.
    primary_login: str

    @property
    def image_sha256(self) -> str:
        return sha256_hex(self.image)

    def to_jsonable(self) -> dict[str, object]:
        return {
            "device": SECONDARY_BANK_DEVICE,
            "image_sha256": self.image_sha256,
            "image_size": len(self.image),
            "footer_checksum": f"0x{self.footer_checksum:08x}",
            "end_offset": self.end_offset,
            "previous_secondary_sha256": self.previous_secondary_sha256,
            "primary_login": self.primary_login,
        }


@dataclass(frozen=True)
class BankAppleFirmwareMatch:
    bank: str
    match: AppleFirmwareMatch

    def to_jsonable(self) -> dict[str, object]:
        return {
            "bank": self.bank,
            "match": self.match.to_jsonable(),
        }


@dataclass(frozen=True)
class FlashPlan:
    mode: str
    target_bank: BankAnalysis | None
    payload: AcpFlashPayload | None
    apple_match: AppleFirmwareMatch | None
    already_satisfied: bool
    warnings: tuple[str, ...] = ()
    apple_matches: tuple[BankAppleFirmwareMatch, ...] = ()
    apple_match_status: str | None = None
    # Set when restore rewrites an invalid secondary bank; it has no analysis.
    secondary_refresh: SecondaryBankRefresh | None = None

    @property
    def write_requested(self) -> bool:
        return self.mode in {"patch", "restore"} and not self.already_satisfied

    @property
    def target_name(self) -> str | None:
        if self.secondary_refresh is not None:
            return "secondary"
        return None if self.target_bank is None else self.target_bank.name

    def to_jsonable(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "target_bank": self.target_name,
            "secondary_refresh": None if self.secondary_refresh is None else self.secondary_refresh.to_jsonable(),
            "write_requested": self.write_requested,
            "already_satisfied": self.already_satisfied,
            "warnings": list(self.warnings),
            "payload": None if self.payload is None else self.payload.to_jsonable(),
            "apple_match": None if self.apple_match is None else self.apple_match.to_jsonable(),
            "apple_matches": [match.to_jsonable() for match in self.apple_matches],
            "apple_match_status": self.apple_match_status,
        }


RESTORE_PRIMARY_AMBIGUOUS_WARNING = (
    "restore targets primary because multiple firmware banks passed active selection checks"
)


def _patch_preflight_lines(reason: str, inspection: FlashInspection) -> list[str]:
    return [
        reason,
        bank_inspection_status_line(inspection.primary),
        bank_inspection_status_line(inspection.secondary),
        # The app shows this text too, and it has no force option.
        "From the command line, `tcapsule flash --patch --force` patches the primary bank anyway "
        "after you review the backup status.",
    ]


def _restore_preflight_lines(reason: str, inspection: FlashInspection) -> list[str]:
    return [
        reason,
        bank_inspection_status_line(inspection.primary),
        bank_inspection_status_line(inspection.secondary),
    ]


def _both_backup_banks_valid(inspection: FlashInspection) -> bool:
    return inspection.primary.backup_valid and inspection.secondary.backup_valid


def _only_secondary_invalid(inspection: FlashInspection) -> bool:
    """The primary is a valid backup and the bank this device runs; the secondary is not a valid backup."""
    return (
        inspection.primary.backup_valid
        and inspection.primary.active_candidate
        and not inspection.secondary.backup_valid
    )


def _secondary_reads(inspection: FlashInspection) -> tuple[str, str | None]:
    """Compare this backup's read of the secondary footer with ACPd's own read.

    ACPd's cks2 is the adler32 of its own read of the bank, up to the end
    offset in the footer it reads, and 0 when it finds no footer. Returns
    "damaged" when both reads fail the footer at bank end - 32 (where ACPd and
    the bootloader read it), "intact" when both pass, and "mismatch" with the
    disagreement otherwise. Some units' flash reads differently from one read
    to the next: when either read passes, the bank on the chip may be a good
    fallback, and rewriting it would only open a window without one.
    """
    bank = inspection.secondary
    cks2 = bank.acp_checksum
    footer = bank.stored_footer_checksum
    if cks2 is None:
        return "mismatch", "this backup has no ACP checksum (cks2) for the secondary bank"
    if footer is None:
        if cks2 == 0:
            return "damaged", None
        return "mismatch", f"this backup's read found no footer, but ACPd's own read found one (cks2 0x{cks2:08x})"
    acpd_intact = cks2 == footer
    if bank.stored_footer_matches_data == acpd_intact:
        return ("intact" if acpd_intact else "damaged"), None
    if acpd_intact:
        return "mismatch", (
            f"this backup's read does not match the footer checksum 0x{footer:08x}, "
            f"but ACPd's own read does (cks2 0x{cks2:08x})"
        )
    return "mismatch", (
        f"this backup's read matches the footer checksum 0x{footer:08x}, "
        f"but ACPd's own read does not (cks2 0x{cks2:08x})"
    )


def _secondary_needs_refresh(inspection: FlashInspection) -> bool:
    """Only the secondary is bad, and both reads of it fail its footer."""
    return _only_secondary_invalid(inspection) and _secondary_reads(inspection)[0] == "damaged"


def _secondary_read_mismatch_reason(action: str, inspection: FlashInspection) -> str | None:
    """The refusal reason when only the secondary is invalid but its two reads disagree."""
    if not _only_secondary_invalid(inspection):
        return None
    state, detail = _secondary_reads(inspection)
    if state != "mismatch":
        return None
    return (
        f"refusing to {action} because the secondary (backup) firmware bank read differently in two reads: "
        f"{detail}. Back up and inspect again."
    )


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

    mismatch = _secondary_read_mismatch_reason("patch primary", inspection)
    if not force and mismatch is not None:
        raise SecondaryBankReadMismatchError("\n".join(_patch_preflight_lines(mismatch, inspection)))

    if not force and _secondary_needs_refresh(inspection):
        raise SecondaryBankInvalidError(
            "\n".join([
                "refusing to patch primary because the secondary (backup) firmware bank is not a valid backup",
                bank_inspection_status_line(inspection.primary),
                bank_inspection_status_line(inspection.secondary),
                "Run restore (`tcapsule flash --restore`) to rewrite the secondary bank with Apple firmware, "
                "then patch again.",
            ])
        )

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


def require_restore_target_bank(inspection: FlashInspection) -> tuple[BankAnalysis, tuple[str, ...]]:
    if not _both_backup_banks_valid(inspection):
        raise FlashAnalysisError(
            "\n".join(_restore_preflight_lines(
                "refusing to restore because both firmware banks must be valid backups",
                inspection,
            ))
        )

    active = _selected_active_for_read_plan(inspection)
    if active is not None:
        return active, ()

    if inspection.active_selection.status == "multiple_candidates":
        primary = inspection.primary
        if primary.analysis is not None and primary.active_candidate:
            return primary.analysis, (RESTORE_PRIMARY_AMBIGUOUS_WARNING,)

    analysis = inspection.strict_analysis
    if analysis is not None:
        raise FlashAnalysisError(active_selection_error_message(analysis, write=True))
    raise FlashAnalysisError(
        "\n".join(_restore_preflight_lines(
            "refusing to restore because firmware bank inspection failed",
            inspection,
        ))
    )


def _candidate_analyses(inspection: FlashInspection) -> tuple[BankAnalysis, ...]:
    candidates: list[BankAnalysis] = []
    for bank in (inspection.primary, inspection.secondary):
        if bank.active_candidate and bank.analysis is not None:
            candidates.append(bank.analysis)
    return tuple(candidates)


def _require_read_candidates(inspection: FlashInspection) -> tuple[BankAnalysis, ...]:
    candidates = _candidate_analyses(inspection)
    if candidates:
        return candidates
    analysis = inspection.strict_analysis
    if analysis is not None:
        raise FlashAnalysisError(active_selection_error_message(analysis, write=False))
    raise FlashAnalysisError("no firmware bank could be checked against Apple firmware")


def _selected_active_for_read_plan(inspection: FlashInspection) -> BankAnalysis | None:
    active_bank = inspection.active_bank
    if active_bank == "primary" and inspection.primary.analysis is not None:
        return inspection.primary.analysis
    if active_bank == "secondary" and inspection.secondary.analysis is not None:
        return inspection.secondary.analysis
    return None


def _apple_match_status(matches: tuple[BankAppleFirmwareMatch, ...]) -> str:
    if not matches:
        return "not_checked"
    matched_count = sum(1 for result in matches if result.match.matched)
    if matched_count == len(matches):
        return "all_candidates_match"
    if matched_count == 0:
        return "no_candidates_match"
    return "some_candidates_match"


def _aggregate_apple_match(matches: tuple[BankAppleFirmwareMatch, ...]) -> AppleFirmwareMatch | None:
    if not matches:
        return None
    first = matches[0].match
    status = _apple_match_status(matches)
    if status == "all_candidates_match":
        return first
    return AppleFirmwareMatch(
        matched=False,
        template_source=first.template_source,
        template_path=first.template_path,
        template_product_id=first.template_product_id,
        template_version=first.template_version,
        template_sha256=first.template_sha256,
        inner_sha256=first.inner_sha256,
        inner_size=first.inner_size,
        key_id=first.key_id,
        inner_model=first.inner_model,
        inner_version=first.inner_version,
    )


def _match_apple_firmware_for_candidates(
    candidates: tuple[BankAnalysis, ...],
    *,
    syap: str | int | None,
    firmware_template: Path | None,
    firmware_version: str | None,
    cache_dir: Path | None,
) -> tuple[BankAppleFirmwareMatch, ...]:
    results: list[BankAppleFirmwareMatch] = []
    for bank in candidates:
        match = find_apple_firmware_match(
            bank,
            syap=syap,
            firmware_template=firmware_template,
            firmware_version=firmware_version,
            cache_dir=cache_dir,
        )
        results.append(BankAppleFirmwareMatch(bank=bank.name, match=match))
    return tuple(results)


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
    inspection: FlashInspection,
    *,
    syap: str | int | None,
    firmware_template: Path | None,
    firmware_version: str | None = None,
    cache_dir: Path | None = None,
) -> FlashPlan:
    mismatch = _secondary_read_mismatch_reason("rewrite the secondary bank", inspection)
    if mismatch is not None:
        raise SecondaryBankReadMismatchError("\n".join(_restore_preflight_lines(mismatch, inspection)))
    if _secondary_needs_refresh(inspection):
        return plan_refresh_secondary(
            inspection,
            syap=syap,
            firmware_template=firmware_template,
            firmware_version=firmware_version,
            cache_dir=cache_dir,
        )
    target, warnings = require_restore_target_bank(inspection)
    payload = build_restore_payload_for_bank(
        target,
        syap=syap,
        firmware_template=firmware_template,
        firmware_version=firmware_version,
        cache_dir=cache_dir,
    )
    already_satisfied = target.data[: len(payload.expected_prefix)] == payload.expected_prefix
    match = apple_match_from_restore_payload(payload=payload, matched=already_satisfied)
    return FlashPlan(
        mode="restore",
        target_bank=target,
        payload=payload,
        apple_match=match,
        already_satisfied=already_satisfied,
        warnings=warnings,
    )


def build_secondary_bank_image(prefix: bytes) -> tuple[bytes, int]:
    """A whole bank as Apple lays it out: firmware, 0xFF, then the footer at bank end - 32."""
    if not MIN_FIRMWARE_BANK_IMAGE_SIZE <= len(prefix) <= FIRMWARE_BANK_FOOTER_OFFSET:
        raise FlashAnalysisError(
            f"Apple firmware payload size {len(prefix)} does not fit a firmware bank "
            f"({MIN_FIRMWARE_BANK_IMAGE_SIZE}..{FIRMWARE_BANK_FOOTER_OFFSET} bytes)"
        )
    checksum = zlib.adler32(prefix) & 0xFFFFFFFF
    image = bytearray(b"\xff" * FIRMWARE_BANK_SIZE)
    image[: len(prefix)] = prefix
    image[FIRMWARE_BANK_FOOTER_OFFSET : FIRMWARE_BANK_FOOTER_OFFSET + 8] = struct.pack(">II", checksum, len(prefix))
    return bytes(image), checksum


def plan_refresh_secondary(
    inspection: FlashInspection,
    *,
    syap: str | int | None,
    firmware_template: Path | None,
    firmware_version: str | None = None,
    cache_dir: Path | None = None,
) -> FlashPlan:
    """Restore the backup bank: the newest Apple firmware for this model into the invalid secondary.

    The caller has checked that only the secondary is invalid and both reads of it fail its footer.
    """
    normalized_syap = normalize_syap(syap)
    if normalized_syap not in SECONDARY_REFRESH_SYAPS:
        raise FlashAnalysisError("\n".join(_restore_preflight_lines(
            "refusing to restore because the secondary firmware bank is invalid "
            f"and rewriting it is not supported on syAP {normalized_syap} yet",
            inspection,
        )))
    for bank in (inspection.primary, inspection.secondary):
        if bank.size != FIRMWARE_BANK_SIZE:
            raise FlashAnalysisError("\n".join(_restore_preflight_lines(
                f"refusing to restore the secondary bank: the {bank.name} bank read {bank.size} bytes, "
                f"expected {FIRMWARE_BANK_SIZE}",
                inspection,
            )))
    payload = build_download_payload_for_syap(
        syap=syap,
        firmware_template=firmware_template,
        firmware_version=firmware_version,
        cache_dir=cache_dir,
    )
    image, checksum = build_secondary_bank_image(payload.expected_prefix)
    primary_analysis = inspection.primary.analysis
    assert primary_analysis is not None  # a valid backup has an analysis
    return FlashPlan(
        mode="restore",
        target_bank=None,
        payload=payload,
        apple_match=apple_match_from_restore_payload(payload=payload, matched=False),
        already_satisfied=False,
        secondary_refresh=SecondaryBankRefresh(
            image=image,
            footer_checksum=checksum,
            end_offset=len(payload.expected_prefix),
            primary_sha256=inspection.primary.sha256,
            previous_secondary_sha256=inspection.secondary.sha256,
            primary_login=primary_analysis.login.classification,
        ),
    )


def plan_check_apple(
    inspection: FlashInspection,
    *,
    syap: str | int | None,
    firmware_template: Path | None,
    firmware_version: str | None = None,
    cache_dir: Path | None = None,
) -> FlashPlan:
    candidates = _require_read_candidates(inspection)
    matches = _match_apple_firmware_for_candidates(
        candidates,
        syap=syap,
        firmware_template=firmware_template,
        firmware_version=firmware_version,
        cache_dir=cache_dir,
    )
    status = _apple_match_status(matches)
    active = _selected_active_for_read_plan(inspection)
    selected_match = next((result.match for result in matches if active is not None and result.bank == active.name), None)
    match = selected_match if selected_match is not None else _aggregate_apple_match(matches)
    return FlashPlan(
        mode="check_apple",
        target_bank=active,
        payload=None,
        apple_match=match,
        already_satisfied=status == "all_candidates_match",
        warnings=(),
        apple_matches=matches,
        apple_match_status=status,
    )


def plan_download_only(
    inspection: FlashInspection,
    *,
    syap: str | int | None,
    firmware_template: Path | None,
    firmware_version: str | None = None,
    cache_dir: Path | None = None,
) -> FlashPlan:
    active = _selected_active_for_read_plan(inspection)
    payload = build_download_payload_for_syap(
        syap=syap,
        firmware_template=firmware_template,
        firmware_version=firmware_version,
        cache_dir=cache_dir,
    )
    candidates = _candidate_analyses(inspection)
    matches = tuple(
        BankAppleFirmwareMatch(
            bank=bank.name,
            match=apple_match_from_restore_payload(
                payload=payload,
                matched=bank.data[: len(payload.expected_prefix)] == payload.expected_prefix,
            ),
        )
        for bank in candidates
    )
    status = _apple_match_status(matches)
    selected_match = next((result.match for result in matches if active is not None and result.bank == active.name), None)
    match = selected_match if selected_match is not None else _aggregate_apple_match(matches)
    return FlashPlan(
        mode="download_only",
        target_bank=active,
        payload=payload,
        apple_match=match,
        already_satisfied=status == "all_candidates_match",
        warnings=(),
        apple_matches=matches,
        apple_match_status=status,
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
            "flash payload expected prefix length does not match target bank footer end_offset: "
            f"payload={len(payload.expected_prefix)}, target_end_offset={active.footer.end_offset}"
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


def _read_checksum_property(get_property_int_func: object, acp_host: str, password: str, name: str, when: str) -> int:
    try:
        return get_property_int_func(acp_host, password, name)  # type: ignore[operator]
    except ACPError as exc:
        raise FlashAnalysisError(f"ACP checksum property {name} read failed {when} the secondary bank write: {exc}") from exc


def write_and_validate_secondary_refresh(
    *,
    connection: SshConnection,
    acp_host: str,
    plan: FlashPlan,
    run_write_func: object,
    dump_remote_bank_func: object,
    get_property_int_func: object,
    timeout: int,
) -> dict[str, object]:
    """Write the secondary bank image, then prove it with independent reads.

    The kernel neither verifies a raw write nor reports an erase that left a
    sector unerased, so the whole bank is read back. ACPd's own cks2 (its
    separate sector by sector read) must equal the footer, and cks1 must not
    move: nothing here may change the primary.
    """
    refresh = plan.secondary_refresh
    if refresh is None or plan.payload is None:
        raise FlashAnalysisError("flash plan has no secondary bank image")
    password = connection.password
    cks1_before = _read_checksum_property(get_property_int_func, acp_host, password, "cks1", "before")
    try:
        run_write_func(connection, SECONDARY_BANK_WRITE_COMMAND, input_bytes=refresh.image, timeout=timeout)  # type: ignore[operator]
    except SshError as exc:
        raise FlashAnalysisError(SECONDARY_BANK_WRITE_FAILED_MESSAGE.format(detail=exc)) from exc

    readback = dump_remote_bank_func(connection, SECONDARY_BANK_DEVICE)  # type: ignore[operator]
    if readback != refresh.image:
        # Some units' flash reads differently from one read to the next.
        readback = dump_remote_bank_func(connection, SECONDARY_BANK_DEVICE)  # type: ignore[operator]
    readback_sha256 = sha256_hex(readback)
    if readback != refresh.image:
        if readback_sha256 == refresh.previous_secondary_sha256:
            raise FlashAnalysisError(SECONDARY_BANK_WRITE_PROTECTED_MESSAGE)
        raise FlashAnalysisError(SECONDARY_BANK_UNVERIFIED_MESSAGE.format(
            detail=f"read-back SHA-256 {readback_sha256}, expected {refresh.image_sha256}",
        ))

    cks2 = _read_checksum_property(get_property_int_func, acp_host, password, "cks2", "after")
    if cks2 != refresh.footer_checksum:
        raise FlashAnalysisError(SECONDARY_BANK_UNVERIFIED_MESSAGE.format(
            detail=f"ACP cks2 0x{cks2:08x}, footer 0x{refresh.footer_checksum:08x}",
        ))
    cks1_after = _read_checksum_property(get_property_int_func, acp_host, password, "cks1", "after")
    if cks1_after != cks1_before:
        raise FlashAnalysisError(
            f"ACP cks1 changed during the secondary bank write: before 0x{cks1_before:08x}, after 0x{cks1_after:08x}"
        )

    return {
        "mode": plan.mode,
        "bank": "secondary",
        "device": SECONDARY_BANK_DEVICE,
        "firmware_version": plan.payload.template_version,
        "firmware_payload_sha256": plan.payload.payload_sha256,
        "expected_prefix_sha256": plan.payload.expected_prefix_sha256,
        "expected_prefix_size": len(plan.payload.expected_prefix),
        "image_sha256": refresh.image_sha256,
        "readback_sha256": readback_sha256,
        "previous_secondary_sha256": refresh.previous_secondary_sha256,
        "acp_checksum_property": "cks2",
        "acp_checksum": f"0x{cks2:08x}",
        "footer_checksum": f"0x{refresh.footer_checksum:08x}",
        "cks1_before": f"0x{cks1_before:08x}",
        "cks1_after": f"0x{cks1_after:08x}",
    }
