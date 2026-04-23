from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from timecapsulesmb.discovery.bonjour import (
    Discovered,
    discover,
    discover_time_capsule_candidates,
    discovered_record_airport_syap,
    discovered_record_root_host,
    enrich_airport_properties_by_ipv4,
    looks_like_time_capsule,
    prefer_routable_ipv4,
    preferred_host,
)


class DiscoveryTests(unittest.TestCase):
    def test_preferred_host_uses_hostname_then_ipv4(self) -> None:
        record = Discovered(name="TC", hostname="capsule.local", ipv4=["10.0.0.2"])
        self.assertEqual(preferred_host(record), "capsule.local")

    def test_prefer_routable_ipv4_skips_link_local(self) -> None:
        record = Discovered(name="TC", hostname="", ipv4=["169.254.1.2", "10.0.0.4"])
        self.assertEqual(prefer_routable_ipv4(record), "10.0.0.4")

    def test_discovered_record_root_host_prefers_routable_ipv4(self) -> None:
        record = Discovered(name="TC", hostname="capsule.local", ipv4=["169.254.1.2", "10.0.0.4"])
        self.assertEqual(discovered_record_root_host(record), "root@10.0.0.4")

    def test_discovered_record_root_host_falls_back_to_hostname(self) -> None:
        record = Discovered(name="TC", hostname="capsule.local", ipv4=[])
        self.assertEqual(discovered_record_root_host(record), "root@capsule.local")

    def test_discovered_record_root_host_returns_none_when_no_host_data_exists(self) -> None:
        record = Discovered(name="TC", hostname="", ipv4=[], ipv6=[])
        self.assertIsNone(discovered_record_root_host(record))

    def test_discovered_record_airport_syap_returns_property_value(self) -> None:
        record = Discovered(name="TC", hostname="capsule.local", properties={"syAP": "119"})
        self.assertEqual(discovered_record_airport_syap(record), "119")

    def test_discovered_record_airport_syap_returns_none_when_missing(self) -> None:
        record = Discovered(name="TC", hostname="capsule.local", properties={})
        self.assertIsNone(discovered_record_airport_syap(record))

    def test_looks_like_time_capsule_matches_model_hint(self) -> None:
        self.assertTrue(looks_like_time_capsule("Base Station", "router.local", {"model": "AirPort Time Capsule"}))

    def test_discover_uses_ipv4_only_zeroconf(self) -> None:
        fake_zc = mock.Mock()
        fake_collector = mock.Mock()
        fake_collector.results.return_value = []
        fake_ip_version = mock.Mock()
        fake_ip_version.V4Only = object()
        fake_zeroconf_module = mock.Mock(Zeroconf=mock.Mock(return_value=fake_zc), IPVersion=fake_ip_version)

        with mock.patch.dict(sys.modules, {"zeroconf": fake_zeroconf_module}):
            with mock.patch("timecapsulesmb.discovery.bonjour.Collector", return_value=fake_collector):
                with mock.patch("timecapsulesmb.discovery.bonjour.time.sleep"):
                    discover(timeout=0.1)

        fake_zeroconf_module.Zeroconf.assert_called_once_with(ip_version=fake_ip_version.V4Only)
        fake_collector.start.assert_called_once()
        fake_zc.close.assert_called_once()

    def test_discover_time_capsule_candidates_delegates_to_discover(self) -> None:
        record = Discovered(name="TC", hostname="capsule.local")
        with mock.patch("timecapsulesmb.discovery.bonjour.discover", return_value=[record]) as discover_mock:
            results = discover_time_capsule_candidates(timeout=1.5)
        self.assertEqual(results, [record])
        discover_mock.assert_called_once_with(timeout=1.5)

    def test_enrich_airport_properties_by_ipv4_copies_syap_to_samba_record(self) -> None:
        airport = Discovered(
            name="James's AirPort Time Capsule",
            hostname="Jamess-AirPort-Time-Capsule.local",
            ipv4=["192.168.1.217"],
            services={"_airport._tcp.local."},
            properties={"syAP": "119", "syVs": "7.9.1"},
        )
        samba = Discovered(
            name="Time Capsule Samba 4",
            hostname="timecapsulesamba4.local",
            ipv4=["192.168.1.217"],
            services={"_smb._tcp.local.", "_device-info._tcp.local."},
            properties={"model": "TimeCapsule8,119"},
        )
        enrich_airport_properties_by_ipv4([airport, samba])
        self.assertEqual(samba.properties["syAP"], "119")
        self.assertEqual(samba.properties["syVs"], "7.9.1")
        self.assertEqual(samba.properties["model"], "TimeCapsule8,119")

    def test_enrich_airport_properties_by_ipv4_does_not_cross_devices(self) -> None:
        airport = Discovered(
            name="Other AirPort Time Capsule",
            hostname="other.local",
            ipv4=["192.168.1.72"],
            services={"_airport._tcp.local."},
            properties={"syAP": "106"},
        )
        samba = Discovered(
            name="Time Capsule Samba 4",
            hostname="timecapsulesamba4.local",
            ipv4=["192.168.1.217"],
            services={"_smb._tcp.local."},
            properties={"model": "TimeCapsule8,119"},
        )
        enrich_airport_properties_by_ipv4([airport, samba])
        self.assertNotIn("syAP", samba.properties)


if __name__ == "__main__":
    unittest.main()
