"""This Mac's nsmb.conf settings that keep Time Machine off SMB 3."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from timecapsulesmb.checks.nsmb_conf import check_nsmb_conf, parse_nsmb_conf, strtol_base0


class NsmbConfTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.global_path = Path(self.tmp.name) / "etc-nsmb.conf"
        self.user_path = Path(self.tmp.name) / "user-nsmb.conf"

    def check(self, global_text: str | None = None, user_text: str | None = None, *, platform="darwin"):
        if global_text is not None:
            self.global_path.write_text(global_text)
        if user_text is not None:
            self.user_path.write_text(user_text)
        return check_nsmb_conf(platform=platform, global_path=self.global_path, user_path=self.user_path)

    def statuses(self, results) -> list[tuple[str, str | None]]:
        return [(result.status, result.details.get("code")) for result in results]

    def test_other_platforms_have_no_nsmb_conf_to_check(self) -> None:
        self.assertEqual(self.check("[default]\nprotocol_vers_map=2\n", platform="linux"), ([], None))

    def test_no_file_allows_smb3(self) -> None:
        results, summary = self.check()
        self.assertEqual(self.statuses(results), [("PASS", None)])
        self.assertEqual(summary, {"global": {"present": False}, "user": {"present": False}})

    def test_a_global_default_without_smb3_fails_with_the_fix(self) -> None:
        # Issue #372's file, exactly.
        results, summary = self.check("[default]\nprotocol_vers_map=2\n")
        self.assertEqual(self.statuses(results), [("FAIL", "mac_smb3_disabled")])
        self.assertEqual(results[0].details["path"], str(self.global_path))
        self.assertIn("protocol_vers_map=2", results[0].message)
        self.assertIn("remove and add the disk again in Time Machine settings", results[0].message)
        self.assertEqual(summary["global"], {"present": True, "sections": [{"kind": "default", "protocol_vers_map": "2"}]})

    def test_every_bitmap_without_the_smb3_bit_fails_and_every_one_with_it_passes(self) -> None:
        # Read as macOS reads it: strtol(value, NULL, 0).
        for value, status in (("1", "FAIL"), ("2", "FAIL"), ("3", "FAIL"), ("0x2", "FAIL"), ("02", "FAIL"),
                              ("2 # only SMB 2", "FAIL"), ("4", "PASS"), ("6", "PASS"), ("7", "PASS"),
                              ("0x6", "PASS"), ("06", "PASS"), ("010", "FAIL")):
            with self.subTest(value=value):
                results, _ = self.check(f"[default]\nprotocol_vers_map={value}\n")
                self.assertEqual([result.status for result in results], [status])

    def test_a_value_that_reads_as_zero_or_less_is_not_a_cap(self) -> None:
        for value in ("0", "smb3", "", "-6"):
            with self.subTest(value=value):
                results, _ = self.check(f"[default]\nprotocol_vers_map={value}\n")
                self.assertEqual(self.statuses(results), [("PASS", None)])

    def test_strtol_base0_matches_c(self) -> None:
        cases = {"7": 7, " 7": 7, "07": 7, "010": 8, "08": 0, "0x1F": 31, "0X10z": 16, "0x": 0,
                 "12abc": 12, "+3": 3, "-3": -3, "": 0, "abc": 0}
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(strtol_base0(text), expected)

    def test_comments_spacing_and_names_are_read_as_the_smb_library_reads_them(self) -> None:
        text = "# managed by IT\nprotocol_vers_map=2\n\n[default]\n  ; old setting\n  protocol_vers_map = 2  \r\n"
        results, _ = self.check(text)
        # The line before any section belongs to no section.
        self.assertEqual(self.statuses(results), [("FAIL", "mac_smb3_disabled")])
        self.assertEqual(parse_nsmb_conf(text)[0].values, {"protocol_vers_map": "2"})

    def test_names_are_case_sensitive_and_global_means_default(self) -> None:
        # macOS ignores a capitalized key or section name, and reads [global] as [default].
        for text, status in (("[default]\nProtocol_Vers_Map=2\n", "PASS"), ("[Default]\nprotocol_vers_map=2\n", "WARN"),
                             ("[global]\nprotocol_vers_map=2\n", "FAIL")):
            with self.subTest(text=text):
                results, _ = self.check(text)
                self.assertEqual([result.status for result in results], [status])

    def test_a_cap_only_in_the_users_file_warns(self) -> None:
        results, summary = self.check(user_text="[default]\nprotocol_vers_map=2\n")
        self.assertEqual(self.statuses(results), [("WARN", "mac_smb3_disabled")])
        self.assertEqual(results[0].details["path"], str(self.user_path))
        self.assertEqual(summary["global"], {"present": False})

    def test_the_global_file_wins_over_the_users(self) -> None:
        results, _ = self.check("[default]\nprotocol_vers_map=7\n", "[default]\nprotocol_vers_map=2\n")
        self.assertEqual(self.statuses(results), [("PASS", None)])
        results, _ = self.check("[default]\nprotocol_vers_map=2\n", "[default]\nprotocol_vers_map=7\n")
        self.assertEqual(self.statuses(results), [("FAIL", "mac_smb3_disabled")])

    def test_sections_merge_as_macos_merges_them(self) -> None:
        # Same-named sections merge and the later value wins, in one file and
        # across both: the global file is read last. A key in only one file stays.
        cases = (
            (("[default]\nprotocol_vers_map=2\n[default]\nprotocol_vers_map=7\n", None), [("PASS", None)]),
            (("[default]\nprotocol_vers_map=7\n[default]\nprotocol_vers_map=2\n", None), [("FAIL", "mac_smb3_disabled")]),
            (("[default]\nsigning_required=no\n", "[default]\nprotocol_vers_map=2\n"), [("WARN", "mac_smb3_disabled")]),
            (("[default]\nprotocol_vers_map=6\n", "[default]\nprotocol_vers_map=2\n"), [("PASS", None)]),
        )
        for (global_text, user_text), expected in cases:
            with self.subTest(global_text=global_text, user_text=user_text):
                self.user_path.unlink(missing_ok=True)
                results, _ = self.check(global_text, user_text)
                self.assertEqual(self.statuses(results), expected)

    def test_spaces_in_brackets_name_another_section_and_spaces_in_keys_are_dropped(self) -> None:
        # rcfile.c keeps "[ default ]" as the section " default " and drops
        # every space in a key.
        results, _ = self.check("[ default ]\nprotocol_vers_map=2\n")
        self.assertEqual(self.statuses(results), [("WARN", "mac_smb3_disabled_for_server")])
        self.assertEqual(results[0].details["section"], " default ")
        results, _ = self.check("[default]\nprotocol_vers_map \t = 2\n")
        self.assertEqual(self.statuses(results), [("FAIL", "mac_smb3_disabled")])
        results, _ = self.check("[default]\nprotocol vers map=2\n")
        self.assertEqual(self.statuses(results), [("PASS", None)])

    def test_a_server_section_without_smb3_warns_and_keeps_its_name_out_of_telemetry(self) -> None:
        results, summary = self.check("[default]\nprotocol_vers_map=6\n[FILESERVER.CORP]\nprotocol_vers_map=2\n")
        self.assertEqual(self.statuses(results), [("WARN", "mac_smb3_disabled_for_server")])
        self.assertEqual(results[0].details["section"], "FILESERVER.CORP")
        self.assertNotIn("FILESERVER", repr(summary))
        self.assertEqual(
            summary["global"]["sections"],
            [{"kind": "default", "protocol_vers_map": "6"}, {"kind": "server", "protocol_vers_map": "2"}],
        )

    def test_a_share_section_cannot_set_the_protocol_and_is_ignored(self) -> None:
        results, summary = self.check("[SERVER:SHARE]\nprotocol_vers_map=2\n")
        self.assertEqual(self.statuses(results), [("PASS", None)])
        self.assertEqual(summary["global"]["sections"], [{"kind": "share", "protocol_vers_map": "2"}])

    def test_other_settings_do_not_matter_and_stay_out_of_telemetry(self) -> None:
        results, summary = self.check("[default]\nsigning_required=no\nminauth=ntlmv2\n")
        self.assertEqual(self.statuses(results), [("PASS", None)])
        self.assertEqual(summary["global"], {"present": True, "sections": []})

    def test_smb_neg_is_ignored_as_macos_ignores_it(self) -> None:
        for value in ("smb1_only", "smb2_only", "normal"):
            with self.subTest(value=value):
                results, summary = self.check(f"[default]\nsmb_neg={value}\n")
                self.assertEqual(self.statuses(results), [("PASS", None)])
                self.assertEqual(summary["global"], {"present": True, "sections": []})

    def test_protocol_vers_map_beside_smb_neg_still_decides(self) -> None:
        results, _ = self.check("[default]\nsmb_neg=normal\nprotocol_vers_map=2\n")
        self.assertEqual(self.statuses(results), [("FAIL", "mac_smb3_disabled")])

    def test_an_unreadable_file_warns(self) -> None:
        self.global_path.mkdir()
        results, summary = self.check()
        self.assertEqual([result.status for result in results], ["WARN"])
        self.assertIn(f"could not read {self.global_path}", results[0].message)
        self.assertEqual(summary["global"], {"present": True, "readable": False})


if __name__ == "__main__":
    unittest.main()
