from __future__ import annotations

import time
from typing import Iterable

from timecapsulesmb.cli.context import CommandContext
from timecapsulesmb.cli.runtime import LogCallback
from timecapsulesmb.core.errors import system_exit_message
from timecapsulesmb.deploy.executor import remote_request_reboot
from timecapsulesmb.deploy.verify import (
    managed_runtime_ready,
    render_managed_runtime_verification,
    verify_managed_runtime,
)
from timecapsulesmb.device.probe import (
    read_remote_network_diagnostics_conn,
    read_runtime_log_tails_conn,
    runtime_startup_failure_debug_fields,
    wait_for_ssh_state_conn,
)
from timecapsulesmb.integrations.acp import reboot as acp_reboot
from timecapsulesmb.services import runtime as runtime_service
from timecapsulesmb.services.runtime import RuntimeOperationCallbacks
from timecapsulesmb.transport.local import tcp_open
from timecapsulesmb.transport.ssh import SshConnection


REBOOT_UP_TIMEOUT_MESSAGE = "Timed out waiting for SSH after reboot."
DEPLOY_REBOOT_UP_TIMEOUT_MESSAGE = (
    "Timed out waiting for SSH after reboot.\n\n"
    "The payload was uploaded and the reboot request succeeded, but the device did not accept SSH again "
    "before the 4 minute timeout. It may still be booting, or it may have come back with a different IP address.\n\n"
    "Next steps:\n"
    "  1. Wait a few more minutes.\n"
    "  2. If the device is reachable at a new IP, update TC_HOST or rerun configure.\n"
    "  3. Make sure you are connected to the same network/wifi as the device.\n"
    "  4. On NetBSD 4 devices, run `tcapsule activate` once SSH is reachable; "
    "deploy did not get far enough to activate Samba after reboot."
)
ACP_REBOOT_REQUEST_TIMEOUT_SECONDS = 10
SSH_SHUTDOWN_REBOOT_PROGRESS_MESSAGE = "SSH: /bin/sync; /sbin/shutdown -r now (fallback /sbin/reboot)"


def wait_for_tcp_port_state(
    host: str,
    port: int,
    *,
    expected_state: bool,
    timeout_seconds: int = 120,
    interval_seconds: int = 5,
    verbose: bool = True,
    service_name: str | None = None,
) -> bool:
    return runtime_service.wait_for_tcp_port_state(
        host,
        port,
        expected_state=expected_state,
        timeout_seconds=timeout_seconds,
        interval_seconds=interval_seconds,
        log=print if verbose else None,
        service_name=service_name,
        tcp_open_func=tcp_open,
    )


def wait_for_device_up(
    host: str,
    *,
    probe_ports: Iterable[int] = (5009, 445, 139),
    timeout_seconds: int = 180,
    interval_seconds: int = 5,
) -> bool:
    deadline = time.time() + timeout_seconds
    while True:
        if any(tcp_open(host, port) for port in probe_ports):
            return True
        if time.time() >= deadline:
            break
        time.sleep(interval_seconds)
    return False


def _runtime_callbacks(command_context: CommandContext) -> RuntimeOperationCallbacks:
    return RuntimeOperationCallbacks(
        set_stage=command_context.set_stage,
        log=print,
        add_debug_fields=command_context.add_debug_fields,
        update_fields=command_context.update_fields,
    )


def request_reboot_and_wait(
    connection: SshConnection,
    command_context: CommandContext,
    *,
    reboot_no_down_message: str,
    down_timeout_seconds: int = 60,
    up_timeout_seconds: int = 240,
    reboot_up_timeout_message: str = REBOOT_UP_TIMEOUT_MESSAGE,
) -> bool:
    result = runtime_service.request_runtime_reboot_and_observe(
        connection,
        strategy="acp_then_ssh",
        callbacks=_runtime_callbacks(command_context),
        down_timeout_seconds=down_timeout_seconds,
        up_timeout_seconds=up_timeout_seconds,
        request_reboot=remote_request_reboot,
        request_acp_reboot=acp_reboot,
        wait_for_ssh_state=wait_for_ssh_state_conn,
    )
    return _reboot_cycle_ok(command_context, result, reboot_no_down_message, reboot_up_timeout_message)


