"""Service labels are network identities, including their whitespace."""
from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from dataclasses import replace
from unittest import mock

import pytest

from tests.fixtures.bonjour import (
    APPLE_STAMP, cap_browse_window, install_native, install_zeroconf, native_fullname, records,
)
from timecapsulesmb.app.context import AppOperationContext
from timecapsulesmb.app.events import EventSink
from timecapsulesmb.app.ops.discovery import discover_operation
from timecapsulesmb.checks.bonjour import (
    BonjourExpectedIdentity, build_expected_smb_instance, resolve_expected_smb_record,
)
from timecapsulesmb.checks.doctor_steps import _evaluate_bonjour_snapshot
from timecapsulesmb.checks.models import CheckResult
from timecapsulesmb.cli.discover import run_cli
from timecapsulesmb.discovery import bonjour, native_dns_sd as native, zeroconf_backend
from timecapsulesmb.discovery.devices import device_candidates_from_records
from timecapsulesmb.device.probe import derive_runtime_naming_identity
from timecapsulesmb.discovery.models import (
    BonjourDiscoverySnapshot, BonjourResolvedService, BonjourServiceInstance, _merge_snapshots,
)
from timecapsulesmb.services.configure_target import bonjour_record_from_selected_record


LABELS = [
    "AirPort Time\u00a0Capsule ", "Capsule ", "Capsule   ", " Capsule", "   Capsule",
    " Capsule ", "\u00a0Capsule\u00a0", "\tCapsule\t", "Capsule  Office\tDesk", "   ",
    "Capsule\u0085Office", "Capsule\u2028Office", "Capsule\u2029Office", "Café 胶囊 🛜",
    "Café", "Cafe\u0301", "x" * 63, "é" * 31 + "a", "Capsule.", r"Bob's.Café\123",
    "...STARTING...", "Timestamp", "DATE:", "Browsing for services", "Error -65570",
    "120 Error code -65570", "Printer can be reached at Office", "-L", "$(echo x); & 'quoted'",
]


def browse_row(name, *, action="Add", domain="local.", service="_smb._tcp.", index=14, flags=2):
    # The C formatter's minimum widths apply to UTF-8 bytes, not Python code points.
    return (f"{APPLE_STAMP}{action} {flags:8X} {index:3d} ".encode()
            + domain.encode().ljust(20) + b" " + service.encode().ljust(20) + b" "
            + name.encode() + b"\n").decode()


def service(name, *, address="192.0.2.10", hostname="host.local", stype="_smb._tcp.local.", index=14):
    return BonjourResolvedService(name, hostname, stype, 445, ipv4=[address],
                                  fullname=f"{name}.{stype}", interface_index=index)


def instance(record):
    return BonjourServiceInstance(record.service_type, record.name, record.fullname, record.interface_index)


@pytest.mark.parametrize("name", LABELS)
def test_browse_preserves_complete_label(name):
    events, errors = native._parse_dns_sd_browse_output("_smb._tcp", browse_row(name))
    assert errors == 0 and len(events) == 1
    assert events[0].name == name
    assert (events[0].action, events[0].flags, events[0].interface_index) == ("Add", "2", 14)


# Captured from macOS dns-sd (2026-10-03) with only the labels changed: the
# stamp is "%2d:%02d:%02d.%03d  ", so before 10:00 it starts with a space.
@pytest.mark.parametrize("stamp", [" 1:57:05.199", "18:56:53.249", " 0:00:00.000", "23:59:59.999"])
def test_captured_apple_browse_output_parses_at_every_hour(stamp):
    output = (
        "Browsing for _smb._tcp.local.\n"
        "DATE: ---Sun 04 Oct 2026---\n"
        f"{stamp}  ...STARTING...\n"
        "Timestamp     A/R    Flags  if Domain               Service Type         Instance Name\n"
        f"{stamp}  Add        3  17 local.               _smb._tcp.           Office Capsule\n"
        f"{stamp}  Add        2  17 local.               _smb._tcp.             Capsule  \n"
        f"{stamp}  Rmv        0  17 local.               _smb._tcp.           \u00a0Capsule\n"
    )
    events, errors = native._parse_dns_sd_browse_output("_smb._tcp", output)
    assert errors == 0
    assert [(e.action, e.flags, e.interface_index, e.name) for e in events] == [
        ("Add", "3", 17, "Office Capsule"), ("Add", "2", 17, "  Capsule  "), ("Rmv", "0", 17, "\u00a0Capsule"),
    ]


