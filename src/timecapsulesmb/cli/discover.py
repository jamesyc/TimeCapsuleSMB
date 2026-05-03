from __future__ import annotations

import argparse
import json
from typing import Optional

from timecapsulesmb.discovery.bonjour import (
    DEFAULT_BROWSE_TIMEOUT_SEC,
    BonjourResolvedService,
    BonjourServiceInstance,
    discover_snapshot,
    discovery_record_to_jsonable,
    service_instance_to_jsonable,
)


def print_instance_table(instances: list[BonjourServiceInstance]) -> None:
    if not instances:
        print("No Bonjour service instances discovered.")
        return
    headers = ["#", "Service", "Instance", "Full Name"]
    rows = []
    for i, instance in enumerate(instances, start=1):
        rows.append(
            [
                str(i),
                instance.service_type or "-",
                instance.name or "-",
                instance.fullname or "-",
            ]
        )
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
        rows.append(
            [
                str(i),
                record.service_type or "-",
                record.name,
                record.hostname or "-",
                str(record.port) if record.port else "-",
                ",".join(record.ipv4) if record.ipv4 else "-",
                ",".join(record.ipv6) if record.ipv6 else "-",
            ]
        )
    widths = [max(len(h), max((len(row[i]) for row in rows), default=0)) for i, h in enumerate(headers)]

    def fmt(cols: list[str]) -> str:
        return "  ".join(col.ljust(widths[i]) for i, col in enumerate(cols))

    print(fmt(headers))
    print(fmt(["-" * w for w in widths]))
    for row in rows:
        print(fmt(row))


def run_cli(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Discover Apple AirPort storage devices via mDNS/Bonjour")
    parser.add_argument("--timeout", type=float, default=DEFAULT_BROWSE_TIMEOUT_SEC, help="Browse time in seconds (default: 6)")
    parser.add_argument("--json", action="store_true", help="Output results as JSON")
    parser.add_argument("--select", action="store_true", help="Interactively select one and print selection")
    args = parser.parse_args(argv)

    try:
        snapshot = discover_snapshot(timeout=args.timeout)
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
    records = snapshot.resolved
    if args.json:
        print(
            json.dumps(
                {
                    "instances": [service_instance_to_jsonable(instance) for instance in snapshot.instances],
                    "resolved": [discovery_record_to_jsonable(record) for record in records],
                },
                indent=2,
                sort_keys=True,
            )
        )
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


def main(argv: Optional[list[str]] = None) -> int:
    return run_cli(argv)
