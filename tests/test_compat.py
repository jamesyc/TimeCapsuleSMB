from __future__ import annotations

import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from timecapsulesmb.device.compat import classify_device_compatibility


class CompatibilityTests(unittest.TestCase):
    def test_classify_netbsd6_as_supported(self) -> None:
        compat = classify_device_compatibility("NetBSD", "6.0", "earmv4")
        self.assertTrue(compat.supported)
        self.assertEqual(compat.payload_family, "netbsd6_samba4")

    def test_classify_netbsd4_as_unsupported(self) -> None:
        compat = classify_device_compatibility("NetBSD", "4.0", "earmv4")
        self.assertFalse(compat.supported)
        self.assertIsNone(compat.payload_family)
        self.assertIn("NetBSD 4", compat.message)

    def test_classify_other_os_as_unsupported(self) -> None:
        compat = classify_device_compatibility("Linux", "6.8", "armv7")
        self.assertFalse(compat.supported)
        self.assertIsNone(compat.payload_family)
        self.assertIn("Unsupported device OS", compat.message)
