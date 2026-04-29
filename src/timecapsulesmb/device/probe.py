from __future__ import annotations

import shlex
import subprocess
import time
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from timecapsulesmb.core.errors import system_exit_message
from timecapsulesmb.transport.local import tcp_open
from timecapsulesmb.transport.ssh import SshConnection, run_ssh, ssh_opts_use_proxy
from timecapsulesmb.core.config import AIRPORT_IDENTITIES_BY_MODEL, AIRPORT_IDENTITIES_BY_SYAP

if TYPE_CHECKING:
    from timecapsulesmb.device.compat import DeviceCompatibility


RUNTIME_SMB_CONF = "/mnt/Memory/samba4/etc/smb.conf"
SMBD_STATUS_HELPERS = rf'''
runtime_smb_conf_present() {{
    [ -f {RUNTIME_SMB_CONF} ]
}}

capture_ps_out() {{
    /bin/ps axww -o pid= -o ppid= -o stat= -o time= -o ucomm= -o command= 2>/dev/null || true
}}

smbd_parent_process_present() {{
    ps_out=$1
    smbd_pids=""
    while IFS= read -r line; do
        [ -n "$line" ] || continue
        set -- $line
        [ "$#" -ge 5 ] || continue
        if [ "$5" = "smbd" ]; then
            smbd_pids="$smbd_pids $1"
        fi
    done <<EOF
$ps_out
EOF

    while IFS= read -r line; do
        [ -n "$line" ] || continue
        set -- $line
        [ "$#" -ge 5 ] || continue
        if [ "$5" = "smbd" ]; then
            case " $smbd_pids " in
                *" $2 "*) ;;
                *) return 0 ;;
            esac
        fi
    done <<EOF
$ps_out
EOF
    return 1
}}

mdns_process_present() {{
    ps_out=$1
    while IFS= read -r line; do
        [ -n "$line" ] || continue
        set -- $line
        [ "$#" -ge 5 ] || continue
        if [ "$5" = "mdns-advertiser" ]; then
            return 0
        fi
    done <<EOF
$ps_out
EOF
    return 1
}}

apple_mdns_present() {{
    ps_out=$1
    while IFS= read -r line; do
        [ -n "$line" ] || continue
        set -- $line
        [ "$#" -ge 5 ] || continue
        if [ "$5" = "mDNSResponder" ]; then
            case "$3" in
                Z*) ;;
                *) return 0 ;;
            esac
        fi
    done <<EOF
$ps_out
EOF
    return 1
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
    ps_out=$1
    fstat_out=$2
    runtime_smb_conf_present || return 1
    smbd_parent_process_present "$ps_out" || return 1
    smbd_bound_445 "$fstat_out" || return 1
    return 0
}}

describe_managed_smbd_status() {{
    ps_out=$1
    fstat_out=$2
    status=0
    if runtime_smb_conf_present; then
        echo "PASS:managed runtime smb.conf present"
    else
        echo "FAIL:managed runtime smb.conf missing"
        status=1
    fi
    if smbd_parent_process_present "$ps_out"; then
        echo "PASS:managed smbd parent process is running"
    else
        echo "FAIL:managed smbd parent process is not running"
        status=1
    fi
    if smbd_bound_445 "$fstat_out"; then
        echo "PASS:smbd bound to TCP 445"
    else
        echo "FAIL:smbd is not bound to TCP 445"
        status=1
    fi
    return "$status"
}}

managed_mdns_takeover_ready() {{
    ps_out=$1
    fstat_out=$2
    mdns_process_present "$ps_out" || return 1
    mdns_bound_5353 "$fstat_out" || return 1
    apple_mdns_present "$ps_out" && return 1
    return 0
}}

describe_managed_mdns_status() {{
    ps_out=$1
    fstat_out=$2
    status=0
    if mdns_process_present "$ps_out"; then
        echo "PASS:mdns-advertiser process is running"
    else
        echo "FAIL:mdns-advertiser process is not running"
        status=1
    fi
    if mdns_bound_5353 "$fstat_out"; then
        echo "PASS:mdns-advertiser bound to UDP 5353"
    else
        echo "FAIL:mdns-advertiser is not bound to UDP 5353"
        status=1
    fi
    if apple_mdns_present "$ps_out"; then
        echo "FAIL:Apple mDNSResponder is still running"
        status=1
    else
        echo "PASS:Apple mDNSResponder is stopped"
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
  metadata_wedges=$(echo "$dmesg_disk_lines" | /usr/bin/sed -n 's/^\(dk[0-9][0-9]*\) at .*: APconfig$/\1/p;s/^\(dk[0-9][0-9]*\) at .*: APswap$/\1/p')

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
    lines: tuple[str, ...] = ()


@dataclass(frozen=True)
class ManagedMdnsTakeoverProbeResult:
    ready: bool
    detail: str
    lines: tuple[str, ...] = ()


@dataclass(frozen=True)
class ManagedRuntimeProbeResult:
    ready: bool
    detail: str
    smbd: ManagedSmbdProbeResult
    mdns: ManagedMdnsTakeoverProbeResult
    lines: tuple[str, ...] = ()


@dataclass(frozen=True)
class AirportIdentityProbeResult:
    model: str | None
    syap: str | None
    detail: str


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
            error=system_exit_message(exc) or "SSH authentication failed.",
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


def probe_ssh_command_conn(
    connection: SshConnection,
    command: str,
    *,
    timeout: int = 30,
    expected_stdout_suffix: str | None = None,
) -> SshCommandProbeResult:
    try:
        proc = run_ssh(connection, command, check=False, timeout=timeout)
    except SystemExit as exc:
        return SshCommandProbeResult(ok=False, detail=system_exit_message(exc))
    if proc.returncode == 0:
        stdout = proc.stdout.strip()
        if expected_stdout_suffix is None or stdout.endswith(expected_stdout_suffix):
            return SshCommandProbeResult(ok=True, detail=stdout)
    detail = proc.stdout.strip() or f"rc={proc.returncode}"
    return SshCommandProbeResult(ok=False, detail=detail)


def _probe_remote_os_info_conn(connection: SshConnection) -> tuple[str, str, str]:
    script = "printf '%s\\n%s\\n%s\\n' \"$(uname -s)\" \"$(uname -r)\" \"$(uname -m)\""
    proc = run_ssh(connection, f"/bin/sh -c {shlex.quote(script)}")
    lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    if len(lines) < 3:
        raise SystemExit("Failed to determine remote device OS compatibility.")
    return lines[0], lines[1], lines[2]


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
    proc = run_ssh(connection, f"/bin/sh -c {shlex.quote(script)}", check=False)
    endianness = (proc.stdout or "").strip().splitlines()
    value = endianness[-1].strip() if endianness else ""
    if value in {"little", "big", "unknown"}:
        return value
    return "unknown"


def extract_airport_identity_from_text(text: str) -> AirportIdentityProbeResult:
    for model, identity in AIRPORT_IDENTITIES_BY_MODEL.items():
        if model in text:
            return AirportIdentityProbeResult(model=model, syap=identity.syap, detail=f"found AirPort model {model}")
    return AirportIdentityProbeResult(model=None, syap=None, detail="no supported AirPort model found")


def _parse_airport_syap_value(value: str) -> str | None:
    stripped = value.strip()
    if not stripped:
        return None
    try:
        syap = int(stripped, 0)
    except ValueError:
        return None
    return str(syap)


def _extract_airport_syap_from_acp_output(text: str) -> tuple[str | None, str | None]:
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = re.match(r"^syAP\s*=\s*(\S+)", line)
        if match:
            parsed = _parse_airport_syap_value(match.group(1))
            if parsed is None:
                return None, f"AirPort identity syAP was not parseable: {match.group(1)}"
            return parsed, None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or any(char.isalpha() for char in line.replace("x", "").replace("X", "")):
            continue
        parsed = _parse_airport_syap_value(line)
        if parsed is not None:
            return parsed, None

    return None, None


def extract_airport_identity_from_acp_output(text: str) -> AirportIdentityProbeResult:
    model_result = extract_airport_identity_from_text(text)
    syap, syap_error = _extract_airport_syap_from_acp_output(text)
    if syap_error is not None and model_result.model is None:
        return AirportIdentityProbeResult(model=None, syap=None, detail=syap_error)

    if model_result.model is not None:
        expected_syap = model_result.syap
        if syap is not None and syap != expected_syap:
            return AirportIdentityProbeResult(
                model=None,
                syap=None,
                detail=f"AirPort identity mismatch: syAM {model_result.model} expects syAP {expected_syap}, got {syap}",
            )
        if syap_error is not None:
            return AirportIdentityProbeResult(
                model=model_result.model,
                syap=model_result.syap,
                detail=f"{model_result.detail}; {syap_error}",
            )
        return model_result

    if syap is not None:
        identity = AIRPORT_IDENTITIES_BY_SYAP.get(syap)
        if identity is None:
            return AirportIdentityProbeResult(model=None, syap=None, detail=f"unsupported AirPort syAP {syap}")
        return AirportIdentityProbeResult(
            model=identity.mdns_model,
            syap=identity.syap,
            detail=f"found AirPort syAP {identity.syap}",
        )

    if syap_error is not None:
        return AirportIdentityProbeResult(model=None, syap=None, detail=syap_error)
    return AirportIdentityProbeResult(model=None, syap=None, detail="no supported AirPort identity found")


def probe_remote_airport_identity_conn(connection: SshConnection) -> AirportIdentityProbeResult:
    script = r"""
if [ ! -x /usr/bin/acp ]; then
  exit 0
fi
/usr/bin/acp syAP syAM 2>/dev/null
"""
    proc = run_ssh(connection, f"/bin/sh -c {shlex.quote(script)}", check=False, timeout=30)
    if proc.returncode != 0:
        return AirportIdentityProbeResult(model=None, syap=None, detail=f"could not read AirPort identity: rc={proc.returncode}")
    if not proc.stdout:
        return AirportIdentityProbeResult(model=None, syap=None, detail="AirPort identity unavailable: /usr/bin/acp missing or empty output")
    return extract_airport_identity_from_acp_output(proc.stdout)


def discover_mounted_volume_conn(connection: SshConnection) -> MountedVolume:
    script = DISK_NAME_CANDIDATES_SH + r'''
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
    proc = run_ssh(connection, f"/bin/sh -c {shlex.quote(script)}", check=False)
    lines = proc.stdout.strip().splitlines()
    result = lines[-1].strip() if lines else ""
    if proc.returncode != 0 or not result:
        raise SystemExit("Failed to discover a mounted AirPort HFS data volume on the device.")
    device, mountpoint = result.split(" ", 1)
    return MountedVolume(device=device, mountpoint=mountpoint)


