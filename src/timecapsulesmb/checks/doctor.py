from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from timecapsulesmb.checks.bonjour import (
    BonjourServiceTarget,
    build_bonjour_expected_identity,
    check_bonjour_host_ip,
    check_bonjour_host_link_local_ips,
    check_smb_instance,
    check_smb_service_target,
    discover_smb_services_detailed,
    resolve_smb_instance,
    resolve_smb_service_target,
    select_resolved_smb_record,
    select_resolved_smb_record_by_ip,
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
from timecapsulesmb.checks.smb_targets import doctor_smb_servers
from timecapsulesmb.core.config import AppConfig, extract_host, validate_app_config
from timecapsulesmb.device.compat import is_netbsd4_payload_family, is_netbsd6_payload_family, render_compatibility_message
from timecapsulesmb.device.probe import (
    ProbedDeviceState,
    RemoteInterfaceProbeResult,
    RUNTIME_SMB_CONF,
    RuntimeNamingIdentityProbeResult,
    nbns_flash_config_enabled_conn,
    probe_connection_state,
    probe_managed_mdns_takeover_conn,
    probe_managed_smbd_conn,
    probe_remote_interface_conn,
    probe_remote_runtime_naming_identity_conn,
    read_remote_service_socket_diagnostics_conn,
    read_active_smb_conf_conn,
    read_interface_ipv4_conn,
    read_runtime_share_names_conn,
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
    service_targets: dict[str, tuple[str, ...]]
    reason: str
    debug_needed: bool
    expected_debug: dict[str, str | None] | None
    zeroconf_debug: object | None


@dataclass
class DoctorRunContext:
    config: AppConfig
    repo_root: Path
    connection: SshConnection | None
    precomputed_interface_probe: RemoteInterfaceProbeResult | None
    precomputed_probe_state: ProbedDeviceState | None
    skip_ssh: bool
    skip_bonjour: bool
    skip_smb: bool
    on_result: Callable[[CheckResult], None] | None
    debug_fields: dict[str, object] | None
    results: list[CheckResult] = field(default_factory=list)
    host: str | None = None
    smb_password: str | None = None
    proxied_ssh: bool = False
    ssh_ok: bool = False
    active_smb_conf: str | None = None
    active_smb_conf_reason: str = "SSH check not run"
    runtime_naming_identity: RuntimeNamingIdentityProbeResult | None = None
    bonjour_result: DoctorBonjourResult | None = None
    stop: bool = False

    def add_result(self, result: CheckResult) -> None:
        self.results.append(result)
        if self.on_result is not None:
            self.on_result(result)

    def fatal(self) -> bool:
        return any(is_fatal(result) for result in self.results)


@dataclass(frozen=True)
class DoctorCheck:
    id: str
    requires: tuple[str, ...]
    provides: tuple[str, ...]
    run: Callable[[DoctorRunContext], None]


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


def _add_config_validation_results(
    config: AppConfig,
    *,
    repo_root: Path,
    add_result: Callable[[CheckResult], None],
) -> bool:
    if not config.exists:
        add_result(CheckResult("FAIL", f"missing required configuration file: {config.path}"))
        return False

    add_result(CheckResult("PASS", f"configuration file exists: {config.path}"))
    validation_errors = validate_app_config(config, profile="doctor")
    if validation_errors:
        for error in validation_errors:
            add_result(CheckResult("FAIL", error.format_for_cli().replace("\n", " ")))
        return False

    add_result(CheckResult("PASS", f"{config.path} contains all required settings"))

    for result in check_required_local_tools():
        add_result(result)
    for result in check_required_artifacts(repo_root):
        add_result(result)
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


_BONJOUR_TARGET_SERVICE_ORDER = ("_airport", "_smb", "_adisk", "_device-info")


def _bonjour_service_label(service_type: str) -> str:
    normalized = service_type.strip().rstrip(".")
    for suffix in ("._tcp.local", "._udp.local", "._tcp", "._udp"):
        if normalized.endswith(suffix):
            return normalized[: -len(suffix)]
    return normalized


def _bonjour_service_targets_for_instance(records: Iterable[object], instance_name: str | None) -> dict[str, tuple[str, ...]]:
    if instance_name is None:
        return {}

    found: dict[str, set[str]] = {}
    for record in records:
        if getattr(record, "name", None) != instance_name:
            continue
        hostname = str(getattr(record, "hostname", "") or "").strip().rstrip(".")
        if not hostname:
            continue
        service_label = _bonjour_service_label(str(getattr(record, "service_type", "") or ""))
        if service_label not in _BONJOUR_TARGET_SERVICE_ORDER:
            continue
        found.setdefault(service_label, set()).add(hostname)

    return {service: tuple(sorted(found[service], key=lambda host: host.lower())) for service in _BONJOUR_TARGET_SERVICE_ORDER if service in found}


def _format_bonjour_service_targets(service_targets: dict[str, tuple[str, ...]]) -> str:
    return "; ".join(f"{service}={','.join(hosts)}" for service, hosts in service_targets.items())


def _add_bonjour_service_target_consistency_results(
    instance_name: str | None,
    service_targets: dict[str, tuple[str, ...]],
    add_result: Callable[[CheckResult], None],
) -> bool:
    if instance_name is None:
        return False
    if not service_targets:
        return False

    formatted_targets = _format_bonjour_service_targets(service_targets)
    add_result(CheckResult("INFO", f"advertised Bonjour service targets for {instance_name!r}: {formatted_targets}"))

    canonical_hosts = {
        host.strip().rstrip(".").lower()
        for hosts in service_targets.values()
        for host in hosts
        if host.strip().rstrip(".")
    }
    service_count = sum(1 for hosts in service_targets.values() if hosts)
    if len(canonical_hosts) > 1:
        add_result(CheckResult("FAIL", f"Bonjour services for {instance_name!r} advertise inconsistent host targets: {formatted_targets}"))
        return True
    elif service_count > 1:
        host = next(iter(canonical_hosts))
        add_result(CheckResult("PASS", f"Bonjour services for {instance_name!r} advertise consistent host target {host}"))
    return False


def _add_bonjour_host_ip_results(
    hostname: str,
    *,
    expected_ip: str | None,
    record_ips: list[str],
    add_result: Callable[[CheckResult], None],
) -> None:
    host_ip_result = check_bonjour_host_ip(
        hostname,
        expected_ip=expected_ip,
        record_ips=record_ips,
    )
    add_result(host_ip_result)
    if host_ip_result.status != "PASS":
        return

    link_local_result = check_bonjour_host_link_local_ips(
        hostname,
        expected_ip=expected_ip,
        record_ips=record_ips,
    )
    if link_local_result is not None:
        add_result(link_local_result)


def _add_bonjour_results(
    config: AppConfig,
    runtime_naming_identity: RuntimeNamingIdentityProbeResult | None,
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
    bonjour_service_targets: dict[str, tuple[str, ...]] = {}

    if proxied_ssh and not skip_bonjour:
        bonjour_reason = "Bonjour check skipped for SSH-proxied target"
        add_result(CheckResult("SKIP", "Bonjour check skipped for SSH-proxied target; local mDNS may find a different AirPort device"))
    elif not skip_bonjour:
        try:
            bonjour_expected = build_bonjour_expected_identity(config, runtime_naming_identity)
            bonjour_expected_debug = {
                "instance_name": bonjour_expected.instance_name,
                "host_label": bonjour_expected.host_label,
                "target_ip": bonjour_expected.target_ip,
            }
            if bonjour_expected.instance_name is None and bonjour_expected.target_ip is None:
                bonjour_reason = "Bonjour identity check skipped; device naming probe unavailable and TC_HOST is not a literal IP"
                add_result(CheckResult("SKIP", bonjour_reason))
                return DoctorBonjourResult(
                    instance=None,
                    target=None,
                    service_targets={},
                    reason=bonjour_reason,
                    debug_needed=False,
                    expected_debug=bonjour_expected_debug,
                    zeroconf_debug=None,
                )
            smb_snapshot, discovery_error, bonjour_zeroconf_debug = discover_smb_services_detailed(include_related=True)
            bonjour_reason = ""
            if discovery_error is not None:
                bonjour_reason = discovery_error.message
                bonjour_debug_needed = True
                add_result(discovery_error)
            else:
                assert smb_snapshot is not None
                smb_instances = [instance for instance in smb_snapshot.instances if _bonjour_service_label(instance.service_type) == "_smb"]
                smb_records = [record for record in smb_snapshot.resolved if _bonjour_service_label(record.service_type) == "_smb"]
                if bonjour_expected.instance_name is not None:
                    selection = select_smb_instance(
                        smb_instances,
                        expected_instance_name=bonjour_expected.instance_name,
                    )
                    for result in check_smb_instance(selection):
                        add_result(result)
                    if selection.instance is not None:
                        bonjour_instance = selection.instance.name
                        bonjour_service_targets = _bonjour_service_targets_for_instance(smb_snapshot.resolved, selection.instance.name)
                        if _add_bonjour_service_target_consistency_results(selection.instance.name, bonjour_service_targets, add_result):
                            bonjour_debug_needed = True
                        resolved_record = select_resolved_smb_record(smb_records, selection.instance)
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
                                _add_bonjour_host_ip_results(
                                    target.hostname,
                                    expected_ip=bonjour_expected.target_ip,
                                    record_ips=record_ips,
                                    add_result=add_result,
                                )
                    else:
                        bonjour_debug_needed = True
                elif bonjour_expected.target_ip is not None:
                    resolved_record = select_resolved_smb_record_by_ip(
                        smb_records,
                        bonjour_expected.target_ip,
                    )
                    if resolved_record is None:
                        bonjour_debug_needed = True
                        bonjour_reason = f"no resolved _smb._tcp service matched target IP {bonjour_expected.target_ip}"
                        add_result(CheckResult("FAIL", bonjour_reason))
                    else:
                        bonjour_instance = resolved_record.name
                        bonjour_service_targets = _bonjour_service_targets_for_instance(smb_snapshot.resolved, resolved_record.name)
                        if _add_bonjour_service_target_consistency_results(resolved_record.name, bonjour_service_targets, add_result):
                            bonjour_debug_needed = True
                        add_result(CheckResult("PASS", f"discovered _smb._tcp service matching target IP {bonjour_expected.target_ip}"))
                        target = resolve_smb_service_target(
                            resolved_record,
                            expected_instance_name=None,
                        )
                        target_result = check_smb_service_target(target)
                        if target_result.status == "FAIL":
                            bonjour_debug_needed = True
                        add_result(target_result)
                        if target.hostname:
                            bonjour_target = target
                            record_ips = list(getattr(resolved_record, "ipv4", []) or [])
                            _add_bonjour_host_ip_results(
                                target.hostname,
                                expected_ip=bonjour_expected.target_ip,
                                record_ips=record_ips,
                                add_result=add_result,
                            )
        except Exception as e:
            bonjour_reason = str(e)
            bonjour_debug_needed = True
            add_result(CheckResult("FAIL", f"Bonjour check failed: {e}"))
    else:
        bonjour_reason = "Bonjour check skipped"

    return DoctorBonjourResult(
        instance=bonjour_instance,
        target=bonjour_target,
        service_targets=bonjour_service_targets,
        reason=bonjour_reason,
        debug_needed=bonjour_debug_needed,
        expected_debug=bonjour_expected_debug,
        zeroconf_debug=bonjour_zeroconf_debug,
    )


def _doctor_share_name(connection: SshConnection, active_smb_conf: str | None) -> str:
    try:
        runtime_share_names = read_runtime_share_names_conn(connection)
    except Exception:
        runtime_share_names = []
    if runtime_share_names:
        return runtime_share_names[0]
    active_share_names = parse_active_share_names(active_smb_conf or "")
    if active_share_names:
        return active_share_names[0]
    raise RuntimeError("could not determine active Samba share name")


def _add_nbns_results(
    connection: SshConnection,
    config: AppConfig,
    *,
    host: str,
    proxied_ssh: bool,
    active_smb_conf: str | None,
    runtime_naming_identity: RuntimeNamingIdentityProbeResult | None,
    add_result: Callable[[CheckResult], None],
) -> None:
    try:
        if nbns_flash_config_enabled_conn(connection):
            if proxied_ssh:
                add_result(CheckResult("SKIP", "NBNS check skipped for SSH-proxied target; UDP/137 is not reachable through the SSH jump host"))
            else:
                expected_name = parse_active_netbios_name(active_smb_conf or "")
                if expected_name is None and runtime_naming_identity is not None:
                    expected_name = runtime_naming_identity.netbios_name
                if expected_name is None:
                    add_result(CheckResult("SKIP", "NBNS check skipped; active/probed NetBIOS name unavailable"))
                    return
                expected_ip = read_interface_ipv4_conn(connection, config.require("TC_NET_IFACE"))
                nbns_result = check_nbns_name_resolution(expected_name, host, expected_ip)
                if nbns_result.status == "FAIL":
                    nbns_result = CheckResult(
                        "INFO",
                        f"optional NBNS check failed: {nbns_result.message}",
                        nbns_result.details,
                    )
                add_result(nbns_result)
        else:
            add_result(CheckResult("SKIP", "NBNS responder not enabled"))
    except Exception as e:
        add_result(CheckResult("WARN", f"NBNS check skipped: {e}"))


def _add_authenticated_smb_results(
    connection: SshConnection,
    config: AppConfig,
    bonjour_target: BonjourServiceTarget | None,
    runtime_naming_identity: RuntimeNamingIdentityProbeResult | None,
    *,
    host: str,
    smb_password: str,
    proxied_ssh: bool,
    active_smb_conf: str | None,
    debug_fields: dict[str, object] | None,
    add_result: Callable[[CheckResult], None],
) -> None:
    try:
        share_name = _doctor_share_name(connection, active_smb_conf)
    except RuntimeError as exc:
        add_result(CheckResult("FAIL", str(exc)))
        return
    if proxied_ssh:
        local_port = find_free_local_port()
        if debug_fields is not None:
            debug_fields["authenticated_smb_listing_servers"] = ["127.0.0.1"]
            debug_fields["authenticated_smb_listing_expected_share"] = share_name
        try:
            with ssh_local_forward(
                connection,
                local_port=local_port,
                remote_host=host,
                remote_port=445,
            ):
                listing_result = check_authenticated_smb_listing(
                    config.require("TC_SAMBA_USER"),
                    smb_password,
                    "127.0.0.1",
                    expected_share_name=share_name,
                    port=local_port,
                )
                if debug_fields is not None and listing_result.details.get("attempts"):
                    debug_fields["authenticated_smb_listing_attempts"] = listing_result.details["attempts"]
                add_result(listing_result)
                for result in check_authenticated_smb_file_ops_detailed(
                    config.require("TC_SAMBA_USER"),
                    smb_password,
                    "127.0.0.1",
                    share_name,
                    port=local_port,
                ):
                    add_result(result)
        except Exception as e:
            add_result(CheckResult("FAIL", f"authenticated SMB checks failed through SSH tunnel: {e}"))
        return

    smb_servers = doctor_smb_servers(config, bonjour_target, runtime_naming_identity)
    if debug_fields is not None:
        debug_fields["authenticated_smb_listing_servers"] = smb_servers
        debug_fields["authenticated_smb_listing_expected_share"] = share_name
    listing_result = check_authenticated_smb_listing(
        config.require("TC_SAMBA_USER"),
        smb_password,
        smb_servers,
        expected_share_name=share_name,
    )
    if debug_fields is not None and listing_result.details.get("attempts"):
        debug_fields["authenticated_smb_listing_attempts"] = listing_result.details["attempts"]
    add_result(listing_result)
    if listing_result.status != "PASS":
        return

    smb_server = listing_result.details.get("server")
    if not isinstance(smb_server, str) or not smb_server:
        add_result(CheckResult("FAIL", "authenticated SMB listing did not report the server used for file-ops checks"))
        return
    for result in check_authenticated_smb_file_ops_detailed(
        config.require("TC_SAMBA_USER"),
        smb_password,
        smb_server,
        share_name,
    ):
        add_result(result)


def _doctor_check_config_validation(context: DoctorRunContext) -> None:
    config_valid = _add_config_validation_results(
        context.config,
        repo_root=context.repo_root,
        add_result=context.add_result,
    )
    if not config_valid:
        context.stop = True


def _doctor_check_connection_context(context: DoctorRunContext) -> None:
    if context.connection is None:
        context.connection = SshConnection(
            host=context.config.require("TC_HOST"),
            password=context.config.get("TC_PASSWORD"),
            ssh_opts=context.config.get("TC_SSH_OPTS"),
        )
    context.host = extract_host(context.connection.host)
    context.smb_password = context.config.require("TC_PASSWORD")
    context.proxied_ssh = ssh_opts_use_proxy(context.connection.ssh_opts)


def _doctor_check_ssh_login(context: DoctorRunContext) -> None:
    assert context.connection is not None
    if context.skip_ssh:
        context.ssh_ok = True
        context.active_smb_conf_reason = "SSH check skipped"
        return

    ssh_result = check_ssh_login(context.connection)
    context.add_result(ssh_result)
    context.ssh_ok = ssh_result.status == "PASS"
    if not context.ssh_ok:
        context.active_smb_conf_reason = "SSH login failed"


def _doctor_check_runtime_naming_identity(context: DoctorRunContext) -> None:
    if context.skip_ssh or not context.ssh_ok:
        return

    assert context.connection is not None
    try:
        context.runtime_naming_identity = probe_remote_runtime_naming_identity_conn(context.connection)
        if context.debug_fields is not None:
            context.debug_fields["runtime_naming_identity"] = {
                "system_name": context.runtime_naming_identity.system_name,
                "hostname": context.runtime_naming_identity.hostname,
                "mdns_instance_name": context.runtime_naming_identity.mdns_instance_name,
                "mdns_host_label": context.runtime_naming_identity.mdns_host_label,
                "netbios_name": context.runtime_naming_identity.netbios_name,
            }
    except Exception as e:
        context.add_result(CheckResult("WARN", f"runtime naming identity probe skipped: {e}"))


def _doctor_check_remote_interface(context: DoctorRunContext) -> None:
    if context.skip_ssh or not context.ssh_ok:
        return

    assert context.connection is not None
    if (
        context.precomputed_interface_probe is not None
        and context.precomputed_interface_probe.iface == context.config.require("TC_NET_IFACE")
    ):
        interface_probe = context.precomputed_interface_probe
    else:
        interface_probe = probe_remote_interface_conn(context.connection, context.config.require("TC_NET_IFACE"))
    if not interface_probe.exists:
        context.add_result(
            CheckResult(
                "FAIL",
                f"TC_NET_IFACE is invalid. Run the `configure` command again. {interface_probe.detail}.",
            )
        )


def _doctor_check_device_compatibility(context: DoctorRunContext) -> None:
    if context.skip_ssh or not context.ssh_ok:
        return

    assert context.connection is not None
    try:
        probed_state = context.precomputed_probe_state or probe_connection_state(context.connection)
        probe_result = probed_state.probe_result
        compatibility = probed_state.compatibility
        if compatibility is None:
            context.add_result(CheckResult("FAIL", probe_result.error or "could not determine device compatibility"))
        elif compatibility.supported:
            context.add_result(CheckResult("PASS", render_compatibility_message(compatibility)))
            _add_sshpass_result_for_payload(context.add_result, compatibility.payload_family)
        else:
            context.add_result(CheckResult("FAIL", render_compatibility_message(compatibility)))
    except Exception as e:
        context.add_result(CheckResult("FAIL", f"device compatibility check failed: {e}"))


def _doctor_check_managed_smbd(context: DoctorRunContext) -> None:
    if context.skip_ssh or not context.ssh_ok:
        return

    assert context.connection is not None
    smbd_probe = probe_managed_smbd_conn(context.connection)
    _add_probe_line_results(
        context.add_result,
        getattr(smbd_probe, "lines", ()),
        fallback_ready=smbd_probe.ready,
        fallback_pass_message="managed smbd is ready",
        fallback_fail_message=f"managed smbd is not ready ({smbd_probe.detail})",
    )


def _doctor_check_managed_mdns(context: DoctorRunContext) -> None:
    if context.skip_ssh or not context.ssh_ok:
        return

    assert context.connection is not None
    mdns_probe = probe_managed_mdns_takeover_conn(context.connection)
    if mdns_probe.ready:
        context.add_result(CheckResult("PASS", "managed mDNS takeover is active"))
    else:
        context.add_result(CheckResult("FAIL", f"managed mDNS takeover is not active ({mdns_probe.detail})"))


def _add_remote_service_socket_debug(context: DoctorRunContext) -> None:
    if context.debug_fields is None or context.skip_ssh or not context.ssh_ok:
        return
    if context.connection is None or "remote_service_sockets" in context.debug_fields:
        return
    try:
        context.debug_fields["remote_service_sockets"] = read_remote_service_socket_diagnostics_conn(context.connection)
    except Exception as e:
        context.debug_fields["remote_service_sockets_error"] = f"{type(e).__name__}: {e}"


def _doctor_check_active_smb_conf(context: DoctorRunContext) -> None:
    if context.skip_ssh or not context.ssh_ok:
        return

    assert context.connection is not None
    try:
        context.active_smb_conf = read_active_smb_conf_conn(context.connection)
        if not context.active_smb_conf.strip():
            context.active_smb_conf_reason = "active smb.conf unavailable"
        else:
            context.active_smb_conf_reason = ""
        context.add_result(check_xattr_tdb_persistence(context.connection))
    except Exception as e:
        context.active_smb_conf_reason = str(e)
        context.add_result(CheckResult("WARN", f"xattr_tdb:file check skipped: {e}"))


def _doctor_check_direct_smb_port(context: DoctorRunContext) -> None:
    assert context.host is not None
    if context.proxied_ssh:
        context.add_result(CheckResult("SKIP", f"direct SMB port check skipped for SSH-proxied target {context.host}"))
    else:
        result = check_smb_port(context.host)
        context.add_result(result)
        if result.status != "PASS":
            _add_remote_service_socket_debug(context)


def _doctor_check_bonjour(context: DoctorRunContext) -> None:
    context.bonjour_result = _add_bonjour_results(
        context.config,
        context.runtime_naming_identity,
        proxied_ssh=context.proxied_ssh,
        skip_bonjour=context.skip_bonjour,
        add_result=context.add_result,
    )


def _doctor_check_bonjour_debug_fields(context: DoctorRunContext) -> None:
    assert context.bonjour_result is not None
    _add_bonjour_debug_fields(
        context.debug_fields,
        bonjour_debug_needed=context.bonjour_result.debug_needed,
        bonjour_expected_debug=context.bonjour_result.expected_debug,
        bonjour_zeroconf_debug=context.bonjour_result.zeroconf_debug,
    )


def _doctor_check_bonjour_naming_info(context: DoctorRunContext) -> None:
    assert context.bonjour_result is not None
    if context.bonjour_result.instance is not None:
        context.add_result(CheckResult("INFO", f"advertised Bonjour instance: {context.bonjour_result.instance}"))
    else:
        context.add_result(CheckResult("INFO", f"advertised Bonjour instance: unavailable ({context.bonjour_result.reason})"))

    bonjour_host_label = context.bonjour_result.target.host_label() if context.bonjour_result.target is not None else None
    if bonjour_host_label is not None:
        context.add_result(CheckResult("INFO", f"advertised Bonjour host label: {bonjour_host_label}"))
    else:
        context.add_result(CheckResult("INFO", f"advertised Bonjour host label: unavailable ({context.bonjour_result.reason})"))


def _doctor_check_active_smb_conf_info(context: DoctorRunContext) -> None:
    _add_active_smb_conf_results(context.active_smb_conf, context.active_smb_conf_reason, context.add_result)


def _doctor_check_nbns(context: DoctorRunContext) -> None:
    if context.skip_ssh or not context.ssh_ok:
        return

    assert context.connection is not None
    assert context.host is not None
    result_start = len(context.results)
    _add_nbns_results(
        context.connection,
        context.config,
        host=context.host,
        proxied_ssh=context.proxied_ssh,
        active_smb_conf=context.active_smb_conf,
        runtime_naming_identity=context.runtime_naming_identity,
        add_result=context.add_result,
    )
    if any(result.status == "FAIL" for result in context.results[result_start:]):
        _add_remote_service_socket_debug(context)


def _doctor_check_authenticated_smb(context: DoctorRunContext) -> None:
    if context.skip_smb:
        return

    assert context.connection is not None
    assert context.host is not None
    assert context.smb_password is not None
    assert context.bonjour_result is not None
    _add_authenticated_smb_results(
        context.connection,
        context.config,
        context.bonjour_result.target,
        context.runtime_naming_identity,
        host=context.host,
        smb_password=context.smb_password,
        proxied_ssh=context.proxied_ssh,
        active_smb_conf=context.active_smb_conf,
        debug_fields=context.debug_fields,
        add_result=context.add_result,
    )


def _doctor_check_fatal_runtime_log_tails(context: DoctorRunContext) -> None:
    if context.fatal() and context.debug_fields is not None and not context.skip_ssh and context.ssh_ok:
        assert context.connection is not None
        context.debug_fields.update(read_runtime_log_tails_conn(context.connection))


DOCTOR_CHECKS: tuple[DoctorCheck, ...] = (
    DoctorCheck(
        id="config_validation",
        requires=("config", "repo_root"),
        provides=("validated_config",),
        run=_doctor_check_config_validation,
    ),
    DoctorCheck(
        id="connection_context",
        requires=("validated_config",),
        provides=("connection", "host", "smb_password", "proxied_ssh"),
        run=_doctor_check_connection_context,
    ),
    DoctorCheck(
        id="ssh_login",
        requires=("connection",),
        provides=("ssh_status",),
        run=_doctor_check_ssh_login,
    ),
    DoctorCheck(
        id="runtime_naming_identity",
        requires=("connection", "ssh_status"),
        provides=("runtime_naming_identity",),
        run=_doctor_check_runtime_naming_identity,
    ),
    DoctorCheck(
        id="remote_interface",
        requires=("connection", "ssh_status"),
        provides=("remote_interface",),
        run=_doctor_check_remote_interface,
    ),
    DoctorCheck(
        id="device_compatibility",
        requires=("connection", "ssh_status"),
        provides=("device_compatibility",),
        run=_doctor_check_device_compatibility,
    ),
    DoctorCheck(
        id="managed_smbd",
        requires=("connection", "ssh_status"),
        provides=("managed_smbd",),
        run=_doctor_check_managed_smbd,
    ),
    DoctorCheck(
        id="managed_mdns",
        requires=("connection", "ssh_status"),
        provides=("managed_mdns",),
        run=_doctor_check_managed_mdns,
    ),
    DoctorCheck(
        id="active_smb_conf",
        requires=("connection", "ssh_status"),
        provides=("active_smb_conf_state",),
        run=_doctor_check_active_smb_conf,
    ),
    DoctorCheck(
        id="direct_smb_port",
        requires=("host", "proxied_ssh"),
        provides=("direct_smb_port",),
        run=_doctor_check_direct_smb_port,
    ),
    DoctorCheck(
        id="bonjour",
        requires=("config", "proxied_ssh", "runtime_naming_identity"),
        provides=("bonjour_result",),
        run=_doctor_check_bonjour,
    ),
    DoctorCheck(
        id="bonjour_debug_fields",
        requires=("bonjour_result",),
        provides=("bonjour_debug_fields",),
        run=_doctor_check_bonjour_debug_fields,
    ),
    DoctorCheck(
        id="bonjour_naming_info",
        requires=("bonjour_result",),
        provides=("bonjour_naming_info",),
        run=_doctor_check_bonjour_naming_info,
    ),
    DoctorCheck(
        id="active_smb_conf_info",
        requires=("active_smb_conf_state",),
        provides=("active_smb_conf_info",),
        run=_doctor_check_active_smb_conf_info,
    ),
    DoctorCheck(
        id="nbns",
        requires=("config", "connection", "host", "proxied_ssh", "ssh_status", "active_smb_conf_state", "runtime_naming_identity"),
        provides=("nbns",),
        run=_doctor_check_nbns,
    ),
    DoctorCheck(
        id="authenticated_smb",
        requires=("config", "connection", "host", "smb_password", "proxied_ssh", "bonjour_result", "runtime_naming_identity"),
        provides=("authenticated_smb",),
        run=_doctor_check_authenticated_smb,
    ),
    DoctorCheck(
        id="fatal_runtime_log_tails",
        requires=("connection", "ssh_status"),
        provides=("fatal_runtime_log_tails",),
        run=_doctor_check_fatal_runtime_log_tails,
    ),
)


def _run_doctor_registry(context: DoctorRunContext, checks: Iterable[DoctorCheck]) -> None:
    provided = {"config", "repo_root"}
    for check in checks:
        missing = [dependency for dependency in check.requires if dependency not in provided]
        if missing:
            missing_text = ", ".join(missing)
            raise AssertionError(f"doctor check {check.id!r} missing dependencies: {missing_text}")
        check.run(context)
        provided.update(check.provides)
        if context.stop:
            return


def run_doctor_checks(
    config: AppConfig,
    *,
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
    context = DoctorRunContext(
        config=config,
        repo_root=repo_root,
        connection=connection,
        precomputed_interface_probe=precomputed_interface_probe,
        precomputed_probe_state=precomputed_probe_state,
        skip_ssh=skip_ssh,
        skip_bonjour=skip_bonjour,
        skip_smb=skip_smb,
        on_result=on_result,
        debug_fields=debug_fields,
    )
    _run_doctor_registry(context, DOCTOR_CHECKS)
    return context.results, context.fatal()
