from __future__ import annotations

import ipaddress
import shlex
import subprocess
import time
import re
from collections.abc import Iterable
from dataclasses import dataclass, replace
from enum import Enum
from typing import TYPE_CHECKING, Literal

from timecapsulesmb.core.smb_config import parse_active_payload_dir
from timecapsulesmb.device.compat import compatibility_from_probe_result
from timecapsulesmb.device.errors import DeviceError
from timecapsulesmb.device.processes import PROBE_PROCESS_HELPERS, PS_CAPTURE_COMMAND, service_role_lines
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

if TYPE_CHECKING:
    from timecapsulesmb.device.compat import DeviceCompatibility


RUNTIME_RAM_ROOT = "/mnt/Memory/samba4"
RUNTIME_SMB_CONF = f"{RUNTIME_RAM_ROOT}/etc/smb.conf"
RUNTIME_RSYNC_BIN = f"{RUNTIME_RAM_ROOT}/sbin/rsync"
RUNTIME_RSYNC_CONF = f"{RUNTIME_RAM_ROOT}/etc/rsyncd.conf"
FLASH_RUNTIME_CONFIG = "/mnt/Flash/tcapsulesmb.conf"
REMOTE_STATE_PROBE_TIMEOUT_SECONDS = 30
REMOTE_LOG_TAIL_LINES = 80

