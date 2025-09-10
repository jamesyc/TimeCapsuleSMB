#!/usr/bin/env python3
"""
Disable SSH on an Apple AirPort/Time Capsule using AirPyrt (the `acp` module).
"""

from __future__ import annotations

import argparse
import sys
from typing import Iterable, List, Optional

from .common import (
    reboot,
    ssh_run_command
)


def disable_ssh(host: str, password: str, *, reboot_device: bool = True, python_candidates: Optional[Iterable[str]] = None, verbose: bool = True) -> None:
    """Disable SSH by removing the 'dbug' property. Some firmware only disables SSH when the 
    property is removed entirely rather than set to 0x0000.
    """
    # Prefer running 'acp remove dbug' locally over SSH; this persists best on some firmware.
    cmds = [
        "acp remove dbug",
        "/usr/sbin/acp remove dbug",
        "/usr/bin/acp remove dbug",
    ]
    last_err = None
    for c in cmds:
        rc, out = ssh_run_command(host, password, c, verbose=verbose)
        if rc == 0:
            if verbose:
                print("Removed 'dbug' via:", c)
            break
        last_err = (rc, out)
    else:
        code, out = last_err or (1, "unknown error")
        raise RuntimeError(f"Failed to remove 'dbug' via on-device acp (rc={code}). Output: {out}")

    if reboot_device:
        reboot(host, password, python_candidates=python_candidates, verbose=verbose)


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Disable SSH on AirPort/Time Capsule using AirPyrt (acp)")
    p.add_argument("--host", required=True, help="AirPort IPv4 address (preferred) or hostname")
    p.add_argument("--password", help="AirPort admin password (not Wi‑Fi password)")
    p.add_argument("--no-reboot", action="store_true", help="Do not reboot after disabling")
    p.add_argument("--quiet", action="store_true", help="Reduce output")
    args = p.parse_args(argv)

    password = args.password
    if not password:
        print("Error: --password is required", file=sys.stderr)
        return 2

    try:
        disable_ssh(args.host, password, reboot_device=not args.no_reboot, verbose=not args.quiet)
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
