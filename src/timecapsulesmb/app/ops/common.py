from __future__ import annotations

from dataclasses import replace

from timecapsulesmb.checks.network import saved_router_mac
from timecapsulesmb.app.context import AppOperationContext
from timecapsulesmb.core.config import ATA_IDLE_MAX_SECONDS, DEFAULTS, AppConfig, effective_smb_security, parse_bool, vfs_aio_fork_enabled_in
from timecapsulesmb.core.error_codes import ErrorCode
from timecapsulesmb.core.runtime_settings import ManagedSettings
from timecapsulesmb.services.app import AppOperationError, bool_param, config_path, int_param, optional_bool_param
from timecapsulesmb.services.credentials import overlay_request_airport_mac, overlay_request_credentials
from timecapsulesmb.services.runtime import (
    DeviceAccessError,
    ManagedTargetState,
    load_env_config,
    load_optional_env_config,
    resolve_env_connection,
    require_acpd_ready,
    require_ready_without_restart,
    resolve_validated_managed_target,
)
from timecapsulesmb.transport.ssh import SshConnection


LOCAL_NETWORK_PREFLIGHT_PARAM_KEYS = (
    "macos_local_network_preflight_result",
    "macos_local_network_preflight_duration_ms",
    "macos_local_network_preflight_service",
    "macos_local_network_preflight_error",
)


def optional_unsigned_int_override_param(params: dict[str, object], name: str) -> int | str | None:
    if name not in params or params.get(name) is None:
        return None
    value = params.get(name)
    if isinstance(value, str) and value.strip() == "":
        return ""
    return int_param(params, name, 0)


def _sent_bool_param(params: dict[str, object], name: str) -> bool | None:
    """A boolean the request sends, or None when it leaves it out. A null or
    empty value is refused, not taken as left out."""
    return bool_param(params, name) if name in params else None


def _ata_idle_seconds_param(params: dict[str, object]) -> int | None:
    """The idle timer the request sends, or None. Refuses one the device
    would run as 300 (config.ATA_IDLE_MAX_SECONDS)."""
    if params.get("ata_idle_seconds") is None:
        return None
    value = int_param(params, "ata_idle_seconds", int(DEFAULTS["TC_ATA_IDLE_SECONDS"]))
    if value > ATA_IDLE_MAX_SECONDS:
        raise AppOperationError(
            f"ata_idle_seconds must be {ATA_IDLE_MAX_SECONDS} or less", code=ErrorCode.VALIDATION_FAILED,
        )
    return value


def managed_setting_params(params: dict[str, object]) -> ManagedSettings:
    """The managed settings the request sends (the app sends the device
    profile's); None for one it leaves out. Checks their types and ranges
    before anything reads the .env or the device."""
    return ManagedSettings(
        # Debug logging has always taken null as left out.
        debug_logging=optional_bool_param(params, "debug_logging"),
        internal_share_use_disk_root=_sent_bool_param(params, "internal_share_use_disk_root"),
        smb_browse_compatibility=_sent_bool_param(params, "smb_browse_compatibility"),
        mdns_advertise_afp=_sent_bool_param(params, "mdns_advertise_afp"),
        any_protocol=_sent_bool_param(params, "any_protocol"),
        require_smb_signing=_sent_bool_param(params, "require_smb_signing"),
        require_smb_encryption=_sent_bool_param(params, "require_smb_encryption"),
        disable_smb_encryption=_sent_bool_param(params, "disable_smb_encryption"),
        fruit_metadata_netatalk=_sent_bool_param(params, "fruit_metadata_netatalk"),
        vfs_aio_fork_enabled=_sent_bool_param(params, "vfs_aio_fork_enabled"),
        ata_idle_seconds=_ata_idle_seconds_param(params),
        ata_standby=optional_unsigned_int_override_param(params, "ata_standby"),
        rsync_enabled=_sent_bool_param(params, "rsync_enabled"),
    )