REMOTE_LOG_TAIL_MAX_CHARS = 8192
REMOTE_LOG_TAIL_TIMEOUT_SECONDS = 30
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
    "remote_manager_log_tail": "/mnt/Memory/samba4/var/runtime.log",
    "remote_rsync_log_tail": "/mnt/Memory/samba4/var/rsync.log",
    "remote_telemetry_log_tail": "/mnt/Memory/samba4/var/telemetry.log",
    # Discovery logs here, not to the payload, whenever smbd is not ready.
    "remote_diskless_discovery_log_tail": "/mnt/Memory/samba4/var/discovery.log",
}
REMOTE_PAYLOAD_LOG_FILENAMES = {
    "remote_smbd_log_tail": "log.smbd",
    "remote_smbd_console_log_tail": "smbd-console.log",
    "remote_discovery_log_tail": "discovery.log",
}
SMBD_STATUS_HELPERS = rf'''
    RUNTIME_RAM_ROOT=${{RUNTIME_RAM_ROOT:-/mnt/Memory/samba4}}
    RUNTIME_RAM_SBIN="$RUNTIME_RAM_ROOT/sbin"
    RUNTIME_RAM_PRIVATE="$RUNTIME_RAM_ROOT/private"
    RUNTIME_SERVICE_BIN=${{RUNTIME_SERVICE_BIN:-/mnt/Flash/service}}
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
    has_ipv4=0
    has_ipv6=0
    while IFS= read -r line; do
        case "$line" in
            *smbd*" internet stream tcp "*\*:445|*smbd*" internet stream tcp "*"0.0.0.0:445") has_ipv4=1 ;;
            *smbd*" internet6 stream tcp "*\*:445|*smbd*" internet6 stream tcp "*"[::]:445"|*smbd*" internet6 stream tcp "*"[*]:445") has_ipv6=1 ;;
        esac
    done <<EOF
$fstat_out
EOF
    [ "$has_ipv4" -eq 1 ] && [ "$has_ipv6" -eq 1 ]
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
    if smbd_bound_445 "$fstat_out"; then
        echo "PASS:smbd owns IPv4 and IPv6 wildcard TCP 445 listeners"
    else
        echo "FAIL:smbd is missing an IPv4 or IPv6 wildcard TCP 445 listener"
        status=1
    fi
    if ! describe_runtime_smbd_version; then
        status=1
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
class DeployedVersionProbeResult:
    release_tag: str | None
    cli_version_code: int | None
    detail: str


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


def _hostname_first_label(value: str) -> str:
    return value.strip().split(".", 1)[0].strip()


def normalize_runtime_mdns_instance_name(value: str) -> str:
    normalized = "".join("-" if ord(char) < 0x20 or ord(char) == 0x7F else char for char in value)
    return _truncate_utf8(normalized.strip(), MAX_DNS_LABEL_BYTES)


def _normalize_runtime_mdns_host_label_text(value: str) -> str:
    candidate = value.strip().lower()
    normalized = re.sub(r"[^a-z0-9-]", "-", candidate).strip("-")
    normalized = _truncate_utf8(normalized, MAX_DNS_LABEL_BYTES).strip("-")
    return normalized


def normalize_runtime_mdns_host_label(value: str) -> str:
    return _normalize_runtime_mdns_host_label_text(_hostname_first_label(value))


def normalize_runtime_netbios_name(value: str) -> str:
    candidate = _hostname_first_label(value)
    normalized = re.sub(r"[^A-Za-z0-9_-]", "", candidate)
    if not re.search(r"[A-Za-z0-9]", normalized):
        return ""
    return _truncate_utf8(normalized, MAX_NETBIOS_NAME_BYTES)


def derive_runtime_naming_identity(system_name: str | None, hostname: str | None) -> RuntimeNamingIdentityProbeResult:
    raw_system_name = (system_name or "").strip() or None
    raw_hostname = (hostname or "").strip() or None

    mdns_host_label = normalize_runtime_mdns_host_label(raw_hostname or "")
    if not mdns_host_label:
        mdns_host_label = _normalize_runtime_mdns_host_label_text(raw_system_name or "")
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


def _fstat_has_udp_port(fstat_out: str, proc_name: str, family: str, port: int) -> bool:
    socket_family = "internet6" if family == "ipv6" else "internet"
    needle = f" {socket_family} dgram udp "
    port_suffix = f":{port}"
    for line in fstat_out.splitlines():
        if proc_name in line and needle in line and port_suffix in line:
            return True
    return False


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


def _fstat_5353_listeners(fstat_out: str) -> dict[str, set[str]]:
    """Process name -> socket families ("ipv4"/"ipv6") bound on UDP 5353."""
    listeners: dict[str, set[str]] = {}
    for line in fstat_out.splitlines():
        fields = line.split()
        if len(fields) < 2 or " dgram udp " not in line or ":5353" not in line:
            continue
        family = "ipv6" if " internet6 " in line else "ipv4" if " internet " in line else None
        if family is None:
            continue
        listeners.setdefault(fields[1], set()).add(family)
    return listeners


def _argv_has_pair(argv: list[str], flag: str, value: str) -> bool:
    return any(argv[i] == flag and argv[i + 1] == value for i in range(len(argv) - 1))


def _parse_link_plan(text: str) -> dict[str, object]:
    """Parses `--print-link-plan` output (guide C.2): status, mode, links with masks."""
    status = ""
    mode = ""
    reason = ""
    links: list[dict[str, str]] = []
    addresses: list[dict[str, str]] = []
    for raw_line in text.splitlines():
        kind, _, rest = raw_line.partition(": ")
        fields: dict[str, str] = {}
        try:
            tokens = shlex.split(rest) if rest else []
        except ValueError as e:
            # C.2: quoted fields escape `"` and `\\`; anything else is a
            # malformed line and a diagnostic, never an uncaught exception.
            fields["malformed"] = f"{raw_line!r}: {e}"
            tokens = []
        for token in tokens:
            key, _, value = token.partition("=")
            fields[key] = value
        if kind == "plan":
            status = fields.get("status", "")
            mode = fields.get("mode", "")
            reason = fields.get("reason", "")
        elif kind == "link":
            links.append(fields)
        elif kind == "addr":
            addresses.append(fields)
    return {"status": status, "mode": mode, "reason": reason, "links": links, "addresses": addresses}


def probe_managed_mdns_conn(
    connection: SshConnection,
    *,
    binary_timeout_seconds: int = MDNS_BINARY_PROBE_TIMEOUT_SECONDS,
    process_timeout_seconds: int = MDNS_PROCESS_TABLE_PROBE_TIMEOUT_SECONDS,
    plan_timeout_seconds: int = MDNS_SOCKET_FAMILIES_PROBE_TIMEOUT_SECONDS,
    fstat_timeout_seconds: int = MDNS_FSTAT_PROBE_TIMEOUT_SECONDS,
) -> ReadinessProbeResult:
    """v3.1.0 mDNS health (guide C.9): Apple's mDNSResponder is the only
    responder on the device, diskd runs on loopback, and our registrant is
    alive with a plan that grants SMB somewhere when a payload is active."""
    steps: list[ProbeStepResult] = []
    not_ready = "managed mDNS registrant not active"

    binary_script = r'''
