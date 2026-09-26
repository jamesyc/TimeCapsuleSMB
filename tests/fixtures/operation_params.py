"""Generate the helper request-param contract fixture shared with the macOS app.

The app's request builders are checked against it, so a param the helper would
reject fails the Swift tests instead of a run.

    python -m tests.fixtures.operation_params --write
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from timecapsulesmb.app.ops import COMMON_PARAMS, OPERATION_SPECS


FIXTURE_PATH = (
    Path(__file__).resolve().parents[2]
    / "macos/TimeCapsuleSMB/Tests/TimeCapsuleSMBAppTests/Fixtures/operation_params.json"
)


def build() -> dict[str, object]:
    return {
        "common": sorted(COMMON_PARAMS),
        "operations": {spec.name: sorted(spec.params) for spec in OPERATION_SPECS},
    }


def render() -> str:
    return json.dumps(build(), indent=2, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--write", action="store_true", help="rewrite the fixture instead of checking it")
    args = parser.parse_args(argv)
    text = render()
    if args.write:
        FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
        FIXTURE_PATH.write_text(text)
        return 0
    if not FIXTURE_PATH.exists() or FIXTURE_PATH.read_text() != text:
        print(f"{FIXTURE_PATH} is stale; run python -m tests.fixtures.operation_params --write", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
