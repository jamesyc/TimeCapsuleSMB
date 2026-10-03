from __future__ import annotations

import ipaddress
import threading
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from timecapsulesmb.discovery.models import (
    BonjourDiscoveryDiagnostics,
    BonjourDiscoveryError,
    BonjourDiscoverySnapshot,
    BonjourFamilyDiscoveryAttempt,
    BonjourIPFamily,
    BonjourQueryDiagnostics,
    BonjourPtrRecordObservation,
    BonjourResolvedService,
    BonjourServiceEvent,
    BonjourServiceInstance,
    DEFAULT_BROWSE_TIMEOUT_SEC,
    DNS_RECORD_TYPE_PTR,
    FINAL_PENDING_RESOLVE_TIMEOUT_MS,
    MAX_DIAGNOSTIC_OBSERVATIONS,
    PENDING_RESOLVE_TIMEOUT_MS,
    SPLIT_FAMILIES,
    _decode_props,
    _matching_service_types,
    _merge_snapshots,
    _normalize_hostname,
    _sort_instances,
    _sort_records
)

from timecapsulesmb.discovery.interfaces import ipv4_interface_addresses
from timecapsulesmb.core.errors import missing_dependency_message
from timecapsulesmb.core.net import ipv6_scope_index, scoped_ip_literal, select_route_to_address, is_link_local_ipv6


def _resolve_record(zc: Any, key: tuple[str, str], timeout_ms: int,
                    required_families: Sequence[BonjourIPFamily]) -> BonjourResolvedService | None:
    from zeroconf import AddressResolverIPv4, AddressResolverIPv6, DNSQuestionType, IPVersion

    end = time.monotonic() + timeout_ms / 1000
    info = zc.get_service_info(*key, timeout_ms, question_type=DNSQuestionType.QM)
    if info is None:
        return None
    record = resolved_service_from_info(key[0], info)
    # ServiceInfo accepts either A or AAAA as complete. Ask for a missing type
    # explicitly: another get_service_info call can return the same cached answer.
    for family in required_families:
        remaining = int((end - time.monotonic()) * 1000)
        if getattr(record, family) or remaining <= 0:
            continue
        resolver = (AddressResolverIPv4 if family == "ipv4" else AddressResolverIPv6)(info.server)
        resolver.interface_index = record.interface_index
        try:
            if resolver.request(zc, remaining, question_type=DNSQuestionType.QM):
                version = IPVersion.V4Only if family == "ipv4" else IPVersion.V6Only
                addresses = resolver.parsed_scoped_addresses(version)
                if family == "ipv6" and record.interface_index and record.interface_index > 0:
                    addresses = [scoped_ip_literal(ip, scope_id=record.interface_index) or ip
                                 if is_link_local_ipv6(ip) and "%" not in ip else ip for ip in addresses]
                setattr(record, family, addresses)
        except Exception:
            # An unsuccessful supplementary lookup cannot erase the service or
            # the other family already observed during this bounded attempt.
            pass
    return record


def _record_complete(record: BonjourResolvedService, families: Sequence[BonjourIPFamily]) -> bool:
    return all(getattr(record, family) for family in families)


def _display_name(fullname: str, service_type: str) -> str:
    suffix = service_type
    if fullname.endswith(suffix):
        return fullname[: -len(suffix)].rstrip(".")
    return fullname.rstrip(".")


def _observation_merge_key(observation: BonjourResolvedService) -> tuple[str, str, str]:
    return (
        observation.service_type,
        observation.name.strip(),
        _normalize_hostname(observation.hostname),
    )


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


