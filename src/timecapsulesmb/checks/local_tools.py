from __future__ import annotations

from pathlib import Path

from timecapsulesmb.checks.models import CheckResult
from timecapsulesmb.deploy.artifacts import validate_artifacts
from timecapsulesmb.transport.local import command_exists


def check_required_local_tools() -> list[CheckResult]:
    results: list[CheckResult] = []
    for tool in ("smbclient", "ssh"):
        if command_exists(tool):
            results.append(CheckResult("PASS", f"found local tool {tool}"))
        else:
            status = "WARN" if tool == "ssh" else "FAIL"
            results.append(CheckResult(status, f"missing local tool {tool}"))
    return results


def check_required_artifacts(repo_root: Path) -> list[CheckResult]:
    results: list[CheckResult] = []
    for _, ok, message in validate_artifacts(repo_root):
        if ok:
            results.append(CheckResult("PASS", message))
        else:
            results.append(CheckResult("FAIL", message))
    return results
