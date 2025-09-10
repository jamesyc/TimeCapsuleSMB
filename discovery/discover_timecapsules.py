#!/usr/bin/env python3
"""
Discover Apple Time Capsules on the local network via mDNS/Bonjour.

Features
- Browses common service types advertised by AirPort/Time Capsule devices.
- Extracts mDNS hostname (preferred) and IP addresses.
- Optional interactive selection, JSON output, and configurable timeout.

Usage
  python discovery/discover_timecapsules.py            # list discovered devices
  python discovery/discover_timecapsules.py --select   # interactively choose one
  python discovery/discover_timecapsules.py --json     # machine-readable output

Notes
- Requires the `zeroconf` library (see requirements.txt).
- Discovery is best-effort: we look at multiple Bonjour services and
  filter by model/name hints typical for Time Capsule.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import socket
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Set

from zeroconf import IPVersion, ServiceBrowser, ServiceInfo, ServiceStateChange, Zeroconf


# Common Bonjour services relevant to Apple base stations / file sharing
SERVICE_TYPES = [
    "_airport._tcp.local.",       # AirPort base station service; includes model info
    "_smb._tcp.local.",           # SMB file sharing
    "_afpovertcp._tcp.local.",    # AFP file sharing (legacy)
    "_device-info._tcp.local.",   # May expose Apple device model hints
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
    ipv4: List[str] = field(default_factory=list)
    ipv6: List[str] = field(default_factory=list)
    services: Set[str] = field(default_factory=set)
    properties: Dict[str, str] = field(default_factory=dict)

    def prefer_host(self) -> str:
        return self.hostname or (self.ipv4[0] if self.ipv4 else (self.ipv6[0] if self.ipv6 else ""))


def _bytes_to_ip(addr_bytes: bytes) -> str:
    try:
        return str(ipaddress.ip_address(addr_bytes))
    except Exception:
        # Some zeroconf versions may provide packed network-byte-order; try fallback
        if len(addr_bytes) == 4:
            return socket.inet_ntop(socket.AF_INET, addr_bytes)
        if len(addr_bytes) == 16:
            return socket.inet_ntop(socket.AF_INET6, addr_bytes)
        return ""


def _decode_props(props: Dict[bytes, bytes]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for k, v in props.items():
        try:
            out[k.decode("utf-8", "ignore")] = v.decode("utf-8", "ignore")
        except Exception:
            continue
    return out


def _looks_like_time_capsule(name: str, props: Dict[str, str]) -> bool:
    s = name.lower()
    if any(h in s for h in TIME_CAPSULE_HINTS):
        return True
    model = props.get("model", "").lower()
    if any(h in model for h in TIME_CAPSULE_HINTS):
        return True
    # Some devices expose `model=TimeCapsuleX` or similar codes
    if "timecapsule" in model.replace(" ", ""):
        return True
    return False


class Collector:
    def __init__(self, zc: Zeroconf, services: Iterable[str]):
        self.zc = zc
        self.services = list(services)
        self.lock = threading.Lock()
        self.records: Dict[str, Discovered] = {}
        self._browsers: List[ServiceBrowser] = []

    def start(self):
        for stype in self.services:
            browser = ServiceBrowser(self.zc, stype, handlers=[self._on_service_state_change])
            self._browsers.append(browser)

    def _on_service_state_change(self, *, zeroconf: Zeroconf, service_type: str, name: str, state_change: ServiceStateChange):
        try:
            if state_change is ServiceStateChange.Added or state_change is ServiceStateChange.Updated:
                info = zeroconf.get_service_info(service_type, name, 2000)
                if info:
                    self._add_info(service_type, info)
        except Exception:
            # Swallow exceptions from the zeroconf thread to avoid noisy stack traces
            # during discovery; discovery is best-effort.
            pass

    def _add_info(self, stype: str, info: ServiceInfo):
        name = info.name or ""
        hostname = info.server or ""
        props = _decode_props({k: v for k, v in (info.properties or {}).items() if v is not None})

        ipv4: List[str] = []
        ipv6: List[str] = []

        try:
            addrs = list(info.addresses or [])
        except Exception:
            addrs = []

        for a in addrs:
            ip = _bytes_to_ip(a)
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
            # Update host if we previously didn't have it
            if not rec.hostname and hostname:
                rec.hostname = hostname.rstrip(".")
            # Merge addresses
            for ip in ipv4:
                if ip not in rec.ipv4:
                    rec.ipv4.append(ip)
            for ip in ipv6:
                if ip not in rec.ipv6:
                    rec.ipv6.append(ip)
            # Merge props
            rec.properties.update({k: v for k, v in props.items() if v})

    def results(self) -> List[Discovered]:
        with self.lock:
            return list(self.records.values())


def discover(timeout: float = 5.0) -> List[Discovered]:
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

    # Filter for Time Capsules (by explicit hints in name/model)
    filtered = [r for r in all_results if _looks_like_time_capsule(r.name, r.properties)]

    # If nothing matched hints, fall back to any _airport entries (likely Apple base stations)
    if not filtered:
        # Fallback: anything advertising the AirPort base station service
        filtered = [r for r in all_results if "_airport._tcp.local." in r.services]

    # Sort by hostname then name for stable output
    filtered.sort(key=lambda r: (r.hostname or "", r.name or ""))
    return filtered


def print_table(records: List[Discovered]) -> None:
    if not records:
        print("No Time Capsules discovered.")
        return
    headers = ["#", "Name", "Hostname (preferred)", "IPv4", "IPv6"]
    rows = []
    for i, r in enumerate(records, start=1):
        rows.append([
            str(i),
            r.name,
            r.hostname,
            ",".join(r.ipv4) if r.ipv4 else "-",
            ",".join(r.ipv6) if r.ipv6 else "-",
        ])
    widths = [max(len(h), max((len(row[i]) for row in rows), default=0)) for i, h in enumerate(headers)]
    def fmt(cols: List[str]) -> str:
        return "  ".join(col.ljust(widths[i]) for i, col in enumerate(cols))
    print(fmt(headers))
    print(fmt(["-" * w for w in widths]))
    for row in rows:
        print(fmt(row))


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Discover Apple Time Capsules via mDNS/Bonjour")
    p.add_argument("--timeout", type=float, default=5.0, help="Browse time in seconds (default: 5)")
    p.add_argument("--json", action="store_true", help="Output results as JSON")
    p.add_argument("--select", action="store_true", help="Interactively select one and print selection")
    args = p.parse_args(argv)

    records = discover(timeout=args.timeout)

    if args.json:
        out = [
            {
                "name": r.name,
                "hostname": r.hostname,
                "ipv4": r.ipv4,
                "ipv6": r.ipv6,
                "services": sorted(list(r.services)),
                "properties": r.properties,
            }
            for r in records
        ]
        print(json.dumps(out, indent=2))
        return 0

    if not args.select:
        print_table(records)
        return 0

    # Interactive selection
    if not records:
        print("No Time Capsules discovered.")
        return 1

    print_table(records)
    while True:
        try:
            choice = input("Select a device by number (q to quit): ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 1
        if choice.lower() in {"q", "quit", "exit"}:
            return 1
        if not choice.isdigit():
            print("Please enter a number from the list.")
            continue
        idx = int(choice)
        if not (1 <= idx <= len(records)):
            print("Out of range.")
            continue
        r = records[idx - 1]
        # Print a concise, script-friendly line: hostname preferred, plus first IPv4
        preferred = r.hostname or (r.ipv4[0] if r.ipv4 else (r.ipv6[0] if r.ipv6 else ""))
        print(json.dumps({
            "name": r.name,
            "hostname": r.hostname,
            "preferred": preferred,
            "ipv4": r.ipv4,
            "ipv6": r.ipv6,
        }))
        return 0


if __name__ == "__main__":
    sys.exit(main())
