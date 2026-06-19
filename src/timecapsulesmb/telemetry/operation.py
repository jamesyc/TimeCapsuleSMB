from __future__ import annotations

import os
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

from timecapsulesmb.telemetry import TelemetryClient


OPTION_KEYS = frozenset({
    "action",
    "allow_unsupported",
    "any_protocol",
    "ata_idle_seconds",
    "ata_standby",
    "bonjour_timeout",
    "debug_logging",
    "dry_run",
    "enable_ssh",
    "fix_permissions",
    "force",
    "fruit_metadata_netatalk",
    "include_hidden",
    "include_time_machine",
    "internal_share_use_disk_root",
    "list_volumes",
    "macos_local_network_preflight_duration_ms",
    "macos_local_network_preflight_error",
    "macos_local_network_preflight_result",
    "macos_local_network_preflight_service",
    "max_depth",
    "mdns_advertise_afp",
    "mode",
    "mount_wait",
    "nbns_enabled",
    "no_reboot",
    "no_wait",
    "persist_password",
    "recursive",
    "reboot_after_write",
    "smb_bind_lan_only",
    "smb_browse_compatibility",
    "skip_bonjour",
    "skip_smb",
    "skip_ssh",
    "ssh_wait_timeout",
    "timeout",
    "verbose",
    "wait_after_reboot",
    "yes",
})
SENSITIVE_KEY_PARTS = ("credentials", "password", "secret", "token", "key")
RESERVED_EVENT_FIELD_KEYS = frozenset({
    "schema_version",
    "event",
    "event_id",
    "occurred_at",
    "operation",
    "phase",
    "operation_id",
    "entrypoint",
    "client",
    "options",
    "details",
    "command_id",
})


class OperationTelemetrySession:
    def __init__(
        self,
        telemetry: TelemetryClient,
        operation: str,
        *,
        entrypoint: str,
        client: str,
        started_event: str | None = None,
        finished_event: str | None = None,
        operation_id: str | None = None,
        options: Mapping[str, object] | None = None,
    ) -> None:
        self.telemetry = telemetry
        self.operation = operation
        self.entrypoint = entrypoint
        self.client = client
        self.started_event = started_event or legacy_event_name(operation, "started")
        self.finished_event = finished_event or legacy_event_name(operation, "finished")
        self.operation_id = operation_id or str(uuid.uuid4())
        self.options = dict(options or {})
        self.start_time = time.monotonic()

    def start(self, **fields: object) -> None:
        self._emit(
            self.started_event,
            phase="started",
            options=self.options or None,
            **fields,
        )

    def finish(
        self,
        *,
        result: str,
        error: object | None = None,
        stage: str | None = None,
        risk: str | None = None,
        options: Mapping[str, object] | None = None,
        details: Mapping[str, object] | None = None,
        **fields: object,
    ) -> None:
        emit_options = dict(options or self.options)
        duration_sec = round(time.monotonic() - self.start_time, 3)
        self._emit(
            self.finished_event,
            synchronous=True,
            phase="finished",
            result=result,
            duration_sec=duration_sec,
            error=error,
            stage=stage,
            risk=risk,
            options=emit_options or None,
            details=dict(details or {}) or None,
            **fields,
        )

    def _emit(self, event: str, *, phase: str, **fields: object) -> None:
        try:
            synchronous = bool(fields.pop("synchronous", False))
            options = fields.pop("options", None)
            details = fields.pop("details", None)
            emit_fields = _avoid_reserved_field_collisions(fields)
            self.telemetry.emit(
                event,
                synchronous=synchronous,
                operation=self.operation,
                phase=phase,
                operation_id=self.operation_id,
                entrypoint=self.entrypoint,
                client=self.client,
                options=options if isinstance(options, dict) else None,
                details=details if isinstance(details, dict) else None,
                # Retain the old field for existing dashboards/queries.
                command_id=self.operation_id,
                **emit_fields,
            )
        except Exception:
            pass


