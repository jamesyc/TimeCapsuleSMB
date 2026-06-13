from __future__ import annotations

import shlex
import subprocess
import time
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from enum import Enum
from typing import TYPE_CHECKING, Literal

from timecapsulesmb.core.smb_config import parse_active_payload_dir
from timecapsulesmb.device.compat import compatibility_from_probe_result
from timecapsulesmb.device.errors import DeviceError
from timecapsulesmb.device.processes import PROBE_PROCESS_HELPERS, PS_CAPTURE_COMMAND
from timecapsulesmb.transport.local import tcp_open
from timecapsulesmb.transport.errors import (
    SshAlgorithmNegotiationError,
    SshAuthenticationError,
    TransportError,
)
from timecapsulesmb.transport.ssh import SshCommandTimeout, SshConnection, run_ssh, run_ssh_capture_bytes, ssh_opts_use_proxy
from timecapsulesmb.core.config import (
    AIRPORT_IDENTITIES_BY_MODEL,
    AIRPORT_IDENTITIES_BY_SYAP,
    MAX_DNS_LABEL_BYTES,
    MAX_NETBIOS_NAME_BYTES,
)
from timecapsulesmb.core.net import endpoint_host, is_link_local_ipv4, is_loopback_ipv4

if TYPE_CHECKING:
    from timecapsulesmb.device.compat import DeviceCompatibility


RUNTIME_RAM_ROOT = "/mnt/Memory/samba4"
RUNTIME_SMB_CONF = f"{RUNTIME_RAM_ROOT}/etc/smb.conf"
RUNTIME_NBNS_BIN = f"{RUNTIME_RAM_ROOT}/sbin/nbns-advertiser"
FLASH_RUNTIME_CONFIG = "/mnt/Flash/tcapsulesmb.conf"
REMOTE_STATE_PROBE_TIMEOUT_SECONDS = 30
REMOTE_LOG_TAIL_LINES = 80

