from __future__ import annotations

import argparse
import json
from typing import Optional

from timecapsulesmb.checks.doctor import run_doctor_checks
from timecapsulesmb.checks.models import CheckResult
from timecapsulesmb.cli.context import CommandContext
from timecapsulesmb.cli.runtime import add_config_argument, load_env_config
from timecapsulesmb.cli.util import color_green, color_red
from timecapsulesmb.core.config import ConfigError
from timecapsulesmb.core.errors import system_exit_message
from timecapsulesmb.device.errors import DeviceError
from timecapsulesmb.identity import ensure_install_id
from timecapsulesmb.telemetry import TelemetryClient
from timecapsulesmb.transport.errors import TransportError
from timecapsulesmb.core.paths import resolve_app_paths


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
    info_lines = [
        f"{result.status} {result.message}"
        for result in results
        if result.status == "INFO" and result.message.startswith("discovered _smb._tcp candidates:")
    ]
    lines: list[str] = []
    if fail_lines:
        lines.append("Doctor failures:")
        lines.extend(fail_lines)
    if warn_lines:
        if lines:
            lines.append("")
        lines.append("Doctor warnings:")
        lines.extend(warn_lines)
    if info_lines:
        if lines:
            lines.append("")
        lines.append("Doctor context:")
        lines.extend(info_lines)
    return "\n".join(lines) if lines else None


def print_followup_help() -> None:
    print("")
    print("Some troubleshooting tips:")
    print("- (To remove old Apple devices entries from mDNS cache) try running:")
    print("    sudo dscacheutil -flushcache && sudo killall -HUP mDNSResponder")
    print("- (If you have disk corruption issues, or error 22) then try running:")
    print("    .venv/bin/tcapsule fsck")
    print("- (If you have xattr issues, or macOS Error -50) then try running:")
    print("    .venv/bin/tcapsule repair-xattrs")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run local diagnostics for the current TimeCapsuleSMB setup.")
    add_config_argument(parser)
    parser.add_argument("--skip-ssh", action="store_true", help="Skip SSH reachability checks")
    parser.add_argument("--skip-bonjour", action="store_true", help="Skip Bonjour browse/resolve checks")
    parser.add_argument("--skip-smb", action="store_true", help="Skip authenticated SMB listing check")
    parser.add_argument("--json", action="store_true", help="Output doctor results as JSON")
    args = parser.parse_args(argv)

    ensure_install_id()
    app_paths = resolve_app_paths(config_path=args.config)
    config = load_env_config(env_path=args.config)
    telemetry = TelemetryClient.from_config(config)
    with CommandContext(telemetry, "doctor", "doctor_started", "doctor_finished", config=config, args=args) as command_context:
        command_context.update_fields(
            skip_ssh=args.skip_ssh,
            skip_bonjour=args.skip_bonjour,
            skip_smb=args.skip_smb,
            json_output=args.json,
        )
        if config.exists and not args.skip_ssh and config.get("TC_NET_IFACE"):
            command_context.set_stage("preinspect_device")
            try:
                command_context.inspect_managed_connection(
                    iface=config.require("TC_NET_IFACE"),
                    include_probe=True,
                )
            except (ConfigError, TransportError, DeviceError) as exc:
                command_context.preflight_error = f"doctor pre-inspection failed: {system_exit_message(exc)}"

        command_context.set_stage("run_checks")
        doctor_debug: dict[str, object] = {}
        results, fatal = run_doctor_checks(
            config,
            repo_root=app_paths.distribution_root,
            connection=command_context.connection,
            precomputed_interface_probe=command_context.interface_probe,
            precomputed_probe_state=command_context.probe_state,
            skip_ssh=args.skip_ssh,
            skip_bonjour=args.skip_bonjour,
            skip_smb=args.skip_smb,
            on_result=None if args.json else print_result,
            debug_fields=doctor_debug,
        )
        command_context.add_debug_fields(**doctor_debug)
        status_counts = {status: sum(1 for result in results if result.status == status) for status in ("PASS", "WARN", "FAIL", "INFO")}
        command_context.update_fields(
            fatal=fatal,
            check_count=len(results),
            pass_count=status_counts["PASS"],
            warn_count=status_counts["WARN"],
            fail_count=status_counts["FAIL"],
            info_count=status_counts["INFO"],
        )

        if args.json:
            command_context.set_stage("render_json")
            print(json.dumps({
                "fatal": fatal,
                "results": [{"status": result.status, "message": result.message} for result in results],
                "summary": "doctor found one or more fatal problems." if fatal else "doctor checks passed.",
            }, indent=2, sort_keys=True))
            if fatal:
                error = build_doctor_error(results)
                if error:
                    command_context.set_error(error)
                command_context.fail()
            else:
                command_context.succeed()
            return 1 if fatal else 0

        command_context.set_stage("render_results")
        if fatal:
            print("\nSummary: doctor found one or more fatal problems.")
            print_followup_help()
            error = build_doctor_error(results)
            if error:
                command_context.set_error(error)
            command_context.fail()
            return 1

        print("\nSummary: doctor checks passed.")
        print_followup_help()
        command_context.succeed()
        return 0
    return 1
