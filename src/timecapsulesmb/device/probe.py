from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import PurePosixPath

from timecapsulesmb.transport.local import tcp_open
from timecapsulesmb.transport.ssh import run_ssh


@dataclass(frozen=True)
class DevicePaths:
    volume_root: str
    payload_dir: str
    disk_key: str


def discover_volume_root(host: str, password: str, ssh_opts: str) -> str:
    script = r'''
for dev in dk2 dk3; do
  if [ -b "/dev/$dev" ]; then
    volume="/Volumes/$dev"
    [ -d "$volume" ] || mkdir -p "$volume"
    /sbin/mount_hfs "/dev/$dev" "$volume" >/dev/null 2>&1 || true
    if [ -d "$volume" ]; then
      echo "$volume"
      exit 0
    fi
  fi
done
exit 1
    '''
    proc = run_ssh(host, password, ssh_opts, f"/bin/sh -c {shlex.quote(script)}")
    lines = proc.stdout.strip().splitlines()
    volume = lines[-1].strip() if lines else ""
    if not volume:
        raise SystemExit("Failed to discover a Time Capsule volume root on the device.")
    return volume


def build_device_paths(volume_root: str, payload_dir_name: str) -> DevicePaths:
    disk_key = PurePosixPath(volume_root).name
    return DevicePaths(
        volume_root=volume_root,
        payload_dir=f"{volume_root}/{payload_dir_name}",
        disk_key=disk_key,
    )


def wait_for_ssh_state(hostname: str, *, expected_up: bool, timeout_seconds: int = 180) -> bool:
    import time

    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if tcp_open(hostname, 22) == expected_up:
            return True
        time.sleep(5)
    return False
