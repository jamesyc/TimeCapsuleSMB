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
        compat = classify_device_compatibility("NetBSD", "6.0", "earmv4", "little")
        self.assertTrue(compat.supported)
        self.assertEqual(compat.payload_family, "netbsd6_samba4")
        self.assertEqual(compat.device_generation, "gen5")
        self.assertEqual(compat.exact_model, "TimeCapsule8,119")

    def test_classify_netbsd6_unknown_endianness_as_unsupported(self) -> None:
        compat = classify_device_compatibility("NetBSD", "6.0", "earmv4", "unknown")
        self.assertFalse(compat.supported)
        self.assertIsNone(compat.payload_family)
        self.assertEqual(compat.syap_candidates, ())
        self.assertEqual(compat.model_candidates, ())
        self.assertIn("unknown-endian", compat.message)

    def test_classify_netbsd6_big_endian_as_unsupported(self) -> None:
        compat = classify_device_compatibility("NetBSD", "6.0", "earmv4", "big")
        self.assertFalse(compat.supported)
        self.assertIsNone(compat.payload_family)
        self.assertEqual(compat.syap_candidates, ())
        self.assertEqual(compat.model_candidates, ())
        self.assertIn("big-endian", compat.message)

    def test_classify_netbsd4_as_supported(self) -> None:
        compat = classify_device_compatibility("NetBSD", "4.0", "earmv4", "little")
        self.assertTrue(compat.supported)
        self.assertEqual(compat.payload_family, "netbsd4le_samba4")
        self.assertEqual(compat.device_generation, "gen1-4")
        self.assertEqual(compat.model_candidates, ("TimeCapsule6,113", "TimeCapsule6,116"))
        self.assertEqual(compat.elf_endianness, "little")
        self.assertIn("NetBSD 4", compat.message)

    def test_classify_netbsd4_big_endian_as_supported(self) -> None:
        compat = classify_device_compatibility("NetBSD", "4.0_STABLE", "earmv4", "big")
        self.assertTrue(compat.supported)
        self.assertEqual(compat.payload_family, "netbsd4be_samba4")
        self.assertEqual(compat.elf_endianness, "big")

    def test_classify_netbsd4_unknown_endianness_as_unsupported(self) -> None:
        compat = classify_device_compatibility("NetBSD", "4.0_STABLE", "earmv4", "unknown")
        self.assertFalse(compat.supported)
        self.assertIsNone(compat.payload_family)
        self.assertEqual(compat.syap_candidates, ())
        self.assertEqual(compat.model_candidates, ())
        self.assertIn("unknown-endian", compat.message)

    def test_classify_netbsd5_as_unsupported_without_candidates(self) -> None:
        compat = classify_device_compatibility("NetBSD", "5.0", "earmv4", "little")
        self.assertFalse(compat.supported)
        self.assertIsNone(compat.payload_family)
        self.assertEqual(compat.syap_candidates, ())
        self.assertEqual(compat.model_candidates, ())
        self.assertIn("NetBSD 5.0", compat.message)

    def test_classify_other_os_as_unsupported(self) -> None:
        compat = classify_device_compatibility("Linux", "6.8", "armv7")
        self.assertFalse(compat.supported)
        self.assertIsNone(compat.payload_family)
        self.assertEqual(compat.syap_candidates, ())
        self.assertEqual(compat.model_candidates, ())
        self.assertIn("Unsupported device OS", compat.message)
