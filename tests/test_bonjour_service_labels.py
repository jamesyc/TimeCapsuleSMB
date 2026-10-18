"""Service labels are network identities, including their whitespace."""
from __future__ import annotations

import io
import json
import unicodedata
from contextlib import redirect_stdout
from dataclasses import replace
from types import SimpleNamespace
from unittest import mock

import pytest

from tests.fixtures.bonjour import (
    APPLE_STAMP, browse_row, cap_browse_window, install_native, install_zeroconf, native_fullname, records,
)
from timecapsulesmb.app.context import AppOperationContext
from timecapsulesmb.app.events import EventSink
from timecapsulesmb.app.ops.discovery import discover_operation
from timecapsulesmb.checks import doctor_steps
from timecapsulesmb.checks.bonjour import (
    BonjourExpectedIdentity, build_expected_smb_instance, own_bonjour_host, resolve_expected_smb_record,
    resolve_smb_service_target,
)
from timecapsulesmb.checks.doctor_steps import _add_expected_bonjour_host_label_result, _evaluate_bonjour_snapshot
from timecapsulesmb.checks.models import CheckResult
from timecapsulesmb.cli.discover import run_cli
from timecapsulesmb.core.config import AppConfig
from timecapsulesmb.discovery import bonjour, native_dns_sd as native, zeroconf_backend
from timecapsulesmb.discovery.devices import device_candidates_from_records
from timecapsulesmb.device.probe import apple_bonjour_host_label, derive_runtime_naming_identity
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


def service(name, *, address="192.0.2.10", hostname="host.local", stype="_smb._tcp.local.", index=14):
    return BonjourResolvedService(name, hostname, stype, 445, ipv4=[address],
                                  fullname=f"{name}.{stype}", interface_index=index)


def instance(record):
    return BonjourServiceInstance(record.service_type, record.name, record.fullname, record.interface_index)


@pytest.mark.parametrize("name", LABELS)
def test_browse_preserves_complete_label(name):
    events, errors = native._parse_dns_sd_browse_output("_smb._tcp", browse_row(name))
    assert errors == [] and len(events) == 1
    assert events[0].name == name
    assert (events[0].action, events[0].flags, events[0].interface_index) == ("Add", "2", 14)


# Captured from macOS dns-sd (2026-10-03) with only the labels changed: the
# stamp is "%2d:%02d:%02d.%03d  ", so before 10:00 it starts with a space.
# dns-sd -i N prints "Using interface N" before everything else (macOS 26.6.2).
@pytest.mark.parametrize("banner", ["", "Using interface 17\n"])
@pytest.mark.parametrize("stamp", [" 1:57:05.199", "18:56:53.249", " 0:00:00.000", "23:59:59.999"])
def test_captured_apple_browse_output_parses_at_every_hour(stamp, banner):
    output = (
        f"{banner}Browsing for _smb._tcp.local.\n"
        "DATE: ---Sun 04 Oct 2026---\n"
        f"{stamp}  ...STARTING...\n"
        "Timestamp     A/R    Flags  if Domain               Service Type         Instance Name\n"
        f"{stamp}  Add        3  17 local.               _smb._tcp.           Office Capsule\n"
        f"{stamp}  Add        2  17 local.               _smb._tcp.             Capsule  \n"
        f"{stamp}  Rmv        0  17 local.               _smb._tcp.           \u00a0Capsule\n"
    )
    events, errors = native._parse_dns_sd_browse_output("_smb._tcp", output)
    assert errors == []
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
    output = (f"Using interface 14\nBrowsing for _smb._tcp\nDATE: ---Sat 03 Oct 2026---\n"
              f"Timestamp A/R Flags if Domain Service Type Instance Name\n{APPLE_STAMP}...STARTING...\n\n")
    assert native._parse_dns_sd_browse_output("_smb._tcp", output) == ([], [])
    # Only callbacks carry a timestamp, so untimestamped text is preamble, not a row.
    assert native._parse_dns_sd_browse_output("_smb._tcp", "future preamble line\n") == ([], [])
    for malformed in ["10:20:00 Add", browse_row(""), f"{APPLE_STAMP}Error code -65563\n"]:
        assert native._parse_dns_sd_browse_output("_smb._tcp", malformed) == ([], [malformed.removesuffix("\n")])


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


