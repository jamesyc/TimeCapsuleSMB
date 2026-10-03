from __future__ import annotations

import ipaddress
import json
from dataclasses import dataclass, replace
from typing import Iterable

from timecapsulesmb.core.config import AIRPORT_SYAP_TO_MODEL
from timecapsulesmb.core.net import is_link_local_ipv4
from timecapsulesmb.device.compat import airport_syap_supported
from timecapsulesmb.discovery.bonjour import (
    AIRPORT_SERVICE,
    BonjourResolvedService,
    discovered_record_root_host,
    discovery_record_to_jsonable,
)

_GENERIC_MDNS_MODELS = frozenset({"AirPort", "TimeCapsule", "Time Capsule"})


@dataclass(frozen=True)
class DiscoveredDeviceCandidate:
    id: str
    name: str
    host: str
    ssh_host: str | None
    hostname: str
    addresses: tuple[str, ...]
    ipv4: tuple[str, ...]
    ipv6: tuple[str, ...]
    preferred_ipv4: str | None
    link_local_only: bool
    syap: str | None
    model: str | None
    service_type: str
    fullname: str
    selected_record: BonjourResolvedService
    airport_mac: str | None = None


def device_candidates_from_records(
    records: Iterable[BonjourResolvedService],
    *,
    airport_only: bool = True,
) -> list[DiscoveredDeviceCandidate]:
    materialized = list(records)
    source_records = [record for record in materialized if _record_has_service(record, AIRPORT_SERVICE)]
    if not airport_only and not source_records:
        source_records = materialized
    groups: dict[str, list[DiscoveredDeviceCandidate]] = {}
    for record in source_records:
        candidate = _candidate_from_record(record)
        groups.setdefault(candidate.id, []).append(candidate)
    devices = []
    for candidates in groups.values():
        # Keep configure's target tied to one real observation. Other addresses
        # describe the appliance but never invent an interface for that record.
        candidates.sort(key=lambda c: (_normalize(c.host), c.selected_record.interface_index or 0))
        selected = max(candidates, key=_candidate_score)
        ipv4 = tuple(dict.fromkeys(ip for c in candidates for ip in c.ipv4))
        ipv6 = tuple(dict.fromkeys(ip for c in candidates for ip in c.ipv6))
        devices.append(replace(selected, ipv4=ipv4, ipv6=ipv6, addresses=ipv4 + ipv6))
    return sorted(devices, key=lambda c: (c.name.casefold(), c.host.casefold(), c.id))


def device_candidate_to_jsonable(candidate: DiscoveredDeviceCandidate) -> dict[str, object]:
    return {
        "id": candidate.id,
        "airport_mac": candidate.airport_mac,
        "name": candidate.name,
        "host": candidate.host,
        "ssh_host": candidate.ssh_host,
        "hostname": candidate.hostname,
        "addresses": list(candidate.addresses),
        "ipv4": list(candidate.ipv4),
        "ipv6": list(candidate.ipv6),
        "preferred_ipv4": candidate.preferred_ipv4,
        "link_local_only": candidate.link_local_only,
        "syap": candidate.syap,
        "model": candidate.model,
        "supported_model": airport_syap_supported(candidate.syap),
        "service_type": candidate.service_type,
        "fullname": candidate.fullname,
        "selected_record": discovery_record_to_jsonable(candidate.selected_record),
    }


def _candidate_from_record(record: BonjourResolvedService) -> DiscoveredDeviceCandidate:
    preferred_ipv4 = _first_non_link_local_ipv4(record.ipv4)
    ssh_host = discovered_record_root_host(record)
    host = _host_from_ssh_host(ssh_host) or record.hostname or _first_value(record.ipv6) or ""
    name = record.name or record.hostname or host or "AirPort Device"
    fullname = record.fullname or ""
    syap = _non_empty(record.properties.get("syAP") or record.properties.get("syap"))
    model = _candidate_model(_non_empty(record.properties.get("model") or record.properties.get("am")), syap)
    return DiscoveredDeviceCandidate(
        id=_candidate_id(record, host=host),
        airport_mac=record.airport_mac,
        name=name,
        host=host,
        ssh_host=ssh_host,
        hostname=record.hostname or "",
        addresses=tuple([*record.ipv4, *record.ipv6]),
        ipv4=tuple(record.ipv4),
        ipv6=tuple(record.ipv6),
        preferred_ipv4=preferred_ipv4,
        link_local_only=bool(record.ipv4) and preferred_ipv4 is None,
        syap=syap,
        model=model,
        service_type=record.service_type or "",
        fullname=fullname,
        selected_record=record,
    )


def _record_has_service(record: BonjourResolvedService, service: str) -> bool:
    raw_service = getattr(record, "service_type", "")
    if isinstance(raw_service, str) and raw_service.startswith(service):
        return True
    services = getattr(record, "services", set())
    return isinstance(services, (set, frozenset, list, tuple)) and any(
        isinstance(value, str) and value.startswith(service)
        for value in services
    )


def _candidate_model(model: str | None, syap: str | None) -> str | None:
    inferred = AIRPORT_SYAP_TO_MODEL.get(syap or "")
    if inferred is not None and (model is None or model in _GENERIC_MDNS_MODELS):
        return inferred
    return model


def _candidate_score(candidate: DiscoveredDeviceCandidate) -> tuple[int, int, int, int]:
    return (
        1 if candidate.preferred_ipv4 else 0,
        1 if candidate.ssh_host else 0,
        1 if candidate.syap else 0,
        len(candidate.addresses),
    )


def _candidate_id(record: BonjourResolvedService, *, host: str) -> str:
    if record.airport_mac:
        return f"airport:{record.airport_mac}"
    # Without a hardware hint an ID describes this observation, not an appliance.
    # It must not change just because another similarly named peer appears.
    key = (_normalize(record.fullname) or _normalize(record.name),
           _normalize(record.hostname) or _normalize(host), record.service_type,
           record.interface_index or 0, record.port)
    return "observation:" + json.dumps(key, separators=(",", ":"), ensure_ascii=False)


def _first_non_link_local_ipv4(values: Iterable[str]) -> str | None:
    for value in values:
        if not value or is_link_local_ipv4(value):
            continue
        try:
            if ipaddress.ip_address(value).version == 4:
                return value
        except ValueError:
            continue
    return None


def _host_from_ssh_host(value: str | None) -> str:
    if not value:
        return ""
    return value.removeprefix("root@")


def _first_value(values: Iterable[str]) -> str:
    for value in values:
        if value:
            return value
    return ""


def _normalize(value: str | None) -> str:
    return (value or "").strip().rstrip(".").casefold()


def _non_empty(value: str | None) -> str | None:
    stripped = (value or "").strip()
    return stripped or None
