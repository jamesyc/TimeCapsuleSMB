from __future__ import annotations

import shlex
import subprocess
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from timecapsulesmb.transport.local import tcp_open
from timecapsulesmb.transport.ssh import SshConnection, run_ssh, ssh_opts_use_proxy
from timecapsulesmb.core.config import AIRPORT_SYAP_TO_MODEL

if TYPE_CHECKING:
    from timecapsulesmb.device.compat import DeviceCompatibility


RUNTIME_SMB_CONF = "/mnt/Memory/samba4/etc/smb.conf"
SMBD_STATUS_HELPERS = rf'''
runtime_smb_conf_present() {{
    [ -f {RUNTIME_SMB_CONF} ]
}}

smbd_log_path_from_config() {{
    /usr/bin/sed -n 's/^[[:space:]]*log file[[:space:]]*=[[:space:]]*//p' {RUNTIME_SMB_CONF} 2>/dev/null \
        | /usr/bin/sed -n '1p'
}}

file_tail_bytes() {{
    file_tail_path=$1
    file_tail_bytes=${{2:-262144}}
    if [ -f "$file_tail_path" ]; then
        set -- $(/bin/ls -l "$file_tail_path" 2>/dev/null)
        file_tail_size=$5
        case "$file_tail_size" in
            ''|*[!0-9]*) file_tail_size=0 ;;
        esac
        case "$file_tail_bytes" in
            ''|*[!0-9]*) file_tail_bytes=262144 ;;
        esac
        file_tail_block_size=4096
        file_tail_blocks=$(((file_tail_bytes + file_tail_block_size - 1) / file_tail_block_size))
        if [ "$file_tail_size" -gt "$file_tail_bytes" ]; then
            file_tail_skip=$((file_tail_size / file_tail_block_size - file_tail_blocks))
            [ "$file_tail_skip" -lt 0 ] && file_tail_skip=0
        else
            file_tail_skip=0
        fi
        /bin/dd if="$file_tail_path" bs=$file_tail_block_size skip=$file_tail_skip 2>/dev/null \
            || /bin/cat "$file_tail_path" 2>/dev/null \
            || true
    fi
}}

smbd_log_has_daemon_ready() {{
    smbd_log_path=$(smbd_log_path_from_config)
    [ -n "$smbd_log_path" ] || return 1
    [ -f "$smbd_log_path" ] || return 1
    ready_line=$(file_tail_bytes "$smbd_log_path" 262144 | /usr/bin/sed -n '/daemon_ready/p' | /usr/bin/sed -n '1p')
    [ -n "$ready_line" ]
}}

smbd_bound_445() {{
    case "$1" in
        *smbd*":445"*) return 0 ;;
        *) return 1 ;;
    esac
}}

mdns_bound_5353() {{
    case "$1" in
        *mdns-advertiser*":5353"*) return 0 ;;
        *) return 1 ;;
    esac
}}

managed_smbd_ready() {{
    require_daemon_ready=$1
    fstat_out=$2
    runtime_smb_conf_present || return 1
    smbd_bound_445 "$fstat_out" || return 1
    if [ "$require_daemon_ready" = "1" ]; then
        smbd_log_has_daemon_ready || return 1
    fi
    return 0
}}

describe_managed_smbd_status() {{
    require_daemon_ready=$1
    fstat_out=$2
    status=0
    if runtime_smb_conf_present; then
        echo "PASS:managed runtime smb.conf present"
    else
        echo "FAIL:managed runtime smb.conf missing"
        status=1
    fi
    if smbd_bound_445 "$fstat_out"; then
        echo "PASS:smbd bound to TCP 445"
    else
        echo "FAIL:smbd is not bound to TCP 445"
        status=1
    fi
    if [ "$require_daemon_ready" = "1" ]; then
        if smbd_log_has_daemon_ready; then
            echo "PASS:managed smbd reported daemon_ready"
        else
            echo "FAIL:managed smbd did not report daemon_ready"
            status=1
        fi
    fi
    return "$status"
}}
'''


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
    airport_model: str | None = None
    airport_syap: str | None = None


@dataclass(frozen=True)
class ProbedDeviceState:
    probe_result: ProbeResult
    compatibility: DeviceCompatibility | None


@dataclass(frozen=True)
class SshCommandProbeResult:
    ok: bool
    detail: str


