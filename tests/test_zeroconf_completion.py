from __future__ import annotations

import time
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

import pytest

from tests.fixtures.bonjour import install_zeroconf, records
from timecapsulesmb.discovery import bonjour, zeroconf_backend


@pytest.mark.parametrize("late_family", ["ipv4", "ipv6"])
def test_partial_answers_on_both_transports_complete_during_shared_grace(monkeypatch, late_family):
    closed = install_zeroconf(monkeypatch, records()[:1], address_delays={late_family: .35})
    snapshot, diagnostics = bonjour.BonjourQuery().browse("_airport", .15)
    assert snapshot.resolved[0].ipv4 == ["192.0.2.10"]
    assert snapshot.resolved[0].ipv6 == ["fd00::10"]
    assert .3 <= diagnostics.elapsed_sec < 1.5
    assert diagnostics.pending_count == 0
    assert sorted(closed) == ["ipv4", "ipv6"]


def test_single_stack_scan_preserves_partial_answer_at_shared_deadline(monkeypatch):
    install_zeroconf(monkeypatch, [{**records()[0], "ipv6": []}])
    start = time.monotonic()
    snapshot, diagnostics = bonjour.BonjourQuery().browse("_airport", .1, deadline=start + .3)
    assert snapshot.resolved[0].ipv4 == ["192.0.2.10"]
    assert snapshot.resolved[0].ipv6 == []
    assert .25 <= diagnostics.elapsed_sec < 1
    assert diagnostics.pending_count == 2


@pytest.mark.parametrize("family", ["ipv4", "ipv6"])
def test_explicit_family_does_not_wait_for_the_other(monkeypatch, family):
    install_zeroconf(monkeypatch, [{**records()[0], "ipv6" if family == "ipv4" else "ipv4": []}])
    snapshot, diagnostics = bonjour.BonjourQuery().browse("_airport", .1, family=family)
    assert getattr(snapshot.resolved[0], family)
    assert diagnostics.elapsed_sec < .8
    assert diagnostics.pending_count == 0


@pytest.mark.parametrize("late_family", ["ipv4", "ipv6"])
def test_targeted_resolve_collects_late_family_within_its_existing_budget(monkeypatch, late_family):
    install_zeroconf(monkeypatch, records()[:1], address_delays={late_family: .2})
    instance = bonjour.BonjourServiceInstance("_airport._tcp.local.", "Office", "Office._airport._tcp.local.")
    record, diagnostics = bonjour.BonjourQuery().resolve_detailed(instance, 400)
    assert record.ipv4 == ["192.0.2.10"] and record.ipv6 == ["fd00::10"]
    assert diagnostics.elapsed_sec < 1


@pytest.mark.parametrize("later", [None, OSError("later lookup failed")])
def test_targeted_resolve_preserves_earlier_evidence_after_an_inconclusive_retry(monkeypatch, later):
    install_zeroconf(monkeypatch, [{**records()[0], "ipv6": []}])
    open_zc = zeroconf_backend._open_zeroconf
    def opened(*args, **kwargs):
        zc = open_zc(*args, **kwargs)
        initial = zc.get_service_info("_airport._tcp.local.", "Office._airport._tcp.local.", 500)
        zc.get_service_info = mock.Mock(side_effect=[initial, later, later, later])
        return zc
    monkeypatch.setattr(zeroconf_backend, "_open_zeroconf", opened)
    instance = bonjour.BonjourServiceInstance("_airport._tcp.local.", "Office", "Office._airport._tcp.local.")
    record, _diagnostics = bonjour.BonjourQuery().resolve_detailed(instance, 900)
    assert record.ipv4 == ["192.0.2.10"] and record.ipv6 == []


def test_late_link_local_answer_retains_the_observed_interface(monkeypatch):
    from timecapsulesmb.core.net import same_scoped_ip

    install_zeroconf(monkeypatch, [{**records()[0], "ipv6": ["fe80::10"]}], address_delays={"ipv6": .15})
    snapshot, _diagnostics = bonjour.BonjourQuery().browse("_airport", .1)
    assert len(snapshot.resolved[0].ipv6) == 1
    assert same_scoped_ip(snapshot.resolved[0].ipv6[0], "fe80::10%14")


def test_public_scan_collects_ipv6_after_five_second_browse(monkeypatch):
    from timecapsulesmb.app.context import AppOperationContext
    from timecapsulesmb.app.events import EventSink
    from timecapsulesmb.app.ops.discovery import discover_operation

    install_zeroconf(monkeypatch, records()[:1], address_delays={"ipv6": 5.3})
    result = discover_operation({"timeout": 5, "service": "_airport"},
                               AppOperationContext("discover", EventSink(lambda _event: None)))
    assert result.payload["devices"][0]["ipv6"] == ["fd00::10"]


