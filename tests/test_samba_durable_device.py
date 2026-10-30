"""The host-side parts of tests/samba/durable_device.py: telling the smbd
parent's forked scavenger from its client connections, judging a durable-v2
grant, and the flows of the stopped+previous case around a session setup that
is or is not answered, of the timemachine case around the timeout answers
macOS accepts, and of the expiry case around a handle that outlives its grant
or does not."""
from __future__ import annotations

import contextlib
import errno
import io
import logging
import types
import unittest
from unittest import mock

from tests.device_ps import ps_row
from tests.samba import device_processes, durable_device, tdb_records
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


GUID = b"\x11" * 16
KILLED_CHECKS = ["orphan:killed: one smbd serves the client and holds every open",
                 "orphan:killed: the dead smbd's opens, replay records and leases are released",
                 "orphan:killed: the parent logs the signal and the scavenger the release",
                 "orphan:killed: a reconnect of a released open is refused"]
SUPERSEDED_CHECKS = ["orphan:superseded: the dropped opens are disconnected and keep their leases",
                     "orphan:superseded: the new client's opens release the old leases",
                     "orphan:superseded: the new client keeps its own leases"]
OTHER_GUID = b"\x22" * 16
FAILED_CHECKS = ["orphan:failed-reconnect: the open locks its first byte",
                 "orphan:failed-reconnect: the dropped open is disconnected",
                 "orphan:failed-reconnect: the reconnect is refused",
                 "orphan:failed-reconnect: its lease is gone at once",
                 "orphan:failed-reconnect: its open stays disconnected with its own disconnect time",
                 "orphan:failed-reconnect: another client locks the same byte at once",
                 "orphan:failed-reconnect: the open and its replay record expire"]
STATUS_LOCK_NOT_GRANTED = 0xC0000055  # what a fail-at-once lock gets on the device


