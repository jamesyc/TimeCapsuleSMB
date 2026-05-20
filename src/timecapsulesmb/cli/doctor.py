from __future__ import annotations

import argparse
import re
from collections.abc import Mapping
from typing import Optional

from timecapsulesmb.checks.doctor import run_doctor_checks
from timecapsulesmb.checks.models import CheckResult
from timecapsulesmb.cli.context import CommandContext
from timecapsulesmb.cli.runtime import add_bonjour_timeout_argument, add_config_argument, load_env_config, print_json
from timecapsulesmb.cli.util import color_green, color_red
from timecapsulesmb.identity import ensure_install_id
from timecapsulesmb.services.doctor import doctor_status_counts
from timecapsulesmb.telemetry import TelemetryClient
from timecapsulesmb.core.paths import resolve_app_paths


BONJOUR_INSTANCE_FAILURE_PREFIX = "no discovered _smb._tcp instance matched"


def print_result(result: CheckResult) -> None:
    status = result.status
    if status == "PASS":
        status = color_green(status)
    elif status == "FAIL":
        status = color_red(status)
    print(f"{status} {result.message}")


def _mapping_value(value: object, key: str) -> object | None:
    if isinstance(value, Mapping):
        return value.get(key)
    return None


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _as_sequence(value: object) -> list[object]:
    if isinstance(value, list):
        return list(value)
    if isinstance(value, tuple):
        return list(value)
    return []


def _expected_bonjour_instance_from_results(results: list[CheckResult]) -> str | None:
    for result in results:
        if result.status != "FAIL" or BONJOUR_INSTANCE_FAILURE_PREFIX not in result.message:
            continue
        match = re.search(
            r"expected (?:device |configured )?instance (?P<quote>['\"])(?P<name>.*?)(?P=quote)",
            result.message,
        )
        if match:
            return match.group("name")
    return None


def _debug_bonjour_expected_instance(debug_fields: Mapping[str, object]) -> str | None:
    expected = _mapping_value(debug_fields, "bonjour_expected")
    value = _mapping_value(expected, "instance_name")
    return value if isinstance(value, str) and value else None


def _bonjour_failure_uses_instance_match(results: list[CheckResult]) -> bool:
    return any(result.status == "FAIL" and BONJOUR_INSTANCE_FAILURE_PREFIX in result.message for result in results)


def _native_dns_sd_smb_names(debug_fields: Mapping[str, object]) -> list[str]:
    native_dns_sd = _mapping_value(debug_fields, "bonjour_native_dns_sd")
    names: list[str] = []
    for browse in _as_sequence(_mapping_value(native_dns_sd, "browses")):
        browse_type = str(_mapping_value(browse, "service_type") or "")
        for event in _as_sequence(_mapping_value(browse, "events")):
            event_type = str(_mapping_value(event, "service_type") or browse_type)
            if not event_type.rstrip(".").startswith("_smb._tcp"):
                continue
            if str(_mapping_value(event, "action") or "").lower() != "add":
                continue
            name = _mapping_value(event, "name")
            if isinstance(name, str) and name and name not in names:
                names.append(name)
    return names


def build_discovery_context(results: list[CheckResult], debug_fields: Mapping[str, object]) -> list[str]:
    if not _bonjour_failure_uses_instance_match(results):
        return []

    zeroconf = _mapping_value(debug_fields, "bonjour_zeroconf")
    zeroconf_instance_count = _as_int(_mapping_value(zeroconf, "instance_count"))
    if zeroconf_instance_count != 0:
        return []

    native_smb_names = _native_dns_sd_smb_names(debug_fields)
    expected_instance = _debug_bonjour_expected_instance(debug_fields) or _expected_bonjour_instance_from_results(results)
    native_saw_expected = expected_instance is not None and expected_instance in native_smb_names
    if not native_saw_expected:
        return []

    return [
        "INFO Python zeroconf discovered 0 Bonjour instances during doctor",
        f"INFO native dns-sd discovered expected _smb._tcp instance {expected_instance!r}",
        (
            "INFO likely doctor false negative: native macOS mDNS saw the expected service "
            "but Python zeroconf did not receive browse events"
        ),
    ]


def _last_regex_group(pattern: str, text: str) -> str | None:
    matches = list(re.finditer(pattern, text))
    if not matches:
        return None
    match = matches[-1]
    return match.group(1) if match.groups() else match.group(0)


