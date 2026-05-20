from __future__ import annotations

from timecapsulesmb.app.contracts import discover_payload, install_validation_payload, paths_payload
from timecapsulesmb.app.events import EventSink
from timecapsulesmb.core.paths import resolve_app_paths
from timecapsulesmb.discovery.bonjour import (
    DEFAULT_BROWSE_TIMEOUT_SEC,
    BonjourDiscoverySnapshot,
    BonjourResolvedService,
    discover_snapshot,
    discovered_record_root_host,
    discovery_record_to_jsonable,
    service_instance_to_jsonable,
)
from timecapsulesmb.install_validation import (
    install_checks_to_jsonable,
    install_ok,
    paths_to_jsonable,
    validate_install,
)
from timecapsulesmb.services.app import (
    OperationResult,
    config_path,
    float_param,
)


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
    return {
        "instances": [service_instance_to_jsonable(instance) for instance in snapshot.instances],
        "resolved": [discovery_record_to_jsonable(record) for record in snapshot.resolved],
    }


def discover_operation(params: dict[str, object], sink: EventSink) -> OperationResult:
    operation = "discover"
    timeout = float_param(params, "timeout", DEFAULT_BROWSE_TIMEOUT_SEC)
    sink.stage(operation, "bonjour_discovery")
    snapshot = discover_snapshot(timeout=timeout)
    return OperationResult(True, discover_payload(snapshot_payload(snapshot)))


def paths_operation(params: dict[str, object], sink: EventSink) -> OperationResult:
    operation = "paths"
    sink.stage(operation, "resolve_paths")
    app_paths = resolve_app_paths(config_path=config_path(params))
    sink.stage(operation, "summarize_artifacts")
    return OperationResult(True, paths_payload(paths_to_jsonable(app_paths)))


def validate_install_operation(params: dict[str, object], sink: EventSink) -> OperationResult:
    operation = "validate-install"
    sink.stage(operation, "resolve_paths")
    app_paths = resolve_app_paths(config_path=config_path(params))
    sink.stage(operation, "validate_install")
    checks = validate_install(app_paths)
    ok = install_ok(checks)
    for check in checks:
        sink.check(
            operation,
            status="PASS" if check.ok else "FAIL",
            message=check.message,
            details=check.details,
        )
    return OperationResult(ok, install_validation_payload(ok=ok, checks=install_checks_to_jsonable(checks)))
