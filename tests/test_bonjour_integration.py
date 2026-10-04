from __future__ import annotations

import io
import json
import signal
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from unittest import mock

import pytest

from tests.fixtures.bonjour import install_native, install_zeroconf, records, cap_browse_window, info
from timecapsulesmb.app.context import AppOperationContext
from timecapsulesmb.app.events import EventSink
from timecapsulesmb.app.ops.discovery import discover_operation
from timecapsulesmb.cli.discover import run_cli
from timecapsulesmb.discovery import bonjour, native_dns_sd, zeroconf_backend
from timecapsulesmb.discovery.models import SERVICE_TYPES


@pytest.fixture(params=["dns-sd", "zeroconf"])
def provider(request, monkeypatch, tmp_path):
    observations = records()
    if request.param == "dns-sd":
        resources = install_native(monkeypatch, tmp_path, observations)
    else:
        resources = install_zeroconf(monkeypatch, observations)
    requested = cap_browse_window(monkeypatch, request.param)
    yield request.param, resources, requested
    if request.param == "dns-sd":
        assert all(proc.poll() is not None for proc in resources)
    else:
        assert {"ipv4", "ipv6"} <= set(resources)


def test_cli_json_keeps_broad_results_and_neutral_decoding(provider):
    out = io.StringIO()
    with redirect_stdout(out):
        assert run_cli(["--timeout", "5", "--json"]) == 0
    payload = json.loads(out.getvalue())
    assert {r["service_type"] for r in payload["resolved"]} == set(SERVICE_TYPES)
    assert len(payload["instances"]) == 5
    airport = next(r for r in payload["resolved"] if r["service_type"].startswith("_airport"))
    assert airport["properties"]["syAP"] == "116"
    assert airport["ipv4"] == ["192.0.2.10"] and airport["ipv6"] == ["fd00::10"]
    assert next(r for r in payload["resolved"] if r["service_type"].startswith("_device-info"))["port"] == 0
    assert provider[2] == [(None, 5.0)]


def test_api_gui_filter_and_selected_record_roundtrip(provider):
    from timecapsulesmb.services.configure_target import bonjour_record_from_selected_record
    context = AppOperationContext("discover", EventSink(lambda _event: None))
    result = discover_operation({"timeout": 5.5, "service": "_airport"}, context)
    assert result.ok
    payload = result.payload
    assert payload["schema_version"] == 1
    assert payload["counts"] == {"instances": 1, "resolved": 1, "devices": 1}
    candidate = payload["devices"][0]
    assert candidate["host"] == "192.0.2.10" and candidate["ssh_host"] == "root@192.0.2.10"
    assert candidate["model"] == "TimeCapsule6,116" and candidate["supported_model"] is True
    selected = bonjour_record_from_selected_record(candidate["selected_record"])
    assert selected is not None and selected.interface_index == 14
    assert selected.ipv4 == ["192.0.2.10"] and selected.ipv6 == ["fd00::10"]
    assert provider[2] == [("_airport", 5.5)]


@pytest.mark.parametrize("provider_name", ["dns-sd", "zeroconf"])
@pytest.mark.parametrize("name", ["Error -65570", "Error -65554", "120 Error code -65570"])
def test_error_like_device_names_and_txt_survive_real_adapter_and_api(monkeypatch, tmp_path, provider_name, name):
    observations = [{**records()[0], "name": name,
                     "properties": {"syAP": "116", "ErrorCode": "-65570", "label": "ErrorCode=-65554"}}]
    if provider_name == "dns-sd":
        resources = install_native(monkeypatch, tmp_path, observations)
    else:
        resources = install_zeroconf(monkeypatch, observations)
    cap_browse_window(monkeypatch, provider_name)
    result = discover_operation({"timeout": 5, "service": "_airport"},
                               AppOperationContext("discover", EventSink(lambda _event: None)))
    assert result.ok and result.payload["counts"]["devices"] == 1
    device = result.payload["devices"][0]
    assert device["name"] == name and device["host"] == "192.0.2.10"
    assert device["selected_record"]["properties"]["ErrorCode"] == "-65570"
    assert device["ipv4"] == ["192.0.2.10"] and device["ipv6"] == ["fd00::10"]
    if provider_name == "dns-sd":
        assert all(p.poll() is not None for p in resources)
    else:
        assert sorted(resources) == ["ipv4", "ipv6"]