@pytest.mark.parametrize("stamp", [" 1:57:07.261", "18:57:07.261"])
@pytest.mark.parametrize("escaped,fullname", [
    (r"AirPort\032Time\032Capsule._smb._tcp.local.", "AirPort Time Capsule._smb._tcp.local."),
    (r"\032\032Capsule\032\032._smb._tcp.local.", "  Capsule  ._smb._tcp.local."),
    ("\u00a0Capsule._smb._tcp.local.", "\u00a0Capsule._smb._tcp.local."),
])
def test_captured_apple_lookup_fullname_excludes_timestamp_framing(stamp, escaped, fullname):
    lookup = (f"Lookup Capsule._smb._tcp.local.\nDATE: ---Sun 04 Oct 2026---\n{stamp}  ...STARTING...\n"
              f"{stamp}  {escaped} can be reached at AirPort-Time-Capsule.local.:445 (interface 17)\n"
              " txtvers=1\n")
    assert native._parse_dns_sd_lookup_output("_smb._tcp", "Capsule", lookup)[:4] == (
        fullname, "AirPort-Time-Capsule.local", 445, 17)
    assert native._parse_dns_sd_txt_output(lookup) == {"txtvers": "1"}


@pytest.mark.parametrize("stamp", [" 1:57:05.199", "18:56:53.249"])
@pytest.mark.parametrize("operation,line", [
    ("-B", "{stamp}  Error code -65570"),
    ("-L", "{stamp}  Capsule._smb._tcp.local. error code -65570"),
    ("-G", "{stamp}  Add        2  17 host.local.                            0.0.0.0                                      0  Error code -65570"),
])
def test_permission_denial_is_detected_at_every_hour(stamp, operation, line):
    output = line.format(stamp=stamp) + "\n"
    assert native._command_error(output, "", 0, False, operation=operation) == "Bonjour query error -65570"


@pytest.mark.parametrize("domain,stype", [
    ("local.", "_smb._tcp."), ("a" * 19 + ".", "_pdl-datastream._tcp."),
    ("a" * 25 + ".", "_service_with_long_name._tcp."), ("é.example.", "_smb._tcp."),
])
@pytest.mark.parametrize("action,index,flags", [("Add", 14, 0xA5), ("Rmv", 123456, 0), ("Add", -1, 0xFFFFFFFF)])
def test_browse_column_widths_and_metadata(domain, stype, action, index, flags):
    events, errors = native._parse_dns_sd_browse_output("_smb._tcp", browse_row(" Label ", action=action, domain=domain, service=stype, index=index, flags=flags))
    assert not errors and len(events) == 1
    assert (events[0].name, events[0].domain, events[0].service_type, events[0].action, events[0].interface_index) == (
        " Label ", domain, stype.rstrip("."), action, index,
    )
    assert events[0].flags == f"{flags:X}"


@pytest.mark.parametrize("ending", ["\n", "\r\n", ""])
def test_browse_line_endings_do_not_trim_the_label(ending):
    events, errors = native._parse_dns_sd_browse_output("_smb._tcp", browse_row(" Label  ").removesuffix("\n") + ending)
    assert not errors and events[0].name == " Label  "


