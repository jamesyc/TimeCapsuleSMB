from __future__ import annotations

import argparse
import time
from typing import Iterable, Optional

from timecapsulesmb.core.config import ENV_PATH, extract_host, parse_env_values
from timecapsulesmb.integrations.airpyrt import disable_ssh, enable_ssh
from timecapsulesmb.transport.local import tcp_open


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
    parser = argparse.ArgumentParser(description="Use the configured device target from .env to enable or disable SSH via AirPyrt.")
    parser.parse_args(argv)

    values = parse_env_values(ENV_PATH, defaults={})
    host_target = values.get("TC_HOST", "")
    password = values.get("TC_PASSWORD", "")
    if not host_target or not password:
        print(f"Missing {ENV_PATH} settings. Run '.venv/bin/tcapsule configure' first.")
        return 1
    airpyrt_host = extract_host(host_target)

    print(f"Using configured target from {ENV_PATH}: {host_target}")
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
