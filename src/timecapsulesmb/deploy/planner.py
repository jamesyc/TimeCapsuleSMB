from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from timecapsulesmb.deploy.commands import (
    RemovePathAction,
    RemoteAction,
    RemotePermission,
    RemoteSymlink,
    RunScriptAction,
    StopProcessAction,
    StopWatchdogAction,
    ensure_volume_mounted_action,
    install_permissions_action,
    prepare_dirs_action,
)
from timecapsulesmb.device.storage import PayloadHome


TransferMode = Literal["scp", "flash_atomic", "generated"]

BINARY_SMBD_SOURCE = "binary:smbd"
BINARY_MDNS_SOURCE = "binary:mdns-advertiser"
BINARY_NBNS_SOURCE = "binary:nbns-advertiser"
PACKAGED_RC_LOCAL_SOURCE = "packaged:rc.local"
PACKAGED_COMMON_SH_SOURCE = "packaged:common.sh"
PACKAGED_DFREE_SH_SOURCE = "packaged:dfree.sh"
PACKAGED_START_SAMBA_SOURCE = "packaged:start-samba.sh"
PACKAGED_WATCHDOG_SOURCE = "packaged:watchdog.sh"
GENERATED_FLASH_CONFIG_SOURCE = "generated:tcapsulesmb.conf"
GENERATED_SMBPASSWD_SOURCE = "generated:smbpasswd"
GENERATED_USERNAME_MAP_SOURCE = "generated:username.map"
DEFAULT_APPLE_MOUNT_WAIT_SECONDS = 30
DEFAULT_ATA_IDLE_SECONDS = 300
DEFAULT_DISKD_USE_VOLUME_ATTEMPTS = 2
PAYLOAD_BINARY_UPLOAD_TIMEOUT_SECONDS = 180
FLASH_TEXT_UPLOAD_TIMEOUT_SECONDS = 120


@dataclass(frozen=True)
class FileTransfer:
    source_id: str
    destination: str
    mode: TransferMode
    timeout_seconds: int | None
    description: str


@dataclass(frozen=True)
class PlannedCheck:
    id: str
    description: str


@dataclass(frozen=True)
class DeploymentPlan:
    host: str
    volume_root: str
    device_path: str
    payload_dir: str
    disk_key: str
    smbd_path: Path
    mdns_path: Path
    nbns_path: Path
    flash_targets: dict[str, str]
    payload_targets: dict[str, str]
    private_dir: str
    remote_directories: list[str]
    legacy_symlinks: list[RemoteSymlink]
    permissions: list[RemotePermission]
    uploads: list[FileTransfer]
    pre_upload_actions: list[RemoteAction]
    post_upload_actions: list[RemoteAction]
    activation_actions: list[RemoteAction]
    reboot_required: bool
    post_deploy_checks: list[PlannedCheck]
    apple_mount_wait_seconds: int


@dataclass(frozen=True)
class ActivationPlan:
    actions: list[RemoteAction]
    post_activation_checks: list[PlannedCheck]


@dataclass(frozen=True)
class UninstallPlan:
    host: str
    volume_roots: list[str]
    payload_dirs: list[str]
    flash_targets: dict[str, str]
    verify_absent_targets: list[str]
    remote_actions: list[RemoteAction]
    reboot_required: bool
    post_uninstall_checks: list[PlannedCheck]


NETBSD4_ACTIVATION_CHECKS = [
    PlannedCheck("netbsd4_runtime_smb_conf_present", "managed runtime smb.conf is present"),
    PlannedCheck("netbsd4_smbd_parent_process", "managed smbd parent process is running"),
    PlannedCheck("netbsd4_smbd_bound_445", "smbd is bound to TCP 445"),
    PlannedCheck("netbsd4_mdns_bound_5353", "mdns-advertiser is bound to UDP 5353"),
]

