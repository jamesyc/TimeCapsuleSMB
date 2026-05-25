from __future__ import annotations

import ipaddress
import socket
import threading
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Literal

from timecapsulesmb.core.errors import missing_dependency_message
from timecapsulesmb.core.net import is_link_local_ipv4


SERVICE_TYPES = [
    "_airport._tcp.local.",
    "_smb._tcp.local.",
    "_adisk._tcp.local.",
    "_afpovertcp._tcp.local.",
    "_device-info._tcp.local.",
]

AIRPORT_SERVICE = "_airport"
SMB_SERVICE = "_smb"
DEFAULT_BROWSE_TIMEOUT_SEC = 6.0
PENDING_RESOLVE_INTERVAL_SEC = 0.5
PENDING_RESOLVE_TIMEOUT_MS = 500
FINAL_PENDING_RESOLVE_TIMEOUT_MS = 3000
MAX_DIAGNOSTIC_OBSERVATIONS = 100
DNS_RECORD_TYPE_PTR = 12
MDNS_PORT = 5353
BonjourIPFamily = Literal["ipv4", "ipv6"]


@dataclass
class BonjourServiceInstance:
    service_type: str
    name: str
    fullname: str


@dataclass
class BonjourResolvedService:
    name: str
    hostname: str
    service_type: str = ""
    port: int = 0
    ipv4: Sequence[str] = field(default_factory=list)
    ipv6: Sequence[str] = field(default_factory=list)
    services: set[str] = field(default_factory=set)
    properties: dict[str, str] = field(default_factory=dict)
    fullname: str = ""

    def __post_init__(self) -> None:
        self.ipv4 = list(self.ipv4)
        self.ipv6 = list(self.ipv6)
        if self.service_type and not self.services:
            self.services.add(self.service_type)
        elif not self.service_type and len(self.services) == 1:
            self.service_type = next(iter(self.services))

    def prefer_host(self) -> str:
        return self.hostname or (self.ipv4[0] if self.ipv4 else (self.ipv6[0] if self.ipv6 else ""))


@dataclass
class BonjourDiscoverySnapshot:
    instances: list[BonjourServiceInstance]
    resolved: list[BonjourResolvedService]


@dataclass
class BonjourServiceEvent:
    service_type: str
    state: str
    name: str
    fullname: str
    elapsed_sec: float


@dataclass
class BonjourPtrRecordObservation:
    service_type: str
    alias: str
    alias_name: str
    ttl: int
    expired: bool
    old_record_present: bool
    elapsed_sec: float


@dataclass
class BonjourDiscoveryDiagnostics:
    service: str | None
    service_types: list[str]
    timeout_sec: float
    elapsed_sec: float
    ip_version: str
    instance_count: int
    resolved_count: int
    pending_count: int
    service_added_count: int
    service_updated_count: int
    resolve_attempt_count: int
    resolve_success_count: int
    resolve_error_count: int
    zeroconf_version: str = ""
    zeroconf_interfaces: str = "All"
    zeroconf_apple_p2p: bool = False
    instances: list[BonjourServiceInstance] = field(default_factory=list)
    resolved: list[BonjourResolvedService] = field(default_factory=list)
    service_events: list[BonjourServiceEvent] = field(default_factory=list)
    ptr_records: list[BonjourPtrRecordObservation] = field(default_factory=list)
    ptr_record_error: str | None = None


Discovered = BonjourResolvedService


@dataclass
class ServiceObservation:
    name: str
    hostname: str
    service_type: str
    port: int = 0
    ipv4: list[str] = field(default_factory=list)
    ipv6: list[str] = field(default_factory=list)
    properties: dict[str, str] = field(default_factory=dict)
    fullname: str = ""


def discovered_record_root_host(rec: BonjourResolvedService) -> str | None:
    chosen_host = ""
    for ip in rec.ipv4:
        if not is_link_local_ipv4(ip):
            chosen_host = ip
            break
    if not chosen_host and rec.ipv4:
        return None
    if not chosen_host:
        chosen_host = rec.prefer_host()
    return f"root@{chosen_host}" if chosen_host else None


def _bytes_to_ip(addr_bytes: bytes) -> str:
    try:
        return str(ipaddress.ip_address(addr_bytes))
    except Exception:
        if len(addr_bytes) == 4:
            return socket.inet_ntop(socket.AF_INET, addr_bytes)
        if len(addr_bytes) == 16:
            return socket.inet_ntop(socket.AF_INET6, addr_bytes)
        return ""


