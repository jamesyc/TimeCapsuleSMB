from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
import plistlib
import shlex
import uuid

from timecapsulesmb.transport.ssh import SshConnection, run_ssh


NO_WRITABLE_PERSISTENT_VOLUME_MESSAGE = "no writable persistent volume found"
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


def parse_mast_plist(content: str | bytes) -> tuple[MaStVolume, ...]:
    if isinstance(content, bytes):
        data = content
    else:
        text = content.strip()
        xml_start = text.find("<?xml")
        if xml_start >= 0:
            text = text[xml_start:]
        elif text.startswith("MaSt="):
            text = text.split("=", 1)[1]
        data = text.encode("utf-8", errors="replace")
    root = plistlib.loads(data)
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


def read_mast_volumes_conn(connection: SshConnection) -> tuple[MaStVolume, ...]:
    proc = run_ssh(connection, "/usr/bin/acp MaSt", timeout=60)
    return parse_mast_plist(proc.stdout)


def _remote_mounted_test(volume_root: str) -> str:
    quoted_root = shlex.quote(volume_root)
    return (
        f"df_line=$(/bin/df -k {quoted_root} 2>/dev/null | /usr/bin/tail -n +2 || true); "
        f'case "$df_line" in *" {volume_root}") exit 0 ;; esac; exit 1'
    )


def ensure_mast_volume_mounted_conn(
    connection: SshConnection,
    volume: MaStVolume,
    *,
    wait_seconds: int,
) -> bool:
    root = shlex.quote(volume.volume_root)
    dev = shlex.quote(volume.device_path)
    mounted_test = _remote_mounted_test(volume.volume_root)
    script = (
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
    proc = run_ssh(connection, f"/bin/sh -c {shlex.quote(script)}", check=False, timeout=max(30, wait_seconds + 45))
    return proc.returncode == 0


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


def select_payload_home_conn(
    connection: SshConnection,
    volumes: tuple[MaStVolume, ...],
    payload_dir_name: str,
    *,
    wait_seconds: int,
) -> PayloadHome:
    ordered = tuple(volume for volume in volumes if volume.builtin) + tuple(volume for volume in volumes if not volume.builtin)
    for volume in ordered:
        if not ensure_mast_volume_mounted_conn(connection, volume, wait_seconds=wait_seconds):
            continue
        if not volume_root_is_writable_conn(connection, volume.volume_root):
            continue
        return PayloadHome(
            volume_root=volume.volume_root,
            device_path=volume.device_path,
            payload_dir_name=payload_dir_name,
        )
    raise RuntimeError(NO_WRITABLE_PERSISTENT_VOLUME_MESSAGE)
