from __future__ import annotations

from collections.abc import Iterable

from timecapsulesmb.checks.models import CheckResult
from timecapsulesmb.checks.doctor_state import DoctorBonjourResult, DoctorSink, DoctorTarget, RemoteAccess
from timecapsulesmb.device.probe import read_remote_service_socket_diagnostics_conn, read_runtime_log_tails_conn
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


def _doctor_add_bonjour_debug_fields(bonjour_result: DoctorBonjourResult, sink: DoctorSink) -> None:
    _add_bonjour_debug_fields(
        sink.debug_fields,
        bonjour_debug_needed=bonjour_result.debug_needed,
        bonjour_expected_debug=bonjour_result.expected_debug,
        bonjour_zeroconf_debug=bonjour_result.zeroconf_debug,
    )


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


def _doctor_add_fatal_runtime_log_tails(target: DoctorTarget, remote: RemoteAccess, sink: DoctorSink) -> None:
    if sink.fatal() and sink.debug_fields is not None and remote.remote_checks_enabled:
        sink.debug_fields.update(read_runtime_log_tails_conn(target.connection))