@dataclass(frozen=True)
class RemoteInterfaceProbeResult:
    iface: str
    exists: bool
    detail: str


@dataclass(frozen=True)
class RemoteInterfaceCandidate:
    name: str
    ipv4_addrs: tuple[str, ...]
    up: bool
    active: bool
    loopback: bool


@dataclass(frozen=True)
class RemoteInterfaceCandidatesProbeResult:
    candidates: tuple[RemoteInterfaceCandidate, ...]
    preferred_iface: str | None
    detail: str


@dataclass(frozen=True)
class ManagedSmbdProbeResult:
    ready: bool
    detail: str


@dataclass(frozen=True)
class ManagedMdnsTakeoverProbeResult:
    ready: bool
    detail: str


@dataclass(frozen=True)
class AirportIdentityProbeResult:
    model: str | None
    syap: str | None
    detail: str


def _conn(host: str, password: str, ssh_opts: str) -> SshConnection:
    return SshConnection(host=host, password=password, ssh_opts=ssh_opts)


def run_ssh_conn(connection: SshConnection, remote_cmd: str, *, check: bool = True, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return run_ssh(connection.host, connection.password, connection.ssh_opts, remote_cmd, check=check, timeout=timeout)


def probe_device(host: str, password: str, ssh_opts: str) -> ProbeResult:
    connection = _conn(host, password, ssh_opts)
    return probe_device_conn(connection)


def probe_device_conn(connection: SshConnection) -> ProbeResult:
    probe_host = connection.host.split("@", 1)[1] if "@" in connection.host else connection.host
    if not ssh_opts_use_proxy(connection.ssh_opts) and not tcp_open(probe_host, 22):
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
        os_name, os_release, arch = _probe_remote_os_info_conn(connection)
        elf_endianness = _probe_remote_elf_endianness_conn(connection)
        airport_identity = probe_remote_airport_identity_conn(connection)
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
        airport_model=airport_identity.model,
        airport_syap=airport_identity.syap,
    )


def probe_ssh_command(
    host: str,
    password: str,
    ssh_opts: str,
    command: str,
    *,
    timeout: int = 30,
    expected_stdout_suffix: str | None = None,
) -> SshCommandProbeResult:
    try:
        proc = run_ssh_conn(_conn(host, password, ssh_opts), command, check=False, timeout=timeout)
    except SystemExit as exc:
        return SshCommandProbeResult(ok=False, detail=str(exc))
    if proc.returncode == 0:
        stdout = proc.stdout.strip()
        if expected_stdout_suffix is None or stdout.endswith(expected_stdout_suffix):
            return SshCommandProbeResult(ok=True, detail=stdout)
    detail = proc.stdout.strip() or f"rc={proc.returncode}"
    return SshCommandProbeResult(ok=False, detail=detail)


def _probe_remote_os_info(host: str, password: str, ssh_opts: str) -> tuple[str, str, str]:
    return _probe_remote_os_info_conn(_conn(host, password, ssh_opts))


def _probe_remote_os_info_conn(connection: SshConnection) -> tuple[str, str, str]:
    script = "printf '%s\\n%s\\n%s\\n' \"$(uname -s)\" \"$(uname -r)\" \"$(uname -m)\""
    proc = run_ssh_conn(connection, f"/bin/sh -c {shlex.quote(script)}")
    lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    if len(lines) < 3:
        raise SystemExit("Failed to determine remote device OS compatibility.")
    return lines[0], lines[1], lines[2]


def _probe_remote_elf_endianness(host: str, password: str, ssh_opts: str, path: str = "/bin/sh") -> str:
    return _probe_remote_elf_endianness_conn(_conn(host, password, ssh_opts), path=path)


def _probe_remote_elf_endianness_conn(connection: SshConnection, path: str = "/bin/sh") -> str:
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
    proc = run_ssh_conn(connection, f"/bin/sh -c {shlex.quote(script)}", check=False)
    endianness = (proc.stdout or "").strip().splitlines()
    value = endianness[-1].strip() if endianness else ""
    if value in {"little", "big", "unknown"}:
        return value
    return "unknown"


def extract_airport_identity_from_acpdata(text: str) -> AirportIdentityProbeResult:
    for syap, model in AIRPORT_SYAP_TO_MODEL.items():
        if model in text:
            return AirportIdentityProbeResult(model=model, syap=syap, detail=f"found {model} in ACPData")
    return AirportIdentityProbeResult(model=None, syap=None, detail="no TimeCapsule model found in ACPData")


