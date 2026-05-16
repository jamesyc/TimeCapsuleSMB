from __future__ import annotations

import shlex
import subprocess
import time
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from timecapsulesmb.device.compat import compatibility_from_probe_result
from timecapsulesmb.device.errors import DeviceError
from timecapsulesmb.device.processes import PROBE_PROCESS_HELPERS
from timecapsulesmb.transport.local import tcp_open
from timecapsulesmb.transport.errors import TransportError
from timecapsulesmb.transport.ssh import SshCommandTimeout, SshConnection, run_ssh, ssh_opts_use_proxy
from timecapsulesmb.core.config import (
    AIRPORT_IDENTITIES_BY_MODEL,
    AIRPORT_IDENTITIES_BY_SYAP,
    MAX_DNS_LABEL_BYTES,
    MAX_NETBIOS_NAME_BYTES,
)
from timecapsulesmb.core.net import extract_host, is_link_local_ipv4, is_loopback_ipv4

if TYPE_CHECKING:
    from timecapsulesmb.device.compat import DeviceCompatibility


RUNTIME_SMB_CONF = "/mnt/Memory/samba4/etc/smb.conf"
RUNTIME_SHARES_TSV = "/mnt/Memory/samba4/var/shares.tsv"
RUNTIME_PAYLOAD_TSV = "/mnt/Memory/samba4/var/payload.tsv"
FLASH_RUNTIME_CONFIG = "/mnt/Flash/tcapsulesmb.conf"
REMOTE_STATE_PROBE_TIMEOUT_SECONDS = 10
REMOTE_LOG_TAIL_LINES = 80
REMOTE_LOG_TAIL_MAX_CHARS = 8192
REMOTE_LOG_TAIL_TIMEOUT_SECONDS = 10
REMOTE_NETWORK_DIAGNOSTICS_TIMEOUT_SECONDS = 10
REMOTE_RUNTIME_RAM_LOG_PATHS = {
    "remote_rc_local_log_tail": "/mnt/Memory/samba4/var/rc.local.log",
    "remote_watchdog_log_tail": "/mnt/Memory/samba4/var/watchdog.log",
}
REMOTE_PAYLOAD_LOG_FILENAMES = {
    "remote_smbd_log_tail": "log.smbd",
    "remote_mdns_log_tail": "mdns.log",
    "remote_nbns_log_tail": "nbns.log",
}
REMOTE_RUNTIME_FALLBACK_LOG_PATHS = {
    "remote_mdns_log_tail": "/mnt/Memory/samba4/var/mdns.log",
    "remote_nbns_log_tail": "/mnt/Memory/samba4/var/nbns.log",
}
SMBD_STATUS_HELPERS = rf'''
    RUNTIME_RAM_ROOT=${{RUNTIME_RAM_ROOT:-/mnt/Memory/samba4}}
    RUNTIME_RAM_SBIN="$RUNTIME_RAM_ROOT/sbin"
    RUNTIME_RAM_PRIVATE="$RUNTIME_RAM_ROOT/private"
    RUNTIME_MDNS_BIN=${{RUNTIME_MDNS_BIN:-/mnt/Flash/mdns-advertiser}}
    RUNTIME_SMB_CONF_PATH=${{RUNTIME_SMB_CONF_PATH:-{RUNTIME_SMB_CONF}}}
RUNTIME_SHARES_TSV_PATH=${{RUNTIME_SHARES_TSV_PATH:-{RUNTIME_SHARES_TSV}}}
RUNTIME_PERSISTENT_ROOT_PREFIX=${{RUNTIME_PERSISTENT_ROOT_PREFIX:-/Volumes/}}
RUNTIME_TAB=$(printf '\t')

runtime_smb_conf_present() {{
    [ -f "$RUNTIME_SMB_CONF_PATH" ]
}}

runtime_smbd_binary_present() {{
    [ -x "$RUNTIME_RAM_SBIN/smbd" ]
}}

read_smb_conf_value() {{
    key=$1
    if ! runtime_smb_conf_present; then
        return 1
    fi
    /usr/bin/sed -n "s/^[[:space:]]*$key[[:space:]]*=[[:space:]]*//p" "$RUNTIME_SMB_CONF_PATH" | /usr/bin/sed -n '1p'
}}

runtime_passdb_path() {{
    passdb_backend=$(read_smb_conf_value "passdb backend" || true)
    case "$passdb_backend" in
        smbpasswd:*)
            printf '%s\n' "${{passdb_backend#smbpasswd:}}"
            return 0
            ;;
    esac
    return 1
}}

runtime_username_map_path() {{
    read_smb_conf_value "username map"
}}

runtime_xattr_tdb_path() {{
    read_smb_conf_value "xattr_tdb:file"
}}

runtime_data_root_path() {{
    read_smb_conf_value "path"
}}

runtime_volume_root() {{
    data_root=$(runtime_data_root_path || true)
    case "$data_root" in
        "$RUNTIME_PERSISTENT_ROOT_PREFIX"*)
            rest=${{data_root#"$RUNTIME_PERSISTENT_ROOT_PREFIX"}}
            device_name=${{rest%%/*}}
            if [ -n "$device_name" ]; then
                printf '%s%s\n' "$RUNTIME_PERSISTENT_ROOT_PREFIX" "$device_name"
                return 0
            fi
            ;;
    esac
    return 1
}}

runtime_volume_device() {{
    volume_root=$(runtime_volume_root || true)
    if [ -n "$volume_root" ]; then
        printf '/dev/%s\n' "${{volume_root##*/}}"
        return 0
    fi
    return 1
}}

runtime_share_volume_roots() {{
    seen_roots=""
    if [ -s "$RUNTIME_SHARES_TSV_PATH" ]; then
        while IFS="$RUNTIME_TAB" read -r share_name share_path part_device builtin part_uuid; do
            [ -n "$part_device" ] || continue
            volume_root="$RUNTIME_PERSISTENT_ROOT_PREFIX$part_device"
            case " $seen_roots " in
                *" $volume_root "*) ;;
                *)
                    seen_roots="$seen_roots $volume_root"
                    printf '%s\n' "$volume_root"
                    ;;
            esac
        done <"$RUNTIME_SHARES_TSV_PATH"
        if [ -n "$seen_roots" ]; then
            return 0
        fi
    fi
    runtime_volume_root
}}

capture_df_for_volume_root() {{
    volume_root=$1
    /bin/df -k "$volume_root" 2>/dev/null | /usr/bin/tail -n +2 || true
}}

runtime_volume_mounted() {{
    volume_root=$(runtime_volume_root || true)
    if [ -z "$volume_root" ]; then
        return 1
    fi
    df_line=$(capture_df_for_volume_root "$volume_root")
    case "$df_line" in
        *" $volume_root")
            return 0
            ;;
    esac
    return 1
}}

runtime_share_volumes_mounted() {{
    found=0
    status=0
    for volume_root in $(runtime_share_volume_roots); do
        found=1
        df_line=$(capture_df_for_volume_root "$volume_root")
        case "$df_line" in
            *" $volume_root") ;;
            *) status=1 ;;
        esac
    done
    [ "$found" -eq 1 ] || return 1
    return "$status"
}}

{PROBE_PROCESS_HELPERS}

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

describe_managed_smbd_status() {{
    ps_out=$1
    fstat_out=$2
    status=0
    if runtime_smbd_binary_present; then
        echo "PASS:managed runtime smbd binary present"
    else
        echo "FAIL:managed runtime smbd binary missing"
        status=1
    fi
    if runtime_smb_conf_present; then
        echo "PASS:managed runtime smb.conf present"
    else
        echo "FAIL:managed runtime smb.conf missing"
        status=1
    fi
    passdb_path=$(runtime_passdb_path || true)
    if [ "$passdb_path" = "$RUNTIME_RAM_PRIVATE/smbpasswd" ] && [ -f "$passdb_path" ]; then
        echo "PASS:active smb.conf passdb backend uses RAM smbpasswd"
    else
        echo "FAIL:active smb.conf passdb backend is not staged in RAM"
        status=1
    fi
    username_map_path=$(runtime_username_map_path || true)
    if [ "$username_map_path" = "$RUNTIME_RAM_PRIVATE/username.map" ] && [ -f "$username_map_path" ]; then
        echo "PASS:active smb.conf username map uses RAM username.map"
    else
        echo "FAIL:active smb.conf username map is not staged in RAM"
        status=1
    fi
    xattr_tdb_path=$(runtime_xattr_tdb_path || true)
    case "$xattr_tdb_path" in
        "$RUNTIME_PERSISTENT_ROOT_PREFIX"*)
            xattr_tdb_parent=${{xattr_tdb_path%/*}}
            if [ -d "$xattr_tdb_parent" ]; then
                echo "PASS:active smb.conf xattr_tdb:file is persistent"
            else
                echo "FAIL:active smb.conf xattr_tdb:file parent is missing"
                status=1
            fi
            ;;
        *)
            echo "FAIL:active smb.conf xattr_tdb:file is not persistent disk storage"
            status=1
            ;;
    esac
    if runtime_share_volumes_mounted; then
        echo "PASS:all managed share volumes are mounted"
    else
        echo "FAIL:one or more managed share volumes are not mounted"
        status=1
    fi
    if watchdog_process_present_for_volume "$ps_out"; then
        echo "PASS:watchdog is running for managed runtime"
    else
        echo "FAIL:watchdog is not running for managed runtime"
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

describe_managed_mdns_status() {{
    ps_out=$1
    fstat_out=$2
    status=0
    mdns_auto_ip_state=waiting
    mdns_auto_ip_failure=
    if [ ! -e "$RUNTIME_MDNS_BIN" ]; then
        mdns_auto_ip_state=failed
        mdns_auto_ip_failure="mdns-advertiser binary missing at $RUNTIME_MDNS_BIN"
    elif [ ! -x "$RUNTIME_MDNS_BIN" ]; then
        mdns_auto_ip_state=failed
        mdns_auto_ip_failure="mdns-advertiser binary is not executable at $RUNTIME_MDNS_BIN"
    else
        "$RUNTIME_MDNS_BIN" --check-auto-ip >/dev/null 2>&1
        mdns_auto_ip_rc=$?
        case "$mdns_auto_ip_rc" in
            0) mdns_auto_ip_state=active ;;
            11) mdns_auto_ip_state=waiting ;;
            *)
                mdns_auto_ip_state=failed
                mdns_auto_ip_failure="mdns-advertiser auto-IP check failed with exit code $mdns_auto_ip_rc"
                ;;
        esac
    fi

    if [ "$mdns_auto_ip_state" = "failed" ]; then
        echo "FAIL:$mdns_auto_ip_failure"
        status=1
    fi

    if mdns_process_present "$ps_out"; then
        echo "PASS:mdns-advertiser process is running"
    else
        if [ "$mdns_auto_ip_state" = "waiting" ]; then
            echo "FAIL:mDNS startup deferred; no usable IPv4 has appeared yet"
        else
            echo "FAIL:mdns-advertiser process is not running"
        fi
        status=1
    fi
    if mdns_bound_5353 "$fstat_out"; then
        echo "PASS:mdns-advertiser bound to UDP 5353"
        if [ "$mdns_auto_ip_state" = "active" ]; then
            echo "PASS:mdns-advertiser auto-IP active"
        else
            echo "FAIL:mdns-advertiser bound to UDP 5353 but auto-IP is not active"
            status=1
        fi
    else
        if mdns_process_present "$ps_out" && [ "$mdns_auto_ip_state" = "waiting" ]; then
            echo "FAIL:mdns-advertiser is waiting for auto-IP"
            status=1
        else
            echo "FAIL:mdns-advertiser is not bound to UDP 5353"
            status=1
        fi
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
    target_ip_matches: tuple[RemoteInterfaceCandidate, ...] = ()


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


@dataclass(frozen=True)
class RuntimeNamingIdentityProbeResult:
    system_name: str | None
    hostname: str | None
    mdns_instance_name: str
    mdns_host_label: str
    netbios_name: str
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
    except (TransportError, DeviceError) as exc:
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


def probe_connection_state(connection: SshConnection) -> ProbedDeviceState:
    probe_result = probe_device_conn(connection)
    compatibility = compatibility_from_probe_result(probe_result)
    return ProbedDeviceState(probe_result=probe_result, compatibility=compatibility)


def probe_ssh_command_conn(
    connection: SshConnection,
    command: str,
    *,
    timeout: int = 30,
    expected_stdout_suffix: str | None = None,
) -> SshCommandProbeResult:
    try:
        proc = run_ssh(connection, command, check=False, timeout=timeout)
    except TransportError as exc:
        return SshCommandProbeResult(ok=False, detail=str(exc))
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
        raise DeviceError("Failed to determine remote device OS compatibility.")
    # SSH client warnings from user config can be emitted before command stdout.
    # The probe command's own output is the trailing uname triplet.
    return lines[-3], lines[-2], lines[-1]


def _probe_remote_elf_endianness_conn(connection: SshConnection, path: str = "/bin/sh") -> str:
    script = rf"""
path={shlex.quote(path)}
if [ ! -f "$path" ]; then
  exit 1
fi
b5=$(/bin/dd if="$path" bs=1 skip=5 count=1 2>/dev/null | /usr/bin/sed -n l 2>/dev/null)
case "$b5" in
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
    if not re.fullmatch(r"(?:0[xX][0-9A-Fa-f]+|[0-9]+)", stripped):
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
        if not re.fullmatch(r"(?:0[xX][0-9A-Fa-f]+|[0-9]+)", line):
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


def _truncate_utf8(value: str, max_bytes: int) -> str:
    output: list[str] = []
    used = 0
    for char in value:
        char_len = len(char.encode("utf-8"))
        if used + char_len > max_bytes:
            break
        output.append(char)
        used += char_len
    return "".join(output)


def _first_dns_label(value: str) -> str:
    return value.strip().split(".", 1)[0].strip()


def normalize_runtime_mdns_instance_name(value: str) -> str:
    normalized = "".join("-" if char == "." or ord(char) < 0x20 or ord(char) == 0x7F else char for char in value)
    return _truncate_utf8(normalized.strip(), MAX_DNS_LABEL_BYTES)


def normalize_runtime_mdns_host_label(value: str) -> str:
    candidate = _first_dns_label(value).lower()
    normalized = re.sub(r"[^a-z0-9-]", "-", candidate).strip("-")
    normalized = _truncate_utf8(normalized, MAX_DNS_LABEL_BYTES)
    return normalized.strip("-")


def normalize_runtime_netbios_name(value: str) -> str:
    candidate = _first_dns_label(value)
    normalized = re.sub(r"[^A-Za-z0-9_-]", "", candidate)
    if not re.search(r"[A-Za-z0-9]", normalized):
        return ""
    return _truncate_utf8(normalized, MAX_NETBIOS_NAME_BYTES)


def derive_runtime_naming_identity(system_name: str | None, hostname: str | None) -> RuntimeNamingIdentityProbeResult:
    raw_system_name = (system_name or "").strip() or None
    raw_hostname = (hostname or "").strip() or None

    mdns_host_label = normalize_runtime_mdns_host_label(raw_hostname or "")
    if not mdns_host_label:
        mdns_host_label = normalize_runtime_mdns_host_label(raw_system_name or "")
    if not mdns_host_label:
        mdns_host_label = "timecapsule"

    mdns_instance_name = normalize_runtime_mdns_instance_name(raw_system_name or "")
    if not mdns_instance_name:
        mdns_instance_name = mdns_host_label

    netbios_name = normalize_runtime_netbios_name(raw_hostname or "")
    if not netbios_name:
        netbios_name = normalize_runtime_netbios_name(raw_system_name or "")
    if not netbios_name:
        netbios_name = "TimeCapsule"

    return RuntimeNamingIdentityProbeResult(
        system_name=raw_system_name,
        hostname=raw_hostname,
        mdns_instance_name=mdns_instance_name,
        mdns_host_label=mdns_host_label,
        netbios_name=netbios_name,
        detail=(
            "derived runtime naming identity: "
            f"mdns_instance={mdns_instance_name} mdns_host={mdns_host_label} netbios={netbios_name}"
        ),
    )


def _parse_runtime_naming_probe_output(text: str) -> RuntimeNamingIdentityProbeResult:
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        key, separator, value = raw_line.partition("=")
        if separator:
            values[key.strip()] = value.strip()
    return derive_runtime_naming_identity(values.get("system_name"), values.get("hostname"))


def probe_remote_runtime_naming_identity_conn(connection: SshConnection) -> RuntimeNamingIdentityProbeResult:
    script = r"""
system_name=
if [ -x /usr/bin/acp ]; then
  system_name=$(/usr/bin/acp -q syNm 2>/dev/null | /usr/bin/sed -n '1p')
fi
hostname=$(/bin/hostname 2>/dev/null | /usr/bin/sed -n '1p')
printf 'system_name=%s\n' "$system_name"
printf 'hostname=%s\n' "$hostname"
"""
    proc = run_ssh(connection, f"/bin/sh -c {shlex.quote(script)}", check=False, timeout=30)
    if getattr(proc, "returncode", 0) != 0:
        raise RuntimeError(f"could not read runtime naming identity: rc={proc.returncode}")
    return _parse_runtime_naming_probe_output(proc.stdout or "")


def probe_remote_interface_conn(connection: SshConnection, iface: str) -> RemoteInterfaceProbeResult:
    script = f"/sbin/ifconfig {shlex.quote(iface)} >/dev/null 2>&1"
    proc = run_ssh(connection, f"/bin/sh -c {shlex.quote(script)}", check=False)
    if proc.returncode == 0:
        return RemoteInterfaceProbeResult(iface=iface, exists=True, detail=f"interface {iface} exists")
    return RemoteInterfaceProbeResult(iface=iface, exists=False, detail=f"interface {iface} was not found on the device")


def is_runtime_usable_ipv4(value: str) -> bool:
    return (
        bool(value)
        and not re.search(r"[^0-9.]", value)
        and value != "0.0.0.0"
        and not is_loopback_ipv4(value)
        and not is_link_local_ipv4(value)
    )


def runtime_usable_ipv4s(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(value for value in values if is_runtime_usable_ipv4(value))


def runtime_usable_ipv4_addrs(values: Iterable[str]) -> tuple[str, ...]:
    return runtime_usable_ipv4s(values)


def runtime_interface_candidates(
    candidates: Iterable[RemoteInterfaceCandidate],
) -> tuple[RemoteInterfaceCandidate, ...]:
    return tuple(
        candidate
        for candidate in candidates
        if not candidate.loopback and runtime_usable_ipv4s(candidate.ipv4_addrs)
    )


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
            parts = _ifconfig_inet_parts(stripped)
            if len(parts) >= 2:
                current_ipv4.append(parts[1])
            continue
        if stripped == "status: active":
            current_active = True

    flush()
    return tuple(candidates)


def _ifconfig_inet_parts(stripped: str) -> list[str]:
    parts = stripped.split()
    if len(parts) >= 2 and parts[1] == "alias":
        return [parts[0], *parts[2:]]
    return parts


def _interface_preference_key(candidate: RemoteInterfaceCandidate, target_ips: Iterable[str] = ()) -> tuple[int, int, int, int, int, int, int]:
    target_ip_tuple = tuple(value for value in target_ips if value)
    target_ip_set = set(target_ip_tuple)
    non_loopback_ipv4 = tuple(addr for addr in candidate.ipv4_addrs if not is_loopback_ipv4(addr))
    non_link_local_ipv4 = tuple(addr for addr in non_loopback_ipv4 if not is_link_local_ipv4(addr))
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
    eligible = [
        candidate
        for candidate in candidates
        if not candidate.loopback and runtime_usable_ipv4s(candidate.ipv4_addrs)
    ]
    if not eligible:
        return None
    target_ip_tuple = runtime_usable_ipv4s(target_ips)
    best = max(eligible, key=lambda candidate: (_interface_preference_key(candidate, target_ip_tuple), candidate.name))
    return best.name


def _parse_netmask_value(value: str) -> int | None:
    value = value.strip().lower()
    if not value:
        return None
    try:
        if value.startswith("0x"):
            parsed = int(value, 16)
        elif "." in value:
            octets = value.split(".")
            if len(octets) != 4:
                return None
            parsed = 0
            for octet_text in octets:
                if not octet_text.isdigit():
                    return None
                octet = int(octet_text, 10)
                if octet < 0 or octet > 255:
                    return None
                parsed = (parsed << 8) | octet
        else:
            parsed = int(value, 10)
    except ValueError:
        return None
    if parsed < 0 or parsed > 0xFFFFFFFF:
        return None
    return parsed


def _netmask_to_prefix(value: str) -> int | None:
    parsed = _parse_netmask_value(value)
    if parsed is None:
        return None
    bits = f"{parsed:032b}"
    if "01" in bits:
        return None
    return bits.count("1")


def runtime_ipv4_cidr_from_ifconfig(output: str, hint_ip: str = "") -> str | None:
    selected: tuple[str, int] | None = None
    hinted: tuple[str, int] | None = None
    usable_hint = hint_ip if is_runtime_usable_ipv4(hint_ip) else ""

    for raw_line in output.splitlines():
        stripped = raw_line.strip()
        if not stripped.startswith("inet "):
            continue
        parts = _ifconfig_inet_parts(stripped)
        if len(parts) < 2:
            continue
        ip_addr = parts[1]
        if not is_runtime_usable_ipv4(ip_addr):
            continue
        netmask = ""
        for index, value in enumerate(parts):
            if value == "netmask" and index + 1 < len(parts):
                netmask = parts[index + 1]
                break
        prefix = _netmask_to_prefix(netmask)
        if prefix is None:
            prefix = 24
        candidate = (ip_addr, prefix)
        if selected is None:
            selected = candidate
        if usable_hint and ip_addr == usable_hint:
            hinted = candidate

    if hinted is not None:
        ip_addr, prefix = hinted
        return f"{ip_addr}/{prefix}"
    if selected is not None:
        ip_addr, prefix = selected
        return f"{ip_addr}/{prefix}"
    return None


def probe_remote_interface_candidates_conn(
    connection: SshConnection,
    *,
    target_ips: Iterable[str] = (),
) -> RemoteInterfaceCandidatesProbeResult:
    proc = run_ssh(connection, "/sbin/ifconfig -a", check=False, timeout=30)
    if proc.returncode != 0:
        return RemoteInterfaceCandidatesProbeResult(
            candidates=(),
            preferred_iface=None,
            detail=f"ifconfig -a failed: rc={proc.returncode}",
        )
    candidates = _parse_ifconfig_candidates(proc.stdout)
    target_ip_tuple = tuple(value for value in target_ips if value)
    target_ip_set = set(target_ip_tuple)
    target_ip_matches = tuple(
        candidate
        for candidate in candidates
        if not candidate.loopback and target_ip_set.intersection(candidate.ipv4_addrs)
    )
    preferred_iface = preferred_interface_name(candidates, target_ips=target_ip_tuple)
    if preferred_iface is None:
        return RemoteInterfaceCandidatesProbeResult(
            candidates=candidates,
            preferred_iface=None,
            detail="no non-loopback IPv4 interface candidates found",
            target_ip_matches=target_ip_matches,
        )
    return RemoteInterfaceCandidatesProbeResult(
        candidates=candidates,
        preferred_iface=preferred_iface,
        detail=f"preferred interface {preferred_iface}",
        target_ip_matches=target_ip_matches,
    )


def read_interface_ipv4_conn(connection: SshConnection, iface: str) -> str:
    iface_addrs = read_interface_ipv4_addrs_conn(connection, iface)
    usable_addrs = runtime_usable_ipv4s(iface_addrs)
    if usable_addrs:
        return usable_addrs[0]
    if iface_addrs:
        raise DeviceError(
            f"could not determine non-link-local IPv4 for interface {iface} "
            f"(reported: {', '.join(iface_addrs)})"
        )
    raise DeviceError(f"could not determine IPv4 for interface {iface}")


def read_interface_ipv4_addrs_conn(connection: SshConnection, iface: str) -> tuple[str, ...]:
    probe_cmd = (
        f"/sbin/ifconfig {shlex.quote(iface)} 2>/dev/null | "
        "sed -n 's/^[[:space:]]*inet[[:space:]]\\([0-9.]*\\).*/\\1/p' | "
        "sed -n '/^$/d;p'"
    )
    proc = run_ssh(
        connection,
        f"/bin/sh -c {shlex.quote(probe_cmd)}",
        check=False,
    )
    return tuple(line.strip() for line in proc.stdout.splitlines() if line.strip())


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
if [ ! -x /usr/bin/fstat ]; then
    echo "FAIL:fstat missing"
    exit 1
fi
ps_out="$(capture_ps_out)"
out="$(capture_fstat_for_ucomm "$ps_out" smbd)"
status=0
if ! describe_managed_smbd_status "$ps_out" "$out"; then
    status=1
fi
exit "$status"
'''
    try:
        proc = run_ssh(
            connection,
            f"/bin/sh -c {shlex.quote(script)}",
            check=False,
            timeout=timeout_seconds,
        )
    except SshCommandTimeout:
        lines = ("FAIL:managed smbd readiness probe timed out",)
        return ManagedSmbdProbeResult(ready=False, detail=_probe_detail(lines, "managed smbd not ready"), lines=lines)
    lines = _probe_lines(proc.stdout)
    if proc.returncode == 0:
        return ManagedSmbdProbeResult(ready=True, detail=_probe_detail(lines, "managed smbd ready"), lines=lines)
    return ManagedSmbdProbeResult(ready=False, detail=_probe_detail(lines, "managed smbd not ready"), lines=lines)