def _decode_props(props: dict[bytes, bytes]) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in props.items():
        if v is None:
            continue
        try:
            key = k.decode("utf-8", "ignore")
            value = v.decode("utf-8", "ignore")
        except Exception:
            continue
        if not key:
            continue
        if "," not in value:
            out[key] = value
            continue

        chunks = [chunk.strip() for chunk in value.split(",")]
        first_chunk = chunks[0] if chunks else ""
        if "=" in first_chunk:
            out[key] = value
        else:
            out[key] = first_chunk

        for chunk in chunks:
            if "=" not in chunk:
                continue
            extra_key, extra_value = chunk.split("=", 1)
            extra_key = extra_key.strip()
            if extra_key and extra_key not in out:
                out[extra_key] = extra_value.strip()
    return out


def _display_name(fullname: str, service_type: str) -> str:
    suffix = service_type
    if fullname.endswith(suffix):
        return fullname[: -len(suffix)].rstrip(".")
    return fullname.rstrip(".")


def _normalize_hostname(value: str) -> str:
    return value.strip().rstrip(".").lower()


def _observation_merge_key(observation: ServiceObservation) -> tuple[str, str, str]:
    return (
        observation.service_type,
        observation.name.strip(),
        _normalize_hostname(observation.hostname),
    )


def _service_matches(service_type: str, service: str) -> bool:
    return service_type.startswith(service)


def _matching_service_types(service: str | None = None) -> list[str]:
    if not service:
        return list(SERVICE_TYPES)
    matching = [service_type for service_type in SERVICE_TYPES if _service_matches(service_type, service)]
    if matching:
        return matching
    candidate = service.strip()
    if candidate.endswith("."):
        return [candidate]
    if "._tcp.local" in candidate or "._udp.local" in candidate:
        return [f"{candidate}."]
    return [candidate]


def _append_bounded(values: list[Any], value: Any, limit: int = MAX_DIAGNOSTIC_OBSERVATIONS) -> None:
    if len(values) < limit:
        values.append(value)


def _elapsed_since(start_time: float) -> float:
    return round(max(0.0, time.monotonic() - start_time), 3)


def _state_change_name(state_change: Any) -> str:
    name = getattr(state_change, "name", None)
    if isinstance(name, str) and name:
        return name
    text = str(state_change)
    return text.rsplit(".", 1)[-1] if text else ""


def _installed_zeroconf_version() -> str:
    try:
        return version("zeroconf")
    except PackageNotFoundError:
        pass
    try:
        import zeroconf

        value = getattr(zeroconf, "__version__", "")
    except Exception:
        return ""
    return value if isinstance(value, str) else ""


def _source_ipv4_for_target(target_ip: str | None) -> str | None:
    if not target_ip:
        return None
    try:
        parsed = ipaddress.ip_address(target_ip)
    except ValueError:
        return None
    if parsed.version != 4:
        return None

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    except OSError:
        return None
    try:
        sock.connect((target_ip, MDNS_PORT))
        source_ip = sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()

    if not source_ip or source_ip == "0.0.0.0":
        return None
    try:
        source = ipaddress.ip_address(source_ip)
    except ValueError:
        return None
    return source_ip if source.version == 4 else None


def _source_ipv6_for_target(target_ip: str | None) -> str | None:
    if not target_ip:
        return None
    try:
        parsed = ipaddress.ip_address(target_ip)
    except ValueError:
        return None
    if parsed.version != 6:
        return None

    try:
        sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
    except OSError:
        return None
    try:
        sock.connect((target_ip, MDNS_PORT))
        source_ip = sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()

    if not source_ip or source_ip == "::":
        return None
    try:
        source = ipaddress.ip_address(source_ip.split("%", 1)[0])
    except ValueError:
        return None
    return source_ip if source.version == 6 else None


