from __future__ import annotations

import argparse

from . import bootstrap, configure, deploy, discover, doctor, prep_device, uninstall


COMMANDS = {
    "bootstrap": bootstrap.main,
    "configure": configure.main,
    "deploy": deploy.main,
    "discover": discover.main,
    "doctor": doctor.main,
    "prep-device": prep_device.main,
    "uninstall": uninstall.main,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tcapsule", description="TimeCapsuleSMB command line interface.")
    parser.add_argument("command", choices=sorted(COMMANDS))
    parser.add_argument("args", nargs=argparse.REMAINDER)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return COMMANDS[args.command](args.args)


if __name__ == "__main__":
    raise SystemExit(main())
