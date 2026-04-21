from __future__ import annotations

import time
import uuid
from typing import TYPE_CHECKING, Callable

from timecapsulesmb.cli import runtime
from timecapsulesmb.telemetry import build_device_os_version, detect_device_family

if TYPE_CHECKING:
    from timecapsulesmb.cli.runtime import ResolvedConnection
    from timecapsulesmb.device.compat import DeviceCompatibility
    from timecapsulesmb.telemetry import TelemetryClient


class CommandContext:
    def __init__(
        self,
        telemetry: TelemetryClient,
        command_name: str,
        started_event: str,
        finished_event: str,
        *,
        values: dict[str, str] | None = None,
        args: object | None = None,
        **fields: object,
    ) -> None:
        self.telemetry = telemetry
        self.command_name = command_name
        self.values = values
        self.args = args
        self.finished_event = finished_event
        self.start_time = time.monotonic()
        self.finished = False
        self.command_id = str(uuid.uuid4())
        self.result = "failure"
        self.finish_fields: dict[str, object] = {}
        self.connection: ResolvedConnection | None = None
        self.compatibility: DeviceCompatibility | None = None
        self.telemetry.emit(started_event, command_id=self.command_id, **fields)

    def __enter__(self) -> "CommandContext":
        return self

    def __exit__(self, exc_type: object, _exc: object, _tb: object) -> bool:
        if exc_type is KeyboardInterrupt and self.result != "cancelled":
            self.result = "cancelled"
        self.finish(result=self.result, **self.finish_fields)
        return False

    def set_result(self, result: str) -> None:
        self.result = result

    def succeed(self) -> None:
        self.result = "success"

    def cancel(self) -> None:
        self.result = "cancelled"

    def fail(self) -> None:
        self.result = "failure"

    def update_fields(self, **fields: object) -> None:
        for key, value in fields.items():
            if value is not None:
                self.finish_fields[key] = value

    def set_values(self, values: dict[str, str]) -> None:
        self.values = values

    def resolve_env_connection(
        self,
        *,
        required_keys: tuple[str, ...] = (),
        allow_empty_password: bool = False,
    ) -> ResolvedConnection:
        if self.values is None:
            raise RuntimeError("CommandContext values are not set.")
        self.connection = runtime.resolve_env_connection(
            self.values,
            required_keys=required_keys,
            allow_empty_password=allow_empty_password,
        )
        return self.connection

    def resolve_validated_managed_connection(self, *, profile: str) -> ResolvedConnection:
        if self.values is None:
            raise RuntimeError("CommandContext values are not set.")
        self.connection = runtime.resolve_validated_managed_connection(
            self.values,
            command_name=self.command_name,
            profile=profile,
        )
        return self.connection

    def probe_compatibility(
        self,
        probe: Callable[[ResolvedConnection], DeviceCompatibility] | None = None,
    ) -> DeviceCompatibility:
        if self.connection is None:
            raise RuntimeError("CommandContext connection is not set.")
        probe_fn = probe or runtime.probe_compatibility
        self.compatibility = probe_fn(self.connection)
        self.update_fields(device_os_version=build_device_os_version(
            self.compatibility.os_name,
            self.compatibility.os_release,
            self.compatibility.arch,
        ))
        self.update_fields(device_family=detect_device_family(self.compatibility.payload_family))
        return self.compatibility

    def finish(self, *, result: str, **fields: object) -> None:
        if self.finished:
            return
        self.finished = True
        duration_sec = round(time.monotonic() - self.start_time, 3)
        self.telemetry.emit(
            self.finished_event,
            synchronous=True,
            command_id=self.command_id,
            result=result,
            duration_sec=duration_sec,
            **fields,
        )
