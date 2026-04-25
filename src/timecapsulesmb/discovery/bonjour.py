from __future__ import annotations

import argparse
import ipaddress
import json
import socket
import sys
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


@dataclass
class Discovered:
    name: str
    hostname: str
    service_type: str = ""
    ipv4: Sequence[str] = field(default_factory=list)
    ipv6: Sequence[str] = field(default_factory=list)
    services: set[str] = field(default_factory=set)
    properties: dict[str, str] = field(default_factory=dict)

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
class ServiceObservation:
    name: str
    hostname: str
    service_type: str
    ipv4: list[str] = field(default_factory=list)
    ipv6: list[str] = field(default_factory=list)
    properties: dict[str, str] = field(default_factory=dict)


def prefer_routable_ipv4(rec: Discovered) -> str:
    for ip in rec.ipv4:
        if not ip.startswith("169.254."):
            return ip
    return rec.ipv4[0] if rec.ipv4 else ""


def discovered_record_root_host(rec: Discovered) -> str | None:
    chosen_host = prefer_routable_ipv4(rec) or rec.prefer_host()
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
        out[key] = value
        if "," not in value:
            continue
        for chunk in value.split(","):
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


class Collector:
    def __init__(self, zc: Any, services: list[str]):
        self.zc = zc
        self.services = services
        self.lock = threading.Lock()
        self.observations: dict[tuple[str, str, str], ServiceObservation] = {}
        self._browsers: list[Any] = []

    def start(self) -> None:
        from zeroconf import ServiceBrowser

        for stype in self.services:
            browser = ServiceBrowser(self.zc, stype, handlers=[self._on_service_state_change])
            self._browsers.append(browser)

    def _on_service_state_change(self, *, zeroconf: Any, service_type: str, name: str, state_change: Any) -> None:
        from zeroconf import ServiceStateChange

        try:
            if state_change is ServiceStateChange.Added or state_change is ServiceStateChange.Updated:
                info = zeroconf.get_service_info(service_type, name, 2000)
                if info:
                    self._add_info(service_type, info)
        except Exception:
            pass

    def _add_info(self, stype: str, info: Any) -> None:
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

        key = _observation_merge_key(
            ServiceObservation(
                name=name,
                hostname=hostname,
                service_type=stype,
            )
        )
        if not key:
            return

        with self.lock:
            rec = self.observations.get(key)
            if not rec:
                rec = ServiceObservation(name=name, hostname=hostname.rstrip("."), service_type=stype)
                self.observations[key] = rec
            for ip in ipv4:
                if ip not in rec.ipv4:
                    rec.ipv4.append(ip)
            for ip in ipv6:
                if ip not in rec.ipv6:
                    rec.ipv6.append(ip)
            rec.properties.update({k: v for k, v in props.items() if v})

    def results(self) -> list[Discovered]:
        with self.lock:
            return [
                Discovered(
                    name=observation.name,
                    hostname=observation.hostname.rstrip("."),
                    service_type=observation.service_type,
                    ipv4=list(observation.ipv4),
                    ipv6=list(observation.ipv6),
                    properties=dict(observation.properties),
                )
                for observation in self.observations.values()
            ]


def discover(timeout: float = 5.0) -> list[Discovered]:
    try:
        from zeroconf import IPVersion, Zeroconf
    except Exception as e:
        print(
            "Failed to import zeroconf.\n"
            "Run './tcapsule bootstrap' first, or use 'make install'.\n",
            e,
            file=sys.stderr,
        )
        sys.exit(1)

    # Our Time Capsule targets advertise over IPv4, and zeroconf 0.147.x can
    # miss _smb._tcp browse results on macOS when run in dual-stack mode.
    zc = Zeroconf(ip_version=IPVersion.V4Only)
    try:
        collector = Collector(zc, SERVICE_TYPES)
        collector.start()
        time.sleep(timeout)
        records = collector.results()
    finally:
        try:
            zc.close()
        except Exception:
            pass

    records.sort(key=lambda record: (record.service_type or "", record.hostname or "", record.name or ""))
    return records


def record_has_service(record: Discovered, service: str) -> bool:
    raw_service = getattr(record, "service_type", "")
    if isinstance(raw_service, str) and raw_service.startswith(service):
        return True
    services = getattr(record, "services", set())
    return isinstance(services, (set, frozenset, list, tuple)) and any(
        isinstance(value, str) and value.startswith(service)
        for value in services
    )


def filter_service_records(records: list[Discovered], service: str) -> list[Discovered]:
    return [record for record in records if record_has_service(record, service)]


def print_table(records: list[Discovered]) -> None:
    if not records:
        print("No Bonjour services discovered.")
        return
    headers = ["#", "Service", "Name", "Hostname (preferred)", "IPv4", "IPv6"]
    rows = []
    for i, record in enumerate(records, start=1):
        rows.append([
            str(i),
            record.service_type or "-",
            record.name,
            record.hostname,
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


def discovery_record_to_jsonable(record: Discovered) -> dict[str, object]:
    data = asdict(record)
    data["services"] = sorted(record.services)
    return data


def run_cli(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Discover Apple Time Capsules via mDNS/Bonjour")
    parser.add_argument("--timeout", type=float, default=5.0, help="Browse time in seconds (default: 5)")
    parser.add_argument("--json", action="store_true", help="Output results as JSON")
    parser.add_argument("--select", action="store_true", help="Interactively select one and print selection")
    args = parser.parse_args(argv)

    records = discover(timeout=args.timeout)
    if args.json:
        print(json.dumps([discovery_record_to_jsonable(record) for record in records], indent=2, sort_keys=True))
        return 0

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
