from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable, Mapping

from timecapsulesmb.deploy.commands import RemoteAction, render_remote_actions
from timecapsulesmb.deploy.planner import FLASH_TEXT_UPLOAD_TIMEOUT_SECONDS, DeploymentPlan, FileTransfer, UninstallPlan
from timecapsulesmb.device.storage import MaStVolume, ensure_volume_root_mounted_conn, read_mast_volumes_conn
from timecapsulesmb.transport.errors import SshCommandTimeout
from timecapsulesmb.transport.ssh import SshConnection, run_scp, run_ssh


DETACHED_SHUTDOWN_REBOOT_COMMAND = (
    "/bin/sh -c 'exec </dev/null >/dev/null 2>&1; "
    "(/bin/sync; /bin/sleep 1; "
    "/sbin/shutdown -r now || /sbin/reboot"
    ") & exit 0'"
)
REBOOT_REQUEST_TIMEOUT_SECONDS = 30
PAYLOAD_FLUSH_SETTLE_SECONDS = 5
FLUSH_REMOTE_FILESYSTEMS_COMMAND = (
    f"/bin/sh -c {shlex.quote(f'/bin/sync; /bin/sleep {PAYLOAD_FLUSH_SETTLE_SECONDS}; /bin/sync')}"
)
# Time Capsule HFS disks can spend well over 30 seconds flushing the Samba
# payload after a slow upload. Keep this bounded, but long enough for real disks.
FLUSH_REMOTE_FILESYSTEMS_TIMEOUT_SECONDS = 300
# Old disks may contain millions of files. Migration runs only during deploy;
# allow a long bounded scan without imposing its timeout on ordinary SSH calls.
XATTR_HFS_MIGRATION_TIMEOUT_SECONDS = 6 * 60 * 60
XATTR_MIGRATION_LOG_TAIL_BYTES = 8192


@dataclass(frozen=True)
class XattrMigrationResult:
    output: str
    roots: tuple[MaStVolume, ...]
    unavailable_roots: tuple[str, ...] = ()


def migrate_xattr_tdb_to_hfs(
    connection: SshConnection,
    plan: DeploymentPlan,
    *,
    phase: str,
    legacy_metadata: str,
    roots: tuple[MaStVolume, ...] | None = None,
) -> XattrMigrationResult:
    """Migrate legacy metadata before native-HFS smbd is started."""
    if phase not in {"copy", "cleanup"}:
        raise ValueError(f"unsupported xattr migration phase: {phase}")
    if legacy_metadata not in {"stream", "netatalk"}:
        raise ValueError(f"unsupported legacy fruit metadata backend: {legacy_metadata}")
    tdb_path = f"{plan.private_dir}/xattr.tdb"
    migrator_path = plan.payload_targets["xattr_migrator"]
    if not ensure_volume_root_mounted_conn(
        connection, plan.volume_root, plan.device_path,
        wait_seconds=plan.apple_mount_wait_seconds,
    ):
        raise RuntimeError("migration payload volume is unavailable")
    probe = run_ssh(connection, f"test -f {shlex.quote(tdb_path)}", check=False)
    if probe.returncode == 1:
        return XattrMigrationResult(
            f"migration_phase={phase} skipped reason=no_legacy_tdb",
            (),
        )
    if probe.returncode != 0:
        raise RuntimeError("could not probe legacy metadata")
    candidates = tuple(read_mast_volumes_conn(connection)) if roots is None else roots
    if not candidates:
        raise RuntimeError("migration found no attached HFS volumes")
    mounted: list[MaStVolume] = []
    unavailable: list[str] = []
    for volume in candidates:
        if ensure_volume_root_mounted_conn(
            connection, volume.volume_root, volume.device_path,
            wait_seconds=plan.apple_mount_wait_seconds,
        ):
            mounted.append(volume)
        else:
            unavailable.append(volume.volume_root)
    if not mounted:
        if phase == "cleanup" and roots is not None:
            return XattrMigrationResult(
                "migration_phase=cleanup skipped reason=copied_roots_unavailable",
                (),
                tuple(unavailable),
            )
        raise RuntimeError("migration found no mounted HFS volumes")
    root_args = shlex.join([volume.volume_root for volume in mounted])
    migration_log = f"{plan.payload_dir}/logs/xattr-migration-{phase}.log"
    script = f"""
tdb={shlex.quote(tdb_path)}
migration_ram=/mnt/Memory/tc-xattr-hfs-migrate
migration_log={shlex.quote(migration_log)}
migration_child=
trap 'if [ -n "$migration_child" ]; then kill -TERM "$migration_child" 2>/dev/null || true; wait "$migration_child" 2>/dev/null || true; fi; rm -f "$migration_ram"' 0
trap 'exit 1' 1 2 15
cp {shlex.quote(migrator_path)} "$migration_ram" || exit $?
chmod 755 "$migration_ram" || exit $?
mkdir -p {shlex.quote(f'{plan.payload_dir}/logs')} || exit $?
{{
    printf 'migration_phase=%s legacy_metadata=%s timeout_seconds=%s\\n' {shlex.quote(phase)} {shlex.quote(legacy_metadata)} {XATTR_HFS_MIGRATION_TIMEOUT_SECONDS}
    /bin/date -u '+started_at=%Y-%m-%dT%H:%M:%SZ'
    printf 'tdb=%s\\n' "$tdb"
    /bin/ls -ln "$tdb"
    printf 'root=%s\\n' {root_args}
}} >"$migration_log" || exit $?
"$migration_ram" {shlex.quote(phase)} "$tdb" {shlex.quote(legacy_metadata)} {root_args} >>"$migration_log" 2>&1 &
migration_child=$!
migration_status=0
wait "$migration_child" || migration_status=$?
migration_child=
printf 'migration_exit_code=%s\\n' "$migration_status" >>"$migration_log"
/bin/date -u '+finished_at=%Y-%m-%dT%H:%M:%SZ' >>"$migration_log"
cat "$migration_log"
[ "$migration_status" = 0 ] || exit "$migration_status"
/bin/sync || exit $?
echo migration_phase={shlex.quote(phase)} complete unavailable_roots={len(unavailable)}
""".strip()
    try:
        proc = run_ssh(
            connection,
            f"/bin/sh -c {shlex.quote(script)}",
            timeout=XATTR_HFS_MIGRATION_TIMEOUT_SECONDS,
        )
    except SshCommandTimeout as exc:
        # A timed-out command never reaches cat. Fetch a bounded, best-effort
        # snapshot without letting a failed diagnostic read hide the timeout.
        try:
            saved = run_ssh(connection,
                            f"/usr/bin/tail -c {XATTR_MIGRATION_LOG_TAIL_BYTES} {shlex.quote(migration_log)}",
                            check=False, timeout=10)
            detail = saved.stdout[-XATTR_MIGRATION_LOG_TAIL_BYTES:].strip() if saved.returncode == 0 else "unavailable"
        except Exception:
            detail = "unavailable"
        raise SshCommandTimeout(
            f"{exc}\nSaved migration log snapshot ({migration_log}; may be incomplete):\n{detail}"
        ) from exc
    return XattrMigrationResult(proc.stdout, tuple(mounted), tuple(unavailable))