def test_browse_headers_and_malformed_rows_are_not_instances():
    output = f"Browsing for _smb._tcp\nDATE: ---Sat 03 Oct 2026---\nTimestamp A/R Flags if Domain Service Type Instance Name\n{APPLE_STAMP}...STARTING...\n\n"
    assert native._parse_dns_sd_browse_output("_smb._tcp", output) == ([], 0)
    for malformed in ["10:20:00 Add", browse_row("")]:
        assert native._parse_dns_sd_browse_output("_smb._tcp", malformed) == ([], 1)


@pytest.mark.parametrize("separator", ["\u0085", "\u2028", "\u2029"])
@pytest.mark.parametrize("exit_code", [None, 0])
def test_unicode_label_content_cannot_become_a_permission_diagnostic(separator, exit_code):
    output = browse_row(f"Capsule{separator}10:20:00 Error code -65570")
    assert native._command_error(output, "", exit_code, False, operation="-B") == ""
    assert native._command_error(output + "10:20:01 Error code -65570\n", "", exit_code, False, operation="-B") == "Bonjour query error -65570"


@pytest.mark.parametrize("name", LABELS)
def test_lookup_decodes_full_label_without_unicode_line_splitting(name):
    lookup = f"{APPLE_STAMP}{native_fullname(name, '_smb._tcp.local.')} can be reached at host.local.:445 (interface 14)\n label=unchanged\n"
    fullname, host, port, index, props = native._parse_dns_sd_lookup_output("_smb._tcp", name, lookup)
    assert (fullname, host, port, index) == (f"{name}._smb._tcp.local.", "host.local", 445, 14)
    assert props == {"label": "unchanged"}


@pytest.mark.parametrize("name", [" AirPort Time\u00a0Capsule ", "\tCapsule\t", "-L", "$(echo x); &"])
def test_parser_to_resolver_uses_exact_label_and_one_argument(name):
    event = native._parse_dns_sd_browse_output("_smb._tcp", browse_row(name))[0][0]
    observed = instance(service(event.name))
    def reply(args, **kwargs):
        if "-L" in args:
            queried = args[args.index("-L") + 1]
            if queried != name:
                return "Lookup pending\n", "", -15, True, ""
            return f"{APPLE_STAMP}{native_fullname(name, observed.service_type)} can be reached at host.local.:445 (interface 14)\n", "", 0, False, ""
        return f"{APPLE_STAMP}Add 2 14 host.local. 192.0.2.10 120\n", "", -15, True, ""
    with mock.patch.object(native, "_run_dns_sd_command", side_effect=reply) as command:
        record, _ = native.resolve_service_instance_detailed(observed, 1000)
    assert record is not None and record.name == name and record.ipv4 == ["192.0.2.10"]
    assert command.call_args_list[0].args[0] == ["dns-sd", "-m", "-i", "14", "-L", name, "_smb._tcp", "local"]


@pytest.mark.parametrize("name", ["Capsule ", " Capsule", "\u00a0Capsule\u00a0"])
def test_label_lookup_failure_never_starts_address_lookup(name):
    with mock.patch.object(native, "_run_dns_sd_command", return_value=("Lookup pending\n", "", -15, True, "")) as command:
        record, _ = native.resolve_service_instance_detailed(instance(service(name)), 1000)
    assert record is None and command.call_count == 1


@pytest.mark.parametrize("fullname", [True, False])
def test_candidate_ids_keep_whitespace_variants_without_hardware_mac(fullname):
    observations = [service(name, stype="_airport._tcp.local.") for name in ["Capsule", " Capsule", "Capsule "]]
    if not fullname:
        observations = [replace(r, fullname="") for r in observations]
    candidates = device_candidates_from_records(observations)
    assert len(candidates) == len({c.id for c in candidates}) == 3
    assert {c.name for c in candidates} == {r.name for r in observations}


def test_hardware_identity_still_groups_observations_of_one_device():
    observations = [replace(service(name, stype="_airport._tcp.local."), properties={"waMA": "00:11:22:33:44:55"})
                    for name in ["Capsule", "Capsule "]]
    assert len(device_candidates_from_records(observations)) == 1


