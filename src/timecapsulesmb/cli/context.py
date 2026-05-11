from __future__ import annotations

import time
import uuid
from collections.abc import Mapping
from typing import TYPE_CHECKING

from timecapsulesmb.cli import runtime
from timecapsulesmb.core.config import ConfigError
from timecapsulesmb.core.errors import system_exit_message
from timecapsulesmb.device.errors import DeviceError
from timecapsulesmb.telemetry import build_device_os_version
from timecapsulesmb.telemetry.debug import debug_summary, render_debug_mapping
from timecapsulesmb.transport.errors import TransportError

if TYPE_CHECKING:
    from timecapsulesmb.cli.runtime import ManagedTargetState
    from timecapsulesmb.core.config import AppConfig
    from timecapsulesmb.device.compat import DeviceCompatibility
    from timecapsulesmb.device.probe import ProbedDeviceState, RemoteInterfaceProbeResult
    from timecapsulesmb.telemetry import TelemetryClient
    from timecapsulesmb.transport.ssh import SshConnection


COMMAND_VALUE_BLACKLIST = {
    "TC_PASSWORD",
    # Removed naming keys may still exist in old .env files. They are
    # intentionally ignored and should not appear as command inputs.
    "TC_MDNS_HOST_LABEL",
    "TC_MDNS_INSTANCE_NAME",
    "TC_NETBIOS_NAME",
    # These are already first-class telemetry fields.
    "TC_CONFIGURE_ID",
    "TC_MDNS_DEVICE_MODEL",
    "TC_AIRPORT_SYAP",
}
COMMAND_FIELD_BLACKLIST = {
    # These are already first-class telemetry fields.
    "configure_id",
    "device_model",
    "device_syap",
    "device_os_version",
    "device_family",
    "nbns_enabled",
    "reboot_was_attempted",
    "device_came_back_after_reboot",
}


def _render_connection_debug_lines(connection: SshConnection | None, values: Mapping[str, str] | None) -> list[str]:
    host = None
    ssh_opts = None
    if connection is not None:
        host = connection.host
        ssh_opts = connection.ssh_opts
    elif values is not None:
        host = values.get("TC_HOST") or None
        ssh_opts = values.get("TC_SSH_OPTS") or None
    lines: list[str] = []
    if host:
        lines.append(f"host={host}")
    if ssh_opts:
        lines.append(f"ssh_opts={ssh_opts}")
    return lines


def render_command_debug_lines(
    *,
    command_name: str,
    stage: str | None,
    connection: SshConnection | None,
    values: Mapping[str, str] | None,
    preflight_error: str | None,
    finish_fields: Mapping[str, object],
    probe_state: ProbedDeviceState | None,
    debug_fields: Mapping[str, object],
    config: AppConfig | None = None,
) -> list[str]:
    debug_values = config.values if config is not None else values
    lines = ["Debug context:", f"command={command_name}"]
    if stage:
        lines.append(f"stage={stage}")
    if config is not None:
        lines.append(f"env_path={config.path}")
    lines.extend(_render_connection_debug_lines(connection, debug_values))
    if debug_values is not None:
        lines.extend(render_debug_mapping(debug_values, blacklist=COMMAND_VALUE_BLACKLIST))
    if preflight_error:
        lines.append(f"preflight_error={preflight_error}")
    lines.extend(render_debug_mapping(finish_fields, blacklist=COMMAND_FIELD_BLACKLIST))
    if probe_state is not None:
        lines.extend(render_debug_mapping(debug_summary(probe_state), blacklist=COMMAND_FIELD_BLACKLIST))
    lines.extend(render_debug_mapping(debug_fields, blacklist=COMMAND_FIELD_BLACKLIST))
    return lines


