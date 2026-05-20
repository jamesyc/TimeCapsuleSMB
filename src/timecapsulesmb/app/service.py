from __future__ import annotations

import traceback
from collections.abc import Callable

from timecapsulesmb.app.events import EventSink, redact
from timecapsulesmb.app.ops import OPERATIONS
from timecapsulesmb.app.confirmations import AppConfirmationRequired
from timecapsulesmb.app.requests import parse_api_request
from timecapsulesmb.app.recovery import recovery_for
from timecapsulesmb.core.config import ConfigError
from timecapsulesmb.services.app import AppOperationError, OperationResult
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
    except AppConfirmationRequired as exc:
        sink.error(
            operation,
            str(exc),
            code=exc.code,
            details=exc.confirmation.to_jsonable(),
            recovery=recovery_for(operation, exc.code, stage=sink.current_stage(operation)),
        )
        return 1
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
