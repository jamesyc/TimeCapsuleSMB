#!/usr/bin/env python3
"""
Interactive setup helper: discovers Apple Time Capsules via mDNS and lets the
user select one. Stores the selection in a local variable and prints a summary.

This script does not perform any further actions yet.
"""

from __future__ import annotations

import sys
import socket
from contextlib import closing
from typing import Optional, List
import os
import time
import getpass

try:
    from discovery.discover_timecapsules import Discovered, discover
except Exception as e:
    print("Failed to import discovery module. Did you run 'make install'?\n", e, file=sys.stderr)
    sys.exit(1)

try:
    from ssh.enable_ssh import enable_ssh, wait_for_ssh_open
    from ssh.disable_ssh import (
        disable_ssh,
        wait_for_ssh_close,
        wait_for_device_up,
        ssh_stays_disabled,
    )
except Exception as e:
    print("Failed to import ssh module.\n", e, file=sys.stderr)
    sys.exit(1)

def preferred_host(rec: Discovered) -> str:
    if rec.hostname:
        return rec.hostname
    if rec.ipv4:
        return rec.ipv4[0]
    if rec.ipv6:
        return rec.ipv6[0]
    return ""

def prefer_routable_ipv4(rec: Discovered) -> str:
    # Prefer RFC1918 over link-local; skip 169.254/16 when possible
    for ip in rec.ipv4:
        if not ip.startswith("169.254."):
            return ip
    return rec.ipv4[0] if rec.ipv4 else ""

def ssh_open(host: str, port: int = 22, timeout: float = 2.0) -> bool:
    """Return True if a TCP connection to host:port succeeds within timeout."""
    try:
        for family, socktype, proto, _, sockaddr in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM):
            with closing(socket.socket(family, socktype, proto)) as s:
                s.settimeout(timeout)
                try:
                    s.connect(sockaddr)
                    return True
                except (socket.timeout, ConnectionRefusedError, OSError):
                    continue
    except Exception:
        return False
    return False

def list_devices(records: List[Discovered]) -> None:
    print("Found devices:")
    for i, r in enumerate(records, start=1):
        pref = preferred_host(r)
        ipv4 = ",".join(r.ipv4) if r.ipv4 else "-"
        print(f"  {i}. {r.name} | host: {pref} | IPv4: {ipv4}")

def choose_device(records: List[Discovered]) -> Optional[Discovered]:
    while True:
        try:
            s = input("Select a device by number (q to quit): ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if s.lower() in {"q", "quit", "exit"}:
            return None
        if not s.isdigit():
            print("Please enter a valid number.")
            continue
        idx = int(s)
        if not (1 <= idx <= len(records)):
            print("Out of range.")
            continue
        return records[idx - 1]

def main(argv: Optional[list[str]] = None) -> int:
    print("Discovering Time Capsules on the local network...")
    records = discover(timeout=5.0)

    if not records:
        print("No Time Capsules discovered. Ensure you're on the same network and try again.")
        return 1

    # Display a concise list and prompt for selection
    list_devices(records)
    selected_device = choose_device(records)
    if selected_device is None:
        return 0

    # Save selection to a variable and print summary
    # (Downstream steps will use `selected_device`).
    print("Selected:")
    print(f"  Name: {selected_device.name}")
    print(f"  Hostname (preferred): {preferred_host(selected_device)}")
    print(f"  IPv4: {','.join(selected_device.ipv4) if selected_device.ipv4 else '-'}")
    print(f"  IPv6: {','.join(selected_device.ipv6) if selected_device.ipv6 else '-'}")

    # Check if SSH is enabled on the selected device.
    # For AirPyrt, IPv4 is recommended by the upstream docs
    host = preferred_host(selected_device)
    airpyrt_host = prefer_routable_ipv4(selected_device) or host
    if not airpyrt_host:
        print("Could not determine a routable IPv4 or hostname for the selected device.")
        return 1

    print(f"Probing SSH on {airpyrt_host}:22 ...")
    if not ssh_open(airpyrt_host):
        print("SSH not reachable. Attempting to enable via AirPyrt...")

        if enable_ssh is None:
            print("AirPyrt enable script not available. Ensure ssh/enable_ssh.py exists.")
            return 1

        try:
            password = getpass.getpass("Enter AirPort admin password: ")
        except (EOFError, KeyboardInterrupt):
            print()
            return 1
        if not password:
            print("No password provided.")
            return 1

        try:
            enable_ssh(airpyrt_host, password, reboot_device=True, verbose=True)
        except Exception as e:
            print(f"Failed to enable SSH via AirPyrt: {e}")
            return 1

        # Wait for SSH to become reachable after reboot, then finish.
        if not wait_for_ssh_open(airpyrt_host):
            return 1
    
    else: 
        # SSH is reachable; offer to disable and revert to default
        while True:
            try:
                resp = input("SSH already enabled. Disable? [y/N]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                resp = ""
            if resp in {"", "n", "no"}:
                print("Leaving SSH enabled.")
                return 0
            if resp in {"y", "yes"}:
                break
            print("Please answer 'y' or 'n'.")

        try:
            password = getpass.getpass("Enter AirPort admin password: ")
        except (EOFError, KeyboardInterrupt):
            print()
            return 1
        if not password:
            print("No password provided.")
            return 1

        try:
            disable_ssh(airpyrt_host, password, reboot_device=True, verbose=True)
        except Exception as e:
            print(f"Failed to disable SSH via AirPyrt: {e}")
            return 1

        if not wait_for_ssh_close(airpyrt_host):
            return 0

        wait_for_device_up(airpyrt_host)

        if not ssh_stays_disabled(airpyrt_host, stabilization_seconds=30):
            print("Warning: SSH reopened after reboot. Disable may not have persisted.")
            print("- Ensure AirPyrt is installed and try disabling again.")
            print("- If this persists, the firmware may re-enable SSH via other debug flags.")
            print("  Consider resetting debug settings or power-cycling the device.")
        else:
            print("SSH disabled (remains closed after reboot).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
