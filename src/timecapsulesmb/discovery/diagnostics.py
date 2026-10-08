from __future__ import annotations

from collections.abc import Sequence
from functools import singledispatch

from timecapsulesmb.discovery.bonjour import (
    BonjourQueryDiagnostics,
    BonjourFamilyDiscoveryAttempt,
    BonjourDiscoveryDiagnostics,
    BonjourDiscoverySnapshot,
    BonjourPtrRecordObservation,
    BonjourResolvedService,
    BonjourServiceEvent,
    BonjourServiceInstance,
)
from timecapsulesmb.discovery.native_dns_sd import (
    NativeDnsSdAddressResult,
    NativeDnsSdBrowseResult,
    NativeDnsSdDiscoveryDiagnostics,
    NativeDnsSdResolveResult,
    NativeDnsSdServiceEvent,
)


MAX_BONJOUR_DEBUG_ITEMS = 50
MAX_DEBUG_TEXT = 200
MAX_DEBUG_ERROR_TEXT = 1024


@singledispatch
def debug_summary(value: object) -> object:
    return value


def _truncate_debug_text(value: object, limit: int = MAX_DEBUG_TEXT) -> str:
    text = str(value)
    if len(text) <= limit:
        return text
    return f"{text[:limit]}..."


def _debug_limited(values: Sequence[object], limit: int = MAX_BONJOUR_DEBUG_ITEMS) -> list[object]:
    return list(values[:limit])


def _bonjour_instance_summary(value: BonjourServiceInstance) -> dict[str, object]:
    summary: dict[str, object] = {
        "service_type": _truncate_debug_text(value.service_type),
        "name": _truncate_debug_text(value.name),
        "fullname": _truncate_debug_text(value.fullname),
    }
    if value.interface_index is not None:
        summary["interface_index"] = value.interface_index
    return summary


def _bonjour_record_summary(value: BonjourResolvedService) -> dict[str, object]:
    summary: dict[str, object] = {
        "service_type": _truncate_debug_text(value.service_type),
        "name": _truncate_debug_text(value.name),
        "hostname": _truncate_debug_text(value.hostname),
        "port": value.port,
        "ipv4": list(value.ipv4),
    }
    if value.ipv6:
        summary["ipv6"] = list(value.ipv6)
    if value.fullname:
        summary["fullname"] = _truncate_debug_text(value.fullname)
    if value.interface_index is not None:
        summary["interface_index"] = value.interface_index
    for key in ("syAP", "model"):
        prop = value.properties.get(key)
        if prop:
            summary[key] = _truncate_debug_text(prop)
    return summary


@debug_summary.register
def _(value: BonjourResolvedService) -> dict[str, object]:
    return _bonjour_record_summary(value)


def _bonjour_service_event_summary(value: BonjourServiceEvent) -> dict[str, object]:
    return {
        "service_type": _truncate_debug_text(value.service_type),
        "state": _truncate_debug_text(value.state),
        "name": _truncate_debug_text(value.name),
        "fullname": _truncate_debug_text(value.fullname),
        "elapsed_sec": value.elapsed_sec,
    }


def _bonjour_ptr_record_summary(value: BonjourPtrRecordObservation) -> dict[str, object]:
    return {
        "service_type": _truncate_debug_text(value.service_type),
        "alias": _truncate_debug_text(value.alias),
        "alias_name": _truncate_debug_text(value.alias_name),
        "ttl": value.ttl,
        "expired": value.expired,
        "old_record_present": value.old_record_present,
        "elapsed_sec": value.elapsed_sec,
    }


@debug_summary.register
def _(value: BonjourServiceInstance) -> dict[str, object]:
    return _bonjour_instance_summary(value)


@debug_summary.register
def _(value: BonjourServiceEvent) -> dict[str, object]:
    return _bonjour_service_event_summary(value)


@debug_summary.register
def _(value: BonjourPtrRecordObservation) -> dict[str, object]:
    return _bonjour_ptr_record_summary(value)


@debug_summary.register
def _(value: BonjourDiscoverySnapshot) -> dict[str, object]:
    return {
        "instance_count": len(value.instances),
        "resolved_count": len(value.resolved),
        "instances": [_bonjour_instance_summary(instance) for instance in _debug_limited(value.instances)],
        "resolved": [_bonjour_record_summary(record) for record in _debug_limited(value.resolved)],
    }


@debug_summary.register
def _(value: BonjourDiscoveryDiagnostics) -> dict[str, object]:
    summary: dict[str, object] = {
        "service": value.service,
        "service_types": list(value.service_types),
        "timeout_sec": value.timeout_sec,
        "elapsed_sec": value.elapsed_sec,
        "ip_version": value.ip_version,
        "zeroconf_version": value.zeroconf_version,
        "zeroconf_interfaces": value.zeroconf_interfaces,
        "instance_count": value.instance_count,
        "resolved_count": value.resolved_count,
        "pending_count": value.pending_count,
        "service_added_count": value.service_added_count,
        "service_updated_count": value.service_updated_count,
        "resolve_attempt_count": value.resolve_attempt_count,
        "resolve_success_count": value.resolve_success_count,
        "resolve_error_count": value.resolve_error_count,
        "service_event_count": len(value.service_events),
        "ptr_record_count": len(value.ptr_records),
        "instances": [_bonjour_instance_summary(instance) for instance in _debug_limited(value.instances)],
        "resolved": [_bonjour_record_summary(record) for record in _debug_limited(value.resolved)],
        "service_events": [_bonjour_service_event_summary(event) for event in _debug_limited(value.service_events)],
        "ptr_records": [_bonjour_ptr_record_summary(record) for record in _debug_limited(value.ptr_records)],
    }
    if value.ptr_record_error:
        summary["ptr_record_error"] = _truncate_debug_text(value.ptr_record_error, MAX_DEBUG_ERROR_TEXT)
    return summary


