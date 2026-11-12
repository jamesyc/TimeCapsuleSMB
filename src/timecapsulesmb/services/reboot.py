from __future__ import annotations

import ipaddress
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Union

from timecapsulesmb.checks.network import (
    PORT_CHECK_TIMEOUT_SECONDS,
    SSH_PORT,
    SSH_PROOF_TIMEOUT_SECONDS,
    IpNetwork,
    LocalNetworkBlock,
    NetworkLinkResult,
    NetworkPlace,
    PlaceComparison,
    classify_network_link,
    compare_place,
    host_networks,
    local_lan_networks,
    local_network_block,
    macos_may_hold_this_process,
    read_network_place,
    reportable_network,
    ssh_reached_device,
)
from timecapsulesmb.core.error_codes import CodedError, ErrorCode
from timecapsulesmb.core.install import this_computer
from timecapsulesmb.core.net import endpoint_host
from timecapsulesmb.device import acpd
from timecapsulesmb.device.probe import probe_ssh_command_conn
from timecapsulesmb.discovery.models import normalize_airport_mac
from timecapsulesmb.integrations import acp
from timecapsulesmb.services import acp_diagnostics
from timecapsulesmb.services.callbacks import OperationCallbacks
from timecapsulesmb.services.locate import LocateResult, locate_airport
from timecapsulesmb.transport.errors import TransportError
from timecapsulesmb.transport.local import connect_answered, connect_error_kind, lan_routers, tcp_connect_error, tcp_open
from timecapsulesmb.transport.ssh import SshConnection, close_ssh_masters


# Every TimeCapsuleSMB reboot goes through Apple's ACPd over the network, as
# AirPort Utility does: ACPd saves ACPData.bin and only then runs shutdown
# (issue #177). The same connection proves the reboot happened: ACP property
# syUT is the device's uptime in seconds, so a reading smaller than the time
# since the request can only come from a new boot.
REBOOT_STRATEGY = "network_acp"
REBOOT_START_TIMEOUT_SECONDS = 90
# The new boot can take minutes to start: 122 network ACP reboots of
# TimeCapsule6,116 in v3.2.0-2 to v3.3.0 telemetry reached the new kernel
# 47-175 s after the request, but one device took 413 and 488 s, after ACP had
# stopped answering at 11 s. Counted from the first unanswered read.
REBOOT_UP_TIMEOUT_SECONDS = 600
# SSH gets its own limit from the new boot, so a slow restart does not use it
# up. In 456 network ACP reboots since v3.2.0-1, SSH was open within 1 s of the
# new boot being seen, but only one of them turned SSH on, which is slower: in
# v3.1.x telemetry, successful enable waits had a p99 of 163 s from the
# request, and 17 of 57 timeouts at 180 s found SSH open when the user retried
# 1.4-5.8 minutes later.
REBOOT_SSH_TIMEOUT_SECONDS = 240
REBOOT_POLL_SECONDS = 5
ACP_REQUEST_TIMEOUT_SECONDS = 25
UPTIME_READ_TIMEOUT_SECONDS = 5
# syUT counts whole seconds, and the device's clock may run a little slower
# than this host's. A new boot reads minutes below the old boot's count, so
# two seconds of margin costs nothing.
UPTIME_SLACK_SECONDS = 2
# sshd starts 7-10 s after the kernel on both device families, ACPd at 4-5 s.
# A device whose SSH is still closed at this uptime kept SSH off.
SSH_CLOSED_CHECK_UPTIME_SECONDS = 60
# A device that is down this long may have come back at another DHCP address
# (51 of 259 installs whose wait timed out since v3.0 later reached it at a new
# address), so it is looked for by its AirPort MAC from then on. The new kernel
# starts 47-175 s after the request, so earlier looks find nothing.
FOLLOW_AFTER_DOWN_SECONDS = 60
FOLLOW_INTERVAL_SECONDS = 30
# Apple's ACPd can take a reboot request, tear itself down and never restart
# the kernel (build/native/README.md, "Apple ACPd"): ACP goes quiet as in a
# reboot, but the old kernel and its sshd keep running. The restart marker
# written before the request tells (device.acpd.restart_verdict). The wait
# looks only once the marker is old enough to judge, while ACP is down and
# SSH answers: a real restart has closed SSH by then. A hang asks the user to
# unplug the device and the wait goes on, its limit (up_timeout_seconds)
# counted again from that request: the power-on is a new boot like any other,
# and a restart that was only slow ends the wait the same way.
HANG_CHECK_AFTER_REQUEST_SECONDS = acpd.RESTART_HANG_SECONDS
HANG_CHECK_INTERVAL_SECONDS = 15
UNPLUG_STEPS = (
    "Unplug the device, wait 10 seconds and plug it back in. TimeCapsuleSMB keeps waiting and carries on when it "
    "starts up."
)
UNPLUG_MESSAGE = f"Apple's AirPort service stopped without restarting the device. {UNPLUG_STEPS}"
# The device's restart drops this Mac's link to it, and macOS can then join
# another known network (a neighbour's Wi-Fi, an iPhone's hotspot) and stay
# there. Most Macs are back within about 70 s of the request and the slowest
# tenth after about 136 s, so the hint waits this long after the device went
# down, and for this many polls in a row on another network.
CLIENT_AWAY_HINT_AFTER_DOWN_SECONDS = 180
CLIENT_AWAY_POLLS = 2
# A device whose restart has not answered this long after it went down may
# have stopped partway through the restart, which only unplugging it ends; nearly every restart that finishes answers well before. The wait then
# asks for the unplug, when nothing at the device's address answers and this
# computer is still on the device's network, and goes on waiting.
RESTART_STUCK_AFTER_DOWN_SECONDS = 360
# Once the device has been down a while, this computer's routers are read at
# most this often (each read runs netstat and arp on macOS).
PLACE_READ_INTERVAL_SECONDS = 15
# Read failures that show something answers at the device's address: a
# refused port, a connection the device reset or closed, a rejected password
# (a device reset to factory settings), a reply ACP could not use, or another
# AirPort holding the address.
_ANSWER_KINDS = frozenset({"refused", "reset", "closed", "auth", "property", "protocol", "other_device"})
# Read errors that mean nothing at the address answered: a connect that timed
# out, a host that is down, no route, a name that no longer resolves (a saved
# .local name while the device is off), an address this computer lost. Any
# other failure ("operation not permitted" from a firewall such as LuLu, an
# address this computer cannot use, a connection that was made but got no
# reply, an error with no known text) says nothing either way, so it is
# unclear: neither silence nor an answer. No route counts here except for a
# process macOS may hold (DeviceSilence).
_SILENT_KINDS = frozenset({"timeout", "host_down", "network_unreachable", "name_lookup_failed", "no_route"})
# How many unanswered reads in a row make the device silent, and how many
# unclear reads in a row end that.
STUCK_QUIET_READS = 3
# iOS's Personal Hotspot gives its clients addresses in this network.
PERSONAL_HOTSPOT_NETWORK = ipaddress.ip_network("172.20.10.0/28")

