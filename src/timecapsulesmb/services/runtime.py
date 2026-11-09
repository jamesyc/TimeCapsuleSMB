from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from pathlib import Path
import time

from timecapsulesmb.checks.network import (
    LinkLocalInterfaceGone,
    closed_ssh_port_error,
    link_local_interface_gone,
    local_network_block,
    local_network_block_possible,
    ssh_reached_device,
)
from timecapsulesmb.core.config import DEFAULTS, AppConfig, ConfigError, load_app_config, require_valid_app_config
from timecapsulesmb.core.error_codes import ErrorCode, coded_error_in
from timecapsulesmb.core.net import canonical_ssh_target, endpoint_host
from timecapsulesmb.core.paths import AppPaths, resolve_app_paths
from timecapsulesmb.device.compat import DeviceCompatibility
from timecapsulesmb.device.errors import DeviceError, DeviceRestartingError
from timecapsulesmb.device.probe import (
    ProbeResult,
    ProbedDeviceState,
    SshAccessStatus,
    apple_bonjour_host_label,
    password_check_fields,
    read_admin_password,
    probe_connection_state,
    probe_ssh_command_conn,
)
from timecapsulesmb.device import probe as device_probe
from timecapsulesmb.device import acpd
from timecapsulesmb.device.disk_errors import (
    DISK_NOT_RESPONDING_MESSAGE,
    PROCESS_LIST_UNREADABLE_MESSAGE,
    disk_check_facts,
    disk_check_running_message,
    disk_stall_code,
    disk_stall_facts,
)
from timecapsulesmb.device.processes import (
    Process, StuckProcess, disk_io_stalls, disk_jobs_running, is_disk_check, listing_complete, wifi_client,
)
from timecapsulesmb.services.acp_ssh import client_networks_if_off_network, device_off_network
from timecapsulesmb.services.callbacks import OperationCallbacks
from timecapsulesmb.services.locate import locate_airport
from timecapsulesmb.transport.local import tcp_open
from timecapsulesmb.transport.errors import bare_timeout
from timecapsulesmb.transport.ssh import SshConnection

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
    """The device cannot take the work: the SSH probe did not reach a login
    session, or a preflight refused it before anything changed
    (require_acpd_ready). `code` says why."""

    code: ErrorCode


AIRPORT_ADMIN_PASSWORD_REJECTED_MESSAGE = "The AirPort admin password did not work."


