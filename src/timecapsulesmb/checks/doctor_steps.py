from __future__ import annotations

import ipaddress
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from timecapsulesmb.checks.bonjour import (
    BonjourExpectedIdentity,
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
    authenticated_smb_listing_attempts,
    authenticated_smb_listing_retryable,
    authenticated_smb_listing_with_attempts,
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
from timecapsulesmb.core.net import endpoint_host
from timecapsulesmb.device.compat import is_netbsd4_payload_family, is_netbsd6_payload_family, render_compatibility_message
from timecapsulesmb.device.probe import (
    FLASH_RUNTIME_CONFIG,
    ProbedDeviceState,
    ReadinessProbeResult,
    RemoteInterfaceProbeResult,
    RUNTIME_RAM_ROOT,
    RUNTIME_SMB_CONF,
    RuntimeNamingIdentityProbeResult,
    flash_runtime_config_present_conn,
    nbns_flash_config_enabled_conn,
    probe_connection_state,
    probe_managed_mdns_takeover_conn,
    probe_managed_smbd_conn,
    probe_manager_startup_age_conn,
    probe_remote_network_capabilities_conn,
    probe_remote_interface_conn,
    probe_remote_runtime_naming_identity_conn,
    read_deployed_version_conn,
    read_active_smb_conf_conn,
    runtime_ram_root_present_conn,
)
from timecapsulesmb.discovery.bonjour import BonjourDiscoverySnapshot, BonjourResolvedService, BonjourServiceInstance
from timecapsulesmb.discovery.native_dns_sd import (
    NativeDnsSdDiscoveryDiagnostics,
    discover_native_dns_sd_snapshot_detailed,
    native_dns_sd_available,
    resolve_native_dns_sd_service_instance,
)
from timecapsulesmb.transport.local import find_free_local_port
from timecapsulesmb.transport.local import command_exists
from timecapsulesmb.transport.ssh import SshConnection, ssh_local_forward


T = TypeVar("T")


DOCTOR_TRANSIENT_RETRY_DELAYS = (10, 15)
TRANSIENT_SMBD_READINESS_FAILURES = {
    "managed smbd parent process is not running",
    "smbd is not bound to required TCP 445 sockets",
}
TRANSIENT_MDNS_READINESS_FAILURES = {
    "mdns-advertiser process is not running",
    "mdns-advertiser is not bound to required UDP 5353 listener",
}
STARTUP_GRACE_MASK = "mask"
STARTUP_GRACE_PRESERVE = "preserve"
STARTUP_GRACE_DETAIL_KEY = "startup_grace"
DOCTOR_CODE_RUNTIME_NOT_INSTALLED = "runtime_not_installed"
DOCTOR_CODE_SMB_BIND_LAN_ONLY_UNREACHABLE = "smb_bind_lan_only_unreachable"
DOCTOR_CODE_DEVICE_STARTING_UP = "device_starting_up"
DOCTOR_CODE_PAYLOAD_MISSING_FROM_DISK = "payload_missing_from_disk"
DOCTOR_PAYLOAD_MISSING_FROM_DISK_MESSAGE = "active smb.conf xattr_tdb:file parent is missing"
DOCTOR_STARTUP_GRACE_SECONDS = 180
STARTUP_GRACE_TRANSIENT_PROBE_FAILURES = {
    "managed runtime smbd binary missing",
    "managed runtime smb.conf missing",
    "active smb.conf passdb backend is not staged in RAM",
    "active smb.conf username map is not staged in RAM",
    "active smb.conf xattr_tdb:file is not persistent disk storage",
    "one or more managed share volumes are not mounted",
    "manager is not running for managed runtime",
    "managed smbd parent process is not running",
    "smbd is not bound to required TCP 445 sockets",
    "managed smbd readiness probe timed out",
    "device Samba version unavailable (managed runtime smbd binary missing)",
    "mDNS startup deferred; no usable address has appeared yet",
    "mdns-advertiser process is not running",
    "mdns-advertiser bound to UDP 5353 but bind address is not active",
    "mdns-advertiser is waiting for a usable address",
    "mdns-advertiser is not bound to required UDP 5353 listener",
    "Apple mDNSResponder is still running",
}
SMB_CONNECTION_SHAPED_FAILURE_TOKENS = (
    "NT_STATUS_CONNECTION_REFUSED",
    "NT_STATUS_HOST_UNREACHABLE",
    "NT_STATUS_IO_TIMEOUT",
    "NT_STATUS_CONNECTION_RESET",
    "NT_STATUS_INVALID_NETWORK_RESPONSE",
    "NT_STATUS_BAD_NETWORK_NAME",
    "Connection refused",
    "Connection reset",
    "Host is down",
    "No route to host",
    "Network is unreachable",
    "Operation timed out",
    "timed out",
)
SMB_PERSISTENT_FAILURE_TOKENS = (
    "NT_STATUS_LOGON_FAILURE",
    "NT_STATUS_ACCESS_DENIED",
    "NT_STATUS_WRONG_PASSWORD",
)


def _run_doctor_retryable_check(
    run_attempt: Callable[[], T],
    should_retry: Callable[[T], bool],
    *,
    retry_delays: tuple[int, ...] = DOCTOR_TRANSIENT_RETRY_DELAYS,
    before_retry: Callable[[T, int], None] | None = None,
) -> T:
    result = run_attempt()
    for retry_delay in retry_delays:
        if not should_retry(result):
            break
        if before_retry is not None:
            before_retry(result, retry_delay)
        time.sleep(retry_delay)
        result = run_attempt()
    return result


def _readiness_failure_details(probe: ReadinessProbeResult) -> list[str]:
    details: list[str] = []
    steps = getattr(probe, "steps", ())
    if isinstance(steps, (list, tuple)):
        for step in steps:
            if getattr(step, "status", None) in {"fail", "timeout"}:
                detail = getattr(step, "detail", None)
                if isinstance(detail, str) and detail:
                    details.append(detail)
    if details:
        return details

    lines = getattr(probe, "lines", ())
    if not isinstance(lines, (list, tuple)):
        return []
    return [line.removeprefix("FAIL:") for line in lines if isinstance(line, str) and line.startswith("FAIL:")]


def _readiness_probe_retryable(probe: ReadinessProbeResult, retryable_failures: set[str]) -> bool:
    if probe.ready:
        return False
    failure_details = _readiness_failure_details(probe)
    return bool(failure_details) and all(detail in retryable_failures for detail in failure_details)


def _with_startup_grace_policy(result: CheckResult, policy: str) -> CheckResult:
    details = dict(result.details)
    details[STARTUP_GRACE_DETAIL_KEY] = policy
    return CheckResult(result.status, result.message, details)


def _startup_transient_result(status: str, message: str, details: dict[str, object] | None = None) -> CheckResult:
    resolved_details = dict(details or {})
    resolved_details[STARTUP_GRACE_DETAIL_KEY] = STARTUP_GRACE_MASK
    return CheckResult(status, message, resolved_details)


def _probe_failure_details(message: str) -> dict[str, object]:
    details: dict[str, object] = {}
    if message == DOCTOR_PAYLOAD_MISSING_FROM_DISK_MESSAGE:
        details["code"] = DOCTOR_CODE_PAYLOAD_MISSING_FROM_DISK
        details[STARTUP_GRACE_DETAIL_KEY] = STARTUP_GRACE_PRESERVE
    elif message in STARTUP_GRACE_TRANSIENT_PROBE_FAILURES:
        details[STARTUP_GRACE_DETAIL_KEY] = STARTUP_GRACE_MASK
    return details


def _smb_failure_texts(result: CheckResult) -> list[str]:
    texts = [result.message]
    attempts = result.details.get("attempts")
    if isinstance(attempts, list):
        for attempt in attempts:
            if not isinstance(attempt, dict):
                continue
            for key in ("failure", "stderr_tail", "stdout_tail", "outcome"):
                value = attempt.get(key)
                if isinstance(value, str) and value:
                    texts.append(value)
    for key in ("failure", "stderr_tail", "stdout_tail", "error"):
        value = result.details.get(key)
        if isinstance(value, str) and value:
            texts.append(value)
    return texts


def _smb_failure_is_connection_shaped(result: CheckResult) -> bool:
    if result.status != "FAIL":
        return False
    texts = _smb_failure_texts(result)
    if any(token in text for token in SMB_PERSISTENT_FAILURE_TOKENS for text in texts):
        return False
    return any(token in text for token in SMB_CONNECTION_SHAPED_FAILURE_TOKENS for text in texts)


def _tag_smb_startup_transient_if_connection_shaped(result: CheckResult) -> CheckResult:
    if _smb_failure_is_connection_shaped(result):
        return _with_startup_grace_policy(result, STARTUP_GRACE_MASK)
    return result


def _authenticated_smb_listing_with_doctor_retries(
    username: str,
    password: str,
    server: SmbClientTargetInput | list[SmbClientTargetInput],
    *,
    port: int | None = None,
) -> CheckResult:
    attempts: list[dict[str, object]] = []

    def run_attempt() -> CheckResult:
        result = check_authenticated_smb_listing(username, password, server, port=port)
        attempts.extend(authenticated_smb_listing_attempts(result))
        return authenticated_smb_listing_with_attempts(result, attempts)

    def mark_retry_delay(result: CheckResult, retry_delay: int) -> None:
        for attempt in authenticated_smb_listing_attempts(result):
            if "next_retry_delay_sec" not in attempt:
                attempt["next_retry_delay_sec"] = retry_delay

    return _run_doctor_retryable_check(
        run_attempt,
        authenticated_smb_listing_retryable,
        before_retry=mark_retry_delay,
    )


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
            message = line.removeprefix("FAIL:")
            add_result(CheckResult("FAIL", message, _probe_failure_details(message)))
            emitted = True

    if emitted:
        return

    if fallback_ready:
        add_result(CheckResult("PASS", fallback_pass_message))
    else:
        add_result(_startup_transient_result("FAIL", fallback_fail_message))


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
_BONJOUR_HOST_LABEL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_ADISK_DISK_KEY_RE = re.compile(r"^dk[0-9]+$")


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


def _canonical_bonjour_host(hostname: str | None) -> str:
    return (hostname or "").strip().rstrip(".").lower()


def _bonjour_host_label(hostname: str | None) -> str | None:
    host = (hostname or "").strip().rstrip(".")
    if not host:
        return None
    if host.lower().endswith(".local"):
        return host[: -len(".local")]
    return host


def _is_bonjour_host_label_safe(label: str | None) -> bool:
    return bool(label and _BONJOUR_HOST_LABEL_RE.fullmatch(label))


def _bonjour_tcp_service_name(service_label: str) -> str:
    return f"{service_label}._tcp"


def _add_bonjour_target_host_label_result(
    service_label: str,
    hostname: str | None,
    add_result: Callable[[CheckResult], None],
) -> bool:
    service_name = _bonjour_tcp_service_name(service_label)
    host = (hostname or "").strip().rstrip(".")
    label = _bonjour_host_label(host)
    if not host or label is None:
        add_result(CheckResult("FAIL", f"Bonjour {service_name} service target host is unavailable"))
        return True
    if _is_bonjour_host_label_safe(label):
        add_result(CheckResult("PASS", f"Bonjour {service_name} target host label is DNS-safe for Time Machine: {label}"))
        return False
    add_result(
        CheckResult(
            "FAIL",
            f"Bonjour {service_name} target host {host!r} uses unsafe label {label!r}; "
            "Time Machine Settings may ignore SRV targets with spaces or punctuation",
        )
    )
    return True


def _add_expected_bonjour_host_label_result(
    target: BonjourServiceTarget,
    expected_host_label: str | None,
    add_result: Callable[[CheckResult], None],
) -> bool:
    if not expected_host_label:
        return False
    actual_host_label = target.host_label()
    if not actual_host_label:
        add_result(CheckResult("FAIL", f"_smb._tcp service target did not expose a host label; expected {expected_host_label!r}"))
        return True
    if actual_host_label.lower() == expected_host_label.lower():
        add_result(CheckResult("PASS", f"_smb._tcp target host label matches runtime mDNS host label {expected_host_label!r}"))
        return False
    add_result(
        CheckResult(
            "FAIL",
            f"_smb._tcp target host label {actual_host_label!r} does not match runtime mDNS host label {expected_host_label!r}",
        )
    )
    return True


def _bonjour_records_for_instance(
    records: Iterable[BonjourResolvedService],
    instance_name: str | None,
    service_label: str,
) -> list[BonjourResolvedService]:
    if instance_name is None:
        return []
    return [
        record
        for record in records
        if record.name == instance_name and _bonjour_service_label(record.service_type) == service_label
    ]


def _packed_txt_fields(value: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for chunk in value.split(","):
        if "=" not in chunk:
            continue
        key, field_value = chunk.split("=", 1)
        key = key.strip()
        if key:
            fields[key] = field_value.strip()
    return fields


def _adisk_disk_fields(record: BonjourResolvedService) -> dict[str, dict[str, str]]:
    disks: dict[str, dict[str, str]] = {}
    for key, value in record.properties.items():
        if _ADISK_DISK_KEY_RE.fullmatch(key):
            disks[key] = _packed_txt_fields(value)
    return disks


def _add_time_machine_adisk_results(
    records: Iterable[BonjourResolvedService],
    *,
    instance_name: str | None,
    smb_hostname: str | None,
    active_share_names: list[str],
    add_result: Callable[[CheckResult], None],
) -> bool:
    if instance_name is None:
        return False

    failed = False
    adisk_records = _bonjour_records_for_instance(records, instance_name, "_adisk")
    if not adisk_records:
        related_records = [
            record
            for record in records
            if record.name == instance_name and _bonjour_service_label(record.service_type) in {"_airport", "_device-info"}
        ]
        if active_share_names and related_records:
            add_result(
                CheckResult(
                    "FAIL",
                    f"_adisk._tcp Time Machine service missing for {instance_name!r}; "
                    f"Time Machine Settings will not list active shares: {', '.join(active_share_names)}",
                )
            )
            return True
        return False

    adisk_record = sorted(adisk_records, key=lambda record: (record.hostname or "", record.fullname or ""))[0]
    add_result(CheckResult("PASS", f"discovered _adisk._tcp Time Machine service for {instance_name!r}"))

    failed = _add_bonjour_target_host_label_result("_adisk", adisk_record.hostname, add_result) or failed
    if smb_hostname and _canonical_bonjour_host(adisk_record.hostname) == _canonical_bonjour_host(smb_hostname):
        add_result(CheckResult("PASS", f"_adisk._tcp target host matches _smb._tcp target host {_canonical_bonjour_host(smb_hostname)}"))
    elif smb_hostname:
        failed = True
        add_result(
            CheckResult(
                "FAIL",
                f"_adisk._tcp target host {_canonical_bonjour_host(adisk_record.hostname) or 'unavailable'} "
                f"does not match _smb._tcp target host {_canonical_bonjour_host(smb_hostname)}",
            )
        )

    sys_txt = adisk_record.properties.get("sys", "")
    if sys_txt and "adVF=" in sys_txt:
        add_result(CheckResult("PASS", "_adisk._tcp TXT includes Time Machine system flags"))
    else:
        failed = True
        add_result(CheckResult("FAIL", "_adisk._tcp TXT is missing Time Machine system flags"))

    disk_fields = _adisk_disk_fields(adisk_record)
    if not disk_fields:
        failed = True
        add_result(CheckResult("FAIL", "_adisk._tcp TXT does not advertise any Time Machine disks"))
        return failed

    advertised_shares: list[str] = []
    for disk_key, fields in sorted(disk_fields.items()):
        missing_fields = [field for field in ("adVF", "adVN", "adVU") if not fields.get(field)]
        if missing_fields:
            failed = True
            add_result(CheckResult("FAIL", f"_adisk._tcp TXT disk {disk_key} is missing fields: {', '.join(missing_fields)}"))
            continue
        share_name = fields["adVN"]
        if share_name not in advertised_shares:
            advertised_shares.append(share_name)

    if active_share_names:
        active_set = set(active_share_names)
        advertised_set = set(advertised_shares)
        missing = [share for share in active_share_names if share not in advertised_set]
        extra = [share for share in advertised_shares if share not in active_set]
        if missing:
            failed = True
            add_result(
                CheckResult(
                    "FAIL",
                    f"_adisk._tcp TXT does not advertise active Samba share(s): {', '.join(missing)}",
                )
            )
        if extra:
            failed = True
            add_result(
                CheckResult(
                    "FAIL",
                    f"_adisk._tcp TXT advertises stale share(s) not present in active Samba config: {', '.join(extra)}",
                )
            )
        if not missing and not extra:
            add_result(CheckResult("PASS", f"_adisk._tcp TXT advertises active Time Machine shares: {', '.join(active_share_names)}"))

    return failed


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


@dataclass
class _BonjourAttemptOutcome:
    results: list[CheckResult]
    instance: str | None = None
    target: BonjourServiceTarget | None = None
    service_targets: dict[str, tuple[str, ...]] | None = None
    reason: str = ""
    debug_needed: bool = False
    fallback_allowed: bool = False


def _status_failed(results: Iterable[CheckResult]) -> bool:
    return any(result.status == "FAIL" for result in results)


def _evaluate_bonjour_snapshot(
    smb_snapshot: BonjourDiscoverySnapshot,
    bonjour_expected: BonjourExpectedIdentity,
    *,
    target_ip: str | None,
    family: str | None,
    interfaces: list[str] | None,
    active_share_names: list[str],
    resolver: Callable[..., tuple[BonjourResolvedService | None, CheckResult | None]],
    browse_miss_message: str,
    targeted_resolve_pass_message: str,
) -> _BonjourAttemptOutcome:
    results: list[CheckResult] = []
    outcome = _BonjourAttemptOutcome(results=results, service_targets={})

    def add(result: CheckResult) -> None:
        results.append(result)

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
            resolver=resolver,
        )
        if resolution.source == "browse":
            for result in check_smb_instance(resolution.selection):
                add(result)
        elif resolution.record is not None:
            add(CheckResult("INFO", browse_miss_message))
            add(CheckResult("PASS", targeted_resolve_pass_message))
        else:
            for result in check_smb_instance(resolution.selection):
                add(result)

        if resolution.error is not None:
            outcome.reason = resolution.error.message
            outcome.debug_needed = True
            outcome.fallback_allowed = True
            add(resolution.error)
        elif resolution.record is not None:
            outcome.instance = resolution.instance.name
            records_for_targets = list(smb_snapshot.resolved)
            records_for_targets.append(resolution.record)
            outcome.service_targets = _bonjour_service_targets_for_instance(records_for_targets, resolution.instance.name)
            if _add_bonjour_service_target_consistency_results(resolution.instance.name, outcome.service_targets, add):
                outcome.debug_needed = True
            target = resolve_smb_service_target(
                resolution.record,
                expected_instance_name=bonjour_expected.instance_name,
            )
            target_result = check_smb_service_target(target)
            if target_result.status == "FAIL":
                outcome.debug_needed = True
            add(target_result)
            if target.hostname:
                outcome.target = target
                if _add_bonjour_target_host_label_result("_smb", target.hostname, add):
                    outcome.debug_needed = True
                    outcome.fallback_allowed = True
                if _add_expected_bonjour_host_label_result(target, bonjour_expected.host_label, add):
                    outcome.debug_needed = True
                    outcome.fallback_allowed = True
                host_ip_result = _add_bonjour_host_ip_results(
                    target.hostname,
                    expected_ip=target_ip,
                    record_ips=_record_ips(resolution.record),
                    add_result=add,
                )
                if host_ip_result.status == "FAIL":
                    outcome.debug_needed = True
            if _add_time_machine_adisk_results(
                records_for_targets,
                instance_name=resolution.instance.name,
                smb_hostname=target.hostname,
                active_share_names=active_share_names,
                add_result=add,
            ):
                outcome.debug_needed = True
                outcome.fallback_allowed = True
        else:
            outcome.debug_needed = True
            outcome.fallback_allowed = True
    elif target_ip is not None:
        resolved_record = select_resolved_smb_record_by_ip(
            smb_records,
            target_ip,
        )
        if resolved_record is None:
            outcome.debug_needed = True
            outcome.fallback_allowed = True
            outcome.reason = f"no resolved _smb._tcp service matched target IP {target_ip}"
            add(CheckResult("FAIL", outcome.reason))
        else:
            outcome.instance = resolved_record.name
            outcome.service_targets = _bonjour_service_targets_for_instance(smb_snapshot.resolved, resolved_record.name)
            if _add_bonjour_service_target_consistency_results(resolved_record.name, outcome.service_targets, add):
                outcome.debug_needed = True
            add(CheckResult("PASS", f"discovered _smb._tcp service matching target IP {target_ip}"))
            target = resolve_smb_service_target(
                resolved_record,
                expected_instance_name=None,
            )
            target_result = check_smb_service_target(target)
            if target_result.status == "FAIL":
                outcome.debug_needed = True
            add(target_result)
            if target.hostname:
                outcome.target = target
                if _add_bonjour_target_host_label_result("_smb", target.hostname, add):
                    outcome.debug_needed = True
                    outcome.fallback_allowed = True
                if _add_expected_bonjour_host_label_result(target, bonjour_expected.host_label, add):
                    outcome.debug_needed = True
                    outcome.fallback_allowed = True
                host_ip_result = _add_bonjour_host_ip_results(
                    target.hostname,
                    expected_ip=target_ip,
                    record_ips=_record_ips(resolved_record),
                    add_result=add,
                )
                if host_ip_result.status == "FAIL":
                    outcome.debug_needed = True
            if _add_time_machine_adisk_results(
                smb_snapshot.resolved,
                instance_name=resolved_record.name,
                smb_hostname=target.hostname,
                active_share_names=active_share_names,
                add_result=add,
            ):
                outcome.debug_needed = True
                outcome.fallback_allowed = True

    return outcome


def _evaluate_zeroconf_bonjour_attempt(
    smb_snapshot: BonjourDiscoverySnapshot | None,
    discovery_error: CheckResult | None,
    bonjour_expected: BonjourExpectedIdentity,
    *,
    target_ip: str | None,
    family: str | None,
    interfaces: list[str] | None,
    active_share_names: list[str],
) -> _BonjourAttemptOutcome:
    if discovery_error is not None:
        return _BonjourAttemptOutcome(
            results=[discovery_error],
            reason=discovery_error.message,
            debug_needed=True,
            fallback_allowed=True,
            service_targets={},
        )

    assert smb_snapshot is not None
    expected_name = bonjour_expected.instance_name
    return _evaluate_bonjour_snapshot(
        smb_snapshot,
        bonjour_expected,
        target_ip=target_ip,
        family=family,
        interfaces=interfaces,
        active_share_names=active_share_names,
        resolver=resolve_smb_instance,
        browse_miss_message=(
            f"Python zeroconf browse did not observe expected _smb._tcp instance {expected_name!r}; "
            "targeted resolve succeeded"
        ),
        targeted_resolve_pass_message=f"resolved expected _smb._tcp instance {expected_name!r} by targeted query",
    )


def _native_smb_resolver(
    diagnostics: NativeDnsSdDiscoveryDiagnostics,
) -> Callable[..., tuple[BonjourResolvedService | None, CheckResult | None]]:
    def resolve(
        instance: BonjourServiceInstance,
        *,
        missing_message: str | None = None,
        family: str | None = None,
        **_kwargs: object,
    ) -> tuple[BonjourResolvedService | None, CheckResult | None]:
        record, resolve_result = resolve_native_dns_sd_service_instance(
            instance.service_type,
            instance.name,
            timeout_sec=diagnostics.timeout_sec,
            family=family,  # type: ignore[arg-type]
        )
        diagnostics.resolves.append(resolve_result)
        if record is None:
            return None, CheckResult(
                "FAIL",
                missing_message or f"discovered _smb._tcp instance {instance.name!r} but could not resolve service target",
            )
        return record, None

    return resolve


def _evaluate_native_bonjour_attempt(
    bonjour_expected: BonjourExpectedIdentity,
    *,
    target_ip: str | None,
    family: str | None,
    interfaces: list[str] | None,
    active_share_names: list[str],
) -> tuple[_BonjourAttemptOutcome | None, NativeDnsSdDiscoveryDiagnostics | None]:
    native_result = discover_native_dns_sd_snapshot_detailed(
        None,
        target_ip=target_ip,
        family=family,  # type: ignore[arg-type]
    )
    if native_result is None:
        return None, None

    native_snapshot, native_debug = native_result
    expected_name = bonjour_expected.instance_name
    outcome = _evaluate_bonjour_snapshot(
        native_snapshot,
        bonjour_expected,
        target_ip=target_ip,
        family=family,
        interfaces=interfaces,
        active_share_names=active_share_names,
        resolver=_native_smb_resolver(native_debug),
        browse_miss_message=(
            f"native macOS dns-sd browse did not observe expected _smb._tcp instance {expected_name!r}; "
            "targeted resolve succeeded"
        ),
        targeted_resolve_pass_message=f"native macOS dns-sd resolved expected _smb._tcp instance {expected_name!r} by targeted query",
    )
    return outcome, native_debug


def _should_try_native_bonjour_fallback(outcome: _BonjourAttemptOutcome) -> bool:
    return outcome.fallback_allowed and _status_failed(outcome.results) and native_dns_sd_available()


def _add_bonjour_results(
    config: AppConfig,
    runtime_naming_identity: RuntimeNamingIdentityProbeResult | None,
    *,
    proxied_ssh: bool,
    skip_bonjour: bool,
    network_plan: NetworkCheckPlan | None = None,
    active_share_names: list[str] | None = None,
    add_result: Callable[[CheckResult], None],
) -> DoctorBonjourResult:
    bonjour_instance: str | None = None
    bonjour_target: BonjourServiceTarget | None = None
    bonjour_reason = "Bonjour check not run"
    bonjour_debug_needed = False
    bonjour_expected_debug: dict[str, str | None] | None = None
    bonjour_zeroconf_debug: object | None = None
    bonjour_native_fallback_debug: object | None = None
    native_fallback_diagnostics: list[object] = []
    bonjour_backend_debug: dict[str, str] = {}
    bonjour_service_targets: dict[str, tuple[str, ...]] = {}
    active_share_names = active_share_names or []

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
                backend_key = family or "default"

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
                zeroconf_outcome = _evaluate_zeroconf_bonjour_attempt(
                    smb_snapshot,
                    discovery_error,
                    bonjour_expected,
                    target_ip=target_ip,
                    family=family,
                    interfaces=interfaces,
                    active_share_names=active_share_names,
                )

                chosen_outcome = zeroconf_outcome
                if _should_try_native_bonjour_fallback(zeroconf_outcome):
                    native_outcome, native_debug = _evaluate_native_bonjour_attempt(
                        bonjour_expected,
                        target_ip=target_ip,
                        family=family,
                        interfaces=interfaces,
                        active_share_names=active_share_names,
                    )
                    if native_debug is not None:
                        native_fallback_diagnostics.append(native_debug)
                    if native_outcome is not None and not _status_failed(native_outcome.results):
                        bonjour_debug_needed = True
                        bonjour_backend_debug[backend_key] = "native_dns_sd"
                        add_attempt_result(CheckResult("INFO", "Python zeroconf did not produce a usable Bonjour result; using native macOS dns-sd fallback"))
                        chosen_outcome = native_outcome

                if backend_key not in bonjour_backend_debug:
                    bonjour_backend_debug[backend_key] = "zeroconf"
                for result in chosen_outcome.results:
                    add_attempt_result(result)
                if chosen_outcome.reason:
                    bonjour_reason = chosen_outcome.reason
                bonjour_debug_needed = bonjour_debug_needed or chosen_outcome.debug_needed
                if chosen_outcome.instance is not None:
                    bonjour_instance = chosen_outcome.instance
                if chosen_outcome.target is not None:
                    bonjour_target = chosen_outcome.target
                if chosen_outcome.service_targets:
                    bonjour_service_targets = chosen_outcome.service_targets
            if len(attempt_diagnostics) == 1:
                bonjour_zeroconf_debug = attempt_diagnostics[0]
            elif attempt_diagnostics:
                bonjour_zeroconf_debug = attempt_diagnostics
            if len(native_fallback_diagnostics) == 1:
                bonjour_native_fallback_debug = native_fallback_diagnostics[0]
            elif native_fallback_diagnostics:
                bonjour_native_fallback_debug = native_fallback_diagnostics
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
        native_fallback_debug=bonjour_native_fallback_debug,
        backend_debug=bonjour_backend_debug or None,
    )