def probe_remote_interface_conn(connection: SshConnection, iface: str) -> RemoteInterfaceProbeResult:
    script = f"/sbin/ifconfig {shlex.quote(iface)} >/dev/null 2>&1"
    proc = run_ssh(connection, f"/bin/sh -c {shlex.quote(script)}", check=False)
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


def probe_remote_interface_candidates_conn(connection: SshConnection) -> RemoteInterfaceCandidatesProbeResult:
    proc = run_ssh(connection, "/sbin/ifconfig -a", check=False, timeout=30)
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


def read_interface_ipv4_conn(connection: SshConnection, iface: str) -> str:
    probe_cmd = (
        f"/sbin/ifconfig {shlex.quote(iface)} 2>/dev/null | "
        "sed -n 's/^[[:space:]]*inet[[:space:]]\\([0-9.]*\\).*/\\1/p' | "
        "sed -n '1p'"
    )
    proc = run_ssh(
        connection,
        f"/bin/sh -c {shlex.quote(probe_cmd)}",
        check=False,
    )
    iface_ip = proc.stdout.strip()
    if not iface_ip:
        raise SystemExit(f"could not determine IPv4 for interface {iface}")
    return iface_ip


def read_active_smb_conf_conn(connection: SshConnection) -> str:
    quoted_conf = shlex.quote(RUNTIME_SMB_CONF)
    script = f"if [ -f {quoted_conf} ]; then cat {quoted_conf}; fi"
    proc = run_ssh(
        connection,
        f"/bin/sh -c {shlex.quote(script)}",
        check=False,
    )
    return proc.stdout


