from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
import time

from timecapsulesmb.checks.network import closed_ssh_port_error
from timecapsulesmb.core.config import DEFAULTS, AppConfig, ConfigError, load_app_config, require_valid_app_config
from timecapsulesmb.core.error_codes import ErrorCode
from timecapsulesmb.core.net import canonical_ssh_target, endpoint_host
from timecapsulesmb.core.paths import AppPaths, resolve_app_paths
from timecapsulesmb.device.compat import DeviceCompatibility
from timecapsulesmb.device.errors import DeviceError
from timecapsulesmb.device.probe import (
    ProbeResult,
    ProbedDeviceState,
    SshAccessStatus,
    apple_bonjour_host_label,
    password_check_fields,
    read_admin_password,
    probe_connection_state,
)
from timecapsulesmb.device import probe as device_probe
from timecapsulesmb.device import acpd
from timecapsulesmb.services.acp_ssh import client_networks_if_off_network, device_off_network_message
from timecapsulesmb.services.callbacks import OperationCallbacks
from timecapsulesmb.services.locate import locate_airport
from timecapsulesmb.transport.ssh import SshConnection
from timecapsulesmb.transport.local import tcp_open

PasswordProvider = Callable[[str], str]

# The error code for each way an SSH probe can stop short of a login session.
# Configure uses the same codes. An unsupported model is never one of these:
# that needs a successful login, and its callers raise `unsupported_device`.
PROBE_STATUS_ERROR_CODES: dict[SshAccessStatus, ErrorCode] = {
    SshAccessStatus.AUTH_REJECTED: ErrorCode.AUTH_FAILED,
    SshAccessStatus.ALGORITHM_NEGOTIATION_FAILED: ErrorCode.SSH_COMPATIBILITY_FAILED,
    SshAccessStatus.CLIENT_FAILED: ErrorCode.SSH_CLIENT_FAILED,
    SshAccessStatus.TRANSPORT_FAILED: ErrorCode.SSH_TRANSPORT_FAILED,
    SshAccessStatus.LOCAL_NETWORK_FILTERED: ErrorCode.LOCAL_NETWORK_FILTERED,
    SshAccessStatus.DEVICE_PROBE_FAILED: ErrorCode.DEVICE_PROBE_FAILED,
}
# How long a probe of an already set up device keeps trying a closed SSH port.
# The probe tries port 22 once, for 2 s; in v3.1.2 telemetry several deploys
# stopped on that one timeout although SSH was on.
CLOSED_SSH_RECHECK_WINDOW_SECONDS = 6.0
CLOSED_SSH_RECHECK_INTERVAL_SECONDS = 1.0


class DeviceAccessError(DeviceError):
    """The SSH probe did not reach a login session; `code` says why."""

    code: ErrorCode


AIRPORT_ADMIN_PASSWORD_REJECTED_MESSAGE = "The AirPort admin password did not work."
# Commands other than configure say where to enter it again.
AIRPORT_PASSWORD_MISMATCH_MESSAGE = (
    f"{AIRPORT_ADMIN_PASSWORD_REJECTED_MESSAGE} Re-enter it in the macOS app, "
    "or run tcapsule configure from the command line."
)


ACP_UNREACHABLE_MESSAGE = (
    "This computer cannot reach AirPort ACP (port 5009) on {host}, although the device's own ACP works. "
    "This command needs ACP from this computer, so nothing was changed. Connect this computer to the "
    "device's network by Wi-Fi or one of its LAN ports, and turn off any VPN or firewall that filters "
    "local traffic, then try again."
)


def airport_identity_fields(
    airport_mac: str | None,
    system_name: str | None,
    system_dns_name: str | None = None,
) -> dict[str, object]:
    """The device identity a command reports, for the app to save with its profile.

    The app saves the MAC only for a profile without one, and only when the
    names match the ones the profile was saved with (DeviceRegistryStore). The
    names are the ones Apple advertises before any conflict rename: the
    instance name from syNm and ACPd's host label from syDN or syNm, so the
    app needs no copy of Apple's rules. A device with no name sends none.
    `airport_host_label_source` says which one: a label from syDN stays when
    the device is renamed, so it still names the device then.
    """
    if not airport_mac:
        return {}
    fields: dict[str, object] = {"airport_mac": airport_mac}
    naming = device_probe.derive_runtime_naming_identity(system_name, None, system_dns_name=system_dns_name)
    if naming.mdns_instance_name:
        fields["airport_instance_name"] = naming.mdns_instance_name
    from_dns_name = bool(apple_bonjour_host_label(system_dns_name or ""))
    if from_dns_name or apple_bonjour_host_label(system_name or ""):
        fields["airport_host_label"] = naming.mdns_host_label
        fields["airport_host_label_source"] = "syDN" if from_dns_name else "syNm"
    return fields


def report_airport_identity(callbacks: OperationCallbacks, reading: device_probe.AirportAcpReading) -> None:
    """Report the device a read with the right password reached."""
    if reading.password_matches:
        callbacks.update(**airport_identity_fields(reading.airport_mac, reading.airport_name, reading.airport_dns_name))


