from __future__ import annotations

import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from timecapsulesmb.device.compat import (
    DeviceCompatibility,
    PAYLOAD_FAMILY_NETBSD4BE,
    PAYLOAD_FAMILY_NETBSD4LE,
    PAYLOAD_FAMILY_NETBSD6,
    classify_device_compatibility,
    is_netbsd4_payload_family,
    is_netbsd6_payload_family,
    payload_family_description,
    require_compatibility,
    render_compatibility_message,
)


class CompatibilityTests(unittest.TestCase):
    def test_classify_netbsd6_as_supported(self) -> None:
        compat = classify_device_compatibility("NetBSD", "6.0", "earmv4", "little")
        self.assertTrue(compat.supported)
        self.assertEqual(compat.payload_family, "netbsd6_samba4")
        self.assertEqual(compat.device_generation, "gen5")
        self.assertEqual(compat.model_candidates, ("TimeCapsule8,119", "AirPort7,120"))

    def test_classify_netbsd6_unknown_endianness_as_unsupported(self) -> None:
        compat = classify_device_compatibility("NetBSD", "6.0", "earmv4", "unknown")
        self.assertFalse(compat.supported)
        self.assertIsNone(compat.payload_family)
        self.assertEqual(compat.syap_candidates, ())
        self.assertEqual(compat.model_candidates, ())
        self.assertEqual(compat.reason_code, "unsupported_netbsd6_endianness")
        self.assertIn("unknown-endian", render_compatibility_message(compat))

    def test_classify_netbsd6_big_endian_as_unsupported(self) -> None:
        compat = classify_device_compatibility("NetBSD", "6.0", "earmv4", "big")
        self.assertFalse(compat.supported)
        self.assertIsNone(compat.payload_family)
        self.assertEqual(compat.syap_candidates, ())
        self.assertEqual(compat.model_candidates, ())
        self.assertEqual(compat.reason_code, "unsupported_netbsd6_endianness")
        self.assertIn("big-endian", render_compatibility_message(compat))

    def test_classify_netbsd4_as_supported(self) -> None:
        compat = classify_device_compatibility("NetBSD", "4.0", "earmv4", "little")
        self.assertTrue(compat.supported)
        self.assertEqual(compat.payload_family, "netbsd4le_samba4")
        self.assertEqual(compat.device_generation, "gen1-4")
        self.assertEqual(compat.model_candidates, ("AirPort5,108", "TimeCapsule6,113", "AirPort5,114", "TimeCapsule6,116", "AirPort5,117"))
        self.assertEqual(compat.elf_endianness, "little")
        self.assertEqual(compat.reason_code, "supported_netbsd4")
        self.assertIn("NetBSD 4", render_compatibility_message(compat))

    def test_classify_netbsd4_big_endian_as_supported(self) -> None:
        compat = classify_device_compatibility("NetBSD", "4.0_STABLE", "earmv4", "big")
        self.assertTrue(compat.supported)
        self.assertEqual(compat.payload_family, "netbsd4be_samba4")
        self.assertEqual(compat.elf_endianness, "big")
        self.assertEqual(compat.exact_syap, None)
        self.assertEqual(compat.exact_model, None)
        self.assertEqual(compat.model_candidates, ("AirPort5,104", "AirPort5,105", "TimeCapsule6,106", "TimeCapsule6,109"))

    def test_classify_netbsd6_narrows_from_airport_extreme_airport_identity_model(self) -> None:
        compat = classify_device_compatibility(
            "NetBSD",
            "6.0",
            "earmv4",
            "little",
            airport_model="AirPort7,120",
            airport_syap="120",
        )
        self.assertTrue(compat.supported)
        self.assertEqual(compat.payload_family, "netbsd6_samba4")
        self.assertEqual(compat.exact_syap, "120")
        self.assertEqual(compat.exact_model, "AirPort7,120")

    def test_classify_netbsd4_big_endian_narrows_from_airport_identity_model(self) -> None:
        compat = classify_device_compatibility(
            "NetBSD",
            "4.0_STABLE",
            "earmv4",
            "big",
            airport_model="TimeCapsule6,106",
            airport_syap="106",
        )
        self.assertTrue(compat.supported)
        self.assertEqual(compat.payload_family, "netbsd4be_samba4")
        self.assertEqual(compat.exact_syap, "106")
        self.assertEqual(compat.exact_model, "TimeCapsule6,106")

    def test_classify_netbsd4_big_endian_narrows_from_airport_extreme_airport_identity_model(self) -> None:
        compat = classify_device_compatibility(
            "NetBSD",
            "4.0_STABLE",
            "earmv4",
            "big",
            airport_model="AirPort5,104",
            airport_syap="104",
        )
        self.assertTrue(compat.supported)
        self.assertEqual(compat.payload_family, "netbsd4be_samba4")
        self.assertEqual(compat.exact_syap, "104")
        self.assertEqual(compat.exact_model, "AirPort5,104")

    def test_classify_netbsd4_little_endian_narrows_from_airport_identity_model(self) -> None:
        compat = classify_device_compatibility(
            "NetBSD",
            "4.0_STABLE",
            "earmv4",
            "little",
            airport_model="TimeCapsule6,113",
            airport_syap="113",
        )
        self.assertTrue(compat.supported)
        self.assertEqual(compat.payload_family, "netbsd4le_samba4")
        self.assertEqual(compat.exact_syap, "113")
        self.assertEqual(compat.exact_model, "TimeCapsule6,113")

    def test_classify_netbsd4_little_endian_narrows_from_airport_extreme_airport_identity_model(self) -> None:
        compat = classify_device_compatibility(
            "NetBSD",
            "4.0_STABLE",
            "earmv4",
            "little",
            airport_model="AirPort5,117",
            airport_syap="117",
        )
        self.assertTrue(compat.supported)
        self.assertEqual(compat.payload_family, "netbsd4le_samba4")
        self.assertEqual(compat.exact_syap, "117")
        self.assertEqual(compat.exact_model, "AirPort5,117")

    def test_classify_netbsd4_keeps_candidate_set_when_airport_identity_mismatches_endian_lane(self) -> None:
        compat = classify_device_compatibility(
            "NetBSD",
            "4.0_STABLE",
            "earmv4",
            "big",
            airport_model="TimeCapsule6,113",
            airport_syap="113",
        )
        self.assertTrue(compat.supported)
        self.assertEqual(compat.syap_candidates, ("104", "105", "106", "109"))
        self.assertEqual(compat.model_candidates, ("AirPort5,104", "AirPort5,105", "TimeCapsule6,106", "TimeCapsule6,109"))
        self.assertIn("did not match detected device candidates", compat.reason_detail)

    def test_classify_netbsd4_unknown_endianness_as_unsupported(self) -> None:
        compat = classify_device_compatibility("NetBSD", "4.0_STABLE", "earmv4", "unknown")
        self.assertFalse(compat.supported)
        self.assertIsNone(compat.payload_family)
        self.assertEqual(compat.syap_candidates, ())
        self.assertEqual(compat.model_candidates, ())
        self.assertEqual(compat.reason_code, "unsupported_netbsd4_endianness")
        self.assertIn("unknown-endian", render_compatibility_message(compat))

    def test_classify_netbsd5_as_unsupported_without_candidates(self) -> None:
        compat = classify_device_compatibility("NetBSD", "5.0", "earmv4", "little")
        self.assertFalse(compat.supported)
        self.assertIsNone(compat.payload_family)
        self.assertEqual(compat.syap_candidates, ())
        self.assertEqual(compat.model_candidates, ())
        self.assertEqual(compat.reason_code, "unsupported_netbsd_release")
        self.assertIn("NetBSD 5.0", render_compatibility_message(compat))

    def test_classify_other_os_as_unsupported(self) -> None:
        compat = classify_device_compatibility("Linux", "6.8", "armv7")
        self.assertFalse(compat.supported)
        self.assertIsNone(compat.payload_family)
        self.assertEqual(compat.syap_candidates, ())
        self.assertEqual(compat.model_candidates, ())
        self.assertEqual(compat.reason_code, "unsupported_os")
        self.assertIn("Unsupported device OS", render_compatibility_message(compat))

    def test_render_supported_netbsd6_message_is_human_readable(self) -> None:
        compat = classify_device_compatibility("NetBSD", "6.0", "earmv4", "little")
        self.assertEqual(
            render_compatibility_message(compat),
            "Detected supported device: NetBSD 6.0 (earmv4, little-endian).",
        )

    def test_require_compatibility_raises_with_fallback_for_missing_probe(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            require_compatibility(None, fallback_error="probe failed")
        self.assertEqual(str(ctx.exception), "probe failed")

    def test_render_compatibility_message_falls_back_to_reason_detail(self) -> None:
        compat = DeviceCompatibility(
            os_name="NetBSD",
            os_release="9.9",
            arch="earmv4",
            elf_endianness="little",
            payload_family=None,
            device_generation="unknown",
            supported=False,
            reason_code="custom_reason",
            reason_detail="custom detail",
        )
        self.assertEqual(render_compatibility_message(compat), "custom detail")

    def test_payload_family_helpers_classify_supported_lanes(self) -> None:
        self.assertTrue(is_netbsd4_payload_family(PAYLOAD_FAMILY_NETBSD4LE))
        self.assertTrue(is_netbsd4_payload_family(PAYLOAD_FAMILY_NETBSD4BE))
        self.assertFalse(is_netbsd4_payload_family(PAYLOAD_FAMILY_NETBSD6))
        self.assertTrue(is_netbsd6_payload_family(PAYLOAD_FAMILY_NETBSD6))
        self.assertFalse(is_netbsd6_payload_family(PAYLOAD_FAMILY_NETBSD4LE))

    def test_payload_family_description_names_endian_lanes(self) -> None:
        self.assertEqual(payload_family_description(PAYLOAD_FAMILY_NETBSD4LE), "NetBSD 4 little-endian")
        self.assertEqual(payload_family_description(PAYLOAD_FAMILY_NETBSD4BE), "NetBSD 4 big-endian")
        self.assertEqual(payload_family_description(PAYLOAD_FAMILY_NETBSD6), "NetBSD 6 little-endian")