class Collector:
    def __init__(self, zc: Any, services: list[str], *, start_time: float | None = None):
        self.zc = zc
        self.services = services
        self.start_time = time.monotonic() if start_time is None else start_time
        self.lock = threading.Lock()
        self.instances: dict[tuple[str, str], BonjourServiceInstance] = {}
        self.observations: dict[tuple[str, str, str], ServiceObservation] = {}
        self.pending: set[tuple[str, str]] = set()
        self.events: list[BonjourServiceEvent] = []
        self._browsers: list[Any] = []
        self.service_added_count = 0
        self.service_updated_count = 0
        self.resolve_attempt_count = 0
        self.resolve_success_count = 0
        self.resolve_error_count = 0

    def start(self) -> None:
        from zeroconf import DNSQuestionType, ServiceBrowser

        for stype in self.services:
            browser = ServiceBrowser(self.zc, stype, handlers=[self._on_service_state_change], question_type=DNSQuestionType.QM)
            self._browsers.append(browser)

    def _on_service_state_change(self, *, zeroconf: Any, service_type: str, name: str, state_change: Any) -> None:
        from zeroconf import ServiceStateChange

        event = BonjourServiceEvent(
            service_type=service_type,
            state=_state_change_name(state_change),
            name=_display_name(name, service_type),
            fullname=name,
            elapsed_sec=_elapsed_since(self.start_time),
        )
        with self.lock:
            _append_bounded(self.events, event)

        if state_change is ServiceStateChange.Added or state_change is ServiceStateChange.Updated:
            instance = BonjourServiceInstance(
                service_type=service_type,
                name=_display_name(name, service_type),
                fullname=name,
            )
            with self.lock:
                if state_change is ServiceStateChange.Added:
                    self.service_added_count += 1
                else:
                    self.service_updated_count += 1
                self.instances[(service_type, name)] = instance
                self.pending.add((service_type, name))

    def service_instances(self) -> list[BonjourServiceInstance]:
        with self.lock:
            return list(self.instances.values())

    def service_events(self) -> list[BonjourServiceEvent]:
        with self.lock:
            return list(self.events)

    def resolve_pending(self, timeout_ms: int = FINAL_PENDING_RESOLVE_TIMEOUT_MS) -> None:
        from zeroconf import DNSQuestionType

        with self.lock:
            pending = sorted(self.pending)

        for service_type, name in pending:
            try:
                self.resolve_attempt_count += 1
                info = self.zc.get_service_info(service_type, name, timeout_ms, question_type=DNSQuestionType.QM)
            except Exception:
                self.resolve_error_count += 1
                info = None
            if info:
                self.resolve_success_count += 1
                self.add_info(service_type, info)
                with self.lock:
                    self.pending.discard((service_type, name))

    def add_info(self, stype: str, info: Any) -> None:
        observation = resolved_service_from_info(stype, info)
        key = _observation_merge_key(
            ServiceObservation(
                name=observation.name,
                hostname=observation.hostname,
                service_type=observation.service_type,
            )
        )
        if not key:
            return

        with self.lock:
            rec = self.observations.get(key)
            if not rec:
                rec = ServiceObservation(
                    name=observation.name,
                    hostname=observation.hostname.rstrip("."),
                    service_type=observation.service_type,
                    port=observation.port,
                    fullname=observation.fullname,
                )
                self.observations[key] = rec
            if observation.port:
                rec.port = observation.port
            if observation.fullname:
                rec.fullname = observation.fullname
            for ip in observation.ipv4:
                if ip not in rec.ipv4:
                    rec.ipv4.append(ip)
            for ip in observation.ipv6:
                if ip not in rec.ipv6:
                    rec.ipv6.append(ip)
            rec.properties.update({k: v for k, v in observation.properties.items() if v})

    def results(self) -> list[BonjourResolvedService]:
        with self.lock:
            return [
                BonjourResolvedService(
                    name=observation.name,
                    hostname=observation.hostname.rstrip("."),
                    service_type=observation.service_type,
                    port=observation.port,
                    ipv4=list(observation.ipv4),
                    ipv6=list(observation.ipv6),
                    properties=dict(observation.properties),
                    fullname=observation.fullname,
                )
                for observation in self.observations.values()
            ]

    def pending_count(self) -> int:
        with self.lock:
            return len(self.pending)


