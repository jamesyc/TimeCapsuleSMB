from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Optional, Protocol

from timecapsulesmb.core.config import AIRPORT_DEVICE_IDENTITIES, AIRPORT_SYAP_TO_MODEL, VALID_AIRPORT_SYAP_CODES


def _syaps_for_group(group: str) -> tuple[str, ...]:
    return tuple(identity.syap for identity in AIRPORT_DEVICE_IDENTITIES if identity.compatibility_group == group)


NETBSD4LE_SYAP_CANDIDATES = _syaps_for_group("netbsd4le")
NETBSD4BE_SYAP_CANDIDATES = _syaps_for_group("netbsd4be")
NETBSD6_SYAP_CANDIDATES = _syaps_for_group("netbsd6")
PAYLOAD_FAMILY_NETBSD6 = "netbsd6_samba4"
PAYLOAD_FAMILY_NETBSD4LE = "netbsd4le_samba4"
PAYLOAD_FAMILY_NETBSD4BE = "netbsd4be_samba4"
NETBSD4_PAYLOAD_FAMILIES = frozenset((PAYLOAD_FAMILY_NETBSD4LE, PAYLOAD_FAMILY_NETBSD4BE))
# `uname -m` is evbarm on every Time Capsule and AirPort Extreme in telemetry,
# both NetBSD 4 byte orders and NetBSD 6. earmv4 appeared once in telemetry and in
# test fixtures; it is ARM, so accepting it is harmless. Our payloads are ARM-only.
# The AirPort Express runs NetBSD 4 on a MIPS ar7240 and would otherwise pass the
# release and endianness checks as NetBSD 4 big-endian.
SUPPORTED_ARCHES = frozenset({"evbarm", "earmv4"})


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


def airport_syap_supported(syap: str | None) -> bool | None:
    """Whether a Bonjour-advertised syAP names a supported model.

    None means the value cannot tell us: missing or not a decimal model code.
    Each syAP belongs to one model, so a well-formed code outside our table is
    another AirPort, such as an AirPort Express.
    """
    value = (syap or "").strip()
    if not value.isascii() or not value.isdigit():
        return None
    return str(int(value)) in VALID_AIRPORT_SYAP_CODES


def unsupported_syaps(syaps: Iterable[str | None]) -> list[str]:
    """Distinct advertised syAPs of unsupported models, for discovery telemetry.

    The app and CLI pickers stop on these before configure runs, so discovery
    is the only place that sees which codes unsupported AirPorts advertise.
    """
    return sorted(
        {str(int(value.strip())) for value in syaps if value is not None and airport_syap_supported(value) is False},
        key=int,
    )


def unsupported_syap_message(syap: str) -> str:
    return (
        f"The selected AirPort reports model code syAP {syap.strip()}, which is not an AirPort Time Capsule "
        "or AirPort Extreme. TimeCapsuleSMB supports only those models; AirPort Express is not supported."
    )


def render_compatibility_message(compat: DeviceCompatibility) -> str:
    if compat.reason_code == "unsupported_os":
        return (
            f"Unsupported device OS: {compat.os_name or 'unknown'} {compat.os_release or 'unknown'}. "
            "This repo currently supports NetBSD 4 and NetBSD 6 AirPort storage devices."
        )
    if compat.reason_code == "unsupported_arch":
        return (
            f"Detected NetBSD {compat.os_release} on a {compat.arch or 'unknown'} processor. "
            "TimeCapsuleSMB runs only on AirPort Time Capsule and AirPort Extreme base stations, "
            "which use ARM processors. This is likely an AirPort Express, which is not supported."
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

    if normalized_arch not in SUPPORTED_ARCHES:
        return DeviceCompatibility(
            os_name=normalized_name,
            os_release=normalized_release,
            arch=normalized_arch,
            elf_endianness=normalized_endianness,
            payload_family=None,
            device_generation="unknown",
            supported=False,
            reason_code="unsupported_arch",
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
