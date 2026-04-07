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


SERVICE_TYPES = [
    "_airport._tcp.local.",
    "_smb._tcp.local.",
    "_afpovertcp._tcp.local.",
    "_device-info._tcp.local.",
]

TIME_CAPSULE_HINTS = (
    "time capsule",
    "timecapsule",
    "capsule",
    "airport time capsule",
    "airport",
)


@dataclass
class Discovered:
    name: str
    hostname: str
    ipv4: list[str] = field(default_factory=list)
    ipv6: list[str] = field(default_factory=list)
    services: set[str] = field(default_factory=set)
    properties: dict[str, str] = field(default_factory=dict)

    def prefer_host(self) -> str:
        return self.hostname or (self.ipv4[0] if self.ipv4 else (self.ipv6[0] if self.ipv6 else ""))


def preferred_host(rec: Discovered) -> str:
    return rec.prefer_host()


def prefer_routable_ipv4(rec: Discovered) -> str:
    for ip in rec.ipv4:
        if not ip.startswith("169.254."):
            return ip
    return rec.ipv4[0] if rec.ipv4 else ""


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


def looks_like_time_capsule(name: str, hostname: str, props: dict[str, str]) -> bool:
    lowered_name = name.lower()
    if any(hint in lowered_name for hint in TIME_CAPSULE_HINTS):
        return True
    lowered_host = hostname.lower()
    if any(hint in lowered_host for hint in TIME_CAPSULE_HINTS):
        return True
    model = props.get("model", "").lower()
    if any(hint in model for hint in TIME_CAPSULE_HINTS):
        return True
    return "timecapsule" in model.replace(" ", "")


class Collector:
    def __init__(self, zc: Any, services: list[str]):
        self.zc = zc
        self.services = services
        self.lock = threading.Lock()
        self.records: dict[str, Discovered] = {}
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

        key = hostname or name
        if not key:
            return

        with self.lock:
            rec = self.records.get(key)
            if not rec:
                rec = Discovered(name=name, hostname=hostname.rstrip("."))
                self.records[key] = rec
            rec.services.add(stype)
            if not rec.name and name:
                rec.name = name
            if not rec.hostname and hostname:
                rec.hostname = hostname.rstrip(".")
            for ip in ipv4:
                if ip not in rec.ipv4:
                    rec.ipv4.append(ip)
            for ip in ipv6:
                if ip not in rec.ipv6:
                    rec.ipv6.append(ip)
            rec.properties.update({k: v for k, v in props.items() if v})

    def results(self) -> list[Discovered]:
        with self.lock:
            return list(self.records.values())


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

    zc = Zeroconf(ip_version=IPVersion.All)
    try:
        collector = Collector(zc, SERVICE_TYPES)
        collector.start()
        time.sleep(timeout)
        all_results = collector.results()
    finally:
        try:
            zc.close()
        except Exception:
            pass

    filtered = [record for record in all_results if looks_like_time_capsule(record.name, record.hostname, record.properties)]
    if not filtered:
        filtered = [record for record in all_results if "_airport._tcp.local." in record.services]
    filtered.sort(key=lambda record: (record.hostname or "", record.name or ""))
    return filtered


def print_table(records: list[Discovered]) -> None:
    if not records:
        print("No Time Capsules discovered.")
        return
    headers = ["#", "Name", "Hostname (preferred)", "IPv4", "IPv6"]
    rows = []
    for i, record in enumerate(records, start=1):
        rows.append([
            str(i),
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
