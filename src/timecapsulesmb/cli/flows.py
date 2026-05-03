from __future__ import annotations

from timecapsulesmb.cli.context import CommandContext
from timecapsulesmb.core.errors import system_exit_message
from timecapsulesmb.deploy.executor import remote_request_reboot
from timecapsulesmb.deploy.verify import (
    managed_runtime_ready,
    render_managed_runtime_verification,
    verify_managed_runtime,
)
from timecapsulesmb.device.probe import wait_for_ssh_state_conn
from timecapsulesmb.transport.ssh import SshCommandTimeout, SshConnection


REBOOT_UP_TIMEOUT_MESSAGE = "Timed out waiting for SSH after reboot."


def request_reboot_and_wait(
    connection: SshConnection,
    command_context: CommandContext,
    *,
    timeout_no_down_message: str,
    down_timeout_seconds: int = 60,
    up_timeout_seconds: int = 240,
) -> bool:
    command_context.set_stage("reboot")
    command_context.update_fields(reboot_was_attempted=True)
    reboot_request_timed_out = False
    try:
        remote_request_reboot(connection)
    except SshCommandTimeout as exc:
        reboot_request_timed_out = True
        command_context.add_debug_fields(
            reboot_request_timed_out=True,
            reboot_request_error=system_exit_message(exc),
        )
        print("Reboot request timed out; checking whether the device is rebooting...")

    if not reboot_request_timed_out:
        print("Reboot requested. Waiting for the device to go down...")
    command_context.set_stage("wait_for_reboot_down")
    reboot_went_down = wait_for_ssh_state_conn(connection, expected_up=False, timeout_seconds=down_timeout_seconds)
    if reboot_request_timed_out and not reboot_went_down:
        print(timeout_no_down_message)
        command_context.fail_with_error(timeout_no_down_message)
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
        print(failure_message)
        command_context.fail_with_error(failure_message)
        return False
    return True
