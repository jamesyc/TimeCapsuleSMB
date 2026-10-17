"""Behavioral regression tests for the Apple wcifsnd controller and codec."""
from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

import pytest

from tests.native.build import ROOT, compile_modules, compile_service
from tests.native.integration.fake_dnssd_daemon import FakeDnssdDaemon
from tests.native.test_plan import MODE, NAT_ADDRS, NAT_LINKS, facts_text
from tests.timing import hang_guard


def wait_for(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    return None


def free_port():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def event_lines(path):
    try:
        return path.read_text().splitlines()
    except FileNotFoundError:
        return []


def decode_name(packet):
    encoded = packet[13:45]
    raw = bytes(((encoded[i] - 65) << 4) | (encoded[i + 1] - 65)
                for i in range(0, 32, 2))
    return raw[:15].decode().rstrip(), raw[15]


NAT_OK = facts_text(
    acp={**MODE["nat"], "laIP": "10.0.1.1", "waIP": "192.168.1.10",
         "usbF": "0x458", "syNm": "Capsule", "waMA": "e8:8d:28:58:f1:5c"},
    links=NAT_LINKS, addrs=NAT_ADDRS, config={"nbns_enabled": 1})

# A bridge-mode device with no DHCP server: dhclient-script's PREINIT leaves
# 0.0.0.0 on bridge0 and link_local adds a 169.254 address, which ACPd
# reports as laIP. Apple's wcifsnd answers NBNS on neither.
BRIDGE_NO_LEASE = facts_text(
    acp={**MODE["bridge"], "laIP": "169.254.147.85", "usbF": "0x458",
         "syNm": "Capsule", "waMA": "e8:8d:28:58:f1:5c"},
    links=[("bridge0", 9), ("lo0", 5)],
    addrs=[(9, "0.0.0.0", 0), (9, "169.254.147.85", 16), (9, "fe80::ff:fe00:1", 64), (5, "127.0.0.1", 8)],
    config={"nbns_enabled": 1})
# BOUND replaced 0.0.0.0 with the lease, which ACPd then reports.
BRIDGE_LEASED = (BRIDGE_NO_LEASE
                 .replace("addr=0.0.0.0 scope=9 prefix=0", "addr=192.168.1.10 scope=9 prefix=24")
                 .replace("key=laIP status=ok value=169.254.147.85", "key=laIP status=ok value=192.168.1.10"))
# 169.254 with no other IPv4 address: Apple's wcifsnd answers on it.
BRIDGE_LINK_LOCAL_ONLY = BRIDGE_NO_LEASE.replace("addr: link=9 family=inet addr=0.0.0.0 scope=9 prefix=0\n", "")

WCIFSND_UNIT_MODULES = (
    "native/common/acp.c",
    "native/common/addr.c",
    "native/common/log.c",
    "native/common/process.c",
)


# Base of the doubling retry backoff after a failed child (device 1 s). The
# first retry comes two bases after a failure; waiting three first retries
# proves that none was scheduled.
WCIFSND_RETRY_MS = 200
PAST_FIRST_RETRY = 3 * 2 * WCIFSND_RETRY_MS / 1000
# The cancel cases must act inside a first backoff: give them a 2 s one, and
# prove none was scheduled after two of them.
CANCEL_RETRY_MS = 1000
CANCEL_PAST_FIRST_RETRY = 2 * 2 * CANCEL_RETRY_MS / 1000
# The reply wait before any timed out, and after one did (device 30 s, 100 s).
# A fake's delayed answer (up to 0.6 s) stays far inside the first.
FIRST_REPLY_MS = 3000
REPLY_MS = 4000
# Listener and ownership-check deadlines, and the stop grace before SIGKILL.
START_MS = 3000
INSPECT_MS = 3000
STOP_MS = 500
GRACE_MS = 500
# Starting the Python fake child and its fake fstat on a loaded runner.
CHILD_START_MS = 600


def first_retry_ms(retry_ms, failures=1):
    """The backoff before the retry after this many failures in a row."""
    return 2 * retry_ms << (failures - 1)


# A child that never answers its first add: the short reply wait, its stop,
# the first backoff and the next child's start.
FIRST_REPLACEMENT = (FIRST_REPLY_MS, STOP_MS, first_retry_ms(WCIFSND_RETRY_MS), CHILD_START_MS)


def build_rig(retry_ms):
    root = Path(tempfile.mkdtemp(prefix="tcwcifsnd-"))
    port = free_port()
    fake = ROOT / "tests/native/integration/fake_wcifsnd.py"
    fake.chmod(0o755)
    fake_fstat = ROOT / "tests/native/integration/fake_fstat.py"
    fake_fstat.chmod(0o755)
    sock = root / "mDNSResponder"
    # Discovery runs boot.sh's /usr/libexec link, and /sbin/wcifsnd only when
    # that link is missing; the stand-in for the second marks its starts.
    primary, fallback = root / "libexec-wcifsnd", root / "sbin-wcifsnd"
    primary.symlink_to(fake)
    fallback.write_text(f'#!/bin/sh\necho FALLBACK >>"$TC_FAKE_WCIFSND_EVENTS"\nexec "{fake}" "$@"\n')
    fallback.chmod(0o755)
    binary = compile_service(root / "service", flags=[
        f'-DMDNS_UDS_SERVERPATH="{sock}"', f'-DWCIFSND_PATH="{primary}"',
        f'-DWCIFSND_FALLBACK_PATH="{fallback}"',
        f'-DTC_FSTAT_PATH="{fake_fstat}"',
        f"-DWCIFSND_PORT={port}", f"-DWCIFSND_START_MS={START_MS}", f"-DWCIFSND_INSPECT_MS={INSPECT_MS}",
        f"-DTC_CHILD_GRACE_MS={GRACE_MS}", f"-DWCIFSND_FIRST_REPLY_MS={FIRST_REPLY_MS}",
        f"-DWCIFSND_REPLY_MS={REPLY_MS}",
        f"-DWCIFSND_STOP_MS={STOP_MS}", "-DTC_PLAN_POLL_MS=200", "-DREG_BACKOFF_MIN_MS=100",
        "-DREG_BACKOFF_MAX_MS=200", "-DREG_IPC_ALARM_SECONDS=1", "-D_DNS_SD_LIBDISPATCH=0",
        f"-DWCIFSND_RETRY_MS={retry_ms}"])
    # The new binary's first-exec check belongs to setup, not a startup deadline.
    subprocess.run([str(binary), 'discovery', '--help'], capture_output=True, check=True, timeout=10)
    return root, sock, binary, port


@pytest.fixture(scope="module")
def rig():
    return build_rig(WCIFSND_RETRY_MS)


@pytest.fixture(scope="module")
def cancel_rig():
    return build_rig(CANCEL_RETRY_MS)


class Discovery:
    def __init__(self, rig, facts=NAT_OK, name="machine", diskless=False, mode="success",
                 extra_env=None, pass_fds=(), shares=False, stdin=None, fstat_mode="owned", debug=False):
        root, _, binary, port = rig
        stamp = time.monotonic_ns()
        self.facts = root / f"facts-{stamp}"
        self.events = root / f"events-{stamp}"
        self.mode = root / f"mode-{stamp}"
        self.fstat_mode = root / f"fstat-mode-{stamp}"
        self.facts.write_text(facts)
        self.mode.write_text(mode)
        self.fstat_mode.write_text(fstat_mode)
        env = {**os.environ, "TC_FAKE_WCIFSND_PORT": str(port),
               "TC_FAKE_WCIFSND_EVENTS": str(self.events), "TC_FAKE_WCIFSND_MODE": str(self.mode),
               "TC_FAKE_FSTAT_MODE": str(self.fstat_mode)}
        env.update(extra_env or {})
        args = [str(binary), "discovery", "--facts-file", str(self.facts)]
        if diskless:
            args.append("--diskless")
        else:
            args += ["--netbios-name", name]
        if shares:
            args += ["--adisk-share", "Data", "dk2", "11111111-1111-1111-1111-111111111111", "0x82"]
        if debug:
            args.append('--debug-logging')
        self.log = bytearray()
        self.proc = subprocess.Popen(args, env=env, pass_fds=pass_fds,
                                     stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def replace(self, facts):
        temporary = self.facts.with_suffix(".tmp")
        temporary.write_text(facts)
        os.replace(temporary, self.facts)

    def read_log(self):
        fd = self.proc.stderr.fileno()
        os.set_blocking(fd, False)
        try:
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                self.log.extend(chunk)
        except BlockingIOError:
            pass
        finally:
            os.set_blocking(fd, True)
        return self.log.decode()

    def wait_incomplete_passes(self, count):
        # wcifsnd_apply_plan precedes this existing registrant debug event.
        assert wait_for(lambda: self.read_log().count('registrant: plan incomplete') >= count, timeout=10)

    def stop(self, timeout=3):
        if self.proc.poll() is None:
            self.proc.terminate()
        try:
            out, err = self.proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            out, err = self.proc.communicate(timeout=timeout)
        # pytest shows captured stderr only for a failed test; this log
        # names which deadline or check replaced a child.
        err = self.log.decode() + (err or "")
        sys.stderr.write(err)
        return out, err


@pytest.fixture
def dnssd(request):
    # The daemon of whichever rig the test runs.
    sock = request.getfixturevalue("cancel_rig" if "cancel_rig" in request.fixturenames else "rig")[1]
    fake = FakeDnssdDaemon(str(sock))
    yield fake
    fake.close()


def add_events(discovery):
    """(child pid, request) for every add the fake daemon received."""
    return [(int(line.split()[1]), bytes.fromhex(line.split()[2]))
            for line in event_lines(discovery.events) if line.startswith("ADD ")]


def adds(discovery):
    return [request for _, request in add_events(discovery)]


def children(discovery):
    return [int(line.split()[1]) for line in event_lines(discovery.events) if line.startswith("START ")]


def first_added_child(discovery):
    """The child that received the first add. On a loaded host discovery can
    discard a slow child at startup or inspection before sending it anything,
    then retry as it should; that earlier child is not the one under test."""
    events = add_events(discovery)
    return events[0][0] if events else None


def generation(discovery, pid):
    """Event lines from pid's START up to the next child's START."""
    lines = event_lines(discovery.events)
    start = lines.index(f"START {pid}")
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("START ")), len(lines))
    return lines[start:end]


