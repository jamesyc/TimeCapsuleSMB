from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from timecapsulesmb.checks.bonjour import (
    BonjourServiceTarget,
    build_bonjour_expected_identity,
    check_bonjour_host_ip,
    check_smb_instance,
    check_smb_service_target,
    discover_smb_services_detailed,
    resolve_smb_instance,
    resolve_smb_service_target,
    select_resolved_smb_record,
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
from timecapsulesmb.checks.smb_config import (
    parse_active_netbios_name,
    parse_active_share_names,
    parse_xattr_tdb_paths,
)
from timecapsulesmb.checks.smb_targets import (
    configured_smb_server as _configured_smb_server,
    doctor_smb_servers as _doctor_smb_servers,
)
from timecapsulesmb.core.config import extract_host, missing_required_keys, validate_config_values
from timecapsulesmb.device.compat import is_netbsd4_payload_family, is_netbsd6_payload_family, render_compatibility_message
from timecapsulesmb.device.probe import (
    ProbedDeviceState,
    RemoteInterfaceProbeResult,
    RUNTIME_SMB_CONF,
    build_device_paths,
    discover_volume_root_conn,
    probe_connection_state,
    probe_managed_mdns_takeover_conn,
    probe_managed_smbd_conn,
    nbns_marker_enabled_conn,
    probe_remote_interface_conn,
    read_active_smb_conf_conn,
    read_interface_ipv4_conn,
    read_runtime_log_tails_conn,
)
from timecapsulesmb.discovery.native_dns_sd import browse_native_dns_sd
from timecapsulesmb.transport.local import find_free_local_port
from timecapsulesmb.transport.local import command_exists
from timecapsulesmb.transport.ssh import SshConnection, ssh_local_forward


@dataclass(frozen=True)
class DoctorBonjourResult:
    instance: str | None
    target: BonjourServiceTarget | None
    reason: str
    debug_needed: bool
    expected_debug: dict[str, str | None] | None
    zeroconf_debug: object | None


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


def _add_sshpass_result_for_payload(add_result: Callable[[CheckResult], None], payload_family: str | None) -> None:
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


def _add_env_validation_results(
    values: dict[str, str],
    *,
    env_exists: bool,
    repo_root: Path,
    add_result: Callable[[CheckResult], None],
) -> bool:
    for result in check_required_local_tools():
        add_result(result)
    for result in check_required_artifacts(repo_root):
        add_result(result)

    if not env_exists:
        add_result(CheckResult("FAIL", f"missing {repo_root / '.env'}"))
        return False

    missing = missing_required_keys(values)
    if missing:
        add_result(CheckResult("FAIL", f".env is missing required keys: {', '.join(missing)}"))
        return False

    validation_errors = validate_config_values(values, profile="doctor")
    if validation_errors:
        for error in validation_errors:
            add_result(CheckResult("FAIL", error.format_for_cli().replace("\n", " ")))
        return False

    add_result(CheckResult("PASS", ".env contains all required keys"))
    return True


def check_xattr_tdb_persistence(connection: SshConnection) -> CheckResult:
    proc_stdout = read_active_smb_conf_conn(connection)
    if not proc_stdout.strip():
        return CheckResult("WARN", f"could not inspect active smb.conf at {RUNTIME_SMB_CONF}")

    paths = parse_xattr_tdb_paths(proc_stdout)
    if not paths:
        return CheckResult("WARN", "active smb.conf does not contain xattr_tdb:file")

    memory_paths = [path for path in paths if path == "/mnt/Memory" or path.startswith("/mnt/Memory/")]
    if memory_paths:
        return CheckResult("FAIL", f"xattr_tdb:file points at non-persistent ramdisk: {', '.join(memory_paths)}")

    return CheckResult("PASS", f"xattr_tdb:file is persistent: {', '.join(paths)}")


def _add_active_smb_conf_results(
    active_smb_conf: str | None,
    active_smb_conf_reason: str,
    add_result: Callable[[CheckResult], None],
) -> None:
    if active_smb_conf and active_smb_conf.strip():
        active_netbios = parse_active_netbios_name(active_smb_conf)
        share_names = parse_active_share_names(active_smb_conf)
        if active_netbios is not None:
            add_result(CheckResult("INFO", f"active Samba NetBIOS name: {active_netbios}"))
        else:
            add_result(CheckResult("INFO", "active Samba NetBIOS name: unavailable (netbios name not found in active smb.conf)"))
        if share_names:
            add_result(CheckResult("INFO", f"active Samba share names: {', '.join(share_names)}"))
        else:
            add_result(CheckResult("INFO", "active Samba share names: unavailable (no share sections found in active smb.conf)"))
        return

    add_result(CheckResult("INFO", f"active Samba NetBIOS name: unavailable ({active_smb_conf_reason})"))
    add_result(CheckResult("INFO", f"active Samba share names: unavailable ({active_smb_conf_reason})"))


def _add_bonjour_debug_fields(
    debug_fields: dict[str, object] | None,
    *,
    bonjour_debug_needed: bool,
    bonjour_expected_debug: dict[str, str | None] | None,
    bonjour_zeroconf_debug: object | None,
) -> None:
    if not bonjour_debug_needed or debug_fields is None:
        return
    if bonjour_expected_debug is not None:
        debug_fields["bonjour_expected"] = bonjour_expected_debug
    if bonjour_zeroconf_debug is not None:
        debug_fields["bonjour_zeroconf"] = bonjour_zeroconf_debug
    try:
        native_dns_sd = browse_native_dns_sd()
    except Exception as e:
        debug_fields["bonjour_native_dns_sd_error"] = f"{type(e).__name__}: {e}"
    else:
        if native_dns_sd is not None:
            debug_fields["bonjour_native_dns_sd"] = native_dns_sd


def _add_bonjour_results(
    values: dict[str, str],
    *,
    proxied_ssh: bool,
    skip_bonjour: bool,
    add_result: Callable[[CheckResult], None],
) -> DoctorBonjourResult:
    bonjour_instance: str | None = None
    bonjour_target: BonjourServiceTarget | None = None
    bonjour_reason = "Bonjour check not run"
    bonjour_debug_needed = False
    bonjour_expected_debug: dict[str, str | None] | None = None
    bonjour_zeroconf_debug: object | None = None

    if proxied_ssh and not skip_bonjour:
        bonjour_reason = "Bonjour check skipped for SSH-proxied target"
        add_result(CheckResult("SKIP", "Bonjour check skipped for SSH-proxied target; local mDNS may find a different AirPort device"))
    elif not skip_bonjour:
        try:
            bonjour_expected = build_bonjour_expected_identity(values)
            bonjour_expected_debug = {
                "instance_name": bonjour_expected.instance_name,
                "host_label": bonjour_expected.host_label,
                "target_ip": bonjour_expected.target_ip,
            }
            smb_snapshot, discovery_error, bonjour_zeroconf_debug = discover_smb_services_detailed()
            bonjour_reason = ""
            if discovery_error is not None:
                bonjour_reason = discovery_error.message
                bonjour_debug_needed = True
                add_result(discovery_error)
            else:
                assert smb_snapshot is not None
                selection = select_smb_instance(
                    smb_snapshot.instances,
                    expected_instance_name=bonjour_expected.instance_name,
                )
                for result in check_smb_instance(selection):
                    add_result(result)
                if selection.instance is not None:
                    bonjour_instance = selection.instance.name
                    resolved_record = select_resolved_smb_record(smb_snapshot.resolved, selection.instance)
                    resolve_error = None
                    if resolved_record is None:
                        resolved_record, resolve_error = resolve_smb_instance(selection.instance)
                    if resolve_error is not None:
                        bonjour_reason = resolve_error.message
                        bonjour_debug_needed = True
                        add_result(resolve_error)
                    elif resolved_record is not None:
                        target = resolve_smb_service_target(
                            resolved_record,
                            expected_instance_name=bonjour_expected.instance_name,
                        )
                        target_result = check_smb_service_target(target)
                        if target_result.status == "FAIL":
                            bonjour_debug_needed = True
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
                else:
                    bonjour_debug_needed = True
        except Exception as e:
            bonjour_reason = str(e)
            bonjour_debug_needed = True
            add_result(CheckResult("FAIL", f"Bonjour check failed: {e}"))
    else:
        bonjour_reason = "Bonjour check skipped"

    return DoctorBonjourResult(
        instance=bonjour_instance,
        target=bonjour_target,
        reason=bonjour_reason,
        debug_needed=bonjour_debug_needed,
        expected_debug=bonjour_expected_debug,
        zeroconf_debug=bonjour_zeroconf_debug,
    )


def _add_nbns_results(
    connection: SshConnection,
    values: dict[str, str],
    *,
    host: str,
    proxied_ssh: bool,
    add_result: Callable[[CheckResult], None],
) -> None:
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


def _add_authenticated_smb_results(
    connection: SshConnection,
    values: dict[str, str],
    bonjour_target: BonjourServiceTarget | None,
    *,
    host: str,
    smb_password: str,
    proxied_ssh: bool,
    add_result: Callable[[CheckResult], None],
) -> None:
    if proxied_ssh:
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
        return

    smb_servers = _doctor_smb_servers(values, bonjour_target)
    listing_result = check_authenticated_smb_listing(
        values["TC_SAMBA_USER"],
        smb_password,
        smb_servers,
        expected_share_name=values["TC_SHARE_NAME"],
    )
    add_result(listing_result)
    if listing_result.status != "PASS":
        return

    smb_server = listing_result.details.get("server")
    if not isinstance(smb_server, str) or not smb_server:
        add_result(CheckResult("FAIL", "authenticated SMB listing did not report the server used for file-ops checks"))
        return
    for result in check_authenticated_smb_file_ops_detailed(
        values["TC_SAMBA_USER"],
        smb_password,
        smb_server,
        values["TC_SHARE_NAME"],
    ):
        add_result(result)


def run_doctor_checks(
    values: dict[str, str],
    *,
    env_exists: bool,
    repo_root: Path,
    connection: SshConnection | None = None,
    precomputed_interface_probe: RemoteInterfaceProbeResult | None = None,
    precomputed_probe_state: ProbedDeviceState | None = None,
    skip_ssh: bool = False,
    skip_bonjour: bool = False,
    skip_smb: bool = False,
    on_result: Optional[Callable[[CheckResult], None]] = None,
    debug_fields: dict[str, object] | None = None,
) -> tuple[list[CheckResult], bool]:
    results: list[CheckResult] = []

    def add_result(result: CheckResult) -> None:
        results.append(result)
        if on_result is not None:
            on_result(result)

    env_valid = _add_env_validation_results(
        values,
        env_exists=env_exists,
        repo_root=repo_root,
        add_result=add_result,
    )

    if not env_valid:
        return results, any(is_fatal(result) for result in results)

    if connection is None:
        connection = SshConnection(
            host=values["TC_HOST"],
            password=values.get("TC_PASSWORD", ""),
            ssh_opts=values.get("TC_SSH_OPTS", ""),
        )
    host = extract_host(connection.host)
    smb_password = values["TC_PASSWORD"]
    proxied_ssh = ssh_opts_use_proxy(connection.ssh_opts)
    ssh_ok = False
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
        if (
            precomputed_interface_probe is not None
            and precomputed_interface_probe.iface == values["TC_NET_IFACE"]
        ):
            interface_probe = precomputed_interface_probe
        else:
            interface_probe = probe_remote_interface_conn(connection, values["TC_NET_IFACE"])
        if not interface_probe.exists:
            add_result(
                CheckResult(
                    "FAIL",
                    f"TC_NET_IFACE is invalid. Run the `configure` command again. {interface_probe.detail}.",
                )
            )
        try:
            probed_state = precomputed_probe_state or probe_connection_state(connection)
            probe_result = probed_state.probe_result
            compatibility = probed_state.compatibility
            if compatibility is None:
                add_result(CheckResult("FAIL", probe_result.error or "could not determine device compatibility"))
            elif compatibility.supported:
                add_result(CheckResult("PASS", render_compatibility_message(compatibility)))
                _add_sshpass_result_for_payload(add_result, compatibility.payload_family)
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

    bonjour_result = _add_bonjour_results(
        values,
        proxied_ssh=proxied_ssh,
        skip_bonjour=skip_bonjour,
        add_result=add_result,
    )

    _add_bonjour_debug_fields(
        debug_fields,
        bonjour_debug_needed=bonjour_result.debug_needed,
        bonjour_expected_debug=bonjour_result.expected_debug,
        bonjour_zeroconf_debug=bonjour_result.zeroconf_debug,
    )

    if bonjour_result.instance is not None:
        add_result(CheckResult("INFO", f"advertised Bonjour instance: {bonjour_result.instance}"))
    else:
        add_result(CheckResult("INFO", f"advertised Bonjour instance: unavailable ({bonjour_result.reason})"))

    bonjour_host_label = bonjour_result.target.host_label() if bonjour_result.target is not None else None
    if bonjour_host_label is not None:
        add_result(CheckResult("INFO", f"advertised Bonjour host label: {bonjour_host_label}"))
    else:
        add_result(CheckResult("INFO", f"advertised Bonjour host label: unavailable ({bonjour_result.reason})"))

    _add_active_smb_conf_results(active_smb_conf, active_smb_conf_reason, add_result)

    if not skip_ssh and ssh_ok:
        _add_nbns_results(
            connection,
            values,
            host=host,
            proxied_ssh=proxied_ssh,
            add_result=add_result,
        )

    if not skip_smb:
        _add_authenticated_smb_results(
            connection,
            values,
            bonjour_result.target,
            host=host,
            smb_password=smb_password,
            proxied_ssh=proxied_ssh,
            add_result=add_result,
        )

    fatal = any(is_fatal(result) for result in results)
    if fatal and debug_fields is not None and not skip_ssh and ssh_ok:
        debug_fields.update(read_runtime_log_tails_conn(connection))
    return results, fatal
