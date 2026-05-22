from __future__ import annotations

import os
import time
import uuid
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path

from timecapsulesmb.telemetry import TelemetryClient


OPTION_KEYS = frozenset({
    "allow_unsupported",
    "any_protocol",
    "ata_idle_seconds",
    "ata_standby",
    "bonjour_timeout",
    "debug_logging",
    "dry_run",
    "fix_permissions",
    "include_hidden",
    "include_time_machine",
    "internal_share_use_disk_root",
    "list_volumes",
    "max_depth",
    "mount_wait",
    "nbns_enabled",
    "no_reboot",
    "no_wait",
    "recursive",
    "skip_bonjour",
    "skip_smb",
    "skip_ssh",
    "verbose",
    "yes",
})
DETAIL_KEYS_BY_OPERATION = {
    "activate": ("already_active", "message", "summary"),
    "configure": ("configure_id", "ssh_authenticated", "device_model", "device_syap", "summary"),
    "deploy": (
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
    ),
    "doctor": ("counts", "error", "fatal", "summary"),
    "repair-xattrs": (
        "error",
        "finding_count",
        "repairable_count",
        "returncode",
        "root",
        "summary_text",
        "telemetry_result",
    ),
    "uninstall": ("reboot_requested", "rebooted", "requires_reboot", "summary", "verified", "waited"),
}
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
                # Retain the old field during schema v4 rollout for existing dashboards/queries.
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
    return "terminal" if entrypoint == "cli" else "terminal"


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
    details: dict[str, object] = {}
    if operation == "fsck":
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
            for key in ("returncode", "reboot_requested", "waited", "verified", "summary"):
                _copy_payload_key(payload, details, key)
        return details

    if isinstance(payload, Mapping):
        for key in DETAIL_KEYS_BY_OPERATION.get(operation, ()):
            _copy_payload_key(payload, details, key)
        compatibility = payload.get("compatibility")
        if isinstance(compatibility, Mapping):
            _copy_payload_key(compatibility, details, "payload_family", to_key="device_family")
            os_name = compatibility.get("os_name")
            os_release = compatibility.get("os_release")
            arch = compatibility.get("arch")
            if os_name and os_release and arch:
                details["device_os_version"] = f"{os_name} {os_release} ({arch})"
    return details


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
