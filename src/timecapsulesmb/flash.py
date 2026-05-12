from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import importlib
import re
import struct
import zlib

from timecapsulesmb.core.errors import require_python_module


STOCK_LOGIN_NETBSD4_DUMMY = (
    b"#!/bin/sh\n"
    b"#\n"
    b"# $NetBSD: LOGIN,v 1.7 2002/03/22 04:33:57 thorpej Exp $\n"
    b"#\n"
    b"\n"
    b"# PROVIDE: LOGIN\n"
    b"# REQUIRE: DAEMON\n"
    b"\n"
    b"#\tThis is a dummy dependency to ensure user services such as xdm,\n"
    b"#\tinetd, cron and kerberos are started after everything else, incase\n"
    b"#\tthe administrator has increased the system security level and\n"
    b"#\twants to delay user logins until the system is (almost) fully\n"
    b"#\toperational.\n"
)

PATCHED_LOGIN_SCRIPT = (
    b"#!/bin/sh\n"
    b"#\n"
    b"# $NetBSD: LOGIN,v 1.7 2002/03/22 04:33:57 thorpej Exp $\n"
    b"#\n"
    b"\n"
    b"# PROVIDE: LOGIN\n"
    b"# REQUIRE: DAEMON\n"
    b"\n"
    b'if [ "$1" = start ]; then\n'
    b"    if [ -x /mnt/Flash/rc.local ]; then\n"
    b"        /mnt/Flash/rc.local\n"
    b"    fi\n"
    b"fi\n"
    b"exit 0\n"
)

KNOWN_STOCK_LOGIN_SCRIPTS = (STOCK_LOGIN_NETBSD4_DUMMY,)
GZIP_MAGIC = b"\x1f\x8b\x08"
FOOTER_SCAN_BYTES = 4096
ZOPFLI_BOOTSTRAP_MESSAGE = (
    "Python package zopfli is required for flash patch compression. "
    "Run `./tcapsule bootstrap` to install it, then rerun `.venv/bin/tcapsule flash`."
)


class FlashAnalysisError(RuntimeError):
    """Raised when a firmware bank cannot be safely classified."""


@dataclass(frozen=True)
class FooterInfo:
    offset: int
    checksum: int
    end_offset: int


@dataclass(frozen=True)
class GzipMemberInfo:
    offset: int
    consumed_length: int
    decompressed: bytes

    @property
    def end_offset(self) -> int:
        return self.offset + self.consumed_length


@dataclass(frozen=True)
class LoginInfo:
    classification: str
    offset: int | None
    length: int | None
    sha256: str | None
    match_count: int

    @property
    def patchable(self) -> bool:
        return self.classification == "stock" and self.offset is not None and self.length is not None


@dataclass(frozen=True)
class CompressionResult:
    method: str
    data: bytes

    @property
    def length(self) -> int:
        return len(self.data)


@dataclass(frozen=True)
class PatchBuildInfo:
    target_bank_sha256: str
    patched_image_sha256: str
    patched_gzip_length: int
    compression_method: str
    changed_range_start: int
    changed_range_end: int
    footer_checksum: int
    target_bank: bytes


@dataclass(frozen=True)
class BankAnalysis:
    name: str
    device: str
    data: bytes
    sha256: str
    size: int
    footer: FooterInfo
    footer_valid: bool
    acp_checksum: int | None
    acp_checksum_matches: bool | None
    gzip_member: GzipMemberInfo
    decompressed_sha256: str
    login: LoginInfo
    kernel_identity_match: bool
    kernel_identity_detail: str
    patch: PatchBuildInfo | None
    patch_error: str | None

    @property
    def valid_for_active_selection(self) -> bool:
        return self.footer_valid and self.acp_checksum_matches is True and self.kernel_identity_match


@dataclass(frozen=True)
class FlashAnalysis:
    primary: BankAnalysis
    secondary: BankAnalysis
    active_bank: str | None

    @property
    def active(self) -> BankAnalysis | None:
        if self.active_bank == self.primary.name:
            return self.primary
        if self.active_bank == self.secondary.name:
            return self.secondary
        return None


