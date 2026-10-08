from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from typing import TYPE_CHECKING

from timecapsulesmb.cli import runtime as cli_runtime
from timecapsulesmb.cli.util import color_red
from timecapsulesmb.core.config import ConfigError, airport_exact_display_name_from_identity
from timecapsulesmb.core.errors import system_exit_message
from timecapsulesmb.device.errors import DeviceError
from timecapsulesmb.device.probe import probe_remote_airport_identity_conn
from timecapsulesmb.services.callbacks import OperationCallbacks
from timecapsulesmb.services.context import (
    message_with_exception_cause,
    OperationContext,
)
from timecapsulesmb.services import runtime as service_runtime
from timecapsulesmb.telemetry import build_device_os_version
from timecapsulesmb.transport import local as local_transport
from timecapsulesmb.telemetry.operation import (
    OperationTelemetrySession,
    client_from_environment,
    telemetry_details_from_payload,
    telemetry_options_from_args,
)
from timecapsulesmb.transport.errors import (
    format_ssh_timeout_slow_device_error,
    is_ssh_timeout_error,
    ssh_timeout_slow_device_message,
    TransportError,
)

if TYPE_CHECKING:
    from timecapsulesmb.core.config import AppConfig
    from timecapsulesmb.device.compat import DeviceCompatibility
    from timecapsulesmb.device.probe import ProbedDeviceState
    from timecapsulesmb.services.runtime import ManagedTargetState
    from timecapsulesmb.telemetry import TelemetryClient
    from timecapsulesmb.transport.ssh import SshConnection


