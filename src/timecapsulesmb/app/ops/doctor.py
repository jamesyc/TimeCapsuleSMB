from __future__ import annotations

from timecapsulesmb.app.context import AppOperationContext
from timecapsulesmb.app.contracts import doctor_payload
from timecapsulesmb.app.ops.common import load_request_config, resolve_request_connection
from timecapsulesmb.checks.doctor import run_doctor_checks
from timecapsulesmb.checks.models import CheckResult
from timecapsulesmb.core.paths import resolve_app_paths
from timecapsulesmb.services.app import OperationResult, bool_param, config_path
from timecapsulesmb.services.doctor import build_doctor_error, doctor_status_counts


def doctor_operation(params: dict[str, object], context: AppOperationContext) -> OperationResult:
    config = load_request_config(params, context)
    app_paths = resolve_app_paths(config_path=config_path(params))
    skip_ssh = bool_param(params, "skip_ssh")
    skip_bonjour = bool_param(params, "skip_bonjour")
    skip_smb = bool_param(params, "skip_smb")
    startup_grace = bool_param(params, "startup_grace", default=True)
    context.update_fields(skip_ssh=skip_ssh, skip_bonjour=skip_bonjour, skip_smb=skip_smb, startup_grace=startup_grace)
    connection = None
    if not skip_ssh and config.has_value("TC_HOST"):
        connection = resolve_request_connection(config, context, allow_empty_password=True)
    debug_fields: dict[str, object] = {}

    def on_result(result: CheckResult) -> None:
        context.check(status=result.status, message=result.message, details=result.details)

    context.stage("run_checks")
    results, fatal = run_doctor_checks(
        config,
        repo_root=app_paths.distribution_root,
        connection=connection,
        skip_ssh=skip_ssh,
        skip_bonjour=skip_bonjour,
        skip_smb=skip_smb,
        startup_grace=startup_grace,
        on_result=on_result,
        debug_fields=debug_fields,
    )
    context.add_debug_fields(**debug_fields)
    status_counts = doctor_status_counts(results)
    context.update_fields(
        fatal=fatal,
        check_count=len(results),
        pass_count=status_counts["PASS"],
        warn_count=status_counts["WARN"],
        fail_count=status_counts["FAIL"],
        info_count=status_counts["INFO"],
    )
    error = build_doctor_error(results, debug_fields) if fatal else None
    if error:
        context.set_error(error)
    return OperationResult(
        not fatal,
        doctor_payload(fatal=fatal, results=results, error=error),
        diagnostic_error=context.build_error() if fatal else None,
    )
