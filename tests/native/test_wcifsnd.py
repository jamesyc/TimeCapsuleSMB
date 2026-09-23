"""Behavioral regression tests for the Apple wcifsnd controller and codec."""
from __future__ import annotations

import os
import signal
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from unittest import mock

import pytest

from tests.native.build import ROOT, compile_modules, compile_service
from tests.native.integration.fake_dnssd_daemon import FakeDnssdDaemon
from tests.native.test_plan import MODE, NAT_ADDRS, NAT_LINKS, facts_text


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

WCIFSND_UNIT_MODULES = (
    "native/common/acp.c",
    "native/common/addr.c",
    "native/common/log.c",
    "native/common/process.c",
)


@pytest.fixture(scope="module")
def rig():
    root = Path(tempfile.mkdtemp(prefix="tcwcifsnd-"))
    port = free_port()
    fake = ROOT / "tests/native/integration/fake_wcifsnd.py"
    fake.chmod(0o755)
    fake_fstat = ROOT / "tests/native/integration/fake_fstat.py"
    fake_fstat.chmod(0o755)
    sock = root / "mDNSResponder"
    binary = compile_service(root / "service", flags=[
        f'-DMDNS_UDS_SERVERPATH="{sock}"', f'-DWCIFSND_PATH="{fake}"',
        f'-DTC_FSTAT_PATH="{fake_fstat}"',
        f"-DWCIFSND_PORT={port}", "-DWCIFSND_START_MS=3000", "-DWCIFSND_INSPECT_MS=3000",
        "-DTC_CHILD_GRACE_MS=500", "-DWCIFSND_REPLY_MS=1500",
        "-DWCIFSND_STOP_MS=500", "-DTC_PLAN_POLL_MS=200", "-DREG_BACKOFF_MIN_MS=100",
        "-DREG_BACKOFF_MAX_MS=200", "-DREG_IPC_ALARM_SECONDS=1", "-D_DNS_SD_LIBDISPATCH=0"])
    return root, sock, binary, port


class Discovery:
    def __init__(self, rig, facts=NAT_OK, name="machine", diskless=False, mode="success",
                 extra_env=None, pass_fds=(), shares=False, stdin=None, fstat_mode="owned"):
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
        self.proc = subprocess.Popen(args, env=env, pass_fds=pass_fds,
                                     stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def replace(self, facts):
        temporary = self.facts.with_suffix(".tmp")
        temporary.write_text(facts)
        os.replace(temporary, self.facts)

    def stop(self, timeout=3):
        if self.proc.poll() is None:
            self.proc.terminate()
        try:
            return self.proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            return self.proc.communicate(timeout=timeout)


@pytest.fixture
def dnssd(rig):
    fake = FakeDnssdDaemon(str(rig[1]))
    yield fake
    fake.close()


def adds(discovery):
    return [bytes.fromhex(line[4:]) for line in event_lines(discovery.events) if line.startswith("ADD ")]


def children(discovery):
    return [int(line.split()[1]) for line in event_lines(discovery.events) if line.startswith("START ")]


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
    assert lines[3:] == ["success=1", "stale=0", "malformed=-1", "negative=-1", "wack=0", "unrelated=1"]


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
                            capture_output=True, text=True, timeout=2)
    assert result.returncode == 3


@pytest.mark.parametrize('mode,requests', [('drop', 1), ('drop-after-1', 2), ('drop-after-2', 3),
                                         ('negative', 1), ('malformed', 1), ('wack', 1), ('no-listener', 0)])
def test_failed_child_retries_without_withdrawing_bonjour(rig, dnssd, mode, requests):
    discovery = Discovery(rig, mode=mode, shares=True)
    try:
        connections = bonjour_connections(dnssd)
        assert wait_for(lambda: len(children(discovery)) >= 2, timeout=20)
        first = children(discovery)[0]
        assert_reaped(first, discovery.proc.pid)
        # Apple keeps accepted adds even after client exit. Every uncertain
        # request is sent once, then the native child is discarded before retry.
        first_events = event_lines(discovery.events)
        second_start = next(i for i, line in enumerate(first_events) if line.startswith('START ') and int(line.split()[1]) != first)
        assert sum(line.startswith('ADD ') for line in first_events[:second_start]) == requests
        assert 'STOP' in first_events[:second_start]
        assert_bonjour_unchanged(discovery, dnssd, connections)
        discovery.mode.write_text('success')
        assert wait_for(lambda: any(line == 'HUP' for line in event_lines(discovery.events)), timeout=20)
        assert_bonjour_unchanged(discovery, dnssd, connections)
    finally:
        discovery.stop()


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
        assert wait_for(lambda: len(adds(discovery)) == 3, timeout=15)
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
        time.sleep(2.5)
        assert len(children(discovery)) == 1
        assert not adds(discovery)
        assert discovery.proc.poll() is None
    finally:
        discovery.stop()