def _address_row(hostname, address="192.0.2.10", tail="120"):
    return f"{APPLE_STAMP}Add  40000002  14  {native_fullname(hostname, 'local.')}  {address}  {tail}\n"


@pytest.mark.parametrize("name", LABELS)
def test_address_rows_read_the_address_whatever_the_hostname_holds(name):
    # dns-sd escapes ASCII spaces in the hostname and prints other whitespace raw.
    assert native._parse_dns_sd_address_output(_address_row(name) + _address_row(name, "fd00::10")) == [
        "192.0.2.10", "fd00::10"]


@pytest.mark.parametrize("name", LABELS)
def test_address_error_rows_are_errors_whatever_the_hostname_holds(name):
    stdout = _address_row(name, "<error>", "0  Error code -65570")
    assert native._command_error(stdout, "", 0, False, operation="-G") == "Bonjour query error -65570"


@pytest.mark.parametrize("name", LABELS)
def test_recheck_reads_each_cached_copy_whatever_the_name_holds(name):
    printed = native_fullname(name, "_adisk._tcp.local.")
    stdout = "".join(f"{APPLE_STAMP}Add  40000002  {index:>7}  {printed} TXT    IN     4 bytes: 03 61 3D 31\n"
                     for index in (14, -1))
    reconfirm = mock.Mock(return_value=0)
    with mock.patch.object(native, "_reconfirm_record", return_value=reconfirm), \
         mock.patch.object(native, "_run_dns_sd_command", return_value=(stdout, "", None, True, "")) as run:
        assert native.recheck_cached_txt(BonjourServiceInstance("_adisk._tcp.local.", name, "")) == (2, 2)
    # Each copy is reconfirmed under the name dns-sd was asked for.
    queried = run.call_args.args[0][3].encode()
    assert [c.args[1:3] for c in reconfirm.call_args_list] == [(14, queried), (0xFFFFFFFF, queried)]


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


# Apple's renames of these labels, from its own mDNSCore code in the firmware
# (Fixtures/apple_mdns_names.json): trailing ASCII spaces go before " (2)",
# other whitespace stays.
APPLE_WHITESPACE_RENAMES = {
    "AirPort Time\u00a0Capsule ": "AirPort Time\u00a0Capsule (2)",
    " Capsule ": " Capsule (2)",
    "\u00a0Capsule\u00a0": "\u00a0Capsule\u00a0 (2)",
    "   ": " (2)",
}


@pytest.mark.parametrize("label", WHITESPACE_LABELS)
def test_doctor_follows_apples_conflict_rename_of_a_whitespace_label(label):
    renamed = service(APPLE_WHITESPACE_RENAMES[label])
    resolver = mock.Mock(return_value=(renamed, None))
    result = resolve_expected_smb_record([instance(renamed)], [], expected_instance_name=label,
        expected_host_label="host", target_ip="192.0.2.10", resolver=resolver)
    assert [c.args[0].name for c in resolver.call_args_list] == [renamed.name]
    assert result.record == renamed and result.instance.name == renamed.name


@pytest.mark.parametrize("seen", ["Capsule  (2)", "Capsule(2)", "Capsule (1)"])
def test_doctor_does_not_follow_a_suffix_apple_would_not_give(seen):
    other = service(seen)
    missing = CheckResult("FAIL", "not found")
    resolver = mock.Mock(side_effect=lambda i, **kw: (other, None) if i.name == other.name else (None, missing))
    result = resolve_expected_smb_record([instance(other)], [], expected_instance_name="Capsule ",
        expected_host_label="host", target_ip="192.0.2.10", resolver=resolver)
    assert [c.args[0].name for c in resolver.call_args_list] == ["Capsule "]
    assert result.record is None and result.error == missing


