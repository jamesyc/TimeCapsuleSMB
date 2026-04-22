from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sys


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from timecapsulesmb.cli.context import CommandContext
from timecapsulesmb.telemetry import MAX_SEND_ATTEMPTS, TelemetryClient


class TelemetryTests(unittest.TestCase):
    def test_emit_builds_schema_v2_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bootstrap_path = Path(tmp) / ".bootstrap"
            bootstrap_path.write_text("INSTALL_ID=test-install\n")
            with mock.patch.dict(os.environ, {"TCAPSULE_TELEMETRY_TOKEN": "secret-token"}, clear=False):
                client = TelemetryClient.from_values(
                    {
                        "TC_CONFIGURE_ID": "config-id",
                        "TC_MDNS_DEVICE_MODEL": "TimeCapsule8,119",
                        "TC_AIRPORT_SYAP": "119",
                    },
                    nbns_enabled=True,
                    bootstrap_path=bootstrap_path,
                )
                with mock.patch.object(client, "_dispatch_payload_async") as dispatch_mock:
                    client.emit("deploy_started")
        payload = dispatch_mock.call_args.args[0]
        self.assertEqual(payload["schema_version"], 2)
        self.assertEqual(payload["event"], "deploy_started")
        self.assertEqual(payload["install_id"], "test-install")
        self.assertEqual(payload["configure_id"], "config-id")
        self.assertEqual(payload["device_model"], "TimeCapsule8,119")
        self.assertEqual(payload["device_syap"], "119")
        self.assertTrue(payload["nbns_enabled"])
        self.assertEqual(payload["host_os"], "macOS" if sys.platform == "darwin" else payload["host_os"])
        self.assertNotIn("command_id", payload)

    def test_emit_is_disabled_when_bootstrap_has_telemetry_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bootstrap_path = Path(tmp) / ".bootstrap"
            bootstrap_path.write_text("INSTALL_ID=test-install\nTELEMETRY=false\n")
            with mock.patch.dict(os.environ, {"TCAPSULE_TELEMETRY_TOKEN": "secret-token"}, clear=False):
                client = TelemetryClient.from_values({}, bootstrap_path=bootstrap_path)
                with mock.patch.object(client, "_dispatch_payload_async") as dispatch_mock:
                    client.emit("doctor_started")
        dispatch_mock.assert_not_called()

    def test_send_payload_retries_once_on_transport_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bootstrap_path = Path(tmp) / ".bootstrap"
            bootstrap_path.write_text("INSTALL_ID=test-install\n")
            with mock.patch.dict(os.environ, {"TCAPSULE_TELEMETRY_TOKEN": "secret-token"}, clear=False):
                client = TelemetryClient.from_values({}, bootstrap_path=bootstrap_path)
                success_response = mock.MagicMock()
                success_response.__enter__.return_value = success_response
                success_response.__exit__.return_value = None
                with mock.patch("urllib.request.urlopen", side_effect=[OSError("boom"), success_response]) as urlopen_mock:
                    client._send_payload({"event": "doctor_started"})
        self.assertEqual(urlopen_mock.call_count, MAX_SEND_ATTEMPTS)

    def test_command_context_reuses_command_id_for_started_and_finished_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bootstrap_path = Path(tmp) / ".bootstrap"
            bootstrap_path.write_text("INSTALL_ID=test-install\n")
            with mock.patch.dict(os.environ, {"TCAPSULE_TELEMETRY_TOKEN": "secret-token"}, clear=False):
                client = TelemetryClient.from_values({}, bootstrap_path=bootstrap_path)
                with mock.patch.object(client, "_dispatch_payload_async") as dispatch_mock:
                    with mock.patch.object(client, "_send_payload") as send_mock:
                        command = CommandContext(client, "deploy", "deploy_started", "deploy_finished")
                        command.finish(result="success")
        started_payload = dispatch_mock.call_args.args[0]
        finished_payload = send_mock.call_args.args[0]
        self.assertIn("command_id", started_payload)
        self.assertEqual(started_payload["command_id"], finished_payload["command_id"])
        self.assertEqual(finished_payload["event"], "deploy_finished")
        self.assertEqual(finished_payload["result"], "success")

    def test_command_context_marks_keyboard_interrupt_as_cancelled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bootstrap_path = Path(tmp) / ".bootstrap"
            bootstrap_path.write_text("INSTALL_ID=test-install\n")
            with mock.patch.dict(os.environ, {"TCAPSULE_TELEMETRY_TOKEN": "secret-token"}, clear=False):
                client = TelemetryClient.from_values({}, bootstrap_path=bootstrap_path)
                with mock.patch.object(client, "_dispatch_payload_async"):
                    with mock.patch.object(client, "_send_payload") as send_mock:
                        with self.assertRaises(KeyboardInterrupt):
                            with CommandContext(client, "doctor", "doctor_started", "doctor_finished"):
                                raise KeyboardInterrupt
        finished_payload = send_mock.call_args.args[0]
        self.assertEqual(finished_payload["event"], "doctor_finished")
        self.assertEqual(finished_payload["result"], "cancelled")
        self.assertEqual(finished_payload["error"], "Cancelled by user")

    def test_command_context_captures_system_exit_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bootstrap_path = Path(tmp) / ".bootstrap"
            bootstrap_path.write_text("INSTALL_ID=test-install\n")
            with mock.patch.dict(os.environ, {"TCAPSULE_TELEMETRY_TOKEN": "secret-token"}, clear=False):
                client = TelemetryClient.from_values(
                    {
                        "TC_HOST": "root@192.168.1.118",
                        "TC_SSH_OPTS": "-L 108:127.0.0.1:108",
                        "TC_MDNS_DEVICE_MODEL": "TimeCapsule",
                        "TC_AIRPORT_SYAP": "119",
                    },
                    bootstrap_path=bootstrap_path,
                )
                with mock.patch.object(client, "_dispatch_payload_async"):
                    with mock.patch.object(client, "_send_payload") as send_mock:
                        with self.assertRaises(SystemExit):
                            with CommandContext(client, "deploy", "deploy_started", "deploy_finished", values={
                                "TC_HOST": "root@192.168.1.118",
                                "TC_SSH_OPTS": "-L 108:127.0.0.1:108",
                                "TC_MDNS_DEVICE_MODEL": "TimeCapsule",
                                "TC_AIRPORT_SYAP": "119",
                            }):
                                raise SystemExit("Connecting to the device failed, SSH error: bind [127.0.0.1]:108: Permission denied")
        finished_payload = send_mock.call_args.args[0]
        self.assertEqual(finished_payload["result"], "failure")
        self.assertIn("Connecting to the device failed, SSH error: bind [127.0.0.1]:108: Permission denied", finished_payload["error"])
        self.assertIn("Debug context:", finished_payload["error"])
        self.assertIn("ssh_opts=-L 108:127.0.0.1:108", finished_payload["error"])


if __name__ == "__main__":
    unittest.main()
