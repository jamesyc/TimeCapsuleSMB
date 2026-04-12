from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Optional
import shlex

from timecapsulesmb.checks.bonjour import run_bonjour_checks
from timecapsulesmb.checks.local_tools import check_required_artifacts, check_required_local_tools
from timecapsulesmb.checks.models import CheckResult, is_fatal
from timecapsulesmb.checks.network import check_smb_port, check_ssh_reachability
from timecapsulesmb.checks.nbns import check_nbns_name_resolution
from timecapsulesmb.checks.smb import check_authenticated_smb_file_ops, check_authenticated_smb_listing
from timecapsulesmb.core.config import extract_host, missing_required_keys
from timecapsulesmb.device.probe import build_device_paths, discover_volume_root
from timecapsulesmb.transport.ssh import run_ssh


def _read_interface_ipv4(host: str, password: str, ssh_opts: str, iface: str) -> str:
    proc = run_ssh(
        host,
        password,
        ssh_opts,
        f"/bin/sh -c {shlex.quote(f'/sbin/ifconfig {shlex.quote(iface)} 2>/dev/null | sed -n \"s/^[[:space:]]*inet[[:space:]]\\\\([0-9.]*\\\\).*/\\\\1/p\" | sed -n \"1p\"')}",
        check=False,
    )
    iface_ip = proc.stdout.strip()
    if not iface_ip:
        raise SystemExit(f"could not determine IPv4 for interface {iface}")
    return iface_ip


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
    ssh_ok = False

    if not skip_ssh:
        ssh_result = check_ssh_reachability(host)
        add_result(ssh_result)
        ssh_ok = ssh_result.status == "PASS"
    else:
        ssh_ok = True

    add_result(check_smb_port(host))

    if not skip_bonjour:
        try:
            bonjour_results, _, _ = run_bonjour_checks(values["TC_MDNS_INSTANCE_NAME"])
            for result in bonjour_results:
                add_result(result)
        except Exception as e:
            add_result(CheckResult("FAIL", f"Bonjour check failed: {e}"))

    if not skip_ssh and ssh_ok:
        try:
            volume_root = discover_volume_root(values["TC_HOST"], values["TC_PASSWORD"], values["TC_SSH_OPTS"])
            device_paths = build_device_paths(volume_root, values["TC_PAYLOAD_DIR_NAME"])
            marker_path = f"{device_paths.payload_dir}/private/nbns.enabled"
            proc = run_ssh(
                values["TC_HOST"],
                values["TC_PASSWORD"],
                values["TC_SSH_OPTS"],
                f"/bin/sh -c {shlex.quote(f'if [ -f {shlex.quote(marker_path)} ]; then echo enabled; fi')}",
                check=False,
            )
            if proc.stdout.strip() == "enabled":
                expected_ip = _read_interface_ipv4(
                    values["TC_HOST"],
                    values["TC_PASSWORD"],
                    values["TC_SSH_OPTS"],
                    values["TC_NET_IFACE"],
                )
                add_result(check_nbns_name_resolution(values["TC_NETBIOS_NAME"], host, expected_ip))
            else:
                add_result(CheckResult("SKIP", "NBNS responder not enabled"))
        except (Exception, SystemExit) as e:
            add_result(CheckResult("WARN", f"NBNS check skipped: {e}"))

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
