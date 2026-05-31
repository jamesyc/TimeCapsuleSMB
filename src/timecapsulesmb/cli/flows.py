from __future__ import annotations

import time
from typing import Iterable

from timecapsulesmb.cli.context import CommandContext
from timecapsulesmb.core.errors import system_exit_message
from timecapsulesmb.deploy.verify import (
    render_managed_runtime_verification,
)
from timecapsulesmb.device.probe import (
    probe_managed_runtime_conn,
    read_remote_network_diagnostics_conn,
    read_runtime_log_tails_conn,
    runtime_startup_failure_debug_fields,
)
from timecapsulesmb.services import runtime as runtime_service
from timecapsulesmb.transport.local import tcp_open
from timecapsulesmb.transport.ssh import SshConnection


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
    verification = probe_managed_runtime_conn(connection, timeout_seconds=timeout_seconds)
    for line in render_managed_runtime_verification(verification, heading=heading):
        print(line)
    if not verification.ready:
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