REBOOT_NO_DOWN_MESSAGE = "Reboot was requested but the device did not restart."
REBOOT_UP_TIMEOUT_MESSAGE = "Timed out waiting for SSH after reboot."
# Two ACP reads of the uptime just before the request did not answer, so
# nothing was requested: SSH worked moments before, and the work before the
# restart is done.
REBOOT_NOT_REQUESTED_MESSAGE = (
    "The device did not answer AirPort ACP just before the restart, so the restart was not requested."
)
SSH_STILL_OPEN_MESSAGE = "SSH reopened after reboot. Disable did not persist."
# Silence is all the wait has seen, as for restart_stuck_message: the device
# may have stopped partway through the restart, which an unplug ends, or be
# back at an address only Bonjour could have shown. So the unplug is a
# suggestion.
RESTART_STUCK_HINT = (
    "The device has not come back on this network. Try unplugging the device, waiting 10 seconds and plugging it "
    "back in; TimeCapsuleSMB keeps waiting and carries on when it starts up. If the network blocks Bonjour, the "
    "device may be at a new address instead."
)
# The device's address stopped looking silent after RESTART_STUCK_HINT:
# something answered (the device starting up, which an unplug would
# interrupt, or another AirPort), or several reads in a row said nothing
# either way, such as the app's no route under macOS's Local Network block.
RESTART_STUCK_WITHDRAWN_MESSAGE = (
    "TimeCapsuleSMB no longer sees the device's address as silent, so leave the device plugged in for now. "
    "It keeps waiting."
)
# A terminal's last stuck line once the device has answered and fallen silent
# again twice: it stays true however the wait goes on.
RESTART_STUCK_FLAPPING_MESSAGE = (
    "The device keeps answering and then going quiet. TimeCapsuleSMB keeps waiting, and says what to do if the "
    "device does not come back."
)
ACPD_STOPPED_MESSAGE = acpd.acpd_broken_message(
    "the AirPort service on this device stopped but did not restart the device",
)
# The same hang inferred from refused reads, not seen through SSH: another
# host that took the address refuses too, and so does an old kernel whose
# restart is only slow. The words say what was seen, and their first sentence
# keeps these out of the telemetry dashboard's known ACPd bug, which the
# marker look's sightings stay in.
ACPD_REFUSED_MESSAGE = (
    f"The AirPort service at the device's address refused every connection for "
    f"{RESTART_STUCK_AFTER_DOWN_SECONDS // 60} minutes after the restart request: it may have stopped without "
    "restarting the device."
)
ACPD_REFUSED_UNPLUG_MESSAGE = f"{ACPD_REFUSED_MESSAGE} {UNPLUG_STEPS}"


@dataclass
class DeviceSilence:
    """Whether nothing has answered at the device's address lately, from the
    restart wait's reads once the device went down.

    Each read, the SSH port's look included, is an answer, unanswered
    (_SILENT_KINDS) or unclear. The device turns silent (`quiet`) after
    STUCK_QUIET_READS unanswered reads in a row, and stays silent until
    something answers or STUCK_QUIET_READS unclear reads come in a row. So one
    unclear read, such as a stray "No route to host" while the device is
    down, leaves a silent device silent, but delays one that is not silent
    yet; and after a run of unclear reads, silence needs a new unbroken run of
    unanswered ones. Unclear: any failure that says nothing about the
    device, and no route for a process macOS's Local Network access holds
    (`held`, the app or a terminal app other than Terminal:
    checks.network.macos_may_hold_this_process), which macOS gives for such a
    process's connections while the device is up. Maybe blocked: the last
    STUCK_QUIET_READS reads all got that no route. Linux reports a host gone
    from its link as no route, so there it is unanswered.
    """

    held: bool
    # Unanswered reads in a row.
    quiet_reads: int = 0
    # Unclear reads in a row.
    unclear_reads: int = 0
    quiet: bool = False
    no_route_errors: list[str] = field(default_factory=list)

    def observe_read(self, kind: str, error: str) -> None:
        """One failed read of the uptime, by _read_error_kind (or "other_device");
        a name's addresses give one kind each, joined by "+"."""
        kinds = set(kind.split("+"))
        held_no_route = kinds == {"no_route"} and self.held
        if kinds & _ANSWER_KINDS:
            self.quiet = False
            self.quiet_reads = 0
            self.unclear_reads = 0
            self.no_route_errors = []
        elif kinds <= _SILENT_KINDS and not held_no_route:
            self.quiet_reads += 1
            self.unclear_reads = 0
            self.no_route_errors = []
            if self.quiet_reads >= STUCK_QUIET_READS:
                self.quiet = True
        else:
            self.quiet_reads = 0
            self.unclear_reads += 1
            # maybe_blocked and the block check need only the last
            # STUCK_QUIET_READS.
            self.no_route_errors = [*self.no_route_errors[1 - STUCK_QUIET_READS:], error] if held_no_route else []
            if self.unclear_reads >= STUCK_QUIET_READS:
                self.quiet = False

    def observe_ssh_port(self, error: str | None) -> None:
        """A connect to port 22 (tcp_connect_error's result). A device back on
        a new boot whose ACPd quit at once, or an old kernel whose sshd still
        takes connections, is up."""
        kind = "refused" if connect_answered(error) else connect_error_kind(error) or "other"
        self.observe_read(kind, str(error))

    @property
    def maybe_blocked(self) -> bool:
        return len(self.no_route_errors) >= STUCK_QUIET_READS


class RebootFlowError(CodedError):
    """A reboot that failed or could not be proven; `code` is the app's error
    code, and `result` the restart wait's own outcome (its reboot_cycle
    measurement's result), when the wait raised it."""

    code: ErrorCode

    def __init__(
        self, message: str, *, code: ErrorCode | str, detail: str | None = None, result: str | None = None,
    ) -> None:
        super().__init__(message, code=code, detail=detail)
        self.result = result


# The restart that turns SSH on (reboot_to_enable_ssh). The dashboard groups
# enable timeouts by the first message's text.
SSH_ENABLE_TIMEOUT_MESSAGE = "SSH did not open after enabling via ACP."
# The wait's limit counts again from an unplug request (RESTART_STUCK_HINT,
# the hang's), so the message names no length.
SSH_ENABLE_NO_RETURN_MESSAGE = "After the restart that turns on SSH, the device did not answer before the wait ended."
SSH_ENABLE_FAILURE_CODES = frozenset({ErrorCode.SSH_CLOSED_AFTER_RESTART, ErrorCode.SSH_ENABLE_TIMEOUT})


def reboot_to_enable_ssh(
    host: str, password: str, *, wait: bool, callbacks: OperationCallbacks | None = None,
    wifi_name: str | None = None,
) -> str | None:
    """reboot_device for the restart that turns SSH on (configure, set-ssh).
    SSH that did not open in time raises RebootFlowError with a code that
    says whether the device came back at all, which decides what the user
    does next: ssh_closed_after_restart when it came back on a new boot and
    only SSH stayed closed for REBOOT_SSH_TIMEOUT_SECONDS, ssh_enable_timeout
    when no new boot answered within the restart wait
    (REBOOT_UP_TIMEOUT_SECONDS), so nothing tried SSH yet."""
    try:
        return reboot_device(
            host, password, wait=wait, callbacks=callbacks, wifi_name=wifi_name,
            up_timeout_message=SSH_ENABLE_NO_RETURN_MESSAGE, ssh_closed_message=SSH_ENABLE_TIMEOUT_MESSAGE,
        )
    except RebootFlowError as exc:
        if exc.code != ErrorCode.REBOOT_NOT_FINISHED:
            raise
        code = ErrorCode.SSH_CLOSED_AFTER_RESTART if exc.result == "ssh_not_open" else ErrorCode.SSH_ENABLE_TIMEOUT
        raise RebootFlowError(str(exc), code=code, result=exc.result) from exc


