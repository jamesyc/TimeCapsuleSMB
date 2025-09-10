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
"""

from __future__ import annotations

import argparse
import sys
from typing import Iterable, List, Optional

from .common import set_dbug, reboot


def enable_ssh(host: str, password: str, *, reboot_device: bool = True, python_candidates: Optional[Iterable[str]] = None, verbose: bool = True) -> None:
    """Enable SSH using AirPyrt `acp` module.

    Raises RuntimeError on failure.
    """
    # Set dbug to 0x3000
    set_dbug(host, password, "0x3000", python_candidates=python_candidates, verbose=verbose)

    if reboot_device:
        reboot(host, password, python_candidates=python_candidates, verbose=verbose)


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Enable SSH on AirPort/Time Capsule using AirPyrt (acp)")
    p.add_argument("--host", required=True, help="AirPort hostname (preferred) or IPv4 address")
    p.add_argument("--password", help="AirPort admin password (not Wi‑Fi password)")
    p.add_argument("--no-reboot", action="store_true", help="Do not send reboot command after enabling")
    p.add_argument("--quiet", action="store_true", help="Reduce output")
    args = p.parse_args(argv)

    password = args.password
    if not password:
        print("Error: --password is required", file=sys.stderr)
        return 2

    try:
        enable_ssh(args.host, password, reboot_device=not args.no_reboot, verbose=not args.quiet)
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 1

    if not args.no_reboot and not args.quiet:
        print("Reboot initiated. The device may take 60-120s to come back up.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