def probe_remote_airport_identity_conn(connection: SshConnection) -> AirportIdentityProbeResult:
    script = r"""
if [ ! -f /mnt/Flash/ACPData.bin ]; then
  exit 0
fi
if [ -x /usr/bin/strings ]; then
  /usr/bin/strings /mnt/Flash/ACPData.bin 2>/dev/null
else
  /usr/bin/sed -n 's/.*\(TimeCapsule[0-9],[0-9][0-9][0-9]\).*/\1/p' /mnt/Flash/ACPData.bin 2>/dev/null
fi | /usr/bin/sed -n 's/.*\(TimeCapsule[0-9],[0-9][0-9][0-9]\).*/\1/p' | /usr/bin/sed -n '1p'
"""
    proc = run_ssh_conn(connection, f"/bin/sh -c {shlex.quote(script)}", check=False, timeout=30)
    if proc.returncode != 0:
        return AirportIdentityProbeResult(model=None, syap=None, detail=f"could not read ACPData: rc={proc.returncode}")
    if not proc.stdout:
        return AirportIdentityProbeResult(model=None, syap=None, detail="ACPData missing or empty")
    return extract_airport_identity_from_acpdata(proc.stdout)


def discover_mounted_volume(host: str, password: str, ssh_opts: str) -> MountedVolume:
    return discover_mounted_volume_conn(_conn(host, password, ssh_opts))


def discover_mounted_volume_conn(connection: SshConnection) -> MountedVolume:
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
    proc = run_ssh_conn(connection, f"/bin/sh -c {shlex.quote(script)}", check=False)
    lines = proc.stdout.strip().splitlines()
    result = lines[-1].strip() if lines else ""
    if proc.returncode != 0 or not result:
        raise SystemExit("Failed to discover a mounted Time Capsule HFS data volume on the device.")
    device, mountpoint = result.split(" ", 1)
    return MountedVolume(device=device, mountpoint=mountpoint)


def probe_remote_interface(host: str, password: str, ssh_opts: str, iface: str) -> RemoteInterfaceProbeResult:
    return probe_remote_interface_conn(_conn(host, password, ssh_opts), iface)


def probe_remote_interface_conn(connection: SshConnection, iface: str) -> RemoteInterfaceProbeResult:
    script = f"/sbin/ifconfig {shlex.quote(iface)} >/dev/null 2>&1"
    proc = run_ssh_conn(connection, f"/bin/sh -c {shlex.quote(script)}", check=False)
    return_code = getattr(proc, "returncode", 0)
    if not isinstance(return_code, int):
        return RemoteInterfaceProbeResult(iface=iface, exists=True, detail=f"ifconfig {iface} returned non-integer status")
    if return_code == 0:
        return RemoteInterfaceProbeResult(iface=iface, exists=True, detail=f"interface {iface} exists")
    return RemoteInterfaceProbeResult(iface=iface, exists=False, detail=f"interface {iface} was not found on the device")


def _is_link_local_ipv4(value: str) -> bool:
    return value.startswith("169.254.")


def _is_loopback_ipv4(value: str) -> bool:
    return value.startswith("127.")


def _is_private_ipv4(value: str) -> bool:
    if value.startswith("10.") or value.startswith("192.168."):
        return True
    if not value.startswith("172."):
        return False
    parts = value.split(".")
    if len(parts) < 2:
        return False
    try:
        second = int(parts[1])
    except ValueError:
        return False
    return 16 <= second <= 31


def _parse_ifconfig_candidates(output: str) -> tuple[RemoteInterfaceCandidate, ...]:
    candidates: list[RemoteInterfaceCandidate] = []
    current_name: str | None = None
    current_ipv4: list[str] = []
    current_up = False
    current_active = False
    current_loopback = False

    def flush() -> None:
        nonlocal current_name, current_ipv4, current_up, current_active, current_loopback
        if current_name is not None:
            candidates.append(
                RemoteInterfaceCandidate(
                    name=current_name,
                    ipv4_addrs=tuple(current_ipv4),
                    up=current_up,
                    active=current_active,
                    loopback=current_loopback,
                )
            )
        current_name = None
        current_ipv4 = []
        current_up = False
        current_active = False
        current_loopback = False

    for raw_line in output.splitlines():
        line = raw_line.rstrip()
        if not line:
            continue
        if not line.startswith((" ", "\t")) and ":" in line:
            flush()
            header, _sep, _rest = line.partition(":")
            flags = line.partition("<")[2].partition(">")[0]
            current_name = header.strip()
            current_up = "UP" in flags.split(",")
            current_loopback = "LOOPBACK" in flags.split(",")
            continue
        if current_name is None:
            continue
        stripped = line.strip()
        if stripped.startswith("inet "):
            parts = stripped.split()
            if len(parts) >= 2:
                current_ipv4.append(parts[1])
            continue
        if stripped == "status: active":
            current_active = True

    flush()
    return tuple(candidates)