def _flash_upload_tmp_path(destination: str) -> str:
    path = PurePosixPath(destination)
    return str(path.with_name(f".{path.name}.tmp"))


def _cleanup_flash_upload_tmp_paths(connection: SshConnection, destinations: Iterable[str]) -> None:
    tmp_paths = tuple(dict.fromkeys(_flash_upload_tmp_path(destination) for destination in destinations))
    if not tmp_paths:
        return
    quoted_paths = " ".join(shlex.quote(path) for path in tmp_paths)
    run_ssh(connection, f"/bin/sh -c {shlex.quote(f'rm -f {quoted_paths}')}")


def _best_effort_cleanup_flash_upload_tmp_path(connection: SshConnection, tmp_destination: str) -> None:
    try:
        run_ssh(connection, f"/bin/sh -c {shlex.quote(f'rm -f {shlex.quote(tmp_destination)}')}", check=False)
    except Exception:
        pass


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
    try:
        run_scp(connection, source, tmp_destination, timeout=timeout)
        install_script = (
            "rc=0; "
            f"chmod {quoted_mode} {quoted_tmp} && mv -f {quoted_tmp} {quoted_destination} || rc=$?; "
            f"rm -f {quoted_tmp}; "
            'exit "$rc"'
        )
        run_ssh(connection, f"/bin/sh -c {shlex.quote(install_script)}")
    except Exception:
        _best_effort_cleanup_flash_upload_tmp_path(connection, tmp_destination)
        raise


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
    planned_modes = {permission.path: permission.mode for permission in plan.permissions}
    flash_tmp_paths_cleaned = False
    for transfer in plan.uploads:
        source = _resolve_transfer_source(source_resolver, transfer)
        if on_uploading is not None:
            on_uploading(transfer)
        _ensure_payload_volume_before_transfer(connection, plan, transfer)
        if transfer.mode in {"scp", "generated"}:
            _scp_transfer(connection, source, transfer)
        elif transfer.mode == "flash_atomic":
            if not flash_tmp_paths_cleaned:
                _cleanup_flash_upload_tmp_paths(
                    connection,
                    (planned.destination for planned in plan.uploads if planned.mode == "flash_atomic"),
                )
                flash_tmp_paths_cleaned = True
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
