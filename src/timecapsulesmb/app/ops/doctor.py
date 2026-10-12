from __future__ import annotations

from timecapsulesmb.app.context import AppOperationContext
from timecapsulesmb.app.contracts import doctor_payload
from timecapsulesmb.app.ops.common import load_request_config, resolve_request_connection
from timecapsulesmb.checks.doctor import run_doctor_checks
from timecapsulesmb.checks.models import CheckResult
from timecapsulesmb.core.paths import resolve_app_paths
from timecapsulesmb.services.app import OperationResult, bool_param, config_path
from timecapsulesmb.services.doctor import build_doctor_error, doctor_finished_fields, doctor_result_details
from timecapsulesmb.services.runtime import airport_identity_fields


def doctor_operation(params: dict[str, object], context: AppOperationContext) -> OperationResult:
    config = load_request_config(params, context)
    app_paths = resolve_app_paths(config_path=config_path(params))
    skip_ssh = bool_param(params, "skip_ssh")
    skip_bonjour = bool_param(params, "skip_bonjour")
    skip_smb = bool_param(params, "skip_smb")
    startup_grace = bool_param(params, "startup_grace", default=True)
    context.update_fields(skip_ssh=skip_ssh, skip_bonjour=skip_bonjour, skip_smb=skip_smb, startup_grace=startup_grace)
    local_network_permission = params.get("macos_local_network_preflight_result")
    connection = None
    if not skip_ssh and config.has_value("TC_HOST"):
        connection = resolve_request_connection(config, context, allow_empty_password=True)
    debug_fields: dict[str, object] = {}

    def on_result(result: CheckResult) -> None:
        context.check(status=result.status, message=result.message, details=doctor_result_details(result))

    context.stage("run_checks")
    results, fatal = run_doctor_checks(
        config,
        repo_root=app_paths.distribution_root,
        connection=connection,
        device_reading=context.reading_at(connection),
        skip_ssh=skip_ssh,
        skip_bonjour=skip_bonjour,
        skip_smb=skip_smb,
        startup_grace=startup_grace,
        local_network_permission=local_network_permission if isinstance(local_network_permission, str) else None,
        on_result=on_result,
        debug_fields=debug_fields,
    )
    airport_mac = debug_fields.pop("airport_mac", None)
    naming = debug_fields.get("runtime_naming_identity")
    naming = naming if isinstance(naming, dict) else {}
    finished = doctor_finished_fields(results, fatal, debug_fields)
    context.add_debug_fields(**debug_fields)
    context.update_fields(**finished, **airport_identity_fields(
        airport_mac, naming.get("system_name"), naming.get("system_dns_name"),
    ))
    error = build_doctor_error(results, debug_fields) if fatal else None
    if error:
        context.set_error(error)
    return OperationResult(
        not fatal,
        doctor_payload(fatal=fatal, results=results, error=error),
        diagnostic_error=context.build_error() if fatal else None,
    )
