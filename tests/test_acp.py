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


def response_with_header_version(response: bytes, version: int) -> bytes:
    header = response[:acp.HEADER.size]
    body = response[acp.HEADER.size:]
    (
        magic,
        _old_version,
        _old_checksum,
        body_checksum,
        body_size,
        flags,
        unused,
        command,
        error_code,
        key,
    ) = acp.HEADER.unpack(header)
    checksum_input = acp.HEADER.pack(
        magic,
        version,
        0,
        body_checksum,
        body_size,
        flags,
        unused,
        command,
        error_code,
        key,
    )
    rewritten_header = acp.HEADER.pack(
        magic,
        version,
        acp._adler32_i32(checksum_input),
        body_checksum,
        body_size,
        flags,
        unused,
        command,
        error_code,
        key,
    )
    return rewritten_header + body


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

        create_mock.assert_called_once_with(("10.0.0.2", acp.ACP_PORT), timeout=25.0)
        self.assertIn(b"dbug", fake_socket.sent)
        self.assertIn(b"\x00\x00\x30\x00", fake_socket.sent)
        self.assertTrue(fake_socket.closed)

    def test_reboot_sets_acrb_property(self) -> None:
        fake_socket = FakeSocket(successful_setprop_response())
        with mock.patch("timecapsulesmb.integrations.acp.socket.create_connection", return_value=fake_socket):
            acp.reboot("10.0.0.2", "pw")

        self.assertIn(b"acRB", fake_socket.sent)
        self.assertIn(b"\x00\x00\x00\x00", fake_socket.sent)

    def test_flash_firmware_bank_sends_primary_command_with_raw_payload(self) -> None:
        response = acp._compose_header(command=acp.COMMAND_FLASH_PRIMARY, payload=b"accepted") + b"accepted"
        fake_socket = FakeSocket(response)
        with mock.patch("timecapsulesmb.integrations.acp.socket.create_connection", return_value=fake_socket):
            result = acp.flash_firmware_bank("10.0.0.2", "pw", "primary", b"raw-bank")

        self.assertEqual(result.command, acp.COMMAND_FLASH_PRIMARY)
        self.assertEqual(result.reply_body, b"accepted")
        self.assertIn(b"raw-bank", fake_socket.sent)

    def test_flash_firmware_bank_rejects_bad_reply_body_checksum(self) -> None:
        response = acp._compose_header(command=acp.COMMAND_FLASH_PRIMARY, payload=b"accepted") + b"acceptEd"
        fake_socket = FakeSocket(response)
        with mock.patch("timecapsulesmb.integrations.acp.socket.create_connection", return_value=fake_socket):
            with self.assertRaises(acp.ACPProtocolError) as raised:
                acp.flash_firmware_bank("10.0.0.2", "pw", "primary", b"raw-bank")

        self.assertIn("body checksum mismatch", str(raised.exception))

    def test_flash_firmware_bank_sends_secondary_command(self) -> None:
        response = acp._compose_header(command=acp.COMMAND_FLASH_SECONDARY)
        fake_socket = FakeSocket(response)
        with mock.patch("timecapsulesmb.integrations.acp.socket.create_connection", return_value=fake_socket):
            result = acp.flash_firmware_bank("10.0.0.2", "pw", "secondary", b"raw-bank")

        self.assertEqual(result.command, acp.COMMAND_FLASH_SECONDARY)
        self.assertIn(b"raw-bank", fake_socket.sent)

    def test_flash_firmware_bank_rejects_unknown_bank_name(self) -> None:
        with self.assertRaises(acp.ACPProtocolError):
            acp.flash_firmware_bank("10.0.0.2", "pw", "tertiary", b"raw-bank")

    def test_enable_ssh_can_skip_reboot(self) -> None:
        with mock.patch("timecapsulesmb.integrations.acp.set_dbug") as set_dbug_mock:
            with mock.patch("timecapsulesmb.integrations.acp.reboot") as reboot_mock:
                acp.enable_ssh("10.0.0.2", "pw", reboot_device=False)

        set_dbug_mock.assert_called_once_with("10.0.0.2", "pw", acp.DBUG_SSH_VALUE, log=None, timeout=25.0)
        reboot_mock.assert_not_called()

    def test_nonzero_acp_response_is_auth_error(self) -> None:
        response = acp._compose_header(command=acp.COMMAND_SETPROP, error_code=-0x1234)
        fake_socket = FakeSocket(response)
        with mock.patch("timecapsulesmb.integrations.acp.socket.create_connection", return_value=fake_socket):
            with self.assertRaises(acp.ACPAuthError) as exc:
                acp.set_dbug("10.0.0.2", "wrong", "0x3000")

        self.assertIn("-0x1234", str(exc.exception))
        self.assertIn("wrong AirPort admin password", str(exc.exception))

    def test_get_property_int_validates_sized_reply_body_checksum(self) -> None:
        body = acp._compose_property_element("dbug", 0x3000)
        response = acp._compose_header(command=acp.COMMAND_GETPROP, payload=body) + body
        fake_socket = FakeSocket(response)
        with mock.patch("timecapsulesmb.integrations.acp.socket.create_connection", return_value=fake_socket):
            value = acp.get_property_int("10.0.0.2", "pw", "dbug")

        self.assertEqual(value, 0x3000)

    def test_read_identity_reads_syap_property(self) -> None:
        body = acp._compose_property_element("syAP", 119)
        response = acp._compose_header(command=acp.COMMAND_GETPROP, payload=body) + body
        fake_socket = FakeSocket(response)
        with mock.patch("timecapsulesmb.integrations.acp.socket.create_connection", return_value=fake_socket):
            identity = acp.read_identity("10.0.0.2", "pw")

        self.assertEqual(identity.syap, 119)
        self.assertIn(b"syAP", fake_socket.sent)

    def test_read_identity_accepts_older_acp_3_0_response_version(self) -> None:
        body = acp._compose_property_element("syAP", 106)
        response = response_with_header_version(
            acp._compose_header(command=acp.COMMAND_GETPROP, payload=body) + body,
            0x00030000,
        )
        fake_socket = FakeSocket(response)
        with mock.patch("timecapsulesmb.integrations.acp.socket.create_connection", return_value=fake_socket):
            identity = acp.read_identity("10.0.0.2", "pw")

        self.assertEqual(identity.syap, 106)
        self.assertIn(b"syAP", fake_socket.sent)

    def test_get_property_int_rejects_bad_sized_reply_body_checksum(self) -> None:
        body = acp._compose_property_element("dbug", 0x3000)
        corrupted = bytearray(body)
        corrupted[-1] ^= 0x01
        response = acp._compose_header(command=acp.COMMAND_GETPROP, payload=body) + bytes(corrupted)
        fake_socket = FakeSocket(response)
        with mock.patch("timecapsulesmb.integrations.acp.socket.create_connection", return_value=fake_socket):
            with self.assertRaises(acp.ACPProtocolError) as raised:
                acp.get_property_int("10.0.0.2", "pw", "dbug")

        self.assertIn("body checksum mismatch", str(raised.exception))

    def test_bad_header_checksum_is_protocol_error(self) -> None:
        header = bytearray(acp._compose_header(command=acp.COMMAND_SETPROP))
        header[8] ^= 0x01
        with self.assertRaises(acp.ACPProtocolError):
            acp._parse_header(bytes(header))

    def test_unsupported_response_version_is_protocol_error(self) -> None:
        response = response_with_header_version(
            acp._compose_header(command=acp.COMMAND_SETPROP),
            0x00030002,
        )
        with self.assertRaises(acp.ACPProtocolError) as raised:
            acp._parse_header(response[:acp.HEADER.size])

        self.assertIn("unsupported version 0x30002", str(raised.exception))

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
        self.assertEqual(messages, ["SSH debug flag 'dbug' already absent via: acp remove dbug"])

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

        self.assertIn("Reboot request timed out; continuing to observe whether the device is rebooting...", messages[-1])


if __name__ == "__main__":
    unittest.main()
