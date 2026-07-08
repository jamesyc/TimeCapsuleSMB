from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
import plistlib
import re
import shlex
import time
import uuid

from timecapsulesmb.transport.ssh import SshConnection, run_ssh


MAST_DISCOVERY_ATTEMPTS = 10
MAST_DISCOVERY_DELAY_SECONDS = 3
MAST_ACP_COMMAND = "/usr/bin/acp MaSt"
MAST_PROBE_COMMAND = "/usr/bin/acp -A MaSt"
MAST_PROBE_TIMEOUT_SECONDS = 30
MAST_PROBE_OUTPUT_DEBUG_LIMIT = 8192
DISKD_USE_VOLUME_GUARD_ATTEMPTS = 2
DRY_RUN_VOLUME_ROOT_PLACEHOLDER = "resolved from MaSt at deploy time"
DRY_RUN_DEVICE_PATH_PLACEHOLDER = "resolved from MaSt at deploy time"
UNINSTALL_DRY_RUN_VOLUME_ROOT_PLACEHOLDER = "resolved from MaSt at uninstall time"


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
    mounted: bool
    writable: bool | None


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


def _parse_mast_openstep(content: str) -> tuple[MaStVolume, ...]:
    text = _strip_mast_assignment_prefix(content)
    volumes: list[MaStVolume] = []
    pending_partitions: list[tuple[str, str, str, str]] = []
    disk_device = ""
    disk_builtin = False
    in_partitions = False
    part_device = ""
    part_name = ""
    part_format = ""
    part_uuid = ""

    def emit_pending_partition() -> None:
        nonlocal part_device, part_name, part_format, part_uuid
        fmt = part_format.lower()
        adisk_uuid = _uuid_from_value(part_uuid)
        if part_device.startswith("dk") and fmt == "hfs" and part_name and adisk_uuid:
            pending_partitions.append((part_device, part_name, adisk_uuid, fmt))
        part_device = ""
        part_name = ""
        part_format = ""
        part_uuid = ""

    def flush_disk() -> None:
        nonlocal pending_partitions
        if not disk_device:
            pending_partitions = []
            return
        for pending_device, pending_name, pending_uuid, pending_format in pending_partitions:
            volumes.append(
                MaStVolume(
                    disk_device=disk_device,
                    partition_device=pending_device,
                    volume_root=f"/Volumes/{pending_device}",
                    name=pending_name,
                    adisk_uuid=pending_uuid,
                    builtin=disk_builtin,
                    format=pending_format,
                )
            )
        pending_partitions = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line == "(":
            continue
        if re.match(r"^partitions\s*=", line):
            in_partitions = True
            continue
        if _openstep_collection_close(line):
            in_partitions = False
            continue
        if _openstep_object_close(line):
            if in_partitions and part_device:
                emit_pending_partition()
            elif disk_device:
                flush_disk()
                disk_device = ""
                disk_builtin = False
            continue

        device_name = _openstep_assignment_value(line, "deviceName")
        if device_name is not None:
            if in_partitions:
                part_device = device_name
            else:
                if disk_device:
                    flush_disk()
                    disk_builtin = False
                disk_device = device_name
            continue

        builtin = _openstep_bool_assignment(line, "builtin")
        if builtin is not None and not in_partitions:
            disk_builtin = builtin
            continue

        if in_partitions:
            name = _openstep_assignment_value(line, "name")
            if name is not None:
                part_name = name
                continue
            fmt = _openstep_assignment_value(line, "format")
            if fmt is not None:
                part_format = fmt
                continue
            raw_uuid = _openstep_assignment_value(line, "uuid")
            if raw_uuid is not None:
                part_uuid = raw_uuid
                continue

    if part_device:
        emit_pending_partition()
    if disk_device:
        flush_disk()
    return tuple(volumes)


