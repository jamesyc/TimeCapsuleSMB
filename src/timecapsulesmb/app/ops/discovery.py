from __future__ import annotations

from timecapsulesmb.app.context import AppOperationContext
from timecapsulesmb.app.contracts import discover_payload
from timecapsulesmb.discovery.bonjour import (
    DEFAULT_BROWSE_TIMEOUT_SEC,
    BonjourDiscoverySnapshot,
    BonjourResolvedService,
    discover_snapshot_merged_detailed,
    discovered_record_root_host,
    discovery_record_to_jsonable,
    service_instance_to_jsonable,
)
from timecapsulesmb.discovery.devices import device_candidate_to_jsonable, device_candidates_from_records
from timecapsulesmb.services.app import OperationResult, float_param


def selected_record_properties(params: dict[str, object]) -> dict[str, str]:
    selected = params.get("selected_record")
    if not isinstance(selected, dict):
        return {}
    properties = selected.get("properties")
    if not isinstance(properties, dict):
        return {}
    return {str(key): str(value) for key, value in properties.items()}


def selected_record_host(params: dict[str, object]) -> str:
    selected = params.get("selected_record")
    if not isinstance(selected, dict):
        return ""
    record = BonjourResolvedService(
        name=str(selected.get("name") or ""),
        hostname=str(selected.get("hostname") or ""),
        service_type=str(selected.get("service_type") or ""),
        port=int(selected.get("port") or 0),
        ipv4=tuple(str(ip) for ip in selected.get("ipv4", ()) if ip),
        ipv6=tuple(str(ip) for ip in selected.get("ipv6", ()) if ip),
        properties=selected_record_properties(params),
        fullname=str(selected.get("fullname") or ""),
    )
    return discovered_record_root_host(record) or ""


def snapshot_payload(snapshot: BonjourDiscoverySnapshot) -> dict[str, object]:
    devices = device_candidates_from_records(snapshot.resolved)
    return {
        "instances": [service_instance_to_jsonable(instance) for instance in snapshot.instances],
        "resolved": [discovery_record_to_jsonable(record) for record in snapshot.resolved],
        "devices": [device_candidate_to_jsonable(device) for device in devices],
    }


def discover_operation(params: dict[str, object], context: AppOperationContext) -> OperationResult:
    timeout = float_param(params, "timeout", DEFAULT_BROWSE_TIMEOUT_SEC)
    context.stage("bonjour_discovery")
    snapshot, _diagnostics = discover_snapshot_merged_detailed(timeout=timeout)
    return OperationResult(True, discover_payload(snapshot_payload(snapshot)))