def _interface_preference_key(candidate: RemoteInterfaceCandidate, target_ips: Iterable[str] = ()) -> tuple[int, int, int, int, int, int, int]:
    target_ip_set = {value for value in target_ips if value}
    non_loopback_ipv4 = tuple(addr for addr in candidate.ipv4_addrs if not _is_loopback_ipv4(addr))
    non_link_local_ipv4 = tuple(addr for addr in non_loopback_ipv4 if not _is_link_local_ipv4(addr))
    private_non_link_local_ipv4 = tuple(addr for addr in non_link_local_ipv4 if _is_private_ipv4(addr))
    bridge_bonus = 1 if candidate.name.startswith("bridge") else 0
    ethernet_bonus = 1 if candidate.name.startswith(("bcmeth", "gec", "en", "eth", "wm", "re")) else 0
    return (
        1 if target_ip_set.intersection(candidate.ipv4_addrs) else 0,
        1 if private_non_link_local_ipv4 else 0,
        1 if non_link_local_ipv4 else 0,
        1 if candidate.active else 0,
        1 if candidate.up else 0,
        bridge_bonus,
        ethernet_bonus,
    )


def preferred_interface_name(
    candidates: tuple[RemoteInterfaceCandidate, ...],
    *,
    target_ips: Iterable[str] = (),
) -> str | None:
    eligible = [candidate for candidate in candidates if not candidate.loopback and candidate.ipv4_addrs]
    if not eligible:
        return None
    best = max(eligible, key=lambda candidate: (_interface_preference_key(candidate, target_ips), candidate.name))
    return best.name


def probe_remote_interface_candidates(host: str, password: str, ssh_opts: str) -> RemoteInterfaceCandidatesProbeResult:
    return probe_remote_interface_candidates_conn(_conn(host, password, ssh_opts))


def probe_remote_interface_candidates_conn(connection: SshConnection) -> RemoteInterfaceCandidatesProbeResult:
    proc = run_ssh_conn(connection, "/sbin/ifconfig -a", check=False, timeout=30)
    if proc.returncode != 0:
        return RemoteInterfaceCandidatesProbeResult(
            candidates=(),
            preferred_iface=None,
            detail=f"ifconfig -a failed: rc={proc.returncode}",
        )
    candidates = _parse_ifconfig_candidates(proc.stdout)
    preferred_iface = preferred_interface_name(candidates)
    if preferred_iface is None:
        return RemoteInterfaceCandidatesProbeResult(
            candidates=candidates,
            preferred_iface=None,
            detail="no non-loopback IPv4 interface candidates found",
        )
    return RemoteInterfaceCandidatesProbeResult(
        candidates=candidates,
        preferred_iface=preferred_iface,
        detail=f"preferred interface {preferred_iface}",
    )


def read_interface_ipv4(host: str, password: str, ssh_opts: str, iface: str) -> str:
    return read_interface_ipv4_conn(_conn(host, password, ssh_opts), iface)


def read_interface_ipv4_conn(connection: SshConnection, iface: str) -> str:
    probe_cmd = (
        f"/sbin/ifconfig {shlex.quote(iface)} 2>/dev/null | "
        "sed -n 's/^[[:space:]]*inet[[:space:]]\\([0-9.]*\\).*/\\1/p' | "
        "sed -n '1p'"
    )
    proc = run_ssh_conn(
        connection,
        f"/bin/sh -c {shlex.quote(probe_cmd)}",
        check=False,
    )
    iface_ip = proc.stdout.strip()
    if not iface_ip:
        raise SystemExit(f"could not determine IPv4 for interface {iface}")
    return iface_ip


