from __future__ import annotations

import shlex
from pathlib import Path, PurePosixPath
from typing import Iterable, Mapping

from timecapsulesmb.deploy.commands import RemoteAction, render_remote_actions
from timecapsulesmb.deploy.planner import FLASH_TEXT_UPLOAD_TIMEOUT_SECONDS, DeploymentPlan, FileTransfer, UninstallPlan
from timecapsulesmb.device.storage import ensure_volume_root_mounted_conn
from timecapsulesmb.transport.ssh import SshConnection, run_scp, run_ssh


DETACHED_REBOOT_COMMAND = "/bin/sh -c 'exec </dev/null >/dev/null 2>&1; (/bin/sleep 1; /sbin/reboot) & exit 0'"
REBOOT_REQUEST_TIMEOUT_SECONDS = 30
PAYLOAD_FLUSH_SETTLE_SECONDS = 5
FLUSH_REMOTE_FILESYSTEMS_COMMAND = (
    f"/bin/sh -c {shlex.quote(f'/bin/sync; /bin/sleep {PAYLOAD_FLUSH_SETTLE_SECONDS}; /bin/sync')}"
)
# Time Capsule HFS disks can spend well over 30 seconds flushing the Samba
# payload after a slow upload. Keep this bounded, but long enough for real disks.
FLUSH_REMOTE_FILESYSTEMS_TIMEOUT_SECONDS = 300


def _flash_upload_tmp_path(destination: str) -> str:
    path = PurePosixPath(destination)
    return str(path.with_name(f".{path.name}.tmp"))


def upload_flash_file(
    connection: SshConnection,
    source: Path,
    destination: str,
    *,
    timeout: int = 120,
    mode: str = "755",
) -> None:
    tmp_destination = _flash_upload_tmp_path(destination)
    quoted_tmp = shlex.quote(tmp_destination)
    quoted_destination = shlex.quote(destination)
    quoted_mode = shlex.quote(mode)

    run_ssh(connection, f"/bin/sh -c {shlex.quote(f'rm -f {quoted_tmp}')}")
    run_scp(connection, source, tmp_destination, timeout=timeout)
    install_script = (
        "rc=0; "
        f"chmod {quoted_mode} {quoted_tmp} && mv -f {quoted_tmp} {quoted_destination} || rc=$?; "
        f"rm -f {quoted_tmp}; "
        'exit "$rc"'
    )
    run_ssh(connection, f"/bin/sh -c {shlex.quote(install_script)}")


def _resolve_transfer_source(source_resolver: Mapping[str, Path], transfer: FileTransfer) -> Path:
    try:
        return source_resolver[transfer.source_id]
    except KeyError as e:
        raise KeyError(f"No local source for planned transfer {transfer.source_id!r}") from e


def _scp_transfer(connection: SshConnection, source: Path, transfer: FileTransfer) -> None:
    if transfer.timeout_seconds is None:
        run_scp(connection, source, transfer.destination)
        return
    run_scp(connection, source, transfer.destination, timeout=transfer.timeout_seconds)


def _destination_is_under(path: str, root: str) -> bool:
    normalized_path = path.rstrip("/")
    normalized_root = root.rstrip("/")
    return normalized_path == normalized_root or normalized_path.startswith(f"{normalized_root}/")


def _ensure_payload_volume_before_transfer(connection: SshConnection, plan: DeploymentPlan, transfer: FileTransfer) -> None:
    if not _destination_is_under(transfer.destination, plan.payload_dir):
        return
    if ensure_volume_root_mounted_conn(
        connection,
        plan.volume_root,
        plan.device_path,
        wait_seconds=plan.apple_mount_wait_seconds,
    ):
        return
    raise RuntimeError(f"payload volume {plan.volume_root} is not mounted before upload to {transfer.destination}")


def upload_deployment_payload(
    plan: DeploymentPlan,
    *,
    connection: SshConnection,
    source_resolver: Mapping[str, Path],
) -> None:
    planned_modes = {permission.path: permission.mode for permission in plan.permissions}
    for transfer in plan.uploads:
        source = _resolve_transfer_source(source_resolver, transfer)
        _ensure_payload_volume_before_transfer(connection, plan, transfer)
        if transfer.mode in {"scp", "generated"}:
            _scp_transfer(connection, source, transfer)
        elif transfer.mode == "flash_atomic":
            timeout = transfer.timeout_seconds if transfer.timeout_seconds is not None else FLASH_TEXT_UPLOAD_TIMEOUT_SECONDS
            upload_flash_file(
                connection,
                source,
                transfer.destination,
                timeout=timeout,
                mode=planned_modes.get(transfer.destination, "755"),
            )
        else:
            raise ValueError(f"Unsupported deployment upload mode {transfer.mode!r} for {transfer.source_id!r}")


def run_remote_actions(connection: SshConnection, actions: Iterable[RemoteAction]) -> None:
    for command in render_remote_actions(list(actions)):
        run_ssh(connection, command)


def remote_request_reboot(connection: SshConnection) -> None:
    run_ssh(connection, DETACHED_REBOOT_COMMAND, check=False, timeout=REBOOT_REQUEST_TIMEOUT_SECONDS)


def flush_remote_filesystem_writes(connection: SshConnection) -> None:
    run_ssh(connection, FLUSH_REMOTE_FILESYSTEMS_COMMAND, timeout=FLUSH_REMOTE_FILESYSTEMS_TIMEOUT_SECONDS)


def remote_uninstall_payload(connection: SshConnection, plan: UninstallPlan) -> None:
    # Use for loop to avoid rc=255 bug on NetBSD 4 Time Capsules
    for command in render_remote_actions(plan.remote_actions):
        run_ssh(connection, command)
