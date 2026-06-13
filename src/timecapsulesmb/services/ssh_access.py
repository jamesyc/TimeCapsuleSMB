from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from timecapsulesmb.core.net import endpoint_host
from timecapsulesmb.integrations.acp import ACP_PORT
from timecapsulesmb.services.acp_ssh import enable_ssh_with_port_preflight
from timecapsulesmb.services.callbacks import OperationCallbacks
from timecapsulesmb.services import runtime as runtime_service
from timecapsulesmb.transport.local import tcp_connect_error
from timecapsulesmb.transport.ssh import SshConnection


SSH_PORT = 22


@dataclass(frozen=True)
class SshAccessProbeResult:
    host: str
    acp_port_reachable: bool
    ssh_port_reachable: bool
    acp_port_error: str | None = None
    ssh_port_error: str | None = None

    @property
    def ssh_disabled_likely(self) -> bool:
        return self.acp_port_reachable and not self.ssh_port_reachable

    @property
    def summary(self) -> str:
        if self.ssh_port_reachable:
            return "SSH is reachable."
        if self.ssh_disabled_likely:
            return "AirPort ACP is reachable, but SSH is closed."
        if self.acp_port_reachable:
            return "AirPort ACP is reachable."
        return "AirPort ACP and SSH are not reachable."


@dataclass(frozen=True)
class SshAccessEnableResult:
    host: str
    action: str
    ssh_initially_reachable: bool
    ssh_final_reachable: bool
    acp_port_reachable: bool
    reboot_requested: bool
    waited: bool
    ssh_verification_skipped: bool = False
    summary: str = ""


def probe_ssh_access(
    host: str,
    *,
    timeout: float = 2.0,
    tcp_connect_error_func: Callable[[str, int, float], str | None] | None = None,
) -> SshAccessProbeResult:
    target_host = endpoint_host(host)
    if not target_host:
        return SshAccessProbeResult(
            host="",
            acp_port_reachable=False,
            ssh_port_reachable=False,
            acp_port_error="No host is configured.",
            ssh_port_error="No host is configured.",
        )
    tcp_probe = tcp_connect_error_func or tcp_connect_error
    acp_error = tcp_probe(target_host, ACP_PORT, timeout)
    ssh_error = tcp_probe(target_host, SSH_PORT, timeout)
    return SshAccessProbeResult(
        host=target_host,
        acp_port_reachable=acp_error is None,
        ssh_port_reachable=ssh_error is None,
        acp_port_error=acp_error,
        ssh_port_error=ssh_error,
    )


def enable_ssh_access(
    connection: SshConnection,
    *,
    no_wait: bool,
    callbacks: OperationCallbacks | None = None,
    wait_for_tcp_port_state: Callable[..., bool] | None = None,
    probe: Callable[[str], SshAccessProbeResult] | None = None,
) -> SshAccessEnableResult:
    callbacks = callbacks or OperationCallbacks()
    target_host = endpoint_host(connection.host)
    probe_func = probe or (lambda host: probe_ssh_access(host))
    initial = probe_func(connection.host)
    callbacks.debug(
        ssh_initially_reachable=initial.ssh_port_reachable,
        acp_initially_reachable=initial.acp_port_reachable,
        acp_host=target_host,
    )
    if initial.ssh_port_reachable:
        return SshAccessEnableResult(
            host=target_host,
            action="enable_noop",
            ssh_initially_reachable=True,
            ssh_final_reachable=True,
            acp_port_reachable=initial.acp_port_reachable,
            reboot_requested=False,
            waited=False,
            summary="SSH is already enabled.",
        )

    enable_ssh_with_port_preflight(
        target_host,
        connection.password,
        reboot_device=True,
        callbacks=callbacks,
    )

    if no_wait:
        return SshAccessEnableResult(
            host=target_host,
            action="enable_ssh",
            ssh_initially_reachable=False,
            ssh_final_reachable=False,
            acp_port_reachable=True,
            reboot_requested=True,
            waited=False,
            ssh_verification_skipped=True,
            summary="SSH enable requested; not waiting for SSH to open.",
        )

    callbacks.stage("wait_for_ssh_enabled")
    wait_func = wait_for_tcp_port_state or runtime_service.wait_for_tcp_port_state
    final_reachable = wait_func(
        target_host,
        SSH_PORT,
        expected_state=True,
        log=callbacks.log,
        service_name="SSH port",
    )
    if not final_reachable:
        return SshAccessEnableResult(
            host=target_host,
            action="enable_ssh",
            ssh_initially_reachable=False,
            ssh_final_reachable=False,
            acp_port_reachable=True,
            reboot_requested=True,
            waited=True,
            summary="SSH did not open after enabling via ACP.",
        )
    return SshAccessEnableResult(
        host=target_host,
        action="enable_ssh",
        ssh_initially_reachable=False,
        ssh_final_reachable=True,
        acp_port_reachable=True,
        reboot_requested=True,
        waited=True,
        summary="SSH is configured.",
    )
