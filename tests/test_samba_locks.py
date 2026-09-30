"""tests/samba/locks.py: claiming and releasing rows of the shared lock file."""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from tests.samba.locks import Busy, Locks, Unknown, claim, holders, release

TABLE = """Shared resource locks. Rules: see AGENTS.md.

| Resource       | Holder | Since (Mac time) | Doing |
| VM checkout    |        |                  |       |
| VM Samba lanes | other (wt @ abc) | 2026-09-28 10:00 | building |
| NetBSD 6       |        |                  |       |
| NetBSD 4       | me (wt @ 123) | 2026-09-28 11:00 | old job |
"""


class HoldersTest(unittest.TestCase):
    def test_reads_every_row_but_the_header(self) -> None:
        self.assertEqual(holders(TABLE), {"VM checkout": "", "VM Samba lanes": "other (wt @ abc)",
                                          "NetBSD 6": "", "NetBSD 4": "me (wt @ 123)"})


class ClaimTest(unittest.TestCase):
    def test_claims_free_rows_and_keeps_the_rest(self) -> None:
        text = claim(TABLE, ["NetBSD 6"], "me (wt @ 123)", "tests", "2026-09-28 12:00")
        rows = holders(text)
        self.assertEqual(rows["NetBSD 6"], "me (wt @ 123)")
        self.assertEqual(rows["VM Samba lanes"], "other (wt @ abc)")
        self.assertIn("| 2026-09-28 12:00 | tests |", text)
        self.assertTrue(text.startswith("Shared resource locks."))

    def test_reclaims_its_own_row(self) -> None:
        text = claim(TABLE, ["NetBSD 4"], "me (wt @ 123)", "new job", "2026-09-28 12:00")
        self.assertIn("new job", text)
        self.assertNotIn("old job", text)

    def test_a_row_someone_else_holds_is_busy_and_nothing_changes(self) -> None:
        with self.assertRaises(Busy):
            claim(TABLE, ["NetBSD 6", "VM Samba lanes"], "me (wt @ 123)", "x", "now")

    def test_a_resource_without_a_row_is_an_error(self) -> None:
        with self.assertRaises(Unknown):
            claim(TABLE, ["NetBSD 5"], "me (wt @ 123)", "x", "now")


class ReleaseTest(unittest.TestCase):
    def test_frees_only_its_own_rows(self) -> None:
        text = release(TABLE, ["NetBSD 4", "VM Samba lanes"], "me (wt @ 123)")
        rows = holders(text)
        self.assertEqual(rows["NetBSD 4"], "")
        self.assertEqual(rows["VM Samba lanes"], "other (wt @ abc)")


class LocksTest(unittest.TestCase):
    def test_holds_rows_for_the_block_and_frees_them_after_a_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "semaphore.txt"
            path.write_text(TABLE)
            with self.assertRaises(RuntimeError):
                with Locks(["NetBSD 6", "VM checkout"], "me (wt @ 123)", "check", path):
                    rows = holders(path.read_text())
                    self.assertEqual(rows["NetBSD 6"], "me (wt @ 123)")
                    self.assertEqual(rows["VM checkout"], "me (wt @ 123)")
                    raise RuntimeError("step failed")
            rows = holders(path.read_text())
            self.assertEqual(rows["NetBSD 6"], "")
            self.assertEqual(rows["VM checkout"], "")
            self.assertEqual(rows["NetBSD 4"], "me (wt @ 123)")  # not part of this block

    def test_busy_rows_are_not_claimed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "semaphore.txt"
            path.write_text(TABLE)
            with self.assertRaises(Busy):
                with Locks(["VM Samba lanes"], "me (wt @ 123)", "check", path):
                    self.fail("entered a block without the lock")
            self.assertEqual(path.read_text(), TABLE)


if __name__ == "__main__":
    unittest.main()
