from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from timecapsulesmb.core.error_codes import ErrorCode
from timecapsulesmb.core.release import CLI_VERSION_CODE, RELEASE_TAG, release_major
from timecapsulesmb.device import acpd
from timecapsulesmb.device.compat import is_netbsd4_payload_family
from timecapsulesmb.device.disk_errors import (
    DiskFailingError,
    DiskNotRespondingError,
    disk_errors_explain,
    read_disk_errors_conn,
)
from timecapsulesmb.device.errors import AcpdQuitError, DeviceError
from timecapsulesmb.device.probe import (
    ACPD_QUIT_STEP,
    ManagedRuntimeProbeResult,
    ProbedDeviceState,
    flash_runtime_config_present_conn,
    probe_managed_runtime_conn,
    read_deployed_version_conn,
    runtime_payload_problem,
)
from timecapsulesmb.deploy.commands import RemoteAction
from timecapsulesmb.deploy.executor import run_remote_actions
from timecapsulesmb.services.callbacks import OperationCallbacks
from timecapsulesmb.services.runtime import acpd_problems_and_disk_stalls
from timecapsulesmb.services.runtime_verification import runtime_failure_error, verify_managed_runtime_ready
from timecapsulesmb.transport.ssh import SshConnection


ActivationDecisionReason = Literal[
    "runtime_already_ready",
    "samba_already_ready",
    "runtime_not_ready",
]


@dataclass(frozen=True)
class ActivationDecision:
    run_actions: bool
    verify_runtime: bool
    reason: ActivationDecisionReason
    detail: str
    runtime: ManagedRuntimeProbeResult | None = None


# Activation starts the installed runtime and checks it with this version's
# readiness probes. They hold for every release of this major version from
# v3.3.0 on, older or newer than this one: they require Apple's mDNSResponder
# left running (v3.1.0 on; v3.0.x stopped it) and boot.sh's /sbin/diskd guard
# (v3.3.0 on). A missing /sbin/wcifsnd guard is only reported there. Keep this
# in this major version, and raise it when a release changes what the probes
# check.
OLDEST_ACTIVATABLE_RELEASE_TAG = "v3.3.0"
OLDEST_ACTIVATABLE_VERSION_CODE = 30300


class ActivationInstallError(DeviceError):
    """The device does not hold an install this version can start and verify."""

    code: ErrorCode


def require_compatible_install(connection: SshConnection, callbacks: OperationCallbacks) -> None:
    """Refuse to start an install this version cannot verify.

    Activation runs the installed /mnt/Flash/rc.local and then verifies the
    runtime with this version's probes. Without an install that only fails
    with "Can't open /mnt/Flash/rc.local"; an install from another major
    version, or older than OLDEST_ACTIVATABLE_RELEASE_TAG, fails verification
    after the full timeout. Any other release of this major version starts.
    """
    if not flash_runtime_config_present_conn(connection):
        callbacks.debug(deployed_config_present=False)
        raise ActivationInstallError(
            "TimeCapsuleSMB is not installed on this device.",
            code=ErrorCode.RUNTIME_NOT_INSTALLED,
        )
    version = read_deployed_version_conn(connection)
    callbacks.debug(
        deployed_config_present=True,
        deployed_release_tag=version.release_tag,
        deployed_cli_version_code=version.cli_version_code,
    )
    if version.release_tag is None or version.cli_version_code is None:
        raise ActivationInstallError(
            "The installed TimeCapsuleSMB has no version information, so it is older than the oldest release "
            "this version can start.",
            code=ErrorCode.RUNTIME_OUTDATED,
            detail=f"Oldest startable release {OLDEST_ACTIVATABLE_RELEASE_TAG}; this version {RELEASE_TAG}.",
        )
    if release_major(version.cli_version_code) > release_major(CLI_VERSION_CODE):
        raise ActivationInstallError(
            "The installed TimeCapsuleSMB is from a newer major version than this version.",
            code=ErrorCode.CLIENT_OUTDATED,
            detail=f"Installed {version.release_tag}; this version {RELEASE_TAG}.",
        )
    if version.cli_version_code < OLDEST_ACTIVATABLE_VERSION_CODE:
        raise ActivationInstallError(
            "The installed TimeCapsuleSMB is older than the oldest release this version can start.",
            code=ErrorCode.RUNTIME_OUTDATED,
            detail=(
                f"Installed {version.release_tag}; oldest startable release {OLDEST_ACTIVATABLE_RELEASE_TAG}; "
                f"this version {RELEASE_TAG}."
            ),
        )


def decide_manual_activation(
    connection: SshConnection,
    *,
    runtime_probe_timeout_seconds: int = 20,
) -> ActivationDecision:
    runtime = probe_managed_runtime_conn(connection, timeout_seconds=runtime_probe_timeout_seconds)
    if runtime.ready:
        return ActivationDecision(
            run_actions=False,
            verify_runtime=False,
            reason="runtime_already_ready",
            detail=runtime.detail,
            runtime=runtime,
        )
    if runtime.samba_ready:
        # The manager runs and serves: it starts rsync itself, and starting
        # the runtime again would stop file sharing that works. Only the
        # verification's wait can tell how rsync ends up.
        return ActivationDecision(
            run_actions=False,
            verify_runtime=True,
            reason="samba_already_ready",
            detail=runtime.detail,
            runtime=runtime,
        )
    return ActivationDecision(
        run_actions=True,
        verify_runtime=True,
        reason="runtime_not_ready",
        detail=runtime.detail,
        runtime=runtime,
    )