def write_decision_for_bank(analysis: FlashAnalysis, bank: BankAnalysis) -> str:
    if analysis.active_bank is None:
        return "active bank selection ambiguous; no patched output written"
    if bank.name != analysis.active_bank:
        return "inactive bank left unmodified"
    if bank.login.classification == "already_patched":
        return "active bank already patched; no patched output written"
    if bank.patch is not None:
        return "active bank patch candidate"
    if bank.patch_error:
        return f"active bank patch refused: {bank.patch_error}"
    return f"active bank patch refused: LOGIN classification {bank.login.classification}"


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def find_footer(data: bytes) -> FooterInfo:
    start = max(0, len(data) - FOOTER_SCAN_BYTES)
    matches: list[FooterInfo] = []
    data_view = memoryview(data)
    for offset in range(start, len(data) - 7):
        checksum, end_offset = struct.unpack_from(">II", data, offset)
        if end_offset > len(data):
            continue
        if end_offset >= offset:
            continue
        if zlib.adler32(data_view[:end_offset]) & 0xFFFFFFFF == checksum:
            matches.append(FooterInfo(offset, checksum, end_offset))
    if len(matches) != 1:
        raise FlashAnalysisError(f"expected exactly one valid footer, found {len(matches)}")
    return matches[0]


def _decompress_gzip_member(data: bytes, offset: int) -> GzipMemberInfo | None:
    decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
    remaining = memoryview(data)[offset:]
    try:
        decompressed = decompressor.decompress(remaining) + decompressor.flush()
    except zlib.error:
        return None
    consumed = len(remaining) - len(decompressor.unused_data)
    if consumed <= 0:
        return None
    return GzipMemberInfo(offset=offset, consumed_length=consumed, decompressed=decompressed)


def find_gzip_member(data: bytes, footer: FooterInfo) -> GzipMemberInfo:
    matches: list[GzipMemberInfo] = []
    for match in re.finditer(re.escape(GZIP_MAGIC), data[: footer.end_offset]):
        member = _decompress_gzip_member(data, match.start())
        if member is None:
            continue
        if member.end_offset <= footer.end_offset:
            matches.append(member)
    if len(matches) != 1:
        raise FlashAnalysisError(f"expected exactly one valid gzip member, found {len(matches)}")
    return matches[0]


def classify_firmware_prefix_login(data: bytes) -> LoginInfo:
    footer = FooterInfo(offset=len(data), checksum=0, end_offset=len(data))
    gzip_member = find_gzip_member(data, footer)
    return classify_login(gzip_member.decompressed)


def classify_login(decompressed: bytes) -> LoginInfo:
    stock_matches: list[tuple[int, bytes]] = []
    for script in KNOWN_STOCK_LOGIN_SCRIPTS:
        start = 0
        while True:
            offset = decompressed.find(script, start)
            if offset < 0:
                break
            stock_matches.append((offset, script))
            start = offset + 1

    patched_matches: list[int] = []
    start = 0
    while True:
        offset = decompressed.find(PATCHED_LOGIN_SCRIPT, start)
        if offset < 0:
            break
        patched_matches.append(offset)
        start = offset + 1

    if len(stock_matches) == 1 and not patched_matches:
        offset, script = stock_matches[0]
        return LoginInfo("stock", offset, len(script), sha256_hex(script), 1)
    if len(patched_matches) == 1 and not stock_matches:
        return LoginInfo("already_patched", patched_matches[0], len(PATCHED_LOGIN_SCRIPT), sha256_hex(PATCHED_LOGIN_SCRIPT), 1)
    return LoginInfo("unknown", None, None, None, len(stock_matches) + len(patched_matches))


def _identity_matches(decompressed: bytes, *, os_release: str) -> tuple[bool, str]:
    release = os_release.strip()
    if not release:
        return False, "missing running OS release"
    release_bytes = release.encode("ascii", errors="ignore")
    if release_bytes and release_bytes in decompressed:
        return True, f"running OS release {release!r} found in decompressed image"
    return False, f"running OS release {release!r} not found in decompressed image"


def _load_zopfli_gzip() -> object:
    return importlib.import_module("zopfli.gzip")


def require_zopfli_gzip_available() -> None:
    try:
        require_python_module("zopfli.gzip", ZOPFLI_BOOTSTRAP_MESSAGE)
    except RuntimeError as exc:
        raise FlashAnalysisError(str(exc)) from exc


