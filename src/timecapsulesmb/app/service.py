from __future__ import annotations

import traceback
from collections.abc import Callable

from timecapsulesmb.app.events import EventSink, redact
from timecapsulesmb.app.operations import OPERATIONS
from timecapsulesmb.app.recovery import recovery_for
from timecapsulesmb.core.config import ConfigError
from timecapsulesmb.services.app import AppOperationError, OperationResult
from timecapsulesmb.transport.errors import TransportError


def _request_operation(request: dict[str, object]) -> str:
    return str(request.get("operation") or "")


def _request_params(request: dict[str, object]) -> object:
    if "params" not in request or request.get("params") is None:
        return {}
    return request.get("params")


def run_api_request(request: dict[str, object], sink: EventSink) -> int:
    request_id = request.get("request_id")
    if request_id is not None and str(request_id).strip():
        sink = sink.with_request_id(str(request_id))

    operation = _request_operation(request)
    params = _request_params(request)
    if not operation:
        sink.error(
            "api",
            "missing required field: operation",
            code="invalid_request",
            recovery=recovery_for("api", "invalid_request"),
        )
        return 1
    if not isinstance(params, dict):
        sink.error(
            operation,
            "params must be a JSON object",
            code="invalid_request",
            recovery=recovery_for(operation, "invalid_request"),
        )
        return 1
    handler: Callable[[dict[str, object], EventSink], OperationResult] | None = OPERATIONS.get(operation)
    if handler is None:
        sink.error(
            operation,
            f"unknown operation: {operation}",
            code="unknown_operation",
            debug={"known_operations": sorted(OPERATIONS)},
            recovery=recovery_for(operation, "unknown_operation"),
        )
        return 1
    try:
        result = handler(params, sink)
    except AppOperationError as exc:
        recovery = exc.recovery or recovery_for(operation, exc.code, stage=sink.current_stage(operation))
        sink.error(
            operation,
            str(exc),
            code=exc.code,
            debug=redact(exc.debug) if exc.debug is not None else None,
            recovery=recovery,
        )
        return 1
    except ConfigError as exc:
        sink.error(
            operation,
            str(exc),
            code="config_error",
            recovery=recovery_for(operation, "config_error", stage=sink.current_stage(operation)),
        )
        return 1
    except TransportError as exc:
        sink.error(
            operation,
            str(exc),
            code="remote_error",
            recovery=recovery_for(operation, "remote_error", stage=sink.current_stage(operation)),
        )
        return 1
    except (SystemExit, KeyboardInterrupt):
        raise
    except Exception as exc:
        sink.error(
            operation,
            f"{type(exc).__name__}: {exc}",
            code="operation_failed",
            debug={"traceback": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))},
            recovery=recovery_for(operation, "operation_failed", stage=sink.current_stage(operation)),
        )
        return 1
    sink.result(operation, ok=result.ok, payload=result.payload)
    return 0 if result.ok else 1
