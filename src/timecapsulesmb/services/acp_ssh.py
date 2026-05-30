from __future__ import annotations

from timecapsulesmb.integrations.acp import ACPAuthError, ACPError, ACPIdentity, enable_ssh, read_identity
from timecapsulesmb.services.runtime import RuntimeOperationCallbacks


def _call_stage(callbacks: RuntimeOperationCallbacks, stage: str) -> None:
    if callbacks.set_stage is not None:
        callbacks.set_stage(stage)


def _call_log(callbacks: RuntimeOperationCallbacks, message: str) -> None:
    if callbacks.log is not None:
        callbacks.log(message)


def _call_debug(callbacks: RuntimeOperationCallbacks, **fields: object) -> None:
    if callbacks.add_debug_fields is not None:
        callbacks.add_debug_fields(**fields)


def _call_update(callbacks: RuntimeOperationCallbacks, **fields: object) -> None:
    if callbacks.update_fields is not None:
        callbacks.update_fields(**fields)


def read_identity_preflight(
    host: str,
    password: str,
    *,
    timeout: float = 10.0,
    callbacks: RuntimeOperationCallbacks | None = None,
) -> ACPIdentity:
    callbacks = callbacks or RuntimeOperationCallbacks()
    _call_debug(callbacks, acp_identity_probe_attempted=True)
    _call_log(callbacks, f"Reading AirPort identity through ACP on {host}...")
    _call_stage(callbacks, "acp_identity_probe")
    try:
        identity = read_identity(host, password, timeout=timeout)
    except ACPAuthError:
        _call_debug(
            callbacks,
            acp_identity_probe_succeeded=False,
            acp_identity_probe_failure="authentication_failed",
        )
        raise
    except ACPError:
        _call_debug(callbacks, acp_identity_probe_succeeded=False)
        raise

    fields: dict[str, object] = {"acp_identity_probe_succeeded": True}
    if identity.syap is not None:
        syap = str(identity.syap)
        fields["acp_identity_syap"] = syap
        _call_update(callbacks, device_syap=syap)
    _call_debug(callbacks, **fields)
    return identity


def enable_ssh_with_identity_preflight(
    host: str,
    password: str,
    *,
    reboot_device: bool = True,
    timeout: float = 10.0,
    callbacks: RuntimeOperationCallbacks | None = None,
) -> ACPIdentity:
    callbacks = callbacks or RuntimeOperationCallbacks()
    identity = read_identity_preflight(host, password, timeout=timeout, callbacks=callbacks)
    _call_debug(callbacks, acp_ssh_enable_attempted=True)
    _call_log(callbacks, f"Enabling SSH through ACP on {host}...")
    _call_stage(callbacks, "acp_enable_ssh")
    try:
        enable_ssh(host, password, reboot_device=reboot_device, log=callbacks.log, timeout=timeout)
    except ACPAuthError:
        _call_debug(
            callbacks,
            acp_ssh_enable_succeeded=False,
            acp_ssh_enable_failure="authentication_failed",
        )
        raise
    except ACPError:
        _call_debug(callbacks, acp_ssh_enable_succeeded=False)
        raise

    _call_debug(callbacks, acp_ssh_enable_succeeded=True)
    return identity
