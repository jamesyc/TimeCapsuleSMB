from __future__ import annotations

from typing import Optional


def main(argv: Optional[list[str]] = None) -> int:
    from timecapsulesmb.app import helper

    return helper.main(argv)