def process_exists(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def assert_reaped(pid, owner_pid=None, timeout=1):
    row = ""

    def reaped():
        nonlocal row
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        process = subprocess.run(["ps", "-p", str(pid), "-o", "ppid=", "-o", "stat=", "-o", "command="],
                                 capture_output=True, text=True)
        row = process.stdout.strip()
        if not row:
            return True  # Exited between kill(0) and ps.
        parent, state, *command = row.split(maxsplit=2)
        # A different parent means the original child was reaped and this PID
        # was reused. Zombie argv alone cannot establish process identity.
        if owner_pid is not None and int(parent) != owner_pid:
            return True
        return not state.startswith("Z") and "fake_wcifsnd.py" not in " ".join(command)

    assert wait_for(reaped, timeout=timeout), row


@pytest.mark.parametrize("missing,output,owner_pid,reaped", [
    (True, "", 123, True),
    (False, "", 123, True),
    (False, "456 S python unrelated.py\n", 123, True),
    (False, "456 Z <defunct>\n", 123, True),  # Reused PID, including a zombie.
    (False, "123 S python fake_wcifsnd.py\n", 123, False),
    (False, "123 Z <defunct>\n", 123, False),  # macOS drops the argv.
    (False, "123 Z+ [python3] <defunct>\n", 123, False),  # Linux zombie.
    (False, "456 Z <defunct>\n", None, False),  # No owner to disambiguate shutdown.
])
def test_assert_reaped_distinguishes_gone_reused_live_and_zombie_pids(missing, output, owner_pid, reaped):
    with mock.patch.object(os, "kill", side_effect=ProcessLookupError if missing else None), \
         mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess(
             [], 0 if output else 1, stdout=output, stderr="")) as ps:
        if reaped:
            assert_reaped(12345, owner_pid, timeout=0.05)
        else:
            with pytest.raises(AssertionError):
                assert_reaped(12345, owner_pid, timeout=0.05)
        if missing:
            assert ps.call_count == 0
        else:
            assert ps.call_count >= 1