class Collector:
    def __init__(self, zc: Any, services: list[str], *, start_time: float | None = None,
                 required_families: Sequence[BonjourIPFamily] = SPLIT_FAMILIES):
        self.zc = zc
        self.services = services
        self.required_families = required_families
        self.start_time = time.monotonic() if start_time is None else start_time
        self.lock = threading.RLock()
        self.instances: dict[tuple[str, str], BonjourServiceInstance] = {}
        self.observations: dict[tuple[str, str, str], BonjourResolvedService] = {}
        self.pending: dict[tuple[str, str], None] = {}
        self.generations: dict[tuple[str, str], int] = {}
        self.accepting = True
        self.events: list[BonjourServiceEvent] = []
        self._browsers: list[Any] = []
        self.service_added_count = self.service_updated_count = 0
        self.resolve_attempt_count = self.resolve_success_count = self.resolve_error_count = 0

    def start(self) -> None:
        from zeroconf import DNSQuestionType, ServiceBrowser
        for stype in self.services:
            self._browsers.append(ServiceBrowser(self.zc, stype, handlers=[self._on_service_state_change], question_type=DNSQuestionType.QM))

    def stop(self) -> None:
        for browser in self._browsers:
            browser.cancel()

    def _on_service_state_change(self, *, zeroconf: Any, service_type: str, name: str, state_change: Any) -> None:
        from zeroconf import ServiceStateChange
        key = (service_type, name)
        with self.lock:
            if not self.accepting and key not in self.instances:
                return
            _append_bounded(self.events, BonjourServiceEvent(service_type, _state_change_name(state_change), _display_name(name, service_type), name, _elapsed_since(self.start_time)))
            self.generations[key] = self.generations.get(key, 0) + 1
            if state_change is ServiceStateChange.Removed:
                self.instances.pop(key, None)
                self.pending.pop(key, None)
                self.observations = {k: v for k, v in self.observations.items() if (v.service_type, v.fullname) != key}
                return
            self.instances[key] = BonjourServiceInstance(service_type, _display_name(name, service_type), name)
            self.pending[key] = None
            if state_change is ServiceStateChange.Added:
                self.service_added_count += 1
            else:
                self.service_updated_count += 1

    def service_instances(self) -> list[BonjourServiceInstance]:
        with self.lock:
            return list(self.instances.values())

    def service_events(self) -> list[BonjourServiceEvent]:
        with self.lock:
            return list(self.events)

    def _lookup(self, key: tuple[str, str], timeout_ms: int, deadline: float | None) -> Any:
        remaining_ms = timeout_ms if deadline is None else min(timeout_ms, int((deadline - time.monotonic()) * 1000))
        if remaining_ms <= 0:
            return None
        with self.lock:
            self.resolve_attempt_count += 1
        try:
            return _resolve_record(self.zc, key, remaining_ms, self.required_families)
        except Exception:
            with self.lock:
                self.resolve_error_count += 1
            return None

    def submit_pending(self, executor: Any, futures: dict[Any, Any], timeout_ms: int, deadline: float | None) -> None:
        busy = {key for key, _generation in futures.values()}
        with self.lock:
            # Failed names rotate behind fresh instances, so four slow peers cannot starve the list.
            keys = [key for key in self.pending if key not in busy]
            jobs = [(key, self.generations.get(key, 0)) for key in keys[:max(0, 4 - len(futures))]]
        for key, generation in jobs:
            futures[executor.submit(self._lookup, key, timeout_ms, deadline)] = (key, generation)

    def finish_pending(self, futures: dict[Any, Any]) -> None:
        for future in list(futures):
            if not future.done():
                continue
            key, generation = futures.pop(future)
            if future.cancelled():
                continue
            info = future.result()
            with self.lock:
                if generation != self.generations.get(key, 0) or key not in self.instances:
                    continue
                if info:
                    self.resolve_success_count += 1
                    self.add_record(info)
                    index = info.interface_index
                    if type(index) is int and index > 0:
                        self.instances[key].interface_index = index
                self.pending.pop(key, None)
                if info is None or not _record_complete(info, self.required_families):
                    # Rotate partial/failed work behind fresh names. Generations
                    # identify source changes, rather than scheduling priority.
                    self.pending[key] = None

    def add_record(self, observation: BonjourResolvedService) -> None:
        key = _observation_merge_key(observation)
        # Replace one source's previous data; append-only merging retains withdrawn addresses/TXT.
        self.observations = {k: v for k, v in self.observations.items() if (v.service_type, v.fullname) != (observation.service_type, observation.fullname)}
        self.observations[key] = observation

    def results(self) -> list[BonjourResolvedService]:
        with self.lock:
            return list(self.observations.values())

    def pending_count(self) -> int:
        with self.lock:
            return len(self.pending)


class PtrRecordObserver:
    def __init__(self, services: list[str], *, start_time: float):
        self.services = set(services)
        self.start_time = start_time
        self.lock = threading.RLock()
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

    for ip in info.parsed_scoped_addresses():
        try:
            ip_obj = ipaddress.ip_address(ip.split("%", 1)[0])
            index = getattr(info, "interface_index", None)
            if ip_obj.version == 6 and ip_obj.is_link_local and "%" not in ip and isinstance(index, int) and index > 0:
                ip = scoped_ip_literal(ip, scope_id=index) or ip
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
        interface_index=getattr(info, "interface_index", None) if type(getattr(info, "interface_index", None)) is int else None,
    )