def _probe_lines(stdout: str) -> tuple[str, ...]:
    return tuple(line.strip() for line in stdout.splitlines() if line.strip())


def _probe_detail(lines: tuple[str, ...], default: str) -> str:
    failures = [line.removeprefix("FAIL:") for line in lines if line.startswith("FAIL:")]
    if failures:
        return "; ".join(failures)
    passes = [line.removeprefix("PASS:") for line in lines if line.startswith("PASS:")]
    if passes:
        return "; ".join(passes)
    return default


def probe_managed_smbd_conn(connection: SshConnection, *, timeout_seconds: int = 20) -> ManagedSmbdProbeResult:
    script = rf'''
{SMBD_STATUS_HELPERS}
if ! command -v fstat >/dev/null 2>&1; then
    echo "FAIL:fstat missing"
    exit 1
fi
ps_out="$(capture_ps_out)"
out="$(fstat 2>&1)"
status=0
if ! describe_managed_smbd_status "$ps_out" "$out"; then
    status=1
fi
exit "$status"
'''
    proc = run_ssh(
        connection,
        f"/bin/sh -c {shlex.quote(script)}",
        check=False,
        timeout=timeout_seconds,
    )
    lines = _probe_lines(proc.stdout)
    if proc.returncode == 0:
        return ManagedSmbdProbeResult(ready=True, detail=_probe_detail(lines, "managed smbd ready"), lines=lines)
    return ManagedSmbdProbeResult(ready=False, detail=_probe_detail(lines, "managed smbd not ready"), lines=lines)


