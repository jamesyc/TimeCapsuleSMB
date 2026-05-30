from __future__ import annotations

import argparse
from typing import Optional

from timecapsulesmb.cli.context import CommandContext
from timecapsulesmb.cli.runtime import add_config_argument, print_json
from timecapsulesmb.discovery.bonjour import (
    DEFAULT_BROWSE_TIMEOUT_SEC,
    BonjourResolvedService,
    BonjourServiceInstance,
    discover_snapshot_merged_detailed,
    discovery_record_to_jsonable,
    service_instance_to_jsonable,
)
from timecapsulesmb.identity import ensure_install_id
from timecapsulesmb.services.runtime import load_optional_env_config
from timecapsulesmb.telemetry import TelemetryClient


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
    headers = ["#", "Service", "Name", "Hostname", "Port", "IPv4", "IPv6"]
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Discover Apple AirPort storage devices via mDNS/Bonjour")
    add_config_argument(parser)
    parser.add_argument("--timeout", type=float, default=DEFAULT_BROWSE_TIMEOUT_SEC, help="Browse time in seconds (default: 6)")
    parser.add_argument("--json", action="store_true", help="Output results as JSON")
    parser.add_argument("--select", action="store_true", help="Interactively select one and print selection")
    return parser


def _run_discover(args: argparse.Namespace, command_context: CommandContext | None = None) -> int:
    try:
        if command_context is not None:
            command_context.set_stage("bonjour_discovery")
        snapshot, diagnostics = discover_snapshot_merged_detailed(timeout=args.timeout)
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
    records = snapshot.resolved
    if command_context is not None:
        command_context.update_fields(
            timeout_seconds=args.timeout,
            json_output=args.json,
            interactive_select=args.select,
            bonjour_instance_count=len(snapshot.instances),
            bonjour_resolved_count=len(records),
        )
        command_context.add_debug_fields(discovery_snapshot=snapshot, discovery_diagnostics=diagnostics)
    if args.json:
        print_json({
            "instances": [service_instance_to_jsonable(instance) for instance in snapshot.instances],
            "resolved": [discovery_record_to_jsonable(record) for record in records],
        })
        if command_context is not None:
            command_context.succeed()
        return 0

    if command_context is not None:
        command_context.set_stage("render_results")
    print("Browse Results")
    print_instance_table(snapshot.instances)
    print()
    print("Resolved Records")
    print_table(records)

    if args.select and records:
        if command_context is not None:
            command_context.set_stage("interactive_select")
        while True:
            try:
                raw = input("Select device number (q to quit): ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                if command_context is not None:
                    command_context.cancel_with_error("Cancelled during discovery selection.")
                return 1
            if raw.lower() in {"q", "quit", "exit"}:
                if command_context is not None:
                    command_context.update_fields(selection_cancelled=True)
                    command_context.succeed()
                return 0
            if not raw.isdigit():
                print("Please enter a valid number.")
                continue
            idx = int(raw)
            if not (1 <= idx <= len(records)):
                print("Out of range.")
                continue
            print(records[idx - 1].display_host())
            if command_context is not None:
                command_context.update_fields(selected_index=idx)
                command_context.succeed()
            return 0

    if command_context is not None:
        command_context.succeed()
    return 0


def run_cli(argv: Optional[list[str]] = None, command_context: CommandContext | None = None) -> int:
    args = build_parser().parse_args(argv)
    return _run_discover(args, command_context=command_context)


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    ensure_install_id()
    config = load_optional_env_config(env_path=args.config)
    telemetry = TelemetryClient.from_config(config)
    with CommandContext(
        telemetry,
        "discover",
        "discover_started",
        "discover_finished",
        config=config,
        args=args,
        timeout_seconds=args.timeout,
        json_output=args.json,
        interactive_select=args.select,
    ) as command_context:
        return _run_discover(args, command_context=command_context)
    return 1