def test_assert_reaped_waits_for_transient_zombie():
    outputs = ["123 Z <defunct>\n", ""]
    with mock.patch.object(os, "kill"), mock.patch.object(subprocess, "run", side_effect=[
        subprocess.CompletedProcess([], 0, stdout=output, stderr="") for output in outputs
    ]) as ps, mock.patch(f"{__name__}.wait_for", side_effect=lambda predicate, timeout: predicate() or predicate()):
        assert_reaped(12345, 123, timeout=0.1)
        assert ps.call_count == 2


def bonjour_connections(daemon):
    transcript = daemon.wait_for(lambda rows: len([r for r in rows if r['op'] == 'register']) >= 4, timeout=15)
    assert transcript is not None
    records = [r for r in transcript if r['op'] == 'register']
    assert {'_smb._tcp', '_adisk._tcp,_airport'} <= {r['regtype'] for r in records}
    return {r['conn'] for r in records}


def assert_bonjour_unchanged(discovery, daemon, connections):
    assert discovery.proc.poll() is None
    assert all(int(line.split()[1]) == discovery.proc.pid for line in event_lines(discovery.events)
               if line.startswith('OWNER '))
    with daemon.lock:
        rows = list(daemon.transcript)
    assert {r['conn'] for r in rows if r['op'] == 'register'} == connections
    assert not any(r['op'] == 'close' and r['conn'] in connections for r in rows)


def test_codec_golden_packets_and_adversarial_responses(tmp_path):
    binary = compile_modules(tmp_path / "wcifsnd-unit", WCIFSND_UNIT_MODULES,
                             extra_sources=[ROOT / "tests/native/unit/test_wcifsnd.c"])
    lines = subprocess.check_output([binary], text=True).splitlines()
    packets = [bytes.fromhex(line) for line in lines[:3]]
    assert [decode_name(packet) for packet in packets] == [
        ("MACHINE", 0x00), ("WORKGROUP", 0x00), ("MACHINE", 0x20)]
    assert all(len(packet) == 68 and packet[2:4] == b"\x29\x00" for packet in packets)
    assert lines[3:] == ["success=1", "stale=0", "malformed=-1", "defended=-1", "negative=-1",
                         "conflict=-1", "wack=-1", "rdlength=-1", "short=-1", "runt=0", "unrelated=1"]


def test_retry_deadlines_cleanup_and_eligibility(tmp_path):
    binary = compile_modules(tmp_path / 'recovery-unit', WCIFSND_UNIT_MODULES,
                             extra_sources=[ROOT / 'tests/native/unit/test_wcifsnd_recovery.c'])
    subprocess.run([binary], check=True, capture_output=True, timeout=10)


@pytest.mark.parametrize("facts,diskless", [
    (NAT_OK.replace("key=raNA status=ok value=1", "key=raNA status=abort value="), False),
    (NAT_OK.replace("family=inet addr=10.0.1.1", "family=inet6 addr=2001:db8::10")
           .replace("family=inet addr=192.168.1.10", "family=inet6 addr=2001:db8::11")
           .replace("family=inet addr=169.254.140.130", "family=inet6 addr=2001:db8::12"), False),
    (NAT_OK, True),
    (BRIDGE_NO_LEASE, False),
])
def test_ineligible_cold_start_does_not_spawn(rig, dnssd, facts, diskless):
    discovery = Discovery(rig, facts=facts, diskless=diskless)
    try:
        time.sleep(0.7)
        assert not event_lines(discovery.events)
        assert discovery.proc.poll() is None
    finally:
        discovery.stop()


def test_startup_hup_ipv4_loss_and_child_death_lifecycle(rig, dnssd):
    discovery = Discovery(rig)
    try:
        assert wait_for(lambda: len(adds(discovery)) == 3)
        assert [decode_name(packet) for packet in adds(discovery)] == [
            ("MACHINE", 0), ("WORKGROUP", 0), ("MACHINE", 0x20)]
        time.sleep(0.35)
        assert len(adds(discovery)) == 3
        assert len([line for line in event_lines(discovery.events) if line.startswith("FSTAT ")]) == 1
        assert wait_for(lambda: event_lines(discovery.events).count("HUP") >= 1)
        assert len(adds(discovery)) == 3

        discovery.replace(NAT_OK.replace("family=inet addr=", "family=inet6 addr=::ffff:"))
        assert wait_for(lambda: "STOP" in event_lines(discovery.events))
        assert discovery.proc.poll() is None

        discovery.replace(NAT_OK)
        assert wait_for(lambda: sum(line.startswith("START ") for line in event_lines(discovery.events)) == 2)
        assert wait_for(lambda: len(adds(discovery)) == 6)
        child = int([line.split()[1] for line in event_lines(discovery.events) if line.startswith("START ")][-1])
        os.kill(child, signal.SIGKILL)
        assert wait_for(lambda: len(children(discovery)) == 3, timeout=15)
        assert wait_for(lambda: len(adds(discovery)) == 9)
        assert_reaped(child, discovery.proc.pid)
        assert discovery.proc.poll() is None
    finally:
        discovery.stop()


