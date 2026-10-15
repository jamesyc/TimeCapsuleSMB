"""The host-side parts of tests/samba/durable_device.py: telling the smbd
parent's forked scavenger from its client connections, and the flow of the
stopped+previous case around a session setup that is or is not answered."""
from __future__ import annotations

import unittest
from unittest import mock

from tests.samba import durable_device
from tests.samba.durable_device import OLD_SMBD_EXIT_SECONDS, _scavengers, serves_client
from tests.samba.links_device import Results

HEADER = "USER     CMD          PID   FD MOUNT      INUM MODE         SZ|DV R/W"
# Every child of the smbd parent inherits its listeners.
LISTENERS = ("root     smbd        2871   26* internet6 stream tcp c27f77a8 *:445\n"
             "root     smbd        2871   27* internet stream tcp c27f7620 *:445")
# The scavenger, forked on NetBSD 4: the listeners and its socketpair to the parent.
SCAVENGER = "\n".join((HEADER, "root     smbd        2871   wd /             2 drwxr-xr-x     512 r", LISTENERS,
                       "root     smbd        2871   30* unix stream c285bcc0 <-> c28df100"))
# NetBSD 6 prints no kernel addresses and the IPv6 wildcard in brackets.
NETBSD6_SCAVENGER = "\n".join((HEADER, "root     smbd        3487   26* internet6 stream tcp [*]:445",
                               "root     smbd        3487   27* internet stream tcp *:445",
                               "root     smbd        3487   30* unix stream  <-> 8e58c090"))
# A client's smbd prints the device's end first on NetBSD 4 and the Mac's on NetBSD 6.
NETBSD4_CLIENT = "\n".join((HEADER, LISTENERS, "root     smbd        3994   29* unix stream c28df100 <-> c285bcc0",
                            "root     smbd        3994   30* internet stream tcp c27f7000 "
                            "192.168.1.10:445 <-> 192.168.1.170:54823"))
NETBSD6_CLIENT = "\n".join((HEADER,
                            "root     smbd        4135    5* unix dgram  <-> /mnt/Memory/samba4/private/msg.sock/4135",
                            "root     smbd        4135   30* internet stream tcp 192.168.1.170:54827 <-> "
                            "192.168.1.218:445"))


class ServesClientTest(unittest.TestCase):
    def test_a_connected_tcp_socket_in_either_endpoint_order_serves_a_client(self) -> None:
        self.assertTrue(serves_client(NETBSD4_CLIENT))
        self.assertTrue(serves_client(NETBSD6_CLIENT))

    def test_inherited_listeners_and_unix_sockets_serve_no_client(self) -> None:
        self.assertFalse(serves_client(SCAVENGER))
        self.assertFalse(serves_client(NETBSD6_SCAVENGER))
        self.assertFalse(serves_client("\n".join((HEADER,
            "root     smbd        4135    5* unix dgram  <-> /mnt/Memory/samba4/private/msg.sock/4135"))))

    def test_a_process_that_exited_serves_no_client(self) -> None:
        self.assertFalse(serves_client(""))


class FakeDevice:
    """The smbd parent's children over time: each ps advances to the next
    entry of `polls` (the last one repeats), mapping each child to its fstat."""

    def __init__(self, polls: list[dict[int, str]]) -> None:
        self.polls = polls
        self.now: dict[int, str] = {}

    def sh(self, command: str, *, check: bool = True) -> str:
        if command.startswith("ps "):
            self.now = self.polls.pop(0) if len(self.polls) > 1 else self.polls[0]
            rows = ["  PID  PPID COMMAND", "  146     1 service: role=manager",
                    "  457   146 /mnt/Memory/samba4/sbin/smbd -F --no-process-group"]
            return "\n".join(rows + [f"{pid:5} 457 /mnt/Memory/samba4/sbin/smbd -F --no-process-group"
                                     for pid in self.now]) + "\n"
        pid = int(command.rsplit(None, 1)[1])
        return self.now.get(pid, "")


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class ScavengersTest(unittest.TestCase):
    def find(self, polls: list[dict[int, str]], old: list[int]) -> tuple[list[int], float]:
        clock = Clock()
        return _scavengers(FakeDevice(polls), old, clock=clock, sleep=clock.sleep), clock.now

    def test_waits_for_the_reset_smbd_to_exit_before_counting(self) -> None:
        # The reset connection's smbd serves no client while it closes its
        # files; it is not the scavenger, even before the parent forks one.
        polls = [{3994: SCAVENGER}, {3994: SCAVENGER, 2871: SCAVENGER}, {2871: SCAVENGER, 4135: NETBSD6_CLIENT}]
        self.assertEqual(self.find(polls, [3994]), ([2871], 2))

    def test_waits_until_only_one_child_serves_no_client(self) -> None:
        # Another connection's smbd still closing alongside the scavenger.
        polls = [{2871: SCAVENGER, 5000: SCAVENGER}, {2871: SCAVENGER}]
        self.assertEqual(self.find(polls, [3994]), ([2871], 1))

    def test_finds_a_scavenger_an_earlier_case_started_at_once(self) -> None:
        self.assertEqual(self.find([{2871: SCAVENGER, 3994: NETBSD4_CLIENT}], [4200]), ([2871], 0))

    def test_gives_up_with_what_it_found_when_no_scavenger_appears(self) -> None:
        found, waited = self.find([{3994: NETBSD4_CLIENT}], [4200])
        self.assertEqual(found, [])
        self.assertEqual(waited, OLD_SMBD_EXIT_SECONDS)

    def test_gives_up_while_the_reset_smbd_never_exits(self) -> None:
        found, waited = self.find([{3994: SCAVENGER, 2871: SCAVENGER}], [3994])
        self.assertEqual(found, [])
        self.assertEqual(waited, OLD_SMBD_EXIT_SECONDS)


