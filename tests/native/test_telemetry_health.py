"""Heartbeat resource health (telemetry/health*.c): detectors, the reader on
host fixtures, the worst-case object, and probe rounds against servers."""
import json
import re
import signal
import socket
import struct
import select
import subprocess
import threading
import time

import pytest

from tests.loopback import accept_loop, has_ipv6_loopback, held_refusing_ports
from tests.native.build import ROOT, compile_modules

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
    limit = int(re.search(r"#define TC_HEALTH_JSON_MAX (\d+)", (ROOT / "build/native/telemetry/health.h").read_text())[1])
    assert int(size) <= limit
    health = json.loads(text)["health"]
    # Process and wait names are kernel text: quotes and backslashes become ?.
    assert health["procs"]["groups"][0][0] == "?" * 16 and health["procs"]["waits"][0][0] == "?" * 8
    assert health["kmsg"]["proc_full"] == {"first": 2147483647, "last": 2147483647, "n": 4294967295,
                                           "at_start": True}
    assert "probe_rounds_skipped" in health and health["acp_budget_exhausted"] == 4294967295
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
    answers it), or garbage (an answer that is not SMB2). After banner and
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
            elif behavior in ("smb", "garbage"):
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


def probe_rounds(binary, rounds=1):
    run = subprocess.run([str(binary), "probes", str(rounds)], capture_output=True, text=True, timeout=60)
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


def test_a_probe_round_reads_each_answer(binary, ports):
    made = servers(ports, ["banner"], ["smb"], ["garbage"])
    try:
        [result] = probe_rounds(binary)
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


def test_refused_and_reset_connections(binary, ports):
    # Nothing listens on IPv4 smb; ssh4 resets.
    made = [ProbeServer(socket.AF_INET, ports["ssh"], ["reset"])]
    try:
        [result] = probe_rounds(binary)
    finally:
        for server in made:
            server.close()
    assert result["ssh4"] == "closed" and result["smb4"] == "absent"


def fill_backlog(family, port):
    """A listener that never accepts, its queue filled by connections of our
    own until the kernel drops a new one's SYN (as it does for any client
    of a daemon that stopped accepting). Returns every socket to close."""
    listener = socket.socket(family, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    host = "127.0.0.1" if family == socket.AF_INET else "::1"
    listener.bind((host, port))
    listener.listen(1)
    made = [listener]
    for _ in range(64):
        client = socket.socket(family, socket.SOCK_STREAM)
        client.setblocking(False)
        made.append(client)
        client.connect_ex((host, port))
        # Loopback connects complete at once; only a full queue waits.
        _, writable, _ = select.select([], [client], [], 1.5)
        if not writable:
            return made
    for sock in made:
        sock.close()
    pytest.fail("the listen queue never filled")


def test_a_full_listen_queue_times_out_unlike_a_refusal(binary, ports):
    # smbd's parent stops accepting: its queue fills and the kernel drops
    # new SYNs, so the probe's connect never completes. That is a timeout,
    # after the connect limit; nothing listening is an immediate refusal.
    made = [ProbeServer(socket.AF_INET, ports["ssh"], ["banner"])]
    full = fill_backlog(socket.AF_INET, ports["smb"])
    try:
        [result] = probe_rounds(binary)
    finally:
        for server in made:
            server.close()
        for sock in full:
            sock.close()
    assert result["ssh4"] == "ok" and result["smb4"] == "timeout"
    elapsed = int(result["elapsed_ms"])
    assert CONNECT_MS - 100 <= elapsed < CONNECT_MS + 1500
    if IPV6:
        assert result["smb6"] == "absent"


def test_a_round_runs_one_probe_per_service_at_a_time(binary, ports):
    # Every server silent: IPv4's pair waits out the answer limit together,
    # then IPv6's smbd probe, so a round takes two waits, not three, and no
    # port ever sees two probes at once.
    made = servers(ports, ["silent"], ["silent"], ["silent"])
    try:
        [result] = probe_rounds(binary)
    finally:
        for server in made:
            server.close()
    assert result["smb4"] == "silent" and result["ssh4"] == "silent"
    pairs = 2 if IPV6 else 1
    elapsed = int(result["elapsed_ms"])
    assert pairs * ANSWER_MS - 100 <= elapsed < pairs * ANSWER_MS + 1500
    assert all(server.most_open == 1 for i, server in enumerate(made) if i != 2)
    assert_no_ipv6_ssh_probe(made, result)


def test_term_skips_the_rest_of_a_round(long_binary, ports):
    made = servers(ports, ["silent"], ["silent"], ["silent"])
    try:
        process = subprocess.Popen([str(long_binary), "probes", "1"], stdout=subprocess.PIPE, text=True)
        deadline = time.monotonic() + 5
        while made[0].open == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        started = time.monotonic()
        process.send_signal(signal.SIGTERM)
        out, _ = process.communicate(timeout=60)
    finally:
        for server in made:
            server.close()
    # The stop flag is checked every second; without it the round would
    # wait out two 10 s answer limits.
    assert time.monotonic() - started < LONG_ANSWER_MS / 1250
    fields = dict(item.split("=") for item in out.split())
    assert fields["ssh4"] == fields["smb4"] == fields["smb6"] == "none"
    assert_no_ipv6_ssh_probe(made, fields)


def test_an_smbd_that_does_not_answer_negotiate_is_silent_not_ok(binary, ports):
    # A listener that accepts but answers something other than SMB2 (or a
    # parent that never hands the connection to a child) is not a working
    # smbd: silent, not ok.
    made = [ProbeServer(socket.AF_INET, ports["ssh"], ["banner"]),
            ProbeServer(socket.AF_INET, ports["smb"], ["garbage"])]
    try:
        [result] = probe_rounds(binary)
    finally:
        for server in made:
            server.close()
    assert result["ssh4"] == "ok" and result["smb4"] == "silent"