def probe_managed_mdns_takeover_conn(connection: SshConnection, *, timeout_seconds: int = 20) -> ManagedMdnsTakeoverProbeResult:
    script = rf'''
{SMBD_STATUS_HELPERS}
if [ ! -x /usr/bin/fstat ]; then
    echo "FAIL:fstat missing"
    exit 1
fi
ps_out="$(capture_ps_out)"
out="$(capture_fstat_for_ucomm "$ps_out" mdns-advertiser)"
status=0
if ! describe_managed_mdns_status "$ps_out" "$out"; then
    status=1
fi
exit "$status"
'''
    try:
        proc = run_ssh(
            connection,
            f"/bin/sh -c {shlex.quote(script)}",
            check=False,
            timeout=timeout_seconds,
        )
    except SshCommandTimeout:
        lines = ("FAIL:managed mDNS takeover probe timed out",)
        return ManagedMdnsTakeoverProbeResult(
            ready=False,
            detail=_probe_detail(lines, "managed mDNS takeover not active"),
            lines=lines,
        )
    lines = _probe_lines(proc.stdout)
    if proc.returncode == 0:
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
        probe_timeout = max(1, min(20, int(deadline - iteration_start)))
        if not smbd_ready:
            last_smbd = probe_managed_smbd_conn(connection, timeout_seconds=probe_timeout)
            smbd_ready = last_smbd.ready
        
        if not mdns_ready:
            if not smbd_ready:
                time.sleep(smbd_mdns_stagger_seconds)
            probe_timeout = max(1, min(20, int(deadline - time.monotonic())))
            last_mdns = probe_managed_mdns_takeover_conn(connection, timeout_seconds=probe_timeout)
            mdns_ready = last_mdns.ready

        if smbd_ready and mdns_ready:
            time.sleep(mdns_settle_seconds)
            probe_timeout = max(1, min(20, int(deadline - time.monotonic())))
            settled_mdns = probe_managed_mdns_takeover_conn(connection, timeout_seconds=probe_timeout)
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

    timeout_detail = f"runtime verification timed out after {timeout_seconds}s"
    lines = last_smbd.lines + last_mdns.lines + (f"FAIL:{timeout_detail}",)
    return ManagedRuntimeProbeResult(
        ready=False,
        detail=f"{timeout_detail}; {last_smbd.detail}; {last_mdns.detail}",
        smbd=last_smbd,
        mdns=last_mdns,
        lines=lines,
    )