class StoppedDevice:
    """The smbd parent gains child 3994 at the first connection; records the
    signals sent to it."""

    def __init__(self) -> None:
        self.connected = False
        self.signals: list[str] = []

    def sh(self, command: str, *, check: bool = True) -> str:
        if command.startswith("kill "):
            self.signals.append(command)
            return ""
        rows = ["  PID  PPID COMMAND", "  146     1 service: role=manager",
                "  457   146 /mnt/Memory/samba4/sbin/smbd -F --no-process-group"]
        if self.connected:
            rows.append(" 3994   457 /mnt/Memory/samba4/sbin/smbd -F --no-process-group")
        return "\n".join(rows) + "\n"


class StoppedClient:
    """DurableClient as stopped_previous_case uses it. The second connection
    names the first session; `answer` is what its session setup does."""

    PAYLOAD = b"durable payload stopped+previous\n"

    def __init__(self, device: StoppedDevice, answer) -> None:
        self.device, self.answer = device, answer
        self.conns: list[mock.Mock] = []
        self.reconnects = 0

    def connect(self, previous=None):
        if previous is not None:
            self.answer(previous)
        self.device.connected = True
        conn = mock.Mock()
        self.conns.append(conn)
        return conn, mock.Mock(session_id=0x51), mock.Mock()

    def open_durable(self, tree, path):
        return mock.Mock(file_id=b"id"), True

    def reconnect(self, tree, path, file_id):
        self.reconnects += 1
        return mock.Mock(read=mock.Mock(return_value=self.PAYLOAD))


class StoppedPreviousTest(unittest.TestCase):
    def run_case(self, answer) -> tuple[Results, StoppedDevice, StoppedClient]:
        device = StoppedDevice()
        client = StoppedClient(device, answer)
        results = Results()
        with mock.patch.object(durable_device, "DurableClient", lambda _device: client), \
                mock.patch.object(durable_device, "OLD_SMBD_STOP_SECONDS", 0):
            durable_device.stopped_previous_case(results, device)
        return results, device, client

    def test_an_answered_session_setup_lets_the_reconnect_take_the_open_over(self) -> None:
        results, device, client = self.run_case(lambda previous: self.assertEqual(previous, 0x51))

        self.assertEqual(results.failed, [])
        self.assertEqual(results.passed, 4)
        self.assertEqual(client.reconnects, 1)
        self.assertEqual(device.signals[0], "kill -STOP 3994")
        self.assertIn("kill -CONT 3994", device.signals[1:])
        for conn in client.conns:
            conn.disconnect.assert_called()

    def test_an_unanswered_session_setup_fails_its_check_and_lets_the_suite_go_on(self) -> None:
        def times_out(previous) -> None:
            raise TimeoutError("no session setup response")

        results, device, client = self.run_case(times_out)

        self.assertEqual(results.failed, ["stopped+previous: the session setup is answered while the old smbd is stopped"])
        self.assertEqual(results.passed, 2)
        self.assertEqual(client.reconnects, 0)
        # The old smbd is resumed and the first connection closed all the same.
        self.assertIn("kill -CONT 3994", device.signals[1:])
        client.conns[0].disconnect.assert_called()


if __name__ == "__main__":
    unittest.main()
