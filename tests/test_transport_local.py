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


if __name__ == "__main__":
    unittest.main()
