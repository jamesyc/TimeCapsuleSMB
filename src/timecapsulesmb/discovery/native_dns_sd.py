from __future__ import annotations

import ipaddress
import platform
import re
import subprocess
import time
from dataclasses import dataclass, field

from timecapsulesmb.discovery.bonjour import (
    BonjourDiscoverySnapshot,
    BonjourIPFamily,
    BonjourResolvedService,
    BonjourServiceInstance,
    DEFAULT_BROWSE_TIMEOUT_SEC,
    SERVICE_TYPES,
)
from timecapsulesmb.transport.local import command_exists


DEFAULT_DNS_SD_SERVICE_TYPES = [
    "_smb._tcp",
    "_adisk._tcp",
    "_airport._tcp",
    "_afpovertcp._tcp",
    "_device-info._tcp",
]


@dataclass
class NativeDnsSdServiceEvent:
    service_type: str
    action: str
    interface_index: int | None
    flags: str
    domain: str
    name: str


@dataclass
class NativeDnsSdBrowseResult:
    service_type: str
    events: list[NativeDnsSdServiceEvent] = field(default_factory=list)
    parse_error_count: int = 0
    stderr: str = ""
    exit_code: int | None = None
    terminated_after_timeout: bool = False
    error: str = ""


@dataclass
class NativeDnsSdDiagnostics:
    timeout_sec: float
    elapsed_sec: float
    status: str
    browses: list[NativeDnsSdBrowseResult]


@dataclass
class NativeDnsSdAddressResult:
    hostname: str
    family: str
    addresses: list[str] = field(default_factory=list)
    stderr: str = ""
    exit_code: int | None = None
    terminated_after_timeout: bool = False
    error: str = ""


@dataclass
class NativeDnsSdResolveResult:
    service_type: str
    name: str
    fullname: str = ""
    hostname: str = ""
    port: int = 0
    interface_index: int | None = None
    addresses: list[NativeDnsSdAddressResult] = field(default_factory=list)
    stderr: str = ""
    exit_code: int | None = None
    terminated_after_timeout: bool = False
    error: str = ""


@dataclass
class NativeDnsSdDiscoveryDiagnostics:
    timeout_sec: float
    elapsed_sec: float
    status: str
    service_types: list[str]
    ip_version: str
    instance_count: int
    resolved_count: int
    browses: list[NativeDnsSdBrowseResult]
    resolves: list[NativeDnsSdResolveResult] = field(default_factory=list)
    error: str = ""


def native_dns_sd_available(*, platform_name: str | None = None) -> bool:
    if (platform_name or platform.system()) != "Darwin":
        return False
    return command_exists("dns-sd")


def _normalize_dns_sd_service_type(service_type: str) -> str:
    value = service_type.strip()
    for suffix in (".local.", ".local"):
        if value.endswith(suffix):
            value = value[: -len(suffix)]
    return value.rstrip(".")


def _dns_sd_service_type_domain(service_type: str, domain: str = "local.") -> str:
    normalized = _normalize_dns_sd_service_type(service_type)
    normalized_domain = (domain or "local.").strip().strip(".") or "local"
    return f"{normalized}.{normalized_domain}."


def _matching_dns_sd_service_types(service: str | None) -> list[str]:
    if not service:
        return list(SERVICE_TYPES)
    normalized_service = service.strip()
    if normalized_service.startswith("_") and "._tcp" not in normalized_service and "._udp" not in normalized_service:
        prefix = f"{normalized_service}."
        matches = [service_type for service_type in SERVICE_TYPES if service_type.startswith(prefix)]
        if matches:
            return matches
    if normalized_service.endswith("."):
        return [normalized_service]
    if "._tcp.local" in normalized_service or "._udp.local" in normalized_service:
        return [f"{normalized_service}."]
    return [_dns_sd_service_type_domain(normalized_service)]


def _parse_dns_sd_browse_output(service_type: str, stdout: str) -> tuple[list[NativeDnsSdServiceEvent], int]:
    events: list[NativeDnsSdServiceEvent] = []
    parse_error_count = 0
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if (
            stripped.startswith("Browsing for ")
            or stripped.startswith("DATE:")
            or stripped.startswith("Timestamp")
            or "...STARTING..." in stripped
        ):
            continue
        parts = stripped.split(None, 6)
        if len(parts) < 7:
            parse_error_count += 1
            continue
        _timestamp, action, flags, iface, domain, observed_service_type, name = parts
        try:
            interface_index = int(iface)
        except ValueError:
            interface_index = None
        events.append(
            NativeDnsSdServiceEvent(
                service_type=observed_service_type.rstrip(".") or service_type,
                action=action,
                interface_index=interface_index,
                flags=flags,
                domain=domain,
                name=name.strip(),
            )
        )
    return events, parse_error_count


