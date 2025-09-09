#!/usr/bin/env python3
"""
Interactive setup helper: discovers Apple Time Capsules via mDNS and lets the
user select one. Stores the selection in a local variable and prints a summary.

This script does not perform any further actions yet.
"""

from __future__ import annotations

import sys
from typing import Optional

try:
    from discovery.discover_timecapsules import Discovered, discover
except Exception as e:
    print("Failed to import discovery module. Did you run 'make install'?\n", e, file=sys.stderr)
    sys.exit(1)


def preferred_host(rec: Discovered) -> str:
    if rec.hostname:
        return rec.hostname
    if rec.ipv4:
        return rec.ipv4[0]
    if rec.ipv6:
        return rec.ipv6[0]
    return ""


def main(argv: Optional[list[str]] = None) -> int:
    print("Discovering Time Capsules on the local network...")
    records = discover(timeout=5.0)

    if not records:
        print("No Time Capsules discovered. Ensure you're on the same network and try again.")
        return 1

    # Display a concise list
    print("Found devices:")
    for i, r in enumerate(records, start=1):
        pref = preferred_host(r)
        ipv4 = ",".join(r.ipv4) if r.ipv4 else "-"
        print(f"  {i}. {r.name} | host: {pref} | IPv4: {ipv4}")

    # Interactive selection
    while True:
        try:
            s = input("Select a device by number (q to quit): ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 1
        if s.lower() in {"q", "quit", "exit"}:
            return 1
        if not s.isdigit():
            print("Please enter a valid number.")
            continue
        idx = int(s)
        if not (1 <= idx <= len(records)):
            print("Out of range.")
            continue
        selected_device = records[idx - 1]
        break

    # Save selection to a variable and print summary
    # (Downstream steps will use `selected_device`).
    print("Selected:")
    print(f"  Name: {selected_device.name}")
    print(f"  Hostname (preferred): {preferred_host(selected_device)}")
    print(f"  IPv4: {','.join(selected_device.ipv4) if selected_device.ipv4 else '-'}")
    print(f"  IPv6: {','.join(selected_device.ipv6) if selected_device.ipv6 else '-'}")

    # Intentionally do nothing else with the selection yet
    return 0


if __name__ == "__main__":
    sys.exit(main())

