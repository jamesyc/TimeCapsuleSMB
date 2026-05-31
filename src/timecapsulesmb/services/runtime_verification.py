from __future__ import annotations

from collections.abc import Callable

from timecapsulesmb.core.errors import system_exit_message
from timecapsulesmb.deploy.verify import render_managed_runtime_verification
from timecapsulesmb.device.errors import DeviceError
from timecapsulesmb.device.probe import (
    ManagedRuntimeProbeResult,
    probe_managed_runtime_conn,
    read_remote_network_diagnostics_conn,
    read_runtime_log_tails_conn,
    runtime_startup_failure_debug_fields,
)
from timecapsulesmb.services.callbacks import OperationCallbacks
from timecapsulesmb.transport.ssh import SshConnection


def verify_managed_runtime_ready(
    connection: SshConnection,
    *,
    callbacks: OperationCallbacks | None = None,
    stage: str,
    timeout_seconds: int,
    heading: str,
    failure_message: str,
    probe_runtime: Callable[..., ManagedRuntimeProbeResult] | None = None,
    read_runtime_logs: Callable[[SshConnection], dict[str, object]] | None = None,
    read_network_diagnostics: Callable[[SshConnection], dict[str, object]] | None = None,
) -> ManagedRuntimeProbeResult:
    callbacks = callbacks or OperationCallbacks()
    if probe_runtime is None:
        probe_runtime = probe_managed_runtime_conn
    if read_runtime_logs is None:
        read_runtime_logs = read_runtime_log_tails_conn
    if read_network_diagnostics is None:
        read_network_diagnostics = read_remote_network_diagnostics_conn
    callbacks.stage(stage)
    verification = probe_runtime(connection, timeout_seconds=timeout_seconds)
    for line in render_managed_runtime_verification(verification, heading=heading):
        callbacks.message(line)
    if verification.ready:
        return verification

    detail = verification.detail.strip()
    runtime_log_fields: dict[str, object] = {}
    try:
        runtime_log_fields = read_runtime_logs(connection)
        callbacks.debug(**runtime_log_fields)
    except Exception as exc:
        callbacks.debug(remote_runtime_log_tail_error=system_exit_message(exc))

    startup_failure_fields = runtime_startup_failure_debug_fields(
        runtime_log_fields,
        verification_detail=detail,
    )
    if startup_failure_fields:
        callbacks.debug(**startup_failure_fields)
        if startup_failure_fields.get("runtime_startup_failure") == "network_auto_ip_unavailable":
            try:
                callbacks.debug(**read_network_diagnostics(connection))
            except Exception as exc:
                callbacks.debug(remote_network_diagnostics_error=system_exit_message(exc))

    if detail:
        failure_message = f"{failure_message.rstrip()} {detail}"
    raise DeviceError(failure_message)
