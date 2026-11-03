"""Errno 65 (No route to host) from this program's own connections has
several causes on macOS, and each gets its own error (checks.network):

- a Local Network block: macOS refuses this program's connections to a device
  that /usr/bin/ssh reaches, and one more connect to port 22 gets no route;
- a saved link-local address on an interface this computer no longer has;
- a device that is off or gone, or a block nothing could prove, which keeps
  the "not answering" error.
"""
from __future__ import annotations

import ipaddress
import os
import unittest
from unittest import mock

from timecapsulesmb.checks import network
from timecapsulesmb.core.install import APP_CLIENT
from timecapsulesmb.checks.network import (
    LinkLocalInterfaceGone,
    LocalInterfaceNetwork,
    LocalNetworkBlock,
    check_ssh_login,
    link_local_interface_gone,
    local_network_block,
    local_network_block_possible,
    ssh_reached_device,
)
from timecapsulesmb.core.error_codes import ErrorCode
from timecapsulesmb.device.probe import AirportAcpReading, ProbeResult, SshAccessStatus, SshCommandProbeResult
from timecapsulesmb.integrations.acp import ACPConnectionError
from timecapsulesmb.services import runtime as service_runtime
from timecapsulesmb.services.acp_ssh import ACPLinkLocalInterfaceGoneError, ACPNoRouteError, enable_ssh_with_port_preflight
from timecapsulesmb.services.callbacks import OperationCallbacks
from timecapsulesmb.services.locate import LocateResult
from timecapsulesmb.transport.errors import (
    SshAlgorithmNegotiationError,
    SshAuthenticationError,
    SshCommandTimeout,
    SshNetworkError,
)
from timecapsulesmb.transport.ssh import SshConnection
from timecapsulesmb.device.acpd import RestartVerdict

NO_ROUTE = "[Errno 65] No route to host"
ACP_NO_ROUTE_READ = f"Could not connect to ACP on 10.0.0.2:5009: {NO_ROUTE}"
ON_THE_DEVICES_NETWORK = (LocalInterfaceNetwork("en0", "10.0.0.50", ipaddress.ip_network("10.0.0.0/24")),)
ON_ANOTHER_NETWORK = (LocalInterfaceNetwork("en0", "192.168.1.50", ipaddress.ip_network("192.168.1.0/24")),)


def ssh_refused() -> SshCommandProbeResult:
    """ssh against a device with SSH off: its TCP stack refuses the port."""
    error = SshNetworkError("ssh: connect to host 10.0.0.2 port 22: Connection refused")
    return SshCommandProbeResult(ok=False, detail=str(error), error=error)


def ssh_no_route() -> SshCommandProbeResult:
    error = SshNetworkError("ssh: connect to host 10.0.0.2 port 22: No route to host")
    return SshCommandProbeResult(ok=False, detail=str(error), error=error)


