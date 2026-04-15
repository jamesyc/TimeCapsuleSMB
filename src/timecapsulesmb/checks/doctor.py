from __future__ import annotations

from collections.abc import Callable
import ipaddress
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
from timecapsulesmb.transport.local import find_free_local_port
from timecapsulesmb.transport.ssh import run_ssh, ssh_local_forward


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


def _parse_active_netbios_name(smb_conf: str) -> Optional[str]:
    for line in smb_conf.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ";")):
            continue
        key, separator, value = stripped.partition("=")
        if separator and key.strip().lower() == "netbios name":
            return value.strip()
    return None


def _parse_active_share_names(smb_conf: str) -> list[str]:
    shares: list[str] = []
    for line in smb_conf.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]") and len(stripped) > 2:
            section_name = stripped[1:-1].strip()
            if section_name and section_name.lower() != "global":
                shares.append(section_name)
    return shares


def _read_active_smb_conf(host: str, password: str, ssh_opts: str) -> str:
    smb_conf = "/mnt/Memory/samba4/etc/smb.conf"
    proc = run_ssh(
        host,
        password,
        ssh_opts,
        f"/bin/sh -c {shlex.quote(f'if [ -f {shlex.quote(smb_conf)} ]; then cat {shlex.quote(smb_conf)}; fi')}",
        check=False,
    )
    return proc.stdout


def _parse_bonjour_host_label(target: Optional[str]) -> Optional[str]:
    if not target:
        return None
    host = target.rsplit(":", 1)[0].rstrip(".")
    if not host:
        return None
    if host.endswith(".local"):
        return host[:-len(".local")]
    return host


def _configured_smb_server(host_label: str) -> str:
    value = host_label.strip()
    if not value:
        return value
    try:
        ipaddress.ip_address(value)
        return value
    except ValueError:
        pass
    if "." in value:
        return value
    return f"{value}.local"


