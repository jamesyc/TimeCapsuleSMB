"""Host Bonjour queries. Product callers use this module, never a provider."""
from __future__ import annotations

import math
import threading
from collections.abc import Sequence

from timecapsulesmb.discovery.models import (
    SERVICE_TYPES,
    PRINTER_SERVICE_TYPES,
    AIRPORT_SERVICE,
    SMB_SERVICE,
    DEFAULT_BROWSE_TIMEOUT_SEC,
    PENDING_RESOLVE_TIMEOUT_MS,
    FINAL_PENDING_RESOLVE_TIMEOUT_MS,
    MAX_DIAGNOSTIC_OBSERVATIONS,
    DNS_RECORD_TYPE_PTR,
    MDNS_PORT,
    BonjourIPFamily,
    SPLIT_FAMILIES,
    BonjourServiceInstance,
    BonjourResolvedService,
    BonjourDiscoverySnapshot,
    BonjourServiceEvent,
    BonjourPtrRecordObservation,
    BonjourDiscoveryDiagnostics,
    BonjourFamilyDiscoveryAttempt,
    BonjourQueryDiagnostics,
    BonjourDiscoveryError,
    BonjourPermissionDenied,
    discovered_record_root_host,
    discovered_record_has_only_link_local_ips,
    normalize_properties,
    discovery_record_to_jsonable,
    service_instance_to_jsonable,
)
from timecapsulesmb.transport.local import command_exists

MIN_DISCOVERY_TIMEOUT_SEC = 5.0


class DiscoveryTimeoutError(ValueError):
    code = "discovery_timeout_too_short"


def validate_discovery_timeout(value: object) -> float:
    try:
        timeout = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError) as exc:
        raise DiscoveryTimeoutError("Discovery timeout must be at least 5 seconds.") from exc
    if isinstance(value, bool) or not math.isfinite(timeout) or timeout < MIN_DISCOVERY_TIMEOUT_SEC:
        raise DiscoveryTimeoutError("Discovery timeout must be at least 5 seconds.")
    return timeout




class BonjourQuery:
    """One operation's provider and cancellation lifetime, including follow-up resolves."""

    def __init__(self) -> None:
        # Installation is the sole selection rule; a selected provider never falls back.
        if command_exists("dns-sd"):
            from timecapsulesmb.discovery import native_dns_sd as provider
        else:
            from timecapsulesmb.discovery import zeroconf_backend as provider
        self._provider = provider
        self.cancel = threading.Event()

    def browse(
        self, service: str | None = None, timeout: float = DEFAULT_BROWSE_TIMEOUT_SEC,
        *, target_ip: str | None = None, family: BonjourIPFamily | None = None,
        interfaces: Sequence[str] | None = None, deadline: float | None = None,
        service_types: Sequence[str] | None = None,
    ) -> tuple[BonjourDiscoverySnapshot, BonjourQueryDiagnostics]:
        return self._provider.discover_snapshot_merged_detailed(
            service, timeout, target_ip=target_ip, family=family, interfaces=interfaces,
            deadline=deadline, service_types=service_types, cancel=self.cancel,
        )

    def resolve(
        self, instance: BonjourServiceInstance, timeout_ms: int = FINAL_PENDING_RESOLVE_TIMEOUT_MS,
        *, target_ip: str | None = None, family: BonjourIPFamily | None = None,
        interfaces: Sequence[str] | None = None,
    ) -> BonjourResolvedService | None:
        record, diagnostics = self.resolve_detailed(instance, timeout_ms,
            target_ip=target_ip, family=family, interfaces=interfaces)
        if record is None and diagnostics.errors:
            raise RuntimeError("; ".join(diagnostics.errors.values()))
        return record

    def resolve_detailed(
        self, instance: BonjourServiceInstance, timeout_ms: int = FINAL_PENDING_RESOLVE_TIMEOUT_MS,
        *, target_ip: str | None = None, family: BonjourIPFamily | None = None,
        interfaces: Sequence[str] | None = None,
    ) -> tuple[BonjourResolvedService | None, BonjourQueryDiagnostics]:
        return self._provider.resolve_service_instance_detailed(instance, timeout_ms,
            target_ip=target_ip, family=family, interfaces=interfaces, cancel=self.cancel)


def discover_snapshot_detailed(
    service: str | None = None, timeout: float = DEFAULT_BROWSE_TIMEOUT_SEC,
    *, target_ip: str | None = None, family: BonjourIPFamily | None = None,
    interfaces: Sequence[str] | None = None, deadline: float | None = None,
    service_types: Sequence[str] | None = None,
) -> tuple[BonjourDiscoverySnapshot, BonjourQueryDiagnostics]:
    return BonjourQuery().browse(service, timeout, target_ip=target_ip, family=family,
        interfaces=interfaces, deadline=deadline, service_types=service_types)


def resolve_service_instance(
    instance: BonjourServiceInstance, timeout_ms: int = FINAL_PENDING_RESOLVE_TIMEOUT_MS,
    *, target_ip: str | None = None, family: BonjourIPFamily | None = None,
    interfaces: Sequence[str] | None = None,
) -> BonjourResolvedService | None:
    return BonjourQuery().resolve(instance, timeout_ms, target_ip=target_ip, family=family, interfaces=interfaces)


__all__ = [
    "SERVICE_TYPES",
    "PRINTER_SERVICE_TYPES",
    "AIRPORT_SERVICE",
    "SMB_SERVICE",
    "DEFAULT_BROWSE_TIMEOUT_SEC",
    "PENDING_RESOLVE_TIMEOUT_MS",
    "FINAL_PENDING_RESOLVE_TIMEOUT_MS",
    "MAX_DIAGNOSTIC_OBSERVATIONS",
    "DNS_RECORD_TYPE_PTR",
    "MDNS_PORT",
    "BonjourIPFamily",
    "SPLIT_FAMILIES",
    "BonjourServiceInstance",
    "BonjourResolvedService",
    "BonjourDiscoverySnapshot",
    "BonjourServiceEvent",
    "BonjourPtrRecordObservation",
    "BonjourDiscoveryDiagnostics",
    "BonjourFamilyDiscoveryAttempt",
    "BonjourDiscoveryError",
    "BonjourPermissionDenied",
    "discovered_record_root_host",
    "discovered_record_has_only_link_local_ips",
    "normalize_properties",
    "discovery_record_to_jsonable",
    "service_instance_to_jsonable",
    "BonjourQuery",
    "BonjourQueryDiagnostics",
    "DiscoveryTimeoutError",
    "MIN_DISCOVERY_TIMEOUT_SEC",
    "validate_discovery_timeout",
    "discover_snapshot_detailed",
    "resolve_service_instance",
]
