from __future__ import annotations

from timecapsulesmb.deploy.planner import UninstallPlan
from timecapsulesmb.device.probe import (
    ManagedRuntimeProbeResult,
    probe_managed_runtime_conn,
    probe_paths_absent_conn,
)
from timecapsulesmb.transport.ssh import SshConnection


def verify_managed_runtime(
    connection: SshConnection,
    *,
    timeout_seconds: int = 180,
    heading: str | None = None,
) -> bool:
    if heading:
        print(heading)
    result: ManagedRuntimeProbeResult = probe_managed_runtime_conn(connection, timeout_seconds=timeout_seconds)
    for line in result.lines:
        if line.startswith("PASS:"):
            print(f"  ok: {line.removeprefix('PASS:')}")
        elif line.startswith("FAIL:"):
            print(f"  failed: {line.removeprefix('FAIL:')}")
        elif line:
            print(f"  {line}")
    return result.ready

def verify_post_uninstall(connection: SshConnection, plan: UninstallPlan) -> bool:
    print("Post-uninstall verification:")
    proc = probe_paths_absent_conn(connection, plan.verify_absent_targets)

    ok = proc.returncode == 0
    for line in proc.stdout.strip().splitlines():
        if line.startswith("ABSENT:"):
            print(f"  ok: removed {line.removeprefix('ABSENT:')}")
        elif line.startswith("PRESENT:"):
            print(f"  failed: still present {line.removeprefix('PRESENT:')}")
    return ok