def test_native_complete_fragmented_denial_keeps_typed_failure_and_reaps_child(monkeypatch):
    import sys
    from timecapsulesmb.discovery.models import BonjourPermissionDenied
    owner = native_dns_sd._ProcessOwner(threading.Event())
    launch = owner.launch
    script = "import sys,time; sys.stdout.write('10:20:00 Error code -65'); sys.stdout.flush(); time.sleep(.05); print('570',flush=True)"
    monkeypatch.setattr(owner, "launch", lambda _args: launch([sys.executable, "-u", "-c", script]))
    with pytest.raises(BonjourPermissionDenied):
        native_dns_sd._run_dns_sd_command(["dns-sd", "-B", "_airport._tcp", "local"], timeout_sec=2, owner=owner)
    assert not owner.children


@pytest.mark.parametrize("timeout", [-1, 0, 4.999, True, None, "bad", "nan", float("inf")])
def test_public_timeout_rejects_before_network(timeout, monkeypatch):
    query = mock.Mock(side_effect=AssertionError("must not query"))
    monkeypatch.setattr(bonjour, "command_exists", query)
    from timecapsulesmb.services.app import AppOperationError
    context = AppOperationContext("discover", EventSink(lambda _event: None))
    with pytest.raises(AppOperationError) as exc:
        discover_operation({"timeout": timeout}, context)
    assert exc.value.code == "discovery_timeout_too_short"
    query.assert_not_called()


@pytest.mark.parametrize("timeout", [5, 5.5, 6, "5.5"])
def test_public_timeout_policy_accepts_valid_values(timeout):
    assert bonjour.validate_discovery_timeout(timeout) == float(timeout)


def test_known_permission_denial_stops_before_provider_selection(monkeypatch):
    select = mock.Mock(side_effect=AssertionError("must not open network"))
    monkeypatch.setattr(bonjour, "command_exists", select)
    from timecapsulesmb.services.app import AppOperationError
    context = AppOperationContext("discover", EventSink(lambda _event: None))
    with pytest.raises(AppOperationError) as exc:
        discover_operation({"timeout": 6, "macos_local_network_preflight_result": "denied"}, context)
    assert exc.value.code == "local_network_permission_denied"
    select.assert_not_called()


@pytest.mark.parametrize("delayed_family", ["ipv4", "ipv6"])
def test_native_late_second_family_is_collected_during_shared_grace(monkeypatch, tmp_path, delayed_family):
    # Real child startup consumes browse time; exercise the public minimum budget.
    children = install_native(monkeypatch, tmp_path, records()[:1], address_delay=5.3, delayed_family=delayed_family)
    snapshot, diagnostics = bonjour.discover_snapshot_detailed("_airport", timeout=5)
    assert snapshot.resolved[0].ipv4 == ["192.0.2.10"]
    assert snapshot.resolved[0].ipv6 == ["fd00::10"]
    assert diagnostics.provider == "dns-sd" and 5 <= diagnostics.elapsed_sec < 9
    assert all(p.poll() is not None for p in children)


def test_native_address_replacement_between_children_reaches_api_selection(monkeypatch, tmp_path):
    observations = [{**records()[0], "ipv6": [],
                     "address_updates": [[.6, ["192.0.2.20"], ["fd00::20"]]]}]
    children = install_native(monkeypatch, tmp_path, observations)
    cap_browse_window(monkeypatch, "dns-sd")
    result = discover_operation({"timeout": 5, "service": "_airport"},
                               AppOperationContext("discover", EventSink(lambda _event: None)))
    device = result.payload["devices"][0]
    assert device["ipv4"] == ["192.0.2.20"] and device["ipv6"] == ["fd00::20"]
    assert device["host"] == "192.0.2.20" and device["ssh_host"] == "root@192.0.2.20"
    assert device["selected_record"]["ipv4"] == ["192.0.2.20"]
    assert all(p.poll() is not None for p in children)


