from __future__ import annotations

import os
import sys
import uuid

from timecapsulesmb.app.context import AppOperationContext
from timecapsulesmb.app.confirmations import build_confirmation, require_confirmation
from timecapsulesmb.app.contracts import configure_payload
from timecapsulesmb.core.config import (
    DEFAULTS,
    parse_bool,
    parse_env_file,
)
from timecapsulesmb.core.net import endpoint_host
from timecapsulesmb.core.paths import resolve_app_paths
from timecapsulesmb.core.smb_policy import validate_smb_protocol_options
from timecapsulesmb.device.probe import probe_connection_state
from timecapsulesmb.integrations.acp import ACPConnectionError, ACPError
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
    AIRPORT_ADMIN_PASSWORD_REJECTED_MESSAGE,
    ConfigureFlowError,
    ConfigureFlowHooks,
    ConfigureFlowRequest,
)
from timecapsulesmb.services.callbacks import OperationCallbacks
from timecapsulesmb.services.configure_target import resolve_configure_target


LOCAL_NETWORK_PREFLIGHT_PARAM_KEYS = (
    "macos_local_network_preflight_result",
    "macos_local_network_preflight_duration_ms",
    "macos_local_network_preflight_service",
    "macos_local_network_preflight_error",
)


def add_local_network_preflight_debug_fields(params: dict[str, object], context: AppOperationContext) -> None:
    fields = {
        key: params[key]
        for key in LOCAL_NETWORK_PREFLIGHT_PARAM_KEYS
        if key in params and params[key] is not None
    }
    if fields:
        context.add_debug_fields(**fields)


def local_network_preflight_denied(params: dict[str, object]) -> bool:
    return str(params.get("macos_local_network_preflight_result") or "").strip().lower() == "denied"


def is_macos_gui_local_network_privacy_signal(error: object) -> bool:
    if sys.platform != "darwin":
        return False
    if os.getenv("TCAPSULE_CLIENT") != "macos_gui":
        return False
    text = str(error)
    return "[Errno 65]" in text or "No route to host" in text


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
    add_local_network_preflight_debug_fields(params, context)
    if local_network_preflight_denied(params):
        context.stage("local_network_preflight")
        raise AppOperationError(
            "macOS is blocking TimeCapsuleSMB from accessing devices on your local network.",
            code="local_network_permission_denied",
        )

    context.stage("load_existing_config")
    app_paths = resolve_app_paths(config_path=config_path(params))
    env_path = app_paths.config_path
    existing = parse_env_file(env_path)
    configure_id = str(uuid.uuid4())
    ssh_opts = string_param(params, "ssh_opts", existing.get("TC_SSH_OPTS", DEFAULTS["TC_SSH_OPTS"]))
    try:
        selected_record = params.get("selected_record")
        target = resolve_configure_target(
            explicit_host=string_param(params, "host"),
            selected_record=selected_record if isinstance(selected_record, dict) else None,
            existing=existing,
            ssh_opts=ssh_opts,
        )
    except ValueError as exc:
        raise AppOperationError(str(exc), code="validation_failed") from exc
    host = target.host
    password = require_string_param(params, "password")
    if isinstance(selected_record, dict):
        context.add_debug_fields(selected_bonjour_record=selected_record)
    context.add_debug_fields(configure_target_source=target.source)
    any_protocol = bool_param(
        params,
        "any_protocol",
        parse_bool(existing.get("TC_ANY_PROTOCOL", DEFAULTS["TC_ANY_PROTOCOL"])),
    )
    require_smb_encryption = bool_param(
        params,
        "require_smb_encryption",
        parse_bool(existing.get("TC_REQUIRE_SMB_ENCRYPTION", DEFAULTS["TC_REQUIRE_SMB_ENCRYPTION"])),
    )
    force_disable_smb_signing_and_encryption = bool_param(
        params,
        "force_disable_smb_signing_and_encryption",
        parse_bool(
            existing.get(
                "TC_FORCE_DISABLE_SMB_SIGNING_AND_ENCRYPTION",
                DEFAULTS["TC_FORCE_DISABLE_SMB_SIGNING_AND_ENCRYPTION"],
            )
        ),
    )
    try:
        validate_smb_protocol_options(
            any_protocol=any_protocol,
            require_smb_encryption=require_smb_encryption,
            force_disable_smb_signing_and_encryption=force_disable_smb_signing_and_encryption,
        )
    except ValueError as exc:
        raise AppOperationError(str(exc), code="validation_failed") from exc

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
                discovered_airport_syap=target.discovered_airport_syap,
                enable_ssh=bool_param(params, "enable_ssh", True),
                ssh_wait_timeout=int_param(params, "ssh_wait_timeout", 180),
                internal_share_use_disk_root=bool_param(
                    params,
                    "internal_share_use_disk_root",
                    parse_bool(existing.get("TC_INTERNAL_SHARE_USE_DISK_ROOT", DEFAULTS["TC_INTERNAL_SHARE_USE_DISK_ROOT"])),
                ),
                smb_bind_lan_only=bool_param(
                    params,
                    "smb_bind_lan_only",
                    parse_bool(existing.get("TC_SMB_BIND_LAN_ONLY", DEFAULTS["TC_SMB_BIND_LAN_ONLY"])),
                ),
                smb_browse_compatibility=bool_param(
                    params,
                    "smb_browse_compatibility",
                    parse_bool(existing.get("TC_SMB_BROWSE_COMPATIBILITY", DEFAULTS["TC_SMB_BROWSE_COMPATIBILITY"])),
                ),
                mdns_advertise_afp=bool_param(
                    params,
                    "mdns_advertise_afp",
                    parse_bool(existing.get("TC_MDNS_ADVERTISE_AFP", DEFAULTS["TC_MDNS_ADVERTISE_AFP"])),
                ),
                any_protocol=any_protocol,
                require_smb_encryption=require_smb_encryption,
                force_disable_smb_signing_and_encryption=force_disable_smb_signing_and_encryption,
                fruit_metadata_netatalk=bool_param(
                    params,
                    "fruit_metadata_netatalk",
                    parse_bool(existing.get("TC_FRUIT_METADATA_NETATALK", DEFAULTS["TC_FRUIT_METADATA_NETATALK"])),
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
            raise AppOperationError(
                AIRPORT_ADMIN_PASSWORD_REJECTED_MESSAGE,
                code="auth_failed",
                debug=exc.debug,
            ) from exc
        if exc.code == "ssh_compatibility_failed":
            raise AppOperationError(str(exc), code="ssh_compatibility_failed") from exc
        if exc.code == "ssh_enable_timeout":
            raise AppOperationError(str(exc), code="ssh_enable_timeout") from exc
        if exc.code == "unsupported_device":
            raise AppOperationError(str(exc), code="unsupported_device") from exc
        raise AppOperationError(str(exc), code="remote_error") from exc
    except ValueError as exc:
        raise AppOperationError(str(exc), code="validation_failed") from exc
    except ACPConnectionError as exc:
        if context.current_stage == "acp_port_probe":
            if is_macos_gui_local_network_privacy_signal(exc):
                context.add_debug_fields(
                    macos_local_network_privacy_suspected=True,
                    macos_local_network_privacy_signal="errno65_no_route_to_host",
                )
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