RUNTIME_SERVICE_BIN=${RUNTIME_SERVICE_BIN:-/mnt/Flash/service}
if [ ! -e "$RUNTIME_SERVICE_BIN" ]; then
    echo "missing"
    exit 2
fi
if [ ! -x "$RUNTIME_SERVICE_BIN" ]; then
    echo "not_executable"
    exit 3
fi
echo "$RUNTIME_SERVICE_BIN"
'''
    binary_step, binary_proc = _run_timed_probe_step(
        connection,
        step_id="mdns_binary_probe",
        timeout_detail="mdns binary probe",
        script=binary_script,
        timeout_seconds=binary_timeout_seconds,
    )
    for _attempt in range(1, MDNS_BINARY_PROBE_ATTEMPTS):
        if binary_step.status != "timeout":
            break
        binary_step, binary_proc = _run_timed_probe_step(
            connection,
            step_id="mdns_binary_probe",
            timeout_detail="mdns binary probe",
            script=binary_script,
            timeout_seconds=binary_timeout_seconds,
        )
    if binary_step.status == "timeout":
        steps.append(binary_step)
        return _readiness_result_from_steps(ready=False, steps=steps, default_detail=not_ready)
    if binary_proc is None or binary_proc.returncode != 0:
        stdout = ("" if binary_proc is None else binary_proc.stdout).strip()
        if stdout == "missing":
            detail = "native service binary missing at /mnt/Flash/service"
        elif stdout == "not_executable":
            detail = "native service binary is not executable at /mnt/Flash/service"
        else:
            rc = "unknown" if binary_proc is None else str(binary_proc.returncode)
            detail = f"native service binary probe failed with exit code {rc}"
        _append_step(steps, "mdns_binary", "fail", detail)
        return _readiness_result_from_steps(ready=False, steps=steps, default_detail=not_ready)
    _append_step(steps, "mdns_binary", "pass", "native service binary is executable")

    ps_step, ps_proc = _run_timed_probe_step(
        connection,
        step_id="mdns_process_table_probe",
        timeout_detail="mDNS process table probe",
        script=PS_CAPTURE_COMMAND,
        timeout_seconds=process_timeout_seconds,
    )
    if ps_step.status == "timeout":
        steps.append(ps_step)
        return _readiness_result_from_steps(ready=False, steps=steps, default_detail=not_ready)
    ps_out = "" if ps_proc is None else ps_proc.stdout
    discovery_lines = service_role_lines(ps_out, "discovery")
    mdns_pids = [line.split()[0] for line in discovery_lines]
    apple_pids = _parse_live_pids_for_ucomm(ps_out, "mDNSResponder")
    diskd_lines = [line for line in ps_out.splitlines() if len(line.split()) >= 5 and line.split()[4] == "diskd" and not line.split()[2].startswith("Z")]
    # Same classification as the runtime's tc_apple_diskd_probe: a diskd is
    # ours only if its argv carries the `-i lo0` pair; every other live diskd
    # is ACPd's and advertises on the LAN regardless of ours (review 2, R8).
    loopback_diskd = [line for line in diskd_lines if _argv_has_pair(line.split()[5:], "-i", "lo0")]
    stray_diskd = [line for line in diskd_lines if line not in loopback_diskd]
    diskless = any("--diskless" in line.split() for line in discovery_lines)

    fstat_proc: subprocess.CompletedProcess[str] | None = None
    if apple_pids:
        _append_step(steps, "apple_mdns", "pass", "Apple mDNSResponder is running")
    else:
        # F11: nothing respawns it and a hand-started one lacks _airport.
        _append_step(steps, "apple_mdns", "fail", "Apple mDNSResponder is not running (reboot the device; it cannot be restarted by hand)")
    if loopback_diskd and not stray_diskd:
        _append_step(steps, "diskd_loopback", "pass", "Apple diskd runs on loopback (-i lo0)")
    elif stray_diskd:
        stray_pids = ", ".join(line.split()[0] for line in stray_diskd)
        _append_step(
            steps, "diskd_loopback", "fail",
            f"Apple diskd pid(s) {stray_pids} are not on loopback; their _smb/_adisk/_afpovertcp names may be visible"
            + (" (a loopback diskd also runs; the manager retries the cleanup every disk pass)" if loopback_diskd else ""),
        )
    else:
        _append_step(steps, "diskd_loopback", "fail", "Apple diskd is not running")
    if len(mdns_pids) == 1:
        _append_step(steps, "mdns_process", "pass", "discovery process is running")
    elif len(mdns_pids) > 1:
        _append_step(steps, "mdns_process", "fail", "multiple discovery processes are running")
    else:
        _append_step(steps, "mdns_process", "fail", "discovery process is not running")

    if apple_pids:
        fstat_script = "if [ ! -x /usr/bin/fstat ]; then echo fstat_missing; exit 127; fi; /usr/bin/fstat 2>/dev/null | /usr/bin/sed -n '/internet/p'; exit 0"
        fstat_step, fstat_proc = _run_timed_probe_step(
            connection,
            step_id="mdns_fstat_probe",
            timeout_detail="mdns fstat probe",
            script=fstat_script,
            timeout_seconds=fstat_timeout_seconds,
        )
        if fstat_step.status == "timeout":
            steps.append(fstat_step)
            return _readiness_result_from_steps(ready=False, steps=steps, default_detail=not_ready)
        if fstat_proc is None or fstat_proc.returncode == 127:
            _append_step(steps, "mdns_fstat", "fail", "fstat missing")
            return _readiness_result_from_steps(ready=False, steps=steps, default_detail=not_ready)
        listeners = _fstat_5353_listeners("" if fstat_proc is None else fstat_proc.stdout)
        apple_families = listeners.get("mDNSResponder", set())
        if apple_families >= {"ipv4", "ipv6"}:
            _append_step(steps, "apple_mdns_5353", "pass", "Apple mDNSResponder listens on UDP 5353 for IPv4 and IPv6")
        else:
            _append_step(steps, "apple_mdns_5353", "fail", "Apple mDNSResponder is not bound to UDP 5353 for both IPv4 and IPv6")
        others = sorted(name for name in listeners if name != "mDNSResponder")
        if others:
            _append_step(steps, "mdns_5353_exclusive", "fail", f"other processes hold UDP 5353: {' '.join(others)}")
        else:
            _append_step(steps, "mdns_5353_exclusive", "pass", "no other process holds UDP 5353")

    plan_script = r'''
