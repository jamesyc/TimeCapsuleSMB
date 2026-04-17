from __future__ import annotations

import shlex
import time
from dataclasses import dataclass
from pathlib import PurePosixPath

from timecapsulesmb.transport.ssh import run_ssh


@dataclass(frozen=True)
class DevicePaths:
    volume_root: str
    payload_dir: str
    disk_key: str
    data_root: str
    data_root_marker: str


@dataclass(frozen=True)
class MountedVolume:
    device: str
    mountpoint: str


def discover_mounted_volume(host: str, password: str, ssh_opts: str) -> MountedVolume:
    script = r'''
for dev in dk2 dk3; do
  volume="/Volumes/$dev"
  if [ ! -d "$volume" ]; then
    continue
  fi

  df_line=$(/bin/df -k "$volume" 2>/dev/null | /usr/bin/tail -n +2 || true)
  case "$df_line" in
    /dev/$dev*" $volume")
      echo "/dev/$dev $volume"
      exit 0
      ;;
  esac
done

exit 1
    '''
    proc = run_ssh(host, password, ssh_opts, f"/bin/sh -c {shlex.quote(script)}", check=False)
    lines = proc.stdout.strip().splitlines()
    result = lines[-1].strip() if lines else ""
    if proc.returncode != 0 or not result:
        raise SystemExit("Failed to discover a mounted Time Capsule HFS data volume on the device.")
    device, mountpoint = result.split(" ", 1)
    return MountedVolume(device=device, mountpoint=mountpoint)


def discover_volume_root(host: str, password: str, ssh_opts: str) -> str:
    script = r'''
best_any=""
best_existing=""

for dev in dk2 dk3; do
  if [ ! -b "/dev/$dev" ]; then
    continue
  fi

  volume="/Volumes/$dev"
  if [ ! -d "$volume" ]; then
    continue
  fi

  df_line=$(/bin/df -k "$volume" 2>/dev/null | /usr/bin/tail -n +2 || true)
  case "$df_line" in
    *" $volume")
      :
      ;;
    *)
      continue
      ;;
  esac

  if [ ! -w "$volume" ]; then
    continue
  fi

  if [ -d "$volume/ShareRoot" ] || [ -d "$volume/Shared" ]; then
    best_existing="$volume"
    break
  fi
  if [ -z "$best_any" ]; then
    best_any="$volume"
  fi
done

for dev in dk2 dk3; do
  if [ ! -b "/dev/$dev" ]; then
    continue
  fi

  volume="/Volumes/$dev"
  created_mountpoint=0
  if [ ! -d "$volume" ]; then
    mkdir -p "$volume"
    created_mountpoint=1
  fi

  /sbin/mount_hfs "/dev/$dev" "$volume" >/dev/null 2>&1 || true

  df_line=$(/bin/df -k "$volume" 2>/dev/null | /usr/bin/tail -n +2 || true)
  case "$df_line" in
    *" $volume")
      :
      ;;
    *)
      if [ "$created_mountpoint" -eq 1 ]; then
        /bin/rmdir "$volume" >/dev/null 2>&1 || true
      fi
      continue
      ;;
  esac

  if [ ! -w "$volume" ]; then
    continue
  fi

  if [ -d "$volume/ShareRoot" ] || [ -d "$volume/Shared" ]; then
    best_existing="$volume"
    break
  fi
  if [ -z "$best_any" ]; then
    best_any="$volume"
  fi
done

if [ -n "$best_existing" ]; then
  echo "$best_existing"
  exit 0
fi

if [ -n "$best_any" ]; then
  echo "$best_any"
  exit 0
fi

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
    data_root = f"{volume_root}/ShareRoot"
    return DevicePaths(
        volume_root=volume_root,
        payload_dir=f"{volume_root}/{payload_dir_name}",
        disk_key=disk_key,
        data_root=data_root,
        data_root_marker=f"{data_root}/.com.apple.timemachine.supported",
    )


def wait_for_ssh_state(
    host: str,
    password: str,
    ssh_opts: str,
    *,
    expected_up: bool,
    timeout_seconds: int = 180,
) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            proc = run_ssh(host, password, ssh_opts, "/bin/echo ok", check=False, timeout=10)
            is_up = proc.returncode == 0 and proc.stdout.strip().endswith("ok")
        except SystemExit:
            is_up = False
        if is_up == expected_up:
            return True
        time.sleep(5)
    return False
