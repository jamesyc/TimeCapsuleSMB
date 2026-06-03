from __future__ import annotations

import argparse
import sys
from typing import Optional

from . import activate, api, bootstrap, configure, deploy, discover, doctor, flash, fsck, paths, set_ssh, repair_xattrs, uninstall, validate_install
from timecapsulesmb.core.paths import DistributionRootError
from timecapsulesmb.services.version_check import check_client_version, render_version_block_message


COMMANDS = {
    "api": api.main,
    "bootstrap": bootstrap.main,
    "activate": activate.main,
    "configure": configure.main,
    "deploy": deploy.main,
    "discover": discover.main,
    "doctor": doctor.main,
    "flash": flash.main,
    "fsck": fsck.main,
    "paths": paths.main,
    "set-ssh": set_ssh.main,
    "repair-xattrs": repair_xattrs.main,
    "uninstall": uninstall.main,
    "validate-install": validate_install.main,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tcapsule", description="TimeCapsuleSMB command line interface.")
    parser.add_argument("command", choices=sorted(COMMANDS))
    parser.add_argument("args", nargs=argparse.REMAINDER)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command != "api" and "-h" not in args.args and "--help" not in args.args:
        try:
            version_check = check_client_version()
            if version_check.should_block:
                print(render_version_block_message(version_check), file=sys.stderr)
                return 1
        except Exception:
            pass
    try:
        return COMMANDS[args.command](args.args)
    except DistributionRootError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
