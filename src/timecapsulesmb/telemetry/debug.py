from __future__ import annotations

from collections.abc import Mapping, Sequence
from functools import singledispatch
from timecapsulesmb.device.compat import DeviceCompatibility
from timecapsulesmb.device.probe import ProbedDeviceState
from timecapsulesmb.discovery.diagnostics import debug_summary as bonjour_debug_summary


@singledispatch
def debug_summary(value: object) -> object:
    return bonjour_debug_summary(value)


@debug_summary.register
def _(value: ProbedDeviceState) -> dict[str, object]:
    probe = value.probe_result
    summary: dict[str, object] = {
        "probe_ssh_status": probe.ssh_status.value,
        "probe_ssh_port_reachable": probe.ssh_port_reachable,
        "probe_ssh_authenticated": probe.ssh_authenticated,
    }
    if probe.error:
        summary["probe_error"] = probe.error
    if probe.mac_network_filters:
        summary.update(probe.mac_network_filters)
    elf_endianness_detail = getattr(probe, "elf_endianness_detail", None)
    if isinstance(elf_endianness_detail, str) and elf_endianness_detail:
        summary["probe_elf_endianness"] = probe.elf_endianness
        summary["probe_elf_endianness_detail"] = elf_endianness_detail
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
