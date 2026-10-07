from __future__ import annotations

import ipaddress
import re
import sys
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
    discover_printer_services_detailed,
    discover_smb_services_detailed,
    resolve_expected_smb_record,
    resolve_smb_instance,
    resolve_smb_service_target,
    select_resolved_smb_record_by_ip,
)
from timecapsulesmb.checks.doctor_debug import _add_remote_service_socket_debug
from timecapsulesmb.checks.doctor_state import (
    DirectSmbState,
    DoctorBonjourResult,
    DoctorInputs,
    DoctorOptions,
    DoctorSink,
    DoctorTarget,
    ProcessSnapshotState,
    RemoteAccess,
    RuntimeNamingState,
    SmbConfigState,
    StepDecision,
)
from timecapsulesmb.checks.local_tools import check_required_artifacts, check_required_local_tools
from timecapsulesmb.checks.models import CheckResult
from timecapsulesmb.checks.network import (
    IpNetwork,
    NetworkLinkResult,
    check_smb_port,
    check_ssh_login,
    classify_network_link,
    host_networks,
    local_interface_addresses,
    local_interface_networks,
    local_lan_networks,
    network_display,
    reportable_network,
)
from timecapsulesmb.core.net import RouteSelection, select_route_to_address
from timecapsulesmb.checks.nbns import (
    NBNS_NEGATIVE_RESPONSE_CODE,
    NBNS_OFF_SUBNET_CODE,
    NBNS_QUERY_TIMEOUT_CODE,
    apple_nbns_client_on_subnet,
    check_nbns_name_resolution,
)
from timecapsulesmb.checks.smb import (
    SmbClientTarget,
    SmbClientTargetInput,
    authenticated_smb_listing_attempts,
    authenticated_smb_listing_retryable,
    authenticated_smb_listing_with_attempts,
    check_authenticated_smb_listing,
    check_authenticated_smb_file_ops_detailed,
)
from timecapsulesmb.core.smb_config import (
    parse_active_netbios_name,
    parse_active_share_names,
    parse_xattr_tdb_paths,
)
from timecapsulesmb.checks.smb_targets import doctor_smb_servers
from timecapsulesmb.core.config import AppConfig, DEFAULT_SAMBA_AUTH_USER, validate_app_config
from timecapsulesmb.core.release import CLI_VERSION_CODE, RELEASE_TAG
from timecapsulesmb.core.net import (
    endpoint_host,
    ipv6_scope_index,
    is_link_local_ipv4,
    is_link_local_ipv6,
    resolve_host_ips,
    same_scoped_ip,
)
from timecapsulesmb.device.compat import render_compatibility_message
from timecapsulesmb.device.storage import diskd_rpc_status_conn
from timecapsulesmb.device.migration_jobs import probe_migration_activity
from timecapsulesmb.device.processes import manager_unnamed_stuck_count, stuck_processes
from timecapsulesmb.device.probe import (
    DeviceIpv4SubnetsProbeResult,
    DeviceNetworksProbeResult,
    FLASH_RUNTIME_CONFIG,
    PROCESS_SNAPSHOT_TIMEOUT_SECONDS,
    ReadinessProbeResult,
    RUNTIME_RAM_ROOT,
    RUNTIME_SMB_CONF,
    RuntimeNamingIdentityProbeResult,
    UsbPrinterProbeResult,
    flash_runtime_config_present_conn,
    limit_remote_log_tail,
    link_plan_networks,
    probe_connection_state,
    probe_device_networks_conn,
    probe_managed_mdns_conn,
    probe_managed_rsync_conn,
    probe_usb_printer_conn,
    probe_device_hostname_conn,
    probe_managed_smbd_conn,
    probe_manager_startup_age_conn,
    probe_remote_runtime_naming_identity_conn,
    read_deployed_version_conn,
    read_process_snapshot_conn,
    read_active_smb_conf_conn,
    runtime_ram_root_present_conn,
)
from timecapsulesmb.discovery.bonjour import (
    BonjourQuery,
    BonjourDiscoverySnapshot, BonjourResolvedService,
)

from timecapsulesmb.transport.local import find_free_local_port
from timecapsulesmb.transport.local import command_exists, scoped_tcp_connect_errors
from timecapsulesmb.transport.ssh import SshCommandTimeout, SshConnection, ssh_local_forward


T = TypeVar("T")


DOCTOR_TRANSIENT_RETRY_DELAYS = (10, 15)
# Apple's client gives each NetBIOS name 20 x 5 s. Registration normally takes
# ~2 s per name (~24 s through WINS), so only "still starting" waits this long.
DOCTOR_NBNS_STARTING_RETRY_DELAYS = (20, 25, 30)
NATIVE_NBNS_STILL_STARTING = "discovery native NBNS is still starting"
TRANSIENT_SMBD_READINESS_FAILURES = {
    "managed smbd parent process is not running",
    "smbd is missing an IPv4 or IPv6 wildcard TCP 445 listener",
}
TRANSIENT_MDNS_READINESS_FAILURES = {
    "discovery process is not running",
    "discovery NBNS state is not available yet",
    NATIVE_NBNS_STILL_STARTING,
    "discovery native NBNS is not ready",
}
TRANSIENT_RSYNC_READINESS_FAILURES = {
    "persistent rsync binary is missing",
    "persistent rsync config is missing",
    "managed rsync binary is missing from RAM",
    "managed rsync config is missing from RAM",
    "managed rsync process is not running",
    "managed rsync is not bound to TCP 873",
}
STARTUP_GRACE_MASK = "mask"
STARTUP_GRACE_PRESERVE = "preserve"
STARTUP_GRACE_DETAIL_KEY = "startup_grace"
DOCTOR_CODE_RUNTIME_NOT_INSTALLED = "runtime_not_installed"
# Installed (the checks above passed) but not running. On NetBSD4 the app
# offers Activate for it: stock firmware does not start Samba at boot.
DOCTOR_CODE_RUNTIME_NOT_STARTED = "runtime_not_started"
DOCTOR_CODE_DEVICE_STARTING_UP = "device_starting_up"
# A migration an interrupted deploy left running; the installed version and
# runtime are whatever that deploy had reached, so nothing after it applies.
DOCTOR_CODE_METADATA_MIGRATION_IN_PROGRESS = "metadata_migration_in_progress"
DOCTOR_CODE_PAYLOAD_MISSING_FROM_DISK = "payload_missing_from_disk"
DOCTOR_CODE_HOSTNAME_WAITING = "hostname_waiting"
DOCTOR_CODE_HOSTNAME_UNMAPPED = "hostname_unmapped"
# This computer is only on networks where the device does not share its disks
# (its WAN side in router mode, its guest network), so nothing that checks
# Bonjour or SMB from here can pass.
DOCTOR_CODE_CLIENT_ON_UNSHARED_NETWORK = "client_on_unshared_network"
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
    "smbd is missing an IPv4 or IPv6 wildcard TCP 445 listener",
    "managed smbd readiness probe timed out",
    "device Samba version unavailable (managed runtime smbd binary missing)",
    "discovery process is not running",
    "discovery NBNS state is not available yet",
    "discovery native NBNS is still starting",
    "discovery native NBNS is not ready",
    "persistent rsync binary is missing",
    "persistent rsync config is missing",
    "managed rsync binary is missing from RAM",
    "managed rsync config is missing from RAM",
    "managed rsync process is not running",
    "managed rsync is not bound to TCP 873",
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
    retry_delays: tuple[int, ...] = DOCTOR_TRANSIENT_RETRY_DELAYS,
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
        retry_delays=retry_delays,
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
        elif line.startswith("SKIP:"):
            add_result(CheckResult("SKIP", line.removeprefix("SKIP:")))
            emitted = True
        elif line.startswith("INFO:"):
            add_result(CheckResult("INFO", line.removeprefix("INFO:")))
            emitted = True

    if emitted:
        return

    if fallback_ready:
        add_result(CheckResult("PASS", fallback_pass_message))
    else:
        add_result(_startup_transient_result("FAIL", fallback_fail_message))


