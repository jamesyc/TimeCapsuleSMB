"""The host-side parts of tests/samba/durable_device.py: telling the smbd
parent's forked scavenger from its client connections, judging a durable-v2
grant, and the flows of the stopped+previous case around a session setup that
is or is not answered, of the timemachine case around the timeout answers
macOS accepts, and of the expiry case around a handle that outlives its grant
or does not."""
from __future__ import annotations

import contextlib
import io
import unittest
from unittest import mock

from tests.device_ps import ps_row
from tests.samba import device_processes, durable_device
from tests.samba.durable_device import (DURABLE_TIMEOUT_SECONDS, OLD_SMBD_EXIT_SECONDS, REQUESTED_TIMEOUT_MSEC,
                                        STATUS_OBJECT_NAME_NOT_FOUND, _scavengers,
                                        serves_client)
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
        if command == device_processes.COMMAND:
            self.now = self.polls.pop(0) if len(self.polls) > 1 else self.polls[0]
            # Children asleep on the disk among them.
            rows = [ps_row(146, "service", "service: role=manager", pgid=2), ps_row(457, "smbd", ppid=146)]
            return "\n".join(rows + [ps_row(pid, "smbd", ppid=457, pgid=457, stat="D") for pid in self.now]) + "\n"
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
        assert command == device_processes.COMMAND
        rows = [ps_row(146, "service", "service: role=manager", pgid=2), ps_row(457, "smbd", ppid=146)]
        if self.connected:
            rows.append(ps_row(3994, "smbd", ppid=457, pgid=457))
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


class SMB2CreateDurableHandleResponseV2(dict):
    """Named like smbprotocol's durable-v2 answer, which open_durable matches by name."""


def durable_answer(timeout_msec: int) -> SMB2CreateDurableHandleResponseV2:
    return SMB2CreateDurableHandleResponseV2(timeout=mock.Mock(get_value=mock.Mock(return_value=timeout_msec)))


class OpenDurableTest(unittest.TestCase):
    def open_answered(self, answer: list, *args) -> tuple[bool, int]:
        """open_durable(tree, path, *args) against a server answering `answer`;
        return whether it counts as granted and the timeout it asked for."""
        sent: dict[str, list] = {}

        class AnsweringOpen:
            def __init__(self, tree, path) -> None:
                pass

            def create(self, *create_args, create_contexts, **kwargs):
                sent["contexts"] = create_contexts
                return answer

        with mock.patch("smbprotocol.open.Open", AnsweringOpen):
            _, granted = durable_device.DurableClient(mock.Mock()).open_durable(mock.Mock(), "f.bin", *args)
        request = next(c["buffer_data"].get_value() for c in sent["contexts"]
                       if c["buffer_name"].get_value() == b"DH2Q")
        return granted, int.from_bytes(request[:4], "little")

    def test_a_default_open_asks_for_time_machines_30_s_and_needs_exactly_that(self) -> None:
        self.assertEqual(self.open_answered([durable_answer(30_000)]), (True, 30_000))
        # 3.4.0 answered 300 s whatever was asked, which macOS refuses.
        self.assertEqual(self.open_answered([durable_answer(300_000)]), (False, 30_000))

    def test_a_request_is_judged_against_the_grant_its_case_expects(self) -> None:
        self.assertEqual(self.open_answered([durable_answer(60_000)], 0, 60_000), (True, 0))
        self.assertEqual(self.open_answered([durable_answer(0)], 0, 60_000), (False, 0))
        self.assertEqual(self.open_answered([durable_answer(300_000)], 600_000, 300_000), (True, 600_000))

    def test_an_open_without_a_durable_answer_is_not_granted(self) -> None:
        self.assertEqual(self.open_answered([]), (False, 30_000))
        self.assertEqual(self.open_answered(None), (False, 30_000))


TIME_MACHINE_FILE = f"{durable_device.TEST_DIR}\\time-machine.bin"
TIME_MACHINE_CHECKS = [f"timemachine: a {seconds} s reconnect timeout is granted as asked"
                       for seconds in (30, 60, 120, 180)]


