from __future__ import annotations

import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from timecapsulesmb.discovery.bonjour import Discovered, looks_like_time_capsule, prefer_routable_ipv4, preferred_host


class DiscoveryTests(unittest.TestCase):
    def test_preferred_host_uses_hostname_then_ipv4(self) -> None:
        record = Discovered(name="TC", hostname="capsule.local", ipv4=["10.0.0.2"])
        self.assertEqual(preferred_host(record), "capsule.local")

    def test_prefer_routable_ipv4_skips_link_local(self) -> None:
        record = Discovered(name="TC", hostname="", ipv4=["169.254.1.2", "10.0.0.4"])
        self.assertEqual(prefer_routable_ipv4(record), "10.0.0.4")

    def test_looks_like_time_capsule_matches_model_hint(self) -> None:
        self.assertTrue(looks_like_time_capsule("Base Station", "router.local", {"model": "AirPort Time Capsule"}))


if __name__ == "__main__":
    unittest.main()