def _listing_disk_shares(listing_result: CheckResult) -> list[str]:
    value = listing_result.details.get("disk_shares")
    if not isinstance(value, list):
        return []
    shares: list[str] = []
    for item in value:
        if isinstance(item, str) and item and item not in shares:
            shares.append(item)
    return shares


def _select_smb_file_ops_share(
    listing_result: CheckResult,
    active_share_names: list[str],
    active_smb_conf_reason: str,
    add_result: Callable[[CheckResult], None],
) -> str | None:
    disk_shares = _listing_disk_shares(listing_result)
    if active_share_names:
        for active_share_name in active_share_names:
            if active_share_name in disk_shares:
                add_result(CheckResult("PASS", f"authenticated SMB listing includes active share {active_share_name!r}"))
                return active_share_name
        expected = ", ".join(active_share_names)
        listed = ", ".join(disk_shares) if disk_shares else "none"
        add_result(
            CheckResult(
                "FAIL",
                f"authenticated SMB listing did not include any active Samba share; expected one of: {expected}; listed disk shares: {listed}",
            )
        )
        return None

    if not disk_shares:
        add_result(CheckResult("FAIL", "authenticated SMB listing worked, but no disk shares were advertised"))
        return None

    reason = active_smb_conf_reason or "active smb.conf did not list share names"
    add_result(CheckResult("INFO", f"active Samba share comparison skipped; {reason}"))
    return disk_shares[0]


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


