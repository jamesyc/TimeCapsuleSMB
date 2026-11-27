"""Heartbeat resource health (telemetry/health*.c): detectors, the reader on
host fixtures, the worst-case object, and probe rounds against servers."""
import json
import signal
import socket
import struct
import subprocess
import threading
import time

import pytest

from tests.loopback import accept_loop, fill_listen_queue, has_ipv6_loopback, held_refusing_ports
from tests.native.build import ROOT, compile_modules, header_define

# Probe limits for the test build: long enough that a server thread's
# reaction (well under 100 ms) never races them. Servers that must not
# answer hold their connection until the test releases them, so only the
# product's own limits end those probes.
CONNECT_MS = 2000
ANSWER_MS = 2000
# The TERM test's own build: an answer limit far past the 1 s stop-flag poll.
LONG_ANSWER_MS = 10000
# A hang guard for server threads, never reached when the product works.
HOLD_S = 30


IPV6 = has_ipv6_loopback()


@pytest.fixture(scope="module")
def ports():
    # Held for the module: refused unless a test's server listens on them,
    # and never taken by another worker.
    with held_refusing_ports(2) as (ssh, smb):
        yield {"ssh": ssh, "smb": smb}


def build_health(path, ports, answer_ms):
    native = ROOT / "build/native"
    compile_modules(path, ["native/telemetry/health.c", "native/telemetry/health_read.c",
                           "native/telemetry/health_json.c", "native/telemetry/errclass.c",
                           "native/service/proctable.c", "native/common/acp.c"],
                    flags=("-I", str(native / "telemetry"), "-I", str(native),
                           f"-DTC_HEALTH_SSH_PORT={ports['ssh']}", f"-DTC_HEALTH_SMB_PORT={ports['smb']}",
                           f"-DTC_HEALTH_PROBE_CONNECT_MS={CONNECT_MS}", f"-DTC_HEALTH_PROBE_ANSWER_MS={answer_ms}"),
                    extra_sources=(ROOT / "tests/native/unit/test_health.c",))
    return path


@pytest.fixture(scope="module")
def binary(tmp_path_factory, ports):
    return build_health(tmp_path_factory.mktemp("health") / "health", ports, ANSWER_MS)


@pytest.fixture(scope="module")
def long_binary(tmp_path_factory, ports):
    return build_health(tmp_path_factory.mktemp("health-long") / "health", ports, LONG_ANSWER_MS)


def test_detectors_and_ledger(binary):
    run = subprocess.run([str(binary), "detectors"], capture_output=True, text=True, timeout=10)
    assert run.returncode == 0, run.stderr


def test_worst_case_health_fits_its_budget_and_escapes_names(binary):
    run = subprocess.run([str(binary), "worst"], capture_output=True, text=True, timeout=10)
    assert run.returncode == 0, run.stderr
    size, text = run.stdout.split(" ", 1)
    assert int(size) <= header_define("telemetry/health.h", "TC_HEALTH_JSON_MAX")
    health = json.loads(text)["health"]
    # Process and wait names are kernel text: quotes and backslashes become ?.
    assert health["procs"]["groups"][0][0] == "?" * 16 and health["procs"]["waits"][0][0] == "?" * 8
    assert health["kmsg"]["proc_full"] == {"first": 2147483647, "last": 2147483647, "n": 4294967295,
                                           "at_start": True}
    assert health["probe_rounds_skipped"] == 4294967295 and health["acp_budget_exhausted"] == 4294967295
    assert health["acp_timeouts"] == health["acp_empty"] == 4294967295
    assert health["fork_failures"] == {"eagain": 4294967295, "enomem": 4294967295, "other": 4294967295}
    assert set(health["socket_failures"].values()) == {4294967295}
    assert len(health["news"]) == 11 and len(health["triggers"]) == 11 and "acp_empty" in health["news"]
    assert health["probes"]["ssh4"]["last"] == "timeout"
    assert {"refused", "timeout", "down"} <= set(health["probes"]["ssh4"])