def reboot_device(
    host: str,
    password: str,
    *,
    wait: bool,
    callbacks: OperationCallbacks | None = None,
    expect_ssh: bool = True,
    start_timeout_seconds: int = REBOOT_START_TIMEOUT_SECONDS,
    up_timeout_seconds: int = REBOOT_UP_TIMEOUT_SECONDS,
    no_down_message: str = REBOOT_NO_DOWN_MESSAGE,
    up_timeout_message: str = REBOOT_UP_TIMEOUT_MESSAGE,
    ssh_closed_message: str | None = None,
    stopped_message: str = ACPD_STOPPED_MESSAGE,
    stuck_message: str | None = None,
    ssh: SshConnection | None = None,
    wifi_name: str | None = None,
) -> str | None:
    """Reboot the device through ACP and, with `wait`, prove it rebooted.

    The new boot must answer ACP within `up_timeout_seconds` of the first
    unanswered read. Then SSH must open within REBOOT_SSH_TIMEOUT_SECONDS of
    the new boot being seen (`expect_ssh`), or must stay closed.
    `ssh`, when SSH works before the request, marks the restart on the
    device (device.acpd.RESTART_MARKER), and the wait watches for a reboot
    that never happens: ACPd quiet while the marker is still there. It then
    asks the user to unplug the device (wait_for_reboot_up's progress,
    acpd_hung) and waits on for the new boot; one that never comes is
    reported as ACPd's hang.
    `up_timeout_message` ends a wait no new boot answered, and
    `ssh_closed_message` (by default the same) one whose new boot kept SSH
    closed.
    Returns the device's SSH target when the new boot came up at another
    address (see services.locate), else None. Raises RebootFlowError.
    When this Mac shared a network with the device before the request, the
    wait also watches for it leaving that network (_client_away, and a router
    toward the device other than the one before: checks.network.compare_place),
    and names `wifi_name`, the device's Wi-Fi network (its Bonjour raNm), in
    the hint. A device that stays silent past RESTART_STUCK_AFTER_DOWN_SECONDS
    while this Mac stays on that network is asked to be unplugged, and the
    wait goes on; one that never comes back ends with `stuck_message`.
    """
    callbacks = callbacks or OperationCallbacks()
    host = endpoint_host(host)
    if not wait:
        restart_marker = _mark(ssh)
        if restart_marker is not None:
            callbacks.debug(restart_marker=restart_marker)
        _request(host, password, callbacks, raise_errors=True)
        return None
    # Two reads, as the preflight's identity read makes: a busy ACPd or a
    # lossy link can miss one. Nothing is requested without the uptime that
    # proves the restart later.
    u0, airport_mac, error = _read_uptime(host, password)
    if u0 is None and not isinstance(error, acp.ACPAuthError):
        time.sleep(REBOOT_POLL_SECONDS)
        u0, airport_mac, error = _read_uptime(host, password)
    if u0 is None:
        if isinstance(error, acp.ACPAuthError):
            raise RebootFlowError(
                f"Could not read the device's uptime through AirPort ACP before rebooting: {error}",
                code=ErrorCode.AUTH_FAILED,
            )
        raise RebootFlowError(f"{REBOOT_NOT_REQUESTED_MESSAGE} ({error})", code=ErrorCode.REBOOT_NOT_REQUESTED)
    started = time.monotonic()
    restart_marker = _mark(ssh)
    # Resolved before the device goes down, when a name still resolves.
    device_networks = _device_networks(host)
    link = _network_link(device_networks) if device_networks is not None else None
    # A device reached through a router, or with nothing to compare, is not
    # watched: this Mac's own networks say nothing about reaching it.
    watched = link if link is not None and link.verdict == "shared" else None
    # Where this Mac is before the request: the router toward the device is
    # what tells another network with the same addresses from this one.
    place_before = _read_place()
    # Before the request: a router-mode device's restart takes this route away.
    device_is_gateway = _device_is_gateway(device_networks, place_before)
    time.sleep(1)
    _request(host, password, callbacks, raise_errors=False)
    return _wait(
        host,
        password,
        u0,
        started,
        airport_mac=airport_mac,
        callbacks=callbacks,
        expect_ssh=expect_ssh,
        start_timeout_seconds=start_timeout_seconds,
        up_timeout_seconds=up_timeout_seconds,
        no_down_message=no_down_message,
        up_timeout_message=up_timeout_message,
        ssh_closed_message=ssh_closed_message or up_timeout_message,
        stopped_message=stopped_message,
        stuck_message=stuck_message or restart_stuck_message(),
        ssh=ssh,
        restart_marker=restart_marker,
        watched_link=watched,
        device_is_gateway=device_is_gateway,
        wifi_name=wifi_name,
        place_before=place_before if watched is not None else None,
    )


def _device_networks(host: str) -> list[IpNetwork] | None:
    try:
        return host_networks(host) or None
    except Exception:
        return None


def _network_link(device_networks: Sequence[IpNetwork]) -> NetworkLinkResult | None:
    """Whether this Mac is on a network the device's addresses are on, read
    from this Mac's own interfaces (no packet is sent)."""
    try:
        return classify_network_link(
            device_networks, [item.network for item in local_lan_networks()], source="reboot_wait",
        )
    except Exception:
        return None


def _read_place() -> NetworkPlace | None:
    return read_network_place(networks=local_lan_networks, routers=lan_routers)


def _device_is_gateway(device_networks: list[IpNetwork] | None, place: NetworkPlace | None) -> bool | None:
    """Whether the device is one of this Mac's default routers, as a Time
    Capsule in router mode is: its restart then takes this Mac's whole
    connection, which telemetry weighs against how often the Mac leaves.
    None when unknown."""
    if place is None or not place.routers or device_networks is None:
        return None
    return place.routes_through(device_networks)


def _compare_now(
    before: NetworkPlace, device_networks: Sequence[IpNetwork], *, device_is_router: bool,
) -> PlaceComparison:
    """compare_place for this Mac now against `before`, read just before the
    request."""
    router = before.router_toward(device_networks)
    return compare_place(
        _read_place(),
        device_networks,
        router_mac=router.mac if router is not None else None,
        known_router_macs=before.router_macs,
        known_networks=[item.network for item in before.networks],
        device_is_router=device_is_router,
    )


def _client_away(before: NetworkLinkResult) -> tuple[IpNetwork, ...] | None:
    """The networks this Mac joined since `before` (the link read before the
    request), once it has addresses only on other networks than the device's:
    it left. None while it shares one; when it has nothing to compare (no
    address, or only link-local ones), which a restart's dropped link is; and
    when it is only on networks it already had, as a Mac on the device's
    Ethernet and on another Wi-Fi network is once the restart drops the
    Ethernet link: it went nowhere, and the link comes back with the device."""
    link = _network_link(before.device_networks)
    if link is None or link.verdict != "separate":
        return None
    return tuple(network for network in link.compared()[0] if network not in before.local_networks) or None


def _network_names(networks: tuple[IpNetwork, ...]) -> str:
    return ", ".join(
        f"{network} (an iPhone's Personal Hotspot)" if network == PERSONAL_HOTSPOT_NETWORK
        else reportable_network(network, hide_hosts=True)
        for network in networks
    )


def _now_on(joined: tuple[IpNetwork, ...]) -> str:
    """The networks this computer joined. None are named when it joined one
    with the same addresses, which only its router tells apart."""
    return _network_names(joined) if joined else "another network"