def test_doctor_follows_apples_rename_only_to_this_devices_address():
    renamed = service("Capsule (2)", address="192.0.2.99", hostname="peer.local")
    missing = CheckResult("FAIL", "not found")
    resolver = mock.Mock(side_effect=lambda i, **kw: (renamed, None) if i.name == renamed.name else (None, missing))
    result = resolve_expected_smb_record([instance(renamed)], [], expected_instance_name="Capsule ",
        expected_host_label="host", target_ip="192.0.2.10", resolver=resolver)
    assert [c.args[0].name for c in resolver.call_args_list][0] == "Capsule (2)"
    assert result.record is None


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


# Slow: five browse children starting one after another outlast the 2 s
# window, and each -L outlasts its resolve attempt. Real dns-sd starts at once.
@pytest.mark.parametrize("startup", [0, 0.6], ids=["quick-start", "slow-start"])
def test_native_cli_json_preserves_all_three_service_labels(monkeypatch, tmp_path, startup):
    name = "AirPort Time\u00a0Capsule "
    observations = [{**r, "name": name} for r in records()[:3]]
    children = install_native(monkeypatch, tmp_path, observations, child_startup_delay=startup)
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


def evaluate(observed, *, host_label="home", instance_name="Home", resolved=None, target_ip="192.0.2.10"):
    """Doctor's Bonjour evaluation of `observed`; `resolved` maps host names to addresses."""
    lookup = (lambda host: tuple((resolved or {}).get(host.rstrip(".").lower(), ())))
    with mock.patch("timecapsulesmb.checks.doctor_steps.resolve_host_ips", side_effect=lookup), \
            mock.patch("timecapsulesmb.checks.bonjour.resolve_host_ips", side_effect=lookup):
        return _evaluate_bonjour_snapshot(BonjourDiscoverySnapshot([instance(r) for r in observed], observed),
            BonjourExpectedIdentity(instance_name=instance_name, host_label=host_label, target_ip=target_ip,
                                    advertise_afp=False),
            target_ip=target_ip, family="ipv4", interfaces=None, active_share_names=["Data"],
            resolver=mock.Mock(return_value=(None, CheckResult("FAIL", "no reply"))),
            browse_miss_message="browse missed", targeted_resolve_pass_message="resolved")


OUR_MAC = "02:00:00:00:00:10"
TWIN_MAC = "02:00:00:00:00:99"


def adisk_for(smb, mac=OUR_MAC):
    return replace(smb, service_type="_adisk._tcp.local.", services={"_adisk._tcp.local."}, port=9,
        fullname=f"{smb.name}._adisk._tcp.local.", properties={"sys": f"waMA={mac},adVF=0x1010",
            "dk2": "adVF=0x82,adVN=Data,adVU=117b94b1-3cf3-5600-b192-cc0dd671b852"})


def airport_for(smb, mac=OUR_MAC):
    return replace(smb, service_type="_airport._tcp.local.", services={"_airport._tcp.local."}, port=5009,
                   fullname=f"{smb.name}._airport._tcp.local.", properties={"waMA": mac.replace(":", "-")})


def device(smb, mac=OUR_MAC):
    """The records one AirPort advertises: _smb, _adisk and _airport on one host."""
    return [smb, adisk_for(smb, mac), airport_for(smb, mac)]


def messages(outcome, status):
    return [r.message for r in outcome.results if r.status == status]


