from __future__ import annotations

from timecapsulesmb.checks.models import CheckResult
from timecapsulesmb.transport.local import tcp_open


def check_ssh_reachability(host: str) -> CheckResult:
    if tcp_open(host, 22):
        return CheckResult("PASS", f"SSH reachable at {host}:22")
    return CheckResult("FAIL", f"SSH not reachable at {host}:22")


def check_smb_port(host: str) -> CheckResult:
    if tcp_open(host, 445):
        return CheckResult("PASS", f"SMB reachable at {host}:445")
    return CheckResult("WARN", f"SMB not reachable at {host}:445")
