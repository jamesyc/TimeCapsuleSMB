from __future__ import annotations

from collections.abc import Callable

from timecapsulesmb.integrations.acp import (
    ACP_PORT,
    ACPAuthError,
    ACPConnectionError,
    ACPError,
    ACPIdentity,
    enable_ssh,
    read_identity,
)
from timecapsulesmb.services.callbacks import OperationCallbacks
from timecapsulesmb.transport.local import tcp_open


def read_identity_preflight(
    host: str,
    password: str,
    *,
    timeout: float = 25.0,
    callbacks: OperationCallbacks | None = None,
) -> ACPIdentity:
    callbacks = callbacks or OperationCallbacks()
    callbacks.debug(acp_identity_probe_attempted=True)
    callbacks.message(f"Reading AirPort identity through ACP on {host}...")
    callbacks.stage("acp_identity_probe")
    try:
        identity = read_identity(host, password, timeout=timeout)
    except ACPAuthError:
        callbacks.debug(
            acp_identity_probe_succeeded=False,
            acp_identity_probe_failure="authentication_failed",
        )
        raise
    except ACPError:
        callbacks.debug(acp_identity_probe_succeeded=False)
        raise

    fields: dict[str, object] = {"acp_identity_probe_succeeded": True}
    if identity.syap is not None:
        syap = str(identity.syap)
        fields["acp_identity_syap"] = syap
        callbacks.update(device_syap=syap)
    callbacks.debug(**fields)
    return identity


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


def enable_ssh_with_identity_preflight(
    host: str,
    password: str,
    *,
    reboot_device: bool = True,
    timeout: float = 25.0,
    callbacks: OperationCallbacks | None = None,
) -> ACPIdentity:
    callbacks = callbacks or OperationCallbacks()
    identity = read_identity_preflight(host, password, timeout=timeout, callbacks=callbacks)
    _run_enable_ssh(
        host,
        password,
        reboot_device=reboot_device,
        timeout=timeout,
        callbacks=callbacks,
    )
    return identity


def enable_ssh_with_port_preflight(
    host: str,
    password: str,
    *,
    reboot_device: bool = True,
    timeout: float = 25.0,
    callbacks: OperationCallbacks | None = None,
    tcp_open_func: Callable[[str, int], bool] | None = None,
) -> None:
    callbacks = callbacks or OperationCallbacks()
    tcp_open_func = tcp_open_func or tcp_open
    callbacks.debug(acp_port_probe_attempted=True)
    callbacks.message(f"Checking AirPort ACP on {host}:{ACP_PORT}...")
    callbacks.stage("acp_port_probe")
    if not tcp_open_func(host, ACP_PORT):
        callbacks.debug(acp_port_probe_succeeded=False)
        raise ACPConnectionError(
            f"Could not connect to ACP on {host}:{ACP_PORT}. "
            "Check the device IP address or hostname."
        )

    callbacks.debug(acp_port_probe_succeeded=True)
    _run_enable_ssh(
        host,
        password,
        reboot_device=reboot_device,
        timeout=timeout,
        callbacks=callbacks,
    )