def doctor_bonjour(observed, *, mac=OUR_MAC, target_ip="192.0.2.10", instance_name="Home", host_label="home",
                   resolved=None, naming=None):
    """Doctor's whole Bonjour step over `observed`, both address families.

    Returns its results and the host names it looked up; `resolved` maps host
    names to their addresses.
    """
    results, lookups = [], []

    def lookup(host):
        lookups.append(host.rstrip(".").lower())
        return tuple((resolved or {}).get(host.rstrip(".").lower(), ()))

    snapshot = BonjourDiscoverySnapshot([instance(r) for r in observed], observed)
    expected = BonjourExpectedIdentity(instance_name, host_label, target_ip, advertise_afp=False)
    with mock.patch.object(doctor_steps, "build_bonjour_expected_identity", return_value=expected), \
            mock.patch.object(doctor_steps, "discover_smb_services_detailed", return_value=(snapshot, None, None)), \
            mock.patch.object(doctor_steps, "resolve_host_ips", side_effect=lookup), \
            mock.patch("timecapsulesmb.checks.bonjour.resolve_host_ips", side_effect=lookup), \
            mock.patch.object(doctor_steps, "resolve_smb_instance",
                              return_value=(None, CheckResult("FAIL", "no reply"))):
        result = doctor_steps._add_bonjour_results(
            AppConfig(values={"TC_HOST": target_ip or "capsule.example"}), naming, skip_bonjour=False,
            active_share_names=["Data"], add_result=results.append, airport_mac=mac,
        )
    return SimpleNamespace(results=results, result=result, lookups=lookups)


def warnings(run):
    return [r for r in run.results if r.status == "WARN"]


def test_doctor_accepts_apples_renamed_host_at_this_devices_address():
    # Another host took home.local, so Apple advertises this device on home-2.local.
    ours = service("Home", hostname="Home-2.local")
    outcome = evaluate([ours, adisk_for(ours)], resolved={"home-2.local": ("192.0.2.10",)})
    assert not messages(outcome, "FAIL"), outcome.results
    assert any("'Home-2' is Apple's conflict rename of runtime mDNS host label 'home'" in m for m in messages(outcome, "PASS"))
    # The holder of home.local is the conflict: a Mac whose SMB share has another name.
    mac = service("Mac mini", hostname="Home.local", address="192.0.2.77")
    run = doctor_bonjour([*device(ours), mac], resolved={"home-2.local": ("192.0.2.10",)})
    assert not [r for r in run.results if r.status == "FAIL"], run.results
    [warning] = warnings(run)
    assert warning.details["code"] == "bonjour_name_conflict"
    assert warning.details["others"] == ["'Mac mini' on Home.local at 192.0.2.77"]


def test_doctor_rejects_a_renamed_host_that_is_not_at_this_devices_address():
    # "Home 2" is a real name whose host label is Home-2: a suffix alone proves nothing.
    peer = service("Home", hostname="Home-2.local", address="192.0.2.99")
    outcome = evaluate([peer, adisk_for(peer)], resolved={"home-2.local": ("192.0.2.99",)})
    assert any("belongs to another device" in m for m in messages(outcome, "FAIL")), outcome.results
    assert not any("conflict rename" in m for m in messages(outcome, "PASS"))
    # The label check alone refuses it too, on another host and at another address.
    target = resolve_smb_service_target(peer, expected_instance_name="Home")
    for kwargs in ({"own_host": "home.local"}, {"record_ips": ["192.0.2.99"], "target_ip": "192.0.2.10"}):
        results = []
        assert _add_expected_bonjour_host_label_result(target, "home", results.append, **kwargs)
        assert results[0].status == "FAIL" and "does not match runtime mDNS host label 'home'" in results[0].message


def test_a_rename_nobody_else_explains_is_a_stale_record_found_without_lookups():
    ours = service("Home (2)", hostname="Home-2.local")
    run = doctor_bonjour(device(ours), resolved={"home-2.local": ("192.0.2.10",)})
    assert not [r for r in run.results if r.status in ("FAIL", "WARN")], run.results
    assert any("Apple renamed this device's Bonjour name to 'Home (2)', 'Home-2'" in r.message
               and "no other device advertises the original name now" in r.message for r in run.results)
    # Nobody owns home.local: looking it up would take 10 s per try.
    assert "home.local" not in run.lookups


