from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from timecapsulesmb.checks.bonjour import BonjourServiceTarget  # noqa: E402
from timecapsulesmb.checks.smb_config import (  # noqa: E402
    SmbShare,
    parse_active_netbios_name,
    parse_active_payload_dir,
    parse_active_share_names,
    parse_active_share_paths,
    parse_active_shares,
    parse_xattr_tdb_paths,
)
from timecapsulesmb.checks.smb_targets import doctor_smb_servers  # noqa: E402
from timecapsulesmb.core.config import AppConfig  # noqa: E402
from timecapsulesmb.device.probe import RuntimeNamingIdentityProbeResult  # noqa: E402
from timecapsulesmb.checks import doctor_steps
from timecapsulesmb.checks.bonjour import BonjourExpectedIdentity
from timecapsulesmb.checks.doctor_state import DoctorSink
from timecapsulesmb.checks.models import CheckResult
from timecapsulesmb.checks.network import RouteSelection
from timecapsulesmb.discovery.bonjour import BonjourDiscoverySnapshot, BonjourResolvedService, BonjourServiceInstance


class DoctorHelperTests(unittest.TestCase):
    def scope_snapshot(self, *, hostname="home.local", addresses=("fe80::40%18",), port=445):
        record = BonjourResolvedService("Home", hostname, "_smb._tcp.local.", port=port, ipv6=list(addresses), fullname="Home._smb._tcp.local.")
        instance = BonjourServiceInstance(record.service_type, record.name, record.fullname)
        return BonjourDiscoverySnapshot(instances=[instance], resolved=[record])

    def test_direct_smb_scope_alternatives_pin_authentication_to_the_working_scope(self):
        for successful in (True, False):
            with self.subTest(successful=successful):
                sink = DoctorSink(None, {})
                errors = {"fe80::40%17": "timeout", "fe80::40%18": None if successful else "no peer"}
                with (
                    mock.patch.object(doctor_steps, "select_route_to_address", return_value=RouteSelection("available")),
                    mock.patch.object(doctor_steps, "scoped_tcp_connect_errors", return_value=errors) as probe,
                    mock.patch.object(doctor_steps, "_add_remote_service_socket_debug") as debug,
                ):
                    state = doctor_steps._doctor_check_direct_smb_port(
                        SimpleNamespace(proxied_ssh=False, host="10.0.1.1"),
                        mock.Mock(),
                        ("fe80::40%17", "fe80::40%18"),
                        sink,
                    )
                probe.assert_called_once_with(["fe80::40%17", "fe80::40%18"], 445)
                self.assertEqual([result.status for result in sink.results], ["PASS" if successful else "FAIL"])
                self.assertEqual(state.reachable_addresses, ("fe80::40%18",) if successful else ())
                self.assertEqual(debug.called, not successful)
                if successful:
                    config = AppConfig(values={"TC_HOST": "10.0.1.1"})
                    targets = doctor_steps._doctor_smb_client_targets(config, None, None, state.reachable_addresses)
                    groups = doctor_steps._authenticated_smb_target_groups(targets, state.reachable_addresses)
                    self.assertEqual([target.ip_address for _family, group in groups for target in group], ["fe80::40%18"])

    def test_unscoped_link_local_is_bounded_across_client_interfaces(self):
        sink = DoctorSink(None, {})
        errors = {"fe80::40%en0": "timeout", "fe80::40%en1": None}
        with (
            mock.patch.object(doctor_steps, "local_interface_addresses", return_value=("fe80::1%en0", "fe80::1%en1")),
            mock.patch("timecapsulesmb.core.net.socket.if_nametoindex", side_effect=lambda name: {"en0": 17, "en1": 18}[name]),
            mock.patch.object(doctor_steps, "select_route_to_address", return_value=RouteSelection("available")),
            mock.patch.object(doctor_steps, "scoped_tcp_connect_errors", return_value=errors) as probe,
        ):
            state = doctor_steps._doctor_check_direct_smb_port(
                SimpleNamespace(proxied_ssh=False, host="10.0.1.1"), mock.Mock(), ("fe80::40",), sink,
            )
        probe.assert_called_once_with(["fe80::40%en0", "fe80::40%en1"], 445)
        self.assertEqual(state.observed_addresses, ("fe80::40",))
        self.assertEqual(state.reachable_addresses, ("fe80::40%en1",))

    def test_unscoped_link_local_without_client_scope_fails_as_untested(self):
        sink = DoctorSink(None, {})
        with mock.patch.object(doctor_steps, "local_interface_addresses", return_value=()):
            state = doctor_steps._doctor_check_direct_smb_port(
                SimpleNamespace(proxied_ssh=False, host="10.0.1.1"), mock.Mock(), ("fe80::40",), sink,
            )
        self.assertEqual(state.testable_addresses, ())
        self.assertEqual(sink.results[-1].status, "FAIL")
        self.assertIn("no usable client route or IPv6 scope", sink.results[-1].message)

    def test_ipv4_ipv6_and_dual_stack_tcp_outcome_matrix(self):
        cases = {
            "ipv4-pass": (("10.0.0.2",), {}, {"10.0.0.2": None}, False, ["PASS"]),
            "ipv4-fail": (("10.0.0.2",), {}, {"10.0.0.2": "timed out"}, True, ["FAIL"]),
            "ipv6-pass": (("fd00::2",), {}, {"fd00::2": None}, False, ["PASS"]),
            "ipv6-fail": (("fd00::2",), {}, {"fd00::2": "timed out"}, True, ["FAIL"]),
            "dual-pass": (("10.0.0.2", "fd00::2"), {}, {"10.0.0.2": None, "fd00::2": None}, False, ["PASS", "PASS"]),
            "dual-fail": (("10.0.0.2", "fd00::2"), {}, {"10.0.0.2": "timed out", "fd00::2": "timed out"}, True, ["FAIL", "FAIL"]),
            "dual-degraded": (("10.0.0.2", "fd00::2"), {}, {"10.0.0.2": None, "fd00::2": "timed out"}, False, ["PASS", "WARN"]),
            "dual-unrouted-v4": (("10.0.0.2", "fd00::2"), {"10.0.0.2": "no route"}, {"fd00::2": None}, False, ["INFO", "PASS"]),
        }
        for name, (addresses, unavailable, tcp, fatal, statuses) in cases.items():
            with self.subTest(name=name):
                sink = DoctorSink(None, {})

                def route(address):
                    error = unavailable.get(address)
                    return RouteSelection("unavailable", error=error) if error else RouteSelection("available")

                def port(address):
                    error = tcp[address]
                    return CheckResult(
                        "PASS" if error is None else "WARN",
                        f"SMB {'reachable' if error is None else 'not reachable'} at {address}:445",
                        {} if error is None else {"error": error},
                    )

                with (
                    mock.patch.object(doctor_steps, "select_route_to_address", side_effect=route),
                    mock.patch.object(doctor_steps, "check_smb_port", side_effect=port),
                    mock.patch.object(doctor_steps, "_add_remote_service_socket_debug"),
                ):
                    doctor_steps._doctor_check_direct_smb_port(
                        SimpleNamespace(proxied_ssh=False, host="10.0.0.2"), mock.Mock(), addresses, sink,
                    )
                self.assertEqual([result.status for result in sink.results], statuses)
                self.assertEqual(sink.fatal(), fatal)

    def test_bonjour_merge_preserves_same_link_local_address_on_two_interfaces(self):
        results = []
        empty = (None, CheckResult("FAIL", "no IPv4 _smb record"), None)
        with (
            mock.patch.object(doctor_steps, "build_bonjour_expected_identity", return_value=BonjourExpectedIdentity("Home", "home", None)),
            mock.patch.object(
                doctor_steps,
                "discover_smb_services_detailed",
                side_effect=[empty, (self.scope_snapshot(addresses=("fe80::40%17", "fe80::40%18")), None, None)],
            ) as browse,
            mock.patch("timecapsulesmb.checks.bonjour.resolve_host_ips", return_value=()),
            mock.patch.object(doctor_steps, "native_dns_sd_available", return_value=False),
        ):
            result = doctor_steps._add_bonjour_results(
                AppConfig(values={"TC_HOST": "10.0.1.1"}), None,
                proxied_ssh=False, skip_bonjour=False, add_result=results.append,
            )
        self.assertEqual(browse.call_count, 2)
        self.assertFalse(any(item.status == "FAIL" for item in results), results)
        self.assertEqual(result.instance, "Home")
        self.assertEqual(result.addresses, ("fe80::40%17", "fe80::40%18"))

    def test_parse_xattr_tdb_paths_ignores_comments_and_preserves_multiple_paths(self) -> None:
        smb_conf = """
        [global]
          # xattr_tdb:file = /mnt/Memory/bad.tdb
          ; xattr_tdb:file = /mnt/Memory/also-bad.tdb
          xattr_tdb:file = /Volumes/dk2/.samba4/private/xattr.tdb
          XATTR_TDB:FILE=/Volumes/dk3/.samba4/private/xattr.tdb
        """

        self.assertEqual(
            parse_xattr_tdb_paths(smb_conf),
            [
                "/Volumes/dk2/.samba4/private/xattr.tdb",
                "/Volumes/dk3/.samba4/private/xattr.tdb",
            ],
        )

    def test_parse_active_netbios_name_returns_first_non_comment_value_case_insensitively(self) -> None:
        smb_conf = """
        [global]
          ; netbios name = Ignored
          NETBIOS NAME = KitchenCapsule
          netbios name = Other
        """

        self.assertEqual(parse_active_netbios_name(smb_conf), "KitchenCapsule")

    def test_parse_active_share_names_skips_global_and_empty_sections(self) -> None:
        smb_conf = """
        [global]
        [Data]
        [ Time Machine ]
        []
        [GLOBAL]
        """

        self.assertEqual(parse_active_share_names(smb_conf), ["Data", "Time Machine"])

    def test_parse_active_shares_tracks_section_paths(self) -> None:
        smb_conf = """
        [global]
          path = /ignored/global
        [Data]
          path = /Volumes/dk2/ShareRoot
        [ Time Machine ]
          comment = backups
          path = /Volumes/dk3
        [Empty]
        """

        self.assertEqual(
            parse_active_shares(smb_conf),
            [
                SmbShare("Data", "/Volumes/dk2/ShareRoot"),
                SmbShare("Time Machine", "/Volumes/dk3"),
                SmbShare("Empty", None),
            ],
        )
        self.assertEqual(parse_active_share_paths(smb_conf), ["/Volumes/dk2/ShareRoot", "/Volumes/dk3"])

    def test_parse_active_payload_dir_derives_parent_from_global_log_file(self) -> None:
        smb_conf = """
        [global]
          log file = /Volumes/dk2/.samba4/logs/log.smbd
        [Data]
          path = /Volumes/dk2/ShareRoot
        """

        self.assertEqual(parse_active_payload_dir(smb_conf), "/Volumes/dk2/.samba4")

    def runtime_identity(self, host_label: str = "timecapsulesamba4") -> RuntimeNamingIdentityProbeResult:
        return RuntimeNamingIdentityProbeResult(
            system_name="Time Capsule",
            hostname=host_label,
            mdns_instance_name="Time Capsule",
            mdns_host_label=host_label,
            netbios_name="TimeCapsule",
            detail="ok",
        )

    def test_doctor_smb_servers_uses_probed_host_label(self) -> None:
        base_values = {"TC_HOST": "root@10.0.1.99"}
        self.assertEqual(
            doctor_smb_servers(AppConfig.from_values(base_values), None, self.runtime_identity()),
            ["timecapsulesamba4.local", "10.0.1.99"],
        )
        self.assertEqual(
            doctor_smb_servers(AppConfig.from_values(base_values), None),
            ["10.0.1.99"],
        )

    def test_doctor_smb_servers_orders_probed_bonjour_then_ssh_host_and_deduplicates(self) -> None:
        values = {
            "TC_HOST": "root@10.0.1.99",
        }
        target = BonjourServiceTarget("Time Capsule Samba 4", "timecapsulesamba4.local", 445)

        self.assertEqual(
            doctor_smb_servers(AppConfig.from_values(values), target, self.runtime_identity()),
            ["timecapsulesamba4.local", "10.0.1.99"],
        )


if __name__ == "__main__":
    unittest.main()
