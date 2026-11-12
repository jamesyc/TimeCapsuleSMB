from __future__ import annotations

import sys
import unittest
import errno
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from timecapsulesmb.transport.local import (
    command_exists,
    connect_answered,
    connect_unanswered,
    Router,
    lan_routers,
    mac_network_filters,
    run_local_capture,
    scoped_tcp_connect_errors,
    tcp_connect_error,
)


class TcpConnectErrorTests(unittest.TestCase):
    """tcp_connect_error with fake sockets: each address's connect is scripted."""

    def run_connect(self, host: str, addresses: list[tuple[int, tuple]], outcomes: dict[str, object]):
        import socket
        import threading

        released = threading.Event()
        self.addCleanup(released.set)
        connected: list[str] = []

        class FakeSocket:
            def __init__(self, family, socktype, proto):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def settimeout(self, timeout):
                pass

            def connect(self, sockaddr):
                connected.append(sockaddr[0])
                outcome = outcomes[sockaddr[0]]
                if outcome == "blackhole":
                    released.wait(10)
                    raise TimeoutError("timed out")
                if isinstance(outcome, BaseException):
                    raise outcome

        infos = [(family, socket.SOCK_STREAM, 6, "", sockaddr) for family, sockaddr in addresses]
        with mock.patch("timecapsulesmb.transport.local.socket.getaddrinfo", return_value=infos), \
                mock.patch("timecapsulesmb.transport.local.socket.socket", FakeSocket):
            result = tcp_connect_error(host, 5009, timeout=10)
        return result, connected

    def test_an_address_is_connected_once(self) -> None:
        import socket

        result, connected = self.run_connect("10.0.0.2", [(socket.AF_INET, ("10.0.0.2", 5009))], {"10.0.0.2": None})

        self.assertIsNone(result)
        self.assertEqual(connected, ["10.0.0.2"])

    def test_a_name_answers_from_any_address_without_waiting_for_a_blackholed_one(self) -> None:
        # AirPort-Time-Capsule.local with its IPv4 address off this Mac's network.
        import socket
        import time

        start = time.monotonic()
        result, connected = self.run_connect(
            "AirPort-Time-Capsule.local",
            [(socket.AF_INET, ("192.168.1.218", 5009)), (socket.AF_INET6, ("fe80::1", 5009, 0, 4))],
            {"192.168.1.218": "blackhole", "fe80::1": None},
        )

        self.assertIsNone(result)
        self.assertLess(time.monotonic() - start, 5)
        self.assertEqual(sorted(connected), ["192.168.1.218", "fe80::1"])

    def test_a_name_reports_each_distinct_error_in_address_order(self) -> None:
        import socket

        refused = ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused")
        unreachable = OSError(errno.EHOSTUNREACH, "No route to host")
        result, _connected = self.run_connect(
            "capsule.local",
            [(socket.AF_INET, ("10.0.0.2", 5009)), (socket.AF_INET, ("10.0.0.3", 5009)), (socket.AF_INET6, ("fe80::1", 5009, 0, 4))],
            {"10.0.0.2": refused, "10.0.0.3": refused, "fe80::1": unreachable},
        )

        self.assertEqual(result, f"{refused}; {unreachable}")

    def test_a_failed_lookup_is_the_error(self) -> None:
        import socket

        with mock.patch("timecapsulesmb.transport.local.socket.getaddrinfo", side_effect=socket.gaierror(8, "nodename nor servname provided")):
            self.assertEqual(tcp_connect_error("missing.local", 22), "[Errno 8] nodename nor servname provided")


