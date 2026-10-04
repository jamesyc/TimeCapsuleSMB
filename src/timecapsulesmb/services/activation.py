from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from timecapsulesmb.core.release import CLI_VERSION_CODE, RELEASE_TAG, release_major
from timecapsulesmb.device.errors import DeviceError
from timecapsulesmb.device.probe import (
    ManagedRuntimeProbeResult,
    RcLocalAutostartProbeResult,
    flash_runtime_config_present_conn,
    probe_managed_runtime_conn,
    probe_netbsd4_rc_local_autostart_conn,
    read_deployed_version_conn,
)
from timecapsulesmb.deploy.commands import RemoteAction
from timecapsulesmb.deploy.executor import run_remote_actions
from timecapsulesmb.services.callbacks import OperationCallbacks
from timecapsulesmb.services.runtime_verification import verify_managed_runtime_ready
from timecapsulesmb.transport.ssh import SshConnection


ActivationDecisionReason = Literal[
    "runtime_already_ready",
    "runtime_not_ready",
    "firmware_autostart_enabled",
    "firmware_autostart_missing",
]


@dataclass(frozen=True)
class ActivationDecision:
    run_actions: bool
    verify_runtime: bool
    reason: ActivationDecisionReason
    detail: str
    runtime: ManagedRuntimeProbeResult | None = None
    autostart: RcLocalAutostartProbeResult | None = None


INSTALL_UPDATE_HINT = (
    'Run "Install / Update Samba" in the macOS app, or tcapsule deploy from the command line.'
)
# Activation starts the installed runtime and checks it with this version's
# readiness probes. They hold for every release of this major version from
# v3.1.0 on, older or newer than this one: v3.1.0 is the first runtime that
# leaves Apple's mDNSResponder running, which the probes require (v3.0.x
# stopped it). Keep this in this major version, and raise it when a release
# changes what the probes check.
OLDEST_ACTIVATABLE_RELEASE_TAG = "v3.1.0"
OLDEST_ACTIVATABLE_VERSION_CODE = 30100


class ActivationInstallError(DeviceError):
    """The device does not hold an install this version can start and verify."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


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
            f"TimeCapsuleSMB is not installed on this device. {INSTALL_UPDATE_HINT}",
            code="runtime_not_installed",
        )
    version = read_deployed_version_conn(connection)
    callbacks.debug(
        deployed_config_present=True,
        deployed_release_tag=version.release_tag,
        deployed_cli_version_code=version.cli_version_code,
    )
    if version.release_tag is None or version.cli_version_code is None:
        raise ActivationInstallError(
            "The installed TimeCapsuleSMB has no version information, so it is older than "
            f"{OLDEST_ACTIVATABLE_RELEASE_TAG}, the oldest release {RELEASE_TAG} can start. {INSTALL_UPDATE_HINT}",
            code="runtime_outdated",
        )
    if release_major(version.cli_version_code) > release_major(CLI_VERSION_CODE):
        raise ActivationInstallError(
            f"The installed TimeCapsuleSMB {version.release_tag} is from a newer major version than this "
            f"version, {RELEASE_TAG}. Update TimeCapsuleSMB, then start it again.",
            code="client_outdated",
        )
    if version.cli_version_code < OLDEST_ACTIVATABLE_VERSION_CODE:
        raise ActivationInstallError(
            f"The installed TimeCapsuleSMB {version.release_tag} is older than {OLDEST_ACTIVATABLE_RELEASE_TAG}, "
            f"the oldest release {RELEASE_TAG} can start. {INSTALL_UPDATE_HINT}",
            code="runtime_outdated",
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
    return ActivationDecision(
        run_actions=True,
        verify_runtime=True,
        reason="runtime_not_ready",
        detail=runtime.detail,
        runtime=runtime,
    )


def decide_netbsd4_post_reboot_activation(
    connection: SshConnection,
    *,
    autostart_probe_timeout_seconds: int = 30,
) -> ActivationDecision:
    autostart = probe_netbsd4_rc_local_autostart_conn(connection, timeout_seconds=autostart_probe_timeout_seconds)
    if autostart.enabled:
        return ActivationDecision(
            run_actions=False,
            verify_runtime=True,
            reason="firmware_autostart_enabled",
            detail=autostart.detail,
            autostart=autostart,
        )
    return ActivationDecision(
        run_actions=True,
        verify_runtime=True,
        reason="firmware_autostart_missing",
        detail=autostart.detail,
        autostart=autostart,
    )


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


def activate_runtime(
    connection: SshConnection,
    activation_actions: list[RemoteAction],
    *,
    callbacks: OperationCallbacks,
) -> ActivationDecision:
    """Start an already-deployed NetBSD4 runtime unless it is already running.

    Raises ActivationInstallError when the device has no install this version
    can start, and DeviceError when the started runtime does not become ready.
    """
    callbacks.stage("probe_runtime")
    require_compatible_install(connection, callbacks)
    decision = decide_manual_activation(connection)
    callbacks.debug(
        activation_decision=decision.reason,
        manual_activation_required=decision.run_actions,
    )
    callbacks.message(decision.detail)
    if decision.run_actions:
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