def test_native_child_exit_status_and_port_collision_recover(rig, dnssd):
    port = rig[3]
    foreign = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    foreign.bind(("127.0.0.1", port))
    discovery = Discovery(rig, mode="exit-7", shares=True)
    try:
        connections = bonjour_connections(dnssd)
        assert wait_for(lambda: len(children(discovery)) >= 2, timeout=12)
        assert not adds(discovery)
        discovery.mode.write_text("success")
        assert wait_for(lambda: "BIND_FAILED" in event_lines(discovery.events), timeout=15)
        assert not adds(discovery)
        foreign.close()
        assert wait_for(lambda: len(adds(discovery)) == 3, timeout=20)
        assert_bonjour_unchanged(discovery, dnssd, connections)
    finally:
        foreign.close()
        log = discovery.stop()[1]
    assert "exited with status 7" in log
    assert "exited with status 1" in log


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
        child = int(next(line.split()[1] for line in event_lines(discovery.events)
                         if line.startswith("START ")))
        started = time.monotonic()
        discovery.replace(NAT_OK.replace("family=inet addr=", "family=inet6 addr=::ffff:"))

        def child_is_gone():
            try:
                os.kill(child, 0)
                return False
            except ProcessLookupError:
                return True

        assert wait_for(child_is_gone, timeout=2)
        assert time.monotonic() - started < 2
        assert discovery.proc.poll() is None
    finally:
        discovery.stop()


def test_failed_term_ignoring_child_is_reaped_before_retry(rig, dnssd):
    discovery = Discovery(rig, mode='drop', shares=True, extra_env={'TC_FAKE_WCIFSND_IGNORE_TERM': '1'})
    try:
        connections = bonjour_connections(dnssd)
        assert wait_for(lambda: len(children(discovery)) == 2, timeout=15)
        assert_reaped(children(discovery)[0], discovery.proc.pid)
        assert_bonjour_unchanged(discovery, dnssd, connections)
        discovery.mode.write_text('success')
        assert wait_for(lambda: 'HUP' in event_lines(discovery.events), timeout=20)
        assert_bonjour_unchanged(discovery, dnssd, connections)
    finally:
        discovery.stop()


def test_active_generation_retains_child_without_hup_on_incomplete_facts(rig, dnssd):
    discovery = Discovery(rig)
    try:
        assert wait_for(lambda: len(adds(discovery)) == 3)
        child = next(line for line in event_lines(discovery.events) if line.startswith("START "))
        incomplete = NAT_OK.replace("key=raNA status=ok value=1", "key=raNA status=abort value=")
        discovery.replace(incomplete)
        time.sleep(0.5)  # consume a collection already scheduled from the prior valid snapshot
        hups = event_lines(discovery.events).count("HUP")
        time.sleep(0.5)
        lines = event_lines(discovery.events)
        assert [line for line in lines if line.startswith("START ")] == [child]
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
        first = next(line for line in event_lines(discovery.events) if line.startswith("START "))
        discovery.replace(no_ipv4)
        assert wait_for(lambda: "STOP" in event_lines(discovery.events))
        assert discovery.proc.poll() is None
        discovery.replace(NAT_OK)
        assert wait_for(lambda: sum(line.startswith("START ") for line in event_lines(discovery.events)) == 2)
        starts = [line for line in event_lines(discovery.events) if line.startswith("START ")]
        assert starts[0] == first and starts[1] != first
        assert wait_for(lambda: len(adds(discovery)) == 6)
    finally:
        discovery.stop()


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
def test_recovery_can_be_cancelled_without_respawning(rig, dnssd, phase, action):
    parent_read, parent_write = os.pipe()
    # Parent EOF is tested with a real lifetime pipe on stdin, exactly as the
    # manager launches this role.
    discovery = Discovery(rig, mode='drop', shares=True, stdin=parent_read if action == 'parent-eof' else None)
    try:
        bonjour_connections(dnssd)
        assert wait_for(lambda: len(adds(discovery)) == 1)
        if phase == 'backoff':
            assert wait_for(lambda: 'STOP' in event_lines(discovery.events))
        if action == 'ipv4-loss':
            discovery.replace(NAT_OK.replace("family=inet addr=", "family=inet6 addr=::ffff:"))
            assert wait_for(lambda: 'STOP' in event_lines(discovery.events))
            time.sleep(2.5)  # Beyond the first retry deadline; no new child.
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
        assert len(children(discovery)) == 1
        assert_reaped(children(discovery)[0])
    finally:
        discovery.stop()
        os.close(parent_read)
        if parent_write >= 0:
            os.close(parent_write)


def test_retry_waits_for_valid_facts_without_dropping_bonjour(rig, dnssd):
    discovery = Discovery(rig, shares=True)
    try:
        connections = bonjour_connections(dnssd)
        assert wait_for(lambda: len(adds(discovery)) == 3)
        discovery.replace(NAT_OK.replace('key=raNA status=ok value=1', 'key=raNA status=abort value='))
        time.sleep(.6)  # Consume the new incomplete snapshot before faulting.
        old = children(discovery)[0]
        os.kill(old, signal.SIGKILL)
        time.sleep(3)  # The retry deadline expires, but startup is not eligible.
        assert_reaped(old, discovery.proc.pid)
        assert children(discovery) == [old]
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