def client_away_message(joined: tuple[IpNetwork, ...], wifi_name: str | None, *, platform: str | None = None) -> str:
    """The hint once this Mac has been on another network for a while: which
    one, and where to go back to. The device's Wi-Fi name fits a Mac on its
    Ethernet too, so it is given as the device's, not as this Mac's."""
    where = "the device's network" + (f" (its Wi-Fi network is \u201c{wifi_name}\u201d)" if wifi_name else "")
    return (
        f"{this_computer(platform, capitalized=True)} left the device's network and is now on "
        f"{_now_on(joined)}. Reconnect it to {where}; the device is still restarting."
    )


def client_back_message(*, platform: str | None = None) -> str:
    return f"{this_computer(platform, capitalized=True)} is back on the device's network."


def client_left_message(joined: tuple[IpNetwork, ...], *, platform: str | None = None) -> str:
    return (
        f"{this_computer(platform, capitalized=True)} is on another network"
        f"{f' ({_network_names(joined)})' if joined else ''} and did not "
        "come back to the device's network while the device restarted, so TimeCapsuleSMB could not see the device "
        "come back."
    )


def restart_stuck_offline_hint(wifi_name: str | None, *, platform: str | None = None) -> str:
    """The unplug hint when this computer has no network at all: true when it
    reached the network through the device, by the device's own Wi-Fi or one
    of its LAN ports, whose link the restart takes down too."""
    wifi = f" network \u201c{wifi_name}\u201d" if wifi_name else ""
    return (
        f"{this_computer(platform, capitalized=True)} has no network connection, and the device has not come back. "
        f"If it reached the network through the device (its Wi-Fi{wifi} or one of its LAN ports), unplug the device, "
        "wait 10 seconds and plug it back in. TimeCapsuleSMB keeps waiting and carries on when it starts up."
    )


def restart_stuck_message(*, platform: str | None = None) -> str:
    """The error when the device stayed silent through the wait while this
    computer stayed on its network. The wait follows a device that moved
    only through Bonjour (services.locate), so on a network that drops
    multicast a device back at a new DHCP address looks the same. The steps
    (app.recovery) say what to do."""
    return (
        f"The device did not come back on this network after restarting, while {this_computer(platform)} stayed on "
        "it. The device may have stopped partway through the restart. If the network blocks Bonjour, it may "
        "instead be back at a new address."
    )


def unplug_if_on_network_note(wifi_name: str | None, *, platform: str | None = None) -> str:
    """What a wait that ended without knowing where this computer is adds:
    the unplug, only if it is on the device's network."""
    wifi = f" (its Wi-Fi network is \u201c{wifi_name}\u201d)" if wifi_name else ""
    return (
        f"If {this_computer(platform)} is on the device's network{wifi}, the device may have stopped partway through "
        "the restart: unplug it, wait 10 seconds and plug it back in."
    )


def _local_network_block(host: str, errors: Sequence[str], ssh: SshConnection | None):
    """checks.network.local_network_block for a wait whose last reads all got
    no route in the app: proven by Apple's ssh reaching the device, which
    macOS does not hold. Without an SSH connection (configure, enable SSH)
    nothing can prove it."""
    if ssh is None:
        return None
    try:
        reached = ssh_reached_device(probe_ssh_command_conn(ssh, "/bin/echo ok", timeout=SSH_PROOF_TIMEOUT_SECONDS))
    except TransportError:
        return None
    return local_network_block(host, errors, proof="ssh" if reached else None)


def _mark(ssh: SshConnection | None) -> str | None:
    """Mark the restart about to be requested: "written", or "unwritten",
    which only loses the hang check; None without SSH."""
    if ssh is None:
        return None
    return "written" if acpd.mark_restart(ssh) else "unwritten"


def followed(connection: SshConnection, moved: str | None) -> SshConnection:
    """`connection`, at the address reboot_device found the device at."""
    return connection if moved is None else replace(connection, host=moved)


def _read_uptime(host: str, password: str) -> tuple[int | None, str | None, acp.ACPError | None]:
    """The uptime and AirPort MAC at `host`, read in one request.

    The MAC is None when the device could not read waMA; the reboot is then
    proven at `host` only.
    """
    try:
        values = acp.get_properties(host, password, ("syUT", "waMA"), timeout=UPTIME_READ_TIMEOUT_SECONDS)
        uptime = values.get("syUT")
        if isinstance(uptime, acp.ACPError):
            raise uptime
        if uptime is None:
            raise acp.ACPPropertyError("ACP property syUT was not returned")
        mac = values.get("waMA")
        return (
            acp.property_uint32("syUT", uptime),
            normalize_airport_mac(mac.hex(":")) if isinstance(mac, bytes) else None,
            None,
        )
    except acp.ACPError as exc:
        return None, None, exc


_RESET_TEXTS = ("connection reset", "broken pipe")
# How integrations.acp words a failure after the connection was made, and an
# orderly close by the other end.
_AFTER_CONNECT_PREFIXES = ("ACP receive failed:", "ACP send failed:")
_CLOSED_PREFIX = "ACP connection closed while reading"


def _read_error_kind(error: acp.ACPError) -> str:
    if isinstance(error, acp.ACPAuthError):
        # A device reset to factory settings comes back with another password.
        return "auth"
    if isinstance(error, acp.ACPPropertyError):
        return "property"
    if isinstance(error, acp.ACPProtocolError):
        return "protocol"
    text = str(error)
    if any(needle in text.lower() for needle in _RESET_TEXTS):
        # The connection was made and then dropped by the other end, such as
        # ACPd closing it as it stops or starts: something answered.
        return "reset"
    if text.startswith(_CLOSED_PREFIX):
        # The same, closed in order (ACPd exiting closes its sockets).
        return "closed"
    if text.startswith(_AFTER_CONNECT_PREFIXES):
        # The device's TCP stack took the connection, so it is not silence,
        # but no reply came (a busy or stopping ACPd, a link that broke): no
        # answer either.
        return "no_reply"
    return connect_error_kind(text) or "other"


def _network_context(host: str) -> dict[str, object]:
    """How this computer reaches `host` when a wait fails.

    The computer's own networks can change during the reboot: telemetry
    install 00094b92 lost one of its two interfaces on the device's network,
    and nothing else showed whether the device or the computer went away.
    """
    try:
        return acp_diagnostics.probe_context_fields(host, None)
    except Exception as exc:
        return {"acp_diagnostics_error": f"{type(exc).__name__}: {exc}"[:200]}


def _request(host: str, password: str, callbacks: OperationCallbacks, *, raise_errors: bool) -> None:
    callbacks.stage("reboot")
    callbacks.update(reboot_was_attempted=True)
    callbacks.debug(reboot_request_strategy=REBOOT_STRATEGY)
    # The reboot drops the device's SSH connections. Close the shared one now
    # so the first command after the reboot logs in afresh.
    close_ssh_masters(host)
    started = time.monotonic()
    error: acp.ACPError | None = None
    # One request, never a second mechanism: a lost reply is only observed,
    # because ACPd may already be saving and shutting down.
    try:
        acp.reboot(host, password, timeout=ACP_REQUEST_TIMEOUT_SECONDS)
    except acp.ACPError as exc:
        error = exc
        callbacks.debug(acp_reboot_succeeded=False, acp_reboot_error=str(exc))
    else:
        callbacks.debug(acp_reboot_succeeded=True)
    callbacks.measurement(
        "reboot_request",
        strategy=REBOOT_STRATEGY,
        duration_sec=round(time.monotonic() - started, 3),
        result="success" if error is None else "failure",
        error_type=None if error is None else type(error).__name__,
    )
    if error is None:
        callbacks.message("ACP reboot requested.")
    elif raise_errors:
        code = ErrorCode.AUTH_FAILED if isinstance(error, acp.ACPAuthError) else ErrorCode.REMOTE_ERROR
        raise RebootFlowError(f"ACP reboot request failed: {error}", code=code) from error
    else:
        callbacks.message("ACP reboot request failed; checking whether the device is restarting anyway...")


