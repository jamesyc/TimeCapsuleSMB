from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from timecapsulesmb.core.errors import system_exit_message
from timecapsulesmb.core.net import endpoint_host
from timecapsulesmb.deploy.executor import remote_request_reboot
from timecapsulesmb.device.probe import wait_for_ssh_state_conn
from timecapsulesmb.integrations.acp import ACPError
from timecapsulesmb.integrations.acp import reboot as acp_reboot
from timecapsulesmb.services.callbacks import OperationCallbacks
from timecapsulesmb.transport.ssh import SshCommandTimeout, SshConnection, SshError


ACP_REBOOT_REQUEST_TIMEOUT_SECONDS = 10
SSH_SHUTDOWN_REBOOT_PROGRESS_MESSAGE = "SSH: /bin/sync; /sbin/shutdown -r now (fallback /sbin/reboot)"


@dataclass(frozen=True)
class RebootCycleResult:
    went_down: bool
    came_back_up: bool

    @property
    def completed(self) -> bool:
        return self.went_down and self.came_back_up


@dataclass(frozen=True)
class RebootFlowError(RuntimeError):
    message: str
    reason: str

    def __str__(self) -> str:
        return self.message


def request_reboot(
    connection: SshConnection,
    *,
    strategy: str,
    callbacks: OperationCallbacks | None = None,
    progress_log: Callable[[str], None] | None = None,
    raise_on_request_error: bool = False,
    request_reboot_func: Callable[[SshConnection], None] | None = None,
    request_acp_reboot: Callable[..., object] | None = None,
) -> None:
    if request_reboot_func is None:
        request_reboot_func = remote_request_reboot
    if request_acp_reboot is None:
        request_acp_reboot = acp_reboot
    try:
        _request_reboot(
            connection,
            strategy=strategy,
            callbacks=callbacks,
            progress_log=progress_log,
            raise_on_request_error=raise_on_request_error,
            request_reboot=request_reboot_func,
            request_acp_reboot=request_acp_reboot,
        )
    except SshCommandTimeout as exc:
        raise RebootFlowError(f"SSH reboot request timed out: {exc}", "request_timeout") from exc
    except SshError as exc:
        raise RebootFlowError(f"SSH reboot request failed: {exc}", "request_failed") from exc


def request_reboot_and_wait(
    connection: SshConnection,
    *,
    strategy: str,
    callbacks: OperationCallbacks | None = None,
    progress_log: Callable[[str], None] | None = None,
    raise_on_request_error: bool = False,
    down_timeout_seconds: int,
    up_timeout_seconds: int,
    reboot_no_down_message: str,
    reboot_up_timeout_message: str,
    request_reboot_func: Callable[[SshConnection], None] | None = None,
    request_acp_reboot: Callable[..., object] | None = None,
    wait_for_ssh_state: Callable[..., bool] | None = None,
) -> None:
    if request_reboot_func is None:
        request_reboot_func = remote_request_reboot
    if request_acp_reboot is None:
        request_acp_reboot = acp_reboot
    if wait_for_ssh_state is None:
        wait_for_ssh_state = wait_for_ssh_state_conn
    try:
        result = _request_reboot_and_observe(
            connection,
            strategy=strategy,
            callbacks=callbacks,
            progress_log=progress_log,
            raise_on_request_error=raise_on_request_error,
            down_timeout_seconds=down_timeout_seconds,
            up_timeout_seconds=up_timeout_seconds,
            request_reboot=request_reboot_func,
            request_acp_reboot=request_acp_reboot,
            wait_for_ssh_state=wait_for_ssh_state,
        )
    except SshCommandTimeout as exc:
        raise RebootFlowError(f"SSH reboot request timed out: {exc}", "request_timeout") from exc
    except SshError as exc:
        raise RebootFlowError(f"SSH reboot request failed: {exc}", "request_failed") from exc
    _raise_if_incomplete(result, reboot_no_down_message, reboot_up_timeout_message)


def observe_reboot_cycle(
    connection: SshConnection,
    *,
    callbacks: OperationCallbacks | None = None,
    down_timeout_seconds: int,
    up_timeout_seconds: int,
    reboot_no_down_message: str,
    reboot_up_timeout_message: str,
    wait_for_ssh_state: Callable[..., bool] | None = None,
) -> None:
    if wait_for_ssh_state is None:
        wait_for_ssh_state = wait_for_ssh_state_conn
    result = _observe_reboot_cycle(
        connection,
        callbacks=callbacks,
        down_timeout_seconds=down_timeout_seconds,
        up_timeout_seconds=up_timeout_seconds,
        wait_for_ssh_state=wait_for_ssh_state,
    )
    _raise_if_incomplete(result, reboot_no_down_message, reboot_up_timeout_message)


def _request_reboot(
    connection: SshConnection,
    *,
    strategy: str,
    callbacks: OperationCallbacks | None = None,
    progress_log: Callable[[str], None] | None = None,
    raise_on_request_error: bool = False,
    request_reboot: Callable[[SshConnection], None] = remote_request_reboot,
    request_acp_reboot: Callable[..., object] = acp_reboot,
) -> None:
    callbacks = callbacks or OperationCallbacks()
    callbacks.stage("reboot")
    callbacks.update(reboot_was_attempted=True)
    callbacks.debug(reboot_request_strategy=strategy)
    if strategy == "acp_then_ssh":
        _request_reboot_acp_then_ssh(
            connection,
            callbacks=callbacks,
            progress_log=progress_log,
            raise_on_request_error=raise_on_request_error,
            request_reboot=request_reboot,
            request_acp_reboot=request_acp_reboot,
        )
        return
    _request_reboot_via_ssh(
        connection,
        callbacks=callbacks,
        progress_log=progress_log,
        request_reboot=request_reboot,
        raise_on_request_error=raise_on_request_error,
    )


