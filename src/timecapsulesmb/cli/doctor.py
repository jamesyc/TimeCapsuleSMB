from __future__ import annotations

import argparse
from collections.abc import Callable
from typing import Optional

from timecapsulesmb.checks.doctor import run_doctor_checks
from timecapsulesmb.checks.doctor_steps import DOCTOR_STAGE_RUN_CHECKS
from timecapsulesmb.checks.models import DOMAIN_COMPUTER, DOMAIN_DEVICE, CheckResult
from timecapsulesmb.cli.context import CommandContext
from timecapsulesmb.cli.runtime import add_config_argument, print_json
from timecapsulesmb.cli.util import color_green, color_red
from timecapsulesmb.identity import ensure_install_id
from timecapsulesmb.services.doctor import build_doctor_error, doctor_finished_fields
from timecapsulesmb.services.runtime import load_env_config
from timecapsulesmb.telemetry import TelemetryClient
from timecapsulesmb.core.paths import resolve_app_paths


SECTION_TITLES = {DOMAIN_COMPUTER: "This computer", DOMAIN_DEVICE: "Device"}


def print_result(result: CheckResult) -> None:
    status = result.status
    if status == "PASS":
        status = color_green(status)
    elif status == "FAIL":
        status = color_red(status)
    print(f"{status} {result.message}")


def section_printer() -> Callable[[CheckResult], None]:
    """Print results under a heading each time doctor moves between checking
    this computer and checking the device."""
    current: str | None = None
    seen: set[str] = set()

    def emit(result: CheckResult) -> None:
        nonlocal current
        if result.domain is not None and result.domain != current:
            if current is not None:
                print("")
            title = SECTION_TITLES.get(result.domain, result.domain)
            # This computer's network checks come after the device's (they use
            # what the device reported); a device result can come last (a
            # data disk that stopped answering).
            if result.domain in seen:
                title += " (network)" if result.domain == DOMAIN_COMPUTER else " (continued)"
            print(f"== {title} ==")
            current = result.domain
            seen.add(result.domain)
        print_result(result)

    return emit


def section_status_text(status: str) -> str:
    return status.replace("_", " ").upper()


def print_section_summary(fields: dict[str, object]) -> None:
    sections = fields["doctor_sections"]
    print("")
    print("   ".join(
        f"{SECTION_TITLES[name]}: {section_status_text(sections[name]['status'])}"
        for name in (DOMAIN_COMPUTER, DOMAIN_DEVICE)
    ) + f"   Overall: {section_status_text(fields['doctor_status'])}")


def print_followup_help() -> None:
    print("")
    print("Some troubleshooting tips:")
    print("- (To remove old Apple devices entries from mDNS cache) try running:")
    print("    sudo dscacheutil -flushcache && sudo killall -HUP mDNSResponder")
    print("- (If you have disk corruption issues, or error 22) then try running:")
    print("    .venv/bin/tcapsule fsck")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run local diagnostics for the current TimeCapsuleSMB setup.")
    add_config_argument(parser)
    parser.add_argument("--skip-ssh", action="store_true", help="Skip SSH reachability checks")
    parser.add_argument("--skip-bonjour", action="store_true", help="Skip Bonjour browse/resolve checks")
    parser.add_argument("--skip-smb", action="store_true", help="Skip the SMB login and file checks (they write a test folder through SMB)")
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
        if not args.skip_ssh and config.has_value("TC_HOST"):
            command_context.set_stage("resolve_connection")
            connection = command_context.resolve_env_connection(allow_empty_password=True)
            if connection.password:
                command_context.start_optional_airport_identity_probe(connection)
        command_context.set_stage(DOCTOR_STAGE_RUN_CHECKS)
        doctor_debug: dict[str, object] = {}

        def on_stage(stage: str, detail: str | None) -> None:
            command_context.set_stage(stage)
            if detail and not args.json:
                print(detail, flush=True)

        # The followed config: Bonjour and SMB checks look where SSH reaches the device.
        results, fatal = run_doctor_checks(
            command_context.config or config,
            repo_root=app_paths.distribution_root,
            connection=command_context.connection,
            device_reading=command_context.reading_at(command_context.connection),
            precomputed_probe_state=command_context.probe_state,
            skip_ssh=args.skip_ssh,
            skip_bonjour=args.skip_bonjour,
            skip_smb=args.skip_smb,
            on_result=None if args.json else section_printer(),
            on_stage=on_stage,
            debug_fields=doctor_debug,
        )
        finished = doctor_finished_fields(results, fatal, doctor_debug)
        command_context.add_debug_fields(**doctor_debug)
        command_context.update_fields(**finished)

        if args.json:
            command_context.set_stage("render_json")
            print_json({
                "fatal": fatal,
                "results": [
                    {"status": result.status, "message": result.message, "section": result.domain}
                    for result in results
                ],
                "status": finished["doctor_status"],
                "sections": finished["doctor_sections"],
                "summary": "doctor found one or more fatal problems." if fatal else "doctor checks passed.",
            })
            if fatal:
                error = build_doctor_error(results, command_context.debug_fields)
                if error:
                    command_context.set_error(error)
                command_context.fail()
            else:
                command_context.succeed()
            return 1 if fatal else 0

        command_context.set_stage("render_results")
        print_section_summary(finished)
        if fatal:
            print("\nSummary: doctor found one or more fatal problems.")
            print_followup_help()
            error = build_doctor_error(results, command_context.debug_fields)
            if error:
                command_context.set_error(error)
            command_context.fail()
            return 1

        print("\nSummary: doctor checks passed.")
        print_followup_help()
        command_context.succeed()
        return 0
    return 1
