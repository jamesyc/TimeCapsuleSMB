from __future__ import annotations

from timecapsulesmb.discovery.bonjour import run_cli


def main(argv: list[str] | None = None) -> int:
    return run_cli(argv)
