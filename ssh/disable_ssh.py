#!/usr/bin/env python3
"""
Disable SSH on an Apple AirPort/Time Capsule using AirPyrt (the `acp` module).

This sets the `dbug` property to `0x0000` and optionally reboots the device.
"""

from __future__ import annotations

import argparse
import os
import sys
import socket
import time
from contextlib import closing
from typing import Iterable, List, Optional

from .common import set_dbug, remove_property, reboot, ssh_run_command


def disable_ssh(host: str, password: str, *, reboot_device: bool = True, python_candidates: Optional[Iterable[str]] = None, verbose: bool = True) -> None:
    """Disable SSH by removing the 'dbug' property, with fallback to 0x0000.

    Some firmware only disables SSH when the property is removed entirely rather
    than set to 0x0000. Try removal first; on failure, fall back to setting 0x0000.
    """
    # Prefer running 'acp remove dbug' locally over SSH, which is known to stick
    # on some firmware versions.
    try:
        rc, _ = ssh_run_command(host, password, "true", timeout=10, verbose=verbose)
        if rc == 0:
            if verbose:
                print("SSH reachable; removing 'dbug' locally on the device...")
            # Try a few likely acp paths; most firmwares have it on PATH.
            for c in ("acp remove dbug", "/usr/sbin/acp remove dbug", "/usr/bin/acp remove dbug"):
                rc2, out2 = ssh_run_command(host, password, c, timeout=20, verbose=verbose)
                if rc2 == 0:
                    if verbose:
                        print("Removed 'dbug' via:", c)
                    break
            else:
                raise RuntimeError("Failed to remove 'dbug' via on-device acp.")

            time.sleep(1.0)
            if reboot_device:
                reboot(host, password, python_candidates=python_candidates, verbose=verbose)
            return
    except RuntimeError:
        # pexpect missing or ssh not available; fall back to AirPyrt flow below
        pass

    # Fall back to remote AirPyrt control plane
    try:
        remove_property(host, password, "dbug", python_candidates=python_candidates, verbose=verbose)
    except RuntimeError as e:
        if verbose:
            print(f"Remote remove failed ({e}); falling back to setting dbug=0x0000")
        set_dbug(host, password, "0x0000", python_candidates=python_candidates, verbose=verbose)

    # Give the base station a moment to persist the change before rebooting.
    time.sleep(1.0)
    if reboot_device:
        reboot(host, password, python_candidates=python_candidates, verbose=verbose)


# --- Post-disable helpers used by setup.py ---

def _tcp_open(host: str, port: int, timeout: float = 2.0) -> bool:
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


def wait_for_ssh_close(host: str, *, timeout_seconds: int = 120, interval_seconds: int = 3, verbose: bool = True) -> bool:
    if verbose:
        print("Reboot requested to disable SSH; waiting for SSH to close...")
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        time.sleep(interval_seconds)
        if not _tcp_open(host, 22):
            if verbose:
                print("SSH port closed; waiting for device to finish rebooting...")
            return True
    if verbose:
        print("SSH still reachable; it may take longer. Verify later.")
    return False


def wait_for_device_up(host: str, *, probe_ports: Iterable[int] = (5009, 445, 139), timeout_seconds: int = 180, interval_seconds: int = 5) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        time.sleep(interval_seconds)
        if any(_tcp_open(host, p) for p in probe_ports):
            return True
    return False


def ssh_stays_disabled(host: str, *, stabilization_seconds: int = 30, interval_seconds: int = 5, verbose: bool = True) -> bool:
    if verbose:
        print("Device is back up; confirming SSH remains disabled...")
    deadline = time.time() + stabilization_seconds
    while time.time() < deadline:
        time.sleep(interval_seconds)
        if _tcp_open(host, 22):
            return False
    return True


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Disable SSH on AirPort/Time Capsule using AirPyrt (acp)")
    p.add_argument("--host", required=True, help="AirPort IPv4 address (preferred) or hostname")
    p.add_argument("--password", help="AirPort admin password (not Wi‑Fi password)")
    p.add_argument("--no-reboot", action="store_true", help="Do not reboot after disabling")
    p.add_argument("--quiet", action="store_true", help="Reduce output")
    args = p.parse_args(argv)

    password = args.password or os.environ.get("AIRPORT_ADMIN_PASSWORD")
    if not password:
        print("Error: --password or AIRPORT_ADMIN_PASSWORD is required", file=sys.stderr)
        return 2

    try:
        disable_ssh(args.host, password, reboot_device=not args.no_reboot, verbose=not args.quiet)
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
