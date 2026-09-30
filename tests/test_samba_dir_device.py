"""The host-side parts of tests/samba/dir_device.py: how --compare reads two
records, and the SMB2 hard link request the deep case sends."""
from __future__ import annotations

import struct
import unittest
from unittest import mock

from tests.samba.dir_device import Client, compare_records


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


if __name__ == "__main__":
    unittest.main()