def test_cached_service_triggers_a_real_missing_family_query(monkeypatch):
    from zeroconf import DNSIncoming, DNSOutgoing, IPVersion, ServiceInfo, Zeroconf

    stype = "_airport._tcp.local."
    name = "Completion test." + stype
    cached = ServiceInfo(stype, name, server="completion-test.local.", port=5009,
                         parsed_addresses=["192.0.2.10"], properties={"syAP": "116"})
    late = ServiceInfo(stype, name, server=cached.server, port=5009, parsed_addresses=["fd00::10"])
    def packet(records):
        outgoing = DNSOutgoing(0x8400)
        for record in records:
            outgoing.add_answer_at_time(record, 0)
        return DNSIncoming(outgoing.packets()[0])
    seeded = threading.Event()
    questions = []
    def send(zc, outgoing, *args, **kwargs):
        questions.extend(question.type for question in outgoing.questions)
        if any(question.type == 28 for question in outgoing.questions):
            zc.loop.call_later(.05, zc.record_manager.async_updates_from_response,
                               packet(late.dns_addresses()))
    # Exercise the real cache, request generation and response/listener delivery.
    # Only the socket send is replaced, so the check never depends on a LAN peer.
    monkeypatch.setattr(Zeroconf, "async_send", send)
    with Zeroconf(interfaces=["127.0.0.1"], ip_version=IPVersion.V4Only) as zc:
        def seed():
            zc.record_manager.async_updates_from_response(packet(
                [cached.dns_service(), cached.dns_text(), *cached.dns_addresses()]))
            seeded.set()
        zc.loop.call_soon_threadsafe(seed)
        assert seeded.wait(3)
        record = zeroconf_backend._resolve_record(zc, (stype, name), 1000, ("ipv4", "ipv6"))
    assert record.ipv4 == ["192.0.2.10"] and record.ipv6 == ["fd00::10"]
    assert questions == [28]


def test_partial_services_rotate_behind_fresh_names(monkeypatch):
    observations = [{**records()[0], "name": f"Device {index}", "ipv6": [] if index < 4 else ["fd00::10"]}
                    for index in range(6)]
    install_zeroconf(monkeypatch, observations)
    calls = []
    open_zc = zeroconf_backend._open_zeroconf
    def opened(*args, **kwargs):
        zc = open_zc(*args, **kwargs)
        lookup = zc.get_service_info
        def get(stype, fullname, *args, **kwargs):
            calls.append(fullname)
            return lookup(stype, fullname, *args, **kwargs)
        zc.get_service_info = get
        return zc
    monkeypatch.setattr(zeroconf_backend, "_open_zeroconf", opened)
    snapshot, _diagnostics = bonjour.BonjourQuery().browse("_airport", .1, deadline=time.monotonic() + .9)
    assert {r.name for r in snapshot.resolved} == {r["name"] for r in observations}
    # Both transports eventually give the later devices their first attempt.
    assert all(sum(name.startswith(f"Device {index}.") for name in calls) >= 2 for index in (4, 5))


def test_cancelling_missing_family_grace_closes_both_transports(monkeypatch):
    closed = install_zeroconf(monkeypatch, records()[:1], address_delays={"ipv6": 10})
    query = bonjour.BonjourQuery()
    started = threading.Event()
    request = zeroconf_backend._resolve_record
    def entered(*args, **kwargs):
        started.set()
        return request(*args, **kwargs)
    monkeypatch.setattr(zeroconf_backend, "_resolve_record", entered)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(query.browse, "_airport", .1)
        assert started.wait(3)
        query.cancel.set()
        with pytest.raises(KeyboardInterrupt):
            future.result(timeout=3)
    assert sorted(closed) == ["ipv4", "ipv6"]


@pytest.mark.parametrize("change", ["remove", "replace_target"])
def test_missing_family_worker_cannot_restore_an_old_service_generation(monkeypatch, change):
    from zeroconf import ServiceStateChange
    from tests.fixtures.bonjour import info

    observation = records()[0]
    name = observation["name"] + "." + observation["service_type"]
    entered, release = threading.Event(), threading.Event()
    zc = mock.Mock(get_service_info=lambda *_args, **_kwargs: info({**observation, "ipv6": []}))
    collector = zeroconf_backend.Collector(zc, [observation["service_type"]])
    def event(state):
        collector._on_service_state_change(zeroconf=zc, service_type=observation["service_type"],
                                          name=name, state_change=state)
    class Resolver:
        interface_index = None
        def __init__(self, server): pass
        def request(self, *_args, **_kwargs):
            entered.set()
            assert release.wait(3)
            return True
        def parsed_scoped_addresses(self, version): return ["fd00::10"]
    monkeypatch.setattr("zeroconf.AddressResolverIPv6", Resolver)
    event(ServiceStateChange.Added)
    with ThreadPoolExecutor(max_workers=1) as pool:
        futures = {}
        collector.submit_pending(pool, futures, 500, None)
        assert entered.wait(3)
        event(ServiceStateChange.Removed if change == "remove" else ServiceStateChange.Updated)
        if change == "replace_target":
            zc.get_service_info = lambda *_args, **_kwargs: info({**observation, "hostname": "new.local"})
        release.set()
    collector.finish_pending(futures)
    assert collector.results() == []
    assert collector.pending_count() == (0 if change == "remove" else 1)
    if change == "replace_target":
        with ThreadPoolExecutor(max_workers=1) as pool:
            collector.submit_pending(pool, futures, 500, None)
        collector.finish_pending(futures)
        assert collector.results()[0].hostname == "new.local"
