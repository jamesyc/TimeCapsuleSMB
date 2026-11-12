from __future__ import annotations

from timecapsulesmb.checks.network import saved_router_mac
from timecapsulesmb.app.context import AppOperationContext
from timecapsulesmb.core.config import AppConfig
from timecapsulesmb.core.error_codes import ErrorCode
from timecapsulesmb.services.app import AppOperationError, config_path
from timecapsulesmb.services.credentials import overlay_request_airport_mac, overlay_request_credentials
from timecapsulesmb.services.runtime import (
    DeviceAccessError,
    ManagedTargetState,
    load_env_config,
    load_optional_env_config,
    resolve_env_connection,
    require_acpd_ready,
    require_no_disk_check,
    resolve_validated_managed_target,
)
from timecapsulesmb.transport.ssh import SshConnection


LOCAL_NETWORK_PREFLIGHT_PARAM_KEYS = (
    "macos_local_network_preflight_result",
    "macos_local_network_preflight_duration_ms",
    "macos_local_network_preflight_service",
    "macos_local_network_preflight_error",
)


def refuse_if_local_network_denied(params: dict[str, object], context: AppOperationContext) -> None:
    """Records the app's Local Network preflight, and stops when macOS denied it."""
    fields = {
        key: params[key]
        for key in LOCAL_NETWORK_PREFLIGHT_PARAM_KEYS
        if key in params and params[key] is not None
    }
    if fields:
        context.add_debug_fields(**fields)
    if str(params.get("macos_local_network_preflight_result") or "").strip().lower() == "denied":
        context.stage("local_network_preflight")
        raise AppOperationError(
            "macOS is blocking TimeCapsuleSMB from accessing devices on your local network.",
            code=ErrorCode.LOCAL_NETWORK_PERMISSION_DENIED,
        )


def load_request_config(params: dict[str, object], context: AppOperationContext) -> AppConfig:
    # Finding the saved device runs inside load_config although it reads the
    # network: a stage of its own would need timeline strings in every locale.
    # It is cancellable like load_config, and slow only when the saved address
    # is out of date.
    context.stage("load_config")
    try:
        config = overlay_request_airport_mac(
            overlay_request_credentials(load_env_config(env_path=config_path(params)), params), params,
        )
        context.config = config
        context.diagnostics.follow_saved_device(context.to_operation_callbacks())
    except ValueError as exc:
        raise AppOperationError(str(exc), code=ErrorCode.VALIDATION_FAILED) from exc
    except DeviceAccessError as exc:
        raise AppOperationError(str(exc), code=exc.code) from exc
    # follow_saved_device only swaps the config for one at the device's new address.
    return context.config or config


def load_optional_request_config(params: dict[str, object], context: AppOperationContext) -> AppConfig:
    context.stage("load_config")
    config = overlay_request_credentials(load_optional_env_config(env_path=config_path(params)), params)
    context.config = config
    return config


def resolve_request_connection(
    config: AppConfig,
    context: AppOperationContext,
    *,
    allow_empty_password: bool = True,
) -> SshConnection:
    context.stage("resolve_connection")
    connection = resolve_env_connection(config, allow_empty_password=allow_empty_password)
    context.connection = connection
    return connection


def resolve_request_target(
    config: AppConfig,
    context: AppOperationContext,
    *,
    profile: str,
    include_probe: bool,
) -> ManagedTargetState:
    context.stage("resolve_managed_target")
    target = resolve_validated_managed_target(
        config,
        command_name=context.operation,
        profile=profile,
        include_probe=include_probe,
    )
    context.apply_managed_target(target)
    return target


def require_request_acpd_ready(
    context: AppOperationContext, connection: SshConnection, *, restarts: bool = True,
) -> None:
    """services.runtime.require_acpd_ready, raising its code as an AppOperationError."""
    try:
        require_acpd_ready(
            connection, context.to_operation_callbacks(), reading=context.reading_at(connection), restarts=restarts,
            router_mac=saved_router_mac(context.config),
        )
    except DeviceAccessError as exc:
        raise AppOperationError(str(exc), code=exc.code) from exc


def require_request_no_disk_check(context: AppOperationContext, connection: SshConnection) -> None:
    """services.runtime.require_no_disk_check, for work that writes the disk
    without a restart, raising its code as an AppOperationError."""
    try:
        require_no_disk_check(connection, context.to_operation_callbacks())
    except DeviceAccessError as exc:
        raise AppOperationError(str(exc), code=exc.code) from exc
