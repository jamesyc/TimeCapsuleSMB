from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
import time

from timecapsulesmb.core.net import endpoint_host
from timecapsulesmb.deploy.executor import remote_request_reboot
from timecapsulesmb.integrations.acp import ACP_PORT
from timecapsulesmb.services import runtime as runtime_service
from timecapsulesmb.services.acp_ssh import enable_ssh_with_port_preflight
from timecapsulesmb.services.callbacks import OperationCallbacks
from timecapsulesmb.transport.local import tcp_connect_error, tcp_open
from timecapsulesmb.transport.ssh import SshCommandTimeout, SshConnection, run_ssh


SSH_PORT = 22


@dataclass(frozen=True)
class SetSshStatusResult:
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
class SetSshResult:
    host: str
    action: str
    ssh_initially_reachable: bool
    ssh_final_reachable: bool
    acp_port_reachable: bool
    reboot_requested: bool
    waited: bool
    acp_port_error: str | None = None
    ssh_port_error: str | None = None
    ssh_verification_skipped: bool = False
    ssh_disable_persisted: bool | None = None
    ssh_reboot_observed_down: bool | None = None
    device_recovered: bool | None = None
    summary: str = ""


class SetSshAction(Enum):
    ENABLE = "enable_ssh"
    ENABLE_NOOP = "enable_noop"
    DISABLE = "disable_ssh"
    DISABLE_NOOP = "disable_noop"
    PROMPT_DISABLE = "prompt_disable_ssh"


class SetSshVerificationError(RuntimeError):
    pass


def select_set_ssh_action(*, explicit_enable: bool, explicit_disable: bool, ssh_open: bool) -> SetSshAction:
    if explicit_enable:
        return SetSshAction.ENABLE_NOOP if ssh_open else SetSshAction.ENABLE
    if explicit_disable:
        return SetSshAction.DISABLE if ssh_open else SetSshAction.DISABLE_NOOP
    return SetSshAction.PROMPT_DISABLE if ssh_open else SetSshAction.ENABLE


def probe_set_ssh_status(
    host: str,
    *,
    timeout: float = 2.0,
    tcp_connect_error_func: Callable[[str, int, float], str | None] | None = None,
) -> SetSshStatusResult:
    target_host = endpoint_host(host)
    if not target_host:
        return SetSshStatusResult(
            host="",
            acp_port_reachable=False,
            ssh_port_reachable=False,
            acp_port_error="No host is configured.",
            ssh_port_error="No host is configured.",
        )
    tcp_probe = tcp_connect_error_func or tcp_connect_error
    acp_error = tcp_probe(target_host, ACP_PORT, timeout)
    ssh_error = tcp_probe(target_host, SSH_PORT, timeout)
    return SetSshStatusResult(
        host=target_host,
        acp_port_reachable=acp_error is None,
        ssh_port_reachable=ssh_error is None,
        acp_port_error=acp_error,
        ssh_port_error=ssh_error,
    )