def _config_bool_enabled(config: AppConfig, key: str) -> bool:
    return config.get(key).strip().lower() in {"1", "true", "yes", "on"}


def _format_list_for_message(values: Iterable[str]) -> str:
    items = [value for value in values if value]
    if not items:
        return "none"
    return ", ".join(items)


def _lan_only_smb_bind_unreachable_result(
    config: AppConfig,
    network_plan: NetworkCheckPlan | None,
    smb_targets: Iterable[SmbClientTargetInput],
) -> CheckResult | None:
    if network_plan is None or not _config_bool_enabled(config, "TC_SMB_BIND_LAN_ONLY"):
        return None

    samba_families = [
        family_plan
        for family_plan in network_plan.families()
        if family_plan.samba_expected and family_plan.remote_addresses
    ]
    if not samba_families:
        return None
    if any(family_plan.local_sources for family_plan in samba_families):
        return None

    bound_addresses = [
        address
        for family_plan in samba_families
        for address in family_plan.remote_addresses
    ]
    bound_cidrs = [
        cidr
        for family_plan in samba_families
        for cidr in family_plan.remote_cidrs
    ]
    target_displays = [_smb_client_target_debug(target) for target in smb_targets]
    target_ips = [
        ip
        for target in smb_targets
        for ip in [_ip_literal(target.ip_address if isinstance(target, SmbClientTarget) else target)]
        if ip is not None
    ]
    outside_targets = [ip for ip in target_ips if ip not in bound_addresses]
    outside_clause = ""
    if outside_targets:
        outside_clause = (
            f"; checked SMB target(s) {_format_list_for_message(target_displays)} "
            f"outside the bound address(es) {_format_list_for_message(bound_addresses)}"
        )
    return CheckResult(
        "FAIL",
        "SMB is configured to bind to LAN-only interface(s) "
        f"{_format_list_for_message(bound_cidrs or bound_addresses)}, "
        "but this Mac has no address on those runtime Samba network(s)"
        f"{outside_clause}. Disable Bind SMB to LAN Only for this profile and redeploy, "
        "or connect from the Time Capsule LAN side.",
        {
            "code": DOCTOR_CODE_SMB_BIND_LAN_ONLY_UNREACHABLE,
            "domain": "SMB Auth",
            "smb_bind_lan_only": True,
            "bound_addresses": bound_addresses,
            "bound_cidrs": bound_cidrs,
            "checked_targets": target_displays,
            "outside_checked_target_ips": outside_targets,
        },
    )


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
    active_share_names: list[str],
    active_smb_conf_reason: str,
    remote_port: int,
    debug_prefix: str,
    debug_fields: dict[str, object] | None,
    add_result: Callable[[CheckResult], None],
) -> bool:
    local_port = find_free_local_port()
    if debug_fields is not None:
        debug_fields[f"{debug_prefix}_listing_servers"] = ["127.0.0.1"]
        debug_fields[f"{debug_prefix}_listing_active_shares"] = active_share_names
    try:
        with ssh_local_forward(
            connection,
            local_port=local_port,
            remote_host=host,
            remote_port=remote_port,
        ):
            listing_result = _authenticated_smb_listing_with_doctor_retries(
                DEFAULT_SAMBA_AUTH_USER,
                smb_password,
                "127.0.0.1",
                port=local_port,
            )
            if debug_fields is not None and listing_result.details.get("attempts"):
                debug_fields[f"{debug_prefix}_listing_attempts"] = listing_result.details["attempts"]
            add_result(_tag_smb_startup_transient_if_connection_shaped(listing_result))
            if listing_result.status != "PASS":
                return False
            share_name = _select_smb_file_ops_share(
                listing_result,
                active_share_names,
                active_smb_conf_reason,
                add_result,
            )
            if share_name is None:
                return False

            file_ops_ok = True
            for result in check_authenticated_smb_file_ops_detailed(
                DEFAULT_SAMBA_AUTH_USER,
                smb_password,
                "127.0.0.1",
                share_name,
                port=local_port,
            ):
                add_result(_tag_smb_startup_transient_if_connection_shaped(result))
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
    active_smb_conf_reason: str,
    network_plan: NetworkCheckPlan | None,
    debug_fields: dict[str, object] | None,
    add_result: Callable[[CheckResult], None],
) -> None:
    active_share_names = parse_active_share_names(active_smb_conf or "")
    if proxied_ssh:
        _add_tunneled_authenticated_smb_results(
            connection,
            host=host,
            smb_password=smb_password,
            active_share_names=active_share_names,
            active_smb_conf_reason=active_smb_conf_reason,
            remote_port=445,
            debug_prefix="authenticated_smb",
            debug_fields=debug_fields,
            add_result=add_result,
        )
        return

    smb_servers = _doctor_smb_client_targets(config, bonjour_target, runtime_naming_identity, network_plan)
    if debug_fields is not None:
        debug_fields["authenticated_smb_listing_servers"] = [_smb_client_target_debug(target) for target in smb_servers]
        debug_fields["authenticated_smb_listing_active_shares"] = active_share_names
    listing_result = _authenticated_smb_listing_with_doctor_retries(
        DEFAULT_SAMBA_AUTH_USER,
        smb_password,
        smb_servers,
        port=445,
    )
    if debug_fields is not None and listing_result.details.get("attempts"):
        debug_fields["authenticated_smb_listing_attempts"] = listing_result.details["attempts"]
    if listing_result.status != "PASS":
        bind_result = _lan_only_smb_bind_unreachable_result(config, network_plan, smb_servers)
        if bind_result is not None:
            add_result(bind_result)
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
                active_share_names=active_share_names,
                active_smb_conf_reason=active_smb_conf_reason,
                remote_port=445,
                debug_prefix="authenticated_smb_tunnel",
                debug_fields=debug_fields,
                add_result=add_result,
            ):
                return
        add_result(_tag_smb_startup_transient_if_connection_shaped(listing_result))
        return
    add_result(listing_result)
    share_name = _select_smb_file_ops_share(
        listing_result,
        active_share_names,
        active_smb_conf_reason,
        add_result,
    )
    if share_name is None:
        return

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
        add_result(_tag_smb_startup_transient_if_connection_shaped(result))


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
        host=endpoint_host(connection.host),
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
        sink.add(
            CheckResult(
                "FAIL",
                "deployed payload config not found; please run deploy to install on your device",
                details={"code": DOCTOR_CODE_RUNTIME_NOT_INSTALLED},
            )
        )
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


