from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Optional

from timecapsulesmb.core.config import (
    CONFIG_VALIDATORS,
    extract_host,
)
from timecapsulesmb.device.probe import (
    RemoteInterfaceCandidatesProbeResult,
    runtime_interface_candidates,
    runtime_usable_ipv4s,
)
from timecapsulesmb.discovery.bonjour import BonjourResolvedService


@dataclass(frozen=True)
class ConfigureValueChoice:
    value: str
    source: str


@dataclass(frozen=True)
class InterfaceIpMatch:
    iface: str
    ip: str


def validated_value_or_empty(key: str, value: str, label: str) -> str:
    validator = CONFIG_VALIDATORS.get(key)
    if not value or validator is None:
        return value
    if validator(value, label):
        return ""
    return value


def valid_existing_config_value(existing: dict[str, str], key: str, label: str) -> str:
    return validated_value_or_empty(key, existing.get(key, ""), label)


def saved_value_choice(existing: dict[str, str], key: str, label: str) -> Optional[ConfigureValueChoice]:
    value = valid_existing_config_value(existing, key, label)
    if not value:
        return None
    return ConfigureValueChoice(value=value, source="saved")


def saved_syap_value_for_candidates(
    saved_syap_choice: ConfigureValueChoice | None,
    candidate_syaps: tuple[str, ...],
) -> str | None:
    if saved_syap_choice is None:
        return None
    if candidate_syaps and saved_syap_choice.value not in candidate_syaps:
        return None
    return saved_syap_choice.value


def ipv4_literal(value: str) -> str | None:
    value = value.strip()
    try:
        parsed = ipaddress.ip_address(value)
    except ValueError:
        parts = value.split(".")
        if len(parts) != 4 or any(not part.isdigit() for part in parts):
            return None
        octets: list[str] = []
        for part in parts:
            octet = int(part, 10)
            if octet < 0 or octet > 255:
                return None
            octets.append(str(octet))
        return ".".join(octets)
    if parsed.version != 4:
        return None
    return str(parsed)


def interface_target_ips(values: dict[str, str], discovered_record: BonjourResolvedService | None) -> tuple[str, ...]:
    ordered: list[str] = []
    host_ip = ipv4_literal(extract_host(values.get("TC_HOST", "")))
    if host_ip:
        ordered.append(host_ip)
    if discovered_record is not None:
        for value in discovered_record.ipv4:
            ip_value = ipv4_literal(value)
            if ip_value and ip_value not in ordered:
                ordered.append(ip_value)
    return tuple(ordered)


def interface_candidate_for_ip(
    result: RemoteInterfaceCandidatesProbeResult,
    target_ips: tuple[str, ...],
) -> InterfaceIpMatch | None:
    runtime_candidates = runtime_interface_candidates(result.candidates)
    runtime_target_ips = tuple(ip for ip in target_ips if runtime_usable_ipv4s((ip,)))
    for target_ip in runtime_target_ips:
        for candidate in runtime_candidates:
            if target_ip in runtime_usable_ipv4s(candidate.ipv4_addrs):
                return InterfaceIpMatch(iface=candidate.name, ip=target_ip)
    return None