def _extract_generated_service_types(mdns_log: str) -> list[str]:
    service_types: list[str] = []
    for match in re.finditer(r"serving service: type=([^ ]+)", mdns_log):
        service_type = match.group(1)
        if service_type not in service_types:
            service_types.append(service_type)
    return service_types


def build_mdns_boot_context(debug_fields: Mapping[str, object]) -> list[str]:
    rc_log = _mapping_value(debug_fields, "remote_rc_local_log_tail")
    mdns_log = _mapping_value(debug_fields, "remote_mdns_log_tail")
    rc_text = rc_log if isinstance(rc_log, str) else ""
    mdns_text = mdns_log if isinstance(mdns_log, str) else ""
    combined = f"{rc_text}\n{mdns_text}"
    if not combined.strip():
        return []

    lines: list[str] = []
    capture_failed = any(
        marker in combined
        for marker in (
            "mDNS snapshot capture exited with failure",
            "mDNS snapshot capture ended without status",
            "mDNS snapshot capture timed out",
            "mDNS snapshot capture did not produce trusted Apple snapshot",
            "warning: could not identify local Apple mDNS records",
        )
    )
    fallback_generated = (
        "generating AirPort fallback" in combined
        or "airport snapshot: wrote" in combined
        or "mDNS AirPort snapshot generated" in combined
    )
    generated_fallback = "mdns advertiser will fall back to generated records" in combined

    if capture_failed and fallback_generated:
        lines.append("INFO trusted Apple mDNS snapshot capture failed; AirPort fallback snapshot was generated")
    elif capture_failed and generated_fallback:
        lines.append(
            "INFO trusted Apple mDNS snapshot capture failed; mdns-advertiser fell back to generated records"
        )
    elif capture_failed:
        lines.append("INFO trusted Apple mDNS snapshot capture failed")

    snapshot_load = _last_regex_group(r"snapshot load: loaded ([^\n]+)", mdns_text)
    if snapshot_load:
        lines.append(f"INFO mDNS snapshot load: loaded {snapshot_load}")

    source = _last_regex_group(r"serving summary: source=([^\s]+)", mdns_text)
    service_types = _extract_generated_service_types(mdns_text)
    if source and service_types:
        lines.append(
            f"INFO mdns-advertiser source={source}; generated services include {', '.join(service_types)}"
        )
    elif source:
        lines.append(f"INFO mdns-advertiser source={source}")

    takeover = _last_regex_group(r"mDNS takeover established after ([^\n]+)", mdns_text)
    if takeover:
        lines.append(f"INFO mDNS takeover established after {takeover}")

    return lines


def build_doctor_error(results: list[CheckResult], debug_fields: Mapping[str, object] | None = None) -> str | None:
    debug_fields = debug_fields or {}
    fail_lines = [f"{result.status} {result.message}" for result in results if result.status == "FAIL"]
    warn_lines = [f"{result.status} {result.message}" for result in results if result.status == "WARN"]
    info_lines = [
        f"{result.status} {result.message}"
        for result in results
        if result.status == "INFO" and result.message.startswith("discovered _smb._tcp candidates:")
    ]
    discovery_lines = build_discovery_context(results, debug_fields)
    mdns_boot_lines = build_mdns_boot_context(debug_fields)
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
    if discovery_lines:
        if lines:
            lines.append("")
        lines.append("Discovery context:")
        lines.extend(discovery_lines)
    if mdns_boot_lines:
        if lines:
            lines.append("")
        lines.append("mDNS boot context:")
        lines.extend(mdns_boot_lines)
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
    add_bonjour_timeout_argument(parser)
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
            bonjour_timeout=args.bonjour_timeout,
            json_output=args.json,
        )
        if not args.skip_ssh and config.has_value("TC_HOST"):
            command_context.set_stage("resolve_connection")
            connection = command_context.resolve_env_connection(allow_empty_password=True)
            if connection.password:
                command_context.start_optional_airport_identity_probe(connection)
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
            bonjour_timeout=args.bonjour_timeout,
            on_result=None if args.json else print_result,
            debug_fields=doctor_debug,
        )
        command_context.add_debug_fields(**doctor_debug)
        status_counts = doctor_status_counts(results)
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
            print_json({
                "fatal": fatal,
                "results": [{"status": result.status, "message": result.message} for result in results],
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
