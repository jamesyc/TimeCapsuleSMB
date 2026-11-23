"""telemetry/http.c and dns.c: the fork-free heartbeat client, against real
HTTP and DNS servers on loopback (both families)."""
import re
import socket
import socketserver
import struct
import subprocess
import threading
import time

import pytest

from tests.native.build import ROOT, compile_modules

DEADLINE_MS = 3000
CONNECT_MS = 1000
DNS_SERVER_MS = 500
# The TERM test's own build: a deadline far past the 1 s stop-flag poll, so
# stopping on TERM and running to the deadline cannot be mistaken.
LONG_DEADLINE_MS = 20000


def have_ipv6_loopback():
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as s:
            s.bind(("::1", 0))
        return True
    except OSError:
        return False


IPV6 = have_ipv6_loopback()


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
                }[mode]
                if reply:
                    self.request.sendall(reply)
            except OSError:
                pass
    return Handler


def dns_name(data, pos):
    labels = []
    while data[pos]:
        n = data[pos]
        labels.append(data[pos + 1:pos + 1 + n].decode())
        pos += n + 1
    return ".".join(labels), pos + 1


def encode_name(name):
    return b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\0"


def record(name, rtype, rdata, ttl=300):
    return encode_name(name) + struct.pack(">HHIH", rtype, 1, ttl, len(rdata)) + rdata


# Answers by question name: A and AAAA rdata lists, a CNAME target, or NXDOMAIN.
ZONE = {
    "svc.test": {1: [socket.inet_pton(socket.AF_INET, "127.0.0.1")], 28: [socket.inet_pton(socket.AF_INET6, "::1")]},
    "v4only.test": {1: [socket.inet_pton(socket.AF_INET, "127.0.0.1")]},
    "v6only.test": {28: [socket.inet_pton(socket.AF_INET6, "::1")]},
}


def dns_reply(query, spoof, mode="answer"):
    qid = query[:2]
    name, pos = dns_name(query, 12)
    qtype = struct.unpack(">H", query[pos:pos + 2])[0]
    question = query[12:pos + 4]
    answers = []
    rcode = 0
    target = name
    if name == "alias.test":
        # A CNAME chain: alias.test -> middle.test -> svc.test, out of order.
        answers.append(record("middle.test", 5, encode_name("svc.test")))
        answers.append(record("alias.test", 5, encode_name("middle.test")))
        target = "svc.test"
    if name == "missing.test":
        rcode = 3
    for rdata in ZONE.get(target, {}).get(qtype, []):
        answers.append(record(target, qtype, rdata))
    if name == "decoy.test" and qtype == 1:
        # An address for some other name in the answer must be ignored.
        answers.append(record("elsewhere.test", 1, socket.inet_pton(socket.AF_INET, "10.9.9.9")))
        answers.append(record("decoy.test", 1, socket.inet_pton(socket.AF_INET, "127.0.0.1")))
    header = qid + struct.pack(">HHHHH", 0x8180 | rcode, 1, len(answers), 0, 0)
    replies = [header + question + b"".join(answers)]
    if spoof:
        wrong = bytes([qid[0] ^ 0xff, qid[1]])
        replies.insert(0, wrong + replies[0][2:])
    if mode == "malformed_first":
        # The right ID, but the answer count runs past the end of the packet.
        bad = qid + struct.pack(">HHHHH", 0x8180, 1, 3, 0, 0) + question
        replies.insert(0, bad)
    if mode == "duplicate":
        replies.append(replies[-1])
    return replies


class Dns:
    def __init__(self, family, host, port):
        self.sock = socket.socket(family, socket.SOCK_DGRAM)
        self.sock.bind((host, port))
        self.port = self.sock.getsockname()[1]
        self.mode = "answer"
        self.queries = []
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        seen = set()
        while True:
            try:
                data, peer = self.sock.recvfrom(1500)
            except OSError:
                return
            self.queries.append(data)
            qtype = struct.unpack(">H", data[-4:-2])[0]
            if self.mode == "silent":
                continue
            if self.mode == "drop_first" and data[:2] not in seen:
                seen.add(data[:2])  # lose the first copy of each query
                continue
            if self.mode == "aaaa_only" and qtype == 1:
                continue
            for reply in dns_reply(data, self.mode == "spoof", self.mode):
                self.sock.sendto(reply, peer)


@pytest.fixture(scope="module")
def rig(tmp_path_factory):
    root = tmp_path_factory.mktemp("http")
    dns4 = Dns(socket.AF_INET, "127.0.0.1", 0)
    dns6 = Dns(socket.AF_INET6, "::1", dns4.port) if IPV6 else None
    resolv = root / "resolv.conf"
    resolv.write_text("nameserver 127.0.0.1\n")
    native = ROOT / "build/native"
    def build(path, deadline_ms):
        compile_modules(path, ("native/telemetry/http.c", "native/telemetry/dns.c", "native/common/acp.c",
                               "native/telemetry/errclass.c"),
                        flags=("-I", str(native / "telemetry"), "-I", str(native / "common"), "-I", str(native),
                               f"-DTC_HTTP_DEADLINE_MS={deadline_ms}", f"-DTC_HTTP_CONNECT_MS={CONNECT_MS}",
                               f"-DTC_DNS_SERVER_MS={DNS_SERVER_MS}", f"-DTC_DNS_PORT={dns4.port}",
                               f'-DTC_RESOLV_CONF="{resolv}"'),
                        extra_sources=(ROOT / "tests/native/unit/test_http.c",))
        return path
    binary = build(root / "http", DEADLINE_MS)
    long_binary = build(root / "http-long", LONG_DEADLINE_MS)
    state = {"mode": "ok", "requests": []}
    http4 = Http(("127.0.0.1", 0), responder(state))
    threading.Thread(target=http4.serve_forever, daemon=True).start()
    http6 = None
    if IPV6:
        http6 = Http6(("::1", http4.server_address[1]), responder(state))
        threading.Thread(target=http6.serve_forever, daemon=True).start()
    yield {"binary": binary, "long_binary": long_binary, "state": state, "port": http4.server_address[1], "resolv": resolv,
           "dns4": dns4, "dns6": dns6}
    http4.shutdown()
    if http6:
        http6.shutdown()


