from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Optional

from timecapsulesmb.checks.doctor import run_doctor_checks
from timecapsulesmb.checks.models import CheckResult
from timecapsulesmb.cli.context import CommandContext
from timecapsulesmb.cli.runtime import inspect_managed_connection, load_env_values
from timecapsulesmb.cli.util import color_green, color_red
from timecapsulesmb.core.config import ENV_PATH
from timecapsulesmb.identity import ensure_install_id
from timecapsulesmb.telemetry import TelemetryClient, build_device_os_version


REPO_ROOT = Path(__file__).resolve().parents[3]


def print_result(result: CheckResult) -> None:
    status = result.status
    if status == "PASS":
        status = color_green(status)
    elif status == "FAIL":
        status = color_red(status)
    print(f"{status} {result.message}")


def build_doctor_error(results: list[CheckResult]) -> str | None:
    fail_lines = [f"{result.status} {result.message}" for result in results if result.status == "FAIL"]
    warn_lines = [f"{result.status} {result.message}" for result in results if result.status == "WARN"]
    lines: list[str] = []
    if fail_lines:
        lines.append("Doctor failures:")
        lines.extend(fail_lines)
    if warn_lines:
        if lines:
            lines.append("")
        lines.append("Doctor warnings:")
        lines.extend(warn_lines)
    return "\n".join(lines) if lines else None


def print_followup_help() -> None:
    print("")
    print("For other issues:")
    print("- (If you have xattr issues, or macOS Error -50) then try running:")
    print("    .venv/bin/tcapsule repair-xattrs")
    print("")
    print("- (If you have disk corruption issues) then try running:")
    print("    .venv/bin/tcapsule fsck")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run local diagnostics for the current TimeCapsuleSMB setup.")
    parser.add_argument("--skip-ssh", action="store_true", help="Skip SSH reachability checks")
    parser.add_argument("--skip-bonjour", action="store_true", help="Skip Bonjour browse/resolve checks")
    parser.add_argument("--skip-smb", action="store_true", help="Skip authenticated SMB listing check")
    parser.add_argument("--json", action="store_true", help="Output doctor results as JSON")
    args = parser.parse_args(argv)

    ensure_install_id()
    values = load_env_values()
    telemetry = TelemetryClient.from_values(values)
    with CommandContext(telemetry, "doctor", "doctor_started", "doctor_finished", values=values, args=args) as command_context:
        if ENV_PATH.exists() and not args.skip_ssh and values.get("TC_NET_IFACE"):
            try:
                connection = command_context.resolve_env_connection()
                managed_target = inspect_managed_connection(
                    connection,
                    values["TC_NET_IFACE"],
                    include_probe=True,
                )
                command_context.connection = managed_target.connection
                command_context.probe_state = managed_target.probe_state
                command_context.compatibility = (
                    managed_target.probe_state.compatibility
                    if managed_target.probe_state is not None
                    else None
                )
                if command_context.compatibility is not None:
                    command_context.update_fields(
                        device_os_version=build_device_os_version(
                            command_context.compatibility.os_name,
                            command_context.compatibility.os_release,
                            command_context.compatibility.arch,
                        ),
                        device_family=command_context.compatibility.payload_family,
                    )
            except SystemExit as exc:
                command_context.preflight_error = f"doctor pre-inspection failed: {exc}"

        results, fatal = run_doctor_checks(
            values,
            env_exists=ENV_PATH.exists(),
            repo_root=REPO_ROOT,
            precomputed_probe_state=command_context.probe_state,
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
            if fatal:
                error = build_doctor_error(results)
                if error:
                    command_context.set_error(error)
                    command_context.add_debug_context()
                command_context.fail()
            else:
                command_context.succeed()
            return 1 if fatal else 0

        if fatal:
            print("\nSummary: doctor found one or more fatal problems.")
            print_followup_help()
            error = build_doctor_error(results)
            if error:
                command_context.set_error(error)
                command_context.add_debug_context()
            command_context.fail()
            return 1

        print("\nSummary: doctor checks passed.")
        print_followup_help()
        command_context.succeed()
        return 0
