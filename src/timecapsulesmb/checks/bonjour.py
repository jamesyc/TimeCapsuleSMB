from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass

from timecapsulesmb.checks.models import CheckResult
from timecapsulesmb.core.config import extract_host
from timecapsulesmb.discovery.bonjour import (
    BonjourResolvedService,
    BonjourServiceInstance,
    SMB_SERVICE,
    browse_service_instances,
    resolve_service_instance,
)


@dataclass(frozen=True)
class BonjourExpectedIdentity:
    instance_name: str
    host_label: str | None
    target_ip: str | None


@dataclass(frozen=True)
class BonjourInstanceSelection:
    instance: BonjourServiceInstance | None
    candidates: list[BonjourServiceInstance]
    expected_instance_name: str


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


def _describe_instance(instance: BonjourServiceInstance) -> str:
    name = instance.name or "-"
    return f"{name!r}"


def _candidate_summary(instances: list[BonjourServiceInstance]) -> str:
    if not instances:
        return "none"
    return "; ".join(_describe_instance(instance) for instance in instances)


def browse_smb_instances(timeout: float = 5.0) -> tuple[list[BonjourServiceInstance], CheckResult | None]:
    try:
        instances = browse_service_instances(SMB_SERVICE, timeout=timeout)
    except SystemExit as e:
        return [], CheckResult("FAIL", f"Bonjour check failed: {e}")
    except Exception as e:
        return [], CheckResult("FAIL", f"Bonjour check failed: {e}")
    return instances, None


def select_smb_instance(
    instances: list[BonjourServiceInstance],
    *,
    expected_instance_name: str,
) -> BonjourInstanceSelection:
    matching = [instance for instance in instances if instance.name == expected_instance_name]
    ranked = sorted(matching, key=lambda instance: instance.fullname or instance.name)
    return BonjourInstanceSelection(
        instance=ranked[0] if ranked else None,
        candidates=instances,
        expected_instance_name=expected_instance_name,
    )


def check_smb_instance(selection: BonjourInstanceSelection) -> list[CheckResult]:
    if selection.instance is not None:
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


def resolve_smb_instance(instance: BonjourServiceInstance, timeout_ms: int = 2000) -> tuple[BonjourResolvedService | None, CheckResult | None]:
    try:
        record = resolve_service_instance(instance, timeout_ms=timeout_ms)
    except SystemExit as e:
        return None, CheckResult("FAIL", f"Bonjour check failed: {e}")
    except Exception as e:
        return None, CheckResult("FAIL", f"Bonjour check failed: {e}")
    if record is None:
        return None, CheckResult(
            "FAIL",
            f"discovered _smb._tcp instance {instance.name!r} but could not resolve service target",
        )
    return record, None


def resolve_smb_service_target(
    record: BonjourResolvedService,
    *,
    expected_instance_name: str,
) -> BonjourServiceTarget:
    hostname = (record.hostname or "").strip().rstrip(".")
    return BonjourServiceTarget(
        instance_name=expected_instance_name,
        hostname=hostname or None,
        port=record.port or 445,
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
