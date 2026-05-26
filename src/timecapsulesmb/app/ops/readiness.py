from __future__ import annotations

import hashlib
from urllib.parse import urlparse

from timecapsulesmb.app.contracts import (
    capabilities_payload,
    discover_payload,
    install_validation_payload,
    paths_payload,
    telemetry_identity_payload,
    version_check_payload,
)
from timecapsulesmb.app.events import EventSink
from timecapsulesmb.cli.version_check import VERSION_CHECK_URL, check_client_version
from timecapsulesmb.core.paths import artifact_manifest_resource, resolve_app_paths
from timecapsulesmb.core.release import CLI_VERSION, CLI_VERSION_CODE
from timecapsulesmb.discovery.bonjour import (
    DEFAULT_BROWSE_TIMEOUT_SEC,
    BonjourDiscoverySnapshot,
    BonjourResolvedService,
    discover_snapshot,
    discovered_record_root_host,
    discovery_record_to_jsonable,
    service_instance_to_jsonable,
)
from timecapsulesmb.discovery.devices import device_candidate_to_jsonable, device_candidates_from_records
from timecapsulesmb.install_validation import (
    install_checks_to_jsonable,
    install_ok,
    paths_to_jsonable,
    validate_install,
)
from timecapsulesmb.identity import load_install_identity, set_telemetry_enabled
from timecapsulesmb.services.app import (
    AppOperationError,
    OperationResult,
    bool_param,
    config_path,
    float_param,
    string_param,
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
    devices = device_candidates_from_records(snapshot.resolved)
    return {
        "instances": [service_instance_to_jsonable(instance) for instance in snapshot.instances],
        "resolved": [discovery_record_to_jsonable(record) for record in snapshot.resolved],
        "devices": [device_candidate_to_jsonable(device) for device in devices],
    }


def discover_operation(params: dict[str, object], sink: EventSink) -> OperationResult:
    operation = "discover"
    timeout = float_param(params, "timeout", DEFAULT_BROWSE_TIMEOUT_SEC)
    sink.stage(operation, "bonjour_discovery")
    snapshot = discover_snapshot(timeout=timeout)
    return OperationResult(True, discover_payload(snapshot_payload(snapshot)))


def capabilities_operation(params: dict[str, object], sink: EventSink) -> OperationResult:
    operation = "capabilities"
    sink.stage(operation, "resolve_paths")
    app_paths = resolve_app_paths(config_path=config_path(params))
    sink.stage(operation, "summarize_capabilities")
    try:
        manifest_hash = hashlib.sha256(artifact_manifest_resource().read_bytes()).hexdigest()
    except OSError:
        manifest_hash = None
    return OperationResult(True, capabilities_payload(
        helper_version=CLI_VERSION,
        helper_version_code=CLI_VERSION_CODE,
        operations=[
            "activate",
            "capabilities",
            "configure",
            "deploy",
            "discover",
            "doctor",
            "flash",
            "fsck",
            "paths",
            "repair-xattrs",
            "set-telemetry",
            "telemetry-identity",
            "uninstall",
            "validate-install",
            "version-check",
        ],
        distribution_root=str(app_paths.distribution_root),
        artifact_manifest_sha256=manifest_hash,
    ))


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


def telemetry_identity_operation(params: dict[str, object], sink: EventSink) -> OperationResult:
    operation = "telemetry-identity"
    sink.stage(operation, "resolve_paths")
    app_paths = resolve_app_paths(config_path=config_path(params))
    sink.stage(operation, "read_bootstrap")
    identity = load_install_identity(app_paths.bootstrap_path)
    return OperationResult(
        True,
        telemetry_identity_payload(identity=identity, bootstrap_path=str(app_paths.bootstrap_path)),
    )


def set_telemetry_operation(params: dict[str, object], sink: EventSink) -> OperationResult:
    operation = "set-telemetry"
    if "enabled" not in params:
        raise AppOperationError("missing required parameter: enabled", code="validation_failed")
    enabled = bool_param(params, "enabled")
    sink.stage(operation, "resolve_paths")
    app_paths = resolve_app_paths(config_path=config_path(params))
    sink.stage(operation, "write_bootstrap")
    identity = set_telemetry_enabled(enabled, app_paths.bootstrap_path)
    return OperationResult(
        True,
        telemetry_identity_payload(identity=identity, bootstrap_path=str(app_paths.bootstrap_path)),
    )


def version_check_operation(params: dict[str, object], sink: EventSink) -> OperationResult:
    operation = "version-check"
    url = string_param(params, "url", VERSION_CHECK_URL).strip() or VERSION_CHECK_URL
    parsed_url = urlparse(url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        raise AppOperationError("url must be an HTTP/HTTPS URL", code="validation_failed")
    sink.stage(operation, "resolve_paths")
    app_paths = resolve_app_paths(config_path=config_path(params))
    sink.stage(operation, "check_version")
    result = check_client_version(url=url, cache_path=app_paths.version_check_cache_path)
    return OperationResult(True, version_check_payload(result))
