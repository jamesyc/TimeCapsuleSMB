from __future__ import annotations

from timecapsulesmb.app.context import AppOperationContext
from timecapsulesmb.app.contracts import reachability_payload
from timecapsulesmb.services.app import OperationResult, config_path
from timecapsulesmb.services.credentials import overlay_request_credentials, request_password
from timecapsulesmb.services.reachability import run_reachability
from timecapsulesmb.services.runtime import load_optional_env_config


def reachability_operation(params: dict[str, object], context: AppOperationContext) -> OperationResult:
    context.stage("load_config")
    config = load_optional_env_config(env_path=config_path(params))
    config = overlay_request_credentials(config, params)
    context.config = config

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