RUNTIME_SERVICE_BIN=${RUNTIME_SERVICE_BIN:-/mnt/Flash/service}
"$RUNTIME_SERVICE_BIN" --print-link-plan
'''
    plan_step, plan_proc = _run_timed_probe_step(
        connection,
        step_id="mdns_link_plan_probe",
        timeout_detail="mdns link plan probe",
        script=plan_script,
        timeout_seconds=plan_timeout_seconds,
    )
    if plan_step.status == "timeout":
        steps.append(plan_step)
        return _readiness_result_from_steps(ready=False, steps=steps, default_detail=not_ready)
    if plan_proc is None or plan_proc.returncode != 0:
        rc = "unknown" if plan_proc is None else str(plan_proc.returncode)
        _append_step(steps, "mdns_link_plan", "fail", f"mdns link plan probe failed with exit code {rc}")
    else:
        plan = _parse_link_plan(plan_proc.stdout)
        status = str(plan["status"])
        links = plan["links"]
        addresses = plan["addresses"]
        smb_links = [link for link in links if "smb" in str(link.get("mask", "")).split(",")]
        smb_indexes = {str(link.get("index", "")) for link in smb_links}
        def service_ipv4(address: str) -> bool:
            try:
                parsed = ipaddress.IPv4Address(address)
            except ipaddress.AddressValueError:
                return False
            first = int(str(parsed).split(".", 1)[0])
            return 1 <= first <= 223 and first != 127

        smb_ipv4 = any(
            addr.get("family") == "inet"
            and addr.get("link") in smb_indexes
            and service_ipv4(str(addr.get("addr", "")))
            for addr in addresses
        )
        if status not in {"validated", "incomplete", "cold-start"}:
            _append_step(steps, "mdns_link_plan", "fail", "mdns link plan output could not be parsed")
        elif diskless:
            _append_step(steps, "mdns_link_plan", "pass", f"mdns link plan {status} (diskless; nothing advertised)")
        elif status != "validated":
            _append_step(steps, "mdns_link_plan", "fail", f"sharing facts are incomplete ({plan.get('reason') or 'unknown'}); the registrant waits or retains its previous validated policy")
        elif smb_links:
            names = " ".join(f"{link.get('name') or '?'}({link.get('role')})" for link in smb_links)
            _append_step(steps, "mdns_link_plan", "pass", f"mdns link plan {status} mode={plan['mode']}; SMB on {names}")
        else:
            _append_step(steps, "mdns_link_plan", "fail", f"mdns link plan {status} mode={plan['mode']} grants SMB on no link")

        title = discovery_lines[0] if len(discovery_lines) == 1 else ""
        marker = re.search(r"\bnbns=(disabled|waiting|starting|ready)\b", title)
        nbns_state = marker.group(1) if marker else ""
        eligible = not diskless and status == "validated" and smb_ipv4
        wcifsnd_lines = [
            line for line in ps_out.splitlines()
            if len(line.split()) >= 5 and line.split()[4] == "wcifsnd" and not line.split()[2].startswith("Z")
        ]
        controller_pid = mdns_pids[0] if len(mdns_pids) == 1 else ""
        owned_wcifsnd = [line for line in wcifsnd_lines if line.split()[1] == controller_pid]
        fstat_out = "" if fstat_proc is None else fstat_proc.stdout
        owned_fstat = "\n".join(
            line for line in fstat_out.splitlines()
            if len(line.split()) >= 3 and line.split()[1] == "wcifsnd" and line.split()[2] == (owned_wcifsnd[0].split()[0] if len(owned_wcifsnd) == 1 else "")
        )
        ports_ready = (
            _fstat_has_udp_port(owned_fstat, "wcifsnd", "ipv4", 137)
            and _fstat_has_udp_port(owned_fstat, "wcifsnd", "ipv4", 138)
        )
        if not marker:
            _append_step(steps, "native_nbns", "fail", "discovery NBNS state is not available yet")
        elif eligible and nbns_state == "starting":
            _append_step(steps, "native_nbns", "fail", "discovery native NBNS is still starting")
        elif eligible and nbns_state == "ready" and len(owned_wcifsnd) == 1 and len(wcifsnd_lines) == 1 and ports_ready:
            _append_step(steps, "native_nbns", "pass", "Apple wcifsnd is ready on UDP 137 and 138")
        elif eligible:
            _append_step(steps, "native_nbns", "fail", "discovery native NBNS is not ready")
        elif nbns_state in {"disabled", "waiting"} and not wcifsnd_lines:
            _append_step(steps, "native_nbns", "pass", f"native NBNS is {nbns_state}")
        else:
            _append_step(steps, "native_nbns", "fail", "discovery native NBNS state does not match the active plan")

    ready = all(step.status == "pass" for step in steps)
    return _readiness_result_from_steps(
        ready=ready,
        steps=steps,
        default_detail="managed mDNS registrant active" if ready else not_ready,
    )


def probe_managed_rsync_conn(
    connection: SshConnection,
    *,
    timeout_seconds: int = REMOTE_STATE_PROBE_TIMEOUT_SECONDS,
) -> ReadinessProbeResult:
    payload_dir = read_runtime_payload_dir_conn(connection, timeout_seconds=timeout_seconds) or ""
    script = rf'''
