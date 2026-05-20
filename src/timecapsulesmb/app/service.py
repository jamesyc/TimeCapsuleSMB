from __future__ import annotations

from collections.abc import Callable

from timecapsulesmb.app.events import EventSink, redact
from timecapsulesmb.app.operations import (
    OPERATIONS,
    AppOperationError,
    OperationResult,
    activate_operation,
    configure_operation,
    deploy_operation,
    discover_operation,
    doctor_operation,
    fsck_operation,
    paths_operation,
    repair_xattrs_operation,
    uninstall_operation,
    validate_install_operation,
)
from timecapsulesmb.core.config import ConfigError
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
        sink.error("api", "missing required field: operation", code="invalid_request")
        return 1
    if not isinstance(params, dict):
        sink.error(operation, "params must be a JSON object", code="invalid_request")
        return 1
    handler: Callable[[dict[str, object], EventSink], OperationResult] | None = OPERATIONS.get(operation)
    if handler is None:
        sink.error(operation, f"unknown operation: {operation}", code="unknown_operation", debug={"known_operations": sorted(OPERATIONS)})
        return 1
    try:
        result = handler(params, sink)
    except AppOperationError as exc:
        sink.error(operation, str(exc), code=exc.code, debug=redact(exc.debug) if exc.debug is not None else None)
        return 1
    except ConfigError as exc:
        sink.error(operation, str(exc), code="config_error")
        return 1
    except TransportError as exc:
        sink.error(operation, str(exc), code="remote_error")
        return 1
    except (SystemExit, KeyboardInterrupt):
        raise
    except Exception as exc:
        sink.error(operation, f"{type(exc).__name__}: {exc}", code="operation_failed")
        return 1
    sink.result(operation, ok=result.ok, payload=result.payload)
    return 0 if result.ok else 1
