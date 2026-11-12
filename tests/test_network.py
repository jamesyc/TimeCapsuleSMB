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
    ClosedSshPort,
    LocalInterfaceNetwork,
    NetworkLinkResult,
    NetworkPlace,
    classify_network_link,
    compare_place,
    local_interface_networks,
    macos_may_hold_this_process,
    network_display,
    read_network_place,
    reportable_network,
    terminal_may_be_held,
)
from timecapsulesmb.transport.local import Router
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



HOME = "aa:00:00:00:00:01"
NEIGHBOUR = "aa:00:00:00:00:02"
DEVICE = [ipaddress.ip_network("192.168.1.54/32")]


def place(*interfaces: tuple[str, str, str | None]) -> NetworkPlace:
    """(interface, network, router MAC or None for no router) per interface."""
    networks = tuple(
        LocalInterfaceNetwork(name, str(next(ipaddress.ip_network(network).hosts())), ipaddress.ip_network(network))
        for name, network, _mac in interfaces
    )
    routers = tuple(
        Router(name, str(next(ipaddress.ip_network(network).hosts())), mac)
        for name, network, mac in interfaces if mac != "none"
    )
    return NetworkPlace(networks, routers)


class ComparePlaceTests(unittest.TestCase):
    """Whether this computer is on the network it reached the device on."""

    def test_the_same_router_toward_the_device_is_the_same_network(self) -> None:
        result = compare_place(place(("en0", "192.168.1.0/24", HOME)), DEVICE, router_mac=HOME)

        self.assertEqual((result.verdict, result.evidence), ("same", "router"))
        self.assertEqual(result.telemetry(), {
            "client_place": "same", "place_evidence": "router", "router_mac_before": HOME, "router_mac_now": HOME,
        })

    def test_roaming_between_access_points_keeps_the_router(self) -> None:
        # Another interface, the same router: the Mac moved from cable to Wi-Fi.
        result = compare_place(place(("en1", "192.168.1.0/24", HOME)), DEVICE, router_mac=HOME)

        self.assertEqual(result.verdict, "same")

    def test_another_router_on_the_same_addresses_is_another_network(self) -> None:
        result = compare_place(place(("en0", "192.168.1.0/24", NEIGHBOUR)), DEVICE, router_mac=HOME)

        self.assertEqual((result.verdict, result.evidence, result.router_now), ("moved", "router", NEIGHBOUR))

    def test_no_network_holding_the_device_is_another_network(self) -> None:
        result = compare_place(place(("en0", "172.20.10.0/28", NEIGHBOUR)), DEVICE, router_mac=HOME)

        self.assertEqual((result.verdict, result.evidence), ("moved", "subnet"))

    def test_left_only_on_a_network_it_already_had_is_not_a_move(self) -> None:
        # A Mac on the device's Ethernet and another Wi-Fi: the restart drops
        # the Ethernet link and leaves it on the Wi-Fi it already had.
        before = place(("en0", "192.168.1.0/24", HOME), ("en1", "10.9.0.0/24", NEIGHBOUR))
        result = compare_place(
            place(("en1", "10.9.0.0/24", NEIGHBOUR)), DEVICE, router_mac=HOME,
            known_router_macs=before.router_macs, known_networks=[item.network for item in before.networks],
        )

        self.assertEqual((result.verdict, result.evidence), ("unknown", "known_networks"))

    def test_a_router_it_already_had_elsewhere_is_not_a_move(self) -> None:
        result = compare_place(
            place(("en0", "192.168.1.0/24", NEIGHBOUR)), DEVICE, router_mac=HOME,
            known_router_macs=frozenset({HOME, NEIGHBOUR}),
        )

        self.assertEqual((result.verdict, result.evidence), ("unknown", "known_router"))

    def test_no_lan_address_is_offline(self) -> None:
        result = compare_place(NetworkPlace((), ()), DEVICE, router_mac=HOME)

        self.assertEqual((result.verdict, result.evidence), ("offline", "no_address"))

    def test_an_unreadable_router_or_no_reference_is_unknown(self) -> None:
        cases = (
            ("router unresolved now", place(("en0", "192.168.1.0/24", None)), HOME),
            ("no router now", place(("en0", "192.168.1.0/24", "none")), HOME),
            ("no reference", place(("en0", "192.168.1.0/24", HOME)), None),
        )
        for name, now, reference in cases:
            with self.subTest(name):
                self.assertEqual(compare_place(now, DEVICE, router_mac=reference).verdict, "unknown")

    def test_a_name_that_no_longer_resolves_is_unknown_even_with_the_same_router(self) -> None:
        # A device saved by its .local name resolves to nothing while it is
        # off, and also while it is up on a network that drops multicast: the
        # router alone must not tell a working device to be unplugged.
        for name, now, reference in (
            ("same router", place(("en0", "192.168.1.0/24", HOME)), HOME),
            ("other router", place(("en0", "192.168.1.0/24", HOME)), NEIGHBOUR),
            ("no reference", place(("en0", "192.168.1.0/24", HOME)), None),
            ("router unreadable", place(("en0", "192.168.1.0/24", None)), HOME),
        ):
            with self.subTest(name):
                result = compare_place(now, [], router_mac=reference)
                self.assertEqual((result.verdict, result.evidence), ("unknown", "unresolved"))
        # Offline still wins: no address at all says more than the name.
        self.assertEqual(compare_place(place(), [], router_mac=HOME).verdict, "offline")

    def test_nothing_to_compare_is_unknown(self) -> None:
        # An unreadable place, another family only.
        self.assertEqual(compare_place(None, DEVICE, router_mac=HOME).verdict, "unknown")
        v6 = NetworkPlace((LocalInterfaceNetwork("en0", "2001:db8::2", ipaddress.ip_network("2001:db8::/64")),), ())
        self.assertEqual(compare_place(v6, DEVICE, router_mac=HOME).verdict, "unknown")

    def test_a_router_mode_device_takes_the_router_away(self) -> None:
        # The device is the router: while it is down nothing answers for it.
        for name, now, verdict in (
            ("unresolved", place(("en0", "192.168.1.0/24", None)), "same"),
            ("gone", place(("en0", "192.168.1.0/24", "none")), "same"),
            ("back", place(("en0", "192.168.1.0/24", HOME)), "same"),
            ("another", place(("en0", "192.168.1.0/24", NEIGHBOUR)), "moved"),
        ):
            with self.subTest(name):
                result = compare_place(now, DEVICE, router_mac=HOME, device_is_router=True)
                self.assertEqual(result.verdict, verdict)
        # Without the device's own hardware address from before the restart, a
        # router that answers now may be the device back or another one.
        unread = compare_place(place(("en0", "192.168.1.0/24", NEIGHBOUR)), DEVICE, router_mac=None, device_is_router=True)
        self.assertEqual(unread.verdict, "unknown")
        still_down = compare_place(place(("en0", "192.168.1.0/24", None)), DEVICE, router_mac=None, device_is_router=True)
        self.assertEqual(still_down.verdict, "same")

    def test_the_router_toward_the_device_is_the_one_on_its_network_not_a_vpns(self) -> None:
        now = NetworkPlace(
            (
                LocalInterfaceNetwork("en0", "192.168.1.20", ipaddress.ip_network("192.168.1.0/24")),
                LocalInterfaceNetwork("en5", "10.8.0.2", ipaddress.ip_network("10.8.0.0/24")),
            ),
            (Router("en5", "10.8.0.1", NEIGHBOUR), Router("en0", "192.168.1.1", HOME)),
        )

        self.assertEqual(now.router_toward(DEVICE), Router("en0", "192.168.1.1", HOME))

    def test_reading_the_place_leaves_out_vpn_and_virtual_routers(self) -> None:
        networks = (LocalInterfaceNetwork("en0", "192.168.1.20", ipaddress.ip_network("192.168.1.0/24")),)
        routers = (Router("en0", "192.168.1.1", HOME), Router("utun4", "100.64.0.1", NEIGHBOUR),
                   Router("bridge100", "192.168.64.1", NEIGHBOUR))

        result = read_network_place(networks=lambda: networks, routers=lambda: routers)

        self.assertEqual(result, NetworkPlace(networks, (Router("en0", "192.168.1.1", HOME),)))

    def test_an_unreadable_interface_list_is_no_place(self) -> None:
        def broken():
            raise OSError("ifaddr failed")

        self.assertIsNone(read_network_place(networks=broken, routers=lambda: ()))


