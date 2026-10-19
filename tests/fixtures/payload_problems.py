"""Generate the manager-verdict contract fixture shared with the macOS app.

The app's DoctorSummary.payloadProblems is checked against it, so a verdict
the helper reports that the app does not know, or reads as stopping Samba
when it does not, fails the Swift tests instead of a dashboard.

    python -m tests.fixtures.payload_problems --write
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from timecapsulesmb.device.probe import DISK_PAYLOAD_PROBLEMS


FIXTURE_PATH = (
    Path(__file__).resolve().parents[2]
    / "macos/TimeCapsuleSMB/Tests/TimeCapsuleSMBAppTests/Fixtures/payload_problems.json"
)


def build() -> list[dict[str, object]]:
    return [{"code": problem.code, "samba_off": problem.samba_off, "token": problem.token}
            for problem in DISK_PAYLOAD_PROBLEMS]


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
        print(f"{FIXTURE_PATH} is stale; run python -m tests.fixtures.payload_problems --write", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
