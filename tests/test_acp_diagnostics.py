from __future__ import annotations

import ipaddress
import unittest
from unittest import mock

from timecapsulesmb.checks.network import LocalInterfaceNetwork
from timecapsulesmb.core.net import RouteSelection
from timecapsulesmb.discovery.bonjour import BonjourResolvedService
from timecapsulesmb.integrations.acp import ACP_PORT
from timecapsulesmb.services import acp_diagnostics


def network(interface: str, address: str, prefix: str) -> LocalInterfaceNetwork:
    return LocalInterfaceNetwork(interface, address, ipaddress.ip_network(prefix))


HOME_LAN = (
    network("en0", "192.168.1.170", "192.168.1.0/24"),
    network("en0", "2001:db8:1::10", "2001:db8:1::/64"),
)
RESET_AIRPORT = BonjourResolvedService(
    name="Time Capsule b67fdb",
    hostname="Time-Capsule-b67fdb.local",
    service_type="_airport._tcp.local.",
    port=5009,
    ipv4=["10.0.1.1", "169.254.155.34"],
    ipv6=["fe80::7273:cbff:feb2:71a2", "fe80::7273:cbff:feb2:71a2%en0"],
    properties={
        "raNm": "Apple Network b67fdb",
        "raNA": "1",
        "raSt": "0",
        "prob": "waCF;opNW;pubP;+",
        "syFl": "0x8A6C",
        "syAP": "116",
    },
    fullname="Time Capsule b67fdb._airport._tcp.local.",
)


def routes(table: dict[str, RouteSelection]):
    return lambda address: table.get(address, RouteSelection("unknown", error="not in table"))


class ConnectErrorKindTests(unittest.TestCase):
    def test_each_connect_error_gets_one_kind(self) -> None:
        cases = {
            "timed out": "timeout",
            "[Errno 60] Operation timed out": "timeout",
            "[Errno 61] Connection refused": "refused",
            "[Errno 64] Host is down": "host_down",
            "[Errno 65] No route to host": "no_route",
            "[Errno 51] Network is unreachable": "network_unreachable",
            "[Errno 50] Network is down": "network_unreachable",
            "[Errno 49] Can't assign requested address": "address_unavailable",
            "[Errno 8] nodename nor servname provided, or not known": "name_lookup_failed",
            "[Errno -2] Name or service not known": "name_lookup_failed",
            "[Errno 1] Operation not permitted": "not_permitted",
            "connection failed": "other",
            "": "other",
        }
        for error, kind in cases.items():
            with self.subTest(error=error):
                self.assertEqual(acp_diagnostics.connect_error_kind(error), kind)

    def test_no_error_has_no_kind(self) -> None:
        self.assertIsNone(acp_diagnostics.connect_error_kind(None))

    def test_errors_from_several_addresses_keep_each_distinct_kind_once(self) -> None:
        error = "timed out; [Errno 65] No route to host; timed out"

        self.assertEqual(acp_diagnostics.connect_error_kind(error), "timeout+no_route")


class AddressSummaryTests(unittest.TestCase):
    def test_scopes(self) -> None:
        cases = {
            "10.0.1.1": "private",
            "192.168.1.5": "private",
            "100.99.99.10": "shared",
            "169.254.9.9": "link_local",
            "127.0.0.1": "loopback",
            "8.8.8.8": "global",
            "fd7a:115c:a1e0::5": "ula",
            "fe80::1": "link_local",
            "2600:1700::1": "global",
        }
        for address, scope in cases.items():
            with self.subTest(address=address):
                self.assertEqual(acp_diagnostics.address_scope(ipaddress.ip_address(address)), scope)

    def test_public_addresses_are_reduced_to_family_and_scope(self) -> None:
        self.assertEqual(
            acp_diagnostics.address_summary("2600:1700::1"),
            {"family": "ipv6", "scope": "global"},
        )
        self.assertEqual(
            acp_diagnostics.address_summary("fe80::1%en0"),
            {"family": "ipv6", "scope": "link_local", "address": "fe80::1%en0"},
        )
        self.assertEqual(acp_diagnostics.address_summary("capsule.local"), {"family": "unknown"})


class LocalNetworksFieldTests(unittest.TestCase):
    def test_groups_by_interface_and_hides_public_and_host_prefixes(self) -> None:
        field = acp_diagnostics.local_networks_field([
            network("en0", "192.168.1.170", "192.168.1.0/24"),
            network("en0", "2600:1700:83b7:20f::1", "2600:1700:83b7:20f::/64"),
            network("en0", "2600:1700:83b7:20f::2", "2600:1700:83b7:20f::/64"),
            network("utun4", "100.99.99.10", "100.99.99.10/32"),
            network("utun4", "fd7a:115c:a1e0::5", "fd7a:115c:a1e0::/48"),
        ])

        self.assertEqual(field, [
            {"interface": "en0", "kind": "lan", "networks": [
                {"family": "ipv4", "scope": "private", "prefixlen": 24, "network": "192.168.1.0/24"},
                {"family": "ipv6", "scope": "global", "prefixlen": 64},
            ]},
            {"interface": "utun4", "kind": "vpn", "networks": [
                {"family": "ipv4", "scope": "shared", "prefixlen": 32},
                {"family": "ipv6", "scope": "ula", "prefixlen": 48, "network": "fd7a:115c:a1e0::/48"},
            ]},
        ])


