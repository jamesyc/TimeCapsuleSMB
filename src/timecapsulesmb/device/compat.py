from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol

from timecapsulesmb.core.config import AIRPORT_DEVICE_IDENTITIES, AIRPORT_SYAP_TO_MODEL


def _syaps_for_group(group: str) -> tuple[str, ...]:
    return tuple(identity.syap for identity in AIRPORT_DEVICE_IDENTITIES if identity.compatibility_group == group)


NETBSD4LE_SYAP_CANDIDATES = _syaps_for_group("netbsd4le")
NETBSD4BE_SYAP_CANDIDATES = _syaps_for_group("netbsd4be")
NETBSD6_SYAP_CANDIDATES = _syaps_for_group("netbsd6")
PAYLOAD_FAMILY_NETBSD6 = "netbsd6_samba4"
PAYLOAD_FAMILY_NETBSD4LE = "netbsd4le_samba4"
PAYLOAD_FAMILY_NETBSD4BE = "netbsd4be_samba4"
NETBSD4_PAYLOAD_FAMILIES = frozenset((PAYLOAD_FAMILY_NETBSD4LE, PAYLOAD_FAMILY_NETBSD4BE))
SUPPORTED_PAYLOAD_FAMILIES = frozenset((PAYLOAD_FAMILY_NETBSD6, PAYLOAD_FAMILY_NETBSD4LE, PAYLOAD_FAMILY_NETBSD4BE))


class ProbeFacts(Protocol):
    @property
    def ssh_authenticated(self) -> bool: ...

    @property
    def error(self) -> str | None: ...

    @property
    def os_name(self) -> str: ...

    @property
    def os_release(self) -> str: ...

    @property
    def arch(self) -> str: ...

    @property
    def elf_endianness(self) -> str: ...

    @property
    def airport_model(self) -> str | None: ...

    @property
    def airport_syap(self) -> str | None: ...


