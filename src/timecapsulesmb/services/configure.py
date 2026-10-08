from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping

from timecapsulesmb.configure_defaults import existing_config_value_or_default, validated_value_or_empty
from timecapsulesmb.core.config import (
    AIRPORT_SYAP_TO_MODEL,
    DEFAULTS,
    CONFIG_VALIDATORS,
    parse_bool,
    preserved_env_file_values,
    REMOVED_ENV_FILE_KEYS,
    write_env_file,
)
from timecapsulesmb.core.net import canonical_ssh_target, endpoint_host
from timecapsulesmb.core.smb_policy import validate_smb_protocol_options
from timecapsulesmb.device.compat import (
    DeviceCompatibility,
    airport_syap_supported,
    render_compatibility_message,
    unsupported_syap_message,
)
from timecapsulesmb.device.probe import (
    ProbedDeviceState,
    SshAccessStatus,
    password_check_fields,
    read_admin_password,
    probe_connection_state,
)
from timecapsulesmb.discovery.bonjour import BonjourResolvedService
from timecapsulesmb.integrations.acp import ACPAuthError, ACPError
from timecapsulesmb.services.acp_ssh import SSH_ENABLE_TIMEOUT_MESSAGE, AirportIdentityMismatchError, enable_ssh_with_port_preflight
from timecapsulesmb.services.reboot import RebootFlowError, reboot_device
from timecapsulesmb.services.callbacks import OperationCallbacks
from timecapsulesmb.services.runtime import AIRPORT_ADMIN_PASSWORD_REJECTED_MESSAGE, PROBE_STATUS_ERROR_CODES
from timecapsulesmb.transport.ssh import SshConnection


SSH_ONLY_PASSWORD_DEBUG = "SSH accepted the password, but it is not the device's AirPort admin password (syPW)."
# The stage configure fails in when the selected Bonjour record's syAP names an
# unsupported model. The CLI asks for another device before it gets this far.
CHECK_DEVICE_MODEL_STAGE = "check_device_model"


