from __future__ import annotations

import os
from pathlib import Path

from timecapsulesmb.checks.models import CheckResult
from timecapsulesmb.deploy.artifacts import validate_artifacts
from timecapsulesmb.transport.local import command_exists
from timecapsulesmb.transport.ssh_client import require_local_ssh
from timecapsulesmb.transport.errors import SshError
from timecapsulesmb.transport.ssh import SSH_ASKPASS_PATH


def check_required_local_tools() -> list[CheckResult]:
    results: list[CheckResult] = []
    for tool in ("ssh", "smbclient"):
        if command_exists(tool):
            if tool == "ssh":
                try:
                    require_local_ssh()
                except SshError as exc:
                    results.append(CheckResult("FAIL", str(exc)))
                    continue
            results.append(CheckResult("PASS", f"found local tool {tool}"))
        else:
            results.append(CheckResult("FAIL", f"missing local tool {tool}, please install {tool} on your computer"))
    # ssh gets the device password from this helper, installed with TimeCapsuleSMB.
    if os.access(SSH_ASKPASS_PATH, os.X_OK):
        results.append(CheckResult("PASS", "found local tool ssh-askpass, the SSH password helper"))
    else:
        results.append(CheckResult(
            "FAIL",
            f"local tool ssh-askpass, the SSH password helper, is missing or not executable at {SSH_ASKPASS_PATH}; "
            "reinstall TimeCapsuleSMB",
        ))
    return results


def check_required_artifacts(repo_root: Path) -> list[CheckResult]:
    results: list[CheckResult] = []
    for _, ok, message in validate_artifacts(repo_root):
        if ok:
            results.append(CheckResult("PASS", message))
        else:
            results.append(CheckResult("FAIL", message))
    return results
