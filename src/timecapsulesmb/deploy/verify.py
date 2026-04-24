from __future__ import annotations

from timecapsulesmb.deploy.planner import UninstallPlan
from timecapsulesmb.device.probe import (
    ManagedRuntimeProbeResult,
    probe_managed_runtime_conn,
    probe_paths_absent_conn,
)
from timecapsulesmb.transport.ssh import SshConnection


def _connection_from_args(connection_or_host: SshConnection | str, password: str | None, ssh_opts: str | None) -> SshConnection:
    if isinstance(connection_or_host, SshConnection):
        return connection_or_host
    if password is None or ssh_opts is None:
        raise TypeError("password and ssh_opts are required when passing host string")
    return SshConnection(connection_or_host, password, ssh_opts)

def verify_managed_runtime(
    connection: SshConnection | str,
    password: str | None = None,
    ssh_opts: str | None = None,
    *,
    timeout_seconds: int = 180,
    heading: str | None = None,
) -> bool:
    connection = _connection_from_args(connection, password, ssh_opts)
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

def verify_post_uninstall(connection: SshConnection | str, password_or_plan, ssh_opts: str | None = None, plan: UninstallPlan | None = None) -> bool:
    if isinstance(connection, SshConnection):
        resolved_connection = connection
        resolved_plan = password_or_plan
    else:
        resolved_connection = _connection_from_args(connection, password_or_plan, ssh_opts)
        if plan is None:
            raise TypeError("plan is required when passing host string")
        resolved_plan = plan
    print("Post-uninstall verification:")
    proc = probe_paths_absent_conn(resolved_connection, resolved_plan.verify_absent_targets)

    ok = proc.returncode == 0
    for line in proc.stdout.strip().splitlines():
        if line.startswith("ABSENT:"):
            print(f"  ok: removed {line.removeprefix('ABSENT:')}")
        elif line.startswith("PRESENT:"):
            print(f"  failed: still present {line.removeprefix('PRESENT:')}")
    return ok
