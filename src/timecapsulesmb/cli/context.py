from __future__ import annotations

import time
import uuid
from typing import TYPE_CHECKING, Callable

from timecapsulesmb.cli import runtime
from timecapsulesmb.telemetry import build_device_os_version

if TYPE_CHECKING:
    from timecapsulesmb.cli.runtime import ManagedTargetState
    from timecapsulesmb.device.compat import DeviceCompatibility
    from timecapsulesmb.device.probe import ProbedDeviceState
    from timecapsulesmb.telemetry import TelemetryClient
    from timecapsulesmb.transport.ssh import SshConnection


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
        self.error_lines: list[str] = []
        self.preflight_error: str | None = None
        self._debug_context_added = False
        self.connection: SshConnection | None = None
        self.probe_state: ProbedDeviceState | None = None
        self.compatibility: DeviceCompatibility | None = None
        self.telemetry.emit(started_event, command_id=self.command_id, **fields)

    def __enter__(self) -> "CommandContext":
        return self

    def __exit__(self, exc_type: object, exc: object, _tb: object) -> bool:
        if exc_type is KeyboardInterrupt and self.result != "cancelled":
            self.result = "cancelled"
            if not self.error_lines:
                self.set_error("Cancelled by user")
        elif exc_type is SystemExit:
            message = str(exc)
            if message and message not in {"0", "None"}:
                self.result = "failure"
                if not self.error_lines:
                    self.set_error(message)
                if "SSH error:" in message:
                    self.add_debug_context()
        elif exc_type is not None:
            self.result = "failure"
            if not self.error_lines:
                exc_name = getattr(exc_type, "__name__", str(exc_type))
                message = str(exc) if exc is not None else ""
                self.set_error(f"{exc_name}: {message}" if message else exc_name)
        self.finish(result=self.result, **self.finish_fields)
        return False

    def set_result(self, result: str) -> None:
        self.result = result

    def succeed(self) -> None:
        self.result = "success"

    def cancel(self) -> None:
        self.result = "cancelled"

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

    def set_error(self, message: str) -> None:
        self.error_lines = [line.rstrip() for line in message.splitlines() if line.strip()]

    def add_error_line(self, message: str) -> None:
        line = message.strip()
        if line:
            self.error_lines.append(line)

    def add_debug_context(self, *, extra_fields: dict[str, object] | None = None) -> None:
        if self._debug_context_added:
            return
        self._debug_context_added = True
        context_lines = ["Debug context:", f"command={self.command_name}"]
        host = None
        ssh_opts = None
        if self.connection is not None:
            host = self.connection.host
            ssh_opts = self.connection.ssh_opts
        elif self.values is not None:
            host = self.values.get("TC_HOST") or None
            ssh_opts = self.values.get("TC_SSH_OPTS") or None
        if host:
            context_lines.append(f"host={host}")
        if ssh_opts:
            context_lines.append(f"ssh_opts={ssh_opts}")
        if self.values is not None:
            net_iface = self.values.get("TC_NET_IFACE")
            device_model = self.values.get("TC_MDNS_DEVICE_MODEL")
            device_syap = self.values.get("TC_AIRPORT_SYAP")
            if net_iface:
                context_lines.append(f"net_iface={net_iface}")
            if device_model:
                context_lines.append(f"device_model={device_model}")
            if device_syap:
                context_lines.append(f"device_syap={device_syap}")
        if self.preflight_error:
            context_lines.append(f"preflight_error={self.preflight_error}")
        for key in ("device_os_version", "device_family", "nbns_enabled", "reboot_was_attempted", "device_came_back_after_reboot"):
            value = self.finish_fields.get(key)
            if value is None:
                continue
            if isinstance(value, bool):
                rendered = str(value).lower()
            else:
                rendered = str(value)
            context_lines.append(f"{key}={rendered}")
        if self.probe_state is not None:
            probe_result = self.probe_state.probe_result
            context_lines.append(f"probe_ssh_port_reachable={str(probe_result.ssh_port_reachable).lower()}")
            context_lines.append(f"probe_ssh_authenticated={str(probe_result.ssh_authenticated).lower()}")
            context_lines.append(f"probe_os_name={probe_result.os_name or 'unknown'}")
            context_lines.append(f"probe_os_release={probe_result.os_release or 'unknown'}")
            context_lines.append(f"probe_arch={probe_result.arch or 'unknown'}")
            context_lines.append(f"probe_elf_endianness={probe_result.elf_endianness or 'unknown'}")
            if probe_result.error:
                context_lines.append(f"probe_error={probe_result.error}")
            compatibility = self.probe_state.compatibility
            if compatibility is not None:
                context_lines.append(f"probe_payload_family={compatibility.payload_family or 'unknown'}")
                context_lines.append(f"probe_supported={str(compatibility.supported).lower()}")
                context_lines.append(f"probe_device_generation={compatibility.device_generation}")
        if extra_fields:
            for key, value in extra_fields.items():
                if value is None:
                    continue
                context_lines.append(f"{key}={str(value).lower() if isinstance(value, bool) else value}")
        if self.error_lines:
            self.error_lines.append("")
        self.error_lines.extend(context_lines)

    def build_error(self) -> str | None:
        if not self.error_lines:
            return None
        return "\n".join(self.error_lines)

    def set_values(self, values: dict[str, str]) -> None:
        self.values = values

    def resolve_env_connection(
        self,
        *,
        required_keys: tuple[str, ...] = (),
        allow_empty_password: bool = False,
    ) -> SshConnection:
        if self.values is None:
            raise RuntimeError("CommandContext values are not set.")
        self.connection = runtime.resolve_env_connection(
            self.values,
            required_keys=required_keys,
            allow_empty_password=allow_empty_password,
        )
        return self.connection

    def resolve_validated_managed_connection(self, *, profile: str) -> SshConnection:
        return self.resolve_validated_managed_target(profile=profile, include_probe=False).connection

    def resolve_validated_managed_target(self, *, profile: str, include_probe: bool = False) -> ManagedTargetState:
        if self.values is None:
            raise RuntimeError("CommandContext values are not set.")
        target = runtime.resolve_validated_managed_target(
            self.values,
            command_name=self.command_name,
            profile=profile,
            include_probe=include_probe,
        )
        self.connection = target.connection
        if target.probe_state is not None:
            self.probe_state = target.probe_state
            self.compatibility = target.probe_state.compatibility
        return target

    def probe_device_state(
        self,
        probe: Callable[[SshConnection], ProbedDeviceState] | None = None,
    ) -> ProbedDeviceState:
        if self.connection is None:
            raise RuntimeError("CommandContext connection is not set.")
        probe_fn = probe or runtime.probe_connection_state
        self.probe_state = probe_fn(self.connection)
        self.compatibility = self.probe_state.compatibility
        return self.probe_state

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
        self.telemetry.emit(
            self.finished_event,
            synchronous=True,
            command_id=self.command_id,
            result=result,
            duration_sec=duration_sec,
            error=error,
            **fields,
        )
