"""Wipe Internal Disk's device side: the disk's geometry, the commands that
zero both ends of the disk and read them back, and what the device says about
the disk (its partition table, diskd's disk jobs, where the Samba files are,
and which volumes an erase left only in memory).

Apple's `gpt` rejects some partition tables ("bogus map": a table made for a
bigger disk, partitions past the end or overlapping, a legacy MBR beside a
GPT), and cannot create one beside a legacy MBR partition that reaches the
last sector ("no room for the backup header"). It still exits 0, so AirPort
Utility's erase formats partitions that exist only in memory, gone at the
next boot. Zeroing 10 MiB at each end removes every such table; Apple's own
erase then writes its layout (services.wipe_disk)."""
from __future__ import annotations

from dataclasses import dataclass, field
import re
import shlex
from typing import TYPE_CHECKING
import unicodedata

from timecapsulesmb.transport.errors import TransportError
from timecapsulesmb.transport.ssh import SshConnection, run_ssh

if TYPE_CHECKING:
    from timecapsulesmb.device.storage import MaStDiskSnapshot, MaStPartitionSnapshot



WIPE_BYTES_PER_END = 10 * 1024 * 1024
# A dd larger than this gains nothing on these disks.
WIPE_MAX_BLOCK_BYTES = 1024 * 1024
SUPPORTED_SECTOR_SIZES = (512, 4096)
WIPE_LINE_PREFIX = "tcapsule-wipe: "
WIPE_GEOMETRY_CHANGED_LINE = f"{WIPE_LINE_PREFIX}the disk changed"
WIPE_DONE_LINE = f"{WIPE_LINE_PREFIX}done"
# The processes diskd runs (through sh -c) to erase a disk and mount the new
# volume: the teardown's umount, gpt, newfs_hfs, then fsck_hfs and mount_hfs.
DISK_JOB_COMMANDS = frozenset({"umount", "gpt", "newfs_hfs", "fsck_hfs", "mount_hfs"})
# ps without a command-line column, so it never waits on a process's map lock.
PS_DISK_JOBS_COMMAND = "/bin/ps ax -o pid= -o ppid= -o stat= -o ucomm="
# A gpt show that hangs on a failing disk is killed after this long, so it
# cannot outlive the SSH call (the device has no `timeout`).
GPT_SHOW_KILL_SECONDS = 60
DEFAULT_VOLUME_NAME = "Data"
VOLUME_NAME_MAX_CHARACTERS = 255
# diskd builds `newfs_hfs -J -v "<name>"`, so these would break its quoting;
# ":" is HFS's path separator.
_VOLUME_NAME_FORBIDDEN = frozenset('"`$\\:')
_DRVCTL_INTEGER = r"<key>{key}</key>\s*<integer>\s*([^<\s]+)\s*</integer>"


@dataclass(frozen=True)
class DiskGeometry:
    """The kernel's sector size and sector count (`drvctl -p`), as numbers and
    as the exact text drvctl printed, which the wipe script compares again
    right before it writes."""

    sector_size: int
    sectors: int
    sector_size_text: str
    sectors_text: str

    @property
    def size_bytes(self) -> int:
        return self.sector_size * self.sectors


def _drvctl_integer(text: str, key: str) -> tuple[int, str] | None:
    match = re.search(_DRVCTL_INTEGER.format(key=re.escape(key)), text)
    if match is None:
        return None
    try:
        return int(match.group(1), 0), match.group(1)
    except ValueError:
        return None


def parse_drvctl_geometry(text: str) -> DiskGeometry | None:
    """`drvctl -p <disk>`'s `disk-info` geometry, or None when it lacks one."""
    sector_size = _drvctl_integer(text, "sector-size")
    sectors = _drvctl_integer(text, "sectors-per-unit")
    if sector_size is None or sectors is None or sector_size[0] <= 0 or sectors[0] <= 0:
        return None
    return DiskGeometry(sector_size[0], sectors[0], sector_size[1], sectors[1])


def drvctl_command(disk: str) -> str:
    return f"/sbin/drvctl -p {shlex.quote(disk)}"


