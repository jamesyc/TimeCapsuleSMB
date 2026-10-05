from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
import plistlib
import re
import shlex
import time
import uuid

from timecapsulesmb.device.errors import DeviceError
from timecapsulesmb.integrations.acp import DEVICE_ACP_PATH
from timecapsulesmb.transport.ssh import SshCommandTimeout, SshConnection, run_ssh


MAST_DISCOVERY_ATTEMPTS = 10
MAST_DISCOVERY_DELAY_SECONDS = 3
MAST_ACP_COMMAND = f"{DEVICE_ACP_PATH} MaSt"
MAST_PROBE_COMMAND = f"{DEVICE_ACP_PATH} -A MaSt"
MAST_PROBE_TIMEOUT_SECONDS = 30
MAST_PROBE_OUTPUT_DEBUG_LIMIT = 8192
DISKD_USE_VOLUME_GUARD_ATTEMPTS = 2
DRY_RUN_VOLUME_ROOT_PLACEHOLDER = "resolved from MaSt at deploy time"
DRY_RUN_DEVICE_PATH_PLACEHOLDER = "resolved from MaSt at deploy time"
UNINSTALL_DRY_RUN_VOLUME_ROOT_PLACEHOLDER = "resolved from MaSt at uninstall time"
DISK_WRITE_TEST_UNRESPONSIVE_MESSAGE = (
    "The disk did not respond when tested. It may be failing or unable to spin up. "
    "Run Disk Repair; if this keeps happening, the disk may need replacing."
)


class StorageDeviceError(DeviceError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class MaStVolume:
    disk_device: str
    partition_device: str
    volume_root: str
    name: str
    adisk_uuid: str
    builtin: bool
    format: str

    @property
    def device_path(self) -> str:
        return f"/dev/{self.partition_device}"


@dataclass(frozen=True)
class MaStPartitionSnapshot:
    device: str
    name: str
    format: str


@dataclass(frozen=True)
class MaStDiskSnapshot:
    device: str
    name: str
    size: object | None
    builtin: bool
    partitions: tuple[MaStPartitionSnapshot, ...]


@dataclass(frozen=True)
class PayloadHome:
    volume_root: str
    device_path: str
    payload_dir_name: str

    @property
    def payload_dir(self) -> str:
        return f"{self.volume_root}/{self.payload_dir_name}"

    @property
    def private_dir(self) -> str:
        return f"{self.payload_dir}/private"

    @property
    def disk_key(self) -> str:
        return PurePosixPath(self.volume_root).name


@dataclass(frozen=True)
class MaStDiscoveryResult:
    volumes: tuple[MaStVolume, ...]
    attempts: int
    raw_output: str = ""


@dataclass(frozen=True)
class MaStReadResult:
    volumes: tuple[MaStVolume, ...]
    raw_output: str


@dataclass(frozen=True)
class MaStProbeDiagnostics:
    command: str
    returncode: int | None
    volumes: tuple[MaStVolume, ...]
    stdout: str
    stderr: str
    error: str | None = None


@dataclass(frozen=True)
class PayloadCandidateCheck:
    volume: MaStVolume
    mount: VolumeMountResult
    writable: bool | None

    @property
    def mounted(self) -> bool:
        return bool(self.mount)


@dataclass(frozen=True)
class PayloadHomeSelection:
    payload_home: PayloadHome | None
    checks: tuple[PayloadCandidateCheck, ...]


@dataclass(frozen=True)
class PayloadVerificationResult:
    ok: bool
    detail: str


def build_dry_run_payload_home(payload_dir_name: str) -> PayloadHome:
    return PayloadHome(
        volume_root=DRY_RUN_VOLUME_ROOT_PLACEHOLDER,
        device_path=DRY_RUN_DEVICE_PATH_PLACEHOLDER,
        payload_dir_name=payload_dir_name,
    )


def _uuid_from_value(value: object) -> str:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, bytes):
        if len(value) == 16:
            return str(uuid.UUID(bytes=value))
        text = value.hex()
    else:
        text = str(value or "").strip()
    text = text.split("|", 1)[0].strip()
    leading_hex = re.match(r"^<?\s*([0-9A-Fa-f][0-9A-Fa-f\s-]*)", text)
    if leading_hex:
        text = leading_hex.group(1)
    text = text.replace("<", "").replace(">", "").replace(" ", "").replace("-", "")
    if len(text) != 32:
        return ""
    try:
        return str(uuid.UUID(hex=text))
    except ValueError:
        return ""


