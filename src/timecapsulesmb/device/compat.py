from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol

from timecapsulesmb.core.config import AIRPORT_SYAP_TO_MODEL


NETBSD4LE_SYAP_CANDIDATES = ("113", "116")
NETBSD4BE_SYAP_CANDIDATES = ("106", "109")
NETBSD6_SYAP_CANDIDATES = ("119",)
PAYLOAD_FAMILY_NETBSD6 = "netbsd6_samba4"
PAYLOAD_FAMILY_NETBSD4LE = "netbsd4le_samba4"
PAYLOAD_FAMILY_NETBSD4BE = "netbsd4be_samba4"
NETBSD4_PAYLOAD_FAMILIES = frozenset((PAYLOAD_FAMILY_NETBSD4LE, PAYLOAD_FAMILY_NETBSD4BE))
SUPPORTED_PAYLOAD_FAMILIES = frozenset((PAYLOAD_FAMILY_NETBSD6, PAYLOAD_FAMILY_NETBSD4LE, PAYLOAD_FAMILY_NETBSD4BE))


class ProbeFacts(Protocol):
    ssh_authenticated: bool
    error: str | None
    os_name: str
    os_release: str
    arch: str
    elf_endianness: str


def _models_for_syaps(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(AIRPORT_SYAP_TO_MODEL[value] for value in values if value in AIRPORT_SYAP_TO_MODEL)


def is_netbsd4_payload_family(payload_family: str | None) -> bool:
    return payload_family in NETBSD4_PAYLOAD_FAMILIES


def is_netbsd6_payload_family(payload_family: str | None) -> bool:
    return payload_family == PAYLOAD_FAMILY_NETBSD6


def device_family_from_payload_family(payload_family: str | None) -> str | None:
    if is_netbsd4_payload_family(payload_family):
        return "netbsd4"
    if is_netbsd6_payload_family(payload_family):
        return "netbsd6"
    return None


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
            "This repo currently supports NetBSD 4 and NetBSD 6 Time Capsules."
        )
    if compat.reason_code == "unsupported_netbsd6_endianness":
        return (
            f"Detected NetBSD {compat.os_release} ({compat.arch}) with {compat.elf_endianness}-endian binaries, "
            "which is not supported by the current Samba payload."
        )
    if compat.reason_code == "supported_netbsd6":
        return f"Detected supported device: NetBSD {compat.os_release} ({compat.arch})..."
    if compat.reason_code == "unsupported_netbsd4_endianness":
        return (
            f"Detected NetBSD {compat.os_release} ({compat.arch}) with {compat.elf_endianness}-endian binaries, "
            "which is not supported by the current checked-in Samba payload."
        )
    if compat.reason_code == "supported_netbsd4":
        return f"Detected supported older device: NetBSD {compat.os_release} ({compat.arch})."
    if compat.reason_code == "unsupported_netbsd_release":
        return (
            f"This Time Capsule is running NetBSD {compat.os_release}, which is not supported by the current Samba payload. "
            "Only NetBSD 4 and NetBSD 6 devices are supported right now."
        )
    return compat.reason_detail or "Failed to classify remote device compatibility."


def classify_device_compatibility(os_name: str, os_release: str, arch: str, elf_endianness: str = "unknown") -> DeviceCompatibility:
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
        return DeviceCompatibility(
            os_name=normalized_name,
            os_release=normalized_release,
            arch=normalized_arch,
            elf_endianness=normalized_endianness,
            payload_family=PAYLOAD_FAMILY_NETBSD6,
            device_generation="gen5",
            syap_candidates=NETBSD6_SYAP_CANDIDATES,
            model_candidates=_models_for_syaps(NETBSD6_SYAP_CANDIDATES),
            supported=True,
            reason_code="supported_netbsd6",
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
        return DeviceCompatibility(
            os_name=normalized_name,
            os_release=normalized_release,
            arch=normalized_arch,
            elf_endianness=normalized_endianness,
            payload_family=payload_family,
            device_generation="gen1-4",
            syap_candidates=syap_candidates,
            model_candidates=_models_for_syaps(syap_candidates),
            supported=True,
            reason_code="supported_netbsd4",
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
    return classify_device_compatibility(result.os_name, result.os_release, result.arch, result.elf_endianness)
