from __future__ import annotations

import errno
import unittest
from unittest import mock

from timecapsulesmb.checks.network_plan import (
    RouteSelection,
    bind_interface_families,
    build_network_check_plan,
    local_sources_for_remote_cidrs,
    normalize_family_tokens,
    parse_bind_cidrs,
    parse_bind_interfaces,
    select_route_to_address,
)


class NetworkPlanTests(unittest.TestCase):
    def test_parse_bind_cidrs_ignores_loopback_and_invalid_tokens(self) -> None:
        cidrs = parse_bind_cidrs("127.0.0.1/8 ::1/128 10.0.1.2/24 fdbb::1/64 bad")
        self.assertEqual(cidrs, ("10.0.1.0/24", "fdbb::/64"))

    def test_bind_interface_families_uses_parsed_non_loopback_interfaces(self) -> None:
        families = bind_interface_families("127.0.0.1/8 ::1/128 10.0.1.2/24 bad fd00::2/64 10.0.1.3/24")
        self.assertEqual(families, ("ipv4", "ipv6"))

    def test_parse_bind_interfaces_preserves_remote_addresses(self) -> None:
        interfaces = parse_bind_interfaces("127.0.0.1/8 ::1/128 10.0.1.2/8 fdbb::1/64 fe80:8::40/64")
        self.assertEqual(tuple((interface.address, interface.cidr, interface.family) for interface in interfaces), (
            ("10.0.1.2", "10.0.0.0/8", "ipv4"),
            ("fdbb::1", "fdbb::/64", "ipv6"),
            ("fe80::40", "fe80::/64", "ipv6"),
        ))

    def test_network_plan_scopes_netbsd_link_local_bind_address_for_local_interface(self) -> None:
        selected: list[str] = []

        def select_route(address: str) -> RouteSelection:
            selected.append(address)
            return RouteSelection("available", source="fe80::55%17")

        plan = build_network_check_plan(
            smb_bind_interfaces="fe80:8::40/64",
            mdns_families=("ipv6",),
            nbns_families=(),
            local_addresses=("fe80::55%17",),
            route_selector=select_route,
        )

        self.assertEqual(selected, ["fe80::40%17"])
        self.assertEqual(plan.ipv6.remote_addresses, ("fe80::40%17",))
        self.assertEqual(plan.ipv6.remote_cidrs, ("fe80::/64",))
        self.assertEqual(plan.ipv6.local_sources, ("fe80::55%17",))

    def test_local_sources_match_remote_ipv4_and_ipv6_cidrs(self) -> None:
        self.assertEqual(
            local_sources_for_remote_cidrs(
                ("10.0.1.0/24", "fdbb::/64"),
                family="ipv4",
                local_addresses=("10.0.1.3", "192.168.1.3", "fdbb::3"),
            ),
            ("10.0.1.3",),
        )
        self.assertEqual(
            local_sources_for_remote_cidrs(
                ("10.0.1.0/24", "fdbb::/64"),
                family="ipv6",
                local_addresses=("10.0.1.3", "fdbb::3", "fdcc::3"),
            ),
            ("fdbb::3",),
        )

    def test_build_network_check_plan_keeps_mdns_samba_dual_stack_and_nbns_ipv4_only(self) -> None:
        routes = {
            "10.0.1.2": RouteSelection("available", source="10.0.1.3"),
            "fdbb::1": RouteSelection("unavailable", error="no route", error_number=errno.ENETUNREACH),
        }
        plan = build_network_check_plan(
            smb_bind_interfaces="127.0.0.1/8 ::1/128 10.0.1.2/24 fdbb::1/64",
            mdns_families=("ipv4", "ipv6"),
            nbns_families=("ipv4", "ipv6"),
            local_addresses=("10.0.1.3",),
            route_selector=routes.__getitem__,
        )

        self.assertTrue(plan.ipv4.mdns_expected)
        self.assertTrue(plan.ipv4.samba_expected)
        self.assertTrue(plan.ipv4.nbns_expected)
        self.assertEqual(plan.ipv4.remote_addresses, ("10.0.1.2",))
        self.assertEqual(plan.ipv4.local_sources, ("10.0.1.3",))
        self.assertTrue(plan.ipv6.mdns_expected)
        self.assertTrue(plan.ipv6.samba_expected)
        self.assertFalse(plan.ipv6.nbns_expected)
        self.assertEqual(plan.ipv6.remote_addresses, ("fdbb::1",))
        self.assertEqual(plan.ipv6.local_sources, ())
        self.assertEqual(plan.ipv6.endpoints[0].route.state, "unavailable")

    def test_network_plan_keeps_route_state_per_ipv6_address(self) -> None:
        routes = {
            "10.0.1.2": RouteSelection("available", source="10.0.1.3"),
            "fdbb::2": RouteSelection("unavailable", error="no route", error_number=errno.ENETUNREACH),
            "fda3::2": RouteSelection("available", source="fda3::9"),
        }

        plan = build_network_check_plan(
            smb_bind_interfaces="10.0.1.2/24 fdbb::2/64 fda3::2/64",
            mdns_families=("ipv4", "ipv6"),
            nbns_families=("ipv4",),
            local_addresses=("10.0.1.3", "fda3::9"),
            route_selector=routes.__getitem__,
        )

        self.assertEqual(
            [
                (
                    endpoint.address,
                    endpoint.cidr,
                    endpoint.route.state,
                    endpoint.on_link_sources,
                    endpoint.local_sources,
                )
                for endpoint in plan.ipv6.endpoints
            ],
            [
                ("fdbb::2", "fdbb::/64", "unavailable", (), ()),
                ("fda3::2", "fda3::/64", "available", ("fda3::9",), ("fda3::9",)),
            ],
        )
        self.assertEqual(plan.ipv6.local_sources, ("fda3::9",))

    def test_network_plan_supports_routed_ipv6_outside_the_remote_prefix(self) -> None:
        plan = build_network_check_plan(
            smb_bind_interfaces="fd00::2/64",
            mdns_families=("ipv6",),
            nbns_families=(),
            local_addresses=("2001:db8:1::9",),
            route_selector=lambda _address: RouteSelection("available", source="2001:db8:1::9"),
        )

        endpoint = plan.ipv6.endpoints[0]
        self.assertEqual(endpoint.on_link_sources, ())
        self.assertEqual(endpoint.local_sources, ("2001:db8:1::9",))
        self.assertTrue(endpoint.applicable)

    def test_network_plan_does_not_treat_stale_on_link_address_as_a_usable_route(self) -> None:
        plan = build_network_check_plan(
            smb_bind_interfaces="fdbb::2/64",
            mdns_families=("ipv6",),
            nbns_families=(),
            local_addresses=("fdbb::9",),
            route_selector=lambda _address: RouteSelection(
                "unavailable",
                error="No route to host",
                error_number=errno.EHOSTUNREACH,
            ),
        )

        endpoint = plan.ipv6.endpoints[0]
        self.assertEqual(endpoint.on_link_sources, ("fdbb::9",))
        self.assertEqual(endpoint.local_sources, ())
        self.assertFalse(endpoint.applicable)

    def test_select_route_to_address_reports_kernel_selected_source(self) -> None:
        fake_socket = mock.MagicMock()
        fake_socket.__enter__.return_value = fake_socket
        fake_socket.getsockname.return_value = ("fda3::9", 43210, 0, 0)

        with mock.patch("timecapsulesmb.checks.network_plan.socket.socket", return_value=fake_socket):
            result = select_route_to_address("fda3::2")

        self.assertEqual(result, RouteSelection("available", source="fda3::9"))
        fake_socket.connect.assert_called_once_with(("fda3::2", 445, 0, 0))

    def test_select_route_to_address_uses_ipv6_scope_id_without_putting_it_in_address(self) -> None:
        fake_socket = mock.MagicMock()
        fake_socket.__enter__.return_value = fake_socket
        fake_socket.getsockname.return_value = ("fe80::9", 43210, 0, 17)

        with (
            mock.patch("timecapsulesmb.checks.network_plan.socket.socket", return_value=fake_socket),
            mock.patch("timecapsulesmb.checks.network_plan.socket.if_nametoindex", return_value=17),
            mock.patch("timecapsulesmb.checks.network_plan.socket.if_indextoname", return_value="en0"),
        ):
            result = select_route_to_address("fe80::2%en0")

        self.assertEqual(result, RouteSelection("available", source="fe80::9%en0"))
        fake_socket.connect.assert_called_once_with(("fe80::2", 445, 0, 17))

    def test_select_route_to_address_distinguishes_unavailable_and_unknown_errors(self) -> None:
        unavailable_socket = mock.MagicMock()
        unavailable_socket.__enter__.return_value = unavailable_socket
        unavailable_socket.connect.side_effect = OSError(errno.ENETUNREACH, "Network is unreachable")
        with mock.patch("timecapsulesmb.checks.network_plan.socket.socket", return_value=unavailable_socket):
            unavailable = select_route_to_address("fdbb::2")

        unknown_socket = mock.MagicMock()
        unknown_socket.__enter__.return_value = unknown_socket
        unknown_socket.connect.side_effect = OSError(errno.EACCES, "Permission denied")
        with mock.patch("timecapsulesmb.checks.network_plan.socket.socket", return_value=unknown_socket):
            unknown = select_route_to_address("fdbb::2")

        self.assertEqual(unavailable.state, "unavailable")
        self.assertEqual(unavailable.error_number, errno.ENETUNREACH)
        self.assertEqual(unknown.state, "unknown")
        self.assertEqual(unknown.error_number, errno.EACCES)

    def test_normalize_family_tokens_filters_unknowns_and_deduplicates(self) -> None:
        self.assertEqual(normalize_family_tokens(["ipv4", "bad", "ipv6", "ipv4"]), ("ipv4", "ipv6"))


if __name__ == "__main__":
    unittest.main()
