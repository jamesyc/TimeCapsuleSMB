from __future__ import annotations

from typing import Optional, Tuple

from timecapsulesmb.checks.models import CheckResult
from timecapsulesmb.discovery.bonjour import discover


def parse_browse_instance(output: str) -> Optional[str]:
    for line in output.splitlines():
        if " Add " in line and "_smb._tcp." in line:
            marker = "_smb._tcp."
            idx = line.find(marker)
            if idx != -1:
                return line[idx + len(marker):].strip()
    return None


def parse_lookup_target(output: str) -> Optional[str]:
    for line in output.splitlines():
        if " can be reached at " in line:
            return line.split(" can be reached at ", 1)[1].strip()
    return None


def _normalize_host_name(value: str) -> str:
    normalized = value.strip().rstrip(".").lower()
    if normalized.endswith(".local"):
        normalized = normalized[: -len(".local")]
    return normalized


def _record_matches_preferred_host(record, preferred_hosts: set[str]) -> bool:
    if not preferred_hosts:
        return False
    if _normalize_host_name(record.hostname or "") in preferred_hosts:
        return True
    return any(_normalize_host_name(ip) in preferred_hosts for ip in record.ipv4 + record.ipv6)


def _record_matches_preferred_ip(record, preferred_ips: set[str]) -> bool:
    if not preferred_ips:
        return False
    return any(ip in preferred_ips for ip in record.ipv4 + record.ipv6)


def _select_record(
    records,
    expected_instance_name: str,
    *,
    preferred_hosts: set[str],
    preferred_ips: set[str],
):
    if not records:
        return None
    ranked = sorted(
        records,
        key=lambda record: (
            not _record_matches_preferred_host(record, preferred_hosts),
            not _record_matches_preferred_ip(record, preferred_ips),
            record.name != expected_instance_name,
            record.hostname or "",
            record.name or "",
        ),
    )
    return ranked[0]


def run_bonjour_checks(
    expected_instance_name: str,
    *,
    service_type: str = "_smb._tcp.local.",
    timeout: float = 5.0,
    preferred_host: str | None = None,
    preferred_ip: str | None = None,
) -> Tuple[list[CheckResult], Optional[str], Optional[str]]:
    try:
        records = discover(timeout=timeout)
    except SystemExit as e:
        return [CheckResult("FAIL", f"Bonjour check failed: {e}")], None, None
    except Exception as e:
        return [CheckResult("FAIL", f"Bonjour check failed: {e}")], None, None

    results: list[CheckResult] = []
    matching = [record for record in records if service_type in record.services]
    preferred_hosts = {_normalize_host_name(preferred_host)} if preferred_host else set()
    preferred_ips = {preferred_ip.strip()} if preferred_ip else set()
    selected = _select_record(
        matching,
        expected_instance_name,
        preferred_hosts=preferred_hosts,
        preferred_ips=preferred_ips,
    )
    discovered_instance = selected.name if selected else None
    target = None

    if discovered_instance:
        if discovered_instance == expected_instance_name:
            results.append(CheckResult("PASS", f"discovered _smb._tcp instance {discovered_instance!r}"))
        else:
            results.append(CheckResult("WARN", f"discovered _smb._tcp instance {discovered_instance!r}, expected {expected_instance_name!r}"))
    else:
        results.append(CheckResult("FAIL", "could not discover any _smb._tcp instance"))

    if selected:
        record = selected
        host = record.hostname or (record.ipv4[0] if record.ipv4 else (record.ipv6[0] if record.ipv6 else ""))
        if host:
            target = f"{host}:445"
            lookup_name = discovered_instance or expected_instance_name
            results.append(CheckResult("PASS", f"resolved {lookup_name!r} to {target}"))
        else:
            lookup_name = discovered_instance or expected_instance_name
            results.append(CheckResult("FAIL", f"could not resolve {lookup_name!r}"))
    else:
        results.append(CheckResult("FAIL", f"could not resolve {expected_instance_name!r}"))

    return results, discovered_instance, target