@dataclass(frozen=True)
class Netbsd4StartMessages:
    activation_message: str
    autostart_message: str
    heading: str
    failure_message: str


def start_netbsd4_runtime_after_reboot(
    connection: SshConnection,
    activation_actions: list[RemoteAction],
    *,
    rc_local_autostart: bool,
    callbacks: OperationCallbacks,
    messages: Netbsd4StartMessages,
    run_remote_actions_func=None,
    verify_runtime_func=None,
) -> None:
    """Start the installed NetBSD4 runtime on a device that just rebooted.

    `rc_local_autostart` is the device probe's answer from before the reboot
    (ProbeResult.rc_local_autostart). Stock NetBSD4 firmware does not run
    /mnt/Flash/rc.local at boot, so the runtime is started here; with the
    firmware autostart patch the boot already started it. Either way the
    runtime must become ready. Raises DeviceError when it does not.
    """
    if verify_runtime_func is None:
        verify_runtime_func = verify_managed_runtime_ready
    callbacks.debug(
        activation_decision="firmware_autostart_enabled" if rc_local_autostart else "firmware_autostart_missing",
        manual_activation_required=not rc_local_autostart,
    )
    if not rc_local_autostart:
        run_activation_actions_and_verify(
            connection,
            activation_actions,
            callbacks=callbacks,
            activation_message=messages.activation_message,
            activation_stage="post_reboot_activation",
            verification_stage="verify_runtime_activation",
            verification_timeout_seconds=200,
            verification_heading=messages.heading,
            failure_message=messages.failure_message,
            run_remote_actions_func=run_remote_actions_func,
            verify_runtime_func=verify_runtime_func,
        )
        return
    callbacks.message(messages.autostart_message)
    verify_runtime_func(
        connection,
        callbacks=callbacks,
        stage="verify_runtime_activation",
        timeout_seconds=200,
        heading=messages.heading,
        failure_message=messages.failure_message,
    )


RUNTIME_RESTART_FAILURE_MESSAGE = "File sharing did not restart after the reboot."
RESTART_AFTER_REBOOT_MESSAGES = Netbsd4StartMessages(
    activation_message="Starting the installed runtime after reboot.",
    autostart_message="NetBSD4 firmware autostart is enabled; waiting for managed runtime.",
    heading="Waiting for managed runtime to finish starting...",
    failure_message=RUNTIME_RESTART_FAILURE_MESSAGE,
)
MANUAL_START_AFTER_REBOOT_MESSAGE = (
    "File sharing will not start by itself after this restart. Once the device is back, run "
    "Activate in the macOS app, or tcapsule activate from the command line."
)


def installed_netbsd4_autostart(
    connection: SshConnection,
    probe_state: ProbedDeviceState,
    callbacks: OperationCallbacks,
) -> bool | None:
    """Before a reboot that is not a deploy: what file sharing needs afterwards.

    None when nothing is to be started: the device is not NetBSD4 (NetBSD6
    starts the runtime at boot), or it has no install this version can start.
    Otherwise whether the firmware autostart patch starts it (True) or it must
    be started after the reboot (False). Apple's file sharing returns after any
    reboot; ours should too.
    """
    compatibility = probe_state.compatibility
    if compatibility is None or not is_netbsd4_payload_family(compatibility.payload_family):
        callbacks.update(runtime_start_after_reboot="not_netbsd4")
        return None
    try:
        require_compatible_install(connection, callbacks)
    except ActivationInstallError as exc:
        callbacks.update(runtime_start_after_reboot=exc.code)
        if exc.code != ErrorCode.RUNTIME_NOT_INSTALLED:
            # An install that cannot be started stays off after the reboot; say why.
            callbacks.message(str(exc))
        return None
    autostart = probe_state.probe_result.rc_local_autostart
    callbacks.update(runtime_start_after_reboot="firmware_autostart" if autostart else "rc_local")
    return autostart


def run_activation_actions_and_verify(
    connection: SshConnection,
    activation_actions: list[RemoteAction],
    *,
    callbacks: OperationCallbacks,
    activation_message: str,
    activation_stage: str,
    verification_stage: str,
    verification_timeout_seconds: int,
    verification_heading: str,
    failure_message: str,
    run_remote_actions_func=None,
    verify_runtime_func=None,
) -> None:
    if run_remote_actions_func is None:
        run_remote_actions_func = run_remote_actions
    if verify_runtime_func is None:
        verify_runtime_func = verify_managed_runtime_ready
    callbacks.stage(activation_stage)
    callbacks.message(activation_message)
    run_remote_actions_func(connection, activation_actions)
    verify_runtime_func(
        connection,
        callbacks=callbacks,
        stage=verification_stage,
        timeout_seconds=verification_timeout_seconds,
        heading=verification_heading,
        failure_message=failure_message,
    )