def read_active_smb_conf(host: str, password: str, ssh_opts: str) -> str:
    return read_active_smb_conf_conn(_conn(host, password, ssh_opts))


def read_active_smb_conf_conn(connection: SshConnection) -> str:
    quoted_conf = shlex.quote(RUNTIME_SMB_CONF)
    script = f"if [ -f {quoted_conf} ]; then cat {quoted_conf}; fi"
    proc = run_ssh_conn(
        connection,
        f"/bin/sh -c {shlex.quote(script)}",
        check=False,
    )
    return proc.stdout


def probe_managed_smbd(host: str, password: str, ssh_opts: str, *, timeout_seconds: int = 120) -> ManagedSmbdProbeResult:
    return probe_managed_smbd_conn(_conn(host, password, ssh_opts), timeout_seconds=timeout_seconds)


def probe_managed_smbd_conn(connection: SshConnection, *, timeout_seconds: int = 120) -> ManagedSmbdProbeResult:
    script = rf'''
{SMBD_STATUS_HELPERS}
if ! command -v fstat >/dev/null 2>&1; then
    echo "FAIL:fstat missing"
    exit 1
fi
attempt=0
max_attempts=$((({timeout_seconds} + 4) / 5))
while [ "$attempt" -lt "$max_attempts" ]; do
    out="$(fstat 2>&1)"
    if managed_smbd_ready 1 "$out"; then
        exit 0
    fi
    attempt=$((attempt + 1))
    sleep 5
done
describe_managed_smbd_status 1 "$out"
exit 1
'''
    proc = run_ssh_conn(
        connection,
        f"/bin/sh -c {shlex.quote(script)}",
        check=False,
        timeout=timeout_seconds + 30,
    )
    if proc.returncode == 0:
        return ManagedSmbdProbeResult(ready=True, detail="managed smbd ready")
    return ManagedSmbdProbeResult(ready=False, detail=proc.stdout.strip() or "managed smbd not ready")


def managed_smbd_ready(host: str, password: str, ssh_opts: str, *, timeout_seconds: int = 120) -> bool:
    return probe_managed_smbd(host, password, ssh_opts, timeout_seconds=timeout_seconds).ready


def probe_managed_mdns_takeover(host: str, password: str, ssh_opts: str, *, timeout_seconds: int = 20) -> ManagedMdnsTakeoverProbeResult:
    return probe_managed_mdns_takeover_conn(_conn(host, password, ssh_opts), timeout_seconds=timeout_seconds)


def probe_managed_mdns_takeover_conn(connection: SshConnection, *, timeout_seconds: int = 20) -> ManagedMdnsTakeoverProbeResult:
    script = r'''
mdns_ready=0
apple_alive=0

if /usr/bin/pkill -0 mdns-advertiser >/dev/null 2>&1; then
    mdns_ready=1
fi

ps_out="$(/bin/ps ax -o stat= -o ucomm= 2>/dev/null || true)"
while IFS= read -r line; do
    [ -n "$line" ] || continue
    stat_field="${line%% *}"
    ucomm_field="${line#* }"
    if [ "$ucomm_field" = "mDNSResponder" ] && [ "${stat_field#Z}" = "$stat_field" ]; then
        apple_alive=1
        break
    fi
done <<EOF
$ps_out
EOF

if [ "$mdns_ready" -eq 1 ] && [ "$apple_alive" -eq 0 ]; then
    exit 0
fi
exit 1
'''
    proc = run_ssh_conn(
        connection,
        f"/bin/sh -c {shlex.quote(script)}",
        check=False,
        timeout=timeout_seconds,
    )
    return_code = getattr(proc, "returncode", 0)
    if not isinstance(return_code, int):
        return ManagedMdnsTakeoverProbeResult(ready=True, detail="managed mDNS takeover probe returned non-integer status")
    if return_code == 0:
        return ManagedMdnsTakeoverProbeResult(ready=True, detail="managed mDNS takeover active")
    return ManagedMdnsTakeoverProbeResult(ready=False, detail=proc.stdout.strip() or "managed mDNS takeover not active")


def managed_mdns_takeover_ready(host: str, password: str, ssh_opts: str, *, timeout_seconds: int = 20) -> bool:
    return probe_managed_mdns_takeover(host, password, ssh_opts, timeout_seconds=timeout_seconds).ready


