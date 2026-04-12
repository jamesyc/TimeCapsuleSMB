from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from timecapsulesmb.device.probe import DevicePaths


@dataclass(frozen=True)
class FileTransfer:
    source: str
    destination: str
    kind: str


@dataclass(frozen=True)
class DeploymentPlan:
    host: str
    volume_root: str
    payload_dir: str
    disk_key: str
    smbd_path: Path
    mdns_path: Path
    nbns_path: Path
    flash_targets: dict[str, str]
    payload_targets: dict[str, str]
    private_dir: str
    remote_directories: list[str]
    uploads: list[FileTransfer]
    generated_auth_files: list[FileTransfer]
    permission_commands: list[str]
    reboot_required: bool


@dataclass(frozen=True)
class UninstallPlan:
    host: str
    volume_root: str
    payload_dir: str
    flash_targets: dict[str, str]
    remove_targets: list[str]
    verify_absent_targets: list[str]
    stop_commands: list[str]
    reboot_required: bool


def build_deployment_plan(host: str, device_paths: DevicePaths, smbd_path: Path, mdns_path: Path, nbns_path: Path, *, install_nbns: bool = False) -> DeploymentPlan:
    payload_dir = device_paths.payload_dir
    flash_targets = {
        "rc.local": "/mnt/Flash/rc.local",
        "start-samba.sh": "/mnt/Flash/start-samba.sh",
        "watchdog.sh": "/mnt/Flash/watchdog.sh",
        "dfree.sh": "/mnt/Flash/dfree.sh",
    }
    payload_targets = {
        "smbd": f"{payload_dir}/smbd",
        "mdns-smbd-advertiser": f"{payload_dir}/mdns-smbd-advertiser",
        "nbns-name-advertiser": f"{payload_dir}/nbns-name-advertiser",
        "smb.conf.template": f"{payload_dir}/smb.conf.template",
    }
    private_dir = f"{payload_dir}/private"
    return DeploymentPlan(
        host=host,
        volume_root=device_paths.volume_root,
        payload_dir=payload_dir,
        disk_key=device_paths.disk_key,
        smbd_path=smbd_path,
        mdns_path=mdns_path,
        nbns_path=nbns_path,
        flash_targets=flash_targets,
        payload_targets=payload_targets,
        private_dir=private_dir,
        remote_directories=[
            payload_dir,
            private_dir,
            "/mnt/Flash",
        ],
        uploads=[
            FileTransfer(source=str(smbd_path), destination=payload_targets["smbd"], kind="checked-in binary"),
            FileTransfer(source=str(mdns_path), destination=payload_targets["mdns-smbd-advertiser"], kind="checked-in binary"),
            FileTransfer(source=str(nbns_path), destination=payload_targets["nbns-name-advertiser"], kind="checked-in binary"),
            FileTransfer(source="packaged rc.local", destination=flash_targets["rc.local"], kind="packaged asset"),
            FileTransfer(source="rendered start-samba.sh", destination=flash_targets["start-samba.sh"], kind="rendered asset"),
            FileTransfer(source="rendered watchdog.sh", destination=flash_targets["watchdog.sh"], kind="rendered asset"),
            FileTransfer(source="packaged dfree.sh", destination=flash_targets["dfree.sh"], kind="packaged asset"),
            FileTransfer(source="rendered smb.conf.template", destination=payload_targets["smb.conf.template"], kind="rendered asset"),
        ],
        generated_auth_files=[
            FileTransfer(source="generated smbpasswd", destination=f"{private_dir}/smbpasswd", kind="generated auth"),
            FileTransfer(source="generated username.map", destination=f"{private_dir}/username.map", kind="generated auth"),
            FileTransfer(source="generated adisk UUID", destination=f"{private_dir}/adisk.uuid", kind="generated metadata"),
        ]
        + ([
            FileTransfer(source="generated nbns marker", destination=f"{private_dir}/nbns.enabled", kind="generated metadata"),
        ] if install_nbns else []),
        permission_commands=[
            "chmod 755 /mnt/Flash/rc.local /mnt/Flash/start-samba.sh /mnt/Flash/watchdog.sh /mnt/Flash/dfree.sh",
            f"chmod 755 {payload_targets['smbd']} {payload_targets['mdns-smbd-advertiser']} {payload_targets['nbns-name-advertiser']}",
            f"chmod 700 {private_dir}",
            f"chmod 600 {private_dir}/smbpasswd {private_dir}/username.map {private_dir}/adisk.uuid {private_dir}/nbns.enabled >/dev/null 2>&1 || "
            f"chmod 600 {private_dir}/smbpasswd {private_dir}/username.map {private_dir}/adisk.uuid",
        ],
        reboot_required=True,
    )


def build_uninstall_plan(host: str, device_paths: DevicePaths) -> UninstallPlan:
    payload_dir = device_paths.payload_dir
    flash_targets = {
        "rc.local": "/mnt/Flash/rc.local",
        "start-samba.sh": "/mnt/Flash/start-samba.sh",
        "watchdog.sh": "/mnt/Flash/watchdog.sh",
        "dfree.sh": "/mnt/Flash/dfree.sh",
    }
    remove_targets = [
        payload_dir,
        *flash_targets.values(),
        "/mnt/Memory/samba4",
        "/root/tc-stage4",
    ]
    verify_absent_targets = [
        payload_dir,
        *flash_targets.values(),
    ]
    stop_commands = [
        "pkill smbd >/dev/null 2>&1 || true",
        "pkill mdns-smbd-advertiser >/dev/null 2>&1 || true",
        "pkill nbns-name-advertiser >/dev/null 2>&1 || true",
    ]
    return UninstallPlan(
        host=host,
        volume_root=device_paths.volume_root,
        payload_dir=payload_dir,
        flash_targets=flash_targets,
        remove_targets=remove_targets,
        verify_absent_targets=verify_absent_targets,
        stop_commands=stop_commands,
        reboot_required=True,
    )