# The restart wait's state machine (_wait drives it).
#
# Every poll the wait reads the uptime and feeds step() what it saw, one
# observation at a time, in this order:
#
#   Answered      the old boot answered; nothing else this poll
#   Read          the read failed: its kind (_read_error_kind)
#   Surroundings  this Mac's place, when place_due (every
#                 PLACE_READ_INTERVAL_SECONDS once the device has been down
#                 CLIENT_AWAY_HINT_AFTER_DOWN_SECONDS), and the networks it
#                 joined (_client_away) when the wait watches it; refusals
#                 are judged here
#   Look          the restart marker's look over SSH, only when look_due
#   StuckCheck    this poll's place again, and the SSH port's answer when
#                 stuck_candidate asked for it; the stuck hint is judged here
#   Located       a look for the device by its MAC that did not find it on a
#                 new boot, only when follow_due
#
# A new boot read at the address, or at one the look by MAC found, ends the
# wait outside the machine. verdict() decides every other ending: at the
# start limit while GOING_DOWN, and at the up limit once DOWN. _wait's show()
# says each transition: one progress event per hint that changes, in the
# order of the parts below, each carrying the parts said so far.
#
# The state has four parts that change on their own, so their hints can show
# together: AWAY beside STUCK or a hang, and STUCK beside a MARKER hang found
# by a later look.
#
#   phase   GOING_DOWN -Read-> DOWN -Answered-> GOING_DOWN
#           The up limit counts from the first unanswered read since the last
#           answer (down_since), or from a later request to unplug.
#   client  HOME -(away)-> LEAVING -(CLIENT_AWAY_POLLS polls away, the device
#           down CLIENT_AWAY_HINT_AFTER_DOWN_SECONDS)-> AWAY, which shows its
#           hint; any poll back on the device's network -> HOME. Away: on
#           networks the device is not on, or behind another router toward
#           it for CLIENT_AWAY_POLLS readings in a row. An unwatched wait
#           stays HOME.
#   hang    NONE -(ACP refused RESTART_STUCK_AFTER_DOWN_SECONDS, all of it
#           HOME)-> REFUSED -(silent: DeviceSilence.quiet)-> NONE;
#           NONE -(Look "hung")-> MARKER, which stays. Both ask for an unplug.
#   stuck   NONE -(silent, down RESTART_STUCK_AFTER_DOWN_SECONDS, place same
#           or offline, HOME, no hang, no rejected password, then the SSH
#           port silent too)-> SHOWN -(no longer silent)-> WITHDRAWN -(as from
#           NONE)-> SHOWN. stuck_offline is the showing hint's wording (this
#           Mac had no network): it follows the place while SHOWN and clears
#           on withdrawal.
#
# verdict() reads the parts as they are at the limit: AWAY ends as this Mac
# away, a hang as the hang, and the stuck ending needs the stuck hint SHOWN
# (not offline), the device still silent and this Mac HOME.
#
# Beside them: the silence record, polls away, router readings in a row,
# since when ACP has refused, when the first hang and the first stuck hint
# asked for an unplug (the limit counts from those only, so a device that
# keeps changing cannot hold the wait open), a rejected password, and the
# look and follow timers.


class Phase(Enum):
    GOING_DOWN = "going_down"
    DOWN = "down"


class Client(Enum):
    HOME = "home"
    LEAVING = "leaving"
    AWAY = "away"


class Hang(Enum):
    NONE = "none"
    REFUSED = "acp_refused"
    MARKER = "marker"


class Stuck(Enum):
    NONE = "none"
    SHOWN = "shown"
    WITHDRAWN = "withdrawn"


@dataclass(frozen=True)
class Answered:
    """The old boot answered the read."""


@dataclass(frozen=True)
class Read:
    """The read failed: _read_error_kind's kind (or "other_device"), and the error."""

    kind: str | None
    error: str


@dataclass(frozen=True)
class Surroundings:
    """This Mac's place, when it was read this poll (place_due), and the
    networks it joined (_client_away; None while it shares one, and always
    for a wait that does not watch it)."""

    place: PlaceComparison | None
    away: tuple[IpNetwork, ...] | None


@dataclass(frozen=True)
class Look:
    """The restart marker's look over SSH (look_due): "hung" or what else it saw."""

    look: str


@dataclass(frozen=True)
class StuckCheck:
    """This poll's place, if read, and the SSH port's answer (tcp_connect_error)
    when stuck_candidate asked for it (`probed`)."""

    place: PlaceComparison | None
    probed: bool = False
    ssh_error: str | None = None


@dataclass(frozen=True)
class Located:
    """A look for the device by its MAC (follow_due) that did not find it on
    a new boot; `rejected` when an AirPort with its MAC rejected the password."""

    rejected: LocateResult | None


Observation = Union[Answered, Read, Surroundings, Look, StuckCheck, Located]


@dataclass(frozen=True)
class WaitState:
    """Where the restart wait is (see the comment above Phase)."""

    started: float
    silence: DeviceSilence
    phase: Phase = Phase.GOING_DOWN
    # Whether the device has been seen down at all: the up stage began.
    went_down: bool = False
    # The first unanswered read since the last answer.
    down_since: float | None = None
    # The latest read's kind.
    read_kind: str | None = None
    client: Client = Client.HOME
    # Polls in a row on another network, and what this Mac joined.
    away_polls: int = 0
    joined: tuple[IpNetwork, ...] = ()
    # The latest place reading, and how many in a row put this Mac behind
    # another router toward the device.
    place_read_at: float | None = None
    router_moved_reads: int = 0
    hang: Hang = Hang.NONE
    # When the first hang asked for an unplug.
    hang_first_at: float | None = None
    # Since when every read has been refused while this Mac was HOME.
    refused_since: float | None = None
    next_hang_check: float = 0.0
    stuck: Stuck = Stuck.NONE
    # When the stuck hint first showed, and whether its latest showing was
    # the one for this Mac without a network.
    stuck_at: float | None = None
    stuck_offline: bool = False
    next_follow: float = 0.0
    rejected: LocateResult | None = None

    @property
    def limit_from(self) -> float:
        """The up limit counts from here: going down, or the first request to
        unplug (a hang's, the stuck hint's), whichever came last."""
        return max(at for at in (self.down_since, self.hang_first_at, self.stuck_at) if at is not None)

    def place_due(self, now: float) -> bool:
        """Whether this poll reads this Mac's place (for a wait that has one to
        compare): once the device has been down a while, at most every
        PLACE_READ_INTERVAL_SECONDS."""
        return (
            self.down_since is not None and now - self.down_since >= CLIENT_AWAY_HINT_AFTER_DOWN_SECONDS
            and (self.place_read_at is None or now - self.place_read_at >= PLACE_READ_INTERVAL_SECONDS)
        )

    def look_due(self, now: float) -> bool:
        """Whether this poll looks at the restart marker (for a wait that
        wrote one and has SSH)."""
        return (
            self.hang is Hang.NONE and now - self.started >= HANG_CHECK_AFTER_REQUEST_SECONDS
            and now >= self.next_hang_check
        )

    def stuck_candidate(self, place: PlaceComparison | None, now: float) -> bool:
        """Whether the stuck hint would show if the SSH port is silent too."""
        return (
            place is not None and place.verdict in ("same", "offline")
            and self.stuck is not Stuck.SHOWN and self.hang is Hang.NONE and self.away_polls == 0
            and self.rejected is None and self.silence.quiet
            and self.down_since is not None and now - self.down_since >= RESTART_STUCK_AFTER_DOWN_SECONDS
        )

    def follow_due(self, now: float) -> bool:
        """Whether this poll looks for the device by its MAC (for a wait that knows it)."""
        return (
            self.down_since is not None and now - self.down_since >= FOLLOW_AFTER_DOWN_SECONDS
            and now >= self.next_follow
        )