class PtrRecordObserver:
    def __init__(self, services: list[str], *, start_time: float):
        self.services = set(services)
        self.start_time = start_time
        self.lock = threading.Lock()
        self.records: list[BonjourPtrRecordObservation] = []
        self.error: str | None = None
        self._registered = False
        self._listener: Any | None = None
        self.ptr_record_type = DNS_RECORD_TYPE_PTR

    def start(self, zc: Any) -> None:
        try:
            from zeroconf import DNSQuestion, RecordUpdateListener
            from zeroconf.const import _CLASS_IN, _TYPE_PTR

            observer = self
            self.ptr_record_type = _TYPE_PTR

            class Listener(RecordUpdateListener):
                def async_update_records(self, zc: Any, now: float, records: list[Any]) -> None:
                    observer.async_update_records(zc, now, records)

                def async_update_records_complete(self) -> None:
                    observer.async_update_records_complete()

                def update_record(self, zc: Any, now: float, *records: Any) -> None:
                    observer.update_record(zc, now, *records)

            questions = [
                DNSQuestion(service_type, self.ptr_record_type, _CLASS_IN)
                for service_type in sorted(self.services)
            ]
            self._listener = Listener()
            zc.add_listener(self._listener, questions)
            self._registered = True
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"

    def stop(self, zc: Any) -> None:
        if not self._registered:
            return
        try:
            zc.remove_listener(self._listener)
        except Exception:
            pass

    def async_update_records(self, zc: Any, now: float, records: list[Any]) -> None:
        for update in records:
            record = getattr(update, "new", update)
            if record is None:
                continue
            if getattr(record, "type", None) != self.ptr_record_type:
                continue
            service_type = str(getattr(record, "name", "") or "")
            if service_type not in self.services:
                continue
            alias = str(getattr(record, "alias", "") or "")
            old_record = getattr(update, "old", None)
            observation = BonjourPtrRecordObservation(
                service_type=service_type,
                alias=alias,
                alias_name=_display_name(alias, service_type),
                ttl=int(getattr(record, "ttl", 0) or 0),
                expired=_record_is_expired(record, now),
                old_record_present=old_record is not None,
                elapsed_sec=round(max(0.0, now - self.start_time), 3),
            )
            with self.lock:
                _append_bounded(self.records, observation)

    def async_update_records_complete(self) -> None:
        return

    def update_record(self, zc: Any, now: float, *records: Any) -> None:
        if records:
            self.async_update_records(zc, now, [records[-1]])

    def observations(self) -> list[BonjourPtrRecordObservation]:
        with self.lock:
            return list(self.records)


def _record_is_expired(record: Any, now: float) -> bool:
    is_expired = getattr(record, "is_expired", None)
    if callable(is_expired):
        try:
            return bool(is_expired(now))
        except TypeError:
            try:
                return bool(is_expired())
            except Exception:
                pass
        except Exception:
            pass
    return int(getattr(record, "ttl", 0) or 0) <= 0


def resolved_service_from_info(stype: str, info: Any) -> BonjourResolvedService:
    name = _display_name(info.name or "", stype)
    hostname = info.server or ""
    props = _decode_props({k: v for k, v in (info.properties or {}).items() if v is not None})
    ipv4: list[str] = []
    ipv6: list[str] = []

    try:
        addrs = list(info.addresses or [])
    except Exception:
        addrs = []

    for addr in addrs:
        ip = _bytes_to_ip(addr)
        if not ip:
            continue
        try:
            ip_obj = ipaddress.ip_address(ip)
            (ipv6 if ip_obj.version == 6 else ipv4).append(ip)
        except Exception:
            continue

    return BonjourResolvedService(
        name=name,
        hostname=hostname.rstrip("."),
        service_type=stype,
        port=int(getattr(info, "port", 0) or 0),
        ipv4=ipv4,
        ipv6=ipv6,
        properties=props,
        fullname=info.name or "",
    )


def _ip_text(value: object) -> str | None:
    if isinstance(value, tuple):
        value = value[0] if value else ""
    if not isinstance(value, str):
        return None
    try:
        return str(ipaddress.ip_address(value))
    except Exception:
        return None


def _adapter_ipv6_addresses_for_ipv4(source_ipv4: str) -> list[str]:
    try:
        import ifaddr
    except Exception:
        return []

    try:
        adapters = ifaddr.get_adapters()
    except Exception:
        return []

    for adapter in adapters:
        adapter_has_source_ipv4 = False
        ipv6_addresses: list[str] = []
        for adapter_ip in getattr(adapter, "ips", []):
            ip_text = _ip_text(getattr(adapter_ip, "ip", None))
            if not ip_text:
                continue
            try:
                ip_obj = ipaddress.ip_address(ip_text)
            except Exception:
                continue
            if ip_obj.version == 4 and ip_text == source_ipv4:
                adapter_has_source_ipv4 = True
            elif ip_obj.version == 6 and not ip_obj.is_loopback and ip_text not in ipv6_addresses:
                ipv6_addresses.append(ip_text)
        if adapter_has_source_ipv4:
            return ipv6_addresses
    return []