def _request_reboot_acp_then_ssh(
    connection: SshConnection,
    *,
    callbacks: OperationCallbacks,
    progress_log: Callable[[str], None] | None,
    raise_on_request_error: bool,
    request_reboot: Callable[[SshConnection], None],
    request_acp_reboot: Callable[..., object],
) -> None:
    callbacks.debug(acp_reboot_attempted=True)
    try:
        request_acp_reboot(
            endpoint_host(connection.host),
            connection.password,
            timeout=ACP_REBOOT_REQUEST_TIMEOUT_SECONDS,
        )
    except ACPError as exc:
        callbacks.debug(
            acp_reboot_succeeded=False,
            acp_reboot_error=system_exit_message(exc),
        )
        callbacks.message("ACP reboot request failed; trying SSH reboot request.")
        _request_reboot_via_ssh(
            connection,
            callbacks=callbacks,
            progress_log=progress_log,
            request_reboot=request_reboot,
            raise_on_request_error=raise_on_request_error,
        )
        return

    callbacks.debug(acp_reboot_succeeded=True)
    callbacks.message("ACP reboot requested.")


def _request_reboot_via_ssh(
    connection: SshConnection,
    *,
    callbacks: OperationCallbacks,
    progress_log: Callable[[str], None] | None,
    request_reboot: Callable[[SshConnection], None],
    progress_message: str = SSH_SHUTDOWN_REBOOT_PROGRESS_MESSAGE,
    raise_on_request_error: bool,
) -> None:
    callbacks.debug(ssh_reboot_attempted=True)
    if progress_log is not None:
        progress_log(progress_message)
    try:
        request_reboot(connection)
    except SshCommandTimeout as exc:
        callbacks.debug(
            ssh_reboot_succeeded=False,
            ssh_reboot_timed_out=True,
            ssh_reboot_error=system_exit_message(exc),
        )
        if raise_on_request_error:
            raise
        callbacks.message("SSH reboot request timed out; checking whether the device is rebooting...")
        return
    except SshError as exc:
        callbacks.debug(
            ssh_reboot_succeeded=False,
            ssh_reboot_error=system_exit_message(exc),
        )
        if raise_on_request_error:
            raise
        callbacks.message("SSH reboot request failed; checking whether the device is rebooting anyway...")
        return

    callbacks.debug(ssh_reboot_succeeded=True)
    callbacks.message("SSH reboot requested.")


def _request_reboot_and_observe(
    connection: SshConnection,
    *,
    strategy: str,
    callbacks: OperationCallbacks | None = None,
    progress_log: Callable[[str], None] | None = None,
    raise_on_request_error: bool = False,
    down_timeout_seconds: int,
    up_timeout_seconds: int,
    request_reboot: Callable[[SshConnection], None] = remote_request_reboot,
    request_acp_reboot: Callable[..., object] = acp_reboot,
    wait_for_ssh_state: Callable[..., bool] = wait_for_ssh_state_conn,
) -> RebootCycleResult:
    callbacks = callbacks or OperationCallbacks()
    _request_reboot(
        connection,
        strategy=strategy,
        callbacks=callbacks,
        progress_log=progress_log,
        raise_on_request_error=raise_on_request_error,
        request_reboot=request_reboot,
        request_acp_reboot=request_acp_reboot,
    )
    return _observe_reboot_cycle(
        connection,
        callbacks=callbacks,
        down_timeout_seconds=down_timeout_seconds,
        up_timeout_seconds=up_timeout_seconds,
        wait_for_ssh_state=wait_for_ssh_state,
    )


def _observe_reboot_cycle(
    connection: SshConnection,
    *,
    callbacks: OperationCallbacks | None = None,
    down_timeout_seconds: int,
    up_timeout_seconds: int,
    wait_for_ssh_state: Callable[..., bool] = wait_for_ssh_state_conn,
) -> RebootCycleResult:
    callbacks = callbacks or OperationCallbacks()
    callbacks.message("Waiting for the device to go down...")
    callbacks.stage("wait_for_reboot_down")
    if not wait_for_ssh_state(connection, expected_up=False, timeout_seconds=down_timeout_seconds):
        return RebootCycleResult(went_down=False, came_back_up=False)

    callbacks.message("Device went down; waiting for it to come back up...")
    callbacks.stage("wait_for_reboot_up")
    if not wait_for_ssh_state(connection, expected_up=True, timeout_seconds=up_timeout_seconds):
        return RebootCycleResult(went_down=True, came_back_up=False)

    callbacks.update(device_came_back_after_reboot=True)
    callbacks.message("Device is back online.")
    return RebootCycleResult(went_down=True, came_back_up=True)


def _raise_if_incomplete(
    result: RebootCycleResult,
    reboot_no_down_message: str,
    reboot_up_timeout_message: str,
) -> None:
    if not result.went_down:
        raise RebootFlowError(reboot_no_down_message, "did_not_go_down")
    if not result.came_back_up:
        raise RebootFlowError(reboot_up_timeout_message, "did_not_come_back_up")
