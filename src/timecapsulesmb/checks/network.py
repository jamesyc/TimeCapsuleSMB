from __future__ import annotations

import shlex

from timecapsulesmb.checks.models import CheckResult
from timecapsulesmb.transport.local import tcp_open
from timecapsulesmb.transport.ssh import run_ssh


def ssh_opts_use_proxy(ssh_opts: str) -> bool:
    try:
        tokens = shlex.split(ssh_opts)
    except ValueError:
        tokens = ssh_opts.split()

    for token in tokens:
        if token in ("-J",):
            return True
        if token.startswith("-J"):
            return True
        if token in ("ProxyCommand", "ProxyJump"):
            return True
        if token.startswith("ProxyCommand=") or token.startswith("ProxyJump="):
            return True
        if token.startswith("-oProxyCommand=") or token.startswith("-oProxyJump="):
            return True

    return False


def check_ssh_reachability(host: str) -> CheckResult:
    if tcp_open(host, 22):
        return CheckResult("PASS", f"SSH reachable at {host}:22")
    return CheckResult("FAIL", f"SSH not reachable at {host}:22")


def check_ssh_login(target: str, password: str, ssh_opts: str) -> CheckResult:
    try:
        proc = run_ssh(target, password, ssh_opts, "/bin/echo ok", check=False, timeout=30)
    except SystemExit as e:
        return CheckResult("FAIL", str(e))
    if proc.returncode == 0 and proc.stdout.strip().endswith("ok"):
        return CheckResult("PASS", f"SSH command works for {target}")
    detail = proc.stdout.strip() or f"rc={proc.returncode}"
    return CheckResult("FAIL", f"SSH command failed for {target}: {detail}")


def check_smb_port(host: str) -> CheckResult:
    if tcp_open(host, 445):
        return CheckResult("PASS", f"SMB reachable at {host}:445")
    return CheckResult("WARN", f"SMB not reachable at {host}:445")