ACP_UNREACHABLE_MESSAGE = (
    "This computer cannot reach AirPort ACP (port 5009) on the device, although the device's own ACP works. "
    "This command needs ACP from this computer, so nothing was changed."
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


def acpd_problems_and_disk_stalls(
    connection: SshConnection,
    callbacks: OperationCallbacks,
    *,
    network_reading: device_probe.AirportAcpReading | None = None,
    disk_check: bool = True,
) -> tuple[list[acpd.AcpdProblem], list[StuckProcess]]:
    """acpd.acpd_problems, with the processes stuck on the disk
    (processes.disk_io_stalls), which callers report instead of the problems:
    ACPd waits behind them, so the disk is what to report, and work would
    hang on that disk anyway. A stall in the first listing returns before
    ACPd is read; the list is read again when a problem is one a stuck disk
    causes (acpd.disk_stall_could_explain), as ACPd's slow reads can let a
    disk process pass the stall threshold. With `disk_check`, a disk checker
    that keeps running raises DiskCheckRunningError first
    (require_no_disk_check): the unplug that a stall or ACPd's problems ask
    for would interrupt it. Work that neither restarts the device nor uses
    the disk leaves it out. A restart under way raises
    DeviceRestartingError, with the marker's fields recorded."""

    def stalls_in(ps_output: str) -> list[StuckProcess]:
        stalls = disk_io_stalls(ps_output)
        if stalls:
            callbacks.debug(disk_io_stalls=[stall.describe() for stall in stalls])
        return stalls

    listing = require_no_disk_check(connection, callbacks) if disk_check else acpd.read_processes(connection)
    record_wifi_client(callbacks, listing)
    stalls = stalls_in(listing)
    if stalls:
        return [], stalls
    try:
        problems = acpd.acpd_problems(connection, listing, network_reading=network_reading)
    except DeviceRestartingError as exc:
        callbacks.debug(**exc.details)
        raise
    if acpd.disk_stall_could_explain(problems):
        stalls = stalls_in(acpd.read_processes(connection))
        if stalls:
            # Callers report only the disk; what ACPd showed is still recorded.
            callbacks.debug(acpd_problems=[problem.code for problem in problems],
                            **{key: value for problem in problems for key, value in problem.details.items()})
    return problems, stalls


class DiskCheckRunningError(DeviceAccessError):
    """A disk job kept running (require_no_disk_check): `checks`, disk
    checkers and, for a strict check, diskd's steps; or, `unreadable`, a strict
    check got no process list. `prefix` and `suffix` frame the message for the
    command that stopped, as disk_errors.DiskNotRespondingError's do. It is a
    DeviceAccessError, not that error's sibling in device.disk_errors, because
    the app and CLI preflights catch DeviceAccessError for every refusal."""

    code = ErrorCode.FSCK_ALREADY_RUNNING

    def __init__(
        self,
        checks: Iterable[Process],
        *,
        unanswered: bool = False,
        unreadable: bool = False,
        prefix: str = "",
        suffix: str = "Nothing was changed.",
    ) -> None:
        self.checks = tuple(checks)
        self.unanswered = unanswered
        self.unreadable = unreadable
        if unreadable:
            running, detail = PROCESS_LIST_UNREADABLE_MESSAGE, None
        else:
            running, detail = disk_check_running_message(self.checks, unanswered=unanswered), disk_check_facts(self.checks)
        message = " ".join(part for part in (prefix, running, suffix) if part)
        super().__init__(message, detail=detail)


# Apple's diskd checks a volume with fsck_hfs for a few seconds before it
# mounts it, one volume after another. The same checker still listed this much
# later is a longer check, such as a Disk Repair, which the work would have to
# wait for. Another checker each time is diskd's next short one, so the list
# is read again, this many times at most; a read that cannot be used counts
# as one of them.
DISK_CHECK_RECHECK_SECONDS = 10.0
DISK_CHECK_RECHECKS = 3


def require_no_disk_check(connection: SshConnection, callbacks: OperationCallbacks, *, strict: bool = False) -> str:
    """The process listing (acpd.read_processes), once no disk checker
    (processes.disk_jobs_running) runs in it; raises DiskCheckRunningError
    for one that keeps running: its volume is unmounted until it ends, and a
    restart or an unplug would interrupt it. `strict`, for work that writes the
    disk itself (Wipe Internal Disk), counts diskd's erase, format and mount
    steps too, and refuses a first listing that cannot be read: going on
    there could write a disk that another job is changing.

    A checker stuck on the disk itself does not count: the caller reports it
    as the disk (processes.disk_io_stalls). Another process stuck on the disk
    beside a live checker does not hide the checker, as that report's unplug
    would cut it off. Once a checker is seen, only a complete listing
    (processes.listing_complete) can show it gone: SSH or ps that does not
    answer, as on a disk kept busy, uses up a look. A first listing that
    cannot be read shows nothing, and the work goes on as it would without
    this check; a refusal after a last look that could not be read says the
    checker was running when the device stopped answering. Every wait is a
    measurement (disk_check_wait), so it reaches telemetry whether the work
    then goes on or not. The app runs its preflight in the confirmation pass
    and again in the run pass, so one operation can record two waits."""

    def live_checks(ps_output: str) -> list[Process]:
        stuck = {stall.pid for stall in disk_io_stalls(ps_output)}
        return [check for check in disk_jobs_running(ps_output, diskd_jobs=strict) if check.pid not in stuck]

    listing = acpd.read_processes(connection)
    if strict and not listing_complete(listing):
        callbacks.measurement("disk_check_wait", result="unreadable", looks=["unreadable"], duration_sec=0.0, strict=True)
        raise DiskCheckRunningError((), unreadable=True)
    checks = live_checks(listing)
    if not checks:
        return listing
    started = time.monotonic()
    # Each look's checkers, or "unreadable" for a listing that did not finish.
    looks: list[list[str] | str] = [[check.describe() for check in checks]]
    lasting: list[Process] = []
    ended = False
    for _recheck in range(DISK_CHECK_RECHECKS):
        programs = ", ".join(sorted({check.ucomm for check in checks}))
        kind = "disk check" if all(is_disk_check(check.ucomm) for check in checks) else "disk job"
        callbacks.message(
            f"A {kind} ({programs}) is running on the device. "
            f"Checking again in {DISK_CHECK_RECHECK_SECONDS:.0f} seconds..."
        )
        acpd.pause(DISK_CHECK_RECHECK_SECONDS)
        later_listing = acpd.read_processes(connection)
        if not listing_complete(later_listing):
            looks.append("unreadable")
            continue
        listing = later_listing
        earlier = {check.pid for check in checks}
        later = live_checks(listing)
        looks.append([check.describe() for check in later])
        if not later:
            ended = True
            break
        checks = later
        lasting = [check for check in later if check.pid in earlier]
        if lasting:
            break
    callbacks.measurement(
        "disk_check_wait",
        result="ended" if ended else "refused",
        looks=looks,
        duration_sec=round(time.monotonic() - started, 3),
        strict=strict,
    )
    if ended:
        return listing
    refused = lasting or checks
    callbacks.debug(disk_checks_running=[check.describe() for check in refused])
    raise DiskCheckRunningError(refused, unanswered=not lasting and looks[-1] == "unreadable")


WIFI_LINK_SLOW_MESSAGE = (
    "The device reaches your network over Wi-Fi rather than an Ethernet cable, and it did not answer in time. "
    "A slow Wi-Fi link can cause this."
)


def record_wifi_client(callbacks: OperationCallbacks, listing: str) -> None:
    """Record, as an operation field (telemetry on every run, and
    wifi_link_error's answer later in the operation), whether a finished
    listing shows the device reaching its network over Wi-Fi
    (processes.wifi_client). The link does not change during an operation:
    changing it takes AirPort Utility and a restart."""
    callbacks.update(wifi_client=wifi_client(listing))


# Coded errors that are an SSH deadline running out, which a slow link hits
# as surely as a slow disk.
_SLOW_LINK_CODES = frozenset({
    ErrorCode.PAYLOAD_UPLOAD_TIMEOUT,
    ErrorCode.DISK_WRITE_TEST_UNRESPONSIVE,
    ErrorCode.XATTR_MIGRATION_TIMEOUT,
    ErrorCode.RUNTIME_NOT_ANSWERED,
})


def _slow_link_symptom(exc: BaseException) -> bool:
    """An SSH command that timed out (transport.errors.bare_timeout), or a
    coded error that is one (_SLOW_LINK_CODES), even behind a wrapper such as
    a command's exit message: what a crawling link does. Other coded errors
    keep their own diagnosis."""
    coded = coded_error_in(exc)
    if coded is not None and coded.code in _SLOW_LINK_CODES:
        return True
    return bare_timeout(exc)


def wifi_link_error(exc: BaseException, *, known: object) -> bool:
    """Whether the error an operation ends with is the device's Wi-Fi link
    (code wifi_link_slow): an SSH or upload timeout on a device that reaches
    its network over Wi-Fi. `known` is the operation's wifi_client field
    (record_wifi_client); unrecorded counts as no, and the error keeps its
    own message, so a restart is still advised for a device that is just slow.
    It does not look at the device itself when nothing was recorded: that
    happens when the preflight's listing did not finish, on a link that was
    already not answering, or before any preflight, and a new login after a
    timeout could add more than a minute of silence before the error."""
    return known is True and _slow_link_symptom(exc)


def require_acpd_ready(
    connection: SshConnection,
    callbacks: OperationCallbacks,
    *,
    reading: device_probe.AirportAcpReading | None = None,
    restarts: bool = True,
) -> None:
    """Refuse, before anything changes, a device whose ACPd cannot take the work.

    Every reboot and every flash command goes through Apple's ACPd: the
    password must be the one ACP takes, ACPd must answer the device's own acp
    and the network, the last restart requested must have happened, and no
    stuck fork of ACPd may be waiting to hang the next reboot. A process stuck
    on the disk (processes.disk_io_stalls) refuses the work too, before ACPd is
    read: ACPd waits behind it, so the disk is what to report, and the work
    would hang on that disk anyway. A stuck disk checker gets its own code
    (disk_errors.disk_stall_code). A disk checker that keeps running, such
    as a Disk Repair whose connection ended, refuses it before either
    (require_no_disk_check), unless `restarts` is False: a flash backup,
    plan or patch neither restarts the device nor uses its disk, so it
    cannot interrupt the check. A restart under way, such as another
    command's, refuses it as well (code device_restarting). The first
    listing also records whether the device reaches its network over Wi-Fi
    (record_wifi_client). `reading` is
    this command's earlier network read of the device at this address
    (confirm_device_address); an unanswered one is not read again from here,
    as the second read would only repeat the same retry, but ACPd's listener
    is read through SSH (acpd.acpd_problems).
    """
    if reading is None:
        reading = read_admin_password(connection)
    callbacks.debug(**password_check_fields(reading))
    if reading.password_matches is False:
        raise DeviceAccessError(AIRPORT_ADMIN_PASSWORD_REJECTED_MESSAGE, code=ErrorCode.AUTH_FAILED)
    report_airport_identity(callbacks, reading)
    try:
        problems, stalls = acpd_problems_and_disk_stalls(
            connection, callbacks, network_reading=reading, disk_check=restarts,
        )
    except DeviceRestartingError as exc:
        raise DeviceAccessError(f"{exc.message} Nothing was changed.", code=exc.code) from None
    if stalls:
        raise DeviceAccessError(
            f"{DISK_NOT_RESPONDING_MESSAGE} Nothing was changed.",
            code=disk_stall_code(stalls),
            detail=disk_stall_facts(stalls),
        )
    if problems:
        details = {key: value for problem in problems for key, value in problem.details.items()}
        callbacks.debug(acpd_problems=[problem.code for problem in problems], **details)
        # Apple's firmware, not ours (build/native/README.md, "Apple ACPd").
        raise DeviceAccessError(
            acpd.problem_message(problems[0], note=f"{problems[0].note}Nothing was changed."),
            code=problems[0].error_code,
        )
    if reading.password_matches is None and reading.error:
        # ACPd answered its own acp and, through SSH, the network: the path
        # from here is what is blocked.
        host = endpoint_host(connection.host)
        client_networks = client_networks_if_off_network(host)
        if client_networks is not None:
            message, facts = device_off_network(host, client_networks)
            raise DeviceAccessError(message, code=ErrorCode.DEVICE_OFF_NETWORK, detail=facts)
        # SSH reached the device for the checks above: a read refused with no
        # route is macOS refusing this program's connections, when a connect
        # to port 22 gets no route too.
        block = local_network_block(host, (reading.error,), proof="ssh")
        if block is not None:
            raise DeviceAccessError(block.message, code=block.code, detail=block.detail)
        raise DeviceAccessError(ACP_UNREACHABLE_MESSAGE, code=ErrorCode.ACP_UNREACHABLE, detail=f"Device address {host}.")


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
            "A different AirPort answers at the saved address, and this device was not found on the network.",
            code=ErrorCode.DEVICE_IDENTITY_MISMATCH,
            detail=f"Saved address {endpoint_host(host)}.",
        )
    return host, replace(reading, listed_by_bonjour=located.listed)


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


