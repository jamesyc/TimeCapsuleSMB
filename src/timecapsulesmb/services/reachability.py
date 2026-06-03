from __future__ import annotations

import ipaddress
import shutil
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Callable

from timecapsulesmb.core.config import DEFAULTS, AppConfig
from timecapsulesmb.core.net import canonical_ssh_target, endpoint_host, parse_endpoint, resolve_host_ips
from timecapsulesmb.transport.errors import TransportError
from timecapsulesmb.transport.local import tcp_connect_error
from timecapsulesmb.transport.ssh import SshCommandTimeout, SshConnection, run_ssh, ssh_opts_use_proxy


REACHABILITY_OK_TOKEN = "timecapsulesmb-reachability-ok"


@dataclass(frozen=True)
class ReachabilityCheck:
    id: str
    status: str
    message: str
    host: str | None = None
    detail: str | None = None


@dataclass(frozen=True)
class ReachabilityResult:
    status: str
    summary: str
    ssh_host: str | None
    smb_host: str | None
    checks: list[ReachabilityCheck] = field(default_factory=list)


def run_reachability(
    config: AppConfig,
    params: Mapping[str, object],
    *,
    password: str = "",
    stage: Callable[[str], None] | None = None,
) -> ReachabilityResult:
    emit_stage(stage, "build_candidates")
    ssh_target = ssh_target_from_params(config, params)
    ssh_host = endpoint_host(ssh_target)
    smb_hosts = smb_hosts_from_params(config, params, ssh_host=ssh_host)
    ping_hosts = unique_hosts([ssh_host, *smb_hosts])
    tcp_timeout = non_negative_float(params.get("tcp_timeout"), default=2.0)
    ssh_timeout = non_negative_int(params.get("ssh_timeout"), default=8)

    if not ssh_host and not smb_hosts:
        check = ReachabilityCheck(
            id="candidates",
            status="SKIP",
            message="No saved host candidates were available.",
        )
        return ReachabilityResult(
            status="skipped",
            summary="No saved host candidates were available.",
            ssh_host=ssh_target or None,
            smb_host=None,
            checks=[check],
        )

    checks: list[ReachabilityCheck] = []
    emit_stage(stage, "check_dns")
    checks.append(check_dns(ping_hosts))
    emit_stage(stage, "check_ping")
    checks.append(check_ping(ping_hosts, timeout=tcp_timeout))
    emit_stage(stage, "check_ssh_port")
    ssh_port = check_ssh_port(ssh_host, config, timeout=tcp_timeout)
    checks.append(ssh_port)
    emit_stage(stage, "check_ssh_auth")
    ssh_auth = check_ssh_auth(
        ssh_target,
        config,
        password=password,
        port_check=ssh_port,
        timeout=ssh_timeout,
    )
    checks.append(ssh_auth)
    emit_stage(stage, "check_smb_port")
    smb_port = check_smb_port(smb_hosts, timeout=tcp_timeout)
    checks.append(smb_port)

    return result_from_checks(ssh_target=ssh_target, smb_hosts=smb_hosts, checks=checks)


def ssh_target_from_params(config: AppConfig, params: Mapping[str, object]) -> str:
    for key in ("ssh_host", "host"):
        value = string_value(params.get(key))
        if value:
            return root_ssh_target(value)
    return config.get("TC_HOST", "")


def smb_hosts_from_params(config: AppConfig, params: Mapping[str, object], *, ssh_host: str) -> list[str]:
    candidates: list[str] = []
    add_param_hosts(candidates, params.get("smb_hosts"))
    add_param_hosts(candidates, params.get("smb_host"))
    add_param_hosts(candidates, params.get("hosts"))
    add_param_hosts(candidates, params.get("host"))
    if config.has_value("TC_HOST"):
        candidates.append(endpoint_host(config.get("TC_HOST")))
    if ssh_host:
        candidates.append(ssh_host)
    return unique_hosts(candidates)


def add_param_hosts(candidates: list[str], value: object) -> None:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            candidates.append(endpoint_host(string_value(item)))
        return
    candidates.append(endpoint_host(string_value(value)))