def require_acpd_ready(
    connection: SshConnection,
    callbacks: OperationCallbacks,
    *,
    reading: device_probe.AirportAcpReading | None = None,
) -> None:
    """Refuse, before anything changes, a device whose ACPd cannot take the work.

    Every reboot and every flash command goes through Apple's ACPd: the
    password must be the one ACP takes, ACPd must answer the device's own acp
    and the network, the last restart requested must have happened, and no
    stuck fork of ACPd may be waiting to hang the next reboot. `reading` is
    this command's earlier network read of the device at this address
    (confirm_device_address); an unanswered one is not read again from here,
    as the second read would only repeat the same retry, but ACPd's listener
    is read through SSH (acpd.acpd_problems).
    """
    if reading is None:
        reading = read_admin_password(connection)
    callbacks.debug(**password_check_fields(reading))
    if reading.password_matches is False:
        raise DeviceAccessError(AIRPORT_PASSWORD_MISMATCH_MESSAGE, code=ErrorCode.AUTH_FAILED)
    report_airport_identity(callbacks, reading)
    problems = acpd.acpd_problems(connection, network_reading=reading)
    if problems:
        details = {key: value for problem in problems for key, value in problem.details.items()}
        callbacks.debug(acpd_problems=[problem.code for problem in problems], **details)
        # Apple's firmware, not ours (DETAIL.md, "Apple ACPd can hang during its own restart").
        raise DeviceAccessError(
            acpd.acpd_broken_message(problems[0].what, note=f"{problems[0].note}Nothing was changed. "),
            code=ErrorCode.ACPD_BROKEN,
        )
    if reading.password_matches is None and reading.error:
        # ACPd answered its own acp and, through SSH, the network: the path
        # from here is what is blocked.
        host = endpoint_host(connection.host)
        client_networks = client_networks_if_off_network(host)
        if client_networks is not None:
            raise DeviceAccessError(device_off_network_message(host, client_networks), code=ErrorCode.DEVICE_OFF_NETWORK)
        raise DeviceAccessError(ACP_UNREACHABLE_MESSAGE.format(host=host), code=ErrorCode.ACP_UNREACHABLE)


def learn_airport_identity(host: str, password: str, callbacks: OperationCallbacks) -> None:
    """Report what answers at a saved address that has no MAC yet.

    The app's background SSH check runs this for each saved device it sees at
    its address, so an old profile learns the MAC before DHCP moves the device.
    One try: a device that does not answer is looked at again next time.
    """
    report_airport_identity(callbacks, device_probe.read_airport_acp(host, password, attempts=1))


def confirm_device_address(
    host: str,
    password: str,
    airport_mac: str,
    callbacks: OperationCallbacks,
    *,
    attempts: int = 2,
) -> tuple[str, device_probe.AirportAcpReading]:
    """The SSH target of the AirPort with this MAC, and the read that showed it.

    A saved address goes stale when DHCP gives the device another one, and
    another AirPort may then answer at it. Either way the device is looked
    for by its MAC (services.locate). Where it is not found, `host` stays and
    the command's own checks report what answers there. `attempts` is how
    many times the address is read before that.
    """
    reading = device_probe.read_airport_acp(host, password, attempts=attempts)
    if reading.password_matches and reading.airport_mac in (None, airport_mac):
        report_airport_identity(callbacks, reading)
        return host, reading
    if reading.password_matches is None:
        trigger = "unreachable"
    elif reading.password_matches is False:
        trigger = "password_rejected"
    else:
        trigger = "identity_mismatch"
    located = locate_airport(airport_mac, password, current_host=host, trigger=trigger, callbacks=callbacks)
    if located.host is not None:
        callbacks.message(f"The device now answers at {endpoint_host(located.host)}; the saved address is out of date.")
        callbacks.update(current_host=located.host)
        # locate accepted it only after its read took the password and gave this MAC.
        return located.host, device_probe.AirportAcpReading(password_matches=True, airport_mac=airport_mac)
    if located.outcome == "password_rejected":
        callbacks.message(located.rejected_note)
    if trigger == "identity_mismatch":
        raise DeviceAccessError(
            f"A different AirPort answers at {endpoint_host(host)}, and this device was not found on the network.",
            code=ErrorCode.DEVICE_IDENTITY_MISMATCH,
        )
    return host, reading


def follow_saved_device(
    config: AppConfig,
    airport_mac: str | None,
    callbacks: OperationCallbacks,
) -> tuple[AppConfig, tuple[str, device_probe.AirportAcpReading] | None]:
    """The config with TC_HOST where the saved device answers now, and that read.

    Only this command's copy changes: the app saves a new address after the
    user confirms it, the CLI with `tcapsule configure`. Without a saved MAC
    or a password nothing is read, and the read is None.
    """
    host = config.get("TC_HOST")
    password = config.get("TC_PASSWORD")
    if not airport_mac or not host or not password:
        return config, None
    current, reading = confirm_device_address(host, password, airport_mac, callbacks)
    device_reading = (current, reading)
    if current == host:
        return config, device_reading
    moved = AppConfig.from_values(
        {**config.values, "TC_HOST": current},
        path=config.path,
        exists=config.exists,
        file_values=config.file_values,
    )
    return moved, device_reading


