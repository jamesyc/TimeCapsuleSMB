from __future__ import annotations

from dataclasses import dataclass

from timecapsulesmb.deploy.planner import UninstallPlan
from timecapsulesmb.device.probe import (
    ManagedRuntimeProbeResult,
    probe_managed_runtime_conn,
    probe_paths_absent_conn,
)
from timecapsulesmb.transport.ssh import SshConnection


@dataclass(frozen=True)
class RuntimeVerificationResult:
    ready: bool
    detail: str
    lines: tuple[str, ...]

    def __bool__(self) -> bool:
        return self.ready


@dataclass(frozen=True)
class PostUninstallVerificationResult:
    ok: bool
    lines: tuple[str, ...]

    def __bool__(self) -> bool:
        return self.ok


def verify_managed_runtime(
    connection: SshConnection,
    *,
    timeout_seconds: int = 180,
) -> RuntimeVerificationResult:
    result: ManagedRuntimeProbeResult = probe_managed_runtime_conn(connection, timeout_seconds=timeout_seconds)
    return RuntimeVerificationResult(ready=result.ready, detail=result.detail, lines=result.lines)


def render_managed_runtime_verification(
    result: RuntimeVerificationResult | bool,
    *,
    heading: str | None = None,
) -> list[str]:
    if not isinstance(result, RuntimeVerificationResult):
        return []
    lines: list[str] = []
    if heading:
        lines.append(heading)
    for line in result.lines:
        if line.startswith("PASS:"):
            lines.append(f"  ok: {line.removeprefix('PASS:')}")
        elif line.startswith("FAIL:"):
            lines.append(f"  failed: {line.removeprefix('FAIL:')}")
        elif line:
            lines.append(f"  {line}")
    return lines



def verify_post_uninstall(connection: SshConnection, plan: UninstallPlan) -> PostUninstallVerificationResult:
    proc = probe_paths_absent_conn(connection, plan.verify_absent_targets)

    ok = proc.returncode == 0
    return PostUninstallVerificationResult(ok=ok, lines=tuple(proc.stdout.strip().splitlines()))


def render_post_uninstall_verification(result: PostUninstallVerificationResult | bool) -> list[str]:
    if not isinstance(result, PostUninstallVerificationResult):
        return []
    lines = ["Post-uninstall verification:"]
    for line in result.lines:
        if line.startswith("ABSENT:"):
            lines.append(f"  ok: removed {line.removeprefix('ABSENT:')}")
        elif line.startswith("PRESENT:"):
            lines.append(f"  failed: still present {line.removeprefix('PRESENT:')}")
        elif line:
            lines.append(f"  {line}")
    return lines