def nbns_marker_enabled(host: str, password: str, ssh_opts: str, payload_dir: str) -> bool:
    return nbns_marker_enabled_conn(_conn(host, password, ssh_opts), payload_dir)


def nbns_marker_enabled_conn(connection: SshConnection, payload_dir: str) -> bool:
    marker_path = f"{payload_dir}/private/nbns.enabled"
    quoted_marker = shlex.quote(marker_path)
    script = f"if [ -f {quoted_marker} ]; then echo enabled; fi"
    proc = run_ssh_conn(
        connection,
        f"/bin/sh -c {shlex.quote(script)}",
        check=False,
    )
    return proc.stdout.strip() == "enabled"


def netbsd4_runtime_services_healthy_conn(connection: SshConnection) -> bool:
    script = rf'''
{SMBD_STATUS_HELPERS}
if ! command -v fstat >/dev/null 2>&1; then
    exit 1
fi
out="$(fstat 2>&1)"
if ! managed_smbd_ready 0 "$out"; then
    exit 1
fi
mdns_bound_5353 "$out"
'''
    proc = run_ssh_conn(connection, f"/bin/sh -c {shlex.quote(script)}", check=False)
    return proc.returncode == 0


def probe_netbsd4_activation_status_conn(
    connection: SshConnection,
    *,
    timeout_seconds: int = 180,
) -> subprocess.CompletedProcess[str]:
    script = rf'''
{SMBD_STATUS_HELPERS}
if ! command -v fstat >/dev/null 2>&1; then
    echo "FAIL:fstat missing"
    exit 1
fi
attempt=0
max_attempts=$((({timeout_seconds} + 4) / 5))
while [ "$attempt" -lt "$max_attempts" ]; do
    out="$(fstat 2>&1)"
    if managed_smbd_ready 1 "$out" && mdns_bound_5353 "$out"; then
        break
    fi
    attempt=$((attempt + 1))
    sleep 5
done
echo "$out" | sed -n '/\.445/p;/\.5353/p'
status=0
if ! describe_managed_smbd_status 1 "$out"; then
    status=1
fi
if mdns_bound_5353 "$out"; then
    echo "PASS:mdns-advertiser bound to UDP 5353"
else
    echo "FAIL:mdns-advertiser is not bound to UDP 5353"
    status=1
fi
exit "$status"
'''
    return run_ssh_conn(
        connection,
        f"/bin/sh -c {shlex.quote(script)}",
        check=False,
        timeout=timeout_seconds + 30,
    )


def probe_paths_absent_conn(
    connection: SshConnection,
    paths: Iterable[str],
) -> subprocess.CompletedProcess[str]:
    script_lines = [
        "missing=0",
    ]
    for target in paths:
        quoted = shlex.quote(target)
        script_lines.append(f"if [ -e {quoted} ]; then echo PRESENT:{target}; missing=1; else echo ABSENT:{target}; fi")
    script_lines.append("exit \"$missing\"")
    return run_ssh_conn(connection, f"/bin/sh -c {shlex.quote('; '.join(script_lines))}", check=False)


def discover_volume_root(host: str, password: str, ssh_opts: str) -> str:
    return discover_volume_root_conn(_conn(host, password, ssh_opts))


def discover_volume_root_conn(connection: SshConnection) -> str:
    try:
        return discover_mounted_volume_conn(connection).mountpoint
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
    proc = run_ssh_conn(connection, f"/bin/sh -c {shlex.quote(script)}")
    lines = proc.stdout.strip().splitlines()
    volume = lines[-1].strip() if lines else ""
    if not volume:
        raise SystemExit("Failed to discover a Time Capsule volume root on the device.")
    return volume


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


def wait_for_ssh_state(
    host: str,
    password: str,
    ssh_opts: str,
    *,
    expected_up: bool,
    timeout_seconds: int = 180,
) -> bool:
    return wait_for_ssh_state_conn(_conn(host, password, ssh_opts), expected_up=expected_up, timeout_seconds=timeout_seconds)


def wait_for_ssh_state_conn(
    connection: SshConnection,
    *,
    expected_up: bool,
    timeout_seconds: int = 180,
) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            proc = run_ssh_conn(connection, "/bin/echo ok", check=False, timeout=10)
            is_up = proc.returncode == 0 and proc.stdout.strip().endswith("ok")
        except SystemExit:
            is_up = False
        if is_up == expected_up:
            return True
        time.sleep(5)
    return False
