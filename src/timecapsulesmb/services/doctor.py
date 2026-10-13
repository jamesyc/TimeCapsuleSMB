from __future__ import annotations

import re
from collections.abc import Mapping

from timecapsulesmb.checks.models import DEVICE_NOT_CHECKED_CODE, DOMAIN_COMPUTER, DOMAIN_DEVICE, CheckResult


BONJOUR_INSTANCE_FAILURE_PREFIX = "no discovered _smb._tcp instance matched"


def doctor_status_counts(results: list[CheckResult]) -> dict[str, int]:
    return {
        status: sum(1 for result in results if result.status == status)
        for status in ("PASS", "WARN", "FAIL", "INFO")
    }


def doctor_result_details(result: CheckResult) -> dict[str, object]:
    """A result's details as the app reads them, with the section it ran in."""
    if result.domain is None:
        return dict(result.details)
    return {**result.details, "domain": result.domain}


def doctor_report(results: list[CheckResult]) -> dict[str, object]:
    """Checkup's two sections and its overall status, as the app, the CLI and
    telemetry read them. Either section failing fails the whole Checkup, but
    each says on its own whether the device or this computer is at fault: a
    healthy device with a network, VPN or setting problem on this computer is
    common (a campus network that drops multicast, a Mac that turns off SMB 3).

    A section's status is fail or warn as its results say, pass otherwise, and
    not_checked for a device nothing ran on (SSH skipped or refused)."""
    sections: dict[str, dict[str, object]] = {}
    for section in (DOMAIN_COMPUTER, DOMAIN_DEVICE):
        section_results = [result for result in results if (result.domain or DOMAIN_DEVICE) == section]
        counts = doctor_status_counts(section_results)
        checked = section == DOMAIN_COMPUTER or any(
            result.details.get("code") != DEVICE_NOT_CHECKED_CODE for result in section_results
        )
        status = "fail" if counts["FAIL"] else "warn" if counts["WARN"] else "pass" if checked else "not_checked"
        sections[section] = {
            "status": status,
            "counts": counts,
            "fail_codes": _codes(section_results, "FAIL"),
            "warn_codes": _codes(section_results, "WARN"),
        }
    statuses = [section["status"] for section in sections.values()]
    overall = "fail" if "fail" in statuses else "warn" if "warn" in statuses else "pass"
    return {"status": overall, "sections": sections}


def doctor_finished_fields(
    results: list[CheckResult], fatal: bool, debug_fields: dict[str, object],
) -> dict[str, object]:
    """The CLI's and the app's telemetry fields for a finished doctor run.
    Debug fields reach telemetry only in a fatal run's error, so the outcomes
    every run should report ride on the event; nsmb_conf leaves debug_fields."""
    counts = doctor_status_counts(results)
    report = doctor_report(results)
    return {
        "fatal": fatal,
        "check_count": len(results),
        "pass_count": counts["PASS"],
        "warn_count": counts["WARN"],
        "fail_count": counts["FAIL"],
        "info_count": counts["INFO"],
        # An off-subnet NBNS timeout or refusal is a SKIP.
        "nbns_subnet": debug_fields.get("nbns_subnet"),
        # Likewise for Bonjour and printer checks skipped off the device's network.
        "bonjour_link": debug_fields.get("bonjour_link"),
        "nsmb_conf": debug_fields.pop("nsmb_conf", None),
        # How many devices carry a stuck fork of Apple's ACPd before any reboot.
        "acpd_child_count": debug_fields.get("acpd_child_count"),
        # Whether doctor waited for device services still starting, and how
        # that ended, on passing runs too.
        "startup_wait": debug_fields.get("startup_wait"),
        "doctor_status": report["status"],
        "doctor_sections": report["sections"],
    }


def _codes(results: list[CheckResult], status: str) -> list[str]:
    return sorted({
        code for result in results
        if result.status == status and isinstance(code := result.details.get("code"), str)
    })


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


def _bonjour_failure_uses_instance_match(results: list[CheckResult]) -> bool:
    return any(result.status == "FAIL" and BONJOUR_INSTANCE_FAILURE_PREFIX in result.message for result in results)


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