def test_snapshot_merge_combines_families_only_for_the_exact_label():
    first = service("Capsule ")
    second = replace(first, ipv4=[], ipv6=["fd00::10"])
    different = service("Capsule")
    composed = service("Café")
    decomposed = service("Cafe\u0301")
    result = _merge_snapshots([BonjourDiscoverySnapshot([], [first, different, composed]), BonjourDiscoverySnapshot([], [second, decomposed])])
    assert len(result.resolved) == 4
    ours = next(r for r in result.resolved if r.name == first.name)
    assert ours.ipv4 == ["192.0.2.10"] and ours.ipv6 == ["fd00::10"]


def test_zeroconf_update_and_removal_do_not_replace_a_whitespace_peer():
    from zeroconf import ServiceStateChange
    collector = zeroconf_backend.Collector(None, ["_smb._tcp.local."])
    first, second = service("Capsule"), service("Capsule ")
    for r in [first, second]:
        collector._on_service_state_change(zeroconf=None, service_type=r.service_type, name=r.fullname, state_change=ServiceStateChange.Added)
        collector.add_record(r)
    assert len(collector.results()) == 2
    collector.add_record(replace(second, ipv4=["192.0.2.20"], properties={"label": "updated"}))
    assert next(r for r in collector.results() if r.name == first.name).ipv4 == first.ipv4
    collector._on_service_state_change(zeroconf=None, service_type=second.service_type, name=second.fullname, state_change=ServiceStateChange.Removed)
    assert collector.results() == [first]
    assert [i.name for i in collector.service_instances()] == [first.name]


@pytest.mark.parametrize("name", LABELS[:10])
def test_targeted_instance_preserves_supplied_label(name):
    result = build_expected_smb_instance(name)
    assert (result.name, result.fullname) == (name, f"{name}._smb._tcp.local.")


# Doctor expects syNm exactly as Apple advertises it, so only that exact label
# (or Apple's conflict rename of it) is this device; a trimmed spelling is not.
WHITESPACE_LABELS = ["AirPort Time\u00a0Capsule ", " Capsule ", "\u00a0Capsule\u00a0", "   "]


@pytest.mark.parametrize("label", WHITESPACE_LABELS)
@pytest.mark.parametrize("pending", [False, True])
def test_doctor_selects_the_exact_label_by_name(label, pending):
    ours = service(label)
    resolver = mock.Mock(return_value=(ours, None))
    result = resolve_expected_smb_record([instance(ours)], [] if pending else [ours],
        expected_instance_name=label, resolver=resolver)
    assert result.record == ours and result.instance.name == label and result.error is None
    if pending:
        assert [c.args[0].name for c in resolver.call_args_list] == [label]
    else:
        resolver.assert_not_called()


@pytest.mark.parametrize("label", WHITESPACE_LABELS[:3])
def test_doctor_queries_the_exact_label_instead_of_a_trimmed_peer(label):
    peer = service(label.strip(), address="192.0.2.99", hostname="peer.local")
    ours = service(label)
    resolver = mock.Mock(side_effect=lambda i, **kw: (ours, None) if i.name == label else (peer, None))
    result = resolve_expected_smb_record([instance(peer)], [peer], expected_instance_name=label,
        expected_host_label="host", target_ip="192.0.2.10", resolver=resolver)
    assert [c.args[0].name for c in resolver.call_args_list] == [label]
    assert result.source == "targeted_resolve" and result.record == ours and result.error is None


def test_doctor_reports_a_missing_label_although_its_trimmed_spelling_is_advertised():
    peer = service("Capsule", address="192.0.2.99", hostname="peer.local")
    missing = CheckResult("FAIL", "not found")
    resolver = mock.Mock(return_value=(None, missing))
    result = resolve_expected_smb_record([instance(peer)], [peer], expected_instance_name="Capsule ", resolver=resolver)
    assert [c.args[0].name for c in resolver.call_args_list] == ["Capsule "]
    assert result.record is None and result.error == missing and result.source == "targeted_resolve"


