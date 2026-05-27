from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from timecapsulesmb.checks.models import CheckResult
from timecapsulesmb.core.config import AppConfig
from timecapsulesmb.core.net import extract_host, ipv4_literal, ipv6_literal, resolve_host_ips
from timecapsulesmb.discovery.bonjour import (
    BonjourIPFamily,
    BonjourDiscoverySnapshot,
    BonjourDiscoveryDiagnostics,
    BonjourResolvedService,
    BonjourServiceInstance,
    DEFAULT_BROWSE_TIMEOUT_SEC,
    FINAL_PENDING_RESOLVE_TIMEOUT_MS,
    SMB_SERVICE,
    discover_snapshot_detailed,
    resolve_service_instance,
)
from timecapsulesmb.device.probe import RuntimeNamingIdentityProbeResult


@dataclass(frozen=True)
class BonjourExpectedIdentity:
    instance_name: str | None
    host_label: str | None
    target_ip: str | None


@dataclass(frozen=True)
class BonjourInstanceSelection:
    instance: BonjourServiceInstance | None
    candidates: list[BonjourServiceInstance]
    expected_instance_name: str | None


@dataclass(frozen=True)
class BonjourExpectedSmbResolution:
    selection: BonjourInstanceSelection
    instance: BonjourServiceInstance
    record: BonjourResolvedService | None
    source: Literal["browse", "targeted_resolve"]
    error: CheckResult | None


@dataclass(frozen=True)
class BonjourServiceTarget:
    instance_name: str
    hostname: str | None
    port: int = 445

    def host_label(self) -> str | None:
        if not self.hostname:
            return None
        host = self.hostname.strip().rstrip(".")
        if not host:
            return None
        if host.endswith(".local"):
            return host[: -len(".local")]
        return host


def build_bonjour_expected_identity(
    config: AppConfig,
    runtime_naming_identity: RuntimeNamingIdentityProbeResult | None = None,
) -> BonjourExpectedIdentity:
    target_ip = None
    candidate_ip = extract_host(config.get("TC_HOST")).strip()
    if candidate_ip:
        target_ip = ipv4_literal(candidate_ip) or ipv6_literal(candidate_ip)
    return BonjourExpectedIdentity(
        instance_name=runtime_naming_identity.mdns_instance_name if runtime_naming_identity is not None else None,
        host_label=runtime_naming_identity.mdns_host_label if runtime_naming_identity is not None else None,
        target_ip=target_ip,
    )


def discover_smb_services_detailed(
    timeout: float = DEFAULT_BROWSE_TIMEOUT_SEC,
    *,
    include_related: bool = False,
    target_ip: str | None = None,
    family: BonjourIPFamily | None = None,
    interfaces: list[str] | None = None,
) -> tuple[BonjourDiscoverySnapshot | None, CheckResult | None, BonjourDiscoveryDiagnostics | None]:
    try:
        snapshot, diagnostics = discover_snapshot_detailed(
            None if include_related else SMB_SERVICE,
            timeout=timeout,
            target_ip=target_ip,
            family=family,
            interfaces=interfaces,
        )
        return snapshot, None, diagnostics
    except Exception as e:
        return None, CheckResult("FAIL", f"Bonjour check failed: {e}"), None


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


def build_expected_smb_instance(instance_name: str) -> BonjourServiceInstance:
    service_type = f"{SMB_SERVICE}._tcp.local."
    stripped_name = instance_name.strip()
    return BonjourServiceInstance(
        service_type=service_type,
        name=stripped_name,
        fullname=f"{stripped_name}.{service_type}",
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
            f"no discovered _smb._tcp instance matched expected device instance {selection.expected_instance_name!r}",
        ),
        CheckResult(
            "INFO",
            "discovered _smb._tcp candidates: "
            + (
                "; ".join(f"{(instance.name or '-')!r}" for instance in selection.candidates)
                if selection.candidates
                else "none"
            ),
        ),
    ]