def _run_dns_sd_command(args: list[str], *, timeout_sec: float) -> tuple[str, str, int | None, bool, str]:
    try:
        proc = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as e:
        return "", "", None, False, f"{type(e).__name__}: {e}"

    deadline = time.monotonic() + max(0.0, timeout_sec)
    while proc.poll() is None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(0.1, remaining))

    terminated_after_timeout = proc.poll() is None
    if terminated_after_timeout:
        proc.terminate()
    try:
        stdout, stderr = proc.communicate(timeout=1)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
    return stdout, stderr, proc.returncode, terminated_after_timeout, ""


def _parse_reached_line(line: str) -> tuple[str, str, int, int | None] | None:
    marker = " can be reached at "
    if marker not in line:
        return None
    left, right = line.split(marker, 1)
    left = left.strip()
    parts = left.split(None, 1)
    fullname = parts[1] if len(parts) > 1 and re.match(r"^\d\d:\d\d:\d\d(?:\.\d+)?$", parts[0]) else left

    host_port = right.split(" (", 1)[0].strip()
    if ":" not in host_port:
        return None
    host, port_text = host_port.rsplit(":", 1)
    try:
        port = int(port_text)
    except ValueError:
        return None

    interface_index = None
    interface_match = re.search(r"\(interface\s+(\d+)\)", right)
    if interface_match:
        interface_index = int(interface_match.group(1))

    return fullname.strip(), host.strip().rstrip("."), port, interface_index


def _parse_dns_sd_lookup_output(service_type: str, name: str, stdout: str) -> tuple[str, str, int, int | None]:
    for line in stdout.splitlines():
        parsed = _parse_reached_line(line.strip())
        if parsed is not None:
            return parsed
    return f"{name}.{_dns_sd_service_type_domain(service_type)}", "", 0, None


def _append_ip(values: list[str], candidate: str) -> None:
    cleaned = candidate.strip().rstrip(",")
    if not cleaned:
        return
    try:
        address = ipaddress.ip_address(cleaned.split("%", 1)[0])
    except ValueError:
        return
    value = str(address)
    if "%" in cleaned and address.version == 6:
        value = cleaned
    if value not in values:
        values.append(value)


def _parse_dns_sd_address_output(stdout: str) -> list[str]:
    addresses: list[str] = []
    for line in stdout.splitlines():
        stripped = line.strip()
        if (
            not stripped
            or stripped.startswith("DATE:")
            or stripped.startswith("Timestamp")
            or "...STARTING..." in stripped
        ):
            continue
        for part in reversed(stripped.split()):
            _append_ip(addresses, part)
    return addresses


def _dns_sd_address_families(family: BonjourIPFamily | None) -> list[str]:
    if family == "ipv6":
        return ["v6"]
    if family == "ipv4":
        return ["v4"]
    return ["v4", "v6"]


def _native_ip_version_name(family: BonjourIPFamily | None) -> str:
    if family == "ipv6":
        return "V6Only"
    if family == "ipv4":
        return "V4Only"
    return "Split"


def resolve_native_dns_sd_service_instance(
    service_type: str,
    name: str,
    *,
    timeout_sec: float = DEFAULT_BROWSE_TIMEOUT_SEC,
    family: BonjourIPFamily | None = None,
) -> tuple[BonjourResolvedService | None, NativeDnsSdResolveResult]:
    normalized_service_type = _normalize_dns_sd_service_type(service_type)
    stdout, stderr, exit_code, terminated_after_timeout, start_error = _run_dns_sd_command(
        ["dns-sd", "-L", name, normalized_service_type, "local"],
        timeout_sec=timeout_sec,
    )
    if start_error:
        return None, NativeDnsSdResolveResult(service_type=normalized_service_type, name=name, error=start_error)

    fullname, hostname, port, interface_index = _parse_dns_sd_lookup_output(normalized_service_type, name, stdout)
    result = NativeDnsSdResolveResult(
        service_type=normalized_service_type,
        name=name,
        fullname=fullname,
        hostname=hostname,
        port=port,
        interface_index=interface_index,
        stderr=stderr.strip(),
        exit_code=exit_code,
        terminated_after_timeout=terminated_after_timeout,
    )
    if not hostname or not port:
        result.error = "dns-sd lookup did not resolve a service target"
        return None, result

    ipv4: list[str] = []
    ipv6: list[str] = []
    for address_family in _dns_sd_address_families(family):
        address_stdout, address_stderr, address_exit, address_terminated, address_error = _run_dns_sd_command(
            ["dns-sd", "-G", address_family, hostname],
            timeout_sec=timeout_sec,
        )
        addresses = [] if address_error else _parse_dns_sd_address_output(address_stdout)
        address_result = NativeDnsSdAddressResult(
            hostname=hostname,
            family=address_family,
            addresses=addresses,
            stderr=address_stderr.strip(),
            exit_code=address_exit,
            terminated_after_timeout=address_terminated,
            error=address_error,
        )
        result.addresses.append(address_result)
        for address in addresses:
            try:
                parsed = ipaddress.ip_address(address.split("%", 1)[0])
            except ValueError:
                continue
            (ipv6 if parsed.version == 6 else ipv4).append(address)

    record = BonjourResolvedService(
        name=name,
        hostname=hostname,
        service_type=_dns_sd_service_type_domain(normalized_service_type),
        port=port,
        ipv4=ipv4,
        ipv6=ipv6,
        fullname=fullname,
    )
    return record, result


