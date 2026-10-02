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

from timecapsulesmb.checks.network import (
    LocalInterfaceNetwork,
    RouteSelection,
    local_interface_networks,
    select_route_to_address,
)


def adapter(name: str, *ips: tuple[object, object]) -> SimpleNamespace:
    return SimpleNamespace(name=name, ips=[SimpleNamespace(ip=ip, network_prefix=prefix) for ip, prefix in ips])


class NetworkCheckTests(unittest.TestCase):
    def test_route_selection_preserves_client_link_local_scope(self) -> None:
        sock = mock.MagicMock()
        sock.__enter__.return_value = sock
        sock.getsockname.return_value = ("fe80::9", 43210, 0, 17)
        with (
            mock.patch("timecapsulesmb.checks.network.socket.socket", return_value=sock),
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
        with mock.patch("timecapsulesmb.checks.network.socket.socket", return_value=unavailable):
            unavailable_result = select_route_to_address("fd00::2")

        unknown = mock.MagicMock()
        unknown.__enter__.return_value = unknown
        unknown.connect.side_effect = OSError(errno.EACCES, "Permission denied")
        with mock.patch("timecapsulesmb.checks.network.socket.socket", return_value=unknown):
            unknown_result = select_route_to_address("fd00::2")

        self.assertEqual(unavailable_result.state, "unavailable")
        self.assertEqual(unavailable_result.error_number, errno.ENETUNREACH)
        self.assertEqual(unknown_result.state, "unknown")
        self.assertEqual(unknown_result.error_number, errno.EACCES)

    def test_unscoped_link_local_never_uses_the_default_route(self) -> None:
        with mock.patch("timecapsulesmb.checks.network.socket.socket") as socket_mock:
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


if __name__ == "__main__":
    unittest.main()