class TimeMachineCaseTest(unittest.TestCase):
    """time_machine_case through the real open_durable, against a server whose
    durable-v2 answer to a request for N ms is answer(N).

    macOS sets Time Machine's reconnect timeout by asking for it in a
    durable-v2 open and fails the setting with E2BIG unless the answer is
    exactly the value it asked for; after 30, 60, 120 and 180 s all fail, Time
    Machine cannot use the disk. These tests pin that every one of those
    values must come back unchanged, so a server answering anything else,
    longer or shorter, fails the case."""

    def run_case(self, answer) -> tuple[Results, list[tuple[str, int]], list[str]]:
        requests: list[tuple[str, int]] = []
        closes: list[str] = []

        class ServerOpen:
            def __init__(self, tree, path: str) -> None:
                self.path = path

            def create(self, *args, create_contexts, **kwargs):
                data = next(c["buffer_data"].get_value() for c in create_contexts
                            if c["buffer_name"].get_value() == b"DH2Q")
                requested = int.from_bytes(data[:4], "little")
                requests.append((self.path, requested))
                return [durable_answer(answer(requested))]

            def close(self) -> None:
                closes.append(self.path)

        conn = mock.Mock()
        results = Results()
        with mock.patch.object(durable_device.DurableClient, "connect",
                               return_value=(conn, mock.Mock(), mock.Mock())), \
                mock.patch("smbprotocol.open.Open", ServerOpen), \
                contextlib.redirect_stdout(io.StringIO()):
            durable_device.time_machine_case(results, mock.Mock())
        conn.disconnect.assert_called_once()
        return results, requests, closes

    def test_each_timeout_time_machine_tries_is_granted_exactly_as_asked(self) -> None:
        # 0024 answers the request itself (it keeps the handle 300 s anyway).
        results, requests, closes = self.run_case(lambda requested: requested)

        self.assertEqual(results.failed, [])
        self.assertEqual(results.passed, 4)
        self.assertEqual(requests, [(TIME_MACHINE_FILE, 30_000), (TIME_MACHINE_FILE, 60_000),
                                    (TIME_MACHINE_FILE, 120_000), (TIME_MACHINE_FILE, 180_000)])
        # Each attempt closes its handle before the next opens the same file,
        # since one lease key serves one file.
        self.assertEqual(closes, [TIME_MACHINE_FILE] * 4)

    def test_a_fixed_300_s_answer_fails_every_attempt(self) -> None:
        # Longer than asked is no better: macOS compares for equality, so
        # Time Machine runs out of values and gives up on the disk.
        results, requests, closes = self.run_case(lambda requested: 300_000)

        self.assertEqual(results.failed, TIME_MACHINE_CHECKS)
        self.assertEqual(results.passed, 0)
        self.assertEqual(len(requests), 4)
        self.assertEqual(closes, [TIME_MACHINE_FILE] * 4)

    def test_each_attempt_is_judged_on_its_own_answer(self) -> None:
        # A server capping grants at 60 s gets the first two right and still
        # fails Time Machine's 120 and 180 s.
        results, _, _ = self.run_case(lambda requested: min(requested, 60_000))

        self.assertEqual(results.failed, TIME_MACHINE_CHECKS[2:])
        self.assertEqual(results.passed, 2)


EXPIRY_PAYLOAD = b"durable payload expiry\n"
KEPT_CHECK = "expiry: the kept handle reconnects after twice its granted timeout"


class FakeClock:
    """time as expiry_case uses it: sleeping moves the clock, nothing else does."""

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class ResponseError(Exception):
    """Stands in for smbprotocol's SMBResponseException, which needs a whole header."""

    def __init__(self, status: int) -> None:
        super().__init__(hex(status))
        self.status = status


class FileOpen:
    """smbprotocol's Open for the probe and the exclusive open, which find the file free."""

    def __init__(self, tree, path) -> None:
        pass

    def create(self, *args, **kwargs) -> None:
        pass

    def read(self, offset: int, length: int) -> bytes:
        return EXPIRY_PAYLOAD

    def close(self) -> None:
        pass


class ExpiryClient:
    """DurableClient as expiry_case uses it. Each reconnect is recorded with
    the clock's time and expires the handle from its server disconnect time."""

    def __init__(self, clock: FakeClock, retention: float = DURABLE_TIMEOUT_SECONDS) -> None:
        self.clock, self.retention = clock, retention
        self.disconnected_at: float | None = None
        self.conns: list[mock.Mock] = []
        self.reconnects: list[float] = []

    def connect(self):
        conn = mock.Mock()
        self.conns.append(conn)
        return conn, mock.Mock(), mock.Mock()

    def open_durable(self, tree, path):
        return mock.Mock(file_id=b"id"), True

    def reconnect(self, tree, path, file_id):
        self.reconnects.append(self.clock.now)
        if self.disconnected_at is not None and self.clock.now >= self.disconnected_at + self.retention:
            raise ResponseError(STATUS_OBJECT_NAME_NOT_FOUND)
        return mock.Mock(read=mock.Mock(return_value=EXPIRY_PAYLOAD))