class LinkLocalInterfaceGoneTests(unittest.TestCase):
    """A saved fe80:: address names an interface: an unplugged USB Ethernet
    adapter takes it away, and its connects then get no route."""

    def gone(self, host: str, *, scope_index: int | None, addresses: tuple[str, ...]) -> str | None:
        with mock.patch("timecapsulesmb.checks.network.ipv6_scope_index", return_value=scope_index):
            return link_local_interface_gone(host, addresses)

    def test_an_interface_this_computer_no_longer_has_is_named(self) -> None:
        self.assertEqual(self.gone("root@fe80::f299:bfff:fe08:fb07%en8", scope_index=None, addresses=("10.0.0.50",)), "en8")

    def test_an_interface_without_an_ipv6_link_local_address_is_gone_too(self) -> None:
        # It is listed (a bridge with nothing behind it) but has no fe80 address.
        addresses = ("192.168.1.50", "fe80::1%en0")
        self.assertEqual(self.gone("fe80::f299:bfff:fe08:fb07%en8", scope_index=12, addresses=addresses), "en8")

    def test_an_interface_still_on_the_link_is_not_gone(self) -> None:
        addresses = ("192.168.1.50", "fe80::aede:48ff:fe00:1122%en8")
        self.assertIsNone(self.gone("[fe80::f299:bfff:fe08:fb07%en8]", scope_index=12, addresses=addresses))

    def test_unreadable_interfaces_say_nothing(self) -> None:
        self.assertIsNone(self.gone("fe80::f299:bfff:fe08:fb07%en8", scope_index=12, addresses=()))

    def test_other_addresses_are_never_gone(self) -> None:
        for host in ("192.168.1.10", "root@10.0.0.2", "fd00::2", "fe80::1", "Office-TC.local"):
            with self.subTest(host=host):
                self.assertIsNone(self.gone(host, scope_index=None, addresses=()))

    def test_a_numeric_scope_names_its_interface(self) -> None:
        with mock.patch("timecapsulesmb.checks.network.socket.if_indextoname", side_effect=OSError("no such index")):
            self.assertEqual(link_local_interface_gone("fe80::1%9", ()), "9")
        with mock.patch("timecapsulesmb.checks.network.socket.if_indextoname", return_value="en8"), \
                mock.patch("timecapsulesmb.checks.network.ipv6_scope_index", return_value=9):
            self.assertIsNone(link_local_interface_gone("fe80::1%9", ("fe80::2%en8",)))

    def test_its_words_name_the_address_and_the_interface(self) -> None:
        gone = LinkLocalInterfaceGone("fe80::1%en8", "en8", platform="darwin")
        self.assertEqual(gone.code, "link_local_interface_gone")
        self.assertEqual(gone.message, "The address fe80::1%en8 is on a network connection this Mac no longer has (en8).")
        self.assertIn("this computer", LinkLocalInterfaceGone("fe80::1%en8", "en8", platform="linux").message)


class SshReachedDeviceTests(unittest.TestCase):
    """ssh is independent proof the device is up only when it reached its TCP stack."""

    def test_answers_from_the_device_count(self) -> None:
        for result in (
            SshCommandProbeResult(ok=True, detail="ok"),
            SshCommandProbeResult(ok=False, detail="rc=1"),
            SshCommandProbeResult(ok=False, detail="denied", error=SshAuthenticationError("Permission denied")),
            SshCommandProbeResult(ok=False, detail="kex", error=SshAlgorithmNegotiationError(
                "no matching key exchange", algorithm="kex", offered=("diffie-hellman-group1-sha1",),
            )),
            ssh_refused(),
        ):
            with self.subTest(detail=result.detail):
                self.assertTrue(ssh_reached_device(result))

    def test_no_answer_does_not_count(self) -> None:
        for result in (
            ssh_no_route(),
            SshCommandProbeResult(ok=False, detail="t", error=SshNetworkError("ssh: connect to host x port 22: Operation timed out")),
            SshCommandProbeResult(ok=False, detail="t", error=SshCommandTimeout("Timed out waiting for ssh command")),
        ):
            with self.subTest(detail=result.detail):
                self.assertFalse(ssh_reached_device(result))