def _plist_root_items(root: object) -> list[dict[str, object]]:
    if isinstance(root, list):
        return [item for item in root if isinstance(item, dict)]
    if isinstance(root, dict):
        if isinstance(root.get("MaSt"), list):
            return [item for item in root["MaSt"] if isinstance(item, dict)]
        if isinstance(root.get("disks"), list):
            return [item for item in root["disks"] if isinstance(item, dict)]
        return [root]
    return []


def _strip_mast_assignment_prefix(text: str) -> str:
    return re.sub(r"^\s*MaSt\s*=\s*", "", text.strip(), count=1)


def _openstep_assignment_value(line: str, key: str) -> str | None:
    match = re.match(rf"^{re.escape(key)}\s*=\s*(.+?)\s*;?\s*,?$", line)
    if not match:
        return None
    value = match.group(1).strip()
    value = value.rstrip(",").rstrip(";").strip()
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1].replace(r"\"", '"').replace(r"\\", "\\")
    return value


def _openstep_bool_assignment(line: str, key: str) -> bool | None:
    value = _openstep_assignment_value(line, key)
    if value is None:
        return None
    lowered = value.lower()
    if lowered in {"true", "yes", "1"}:
        return True
    if lowered in {"false", "no", "0"}:
        return False
    return None


def _openstep_object_open(line: str) -> bool:
    return line == "{"


def _openstep_object_close(line: str) -> bool:
    return re.fullmatch(r"\}\s*[;,]?", line) is not None


def _openstep_collection_close(line: str) -> bool:
    return re.fullmatch(r"[\)\]]\s*[;,]?", line) is not None


def _volumes_from_plist_root(root: object) -> tuple[MaStVolume, ...]:
    volumes: list[MaStVolume] = []
    for disk in _plist_root_items(root):
        disk_device = str(disk.get("deviceName") or "")
        builtin = bool(disk.get("builtin"))
        partitions = disk.get("partitions")
        if not isinstance(partitions, list):
            continue
        for partition in partitions:
            if not isinstance(partition, dict):
                continue
            partition_device = str(partition.get("deviceName") or "")
            fmt = str(partition.get("format") or "")
            name = str(partition.get("name") or partition_device or "")
            adisk_uuid = _uuid_from_value(partition.get("uuid"))
            if not partition_device.startswith("dk"):
                continue
            if fmt.lower() != "hfs":
                continue
            if not name or not adisk_uuid:
                continue
            volumes.append(
                MaStVolume(
                    disk_device=disk_device,
                    partition_device=partition_device,
                    volume_root=f"/Volumes/{partition_device}",
                    name=name,
                    adisk_uuid=adisk_uuid,
                    builtin=builtin,
                    format=fmt.lower(),
                )
            )
    return tuple(volumes)


def _partition_snapshot_from_mapping(partition: dict[str, object]) -> MaStPartitionSnapshot:
    return MaStPartitionSnapshot(
        device=str(partition.get("deviceName") or ""),
        name=str(partition.get("name") or ""),
        format=str(partition.get("format") or "").lower(),
    )


def _disk_snapshot_from_mapping(disk: dict[str, object]) -> MaStDiskSnapshot:
    partitions = disk.get("partitions")
    return MaStDiskSnapshot(
        device=str(disk.get("deviceName") or ""),
        name=str(disk.get("name") or disk.get("model") or ""),
        size=disk.get("size") or disk.get("capacity") or disk.get("totalSize"),
        builtin=bool(disk.get("builtin")),
        partitions=tuple(
            _partition_snapshot_from_mapping(partition)
            for partition in partitions
            if isinstance(partition, dict)
        )
        if isinstance(partitions, list)
        else (),
    )


def _disk_snapshots_from_plist_root(root: object) -> tuple[MaStDiskSnapshot, ...]:
    return tuple(_disk_snapshot_from_mapping(disk) for disk in _plist_root_items(root))