def _doctor_probe_startup_age(target: DoctorTarget, remote: RemoteAccess, sink: DoctorSink) -> float | None:
    if not remote.remote_checks_enabled:
        return None
    try:
        probe = probe_manager_startup_age_conn(target.connection)
    except Exception as e:
        if sink.debug_fields is not None:
            sink.debug_fields["manager_startup_age_error"] = f"{type(e).__name__}: {e}"
        return None
    if sink.debug_fields is not None:
        sink.debug_fields["manager_startup_age"] = {
            "seconds_ago": probe.manager_started_seconds_ago,
            "detail": probe.detail,
        }
    return probe.manager_started_seconds_ago


def _apply_startup_grace(
    results: list[CheckResult],
    manager_started_seconds_ago: float | None,
    *,
    grace_seconds: int = DOCTOR_STARTUP_GRACE_SECONDS,
) -> tuple[list[CheckResult], tuple[CheckResult, ...]]:
    """Collapse pure startup-window failures into a single actionable FAIL.

    When the device's manager started less than grace_seconds ago, failures are
    only maskable when every FAIL explicitly opted in with startup_grace=mask.
    Unknown or persistent failures keep the original results so "wait and retry"
    is never the headline for a failure that waiting cannot fix.
    """
    if manager_started_seconds_ago is None:
        return results, ()
    if not 0 <= manager_started_seconds_ago < grace_seconds:
        return results, ()
    failures = [result for result in results if result.status == "FAIL"]
    if not failures:
        return results, ()
    if not all(_startup_grace_can_mask_failure(result) for result in failures):
        recent_startup_note = CheckResult(
            "INFO",
            f"device services started {int(manager_started_seconds_ago)}s ago; "
            "some failures above may resolve once startup completes",
            {
                "domain": "Runtime",
                "manager_started_seconds_ago": int(manager_started_seconds_ago),
                "startup_grace_seconds": grace_seconds,
            },
        )
        return [*results, recent_startup_note], (recent_startup_note,)
    transformed: list[CheckResult] = []
    for result in results:
        if result.status == "FAIL":
            details = dict(result.details)
            details["masked_by"] = DOCTOR_CODE_DEVICE_STARTING_UP
            transformed.append(CheckResult("INFO", result.message, details))
        else:
            transformed.append(result)
    startup_fail = CheckResult(
        "FAIL",
        f"some checks failed while the device was still starting up "
        f"(managed services started {int(manager_started_seconds_ago)}s ago); "
        "wait a few minutes and run doctor again",
        {
            "code": DOCTOR_CODE_DEVICE_STARTING_UP,
            "domain": "Runtime",
            "manager_started_seconds_ago": int(manager_started_seconds_ago),
            "startup_grace_seconds": grace_seconds,
            "masked_failures": [result.message for result in failures],
        },
    )
    transformed.append(startup_fail)
    return transformed, (startup_fail,)


