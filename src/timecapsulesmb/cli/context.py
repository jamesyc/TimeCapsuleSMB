from __future__ import annotations

import threading
import uuid
from collections.abc import Mapping
from typing import TYPE_CHECKING

from timecapsulesmb.cli import runtime
from timecapsulesmb.core.config import ConfigError, airport_exact_display_name_from_identity
from timecapsulesmb.core.errors import system_exit_message
from timecapsulesmb.device.compat import require_compatibility as require_device_compatibility
from timecapsulesmb.device.errors import DeviceError
from timecapsulesmb.device.probe import probe_connection_state, probe_remote_airport_identity_conn
from timecapsulesmb.device.storage import (
    mast_volumes_debug_summary,
    mounted_mast_volumes_conn,
    payload_candidate_checks_debug_summary,
    read_mast_volumes_conn,
    select_payload_home_with_diagnostics_conn,
    wait_for_mast_volumes_conn,
)
from timecapsulesmb.services.context import (
    COMMAND_FIELD_BLACKLIST,
    COMMAND_VALUE_BLACKLIST,
    OperationContext,
    render_operation_debug_lines,
)
from timecapsulesmb.telemetry import build_device_os_version
from timecapsulesmb.telemetry.operation import (
    OperationTelemetrySession,
    client_from_environment,
    telemetry_details_from_payload,
    telemetry_options_from_args,
)
from timecapsulesmb.transport.errors import TransportError

if TYPE_CHECKING:
    from timecapsulesmb.cli.runtime import ManagedTargetState
    from timecapsulesmb.core.config import AppConfig
    from timecapsulesmb.device.compat import DeviceCompatibility
    from timecapsulesmb.device.probe import ProbedDeviceState, RemoteInterfaceProbeResult
    from timecapsulesmb.device.storage import MaStDiscoveryResult, MaStVolume, PayloadHomeSelection
    from timecapsulesmb.telemetry import TelemetryClient
    from timecapsulesmb.transport.ssh import SshConnection


MAST_ACP_OUTPUT_DEBUG_LIMIT = 8192
OPTIONAL_IDENTITY_PROBE_FINISH_TIMEOUT_SECONDS = 0.1


def _mast_acp_output_debug_text(raw_output: str) -> str:
    if not raw_output:
        return "<empty>"
    if len(raw_output) <= MAST_ACP_OUTPUT_DEBUG_LIMIT:
        return raw_output
    omitted = len(raw_output) - MAST_ACP_OUTPUT_DEBUG_LIMIT
    return f"{raw_output[:MAST_ACP_OUTPUT_DEBUG_LIMIT]}...<truncated {omitted} chars>"


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
    return render_operation_debug_lines(
        operation_name=command_name,
        stage=stage,
        connection=connection,
        values=values,
        preflight_error=preflight_error,
        finish_fields=finish_fields,
        probe_state=probe_state,
        debug_fields=debug_fields,
        config=config,
    )


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
        self.interface_probe: RemoteInterfaceProbeResult | None = None
        self.compatibility: DeviceCompatibility | None = None
        self._optional_airport_identity_thread: threading.Thread | None = None
        self._optional_airport_identity: tuple[str | None, str | None] | None = None
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

    def set_stage(self, stage: str) -> None:
        self.operation_context.set_stage(stage)

    def add_debug_fields(self, **fields: object) -> None:
        self.operation_context.add_debug_fields(**fields)

    def set_error(self, message: str) -> None:
        self.operation_context.set_error(message)

    def build_error(self) -> str | None:
        return self.operation_context.build_error()

    def confirm_or_fail(
        self,
        prompt_text: str,
        *,
        default: bool,
        noninteractive_message: str,
        eof_default: bool | None = None,
        interrupt_default: bool | None = None,
    ) -> bool | None:
        try:
            return runtime.confirm(
                prompt_text,
                default=default,
                eof_default=eof_default,
                interrupt_default=interrupt_default,
                noninteractive_message=noninteractive_message,
            )
        except runtime.NonInteractivePromptError as exc:
            message = str(exc)
            print(message)
            self.fail_with_error(message)
            return None

    def _storage_connection(self, connection: SshConnection | None) -> SshConnection:
        if connection is not None:
            return connection
        if self.connection is None:
            raise RuntimeError("CommandContext connection is not set.")
        return self.connection

    def read_mast_volumes(
        self,
        connection: SshConnection | None = None,
        *,
        stage: str = "read_mast",
    ) -> tuple[MaStVolume, ...]:
        connection = self._storage_connection(connection)
        self.set_stage(stage)
        volumes = read_mast_volumes_conn(connection)
        self.add_debug_fields(
            mast_volume_count=len(volumes),
            mast_candidates=mast_volumes_debug_summary(volumes),
        )
        return volumes

    def mount_mast_volumes(
        self,
        connection: SshConnection | None = None,
        *,
        wait_seconds: int,
        read_stage: str = "read_mast",
        mount_stage: str = "mount_mast_volumes",
    ) -> tuple[MaStVolume, ...]:
        connection = self._storage_connection(connection)
        mast_volumes = self.read_mast_volumes(connection, stage=read_stage)
        self.set_stage(mount_stage)
        mounted_volumes = mounted_mast_volumes_conn(
            connection,
            mast_volumes,
            wait_seconds=wait_seconds,
        )
        self.add_debug_fields(
            mast_mounted_volume_count=len(mounted_volumes),
            mast_mounted_candidates=mast_volumes_debug_summary(mounted_volumes),
        )
        return mounted_volumes

    def wait_for_mast_volumes(
        self,
        connection: SshConnection | None = None,
        *,
        attempts: int,
        delay_seconds: int,
        stage: str = "read_mast",
    ) -> MaStDiscoveryResult:
        connection = self._storage_connection(connection)
        self.set_stage(stage)
        mast_discovery = wait_for_mast_volumes_conn(
            connection,
            attempts=attempts,
            delay_seconds=delay_seconds,
        )
        mast_volumes = mast_discovery.volumes
        fields: dict[str, object] = {
            "mast_read_attempts": mast_discovery.attempts,
            "mast_volume_count": len(mast_volumes),
            "mast_candidates": mast_volumes_debug_summary(mast_volumes),
        }
        if not mast_volumes:
            fields["mast_acp_output_chars"] = len(mast_discovery.raw_output)
            fields["mast_acp_output"] = _mast_acp_output_debug_text(mast_discovery.raw_output)
        self.add_debug_fields(**fields)
        return mast_discovery

    def select_payload_home(
        self,
        connection: SshConnection | None,
        mast_volumes: tuple[MaStVolume, ...],
        payload_dir_name: str,
        *,
        wait_seconds: int,
        stage: str = "select_payload_home",
    ) -> PayloadHomeSelection:
        connection = self._storage_connection(connection)
        self.set_stage(stage)
        selection = select_payload_home_with_diagnostics_conn(
            connection,
            mast_volumes,
            payload_dir_name,
            wait_seconds=wait_seconds,
        )
        self.add_debug_fields(mast_candidate_checks=payload_candidate_checks_debug_summary(selection.checks))
        return selection

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
        if self.probe_state is None:
            self.probe_state = probe_connection_state(self.connection)
        self.compatibility = require_device_compatibility(
            self.probe_state.compatibility,
            fallback_error=self.probe_state.probe_result.error or "Failed to determine remote device OS compatibility.",
        )
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
            **emit_fields,
        )