def legacy_event_name(operation: str, phase: str) -> str:
    return f"{operation.replace('-', '_')}_{phase}"


def client_from_environment(*, entrypoint: str) -> str:
    value = os.getenv("TCAPSULE_CLIENT", "").strip()
    if value:
        return value
    return "terminal" if entrypoint == "cli" else entrypoint


def telemetry_options_from_params(params: Mapping[str, object]) -> dict[str, object]:
    options: dict[str, object] = {}
    for key in sorted(OPTION_KEYS):
        if key in params:
            value = params.get(key)
            if value is not None:
                options[key] = _jsonable(value)
    return options


def telemetry_options_from_args(args: object | None) -> dict[str, object]:
    if args is None:
        return {}
    if isinstance(args, Mapping):
        return telemetry_options_from_params(args)
    try:
        values = vars(args)
    except TypeError:
        return {}
    return telemetry_options_from_params(values)


def telemetry_details_from_payload(
    operation: str,
    params: Mapping[str, object],
    payload: object | None,
) -> dict[str, object]:
    extractor = DETAIL_EXTRACTORS.get(operation, _details_common)
    return extractor(params, payload)


DetailExtractor = Callable[[Mapping[str, object], Optional[object]], dict[str, object]]


def _details_common(_params: Mapping[str, object], payload: object | None) -> dict[str, object]:
    details: dict[str, object] = {}
    if isinstance(payload, Mapping):
        _copy_payload_key(payload, details, "summary")
    return details


def _details_activate(_params: Mapping[str, object], payload: object | None) -> dict[str, object]:
    details: dict[str, object] = {}
    if isinstance(payload, Mapping):
        _copy_payload_keys(payload, details, ("already_active", "message", "summary"))
        _copy_counts(payload, details)
    return details


def _details_configure(params: Mapping[str, object], payload: object | None) -> dict[str, object]:
    details: dict[str, object] = {}
    _copy_param(params, details, "enable_ssh")
    _copy_param(params, details, "persist_password")
    _copy_param(params, details, "macos_local_network_preflight_result")
    if isinstance(params.get("selected_record"), Mapping):
        details["selected_bonjour_record"] = True
    if isinstance(payload, Mapping):
        _copy_payload_keys(payload, details, (
            "configure_id",
            "host",
            "ssh_authenticated",
            "device_model",
            "device_syap",
            "summary",
        ))
        _copy_compatibility(payload, details)
    return details


def _details_deploy(_params: Mapping[str, object], payload: object | None) -> dict[str, object]:
    details: dict[str, object] = {}
    if isinstance(payload, Mapping):
        _copy_payload_keys(payload, details, (
            "message",
            "netbsd4",
            "payload_dir",
            "payload_family",
            "reboot_requested",
            "rebooted",
            "requires_reboot",
            "summary",
            "verified",
            "waited",
        ))
    return details


def _details_discover(_params: Mapping[str, object], payload: object | None) -> dict[str, object]:
    details: dict[str, object] = {}
    if isinstance(payload, Mapping):
        counts = payload.get("counts")
        if isinstance(counts, Mapping):
            _copy_payload_key(counts, details, "instances", to_key="instance_count")
            _copy_payload_key(counts, details, "resolved", to_key="resolved_count")
            _copy_payload_key(counts, details, "devices", to_key="device_count")
        _copy_payload_key(payload, details, "summary")
    return details


def _details_doctor(_params: Mapping[str, object], payload: object | None) -> dict[str, object]:
    details: dict[str, object] = {}
    if isinstance(payload, Mapping):
        _copy_payload_keys(payload, details, ("counts", "error", "fatal", "summary"))
    return details