class OrphanLocks:
    """What /mnt/Locks holds of the client, as the device would answer
    _orphans(): the opens, their replay records and leases. Each case's
    device behaviour is a set of times on the fake clock."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.opens: list[tdb_records.Open] = []
        self.replays = self.leases = 0
        # Leases of other client GUIDs.
        self.other_leases: dict[bytes, int] = {}
        self.release_at: float | None = None
        self.commands: list[str] = []
        # The smbd each new open of the client is stored under, in turn.
        self.owner_pids = [3994]
        # A copy of /mnt/Locks that does not parse, as a failed cat gives.
        self.invalid = self.invalid_after_kill = False
        # The failed reconnect kept the open's byte-range lock.
        self.lock_conflict = False

    def sh(self, command: str, *, check: bool = True) -> str:
        self.commands.append(command)
        return ""

    def orphans(self, _device, guid, owner=None):
        if self.invalid:
            raise tdb_records.InvalidTdb("no tdb header in 0 bytes")
        if self.release_at is not None and self.clock.now >= self.release_at:
            self.opens, self.replays, self.leases = [], 0, 0
        if guid != GUID:
            return [], 0, self.other_leases.get(guid, 0)
        return ([o for o in self.opens if owner is None or o.pid == owner], self.replays, self.leases)


class OrphanClient:
    """DurableClient as orphan_case uses it; reconnects are refused."""

    def __init__(self, locks: OrphanLocks, on_reconnect=None, guid: bytes = GUID, on_open=None) -> None:
        self.client_guid = mock.Mock(bytes=guid)
        self.lease_key = self.create_guid = None
        self.locks, self.on_reconnect, self.on_open = locks, on_reconnect, on_open
        self.conns: list[mock.Mock] = []

    def connect(self, previous_session_id: int = 0):
        conn = mock.Mock()
        self.conns.append(conn)
        return conn, mock.Mock(), mock.Mock()

    def open_durable(self, tree, path):
        guid = self.client_guid.bytes
        if guid != GUID:
            # Another client: its open may drop the first client's entries.
            self.locks.other_leases[guid] = self.locks.other_leases.get(guid, 0) + 1
            if self.on_open:
                self.on_open(self.locks)
            handle = mock.Mock(file_id=b"other")
            if self.locks.lock_conflict:
                handle.lock.side_effect = ResponseError(STATUS_LOCK_NOT_GRANTED)
            return handle, True
        n = len(self.locks.opens)
        pid = self.locks.owner_pids[n % len(self.locks.owner_pids)]
        self.locks.opens.append(tdb_records.Open(n + 1, pid, 0, GUID))
        self.locks.replays += 1
        self.locks.leases += 1
        return mock.Mock(file_id=b"id%d" % n), True

    def reconnect(self, tree, path, file_id):
        if self.on_reconnect:
            self.on_reconnect()
        raise ResponseError(STATUS_OBJECT_NAME_NOT_FOUND)


class OrphanCaseTest(unittest.TestCase):
    def run_case(self, mode, *, release_after=None, logged=None, on_reset=None, on_reconnect=None, on_other_open=None,
                 setup=None, children=None):
        clock = FakeClock()
        locks = OrphanLocks(clock)
        if setup:
            setup(locks)
        client = OrphanClient(locks, on_reconnect and (lambda: on_reconnect(locks)))
        clients = iter([client, OrphanClient(locks, guid=OTHER_GUID, on_open=on_other_open)])

        def kill_or_sh(command, *, check=True):
            locks.sh(command)
            if command == "kill -9 3994" and release_after is not None:
                locks.release_at = clock.now + release_after
            if command.startswith("kill -9 ") and locks.invalid_after_kill:
                locks.invalid = True
            # fstat's output names the process; serves_client is faked on it.
            return command if command.startswith("/usr/bin/fstat -p ") else ""

        locks_device = mock.Mock(root="/Volumes/dk2/Share", sh=kill_or_sh)
        # 3994 serves this client; a scavenger forked meanwhile (4100) does not.
        children = children or [[457], [457, 3994, 4100]]
        results = Results()
        with mock.patch.object(durable_device, "time", clock), \
                mock.patch.object(durable_device, "DurableClient", lambda _device: next(clients)), \
                mock.patch.object(durable_device, "_smbd_children", side_effect=children), \
                mock.patch.object(durable_device, "serves_client", lambda fstat: "3994" in fstat), \
                mock.patch.object(durable_device, "_orphans", locks.orphans), \
                mock.patch.object(durable_device, "_smbd_log", return_value=("log.smbd", 100)), \
                mock.patch.object(durable_device, "_smbd_log_since", return_value=logged or ""), \
                mock.patch.object(durable_device, "_reset", lambda conn: on_reset(locks, clock)), \
                mock.patch("smbprotocol.exceptions.SMBResponseException", ResponseError), \
                contextlib.redirect_stdout(io.StringIO()):
            durable_device.orphan_case(results, locks_device, mode)
        return results, locks, client, clock

    LOGGED = "child 3994 ended by signal 9\n... removed 20 opens left by exited smbds\n"

    def test_killed_smbd_released_at_once_passes(self) -> None:
        results, locks, client, _ = self.run_case("killed", release_after=5, logged=self.LOGGED)
        self.assertEqual(results.failed, [])
        self.assertEqual(results.passed, len(KILLED_CHECKS))
        self.assertIn("kill -9 3994", locks.commands)
        for conn in client.conns:
            conn.disconnect.assert_called()

    def test_killed_smbd_records_that_stay_fail_the_release(self) -> None:
        # Before 0077 nothing releases them: the 60 s wait runs out.
        results, _, _, clock = self.run_case("killed", logged=self.LOGGED)
        self.assertEqual(results.failed, [KILLED_CHECKS[1]])
        self.assertGreaterEqual(clock.now - 1000, durable_device.ORPHAN_RELEASE_SECONDS)

    def assert_no_kill(self, locks) -> None:
        self.assertFalse([c for c in locks.commands if c.startswith("kill")])

    def test_killed_opens_stored_under_two_smbds_send_no_kill(self) -> None:
        results, locks, _, _ = self.run_case("killed", release_after=5, logged=self.LOGGED,
                                             setup=lambda locks: setattr(locks, "owner_pids", [3994, 4001]))
        self.assertEqual(results.failed, [KILLED_CHECKS[0]])
        self.assert_no_kill(locks)

    def test_killed_owner_that_started_no_client_connection_is_not_killed(self) -> None:
        # Another client's smbd (100) appeared between the snapshots; the
        # client's own records name 3994, which is not among them.
        results, locks, _, _ = self.run_case("killed", release_after=5, logged=self.LOGGED,
                                             children=[[457], [457, 100, 4100]])
        self.assertEqual(results.failed, [KILLED_CHECKS[0]])
        self.assert_no_kill(locks)
        self.assertNotIn("kill -9 100", locks.commands)

    def test_killed_unreadable_open_records_send_no_kill(self) -> None:
        results, locks, _, _ = self.run_case("killed", release_after=5, logged=self.LOGGED,
                                             setup=lambda locks: setattr(locks, "invalid", True))
        self.assertEqual(results.failed, [KILLED_CHECKS[0]])
        self.assert_no_kill(locks)

    def test_killed_copies_that_stop_parsing_never_count_as_released(self) -> None:
        # An empty or cut-short cat would otherwise read as "no records".
        results, locks, _, clock = self.run_case("killed", logged=self.LOGGED,
                                                 setup=lambda locks: setattr(locks, "invalid_after_kill", True))
        self.assertIn("kill -9 3994", locks.commands)
        self.assertIn(KILLED_CHECKS[1], results.failed)
        self.assertGreaterEqual(clock.now - 1000, durable_device.ORPHAN_RELEASE_SECONDS)

    def test_killed_smbd_released_without_its_log_lines_fails_the_log_check(self) -> None:
        results, _, _, _ = self.run_case("killed", release_after=5, logged="removed 20 opens left by exited smbds\n")
        self.assertEqual(results.failed, [KILLED_CHECKS[2]])

    def superseded(self, *, released=True, own_kept=True, leases_at_reset=None):
        """The reset leaves the 5 opens disconnected with their leases (or
        leases_at_reset of them); each of the new client's opens releases
        the old leases or not, and keeps its own or not."""
        n = durable_device.ORPHAN_SUPERSEDED_OPENS

        def reset(locks, clock):
            locks.opens = [tdb_records.Open(i + 1, tdb_records.DISCONNECTED_PID, 2000, GUID) for i in range(n)]
            if leases_at_reset is not None:
                locks.leases = leases_at_reset

        def other_open(locks):
            if released:
                locks.leases = 0
            if not own_kept:
                locks.other_leases[OTHER_GUID] = 0

        return self.run_case("superseded", on_reset=reset, on_other_open=other_open)

    def test_superseded_leases_released_with_the_dropped_entries_pass(self) -> None:
        results, locks, client, _ = self.superseded()
        self.assertEqual(results.failed, [])
        self.assertEqual(results.passed, len(SUPERSEDED_CHECKS))
        self.assertEqual(locks.other_leases[OTHER_GUID], durable_device.ORPHAN_SUPERSEDED_OPENS)
        client.conns[0].disconnect.assert_called()

    def test_superseded_leases_that_stay_fail_after_the_release_wait(self) -> None:
        # Before 0077 the dropped entries' leases stayed in leases.tdb for good.
        results, _, _, clock = self.superseded(released=False)
        self.assertEqual(results.failed, [SUPERSEDED_CHECKS[1]])
        self.assertGreaterEqual(clock.now - 1000, durable_device.ORPHAN_RELEASE_SECONDS)

    def test_superseded_release_that_takes_the_new_clients_leases_fails(self) -> None:
        results, _, _, _ = self.superseded(own_kept=False)
        self.assertEqual(results.failed, [SUPERSEDED_CHECKS[2]])

    def test_superseded_leases_gone_before_the_new_client_fail_the_setup_check(self) -> None:
        # Nothing would show a lease kept by the dropped entries.
        results, _, _, _ = self.superseded(leases_at_reset=0)
        self.assertEqual(results.failed, [SUPERSEDED_CHECKS[0]])

    def failed_reconnect(self, *, lease_kept=False, time_after=None, expires=True, lock_kept=False):
        """The dropped open is disconnected at 2000 (NTTIME units are
        irrelevant here); the failed reconnect then leaves its lease or not
        and stores time_after; the open expires 300 s after the reset."""
        def reset(locks, clock):
            locks.opens = [tdb_records.Open(1, tdb_records.DISCONNECTED_PID, 2000, GUID)]
            if expires:
                locks.release_at = clock.now + DURABLE_TIMEOUT_SECONDS

        def reconnect(locks):
            if not lease_kept:
                locks.leases = 0
            if time_after is not None:
                locks.opens = [tdb_records.Open(1, tdb_records.DISCONNECTED_PID, time_after, GUID)]

        def setup(locks):
            locks.lock_conflict = lock_kept

        return self.run_case("failed-reconnect", on_reset=reset, on_reconnect=reconnect, setup=setup)

    def test_failed_reconnect_that_keeps_its_time_and_drops_its_lease_passes(self) -> None:
        results, locks, _, _ = self.failed_reconnect()
        self.assertEqual(results.failed, [])
        self.assertEqual(results.passed, len(FAILED_CHECKS))
        self.assertIn("echo grown >> /Volumes/dk2/Share/__tc_durable_test__/orphan-failed.bin", locks.commands)

    def test_a_lease_kept_after_the_failed_reconnect_fails_its_check(self) -> None:
        results, _, _, _ = self.failed_reconnect(lease_kept=True)
        self.assertEqual(results.failed, [FAILED_CHECKS[3]])

    def test_an_open_stored_with_disconnect_time_0_fails_its_check(self) -> None:
        # Before 0077 the failed reconnect's TALLOC_FREE stored it with 0.
        results, _, _, _ = self.failed_reconnect(time_after=0)
        self.assertEqual(results.failed, [FAILED_CHECKS[4]])

    def test_a_lock_kept_by_the_failed_reconnect_fails_the_other_clients_check(self) -> None:
        # Before 0077 the reconnecting smbd kept the lock it took over.
        results, _, _, _ = self.failed_reconnect(lock_kept=True)
        self.assertEqual(results.failed, [FAILED_CHECKS[5]])

    def test_an_open_that_never_expires_fails_after_the_durable_timeout(self) -> None:
        results, _, _, clock = self.failed_reconnect(expires=False)
        self.assertEqual(results.failed, [FAILED_CHECKS[6]])
        self.assertGreaterEqual(clock.now - 1000, DURABLE_TIMEOUT_SECONDS)



class ShortWriteHelpersTest(unittest.TestCase):
    """The pieces the short-WRITE cases build their streams and checks from."""

    @staticmethod
    def frame(payload: bytes) -> bytes:
        return len(payload).to_bytes(4, "big") + payload

    def test_a_server_reading_the_writes_length_takes_the_cut_bytes_from_what_follows(self) -> None:
        write = self.frame(b"W" * 64 + b"B" * 48 + b"D" * 1000)
        following = self.frame(b"E" * 70) + self.frame(b"C" * 200)
        for cut in (1, 74, 75, len(following)):
            stream = durable_device._short_stream(write, following, cut)
            length = int.from_bytes(stream[:4], "big")
            self.assertEqual(stream[4:4 + length], write[4:-cut] + following[:cut])
            self.assertEqual(stream[4 + length:], following[cut:])

    def test_a_cut_beyond_what_follows_or_into_the_writes_fixed_part_is_refused(self) -> None:
        write = self.frame(b"W" * 64 + b"B" * 48 + b"D" * 100)
        following = self.frame(b"E" * 70)
        for cut in (0, len(following) + 1):
            with self.assertRaises(ValueError):
                durable_device._short_stream(write, following, cut)
        with self.assertRaises(ValueError):
            durable_device._short_stream(write, b"X" * 500, 101)
        durable_device._short_stream(write, b"X" * 500, 100)

    def test_capture_keeps_the_frames_and_restores_the_transport(self) -> None:
        sent = []
        conn = mock.Mock()
        conn.transport.send = sent.append
        real = conn.transport.send

        def send():
            conn.transport.send(b"abc")
            conn.transport.send(b"de")
            return "request"

        self.assertEqual(durable_device._capture(conn, send), ("request", b"\0\0\0\x03abc\0\0\0\x02de"))
        self.assertEqual(sent, [])
        self.assertIs(conn.transport.send, real)

        def fails():
            raise RuntimeError("no")

        with self.assertRaises(RuntimeError):
            durable_device._capture(conn, fails)
        self.assertIs(conn.transport.send, real)

    def test_the_end_is_timed_from_start_and_missing_after_the_limit(self) -> None:
        clock = [100.0]

        def sleep(seconds):
            clock[0] += seconds

        conn = mock.Mock()
        conn._t_worker.is_alive.side_effect = [True, True, True, False]
        with mock.patch.object(durable_device.time, "monotonic", lambda: clock[0]), \
                mock.patch.object(durable_device.time, "sleep", sleep):
            self.assertAlmostEqual(durable_device._ended_within(conn, 99.5, 2.0), 0.8)
            conn._t_worker.is_alive.side_effect = None
            conn._t_worker.is_alive.return_value = True
            self.assertIsNone(durable_device._ended_within(conn, clock[0], 2.0))


STALE_BREAK_CHECKS = ["lease:stale-break: the holder gets a durable open",
                      "lease:stale-break: the conflicting open breaks the holder's lease",
                      "lease:stale-break: the conflicting open is granted at once"]


class SecondOpen:
    """smbprotocol's Open for the second client's CREATE, built and not sent;
    records the arguments it is created with."""

    creates: list[tuple] = []

    def __init__(self, tree, path) -> None:
        pass

    def create(self, *args, send=True):
        SecondOpen.creates.append(args)
        return ("create request",)


def answer(command: int) -> dict:
    """The part of an SMB2HeaderResponse that _is_break reads."""
    return {"command": mock.Mock(get_value=lambda: command)}


class StaleBreakCaseTest(unittest.TestCase):
    def run_case(self, *, durable: bool = True, breaks: bool = True,
                 response: dict | None = answer(durable_device.SMB2_OPLOCK_BREAK),
                 grant: Exception | None = None, holder_open=None):
        """Run stale_break_case. The holder's open is durable if durable. Once
        the second client sends its CREATE, the awaited request's event is set
        with response if breaks; that CREATE's answer raises grant, if given."""
        from smbprotocol.open import CreateDisposition, FilePipePrinterAccessMask

        clock = FakeClock()
        holder_conn, other_conn = mock.Mock(), mock.Mock()
        holder, other = mock.Mock(), mock.Mock()
        holder.connect.return_value = (holder_conn, mock.Mock(session_id=5), mock.Mock())
        holder.open_durable.side_effect = holder_open
        holder.open_durable.return_value = (mock.Mock(), durable)
        other.connect.return_value = (other_conn, mock.Mock(session_id=7), mock.Mock(tree_connect_id=3))
        sent: list[bool] = []
        events: list[str] = []
        lease_break = mock.Mock(response=None)

        def wait(seconds):
            # Set as soon as the CREATE is sent; otherwise the wait runs out.
            if not sent:
                clock.now += seconds
                return False
            lease_break.response = response
            return True

        lease_break.response_event.wait.side_effect = wait

        def awaited_break(conn, session_id):
            self.assertEqual((conn, session_id), (holder_conn, 5))
            # The break is awaited before the second client's CREATE can cause it.
            self.assertEqual(other_conn.send.call_count, 0)
            return lease_break

        def send(create, session_id, tree_id):
            self.assertEqual((create, session_id, tree_id), ("create request", 7, 3))
            if breaks:
                sent.append(True)
            return "request"

        def receive(request, timeout):
            self.assertEqual((request, timeout), ("request", durable_device.FREE_OPEN_SECONDS))
            events.append("receive")
            if grant is not None:
                raise grant

        other_conn.send.side_effect = send
        other_conn.receive.side_effect = receive
        clients = iter((holder, other))
        SecondOpen.creates = []
        results = Results()
        with mock.patch.object(durable_device, "time", clock), \
                mock.patch.object(durable_device, "DurableClient", lambda _device: next(clients)), \
                mock.patch.object(durable_device, "_awaited_break", awaited_break), \
                mock.patch.object(durable_device, "_reset", lambda conn: events.append(
                    "reset" if conn is holder_conn else "reset other")), \
                mock.patch("smbprotocol.open.Open", SecondOpen), \
                contextlib.redirect_stdout(io.StringIO()):
            try:
                durable_device.stale_break_case(results, mock.Mock())
            finally:
                holder_conn.disconnect.assert_called()
        self.assertEqual(holder.lease_state, durable_device.LEASE_RH)
        if SecondOpen.creates:
            args = SecondOpen.creates[0]
            # The second client asks to write, which the holder's read-only
            # sharing refuses, and opens without overwriting.
            self.assertTrue(args[1] & FilePipePrinterAccessMask.GENERIC_WRITE)
            self.assertEqual(args[4], CreateDisposition.FILE_OPEN_IF)
            other_conn.disconnect.assert_called()
        return results, clock, events

    def test_an_open_granted_once_the_holder_vanishes_passes(self) -> None:
        results, clock, events = self.run_case()

        self.assertEqual(results.failed, [])
        self.assertEqual(results.passed, 3)
        # The holder vanishes only after its break, before the open is answered.
        self.assertEqual(events, ["reset", "receive"])
        self.assertEqual(clock.now, 1000.0)

    def test_an_open_that_is_not_durable_stops_the_case_before_the_second_client(self) -> None:
        # The reset would close it, and the open would be granted with or
        # without 0078: a pass would prove nothing.
        results, _, events = self.run_case(durable=False)

        self.assertEqual(results.failed, STALE_BREAK_CHECKS[:1])
        self.assertEqual(results.passed, 0)
        self.assertEqual((events, SecondOpen.creates), ([], []))

    def test_an_open_still_waiting_or_refused_fails_the_grant(self) -> None:
        from smbprotocol.exceptions import SMBException

        # Before 0078 the open waits out its 70 s timeout; the answer's wait
        # gives up first.
        for grant in (SMBException("Connection timeout of 15 seconds exceeded"),
                      SMBException("STATUS_SHARING_VIOLATION")):
            with self.subTest(grant=str(grant)):
                results, _, _ = self.run_case(grant=grant)
                self.assertEqual(results.failed, STALE_BREAK_CHECKS[2:])

    def test_no_break_stops_the_case_without_dropping_the_holder(self) -> None:
        cases = {"none within the bound": dict(breaks=False),
                 "the connection closed, no answer": dict(response=None),
                 "another message": dict(response=answer(0x0D))}  # SMB2 ECHO
        for name, kwargs in cases.items():
            with self.subTest(name):
                results, _, events = self.run_case(**kwargs)
                self.assertEqual(results.failed, STALE_BREAK_CHECKS[1:2])
                self.assertEqual(results.passed, 1)
                self.assertEqual(events, [])
        # With none, it waited the whole bound.
        _, clock, _ = self.run_case(breaks=False)
        self.assertEqual(clock.now, 1000.0 + durable_device.FREE_OPEN_SECONDS)

    def test_a_failed_holder_open_closes_the_connection(self) -> None:
        with self.assertRaises(RuntimeError):
            self.run_case(holder_open=RuntimeError("refused"))