class ConfigureFlowError(Exception):
    def __init__(
        self,
        message: str,
        *,
        code: str = "configure_failed",
        debug: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.debug = debug


@dataclass(frozen=True)
class ObservedDeviceIdentity:
    syap: str | None
    syap_source: str | None
    model: str | None


@dataclass(frozen=True)
class ConfigureFlowRequest:
    existing: dict[str, str]
    env_path: Path
    host: str
    password: str
    ssh_opts: str
    configure_id: str
    persist_password: bool
    discovered_airport_syap: str | None = None
    # The syAP of the Bonjour record the host came from. Unlike
    # discovered_airport_syap, it is unset when the user typed another host, so
    # it can reject this device before ACP enables SSH and reboots it.
    selected_record_airport_syap: str | None = None
    # The Bonjour record the host came from, for ACP probe telemetry.
    selected_record: BonjourResolvedService | None = None
    enable_ssh: bool = True
    internal_share_use_disk_root: bool | None = None
    smb_browse_compatibility: bool | None = None
    mdns_advertise_afp: bool | None = None
    any_protocol: bool | None = None
    require_smb_encryption: bool | None = None
    force_disable_smb_signing_and_encryption: bool | None = None
    fruit_metadata_netatalk: bool | None = None
    vfs_aio_fork_enabled: bool | None = None
    debug_logging: bool | None = None
    ata_idle_seconds: object | None = None
    ata_standby: object | None = None
    probe: Callable[[SshConnection], ProbedDeviceState] | None = None
    write_env: Callable[[Path, Mapping[str, str]], None] | None = None


@dataclass(frozen=True)
class ConfigureFlowHooks:
    after_probe: Callable[[SshConnection, ProbedDeviceState], None] | None = None
    before_enable_ssh: Callable[[SshConnection, ProbedDeviceState], None] | None = None
    save_without_authentication: Callable[[ProbedDeviceState], bool] | None = None


@dataclass(frozen=True)
class ConfigureFlowResult:
    values: dict[str, str]
    host: str
    configure_id: str
    connection: SshConnection
    probe_state: ProbedDeviceState
    compatibility: DeviceCompatibility | None
    identity: ObservedDeviceIdentity
    airport_mac: str | None = None


def configure_ssh_target(
    value: str,
    ssh_opts: str,
    *,
    label: str = "Device SSH target",
    validate_config_value: bool = False,
) -> str:
    target = canonical_ssh_target(value)
    if validate_config_value:
        validation_error = CONFIG_VALIDATORS["TC_HOST"](target, label)
        if validation_error is not None:
            raise ValueError(validation_error)
    opts_error = CONFIG_VALIDATORS["TC_SSH_OPTS"](ssh_opts, "TC_SSH_OPTS")
    if opts_error is not None:
        raise ValueError(opts_error)
    return target


def enable_ssh_and_reprobe(
    connection: SshConnection,
    *,
    callbacks: OperationCallbacks | None = None,
    probe: Callable[[SshConnection], ProbedDeviceState] | None = None,
    record: BonjourResolvedService | None = None,
) -> tuple[SshConnection, ProbedDeviceState | None]:
    """Turn SSH on through ACP, reboot, and probe again.

    Returns the connection to the address SSH was turned on at, which moves
    when the device is found at a new address by its AirPort MAC, and the new
    probe, or None when SSH did not open in time.
    """
    callbacks = callbacks or OperationCallbacks()
    if probe is None:
        probe = probe_connection_state
    callbacks.debug(
        configure_acp_enable_attempted=True,
        ssh_initially_reachable=False,
    )
    callbacks.message("\nSSH is not reachable. Attempting to enable SSH on the device...")
    try:
        host = enable_ssh_with_port_preflight(
            endpoint_host(connection.host),
            connection.password,
            callbacks=callbacks,
            record=record,
        )
    except ACPAuthError:
        callbacks.debug(
            configure_acp_enable_succeeded=False,
            configure_retry_reason="acp_authentication_failed",
        )
        raise
    except ACPError:
        callbacks.debug(configure_acp_enable_succeeded=False)
        raise

    callbacks.debug(configure_acp_enable_succeeded=True)
    if host != endpoint_host(connection.host):
        connection = replace(connection, host=canonical_ssh_target(host))
    try:
        reboot_device(host, connection.password, wait=True, callbacks=callbacks, up_timeout_message=SSH_ENABLE_TIMEOUT_MESSAGE)
    except RebootFlowError as exc:
        if exc.code != "reboot_not_finished":
            raise
        callbacks.update(ssh_final_reachable=False)
        return connection, None

    callbacks.update(ssh_final_reachable=True)
    callbacks.stage("ssh_probe_after_acp")
    return connection, probe(connection)


def observed_device_identity(
    compatibility: DeviceCompatibility | None,
    *,
    discovered_airport_syap: str | None = None,
) -> ObservedDeviceIdentity:
    syap_source: str | None = "probed"
    syap = None if compatibility is None else compatibility.exact_syap
    if syap is None:
        syap = validated_value_or_empty(
            "TC_AIRPORT_SYAP",
            discovered_airport_syap or "",
            "Airport Utility syAP code",
        ) or None
        syap_source = "discovered" if syap is not None else None

    model = None if compatibility is None else compatibility.exact_model
    if model is None and syap is not None:
        model = AIRPORT_SYAP_TO_MODEL.get(syap)

    return ObservedDeviceIdentity(
        syap=syap,
        syap_source=syap_source,
        model=model,
    )


def run_configure_flow(
    request: ConfigureFlowRequest,
    *,
    callbacks: OperationCallbacks | None = None,
    hooks: ConfigureFlowHooks | None = None,
) -> ConfigureFlowResult:
    callbacks = callbacks or OperationCallbacks()
    hooks = hooks or ConfigureFlowHooks()

    if airport_syap_supported(request.selected_record_airport_syap) is False:
        callbacks.stage(CHECK_DEVICE_MODEL_STAGE)
        callbacks.debug(
            configure_failure_reason="unsupported_device",
            discovered_airport_syap=request.selected_record_airport_syap,
        )
        raise ConfigureFlowError(
            unsupported_syap_message(request.selected_record_airport_syap or ""),
            code="unsupported_device",
        )

    values = build_configure_env_values(
        request.existing,
        host=request.host,
        password=request.password,
        ssh_opts=request.ssh_opts,
        configure_id=request.configure_id,
        internal_share_use_disk_root=request.internal_share_use_disk_root,
        smb_browse_compatibility=request.smb_browse_compatibility,
        mdns_advertise_afp=request.mdns_advertise_afp,
        any_protocol=request.any_protocol,
        require_smb_encryption=request.require_smb_encryption,
        force_disable_smb_signing_and_encryption=request.force_disable_smb_signing_and_encryption,
        fruit_metadata_netatalk=request.fruit_metadata_netatalk,
        vfs_aio_fork_enabled=request.vfs_aio_fork_enabled,
        debug_logging=request.debug_logging,
        ata_idle_seconds=request.ata_idle_seconds,
        ata_standby=request.ata_standby,
    )

    callbacks.stage("ssh_probe")
    connection = SshConnection(request.host, request.password, request.ssh_opts)
    probe_connection = request.probe or probe_connection_state
    probed_state = probe_connection(connection)
    if hooks.after_probe is not None:
        hooks.after_probe(connection, probed_state)
    probe = probed_state.probe_result

    if not probe.ssh_port_reachable:
        if not request.enable_ssh:
            raise ConfigureFlowError("SSH is not reachable and enable_ssh is false.", code="ssh_unreachable")
        if hooks.before_enable_ssh is not None:
            hooks.before_enable_ssh(connection, probed_state)
        try:
            connection, probed_state = enable_ssh_and_reprobe(
                connection,
                callbacks=callbacks,
                probe=probe_connection,
                record=request.selected_record,
            )
        except ACPAuthError as exc:
            raise ConfigureFlowError(
                AIRPORT_ADMIN_PASSWORD_REJECTED_MESSAGE,
                code="auth_failed",
                debug=str(exc),
            ) from exc
        except AirportIdentityMismatchError as exc:
            raise ConfigureFlowError(str(exc), code="device_identity_mismatch") from exc
        except RebootFlowError as exc:
            raise ConfigureFlowError(str(exc), code=exc.code) from exc
        if probed_state is None:
            raise ConfigureFlowError(SSH_ENABLE_TIMEOUT_MESSAGE, code="ssh_enable_timeout")
        # The device answered at a new address: that is the one to save.
        values["TC_HOST"] = connection.host
        if hooks.after_probe is not None:
            hooks.after_probe(connection, probed_state)
        probe = probed_state.probe_result
        if not probe.ssh_port_reachable:
            raise ConfigureFlowError("SSH did not become reachable after enabling via ACP.", code="ssh_unreachable")

    # SSH accepts a password whose first 8 characters are right; every later
    # reboot goes through ACP, which wants all of it, so both must accept it.
    password_rejected = probe.ssh_status == SshAccessStatus.AUTH_REJECTED
    password_error = probe.error
    if probe.ssh_status == SshAccessStatus.OPEN_AUTHENTICATED:
        password_reading = read_admin_password(connection)
        callbacks.debug(**password_check_fields(password_reading))
        if password_reading.password_matches is False:
            password_rejected = True
            password_error = SSH_ONLY_PASSWORD_DEBUG

    if password_rejected:
        callbacks.update(ssh_final_reachable=probe.ssh_port_reachable)
        if hooks.save_without_authentication is None or not hooks.save_without_authentication(probed_state):
            raise ConfigureFlowError(
                AIRPORT_ADMIN_PASSWORD_REJECTED_MESSAGE,
                code="auth_failed",
                debug=password_error,
            )
    elif probe.ssh_status == SshAccessStatus.OPEN_AUTHENTICATED:
        callbacks.debug(ssh_final_reachable=True)
        callbacks.update(ssh_final_reachable=True)
    elif probe.ssh_status == SshAccessStatus.ALGORITHM_NEGOTIATION_FAILED:
        callbacks.update(ssh_final_reachable=probe.ssh_port_reachable)
        raise ConfigureFlowError(
            probe.error or "SSH algorithm negotiation failed.",
            code=PROBE_STATUS_ERROR_CODES[probe.ssh_status],
        )
    elif probe.ssh_status in (SshAccessStatus.TRANSPORT_FAILED, SshAccessStatus.LOCAL_NETWORK_FILTERED):
        callbacks.update(ssh_final_reachable=probe.ssh_port_reachable)
        raise ConfigureFlowError(probe.error or "SSH transport failed.", code=PROBE_STATUS_ERROR_CODES[probe.ssh_status])
    elif probe.ssh_status == SshAccessStatus.DEVICE_PROBE_FAILED:
        callbacks.update(ssh_final_reachable=probe.ssh_port_reachable)
        raise ConfigureFlowError(
            probe.error or "Failed to probe device compatibility.",
            code=PROBE_STATUS_ERROR_CODES[probe.ssh_status],
        )
    else:
        callbacks.update(ssh_final_reachable=probe.ssh_port_reachable)
        raise ConfigureFlowError(probe.error or "SSH did not become reachable.", code="ssh_unreachable")

    compatibility = probed_state.compatibility
    if compatibility is not None and not compatibility.supported:
        callbacks.debug(configure_failure_reason="unsupported_device")
        raise ConfigureFlowError(render_compatibility_message(compatibility), code="unsupported_device")

    identity = observed_device_identity(
        compatibility,
        discovered_airport_syap=request.discovered_airport_syap,
    )
    airport_mac = probe.airport_mac if probe.ssh_authenticated else None
    if (request.selected_record is not None and request.selected_record.airport_mac
            and airport_mac and request.selected_record.airport_mac != airport_mac):
        raise ConfigureFlowError("The device identity could not be confirmed. Refresh discovery and reconnect the saved device.", code="device_identity_mismatch")
    if identity.syap is not None:
        values["TC_AIRPORT_SYAP"] = identity.syap

    for removed_key, removed_reason in REMOVED_ENV_FILE_KEYS.items():
        if removed_key in request.existing:
            callbacks.message(f"Removing {removed_key} from {request.env_path}: {removed_reason}.")
    callbacks.stage("write_env")
    request.env_path.parent.mkdir(parents=True, exist_ok=True)
    write_configure_env_file(
        request.env_path,
        values,
        persist_password=request.persist_password,
        writer=request.write_env or write_env_file,
    )
    callbacks.update(
        configure_id=request.configure_id,
        device_syap=identity.syap,
        device_model=identity.model,
    )

    return ConfigureFlowResult(
        values=values,
        host=connection.host,
        configure_id=request.configure_id,
        connection=connection,
        probe_state=probed_state,
        compatibility=compatibility,
        identity=identity,
        airport_mac=airport_mac,
    )


def _optional_unsigned_config_value(value: object, key: str) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        raise ValueError(f"{key} must be a non-negative integer")
    if isinstance(value, int):
        if value < 0:
            raise ValueError(f"{key} must be a non-negative integer")
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer() or value < 0:
            raise ValueError(f"{key} must be a non-negative integer")
        return str(int(value))
    raw_value = str(value).strip()
    if raw_value == "":
        return ""
    if not raw_value.isdigit():
        raise ValueError(f"{key} must be a non-negative integer")
    return str(int(raw_value))


def build_configure_env_values(
    existing: dict[str, str],
    *,
    host: str,
    password: str,
    ssh_opts: str,
    configure_id: str,
    internal_share_use_disk_root: bool | None = None,
    smb_browse_compatibility: bool | None = None,
    mdns_advertise_afp: bool | None = None,
    any_protocol: bool | None = None,
    require_smb_encryption: bool | None = None,
    force_disable_smb_signing_and_encryption: bool | None = None,
    fruit_metadata_netatalk: bool | None = None,
    vfs_aio_fork_enabled: bool | None = None,
    debug_logging: bool | None = None,
    ata_idle_seconds: object | None = None,
    ata_standby: object | None = None,
) -> dict[str, str]:
    values = build_managed_config_env_values(
        existing,
        internal_share_use_disk_root=internal_share_use_disk_root,
        smb_browse_compatibility=smb_browse_compatibility,
        mdns_advertise_afp=mdns_advertise_afp,
        any_protocol=any_protocol,
        require_smb_encryption=require_smb_encryption,
        force_disable_smb_signing_and_encryption=force_disable_smb_signing_and_encryption,
        fruit_metadata_netatalk=fruit_metadata_netatalk,
        vfs_aio_fork_enabled=vfs_aio_fork_enabled,
        debug_logging=debug_logging,
        ata_idle_seconds=ata_idle_seconds,
        ata_standby=ata_standby,
    )
    values.update({
        "TC_HOST": host,
        "TC_PASSWORD": password,
        "TC_SSH_OPTS": ssh_opts,
        "TC_CONFIGURE_ID": configure_id,
    })
    return values


def build_managed_config_env_values(
    existing: dict[str, str],
    *,
    internal_share_use_disk_root: bool | None = None,
    smb_browse_compatibility: bool | None = None,
    mdns_advertise_afp: bool | None = None,
    any_protocol: bool | None = None,
    require_smb_encryption: bool | None = None,
    force_disable_smb_signing_and_encryption: bool | None = None,
    fruit_metadata_netatalk: bool | None = None,
    vfs_aio_fork_enabled: bool | None = None,
    debug_logging: bool | None = None,
    ata_idle_seconds: object | None = None,
    ata_standby: object | None = None,
) -> dict[str, str]:
    """Apply profile-managed settings without changing device identity or credentials."""
    effective_any_protocol = (
        parse_bool(existing.get("TC_ANY_PROTOCOL", DEFAULTS["TC_ANY_PROTOCOL"]))
        if any_protocol is None
        else any_protocol
    )
    effective_require_smb_encryption = (
        parse_bool(existing.get("TC_REQUIRE_SMB_ENCRYPTION", DEFAULTS["TC_REQUIRE_SMB_ENCRYPTION"]))
        if require_smb_encryption is None
        else require_smb_encryption
    )
    effective_force_disable_smb_signing_and_encryption = (
        parse_bool(
            existing.get(
                "TC_FORCE_DISABLE_SMB_SIGNING_AND_ENCRYPTION",
                DEFAULTS["TC_FORCE_DISABLE_SMB_SIGNING_AND_ENCRYPTION"],
            )
        )
        if force_disable_smb_signing_and_encryption is None
        else force_disable_smb_signing_and_encryption
    )
    validate_smb_protocol_options(
        any_protocol=effective_any_protocol,
        require_smb_encryption=effective_require_smb_encryption,
        force_disable_smb_signing_and_encryption=effective_force_disable_smb_signing_and_encryption,
    )

    values = preserved_env_file_values(existing)
    values.update({
        "TC_INTERNAL_SHARE_USE_DISK_ROOT": "true" if (
            parse_bool(existing.get("TC_INTERNAL_SHARE_USE_DISK_ROOT", DEFAULTS["TC_INTERNAL_SHARE_USE_DISK_ROOT"]))
            if internal_share_use_disk_root is None
            else internal_share_use_disk_root
        ) else "false",
        "TC_SMB_BROWSE_COMPATIBILITY": "true" if (
            parse_bool(existing.get("TC_SMB_BROWSE_COMPATIBILITY", DEFAULTS["TC_SMB_BROWSE_COMPATIBILITY"]))
            if smb_browse_compatibility is None
            else smb_browse_compatibility
        ) else "false",
        "TC_MDNS_ADVERTISE_AFP": "true" if (
            parse_bool(existing.get("TC_MDNS_ADVERTISE_AFP", DEFAULTS["TC_MDNS_ADVERTISE_AFP"]))
            if mdns_advertise_afp is None
            else mdns_advertise_afp
        ) else "false",
        "TC_ANY_PROTOCOL": "true" if effective_any_protocol else "false",
        "TC_REQUIRE_SMB_ENCRYPTION": "true" if effective_require_smb_encryption else "false",
        "TC_FORCE_DISABLE_SMB_SIGNING_AND_ENCRYPTION": (
            "true" if effective_force_disable_smb_signing_and_encryption else "false"
        ),
        "TC_FRUIT_METADATA_NETATALK": "true" if (
            parse_bool(existing.get("TC_FRUIT_METADATA_NETATALK", DEFAULTS["TC_FRUIT_METADATA_NETATALK"]))
            if fruit_metadata_netatalk is None
            else fruit_metadata_netatalk
        ) else "false",
        "TC_VFS_AIO_FORK_ENABLED": "true" if (
            parse_bool(existing.get("TC_VFS_AIO_FORK_ENABLED", DEFAULTS["TC_VFS_AIO_FORK_ENABLED"]))
            if vfs_aio_fork_enabled is None
            else vfs_aio_fork_enabled
        ) else "false",
        "TC_DEBUG_LOGGING": "true" if (
            parse_bool(existing.get("TC_DEBUG_LOGGING", DEFAULTS["TC_DEBUG_LOGGING"]))
            if debug_logging is None
            else debug_logging
        ) else "false",
        "TC_ATA_IDLE_SECONDS": (
            existing_config_value_or_default(existing, "TC_ATA_IDLE_SECONDS", "ATA idle seconds")
            if ata_idle_seconds is None
            else _optional_unsigned_config_value(ata_idle_seconds, "TC_ATA_IDLE_SECONDS")
        ),
        "TC_ATA_STANDBY": (
            existing_config_value_or_default(existing, "TC_ATA_STANDBY", "ATA standby timer")
            if ata_standby is None
            else _optional_unsigned_config_value(ata_standby, "TC_ATA_STANDBY")
        ),
    })
    return values


def write_configure_env_file(
    path: Path,
    values: Mapping[str, str],
    *,
    persist_password: bool,
    writer: Callable[[Path, Mapping[str, str]], None] = write_env_file,
) -> None:
    output = dict(values)
    if not persist_password:
        output.pop("TC_PASSWORD", None)
    writer(path, output)
