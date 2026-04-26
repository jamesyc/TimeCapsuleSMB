from __future__ import annotations

from collections.abc import Callable, Iterable
import ipaddress
from pathlib import Path
from typing import Optional

from timecapsulesmb.checks.bonjour import (
    BonjourServiceTarget,
    browse_smb_instances,
    build_bonjour_expected_identity,
    check_bonjour_host_ip,
    check_smb_instance,
    check_smb_service_target,
    resolve_smb_instance,
    resolve_smb_service_target,
    select_smb_instance,
)
from timecapsulesmb.checks.local_tools import check_required_artifacts, check_required_local_tools
from timecapsulesmb.checks.models import CheckResult, is_fatal
from timecapsulesmb.checks.network import check_smb_port, check_ssh_login, ssh_opts_use_proxy
from timecapsulesmb.checks.nbns import check_nbns_name_resolution
from timecapsulesmb.checks.smb import (
    check_authenticated_smb_listing,
    check_authenticated_smb_file_ops_detailed,
)
from timecapsulesmb.cli.runtime import probe_connection_state, resolve_env_connection
from timecapsulesmb.core.config import extract_host, missing_required_keys, validate_config_values
from timecapsulesmb.device.compat import is_netbsd4_payload_family, is_netbsd6_payload_family, render_compatibility_message
from timecapsulesmb.device.probe import (
    ProbedDeviceState,
    RUNTIME_SMB_CONF,
    build_device_paths,
    discover_volume_root_conn,
    probe_managed_mdns_takeover_conn,
    probe_managed_smbd_conn,
    nbns_marker_enabled_conn,
    probe_remote_interface_conn,
    read_active_smb_conf_conn,
    read_interface_ipv4_conn,
)
from timecapsulesmb.transport.local import find_free_local_port
from timecapsulesmb.transport.local import command_exists
from timecapsulesmb.transport.ssh import SshConnection, ssh_local_forward


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


def _add_probe_line_results(
    add_result: Callable[[CheckResult], None],
    lines: Iterable[str],
    *,
    fallback_ready: bool,
    fallback_pass_message: str,
    fallback_fail_message: str,
) -> None:
    emitted = False
    for line in lines:
        if line.startswith("PASS:"):
            add_result(CheckResult("PASS", line.removeprefix("PASS:")))
            emitted = True
        elif line.startswith("FAIL:"):
            add_result(CheckResult("FAIL", line.removeprefix("FAIL:")))
            emitted = True

    if emitted:
        return

    if fallback_ready:
        add_result(CheckResult("PASS", fallback_pass_message))
    else:
        add_result(CheckResult("FAIL", fallback_fail_message))


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


def _doctor_smb_servers(values: dict[str, str], bonjour_target: BonjourServiceTarget | None) -> list[str]:
    ordered: list[str] = []

    def add(value: Optional[str]) -> None:
        if value and value not in ordered:
            ordered.append(value)

    add(_configured_smb_server(values["TC_MDNS_HOST_LABEL"]))
    add(bonjour_target.hostname if bonjour_target is not None else None)
    add(extract_host(values["TC_HOST"]))
    return ordered


