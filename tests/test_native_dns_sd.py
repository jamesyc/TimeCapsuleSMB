from __future__ import annotations

import sys
import threading
from unittest import mock

import pytest

from timecapsulesmb.discovery import native_dns_sd as native, zeroconf_backend
from timecapsulesmb.discovery.models import BonjourServiceInstance

LOOKUP = "10:20:07.456 Example._airport._tcp.local. can be reached at example.local.:5009 (interface 14)\n waMA=00:11:22:33:44:55,syAP=116,syVs=7.9.1\n"
ADDRESSES = "10:20:07.789 Add 2 14 example.local. 192.0.2.10 120\n10:20:07.790 Add 2 14 example.local. fd00::10 120\n"


@pytest.mark.parametrize("operation,stdout", [
    ("-B", "10:20:00 Add 2 14 local. _airport._tcp. Error -65570\n"),
    ("-B", "10:20:00 Add 2 14 local. _airport._tcp. Error -65554\n"),
    ("-B", "10:20:00 Add 2 14 local. _airport._tcp. 120 Error code -65570\n"),
    ("-L", "Lookup Error -65570._airport._tcp.local.\n"),
    ("-L", "10:20:00 Error -65570._airport._tcp.local. can be reached at host.local.:5009 (interface 14)\n"),
    ("-L", LOOKUP + " ErrorCode=-65570 label=ErrorCode=-65554\n"),
    ("-G", "10:20:00 Add 2 14 ErrorCode-65570.local. 192.0.2.10 120\n"),
])
def test_service_data_is_not_a_diagnostic(operation, stdout):
    assert native._command_error(stdout, "", 0, False, operation=operation) == ""


@pytest.mark.parametrize("operation,stdout,stderr,expected", [
    ("-B", "10:20:00 Error code -65570\n", "", -65570),
    ("-L", "10:20:00 Name._airport._tcp.local. error code -65570 Flags: 2\n", "", -65570),
    ("-L", "10:20:00 Error -65570._airport._tcp.local. error code -65537\n", "", -65537),
    ("-G", "10:20:00 Add 2 14 host.local. <error> 0 Error code -65570\n", "", -65570),
    ("-G", "10:20:00 Add 2 N 14 host.local. <error> 0 Error code -65570\n", "", -65570),
    ("-G", "10:20:00 Add 2 ? 14 host.local. <error> 0 Error code -65570\n", "", -65570),
    ("-B", "", "DNSServiceBrowse failed -65570\n", -65570),
    ("-L", "", "DNSServiceResolve failed -65537 (Service Not Running)\n", -65537),
    ("-G", "", "DNSServiceCreateConnection returned -65570\n", -65570),
    ("-B", "10:20:00 Add 2 14 local. _airport._tcp. Error -65554\n", "Error code -65570\n", -65570),
    ("-B", "10:20:00 Error code -65554\n10:20:01 Error code -65537\n", "", -65537),
    ("-B", "10:20:00 Error code -65537\n10:20:01 Error code -65570\n", "", -65570),
])
def test_actual_callback_and_startup_diagnostics_remain_errors(operation, stdout, stderr, expected):
    assert native._command_error(stdout, stderr, 0, False, operation=operation) == f"Bonjour query error {expected}"


def test_incomplete_live_diagnostic_waits_for_newline_but_final_eof_is_accepted():
    assert native._command_error("10:20:00 Error code -65570", "", None, False, operation="-B") == ""
    assert native._command_error("10:20:00 Error code -65570", "", 0, False, operation="-B") == "Bonjour query error -65570"


def test_no_such_record_and_unexpected_exit_behaviors_are_preserved():
    assert native._command_error("10:20:00 Error code -65554\n", "", 0, False, operation="-B") == ""
    assert native._command_error("10:20:00 Name._airport._tcp.local. No Such Record\n", "", 0, False, operation="-L") == ""
    assert native._command_error("", "ordinary failure", 1, False, operation="-B") == "ordinary failure"
    assert native._command_error("", "", 1, False, operation="-B") == "Bonjour command exited with status 1"
    assert native._command_error("", "", -15, True, operation="-B") == ""


@pytest.mark.parametrize("installed,expected", [(True, native), (False, zeroconf_backend)])
def test_neutral_selection_is_pinned_by_installation(installed, expected):
    from timecapsulesmb.discovery import bonjour
    with mock.patch.object(bonjour, "command_exists", return_value=installed) as exists:
        query = bonjour.BonjourQuery()
        with mock.patch.object(expected, "discover_snapshot_merged_detailed", side_effect=RuntimeError("query failed")) as browse:
            with pytest.raises(RuntimeError, match="query failed"):
                query.browse("_airport", timeout=.01)
        browse.assert_called_once()
        exists.assert_called_once_with("dns-sd")


