from __future__ import annotations

import shlex
import time
from dataclasses import dataclass
from pathlib import PurePosixPath

from timecapsulesmb.transport.local import tcp_open
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


@dataclass(frozen=True)
class ProbeResult:
    ssh_port_reachable: bool
    ssh_authenticated: bool
    error: str | None
    os_name: str
    os_release: str
    arch: str
    elf_endianness: str


def probe_device(host: str, password: str, ssh_opts: str) -> ProbeResult:
    probe_host = host.split("@", 1)[1] if "@" in host else host
    if not tcp_open(probe_host, 22):
        return ProbeResult(
            ssh_port_reachable=False,
            ssh_authenticated=False,
            error="SSH is not reachable yet.",
            os_name="",
            os_release="",
            arch="",
            elf_endianness="unknown",
        )

    try:
        os_name, os_release, arch = _probe_remote_os_info(host, password, ssh_opts)
        elf_endianness = _probe_remote_elf_endianness(host, password, ssh_opts)
    except SystemExit as exc:
        return ProbeResult(
            ssh_port_reachable=True,
            ssh_authenticated=False,
            error=str(exc) or "SSH authentication failed.",
            os_name="",
            os_release="",
            arch="",
            elf_endianness="unknown",
        )

    return ProbeResult(
        ssh_port_reachable=True,
        ssh_authenticated=True,
        error=None,
        os_name=os_name,
        os_release=os_release,
        arch=arch,
        elf_endianness=elf_endianness,
    )


def _probe_remote_os_info(host: str, password: str, ssh_opts: str) -> tuple[str, str, str]:
    script = "printf '%s\\n%s\\n%s\\n' \"$(uname -s)\" \"$(uname -r)\" \"$(uname -m)\""
    proc = run_ssh(host, password, ssh_opts, f"/bin/sh -c {shlex.quote(script)}")
    lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    if len(lines) < 3:
        raise SystemExit("Failed to determine remote device OS compatibility.")
    return lines[0], lines[1], lines[2]


def _probe_remote_elf_endianness(host: str, password: str, ssh_opts: str, path: str = "/bin/sh") -> str:
    script = rf"""
path={shlex.quote(path)}
if [ ! -f "$path" ]; then
  exit 1
fi
if [ -x /usr/bin/od ] && [ -x /usr/bin/tr ]; then
  b5=$(/bin/dd if="$path" bs=1 skip=5 count=1 2>/dev/null | /usr/bin/od -An -t u1 | /usr/bin/tr -d '[:space:]')
else
  b5=$(/bin/dd if="$path" bs=1 skip=5 count=1 2>/dev/null | /usr/bin/sed -n l 2>/dev/null)
fi
case "$b5" in
  1) echo little ;;
  2) echo big ;;
  "\\001$") echo little ;;
  "\\002$") echo big ;;
  *) echo unknown ;;
esac
"""
    proc = run_ssh(host, password, ssh_opts, f"/bin/sh -c {shlex.quote(script)}", check=False)
    endianness = (proc.stdout or "").strip().splitlines()
    value = endianness[-1].strip() if endianness else ""
    if value in {"little", "big", "unknown"}:
        return value
    return "unknown"


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


def remote_interface_exists(host: str, password: str, ssh_opts: str, iface: str) -> bool:
    script = f"/sbin/ifconfig {shlex.quote(iface)} >/dev/null 2>&1"
    proc = run_ssh(host, password, ssh_opts, f"/bin/sh -c {shlex.quote(script)}", check=False)
    return_code = getattr(proc, "returncode", 0)
    if not isinstance(return_code, int):
        return True
    return return_code == 0


def discover_volume_root(host: str, password: str, ssh_opts: str) -> str:
    try:
        return discover_mounted_volume(host, password, ssh_opts).mountpoint
    except SystemExit:
        pass

    script = r'''
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

  echo "$volume"
  exit 0
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
