from __future__ import annotations

from timecapsulesmb.checks.models import CheckResult
from timecapsulesmb.device.probe import probe_ssh_command_conn
from timecapsulesmb.transport.local import tcp_connect_error
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
    connect_error = tcp_connect_error(host, 445)
    if connect_error is None:
        return CheckResult("PASS", f"SMB reachable at {host}:445")
    return CheckResult("WARN", f"SMB not reachable at {host}:445 ({connect_error})", {"error": connect_error})
