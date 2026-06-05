from __future__ import annotations

from timecapsulesmb.integrations.acp import ACPAuthError, ACPError, ACPIdentity, enable_ssh, read_identity
from timecapsulesmb.services.callbacks import OperationCallbacks


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
    return identity
