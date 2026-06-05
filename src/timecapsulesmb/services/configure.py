from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Mapping

from timecapsulesmb.configure_defaults import existing_config_value_or_default, validated_value_or_empty
from timecapsulesmb.core.config import (
    DEFAULTS,
    CONFIG_VALIDATORS,
    infer_mdns_device_model_from_airport_syap,
    parse_bool,
    preserved_env_file_values,
    write_env_file,
)
from timecapsulesmb.core.net import canonical_ssh_target, endpoint_host
from timecapsulesmb.device.compat import DeviceCompatibility, render_compatibility_message
from timecapsulesmb.device.probe import ProbedDeviceState, probe_connection_state
from timecapsulesmb.integrations.acp import ACPAuthError, ACPError
from timecapsulesmb.services.acp_ssh import enable_ssh_with_identity_preflight, enable_ssh_with_port_preflight
from timecapsulesmb.services.callbacks import OperationCallbacks
from timecapsulesmb.services.runtime import ssh_target_link_local_resolution_error, wait_for_tcp_port_state
from timecapsulesmb.transport.ssh import SshConnection


class ConfigureFlowError(Exception):
    def __init__(self, message: str, *, code: str = "configure_failed") -> None:
        super().__init__(message)
        self.code = code


SshEnablePreflight = Literal["identity", "acp_port"]


@dataclass(frozen=True)
class ObservedDeviceIdentity:
    syap: str | None
    syap_source: str | None
    model: str | None
    model_source: str | None


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
    enable_ssh: bool = True
    ssh_enable_preflight: SshEnablePreflight = "identity"
    ssh_wait_timeout: int = 180
    verbose_wait: bool = True
    internal_share_use_disk_root: bool | None = None
    any_protocol: bool | None = None
    debug_logging: bool | None = None
    ata_idle_seconds: object | None = None
    ata_standby: object | None = None
    probe: Callable[[SshConnection], ProbedDeviceState] | None = None
    write_env: Callable[[Path, Mapping[str, str]], None] | None = None
    infer_model_from_syap: Callable[[str], str | None] = infer_mdns_device_model_from_airport_syap


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


def configure_ssh_target(
    value: str,
    ssh_opts: str,
    *,
    label: str = "Device SSH target",
    validate_config_value: bool = False,
) -> str:
    if validate_config_value:
        validation_error = CONFIG_VALIDATORS["TC_HOST"](value, label)
        if validation_error is not None:
            raise ValueError(validation_error)
    target = canonical_ssh_target(value)
    resolution_error = ssh_target_link_local_resolution_error(target, ssh_opts)
    if resolution_error is not None:
        raise ValueError(resolution_error)
    return target


def enable_ssh_and_reprobe(
    connection: SshConnection,
    *,
    timeout_seconds: int = 180,
    ssh_enable_preflight: SshEnablePreflight = "identity",
    verbose_wait: bool = True,
    callbacks: OperationCallbacks | None = None,
    probe: Callable[[SshConnection], ProbedDeviceState] | None = None,
) -> ProbedDeviceState | None:
    callbacks = callbacks or OperationCallbacks()
    if probe is None:
        probe = probe_connection_state
    host = endpoint_host(connection.host)
    callbacks.debug(
        configure_acp_enable_attempted=True,
        ssh_initially_reachable=False,
    )
    callbacks.message("\nSSH is not reachable. Attempting to enable SSH on the device...")
    try:
        if ssh_enable_preflight == "identity":
            enable_ssh_with_identity_preflight(
                host,
                connection.password,
                reboot_device=True,
                callbacks=callbacks,
            )
        elif ssh_enable_preflight == "acp_port":
            enable_ssh_with_port_preflight(
                host,
                connection.password,
                reboot_device=True,
                callbacks=callbacks,
            )
        else:
            raise ValueError(f"unsupported SSH enable preflight: {ssh_enable_preflight}")
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
    callbacks.stage("wait_for_ssh_after_acp")
    if not wait_for_tcp_port_state(
        host,
        22,
        expected_state=True,
        timeout_seconds=timeout_seconds,
        service_name="SSH port",
        log=callbacks.log if verbose_wait else None,
    ):
        callbacks.update(ssh_final_reachable=False)
        return None

    callbacks.update(ssh_final_reachable=True)
    callbacks.stage("ssh_probe_after_acp")
    return probe(connection)