def probe_managed_mdns_takeover_conn(connection: SshConnection, *, timeout_seconds: int = 20) -> ManagedMdnsTakeoverProbeResult:
    script = rf'''
{SMBD_STATUS_HELPERS}
if ! command -v fstat >/dev/null 2>&1; then
    echo "FAIL:fstat missing"
    exit 1
fi
ps_out="$(capture_ps_out)"
out="$(fstat 2>&1)"
status=0
if ! describe_managed_mdns_status "$ps_out" "$out"; then
    status=1
fi
exit "$status"
'''
    proc = run_ssh(
        connection,
        f"/bin/sh -c {shlex.quote(script)}",
        check=False,
        timeout=timeout_seconds,
    )
    lines = _probe_lines(proc.stdout)
    return_code = getattr(proc, "returncode", 0)
    if not isinstance(return_code, int):
        return ManagedMdnsTakeoverProbeResult(
            ready=True,
            detail="managed mDNS takeover probe returned non-integer status",
            lines=lines,
        )
    if return_code == 0:
        return ManagedMdnsTakeoverProbeResult(
            ready=True,
            detail=_probe_detail(lines, "managed mDNS takeover active"),
            lines=lines,
        )
    return ManagedMdnsTakeoverProbeResult(
        ready=False,
        detail=_probe_detail(lines, "managed mDNS takeover not active"),
        lines=lines,
    )


