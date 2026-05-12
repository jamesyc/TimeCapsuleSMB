from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from timecapsulesmb.apple_firmware import (
    FirmwareTemplateCandidate,
    UNSUPPORTED_FIRMWARE_KEY_MESSAGE,
    is_missing_key_error,
    normalize_syap,
    refresh_cached_firmware_template_candidate,
    resolve_firmware_template_candidates,
)
from timecapsulesmb.basebinary import (
    BasebinaryError,
    NestedBasebinary,
    compose_nested_basebinary,
    parse_nested_basebinary,
)
from timecapsulesmb.flash import BankAnalysis, FlashAnalysisError, classify_firmware_prefix_login, sha256_hex


T = TypeVar("T")


@dataclass(frozen=True)
class AcpFlashPayload:
    data: bytes
    expected_prefix: bytes
    expected_login_classification: str | None
    template_source: str
    template_path: Path | None
    template_product_id: str | None
    template_version: str | None
    template_sha256: str
    payload_sha256: str
    key_id: str | None
    inner_model: int
    inner_version: int
    inner_payload_size: int

    @property
    def expected_prefix_sha256(self) -> str:
        return sha256_hex(self.expected_prefix)

    def to_jsonable(self) -> dict[str, object]:
        return {
            "template_source": self.template_source,
            "template_path": None if self.template_path is None else str(self.template_path),
            "template_product_id": self.template_product_id,
            "template_version": self.template_version,
            "template_sha256": self.template_sha256,
            "payload_sha256": self.payload_sha256,
            "payload_size": len(self.data),
            "expected_prefix_sha256": self.expected_prefix_sha256,
            "expected_prefix_size": len(self.expected_prefix),
            "expected_login_classification": self.expected_login_classification,
            "key_id": self.key_id,
            "inner_model": self.inner_model,
            "inner_version": f"0x{self.inner_version:08x}",
            "inner_payload_size": self.inner_payload_size,
        }


@dataclass(frozen=True)
class AppleFirmwareMatch:
    matched: bool
    template_source: str
    template_path: Path | None
    template_product_id: str | None
    template_version: str | None
    template_sha256: str
    inner_sha256: str
    inner_size: int
    key_id: str | None
    inner_model: int
    inner_version: int

    def to_jsonable(self) -> dict[str, object]:
        return {
            "matched": self.matched,
            "template_source": self.template_source,
            "template_path": None if self.template_path is None else str(self.template_path),
            "template_product_id": self.template_product_id,
            "template_version": self.template_version,
            "template_sha256": self.template_sha256,
            "inner_sha256": self.inner_sha256,
            "inner_size": self.inner_size,
            "key_id": self.key_id,
            "inner_model": self.inner_model,
            "inner_version": f"0x{self.inner_version:08x}",
        }


def parse_firmware_template_for_syap(*, candidate: FirmwareTemplateCandidate, syap: str | int | None) -> NestedBasebinary:
    syap_model = int(normalize_syap(syap), 10)
    try:
        template = parse_nested_basebinary(candidate.data)
    except BasebinaryError as exc:
        raise FlashAnalysisError(str(exc)) from exc
    if template.inner.header.model != syap_model:
        raise FlashAnalysisError(
            f"firmware template model {template.inner.header.model} does not match device syAP {syap_model}"
        )
    return template


def _payload_from_template(
    *,
    candidate: FirmwareTemplateCandidate,
    template: NestedBasebinary,
    data: bytes,
    expected_prefix: bytes,
    expected_login_classification: str | None,
) -> AcpFlashPayload:
    return AcpFlashPayload(
        data=data,
        expected_prefix=expected_prefix,
        expected_login_classification=expected_login_classification,
        template_source=candidate.source,
        template_path=candidate.path,
        template_product_id=candidate.product_id,
        template_version=candidate.version,
        template_sha256=sha256_hex(candidate.data),
        payload_sha256=sha256_hex(data),
        key_id=template.inner.key_id,
        inner_model=template.inner.header.model,
        inner_version=template.inner.header.version,
        inner_payload_size=len(template.inner.payload),
    )


