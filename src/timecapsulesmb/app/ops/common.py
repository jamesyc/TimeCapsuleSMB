from __future__ import annotations

from timecapsulesmb.app.context import AppOperationContext
from timecapsulesmb.core.config import AppConfig
from timecapsulesmb.core.net import endpoint_host
from timecapsulesmb.device.probe import AirportAcpReading
from timecapsulesmb.services.app import AppOperationError, config_path
from timecapsulesmb.services.credentials import overlay_request_credentials, request_airport_mac
from timecapsulesmb.services.runtime import (
    DeviceAccessError,
    ManagedTargetState,
    confirm_device_address,
    load_env_config,
    load_optional_env_config,
    resolve_env_connection,
    require_device_password,
    resolve_validated_managed_target,
)
from timecapsulesmb.transport.ssh import SshConnection


def load_request_config(params: dict[str, object], context: AppOperationContext) -> AppConfig:
    # Finding the saved device runs inside load_config although it reads the
    # network: a stage of its own would need timeline strings in every locale.
    # It is cancellable like load_config, and slow only when the saved address
    # is out of date.
    context.stage("load_config")
    config = _follow_saved_device(overlay_request_credentials(load_env_config(env_path=config_path(params)), params), params, context)
    context.config = config
    return config


def _follow_saved_device(config: AppConfig, params: dict[str, object], context: AppOperationContext) -> AppConfig:
    """The config with TC_HOST where the saved device answers now.

    Only the operation's copy changes: the app saves a new address itself,
    after the user confirms it (its stale-address banner).
    """
    try:
        airport_mac = request_airport_mac(params)
    except ValueError as exc:
        raise AppOperationError(str(exc), code="validation_failed") from exc
    host = config.get("TC_HOST")
    password = config.get("TC_PASSWORD")
    if not airport_mac or not host or not password:
        return config
    try:
        current, reading = confirm_device_address(host, password, airport_mac, context.to_operation_callbacks())
    except DeviceAccessError as exc:
        raise AppOperationError(str(exc), code=exc.code) from exc
    context.device_reading = (current, reading)
    if current == host:
        return config
    return AppConfig.from_values(
        {**config.values, "TC_HOST": current},
        path=config.path,
        exists=config.exists,
        file_values=config.file_values,
    )


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


def request_device_reading(context: AppOperationContext, connection: SshConnection) -> AirportAcpReading | None:
    """The read load_request_config took of the device at this connection's address."""
    if context.device_reading is None:
        return None
    host, reading = context.device_reading
    return reading if endpoint_host(host) == endpoint_host(connection.host) else None


def require_request_device_password(context: AppOperationContext, connection: SshConnection) -> None:
    """Refuse a password the device's ACP would reject, as `auth_failed`."""
    try:
        require_device_password(connection, context.to_operation_callbacks(), reading=request_device_reading(context, connection))
    except DeviceAccessError as exc:
        raise AppOperationError(str(exc), code=exc.code) from exc