def _zeroconf_interfaces_for_target(target_ip: str | None, *, family: BonjourIPFamily | None = None) -> list[str] | None:
    if not target_ip:
        return None
    source = select_route_to_address(target_ip, port=5353).source
    if source is None or ((":" in source) != (family == "ipv6")):
        return None
    return [source]


def _zeroconf_ip_version(IPVersion: Any, *, family: BonjourIPFamily | None = None) -> tuple[Any, str]:
    if family == "ipv6":
        try:
            return IPVersion.V6Only, "V6Only"
        except AttributeError:
            raise RuntimeError("Installed zeroconf does not support IPv6-only browsing")
    # Do not use IPVersion.All here. Current zeroconf can miss IPv4 answers in
    # that mode on macOS; callers that need dual-stack must run split browses.
    return IPVersion.V4Only, "V4Only"


def _zeroconf_ip_version_name(*, family: BonjourIPFamily | None = None) -> str:
    try:
        from zeroconf import IPVersion
    except Exception:
        return "V4Only"
    _ip_version, ip_version_name = _zeroconf_ip_version(IPVersion, family=family)
    return ip_version_name


def _format_zeroconf_interfaces(interfaces: Sequence[str] | None) -> str:
    if not interfaces:
        return "system-default"
    return ",".join(interfaces)


def _open_zeroconf(interfaces: Sequence[str] | None = None, *, family: BonjourIPFamily | None = None) -> Any:
    try:
        from zeroconf import IPVersion, Zeroconf
    except Exception as e:
        raise RuntimeError(missing_dependency_message("zeroconf", e)) from e

    ip_version, _ip_version_name = _zeroconf_ip_version(IPVersion, family=family)
    if interfaces:
        choices: list[str | int] = []
        for address in interfaces:
            choice: str | int = address
            if family == "ipv6" and isinstance(address, str) and "%" in address:
                index = ipv6_scope_index(address.partition("%")[2])
                if index is None:
                    raise ValueError(f"unknown local IPv6 scope in {address}")
                # zeroconf strips zones when selecting by address. Indexes also
                # distinguish interfaces that share identical link-local bytes.
                choice = index
            if choice not in choices:
                choices.append(choice)
        return Zeroconf(interfaces=choices, ip_version=ip_version)
    return Zeroconf(ip_version=ip_version)


def resolve_service_instance(
    instance: BonjourServiceInstance,
    timeout_ms: int = FINAL_PENDING_RESOLVE_TIMEOUT_MS,
    *,
    target_ip: str | None = None,
    family: BonjourIPFamily | None = None,
    interfaces: Sequence[str] | None = None,
    cancel: threading.Event | None = None,
    required_families: Sequence[BonjourIPFamily] | None = None,
    deadline: float | None = None,
) -> BonjourResolvedService | None:
    cancel = cancel or threading.Event()
    if family is None:
        record, detail = resolve_service_instance_detailed(instance, timeout_ms,
            target_ip=target_ip, interfaces=interfaces, cancel=cancel)
        if record is None and any(a.error for a in detail.attempts):
            raise BonjourDiscoveryError(detail.attempts)
        return record
    if interfaces is None and instance.interface_index:
        if family == "ipv6":
            interfaces = [instance.interface_index]
        else:
            interfaces = ipv4_interface_addresses(instance.interface_index)
            if not interfaces:
                return None
    if interfaces is None:
        interfaces = _zeroconf_interfaces_for_target(target_ip, family=family)
    zc = _open_zeroconf(interfaces, family=family)
    try:
        end = time.monotonic() + max(0, timeout_ms) / 1000
        if deadline is not None:
            end = min(end, deadline)
        required_families = required_families or (family,)
        record = None
        while time.monotonic() < end and not cancel.is_set():
            budget = min(PENDING_RESOLVE_TIMEOUT_MS, max(1, int((end - time.monotonic()) * 1000)))
            try:
                fresh = _resolve_record(zc, (instance.service_type, instance.fullname), budget, required_families)
            except Exception:
                if record is None:
                    raise
                break
            if fresh is not None:
                record = fresh
                if _record_complete(record, required_families):
                    break
            cancel.wait(min(0.05, max(0.0, end - time.monotonic())))
        if cancel.is_set():
            raise KeyboardInterrupt
    finally:
        try:
            zc.close()
        except Exception:
            pass
    return record