class CommandContext:
    def __init__(
        self,
        telemetry: TelemetryClient,
        command_name: str,
        started_event: str,
        finished_event: str,
        *,
        values: dict[str, str] | None = None,
        config: AppConfig | None = None,
        args: object | None = None,
        **fields: object,
    ) -> None:
        self.telemetry = telemetry
        self.command_name = command_name
        self.values = values
        self.config = config
        self.args = args
        self.finished_event = finished_event
        self.start_time = time.monotonic()
        self.finished = False
        self.command_id = str(uuid.uuid4())
        self.result = "failure"
        self.finish_fields: dict[str, object] = {}
        self.error_lines: list[str] = []
        self.preflight_error: str | None = None
        self.debug_stage: str | None = None
        self.debug_fields: dict[str, object] = {}
        self.connection: SshConnection | None = None
        self.interface_probe: RemoteInterfaceProbeResult | None = None
        self.probe_state: ProbedDeviceState | None = None
        self.compatibility: DeviceCompatibility | None = None
        self._emit_telemetry(started_event, command_id=self.command_id, **fields)

    def __enter__(self) -> "CommandContext":
        return self

    def __exit__(self, exc_type: object, exc: object, _tb: object) -> bool:
        if exc_type is KeyboardInterrupt and self.result != "cancelled":
            self.result = "cancelled"
            if not self.error_lines:
                self.set_error("Cancelled by user")
        elif isinstance(exc, (TransportError, ConfigError, DeviceError)):
            message = str(exc)
            self.result = "failure"
            if message and not self.error_lines:
                self.set_error(message)
            self.finish(result=self.result, **self.finish_fields)
            raise SystemExit(message) from exc
        elif exc_type is SystemExit:
            message = system_exit_message(exc)
            if message and message not in {"0", "None"}:
                self.result = "failure"
                if not self.error_lines:
                    self.set_error(message)
        elif exc_type is not None:
            self.result = "failure"
            if not self.error_lines:
                exc_name = getattr(exc_type, "__name__", str(exc_type))
                message = str(exc) if exc is not None else ""
                self.set_error(f"{exc_name}: {message}" if message else exc_name)
        self.finish(result=self.result, **self.finish_fields)
        return False

    def succeed(self) -> None:
        self.result = "success"

    def cancel_with_error(self, message: str = "Cancelled by user") -> None:
        self.result = "cancelled"
        self.set_error(message)

    def fail(self) -> None:
        self.result = "failure"

    def fail_with_error(self, message: str) -> None:
        self.result = "failure"
        self.set_error(message)

    def update_fields(self, **fields: object) -> None:
        for key, value in fields.items():
            if value is not None:
                self.finish_fields[key] = value

    def set_stage(self, stage: str) -> None:
        self.debug_stage = stage

    def add_debug_fields(self, **fields: object) -> None:
        for key, value in fields.items():
            if value is not None:
                self.debug_fields[key] = debug_summary(value)

    def set_error(self, message: str) -> None:
        self.error_lines = [line.rstrip() for line in message.splitlines() if line.strip()]

    def build_error(self) -> str | None:
        if not self.error_lines:
            return None
        return "\n".join([
            *self.error_lines,
            "",
            *render_command_debug_lines(
                command_name=self.command_name,
                stage=self.debug_stage,
                connection=self.connection,
                values=self.values,
                preflight_error=self.preflight_error,
                finish_fields=self.finish_fields,
                probe_state=self.probe_state,
                debug_fields=self.debug_fields,
                config=self.config,
            ),
        ])

    def _emit_telemetry(self, event: str, **fields: object) -> None:
        try:
            self.telemetry.emit(event, **fields)
        except Exception:
            pass

    def resolve_env_connection(
        self,
        *,
        required_keys: tuple[str, ...] = (),
        allow_empty_password: bool = False,
    ) -> SshConnection:
        if self.config is None:
            raise RuntimeError("CommandContext config is not set.")
        self.connection = runtime.resolve_env_connection(
            self.config,
            required_keys=required_keys,
            allow_empty_password=allow_empty_password,
        )
        return self.connection

    def require_valid_config(self, *, profile: str) -> None:
        if self.config is None:
            raise RuntimeError("CommandContext config is not set.")
        from timecapsulesmb.core.config import require_valid_app_config
        require_valid_app_config(
            self.config,
            profile=profile,
            command_name=self.command_name,
        )

    def _apply_managed_target_state(self, target: ManagedTargetState) -> ManagedTargetState:
        self.connection = target.connection
        self.interface_probe = target.interface_probe
        if target.probe_state is not None:
            self.probe_state = target.probe_state
            self.compatibility = target.probe_state.compatibility
            if self.compatibility is not None:
                self.update_fields(
                    device_os_version=build_device_os_version(
                        self.compatibility.os_name,
                        self.compatibility.os_release,
                        self.compatibility.arch,
                    ),
                    device_family=self.compatibility.payload_family,
                )
        return target

    def inspect_managed_connection(self, *, iface: str, include_probe: bool = False) -> ManagedTargetState:
        connection = self.connection if self.connection is not None else self.resolve_env_connection()
        target = runtime.inspect_managed_connection(connection, iface, include_probe=include_probe)
        return self._apply_managed_target_state(target)

    def resolve_validated_managed_target(self, *, profile: str, include_probe: bool = False) -> ManagedTargetState:
        if self.config is None:
            raise RuntimeError("CommandContext config is not set.")
        target = runtime.resolve_validated_managed_target(
            self.config,
            command_name=self.command_name,
            profile=profile,
            include_probe=include_probe,
        )
        return self._apply_managed_target_state(target)

    def require_compatibility(self) -> DeviceCompatibility:
        if self.connection is None:
            raise RuntimeError("CommandContext connection is not set.")
        self.compatibility = runtime.require_connection_compatibility(self.connection) if self.probe_state is None else runtime.require_compatibility(
            self.probe_state.compatibility,
            fallback_error=self.probe_state.probe_result.error or "Failed to determine remote device OS compatibility.",
        )
        self.update_fields(device_os_version=build_device_os_version(
            self.compatibility.os_name,
            self.compatibility.os_release,
            self.compatibility.arch,
        ))
        self.update_fields(device_family=self.compatibility.payload_family)
        return self.compatibility

    def finish(self, *, result: str, **fields: object) -> None:
        if self.finished:
            return
        self.finished = True
        duration_sec = round(time.monotonic() - self.start_time, 3)
        error = None if result == "success" else self.build_error()
        if result != "success" and error is None:
            error = f"{self.command_name} failed without additional details."
        self._emit_telemetry(
            self.finished_event,
            synchronous=True,
            command_id=self.command_id,
            result=result,
            duration_sec=duration_sec,
            error=error,
            **fields,
        )
