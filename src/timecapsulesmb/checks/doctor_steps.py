from __future__ import annotations

import ipaddress
from collections.abc import Callable, Iterable
from pathlib import Path

from timecapsulesmb.checks.bonjour import (
    BonjourServiceTarget,
    build_bonjour_expected_identity,
    check_bonjour_host_ip,
    check_smb_instance,
    check_smb_service_target,
    discover_smb_services_detailed,
    resolve_expected_smb_record,
    resolve_smb_instance,
    resolve_smb_service_target,
    select_resolved_smb_record_by_ip,
)
from timecapsulesmb.checks.doctor_debug import _add_remote_service_socket_debug
from timecapsulesmb.checks.doctor_state import (
    DoctorBonjourResult,
    DoctorInputs,
    DoctorOptions,
    DoctorSink,
    DoctorTarget,
    NetworkPlanState,
    RemoteAccess,
    RuntimeNamingState,
    SmbConfigState,
    StepDecision,
)
from timecapsulesmb.checks.local_tools import check_required_artifacts, check_required_local_tools
from timecapsulesmb.checks.models import CheckResult
from timecapsulesmb.checks.network import check_smb_port, check_ssh_login, ssh_opts_use_proxy
from timecapsulesmb.checks.network_plan import (
    NetworkCheckPlan,
    NetworkFamilyPlan,
    bind_interface_families,
    build_network_check_plan,
    local_interface_addresses,
)
from timecapsulesmb.checks.nbns import check_nbns_name_resolution
from timecapsulesmb.checks.smb import (
    SmbClientTarget,
    SmbClientTargetInput,
    check_authenticated_smb_listing,
    check_authenticated_smb_file_ops_detailed,
)
from timecapsulesmb.checks.smb_config import (
    parse_active_netbios_name,
    parse_active_share_names,
    parse_global_option,
    parse_xattr_tdb_paths,
)
from timecapsulesmb.checks.smb_targets import doctor_smb_servers
from timecapsulesmb.core.config import AppConfig, DEFAULT_SAMBA_AUTH_USER, validate_app_config
from timecapsulesmb.core.release import CLI_VERSION_CODE, RELEASE_TAG
from timecapsulesmb.core.net import extract_host
from timecapsulesmb.device.compat import is_netbsd4_payload_family, is_netbsd6_payload_family, render_compatibility_message
from timecapsulesmb.device.probe import (
    FLASH_RUNTIME_CONFIG,
    ProbedDeviceState,
    RemoteInterfaceProbeResult,
    RUNTIME_RAM_ROOT,
    RUNTIME_SMB_CONF,
    RuntimeNamingIdentityProbeResult,
    flash_runtime_config_present_conn,
    nbns_flash_config_enabled_conn,
    probe_connection_state,
    probe_managed_mdns_takeover_conn,
    probe_managed_smbd_conn,
    probe_remote_network_capabilities_conn,
    probe_remote_interface_conn,
    probe_remote_runtime_naming_identity_conn,
    read_deployed_version_conn,
    read_active_smb_conf_conn,
    runtime_ram_root_present_conn,
)
from timecapsulesmb.transport.local import find_free_local_port
from timecapsulesmb.transport.local import command_exists
from timecapsulesmb.transport.ssh import SshConnection, ssh_local_forward


AUTHENTICATED_SMB_LISTING_RETRY_DELAYS = (10, 15)


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


def check_xattr_tdb_persistence(connection: SshConnection, config_text: str | None = None) -> CheckResult:
    active_smb_conf = config_text if config_text is not None else read_active_smb_conf_conn(connection)
    if not active_smb_conf.strip():
        return CheckResult("WARN", f"could not inspect active smb.conf at {RUNTIME_SMB_CONF}")

    paths = parse_xattr_tdb_paths(active_smb_conf)
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
) -> CheckResult:
    host_ip_result = check_bonjour_host_ip(
        hostname,
        expected_ip=expected_ip,
        record_ips=record_ips,
    )
    add_result(host_ip_result)
    return host_ip_result


def _record_ips(record: object) -> list[str]:
    ips: list[str] = []
    for ip in list(getattr(record, "ipv4", []) or []) + list(getattr(record, "ipv6", []) or []):
        if ip and ip not in ips:
            ips.append(ip)
    return ips


