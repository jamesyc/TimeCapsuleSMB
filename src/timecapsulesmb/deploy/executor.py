from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping

from timecapsulesmb.deploy.commands import RemoteAction, render_remote_actions
from timecapsulesmb.deploy.planner import DeploymentPlan, FileTransfer, UninstallPlan
from timecapsulesmb.device.storage import MaStVolume, ensure_volume_root_mounted_conn
from timecapsulesmb.transport.ssh import SshConnection, run_scp, run_ssh


DETACHED_SHUTDOWN_REBOOT_COMMAND = (
    "/bin/sh -c 'exec </dev/null >/dev/null 2>&1; "
    "(/bin/sync; /bin/sleep 1; "
    "/sbin/shutdown -r now || /sbin/reboot"
    ") & exit 0'"
)
REBOOT_REQUEST_TIMEOUT_SECONDS = 30
PAYLOAD_FLUSH_SETTLE_SECONDS = 10
FLUSH_REMOTE_FILESYSTEMS_COMMAND = (
    f"/bin/sh -c {shlex.quote(f'/bin/sync && /bin/sleep {PAYLOAD_FLUSH_SETTLE_SECONDS} && /bin/sync')}"
)
# Time Capsule HFS disks can spend well over 30 seconds flushing the Samba
# payload after a slow upload. Keep this bounded, but long enough for real disks.
FLUSH_REMOTE_FILESYSTEMS_TIMEOUT_SECONDS = 300
# Old disks may contain millions of files. Migration runs only during deploy;
# allow a long bounded scan without imposing its timeout on ordinary SSH calls.
XATTR_HFS_MIGRATION_TIMEOUT_SECONDS = 6 * 60 * 60


@dataclass(frozen=True)
class XattrMigrationResult:
    output: str
    roots: tuple[MaStVolume, ...]
    unavailable_roots: tuple[str, ...] = ()


def migrate_xattr_tdb_to_hfs(connection: SshConnection, plan: DeploymentPlan, *, phase: str, inventory) -> XattrMigrationResult:
    from timecapsulesmb.deploy.migration import migrate_phase
    output = migrate_phase(connection, plan, inventory, phase)
    return XattrMigrationResult(output, inventory.volumes, tuple(inventory.unavailable))


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
    on_uploading: Callable[[FileTransfer], None] | None = None,
    on_uploaded: Callable[[FileTransfer], None] | None = None,
) -> None:
    for transfer in plan.uploads:
        source = _resolve_transfer_source(source_resolver, transfer)
        if on_uploading is not None:
            on_uploading(transfer)
        _ensure_payload_volume_before_transfer(connection, plan, transfer)
        if transfer.mode in {"scp", "generated"}:
            _scp_transfer(connection, source, transfer)
        else:
            raise ValueError(f"Unsupported deployment upload mode {transfer.mode!r} for {transfer.source_id!r}")
        # run_scp verifies the size for both SCP and the SSH-pipe fallback.
        # HDD permissions belong to the later mount-guarded action: Apple's
        # diskd may unmount the volume after the transfer closes its files.
        if on_uploaded is not None:
            on_uploaded(transfer)


def run_remote_actions(
    connection: SshConnection,
    actions: Iterable[RemoteAction],
    *,
    on_action_done: Callable[[RemoteAction, int, int], None] | None = None,
) -> None:
    action_list = list(actions)
    commands = render_remote_actions(action_list)
    total = len(action_list)
    for index, (action, command) in enumerate(zip(action_list, commands), start=1):
        run_ssh(connection, command)
        if on_action_done is not None:
            on_action_done(action, index, total)


def remote_request_reboot(connection: SshConnection) -> None:
    run_ssh(connection, DETACHED_SHUTDOWN_REBOOT_COMMAND, check=False, timeout=REBOOT_REQUEST_TIMEOUT_SECONDS)


def flush_remote_filesystem_writes(connection: SshConnection) -> None:
    run_ssh(connection, FLUSH_REMOTE_FILESYSTEMS_COMMAND, timeout=FLUSH_REMOTE_FILESYSTEMS_TIMEOUT_SECONDS)


def remote_uninstall_payload(connection: SshConnection, plan: UninstallPlan) -> None:
    # Use for loop to avoid rc=255 bug on NetBSD 4 Time Capsules
    for command in render_remote_actions(plan.remote_actions):
        run_ssh(connection, command)