NETBSD6_REBOOT_DEPLOY_CHECKS = [
    PlannedCheck("ssh_goes_down_after_reboot", "SSH goes down after reboot request"),
    PlannedCheck("ssh_returns_after_reboot", "SSH returns after reboot"),
    PlannedCheck("managed_runtime_smb_conf_present", "managed runtime smb.conf is present"),
    PlannedCheck("managed_smbd_parent_process", "managed smbd parent process is running"),
    PlannedCheck("managed_smbd_bound_445", "smbd is bound to TCP 445"),
    PlannedCheck("managed_mdns_takeover_ready", "managed mDNS takeover becomes ready"),
    PlannedCheck("authenticated_smb_listing", "authenticated SMB listing"),
]

UNINSTALL_REBOOT_CHECKS = [
    PlannedCheck("ssh_goes_down_after_reboot", "SSH goes down after reboot request"),
    PlannedCheck("ssh_returns_after_reboot", "SSH returns after reboot"),
    PlannedCheck("managed_files_absent", "managed payload and flash hooks are absent"),
]


def build_netbsd4_activation_actions() -> list[RemoteAction]:
    return [
        # NetBSD4 activation is re-runnable after deploy or reboot. Stop the
        # old watchdog first so it cannot race the fresh rc.local launch.
        StopWatchdogAction(),
        StopProcessAction("smbd"),
        StopProcessAction("mdns-advertiser"),
        StopProcessAction("nbns-advertiser"),
        StopProcessAction("wcifsfs"),
        RunScriptAction("/mnt/Flash/rc.local"),
    ]


def build_netbsd4_activation_plan() -> ActivationPlan:
    return ActivationPlan(
        actions=build_netbsd4_activation_actions(),
        post_activation_checks=NETBSD4_ACTIVATION_CHECKS,
    )