class AddressContextTests(unittest.TestCase):
    def context(self, address: str, selection: RouteSelection, networks=HOME_LAN) -> dict[str, object]:
        return acp_diagnostics.address_context(address, "target", networks, lambda _address: selection)

    def test_address_on_this_computers_network_is_reached_directly(self) -> None:
        entry = self.context("192.168.1.1", RouteSelection("available", source="192.168.1.170"))

        self.assertEqual(entry, {
            "role": "target",
            "family": "ipv4",
            "scope": "private",
            "address": "192.168.1.1",
            "link": "on_link",
            "route": "direct",
            "route_interface": "en0",
        })

    def test_reset_router_address_off_this_network_goes_through_the_gateway(self) -> None:
        entry = self.context("10.0.1.1", RouteSelection("available", source="192.168.1.170"))

        self.assertEqual((entry["link"], entry["route"], entry["route_interface"]), ("off_link", "gateway", "en0"))

    def test_ipv6_is_placed_by_its_own_prefixes(self) -> None:
        on_link = self.context("2001:db8:1::2", RouteSelection("available", source="2001:db8:1::10"))
        off_link = self.context("2001:db8:2::2", RouteSelection("available", source="2001:db8:1::10"))

        self.assertEqual((on_link["link"], on_link["route"]), ("on_link", "direct"))
        self.assertEqual((off_link["link"], off_link["route"]), ("off_link", "gateway"))
        self.assertNotIn("address", off_link)

    def test_vpn_route_is_reported_as_vpn(self) -> None:
        networks = (*HOME_LAN, network("utun4", "100.99.99.10", "100.99.99.10/32"))

        entry = self.context("10.0.1.1", RouteSelection("available", source="100.99.99.10"), networks)

        self.assertEqual((entry["route"], entry["route_interface"]), ("vpn", "utun4"))

    def test_link_local_is_direct_and_its_link_is_unknown(self) -> None:
        entry = self.context("fe80::2%en0", RouteSelection("available", source="fe80::9%en0"))

        self.assertEqual((entry["link"], entry["route"], entry["route_interface"]), ("unknown", "direct", "en0"))

    def test_family_without_local_networks_is_unknown(self) -> None:
        ipv4_only = (network("en0", "192.168.1.170", "192.168.1.0/24"),)

        entry = self.context("2001:db8:1::2", RouteSelection("unavailable", error="no route"), ipv4_only)

        self.assertEqual((entry["link"], entry["route"]), ("unknown", "none"))
        self.assertNotIn("route_interface", entry)

    def test_route_errors_are_unknown(self) -> None:
        entry = self.context("192.168.1.1", RouteSelection("unknown", error="odd"))

        self.assertEqual(entry["route"], "unknown")


class CandidateAddressTests(unittest.TestCase):
    def test_record_adds_its_other_addresses_after_the_target(self) -> None:
        candidates = acp_diagnostics.candidate_addresses("10.0.1.1", RESET_AIRPORT, resolve=mock.Mock())

        self.assertEqual(candidates, [
            ("10.0.1.1", "target"),
            ("169.254.155.34", "record"),
            ("fe80::7273:cbff:feb2:71a2%en0", "record"),
        ])

    def test_hostname_target_is_resolved_and_not_repeated_from_the_record(self) -> None:
        resolve = mock.Mock(return_value=("10.0.1.1", "fe80::7273:cbff:feb2:71a2%en0"))

        candidates = acp_diagnostics.candidate_addresses("Time-Capsule-b67fdb.local", RESET_AIRPORT, resolve=resolve)

        resolve.assert_called_once_with("Time-Capsule-b67fdb.local")
        self.assertEqual(candidates, [
            ("10.0.1.1", "target"),
            ("fe80::7273:cbff:feb2:71a2%en0", "target"),
            ("169.254.155.34", "record"),
        ])

    def test_unresolvable_hostname_without_a_record_has_no_addresses(self) -> None:
        self.assertEqual(acp_diagnostics.candidate_addresses("gone.local", None, resolve=mock.Mock(return_value=())), [])


