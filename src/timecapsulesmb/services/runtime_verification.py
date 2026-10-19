from __future__ import annotations

import time
from collections.abc import Callable

from timecapsulesmb.core.errors import system_exit_message
from timecapsulesmb.deploy.verify import render_managed_runtime_verification
from timecapsulesmb.device.disk_errors import DiskFailingError, disk_errors_explain, read_disk_errors_conn
from timecapsulesmb.device.errors import DeviceError, RsyncNotRunningError
from timecapsulesmb.device.probe import (
    ManagedRuntimeProbeResult,
    RuntimeNamingIdentityProbeResult,
    limit_remote_log_tail,
    probe_managed_runtime_conn,
    probe_remote_runtime_naming_identity_conn,
    read_runtime_log_tails_conn,
    runtime_payload_problem,
)
from timecapsulesmb.discovery.bonjour import BonjourQuery, BonjourServiceInstance
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
    read_disk_errors: Callable[[SshConnection], tuple[str, ...] | None] | None = None,
) -> ManagedRuntimeProbeResult:
    callbacks = callbacks or OperationCallbacks()
    if probe_runtime is None:
        probe_runtime = probe_managed_runtime_conn
    if read_runtime_logs is None:
        read_runtime_logs = read_runtime_log_tails_conn
    if read_disk_errors is None:
        read_disk_errors = read_disk_errors_conn
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
    if verification.ready or verification.samba_ready:
        # File sharing serves, with rsync or without it: Time Machine finds
        # the device through this Mac's copy of its _adisk record either way.
        recheck_cached_adisk(connection, callbacks, stage=stage)
    if verification.ready:
        return verification

    try:
        callbacks.debug(**read_runtime_logs(connection))
    except Exception as exc:
        callbacks.debug(remote_runtime_log_tail_error=system_exit_message(exc))

    disk_errors = read_disk_errors(connection)
    if disk_errors:
        callbacks.debug(disk_error_lines=list(disk_errors[-5:]))
    raise runtime_failure_error(verification, failure_message, disk_errors=disk_errors)


CHECKUP_SENTENCE = 'Run "Checkup" in the macOS app, or tcapsule doctor from the command line, for details.'


def runtime_failure_error(
    verification: ManagedRuntimeProbeResult, failure_message: str, *, disk_errors: tuple[str, ...] | None = None,
) -> DeviceError:
    """The error for a runtime that is not ready: this boot's disk errors
    when they explain it (disk_errors_explain), else the manager's verdict,
    or rsync's alone while file sharing serves (whose error must not say file
    sharing failed), or failure_message with the probe's detail."""
    problem = runtime_payload_problem(verification)
    if disk_errors_explain(disk_errors, (problem,) if problem else (), samba_ready=verification.samba_ready):
        return DiskFailingError(disk_errors)
    if problem is not None:
        return problem.error_type(problem.error_after(failure_message))
    if verification.samba_ready:
        # No verdict says why: Checkup's rsync check does.
        return RsyncNotRunningError(
            f"File sharing and Time Machine are running, but rsync is not: {verification.detail.strip()}. "
            f"{CHECKUP_SENTENCE}"
        )
    detail = verification.detail.strip()
    return DeviceError(f"{failure_message.rstrip()} {detail}" if detail else failure_message)


def recheck_cached_adisk(
    connection: SshConnection,
    callbacks: OperationCallbacks,
    *,
    stage: str,
    read_identity: Callable[[SshConnection], RuntimeNamingIdentityProbeResult] = probe_remote_runtime_naming_identity_conn,
    query: Callable[[], BonjourQuery] = BonjourQuery,
) -> None:
    """Have this computer re-check its cached copy of the device's _adisk record.

    A Mac that missed the device's announcements after this start keeps the
    TXT it had before for up to 75 minutes: Apple's own, which advertises AFP,
    after an install, or ours from before the AFP setting changed. Time Machine
    on macOS 26.x/27 hides a Time Capsule that advertises AFP. mDNSResponder
    drops a copy the device no longer confirms a few seconds later.

    The name is the one Apple derives from the device name. After a name
    conflict mDNSResponder registers the service as "Name (2)" instead, and a
    cached copy under that name is not re-checked here; discoveryd announces
    the renamed registration again later (registrant.c, reannounce()).

    Best effort: it never fails the operation. The measurement rides on the
    operation's event, successful ones included, and says what happened:
    rechecked, refused (mDNSResponder would not re-check the copies it has),
    nothing_cached, unsupported (no system cache this computer can re-check),
    unnamed (a device name over 63 bytes, which Apple replaces with a name of
    its own), name_unreadable (acp printed no device name) or error."""
    started = time.monotonic()
    fields: dict[str, object] = {"stage": stage}
    try:
        identity = read_identity(connection)
        name = identity.mdns_instance_name
        counts = query().recheck_cached_txt(BonjourServiceInstance("_adisk._tcp.local.", name, "")) if name else None
        if not name:
            # acp prints a failed read on stderr and exits 0, which leaves the
            # name empty, the same as an unset one.
            fields["result"] = "unnamed" if identity.system_name else "name_unreadable"
        elif counts is None:
            fields["result"] = "unsupported"
        else:
            result = "rechecked" if counts[1] else "refused" if counts[0] else "nothing_cached"
            fields.update(result=result, copies=counts[0], rechecked=counts[1])
    except Exception as exc:
        fields.update(result="error", error=system_exit_message(exc)[:200])
    callbacks.measurement("adisk_cache_recheck", **fields, duration_sec=round(time.monotonic() - started, 3))


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
        for status in ("pass", "fail", "timeout", "skip", "info")
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
        # rsync_not_ready: file sharing serves and only rsync does not.
        "result": "success" if verification.ready else "rsync_not_ready" if verification.samba_ready else "not_ready",
        "smbd_ready": verification.smbd.ready,
        "mdns_ready": verification.mdns.ready,
        "samba_ready": verification.samba_ready,
        "step_count": len(steps),
        "pass_step_count": status_counts["pass"],
        "fail_step_count": status_counts["fail"],
        "timeout_step_count": status_counts["timeout"],
        "skip_step_count": status_counts["skip"],
        "info_step_count": status_counts["info"],
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
    # Deploy reports native NBNS without waiting for it (advisory); telemetry
    # keeps its result so NBNS failures stay visible.
    nbns = next((step for step in verification.mdns.steps if step.id == "native_nbns"), None)
    measurement["native_nbns_status"] = "none" if nbns is None else nbns.status
    if nbns is not None:
        measurement["native_nbns_detail"] = nbns.detail
    # Bonjour registration is reported the same way.
    bonjour = next((step for step in verification.mdns.steps if step.id == "mdns_registered"), None)
    measurement["native_mdns_status"] = "none" if bonjour is None else bonjour.status
    if bonjour is not None:
        measurement["native_mdns_detail"] = bonjour.detail
    if final_blocker is not None:
        measurement.update(
            final_blocker_step=final_blocker.id,
            final_blocker_status=final_blocker.status,
            final_blocker_detail=final_blocker.detail,
        )
        if final_blocker.status == "timeout" and final_blocker.stdout:
            # A process listing that timed out: the table without command
            # lines, read just after it.
            measurement["final_blocker_process_table"] = limit_remote_log_tail(final_blocker.stdout.rstrip())
        if final_blocker.asleep_in_kernel:
            measurement["final_blocker_asleep_in_kernel"] = final_blocker.asleep_in_kernel
    return measurement