def step(state: WaitState, observation: Observation, now: float) -> WaitState:
    """The restart wait's state after `observation` at `now`."""
    if isinstance(observation, Answered):
        return replace(state, phase=Phase.GOING_DOWN, down_since=None, refused_since=None)
    if isinstance(observation, Read):
        silence = replace(state.silence)
        if observation.kind is not None:
            silence.observe_read(observation.kind, observation.error)
        return replace(
            state,
            phase=Phase.DOWN,
            went_down=True,
            down_since=now if state.down_since is None else state.down_since,
            read_kind=observation.kind,
            silence=silence,
        )
    if isinstance(observation, Surroundings):
        return _judge_refusals(_follow_client(state, observation.place, observation.away, now), now)
    if isinstance(observation, Look):
        state = replace(state, next_hang_check=now + HANG_CHECK_INTERVAL_SECONDS)
        return _hang(state, Hang.MARKER, now) if observation.look == "hung" else state
    if isinstance(observation, StuckCheck):
        return _judge_stuck(state, observation, now)
    state = replace(state, next_follow=now + FOLLOW_INTERVAL_SECONDS)
    return state if observation.rejected is None else replace(state, rejected=observation.rejected)


def _follow_client(
    state: WaitState, place: PlaceComparison | None, away: tuple[IpNetwork, ...] | None, now: float,
) -> WaitState:
    if place is not None:
        moved = place.verdict == "moved" and place.evidence == "router"
        state = replace(state, place_read_at=now, router_moved_reads=state.router_moved_reads + 1 if moved else 0)
    if away is None and state.router_moved_reads >= CLIENT_AWAY_POLLS:
        # Another network with the same addresses: only its router tells it
        # apart, so no network is named.
        away = ()
    if away is None:
        return replace(state, client=Client.HOME, away_polls=0)
    away_polls = state.away_polls + 1
    client = state.client
    if (
        client is not Client.AWAY and away_polls >= CLIENT_AWAY_POLLS
        and state.down_since is not None and now - state.down_since >= CLIENT_AWAY_HINT_AFTER_DOWN_SECONDS
    ):
        client = Client.AWAY
    elif client is Client.HOME:
        client = Client.LEAVING
    return replace(state, client=client, away_polls=away_polls, joined=away)


def _hang(state: WaitState, hang: Hang, now: float) -> WaitState:
    return replace(state, hang=hang, hang_first_at=now if state.hang_first_at is None else state.hang_first_at)


def _judge_refusals(state: WaitState, now: float) -> WaitState:
    # Refusals count only while this Mac is on the device's network: on
    # another one with the same addresses, another host refuses.
    if state.read_kind != "refused" or state.away_polls:
        state = replace(state, refused_since=None)
    elif state.refused_since is None:
        state = replace(state, refused_since=now)
    if (
        state.hang is Hang.NONE and state.refused_since is not None
        and now - state.refused_since >= RESTART_STUCK_AFTER_DOWN_SECONDS
    ):
        # ACP refused for this long: the kernel is up and ACPd is gone, as
        # in the hang the marker look finds (a restart that finishes
        # listens again seconds after its network comes up). It also
        # covers the restart that turns SSH on, which has no SSH to look
        # with. Another host that took the address refuses too, but a
        # device that came back elsewhere is followed by its MAC first.
        return _hang(state, Hang.REFUSED, now)
    if state.hang is Hang.REFUSED and state.silence.quiet:
        # The refusals gave way to silence: the restart's teardown started
        # after all, or the device was unplugged. What comes next is
        # judged as any other restart's, and the limit still counts from
        # the first hang, so refusals and silence in turns cannot hold the
        # wait open.
        return replace(state, hang=Hang.NONE)
    return state


def _judge_stuck(state: WaitState, check: StuckCheck, now: float) -> WaitState:
    place = check.place
    if state.stuck is Stuck.SHOWN and not state.silence.quiet:
        # Something answers at the address again, such as ACPd refusing
        # while the device starts up: an unplug now would interrupt it.
        # Its wording goes with it, so no ending reads a hint that is gone.
        return replace(state, stuck=Stuck.WITHDRAWN, stuck_offline=False)
    if (
        state.stuck is Stuck.SHOWN and place is not None and place.verdict in ("same", "offline")
        and (place.verdict == "offline") != state.stuck_offline
    ):
        # This Mac lost or got back its network connection: the hint's
        # wording follows it.
        return replace(state, stuck_offline=place.verdict == "offline")
    if not check.probed or place is None:
        return state
    silence = replace(state.silence)
    silence.observe_ssh_port(check.ssh_error)
    state = replace(state, silence=silence)
    if not silence.quiet:
        return state
    # Shown again after the device answered, the hint does not restart the
    # limit: a device that keeps answering and falling silent cannot hold
    # the wait open.
    return replace(
        state, stuck=Stuck.SHOWN, stuck_offline=place.verdict == "offline",
        stuck_at=now if state.stuck_at is None else state.stuck_at,
    )


@dataclass(frozen=True)
class WaitEnding:
    """How a restart wait that did not see the new boot ends: its
    reboot_cycle result, error code, message and any telemetry it adds."""

    result: str
    code: ErrorCode
    message: str
    fields: tuple[tuple[str, object], ...] = ()


@dataclass(frozen=True)
class WaitTerms:
    """The caller's limits and words for verdict."""

    start_timeout_seconds: int
    up_timeout_seconds: int
    no_down_message: str
    up_timeout_message: str
    stopped_message: str
    stuck_message: str
    wifi_name: str | None