class HeldTerminalTests(unittest.TestCase):
    """macOS's Local Network access holds apps and every terminal app but
    Terminal: such a process's connects get no route and its .local lookups
    fail while the device is up, so outside the app they are not silence."""

    ITERM = {"TERM_PROGRAM": "iTerm.app"}

    def test_which_processes_macos_may_hold(self) -> None:
        cases = (
            ("iTerm2", "darwin", self.ITERM, False, True),
            ("Visual Studio Code", "darwin", {"TERM_PROGRAM": "vscode"}, False, True),
            ("no terminal at all", "darwin", {}, False, True),
            ("Terminal", "darwin", {"TERM_PROGRAM": "Apple_Terminal"}, False, False),
            ("an SSH session", "darwin", {**self.ITERM, "SSH_CONNECTION": "10.0.0.9 5 10.0.0.20 22"}, False, False),
            ("the app", "darwin", {"TERM_PROGRAM": "Apple_Terminal"}, True, True),
            ("Linux", "linux", self.ITERM, False, False),
        )
        for name, platform, environ, in_app, held in cases:
            with self.subTest(name), mock.patch("timecapsulesmb.checks.network.running_in_app", return_value=in_app):
                self.assertEqual(macos_may_hold_this_process(platform=platform, environ=environ), held)

    def test_only_what_a_held_terminal_gets_is_unproven_outside_the_app(self) -> None:
        cases = (
            ("no route", ["[Errno 65] No route to host"], True),
            ("an unresolved name", ["[Errno 8] nodename nor servname provided, or not known"], True),
            ("one address of several with no route", ["timed out; [Errno 65] No route to host"], True),
            ("timeouts", ["timed out"], False),
            ("host down", ["[Errno 64] Host is down"], False),
            ("no errors", [None], False),
        )
        for name, errors, unproven in cases:
            with self.subTest(name), mock.patch("timecapsulesmb.checks.network.running_in_app", return_value=False):
                self.assertEqual(terminal_may_be_held(errors, platform="darwin", environ=self.ITERM), unproven)
                # Terminal is not held, and Linux holds nothing.
                self.assertFalse(terminal_may_be_held(errors, platform="darwin", environ={"TERM_PROGRAM": "Apple_Terminal"}))
                self.assertFalse(terminal_may_be_held(errors, platform="linux", environ=self.ITERM))
        # The app proves or rules out its own block before judging silence.
        with mock.patch("timecapsulesmb.checks.network.running_in_app", return_value=True):
            self.assertFalse(terminal_may_be_held(["[Errno 65] No route to host"], platform="darwin", environ=self.ITERM))

    def test_a_closed_port_seen_from_a_held_terminal_is_not_silent(self) -> None:
        closed = ClosedSshPort("10.0.0.2", "[Errno 65] No route to host")
        with mock.patch("timecapsulesmb.checks.network.macos_may_hold_this_process", return_value=True):
            self.assertFalse(closed.silent)
            self.assertTrue(ClosedSshPort("10.0.0.2", "timed out").silent)
        with mock.patch("timecapsulesmb.checks.network.macos_may_hold_this_process", return_value=False):
            self.assertTrue(closed.silent)


if __name__ == "__main__":
    unittest.main()
