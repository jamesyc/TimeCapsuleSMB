from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from timecapsulesmb.device.probe import (
    ManagedRuntimeProbeResult,
    RcLocalAutostartProbeResult,
    probe_managed_runtime_conn,
    probe_netbsd4_rc_local_autostart_conn,
)
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