@pytest.mark.parametrize("label", WHITESPACE_LABELS)
def test_doctor_follows_apples_conflict_rename_of_a_whitespace_label(label):
    # mDNSCore appends " (2)" without trimming the label first.
    renamed = service(f"{label} (2)")
    resolver = mock.Mock(return_value=(renamed, None))
    result = resolve_expected_smb_record([instance(renamed)], [], expected_instance_name=label,
        expected_host_label="host", target_ip="192.0.2.10", resolver=resolver)
    assert [c.args[0].name for c in resolver.call_args_list] == [f"{label} (2)"]
    assert result.record == renamed and result.instance.name == f"{label} (2)"


def test_doctor_does_not_follow_the_rename_of_a_trimmed_spelling():
    other = service("Capsule (2)")
    missing = CheckResult("FAIL", "not found")
    resolver = mock.Mock(side_effect=lambda i, **kw: (other, None) if i.name == other.name else (None, missing))
    result = resolve_expected_smb_record([instance(other)], [], expected_instance_name="Capsule ",
        expected_host_label="host", target_ip="192.0.2.10", resolver=resolver)
    assert [c.args[0].name for c in resolver.call_args_list] == ["Capsule "]
    assert result.record is None and result.error == missing


@pytest.mark.parametrize("radio_mac, host_failures", [("E8:8D:28:61:9B:7D", 0), (None, 1)])
def test_doctor_blank_name_expects_apples_bonjour_host(radio_mac, host_failures):
    # NetBSD 4 LE with syNm "   " advertised Base-Station-619b7d (raMA) while
    # /bin/hostname read base-station-edffbf; only raMA predicts the SRV host.
    identity = derive_runtime_naming_identity("   ", "base-station-edffbf", radio_mac=radio_mac)
    smb = service("   ", hostname="Base-Station-619b7d.local")
    with mock.patch("timecapsulesmb.core.net.socket.getaddrinfo", return_value=[]):
        outcome = _evaluate_bonjour_snapshot(BonjourDiscoverySnapshot([instance(smb)], [smb]),
            BonjourExpectedIdentity(instance_name=identity.mdns_instance_name, host_label=identity.mdns_host_label,
                                    target_ip="192.0.2.10", advertise_afp=False),
            target_ip="192.0.2.10", family="ipv4", interfaces=None, active_share_names=["Data"],
            resolver=mock.Mock(return_value=(None, CheckResult("FAIL", "no reply"))),
            browse_miss_message="browse missed", targeted_resolve_pass_message="resolved")
    assert outcome.instance == "   "
    failures = [r.message for r in outcome.results if r.status == "FAIL" and "host label" in r.message]
    assert len(failures) == host_failures, outcome.results


@pytest.mark.parametrize("system_dns_name, host_failures", [("Dn Test.Name\u2019s", 0), (None, 1)])
def test_doctor_expects_the_bonjour_host_acpd_takes_from_syDN(system_dns_name, host_failures):
    # NetBSD 4 LE with syDN "Dn Test.Name’s" and syNm "AirPort Time Capsule"
    # advertised its services on Dn-Test-Names.local after a reboot. Without
    # syDN the expectation comes from syNm, so the same record must still fail.
    identity = derive_runtime_naming_identity("AirPort Time Capsule", "dn-test-names", system_dns_name=system_dns_name,
                                              radio_mac="E8:8D:28:61:9B:7D")
    smb = service("AirPort Time Capsule", hostname="Dn-Test-Names.local")
    with mock.patch("timecapsulesmb.core.net.socket.getaddrinfo", return_value=[]):
        outcome = _evaluate_bonjour_snapshot(BonjourDiscoverySnapshot([instance(smb)], [smb]),
            BonjourExpectedIdentity(instance_name=identity.mdns_instance_name, host_label=identity.mdns_host_label,
                                    target_ip="192.0.2.10", advertise_afp=False),
            target_ip="192.0.2.10", family="ipv4", interfaces=None, active_share_names=["Data"],
            resolver=mock.Mock(return_value=(None, CheckResult("FAIL", "no reply"))),
            browse_miss_message="browse missed", targeted_resolve_pass_message="resolved")
    assert outcome.instance == "AirPort Time Capsule"
    failures = [r.message for r in outcome.results if r.status == "FAIL" and "host label" in r.message]
    assert len(failures) == host_failures, outcome.results
    passes = [r.message for r in outcome.results if r.status == "PASS" and "matches runtime mDNS host label" in r.message]
    assert len(passes) == 1 - host_failures, outcome.results