def observed_device_identity(
    compatibility: DeviceCompatibility | None,
    *,
    discovered_airport_syap: str | None = None,
    infer_model_from_syap: Callable[[str], str | None] = infer_mdns_device_model_from_airport_syap,
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

    model_source: str | None = "probed"
    model = None if compatibility is None else compatibility.exact_model
    if model is None and syap is not None:
        model = infer_model_from_syap(syap)
        model_source = "derived" if model is not None else None
    elif model is None:
        model_source = None

    return ObservedDeviceIdentity(
        syap=syap,
        syap_source=syap_source,
        model=model,
        model_source=model_source,
    )


def run_configure_flow(
    request: ConfigureFlowRequest,
    *,
    callbacks: OperationCallbacks | None = None,
    hooks: ConfigureFlowHooks | None = None,
) -> ConfigureFlowResult:
    callbacks = callbacks or OperationCallbacks()
    hooks = hooks or ConfigureFlowHooks()

    values = build_configure_env_values(
        request.existing,
        host=request.host,
        password=request.password,
        ssh_opts=request.ssh_opts,
        configure_id=request.configure_id,
        internal_share_use_disk_root=request.internal_share_use_disk_root,
        any_protocol=request.any_protocol,
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
        probed_state = enable_ssh_and_reprobe(
            connection,
            timeout_seconds=request.ssh_wait_timeout,
            ssh_enable_preflight=request.ssh_enable_preflight,
            verbose_wait=request.verbose_wait,
            callbacks=callbacks,
            probe=probe_connection,
        )
        if probed_state is None:
            raise ConfigureFlowError("SSH did not open after enabling via ACP.", code="ssh_enable_timeout")
        if hooks.after_probe is not None:
            hooks.after_probe(connection, probed_state)
        probe = probed_state.probe_result
        if not probe.ssh_port_reachable:
            raise ConfigureFlowError("SSH did not become reachable after enabling via ACP.", code="ssh_unreachable")

    if not probe.ssh_authenticated:
        callbacks.update(ssh_final_reachable=probe.ssh_port_reachable)
        if hooks.save_without_authentication is None or not hooks.save_without_authentication(probed_state):
            raise ConfigureFlowError(
                probe.error or "The provided AirPort SSH target and password did not work.",
                code="auth_failed",
            )
    else:
        callbacks.debug(ssh_final_reachable=True)
        callbacks.update(ssh_final_reachable=True)

    compatibility = probed_state.compatibility
    if compatibility is not None and not compatibility.supported:
        callbacks.debug(configure_failure_reason="unsupported_device")
        raise ConfigureFlowError(render_compatibility_message(compatibility), code="unsupported_device")

    identity = observed_device_identity(
        compatibility,
        discovered_airport_syap=request.discovered_airport_syap,
        infer_model_from_syap=request.infer_model_from_syap,
    )
    if identity.syap is not None:
        values["TC_AIRPORT_SYAP"] = identity.syap
    if identity.model is not None:
        values["TC_MDNS_DEVICE_MODEL"] = identity.model

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
        host=request.host,
        configure_id=request.configure_id,
        connection=connection,
        probe_state=probed_state,
        compatibility=compatibility,
        identity=identity,
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
    any_protocol: bool | None = None,
    debug_logging: bool | None = None,
    ata_idle_seconds: object | None = None,
    ata_standby: object | None = None,
) -> dict[str, str]:
    values = preserved_env_file_values(existing)
    values.update({
        "TC_HOST": host,
        "TC_PASSWORD": password,
        "TC_SSH_OPTS": ssh_opts,
        "TC_INTERNAL_SHARE_USE_DISK_ROOT": "true" if (
            parse_bool(existing.get("TC_INTERNAL_SHARE_USE_DISK_ROOT", DEFAULTS["TC_INTERNAL_SHARE_USE_DISK_ROOT"]))
            if internal_share_use_disk_root is None
            else internal_share_use_disk_root
        ) else "false",
        "TC_ANY_PROTOCOL": "true" if (
            parse_bool(existing.get("TC_ANY_PROTOCOL", DEFAULTS["TC_ANY_PROTOCOL"]))
            if any_protocol is None
            else any_protocol
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
        "TC_CONFIGURE_ID": configure_id,
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