def _format_attempt_error(exc: BaseException) -> str:
    message = str(exc)
    name = type(exc).__name__
    return f"{name}: {message}" if message else name


def _ordered_attempts(
    attempts_by_family: dict[BonjourIPFamily, BonjourFamilyDiscoveryAttempt],
) -> list[BonjourFamilyDiscoveryAttempt]:
    return [
        attempts_by_family[family]
        for family in SPLIT_FAMILIES
        if family in attempts_by_family
    ]


def discover_snapshot_detailed(
    service: str | None = None,
    timeout: float = DEFAULT_BROWSE_TIMEOUT_SEC,
    *,
    target_ip: str | None = None,
    family: BonjourIPFamily | None = None,
    interfaces: Sequence[str] | None = None,
    deadline: float | None = None,
    service_types: Sequence[str] | None = None,
    cancel: threading.Event | None = None,
    required_families: Sequence[BonjourIPFamily] | None = None,
    browse_deadline: float | None = None,
) -> tuple[BonjourDiscoverySnapshot, BonjourDiscoveryDiagnostics]:
    service_types = list(service_types) if service_types is not None else _matching_service_types(service)
    cancel = cancel or threading.Event()
    start = time.monotonic()
    zeroconf_interfaces = interfaces if interfaces is not None else _zeroconf_interfaces_for_target(target_ip, family=family)
    zc = _open_zeroconf(zeroconf_interfaces, family=family)
    ptr_observer: PtrRecordObserver | None = None
    ptr_records: list[BonjourPtrRecordObservation] = []
    ptr_record_error: str | None = None
    executor = ThreadPoolExecutor(max_workers=4)
    futures: dict[Any, Any] = {}
    collector = Collector(zc, service_types, start_time=start,
                          required_families=required_families or ((family,) if family else SPLIT_FAMILIES))
    try:
        ptr_observer = PtrRecordObserver(service_types, start_time=start)
        ptr_observer.start(zc)
        collector.start()
        browse_deadline = browse_deadline if browse_deadline is not None else start + max(0.0, timeout)
        if deadline is not None:
            browse_deadline = min(browse_deadline, deadline)
        resolve_deadline = browse_deadline + FINAL_PENDING_RESOLVE_TIMEOUT_MS / 1000
        if deadline is not None:
            resolve_deadline = min(resolve_deadline, deadline)
        while time.monotonic() < resolve_deadline:
            if cancel.is_set():
                raise KeyboardInterrupt
            now = time.monotonic()
            if now >= browse_deadline:
                with collector.lock:
                    collector.accepting = False
                if not collector.pending_count() and not futures:
                    break
            collector.finish_pending(futures)
            collector.submit_pending(executor, futures, PENDING_RESOLVE_TIMEOUT_MS,
                                     min(resolve_deadline, browse_deadline) if now < browse_deadline else resolve_deadline)
            time.sleep(min(0.05, max(0.0, resolve_deadline - time.monotonic())))
        collector.finish_pending(futures)
    except (KeyboardInterrupt, SystemExit):
        cancel.set()
        raise
    finally:
        collector.accepting = False
        collector.stop()
        for future in futures:
            future.cancel()
        executor.shutdown(wait=True, cancel_futures=True)
        if not cancel.is_set():
            collector.finish_pending(futures)
        if ptr_observer is not None:
            ptr_observer.stop(zc)
            ptr_records = ptr_observer.observations()
            ptr_record_error = ptr_observer.error
        try:
            zc.close()
        except Exception:
            pass

    sorted_instances = _sort_instances(collector.service_instances())
    sorted_records = _sort_records(collector.results())
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
        instances=sorted_instances,
        resolved=sorted_records,
        service_events=collector.service_events(),
        ptr_records=ptr_records,
        ptr_record_error=ptr_record_error,
    )
    return snapshot, diagnostics