# ACPd's problems that leave a start nothing to run on: the manager reads the
# disks and the admin password from the device's own ACPd. A stuck fork of it
# only hangs the next restart, and its network listener is not used here.
ACPD_START_BLOCKERS = frozenset({"restart_never_happened", "quit", "syut_silent"})
ACPD_QUIT_START_MESSAGE = f"NetBSD4 activation failed. {acpd.ACPD_QUIT_MESSAGE} Nothing was started."


def require_acpd_for_start(
    connection: SshConnection,
    callbacks: OperationCallbacks,
    *,
    runtime: ManagedRuntimeProbeResult | None = None,
) -> None:
    """Refuse, before anything starts, a device whose ACPd cannot serve the
    runtime (ACPD_START_BLOCKERS): its start would only wait out its whole
    verification. `runtime` is the decision's probe, whose wait already ends
    once two listings show ACPd gone (ACPD_QUIT_STEP): ACPd is not read again
    then. The marker and syUT reads raise TransportError; a process listing
    that fails says nothing. A process stuck on the disk refuses the start
    first, as it refuses require_acpd_ready's work
    (services.runtime.acpd_problems_and_disk_stalls): the manager could
    stage nothing from that disk."""
    if runtime is not None and any(step.id == ACPD_QUIT_STEP for step in runtime.steps):
        callbacks.debug(acpd_problems=["quit"])
        raise AcpdQuitError(ACPD_QUIT_START_MESSAGE)
    problems, stalls = acpd_problems_and_disk_stalls(connection, callbacks)
    if stalls:
        raise DiskNotRespondingError(stalls, prefix="NetBSD4 activation failed.", suffix="Nothing was started.")
    problems = [problem for problem in problems if problem.code in ACPD_START_BLOCKERS]
    if not problems:
        return
    callbacks.debug(acpd_problems=[problem.code for problem in problems])
    problem = problems[0]
    if problem.code == "quit":
        raise AcpdQuitError(ACPD_QUIT_START_MESSAGE)
    raise DeviceError(
        f"NetBSD4 activation failed. {acpd.acpd_broken_message(problem.what, note=f'{problem.note}Nothing was started. ')}",
        code=ErrorCode.ACPD_BROKEN,
    )


def activate_runtime(
    connection: SshConnection,
    activation_actions: list[RemoteAction],
    *,
    callbacks: OperationCallbacks,
) -> ActivationDecision:
    """Start an already-deployed NetBSD4 runtime unless it is already running.

    Raises ActivationInstallError when the device has no install this version
    can start, DeviceError when ACPd cannot serve a start
    (require_acpd_for_start) or the started runtime does not become ready.
    """
    callbacks.stage("probe_runtime")
    require_compatible_install(connection, callbacks)
    decision = decide_manual_activation(connection)
    callbacks.debug(
        activation_decision=decision.reason,
        manual_activation_required=decision.run_actions,
    )
    if decision.run_actions:
        # Its error comes before the manager's verdicts: without ACPd they say nothing.
        try:
            require_acpd_for_start(connection, callbacks, runtime=decision.runtime)
        except DeviceError:
            # ACPd cannot invalidate the kernel's failed disk transfers.
            # Ignore stale payload verdicts, as the runtime wait does on quit.
            errors = read_disk_errors_conn(connection)
            if disk_errors_explain(errors):
                callbacks.debug(disk_error_lines=list(errors[-5:]))
                raise DiskFailingError(errors) from None
            raise
    runtime = decision.runtime
    if runtime is not None and not runtime.ready and runtime_payload_problem(runtime) is not None:
        # The running manager already found Samba's or rsync's files missing,
        # damaged or unreadable: starting it again cannot help. A reinstall
        # fixes the first two, a new disk the last; the manager keeps
        # checking the disk itself and clears a verdict the disk outgrew.
        raise runtime_failure_error(
            runtime, "NetBSD4 activation failed.", disk_errors=read_disk_errors_conn(connection),
        )
    callbacks.message(decision.detail)
    if decision.verify_runtime and not decision.run_actions:
        verify_managed_runtime_ready(
            connection,
            callbacks=callbacks,
            stage="verify_runtime_activation",
            timeout_seconds=200,
            heading="Waiting for rsync to start on the NetBSD 4 device...",
            failure_message="NetBSD4 activation failed.",
        )
    elif decision.run_actions:
        run_activation_actions_and_verify(
            connection,
            activation_actions,
            callbacks=callbacks,
            activation_message="Activating NetBSD4 payload without file transfer.",
            activation_stage="run_activation",
            verification_stage="verify_runtime_activation",
            verification_timeout_seconds=200,
            verification_heading="Waiting for NetBSD 4 device activation, this can take a few minutes for Samba to start up...",
            failure_message="NetBSD4 activation failed.",
        )
    return decision
