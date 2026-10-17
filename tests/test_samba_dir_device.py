"""The host-side parts of tests/samba/dir_device.py: how --compare reads two
records, the SMB2 hard link request the deep case sends, the AAPL query a Mac
client sends, and how the rename-open-below case judges a server."""
from __future__ import annotations

import itertools
import shlex
import struct
import unittest
from unittest import mock

from smbprotocol.exceptions import SMBResponseException
from smbprotocol.header import SMB2HeaderResponse

from tests.samba.dir_device import (FILE_DIRECTORY_FILE, FILE_NON_DIRECTORY_FILE, HFS_FIRST, HFS_LAST,
                                    NETBSD4_LAST, RENAME_ACCESS, STATUS_ACCESS_DENIED, TIME_RANGE, Client,
                                    aapl_query, compare_records, filetime, kept_time, rename_open_below,
                                    time_range)
from tests.samba.links_device import Results


class CompareRecordsTest(unittest.TestCase):
    def test_same_records_report_nothing(self) -> None:
        record = {"deep": {"levels": 40, "rename_up": "0x00000000"}}
        self.assertEqual(compare_records(record, record), [])

    def test_changed_value_is_a_change(self) -> None:
        before = {"deep": {"rename_up": "0xc0000095"}}
        after = {"deep": {"rename_up": "0x00000000"}}
        self.assertEqual(compare_records(before, after), ["CHANGED deep rename_up: 0xc0000095 -> 0x00000000"])

    def test_renamed_key_is_new_and_gone_not_a_change(self) -> None:
        # An older record has rename_across; the current script records rename_up.
        before = {"deep": {"levels": 40, "rename_across": "0xc0000095"}}
        after = {"deep": {"levels": 40, "rename_up": "0x00000000"}}
        self.assertEqual(compare_records(before, after), [
            "GONE deep rename_across: 0xc0000095",
            "NEW deep rename_up: 0x00000000",
        ])

    def test_case_only_in_one_record(self) -> None:
        # A case skipped in one run (None when it crashed, absent when not run).
        self.assertEqual(compare_records({"deep": None}, {"deep": {"levels": 40}}), ["NEW deep levels: 40"])
        self.assertEqual(compare_records({"times": {"file": 1}}, {}), ["GONE times file: 1"])

    def test_values_are_compared_exactly(self) -> None:
        before = {"deep": {"delete_refused_levels": [40]}}
        self.assertEqual(compare_records(before, {"deep": {"delete_refused_levels": [40]}}), [])
        self.assertEqual(compare_records(before, {"deep": {"delete_refused_levels": [39, 40]}}),
                         ["CHANGED deep delete_refused_levels: [40] -> [39, 40]"])


class LinkRequestTest(unittest.TestCase):
    def client(self) -> Client:
        client = Client.__new__(Client)  # no connection: only the request layout is under test
        client.set_info = mock.Mock()
        return client

    def test_link_sends_file_link_information(self) -> None:
        client = self.client()
        handle = object()
        client.link(handle, "__tc_dir_test__\\glink")
        name = "__tc_dir_test__\\glink".encode("utf-16-le")
        client.set_info.assert_called_once_with(
            handle, 11, struct.pack("<B7xQI", 0, 0, len(name)) + name)

    def test_link_replace_flag(self) -> None:
        client = self.client()
        client.link(object(), "x", replace=True)
        buffer = client.set_info.call_args.args[2]
        self.assertEqual(buffer[0], 1)
        self.assertEqual(struct.unpack_from("<I", buffer, 16)[0], 2)
        self.assertEqual(buffer[20:], "x".encode("utf-16-le"))

    def test_link_and_rename_share_the_layout(self) -> None:
        # FileLinkInformation (11) and FileRenameInformation (10) have the same
        # layout; only the class differs.
        client = self.client()
        client.link(object(), "a\\b")
        client.rename(object(), "a\\b")
        (_, link_class, link_buffer), (_, rename_class, rename_buffer) = (
            c.args for c in client.set_info.call_args_list)
        self.assertEqual((link_class, rename_class), (11, 10))
        self.assertEqual(link_buffer, rename_buffer)



class DeviceTimes:
    """A client over a device that keeps [first, last] of a write time the way
    tc_at_emulation.c clamps it, or returns what `answer` says."""

    def __init__(self, answer) -> None:
        self.answer = answer
        self.stored = None
        self.set_calls: list[int] = []

    def open(self, path, access, disposition, options):
        return mock.Mock(path=path)

    def set_write_time(self, handle, value: int) -> None:
        self.set_calls.append(value)
        self.stored = self.answer(value)

    def write_time(self, handle) -> int:
        return self.stored


def unix_of(value: int) -> int:
    return value // 10_000_000 - 11644473600


