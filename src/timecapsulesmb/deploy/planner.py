from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from timecapsulesmb.device.probe import DevicePaths


@dataclass(frozen=True)
class DeploymentPlan:
    host: str
    payload_dir: str
    smbd_path: Path
    mdns_path: Path
    flash_targets: dict[str, str]
    payload_targets: dict[str, str]
    private_dir: str


def build_deployment_plan(host: str, device_paths: DevicePaths, smbd_path: Path, mdns_path: Path) -> DeploymentPlan:
    payload_dir = device_paths.payload_dir
    return DeploymentPlan(
        host=host,
        payload_dir=payload_dir,
        smbd_path=smbd_path,
        mdns_path=mdns_path,
        flash_targets={
            "rc.local": "/mnt/Flash/rc.local",
            "start-samba.sh": "/mnt/Flash/start-samba.sh",
            "watchdog.sh": "/mnt/Flash/watchdog.sh",
            "dfree.sh": "/mnt/Flash/dfree.sh",
        },
        payload_targets={
            "smbd": f"{payload_dir}/smbd",
            "mdns-smbd-advertiser": f"{payload_dir}/mdns-smbd-advertiser",
            "smb.conf.template": f"{payload_dir}/smb.conf.template",
        },
        private_dir=f"{payload_dir}/private",
    )
