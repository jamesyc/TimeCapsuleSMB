from __future__ import annotations

import ipaddress
import time
from collections.abc import Sequence
from dataclasses import replace

from timecapsulesmb.checks.network import (
    SSH_PORT,
    IpNetwork,
    NetworkLinkResult,
    classify_network_link,
    host_networks,
    local_lan_networks,
    reportable_network,
)
from timecapsulesmb.core.error_codes import CodedError, ErrorCode
from timecapsulesmb.core.install import this_computer
from timecapsulesmb.core.net import endpoint_host
from timecapsulesmb.device import acpd
from timecapsulesmb.discovery.models import normalize_airport_mac
from timecapsulesmb.integrations import acp
from timecapsulesmb.services import acp_diagnostics
from timecapsulesmb.services.callbacks import OperationCallbacks
from timecapsulesmb.services.locate import LocateResult, locate_airport
from timecapsulesmb.transport.errors import TransportError
from timecapsulesmb.transport.local import connect_error_kind, mac_default_gateway, tcp_open
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
UNPLUG_MESSAGE = (
    "Apple's AirPort service stopped without restarting the device. Unplug the device, wait 10 seconds "
    "and plug it back in. TimeCapsuleSMB keeps waiting and carries on when it starts up."
)
# The device's restart drops this Mac's link to it, and macOS can then join
# another known network (a neighbour's Wi-Fi, an iPhone's hotspot) and stay
# there. Most Macs are back within about 70 s of the request and the slowest
# tenth after about 136 s, so the hint waits this long after the device went
# down, and for this many polls in a row on another network.
CLIENT_AWAY_HINT_AFTER_DOWN_SECONDS = 180
CLIENT_AWAY_POLLS = 2
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
ACPD_STOPPED_MESSAGE = acpd.acpd_broken_message(
    "the AirPort service on this device stopped but did not restart the device",
)


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
SSH_ENABLE_NO_RETURN_MESSAGE = "The device did not answer within 10 minutes of the restart that turns on SSH."
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
    wait also watches for it leaving that network (_client_away), and names
    `wifi_name`, the device's Wi-Fi network (its Bonjour raNm), in the hint.
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
    # Before the request: a router-mode device's restart takes this route away.
    device_is_gateway = _device_is_gateway(device_networks)
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
        ssh=ssh,
        restart_marker=restart_marker,
        watched_link=watched,
        device_is_gateway=device_is_gateway,
        wifi_name=wifi_name,
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


def _device_is_gateway(device_networks: list[IpNetwork] | None) -> bool | None:
    """Whether the device is this Mac's default router, as a Time Capsule in
    router mode is: its restart then takes this Mac's whole connection, which
    telemetry weighs against how often the Mac leaves. None when unknown."""
    gateway = mac_default_gateway()
    if gateway is None or device_networks is None:
        return None
    return any(str(network.network_address) == gateway.partition("%")[0] for network in device_networks)


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


def client_away_message(joined: tuple[IpNetwork, ...], wifi_name: str | None, *, platform: str | None = None) -> str:
    """The hint once this Mac has been on another network for a while: which
    one, and where to go back to. The device's Wi-Fi name fits a Mac on its
    Ethernet too, so it is given as the device's, not as this Mac's."""
    where = "the device's network" + (f" (its Wi-Fi network is \u201c{wifi_name}\u201d)" if wifi_name else "")
    return (
        f"{this_computer(platform, capitalized=True)} left the device's network and is now on "
        f"{_network_names(joined)}. Reconnect it to {where}; the device is still restarting."
    )


def client_back_message(*, platform: str | None = None) -> str:
    return f"{this_computer(platform, capitalized=True)} is back on the device's network."


def client_left_message(joined: tuple[IpNetwork, ...], *, platform: str | None = None) -> str:
    return (
        f"{this_computer(platform, capitalized=True)} is on another network ({_network_names(joined)}) and did not "
        "come back to the device's network while the device restarted, so TimeCapsuleSMB could not see the device "
        "come back."
    )


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