OPTIONAL_IDENTITY_PROBE_FINISH_TIMEOUT_SECONDS = 0.1
CLI_PROGRESS_SECONDS = 30


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
        self.operation_context = OperationContext(command_name, values=values, config=config)
        self.args = args
        self.finished_event = finished_event
        self.finished = False
        self.command_id = str(uuid.uuid4())
        self.result = "failure"
        self.compatibility: DeviceCompatibility | None = None
        self._optional_airport_identity_thread: threading.Thread | None = None
        self._optional_airport_identity: tuple[str | None, str | None] | None = None
        # The stage and time of the last progress line printed.
        self._last_progress_print: tuple[str, float] | None = None
        self.telemetry_session = OperationTelemetrySession(
            telemetry,
            command_name,
            entrypoint="cli",
            client=client_from_environment(entrypoint="cli"),
            started_event=started_event,
            finished_event=finished_event,
            operation_id=self.command_id,
            options=telemetry_options_from_args(args),
        )
        self.telemetry_session.start(**fields)

    def __enter__(self) -> "CommandContext":
        # Under --json every command keeps stdout for its JSON document.
        self._stdout_scope = ExitStack()
        self._stdout_scope.enter_context(cli_runtime.json_mode_stdout(bool(getattr(self.args, "json", False))))
        return self

    @property
    def values(self) -> Mapping[str, str] | None:
        return self.operation_context.values

    @property
    def config(self) -> AppConfig | None:
        return self.operation_context.config

    @property
    def finish_fields(self) -> dict[str, object]:
        return self.operation_context.finish_fields

    @property
    def error_lines(self) -> list[str]:
        return self.operation_context.error_lines

    @property
    def preflight_error(self) -> str | None:
        return self.operation_context.preflight_error

    @preflight_error.setter
    def preflight_error(self, value: str | None) -> None:
        self.operation_context.preflight_error = value

    @property
    def debug_stage(self) -> str | None:
        return self.operation_context.debug_stage

    @property
    def debug_fields(self) -> dict[str, object]:
        return self.operation_context.debug_fields

    @property
    def connection(self) -> SshConnection | None:
        return self.operation_context.connection

    @connection.setter
    def connection(self, value: SshConnection | None) -> None:
        self.operation_context.connection = value

    @property
    def probe_state(self) -> ProbedDeviceState | None:
        return self.operation_context.probe_state

    @probe_state.setter
    def probe_state(self, value: ProbedDeviceState | None) -> None:
        self.operation_context.probe_state = value

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        try:
            return self._exit(exc_type, exc, tb)
        finally:
            self._stdout_scope.close()

    def _exit(self, exc_type: object, exc: object, _tb: object) -> bool:
        if exc_type is KeyboardInterrupt and self.result != "cancelled":
            self.result = "cancelled"
            if not self.error_lines:
                self.set_error("Cancelled by user")
        elif isinstance(exc, (TransportError, ConfigError, DeviceError)):
            message = str(exc)
            display_message = message
            telemetry_message = message_with_exception_cause(message, exc)
            if isinstance(exc, TransportError) and is_ssh_timeout_error(exc):
                device_name = self.known_airport_display_name()
                slow_message = ssh_timeout_slow_device_message(device_name)
                telemetry_message = format_ssh_timeout_slow_device_error(exc, device_name=device_name)
                display_message = telemetry_message.replace(
                    slow_message,
                    color_red(slow_message),
                    1,
                )
            self.result = "failure"
            if telemetry_message and not self.error_lines:
                self.set_error(telemetry_message)
            self.finish(result=self.result, **self.finish_fields)
            raise SystemExit(display_message) from exc
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

    def to_operation_callbacks(self) -> OperationCallbacks:
        return OperationCallbacks(
            set_stage=self.set_stage,
            log=print,
            add_debug_fields=self.add_debug_fields,
            update_fields=self.update_fields,
            record_execution_measurement=self.record_execution_measurement,
            report_progress=self.print_progress,
        )

    def print_progress(self, stage: str, *, entries: int, now: Callable[[], float] = time.monotonic) -> None:
        # A terminal line every half minute is enough for a walk of many
        # minutes; a new stage (cleanup after copy) gets its first line at once.
        current = now()
        last = self._last_progress_print
        if last is not None and last[0] == stage and current - last[1] < CLI_PROGRESS_SECONDS:
            return
        self._last_progress_print = (stage, current)
        print(f"  {entries:,} files checked", flush=True)

    def update_fields(self, **fields: object) -> None:
        self.operation_context.update_fields(**fields)

    def _update_device_identity_fields(self, *, model: str | None, syap: str | None) -> None:
        self.update_fields(device_model=model, device_syap=syap)

    def _update_device_identity_from_probe_state(self, probe_state: ProbedDeviceState) -> None:
        probe = probe_state.probe_result
        self._update_device_identity_fields(
            model=probe.airport_model,
            syap=probe.airport_syap,
        )

    def start_optional_airport_identity_probe(self, connection: SshConnection | None = None) -> None:
        if self.finish_fields.get("device_model") or self.finish_fields.get("device_syap"):
            return
        if self._optional_airport_identity_thread is not None:
            return
        connection = connection or self.connection
        if connection is None:
            return

        def probe_identity() -> None:
            try:
                identity = probe_remote_airport_identity_conn(connection)
            except Exception:
                return
            self._optional_airport_identity = (identity.model, identity.syap)

        thread = threading.Thread(target=probe_identity, name=f"{self.command_name}-airport-identity", daemon=True)
        self._optional_airport_identity_thread = thread
        thread.start()

    def harvest_optional_airport_identity_probe(self, *, timeout_seconds: float = 0.0) -> None:
        thread = self._optional_airport_identity_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(0.0, timeout_seconds))
        if thread is not None and thread.is_alive():
            return
        if self._optional_airport_identity is None:
            self._optional_airport_identity_thread = None
            return
        model, syap = self._optional_airport_identity
        self._update_device_identity_fields(model=model, syap=syap)
        self._optional_airport_identity_thread = None

    def optional_airport_display_name(self, *, timeout_seconds: float = 0.0) -> str:
        self.harvest_optional_airport_identity_probe(timeout_seconds=timeout_seconds)
        model = self.finish_fields.get("device_model")
        syap = self.finish_fields.get("device_syap")
        return airport_exact_display_name_from_identity(
            model=model if isinstance(model, str) else None,
            syap=syap if isinstance(syap, str) else None,
        )

    def known_airport_display_name(self) -> str | None:
        self.harvest_optional_airport_identity_probe(timeout_seconds=0.0)
        model = self.finish_fields.get("device_model")
        syap = self.finish_fields.get("device_syap")
        if not isinstance(model, str) and not isinstance(syap, str):
            return None
        return airport_exact_display_name_from_identity(
            model=model if isinstance(model, str) else None,
            syap=syap if isinstance(syap, str) else None,
        )

    def set_stage(self, stage: str) -> None:
        self.operation_context.set_stage(stage)

    def add_debug_fields(self, **fields: object) -> None:
        self.operation_context.add_debug_fields(**fields)

    def record_execution_measurement(self, kind: str, **fields: object) -> None:
        self.operation_context.record_execution_measurement(kind, **fields)

    def set_error(self, message: str) -> None:
        self.operation_context.set_error(message)

    def build_error(self) -> str | None:
        return self.operation_context.build_error()

    def _password_provider(self) -> service_runtime.PasswordProvider | None:
        # Without a provider the services refuse a missing TC_PASSWORD instead of prompting.
        return None if cli_runtime.no_input_enabled(self.args) else cli_runtime.prompt_device_password

    def resolve_env_connection(
        self,
        *,
        required_keys: tuple[str, ...] = (),
        allow_empty_password: bool = False,
    ) -> SshConnection:
        if self.config is None:
            raise RuntimeError("CommandContext config is not set.")
        self.connection = service_runtime.resolve_env_connection(
            self.config,
            required_keys=required_keys,
            allow_empty_password=allow_empty_password,
            password_provider=self._password_provider(),
        )
        return self.connection

    def require_local_sshpass(self) -> bool:
        """Fail before touching the device when local sshpass is missing.

        Without this check a command found out partway through, after it had
        already probed the device or started its work.
        """
        self.set_stage("check_local_tools")
        if not local_transport.sshpass_missing():
            return True
        message = "local tool sshpass is missing; run `./tcapsule bootstrap` to install it"
        print(color_red(message))
        self.fail_with_error(message)
        return False

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
        if target.probe_state is not None:
            self.probe_state = target.probe_state
            self.compatibility = target.probe_state.compatibility
            self._update_device_identity_from_probe_state(target.probe_state)
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

    def resolve_validated_managed_target(self, *, profile: str, include_probe: bool = False) -> ManagedTargetState:
        if self.config is None:
            raise RuntimeError("CommandContext config is not set.")
        target = service_runtime.resolve_validated_managed_target(
            self.config,
            command_name=self.command_name,
            profile=profile,
            include_probe=include_probe,
            password_provider=self._password_provider(),
        )
        return self._apply_managed_target_state(target)

    def require_compatibility(self) -> DeviceCompatibility:
        if self.connection is None:
            raise RuntimeError("CommandContext connection is not set.")
        if self.probe_state is None:
            self.probe_state = service_runtime.probe_managed_connection_state(self.connection)
        if self.probe_state.compatibility is None:
            # Say why SSH did not log in (SSH off, device unreachable, password
            # rejected) rather than leave it to look like an unsupported device.
            raise service_runtime.probe_failure_error(self.probe_state.probe_result, self.connection.host)
        self.compatibility = self.probe_state.compatibility
        self._update_device_identity_from_probe_state(self.probe_state)
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
        self.harvest_optional_airport_identity_probe(timeout_seconds=OPTIONAL_IDENTITY_PROBE_FINISH_TIMEOUT_SECONDS)
        emit_fields = dict(self.finish_fields)
        emit_fields.update(fields)
        try:
            error = None if result == "success" else self.build_error()
        except Exception as exc:
            error = f"{self.command_name} failed, and debug context rendering also failed: {type(exc).__name__}: {exc}"
        if result != "success" and error is None:
            error = f"{self.command_name} failed without additional details."
        if self.args is None:
            params: Mapping[str, object] = {}
        elif isinstance(self.args, Mapping):
            params = self.args
        else:
            try:
                params = vars(self.args)
            except TypeError:
                params = {}
        details = telemetry_details_from_payload(self.command_name, params, emit_fields)
        self.telemetry_session.finish(
            result=result,
            error=error,
            stage=self.debug_stage,
            details=details,
            execution=self.operation_context.execution_telemetry(result=result),
            **emit_fields,
        )