def _add_sshpass_result(add_result: Callable[[CheckResult], None], *, password_auth: bool) -> None:
    if command_exists("sshpass"):
        add_result(CheckResult("PASS", "found local tool sshpass"))
        return
    if password_auth:
        add_result(CheckResult("FAIL", "missing local tool sshpass; password-based SSH uploads require sshpass"))
        return
    add_result(CheckResult("INFO", "local sshpass not installed; key-authenticated SSH uploads do not require it"))


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
    advertise_afp: bool,
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
    # Read AFP from each disk's adVF bit 0x01, not an _afpovertcp browse: a
    # deploy reboot sends no goodbye, so the Mac can keep a stale AFP PTR.
    afp_mismatches: list[str] = []
    afp_checked = False
    for disk_key, fields in sorted(disk_fields.items()):
        missing_fields = [field for field in ("adVF", "adVN", "adVU") if not fields.get(field)]
        if missing_fields:
            failed = True
            add_result(CheckResult("FAIL", f"_adisk._tcp TXT disk {disk_key} is missing fields: {', '.join(missing_fields)}"))
            continue
        try:
            disk_afp = bool(int(fields["adVF"], 16) & 0x01)
        except ValueError:
            failed = True
            add_result(CheckResult("FAIL", f"_adisk._tcp TXT disk {disk_key} adVF {fields['adVF']!r} is not hexadecimal"))
        else:
            afp_checked = True
            if disk_afp != advertise_afp:
                afp_mismatches.append(f"{disk_key} adVF={fields['adVF']}")
        share_name = fields["adVN"]
        if share_name not in advertised_shares:
            advertised_shares.append(share_name)

    if afp_mismatches and not advertise_afp:
        failed = True
        add_result(
            CheckResult(
                "FAIL",
                f"_adisk._tcp TXT advertises AFP ({', '.join(afp_mismatches)}) although Advertise AFP over Bonjour is off; "
                "macOS 26.x/27 hides Time Capsules that advertise AFP; run Install / Update Samba",
            )
        )
    elif afp_mismatches:
        failed = True
        add_result(
            CheckResult(
                "FAIL",
                f"_adisk._tcp TXT advertises SMB only ({', '.join(afp_mismatches)}) although Advertise AFP over Bonjour is on; "
                "run Install / Update Samba",
            )
        )
    elif afp_checked:
        mode = "AFP and SMB" if advertise_afp else "SMB only"
        add_result(CheckResult("PASS", f"_adisk._tcp TXT advertises {mode} as configured"))

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


def _add_apple_responder_results(
    snapshot: BonjourDiscoverySnapshot,
    *,
    instance_name: str | None,
    smb_hostname: str | None,
    add_result: Callable[[CheckResult], None],
) -> bool:
    """Check the selected device's registrations, including real duplicates.

    Stock diskd accepts a shared '(2)' name after an SMB-only conflict. A suffix
    is normal; multiple names for one service on the same device are not.
    """
    if instance_name is None:
        return False
    failed = False
    if smb_hostname:
        duplicates = []
        for service in ("_smb", "_adisk"):
            names = sorted({record.name for record in snapshot.resolved
                            if _bonjour_service_label(record.service_type) == service
                            and _canonical_bonjour_host(record.hostname) == _canonical_bonjour_host(smb_hostname)})
            if len(names) > 1:
                duplicates.append(f"{service}: {', '.join(names)}")
        if duplicates:
            add_result(CheckResult("FAIL", f"duplicate Bonjour registrations for device {smb_hostname}: "
                                   + "; ".join(duplicates)))
            failed = True
        else:
            add_result(CheckResult("PASS", f"no duplicate SMB/ADisk registrations for device {smb_hostname}"))

    device_info = _bonjour_records_for_instance(snapshot.resolved, instance_name, "_device-info")
    if device_info:
        model = (device_info[0].properties.get("model") or "").strip()
        if model.startswith("TimeCapsule"):
            add_result(CheckResult("PASS", f"_device-info._tcp model is Apple's: {model}"))
        else:
            add_result(CheckResult("FAIL", f"_device-info._tcp model for {instance_name!r} is {model or 'missing'}; expected Apple's TimeCapsule model"))
            failed = True
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


def _record_ips_for_family(record: object, family: str | None) -> list[str]:
    ips = _record_ips(record)
    if family not in {"ipv4", "ipv6"}:
        return ips
    version = 4 if family == "ipv4" else 6
    return [ip for ip in ips if ipaddress.ip_address(ip.split("%", 1)[0]).version == version]


def _family_label(family: str) -> str:
    return "IPv6" if family == "ipv6" else "IPv4"


def _prefixed_check_result(result: CheckResult, prefix: str) -> CheckResult:
    if not prefix:
        return result
    return CheckResult(result.status, f"{prefix}{result.message}", result.details)


def _bonjour_family_attempts(
    target_ip: str | None,
) -> list[tuple[str, str | None, list[str] | None]]:
    target_family = None
    if target_ip:
        parsed = ipaddress.ip_address(target_ip.split("%", 1)[0])
        target_family = "ipv6" if parsed.version == 6 else "ipv4"
    return [
        (family, target_ip if target_family == family else None, None)
        for family in ("ipv4", "ipv6")
    ]


@dataclass
class _BonjourAttemptOutcome:
    results: list[CheckResult]
    instance: str | None = None
    target: BonjourServiceTarget | None = None
    service_targets: dict[str, tuple[str, ...]] | None = None
    reason: str = ""
    debug_needed: bool = False
    identity_mismatch: bool = False
    addresses: tuple[str, ...] = ()
    # Nothing on the network answered for this device: no record with its
    # instance name, host or address. Off the device's network that is
    # expected, while a record that does not match is a real problem.
    nothing_seen: bool = False


def _status_failed(results: Iterable[CheckResult]) -> bool:
    return any(result.status == "FAIL" for result in results)