def root_ssh_target(value: str) -> str:
    try:
        return canonical_ssh_target(value)
    except ValueError:
        endpoint = parse_endpoint(value)
        if not endpoint.host:
            return value.strip()
        user = endpoint.user or "root"
        return f"{user}@{endpoint.host}"


def check_dns(hosts: Sequence[str]) -> ReachabilityCheck:
    if not hosts:
        return ReachabilityCheck(id="dns", status="SKIP", message="No hosts were available for DNS resolution.")

    resolved: list[str] = []
    failures: list[str] = []
    for host in hosts:
        if is_ip_literal(host):
            resolved.append(host)
            continue
        ips = resolve_host_ips(host)
        if ips:
            resolved.append(f"{host} -> {', '.join(ips)}")
        else:
            failures.append(host)

    if resolved:
        return ReachabilityCheck(
            id="dns",
            status="PASS",
            message="Host resolution succeeded.",
            host=hosts[0],
            detail="; ".join(resolved),
        )
    return ReachabilityCheck(
        id="dns",
        status="FAIL",
        message="Host resolution failed.",
        host=hosts[0],
        detail=", ".join(failures) if failures else None,
    )


def check_ping(hosts: Sequence[str], *, timeout: float) -> ReachabilityCheck:
    if not hosts:
        return ReachabilityCheck(id="ping", status="SKIP", message="No hosts were available for ping.")

    failures: list[str] = []
    for host in hosts:
        ping = ping_command(host)
        if ping is None:
            return ReachabilityCheck(id="ping", status="SKIP", message="No ping command is available.")
        try:
            proc = subprocess.run(
                ping,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=max(1.0, timeout + 1.0),
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            failures.append(f"{host}: {type(exc).__name__}")
            continue
        if proc.returncode == 0:
            return ReachabilityCheck(
                id="ping",
                status="PASS",
                message="Host responds to ping.",
                host=host,
            )
        error = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
        failures.append(f"{host}: {error or f'rc={proc.returncode}'}")

    return ReachabilityCheck(
        id="ping",
        status="FAIL",
        message="Host did not respond to ping.",
        host=hosts[0],
        detail="; ".join(failures),
    )


def ping_command(host: str) -> list[str] | None:
    command_name = "ping6" if is_ipv6_literal(host) else "ping"
    command = shutil.which(command_name)
    if command is None and command_name == "ping6":
        command = shutil.which("ping")
    if command is None:
        return None
    return [command, "-c", "1", host]


def check_ssh_port(host: str, config: AppConfig, *, timeout: float) -> ReachabilityCheck:
    if not host:
        return ReachabilityCheck(id="ssh_port", status="SKIP", message="No SSH host is configured.")
    ssh_opts = config.get("TC_SSH_OPTS", DEFAULTS["TC_SSH_OPTS"])
    if ssh_opts_use_proxy(ssh_opts):
        return ReachabilityCheck(
            id="ssh_port",
            status="SKIP",
            message="Direct SSH port check skipped because SSH uses a proxy.",
            host=host,
        )
    error = tcp_connect_error(host, 22, timeout=timeout)
    if error is None:
        return ReachabilityCheck(id="ssh_port", status="PASS", message="SSH port is reachable.", host=host)
    return ReachabilityCheck(
        id="ssh_port",
        status="FAIL",
        message="SSH port is not reachable.",
        host=host,
        detail=error,
    )


def check_ssh_auth(
    ssh_target: str,
    config: AppConfig,
    *,
    password: str,
    port_check: ReachabilityCheck,
    timeout: int,
) -> ReachabilityCheck:
    if not ssh_target:
        return ReachabilityCheck(id="ssh_auth", status="SKIP", message="No SSH target is configured.")
    if not password:
        return ReachabilityCheck(id="ssh_auth", status="SKIP", message="SSH authentication skipped because no password is available.")
    if port_check.status == "FAIL":
        return ReachabilityCheck(id="ssh_auth", status="SKIP", message="SSH authentication skipped because the SSH port is closed.")

    connection = SshConnection(
        host=ssh_target,
        password=password,
        ssh_opts=config.get("TC_SSH_OPTS", DEFAULTS["TC_SSH_OPTS"]),
    )
    try:
        proc = run_ssh(
            connection,
            f"/bin/sh -c 'printf {REACHABILITY_OK_TOKEN}'",
            check=False,
            timeout=timeout,
        )
    except (TransportError, SshCommandTimeout) as exc:
        return ReachabilityCheck(
            id="ssh_auth",
            status="FAIL",
            message="SSH authentication failed.",
            host=endpoint_host(ssh_target),
            detail=str(exc),
        )
    if proc.returncode == 0 and proc.stdout.strip().endswith(REACHABILITY_OK_TOKEN):
        return ReachabilityCheck(
            id="ssh_auth",
            status="PASS",
            message="SSH authentication worked.",
            host=endpoint_host(ssh_target),
        )
    return ReachabilityCheck(
        id="ssh_auth",
        status="FAIL",
        message="SSH authentication failed.",
        host=endpoint_host(ssh_target),
        detail=proc.stdout.strip() or f"rc={proc.returncode}",
    )


def check_smb_port(hosts: Sequence[str], *, timeout: float) -> ReachabilityCheck:
    if not hosts:
        return ReachabilityCheck(id="smb_port", status="SKIP", message="No SMB hosts are configured.")

    failures: list[str] = []
    for host in hosts:
        error = tcp_connect_error(host, 445, timeout=timeout)
        if error is None:
            return ReachabilityCheck(id="smb_port", status="PASS", message="SMB port is reachable.", host=host)
        failures.append(f"{host}: {error}")

    return ReachabilityCheck(
        id="smb_port",
        status="FAIL",
        message="SMB port is not reachable.",
        host=hosts[0],
        detail="; ".join(failures),
    )


def result_from_checks(
    *,
    ssh_target: str,
    smb_hosts: Sequence[str],
    checks: Sequence[ReachabilityCheck],
) -> ReachabilityResult:
    by_id = {check.id: check for check in checks}
    ssh_signal = by_id.get("ssh_auth") and by_id["ssh_auth"].status == "PASS"
    if not ssh_signal:
        ssh_signal = by_id.get("ssh_port") and by_id["ssh_port"].status == "PASS"
    smb_signal = by_id.get("smb_port") and by_id["smb_port"].status == "PASS"

    if ssh_signal and smb_signal:
        status = "reachable"
        summary = "SSH reachable; SMB port reachable."
    elif ssh_signal and not smb_signal:
        status = "partial"
        summary = "SSH reachable, SMB port closed."
    elif smb_signal and not ssh_signal:
        status = "partial"
        summary = "SMB port reachable, SSH closed."
    else:
        status = "unreachable"
        summary = "Could not reach SSH or SMB."

    smb_host = None
    smb_check = by_id.get("smb_port")
    if smb_check is not None and smb_check.status == "PASS":
        smb_host = smb_check.host
    elif smb_hosts:
        smb_host = smb_hosts[0]

    return ReachabilityResult(
        status=status,
        summary=summary,
        ssh_host=ssh_target or None,
        smb_host=smb_host,
        checks=list(checks),
    )


def unique_hosts(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    hosts: list[str] = []
    for raw in values:
        host = endpoint_host(raw)
        if not host:
            continue
        key = host.lower()
        if key in seen:
            continue
        seen.add(key)
        hosts.append(host)
    return hosts


def is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.split("%", 1)[0])
        return True
    except ValueError:
        return False


def is_ipv6_literal(host: str) -> bool:
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).version == 6
    except ValueError:
        return False


def string_value(value: object) -> str:
    return "" if value is None else str(value).strip()


def non_negative_float(value: object, *, default: float) -> float:
    if value in (None, ""):
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if parsed < 0:
        return default
    return parsed


def non_negative_int(value: object, *, default: int) -> int:
    if value in (None, ""):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    if parsed < 0:
        return default
    return parsed


def emit_stage(stage: Callable[[str], None] | None, name: str) -> None:
    if stage is not None:
        stage(name)