@pytest.mark.parametrize("family,expected_v4,expected_v6", [
    ("ipv4", ["192.0.2.10"], []), ("ipv6", [], ["fd00::10"]),
])
def test_native_targeted_query_requests_only_selected_record_types(monkeypatch, tmp_path, family, expected_v4, expected_v6):
    children = install_native(monkeypatch, tmp_path, records()[:1])
    instance = bonjour.BonjourServiceInstance("_airport._tcp.local.", "Office", "Office._airport._tcp.local.", 14)
    record, diagnostics = bonjour.BonjourQuery().resolve_detailed(instance, 500, family=family)
    assert record.ipv4 == expected_v4 and record.ipv6 == expected_v6
    assert diagnostics.provider == "dns-sd" and diagnostics.errors == {}
    assert all(p.poll() is not None for p in children)


@pytest.mark.parametrize("failed_family", ["ipv4", "ipv6"])
def test_zeroconf_partial_scan_has_only_evidence_from_healthy_transport(monkeypatch, failed_family):
    observation = records()[0]
    per_family = {"ipv4": [{**observation, "ipv6": []}], "ipv6": [{**observation, "ipv4": []}]}
    closed = install_zeroconf(monkeypatch, [observation], fail_family=failed_family, family_observations=per_family)
    snapshot, diagnostics = bonjour.BonjourQuery().browse("_airport", .15)
    record = snapshot.resolved[0]
    assert record.ipv4 == ([] if failed_family == "ipv4" else ["192.0.2.10"])
    assert record.ipv6 == ([] if failed_family == "ipv6" else ["fd00::10"])
    assert failed_family in diagnostics.errors
    assert closed == ["ipv6" if failed_family == "ipv4" else "ipv4"]


@pytest.mark.parametrize("late_family", ["ipv4", "ipv6"])
def test_zeroconf_delayed_transport_completes_during_shared_grace(monkeypatch, late_family):
    observation = records()[0]
    closed = install_zeroconf(monkeypatch, [observation], delays={late_family: .35}, family_observations={
        "ipv4": [{**observation, "ipv6": []}], "ipv6": [{**observation, "ipv4": []}],
    })
    snapshot, diagnostics = bonjour.BonjourQuery().browse("_airport", .15)
    assert snapshot.resolved[0].ipv4 == ["192.0.2.10"]
    assert snapshot.resolved[0].ipv6 == ["fd00::10"]
    assert diagnostics.errors == {}
    assert sorted(closed) == ["ipv4", "ipv6"]


