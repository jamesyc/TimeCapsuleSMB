from __future__ import annotations

from typing import TYPE_CHECKING

from timecapsulesmb.app.context import AppOperationContext
from timecapsulesmb.services.app import config_path
from timecapsulesmb.services.credentials import overlay_request_credentials
from timecapsulesmb.services.runtime import (
    ManagedTargetState,
    RuntimeOperationCallbacks,
    load_env_config,
    load_optional_env_config,
    resolve_env_connection,
    resolve_validated_managed_target,
)
from timecapsulesmb.transport.ssh import SshConnection

if TYPE_CHECKING:
    from timecapsulesmb.core.config import AppConfig


def load_request_config(params: dict[str, object], context: AppOperationContext) -> "AppConfig":
    context.stage("load_config")
    config = overlay_request_credentials(load_env_config(env_path=config_path(params)), params)
    context.config = config
    return config


def load_optional_request_config(params: dict[str, object], context: AppOperationContext) -> "AppConfig":
    context.stage("load_config")
    config = overlay_request_credentials(load_optional_env_config(env_path=config_path(params)), params)
    context.config = config
    return config


def resolve_request_connection(
    config: "AppConfig",
    context: AppOperationContext,
    *,
    allow_empty_password: bool = True,
) -> SshConnection:
    context.stage("resolve_connection")
    connection = resolve_env_connection(config, allow_empty_password=allow_empty_password)
    context.connection = connection
    return connection


def resolve_request_target(
    config: "AppConfig",
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


def runtime_callbacks(context: AppOperationContext) -> RuntimeOperationCallbacks:
    return RuntimeOperationCallbacks(
        set_stage=context.stage,
        log=context.log,
        add_debug_fields=context.add_debug_fields,
        update_fields=context.update_fields,
    )