def test_doctor_warns_when_another_device_shares_the_name_this_device_kept():
    # This device kept the plain name; its twin advertises the renamed one.
    ours = service("Home", hostname="Home.local")
    twin = service("Home (2)", hostname="Home-2.local", address="192.0.2.99")
    run = doctor_bonjour([*device(ours), *device(twin, TWIN_MAC)], resolved={"home.local": ("192.0.2.10",)})
    assert not [r for r in run.results if r.status == "FAIL"], run.results
    [warning] = warnings(run)
    assert warning.details["others"] == ["'Home (2)' on Home-2.local at 192.0.2.99"]
    assert "AirPort Utility" in warning.message


@pytest.mark.parametrize("with_mac", [True, False])
def test_both_address_families_check_this_device_when_its_twin_kept_the_name(with_mac):
    # Apple renamed this device; the IPv6 pass has no address of its own to go
    # by, and the twin's records carry the plain name.
    ours = replace(service("Home (2)", hostname="Home-2.local"), ipv6=["fd00::10"])
    twin = replace(service("Home", hostname="Home.local", address="192.0.2.99"), ipv6=["fd00::99"])
    run = doctor_bonjour([*device(ours), *device(twin, TWIN_MAC)], mac=OUR_MAC if with_mac else None,
                         resolved={"home-2.local": ("192.0.2.10", "fd00::10"), "home.local": ("192.0.2.99", "fd00::99")})
    assert not [r for r in run.results if r.status == "FAIL"], run.results
    assert run.result.instance == "Home (2)" and run.result.target.hostname == "Home-2.local"
    assert set(run.result.addresses) == {"192.0.2.10", "fd00::10"}
    # One verdict for the device, naming only the other one.
    [warning] = warnings(run)
    assert warning.details["others"] == ["'Home' on Home.local at 192.0.2.99, fd00::99"]


@pytest.mark.parametrize("ours, other", [
    # The household of two numbered Time Capsules: neither name is a rename.
    (("AirPort Time Capsule 3", "AirPort-Time-Capsule-3"), ("AirPort Time Capsule 1", "AirPort-Time-Capsule-1")),
    # "Office 2" has the host label Office-2 of its own.
    (("Office", "Office"), ("Office 2", "Office-2")),
    (("Office", "Office"), ("Office (1)", "Office-1")),
    # mDNSCore folds ASCII case only: "Été" and "été" are two names.
    (("Été", "Ete"), ("été", "Other")),
])
def test_numbered_and_other_names_are_no_conflict(ours, other):
    name, label = ours
    mine = service(name, hostname=f"{label}.local")
    theirs = service(other[0], hostname=f"{other[1]}.local", address="192.0.2.99")
    run = doctor_bonjour([*device(mine), *device(theirs, TWIN_MAC)], instance_name=name, host_label=label,
                         resolved={f"{label.lower()}.local": ("192.0.2.10",)})
    assert not warnings(run), run.results
    assert not any("renamed" in r.message for r in run.results)


@pytest.mark.parametrize("other, others", [
    # Renamed by Apple because of this device: its name and host label both.
    (("Office (2)", "Office-2"), ["'Office (2)' on Office-2.local at 192.0.2.99"]),
    # Only its host label renamed: its own name gives the label Office, so Office-2 is Apple's rename.
    (("Office", "Office-2"), ["'Office' on Office-2.local at 192.0.2.99"]),
    # Names that differ only in ASCII case collide.
    (("OFFICE", "Mac-mini"), ["'OFFICE' on Mac-mini.local at 192.0.2.99"]),
])
def test_names_apple_would_collide_are_a_conflict(other, others):
    mine = service("Office", hostname="Office.local")
    theirs = service(other[0], hostname=f"{other[1]}.local", address="192.0.2.99")
    run = doctor_bonjour([*device(mine), *device(theirs, TWIN_MAC)], instance_name="Office", host_label="Office",
                         resolved={"office.local": ("192.0.2.10",)})
    [warning] = warnings(run)
    assert warning.details["others"] == others


