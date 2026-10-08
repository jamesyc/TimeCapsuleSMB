from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from typing import Any, Literal
from dataclasses import replace
import re

from timecapsulesmb.core.net import is_link_local_ip, is_link_local_ipv4, is_link_local_ipv6, same_scoped_ip

SERVICE_TYPES = [
    "_airport._tcp.local.",
    "_smb._tcp.local.",
    "_adisk._tcp.local.",
    "_afpovertcp._tcp.local.",
    "_device-info._tcp.local.",
]

# Apple's printd advertises a shared USB printer through mDNSResponder as
# these; browsed only by the doctor's printer check (guide G6), never by the
# device list, so they are kept out of SERVICE_TYPES.
PRINTER_SERVICE_TYPES = [
    "_pdl-datastream._tcp.local.",
    "_riousbprint._tcp.local.",
    "_printer._tcp.local.",
    "_ipp._tcp.local.",
]

AIRPORT_SERVICE = "_airport"
SMB_SERVICE = "_smb"
DEFAULT_BROWSE_TIMEOUT_SEC = 6.0
PENDING_RESOLVE_TIMEOUT_MS = 500
FINAL_PENDING_RESOLVE_TIMEOUT_MS = 3000
MAX_DIAGNOSTIC_OBSERVATIONS = 100
DNS_RECORD_TYPE_PTR = 12
MDNS_PORT = 5353
BonjourIPFamily = Literal["ipv4", "ipv6"]
SPLIT_FAMILIES: tuple[BonjourIPFamily, ...] = ("ipv4", "ipv6")


def normalize_airport_mac(value: object) -> str | None:
    """Apple prints waMA with either colons or hyphens; retain one spelling."""
    if not isinstance(value, str):
        return None
    value = value.strip().lower().replace("-", ":")
    if not re.fullmatch(r"[0-9a-f]{2}(?::[0-9a-f]{2}){5}", value):
        return None
    if value == "00:00:00:00:00:00" or int(value[:2], 16) & 1:
        return None
    return value


@dataclass
class BonjourServiceInstance:
    service_type: str
    name: str
    fullname: str
    interface_index: int | None = None

@dataclass
class BonjourResolvedService:
    name: str
    hostname: str
    service_type: str = ""
    port: int = 0
    ipv4: Sequence[str] = field(default_factory=list)
    ipv6: Sequence[str] = field(default_factory=list)
    services: set[str] = field(default_factory=set)
    properties: dict[str, str] = field(default_factory=dict)
    fullname: str = ""
    interface_index: int | None = None

    @property
    def airport_mac(self) -> str | None:
        return normalize_airport_mac(self.properties.get("waMA"))

    def __post_init__(self) -> None:
        self.ipv4 = list(self.ipv4)
        self.ipv6 = list(self.ipv6)
        if self.service_type and not self.services:
            self.services.add(self.service_type)
        elif not self.service_type and len(self.services) == 1:
            self.service_type = next(iter(self.services))

    def preferred_ipv4(self) -> str | None:
        for ip in self.ipv4:
            if not is_link_local_ipv4(ip):
                return ip
        return None

    def preferred_ipv6(self) -> str | None:
        for ip in self.ipv6:
            if not is_link_local_ipv6(ip):
                return ip
        return None

    def preferred_ip(self) -> str | None:
        return self.preferred_ipv4() or self.preferred_ipv6()

    def acp_addresses(self) -> list[str]:
        """Where to reach the AirPort's ACP: its LAN addresses, then link-local IPv6.

        A record merged from several observations can list an old and a new
        LAN address, so all are given. A Mac on another IPv4 subnet of the same
        network reaches the AirPort only over link-local IPv6, as AirPort
        Utility does. 169.254 is never used: where it would answer, the fe80
        address answers too.
        """
        lan = [ip for ip in self.ipv4 if not is_link_local_ipv4(ip)]
        if not lan and (ipv6 := self.preferred_ipv6()):
            lan = [ipv6]
        return lan + [ip for ip in self.ipv6 if is_link_local_ipv6(ip) and "%" in ip]

    def preferred_connection_host(self) -> str:
        preferred_ip = self.preferred_ip()
        if preferred_ip:
            return preferred_ip
        if self.ipv4 or self.ipv6:
            return ""
        return self.hostname

    def display_host(self) -> str:
        return self.preferred_connection_host() or self.hostname or (self.ipv4[0] if self.ipv4 else "")

