from __future__ import annotations

import argparse
from typing import Optional

from timecapsulesmb.identity import default_bootstrap_path, load_install_identity, set_telemetry_enabled


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Enable or disable anonymous usage telemetry.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--enable", action="store_true", help="Enable telemetry")
    group.add_argument("--disable", action="store_true", help="Disable telemetry")
    group.add_argument("--status", action="store_true", help="Only report the current setting")
    args = parser.parse_args(argv)

    path = default_bootstrap_path()
    if args.status:
        identity = load_install_identity(path)
        print(f"telemetry: {'enabled' if identity.telemetry_enabled else 'disabled'}")
        return 0

    if not args.enable and not args.disable:
        parser.error("select --enable, --disable, or --status")

    identity = set_telemetry_enabled(bool(args.enable), path)
    print(f"telemetry: {'enabled' if identity.telemetry_enabled else 'disabled'}")
    return 0