RUNTIME_CONFIG_FILE=${{RUNTIME_CONFIG_FILE:-{FLASH_RUNTIME_CONFIG}}}
RUNTIME_RSYNC_BIN=${{RUNTIME_RSYNC_BIN:-{RUNTIME_RSYNC_BIN}}}
RUNTIME_RSYNC_CONF=${{RUNTIME_RSYNC_CONF:-{RUNTIME_RSYNC_CONF}}}
RSYNC_ENABLED=0
RUNTIME_PAYLOAD_DIR={shlex.quote(payload_dir)}
if [ -f "$RUNTIME_CONFIG_FILE" ]; then
    . "$RUNTIME_CONFIG_FILE"
fi
case "$RSYNC_ENABLED" in
    1|true|TRUE|yes|YES) RSYNC_ENABLED=1 ;;
    *) RSYNC_ENABLED=0 ;;
esac

status=0
if [ -n "$RUNTIME_PAYLOAD_DIR" ] && [ -x "$RUNTIME_PAYLOAD_DIR/rsync" ]; then
    echo "PASS:persistent rsync binary is executable"
else
    echo "FAIL:persistent rsync binary is missing"
    status=1
fi
if [ -n "$RUNTIME_PAYLOAD_DIR" ] && [ -r "$RUNTIME_PAYLOAD_DIR/rsyncd.conf" ]; then
    echo "PASS:persistent rsync config is present"