def probe_failure_error(probe_result: ProbeResult, host: str) -> DeviceAccessError:
    """The error for a probe that did not log in; a closed SSH port says why
    (checks.network.closed_ssh_port_error)."""
    if probe_result.ssh_status == SshAccessStatus.CLOSED:
        code, message, _details = closed_ssh_port_error(endpoint_host(host))
        return DeviceAccessError(message, code=code)
    return DeviceAccessError(
        probe_result.error or "Failed to probe device compatibility.",
        code=PROBE_STATUS_ERROR_CODES.get(probe_result.ssh_status, ErrorCode.REMOTE_ERROR),
    )


def probe_managed_connection_state(
    connection: SshConnection,
    *,
    probe: Callable[[SshConnection], ProbedDeviceState] | None = None,
    tcp_open_func: Callable[[str, int], bool] | None = None,
    sleep_func: Callable[[float], None] = time.sleep,
    monotonic_func: Callable[[], float] = time.monotonic,
) -> ProbedDeviceState:
    """Probe a device that should already have SSH on.

    A closed port gets a few more seconds before the probe's answer stands,
    and the device is probed again as soon as port 22 opens. Configure does not
    use this: there SSH is usually off and ACP turns it on.
    """
    probe = probe or probe_connection_state
    tcp_open_func = tcp_open_func or tcp_open
    state = probe(connection)
    if state.probe_result.ssh_status != SshAccessStatus.CLOSED:
        return state
    host = endpoint_host(connection.host)
    deadline = monotonic_func() + CLOSED_SSH_RECHECK_WINDOW_SECONDS
    while True:
        remaining = deadline - monotonic_func()
        if remaining <= 0:
            return state
        sleep_func(min(CLOSED_SSH_RECHECK_INTERVAL_SECONDS, remaining))
        if tcp_open_func(host, 22):
            return probe(connection)


@dataclass(frozen=True)
class ManagedTargetState:
    connection: SshConnection
    probe_state: ProbedDeviceState | None


def load_env_config(
    *,
    env_path: Path | None = None,
    defaults: dict[str, str] | None = None,
    resolve_paths: Callable[..., AppPaths] | None = None,
) -> AppConfig:
    if resolve_paths is None:
        resolve_paths = resolve_app_paths
    resolved_path = resolve_paths(config_path=env_path).config_path
    return load_app_config(resolved_path, defaults=defaults)


def load_optional_env_config(
    *,
    env_path: Path | None = None,
    defaults: dict[str, str] | None = None,
    resolve_paths: Callable[..., AppPaths] | None = None,
) -> AppConfig:
    try:
        if resolve_paths is None:
            resolve_paths = resolve_app_paths
        resolved_path = resolve_paths(config_path=env_path).config_path
    except Exception:
        return AppConfig.missing(path=env_path or Path.cwd() / ".env")
    if not resolved_path.exists():
        return AppConfig.missing(path=resolved_path)
    try:
        return load_app_config(resolved_path, defaults=defaults)
    except OSError:
        return AppConfig.missing(path=resolved_path)


def resolve_ssh_credentials(
    config: AppConfig,
    *,
    allow_empty_password: bool = False,
    password_provider: PasswordProvider | None = None,
) -> tuple[str, str]:
    raw_host = config.require("TC_HOST")
    try:
        host = canonical_ssh_target(raw_host)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    password = config.get("TC_PASSWORD")
    if not password and not allow_empty_password:
        if password_provider is None:
            raise ConfigError("TC_PASSWORD is required when --no-input is used.")
        password = password_provider("Device root password: ")
    return host, password


def resolve_env_connection(
    config: AppConfig,
    *,
    required_keys: tuple[str, ...] = (),
    allow_empty_password: bool = False,
    password_provider: PasswordProvider | None = None,
) -> SshConnection:
    for key in required_keys:
        config.require(key)
    host, password = resolve_ssh_credentials(
        config,
        allow_empty_password=allow_empty_password,
        password_provider=password_provider,
    )
    return SshConnection(host=host, password=password, ssh_opts=config.get("TC_SSH_OPTS", DEFAULTS["TC_SSH_OPTS"]))


def resolve_validated_managed_target(
    config: AppConfig,
    *,
    command_name: str,
    profile: str,
    include_probe: bool = False,
    password_provider: PasswordProvider | None = None,
) -> ManagedTargetState:
    require_valid_app_config(config, profile=profile, command_name=command_name)
    connection = resolve_env_connection(
        config,
        password_provider=password_provider,
    )
    if profile == "flash":
        return ManagedTargetState(connection=connection, probe_state=None)
    probe_state = probe_managed_connection_state(connection) if include_probe else None
    return ManagedTargetState(connection=connection, probe_state=probe_state)


def require_connection_compatibility(connection: SshConnection) -> DeviceCompatibility:
    state = probe_managed_connection_state(connection)
    if state.compatibility is None:
        raise probe_failure_error(state.probe_result, connection.host)
    return state.compatibility
