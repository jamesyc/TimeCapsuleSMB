"""tests/samba/torture.py: suite lists, known failures, result parsing and the report."""
from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest

from tests.samba.torture import (FULL_SUITES, KNOWN_FAILURES, QUICK_EXCLUDE, Known, command, compare,
                                 format_report, outcomes, parse_known, plan, run_all, selected)


class PlanTest(unittest.TestCase):
    def test_full_runs_everything_with_the_long_suites_split(self) -> None:
        full = plan("full")
        self.assertNotIn("smb2.dir", full)
        self.assertIn("smb2.dir.1kfiles_rename", full)
        self.assertIn("smb2.compound_find.compound_find_close", full)
        self.assertEqual({s for s in full if s.count(".") == 1},
                         set(FULL_SUITES) - {"smb2.dir", "smb2.compound_find"})
        self.assertLessEqual(QUICK_EXCLUDE, set(full))

    def test_quick_leaves_out_only_the_slow_subtests(self) -> None:
        quick = plan("quick")
        self.assertFalse(set(quick) & QUICK_EXCLUDE)
        self.assertNotIn("smb2.dir", quick)
        self.assertIn("smb2.dir.find", quick)
        self.assertIn("smb2.compound_find.compound_find_related", quick)
        # Every other suite still runs whole.
        self.assertEqual({s for s in quick if s.count(".") == 1},
                         set(FULL_SUITES) - {"smb2.dir", "smb2.compound_find"})

    def test_selected_runs_only_named_tests_from_the_full_list(self) -> None:
        self.assertEqual(selected("quick", None), plan("quick"))
        self.assertEqual(selected("quick", ["smb2.dir.1kfiles_rename", "smb2.rw"]),
                         ["smb2.dir.1kfiles_rename", "smb2.rw"])
        with self.assertRaises(ValueError):
            selected("full", ["smb2.nope"])

    def test_unknown_tier(self) -> None:
        with self.assertRaises(ValueError):
            plan("medium")


class KnownTest(unittest.TestCase):
    def test_flags_device_and_names_with_spaces(self) -> None:
        known = parse_known("# comment\n\nsmb2.create.gentest  # upstream\n"
                            "flaky smb2.name-mangling.mangle\n"
                            "flaky only=6 smb2.dir.1kfiles_rename # NetBSD 6\n"
                            "smb2.charset.Testing composite character (a umlaut)\n")
        self.assertEqual(known["smb2.create.gentest"], Known("smb2.create.gentest"))
        self.assertTrue(known["smb2.name-mangling.mangle"].flaky)
        self.assertEqual(known["smb2.dir.1kfiles_rename"], Known("smb2.dir.1kfiles_rename", True, "6"))
        self.assertIn("smb2.charset.Testing composite character (a umlaut)", known)

    def test_the_checked_in_list_parses_and_has_no_fixed_entries(self) -> None:
        known = parse_known(KNOWN_FAILURES.read_text())
        self.assertGreater(len(known), 50)
        # 0066 fixed this one; it must not be listed as expected to fail.
        self.assertNotIn("smb2.maximum_allowed.read_only_file", known)


class OutcomesTest(unittest.TestCase):
    def test_suite_run_names_its_children(self) -> None:
        text = ("test: gentest\nfailure: gentest [\n../x.c:1: bad\n]\nsuccess: open\n"
                "failure: Testing partial surrogate [\nskip: file-index\n")
        self.assertEqual(outcomes("smb2.create", text), {
            "smb2.create.gentest": "failure", "smb2.create.open": "success",
            "smb2.create.Testing partial surrogate": "failure", "smb2.create.file-index": "skip"})

    def test_subtest_run_is_named_once(self) -> None:
        self.assertEqual(outcomes("smb2.dir.find", "success: find\n"), {"smb2.dir.find": "success"})

    def test_a_suite_whose_only_test_shares_its_name(self) -> None:
        # smb2.dosmode runs one test called dosmode.
        self.assertEqual(outcomes("smb2.dosmode", "failure: dosmode [\n"), {"smb2.dosmode.dosmode": "failure"})


class CompareTest(unittest.TestCase):
    known = parse_known("smb2.create.gentest\nflaky smb2.name-mangling.mangle\nonly=6 smb2.dir.odd\n")

    def test_new_failure_fixed_and_flaky(self) -> None:
        report = compare({
            "smb2.create": {"smb2.create.gentest": "success", "smb2.create.open": "failure"},
            "smb2.name-mangling": {"smb2.name-mangling.mangle": "success"},
        }, self.known, "4le")
        self.assertEqual(report.new_failures, ["smb2.create.open"])
        self.assertEqual(report.fixed, ["smb2.create.gentest"])  # the flaky pass is not "fixed"
        self.assertFalse(report.ok)

    def test_known_failures_only_is_ok(self) -> None:
        report = compare({"smb2.create": {"smb2.create.gentest": "failure", "smb2.create.open": "success"}},
                         self.known, "6")
        self.assertTrue(report.ok)
        self.assertEqual((report.passed, report.failed), (1, 1))

    def test_device_specific_entries(self) -> None:
        results = {"smb2.dir": {"smb2.dir.odd": "failure"}}
        self.assertTrue(compare(results, self.known, "6").ok)
        self.assertEqual(compare(results, self.known, "4le").new_failures, ["smb2.dir.odd"])

    def test_a_run_with_no_results_fails(self) -> None:
        report = compare({"smb2.dir": {}}, self.known, "6")
        self.assertEqual(report.no_results, ["smb2.dir"])
        self.assertFalse(report.ok)
        self.assertIn("NO RESULTS  smb2.dir", format_report(report, "6"))


class RunTest(unittest.TestCase):
    def test_command_keeps_a_share_name_with_a_space_as_one_argument(self) -> None:
        argv = command("tc-smbtorture:x", Path("/tmp/auth"), "192.168.1.218", "AirPort Disk", "smb2.rw")
        self.assertIn("//192.168.1.218/AirPort Disk", argv)
        self.assertEqual(argv[-1], "smb2.rw")
        self.assertIn("/tmp/auth:/auth:ro", argv)

    def test_run_all_writes_each_output_and_hides_the_password_file(self) -> None:
        seen = []

        def fake(argv, capture_output, text):
            auth = Path(argv[argv.index("-v") + 1].split(":")[0])
            seen.append((auth.read_text(), oct(auth.stat().st_mode & 0o777)))
            return subprocess.CompletedProcess(argv, 1, stdout="failure: open [\nsuccess: blob\n", stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            results = run_all("img", {"TC_SAMBA_USER": "admin", "TC_PASSWORD": "pw"}, "h", "s",
                              ["smb2.create"], out, run=fake, log=lambda *_: None)
            self.assertEqual(results, {"smb2.create": {"smb2.create.open": "failure",
                                                       "smb2.create.blob": "success"}})
            self.assertIn("failure: open", (out / "smb2.create.txt").read_text())
        self.assertEqual(seen, [("username = admin\npassword = pw\n", "0o600")])


if __name__ == "__main__":
    unittest.main()
