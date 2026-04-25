from __future__ import annotations

from timecapsulesmb.checks.models import CheckResult
from timecapsulesmb.device.probe import probe_ssh_command_conn
from timecapsulesmb.transport.local import tcp_open
from timecapsulesmb.transport.ssh import SshConnection, ssh_opts_use_proxy


def check_ssh_login(connection: SshConnection) -> CheckResult:
    result = probe_ssh_command_conn(
        connection,
        "/bin/echo ok",
        timeout=30,
        expected_stdout_suffix="ok",
    )
    if result.ok:
        return CheckResult("PASS", f"SSH command works for {connection.host}")
    if result.detail.startswith("Connecting to the device failed, SSH error:"):
        return CheckResult("FAIL", result.detail)
    return CheckResult("FAIL", f"SSH command failed for {connection.host}: {result.detail}")


def check_smb_port(host: str) -> CheckResult:
    if tcp_open(host, 445):
        return CheckResult("PASS", f"SMB reachable at {host}:445")
    return CheckResult("WARN", f"SMB not reachable at {host}:445")
