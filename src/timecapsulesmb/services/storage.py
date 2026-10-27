from __future__ import annotations

from collections.abc import Callable

from timecapsulesmb.core.error_codes import ErrorCode
from timecapsulesmb.device.errors import DeviceError
from timecapsulesmb.device.storage import (
    MAST_DISCOVERY_ATTEMPTS,
    MAST_DISCOVERY_DELAY_SECONDS,
    MaStDiscoveryResult,
    MaStDiskSnapshot,
    MaStVolume,
    VolumeClaims,
    mast_volumes_debug_summary,
    mounted_mast_volumes_conn,
    parse_mast_inventory,
    read_mast_volumes_conn,
    read_mast_volumes_with_output_conn,
    wait_for_mast_volumes_conn,
)
from timecapsulesmb.device.wipe import in_memory_volume_message, read_in_memory_volumes_conn
from timecapsulesmb.services.callbacks import OperationCallbacks
from timecapsulesmb.transport.ssh import SshConnection


MAST_ACP_OUTPUT_DEBUG_LIMIT = 8192


def mast_acp_output_debug_text(raw_output: str) -> str:
    if not raw_output:
        return "<empty>"
    if len(raw_output) <= MAST_ACP_OUTPUT_DEBUG_LIMIT:
        return raw_output
    omitted = len(raw_output) - MAST_ACP_OUTPUT_DEBUG_LIMIT
    return f"{raw_output[:MAST_ACP_OUTPUT_DEBUG_LIMIT]}...<truncated {omitted} chars>"


def _best_effort_mast_debug_summary(volumes: object) -> object | None:
    try:
        return mast_volumes_debug_summary(volumes)
    except Exception:
        return None


def _record_mast_read_diagnostics(
    callbacks: OperationCallbacks,
    volumes: tuple[MaStVolume, ...],
) -> None:
    callbacks.debug(
        mast_volume_count=len(volumes),
        mast_candidates=_best_effort_mast_debug_summary(volumes),
    )


def read_mast_volumes_with_diagnostics(
    connection: SshConnection,
    *,
    callbacks: OperationCallbacks,
    stage: str = "read_mast",
    read_mast_volumes: Callable[[SshConnection], tuple[MaStVolume, ...]] | None = None,
) -> tuple[MaStVolume, ...]:
    callbacks.stage(stage)
    if read_mast_volumes is None:
        read_mast_volumes = read_mast_volumes_conn
    volumes = read_mast_volumes(connection)
    _record_mast_read_diagnostics(callbacks, volumes)
    return volumes


def mount_mast_volumes_with_diagnostics(
    connection: SshConnection,
    *,
    callbacks: OperationCallbacks,
    wait_seconds: int,
    claims: VolumeClaims,
    read_stage: str = "read_mast",
    mount_stage: str = "mount_mast_volumes",
    read_mast_volumes: Callable[[SshConnection], tuple[MaStVolume, ...]] | None = None,
    mounted_mast_volumes: Callable[
        [SshConnection, tuple[MaStVolume, ...]],
        tuple[MaStVolume, ...],
    ] | None = None,
) -> tuple[MaStVolume, ...]:
    mast_volumes = read_mast_volumes_with_diagnostics(
        connection,
        callbacks=callbacks,
        stage=read_stage,
        read_mast_volumes=read_mast_volumes,
    )
    callbacks.stage(mount_stage)
    if mounted_mast_volumes is None:
        mounted_mast_volumes = mounted_mast_volumes_conn
    mounted_volumes = mounted_mast_volumes(
        connection,
        mast_volumes,
        wait_seconds=wait_seconds,
        claims=claims,
    )
    callbacks.debug(
        mast_mounted_volume_count=len(mounted_volumes),
        mast_mounted_candidates=_best_effort_mast_debug_summary(mounted_volumes),
    )
    return mounted_volumes


def wait_for_mast_volumes_with_diagnostics(
    connection: SshConnection,
    *,
    callbacks: OperationCallbacks,
    attempts: int = MAST_DISCOVERY_ATTEMPTS,
    delay_seconds: int = MAST_DISCOVERY_DELAY_SECONDS,
    stage: str = "read_mast",
    wait_for_mast_volumes: Callable[..., MaStDiscoveryResult] | None = None,
) -> MaStDiscoveryResult:
    callbacks.stage(stage)
    if wait_for_mast_volumes is None:
        wait_for_mast_volumes = wait_for_mast_volumes_conn
    mast_discovery = wait_for_mast_volumes(
        connection,
        attempts=attempts,
        delay_seconds=delay_seconds,
    )
    mast_volumes = mast_discovery.volumes
    fields: dict[str, object] = {
        "mast_read_attempts": mast_discovery.attempts,
        "mast_volume_count": len(mast_volumes),
        "mast_candidates": _best_effort_mast_debug_summary(mast_volumes),
    }
    if not mast_volumes:
        fields["mast_acp_output_chars"] = len(mast_discovery.raw_output)
        fields["mast_acp_output"] = mast_acp_output_debug_text(mast_discovery.raw_output)
    callbacks.debug(**fields)
    return mast_discovery


RESTARTS_MESSAGE = "This restarts the device, so it stopped before changing anything."


def require_no_in_memory_volumes(
    connection: SshConnection,
    callbacks: OperationCallbacks,
    *,
    disks: tuple[MaStDiskSnapshot, ...] | None = None,
    usb_only: bool = False,
    error: Callable[..., DeviceError] = DeviceError,
) -> None:
    """Stop a command that restarts the device (Install, Disk Repair,
    Uninstall, Wipe Internal Disk, Disable SSH) before it changes anything while a volume
    exists only until the next restart (device.wipe.read_in_memory_volumes_conn):
    the restart would take the volume and everything saved on it. `disks`
    defaults to a MaSt read; one that fails, or a log that cannot be read,
    gives no verdict. usb_only: Wipe Internal Disk, whose wipe is what fixes
    the internal disk. The internal disk's volume is named first: its
    recovery is the Wipe button."""
    if disks is None:
        try:
            disks = parse_mast_inventory(read_mast_volumes_with_output_conn(connection).raw_output)
        except Exception as e:
            callbacks.update(erase_log={"error": f"MaSt: {e or type(e).__name__}"})
            return
    if not any(disk.partitions for disk in disks):
        # Nothing to flag: no log read.
        return
    check = read_in_memory_volumes_conn(connection, disks)
    # On the finished event, so commands that went on count too.
    callbacks.update(erase_log=check.debug())
    flagged = [pair for pair in check.flagged if not (usb_only and pair[0].builtin)]
    if not flagged:
        return
    disk, partition = min(flagged, key=lambda pair: not pair[0].builtin)
    raise error(
        f"{in_memory_volume_message(disk, partition)} {RESTARTS_MESSAGE}",
        code=ErrorCode.DISK_VOLUMES_IN_MEMORY if disk.builtin else ErrorCode.USB_DISK_VOLUMES_IN_MEMORY,
        detail=f"{partition.device} on {disk.device}",
    )