def probe_managed_runtime_conn(
    connection: SshConnection,
    *,
    timeout_seconds: int = 120,
    poll_interval_seconds: float = 5.0,
    smbd_mdns_stagger_seconds: float = 1.0,
    mdns_settle_seconds: float = 3.0,
) -> ManagedRuntimeProbeResult:
    deadline = time.monotonic() + timeout_seconds
    last_smbd = ManagedSmbdProbeResult(ready=False, detail="managed smbd not ready")
    last_mdns = ManagedMdnsTakeoverProbeResult(ready=False, detail="managed mDNS takeover not active")
    smbd_ready = False
    mdns_ready = False

    while time.monotonic() < deadline:
        iteration_start = time.monotonic()
        if not smbd_ready:
            last_smbd = probe_managed_smbd_conn(connection, timeout_seconds=20)
            smbd_ready = last_smbd.ready
        
        if not mdns_ready:
            if not smbd_ready:
                time.sleep(smbd_mdns_stagger_seconds)
            last_mdns = probe_managed_mdns_takeover_conn(connection, timeout_seconds=20)
            mdns_ready = last_mdns.ready

        if smbd_ready and mdns_ready:
            time.sleep(mdns_settle_seconds)
            settled_mdns = probe_managed_mdns_takeover_conn(connection, timeout_seconds=20)
            if settled_mdns.ready:
                lines = last_smbd.lines + settled_mdns.lines + ("PASS:mdns-advertiser remained healthy after settle delay",)
                return ManagedRuntimeProbeResult(
                    ready=True,
                    detail="managed runtime is ready",
                    smbd=last_smbd,
                    mdns=settled_mdns,
                    lines=lines,
                )
            last_mdns = ManagedMdnsTakeoverProbeResult(
                ready=False,
                detail=f"{settled_mdns.detail}; mdns-advertiser did not survive settle delay",
                lines=settled_mdns.lines + ("FAIL:mdns-advertiser did not remain healthy after settle delay",),
            )
            mdns_ready = False

        elapsed = time.monotonic() - iteration_start
        sleep_for = max(0.0, poll_interval_seconds - elapsed)
        if sleep_for > 0:
            time.sleep(sleep_for)

    lines = last_smbd.lines + last_mdns.lines
    return ManagedRuntimeProbeResult(
        ready=False,
        detail=f"{last_smbd.detail}; {last_mdns.detail}",
        smbd=last_smbd,
        mdns=last_mdns,
        lines=lines,
    )


def nbns_marker_enabled_conn(connection: SshConnection, payload_dir: str) -> bool:
    marker_path = f"{payload_dir}/private/nbns.enabled"
    quoted_marker = shlex.quote(marker_path)
    script = f"if [ -f {quoted_marker} ]; then echo enabled; fi"
    proc = run_ssh(
        connection,
        f"/bin/sh -c {shlex.quote(script)}",
        check=False,
    )
    return proc.stdout.strip() == "enabled"


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
    return run_ssh(connection, f"/bin/sh -c {shlex.quote('; '.join(script_lines))}", check=False)


def discover_volume_root_conn(connection: SshConnection) -> str:
    try:
        return discover_mounted_volume_conn(connection).mountpoint
    except SystemExit:
        pass

    script = DISK_NAME_CANDIDATES_SH + r'''
for dev in $(disk_name_candidates); do
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
    proc = run_ssh(connection, f"/bin/sh -c {shlex.quote(script)}")
    lines = proc.stdout.strip().splitlines()
    volume = lines[-1].strip() if lines else ""
    if not volume:
        raise SystemExit("Failed to discover an AirPort volume root on the device.")
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


def wait_for_ssh_state_conn(
    connection: SshConnection,
    *,
    expected_up: bool,
    timeout_seconds: int = 180,
) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            proc = run_ssh(connection, "/bin/echo ok", check=False, timeout=10)
            is_up = proc.returncode == 0 and proc.stdout.strip().endswith("ok")
        except SystemExit:
            is_up = False
        if is_up == expected_up:
            return True
        time.sleep(5)
    return False
