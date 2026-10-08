from __future__ import annotations

import time
from dataclasses import replace

from timecapsulesmb.core.net import endpoint_host
from timecapsulesmb.discovery.models import normalize_airport_mac
from timecapsulesmb.integrations import acp
from timecapsulesmb.services.callbacks import OperationCallbacks
from timecapsulesmb.services.locate import LocateResult, locate_airport
from timecapsulesmb.transport.local import tcp_open
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
SSH_PORT = 22
# A device that is down this long may have come back at another DHCP address
# (51 of 259 installs whose wait timed out since v3.0 later reached it at a new
# address), so it is looked for by its AirPort MAC from then on. The new kernel
# starts 47-175 s after the request, so earlier looks find nothing.
FOLLOW_AFTER_DOWN_SECONDS = 60
FOLLOW_INTERVAL_SECONDS = 30

REBOOT_NO_DOWN_MESSAGE = "Reboot was requested but the device did not restart."
REBOOT_UP_TIMEOUT_MESSAGE = "Timed out waiting for SSH after reboot."
SSH_STILL_OPEN_MESSAGE = "SSH reopened after reboot. Disable did not persist."


class RebootFlowError(RuntimeError):
    """A reboot that failed or could not be proven; `code` is the app's error code."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


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
) -> str | None:
    """Reboot the device through ACP and, with `wait`, prove it rebooted.

    The new boot must answer ACP within `up_timeout_seconds` of the first
    unanswered read. Then SSH must open within REBOOT_SSH_TIMEOUT_SECONDS of
    the new boot being seen (`expect_ssh`), or must stay closed.
    Returns the device's SSH target when the new boot came up at another
    address (see services.locate), else None. Raises RebootFlowError.
    """
    callbacks = callbacks or OperationCallbacks()
    host = endpoint_host(host)
    if not wait:
        _request(host, password, callbacks, raise_errors=True)
        return None
    u0, airport_mac, error = _read_uptime(host, password)
    if u0 is None:
        code = "auth_failed" if isinstance(error, acp.ACPAuthError) else "device_unreachable"
        raise RebootFlowError(f"Could not read the device's uptime through AirPort ACP before rebooting: {error}", code)
    started = time.monotonic()
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
    )


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
        code = "auth_failed" if isinstance(error, acp.ACPAuthError) else "remote_error"
        raise RebootFlowError(f"ACP reboot request failed: {error}", code) from error
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
) -> str | None:
    fields: dict[str, object] = {
        "start_timeout_sec": start_timeout_seconds,
        "up_timeout_sec": up_timeout_seconds,
        "expect_ssh": expect_ssh,
        "u0_sec": u0,
    }
    moved: str | None = None
    rejected: LocateResult | None = None

    def finish(result: str, code: str | None = None, message: str = "") -> None:
        fields["total_wait_duration_sec"] = round(time.monotonic() - started, 3)
        callbacks.measurement("reboot_cycle", result=result, **fields)
        if code is not None:
            raise RebootFlowError(message, code)

    def is_new_boot(uptime: int | None, read_started: float) -> bool:
        return uptime is not None and uptime + UPTIME_SLACK_SECONDS < u0 + (read_started - started)

    callbacks.message("Waiting for the device to restart...")
    callbacks.stage("wait_for_reboot_down")
    down_since: float | None = None
    next_follow = 0.0
    up_stage = False
    while True:
        time.sleep(REBOOT_POLL_SECONDS)
        read_started = time.monotonic()
        uptime, mac, error = _read_uptime(host, password)
        now = time.monotonic()
        if error is not None:
            fields["last_read_error"] = str(error)
        if airport_mac and mac and mac != airport_mac:
            # Another AirPort took this address: the device itself is not here.
            uptime = None
            fields["other_device_at_address"] = True
        if is_new_boot(uptime, read_started):
            break
        if uptime is not None:
            down_since = None
            if now - started >= start_timeout_seconds:
                finish("did_not_go_down", "reboot_not_started", no_down_message)
            continue
        if down_since is None:
            down_since = now
            fields.setdefault("down_seen_after_sec", round(now - started, 3))
        if not up_stage:
            up_stage = True
            callbacks.message("Device went down; waiting for it to come back up...")
            callbacks.stage("wait_for_reboot_up")
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
        if now - down_since >= up_timeout_seconds:
            message = up_timeout_message if rejected is None else f"{up_timeout_message} {rejected.rejected_note}"
            finish("did_not_come_back_up", "reboot_not_finished", message)

    fields["reset_seen_after_sec"] = round(now - started, 3)
    fields["uptime_at_return_sec"] = uptime
    if not up_stage:
        callbacks.stage("wait_for_reboot_up")
    if expect_ssh:
        fields["ssh_timeout_sec"] = REBOOT_SSH_TIMEOUT_SECONDS
        deadline = now + REBOOT_SSH_TIMEOUT_SECONDS
        while not tcp_open(host, SSH_PORT):
            if time.monotonic() >= deadline:
                finish("ssh_not_open", "reboot_not_finished", up_timeout_message)
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
                finish("ssh_still_open", "ssh_still_enabled", SSH_STILL_OPEN_MESSAGE)
    fields["ssh_ready_after_sec"] = round(time.monotonic() - started, 3)
    callbacks.update(device_came_back_after_reboot=True)
    callbacks.message("Device is back online.")
    finish("success")
    return moved
