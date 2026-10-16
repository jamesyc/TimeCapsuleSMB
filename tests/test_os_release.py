from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from timecapsulesmb.core.os_release import parse_os_release


class OsReleaseTests(unittest.TestCase):
    def test_parses_quoted_and_bare_values_and_skips_comments_and_noise(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "os-release"
            path.write_text(
                '# comment\n\nNAME="AlmaLinux"\nID=almalinux\nID_LIKE=\'rhel centos fedora\'\n'
                'no equals sign\nURL="https://example.invalid/?a=b"\n'
            )

            self.assertEqual(
                parse_os_release(path),
                {
                    "NAME": "AlmaLinux",
                    "ID": "almalinux",
                    "ID_LIKE": "rhel centos fedora",
                    "URL": "https://example.invalid/?a=b",
                },
            )

    def test_a_missing_or_unreadable_file_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(parse_os_release(Path(tmp) / "missing"), {})
            self.assertEqual(parse_os_release(Path(tmp)), {})


if __name__ == "__main__":
    unittest.main()