class LocalNetworkBlockTests(unittest.TestCase):
    def block(self, errors, *, proof="ssh", platform="darwin", retry=NO_ROUTE, networks=ON_THE_DEVICES_NETWORK):
        self.retry = mock.Mock(return_value=retry)
        return local_network_block(
            "10.0.0.2", errors, proof=proof, platform=platform, connect_error=self.retry, networks=networks,
        )

    @mock.patch.dict(os.environ, {"TCAPSULE_CLIENT": APP_CLIENT})
    def test_no_route_everywhere_with_proof_is_a_block(self) -> None:
        block = self.block((NO_ROUTE, ACP_NO_ROUTE_READ))
        self.assertEqual(block.code, "acp_no_route")
        self.assertEqual(
            block.message,
            "macOS refuses TimeCapsuleSMB's connections to the device, although the device is on the network "
            "(SSH reaches it).",
        )
        self.assertEqual(
            block.detail,
            f"No route to 10.0.0.2 from this Mac ({NO_ROUTE}; {ACP_NO_ROUTE_READ}), while SSH reaches it.",
        )
        self.assertEqual(block.details["local_network_block_proof"], "ssh")
        # One more connect to SSH's port, which also got no route.
        self.retry.assert_called_once_with("10.0.0.2", 22, network.PORT_CHECK_TIMEOUT_SECONDS)

    def test_the_app_is_named_for_the_app_and_the_terminal_app_for_a_command(self) -> None:
        # macOS holds the app responsible for its helper's connections, and
        # the terminal app for a command's.
        with mock.patch.dict(os.environ, {"TCAPSULE_CLIENT": APP_CLIENT}):
            app = LocalNetworkBlock("10.0.0.2", "ssh", (NO_ROUTE,))
            self.assertNotIn("Terminal", app.message + app.line)
            self.assertTrue(app.line.endswith("then quit and reopen TimeCapsuleSMB"))
        for environment in ({"TCAPSULE_CLIENT": "terminal"}, {"TCAPSULE_CLIENT": "homebrew"}, {}):
            with self.subTest(environment=environment), mock.patch.dict(os.environ, environment, clear=True):
                command = LocalNetworkBlock("10.0.0.2", "ssh", (NO_ROUTE,))
                self.assertTrue(command.message.startswith("macOS refuses this command's connections to the device"))
                self.assertIn("the app tcapsule runs in, such as Terminal", command.message)
                self.assertIn("Turn that app off and on in System Settings > Privacy & Security > Local Network",
                              command.line)

    def test_each_proof_is_named(self) -> None:
        for proof, words in (("ssh", "SSH reaches it"), ("bonjour", "Bonjour finds it")):
            with self.subTest(proof=proof):
                self.assertIn(words, self.block((NO_ROUTE,), proof=proof).message)

    def test_no_route_alone_is_never_a_block(self) -> None:
        # A device that is off or gone gets no route too.
        self.assertIsNone(self.block((NO_ROUTE,), proof=None))
        self.retry.assert_not_called()

    def test_only_macos_has_the_block(self) -> None:
        # Linux reports a failed address lookup on the link as no route.
        self.assertIsNone(self.block((NO_ROUTE,), platform="linux"))

    def test_any_other_failure_is_not_a_block(self) -> None:
        for errors in (("timed out",), (NO_ROUTE, "timed out"), ("[Errno 64] Host is down",), ("Connection refused",), ()):
            with self.subTest(errors=errors):
                self.assertIsNone(self.block(errors))

    def test_a_connect_that_gets_through_on_the_last_try_was_a_passing_fault(self) -> None:
        for retry in (None, "timed out", "[Errno 61] Connection refused"):
            with self.subTest(retry=retry):
                self.assertIsNone(self.block((NO_ROUTE,), retry=retry))

    def test_an_address_on_another_network_is_never_a_block(self) -> None:
        self.assertIsNone(self.block((NO_ROUTE,), networks=ON_ANOTHER_NETWORK))
        self.retry.assert_not_called()


class LocalNetworkBlockPossibleTests(unittest.TestCase):
    """What can be ruled out before a proof that costs time (ssh) is asked."""

    def possible(self, errors=(NO_ROUTE,), *, platform="darwin", networks=ON_THE_DEVICES_NETWORK) -> bool:
        return local_network_block_possible("10.0.0.2", errors, platform=platform, networks=networks)

    def test_no_route_on_macos_to_an_address_on_this_network_is_possible(self) -> None:
        self.assertTrue(self.possible())
        self.assertTrue(self.possible((NO_ROUTE, None, ACP_NO_ROUTE_READ)))

    def test_an_address_whose_network_is_unknown_is_still_possible(self) -> None:
        # Unreadable interfaces do not make the address another network's.
        self.assertTrue(self.possible(networks=()))

    def test_linux_another_network_or_another_failure_is_not(self) -> None:
        self.assertFalse(self.possible(platform="linux"))
        self.assertFalse(self.possible(networks=ON_ANOTHER_NETWORK))
        for errors in ((), (None,), ("timed out",), (NO_ROUTE, "[Errno 64] Host is down")):
            with self.subTest(errors=errors):
                self.assertFalse(self.possible(errors))