def _add_bonjour_observation_conflicts(
    records: list[BonjourResolvedService], selected: BonjourResolvedService,
    add: Callable[[CheckResult], None],
) -> bool:
    """Connection preference must not hide conflicting evidence on the same link."""
    groups: dict[str, list[BonjourResolvedService]] = {}
    for record in records:
        if record.name != selected.name:
            continue
        if (selected.interface_index and record.interface_index
                and selected.interface_index != record.interface_index):
            continue
        service = _bonjour_service_label(record.service_type)
        if service in {"_smb", "_adisk", "_device-info"}:
            groups.setdefault(service, []).append(record)
    failed = False
    for service, observations in groups.items():
        targets = {(_canonical_bonjour_host(r.hostname), r.port) for r in observations}
        properties: dict[str, str] = {}
        txt_conflict = False
        for record in observations:
            for key, value in record.properties.items():
                if key in properties and properties[key] != value:
                    txt_conflict = True
                properties[key] = value
        if len(targets) > 1 or txt_conflict:
            add(CheckResult("FAIL", f"{service}._tcp has conflicting target, port or TXT observations for {selected.name!r}"))
            failed = True
    return failed


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

    reference = (select_resolved_smb_record_by_ip(smb_snapshot.resolved, bonjour_expected.target_ip)
                 if bonjour_expected.target_ip else None)
    if reference is not None and reference.interface_index:
        # The configured endpoint identifies the link, including when evaluating
        # its other address family. Same-name peers on other links are unrelated.
        scope = reference.interface_index
        smb_snapshot = BonjourDiscoverySnapshot(
            [i for i in smb_snapshot.instances if i.interface_index in (None, scope)],
            [r for r in smb_snapshot.resolved if r.interface_index in (None, scope)],
        )
    smb_instances = [instance for instance in smb_snapshot.instances if _bonjour_service_label(instance.service_type) == "_smb"]
    smb_records = [record for record in smb_snapshot.resolved if _bonjour_service_label(record.service_type) == "_smb"]
    if family is not None:
        # Addressless records still need validation/resolution. An observation of
        # only the other family must not win SMB selection. Related SRV/TXT
        # evidence remains valid even when its address lookup is incomplete.
        smb_records = [record for record in smb_records
                       if _record_ips_for_family(record, family) or not _record_ips(record)]
        smb_records.sort(key=lambda record: not bool(_record_ips_for_family(record, family)))
    if bonjour_expected.instance_name is not None:
        resolution = resolve_expected_smb_record(
            smb_instances,
            smb_records,
            expected_instance_name=bonjour_expected.instance_name,
            expected_host_label=bonjour_expected.host_label,
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

        outcome.nothing_seen = (
            resolution.record is None and resolution.source == "targeted_resolve" and not resolution.foreign
            and not (resolution.error and resolution.error.details.get("query_error"))
        )
        if resolution.error is not None:
            outcome.reason = resolution.error.message
            outcome.debug_needed = True
            add(resolution.error)
        elif resolution.record is None:
            outcome.debug_needed = True
        resolved_record = resolution.record if resolution.error is None else None
    elif target_ip is not None:
        resolved_record = select_resolved_smb_record_by_ip(smb_records, target_ip)
        if resolved_record is None:
            outcome.debug_needed = True
            outcome.nothing_seen = True
            outcome.reason = f"no resolved _smb._tcp service matched target IP {target_ip}"
            add(CheckResult("FAIL", outcome.reason))
        else:
            add(CheckResult("PASS", f"discovered _smb._tcp service matching target IP {target_ip}"))
    else:
        resolved_record = None

    if resolved_record is None:
        return outcome

    outcome.instance = resolved_record.name
    records_for_targets = list(smb_snapshot.resolved)
    records_for_targets.append(resolved_record)
    outcome.identity_mismatch = _add_bonjour_observation_conflicts(records_for_targets, resolved_record, add)
    outcome.debug_needed |= outcome.identity_mismatch
    outcome.service_targets = _bonjour_service_targets_for_instance(records_for_targets, resolved_record.name)
    if _add_bonjour_service_target_consistency_results(resolved_record.name, outcome.service_targets, add):
        outcome.debug_needed = True
    target = resolve_smb_service_target(
        resolved_record,
        expected_instance_name=resolved_record.name,
    )
    outcome.addresses = tuple(
        _record_ips_for_family(resolved_record, family)
        or [ip for ip in (resolve_host_ips(target.hostname) if target.hostname else ())
            if (":" in ip) == (family == "ipv6")]
    )
    target_result = check_smb_service_target(target)
    if target.port != 445:
        add(CheckResult("FAIL", f"_smb._tcp port is {target.port}, expected 445"))
        outcome.identity_mismatch = True
    if target_result.status == "FAIL":
        outcome.debug_needed = True
    add(target_result)
    if target.hostname:
        outcome.target = target
        if _add_bonjour_target_host_label_result("_smb", target.hostname, add):
            outcome.debug_needed = True
            outcome.identity_mismatch = True
        if _add_expected_bonjour_host_label_result(target, bonjour_expected.host_label, add):
            outcome.debug_needed = True
            outcome.identity_mismatch = True
        host_ip_result = _add_bonjour_host_ip_results(
            target.hostname,
            expected_ip=target_ip,
            record_ips=_record_ips(resolved_record),
            add_result=add,
        )
        if host_ip_result.status == "FAIL":
            outcome.debug_needed = True
            unverified = bool((host_ip_result.details or {}).get("address_unverified"))
            outcome.identity_mismatch = outcome.identity_mismatch or not unverified
    if _add_time_machine_adisk_results(
        records_for_targets,
        instance_name=resolved_record.name,
        smb_hostname=target.hostname,
        active_share_names=active_share_names,
        advertise_afp=bonjour_expected.advertise_afp,
        add_result=add,
    ):
        outcome.debug_needed = True
    if _add_apple_responder_results(
        smb_snapshot,
        instance_name=resolved_record.name,
        smb_hostname=target.hostname,
        add_result=add,
    ):
        outcome.debug_needed = True
    return outcome


def _evaluate_bonjour_attempt(
    smb_snapshot: BonjourDiscoverySnapshot | None,
    discovery_error: CheckResult | None,
    bonjour_expected: BonjourExpectedIdentity,
    *,
    target_ip: str | None,
    family: str | None,
    interfaces: list[str] | None,
    active_share_names: list[str],
    resolver: Callable[..., tuple[BonjourResolvedService | None, CheckResult | None]] | None = None,
) -> _BonjourAttemptOutcome:
    if discovery_error is not None:
        return _BonjourAttemptOutcome(
            results=[discovery_error],
            reason=discovery_error.message,
            debug_needed=True,
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
        resolver=resolver or resolve_smb_instance,
        browse_miss_message=(
            f"Bonjour browse did not observe expected _smb._tcp instance {expected_name!r}; "
            "targeted resolve succeeded"
        ),
        targeted_resolve_pass_message=f"resolved expected _smb._tcp instance {expected_name!r} by targeted query",
    )


def _add_bonjour_results(
    config: AppConfig,
    runtime_naming_identity: RuntimeNamingIdentityProbeResult | None,
    *,
    skip_bonjour: bool,
    active_share_names: list[str] | None = None,
    add_result: Callable[[CheckResult], None],
    network: DoctorNetworkProbe | None = None,
) -> DoctorBonjourResult:
    bonjour_instance: str | None = None
    bonjour_target: BonjourServiceTarget | None = None
    bonjour_reason = "Bonjour check not run"
    bonjour_debug_needed = False
    bonjour_expected_debug: dict[str, str | None] | None = None
    bonjour_discovery_debug: object | None = None
    bonjour_service_targets: dict[str, tuple[str, ...]] = {}
    bonjour_addresses: list[str] = []
    active_share_names = active_share_names or []

    if not skip_bonjour:
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
                    discovery_debug=None,
                )
            query = BonjourQuery()
            smb_snapshot, discovery_error, bonjour_discovery_debug = discover_smb_services_detailed(
                include_related=True, target_ip=bonjour_expected.target_ip, query=query,
            )
            attempts = _bonjour_family_attempts(bonjour_expected.target_ip)
            outcomes: list[tuple[str, _BonjourAttemptOutcome]] = []
            bonjour_reason = ""
            for family, target_ip, interfaces in attempts:
                chosen_outcome = _evaluate_bonjour_attempt(
                    smb_snapshot, discovery_error, bonjour_expected,
                    target_ip=target_ip, family=family, interfaces=interfaces,
                    active_share_names=active_share_names,
                    resolver=lambda instance, **kwargs: resolve_smb_instance(instance, query=query, **kwargs),
                )
                outcomes.append((family, chosen_outcome))
                if chosen_outcome.reason:
                    bonjour_reason = chosen_outcome.reason
                bonjour_debug_needed = bonjour_debug_needed or chosen_outcome.debug_needed
                if chosen_outcome.instance is not None:
                    bonjour_instance = chosen_outcome.instance
                if chosen_outcome.target is not None:
                    bonjour_target = chosen_outcome.target
                if chosen_outcome.service_targets:
                    bonjour_service_targets = chosen_outcome.service_targets
                for address in chosen_outcome.addresses:
                    if not any(address == known or same_scoped_ip(address, known) for known in bonjour_addresses):
                        bonjour_addresses.append(address)

            # A family with nothing to look up (no IPv6 target without an
            # instance name) reports nothing and has no say.
            checked = [outcome for _, outcome in outcomes if outcome.results]
            if network is not None and checked and all(outcome.nothing_seen for outcome in checked):
                link = network.link()
                if link.verdict == "separate":
                    network.record_skip("bonjour")
                    bonjour_reason = _off_link_message(
                        "Bonjour check skipped",
                        link,
                        "Bonjour only reaches devices on the same network, so SMB is checked by address instead",
                    )
                    add_result(CheckResult("SKIP", bonjour_reason, {"code": BONJOUR_OFF_LINK_CODE}))
                    # As if Bonjour were skipped: later steps use TC_HOST's addresses.
                    return DoctorBonjourResult(
                        instance=None,
                        target=None,
                        service_targets={},
                        reason=bonjour_reason,
                        debug_needed=False,
                        expected_debug=bonjour_expected_debug,
                        discovery_debug=None,
                    )
            usable_family = any(not _status_failed(outcome.results) and outcome.addresses for _, outcome in outcomes)
            for family, outcome in outcomes:
                prefix = f"Bonjour {_family_label(family)}: "
                missing_family = usable_family and not outcome.addresses and not outcome.identity_mismatch
                if missing_family:
                    detail = outcome.reason or "no usable matching _smb._tcp address was discovered"
                    add_result(CheckResult("INFO", f"{prefix}{detail}; another address family was discovered"))
                    continue
                for result in outcome.results:
                    add_result(_prefixed_check_result(result, prefix))
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
        discovery_debug=bonjour_discovery_debug,
        addresses=tuple(bonjour_addresses),
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