def _openstep_disks(content: str) -> list[dict[str, object]]:
    """Decode acp's text MaSt, one key per line, into the disk dictionaries
    the XML plist gives, so both forms go through the same converters.
    Objects are tracked by their braces: a disk or partition is whatever its
    braces enclose, whichever keys it has.

    Plain `acp MaSt` prints XML. `acp -A MaSt` prints Apple's own text form
    (PrintFUtils), an array opened with "[": every "{", "}", "[" and "]"
    alone on a line, entries as `key=value`, a data value as its hex, " |",
    the bytes as text (0x20-0x7e as themselves, anything else as "^") and
    "| (N bytes)", and a string value as its raw UTF-8 between quotes with
    nothing escaped, so a name may hold quotes, backslashes or line breaks.
    The OpenStep form (`key = "value";`, opened with "(") escapes quotes and
    backslashes."""
    text = _strip_mast_assignment_prefix(content)
    native = text.lstrip().startswith("[")
    lines = text.split("\n")
    disks: list[dict[str, object]] = []
    disk: dict[str, object] | None = None
    partitions: list[dict[str, object]] = []
    partition: dict[str, object] | None = None
    in_partitions = False
    index = 0
    while index < len(lines):
        raw_line = lines[index]
        index += 1
        line = raw_line.strip()
        if _openstep_object_open(line):
            if not in_partitions:
                disk = {}
                disks.append(disk)
            else:
                partition = {}
                partitions.append(partition)
            continue
        if re.match(r"^partitions\s*=", line):
            # The list opens on this line or the next; "( )" closes it here.
            in_partitions = not re.search(r"[\)\]]\s*[;,]?$", line)
            partitions = []
            if disk is not None:
                disk["partitions"] = partitions
            continue
        if _openstep_collection_close(line):
            in_partitions = False
            partition = None
            continue
        if _openstep_object_close(line):
            if in_partitions:
                partition = None
            else:
                disk = None
            continue
        key = re.match(r"^\s*([A-Za-z_]\w*)\s*=(.*)$", raw_line.removesuffix("\r"))
        target = partition if in_partitions else disk
        if key is None or target is None:
            continue
        name, rest = key.group(1), key.group(2)
        if native and rest.startswith('"'):
            # The value ends at the first line that ends with a quote, this
            # one or a later one when the name holds a line break.
            value = rest[1:]
            while not value.rstrip(" \t").endswith('"') and index < len(lines):
                value += "\n" + lines[index].removesuffix("\r")
                index += 1
            if value.rstrip(" \t").endswith('"'):
                target[name] = value.rstrip(" \t")[:-1]
            continue
        if name == "builtin":
            builtin = _openstep_bool_assignment(line, "builtin")
            if builtin is not None:
                target["builtin"] = builtin
            continue
        value = _openstep_assignment_value(line, name)
        if value is not None:
            target[name] = value
    return disks


def _mast_root(content: str | bytes) -> object:
    """MaSt as its XML plist root, or acp's text form decoded to the same
    list of disk dictionaries."""
    text: str | None = None
    if isinstance(content, bytes):
        data = content
    else:
        text = content.strip()
        xml_start = text.find("<?xml")
        if xml_start >= 0:
            text = text[xml_start:]
        else:
            text = _strip_mast_assignment_prefix(text)
        data = text.encode("utf-8", errors="replace")
    try:
        return plistlib.loads(data)
    except plistlib.InvalidFileException:
        if text is None:
            text = content.decode("utf-8", errors="replace")
        return _openstep_disks(text)


def parse_mast_plist(content: str | bytes) -> tuple[MaStVolume, ...]:
    return _volumes_from_plist_root(_mast_root(content))


def parse_mast_inventory(content: str | bytes) -> tuple[MaStDiskSnapshot, ...]:
    return _disk_snapshots_from_plist_root(_mast_root(content))


def read_mast_volumes_with_output_conn(connection: SshConnection) -> MaStReadResult:
    proc = run_ssh(connection, MAST_ACP_COMMAND, timeout=60)
    return MaStReadResult(parse_mast_plist(proc.stdout), proc.stdout)


def read_mast_volumes_conn(connection: SshConnection) -> tuple[MaStVolume, ...]:
    return read_mast_volumes_with_output_conn(connection).volumes


def _mast_probe_output_debug_text(raw_output: str) -> str:
    if not raw_output:
        return "<empty>"
    if len(raw_output) <= MAST_PROBE_OUTPUT_DEBUG_LIMIT:
        return raw_output
    omitted = len(raw_output) - MAST_PROBE_OUTPUT_DEBUG_LIMIT
    return f"{raw_output[:MAST_PROBE_OUTPUT_DEBUG_LIMIT]}...<truncated {omitted} chars>"