def _details_flash(params: Mapping[str, object], payload: object | None) -> dict[str, object]:
    details: dict[str, object] = {}
    _copy_param(params, details, "action", to_key="flash_action")
    _copy_param(params, details, "mode", to_key="flash_mode")
    _copy_param(params, details, "force")
    _copy_param(params, details, "reboot_after_write")
    _copy_param(params, details, "wait_after_reboot")
    if params.get("backup_dir") not in (None, ""):
        details["backup_dir_provided"] = True
    if params.get("firmware_template") not in (None, ""):
        details["firmware_template_provided"] = True
    _copy_param(params, details, "firmware_version")
    if isinstance(payload, Mapping):
        _copy_payload_keys(payload, details, (
            "mode",
            "write_requested",
            "already_satisfied",
            "write_status",
            "write_validated",
            "post_write_action",
            "reboot_requested",
            "rebooted",
            "waited_after_reboot",
            "summary",
        ))
        _copy_counts(payload, details)
        plan = payload.get("flash_plan")
        if isinstance(plan, Mapping):
            _copy_payload_key(plan, details, "target_bank")
            _copy_payload_key(plan, details, "write_may_modify_device")
        outcome = payload.get("write_outcome")
        if isinstance(outcome, Mapping):
            _copy_payload_key(outcome, details, "target_bank")
            _copy_payload_key(outcome, details, "write_may_have_modified_device")
    return details


def _details_fsck(params: Mapping[str, object], payload: object | None) -> dict[str, object]:
    details: dict[str, object] = {}
    _copy_param(params, details, "volume")
    if isinstance(payload, Mapping):
        target = payload.get("target")
        if isinstance(target, Mapping):
            _copy_payload_key(target, details, "name", to_key="volume")
            _copy_payload_key(target, details, "device", to_key="fsck_device")
            _copy_payload_key(target, details, "mountpoint", to_key="fsck_mountpoint")
        _copy_payload_key(payload, details, "device", to_key="fsck_device")
        _copy_payload_key(payload, details, "fsck_device")
        _copy_payload_key(payload, details, "mountpoint", to_key="fsck_mountpoint")
        _copy_payload_key(payload, details, "fsck_mountpoint")
        _copy_payload_key(payload, details, "reboot_was_attempted", to_key="reboot_requested")
        _copy_payload_key(payload, details, "device_came_back_after_reboot", to_key="verified")
        _copy_payload_keys(payload, details, ("returncode", "reboot_requested", "waited", "verified", "summary"))
        _copy_counts(payload, details)
    return details


def _details_reachability(_params: Mapping[str, object], payload: object | None) -> dict[str, object]:
    details: dict[str, object] = {}
    if isinstance(payload, Mapping):
        _copy_payload_keys(payload, details, ("status", "ssh_host", "smb_host", "counts", "summary"))
    return details


def _details_set_ssh(_params: Mapping[str, object], payload: object | None) -> dict[str, object]:
    details: dict[str, object] = {}
    if isinstance(payload, Mapping):
        _copy_payload_keys(payload, details, (
            "action",
            "host",
            "acp_port_reachable",
            "ssh_port_reachable",
            "ssh_disabled_likely",
            "ssh_initially_reachable",
            "ssh_final_reachable",
            "reboot_requested",
            "waited",
            "ssh_verification_skipped",
            "ssh_disable_persisted",
            "ssh_reboot_observed_down",
            "device_recovered",
            "summary",
        ))
    return details


def _details_repair_xattrs(_params: Mapping[str, object], payload: object | None) -> dict[str, object]:
    details: dict[str, object] = {}
    if isinstance(payload, Mapping):
        _copy_payload_keys(payload, details, (
            "error",
            "finding_count",
            "repairable_count",
            "returncode",
            "root",
            "summary_text",
            "telemetry_result",
        ))
        _copy_counts(payload, details)
    return details


def _details_uninstall(_params: Mapping[str, object], payload: object | None) -> dict[str, object]:
    details: dict[str, object] = {}
    if isinstance(payload, Mapping):
        _copy_payload_keys(payload, details, (
            "reboot_requested",
            "rebooted",
            "requires_reboot",
            "summary",
            "verified",
            "waited",
        ))
        _copy_counts(payload, details)
    return details