def _network_plan_debug(plan: NetworkCheckPlan) -> dict[str, object]:
    def family_debug(family_plan: NetworkFamilyPlan) -> dict[str, object]:
        return {
            "remote_addresses": list(family_plan.remote_addresses),
            "remote_cidrs": list(family_plan.remote_cidrs),
            "local_sources": list(family_plan.local_sources),
            "mdns_expected": family_plan.mdns_expected,
            "samba_expected": family_plan.samba_expected,
            "nbns_expected": family_plan.nbns_expected,
        }

    return {
        "ipv4": family_debug(plan.ipv4),
        "ipv6": family_debug(plan.ipv6),
    }


def _family_label(family: str) -> str:
    return "IPv6" if family == "ipv6" else "IPv4"


def _prefixed_check_result(result: CheckResult, prefix: str) -> CheckResult:
    if not prefix:
        return result
    return CheckResult(result.status, f"{prefix}{result.message}", result.details)


def _bonjour_family_attempts(
    network_plan: NetworkCheckPlan | None,
    legacy_target_ip: str | None,
    add_result: Callable[[CheckResult], None],
) -> list[tuple[NetworkFamilyPlan | None, str | None, list[str] | None]]:
    if network_plan is None:
        return [(None, legacy_target_ip, None)]

    attempts: list[tuple[NetworkFamilyPlan | None, str | None, list[str] | None]] = []
    for family_plan in network_plan.families():
        if not family_plan.mdns_expected:
            continue
        family_label = _family_label(family_plan.family)
        if not family_plan.remote_addresses:
            add_result(CheckResult("SKIP", f"Bonjour {family_label} check skipped; runtime has no advertised {family_plan.family} address"))
            continue
        if not family_plan.local_sources:
            add_result(CheckResult("SKIP", f"Bonjour {family_label} check skipped; local host has no address on the remote {family_plan.family} network"))
            continue
        attempts.append((family_plan, family_plan.remote_addresses[0], list(family_plan.local_sources)))
    return attempts