def probe_mast_diagnostics_conn(connection: SshConnection) -> MaStProbeDiagnostics:
    proc = run_ssh(
        connection,
        MAST_PROBE_COMMAND,
        check=False,
        timeout=MAST_PROBE_TIMEOUT_SECONDS,
    )
    stdout = proc.stdout or ""
    stderr = getattr(proc, "stderr", "") or ""
    volumes: tuple[MaStVolume, ...] = ()
    error = None
    try:
        volumes = parse_mast_plist(stdout)
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
    return MaStProbeDiagnostics(
        command=MAST_PROBE_COMMAND,
        returncode=getattr(proc, "returncode", None),
        volumes=volumes,
        stdout=stdout,
        stderr=stderr,
        error=error,
    )


def mast_probe_debug_summary(diagnostics: MaStProbeDiagnostics) -> dict[str, object]:
    summary: dict[str, object] = {
        "mast_probe_command": diagnostics.command,
        "mast_probe_returncode": diagnostics.returncode,
        "mast_probe_volume_count": len(diagnostics.volumes),
        "mast_probe_candidates": mast_volumes_debug_summary(diagnostics.volumes),
        "mast_probe_stdout_chars": len(diagnostics.stdout),
        "mast_probe_stdout": _mast_probe_output_debug_text(diagnostics.stdout),
        "mast_probe_stderr_chars": len(diagnostics.stderr),
        "mast_probe_stderr": _mast_probe_output_debug_text(diagnostics.stderr),
    }
    if diagnostics.error:
        summary["mast_probe_error"] = diagnostics.error
    return summary


def wait_for_mast_volumes_conn(
    connection: SshConnection,
    *,
    attempts: int = MAST_DISCOVERY_ATTEMPTS,
    delay_seconds: int = MAST_DISCOVERY_DELAY_SECONDS,
) -> MaStDiscoveryResult:
    if attempts <= 0:
        attempts = 1
    volumes: tuple[MaStVolume, ...] = ()
    raw_output = ""
    for attempt in range(1, attempts + 1):
        read_result = read_mast_volumes_with_output_conn(connection)
        volumes = read_result.volumes
        raw_output = read_result.raw_output
        if volumes:
            return MaStDiscoveryResult(volumes, attempt, raw_output)
        try:
            disks = parse_mast_inventory(raw_output)
        except Exception:
            disks = ()
        if disks:
            return MaStDiscoveryResult(volumes, attempt, raw_output)
        if attempt < attempts:
            time.sleep(delay_seconds)
    return MaStDiscoveryResult(volumes, attempts, raw_output)


def _remote_mounted_test(volume_root: str) -> str:
    quoted_root = shlex.quote(volume_root)
    return (
        f"df_line=$(/bin/df -k {quoted_root} 2>/dev/null | /usr/bin/tail -n +2 || true); "
        f'case "$df_line" in *" {volume_root}") exit 0 ;; esac; exit 1'
    )


# acp prints `### RPC function "NAME" failed: CODE` on stderr; keep CODE.
ACP_RPC_ERROR_CODE_SED = r"""sed -n 's/.*failed: \(-*[0-9][0-9]*\).*/\1/p' | sed -n '$p'"""


def diskd_rpc_status_conn(connection: SshConnection) -> str:
    """"answered" when ACPd routes diskd.getVolumeCounts, else acp's error code.

    getVolumeCounts is the second diskd RPC name a colliding diskd deletes, so
    it is gone before diskd.useVolume (see boot.sh's diskd guard)."""
    script = (
        f"err=$({DEVICE_ACP_PATH} rpc diskd.getVolumeCounts 2>&1 >/dev/null) && echo answered && exit 0; "
        f"""printf '%s\\n' "$err" | {ACP_RPC_ERROR_CODE_SED}"""
    )
    proc = run_ssh(connection, f"/bin/sh -c {shlex.quote(script)}", check=False, timeout=30)
    return proc.stdout.strip() or "?"