def build_patch_payload_from_template(
    *,
    active: BankAnalysis,
    syap: str | int | None,
    candidate: FirmwareTemplateCandidate,
) -> AcpFlashPayload:
    assert active.patch is not None
    template = parse_firmware_template_for_syap(candidate=candidate, syap=syap)
    live_prefix = active.data[: active.footer.end_offset]
    if len(template.inner.payload) != len(live_prefix):
        raise FlashAnalysisError(
            "firmware template payload length does not match active bank prefix: "
            f"template={len(template.inner.payload)}, active_prefix={len(live_prefix)}"
        )
    if template.inner.payload != live_prefix:
        raise FlashAnalysisError("firmware template decrypted payload does not match the live active bank")

    patched_prefix = active.patch.target_bank[: active.footer.end_offset]
    payload = compose_nested_basebinary(template, patched_prefix)
    try:
        reparsed = parse_nested_basebinary(payload)
    except BasebinaryError as exc:
        raise FlashAnalysisError(f"composed basebinary payload did not reparse: {exc}") from exc
    if reparsed.inner.payload != patched_prefix:
        raise FlashAnalysisError("composed basebinary payload did not decrypt back to the patched bank prefix")
    if reparsed.inner.header != template.inner.header or reparsed.outer.header != template.outer.header:
        raise FlashAnalysisError("composed basebinary payload changed template headers unexpectedly")

    return _payload_from_template(
        candidate=candidate,
        template=template,
        data=payload,
        expected_prefix=patched_prefix,
        expected_login_classification="already_patched",
    )


def build_restore_payload_from_template(
    *,
    active: BankAnalysis,
    syap: str | int | None,
    candidate: FirmwareTemplateCandidate,
) -> AcpFlashPayload:
    template = parse_firmware_template_for_syap(candidate=candidate, syap=syap)
    if len(template.inner.payload) != active.footer.end_offset:
        raise FlashAnalysisError(
            "Apple firmware payload length does not match active bank footer end_offset: "
            f"template={len(template.inner.payload)}, active_end_offset={active.footer.end_offset}"
        )
    login = classify_firmware_prefix_login(template.inner.payload)
    if login.classification != "stock":
        raise FlashAnalysisError(
            f"Apple firmware template LOGIN classification is {login.classification}; expected stock"
        )
    return _payload_from_template(
        candidate=candidate,
        template=template,
        data=candidate.data,
        expected_prefix=template.inner.payload,
        expected_login_classification="stock",
    )


def apple_match_from_template(
    *,
    bank: BankAnalysis,
    syap: str | int | None,
    candidate: FirmwareTemplateCandidate,
) -> AppleFirmwareMatch:
    template = parse_firmware_template_for_syap(candidate=candidate, syap=syap)
    matched = len(template.inner.payload) == bank.footer.end_offset and bank.data[: bank.footer.end_offset] == template.inner.payload
    return AppleFirmwareMatch(
        matched=matched,
        template_source=candidate.source,
        template_path=candidate.path,
        template_product_id=candidate.product_id,
        template_version=candidate.version,
        template_sha256=sha256_hex(candidate.data),
        inner_sha256=sha256_hex(template.inner.payload),
        inner_size=len(template.inner.payload),
        key_id=template.inner.key_id,
        inner_model=template.inner.header.model,
        inner_version=template.inner.header.version,
    )


def _try_candidates(
    *,
    syap: str | int | None,
    candidates: Iterable[FirmwareTemplateCandidate],
    build: Callable[[FirmwareTemplateCandidate], T],
) -> T:
    normalized_syap = normalize_syap(syap)
    errors: list[str] = []
    missing_key_errors = 0
    candidate_count = 0
    for candidate in candidates:
        candidate_count += 1
        result, message, missing_key_error = _build_candidate_with_cache_refresh(candidate, build)
        if message is None:
            assert result is not None
            return result
        errors.append(f"{candidate.source}: {message}")
        if missing_key_error:
            missing_key_errors += 1

    if missing_key_errors and missing_key_errors == candidate_count:
        raise FlashAnalysisError(f"{UNSUPPORTED_FIRMWARE_KEY_MESSAGE} syAP={normalized_syap}")
    detail = "; ".join(errors[:3])
    if len(errors) > 3:
        detail += f"; ... {len(errors) - 3} more template errors"
    raise FlashAnalysisError(f"no firmware template matched the active bank for syAP {normalized_syap}: {detail}")