def verdict(
    state: WaitState, now: float, words: WaitTerms,
    block: Callable[[list[str]], LocalNetworkBlock | None] = lambda _errors: None,
) -> WaitEnding | None:
    """How the wait ends at `now`, or None while it goes on. `block` proves a
    Local Network block from the last no route errors (_local_network_block),
    asked only when nothing else decides first."""
    if state.phase is Phase.GOING_DOWN:
        if now - state.started >= words.start_timeout_seconds:
            return WaitEnding("did_not_go_down", ErrorCode.REBOOT_NOT_STARTED, words.no_down_message)
        return None
    # The user gets the whole limit to unplug the device and for it to start up.
    if now - state.limit_from < words.up_timeout_seconds:
        return None
    if state.client is Client.AWAY:
        # Nothing showed whether the device came back: this Mac was
        # not on its network to see it.
        return WaitEnding("client_left_network", ErrorCode.CLIENT_LEFT_NETWORK, client_left_message(state.joined))
    if state.hang is not Hang.NONE:
        message = words.stopped_message
        if state.hang is Hang.REFUSED:
            # The caller's message opens with ACPD_STOPPED_MESSAGE
            # (deploy adds what that means for the install).
            message = message.replace(ACPD_STOPPED_MESSAGE, ACPD_REFUSED_MESSAGE, 1)
        return WaitEnding("acpd_stopped_without_reboot", ErrorCode.ACPD_BROKEN, message)
    silence = state.silence
    if (
        state.stuck is Stuck.SHOWN and not state.stuck_offline and state.client is Client.HOME and silence.quiet
    ):
        # The hint stands, the device is still silent and this Mac is on its
        # network, as the ending says. A Mac that has started leaving, or a
        # hint withdrawn, gets the ending below, which does not say where
        # this Mac stayed.
        return WaitEnding("restart_stuck", ErrorCode.RESTART_STUCK, words.stuck_message)
    if silence.maybe_blocked:
        # The app's reads all got no route at the end: macOS may be
        # refusing its connections to a device that is up.
        found = block(silence.no_route_errors)
        if found is not None:
            return WaitEnding(
                "local_network_blocked", found.code, found.message, (("local_network_block_proof", found.proof),),
            )
    if state.rejected is not None:
        message = f"{words.up_timeout_message} {state.rejected.rejected_note}"
    elif silence.quiet:
        message = f"{words.up_timeout_message} {unplug_if_on_network_note(words.wifi_name)}"
    else:
        # Something answered at the address lately, or macOS may have
        # refused the reads: not a device known to be off.
        message = words.up_timeout_message
    return WaitEnding("did_not_come_back_up", ErrorCode.REBOOT_NOT_FINISHED, message)