def nbns_flash_config_enabled_conn(connection: SshConnection) -> bool:
    quoted_config = shlex.quote(FLASH_RUNTIME_CONFIG)
    script = (
        f"if [ -f {quoted_config} ]; then "
        f". {quoted_config}; "
        "if [ \"${NBNS_ENABLED:-0}\" = \"1\" ]; then echo enabled; fi; "
        "fi"
    )
    proc = run_ssh(
        connection,
        f"/bin/sh -c {shlex.quote(script)}",
        check=False,
        timeout=REMOTE_STATE_PROBE_TIMEOUT_SECONDS,
    )
    return proc.stdout.strip() == "enabled"


def read_runtime_share_names_conn(connection: SshConnection) -> list[str]:
    quoted_state = shlex.quote(RUNTIME_SHARES_TSV)
    script = f"if [ -f {quoted_state} ]; then /bin/cat {quoted_state}; fi"
    proc = run_ssh(
        connection,
        f"/bin/sh -c {shlex.quote(script)}",
        check=False,
        timeout=REMOTE_STATE_PROBE_TIMEOUT_SECONDS,
    )
    names: list[str] = []
    for raw_line in proc.stdout.splitlines():
        line = raw_line.rstrip("\n")
        if not line.strip():
            continue
        if "\t" not in line:
            continue
        name = line.split("\t", 1)[0].strip()
        if name:
            names.append(name)
    return names


