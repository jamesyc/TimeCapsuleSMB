from __future__ import annotations

import argparse
import ipaddress
import json
import socket
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Optional
from collections.abc import Sequence


SERVICE_TYPES = [
    "_airport._tcp.local.",
    "_smb._tcp.local.",
    "_afpovertcp._tcp.local.",
    "_device-info._tcp.local.",
]

AIRPORT_SERVICE = "_airport"
SMB_SERVICE = "_smb"
DEFAULT_BROWSE_TIMEOUT_SEC = 6.0
PENDING_RESOLVE_INTERVAL_SEC = 0.5
PENDING_RESOLVE_TIMEOUT_MS = 500
FINAL_PENDING_RESOLVE_TIMEOUT_MS = 3000


@dataclass
class BonjourServiceInstance:
    service_type: str
    name: str
    fullname: str


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

    def __post_init__(self) -> None:
        self.ipv4 = list(self.ipv4)
        self.ipv6 = list(self.ipv6)
        if self.service_type and not self.services:
            self.services.add(self.service_type)
        elif not self.service_type and len(self.services) == 1:
            self.service_type = next(iter(self.services))

    def prefer_host(self) -> str:
        return self.hostname or (self.ipv4[0] if self.ipv4 else (self.ipv6[0] if self.ipv6 else ""))


@dataclass
class BonjourDiscoverySnapshot:
    instances: list[BonjourServiceInstance]
    resolved: list[BonjourResolvedService]


Discovered = BonjourResolvedService


@dataclass
class ServiceObservation:
    name: str
    hostname: str
    service_type: str
    port: int = 0
    ipv4: list[str] = field(default_factory=list)
    ipv6: list[str] = field(default_factory=list)
    properties: dict[str, str] = field(default_factory=dict)
    fullname: str = ""


def discovered_record_root_host(rec: BonjourResolvedService) -> str | None:
    chosen_host = ""
    for ip in rec.ipv4:
        if not ip.startswith("169.254."):
            chosen_host = ip
            break
    if not chosen_host and rec.ipv4:
        chosen_host = rec.ipv4[0]
    if not chosen_host:
        chosen_host = rec.prefer_host()
    return f"root@{chosen_host}" if chosen_host else None


def _bytes_to_ip(addr_bytes: bytes) -> str:
    try:
        return str(ipaddress.ip_address(addr_bytes))
    except Exception:
        if len(addr_bytes) == 4:
            return socket.inet_ntop(socket.AF_INET, addr_bytes)
        if len(addr_bytes) == 16:
            return socket.inet_ntop(socket.AF_INET6, addr_bytes)
        return ""


def _decode_props(props: dict[bytes, bytes]) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in props.items():
        if v is None:
            continue
        try:
            key = k.decode("utf-8", "ignore")
            value = v.decode("utf-8", "ignore")
        except Exception:
            continue
        if not key:
            continue
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
            if extra_key and extra_key not in out:
                out[extra_key] = extra_value.strip()
    return out


def _display_name(fullname: str, service_type: str) -> str:
    suffix = service_type
    if fullname.endswith(suffix):
        return fullname[: -len(suffix)].rstrip(".")
    return fullname.rstrip(".")


def _normalize_hostname(value: str) -> str:
    return value.strip().rstrip(".").lower()


def _observation_merge_key(observation: ServiceObservation) -> tuple[str, str, str]:
    return (
        observation.service_type,
        observation.name.strip(),
        _normalize_hostname(observation.hostname),
    )


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


class Collector:
    def __init__(self, zc: Any, services: list[str]):
        self.zc = zc
        self.services = services
        self.lock = threading.Lock()
        self.instances: dict[tuple[str, str], BonjourServiceInstance] = {}
        self.observations: dict[tuple[str, str, str], ServiceObservation] = {}
        self.pending: set[tuple[str, str]] = set()
        self._browsers: list[Any] = []

    def start(self) -> None:
        from zeroconf import ServiceBrowser

        for stype in self.services:
            browser = ServiceBrowser(self.zc, stype, handlers=[self._on_service_state_change])
            self._browsers.append(browser)

    def _on_service_state_change(self, *, zeroconf: Any, service_type: str, name: str, state_change: Any) -> None:
        from zeroconf import ServiceStateChange

        if state_change is ServiceStateChange.Added or state_change is ServiceStateChange.Updated:
            instance = BonjourServiceInstance(
                service_type=service_type,
                name=_display_name(name, service_type),
                fullname=name,
            )
            with self.lock:
                self.instances[(service_type, name)] = instance
                self.pending.add((service_type, name))

    def service_instances(self) -> list[BonjourServiceInstance]:
        with self.lock:
            return list(self.instances.values())

    def resolve_pending(self, timeout_ms: int = FINAL_PENDING_RESOLVE_TIMEOUT_MS) -> None:
        with self.lock:
            pending = sorted(self.pending)

        for service_type, name in pending:
            try:
                info = self.zc.get_service_info(service_type, name, timeout_ms)
            except Exception:
                info = None
            if info:
                self.add_info(service_type, info)
                with self.lock:
                    self.pending.discard((service_type, name))

    def add_info(self, stype: str, info: Any) -> None:
        observation = resolved_service_from_info(stype, info)
        key = _observation_merge_key(
            ServiceObservation(
                name=observation.name,
                hostname=observation.hostname,
                service_type=observation.service_type,
            )
        )
        if not key:
            return

        with self.lock:
            rec = self.observations.get(key)
            if not rec:
                rec = ServiceObservation(
                    name=observation.name,
                    hostname=observation.hostname.rstrip("."),
                    service_type=observation.service_type,
                    port=observation.port,
                    fullname=observation.fullname,
                )
                self.observations[key] = rec
            if observation.port:
                rec.port = observation.port
            if observation.fullname:
                rec.fullname = observation.fullname
            for ip in observation.ipv4:
                if ip not in rec.ipv4:
                    rec.ipv4.append(ip)
            for ip in observation.ipv6:
                if ip not in rec.ipv6:
                    rec.ipv6.append(ip)
            rec.properties.update({k: v for k, v in observation.properties.items() if v})

    def results(self) -> list[BonjourResolvedService]:
        with self.lock:
            return [
                BonjourResolvedService(
                    name=observation.name,
                    hostname=observation.hostname.rstrip("."),
                    service_type=observation.service_type,
                    port=observation.port,
                    ipv4=list(observation.ipv4),
                    ipv6=list(observation.ipv6),
                    properties=dict(observation.properties),
                    fullname=observation.fullname,
                )
                for observation in self.observations.values()
            ]


