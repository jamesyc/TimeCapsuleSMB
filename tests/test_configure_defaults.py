from __future__ import annotations

import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from timecapsulesmb.core.net import ipv4_literal  # noqa: E402


class ConfigureDefaultsTests(unittest.TestCase):
    def test_ipv4_literal_accepts_zero_padded_ipv4_and_rejects_ipv6_or_bad_octet(self) -> None:
        self.assertEqual(ipv4_literal("010.000.001.007"), "10.0.1.7")
        self.assertIsNone(ipv4_literal("fe80::1"))
        self.assertIsNone(ipv4_literal("10.0.1.999"))
        self.assertIsNone(ipv4_literal("capsule.local"))

if __name__ == "__main__":
    unittest.main()