@pytest.mark.parametrize("mac_name, mac_host, others, airport_others", [
    # macOS saves its renames, so a Mac that once lost "Office" keeps "Office (2)"
    # and Office-2 as its own names: they never swap back. On an AirPort the
    # same names are Apple's unsaved rename.
    ("Office (2)", "Office-2", [], ["'Office (2)' on Office-2.local at 192.0.2.70"]),
    # Office-2 is no rename of "Office Mac"'s own label on either.
    ("Office Mac", "Office-2", [], []),
    # The plain name itself is a live conflict, Mac or not.
    ("Office", "Mac-mini", ["'Office' on Mac-mini.local at 192.0.2.70"], ["'Office' on Mac-mini.local at 192.0.2.70"]),
    ("OFFICE", "Mac-mini", ["'OFFICE' on Mac-mini.local at 192.0.2.70"], ["'OFFICE' on Mac-mini.local at 192.0.2.70"]),
])
def test_a_mac_conflicts_only_on_the_name_itself(mac_name, mac_host, others, airport_others):
    ours = service("Office", hostname="Office.local")
    other = service(mac_name, hostname=f"{mac_host}.local", address="192.0.2.70")
    # A Mac advertises _smb but no _airport.
    for records, expected in (([other], others), (device(other, TWIN_MAC), airport_others)):
        run = doctor_bonjour([*device(ours), *records], instance_name="Office", host_label="Office",
                             resolved={"office.local": ("192.0.2.10",)})
        assert [w.details["others"] for w in warnings(run)] == ([expected] if expected else []), run.results


def test_a_name_in_a_service_type_this_device_does_not_register_is_no_conflict():
    ours = service("Home", hostname="Home.local")
    printer = service("Home", hostname="Printer.local", address="192.0.2.60", stype="_ipp._tcp.local.")
    run = doctor_bonjour([*device(ours), printer], resolved={"home.local": ("192.0.2.10",)})
    assert not warnings(run), run.results


def test_a_device_name_over_63_bytes_is_checked_by_this_devices_mac():
    # Apple advertises a name of its own for such a syNm; the _airport record has it too.
    ours = service("AirPort Time Capsule-58f15c", hostname="Long.local")
    run = doctor_bonjour(device(ours), instance_name=None, host_label="Long", target_ip=None,
                         resolved={"long.local": ("192.0.2.10",)})
    assert not [r for r in run.results if r.status == "FAIL"], run.results
    assert any("is this device's Bonjour name (its _airport service)" in r.message for r in run.results)
    assert run.result.instance == "AirPort Time Capsule-58f15c"
    # An _smb name the _airport record does not share is not Apple's default name.
    wrong = [replace(ours, name="Capsule"), adisk_for(ours), airport_for(ours)]
    run = doctor_bonjour(wrong, instance_name=None, host_label="Long", target_ip=None,
                         resolved={"long.local": ("192.0.2.10",)})
    assert any(r.status == "FAIL" and "is not this device's Bonjour name 'AirPort Time Capsule-58f15c'" in r.message
               for r in run.results), run.results


def test_without_a_mac_address_or_name_the_check_says_why_it_is_skipped():
    naming = derive_runtime_naming_identity("x" * 64, "capsule")
    run = doctor_bonjour([], mac=None, instance_name=None, host_label="capsule", target_ip=None, naming=naming)
    [skip] = run.results
    assert skip.status == "SKIP" and "over 63 bytes" in skip.message
    run = doctor_bonjour([], mac=None, instance_name=None, host_label=None, target_ip=None)
    assert "device naming probe unavailable" in run.results[0].message


