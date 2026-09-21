"""Registrant state machine against the fake mDNSResponder (guide C.11).

Assertions come from the fake daemon's IPC transcript, never from logs."""
from __future__ import annotations

import os
import re
import signal
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from tests.native.build import compile_service
from tests.native.cases import compile_case, native_case_source, run_case
from tests.native.integration.fake_dnssd_daemon import FakeDnssdDaemon
from tests.native.test_plan import MODE, NAT_ADDRS, NAT_LINKS, facts_text

UUID = "12345678-1234-1234-1234-123456789012"
NAT_OK = facts_text(acp={**MODE["nat"], "laIP": "10.0.1.1", "waIP": "192.168.1.10", "usbF": "0x458",
                         "syNm": "AirPort Time Capsule", "waMA": "e8:8d:28:58:f1:5c"}, links=NAT_LINKS, addrs=NAT_ADDRS)
NAT_DENIED = NAT_OK.replace("usbF status=ok value=0x458", "usbF status=ok value=0x450")
NAT_AFP = NAT_OK.replace("advertise_afp=0", "advertise_afp=1")
NAT_RECREATED = facts_text(acp={**MODE["nat"], "laIP": "10.0.1.1", "waIP": "192.168.1.10", "usbF": "0x450",
                                "syNm": "AirPort Time Capsule", "waMA": "e8:8d:28:58:f1:5c"},
                           links=[("bridge0", 11), ("mgi1", 2), ("lo0", 5)],
                           addrs=[(11, "10.0.1.1", 24), (11, "fe80::ff:fe00:b", 64), (2, "192.168.1.10", 24), (5, "127.0.0.1", 8)])
NAT_NO_WAMA = NAT_OK.replace("key=waMA status=ok value=e8:8d:28:58:f1:5c", "key=waMA status=unavailable value=")


@pytest.fixture(scope="module")
def rig():
    root = Path(tempfile.mkdtemp(prefix="tcdnssd"))
    sock = root / "mDNSResponder"
    binary = compile_service(root / "service", flags=[
        f'-DMDNS_UDS_SERVERPATH="{sock}"', "-DTC_PLAN_POLL_MS=300", "-DREG_BACKOFF_MIN_MS=200",
        "-DREG_BACKOFF_MAX_MS=1000", "-DREG_PENDING_TIMEOUT_MS=1000",
        "-DREG_IPC_ALARM_SECONDS=2", "-D_DNS_SD_LIBDISPATCH=0"])
    yield root, sock, binary


class Advertiser:
    def __init__(self, binary, root, facts, *args):
        self.facts = root / f"facts-{os.getpid()}-{time.monotonic_ns()}.txt"
        self.facts.write_text(facts)
        self.log = open(root / f"log-{time.monotonic_ns()}.txt", "w+")
        self.proc = subprocess.Popen([str(binary), "discovery", "--netbios-name", "TESTCAPSULE",
                                      "--facts-file", str(self.facts), *args],
                                     stdout=self.log, stderr=subprocess.STDOUT)

    def replace_facts(self, text):
        tmp = self.facts.with_suffix(".tmp")
        tmp.write_text(text)
        os.replace(tmp, self.facts)

    def stop(self, sig=signal.SIGTERM):
        if self.proc.poll() is None:
            self.proc.send_signal(sig)
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        self.log.seek(0)
        return self.log.read()


@pytest.fixture
def daemon(rig):
    root, sock, binary = rig
    fake = FakeDnssdDaemon(str(sock))
    yield fake
    fake.close()


def adisk_args(name="Data", key="dk2", uuid=UUID, flags="0x82"):
    return ["--adisk-share", name, key, uuid, flags]


def registered(transcript):
    return [e for e in transcript if e["op"] == "register"]


def run_discovery(binary, *args):
    return subprocess.run([str(binary), "discovery", *args], capture_output=True, text=True, timeout=10)


def run_case_result(name, *args):
    binary = compile_case(native_case_source(name))
    return subprocess.run([str(binary), *args], capture_output=True, text=True, timeout=10)


def test_registrant_keeps_shared_txt_state_below_device_budget():
    assert int(run_case("registrant_size")) < 16 * 1024


def test_adisk_txt_normalizes_lowercase_wama():
    assert run_case("adisk_txt_normalizes_wama").strip() == "sys=waMA=80:EA:96:E6:58:68,adVF=0x1010"


