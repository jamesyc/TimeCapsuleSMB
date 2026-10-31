from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import time
from typing import TYPE_CHECKING, Callable, Iterable, Mapping

from timecapsulesmb.deploy.commands import RemoteAction, RemovePathsAction, render_remote_actions
from timecapsulesmb.deploy.planner import DeploymentPlan, FileTransfer, UninstallPlan
from timecapsulesmb.device.storage import MaStVolume, VolumeClaims, ensure_volume_root_mounted_conn
from timecapsulesmb.transport.errors import SshCommandTimeout
from timecapsulesmb.transport.ssh import SshConnection, run_ssh, upload_file

if TYPE_CHECKING:
    from timecapsulesmb.deploy.migration import OversizedSummary, SkippedSummary
    from timecapsulesmb.services.callbacks import OperationCallbacks


# One sync, no settle wait. /mnt/Flash is mounted synchronously, so a Flash
# write is on flash when it returns; the sync is for the HFS payload disk. The
# ACP restart that follows deploy runs its own sync before its SIGKILL.
FLUSH_REMOTE_FILESYSTEMS_COMMAND = "/bin/sync"
# Time Capsule HFS disks can spend well over 30 seconds flushing the Samba
# payload after a slow upload. Keep this bounded, but long enough for real disks.
FLUSH_REMOTE_FILESYSTEMS_TIMEOUT_SECONDS = 300
REMOTE_ACTION_TIMEOUT_SECONDS = 120
# One rm removes every listed path. NetBSD 4's synchronous flash frees the
# 333 KB service in 28 s, and an older release left several binaries there.
REMOVE_PATHS_TIMEOUT_SECONDS = 300
@dataclass(frozen=True)
class XattrMigrationResult:
    output: str
    roots: tuple[MaStVolume, ...]
    unavailable_roots: tuple[str, ...] = ()
    # Values too large for a native HFS attribute, kept in legacy storage.
    oversized: OversizedSummary | None = None
    # Verified rows a lone retained database dropped, and whether it was
    # copied first (the path names the user's disk, so telemetry gets a flag).
    dropped_rows: int = 0
    backup: bool = False
    # Entries the walk left alone: names it could not open, unusable ._ files.
    skipped: SkippedSummary | None = None


def migrate_xattr_tdb_to_hfs(
    connection: SshConnection, plan: DeploymentPlan, *, phase: str, inventory, claims: VolumeClaims,
) -> XattrMigrationResult:
    from timecapsulesmb.deploy.migration import migrate_phase
    output = migrate_phase(connection, plan, inventory, phase, claims=claims)
    return XattrMigrationResult(output, inventory.volumes, tuple(inventory.unavailable), inventory.oversized.get(phase),
                                inventory.dropped_rows, inventory.backup is not None, inventory.skipped.get(phase))


def _resolve_transfer_source(source_resolver: Mapping[str, Path], transfer: FileTransfer) -> Path:
    try:
        return source_resolver[transfer.source_id]
    except KeyError as e:
        raise KeyError(f"No local source for planned transfer {transfer.source_id!r}") from e


def _upload_transfer(connection: SshConnection, source: Path, transfer: FileTransfer) -> None:
    if transfer.timeout_seconds is None:
        upload_file(connection, source, transfer.destination)
        return
    upload_file(connection, source, transfer.destination, timeout=transfer.timeout_seconds)


def _destination_is_under(path: str, root: str) -> bool:
    normalized_path = path.rstrip("/")
    normalized_root = root.rstrip("/")
    return normalized_path == normalized_root or normalized_path.startswith(f"{normalized_root}/")


def _ensure_payload_volume_before_transfer(
    connection: SshConnection, plan: DeploymentPlan, transfer: FileTransfer, claims: VolumeClaims,
) -> None:
    if not _destination_is_under(transfer.destination, plan.payload_dir):
        return
    # Choosing the payload home claimed the volume, so a transfer only checks
    # that it is still mounted and claims it again if diskd unmounted it
    # (VolumeClaims).
    if ensure_volume_root_mounted_conn(
        connection,
        plan.volume_root,
        plan.device_path,
        wait_seconds=plan.apple_mount_wait_seconds,
        claims=claims,
    ):
        return
    raise RuntimeError(f"payload volume {plan.volume_root} is not mounted before upload to {transfer.destination}")


def upload_deployment_payload(
    plan: DeploymentPlan,
    *,
    connection: SshConnection,
    source_resolver: Mapping[str, Path],
    claims: VolumeClaims,
    on_uploading: Callable[[FileTransfer], None] | None = None,
    on_uploaded: Callable[[FileTransfer], None] | None = None,
) -> None:
    for transfer in plan.uploads:
        source = _resolve_transfer_source(source_resolver, transfer)
        if on_uploading is not None:
            on_uploading(transfer)
        _ensure_payload_volume_before_transfer(connection, plan, transfer, claims)
        _upload_transfer(connection, source, transfer)
        # upload_file checks the written size in the same command.
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
        timeout = REMOVE_PATHS_TIMEOUT_SECONDS if isinstance(action, RemovePathsAction) else REMOTE_ACTION_TIMEOUT_SECONDS
        run_ssh(connection, command, timeout=timeout)
        if on_action_done is not None:
            on_action_done(action, index, total)


def flush_remote_filesystem_writes(connection: SshConnection) -> None:
    run_ssh(connection, FLUSH_REMOTE_FILESYSTEMS_COMMAND, timeout=FLUSH_REMOTE_FILESYSTEMS_TIMEOUT_SECONDS)


def remote_uninstall_payload(
    connection: SshConnection,
    plan: UninstallPlan,
    *,
    callbacks: OperationCallbacks,
) -> None:
    # One SSH command per action: the stop commands chained with rm in one
    # command failed with rc=255 on NetBSD 4 Time Capsules.
    run_remote_actions(connection, plan.remote_actions)
    # The sync is insurance for the removal on the HFS disks. Without a
    # restart, anything it fails to write out is inert payload with no boot
    # hook; ACPd's restart writes the disks out itself. So a sync that times
    # out on a disk that stopped answering must not keep the device from
    # restarting. Any other failure stops the uninstall like the removal's.
    started = time.monotonic()
    result = "ok"
    try:
        flush_remote_filesystem_writes(connection)
    except SshCommandTimeout:
        result = "timeout"
    callbacks.measurement("uninstall_sync", result=result, duration_sec=round(time.monotonic() - started, 3))