def test_parser_preserves_each_ipv6_scope_and_withdrawals():
    output = "10:20:07 Add 2 17 host.local. fe80::40 120\n10:20:07 Add 2 18 host.local. fe80::40 120\n10:20:08 Rmv 0 17 host.local. fe80::40 0\n"
    with mock.patch("timecapsulesmb.core.net.socket.if_indextoname", side_effect=OSError):
        assert native._parse_dns_sd_address_output(output) == ["fe80::40%18"]
    assert native._parse_dns_sd_address_output("10:20:08 Rmv 0 14 host.local. 192.0.2.10 0\n") == []
    assert native._parse_dns_sd_address_output("10:20:08 Rmv 0 14 host.local. 192.0.2.10 0\n", ["192.0.2.10"]) == []


def test_native_resolution_normalizes_txt_and_uses_observed_interface():
    instance = BonjourServiceInstance("_airport._tcp.local.", "Example", "Example._airport._tcp.local.", 14)
    responses = [(LOOKUP, "", 0, False, ""), (ADDRESSES, "", -15, True, "")]
    with mock.patch.object(native, "_run_dns_sd_command", side_effect=responses) as command:
        record = native.resolve_service_instance_detailed(instance, 1000)[0]
    assert record is not None
    assert record.properties["syAP"] == "116"
    assert record.port == 5009 and record.interface_index == 14
    assert record.ipv4 == ["192.0.2.10"] and record.ipv6 == ["fd00::10"]
    assert command.call_args_list[0].args[0] == ["dns-sd", "-m", "-i", "14", "-L", "Example", "_airport._tcp", "local"]
    assert command.call_args_list[1].args[0] == ["dns-sd", "-i", "14", "-G", "v4v6", "example.local"]


@pytest.mark.parametrize("next_output,error,expected_ipv4,expected_ipv6", [
    ("10:20:08 Add 2 14 example.local. 192.0.2.20 120\n"
     "10:20:08 Add 2 14 example.local. fd00::20 120\n", "", ["192.0.2.20"], ["fd00::20"]),
    ("", "", ["192.0.2.10"], []),
    ("", "Bonjour query error -65537", ["192.0.2.10"], []),
    ("10:20:08 Rmv 0 14 example.local. 192.0.2.10 0\n", "", [], []),
    ("10:20:08 Add 2 14 example.local. 0.0.0.0 0 Error code -65554\n", "", [], []),
])
def test_native_retry_replaces_answers_and_preserves_unanswered_families(next_output, error, expected_ipv4, expected_ipv6):
    import time
    from timecapsulesmb.discovery.devices import device_candidates_from_records
    instance = BonjourServiceInstance("_airport._tcp.local.", "Example", "Example._airport._tcp.local.", 14)
    owner = native._ProcessOwner(threading.Event())
    first_output = "10:20:07 Add 2 14 example.local. 192.0.2.10 120\n"
    replies = [(LOOKUP, "", 0, False, ""), (first_output, "", -15, True, ""),
               (next_output, "", -15, True, error)]
    with mock.patch.object(native, "_run_dns_sd_command", side_effect=replies):
        first, _ = native._resolve(instance, None, time.monotonic() + 3, None, owner)
        current, detail = native._resolve(instance, first, time.monotonic() + 3, None, owner)
    assert first.ipv4 == ["192.0.2.10"]  # The previous observation is not mutated.
    assert current.ipv4 == expected_ipv4 and current.ipv6 == expected_ipv6
    assert detail.error == error
    if not next_output:
        assert detail.addresses[0].addresses == []  # Retained evidence is not a fresh callback.
    candidate = device_candidates_from_records([current])[0]
    assert candidate.host == (expected_ipv4[0] if expected_ipv4 else "example.local")


def test_native_retry_with_expired_budget_preserves_previous_observation():
    import time
    from timecapsulesmb.discovery.models import BonjourResolvedService
    instance = BonjourServiceInstance("_airport._tcp.local.", "Example", "Example._airport._tcp.local.", 14)
    previous = BonjourResolvedService("Example", "example.local", instance.service_type,
        ipv4=["192.0.2.10"], ipv6=["fd00::10"], interface_index=14)
    with mock.patch.object(native, "_run_dns_sd_command") as command:
        current, _ = native._resolve(instance, previous, time.monotonic() - 1, None,
                                    native._ProcessOwner(threading.Event()))
    command.assert_not_called()
    assert current.ipv4 == ["192.0.2.10"] and current.ipv6 == ["fd00::10"]


