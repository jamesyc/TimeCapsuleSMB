from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath


DRY_RUN_VOLUME_ROOT_PLACEHOLDER = "unknown until mount"


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


# Keep this candidate order in sync with disk_name_candidates() in
# assets/boot/samba4/start-samba.sh. Deploy/uninstall cannot call the deployed
# script directly, so the consistency boundary is documented and tested here.
DISK_NAME_CANDIDATES_SH = r'''
append_candidate() {
  candidate=$1
  case " $candidates " in
    *" $candidate "*)
      ;;
    *)
      candidates="$candidates $candidate"
      ;;
  esac
}

disk_name_candidates() {
  candidates=""
  dmesg_disk_lines=$(/sbin/dmesg 2>/dev/null | /usr/bin/sed -n '/^dk[0-9][0-9]* at /p' || true)
  metadata_wedges=""
  for dev in $(echo "$dmesg_disk_lines" | /usr/bin/sed -n 's/^\(dk[0-9][0-9]*\) at .*: APconfig$/\1/p;s/^\(dk[0-9][0-9]*\) at .*: APswap$/\1/p'); do
    metadata_wedges="$metadata_wedges $dev"
  done

  for dev in $(echo "$dmesg_disk_lines" | /usr/bin/sed -n 's/^\(dk[0-9][0-9]*\) at .*: APdata$/\1/p'); do
    append_candidate "$dev"
  done
  for dev in $(/sbin/sysctl -n hw.disknames 2>/dev/null); do
    case "$dev" in
      dk[0-9]*)
        case " $metadata_wedges " in
          *" $dev "*)
            ;;
          *)
            append_candidate "$dev"
            ;;
        esac
        ;;
    esac
  done
  if [ -z "$candidates" ]; then
    candidates=" dk2 dk3"
  fi
  echo "$candidates"
}
'''


MOUNTED_VOLUME_DISCOVERY_SH = r'''
for dev in $(disk_name_candidates); do
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


def build_device_paths(volume_root: str, payload_dir_name: str, *, share_use_disk_root: bool = False) -> DevicePaths:
    disk_key = PurePosixPath(volume_root).name
    data_root = volume_root if share_use_disk_root else f"{volume_root}/ShareRoot"
    return DevicePaths(
        volume_root=volume_root,
        payload_dir=f"{volume_root}/{payload_dir_name}",
        disk_key=disk_key,
        data_root=data_root,
        data_root_marker=f"{data_root}/.com.apple.timemachine.supported",
    )


def build_unknown_mount_device_paths(payload_dir_name: str, *, share_use_disk_root: bool = False) -> DevicePaths:
    return build_device_paths(
        DRY_RUN_VOLUME_ROOT_PLACEHOLDER,
        payload_dir_name,
        share_use_disk_root=share_use_disk_root,
    )
