"""The host-side parts of tests/samba/dir_device.py: how --compare reads two
records, and the SMB2 hard link request the deep case sends."""
from __future__ import annotations

import struct
import unittest
from unittest import mock

from tests.samba.dir_device import (HFS_FIRST, HFS_LAST, NETBSD4_LAST, TIME_RANGE, Client, compare_records,
                                    filetime, kept_time, time_range)
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


if __name__ == "__main__":
    unittest.main()
