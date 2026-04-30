from __future__ import annotations

import platform
import subprocess
import time
from dataclasses import dataclass, field

from timecapsulesmb.discovery.bonjour import DEFAULT_BROWSE_TIMEOUT_SEC
from timecapsulesmb.transport.local import command_exists


DEFAULT_DNS_SD_SERVICE_TYPES = [
    "_smb._tcp",
    "_adisk._tcp",
    "_airport._tcp",
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


def _normalize_dns_sd_service_type(service_type: str) -> str:
    value = service_type.strip()
    for suffix in (".local.", ".local"):
        if value.endswith(suffix):
            value = value[: -len(suffix)]
    return value.rstrip(".")


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
