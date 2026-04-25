from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from timecapsulesmb.deploy.commands import (
    RemoteAction,
    enable_nbns_action,
    initialize_data_root_action,
    install_permissions_action,
    prepare_dirs_action,
    remove_path_action,
    run_script_action,
    stop_process_full_action,
    stop_process_action,
)
from timecapsulesmb.device.probe import DevicePaths


@dataclass(frozen=True)
class FileTransfer:
    source: str
    destination: str
    kind: str


@dataclass(frozen=True)
class PlannedCheck:
    id: str
    description: str


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
    pre_upload_actions: list[RemoteAction]
    post_auth_actions: list[RemoteAction]
    activation_actions: list[RemoteAction]
    reboot_required: bool
    post_deploy_checks: list[PlannedCheck]


@dataclass(frozen=True)
class ActivationPlan:
    actions: list[RemoteAction]
    post_activation_checks: list[PlannedCheck]


@dataclass(frozen=True)
class UninstallPlan:
    host: str
    volume_root: str
    payload_dir: str
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
        stop_process_full_action("[w]atchdog.sh"),
        stop_process_action("smbd"),
        stop_process_action("mdns-advertiser"),
        stop_process_action("nbns-advertiser"),
        stop_process_action("wcifsfs"),
        run_script_action("/mnt/Flash/rc.local"),
    ]


def build_netbsd4_activation_plan() -> ActivationPlan:
    return ActivationPlan(
        actions=build_netbsd4_activation_actions(),
        post_activation_checks=NETBSD4_ACTIVATION_CHECKS,
    )


def build_deployment_plan(
    host: str,
    device_paths: DevicePaths,
    smbd_path: Path,
    mdns_path: Path,
    nbns_path: Path,
    *,
    install_nbns: bool = False,
    activate_netbsd4: bool = False,
    reboot_after_deploy: bool = True,
) -> DeploymentPlan:
    payload_dir = device_paths.payload_dir
    flash_targets = {
        "rc.local": "/mnt/Flash/rc.local",
        "common.sh": "/mnt/Flash/common.sh",
        "start-samba.sh": "/mnt/Flash/start-samba.sh",
        "watchdog.sh": "/mnt/Flash/watchdog.sh",
        "dfree.sh": "/mnt/Flash/dfree.sh",
        "mdns-advertiser": "/mnt/Flash/mdns-advertiser",
    }
    payload_targets = {
        "smbd": f"{payload_dir}/smbd",
        "mdns-advertiser": f"{payload_dir}/mdns-advertiser",
        "nbns-advertiser": f"{payload_dir}/nbns-advertiser",
        "smb.conf.template": f"{payload_dir}/smb.conf.template",
    }
    private_dir = f"{payload_dir}/private"
    cache_dir = f"{payload_dir}/cache"
    reboot_required = (not activate_netbsd4) and reboot_after_deploy
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
            cache_dir,
            "/mnt/Flash",
        ],
        uploads=[
            FileTransfer(source=str(smbd_path), destination=payload_targets["smbd"], kind="checked-in binary"),
            FileTransfer(source=str(mdns_path), destination=payload_targets["mdns-advertiser"], kind="checked-in binary"),
            FileTransfer(source=str(mdns_path), destination=flash_targets["mdns-advertiser"], kind="checked-in binary"),
            FileTransfer(source=str(nbns_path), destination=payload_targets["nbns-advertiser"], kind="checked-in binary"),
            FileTransfer(source="packaged rc.local", destination=flash_targets["rc.local"], kind="packaged asset"),
            FileTransfer(source="packaged common.sh", destination=flash_targets["common.sh"], kind="packaged asset"),
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
        pre_upload_actions=[
            # Existing installs run mdns-advertiser directly from /mnt/Flash.
            # Stop the watchdog first so it does not restart daemons while
            # deploy is overwriting the payload and auth files.
            stop_process_full_action("[w]atchdog.sh"),
            stop_process_action("smbd"),
            stop_process_action("mdns-advertiser"),
            stop_process_action("nbns-advertiser"),
            initialize_data_root_action(device_paths.data_root, device_paths.data_root_marker),
            prepare_dirs_action(payload_dir),
        ]
        + ([enable_nbns_action(private_dir)] if install_nbns else []),
        post_auth_actions=[install_permissions_action(payload_dir)],
        activation_actions=build_netbsd4_activation_actions() if activate_netbsd4 else [],
        reboot_required=reboot_required,
        post_deploy_checks=NETBSD4_ACTIVATION_CHECKS if activate_netbsd4 else (NETBSD6_REBOOT_DEPLOY_CHECKS if reboot_required else []),
    )


def build_uninstall_plan(host: str, device_paths: DevicePaths, *, reboot_after_uninstall: bool = True) -> UninstallPlan:
    payload_dir = device_paths.payload_dir
    flash_targets = {
        "rc.local": "/mnt/Flash/rc.local",
        "common.sh": "/mnt/Flash/common.sh",
        "start-samba.sh": "/mnt/Flash/start-samba.sh",
        "watchdog.sh": "/mnt/Flash/watchdog.sh",
        "dfree.sh": "/mnt/Flash/dfree.sh",
        "mdns-advertiser": "/mnt/Flash/mdns-advertiser",
        "allmdns.txt": "/mnt/Flash/allmdns.txt",
        "applemdns.txt": "/mnt/Flash/applemdns.txt",
    }
    verify_absent_targets = [
        payload_dir,
        *flash_targets.values(),
        "/mnt/Memory/samba4",
        "/root/tc-netbsd7",
        "/root/tc-netbsd4",
        "/root/tc-netbsd4le",
        "/root/tc-netbsd4be",
    ]
    return UninstallPlan(
        host=host,
        volume_root=device_paths.volume_root,
        payload_dir=payload_dir,
        flash_targets=flash_targets,
        verify_absent_targets=verify_absent_targets,
        remote_actions=[
            stop_process_full_action("[w]atchdog.sh"),
            stop_process_action("smbd"),
            stop_process_action("mdns-advertiser"),
            stop_process_action("nbns-advertiser"),
            remove_path_action(payload_dir),
            remove_path_action(flash_targets["rc.local"]),
            remove_path_action(flash_targets["common.sh"]),
            remove_path_action(flash_targets["start-samba.sh"]),
            remove_path_action(flash_targets["watchdog.sh"]),
            remove_path_action(flash_targets["dfree.sh"]),
            remove_path_action(flash_targets["mdns-advertiser"]),
            remove_path_action(flash_targets["allmdns.txt"]),
            remove_path_action(flash_targets["applemdns.txt"]),
            remove_path_action("/mnt/Memory/samba4"),
            remove_path_action("/root/tc-netbsd7"),
            remove_path_action("/root/tc-netbsd4"),
            remove_path_action("/root/tc-netbsd4le"),
            remove_path_action("/root/tc-netbsd4be"),
        ],
        reboot_required=reboot_after_uninstall,
        post_uninstall_checks=UNINSTALL_REBOOT_CHECKS if reboot_after_uninstall else [],
    )