def test_adisk_txt_defaults_to_cloned_advf():
    assert run_case("adisk_txt_defaults_to_cloned_advf").strip() == \
        "dk2=adVF=0x1093,adVN=Data,adVU=12345678-1234-1234-1234-123456789012"


def test_adisk_txt_accepts_time_machine_smb_advf():
    assert run_case("adisk_txt_accepts_time_machine_smb_advf").strip() == \
        "dk2=adVF=0x82,adVN=Data,adVU=12345678-1234-1234-1234-123456789012"


@pytest.mark.parametrize("mode,uuid,wama,expected_rc,expected_error", [
    ("diskful", "-", "", 0, ""),
    ("diskful", UUID, "", 7, ""),
    ("diskful", UUID, "not-a-mac", 7, "adisk sys waMA must be a MAC address"),
    ("diskful", UUID, "80:EA:96:E6:58:68", 0, ""),
    ("diskless", UUID, "", 0, ""),
    ("diskless", UUID, "not-a-mac", 0, ""),
    ("diskless", "bad", "", 8, "adisk uuid must be 36 characters"),
])
def test_adisk_argument_validation_respects_diskless_mode(mode, uuid, wama, expected_rc, expected_error):
    result = run_case_result("adisk_txt_argument_validation", mode, uuid, wama)
    assert result.returncode == expected_rc
    if expected_error:
        assert expected_error in result.stderr


def test_repeated_share_arguments_preserve_txt_values(rig, daemon):
    root, _, binary = rig
    names = ["James's Backup", 'USB "Archive" $(literal); café']
    adv = Advertiser(binary, root, NAT_OK,
                     *adisk_args(name=names[0]),
                     *adisk_args(name=names[1], key="dk5", flags="0x83"))
    try:
        transcript = daemon.wait_for(lambda t: len(registered(t)) >= 4)
        assert transcript is not None, adv.stop()
        records = [r for r in registered(transcript) if r["regtype"].startswith("_adisk")]
        assert len(records) == 2
        assert all(r["txt"] == [
            "sys=waMA=E8:8D:28:58:F1:5C,adVF=0x1010",
            f"dk2=adVF=0x82,adVN={names[0]},adVU={UUID}",
            f"dk5=adVF=0x83,adVN={names[1]},adVU={UUID}",
        ] for r in records)
    finally:
        adv.stop()


def test_service_discovery_rejects_extra_adisk_share_fields(rig, daemon):
    _, _, binary = rig
    result = run_discovery(binary, *adisk_args(), "extra")
    assert result.returncode == 3
    assert "Usage:" in result.stderr
    assert daemon.transcript == []


def test_service_discovery_unknown_option_returns_timestamped_usage(rig):
    result = run_discovery(rig[2], "--auto-ip")
    assert result.returncode == 3
    assert "Usage:" in result.stderr and "serving summary" not in result.stderr
    assert all(re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} ", line)
               for line in result.stderr.splitlines())


def test_service_discovery_version(rig):
    result = run_discovery(rig[2], "--version")
    assert result.returncode == 0 and result.stdout == "30100\n" and result.stderr == ""


def test_service_discovery_accepts_debug_logging_before_version(rig):
    result = run_discovery(rig[2], "--debug-logging", "--version")
    assert result.returncode == 0 and result.stdout == "30100\n" and result.stderr == ""


@pytest.mark.parametrize("args", [
    ("--name", "TimeCapsule", "--ipv4", "192.168.1.217"),
    ("--name", "TimeCapsule", "--ttl", "30"),
    ("--name", "TimeCapsule", "--auto-ip"),
    ("--check-auto-ip",),
    ("--print-link-plan",),
    ("--print-mast",),
    ("--print-mast", "--timeout-seconds", "1"),
])
def test_service_discovery_rejects_removed_nbns_cli_modes(rig, args):
    result = run_discovery(rig[2], *args)
    assert result.returncode == 3 and "Usage:" in result.stderr


def test_service_discovery_help_reports_native_interface(rig):
    result = run_discovery(rig[2], "--help")
    assert result.returncode == 0 and "Usage:" in result.stderr
    for removed in ("--auto-ip", "--ipv4", "--ttl", "--check-auto-ip"):
        assert removed not in result.stderr