class TimeRangeTest(unittest.TestCase):
    def test_kept_time_per_device(self) -> None:
        self.assertEqual(kept_time(2147385600, netbsd4=False), 2147385600)   # 2038-01-18
        self.assertEqual(kept_time(2147385600, netbsd4=True), 2147385600)
        self.assertEqual(kept_time(2212012800, netbsd4=False), 2212012800)   # 2040-02-05
        self.assertEqual(kept_time(2212012800, netbsd4=True), NETBSD4_LAST)
        self.assertEqual(kept_time(HFS_LAST + 1, netbsd4=False), HFS_LAST)
        self.assertEqual(kept_time(HFS_FIRST - 1, netbsd4=False), HFS_FIRST)
        self.assertEqual(kept_time(HFS_FIRST - 1, netbsd4=True), HFS_FIRST)
        self.assertEqual(kept_time(-63158400, netbsd4=True), -63158400)      # 1968 while cached

    def run_range(self, netbsd4: bool, answer):
        client, r = DeviceTimes(answer), Results()
        with mock.patch("builtins.print"):
            time_range(r, client, netbsd4)
        return client, r

    def test_a_device_that_clamps_passes(self) -> None:
        for netbsd4 in (False, True):
            client, r = self.run_range(netbsd4, lambda v, n=netbsd4: filetime(kept_time(unix_of(v), n)))
            self.assertEqual(r.failed, [], netbsd4)
            self.assertEqual(r.passed, len(TIME_RANGE))
            self.assertEqual([unix_of(v) for v in client.set_calls], [unix for _, unix in TIME_RANGE])

    def test_samba_s_never_or_a_wrapped_time_fails(self) -> None:
        never = 0x7FFFFFFFFFFFFFFF
        _, r = self.run_range(False, lambda v: never if unix_of(v) > 2147483647 else v)
        # Today's smbd also hands 1903 to the kernel unclamped.
        self.assertEqual(r.failed, ["times: last-write time 2040-02-05 reads back as 2040-02-05 00:00:00",
                                    "times: last-write time 2106-02-07 reads back as 2040-02-06 06:28:15",
                                    "times: last-write time 1903-12-31 reads back as 1904-01-01 00:00:00"])
        # The kernel's own wrap: 2106 stored modulo 2^32 reads back as 1970.
        _, r = self.run_range(False, lambda v: filetime(0) if unix_of(v) > HFS_LAST else
                              filetime(kept_time(unix_of(v), False)))
        self.assertEqual(r.failed, ["times: last-write time 2106-02-07 reads back as 2040-02-06 06:28:15"])

    def test_netbsd4_expects_its_own_last_second(self) -> None:
        # A NetBSD 6 answer on NetBSD 4 fails where the two differ.
        _, r = self.run_range(True, lambda v: filetime(kept_time(unix_of(v), False)))
        self.assertEqual(r.failed, ["times: last-write time 2040-02-05 reads back as 2038-01-19 03:14:06",
                                    "times: last-write time 2106-02-07 reads back as 2038-01-19 03:14:06"])


class AaplQueryTest(unittest.TestCase):
    def test_the_query_is_the_24_byte_server_query_fruit_accepts(self) -> None:
        context = aapl_query()
        self.assertEqual(context["buffer_name"].get_value(), b"AAPL")
        data = context["buffer_data"].get_value()
        self.assertEqual(len(data), 24)
        # Command 1 (server query); server caps, volume caps and model; the client's caps.
        self.assertEqual(struct.unpack("<IIQQ", data), (1, 0, 7, 7))

    def connect(self, aapl: bool) -> mock.Mock:
        with mock.patch("smbprotocol.connection.Connection"), mock.patch("smbprotocol.session.Session"), \
                mock.patch("smbprotocol.tree.TreeConnect"), mock.patch.object(Client, "open") as open_:
            device = mock.Mock(host="tc", share="Data", env={"TC_PASSWORD": "pw"})
            Client(device, aapl=aapl)
        return open_

    def test_a_mac_client_opens_the_share_root_with_the_query_and_closes_it(self) -> None:
        open_ = self.connect(aapl=True)
        open_.assert_called_once()
        (path, _, disposition, options), kwargs = open_.call_args
        self.assertEqual((path, disposition, options), ("", 1, FILE_DIRECTORY_FILE))
        self.assertEqual([c["buffer_name"].get_value() for c in kwargs["contexts"]], [b"AAPL"])
        open_.return_value.close.assert_called_once()

    def test_a_windows_client_sends_no_query(self) -> None:
        self.connect(aapl=False).assert_not_called()


def refused(status: int) -> SMBResponseException:
    header = SMB2HeaderResponse()
    header["status"] = status
    return SMBResponseException(header)


class FakeDevice:
    """The test folder on a device: its paths, as `mkdir -p` and `test -e` see them."""

    def __init__(self) -> None:
        self.dir = "/Volumes/dk2/__tc_dir_test__"
        self.paths: dict[str, int] = {}  # path -> inode
        self.data: dict[int, bytearray] = {}
        self.inodes = itertools.count(1)

    def sh(self, command: str, check: bool = True) -> str:
        args = shlex.split(command)
        if args[:2] == ["mkdir", "-p"]:
            self.paths.setdefault(args[2], next(self.inodes))
            return ""
        if args[0] == "test":
            return "present" if self.exists(args[2]) else "absent"
        raise AssertionError(command)

    def exists(self, path: str) -> bool:
        return any(p == path or p.startswith(path + "/") for p in self.paths)