BONJOUR_OFF_LINK_CODE = "bonjour_off_link"


class DoctorNetworkProbe:
    """The device's networks, and whether this computer is on one, read once per run.

    NBNS needs the device's IPv4 subnets. Bonjour and the USB printer check
    need to know whether this computer shares any network with the device at
    all, since multicast DNS does not cross routers. One ifconfig serves both.
    """

    def __init__(self, target: DoctorTarget, remote: RemoteAccess, debug_fields: dict[str, object] | None) -> None:
        self._target = target
        self._remote = remote
        self._debug_fields = debug_fields
        self._device: DeviceNetworksProbeResult | None = None
        self._link: NetworkLinkResult | None = None
        self._skipped: list[str] = []

    def device(self) -> DeviceNetworksProbeResult:
        if self._device is None:
            if not self._remote.remote_checks_enabled:
                self._device = DeviceNetworksProbeResult(error="SSH checks were not run")
            else:
                try:
                    self._device = probe_device_networks_conn(self._target.connection)
                except Exception as e:
                    self._device = DeviceNetworksProbeResult(error=f"{type(e).__name__}: {e}")
        return self._device

    def ipv4_subnets(self) -> DeviceIpv4SubnetsProbeResult:
        return self.device().ipv4_subnets

    def link(self) -> NetworkLinkResult:
        if self._link is None:
            self._link = self._classify()
            self._record()
        return self._link

    def record_skip(self, check: str) -> None:
        self._skipped.append(check)
        self._record()

    def _record(self) -> None:
        if self._debug_fields is not None and self._link is not None:
            self._debug_fields["bonjour_link"] = {**self._link.telemetry(), "skipped": list(self._skipped)}

    def _classify(self) -> NetworkLinkResult:
        local = [item.network for item in local_interface_networks()]
        device = self.device()
        if device.error is None and device.networks:
            return classify_network_link(
                (ipaddress.ip_network(network) for network in device.networks), local, source="device_ifconfig",
            )
        # Without the device's own list, its address can still show that it
        # is on another network than this computer.
        return classify_network_link(host_networks(self._target.host), local, source="device_address", detail=device.error)


def _off_link_message(prefix: str, link: NetworkLinkResult, consequence: str) -> str:
    local, device = link.compared()
    noun = "network" if len(device) == 1 else "networks"
    return (
        f"{prefix}; this computer ({', '.join(map(network_display, local))}) is not on the device's {noun} "
        f"({', '.join(map(network_display, device))}). {consequence}"
    )


_UNSHARED_NETWORK_PLACES = {
    "wan": "the device's internet (WAN) side",
    "guest": "the device's guest network",
}


def client_on_unshared_network_message(
    client_networks: Iterable[IpNetwork],
    roles: Iterable[str],
    shared_networks: Iterable[IpNetwork],
    *,
    platform: str | None = None,
) -> str:
    computer = "this Mac" if (platform or sys.platform) == "darwin" else "this computer"
    distinct_roles = set(roles)
    place = _UNSHARED_NETWORK_PLACES.get(next(iter(distinct_roles))) if len(distinct_roles) == 1 else None
    # The message reaches telemetry in doctor's error, so public networks are
    # shown only by family and prefix length.
    client = ", ".join(reportable_network(network, hide_hosts=True) for network in client_networks)
    shared = ", ".join(reportable_network(network) for network in shared_networks)
    return (
        f"{computer} is on {place or 'a device network'} ({client}), "
        f"where the device does not share its disks; join the device's main network ({shared}) "
        f"by Wi-Fi or one of its LAN ports, then run doctor again. Bonjour and SMB were not checked from {computer}"
    )


def _doctor_check_unshared_network(
    remote: RemoteAccess,
    link_plan: dict[str, object] | None,
    sink: DoctorSink,
) -> bool:
    """FAIL when this computer is only on networks where the device does not
    share its disks, and return whether it did.

    The device's own link plan says which of its networks it shares disks on
    (the runtime applies Apple's rules: in router mode the WAN side and the
    guest network only with "Share disks over WAN"). Bonjour, SMB and NBNS
    from such a network fail without saying why, so the caller skips them.
    """
    if not remote.remote_checks_enabled:
        return False
    shared, unshared = link_plan_networks(link_plan)
    if not unshared:
        return False
    local = [item.network for item in local_lan_networks()]
    if any(network.overlaps(item) for network in shared for item in local):
        return False
    matched = [(role, network) for role, network in unshared if any(network.overlaps(item) for item in local)]
    if not matched:
        return False
    client = [item for item in local if any(item.overlaps(network) for _, network in matched)]
    sink.add(CheckResult(
        "FAIL",
        client_on_unshared_network_message(client, (role for role, _ in matched), shared),
        {
            "code": DOCTOR_CODE_CLIENT_ON_UNSHARED_NETWORK,
            "client_networks": [reportable_network(network, hide_hosts=True) for network in client],
            "unshared_networks": [{"role": role, "network": reportable_network(network)} for role, network in matched],
            "shared_networks": [reportable_network(network) for network in shared],
        },
    ))
    return True


def _add_nbns_results(
    *,
    active_smb_conf: str | None,
    runtime_naming_identity: RuntimeNamingIdentityProbeResult | None,
    reachable_addresses: tuple[str, ...],
    add_result: Callable[[CheckResult], None],
    native_nbns_ready: bool | None = None,
    route_sources: tuple[tuple[str, str], ...] = (),
    probe_device_subnets: Callable[[], DeviceIpv4SubnetsProbeResult] | None = None,
    debug_fields: dict[str, object] | None = None,
) -> None:
    try:
        expected_name = parse_active_netbios_name(active_smb_conf or "")
        if expected_name is None and runtime_naming_identity is not None:
            expected_name = runtime_naming_identity.netbios_name
        if expected_name is None:
            add_result(CheckResult("SKIP", "NBNS check skipped; active/probed NetBIOS name unavailable"))
            return
        ipv4_addresses = [address for address in reachable_addresses if _smb_target_family(address) == "ipv4"]
        target_ip = next((address for address in ipv4_addresses if not is_link_local_ipv4(address)), None)
        expected_ip = target_ip
        if target_ip is None and ipv4_addresses:
            # The device answers with the address it registered, its LAN one
            # when it has one, even when only its 169.254 address is reachable.
            target_ip = ipv4_addresses[0]
        if target_ip is None:
            add_result(CheckResult("SKIP", "NBNS check skipped; no TCP-reachable IPv4 SMB address was discovered"))
            return
        def query() -> CheckResult:
            return check_nbns_name_resolution(expected_name, target_ip, expected_ip)

        if native_nbns_ready is False:
            # The device has not finished registering its name yet: a
            # timeout may clear, and during startup grace it is a startup
            # failure. Once native NBNS is ready a timeout is a network
            # problem between here and the device that waiting cannot fix.
            result = _run_doctor_retryable_check(query, _nbns_query_timed_out)
            if _nbns_query_timed_out(result):
                result = _with_startup_grace_policy(result, STARTUP_GRACE_MASK)
        else:
            result = query()
        if _nbns_off_subnet_symptom(result) is not None and probe_device_subnets is not None:
            result = _nbns_off_subnet_result(
                result,
                expected_name,
                dict(route_sources).get(target_ip),
                probe_device_subnets,
                debug_fields,
            )
        add_result(result)
    except Exception as e:
        add_result(CheckResult("WARN", f"NBNS check skipped: {e}"))


