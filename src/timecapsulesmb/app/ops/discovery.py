from __future__ import annotations

from timecapsulesmb.app.context import AppOperationContext
from timecapsulesmb.app.contracts import discover_payload
from timecapsulesmb.device.compat import unsupported_syaps
from timecapsulesmb.discovery.bonjour import (
    DEFAULT_BROWSE_TIMEOUT_SEC, DiscoveryTimeoutError, validate_discovery_timeout,
    SERVICE_TYPES,
    BonjourDiscoverySnapshot, BonjourPermissionDenied,
    discover_snapshot_detailed,
    discovery_record_to_jsonable,
    service_instance_to_jsonable,
)
from timecapsulesmb.discovery.devices import device_candidate_to_jsonable, device_candidates_from_records
from timecapsulesmb.services.app import OperationResult, AppOperationError
from timecapsulesmb.app.ops.configure import add_local_network_preflight_debug_fields, local_network_preflight_denied

def snapshot_payload(snapshot: BonjourDiscoverySnapshot) -> dict[str, object]:
    devices = device_candidates_from_records(snapshot.resolved)
    return {
        "instances": [service_instance_to_jsonable(instance) for instance in snapshot.instances],
        "resolved": [discovery_record_to_jsonable(record) for record in snapshot.resolved],
        "devices": [device_candidate_to_jsonable(device) for device in devices],
    }


def discover_operation(params: dict[str, object], context: AppOperationContext) -> OperationResult:
    try:
        timeout = validate_discovery_timeout(params.get("timeout", DEFAULT_BROWSE_TIMEOUT_SEC))
    except DiscoveryTimeoutError as exc:
        raise AppOperationError(str(exc), code=exc.code) from exc
    service = params.get("service")
    if service is not None and (not isinstance(service, str) or not any(t == service or t.startswith(service + ".") for t in SERVICE_TYPES)):
        raise AppOperationError("Unknown Bonjour service filter", code="validation_failed")
    add_local_network_preflight_debug_fields(params, context)
    if local_network_preflight_denied(params):
        context.stage("local_network_preflight")
        raise AppOperationError("macOS is blocking TimeCapsuleSMB from accessing devices on your local network.", code="local_network_permission_denied")
    context.stage("bonjour_discovery")
    try:
        snapshot, diagnostics = discover_snapshot_detailed(**({"service": service} if service is not None else {}), timeout=timeout)
    except BonjourPermissionDenied as exc:
        raise AppOperationError(str(exc), code="local_network_permission_denied") from exc
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
    if isinstance(devices, list):
        unsupported = unsupported_syaps(device.get("syap") for device in devices if isinstance(device, dict))
        if unsupported:
            context.update_fields(discovery_unsupported_syaps=unsupported)
    context.add_debug_fields(discovery_diagnostics=diagnostics)
    return OperationResult(True, payload)