def _startup_grace_can_mask_failure(result: CheckResult) -> bool:
    return result.details.get(STARTUP_GRACE_DETAIL_KEY) == STARTUP_GRACE_MASK


def _doctor_apply_startup_grace(
    sink: DoctorSink,
    manager_started_seconds_ago: float | None,
    *,
    enabled: bool = True,
) -> None:
    if not enabled:
        return
    transformed, synthesized_results = _apply_startup_grace(sink.results, manager_started_seconds_ago)
    if not synthesized_results:
        return
    # Replace the collected results directly: the demoted failures were already
    # streamed via on_result with their original FAIL status, so only synthesized
    # grace results are streamed here.
    sink.results[:] = transformed
    if sink.on_result is not None:
        for result in synthesized_results:
            sink.on_result(result)
    if sink.debug_fields is not None:
        if any(result.status == "FAIL" for result in synthesized_results):
            sink.debug_fields["startup_grace_applied"] = True
        else:
            sink.debug_fields["startup_grace_mixed_failures"] = True


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

    smbd_probe = _run_doctor_retryable_check(
        lambda: probe_managed_smbd_conn(target.connection),
        lambda probe: _readiness_probe_retryable(probe, TRANSIENT_SMBD_READINESS_FAILURES),
    )
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

    mdns_probe = _run_doctor_retryable_check(
        lambda: probe_managed_mdns_takeover_conn(target.connection),
        lambda probe: _readiness_probe_retryable(probe, TRANSIENT_MDNS_READINESS_FAILURES),
    )
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
        active_smb_conf_reason=smb_config.reason,
        network_plan=network_plan.plan,
        debug_fields=sink.debug_fields,
        add_result=sink.add,
    )