def _compress_with_zopfli_gzip(data: bytes) -> CompressionResult:
    try:
        zopfli_gzip = _load_zopfli_gzip()
    except Exception as exc:
        raise FlashAnalysisError(ZOPFLI_BOOTSTRAP_MESSAGE) from exc
    compressed = zopfli_gzip.compress(
        data,
        numiterations=5,
        blocksplitting=1,
        blocksplittinglast=0,
        blocksplittingmax=15,
    )
    return CompressionResult("zopfli-gzip", compressed)


def _compress_patched_image_with_zopfli(data: bytes, *, max_length: int) -> CompressionResult:
    compression = _compress_with_zopfli_gzip(data)
    if compression.length > max_length:
        raise FlashAnalysisError(
            f"patched gzip member is too large: {compression.method}={compression.length}; limit={max_length}"
        )
    return compression


def build_patch(data: bytes, footer: FooterInfo, gzip_member: GzipMemberInfo, login: LoginInfo) -> PatchBuildInfo | None:
    if not login.patchable:
        return None
    assert login.offset is not None
    assert login.length is not None
    if len(PATCHED_LOGIN_SCRIPT) > login.length:
        raise FlashAnalysisError("patched LOGIN script does not fit in the original script region")

    patched_decompressed = bytearray(gzip_member.decompressed)
    replacement = PATCHED_LOGIN_SCRIPT + (b"\x00" * (login.length - len(PATCHED_LOGIN_SCRIPT)))
    patched_decompressed[login.offset : login.offset + login.length] = replacement

    changed_indexes = [
        idx
        for idx, (old, new) in enumerate(zip(gzip_member.decompressed, patched_decompressed))
        if old != new
    ]
    if not changed_indexes:
        raise FlashAnalysisError("patch did not change the decompressed image")
    changed_start = changed_indexes[0]
    changed_end = changed_indexes[-1] + 1
    if changed_start < login.offset or changed_end > login.offset + login.length:
        raise FlashAnalysisError("decompressed patch changed bytes outside the LOGIN script region")

    compression = _compress_patched_image_with_zopfli(bytes(patched_decompressed), max_length=gzip_member.consumed_length)

    rebuilt = bytearray(data)
    patched_gzip = compression.data
    padded_gzip = patched_gzip + (b"\x00" * (gzip_member.consumed_length - len(patched_gzip)))
    rebuilt[gzip_member.offset : gzip_member.end_offset] = padded_gzip
    checksum = zlib.adler32(bytes(rebuilt[: footer.end_offset])) & 0xFFFFFFFF
    rebuilt[footer.offset : footer.offset + 4] = struct.pack(">I", checksum)

    round_trip = _decompress_gzip_member(bytes(rebuilt), gzip_member.offset)
    if round_trip is None or round_trip.decompressed != bytes(patched_decompressed):
        raise FlashAnalysisError("patched gzip member did not round-trip to the expected decompressed image")

    return PatchBuildInfo(
        target_bank_sha256=sha256_hex(bytes(rebuilt)),
        patched_image_sha256=sha256_hex(bytes(patched_decompressed)),
        patched_gzip_length=len(patched_gzip),
        compression_method=compression.method,
        changed_range_start=changed_start,
        changed_range_end=changed_end,
        footer_checksum=checksum,
        target_bank=bytes(rebuilt),
    )


def _with_patch_candidate(bank: BankAnalysis) -> BankAnalysis:
    try:
        patch = build_patch(bank.data, bank.footer, bank.gzip_member, bank.login)
    except FlashAnalysisError as exc:
        return replace(bank, patch=None, patch_error=str(exc))
    return replace(bank, patch=patch, patch_error=None)


def analyze_bank(
    *,
    name: str,
    device: str,
    data: bytes,
    acp_checksum: int | None,
    os_release: str,
    build_patch_candidate: bool = True,
) -> BankAnalysis:
    footer = find_footer(data)
    footer_valid = (zlib.adler32(data[: footer.end_offset]) & 0xFFFFFFFF) == footer.checksum
    acp_matches = None if acp_checksum is None else acp_checksum == footer.checksum
    gzip_member = find_gzip_member(data, footer)
    login = classify_login(gzip_member.decompressed)
    identity_match, identity_detail = _identity_matches(gzip_member.decompressed, os_release=os_release)
    analysis = BankAnalysis(
        name=name,
        device=device,
        data=data,
        sha256=sha256_hex(data),
        size=len(data),
        footer=footer,
        footer_valid=footer_valid,
        acp_checksum=acp_checksum,
        acp_checksum_matches=acp_matches,
        gzip_member=gzip_member,
        decompressed_sha256=sha256_hex(gzip_member.decompressed),
        login=login,
        kernel_identity_match=identity_match,
        kernel_identity_detail=identity_detail,
        patch=None,
        patch_error=None,
    )
    if build_patch_candidate:
        return _with_patch_candidate(analysis)
    return analysis


