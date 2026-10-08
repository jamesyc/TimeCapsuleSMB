"""The set-ssh command."""
from __future__ import annotations

import io
import subprocess
import unittest
from contextlib import redirect_stdout
from unittest import mock
import timecapsulesmb.cli.main as cli_main_module
from timecapsulesmb.cli import set_ssh
from timecapsulesmb.services.runtime import AIRPORT_PASSWORD_MISMATCH_MESSAGE
from timecapsulesmb.transport.ssh import SshConnection
from timecapsulesmb.cli.util import ANSI_RED, ANSI_RESET

from tests.cli_support import CliTestCase


class CliSetSshTests(CliTestCase):
    def test_set_ssh_command_replaces_prep_device(self) -> None:
        self.assertIs(cli_main_module.COMMANDS["set-ssh"], set_ssh.main)
        self.assertNotIn("prep-device", cli_main_module.COMMANDS)

    def test_set_ssh_returns_error_when_env_missing(self) -> None:
        output = io.StringIO()
        with mock.patch("timecapsulesmb.cli.set_ssh.load_env_config", return_value=self.make_app_config({}, exists=False)):
            with redirect_stdout(output):
                rc = set_ssh.main([])
        self.assertEqual(rc, 1)
        self.assertIn("Please run the `configure` command before running `set-ssh`.", output.getvalue())
        started = self.telemetry_payload("set_ssh_started")
        finished = self.telemetry_payload("set_ssh_finished")
        self.assertEqual(started["command_id"], finished["command_id"])
        self.assertEqual(finished["result"], "failure")
        self.assertEqual(finished["set_ssh_action"], "missing_config")
        self.assertIn("stage=load_config", finished["error"])
        self.assertNotIn("TC_PASSWORD", finished["error"])

    def test_set_ssh_action_selection_covers_cli_modes(self) -> None:
        cases = [
            (False, False, False, set_ssh.SetSshAction.ENABLE),
            (False, False, True, set_ssh.SetSshAction.PROMPT_DISABLE),
            (True, False, False, set_ssh.SetSshAction.ENABLE),
            (True, False, True, set_ssh.SetSshAction.ENABLE_NOOP),
            (False, True, False, set_ssh.SetSshAction.DISABLE_NOOP),
            (False, True, True, set_ssh.SetSshAction.DISABLE),
        ]
        for explicit_enable, explicit_disable, ssh_open, expected in cases:
            with self.subTest(
                explicit_enable=explicit_enable,
                explicit_disable=explicit_disable,
                ssh_open=ssh_open,
            ):
                self.assertIs(
                    set_ssh.select_set_ssh_action(
                        explicit_enable=explicit_enable,
                        explicit_disable=explicit_disable,
                        ssh_open=ssh_open,
                    ),
                    expected,
                )

    def test_set_ssh_enable_flow_succeeds(self) -> None:
        output = io.StringIO()
        values = {"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"}
        with mock.patch("timecapsulesmb.cli.set_ssh.load_env_config", return_value=self.make_app_config(values)):
            with mock.patch("timecapsulesmb.cli.set_ssh.tcp_open", return_value=False):
                with mock.patch("timecapsulesmb.services.set_ssh.enable_ssh_with_port_preflight") as enable_ssh_mock:
                    with mock.patch.object(self.device, "ssh_open", False):
                        with redirect_stdout(output):
                            rc = set_ssh.main([])
        self.assertEqual(rc, 0)
        enable_ssh_mock.assert_called_once()
        self.assertIn("SSH is configured", output.getvalue())
        finished = self.telemetry_payload("set_ssh_finished")
        self.assertEqual(finished["result"], "success")
        self.assertEqual(finished["set_ssh_action"], "enable_ssh")
        self.assertEqual(finished["ssh_initially_reachable"], False)
        self.assertEqual(finished["ssh_final_reachable"], True)

    def test_set_ssh_status_requires_only_host(self) -> None:
        output = io.StringIO()
        values = {"TC_HOST": "root@10.0.0.2"}
        with mock.patch("timecapsulesmb.cli.set_ssh.load_env_config", return_value=self.make_app_config(values)):
            with mock.patch("timecapsulesmb.cli.set_ssh.tcp_open", return_value=True):
                with mock.patch("timecapsulesmb.services.set_ssh.enable_ssh_with_port_preflight") as enable_mock:
                    with mock.patch("timecapsulesmb.services.set_ssh.disable_ssh_over_ssh") as disable_mock:
                        with redirect_stdout(output):
                            rc = set_ssh.main(["--status"])

        self.assertEqual(rc, 0)
        self.assertIn("SSH enabled.", output.getvalue())
        enable_mock.assert_not_called()
        disable_mock.assert_not_called()
        finished = self.telemetry_payload("set_ssh_finished")
        self.assertEqual(finished["set_ssh_action"], "status")

    def test_set_ssh_explicit_enable_is_noop_when_already_enabled(self) -> None:
        output = io.StringIO()
        values = {"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"}
        with mock.patch("timecapsulesmb.cli.set_ssh.load_env_config", return_value=self.make_app_config(values)):
            with mock.patch("timecapsulesmb.cli.set_ssh.tcp_open", return_value=True):
                with mock.patch("timecapsulesmb.services.set_ssh.enable_ssh_with_port_preflight") as enable_mock:
                    with redirect_stdout(output):
                        rc = set_ssh.main(["--enable"])

        self.assertEqual(rc, 0)
        self.assertIn("SSH already enabled.", output.getvalue())
        enable_mock.assert_not_called()

    def test_set_ssh_explicit_disable_is_noop_when_already_disabled(self) -> None:
        output = io.StringIO()
        values = {"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"}
        with mock.patch("timecapsulesmb.cli.set_ssh.load_env_config", return_value=self.make_app_config(values)):
            with mock.patch("timecapsulesmb.cli.set_ssh.tcp_open", return_value=False):
                with mock.patch("timecapsulesmb.services.set_ssh.disable_ssh_over_ssh") as disable_mock:
                    with redirect_stdout(output):
                        rc = set_ssh.main(["--disable"])

        self.assertEqual(rc, 0)
        self.assertIn("SSH already disabled.", output.getvalue())
        disable_mock.assert_not_called()

    def test_set_ssh_no_wait_skips_enable_verification(self) -> None:
        output = io.StringIO()
        values = {"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"}
        with mock.patch("timecapsulesmb.cli.set_ssh.load_env_config", return_value=self.make_app_config(values)):
            with mock.patch("timecapsulesmb.cli.set_ssh.tcp_open", return_value=False):
                with mock.patch("timecapsulesmb.services.set_ssh.enable_ssh_with_port_preflight") as enable_mock:
                    with redirect_stdout(output):
                        rc = set_ssh.main(["--enable", "--no-wait"])

        self.assertEqual(rc, 0)
        enable_mock.assert_called_once()
        self.assertEqual(self.device.calls, ["request"])
        self.assertIn("SSH enable requested; not waiting for SSH to open.", output.getvalue().splitlines())
        self.assertNotIn("Summary(", output.getvalue())

    def test_set_ssh_disable_no_wait_prints_the_request_summary(self) -> None:
        output = io.StringIO()
        values = {"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"}
        with mock.patch("timecapsulesmb.cli.set_ssh.load_env_config", return_value=self.make_app_config(values)):
            with mock.patch("timecapsulesmb.cli.set_ssh.tcp_open", return_value=True):
                with mock.patch("timecapsulesmb.services.set_ssh.disable_ssh_over_ssh") as disable_mock:
                    with redirect_stdout(output):
                        rc = set_ssh.main(["--disable", "--yes", "--no-wait"])

        self.assertEqual(rc, 0)
        disable_mock.assert_called_once()
        self.assertEqual(self.device.calls, ["request"])
        self.assertIn(
            "SSH disable requested; not waiting for reboot or verifying SSH stays closed.",
            output.getvalue().splitlines(),
        )
        self.assertNotIn("Summary(", output.getvalue())
        finished = self.telemetry_payload("set_ssh_finished")
        self.assertEqual(finished["result"], "success")
        self.assertEqual(finished["set_ssh_action"], "disable_ssh")

    def test_set_ssh_enable_exception_emits_failure_stage(self) -> None:
        output = io.StringIO()
        values = {"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"}
        with mock.patch("timecapsulesmb.cli.set_ssh.load_env_config", return_value=self.make_app_config(values)):
            with mock.patch("timecapsulesmb.cli.set_ssh.tcp_open", return_value=False):
                with mock.patch("timecapsulesmb.services.set_ssh.enable_ssh_with_port_preflight", side_effect=RuntimeError("ACP failed")):
                    with redirect_stdout(output):
                        rc = set_ssh.main([])
        self.assertEqual(rc, 1)
        message = "Failed to enable SSH via ACP: ACP failed"
        self.assertIn(f"{ANSI_RED}Failed to enable SSH via ACP:{ANSI_RESET}", output.getvalue())
        self.assertIn("ACP failed", output.getvalue())
        finished = self.telemetry_payload("set_ssh_finished")
        self.assertEqual(finished["result"], "failure")
        self.assertEqual(finished["set_ssh_action"], "enable_ssh")
        self.assertIn("stage=probe_ssh", finished["error"])
        self.assertIn(message, finished["error"])
        self.assertNotIn(ANSI_RED, finished["error"])

    def test_set_ssh_enable_stops_when_acp_port_is_closed(self) -> None:
        output = io.StringIO()
        values = {"TC_HOST": "root@10.0.0.99", "TC_PASSWORD": "pw"}
        with mock.patch("timecapsulesmb.cli.set_ssh.load_env_config", return_value=self.make_app_config(values)):
            with mock.patch("timecapsulesmb.cli.set_ssh.tcp_open", return_value=False):
                with mock.patch("timecapsulesmb.services.acp_ssh.tcp_connect_error", return_value="Connection refused"):
                    with mock.patch("timecapsulesmb.services.acp_ssh.time.sleep") as sleep:
                        with mock.patch("timecapsulesmb.services.acp_ssh.set_dbug") as enable_mock:
                            with redirect_stdout(output):
                                rc = set_ssh.main([])

        self.assertEqual(rc, 1)
        self.assertEqual(sleep.call_args_list, [mock.call(2.0), mock.call(2.0)])
        enable_mock.assert_not_called()
        rendered = output.getvalue()
        self.assertIn(f"{ANSI_RED}Failed to enable SSH via ACP:{ANSI_RESET}", rendered)
        self.assertIn("Could not connect to ACP on 10.0.0.99:5009", rendered)
        finished = self.telemetry_payload("set_ssh_finished")
        self.assertIn("stage=acp_port_probe", finished["error"])
        self.assertIn("Failed to enable SSH via ACP: Could not connect to ACP on 10.0.0.99:5009", finished["error"])
        self.assertIn("acp_port_probe_attempts=3", finished["error"])
        self.assertIn("acp_port_probe_last_error=Connection refused", finished["error"])

    def test_set_ssh_enable_port_preflight_runs_enable_after_open_port(self) -> None:
        output = io.StringIO()
        values = {"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"}
        with mock.patch("timecapsulesmb.cli.set_ssh.load_env_config", return_value=self.make_app_config(values)):
            with mock.patch("timecapsulesmb.cli.set_ssh.tcp_open", return_value=False):
                with mock.patch("timecapsulesmb.services.acp_ssh.tcp_connect_error", return_value=None):
                    with mock.patch("timecapsulesmb.services.acp_ssh.set_dbug") as enable_mock:
                        with mock.patch.object(self.device, "ssh_open", False):
                            with redirect_stdout(output):
                                rc = set_ssh.main([])

        self.assertEqual(rc, 0)
        enable_mock.assert_called_once()
        self.assertIn("SSH is configured", output.getvalue())

    def test_set_ssh_enable_failure_reports_acp_error_without_bootstrap_guidance(self) -> None:
        output = io.StringIO()
        values = {"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"}
        error = "ACP command failed with error_code -0x1234 (likely wrong AirPort admin password)"
        with mock.patch("timecapsulesmb.cli.set_ssh.load_env_config", return_value=self.make_app_config(values)):
            with mock.patch("timecapsulesmb.cli.set_ssh.tcp_open", return_value=False):
                with mock.patch("timecapsulesmb.services.set_ssh.enable_ssh_with_port_preflight", side_effect=RuntimeError(error)):
                    with redirect_stdout(output):
                        rc = set_ssh.main([])

        self.assertEqual(rc, 1)
        rendered = output.getvalue()
        self.assertIn(f"{ANSI_RED}Failed to enable SSH via ACP:{ANSI_RESET}", rendered)
        self.assertIn(error, rendered)
        self.assertNotIn("./tcapsule bootstrap", rendered)
        finished = self.telemetry_payload("set_ssh_finished")
        self.assertIn(f"Failed to enable SSH via ACP: {error}", finished["error"])
        self.assertNotIn(ANSI_RED, finished["error"])

    def test_set_ssh_disable_failure_is_reported_as_ssh_error(self) -> None:
        output = io.StringIO()
        values = {"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"}
        error = "on-device acp failed"
        with mock.patch("timecapsulesmb.cli.set_ssh.load_env_config", return_value=self.make_app_config(values)):
            with mock.patch("timecapsulesmb.cli.set_ssh.tcp_open", return_value=True):
                with mock.patch("builtins.input", return_value="y"):
                    with mock.patch("timecapsulesmb.services.set_ssh.disable_ssh_over_ssh", side_effect=RuntimeError(error)):
                        with redirect_stdout(output):
                            rc = set_ssh.main([])

        self.assertEqual(rc, 1)
        rendered = output.getvalue()
        self.assertIn(f"{ANSI_RED}Failed to disable SSH over SSH:{ANSI_RESET}", rendered)
        self.assertIn(error, rendered)
        self.assertNotIn("AirPyrt", rendered)
        self.assertNotIn("./tcapsule bootstrap", rendered)
        finished = self.telemetry_payload("set_ssh_finished")
        self.assertIn(f"Failed to disable SSH over SSH: {error}", finished["error"])
        self.assertNotIn("AirPyrt", finished["error"])
        self.assertNotIn(ANSI_RED, finished["error"])

    def test_set_ssh_legacy_enabled_state_can_leave_ssh_enabled(self) -> None:
        output = io.StringIO()
        values = {"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"}
        with mock.patch("timecapsulesmb.cli.set_ssh.load_env_config", return_value=self.make_app_config(values)):
            with mock.patch("timecapsulesmb.cli.set_ssh.tcp_open", return_value=True):
                with mock.patch("builtins.input", return_value="n"):
                    with mock.patch("timecapsulesmb.services.set_ssh.disable_ssh_over_ssh") as disable_mock:
                        with redirect_stdout(output):
                            rc = set_ssh.main([])

        self.assertEqual(rc, 0)
        self.assertIn("Leaving SSH enabled.", output.getvalue())
        disable_mock.assert_not_called()
        finished = self.telemetry_payload("set_ssh_finished")
        self.assertEqual(finished["result"], "success")
        self.assertEqual(finished["set_ssh_action"], "leave_enabled")
        self.assertEqual(finished["ssh_final_reachable"], True)

    def run_disable(self, argv: list[str], *, answer: str = "y"):
        output = io.StringIO()
        values = {"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw", "TC_SSH_OPTS": "-o ServerAliveInterval=5"}
        with mock.patch("timecapsulesmb.cli.set_ssh.load_env_config", return_value=self.make_app_config(values)):
            with mock.patch("timecapsulesmb.cli.set_ssh.tcp_open", return_value=True):
                with mock.patch("builtins.input", return_value=answer) as input_mock:
                    with mock.patch("timecapsulesmb.services.set_ssh.disable_ssh_over_ssh") as disable_mock:
                        with redirect_stdout(output):
                            rc = set_ssh.main(argv)
        return rc, output.getvalue(), disable_mock, input_mock

    def test_set_ssh_disable_refuses_a_password_the_device_would_reject_before_asking(self) -> None:
        # Both the prompted (legacy) and the explicit --disable path: SSH is
        # not turned off and no reboot is requested with a password ACP rejects.
        for argv in ([], ["--disable"]):
            with self.subTest(argv=argv):
                compare = mock.Mock(return_value=subprocess.CompletedProcess(["ssh"], 1, b"", b""))
                self.device.calls.clear()
                with mock.patch("timecapsulesmb.device.probe.run_ssh_input", compare):
                    with self.assertRaises(SystemExit) as raised:
                        self.run_disable(argv)

                self.assertEqual(str(raised.exception.code), AIRPORT_PASSWORD_MISMATCH_MESSAGE)
                compare.assert_called_once()
                self.assertNotIn("request", self.device.calls)
                finished = self.telemetry_payload("set_ssh_finished")
                self.assertEqual(finished["result"], "failure")

    def test_set_ssh_disable_checks_the_password_before_the_prompt(self) -> None:
        self.device.ssh_up_after_boot = None
        order: list[str] = []
        compare = mock.Mock(side_effect=lambda *_a, **_k: order.append("compare") or subprocess.CompletedProcess(["ssh"], 0, b"", b""))
        with mock.patch("timecapsulesmb.device.probe.run_ssh_input", compare):
            with mock.patch("timecapsulesmb.cli.set_ssh.confirm", side_effect=lambda *_a, **_k: order.append("prompt") or True):
                rc, _text, disable_mock, _input = self.run_disable([])

        self.assertEqual(rc, 0)
        self.assertEqual(order, ["compare", "prompt"])
        disable_mock.assert_called_once()

    def test_set_ssh_disable_fails_when_the_device_never_restarts(self) -> None:
        self.device.reboots = False
        rc, text, _disable, _input = self.run_disable([])

        self.assertEqual(rc, 1)
        self.assertIn("Failed to verify SSH disable:", text)
        self.assertIn("Reboot was requested but the device did not restart.", text)
        self.assertNotIn("tcp 22", self.device.calls)
        finished = self.telemetry_payload("set_ssh_finished")
        self.assertEqual(finished["result"], "failure")
        self.assertEqual(finished["set_ssh_action"], "disable_ssh")
        self.assertIn("stage=wait_for_reboot_down", finished["error"])

    def test_set_ssh_disable_fails_when_device_does_not_come_back(self) -> None:
        self.device.kernel_after = 10_000
        rc, text, _disable, _input = self.run_disable([])

        self.assertEqual(rc, 1)
        self.assertIn("Device went down after disable request but did not come back within timeout.", text)
        finished = self.telemetry_payload("set_ssh_finished")
        self.assertEqual(finished["result"], "failure")
        self.assertEqual(finished["set_ssh_action"], "disable_ssh")
        self.assertIn("stage=wait_for_reboot_up", finished["error"])

    def test_set_ssh_disable_fails_when_ssh_reopens(self) -> None:
        self.device.ssh_up_after_boot = 10.0
        rc, text, disable_ssh_mock, _input = self.run_disable([])

        self.assertEqual(rc, 1)
        disable_ssh_mock.assert_called_once_with(
            SshConnection("root@10.0.0.2", "pw", "-o ServerAliveInterval=5"),
            log=print,
        )
        self.assertIn("SSH reopened after reboot. Disable did not persist.", text)
        finished = self.telemetry_payload("set_ssh_finished")
        self.assertEqual(finished["result"], "failure")
        self.assertEqual(finished["set_ssh_action"], "disable_ssh")
        self.assertEqual(finished["ssh_initially_reachable"], True)
        self.assertNotIn("device_came_back_after_reboot", finished)
        self.assertIn("stage=wait_for_reboot_up", finished["error"])

    def test_set_ssh_disable_flow_confirms_ssh_disabled(self) -> None:
        self.device.ssh_up_after_boot = None
        rc, text, _disable, _input = self.run_disable([])

        self.assertEqual(rc, 0)
        self.assertIn("SSH disabled (remains closed after reboot)", text)
        # The uptime proved the reboot before port 22 was checked twice.
        self.assertTrue(self.device.served_new_boot)
        self.assertEqual(self.device.calls.count("tcp 22"), 2)
        finished = self.telemetry_payload("set_ssh_finished")
        self.assertEqual(finished["result"], "success")
        self.assertEqual(finished["set_ssh_action"], "disable_ssh")
        self.assertEqual(finished["device_came_back_after_reboot"], True)
        self.assertEqual(finished["ssh_final_reachable"], False)

    def test_set_ssh_yes_disables_legacy_enabled_state_without_prompt(self) -> None:
        self.device.ssh_up_after_boot = None
        rc, _text, disable_mock, input_mock = self.run_disable(["--yes"])

        self.assertEqual(rc, 0)
        input_mock.assert_not_called()
        disable_mock.assert_called_once()

if __name__ == "__main__":
    unittest.main()
