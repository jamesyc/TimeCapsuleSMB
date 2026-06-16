from __future__ import annotations

import traceback
from collections.abc import Callable

from timecapsulesmb.app.context import AppOperationContext
from timecapsulesmb.app.events import EventSink, redact
from timecapsulesmb.app.ops import OPERATIONS, TELEMETRY_OPERATIONS
from timecapsulesmb.app.confirmations import AppConfirmationRequired
from timecapsulesmb.app.requests import parse_api_request
from timecapsulesmb.app.recovery import recovery_for, ssh_timeout_slow_device_recovery
from timecapsulesmb.core.errors import system_exit_message
from timecapsulesmb.core.config import ConfigError
from timecapsulesmb.core.paths import resolve_app_paths
from timecapsulesmb.identity import ensure_install_id
from timecapsulesmb.services.app import AppOperationError, OperationResult, config_path
from timecapsulesmb.services.runtime import load_optional_env_config
from timecapsulesmb.telemetry import TelemetryClient
from timecapsulesmb.telemetry.operation import (
    OperationTelemetrySession,
    client_from_environment,
    confirmation_details,
    telemetry_details_from_payload,
    telemetry_options_from_params,
)
from timecapsulesmb.transport.errors import is_ssh_timeout_error, TransportError


def run_api_request(request: dict[str, object], sink: EventSink) -> int:
    try:
        api_request = parse_api_request(request)
    except AppOperationError as exc:
        sink.error(
            "api",
            str(exc),
            code=exc.code,
            recovery=recovery_for("api", "invalid_request"),
        )
        return 1

    if api_request.request_id:
        sink = sink.with_request_id(api_request.request_id)

    operation = api_request.operation
    params = api_request.params
    handler: Callable[[dict[str, object], AppOperationContext], OperationResult] | None = OPERATIONS.get(operation)
    if handler is None:
        sink.error(
            operation,
            f"unknown operation: {operation}",
            code="unknown_operation",
            debug={"known_operations": sorted(OPERATIONS)},
            recovery=recovery_for(operation, "unknown_operation"),
        )
        return 1
    telemetry_session = _api_telemetry_session(operation, params)
    if telemetry_session is not None:
        telemetry_session.start()
    context = AppOperationContext(operation, sink)
    try:
        result = handler(params, context)
    except AppConfirmationRequired as exc:
        sink.error(
            operation,
            str(exc),
            code=exc.code,
            details=exc.confirmation.to_jsonable(),
            recovery=recovery_for(operation, exc.code, stage=context.current_stage),
        )
        _finish_api_telemetry(
            telemetry_session,
            context,
            result="confirmation_required",
            details=confirmation_details(exc.confirmation),
            risk=exc.confirmation.risk,
        )
        return 1
    except AppOperationError as exc:
        recovery = exc.recovery or recovery_for(operation, exc.code, stage=context.current_stage)
        sink.error(
            operation,
            str(exc),
            code=exc.code,
            debug=redact(exc.debug) if exc.debug is not None else None,
            recovery=recovery,
        )
        _finish_api_telemetry(
            telemetry_session,
            context,
            result="failure",
            error=context.diagnostic_error(str(exc)) or str(exc),
        )
        return 1
    except ConfigError as exc:
        sink.error(
            operation,
            str(exc),
            code="config_error",
            recovery=recovery_for(operation, "config_error", stage=context.current_stage),
        )
        _finish_api_telemetry(
            telemetry_session,
            context,
            result="failure",
            error=context.diagnostic_error(str(exc)) or str(exc),
        )
        return 1
    except TransportError as exc:
        device_name = context.known_airport_display_name()
        recovery = (
            ssh_timeout_slow_device_recovery(device_name=device_name)
            if is_ssh_timeout_error(exc)
            else recovery_for(operation, "remote_error", stage=context.current_stage)
        )
        sink.error(
            operation,
            str(exc),
            code="remote_error",
            recovery=recovery,
        )
        _finish_api_telemetry(
            telemetry_session,
            context,
            result="failure",
            error=context.diagnostic_error(str(exc)) or str(exc),
        )
        return 1
    except KeyboardInterrupt:
        message = "Operation cancelled."
        sink.error(
            operation,
            message,
            code="cancelled",
            recovery=recovery_for(operation, "cancelled", stage=context.current_stage),
        )
        _finish_api_telemetry(
            telemetry_session,
            context,
            result="cancelled",
            error=context.diagnostic_error("Cancelled by user") or "Cancelled by user",
        )
        return 130
    except SystemExit as exc:
        message = system_exit_message(exc)
        result = "success" if message in {"0", "None", ""} else "failure"
        if result == "success":
            context.emit_result(ok=True, payload={"summary": "Operation exited."})
            _finish_api_telemetry(
                telemetry_session,
                context,
                result="success",
                details={"summary": "Operation exited."},
            )
            return 0
        error = message or "Operation exited before completion"
        sink.error(
            operation,
            error,
            code="operation_failed",
            recovery=recovery_for(operation, "operation_failed", stage=context.current_stage),
        )
        _finish_api_telemetry(
            telemetry_session,
            context,
            result="failure",
            error=context.diagnostic_error(error) or error,
        )
        return 1
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        sink.error(
            operation,
            message,
            code="operation_failed",
            debug={"traceback": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))},
            recovery=recovery_for(operation, "operation_failed", stage=context.current_stage),
        )
        _finish_api_telemetry(
            telemetry_session,
            context,
            result="failure",
            error=context.diagnostic_error(message) or message,
        )
        return 1
    context.emit_result(ok=result.ok, payload=result.payload)
    payload_error = _payload_error(result.payload) if not result.ok else None
    _finish_api_telemetry(
        telemetry_session,
        context,
        result="success" if result.ok else "failure",
        error=(result.diagnostic_error or context.diagnostic_error(payload_error) or payload_error) if not result.ok else None,
        details=telemetry_details_from_payload(operation, params, result.payload),
    )
    return 0 if result.ok else 1