def resolved_service_from_info(stype: str, info: Any) -> BonjourResolvedService:
    name = _display_name(info.name or "", stype)
    hostname = info.server or ""
    props = _decode_props({k: v for k, v in (info.properties or {}).items() if v is not None})
    ipv4: list[str] = []
    ipv6: list[str] = []

    try:
        addrs = list(info.addresses or [])
    except Exception:
        addrs = []

    for addr in addrs:
        ip = _bytes_to_ip(addr)
        if not ip:
            continue
        try:
            ip_obj = ipaddress.ip_address(ip)
            (ipv6 if ip_obj.version == 6 else ipv4).append(ip)
        except Exception:
            continue

    return BonjourResolvedService(
        name=name,
        hostname=hostname.rstrip("."),
        service_type=stype,
        port=int(getattr(info, "port", 0) or 0),
        ipv4=ipv4,
        ipv6=ipv6,
        properties=props,
        fullname=info.name or "",
    )


def _open_zeroconf() -> Any:
    try:
        from zeroconf import IPVersion, Zeroconf
    except Exception as e:
        raise SystemExit(
            "Failed to import zeroconf. Run './tcapsule bootstrap' first, or use 'make install'. "
            f"{type(e).__name__}: {e}"
        ) from e

    # Our Time Capsule targets advertise over IPv4, and zeroconf 0.147.x can
    # miss _smb._tcp browse results on macOS when run in dual-stack mode.
    return Zeroconf(ip_version=IPVersion.V4Only)


def browse_service_instances(service: str | None = None, timeout: float = DEFAULT_BROWSE_TIMEOUT_SEC) -> list[BonjourServiceInstance]:
    zc = _open_zeroconf()
    try:
        collector = Collector(zc, _matching_service_types(service))
        collector.start()
        time.sleep(max(0.0, timeout))
        instances = collector.service_instances()
    finally:
        try:
            zc.close()
        except Exception:
            pass

    instances.sort(key=lambda instance: (instance.service_type or "", instance.name or "", instance.fullname or ""))
    return instances


def resolve_service_instance(instance: BonjourServiceInstance, timeout_ms: int = FINAL_PENDING_RESOLVE_TIMEOUT_MS) -> BonjourResolvedService | None:
    zc = _open_zeroconf()
    try:
        info = zc.get_service_info(instance.service_type, instance.fullname, timeout_ms)
    finally:
        try:
            zc.close()
        except Exception:
            pass
    if not info:
        return None
    return resolved_service_from_info(instance.service_type, info)


def _sort_instances(instances: list[BonjourServiceInstance]) -> list[BonjourServiceInstance]:
    return sorted(instances, key=lambda instance: (instance.service_type or "", instance.name or "", instance.fullname or ""))


def _sort_records(records: list[BonjourResolvedService]) -> list[BonjourResolvedService]:
    return sorted(records, key=lambda record: (record.service_type or "", record.hostname or "", record.name or ""))


def discover_snapshot(service: str | None = None, timeout: float = DEFAULT_BROWSE_TIMEOUT_SEC) -> BonjourDiscoverySnapshot:
    zc = _open_zeroconf()
    try:
        collector = Collector(zc, _matching_service_types(service))
        collector.start()
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(PENDING_RESOLVE_INTERVAL_SEC, remaining))
            collector.resolve_pending(timeout_ms=PENDING_RESOLVE_TIMEOUT_MS)
        collector.resolve_pending(timeout_ms=FINAL_PENDING_RESOLVE_TIMEOUT_MS)
        records = collector.results()
        instances = collector.service_instances()
    finally:
        try:
            zc.close()
        except Exception:
            pass

    return BonjourDiscoverySnapshot(
        instances=_sort_instances(instances),
        resolved=_sort_records(records),
    )


