from __future__ import annotations

import traceback
from collections.abc import Mapping
from typing import TYPE_CHECKING

from timecapsulesmb.app.events import EventSink
from timecapsulesmb.core.config import airport_exact_display_name_from_identity
from timecapsulesmb.core.redaction import SENSITIVE_KEY_PARTS, redact_sensitive_fields
from timecapsulesmb.core.summaries import Summary
from timecapsulesmb.services.callbacks import OperationCallbacks
from timecapsulesmb.services.context import OperationContext, exception_cause_detail
from timecapsulesmb.telemetry import build_device_os_version

if TYPE_CHECKING:
    from timecapsulesmb.core.config import AppConfig
    from timecapsulesmb.device.probe import ProbedDeviceState
    from timecapsulesmb.services.runtime import ManagedTargetState
    from timecapsulesmb.transport.ssh import SshConnection


class AppOperationContext:
    """GUI/API operation adapter around shared diagnostic context and app events."""

    def __init__(self, operation: str, sink: EventSink) -> None:
        self.operation = operation
        self.sink = sink
        self.diagnostics = OperationContext(operation)
        self.result = "failure"
        self.error: str | None = None

    @property
    def current_stage(self) -> str | None:
        return self.diagnostics.debug_stage or self.sink.current_stage(self.operation)

    @property
    def current_risk(self) -> str | None:
        return self.sink.current_risk(self.operation)

    @property
    def values(self) -> Mapping[str, str] | None:
        return self.diagnostics.values

    @values.setter
    def values(self, value: Mapping[str, str] | None) -> None:
        self.diagnostics.values = value

    @property
    def config(self) -> AppConfig | None:
        return self.diagnostics.config

    @config.setter
    def config(self, value: AppConfig | None) -> None:
        self.diagnostics.config = value

    @property
    def connection(self) -> SshConnection | None:
        return self.diagnostics.connection

    @connection.setter
    def connection(self, value: SshConnection | None) -> None:
        self.diagnostics.connection = value

    @property
    def probe_state(self) -> ProbedDeviceState | None:
        return self.diagnostics.probe_state

    @probe_state.setter
    def probe_state(self, value: ProbedDeviceState | None) -> None:
        self.diagnostics.probe_state = value

    @property
    def finish_fields(self) -> dict[str, object]:
        return self.diagnostics.finish_fields

    def stage(self, stage: str) -> None:
        self.diagnostics.set_stage(stage)
        self.sink.stage(self.operation, stage)

    def log(self, message: str, *, level: str = "info") -> None:
        self.sink.log(self.operation, message, level=level)

    def log_summary(self, summary: Summary) -> None:
        self.sink.log(self.operation, summary.text, summary=summary)

    def check(self, *, status: str, message: str, details: dict[str, object] | None = None) -> None:
        self.sink.check(self.operation, status=status, message=message, details=details)

    def emit_result(self, *, ok: bool, payload: object | None = None, debug: object | None = None) -> None:
        self.sink.result(self.operation, ok=ok, payload=payload, debug=debug)

    def to_operation_callbacks(self) -> OperationCallbacks:
        return OperationCallbacks(
            set_stage=self.stage,
            log=self.log,
            log_summary=self.log_summary,
            add_debug_fields=self.add_debug_fields,
            update_fields=self.update_fields,
            record_execution_measurement=self.record_execution_measurement,
        )

    def update_fields(self, **fields: object) -> None:
        self.diagnostics.update_fields(**fields)

    def add_debug_fields(self, **fields: object) -> None:
        self.diagnostics.add_debug_fields(**fields)

    def record_execution_measurement(self, kind: str, **fields: object) -> None:
        self.diagnostics.record_execution_measurement(kind, **fields)

    def execution_telemetry(self, *, result: str) -> dict[str, object]:
        return self.diagnostics.execution_telemetry(result=result)

    def failure_debug(self, exc: BaseException | None = None, *, include_traceback: bool = False) -> dict[str, object]:
        fields = dict(self.diagnostics.debug_fields)
        exception_debug = getattr(exc, "debug", None)
        if isinstance(exception_debug, Mapping):
            fields.update(exception_debug)
        elif exception_debug is not None:
            fields["exception"] = exception_debug
        if exc is not None:
            cause = exception_cause_detail(exc)
            if cause and "cause" not in fields:
                fields["cause"] = cause
            if include_traceback:
                fields["traceback"] = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        if self.current_stage:
            fields["stage"] = self.current_stage
        fields["execution"] = self.execution_telemetry(result="failure")
        return redact_sensitive_fields(fields, sensitive_values=self._known_sensitive_values())

    def _known_sensitive_values(self) -> set[str]:
        sensitive_values: set[str] = set()
        values = self.config.values if self.config is not None else self.values
        if values is not None:
            for key, value in values.items():
                if value and any(part in key.lower() for part in SENSITIVE_KEY_PARTS):
                    sensitive_values.add(value)
        if self.connection is not None and self.connection.password:
            sensitive_values.add(self.connection.password)
        return sensitive_values

    def known_airport_display_name(self) -> str | None:
        model = self.finish_fields.get("device_model")
        syap = self.finish_fields.get("device_syap")
        if not isinstance(model, str) and not isinstance(syap, str):
            return None
        return airport_exact_display_name_from_identity(
            model=model if isinstance(model, str) else None,
            syap=syap if isinstance(syap, str) else None,
        )

    def set_error(self, message: str) -> None:
        self.error = message
        self.diagnostics.set_error(message)

    def succeed(self) -> None:
        self.result = "success"

    def fail_with_error(self, message: str) -> None:
        self.result = "failure"
        self.set_error(message)

    def build_error(self) -> str | None:
        return self.diagnostics.build_error()

    def diagnostic_error(self, message: object | None = None) -> str | None:
        if message is not None and not self.diagnostics.error_lines:
            self.set_error(str(message))
        return self.build_error()

    def apply_managed_target(self, target: ManagedTargetState) -> ManagedTargetState:
        self.connection = target.connection
        if target.probe_state is not None:
            self.apply_probe_state(target.probe_state)
        return target

    def apply_probe_state(self, probe_state: ProbedDeviceState) -> None:
        self.probe_state = probe_state
        probe = probe_state.probe_result
        self.update_fields(device_model=probe.airport_model, device_syap=probe.airport_syap)
        compatibility = probe_state.compatibility
        if compatibility is not None:
            self.update_fields(
                device_os_version=build_device_os_version(
                    compatibility.os_name,
                    compatibility.os_release,
                    compatibility.arch,
                ),
                device_family=compatibility.payload_family,
            )