def _limit_remote_log_tail(text: str) -> str:
    if len(text) <= REMOTE_LOG_TAIL_MAX_CHARS:
        return text
    return f"(truncated to last {REMOTE_LOG_TAIL_MAX_CHARS} chars)\n{text[-REMOTE_LOG_TAIL_MAX_CHARS:]}"


def read_remote_log_tail_conn(connection: SshConnection, path: str) -> str:
    quoted_path = shlex.quote(path)
    script = (
        f"if [ -f {quoted_path} ]; then "
        f"/usr/bin/tail -n {REMOTE_LOG_TAIL_LINES} {quoted_path}; "
        f"else echo '(missing {path})'; fi"
    )
    proc = run_ssh(
        connection,
        f"/bin/sh -c {shlex.quote(script)}",
        check=False,
        timeout=REMOTE_LOG_TAIL_TIMEOUT_SECONDS,
    )
    parts = []
    stdout = (proc.stdout or "").rstrip()
    stderr = (proc.stderr or "").rstrip()
    if stdout:
        parts.append(stdout)
    if stderr:
        parts.append(f"stderr: {stderr}")
    if proc.returncode != 0:
        parts.append(f"(exit {proc.returncode})")
    text = "\n".join(parts) if parts else "(empty)"
    return _limit_remote_log_tail(text)