def sample(binary, tmp_path, table, counters, kmsg=None):
    procs = tmp_path / "procs"
    procs.mkdir(exist_ok=True)
    (procs / "table").write_text(table)
    (tmp_path / "counters").write_text(counters)
    env = {"TC_TEST_PROCS": str(procs), "TC_TEST_HEALTH": str(tmp_path / "counters")}
    if kmsg is not None:
        (tmp_path / "kmsg").write_text(kmsg)
        env["TC_TEST_KMSG"] = str(tmp_path / "kmsg")
    run = subprocess.run([str(binary), "sample"], capture_output=True, text=True, timeout=10, env=env)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)["health"]


# PID PPID GROUP STAT FLAG SLEPT CPU_US WMESG COMM [rss=N]; STAT 3 sleeping,
# 5 zombie; FLAG 0x80 interruptible, 0x200 a kernel thread.
TABLE = "\n".join([
    "1 0 1 3 0x80 0 0 wait init",
    "2 0 0 3 0x200 0 0 - pagedaemon",
    "50 1 50 3 0x80 0 0 kqread ACPd rss=900",
    "60 50 60 3 0x80 0 0 select sshd",
    "61 60 61 3 0x80 0 0 select sshd",
    "70 1 70 3 0x80 0 0 select service rss=300",
    "80 70 80 3 0x80 0 0 poll smbd rss=4000",
    "81 80 80 3 0x80 0 0 netio smbd rss=2500",
    "82 80 80 3 0x80 0 0 netio smbd",
    "83 80 80 3 0 0 0 biowait smbd",
    "90 70 90 5 0 0 0 - zombie",
    "",
])


def test_sample_reads_the_table_counters_and_kernel_messages(binary, tmp_path):
    health = sample(binary, tmp_path, TABLE,
                    "maxproc 90\nmaxfiles 1000\nfiles 120\nfree 500\npagesize 4096\nmbuf_drops 0\n"
                    "fds.80 40\nfds.50 25\nfds.60 5\n", kmsg="boot\n")
    procs = health["procs"]
    assert procs["n"] == 11 and procs["system"] == 1 and procs["zombie"] == 1
    # 83 waits uninterruptibly on the disk.
    assert procs["blocked"] == 1
    # smbd's listener is 80, whoever started it; its three children count.
    assert procs["smbd_children"] == 3
    assert ["smbd", 3, 3] in procs["groups"] and ["sshd", 3, 1] in procs["groups"]
    assert procs["largest_kb"][:2] == [["smbd", 16000], ["smbd", 10000]]
    assert procs["most_fds"] == [["smbd", 40], ["ACPd", 25], ["sshd", 5]]
    assert health["maxproc"] == 90 and health["files"] == {"n": 120, "max": 1000, "peak": 120}
    assert health["mem"]["free"] == 500 and health["mem"]["page_kb"] == 4
    assert health["kmsg"]["bytes"] == 5 and health["kmsg"]["proc_full"] is None


def test_processes_with_no_descriptors_are_not_among_the_most(binary, tmp_path):
    # The kernel answers an exited process's descriptor query with only its
    # padding, as for a process with none: neither fills a most_fds slot.
    health = sample(binary, tmp_path, TABLE, "maxproc 90\nfds.80 40\nfds.60 0\n")
    assert health["procs"]["most_fds"] == [["smbd", 40]]


def test_sample_without_smbd_or_a_readable_kernel_buffer(binary, tmp_path):
    table = "1 0 1 3 0x80 0 0 wait init\n50 1 50 3 0x80 0 0 kqread ACPd\n"
    health = sample(binary, tmp_path, table, "maxproc 90\n")
    assert health["procs"]["smbd_children"] is None
    assert health["kmsg"]["bytes"] == -1 and health["mem"] is None and health["files"]["n"] is None


def test_messages_present_at_the_first_read_are_marked_not_triggers(binary, tmp_path):
    health = sample(binary, tmp_path, TABLE, "maxproc 90\n", kmsg="old\nproc: table is full\n")
    assert health["kmsg"]["proc_full"] == {"first": None, "last": None, "n": 0, "at_start": True}
    assert health["news"] == [] and health["triggers"] == []


