"""tests.samba.cut_device: the proxy cuts one WRITE the way macOS does and
passes everything else through, and the analysis judges each cut connection."""
from __future__ import annotations

import socket
import struct
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.samba import cut_device, links_device


def pdu(command: int, mid: int, body: bytes = b"", flags: int = 0, status: int = 0, next_command: int = 0) -> bytes:
    header = bytearray(64)
    header[0:4] = b"\xfeSMB"
    struct.pack_into("<HHIHH", header, 4, 64, 1, status, command, 1)
    struct.pack_into("<IIQ", header, 16, flags, next_command, mid)
    return bytes(header) + body


def nbt(payload: bytes) -> bytes:
    return len(payload).to_bytes(4, "big") + payload


class HeadersTest(unittest.TestCase):
    def test_reads_each_pdu_of_a_compound(self) -> None:
        create = pdu(5, 7, b"c" * 16)
        compound = pdu(5, 7, b"c" * 16, next_command=len(create)) + pdu(6, 8, b"x" * 8)
        self.assertEqual([(c, m) for c, m, _, _ in cut_device.headers(compound)], [(5, 7), (6, 8)])

    def test_anything_but_plain_smb2_is_not_parsed(self) -> None:
        self.assertIsNone(cut_device.headers(b"\xfdSMB" + bytes(60)))  # encrypted
        self.assertIsNone(cut_device.headers(pdu(9, 1)[:40]))
        self.assertIsNone(cut_device.headers(b""))

    def test_a_create_close_compound_is_idempotent_and_a_lone_create_is_not(self) -> None:
        self.assertTrue(cut_device.non_idempotent([5]))
        self.assertTrue(cut_device.non_idempotent([6]))
        self.assertTrue(cut_device.non_idempotent([10]))
        self.assertFalse(cut_device.non_idempotent([5, 16, 6]))
        self.assertFalse(cut_device.non_idempotent([9]))
        self.assertFalse(cut_device.non_idempotent([13]))


def events(conn: int, *items) -> list[dict]:
    """(seconds, kind, detail) -> proxy events for one connection."""
    out = [{"ev": "open", "conn": conn, "t": 0.0}]
    for t, kind, detail in items:
        e = {"ev": kind, "conn": conn, "t": 100.0 + t}
        if kind == "cut":
            e.update(mid=50, len=524400, keep=100000)
        elif kind == "req":
            e["h"] = detail
        elif kind == "rsp":
            e["r"] = detail
        out.append(e)
    return out


