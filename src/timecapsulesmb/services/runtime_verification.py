from __future__ import annotations

import time
from collections.abc import Callable
from time import sleep

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


BOOT_SETTLE_STAGE = "post_reboot_boot_settle"
BOOT_SETTLE_SECONDS = 20
BOOT_SETTLE_MESSAGE = "Waiting a few seconds for device to boot..."

ACTIVATION_SETTLE_STAGE = "post_activation_settle"
ACTIVATION_SETTLE_SECONDS = 20
ACTIVATION_SETTLE_MESSAGE = "Waiting a few seconds for device to activate..."


def wait_for_boot_settle(
    callbacks: OperationCallbacks,
    *,
    sleep_func: Callable[[float], None] | None = None,
) -> None:
    sleep_func = sleep_func or sleep
    callbacks.stage(BOOT_SETTLE_STAGE)
    callbacks.message(BOOT_SETTLE_MESSAGE)
    sleep_func(BOOT_SETTLE_SECONDS)


def wait_for_activation_settle(
    callbacks: OperationCallbacks,
    *,
    sleep_func: Callable[[float], None] | None = None,
) -> None:
    sleep_func = sleep_func or sleep
    callbacks.stage(ACTIVATION_SETTLE_STAGE)
    callbacks.message(ACTIVATION_SETTLE_MESSAGE)
    sleep_func(ACTIVATION_SETTLE_SECONDS)


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
    started = time.monotonic()
    try:
        verification = probe_runtime(connection, timeout_seconds=timeout_seconds)
    except Exception as exc:
        callbacks.measurement(
            "runtime_verification",
            stage=stage,
            timeout_sec=timeout_seconds,
            duration_sec=round(time.monotonic() - started, 3),
            result="probe_error",
            error_type=type(exc).__name__,
        )
        raise
    callbacks.measurement(
        "runtime_verification",
        **_runtime_verification_measurement(
            verification,
            stage=stage,
            timeout_seconds=timeout_seconds,
            duration_seconds=time.monotonic() - started,
        ),
    )
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


def _runtime_verification_measurement(
    verification: ManagedRuntimeProbeResult,
    *,
    stage: str,
    timeout_seconds: int,
    duration_seconds: float,
) -> dict[str, object]:
    steps = verification.steps
    status_counts = {
        status: sum(1 for step in steps if step.status == status)
        for status in ("pass", "fail", "timeout", "skip")
    }
    failed_steps = [
        step
        for step in steps
        if step.status in {"fail", "timeout"} and step.id != "runtime_timeout"
    ]
    final_blocker = failed_steps[-1] if failed_steps else None
    attempts = verification.attempts
    soft_window_attempts = tuple(attempt for attempt in attempts if attempt.phase == "soft_window")
    final_check_attempts = tuple(attempt for attempt in attempts if attempt.phase == "final_check")
    last_attempt = attempts[-1] if attempts else None
    measurement: dict[str, object] = {
        "stage": stage,
        "timeout_sec": timeout_seconds,
        "duration_sec": round(duration_seconds, 3),
        "ready": verification.ready,
        "result": "success" if verification.ready else "not_ready",
        "smbd_ready": verification.smbd.ready,
        "mdns_ready": verification.mdns.ready,
        "step_count": len(steps),
        "pass_step_count": status_counts["pass"],
        "fail_step_count": status_counts["fail"],
        "timeout_step_count": status_counts["timeout"],
        "skip_step_count": status_counts["skip"],
        "attempt_count": len(attempts),
        "soft_window_attempt_count": len(soft_window_attempts),
        "final_check_attempt_count": len(final_check_attempts),
        "final_check_attempts_allowed": verification.final_attempts_allowed,
    }
    if verification.soft_timeout_seconds is not None:
        measurement["soft_timeout_sec"] = verification.soft_timeout_seconds
    if last_attempt is not None:
        measurement.update(
            last_attempt_phase=last_attempt.phase,
            last_attempt_duration_sec=last_attempt.duration_seconds,
            last_attempt_ready=last_attempt.ready,
            last_attempt_smbd_ready=last_attempt.smbd_ready,
            last_attempt_mdns_ready=last_attempt.mdns_ready,
        )
        if last_attempt.final_blocker_step is not None:
            measurement.update(
                last_attempt_blocker_step=last_attempt.final_blocker_step,
                last_attempt_blocker_status=last_attempt.final_blocker_status,
                last_attempt_blocker_detail=last_attempt.final_blocker_detail,
            )
    if final_blocker is not None:
        measurement.update(
            final_blocker_step=final_blocker.id,
            final_blocker_status=final_blocker.status,
            final_blocker_detail=final_blocker.detail,
        )
    return measurement
