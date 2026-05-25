from __future__ import annotations

import io
import socket
import sys
import types
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
    BonjourPtrRecordObservation,
    BonjourResolvedService,
    BonjourServiceEvent,
    BonjourServiceInstance,
    Collector,
    DNS_RECORD_TYPE_PTR,
    Discovered,
    SMB_SERVICE,
    PtrRecordObserver,
    ServiceObservation,
    discover,
    discover_snapshot_detailed,
    discover_resolved_records,
    discovered_record_root_host,
    resolved_service_from_info,
    record_has_service,
    _open_zeroconf,
    _source_ipv4_for_target,
    _source_ipv6_for_target,
    _zeroconf_interfaces_for_target,
    resolve_service_instance,
)
from timecapsulesmb.cli.discover import run_cli  # noqa: E402


def make_fake_ip_version() -> types.SimpleNamespace:
    return types.SimpleNamespace(V4Only=object(), V6Only=object(), All=object())


class DiscoveryTests(unittest.TestCase):
    def test_preferred_host_uses_hostname_then_ipv4(self) -> None:
        record = Discovered(name="TC", hostname="capsule.local", ipv4=["10.0.0.2"])
        self.assertEqual(record.prefer_host(), "capsule.local")

    def test_discovered_record_root_host_prefers_routable_ipv4(self) -> None:
        record = Discovered(name="TC", hostname="capsule.local", ipv4=["169.254.1.2", "10.0.0.4"])
        self.assertEqual(discovered_record_root_host(record), "root@10.0.0.4")

    def test_discovered_record_root_host_rejects_link_local_only_ipv4(self) -> None:
        record = Discovered(name="TC", hostname="capsule.local", ipv4=["169.254.1.2"])
        self.assertIsNone(discovered_record_root_host(record))

    def test_discovered_record_root_host_falls_back_to_hostname(self) -> None:
        record = Discovered(name="TC", hostname="capsule.local", ipv4=[])
        self.assertEqual(discovered_record_root_host(record), "root@capsule.local")

    def test_discovered_record_root_host_returns_none_when_no_host_data_exists(self) -> None:
        record = Discovered(name="TC", hostname="", ipv4=[], ipv6=[])
        self.assertIsNone(discovered_record_root_host(record))

    def test_discover_uses_dual_stack_zeroconf(self) -> None:
        fake_zc = mock.Mock()
        fake_collector = mock.Mock()
        fake_collector.results.return_value = []
        fake_collector.service_instances.return_value = []
        fake_ip_version = make_fake_ip_version()
        fake_zeroconf_module = mock.Mock(Zeroconf=mock.Mock(return_value=fake_zc), IPVersion=fake_ip_version)

        with mock.patch.dict(sys.modules, {"zeroconf": fake_zeroconf_module}):
            with mock.patch("timecapsulesmb.discovery.bonjour.Collector", return_value=fake_collector):
                with mock.patch("timecapsulesmb.discovery.bonjour.time.sleep"):
                    discover_resolved_records(timeout=0)

        fake_zeroconf_module.Zeroconf.assert_called_once_with(ip_version=fake_ip_version.All)
        fake_collector.start.assert_called_once()
        fake_collector.resolve_pending.assert_called_once_with(timeout_ms=3000)
        fake_zc.close.assert_called_once()

    def test_discover_uses_selected_ipv4_interface_for_target(self) -> None:
        fake_zc = mock.Mock()
        fake_collector = mock.Mock()
        fake_collector.results.return_value = []
        fake_collector.service_instances.return_value = []
        fake_collector.service_events.return_value = []
        fake_collector.pending_count.return_value = 0
        fake_collector.service_added_count = 0
        fake_collector.service_updated_count = 0
        fake_collector.resolve_attempt_count = 0
        fake_collector.resolve_success_count = 0
        fake_collector.resolve_error_count = 0
        fake_ptr_observer = mock.Mock()
        fake_ptr_observer.observations.return_value = []
        fake_ptr_observer.error = None
        fake_ip_version = make_fake_ip_version()
        fake_zeroconf_module = mock.Mock(Zeroconf=mock.Mock(return_value=fake_zc), IPVersion=fake_ip_version)

        with mock.patch.dict(sys.modules, {"zeroconf": fake_zeroconf_module}):
            with mock.patch("timecapsulesmb.discovery.bonjour._source_ipv4_for_target", return_value="10.0.1.42") as source_mock:
                with mock.patch("timecapsulesmb.discovery.bonjour.Collector", return_value=fake_collector):
                    with mock.patch("timecapsulesmb.discovery.bonjour.PtrRecordObserver", return_value=fake_ptr_observer):
                        with mock.patch("timecapsulesmb.discovery.bonjour.time.sleep"):
                            _snapshot, diagnostics = discover_snapshot_detailed(SMB_SERVICE, timeout=0, target_ip="10.0.1.77")

        source_mock.assert_called_once_with("10.0.1.77")
        fake_zeroconf_module.Zeroconf.assert_called_once_with(interfaces=["10.0.1.42"], ip_version=fake_ip_version.All)
        self.assertEqual(diagnostics.zeroconf_interfaces, "10.0.1.42")

    def test_discover_can_use_explicit_ipv4_family_and_interfaces(self) -> None:
        fake_zc = mock.Mock()
        fake_collector = mock.Mock()
        fake_collector.results.return_value = []
        fake_collector.service_instances.return_value = []
        fake_collector.service_events.return_value = []
        fake_collector.pending_count.return_value = 0
        fake_collector.service_added_count = 0
        fake_collector.service_updated_count = 0
        fake_collector.resolve_attempt_count = 0
        fake_collector.resolve_success_count = 0
        fake_collector.resolve_error_count = 0
        fake_ptr_observer = mock.Mock()
        fake_ptr_observer.observations.return_value = []
        fake_ptr_observer.error = None
        fake_ip_version = make_fake_ip_version()
        fake_zeroconf_module = mock.Mock(Zeroconf=mock.Mock(return_value=fake_zc), IPVersion=fake_ip_version)

        with mock.patch.dict(sys.modules, {"zeroconf": fake_zeroconf_module}):
            with mock.patch("timecapsulesmb.discovery.bonjour.Collector", return_value=fake_collector):
                with mock.patch("timecapsulesmb.discovery.bonjour.PtrRecordObserver", return_value=fake_ptr_observer):
                    with mock.patch("timecapsulesmb.discovery.bonjour.time.sleep"):
                        _snapshot, diagnostics = discover_snapshot_detailed(
                            SMB_SERVICE,
                            timeout=0,
                            family="ipv4",
                            interfaces=["10.0.1.42"],
                        )

        fake_zeroconf_module.Zeroconf.assert_called_once_with(interfaces=["10.0.1.42"], ip_version=fake_ip_version.V4Only)
        self.assertEqual(diagnostics.ip_version, "V4Only")
        self.assertEqual(diagnostics.zeroconf_interfaces, "10.0.1.42")

    def test_discover_can_use_explicit_ipv6_family_and_interfaces(self) -> None:
        fake_zc = mock.Mock()
        fake_collector = mock.Mock()
        fake_collector.results.return_value = []
        fake_collector.service_instances.return_value = []
        fake_collector.service_events.return_value = []
        fake_collector.pending_count.return_value = 0
        fake_collector.service_added_count = 0
        fake_collector.service_updated_count = 0
        fake_collector.resolve_attempt_count = 0
        fake_collector.resolve_success_count = 0
        fake_collector.resolve_error_count = 0
        fake_ptr_observer = mock.Mock()
        fake_ptr_observer.observations.return_value = []
        fake_ptr_observer.error = None
        fake_ip_version = make_fake_ip_version()
        fake_zeroconf_module = mock.Mock(Zeroconf=mock.Mock(return_value=fake_zc), IPVersion=fake_ip_version)

        with mock.patch.dict(sys.modules, {"zeroconf": fake_zeroconf_module}):
            with mock.patch("timecapsulesmb.discovery.bonjour.Collector", return_value=fake_collector):
                with mock.patch("timecapsulesmb.discovery.bonjour.PtrRecordObserver", return_value=fake_ptr_observer):
                    with mock.patch("timecapsulesmb.discovery.bonjour.time.sleep"):
                        _snapshot, diagnostics = discover_snapshot_detailed(
                            SMB_SERVICE,
                            timeout=0,
                            family="ipv6",
                            interfaces=["fd00::42"],
                        )

        fake_zeroconf_module.Zeroconf.assert_called_once_with(interfaces=["fd00::42"], ip_version=fake_ip_version.V6Only)
        self.assertEqual(diagnostics.ip_version, "V6Only")
        self.assertEqual(diagnostics.zeroconf_interfaces, "fd00::42")

    def test_discover_falls_back_to_all_interfaces_when_target_interface_is_unknown(self) -> None:
        fake_zc = mock.Mock()
        fake_collector = mock.Mock()
        fake_collector.results.return_value = []
        fake_collector.service_instances.return_value = []
        fake_collector.service_events.return_value = []
        fake_collector.pending_count.return_value = 0
        fake_collector.service_added_count = 0
        fake_collector.service_updated_count = 0
        fake_collector.resolve_attempt_count = 0
        fake_collector.resolve_success_count = 0
        fake_collector.resolve_error_count = 0
        fake_ptr_observer = mock.Mock()
        fake_ptr_observer.observations.return_value = []
        fake_ptr_observer.error = None
        fake_ip_version = make_fake_ip_version()
        fake_zeroconf_module = mock.Mock(Zeroconf=mock.Mock(return_value=fake_zc), IPVersion=fake_ip_version)

        with mock.patch.dict(sys.modules, {"zeroconf": fake_zeroconf_module}):
            with mock.patch("timecapsulesmb.discovery.bonjour._source_ipv4_for_target", return_value=None):
                with mock.patch("timecapsulesmb.discovery.bonjour.Collector", return_value=fake_collector):
                    with mock.patch("timecapsulesmb.discovery.bonjour.PtrRecordObserver", return_value=fake_ptr_observer):
                        with mock.patch("timecapsulesmb.discovery.bonjour.time.sleep"):
                            _snapshot, diagnostics = discover_snapshot_detailed(SMB_SERVICE, timeout=0, target_ip="10.0.1.77")

        fake_zeroconf_module.Zeroconf.assert_called_once_with(ip_version=fake_ip_version.All)
        self.assertEqual(diagnostics.zeroconf_interfaces, "All")

    def test_target_interfaces_include_ipv6_addresses_from_selected_adapter(self) -> None:
        class FakeAdapterIP:
            def __init__(self, ip: object) -> None:
                self.ip = ip

        class FakeAdapter:
            ips = [
                FakeAdapterIP("192.168.50.9"),
                FakeAdapterIP(("fe80::1234", 0, 4)),
                FakeAdapterIP(("fd00::1234", 0, 4)),
            ]

        fake_ifaddr = types.SimpleNamespace(get_adapters=mock.Mock(return_value=[FakeAdapter()]))

        with mock.patch.dict(sys.modules, {"ifaddr": fake_ifaddr}):
            with mock.patch("timecapsulesmb.discovery.bonjour._source_ipv4_for_target", return_value="192.168.50.9"):
                interfaces = _zeroconf_interfaces_for_target("192.168.50.77")

        self.assertEqual(interfaces, ["192.168.50.9", "fe80::1234", "fd00::1234"])

    def test_source_ipv4_for_target_uses_udp_route_selection(self) -> None:
        fake_sock = mock.Mock()
        fake_sock.getsockname.return_value = ("10.0.1.42", 5353)

        with mock.patch("timecapsulesmb.discovery.bonjour.socket.socket", return_value=fake_sock) as socket_mock:
            source_ip = _source_ipv4_for_target("10.0.1.77")

        socket_mock.assert_called_once()
        fake_sock.connect.assert_called_once_with(("10.0.1.77", 5353))
        fake_sock.close.assert_called_once()
        self.assertEqual(source_ip, "10.0.1.42")

    def test_source_ipv4_for_target_returns_none_when_route_selection_fails(self) -> None:
        fake_sock = mock.Mock()
        fake_sock.connect.side_effect = OSError("no route")

        with mock.patch("timecapsulesmb.discovery.bonjour.socket.socket", return_value=fake_sock):
            self.assertIsNone(_source_ipv4_for_target("10.0.1.77"))

        fake_sock.close.assert_called_once()

    def test_source_ipv6_for_target_uses_udp_route_selection(self) -> None:
        fake_sock = mock.Mock()
        fake_sock.getsockname.return_value = ("fd00::42", 5353, 0, 0)

        with mock.patch("timecapsulesmb.discovery.bonjour.socket.socket", return_value=fake_sock) as socket_mock:
            source_ip = _source_ipv6_for_target("fd00::77")

        socket_mock.assert_called_once_with(socket.AF_INET6, socket.SOCK_DGRAM)
        fake_sock.connect.assert_called_once_with(("fd00::77", 5353))
        fake_sock.close.assert_called_once()
        self.assertEqual(source_ip, "fd00::42")

    def test_open_zeroconf_reports_missing_dependency_with_bootstrap_guidance(self) -> None:
        real_import = __import__

        def missing_zeroconf_import(name: str, *args: object, **kwargs: object) -> object:
            if name == "zeroconf":
                raise ModuleNotFoundError("No module named 'zeroconf'")
            return real_import(name, *args, **kwargs)

        with mock.patch("builtins.__import__", side_effect=missing_zeroconf_import):
            with self.assertRaises(RuntimeError) as exc:
                _open_zeroconf()

        self.assertIn("Failed to load zeroconf. Install the Python package zeroconf.", str(exc.exception))
        self.assertIn("ModuleNotFoundError: No module named 'zeroconf'", str(exc.exception))

    def test_discover_retries_pending_resolution_during_browse_window(self) -> None:
        fake_zc = mock.Mock()
        fake_collector = mock.Mock()
        fake_collector.results.return_value = []
        fake_collector.service_instances.return_value = []
        fake_ip_version = make_fake_ip_version()
        fake_zeroconf_module = mock.Mock(Zeroconf=mock.Mock(return_value=fake_zc), IPVersion=fake_ip_version)

        with mock.patch.dict(sys.modules, {"zeroconf": fake_zeroconf_module}):
            with mock.patch("timecapsulesmb.discovery.bonjour.Collector", return_value=fake_collector):
                with mock.patch("timecapsulesmb.discovery.bonjour.time.monotonic", side_effect=[0.0, 0.0, 0.6, 0.6]):
                    with mock.patch("timecapsulesmb.discovery.bonjour.time.sleep") as fake_sleep:
                        discover_resolved_records(timeout=0.5)

        fake_sleep.assert_called_once_with(0.5)
        fake_collector.resolve_pending.assert_has_calls([
            mock.call(timeout_ms=500),
            mock.call(timeout_ms=3000),
        ])
        self.assertEqual(fake_collector.resolve_pending.call_count, 2)

    def test_discover_snapshot_detailed_returns_bounded_discovery_counters(self) -> None:
        fake_zc = mock.Mock()
        instance = BonjourServiceInstance("_smb._tcp.local.", "Home", "Home._smb._tcp.local.")
        record = BonjourResolvedService("Home", "home.local", "_smb._tcp.local.", port=445, ipv4=["10.0.1.1"])
        event = BonjourServiceEvent("_smb._tcp.local.", "Added", "Home", "Home._smb._tcp.local.", 0.25)
        ptr_record = BonjourPtrRecordObservation(
            "_smb._tcp.local.",
            "Home._smb._tcp.local.",
            "Home",
            120,
            False,
            False,
            0.25,
        )
        fake_collector = mock.Mock()
        fake_collector.results.return_value = [record]
        fake_collector.service_instances.return_value = [instance]
        fake_collector.service_events.return_value = [event]
        fake_collector.pending_count.return_value = 1
        fake_collector.service_added_count = 2
        fake_collector.service_updated_count = 1
        fake_collector.resolve_attempt_count = 3
        fake_collector.resolve_success_count = 1
        fake_collector.resolve_error_count = 1
        fake_ptr_observer = mock.Mock()
        fake_ptr_observer.observations.return_value = [ptr_record]
        fake_ptr_observer.error = None
        ptr_observer_calls = mock.Mock()
        ptr_observer_calls.attach_mock(fake_ptr_observer.stop, "stop")
        ptr_observer_calls.attach_mock(fake_ptr_observer.observations, "observations")
        fake_ip_version = make_fake_ip_version()
        fake_zeroconf_module = mock.Mock(Zeroconf=mock.Mock(return_value=fake_zc), IPVersion=fake_ip_version)

        with mock.patch.dict(sys.modules, {"zeroconf": fake_zeroconf_module}):
            with mock.patch("timecapsulesmb.discovery.bonjour.Collector", return_value=fake_collector):
                with mock.patch("timecapsulesmb.discovery.bonjour.PtrRecordObserver", return_value=fake_ptr_observer):
                    with mock.patch(
                        "timecapsulesmb.discovery.bonjour.time.monotonic",
                        side_effect=[10.0, 10.0, 16.125],
                    ):
                        snapshot, diagnostics = discover_snapshot_detailed(SMB_SERVICE, timeout=0)

        self.assertEqual(snapshot.instances, [instance])
        self.assertEqual(snapshot.resolved, [record])
        fake_ptr_observer.start.assert_called_once_with(fake_zc)
        ptr_observer_calls.assert_has_calls([
            mock.call.stop(fake_zc),
            mock.call.observations(),
        ])
        self.assertEqual(diagnostics.service, "_smb")
        self.assertEqual(diagnostics.service_types, ["_smb._tcp.local."])
        self.assertEqual(diagnostics.ip_version, "All")
        self.assertEqual(diagnostics.elapsed_sec, 6.125)
        self.assertEqual(diagnostics.instance_count, 1)
        self.assertEqual(diagnostics.resolved_count, 1)
        self.assertEqual(diagnostics.pending_count, 1)
        self.assertEqual(diagnostics.service_added_count, 2)
        self.assertEqual(diagnostics.service_updated_count, 1)
        self.assertEqual(diagnostics.resolve_attempt_count, 3)
        self.assertEqual(diagnostics.resolve_success_count, 1)
        self.assertEqual(diagnostics.resolve_error_count, 1)
        self.assertEqual(diagnostics.service_events, [event])
        self.assertEqual(diagnostics.ptr_records, [ptr_record])

    def test_discover_includes_unresolved_browse_instances(self) -> None:
        fake_zc = mock.Mock()
        fake_collector = mock.Mock()
        instance = BonjourServiceInstance("_smb._tcp.local.", "Home", "Home._smb._tcp.local.")
        fake_collector.results.return_value = []
        fake_collector.service_instances.return_value = [instance]
        fake_ip_version = make_fake_ip_version()
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

    def test_resolve_service_instance_returns_resolved_record(self) -> None:
        class FakeQuestionType:
            QM = object()

        class FakeInfo:
            name = "Home._smb._tcp.local."
            server = "home.local."
            port = 445
            properties = {b"path": b"/"}
            addresses = [bytes([10, 0, 1, 1])]

        fake_zc = mock.Mock()
        fake_zc.get_service_info.return_value = FakeInfo()
        fake_ip_version = make_fake_ip_version()
        fake_zeroconf_module = mock.Mock(Zeroconf=mock.Mock(return_value=fake_zc), IPVersion=fake_ip_version, DNSQuestionType=FakeQuestionType)
        instance = BonjourServiceInstance("_smb._tcp.local.", "Home", "Home._smb._tcp.local.")

        with mock.patch.dict(sys.modules, {"zeroconf": fake_zeroconf_module}):
            record = resolve_service_instance(instance, timeout_ms=750)

        fake_zc.get_service_info.assert_called_once_with(
            "_smb._tcp.local.",
            "Home._smb._tcp.local.",
            750,
            question_type=FakeQuestionType.QM,
        )
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.name, "Home")
        self.assertEqual(record.hostname, "home.local")
        self.assertEqual(record.port, 445)
        self.assertEqual(record.ipv4, ["10.0.1.1"])

    def test_resolve_service_instance_uses_selected_interface_for_target(self) -> None:
        class FakeQuestionType:
            QM = object()

        fake_zc = mock.Mock()
        fake_zc.get_service_info.return_value = None
        fake_ip_version = make_fake_ip_version()
        fake_zeroconf_module = mock.Mock(Zeroconf=mock.Mock(return_value=fake_zc), IPVersion=fake_ip_version, DNSQuestionType=FakeQuestionType)
        instance = BonjourServiceInstance("_smb._tcp.local.", "Home", "Home._smb._tcp.local.")

        with mock.patch.dict(sys.modules, {"zeroconf": fake_zeroconf_module}):
            with mock.patch("timecapsulesmb.discovery.bonjour._source_ipv4_for_target", return_value="10.0.1.42"):
                record = resolve_service_instance(instance, timeout_ms=750, target_ip="10.0.1.77")

        self.assertIsNone(record)
        fake_zeroconf_module.Zeroconf.assert_called_once_with(interfaces=["10.0.1.42"], ip_version=fake_ip_version.All)
        fake_zc.get_service_info.assert_called_once_with(
            "_smb._tcp.local.",
            "Home._smb._tcp.local.",
            750,
            question_type=FakeQuestionType.QM,
        )

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

        class FakeQuestionType:
            QM = object()

        class FakeInfo:
            name = "Home._smb._tcp.local."
            server = "home.local."
            properties: dict[bytes, bytes] = {}
            addresses = [bytes([10, 0, 1, 1])]

        fake_zeroconf_module = mock.Mock(ServiceStateChange=FakeStateChange, DNSQuestionType=FakeQuestionType)
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

        fake_zc.get_service_info.assert_called_once_with(
            "_smb._tcp.local.",
            "Home._smb._tcp.local.",
            3000,
            question_type=FakeQuestionType.QM,
        )
        records = collector.results()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].name, "Home")
        self.assertEqual(records[0].hostname, "home.local")
        self.assertEqual(records[0].ipv4, ["10.0.1.1"])

    def test_collector_start_uses_qm_question_type(self) -> None:
        class FakeQuestionType:
            QM = object()

        fake_browser = mock.Mock()
        fake_zeroconf_module = mock.Mock(ServiceBrowser=fake_browser, DNSQuestionType=FakeQuestionType)
        fake_zc = mock.Mock()
        collector = Collector(fake_zc, ["_smb._tcp.local."])

        with mock.patch.dict(sys.modules, {"zeroconf": fake_zeroconf_module}):
            collector.start()

        fake_browser.assert_called_once_with(
            fake_zc,
            "_smb._tcp.local.",
            handlers=[collector._on_service_state_change],
            question_type=FakeQuestionType.QM,
        )

    def test_collector_records_browse_event_diagnostics(self) -> None:
        class FakeChange:
            def __init__(self, name: str) -> None:
                self.name = name

        class FakeStateChange:
            Added = FakeChange("Added")
            Updated = FakeChange("Updated")
            Removed = FakeChange("Removed")

        fake_zeroconf_module = mock.Mock(ServiceStateChange=FakeStateChange)
        fake_zc = mock.Mock()
        collector = Collector(fake_zc, ["_smb._tcp.local."], start_time=10.0)

        with mock.patch.dict(sys.modules, {"zeroconf": fake_zeroconf_module}):
            with mock.patch("timecapsulesmb.discovery.bonjour.time.monotonic", side_effect=[10.25, 10.5]):
                collector._on_service_state_change(
                    zeroconf=fake_zc,
                    service_type="_smb._tcp.local.",
                    name="Home._smb._tcp.local.",
                    state_change=FakeStateChange.Added,
                )
                collector._on_service_state_change(
                    zeroconf=fake_zc,
                    service_type="_smb._tcp.local.",
                    name="Home._smb._tcp.local.",
                    state_change=FakeStateChange.Removed,
                )

        events = collector.service_events()
        self.assertEqual([event.state for event in events], ["Added", "Removed"])
        self.assertEqual([event.elapsed_sec for event in events], [0.25, 0.5])
        self.assertEqual(events[0].name, "Home")
        self.assertEqual(events[0].fullname, "Home._smb._tcp.local.")
        self.assertEqual(collector.service_instances()[0].name, "Home")

    def test_collector_keeps_failed_pending_records_for_later_retry(self) -> None:
        class FakeQuestionType:
            QM = object()

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

        with mock.patch.dict(sys.modules, {"zeroconf": mock.Mock(DNSQuestionType=FakeQuestionType)}):
            collector.resolve_pending(timeout_ms=500)

        self.assertEqual(fake_zc.get_service_info.call_count, 2)
        fake_zc.get_service_info.assert_has_calls([
            mock.call("_smb._tcp.local.", "Home._smb._tcp.local.", 500, question_type=FakeQuestionType.QM),
            mock.call("_smb._tcp.local.", "Kitchen._smb._tcp.local.", 500, question_type=FakeQuestionType.QM),
        ])
        records = collector.results()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].name, "Kitchen")
        self.assertEqual(records[0].ipv4, ["10.0.1.99"])
        self.assertEqual(collector.pending, {("_smb._tcp.local.", "Home._smb._tcp.local.")})
        self.assertEqual(collector.resolve_attempt_count, 2)
        self.assertEqual(collector.resolve_success_count, 1)
        self.assertEqual(collector.resolve_error_count, 1)

    def test_collector_keeps_unresolved_pending_record_for_later_retry(self) -> None:
        class FakeQuestionType:
            QM = object()

        class FakeInfo:
            name = "Home._smb._tcp.local."
            server = "home.local."
            properties: dict[bytes, bytes] = {}
            addresses = [bytes([10, 0, 1, 1])]

        fake_zc = mock.Mock()
        fake_zc.get_service_info.return_value = None
        collector = Collector(fake_zc, ["_smb._tcp.local."])
        collector.pending = {("_smb._tcp.local.", "Home._smb._tcp.local.")}

        with mock.patch.dict(sys.modules, {"zeroconf": mock.Mock(DNSQuestionType=FakeQuestionType)}):
            collector.resolve_pending(timeout_ms=500)

        self.assertEqual(collector.results(), [])
        self.assertEqual(collector.pending, {("_smb._tcp.local.", "Home._smb._tcp.local.")})
        self.assertEqual(collector.resolve_attempt_count, 1)
        self.assertEqual(collector.resolve_success_count, 0)
        self.assertEqual(collector.resolve_error_count, 0)

        fake_zc.get_service_info.return_value = FakeInfo()
        with mock.patch.dict(sys.modules, {"zeroconf": mock.Mock(DNSQuestionType=FakeQuestionType)}):
            collector.resolve_pending(timeout_ms=500)

        records = collector.results()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].name, "Home")
        self.assertEqual(records[0].ipv4, ["10.0.1.1"])
        self.assertEqual(collector.pending, set())
        self.assertEqual(collector.resolve_attempt_count, 2)
        self.assertEqual(collector.resolve_success_count, 1)
        self.assertEqual(collector.resolve_error_count, 0)

    def test_ptr_record_observer_records_matching_ptr_updates(self) -> None:
        class FakePtrRecord:
            name = "_smb._tcp.local."
            type = DNS_RECORD_TYPE_PTR
            alias = "Home._smb._tcp.local."
            ttl = 120

            def is_expired(self, now: float) -> bool:
                return False

        class FakeIgnoredRecord:
            name = "_airport._tcp.local."
            type = DNS_RECORD_TYPE_PTR
            alias = "Home._airport._tcp.local."
            ttl = 120

        class FakeUpdate:
            new = FakePtrRecord()
            old = object()

        observer = PtrRecordObserver(["_smb._tcp.local."], start_time=20.0)

        with mock.patch("timecapsulesmb.discovery.bonjour.time.monotonic", return_value=20.75):
            observer.async_update_records(
                None,
                20.75,
                [FakeUpdate(), FakeIgnoredRecord()],
            )

        records = observer.observations()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].service_type, "_smb._tcp.local.")
        self.assertEqual(records[0].alias, "Home._smb._tcp.local.")
        self.assertEqual(records[0].alias_name, "Home")
        self.assertEqual(records[0].ttl, 120)
        self.assertFalse(records[0].expired)
        self.assertTrue(records[0].old_record_present)
        self.assertEqual(records[0].elapsed_sec, 0.75)

    def test_ptr_record_observer_captures_setup_error_as_diagnostic(self) -> None:
        class FakeDNSQuestion:
            def __init__(self, name: str, record_type: int, record_class: int) -> None:
                self.name = name
                self.record_type = record_type
                self.record_class = record_class

        class FakeRecordUpdateListener:
            pass

        fake_zeroconf_module = types.ModuleType("zeroconf")
        fake_zeroconf_module.__path__ = []
        fake_zeroconf_module.DNSQuestion = FakeDNSQuestion
        fake_zeroconf_module.RecordUpdateListener = FakeRecordUpdateListener
        fake_zeroconf_const_module = types.ModuleType("zeroconf.const")
        fake_zeroconf_const_module._CLASS_IN = 1
        fake_zeroconf_const_module._TYPE_PTR = DNS_RECORD_TYPE_PTR
        fake_zc = mock.Mock()
        fake_zc.add_listener.side_effect = RuntimeError("listener unavailable")
        observer = PtrRecordObserver(["_smb._tcp.local."], start_time=0.0)

        with mock.patch.dict(
            sys.modules,
            {
                "zeroconf": fake_zeroconf_module,
                "zeroconf.const": fake_zeroconf_const_module,
            },
        ):
            observer.start(fake_zc)
        observer.stop(fake_zc)

        self.assertEqual(observer.error, "RuntimeError: listener unavailable")
        fake_zc.remove_listener.assert_not_called()

    def test_record_has_service_matches_requested_service(self) -> None:
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
        self.assertFalse(record_has_service(airport, SMB_SERVICE))
        self.assertTrue(record_has_service(samba, SMB_SERVICE))
        self.assertTrue(record_has_service(samba, "_smb._tcp.local"))

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
        fake_ip_version = make_fake_ip_version()
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

    def test_record_has_service_accepts_service_prefix_for_airport(self) -> None:
        airport = Discovered(name="AirPort Time Capsule", hostname="airport.local", service_type="_airport._tcp.local")
        self.assertTrue(record_has_service(airport, AIRPORT_SERVICE))

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
        with mock.patch("timecapsulesmb.cli.discover.discover_snapshot", return_value=snapshot):
            with redirect_stdout(output):
                rc = run_cli([])
        text = output.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("Browse Results", text)
        self.assertIn("Resolved Records", text)
        self.assertIn("Home._smb._tcp.local.", text)
        self.assertIn("home.local", text)

    def test_run_cli_reports_bonjour_dependency_errors_as_system_exit(self) -> None:
        with mock.patch("timecapsulesmb.cli.discover.discover_snapshot", side_effect=RuntimeError("zeroconf missing")):
            with self.assertRaises(SystemExit) as cm:
                run_cli([])

        self.assertEqual(str(cm.exception), "zeroconf missing")


if __name__ == "__main__":
    unittest.main()
