from __future__ import annotations

import subprocess

from timecapsulesmb.checks.models import CheckResult
from timecapsulesmb.transport.local import command_exists, run_local_capture


def check_authenticated_smb_listing(username: str, password: str, server: str, *, timeout: int = 20) -> CheckResult:
    if not command_exists("smbutil"):
        return CheckResult("FAIL", "missing local tool smbutil")

    proc = run_local_capture(["smbutil", "view", f"//{username}:{password}@{server}"], timeout=timeout)
    if proc.returncode == 0:
        return CheckResult("PASS", f"authenticated SMB listing works for {username}@{server}")
    detail = (proc.stderr or proc.stdout).strip().splitlines()
    msg = detail[-1] if detail else f"failed with rc={proc.returncode}"
    return CheckResult("FAIL", f"authenticated SMB listing failed: {msg}")


def try_authenticated_smb_listing(username: str, password: str, servers: list[str], *, timeout: int = 12) -> CheckResult:
    if not command_exists("smbutil"):
        return CheckResult("WARN", "SMB listing verification skipped: smbutil not found")

    failure_msg = "not attempted"
    for server in servers:
        try:
            proc = run_local_capture(["smbutil", "view", f"//{username}:{password}@{server}"], timeout=timeout)
        except subprocess.TimeoutExpired:
            failure_msg = f"timed out via {server}"
            continue
        if proc.returncode == 0:
            return CheckResult("PASS", f"authenticated SMB listing works for {username}@{server}")
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        failure_msg = detail[-1] if detail else f"failed with rc={proc.returncode} via {server}"
    return CheckResult("FAIL", f"authenticated SMB listing failed: {failure_msg}")
