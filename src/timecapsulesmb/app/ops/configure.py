from __future__ import annotations

import uuid

from timecapsulesmb.app.context import AppOperationContext
from timecapsulesmb.app.confirmations import build_confirmation, require_confirmation
from timecapsulesmb.app.contracts import configure_payload
from timecapsulesmb.app.ops.discovery import selected_record_host, selected_record_properties
from timecapsulesmb.core.config import (
    DEFAULTS,
    parse_bool,
    parse_env_file,
)
from timecapsulesmb.core.net import canonical_ssh_target, extract_host
from timecapsulesmb.core.paths import resolve_app_paths
from timecapsulesmb.device.compat import render_compatibility_message
from timecapsulesmb.device.probe import probe_connection_state
from timecapsulesmb.integrations.acp import ACPAuthError, ACPConnectionError, ACPError
from timecapsulesmb.services.app import (
    AppOperationError,
    OperationResult,
    bool_param,
    config_path,
    int_param,
    jsonable,
    require_string_param,
    string_param,
)
from timecapsulesmb.services import configure as configure_service
from timecapsulesmb.services.configure import (
    build_configure_env_values,
    write_configure_env_file,
)
from timecapsulesmb.services.runtime import RuntimeOperationCallbacks, ssh_target_link_local_resolution_error
from timecapsulesmb.transport.ssh import SshConnection


def configure_ssh_target(value: str) -> str:
    try:
        return canonical_ssh_target(value)
    except ValueError as exc:
        raise AppOperationError(str(exc), code="validation_failed") from exc


def selected_record_name(params: dict[str, object]) -> str:
    selected = params.get("selected_record")
    if not isinstance(selected, dict):
        return ""
    name = str(selected.get("name") or "").strip()
    return name


def require_enable_ssh_confirmation(params: dict[str, object], *, host: str) -> None:
    device_name = selected_record_name(params) or extract_host(host)
    require_confirmation(
        params,
        build_confirmation(
            operation="configure",
            params=params,
            title="Enable SSH and reboot?",
            message=f"SSH is closed on {device_name}. Enable SSH using AirPort ACP and reboot this AirPort device?",
            action_title="Enable SSH and reboot",
            risk="reboot",
            summary="Enable SSH through AirPort ACP and reboot the AirPort device",
            context={
                "host": host,
                "device_name": device_name,
                "requires_reboot": True,
            },
            presentation_id="configure.enable_ssh_reboot",
            presentation_values={
                "device_name": device_name,
                "requires_reboot": True,
            },
        ),
    )