def _wait(
    host: str,
    password: str,
    u0: int,
    started: float,
    *,
    airport_mac: str | None,
    callbacks: OperationCallbacks,
    expect_ssh: bool,
    start_timeout_seconds: int,
    up_timeout_seconds: int,
    no_down_message: str,
    up_timeout_message: str,
    ssh_closed_message: str,
    stopped_message: str,
    stuck_message: str,
    ssh: SshConnection | None,
    restart_marker: str | None,
    watched_link: NetworkLinkResult | None = None,
    device_is_gateway: bool | None = None,
    wifi_name: str | None = None,
    place_before: NetworkPlace | None = None,
) -> str | None:
    """Wait for the new boot: gather each poll's observations for the state
    machine (step), say what its transitions show, and end as verdict says."""
    fields: dict[str, object] = {
        "start_timeout_sec": start_timeout_seconds,
        "up_timeout_sec": up_timeout_seconds,
        "expect_ssh": expect_ssh,
        "u0_sec": u0,
        "client_watched": watched_link is not None,
    }
    if device_is_gateway is not None:
        fields["device_is_gateway"] = device_is_gateway
    if restart_marker is not None:
        fields["restart_marker"] = restart_marker
    # Only a watched device has networks to compare a place against.
    device_networks = watched_link.device_networks if watched_link is not None else ()
    if place_before is not None:
        fields["routers_before"] = [
            {"interface": router.interface, "ip": router.ip, "mac": router.mac} for router in place_before.routers
        ]
    terms = WaitTerms(
        start_timeout_seconds=start_timeout_seconds,
        up_timeout_seconds=up_timeout_seconds,
        no_down_message=no_down_message,
        up_timeout_message=up_timeout_message,
        stopped_message=stopped_message,
        stuck_message=stuck_message,
        wifi_name=wifi_name,
    )
    moved: str | None = None
    # Each kind of failed read, and when it was first and last seen: refused
    # means something answers at the address but not ACPd, host down or no
    # route that nothing answers there at all.
    read_errors: dict[str, dict[str, object]] = {}

    def finish(result: str, code: str | None = None, message: str = "") -> None:
        fields["total_wait_duration_sec"] = round(time.monotonic() - started, 3)
        if read_errors:
            fields["read_errors"] = read_errors
        if code is not None:
            # A failure is reported at the address the wait began with: a
            # follow changes `host` only when it succeeds.
            fields.update(_network_context(host))
            if place_before is not None:
                fields.update(_compare_now(
                    place_before, device_networks, device_is_router=bool(device_is_gateway),
                ).telemetry())
        callbacks.measurement("reboot_cycle", result=result, **fields)
        if code is not None:
            raise RebootFlowError(message, code=code, result=result)

    def end(ending: WaitEnding | None) -> None:
        if ending is not None:
            fields.update(ending.fields)
            finish(ending.result, ending.code, ending.message)

    def is_new_boot(uptime: int | None, read_started: float) -> bool:
        return uptime is not None and uptime + UPTIME_SLACK_SECONDS < u0 + (read_started - started)

    def hints(state: WaitState, *, cleared: bool = False) -> None:
        """Every hint's state in one progress event. The app shows the latest
        event of the step on its own, so one that left a hint out would hide
        it. `cleared`: the device came back, and no hint stands."""
        stuck = state.stuck is Stuck.SHOWN and not cleared
        callbacks.progress(
            "wait_for_reboot_up",
            client_away=state.client is Client.AWAY and not cleared,
            network_name=wifi_name or "",
            acpd_hung=state.hang is not Hang.NONE and not cleared,
            restart_stuck=stuck,
            client_offline=stuck and state.stuck_offline,
        )

    # The stuck hint's lines printed so far. A device that keeps answering and
    # falling silent shows and withdraws the hint again and again: the app's
    # one progress line follows it, and a terminal prints each change until
    # the second withdrawal, then one line that holds whatever comes next, so
    # its last line never contradicts the wait.
    told: list[str] = []

    def tell(message: str) -> None:
        if told.count(RESTART_STUCK_WITHDRAWN_MESSAGE) >= 2:
            message = RESTART_STUCK_FLAPPING_MESSAGE
        if not told or told[-1] != message:
            told.append(message)
            callbacks.message(message)

    def show(before: WaitState, after: WaitState, now: float) -> None:
        """Say what changed from `before` to `after`, one part of the state
        at a time, each hint event showing the parts said so far."""
        if after.down_since is not None and before.down_since is None:
            fields.setdefault("down_seen_after_sec", round(now - started, 3))
        if after.went_down and not before.went_down:
            callbacks.message("Device went down; waiting for it to come back up...")
            callbacks.stage("wait_for_reboot_up")
        if after.away_polls and not before.away_polls:
            fields["client_leaves"] = int(fields.get("client_leaves", 0)) + 1
            fields.setdefault("client_left_after_sec", round(now - started, 3))
        elif before.away_polls and not after.away_polls:
            fields["client_rejoined_after_sec"] = round(now - started, 3)
        view = replace(before, client=after.client, joined=after.joined)
        if (after.client is Client.AWAY) != (before.client is Client.AWAY):
            hints(view)
            if after.client is Client.AWAY:
                fields["client_hinted"] = True
                callbacks.message(client_away_message(after.joined, wifi_name))
            else:
                callbacks.message(client_back_message())
        view = replace(view, hang=after.hang)
        if after.hang is not before.hang:
            if after.hang is Hang.NONE:
                fields["acpd_hang_cleared_after_sec"] = round(now - started, 3)
                hints(view)
            else:
                fields["acpd_hang_seen_after_sec"] = round(now - started, 3)
                fields["acpd_hang_evidence"] = after.hang.value
                hints(view)
                callbacks.message(ACPD_REFUSED_UNPLUG_MESSAGE if after.hang is Hang.REFUSED else UNPLUG_MESSAGE)
        view = replace(view, stuck=after.stuck, stuck_offline=after.stuck_offline)
        stuck_line = restart_stuck_offline_hint(wifi_name) if after.stuck_offline else RESTART_STUCK_HINT
        if before.stuck is Stuck.SHOWN and after.stuck is Stuck.WITHDRAWN:
            fields["stuck_hint_cleared_after_sec"] = round(now - started, 3)
            hints(view)
            tell(RESTART_STUCK_WITHDRAWN_MESSAGE)
        elif after.stuck is Stuck.SHOWN and before.stuck is not Stuck.SHOWN:
            if before.stuck_at is None:
                fields["stuck_hint_after_sec"] = round(now - started, 3)
                fields["stuck_hint_place"] = "offline" if after.stuck_offline else "same"
            hints(view)
            tell(stuck_line)
        elif after.stuck is Stuck.SHOWN and after.stuck_offline != before.stuck_offline:
            hints(view)
            tell(stuck_line)

    callbacks.message("Waiting for the device to restart...")
    callbacks.stage("wait_for_reboot_down")
    state = WaitState(started=started, silence=DeviceSilence(held=macos_may_hold_this_process()))
    can_look = ssh is not None and restart_marker == "written"
    asked_ssh = False

    def advance(observation: Observation, now: float) -> None:
        nonlocal state
        before, state = state, step(state, observation, now)
        show(before, state, now)

    while True:
        time.sleep(REBOOT_POLL_SECONDS)
        read_started = time.monotonic()
        uptime, mac, error = _read_uptime(host, password)
        now = time.monotonic()
        read_kind: str | None = None
        if error is not None:
            read_kind = _read_error_kind(error)
            fields["last_read_error"] = str(error)
            seen = read_errors.setdefault(read_kind, {"count": 0, "first_sec": round(read_started - started, 3)})
            seen["count"] = int(seen["count"]) + 1
            seen["last_sec"] = round(read_started - started, 3)
        if airport_mac and mac and mac != airport_mac:
            # Another AirPort took this address: the device itself is not here.
            uptime = None
            read_kind = "other_device"
            fields["other_device_at_address"] = True
        if is_new_boot(uptime, read_started):
            break
        if uptime is not None:
            advance(Answered(), now)
            end(verdict(state, now, terms))
            continue
        advance(Read(read_kind, str(error)), now)
        # This poll's place, when it is one of the regular readings.
        place: PlaceComparison | None = None
        if place_before is not None and state.place_due(now):
            place = _compare_now(place_before, device_networks, device_is_router=bool(device_is_gateway))
            if place.verdict == "moved" and place.evidence == "router":
                fields["router_mac_away"] = place.router_now
        advance(Surroundings(place, _client_away(watched_link) if watched_link is not None else None), now)
        if can_look and state.look_due(now):
            fields["restart_looks"] = int(fields.get("restart_looks", 0)) + 1
            # The port check ends at once where SSH to a device that is off would
            # wait out the marker read's timeout. A read that fails with the port
            # open shows nothing either way.
            if not tcp_open(host, SSH_PORT):
                look = "ssh_closed"
            else:
                asked_ssh = True
                try:
                    restart = acpd.restart_verdict(ssh)
                except TransportError:
                    look = "ssh_failed"
                else:
                    look = restart.state
                    # What the latest look saw, for a restart that outlives the grace.
                    fields.update(restart.details)
            fields["restart_look_last"] = look
            advance(Look(look), now)
        # Judged on the regular place readings only, so the routers are read no
        # more often than PLACE_READ_INTERVAL_SECONDS.
        if state.stuck_candidate(place, now):
            ssh_error = tcp_connect_error(host, SSH_PORT, PORT_CHECK_TIMEOUT_SECONDS)
            if connect_answered(ssh_error):
                fields["stuck_ssh_port"] = "open" if ssh_error is None else "refused"
            advance(StuckCheck(place, probed=True, ssh_error=ssh_error), now)
        else:
            advance(StuckCheck(place), now)
        if airport_mac and state.follow_due(now):
            # One read per candidate: a device still booting fails every read,
            # and the next look is FOLLOW_INTERVAL_SECONDS away.
            located = locate_airport(
                airport_mac, password, current_host=host, trigger="reboot_wait", callbacks=callbacks, attempts=1,
            )
            if located.host is not None and located.outcome != "password_rejected":
                candidate = endpoint_host(located.host)
                read_started = time.monotonic()
                uptime, _mac, _error = _read_uptime(candidate, password)
                if is_new_boot(uptime, read_started):
                    host, moved = candidate, located.host
                    now = time.monotonic()
                    fields["followed"] = True
                    fields["follow_after_sec"] = round(now - started, 3)
                    callbacks.message(f"The device came back at {candidate}; the saved address is out of date.")
                    callbacks.update(current_host=located.host)
                    break
            advance(Located(located if located.outcome == "password_rejected" else None), now)
        end(verdict(state, now, terms, lambda errors: _local_network_block(host, errors, ssh)))

    fields["reset_seen_after_sec"] = round(now - started, 3)
    if state.away_polls:
        # The device answered, so this Mac is back on its network.
        fields["client_rejoined_after_sec"] = round(now - started, 3)
    if state.client is Client.AWAY or state.stuck is Stuck.SHOWN or state.hang is not Hang.NONE:
        # The device came back, after an unplug or by itself, and this Mac is
        # on its network: no hint stands.
        hints(state, cleared=True)
    if state.client is Client.AWAY:
        callbacks.message(client_back_message())
    if asked_ssh:
        # A look may have opened a shared SSH connection to the old kernel,
        # which the restart left dead: the next command logs in afresh.
        close_ssh_masters(endpoint_host(ssh.host))
    fields["uptime_at_return_sec"] = uptime
    if not state.went_down:
        callbacks.stage("wait_for_reboot_up")
    if expect_ssh:
        fields["ssh_timeout_sec"] = REBOOT_SSH_TIMEOUT_SECONDS
        deadline = now + REBOOT_SSH_TIMEOUT_SECONDS
        while not tcp_open(host, SSH_PORT):
            if time.monotonic() >= deadline:
                finish("ssh_not_open", ErrorCode.REBOOT_NOT_FINISHED, ssh_closed_message)
            time.sleep(REBOOT_POLL_SECONDS)
    else:
        # The device's uptime now is the reading plus the time since it.
        time.sleep(max(0.0, SSH_CLOSED_CHECK_UPTIME_SECONDS - uptime - (time.monotonic() - now)))
        # A lost connection attempt also reads as closed, so SSH counts as
        # off only when a second check a poll later agrees.
        for attempt in range(2):
            if attempt:
                time.sleep(REBOOT_POLL_SECONDS)
            if tcp_open(host, SSH_PORT):
                finish("ssh_still_open", ErrorCode.SSH_STILL_ENABLED, SSH_STILL_OPEN_MESSAGE)
    fields["ssh_ready_after_sec"] = round(time.monotonic() - started, 3)
    callbacks.update(device_came_back_after_reboot=True)
    callbacks.message("Device is back online.")
    finish("success")
    return moved