def _zeroconf_interfaces_for_target(target_ip: str | None, *, family: BonjourIPFamily | None = None) -> list[str] | None:
    if family == "ipv6":
        source_ipv6 = _source_ipv6_for_target(target_ip)
        return [source_ipv6] if source_ipv6 else None

    source_ipv4 = _source_ipv4_for_target(target_ip)
    if not source_ipv4:
        return None
    if family == "ipv4":
        return [source_ipv4]
    return [source_ipv4, *_adapter_ipv6_addresses_for_ipv4(source_ipv4)]


def _zeroconf_ip_version(IPVersion: Any, *, family: BonjourIPFamily | None = None) -> tuple[Any, str]:
    if family == "ipv4":
        return IPVersion.V4Only, "V4Only"
    if family == "ipv6":
        try:
            return IPVersion.V6Only, "V6Only"
        except AttributeError:
            return IPVersion.All, "All"
    try:
        return IPVersion.All, "All"
    except AttributeError:
        return IPVersion.V4Only, "V4Only"


def _zeroconf_ip_version_name(*, family: BonjourIPFamily | None = None) -> str:
    try:
        from zeroconf import IPVersion
    except Exception:
        return "All"
    _ip_version, ip_version_name = _zeroconf_ip_version(IPVersion, family=family)
    return ip_version_name


def _format_zeroconf_interfaces(interfaces: Sequence[str] | None) -> str:
    if not interfaces:
        return "All"
    return ",".join(interfaces)


def _open_zeroconf(interfaces: Sequence[str] | None = None, *, family: BonjourIPFamily | None = None) -> Any:
    try:
        from zeroconf import IPVersion, Zeroconf
    except Exception as e:
        raise RuntimeError(missing_dependency_message("zeroconf", e)) from e

    ip_version, _ip_version_name = _zeroconf_ip_version(IPVersion, family=family)
    if interfaces:
        return Zeroconf(interfaces=list(interfaces), ip_version=ip_version)
    return Zeroconf(ip_version=ip_version)


def resolve_service_instance(
    instance: BonjourServiceInstance,
    timeout_ms: int = FINAL_PENDING_RESOLVE_TIMEOUT_MS,
    *,
    target_ip: str | None = None,
    family: BonjourIPFamily | None = None,
    interfaces: Sequence[str] | None = None,
) -> BonjourResolvedService | None:
    try:
        from zeroconf import DNSQuestionType
    except Exception as e:
        raise RuntimeError(missing_dependency_message("zeroconf", e)) from e

    if interfaces is None:
        interfaces = _zeroconf_interfaces_for_target(target_ip, family=family)
    zc = _open_zeroconf(interfaces, family=family)
    try:
        info = zc.get_service_info(instance.service_type, instance.fullname, timeout_ms, question_type=DNSQuestionType.QM)
    finally:
        try:
            zc.close()
        except Exception:
            pass
    if not info:
        return None
    return resolved_service_from_info(instance.service_type, info)


def _sort_instances(instances: list[BonjourServiceInstance]) -> list[BonjourServiceInstance]:
    return sorted(instances, key=lambda instance: (instance.service_type or "", instance.name or "", instance.fullname or ""))


def _sort_records(records: list[BonjourResolvedService]) -> list[BonjourResolvedService]:
    return sorted(records, key=lambda record: (record.service_type or "", record.hostname or "", record.name or ""))