def build_discovery_context(results: list[CheckResult], debug_fields: Mapping[str, object]) -> list[str]:
    if not _bonjour_failure_uses_instance_match(results):
        return []
    lines: list[str] = []
    expected_summary, _expected_instance = _bonjour_expected_summary(results, debug_fields)
    if expected_summary:
        lines.append(f"INFO expected Bonjour identity: {expected_summary}")
    discovery = _mapping_value(debug_fields, "bonjour_discovery")
    count = _as_int(_mapping_value(discovery, "instance_count"))
    if count is not None:
        lines.append(f"INFO Bonjour discovered {count} instance(s), but no matching _smb._tcp instance")
    if _authenticated_smb_listing_passed(debug_fields):
        lines.append("INFO SMB works over unicast, but Bonjour discovered no matching _smb._tcp records")
    summary = _debug_summary_fields(discovery, ("provider", "timeout_sec", "elapsed_sec", "instance_count", "resolved_count", "pending_count"))
    if summary:
        lines.append(f"INFO Bonjour diagnostics: {summary}")
    return lines


def _authenticated_smb_listing_passed(debug_fields: Mapping[str, object]) -> bool:
    for attempt in _as_sequence(_mapping_value(debug_fields, "authenticated_smb_listing_attempts")):
        outcome = _mapping_value(attempt, "outcome")
        expected_share_found = _mapping_value(attempt, "expected_share_found")
        disk_shares = _as_sequence(_mapping_value(attempt, "disk_shares"))
        if outcome == "pass" and (expected_share_found is True or disk_shares):
            return True
    return False


def _debug_scalar_text(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int, float)):
        return str(value)
    return None


def _debug_summary_fields(value: object, keys: tuple[str, ...]) -> str:
    parts: list[str] = []
    for key in keys:
        text = _debug_scalar_text(_mapping_value(value, key))
        if text is not None:
            parts.append(f"{key}={text}")
    return " ".join(parts)


def _bonjour_expected_summary(
    results: list[CheckResult],
    debug_fields: Mapping[str, object],
) -> tuple[str, str | None]:
    expected = _mapping_value(debug_fields, "bonjour_expected")
    instance = _mapping_value(expected, "instance_name")
    if not isinstance(instance, str) or not instance:
        instance = _expected_bonjour_instance_from_results(results)
    host_label = _mapping_value(expected, "host_label")
    target_ip = _mapping_value(expected, "target_ip")
    parts: list[str] = []
    if isinstance(instance, str) and instance:
        parts.append(f"instance_name={instance!r}")
    if isinstance(host_label, str) and host_label:
        parts.append(f"host_label={host_label!r}")
    if isinstance(target_ip, str) and target_ip:
        parts.append(f"target_ip={target_ip!r}")
    return " ".join(parts), instance if isinstance(instance, str) and instance else None


def _last_regex_group(pattern: str, text: str) -> str | None:
    matches = list(re.finditer(pattern, text))
    if not matches:
        return None
    match = matches[-1]
    return match.group(1) if match.groups() else match.group(0)


def _discovery_log_text(debug_fields: Mapping[str, object]) -> str:
    # Discovery writes the RAM log while smbd is not ready and the payload log
    # afterwards. Lines start with "YYYY-MM-DD HH:MM:SS", so sorting both
    # tails restores their order and "last match" means the latest event.
    lines: list[str] = []
    for key in ("remote_discovery_log_tail", "remote_diskless_discovery_log_tail"):
        text = _mapping_value(debug_fields, key)
        if isinstance(text, str):
            lines.extend(text.splitlines())
    return "\n".join(sorted(lines))


def build_mdns_boot_context(debug_fields: Mapping[str, object]) -> list[str]:
    mdns_text = _discovery_log_text(debug_fields)
    if not mdns_text.strip():
        return []

    lines: list[str] = []
    plan = _last_regex_group(r"registrant: plan ((?:validated|incomplete) mode=[^\n]+)", mdns_text)
    if plan:
        lines.append(f"INFO mdns registrant {plan}")
    unreachable = _last_regex_group(r"registrant: mDNSResponder unreachable[^\n]*", mdns_text)
    reachable = _last_regex_group(r"registrant: mDNSResponder reachable again", mdns_text)
    if unreachable and not reachable:
        lines.append("WARN Apple mDNSResponder is unreachable; nothing respawns it, reboot the device")
    conflict = _last_regex_group(r"registrant: name conflict [^\n]+", mdns_text)
    if conflict:
        lines.append(f"WARN mdns {conflict}")
    stalled = _last_regex_group(r"registrant: (mDNSResponder accepted the connection but did not answer)[^\n]*", mdns_text)
    if stalled:
        lines.append("WARN mdns registrant exited because Apple mDNSResponder stopped answering; the manager relaunches it, a wedged daemon needs a reboot")
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
