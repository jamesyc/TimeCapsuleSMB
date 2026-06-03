from __future__ import annotations

from timecapsulesmb.app.context import AppOperationContext
from timecapsulesmb.app.contracts import reachability_payload
from timecapsulesmb.app.ops.common import load_optional_request_config
from timecapsulesmb.services.app import OperationResult
from timecapsulesmb.services.credentials import request_password
from timecapsulesmb.services.reachability import run_reachability


def reachability_operation(params: dict[str, object], context: AppOperationContext) -> OperationResult:
    config = load_optional_request_config(params, context)

    result = run_reachability(
        config,
        params,
        password=request_password(params),
        stage=context.stage,
    )
    for check in result.checks:
        details = {}
        if check.host is not None:
            details["host"] = check.host
        if check.detail is not None:
            details["detail"] = check.detail
        context.check(status=check.status, message=check.message, details=details)
    return OperationResult(True, reachability_payload(result))
