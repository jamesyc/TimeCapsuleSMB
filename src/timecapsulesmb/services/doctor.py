from __future__ import annotations

import re
from collections.abc import Mapping

from timecapsulesmb.checks.models import CheckResult


BONJOUR_INSTANCE_FAILURE_PREFIX = "no discovered _smb._tcp instance matched"


def doctor_status_counts(results: list[CheckResult]) -> dict[str, int]:
    return {
        status: sum(1 for result in results if result.status == status)
        for status in ("PASS", "WARN", "FAIL", "INFO")
    }


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