def check_xattr_tdb_persistence(host: str, password: str, ssh_opts: str) -> CheckResult:
    smb_conf = "/mnt/Memory/samba4/etc/smb.conf"
    proc_stdout = _read_active_smb_conf(host, password, ssh_opts)
    if not proc_stdout.strip():
        return CheckResult("WARN", f"could not inspect active smb.conf at {smb_conf}")

    paths = _parse_xattr_tdb_paths(proc_stdout)
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
    bonjour_instance: Optional[str] = None
    bonjour_target: Optional[str] = None
    bonjour_reason = "Bonjour check not run"
    active_smb_conf: Optional[str] = None
    active_smb_conf_reason = "SSH check not run"

    if not skip_ssh:
        ssh_result = check_ssh_login(values["TC_HOST"], values["TC_PASSWORD"], ssh_opts)
        add_result(ssh_result)
        ssh_ok = ssh_result.status == "PASS"
    else:
        ssh_ok = True
        active_smb_conf_reason = "SSH check skipped"

    if not skip_ssh and ssh_ok:
        try:
            active_smb_conf = _read_active_smb_conf(
                values["TC_HOST"],
                values["TC_PASSWORD"],
                ssh_opts,
            )
            if not active_smb_conf.strip():
                active_smb_conf_reason = "active smb.conf unavailable"
            else:
                active_smb_conf_reason = ""
            add_result(
                check_xattr_tdb_persistence(
                    values["TC_HOST"],
                    values["TC_PASSWORD"],
                    ssh_opts,
                )
            )
        except (Exception, SystemExit) as e:
            active_smb_conf_reason = str(e)
            add_result(CheckResult("WARN", f"xattr_tdb:file check skipped: {e}"))
    elif not skip_ssh and not ssh_ok:
        active_smb_conf_reason = "SSH login failed"

    if proxied_ssh:
        add_result(CheckResult("SKIP", f"direct SMB port check skipped for SSH-proxied target {host}"))
    else:
        add_result(check_smb_port(host))

    if proxied_ssh and not skip_bonjour:
        bonjour_reason = "Bonjour check skipped for SSH-proxied target"
        add_result(CheckResult("SKIP", "Bonjour check skipped for SSH-proxied target; local mDNS may find a different Time Capsule"))
    elif not skip_bonjour:
        try:
            bonjour_results, bonjour_instance, bonjour_target = run_bonjour_checks(values["TC_MDNS_INSTANCE_NAME"])
            bonjour_reason = ""
            for result in bonjour_results:
                add_result(result)
        except Exception as e:
            bonjour_reason = str(e)
            add_result(CheckResult("FAIL", f"Bonjour check failed: {e}"))
    else:
        bonjour_reason = "Bonjour check skipped"

    if bonjour_instance is not None:
        add_result(CheckResult("INFO", f"advertised Bonjour instance: {bonjour_instance}"))
    else:
        add_result(CheckResult("INFO", f"advertised Bonjour instance: unavailable ({bonjour_reason})"))

    bonjour_host_label = _parse_bonjour_host_label(bonjour_target)
    if bonjour_host_label is not None:
        add_result(CheckResult("INFO", f"advertised Bonjour host label: {bonjour_host_label}"))
    else:
        add_result(CheckResult("INFO", f"advertised Bonjour host label: unavailable ({bonjour_reason})"))

    if active_smb_conf and active_smb_conf.strip():
        active_netbios = _parse_active_netbios_name(active_smb_conf)
        share_names = _parse_active_share_names(active_smb_conf)
        if active_netbios is not None:
            add_result(CheckResult("INFO", f"active Samba NetBIOS name: {active_netbios}"))
        else:
            add_result(CheckResult("INFO", "active Samba NetBIOS name: unavailable (netbios name not found in active smb.conf)"))
        if share_names:
            add_result(CheckResult("INFO", f"active Samba share names: {', '.join(share_names)}"))
        else:
            add_result(CheckResult("INFO", "active Samba share names: unavailable (no share sections found in active smb.conf)"))
    else:
        add_result(CheckResult("INFO", f"active Samba NetBIOS name: unavailable ({active_smb_conf_reason})"))
        add_result(CheckResult("INFO", f"active Samba share names: unavailable ({active_smb_conf_reason})"))

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
        local_port = find_free_local_port()
        try:
            with ssh_local_forward(
                values["TC_HOST"],
                values["TC_PASSWORD"],
                values["TC_SSH_OPTS"],
                local_port=local_port,
                remote_host=host,
                remote_port=445,
            ):
                add_result(
                    check_authenticated_smb_listing(
                        values["TC_SAMBA_USER"],
                        values["TC_PASSWORD"],
                        "127.0.0.1",
                        expected_share_name=values["TC_SHARE_NAME"],
                        port=local_port,
                    )
                )
                for result in check_authenticated_smb_file_ops_detailed(
                    values["TC_SAMBA_USER"],
                    values["TC_PASSWORD"],
                    "127.0.0.1",
                    values["TC_SHARE_NAME"],
                    port=local_port,
                ):
                    add_result(result)
        except (Exception, SystemExit) as e:
            add_result(CheckResult("FAIL", f"authenticated SMB checks failed through SSH tunnel: {e}"))
    elif not skip_smb:
        smb_server = _configured_smb_server(values["TC_MDNS_HOST_LABEL"])
        add_result(
            check_authenticated_smb_listing(
                values["TC_SAMBA_USER"],
                values["TC_PASSWORD"],
                smb_server,
                expected_share_name=values["TC_SHARE_NAME"],
            )
        )
        for result in check_authenticated_smb_file_ops_detailed(
            values["TC_SAMBA_USER"],
            values["TC_PASSWORD"],
            smb_server,
            values["TC_SHARE_NAME"],
        ):
            add_result(result)

    fatal = any(is_fatal(result) for result in results)
    return results, fatal
