from __future__ import annotations

import traceback
from collections.abc import Callable

from timecapsulesmb.app.context import AppOperationContext
from timecapsulesmb.app.events import EventSink, redact
from timecapsulesmb.app.ops import OPERATIONS, TELEMETRY_OPERATIONS
from timecapsulesmb.app.confirmations import AppConfirmationRequired
from timecapsulesmb.app.requests import parse_api_request
from timecapsulesmb.app.recovery import recovery_for
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
from timecapsulesmb.transport.errors import TransportError


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
        sink.error(
            operation,
            str(exc),
            code="remote_error",
            recovery=recovery_for(operation, "remote_error", stage=context.current_stage),
        )
        _finish_api_telemetry(
            telemetry_session,
            context,
            result="failure",
            error=context.diagnostic_error(str(exc)) or str(exc),
        )
        return 1
    except (SystemExit, KeyboardInterrupt):
        raise
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
    if operation not in TELEMETRY_OPERATIONS:
        return None
    try:
        requested_config_path = config_path(params)
        app_paths = resolve_app_paths(config_path=requested_config_path)
        ensure_install_id(app_paths.bootstrap_path)
        config = load_optional_env_config(env_path=requested_config_path)
        telemetry = TelemetryClient.from_config(config, bootstrap_path=app_paths.bootstrap_path)
        return OperationTelemetrySession(
            telemetry,
            operation,
            entrypoint="api",
            client=client_from_environment(entrypoint="api"),
            options=telemetry_options_from_params(params),
        )
    except Exception:
        return None


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
    )


def _payload_error(payload: object | None) -> object | None:
    if not isinstance(payload, dict):
        return "operation returned an unsuccessful result"
    return payload.get("error") or payload.get("summary") or "operation returned an unsuccessful result"
