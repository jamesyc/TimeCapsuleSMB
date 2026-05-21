from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Iterable

from timecapsulesmb.core.net import is_link_local_ipv4
from timecapsulesmb.discovery.bonjour import (
    AIRPORT_SERVICE,
    BonjourResolvedService,
    discovered_record_root_host,
    discovery_record_to_jsonable,
    record_has_service,
)


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


def device_candidates_from_records(
    records: Iterable[BonjourResolvedService],
    *,
    airport_only: bool = True,
) -> list[DiscoveredDeviceCandidate]:
    materialized = list(records)
    source_records = [record for record in materialized if record_has_service(record, AIRPORT_SERVICE)]
    if not airport_only and not source_records:
        source_records = materialized
    candidates = [
        _candidate_from_record(record, index)
        for index, record in enumerate(source_records)
    ]
    by_key: dict[str, DiscoveredDeviceCandidate] = {}
    for candidate in candidates:
        key = _dedupe_key(candidate)
        existing = by_key.get(key)
        if existing is None or _candidate_score(candidate) > _candidate_score(existing):
            by_key[key] = candidate
    return sorted(by_key.values(), key=lambda candidate: (candidate.name.casefold(), candidate.host.casefold(), candidate.id))


def device_candidate_to_jsonable(candidate: DiscoveredDeviceCandidate) -> dict[str, object]:
    return {
        "id": candidate.id,
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
        "service_type": candidate.service_type,
        "fullname": candidate.fullname,
        "selected_record": discovery_record_to_jsonable(candidate.selected_record),
    }


def _candidate_from_record(record: BonjourResolvedService, index: int) -> DiscoveredDeviceCandidate:
    preferred_ipv4 = _first_non_link_local_ipv4(record.ipv4)
    ssh_host = discovered_record_root_host(record)
    host = _host_from_ssh_host(ssh_host) or record.hostname or _first_value(record.ipv6) or ""
    name = record.name or record.hostname or host or "AirPort Device"
    fullname = record.fullname or ""
    return DiscoveredDeviceCandidate(
        id=_candidate_id(record, host=host, index=index),
        name=name,
        host=host,
        ssh_host=ssh_host,
        hostname=record.hostname or "",
        addresses=tuple([*record.ipv4, *record.ipv6]),
        ipv4=tuple(record.ipv4),
        ipv6=tuple(record.ipv6),
        preferred_ipv4=preferred_ipv4,
        link_local_only=bool(record.ipv4) and preferred_ipv4 is None,
        syap=_non_empty(record.properties.get("syAP") or record.properties.get("syap")),
        model=_non_empty(record.properties.get("model") or record.properties.get("am")),
        service_type=record.service_type or "",
        fullname=fullname,
        selected_record=record,
    )


def _candidate_score(candidate: DiscoveredDeviceCandidate) -> tuple[int, int, int, int]:
    return (
        1 if candidate.preferred_ipv4 else 0,
        1 if candidate.ssh_host else 0,
        1 if candidate.syap else 0,
        len(candidate.addresses),
    )


def _candidate_id(record: BonjourResolvedService, *, host: str, index: int) -> str:
    for prefix, value in (
        ("bonjour", record.fullname),
        ("hostname", record.hostname),
        ("host", host),
        ("name", record.name),
    ):
        normalized = _normalize(value)
        if normalized:
            return f"{prefix}:{normalized}"
    return f"discovered:{index}"


def _dedupe_key(candidate: DiscoveredDeviceCandidate) -> str:
    for prefix, value in (
        ("bonjour", candidate.fullname),
        ("hostname", candidate.hostname),
        ("host", candidate.host),
        ("name", candidate.name),
    ):
        normalized = _normalize(value)
        if normalized:
            return f"{prefix}:{normalized}"
    return candidate.id


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