@dataclass(frozen=True)
class WipeOffsets:
    """Both ends of the disk in dd's blocks of `block_size` bytes. The numbers
    are computed here: NetBSD 4's sh arithmetic is 32-bit."""

    block_size: int
    head_count: int
    tail_seek: int
    tail_count: int


def wipe_offsets(geometry: DiskGeometry) -> WipeOffsets:
    """The largest power of two up to 1 MiB that divides the disk size, so it
    divides the tail's start (size - 10 MiB) too, and is never below a
    sector. Raises ValueError for a sector size or a disk dd cannot wipe."""
    if geometry.sector_size not in SUPPORTED_SECTOR_SIZES:
        raise ValueError(f"unsupported sector size {geometry.sector_size}")
    size = geometry.size_bytes
    if size < 2 * WIPE_BYTES_PER_END:
        raise ValueError(f"disk too small to wipe ({size} bytes)")
    block = max(geometry.sector_size, min(WIPE_MAX_BLOCK_BYTES, size & -size))
    count = WIPE_BYTES_PER_END // block
    return WipeOffsets(block, count, (size - WIPE_BYTES_PER_END) // block, count)


def raw_disk_path(disk: str, raw_partition: int) -> str:
    """The whole-disk raw device: /dev/r<disk><letter>, the letter from
    kern.rawpartition (2, "c", on both families)."""
    return f"/dev/r{disk}{chr(ord('a') + raw_partition)}"


RAW_PARTITION_COMMAND = "/sbin/sysctl -n kern.rawpartition"


def render_wipe_script(disk: str, raw_partition: int, geometry: DiskGeometry, offsets: WipeOffsets) -> str:
    """Zero both ends of the disk. It runs after the managed stop
    (services.wipe_disk), with the volumes still mounted and idle.

    Right before writing it reads drvctl again and stops unless the sector
    size and count are the ones the offsets came from: dd writes by device
    name, at offsets computed from the size read before the confirmation, and
    a disk that now reports another size (or none) would be written in the
    wrong place. Each dd's own report
    is printed: NetBSD 6's dd reads or writes past the end as 0 records with
    exit 0, so the caller checks the record counts, not just the status."""
    raw = shlex.quote(raw_disk_path(disk, raw_partition))
    extract = "sed -n '/<key>{key}<\\/key>/{{n;s/.*<integer>[[:space:]]*\\([^<[:space:]]*\\).*/\\1/p;}}'"
    lines = [
        f"geometry=$({drvctl_command(disk)} 2>/dev/null)",
        f"""sector_size=$(printf '%s\\n' "$geometry" | {extract.format(key="sector-size")})""",
        f"""sectors=$(printf '%s\\n' "$geometry" | {extract.format(key="sectors-per-unit")})""",
        f'if [ "$sector_size" != {shlex.quote(geometry.sector_size_text)} ] || '
        f'[ "$sectors" != {shlex.quote(geometry.sectors_text)} ]; then '
        f'echo "{WIPE_GEOMETRY_CHANGED_LINE} ($sector_size x $sectors)"; exit 1; fi',
    ]
    for end, skip in (("head", None), ("tail", offsets.tail_seek)):
        seek = f" seek={skip}" if skip is not None else ""
        count = offsets.head_count if end == "head" else offsets.tail_count
        lines += [
            f"dd_out=$(/bin/dd if=/dev/zero of={raw} bs={offsets.block_size}{seek} count={count} 2>&1); dd_rc=$?",
            f"""printf '%s\\n' "$dd_out" | sed 's/^/{WIPE_LINE_PREFIX}{end}: /'""",
            f'echo "{WIPE_LINE_PREFIX}{end} exit $dd_rc"',
            '[ "$dd_rc" -eq 0 ] || exit 1',
        ]
    lines += ["sync", f"echo '{WIPE_DONE_LINE}'"]
    return "\n".join(lines)


@dataclass(frozen=True)
class WipeReport:
    complete: bool
    reason: str | None = None


def check_wipe_output(output: str, offsets: WipeOffsets) -> WipeReport:
    """Whether the wipe script wrote both ends in full and synced."""
    if WIPE_GEOMETRY_CHANGED_LINE in output:
        return WipeReport(False, "disk_changed")
    for end, count in (("head", offsets.head_count), ("tail", offsets.tail_count)):
        if f"{WIPE_LINE_PREFIX}{end} exit 0" not in output:
            return WipeReport(False, "no_response" if f"{WIPE_LINE_PREFIX}{end} exit" not in output else "write_failed")
        if f"{WIPE_LINE_PREFIX}{end}: {count}+0 records out" not in output:
            return WipeReport(False, "short_write")
    if WIPE_DONE_LINE not in output:
        return WipeReport(False, "no_response")
    return WipeReport(True)


def readback_command(disk: str, raw_partition: int, geometry: DiskGeometry) -> str:
    """Sectors 0 and 1 (the MBR and the GPT header) and the last sector (the
    backup GPT header), raw, to check on the Mac (the device has no od, cmp or
    tr)."""
    raw = shlex.quote(raw_disk_path(disk, raw_partition))
    size = geometry.sector_size
    return (
        f"/bin/dd if={raw} bs={size} count=2 2>/dev/null; "
        f"/bin/dd if={raw} bs={size} skip={geometry.sectors - 1} count=1 2>/dev/null"
    )


def readback_is_blank(data: bytes, geometry: DiskGeometry) -> bool:
    """Three full sectors of zeros. An empty or short read fails: NetBSD 6's
    dd returns nothing, with exit 0, for a sector past the end."""
    return len(data) == 3 * geometry.sector_size and not any(data)


# Apple's erase asks the kernel for wedges under these names, ignores a
# refusal and formats dk0..dk2 instead; the kernel refuses a name another
# disk's wedge already has, and names a disk's wedges after its GPT labels when
# it attaches (services.wipe_disk.disks_with_apple_labels).
APPLE_PARTITION_NAMES = frozenset({"APconfig", "APswap", "APdata"})
# The partition table every GPT tool writes: 128 entries of 128 bytes.
GPT_TABLE_BYTES = 128 * 128
_GPT_SIGNATURE = b"EFI PART"


def gpt_tables_command(disk: str, raw_partition: int, geometry: DiskGeometry) -> str:
    """Both GPT headers with the 16 KiB of table beside each, raw: the
    primary header at LBA 1 and the table after it, then the backup table and
    the backup header at the last LBA. Read only."""
    raw = shlex.quote(raw_disk_path(disk, raw_partition))
    size = geometry.sector_size
    table = GPT_TABLE_BYTES // size
    return (
        f"/bin/dd if={raw} bs={size} skip=1 count={table + 1} 2>/dev/null; "
        f"/bin/dd if={raw} bs={size} skip={geometry.sectors - 1 - table} count={table + 1} 2>/dev/null"
    )


def gpt_partition_names(data: bytes, geometry: DiskGeometry) -> tuple[str, ...] | None:
    """The partition names in both GPT tables (gpt_tables_command's output),
    or None when they cannot be told: a short read, or a valid header whose
    table lies outside what was read. A disk with no GPT has no names."""
    size = geometry.sector_size
    table = GPT_TABLE_BYTES // size
    span = size * (table + 1)
    if len(data) != 2 * span:
        return None
    primary, backup = data[:span], data[span:]
    # (header, the table window, the window's first LBA)
    copies = (
        (primary[:size], primary[size:], 2),
        (backup[-size:], backup[:-size], geometry.sectors - 1 - table),
    )
    names: list[str] = []
    for header, window, window_lba in copies:
        if header[:8] != _GPT_SIGNATURE:
            continue
        entries_lba = int.from_bytes(header[72:80], "little")
        count = int.from_bytes(header[80:84], "little")
        entry_size = int.from_bytes(header[84:88], "little")
        start = (entries_lba - window_lba) * size
        if entry_size < 128 or start < 0 or start + count * entry_size > len(window):
            return None
        for index in range(count):
            entry = window[start + index * entry_size:start + (index + 1) * entry_size]
            if not any(entry[:16]):
                continue
            name = entry[56:128].decode("utf-16-le", errors="replace").split("\0", 1)[0]
            names.append(name)
    return tuple(names)


def disk_jobs_in(ps_output: str) -> tuple[str, ...]:
    """The erase and mount steps running under a diskd (DISK_JOB_COMMANDS
    whose parent or grandparent is a diskd), from PS_DISK_JOBS_COMMAND's
    rows. Only diskd's own count, so our `gpt show` never looks like an
    erase. Zombies do not count."""
    rows: dict[int, tuple[int, str, str]] = {}
    for line in ps_output.splitlines():
        fields = line.split()
        if len(fields) < 4 or not fields[0].isdigit() or not fields[1].isdigit():
            continue
        rows[int(fields[0])] = (int(fields[1]), fields[2], fields[3])
    diskds = {pid for pid, (_parent, _state, command) in rows.items() if command == "diskd"}
    jobs = []
    for pid, (parent, state, command) in sorted(rows.items()):
        if command not in DISK_JOB_COMMANDS or state.startswith("Z"):
            continue
        grandparent = rows.get(parent, (None,))[0]
        if parent in diskds or grandparent in diskds:
            jobs.append(command)
    return tuple(jobs)


def gpt_show_command(disk: str) -> str:
    """`gpt show`, killed after GPT_SHOW_KILL_SECONDS if it hangs."""
    quoted = shlex.quote(disk)
    return (
        f"/sbin/gpt show {quoted} 2>&1 & gpt_pid=$!; "
        # The killer's own output goes nowhere: its sleep would otherwise hold
        # the SSH session's output open for the whole minute.
        f"( sleep {GPT_SHOW_KILL_SECONDS}; kill $gpt_pid 2>/dev/null ) >/dev/null 2>&1 & killer=$!; "
        "wait $gpt_pid; gpt_rc=$?; kill $killer 2>/dev/null; exit $gpt_rc"
    )


# GPT partition types Apple's gpt prints by uuid (it has no name for them).
_GPT_TYPE_FORMATS = {
    "48465300-0000-11aa-aa11-00306543ecac": "apple_hfs",
    "7c3457ef-0000-11aa-aa11-00306543ecac": "apfs",
    "ebd0a0a2-b9e5-4433-87c0-68b6b72699c7": "basic_data",
    "0fc63daf-8483-4772-8e79-3d69d8477de4": "linux",
    "c12a7328-f81f-11d2-ba4b-00a0c93ec93b": "efi",
}
# ...and the names it prints for the types it knows.
_GPT_NAME_FORMATS = {
    "apple hfs": "apple_hfs",
    "linux/windows": "basic_data",
    "efi system": "efi",
    "linux swap": "linux",
}
_MBR_TYPE_FORMATS = {7: "basic_data", 11: "fat32", 12: "fat32", 14: "fat32", 131: "linux"}


@dataclass(frozen=True)
class GptVerdict:
    """What `gpt show` found on the disk itself (MaSt only lists what diskd
    mounted). kind: "apple_hfs" (an Apple HFS partition: when MaSt lists no
    HFS volume, the disk has not mounted yet, so deploy's error offers no
    wipe; a wipe itself takes such a disk), "bogus_map" (a table Apple's
    gpt cannot replace), "foreign" (another system's partitions), "blank"
    (no table) or "unreadable". formats: the partition formats found, for the
    message and telemetry."""

    kind: str
    formats: tuple[str, ...] = ()


def read_gpt_verdict_conn(connection: SshConnection, disk: str) -> GptVerdict:
    """What `gpt show` finds on the disk; "unreadable" when it could not run."""
    try:
        proc = run_ssh(connection, f"/bin/sh -c {shlex.quote(gpt_show_command(disk))}", check=False,
                       timeout=GPT_SHOW_KILL_SECONDS + 30)
    except TransportError:
        return GptVerdict("unreadable")
    return classify_gpt_show(proc.stdout or "")


def classify_gpt_show(output: str) -> GptVerdict:
    lowered = output.lower()
    if "bogus map" in lowered or "no room for the backup header" in lowered:
        return GptVerdict("bogus_map")
    formats: list[str] = []
    rows = 0
    for line in output.splitlines():
        # Apple's gpt prints this on every run, even for a good table.
        if "unable to open device" in line:
            continue
        gpt_part = re.search(r"GPT part - (.+?)\s*$", line)
        mbr_part = re.search(r"MBR part (\d+)", line)
        if gpt_part:
            label = gpt_part.group(1).strip().lower()
            formats.append(_GPT_TYPE_FORMATS.get(label) or _GPT_NAME_FORMATS.get(label) or "other")
        elif mbr_part:
            formats.append(_MBR_TYPE_FORMATS.get(int(mbr_part.group(1)), "other"))
        if re.match(r"^\s*\d+\s+\d+", line):
            rows += 1
    if "apple_hfs" in formats:
        return GptVerdict("apple_hfs", tuple(formats))
    if formats:
        return GptVerdict("foreign", tuple(formats))
    if rows or "doesn't contain a gpt" in lowered:
        return GptVerdict("blank")
    return GptVerdict("unreadable")


GPT_FORMAT_NAMES = {
    "apfs": "APFS",
    "basic_data": "exFAT or NTFS",
    "fat32": "FAT32",
    "linux": "Linux",
}


def gpt_verdict_description(verdict: GptVerdict) -> str | None:
    """The sentence a no-HFS error adds about what is on the disk, if any."""
    if verdict.kind == "bogus_map":
        return "AirPort Utility cannot replace this disk's partition table."
    if verdict.kind == "foreign":
        names = [GPT_FORMAT_NAMES[fmt] for fmt in dict.fromkeys(verdict.formats) if fmt in GPT_FORMAT_NAMES]
        if names:
            return f"This disk is formatted as {' and '.join(names)}."
    if verdict.kind == "blank":
        return "This disk has no partitions."
    return None


SIZE_MISREPORTED_MESSAGE = (
    "The device reports the internal disk as only {size}, much smaller than it is. A restart sometimes clears "
    "this; if it keeps happening, the disk is probably failing."
)


def decimal_size(mib: int) -> str:
    """A size in MiB the way disks are sold: 3950 MiB is "4 GB"."""
    gigabytes = mib * 1024 * 1024 / 1e9
    if gigabytes >= 1000:
        return f"{gigabytes / 1000:.1f} TB"
    return f"{gigabytes:.0f} GB" if gigabytes >= 1 else f"{gigabytes * 1000:.0f} MB"


def size_misreported_message(size_mib: int | None) -> str:
    return SIZE_MISREPORTED_MESSAGE.format(size=decimal_size(size_mib or 0))


def volume_name_allowed(name: str) -> bool:
    """A name diskd can pass to newfs_hfs: UTF-8 is fine; quotes, `$`, `\\`,
    backticks, `:`, control characters, an empty name, leading or trailing
    spaces and more than 255 characters are not."""
    return (
        bool(name)
        and name == name.strip()
        and len(name) <= VOLUME_NAME_MAX_CHARACTERS
        and not any(char in _VOLUME_NAME_FORBIDDEN for char in name)
        and not any(unicodedata.category(char).startswith("C") for char in name)
    )


def erase_volume_name(old_names: tuple[str, ...]) -> str:
    """The first old volume name that passes, else "Data". Keeping the name
    keeps the share name. HFS returns names decomposed (NFD); send them
    composed."""
    for name in old_names:
        composed = unicodedata.normalize("NFC", name)
        if volume_name_allowed(composed):
            return composed
    return DEFAULT_VOLUME_NAME


def erase_disk_arguments(disk_uuid: str, disk: str, volume_name: str, method: int) -> tuple[str, ...]:
    """diskd.eraseDisk's acp words. The disk is addressed by its uuid; the
    name only fills deviceName, which diskd uses for an all-zero uuid."""
    return (
        f"uuid:h:{disk_uuid.replace('-', '')}",
        f"deviceName:s:{disk}",
        f"volumeName:s:{volume_name}",
        f"method:i:{method}",
    )


def payload_home_probe_command(roots: tuple[str, ...], payload_dir_name: str) -> str:
    """For each volume root: "mounted yes"/"mounted no" when the mount table
    lists it (whether the payload directory is there), else "unmounted".
    Nothing is mounted or claimed: a volume diskd has not mounted stays as it
    is."""
    lines = ["mounts=$(/sbin/mount) || exit 1"]
    for root in roots:
        quoted_root = shlex.quote(root)
        payload = shlex.quote(f"{root}/{payload_dir_name}")
        lines.append(
            f'case "$mounts" in *" on "{quoted_root}" type "*) '
            f"if [ -d {payload} ]; then echo {quoted_root}' yes'; else echo {quoted_root}' no'; fi ;; "
            f"*) echo {quoted_root}' unmounted' ;; esac"
        )
    return "\n".join(lines)


def parse_payload_home_probe(output: str) -> dict[str, str]:
    """root -> "yes", "no" or "unmounted"."""
    found: dict[str, str] = {}
    for line in output.splitlines():
        root, _, state = line.rpartition(" ")
        if root and state in {"yes", "no", "unmounted"}:
            found[root] = state
    return found


# diskd is the manager's child, so the gpt and newfs_hfs output of every erase
# lands in the manager's log, which is in RAM and starts empty at each boot.
# Reading it touches no disk: a failing disk cannot slow it, nor is a sleeping
# one woken.
RUNTIME_LOG_PATH = "/mnt/Memory/samba4/var/runtime.log"
ERASE_LOG_TIMEOUT_SECONDS = 30
# Apple's gpt prints its errors as "gpt: error: bogus map" or, naming the
# raw disk, "gpt: rsd0c: error: no room for the backup header".
_GPT_ERROR = re.compile(r"gpt: (?:r(?P<disk>(?:sd|wd)\d+)[a-p]: )?error: (?P<error>.*?)\s*$")
# The errors that leave the old table in place: a table gpt rejects (bogus
# map), or a legacy MBR partition reaching the last sector, where `gpt
# create` cannot put the backup header; every later add then finds no GPT.
_GPT_REJECTIONS = ("bogus map", "no room for the backup header", "no primary GPT header")
# Apple's gpt prints this after a run it could not open the table for; it
# names the disk a bogus map belongs to.
_GPT_FAILED_DISK = re.compile(r"gpt: unable to open device 'r((?:sd|wd)\d+)[a-p]'")
_NEWFS_DONE = re.compile(r"Initialized /dev/rdk(\d+) as a .*HFS Plus volume")


@dataclass(frozen=True)
class WedgeFormat:
    """One newfs_hfs in diskd's output: the wedge, and the disk whose table
    gpt rejected just before it (None when gpt rejected none)."""

    wedge: str
    rejected_disk: str | None


def wedge_formats(log: str) -> tuple[WedgeFormat, ...]:
    """The wedges diskd formatted, in order. An erase runs gpt before each
    newfs_hfs (destroy and create, then add and label per partition). A run
    that succeeds prints nothing; one that cannot replace the table prints a
    rejection naming the disk (or followed by a line naming it), writes no
    table, and exits 0, and diskd formats its in-memory wedge anyway. So each
    format takes its verdict from the gpt lines since the format before it. A
    gpt line naming another disk starts over, so another disk's rejection is
    not carried into this one's erase. A rejection with no disk named gives
    no verdict."""
    formats: list[WedgeFormat] = []
    rejected = False
    disk: str | None = None
    for line in log.splitlines():
        if match := _GPT_ERROR.match(line):
            named, rejects = match["disk"], match["error"].startswith(_GPT_REJECTIONS)
        elif match := _GPT_FAILED_DISK.match(line):
            named, rejects = match.group(1), False
        elif match := _NEWFS_DONE.match(line):
            formats.append(WedgeFormat(f"dk{match.group(1)}", disk if rejected else None))
            rejected, disk = False, None
            continue
        else:
            continue
        if named is not None:
            if disk is not None and named != disk:
                rejected = False
            disk = named
        rejected = rejected or rejects
    return tuple(formats)


def in_memory_volumes(
    disks: tuple[MaStDiskSnapshot, ...], formats: tuple[WedgeFormat, ...],
) -> tuple[tuple[MaStDiskSnapshot, MaStPartitionSnapshot], ...]:
    """(disk, partition) for each MaSt volume whose wedge was last formatted
    after gpt rejected that same disk's table. The log is this boot's history,
    so the disk may have changed since (a USB disk plugged back in, another
    disk taking the same names): read_in_memory_volumes_conn checks each
    against the disk's own table. A volume the log does not show is not
    flagged, so a trimmed log or a disk erased before our runtime ran misses
    it."""
    latest = {fmt.wedge: fmt.rejected_disk for fmt in formats}
    return tuple(
        (disk, partition)
        for disk in disks
        for partition in disk.partitions
        if latest.get(partition.device) == disk.device
    )


def in_memory_volume_message(disk: MaStDiskSnapshot, partition: MaStPartitionSnapshot) -> str:
    """What doctor and every command that restarts the device say about one
    volume that exists only in memory. A volume diskd has not mounted yet
    may have no name: its wedge then stands in."""
    return (
        f"The volume “{partition.name or partition.device}” on the {'internal' if disk.builtin else 'USB'} disk exists only until the "
        "next restart: AirPort Utility's “Erase Disk” could not replace the disk's partition table. Anything saved "
        "on it since that erase, Time Machine backups included, is lost at the next restart."
    )


@dataclass(frozen=True)
class EraseLog:
    """What the manager's log shows of diskd's erases; error when it could
    not be read (then nothing is known)."""

    formats: tuple[WedgeFormat, ...] = ()
    log_bytes: int = 0
    error: str | None = None

    def debug(self) -> dict[str, object]:
        return {
            "error": self.error,
            "log_bytes": self.log_bytes,
            "formats": [[fmt.wedge, fmt.rejected_disk] for fmt in self.formats],
        }


def read_erase_log_conn(connection: SshConnection) -> EraseLog:
    """diskd's erases from the manager's log; no log (no runtime yet) reads as
    none."""
    try:
        proc = run_ssh(connection, f"/bin/cat {RUNTIME_LOG_PATH} 2>/dev/null; true", check=False,
                       timeout=ERASE_LOG_TIMEOUT_SECONDS)
    except TransportError as e:
        return EraseLog(error=str(e) or type(e).__name__)
    log = proc.stdout or ""
    if proc.returncode != 0:
        # The read itself failed (`; true` covers a missing log): the SSH
        # session, whose message is the output.
        last = log.strip().splitlines()[-1:] or ["no output"]
        return EraseLog(error=f"exit {proc.returncode}: {last[0]}")
    return EraseLog(wedge_formats(log), len(log.encode("utf-8", "replace")))


# What `gpt show` (classify_gpt_show) finds on a disk whose volumes an erase
# left only in memory: the table gpt rejected (bogus_map), or only the
# protective MBR `gpt create` writes before it finds no room for the backup
# header (blank); unreadable keeps the log's verdict. A table gpt reads
# (apple_hfs, foreign) means the disk changed since the erase the log shows:
# destroy and create succeed on any such table, so a rejected erase never
# leaves one.
IN_MEMORY_TABLES = frozenset({"bogus_map", "blank", "unreadable"})


@dataclass(frozen=True)
class InMemoryCheck:
    """The volumes that exist only until the next restart, the log they were
    found in, and what `gpt show` found on each disk the log flagged."""

    erase_log: EraseLog
    flagged: tuple[tuple[MaStDiskSnapshot, MaStPartitionSnapshot], ...] = ()
    tables: dict[str, str] = field(default_factory=dict)

    def debug(self) -> dict[str, object]:
        return {
            **self.erase_log.debug(),
            "tables": self.tables,
            "in_memory": [partition.device for _, partition in self.flagged],
        }


def read_in_memory_volumes_conn(connection: SshConnection, disks: tuple[MaStDiskSnapshot, ...]) -> InMemoryCheck:
    """The log's candidates (in_memory_volumes), each confirmed by its disk's
    table. Only a disk the log flagged is read, so a normal run touches no
    disk and wakes no sleeping one."""
    erase_log = read_erase_log_conn(connection)
    candidates = in_memory_volumes(disks, erase_log.formats)
    tables = {
        device: read_gpt_verdict_conn(connection, device).kind
        for device in dict.fromkeys(disk.device for disk, _ in candidates)
    }
    flagged = tuple(pair for pair in candidates if tables[pair[0].device] in IN_MEMORY_TABLES)
    return InMemoryCheck(erase_log, flagged, tables)
