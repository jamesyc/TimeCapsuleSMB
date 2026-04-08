from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Optional

from timecapsulesmb.checks.doctor import run_doctor_checks
from timecapsulesmb.checks.models import CheckResult
from timecapsulesmb.core.config import ENV_PATH, parse_env_values


REPO_ROOT = Path(__file__).resolve().parents[3]


def print_result(result: CheckResult) -> None:
    print(f"{result.status} {result.message}")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run local diagnostics for the current TimeCapsuleSMB setup.")
    parser.add_argument("--skip-ssh", action="store_true", help="Skip SSH reachability checks")
    parser.add_argument("--skip-bonjour", action="store_true", help="Skip Bonjour browse/resolve checks")
    parser.add_argument("--skip-smb", action="store_true", help="Skip authenticated SMB listing check")
    parser.add_argument("--json", action="store_true", help="Output doctor results as JSON")
    args = parser.parse_args(argv)

    values = parse_env_values(ENV_PATH)
    results, fatal = run_doctor_checks(
        values,
        env_exists=ENV_PATH.exists(),
        repo_root=REPO_ROOT,
        skip_ssh=args.skip_ssh,
        skip_bonjour=args.skip_bonjour,
        skip_smb=args.skip_smb,
        on_result=None if args.json else print_result,
    )

    if args.json:
        print(json.dumps({
            "fatal": fatal,
            "results": [{"status": result.status, "message": result.message} for result in results],
            "summary": "doctor found one or more fatal problems." if fatal else "doctor checks passed.",
        }, indent=2, sort_keys=True))
        return 1 if fatal else 0

    if fatal:
        print("\nSummary: doctor found one or more fatal problems.")
        return 1

    print("\nSummary: doctor checks passed.")
    return 0
