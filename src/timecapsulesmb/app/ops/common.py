from __future__ import annotations

from typing import TYPE_CHECKING

from timecapsulesmb.app.context import AppOperationContext
from timecapsulesmb.services.app import AppOperationError, config_path
from timecapsulesmb.services.credentials import overlay_request_credentials
from timecapsulesmb.services.runtime import (
    ManagedTargetState,
    RuntimeOperationCallbacks,
    load_env_config,
    load_optional_env_config,
    resolve_env_connection,
    resolve_validated_managed_target,
)
from timecapsulesmb.services import runtime as runtime_service
from timecapsulesmb.transport.ssh import SshCommandTimeout, SshConnection, SshError
from timecapsulesmb.deploy.executor import remote_request_reboot
from timecapsulesmb.device.probe import wait_for_ssh_state_conn
from timecapsulesmb.integrations.acp import reboot as acp_reboot

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


def request_reboot(
    context: AppOperationContext,
    connection: SshConnection,
    *,
    strategy: str,
    raise_on_request_error: bool = False,
) -> None:
    try:
        runtime_service.request_runtime_reboot(
            connection,
            strategy=strategy,
            callbacks=runtime_callbacks(context),
            raise_on_request_error=raise_on_request_error,
            request_reboot=remote_request_reboot,
            request_acp_reboot=acp_reboot,
        )
    except SshCommandTimeout as exc:
        raise AppOperationError(f"SSH reboot request timed out: {exc}", code="remote_error") from exc
    except SshError as exc:
        raise AppOperationError(f"SSH reboot request failed: {exc}", code="remote_error") from exc


def request_reboot_and_wait(
    context: AppOperationContext,
    connection: SshConnection,
    *,
    strategy: str,
    reboot_no_down_message: str,
    reboot_up_timeout_message: str = "Timed out waiting for SSH after reboot.",
    down_timeout_seconds: int = 60,
    up_timeout_seconds: int = 240,
) -> None:
    try:
        result = runtime_service.request_runtime_reboot_and_observe(
            connection,
            strategy=strategy,
            callbacks=runtime_callbacks(context),
            down_timeout_seconds=down_timeout_seconds,
            up_timeout_seconds=up_timeout_seconds,
            request_reboot=remote_request_reboot,
            request_acp_reboot=acp_reboot,
            wait_for_ssh_state=wait_for_ssh_state_conn,
        )
    except SshCommandTimeout as exc:
        raise AppOperationError(f"SSH reboot request timed out: {exc}", code="remote_error") from exc
    except SshError as exc:
        raise AppOperationError(f"SSH reboot request failed: {exc}", code="remote_error") from exc
    if not result.went_down:
        raise AppOperationError(reboot_no_down_message, code="remote_error")
    if not result.came_back_up:
        raise AppOperationError(reboot_up_timeout_message, code="remote_error")