def discover_native_dns_sd_snapshot_detailed(
    service: str | None = None,
    timeout_sec: float = DEFAULT_BROWSE_TIMEOUT_SEC,
    *,
    target_ip: str | None = None,
    family: BonjourIPFamily | None = None,
    platform_name: str | None = None,
) -> tuple[BonjourDiscoverySnapshot, NativeDnsSdDiscoveryDiagnostics] | None:
    del target_ip
    if not native_dns_sd_available(platform_name=platform_name):
        return None

    service_types = _matching_dns_sd_service_types(service)
    start = time.monotonic()
    browse_diagnostics = browse_native_dns_sd(service_types, timeout_sec=timeout_sec, platform_name=platform_name)
    if browse_diagnostics is None:
        return None

    instances: dict[tuple[str, str], BonjourServiceInstance] = {}
    records: dict[tuple[str, str], BonjourResolvedService] = {}
    resolves: list[NativeDnsSdResolveResult] = []
    seen: set[tuple[str, str]] = set()

    for browse in browse_diagnostics.browses:
        for event in browse.events:
            if event.action.lower() != "add" or not event.name:
                continue
            service_type = _dns_sd_service_type_domain(event.service_type or browse.service_type, event.domain)
            fullname = f"{event.name}.{service_type}"
            instance_key = (service_type, fullname)
            instances.setdefault(
                instance_key,
                BonjourServiceInstance(
                    service_type=service_type,
                    name=event.name,
                    fullname=fullname,
                ),
            )

            resolve_key = (_normalize_dns_sd_service_type(service_type), event.name)
            if resolve_key in seen:
                continue
            seen.add(resolve_key)
            record, resolve_result = resolve_native_dns_sd_service_instance(
                service_type,
                event.name,
                timeout_sec=timeout_sec,
                family=family,
            )
            resolves.append(resolve_result)
            if record is not None:
                records[(record.service_type, record.fullname or record.name)] = record

    snapshot = BonjourDiscoverySnapshot(
        instances=sorted(instances.values(), key=lambda instance: (instance.service_type, instance.name, instance.fullname)),
        resolved=sorted(records.values(), key=lambda record: (record.service_type, record.name, record.hostname)),
    )
    status = "ok" if browse_diagnostics.status == "ok" else "error"
    diagnostics = NativeDnsSdDiscoveryDiagnostics(
        timeout_sec=timeout_sec,
        elapsed_sec=round(time.monotonic() - start, 3),
        status=status,
        service_types=list(service_types),
        ip_version=_native_ip_version_name(family),
        instance_count=len(snapshot.instances),
        resolved_count=len(snapshot.resolved),
        browses=browse_diagnostics.browses,
        resolves=resolves,
    )
    return snapshot, diagnostics


def browse_native_dns_sd(
    service_types: list[str] | None = None,
    *,
    timeout_sec: float = DEFAULT_BROWSE_TIMEOUT_SEC,
    platform_name: str | None = None,
) -> NativeDnsSdDiagnostics | None:
    if (platform_name or platform.system()) != "Darwin":
        return None
    if not command_exists("dns-sd"):
        return None

    normalized_service_types = [
        _normalize_dns_sd_service_type(service_type)
        for service_type in (service_types or DEFAULT_DNS_SD_SERVICE_TYPES)
    ]
    start = time.monotonic()
    browsers: list[tuple[str, subprocess.Popen[str] | None, str]] = []
    for service_type in normalized_service_types:
        try:
            proc = subprocess.Popen(
                ["dns-sd", "-B", service_type, "local"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            browsers.append((service_type, proc, ""))
        except OSError as e:
            browsers.append((service_type, None, f"{type(e).__name__}: {e}"))

    deadline = start + max(0.0, timeout_sec)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        if all(proc is None or proc.poll() is not None for _service_type, proc, _error in browsers):
            break
        time.sleep(min(0.1, remaining))

    results: list[NativeDnsSdBrowseResult] = []
    for service_type, proc, start_error in browsers:
        if proc is None:
            results.append(NativeDnsSdBrowseResult(service_type=service_type, error=start_error))
            continue

        terminated_after_timeout = proc.poll() is None
        if terminated_after_timeout:
            proc.terminate()
        try:
            stdout, stderr = proc.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()

        events, parse_error_count = _parse_dns_sd_browse_output(service_type, stdout)
        results.append(
            NativeDnsSdBrowseResult(
                service_type=service_type,
                events=events,
                parse_error_count=parse_error_count,
                stderr=stderr.strip(),
                exit_code=proc.returncode,
                terminated_after_timeout=terminated_after_timeout,
            )
        )

    status = "ok" if any(not result.error for result in results) else "error"
    return NativeDnsSdDiagnostics(
        timeout_sec=timeout_sec,
        elapsed_sec=round(time.monotonic() - start, 3),
        status=status,
        browses=results,
    )
