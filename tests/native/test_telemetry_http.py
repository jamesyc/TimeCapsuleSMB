"""telemetry/http.c and dns.c: the fork-free heartbeat client, against real
HTTP and DNS servers on loopback (both families)."""
import contextlib
import os
import re
import select
import socket
import socketserver
import struct
import subprocess
import threading
import time

import pytest

from tests.loopback import accept_loop, fill_listen_queue, has_ipv6_loopback, held_refusing_ports
from tests.native.build import ROOT, compile_modules, header_define

DEADLINE_MS = 3000
# The connect limit of the builds that try two addresses (wide_binary): far
# enough below ADDRESS_MS that a test can tell the two apart.
CONNECT_MS = 1000
# The connect limit of the default build: a refused connect must end well
# before it, with room for the Python fakes, and it is under DEADLINE_MS so
# waiting it out would still leave the request time to fail.
PROMPT_CONNECT_MS = 2500
# Each address but the last; the last has what is left of the deadline. Far
# enough past CONNECT_MS and a prompt fallback that a test can bound its
# time just below this.
ADDRESS_MS = 3000
# The resolver's first-round time per nameserver in every build (dns.h,
# TC_DNS_RETRANS_MS): a fake that answers at once is far inside it, and
# moving on promptly is far from waiting a server out.
DNS_RETRANS_MS = 1500
AAAA_LATE_S = 0.4
DNS_MIN_SERVER_MS = header_define("telemetry/dns.h", "TC_DNS_MIN_SERVER_MS")
DNS_TRIES = header_define("telemetry/dns.h", "TC_DNS_TRIES")


def dns_server_ms(round_, servers):
    """One nameserver's time in a round (from 0) of a list of servers, as
    libc's res_send gives it (dns.h)."""
    ms = DNS_RETRANS_MS << round_
    if round_:
        ms //= servers
    return max(DNS_MIN_SERVER_MS, ms)


def dns_silent_ms(servers):
    """How long a lookup takes when no nameserver answers: every round."""
    return sum(servers * dns_server_ms(r, servers) for r in range(DNS_TRIES))
# The build for requests that wait out nameservers or try two addresses:
# room for two silent nameservers (6 s), or for IPv4 after a stalled IPv6
# exchange with 5 s to spare.
WIDE_DEADLINE_MS = 10000
# The TERM test's own build: a deadline far past the 1 s stop-flag poll, so
# stopping on TERM and running to the deadline cannot be mistaken.
LONG_DEADLINE_MS = 20000


IPV6 = has_ipv6_loopback()