def read_runtime_payload_dir_conn(connection: SshConnection) -> str | None:
    script = (
        f"payload_tsv={shlex.quote(RUNTIME_PAYLOAD_TSV)}; "
        'if [ -s "$payload_tsv" ]; then '
        "IFS=$(printf '\\t') read -r payload_dir payload_volume payload_device <\"$payload_tsv\"; "
        'if [ -n "$payload_dir" ]; then printf "%s\\n" "$payload_dir"; exit 0; fi; '
        "fi; exit 1"
    )
    proc = run_ssh(
        connection,
        f"/bin/sh -c {shlex.quote(script)}",
        check=False,
        timeout=REMOTE_LOG_TAIL_TIMEOUT_SECONDS,
    )
    if proc.returncode != 0:
        return None
    stdout = (proc.stdout or "").strip()
    if not stdout:
        return None
    return stdout.splitlines()[0]


def read_runtime_log_tails_conn(connection: SshConnection) -> dict[str, str]:
    logs: dict[str, str] = {}
    for key, path in REMOTE_RUNTIME_RAM_LOG_PATHS.items():
        try:
            logs[key] = read_remote_log_tail_conn(connection, path)
        except Exception as e:
            logs[key] = f"(unavailable: {e})"
    try:
        payload_dir = read_runtime_payload_dir_conn(connection)
    except Exception as e:
        payload_dir = None
        logs["remote_payload_log_dir"] = f"(unavailable: {e})"
    if payload_dir:
        logs["remote_payload_log_dir"] = payload_dir
        for key, filename in REMOTE_PAYLOAD_LOG_FILENAMES.items():
            path = f"{payload_dir.rstrip('/')}/logs/{filename}"
            try:
                logs[key] = read_remote_log_tail_conn(connection, path)
            except Exception as e:
                logs[key] = f"(unavailable: {e})"
    else:
        logs.setdefault("remote_payload_log_dir", f"(missing {RUNTIME_PAYLOAD_TSV})")
    for key, path in REMOTE_RUNTIME_FALLBACK_LOG_PATHS.items():
        if key in logs:
            continue
        try:
            logs[key] = read_remote_log_tail_conn(connection, path)
        except Exception as e:
            logs[key] = f"(unavailable: {e})"
    return logs


