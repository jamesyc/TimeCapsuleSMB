from __future__ import annotations

import time
from typing import Iterable

from timecapsulesmb.cli.context import CommandContext
from timecapsulesmb.core.config import extract_host
from timecapsulesmb.core.errors import system_exit_message
from timecapsulesmb.deploy.executor import remote_request_reboot
from timecapsulesmb.deploy.verify import (
    managed_runtime_ready,
    render_managed_runtime_verification,
    verify_managed_runtime,
)
from timecapsulesmb.device.probe import wait_for_ssh_state_conn
from timecapsulesmb.integrations.acp import ACPError, reboot as acp_reboot
from timecapsulesmb.transport.local import tcp_open
from timecapsulesmb.transport.ssh import SshCommandTimeout, SshConnection, SshError


REBOOT_UP_TIMEOUT_MESSAGE = "Timed out waiting for SSH after reboot."
ACP_REBOOT_REQUEST_TIMEOUT_SECONDS = 10


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
    label = service_name or f"TCP port {port}"
    expected_state_string = "open" if expected_state else "closed"
    if verbose:
        print(f"Waiting for {label} to be {expected_state_string}...")
    deadline = time.time() + timeout_seconds
    while True:
        is_open = tcp_open(host, port)
        if is_open == expected_state:
            if verbose:
                print(f"{label} is {expected_state_string}.")
            return True
        if time.time() >= deadline:
            break
        time.sleep(interval_seconds)
    if verbose:
        print(f"{label} did not become {expected_state_string} within {timeout_seconds}s.")
    return False


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


def request_reboot_and_wait(
    connection: SshConnection,
    command_context: CommandContext,
    *,
    reboot_no_down_message: str,
    down_timeout_seconds: int = 60,
    up_timeout_seconds: int = 240,
) -> bool:
    command_context.set_stage("reboot")
    command_context.update_fields(reboot_was_attempted=True)
    _request_reboot_acp_then_ssh(connection, command_context)

    return _observe_reboot_cycle(
        connection,
        command_context,
        reboot_no_down_message=reboot_no_down_message,
        down_timeout_seconds=down_timeout_seconds,
        up_timeout_seconds=up_timeout_seconds,
    )


def _request_reboot_acp_then_ssh(connection: SshConnection, command_context: CommandContext) -> None:
    command_context.add_debug_fields(reboot_request_strategy="acp_then_ssh")
    if _request_reboot_via_acp(connection, command_context):
        return
    _request_reboot_via_ssh(connection, command_context)


def _request_reboot_via_acp(connection: SshConnection, command_context: CommandContext) -> bool:
    command_context.add_debug_fields(acp_reboot_attempted=True)
    try:
        acp_reboot(
            extract_host(connection.host),
            connection.password,
            timeout=ACP_REBOOT_REQUEST_TIMEOUT_SECONDS,
        )
    except ACPError as exc:
        command_context.add_debug_fields(
            acp_reboot_succeeded=False,
            acp_reboot_error=system_exit_message(exc),
        )
        print("ACP reboot request failed; trying SSH reboot request.")
        return False

    command_context.add_debug_fields(acp_reboot_succeeded=True)
    print("ACP reboot requested.")
    return True


def _request_reboot_via_ssh(connection: SshConnection, command_context: CommandContext) -> None:
    command_context.add_debug_fields(ssh_reboot_attempted=True)
    try:
        remote_request_reboot(connection)
    except SshCommandTimeout as exc:
        command_context.add_debug_fields(
            ssh_reboot_succeeded=False,
            ssh_reboot_timed_out=True,
            ssh_reboot_error=system_exit_message(exc),
        )
        print("SSH reboot request timed out; checking whether the device is rebooting...")
        return
    except SshError as exc:
        command_context.add_debug_fields(
            ssh_reboot_succeeded=False,
            ssh_reboot_error=system_exit_message(exc),
        )
        print("SSH reboot request failed; checking whether the device is rebooting anyway...")
        return

    command_context.add_debug_fields(ssh_reboot_succeeded=True)
    print("SSH reboot requested.")


def _observe_reboot_cycle(
    connection: SshConnection,
    command_context: CommandContext,
    *,
    reboot_no_down_message: str,
    down_timeout_seconds: int,
    up_timeout_seconds: int,
) -> bool:
    print("Waiting for the device to go down...")
    command_context.set_stage("wait_for_reboot_down")
    if not wait_for_ssh_state_conn(connection, expected_up=False, timeout_seconds=down_timeout_seconds):
        print(reboot_no_down_message)
        command_context.fail_with_error(reboot_no_down_message)
        return False

    print("Waiting for the device to come back up...")
    command_context.set_stage("wait_for_reboot_up")
    if not wait_for_ssh_state_conn(connection, expected_up=True, timeout_seconds=up_timeout_seconds):
        print(REBOOT_UP_TIMEOUT_MESSAGE)
        command_context.fail_with_error(REBOOT_UP_TIMEOUT_MESSAGE)
        return False

    command_context.update_fields(device_came_back_after_reboot=True)
    print("Device is back online.")
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
        if detail:
            failure_message = f"{failure_message.rstrip()} {detail}"
        print(failure_message)
        command_context.fail_with_error(failure_message)
        return False
    return True