class AwaitedBreakTest(unittest.TestCase):
    """_awaited_break and _is_break against smbprotocol's own receive loop,
    fed a lease break notification as the server sends it."""

    def lease_break(self) -> bytes:
        from smbprotocol.header import SMB2HeaderResponse

        header = SMB2HeaderResponse()
        header["command"] = durable_device.SMB2_OPLOCK_BREAK
        header["message_id"] = durable_device.UNSOLICITED_MESSAGE_ID
        header["data"] = b"\x2c\x00" + bytes(42)  # a lease break notification's body
        return header.pack()

    def receive(self, messages: list[bytes], awaited: bool = True):
        import uuid

        from smbprotocol.connection import Connection

        conn = Connection(uuid.uuid4(), "device")
        conn.transport = mock.Mock()
        conn.transport.recv.side_effect = messages + [b""]  # then the server closes
        request = durable_device._awaited_break(conn, 5) if awaited else None
        conn._process_message_thread()
        return conn, request

    def test_smbprotocols_receive_loop_hands_the_break_to_the_request(self) -> None:
        conn, request = self.receive([self.lease_break()])

        self.assertTrue(request.response_event.is_set())
        self.assertTrue(durable_device._is_break(request))
        self.assertEqual(request.session_id, 5)
        # The loop ended at the close, not on the break.
        self.assertIsNone(conn._t_exc)

    def test_a_close_sets_the_event_with_no_answer(self) -> None:
        _, request = self.receive([])
        self.assertTrue(request.response_event.is_set())
        self.assertIsNone(request.response)
        self.assertFalse(durable_device._is_break(request))

    def test_without_it_a_lease_break_ends_the_receive_loop(self) -> None:
        # Why _awaited_break exists: smbprotocol has no request for the
        # unsolicited message ID, and its receive loop dies on the lookup.
        conn, _ = self.receive([self.lease_break()], awaited=False)
        self.assertIsInstance(conn._t_exc, KeyError)