def runtime_startup_failure_debug_fields(
    logs: Mapping[str, object],
    *,
    verification_detail: str = "",
) -> dict[str, object]:
    combined = "\n".join(
        str(value)
        for value in (
            logs.get("remote_watchdog_log_tail"),
            logs.get("remote_mdns_log_tail"),
            logs.get("remote_nbns_log_tail"),
            verification_detail,
        )
        if value
    )
    if any(
        marker in combined
        for marker in (
            "mDNS startup deferred; no usable IPv4 has appeared yet",
            "mdns-advertiser is waiting for auto-IP",
            "watchdog steady check: core services healthy; mDNS deferred waiting for usable IPv4",
        )
    ):
        return {
            "runtime_startup_failure": "network_auto_ip_unavailable",
            "runtime_startup_waiting_for_auto_ip": True,
        }
    return {}


def _parse_remote_diagnostic_sections(text: str) -> tuple[dict[str, str], dict[str, str]]:
    values: dict[str, str] = {}
    sections: dict[str, list[str]] = {}
    current_section: str | None = None

    for raw_line in text.splitlines():
        line = raw_line.rstrip("\n")
        if line.startswith("TC_DIAG_BEGIN "):
            current_section = line.partition(" ")[2].strip()
            sections[current_section] = []
            continue
        if line.startswith("TC_DIAG_END "):
            current_section = None
            continue
        if current_section is not None:
            sections[current_section].append(line)
            continue
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value

    return values, {key: "\n".join(lines).strip() for key, lines in sections.items()}


