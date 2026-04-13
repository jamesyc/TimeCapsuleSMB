from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Optional

from timecapsulesmb.transport.ssh import run_ssh


@dataclass(frozen=True)
class DeviceCompatibility:
    os_name: str
    os_release: str
    arch: str
    payload_family: Optional[str]
    device_generation: str
    mdns_device_model_hint: str
    supported: bool
    message: str


def classify_device_compatibility(os_name: str, os_release: str, arch: str) -> DeviceCompatibility:
    normalized_name = os_name.strip()
    normalized_release = os_release.strip()
    normalized_arch = arch.strip()

    if normalized_name != "NetBSD":
        return DeviceCompatibility(
            os_name=normalized_name,
            os_release=normalized_release,
            arch=normalized_arch,
            payload_family=None,
            device_generation="unknown",
            mdns_device_model_hint="TimeCapsule",
            supported=False,
            message=f"Unsupported device OS: {normalized_name or 'unknown'} {normalized_release or 'unknown'}. This repo currently supports NetBSD 4 and NetBSD 6 Time Capsules.",
        )

    major = normalized_release.split(".", 1)[0]
    if major == "6":
        return DeviceCompatibility(
            os_name=normalized_name,
            os_release=normalized_release,
            arch=normalized_arch,
            payload_family="netbsd6_samba4",
            device_generation="gen5",
            mdns_device_model_hint="TimeCapsule8,119",
            supported=True,
            message=f"Detected supported device: NetBSD {normalized_release} ({normalized_arch})...",
        )
    if major == "4":
        return DeviceCompatibility(
            os_name=normalized_name,
            os_release=normalized_release,
            arch=normalized_arch,
            payload_family="netbsd4_samba4",
            device_generation="gen1-4",
            mdns_device_model_hint="TimeCapsule6,106",
            supported=True,
            message=f"Detected supported older device: NetBSD {normalized_release} ({normalized_arch}).",
        )

    return DeviceCompatibility(
        os_name=normalized_name,
        os_release=normalized_release,
        arch=normalized_arch,
        payload_family=None,
        device_generation="unknown",
        mdns_device_model_hint="TimeCapsule",
        supported=False,
        message=f"This Time Capsule is running NetBSD {normalized_release}, which is not supported by the current checked-in Samba payload. Only NetBSD 4 and NetBSD 6 devices are supported right now.",
    )


def probe_device_compatibility(host: str, password: str, ssh_opts: str) -> DeviceCompatibility:
    script = "printf '%s\\n%s\\n%s\\n' \"$(uname -s)\" \"$(uname -r)\" \"$(uname -m)\""
    proc = run_ssh(host, password, ssh_opts, f"/bin/sh -c {shlex.quote(script)}")
    lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    if len(lines) < 3:
        raise SystemExit("Failed to determine remote device OS compatibility.")
    return classify_device_compatibility(lines[0], lines[1], lines[2])
def infer_mdns_device_model_hint(host: str, password: str, ssh_opts: str) -> str:
    return probe_device_compatibility(host, password, ssh_opts).mdns_device_model_hint
