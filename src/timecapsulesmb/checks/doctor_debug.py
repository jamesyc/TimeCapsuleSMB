from __future__ import annotations

from collections.abc import Iterable, Mapping

from timecapsulesmb.checks.models import CheckResult
from timecapsulesmb.checks.doctor_state import DoctorSink, DoctorTarget, RemoteAccess
from timecapsulesmb.device.probe import (
    REMOTE_PAYLOAD_LOG_FILENAMES,
    REMOTE_RUNTIME_RAM_LOG_PATHS,
    read_remote_service_socket_diagnostics_conn,
    read_runtime_log_tails_conn,
    read_runtime_ram_diagnostics_conn,
)
from timecapsulesmb.device.storage import mast_probe_debug_summary, probe_mast_diagnostics_conn
from timecapsulesmb.discovery.native_dns_sd import browse_native_dns_sd


_MAST_PROBE_DISK_FAILURE_MESSAGES = frozenset(
    (
        "managed runtime smb.conf missing",
        "active smb.conf xattr_tdb:file parent is missing",
        "one or more managed share volumes are not mounted",
        "could not determine active Samba share name",
    )
)
_MAST_PROBE_DISK_FAILURE_PREFIXES = (
    "SMB directory create failed: tree connect failed: NT_STATUS_BAD_NETWORK_NAME",
)


def _doctor_results_need_mast_probe(results: Iterable[CheckResult]) -> bool:
    for result in results:
        if result.status != "FAIL":
            continue
        if result.message in _MAST_PROBE_DISK_FAILURE_MESSAGES:
            return True
        if any(result.message.startswith(prefix) for prefix in _MAST_PROBE_DISK_FAILURE_PREFIXES):
            return True
    return False


def _add_bonjour_debug_fields(
    debug_fields: dict[str, object] | None,
    *,
    bonjour_debug_needed: bool,
    bonjour_expected_debug: dict[str, str | None] | None,
    bonjour_zeroconf_debug: object | None,
    bonjour_native_fallback_debug: object | None = None,
    bonjour_backend_debug: dict[str, str] | None = None,
) -> None:
    if not bonjour_debug_needed or debug_fields is None:
        return
    if bonjour_expected_debug is not None:
        debug_fields["bonjour_expected"] = bonjour_expected_debug
    if bonjour_zeroconf_debug is not None:
        debug_fields["bonjour_zeroconf"] = bonjour_zeroconf_debug
    if bonjour_native_fallback_debug is not None:
        debug_fields["bonjour_native_fallback"] = bonjour_native_fallback_debug
    if bonjour_backend_debug:
        debug_fields["bonjour_backend"] = bonjour_backend_debug
    if bonjour_native_fallback_debug is not None:
        return
    try:
        native_dns_sd = browse_native_dns_sd()
    except Exception as e:
        debug_fields["bonjour_native_dns_sd_error"] = f"{type(e).__name__}: {e}"
    else:
        if native_dns_sd is not None:
            debug_fields["bonjour_native_dns_sd"] = native_dns_sd


def _add_remote_service_socket_debug(target: DoctorTarget, remote: RemoteAccess, sink: DoctorSink) -> None:
    if sink.debug_fields is None or not remote.remote_checks_enabled:
        return
    if "remote_service_sockets" in sink.debug_fields:
        return
    try:
        sink.debug_fields["remote_service_sockets"] = read_remote_service_socket_diagnostics_conn(target.connection)
    except Exception as e:
        sink.debug_fields["remote_service_sockets_error"] = f"{type(e).__name__}: {e}"


def _doctor_add_mast_probe_on_disk_failure(target: DoctorTarget, remote: RemoteAccess, sink: DoctorSink) -> None:
    if not remote.remote_checks_enabled or sink.debug_fields is None:
        return
    if not _doctor_results_need_mast_probe(sink.results):
        return

    try:
        sink.debug_fields.update(mast_probe_debug_summary(probe_mast_diagnostics_conn(target.connection)))
    except Exception as e:
        sink.debug_fields["mast_probe_error"] = f"{type(e).__name__}: {e}"


_REMOTE_LOG_TAIL_TIMEOUT_MARKER = "Timed out waiting for ssh command"


def _remote_log_tail_timed_out(value: object) -> bool:
    return (
        isinstance(value, str)
        and value.startswith("(unavailable:")
        and _REMOTE_LOG_TAIL_TIMEOUT_MARKER in value
    )


def _data_disk_unresponsive_result(logs: Mapping[str, object]) -> CheckResult | None:
    # Timeouts reading small log files on the data disk while the ramdisk logs
    # read fine point at the disk itself (failing, or not spinning back up)
    # rather than SSH or the device being down. Surface that as its own FAIL
    # instead of leaving it buried in debug context.
    payload_dir = logs.get("remote_payload_log_dir")
    if not isinstance(payload_dir, str) or payload_dir.startswith("(unavailable"):
        # Without a payload dir the payload log keys are ramdisk fallbacks,
        # so a timeout there says nothing about the data disk.
        return None
    timed_out_files = sorted(
        filename
        for key, filename in REMOTE_PAYLOAD_LOG_FILENAMES.items()
        if _remote_log_tail_timed_out(logs.get(key))
    )
    if not timed_out_files:
        return None
    ram_reads = [logs.get(key) for key in REMOTE_RUNTIME_RAM_LOG_PATHS]
    ram_read_ok = any(
        isinstance(value, str) and not value.startswith("(unavailable") for value in ram_reads
    )
    if not ram_read_ok:
        # SSH or the whole device is unhealthy; do not blame the data disk.
        return None
    return CheckResult(
        "FAIL",
        f"data disk appears unresponsive: reading {', '.join(timed_out_files)} under "
        f"{payload_dir.rstrip('/')}/logs timed out over SSH while ramdisk reads succeeded; "
        "the disk may be failing or not spinning back up "
        "(run disk repair, then power-cycle the device)",
        {"data_disk_timed_out_logs": timed_out_files},
    )


def _doctor_add_fatal_runtime_log_tails(target: DoctorTarget, remote: RemoteAccess, sink: DoctorSink) -> None:
    if sink.fatal() and sink.debug_fields is not None and remote.remote_checks_enabled:
        logs = read_runtime_log_tails_conn(target.connection)
        sink.debug_fields.update(logs)
        disk_result = _data_disk_unresponsive_result(logs)
        if disk_result is not None:
            sink.add(disk_result)
        try:
            sink.debug_fields["remote_runtime_ram_diagnostics"] = read_runtime_ram_diagnostics_conn(target.connection)
        except Exception as e:
            sink.debug_fields["remote_runtime_ram_diagnostics_error"] = f"{type(e).__name__}: {e}"