class RecordFlagsTests(unittest.TestCase):
    def test_reset_airport_reports_its_flags_and_default_name(self) -> None:
        self.assertEqual(acp_diagnostics.record_flags(RESET_AIRPORT), {
            "raNA": "1",
            "raSt": "0",
            "prob": "waCF;opNW;pubP;+",
            "syFl": "0x8A6C",
            "default_network_name": True,
        })

    def test_user_named_network_is_not_reported_by_name(self) -> None:
        record = BonjourResolvedService("TC", "tc.local", properties={"raNm": "Sandra's Wi-Fi", "raNA": "0"})

        flags = acp_diagnostics.record_flags(record)

        self.assertEqual(flags, {"raNA": "0", "default_network_name": False})
        self.assertNotIn("Sandra", repr(flags))

    def test_bridge_mode_record_without_a_network_name_says_so(self) -> None:
        record = BonjourResolvedService("TC", "tc.local", properties={"raSt": "3"})

        self.assertEqual(acp_diagnostics.record_flags(record), {"raSt": "3", "default_network_name": None})


class ProbeContextFieldsTests(unittest.TestCase):
    def test_reports_targets_networks_and_record_flags(self) -> None:
        route = routes({
            "10.0.1.1": RouteSelection("available", source="192.168.1.170"),
            "169.254.155.34": RouteSelection("available", source="192.168.1.170"),
            "fe80::7273:cbff:feb2:71a2%en0": RouteSelection("available", source="fe80::9%en0"),
        })

        fields = acp_diagnostics.probe_context_fields("10.0.1.1", RESET_AIRPORT, networks=HOME_LAN, route=route)

        targets = fields["acp_target_addresses"]
        self.assertEqual([(entry["role"], entry["address"], entry["link"]) for entry in targets], [
            ("target", "10.0.1.1", "off_link"),
            ("record", "169.254.155.34", "unknown"),
            ("record", "fe80::7273:cbff:feb2:71a2%en0", "unknown"),
        ])
        self.assertEqual(fields["local_networks"][0]["interface"], "en0")
        self.assertTrue(fields["acp_record_flags"]["default_network_name"])

    def test_without_a_record_only_the_target_is_described(self) -> None:
        fields = acp_diagnostics.probe_context_fields(
            "192.168.1.1",
            None,
            networks=HOME_LAN,
            route=routes({"192.168.1.1": RouteSelection("available", source="192.168.1.170")}),
        )

        self.assertEqual(len(fields["acp_target_addresses"]), 1)
        self.assertNotIn("acp_record_flags", fields)

    def test_routes_are_checked_against_the_acp_port(self) -> None:
        with mock.patch(
            "timecapsulesmb.services.acp_diagnostics.select_route_to_address",
            return_value=RouteSelection("available", source="192.168.1.170"),
        ) as select_route:
            acp_diagnostics.probe_context_fields("192.168.1.1", None, networks=HOME_LAN)

        select_route.assert_called_once_with("192.168.1.1", port=ACP_PORT)


class FreshLookupTests(unittest.TestCase):
    def resolved(self, ipv4=(), ipv6=()) -> BonjourResolvedService:
        return BonjourResolvedService(RESET_AIRPORT.name, RESET_AIRPORT.hostname, ipv4=list(ipv4), ipv6=list(ipv6))

    def lookup(self, by_family: dict[str, object]) -> tuple[dict[str, object], mock.Mock]:
        def resolve(instance, timeout_ms, *, family, **_kwargs):
            outcome = by_family[family]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        resolver = mock.Mock(side_effect=resolve)
        with mock.patch("timecapsulesmb.discovery.bonjour.command_exists", return_value=False), mock.patch(
            "timecapsulesmb.discovery.zeroconf_backend.resolve_service_instance", resolver
        ):
            return acp_diagnostics.fresh_lookup(RESET_AIRPORT), resolver

    def test_unchanged_addresses(self) -> None:
        result, resolver = self.lookup({
            "ipv4": self.resolved(["10.0.1.1", "169.254.155.34"], ["fe80::7273:cbff:feb2:71a2%4"]),
            "ipv6": self.resolved(ipv6=["fe80::7273:cbff:feb2:71a2%en0"]),
        })

        self.assertEqual(result, {
            "ipv4": "answered",
            "ipv6": "answered",
            "addresses_changed": False,
            "added": [],
            "removed": [],
        })
        instance = resolver.call_args.args[0]
        self.assertEqual((instance.service_type, instance.fullname), (RESET_AIRPORT.service_type, RESET_AIRPORT.fullname))
        self.assertEqual(resolver.call_args.args[1], acp_diagnostics.FRESH_LOOKUP_TIMEOUT_MS)
        self.assertEqual({call.kwargs["family"] for call in resolver.call_args_list}, {"ipv4", "ipv6"})

    def test_new_lan_address_after_a_restart(self) -> None:
        result, _resolver = self.lookup({
            "ipv4": self.resolved(["192.168.1.40", "169.254.155.34"], ["fe80::7273:cbff:feb2:71a2%en0"]),
            "ipv6": None,
        })

        self.assertEqual(result["ipv6"], "answered")
        self.assertTrue(result["addresses_changed"])
        self.assertEqual(result["added"], [{"family": "ipv4", "scope": "private", "address": "192.168.1.40"}])
        self.assertEqual(result["removed"], [{"family": "ipv4", "scope": "private", "address": "10.0.1.1"}])

    def test_healthy_transport_can_supply_both_address_families(self) -> None:
        result, _ = self.lookup({
            "ipv4": self.resolved(RESET_AIRPORT.ipv4, RESET_AIRPORT.ipv6),
            "ipv6": RuntimeError("IPv6 transport unavailable"),
        })
        self.assertEqual(result["ipv4"], "answered")
        self.assertEqual(result["ipv6"], "answered")
        self.assertFalse(result["addresses_changed"])

    def test_no_answer_cannot_say_whether_addresses_changed(self) -> None:
        result, _resolver = self.lookup({"ipv4": None, "ipv6": RuntimeError("zeroconf is missing")})

        self.assertEqual(result, {"ipv4": "no_answer", "ipv6": "error", "ipv6_error": "RuntimeError: zeroconf is missing"})