def build_deployment_plan(
    host: str,
    payload_home: PayloadHome,
    smbd_path: Path,
    mdns_path: Path,
    nbns_path: Path,
    *,
    activate_netbsd4: bool = False,
    reboot_after_deploy: bool = True,
    apple_mount_wait_seconds: int = DEFAULT_APPLE_MOUNT_WAIT_SECONDS,
) -> DeploymentPlan:
    payload_dir = payload_home.payload_dir
    ensure_payload_volume = ensure_volume_mounted_action(
        payload_home.volume_root,
        payload_home.device_path,
        apple_mount_wait_seconds,
    )
    flash_targets = {
        "rc.local": "/mnt/Flash/rc.local",
        "common.sh": "/mnt/Flash/common.sh",
        "start-samba.sh": "/mnt/Flash/start-samba.sh",
        "watchdog.sh": "/mnt/Flash/watchdog.sh",
        "dfree.sh": "/mnt/Flash/dfree.sh",
        "mdns-advertiser": "/mnt/Flash/mdns-advertiser",
        "tcapsulesmb.conf": "/mnt/Flash/tcapsulesmb.conf",
    }
    payload_targets = {
        "smbd": f"{payload_dir}/smbd",
        "mdns-advertiser": f"{payload_dir}/mdns-advertiser",
        "nbns-advertiser": f"{payload_dir}/nbns-advertiser",
    }
    private_dir = f"{payload_dir}/private"
    cache_dir = f"{payload_dir}/cache"
    reboot_required = (not activate_netbsd4) and reboot_after_deploy
    remote_directories = [
        payload_dir,
        private_dir,
        cache_dir,
        "/mnt/Flash",
        "/root",
        "/mnt/Memory/samba4",
    ]
    legacy_symlinks = [
        RemoteSymlink("/root/tc-netbsd4", "/mnt/Memory/samba4"),
        RemoteSymlink("/root/tc-netbsd4le", "/mnt/Memory/samba4"),
        RemoteSymlink("/root/tc-netbsd4be", "/mnt/Memory/samba4"),
        RemoteSymlink("/root/tc-netbsd7", "/mnt/Memory/samba4"),
    ]
    generated_files = [
        FileTransfer(GENERATED_SMBPASSWD_SOURCE, f"{private_dir}/smbpasswd", "generated", None, "generated smbpasswd"),
        FileTransfer(GENERATED_USERNAME_MAP_SOURCE, f"{private_dir}/username.map", "generated", None, "generated username.map"),
    ]
    permissions = [
        RemotePermission(payload_targets["smbd"], "755"),
        RemotePermission(payload_targets["mdns-advertiser"], "755"),
        RemotePermission(payload_targets["nbns-advertiser"], "755"),
        RemotePermission(flash_targets["rc.local"], "755"),
        RemotePermission(flash_targets["common.sh"], "755"),
        RemotePermission(flash_targets["start-samba.sh"], "755"),
        RemotePermission(flash_targets["watchdog.sh"], "755"),
        RemotePermission(flash_targets["dfree.sh"], "755"),
        RemotePermission(flash_targets["mdns-advertiser"], "755"),
        RemotePermission(flash_targets["tcapsulesmb.conf"], "600"),
        RemotePermission(cache_dir, "755"),
        RemotePermission(private_dir, "700"),
        RemotePermission(f"{private_dir}/smbpasswd", "600"),
        RemotePermission(f"{private_dir}/username.map", "600"),
    ]
    return DeploymentPlan(
        host=host,
        volume_root=payload_home.volume_root,
        device_path=payload_home.device_path,
        payload_dir=payload_dir,
        disk_key=payload_home.disk_key,
        smbd_path=smbd_path,
        mdns_path=mdns_path,
        nbns_path=nbns_path,
        flash_targets=flash_targets,
        payload_targets=payload_targets,
        private_dir=private_dir,
        remote_directories=remote_directories,
        legacy_symlinks=legacy_symlinks,
        permissions=permissions,
        uploads=[
            FileTransfer(BINARY_SMBD_SOURCE, payload_targets["smbd"], "scp", PAYLOAD_BINARY_UPLOAD_TIMEOUT_SECONDS, "checked-in smbd"),
            FileTransfer(BINARY_MDNS_SOURCE, payload_targets["mdns-advertiser"], "scp", PAYLOAD_BINARY_UPLOAD_TIMEOUT_SECONDS, "checked-in mdns-advertiser"),
            FileTransfer(BINARY_MDNS_SOURCE, flash_targets["mdns-advertiser"], "flash_atomic", PAYLOAD_BINARY_UPLOAD_TIMEOUT_SECONDS, "flash mdns-advertiser"),
            FileTransfer(BINARY_NBNS_SOURCE, payload_targets["nbns-advertiser"], "scp", PAYLOAD_BINARY_UPLOAD_TIMEOUT_SECONDS, "checked-in nbns-advertiser"),
            FileTransfer(PACKAGED_RC_LOCAL_SOURCE, flash_targets["rc.local"], "flash_atomic", FLASH_TEXT_UPLOAD_TIMEOUT_SECONDS, "packaged rc.local"),
            FileTransfer(PACKAGED_COMMON_SH_SOURCE, flash_targets["common.sh"], "flash_atomic", FLASH_TEXT_UPLOAD_TIMEOUT_SECONDS, "packaged common.sh"),
            FileTransfer(PACKAGED_START_SAMBA_SOURCE, flash_targets["start-samba.sh"], "flash_atomic", FLASH_TEXT_UPLOAD_TIMEOUT_SECONDS, "packaged start-samba.sh"),
            FileTransfer(PACKAGED_WATCHDOG_SOURCE, flash_targets["watchdog.sh"], "flash_atomic", FLASH_TEXT_UPLOAD_TIMEOUT_SECONDS, "packaged watchdog.sh"),
            FileTransfer(PACKAGED_DFREE_SH_SOURCE, flash_targets["dfree.sh"], "flash_atomic", FLASH_TEXT_UPLOAD_TIMEOUT_SECONDS, "packaged dfree.sh"),
            FileTransfer(GENERATED_FLASH_CONFIG_SOURCE, flash_targets["tcapsulesmb.conf"], "flash_atomic", FLASH_TEXT_UPLOAD_TIMEOUT_SECONDS, "generated flash runtime config"),
            *generated_files,
        ],
        pre_upload_actions=[
            # Existing installs run mdns-advertiser directly from /mnt/Flash.
            # Stop the watchdog first so it does not restart daemons while
            # deploy is overwriting the payload and auth files.
            StopWatchdogAction(),
            StopProcessAction("smbd"),
            StopProcessAction("mdns-advertiser"),
            StopProcessAction("nbns-advertiser"),
            ensure_payload_volume,
            RemovePathAction(f"{payload_dir}/smb.conf.template"),
            ensure_payload_volume,
            RemovePathAction(f"{private_dir}/adisk.uuid"),
            ensure_payload_volume,
            RemovePathAction(f"{private_dir}/nbns.enabled"),
            ensure_payload_volume,
            prepare_dirs_action(remote_directories, legacy_symlinks),
        ],
        post_upload_actions=[ensure_payload_volume, install_permissions_action(permissions)],
        activation_actions=build_netbsd4_activation_actions() if activate_netbsd4 else [],
        reboot_required=reboot_required,
        post_deploy_checks=NETBSD4_ACTIVATION_CHECKS if activate_netbsd4 else (NETBSD6_REBOOT_DEPLOY_CHECKS if reboot_required else []),
        apple_mount_wait_seconds=apple_mount_wait_seconds,
    )


