from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from timecapsulesmb.discovery.bonjour import BonjourResolvedService, discovered_record_root_host
from timecapsulesmb.services import configure as configure_service


ConfigureTargetSource = Literal["explicit_host", "selected_record", "existing_config"]


@dataclass(frozen=True)
class ConfigureTargetResolution:
    host: str
    source: ConfigureTargetSource
    selected_record: BonjourResolvedService | None = None
    discovered_airport_syap: str | None = None


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
    )


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
        target = discovered_record_root_host(record) if record is not None else None
        if target:
            source = "selected_record"
        else:
            target = existing.get("TC_HOST", "")
            source = "existing_config"

    return ConfigureTargetResolution(
        host=configure_service.configure_ssh_target(target, ssh_opts, validate_config_value=True),
        source=source,
        selected_record=record,
        discovered_airport_syap=discovered_airport_syap,
    )