def test_service_discovery_rejects_overlong_name_before_truncation(rig):
    result = run_discovery(rig[2], "--netbios-name", "ABCDEFGHIJKLMNOP")
    assert result.returncode == 3 and "15 bytes or fewer" in result.stderr
    assert all(re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} ", line)
               for line in result.stderr.splitlines())


def test_changed_adisk_txt_replaces_only_adisk_registrations(rig, daemon):
    root, _, binary = rig
    adv = Advertiser(binary, root, NAT_OK, *adisk_args())
    try:
        assert daemon.wait_for(lambda t: len(registered(t)) >= 4) is not None
        initial = registered(daemon.transcript)
        smb_connections = {row["conn"] for row in initial if row["regtype"] == "_smb._tcp"}
        adisk_connections = {row["conn"] for row in initial if row["regtype"].startswith("_adisk")}
        adv.replace_facts(NAT_OK.replace("e8:8d:28:58:f1:5c", "00:11:22:33:44:55"))
        transcript = daemon.wait_for(
            lambda rows: len(registered(rows)) >= 6 and
            adisk_connections <= {row["conn"] for row in rows if row["op"] == "close"}
        )
        assert transcript is not None
        replacements = registered(transcript)[4:]
        assert len(replacements) == 2
        assert all(row["regtype"].startswith("_adisk") for row in replacements)
        assert all(row["txt"][0] == "sys=waMA=00:11:22:33:44:55,adVF=0x1010" for row in replacements)
        closed = {row["conn"] for row in transcript if row["op"] == "close"}
        assert not (smb_connections & closed)
    finally:
        adv.stop()


@pytest.mark.parametrize("args,exit_code", [
    (["--adisk-share"], 3),
    (["--adisk-share", "Data", "dk2", UUID], 3),
    (adisk_args() + ["extra"], 3),
    (adisk_args(name=""), 8),
    (adisk_args(name="bad\tname"), 8),
    (adisk_args(key="bad.key"), 8),
    (adisk_args(uuid="bad-uuid"), 8),
    (adisk_args(flags="bad"), 8),
    (adisk_args(flags="0x" + "f" * 14), 8),
    (adisk_args(name="x" * 256), 8),
    (["--diskless", *adisk_args(uuid="bad-uuid")], 8),
    (["--adisk-shares-file", "unused.tsv"], 3),
])
def test_invalid_share_arguments_fail_before_registration(rig, daemon, args, exit_code):
    _, _, binary = rig
    result = subprocess.run([str(binary), "discovery", *args], capture_output=True, text=True, timeout=10)
    assert result.returncode == exit_code, result.stderr
    assert daemon.transcript == []