class LeaseStateTest(unittest.TestCase):
    def lease_state(self, client) -> int:
        from smbprotocol.create_contexts import CreateContextName, SMB2CreateDurableHandleRequestV2

        contexts = client._contexts((CreateContextName.SMB2_CREATE_DURABLE_HANDLE_REQUEST_V2,
                                     SMB2CreateDurableHandleRequestV2()))
        # SMB2_CREATE_REQUEST_LEASE_V2: the lease key, then the lease state.
        return int.from_bytes(contexts[0]["buffer_data"].get_value()[16:20], "little")

    def test_a_client_asks_for_read_handle_and_write_caching_unless_told_otherwise(self) -> None:
        # stale-break needs RH on the wire: with write caching the retry has
        # something to break, and the case passes with or without 0078.
        client = durable_device.DurableClient(mock.Mock())
        self.assertEqual(self.lease_state(client), 0x7)
        client.lease_state = durable_device.LEASE_RH
        self.assertEqual(self.lease_state(client), 0x3)


class QuietDroppedSocketTest(unittest.TestCase):
    """What smbprotocol's receive thread does once _reset closes its socket,
    and what main() keeps quiet of it."""

    def reset_connection(self):
        import uuid

        from smbprotocol.connection import Connection

        conn = Connection(uuid.uuid4(), "device")
        conn.transport = mock.Mock()
        conn.transport.recv.side_effect = OSError(errno.EBADF, "Bad file descriptor")
        conn.transport.close.side_effect = OSError(errno.EBADF, "Bad file descriptor")
        return conn

    def hook_args(self, error: BaseException, thread_name: str = "msg_worker-device:445"):
        """What threading.excepthook is given for an exception that ends a thread."""
        return types.SimpleNamespace(exc_value=error, thread=types.SimpleNamespace(name=thread_name))

    def test_the_cleanup_of_a_reset_socket_raises_out_of_the_receive_thread(self) -> None:
        # The loop's own handler closes the closed socket again: that EBADF
        # escapes the thread, so threading.excepthook sees it.
        with self.assertRaises(OSError) as raised:
            self.reset_connection()._process_message_thread()
        self.assertEqual(raised.exception.errno, errno.EBADF)

    def test_the_hook_drops_only_a_receive_threads_oserror(self) -> None:
        with mock.patch.object(durable_device, "_default_excepthook") as default:
            durable_device._quiet_dropped_socket(self.hook_args(OSError(errno.EBADF, "Bad file descriptor")))
            default.assert_not_called()
            for args in (self.hook_args(KeyError(1)), self.hook_args(OSError(errno.EBADF, "x"), "Thread-1")):
                durable_device._quiet_dropped_socket(args)
                default.assert_called_with(args)


class QuietDroppedSocketLogTest(unittest.TestCase):
    def record(self, error: BaseException | None) -> logging.LogRecord:
        exc_info = (type(error), error, None) if error is not None else None
        return logging.LogRecord("smbprotocol.connection", logging.ERROR, __file__, 1,
                                 "SMB receive worker died", None, exc_info)

    def test_only_the_ebadf_of_a_socket_closed_on_purpose_is_dropped(self) -> None:
        quiet = durable_device._QuietDroppedSocketLog()
        self.assertFalse(quiet.filter(self.record(OSError(errno.EBADF, "Bad file descriptor"))))
        self.assertTrue(quiet.filter(self.record(OSError(errno.ECONNRESET, "Connection reset"))))
        self.assertTrue(quiet.filter(self.record(KeyError(1))))
        self.assertTrue(quiet.filter(self.record(None)))


if __name__ == "__main__":
    unittest.main()