def test_this_devices_host_is_found_by_its_mac_or_its_saved_address():
    ours = service("Home (2)", hostname="Home-2.local.")
    twin = service("Home", hostname="Home.local", address="192.0.2.99")
    records = [*device(ours), *device(twin, TWIN_MAC)]
    assert own_bonjour_host(records, airport_mac=OUR_MAC, target_ip=None) == "home-2.local"
    assert own_bonjour_host(records, airport_mac=TWIN_MAC, target_ip="192.0.2.10") == "home.local"
    assert own_bonjour_host(records, airport_mac=None, target_ip="192.0.2.10") == "home-2.local"
    assert own_bonjour_host(records, airport_mac="02:00:00:00:00:42", target_ip=None) is None
    assert own_bonjour_host(records, airport_mac=None, target_ip="192.0.2.42") is None


def test_a_mac_on_two_hosts_picks_the_one_with_this_devices_smb_record():
    # An _airport record cached from before a host rename still carries the MAC.
    stale = airport_for(service("Home", hostname="Home.local", address="192.0.2.10"))
    current = service("Home", hostname="Home-2.local", address="192.0.2.10")
    records = [stale, *device(current)]
    assert own_bonjour_host(records, airport_mac=OUR_MAC, target_ip="192.0.2.10") == "home-2.local"
    assert own_bonjour_host(records, airport_mac=OUR_MAC, target_ip=None) == "home-2.local"
    # Two hosts with _smb records: the one at the saved address.
    other = service("Home", hostname="Home-3.local", address="192.0.2.77")
    records = [*device(other), *device(current)]
    assert own_bonjour_host(records, airport_mac=OUR_MAC, target_ip="192.0.2.10") == "home-2.local"


@pytest.mark.parametrize("mine, theirs, others", [
    # ACPd turns each non-ASCII byte into "-", so these share the host label Caf
    # and Office: Apple renamed one host label, though the names differ.
    (("Café", "Caf"), ("Cafè", "Caf-2"), ["'Cafè' on Caf-2.local at 192.0.2.99"]),
    (("Office Ä", "Office-2"), ("Office Ö", "Office"), ["'Office Ö' on Office.local at 192.0.2.99"]),
    # Bonjour compares non-ASCII bytes exactly (RFC 6762 section 16): no conflict.
    (("Жук", "Base-Station-000010"), ("жук", "Base-Station-000099"), []),
    (("Café", "Caf"), (unicodedata.normalize("NFD", "Café"), "Mac-mini"), []),
])
def test_international_names_collide_only_where_apple_says(mine, theirs, others):
    name, label = mine
    ours = service(name, hostname=f"{label}.local")
    twin = service(theirs[0], hostname=f"{theirs[1]}.local", address="192.0.2.99")
    expected_label = apple_bonjour_host_label(name) or label
    run = doctor_bonjour([*device(ours), *device(twin, TWIN_MAC)], instance_name=name, host_label=expected_label,
                         resolved={f"{label.lower()}.local": ("192.0.2.10",)})
    assert [w.details["others"] for w in warnings(run)] == ([others] if others else []), run.results
    # This device's own label, renamed or not, is Apple's for its name.
    assert not [r for r in run.results if r.status == "FAIL"], run.results


def test_this_devices_own_records_are_no_conflict():
    ours = service("Home", hostname="Home.local")
    # A second interface's address of the same host, and a record with no address.
    other_link = replace(ours, ipv4=["192.0.2.10"], interface_index=15)
    device_info = BonjourResolvedService("Home", "", "_device-info._tcp.local.", 0, fullname="Home._device-info._tcp.local.")
    run = doctor_bonjour([*device(ours), other_link, device_info], resolved={"home.local": ("192.0.2.10",)})
    assert not warnings(run), run.results
    assert not any("renamed" in r.message for r in run.results)


def test_a_differently_named_device_is_no_conflict():
    ours = service("Home", hostname="Home.local")
    office = service("Office", hostname="Office.local", address="192.0.2.50")
    run = doctor_bonjour([*device(ours), office], resolved={"home.local": ("192.0.2.10",)})
    assert not warnings(run), run.results