def _details_validate_install(_params: Mapping[str, object], payload: object | None) -> dict[str, object]:
    details: dict[str, object] = {}
    if isinstance(payload, Mapping):
        _copy_payload_keys(payload, details, ("ok", "counts", "summary"))
    return details


def _details_version_check(_params: Mapping[str, object], payload: object | None) -> dict[str, object]:
    details: dict[str, object] = {}
    if isinstance(payload, Mapping):
        _copy_payload_keys(payload, details, (
            "should_block",
            "local_version_code",
            "current_version",
            "min_supported_version",
            "latest_tag",
            "source",
            "summary",
        ))
    return details


def _details_capabilities(_params: Mapping[str, object], payload: object | None) -> dict[str, object]:
    details: dict[str, object] = {}
    if isinstance(payload, Mapping):
        _copy_payload_keys(payload, details, (
            "api_schema_version",
            "helper_version",
            "helper_version_code",
            "artifact_manifest_sha256",
            "summary",
        ))
        operations = payload.get("operations")
        if isinstance(operations, list):
            details["operation_count"] = len(operations)
    return details


DETAIL_EXTRACTORS: dict[str, DetailExtractor] = {
    "activate": _details_activate,
    "capabilities": _details_capabilities,
    "configure": _details_configure,
    "deploy": _details_deploy,
    "discover": _details_discover,
    "doctor": _details_doctor,
    "flash": _details_flash,
    "fsck": _details_fsck,
    "reachability": _details_reachability,
    "repair-xattrs": _details_repair_xattrs,
    "set-ssh": _details_set_ssh,
    "uninstall": _details_uninstall,
    "validate-install": _details_validate_install,
    "version-check": _details_version_check,
}


def _avoid_reserved_field_collisions(fields: Mapping[str, object]) -> dict[str, object]:
    output: dict[str, object] = {}
    for key, value in fields.items():
        if key in RESERVED_EVENT_FIELD_KEYS:
            output[f"legacy_{key}"] = value
        else:
            output[key] = value
    return output


def confirmation_details(confirmation: object) -> dict[str, object]:
    to_jsonable = getattr(confirmation, "to_jsonable", None)
    if callable(to_jsonable):
        value = to_jsonable()
    else:
        value = confirmation
    if not isinstance(value, Mapping):
        return {}
    details = _jsonable(value)
    return details if isinstance(details, dict) else {}


def _copy_param(source: Mapping[str, object], target: dict[str, object], key: str, *, to_key: str | None = None) -> None:
    value = source.get(key)
    if value not in (None, ""):
        target[to_key or key] = _jsonable(value)


def _copy_payload_key(source: Mapping[str, object], target: dict[str, object], key: str, *, to_key: str | None = None) -> None:
    value = source.get(key)
    if value is not None:
        target[to_key or key] = _jsonable(value)


def _copy_payload_keys(source: Mapping[str, object], target: dict[str, object], keys: tuple[str, ...]) -> None:
    for key in keys:
        _copy_payload_key(source, target, key)


def _copy_counts(source: Mapping[str, object], target: dict[str, object]) -> None:
    counts = source.get("counts")
    if isinstance(counts, Mapping):
        target["counts"] = _jsonable(counts)


def _copy_compatibility(source: Mapping[str, object], target: dict[str, object]) -> None:
    compatibility = source.get("compatibility")
    if not isinstance(compatibility, Mapping):
        return
    _copy_payload_key(compatibility, target, "payload_family", to_key="device_family")
    os_name = compatibility.get("os_name")
    os_release = compatibility.get("os_release")
    arch = compatibility.get("arch")
    if os_name and os_release and arch:
        target["device_os_version"] = f"{os_name} {os_release} ({arch})"


def _jsonable(value: object) -> object:
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, Enum):
        return _jsonable(value.value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        output: dict[str, object] = {}
        for key, item in value.items():
            key_text = str(key)
            if any(part in key_text.lower() for part in SENSITIVE_KEY_PARTS):
                continue
            output[key_text] = _jsonable(item)
        return output
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    return value