class ProbeServer:
    """Listens on a loopback port; the Nth connection gets behaviors[N]:
    banner (an SSH banner), close (EOF), reset (RST), silent (nothing, the
    connection held until close()), smb (checks the SMB2 NEGOTIATE and
    answers it), smb_partial (four bytes of an answer, then EOF), garbage
    (an answer that is not SMB2), or banner_hold (a banner, then the
    probe's line read and the connection held, setting answered). After banner and
    smb it reads what the probe sends until the probe's EOF and records it
    with how the connection ended ("eof" or "reset") in endings. A
    connection past the list is held silent, as one left unaccepted looks to
    the probe. Tracks how many connections were open at once."""

    def __init__(self, family, port, behaviors):
        self.sock = socket.socket(family, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1" if family == socket.AF_INET else "::1", port))
        self.sock.listen(8)
        self.behaviors = list(behaviors)
        self.requests = []
        self.endings = []
        self.release = threading.Event()
        self.answered = threading.Event()
        self.open = self.most_open = 0
        self.lock = threading.Lock()
        self.stop = accept_loop(self.sock, self.next_connection)

    def next_connection(self, conn):
        with self.lock:
            behavior = self.behaviors.pop(0) if self.behaviors else "silent"
        self.handle(conn, behavior)

    def read_to_end(self, conn):
        conn.settimeout(HOLD_S)
        data = b""
        try:
            while chunk := conn.recv(4096):
                data += chunk
            ending = "eof"
        except ConnectionResetError:
            ending = "reset"
        except OSError as error:
            ending = repr(error)
        self.endings.append((data, ending))

    def handle(self, conn, behavior):
        with self.lock:
            self.open += 1
            self.most_open = max(self.most_open, self.open)
        try:
            if behavior == "banner":
                conn.sendall(b"SSH-2.0-OpenSSH_test\r\n")
                self.read_to_end(conn)
            elif behavior == "banner_hold":
                conn.sendall(b"SSH-2.0-OpenSSH_test\r\n")
                conn.settimeout(HOLD_S)
                line = b""
                while not line.endswith(b"\n") and (chunk := conn.recv(1)):
                    line += chunk
                self.answered.set()  # the probe has its answer and is saying goodbye
                self.release.wait(HOLD_S)
            elif behavior in ("smb", "garbage", "smb_partial"):
                conn.settimeout(ANSWER_MS / 1000)
                request = b""
                try:
                    while len(request) < 112:
                        chunk = conn.recv(4096)
                        if not chunk:
                            break
                        request += chunk
                except OSError:
                    pass
                self.requests.append(request)
                if behavior == "garbage":
                    conn.sendall(b"HTTP/1.0 400 Bad\r\n\r\n")
                    self.release.wait(HOLD_S)
                elif behavior == "smb_partial":
                    conn.sendall(b"\x00\x00\x00\xc0")
                else:
                    # A negotiate reply's length: more than the probe reads.
                    conn.sendall(b"\x00\x00\x00\xc0\xfeSMB" + bytes(188))
                    self.read_to_end(conn)
            elif behavior == "reset":
                conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            elif behavior == "silent":
                self.release.wait(HOLD_S)
        finally:
            conn.close()
            with self.lock:
                self.open -= 1

    def close(self):
        self.release.set()
        self.stop()


def table_env(directory, procs, maxproc=90):
    """The reader's fixtures for a table of procs sleeping processes under
    kern.maxproc: the IPv6 smbd probe runs only when the table, read after
    the IPv4 pair, has room for it."""
    directory.mkdir(exist_ok=True)
    (directory / "procs").mkdir(exist_ok=True)
    (directory / "procs" / "table").write_text(
        "".join(f"{100 + i} 1 {100 + i} 3 0x80 0 0 select p{i}\n" for i in range(procs)))
    (directory / "counters").write_text(f"maxproc {maxproc}\n")
    return {"TC_TEST_PROCS": str(directory / "procs"), "TC_TEST_HEALTH": str(directory / "counters")}


@pytest.fixture(scope="module")
def room(tmp_path_factory):
    return table_env(tmp_path_factory.mktemp("room"), 11)


def probe_rounds(binary, env, rounds=1):
    run = subprocess.run([str(binary), "probes", str(rounds)], capture_output=True, text=True, timeout=60, env=env)
    assert run.returncode == 0, run.stderr
    results = []
    for line in run.stdout.splitlines():
        fields = dict(item.split("=") for item in line.split())
        results.append(fields)
    return results


def servers(ports, ssh4, smb4, smb6=()):
    """sshd and smbd over IPv4, and on a host with ::1, smbd over IPv6 and an
    sshd there that a round must never reach (made[2]): sshd logs every
    probe, so it is probed over IPv4 only (health.h)."""
    made = [ProbeServer(socket.AF_INET, ports["ssh"], ssh4), ProbeServer(socket.AF_INET, ports["smb"], smb4)]
    if IPV6:
        made += [ProbeServer(socket.AF_INET6, ports["ssh"], ["banner"]), ProbeServer(socket.AF_INET6, ports["smb"], smb6)]
    return made


def assert_no_ipv6_ssh_probe(made, result):
    assert "ssh6" not in result
    if IPV6:
        assert made[2].most_open == 0 and made[2].behaviors == ["banner"]  # never connected


def test_a_probe_round_reads_each_answer(binary, ports, room):
    made = servers(ports, ["banner"], ["smb"], ["garbage"])
    try:
        [result] = probe_rounds(binary, room)
    finally:
        for server in made:
            server.close()
    assert result["ssh4"] == "ok" and result["smb4"] == "ok"
    # The probe sent the 108-byte NEGOTIATE behind its session header.
    request = made[1].requests[0]
    assert len(request) == 112 and request[:8] == b"\x00\x00\x00\x6c\xfeSMB"
    # Once answered, the probe says goodbye: sshd gets one line naming the
    # probe that is not an SSH identification (no "SSH-" prefix, so sshd
    # refuses it before forking a pre-auth child), both get EOF, and the
    # probe reads the rest of the reply instead of resetting the connection
    # with it unread.
    [(line, ending)] = made[0].endings
    assert line == b"TimeCapsuleSMB health probe\r\n" and not line.startswith(b"SSH-") and ending == "eof"
    assert made[1].endings == [(b"", "eof")]
    if IPV6:
        assert result["smb6"] == "silent"
    else:
        # No ::1 here: the probe cannot run, which says nothing about smbd.
        assert result["smb6"] == "none"
    assert_no_ipv6_ssh_probe(made, result)


@pytest.mark.skipif(not IPV6, reason="no IPv6 loopback")
def test_ipv6_probes_read_answers_like_ipv4(binary, ports, room):
    made = servers(ports, ["close"], ["smb_partial"], ["smb"])
    try:
        [result] = probe_rounds(binary, room)
    finally:
        for server in made:
            server.close()
    assert result["smb6"] == "ok"
    # Part of an SMB2 answer and then EOF is not an answer: silent.
    assert result["ssh4"] == "closed" and result["smb4"] == "silent"
    # An sshd listening on ::1 too, answering, is still not asked there.
    assert_no_ipv6_ssh_probe(made, result)


@pytest.mark.parametrize("family, service", [(socket.AF_INET, "ssh"), (socket.AF_INET, "smb"),
                                             (socket.AF_INET6, "smb")])
def test_refused_and_reset_connections(binary, ports, family, service, room):
    # One service resets; nothing else of that family listens.
    if family == socket.AF_INET6 and not IPV6:
        pytest.skip("no IPv6 loopback")
    made = [ProbeServer(family, ports[service], ["reset"])]
    try:
        [result] = probe_rounds(binary, room)
    finally:
        for server in made:
            server.close()
    v = "4" if family == socket.AF_INET else "6"
    assert result[f"{service}{v}"] == "closed"
    assert all(result[name] == "absent" for name in ("ssh4", "smb4", "smb6")
               if name.endswith(v) and name != f"{service}{v}")


def fill_backlog(family, port):
    """A listener at port that never accepts, its queue full
    (tests/loopback.py). Returns every socket to close."""
    listener = socket.socket(family, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1" if family == socket.AF_INET else "::1", port))
    return [listener, *fill_listen_queue(listener)]


@pytest.mark.parametrize("call", ["probe-send", "probe-recv"])
def test_a_buffer_shortage_after_connecting_skips_the_probe(binary, ports, room, call):
    # A send or receive that fails for want of mbufs says nothing about the
    # service: skipped (none) and counted as a socket failure, so two such
    # rounds from targets that answered before trigger nothing, where two
    # closed would.
    made = servers(ports, ["banner", "banner"], ["smb", "smb"], ["smb", "smb"])
    try:
        rounds = probe_rounds(binary, {**room, "TC_TEST_ENOBUFS": call}, rounds=2)
    finally:
        for server in made:
            server.close()
    smb = ["smb4", "smb6"] if IPV6 else ["smb4"]
    # The sshd probe sends nothing before its answer, so only a receive
    # shortage reaches it.
    skipped = smb + (["ssh4"] if call == "probe-recv" else [])
    for index, result in enumerate(rounds):
        assert all(result[name] == "none" for name in skipped), result
        assert int(result["enobufs"]) == len(skipped) * (index + 1)
        assert result["probe_event"] == "0"
    if call == "probe-send":
        assert all(result["ssh4"] == "ok" for result in rounds)


@pytest.mark.parametrize("family", [socket.AF_INET, socket.AF_INET6])
def test_a_full_listen_queue_times_out_unlike_a_refusal(binary, ports, family, room):
    # smbd's parent stops accepting: its queue fills and the kernel drops
    # new SYNs, so the probe's connect never completes. That is a timeout,
    # after the connect limit; nothing listening is an immediate refusal.
    if family == socket.AF_INET6 and not IPV6:
        pytest.skip("no IPv6 loopback")
    made = [ProbeServer(socket.AF_INET, ports["ssh"], ["banner"])]
    full = fill_backlog(family, ports["smb"])
    try:
        [result] = probe_rounds(binary, room)
    finally:
        for server in made:
            server.close()
        for sock in full:
            sock.close()
    v, other = ("4", "6") if family == socket.AF_INET else ("6", "4")
    assert result["ssh4"] == "ok" and result[f"smb{v}"] == "timeout"
    elapsed = int(result["elapsed_ms"])
    assert CONNECT_MS - 100 <= elapsed < CONNECT_MS + 1500
    if IPV6 or other == "4":
        assert result[f"smb{other}"] == "absent"


def test_a_round_runs_one_probe_per_service_at_a_time(binary, ports, room):
    # Every server silent: IPv4's pair waits out the answer limit together,
    # then IPv6's smbd probe, so a round takes two waits, not three, and no
    # port ever sees two probes at once.
    made = servers(ports, ["silent"], ["silent"], ["silent"])
    try:
        [result] = probe_rounds(binary, room)
    finally:
        for server in made:
            server.close()
    assert result["smb4"] == "silent" and result["ssh4"] == "silent"
    pairs = 2 if IPV6 else 1
    elapsed = int(result["elapsed_ms"])
    assert pairs * ANSWER_MS - 100 <= elapsed < pairs * ANSWER_MS + 1500
    assert all(server.most_open == 1 for i, server in enumerate(made) if i != 2)
    assert_no_ipv6_ssh_probe(made, result)


def test_term_skips_the_rest_of_a_round(long_binary, ports, room):
    made = servers(ports, ["silent"], ["silent"], ["silent"])
    try:
        process = subprocess.Popen([str(long_binary), "probes", "1"], stdout=subprocess.PIPE, text=True, env=room)
        deadline = time.monotonic() + 5
        while made[0].open == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert made[0].open, "the probe never connected"
        started = time.monotonic()
        process.send_signal(signal.SIGTERM)
        out, _ = process.communicate(timeout=60)
    finally:
        for server in made:
            server.close()
    # The stop flag is checked every second; without it the round would
    # wait out two 10 s answer limits.
    assert time.monotonic() - started < LONG_ANSWER_MS / 1250
    assert process.returncode == 0
    fields = dict(item.split("=") for item in out.split())
    assert fields["ssh4"] == fields["smb4"] == fields["smb6"] == "none"
    assert_no_ipv6_ssh_probe(made, fields)


def test_term_keeps_an_answer_already_in(long_binary, ports, room):
    # sshd answered and the probe is saying goodbye when TERM comes: its
    # result is in and stays ok; smbd, still silent, is skipped.
    made = [ProbeServer(socket.AF_INET, ports["ssh"], ["banner_hold"]),
            ProbeServer(socket.AF_INET, ports["smb"], ["silent"])]
    try:
        process = subprocess.Popen([str(long_binary), "probes", "1"], stdout=subprocess.PIPE, text=True, env=room)
        assert made[0].answered.wait(10), "the probe never answered"
        process.send_signal(signal.SIGTERM)
        out, _ = process.communicate(timeout=60)
    finally:
        for server in made:
            server.close()
    assert process.returncode == 0
    fields = dict(item.split("=") for item in out.split())
    assert fields["ssh4"] == "ok" and fields["smb4"] == "none"


def test_an_smbd_that_does_not_answer_negotiate_is_silent_not_ok(binary, ports, room):
    # A listener that accepts but answers something other than SMB2 (or a
    # parent that never hands the connection to a child) is not a working
    # smbd: silent, not ok.
    made = [ProbeServer(socket.AF_INET, ports["ssh"], ["banner"]),
            ProbeServer(socket.AF_INET, ports["smb"], ["garbage"])]
    try:
        [result] = probe_rounds(binary, room)
    finally:
        for server in made:
            server.close()
    assert result["ssh4"] == "ok" and result["smb4"] == "silent"


@pytest.mark.skipif(not IPV6, reason="no IPv6 loopback")
@pytest.mark.parametrize("procs, runs", [(87, True), (88, False)])
def test_the_ipv6_probe_runs_only_with_room_beside_the_ipv4_pairs_children(binary, ports, tmp_path, procs, runs):
    # tc_health_probe_allowed's rule (procs + 2 < maxproc), asked again of
    # the table after the IPv4 pair: whose sshd and smbd children may still
    # hold their slots. 88 of 90 leaves no room, and the IPv6 probe is
    # skipped without connecting.
    made = servers(ports, ["banner"], ["smb"], ["smb"])
    try:
        [result] = probe_rounds(binary, table_env(tmp_path, procs))
    finally:
        for server in made:
            server.close()
    assert result["ssh4"] == "ok" and result["smb4"] == "ok"
    if runs:
        assert result["smb6"] == "ok"
    else:
        assert result["smb6"] == "none"
        assert made[3].behaviors == ["smb"]  # never connected
    assert_no_ipv6_ssh_probe(made, result)


@pytest.mark.skipif(not IPV6, reason="no IPv6 loopback")
@pytest.mark.parametrize("unreadable", ["counters", "table"])
def test_an_unreadable_table_skips_the_ipv6_probe(binary, ports, tmp_path, unreadable):
    """No kern.maxproc, or a process table that cannot be read: the reader
    cannot tell the IPv6 probe has room, so it skips it after the IPv4
    pair."""
    env = table_env(tmp_path, 11)
    if unreadable == "counters":
        (tmp_path / "counters").unlink()
    else:
        del env["TC_TEST_PROCS"]  # the fixture's table read then fails
    made = servers(ports, ["banner"], ["smb"], ["smb"])
    try:
        [result] = probe_rounds(binary, env)
    finally:
        for server in made:
            server.close()
    assert result["ssh4"] == "ok" and result["smb6"] == "none" and made[3].behaviors == ["smb"]
