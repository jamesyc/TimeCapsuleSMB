from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Optional

from timecapsulesmb.core.config import (
    CONFIG_VALIDATORS,
    DEFAULTS,
    airport_identity_from_values,
    extract_host,
)
from timecapsulesmb.device.probe import (
    RemoteInterfaceCandidatesProbeResult,
    preferred_interface_name,
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


@dataclass(frozen=True)
class DerivedNameDefaults:
    netbios_name: str
    mdns_instance_name: str
    mdns_host_label: str


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


def apply_device_storage_defaults(values: dict[str, str]) -> None:
    identity = airport_identity_from_values(values)
    if identity is not None and identity.family == "airport_extreme":
        values["TC_SHARE_USE_DISK_ROOT"] = "true"


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


def is_link_local_ipv4(value: str) -> bool:
    return value.startswith("169.254.")


def interface_candidate_for_ip(
    result: RemoteInterfaceCandidatesProbeResult,
    target_ips: tuple[str, ...],
) -> InterfaceIpMatch | None:
    # Prefer exact non-link-local matches first. Link-local can still be used,
    # but only when it is the only exact address we can match to an interface.
    ordered_target_ips = tuple(ip for ip in target_ips if not is_link_local_ipv4(ip)) + tuple(
        ip for ip in target_ips if is_link_local_ipv4(ip)
    )
    for target_ip in ordered_target_ips:
        for candidate in result.candidates:
            if candidate.loopback:
                continue
            if target_ip in candidate.ipv4_addrs:
                return InterfaceIpMatch(iface=candidate.name, ip=target_ip)
    return None


def best_non_link_local_ipv4(
    values: dict[str, str],
    discovered_record: BonjourResolvedService | None,
    probed_interfaces: RemoteInterfaceCandidatesProbeResult | None,
) -> str | None:
    host_ip = ipv4_literal(extract_host(values.get("TC_HOST", "")))
    if host_ip and not is_link_local_ipv4(host_ip):
        return host_ip

    if discovered_record is not None:
        for value in discovered_record.ipv4:
            ip_value = ipv4_literal(value)
            if ip_value and not is_link_local_ipv4(ip_value):
                return ip_value

    if probed_interfaces is not None and probed_interfaces.candidates:
        target_ips = interface_target_ips(values, discovered_record)
        preferred_iface = preferred_interface_name(probed_interfaces.candidates, target_ips=target_ips)
        if preferred_iface is None:
            preferred_iface = probed_interfaces.preferred_iface
        if preferred_iface:
            for candidate in probed_interfaces.candidates:
                if candidate.name != preferred_iface or candidate.loopback:
                    continue
                for ip_value in candidate.ipv4_addrs:
                    if not is_link_local_ipv4(ip_value):
                        return ip_value
    return None


def derived_name_defaults(
    values: dict[str, str],
    discovered_record: BonjourResolvedService | None,
    probed_interfaces: RemoteInterfaceCandidatesProbeResult | None,
) -> DerivedNameDefaults | None:
    source_ip = best_non_link_local_ipv4(values, discovered_record, probed_interfaces)
    if source_ip is None:
        return None
    last_octet = source_ip.rsplit(".", 1)[-1]
    suffix = f"{int(last_octet):03d}"
    return DerivedNameDefaults(
        netbios_name=f"TimeCapsule{suffix}",
        mdns_instance_name=f"Time Capsule Samba {suffix}",
        mdns_host_label=f"timecapsulesamba{suffix}",
    )


def derived_prompt_defaults(name_defaults: DerivedNameDefaults | None) -> dict[str, str]:
    return {
        "TC_NETBIOS_NAME": name_defaults.netbios_name if name_defaults is not None else DEFAULTS["TC_NETBIOS_NAME"],
        "TC_MDNS_INSTANCE_NAME": (
            name_defaults.mdns_instance_name if name_defaults is not None else DEFAULTS["TC_MDNS_INSTANCE_NAME"]
        ),
        "TC_MDNS_HOST_LABEL": (
            name_defaults.mdns_host_label if name_defaults is not None else DEFAULTS["TC_MDNS_HOST_LABEL"]
        ),
    }
