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

from timecapsulesmb.transport.local import mac_network_filters, run_local_capture, scoped_tcp_connect_errors


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