def render_ensure_volume_root_mounted_script(volume_root: str, _device_path: str, wait_seconds: int) -> str:
    root = shlex.quote(volume_root)
    mounted_test = shlex.quote(_remote_mounted_test(volume_root))
    attempts = DISKD_USE_VOLUME_GUARD_ATTEMPTS
    # The last line reports each diskd.useVolume exit status, whether the
    # volume was mounted in the end, and the error acp printed for each call
    # (0 on success, ? when it printed no code; see VolumeMountResult).
    # acp exits 22 for every failed RPC, so only stderr carries the cause.
    # -6727 means ACPd lost diskd's RPC names (a second diskd deleted them) or
    # diskd does not know the volume. Either way diskd will not unmount it, so
    # a volume that is mounted is kept; the next reboot restores the names.
    report = '"use_volume_rcs=$use_volume_rcs mounted=%s use_volume_errors=$use_volume_errors"'
    return (
        f"mkdir -p {root}; "
        "use_volume_rcs=; use_volume_errors=; "
        "diskd_attempt=1; "
        f"while [ \"$diskd_attempt\" -le {attempts} ]; do "
        f"use_volume_err=$({DEVICE_ACP_PATH} rpc diskd.useVolume path:s:{root} 2>&1 >/dev/null); use_volume_rc=$?; "
        f"""use_volume_code=$(printf '%s\\n' "$use_volume_err" | {ACP_RPC_ERROR_CODE_SED}); """
        'if [ "$use_volume_rc" -eq 0 ]; then use_volume_code=0; elif [ -z "$use_volume_code" ]; then use_volume_code="?"; fi; '
        'use_volume_rcs="$use_volume_rcs${use_volume_rcs:+,}$use_volume_rc"; '
        'use_volume_errors="$use_volume_errors${use_volume_errors:+,}$use_volume_code"; '
        'if [ "$use_volume_rc" -eq 0 ]; then '
        "wait_attempt=0; "
        f'while [ "$wait_attempt" -le {wait_seconds} ]; do '
        f'if /bin/sh -c {mounted_test}; then echo {report % "yes"}; exit 0; fi; '
        f'if [ "$wait_attempt" -eq {wait_seconds} ]; then break; fi; '
        'wait_attempt=$((wait_attempt + 1)); sleep 1; '
        "done; "
        f'elif [ "$use_volume_code" = -6727 ] && /bin/sh -c {mounted_test}; then echo {report % "yes"}; exit 0; '
        "fi; "
        f'if [ "$diskd_attempt" -lt {attempts} ]; then sleep 1; fi; '
        'diskd_attempt=$((diskd_attempt + 1)); '
        "done; "
        f"if /bin/sh -c {mounted_test}; then mounted=yes; else mounted=no; fi; "
        f"echo {report % '$mounted'}; "
        "exit 1"
    )


@dataclass(frozen=True)
class VolumeMountResult:
    """Whether the volume is mounted and diskd will keep it; truthy on success.

    `detail` is the script's last line: each diskd.useVolume exit status,
    whether the volume was mounted in the end, and acp's error per call, e.g.
    "use_volume_rcs=22,22 mounted=no use_volume_errors=-6727,-6727". Success
    is a claimed volume, or a mounted one whose claim failed with -6727 (diskd
    cannot unmount it). Any other failed request can still end with the volume
    mounted (`present`): diskd refused it, or mounted it after the wait.
    Deploy does not use such a volume, since diskd may unmount it under us."""

    mounted: bool
    detail: str = ""

    def __bool__(self) -> bool:
        return self.mounted

    @property
    def present(self) -> bool:
        return self.mounted or "mounted=yes" in self.detail.split()


def ensure_volume_root_mounted_conn(
    connection: SshConnection,
    volume_root: str,
    device_path: str,
    *,
    wait_seconds: int,
) -> VolumeMountResult:
    script = render_ensure_volume_root_mounted_script(volume_root, device_path, wait_seconds)
    timeout = max(30, wait_seconds * DISKD_USE_VOLUME_GUARD_ATTEMPTS + 45)
    proc = run_ssh(connection, f"/bin/sh -c {shlex.quote(script)}", check=False, timeout=timeout)
    lines = (proc.stdout or "").strip().splitlines()
    return VolumeMountResult(proc.returncode == 0, lines[-1].strip() if lines else "")