def _models_for_syaps(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(AIRPORT_SYAP_TO_MODEL[value] for value in values if value in AIRPORT_SYAP_TO_MODEL)


def _narrow_candidates_from_airport_identity(
    syap_candidates: tuple[str, ...],
    airport_model: str | None,
    airport_syap: str | None,
) -> tuple[tuple[str, ...], tuple[str, ...], str]:
    detail = ""
    if airport_syap in syap_candidates:
        return (airport_syap,), _models_for_syaps((airport_syap,)), "airport_identity"
    if airport_model:
        for syap, model in AIRPORT_SYAP_TO_MODEL.items():
            if model == airport_model:
                if syap in syap_candidates:
                    return (syap,), (model,), "airport_identity"
                detail = f"AirPort identity model {airport_model} did not match detected device candidates: {', '.join(syap_candidates)}"
                break
    elif airport_syap:
        detail = f"AirPort identity syAP {airport_syap} did not match detected device candidates: {', '.join(syap_candidates)}"
    return syap_candidates, _models_for_syaps(syap_candidates), detail


def is_netbsd4_payload_family(payload_family: str | None) -> bool:
    return payload_family in NETBSD4_PAYLOAD_FAMILIES


def is_netbsd6_payload_family(payload_family: str | None) -> bool:
    return payload_family == PAYLOAD_FAMILY_NETBSD6


def payload_family_description(payload_family: str | None) -> str:
    if payload_family == PAYLOAD_FAMILY_NETBSD4LE:
        return "NetBSD 4 little-endian"
    if payload_family == PAYLOAD_FAMILY_NETBSD4BE:
        return "NetBSD 4 big-endian"
    if payload_family == PAYLOAD_FAMILY_NETBSD6:
        return "NetBSD 6 little-endian"
    return "unknown"


@dataclass(frozen=True)
class DeviceCompatibility:
    os_name: str
    os_release: str
    arch: str
    elf_endianness: str
    payload_family: Optional[str]
    device_generation: str
    supported: bool
    reason_code: str
    reason_detail: str = ""
    syap_candidates: tuple[str, ...] = ()
    model_candidates: tuple[str, ...] = ()

    @property
    def exact_syap(self) -> str | None:
        return self.syap_candidates[0] if len(self.syap_candidates) == 1 else None

    @property
    def exact_model(self) -> str | None:
        return self.model_candidates[0] if len(self.model_candidates) == 1 else None


def require_compatibility(compat: DeviceCompatibility | None, *, fallback_error: str | None = None) -> DeviceCompatibility:
    if compat is None:
        raise SystemExit(fallback_error or "Failed to determine remote device OS compatibility.")
    return compat


def render_compatibility_message(compat: DeviceCompatibility) -> str:
    if compat.reason_code == "unsupported_os":
        return (
            f"Unsupported device OS: {compat.os_name or 'unknown'} {compat.os_release or 'unknown'}. "
            "This repo currently supports NetBSD 4 and NetBSD 6 AirPort storage devices."
        )
    if compat.reason_code == "unsupported_netbsd6_endianness":
        return (
            f"Detected NetBSD {compat.os_release} ({compat.arch}) with {compat.elf_endianness}-endian binaries, "
            "which is not supported by the current Samba payload."
        )
    if compat.reason_code == "supported_netbsd6":
        return f"Detected supported device: NetBSD {compat.os_release} ({compat.arch}, {compat.elf_endianness}-endian)."
    if compat.reason_code == "unsupported_netbsd4_endianness":
        return (
            f"Detected NetBSD {compat.os_release} ({compat.arch}) with {compat.elf_endianness}-endian binaries, "
            "which is not supported by the current Samba payload."
        )
    if compat.reason_code == "supported_netbsd4":
        return f"Detected supported device: NetBSD {compat.os_release} ({compat.arch}, {compat.elf_endianness}-endian)."
    if compat.reason_code == "unsupported_netbsd_release":
        return (
            f"Detected NetBSD {compat.os_release} ({compat.arch}) with {compat.elf_endianness}-endian binaries, "
            "which is not supported by the current Samba payload."
        )
    return compat.reason_detail or "Failed to classify remote device compatibility."


def classify_device_compatibility(
    os_name: str,
    os_release: str,
    arch: str,
    elf_endianness: str = "unknown",
    *,
    airport_model: str | None = None,
    airport_syap: str | None = None,
) -> DeviceCompatibility:
    normalized_name = os_name.strip()
    normalized_release = os_release.strip()
    normalized_arch = arch.strip()
    normalized_endianness = elf_endianness.strip() or "unknown"

    if normalized_name != "NetBSD":
        return DeviceCompatibility(
            os_name=normalized_name,
            os_release=normalized_release,
            arch=normalized_arch,
            elf_endianness=normalized_endianness,
            payload_family=None,
            device_generation="unknown",
            supported=False,
            reason_code="unsupported_os",
        )

    major = normalized_release.split(".", 1)[0]
    if major == "6":
        if normalized_endianness != "little":
            return DeviceCompatibility(
                os_name=normalized_name,
                os_release=normalized_release,
                arch=normalized_arch,
                elf_endianness=normalized_endianness,
                payload_family=None,
                device_generation="unknown",
                supported=False,
                reason_code="unsupported_netbsd6_endianness",
            )
        narrowed_syaps, narrowed_models, reason_detail = _narrow_candidates_from_airport_identity(
            NETBSD6_SYAP_CANDIDATES,
            airport_model,
            airport_syap,
        )
        return DeviceCompatibility(
            os_name=normalized_name,
            os_release=normalized_release,
            arch=normalized_arch,
            elf_endianness=normalized_endianness,
            payload_family=PAYLOAD_FAMILY_NETBSD6,
            device_generation="gen5",
            syap_candidates=narrowed_syaps,
            model_candidates=narrowed_models,
            supported=True,
            reason_code="supported_netbsd6",
            reason_detail=reason_detail,
        )
    if major == "4":
        if normalized_endianness not in {"big", "little"}:
            return DeviceCompatibility(
                os_name=normalized_name,
                os_release=normalized_release,
                arch=normalized_arch,
                elf_endianness=normalized_endianness,
                payload_family=None,
                device_generation="unknown",
                supported=False,
                reason_code="unsupported_netbsd4_endianness",
            )
        payload_family = PAYLOAD_FAMILY_NETBSD4BE if normalized_endianness == "big" else PAYLOAD_FAMILY_NETBSD4LE
        syap_candidates = NETBSD4BE_SYAP_CANDIDATES if normalized_endianness == "big" else NETBSD4LE_SYAP_CANDIDATES
        narrowed_syaps, narrowed_models, reason_detail = _narrow_candidates_from_airport_identity(
            syap_candidates,
            airport_model,
            airport_syap,
        )
        return DeviceCompatibility(
            os_name=normalized_name,
            os_release=normalized_release,
            arch=normalized_arch,
            elf_endianness=normalized_endianness,
            payload_family=payload_family,
            device_generation="gen1-4",
            syap_candidates=narrowed_syaps,
            model_candidates=narrowed_models,
            supported=True,
            reason_code="supported_netbsd4",
            reason_detail=reason_detail,
        )

    return DeviceCompatibility(
        os_name=normalized_name,
        os_release=normalized_release,
        arch=normalized_arch,
        elf_endianness=normalized_endianness,
        payload_family=None,
        device_generation="unknown",
        supported=False,
        reason_code="unsupported_netbsd_release",
    )


def compatibility_from_probe_result(result: ProbeFacts) -> DeviceCompatibility | None:
    if not result.ssh_authenticated:
        return None
    return classify_device_compatibility(
        result.os_name,
        result.os_release,
        result.arch,
        result.elf_endianness,
        airport_model=result.airport_model,
        airport_syap=result.airport_syap,
    )
