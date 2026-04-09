from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Optional

from timecapsulesmb.checks.bonjour import run_bonjour_checks
from timecapsulesmb.checks.local_tools import check_required_artifacts, check_required_local_tools
from timecapsulesmb.checks.models import CheckResult, is_fatal
from timecapsulesmb.checks.network import check_smb_port, check_ssh_reachability
from timecapsulesmb.checks.smb import check_authenticated_smb_file_ops, check_authenticated_smb_listing
from timecapsulesmb.core.config import extract_host, missing_required_keys


def run_doctor_checks(
    values: dict[str, str],
    *,
    env_exists: bool,
    repo_root: Path,
    skip_ssh: bool = False,
    skip_bonjour: bool = False,
    skip_smb: bool = False,
    on_result: Optional[Callable[[CheckResult], None]] = None,
) -> tuple[list[CheckResult], bool]:
    results: list[CheckResult] = []

    def add_result(result: CheckResult) -> None:
        results.append(result)
        if on_result is not None:
            on_result(result)

    if not env_exists:
        add_result(CheckResult("FAIL", f"missing {repo_root / '.env'}"))
    else:
        missing = missing_required_keys(values)
        if missing:
            add_result(CheckResult("FAIL", f".env is missing required keys: {', '.join(missing)}"))
        else:
            add_result(CheckResult("PASS", ".env contains all required keys"))

    for result in check_required_local_tools():
        add_result(result)
    for result in check_required_artifacts(repo_root):
        add_result(result)

    host = extract_host(values["TC_HOST"])

    if not skip_ssh:
        add_result(check_ssh_reachability(host))

    add_result(check_smb_port(host))

    if not skip_bonjour:
        try:
            bonjour_results, _, _ = run_bonjour_checks(values["TC_MDNS_INSTANCE_NAME"])
            for result in bonjour_results:
                add_result(result)
        except Exception as e:
            add_result(CheckResult("FAIL", f"Bonjour check failed: {e}"))

    if not skip_smb:
        add_result(
            check_authenticated_smb_listing(
                values["TC_SAMBA_USER"],
                values["TC_PASSWORD"],
                f"{values['TC_MDNS_HOST_LABEL']}.local",
                expected_share_name=values["TC_SHARE_NAME"],
            )
        )
        add_result(
            check_authenticated_smb_file_ops(
                values["TC_SAMBA_USER"],
                values["TC_PASSWORD"],
                f"{values['TC_MDNS_HOST_LABEL']}.local",
                values["TC_SHARE_NAME"],
            )
        )

    fatal = any(is_fatal(result) for result in results)
    return results, fatal
