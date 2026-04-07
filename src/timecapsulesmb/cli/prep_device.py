from __future__ import annotations

import argparse
import getpass
import sys
import time
from typing import Iterable, Optional

from timecapsulesmb.discovery.bonjour import discover, prefer_routable_ipv4, preferred_host
from timecapsulesmb.integrations.airpyrt import disable_ssh, enable_ssh
from timecapsulesmb.transport.local import tcp_open


def list_devices(records) -> None:
    print("Found devices:")
    for i, record in enumerate(records, start=1):
        pref = preferred_host(record)
        ipv4 = ",".join(record.ipv4) if record.ipv4 else "-"
        print(f"  {i}. {record.name} | host: {pref} | IPv4: {ipv4}")


def choose_device(records):
    while True:
        try:
            raw = input("Select a device by number (q to quit): ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if raw.lower() in {"q", "quit", "exit"}:
            return None
        if not raw.isdigit():
            print("Please enter a valid number.")
            continue
        idx = int(raw)
        if not (1 <= idx <= len(records)):
            print("Out of range.")
            continue
        return records[idx - 1]


def wait_for_ssh(
    host: str,
    *,
    expected_state: bool,
    timeout_seconds: int = 120,
    interval_seconds: int = 5,
    verbose: bool = True,
) -> bool:
    expected_state_string = "open" if expected_state else "closed"
    if verbose:
        print(f"Waiting for SSH port to be {expected_state_string}...")
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        time.sleep(interval_seconds)
        is_open = tcp_open(host, 22)
        if (is_open and expected_state) or (not is_open and not expected_state):
            if verbose:
                print(f"SSH is {expected_state_string}.")
            return True
    if verbose:
        print(f"SSH did not {expected_state_string} within {timeout_seconds}s.")
    return False


def wait_for_device_up(
    host: str,
    *,
    probe_ports: Iterable[int] = (5009, 445, 139),
    timeout_seconds: int = 180,
    interval_seconds: int = 5,
) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        time.sleep(interval_seconds)
        if any(tcp_open(host, port) for port in probe_ports):
            return True
    return False


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Discover a Time Capsule and enable or disable SSH via AirPyrt.")
    parser.parse_args(argv)

    print("Discovering Time Capsules on the local network...")
    records = discover(timeout=5.0)
    if not records:
        print("No Time Capsules discovered. Ensure you're on the same network and try again.")
        return 1

    list_devices(records)
    selected_device = choose_device(records)
    if selected_device is None:
        return 0

    print("Selected:")
    print(f"  Name: {selected_device.name}")
    print(f"  Hostname (preferred): {preferred_host(selected_device)}")
    print(f"  IPv4: {','.join(selected_device.ipv4) if selected_device.ipv4 else '-'}")
    print(f"  IPv6: {','.join(selected_device.ipv6) if selected_device.ipv6 else '-'}")

    host = preferred_host(selected_device)
    airpyrt_host = prefer_routable_ipv4(selected_device) or host
    if not airpyrt_host:
        print("Could not determine a routable IPv4 or hostname for the selected device.")
        return 1

    try:
        password = getpass.getpass("Enter AirPort admin password: ")
    except (EOFError, KeyboardInterrupt):
        print()
        return 1
    if not password:
        print("No password provided.")
        return 1

    print(f"Probing SSH on {airpyrt_host}:22 ...")
    if not tcp_open(airpyrt_host, 22):
        print("SSH not reachable. Attempting to enable via AirPyrt...")
        try:
            enable_ssh(airpyrt_host, password, reboot_device=True, verbose=True)
        except Exception as e:
            print(f"Failed to enable SSH via AirPyrt: {e}")
            return 1

        if not wait_for_ssh(airpyrt_host, expected_state=True):
            return 1
    else:
        should_disable = False
        while True:
            try:
                resp = input("SSH already enabled. Disable? [y/N]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                resp = ""
            if resp in {"", "n", "no"}:
                print("Leaving SSH enabled.")
                break
            if resp in {"y", "yes"}:
                should_disable = True
                break
            print("Please answer 'y' or 'n'.")

        if should_disable:
            try:
                disable_ssh(airpyrt_host, password, reboot_device=True, verbose=True)
            except Exception as e:
                print(f"Failed to disable SSH via AirPyrt: {e}")
                return 1

            print("Device is starting reboot now, waiting for it to shut down...")
            if not wait_for_ssh(airpyrt_host, expected_state=False):
                return 0
            print("Device is down now, verifying persistence after reboot...")
            wait_for_device_up(airpyrt_host)
            print("Device successfully rebooted. Checking if SSH is still disabled...")
            if not wait_for_ssh(airpyrt_host, expected_state=False, timeout_seconds=30):
                print("Warning: SSH reopened after reboot. Disable may not have persisted.")
            else:
                print("SSH disabled (remains closed after reboot). Enable SSH again if this was not intended.")
                return 0

    print("SSH is configured. You can connect as 'root' using the AirPort admin password.")
    return 0