@dataclass
class BonjourDiscoverySnapshot:
    instances: list[BonjourServiceInstance]
    resolved: list[BonjourResolvedService]

@dataclass
class BonjourServiceEvent:
    service_type: str
    state: str
    name: str
    fullname: str
    elapsed_sec: float

@dataclass
class BonjourPtrRecordObservation:
    service_type: str
    alias: str
    alias_name: str
    ttl: int
    expired: bool
    old_record_present: bool
    elapsed_sec: float

@dataclass
class BonjourDiscoveryDiagnostics:
    service: str | None
    service_types: list[str]
    timeout_sec: float
    elapsed_sec: float
    ip_version: str
    instance_count: int
    resolved_count: int
    pending_count: int
    service_added_count: int
    service_updated_count: int
    resolve_attempt_count: int
    resolve_success_count: int
    resolve_error_count: int
    zeroconf_version: str = ""
    zeroconf_interfaces: str = "system-default"
    instances: list[BonjourServiceInstance] = field(default_factory=list)
    resolved: list[BonjourResolvedService] = field(default_factory=list)
    service_events: list[BonjourServiceEvent] = field(default_factory=list)
    ptr_records: list[BonjourPtrRecordObservation] = field(default_factory=list)
    ptr_record_error: str | None = None

@dataclass
class BonjourFamilyDiscoveryAttempt:
    family: BonjourIPFamily
    snapshot: BonjourDiscoverySnapshot | None = None
    diagnostics: BonjourDiscoveryDiagnostics | None = None
    error: str | None = None

@dataclass
class BonjourQueryDiagnostics:
    provider: str
    service_types: list[str]
    timeout_sec: float
    elapsed_sec: float
    instance_count: int
    resolved_count: int
    details: object | None = None
    pending_count: int = 0
    errors: dict[str, str] = field(default_factory=dict)
    attempts: list[BonjourFamilyDiscoveryAttempt] = field(default_factory=list)

    def family_result(
        self, record: BonjourResolvedService | None, family: BonjourIPFamily,
    ) -> tuple[list[str], str | None]:
        # A successful transport may answer both A and AAAA. Transport failure
        # must not erase evidence received over the other transport.
        if self.attempts:
            records = [r for attempt in self.attempts if attempt.snapshot for r in attempt.snapshot.resolved]
        else:
            records = [record] if record else []
        addresses = list(dict.fromkeys(ip for r in records for ip in getattr(r, family)))
        error = self.errors.get("query") or (self.errors.get(family) if not addresses else None)
        return addresses, error


class BonjourPermissionDenied(PermissionError):
    """An explicit DNS-SD policy-denied callback, not an inference from empty results."""


class BonjourDiscoveryError(RuntimeError):
    def __init__(self, attempts: Sequence[BonjourFamilyDiscoveryAttempt]) -> None:
        self.attempts = list(attempts)
        errors = [
            f"{attempt.family}: {attempt.error}"
            for attempt in self.attempts
            if attempt.error
        ]
        detail = "; ".join(errors) if errors else "no split-family attempts completed"
        super().__init__(f"Bonjour discovery failed for all usable address families ({detail})")

def discovered_record_root_host(rec: BonjourResolvedService) -> str | None:
    chosen_host = rec.preferred_connection_host()
    return f"root@{chosen_host}" if chosen_host else None

def discovered_record_has_only_link_local_ips(rec: BonjourResolvedService) -> bool:
    addresses = list(rec.ipv4) + list(rec.ipv6)
    return bool(addresses) and all(is_link_local_ip(ip) for ip in addresses)

def _decode_props(props: dict[bytes, bytes]) -> dict[str, str]:
    decoded: dict[str, str] = {}
    for k, v in props.items():
        if v is None:
            continue
        try:
            key = k.decode("utf-8", "ignore")
            value = v.decode("utf-8", "ignore")
        except Exception:
            continue
        if key:
            decoded[key] = value
    return normalize_properties(decoded)