def configure_operation(params: dict[str, object], context: AppOperationContext) -> OperationResult:
    context.stage("load_existing_config")
    app_paths = resolve_app_paths(config_path=config_path(params))
    env_path = app_paths.config_path
    existing = parse_env_file(env_path)
    configure_id = str(uuid.uuid4())
    ssh_opts = string_param(params, "ssh_opts", existing.get("TC_SSH_OPTS", DEFAULTS["TC_SSH_OPTS"]))
    host = configure_ssh_target(string_param(params, "host") or selected_record_host(params) or existing.get("TC_HOST", ""))
    password = require_string_param(params, "password")
    selected_record = params.get("selected_record")
    if isinstance(selected_record, dict):
        context.add_debug_fields(selected_bonjour_record=selected_record)
    if not host:
        raise AppOperationError("missing required parameter: host", code="validation_failed")

    resolution_error = ssh_target_link_local_resolution_error(host, ssh_opts)
    if resolution_error is not None:
        raise AppOperationError(resolution_error, code="config_error")

    try:
        values = build_configure_env_values(
            existing,
            host=host,
            password=password,
            ssh_opts=ssh_opts,
            configure_id=configure_id,
            internal_share_use_disk_root=bool_param(
                params,
                "internal_share_use_disk_root",
                parse_bool(existing.get("TC_INTERNAL_SHARE_USE_DISK_ROOT", DEFAULTS["TC_INTERNAL_SHARE_USE_DISK_ROOT"])),
            ),
            any_protocol=bool_param(
                params,
                "any_protocol",
                parse_bool(existing.get("TC_ANY_PROTOCOL", DEFAULTS["TC_ANY_PROTOCOL"])),
            ),
            debug_logging=bool_param(
                params,
                "debug_logging",
                parse_bool(existing.get("TC_DEBUG_LOGGING", DEFAULTS["TC_DEBUG_LOGGING"])),
            ),
            ata_idle_seconds=params.get("ata_idle_seconds") if "ata_idle_seconds" in params else None,
            ata_standby=params.get("ata_standby") if "ata_standby" in params else None,
        )
    except ValueError as exc:
        raise AppOperationError(str(exc), code="validation_failed") from exc
    context.values = values

    context.stage("ssh_probe")
    connection = SshConnection(host, password, ssh_opts)
    context.connection = connection
    probed_state = probe_connection_state(connection)
    context.apply_probe_state(probed_state)
    probe = probed_state.probe_result

    if not probe.ssh_port_reachable:
        if not bool_param(params, "enable_ssh", True):
            raise AppOperationError("SSH is not reachable and enable_ssh is false.", code="remote_error")
        ssh_wait_timeout = int_param(params, "ssh_wait_timeout", 180)
        context.stage("confirm_enable_ssh")
        require_enable_ssh_confirmation(params, host=host)
        try:
            probed_state = configure_service.enable_ssh_and_reprobe(
                connection,
                timeout_seconds=ssh_wait_timeout,
                callbacks=RuntimeOperationCallbacks(
                    set_stage=context.stage,
                    add_debug_fields=context.add_debug_fields,
                    update_fields=context.update_fields,
                    log=context.log,
                ),
            )
        except ACPAuthError as exc:
            raise AppOperationError("The AirPort admin password did not work.", code="auth_failed", debug=str(exc)) from exc
        except ACPConnectionError as exc:
            if context.current_stage == "acp_identity_probe":
                raise AppOperationError(
                    f"No AirPort ACP service responded at this address: {exc}",
                    code="remote_error",
                ) from exc
            raise AppOperationError(f"Failed to enable SSH via ACP: {exc}", code="remote_error") from exc
        except ACPError as exc:
            if context.current_stage == "acp_identity_probe":
                raise AppOperationError(f"Failed to read AirPort identity via ACP: {exc}", code="remote_error") from exc
            raise AppOperationError(f"Failed to enable SSH via ACP: {exc}", code="remote_error") from exc

        if probed_state is None:
            raise AppOperationError("SSH did not open after enabling via ACP.", code="remote_error")
        context.apply_probe_state(probed_state)
        probe = probed_state.probe_result

    if not probe.ssh_authenticated:
        raise AppOperationError(
            probe.error or "The provided AirPort SSH target and password did not work.",
            code="auth_failed",
        )

    compatibility = probed_state.compatibility
    if compatibility is not None and not compatibility.supported:
        raise AppOperationError(render_compatibility_message(compatibility), code="unsupported_device")

    selected_props = selected_record_properties(params)
    observed_syap = None if compatibility is None else compatibility.exact_syap
    observed_model = None if compatibility is None else compatibility.exact_model
    if observed_syap is None:
        observed_syap = selected_props.get("syAP") or None
    context.update_fields(configure_id=configure_id, device_model=observed_model, device_syap=observed_syap)

    context.stage("write_env")
    env_path.parent.mkdir(parents=True, exist_ok=True)
    write_configure_env_file(
        env_path,
        values,
        persist_password=bool_param(params, "persist_password"),
    )
    return OperationResult(True, configure_payload(
        config_path=str(env_path),
        host=host,
        configure_id=configure_id,
        ssh_authenticated=True,
        device_syap=observed_syap,
        device_model=observed_model,
        compatibility=jsonable(compatibility) if compatibility is not None else None,
    ))