class FakeClient:
    """An smbd connection over FakeDevice. `allow(aapl)` is the server's rule for
    renaming a directory with this client's file open inside; `move` False makes
    a rename that reports success but moves nothing."""

    def __init__(self, device: FakeDevice, aapl: bool, allow, move: bool = True) -> None:
        self.device, self.aapl, self.allow, self.move = device, aapl, allow, move
        self.opens: list[tuple[str, int, int]] = []
        self.open_inodes: set[int] = set()
        self.closed = False

    def full(self, smb_path: str) -> str:
        return "/".join([self.device.dir] + smb_path.split("\\")[1:])

    def open(self, path: str, access: int, disposition: int = 1, options: int = 0):
        full = self.full(path)
        self.opens.append((path, access, options))
        if options & FILE_NON_DIRECTORY_FILE and full not in self.device.paths:
            self.device.paths[full] = next(self.device.inodes)
        inode = self.device.paths[full]
        data = self.device.data.setdefault(inode, bytearray())
        handle = mock.Mock(inode=inode, full=full)

        def write(chunk: bytes, offset: int) -> None:
            data[offset:offset + len(chunk)] = chunk

        handle.write.side_effect = write
        handle.read.side_effect = lambda offset, length: bytes(data[offset:offset + length])
        handle.close.side_effect = lambda: self.open_inodes.discard(inode)
        if options & FILE_NON_DIRECTORY_FILE:
            self.open_inodes.add(inode)
        return handle

    def rename(self, handle, new_path: str) -> None:
        old, new = handle.full, self.full(new_path)
        below = [p for p, i in self.device.paths.items() if p.startswith(old + "/") and i in self.open_inodes]
        if below and not self.allow(self.aapl):
            raise refused(STATUS_ACCESS_DENIED)
        if self.move:
            self.device.paths = {(new + p[len(old):] if p == old or p.startswith(old + "/") else p): i
                                 for p, i in self.device.paths.items()}

    def close(self) -> None:
        self.closed = True


class RenameOpenBelowTest(unittest.TestCase):
    WINDOWS = "rename-open-below: without AAPL a directory holding an open file is refused (Windows semantics)"
    MAC = "rename-open-below: a Mac renames a directory holding its open file"

    def run_case(self, allow, move: bool = True):
        device, clients, r, record = FakeDevice(), [], Results(), {}

        def connect(aapl: bool) -> FakeClient:
            clients.append(FakeClient(device, aapl, allow, move))
            return clients[-1]

        with mock.patch("builtins.print"):
            rename_open_below(r, device, record, connect=connect)
        return device, clients, r, record

    def test_a_fixed_smbd_refuses_windows_and_lets_a_mac_rename(self) -> None:
        device, clients, r, record = self.run_case(lambda aapl: aapl)

        self.assertEqual(r.failed, [])
        self.assertEqual(r.passed, 3)
        self.assertEqual(record, {"rename-open-below": {"windows": "0xc0000022", "mac": "0x00000000"}})
        self.assertEqual([c.aapl for c in clients], [False, True])
        for client in clients:
            # The band is open while the folder is opened the way macOS renames it.
            (band, _, band_options), (folder, access, options) = client.opens[:2]
            self.assertTrue(band.endswith("\\band") and band_options & FILE_NON_DIRECTORY_FILE)
            self.assertEqual((folder, access, options), (band.rsplit("\\", 1)[0], RENAME_ACCESS,
                                                         FILE_DIRECTORY_FILE))
            self.assertTrue(client.closed)
            self.assertEqual(client.open_inodes, set())
        self.assertTrue(device.exists(f"{device.dir}/windows bundle/band"))
        self.assertTrue(device.exists(f"{device.dir}/mac renamed.sparsebundle/band"))
        self.assertFalse(device.exists(f"{device.dir}/mac bundle"))
        mac_band = device.paths[f"{device.dir}/mac renamed.sparsebundle/band"]
        self.assertEqual(bytes(device.data[mac_band]), b"band data")

    def test_stock_smbd_refusing_the_mac_fails_only_the_mac_check(self) -> None:
        device, _, r, record = self.run_case(lambda aapl: False)

        self.assertEqual(r.failed, [f"{self.MAC} (0xc0000022)"])
        self.assertEqual(r.passed, 1)
        self.assertEqual(record["rename-open-below"]["mac"], "0xc0000022")
        self.assertTrue(device.exists(f"{device.dir}/mac bundle/band"))

    def test_losing_windows_semantics_fails_the_windows_check(self) -> None:
        _, _, r, _ = self.run_case(lambda aapl: True)

        self.assertEqual(r.failed, [f"{self.WINDOWS} (0x00000000)"])
        self.assertEqual(r.passed, 3)

    def test_a_success_that_moves_nothing_fails(self) -> None:
        _, _, r, _ = self.run_case(lambda aapl: aapl, move=False)

        self.assertEqual(r.failed, [f"{self.MAC} (0x00000000)"])
        # Only the Windows check passed: nothing moved, so no read under the new name.
        self.assertEqual(r.passed, 1)


if __name__ == "__main__":
    unittest.main()
