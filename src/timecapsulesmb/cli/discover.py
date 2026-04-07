from __future__ import annotations

from typing import Optional

from timecapsulesmb.discovery.bonjour import run_cli


def main(argv: Optional[list[str]] = None) -> int:
    return run_cli(argv)
