from __future__ import annotations

from timecapsulesmb.app.confirmations import build_confirmation, require_confirmation
from timecapsulesmb.app.context import AppOperationContext
from timecapsulesmb.app.contracts import ssh_access_payload
from timecapsulesmb.app.ops.common import load_request_config
from timecapsulesmb.core.net import endpoint_host
from timecapsulesmb.services.app import AppOperationError, OperationResult, bool_param, string_param
from timecapsulesmb.services.runtime import resolve_env_connection
from timecapsulesmb.services.ssh_access import enable_ssh_access, probe_ssh_access


def ssh_access_operation(params: dict[str, object], context: AppOperationContext) -> OperationResult:
    action = string_param(params, "action", "status").strip().lower() or "status"
    config = load_request_config(params, context)
    host = config.require("TC_HOST")
    if action == "status":
        context.stage("probe_ssh_access")
        result = probe_ssh_access(host)
        context.update_fields(
            acp_host=result.host,
            acp_port_reachable=result.acp_port_reachable,
            ssh_port_reachable=result.ssh_port_reachable,
            ssh_disabled_likely=result.ssh_disabled_likely,
        )
        return OperationResult(True, ssh_access_payload(result))
    if action != "enable":
        raise AppOperationError(f"Unsupported SSH access action: {action}", code="validation_failed")

    connection = resolve_env_connection(config)
    acp_host = endpoint_host(connection.host)
    context.stage("confirm_enable_ssh")
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
                "host": connection.host,
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
    result = enable_ssh_access(
        connection,
        no_wait=bool_param(params, "no_wait"),
        callbacks=context.to_operation_callbacks(),
    )
    context.update_fields(
        set_ssh_action=result.action,
        ssh_initially_reachable=result.ssh_initially_reachable,
        ssh_final_reachable=result.ssh_final_reachable,
        acp_port_reachable=result.acp_port_reachable,
        reboot_was_attempted=result.reboot_requested,
    )
    if result.action == "enable_ssh" and not result.ssh_verification_skipped and not result.ssh_final_reachable:
        raise AppOperationError(result.summary, code="remote_error")
    return OperationResult(True, ssh_access_payload(result))