def select_resolved_smb_record_by_ip(
    records: list[BonjourResolvedService],
    target_ip: str,
) -> BonjourResolvedService | None:
    matches = [
        record
        for record in records
        if (record.service_type == SMB_SERVICE or record.service_type.startswith(f"{SMB_SERVICE}."))
        and (target_ip in (record.ipv4 or []) or target_ip in (record.ipv6 or []))
    ]
    if not matches:
        return None
    return sorted(matches, key=lambda record: (record.name or "", record.hostname or "", record.fullname or ""))[0]


def select_resolved_smb_record(
    records: list[BonjourResolvedService],
    instance: BonjourServiceInstance,
) -> BonjourResolvedService | None:
    for record in records:
        if record.service_type != instance.service_type:
            continue
        if record.fullname and instance.fullname and record.fullname == instance.fullname:
            return record

    matching_name = [
        record
        for record in records
        if record.service_type == instance.service_type and record.name == instance.name
    ]
    if not matching_name:
        return None
    return sorted(matching_name, key=lambda record: (record.hostname or "", record.fullname or ""))[0]


def resolve_smb_instance(
    instance: BonjourServiceInstance,
    timeout_ms: int = FINAL_PENDING_RESOLVE_TIMEOUT_MS,
    *,
    target_ip: str | None = None,
    family: BonjourIPFamily | None = None,
    interfaces: list[str] | None = None,
    missing_message: str | None = None,
) -> tuple[BonjourResolvedService | None, CheckResult | None]:
    try:
        record = resolve_service_instance(
            instance,
            timeout_ms=timeout_ms,
            target_ip=target_ip,
            family=family,
            interfaces=interfaces,
        )
    except Exception as e:
        return None, CheckResult("FAIL", f"Bonjour check failed: {e}")
    if record is None:
        return None, CheckResult(
            "FAIL",
            missing_message or f"discovered _smb._tcp instance {instance.name!r} but could not resolve service target",
        )
    return record, None


def resolve_expected_smb_record(
    instances: list[BonjourServiceInstance],
    records: list[BonjourResolvedService],
    *,
    expected_instance_name: str,
    target_ip: str | None = None,
    family: BonjourIPFamily | None = None,
    interfaces: list[str] | None = None,
    resolver: Callable[..., tuple[BonjourResolvedService | None, CheckResult | None]] = resolve_smb_instance,
) -> BonjourExpectedSmbResolution:
    selection = select_smb_instance(instances, expected_instance_name=expected_instance_name)
    if selection.instance is not None:
        resolved_record = select_resolved_smb_record(records, selection.instance)
        resolve_error = None
        if resolved_record is None:
            resolved_record, resolve_error = resolver(
                selection.instance,
                target_ip=target_ip,
                family=family,
                interfaces=interfaces,
            )
        return BonjourExpectedSmbResolution(
            selection=selection,
            instance=selection.instance,
            record=resolved_record,
            source="browse",
            error=resolve_error,
        )

    expected_instance = build_expected_smb_instance(expected_instance_name)
    resolved_record, resolve_error = resolver(
        expected_instance,
        target_ip=target_ip,
        family=family,
        interfaces=interfaces,
        missing_message=(
            f"expected _smb._tcp instance {expected_instance_name!r} "
            "was not discovered and could not be resolved by targeted query"
        ),
    )
    return BonjourExpectedSmbResolution(
        selection=selection,
        instance=expected_instance,
        record=resolved_record,
        source="targeted_resolve",
        error=resolve_error,
    )


def resolve_smb_service_target(
    record: BonjourResolvedService,
    *,
    expected_instance_name: str | None,
) -> BonjourServiceTarget:
    hostname = (record.hostname or "").strip().rstrip(".")
    return BonjourServiceTarget(
        instance_name=expected_instance_name or record.name,
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
    for ip in resolve_host_ips(hostname):
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