class FailureFieldsTests(unittest.TestCase):
    def test_probes_the_records_other_addresses_and_looks_the_name_up_again(self) -> None:
        def connect(address: str, _port: int, _timeout: float) -> str | None:
            return None if address == "169.254.155.34" else "timed out"

        connect_mock = mock.Mock(side_effect=connect)
        lookup = mock.Mock(return_value={"ipv4": "answered"})

        fields = acp_diagnostics.failure_fields(
            "10.0.1.1", RESET_AIRPORT, connect=connect_mock, lookup=lookup, resolve=mock.Mock(),
        )

        self.assertEqual(sorted(call.args for call in connect_mock.call_args_list), [
            ("169.254.155.34", ACP_PORT, acp_diagnostics.ALT_PROBE_TIMEOUT_SECONDS),
            ("fe80::7273:cbff:feb2:71a2%en0", ACP_PORT, acp_diagnostics.ALT_PROBE_TIMEOUT_SECONDS),
        ])
        self.assertEqual(fields["acp_alt_probe"], [
            {"family": "ipv4", "scope": "link_local", "address": "169.254.155.34", "reachable": True},
            {
                "family": "ipv6",
                "scope": "link_local",
                "address": "fe80::7273:cbff:feb2:71a2%en0",
                "reachable": False,
                "error_kind": "timeout",
            },
        ])
        lookup.assert_called_once_with(RESET_AIRPORT)
        self.assertEqual(fields["acp_fresh_lookup"], {"ipv4": "answered"})

    def test_probe_and_lookup_errors_are_recorded(self) -> None:
        fields = acp_diagnostics.failure_fields(
            "10.0.1.1",
            RESET_AIRPORT,
            connect=mock.Mock(side_effect=OSError("bad scope")),
            lookup=mock.Mock(side_effect=RuntimeError("no zeroconf")),
            resolve=mock.Mock(),
        )

        self.assertEqual({entry["reachable"] for entry in fields["acp_alt_probe"]}, {False})
        self.assertEqual({entry["error_kind"] for entry in fields["acp_alt_probe"]}, {"other"})
        self.assertEqual(fields["acp_fresh_lookup"], {"error": "no zeroconf"})

    def test_typed_address_without_a_record_adds_nothing(self) -> None:
        connect = mock.Mock()

        self.assertEqual(acp_diagnostics.failure_fields("10.0.1.1", None, connect=connect), {})
        connect.assert_not_called()

    def test_record_without_other_addresses_or_a_name_adds_nothing(self) -> None:
        record = BonjourResolvedService("TC", "tc.local", ipv4=["10.0.1.1"])
        connect = mock.Mock()

        self.assertEqual(acp_diagnostics.failure_fields("10.0.1.1", record, connect=connect, resolve=mock.Mock()), {})
        connect.assert_not_called()

    def test_record_without_other_addresses_still_looks_the_name_up(self) -> None:
        record = BonjourResolvedService(
            "TC", "tc.local", "_airport._tcp.local.", ipv4=["10.0.1.1"], fullname="TC._airport._tcp.local.",
        )
        lookup = mock.Mock(return_value={"ipv4": "no_answer", "ipv6": "no_answer"})

        fields = acp_diagnostics.failure_fields("10.0.1.1", record, connect=mock.Mock(), lookup=lookup, resolve=mock.Mock())

        self.assertEqual(fields, {"acp_fresh_lookup": {"ipv4": "no_answer", "ipv6": "no_answer"}})


if __name__ == "__main__":
    unittest.main()
