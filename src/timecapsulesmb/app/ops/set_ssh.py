from __future__ import annotations

from timecapsulesmb.app.confirmations import build_confirmation, require_confirmation
from timecapsulesmb.app.context import AppOperationContext
from timecapsulesmb.app.contracts import set_ssh_payload
from timecapsulesmb.app.ops.common import load_request_config
from timecapsulesmb.core.net import endpoint_host
from timecapsulesmb.services.app import AppOperationError, OperationResult, bool_param, string_param
from timecapsulesmb.services.runtime import resolve_env_connection
from timecapsulesmb.services.set_ssh import (
    disable_set_ssh,
    enable_set_ssh,
    probe_set_ssh_status,
)


def set_ssh_operation(params: dict[str, object], context: AppOperationContext) -> OperationResult:
    action = string_param(params, "action", "status").strip().lower() or "status"
    config = load_request_config(params, context)
    host = config.require("TC_HOST")

    if action == "status":
        context.stage("probe_ssh")
        result = probe_set_ssh_status(host)
        _update_status_fields(context, result)
        return OperationResult(True, set_ssh_payload(result))

    if action not in {"enable", "disable"}:
        raise AppOperationError(f"Unsupported set-ssh action: {action}", code="validation_failed")

    connection = resolve_env_connection(config)
    acp_host = endpoint_host(connection.host)
    context.stage("probe_ssh")
    initial = probe_set_ssh_status(connection.host)
    _update_status_fields(context, initial)

    if action == "enable":
        if not initial.ssh_port_reachable:
            context.stage("confirm_enable_ssh")
            _require_enable_confirmation(params, context=context, connection_host=connection.host, acp_host=acp_host)
        try:
            result = enable_set_ssh(
                connection,
                no_wait=bool_param(params, "no_wait"),
                callbacks=context.to_operation_callbacks(),
                initial=initial,
            )
        except Exception as exc:
            raise AppOperationError(f"Failed to enable SSH via ACP: {exc}", code="remote_error") from exc
    else:
        if initial.ssh_port_reachable:
            context.stage("confirm_disable_ssh")
            _require_disable_confirmation(params, context=context, connection_host=connection.host, acp_host=acp_host)
        try:
            result = disable_set_ssh(
                connection,
                no_wait=bool_param(params, "no_wait"),
                callbacks=context.to_operation_callbacks(),
                initial=initial,
            )
        except Exception as exc:
            raise AppOperationError(f"Failed to disable SSH: {exc}", code="remote_error") from exc

    context.update_fields(
        set_ssh_action=result.action,
        ssh_initially_reachable=result.ssh_initially_reachable,
        ssh_final_reachable=result.ssh_final_reachable,
        acp_port_reachable=result.acp_port_reachable,
        reboot_was_attempted=result.reboot_requested,
        ssh_verification_skipped=result.ssh_verification_skipped,
        ssh_disable_persisted=result.ssh_disable_persisted,
        ssh_reboot_observed_down=result.ssh_reboot_observed_down,
        device_recovered=result.device_recovered,
    )
    return OperationResult(True, set_ssh_payload(result))


def _update_status_fields(context: AppOperationContext, result) -> None:
    context.update_fields(
        acp_host=result.host,
        acp_port_reachable=result.acp_port_reachable,
        ssh_port_reachable=result.ssh_port_reachable,
        ssh_disabled_likely=result.ssh_disabled_likely,
    )


def _require_enable_confirmation(
    params: dict[str, object],
    *,
    context: AppOperationContext,
    connection_host: str,
    acp_host: str,
) -> None:
    require_confirmation(
        params,
        build_confirmation(
            operation=context.operation,
            params=params,
            title="Enable SSH and reboot?",
            message=f"Enable SSH using AirPort ACP on {acp_host} and reboot this AirPort device?",
            action_title="Enable SSH and reboot",
            risk="reboot",
            summary="Enable SSH through AirPort ACP and reboot the AirPort device",
            context={
                "host": connection_host,
                "acp_host": acp_host,
                "device_name": acp_host,
                "requires_reboot": True,
            },
            presentation_id="ssh_access.enable_reboot",
            presentation_values={
                "host": acp_host,
                "device_name": acp_host,
                "requires_reboot": True,
            },
        ),
    )


def _require_disable_confirmation(
    params: dict[str, object],
    *,
    context: AppOperationContext,
    connection_host: str,
    acp_host: str,
) -> None:
    require_confirmation(
        params,
        build_confirmation(
            operation=context.operation,
            params=params,
            title="Disable SSH and reboot?",
            message=f"Disable SSH on {acp_host} and reboot this AirPort device?",
            action_title="Disable SSH and reboot",
            risk="reboot",
            summary="Disable SSH and reboot the AirPort device",
            context={
                "host": connection_host,
                "acp_host": acp_host,
                "device_name": acp_host,
                "requires_reboot": True,
            },
            presentation_values={
                "host": acp_host,
                "device_name": acp_host,
                "requires_reboot": True,
            },
        ),
    )