def normalize_properties(props: dict[str, str]) -> dict[str, str]:
    """Expand Apple's packed TXT fields after each transport has decoded them.

    Explicit fields take precedence over embedded fields regardless of wire order.
    ADisk's packed values stay intact for share/volume validation.
    """
    out: dict[str, str] = {}
    embedded: dict[str, str] = {}
    for key, value in props.items():
        if "," not in value:
            out[key] = value
            continue

        chunks = [chunk.strip() for chunk in value.split(",")]
        first_chunk = chunks[0] if chunks else ""
        if "=" in first_chunk:
            out[key] = value
        else:
            out[key] = first_chunk

        for chunk in chunks:
            if "=" not in chunk:
                continue
            extra_key, extra_value = chunk.split("=", 1)
            extra_key = extra_key.strip()
            if extra_key:
                embedded.setdefault(extra_key, extra_value.strip())
    return {**embedded, **out}

def _normalize_hostname(value: str) -> str:
    return value.strip().rstrip(".").lower()

def _service_matches(service_type: str, service: str) -> bool:
    return service_type.startswith(service)

def _matching_service_types(service: str | None = None) -> list[str]:
    if not service:
        return list(SERVICE_TYPES)
    matching = [service_type for service_type in SERVICE_TYPES if _service_matches(service_type, service)]
    if matching:
        return matching
    candidate = service.strip()
    if candidate.endswith("."):
        return [candidate]
    if "._tcp.local" in candidate or "._udp.local" in candidate:
        return [f"{candidate}."]
    return [candidate]

def _sort_instances(instances: list[BonjourServiceInstance]) -> list[BonjourServiceInstance]:
    return sorted(instances, key=lambda instance: (instance.service_type or "", instance.name or "", instance.fullname or ""))

def _sort_records(records: list[BonjourResolvedService]) -> list[BonjourResolvedService]:
    return sorted(records, key=lambda record: (record.service_type or "", record.hostname or "", record.name or ""))

def _append_unique(values: list[str], candidates: Sequence[str]) -> None:
    for candidate in candidates:
        if candidate and not any(candidate == value or same_scoped_ip(candidate, value) for value in values):
            values.append(candidate)

def _merge_snapshots(snapshots: Sequence[BonjourDiscoverySnapshot]) -> BonjourDiscoverySnapshot:
    instances: dict[tuple[Any, ...], BonjourServiceInstance] = {}
    records: dict[tuple[Any, ...], list[BonjourResolvedService]] = {}
    observed_scopes: dict[tuple[Any, ...], set[int]] = {}
    for snapshot in snapshots:
        for r in snapshot.resolved:
            if r.interface_index:
                base = (r.service_type, r.name.casefold(), _normalize_hostname(r.hostname), r.port, r.airport_mac)
                observed_scopes.setdefault(base, set()).add(r.interface_index)
    for snapshot in snapshots:
        for instance in snapshot.instances:
            instances.setdefault((instance.service_type, instance.fullname, instance.interface_index), instance)
        for record in snapshot.resolved:
            base = (record.service_type, record.name.casefold(), _normalize_hostname(record.hostname), record.port, record.airport_mac)
            scopes = observed_scopes.get(base, set())
            scope = record.interface_index or (next(iter(scopes)) if len(scopes) == 1 else None)
            key = (*base, scope)
            group = records.setdefault(key, [])
            # A different TXT value is evidence, not a last-writer-wins update.
            existing = next((r for r in group if all(
                k not in r.properties or r.properties[k] == v
                or (k == "waMA" and r.airport_mac == record.airport_mac)
                for k, v in record.properties.items()
            )), None)
            if existing is None:
                group.append(replace(record, ipv4=list(record.ipv4), ipv6=list(record.ipv6), properties=dict(record.properties), services=set(record.services), hostname=record.hostname.rstrip("."), interface_index=scope))
            else:
                _append_unique(existing.ipv4, record.ipv4)
                _append_unique(existing.ipv6, record.ipv6)
                existing.properties.update(record.properties)
                existing.services.update(record.services)
    return BonjourDiscoverySnapshot(_sort_instances(list(instances.values())), _sort_records([r for group in records.values() for r in group]))


def discovery_record_to_jsonable(record: BonjourResolvedService) -> dict[str, object]:
    data = asdict(record)
    data["services"] = sorted(record.services)
    if record.interface_index is None:
        data.pop("interface_index")
    return data

def service_instance_to_jsonable(instance: BonjourServiceInstance) -> dict[str, object]:
    data = asdict(instance)
    if instance.interface_index is None:
        data.pop("interface_index")
    return data