@pytest.mark.parametrize("label", ["AirPort Time\u00a0Capsule ", "   "])
@pytest.mark.parametrize("family", ["ipv4", "ipv6"])
@pytest.mark.parametrize("fault", [None, "address", "hostname", "port", "missing_adisk", "missing_adisk_without_related", "wrong_adisk"])
def test_doctor_whitespace_label_keeps_endpoint_and_adisk_validation(label, family, fault):
    address = "192.0.2.10" if family == "ipv4" else "fd00::10"
    smb = service(label)
    if family == "ipv6":
        smb = replace(smb, ipv4=[], ipv6=[address])
    adisk = replace(smb, service_type="_adisk._tcp.local.", services={"_adisk._tcp.local."}, port=9,
        fullname=f"{label}._adisk._tcp.local.", properties={"sys": "waMA=00:11:22:33:44:55,adVF=0x1010",
            "dk2": "adVF=0x82,adVN=Data,adVU=117b94b1-3cf3-5600-b192-cc0dd671b852"})
    if fault == "address":
        smb = replace(smb, ipv4=["192.0.2.99"] if family == "ipv4" else [], ipv6=["fd00::99"] if family == "ipv6" else [])
    elif fault == "hostname":
        smb = replace(smb, hostname="peer.local")
    elif fault == "port":
        smb = replace(smb, port=1234)
    elif fault == "wrong_adisk":
        adisk = replace(adisk, properties={**adisk.properties, "dk2": "adVF=0x82,adVN=Other,adVU=117b94b1-3cf3-5600-b192-cc0dd671b852"})
    airport = replace(smb, service_type="_airport._tcp.local.", services={"_airport._tcp.local."}, port=5009,
                      fullname=f"{label}._airport._tcp.local.", properties={"syAP": "116"})
    observed = [smb] if fault in {"missing_adisk", "missing_adisk_without_related"} else [smb, adisk]
    if fault != "missing_adisk_without_related":
        observed.append(airport)
    with mock.patch("timecapsulesmb.core.net.socket.getaddrinfo", return_value=[]):
        outcome = _evaluate_bonjour_snapshot(BonjourDiscoverySnapshot([instance(r) for r in observed], observed),
            BonjourExpectedIdentity(instance_name=label, host_label="host", target_ip=address, advertise_afp=False),
            target_ip=address, family=family, interfaces=None, active_share_names=["Data"],
            resolver=mock.Mock(return_value=(None, CheckResult("FAIL", "no reply"))),
            browse_miss_message="browse missed", targeted_resolve_pass_message="resolved")
    failures = [r.message for r in outcome.results if r.status == "FAIL"]
    if fault in {None, "missing_adisk_without_related"}:
        assert not failures, failures
    else:
        assert failures, outcome.results