class AnalyseTest(unittest.TestCase):
    def test_smbd_ending_it_with_everything_answered_passes(self) -> None:
        rows = cut_device.analyse(events(1, (0, "cut", None), (0.01, "req", [(9, 58)]), (0.02, "rsp", [(58, 1, 9)]),
                                         (0.6, "s_eof", None), (0.6003, "c_eof", None)))
        self.assertEqual(rows[0]["ended_by"], "smbd")
        self.assertEqual(rows[0]["ended"], 0.6)
        self.assertEqual(rows[0]["fail"], [])
        self.assertEqual(rows[0]["missing"], 524400 - 100000)

    def test_the_client_giving_up_first_fails(self) -> None:
        # The stalled-WRITE case before 0072 checked it: only ECHOs, then the Mac closes.
        rows = cut_device.analyse(events(1, (0, "cut", None), (10, "req", [(13, 51)]), (20, "req", [(13, 52)]),
                                         (46.0, "c_eof", None), (46.005, "s_eof", None)))
        self.assertEqual(rows[0]["ended_by"], "client")
        self.assertIn("the client gave up before smbd ended the connection", rows[0]["fail"])
        self.assertEqual([u["commands"] for u in rows[0]["unanswered"]], [["ECHO"], ["ECHO"]])

    def test_a_cut_connection_still_open_when_the_run_stops_fails(self) -> None:
        rows = cut_device.analyse(events(1, (0, "cut", None), (0.1, "req", [(9, 51)]), (0.2, "rsp", [(51, 1, 9)])))
        self.assertIsNone(rows[0]["ended_by"])
        self.assertIsNone(rows[0]["ended"])
        self.assertEqual(rows[0]["fail"], ["the connection never ended"])

    def test_endings_at_the_cap_come_from_smbds_own_line(self) -> None:
        log = ("  tc_desync_tick: tc_desync: ending the connection 0.8 s after the short WRITE: nothing else "
               "outstanding (rest answered, 1 short WRITEs, 7 requests read after the first)\n"
               "  tc_desync_tick: tc_desync: ending the connection 60.0 s after the short WRITE with 1 requests "
               "pending: SMB2_OP_LOCK, 0 bytes unread (rest answered, 1 short WRITEs, 3 requests read after the "
               "first)\n")
        self.assertEqual(cut_device.capped(log), 1)
        self.assertEqual(cut_device.capped(log.splitlines()[0]), 0)
        self.assertIn("FAIL: smbd ended 1 connection(s) at its 60 s cap", cut_device.summary([], 1))
        self.assertIn("RESULT cuts=0 fail=1", cut_device.summary([], 1))

    def test_a_lone_create_unanswered_at_the_end_fails_and_a_compound_does_not(self) -> None:
        lone = cut_device.analyse(events(1, (0, "cut", None), (0.5, "req", [(5, 51)]),
                                         (0.55, "s_eof", None), (0.5503, "c_eof", None)))
        self.assertEqual(len(lone[0]["fail"]), 1)
        self.assertIn("Time Machine", lone[0]["fail"][0])
        compound = cut_device.analyse(events(1, (0, "cut", None), (0.5, "req", [(5, 51), (16, 52), (6, 53)]),
                                             (0.55, "s_eof", None), (0.5503, "c_eof", None)))
        self.assertEqual(compound[0]["fail"], [])
        self.assertEqual(compound[0]["unanswered"][0]["commands"], ["CREATE", "GETINFO", "CLOSE"])

    def test_interim_replies_notify_and_cancel_do_not_count_as_answered_or_missing(self) -> None:
        rows = cut_device.analyse(events(1, (0, "cut", None), (0.1, "req", [(10, 51)]), (0.1, "req", [(15, 52)]),
                                         (0.1, "req", [(12, 51)]), (0.2, "rsp", [(51, 0, 10)]),
                                         (0.7, "s_eof", None), (0.7003, "c_eof", None)))
        self.assertEqual([u["commands"] for u in rows[0]["unanswered"]], [["LOCK"]])

    def test_a_request_sent_before_the_cut_and_never_answered_counts_too(self) -> None:
        # A CREATE still waiting when the connection ends fails a Time Machine
        # reconnect whether it went out before the cut or after.
        mixed = events(1, (-1, "req", [(5, 40)]), (-0.5, "req", [(9, 41)]), (-0.4, "rsp", [(41, 1, 9)]),
                       (0, "cut", None), (0.7, "s_eof", None), (0.7003, "c_eof", None))
        mixed += events(2, (1, "req", [(5, 1)]), (2, "s_eof", None))
        rows = cut_device.analyse(mixed)
        self.assertEqual([r["conn"] for r in rows], [1])
        self.assertEqual(rows[0]["unanswered"], [{"at": -1.0, "commands": ["CREATE"]}])
        self.assertEqual(rows[0]["after"], {})
        self.assertEqual(len(rows[0]["fail"]), 1)
        self.assertIn("RESULT cuts=1 fail=1", cut_device.summary(rows))


class FakeServer:
    """One connection at a time: reads until the client's last frame, answers
    with a reply and a FIN, then reads until the client's FIN."""

    def __init__(self, last: bytes, reply: bytes) -> None:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.port = self.sock.getsockname()[1]
        self.last, self.reply = last, reply
        self.received: list[bytes] = []
        self.done = threading.Event()
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self) -> None:
        conn, _ = self.sock.accept()
        with conn:
            data = b""
            while not data.endswith(self.last):
                chunk = conn.recv(65536)
                if not chunk:
                    break
                data += chunk
            conn.sendall(self.reply)
            conn.shutdown(socket.SHUT_WR)
            while chunk := conn.recv(65536):
                data += chunk
            self.received.append(data)
        self.done.set()


