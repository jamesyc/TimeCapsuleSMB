from __future__ import annotations

import ipaddress
from typing import Optional

from timecapsulesmb.checks.bonjour import BonjourServiceTarget
from timecapsulesmb.core.config import AppConfig, extract_host


def configured_smb_server(host_label: str) -> str:
    value = host_label.strip()
    if not value:
        return value
    try:
        ipaddress.ip_address(value)
        return value
    except ValueError:
        pass
    if "." in value:
        return value
    return f"{value}.local"


def doctor_smb_servers(config: AppConfig, bonjour_target: BonjourServiceTarget | None) -> list[str]:
    ordered: list[str] = []

    def add(value: Optional[str]) -> None:
        if value and value not in ordered:
            ordered.append(value)

    add(configured_smb_server(config.require("TC_MDNS_HOST_LABEL")))
    add(bonjour_target.hostname if bonjour_target is not None else None)
    add(extract_host(config.require("TC_HOST")))
    return ordered
