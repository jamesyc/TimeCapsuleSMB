from __future__ import annotations

from collections.abc import Callable
import time

from timecapsulesmb.integrations.acp import (
    ACP_PORT,
    ACPAuthError,
    ACPConnectionError,
    ACPError,
    enable_ssh,
)
from timecapsulesmb.services.callbacks import OperationCallbacks
from timecapsulesmb.transport.local import tcp_connect_error


ACP_PORT_PROBE_ATTEMPTS = 3
ACP_PORT_PROBE_RETRY_WINDOW_SECONDS = 4.0
ACP_PORT_PROBE_RETRY_DELAY_SECONDS = ACP_PORT_PROBE_RETRY_WINDOW_SECONDS / (ACP_PORT_PROBE_ATTEMPTS - 1)


def _run_enable_ssh(
    host: str,
    password: str,
    *,
    reboot_device: bool,
    timeout: float,
    callbacks: OperationCallbacks,
) -> None:
    callbacks.debug(acp_ssh_enable_attempted=True)
    callbacks.message(f"Enabling SSH through ACP on {host}...")
    callbacks.stage("acp_enable_ssh")
    try:
        enable_ssh(host, password, reboot_device=reboot_device, log=callbacks.log, timeout=timeout)
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


def enable_ssh_with_port_preflight(
    host: str,
    password: str,
    *,
    reboot_device: bool = True,
    timeout: float = 25.0,
    callbacks: OperationCallbacks | None = None,
    tcp_connect_error_func: Callable[[str, int], str | None] | None = None,
    sleep_func: Callable[[float], None] | None = None,
) -> None:
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
            break

        error_text = str(error).strip() or "connection failed"
        errors.append({"attempt": attempt, "error": error_text})
        if attempt < ACP_PORT_PROBE_ATTEMPTS:
            sleep_func(ACP_PORT_PROBE_RETRY_DELAY_SECONDS)
    else:
        callbacks.debug(
            acp_port_probe_succeeded=False,
            acp_port_probe_attempts=ACP_PORT_PROBE_ATTEMPTS,
            acp_port_probe_errors=errors,
            acp_port_probe_last_error=errors[-1]["error"] if errors else "connection failed",
        )
        raise ACPConnectionError(
            f"Could not connect to ACP on {host}:{ACP_PORT}. "
            "Check the device IP address or hostname."
        )

    _run_enable_ssh(
        host,
        password,
        reboot_device=reboot_device,
        timeout=timeout,
        callbacks=callbacks,
    )