def discover_snapshot_detailed(
    service: str | None = None,
    timeout: float = DEFAULT_BROWSE_TIMEOUT_SEC,
    *,
    target_ip: str | None = None,
    family: BonjourIPFamily | None = None,
    interfaces: Sequence[str] | None = None,
) -> tuple[BonjourDiscoverySnapshot, BonjourDiscoveryDiagnostics]:
    service_types = _matching_service_types(service)
    start = time.monotonic()
    zeroconf_interfaces = interfaces if interfaces is not None else _zeroconf_interfaces_for_target(target_ip, family=family)
    zc = _open_zeroconf(zeroconf_interfaces, family=family)
    ptr_observer: PtrRecordObserver | None = None
    ptr_records: list[BonjourPtrRecordObservation] = []
    ptr_record_error: str | None = None
    try:
        collector = Collector(zc, service_types, start_time=start)
        ptr_observer = PtrRecordObserver(service_types, start_time=start)
        ptr_observer.start(zc)
        collector.start()
        deadline = start + max(0.0, timeout)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(PENDING_RESOLVE_INTERVAL_SEC, remaining))
            collector.resolve_pending(timeout_ms=PENDING_RESOLVE_TIMEOUT_MS)
        collector.resolve_pending(timeout_ms=FINAL_PENDING_RESOLVE_TIMEOUT_MS)
        records = collector.results()
        instances = collector.service_instances()
        service_events = collector.service_events()
    finally:
        if ptr_observer is not None:
            ptr_observer.stop(zc)
            ptr_records = ptr_observer.observations()
            ptr_record_error = ptr_observer.error
        try:
            zc.close()
        except Exception:
            pass

    sorted_instances = _sort_instances(instances)
    sorted_records = _sort_records(records)
    snapshot = BonjourDiscoverySnapshot(
        instances=sorted_instances,
        resolved=sorted_records,
    )
    diagnostics = BonjourDiscoveryDiagnostics(
        service=service,
        service_types=list(service_types),
        timeout_sec=timeout,
        elapsed_sec=round(time.monotonic() - start, 3),
        ip_version=_zeroconf_ip_version_name(family=family),
        instance_count=len(sorted_instances),
        resolved_count=len(sorted_records),
        pending_count=collector.pending_count(),
        service_added_count=collector.service_added_count,
        service_updated_count=collector.service_updated_count,
        resolve_attempt_count=collector.resolve_attempt_count,
        resolve_success_count=collector.resolve_success_count,
        resolve_error_count=collector.resolve_error_count,
        zeroconf_version=_installed_zeroconf_version(),
        zeroconf_interfaces=_format_zeroconf_interfaces(zeroconf_interfaces),
        zeroconf_apple_p2p=False,
        instances=sorted_instances,
        resolved=sorted_records,
        service_events=service_events,
        ptr_records=ptr_records,
        ptr_record_error=ptr_record_error,
    )
    return snapshot, diagnostics


def discover_snapshot(
    service: str | None = None,
    timeout: float = DEFAULT_BROWSE_TIMEOUT_SEC,
    *,
    target_ip: str | None = None,
    family: BonjourIPFamily | None = None,
    interfaces: Sequence[str] | None = None,
) -> BonjourDiscoverySnapshot:
    snapshot, _diagnostics = discover_snapshot_detailed(
        service=service,
        timeout=timeout,
        target_ip=target_ip,
        family=family,
        interfaces=interfaces,
    )
    return snapshot


def _records_with_unresolved_instances(snapshot: BonjourDiscoverySnapshot) -> list[BonjourResolvedService]:
    records = list(snapshot.resolved)
    resolved_keys = {(record.service_type, record.fullname) for record in records if record.fullname}
    for instance in snapshot.instances:
        if (instance.service_type, instance.fullname) in resolved_keys:
            continue
        records.append(
            BonjourResolvedService(
                name=instance.name,
                hostname="",
                service_type=instance.service_type,
                fullname=instance.fullname,
            )
        )
    return _sort_records(records)


def discover_resolved_records(
    service: str | None = None,
    timeout: float = DEFAULT_BROWSE_TIMEOUT_SEC,
    *,
    target_ip: str | None = None,
    family: BonjourIPFamily | None = None,
    interfaces: Sequence[str] | None = None,
) -> list[BonjourResolvedService]:
    return discover_snapshot(
        service=service,
        timeout=timeout,
        target_ip=target_ip,
        family=family,
        interfaces=interfaces,
    ).resolved


def discover(
    timeout: float = DEFAULT_BROWSE_TIMEOUT_SEC,
    *,
    target_ip: str | None = None,
    family: BonjourIPFamily | None = None,
    interfaces: Sequence[str] | None = None,
) -> list[BonjourResolvedService]:
    return _records_with_unresolved_instances(
        discover_snapshot(timeout=timeout, target_ip=target_ip, family=family, interfaces=interfaces)
    )


def record_has_service(record: BonjourResolvedService, service: str) -> bool:
    raw_service = getattr(record, "service_type", "")
    if isinstance(raw_service, str) and raw_service.startswith(service):
        return True
    services = getattr(record, "services", set())
    return isinstance(services, (set, frozenset, list, tuple)) and any(
        isinstance(value, str) and value.startswith(service)
        for value in services
    )


def discovery_record_to_jsonable(record: BonjourResolvedService) -> dict[str, object]:
    data = asdict(record)
    data["services"] = sorted(record.services)
    return data


def service_instance_to_jsonable(instance: BonjourServiceInstance) -> dict[str, object]:
    return asdict(instance)