def enable_set_ssh(
    connection: SshConnection,
    *,
    no_wait: bool,
    callbacks: OperationCallbacks | None = None,
    wait_for_tcp_port_state: Callable[..., bool] | None = None,
    initial: SetSshStatusResult | None = None,
    probe: Callable[[str], SetSshStatusResult] | None = None,
) -> SetSshResult:
    callbacks = callbacks or OperationCallbacks()
    target_host = endpoint_host(connection.host)
    initial_status = initial or (probe or probe_set_ssh_status)(connection.host)
    callbacks.debug(
        ssh_initially_reachable=initial_status.ssh_port_reachable,
        acp_initially_reachable=initial_status.acp_port_reachable,
        acp_host=target_host,
    )
    if initial_status.ssh_port_reachable:
        return SetSshResult(
            host=target_host,
            action=SetSshAction.ENABLE_NOOP.value,
            ssh_initially_reachable=True,
            ssh_final_reachable=True,
            acp_port_reachable=initial_status.acp_port_reachable,
            reboot_requested=False,
            waited=False,
            acp_port_error=initial_status.acp_port_error,
            ssh_port_error=initial_status.ssh_port_error,
            summary="SSH is already enabled.",
        )

    enable_ssh_with_port_preflight(
        target_host,
        connection.password,
        reboot_device=True,
        callbacks=callbacks,
    )

    if no_wait:
        return SetSshResult(
            host=target_host,
            action=SetSshAction.ENABLE.value,
            ssh_initially_reachable=False,
            ssh_final_reachable=False,
            acp_port_reachable=True,
            reboot_requested=True,
            waited=False,
            acp_port_error=initial_status.acp_port_error,
            ssh_port_error=initial_status.ssh_port_error,
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
        callbacks.update(ssh_final_reachable=False)
        raise RuntimeError("SSH did not open after enabling via ACP.")
    return SetSshResult(
        host=target_host,
        action=SetSshAction.ENABLE.value,
        ssh_initially_reachable=False,
        ssh_final_reachable=True,
        acp_port_reachable=True,
        reboot_requested=True,
        waited=True,
        acp_port_error=initial_status.acp_port_error,
        ssh_port_error=None,
        summary="SSH is configured.",
    )


def disable_set_ssh(
    connection: SshConnection,
    *,
    no_wait: bool,
    callbacks: OperationCallbacks | None = None,
    wait_for_tcp_port_state: Callable[..., bool] | None = None,
    wait_for_device_up_func: Callable[[str], bool] | None = None,
    disable_func: Callable[..., None] | None = None,
    initial: SetSshStatusResult | None = None,
) -> SetSshResult:
    callbacks = callbacks or OperationCallbacks()
    target_host = endpoint_host(connection.host)
    initial_status = initial or probe_set_ssh_status(connection.host)
    callbacks.debug(
        ssh_initially_reachable=initial_status.ssh_port_reachable,
        acp_initially_reachable=initial_status.acp_port_reachable,
        acp_host=target_host,
    )
    if not initial_status.ssh_port_reachable:
        return SetSshResult(
            host=target_host,
            action=SetSshAction.DISABLE_NOOP.value,
            ssh_initially_reachable=False,
            ssh_final_reachable=False,
            acp_port_reachable=initial_status.acp_port_reachable,
            reboot_requested=False,
            waited=False,
            acp_port_error=initial_status.acp_port_error,
            ssh_port_error=initial_status.ssh_port_error,
            summary="SSH already disabled.",
        )

    callbacks.stage("disable_ssh")
    (disable_func or disable_ssh_over_ssh)(connection, reboot_device=True, log=callbacks.log)

    if no_wait:
        return SetSshResult(
            host=target_host,
            action=SetSshAction.DISABLE.value,
            ssh_initially_reachable=True,
            ssh_final_reachable=True,
            acp_port_reachable=initial_status.acp_port_reachable,
            reboot_requested=True,
            waited=False,
            acp_port_error=initial_status.acp_port_error,
            ssh_port_error=initial_status.ssh_port_error,
            ssh_verification_skipped=True,
            summary="SSH disable requested; not waiting for reboot or verifying SSH stays closed.",
        )

    callbacks.message("Device is starting reboot now, waiting for it to shut down...")
    callbacks.stage("wait_for_ssh_down")
    wait_func = wait_for_tcp_port_state or runtime_service.wait_for_tcp_port_state
    if not wait_func(
        target_host,
        SSH_PORT,
        expected_state=False,
        log=callbacks.log,
        service_name="SSH port",
    ):
        callbacks.update(
            ssh_final_reachable=True,
            ssh_disable_persisted=False,
            ssh_reboot_observed_down=False,
        )
        raise SetSshVerificationError("SSH did not close after disable/reboot request; disable could not be verified.")

    callbacks.update(ssh_reboot_observed_down=True)
    callbacks.message("Device is down now, verifying persistence after reboot...")
    callbacks.stage("wait_for_device_up")
    wait_up = wait_for_device_up_func or wait_for_device_up
    if not wait_up(target_host):
        callbacks.update(device_recovered=False)
        raise SetSshVerificationError("Device went down after disable request but did not come back within timeout.")

    callbacks.update(device_recovered=True)
    callbacks.message("Device successfully rebooted. Checking if SSH is still disabled...")
    callbacks.stage("verify_ssh_disabled")
    if not wait_func(
        target_host,
        SSH_PORT,
        expected_state=False,
        timeout_seconds=30,
        log=callbacks.log,
        service_name="SSH port",
    ):
        callbacks.update(ssh_final_reachable=True, ssh_disable_persisted=False)
        raise SetSshVerificationError("SSH reopened after reboot. Disable did not persist.")

    callbacks.message("SSH disabled (remains closed after reboot). Enable SSH again if this was not intended.")
    return SetSshResult(
        host=target_host,
        action=SetSshAction.DISABLE.value,
        ssh_initially_reachable=True,
        ssh_final_reachable=False,
        acp_port_reachable=initial_status.acp_port_reachable,
        reboot_requested=True,
        waited=True,
        acp_port_error=initial_status.acp_port_error,
        ssh_port_error="SSH port is closed.",
        ssh_disable_persisted=True,
        ssh_reboot_observed_down=True,
        device_recovered=True,
        summary="SSH disabled (remains closed after reboot).",
    )


def disable_ssh_over_ssh(
    connection: SshConnection,
    *,
    reboot_device: bool = True,
    log: Callable[[str], None] | None = None,
) -> None:
    cmds = [
        "acp remove dbug",
        "/usr/sbin/acp remove dbug",
        "/usr/bin/acp remove dbug",
    ]
    last_err: tuple[int, str] | None = None
    for command in cmds:
        proc = run_ssh(connection, command, check=False, timeout=30)
        rc = proc.returncode
        out = proc.stdout or ""
        if rc == 0:
            _emit(log, f"Removed 'dbug' via: {command}")
            break
        if _dbug_property_already_absent(out):
            _emit(log, f"SSH debug flag 'dbug' already absent via: {command}")
            break
        if _looks_like_ssh_auth_failure(out):
            raise RuntimeError("SSH authentication failed while trying to disable SSH over SSH.")
        last_err = (rc, out)
    else:
        code, out = last_err or (1, "unknown error")
        raise RuntimeError(f"Failed to remove 'dbug' via on-device acp (rc={code}). Output: {out}")

    if reboot_device:
        try:
            remote_request_reboot(connection)
        except SshCommandTimeout as exc:
            _emit(log, f"Reboot request timed out; continuing to observe whether the device is rebooting... ({exc})")


def wait_for_device_up(
    host: str,
    *,
    timeout_seconds: int = 180,
    interval_seconds: int = 5,
) -> bool:
    deadline = time.time() + timeout_seconds
    while True:
        if any(tcp_open(host, port) for port in (ACP_PORT, 445, 139)):
            return True
        if time.time() >= deadline:
            return False
        time.sleep(interval_seconds)


def _dbug_property_already_absent(output: str) -> bool:
    # Verified on NetBSD 6 and NetBSD 4 Time Capsules: removing an absent
    # dbug property returns rc=22 and this message.
    return "remove property error: -10" in output


def _looks_like_ssh_auth_failure(output: str) -> bool:
    lowered = output.lower()
    return "permission denied" in lowered or "please try again" in lowered


def _emit(log: Callable[[str], None] | None, message: str) -> None:
    if log is not None:
        log(message)