class ProxyTest(unittest.TestCase):
    def test_one_write_is_cut_the_next_request_follows_and_fins_pass_through(self) -> None:
        write = nbt(pdu(9, 5, b"W" * 48 + b"D" * 70000))
        echo = nbt(pdu(13, 6, b"\x04\x00\x00\x00"))
        reply = nbt(pdu(13, 6, b"\x04\x00\x00\x00", flags=1))
        server = FakeServer(echo, reply)
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "proxy.jsonl"
            proxy = cut_device.Proxy("127.0.0.1", log, port=0, server_port=server.port, settle=0, max_cuts=1)
            proxy.start()
            self.assertNotEqual(proxy.port, 0)
            try:
                with socket.create_connection(("127.0.0.1", proxy.port), timeout=10) as client:
                    client.sendall(write + echo)
                    got = b""
                    while chunk := client.recv(65536):
                        got += chunk
                    # Cut, but its connection has not ended yet.
                    self.assertEqual(proxy.cuts, 1)
                    self.assertFalse(proxy.cuts_done())
                    client.shutdown(socket.SHUT_WR)
                self.assertTrue(server.done.wait(10))
                deadline = time.monotonic() + 10
                while not proxy.cuts_done() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(proxy.cuts_done())
            finally:
                proxy.stop()
            rows = cut_device.load(Path(tmp))
        cut = next(e for e in rows if e["ev"] == "cut")
        # The WRITE's NBT length is unchanged, its tail is gone, the ECHO follows whole.
        self.assertEqual(server.received[0], write[:4 + cut["keep"]] + echo)
        self.assertEqual(cut["len"], len(write) - 4)
        self.assertTrue(64 + 48 + 4096 <= cut["keep"] <= cut["len"] - 4096)
        # The reply reached the client, then the server's FIN as the end of the stream.
        self.assertEqual(got, reply)
        kinds = [e["ev"] for e in rows]
        self.assertLess(kinds.index("s_eof"), kinds.index("c_eof"))
        self.assertEqual(next(e for e in rows if e["ev"] == "rsp")["r"], [[6, 1, 13]])

    def test_after_the_last_cut_writes_pass_whole(self) -> None:
        write = nbt(pdu(9, 5, b"W" * 48 + b"D" * 70000))
        reply = nbt(pdu(9, 5, b"\x11" + bytes(16), flags=1))
        server = FakeServer(write, reply)
        with tempfile.TemporaryDirectory() as tmp:
            proxy = cut_device.Proxy("127.0.0.1", Path(tmp) / "proxy.jsonl", port=0,
                                     server_port=server.port, settle=0, max_cuts=0)
            proxy.start()
            try:
                with socket.create_connection(("127.0.0.1", proxy.port), timeout=10) as client:
                    client.sendall(write)
                    while client.recv(65536):
                        pass
                    client.shutdown(socket.SHUT_WR)
                self.assertTrue(server.done.wait(10))
            finally:
                proxy.stop()
        self.assertEqual(server.received[0], write)
        self.assertEqual(proxy.cuts, 0)


class MountTest(unittest.TestCase):
    device = SimpleNamespace(env={"TC_PASSWORD": "p@ss word", "TC_SAMBA_USER": "admin"}, host="192.168.1.218",
                             share="4TB")

    def test_the_proxy_address_and_options_replace_the_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(links_device.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            links_device.mount("smb", self.device, Path(tmp) / "m", server="127.0.0.1:4450",
                               options="nobrowse,forcenewsession")
            cmd = run.call_args.args[0]
        self.assertEqual(cmd[:3], ["mount_smbfs", "-o", "nobrowse,forcenewsession"])
        self.assertEqual(cmd[3], "//admin:p%40ss%20word@127.0.0.1:4450/4TB")

    def test_the_default_mount_turns_caching_off_and_a_failure_hides_the_password(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(links_device.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 68, "", "mount_smbfs: //admin:p%40ss... failed")
            with self.assertRaises(RuntimeError) as raised:
                links_device.mount("smb", self.device, Path(tmp) / "m")
            cmd = run.call_args.args[0]
        self.assertEqual(cmd[2], "nobrowse,forcenewsession,nomdatacache,nodatacache")
        self.assertIn("@192.168.1.218/4TB", cmd[3])
        self.assertNotIn("p%40ss", str(raised.exception))
        self.assertIn("rc=68", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