def _remote_interface_debug_summary(candidates: Iterable[RemoteInterfaceCandidate]) -> list[dict[str, object]]:
    return [
        {
            "name": candidate.name,
            "ipv4": list(candidate.ipv4_addrs),
            "up": candidate.up,
            "active": candidate.active,
            "loopback": candidate.loopback,
        }
        for candidate in candidates
    ]


def _network_failure_hint(
    *,
    configured_iface: str | None,
    candidates: tuple[RemoteInterfaceCandidate, ...],
    target_host: str,
) -> str | None:
    if not configured_iface:
        return None

    configured_candidate = next((candidate for candidate in candidates if candidate.name == configured_iface), None)
    if configured_candidate is None:
        return f"configured interface {configured_iface} was not reported by ifconfig -a"
    if not configured_candidate.ipv4_addrs:
        return f"configured interface {configured_iface} has no IPv4 address"

    target_matches = [
        candidate.name
        for candidate in candidates
        if target_host and target_host in candidate.ipv4_addrs and candidate.name != configured_iface
    ]
    if target_matches:
        return f"SSH target {target_host} is on {','.join(target_matches)}, not configured interface {configured_iface}"

    if target_host.startswith("169.254."):
        return f"SSH target {target_host} is link-local; configured interface {configured_iface} has IPv4"

    return None


