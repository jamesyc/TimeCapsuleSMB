from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Optional
import shlex

from timecapsulesmb.checks.bonjour import run_bonjour_checks
from timecapsulesmb.checks.local_tools import check_required_artifacts, check_required_local_tools
from timecapsulesmb.checks.models import CheckResult, is_fatal
from timecapsulesmb.checks.network import check_smb_port, check_ssh_login, ssh_opts_use_proxy
from timecapsulesmb.checks.nbns import check_nbns_name_resolution
from timecapsulesmb.checks.smb import (
    check_authenticated_smb_file_ops_detailed,
    check_authenticated_smb_listing,
)
from timecapsulesmb.core.config import extract_host, missing_required_keys
from timecapsulesmb.device.probe import build_device_paths, discover_volume_root
from timecapsulesmb.transport.ssh import run_ssh


def _read_interface_ipv4(host: str, password: str, ssh_opts: str, iface: str) -> str:
    probe_cmd = (
        f"/sbin/ifconfig {shlex.quote(iface)} 2>/dev/null | "
        "sed -n 's/^[[:space:]]*inet[[:space:]]\\([0-9.]*\\).*/\\1/p' | "
        "sed -n '1p'"
    )
    proc = run_ssh(
        host,
        password,
        ssh_opts,
        f"/bin/sh -c {shlex.quote(probe_cmd)}",
        check=False,
    )
    iface_ip = proc.stdout.strip()
    if not iface_ip:
        raise SystemExit(f"could not determine IPv4 for interface {iface}")
    return iface_ip


def _parse_xattr_tdb_paths(smb_conf: str) -> list[str]:
    paths: list[str] = []
    for line in smb_conf.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ";")):
            continue
        key, separator, value = stripped.partition("=")
        if separator and key.strip().lower() == "xattr_tdb:file":
            paths.append(value.strip())
    return paths


def check_xattr_tdb_persistence(host: str, password: str, ssh_opts: str) -> CheckResult:
    smb_conf = "/mnt/Memory/samba4/etc/smb.conf"
    proc = run_ssh(
        host,
        password,
        ssh_opts,
        f"/bin/sh -c {shlex.quote(f'if [ -f {shlex.quote(smb_conf)} ]; then cat {shlex.quote(smb_conf)}; fi')}",
        check=False,
    )
    if not proc.stdout.strip():
        return CheckResult("WARN", f"could not inspect active smb.conf at {smb_conf}")

    paths = _parse_xattr_tdb_paths(proc.stdout)
    if not paths:
        return CheckResult("WARN", "active smb.conf does not contain xattr_tdb:file")

    memory_paths = [path for path in paths if path == "/mnt/Memory" or path.startswith("/mnt/Memory/")]
    if memory_paths:
        return CheckResult("FAIL", f"xattr_tdb:file points at non-persistent ramdisk: {', '.join(memory_paths)}")

    return CheckResult("PASS", f"xattr_tdb:file is persistent: {', '.join(paths)}")


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
        env_valid = False
    else:
        missing = missing_required_keys(values)
        if missing:
            add_result(CheckResult("FAIL", f".env is missing required keys: {', '.join(missing)}"))
            env_valid = False
        else:
            add_result(CheckResult("PASS", ".env contains all required keys"))
            env_valid = True

    for result in check_required_local_tools():
        add_result(result)
    for result in check_required_artifacts(repo_root):
        add_result(result)

    if not env_valid:
        return results, any(is_fatal(result) for result in results)

    host = extract_host(values["TC_HOST"])
    ssh_opts = values.get("TC_SSH_OPTS", "")
    proxied_ssh = ssh_opts_use_proxy(ssh_opts)
    ssh_ok = False

    if not skip_ssh:
        ssh_result = check_ssh_login(values["TC_HOST"], values["TC_PASSWORD"], ssh_opts)
        add_result(ssh_result)
        ssh_ok = ssh_result.status == "PASS"
    else:
        ssh_ok = True

    if not skip_ssh and ssh_ok:
        try:
            add_result(
                check_xattr_tdb_persistence(
                    values["TC_HOST"],
                    values["TC_PASSWORD"],
                    ssh_opts,
                )
            )
        except (Exception, SystemExit) as e:
            add_result(CheckResult("WARN", f"xattr_tdb:file check skipped: {e}"))

    if proxied_ssh:
        add_result(CheckResult("SKIP", f"direct SMB port check skipped for SSH-proxied target {host}"))
    else:
        add_result(check_smb_port(host))

    if proxied_ssh and not skip_bonjour:
        add_result(CheckResult("SKIP", "Bonjour check skipped for SSH-proxied target; local mDNS may find a different Time Capsule"))
    elif not skip_bonjour:
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
                if proxied_ssh:
                    add_result(CheckResult("SKIP", "NBNS check skipped for SSH-proxied target; UDP/137 is not reachable through the SSH jump host"))
                else:
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

    if proxied_ssh and not skip_smb:
        add_result(CheckResult("SKIP", "authenticated SMB checks skipped for SSH-proxied target; TCP/445 is not reachable through the SSH jump host"))
    elif not skip_smb:
        add_result(
            check_authenticated_smb_listing(
                values["TC_SAMBA_USER"],
                values["TC_PASSWORD"],
                f"{values['TC_MDNS_HOST_LABEL']}.local",
                expected_share_name=values["TC_SHARE_NAME"],
            )
        )
        for result in check_authenticated_smb_file_ops_detailed(
            values["TC_SAMBA_USER"],
            values["TC_PASSWORD"],
            f"{values['TC_MDNS_HOST_LABEL']}.local",
            values["TC_SHARE_NAME"],
        ):
            add_result(result)

    fatal = any(is_fatal(result) for result in results)
    return results, fatal