def request_reboot(
    connection: SshConnection,
    command_context: CommandContext,
    *,
    raise_on_request_error: bool = False,
) -> None:
    runtime_service.request_runtime_reboot(
        connection,
        strategy="acp_then_ssh",
        callbacks=_runtime_callbacks(command_context),
        raise_on_request_error=raise_on_request_error,
        request_reboot=remote_request_reboot,
        request_acp_reboot=acp_reboot,
    )


def request_deploy_reboot_and_wait(
    connection: SshConnection,
    command_context: CommandContext,
    *,
    reboot_no_down_message: str,
    down_timeout_seconds: int = 60,
    up_timeout_seconds: int = 240,
    reboot_up_timeout_message: str = DEPLOY_REBOOT_UP_TIMEOUT_MESSAGE,
) -> bool:
    result = runtime_service.request_runtime_reboot_and_observe(
        connection,
        strategy="ssh_shutdown_then_reboot",
        callbacks=_runtime_callbacks(command_context),
        down_timeout_seconds=down_timeout_seconds,
        up_timeout_seconds=up_timeout_seconds,
        request_reboot=remote_request_reboot,
        wait_for_ssh_state=wait_for_ssh_state_conn,
    )
    return _reboot_cycle_ok(command_context, result, reboot_no_down_message, reboot_up_timeout_message)


def request_ssh_reboot(
    connection: SshConnection,
    command_context: CommandContext,
    *,
    log: LogCallback = None,
    raise_on_request_error: bool = False,
) -> None:
    runtime_service.request_runtime_reboot(
        connection,
        strategy="ssh",
        callbacks=_runtime_callbacks(command_context),
        progress_log=log,
        raise_on_request_error=raise_on_request_error,
        request_reboot=remote_request_reboot,
    )


def observe_reboot_cycle(
    connection: SshConnection,
    command_context: CommandContext,
    *,
    reboot_no_down_message: str,
    down_timeout_seconds: int,
    up_timeout_seconds: int,
    reboot_up_timeout_message: str = REBOOT_UP_TIMEOUT_MESSAGE,
) -> bool:
    result = runtime_service.observe_runtime_reboot_cycle(
        connection,
        callbacks=_runtime_callbacks(command_context),
        down_timeout_seconds=down_timeout_seconds,
        up_timeout_seconds=up_timeout_seconds,
        wait_for_ssh_state=wait_for_ssh_state_conn,
    )
    return _reboot_cycle_ok(command_context, result, reboot_no_down_message, reboot_up_timeout_message)


def _reboot_cycle_ok(
    command_context: CommandContext,
    result: runtime_service.RebootCycleResult,
    reboot_no_down_message: str,
    reboot_up_timeout_message: str,
) -> bool:
    if not result.went_down:
        print(reboot_no_down_message)
        command_context.fail_with_error(reboot_no_down_message)
        return False
    if not result.came_back_up:
        print(reboot_up_timeout_message)
        command_context.fail_with_error(reboot_up_timeout_message)
        return False
    return True


def verify_managed_runtime_flow(
    connection: SshConnection,
    command_context: CommandContext,
    *,
    stage: str,
    timeout_seconds: int,
    heading: str,
    failure_message: str,
) -> bool:
    command_context.set_stage(stage)
    verification = verify_managed_runtime(connection, timeout_seconds=timeout_seconds)
    for line in render_managed_runtime_verification(verification, heading=heading):
        print(line)
    if not managed_runtime_ready(verification):
        detail = verification.detail.strip()
        runtime_log_fields: dict[str, object] = {}
        try:
            runtime_log_fields = read_runtime_log_tails_conn(connection)
            command_context.add_debug_fields(**runtime_log_fields)
        except Exception as exc:
            command_context.add_debug_fields(remote_runtime_log_tail_error=system_exit_message(exc))
        startup_failure_fields = runtime_startup_failure_debug_fields(
            runtime_log_fields,
            verification_detail=detail,
        )
        if startup_failure_fields:
            command_context.add_debug_fields(**startup_failure_fields)
            if startup_failure_fields.get("runtime_startup_failure") == "network_auto_ip_unavailable":
                try:
                    command_context.add_debug_fields(**read_remote_network_diagnostics_conn(connection))
                except Exception as exc:
                    command_context.add_debug_fields(remote_network_diagnostics_error=system_exit_message(exc))
        if detail:
            failure_message = f"{failure_message.rstrip()} {detail}"
        print(failure_message)
        command_context.fail_with_error(failure_message)
        return False
    return True