else
    echo "FAIL:persistent rsync config is missing"
    status=1
fi

rsync_pids=
if ps_out=$(/bin/ps axww -o pid= -o stat= -o ucomm= -o command= 2>/dev/null); then
    old_ifs=$IFS
    IFS='
'
    for line in $ps_out; do
        [ -n "$line" ] || continue
        line_ifs=$IFS
        IFS=' 	'
        set -- $line
        IFS=$line_ifs
        [ "$#" -ge 3 ] || continue
        case "$2" in Z*) continue ;; esac
        [ "$3" = rsync ] || continue
        rsync_pids="$rsync_pids $1"
    done
    IFS=$old_ifs
fi

if [ "$RSYNC_ENABLED" != "1" ]; then
    if [ -n "$rsync_pids" ]; then
        echo "FAIL:rsync daemon is disabled but an rsync process is running"
        status=1
    else
        echo "SKIP:rsync daemon is disabled and not running"
    fi
    exit "$status"
fi

if [ -x "$RUNTIME_RSYNC_BIN" ]; then
    echo "PASS:managed rsync binary is executable in RAM"
else
    echo "FAIL:managed rsync binary is missing from RAM"
    status=1
fi
if [ -r "$RUNTIME_RSYNC_CONF" ]; then
    echo "PASS:managed rsync config is present in RAM"
else
    echo "FAIL:managed rsync config is missing from RAM"
    status=1
fi
if [ -n "$rsync_pids" ]; then
    echo "PASS:managed rsync process is running"
else
    echo "FAIL:managed rsync process is not running"
    status=1
fi

rsync_bound=0
for rsync_pid in $rsync_pids; do
    if fstat_out=$(/usr/bin/fstat -p "$rsync_pid" 2>/dev/null); then
        fstat_ifs=$IFS
        IFS='
