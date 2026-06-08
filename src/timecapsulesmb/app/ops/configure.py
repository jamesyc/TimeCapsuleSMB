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
from timecapsulesmb.core.net import endpoint_host
from timecapsulesmb.core.paths import resolve_app_paths
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
    ConfigureFlowError,
    ConfigureFlowHooks,
    ConfigureFlowRequest,
)
from timecapsulesmb.services.callbacks import OperationCallbacks


def selected_record_name(params: dict[str, object]) -> str:
    selected = params.get("selected_record")
    if not isinstance(selected, dict):
        return ""
    name = str(selected.get("name") or "").strip()
    return name


def require_enable_ssh_confirmation(params: dict[str, object], *, host: str) -> None:
    device_name = selected_record_name(params) or endpoint_host(host)
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
    try:
        host = configure_service.configure_ssh_target(
            string_param(params, "host") or selected_record_host(params) or existing.get("TC_HOST", ""),
            ssh_opts,
        )
    except ValueError as exc:
        raise AppOperationError(str(exc), code="validation_failed") from exc
    password = require_string_param(params, "password")
    selected_record = params.get("selected_record")
    if isinstance(selected_record, dict):
        context.add_debug_fields(selected_bonjour_record=selected_record)
    selected_props = selected_record_properties(params)

    def before_enable_ssh(_connection, _probed_state) -> None:
        context.stage("confirm_enable_ssh")
        require_enable_ssh_confirmation(params, host=host)

    def probe_for_context(connection):
        context.connection = connection
        probed_state = probe_connection_state(connection)
        context.apply_probe_state(probed_state)
        return probed_state

    def apply_probe_to_context(connection, probed_state) -> None:
        context.connection = connection
        context.apply_probe_state(probed_state)

    try:
        result = configure_service.run_configure_flow(
            ConfigureFlowRequest(
                existing=existing,
                env_path=env_path,
                host=host,
                password=password,
                ssh_opts=ssh_opts,
                configure_id=configure_id,
                persist_password=bool_param(params, "persist_password"),
                discovered_airport_syap=selected_props.get("syAP") or None,
                enable_ssh=bool_param(params, "enable_ssh", True),
                ssh_wait_timeout=int_param(params, "ssh_wait_timeout", 180),
                internal_share_use_disk_root=bool_param(
                    params,
                    "internal_share_use_disk_root",
                    parse_bool(existing.get("TC_INTERNAL_SHARE_USE_DISK_ROOT", DEFAULTS["TC_INTERNAL_SHARE_USE_DISK_ROOT"])),
                ),
                smb_browse_compatibility=bool_param(
                    params,
                    "smb_browse_compatibility",
                    parse_bool(existing.get("TC_SMB_BROWSE_COMPATIBILITY", DEFAULTS["TC_SMB_BROWSE_COMPATIBILITY"])),
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
                probe=probe_for_context,
            ),
            callbacks=OperationCallbacks(
                set_stage=context.stage,
                add_debug_fields=context.add_debug_fields,
                update_fields=context.update_fields,
                log=context.log,
            ),
            hooks=ConfigureFlowHooks(
                after_probe=apply_probe_to_context,
                before_enable_ssh=before_enable_ssh,
            ),
        )
    except ConfigureFlowError as exc:
        if exc.code == "auth_failed":
            raise AppOperationError(str(exc), code="auth_failed") from exc
        if exc.code == "unsupported_device":
            raise AppOperationError(str(exc), code="unsupported_device") from exc
        raise AppOperationError(str(exc), code="remote_error") from exc
    except ValueError as exc:
        raise AppOperationError(str(exc), code="validation_failed") from exc
    except ACPAuthError as exc:
        raise AppOperationError("The AirPort admin password did not work.", code="auth_failed", debug=str(exc)) from exc
    except ACPConnectionError as exc:
        if context.current_stage == "acp_port_probe":
            raise AppOperationError(
                f"No AirPort ACP service responded at this address: {exc}",
                code="remote_error",
            ) from exc
        raise AppOperationError(f"Failed to enable SSH via ACP: {exc}", code="remote_error") from exc
    except ACPError as exc:
        raise AppOperationError(f"Failed to enable SSH via ACP: {exc}", code="remote_error") from exc

    context.connection = result.connection
    context.apply_probe_state(result.probe_state)
    context.values = result.values
    return OperationResult(True, configure_payload(
        config_path=str(env_path),
        host=result.host,
        configure_id=configure_id,
        ssh_authenticated=True,
        device_syap=result.identity.syap,
        device_model=result.identity.model,
        compatibility=jsonable(result.compatibility) if result.compatibility is not None else None,
    ))