class ConnectAnswerTests(unittest.TestCase):
    """How a host answered tcp_connect_error, whose result joins each
    address's error with "; " (TcpConnectErrorTests)."""

    def test_a_connect_or_a_refusal_at_any_address_is_an_answer(self) -> None:
        cases = {
            "connected": None,
            "refused": "[Errno 61] Connection refused",
            "IPv4 refused, IPv6 timed out": "[Errno 61] Connection refused; timed out",
            "IPv4 timed out, IPv6 refused": "timed out; [Errno 61] Connection refused",
            "IPv4 no route, IPv6 refused": "[Errno 65] No route to host; [Errno 61] Connection refused",
        }
        for case, error in cases.items():
            with self.subTest(case=case):
                self.assertTrue(connect_answered(error))

    def test_drops_and_routing_errors_are_no_answer(self) -> None:
        for error in ("timed out", "[Errno 64] Host is down", "[Errno 65] No route to host",
                      "timed out; [Errno 65] No route to host", "connection failed", ""):
            with self.subTest(error=error):
                self.assertFalse(connect_answered(error))

    def test_a_timeout_refused_at_no_address_is_unanswered(self) -> None:
        # A full listen queue drops the connect; the host's other address may
        # have no route to it at all.
        for error in ("timed out", "timed out; [Errno 65] No route to host", "[Errno 64] Host is down; timed out"):
            with self.subTest(error=error):
                self.assertTrue(connect_unanswered(error))

    def test_a_connect_a_refusal_or_no_timeout_is_not_unanswered(self) -> None:
        # A refusal at any address means nothing listens there, not a listener
        # that takes no connections.
        cases = (
            None,
            "[Errno 61] Connection refused",
            "timed out; [Errno 61] Connection refused",
            "[Errno 65] No route to host",
            "[Errno 64] Host is down",
            "connection failed",
        )
        for error in cases:
            with self.subTest(error=error):
                self.assertFalse(connect_unanswered(error))