def _nbns_query_timed_out(result: CheckResult) -> bool:
    return result.status == "FAIL" and result.details.get("code") == NBNS_QUERY_TIMEOUT_CODE


def _nbns_off_subnet_symptom(result: CheckResult) -> str | None:
    """How an NBNS failure looks when the client is off the device's subnets."""
    if result.status != "FAIL":
        return None
    code = result.details.get("code")
    if code == NBNS_QUERY_TIMEOUT_CODE:
        return "timeout"
    if code == NBNS_NEGATIVE_RESPONSE_CODE:
        return "negative_response"
    return None


def _nbns_off_subnet_result(
    result: CheckResult,
    netbios_name: str,
    client_source: str | None,
    probe_device_subnets: Callable[[], DeviceIpv4SubnetsProbeResult],
    debug_fields: dict[str, object] | None,
) -> CheckResult:
    # Apple's wcifsnd may answer a client off all of its subnets from UDP 922
    # rather than 137 (see apple_nbns_client_on_subnet), and routers between
    # subnets differ in whether that answer arrives. Its default context for
    # such a client holds none of the device's names either, so an answer that
    # does arrive can be negative. Neither says anything about the device, so
    # both are skipped, not failed.
    symptom = _nbns_off_subnet_symptom(result)
    # The device's subnets come from live ifconfig and the client is this
    # host's route source, so the verdict can differ from wcifsnd's when a
    # router translates addresses between the subnets, or briefly after a
    # renumber (wcifsnd keeps the old address in its table).
    device_subnets: list[str] = []
    try:
        client = str(ipaddress.IPv4Address(client_source)) if client_source is not None else None
    except ValueError:
        client = None
    if client is None:
        outcome, detail = "unknown", "no IPv4 source address for the NBNS target"
    else:
        try:
            probe = probe_device_subnets()
        except Exception as e:
            probe = DeviceIpv4SubnetsProbeResult(error=f"{type(e).__name__}: {e}")
        device_subnets = list(dict.fromkeys(entry.network for entry in probe.entries))
        if probe.error is not None:
            outcome, detail = "unknown", probe.error
        elif apple_nbns_client_on_subnet(probe.entries, client):
            outcome, detail = "on_subnet", None
        else:
            outcome, detail = "off_subnet", None
    if debug_fields is not None:
        debug_fields["nbns_subnet"] = {
            "client_source": client,
            "device_subnets": device_subnets,
            "result": symptom,
            "outcome": outcome,
            "detail": detail,
        }
    if outcome != "off_subnet":
        return result
    noun = "subnet" if len(device_subnets) == 1 else "subnets"
    answer = "got no answer" if symptom == "timeout" else f"was refused (rcode {result.details.get('rcode')})"
    return CheckResult(
        "SKIP",
        f"NBNS query for {netbios_name!r} {answer}; this computer ({client}) is outside the device's {noun} {', '.join(device_subnets)}",
        {"code": NBNS_OFF_SUBNET_CODE, "client_source": client, "device_subnets": device_subnets, "result": symptom},
    )


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
    reachable_addresses: tuple[str, ...],
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
    ordered_addresses = sorted(
        reachable_addresses,
        key=lambda address: (is_link_local_ipv4(address), reachable_addresses.index(address)),
    )
    for remote_address in ordered_addresses:
        if _ip_literal(remote_address) is None:
            add(pinned_server or remote_address)
        else:
            add(SmbClientTarget(server=pinned_server or remote_address, ip_address=remote_address))
    return targets


def _smb_target_family(target: SmbClientTargetInput) -> str | None:
    value = target.ip_address if isinstance(target, SmbClientTarget) else target
    ip = _ip_literal(value)
    if ip is None:
        return None
    return "ipv6" if ":" in ip else "ipv4"


def _authenticated_smb_target_groups(
    targets: list[SmbClientTargetInput],
    reachable_addresses: tuple[str, ...],
) -> list[tuple[str | None, list[SmbClientTargetInput]]]:
    if not reachable_addresses:
        return [(None, targets)]

    groups: list[tuple[str, list[SmbClientTargetInput]]] = []
    for family in ("ipv4", "ipv6"):
        family_targets = [
            target
            for target in targets
            if _smb_target_family(target) == family
            and isinstance(target, SmbClientTarget)
            and target.ip_address is not None
            and any(same_scoped_ip(target.ip_address, address) for address in reachable_addresses)
        ]
        if family_targets:
            groups.append((family, family_targets))
    return groups or [(None, targets)]


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
    smb_password: str,
    active_share_names: list[str],
    active_smb_conf_reason: str,
    remote_port: int,
    debug_prefix: str,
    debug_fields: dict[str, object] | None,
    add_result: Callable[[CheckResult], None],
    retry_delays: tuple[int, ...] = DOCTOR_TRANSIENT_RETRY_DELAYS,
) -> bool:
    local_port = find_free_local_port()
    if debug_fields is not None:
        debug_fields[f"{debug_prefix}_listing_servers"] = ["127.0.0.1"]
        debug_fields[f"{debug_prefix}_listing_active_shares"] = active_share_names
    try:
        with ssh_local_forward(
            connection,
            local_port=local_port,
            # The device resolves the forward's host: its own loopback, which
            # smbd's wildcard listener serves, works whatever address we used.
            remote_host="127.0.0.1",
            remote_port=remote_port,
        ):
            listing_result = _authenticated_smb_listing_with_doctor_retries(
                DEFAULT_SAMBA_AUTH_USER,
                smb_password,
                "127.0.0.1",
                port=local_port,
                retry_delays=retry_delays,
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
    active_smb_conf: str | None,
    active_smb_conf_reason: str,
    direct_smb: DirectSmbState,
    debug_fields: dict[str, object] | None,
    add_result: Callable[[CheckResult], None],
    retry_delays: tuple[int, ...] = DOCTOR_TRANSIENT_RETRY_DELAYS,
) -> None:
    active_share_names = parse_active_share_names(active_smb_conf or "")
    smb_servers = _doctor_smb_client_targets(
        config,
        bonjour_target,
        runtime_naming_identity,
        direct_smb.reachable_addresses,
    )
    if not smb_servers:
        add_result(CheckResult("SKIP", "authenticated SMB checks skipped; no TCP-reachable SMB endpoint"))
        return
    target_groups = _authenticated_smb_target_groups(smb_servers, direct_smb.reachable_addresses)
    checked_servers = [target for _, targets in target_groups for target in targets]
    if debug_fields is not None:
        debug_fields["authenticated_smb_listing_servers"] = [_smb_client_target_debug(target) for target in checked_servers]
        debug_fields["authenticated_smb_listing_active_shares"] = active_share_names
    listing_outcomes = [
        (
            family,
            _authenticated_smb_listing_with_doctor_retries(
                DEFAULT_SAMBA_AUTH_USER,
                smb_password,
                targets,
                port=445,
                retry_delays=retry_delays,
            ),
        )
        for family, targets in target_groups
    ]
    all_attempts = [
        attempt
        for _, result in listing_outcomes
        for attempt in authenticated_smb_listing_attempts(result)
    ]
    if debug_fields is not None and all_attempts:
        debug_fields["authenticated_smb_listing_attempts"] = all_attempts
    successful_outcomes = [(family, result) for family, result in listing_outcomes if result.status == "PASS"]
    if len(listing_outcomes) > 1 and successful_outcomes:
        for family, result in listing_outcomes:
            if result.status == "PASS":
                add_result(result)
            else:
                add_result(
                    CheckResult(
                        "WARN",
                        f"authenticated SMB {_family_label(family or '')} listing failed while another network family works: "
                        f"{result.message}",
                        result.details,
                    )
                )
        listing_result = successful_outcomes[0][1]
    elif len(listing_outcomes) > 1:
        status = "FAIL" if any(result.status == "FAIL" for _, result in listing_outcomes) else "WARN"
        listing_result = CheckResult(
            status,
            "authenticated SMB listing failed for all applicable network families",
            {"attempts": all_attempts},
        )
    else:
        listing_result = listing_outcomes[0][1]
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
                smb_password=smb_password,
                active_share_names=active_share_names,
                active_smb_conf_reason=active_smb_conf_reason,
                remote_port=445,
                debug_prefix="authenticated_smb_tunnel",
                debug_fields=debug_fields,
                add_result=add_result,
                retry_delays=retry_delays,
            ):
                return
        add_result(_tag_smb_startup_transient_if_connection_shaped(listing_result))
        return
    if len(listing_outcomes) == 1:
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