def resolve_managed_settings(sent: ManagedSettings, config: AppConfig) -> ManagedSettings:
    """This Mac's managed settings as deploy writes them to the device and
    doctor compares them with it: each one sent, else the value saved in the
    .env. Debug logging and the disk timers stay None when not sent, which
    managed_flash_values reads from the .env itself; rsync, which the .env
    never holds, is off. Refuses a combination the device refuses."""

    def pick(value: bool | None, saved: bool) -> bool:
        return saved if value is None else value

    def saved(key: str) -> bool:
        return parse_bool(config.get(key, DEFAULTS[key]))

    try:
        smb = effective_smb_security(
            config,
            any_protocol=sent.any_protocol,
            require_smb_signing=sent.require_smb_signing,
            require_smb_encryption=sent.require_smb_encryption,
            disable_smb_encryption=sent.disable_smb_encryption,
        )
    except ValueError as exc:
        raise AppOperationError(str(exc), code=ErrorCode.VALIDATION_FAILED) from exc
    return replace(
        sent,
        internal_share_use_disk_root=pick(sent.internal_share_use_disk_root, saved("TC_INTERNAL_SHARE_USE_DISK_ROOT")),
        smb_browse_compatibility=pick(sent.smb_browse_compatibility, saved("TC_SMB_BROWSE_COMPATIBILITY")),
        mdns_advertise_afp=pick(sent.mdns_advertise_afp, saved("TC_MDNS_ADVERTISE_AFP")),
        any_protocol=smb.any_protocol,
        require_smb_signing=smb.require_smb_signing,
        require_smb_encryption=smb.require_smb_encryption,
        disable_smb_encryption=smb.disable_smb_encryption,
        fruit_metadata_netatalk=pick(sent.fruit_metadata_netatalk, saved("TC_FRUIT_METADATA_NETATALK")),
        vfs_aio_fork_enabled=pick(sent.vfs_aio_fork_enabled, vfs_aio_fork_enabled_in(config)),
        rsync_enabled=bool(sent.rsync_enabled),
    )


def refuse_if_local_network_denied(params: dict[str, object], context: AppOperationContext) -> None:
    """Records the app's Local Network preflight, and stops when macOS denied it."""
    fields = {
        key: params[key]
        for key in LOCAL_NETWORK_PREFLIGHT_PARAM_KEYS
        if key in params and params[key] is not None
    }
    if fields:
        context.add_debug_fields(**fields)
    if str(params.get("macos_local_network_preflight_result") or "").strip().lower() == "denied":
        context.stage("local_network_preflight")
        raise AppOperationError(
            "macOS is blocking TimeCapsuleSMB from accessing devices on your local network.",
            code=ErrorCode.LOCAL_NETWORK_PERMISSION_DENIED,
        )


def load_request_config(params: dict[str, object], context: AppOperationContext) -> AppConfig:
    # Finding the saved device runs inside load_config although it reads the
    # network: a stage of its own would need timeline strings in every locale.
    # It is cancellable like load_config, and slow only when the saved address
    # is out of date.
    context.stage("load_config")
    try:
        config = overlay_request_airport_mac(
            overlay_request_credentials(load_env_config(env_path=config_path(params)), params), params,
        )
        context.config = config
        context.diagnostics.follow_saved_device(context.to_operation_callbacks())
    except ValueError as exc:
        raise AppOperationError(str(exc), code=ErrorCode.VALIDATION_FAILED) from exc
    except DeviceAccessError as exc:
        raise AppOperationError(str(exc), code=exc.code) from exc
    # follow_saved_device only swaps the config for one at the device's new address.
    return context.config or config


def load_optional_request_config(params: dict[str, object], context: AppOperationContext) -> AppConfig:
    context.stage("load_config")
    config = overlay_request_credentials(load_optional_env_config(env_path=config_path(params)), params)
    context.config = config
    return config


def resolve_request_connection(
    config: AppConfig,
    context: AppOperationContext,
    *,
    allow_empty_password: bool = True,
) -> SshConnection:
    context.stage("resolve_connection")
    connection = resolve_env_connection(config, allow_empty_password=allow_empty_password)
    context.connection = connection
    return connection


def resolve_request_target(
    config: AppConfig,
    context: AppOperationContext,
    *,
    profile: str,
    include_probe: bool,
) -> ManagedTargetState:
    context.stage("resolve_managed_target")
    target = resolve_validated_managed_target(
        config,
        command_name=context.operation,
        profile=profile,
        include_probe=include_probe,
    )
    context.apply_managed_target(target)
    return target


def require_request_acpd_ready(
    context: AppOperationContext, connection: SshConnection, *, restarts: bool = True,
) -> None:
    """services.runtime.require_acpd_ready, raising its code as an AppOperationError."""
    try:
        require_acpd_ready(
            connection, context.to_operation_callbacks(), reading=context.reading_at(connection), restarts=restarts,
            router_mac=saved_router_mac(context.config),
        )
    except DeviceAccessError as exc:
        raise AppOperationError(str(exc), code=exc.code) from exc


def require_request_ready_without_restart(
    context: AppOperationContext, connection: SshConnection, *, disk_check: bool = True,
) -> None:
    """services.runtime.require_ready_without_restart, for work without a
    restart, raising its code as an AppOperationError."""
    try:
        require_ready_without_restart(
            connection, context.to_operation_callbacks(), router_mac=saved_router_mac(context.config),
            disk_check=disk_check,
        )
    except DeviceAccessError as exc:
        raise AppOperationError(str(exc), code=exc.code) from exc