def analyze_flash_banks(
    *,
    primary_data: bytes,
    secondary_data: bytes,
    cks1: int | None,
    cks2: int | None,
    os_release: str,
    build_patch_candidate: bool = True,
) -> FlashAnalysis:
    primary = analyze_bank(
        name="primary",
        device="/dev/rflash0.raw",
        data=primary_data,
        acp_checksum=cks1,
        os_release=os_release,
        build_patch_candidate=False,
    )
    secondary = analyze_bank(
        name="secondary",
        device="/dev/rflash1.raw",
        data=secondary_data,
        acp_checksum=cks2,
        os_release=os_release,
        build_patch_candidate=False,
    )
    active_candidates = [bank.name for bank in (primary, secondary) if bank.valid_for_active_selection]
    active_bank = active_candidates[0] if len(active_candidates) == 1 else None
    if build_patch_candidate and active_bank == primary.name:
        primary = _with_patch_candidate(primary)
    elif build_patch_candidate and active_bank == secondary.name:
        secondary = _with_patch_candidate(secondary)
    return FlashAnalysis(primary=primary, secondary=secondary, active_bank=active_bank)


def bank_to_jsonable(
    bank: BankAnalysis,
    *,
    include_data: bool = False,
    would_write: bool = False,
    write_decision: str,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "name": bank.name,
        "device": bank.device,
        "size": bank.size,
        "sha256": bank.sha256,
        "would_write": would_write,
        "write_decision": write_decision,
        "footer": {
            "offset": bank.footer.offset,
            "checksum": f"0x{bank.footer.checksum:08x}",
            "end_offset": bank.footer.end_offset,
            "valid": bank.footer_valid,
        },
        "acp_checksum": None if bank.acp_checksum is None else f"0x{bank.acp_checksum:08x}",
        "acp_checksum_matches": bank.acp_checksum_matches,
        "gzip": {
            "offset": bank.gzip_member.offset,
            "consumed_length": bank.gzip_member.consumed_length,
            "decompressed_size": len(bank.gzip_member.decompressed),
            "decompressed_sha256": bank.decompressed_sha256,
        },
        "login": {
            "classification": bank.login.classification,
            "offset": bank.login.offset,
            "length": bank.login.length,
            "sha256": bank.login.sha256,
            "match_count": bank.login.match_count,
        },
        "kernel_identity_match": bank.kernel_identity_match,
        "kernel_identity_detail": bank.kernel_identity_detail,
        "patch": None if bank.patch is None else {
            "target_bank_sha256": bank.patch.target_bank_sha256,
            "patched_image_sha256": bank.patch.patched_image_sha256,
            "patched_gzip_length": bank.patch.patched_gzip_length,
            "compression_method": bank.patch.compression_method,
            "changed_range_start": bank.patch.changed_range_start,
            "changed_range_end": bank.patch.changed_range_end,
            "footer_checksum": f"0x{bank.patch.footer_checksum:08x}",
        },
        "patch_error": bank.patch_error,
    }
    if include_data:
        payload["data"] = bank.data
    return payload


def analysis_to_jsonable(analysis: FlashAnalysis) -> dict[str, object]:
    return {
        "active_bank": analysis.active_bank,
        "write_policy": "active_bank_only",
        "banks": [
            bank_to_jsonable(
                analysis.primary,
                would_write=analysis.active_bank == analysis.primary.name and analysis.primary.patch is not None,
                write_decision=write_decision_for_bank(analysis, analysis.primary),
            ),
            bank_to_jsonable(
                analysis.secondary,
                would_write=analysis.active_bank == analysis.secondary.name and analysis.secondary.patch is not None,
                write_decision=write_decision_for_bank(analysis, analysis.secondary),
            ),
        ],
    }
