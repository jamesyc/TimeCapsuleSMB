from __future__ import annotations

import time
from typing import Iterable

from timecapsulesmb.cli.context import CommandContext
from timecapsulesmb.device.errors import DeviceError
from timecapsulesmb.services.runtime_verification import verify_managed_runtime_ready
from timecapsulesmb.transport.local import tcp_open
from timecapsulesmb.transport.ssh import SshConnection


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
    try:
        verify_managed_runtime_ready(
            connection,
            callbacks=command_context.to_operation_callbacks(),
            stage=stage,
            timeout_seconds=timeout_seconds,
            heading=heading,
            failure_message=failure_message,
        )
    except DeviceError as exc:
        print(str(exc))
        command_context.fail_with_error(str(exc))
        return False
    return True