def _build_candidate_with_cache_refresh(
    candidate: FirmwareTemplateCandidate,
    build: Callable[[FirmwareTemplateCandidate], T],
) -> tuple[T | None, str | None, bool]:
    try:
        return build(candidate), None, False
    except FlashAnalysisError as exc:
        initial_message = str(exc)

    try:
        refreshed = refresh_cached_firmware_template_candidate(candidate)
    except FlashAnalysisError as exc:
        return None, f"{initial_message}; cache refresh failed: {exc}", False
    if refreshed is None:
        return None, initial_message, is_missing_key_error(initial_message)

    try:
        return build(refreshed), None, False
    except FlashAnalysisError as exc:
        refreshed_message = str(exc)
        return None, f"{initial_message}; after cache refresh: {refreshed_message}", is_missing_key_error(refreshed_message)


def build_patch_payload_for_active_bank(
    active: BankAnalysis,
    *,
    syap: str | int | None,
    firmware_template: Path | None,
    firmware_version: str | None = None,
    cache_dir: Path | None = None,
) -> AcpFlashPayload:
    candidates = resolve_firmware_template_candidates(
        syap=syap,
        firmware_template=firmware_template,
        firmware_version=firmware_version,
        cache_dir=cache_dir,
    )
    return _try_candidates(
        syap=syap,
        candidates=candidates,
        build=lambda candidate: build_patch_payload_from_template(active=active, syap=syap, candidate=candidate),
    )


def build_restore_payload_for_active_bank(
    active: BankAnalysis,
    *,
    syap: str | int | None,
    firmware_template: Path | None,
    firmware_version: str | None = None,
    cache_dir: Path | None = None,
) -> AcpFlashPayload:
    candidates = resolve_firmware_template_candidates(
        syap=syap,
        firmware_template=firmware_template,
        firmware_version=firmware_version,
        cache_dir=cache_dir,
    )
    return _try_candidates(
        syap=syap,
        candidates=candidates,
        build=lambda candidate: build_restore_payload_from_template(active=active, syap=syap, candidate=candidate),
    )


def find_apple_firmware_match(
    bank: BankAnalysis,
    *,
    syap: str | int | None,
    firmware_template: Path | None,
    firmware_version: str | None = None,
    cache_dir: Path | None = None,
) -> AppleFirmwareMatch:
    candidates = resolve_firmware_template_candidates(
        syap=syap,
        firmware_template=firmware_template,
        firmware_version=firmware_version,
        cache_dir=cache_dir,
    )
    normalized_syap = normalize_syap(syap)
    errors: list[str] = []
    missing_key_errors = 0
    candidate_count = 0
    first_valid: AppleFirmwareMatch | None = None
    for candidate in candidates:
        candidate_count += 1
        match, message, missing_key_error = _build_candidate_with_cache_refresh(
            candidate,
            lambda refreshed_candidate: apple_match_from_template(bank=bank, syap=syap, candidate=refreshed_candidate),
        )
        if message is not None:
            errors.append(f"{candidate.source}: {message}")
            if missing_key_error:
                missing_key_errors += 1
            continue
        assert match is not None
        if first_valid is None:
            first_valid = match
        if match.matched:
            return match
    if first_valid is not None:
        return first_valid
    if missing_key_errors and missing_key_errors == candidate_count:
        raise FlashAnalysisError(f"{UNSUPPORTED_FIRMWARE_KEY_MESSAGE} syAP={normalized_syap}")
    detail = "; ".join(errors[:3])
    if len(errors) > 3:
        detail += f"; ... {len(errors) - 3} more template errors"
    raise FlashAnalysisError(f"no Apple firmware template could be checked for syAP {normalized_syap}: {detail}")