class ProbeFailureBlockTests(unittest.TestCase):
    """Deploy, Activate and the maintenance commands: a closed SSH port when
    the probe did not log in (services.runtime.probe_failure_error)."""

    CONNECTION = SshConnection("root@10.0.0.2", "pw", "")
    CLOSED = ProbeResult(SshAccessStatus.CLOSED, "SSH is not reachable yet.", "", "", "", "unknown")

    def failure(self, *, acp=NO_ROUTE, retry=NO_ROUTE, ssh=None, host="root@10.0.0.2", gone=None,
                platform="darwin", networks=ON_THE_DEVICES_NETWORK):
        ports = {5009: acp, 22: retry}
        self.ssh = mock.Mock(return_value=ssh or ssh_refused())
        with mock.patch("timecapsulesmb.checks.network.tcp_connect_error", side_effect=lambda _h, port, _t: ports[port]), \
                mock.patch("timecapsulesmb.checks.network.local_lan_networks", return_value=networks), \
                mock.patch("timecapsulesmb.checks.network.sys.platform", platform), \
                mock.patch("timecapsulesmb.services.runtime.link_local_interface_gone", return_value=gone), \
                mock.patch("timecapsulesmb.services.runtime.probe_ssh_command_conn", self.ssh):
            return service_runtime.probe_failure_error(self.CLOSED, SshConnection(host, "pw", ""))

    def test_a_device_ssh_reaches_while_this_program_gets_no_route_is_a_block(self) -> None:
        # 0b33f140 and 45fcd831: deploy said "not answering" while ssh worked.
        error = self.failure()
        self.assertEqual(error.code, "acp_no_route")
        self.assertTrue(error.message.startswith("macOS refuses this command's connections to the device"))
        self.ssh.assert_called_once()
        self.assertEqual(self.ssh.call_args.kwargs["timeout"], service_runtime.SSH_PROOF_TIMEOUT_SECONDS)

    def test_no_route_that_ssh_cannot_get_past_either_is_not_answering(self) -> None:
        # 5dc49ff1: the device itself stopped answering.
        error = self.failure(ssh=ssh_no_route())
        self.assertEqual(error.code, "device_unreachable")
        self.assertEqual(error.message, "The device is not answering.")
        # Telemetry learns how ssh failed: the same block, or a device gone.
        self.assertTrue(error.detail.endswith(
            "SSH from this computer did not reach it either (ssh: connect to host 10.0.0.2 port 22: No route to host)."
        ))

    def test_other_acp_failures_never_ask_ssh(self) -> None:
        for acp, code in (("timed out", "device_unreachable"), (None, "ssh_disabled"), ("[Errno 64] Host is down", "device_unreachable")):
            with self.subTest(acp=acp):
                self.assertEqual(self.failure(acp=acp).code, code)
                self.ssh.assert_not_called()

    def test_no_route_where_no_block_is_possible_never_asks_ssh(self) -> None:
        # ssh could take its whole timeout and still not change the answer.
        for case in ({"platform": "linux"}, {"networks": ON_ANOTHER_NETWORK}):
            with self.subTest(**{key: str(value) for key, value in case.items()}):
                error = self.failure(**case)
                self.assertEqual(error.code, "device_unreachable")
                self.assertNotIn("SSH from this computer", error.detail)
                self.ssh.assert_not_called()

    def test_a_port_22_that_answers_on_the_last_try_is_not_a_block(self) -> None:
        self.assertEqual(self.failure(retry="[Errno 61] Connection refused").code, "device_unreachable")

    def test_a_link_local_address_on_a_missing_interface_says_so_before_any_connect(self) -> None:
        # 8db57d3a and 35f4dcc6: deploy to fe80::...%en8 after en8 went away.
        error = self.failure(host="root@fe80::f299:bfff:fe08:fb07%en8", gone="en8")
        self.assertEqual(error.code, "link_local_interface_gone")
        self.assertIn("fe80::f299:bfff:fe08:fb07%en8", error.message)
        self.assertIn("(en8)", error.message)
        self.ssh.assert_not_called()


