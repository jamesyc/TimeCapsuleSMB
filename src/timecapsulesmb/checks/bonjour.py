from __future__ import annotations

import shlex

from timecapsulesmb.checks.models import CheckResult
from timecapsulesmb.transport.local import command_exists, run_local_capture


def capture_dns_sd_browse(service_type: str, duration_seconds: int = 5) -> str:
    script = (
        f'dns-sd -B {shlex.quote(service_type)} local. & '
        f'pid=$!; sleep {duration_seconds}; kill "$pid" >/dev/null 2>&1 || true; '
        f'wait "$pid" >/dev/null 2>&1 || true'
    )
    proc = run_local_capture(["/bin/sh", "-c", script], timeout=duration_seconds + 5)
    return proc.stdout


def capture_dns_sd_lookup(instance_name: str, service_type: str, duration_seconds: int = 5) -> str:
    script = (
        f'dns-sd -L {shlex.quote(instance_name)} {shlex.quote(service_type)} local. & '
        f'pid=$!; sleep {duration_seconds}; kill "$pid" >/dev/null 2>&1 || true; '
        f'wait "$pid" >/dev/null 2>&1 || true'
    )
    proc = run_local_capture(["/bin/sh", "-c", script], timeout=duration_seconds + 5)
    return proc.stdout


def parse_browse_instance(output: str) -> str | None:
    for line in output.splitlines():
        if " Add " in line and "_smb._tcp." in line:
            marker = "_smb._tcp."
            idx = line.find(marker)
            if idx != -1:
                return line[idx + len(marker):].strip()
    return None


def parse_lookup_target(output: str) -> str | None:
    for line in output.splitlines():
        if " can be reached at " in line:
            return line.split(" can be reached at ", 1)[1].strip()
    return None


def run_bonjour_checks(expected_instance_name: str, *, service_type: str = "_smb._tcp") -> tuple[list[CheckResult], str | None, str | None]:
    if not command_exists("dns-sd"):
        return [CheckResult("FAIL", "missing local tool dns-sd")], None, None

    results: list[CheckResult] = []
    browse_output = capture_dns_sd_browse(service_type)
    discovered_instance = parse_browse_instance(browse_output)
    if discovered_instance:
        if discovered_instance == expected_instance_name:
            results.append(CheckResult("PASS", f"discovered _smb._tcp instance {discovered_instance!r}"))
        else:
            results.append(CheckResult("WARN", f"discovered _smb._tcp instance {discovered_instance!r}, expected {expected_instance_name!r}"))
    else:
        results.append(CheckResult("FAIL", "could not discover any _smb._tcp instance"))

    lookup_name = discovered_instance or expected_instance_name
    lookup_output = capture_dns_sd_lookup(lookup_name, service_type)
    target = parse_lookup_target(lookup_output)
    if target:
        results.append(CheckResult("PASS", f"resolved {lookup_name!r} to {target}"))
    else:
        results.append(CheckResult("FAIL", f"could not resolve {lookup_name!r}"))

    return results, discovered_instance, target