class LocalTransportTests(unittest.TestCase):
    def test_scoped_connections_share_a_budget_and_cleanup_every_socket(self) -> None:
        for mode in ("success", "timeout", "gone"):
            with self.subTest(mode=mode):
                clock = [100.0]
                pending = {}
                selector = mock.MagicMock()
                selector.__enter__.return_value = selector
                selector.get_map.side_effect = lambda: pending
                selector.register.side_effect = lambda sock, _event, host: pending.setdefault(sock, SimpleNamespace(fileobj=sock, data=host))
                selector.unregister.side_effect = pending.pop
                first, second = mock.Mock(), mock.Mock()
                first.connect_ex.return_value = errno.EINPROGRESS
                second.connect_ex.return_value = errno.EINPROGRESS
                second.getsockopt.return_value = 0
                if mode == "gone":
                    second.getsockopt.side_effect = OSError("interface disappeared")

                def select(timeout):
                    self.assertLessEqual(timeout, 2.0)
                    if mode != "timeout" and second in pending:
                        clock[0] += 0.25
                        return [(pending[second], 2)]
                    clock[0] += timeout
                    return []

                selector.select.side_effect = select
                with (
                    mock.patch("timecapsulesmb.transport.local.socket.socket", side_effect=[first, second]),
                    mock.patch("timecapsulesmb.transport.local.selectors.DefaultSelector", return_value=selector),
                    mock.patch("timecapsulesmb.transport.local.time.monotonic", side_effect=lambda: clock[0]),
                ):
                    result = scoped_tcp_connect_errors(["fe80::40%17", "fe80::40%18"], 445)
                first.connect_ex.assert_called_once_with(("fe80::40", 445, 0, 17))
                second.connect_ex.assert_called_once_with(("fe80::40", 445, 0, 18))
                self.assertEqual(result["fe80::40%17"], "connection timed out")
                self.assertEqual(result["fe80::40%18"], {"success": None, "timeout": "connection timed out", "gone": "interface disappeared"}[mode])
                self.assertLessEqual(clock[0], 102.0)
                first.close.assert_called_once()
                second.close.assert_called_once()

    def test_scoped_connections_handle_immediate_results_and_bad_scopes(self) -> None:
        for code, succeeds in ((0, True), (errno.ECONNREFUSED, False)):
            with self.subTest(code=code):
                sock = mock.Mock()
                sock.connect_ex.return_value = code
                with mock.patch("timecapsulesmb.transport.local.socket.socket", return_value=sock):
                    result = scoped_tcp_connect_errors(["fe80::40%17", "fe80::40%17", "fe80::40%0"], 445)
                self.assertEqual(result["fe80::40%17"] is None, succeeds)
                self.assertIn("no usable", result["fe80::40%0"])
                sock.connect_ex.assert_called_once()
                sock.close.assert_called_once()

    def test_run_local_capture_returns_stdout(self) -> None:
        proc = run_local_capture(["/bin/sh", "-c", "printf 'ok'"])
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "ok")

    def test_command_exists_follows_the_spawn_lookup(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            tool = Path(tmp) / "tc-test-tool"
            tool.write_text("#!/bin/sh\n")
            tool.chmod(0o755)
            with mock.patch.dict("os.environ", {"PATH": tmp}):
                self.assertTrue(command_exists("tc-test-tool"))
                self.assertFalse(command_exists("tc-no-such-tool"))


# Captured from macOS 26 with LuLu installed, plus an endpoint security
# extension in another category, which cannot filter connections.
SYSTEM_EXTENSIONS_OUTPUT = (
    "2 extension(s)\n"
    "--- com.apple.system_extension.endpoint_security\n"
    "enabled\tactive\tteamID\tbundleID (version)\tname\t[state]\n"
    "*\t*\tX9E956P446\tcom.crowdstrike.falcon.Agent (7.10/7.10)\tFalcon\t[activated enabled]\n"
    "--- com.apple.system_extension.network_extension (Go to 'System Settings > General > Login Items & "
    "Extensions > Network Extensions' to modify these system extension(s))\n"
    "enabled\tactive\tteamID\tbundleID (version)\tname\t[state]\n"
    "\t*\tVBG97UB4TA\tcom.objective-see.lulu.extension (4.5.1/4.5.1)\tLuLu\t[activated waiting for user]\n"
)
VPN_SERVICES_OUTPUT = (
    "Available network connection services in the current set (*=enabled):\n"
    '* (Connected)      EFF6456D-DF2E-4EE4-BEEA-29AC1F2DEA06 VPN (io.tailscale.ipn.macos) "Tailscale"'
    "                      [VPN:io.tailscale.ipn.macos]\n"
    '  (Disconnected)   0A1B2C3D-0000-4000-8000-000000000001 IPSec               "Acme Corp office"'
    "                [IPSec]\n"
)


class MacNetworkFiltersTests(unittest.TestCase):
    def run_with(self, outputs: dict[str, object]) -> tuple[dict[str, object], mock.Mock]:
        def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            result = outputs[command[0]]
            if isinstance(result, BaseException):
                raise result
            returncode, stdout = result if isinstance(result, tuple) else (0, result)
            return subprocess.CompletedProcess(command, returncode, stdout=stdout, stderr="")

        runner = mock.Mock(side_effect=run)
        return mac_network_filters(platform="darwin", run=runner), runner

    def test_names_network_extensions_and_vpn_services_without_user_chosen_names(self) -> None:
        fields, runner = self.run_with({
            "/usr/bin/systemextensionsctl": SYSTEM_EXTENSIONS_OUTPUT,
            "/usr/sbin/scutil": VPN_SERVICES_OUTPUT,
        })

        self.assertEqual(fields, {
            "mac_network_extensions": ["com.objective-see.lulu.extension [activated waiting for user]"],
            "mac_vpn_services": ["(Connected) VPN (io.tailscale.ipn.macos)", "(Disconnected) IPSec"],
        })
        self.assertNotIn("Acme", repr(fields))
        self.assertNotIn("EFF6456D", repr(fields))
        commands = [call.args[0] for call in runner.call_args_list]
        self.assertEqual(commands, [["/usr/bin/systemextensionsctl", "list"], ["/usr/sbin/scutil", "--nc", "list"]])
        for call in runner.call_args_list:
            self.assertEqual(call.kwargs["timeout"], 5)
            self.assertIs(call.kwargs["stdin"], subprocess.DEVNULL)

    def test_a_mac_with_no_extensions_or_vpns_reports_empty_lists(self) -> None:
        fields, _runner = self.run_with({
            "/usr/bin/systemextensionsctl": "0 extension(s)\n",
            "/usr/sbin/scutil": "Available network connection services in the current set (*=enabled):\n",
        })

        self.assertEqual(fields, {"mac_network_extensions": [], "mac_vpn_services": []})

    def test_a_failed_command_is_recorded_and_the_other_still_collected(self) -> None:
        cases = (
            (FileNotFoundError(2, "No such file"), "/usr/bin/systemextensionsctl: FileNotFoundError"),
            (subprocess.TimeoutExpired(["systemextensionsctl"], 5), "/usr/bin/systemextensionsctl: TimeoutExpired"),
            ((1, ""), "/usr/bin/systemextensionsctl: rc=1"),
        )
        for failure, error in cases:
            with self.subTest(error=error):
                fields, _runner = self.run_with({
                    "/usr/bin/systemextensionsctl": failure,
                    "/usr/sbin/scutil": VPN_SERVICES_OUTPUT,
                })

                self.assertEqual(fields["mac_network_filters_error"], error)
                self.assertNotIn("mac_network_extensions", fields)
                self.assertEqual(len(fields["mac_vpn_services"]), 2)

    def test_other_platforms_report_nothing_and_run_nothing(self) -> None:
        runner = mock.Mock()

        self.assertEqual(mac_network_filters(platform="linux", run=runner), {})
        runner.assert_not_called()


# `route -n get default` on macOS, wired to a router and on a VPN without a
# gateway of its own.
NETSTAT_OUTPUT = """Routing tables

Internet:
Destination        Gateway            Flags               Netif Expire
default            192.168.1.1        UGScg                 en0
default            192.168.1.1        UGScIg                en7
default            10.0.0.1           UGScIg                en1
default            link#27            UCSIg               utun4
default            link#44            UCSIg           bridge100      !
127                127.0.0.1          UCS                   lo0
192.168.1          link#14            UCS                   en0      !
"""
ARP_OUTPUT = """? (169.254.169.254) at (incomplete) on en0 [ethernet]
? (192.168.1.1) at 84:78:48:9e:4:51 on en0 ifscope [ethernet]
? (192.168.1.1) at 84:78:48:9e:4:51 on en7 ifscope [ethernet]
? (192.168.1.20) at 0:1b:63:aa:bb:cc on en0 ifscope [ethernet]
"""
LINUX_ROUTE = """Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT
wlan0\t00000000\t0101A8C0\t0003\t0\t0\t600\t00000000\t0\t0\t0
eth0\t00000000\t0100000A\t0003\t0\t0\t100\t00000000\t0\t0\t0
wlan0\t0001A8C0\t00000000\t0001\t0\t0\t600\t00FFFFFF\t0\t0\t0
tun0\t00000000\t00000000\t0001\t0\t0\t50\t00000000\t0\t0\t0
"""
LINUX_ARP = """IP address       HW type     Flags       HW address            Mask     Device
192.168.1.1      0x1         0x2         84:78:48:9e:04:51     *        wlan0
10.0.0.1         0x1         0x0         00:00:00:00:00:00     *        eth0
"""


class LanRoutersTests(unittest.TestCase):
    """This computer's default router on each interface, with its hardware
    address: what tells another Wi-Fi with the same addresses from this one."""

    def darwin(self, outputs: dict[str, object], **kwargs):
        def run(command: list[str], **run_kwargs: object) -> subprocess.CompletedProcess[str]:
            result = outputs[command[0]]
            if callable(result):
                result = result()
            if isinstance(result, BaseException):
                raise result
            returncode, stdout = result if isinstance(result, tuple) else (0, result)
            return subprocess.CompletedProcess(command, returncode, stdout=stdout, stderr="")

        runner = mock.Mock(side_effect=run)
        primed: list[str] = []
        routers = lan_routers(platform="darwin", run=runner, prime=primed.append, sleep=lambda _s: None, **kwargs)
        return routers, runner, primed

    def test_an_older_netstat_layout_finds_the_interface_by_its_header(self) -> None:
        # macOS before 10.13 printed Refs and Use before Netif: the fourth
        # column is then a count, which would match no interface.
        old_layout = (
            "Routing tables\n\nInternet:\n"
            "Destination        Gateway            Flags        Refs      Use   Netif Expire\n"
            "default            192.168.1.1        UGSc           12        0     en0\n"
            "127                127.0.0.1          UCS             0        0     lo0\n"
        )
        routers, _runner, _primed = self.darwin({"/usr/sbin/netstat": old_layout, "/usr/sbin/arp": ARP_OUTPUT})

        self.assertEqual(routers, (Router("en0", "192.168.1.1", "84:78:48:9e:04:51"),))

    def test_reads_every_default_router_and_its_hardware_address(self) -> None:
        routers, runner, primed = self.darwin({"/usr/sbin/netstat": NETSTAT_OUTPUT, "/usr/sbin/arp": ARP_OUTPUT})

        self.assertEqual(routers, (
            Router("en0", "192.168.1.1", "84:78:48:9e:04:51"),
            Router("en7", "192.168.1.1", "84:78:48:9e:04:51"),
            # Not in the table, even after one datagram asked for it.
            Router("en1", "10.0.0.1", None),
        ))
        self.assertEqual(primed, ["10.0.0.1"])
        commands = [call.args[0] for call in runner.call_args_list]
        self.assertEqual(commands, [
            ["/usr/sbin/netstat", "-rn", "-f", "inet"], ["/usr/sbin/arp", "-an"], ["/usr/sbin/arp", "-an"],
        ])
        self.assertTrue(all(call.kwargs["stdin"] is subprocess.DEVNULL for call in runner.call_args_list))
        self.assertTrue(all(call.kwargs["timeout"] == 5 for call in runner.call_args_list))

    def test_a_router_resolved_after_the_datagram_is_read_again(self) -> None:
        tables = iter(("", "? (10.0.0.1) at a:b:c:d:e:f on en1 ifscope [ethernet]\n"))
        routes = "default            10.0.0.1           UGScg                 en1\n"
        routers, _runner, primed = self.darwin({"/usr/sbin/netstat": routes, "/usr/sbin/arp": lambda: next(tables)})

        self.assertEqual(routers, (Router("en1", "10.0.0.1", "0a:0b:0c:0d:0e:0f"),))
        self.assertEqual(primed, ["10.0.0.1"])

    def test_resolved_routers_are_not_sent_anything(self) -> None:
        routes = "default            192.168.1.1        UGScg                 en0\n"
        _routers, runner, primed = self.darwin({"/usr/sbin/netstat": routes, "/usr/sbin/arp": ARP_OUTPUT})

        self.assertEqual(primed, [])
        self.assertEqual(runner.call_count, 2)

    def test_no_route_or_a_failed_command_is_none(self) -> None:
        cases = (
            ("tunnel only", {"/usr/sbin/netstat": "default            link#27            UCSIg               utun4\n"}),
            ("netstat failed", {"/usr/sbin/netstat": (1, NETSTAT_OUTPUT)}),
            ("netstat missing", {"/usr/sbin/netstat": FileNotFoundError(2, "No such file")}),
            ("netstat timed out", {"/usr/sbin/netstat": subprocess.TimeoutExpired(["netstat"], 5)}),
        )
        for name, outputs in cases:
            with self.subTest(name):
                self.assertEqual(self.darwin(outputs)[0], ())

    def test_an_unreadable_neighbour_table_leaves_the_hardware_addresses_unknown(self) -> None:
        routes = "default            192.168.1.1        UGScg                 en0\n"
        routers, _runner, _primed = self.darwin(
            {"/usr/sbin/netstat": routes, "/usr/sbin/arp": subprocess.TimeoutExpired(["arp"], 5)},
        )

        self.assertEqual(routers, (Router("en0", "192.168.1.1", None),))

    def test_linux_reads_proc(self) -> None:
        files = {"/proc/net/route": LINUX_ROUTE, "/proc/net/arp": LINUX_ARP}
        primed: list[str] = []
        runner = mock.Mock()
        routers = lan_routers(
            platform="linux", run=runner, read_text=files.__getitem__, prime=primed.append, sleep=lambda _s: None,
        )

        self.assertEqual(routers, (
            Router("wlan0", "192.168.1.1", "84:78:48:9e:04:51"),
            # An incomplete entry (flags 0x0, all-zero address) is no address.
            Router("eth0", "10.0.0.1", None),
        ))
        self.assertEqual(primed, ["10.0.0.1"])
        runner.assert_not_called()

    def test_linux_without_proc_is_none(self) -> None:
        def missing(path: str) -> str:
            raise FileNotFoundError(2, "No such file", path)

        self.assertEqual(lan_routers(platform="linux", read_text=missing, prime=lambda _a: None), ())

    def test_other_systems_read_nothing(self) -> None:
        runner = mock.Mock()
        read = mock.Mock()

        self.assertEqual(lan_routers(platform="win32", run=runner, read_text=read), ())
        runner.assert_not_called()
        read.assert_not_called()


if __name__ == "__main__":
    unittest.main()
