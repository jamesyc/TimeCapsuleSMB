from __future__ import annotations

from typing import TYPE_CHECKING

from timecapsulesmb.app.events import EventSink
from timecapsulesmb.services.context import OperationContext
from timecapsulesmb.services.runtime import RuntimeOperationCallbacks
from timecapsulesmb.telemetry import build_device_os_version

if TYPE_CHECKING:
    from collections.abc import Mapping

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

    def set_stage(self, stage: str) -> None:
        self.stage(stage)

    def log(self, message: str, *, level: str = "info") -> None:
        self.sink.log(self.operation, message, level=level)

    def check(self, *, status: str, message: str, details: dict[str, object] | None = None) -> None:
        self.sink.check(self.operation, status=status, message=message, details=details)

    def emit_result(self, *, ok: bool, payload: object | None = None) -> None:
        self.sink.result(self.operation, ok=ok, payload=payload)

    def to_runtime_callbacks(self) -> RuntimeOperationCallbacks:
        return RuntimeOperationCallbacks(
            set_stage=self.stage,
            log=self.log,
            add_debug_fields=self.add_debug_fields,
            update_fields=self.update_fields,
        )

    def update_fields(self, **fields: object) -> None:
        self.diagnostics.update_fields(**fields)

    def add_debug_fields(self, **fields: object) -> None:
        self.diagnostics.add_debug_fields(**fields)

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