class ExpiryDevice:
    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.commands: list[tuple[float, str]] = []

    def sh(self, command: str, *, check: bool = True) -> str:
        self.commands.append((self.clock.now, command))
        return ""


class ExpiryCaseTest(unittest.TestCase):
    def run_case(self, exit_seconds: float = 0, retention: float = DURABLE_TIMEOUT_SECONDS):
        """Run expiry_case; the reset connections' smbds take exit_seconds to
        exit before the scavenger is found."""
        clock = FakeClock()
        device = ExpiryDevice(clock)
        client = ExpiryClient(clock, retention)
        keeper = ExpiryClient(clock, retention)
        clients = iter((client, keeper, ExpiryClient(clock), ExpiryClient(clock)))
        resets: list[float] = []

        def scavengers(_device, old):
            self.assertEqual(old, [3994, 3995])
            clock.now += exit_seconds
            client.disconnected_at = keeper.disconnected_at = clock.now
            return [4100]

        results = Results()
        with mock.patch.object(durable_device, "time", clock), \
                mock.patch.object(durable_device, "DurableClient", lambda _device: next(clients)), \
                mock.patch.object(durable_device, "_smbd_children", side_effect=[[457], [457, 3994, 3995]]), \
                mock.patch.object(durable_device, "_reset", lambda conn: resets.append(clock.now)), \
                mock.patch.object(durable_device, "_scavengers", scavengers), \
                mock.patch("smbprotocol.open.Open", FileOpen), \
                mock.patch("smbprotocol.exceptions.SMBResponseException", ResponseError), \
                contextlib.redirect_stdout(io.StringIO()):
            durable_device.expiry_case(results, device)
        self.assertEqual(len(resets), 2)
        resumed_at = next(at for at, command in device.commands if command == "kill -CONT 4100")
        return results, client, keeper, resets[-1], resumed_at

    def test_the_kept_handle_reconnects_after_its_grant_and_the_other_expires_at_300_s(self) -> None:
        results, client, keeper, reset_at, _ = self.run_case()

        self.assertEqual(results.failed, [])
        self.assertEqual(results.passed, 7)
        self.assertEqual(keeper.reconnects, [reset_at + 2 * REQUESTED_TIMEOUT_MSEC / 1000])
        self.assertEqual(client.reconnects, [reset_at + DURABLE_TIMEOUT_SECONDS + 20])
        keeper.conns[1].disconnect.assert_called()

    def test_a_scavenger_resumed_late_gets_time_to_catch_up_before_the_kept_reconnect(self) -> None:
        # The old smbds exit 100 s after the reset, past twice the 30 s grant:
        # reconnecting right away would race the resumed scavenger.
        results, client, keeper, reset_at, resumed_at = self.run_case(exit_seconds=100)

        self.assertEqual(results.failed, [])
        self.assertEqual(resumed_at, reset_at + 100)
        self.assertEqual(keeper.reconnects, [resumed_at + 2 * REQUESTED_TIMEOUT_MSEC / 1000])
        self.assertEqual(client.reconnects, [resumed_at + DURABLE_TIMEOUT_SECONDS + 20])

    def test_a_handle_expired_at_its_grant_fails_only_the_kept_check(self) -> None:
        results, client, keeper, _, _ = self.run_case(exit_seconds=100, retention=30)

        self.assertEqual(results.failed, [KEPT_CHECK])
        self.assertEqual(results.passed, 6)
        # The expiry is still checked, and the kept handle's connection closed.
        self.assertEqual(len(client.reconnects), 1)
        keeper.conns[1].disconnect.assert_called()

    def test_fast_teardown_also_detects_retention_of_only_30_seconds(self) -> None:
        results, _, _, _, _ = self.run_case(retention=30)
        self.assertEqual(results.failed, [KEPT_CHECK])

    def test_a_handle_retained_past_the_deadline_fails_expiry(self) -> None:
        results, _, _, _, _ = self.run_case(exit_seconds=100, retention=1000)
        self.assertEqual(results.failed, ["expiry: the expired handle no longer reconnects"])


if __name__ == "__main__":
    unittest.main()