REMOTE_LOG_TAIL_MAX_CHARS = 8192
REMOTE_LOG_TAIL_TIMEOUT_SECONDS = 30
REMOTE_NETWORK_DIAGNOSTICS_TIMEOUT_SECONDS = 30
SMBD_READINESS_PROBE_TIMEOUT_SECONDS = 30
MDNS_BINARY_PROBE_TIMEOUT_SECONDS = 30
MDNS_BINARY_PROBE_ATTEMPTS = 2
MDNS_PROCESS_TABLE_PROBE_TIMEOUT_SECONDS = 30
MDNS_SOCKET_FAMILIES_PROBE_TIMEOUT_SECONDS = 30
MDNS_FSTAT_PROBE_TIMEOUT_SECONDS = 30
RUNTIME_READINESS_FINAL_ATTEMPTS = 2
NETBSD4_LOGIN_RC_LOCAL_MARKER = b"/mnt/Flash/rc.local"
NETBSD4_LOGIN_PATH = "/etc/rc.d/LOGIN"
REMOTE_RUNTIME_RAM_LOG_PATHS = {
    "remote_rc_local_log_tail": "/mnt/Memory/samba4/var/rc.local.log",
    "remote_manager_log_tail": "/mnt/Memory/samba4/var/manager.log",
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
RUNTIME_PERSISTENT_ROOT_PREFIX=${{RUNTIME_PERSISTENT_ROOT_PREFIX:-/Volumes/}}

runtime_smb_conf_present() {{
    [ -f "$RUNTIME_SMB_CONF_PATH" ]
}}

runtime_smbd_binary_present() {{
    [ -x "$RUNTIME_RAM_SBIN/smbd" ]
}}

describe_runtime_smbd_version() {{
    if ! runtime_smbd_binary_present; then
        echo "FAIL:device Samba version unavailable (managed runtime smbd binary missing)"
        return 1
    fi

    smbd_version_output=$("$RUNTIME_RAM_SBIN/smbd" --version 2>&1)
    smbd_version_status=$?
    smbd_version=$(printf '%s\n' "$smbd_version_output" | /usr/bin/sed -n 's/^Version[[:space:]][[:space:]]*//p' | /usr/bin/sed -n '1p')
    if [ "$smbd_version_status" -eq 0 ] && [ -n "$smbd_version" ]; then
        echo "PASS:device Samba version: $smbd_version"
        return 0
    fi

    smbd_version_detail=$(printf '%s\n' "$smbd_version_output" | /usr/bin/sed -n '1p')
    if [ -z "$smbd_version_detail" ]; then
        smbd_version_detail="exit code $smbd_version_status"
    fi
    echo "FAIL:device Samba version unavailable ($smbd_version_detail)"
    return 1
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

runtime_share_data_paths() {{
    if ! runtime_smb_conf_present; then
        return 1
    fi
    /usr/bin/sed -n '/^[[:space:]]*[#;]/d;s/^[[:space:]]*[Pp][Aa][Tt][Hh][[:space:]]*=[[:space:]]*//p' "$RUNTIME_SMB_CONF_PATH"
}}

runtime_volume_root_for_data_path() {{
    data_root=$1
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

runtime_volume_root() {{
    share_paths=$(runtime_share_data_paths || true)
    [ -n "$share_paths" ] || return 1
    while IFS= read -r data_root; do
        volume_root=$(runtime_volume_root_for_data_path "$data_root" || true)
        if [ -n "$volume_root" ]; then
            printf '%s\n' "$volume_root"
            return 0
        fi
    done <<EOF
$share_paths
EOF
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
    share_paths=$(runtime_share_data_paths || true)
    [ -n "$share_paths" ] || return 1
    while IFS= read -r data_root; do
        volume_root=$(runtime_volume_root_for_data_path "$data_root" || true)
        [ -n "$volume_root" ] || continue
        case " $seen_roots " in
            *" $volume_root "*) ;;
            *)
                seen_roots="$seen_roots $volume_root"
                printf '%s\n' "$volume_root"
                ;;
        esac
    done <<EOF
$share_paths
EOF
    [ -n "$seen_roots" ]
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
    fstat_out=$1
    bind_interfaces=$2
    require_ipv4=0
    require_ipv6=0
    set -- $bind_interfaces
    for token in "$@"; do
        case "$token" in
            127.*|::1/128) ;;
            *:*) require_ipv6=1 ;;
            *.*/*) require_ipv4=1 ;;
        esac
    done
    if [ "$require_ipv4" -eq 0 ] && [ "$require_ipv6" -eq 0 ]; then
        require_ipv4=1
    fi

    has_ipv4=0
    has_ipv6=0
    case "$fstat_out" in
        *smbd*" internet stream tcp "*":445"*) has_ipv4=1 ;;
    esac
    case "$fstat_out" in
        *smbd*" internet6 stream tcp "*":445"*) has_ipv6=1 ;;
    esac
    if [ "$require_ipv4" -eq 1 ] && [ "$has_ipv4" -ne 1 ]; then
        return 1
    fi
    if [ "$require_ipv6" -eq 1 ] && [ "$has_ipv6" -ne 1 ]; then
        return 1
    fi
    return 0
}}

mdns_bound_5353() {{
    fstat_out=$1
    family=$2

    case "$family" in
        ipv4)
            case "$fstat_out" in
                *mdns-advertiser*" internet dgram udp "*":5353"*) return 0 ;;
                *) return 1 ;;
            esac
            ;;
        ipv6)
            case "$fstat_out" in
                *mdns-advertiser*" internet6 dgram udp "*":5353"*) return 0 ;;
                *) return 1 ;;
            esac
            ;;
        *) return 1 ;;
    esac
}}

mdns_socket_families_supported() {{
    families=$1
    saw_family=0

    set -- $families
    for family in "$@"; do
        case "$family" in
            ipv4|ipv6) saw_family=1 ;;
            *) return 1 ;;
        esac
    done

    [ "$saw_family" -eq 1 ]
}}

mdns_bound_required_5353() {{
    fstat_out=$1
    families=$2
    saw_family=0

    set -- $families
    for family in "$@"; do
        case "$family" in
            ipv4|ipv6)
                saw_family=1
                mdns_bound_5353 "$fstat_out" "$family" || return 1
                ;;
            *) return 1 ;;
        esac
    done

    [ "$saw_family" -eq 1 ]
}}

describe_managed_smbd_status() {{
    ps_out=$1
    fstat_out=$2
    status=0
    bind_interfaces=$(read_smb_conf_value "interfaces" || true)
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
    if manager_process_present_for_volume "$ps_out"; then
        echo "PASS:manager is running for managed runtime"
    else
        echo "FAIL:manager is not running for managed runtime"
        status=1
    fi
    if smbd_parent_process_present "$ps_out"; then
        echo "PASS:managed smbd parent process is running"
    else
        echo "FAIL:managed smbd parent process is not running"
        status=1
    fi
    if smbd_bound_445 "$fstat_out" "$bind_interfaces"; then
        echo "PASS:smbd bound to required TCP 445 sockets"
    else
        echo "FAIL:smbd is not bound to required TCP 445 sockets"
        status=1
    fi
    if ! describe_runtime_smbd_version; then
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
    mdns_socket_families=
    mdns_health_family_supported=1
    if [ ! -e "$RUNTIME_MDNS_BIN" ]; then
        mdns_auto_ip_state=failed
        mdns_auto_ip_failure="mdns-advertiser binary missing at $RUNTIME_MDNS_BIN"
    elif [ ! -x "$RUNTIME_MDNS_BIN" ]; then
        mdns_auto_ip_state=failed
        mdns_auto_ip_failure="mdns-advertiser binary is not executable at $RUNTIME_MDNS_BIN"
    else
        mdns_socket_families=$("$RUNTIME_MDNS_BIN" --print-mdns-socket-families 2>/dev/null)
        mdns_auto_ip_rc=$?
        case "$mdns_auto_ip_rc" in
            0) mdns_auto_ip_state=active ;;
            11) mdns_auto_ip_state=waiting ;;
            *)
                mdns_auto_ip_state=failed
                mdns_auto_ip_failure="mdns-advertiser mDNS socket family probe failed with exit code $mdns_auto_ip_rc"
                ;;
        esac
    fi
    if [ "$mdns_auto_ip_state" = "active" ]; then
        if ! mdns_socket_families_supported "$mdns_socket_families"; then
            mdns_health_family_supported=0
        fi
    fi

    if [ "$mdns_auto_ip_state" = "failed" ]; then
        echo "FAIL:$mdns_auto_ip_failure"
        status=1
    fi

    if mdns_process_present "$ps_out"; then
        echo "PASS:mdns-advertiser process is running"
    else
        if [ "$mdns_auto_ip_state" = "waiting" ]; then
            echo "FAIL:mDNS startup deferred; no usable address has appeared yet"
        else
            echo "FAIL:mdns-advertiser process is not running"
        fi
        status=1
    fi
    if [ "$mdns_health_family_supported" -eq 1 ] && mdns_bound_required_5353 "$fstat_out" "$mdns_socket_families"; then
        echo "PASS:mdns-advertiser bound to required UDP 5353 listeners"
        if [ "$mdns_auto_ip_state" = "active" ]; then
            echo "PASS:mdns-advertiser bind address active"
        else
            echo "FAIL:mdns-advertiser bound to UDP 5353 but bind address is not active"
            status=1
        fi
    else
        if mdns_process_present "$ps_out" && [ "$mdns_auto_ip_state" = "waiting" ]; then
            echo "FAIL:mdns-advertiser is waiting for a usable address"
            status=1
        else
            if [ "$mdns_health_family_supported" -eq 1 ]; then
                echo "FAIL:mdns-advertiser is not bound to required UDP 5353 listener"
            else
                echo "FAIL:mdns-advertiser mDNS socket family probe returned no supported family"
            fi
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


class SshAccessStatus(str, Enum):
    OPEN_AUTHENTICATED = "open_authenticated"
    CLOSED = "closed"
    AUTH_REJECTED = "auth_rejected"
    ALGORITHM_NEGOTIATION_FAILED = "algorithm_negotiation_failed"
    TRANSPORT_FAILED = "transport_failed"
    DEVICE_PROBE_FAILED = "device_probe_failed"


@dataclass(frozen=True)
class ProbeResult:
    ssh_status: SshAccessStatus
    error: str | None
    os_name: str
    os_release: str
    arch: str
    elf_endianness: str
    airport_model: str | None = None
    airport_syap: str | None = None
    elf_endianness_detail: str | None = None

    @property
    def ssh_port_reachable(self) -> bool:
        return self.ssh_status != SshAccessStatus.CLOSED

    @property
    def ssh_authenticated(self) -> bool:
        return self.ssh_status == SshAccessStatus.OPEN_AUTHENTICATED


@dataclass(frozen=True)
class ProbedDeviceState:
    probe_result: ProbeResult
    compatibility: DeviceCompatibility | None


@dataclass(frozen=True)
class ElfEndiannessProbeResult:
    endianness: str
    detail: str | None = None


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
class DeployedVersionProbeResult:
    release_tag: str | None
    cli_version_code: int | None
    detail: str


@dataclass(frozen=True)
class RemoteInterfaceCandidate:
    name: str
    ipv4_addrs: tuple[str, ...]
    up: bool
    active: bool
    loopback: bool


ProbeStepStatus = Literal["pass", "fail", "timeout", "skip"]
RuntimeProbeAttemptPhase = Literal["soft_window", "final_check"]


@dataclass(frozen=True)
class ProbeStepResult:
    id: str
    status: ProbeStepStatus
    detail: str
    timeout_seconds: int | None = None
    duration_seconds: float | None = None
    stdout: str = ""
    stderr: str = ""
    returncode: int | None = None

    @property
    def line(self) -> str:
        if self.status == "pass":
            return f"PASS:{self.detail}"
        if self.status == "skip":
            return f"SKIP:{self.detail}"
        return f"FAIL:{self.detail}"


@dataclass(frozen=True)
class ReadinessProbeResult:
    ready: bool
    detail: str
    steps: tuple[ProbeStepResult, ...] = ()

    @property
    def lines(self) -> tuple[str, ...]:
        return tuple(step.line for step in self.steps if step.detail)


@dataclass(frozen=True)
class RemoteNetworkCapabilitiesProbeResult:
    smb_bind_interfaces: str = ""
    mdns_families: tuple[str, ...] = ()
    nbns_families: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class RuntimeProbeAttemptSummary:
    index: int
    phase: RuntimeProbeAttemptPhase
    duration_seconds: float
    ready: bool
    smbd_ready: bool
    mdns_ready: bool
    detail: str
    final_blocker_step: str | None = None
    final_blocker_status: ProbeStepStatus | None = None
    final_blocker_detail: str | None = None


@dataclass(frozen=True)
class ManagedRuntimeProbeResult:
    ready: bool
    detail: str
    smbd: ReadinessProbeResult
    mdns: ReadinessProbeResult
    extra_steps: tuple[ProbeStepResult, ...] = ()
    attempts: tuple[RuntimeProbeAttemptSummary, ...] = ()
    soft_timeout_seconds: int | None = None
    final_attempts_allowed: int = 0

    @property
    def steps(self) -> tuple[ProbeStepResult, ...]:
        return self.smbd.steps + self.mdns.steps + self.extra_steps

    @property
    def lines(self) -> tuple[str, ...]:
        return tuple(step.line for step in self.steps if step.detail)


@dataclass(frozen=True)
class RcLocalAutostartProbeResult:
    enabled: bool
    detail: str
    login_size: int


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
            ssh_status=SshAccessStatus.CLOSED,
            error="SSH is not reachable yet.",
            os_name="",
            os_release="",
            arch="",
            elf_endianness="unknown",
        )

    try:
        os_name, os_release, arch = _probe_remote_os_info_conn(connection)
        elf_endianness_probe = _probe_remote_elf_endianness_result_conn(connection)
        airport_identity = probe_remote_airport_identity_conn(connection)
    except SshAuthenticationError as exc:
        return ProbeResult(
            ssh_status=SshAccessStatus.AUTH_REJECTED,
            error=str(exc) or "SSH authentication failed.",
            os_name="",
            os_release="",
            arch="",
            elf_endianness="unknown",
        )
    except SshAlgorithmNegotiationError as exc:
        return ProbeResult(
            ssh_status=SshAccessStatus.ALGORITHM_NEGOTIATION_FAILED,
            error=str(exc) or "SSH algorithm negotiation failed.",
            os_name="",
            os_release="",
            arch="",
            elf_endianness="unknown",
        )
    except TransportError as exc:
        return ProbeResult(
            ssh_status=SshAccessStatus.TRANSPORT_FAILED,
            error=str(exc) or "SSH transport failed.",
            os_name="",
            os_release="",
            arch="",
            elf_endianness="unknown",
        )
    except DeviceError as exc:
        return ProbeResult(
            ssh_status=SshAccessStatus.DEVICE_PROBE_FAILED,
            error=str(exc) or "Failed to probe device compatibility.",
            os_name="",
            os_release="",
            arch="",
            elf_endianness="unknown",
        )

    return ProbeResult(
        ssh_status=SshAccessStatus.OPEN_AUTHENTICATED,
        error=None,
        os_name=os_name,
        os_release=os_release,
        arch=arch,
        elf_endianness=elf_endianness_probe.endianness,
        airport_model=airport_identity.model,
        airport_syap=airport_identity.syap,
        elf_endianness_detail=elf_endianness_probe.detail,
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


def _probe_remote_elf_endianness_result_conn(connection: SshConnection, path: str = "/bin/sh") -> ElfEndiannessProbeResult:
    script = rf"""
path={shlex.quote(path)}
if [ ! -f "$path" ]; then
  printf 'path_missing=%s\n' "$path"
  echo unknown
  exit 0
fi
b5=$(/bin/dd if="$path" bs=1 skip=5 count=1 2>/dev/null | /usr/bin/sed -n l 2>/dev/null)
case "$b5" in
  "\\001$") echo little ;;
  "\\002$") echo big ;;
  *) printf 'sed_b5=%s\n' "$b5"; echo unknown ;;
esac
"""
    proc = run_ssh(connection, f"/bin/sh -c {shlex.quote(script)}", check=False)
    value = _endianness_probe_value(proc.stdout)
    if value != "unknown":
        return ElfEndiannessProbeResult(value)

    sed_detail = _elf_endianness_probe_detail("sed", proc, value)
    raw_script = rf"""
path={shlex.quote(path)}
if [ ! -f "$path" ]; then
  printf 'path_missing=%s\n' "$path"
  echo unknown
  exit 0
fi
b5=$(/bin/dd if="$path" bs=1 skip=5 count=1 2>/dev/null)
one=$(printf '\001')
two=$(printf '\002')
case "$b5" in
  "$one") echo little ;;
  "$two") echo big ;;
  *) printf 'raw_compare=nomatch\n'; echo unknown ;;
esac
"""
    raw_proc = run_ssh(connection, f"/bin/sh -c {shlex.quote(raw_script)}", check=False)
    raw_value = _endianness_probe_value(raw_proc.stdout)
    return ElfEndiannessProbeResult(
        raw_value,
        f"{sed_detail}; {_elf_endianness_probe_detail('raw', raw_proc, raw_value)}",
    )


def _endianness_probe_value(stdout: str | None) -> str:
    endianness = (stdout or "").strip().splitlines()
    value = endianness[-1].strip() if endianness else ""
    if value in {"little", "big", "unknown"}:
        return value
    return "unknown"


def _elf_endianness_probe_detail(method: str, proc: subprocess.CompletedProcess[str], value: str) -> str:
    return f"{method}={value},rc={proc.returncode},stdout={_summarize_elf_endianness_stdout(proc.stdout)}"


def _summarize_elf_endianness_stdout(stdout: str | None, *, limit: int = 240) -> str:
    text = (stdout or "").strip()
    if not text:
        return "<empty>"
    escaped = text.encode("unicode_escape", errors="backslashreplace").decode("ascii")
    if len(escaped) <= limit:
        return escaped
    return escaped[: limit - 3] + "..."


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


def read_active_smb_conf_conn(
    connection: SshConnection,
    *,
    timeout_seconds: int = REMOTE_STATE_PROBE_TIMEOUT_SECONDS,
) -> str:
    quoted_conf = shlex.quote(RUNTIME_SMB_CONF)
    script = f"if [ -f {quoted_conf} ]; then cat {quoted_conf}; fi"
    proc = run_ssh(
        connection,
        f"/bin/sh -c {shlex.quote(script)}",
        check=False,
        timeout=timeout_seconds,
    )
    return proc.stdout


def _probe_lines(stdout: str) -> tuple[str, ...]:
    return tuple(line.strip() for line in stdout.splitlines() if line.strip())


def _probe_step_from_line(index: int, line: str) -> ProbeStepResult:
    if line.startswith("PASS:"):
        return ProbeStepResult(id=f"remote_{index}", status="pass", detail=line.removeprefix("PASS:"))
    if line.startswith("FAIL:"):
        return ProbeStepResult(id=f"remote_{index}", status="fail", detail=line.removeprefix("FAIL:"))
    if line.startswith("SKIP:"):
        return ProbeStepResult(id=f"remote_{index}", status="skip", detail=line.removeprefix("SKIP:"))
    return ProbeStepResult(id=f"remote_{index}", status="fail", detail=line)


def _probe_steps_from_lines(lines: tuple[str, ...]) -> tuple[ProbeStepResult, ...]:
    return tuple(_probe_step_from_line(index, line) for index, line in enumerate(lines))


def _probe_detail_from_steps(steps: tuple[ProbeStepResult, ...], default: str) -> str:
    failures = [step.detail for step in steps if step.status in {"fail", "timeout"}]
    if failures:
        return "; ".join(failures)
    passes = [step.detail for step in steps if step.status == "pass"]
    if passes:
        return "; ".join(passes)
    return default


def _readiness_result_from_lines(
    *,
    ready: bool,
    lines: tuple[str, ...],
    default_detail: str,
) -> ReadinessProbeResult:
    steps = _probe_steps_from_lines(lines)
    return ReadinessProbeResult(
        ready=ready,
        detail=_probe_detail_from_steps(steps, default_detail),
        steps=steps,
    )


def _readiness_result_from_steps(
    *,
    ready: bool,
    steps: list[ProbeStepResult],
    default_detail: str,
) -> ReadinessProbeResult:
    tuple_steps = tuple(steps)
    return ReadinessProbeResult(
        ready=ready,
        detail=_probe_detail_from_steps(tuple_steps, default_detail),
        steps=tuple_steps,
    )


def _run_timed_probe_step(
    connection: SshConnection,
    *,
    step_id: str,
    timeout_detail: str,
    script: str,
    timeout_seconds: int,
) -> tuple[ProbeStepResult, subprocess.CompletedProcess[str] | None]:
    started = time.monotonic()
    try:
        proc = run_ssh(
            connection,
            f"/bin/sh -c {shlex.quote(script)}",
            check=False,
            timeout=timeout_seconds,
        )
    except SshCommandTimeout:
        return (
            ProbeStepResult(
                id=step_id,
                status="timeout",
                detail=f"{timeout_detail} timed out after {timeout_seconds}s",
                timeout_seconds=timeout_seconds,
                duration_seconds=time.monotonic() - started,
            ),
            None,
        )
    status: ProbeStepStatus = "pass" if proc.returncode == 0 else "fail"
    return (
        ProbeStepResult(
            id=step_id,
            status=status,
            detail=f"{timeout_detail} completed" if status == "pass" else f"{timeout_detail} failed with exit code {proc.returncode}",
            timeout_seconds=timeout_seconds,
            duration_seconds=time.monotonic() - started,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
            returncode=proc.returncode,
        ),
        proc,
    )


def _append_step(steps: list[ProbeStepResult], step_id: str, status: ProbeStepStatus, detail: str) -> None:
    steps.append(ProbeStepResult(id=step_id, status=status, detail=detail))


def _parse_live_pids_for_ucomm(ps_out: str, ucomm: str) -> tuple[str, ...]:
    pids: list[str] = []
    for raw_line in ps_out.splitlines():
        fields = raw_line.split()
        if len(fields) < 5:
            continue
        pid, _ppid, stat, _time_field, proc_ucomm = fields[:5]
        if stat.startswith("Z") or proc_ucomm != ucomm:
            continue
        if pid.isdigit():
            pids.append(pid)
    return tuple(pids)


def _process_present_for_ucomm(ps_out: str, ucomm: str) -> bool:
    return bool(_parse_live_pids_for_ucomm(ps_out, ucomm))


def _fstat_has_udp_port(fstat_out: str, proc_name: str, family: str, port: int) -> bool:
    socket_family = "internet6" if family == "ipv6" else "internet"
    needle = f" {socket_family} dgram udp "
    port_suffix = f":{port}"
    for line in fstat_out.splitlines():
        if proc_name in line and needle in line and port_suffix in line:
            return True
    return False


def _mdns_bound_required_5353(fstat_out: str, families: tuple[str, ...]) -> bool:
    return bool(families) and all(_fstat_has_udp_port(fstat_out, "mdns-advertiser", family, 5353) for family in families)


def probe_managed_smbd_conn(
    connection: SshConnection,
    *,
    timeout_seconds: int = SMBD_READINESS_PROBE_TIMEOUT_SECONDS,
) -> ReadinessProbeResult:
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
        return _readiness_result_from_lines(ready=False, lines=lines, default_detail="managed smbd not ready")
    lines = _probe_lines(proc.stdout)
    if proc.returncode == 0:
        return _readiness_result_from_lines(ready=True, lines=lines, default_detail="managed smbd ready")
    return _readiness_result_from_lines(ready=False, lines=lines, default_detail="managed smbd not ready")


def probe_managed_mdns_takeover_conn(
    connection: SshConnection,
    *,
    binary_timeout_seconds: int = MDNS_BINARY_PROBE_TIMEOUT_SECONDS,
    process_timeout_seconds: int = MDNS_PROCESS_TABLE_PROBE_TIMEOUT_SECONDS,
    socket_families_timeout_seconds: int = MDNS_SOCKET_FAMILIES_PROBE_TIMEOUT_SECONDS,
    fstat_timeout_seconds: int = MDNS_FSTAT_PROBE_TIMEOUT_SECONDS,
) -> ReadinessProbeResult:
    steps: list[ProbeStepResult] = []

    binary_script = r'''
RUNTIME_MDNS_BIN=${RUNTIME_MDNS_BIN:-/mnt/Flash/mdns-advertiser}
if [ ! -e "$RUNTIME_MDNS_BIN" ]; then
    echo "missing"
    exit 2
fi
if [ ! -x "$RUNTIME_MDNS_BIN" ]; then
    echo "not_executable"
    exit 3
fi
echo "$RUNTIME_MDNS_BIN"
'''
    binary_step, binary_proc = _run_timed_probe_step(
        connection,
        step_id="mdns_binary_probe",
        timeout_detail="mdns-advertiser binary probe",
        script=binary_script,
        timeout_seconds=binary_timeout_seconds,
    )
    for _attempt in range(1, MDNS_BINARY_PROBE_ATTEMPTS):
        if binary_step.status != "timeout":
            break
        binary_step, binary_proc = _run_timed_probe_step(
            connection,
            step_id="mdns_binary_probe",
            timeout_detail="mdns-advertiser binary probe",
            script=binary_script,
            timeout_seconds=binary_timeout_seconds,
        )
    if binary_step.status == "timeout":
        steps.append(binary_step)
        return _readiness_result_from_steps(ready=False, steps=steps, default_detail="managed mDNS takeover not active")
    if binary_proc is None or binary_proc.returncode != 0:
        stdout = ("" if binary_proc is None else binary_proc.stdout).strip()
        if stdout == "missing":
            detail = "mdns-advertiser binary missing at /mnt/Flash/mdns-advertiser"
        elif stdout == "not_executable":
            detail = "mdns-advertiser binary is not executable at /mnt/Flash/mdns-advertiser"
        else:
            rc = "unknown" if binary_proc is None else str(binary_proc.returncode)
            detail = f"mdns-advertiser binary probe failed with exit code {rc}"
        _append_step(steps, "mdns_binary", "fail", detail)
        return _readiness_result_from_steps(ready=False, steps=steps, default_detail="managed mDNS takeover not active")
    _append_step(steps, "mdns_binary", "pass", "mdns-advertiser binary is executable")

    ps_step, ps_proc = _run_timed_probe_step(
        connection,
        step_id="mdns_process_table_probe",
        timeout_detail="mDNS process table probe",
        script=PS_CAPTURE_COMMAND,
        timeout_seconds=process_timeout_seconds,
    )
    if ps_step.status == "timeout":
        steps.append(ps_step)
        return _readiness_result_from_steps(ready=False, steps=steps, default_detail="managed mDNS takeover not active")
    ps_out = "" if ps_proc is None else ps_proc.stdout
    mdns_pids = _parse_live_pids_for_ucomm(ps_out, "mdns-advertiser")
    apple_mdns_running = _process_present_for_ucomm(ps_out, "mDNSResponder")

    if mdns_pids:
        _append_step(steps, "mdns_process", "pass", "mdns-advertiser process is running")

    families_script = r'''
RUNTIME_MDNS_BIN=${RUNTIME_MDNS_BIN:-/mnt/Flash/mdns-advertiser}
"$RUNTIME_MDNS_BIN" --print-mdns-socket-families
'''
    families_step, families_proc = _run_timed_probe_step(
        connection,
        step_id="mdns_socket_families_probe",
        timeout_detail="mdns-advertiser socket family probe",
        script=families_script,
        timeout_seconds=socket_families_timeout_seconds,
    )
    if families_step.status == "timeout":
        steps.append(families_step)
        return _readiness_result_from_steps(ready=False, steps=steps, default_detail="managed mDNS takeover not active")

    family_rc = 1 if families_proc is None else families_proc.returncode
    families_out = "" if families_proc is None else families_proc.stdout
    mdns_families = _capability_family_tokens(families_out)
    if family_rc == 11:
        if mdns_pids:
            _append_step(steps, "mdns_auto_ip", "fail", "mdns-advertiser is waiting for a usable address")
        else:
            _append_step(steps, "mdns_process", "fail", "mDNS startup deferred; no usable address has appeared yet")
        if apple_mdns_running:
            _append_step(steps, "apple_mdns", "fail", "Apple mDNSResponder is still running")
        else:
            _append_step(steps, "apple_mdns", "pass", "Apple mDNSResponder is stopped")
        return _readiness_result_from_steps(ready=False, steps=steps, default_detail="managed mDNS takeover not active")
    if family_rc != 0:
        _append_step(
            steps,
            "mdns_socket_families",
            "fail",
            f"mdns-advertiser mDNS socket family probe failed with exit code {family_rc}",
        )
        return _readiness_result_from_steps(ready=False, steps=steps, default_detail="managed mDNS takeover not active")
    if not mdns_families:
        _append_step(steps, "mdns_socket_families", "fail", "mdns-advertiser mDNS socket family probe returned no supported family")
        return _readiness_result_from_steps(ready=False, steps=steps, default_detail="managed mDNS takeover not active")
    _append_step(steps, "mdns_socket_families", "pass", f"mdns-advertiser socket families active: {' '.join(mdns_families)}")

    if not mdns_pids:
        _append_step(steps, "mdns_process", "fail", "mdns-advertiser process is not running")
        if apple_mdns_running:
            _append_step(steps, "apple_mdns", "fail", "Apple mDNSResponder is still running")
        else:
            _append_step(steps, "apple_mdns", "pass", "Apple mDNSResponder is stopped")
        return _readiness_result_from_steps(ready=False, steps=steps, default_detail="managed mDNS takeover not active")

    fstat_script = "if [ ! -x /usr/bin/fstat ]; then echo fstat_missing; exit 127; fi; " + " ".join(
        f"/usr/bin/fstat -p {pid} 2>/dev/null || true;" for pid in mdns_pids
    )
    fstat_step, fstat_proc = _run_timed_probe_step(
        connection,
        step_id="mdns_fstat_probe",
        timeout_detail="mdns-advertiser fstat probe",
        script=fstat_script,
        timeout_seconds=fstat_timeout_seconds,
    )
    if fstat_step.status == "timeout":
        steps.append(fstat_step)
        return _readiness_result_from_steps(ready=False, steps=steps, default_detail="managed mDNS takeover not active")
    if fstat_proc is None or fstat_proc.returncode == 127:
        _append_step(steps, "mdns_fstat", "fail", "fstat missing")
        return _readiness_result_from_steps(ready=False, steps=steps, default_detail="managed mDNS takeover not active")
    fstat_out = "" if fstat_proc is None else fstat_proc.stdout
    if _mdns_bound_required_5353(fstat_out, mdns_families):
        _append_step(steps, "mdns_udp_5353", "pass", "mdns-advertiser bound to required UDP 5353 listeners")
        _append_step(steps, "mdns_bind_address", "pass", "mdns-advertiser bind address active")
    else:
        _append_step(steps, "mdns_udp_5353", "fail", "mdns-advertiser is not bound to required UDP 5353 listener")

    if apple_mdns_running:
        _append_step(steps, "apple_mdns", "fail", "Apple mDNSResponder is still running")
    else:
        _append_step(steps, "apple_mdns", "pass", "Apple mDNSResponder is stopped")

    ready = all(step.status == "pass" for step in steps)
    return _readiness_result_from_steps(
        ready=ready,
        steps=steps,
        default_detail="managed mDNS takeover active" if ready else "managed mDNS takeover not active",
    )


def _capability_family_tokens(value: str) -> tuple[str, ...]:
    tokens: list[str] = []
    for token in value.split():
        token = token.strip().lower()
        if token in {"ipv4", "ipv6"} and token not in tokens:
            tokens.append(token)
    return tuple(tokens)


def probe_remote_network_capabilities_conn(connection: SshConnection, *, timeout_seconds: int = 25) -> RemoteNetworkCapabilitiesProbeResult:
    script = rf'''
RUNTIME_RAM_ROOT=${{RUNTIME_RAM_ROOT:-/mnt/Memory/samba4}}
RUNTIME_RAM_SBIN="$RUNTIME_RAM_ROOT/sbin"
RUNTIME_MDNS_BIN=${{RUNTIME_MDNS_BIN:-/mnt/Flash/mdns-advertiser}}
RUNTIME_NBNS_BIN=${{RUNTIME_NBNS_BIN:-$RUNTIME_RAM_SBIN/nbns-advertiser}}

tc_probe_cap() {{
    cap_name=$1
    cap_bin=$2
    shift 2
    if [ ! -x "$cap_bin" ]; then
        echo "TC_CAP_ERROR $cap_name missing"
        return 0
    fi
    cap_out=$("$cap_bin" "$@" 2>/dev/null)
    cap_rc=$?
    if [ "$cap_rc" -eq 0 ]; then
        echo "TC_CAP $cap_name $cap_out"
    else
        echo "TC_CAP_ERROR $cap_name rc=$cap_rc"
    fi
}}

tc_probe_cap smb "$RUNTIME_MDNS_BIN" --print-smb-bind-interfaces
tc_probe_cap mdns "$RUNTIME_MDNS_BIN" --print-mdns-socket-families
tc_probe_cap nbns "$RUNTIME_NBNS_BIN" --print-nbns-socket-families
'''
    proc = run_ssh(
        connection,
        f"/bin/sh -c {shlex.quote(script)}",
        check=False,
        timeout=timeout_seconds,
    )

    smb_bind_interfaces = ""
    mdns_families: tuple[str, ...] = ()
    nbns_families: tuple[str, ...] = ()
    errors: list[str] = []
    for raw_line in (proc.stdout or "").splitlines():
        line = raw_line.strip()
        if line.startswith("TC_CAP "):
            fields = line.split(" ", 2)
            if len(fields) < 3:
                errors.append(line.removeprefix("TC_CAP "))
                continue
            _prefix, cap_name, value = fields
            if cap_name == "smb":
                smb_bind_interfaces = value.strip()
            elif cap_name == "mdns":
                mdns_families = _capability_family_tokens(value)
            elif cap_name == "nbns":
                nbns_families = _capability_family_tokens(value)
        elif line.startswith("TC_CAP_ERROR "):
            errors.append(line.removeprefix("TC_CAP_ERROR "))
    stderr = (proc.stderr or "").strip()
    if stderr:
        errors.append(stderr)
    return RemoteNetworkCapabilitiesProbeResult(
        smb_bind_interfaces=smb_bind_interfaces,
        mdns_families=mdns_families,
        nbns_families=nbns_families,
        errors=tuple(errors),
    )


def probe_netbsd4_rc_local_autostart_conn(
    connection: SshConnection,
    *,
    timeout_seconds: int = 30,
) -> RcLocalAutostartProbeResult:
    login = run_ssh_capture_bytes(
        connection,
        f"/bin/dd if={NETBSD4_LOGIN_PATH} bs=4096 2>/dev/null",
        timeout=timeout_seconds,
        missing_tool_message=(
            "Reading NetBSD4 boot autostart state requires local sshpass. "
            "Run `./tcapsule bootstrap` to install sshpass, then rerun `tcapsule deploy`."
        ),
    )
    enabled = NETBSD4_LOGIN_RC_LOCAL_MARKER in login
    detail = (
        f"{NETBSD4_LOGIN_PATH} invokes /mnt/Flash/rc.local"
        if enabled
        else f"{NETBSD4_LOGIN_PATH} does not invoke /mnt/Flash/rc.local"
    )
    return RcLocalAutostartProbeResult(enabled=enabled, detail=detail, login_size=len(login))


def _managed_runtime_detail(smbd: ReadinessProbeResult, mdns: ReadinessProbeResult) -> str:
    details = tuple(detail for detail in (smbd.detail, mdns.detail) if detail)
    return "; ".join(details) if details else "managed runtime not ready"


def _runtime_final_blocker(result: ManagedRuntimeProbeResult) -> ProbeStepResult | None:
    failed_steps = [
        step
        for step in result.steps
        if step.status in {"fail", "timeout"} and step.id != "runtime_timeout"
    ]
    return failed_steps[-1] if failed_steps else None


def _runtime_attempt_summary(
    *,
    index: int,
    phase: RuntimeProbeAttemptPhase,
    duration_seconds: float,
    result: ManagedRuntimeProbeResult,
) -> RuntimeProbeAttemptSummary:
    final_blocker = _runtime_final_blocker(result)
    return RuntimeProbeAttemptSummary(
        index=index,
        phase=phase,
        duration_seconds=round(duration_seconds, 3),
        ready=result.ready,
        smbd_ready=result.smbd.ready,
        mdns_ready=result.mdns.ready,
        detail=result.detail,
        final_blocker_step=None if final_blocker is None else final_blocker.id,
        final_blocker_status=None if final_blocker is None else final_blocker.status,
        final_blocker_detail=None if final_blocker is None else final_blocker.detail,
    )


def _runtime_result_with_attempts(
    result: ManagedRuntimeProbeResult,
    *,
    attempts: list[RuntimeProbeAttemptSummary],
    soft_timeout_seconds: int,
    final_attempts_allowed: int,
) -> ManagedRuntimeProbeResult:
    return replace(
        result,
        attempts=tuple(attempts),
        soft_timeout_seconds=soft_timeout_seconds,
        final_attempts_allowed=final_attempts_allowed,
    )


def probe_managed_runtime_once_conn(
    connection: SshConnection,
    *,
    smbd_timeout_seconds: int = SMBD_READINESS_PROBE_TIMEOUT_SECONDS,
    smbd_mdns_stagger_seconds: float = 1.0,
    mdns_settle_seconds: float = 3.0,
) -> ManagedRuntimeProbeResult:
    smbd = probe_managed_smbd_conn(connection, timeout_seconds=smbd_timeout_seconds)
    if not smbd.ready and smbd_mdns_stagger_seconds > 0:
        time.sleep(smbd_mdns_stagger_seconds)
    mdns = probe_managed_mdns_takeover_conn(connection)

    if smbd.ready and mdns.ready:
        time.sleep(mdns_settle_seconds)
        settled_mdns = probe_managed_mdns_takeover_conn(connection)
        if settled_mdns.ready:
            return ManagedRuntimeProbeResult(
                ready=True,
                detail="managed runtime is ready",
                smbd=smbd,
                mdns=settled_mdns,
                extra_steps=(
                    ProbeStepResult(
                        id="mdns_settle",
                        status="pass",
                        detail="mdns-advertiser remained healthy after settle delay",
                    ),
                ),
            )
        mdns = ReadinessProbeResult(
            ready=False,
            detail=f"{settled_mdns.detail}; mdns-advertiser did not survive settle delay",
            steps=settled_mdns.steps + (
                ProbeStepResult(
                    id="mdns_settle",
                    status="fail",
                    detail="mdns-advertiser did not remain healthy after settle delay",
                ),
            ),
        )

    return ManagedRuntimeProbeResult(
        ready=False,
        detail=_managed_runtime_detail(smbd, mdns),
        smbd=smbd,
        mdns=mdns,
    )


def _run_runtime_probe_attempt(
    connection: SshConnection,
    *,
    index: int,
    phase: RuntimeProbeAttemptPhase,
    smbd_mdns_stagger_seconds: float,
    mdns_settle_seconds: float,
) -> tuple[ManagedRuntimeProbeResult, RuntimeProbeAttemptSummary]:
    started = time.monotonic()
    result = probe_managed_runtime_once_conn(
        connection,
        smbd_mdns_stagger_seconds=smbd_mdns_stagger_seconds,
        mdns_settle_seconds=mdns_settle_seconds,
    )
    duration_seconds = time.monotonic() - started
    return (
        result,
        _runtime_attempt_summary(
            index=index,
            phase=phase,
            duration_seconds=duration_seconds,
            result=result,
        ),
    )


def _sleep_until_next_runtime_attempt(
    *,
    attempt_duration_seconds: float,
    poll_interval_seconds: float,
    deadline: float,
) -> None:
    sleep_for = max(0.0, poll_interval_seconds - attempt_duration_seconds)
    remaining = deadline - time.monotonic()
    if sleep_for <= 0 or remaining <= 0:
        return
    time.sleep(min(sleep_for, remaining))


def _runtime_timeout_detail(timeout_seconds: int, final_attempts_allowed: int) -> str:
    if final_attempts_allowed == 1:
        return f"runtime verification timed out after {timeout_seconds}s plus 1 final check"
    return f"runtime verification timed out after {timeout_seconds}s plus {final_attempts_allowed} final checks"


def probe_managed_runtime_conn(
    connection: SshConnection,
    *,
    timeout_seconds: int = 120,
    poll_interval_seconds: float = 5.0,
    smbd_mdns_stagger_seconds: float = 1.0,
    mdns_settle_seconds: float = 3.0,
    final_attempts: int = RUNTIME_READINESS_FINAL_ATTEMPTS,
) -> ManagedRuntimeProbeResult:
    final_attempts = max(0, final_attempts)
    deadline = time.monotonic() + timeout_seconds
    attempts: list[RuntimeProbeAttemptSummary] = []
    last_result: ManagedRuntimeProbeResult | None = None

    while time.monotonic() < deadline:
        result, summary = _run_runtime_probe_attempt(
            connection,
            index=len(attempts) + 1,
            phase="soft_window",
            smbd_mdns_stagger_seconds=smbd_mdns_stagger_seconds,
            mdns_settle_seconds=mdns_settle_seconds,
        )
        attempts.append(summary)
        last_result = result
        if result.ready:
            return _runtime_result_with_attempts(
                result,
                attempts=attempts,
                soft_timeout_seconds=timeout_seconds,
                final_attempts_allowed=final_attempts,
            )
        _sleep_until_next_runtime_attempt(
            attempt_duration_seconds=summary.duration_seconds,
            poll_interval_seconds=poll_interval_seconds,
            deadline=deadline,
        )

    for _ in range(final_attempts):
        result, summary = _run_runtime_probe_attempt(
            connection,
            index=len(attempts) + 1,
            phase="final_check",
            smbd_mdns_stagger_seconds=smbd_mdns_stagger_seconds,
            mdns_settle_seconds=mdns_settle_seconds,
        )
        attempts.append(summary)
        last_result = result
        if result.ready:
            return _runtime_result_with_attempts(
                result,
                attempts=attempts,
                soft_timeout_seconds=timeout_seconds,
                final_attempts_allowed=final_attempts,
            )

    if last_result is None:
        last_result = ManagedRuntimeProbeResult(
            ready=False,
            detail="managed runtime not ready",
            smbd=ReadinessProbeResult(ready=False, detail="managed smbd not ready"),
            mdns=ReadinessProbeResult(ready=False, detail="managed mDNS takeover not active"),
        )

    timeout_detail = _runtime_timeout_detail(timeout_seconds, final_attempts)
    return _runtime_result_with_attempts(
        replace(
            last_result,
            ready=False,
            detail=f"{timeout_detail}; {last_result.detail}",
            extra_steps=last_result.extra_steps
            + (
                ProbeStepResult(
                    id="runtime_timeout",
                    status="fail",
                    detail=timeout_detail,
                ),
            ),
        ),
        attempts=attempts,
        soft_timeout_seconds=timeout_seconds,
        final_attempts_allowed=final_attempts,
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


def flash_runtime_config_present_conn(connection: SshConnection) -> bool:
    script = f"[ -f {shlex.quote(FLASH_RUNTIME_CONFIG)} ]"
    proc = run_ssh(
        connection,
        f"/bin/sh -c {shlex.quote(script)}",
        check=False,
        timeout=REMOTE_STATE_PROBE_TIMEOUT_SECONDS,
    )
    return proc.returncode == 0


def read_deployed_version_conn(connection: SshConnection) -> DeployedVersionProbeResult:
    script = (
        f"config={shlex.quote(FLASH_RUNTIME_CONFIG)}; "
        "TC_DEPLOY_RELEASE_TAG=; "
        "TC_DEPLOY_CLI_VERSION_CODE=; "
        'if [ -f "$config" ]; then . "$config" >/dev/null 2>&1 || true; fi; '
        'printf "release_tag=%s\\n" "$TC_DEPLOY_RELEASE_TAG"; '
        'printf "cli_version_code=%s\\n" "$TC_DEPLOY_CLI_VERSION_CODE"'
    )
    proc = run_ssh(
        connection,
        f"/bin/sh -c {shlex.quote(script)}",
        check=False,
        timeout=REMOTE_STATE_PROBE_TIMEOUT_SECONDS,
    )
    values: dict[str, str] = {}
    for raw_line in proc.stdout.splitlines():
        key, sep, value = raw_line.partition("=")
        if sep:
            values[key.strip()] = value.strip()

    release_tag = values.get("release_tag") or None
    raw_version_code = values.get("cli_version_code") or ""
    try:
        version_code = int(raw_version_code)
    except ValueError:
        version_code = None

    detail = "ok" if release_tag is not None and version_code is not None else "missing version metadata"
    return DeployedVersionProbeResult(release_tag, version_code, detail)


def runtime_ram_root_present_conn(connection: SshConnection) -> bool:
    script = f"[ -d {shlex.quote(RUNTIME_RAM_ROOT)} ]"
    proc = run_ssh(
        connection,
        f"/bin/sh -c {shlex.quote(script)}",
        check=False,
        timeout=REMOTE_STATE_PROBE_TIMEOUT_SECONDS,
    )
    return proc.returncode == 0


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


def read_runtime_payload_dir_conn(
    connection: SshConnection,
    *,
    timeout_seconds: int = REMOTE_STATE_PROBE_TIMEOUT_SECONDS,
) -> str | None:
    try:
        smb_conf = read_active_smb_conf_conn(connection, timeout_seconds=timeout_seconds)
    except Exception:
        return None
    return parse_active_payload_dir(smb_conf)


def read_runtime_log_tails_conn(connection: SshConnection) -> dict[str, str]:
    logs: dict[str, str] = {}
    for key, path in REMOTE_RUNTIME_RAM_LOG_PATHS.items():
        try:
            logs[key] = read_remote_log_tail_conn(connection, path)
        except Exception as e:
            logs[key] = f"(unavailable: {e})"
    try:
        payload_dir = read_runtime_payload_dir_conn(connection, timeout_seconds=REMOTE_LOG_TAIL_TIMEOUT_SECONDS)
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
        logs.setdefault("remote_payload_log_dir", f"(unavailable from active {RUNTIME_SMB_CONF})")
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
            logs.get("remote_manager_log_tail"),
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
            "mDNS startup deferred; no usable address has appeared yet",
            "mdns-advertiser is waiting for auto-IP",
            "mdns-advertiser is waiting for a usable address",
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
    target_host = endpoint_host(connection.host)
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


def read_runtime_ram_diagnostics_conn(connection: SshConnection) -> str:
    script = rf'''
RUNTIME_RAM_ROOT={RUNTIME_RAM_ROOT}
RUNTIME_RAM_SBIN="$RUNTIME_RAM_ROOT/sbin"
RUNTIME_RAM_ETC="$RUNTIME_RAM_ROOT/etc"
RUNTIME_RAM_PRIVATE="$RUNTIME_RAM_ROOT/private"
RUNTIME_RAM_VAR="$RUNTIME_RAM_ROOT/var"

echo "df /mnt/Memory:"
/bin/df -k /mnt/Memory 2>&1 || true
echo "runtime paths:"
for runtime_path in \
    "$RUNTIME_RAM_ROOT" \
    "$RUNTIME_RAM_SBIN" \
    "$RUNTIME_RAM_ETC" \
    "$RUNTIME_RAM_PRIVATE" \
    "$RUNTIME_RAM_VAR" \
    "$RUNTIME_RAM_SBIN/smbd" \
    $RUNTIME_RAM_SBIN/smbd.tmp.* \
    "$RUNTIME_RAM_SBIN/nbns-advertiser" \
    "$RUNTIME_RAM_PRIVATE/smbpasswd" \
    "$RUNTIME_RAM_PRIVATE/username.map" \
    "$RUNTIME_RAM_ETC/smb.conf"
do
    case "$runtime_path" in
        *"*"*) continue ;;
    esac
    if [ -e "$runtime_path" ]; then
        /bin/ls -ldn "$runtime_path" 2>&1 || true
    else
        echo "missing $runtime_path"
    fi
done
'''
    proc = run_ssh(
        connection,
        f"/bin/sh -c {shlex.quote(script)}",
        check=False,
        timeout=REMOTE_STATE_PROBE_TIMEOUT_SECONDS,
    )
    parts = []
    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    if stdout:
        parts.append(stdout)
    if stderr:
        parts.append(f"stderr: {stderr}")
    if proc.returncode != 0:
        parts.append(f"(exit {proc.returncode})")
    return _limit_remote_log_tail("\n".join(parts) if parts else "(empty)")


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
            proc = run_ssh(connection, "/bin/echo ok", check=False, timeout=30)
            is_up = proc.returncode == 0 and proc.stdout.strip().endswith("ok")
        except TransportError:
            is_up = False
        if is_up == expected_up:
            return True
        time.sleep(5)
    return False
