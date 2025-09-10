#!/usr/bin/env python3
"""
Enable SSH on an Apple AirPort/Time Capsule using AirPyrt (the `acp` module).

This script locates a Python interpreter where `acp` is installed (AirPyrt),
sets the `dbug` property to `0x3000`, and reboots the device. After reboot,
you can connect as `root` using the AirPort admin password.

References:
- https://github.com/x56/airpyrt-tools (archived)
- Known recipe: `python -m acp -t <host> -p <password> --setprop dbug 0x3000` then `--reboot`

Usage (CLI):
  python ssh/enable_ssh.py --host <hostname-or-ip> --password <admin-password>

Environment overrides:
- AIRPORT_ADMIN_PASSWORD: default password if --password not given
- AIRPYRT_PY: path to Python interpreter with `acp` installed (e.g., python2.7)
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
import time
import socket
from contextlib import closing
from typing import Iterable, List, Optional

from .common import set_dbug, reboot as acp_reboot


def enable_ssh(host: str, password: str, *, reboot_device: bool = True, python_candidates: Optional[Iterable[str]] = None, verbose: bool = True) -> None:
    """Enable SSH using AirPyrt `acp` module.

    Raises RuntimeError on failure.
    """
    # Set dbug to 0x3000
    set_dbug(host, password, "0x3000", python_candidates=python_candidates, verbose=verbose)

    if reboot_device:
        acp_reboot(host, password, python_candidates=python_candidates, verbose=verbose)


# --- Post-enable helpers used by setup.py ---

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


def wait_for_ssh_open(host: str, *, timeout_seconds: int = 120, interval_seconds: int = 5, verbose: bool = True) -> bool:
    if verbose:
        print("Reboot requested; waiting for device to come back online...")
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        time.sleep(interval_seconds)
        if _tcp_open(host, 22):
            if verbose:
                print("SSH enabled and reachable.")
            return True
    if verbose:
        print("Device not reachable via SSH yet. It may take longer; try again later.")
    return False


def disable_ssh(host: str, password: str, *, reboot: bool = True, python_candidates: Optional[Iterable[str]] = None, verbose: bool = True) -> None:
    """Disable SSH by setting dbug=0x0000 via AirPyrt and optionally rebooting."""
    acp_exec = _find_acp_executable()
    py = _find_airpyrt_python(python_candidates)
    if not acp_exec and not py:
        raise RuntimeError(
            "AirPyrt (acp) not found. Install per https://github.com/samuelthomas2774/airport/wiki/AirPyrt#installation\n"
            "Example: git clone https://github.com/x56/airpyrt-tools.git && cd airpyrt-tools && python2 setup.py install --user\n"
            "Then ensure 'acp' is on PATH or set AIRPYRT_PY to that interpreter."
        )

    # Set dbug to 0x0000 (default)
    if acp_exec:
        set_cmd = [acp_exec, "-t", host, "-p", password, "--setprop", "dbug", "0x0000"]
    else:
        set_cmd = [py, "-B", "-m", "acp", "-t", host, "-p", password, "--setprop", "dbug", "0x0000"]
    if verbose:
        print("Running:", " ".join(shlex.quote(x) for x in set_cmd))
    try:
        _run(set_cmd, check=True)
    except subprocess.CalledProcessError as e:
        msg = e.stdout.strip() if e.stdout else str(e)
        raise RuntimeError(f"Failed to set dbug=0x0000 via AirPyrt. Output: {msg}")

    if reboot:
        if acp_exec:
            rb_cmd = [acp_exec, "-t", host, "-p", password, "--reboot"]
        else:
            rb_cmd = [py, "-B", "-m", "acp", "-t", host, "-p", password, "--reboot"]
        if verbose:
            print("Rebooting device:", " ".join(shlex.quote(x) for x in rb_cmd))
        try:
            _run(rb_cmd, check=True)
        except subprocess.CalledProcessError as e:
            msg = e.stdout.strip() if e.stdout else str(e)
            raise RuntimeError(f"Reboot command failed. Output: {msg}")


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Enable SSH on AirPort/Time Capsule using AirPyrt (acp)")
    p.add_argument("--host", required=True, help="AirPort hostname (preferred) or IPv4 address")
    p.add_argument("--password", help="AirPort admin password (not Wi‑Fi password)")
    p.add_argument("--no-reboot", action="store_true", help="Do not send reboot command after enabling")
    p.add_argument("--quiet", action="store_true", help="Reduce output")
    args = p.parse_args(argv)

    password = args.password or os.environ.get("AIRPORT_ADMIN_PASSWORD")
    if not password:
        print("Error: --password or AIRPORT_ADMIN_PASSWORD is required", file=sys.stderr)
        return 2

    try:
        enable_ssh(args.host, password, reboot=not args.no_reboot, verbose=not args.quiet)
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 1

    if not args.no_reboot and not args.quiet:
        print("Reboot initiated. The device may take 60–120s to come back up.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