def _dedupe_ordered(values: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        deduped.append(value)
    return deduped


def build_uninstall_plan(
    host: str,
    volume_roots: list[str],
    payload_dirs: list[str],
    *,
    reboot_after_uninstall: bool = True,
) -> UninstallPlan:
    volume_roots = _dedupe_ordered(volume_roots)
    payload_dirs = _dedupe_ordered(payload_dirs)
    flash_targets = {
        "rc.local": "/mnt/Flash/rc.local",
        "common.sh": "/mnt/Flash/common.sh",
        "start-samba.sh": "/mnt/Flash/start-samba.sh",
        "watchdog.sh": "/mnt/Flash/watchdog.sh",
        "dfree.sh": "/mnt/Flash/dfree.sh",
        "mdns-advertiser": "/mnt/Flash/mdns-advertiser",
        "tcapsulesmb.conf": "/mnt/Flash/tcapsulesmb.conf",
        "allmdns.txt": "/mnt/Flash/allmdns.txt",
        "applemdns.txt": "/mnt/Flash/applemdns.txt",
    }
    verify_absent_targets = [
        *payload_dirs,
        *flash_targets.values(),
        "/mnt/Memory/samba4",
        "/root/tc-netbsd7",
        "/root/tc-netbsd4",
        "/root/tc-netbsd4le",
        "/root/tc-netbsd4be",
    ]
    return UninstallPlan(
        host=host,
        volume_roots=volume_roots,
        payload_dirs=payload_dirs,
        flash_targets=flash_targets,
        verify_absent_targets=verify_absent_targets,
        remote_actions=[
            StopWatchdogAction(),
            StopProcessAction("smbd"),
            StopProcessAction("mdns-advertiser"),
            StopProcessAction("nbns-advertiser"),
            *(RemovePathAction(payload_dir) for payload_dir in payload_dirs),
            RemovePathAction(flash_targets["rc.local"]),
            RemovePathAction(flash_targets["common.sh"]),
            RemovePathAction(flash_targets["start-samba.sh"]),
            RemovePathAction(flash_targets["watchdog.sh"]),
            RemovePathAction(flash_targets["dfree.sh"]),
            RemovePathAction(flash_targets["mdns-advertiser"]),
            RemovePathAction(flash_targets["tcapsulesmb.conf"]),
            RemovePathAction(flash_targets["allmdns.txt"]),
            RemovePathAction(flash_targets["applemdns.txt"]),
            RemovePathAction("/mnt/Memory/samba4"),
            RemovePathAction("/root/tc-netbsd7"),
            RemovePathAction("/root/tc-netbsd4"),
            RemovePathAction("/root/tc-netbsd4le"),
            RemovePathAction("/root/tc-netbsd4be"),
        ],
        reboot_required=reboot_after_uninstall,
        post_uninstall_checks=UNINSTALL_REBOOT_CHECKS if reboot_after_uninstall else [],
    )