def _add_bonjour_results(
    config: AppConfig,
    runtime_naming_identity: RuntimeNamingIdentityProbeResult | None,
    *,
    proxied_ssh: bool,
    skip_bonjour: bool,
    network_plan: NetworkCheckPlan | None = None,
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
            if bonjour_expected.instance_name is None and bonjour_expected.target_ip is None and network_plan is None:
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
            attempt_diagnostics: list[object] = []
            attempts = _bonjour_family_attempts(network_plan, bonjour_expected.target_ip, add_result)
            if not attempts:
                bonjour_reason = "Bonjour check skipped; no reachable local address for runtime mDNS families"
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

            bonjour_reason = ""
            for family_plan, target_ip, interfaces in attempts:
                family = family_plan.family if family_plan is not None else None
                result_prefix = f"Bonjour {_family_label(family)}: " if family_plan is not None else ""

                def add_attempt_result(result: CheckResult) -> None:
                    add_result(_prefixed_check_result(result, result_prefix))

                smb_snapshot, discovery_error, attempt_debug = discover_smb_services_detailed(
                    include_related=True,
                    target_ip=target_ip,
                    family=family,
                    interfaces=interfaces,
                )
                if attempt_debug is not None:
                    attempt_diagnostics.append(attempt_debug)
                if discovery_error is not None:
                    bonjour_reason = discovery_error.message
                    bonjour_debug_needed = True
                    add_attempt_result(discovery_error)
                    continue

                assert smb_snapshot is not None
                smb_instances = [instance for instance in smb_snapshot.instances if _bonjour_service_label(instance.service_type) == "_smb"]
                smb_records = [record for record in smb_snapshot.resolved if _bonjour_service_label(record.service_type) == "_smb"]
                if bonjour_expected.instance_name is not None:
                    resolution = resolve_expected_smb_record(
                        smb_instances,
                        smb_records,
                        expected_instance_name=bonjour_expected.instance_name,
                        target_ip=target_ip,
                        family=family,
                        interfaces=interfaces,
                        resolver=resolve_smb_instance,
                    )
                    if resolution.source == "browse":
                        for result in check_smb_instance(resolution.selection):
                            add_attempt_result(result)
                    elif resolution.record is not None:
                        add_attempt_result(CheckResult(
                            "INFO",
                            f"Python zeroconf browse did not observe expected _smb._tcp instance {bonjour_expected.instance_name!r}; targeted resolve succeeded",
                        ))
                        add_attempt_result(CheckResult(
                            "PASS",
                            f"resolved expected _smb._tcp instance {bonjour_expected.instance_name!r} by targeted query",
                        ))
                    else:
                        for result in check_smb_instance(resolution.selection):
                            add_attempt_result(result)

                    if resolution.error is not None:
                        bonjour_reason = resolution.error.message
                        bonjour_debug_needed = True
                        add_attempt_result(resolution.error)
                    elif resolution.record is not None:
                        bonjour_instance = resolution.instance.name
                        records_for_targets = list(smb_snapshot.resolved)
                        records_for_targets.append(resolution.record)
                        bonjour_service_targets = _bonjour_service_targets_for_instance(records_for_targets, resolution.instance.name)
                        if _add_bonjour_service_target_consistency_results(resolution.instance.name, bonjour_service_targets, add_attempt_result):
                            bonjour_debug_needed = True
                        target = resolve_smb_service_target(
                            resolution.record,
                            expected_instance_name=bonjour_expected.instance_name,
                        )
                        target_result = check_smb_service_target(target)
                        if target_result.status == "FAIL":
                            bonjour_debug_needed = True
                        add_attempt_result(target_result)
                        if target.hostname:
                            bonjour_target = target
                            host_ip_result = _add_bonjour_host_ip_results(
                                target.hostname,
                                expected_ip=target_ip,
                                record_ips=_record_ips(resolution.record),
                                add_result=add_attempt_result,
                            )
                            if host_ip_result.status == "FAIL":
                                bonjour_debug_needed = True
                    else:
                        bonjour_debug_needed = True
                elif target_ip is not None:
                    resolved_record = select_resolved_smb_record_by_ip(
                        smb_records,
                        target_ip,
                    )
                    if resolved_record is None:
                        bonjour_debug_needed = True
                        bonjour_reason = f"no resolved _smb._tcp service matched target IP {target_ip}"
                        add_attempt_result(CheckResult("FAIL", bonjour_reason))
                    else:
                        bonjour_instance = resolved_record.name
                        bonjour_service_targets = _bonjour_service_targets_for_instance(smb_snapshot.resolved, resolved_record.name)
                        if _add_bonjour_service_target_consistency_results(resolved_record.name, bonjour_service_targets, add_attempt_result):
                            bonjour_debug_needed = True
                        add_attempt_result(CheckResult("PASS", f"discovered _smb._tcp service matching target IP {target_ip}"))
                        target = resolve_smb_service_target(
                            resolved_record,
                            expected_instance_name=None,
                        )
                        target_result = check_smb_service_target(target)
                        if target_result.status == "FAIL":
                            bonjour_debug_needed = True
                        add_attempt_result(target_result)
                        if target.hostname:
                            bonjour_target = target
                            host_ip_result = _add_bonjour_host_ip_results(
                                target.hostname,
                                expected_ip=target_ip,
                                record_ips=_record_ips(resolved_record),
                                add_result=add_attempt_result,
                            )
                            if host_ip_result.status == "FAIL":
                                bonjour_debug_needed = True
            if len(attempt_diagnostics) == 1:
                bonjour_zeroconf_debug = attempt_diagnostics[0]
            elif attempt_diagnostics:
                bonjour_zeroconf_debug = attempt_diagnostics
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


def _doctor_share_name(active_smb_conf: str | None) -> str:
    active_share_names = parse_active_share_names(active_smb_conf or "")
    if active_share_names:
        return active_share_names[0]
    raise RuntimeError("could not determine active Samba share name")


def _nbns_family_targets(network_plan: NetworkCheckPlan | None) -> tuple[NetworkFamilyPlan, ...]:
    if network_plan is None:
        return ()
    return tuple(family_plan for family_plan in network_plan.families() if family_plan.nbns_expected)


def _add_nbns_results(
    connection: SshConnection,
    *,
    proxied_ssh: bool,
    active_smb_conf: str | None,
    runtime_naming_identity: RuntimeNamingIdentityProbeResult | None,
    network_plan: NetworkCheckPlan | None,
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
                family_targets = _nbns_family_targets(network_plan)
                if not family_targets:
                    add_result(CheckResult("SKIP", "NBNS check skipped; runtime network plan unavailable"))
                    return
                checked = False
                for family_plan in family_targets:
                    family_label = _family_label(family_plan.family)
                    if not family_plan.remote_addresses:
                        add_result(CheckResult("SKIP", f"NBNS {family_label} check skipped; runtime has no advertised {family_plan.family} address"))
                        continue
                    if not family_plan.local_sources:
                        add_result(CheckResult("SKIP", f"NBNS {family_label} check skipped; local host has no address on the remote {family_plan.family} network"))
                        continue
                    checked = True
                    expected_ip = family_plan.remote_addresses[0]
                    nbns_result = check_nbns_name_resolution(expected_name, expected_ip, expected_ip)
                    if nbns_result.status == "FAIL":
                        nbns_result = CheckResult(
                            "INFO",
                            f"optional NBNS {family_label} check failed: {nbns_result.message}",
                            nbns_result.details,
                        )
                    add_result(nbns_result)
                if not checked:
                    add_result(CheckResult("SKIP", "NBNS check skipped; no locally reachable runtime NBNS family"))
        else:
            add_result(CheckResult("SKIP", "NBNS responder not enabled"))
    except Exception as e:
        add_result(CheckResult("WARN", f"NBNS check skipped: {e}"))


def _ip_literal(value: str) -> str | None:
    text = value.strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    try:
        return str(ipaddress.ip_address(text))
    except ValueError:
        return None


def _smb_client_target_debug(target: SmbClientTargetInput) -> str:
    if isinstance(target, SmbClientTarget):
        return target.display
    return target


def _doctor_smb_client_targets(
    config: AppConfig,
    bonjour_target: BonjourServiceTarget | None,
    runtime_naming_identity: RuntimeNamingIdentityProbeResult | None,
    network_plan: NetworkCheckPlan | None,
) -> list[SmbClientTargetInput]:
    servers = doctor_smb_servers(config, bonjour_target, runtime_naming_identity)
    targets: list[SmbClientTargetInput] = []
    seen: set[tuple[str, str | None]] = set()

    def add(target: SmbClientTargetInput) -> None:
        if isinstance(target, SmbClientTarget):
            key = (target.server, target.ip_address)
        else:
            key = (target, None)
        if key not in seen:
            seen.add(key)
            targets.append(target)

    pinned_server = next((server for server in servers if _ip_literal(server) is None), None)
    if network_plan is not None:
        for family_plan in network_plan.families():
            if not family_plan.samba_expected or not family_plan.remote_addresses or not family_plan.local_sources:
                continue
            remote_address = family_plan.remote_addresses[0]
            server = pinned_server
            if server is None and _ip_literal(remote_address) is not None and ":" not in remote_address:
                server = remote_address
            if server is not None:
                add(
                    SmbClientTarget(
                        server=server,
                        ip_address=remote_address,
                    )
                )

    if targets:
        return targets

    for server in servers:
        add(server)
    return targets


def _smb_listing_looks_like_local_route_failure(result: CheckResult) -> bool:
    if "NT_STATUS_HOST_UNREACHABLE" in result.message:
        return True
    attempts = result.details.get("attempts")
    if not isinstance(attempts, list):
        return False
    for attempt in attempts:
        if not isinstance(attempt, dict):
            continue
        for key in ("failure", "stderr_tail", "stdout_tail"):
            value = attempt.get(key)
            if isinstance(value, str) and "NT_STATUS_HOST_UNREACHABLE" in value:
                return True
    return False


def _add_tunneled_authenticated_smb_results(
    connection: SshConnection,
    *,
    host: str,
    smb_password: str,
    share_name: str,
    remote_port: int,
    debug_prefix: str,
    debug_fields: dict[str, object] | None,
    add_result: Callable[[CheckResult], None],
) -> bool:
    local_port = find_free_local_port()
    if debug_fields is not None:
        debug_fields[f"{debug_prefix}_listing_servers"] = ["127.0.0.1"]
        debug_fields[f"{debug_prefix}_listing_expected_share"] = share_name
    try:
        with ssh_local_forward(
            connection,
            local_port=local_port,
            remote_host=host,
            remote_port=remote_port,
        ):
            listing_result = check_authenticated_smb_listing(
                DEFAULT_SAMBA_AUTH_USER,
                smb_password,
                "127.0.0.1",
                expected_share_name=share_name,
                port=local_port,
                retry_delays=AUTHENTICATED_SMB_LISTING_RETRY_DELAYS,
            )
            if debug_fields is not None and listing_result.details.get("attempts"):
                debug_fields[f"{debug_prefix}_listing_attempts"] = listing_result.details["attempts"]
            add_result(listing_result)
            if listing_result.status != "PASS":
                return False

            file_ops_ok = True
            for result in check_authenticated_smb_file_ops_detailed(
                DEFAULT_SAMBA_AUTH_USER,
                smb_password,
                "127.0.0.1",
                share_name,
                port=local_port,
            ):
                add_result(result)
                if result.status == "FAIL":
                    file_ops_ok = False
            return file_ops_ok
    except Exception as e:
        add_result(CheckResult("FAIL", f"authenticated SMB checks failed through SSH tunnel: {e}"))
        return False


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
    network_plan: NetworkCheckPlan | None,
    debug_fields: dict[str, object] | None,
    add_result: Callable[[CheckResult], None],
) -> None:
    try:
        share_name = _doctor_share_name(active_smb_conf)
    except RuntimeError as exc:
        add_result(CheckResult("FAIL", str(exc)))
        return
    if proxied_ssh:
        _add_tunneled_authenticated_smb_results(
            connection,
            host=host,
            smb_password=smb_password,
            share_name=share_name,
            remote_port=445,
            debug_prefix="authenticated_smb",
            debug_fields=debug_fields,
            add_result=add_result,
        )
        return

    smb_servers = _doctor_smb_client_targets(config, bonjour_target, runtime_naming_identity, network_plan)
    if debug_fields is not None:
        debug_fields["authenticated_smb_listing_servers"] = [_smb_client_target_debug(target) for target in smb_servers]
        debug_fields["authenticated_smb_listing_expected_share"] = share_name
    listing_result = check_authenticated_smb_listing(
        DEFAULT_SAMBA_AUTH_USER,
        smb_password,
        smb_servers,
        expected_share_name=share_name,
        port=445,
        retry_delays=AUTHENTICATED_SMB_LISTING_RETRY_DELAYS,
    )
    if debug_fields is not None and listing_result.details.get("attempts"):
        debug_fields["authenticated_smb_listing_attempts"] = listing_result.details["attempts"]
    if listing_result.status != "PASS":
        if _smb_listing_looks_like_local_route_failure(listing_result):
            add_result(
                CheckResult(
                    "WARN",
                    "direct local smbclient authenticated SMB check failed with NT_STATUS_HOST_UNREACHABLE, likely due to macOS permissions issue; retrying through SSH tunnel",
                    {"direct_attempts": listing_result.details.get("attempts", [])},
                )
            )
            if _add_tunneled_authenticated_smb_results(
                connection,
                host=host,
                smb_password=smb_password,
                share_name=share_name,
                remote_port=445,
                debug_prefix="authenticated_smb_tunnel",
                debug_fields=debug_fields,
                add_result=add_result,
            ):
                return
        add_result(listing_result)
        return
    add_result(listing_result)

    smb_server = listing_result.details.get("server")
    if not isinstance(smb_server, str) or not smb_server:
        add_result(CheckResult("FAIL", "authenticated SMB listing did not report the server used for file-ops checks"))
        return
    smb_ip_address = listing_result.details.get("ip_address")
    if not isinstance(smb_ip_address, str) or not smb_ip_address:
        smb_ip_address = None
    file_ops_kwargs = {}
    if smb_ip_address is not None:
        file_ops_kwargs["ip_address"] = smb_ip_address
    for result in check_authenticated_smb_file_ops_detailed(
        DEFAULT_SAMBA_AUTH_USER,
        smb_password,
        smb_server,
        share_name,
        port=445,
        **file_ops_kwargs,
    ):
        add_result(result)


def _doctor_validate_config(inputs: DoctorInputs, sink: DoctorSink) -> StepDecision:
    config_valid = _add_config_validation_results(
        inputs.config,
        repo_root=inputs.repo_root,
        add_result=sink.add,
    )
    return StepDecision(stop=not config_valid)


def _build_doctor_target(inputs: DoctorInputs) -> DoctorTarget:
    connection = inputs.connection
    if connection is None:
        connection = SshConnection(
            host=inputs.config.require("TC_HOST"),
            password=inputs.config.get("TC_PASSWORD"),
            ssh_opts=inputs.config.get("TC_SSH_OPTS"),
        )
    return DoctorTarget(
        connection=connection,
        host=extract_host(connection.host),
        smb_password=inputs.config.require("TC_PASSWORD"),
        proxied_ssh=ssh_opts_use_proxy(connection.ssh_opts),
    )


def _doctor_check_ssh_login(target: DoctorTarget, options: DoctorOptions, sink: DoctorSink) -> RemoteAccess:
    if options.skip_ssh:
        return RemoteAccess(
            ssh_checked=False,
            ssh_ok=True,
            remote_checks_enabled=False,
            active_smb_conf_reason="SSH check skipped",
        )

    ssh_result = check_ssh_login(target.connection)
    sink.add(ssh_result)
    ssh_ok = ssh_result.status == "PASS"
    return RemoteAccess(
        ssh_checked=True,
        ssh_ok=ssh_ok,
        remote_checks_enabled=ssh_ok,
        active_smb_conf_reason="SSH check not run" if ssh_ok else "SSH login failed",
    )


def _doctor_check_deployed_config(target: DoctorTarget, remote: RemoteAccess, sink: DoctorSink) -> StepDecision:
    if not remote.remote_checks_enabled:
        return StepDecision()

    try:
        config_present = flash_runtime_config_present_conn(target.connection)
    except Exception as e:
        sink.add(CheckResult("FAIL", f"deployed payload config probe failed; reboot the device and rerun doctor: {e}"))
        return StepDecision(stop=True)

    if sink.debug_fields is not None:
        sink.debug_fields["deployed_config_present"] = config_present

    if not config_present:
        sink.add(CheckResult("FAIL", "deployed payload config not found; please run deploy to install on your device"))
        return StepDecision(stop=True)

    sink.add(CheckResult("PASS", f"deployed payload config {FLASH_RUNTIME_CONFIG} exists"))
    return StepDecision()


def _doctor_check_deployed_version(target: DoctorTarget, remote: RemoteAccess, sink: DoctorSink) -> StepDecision:
    if not remote.remote_checks_enabled:
        return StepDecision()

    try:
        deployed_version = read_deployed_version_conn(target.connection)
    except Exception as e:
        sink.add(CheckResult("FAIL", f"deployed payload version probe failed; reboot the device and rerun doctor: {e}"))
        return StepDecision(stop=True)

    if sink.debug_fields is not None:
        sink.debug_fields["deployed_release_tag"] = deployed_version.release_tag
        sink.debug_fields["deployed_cli_version_code"] = deployed_version.cli_version_code

    deployed_release_tag = deployed_version.release_tag
    deployed_cli_version_code = deployed_version.cli_version_code
    if deployed_release_tag is None or deployed_cli_version_code is None:
        sink.add(
            CheckResult(
                "FAIL",
                f"deployed payload has no version metadata; current version is {RELEASE_TAG}; please run deploy to update your device",
            )
        )
        return StepDecision(stop=True)

    if deployed_cli_version_code < CLI_VERSION_CODE:
        sink.add(
            CheckResult(
                "FAIL",
                f"deployed version {deployed_release_tag} is older than current {RELEASE_TAG}; please run deploy to update your device",
            )
        )
        return StepDecision(stop=True)

    if deployed_cli_version_code > CLI_VERSION_CODE:
        sink.add(
            CheckResult(
                "FAIL",
                f"deployed version {deployed_release_tag} is newer than this doctor {RELEASE_TAG}; please update before running doctor",
            )
        )
        return StepDecision(stop=True)

    sink.add(CheckResult("PASS", f"deployed version matches current release {RELEASE_TAG}"))
    return StepDecision()


def _doctor_check_runtime_ram_root(target: DoctorTarget, remote: RemoteAccess, sink: DoctorSink) -> StepDecision:
    if not remote.remote_checks_enabled:
        return StepDecision()

    try:
        runtime_ram_root_present = runtime_ram_root_present_conn(target.connection)
    except Exception as e:
        sink.add(CheckResult("FAIL", f"managed runtime directory check failed: {e}"))
        return StepDecision(stop=True)

    if not runtime_ram_root_present:
        sink.add(
            CheckResult(
                "FAIL",
                f"managed runtime directory {RUNTIME_RAM_ROOT} is missing; run deploy or activate to start the managed runtime",
            )
        )
        return StepDecision(stop=True)

    sink.add(CheckResult("PASS", f"managed runtime directory {RUNTIME_RAM_ROOT} exists"))
    return StepDecision()


def _doctor_check_runtime_naming_identity(target: DoctorTarget, remote: RemoteAccess, sink: DoctorSink) -> RuntimeNamingState:
    if not remote.remote_checks_enabled:
        return RuntimeNamingState(identity=None)

    try:
        identity = probe_remote_runtime_naming_identity_conn(target.connection)
        if sink.debug_fields is not None:
            sink.debug_fields["runtime_naming_identity"] = {
                "system_name": identity.system_name,
                "hostname": identity.hostname,
                "mdns_instance_name": identity.mdns_instance_name,
                "mdns_host_label": identity.mdns_host_label,
                "netbios_name": identity.netbios_name,
            }
        return RuntimeNamingState(identity=identity)
    except Exception as e:
        sink.add(CheckResult("WARN", f"runtime naming identity probe skipped: {e}"))
        return RuntimeNamingState(identity=None)


def _doctor_check_device_compatibility(inputs: DoctorInputs, target: DoctorTarget, remote: RemoteAccess, sink: DoctorSink) -> None:
    if not remote.remote_checks_enabled:
        return

    try:
        probed_state = inputs.precomputed_probe_state or probe_connection_state(target.connection)
        probe_result = probed_state.probe_result
        compatibility = probed_state.compatibility
        if compatibility is None:
            sink.add(CheckResult("FAIL", probe_result.error or "could not determine device compatibility"))
        elif compatibility.supported:
            sink.add(CheckResult("PASS", render_compatibility_message(compatibility)))
            _add_sshpass_result_for_payload(sink.add, compatibility.payload_family)
        else:
            sink.add(CheckResult("FAIL", render_compatibility_message(compatibility)))
    except Exception as e:
        sink.add(CheckResult("FAIL", f"device compatibility check failed: {e}"))


def _doctor_check_managed_smbd(target: DoctorTarget, remote: RemoteAccess, sink: DoctorSink) -> None:
    if not remote.remote_checks_enabled:
        return

    smbd_probe = probe_managed_smbd_conn(target.connection)
    smbd_probe_lines = getattr(smbd_probe, "lines", ())
    if not isinstance(smbd_probe_lines, (list, tuple)):
        smbd_probe_lines = ()
    _add_probe_line_results(
        sink.add,
        smbd_probe_lines,
        fallback_ready=smbd_probe.ready,
        fallback_pass_message="managed smbd is ready",
        fallback_fail_message=f"managed smbd is not ready ({smbd_probe.detail})",
    )


def _doctor_check_managed_mdns(target: DoctorTarget, remote: RemoteAccess, sink: DoctorSink) -> None:
    if not remote.remote_checks_enabled:
        return

    mdns_probe = probe_managed_mdns_takeover_conn(target.connection)
    mdns_probe_lines = getattr(mdns_probe, "lines", ())
    if not isinstance(mdns_probe_lines, (list, tuple)):
        mdns_probe_lines = ()
    _add_probe_line_results(
        sink.add,
        mdns_probe_lines,
        fallback_ready=mdns_probe.ready,
        fallback_pass_message="managed mDNS takeover is active",
        fallback_fail_message=f"managed mDNS takeover is not active ({mdns_probe.detail})",
    )


def _doctor_check_active_smb_conf(target: DoctorTarget, remote: RemoteAccess, sink: DoctorSink) -> SmbConfigState:
    if not remote.remote_checks_enabled:
        return SmbConfigState(text=None, reason=remote.active_smb_conf_reason)

    try:
        active_smb_conf = read_active_smb_conf_conn(target.connection)
        if not active_smb_conf.strip():
            reason = "active smb.conf unavailable"
        else:
            reason = ""
        sink.add(check_xattr_tdb_persistence(target.connection, active_smb_conf))
        return SmbConfigState(text=active_smb_conf, reason=reason)
    except Exception as e:
        sink.add(CheckResult("WARN", f"xattr_tdb:file check skipped: {e}"))
        return SmbConfigState(text=None, reason=str(e))


def _doctor_check_network_plan(target: DoctorTarget, remote: RemoteAccess, smb_config: SmbConfigState, sink: DoctorSink) -> NetworkPlanState:
    if not remote.remote_checks_enabled:
        return NetworkPlanState(plan=None, reason="remote checks disabled")

    capability_errors: tuple[str, ...] = ()
    try:
        capabilities = probe_remote_network_capabilities_conn(target.connection)
    except Exception as e:
        capabilities = None
        capability_errors = (f"{type(e).__name__}: {e}",)

    smb_bind_interfaces = ""
    mdns_families: tuple[str, ...] = ()
    nbns_families: tuple[str, ...] = ()
    if capabilities is not None:
        smb_bind_interfaces = capabilities.smb_bind_interfaces
        mdns_families = capabilities.mdns_families
        nbns_families = capabilities.nbns_families
        capability_errors = capabilities.errors

    if not smb_bind_interfaces:
        smb_bind_interfaces = parse_global_option(smb_config.text or "", "interfaces") or ""

    bind_families = bind_interface_families(smb_bind_interfaces)
    if not mdns_families:
        mdns_families = bind_families
    if not nbns_families:
        nbns_families = bind_families
    # NBNS is IPv4-only by protocol; do not inherit Samba/mDNS IPv6 support.
    nbns_families = tuple(family for family in nbns_families if family == "ipv4")

    if not smb_bind_interfaces or not bind_families:
        reason = "runtime network plan unavailable; active smb.conf has no non-loopback bind interfaces"
        if sink.debug_fields is not None:
            sink.debug_fields["runtime_network_plan_unavailable"] = {
                "reason": reason,
                "capability_errors": list(capability_errors),
            }
        return NetworkPlanState(plan=None, reason=reason)

    plan = build_network_check_plan(
        smb_bind_interfaces=smb_bind_interfaces,
        mdns_families=mdns_families,
        nbns_families=nbns_families,
        local_addresses=local_interface_addresses(),
    )
    if sink.debug_fields is not None:
        sink.debug_fields["runtime_network_capabilities"] = {
            "smb_bind_interfaces": smb_bind_interfaces,
            "mdns_families": list(mdns_families),
            "nbns_families": list(nbns_families),
            "errors": list(capability_errors),
        }
        sink.debug_fields["runtime_network_plan"] = _network_plan_debug(plan)
    return NetworkPlanState(plan=plan)


def _doctor_check_direct_smb_port(target: DoctorTarget, remote: RemoteAccess, network_plan: NetworkPlanState, sink: DoctorSink) -> None:
    if target.proxied_ssh:
        sink.add(CheckResult("SKIP", f"direct SMB port check skipped for SSH-proxied target {target.host}"))
        return

    results: list[CheckResult] = []
    if network_plan.plan is not None:
        for family_plan in network_plan.plan.families():
            if not family_plan.samba_expected:
                continue
            family_label = _family_label(family_plan.family)
            if not family_plan.remote_addresses:
                result = CheckResult("SKIP", f"direct SMB {family_label} port check skipped; runtime has no advertised {family_plan.family} address")
            elif not family_plan.local_sources:
                result = CheckResult("SKIP", f"direct SMB {family_label} port check skipped; local host has no address on the remote {family_plan.family} network")
            else:
                result = check_smb_port(family_plan.remote_addresses[0])
            sink.add(result)
            results.append(result)
    else:
        result = check_smb_port(target.host)
        sink.add(result)
        results.append(result)

    if any(result.status != "PASS" and result.status != "SKIP" for result in results):
        _add_remote_service_socket_debug(target, remote, sink)


def _doctor_check_bonjour(
    inputs: DoctorInputs,
    target: DoctorTarget,
    naming: RuntimeNamingState,
    network_plan: NetworkPlanState,
    sink: DoctorSink,
) -> DoctorBonjourResult:
    return _add_bonjour_results(
        inputs.config,
        naming.identity,
        proxied_ssh=target.proxied_ssh,
        skip_bonjour=inputs.options.skip_bonjour,
        network_plan=network_plan.plan,
        add_result=sink.add,
    )


def _doctor_add_bonjour_naming_info(bonjour_result: DoctorBonjourResult, sink: DoctorSink) -> None:
    if bonjour_result.instance is not None:
        sink.add(CheckResult("INFO", f"advertised Bonjour instance: {bonjour_result.instance}"))
    else:
        sink.add(CheckResult("INFO", f"advertised Bonjour instance: unavailable ({bonjour_result.reason})"))

    bonjour_host_label = bonjour_result.target.host_label() if bonjour_result.target is not None else None
    if bonjour_host_label is not None:
        sink.add(CheckResult("INFO", f"advertised Bonjour host label: {bonjour_host_label}"))
    else:
        sink.add(CheckResult("INFO", f"advertised Bonjour host label: unavailable ({bonjour_result.reason})"))


def _doctor_add_active_smb_conf_info(smb_config: SmbConfigState, sink: DoctorSink) -> None:
    _add_active_smb_conf_results(smb_config.text, smb_config.reason, sink.add)


def _doctor_check_nbns(
    target: DoctorTarget,
    remote: RemoteAccess,
    smb_config: SmbConfigState,
    naming: RuntimeNamingState,
    network_plan: NetworkPlanState,
    sink: DoctorSink,
) -> None:
    if not remote.remote_checks_enabled:
        return

    result_start = sink.result_count()
    _add_nbns_results(
        target.connection,
        proxied_ssh=target.proxied_ssh,
        active_smb_conf=smb_config.text,
        runtime_naming_identity=naming.identity,
        network_plan=network_plan.plan,
        add_result=sink.add,
    )
    if any("failed" in result.message for result in sink.new_results_since(result_start)):
        _add_remote_service_socket_debug(target, remote, sink)


def _doctor_check_authenticated_smb(
    inputs: DoctorInputs,
    target: DoctorTarget,
    smb_config: SmbConfigState,
    naming: RuntimeNamingState,
    bonjour_result: DoctorBonjourResult,
    network_plan: NetworkPlanState,
    sink: DoctorSink,
) -> None:
    if inputs.options.skip_smb:
        return

    _add_authenticated_smb_results(
        target.connection,
        inputs.config,
        bonjour_result.target,
        naming.identity,
        host=target.host,
        smb_password=target.smb_password,
        proxied_ssh=target.proxied_ssh,
        active_smb_conf=smb_config.text,
        network_plan=network_plan.plan,
        debug_fields=sink.debug_fields,
        add_result=sink.add,
    )