@pytest.mark.parametrize("provider", ["dns-sd", "zeroconf"])
@pytest.mark.parametrize("name", ["AirPort Time\u00a0Capsule ", " Capsule ", "\tCapsule\t", "Capsule\u2028Office", "...STARTING...", "Capsule\u202810:20:00 Error code -65570"])
def test_provider_api_selection_roundtrip_preserves_label(monkeypatch, tmp_path, provider, name):
    observations = [{**r, "name": name} for r in records()]
    resources = install_native(monkeypatch, tmp_path, observations) if provider == "dns-sd" else install_zeroconf(monkeypatch, observations)
    cap_browse_window(monkeypatch, provider)
    result = discover_operation({"timeout": 5, "service": "_airport"}, AppOperationContext("discover", EventSink(lambda _: None)))
    if provider == "zeroconf" and "\t" in name:
        # The real zeroconf ServiceInfo rejects ASCII controls. Keep its query
        # outcome and transport cleanup, rather than inventing a native fallback.
        assert result.ok and result.payload["counts"] == {"instances": 1, "resolved": 0, "devices": 0}
        assert sorted(resources) == ["ipv4", "ipv6"]
        return
    assert result.ok and result.payload["counts"]["devices"] == 1
    device = result.payload["devices"][0]
    selected = bonjour_record_from_selected_record(device["selected_record"])
    assert device["name"] == selected.name == name
    assert device["fullname"] == selected.fullname == f"{name}._airport._tcp.local."
    assert selected.ipv4 == ["192.0.2.10"] and selected.ipv6 == ["fd00::10"]
    if provider == "dns-sd":
        assert all(p.poll() is not None for p in resources)
    else:
        assert sorted(resources) == ["ipv4", "ipv6"]


def test_native_cli_json_preserves_all_three_service_labels(monkeypatch, tmp_path):
    name = "AirPort Time\u00a0Capsule "
    observations = [{**r, "name": name} for r in records()[:3]]
    children = install_native(monkeypatch, tmp_path, observations)
    cap_browse_window(monkeypatch, "dns-sd")
    output = io.StringIO()
    with redirect_stdout(output):
        assert run_cli(["--timeout", "5", "--json"]) == 0
    data = json.loads(output.getvalue())
    assert len(data["resolved"]) == 3
    assert all(r["name"] == name for r in data["instances"] + data["resolved"])
    assert all(p.poll() is not None for p in children)


@pytest.mark.parametrize("provider", ["dns-sd", "zeroconf"])
def test_provider_keeps_trimmed_peer_and_original_label_at_distinct_endpoints(monkeypatch, tmp_path, provider):
    original = {**records()[0], "name": "Capsule "}
    peer = {**original, "name": "Capsule", "hostname": "peer.local", "ipv4": ["192.0.2.99"], "ipv6": ["fd00::99"],
            "properties": {"syAP": "116", "waMA": "00:11:22:33:44:66"}}
    resources = install_native(monkeypatch, tmp_path, [peer, original]) if provider == "dns-sd" else install_zeroconf(monkeypatch, [peer, original])
    cap_browse_window(monkeypatch, provider)
    result = discover_operation({"timeout": 5, "service": "_airport"}, AppOperationContext("discover", EventSink(lambda _: None)))
    assert {d["name"]: d["host"] for d in result.payload["devices"]} == {"Capsule": "192.0.2.99", "Capsule ": "192.0.2.10"}
    if provider == "dns-sd":
        assert all(p.poll() is not None for p in resources)
    else:
        assert sorted(resources) == ["ipv4", "ipv6"]


@pytest.mark.parametrize("name", ["\u00a0Capsule  ", "é Office\t", "Capsule\u2028Office "])
def test_native_fragmented_utf8_label_and_separate_newline_reach_resolution(monkeypatch, tmp_path, name):
    children = install_native(monkeypatch, tmp_path, [{**records()[0], "name": name}], fragment_browse=True)
    snapshot, diagnostics = bonjour.discover_snapshot_detailed("_airport", timeout=2)
    assert [r.name for r in snapshot.instances] == [name]
    assert [r.name for r in snapshot.resolved] == [name]
    assert snapshot.resolved[0].ipv4 == ["192.0.2.10"]
    assert diagnostics.errors == {}
    assert all(p.poll() is not None for p in children)