def _doctor_check_stuck_processes(target: DoctorTarget, remote: RemoteAccess, sink: DoctorSink) -> ProcessSnapshotState:
    """List the device's processes before any check touches the data disk."""
    if not remote.remote_checks_enabled:
        return ProcessSnapshotState()
    try:
        snapshot = read_process_snapshot_conn(target.connection)
    except SshCommandTimeout as e:
        if sink.debug_fields is not None:
            sink.debug_fields["remote_process_snapshot_error"] = f"{type(e).__name__}: {e}"
        sink.add(
            CheckResult(
                "WARN",
                f"listing the device's processes timed out after {PROCESS_SNAPSHOT_TIMEOUT_SECONDS}s although "
                "SSH login worked; the device may be running out of processes, so doctor tries SMB only once "
                "and does not read logs from the data disk",
                {"domain": "Runtime"},
            )
        )
        return ProcessSnapshotState(timed_out=True)
    except Exception as e:
        if sink.debug_fields is not None:
            sink.debug_fields["remote_process_snapshot_error"] = f"{type(e).__name__}: {e}"
        return ProcessSnapshotState()
    if sink.debug_fields is not None:
        sink.debug_fields["remote_process_snapshot"] = limit_remote_log_tail(snapshot.rstrip() or "(empty)")
    stuck = tuple(stuck_processes(snapshot))
    # The manager's title names the four longest and counts the rest.
    unnamed = manager_unnamed_stuck_count(snapshot)
    if stuck:
        details: dict[str, object] = {
            "domain": "Runtime",
            "stuck_processes": [
                {"pid": p.pid, "name": p.name, "wchan": p.wchan, "sleep_seconds": p.sleep_seconds}
                for p in stuck
            ],
        }
        more = ""
        if unnamed:
            details["stuck_processes_unnamed"] = unnamed
            more = f"; and {unnamed} more the device's manager counts but does not name"
        sink.add(
            CheckResult(
                "FAIL",
                "device processes are blocked in the kernel without making progress: "
                + "; ".join(process.describe() for process in stuck)
                + more
                + ". A disk operation is not completing (a failing disk or a kernel I/O stall); "
                "if this does not clear, power-cycle the device",
                details,
            )
        )
    return ProcessSnapshotState(stuck=stuck)