def _read_error_kind(error: acp.ACPError) -> str:
    if isinstance(error, acp.ACPAuthError):
        # A device reset to factory settings comes back with another password.
        return "auth"
    if isinstance(error, acp.ACPPropertyError):
        return "property"
    if isinstance(error, acp.ACPProtocolError):
        return "protocol"
    return connect_error_kind(str(error)) or "other"


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
    ssh: SshConnection | None,
    restart_marker: str | None,
    watched_link: NetworkLinkResult | None = None,
    device_is_gateway: bool | None = None,
    wifi_name: str | None = None,
) -> str | None:
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
    moved: str | None = None
    rejected: LocateResult | None = None
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
        callbacks.measurement("reboot_cycle", result=result, **fields)
        if code is not None:
            raise RebootFlowError(message, code=code, result=result)

    def is_new_boot(uptime: int | None, read_started: float) -> bool:
        return uptime is not None and uptime + UPTIME_SLACK_SECONDS < u0 + (read_started - started)

    callbacks.message("Waiting for the device to restart...")
    callbacks.stage("wait_for_reboot_down")
    down_since: float | None = None
    next_follow = 0.0
    next_hang_check = 0.0
    # When a look found the hang and the user was asked to unplug the device.
    hang_seen_at: float | None = None
    asked_ssh = False
    up_stage = False
    # Polls in a row with this Mac on another network, what it joined, and
    # whether the hint is showing.
    away_polls = 0
    joined: tuple[IpNetwork, ...] = ()
    hinting = False
    while True:
        time.sleep(REBOOT_POLL_SECONDS)
        read_started = time.monotonic()
        uptime, mac, error = _read_uptime(host, password)
        now = time.monotonic()
        if error is not None:
            fields["last_read_error"] = str(error)
            seen = read_errors.setdefault(_read_error_kind(error), {"count": 0, "first_sec": round(read_started - started, 3)})
            seen["count"] = int(seen["count"]) + 1
            seen["last_sec"] = round(read_started - started, 3)
        if airport_mac and mac and mac != airport_mac:
            # Another AirPort took this address: the device itself is not here.
            uptime = None
            fields["other_device_at_address"] = True
        if is_new_boot(uptime, read_started):
            break
        if uptime is not None:
            down_since = None
            if now - started >= start_timeout_seconds:
                finish("did_not_go_down", ErrorCode.REBOOT_NOT_STARTED, no_down_message)
            continue
        if down_since is None:
            down_since = now
            fields.setdefault("down_seen_after_sec", round(now - started, 3))
        if not up_stage:
            up_stage = True
            callbacks.message("Device went down; waiting for it to come back up...")
            callbacks.stage("wait_for_reboot_up")
        if watched_link is not None:
            away = _client_away(watched_link)
            if away is not None:
                if not away_polls:
                    fields["client_leaves"] = int(fields.get("client_leaves", 0)) + 1
                away_polls += 1
                joined = away
                fields.setdefault("client_left_after_sec", round(now - started, 3))
                if (
                    not hinting and away_polls >= CLIENT_AWAY_POLLS
                    and now - down_since >= CLIENT_AWAY_HINT_AFTER_DOWN_SECONDS
                ):
                    hinting = True
                    fields["client_hinted"] = True
                    callbacks.progress(
                        "wait_for_reboot_up", client_away=True, network_name=wifi_name or "",
                        acpd_hung=hang_seen_at is not None,
                    )
                    callbacks.message(client_away_message(joined, wifi_name))
            else:
                if away_polls:
                    fields["client_rejoined_after_sec"] = round(now - started, 3)
                away_polls = 0
                if hinting:
                    hinting = False
                    callbacks.progress("wait_for_reboot_up", client_away=False, acpd_hung=hang_seen_at is not None)
                    callbacks.message(client_back_message())
        if (
            ssh is not None and restart_marker == "written" and hang_seen_at is None
            and now - started >= HANG_CHECK_AFTER_REQUEST_SECONDS and now >= next_hang_check
        ):
            next_hang_check = now + HANG_CHECK_INTERVAL_SECONDS
            fields["restart_looks"] = int(fields.get("restart_looks", 0)) + 1
            # The port check ends at once where SSH to a device that is off would
            # wait out the marker read's timeout. A read that fails with the port
            # open shows nothing either way.
            if not tcp_open(host, SSH_PORT):
                look = "ssh_closed"
            else:
                asked_ssh = True
                try:
                    verdict = acpd.restart_verdict(ssh)
                except TransportError:
                    look = "ssh_failed"
                else:
                    look = verdict.state
                    # What the latest look saw, for a restart that outlives the grace.
                    fields.update(verdict.details)
            fields["restart_look_last"] = look
            if look == "hung":
                hang_seen_at = now
                fields["acpd_hang_seen_after_sec"] = round(now - started, 3)
                callbacks.progress("wait_for_reboot_up", acpd_hung=True)
                callbacks.message(UNPLUG_MESSAGE)
        if airport_mac and now - down_since >= FOLLOW_AFTER_DOWN_SECONDS and now >= next_follow:
            next_follow = now + FOLLOW_INTERVAL_SECONDS
            # One read per candidate: a device still booting fails every read,
            # and the next look is FOLLOW_INTERVAL_SECONDS away.
            located = locate_airport(
                airport_mac, password, current_host=host, trigger="reboot_wait", callbacks=callbacks, attempts=1,
            )
            if located.outcome == "password_rejected":
                rejected = located
            elif located.host is not None:
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
        # The user gets the whole limit to unplug the device and for it to start up.
        limit_from = down_since if hang_seen_at is None else max(down_since, hang_seen_at)
        if now - limit_from >= up_timeout_seconds:
            if away_polls >= CLIENT_AWAY_POLLS:
                # Nothing showed whether the device came back: this Mac was
                # not on its network to see it.
                finish("client_left_network", ErrorCode.CLIENT_LEFT_NETWORK, client_left_message(joined))
            if hang_seen_at is not None:
                finish("acpd_stopped_without_reboot", ErrorCode.ACPD_BROKEN, stopped_message)
            message = up_timeout_message if rejected is None else f"{up_timeout_message} {rejected.rejected_note}"
            finish("did_not_come_back_up", ErrorCode.REBOOT_NOT_FINISHED, message)

    fields["reset_seen_after_sec"] = round(now - started, 3)
    if away_polls:
        # The device answered, so this Mac is back on its network.
        fields["client_rejoined_after_sec"] = round(now - started, 3)
    if hinting:
        callbacks.progress("wait_for_reboot_up", client_away=False)
        callbacks.message(client_back_message())
    if hang_seen_at is not None:
        # The device came back: the step no longer asks for an unplug.
        callbacks.progress("wait_for_reboot_up", acpd_hung=False)
    if asked_ssh:
        # A look may have opened a shared SSH connection to the old kernel,
        # which the restart left dead: the next command logs in afresh.
        close_ssh_masters(endpoint_host(ssh.host))
    fields["uptime_at_return_sec"] = uptime
    if not up_stage:
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