def _parse_mast_openstep_inventory(content: str) -> tuple[MaStDiskSnapshot, ...]:
    text = _strip_mast_assignment_prefix(content)
    disks: list[MaStDiskSnapshot] = []
    partitions: list[MaStPartitionSnapshot] = []
    disk_device = ""
    disk_name = ""
    disk_size: object | None = None
    disk_builtin = False
    in_partitions = False
    part_device = ""
    part_name = ""
    part_format = ""

    def emit_partition() -> None:
        nonlocal part_device, part_name, part_format
        if part_device or part_name or part_format:
            partitions.append(MaStPartitionSnapshot(part_device, part_name, part_format.lower()))
        part_device = ""
        part_name = ""
        part_format = ""

    def flush_disk() -> None:
        nonlocal partitions, disk_device, disk_name, disk_size, disk_builtin
        if disk_device or disk_name or partitions:
            disks.append(
                MaStDiskSnapshot(
                    device=disk_device,
                    name=disk_name,
                    size=disk_size,
                    builtin=disk_builtin,
                    partitions=tuple(partitions),
                )
            )
        partitions = []
        disk_device = ""
        disk_name = ""
        disk_size = None
        disk_builtin = False

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line == "(":
            continue
        if re.match(r"^partitions\s*=", line):
            in_partitions = True
            continue
        if _openstep_collection_close(line):
            if part_device or part_name or part_format:
                emit_partition()
            in_partitions = False
            continue
        if _openstep_object_close(line):
            if in_partitions and (part_device or part_name or part_format):
                emit_partition()
            elif not in_partitions:
                flush_disk()
            continue

        device_name = _openstep_assignment_value(line, "deviceName")
        if device_name is not None:
            if in_partitions:
                part_device = device_name
            else:
                if disk_device:
                    flush_disk()
                disk_device = device_name
            continue

        name = _openstep_assignment_value(line, "name")
        if name is not None:
            if in_partitions:
                part_name = name
            else:
                disk_name = name
            continue

        fmt = _openstep_assignment_value(line, "format")
        if fmt is not None and in_partitions:
            part_format = fmt
            continue

        size = _openstep_assignment_value(line, "size")
        if size is None:
            size = _openstep_assignment_value(line, "capacity")
        if size is None:
            size = _openstep_assignment_value(line, "totalSize")
        if size is not None and not in_partitions:
            disk_size = size
            continue

        builtin = _openstep_bool_assignment(line, "builtin")
        if builtin is not None and not in_partitions:
            disk_builtin = builtin
            continue

    if part_device or part_name or part_format:
        emit_partition()
    if disk_device or disk_name or partitions:
        flush_disk()
    return tuple(disks)


def parse_mast_plist(content: str | bytes) -> tuple[MaStVolume, ...]:
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
        return _volumes_from_plist_root(plistlib.loads(data))
    except plistlib.InvalidFileException:
        if text is None:
            text = content.decode("utf-8", errors="replace")
        return _parse_mast_openstep(text)


def parse_mast_inventory(content: str | bytes) -> tuple[MaStDiskSnapshot, ...]:
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
        return _disk_snapshots_from_plist_root(plistlib.loads(data))
    except plistlib.InvalidFileException:
        if text is None:
            text = content.decode("utf-8", errors="replace")
        return _parse_mast_openstep_inventory(text)


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


def render_ensure_volume_root_mounted_script(volume_root: str, _device_path: str, wait_seconds: int) -> str:
    root = shlex.quote(volume_root)
    mounted_test = _remote_mounted_test(volume_root)
    attempts = DISKD_USE_VOLUME_GUARD_ATTEMPTS
    return (
        f"mkdir -p {root}; "
        "diskd_attempt=1; "
        f"while [ \"$diskd_attempt\" -le {attempts} ]; do "
        f"if /usr/bin/acp rpc diskd.useVolume path:s:{root} >/dev/null 2>&1; then "
        "wait_attempt=0; "
        f'while [ "$wait_attempt" -le {wait_seconds} ]; do '
        f"if /bin/sh -c {shlex.quote(mounted_test)}; then exit 0; fi; "
        f'if [ "$wait_attempt" -eq {wait_seconds} ]; then break; fi; '
        'wait_attempt=$((wait_attempt + 1)); sleep 1; '
        "done; "
        "fi; "
        f'if [ "$diskd_attempt" -lt {attempts} ]; then sleep 1; fi; '
        'diskd_attempt=$((diskd_attempt + 1)); '
        "done; "
        "exit 1"
    )


def ensure_volume_root_mounted_conn(
    connection: SshConnection,
    volume_root: str,
    device_path: str,
    *,
    wait_seconds: int,
) -> bool:
    script = render_ensure_volume_root_mounted_script(volume_root, device_path, wait_seconds)
    timeout = max(30, wait_seconds * DISKD_USE_VOLUME_GUARD_ATTEMPTS + 45)
    proc = run_ssh(connection, f"/bin/sh -c {shlex.quote(script)}", check=False, timeout=timeout)
    return proc.returncode == 0


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
    proc = run_ssh(connection, f"/bin/sh -c {shlex.quote(script)}", check=False, timeout=30)
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
        mounted = ensure_volume_root_mounted_conn(
            connection,
            volume.volume_root,
            volume.device_path,
            wait_seconds=wait_seconds,
        )
        writable = volume_root_is_writable_conn(connection, volume.volume_root) if mounted else None
        checks.append(PayloadCandidateCheck(volume, mounted, writable))
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
