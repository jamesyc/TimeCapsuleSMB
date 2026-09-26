from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from timecapsulesmb.device.probe import (
    ManagedRuntimeProbeResult,
    RcLocalAutostartProbeResult,
    probe_managed_runtime_conn,
    probe_netbsd4_rc_local_autostart_conn,
)
from timecapsulesmb.deploy.commands import RemoteAction
from timecapsulesmb.deploy.executor import run_remote_actions
from timecapsulesmb.services.callbacks import OperationCallbacks
from timecapsulesmb.services.runtime_verification import verify_managed_runtime_ready, wait_for_activation_settle
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
    wait_for_activation_settle(callbacks)
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

    Raises DeviceError when the started runtime does not become ready.
    """
    callbacks.stage("probe_runtime")
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
