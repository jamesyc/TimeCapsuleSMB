from __future__ import annotations

import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from timecapsulesmb.discovery.bonjour import (
    AIRPORT_SERVICE,
    BonjourDiscoverySnapshot,
    BonjourResolvedService,
    BonjourServiceInstance,
    Collector,
    Discovered,
    SMB_SERVICE,
    ServiceObservation,
    browse_service_instances,
    discover,
    discover_resolved_records,
    discovered_record_root_host,
    resolve_service_instance,
    resolved_service_from_info,
    filter_service_records,
    run_cli,
)


class DiscoveryTests(unittest.TestCase):
    def test_preferred_host_uses_hostname_then_ipv4(self) -> None:
        record = Discovered(name="TC", hostname="capsule.local", ipv4=["10.0.0.2"])
        self.assertEqual(record.prefer_host(), "capsule.local")

    def test_discovered_record_root_host_prefers_routable_ipv4(self) -> None:
        record = Discovered(name="TC", hostname="capsule.local", ipv4=["169.254.1.2", "10.0.0.4"])
        self.assertEqual(discovered_record_root_host(record), "root@10.0.0.4")

    def test_discovered_record_root_host_falls_back_to_hostname(self) -> None:
        record = Discovered(name="TC", hostname="capsule.local", ipv4=[])
        self.assertEqual(discovered_record_root_host(record), "root@capsule.local")

    def test_discovered_record_root_host_returns_none_when_no_host_data_exists(self) -> None:
        record = Discovered(name="TC", hostname="", ipv4=[], ipv6=[])
        self.assertIsNone(discovered_record_root_host(record))

    def test_discover_uses_ipv4_only_zeroconf(self) -> None:
        fake_zc = mock.Mock()
        fake_collector = mock.Mock()
        fake_collector.results.return_value = []
        fake_collector.service_instances.return_value = []
        fake_ip_version = mock.Mock()
        fake_ip_version.V4Only = object()
        fake_zeroconf_module = mock.Mock(Zeroconf=mock.Mock(return_value=fake_zc), IPVersion=fake_ip_version)

        with mock.patch.dict(sys.modules, {"zeroconf": fake_zeroconf_module}):
            with mock.patch("timecapsulesmb.discovery.bonjour.Collector", return_value=fake_collector):
                with mock.patch("timecapsulesmb.discovery.bonjour.time.sleep"):
                    discover_resolved_records(timeout=0)

        fake_zeroconf_module.Zeroconf.assert_called_once_with(ip_version=fake_ip_version.V4Only)
        fake_collector.start.assert_called_once()
        fake_collector.resolve_pending.assert_called_once_with(timeout_ms=2000)
        fake_zc.close.assert_called_once()

    def test_discover_retries_pending_resolution_during_browse_window(self) -> None:
        fake_zc = mock.Mock()
        fake_collector = mock.Mock()
        fake_collector.results.return_value = []
        fake_collector.service_instances.return_value = []
        fake_ip_version = mock.Mock()
        fake_ip_version.V4Only = object()
        fake_zeroconf_module = mock.Mock(Zeroconf=mock.Mock(return_value=fake_zc), IPVersion=fake_ip_version)

        with mock.patch.dict(sys.modules, {"zeroconf": fake_zeroconf_module}):
            with mock.patch("timecapsulesmb.discovery.bonjour.Collector", return_value=fake_collector):
                with mock.patch("timecapsulesmb.discovery.bonjour.time.monotonic", side_effect=[0.0, 0.0, 0.6]):
                    with mock.patch("timecapsulesmb.discovery.bonjour.time.sleep") as fake_sleep:
                        discover_resolved_records(timeout=0.5)

        fake_sleep.assert_called_once_with(0.5)
        fake_collector.resolve_pending.assert_has_calls([
            mock.call(timeout_ms=500),
            mock.call(timeout_ms=2000),
        ])
        self.assertEqual(fake_collector.resolve_pending.call_count, 2)

    def test_discover_includes_unresolved_browse_instances(self) -> None:
        fake_zc = mock.Mock()
        fake_collector = mock.Mock()
        instance = BonjourServiceInstance("_smb._tcp.local.", "Home", "Home._smb._tcp.local.")
        fake_collector.results.return_value = []
        fake_collector.service_instances.return_value = [instance]
        fake_ip_version = mock.Mock()
        fake_ip_version.V4Only = object()
        fake_zeroconf_module = mock.Mock(Zeroconf=mock.Mock(return_value=fake_zc), IPVersion=fake_ip_version)

        with mock.patch.dict(sys.modules, {"zeroconf": fake_zeroconf_module}):
            with mock.patch("timecapsulesmb.discovery.bonjour.Collector", return_value=fake_collector):
                with mock.patch("timecapsulesmb.discovery.bonjour.time.sleep"):
                    records = discover(timeout=0)

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].service_type, "_smb._tcp.local.")
        self.assertEqual(records[0].name, "Home")
        self.assertEqual(records[0].hostname, "")
        self.assertEqual(records[0].fullname, "Home._smb._tcp.local.")

    def test_browse_service_instances_returns_seen_instances_without_resolving(self) -> None:
        fake_zc = mock.Mock()
        fake_collector = mock.Mock()
        instance = BonjourServiceInstance("_smb._tcp.local.", "Home", "Home._smb._tcp.local.")
        fake_collector.service_instances.return_value = [instance]
        fake_ip_version = mock.Mock()
        fake_ip_version.V4Only = object()
        fake_zeroconf_module = mock.Mock(Zeroconf=mock.Mock(return_value=fake_zc), IPVersion=fake_ip_version)

        with mock.patch.dict(sys.modules, {"zeroconf": fake_zeroconf_module}):
            with mock.patch("timecapsulesmb.discovery.bonjour.Collector", return_value=fake_collector) as collector_cls:
                with mock.patch("timecapsulesmb.discovery.bonjour.time.sleep") as fake_sleep:
                    instances = browse_service_instances(SMB_SERVICE, timeout=0.25)

        collector_cls.assert_called_once_with(fake_zc, ["_smb._tcp.local."])
        fake_collector.start.assert_called_once()
        fake_collector.resolve_pending.assert_not_called()
        fake_sleep.assert_called_once_with(0.25)
        self.assertEqual(instances, [instance])

    def test_resolve_service_instance_returns_resolved_record(self) -> None:
        class FakeInfo:
            name = "Home._smb._tcp.local."
            server = "home.local."
            port = 445
            properties = {b"path": b"/"}
            addresses = [bytes([10, 0, 1, 1])]

        fake_zc = mock.Mock()
        fake_zc.get_service_info.return_value = FakeInfo()
        fake_ip_version = mock.Mock()
        fake_ip_version.V4Only = object()
        fake_zeroconf_module = mock.Mock(Zeroconf=mock.Mock(return_value=fake_zc), IPVersion=fake_ip_version)
        instance = BonjourServiceInstance("_smb._tcp.local.", "Home", "Home._smb._tcp.local.")

        with mock.patch.dict(sys.modules, {"zeroconf": fake_zeroconf_module}):
            record = resolve_service_instance(instance, timeout_ms=750)

        fake_zc.get_service_info.assert_called_once_with("_smb._tcp.local.", "Home._smb._tcp.local.", 750)
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.name, "Home")
        self.assertEqual(record.hostname, "home.local")
        self.assertEqual(record.port, 445)
        self.assertEqual(record.ipv4, ["10.0.1.1"])

    def test_resolved_service_from_info_splits_airport_packed_txt_value(self) -> None:
        class FakeInfo:
            name = "AirPort Time Capsule._airport._tcp.local."
            server = "AirPort-Time-Capsule.local."
            port = 5009
            properties = {
                b"waMA": (
                    b"00-23-DF-D9-7B-53,raMA=00-21-E9-B9-70-E3,raSt=3,"
                    b"raNA=0,syAP=106,syVs=7.8.1"
                ),
            }
            addresses = [bytes([192, 168, 1, 72])]

        record = resolved_service_from_info("_airport._tcp.local.", FakeInfo())

        self.assertEqual(record.properties["waMA"], "00-23-DF-D9-7B-53")
        self.assertEqual(record.properties["raMA"], "00-21-E9-B9-70-E3")
        self.assertEqual(record.properties["raSt"], "3")
        self.assertEqual(record.properties["raNA"], "0")
        self.assertEqual(record.properties["syAP"], "106")
        self.assertEqual(record.properties["syVs"], "7.8.1")

    def test_resolved_service_from_info_expands_sys_packed_txt_without_losing_raw_sys(self) -> None:
        class FakeInfo:
            name = "Home._adisk._tcp.local."
            server = "home.local."
            port = 9
            properties = {
                b"sys": b"waMA=80:EA:96:E6:58:68,adVF=0x1010",
            }
            addresses = [bytes([10, 0, 1, 1])]

        record = resolved_service_from_info("_adisk._tcp.local.", FakeInfo())

        self.assertEqual(record.properties["sys"], "waMA=80:EA:96:E6:58:68,adVF=0x1010")
        self.assertEqual(record.properties["waMA"], "80:EA:96:E6:58:68")
        self.assertEqual(record.properties["adVF"], "0x1010")

    def test_collector_queues_browse_events_and_resolves_after_browse_window(self) -> None:
        class FakeStateChange:
            Added = object()
            Updated = object()

        class FakeInfo:
            name = "Home._smb._tcp.local."
            server = "home.local."
            properties: dict[bytes, bytes] = {}
            addresses = [bytes([10, 0, 1, 1])]

        fake_zeroconf_module = mock.Mock(ServiceStateChange=FakeStateChange)
        fake_zc = mock.Mock()
        fake_zc.get_service_info.return_value = FakeInfo()
        collector = Collector(fake_zc, ["_smb._tcp.local."])

        with mock.patch.dict(sys.modules, {"zeroconf": fake_zeroconf_module}):
            collector._on_service_state_change(
                zeroconf=fake_zc,
                service_type="_smb._tcp.local.",
                name="Home._smb._tcp.local.",
                state_change=FakeStateChange.Added,
            )

        fake_zc.get_service_info.assert_not_called()
        collector.resolve_pending()

        fake_zc.get_service_info.assert_called_once_with("_smb._tcp.local.", "Home._smb._tcp.local.", 2000)
        records = collector.results()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].name, "Home")
        self.assertEqual(records[0].hostname, "home.local")
        self.assertEqual(records[0].ipv4, ["10.0.1.1"])

    def test_collector_keeps_failed_pending_records_for_later_retry(self) -> None:
        class FakeInfo:
            def __init__(self, name: str, server: str, address: str) -> None:
                self.name = name
                self.server = server
                self.properties: dict[bytes, bytes] = {}
                self.addresses = [bytes(int(part) for part in address.split("."))]

        fake_zc = mock.Mock()
        fake_zc.get_service_info.side_effect = [
            OSError("transient resolve failure"),
            FakeInfo("Kitchen._smb._tcp.local.", "kitchen.local.", "10.0.1.99"),
        ]
        collector = Collector(fake_zc, ["_smb._tcp.local."])
        collector.pending = {
            ("_smb._tcp.local.", "Home._smb._tcp.local."),
            ("_smb._tcp.local.", "Kitchen._smb._tcp.local."),
        }

        collector.resolve_pending(timeout_ms=500)

        self.assertEqual(fake_zc.get_service_info.call_count, 2)
        records = collector.results()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].name, "Kitchen")
        self.assertEqual(records[0].ipv4, ["10.0.1.99"])
        self.assertEqual(collector.pending, {("_smb._tcp.local.", "Home._smb._tcp.local.")})

    def test_collector_keeps_unresolved_pending_record_for_later_retry(self) -> None:
        class FakeInfo:
            name = "Home._smb._tcp.local."
            server = "home.local."
            properties: dict[bytes, bytes] = {}
            addresses = [bytes([10, 0, 1, 1])]

        fake_zc = mock.Mock()
        fake_zc.get_service_info.return_value = None
        collector = Collector(fake_zc, ["_smb._tcp.local."])
        collector.pending = {("_smb._tcp.local.", "Home._smb._tcp.local.")}

        collector.resolve_pending(timeout_ms=500)

        self.assertEqual(collector.results(), [])
        self.assertEqual(collector.pending, {("_smb._tcp.local.", "Home._smb._tcp.local.")})

        fake_zc.get_service_info.return_value = FakeInfo()
        collector.resolve_pending(timeout_ms=500)

        records = collector.results()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].name, "Home")
        self.assertEqual(records[0].ipv4, ["10.0.1.1"])
        self.assertEqual(collector.pending, set())

    def test_filter_service_records_returns_only_matching_service(self) -> None:
        airport = Discovered(
            name="James's AirPort Time Capsule",
            hostname="Jamess-AirPort-Time-Capsule.local",
            service_type="_airport._tcp.local.",
            ipv4=["192.168.1.217"],
            properties={"syAP": "119", "syVs": "7.9.1"},
        )
        samba = Discovered(
            name="Time Capsule Samba 4",
            hostname="timecapsulesamba4.local",
            service_type="_smb._tcp.local.",
            ipv4=["192.168.1.217"],
            properties={"model": "TimeCapsule8,119"},
        )
        self.assertEqual(filter_service_records([airport, samba], SMB_SERVICE), [samba])
        self.assertEqual(filter_service_records([airport, samba], "_smb._tcp.local"), [samba])

    def test_collector_results_preserve_raw_service_records(self) -> None:
        observations = {
            ("_airport._tcp.local.", "AirPort Time Capsule", "airport.local"): ServiceObservation(
                name="AirPort Time Capsule",
                hostname="airport.local",
                service_type="_airport._tcp.local.",
                ipv4=["192.168.1.217"],
                properties={"syAP": "119"},
            ),
            ("_smb._tcp.local.", "Time Capsule Samba 4", "timecapsulesamba4.local"): ServiceObservation(
                name="Time Capsule Samba 4",
                hostname="timecapsulesamba4.local",
                service_type="_smb._tcp.local.",
                ipv4=["192.168.1.217"],
            ),
            ("_device-info._tcp.local.", "Time Capsule Samba 4", "timecapsulesamba4.local"): ServiceObservation(
                name="Time Capsule Samba 4",
                hostname="timecapsulesamba4.local",
                service_type="_device-info._tcp.local.",
                ipv4=["192.168.1.217"],
                properties={"model": "TimeCapsule8,119"},
            ),
        }
        fake_collector = mock.Mock()
        fake_collector.results.return_value = [
            Discovered(
                name=observation.name,
                hostname=observation.hostname,
                service_type=observation.service_type,
                ipv4=list(observation.ipv4),
                properties=dict(observation.properties),
            )
            for observation in observations.values()
        ]
        fake_collector.service_instances.return_value = []
        fake_zc = mock.Mock()
        fake_ip_version = mock.Mock()
        fake_ip_version.V4Only = object()
        fake_zeroconf_module = mock.Mock(Zeroconf=mock.Mock(return_value=fake_zc), IPVersion=fake_ip_version)

        with mock.patch.dict(sys.modules, {"zeroconf": fake_zeroconf_module}):
            with mock.patch("timecapsulesmb.discovery.bonjour.Collector", return_value=fake_collector):
                with mock.patch("timecapsulesmb.discovery.bonjour.time.sleep"):
                    records = discover_resolved_records(timeout=0)

        self.assertEqual(
            {(record.service_type, record.name, record.hostname, tuple(record.ipv4)) for record in records},
            {
                ("_airport._tcp.local.", "AirPort Time Capsule", "airport.local", ("192.168.1.217",)),
                ("_smb._tcp.local.", "Time Capsule Samba 4", "timecapsulesamba4.local", ("192.168.1.217",)),
                ("_device-info._tcp.local.", "Time Capsule Samba 4", "timecapsulesamba4.local", ("192.168.1.217",)),
            },
        )

    def test_filter_service_records_accepts_service_prefix_for_airport(self) -> None:
        airport = Discovered(name="AirPort Time Capsule", hostname="airport.local", service_type="_airport._tcp.local")
        self.assertEqual(filter_service_records([airport], AIRPORT_SERVICE), [airport])

    def test_run_cli_prints_browse_and_resolved_tables(self) -> None:
        snapshot = BonjourDiscoverySnapshot(
            instances=[
                BonjourServiceInstance("_smb._tcp.local.", "Home", "Home._smb._tcp.local."),
            ],
            resolved=[
                BonjourResolvedService(
                    name="Home",
                    hostname="home.local",
                    service_type="_smb._tcp.local.",
                    port=445,
                    ipv4=["10.0.1.1"],
                    fullname="Home._smb._tcp.local.",
                ),
            ],
        )
        output = io.StringIO()
        with mock.patch("timecapsulesmb.discovery.bonjour.discover_snapshot", return_value=snapshot):
            with redirect_stdout(output):
                rc = run_cli([])
        text = output.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("Browse Results", text)
        self.assertIn("Resolved Records", text)
        self.assertIn("Home._smb._tcp.local.", text)
        self.assertIn("home.local", text)


if __name__ == "__main__":
    unittest.main()
