from __future__ import annotations

from timecapsulesmb.app.contracts import doctor_payload
from timecapsulesmb.app.events import EventSink
from timecapsulesmb.checks.doctor import run_doctor_checks
from timecapsulesmb.checks.models import CheckResult
from timecapsulesmb.core.paths import resolve_app_paths
from timecapsulesmb.services.app import OperationResult, bool_param, config_path
from timecapsulesmb.services.credentials import overlay_request_credentials
from timecapsulesmb.services.doctor import build_doctor_error
from timecapsulesmb.services.runtime import load_env_config, resolve_env_connection


def doctor_operation(params: dict[str, object], sink: EventSink) -> OperationResult:
    operation = "doctor"
    sink.stage(operation, "load_config")
    config = overlay_request_credentials(load_env_config(env_path=config_path(params)), params)
    app_paths = resolve_app_paths(config_path=config_path(params))
    connection = None
    if not bool_param(params, "skip_ssh") and config.has_value("TC_HOST"):
        sink.stage(operation, "resolve_connection")
        connection = resolve_env_connection(config, allow_empty_password=True)
    debug_fields: dict[str, object] = {}

    def on_result(result: CheckResult) -> None:
        sink.check(operation, status=result.status, message=result.message, details=result.details)

    sink.stage(operation, "run_checks")
    results, fatal = run_doctor_checks(
        config,
        repo_root=app_paths.distribution_root,
        connection=connection,
        skip_ssh=bool_param(params, "skip_ssh"),
        skip_bonjour=bool_param(params, "skip_bonjour"),
        skip_smb=bool_param(params, "skip_smb"),
        on_result=on_result,
        debug_fields=debug_fields,
    )
    error = build_doctor_error(results, debug_fields) if fatal else None
    return OperationResult(not fatal, doctor_payload(fatal=fatal, results=results, error=error))
