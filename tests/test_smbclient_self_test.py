from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from timecapsulesmb.checks import smb
from timecapsulesmb.checks.smb import broken_smbclient_output


# What a working smbclient prints when lmhosts has no such name (Samba 4.19 and 4.25).
WORKING_SMBCLIENT = """#!/bin/sh
echo 'do_connect: Connection to tcapsule-selftest.invalid failed (Error NT_STATUS_UNSUCCESSFUL)' >&2
exit 1
"""
# A client that aborts while setting up its connection: Samba's crash handler
# prints a backtrace and exits 1, with no NT_STATUS.
CRASHING_SMBCLIENT = """#!/bin/sh
echo ' #9 9   libtevent.0.17.2.dylib  0x000000010d61b63f tevent_context_init_ops + 33' >&2
echo 'Can not dump core: corepath not set up' >&2
exit 1
"""
SILENT_SMBCLIENT = """#!/bin/sh
exit 1
"""
HANGING_SMBCLIENT = """#!/bin/sh
exec /bin/sleep 600
"""


class BrokenSmbclientOutputTests(unittest.TestCase):
    def setUp(self) -> None:
        broken_smbclient_output.cache_clear()
        self.addCleanup(broken_smbclient_output.cache_clear)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.bin_dir = Path(tmp.name)

    def run_with(self, script: str | None) -> str | None:
        """What doctor learns about the smbclient on PATH, found and tested the
        way doctor does off macOS."""
        if script is not None:
            tool = self.bin_dir / "smbclient"
            tool.write_text(script)
            tool.chmod(0o755)
        with mock.patch.dict(os.environ, {"PATH": str(self.bin_dir)}), \
             mock.patch.object(smb.sys, "platform", "linux"):
            return smb.local_smb_client().problem()

    def test_a_smbclient_that_reports_the_failed_lookup_works(self) -> None:
        self.assertIsNone(self.run_with(WORKING_SMBCLIENT))

    def test_a_smbclient_that_reports_on_stdout_works(self) -> None:
        self.assertIsNone(self.run_with(WORKING_SMBCLIENT.replace(" >&2", "")))

    def test_a_smbclient_that_crashes_returns_its_output(self) -> None:
        output = self.run_with(CRASHING_SMBCLIENT)
        self.assertIn("Can not dump core: corepath not set up", output)

    def test_a_smbclient_that_prints_nothing_does_not_work(self) -> None:
        self.assertEqual(self.run_with(SILENT_SMBCLIENT), "exited with rc=1")

    def test_a_smbclient_that_does_not_finish_counts_as_working(self) -> None:
        # The fake never exits, so the short timeout cannot race it.
        with mock.patch.object(smb, "SMBCLIENT_SELF_TEST_TIMEOUT_SECONDS", 0.2):
            self.assertIsNone(self.run_with(HANGING_SMBCLIENT))

    def test_a_smbclient_that_cannot_start_does_not_work(self) -> None:
        self.assertIsNotNone(self.run_with("not a program\n"))

    def test_a_missing_smbclient_is_not_reported_as_broken(self) -> None:
        # Doctor's local tool check reports a missing smbclient on its own.
        self.assertIsNone(self.run_with(None))

    @unittest.skipUnless(shutil.which("smbclient"), "no smbclient on this computer")
    def test_this_computers_smbclient_passes(self) -> None:
        self.assertIsNone(broken_smbclient_output(shutil.which("smbclient")))


if __name__ == "__main__":
    unittest.main()
