from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol

from timecapsulesmb.core.config import AIRPORT_SYAP_TO_MODEL


NETBSD4LE_SYAP_CANDIDATES = ("113", "116")
NETBSD4BE_SYAP_CANDIDATES = ("106", "109")
NETBSD6_SYAP_CANDIDATES = ("119",)


class ProbeFacts(Protocol):
    ssh_authenticated: bool
    error: str | None
    os_name: str
    os_release: str
    arch: str
    elf_endianness: str


def _models_for_syaps(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(AIRPORT_SYAP_TO_MODEL[value] for value in values if value in AIRPORT_SYAP_TO_MODEL)


@dataclass(frozen=True)
class DeviceCompatibility:
    os_name: str
    os_release: str
    arch: str
    elf_endianness: str
    payload_family: Optional[str]
    device_generation: str
    supported: bool
    message: str
    syap_candidates: tuple[str, ...] = ()
    model_candidates: tuple[str, ...] = ()

    @property
    def exact_syap(self) -> str | None:
        return self.syap_candidates[0] if len(self.syap_candidates) == 1 else None

    @property
    def exact_model(self) -> str | None:
        return self.model_candidates[0] if len(self.model_candidates) == 1 else None


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
            message=f"Unsupported device OS: {normalized_name or 'unknown'} {normalized_release or 'unknown'}. This repo currently supports NetBSD 4 and NetBSD 6 Time Capsules.",
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
                message=f"Detected NetBSD {normalized_release} ({normalized_arch}) with {normalized_endianness}-endian binaries, which is not supported by the current Samba payload.",
            )
        return DeviceCompatibility(
            os_name=normalized_name,
            os_release=normalized_release,
            arch=normalized_arch,
            elf_endianness=normalized_endianness,
            payload_family="netbsd6_samba4",
            device_generation="gen5",
            syap_candidates=NETBSD6_SYAP_CANDIDATES,
            model_candidates=_models_for_syaps(NETBSD6_SYAP_CANDIDATES),
            supported=True,
            message=f"Detected supported device: NetBSD {normalized_release} ({normalized_arch})...",
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
                message=f"Detected NetBSD {normalized_release} ({normalized_arch}) with {normalized_endianness}-endian binaries, which is not supported by the current checked-in Samba payload.",
            )
        payload_family = "netbsd4be_samba4" if normalized_endianness == "big" else "netbsd4le_samba4"
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
            message=f"Detected supported older device: NetBSD {normalized_release} ({normalized_arch}).",
        )

    return DeviceCompatibility(
        os_name=normalized_name,
        os_release=normalized_release,
        arch=normalized_arch,
        elf_endianness=normalized_endianness,
        payload_family=None,
        device_generation="unknown",
        supported=False,
        message=f"This Time Capsule is running NetBSD {normalized_release}, which is not supported by the current Samba payload. Only NetBSD 4 and NetBSD 6 devices are supported right now.",
    )


def compatibility_from_probe_result(result: ProbeFacts) -> DeviceCompatibility | None:
    if not result.ssh_authenticated:
        return None
    return classify_device_compatibility(result.os_name, result.os_release, result.arch, result.elf_endianness)
