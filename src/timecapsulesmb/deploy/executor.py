from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable, Mapping

from timecapsulesmb.deploy.commands import RemoteAction, render_remote_actions
from timecapsulesmb.deploy.planner import FLASH_TEXT_UPLOAD_TIMEOUT_SECONDS, DeploymentPlan, FileTransfer, UninstallPlan
from timecapsulesmb.device.storage import MaStVolume, ensure_volume_root_mounted_conn, read_mast_volumes_conn
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
# The migrator walks every file on the share, so a large disk legitimately runs
# for hours: bounding the run itself kills real work. Bound a lack of progress
# instead, from the device, where the migrator's own counter can be watched.
XATTR_HFS_MIGRATION_POLL_SECONDS = 10
# A single entry can chain a disk spin-up, an ATA retry on a marginal sector and
# several fsyncs, so legitimate silence reaches well over a minute. Leave room
# for that: the cost of waiting too long is minutes, of killing too early a
# failed deploy.
XATTR_HFS_MIGRATION_STALL_SECONDS = 300
# Only a backstop for ssh itself wedging. It must stay above the stall budget,
# or it fires first and the watchdog never gets to report the reason.
XATTR_HFS_MIGRATION_TIMEOUT_SECONDS = 900
XATTR_MIGRATION_STALL_SENTINEL = "migration stalled"
# EX_TEMPFAIL. The sentinel travels in the script's own text, so it cannot tell
# a stall from any other failure whose report happens to quote the command; the
# exit status can, and nothing else in this pipeline uses 75.
XATTR_MIGRATION_STALL_EXIT_CODE = 75
# The status file is created empty and written afterwards, so a migrator killed
# in that window leaves nothing readable behind. Report it as its own code: the
# shell exits 2 on a value it cannot parse, which collides with the migrator's
# own code for bad arguments.
XATTR_MIGRATION_NO_STATUS_EXIT_CODE = 76


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
    script = f"""
tdb={shlex.quote(tdb_path)}
migration_ram=/mnt/Memory/tc-xattr-hfs-migrate
migration_progress="$migration_ram.progress"
migration_status_file="$migration_ram.status"
migration_child=
migration_watchdog=
trap 'if [ -n "$migration_child" ]; then kill -TERM "$migration_child" 2>/dev/null || true; wait "$migration_child" 2>/dev/null || true; fi; if [ -n "$migration_watchdog" ]; then kill -TERM "$migration_watchdog" 2>/dev/null || true; fi; rm -f "$migration_ram" "$migration_progress" "$migration_ram.stalled" "$migration_status_file"' 0
trap 'exit 1' 1 2 15
rm -f "$migration_progress" "$migration_ram.stalled" "$migration_status_file"
cp {shlex.quote(migrator_path)} "$migration_ram" || exit $?
chmod 755 "$migration_ram" || exit $?
TC_XATTR_PROGRESS_PATH="$migration_progress" \
    TC_XATTR_STATUS_PATH="$migration_status_file" \
    "$migration_ram" {shlex.quote(phase)} "$tdb" {shlex.quote(legacy_metadata)} {root_args} &
migration_child=$!
migration_stall_note="$migration_ram.stalled"
(
    watch_seen=
    watch_idle=0
    while kill -0 "$migration_child" 2>/dev/null; do
        sleep {XATTR_HFS_MIGRATION_POLL_SECONDS}
        watch_now=
        if [ -f "$migration_progress" ]; then read watch_now < "$migration_progress"; fi
        if [ "$watch_now" != "$watch_seen" ]; then
            watch_seen=$watch_now
            watch_idle=0
            continue
        fi
        watch_idle=$((watch_idle + {XATTR_HFS_MIGRATION_POLL_SECONDS}))
        if [ "$watch_idle" -ge {XATTR_HFS_MIGRATION_STALL_SECONDS} ]; then
            echo "{XATTR_MIGRATION_STALL_SENTINEL} entries=${{watch_seen:-none}}" > "$migration_stall_note"
            kill -TERM "$migration_child" 2>/dev/null || true
            break
        fi
    done
) </dev/null >/dev/null 2>&1 &
migration_watchdog=$!
migration_status=0
wait "$migration_child" 2>/dev/null || true
migration_child=
# The device shell answers 0 from wait for a background child however that child
# exited, so the status the migrator recorded for itself is the only account of
# the run. The binary is copied from this same deploy above, so a missing file
# means the migrator died before reaching any of its own exits, and the file is
# created empty and filled afterwards, so a value that is not a number means it
# died in that window. Both are failures; exiting with the unparsed value would
# fail the shell itself, which reports 2 here and so reads as the migrator's own
# usage code rather than as the error it is.
if [ -f "$migration_status_file" ]; then
    read migration_status < "$migration_status_file" || migration_status=
else
    migration_status=
fi
# The watchdog only writes the note when it decided to kill, so the note means
# the run was cut short whatever the migrator managed to record in the same
# moment. Checked before the status is judged: a killed migrator has no signal
# handler and so records nothing, and calling that absence a second, separate
# failure would only restate the stall in a code that carries no information.
if [ -f "$migration_stall_note" ]; then
    cat "$migration_stall_note" >&2
    echo "migration status at the stall: ${{migration_status:-none recorded}}" >&2
    exit {XATTR_MIGRATION_STALL_EXIT_CODE}
fi
case "$migration_status" in
    ''|*[!0-9]*)
        echo "migration left no usable status" >&2
        migration_status={XATTR_MIGRATION_NO_STATUS_EXIT_CODE}
        ;;
esac
# exit truncates to a byte, so a value of 256 or more would land on 0 and read
# as success. The migrator has no such code today; this keeps it that way.
if [ "$migration_status" -gt 255 ]; then
    echo "migration reported an out of range status: $migration_status" >&2
    migration_status={XATTR_MIGRATION_NO_STATUS_EXIT_CODE}
fi
[ "$migration_status" = 0 ] || exit "$migration_status"
/bin/sync || exit $?
echo migration_phase={shlex.quote(phase)} complete unavailable_roots={len(unavailable)}
""".strip()
    proc = run_ssh(
        connection,
        f"/bin/sh -c {shlex.quote(script)}",
        timeout=XATTR_HFS_MIGRATION_TIMEOUT_SECONDS,
    )
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