# Apple's ssh, asked whether the device is up when this program's own
# connections get no route: a reachable device answers within seconds. Its
# login has transport.ssh.SSH_LOGIN_SECONDS on top.
SSH_PROOF_TIMEOUT_SECONDS = 15


def probe_failure_error(probe_result: ProbeResult, connection: SshConnection) -> DeviceAccessError:
    """The error for a probe that did not log in. A closed SSH port says why
    (checks.network.closed_ssh_port_error), after two causes that look the
    same from here: a saved link-local address on an interface this computer
    no longer has (checks.network.link_local_interface_gone), and macOS
    refusing this program's connections to a device that ssh reaches
    (checks.network.local_network_block). ssh is asked only when that block
    is possible (checks.network.local_network_block_possible: the macOS app,
    an ACP connect with no route, an address not on another network), so
    other failures cost nothing more."""
    if probe_result.ssh_status == SshAccessStatus.CLOSED:
        host = endpoint_host(connection.host)
        if (interface := link_local_interface_gone(host)) is not None:
            gone = LinkLocalInterfaceGone(host, interface)
            return DeviceAccessError(gone.message, code=gone.code, detail=gone.detail)
        closed = closed_ssh_port_error(host)
        detail = closed.detail
        if closed.code == ErrorCode.DEVICE_UNREACHABLE and local_network_block_possible(host, (closed.acp_error,)):
            ssh = probe_ssh_command_conn(connection, "/bin/echo ok", timeout=SSH_PROOF_TIMEOUT_SECONDS)
            reached = ssh_reached_device(ssh)
            block = local_network_block(host, (closed.acp_error,), proof="ssh" if reached else None)
            if block is not None:
                return DeviceAccessError(block.message, code=block.code, detail=block.detail)
            if not reached:
                # Telemetry then says whether /usr/bin/ssh is held by the same
                # block (no route) or the device is gone (a timeout).
                detail = f"{detail} SSH from this computer did not reach it either ({ssh.detail})."
        return DeviceAccessError(closed.message, code=closed.code, detail=detail)
    if probe_result.timeout is not None:
        # The device did not answer in time; nothing closed the connection.
        # The timeout stays the cause, so the error gets its guidance.
        timed_out = DeviceAccessError(probe_result.error or str(probe_result.timeout), code=ErrorCode.REMOTE_ERROR)
        timed_out.__cause__ = probe_result.timeout
        return timed_out
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
        raise probe_failure_error(state.probe_result, connection)
    return state.compatibility