def test_adisk_txt_preserves_raw_packed_values_and_escaping():
    text = r"""10:20:07 Example._adisk._tcp.local. can be reached at example.local.:9 (interface 14)
 sys=waMA=00:11:22:33:44:55,adVF=0x1010 dk2=adVF=0x83,adVN=Time\ Machine,adVU=uuid
"""
    props = native._parse_dns_sd_txt_output(text)
    assert props["sys"] == "waMA=00:11:22:33:44:55,adVF=0x1010"
    assert props["dk2"] == "adVF=0x83,adVN=Time Machine,adVU=uuid"
    assert props["adVF"] == "0x1010"


def test_lookup_miss_does_not_start_address_query_and_keeps_no_answer():
    with mock.patch.object(native, "_run_dns_sd_command", return_value=("Lookup Example\n", "", -15, True, "")) as command:
        assert native.resolve_service_instance_detailed(BonjourServiceInstance("_smb._tcp.local.", "Example", "Example._smb._tcp.local."), 1000)[0] is None
    assert command.call_count == 1


def test_query_error_keeps_hostname_record_for_diagnostics():
    with mock.patch.object(native, "_run_dns_sd_command", side_effect=[(LOOKUP, "", 0, False, ""), ("", "error code -65570", 0, False, "Bonjour query error -65570")]):
        record = native.resolve_service_instance_detailed(BonjourServiceInstance("_airport._tcp.local.", "Example", "Example._airport._tcp.local."), 1000)[0]
    assert record is not None and record.hostname == "example.local"
    assert record.ipv4 == [] and record.ipv6 == []


def test_command_drains_fragmented_stdout_and_large_stderr_then_reaps():
    script = "import sys,time; sys.stdout.write('first'); sys.stdout.flush(); sys.stderr.write('x'*100000); sys.stderr.flush(); time.sleep(.03); print(' second', flush=True)"
    out, err, code, stopped, error = native._run_dns_sd_command([sys.executable, "-u", "-c", script], timeout_sec=2)
    assert out == "first second\n" and len(err) == 100000
    assert code == 0 and not stopped and not error


def test_normal_command_deadline_is_not_failure_and_ignoring_terminate_is_killed():
    import selectors
    owner = native._ProcessOwner(threading.Event())
    script = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready',flush=True); time.sleep(30)"
    launch = owner.launch
    def ready_launch(args):
        proc = launch(args)
        # Start the short command deadline after the fixture has installed SIGTERM-ignore.
        # Leave its output unread for the real streaming command reader.
        with selectors.DefaultSelector() as ready:
            ready.register(proc.stdout, selectors.EVENT_READ)
            assert ready.select(timeout=10), "fixture did not become ready"
        return proc
    try:
        with mock.patch.object(owner, "launch", side_effect=ready_launch):
            out, _err, code, stopped, error = native._run_dns_sd_command([sys.executable, "-u", "-c", script], timeout_sec=.15, owner=owner)
    finally:
        owner.stop()
    assert out == "ready\n" and stopped and code == -9 and error == ""
    assert not owner.children


def test_cancellation_reaps_running_child():
    owner = native._ProcessOwner(threading.Event())
    launched = []
    original = owner.launch
    def launch(args):
        proc = original(args)
        launched.append(proc)
        threading.Timer(.08, owner.cancel.set).start()
        return proc
    with mock.patch.object(owner, "launch", side_effect=launch):
        with pytest.raises(KeyboardInterrupt):
            native._run_dns_sd_command([sys.executable, "-u", "-c", "import time; time.sleep(30)"], timeout_sec=10, owner=owner)
    assert launched and all(p.poll() is not None for p in launched)
    assert not owner.children


def test_oversized_output_is_bounded_and_child_is_reaped():
    owner = native._ProcessOwner(threading.Event())
    with pytest.raises(RuntimeError, match="output exceeded"):
        native._run_dns_sd_command([sys.executable, "-u", "-c", "import sys,time; sys.stdout.write('x'*2000000); sys.stdout.flush(); time.sleep(30)"], timeout_sec=2, owner=owner)
    assert not owner.children


def test_zero_exit_with_policy_denial_is_a_typed_failure():
    from timecapsulesmb.discovery.models import BonjourPermissionDenied
    with pytest.raises(BonjourPermissionDenied):
        native._run_dns_sd_command([sys.executable, "-c", "print('Error code -65570')"], timeout_sec=2)


