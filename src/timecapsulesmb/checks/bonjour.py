from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass

from timecapsulesmb.checks.models import CheckResult
from timecapsulesmb.core.config import extract_host
from timecapsulesmb.discovery.bonjour import Discovered, SMB_SERVICE, discover, filter_service_records


@dataclass(frozen=True)
class BonjourExpectedIdentity:
    instance_name: str
    host_label: str | None
    target_ip: str | None


@dataclass(frozen=True)
class BonjourInstanceSelection:
    record: Discovered | None
    candidates: list[Discovered]
    expected_instance_name: str
    expected_host_label: str | None = None


@dataclass(frozen=True)
class BonjourServiceTarget:
    instance_name: str
    hostname: str | None
    port: int = 445

    def authority(self) -> str | None:
        if not self.hostname:
            return None
        return f"{self.hostname}:{self.port}"

    def host_label(self) -> str | None:
        if not self.hostname:
            return None
        host = self.hostname.strip().rstrip(".")
        if not host:
            return None
        if host.endswith(".local"):
            return host[: -len(".local")]
        return host


def _ip_literal(value: str) -> str | None:
    candidate = value.strip()
    if not candidate:
        return None
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        return None
    return candidate


def build_bonjour_expected_identity(values: dict[str, str]) -> BonjourExpectedIdentity:
    return BonjourExpectedIdentity(
        instance_name=values["TC_MDNS_INSTANCE_NAME"],
        host_label=values.get("TC_MDNS_HOST_LABEL") or None,
        target_ip=_ip_literal(extract_host(values.get("TC_HOST", ""))),
    )


def _normalize_host_name(value: str) -> str:
    normalized = value.strip().rstrip(".").lower()
    if normalized.endswith(".local"):
        normalized = normalized[: -len(".local")]
    return normalized


def _record_matches_expected_host_label(record: Discovered, expected_host_label: str | None) -> bool:
    if not expected_host_label:
        return False
    return _normalize_host_name(record.hostname or "") == _normalize_host_name(expected_host_label)


def _describe_record(record: Discovered) -> str:
    host = record.hostname or "-"
    ips = ",".join(record.ipv4 + record.ipv6) or "-"
    name = record.name or "-"
    return f"{name!r} @ {host} [{ips}]"


def _candidate_summary(records: list[Discovered]) -> str:
    if not records:
        return "none"
    return "; ".join(_describe_record(record) for record in records)


def discover_smb_records(timeout: float = 5.0) -> tuple[list[Discovered], CheckResult | None]:
    try:
        records = discover(timeout=timeout)
    except SystemExit as e:
        return [], CheckResult("FAIL", f"Bonjour check failed: {e}")
    except Exception as e:
        return [], CheckResult("FAIL", f"Bonjour check failed: {e}")
    return filter_service_records(records, SMB_SERVICE), None


def select_smb_instance(
    records: list[Discovered],
    *,
    expected_instance_name: str,
    expected_host_label: str | None = None,
) -> BonjourInstanceSelection:
    matching = [record for record in records if record.name == expected_instance_name]
    ranked = sorted(
        matching,
        key=lambda record: (
            not _record_matches_expected_host_label(record, expected_host_label),
            record.hostname or "",
        ),
    )
    return BonjourInstanceSelection(
        record=ranked[0] if ranked else None,
        candidates=records,
        expected_instance_name=expected_instance_name,
        expected_host_label=expected_host_label,
    )


def check_smb_instance(selection: BonjourInstanceSelection) -> list[CheckResult]:
    if selection.record is not None:
        return [
            CheckResult(
                "PASS",
                f"discovered _smb._tcp instance {selection.expected_instance_name!r}",
            )
        ]
    return [
        CheckResult(
            "FAIL",
            f"no discovered _smb._tcp instance matched configured instance {selection.expected_instance_name!r}",
        ),
        CheckResult("INFO", f"discovered _smb._tcp candidates: {_candidate_summary(selection.candidates)}"),
    ]


def resolve_smb_service_target(
    record: Discovered,
    *,
    expected_instance_name: str,
    expected_host_label: str | None = None,
) -> BonjourServiceTarget:
    hostname = (record.hostname or "").strip().rstrip(".")
    if not hostname and expected_host_label:
        hostname = expected_host_label.strip().rstrip(".")
        if hostname and "." not in hostname:
            hostname = f"{hostname}.local"
    return BonjourServiceTarget(
        instance_name=expected_instance_name,
        hostname=hostname or None,
        port=445,
    )


def check_smb_service_target(target: BonjourServiceTarget) -> CheckResult:
    if target.hostname:
        return CheckResult(
            "PASS",
            f"resolved _smb._tcp instance {target.instance_name!r} to {target.hostname}:{target.port}",
        )
    return CheckResult(
        "FAIL",
        f"discovered _smb._tcp instance {target.instance_name!r} but could not resolve service target",
    )


def resolve_host_ipv4(hostname: str) -> list[str]:
    resolved: list[str] = []
    try:
        infos = socket.getaddrinfo(hostname, None, socket.AF_INET, socket.SOCK_STREAM)
    except OSError:
        return resolved
    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            continue
        ip = sockaddr[0]
        if ip and ip not in resolved:
            resolved.append(ip)
    return resolved


def check_bonjour_host_ip(
    hostname: str,
    *,
    expected_ip: str | None = None,
    record_ips: list[str] | None = None,
) -> CheckResult:
    known_ips: list[str] = []
    for ip in record_ips or []:
        if ip and ip not in known_ips:
            known_ips.append(ip)
    for ip in resolve_host_ipv4(hostname):
        if ip not in known_ips:
            known_ips.append(ip)

    if expected_ip:
        if expected_ip in known_ips:
            suffix = " from service record" if expected_ip in (record_ips or []) else ""
            return CheckResult("PASS", f"resolved Bonjour host {hostname} to {expected_ip}{suffix}")
        if known_ips:
            return CheckResult(
                "FAIL",
                f"Bonjour host {hostname} resolved to {', '.join(known_ips)}, expected {expected_ip}",
            )
        return CheckResult("FAIL", f"could not resolve Bonjour host {hostname}")

    if known_ips:
        return CheckResult("PASS", f"resolved Bonjour host {hostname} to {', '.join(known_ips)}")
    return CheckResult("FAIL", f"could not resolve Bonjour host {hostname}")