@debug_summary.register
def _(value: BonjourFamilyDiscoveryAttempt) -> dict[str, object]:
    summary: dict[str, object] = {
        "family": value.family,
        "status": "error" if value.error else "ok",
    }
    if value.error:
        summary["error"] = _truncate_debug_text(value.error, MAX_DEBUG_ERROR_TEXT)
    if value.diagnostics is not None:
        summary["diagnostics"] = debug_summary(value.diagnostics)
    elif value.snapshot is not None:
        summary["snapshot"] = debug_summary(value.snapshot)
    return summary


@debug_summary.register
def _(value: BonjourQueryDiagnostics) -> dict[str, object]:
    details = debug_summary(value.details)
    if isinstance(details, dict):
        # Attempts are also used by ACP diagnostics in memory; emit them only once.
        details = {key: item for key, item in details.items() if key not in {"attempts", "service_types", "timeout_sec", "elapsed_sec", "instance_count", "resolved_count", "pending_count"}}
    return {
        "provider": value.provider, "service_types": list(value.service_types),
        "timeout_sec": value.timeout_sec, "elapsed_sec": value.elapsed_sec,
        "instance_count": value.instance_count, "resolved_count": value.resolved_count,
        "pending_count": value.pending_count,
        "errors": dict(value.errors),
        "attempts": [debug_summary(a) for a in value.attempts],
        "details": details,
    }


def _command_fields(value: NativeDnsSdBrowseResult | NativeDnsSdAddressResult | NativeDnsSdResolveResult) -> dict[str, object]:
    fields: dict[str, object] = {"exit_code": value.exit_code, "terminated_after_timeout": value.terminated_after_timeout}
    for key in ("stderr", "error"):
        text = getattr(value, key)
        if text:
            fields[key] = _truncate_debug_text(text, MAX_DEBUG_ERROR_TEXT)
    return fields


def _native_dns_sd_event_summary(value: NativeDnsSdServiceEvent) -> dict[str, object]:
    return {
        "service_type": _truncate_debug_text(value.service_type),
        "action": _truncate_debug_text(value.action),
        "interface_index": value.interface_index,
        "flags": _truncate_debug_text(value.flags),
        "domain": _truncate_debug_text(value.domain),
        "name": _truncate_debug_text(value.name),
    }


@debug_summary.register
def _(value: NativeDnsSdServiceEvent) -> dict[str, object]:
    return _native_dns_sd_event_summary(value)


@debug_summary.register
def _(value: NativeDnsSdBrowseResult) -> dict[str, object]:
    summary: dict[str, object] = {
        "service_type": value.service_type,
        "event_count": len(value.events),
        "parse_error_count": len(value.unparsed_lines),
        "unparsed_lines": [_truncate_debug_text(line) for line in _debug_limited(value.unparsed_lines, 5)],
        **_command_fields(value),
        "events": [_native_dns_sd_event_summary(event) for event in _debug_limited(value.events)],
    }
    return summary


@debug_summary.register
def _(value: NativeDnsSdAddressResult) -> dict[str, object]:
    summary: dict[str, object] = {
        "hostname": _truncate_debug_text(value.hostname),
        "family": value.family,
        "addresses": list(value.addresses),
        **_command_fields(value),
    }
    return summary


@debug_summary.register
def _(value: NativeDnsSdResolveResult) -> dict[str, object]:
    summary: dict[str, object] = {
        "service_type": value.service_type,
        "name": _truncate_debug_text(value.name),
        "fullname": _truncate_debug_text(value.fullname),
        "hostname": _truncate_debug_text(value.hostname),
        "port": value.port,
        "interface_index": value.interface_index,
        **_command_fields(value),
        "addresses": [debug_summary(address) for address in _debug_limited(value.addresses)],
    }
    return summary


@debug_summary.register
def _(value: NativeDnsSdDiscoveryDiagnostics) -> dict[str, object]:
    summary: dict[str, object] = {
        "status": value.status,
        "timeout_sec": value.timeout_sec,
        "elapsed_sec": value.elapsed_sec,
        "service_types": list(value.service_types),
        "ip_version": value.ip_version,
        "instance_count": value.instance_count,
        "resolved_count": value.resolved_count,
        "browses": [debug_summary(browse) for browse in value.browses],
        "resolves": [debug_summary(resolve) for resolve in _debug_limited(value.resolves)],
    }
    if value.error:
        summary["error"] = _truncate_debug_text(value.error, MAX_DEBUG_ERROR_TEXT)
    return summary