def _doctor_check_running_migration(target: DoctorTarget, remote: RemoteAccess, sink: DoctorSink) -> StepDecision:
    if not remote.remote_checks_enabled:
        return StepDecision()
    try:
        activity = probe_migration_activity(target.connection)
    except Exception:
        # The checks after this one still diagnose the device.
        return StepDecision()
    if not activity.migrations:
        return StepDecision()
    phase = activity.phase or "unknown"
    progress = activity.progress
    # NetBSD 4's ps cannot read the migrator's arguments, so its phase may be
    # unknown; say only what is known.
    known = [f"{phase} phase"] if phase != "unknown" else []
    if progress.entries is not None:
        known.append(f"{progress.entries} files checked")
    position = f" ({', '.join(known)})" if known else ""
    if sink.debug_fields is not None:
        sink.debug_fields["running_migration_pids"] = [migration.pid for migration in activity.migrations]
    sink.add(
        CheckResult(
            "FAIL",
            f"a metadata migration is still running{position}; "
            "run \"Install / Update Samba\" in the macOS app, or tcapsule deploy from the command line: "
            "it waits for the migration to finish",
            details={
                "code": DOCTOR_CODE_METADATA_MIGRATION_IN_PROGRESS,
                "phase": phase,
                "entries": progress.entries,
            },
        )
    )
    return StepDecision(stop=True)


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
                "installed Samba configuration not found; run \"Install / Update Samba\" in the macOS app, "
                "or run tcapsule deploy from the command line",
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
                f"installed Samba payload has no version metadata; current version is {RELEASE_TAG}; "
                "run \"Install / Update Samba\" in the macOS app, or run tcapsule deploy from the command line",
            )
        )
        return StepDecision(stop=True)

    if deployed_cli_version_code < CLI_VERSION_CODE:
        sink.add(
            CheckResult(
                "FAIL",
                f"installed Samba version {deployed_release_tag} is older than current {RELEASE_TAG}; "
                "run \"Install / Update Samba\" in the macOS app, or run tcapsule deploy from the command line",
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
                details={"code": DOCTOR_CODE_RUNTIME_NOT_STARTED},
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
            "started_monotonic_s": probe.started_monotonic_s,
            "now_monotonic_ms": probe.now_monotonic_ms,
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
    # Waiting cannot move this computer onto another network, so that failure
    # neither blocks the collapse nor gets a "may resolve" note.
    failures = [
        result for result in results
        if result.status == "FAIL" and result.details.get("code") != DOCTOR_CODE_CLIENT_ON_UNSHARED_NETWORK
    ]
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
    masked = {id(result) for result in failures}
    transformed: list[CheckResult] = []
    for result in results:
        if id(result) in masked:
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
                "system_dns_name": identity.system_dns_name,
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
        if sink.debug_fields is not None and probe_result.ssh_authenticated:
            sink.debug_fields["airport_mac"] = probe_result.airport_mac
        if compatibility is None:
            sink.add(CheckResult("FAIL", probe_result.error or "could not determine device compatibility"))
        elif compatibility.supported:
            sink.add(CheckResult("PASS", render_compatibility_message(compatibility)))
            _add_sshpass_result(sink.add, password_auth=bool(target.connection.password))
        else:
            sink.add(CheckResult("FAIL", render_compatibility_message(compatibility)))
    except Exception as e:
        sink.add(CheckResult("FAIL", f"device compatibility check failed: {e}"))


def _doctor_check_device_hostname(target: DoctorTarget, remote: RemoteAccess, sink: DoctorSink) -> None:
    """Samba resolves the device hostname at every login (issue #54).

    The manager starts Samba only once ACPd has set the hostname, and staging
    maps it in /etc/hosts. Runs before the Samba checks, so a stuck hostname is
    reported ahead of the Samba failures it causes.
    """
    if not remote.remote_checks_enabled:
        return
    probe = probe_device_hostname_conn(target.connection)
    if probe.error:
        sink.add(CheckResult("FAIL", f"could not read the device hostname: {probe.error}"))
        return
    if probe.manager_waiting or not probe.hostname:
        sink.add(_startup_transient_result(
            "FAIL",
            "Samba is waiting for the device hostname (ACPd has not set it); "
            "Samba cannot start or restage until it is set, and the Samba and "
            "Time Machine checks below may fail because of it",
            {"code": DOCTOR_CODE_HOSTNAME_WAITING, "hostname": probe.hostname},
        ))
    elif not probe.mapped:
        sink.add(_startup_transient_result(
            "FAIL",
            f"device hostname {probe.hostname} is not mapped in /etc/hosts; "
            "Samba logins stall until it is (issue #54)",
            {"code": DOCTOR_CODE_HOSTNAME_UNMAPPED, "hostname": probe.hostname},
        ))
    else:
        sink.add(CheckResult("PASS", f"device hostname {probe.hostname} is mapped in /etc/hosts"))
    if probe.boot_wait_ms:
        sink.add(CheckResult(
            "INFO",
            f"the manager waited {probe.boot_wait_ms} ms for the device hostname at boot",
            {"boot_wait_ms": probe.boot_wait_ms},
        ))
    if probe.stale_names:
        sink.add(CheckResult(
            "INFO",
            f"/etc/hosts still maps an earlier hostname: {', '.join(probe.stale_names)}",
            {"stale_names": list(probe.stale_names)},
        ))


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


def _doctor_check_managed_mdns(
    target: DoctorTarget, remote: RemoteAccess, sink: DoctorSink,
) -> tuple[bool | None, dict[str, object] | None]:
    """Return whether native NBNS passed (None when it was not probed), and
    the device's link plan when the probe read one."""
    if not remote.remote_checks_enabled:
        return None, None

    retries = 0

    def should_retry(probe: ReadinessProbeResult) -> bool:
        nonlocal retries
        retries += 1
        if retries <= len(DOCTOR_TRANSIENT_RETRY_DELAYS):
            return _readiness_probe_retryable(probe, TRANSIENT_MDNS_READINESS_FAILURES)
        return _readiness_probe_retryable(probe, {NATIVE_NBNS_STILL_STARTING})

    mdns_probe = _run_doctor_retryable_check(
        lambda: probe_managed_mdns_conn(target.connection),
        should_retry,
        retry_delays=DOCTOR_TRANSIENT_RETRY_DELAYS + DOCTOR_NBNS_STARTING_RETRY_DELAYS,
    )
    mdns_probe_lines = getattr(mdns_probe, "lines", ())
    if not isinstance(mdns_probe_lines, (list, tuple)):
        mdns_probe_lines = ()
    _add_probe_line_results(
        sink.add,
        mdns_probe_lines,
        fallback_ready=mdns_probe.ready,
        fallback_pass_message="managed mDNS registrant is active",
        fallback_fail_message=f"managed mDNS registrant is not active ({mdns_probe.detail})",
    )
    steps = getattr(mdns_probe, "steps", ())
    nbns = next((step for step in steps if getattr(step, "id", None) == "native_nbns"), None) if isinstance(steps, (list, tuple)) else None
    link_plan = getattr(mdns_probe, "link_plan", None)
    return (None if nbns is None else nbns.status == "pass"), (link_plan if isinstance(link_plan, dict) else None)


def _add_usb_printer_results(
    printer: UsbPrinterProbeResult,
    snapshot: BonjourDiscoverySnapshot | None,
    discovery_error: CheckResult | None,
    *,
    host_label: str | None,
    add_result: Callable[[CheckResult], None],
    network: DoctorNetworkProbe | None = None,
) -> None:
    """Guide G6 as a release gate: with a USB printer plugged in, Apple's printd
    must still advertise it through mDNSResponder under our runtime (we never
    touch printd; v3.0 re-advertised printers itself because it killed the
    responder). Skipped, not passed, when no printer is attached, or when this
    computer is not on the device's network and no record names the printer."""
    if printer.error:
        add_result(CheckResult("SKIP", f"USB printer check skipped; could not read the printer list ({printer.error})"))
        return
    if not printer.present:
        add_result(CheckResult("SKIP", "no USB printer is plugged in (acp prni); printer sharing not checked"))
        return
    label = f"{printer.name!r}"
    if discovery_error is not None or snapshot is None:
        add_result(CheckResult("FAIL", f"USB printer {label} is plugged in but the Bonjour printer browse failed: {discovery_error.message if discovery_error else 'no result'}"))
        return
    matches: list[str] = []
    others: list[str] = []
    for record in snapshot.resolved:
        service = _bonjour_service_label(record.service_type)
        record_label = f"{record.name} ({service}, {record.hostname})"
        name_matches = record.name.strip().lower() == (printer.name or "").strip().lower()
        record_host = _bonjour_host_label(record.hostname)
        host_matches = host_label is not None and record_host is not None and record_host.lower() == host_label.lower()
        (matches if name_matches or host_matches else others).append(record_label)
    if not matches:
        # Instances that were seen but not resolved still count as a browse hit.
        for instance in snapshot.instances:
            if instance.name.strip().lower() == (printer.name or "").strip().lower():
                matches.append(f"{instance.name} ({_bonjour_service_label(instance.service_type)}, unresolved)")
    if matches:
        add_result(CheckResult("PASS", f"USB printer {label} is advertised by Apple's printd: {', '.join(sorted(matches))}"))
    elif network is not None and (link := network.link()).verdict == "separate":
        network.record_skip("usb_printer")
        add_result(CheckResult("SKIP", _off_link_message(
            f"USB printer {label} not checked",
            link,
            "Bonjour only reaches devices on the same network",
        ), {"code": BONJOUR_OFF_LINK_CODE}))
    else:
        seen = f"; other printer records seen: {', '.join(sorted(others))}" if others else "; no printer records seen"
        add_result(
            CheckResult(
                "FAIL",
                f"USB printer {label} is plugged in but no _pdl-datastream/_riousbprint/_printer/_ipp record names it or this "
                f"device{seen} (Apple's printd is not advertising it; compare with stock firmware before blaming the runtime)",
            )
        )


def _doctor_check_usb_printer(
    target: DoctorTarget,
    remote: RemoteAccess,
    bonjour_result: DoctorBonjourResult,
    sink: DoctorSink,
    network: DoctorNetworkProbe | None = None,
) -> None:
    if not remote.remote_checks_enabled:
        return
    printer = probe_usb_printer_conn(target.connection)
    snapshot = None
    discovery_error = None
    if printer.present and not printer.error:
        snapshot, discovery_error = discover_printer_services_detailed()
    host_label = None
    if bonjour_result.target is not None and bonjour_result.target.hostname:
        host_label = _bonjour_host_label(bonjour_result.target.hostname)
    _add_usb_printer_results(
        printer, snapshot, discovery_error, host_label=host_label, add_result=sink.add, network=network,
    )


def _doctor_check_managed_rsync(target: DoctorTarget, remote: RemoteAccess, sink: DoctorSink) -> None:
    if not remote.remote_checks_enabled:
        return

    rsync_probe = _run_doctor_retryable_check(
        lambda: probe_managed_rsync_conn(target.connection),
        lambda probe: _readiness_probe_retryable(probe, TRANSIENT_RSYNC_READINESS_FAILURES),
    )
    rsync_probe_lines = getattr(rsync_probe, "lines", ())
    if not isinstance(rsync_probe_lines, (list, tuple)):
        rsync_probe_lines = ()
    _add_probe_line_results(
        sink.add,
        rsync_probe_lines,
        fallback_ready=rsync_probe.ready,
        fallback_pass_message="managed rsync state matches runtime configuration",
        fallback_fail_message=f"managed rsync is not ready ({rsync_probe.detail})",
    )


def _doctor_check_diskd_rpc(target: DoctorTarget, remote: RemoteAccess, sink: DoctorSink) -> None:
    """Information only: whether ACPd still routes diskd's RPCs.

    A second diskd deletes the runtime diskd's RPC names in ACPd (boot.sh's
    diskd guard stops ACPd from starting one). Without diskd.useVolume, disks
    are not activated until the device restarts.
    """
    if not remote.remote_checks_enabled:
        return
    try:
        status = diskd_rpc_status_conn(target.connection)
    except Exception as e:
        sink.add(CheckResult("INFO", f"diskd RPC check unavailable: {e}"))
        return
    if status == "answered":
        sink.add(CheckResult("INFO", "diskd RPC: getVolumeCounts answered"))
    elif status == "-6727":
        sink.add(CheckResult("INFO", "diskd RPC: getVolumeCounts failed: -6727 "
                                     "(ACPd lost diskd's RPC names; restarting the device restores them)"))
    else:
        sink.add(CheckResult("INFO", f"diskd RPC: getVolumeCounts failed: {status}"))


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


def _dedupe_addresses(addresses: Iterable[str]) -> list[str]:
    unique: list[str] = []
    for address in addresses:
        if address and not any(address == known or same_scoped_ip(address, known) for known in unique):
            unique.append(address)
    return unique


def _link_local_scope_candidates(address: str, local_addresses: tuple[str, ...]) -> list[str]:
    base, separator, _scope = address.partition("%")
    if separator:
        return [address]
    candidates: list[str] = []
    indexes: set[int] = set()
    for local in local_addresses:
        if not is_link_local_ipv6(local) or "%" not in local:
            continue
        local_scope = local.partition("%")[2]
        index = ipv6_scope_index(local_scope)
        if index is None or index in indexes:
            continue
        indexes.add(index)
        candidates.append(f"{base}%{local_scope}")
    return candidates


def _fallback_smb_addresses(host: str) -> tuple[str, ...]:
    literal = _ip_literal(host)
    if literal is not None:
        return (literal,)
    resolved = resolve_host_ips(host)
    return resolved or ((host,) if host else ())


def _doctor_check_direct_smb_port(
    target: DoctorTarget,
    remote: RemoteAccess,
    discovered_addresses: tuple[str, ...],
    sink: DoctorSink,
) -> DirectSmbState:
    observed_addresses = _dedupe_addresses(discovered_addresses or _fallback_smb_addresses(target.host))
    if not observed_addresses:
        sink.add(CheckResult("FAIL", "no SMB address was discovered or resolved for direct connectivity testing"))
        return DirectSmbState()

    routes: dict[str, RouteSelection] = {}
    untestable: dict[str, str] = {}
    testable_addresses: list[str] = []
    local_addresses = local_interface_addresses()
    for observed in observed_addresses:
        if _ip_literal(observed) is None:
            routes[observed] = RouteSelection("unknown")
            testable_addresses.append(observed)
            continue
        candidates = _link_local_scope_candidates(observed, local_addresses) if is_link_local_ipv6(observed) else [observed]
        if not candidates:
            untestable[observed] = "no usable local IPv6 scope"
            continue
        for candidate in candidates:
            route = select_route_to_address(candidate)
            routes[candidate] = route
            if route.state == "unavailable":
                untestable[candidate] = route.error or "no usable client route"
            elif not any(candidate == known or same_scoped_ip(candidate, known) for known in testable_addresses):
                testable_addresses.append(candidate)

    tcp_errors: dict[str, str | None] = {}
    scoped_groups: dict[str, list[str]] = {}
    for address in testable_addresses:
        if is_link_local_ipv6(address):
            scoped_groups.setdefault(address.partition("%")[0], []).append(address)
        else:
            result = check_smb_port(address)
            tcp_errors[address] = None if result.status == "PASS" else str(result.details.get("error") or result.message)
    for addresses in scoped_groups.values():
        tcp_errors.update(scoped_tcp_connect_errors(addresses, 445))

    reachable_addresses = [address for address in testable_addresses if tcp_errors.get(address) is None]
    observed_families = {_smb_target_family(address) for address in observed_addresses}
    testable_families = {_smb_target_family(address) for address in testable_addresses}
    reachable_families = {_smb_target_family(address) for address in reachable_addresses}

    if not testable_addresses:
        sink.add(CheckResult(
            "FAIL",
            "SMB connectivity could not be tested; discovered addresses have no usable client route or IPv6 scope",
            {"untested_addresses": untestable},
        ))
    else:
        for family in ("ipv4", "ipv6"):
            if family not in observed_families:
                continue
            label = _family_label(family)
            family_testable = [address for address in testable_addresses if _smb_target_family(address) == family]
            family_reachable = [address for address in reachable_addresses if _smb_target_family(address) == family]
            family_untested = {key: value for key, value in untestable.items() if _smb_target_family(key) == family}
            if family not in testable_families:
                sink.add(CheckResult(
                    "INFO",
                    f"{label} SMB connectivity was not tested; no discovered address has a usable client route or scope",
                    {"untested_addresses": family_untested},
                ))
                continue
            if family_untested:
                sink.add(CheckResult(
                    "INFO",
                    f"{label} SMB connectivity was not tested for {', '.join(family_untested)}; no usable client route or scope",
                    {"untested_addresses": family_untested},
                ))
            if family_reachable:
                sink.add(CheckResult(
                    "PASS",
                    f"SMB {label} reachable at {', '.join(family_reachable)}:445",
                    {"tcp_attempts": {address: tcp_errors[address] for address in family_testable}},
                ))
            else:
                sink.add(CheckResult(
                    "WARN" if reachable_families else "FAIL",
                    f"SMB {label} is not reachable at {', '.join(family_testable)}:445",
                    {"tcp_attempts": {address: tcp_errors[address] for address in family_testable}},
                ))
        hostname_testable = [address for address in testable_addresses if _smb_target_family(address) is None]
        if hostname_testable:
            hostname_reachable = [address for address in reachable_addresses if address in hostname_testable]
            sink.add(CheckResult(
                "PASS" if hostname_reachable else "FAIL",
                (
                    f"SMB reachable at {', '.join(hostname_reachable)}:445"
                    if hostname_reachable
                    else f"SMB is not reachable at {', '.join(hostname_testable)}:445"
                ),
                {"tcp_attempts": {address: tcp_errors[address] for address in hostname_testable}},
            ))

    if sink.debug_fields is not None:
        sink.debug_fields["smb_connectivity"] = {
            "observed_addresses": observed_addresses,
            "testable_addresses": testable_addresses,
            "reachable_addresses": reachable_addresses,
            "routes": {
                address: {"state": route.state, "source": route.source, "error": route.error, "errno": route.error_number}
                for address, route in routes.items()
            },
            "tcp_attempts": tcp_errors,
        }
    if testable_addresses and not reachable_addresses:
        _add_remote_service_socket_debug(target, remote, sink)
    return DirectSmbState(
        observed_addresses=tuple(observed_addresses),
        testable_addresses=tuple(testable_addresses),
        reachable_addresses=tuple(reachable_addresses),
        route_sources=tuple((address, route.source) for address, route in routes.items() if route.source),
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


def _doctor_check_nbns(
    target: DoctorTarget,
    remote: RemoteAccess,
    smb_config: SmbConfigState,
    naming: RuntimeNamingState,
    direct_smb: DirectSmbState,
    sink: DoctorSink,
    native_nbns_ready: bool | None = None,
    network: DoctorNetworkProbe | None = None,
) -> None:
    if not remote.remote_checks_enabled:
        return

    network = network or DoctorNetworkProbe(target, remote, sink.debug_fields)
    result_start = sink.result_count()
    _add_nbns_results(
        active_smb_conf=smb_config.text,
        runtime_naming_identity=naming.identity,
        reachable_addresses=direct_smb.reachable_addresses,
        add_result=sink.add,
        native_nbns_ready=native_nbns_ready,
        route_sources=direct_smb.route_sources,
        probe_device_subnets=network.ipv4_subnets,
        debug_fields=sink.debug_fields,
    )
    if any(result.status == "FAIL" for result in sink.new_results_since(result_start)):
        _add_remote_service_socket_debug(target, remote, sink)


def _doctor_check_authenticated_smb(
    inputs: DoctorInputs,
    target: DoctorTarget,
    smb_config: SmbConfigState,
    naming: RuntimeNamingState,
    bonjour_result: DoctorBonjourResult,
    direct_smb: DirectSmbState,
    processes: ProcessSnapshotState,
    sink: DoctorSink,
) -> None:
    if inputs.options.skip_smb:
        return
    if processes.smbd_stuck:
        # Each new SMB connection forks an smbd child that blocks the same way
        # and cannot be killed; the kernel allows 84 processes in all.
        sink.add(
            CheckResult(
                "SKIP",
                "authenticated SMB checks skipped: smbd is stuck in the kernel, "
                "and each new SMB connection would leave another stuck smbd process",
            )
        )
        return

    _add_authenticated_smb_results(
        target.connection,
        inputs.config,
        bonjour_result.target,
        naming.identity,
        host=target.host,
        smb_password=target.smb_password,
        active_smb_conf=smb_config.text,
        active_smb_conf_reason=smb_config.reason,
        direct_smb=direct_smb,
        debug_fields=sink.debug_fields,
        add_result=sink.add,
        # Without a process snapshot, retries could pile up stuck processes.
        retry_delays=() if processes.timed_out else DOCTOR_TRANSIENT_RETRY_DELAYS,
    )