def _api_telemetry_session(operation: str, params: dict[str, object]) -> OperationTelemetrySession | None:
    if not _should_emit_api_telemetry(operation, params):
        return None
    try:
        requested_config_path = config_path(params)
        app_paths = resolve_app_paths(config_path=requested_config_path)
        ensure_install_id(app_paths.bootstrap_path)
        config = load_optional_env_config(env_path=requested_config_path)
        telemetry = TelemetryClient.from_config(
            config,
            bootstrap_path=app_paths.bootstrap_path,
            nbns_enabled=_nbns_enabled_for_telemetry(operation, params),
        )
        return OperationTelemetrySession(
            telemetry,
            operation,
            entrypoint="api",
            client=client_from_environment(entrypoint="api"),
            options=telemetry_options_from_params(params),
        )
    except Exception:
        return None


def _should_emit_api_telemetry(operation: str, params: dict[str, object]) -> bool:
    if operation not in TELEMETRY_OPERATIONS:
        return False
    if operation == "set-ssh":
        action = str(params.get("action") or "status").strip().lower() or "status"
        return action != "status"
    return True


def _finish_api_telemetry(
    session: OperationTelemetrySession | None,
    context: AppOperationContext,
    *,
    result: str,
    error: object | None = None,
    details: dict[str, object] | None = None,
    risk: str | None = None,
) -> None:
    if session is None:
        return
    session.finish(
        result=result,
        error=error,
        stage=context.current_stage,
        risk=risk or context.current_risk,
        details=details,
        execution=context.execution_telemetry(result=result),
        **context.finish_fields,
    )


def _payload_error(payload: object | None) -> object | None:
    if not isinstance(payload, dict):
        return "operation returned an unsuccessful result"
    return payload.get("error") or payload.get("summary") or "operation returned an unsuccessful result"


def _nbns_enabled_for_telemetry(operation: str, params: dict[str, object]) -> bool | None:
    if operation != "deploy":
        return None
    value = params.get("nbns_enabled", True)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "y"}:
            return True
        if normalized in {"0", "false", "no", "n"}:
            return False
    return None
