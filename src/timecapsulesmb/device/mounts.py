from __future__ import annotations

import shlex

from timecapsulesmb.device.errors import DeviceError
from timecapsulesmb.device.probe import discover_mounted_volume_root_conn
from timecapsulesmb.device.util import DISK_NAME_CANDIDATES_SH
from timecapsulesmb.transport.ssh import SshConnection, run_ssh


ENSURE_VOLUME_ROOT_MOUNTED_SH = r'''
is_volume_root_mounted() {
  volume_root=$1
  df_line=$(/bin/df -k "$volume_root" 2>/dev/null | /usr/bin/tail -n +2 || true)
  case "$df_line" in
    *" $volume_root")
      return 0
      ;;
  esac
  return 1
}

mount_device_if_possible() {
  dev_path=$1
  volume_root=$2
  created_mountpoint=0

  if [ ! -b "$dev_path" ]; then
    return 1
  fi

  if [ ! -d "$volume_root" ]; then
    mkdir -p "$volume_root"
    created_mountpoint=1
  fi

  /sbin/mount_hfs "$dev_path" "$volume_root" >/dev/null 2>&1 &
  mount_pid=$!
  attempt=0
  while kill -0 "$mount_pid" >/dev/null 2>&1; do
    if [ "$attempt" -ge 30 ]; then
      kill "$mount_pid" >/dev/null 2>&1 || true
      sleep 1
      kill -9 "$mount_pid" >/dev/null 2>&1 || true
      wait "$mount_pid" >/dev/null 2>&1 || true
      if is_volume_root_mounted "$volume_root"; then
        return 0
      fi
      if [ "$created_mountpoint" -eq 1 ]; then
        /bin/rmdir "$volume_root" >/dev/null 2>&1 || true
      fi
      return 1
    fi
    attempt=$((attempt + 1))
    sleep 1
  done
  wait "$mount_pid" >/dev/null 2>&1 || true

  if is_volume_root_mounted "$volume_root"; then
    return 0
  fi

  if [ "$created_mountpoint" -eq 1 ]; then
    /bin/rmdir "$volume_root" >/dev/null 2>&1 || true
  fi

  return 1
}

try_mount_candidate() {
  dev_path=$1
  volume_root=$2

  if is_volume_root_mounted "$volume_root"; then
    if [ -w "$volume_root" ]; then
      echo "$volume_root"
      return 0
    fi
    return 1
  fi

  mount_device_if_possible "$dev_path" "$volume_root" || true
  if is_volume_root_mounted "$volume_root"; then
    if [ -w "$volume_root" ]; then
      echo "$volume_root"
      return 0
    fi
  fi

  return 1
}

for dev in $(disk_name_candidates); do
  volume="/Volumes/$dev"
  if try_mount_candidate "/dev/$dev" "$volume"; then
    exit 0
  fi
done

exit 1
'''


def ensure_volume_root_mounted_conn(connection: SshConnection) -> str:
    try:
        return discover_mounted_volume_root_conn(connection)
    except DeviceError:
        pass

    # Keep this fallback mount flow in sync with start-samba.sh's
    # try_mount_candidate() / mount_device_if_possible() path.
    script = DISK_NAME_CANDIDATES_SH + ENSURE_VOLUME_ROOT_MOUNTED_SH
    proc = run_ssh(connection, f"/bin/sh -c {shlex.quote(script)}", check=False)
    lines = proc.stdout.strip().splitlines()
    volume = lines[-1].strip() if lines else ""
    if proc.returncode != 0 or not volume:
        raise DeviceError("Failed to discover an AirPort volume root on the device.")
    return volume
