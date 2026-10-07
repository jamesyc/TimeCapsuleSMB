from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from timecapsulesmb.core.net import is_link_local_ipv6, normalize_endpoint_host
from timecapsulesmb.discovery.bonjour import (
    BonjourResolvedService,
    discovered_record_has_only_link_local_ips,
    discovered_record_root_host,
)
from timecapsulesmb.integrations.acp import ACP_PORT
from timecapsulesmb.services import configure as configure_service
from timecapsulesmb.transport.local import tcp_connect_error


ConfigureTargetSource = Literal["explicit_host", "selected_record", "existing_config"]


@dataclass(frozen=True)
class ConfigureTargetResolution:
    host: str
    source: ConfigureTargetSource
    selected_record: BonjourResolvedService | None = None
    discovered_airport_syap: str | None = None

    @property
    def selected_record_airport_syap(self) -> str | None:
        # A host typed over a selected record may be another device, so the
        # record's syAP only describes the target when the host came from it.
        return self.discovered_airport_syap if self.source == "selected_record" else None

    @property
    def target_record(self) -> BonjourResolvedService | None:
        # Likewise, the record only describes the target when the host came from it.
        return self.selected_record if self.source == "selected_record" else None


def selected_record_properties(selected: Mapping[str, object] | None) -> dict[str, str]:
    if selected is None:
        return {}
    properties = selected.get("properties")
    if not isinstance(properties, Mapping):
        return {}
    return {str(key): str(value) for key, value in properties.items()}


def bonjour_record_from_selected_record(selected: Mapping[str, object] | None) -> BonjourResolvedService | None:
    if selected is None:
        return None
    return BonjourResolvedService(
        name=str(selected.get("name") or ""),
        hostname=str(selected.get("hostname") or ""),
        service_type=str(selected.get("service_type") or ""),
        port=int(selected.get("port") or 0),
        ipv4=tuple(str(ip) for ip in selected.get("ipv4", ()) if ip),
        ipv6=tuple(str(ip) for ip in selected.get("ipv6", ()) if ip),
        properties=selected_record_properties(selected),
        fullname=str(selected.get("fullname") or ""),
        interface_index=selected.get("interface_index") if type(selected.get("interface_index")) is int else None,
    )


def reachable_record_host(record: BonjourResolvedService) -> str | None:
    """The record's address that answers ACP: its LAN address, else link-local IPv6.

    A Mac on another IPv4 subnet of the same network reaches the AirPort only
    over link-local IPv6, as AirPort Utility does. 169.254 is never used: where
    it would answer, the fe80 address answers too.
    """
    preferred = record.preferred_ip()
    candidates = ([preferred] if preferred else []) + [ip for ip in record.ipv6 if is_link_local_ipv6(ip) and "%" in ip]
    for address in candidates:
        if tcp_connect_error(address, ACP_PORT) is None:
            return f"root@{normalize_endpoint_host(address)}"
    return None


def resolve_configure_target(
    *,
    explicit_host: str,
    selected_record: Mapping[str, object] | BonjourResolvedService | None,
    existing: Mapping[str, str],
    ssh_opts: str,
) -> ConfigureTargetResolution:
    record = (
        selected_record
        if isinstance(selected_record, BonjourResolvedService)
        else bonjour_record_from_selected_record(selected_record)
    )
    discovered_airport_syap = None if record is None else (record.properties.get("syAP") or None)

    source: ConfigureTargetSource
    target = explicit_host.strip()
    if target:
        source = "explicit_host"
    else:
        target = (reachable_record_host(record) or discovered_record_root_host(record)) if record is not None else None
        if target:
            source = "selected_record"
        elif record is not None and discovered_record_has_only_link_local_ips(record):
            # Falling back to the saved TC_HOST here would either report a blank
            # target or silently configure whichever device was saved last.
            raise ValueError(
                "Selected device only advertised link-local addresses, and none answered from this computer. "
                "Connect it to your network so it gets a LAN IP, then add it by that IP."
            )
        else:
            target = existing.get("TC_HOST", "")
            source = "existing_config"

    return ConfigureTargetResolution(
        host=configure_service.configure_ssh_target(target, ssh_opts, validate_config_value=True),
        source=source,
        selected_record=record,
        discovered_airport_syap=discovered_airport_syap,
    )