def check_xattr_tdb_persistence(connection: SshConnection) -> CheckResult:
    proc_stdout = read_active_smb_conf_conn(connection)
    if not proc_stdout.strip():
        return CheckResult("WARN", f"could not inspect active smb.conf at {RUNTIME_SMB_CONF}")

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
    precomputed_probe_state: ProbedDeviceState | None = None,
    skip_ssh: bool = False,
    skip_bonjour: bool = False,
    skip_smb: bool = False,
    on_result: Optional[Callable[[CheckResult], None]] = None,
) -> tuple[list[CheckResult], bool]:
    results: list[CheckResult] = []
    env_valid = False

    def add_result(result: CheckResult) -> None:
        results.append(result)
        if on_result is not None:
            on_result(result)

    def add_sshpass_result_for_payload(payload_family: str | None) -> None:
        if command_exists("sshpass"):
            add_result(CheckResult("PASS", "found local tool sshpass"))
            return
        if is_netbsd4_payload_family(payload_family):
            add_result(CheckResult("FAIL", "missing local tool sshpass; NetBSD4 upload fallback requires sshpass"))
            return
        if is_netbsd6_payload_family(payload_family):
            add_result(CheckResult("INFO", "local sshpass not installed; not needed for this NetBSD6 target unless remote scp is unavailable"))
            return
        add_result(CheckResult("INFO", "local sshpass not installed; target upload fallback requirement unknown"))

    for result in check_required_local_tools():
        add_result(result)
    for result in check_required_artifacts(repo_root):
        add_result(result)

    if not env_exists:
        add_result(CheckResult("FAIL", f"missing {repo_root / '.env'}"))
    else:
        missing = missing_required_keys(values)
        if missing:
            add_result(CheckResult("FAIL", f".env is missing required keys: {', '.join(missing)}"))
        else:
            validation_errors = validate_config_values(values, profile="doctor")
            if validation_errors:
                for error in validation_errors:
                    add_result(CheckResult("FAIL", error.format_for_cli().replace("\n", " ")))
            else:
                add_result(CheckResult("PASS", ".env contains all required keys"))
                env_valid = True

    if not env_valid:
        return results, any(is_fatal(result) for result in results)

    connection = resolve_env_connection(values)
    host = extract_host(connection.host)
    smb_password = values["TC_PASSWORD"]
    proxied_ssh = ssh_opts_use_proxy(connection.ssh_opts)
    ssh_ok = False
    bonjour_instance: Optional[str] = None
    bonjour_target: BonjourServiceTarget | None = None
    bonjour_reason = "Bonjour check not run"
    active_smb_conf: Optional[str] = None
    active_smb_conf_reason = "SSH check not run"

    if not skip_ssh:
        ssh_result = check_ssh_login(connection)
        add_result(ssh_result)
        ssh_ok = ssh_result.status == "PASS"
    else:
        ssh_ok = True
        active_smb_conf_reason = "SSH check skipped"

    if not skip_ssh and ssh_ok:
        interface_probe = probe_remote_interface_conn(connection, values["TC_NET_IFACE"])
        if not interface_probe.exists:
            add_result(CheckResult("FAIL", f"TC_NET_IFACE is invalid. Run the `configure` command again. {interface_probe.detail}."))
        try:
            probed_state = precomputed_probe_state or probe_connection_state(connection)
            probe_result = probed_state.probe_result
            compatibility = probed_state.compatibility
            if compatibility is None:
                add_result(CheckResult("FAIL", probe_result.error or "could not determine device compatibility"))
            elif compatibility.supported:
                add_result(CheckResult("PASS", render_compatibility_message(compatibility)))
                add_sshpass_result_for_payload(compatibility.payload_family)
            else:
                add_result(CheckResult("FAIL", render_compatibility_message(compatibility)))
        except (Exception, SystemExit) as e:
            add_result(CheckResult("FAIL", f"device compatibility check failed: {e}"))
        smbd_probe = probe_managed_smbd_conn(connection)
        _add_probe_line_results(
            add_result,
            getattr(smbd_probe, "lines", ()),
            fallback_ready=smbd_probe.ready,
            fallback_pass_message="managed smbd is ready",
            fallback_fail_message=f"managed smbd is not ready ({smbd_probe.detail})",
        )
        mdns_probe = probe_managed_mdns_takeover_conn(connection)
        if mdns_probe.ready:
            add_result(CheckResult("PASS", "managed mDNS takeover is active"))
        else:
            add_result(CheckResult("FAIL", f"managed mDNS takeover is not active ({mdns_probe.detail})"))
        try:
            active_smb_conf = read_active_smb_conf_conn(connection)
            if not active_smb_conf.strip():
                active_smb_conf_reason = "active smb.conf unavailable"
            else:
                active_smb_conf_reason = ""
            add_result(
                check_xattr_tdb_persistence(connection)
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
            bonjour_expected = build_bonjour_expected_identity(values)
            smb_instances, discovery_error = browse_smb_instances()
            bonjour_reason = ""
            if discovery_error is not None:
                bonjour_reason = discovery_error.message
                add_result(discovery_error)
            else:
                selection = select_smb_instance(
                    smb_instances,
                    expected_instance_name=bonjour_expected.instance_name,
                )
                for result in check_smb_instance(selection):
                    add_result(result)
                if selection.instance is not None:
                    bonjour_instance = selection.instance.name
                    resolved_record, resolve_error = resolve_smb_instance(selection.instance)
                    if resolve_error is not None:
                        bonjour_reason = resolve_error.message
                        add_result(resolve_error)
                    elif resolved_record is not None:
                        target = resolve_smb_service_target(
                            resolved_record,
                            expected_instance_name=bonjour_expected.instance_name,
                        )
                        target_result = check_smb_service_target(target)
                        add_result(target_result)
                        if target.hostname:
                            bonjour_target = target
                            record_ips = list(getattr(resolved_record, "ipv4", []) or [])
                            add_result(
                                check_bonjour_host_ip(
                                    target.hostname,
                                    expected_ip=bonjour_expected.target_ip,
                                    record_ips=record_ips,
                                )
                            )
        except Exception as e:
            bonjour_reason = str(e)
            add_result(CheckResult("FAIL", f"Bonjour check failed: {e}"))
    else:
        bonjour_reason = "Bonjour check skipped"

    if bonjour_instance is not None:
        add_result(CheckResult("INFO", f"advertised Bonjour instance: {bonjour_instance}"))
    else:
        add_result(CheckResult("INFO", f"advertised Bonjour instance: unavailable ({bonjour_reason})"))

    bonjour_host_label = bonjour_target.host_label() if bonjour_target is not None else None
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
            volume_root = discover_volume_root_conn(connection)
            device_paths = build_device_paths(volume_root, values["TC_PAYLOAD_DIR_NAME"])
            if nbns_marker_enabled_conn(connection, device_paths.payload_dir):
                if proxied_ssh:
                    add_result(CheckResult("SKIP", "NBNS check skipped for SSH-proxied target; UDP/137 is not reachable through the SSH jump host"))
                else:
                    expected_ip = read_interface_ipv4_conn(connection, values["TC_NET_IFACE"])
                    add_result(check_nbns_name_resolution(values["TC_NETBIOS_NAME"], host, expected_ip))
            else:
                add_result(CheckResult("SKIP", "NBNS responder not enabled"))
        except (Exception, SystemExit) as e:
            add_result(CheckResult("WARN", f"NBNS check skipped: {e}"))

    if proxied_ssh and not skip_smb:
        local_port = find_free_local_port()
        try:
            with ssh_local_forward(
                connection,
                local_port=local_port,
                remote_host=host,
                remote_port=445,
            ):
                add_result(
                    check_authenticated_smb_listing(
                        values["TC_SAMBA_USER"],
                        smb_password,
                        "127.0.0.1",
                        expected_share_name=values["TC_SHARE_NAME"],
                        port=local_port,
                    )
                )
                for result in check_authenticated_smb_file_ops_detailed(
                    values["TC_SAMBA_USER"],
                    smb_password,
                    "127.0.0.1",
                    values["TC_SHARE_NAME"],
                    port=local_port,
                ):
                    add_result(result)
        except (Exception, SystemExit) as e:
            add_result(CheckResult("FAIL", f"authenticated SMB checks failed through SSH tunnel: {e}"))
    elif not skip_smb:
        smb_servers = _doctor_smb_servers(values, bonjour_target)
        listing_result = check_authenticated_smb_listing(
            values["TC_SAMBA_USER"],
            smb_password,
            smb_servers,
            expected_share_name=values["TC_SHARE_NAME"],
        )
        add_result(listing_result)
        if listing_result.status == "PASS":
            smb_server = listing_result.message.removeprefix(
                f"authenticated SMB listing works for {values['TC_SAMBA_USER']}@"
            )
            for result in check_authenticated_smb_file_ops_detailed(
                values["TC_SAMBA_USER"],
                smb_password,
                smb_server,
                values["TC_SHARE_NAME"],
            ):
                add_result(result)

    fatal = any(is_fatal(result) for result in results)
    return results, fatal
