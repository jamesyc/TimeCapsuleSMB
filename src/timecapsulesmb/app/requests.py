from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from timecapsulesmb.services.app import AppOperationError


@dataclass(frozen=True)
class ApiRequest:
    operation: str
    params: dict[str, object]
    request_id: str | None = None


def parse_api_request(request: Mapping[str, object]) -> ApiRequest:
    request_id = request.get("request_id")
    operation = str(request.get("operation") or "")
    if not operation:
        raise AppOperationError("missing required field: operation", code="invalid_request")

    raw_params = request.get("params", {})
    if raw_params is None:
        raw_params = {}
    if not isinstance(raw_params, dict):
        raise AppOperationError("params must be a JSON object", code="invalid_request")

    return ApiRequest(
        operation=operation,
        params=dict(raw_params),
        request_id=str(request_id) if request_id is not None and str(request_id).strip() else None,
    )