def _records_with_unresolved_instances(snapshot: BonjourDiscoverySnapshot) -> list[BonjourResolvedService]:
    records = list(snapshot.resolved)
    resolved_keys = {(record.service_type, record.fullname) for record in records if record.fullname}
    for instance in snapshot.instances:
        if (instance.service_type, instance.fullname) in resolved_keys:
            continue
        records.append(
            BonjourResolvedService(
                name=instance.name,
                hostname="",
                service_type=instance.service_type,
                fullname=instance.fullname,
            )
        )
    return _sort_records(records)


def discover_resolved_records(service: str | None = None, timeout: float = DEFAULT_BROWSE_TIMEOUT_SEC) -> list[BonjourResolvedService]:
    return discover_snapshot(service=service, timeout=timeout).resolved


def discover(timeout: float = DEFAULT_BROWSE_TIMEOUT_SEC) -> list[BonjourResolvedService]:
    return _records_with_unresolved_instances(discover_snapshot(timeout=timeout))


def record_has_service(record: BonjourResolvedService, service: str) -> bool:
    raw_service = getattr(record, "service_type", "")
    if isinstance(raw_service, str) and raw_service.startswith(service):
        return True
    services = getattr(record, "services", set())
    return isinstance(services, (set, frozenset, list, tuple)) and any(
        isinstance(value, str) and value.startswith(service)
        for value in services
    )


def filter_service_records(records: list[BonjourResolvedService], service: str) -> list[BonjourResolvedService]:
    return [record for record in records if record_has_service(record, service)]


def print_instance_table(instances: list[BonjourServiceInstance]) -> None:
    if not instances:
        print("No Bonjour service instances discovered.")
        return
    headers = ["#", "Service", "Instance", "Full Name"]
    rows = []
    for i, instance in enumerate(instances, start=1):
        rows.append([
            str(i),
            instance.service_type or "-",
            instance.name or "-",
            instance.fullname or "-",
        ])
    widths = [max(len(h), max((len(row[i]) for row in rows), default=0)) for i, h in enumerate(headers)]

    def fmt(cols: list[str]) -> str:
        return "  ".join(col.ljust(widths[i]) for i, col in enumerate(cols))

    print(fmt(headers))
    print(fmt(["-" * w for w in widths]))
    for row in rows:
        print(fmt(row))


def print_table(records: list[BonjourResolvedService]) -> None:
    if not records:
        print("No resolved Bonjour service records discovered.")
        return
    headers = ["#", "Service", "Name", "Hostname (preferred)", "Port", "IPv4", "IPv6"]
    rows = []
    for i, record in enumerate(records, start=1):
        rows.append([
            str(i),
            record.service_type or "-",
            record.name,
            record.hostname or "-",
            str(record.port) if record.port else "-",
            ",".join(record.ipv4) if record.ipv4 else "-",
            ",".join(record.ipv6) if record.ipv6 else "-",
        ])
    widths = [max(len(h), max((len(row[i]) for row in rows), default=0)) for i, h in enumerate(headers)]

    def fmt(cols: list[str]) -> str:
        return "  ".join(col.ljust(widths[i]) for i, col in enumerate(cols))

    print(fmt(headers))
    print(fmt(["-" * w for w in widths]))
    for row in rows:
        print(fmt(row))


def discovery_record_to_jsonable(record: BonjourResolvedService) -> dict[str, object]:
    data = asdict(record)
    data["services"] = sorted(record.services)
    return data


def service_instance_to_jsonable(instance: BonjourServiceInstance) -> dict[str, object]:
    return asdict(instance)


def run_cli(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Discover Apple Time Capsules via mDNS/Bonjour")
    parser.add_argument("--timeout", type=float, default=DEFAULT_BROWSE_TIMEOUT_SEC, help="Browse time in seconds (default: 6)")
    parser.add_argument("--json", action="store_true", help="Output results as JSON")
    parser.add_argument("--select", action="store_true", help="Interactively select one and print selection")
    args = parser.parse_args(argv)

    snapshot = discover_snapshot(timeout=args.timeout)
    records = snapshot.resolved
    if args.json:
        print(json.dumps(
            {
                "instances": [service_instance_to_jsonable(instance) for instance in snapshot.instances],
                "resolved": [discovery_record_to_jsonable(record) for record in records],
            },
            indent=2,
            sort_keys=True,
        ))
        return 0

    print("Browse Results")
    print_instance_table(snapshot.instances)
    print()
    print("Resolved Records")
    print_table(records)

    if args.select and records:
        while True:
            try:
                raw = input("Select device number (q to quit): ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return 1
            if raw.lower() in {"q", "quit", "exit"}:
                return 0
            if not raw.isdigit():
                print("Please enter a valid number.")
                continue
            idx = int(raw)
            if not (1 <= idx <= len(records)):
                print("Out of range.")
                continue
            print(records[idx - 1].prefer_host())
            return 0

    return 0
