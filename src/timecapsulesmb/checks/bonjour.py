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


def run_bonjour_checks(
    expected_instance_name: str,
    *,
    service_type: str = "_smb._tcp.local.",
    timeout: float = 5.0,
) -> Tuple[list[CheckResult], Optional[str], Optional[str]]:
    try:
        records = discover(timeout=timeout)
    except SystemExit as e:
        return [CheckResult("FAIL", f"Bonjour check failed: {e}")], None, None
    except Exception as e:
        return [CheckResult("FAIL", f"Bonjour check failed: {e}")], None, None

    results: list[CheckResult] = []
    matching = [record for record in records if service_type in record.services]
    discovered_instance = matching[0].name if matching else None
    target = None

    if discovered_instance:
        if discovered_instance == expected_instance_name:
            results.append(CheckResult("PASS", f"discovered _smb._tcp instance {discovered_instance!r}"))
        else:
            results.append(CheckResult("WARN", f"discovered _smb._tcp instance {discovered_instance!r}, expected {expected_instance_name!r}"))
    else:
        results.append(CheckResult("FAIL", "could not discover any _smb._tcp instance"))

    if matching:
        record = matching[0]
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