@pytest.mark.parametrize("name", ["Office", " AirPort Time\u00a0Capsule "])
def test_native_removed_then_readded_instance_survives_old_inflight_resolve(monkeypatch, tmp_path, name):
    import signal
    # Withdraw only after the first generation is resolving, then release it
    # once the real browse parser has delivered the replacement generation.
    observations = [{**records()[0], "name": name, "browse_events": [[None, "Rmv"], [0, "Add"]]}]
    children = install_native(monkeypatch, tmp_path, observations)
    original = native_dns_sd._resolve
    parse = native_dns_sd._parse_dns_sd_browse_output
    replacement_seen = threading.Event()
    withdrawn = False
    def observed(*args):
        nonlocal withdrawn
        events, malformed = parse(*args)
        for event in events:
            if event.action == "Rmv":
                withdrawn = True
            elif withdrawn and event.action == "Add":
                replacement_seen.set()
        return events, malformed
    calls = 0
    def resolve(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            children[0].send_signal(signal.SIGUSR1)
            assert replacement_seen.wait(3)
        return original(*args, **kwargs)
    monkeypatch.setattr(native_dns_sd, "_parse_dns_sd_browse_output", observed)
    monkeypatch.setattr(native_dns_sd, "_resolve", resolve)
    snapshot, _diagnostics = bonjour.discover_snapshot_detailed("_airport", timeout=2)
    assert calls >= 2
    assert len(snapshot.instances) == 1 and len(snapshot.resolved) == 1
    assert snapshot.resolved[0].ipv4 == ["192.0.2.10"]
    assert all(p.poll() is not None for p in children)


@pytest.mark.parametrize("name", ["Office", " AirPort Time\u00a0Capsule "])
def test_native_removal_is_scoped_to_its_interface(monkeypatch, tmp_path, name):
    observations = [{**records()[0], "name": name, "browse_events": [[.1, "Rmv"]]},
                    {**records()[0], "name": name, "interface_index": 18}]
    children = install_native(monkeypatch, tmp_path, observations)
    snapshot, _diagnostics = bonjour.discover_snapshot_detailed("_airport", timeout=2)
    assert [r.interface_index for r in snapshot.resolved] == [18]
    assert [r.interface_index for r in snapshot.instances] == [18]
    assert all(p.poll() is not None for p in children)


def test_native_keeps_full_browse_window_but_closes_admission_for_grace(monkeypatch, tmp_path):
    observations = [
        {**records()[0], "name": "Late", "initial": False, "browse_events": [[.2, "Add"]]},
        {**records()[0], "name": "Too late", "initial": False, "browse_events": [[5.3, "Add"]]},
    ]
    children = install_native(monkeypatch, tmp_path, observations, address_delay=5.5)
    snapshot, diagnostics = bonjour.discover_snapshot_detailed("_airport", timeout=5)
    assert [r.name for r in snapshot.resolved] == ["Late"]
    assert 5 <= diagnostics.elapsed_sec < 9
    assert all(p.poll() is not None for p in children)


@pytest.mark.parametrize("name", ["Office", " AirPort Time\u00a0Capsule "])
def test_native_withdrawal_during_resolution_grace_invalidates_admitted_service(monkeypatch, tmp_path, name):
    observations = [{**records()[0], "name": name, "browse_events": [[2.3, "Rmv"]]}]
    children = install_native(monkeypatch, tmp_path, observations, address_delay=20)
    snapshot, diagnostics = bonjour.discover_snapshot_detailed("_airport", timeout=2)
    assert snapshot.instances == [] and snapshot.resolved == []
    assert diagnostics.pending_count == 0
    assert all(p.poll() is not None for p in children)


def test_zeroconf_cancellation_closes_both_family_workers_and_listeners(monkeypatch):
    import time
    import zeroconf
    closed = install_zeroconf(monkeypatch, records()[:1])
    entered = threading.Event()
    def slow_info(*args, **kwargs):
        entered.set()
        time.sleep(.15)  # A bounded in-progress ServiceInfo request.
        return None
    monkeypatch.setattr(zeroconf.Zeroconf, "get_service_info", slow_info)
    query = bonjour.BonjourQuery()
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(query.browse, "_airport", 6)
        assert entered.wait(3)
        query.cancel.set()
        with pytest.raises(KeyboardInterrupt): future.result(timeout=2)
    assert sorted(closed) == ["ipv4", "ipv6"]
    assert time.monotonic() - started < 2


def test_native_pool_overlaps_and_more_than_four_devices_are_not_starved(monkeypatch, tmp_path):
    observations = [{**records()[0], "name": f"Device {i}", "hostname": f"device-{i}.local"} for i in range(6)]
    children = install_native(monkeypatch, tmp_path, observations)
    original = native_dns_sd._resolve
    lock = threading.Lock()
    gate = threading.Barrier(4)
    active = peak = entered = 0
    def resolve(*args, **kwargs):
        nonlocal active, peak, entered
        with lock:
            active += 1; entered += 1; peak = max(peak, active)
            initial = entered <= 4
        try:
            if initial: gate.wait(timeout=3)
            return original(*args, **kwargs)
        finally:
            with lock: active -= 1
    monkeypatch.setattr(native_dns_sd, "_resolve", resolve)
    snapshot, _diag = bonjour.discover_snapshot_detailed("_airport", timeout=2)
    assert len(snapshot.resolved) == 6 and peak == 4
    assert all(p.poll() is not None for p in children)


def test_zeroconf_inflight_removed_generation_cannot_resurrect(monkeypatch):
    from zeroconf import ServiceStateChange
    arrived, release = threading.Event(), threading.Event()
    record = records()[0]
    def lookup(*args, **kwargs):
        arrived.set(); assert release.wait(3); return info(record)
    collector = zeroconf_backend.Collector(mock.Mock(get_service_info=lookup), [record["service_type"]])
    instance_name = f'{record["name"]}.{record["service_type"]}'
    def event(state):
        collector._on_service_state_change(zeroconf=collector.zc, service_type=record["service_type"], name=instance_name, state_change=state)
    event(ServiceStateChange.Added)
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {}
        collector.submit_pending(pool, futures, 500, None)
        assert arrived.wait(3)
        event(ServiceStateChange.Removed)
        event(ServiceStateChange.Added)
        release.set()
        for f in futures: f.result()
        collector.finish_pending(futures)
    assert collector.results() == []
    assert collector.pending_count() == 1


def test_acp_partial_family_failure_does_not_report_old_ipv6_removed():
    from timecapsulesmb.services.acp_diagnostics import fresh_lookup
    from timecapsulesmb.discovery.models import BonjourResolvedService
    before = BonjourResolvedService("Office", "office.local", "_airport._tcp.local.", ipv4=["192.0.2.10"], ipv6=["fd00::10"], interface_index=14)
    def resolve(instance, _timeout, *, family, **_kwargs):
        assert instance.interface_index == 14
        if family == "ipv6": raise OSError("IPv6 query failed")
        return BonjourResolvedService("Office", "office.local", "_airport._tcp.local.", ipv4=["192.0.2.10"])
    with mock.patch.object(bonjour, "command_exists", return_value=False), mock.patch.object(zeroconf_backend, "resolve_service_instance", side_effect=resolve):
        result = fresh_lookup(before)
    assert result["ipv6"] == "error"
    assert result["addresses_changed"] is False and result["removed"] == []


def test_candidate_boundary_keeps_conflicting_ports_as_distinct_evidence():
    from timecapsulesmb.discovery.devices import device_candidates_from_records
    from timecapsulesmb.discovery.models import BonjourResolvedService
    candidates = device_candidates_from_records([
        BonjourResolvedService("Office", "office.local", "_airport._tcp.local.", port=port,
                               fullname="Office._airport._tcp.local.", ipv4=["192.0.2.10"])
        for port in (5009, 5010)
    ])
    assert len(candidates) == len({c.id for c in candidates}) == 2
    assert all(c.airport_mac is None for c in candidates)
    assert {c.selected_record.port for c in candidates} == {5009, 5010}


# A shell's `&` job hands its children SIGINT ignored; the helper must still cancel.
@pytest.mark.parametrize("inherited_sigint", [signal.SIG_DFL, signal.SIG_IGN], ids=["default", "ignored"])
def test_real_helper_sigint_reaps_several_native_children_and_emits_one_terminal_event(tmp_path, monkeypatch, inherited_sigint):
    import os
    import selectors
    import subprocess
    import sys
    import time
    env = dict(os.environ, PYTHONPATH=str(__import__('pathlib').Path(__file__).resolve().parents[1] / "src") + os.pathsep + str(__import__('pathlib').Path(__file__).resolve().parents[1]), TCAPSULE_STATE_DIR=str(tmp_path / "state"))
    previous = signal.signal(signal.SIGINT, inherited_sigint)
    try:
        proc = subprocess.Popen([sys.executable, "-u", "-m", "tests.fixtures.bonjour_cancel_helper", str(tmp_path)], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    finally:
        signal.signal(signal.SIGINT, previous)
    child_pids = []
    stderr = bytearray()
    try:
        proc.stdin.write(json.dumps({"operation": "discover", "params": {"timeout": 6, "service": "_airport"}}).encode())
        proc.stdin.close(); proc.stdin = None
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stderr, selectors.EVENT_READ)
            end = time.monotonic() + 10
            while len(child_pids) < 9 and time.monotonic() < end:
                for key, _events in selector.select(.1):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk: break
                    stderr.extend(chunk)
                child_pids = [int(line.split()[1]) for line in stderr.decode().split("\n")[:-1] if line.startswith("CHILD ")]
                if proc.poll() is not None: break
        assert len(child_pids) >= 9, stderr.decode()
        proc.send_signal(signal.SIGINT)
        out, remaining_err = proc.communicate(timeout=3)
        assert proc.returncode == 130, (out, remaining_err)
        events = [json.loads(line) for line in out.splitlines()]
        terminals = [e for e in events if e["type"] in {"result", "error"}]
        assert len(terminals) == 1 and terminals[0]["code"] == "cancelled"
        for pid in child_pids:
            with pytest.raises(ProcessLookupError): os.kill(pid, 0)
        children = install_native(monkeypatch, tmp_path, records()[:1])
        cap_browse_window(monkeypatch, "dns-sd", 2)
        result = discover_operation({"timeout": 5, "service": "_airport"},
                                   AppOperationContext("discover", EventSink(lambda _event: None)))
        assert result.ok and result.payload["counts"]["devices"] == 1
        assert all(p.poll() is not None for p in children)
    finally:
        if proc.poll() is None:
            proc.kill(); proc.wait()
        for pid in child_pids:
            try: os.kill(pid, signal.SIGKILL)
            except ProcessLookupError: pass


def test_provider_to_swift_fixture_is_current_and_reviewed():
    from tests.fixtures.bonjour_payloads import FIXTURE_PATH, render
    assert FIXTURE_PATH.read_text() == render()


def test_family_browser_failure_is_diagnostic_and_keeps_healthy_scan(monkeypatch):
    closed = install_zeroconf(monkeypatch, records())
    original = zeroconf_backend.Collector.start
    def start(collector):
        if collector.zc.family == "ipv6":
            raise OSError("IPv6 browse unavailable")
        original(collector)
    monkeypatch.setattr(zeroconf_backend.Collector, "start", start)
    snapshot, diagnostics = bonjour.BonjourQuery().browse("_airport", timeout=.15)
    assert snapshot.resolved[0].ipv4 == ["192.0.2.10"]
    assert diagnostics.attempts[1].error == "OSError: IPv6 browse unavailable"
    assert sorted(closed) == ["ipv4", "ipv6"]


@pytest.mark.parametrize("family,address", [("ipv4", "192.0.2.10"), ("ipv6", "fd00::10")])
@pytest.mark.parametrize("startup_delay", [0, .2])
def test_public_discovery_preserves_answer_before_inconclusive_final_retries(monkeypatch, tmp_path, family, address, startup_delay):
    import time
    observation = {**records()[0], "ipv4": [], "ipv6": [], family: [address]}
    children = install_native(monkeypatch, tmp_path, [observation])
    launch = native_dns_sd._ProcessOwner.launch
    def delayed_launch(owner, args):
        if "-B" in args:
            time.sleep(startup_delay)
        return launch(owner, args)
    monkeypatch.setattr(native_dns_sd._ProcessOwner, "launch", delayed_launch)
    cap_browse_window(monkeypatch, "dns-sd")
    command = native_dns_sd._run_dns_sd_command
    address_calls = 0

    def answer_once(args, **kwargs):
        nonlocal address_calls
        if "-G" in args:
            address_calls += 1
            if address_calls > 1:
                # An unfinished retry has no callbacks, including no withdrawal.
                return "", "", -15, True, ""
        return command(args, **kwargs)

    monkeypatch.setattr(native_dns_sd, "_run_dns_sd_command", answer_once)
    result = discover_operation({"timeout": 5, "service": "_airport"},
                               AppOperationContext("discover", EventSink(lambda _event: None)))
    device = result.payload["devices"][0]
    assert address_calls > 1
    assert device["host"] == address
    assert device["addresses"] == [address]
    assert device["selected_record"][family] == [address]
    assert all(child.poll() is not None for child in children)


def test_native_delayed_answers_do_not_depend_on_the_parent_process_clock_origin(monkeypatch, tmp_path):
    import time
    monotonic = time.monotonic
    # Model macOS Python 3.9, where monotonic() has a different origin per process.
    monkeypatch.setattr(time, "monotonic", lambda: monotonic() + 10000)
    children = install_native(monkeypatch, tmp_path, records()[:1], address_delay=.1)
    snapshot, _diagnostics = bonjour.discover_snapshot_detailed("_airport", timeout=2)
    assert snapshot.resolved[0].ipv4 == ["192.0.2.10"]
    assert snapshot.resolved[0].ipv6 == ["fd00::10"]
    assert all(child.poll() is not None for child in children)


@pytest.mark.parametrize("scope,expected", [("en2", ["fe80::10%14"]), ("en3", ["fe80::10%14", "fe80::10%en3"])])
def test_snapshot_merge_compares_ipv6_interface_identity(monkeypatch, scope, expected):
    from timecapsulesmb.discovery.models import BonjourDiscoverySnapshot, BonjourResolvedService, _merge_snapshots
    monkeypatch.setattr("timecapsulesmb.core.net.socket.if_nametoindex", lambda name: {"en2": 14, "en3": 15}[name])
    records = [BonjourResolvedService("Office", "office.local", "_airport._tcp.local.",
                                     ipv6=[address], interface_index=14)
               for address in ("fe80::10%14", f"fe80::10%{scope}")]
    snapshot = _merge_snapshots([BonjourDiscoverySnapshot([], [record]) for record in records])
    assert len(snapshot.resolved) == 1
    assert snapshot.resolved[0].ipv6 == expected