class Http(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True


class Http6(Http):
    address_family = socket.AF_INET6


def responder(state):
    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            data = b""
            self.request.settimeout(5)
            try:
                while b"\r\n\r\n" not in data:
                    chunk = self.request.recv(4096)
                    if not chunk:
                        return
                    data += chunk
                head, _, body = data.partition(b"\r\n\r\n")
                lines = head.decode().split("\r\n")
                headers = {k.lower(): v.strip() for k, _, v in (line.partition(":") for line in lines[1:])}
                length = int(headers.get("content-length", "0"))
                while len(body) < length:
                    body += self.request.recv(4096)
                state["requests"].append({"line": lines[0], "headers": headers, "body": body})
                mode = state["mode"]
                if mode == "silent":
                    time.sleep(DEADLINE_MS / 1000 + 2)
                    return
                if mode == "silent_long":
                    time.sleep(LONG_DEADLINE_MS / 1000 + 2)
                    return
                if mode == "slow_ok":
                    time.sleep(0.2)
                if mode == "long_split":
                    # The n-th byte in one segment, extra bytes in the next.
                    self.request.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    self.request.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
                    time.sleep(0.05)
                    self.request.sendall(b"EXTRA")
                    return
                if mode == "trickle":
                    # One byte per segment: the head arrives split across reads.
                    self.request.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    for byte in b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok":
                        self.request.sendall(bytes([byte]))
                        time.sleep(0.002)
                    return
                reply = {
                    "ok": b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok",
                    "slow_ok": b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok",
                    "close_body": b"HTTP/1.0 200 OK\r\nContent-Type: text/plain\r\n\r\nstreamed",
                    "status404": b"HTTP/1.1 404 Not Found\r\nContent-Length: 2\r\n\r\nno",
                    "status500": b"HTTP/1.1 500 Error\r\nContent-Length: 2\r\n\r\nno",
                    "redirect": b"HTTP/1.1 302 Found\r\nLocation: /x\r\nContent-Length: 0\r\n\r\n",
                    "big": b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n" + b"x" * 100,
                    "big_close": b"HTTP/1.0 200 OK\r\n\r\n" + b"x" * 100,
                    "short": b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\n\r\nok",
                    # One small write, so the read that brings the second
                    # byte brings the rest with it.
                    "long": b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nokEXTRA",
                    "chunked": b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n2\r\nok\r\n0\r\n\r\n",
                    "early_close": b"",
                    "garbage": b"SSH-2.0-OpenSSH\r\n\r\n",
                    "cl_spaces": b"HTTP/1.1 200 OK\r\nContent-Length:   2  \r\n\r\nok",
                    "cl_plus": b"HTTP/1.1 200 OK\r\nContent-Length: +2\r\n\r\nok",
                    "cl_junk": b"HTTP/1.1 200 OK\r\nContent-Length: 2abc\r\n\r\nok",
                    "cl_empty": b"HTTP/1.1 200 OK\r\nContent-Length: \r\n\r\nok",
                    "cl_negative": b"HTTP/1.1 200 OK\r\nContent-Length: -1\r\n\r\nok",
                    "cl_conflict": b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\ncontent-length: 3\r\n\r\nok",
                    "cl_same_twice": b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Length: 2\r\n\r\nok",
                    "head_max": padded_head(HEADER_MAX) + b"ok",
                    "head_over": padded_head(HEADER_MAX + 1) + b"ok",
                    "head_max_big": padded_head(HEADER_MAX, 100) + b"x" * 100,
                }[mode]
                if reply:
                    self.request.sendall(reply)
            except OSError:
                pass
    return Handler


HEADER_MAX = header_define("telemetry/telemetry.h", "TC_HTTP_HEADER_MAX")


def padded_head(size, length=2):
    """A 200 head of exactly size bytes, blank line included."""
    head = b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\nX-Pad: " % length
    return head + b"p" * (size - len(head) - 4) + b"\r\n\r\n"


def dns_name(data, pos):
    labels = []
    while data[pos]:
        n = data[pos]
        labels.append(data[pos + 1:pos + 1 + n].decode())
        pos += n + 1
    return ".".join(labels), pos + 1


def encode_name(name):
    return b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\0"


def record(name, rtype, rdata, ttl=300, klass=1):
    owner = name if isinstance(name, bytes) else encode_name(name)
    return owner + struct.pack(">HHIH", rtype, klass, ttl, len(rdata)) + rdata


V4 = {"127.0.0.1": socket.inet_pton(socket.AF_INET, "127.0.0.1")}
V6 = {"::1": socket.inet_pton(socket.AF_INET6, "::1")}
# What a reply the resolver must refuse would add if it took it.
BOGUS = {1: socket.inet_pton(socket.AF_INET, "10.9.9.9"), 28: socket.inet_pton(socket.AF_INET6, "2001:db8::9")}

# Answers by question name: A and AAAA rdata lists, a CNAME target, or NXDOMAIN.
ZONE = {
    "svc.test": {1: [V4["127.0.0.1"]], 28: [V6["::1"]]},
    "v4only.test": {1: [V4["127.0.0.1"]]},
    "v6only.test": {28: [V6["::1"]]},
    "ttl0.test": {1: [V4["127.0.0.1"]]},
    "ttlbig.test": {1: [V4["127.0.0.1"]]},
    "ttlneg.test": {1: [V4["127.0.0.1"]]},
    # The same address twice in one answer.
    "dup.test": {1: [V4["127.0.0.1"], V4["127.0.0.1"]]},
    # Reached through shortalias.test's CNAME, which has the shorter TTL.
    "long.test": {1: [V4["127.0.0.1"]]},
    "offchain.test": {1: [V4["127.0.0.1"]]},
}
# ttlneg: the top bit set, which RFC 2181 says to read as zero.
TTL = {"ttl0.test": 0, "ttlbig.test": 0x7fffffff, "ttlneg.test": 0x80000000, "long.test": 3600,
       "offchain.test": 3600}


def packed_answers(question, qtype):
    """packed.test as a compressing server writes it: a CNAME whose owner
    points at the question and whose target (svc + a pointer to the
    question's "test" label) is in turn the owner of the address, by
    pointer."""
    start = 12 + len(question)
    test_label = 12 + 1 + len("packed")
    cname = record(b"\xc0\x0c", 5, b"\x03svc" + struct.pack(">H", 0xc000 | test_label))
    target = start + 2 + 10  # the CNAME's rdata
    return [cname] + [record(struct.pack(">H", 0xc000 | target), qtype, rdata) for rdata in ZONE["svc.test"][qtype]]


def bad_reply(qid, question, qtype, kind):
    """A reply with our id and question type that the resolver must refuse,
    carrying an address it would otherwise take."""
    flags = 0x8180
    name = question[:-4]
    tail = question[-4:]
    owner = b"\xc0\x0c"
    if kind == "loop":
        owner = struct.pack(">H", 0xc000 | (12 + len(question)))  # points at itself
    elif kind == "oob":
        owner = b"\xff\xff"  # far past the end of the reply
    elif kind == "nul":
        # Decoded as C text, "v4only" "test\0x" would read v4only.test.
        name = b"\x06v4only\x06test\0x\0"
    elif kind == "dot":
        name = b"\x0bv4only.test\0"  # one label that reads as two
    answers = record(owner, qtype, BOGUS[qtype])
    return qid + struct.pack(">HHHHH", flags, 1, 1, 0, 0) + name + tail + answers


def dns_reply(query, mode="answer", zone=ZONE):
    qid = query[:2]
    name, pos = dns_name(query, 12)
    qtype = struct.unpack(">H", query[pos:pos + 2])[0]
    question = query[12:pos + 4]
    answers = []
    rcode = 0
    flags = 0x8180
    target = name
    if name == "alias.test":
        # A CNAME chain: alias.test -> middle.test -> svc.test, out of order.
        answers.append(record("middle.test", 5, encode_name("svc.test")))
        answers.append(record("alias.test", 5, encode_name("middle.test")))
        target = "svc.test"
    if name == "dupalias.test":
        # Its own A record and a CNAME to v4only.test, which has the same one.
        answers.append(record("dupalias.test", 5, encode_name("v4only.test")))
        if qtype == 1:
            answers.append(record("dupalias.test", 1, V4["127.0.0.1"]))
        target = "v4only.test"
    if name == "shortalias.test":
        # A CNAME kept for a minute to an address kept for an hour.
        answers.append(record("shortalias.test", 5, encode_name("long.test"), 60))
        target = "long.test"
    if name == "offchain.test":
        # A short-lived CNAME whose owner is not on the question's chain
        # (only its target is).
        answers.append(record("unrelated.test", 5, encode_name("offchain.test"), 60))
    if name == "chaosalias.test":
        # Its own A record, and a CNAME of class CH to bogus.test, whose
        # addresses the resolver would take if it followed that CNAME.
        answers.append(record("chaosalias.test", 5, encode_name("bogus.test"), klass=3))
        answers.append(record("bogus.test", qtype, BOGUS[qtype]))
        if qtype == 1:
            answers.append(record("chaosalias.test", 1, V4["127.0.0.1"]))
    if name == "missing.test":
        rcode = 3
    if name == "packed.test":
        answers += packed_answers(question, qtype)
    for rdata in zone.get(target, {}).get(qtype, []):
        answers.append(record(target, qtype, rdata, TTL.get(target, 300)))
    if name == "decoy.test" and qtype == 1:
        # An address for some other name in the answer must be ignored.
        answers.append(record("elsewhere.test", 1, BOGUS[1]))
        answers.append(record("decoy.test", 1, V4["127.0.0.1"]))
    if mode in ("servfail", "refused") or (mode == "servfail_a" and qtype == 1):
        rcode, answers = (5 if mode == "refused" else 2), []
    if (mode in ("nxdomain_a", "nxdomain_a_aaaa_lost", "nxdomain_a_aaaa_late") and qtype == 1) or \
            (mode in ("nxdomain_aaaa", "nxdomain_aaaa_a_lost") and qtype == 28):
        rcode, answers = 3, []
    if mode == "nodata":
        answers = []  # a clean reply with no record
    if mode == "truncated":
        flags |= 0x0200
        answers = [record(name, qtype, BOGUS[qtype])]
    header = qid + struct.pack(">HHHHH", flags | rcode, 1, len(answers), 0, 0)
    replies = [header + question + b"".join(answers)]
    if mode == "spoof":
        wrong = bytes([qid[0] ^ 0xff, qid[1]])
        replies.insert(0, wrong + replies[0][2:])
    if mode == "malformed_first":
        # The right ID, but the answer count runs past the end of the packet.
        bad = qid + struct.pack(">HHHHH", 0x8180, 1, 3, 0, 0) + question
        replies.insert(0, bad)
    if mode.startswith("bad_"):
        replies.insert(0, bad_reply(qid, question, qtype, mode[4:]))
    if mode == "wrong_question":
        # Our id, but another name, and then the other record type.
        other = encode_name("other.test") + question[-4:]
        replies.insert(0, qid + struct.pack(">HHHHH", 0x8180, 1, 1, 0, 0) + other +
                       record(b"\xc0\x0c", qtype, BOGUS[qtype]))
        swapped = 28 if qtype == 1 else 1
        replies.insert(1, qid + struct.pack(">HHHHH", 0x8180, 1, 1, 0, 0) + question[:-4] +
                       struct.pack(">HH", swapped, 1) + record(b"\xc0\x0c", swapped, BOGUS[swapped]))
    if mode == "wrong_class":
        # Our id, name and type, but the question of class CH: not a reply to
        # our IN question, so its address must not be taken.
        chaos = question[:-2] + struct.pack(">H", 3)
        replies.insert(0, qid + struct.pack(">HHHHH", 0x8180, 1, 1, 0, 0) + chaos +
                       record(b"\xc0\x0c", qtype, BOGUS[qtype]))
    if mode == "duplicate":
        replies.append(replies[-1])
    return replies


class Dns:
    def __init__(self, family, host, port, scope=0):
        self.sock = socket.socket(family, socket.SOCK_DGRAM)
        try:
            self.sock.bind((host, port, 0, scope) if scope else (host, port))
        except OSError:
            self.sock.close()
            raise
        self.port = self.sock.getsockname()[1]
        # serve() looks at stopping between waits: on Linux close() does not
        # wake a thread blocked in recvfrom(), which would go on reading,
        # through descriptor reuse, datagrams meant for a later socket.
        self.sock.settimeout(0.2)
        self.stopping = False
        self.reset()
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def close(self):
        self.stopping = True
        self.thread.join(timeout=10)
        assert not self.thread.is_alive(), "DNS server did not stop"
        self.sock.close()

    def reset(self):
        self.mode = "answer"
        # The names this server answers for; a test may give one new records.
        self.zone = ZONE
        # The mode applies to the first mode_peers sockets that ask (each
        # nameserver a lookup asks gets a socket of its own, kept for every
        # round); later ones get plain answers. None: every one.
        self.mode_peers = None
        self.queries = []
        self.peers = []

    def send_late(self, replies, peer):
        try:
            for reply in replies:
                self.sock.sendto(reply, peer)
        except OSError:
            pass

    def read(self, count, timeout=5):
        """The queries, once this thread has read at least count. A lookup
        that stops at NXDOMAIN for A returns before the AAAA query it sent
        beside the A one has been read here."""
        deadline = time.monotonic() + timeout
        while len(self.queries) < count and time.monotonic() < deadline:
            time.sleep(0.01)
        return self.queries

    def serve(self):
        seen = set()
        while not self.stopping:
            try:
                data, peer = self.sock.recvfrom(1500)
            except TimeoutError:
                continue
            except OSError:
                return
            self.queries.append(data)
            if peer not in self.peers:
                self.peers.append(peer)
            mode = self.mode
            if self.mode_peers is not None and self.peers.index(peer) >= self.mode_peers:
                mode = "answer"
            qtype = struct.unpack(">H", data[-4:-2])[0]
            if mode == "silent":
                continue
            if mode == "drop_first" and data[:2] not in seen:
                seen.add(data[:2])  # lose the first copy of each query
                continue
            if mode == "late_first":
                # Answer the first copy of each query two first-round
                # times late, in the middle of the second round; lose the
                # resend.
                if data[:2] not in seen:
                    seen.add(data[:2])
                    threading.Timer(2 * DNS_RETRANS_MS / 1000, self.send_late,
                                    (dns_reply(data, "answer", self.zone), peer)).start()
                continue
            if mode == "nxdomain_a_aaaa_late" and qtype == 28:
                # NXDOMAIN for A at once (dns_reply), the AAAA answer later.
                threading.Timer(AAAA_LATE_S, self.send_late, (dns_reply(data, "answer", self.zone), peer)).start()
                continue
            if (mode in ("aaaa_only", "nxdomain_aaaa_a_lost") and qtype == 1) or \
                    (mode in ("a_only", "nxdomain_a_aaaa_lost") and qtype == 28):
                continue
            for reply in dns_reply(data, mode, self.zone):
                self.sock.sendto(reply, peer)


def bound_on_both(make4, make6, close):
    """make4() on a free 127.0.0.1 port, then make6(port) on ::1 at the same
    one, picking another port when another process has that one on ::1.
    (server4, server6 or None without IPv6 loopback)."""
    for _ in range(20):
        first = make4()
        if not IPV6:
            return first, None
        try:
            return first, make6(first)
        except OSError:
            close(first)
    pytest.fail("no port free on both loopbacks")


@pytest.fixture(scope="module")
def rig(tmp_path_factory):
    root = tmp_path_factory.mktemp("http")
    dns4, dns6 = bound_on_both(lambda: Dns(socket.AF_INET, "127.0.0.1", 0),
                               lambda d: Dns(socket.AF_INET6, "::1", d.port), Dns.close)
    resolv = root / "resolv.conf"
    resolv.write_text("nameserver 127.0.0.1\n")
    native = ROOT / "build/native"
    def build(path, deadline_ms, connect_ms=CONNECT_MS):
        # test_http.c supplies acp_monotonic_ms (a clock the script moves on).
        compile_modules(path, ("native/telemetry/http.c", "native/telemetry/dns.c", "native/telemetry/errclass.c",
                               "native/telemetry/entropy.c", "native/vendor/tweetnacl.c", "native/vendor/random.c"),
                        flags=("-I", str(native / "telemetry"), "-I", str(native / "common"), "-I", str(native),
                               f"-DTC_HTTP_DEADLINE_MS={deadline_ms}", f"-DTC_HTTP_CONNECT_MS={connect_ms}",
                               f"-DTC_HTTP_ADDRESS_MS={ADDRESS_MS}",
                               f"-DTC_DNS_RETRANS_MS={DNS_RETRANS_MS}", f"-DTC_DNS_PORT={dns4.port}",
                               f'-DTC_RESOLV_CONF="{resolv}"'),
                        extra_sources=(ROOT / "tests/native/unit/test_http.c",))
        return path
    binary = build(root / "http", DEADLINE_MS, PROMPT_CONNECT_MS)
    long_binary = build(root / "http-long", LONG_DEADLINE_MS)
    wide_binary = build(root / "http-wide", WIDE_DEADLINE_MS)
    state = {"mode": "ok", "requests": []}
    http4, http6 = bound_on_both(lambda: Http(("127.0.0.1", 0), responder(state)),
                                 lambda h: Http6(("::1", h.server_address[1]), responder(state)),
                                 Http.server_close)
    for server in (http4, http6):
        if server:
            threading.Thread(target=server.serve_forever, daemon=True).start()
    yield {"binary": binary, "long_binary": long_binary, "wide_binary": wide_binary, "state": state, "port": http4.server_address[1], "resolv": resolv,
           "dns4": dns4, "dns6": dns6}
    for server in (http4, http6):
        if server:
            server.shutdown()
            server.server_close()
    for dns in (dns4, dns6):
        if dns:
            dns.close()


@pytest.fixture
def http(rig):
    rig["state"]["mode"] = "ok"
    rig["state"]["requests"].clear()
    rig["dns4"].reset()
    if rig["dns6"]:
        rig["dns6"].reset()
    rig["resolv"].write_text("nameserver 127.0.0.1\n")

    def run(command, url, *extra, binary="binary"):
        result = subprocess.run([str(rig[binary]), command, url, *extra], capture_output=True, text=True,
                                timeout=20)
        assert result.returncode == 0, result.stderr
        lines = result.stdout.splitlines()
        return lines[0], int(lines[1].split()[1])
    return rig, run


def test_post_sends_the_heartbeat_request(http):
    rig, run = http
    outcome, _ = run("post", f"http://127.0.0.1:{rig['port']}/v1/router-heartbeats")
    assert outcome == "ok ok"
    request = rig["state"]["requests"][0]
    assert request["line"] == "POST /v1/router-heartbeats HTTP/1.0"
    assert request["headers"]["host"] == f"127.0.0.1:{rig['port']}"
    assert request["headers"]["authorization"].startswith("Bearer ")
    assert request["headers"]["content-type"] == "application/json"
    assert request["headers"]["connection"] == "close"
    version = re.search(r'#define HEARTBEAT_AGENT_VERSION "(\w+)"', (ROOT / "build/native/telemetry/telemetry.h").read_text())[1]
    assert request["headers"]["user-agent"] == f"timecapsulesmb-telemetry/{version}"
    assert request["body"] == b"{}"


def test_get_sends_no_credentials_and_reads_to_the_close(http):
    rig, run = http
    rig["state"]["mode"] = "close_body"
    outcome, _ = run("get", f"http://127.0.0.1:{rig['port']}/downloads/bin/debug6")
    assert outcome == "ok streamed"
    request = rig["state"]["requests"][0]
    assert request["line"] == "GET /downloads/bin/debug6 HTTP/1.0"
    assert "authorization" not in request["headers"] and request["body"] == b""


@pytest.mark.parametrize("mode", ["status404", "status500", "redirect", "big", "big_close", "short",
                                  "chunked", "garbage"])
def test_anything_but_a_complete_2xx_response_within_the_limit_fails(http, mode):
    # http.c's response table, the rows ending "SERVER, answered": other
    # status (404, 500, redirect, garbage), length over limit (big), body
    # past limit (big_close), close before n bytes (short), chunked.
    rig, run = http
    rig["state"]["mode"] = mode
    outcome, _ = run("post", f"http://127.0.0.1:{rig['port']}/v1/router-heartbeats")
    assert outcome == "fail server"


@pytest.mark.parametrize("mode", ["long", "long_split"])
def test_bytes_past_content_length_are_dropped_however_they_arrive(http, mode):
    # Response table: BODY_LENGTH, n bytes, 2xx. The body is the first n
    # bytes, whether the extra ones come in the n-th byte's read (long) or
    # after it (long_split), as curl -fsS kept them.
    rig, run = http
    rig["state"]["mode"] = mode
    assert run("post", f"http://127.0.0.1:{rig['port']}/x")[0] == "ok ok"


def test_a_close_with_no_answer_at_all_is_a_network_failure(http):
    # Response table: HEAD, close, no byte.
    rig, run = http
    rig["state"]["mode"] = "early_close"
    outcome, _ = run("post", f"http://127.0.0.1:{rig['port']}/v1/router-heartbeats")
    assert outcome == "fail network"


def test_a_body_exactly_at_the_limit_is_kept(http):
    rig, run = http
    rig["state"]["mode"] = "big"
    assert run("post", f"http://127.0.0.1:{rig['port']}/x", "100")[0] == "ok " + "x" * 100
    assert run("post", f"http://127.0.0.1:{rig['port']}/x", "99")[0] == "fail server"


def test_a_silent_server_fails_at_the_deadline(http):
    rig, run = http
    rig["state"]["mode"] = "silent"
    outcome, elapsed = run("post", f"http://127.0.0.1:{rig['port']}/v1/router-heartbeats")
    assert outcome == "fail network"
    # The production budget, not the server's longer silence (2 s more).
    assert DEADLINE_MS - 100 <= elapsed < DEADLINE_MS + 1500


def test_wall_clock_steps_do_not_touch_the_deadline(http):
    rig, run = http
    rig["state"]["mode"] = "slow_ok"
    outcome, _ = run("step", f"http://127.0.0.1:{rig['port']}/v1/router-heartbeats")
    assert outcome == "ok ok"


@pytest.mark.parametrize("url", ["https://127.0.0.1/x", "http://127.0.0.1", "http://[::1/x", "http://h:0/x",
                                 "http://h:99999/x", "http://:80/x"])
def test_malformed_urls_fail_without_connecting(http, url):
    rig, run = http
    assert run("post", url)[0].startswith("fail ")
    assert rig["state"]["requests"] == []


def test_a_refused_port_fails_promptly(http):
    rig, run = http
    with held_refusing_ports(1) as (port,):
        outcome, elapsed = run("post", f"http://127.0.0.1:{port}/x")
    assert outcome == "fail network" and elapsed < PROMPT_CONNECT_MS


def test_names_resolve_through_resolv_conf(http):
    rig, run = http
    outcome, _ = run("post", f"http://v4only.test:{rig['port']}/v1/router-heartbeats")
    assert outcome == "ok ok"
    assert rig["state"]["requests"][0]["headers"]["host"] == f"v4only.test:{rig['port']}"
    # One A and one AAAA question.
    types = sorted(struct.unpack(">H", q[-4:-2])[0] for q in rig["dns4"].queries)
    assert types == [1, 28]


def test_cname_chains_are_followed_and_other_names_ignored(http):
    rig, run = http
    assert run("post", f"http://alias.test:{rig['port']}/x")[0] == "ok ok"
    assert run("post", f"http://decoy.test:{rig['port']}/x")[0] == "ok ok"


def test_a_reply_with_the_wrong_id_is_ignored(http):
    rig, run = http
    rig["dns4"].mode = "spoof"
    assert run("post", f"http://v4only.test:{rig['port']}/x")[0] == "ok ok"


def test_nxdomain_fails_without_asking_the_next_server(http):
    # NXDOMAIN for both types: the name does not exist, and the second
    # nameserver (a socket of its own) is never asked.
    rig, run = http
    rig["resolv"].write_text("nameserver 127.0.0.1\nnameserver 127.0.0.1\n")
    outcome, elapsed = run("post", f"http://missing.test:{rig['port']}/x")
    assert outcome == "fail network"
    assert len(rig["dns4"].peers) == 1
    # Both replies came at once: no second round, no wait for the server's time.
    assert elapsed < DNS_RETRANS_MS
    assert len(rig["dns4"].read(2)) == 2


def test_nxdomain_for_aaaa_after_a_lost_a_reply_asks_the_next_server(http):
    # The first nameserver loses both copies of the A query and says
    # NXDOMAIN for AAAA: that is AAAA answered with nothing, not a missing
    # name, so the second nameserver is asked and its answer used.
    rig, run = http
    rig["dns4"].mode = "nxdomain_aaaa_a_lost"
    rig["dns4"].mode_peers = 1
    rig["resolv"].write_text("nameserver 127.0.0.1\nnameserver 127.0.0.1\n")
    outcome, elapsed = run("resolve", "svc.test")
    assert outcome == "::1 127.0.0.1"
    assert len(rig["dns4"].peers) == 2
    assert elapsed >= DNS_RETRANS_MS - 100  # the first server's time, waiting for A


def test_a_silent_nameserver_hands_over_to_the_next(http):
    rig, run = http
    rig["dns4"].mode = "silent"
    rig["resolv"].write_text("nameserver 127.0.0.1\nnameserver 127.0.0.1\n")
    outcome, elapsed = run("post", f"http://v4only.test:{rig['port']}/x", binary="wide_binary")
    # Both copies of the silent server use up their time in both rounds.
    assert outcome == "fail network" and dns_silent_ms(2) - 100 <= elapsed < dns_silent_ms(2) + 1500
    # Each server asked A and AAAA once per round, under the same ids.
    assert len(rig["dns4"].queries) == 2 * 2 * DNS_TRIES
    assert len({q[:2] for q in rig["dns4"].queries}) == 4


def test_three_silent_nameservers_share_the_second_round(http):
    # libc's schedule: 1.5 s each, then 3 s shared three ways, at least 1 s
    # each (7.5 s), where a fixed time per server would give 9 s.
    rig, run = http
    rig["dns4"].mode = "silent"
    rig["resolv"].write_text("nameserver 127.0.0.1\n" * 3)
    outcome, elapsed = run("resolve", "v4only.test", binary="wide_binary")
    assert outcome == "fail" and dns_silent_ms(3) - 100 <= elapsed < dns_silent_ms(3) + 1500
    # One socket per server, kept for both rounds.
    assert len(rig["dns4"].peers) == 3
    assert len(rig["dns4"].queries) == 3 * 2 * DNS_TRIES


def test_a_reply_to_the_first_round_that_comes_late_is_read_on_the_next(http):
    # The server answers the first copies in the middle of the second round
    # and loses the resends: the socket kept from the first round still
    # takes the late answer, as libc's does.
    rig, run = http
    rig["dns4"].mode = "late_first"
    outcome, elapsed = run("resolve", "svc.test", binary="wide_binary")
    assert outcome == "::1 127.0.0.1"
    assert elapsed >= 2 * DNS_RETRANS_MS - 100
    assert len(rig["dns4"].peers) == 1 and len(rig["dns4"].queries) == 2 * DNS_TRIES


def test_term_stops_a_lookup_without_asking_the_remaining_nameservers(http):
    rig, _ = http
    rig["dns4"].mode = "silent"
    rig["resolv"].write_text("nameserver 127.0.0.1\n" * 3)
    process = subprocess.Popen([str(rig["long_binary"]), "resolve", "v4only.test"], stdout=subprocess.PIPE, text=True)
    # TERM once the first server is asked: before main() installs the
    # handler it would kill the process, which a loaded host can delay.
    deadline = time.monotonic() + 20
    while not rig["dns4"].queries and process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert rig["dns4"].queries, "no query arrived"
    started = time.monotonic()
    process.terminate()
    out, _ = process.communicate(timeout=30)
    assert out.splitlines()[0] == "fail"
    # The stop flag is checked every second; the first server's time is
    # 1.5 s, so the second would be asked by then without the check.
    assert time.monotonic() - started < 1.4
    assert len(rig["dns4"].peers) == 1


@pytest.mark.skipif(not IPV6, reason="no IPv6 loopback")
def test_an_ipv6_nameserver_answers_after_a_silent_ipv4_one(http):
    rig, run = http
    rig["dns4"].mode = "silent"
    rig["resolv"].write_text("nameserver 127.0.0.1\nnameserver ::1\n")
    outcome, elapsed = run("post", f"http://v4only.test:{rig['port']}/x", binary="wide_binary")
    assert outcome == "ok ok" and elapsed >= DNS_RETRANS_MS - 100
    assert rig["dns6"].queries


def test_the_dns_answer_is_reused_within_its_ttl(http):
    rig, run = http
    assert run("twice", f"http://v4only.test:{rig['port']}/x")[0] == "ok ok"
    assert len(rig["dns4"].queries) == 2  # A and AAAA once, for two requests
    assert len(rig["state"]["requests"]) == 2


@pytest.mark.skipif(not IPV6, reason="no IPv6 loopback")
def test_ipv6_literals_and_aaaa_records_connect_over_ipv6(http):
    rig, run = http
    assert run("post", f"http://[::1]:{rig['port']}/x")[0] == "ok ok"
    assert rig["state"]["requests"][0]["headers"]["host"] == f"[::1]:{rig['port']}"
    assert run("post", f"http://v6only.test:{rig['port']}/x")[0] == "ok ok"


def test_a_refused_address_falls_through_to_the_next(http):
    # svc.test has AAAA ::1 and A 127.0.0.1; tried IPv6 first. The port is
    # held refusing on both loopbacks, and the IPv4 server listens on it
    # beside the hold (tests/loopback.py): [::1] refuses, IPv4 answers.
    rig, run = http
    with held_refusing_ports(1) as [port]:
        server = Http(("127.0.0.1", port), responder(rig["state"]))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            outcome, elapsed = run("post", f"http://svc.test:{port}/x")
        finally:
            server.shutdown()
            server.server_close()
    assert outcome == "ok ok"
    # Refused at once, not at the connect limit.
    assert elapsed < PROMPT_CONNECT_MS
    assert len(rig["state"]["requests"]) == 1


@pytest.mark.parametrize("mode,ok", [("cl_spaces", True), ("cl_same_twice", True), ("cl_plus", False),
                                     ("cl_junk", False), ("cl_empty", False), ("cl_negative", False),
                                     ("cl_conflict", False)])
def test_content_length_is_digits_only(http, mode, ok):
    rig, run = http
    rig["state"]["mode"] = mode
    assert run("post", f"http://127.0.0.1:{rig['port']}/x")[0] == ("ok ok" if ok else "fail server")


def test_a_lost_query_is_sent_again(http):
    rig, run = http
    rig["dns4"].mode = "drop_first"
    outcome, elapsed = run("resolve", "v4only.test")
    assert outcome == "127.0.0.1"
    # The second round resends both queries once the first round's time is
    # up, and the fake answers the copies at once; the second round's own
    # time would end at 3 * DNS_RETRANS_MS.
    assert DNS_RETRANS_MS - 100 <= elapsed < 2 * DNS_RETRANS_MS
    # A and AAAA, each sent twice under one id, as libc resends a query.
    assert len(rig["dns4"].queries) == 4 and len({q[:2] for q in rig["dns4"].queries}) == 2


@pytest.mark.skipif(not IPV6, reason="no IPv6 loopback")
def test_half_an_answer_serves_the_request_but_is_not_kept(http):
    # The A reply never comes: the AAAA-only answer carries both requests
    # over IPv6, and is not cached, so a device whose IPv6 cannot reach the
    # server is not stuck with it.
    rig, run = http
    rig["dns4"].mode = "aaaa_only"
    assert run("twice", f"http://svc.test:{rig['port']}/x", binary="wide_binary")[0] == "ok ok"
    assert len(rig["state"]["requests"]) == 2
    a_queries = [q for q in rig["dns4"].queries if struct.unpack(">H", q[-4:-2])[0] == 1]
    # Two requests, each asking A in both rounds before using the half
    # answer (AAAA, answered in the first, is not asked again).
    assert len(a_queries) == 4
    assert len(rig["dns4"].queries) - len(a_queries) == 2


def test_nxdomain_for_a_uses_the_aaaa_records_beside_it(http):
    # svc.test has an AAAA record, and NXDOMAIN for A: as libc does, the
    # lookup returns the AAAA addresses, without asking the second
    # nameserver.
    rig, run = http
    rig["dns4"].mode = "nxdomain_a"
    rig["resolv"].write_text("nameserver 127.0.0.1\nnameserver 127.0.0.1\n")
    assert run("resolve", "svc.test")[0] == "::1"
    assert len(rig["dns4"].peers) == 1 and len(rig["dns4"].read(2)) == 2


def test_nxdomain_for_a_waits_for_a_late_aaaa_reply(http):
    # The AAAA answer comes after the NXDOMAIN for A: the lookup waits for
    # it, within the server's time, and uses it.
    rig, run = http
    rig["dns4"].mode = "nxdomain_a_aaaa_late"
    rig["resolv"].write_text("nameserver 127.0.0.1\nnameserver 127.0.0.1\n")
    outcome, elapsed = run("resolve", "svc.test")
    assert outcome == "::1"
    assert len(rig["dns4"].peers) == 1
    assert AAAA_LATE_S * 1000 <= elapsed < DNS_RETRANS_MS


def test_nxdomain_for_a_with_the_aaaa_reply_lost_fails_after_the_servers_time(http):
    # The AAAA reply never comes: the lookup fails once the first server's
    # time is out, without asking the second (libc would ask it for AAAA;
    # dns.c's judge() says why this one does not).
    rig, run = http
    rig["dns4"].mode = "nxdomain_a_aaaa_lost"
    rig["resolv"].write_text("nameserver 127.0.0.1\nnameserver 127.0.0.1\n")
    outcome, elapsed = run("resolve", "svc.test")
    assert outcome == "fail"
    assert len(rig["dns4"].peers) == 1 and len(rig["dns4"].read(2)) == 2
    assert elapsed >= DNS_RETRANS_MS - 100


def test_a_cname_of_another_class_is_not_followed(http):
    rig, run = http
    assert run("resolve", "chaosalias.test")[0] == "127.0.0.1"


def test_a_malformed_reply_with_our_id_does_not_end_the_query(http):
    rig, run = http
    rig["dns4"].mode = "malformed_first"
    assert run("resolve", "v4only.test")[0] == "127.0.0.1"


def test_a_repeated_reply_adds_no_addresses(http):
    rig, run = http
    rig["dns4"].mode = "duplicate"
    expected = "::1 127.0.0.1"
    assert run("resolve", "svc.test")[0] == expected


@pytest.mark.parametrize("name", ["dup.test", "dupalias.test"])
def test_an_address_given_twice_is_listed_once(http, name):
    # Twice in one answer, or once for the name and once for its CNAME
    # target.
    rig, run = http
    assert run("resolve", name)[0] == "127.0.0.1"


def test_term_stops_a_request_within_a_second(http):
    rig, _ = http
    rig["state"]["mode"] = "silent_long"
    process = subprocess.Popen([str(rig["long_binary"]), "post", f"http://127.0.0.1:{rig['port']}/x"],
                               stdout=subprocess.PIPE, text=True)
    # TERM only once the request is in flight: before main() installs the
    # handler it would kill the process, which a loaded host can delay.
    deadline = time.monotonic() + 20
    while not rig["state"]["requests"] and process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert rig["state"]["requests"], "the request never arrived"
    started = time.monotonic()
    process.terminate()
    out, _ = process.communicate(timeout=30)
    assert out.splitlines()[0] == "fail cancelled"
    # The stop flag is checked every second; without it the request would
    # run to its 20 s deadline.
    assert time.monotonic() - started < LONG_DEADLINE_MS / 2000


@pytest.mark.parametrize("call", ["connect", "send"])
@pytest.mark.parametrize("target", ["127.0.0.1", "v4only.test"])
def test_no_buffers_for_a_connection_is_a_local_failure(http, call, target):
    # ENOBUFS from connect() or send(), as on a device out of mbufs, gets the
    # local retry rules and is counted, both for the POST (an address
    # literal: no lookup) and for the name lookup before it.
    rig, _ = http
    result = subprocess.run([str(rig["binary"]), "post", f"http://{target}:{rig['port']}/x"],
                            env={**os.environ, "TC_TEST_ENOBUFS": call}, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert lines[0] == "fail local_resource"
    assert lines[2] != "socket_failures 0"
    assert rig["state"]["requests"] == []


def test_no_descriptor_for_a_socket_is_a_local_failure(http):
    # A device out of descriptors is short of resources, not offline: the
    # retry rules treat the two differently, and the failure is counted.
    rig, run = http
    result = subprocess.run([str(rig["binary"]), "nofile", f"http://127.0.0.1:{rig['port']}/x"],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert lines[0] == "fail local_resource"
    assert lines[2] == "socket_failures 1"
    assert rig["state"]["requests"] == []


def test_a_response_split_into_single_bytes_is_read_whole(http):
    rig, run = http
    rig["state"]["mode"] = "trickle"
    assert run("post", f"http://127.0.0.1:{rig['port']}/x")[0] == "ok ok"


@pytest.mark.parametrize("mode,limit,expected", [("head_max", "64", "ok ok"), ("head_over", "64", "fail server"),
                                                 ("head_max_big", "100", "ok " + "x" * 100)])
def test_a_head_longer_than_the_maximum_is_refused_even_in_one_read(http, mode, limit, expected):
    # Response table: HEAD, head past TC_HTTP_HEADER_MAX. The whole response goes out in one write and fits the buffer, so one
    # read can bring the blank line in past the maximum.
    rig, run = http
    rig["state"]["mode"] = mode
    assert run("post", f"http://127.0.0.1:{rig['port']}/x", limit)[0] == expected


# --- Name compression and replies the resolver must refuse

def test_compressed_owners_and_cname_targets_are_followed(http):
    rig, run = http
    assert run("resolve", "packed.test")[0] == "::1 127.0.0.1"


@pytest.mark.parametrize("kind", ["loop", "oob", "nul", "dot"])
def test_a_reply_with_a_bad_name_is_refused_and_the_good_one_after_it_counts(http, kind):
    # loop: an owner that points at itself (the hop limit ends it); oob: a
    # pointer past the end; nul and dot: labels whose text would read as the
    # question's name. Each carries 10.9.9.9 / 2001:db8::9: taken, it would
    # be the answer and the valid reply after it a repeat.
    rig, run = http
    rig["dns4"].mode = f"bad_{kind}"
    outcome, elapsed = run("resolve", "v4only.test")
    assert outcome == "127.0.0.1"
    # Answered by the valid reply, before the second round a refused one
    # would leave waiting for.
    assert elapsed < DNS_RETRANS_MS


def test_a_reply_to_a_question_of_another_class_is_ignored(http):
    rig, run = http
    rig["dns4"].mode = "wrong_class"
    assert run("resolve", "svc.test")[0] == "::1 127.0.0.1"


def test_a_reply_for_another_name_or_type_is_ignored(http):
    rig, run = http
    rig["dns4"].mode = "wrong_question"
    assert run("resolve", "svc.test")[0] == "::1 127.0.0.1"


@pytest.mark.parametrize("mode", ["servfail", "refused", "truncated"])
def test_a_failing_nameserver_hands_over_to_the_next_at_once(http, mode):
    # Its replies count as answers with no records, so the resolver moves on
    # without waiting out the server's time.
    rig, run = http
    rig["dns4"].mode = mode
    rig["dns4"].mode_peers = 1
    rig["resolv"].write_text("nameserver 127.0.0.1\nnameserver 127.0.0.1\n")
    outcome, elapsed = run("resolve", "v4only.test")
    assert outcome == "127.0.0.1"
    assert elapsed < DNS_RETRANS_MS
    assert len(rig["dns4"].peers) == 2


def test_every_nameserver_failing_fails_without_waiting(http):
    rig, run = http
    rig["dns4"].mode = "servfail"
    rig["resolv"].write_text("nameserver 127.0.0.1\nnameserver 127.0.0.1\n")
    outcome, elapsed = run("resolve", "v4only.test")
    assert outcome == "fail" and elapsed < DNS_RETRANS_MS
    # A server that answered both queries is not asked again in round two.
    assert len(rig["dns4"].peers) == 2


# --- resolv.conf

def scoped_loopback_dns(port):
    """A fake nameserver on fe80::1%lo0 at port, or None where loopback has
    no link-local address (Linux)."""
    try:
        return Dns(socket.AF_INET6, "fe80::1", port, socket.if_nametoindex("lo0"))
    except OSError:
        return None


@pytest.mark.parametrize("scope", ["lo0", "index"])
def test_a_scoped_link_local_nameserver_is_asked_on_its_interface(http, scope):
    rig, run = http
    dns = scoped_loopback_dns(rig["dns4"].port)
    if dns is None:
        pytest.skip("no fe80::1 on loopback")
    try:
        written = "lo0" if scope == "lo0" else str(socket.if_nametoindex("lo0"))
        rig["resolv"].write_text(f"nameserver fe80::1%{written}\n")
        assert run("resolve", "v4only.test")[0] == "127.0.0.1"
        assert dns.queries and not rig["dns4"].queries
    finally:
        dns.close()


@pytest.mark.parametrize("entry", ["fe80::1", "fe80::1%nosuchif0", "fe80::1%"])
def test_a_link_local_nameserver_without_a_usable_scope_is_skipped(http, entry):
    # It cannot be reached, so the file names no usable server and the local
    # one is asked, as libc does with none.
    rig, run = http
    rig["resolv"].write_text(f"nameserver {entry}\n")
    assert run("resolve", "v4only.test")[0] == "127.0.0.1"
    assert rig["dns4"].queries


def test_comments_and_other_lines_name_no_nameserver(http):
    rig, run = http
    rig["resolv"].write_text("# dns-update-script -M rtsold 2001:db8::53\nsearch example\n;nameserver 10.9.9.9\n")
    assert run("resolve", "v4only.test")[0] == "127.0.0.1"
    assert rig["dns4"].queries


@pytest.mark.skipif(not IPV6, reason="no IPv6 loopback")
def test_nameservers_past_the_third_are_not_asked(http):
    rig, run = http
    rig["dns4"].mode = "silent"
    rig["resolv"].write_text("nameserver 127.0.0.1\n" * 3 + "nameserver ::1\n")
    assert run("resolve", "v4only.test", binary="wide_binary")[0] == "fail"
    assert len(rig["dns4"].peers) == 3 and not rig["dns6"].queries


def test_a_missing_resolv_conf_asks_the_local_nameserver(http):
    # libc's default: the rig's nameserver is on 127.0.0.1.
    rig, run = http
    rig["resolv"].unlink()
    assert run("resolve", "v4only.test")[0] == "127.0.0.1"
    assert rig["dns4"].queries


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_an_unreadable_resolv_conf_fails_without_asking_the_local_nameserver(http):
    rig, run = http
    rig["resolv"].chmod(0)
    try:
        assert run("resolve", "v4only.test")[0] == "fail"
    finally:
        rig["resolv"].chmod(0o644)
    assert rig["dns4"].queries == []


def test_no_descriptor_for_resolv_conf_is_a_local_failure(http):
    # The lookup ends at resolv.conf: no nameserver is asked and no socket
    # is tried, so none is counted.
    rig, _ = http
    result = subprocess.run([str(rig["binary"]), "nofile", f"http://v4only.test:{rig['port']}/x"],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert lines[0] == "fail local_resource" and lines[2] == "socket_failures 0"
    assert rig["dns4"].queries == [] and rig["state"]["requests"] == []


# --- The answer cache, on the driver's clock

class Script:
    """test_http script: one command, one reply line."""
    def __init__(self, binary):
        self.process = subprocess.Popen([str(binary), "script", "-"], stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, text=True)

    def __call__(self, line):
        self.process.stdin.write(line + "\n")
        self.process.stdin.flush()
        assert select.select([self.process.stdout], [], [], 20)[0], f"no reply to {line}"
        return self.process.stdout.readline().strip()

    def close(self):
        self.process.stdin.close()
        assert self.process.wait(timeout=20) == 0


@pytest.fixture
def script(http):
    rig, _ = http
    scripts = []

    def start(binary="binary"):
        scripts.append(Script(rig[binary]))
        return scripts[-1]
    yield rig, start
    for s in scripts:
        s.close()


def a_queries(dns):
    return sum(1 for q in dns.queries if struct.unpack(">H", q[-4:-2])[0] == 1)


def test_a_clean_a_reply_is_kept_when_aaaa_is_lost_and_serves_an_outage(script):
    rig, start = script
    resolve = start()
    rig["dns4"].mode = "a_only"
    assert resolve("resolve svc.test") == "127.0.0.1 resource=0"
    asked = len(rig["dns4"].queries)
    assert resolve("resolve svc.test") == "127.0.0.1 resource=0"
    assert len(rig["dns4"].queries) == asked  # kept
    # Past its TTL with DNS gone, the kept answer still serves.
    assert resolve("advance 301") == "ok"
    rig["dns4"].mode = "silent"
    assert resolve("resolve svc.test") == "127.0.0.1 resource=0"
    assert len(rig["dns4"].queries) > asked


@pytest.mark.parametrize("mode", ["aaaa_only", "servfail_a"])
def test_an_answer_without_a_clean_a_reply_serves_once_and_is_not_kept(script, mode):
    # The A reply lost twice, or SERVFAIL for A: the AAAA-only answer is
    # used but asked for again next time. With no kept answer, nothing
    # follows its address.
    rig, start = script
    resolve = start()
    rig["dns4"].mode = mode
    assert resolve("resolve svc.test") == "::1 resource=0"
    asked = a_queries(rig["dns4"])
    assert resolve("resolve svc.test") == "::1 resource=0"
    assert a_queries(rig["dns4"]) > asked


def test_nxdomain_for_aaaa_alone_leaves_a_clean_a_reply_to_keep(script):
    rig, start = script
    resolve = start()
    rig["dns4"].mode = "nxdomain_aaaa"
    assert resolve("resolve svc.test") == "127.0.0.1 resource=0"
    asked = len(rig["dns4"].queries)
    assert resolve("resolve svc.test") == "127.0.0.1 resource=0"
    assert len(rig["dns4"].queries) == asked  # kept


def test_a_clean_empty_a_reply_beside_aaaa_records_is_kept(script):
    # v6only.test has no A record, and its A reply says so cleanly: the
    # AAAA-only answer is the name's whole answer, and kept.
    rig, start = script
    resolve = start()
    assert resolve("resolve v6only.test") == "::1 resource=0"
    asked = len(rig["dns4"].queries)
    assert resolve("resolve v6only.test") == "::1 resource=0"
    assert len(rig["dns4"].queries) == asked


def with_aaaa(name):
    """The zone with name given an AAAA record ::1 beside its A one."""
    return {**ZONE, name: {**ZONE[name], 28: [V6["::1"]]}}


@pytest.mark.parametrize("mode", ["servfail_a", "aaaa_only"])
def test_an_answer_without_a_clean_a_reply_is_followed_by_the_kept_ipv4_address(script, mode):
    # v4only.test is kept with 127.0.0.1; past its TTL it gains an AAAA
    # record and the A reply fails or is lost. The new answer is used, and
    # the IPv4 address from the last clean answer comes after it. (The
    # outage fallback would give 127.0.0.1 alone; the new answer alone,
    # ::1.)
    rig, start = script
    resolve = start()
    assert resolve("resolve v4only.test") == "127.0.0.1 resource=0"
    assert resolve("advance 301") == "ok"
    rig["dns4"].zone = with_aaaa("v4only.test")
    rig["dns4"].mode = mode
    assert resolve("resolve v4only.test") == "::1 127.0.0.1 resource=0"
    # Still not kept: the next lookup asks again.
    asked = a_queries(rig["dns4"])
    assert resolve("resolve v4only.test") == "::1 127.0.0.1 resource=0"
    assert a_queries(rig["dns4"]) > asked


def test_servfail_for_a_beside_aaaa_records_asks_the_next_nameserver(script):
    # The first nameserver fails the A query but answers AAAA: as libc
    # takes the A query on to the next server, the second one is asked, and
    # its whole answer is used and kept.
    rig, start = script
    resolve = start()
    rig["dns4"].mode = "servfail_a"
    rig["dns4"].mode_peers = 1
    rig["resolv"].write_text("nameserver 127.0.0.1\nnameserver 127.0.0.1\n")
    assert resolve("resolve svc.test") == "::1 127.0.0.1 resource=0"
    assert len(rig["dns4"].peers) == 2
    asked = len(rig["dns4"].queries)
    assert resolve("resolve svc.test") == "::1 127.0.0.1 resource=0"
    assert len(rig["dns4"].queries) == asked  # kept


def test_servfail_for_a_on_every_nameserver_uses_the_half_answer_at_once(http):
    # Each server fails A and answers AAAA: none is asked again in the
    # second round, and the AAAA-only answer serves without waiting.
    rig, run = http
    rig["dns4"].mode = "servfail_a"
    rig["resolv"].write_text("nameserver 127.0.0.1\nnameserver 127.0.0.1\n")
    outcome, elapsed = run("resolve", "svc.test")
    assert outcome == "::1" and elapsed < DNS_RETRANS_MS
    assert len(rig["dns4"].queries) == 4


def test_the_kept_answer_for_another_name_is_not_appended(script):
    rig, start = script
    resolve = start()
    assert resolve("resolve v4only.test") == "127.0.0.1 resource=0"
    rig["dns4"].mode = "servfail_a"
    assert resolve("resolve svc.test") == "::1 resource=0"


def test_a_kept_answer_past_its_week_is_not_appended(script):
    rig, start = script
    resolve = start()
    assert resolve("resolve v4only.test") == "127.0.0.1 resource=0"
    assert resolve(f"advance {7 * 86400 + 600}") == "ok"
    rig["dns4"].zone = with_aaaa("v4only.test")
    rig["dns4"].mode = "servfail_a"
    assert resolve("resolve v4only.test") == "::1 resource=0"


def test_a_cname_with_a_shorter_ttl_ends_the_kept_answer(script):
    # shortalias.test is a CNAME kept 60 s to long.test, whose A record is
    # kept an hour: the answer lasts the minute.
    rig, start = script
    resolve = start()
    assert resolve("resolve shortalias.test") == "127.0.0.1 resource=0"
    asked = len(rig["dns4"].queries)
    assert resolve("advance 57") == "ok"
    assert resolve("resolve shortalias.test") == "127.0.0.1 resource=0"
    assert len(rig["dns4"].queries) == asked
    assert resolve("advance 4") == "ok"
    assert resolve("resolve shortalias.test") == "127.0.0.1 resource=0"
    assert len(rig["dns4"].queries) > asked


def test_a_cname_off_the_chain_does_not_shorten_the_ttl(script):
    # offchain.test's reply carries a 60 s CNAME owned by another name: the
    # answer lasts its own A record's hour.
    rig, start = script
    resolve = start()
    assert resolve("resolve offchain.test") == "127.0.0.1 resource=0"
    asked = len(rig["dns4"].queries)
    assert resolve("advance 61") == "ok"
    assert resolve("resolve offchain.test") == "127.0.0.1 resource=0"
    assert len(rig["dns4"].queries) == asked
    assert resolve(f"advance {3600 - 61 - 3}") == "ok"
    assert resolve("resolve offchain.test") == "127.0.0.1 resource=0"
    assert len(rig["dns4"].queries) == asked
    assert resolve("advance 4") == "ok"
    assert resolve("resolve offchain.test") == "127.0.0.1 resource=0"
    assert len(rig["dns4"].queries) > asked


@pytest.mark.parametrize("mode", ["nxdomain_a", "nodata"])
def test_a_name_with_no_address_fails_and_leaves_the_kept_answer(script, mode):
    # v4only.test is kept with 127.0.0.1. Past its TTL, NXDOMAIN for A, or a
    # clean reply with no record to either type, says the name has no
    # address now: the lookup fails without the kept address and without
    # asking the second nameserver, as libc's does. The kept answer stays:
    # a later lookup that no nameserver answers is served from it.
    rig, start = script
    resolve = start()
    assert resolve("resolve v4only.test") == "127.0.0.1 resource=0"
    assert resolve("advance 301") == "ok"
    rig["resolv"].write_text("nameserver 127.0.0.1\nnameserver 127.0.0.1\n")
    rig["dns4"].mode = mode
    asked, peers = len(rig["dns4"].queries), len(rig["dns4"].peers)
    assert resolve("resolve v4only.test") == "fail resource=0"
    assert len(rig["dns4"].read(asked + 2)) == asked + 2
    assert len(rig["dns4"].peers) == peers + 1
    rig["dns4"].mode = "servfail"
    asked = len(rig["dns4"].queries)
    assert resolve("resolve v4only.test") == "127.0.0.1 resource=0"
    assert len(rig["dns4"].queries) == asked + 4


def test_aaaa_records_beside_nxdomain_for_a_go_first_with_the_kept_ipv4_behind(script):
    # v4only.test is kept with 127.0.0.1; past its TTL it gains an AAAA
    # record and its A query gets NXDOMAIN. The AAAA address is used at
    # once with the kept IPv4 address behind it, and is not kept: the kept
    # answer is still the one a later outage gets.
    rig, start = script
    resolve = start()
    assert resolve("resolve v4only.test") == "127.0.0.1 resource=0"
    assert resolve("advance 301") == "ok"
    rig["resolv"].write_text("nameserver 127.0.0.1\nnameserver 127.0.0.1\n")
    rig["dns4"].zone = with_aaaa("v4only.test")
    rig["dns4"].mode = "nxdomain_a"
    peers = len(rig["dns4"].peers)
    assert resolve("resolve v4only.test") == "::1 127.0.0.1 resource=0"
    assert len(rig["dns4"].peers) == peers + 1
    rig["dns4"].mode = "servfail"
    assert resolve("resolve v4only.test") == "127.0.0.1 resource=0"


def test_failing_nameservers_leave_the_kept_answer_to_serve(script):
    # Every nameserver failing is no answer, not a missing name.
    rig, start = script
    resolve = start()
    assert resolve("resolve v4only.test") == "127.0.0.1 resource=0"
    assert resolve("advance 301") == "ok"
    rig["dns4"].mode = "servfail"
    asked = len(rig["dns4"].queries)
    assert resolve("resolve v4only.test") == "127.0.0.1 resource=0"
    assert len(rig["dns4"].queries) > asked


def test_a_missing_name_leaves_the_kept_answer_for_another(script):
    rig, start = script
    resolve = start()
    assert resolve("resolve v4only.test") == "127.0.0.1 resource=0"
    assert resolve("advance 301") == "ok"
    assert resolve("resolve missing.test") == "fail resource=0"
    rig["dns4"].mode = "servfail"
    assert resolve("resolve v4only.test") == "127.0.0.1 resource=0"


def test_a_kept_answer_serves_a_dns_outage_for_a_week(script):
    rig, start = script
    resolve = start()
    assert resolve("resolve v4only.test") == "127.0.0.1 resource=0"
    rig["dns4"].mode = "silent"
    assert resolve("advance 301") == "ok"  # TTL 300
    asked = len(rig["dns4"].queries)
    assert resolve("resolve v4only.test") == "127.0.0.1 resource=0"
    assert len(rig["dns4"].queries) > asked  # asked first, then fell back
    assert resolve(f"advance {7 * 86400 - 301 - 600}") == "ok"
    assert resolve("resolve v4only.test") == "127.0.0.1 resource=0"
    assert resolve("advance 1200") == "ok"
    assert resolve("resolve v4only.test") == "fail resource=0"


@pytest.mark.parametrize("name,kept_for", [("ttl0.test", 60), ("ttlbig.test", 86400), ("ttlneg.test", 60)])
def test_ttls_are_clamped_to_a_minute_and_a_day(script, name, kept_for):
    rig, start = script
    resolve = start()
    assert resolve(f"resolve {name}") == "127.0.0.1 resource=0"
    asked = len(rig["dns4"].queries)
    assert resolve(f"advance {kept_for - 3}") == "ok"
    assert resolve(f"resolve {name}") == "127.0.0.1 resource=0"
    assert len(rig["dns4"].queries) == asked
    assert resolve("advance 4") == "ok"
    assert resolve(f"resolve {name}") == "127.0.0.1 resource=0"
    assert len(rig["dns4"].queries) > asked


def test_the_resource_flag_reports_only_a_failed_lookup(script):
    # No descriptor for resolv.conf: with a kept answer the lookup succeeds
    # and reports no shortage; without one it fails and reports it.
    rig, start = script
    resolve = start()
    assert resolve("resolve v4only.test") == "127.0.0.1 resource=0"
    assert resolve("advance 301") == "ok"
    assert resolve("limit 3") == "ok"
    assert resolve("resolve v4only.test") == "127.0.0.1 resource=0"
    assert resolve("resolve svc.test") == "fail resource=1"
    assert resolve("limit 0") == "ok"
    assert resolve("resolve svc.test") == "::1 127.0.0.1 resource=0"


# --- One address after another

def held_listener(family, port):
    """A TCP socket bound, not yet listening, to the loopback of family at
    a port held_refusing_ports holds."""
    sock = socket.socket(family)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if family == socket.AF_INET6:
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
    sock.bind(("::1" if family == socket.AF_INET6 else "127.0.0.1", port))
    return sock


def read_request(conn):
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = conn.recv(4096)
        if not chunk:
            return None
        data += chunk
    head, _, body = data.partition(b"\r\n\r\n")
    length = re.search(rb"(?im)^content-length:\s*(\d+)", head)
    while length and len(body) < int(length[1]):
        body += conn.recv(4096)
    return head.split(b"\r\n")[0].decode(), body


def serve(listener, behavior, log):
    """Accepts on listener, answering each connection by behavior and
    logging what arrived, until the returned stop() (accept_loop's) is
    called."""
    listener.listen(8)

    def handle(conn):
        with conn:
            conn.settimeout(10)
            try:
                if behavior == "reset":
                    log.append(conn.recv(16))  # part of the request, then a reset
                    conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                    return
                log.append(read_request(conn))
                if behavior == "ok":
                    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
                elif behavior == "status500":
                    conn.sendall(b"HTTP/1.1 500 Error\r\nContent-Length: 2\r\n\r\nno")
                elif behavior == "stall":
                    while conn.recv(4096):  # until the client gives up and closes
                        pass
                elif behavior == "partial_head":
                    conn.sendall(b"HTTP/1.1 500")  # some of an answer, then the close
                elif behavior == "head_reset":
                    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\n\r\nok")
                    conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                elif behavior == "head_stall":
                    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\n\r\nok")
                    while conn.recv(4096):
                        pass
                elif behavior == "body_stall":
                    conn.sendall(b"HTTP/1.0 200 OK\r\n\r\npart")  # read to the close, which never comes
                    while conn.recv(4096):
                        pass
                elif behavior == "short_body":
                    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\n\r\nok")
            except OSError:
                pass
    return accept_loop(listener, handle)


@pytest.fixture
def dual(http):
    # One port on both loopbacks, held refusing (tests/loopback.py): a leg
    # set up "refused" leaves it so, and every other leg listens on it.
    rig, run = http
    logs = {"v6": [], "v4": []}
    sockets, stops = [], []

    def setup(v6_behavior, v4_behavior):
        for family, behavior, log in ((socket.AF_INET6, v6_behavior, logs["v6"]),
                                      (socket.AF_INET, v4_behavior, logs["v4"])):
            if behavior == "refused":
                continue
            sock = held_listener(family, port)
            sockets.append(sock)
            if behavior == "blackhole":
                # A full accept queue: the kernel drops further SYNs, so a
                # connect gets no answer at all.
                sockets.extend(fill_listen_queue(sock))
            else:
                stops.append(serve(sock, behavior, log))
        return run("post", f"http://svc.test:{port}/v1/router-heartbeats", binary="wide_binary")
    with held_refusing_ports(1) as [port]:
        yield setup, logs
        for stop in stops:
            stop()
        for sock in sockets:
            sock.close()


pytestmark_ipv6 = pytest.mark.skipif(not IPV6, reason="no IPv6 loopback")


@pytestmark_ipv6
def test_an_unanswered_ipv6_connect_falls_back_to_ipv4(dual):
    setup, logs = dual
    outcome, elapsed = setup("blackhole", "ok")
    assert outcome == "ok ok"
    assert logs["v4"] == [("POST /v1/router-heartbeats HTTP/1.0", b"{}")]
    # The connect limit, not the per-address one.
    assert CONNECT_MS - 100 <= elapsed < ADDRESS_MS - 100


@pytestmark_ipv6
def test_an_ipv6_server_that_stalls_after_connecting_falls_back_to_ipv4(dual):
    # The POST reaches IPv6 and gets no answer; at the per-address budget it
    # goes again over IPv4, with the rest of the request's time.
    setup, logs = dual
    outcome, elapsed = setup("stall", "ok")
    assert outcome == "ok ok"
    assert logs["v6"] == [("POST /v1/router-heartbeats HTTP/1.0", b"{}")]
    assert logs["v4"] == [("POST /v1/router-heartbeats HTTP/1.0", b"{}")]
    assert ADDRESS_MS - 100 <= elapsed < ADDRESS_MS + 1500


@pytestmark_ipv6
def test_an_ipv6_reset_mid_request_falls_back_to_ipv4(dual):
    setup, logs = dual
    outcome, elapsed = setup("reset", "ok")
    # At once, not at the end of the per-address limit.
    assert outcome == "ok ok" and elapsed < ADDRESS_MS - 100
    assert logs["v6"] and logs["v4"] == [("POST /v1/router-heartbeats HTTP/1.0", b"{}")]


@pytestmark_ipv6
def test_an_ipv6_close_without_an_answer_falls_back_to_ipv4(dual):
    setup, logs = dual
    outcome, _ = setup("close", "ok")
    assert outcome == "ok ok"
    assert len(logs["v6"]) == 1 and len(logs["v4"]) == 1


@pytestmark_ipv6
def test_a_complete_error_answer_is_not_retried_on_ipv4(dual):
    # Response table: BODY_LENGTH, n bytes, other status (answered).
    setup, logs = dual
    outcome, _ = setup("status500", "ok")
    assert outcome == "fail server"
    assert len(logs["v6"]) == 1 and logs["v4"] == []


@pytestmark_ipv6
def test_a_server_that_was_reached_outranks_a_later_network_failure(dual):
    # Response table: HEAD, close, some bytes (SERVER, next address).
    # IPv6 sent part of an answer and closed; IPv4 refused.
    setup, logs = dual
    outcome, _ = setup("partial_head", "refused")
    assert outcome == "fail server"
    assert len(logs["v6"]) == 1


@pytestmark_ipv6
def test_a_close_before_any_byte_is_the_network_not_the_server(dual):
    # Response table: HEAD, close, no byte.
    # A middlebox that accepts and hangs up on IPv6 says nothing about the
    # server: with IPv4 refused, the request failed on the network.
    setup, logs = dual
    outcome, _ = setup("close", "refused")
    assert outcome == "fail network"
    assert len(logs["v6"]) == 1


@pytestmark_ipv6
@pytest.mark.parametrize("v6", ["head_reset", "head_stall", "body_stall"])
def test_a_reset_or_stall_after_the_head_moves_on_to_the_next_address(dual, v6):
    # Response table: any phase, reset or stall; BODY_LENGTH for head_reset
    # and head_stall, BODY_TO_CLOSE for body_stall.
    setup, logs = dual
    outcome, _ = setup(v6, "ok")
    assert outcome == "ok ok"
    assert len(logs["v6"]) == 1 and len(logs["v4"]) == 1


@pytestmark_ipv6
def test_a_close_that_cuts_the_body_short_is_the_servers_answer(dual):
    # Response table: BODY_LENGTH, close before n bytes.
    # The server ended its answer: another address would only repeat it.
    setup, logs = dual
    outcome, _ = setup("short_body", "ok")
    assert outcome == "fail server"
    assert len(logs["v6"]) == 1 and logs["v4"] == []


@pytestmark_ipv6
def test_a_stalled_ipv6_path_reaches_ipv4_through_the_kept_address(script):
    # One process: a heartbeat to v4only.test over IPv4 keeps the answer;
    # past its TTL the name gains an AAAA record whose path stalls, and the A
    # reply fails. The request goes to IPv6, then at the per-address budget
    # to the kept IPv4 address. Without it, IPv6 would be the last address
    # and have the whole deadline.
    rig, start = script
    logs = {"v6": [], "v4": []}
    with held_refusing_ports(1) as [port], contextlib.ExitStack() as stack:
        stack.callback(serve(held_listener(socket.AF_INET6, port), "stall", logs["v6"]))
        stack.callback(serve(held_listener(socket.AF_INET, port), "ok", logs["v4"]))
        url = f"http://v4only.test:{port}/v1/router-heartbeats"
        post = start("wide_binary")
        assert post(f"post {url}").startswith("ok ok ")
        assert post("advance 301") == "ok"
        rig["dns4"].zone = with_aaaa("v4only.test")
        rig["dns4"].mode = "servfail_a"
        outcome, elapsed = post(f"post {url}").split(" elapsed_ms=")
    assert outcome == "ok ok"
    assert ADDRESS_MS - 100 <= int(elapsed) < ADDRESS_MS + 1500
    request = ("POST /v1/router-heartbeats HTTP/1.0", b"{}")
    assert logs["v6"] == [request] and logs["v4"] == [request, request]


@pytest.mark.parametrize("name", ["dup.test", "dupalias.test"])
def test_an_address_given_twice_gets_one_attempt(http, name):
    # The server accepts and closes without answering, which moves a request
    # on to its next address: a second copy of the address would connect
    # again.
    rig, run = http
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    log = []
    stop = serve(listener, "close", log)
    try:
        outcome, _ = run("post", f"http://{name}:{listener.getsockname()[1]}/x")
    finally:
        stop()
    assert outcome == "fail network"  # a close before any byte of an answer
    assert log == [("POST /x HTTP/1.0", b"{}")]


def test_failures_across_addresses_rank_server_then_local_resource_then_network(rig):
    result = subprocess.run([str(rig["binary"]), "merge"], capture_output=True, text=True, timeout=20)
    assert (result.returncode, result.stdout) == (0, "ok\n"), result.stdout + result.stderr


def test_each_combination_of_dns_replies_gets_the_verdict_in_the_table(rig):
    # dns.c's judge() on every A and AAAA reply state, with no address, an
    # IPv4 one or an IPv6 one, against the table beside it.
    result = subprocess.run([str(rig["binary"]), "judge"], capture_output=True, text=True, timeout=20)
    assert (result.returncode, result.stdout) == (0, "ok\n"), result.stdout + result.stderr
