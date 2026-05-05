from __future__ import annotations

import ipaddress

from timecapsulesmb.checks.bonjour import BonjourServiceTarget
from timecapsulesmb.core.config import AppConfig, extract_host


def doctor_smb_servers(config: AppConfig, bonjour_target: BonjourServiceTarget | None) -> list[str]:
    ordered: list[str] = []

    def add(value: str | None) -> None:
        if value and value not in ordered:
            ordered.append(value)

    configured_host_label = config.require("TC_MDNS_HOST_LABEL").strip()
    if configured_host_label and "." not in configured_host_label:
        try:
            ipaddress.ip_address(configured_host_label)
        except ValueError:
            configured_host_label = f"{configured_host_label}.local"
    add(configured_host_label)
    add(bonjour_target.hostname if bonjour_target is not None else None)
    add(extract_host(config.require("TC_HOST")))
    return ordered
