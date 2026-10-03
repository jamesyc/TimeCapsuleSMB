from __future__ import annotations

import errno
import sys
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

import ipaddress
from types import SimpleNamespace

from timecapsulesmb.checks.network import LocalInterfaceNetwork, NetworkLinkResult, classify_network_link, local_interface_networks, network_display, reportable_network
from timecapsulesmb.core.net import RouteSelection, select_route_to_address


def adapter(name: str, *ips: tuple[object, object]) -> SimpleNamespace:
    return SimpleNamespace(name=name, ips=[SimpleNamespace(ip=ip, network_prefix=prefix) for ip, prefix in ips])


class NetworkCheckTests(unittest.TestCase):
    def test_route_selection_preserves_client_link_local_scope(self) -> None:
        sock = mock.MagicMock()
        sock.__enter__.return_value = sock
        sock.getsockname.return_value = ("fe80::9", 43210, 0, 17)
        with (
            mock.patch("timecapsulesmb.core.net.socket.socket", return_value=sock),
            mock.patch("timecapsulesmb.core.net.socket.if_nametoindex", return_value=17),
            mock.patch("timecapsulesmb.core.net.socket.if_indextoname", return_value="en0"),
        ):
            result = select_route_to_address("fe80::2%en0")
        self.assertEqual(result, RouteSelection("available", source="fe80::9%en0"))
        sock.connect.assert_called_once_with(("fe80::2", 445, 0, 17))

    def test_route_selection_distinguishes_unavailable_and_unknown_errors(self) -> None:
        unavailable = mock.MagicMock()
        unavailable.__enter__.return_value = unavailable
        unavailable.connect.side_effect = OSError(errno.ENETUNREACH, "Network is unreachable")
        with mock.patch("timecapsulesmb.core.net.socket.socket", return_value=unavailable):
            unavailable_result = select_route_to_address("fd00::2")

        unknown = mock.MagicMock()
        unknown.__enter__.return_value = unknown
        unknown.connect.side_effect = OSError(errno.EACCES, "Permission denied")
        with mock.patch("timecapsulesmb.core.net.socket.socket", return_value=unknown):
            unknown_result = select_route_to_address("fd00::2")

        self.assertEqual(unavailable_result.state, "unavailable")
        self.assertEqual(unavailable_result.error_number, errno.ENETUNREACH)
        self.assertEqual(unknown_result.state, "unknown")
        self.assertEqual(unknown_result.error_number, errno.EACCES)

    def test_unscoped_link_local_never_uses_the_default_route(self) -> None:
        with mock.patch("timecapsulesmb.core.net.socket.socket") as socket_mock:
            result = select_route_to_address("fe80::40")
        self.assertEqual(result.state, "unavailable")
        self.assertIn("scope", result.error or "")
        socket_mock.assert_not_called()


class LocalInterfaceNetworkTests(unittest.TestCase):
    def test_lists_each_interface_network_in_both_families(self) -> None:
        networks = local_interface_networks([
            adapter(
                "en0",
                ("192.168.1.170", 24),
                (("2001:db8:1::10", 0, 0), 64),
                (("2001:db8:1::11", 0, 0), 64),
            ),
            adapter("utun4", ("100.99.99.10", 32), (("fd7a:115c:a1e0::5", 0, 0), 48)),
        ])

        self.assertEqual(networks, (
            LocalInterfaceNetwork("en0", "192.168.1.170", ipaddress.ip_network("192.168.1.0/24")),
            LocalInterfaceNetwork("en0", "2001:db8:1::10", ipaddress.ip_network("2001:db8:1::/64")),
            LocalInterfaceNetwork("en0", "2001:db8:1::11", ipaddress.ip_network("2001:db8:1::/64")),
            LocalInterfaceNetwork("utun4", "100.99.99.10", ipaddress.ip_network("100.99.99.10/32")),
            LocalInterfaceNetwork("utun4", "fd7a:115c:a1e0::5", ipaddress.ip_network("fd7a:115c:a1e0::/48")),
        ))

    def test_leaves_out_loopback_link_local_and_unusable_entries(self) -> None:
        networks = local_interface_networks([
            adapter("lo0", ("127.0.0.1", 8), (("::1", 0, 0), 128)),
            adapter(
                "en0",
                (("fe80::1", 0, 4), 64),
                ("169.254.10.2", 16),
                ("not-an-ip", 24),
                ("10.0.0.5", None),
                ("10.0.0.5", 24),
            ),
        ])

        self.assertEqual(networks, (LocalInterfaceNetwork("en0", "10.0.0.5", ipaddress.ip_network("10.0.0.0/24")),))

    def test_returns_nothing_when_interfaces_cannot_be_read(self) -> None:
        with mock.patch.dict(sys.modules, {"ifaddr": None}):
            self.assertEqual(local_interface_networks(), ())


