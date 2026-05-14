from __future__ import annotations

from timecapsulesmb.checks.bonjour import BonjourServiceTarget
from timecapsulesmb.core.config import AppConfig
from timecapsulesmb.core.net import extract_host
from timecapsulesmb.device.probe import RuntimeNamingIdentityProbeResult


def doctor_smb_servers(
    config: AppConfig,
    bonjour_target: BonjourServiceTarget | None,
    runtime_naming_identity: RuntimeNamingIdentityProbeResult | None = None,
) -> list[str]:
    ordered: list[str] = []

    def add(value: str | None) -> None:
        if value and value not in ordered:
            ordered.append(value)

    if runtime_naming_identity is not None and runtime_naming_identity.mdns_host_label:
        add(f"{runtime_naming_identity.mdns_host_label}.local")
    add(bonjour_target.hostname if bonjour_target is not None else None)
    add(extract_host(config.require("TC_HOST")))
    return ordered