'
        for fstat_line in $fstat_out; do
            case "$fstat_line" in
                *" internet stream tcp "*":873"*|*" internet6 stream tcp "*":873"*) rsync_bound=1 ;;
            esac
        done
        IFS=$fstat_ifs
    fi
done
if [ "$rsync_bound" -eq 1 ]; then
    echo "PASS:managed rsync is bound to TCP 873"
else
    echo "FAIL:managed rsync is not bound to TCP 873"
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
        return _readiness_result_from_lines(
            ready=False,
            lines=("FAIL:managed rsync readiness probe timed out",),
            default_detail="managed rsync not ready",
        )
    lines = _probe_lines(proc.stdout)
    return _readiness_result_from_lines(
        ready=proc.returncode == 0,
        lines=lines,
        default_detail="managed rsync ready" if proc.returncode == 0 else "managed rsync not ready",
    )


@dataclass(frozen=True)
class UsbPrinterProbeResult:
    """What ACP's `prni` says about a USB printer (guide G6, release gate).

    `present` means a printer entry with `pluggedIn=true`; `name` is its
    ACP name (what Apple's printd advertises the queue as); `error` is set
    when the read itself failed, which is distinct from "no printer"."""
    present: bool
    name: str | None
    error: str | None = None
    make: str | None = None
    model: str | None = None


def _prni_string_value(raw: str) -> str:
    value = raw.strip()
    if value.startswith('"') and value.endswith('"') and len(value) >= 2:
        value = value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return value


def parse_prni_printers(text: str) -> UsbPrinterProbeResult:
    """Parse `acp -A prni` output: `printers=[ { key=value ... } ... ]`.
    String values are quoted on NetBSD 6 and bare on NetBSD 4."""
    entry: dict[str, str] = {}
    entries: list[dict[str, str]] = []
    depth = 0
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("{"):
            depth += 1
            if depth >= 2:
                entry = {}
            continue
        if stripped.startswith("}"):
            if depth >= 2 and entry:
                entries.append(entry)
                entry = {}
            depth = max(0, depth - 1)
            continue
        key, sep, value = stripped.partition("=")
        if sep and depth >= 2:
            entry[key.strip()] = value
    for candidate in entries:
        if candidate.get("pluggedIn", "").strip() != "true":
            continue
        name = _prni_string_value(candidate.get("name", ""))
        if not name:
            continue
        return UsbPrinterProbeResult(
            present=True,
            name=name,
            make=_prni_string_value(candidate.get("make", "")) or None,
            model=_prni_string_value(candidate.get("model", "")) or None,
        )
    return UsbPrinterProbeResult(present=False, name=None)


def probe_usb_printer_conn(connection: SshConnection, *, timeout_seconds: int = REMOTE_STATE_PROBE_TIMEOUT_SECONDS) -> UsbPrinterProbeResult:
    try:
        proc = run_ssh(connection, "/usr/bin/acp -A prni 2>/dev/null", check=False, timeout=timeout_seconds)
    except SshCommandTimeout:
        return UsbPrinterProbeResult(present=False, name=None, error="acp -A prni timed out")
    if proc.returncode != 0:
        return UsbPrinterProbeResult(present=False, name=None, error=f"acp -A prni exited {proc.returncode}")
    return parse_prni_printers(proc.stdout or "")


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


