from __future__ import annotations

from timecapsulesmb.app.context import AppOperationContext
from timecapsulesmb.app.contracts import discover_payload
from timecapsulesmb.discovery.bonjour import (
    DEFAULT_BROWSE_TIMEOUT_SEC,
    BonjourDiscoverySnapshot,
    discover_snapshot_merged_detailed,
    discovery_record_to_jsonable,
    service_instance_to_jsonable,
)
from timecapsulesmb.discovery.devices import device_candidate_to_jsonable, device_candidates_from_records
from timecapsulesmb.services.app import OperationResult, float_param

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
    snapshot, diagnostics = discover_snapshot_merged_detailed(timeout=timeout)
    payload = discover_payload(snapshot_payload(snapshot))
    counts = payload.get("counts")
    devices = payload.get("devices")
    context.update_fields(
        discovery_timeout_sec=timeout,
        discovery_instance_count=len(snapshot.instances),
        discovery_resolved_count=len(snapshot.resolved),
        discovery_device_count=len(devices) if isinstance(devices, list) else None,
    )
    if isinstance(counts, dict):
        context.update_fields(discovery_counts=counts)
    context.add_debug_fields(discovery_diagnostics=diagnostics)
    return OperationResult(True, payload)