def test_dns_byte_escapes_decode_utf8_and_malformed_bytes_consistently():
    assert native._decode_dns_sd_text(r"Caf\195\169") == "Café"
    assert native._decode_dns_sd_text(r"Caf\xC3\xA9") == "Café"
    assert native._decode_dns_sd_text(r"bad\255value") == "badvalue"
    assert native._decode_dns_sd_text(r"Dot\.Slash\\123") == "Dot.Slash\\123"
    text = LOOKUP.splitlines()[0] + r"\n label=Caf\\xC3\\xA9".replace(r"\n", "\n")
    assert native._parse_dns_sd_txt_output(text)["label"] == "Café"


def test_txt_shell_metacharacters_literal_backslash_digits_and_empty_values_match_zeroconf():
    from tests.fixtures.bonjour import native_txt_output
    expected = {"label": "Café Bob's \"Capsule\" & \\123", "empty": "", "control": "\x01", "whitespace": " leading/trailing "}
    text = LOOKUP.splitlines()[0] + "\n" + native_txt_output(expected) + "\n"
    assert native._parse_dns_sd_txt_output(text) == expected
    events, errors = native._parse_dns_sd_browse_output("_airport._tcp",
        f"10:20:00 Add {2:8X} {14:3d} {'local.':<20} {'_airport._tcp.':<20} " + r"Bob's.Café\123")
    assert not errors and events[0].name == r"Bob's.Café\123"


def test_malformed_txt_line_does_not_discard_valid_service_target():
    fullname, host, port, index, properties = native._parse_dns_sd_lookup_output(
        "_airport._tcp", "Example", LOOKUP.splitlines()[0] + '\n label="unfinished\n',
    )
    assert host == "example.local" and port == 5009 and index == 14
    assert properties == {} and fullname == "Example._airport._tcp.local."


def test_partial_process_setup_failure_closes_pipes_and_reaps_child():
    import selectors
    owner = native._ProcessOwner(threading.Event())
    selector = selectors.DefaultSelector()
    with mock.patch.object(native.selectors, "DefaultSelector", return_value=selector):
        with mock.patch.object(selector, "register", side_effect=OSError("registration failed")):
            with pytest.raises(OSError, match="registration failed"):
                native._ProcessIO(owner, [sys.executable, "-c", "import time; time.sleep(30)"])
    assert not owner.children


def test_cancel_during_child_launch_reaps_child_before_io_setup():
    owner = native._ProcessOwner(threading.Event())
    popen = native.subprocess.Popen
    launched = []
    def launch(*args, **kwargs):
        proc = popen(*args, **kwargs)
        launched.append(proc)
        owner.cancel.set()
        return proc
    with mock.patch.object(native.subprocess, "Popen", side_effect=launch):
        with pytest.raises(KeyboardInterrupt):
            native._ProcessIO(owner, [sys.executable, "-c", "import time; time.sleep(30)"])
    assert launched and launched[0].poll() is not None
    assert launched[0].stdout.closed and launched[0].stderr.closed
    assert not owner.children


@pytest.mark.parametrize("family,other,address,prior", [
    (4, 6, "192.0.2.20", "fd00::10"),
    (6, 4, "fd00::20", "192.0.2.10"),
])
def test_address_callbacks_replace_only_the_answered_family(family, other, address, prior):
    initial = ["192.0.2.10", "fd00::10"]
    output = f"10:20:08 Add 2 14 example.local. {address} 120\n"
    result = native._parse_dns_sd_address_output(output, initial)
    assert address in result and prior in result and len(result) == 2
    assert set(native._parse_dns_sd_address_observations(output, initial)) == {family}
    empty = "0.0.0.0" if family == 4 else "::"
    negative = f"10:20:08 Add 2 N 14 example.local. {empty} 0 Error code -65554\n"
    assert native._parse_dns_sd_address_output(negative, initial) == [prior]
    # Apple's addrinfo_reply prints this spelling for kDNSServiceErr_NoSuchRecord.
    assert native._parse_dns_sd_address_output(negative.replace("Error code -65554", "No Such Record"), initial) == [prior]
    unknown = "10:20:08 Add 2 14 example.local. <error> 0 Error code -65554\n"
    assert native._parse_dns_sd_address_output(unknown, initial) == initial


def test_withdrawal_removes_only_its_address_and_error_is_not_absence():
    initial = ["192.0.2.10", "192.0.2.11", "fd00::10"]
    removed = "10:20:08 Rmv 0 14 example.local. 192.0.2.10 0\n"
    assert native._parse_dns_sd_address_output(removed, initial) == ["192.0.2.11", "fd00::10"]
    error = "10:20:08 Add 2 14 example.local. 0.0.0.0 0 Error code -65537\n"
    assert native._parse_dns_sd_address_output(error, initial) == initial