def test_share_argument_count_is_bounded(rig):
    _, _, binary = rig
    args = [arg for i in range(16) for arg in adisk_args(key=f"dk{i}")]
    result = subprocess.run([str(binary), "discovery", *args, "--version"], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    result = subprocess.run([str(binary), "discovery", *args, *adisk_args(key="dk16"), "--version"],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 8 and "too many adisk disks" in result.stderr


def test_startup_registers_with_apples_shared_default_name(rig, daemon):
    root, _, binary = rig
    adv = Advertiser(binary, root, NAT_OK, *adisk_args())
    try:
        transcript = daemon.wait_for(lambda t: len(registered(t)) >= 4)
        assert transcript is not None, adv.stop()
        regs = registered(transcript)
        # Stock diskd renamed SMB and ADisk together in the 2026-09-19 device
        # capture. An empty IPC name means DNSServiceRegister(name=NULL).
        assert all(not r["no_auto_rename"] and r["name"] == "" and r["version"] == 1
                   and r["domain"] == "" and r["host"] == "" for r in regs)
        assert daemon.registrations() == [(2, "_adisk._tcp,_airport", "AirPort Time Capsule"), (2, "_smb._tcp", "AirPort Time Capsule"),
                                          (9, "_adisk._tcp,_airport", "AirPort Time Capsule"), (9, "_smb._tcp", "AirPort Time Capsule")]
        smb = next(r for r in regs if r["regtype"] == "_smb._tcp")
        adisk = next(r for r in regs if r["regtype"].startswith("_adisk"))
        assert smb["port"] == 445 and smb["txt"] == []
        assert adisk["port"] == 9
        assert adisk["txt"] == ["sys=waMA=E8:8D:28:58:F1:5C,adVF=0x1010", f"dk2=adVF=0x82,adVN=Data,adVU={UUID}"]
        # No AFP unless MDNS_ADVERTISE_AFP=1 (macOS 27 hides AFP-advertising capsules).
        assert not any(r["regtype"] == "_afpovertcp._tcp" for r in regs)
        # Steady state: nothing gets re-registered while the plan is unchanged.
        time.sleep(1.0)
        assert len(registered(daemon.transcript)) == 4
    finally:
        log = adv.stop()
    # SIGTERM deallocates every ref: the daemon sees every connection close.
    transcript = daemon.wait_for(lambda t: sum(e["op"] == "close" for e in t) >= 4)
    assert transcript is not None, log
    assert daemon.registrations() == []
    assert adv.proc.returncode == 0


def test_apple_conflict_rename_keeps_all_registration_connections(rig, daemon):
    # Live stock diskd renamed SMB and ADisk together after an SMB-only
    # conflict, without changing syNm or the hostname (2026-09-19). Both the
    # initial callback and later default-name changes must stay registered.
    root, _, binary = rig
    daemon.rename_default("AirPort Time Capsule (2)")
    adv = Advertiser(binary, root, NAT_DENIED, *adisk_args())
    try:
        assert daemon.wait_for(lambda t: len(registered(t)) == 2) is not None
        time.sleep(0.7)
        daemon.rename_default("AirPort Time Capsule (3)")
        # ACP is not the owner of the Bonjour instance name. A changed or
        # unavailable syNm must not replace the daemon's live registrations.
        adv.replace_facts(NAT_DENIED.replace("value=AirPort Time Capsule", "value=Stale ACP Name"))
        time.sleep(1.0)
        assert adv.proc.poll() is None
        assert len(registered(daemon.transcript)) == 2
        assert not any(entry["op"] == "close" for entry in daemon.transcript)
        assert daemon.registrations() == [(9, "_adisk._tcp,_airport", "AirPort Time Capsule (3)"),
                                          (9, "_smb._tcp", "AirPort Time Capsule (3)")]
    finally:
        adv.stop()


def test_native_default_name_does_not_require_an_acp_name(rig, daemon):
    # Apple's daemon already owns its computer name. An unavailable ACP name
    # must not suppress otherwise valid default-name registrations.
    root, _, binary = rig
    facts = NAT_DENIED.replace("key=syNm status=ok value=AirPort Time Capsule", "key=syNm status=abort value=")
    facts = facts.replace("hostname: airport-time-capsule", "hostname: ")
    adv = Advertiser(binary, root, facts, *adisk_args())
    try:
        assert daemon.wait_for(lambda t: len(registered(t)) == 2) is not None
        assert daemon.registrations() == [(9, "_adisk._tcp,_airport", "AirPort Time Capsule"),
                                          (9, "_smb._tcp", "AirPort Time Capsule")]
    finally:
        adv.stop()


@pytest.mark.parametrize("missing", ["mode", "usbF"])
def test_cold_start_waits_for_critical_facts_then_registers(rig, daemon, missing):
    root, _, binary = rig
    cold = NAT_OK.replace("key=raNA status=ok value=1", "key=raNA status=abort value=") if missing == "mode" else NAT_OK.replace("key=usbF status=ok value=0x458", "key=usbF status=abort value=")
    assert cold != NAT_OK
    adv = Advertiser(binary, root, cold, *adisk_args())
    try:
        time.sleep(1.0)
        assert registered(daemon.transcript) == [] and adv.proc.poll() is None
        adv.replace_facts(NAT_OK)
        assert daemon.wait_for(lambda t: len(registered(t)) >= 4) is not None
    finally:
        adv.stop()


@pytest.mark.parametrize("key", ["syNm", "waMA"])
def test_aborted_identity_read_does_not_rename_or_withdraw(rig, daemon, key):
    root, _, binary = rig
    adv = Advertiser(binary, root, NAT_OK, *adisk_args())
    try:
        assert daemon.wait_for(lambda t: len(registered(t)) >= 4) is not None, adv.stop()
        value = {"syNm": "AirPort Time Capsule", "waMA": "e8:8d:28:58:f1:5c"}[key]
        aborted = NAT_OK.replace(f"key={key} status=ok value={value}", f"key={key} status=abort value=")
        assert aborted != NAT_OK
        adv.replace_facts(aborted)
        time.sleep(1.5)
        assert sum(e["op"] == "close" for e in daemon.transcript) == 0
        assert len(registered(daemon.transcript)) == 4
        assert daemon.registrations() == [(2, "_adisk._tcp,_airport", "AirPort Time Capsule"), (2, "_smb._tcp", "AirPort Time Capsule"),
                                          (9, "_adisk._tcp,_airport", "AirPort Time Capsule"), (9, "_smb._tcp", "AirPort Time Capsule")]
    finally:
        adv.stop()


def test_plan_change_deregisters_and_registers_only_the_delta(rig, daemon):
    root, _, binary = rig
    adv = Advertiser(binary, root, NAT_OK, *adisk_args())
    try:
        assert daemon.wait_for(lambda t: len(registered(t)) >= 4) is not None
        adv.replace_facts(NAT_DENIED)   # disks over WAN switched off
        assert daemon.wait_for(lambda t: sum(e["op"] == "close" for e in t) >= 2) is not None, adv.log
        assert daemon.registrations() == [(9, "_adisk._tcp,_airport", "AirPort Time Capsule"), (9, "_smb._tcp", "AirPort Time Capsule")]
        assert len(registered(daemon.transcript)) == 4   # the LAN refs were left alone
        adv.replace_facts(NAT_OK)       # switched back on: only the WAN pair returns
        assert daemon.wait_for(lambda t: len(registered(t)) >= 6) is not None
        assert {(r["ifindex"], r["regtype"]) for r in registered(daemon.transcript)[4:]} == {(2, "_smb._tcp"), (2, "_adisk._tcp,_airport")}
    finally:
        adv.stop()


def test_recreated_link_with_new_index_is_a_new_registration(rig, daemon):
    root, _, binary = rig
    adv = Advertiser(binary, root, NAT_DENIED)
    try:
        assert daemon.wait_for(lambda t: len(registered(t)) >= 1) is not None
        assert daemon.registrations() == [(9, "_smb._tcp", "AirPort Time Capsule")]
        adv.replace_facts(NAT_RECREATED)
        assert daemon.wait_for(lambda t: len(registered(t)) >= 2 and any(e["op"] == "close" for e in t)) is not None
        assert daemon.registrations() == [(11, "_smb._tcp", "AirPort Time Capsule")]
    finally:
        adv.stop()


def test_name_conflict_backs_off_and_retries_until_the_name_is_free(rig, daemon):
    root, _, binary = rig
    daemon.script("AirPort Time Capsule", "conflict")
    adv = Advertiser(binary, root, NAT_DENIED)
    try:
        # Conflict -> dealloc (close) -> retry after backoff, still conflicting.
        assert daemon.wait_for(lambda t: len(registered(t)) >= 2 and sum(e["op"] == "close" for e in t) >= 1, timeout=6) is not None
        regs = registered(daemon.transcript)
        assert all(r["name"] == "" for r in regs)  # retry using Apple's default, not an invented suffix
        daemon.script("AirPort Time Capsule", "accept")
        before = len(regs)
        assert daemon.wait_for(lambda t: len(registered(t)) > before, timeout=6) is not None
        time.sleep(0.5)
        assert daemon.registrations() == [(9, "_smb._tcp", "AirPort Time Capsule")]
        settled = len(registered(daemon.transcript))
        time.sleep(1.5)
        assert len(registered(daemon.transcript)) == settled   # backoff stopped once registered
    finally:
        adv.stop()


def test_retry_backoff_counts_failed_rounds_not_registrations(rig, daemon):
    root, _, binary = rig
    daemon.script("AirPort Time Capsule", "conflict")

    def intervals(facts):
        start = len(daemon.transcript)
        adv = Advertiser(binary, root, facts)
        try:
            transcript = daemon.wait_for(
                lambda rows: len([row for row in rows[start:] if row["op"] == "register" and
                                  row["ifindex"] == 9 and row["regtype"] == "_smb._tcp"]) >= 3,
                timeout=6,
            )
            assert transcript is not None, adv.stop()
            times = [row["time"] for row in transcript[start:] if row["op"] == "register" and
                     row["ifindex"] == 9 and row["regtype"] == "_smb._tcp"][:3]
            return times[1] - times[0], times[2] - times[1]
        finally:
            adv.stop()

    single = intervals(NAT_DENIED)
    several = intervals(NAT_AFP)
    assert 0.1 <= single[0] < 0.7 and 0.2 <= single[1] < 0.8
    assert 0.1 <= several[0] < 0.7 and 0.2 <= several[1] < 0.8
    assert abs(single[1] - several[1]) < 0.25


def test_daemon_absent_is_degraded_and_recovers_when_it_returns(rig, daemon):
    root, _, binary = rig
    daemon.go_away()
    adv = Advertiser(binary, root, NAT_DENIED)
    try:
        time.sleep(1.0)
        assert registered(daemon.transcript) == []
        assert adv.proc.poll() is None            # never exits, never spawns a daemon
        daemon.come_back()
        assert daemon.wait_for(lambda t: len(registered(t)) >= 1, timeout=6) is not None
        assert daemon.registrations() == [(9, "_smb._tcp", "AirPort Time Capsule")]
        # The daemon dies underneath a live registration: reconnect on backoff.
        daemon.go_away()
        time.sleep(0.5)
        daemon.come_back()
        assert daemon.wait_for(lambda t: len(registered(t)) >= 2, timeout=6) is not None
        assert daemon.registrations() == [(9, "_smb._tcp", "AirPort Time Capsule")]
    finally:
        log = adv.stop()
    assert "mDNSResponder unreachable" in log and "reachable again" in log


def test_daemon_that_never_acknowledges_is_fenced_by_the_alarm(rig, daemon):
    """B.7 IPC fence (review finding 5): the stub blocks synchronously on the
    acknowledgement; the alarm turns that into an exit the manager relaunches
    from, and the daemon sees the socket close."""
    root, _, binary = rig
    daemon.script("AirPort Time Capsule", "stall")
    started = time.monotonic()
    adv = Advertiser(binary, root, NAT_DENIED)
    try:
        assert daemon.wait_for(lambda t: len(registered(t)) >= 1) is not None
        adv.proc.wait(timeout=6)
        elapsed = time.monotonic() - started
        assert adv.proc.returncode == 14, adv.stop()
        assert elapsed < 5
        assert daemon.wait_for(lambda t: any(e["op"] == "close" for e in t)) is not None
    finally:
        log = adv.stop()
    assert "did not answer; exiting for relaunch" in log


def test_sigterm_during_a_stalled_call_still_exits_within_the_alarm(rig, daemon):
    root, _, binary = rig
    daemon.script("AirPort Time Capsule", "stall")
    adv = Advertiser(binary, root, NAT_DENIED)
    try:
        assert daemon.wait_for(lambda t: len(registered(t)) >= 1) is not None
        time.sleep(0.3)
        adv.proc.send_signal(signal.SIGTERM)
        adv.proc.wait(timeout=6)
        assert adv.proc.returncode == 14
    finally:
        adv.stop()


def test_slow_but_answering_daemon_is_not_fenced(rig, daemon):
    root, _, binary = rig
    daemon.script("AirPort Time Capsule", "slow")
    adv = Advertiser(binary, root, NAT_DENIED)
    try:
        assert daemon.wait_for(lambda t: len(registered(t)) >= 1) is not None
        time.sleep(2.5)
        assert adv.proc.poll() is None
        assert daemon.registrations() == [(9, "_smb._tcp", "AirPort Time Capsule")]
    finally:
        log = adv.stop()
    assert adv.proc.returncode == 0, log


def test_acknowledged_registration_without_callback_times_out_and_retries(rig, daemon):
    root, _, binary = rig
    daemon.script("AirPort Time Capsule", "delay")
    adv = Advertiser(binary, root, NAT_DENIED)
    try:
        transcript = daemon.wait_for(
            lambda events: len(registered(events)) >= 2 and
            any(event["op"] == "close" for event in events),
            timeout=6,
        )
        assert transcript is not None, adv.stop()
        assert adv.proc.poll() is None
    finally:
        log = adv.stop()
    assert "initial callback timed out" in log


def test_slow_ack_gets_full_pending_callback_deadline(rig, daemon):
    root, _, binary = rig
    daemon.script("AirPort Time Capsule", "slow-delay")
    adv = Advertiser(binary, root, NAT_DENIED)
    try:
        assert daemon.wait_for(lambda events: len(registered(events)) == 1) is not None
        assert daemon.wait_for(lambda _events: daemon.held_reply_count() == 1, timeout=3) is not None
        time.sleep(0.6)
        daemon.release("AirPort Time Capsule")
        time.sleep(1.2)
        assert len(registered(daemon.transcript)) == 1
        assert not any(event["op"] == "close" for event in daemon.transcript)
    finally:
        adv.stop()


def test_dropped_connection_is_retried(rig, daemon):
    root, _, binary = rig
    daemon.script("AirPort Time Capsule", "drop")
    adv = Advertiser(binary, root, NAT_DENIED)
    try:
        assert daemon.wait_for(lambda t: len(registered(t)) >= 2, timeout=6) is not None
        daemon.script("AirPort Time Capsule", "accept")
        assert daemon.wait_for(lambda t: daemon.registrations() == [(9, "_smb._tcp", "AirPort Time Capsule")], timeout=6) is not None
    finally:
        adv.stop()


def test_diskless_registers_nothing_but_stays_alive(rig, daemon):
    root, _, binary = rig
    adv = Advertiser(binary, root, NAT_OK, "--diskless", *adisk_args())
    try:
        time.sleep(1.0)
        assert registered(daemon.transcript) == [] and adv.proc.poll() is None
        adv.replace_facts(NAT_DENIED)
        time.sleep(0.8)
        assert registered(daemon.transcript) == []
    finally:
        adv.stop()
    assert adv.proc.returncode == 0


def test_afp_registered_only_when_config_enables_it(rig, daemon):
    root, _, binary = rig
    adv = Advertiser(binary, root, NAT_AFP)
    try:
        assert daemon.wait_for(lambda t: len(registered(t)) >= 4) is not None
        assert daemon.registrations() == [(2, "_afpovertcp._tcp", "AirPort Time Capsule"), (2, "_smb._tcp", "AirPort Time Capsule"),
                                          (9, "_afpovertcp._tcp", "AirPort Time Capsule"), (9, "_smb._tcp", "AirPort Time Capsule")]
        afp = next(r for r in registered(daemon.transcript) if r["regtype"] == "_afpovertcp._tcp")
        assert afp["port"] == 548 and afp["txt"] == []
        adv.replace_facts(NAT_OK)
        assert daemon.wait_for(lambda t: sum(e["op"] == "close" for e in t) >= 2) is not None
        assert daemon.registrations() == [(2, "_smb._tcp", "AirPort Time Capsule"), (9, "_smb._tcp", "AirPort Time Capsule")]
    finally:
        adv.stop()


def test_adisk_skipped_without_wama_and_without_rows(rig, daemon):
    root, _, binary = rig
    adv = Advertiser(binary, root, NAT_NO_WAMA, *adisk_args())
    try:
        assert daemon.wait_for(lambda t: len(registered(t)) >= 2) is not None
        time.sleep(0.5)
        assert daemon.registrations() == [(2, "_smb._tcp", "AirPort Time Capsule"), (9, "_smb._tcp", "AirPort Time Capsule")]
    finally:
        adv.stop()
    adv = Advertiser(binary, root, NAT_OK)
    try:
        assert daemon.wait_for(lambda t: len(registered(t)) >= 2) is not None
        time.sleep(0.5)
        assert all(r["regtype"] == "_smb._tcp" for r in registered(daemon.transcript))
    finally:
        adv.stop()


def test_print_link_plan_and_bad_share_arguments(rig):
    root, _, binary = rig
    facts = root / "plan-facts.txt"
    facts.write_text(NAT_OK)
    result = subprocess.run([str(binary), "--print-link-plan", "--facts-file", str(facts)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0 and result.stdout.startswith("plan: status=validated mode=nat")
    assert "link: name=bridge0 index=9 role=lan mask=smb,adisk" in result.stdout
    result = subprocess.run([str(binary), "discovery", "--netbios-name", "TESTCAPSULE", "--facts-file", str(facts), *adisk_args(uuid="bad-uuid")], capture_output=True, text=True, timeout=10)
    assert result.returncode == 8
    assert subprocess.run([str(binary), "discovery", "--version"], capture_output=True, text=True, timeout=10).stdout == "30100\n"
    assert subprocess.run([str(binary), "discovery", "--instance", "x"], capture_output=True, text=True, timeout=10).returncode == 3