def discover_snapshot_merged_detailed(
    service: str | None = None,
    timeout: float = DEFAULT_BROWSE_TIMEOUT_SEC,
    *, target_ip: str | None = None, family: BonjourIPFamily | None = None,
    interfaces: Sequence[str] | None = None, deadline: float | None = None,
    service_types: Sequence[str] | None = None, cancel: threading.Event | None = None,
) -> tuple[BonjourDiscoverySnapshot, BonjourQueryDiagnostics]:
    service_types = list(service_types) if service_types is not None else _matching_service_types(service)
    cancel = cancel or threading.Event()
    families = (family,) if family else SPLIT_FAMILIES
    start = time.monotonic()
    browse_end = start + max(0.0, timeout)
    end = browse_end + FINAL_PENDING_RESOLVE_TIMEOUT_MS / 1000
    if deadline is not None:
        browse_end, end = min(browse_end, deadline), min(end, deadline)
    attempts_by_family: dict[BonjourIPFamily, BonjourFamilyDiscoveryAttempt] = {}

    executor = ThreadPoolExecutor(max_workers=len(families))
    try:
        # Run one browse per family instead of IPVersion.All; All has proven
        # unreliable for returning IPv4 records in this environment.
        futures = {
            executor.submit(
                discover_snapshot_detailed,
                service,
                timeout,
                family=family, target_ip=target_ip, interfaces=interfaces, deadline=end,
                browse_deadline=browse_end, required_families=families,
                service_types=service_types, cancel=cancel,
            ): family
            for family in families
        }
        for future in as_completed(futures):
            family = futures[future]
            try:
                snapshot, diagnostics = future.result()
            except Exception as exc:
                attempts_by_family[family] = BonjourFamilyDiscoveryAttempt(
                    family=family,
                    error=_format_attempt_error(exc),
                )
                continue

            attempts_by_family[family] = BonjourFamilyDiscoveryAttempt(
                family=family,
                snapshot=snapshot,
                diagnostics=diagnostics,
            )

    except BaseException:
        cancel.set()
        raise
    finally:
        executor.shutdown(wait=True, cancel_futures=True)

    attempts = _ordered_attempts(attempts_by_family)
    snapshots = [attempt.snapshot for attempt in attempts if attempt.snapshot is not None]
    merged_snapshot = _merge_snapshots(snapshots)
    if not merged_snapshot.instances and not merged_snapshot.resolved and any(attempt.error for attempt in attempts):
        raise BonjourDiscoveryError(attempts)

    diagnostics = BonjourQueryDiagnostics(
        provider="zeroconf",
        service_types=list(service_types),
        timeout_sec=timeout,
        elapsed_sec=round(time.monotonic() - start, 3),
        instance_count=len(merged_snapshot.instances),
        resolved_count=len(merged_snapshot.resolved),
        pending_count=sum(a.diagnostics.pending_count for a in attempts if a.diagnostics is not None),
        errors={a.family: a.error for a in attempts if a.error},
        attempts=attempts,
    )
    return merged_snapshot, diagnostics




def resolve_service_instance_detailed(instance: BonjourServiceInstance, timeout_ms: int = 3000,
                                      *, family: BonjourIPFamily | None = None,
                                      cancel: threading.Event | None = None, target_ip: str | None = None,
                                      interfaces: Sequence[str] | None = None) -> tuple[BonjourResolvedService | None, BonjourQueryDiagnostics]:
    cancel = cancel or threading.Event()
    start = time.monotonic()
    end = start + max(0, timeout_ms) / 1000
    families = (family,) if family else SPLIT_FAMILIES
    executor = ThreadPoolExecutor(max_workers=len(families))
    attempts = []
    try:
        futures = {executor.submit(resolve_service_instance, instance, timeout_ms,
                   family=f, cancel=cancel, target_ip=target_ip, interfaces=interfaces,
                   required_families=families, deadline=end): f for f in families}
        for future in as_completed(futures):
            f = futures[future]
            try:
                record = future.result()
                attempts.append(BonjourFamilyDiscoveryAttempt(f, BonjourDiscoverySnapshot([], [record] if record else [])))
            except Exception as exc:
                attempts.append(BonjourFamilyDiscoveryAttempt(f, error=_format_attempt_error(exc)))
    except BaseException:
        cancel.set()
        raise
    finally:
        executor.shutdown(wait=True, cancel_futures=True)
    attempts.sort(key=lambda a: a.family)
    snapshot = _merge_snapshots([a.snapshot for a in attempts if a.snapshot is not None])
    detail = BonjourQueryDiagnostics("zeroconf", [instance.service_type], timeout_ms / 1000,
             round(time.monotonic() - start, 3), 0, len(snapshot.resolved),
             errors={a.family: a.error for a in attempts if a.error}, attempts=attempts)
    return (snapshot.resolved[0] if snapshot.resolved else None), detail