@pytest.fixture
def http(rig):
    rig["state"]["mode"] = "ok"
    rig["state"]["requests"].clear()
    rig["dns4"].mode = "answer"
    rig["dns4"].queries.clear()
    if rig["dns6"]:
        rig["dns6"].mode = "answer"
        rig["dns6"].queries.clear()
    rig["resolv"].write_text("nameserver 127.0.0.1\n")

    def run(command, url, *extra):
        result = subprocess.run([str(rig["binary"]), command, url, *extra], capture_output=True, text=True,
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


@pytest.mark.parametrize("mode", ["status404", "status500", "redirect", "big", "big_close", "short", "chunked",
                                  "early_close", "garbage"])
def test_anything_but_a_complete_2xx_response_within_the_limit_fails(http, mode):
    rig, run = http
    rig["state"]["mode"] = mode
    outcome, _ = run("post", f"http://127.0.0.1:{rig['port']}/v1/router-heartbeats")
    assert outcome == "fail server"


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
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    outcome, elapsed = run("post", f"http://127.0.0.1:{port}/x")
    assert outcome == "fail network" and elapsed < CONNECT_MS


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
    rig, run = http
    if rig["dns6"]:
        rig["resolv"].write_text("nameserver 127.0.0.1\nnameserver ::1\n")
    outcome, _ = run("post", f"http://missing.test:{rig['port']}/x")
    assert outcome == "fail network"
    if rig["dns6"]:
        assert rig["dns6"].queries == []


def test_a_silent_nameserver_hands_over_to_the_next(http):
    rig, run = http
    rig["dns4"].mode = "silent"
    rig["resolv"].write_text("nameserver 127.0.0.1\nnameserver 127.0.0.1\n")
    outcome, elapsed = run("post", f"http://v4only.test:{rig['port']}/x")
    # Both copies of the silent server use up their time; nothing answers.
    assert outcome == "fail network" and 2 * DNS_SERVER_MS - 100 <= elapsed < 2 * DNS_SERVER_MS + 1500


@pytest.mark.skipif(not IPV6, reason="no IPv6 loopback")
def test_an_ipv6_nameserver_answers_after_a_silent_ipv4_one(http):
    rig, run = http
    rig["dns4"].mode = "silent"
    rig["resolv"].write_text("nameserver 127.0.0.1\nnameserver ::1\n")
    outcome, elapsed = run("post", f"http://v4only.test:{rig['port']}/x")
    assert outcome == "ok ok" and elapsed >= DNS_SERVER_MS - 100
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
    # svc.test has AAAA ::1 and A 127.0.0.1; tried IPv6 first. With no
    # IPv6 server on that port (or none at all), the IPv4 one answers.
    rig, run = http
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = Http(("127.0.0.1", port), responder(rig["state"]))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        assert run("post", f"http://svc.test:{port}/x")[0] == "ok ok"
    finally:
        server.shutdown()
        server.server_close()


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
    # The resend goes out halfway through the server's time.
    assert DNS_SERVER_MS / 2 - 100 <= elapsed < DNS_SERVER_MS + 1500
    assert len(rig["dns4"].queries) == 4  # A and AAAA, each sent twice


@pytest.mark.skipif(not IPV6, reason="no IPv6 loopback")
def test_half_an_answer_serves_the_request_but_is_not_kept(http):
    # The A reply never comes: the AAAA-only answer is used once, not cached,
    # so a device whose IPv6 cannot reach the server is not stuck with it.
    rig, run = http
    rig["dns4"].mode = "aaaa_only"
    assert run("twice", f"http://svc.test:{rig['port']}/x")[0] == "ok ok"
    a_queries = [q for q in rig["dns4"].queries if struct.unpack(">H", q[-4:-2])[0] == 1]
    assert len(a_queries) == 4  # two requests, each asking twice


def test_a_malformed_reply_with_our_id_does_not_end_the_query(http):
    rig, run = http
    rig["dns4"].mode = "malformed_first"
    assert run("resolve", "v4only.test")[0] == "127.0.0.1"


def test_a_repeated_reply_adds_no_addresses(http):
    rig, run = http
    rig["dns4"].mode = "duplicate"
    expected = "::1 127.0.0.1"
    assert run("resolve", "svc.test")[0] == expected


def test_term_stops_a_request_within_a_second(http):
    rig, _ = http
    rig["state"]["mode"] = "silent_long"
    process = subprocess.Popen([str(rig["long_binary"]), "post", f"http://127.0.0.1:{rig['port']}/x"],
                               stdout=subprocess.PIPE, text=True)
    deadline = time.monotonic() + 5
    while not rig["state"]["requests"] and time.monotonic() < deadline:
        time.sleep(0.01)
    started = time.monotonic()
    process.terminate()
    out, _ = process.communicate(timeout=30)
    assert out.splitlines()[0] == "fail cancelled"
    # The stop flag is checked every second; without it the request would
    # run to its 20 s deadline.
    assert time.monotonic() - started < LONG_DEADLINE_MS / 2000


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
