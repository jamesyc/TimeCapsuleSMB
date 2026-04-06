from __future__ import annotations

from pathlib import Path

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
) -> tuple[list[CheckResult], bool]:
    results: list[CheckResult] = []

    if not env_exists:
        results.append(CheckResult("FAIL", f"missing {repo_root / '.env'}"))
    else:
        missing = missing_required_keys(values)
        if missing:
            results.append(CheckResult("FAIL", f".env is missing required keys: {', '.join(missing)}"))
        else:
            results.append(CheckResult("PASS", ".env contains all required keys"))

    results.extend(check_required_local_tools())
    results.extend(check_required_artifacts(repo_root))

    host = extract_host(values["TC_HOST"])

    if not skip_ssh:
        results.append(check_ssh_reachability(host))

    results.append(check_smb_port(host))

    if not skip_bonjour:
        try:
            bonjour_results, _, _ = run_bonjour_checks(values["TC_MDNS_INSTANCE_NAME"])
            results.extend(bonjour_results)
        except Exception as e:
            results.append(CheckResult("FAIL", f"Bonjour check failed: {e}"))

    if not skip_smb:
        results.append(
            check_authenticated_smb_listing(
                values["TC_SAMBA_USER"],
                values["TC_PASSWORD"],
                f"{values['TC_MDNS_HOST_LABEL']}.local",
            )
        )
        results.append(
            check_authenticated_smb_file_ops(
                values["TC_SAMBA_USER"],
                values["TC_PASSWORD"],
                f"{values['TC_MDNS_HOST_LABEL']}.local",
                values["TC_SHARE_NAME"],
            )
        )

    fatal = any(is_fatal(result) for result in results)
    return results, fatal
