from __future__ import annotations

import hashlib
from urllib.parse import urlparse

from timecapsulesmb.app.context import AppOperationContext
from timecapsulesmb.app.contracts import (
    capabilities_payload,
    install_validation_payload,
    telemetry_preference_payload,
    update_check_payload,
    version_check_payload,
)
from timecapsulesmb.core.paths import artifact_manifest_resource, resolve_app_paths
from timecapsulesmb.core.release import CLI_VERSION, CLI_VERSION_CODE
from timecapsulesmb.install_validation import (
    install_checks_to_jsonable,
    install_ok,
    validate_install,
)
from timecapsulesmb.identity import set_telemetry_enabled
from timecapsulesmb.services.app import (
    AppOperationError,
    OperationResult,
    bool_param,
    config_path,
    string_param,
)
from timecapsulesmb.services.release_info import (
    load_release_info,
    release_api_url,
    release_info_to_jsonable,
)
from timecapsulesmb.services.version_check import VERSION_CHECK_URL, check_client_version


def capabilities_operation(params: dict[str, object], context: AppOperationContext) -> OperationResult:
    context.stage("resolve_paths")
    app_paths = resolve_app_paths(config_path=config_path(params))
    context.stage("summarize_capabilities")
    try:
        manifest_hash = hashlib.sha256(artifact_manifest_resource().read_bytes()).hexdigest()
    except OSError:
        manifest_hash = None
    return OperationResult(True, capabilities_payload(
        helper_version=CLI_VERSION,
        helper_version_code=CLI_VERSION_CODE,
        operations=_public_operation_names(),
        distribution_root=str(app_paths.distribution_root),
        artifact_manifest_sha256=manifest_hash,
    ))


def validate_install_operation(params: dict[str, object], context: AppOperationContext) -> OperationResult:
    context.stage("resolve_paths")
    app_paths = resolve_app_paths(config_path=config_path(params))
    context.stage("validate_install")
    checks = validate_install(app_paths)
    ok = install_ok(checks)
    for check in checks:
        context.check(
            status="PASS" if check.ok else "FAIL",
            message=check.message,
            details=check.details,
        )
    return OperationResult(ok, install_validation_payload(ok=ok, checks=install_checks_to_jsonable(checks)))


def set_telemetry_operation(params: dict[str, object], context: AppOperationContext) -> OperationResult:
    if "enabled" not in params:
        raise AppOperationError("missing required parameter: enabled", code="validation_failed")
    enabled = bool_param(params, "enabled")
    context.stage("resolve_paths")
    app_paths = resolve_app_paths(config_path=config_path(params))
    context.stage("write_bootstrap")
    identity = set_telemetry_enabled(enabled, app_paths.bootstrap_path)
    return OperationResult(
        True,
        telemetry_preference_payload(
            install_id=identity.install_id,
            telemetry_enabled=identity.telemetry_enabled,
            bootstrap_path=str(app_paths.bootstrap_path),
        ),
    )


def _validated_http_url(params: dict[str, object], name: str, default: str) -> str:
    url = string_param(params, name, default).strip() or default
    parsed_url = urlparse(url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        raise AppOperationError(f"{name} must be an HTTP/HTTPS URL", code="validation_failed")
    return url


def version_check_operation(params: dict[str, object], context: AppOperationContext) -> OperationResult:
    url = _validated_http_url(params, "url", VERSION_CHECK_URL)
    context.stage("resolve_paths")
    app_paths = resolve_app_paths(config_path=config_path(params))
    context.stage("check_version")
    result = check_client_version(url=url, cache_path=app_paths.version_check_cache_path)
    return OperationResult(True, version_check_payload(result))


def update_check_operation(params: dict[str, object], context: AppOperationContext) -> OperationResult:
    """Version check plus GitHub release metadata (notes and app asset) for the app updater."""
    url = _validated_http_url(params, "url", VERSION_CHECK_URL)
    release_url = _validated_http_url(params, "release_url", release_api_url())
    context.stage("resolve_paths")
    app_paths = resolve_app_paths(config_path=config_path(params))
    context.stage("check_version")
    result = check_client_version(url=url, cache_path=app_paths.version_check_cache_path)
    context.stage("fetch_release")
    release = load_release_info(url=release_url, cache_path=app_paths.release_info_cache_path)
    release_payload = release_info_to_jsonable(release) if release is not None else None
    return OperationResult(True, update_check_payload(result, release_payload))


def _public_operation_names() -> list[str]:
    from timecapsulesmb.app.ops import public_operation_names

    return public_operation_names()
