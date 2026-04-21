from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Optional

from timecapsulesmb.checks.doctor import run_doctor_checks
from timecapsulesmb.checks.models import CheckResult
from timecapsulesmb.cli.context import CommandContext
from timecapsulesmb.core.config import ENV_PATH, parse_env_values
from timecapsulesmb.device.compat import probe_device_compatibility
from timecapsulesmb.identity import ensure_install_id
from timecapsulesmb.telemetry import TelemetryClient, build_device_os_version, detect_device_family
from timecapsulesmb.cli.util import resolve_env_connection


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

    ensure_install_id()
    values = parse_env_values(ENV_PATH)
    telemetry = TelemetryClient.from_values(values)
    with CommandContext(telemetry, "doctor", "doctor_started", "doctor_finished") as command_context:
        if ENV_PATH.exists() and not args.skip_ssh:
            try:
                host, password, ssh_opts = resolve_env_connection(values)
                compatibility = probe_device_compatibility(host, password, ssh_opts)
                command_context.update_fields(device_os_version=build_device_os_version(
                    compatibility.os_name,
                    compatibility.os_release,
                    compatibility.arch,
                ))
                command_context.update_fields(device_family=detect_device_family(compatibility.payload_family))
            except SystemExit:
                pass

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
            command_context.set_result("failure" if fatal else "success")
            return 1 if fatal else 0

        if fatal:
            print("\nSummary: doctor found one or more fatal problems.")
            command_context.set_result("failure")
            return 1

        print("\nSummary: doctor checks passed.")
        command_context.set_result("success")
        return 0
