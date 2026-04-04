from __future__ import annotations

import argparse
from pathlib import Path

from timecapsulesmb.checks.doctor import run_doctor_checks
from timecapsulesmb.checks.models import CheckResult
from timecapsulesmb.core.config import ENV_PATH, parse_env_values


REPO_ROOT = Path(__file__).resolve().parents[3]


def print_result(result: CheckResult) -> None:
    print(f"{result.status} {result.message}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run local diagnostics for the current TimeCapsuleSMB setup.")
    parser.add_argument("--skip-ssh", action="store_true", help="Skip SSH reachability checks")
    parser.add_argument("--skip-bonjour", action="store_true", help="Skip Bonjour browse/resolve checks")
    parser.add_argument("--skip-smb", action="store_true", help="Skip authenticated SMB listing check")
    args = parser.parse_args(argv)

    values = parse_env_values(ENV_PATH)
    results, fatal = run_doctor_checks(
        values,
        env_exists=ENV_PATH.exists(),
        repo_root=REPO_ROOT,
        skip_ssh=args.skip_ssh,
        skip_bonjour=args.skip_bonjour,
        skip_smb=args.skip_smb,
    )

    for result in results:
        print_result(result)

    if fatal:
        print("\nSummary: doctor found one or more fatal problems.")
        return 1

    print("\nSummary: doctor checks passed.")
    return 0
