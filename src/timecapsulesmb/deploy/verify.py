from __future__ import annotations

from dataclasses import dataclass

from timecapsulesmb.deploy.planner import UninstallPlan
from timecapsulesmb.device.probe import (
    ManagedRuntimeProbeResult,
    probe_paths_absent_conn,
)
from timecapsulesmb.transport.ssh import SshConnection


@dataclass(frozen=True)
class VerificationResult:
    ok: bool
    lines: tuple[str, ...]

    def __bool__(self) -> bool:
        return self.ok


def render_managed_runtime_verification(
    result: ManagedRuntimeProbeResult,
    *,
    heading: str | None = None,
) -> list[str]:
    lines: list[str] = []
    if heading:
        lines.append(heading)
    for step in result.steps:
        if step.status == "pass":
            lines.append(f"  ok: {step.detail}")
        elif step.status == "skip":
            lines.append(f"  skipped: {step.detail}")
        elif step.detail:
            lines.append(f"  failed: {step.detail}")
    return lines


def verify_post_uninstall(connection: SshConnection, plan: UninstallPlan) -> VerificationResult:
    proc = probe_paths_absent_conn(connection, plan.verify_absent_targets)

    ok = proc.returncode == 0
    return VerificationResult(ok=ok, lines=tuple(proc.stdout.strip().splitlines()))


def render_post_uninstall_verification(result: VerificationResult) -> list[str]:
    lines = ["Post-uninstall verification:"]
    for line in result.lines:
        if line.startswith("ABSENT:"):
            lines.append(f"  ok: removed {line.removeprefix('ABSENT:')}")
        elif line.startswith("PRESENT:"):
            lines.append(f"  failed: still present {line.removeprefix('PRESENT:')}")
        elif line:
            lines.append(f"  {line}")
    return lines