def verify_payload_home_conn(
    connection: SshConnection,
    payload_home: PayloadHome,
    *,
    wait_seconds: int,
) -> PayloadVerificationResult:
    if not ensure_volume_root_mounted_conn(
        connection,
        payload_home.volume_root,
        payload_home.device_path,
        wait_seconds=wait_seconds,
    ):
        return PayloadVerificationResult(False, f"volume {payload_home.volume_root} is not mounted")

    payload_dir = shlex.quote(payload_home.payload_dir)
    script = (
        "missing=; "
        "add_missing() { if [ -z \"$missing\" ]; then missing=\"$1\"; else missing=\"$missing; $1\"; fi; }; "
        f"[ -d {payload_dir} ] || add_missing 'missing payload directory'; "
        f"[ -x {payload_dir}/smbd ] || [ -x {payload_dir}/sbin/smbd ] || add_missing 'missing smbd'; "
        f"[ -x {payload_dir}/rsync ] || add_missing 'missing rsync'; "
        f"[ -r {payload_dir}/rsyncd.conf ] || add_missing 'missing rsyncd.conf'; "
        f"[ -d {payload_dir}/private ] || add_missing 'missing private directory'; "
        "if [ -z \"$missing\" ]; then echo ok; exit 0; fi; "
        "echo \"$missing\"; exit 1"
    )
    proc = run_ssh(connection, f"/bin/sh -c {shlex.quote(script)}", check=False, timeout=30)
    detail = proc.stdout.strip() or "payload verification command failed"
    return PayloadVerificationResult(proc.returncode == 0, "ok" if proc.returncode == 0 else detail)


def mounted_mast_volumes_conn(
    connection: SshConnection,
    volumes: tuple[MaStVolume, ...],
    *,
    wait_seconds: int,
) -> tuple[MaStVolume, ...]:
    mounted: list[MaStVolume] = []
    for volume in volumes:
        if ensure_volume_root_mounted_conn(
            connection,
            volume.volume_root,
            volume.device_path,
            wait_seconds=wait_seconds,
        ):
            mounted.append(volume)
    return tuple(mounted)


def volume_root_is_writable_conn(connection: SshConnection, volume_root: str) -> bool:
    quoted_root = shlex.quote(volume_root)
    script = (
        f"test_dir={quoted_root}/.tcapsulesmb-write-test.$$; "
        'if mkdir "$test_dir" >/dev/null 2>&1; then '
        'rmdir "$test_dir" >/dev/null 2>&1 || true; '
        "exit 0; "
        "fi; "
        "exit 1"
    )
    try:
        proc = run_ssh(connection, f"/bin/sh -c {shlex.quote(script)}", check=False, timeout=30)
    except SshCommandTimeout as exc:
        raise StorageDeviceError(
            DISK_WRITE_TEST_UNRESPONSIVE_MESSAGE,
            code="disk_write_test_unresponsive",
        ) from exc
    return proc.returncode == 0


def ordered_payload_candidate_volumes(
    volumes: tuple[MaStVolume, ...],
) -> tuple[MaStVolume, ...]:
    return tuple(volume for volume in volumes if volume.builtin) + tuple(volume for volume in volumes if not volume.builtin)


def mast_volume_debug_summary(volume: MaStVolume) -> dict[str, object]:
    return {
        "disk": volume.disk_device,
        "part": volume.partition_device,
        "root": volume.volume_root,
        "name": volume.name,
        "format": volume.format,
        "builtin": volume.builtin,
        "uuid": volume.adisk_uuid,
    }


def mast_volumes_debug_summary(volumes: Sequence[MaStVolume]) -> list[dict[str, object]]:
    return [mast_volume_debug_summary(volume) for volume in volumes]


def payload_candidate_checks_debug_summary(checks: Sequence[PayloadCandidateCheck]) -> list[dict[str, object]]:
    summaries: list[dict[str, object]] = []
    for check in checks:
        summary = mast_volume_debug_summary(check.volume)
        summary["mounted"] = check.mounted
        summary["writable"] = check.writable
        if check.mount.detail:
            summary["mount"] = check.mount.detail
        summaries.append(summary)
    return summaries


def select_payload_home_with_diagnostics_conn(
    connection: SshConnection,
    volumes: tuple[MaStVolume, ...],
    payload_dir_name: str,
    *,
    wait_seconds: int,
) -> PayloadHomeSelection:
    checks: list[PayloadCandidateCheck] = []
    for volume in ordered_payload_candidate_volumes(volumes):
        mount = ensure_volume_root_mounted_conn(
            connection,
            volume.volume_root,
            volume.device_path,
            wait_seconds=wait_seconds,
        )
        mounted = bool(mount)
        writable = volume_root_is_writable_conn(connection, volume.volume_root) if mounted else None
        checks.append(PayloadCandidateCheck(volume, mount, writable))
        if mounted and writable:
            return PayloadHomeSelection(
                PayloadHome(
                    volume_root=volume.volume_root,
                    device_path=volume.device_path,
                    payload_dir_name=payload_dir_name,
                ),
                tuple(checks),
            )
    return PayloadHomeSelection(None, tuple(checks))
