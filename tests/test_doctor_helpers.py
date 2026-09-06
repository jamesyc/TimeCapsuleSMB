from __future__ import annotations

import sys
import unittest
import time
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
from timecapsulesmb.checks.doctor_state import DoctorSink, NetworkPlanState
from timecapsulesmb.checks.network_plan import build_network_check_plan, RouteSelection
from timecapsulesmb.discovery.bonjour import BonjourDiscoverySnapshot, BonjourResolvedService, BonjourServiceInstance


class DoctorHelperTests(unittest.TestCase):
    def scope_plan(self):
        return build_network_check_plan(
            smb_bind_interfaces="fe80:8::40/64", mdns_families=("ipv6",), nbns_families=(),
            local_addresses=("fe80::1%17", "fe80::1%18"),
            route_selector=lambda address: RouteSelection("available", source=f"fe80::1%{address.split('%')[1]}"),
        )

    def scope_snapshot(self, *, hostname="home.local", address="fe80::40%18", port=445):
        record = BonjourResolvedService("Home", hostname, "_smb._tcp.local.", port=port, ipv6=[address], fullname="Home._smb._tcp.local.")
        instance = BonjourServiceInstance(record.service_type, record.name, record.fullname)
        return BonjourDiscoverySnapshot(instances=[instance], resolved=[record])

    def test_direct_smb_scope_alternatives_emit_one_result_and_pin_authentication(self):
        plan = self.scope_plan()
        for successful in (True, False):
            with self.subTest(successful=successful):
                sink = DoctorSink(None, {})
                errors = {"fe80::40%17": "timeout", "fe80::40%18": None if successful else "no peer"}
                with (
                    mock.patch.object(doctor_steps, "scoped_tcp_connect_errors", return_value=errors) as probe,
                    mock.patch.object(doctor_steps, "_add_remote_service_socket_debug") as debug,
                ):
                    state = doctor_steps._doctor_check_direct_smb_port(SimpleNamespace(proxied_ssh=False), mock.Mock(), NetworkPlanState(plan), sink)
                probe.assert_called_once_with(["fe80::40%17", "fe80::40%18"], 445)
                self.assertEqual([result.status for result in sink.results], ["PASS" if successful else "WARN"])
                self.assertEqual(state.reachable_addresses, ("fe80::40%18",) if successful else ())
                self.assertEqual(debug.called, not successful)
                if successful:
                    config = AppConfig(values={"TC_HOST": "10.0.1.1"})
                    targets = doctor_steps._doctor_smb_client_targets(config, None, None, plan)
                    groups = doctor_steps._authenticated_smb_target_groups(targets, state.reachable_addresses)
                    self.assertEqual([target.ip_address for _family, group in groups for target in group], ["fe80::40%18"])

    def test_bonjour_browses_scope_candidates_once_and_reports_only_valid_scope(self):
        results = []
        with (
            mock.patch.object(doctor_steps, "build_bonjour_expected_identity", return_value=BonjourExpectedIdentity("Home", "home", None)),
            mock.patch.object(doctor_steps, "discover_smb_services_detailed", return_value=(self.scope_snapshot(), None, None)) as browse,
            mock.patch("timecapsulesmb.checks.bonjour.resolve_host_ips", return_value=()),
            mock.patch.object(doctor_steps, "_evaluate_native_bonjour_attempt") as native,
        ):
            result = doctor_steps._add_bonjour_results(AppConfig(values={"TC_HOST": "10.0.1.1"}), None, proxied_ssh=False, skip_bonjour=False, network_plan=self.scope_plan(), add_result=results.append)
        browse.assert_called_once()
        self.assertEqual(browse.call_args.kwargs["interfaces"], ["fe80::1%17", "fe80::1%18"])
        self.assertIn("deadline", browse.call_args.kwargs)
        self.assertFalse(any(item.status == "FAIL" for item in results), results)
        self.assertTrue(any("fe80::40%18" in item.message for item in results))
        self.assertEqual(result.instance, "Home")
        native.assert_not_called()
        self.assertEqual(result.zeroconf_debug["selected_address"], "fe80::40%18")

    def test_scoped_bonjour_identity_mismatches_cannot_enable_native_fallback(self):
        for changes in ({"hostname": "wrong.local"}, {"port": 999}, {"address": "fe80::99%18"}, {"address": "fe80::40%19"}):
            with self.subTest(changes=changes), mock.patch("timecapsulesmb.checks.bonjour.resolve_host_ips", return_value=()):
                outcome, _address, _details = doctor_steps._evaluate_scoped_bonjour_candidates(
                    self.scope_snapshot(**changes), None, BonjourExpectedIdentity("Home", "home", None),
                    ["fe80::40%17", "fe80::40%18"], ["fe80::1%17", "fe80::1%18"], [], time.monotonic() + 9,
                )
                self.assertTrue(outcome.identity_mismatch)
                self.assertTrue(any(result.status == "FAIL" for result in outcome.results))
                with mock.patch.object(doctor_steps, "native_dns_sd_available", return_value=True):
                    self.assertFalse(doctor_steps._should_try_native_bonjour_fallback(outcome))

    def test_targeted_resolve_is_shared_across_scopes_and_obeys_remaining_budget(self):
        snapshot = self.scope_snapshot()
        record = snapshot.resolved.pop()
        with (
            mock.patch.object(doctor_steps, "resolve_smb_instance", return_value=(record, None)) as resolve,
            mock.patch.object(doctor_steps.time, "monotonic", return_value=100.0),
            mock.patch("timecapsulesmb.checks.bonjour.resolve_host_ips", return_value=()),
        ):
            outcome, address, _details = doctor_steps._evaluate_scoped_bonjour_candidates(
                snapshot, None, BonjourExpectedIdentity("Home", "home", None),
                ["fe80::40%17", "fe80::40%18"], ["fe80::1%17", "fe80::1%18"], [], 100.75,
            )
        resolve.assert_called_once()
        self.assertEqual(resolve.call_args.kwargs["timeout_ms"], 750)
        self.assertEqual(address, "fe80::40%18")
        self.assertFalse(any(result.status == "FAIL" for result in outcome.results))

        with mock.patch.object(doctor_steps, "resolve_smb_instance") as resolve:
            outcome, _address, _details = doctor_steps._evaluate_scoped_bonjour_candidates(
                snapshot, None, BonjourExpectedIdentity("Home", "home", None),
                ["fe80::40%17", "fe80::40%18"], [], [], 0,
            )
        resolve.assert_not_called()
        self.assertTrue(any("budget exhausted" in result.message for result in outcome.results))

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
