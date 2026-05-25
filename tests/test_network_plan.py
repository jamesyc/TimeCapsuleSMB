from __future__ import annotations

import unittest

from timecapsulesmb.checks.network_plan import (
    bind_interface_families,
    build_network_check_plan,
    local_sources_for_remote_cidrs,
    normalize_family_tokens,
    parse_bind_cidrs,
    parse_bind_interfaces,
)


class NetworkPlanTests(unittest.TestCase):
    def test_parse_bind_cidrs_ignores_loopback_and_invalid_tokens(self) -> None:
        cidrs = parse_bind_cidrs("127.0.0.1/8 ::1/128 10.0.1.2/24 fdbb::1/64 bad")
        self.assertEqual(cidrs, ("10.0.1.0/24", "fdbb::/64"))

    def test_bind_interface_families_uses_parsed_non_loopback_interfaces(self) -> None:
        families = bind_interface_families("127.0.0.1/8 ::1/128 10.0.1.2/24 bad fd00::2/64 10.0.1.3/24")
        self.assertEqual(families, ("ipv4", "ipv6"))

    def test_parse_bind_interfaces_preserves_remote_addresses(self) -> None:
        interfaces = parse_bind_interfaces("127.0.0.1/8 ::1/128 10.0.1.2/8 fdbb::1/64")
        self.assertEqual(tuple((interface.address, interface.cidr, interface.family) for interface in interfaces), (
            ("10.0.1.2", "10.0.0.0/8", "ipv4"),
            ("fdbb::1", "fdbb::/64", "ipv6"),
        ))

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
        plan = build_network_check_plan(
            smb_bind_interfaces="127.0.0.1/8 ::1/128 10.0.1.2/24 fdbb::1/64",
            mdns_families=("ipv4", "ipv6"),
            nbns_families=("ipv4", "ipv6"),
            local_addresses=("10.0.1.3",),
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

    def test_normalize_family_tokens_filters_unknowns_and_deduplicates(self) -> None:
        self.assertEqual(normalize_family_tokens(["ipv4", "bad", "ipv6", "ipv4"]), ("ipv4", "ipv6"))


if __name__ == "__main__":
    unittest.main()
