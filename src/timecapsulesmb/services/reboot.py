from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from timecapsulesmb.deploy.executor import remote_request_reboot
from timecapsulesmb.device.probe import wait_for_ssh_state_conn
from timecapsulesmb.integrations.acp import reboot as acp_reboot
from timecapsulesmb.services import runtime as runtime_service
from timecapsulesmb.services.runtime import RuntimeOperationCallbacks
from timecapsulesmb.transport.ssh import SshCommandTimeout, SshConnection, SshError


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
    callbacks: RuntimeOperationCallbacks | None = None,
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
        runtime_service.request_runtime_reboot(
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
    callbacks: RuntimeOperationCallbacks | None = None,
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
        result = runtime_service.request_runtime_reboot_and_observe(
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
    callbacks: RuntimeOperationCallbacks | None = None,
    down_timeout_seconds: int,
    up_timeout_seconds: int,
    reboot_no_down_message: str,
    reboot_up_timeout_message: str,
    wait_for_ssh_state: Callable[..., bool] | None = None,
) -> None:
    if wait_for_ssh_state is None:
        wait_for_ssh_state = wait_for_ssh_state_conn
    result = runtime_service.observe_runtime_reboot_cycle(
        connection,
        callbacks=callbacks,
        down_timeout_seconds=down_timeout_seconds,
        up_timeout_seconds=up_timeout_seconds,
        wait_for_ssh_state=wait_for_ssh_state,
    )
    _raise_if_incomplete(result, reboot_no_down_message, reboot_up_timeout_message)


def _raise_if_incomplete(
    result: runtime_service.RebootCycleResult,
    reboot_no_down_message: str,
    reboot_up_timeout_message: str,
) -> None:
    if not result.went_down:
        raise RebootFlowError(reboot_no_down_message, "did_not_go_down")
    if not result.came_back_up:
        raise RebootFlowError(reboot_up_timeout_message, "did_not_come_back_up")