class RequireAcpdReadyBlockTests(unittest.TestCase):
    """Uninstall, Disk Repair, Wipe Disk, flash and SSH Access: SSH works for
    the preflight, so a network ACP read refused with no route is a block
    when port 22 gets no route from this program too."""

    CONNECTION = SshConnection("root@10.0.0.2", "pw", "")

    def gate(self, read_error: str, *, retry=NO_ROUTE, networks=ON_THE_DEVICES_NETWORK, off_network=None):
        reading = AirportAcpReading(password_matches=None, error=read_error)
        with mock.patch("timecapsulesmb.device.acpd.acpd_answers", return_value=True), \
                mock.patch("timecapsulesmb.device.acpd.read_processes", return_value=""), \
                mock.patch("timecapsulesmb.device.acpd.restart_verdict", return_value=RestartVerdict("none")), \
                mock.patch("timecapsulesmb.device.acpd.acpd_answers_network", return_value=True), \
                mock.patch("timecapsulesmb.services.runtime.client_networks_if_off_network", return_value=off_network), \
                mock.patch("timecapsulesmb.checks.network.local_lan_networks", return_value=networks), \
                mock.patch("timecapsulesmb.checks.network.sys.platform", "darwin"), \
                mock.patch("timecapsulesmb.checks.network.tcp_connect_error", return_value=retry):
            with self.assertRaises(service_runtime.DeviceAccessError) as raised:
                service_runtime.require_acpd_ready(self.CONNECTION, OperationCallbacks(), reading=reading)
        return raised.exception

    def test_a_read_refused_with_no_route_is_a_block_instead_of_vpn_advice(self) -> None:
        # 0b33f140: Uninstall and Disk Repair said to turn off a VPN.
        error = self.gate(ACP_NO_ROUTE_READ)
        self.assertEqual(error.code, "acp_no_route")
        self.assertIn("SSH reaches it", error.message)

    def test_a_port_22_this_program_reaches_leaves_the_acp_path_to_blame(self) -> None:
        self.assertEqual(self.gate(ACP_NO_ROUTE_READ, retry=None).code, "acp_unreachable")

    def test_other_read_failures_keep_their_error(self) -> None:
        self.assertEqual(self.gate("ACP receive failed: timed out").code, "acp_unreachable")

    def test_an_address_off_this_computers_network_says_so_before_any_block(self) -> None:
        error = self.gate(ACP_NO_ROUTE_READ, off_network=[ipaddress.ip_network("192.168.1.0/24")])
        self.assertEqual(error.code, "device_off_network")


