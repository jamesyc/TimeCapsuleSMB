from __future__ import annotations

from collections.abc import Mapping, Sequence
from functools import singledispatch

from timecapsulesmb.device.compat import DeviceCompatibility
from timecapsulesmb.device.probe import ProbedDeviceState, RemoteInterfaceCandidatesProbeResult
from timecapsulesmb.discovery.bonjour import (
    BonjourDiscoveryDiagnostics,
    BonjourDiscoverySnapshot,
    BonjourResolvedService,
    BonjourServiceInstance,
)
from timecapsulesmb.discovery.native_dns_sd import (
    NativeDnsSdBrowseResult,
    NativeDnsSdDiagnostics,
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
    return {
        "service_type": _truncate_debug_text(value.service_type),
        "name": _truncate_debug_text(value.name),
        "fullname": _truncate_debug_text(value.fullname),
    }


def _bonjour_record_summary(value: BonjourResolvedService) -> dict[str, object]:
    summary: dict[str, object] = {
        "service_type": _truncate_debug_text(value.service_type),
        "name": _truncate_debug_text(value.name),
        "hostname": _truncate_debug_text(value.hostname),
        "port": value.port,
        "ipv4": list(value.ipv4),
    }
    if value.fullname:
        summary["fullname"] = _truncate_debug_text(value.fullname)
    for key in ("syAP", "model"):
        prop = value.properties.get(key)
        if prop:
            summary[key] = _truncate_debug_text(prop)
    return summary


@debug_summary.register
def _(value: BonjourResolvedService) -> dict[str, object]:
    summary: dict[str, object] = {
        "service_type": value.service_type,
        "name": value.name,
        "hostname": value.hostname,
        "ipv4": list(value.ipv4),
    }
    for key in ("syAP", "model"):
        prop = value.properties.get(key)
        if prop:
            summary[key] = prop
    return summary


@debug_summary.register
def _(value: BonjourServiceInstance) -> dict[str, object]:
    return _bonjour_instance_summary(value)


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
    return {
        "service": value.service,
        "service_types": list(value.service_types),
        "timeout_sec": value.timeout_sec,
        "elapsed_sec": value.elapsed_sec,
        "ip_version": value.ip_version,
        "instance_count": value.instance_count,
        "resolved_count": value.resolved_count,
        "pending_count": value.pending_count,
        "service_added_count": value.service_added_count,
        "service_updated_count": value.service_updated_count,
        "resolve_attempt_count": value.resolve_attempt_count,
        "resolve_success_count": value.resolve_success_count,
        "resolve_error_count": value.resolve_error_count,
        "instances": [_bonjour_instance_summary(instance) for instance in _debug_limited(value.instances)],
        "resolved": [_bonjour_record_summary(record) for record in _debug_limited(value.resolved)],
    }


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
        "parse_error_count": value.parse_error_count,
        "exit_code": value.exit_code,
        "terminated_after_timeout": value.terminated_after_timeout,
        "events": [_native_dns_sd_event_summary(event) for event in _debug_limited(value.events)],
    }
    if value.stderr:
        summary["stderr"] = _truncate_debug_text(value.stderr, MAX_DEBUG_ERROR_TEXT)
    if value.error:
        summary["error"] = _truncate_debug_text(value.error, MAX_DEBUG_ERROR_TEXT)
    return summary


@debug_summary.register
def _(value: NativeDnsSdDiagnostics) -> dict[str, object]:
    return {
        "status": value.status,
        "timeout_sec": value.timeout_sec,
        "elapsed_sec": value.elapsed_sec,
        "browses": [debug_summary(browse) for browse in value.browses],
    }


@debug_summary.register
def _(value: RemoteInterfaceCandidatesProbeResult) -> list[dict[str, object]]:
    return [
        {
            "name": candidate.name,
            "ipv4": list(candidate.ipv4_addrs),
            "loopback": candidate.loopback,
        }
        for candidate in value.candidates
    ]


@debug_summary.register
def _(value: ProbedDeviceState) -> dict[str, object]:
    probe = value.probe_result
    summary: dict[str, object] = {
        "probe_ssh_port_reachable": probe.ssh_port_reachable,
        "probe_ssh_authenticated": probe.ssh_authenticated,
    }
    if probe.error:
        summary["probe_error"] = probe.error
    compatibility = value.compatibility
    if compatibility is not None and not compatibility.supported:
        summary["probe_supported"] = compatibility.supported
        if compatibility.reason_code:
            summary["probe_reason_code"] = compatibility.reason_code
    return summary


@debug_summary.register
def _(value: DeviceCompatibility) -> dict[str, object]:
    if value.supported:
        return {}
    summary: dict[str, object] = {"probe_supported": value.supported}
    if value.reason_code:
        summary["probe_reason_code"] = value.reason_code
    return summary


def render_debug_value(value: object) -> str:
    value = debug_summary(value)
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, Mapping):
        items = [
            f"{key}:{render_debug_value(item_value)}"
            for key, item_value in value.items()
            if item_value is not None
        ]
        return "{" + ",".join(items) + "}"
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return "[" + ",".join(render_debug_value(item) for item in value if item is not None) + "]"
    return str(value)


def render_debug_mapping(fields: Mapping[str, object], *, blacklist: set[str] | None = None) -> list[str]:
    skipped = blacklist or set()
    lines: list[str] = []
    for key in sorted(fields):
        if key in skipped:
            continue
        value = fields.get(key)
        if value is not None and value != "":
            lines.append(f"{key}={render_debug_value(value)}")
    return lines