def nets(*values: str) -> tuple:
    return tuple(ipaddress.ip_network(value) for value in values)


class NetworkLinkTests(unittest.TestCase):
    def classify(self, device: tuple, local: tuple) -> NetworkLinkResult:
        return classify_network_link(device, local, source="device_ifconfig")

    def test_any_overlap_in_either_family_means_shared(self) -> None:
        cases = {
            "same IPv4 LAN": (nets("192.168.1.0/24"), nets("192.168.1.0/24")),
            "wider local prefix": (nets("192.168.1.0/24"), nets("192.168.0.0/16")),
            "IPv6 shared while IPv4 differs": (
                nets("10.0.1.0/24", "2001:db8:1::/64"),
                nets("192.168.1.0/24", "2001:db8:1::/64"),
            ),
            "second local interface on the device's LAN": (
                nets("192.168.1.0/24"),
                nets("10.20.0.0/24", "192.168.1.0/24"),
            ),
            "device address inside a local network": (nets("192.168.1.5/32"), nets("192.168.1.0/24")),
        }
        for name, (device, local) in cases.items():
            with self.subTest(name):
                self.assertEqual(self.classify(device, local).verdict, "shared")

    def test_a_compared_family_without_overlap_means_separate(self) -> None:
        cases = {
            "reset router network": (nets("10.0.1.0/24"), nets("192.168.1.0/24")),
            "IPv6 only on this computer": (nets("10.0.1.0/24"), nets("192.168.1.0/24", "2001:db8:9::/64")),
            "IPv6-only home network": (nets("2001:db8:1::/64"), nets("2001:db8:2::/64")),
            "VPN only": (nets("192.168.1.0/24"), nets("100.99.99.10/32", "fd7a:115c:a1e0::/48")),
        }
        for name, (device, local) in cases.items():
            with self.subTest(name):
                self.assertEqual(self.classify(device, local).verdict, "separate")

    def test_no_family_on_both_sides_is_unknown(self) -> None:
        cases = {
            "IPv6-only computer, IPv4-only device": (nets("192.168.1.0/24"), nets("2001:db8:1::/64")),
            "nothing known about the device": ((), nets("192.168.1.0/24")),
            "no local networks": (nets("192.168.1.0/24"), ()),
        }
        for name, (device, local) in cases.items():
            with self.subTest(name):
                self.assertEqual(self.classify(device, local).verdict, "unknown")

    def test_families_and_compared_networks_leave_out_one_sided_families(self) -> None:
        link = self.classify(nets("10.0.1.0/24", "10.0.1.0/24"), nets("192.168.1.0/24", "2001:db8:9::/64"))

        self.assertEqual(link.device_networks, nets("10.0.1.0/24"))
        self.assertEqual(link.families, ("ipv4",))
        self.assertEqual(link.compared(), (nets("192.168.1.0/24"), nets("10.0.1.0/24")))

    def test_telemetry_hides_public_prefixes_and_this_computers_address(self) -> None:
        link = classify_network_link(
            nets("192.168.1.0/24", "2600:1700:83b7:20f::/64"),
            nets("10.20.0.0/24", "100.99.99.10/32", "2600:1700:aaaa::/64", "fd7a:115c:a1e0::/48"),
            source="device_ifconfig",
        )

        self.assertEqual(link.telemetry(), {
            "verdict": "separate",
            "source": "device_ifconfig",
            "families": ["ipv4", "ipv6"],
            "device_networks": ["192.168.1.0/24", "ipv6-public/64"],
            "local_networks": ["10.20.0.0/24", "ipv4-host/32", "ipv6-public/64", "fd7a:115c:a1e0::/48"],
            "detail": None,
        })

    def test_reportable_network_keeps_device_addresses_and_display_drops_host_lengths(self) -> None:
        self.assertEqual(reportable_network(ipaddress.ip_network("192.168.1.5/32")), "192.168.1.5/32")
        self.assertEqual(reportable_network(ipaddress.ip_network("8.8.8.0/24")), "ipv4-public/24")
        self.assertEqual(network_display(ipaddress.ip_network("192.168.1.5/32")), "192.168.1.5")
        self.assertEqual(network_display(ipaddress.ip_network("2001:db8::/64")), "2001:db8::/64")


if __name__ == "__main__":
    unittest.main()