def read_remote_network_diagnostics_conn(connection: SshConnection) -> dict[str, object]:
    script = r'''
printf 'TC_DIAG_BEGIN ifconfig_a\n'
/sbin/ifconfig -a 2>&1 | /usr/bin/sed -n '/ether /d;/address /d;p'
printf 'TC_DIAG_END ifconfig_a\n'
printf 'TC_DIAG_BEGIN routes\n'
if [ -x /usr/bin/netstat ]; then
    /usr/bin/netstat -rn -f inet 2>&1
elif [ -x /bin/netstat ]; then
    /bin/netstat -rn -f inet 2>&1
else
    echo "(route diagnostics unavailable)"
fi
printf 'TC_DIAG_END routes\n'
'''
    proc = run_ssh(
        connection,
        f"/bin/sh -c {shlex.quote(script)}",
        check=False,
        timeout=REMOTE_NETWORK_DIAGNOSTICS_TIMEOUT_SECONDS,
    )
    _values, sections = _parse_remote_diagnostic_sections(proc.stdout or "")
    all_ifconfig = sections.get("ifconfig_a", "")
    candidates = _parse_ifconfig_candidates(all_ifconfig)
    target_host = extract_host(connection.host)
    target_ip_matches = tuple(
        candidate
        for candidate in candidates
        if target_host and target_host in candidate.ipv4_addrs and not candidate.loopback
    )
    diagnostics: dict[str, object] = {
        "remote_network_config": {
            "ssh_target_host": target_host,
        },
        "remote_network_probe_rc": proc.returncode,
        "remote_network_ipv4_interfaces": _remote_interface_debug_summary(candidates),
        "remote_network_preferred_iface": preferred_interface_name(candidates, target_ips=(target_host,)),
        "remote_network_target_ip_matches": [candidate.name for candidate in target_ip_matches],
        "remote_network_routes": _limit_remote_log_tail(sections.get("routes", "")),
    }
    stderr = (proc.stderr or "").strip()
    if stderr:
        diagnostics["remote_network_probe_stderr"] = _limit_remote_log_tail(stderr)
    return diagnostics


def read_remote_service_socket_diagnostics_conn(connection: SshConnection) -> str:
    script = rf'''
{SMBD_STATUS_HELPERS}
if [ ! -x /usr/bin/fstat ]; then
    echo "fstat missing"
    exit 0
fi
ps_out="$(capture_ps_out)"
for proc_name in smbd nbns-advertiser; do
    echo "$proc_name:"
    socket_lines=$(capture_fstat_for_ucomm "$ps_out" "$proc_name" | /usr/bin/sed -n '/internet/p' | /usr/bin/sed -n '1,40p')
    if [ -n "$socket_lines" ]; then
        printf '%s\n' "$socket_lines"
    else
        echo "(no internet sockets reported)"
    fi
done
'''
    proc = run_ssh(
        connection,
        f"/bin/sh -c {shlex.quote(script)}",
        check=False,
        timeout=REMOTE_STATE_PROBE_TIMEOUT_SECONDS,
    )
    return proc.stdout.strip()


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
        except TransportError:
            is_up = False
        if is_up == expected_up:
            return True
        time.sleep(5)
    return False