class SetSshLinkLocalTests(unittest.TestCase):
    """SSH Access and Add Device: a link-local address on a missing interface
    was shown the Local Network card (a3866735, 26f4a362); 26f4a362 came back
    once set-ssh looked the device up by its AirPort MAC."""

    def probe(self, *, record=None, located=None):
        callbacks = OperationCallbacks()
        with mock.patch("timecapsulesmb.services.acp_ssh.link_local_interface_gone", return_value="en9"), \
                mock.patch("timecapsulesmb.services.acp_ssh.local_lan_networks", return_value=ON_ANOTHER_NETWORK), \
                mock.patch("timecapsulesmb.services.acp_ssh._record_port_probe_context"), \
                mock.patch("timecapsulesmb.services.acp_ssh.locate_airport",
                           return_value=LocateResult("found", host=located) if located else LocateResult("not_found")) as follow, \
                mock.patch("timecapsulesmb.services.acp_ssh._confirm_identity", side_effect=lambda host, *_a: host), \
                mock.patch("timecapsulesmb.services.acp_ssh.set_dbug") as enable:
            try:
                host = enable_ssh_with_port_preflight(
                    "fe80::26a0:74ff:fe74:7908%en9", "pw", callbacks=callbacks, record=record,
                    tcp_connect_error_func=lambda _h, port: None if located and _h == located else NO_ROUTE,
                    sleep_func=mock.Mock(),
                )
                return host, None, follow, enable
            except ACPConnectionError as exc:
                return None, exc, follow, enable

    def test_it_is_said_as_such_and_not_as_a_local_network_block(self) -> None:
        _host, error, _follow, enable = self.probe()
        self.assertIsInstance(error, ACPLinkLocalInterfaceGoneError)
        self.assertNotIsInstance(error, ACPNoRouteError)
        self.assertEqual(error.code, ErrorCode.LINK_LOCAL_INTERFACE_GONE)
        self.assertIn("(en9)", str(error))
        enable.assert_not_called()

    def test_a_device_found_by_its_mac_goes_on_at_its_current_address(self) -> None:
        record = mock.Mock(airport_mac="24:a0:74:74:79:08")
        host, error, follow, enable = self.probe(record=record, located="192.168.178.112")
        self.assertIsNone(error)
        self.assertEqual(host, "192.168.178.112")
        follow.assert_called_once()
        enable.assert_called_once()


class DoctorSshLoginBlockTests(unittest.TestCase):
    """Checkup's SSH line: ssh itself reached the device (SSH off: the port
    is refused) while this program's own connects got no route (4c5c2a9b)."""

    CONNECTION = SshConnection("root@10.0.0.2", "pw", "")

    def check(self, ssh: SshCommandProbeResult, *, port22=NO_ROUTE, acp=NO_ROUTE, gone=None):
        ports = {22: port22, 5009: acp}
        with mock.patch("timecapsulesmb.checks.network.probe_ssh_command_conn", return_value=ssh), \
                mock.patch("timecapsulesmb.checks.network.tcp_connect_error", side_effect=lambda _h, port, _t: ports[port]), \
                mock.patch("timecapsulesmb.checks.network.local_lan_networks", return_value=ON_THE_DEVICES_NETWORK), \
                mock.patch("timecapsulesmb.checks.network.sys.platform", "darwin"), \
                mock.patch("timecapsulesmb.checks.network.link_local_interface_gone", return_value=gone):
            return check_ssh_login(self.CONNECTION)

    def test_ssh_refused_while_this_program_gets_no_route_is_a_block(self) -> None:
        result = self.check(ssh_refused())
        self.assertEqual(result.status, "FAIL")
        self.assertEqual(result.details["code"], "acp_no_route")
        self.assertTrue(result.message.startswith("macOS refuses this Mac's connections to 10.0.0.2"))
        self.assertIn("System Settings > Privacy & Security > Local Network", result.message)

    def test_ssh_that_gets_no_route_too_keeps_the_not_answering_line(self) -> None:
        result = self.check(ssh_no_route())
        self.assertEqual(result.details["code"], "device_unreachable")

    def test_a_link_local_address_on_a_missing_interface_gets_its_own_line(self) -> None:
        result = self.check(ssh_no_route(), gone="en9")
        self.assertEqual(result.details["code"], "link_local_interface_gone")
        self.assertIn("add the device again", result.message)


if __name__ == "__main__":
    unittest.main()
