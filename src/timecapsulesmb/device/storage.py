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


NO_WRITABLE_PERSISTENT_VOLUME_MESSAGE = "no writable persistent volume found"
MAST_DISCOVERY_ATTEMPTS = 10
MAST_DISCOVERY_DELAY_SECONDS = 3
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


def read_mast_volumes_conn(connection: SshConnection) -> tuple[MaStVolume, ...]:
    proc = run_ssh(connection, "/usr/bin/acp MaSt", timeout=60)
    return parse_mast_plist(proc.stdout)


def wait_for_mast_volumes_conn(
    connection: SshConnection,
    *,
    attempts: int = MAST_DISCOVERY_ATTEMPTS,
    delay_seconds: int = MAST_DISCOVERY_DELAY_SECONDS,
) -> MaStDiscoveryResult:
    if attempts <= 0:
        attempts = 1
    volumes: tuple[MaStVolume, ...] = ()
    for attempt in range(1, attempts + 1):
        volumes = read_mast_volumes_conn(connection)
        if volumes:
            return MaStDiscoveryResult(volumes, attempt)
        if attempt < attempts:
            time.sleep(delay_seconds)
    return MaStDiscoveryResult(volumes, attempts)


def _remote_mounted_test(volume_root: str) -> str:
    quoted_root = shlex.quote(volume_root)
    return (
        f"df_line=$(/bin/df -k {quoted_root} 2>/dev/null | /usr/bin/tail -n +2 || true); "
        f'case "$df_line" in *" {volume_root}") exit 0 ;; esac; exit 1'
    )


def render_ensure_volume_root_mounted_script(volume_root: str, device_path: str, wait_seconds: int) -> str:
    root = shlex.quote(volume_root)
    dev = shlex.quote(device_path)
    mounted_test = _remote_mounted_test(volume_root)
    return (
        f"mkdir -p {root}; "
        f"if /bin/sh -c {shlex.quote(mounted_test)}; then exit 0; fi; "
        f"/usr/bin/acp rpc diskd.useVolume path:s:{root} >/dev/null 2>&1 || true; "
        "attempt=0; "
        f'while [ "$attempt" -lt {wait_seconds} ]; do '
        f"if /bin/sh -c {shlex.quote(mounted_test)}; then exit 0; fi; "
        'attempt=$((attempt + 1)); sleep 1; '
        "done; "
        f"if [ -b {dev} ]; then /sbin/mount_hfs {dev} {root} >/dev/null 2>&1 || true; fi; "
        f"/bin/sh -c {shlex.quote(mounted_test)}"
    )


def ensure_volume_root_mounted_conn(
    connection: SshConnection,
    volume_root: str,
    device_path: str,
    *,
    wait_seconds: int,
) -> bool:
    script = render_ensure_volume_root_mounted_script(volume_root, device_path, wait_seconds)
    proc = run_ssh(connection, f"/bin/sh -c {shlex.quote(script)}", check=False, timeout=max(30, wait_seconds + 45))
    return proc.returncode == 0


def ensure_mast_volume_mounted_conn(
    connection: SshConnection,
    volume: MaStVolume,
    *,
    wait_seconds: int,
) -> bool:
    return ensure_volume_root_mounted_conn(
        connection,
        volume.volume_root,
        volume.device_path,
        wait_seconds=wait_seconds,
    )


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
        f"[ -f {payload_dir}/private/smbpasswd ] || add_missing 'missing private/smbpasswd'; "
        f"[ -f {payload_dir}/private/username.map ] || add_missing 'missing private/username.map'; "
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
        if ensure_mast_volume_mounted_conn(connection, volume, wait_seconds=wait_seconds):
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
        mounted = ensure_mast_volume_mounted_conn(connection, volume, wait_seconds=wait_seconds)
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


def select_payload_home_conn(
    connection: SshConnection,
    volumes: tuple[MaStVolume, ...],
    payload_dir_name: str,
    *,
    wait_seconds: int,
) -> PayloadHome:
    selection = select_payload_home_with_diagnostics_conn(
        connection,
        volumes,
        payload_dir_name,
        wait_seconds=wait_seconds,
    )
    if selection.payload_home is not None:
        return selection.payload_home
    raise RuntimeError(NO_WRITABLE_PERSISTENT_VOLUME_MESSAGE)