def test_cold_invalid_plan_recovers_when_facts_become_valid(rig, dnssd):
    cold = NAT_OK.replace("key=raNA status=ok value=1", "key=raNA status=abort value=")
    discovery = Discovery(rig, facts=cold)
    try:
        time.sleep(0.5)
        assert not event_lines(discovery.events)
        discovery.replace(NAT_OK)
        assert wait_for(lambda: len(adds(discovery)) == 3)
    finally:
        discovery.stop()


def test_normal_launch_requires_netbios_name(rig):
    result = subprocess.run([str(rig[2]), "discovery", "--facts-file", str(rig[0] / "missing")],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 3


@pytest.mark.parametrize('mode,requests', [('drop', 1), ('drop-after-1', 2), ('drop-after-2', 3),
                                         ('negative', 1), ('malformed', 1), ('wack', 1), ('no-listener', 0)])
def test_failed_child_retries_without_withdrawing_bonjour(rig, dnssd, mode, requests):
    discovery = Discovery(rig, mode=mode, shares=True)
    try:
        connections = bonjour_connections(dnssd)
        # Without a listener no child receives an add, so the first one is
        # under test.
        tried = wait_for(lambda: first_added_child(discovery) if requests else
                         next(iter(children(discovery)), None), timeout=20)
        assert tried
        # Without a listener the start deadline replaces the reply wait.
        assert wait_for(lambda: children(discovery)[-1] != tried,
                        timeout=hang_guard(*FIRST_REPLACEMENT) if requests else
                        hang_guard(START_MS, STOP_MS, first_retry_ms(WCIFSND_RETRY_MS), CHILD_START_MS))
        assert_reaped(tried, discovery.proc.pid)
        # Apple keeps accepted adds even after client exit. Every uncertain
        # request is sent once, then the native child is discarded before retry.
        lines = generation(discovery, tried)
        assert sum(line.startswith('ADD ') for line in lines) == requests
        assert 'STOP' in lines
        assert_bonjour_unchanged(discovery, dnssd, connections)
        discovery.mode.write_text('success')
        assert wait_for(lambda: any(line == 'HUP' for line in event_lines(discovery.events)), timeout=20)
        assert_bonjour_unchanged(discovery, dnssd, connections)
    finally:
        discovery.stop()


def adds_per_child(discovery):
    counts = {}
    for pid, _ in add_events(discovery):
        counts[pid] = counts.get(pid, 0) + 1
    return counts


def test_defended_names_register_without_resend_or_replacement(rig, dnssd):
    # Issue #342: another host on the LAN owns the name. Apple's wcifsnd
    # reports each defended broadcast with rcode 3 but still registers the
    # name ~2 s later on the same transaction, as stock firmware does.
    discovery = Discovery(rig, mode="defended", shares=True)
    try:
        connections = bonjour_connections(dnssd)
        assert wait_for(lambda: "HUP" in event_lines(discovery.events), timeout=15)
        assert [decode_name(packet) for packet in adds(discovery)] == [
            ("MACHINE", 0), ("WORKGROUP", 0), ("MACHINE", 0x20)]
        time.sleep(PAST_FIRST_RETRY)
        assert len(children(discovery)) == 1
        assert adds_per_child(discovery) == {children(discovery)[0]: 3}
        assert "STOP" not in event_lines(discovery.events)
        assert_bonjour_unchanged(discovery, dnssd, connections)
    finally:
        log = discovery.stop()[1]
    for label in ("MACHINE<00>", "WORKGROUP<00>", "MACHINE<20>"):
        assert log.count(f"wcifsnd: {label} reply rcode 3; waiting for registration") == 1
    assert "wcifsnd: registered MACHINE with Apple's native NetBIOS daemon" in log
    assert "replacing native child" not in log


def test_defended_forever_times_out_then_replaces_child_without_resend(rig, dnssd):
    discovery = Discovery(rig, mode="defended-forever", shares=True)
    try:
        connections = bonjour_connections(dnssd)
        assert wait_for(lambda: len(children(discovery)) >= 2, timeout=hang_guard(CHILD_START_MS, *FIRST_REPLACEMENT))
        assert_reaped(children(discovery)[0], discovery.proc.pid)
        # One add per child generation, however many rcode 3 replies arrive.
        assert set(adds_per_child(discovery).values()) == {1}
        assert_bonjour_unchanged(discovery, dnssd, connections)
    finally:
        log = discovery.stop()[1]
    assert (f"registration of MACHINE<00> timed out after {FIRST_REPLY_MS} ms (last reply rcode 3); "
            "replacing native child") in log
    assert "wcifsnd: registered" not in log


def test_unanswered_first_add_is_replaced_after_the_short_wait_then_registers(rig, dnssd):
    # After a reboot the first add can get no reply at all while ACPd starts
    # its own wcifsnd and wcifsfs; a fresh child then registers in seconds.
    discovery = Discovery(rig, mode="drop", shares=True)
    try:
        connections = bonjour_connections(dnssd)
        first = wait_for(lambda: first_added_child(discovery), timeout=10)
        assert first
        assert wait_for(lambda: "STOP" in generation(discovery, first), timeout=hang_guard(FIRST_REPLY_MS, STOP_MS))
        discovery.mode.write_text("success")
        assert wait_for(lambda: "HUP" in event_lines(discovery.events), timeout=15)
        assert_reaped(first, discovery.proc.pid)
        assert set(adds_per_child(discovery).values()) <= {1, 3}
        assert adds_per_child(discovery)[first] == 1
        assert_bonjour_unchanged(discovery, dnssd, connections)
    finally:
        log = discovery.stop()[1]
    assert (f"registration of MACHINE<00> timed out after {FIRST_REPLY_MS} ms (last no reply); "
            "replacing native child") in log
    assert "wcifsnd: registered MACHINE with Apple's native NetBIOS daemon" in log


def test_reply_wait_lengthens_after_a_timeout_for_slow_name_servers(rig, dnssd):
    # A WINS server slower than the short wait would otherwise replace every
    # child forever: after one timeout each add gets the full wait.
    discovery = Discovery(rig, mode="drop")
    try:
        # The short wait, then the long one, each followed by its backoff.
        assert wait_for(lambda: len(set(pid for pid, _ in add_events(discovery))) >= 3,
                        timeout=hang_guard(CHILD_START_MS, *FIRST_REPLACEMENT, REPLY_MS, STOP_MS,
                                      first_retry_ms(WCIFSND_RETRY_MS, 2), CHILD_START_MS))
    finally:
        log = discovery.stop()[1]
    timeouts = [line.split("timed out after ")[1].split(" ms")[0]
                for line in log.splitlines() if "registration of MACHINE<00> timed out after" in line]
    assert timeouts[:2] == [str(FIRST_REPLY_MS), str(REPLY_MS)]
    assert set(timeouts[1:]) == {str(REPLY_MS)}


def test_unanswered_second_record_uses_the_short_wait_then_the_long_one(rig, dnssd):
    # The machine name registers, then WORKGROUP<00> gets no reply.
    discovery = Discovery(rig, mode="drop-after-1")
    try:
        first = wait_for(lambda: first_added_child(discovery), timeout=10)
        assert first
        assert wait_for(lambda: len(set(pid for pid, _ in add_events(discovery))) >= 2,
                        timeout=hang_guard(*FIRST_REPLACEMENT))
        second = [pid for pid in adds_per_child(discovery) if pid != first][0]
        assert wait_for(lambda: "STOP" in generation(discovery, second), timeout=hang_guard(REPLY_MS, STOP_MS))
        discovery.mode.write_text("success")
        assert wait_for(lambda: "HUP" in event_lines(discovery.events), timeout=20)
    finally:
        log = discovery.stop()[1]
    timeouts = [line.split("wcifsnd: ")[1].split(";")[0] for line in log.splitlines()
                if "timed out after" in line]
    assert timeouts[:2] == [
        f"registration of WORKGROUP<00> timed out after {FIRST_REPLY_MS} ms (last no reply)",
        f"registration of WORKGROUP<00> timed out after {REPLY_MS} ms (last no reply)",
    ]
    assert "wcifsnd: registered MACHINE with Apple's native NetBIOS daemon" in log


@pytest.mark.parametrize("mode,waiting", [
    ("late-other-xid", None),
    ("malformed-then-success", "unexpected reply (62 bytes, flags 0xa800); waiting for registration"),
])
def test_stale_or_malformed_reply_then_success_registers(rig, dnssd, mode, waiting):
    discovery = Discovery(rig, mode=mode)
    try:
        assert wait_for(lambda: "HUP" in event_lines(discovery.events), timeout=15)
        time.sleep(PAST_FIRST_RETRY)
        assert len(children(discovery)) == 1
        assert adds_per_child(discovery) == {children(discovery)[0]: 3}
    finally:
        log = discovery.stop()[1]
    assert "wcifsnd: registered MACHINE with Apple's native NetBIOS daemon" in log
    assert "replacing native child" not in log
    if waiting is None:
        # Another transaction's success is ignored silently.
        assert "waiting for registration" not in log
    else:
        assert log.count(waiting) == 3


def test_child_exit_status_zero_names_the_likely_port_collision(rig, dnssd):
    discovery = Discovery(rig, mode="exit-0")
    try:
        assert wait_for(lambda: len(children(discovery)) >= 2, timeout=12)
        assert not adds(discovery)
    finally:
        log = discovery.stop()[1]
    assert ("exited with status 0 (Apple wcifsnd exits 0 when UDP 137/138/922/923 is already bound); "
            "replacing native child") in log


@pytest.mark.parametrize("fstat_mode,reason", [
    ("foreign", "native child does not own UDP 137/138/922"),
    ("missing-control", "native child does not own UDP 137/138/922"),
    ("error", "native socket ownership inspection failed"),
])
def test_unverified_native_listener_never_receives_an_add(rig, dnssd, fstat_mode, reason):
    discovery = Discovery(rig, shares=True, fstat_mode=fstat_mode)
    try:
        connections = bonjour_connections(dnssd)
        assert wait_for(lambda: "STOP" in event_lines(discovery.events), timeout=10)
        assert not adds(discovery)
        first = children(discovery)[0]
        assert_reaped(first, discovery.proc.pid)
        discovery.fstat_mode.write_text("owned")
        assert wait_for(lambda: len(adds(discovery)) == 3, timeout=15)
        assert_bonjour_unchanged(discovery, dnssd, connections)
    finally:
        log = discovery.stop()[1]
    assert reason in log


def test_child_exit_during_ownership_check_is_reported_and_retried(rig, dnssd):
    discovery = Discovery(rig, shares=True, fstat_mode="kill")
    try:
        connections = bonjour_connections(dnssd)
        assert wait_for(lambda: len(children(discovery)) >= 2, timeout=12)
        assert not adds(discovery)
        first = children(discovery)[0]
        assert_reaped(first, discovery.proc.pid)
        discovery.fstat_mode.write_text("owned")
        assert wait_for(lambda: len(adds(discovery)) == 3, timeout=15)
        assert_bonjour_unchanged(discovery, dnssd, connections)
    finally:
        log = discovery.stop()[1]
    assert "killed by signal" in log


def test_stalled_inspection_keeps_bonjour_responsive_and_stops_cleanly(rig, dnssd):
    discovery = Discovery(rig, shares=True, fstat_mode="hang")
    captured = bytearray()
    try:
        connections = bonjour_connections(dnssd)
        assert wait_for(lambda: any(line.startswith("HANG ") for line in event_lines(discovery.events)))
        helper = int(next(line.split()[1] for line in event_lines(discovery.events) if line.startswith("HANG ")))
        dnssd.rename_default("During Inspection")
        fd = discovery.proc.stderr.fileno()
        os.set_blocking(fd, False)

        def callback_logged():
            try:
                captured.extend(os.read(fd, 4096))
            except BlockingIOError:
                pass
            return b"During Inspection" in captured

        try:
            assert wait_for(callback_logged, timeout=2)
        finally:
            os.set_blocking(fd, True)
        assert not adds(discovery)
        assert_bonjour_unchanged(discovery, dnssd, connections)
        started = time.monotonic()
        rest = discovery.stop(timeout=8)[1]
        assert discovery.proc.returncode == 0
        assert time.monotonic() - started < 8
        assert wait_for(lambda: not process_exists(helper), timeout=2)
    finally:
        if discovery.proc.poll() is None:
            discovery.stop(timeout=8)
    assert "During Inspection" in captured.decode() + rest


def test_stalled_inspection_times_out_then_recovers_without_an_add(rig, dnssd):
    discovery = Discovery(rig, shares=True, fstat_mode="hang")
    try:
        connections = bonjour_connections(dnssd)
        assert wait_for(lambda: any(line.startswith("HANG ") for line in event_lines(discovery.events)))
        helper = int(next(line.split()[1] for line in event_lines(discovery.events) if line.startswith("HANG ")))
        assert not adds(discovery)
        discovery.fstat_mode.write_text("owned")
        # The fake fstat ignores SIGTERM, so its stop takes the whole grace.
        assert wait_for(lambda: len(adds(discovery)) == 3,
                        timeout=hang_guard(INSPECT_MS, GRACE_MS, first_retry_ms(WCIFSND_RETRY_MS), CHILD_START_MS))
        assert wait_for(lambda: not process_exists(helper), timeout=2)
        assert_bonjour_unchanged(discovery, dnssd, connections)
    finally:
        log = discovery.stop()[1]
    assert "native socket ownership inspection failed" in log


def test_losing_ipv4_cancels_pending_inspection_without_restarting(rig, dnssd):
    discovery = Discovery(rig, shares=True, fstat_mode="hang")
    try:
        assert wait_for(lambda: any(line.startswith("HANG ") for line in event_lines(discovery.events)))
        helper = int(next(line.split()[1] for line in event_lines(discovery.events) if line.startswith("HANG ")))
        discovery.replace(NAT_OK.replace("family=inet addr=", "family=inet6 addr=::ffff:"))
        assert wait_for(lambda: "STOP" in event_lines(discovery.events))
        assert wait_for(lambda: not process_exists(helper), timeout=3)
        time.sleep(PAST_FIRST_RETRY)
        assert len(children(discovery)) == 1
        assert not adds(discovery)
        assert discovery.proc.poll() is None
    finally:
        discovery.stop()


def set_text(path, text):
    # A running fake may read the file mid-write_text and see it empty.
    temporary = path.with_suffix(".tmp")
    temporary.write_text(text)
    os.replace(temporary, path)


def datagrams(sock):
    sock.setblocking(False)
    received = []
    while True:
        try:
            received.append(sock.recv(512))
        except BlockingIOError:
            return received


def test_native_child_exit_status_and_port_collision_recover(rig, dnssd):
    port = rig[3]
    foreign = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    discovery = Discovery(rig, mode="exit-7", shares=True)
    try:
        connections = bonjour_connections(dnssd)
        # The port stays free here, so each exit-7 child is reaped as its own
        # failure rather than stopped by a failed ownership check.
        assert wait_for(lambda: len(children(discovery)) >= 2, timeout=12)
        assert not adds(discovery)
        # Another process now holds the control port, so fstat truthfully
        # names that process. The child stays alive without binding, which
        # forces the ownership check instead of racing the child's own bind.
        set_text(discovery.fstat_mode, "foreign")
        foreign.bind(("127.0.0.1", port))
        set_text(discovery.mode, "no-listener")
        assert wait_for(lambda: "STOP" in event_lines(discovery.events), timeout=15)
        assert not adds(discovery)
        # Apple's adds are reference-counted: none may reach a listener that
        # is not our verified child.
        assert datagrams(foreign) == []
        foreign.close()
        set_text(discovery.fstat_mode, "owned")
        set_text(discovery.mode, "success")
        assert wait_for(lambda: len(adds(discovery)) == 3, timeout=20)
        assert_bonjour_unchanged(discovery, dnssd, connections)
    finally:
        foreign.close()
        log = discovery.stop()[1]
    assert "exited with status 7" in log
    assert "native child does not own UDP 137/138/922" in log


def test_wack_then_success_and_workgroup_collision(rig, dnssd):
    discovery = Discovery(rig, name="workgroup", mode="wack-success")
    try:
        assert wait_for(lambda: len(adds(discovery)) == 2)
        assert [decode_name(packet) for packet in adds(discovery)] == [
            ("WORKGROUP", 0), ("WORKGROUP", 0x20)]
        time.sleep(0.35)
        assert len(adds(discovery)) == 2 and discovery.proc.poll() is None
    finally:
        discovery.stop()


def test_exec_child_closes_inherited_non_cloexec_descriptors(rig, dnssd):
    sentinel, write_fd = os.pipe()
    try:
        os.set_inheritable(sentinel, True)
        discovery = Discovery(rig, extra_env={"TC_FAKE_WCIFSND_SENTINEL_FD": str(sentinel)},
                              pass_fds=(sentinel,))
        try:
            assert wait_for(lambda: "FD_CLOSED" in event_lines(discovery.events))
            assert "FD_OPEN" not in event_lines(discovery.events)
        finally:
            discovery.stop()
    finally:
        os.close(sentinel)
        os.close(write_fd)


def test_term_ignoring_child_is_killed_and_reaped_within_bound(rig, dnssd):
    discovery = Discovery(rig, extra_env={"TC_FAKE_WCIFSND_IGNORE_TERM": "1"})
    try:
        assert wait_for(lambda: len(adds(discovery)) == 3)
        child = first_added_child(discovery)
        started = time.monotonic()
        discovery.replace(NAT_OK.replace("family=inet addr=", "family=inet6 addr=::ffff:"))

        def child_is_gone():
            try:
                os.kill(child, 0)
                return False
            except ProcessLookupError:
                return True

        assert wait_for(child_is_gone, timeout=10)
        # A plan poll, WCIFSND_STOP_MS and the kill take about 0.8 s; waiting
        # for a reply timeout instead would take FIRST_REPLY_MS.
        assert time.monotonic() - started < FIRST_REPLY_MS / 1000 - .5
        assert_reaped(child, discovery.proc.pid)
        assert discovery.proc.poll() is None
    finally:
        discovery.stop()


def test_failed_term_ignoring_child_is_reaped_before_retry(rig, dnssd):
    discovery = Discovery(rig, mode='drop', shares=True, extra_env={'TC_FAKE_WCIFSND_IGNORE_TERM': '1'})
    try:
        connections = bonjour_connections(dnssd)
        # The TERM-ignoring child also takes the whole grace to stop.
        assert wait_for(lambda: len(children(discovery)) == 2,
                        timeout=hang_guard(CHILD_START_MS, *FIRST_REPLACEMENT, GRACE_MS))
        assert_reaped(children(discovery)[0], discovery.proc.pid)
        assert_bonjour_unchanged(discovery, dnssd, connections)
        discovery.mode.write_text('success')
        assert wait_for(lambda: 'HUP' in event_lines(discovery.events), timeout=20)
        assert_bonjour_unchanged(discovery, dnssd, connections)
    finally:
        discovery.stop()


def test_active_generation_retains_child_without_hup_on_incomplete_facts(rig, dnssd):
    discovery = Discovery(rig, debug=True)
    try:
        assert wait_for(lambda: len(adds(discovery)) == 3)
        child = first_added_child(discovery)
        incomplete = NAT_OK.replace("key=raNA status=ok value=1", "key=raNA status=abort value=")
        discovery.replace(incomplete)
        discovery.wait_incomplete_passes(1)
        hups = event_lines(discovery.events).count("HUP")
        passes = discovery.read_log().count('registrant: plan incomplete')
        discovery.wait_incomplete_passes(passes + 2)
        lines = event_lines(discovery.events)
        assert children(discovery)[-1] == child
        assert lines.count("HUP") == hups
        assert discovery.proc.poll() is None
    finally:
        discovery.stop()


def test_loss_of_last_eligible_ipv4_stops_and_reenable_starts_fresh_child(rig, dnssd):
    discovery = Discovery(rig)
    no_ipv4 = (NAT_OK.replace("family=inet addr=10.0.1.1", "family=inet6 addr=2001:db8::10")
               .replace("family=inet addr=192.168.1.10", "family=inet6 addr=2001:db8::11")
               .replace("family=inet addr=169.254.140.130", "family=inet6 addr=2001:db8::12"))
    try:
        assert wait_for(lambda: len(adds(discovery)) == 3)
        first = first_added_child(discovery)
        discovery.replace(no_ipv4)
        assert wait_for(lambda: "STOP" in generation(discovery, first))
        assert discovery.proc.poll() is None
        discovery.replace(NAT_OK)
        assert wait_for(lambda: len(adds(discovery)) == 6)
        assert adds_per_child(discovery) == {first: 3, children(discovery)[-1]: 3}
    finally:
        discovery.stop()


def test_no_lease_starts_no_child_until_dhcp_replaces_the_zero_address(rig, dnssd):
    discovery = Discovery(rig, facts=BRIDGE_NO_LEASE)
    try:
        time.sleep(PAST_FIRST_RETRY)
        assert not children(discovery) and not adds(discovery)
        discovery.replace(BRIDGE_LEASED)
        assert wait_for(lambda: len(adds(discovery)) == 3)
        first = first_added_child(discovery)
        discovery.replace(BRIDGE_NO_LEASE)
        assert wait_for(lambda: "STOP" in generation(discovery, first))
        started = len(children(discovery))
        time.sleep(PAST_FIRST_RETRY)
        # No address left to answer on: nothing is retried.
        assert len(children(discovery)) == started and len(adds(discovery)) == 3
        assert discovery.proc.poll() is None
    finally:
        discovery.stop()


def test_a_link_local_address_alone_still_registers(rig, dnssd):
    discovery = Discovery(rig, facts=BRIDGE_LINK_LOCAL_ONLY)
    try:
        assert wait_for(lambda: len(adds(discovery)) == 3)
        assert [decode_name(packet) for packet in adds(discovery)] == [
            ("MACHINE", 0), ("WORKGROUP", 0), ("MACHINE", 0x20)]
    finally:
        discovery.stop()


@pytest.mark.parametrize("linked", [True, False])
def test_child_runs_the_boot_link_and_falls_back_to_sbin_without_it(rig, dnssd, linked):
    primary = rig[0] / "libexec-wcifsnd"
    aside = rig[0] / "libexec-wcifsnd.aside"
    if not linked:
        # boot.sh could not link /usr/libexec/wcifsnd, so it left Apple's
        # /sbin/wcifsnd unguarded.
        primary.rename(aside)
    discovery = Discovery(rig)
    try:
        assert wait_for(lambda: len(adds(discovery)) == 3)
        assert ("FALLBACK" in event_lines(discovery.events)) == (not linked)
    finally:
        discovery.stop()
        if not linked:
            aside.rename(primary)


@pytest.mark.parametrize('afp', [False, True])
def test_repeated_child_death_preserves_bonjour_and_processes_callbacks(rig, dnssd, afp):
    facts = NAT_OK.replace('advertise_afp=0', 'advertise_afp=1') if afp else NAT_OK
    discovery = Discovery(rig, facts=facts, shares=True)
    try:
        expected = 6 if afp else 4
        assert dnssd.wait_for(lambda rows: sum(r['op'] == 'register' for r in rows) == expected, timeout=15)
        connections = bonjour_connections(dnssd)
        for generation in range(1, 4):
            assert wait_for(lambda: len(adds(discovery)) == generation * 3, timeout=20)
            old = children(discovery)[-1]
            os.kill(old, signal.SIGKILL)
            if generation == 1:
                # Callback handling must continue while the native child is
                # absent. Apple can rename its shared default instance then.
                dnssd.rename_default('Renamed Capsule')
            assert wait_for(lambda: len(children(discovery)) == generation + 1, timeout=20)
            assert_reaped(old, discovery.proc.pid)
            assert_bonjour_unchanged(discovery, dnssd, connections)
        assert wait_for(lambda: len(adds(discovery)) == 12)
    finally:
        log = discovery.stop()[1]
    assert 'Renamed Capsule' in log
    assert dnssd.wait_for(lambda rows: sum(r['op'] == 'close' for r in rows) == expected)


@pytest.mark.parametrize('phase', ['registering', 'backoff'])
@pytest.mark.parametrize('action', ['ipv4-loss', 'term', 'parent-eof'])
def test_recovery_can_be_cancelled_without_respawning(cancel_rig, dnssd, phase, action):
    parent_read, parent_write = os.pipe()
    # Parent EOF is tested with a real lifetime pipe on stdin, exactly as the
    # manager launches this role.
    discovery = Discovery(cancel_rig, mode='drop', shares=True, stdin=parent_read if action == 'parent-eof' else None)
    try:
        bonjour_connections(dnssd)
        assert wait_for(lambda: len(adds(discovery)) == 1)
        tried = first_added_child(discovery)
        if phase == 'backoff':
            assert wait_for(lambda: 'STOP' in generation(discovery, tried), timeout=hang_guard(FIRST_REPLY_MS, STOP_MS))
        if action == 'ipv4-loss':
            discovery.replace(NAT_OK.replace("family=inet addr=", "family=inet6 addr=::ffff:"))
            assert wait_for(lambda: 'STOP' in generation(discovery, tried))
            time.sleep(CANCEL_PAST_FIRST_RETRY)  # No new child.
            # Losing IPv4 can also change Bonjour’s eligible interfaces.
            # Recovery must stop without terminating the discovery controller.
            assert discovery.proc.poll() is None
        else:
            if action == 'term':
                discovery.proc.terminate()
            else:
                os.close(parent_write); parent_write = -1
            assert wait_for(lambda: discovery.proc.poll() is not None)
            assert discovery.proc.returncode == 0
        assert children(discovery)[-1] == tried
        assert len(adds(discovery)) == 1
        assert_reaped(tried)
    finally:
        discovery.stop()
        os.close(parent_read)
        if parent_write >= 0:
            os.close(parent_write)


def test_retry_waits_for_valid_facts_without_dropping_bonjour(rig, dnssd):
    discovery = Discovery(rig, shares=True, debug=True)
    try:
        connections = bonjour_connections(dnssd)
        assert wait_for(lambda: len(adds(discovery)) == 3)
        discovery.replace(NAT_OK.replace('key=raNA status=ok value=1', 'key=raNA status=abort value='))
        discovery.wait_incomplete_passes(1)
        old = first_added_child(discovery)
        os.kill(old, signal.SIGKILL)
        time.sleep(PAST_FIRST_RETRY)  # The retry deadline expires, but startup is not eligible.
        assert_reaped(old, discovery.proc.pid)
        assert children(discovery)[-1] == old
        assert_bonjour_unchanged(discovery, dnssd, connections)
        discovery.replace(NAT_OK)
        assert wait_for(lambda: len(adds(discovery)) == 6)
        assert_bonjour_unchanged(discovery, dnssd, connections)
    finally:
        discovery.stop()


@pytest.mark.parametrize("legacy", ["0", "1", "invalid", None])
def test_legacy_nbns_setting_cannot_disable_native_discovery(rig, dnssd, legacy):
    facts = NAT_OK.replace("nbns_enabled=1", "" if legacy is None else f"nbns_enabled={legacy}")
    discovery = Discovery(rig, facts=facts)
    try:
        assert wait_for(lambda: len(adds(discovery)) == 3)
        assert discovery.proc.poll() is None
    finally:
        discovery.stop()