def _managed_runtime_detail(
    smbd: ReadinessProbeResult,
    mdns: ReadinessProbeResult,
    rsync: ReadinessProbeResult | None = None,
) -> str:
    details = tuple(detail for detail in (smbd.detail, mdns.detail, None if rsync is None else rsync.detail) if detail)
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
    mdns = probe_managed_mdns_conn(connection)
    rsync = probe_managed_rsync_conn(connection)

    if smbd.ready and mdns.ready and rsync.ready:
        time.sleep(mdns_settle_seconds)
        settled_mdns = probe_managed_mdns_conn(connection)
        if settled_mdns.ready:
            return ManagedRuntimeProbeResult(
                ready=True,
                detail="managed runtime is ready",
                smbd=smbd,
                mdns=settled_mdns,
                extra_steps=rsync.steps + (
                    ProbeStepResult(
                        id="mdns_settle",
                        status="pass",
                        detail="mdns remained healthy after settle delay",
                    ),
                ),
            )
        mdns = ReadinessProbeResult(
            ready=False,
            detail=f"{settled_mdns.detail}; mdns did not survive settle delay",
            steps=settled_mdns.steps + (
                ProbeStepResult(
                    id="mdns_settle",
                    status="fail",
                    detail="mdns did not remain healthy after settle delay",
                ),
            ),
        )

    return ManagedRuntimeProbeResult(
        ready=False,
        detail=_managed_runtime_detail(smbd, mdns, rsync),
        smbd=smbd,
        mdns=mdns,
        extra_steps=rsync.steps,
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
            mdns=ReadinessProbeResult(ready=False, detail="managed mDNS registrant not active"),
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


MANAGER_ELAPSED_PS_COMMAND = "/bin/ps axww -o pid= -o ppid= -o stat= -o etime= -o ucomm= -o command="


@dataclass(frozen=True)
class ManagerStartupAgeProbeResult:
    manager_started_seconds_ago: float | None
    detail: str


def _parse_ps_elapsed(value: str) -> int | None:
    # BSD ps etime is [[dd-]hh:]mm:ss.
    days, separator, clock = value.rpartition("-")
    parts = clock.split(":")
    if not 2 <= len(parts) <= 3 or not all(part.isdigit() for part in parts):
        return None
    if separator and not days.isdigit():
        return None
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + int(part)
    return seconds + int(days or 0) * 86400


def probe_manager_startup_age_conn(connection: SshConnection) -> ManagerStartupAgeProbeResult:
    # The kernel's elapsed time for the live manager process is the startup
    # age: no log, state file or device clock is involved. boot.sh execs the
    # service binary, so the count includes boot.sh's brief preparation.
    try:
        proc = run_ssh(
            connection,
            MANAGER_ELAPSED_PS_COMMAND,
            check=False,
            timeout=REMOTE_STATE_PROBE_TIMEOUT_SECONDS,
        )
    except SshCommandTimeout:
        return ManagerStartupAgeProbeResult(None, "manager startup age probe timed out")
    if proc.returncode != 0:
        return ManagerStartupAgeProbeResult(None, f"manager startup age probe failed (rc={proc.returncode})")
    rows = service_role_lines(proc.stdout or "", "manager")
    if not rows:
        return ManagerStartupAgeProbeResult(None, "manager is not running")
    if len(rows) > 1:
        return ManagerStartupAgeProbeResult(None, f"{len(rows)} manager processes are running")
    seconds_ago = _parse_ps_elapsed(rows[0].split()[3])
    if seconds_ago is None:
        return ManagerStartupAgeProbeResult(None, "manager startup age probe output unparseable")
    return ManagerStartupAgeProbeResult(float(seconds_ago), f"manager started {seconds_ago}s ago")


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
    return logs


def read_remote_service_socket_diagnostics_conn(connection: SshConnection) -> str:
    script = rf'''
{SMBD_STATUS_HELPERS}
if [ ! -x /usr/bin/fstat ]; then
    echo "fstat missing"
    exit 0
fi
ps_out="$(capture_ps_out)"
for proc_name in smbd wcifsnd rsync; do
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
    "$RUNTIME_RAM_SBIN/rsync" \
    "$RUNTIME_RAM_PRIVATE/smbpasswd" \
    "$RUNTIME_RAM_PRIVATE/username.map" \
    "$RUNTIME_RAM_ETC/smb.conf" \
    "$RUNTIME_RAM_ETC/rsyncd.conf" \
    "$RUNTIME_RAM_VAR/rsync.log"
do
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
