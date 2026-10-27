"""Wipe Internal Disk: format the internal disk the way AirPort Utility's
Erase Disk does, after zeroing both ends of it so Apple's `gpt` can replace
any partition table. Only the internal disk: Apple's erase of a USB disk
beside it formats the internal disk instead (disks_with_apple_labels).

The flow (CLI `tcapsule wipe-internal-disk`, the app's Wipe Internal Disk):
1. prepare_wipe reads only: MaSt, the kernel's geometry (drvctl), the raw
   partition, the other disks' GPT labels, `gpt show` and where the Samba
   files are. Nothing in diskd is called and nothing is mounted or claimed.
2. The user confirms.
3. run_wipe re-checks the disk's identity and the other disks' labels, then
   probes diskd.eraseDisk with an invalid method (only now: the probe's
   harmlessness rests on the handler's check order, verified on two diskd
   builds), stops file sharing and AFP, zeroes 10 MiB at each end with the
   volumes still mounted and idle, reads the ends back, and asks diskd for a
   Quick erase by the disk's uuid. Apple's erase unmounts the disk's volumes
   itself (whatever their diskd.useVolume count), deletes the wedges, and
   partitions and formats.
4. It waits on diskd's erase jobs, reboots through ACP, checks the new volume
   persisted, and starts file sharing again as fsck does, unless the wiped
   disk held the Samba files (then Install comes next).

Known gaps, not fixed: two Macs could wipe the same disk at once (diskd's busy
flag is cleared by any second call, and the app serializes only its own
operations; a device-side lock would need a state file), and a multi-pass
erase started in AirPort Utility runs without a process this can see.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import functools
import re
import shlex
import time

from timecapsulesmb.core.config import MANAGED_PAYLOAD_DIR_NAME
from timecapsulesmb.core.error_codes import ErrorCode
from timecapsulesmb.device.disk_errors import DiskFailingEvidence, read_disk_failing_evidence_conn
from timecapsulesmb.device.errors import DeviceError
from timecapsulesmb.device.storage import (
    MAST_ACP_COMMAND,
    MaStDiskSnapshot,
    MaStPartitionSnapshot,
    StorageDeviceError,
    diskd_rpc_conn,
    disk_size_misreported,
    parse_mast_inventory,
    parse_mast_plist,
    raise_if_acpd_silent,
    read_mast_volumes_with_output_conn,
)
from timecapsulesmb.device.wipe import (
    APPLE_PARTITION_NAMES,
    PS_DISK_JOBS_COMMAND,
    RAW_PARTITION_COMMAND,
    WIPE_LINE_PREFIX,
    DiskGeometry,
    GptVerdict,
    WipeOffsets,
    check_wipe_output,
    decimal_size,
    disk_jobs_in,
    drvctl_command,
    erase_disk_arguments,
    erase_volume_name,
    gpt_partition_names,
    gpt_tables_command,
    gpt_verdict_description,
    parse_drvctl_geometry,
    parse_payload_home_probe,
    raw_disk_path,
    payload_home_probe_command,
    readback_command,
    read_gpt_verdict_conn,
    readback_is_blank,
    render_wipe_script,
    size_misreported_message,
    wipe_offsets,
)
from timecapsulesmb.services import storage as storage_service
from timecapsulesmb.services.callbacks import OperationCallbacks
from timecapsulesmb.services.maintenance import render_disk_maintenance_preamble, reboot_and_restart_sharing
from timecapsulesmb.services.reboot import RebootFlowError
from timecapsulesmb.transport.errors import TransportError
from timecapsulesmb.transport.ssh import SshCommandTimeout, SshConnection, run_ssh, run_ssh_capture_bytes


# 20 MiB at a failing disk's worst seen 32 KB/s is 640 s.
WIPE_REMOTE_TIMEOUT_SECONDS = 20 * 60
ERASE_POLL_SECONDS = 3
# No erase job for this long, and no new volume: the erase failed.
ERASE_IDLE_SECONDS = 60
# A disk job still running at this point is left to finish; nothing reboots.
ERASE_CAP_SECONDS = 30 * 60
PERSIST_READS = 20
PROBE_METHOD = 9
QUICK_ERASE_METHOD = 0
RPC_STATUS_BAD_ARGUMENT = -6705
RPC_STATUS_BUSY = -6721
RPC_STATUS_UNKNOWN_DISK = -6727

SOFT_DISCONNECTED_MESSAGE = "The device has lost contact with this disk, so it is probably failing."
OTHER_DISK_MESSAGE = (
    "Another connected disk has partitions named like an internal disk's (APconfig, APswap, APdata), or could not "
    "be read. While it is connected, the device's erase can format that disk instead of the internal one."
)
NO_INTERNAL_DISK_MESSAGE = "This device has no internal disk."
# diskd's jobs name no disk: gpt and newfs_hfs format one, fsck_hfs and
# mount_hfs check or mount a volume, possibly on another disk.
DISK_JOB_RUNNING_MESSAGE = "The device is formatting a disk, or checking or mounting a volume."
BUSY_MESSAGE = (
    "The device is already erasing this disk, for example a secure erase started in AirPort Utility, which can take hours."
)
STILL_FORMATTING_MESSAGE = "The disk is still being formatted."
DISK_CHANGED_MESSAGE = "The device stopped reporting the internal disk the way it did when the wipe was confirmed."
GEOMETRY_UNREADABLE_MESSAGE = "The device did not report this disk's size."
RPC_MISSING_MESSAGE = "The device's disk service did not accept the erase request."
STOP_FAILED_MESSAGE = "File sharing could not be stopped, so the disk was not wiped."
NO_RESPONSE_MESSAGE = "The disk did not respond while it was being wiped."
READBACK_MESSAGE = "The disk did not keep the zeros written to it."
UNKNOWN_DISK_MESSAGE = "The device did not find this disk when it tried to format it."
ERASE_FAILED_MESSAGE = "The device did not finish formatting the disk."
ERASE_TABLE_CLEARED = "The disk's partition table was already cleared."
ERASE_STILL_FORMATTING = "The device is still formatting the disk."
NOT_PERSISTED_MESSAGE = "The new volume was gone after the device restarted."


class WipeDiskError(DeviceError):
    """A Wipe Internal Disk failure: `code` says what the user does next,
    `reason` the specific cause (telemetry's wipe_reason)."""

    def __init__(self, message: str, *, code: ErrorCode, reason: str, detail: str | None = None) -> None:
        super().__init__(message, code=code, detail=detail)
        self.reason = reason


def require_wipeable(disk: MaStDiskSnapshot) -> None:
    """Refuse an internal disk that reports the wrong size (an erase would
    format it as that size), or that diskd has lost (its RPCs about it can
    hang). Other disks: disks_with_apple_labels."""
    if disk_size_misreported(disk):
        raise WipeDiskError(size_misreported_message(disk.size_mib), code=ErrorCode.DISK_SIZE_MISREPORTED,
                            reason="size_misreported")
    if disk.soft_disconnected:
        raise WipeDiskError(SOFT_DISCONNECTED_MESSAGE, code=ErrorCode.DISK_FAILING, reason="soft_disconnected")


def describe_disk(disk: MaStDiskSnapshot) -> str:
    """The internal disk: Apple's label, device, size, model."""
    size = decimal_size(disk.size_mib) if disk.size_mib is not None else "unknown size"
    model = f", {disk.model}" if disk.model else ""
    return f"{disk.label or disk.device} ({disk.device}, internal, {size}{model})"


def volume_summaries(disk: MaStDiskSnapshot) -> list[str]:
    """The disk's named volumes with their used space ("Data (9 GB used)")."""
    return [
        partition.name + (f" ({decimal_size(partition.used_mib)} used)" if partition.used_mib is not None else "")
        for partition in disk.partitions
        if partition.name
    ]


def read_disks_conn(connection: SshConnection) -> tuple[MaStDiskSnapshot, ...]:
    """MaSt's disks. Nothing is mounted or claimed (no diskd.useVolume)."""
    return parse_mast_inventory(read_mast_volumes_with_output_conn(connection).raw_output)


def internal_disk(disks: tuple[MaStDiskSnapshot, ...]) -> MaStDiskSnapshot:
    """The internal disk, the only one Wipe Internal Disk wipes."""
    disk = next((disk for disk in disks if disk.builtin), None)
    if disk is None:
        raise WipeDiskError(NO_INTERNAL_DISK_MESSAGE, code=ErrorCode.VALIDATION_FAILED, reason="no_internal_disk")
    return disk


def read_raw_partition_conn(connection: SshConnection) -> int | None:
    text = (_read_text(connection, RAW_PARTITION_COMMAND, timeout=30) or "").strip()
    return int(text) if text.isdigit() else None


# The disks the kernel has attached: the wedge names that clash live in the
# kernel, and a disk diskd has not listed (just plugged in, or diskd slow) has
# wedges all the same.
KERNEL_DISKS_COMMAND = "/sbin/sysctl -n hw.disknames"
_KERNEL_DISK = re.compile(r"(?:sd|wd)\d+")
OTHER_DISK_APPLE_LABELS = "apple_labels"
# The verdict's key when the kernel's list was not read.
KERNEL_DISKS_KEY = "hw.disknames"
OTHER_DISK_UNREADABLE = "unreadable"


def kernel_disks_conn(connection: SshConnection) -> tuple[str, ...]:
    """The `sd` and `wd` disks in hw.disknames (no flash, RAM disk or wedge)."""
    text = _read_text(connection, KERNEL_DISKS_COMMAND, timeout=15) or ""
    return tuple(name for name in text.split() if _KERNEL_DISK.fullmatch(name))


@dataclass(frozen=True)
class OtherDisks:
    """disks_with_apple_labels' answer: every disk looked at besides the
    target (`checked`), and the ones that refuse the wipe with why
    (`verdicts`: device -> "apple_labels" or "unreadable"; KERNEL_DISKS_KEY
    when the kernel's list could not be read)."""

    checked: tuple[str, ...]
    verdicts: dict[str, str]


def disks_with_apple_labels(
    connection: SshConnection, disks: tuple[MaStDiskSnapshot, ...], target: MaStDiskSnapshot, raw_partition: int,
) -> OtherDisks:
    """The other disks (MaSt's and the kernel's) that carry a partition named
    APconfig, APswap or APdata in either GPT table ("apple_labels"), or whose
    tables could not be read ("unreadable"). Reads only. A kernel list
    without the target was not read, and refuses too: an empty list would
    otherwise pass with nothing checked.

    Apple's erase deletes the target's wedges, then asks the kernel for new
    ones under those names (DIOCAWEDGE), ignores a refusal and runs newfs_hfs
    on its own guesses, dk0 to dk2. The kernel refuses a name another disk's
    wedge has, and names a disk's wedges after its GPT labels when the disk
    attaches (when the name is free). So a disk with those labels can hold
    those names and dk0 to dk2 (it attached while the internal disk had no
    wedges), and the erase would format it.

    The labels stand in for the wedge names, which nothing on the device can
    list. Known limits: labels changed after the disk attached (by hand)
    differ from its live wedge names; and if the erase cannot delete one of
    the internal disk's own wedges (one in use), that wedge's name clashes
    and dk0 to dk2 are formatted, which is wrong only if another disk owns
    them."""
    kernel = kernel_disks_conn(connection)
    found: dict[str, str] = {}
    if target.device not in kernel:
        found[KERNEL_DISKS_KEY] = OTHER_DISK_UNREADABLE
    devices = [device for device in dict.fromkeys([*(disk.device for disk in disks), *kernel])
               if device != target.device]
    for device in devices:
        geometry = parse_drvctl_geometry(_read_text(connection, drvctl_command(device), timeout=15) or "")
        names = None
        if geometry is not None:
            # A disk too small for both tables reads short: unreadable.
            try:
                # Long enough for a sleeping USB disk to spin up.
                data = run_ssh_capture_bytes(
                    connection, f"/bin/sh -c {shlex.quote(gpt_tables_command(device, raw_partition, geometry))}",
                    timeout=45,
                )
            except TransportError:
                data = b""
            names = gpt_partition_names(data, geometry)
        if names is None:
            found[device] = OTHER_DISK_UNREADABLE
        elif APPLE_PARTITION_NAMES.intersection(names):
            found[device] = OTHER_DISK_APPLE_LABELS
    return OtherDisks(tuple(devices), found)


def require_no_apple_labels_elsewhere(
    connection: SshConnection,
    disks: tuple[MaStDiskSnapshot, ...],
    target: MaStDiskSnapshot,
    raw_partition: int,
    *,
    callbacks: OperationCallbacks,
    field: str,
) -> None:
    """Refuse when disks_with_apple_labels names any disk; its verdicts go to
    telemetry under `field` (one per check, so a later check keeps an
    earlier one's answer)."""
    others = disks_with_apple_labels(connection, disks, target, raw_partition)
    callbacks.update(**{field: others.verdicts, f"{field}_checked": len(others.checked)})
    if others.verdicts:
        raise WipeDiskError(OTHER_DISK_MESSAGE, code=ErrorCode.WIPE_DISK_OTHER_DISK, reason="other_disk",
                            detail=", ".join(f"{device}: {verdict}" for device, verdict in others.verdicts.items()))


def disk_jobs_conn(connection: SshConnection) -> tuple[str, ...] | None:
    """diskd's erase and mount jobs now running; None when the process list
    could not be read."""
    try:
        proc = run_ssh(connection, PS_DISK_JOBS_COMMAND, check=False, timeout=30)
    except TransportError:
        return None
    if proc.returncode != 0 or not (proc.stdout or "").strip():
        return None
    return disk_jobs_in(proc.stdout)


def require_no_disk_jobs(connection: SshConnection) -> None:
    jobs = disk_jobs_conn(connection)
    if jobs is None or jobs:
        detail = f"running: {', '.join(jobs)}" if jobs else "the process list could not be read"
        raise WipeDiskError(DISK_JOB_RUNNING_MESSAGE, code=ErrorCode.WIPE_DISK_JOB_RUNNING, reason="disk_job_running",
                            detail=detail)


@dataclass(frozen=True)
class WipePlan:
    """What prepare_wipe read, and what run_wipe will do."""

    disk: MaStDiskSnapshot
    geometry: DiskGeometry
    offsets: WipeOffsets
    raw_partition: int
    volume_name: str
    gpt_verdict: GptVerdict
    # The wiped disk holds (or may hold) the Samba files, or no disk does:
    # nothing is started after the reboot, and Install comes next.
    offer_install: bool

    @property
    def old_volume_uuids(self) -> frozenset[str]:
        return frozenset(partition.uuid for partition in self.disk.partitions if partition.uuid)

    def confirmation_context(self) -> dict[str, object]:
        """Only what cannot change between the app's two calls: the
        confirmation id hashes it, so used space or gpt text here would ask
        again forever. A different disk under the same name gets a new id."""
        return {"disk": self.disk.device, "disk_uuid": self.disk.uuid, "volume_name": self.volume_name}


def partition_table_text(verdict: GptVerdict) -> str:
    """What `gpt show` found, in words, for the dry run."""
    if verdict.kind == "apple_hfs":
        return "Apple HFS partitions."
    if verdict.kind == "unreadable":
        return "could not be read."
    return gpt_verdict_description(verdict) or "partitions of another system."


def format_wipe_plan(plan: WipePlan) -> str:
    volumes = volume_summaries(plan.disk)
    erase = " ".join(erase_disk_arguments(plan.disk.uuid, plan.disk.device, plan.volume_name, QUICK_ERASE_METHOD))
    lines = [
        "Dry run: Wipe Internal Disk plan",
        "",
        f"Disk: {describe_disk(plan.disk)}" + (f": {'; '.join(volumes)}" if volumes else ""),
        f"Partition table: {partition_table_text(plan.gpt_verdict)}",
        "",
        "Actions:",
        "  stop file sharing and AFP",
        f"  zero {plan.offsets.head_count} x {plan.offsets.block_size} bytes at the start of {raw_disk_path(plan.disk.device, plan.raw_partition)}",
        f"  zero {plan.offsets.tail_count} x {plan.offsets.block_size} bytes from block {plan.offsets.tail_seek}",
        f"  erase (Quick): diskd.eraseDisk {erase}",
        "  reboot, then check the new volume",
        f"Volume name: {plan.volume_name}",
    ]
    if plan.offer_install:
        lines.append("Afterwards: install Samba again (tcapsule deploy).")
    return "\n".join(lines)


def _read_text(connection: SshConnection, command: str, *, timeout: int) -> str | None:
    try:
        proc = run_ssh(connection, f"/bin/sh -c {shlex.quote(command)}", check=False, timeout=timeout)
    except TransportError:
        return None
    return proc.stdout or ""


def _payload_on_disk(connection: SshConnection, raw_mast: str, disk: MaStDiskSnapshot) -> bool:
    """Whether Install comes next: the wiped disk holds (or may hold) the
    Samba files, or no mounted volume elsewhere holds them. Reads only the
    volumes already mounted; nothing is mounted or claimed."""
    volumes = parse_mast_plist(raw_mast)
    if not volumes:
        return True
    output = _read_text(connection, payload_home_probe_command(
        tuple(volume.volume_root for volume in volumes), MANAGED_PAYLOAD_DIR_NAME,
    ), timeout=60)
    states = parse_payload_home_probe(output or "")
    here = [states.get(volume.volume_root) for volume in volumes if volume.disk_device == disk.device]
    elsewhere = [states.get(volume.volume_root) for volume in volumes if volume.disk_device != disk.device]
    if any(state != "no" for state in here):
        return True
    return "yes" not in elsewhere


def prepare_wipe(connection: SshConnection, *, callbacks: OperationCallbacks) -> WipePlan:
    """Everything before the confirmation, for the internal disk. Reads only."""
    callbacks.stage("read_disks")
    read = read_mast_volumes_with_output_conn(connection)
    disks = parse_mast_inventory(read.raw_output)
    disk = internal_disk(disks)
    callbacks.update(
        wipe_disk=disk.device,
        wipe_disk_model=disk.model,
        wipe_disk_serial=disk.product,
        wipe_disk_size_mib=disk.size_mib,
    )
    require_wipeable(disk)
    callbacks.stage("check_disk_jobs")
    require_no_disk_jobs(connection)
    # The wipe ends by restarting the device: a USB volume that exists only
    # until then would go with it. The internal disk's is what the wipe fixes.
    callbacks.stage("check_in_memory_volumes")
    storage_service.require_no_in_memory_volumes(
        connection, callbacks, disks=disks, usb_only=True,
        error=functools.partial(WipeDiskError, reason="volumes_in_memory"),
    )

    callbacks.stage("read_disk_geometry")
    geometry_text = _read_text(connection, drvctl_command(disk.device), timeout=30)
    geometry = parse_drvctl_geometry(geometry_text or "")
    raw_partition = read_raw_partition_conn(connection)
    if geometry is None or raw_partition is None:
        raise WipeDiskError(GEOMETRY_UNREADABLE_MESSAGE, code=ErrorCode.WIPE_DISK_RESTART_NEEDED,
                            reason="geometry_unreadable")
    callbacks.stage("check_other_disks")
    require_no_apple_labels_elsewhere(connection, disks, disk, raw_partition, callbacks=callbacks,
                                      field="wipe_other_disks")
    try:
        offsets = wipe_offsets(geometry)
    except ValueError as exc:
        raise WipeDiskError(f"This disk cannot be wiped: {exc}.", code=ErrorCode.VALIDATION_FAILED,
                            reason="unsupported_geometry") from exc
    callbacks.update(wipe_sector_size=geometry.sector_size, wipe_sectors=geometry.sectors)

    callbacks.stage("read_partition_table")
    verdict = read_gpt_verdict_conn(connection, disk.device)
    callbacks.update(wipe_gpt_verdict=verdict.kind, wipe_gpt_formats=list(verdict.formats))

    callbacks.stage("find_payload_home")
    offer_install = _payload_on_disk(connection, read.raw_output, disk)
    volume_name = erase_volume_name(tuple(partition.name for partition in disk.partitions
                                          if partition.format == "hfs" and partition.name))
    return WipePlan(disk, geometry, offsets, raw_partition, volume_name, verdict, offer_install)


def wipe_failing_evidence(connection: SshConnection, plan: WipePlan) -> DiskFailingEvidence | None:
    """What says the disk to be wiped is failing: its own kernel errors, or
    the Samba files unreadable when they may be on it. Another disk's
    errors are no reason to warn about this one."""
    return read_disk_failing_evidence_conn(
        connection, internal_device=plan.disk.device, holds_payload=plan.offer_install,
    )


@dataclass(frozen=True)
class WipeOutcome:
    new_volume_uuid: str
    volume_name: str
    offer_install: bool
    runtime_restart_error: str | None = None
    runtime_restart_code: ErrorCode | None = None


def _check_identity(connection: SshConnection, plan: WipePlan, callbacks: OperationCallbacks) -> None:
    """The disk is still the one confirmed, and no other disk carries Apple's
    partition names: one plugged in since the confirmation refuses too. This
    is the last check before anything is written (see run_wipe)."""
    disks = read_disks_conn(connection)
    current = next((disk for disk in disks if disk.device == plan.disk.device), None)
    if current is None or not plan.disk.uuid or current.uuid != plan.disk.uuid:
        raise WipeDiskError(DISK_CHANGED_MESSAGE, code=ErrorCode.WIPE_DISK_CHANGED, reason="identity_changed")
    require_no_apple_labels_elsewhere(connection, disks, current, plan.raw_partition, callbacks=callbacks,
                                      field="wipe_other_disks_confirmed")


def _probe_erase_rpc(connection: SshConnection, plan: WipePlan, callbacks: OperationCallbacks) -> None:
    reply = diskd_rpc_conn(connection, "eraseDisk", *erase_disk_arguments(
        plan.disk.uuid, plan.disk.device, plan.volume_name, PROBE_METHOD,
    ))
    callbacks.update(wipe_probe_status=reply.status, wipe_probe_error=reply.error_code)
    if reply.status == RPC_STATUS_BAD_ARGUMENT:
        return
    if reply.status == RPC_STATUS_BUSY:
        raise WipeDiskError(BUSY_MESSAGE, code=ErrorCode.WIPE_DISK_BUSY, reason="erase_busy")
    # Status 0 would be an erase that started (a diskd that skipped the method
    # check); it is reported like any other unexpected answer.
    reason = "rpc_missing" if reply.error_code is not None else "probe_unexpected"
    raise WipeDiskError(RPC_MISSING_MESSAGE, code=ErrorCode.WIPE_DISK_RESTART_NEEDED, reason=reason,
                        detail=f"status {reply.status}, error {reply.error_code}")


def _raise_if_acpd_silent_after_wipe(connection: SshConnection, exc: TransportError) -> None:
    """After an erase request that got no answer: raise when ACPd itself is
    silent. The reboot goes through ACPd, so none is requested; the guidance
    says to unplug the device, after any format still running finishes.
    Returns when ACPd answers: diskd may be erasing, and the wait decides."""
    try:
        raise_if_acpd_silent(connection, after=exc if isinstance(exc, SshCommandTimeout) else None)
    except StorageDeviceError as silent:
        jobs = disk_jobs_conn(connection)
        detail = f" {ERASE_STILL_FORMATTING}" if jobs else ""
        if silent.code == ErrorCode.DISK_NOT_RESPONDING:
            # A process stuck on the disk, which ACPd waits behind: the disk
            # is what to report, with the stuck processes as its detail.
            raise WipeDiskError(f"{silent.message} {ERASE_TABLE_CLEARED}{detail}", code=ErrorCode.DISK_NOT_RESPONDING,
                                reason="disk_stalled", detail=silent.detail) from exc
        raise WipeDiskError(f"{silent} {ERASE_TABLE_CLEARED}{detail}", code=ErrorCode.ACPD_BROKEN,
                            reason="acpd_silent") from exc


def _reboot_after_failure(connection: SshConnection, error: WipeDiskError, callbacks: OperationCallbacks) -> WipeDiskError:
    """After a failure past the wipe: file sharing and AFP are stopped, which
    only a reboot undoes (AGENTS.md lets afpserver stop only before one). The
    disk is blank or half-formatted, so nothing is started. The reason goes to
    telemetry first: a failed reboot raises its own error instead."""
    callbacks.update(wipe_reason=error.reason)
    callbacks.stage("reboot")
    try:
        reboot_and_restart_sharing(connection, wait=True, netbsd4_autostart=None, callbacks=callbacks)
    except RebootFlowError as exc:
        raise RebootFlowError(f"{exc}\n{error}", code=exc.code) from exc
    return error


def data_volume(disk: MaStDiskSnapshot) -> MaStPartitionSnapshot | None:
    """The disk's data volume: its largest HFS partition with a uuid. Apple's
    erase makes APconfig and APswap HFS partitions too; only the data volume
    marks the erase as done."""
    volumes = [partition for partition in disk.partitions if partition.format == "hfs" and partition.uuid]
    return max(volumes, key=lambda partition: partition.size_mib or 0, default=None)


def _wait_for_erase(
    connection: SshConnection,
    plan: WipePlan,
    *,
    callbacks: OperationCallbacks,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> str:
    """The new volume's uuid once MaSt lists it on the disk. diskd's erase jobs
    decide whether it is still working: a MaSt read can block or fail while
    they run (diskd erases on its main loop), and that never counts as an
    ACPd failure here."""
    started = clock()
    last_job = started
    while True:
        jobs = disk_jobs_conn(connection)
        if jobs is None or jobs:
            last_job = clock()
        disks: tuple[MaStDiskSnapshot, ...] = ()
        try:
            proc = run_ssh(connection, MAST_ACP_COMMAND, check=False, timeout=30)
            disks = parse_mast_inventory(proc.stdout or "")
        except (TransportError, ValueError):
            disks = ()
        for disk in disks:
            if disk.uuid != plan.disk.uuid:
                continue
            data = data_volume(disk)
            if data is not None and data.uuid not in plan.old_volume_uuids:
                return data.uuid
        now = clock()
        if now - started >= ERASE_CAP_SECONDS:
            if jobs is None or jobs:
                raise WipeDiskError(STILL_FORMATTING_MESSAGE, code=ErrorCode.WIPE_DISK_ERASE_RUNNING,
                                    reason="still_formatting")
            raise _reboot_after_failure(connection, WipeDiskError(
                ERASE_FAILED_MESSAGE, code=ErrorCode.WIPE_DISK_ERASE_FAILED, reason="erase_cap"), callbacks)
        if not jobs and jobs is not None and now - last_job >= ERASE_IDLE_SECONDS:
            raise _reboot_after_failure(connection, WipeDiskError(
                ERASE_FAILED_MESSAGE, code=ErrorCode.WIPE_DISK_ERASE_FAILED, reason="erase_idle"), callbacks)
        sleep(ERASE_POLL_SECONDS)


def _check_persisted(plan: WipePlan, new_uuid: str, sleep: Callable[[float], None]) -> Callable[[SshConnection], None]:
    """After the reboot: the new volume must still be there."""
    def check(connection: SshConnection) -> None:
        for read in range(PERSIST_READS):
            try:
                disks = read_disks_conn(connection)
            except (DeviceError, TransportError, ValueError):
                disks = ()
            for disk in disks:
                if disk.uuid == plan.disk.uuid and any(partition.uuid == new_uuid for partition in disk.partitions):
                    return
            if read + 1 < PERSIST_READS:
                sleep(ERASE_POLL_SECONDS)
        raise WipeDiskError(NOT_PERSISTED_MESSAGE, code=ErrorCode.WIPE_DISK_ERASE_FAILED, reason="not_persisted")
    return check


def run_wipe(
    connection: SshConnection,
    plan: WipePlan,
    *,
    netbsd4_autostart: bool | None,
    callbacks: OperationCallbacks,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> WipeOutcome:
    """Everything after the confirmation. Raises WipeDiskError (or
    RebootFlowError for the reboot)."""
    callbacks.stage("check_disk_identity")
    _check_identity(connection, plan, callbacks)
    require_no_disk_jobs(connection)
    callbacks.stage("probe_erase")
    _probe_erase_rpc(connection, plan, callbacks)

    callbacks.stage("wipe_disk")
    script = "\n".join([
        *render_disk_maintenance_preamble(),
        render_wipe_script(plan.disk.device, plan.raw_partition, plan.geometry, plan.offsets),
    ])
    wipe_started = clock()
    timed_out = False
    try:
        proc = run_ssh(connection, f"/bin/sh -c {shlex.quote(script)}", check=False, timeout=WIPE_REMOTE_TIMEOUT_SECONDS)
        output = proc.stdout or ""
    except SshCommandTimeout:
        output, timed_out = "", True
    callbacks.update(wipe_seconds=round(clock() - wipe_started, 1))
    if WIPE_LINE_PREFIX not in output:
        if timed_out:
            raise WipeDiskError(NO_RESPONSE_MESSAGE, code=ErrorCode.DISK_FAILING, reason="no_response")
        # The managed stop failed before anything was written.
        raise WipeDiskError(STOP_FAILED_MESSAGE, code=ErrorCode.WIPE_DISK_RESTART_NEEDED, reason="stop_failed",
                            detail=output.strip()[-500:] or None)
    report = check_wipe_output(output, plan.offsets)
    if not report.complete:
        if report.reason == "disk_changed":
            raise WipeDiskError(DISK_CHANGED_MESSAGE, code=ErrorCode.WIPE_DISK_CHANGED, reason="target_changed")
        raise WipeDiskError(NO_RESPONSE_MESSAGE, code=ErrorCode.DISK_FAILING, reason=report.reason or "no_response")

    callbacks.stage("check_wipe")
    try:
        data = run_ssh_capture_bytes(
            connection, f"/bin/sh -c {shlex.quote(readback_command(plan.disk.device, plan.raw_partition, plan.geometry))}",
            timeout=120,
        )
    except TransportError:
        data = b""
    if not readback_is_blank(data, plan.geometry):
        raise WipeDiskError(READBACK_MESSAGE, code=ErrorCode.DISK_FAILING, reason="readback",
                            detail=f"read {len(data)} bytes")

    # The other disks were last checked after the confirmation
    # (_check_identity), before anything was written. A disk with Apple's
    # labels plugged in during the zeroing (seconds; minutes on a failing
    # disk) is not caught: a check here would refuse with the internal disk
    # already cleared and file sharing stopped, and the window is too short
    # to be worth that.
    callbacks.stage("erase_disk")
    erase_started = clock()
    try:
        reply = diskd_rpc_conn(connection, "eraseDisk", *erase_disk_arguments(
            plan.disk.uuid, plan.disk.device, plan.volume_name, QUICK_ERASE_METHOD,
        ), timeout=120)
    except TransportError as exc:
        # No answer: ACPd may have stopped (then nothing can reboot the
        # device), or diskd may be busy with the erase it was asked for.
        _raise_if_acpd_silent_after_wipe(connection, exc)
        callbacks.update(wipe_erase_status=None, wipe_erase_error="no_answer")
        reply = None
    if reply is not None:
        callbacks.update(wipe_erase_status=reply.status, wipe_erase_error=reply.error_code)
    if reply is not None and reply.status == RPC_STATUS_BUSY:
        raise WipeDiskError(BUSY_MESSAGE, code=ErrorCode.WIPE_DISK_BUSY, reason="erase_busy")
    if reply is not None and reply.status != 0:
        reason = "unknown_disk" if reply.status == RPC_STATUS_UNKNOWN_DISK else (
            "rpc_missing" if reply.error_code is not None else "erase_refused")
        message = UNKNOWN_DISK_MESSAGE if reason == "unknown_disk" else ERASE_FAILED_MESSAGE
        raise _reboot_after_failure(connection, WipeDiskError(
            message, code=ErrorCode.WIPE_DISK_ERASE_FAILED, reason=reason,
            detail=f"status {reply.status}, error {reply.error_code}",
        ), callbacks)

    callbacks.stage("wait_for_erase")
    new_uuid = _wait_for_erase(connection, plan, callbacks=callbacks, clock=clock, sleep=sleep)
    callbacks.update(wipe_erase_seconds=round(clock() - erase_started, 1), wipe_new_volume_uuid=new_uuid)

    callbacks.stage("reboot")
    restart = reboot_and_restart_sharing(
        connection,
        wait=True,
        netbsd4_autostart=None if plan.offer_install else netbsd4_autostart,
        callbacks=callbacks,
        after_reboot=_check_persisted(plan, new_uuid, sleep),
    )
    callbacks.update(wipe_persisted=True)
    return WipeOutcome(new_uuid, plan.volume_name, plan.offer_install, restart.error, restart.code)
