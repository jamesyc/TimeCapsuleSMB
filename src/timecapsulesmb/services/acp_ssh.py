from __future__ import annotations

from collections.abc import Callable
import os
import sys
import time

from timecapsulesmb.checks.network import IpNetwork, classify_network_link, host_networks, local_lan_networks, reportable_network
from timecapsulesmb.discovery.bonjour import BonjourResolvedService
from timecapsulesmb.integrations.acp import (
    ACP_PORT,
    ACPAuthError,
    ACPConnectionError,
    ACPError,
    DBUG_SSH_VALUE,
    set_dbug,
)
from timecapsulesmb.services import acp_diagnostics
from timecapsulesmb.services.callbacks import OperationCallbacks
from timecapsulesmb.transport.local import tcp_connect_error


# The dashboard groups enable timeouts by this message's text.
SSH_ENABLE_TIMEOUT_MESSAGE = "SSH did not open after enabling via ACP."
ACP_PORT_PROBE_ATTEMPTS = 3
ACP_PORT_PROBE_RETRY_WINDOW_SECONDS = 4.0
ACP_PORT_PROBE_RETRY_DELAY_SECONDS = ACP_PORT_PROBE_RETRY_WINDOW_SECONDS / (ACP_PORT_PROBE_ATTEMPTS - 1)


class ACPDeviceOffNetworkError(ACPConnectionError):
    """ACP did not answer, and the device's address is on none of this
    computer's networks (VPN tunnels and virtual bridges aside)."""


def device_off_network_message(
    address: str,
    client_networks: list[IpNetwork],
    *,
    platform: str | None = None,
) -> str:
    computer = "this Mac" if (platform or sys.platform) == "darwin" else "this computer"
    # The message reaches telemetry as the operation's error, so this
    # computer's public networks are shown only by family and prefix length.
    networks = ", ".join(reportable_network(network, hide_hosts=True) for network in client_networks)
    return (
        f"{address} is not on {computer}'s network ({networks}). "
        f"Check the address, or connect {computer} to the device's network by Wi-Fi or one of its LAN ports, "
        "then try again."
    )


def _client_networks_if_off_network(host: str) -> list[IpNetwork] | None:
    """This computer's networks when `host` is on none of them, else None.

    Only explains a failure: an address on another network can still be
    reachable through a router, so this never decides whether to try it.
    """
    try:
        link = classify_network_link(
            host_networks(host), [item.network for item in local_lan_networks()], source="acp_target",
        )
    except Exception:
        return None
    if link.verdict != "separate":
        return None
    client, _device = link.compared()
    return list(client)


def is_macos_gui_local_network_privacy_signal(error: object) -> bool:
    if sys.platform != "darwin":
        return False
    if os.getenv("TCAPSULE_CLIENT") != "macos_gui":
        return False
    text = str(error)
    return "[Errno 65]" in text or "No route to host" in text


def _run_enable_ssh(
    host: str,
    password: str,
    *,
    timeout: float,
    callbacks: OperationCallbacks,
) -> None:
    callbacks.debug(acp_ssh_enable_attempted=True)
    callbacks.message(f"Enabling SSH through ACP on {host}...")
    callbacks.stage("acp_enable_ssh")
    try:
        set_dbug(host, password, DBUG_SSH_VALUE, log=callbacks.log, timeout=timeout)
    except ACPAuthError:
        callbacks.debug(
            acp_ssh_enable_succeeded=False,
            acp_ssh_enable_failure="authentication_failed",
        )
        raise
    except ACPError:
        callbacks.debug(acp_ssh_enable_succeeded=False)
        raise

    callbacks.debug(acp_ssh_enable_succeeded=True)


def _record_port_probe_context(
    host: str,
    record: BonjourResolvedService | None,
    callbacks: OperationCallbacks,
    *,
    failed: bool,
) -> None:
    # Sent on success too, as the baseline for failures. Diagnostics must never
    # change the outcome, so their own errors are only recorded.
    try:
        fields = acp_diagnostics.probe_context_fields(host, record)
        if failed:
            fields.update(acp_diagnostics.failure_fields(host, record))
    except Exception as exc:
        fields = {"acp_diagnostics_error": f"{type(exc).__name__}: {exc}"[:200]}
    callbacks.update(**fields)


def enable_ssh_with_port_preflight(
    host: str,
    password: str,
    *,
    timeout: float = 25.0,
    callbacks: OperationCallbacks | None = None,
    record: BonjourResolvedService | None = None,
    tcp_connect_error_func: Callable[[str, int], str | None] | None = None,
    sleep_func: Callable[[float], None] | None = None,
) -> None:
    """Ask ACP to turn SSH on at the next boot, once its port answers.

    `record` is the Bonjour record `host` came from, if any; it only adds
    telemetry about the device's other addresses.
    """
    callbacks = callbacks or OperationCallbacks()
    tcp_connect_error_func = tcp_connect_error_func or tcp_connect_error
    sleep_func = sleep_func or time.sleep
    callbacks.debug(acp_port_probe_attempted=True)
    callbacks.message(f"Checking AirPort ACP on {host}:{ACP_PORT}...")
    callbacks.stage("acp_port_probe")
    errors: list[dict[str, object]] = []
    for attempt in range(1, ACP_PORT_PROBE_ATTEMPTS + 1):
        error = tcp_connect_error_func(host, ACP_PORT)
        if error is None:
            debug_fields: dict[str, object] = {
                "acp_port_probe_succeeded": True,
                "acp_port_probe_attempts": attempt,
            }
            if errors:
                debug_fields["acp_port_probe_errors"] = errors
                debug_fields["acp_port_probe_last_error"] = errors[-1]["error"]
            callbacks.debug(**debug_fields)
            callbacks.update(
                acp_port_probe_succeeded=True,
                acp_port_probe_error_kinds=[entry["kind"] for entry in errors],
            )
            _record_port_probe_context(host, record, callbacks, failed=False)
            break

        error_text = str(error).strip() or "connection failed"
        errors.append({"attempt": attempt, "error": error_text, "kind": acp_diagnostics.connect_error_kind(error_text)})
        if attempt < ACP_PORT_PROBE_ATTEMPTS:
            sleep_func(ACP_PORT_PROBE_RETRY_DELAY_SECONDS)
    else:
        last_error = errors[-1]["error"] if errors else "connection failed"
        debug_fields: dict[str, object] = {
            "acp_port_probe_succeeded": False,
            "acp_port_probe_attempts": ACP_PORT_PROBE_ATTEMPTS,
            "acp_port_probe_errors": errors,
            "acp_port_probe_last_error": last_error,
        }
        if is_macos_gui_local_network_privacy_signal(last_error):
            debug_fields.update(
                macos_local_network_privacy_suspected=True,
                macos_local_network_privacy_signal="errno65_no_route_to_host",
            )
        callbacks.debug(**debug_fields)
        callbacks.update(
            acp_port_probe_succeeded=False,
            acp_port_probe_error_kinds=[entry["kind"] for entry in errors],
        )
        _record_port_probe_context(host, record, callbacks, failed=True)
        failure = f"Could not connect to ACP on {host}:{ACP_PORT}. "
        client_networks = _client_networks_if_off_network(host)
        if client_networks:
            raise ACPDeviceOffNetworkError(failure + device_off_network_message(host, client_networks))
        raise ACPConnectionError(failure + "Check the device IP address or hostname.")

    _run_enable_ssh(
        host,
        password,
        timeout=timeout,
        callbacks=callbacks,
    )
