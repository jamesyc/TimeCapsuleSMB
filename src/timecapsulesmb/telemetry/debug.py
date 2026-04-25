from __future__ import annotations

from collections.abc import Mapping, Sequence
from functools import singledispatch

from timecapsulesmb.device.compat import DeviceCompatibility
from timecapsulesmb.device.probe import ProbedDeviceState, RemoteInterfaceCandidatesProbeResult
from timecapsulesmb.discovery.bonjour import Discovered
from timecapsulesmb.transport.ssh import SshConnection


DEBUG_VALUE_BLACKLIST = {
    "TC_PASSWORD",
    # These are already first-class telemetry fields.
    "TC_CONFIGURE_ID",
    "TC_MDNS_DEVICE_MODEL",
    "TC_AIRPORT_SYAP",
}
DEBUG_FIELD_BLACKLIST = {
    # These are already first-class telemetry fields.
    "configure_id",
    "device_model",
    "device_syap",
    "device_os_version",
    "device_family",
    "nbns_enabled",
    "reboot_was_attempted",
    "device_came_back_after_reboot",
}


@singledispatch
def debug_summary(value: object) -> object:
    return value


@debug_summary.register
def _(value: Discovered) -> dict[str, object]:
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


def _render_connection_debug_lines(connection: SshConnection | None, values: Mapping[str, str] | None) -> list[str]:
    host = None
    ssh_opts = None
    if connection is not None:
        host = connection.host
        ssh_opts = connection.ssh_opts
    elif values is not None:
        host = values.get("TC_HOST") or None
        ssh_opts = values.get("TC_SSH_OPTS") or None
    lines: list[str] = []
    if host:
        lines.append(f"host={host}")
    if ssh_opts:
        lines.append(f"ssh_opts={ssh_opts}")
    return lines


def render_command_debug_lines(
    *,
    command_name: str,
    stage: str | None,
    connection: SshConnection | None,
    values: Mapping[str, str] | None,
    preflight_error: str | None,
    finish_fields: Mapping[str, object],
    probe_state: ProbedDeviceState | None,
    debug_fields: Mapping[str, object],
) -> list[str]:
    lines = ["Debug context:", f"command={command_name}"]
    if stage:
        lines.append(f"stage={stage}")
    lines.extend(_render_connection_debug_lines(connection, values))
    if values is not None:
        lines.extend(render_debug_mapping(values, blacklist=DEBUG_VALUE_BLACKLIST))
    if preflight_error:
        lines.append(f"preflight_error={preflight_error}")
    lines.extend(render_debug_mapping(finish_fields, blacklist=DEBUG_FIELD_BLACKLIST))
    if probe_state is not None:
        lines.extend(render_debug_mapping(debug_summary(probe_state), blacklist=DEBUG_FIELD_BLACKLIST))
    lines.extend(render_debug_mapping(debug_fields, blacklist=DEBUG_FIELD_BLACKLIST))
    return lines
