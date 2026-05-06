from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from timecapsulesmb.cli import set_ssh
from timecapsulesmb.integrations import acp
from timecapsulesmb.transport.ssh import SshCommandTimeout, SshConnection


def ssh_result(returncode: int, stdout: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["ssh"], returncode, stdout=stdout, stderr="")


class FakeSocket:
    def __init__(self, response: bytes) -> None:
        self._response = bytearray(response)
        self.sent = b""
        self.closed = False
        self.timeout: float | None = None

    def settimeout(self, timeout: float) -> None:
        self.timeout = timeout

    def sendall(self, data: bytes) -> None:
        self.sent += data

    def recv(self, size: int) -> bytes:
        chunk = bytes(self._response[:size])
        del self._response[:size]
        return chunk

    def close(self) -> None:
        self.closed = True


def successful_setprop_response() -> bytes:
    return (
        acp._compose_header(command=acp.COMMAND_SETPROP)
        + acp._compose_property_element(None, None)
    )


class ACPTests(unittest.TestCase):
    def test_header_key_matches_legacy_acp_algorithm(self) -> None:
        self.assertEqual(
            acp._generate_acp_header_key("password").hex(),
            "7e588b76b36e272b0cac857d868ab5173e09c835f431657f3c9cb56d969aa507",
        )

    def test_compose_int_property_element(self) -> None:
        self.assertEqual(
            acp._compose_property_element("dbug", 0x3000).hex(),
            "64627567000000000000000400003000",
        )

    def test_set_dbug_sends_self_contained_python3_acp_request(self) -> None:
        fake_socket = FakeSocket(successful_setprop_response())
        with mock.patch("timecapsulesmb.integrations.acp.socket.create_connection", return_value=fake_socket) as create_mock:
            acp.set_dbug("10.0.0.2", "pw", "0x3000")

        create_mock.assert_called_once_with(("10.0.0.2", acp.ACP_PORT), timeout=10.0)
        self.assertIn(b"dbug", fake_socket.sent)
        self.assertIn(b"\x00\x00\x30\x00", fake_socket.sent)
        self.assertTrue(fake_socket.closed)

    def test_reboot_sets_acrb_property(self) -> None:
        fake_socket = FakeSocket(successful_setprop_response())
        with mock.patch("timecapsulesmb.integrations.acp.socket.create_connection", return_value=fake_socket):
            acp.reboot("10.0.0.2", "pw")

        self.assertIn(b"acRB", fake_socket.sent)
        self.assertIn(b"\x00\x00\x00\x00", fake_socket.sent)

    def test_enable_ssh_can_skip_reboot(self) -> None:
        with mock.patch("timecapsulesmb.integrations.acp.set_dbug") as set_dbug_mock:
            with mock.patch("timecapsulesmb.integrations.acp.reboot") as reboot_mock:
                acp.enable_ssh("10.0.0.2", "pw", reboot_device=False)

        set_dbug_mock.assert_called_once_with("10.0.0.2", "pw", acp.DBUG_SSH_VALUE, log=None, timeout=10.0)
        reboot_mock.assert_not_called()

    def test_nonzero_acp_response_is_auth_error(self) -> None:
        response = acp._compose_header(command=acp.COMMAND_SETPROP, error_code=-0x1234)
        fake_socket = FakeSocket(response)
        with mock.patch("timecapsulesmb.integrations.acp.socket.create_connection", return_value=fake_socket):
            with self.assertRaises(acp.ACPAuthError) as exc:
                acp.set_dbug("10.0.0.2", "wrong", "0x3000")

        self.assertIn("-0x1234", str(exc.exception))
        self.assertIn("wrong AirPort admin password", str(exc.exception))

    def test_bad_header_checksum_is_protocol_error(self) -> None:
        header = bytearray(acp._compose_header(command=acp.COMMAND_SETPROP))
        header[8] ^= 0x01
        with self.assertRaises(acp.ACPProtocolError):
            acp._parse_header(bytes(header))

    def test_disable_ssh_over_ssh_retries_and_reboots_on_success(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "-o ProxyJump=bastion")
        with mock.patch(
            "timecapsulesmb.cli.set_ssh.run_ssh",
            side_effect=[ssh_result(1, "nope"), ssh_result(0, "ok")],
        ) as run_ssh_mock:
            with mock.patch("timecapsulesmb.cli.set_ssh.remote_request_reboot") as reboot_mock:
                set_ssh.disable_ssh_over_ssh(connection, reboot_device=True)

        self.assertEqual(run_ssh_mock.call_count, 2)
        run_ssh_mock.assert_has_calls([
            mock.call(connection, "acp remove dbug", check=False, timeout=30),
            mock.call(connection, "/usr/sbin/acp remove dbug", check=False, timeout=30),
        ])
        reboot_mock.assert_called_once_with(connection)

    def test_disable_ssh_over_ssh_treats_absent_dbug_as_success(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")
        messages: list[str] = []
        with mock.patch(
            "timecapsulesmb.cli.set_ssh.run_ssh",
            return_value=ssh_result(22, "### remove property error: -10"),
        ) as run_ssh_mock:
            with mock.patch("timecapsulesmb.cli.set_ssh.remote_request_reboot") as reboot_mock:
                set_ssh.disable_ssh_over_ssh(connection, reboot_device=True, log=messages.append)

        run_ssh_mock.assert_called_once_with(connection, "acp remove dbug", check=False, timeout=30)
        reboot_mock.assert_called_once_with(connection)
        self.assertEqual(messages, ["'dbug' already absent via: acp remove dbug"])

    def test_disable_ssh_over_ssh_reports_bad_ssh_password_as_auth_failure(self) -> None:
        connection = SshConnection("root@10.0.0.2", "bad", "-o foo")
        with mock.patch(
            "timecapsulesmb.cli.set_ssh.run_ssh",
            return_value=ssh_result(255, "Permission denied, please try again."),
        ):
            with self.assertRaises(RuntimeError) as exc:
                set_ssh.disable_ssh_over_ssh(connection, reboot_device=False)

        self.assertEqual(str(exc.exception), "SSH authentication failed while trying to disable SSH over SSH.")

    def test_disable_ssh_over_ssh_continues_after_detached_reboot_timeout(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")
        messages: list[str] = []
        with mock.patch("timecapsulesmb.cli.set_ssh.run_ssh", return_value=ssh_result(0, "ok")):
            with mock.patch(
                "timecapsulesmb.cli.set_ssh.remote_request_reboot",
                side_effect=SshCommandTimeout("Timed out waiting for ssh command to finish: reboot"),
            ):
                set_ssh.disable_ssh_over_ssh(connection, reboot_device=True, log=messages.append)

        self.assertIn("Reboot request timed out; checking whether the device is rebooting...", messages[-1])


if __name__ == "__main__":
    unittest.main()
