"""The set-ssh command."""
from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from unittest import mock
import timecapsulesmb.cli.main as cli_main_module
from timecapsulesmb.cli import set_ssh
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
                    with mock.patch("timecapsulesmb.services.set_ssh.runtime_service.wait_for_tcp_port_state", return_value=True):
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
                    with mock.patch("timecapsulesmb.services.set_ssh.runtime_service.wait_for_tcp_port_state") as wait_mock:
                        with redirect_stdout(output):
                            rc = set_ssh.main(["--enable", "--no-wait"])

        self.assertEqual(rc, 0)
        enable_mock.assert_called_once()
        wait_mock.assert_not_called()
        self.assertIn("SSH enable requested; not waiting for SSH to open.", output.getvalue().splitlines())
        self.assertNotIn("Summary(", output.getvalue())

    def test_set_ssh_disable_no_wait_prints_the_request_summary(self) -> None:
        output = io.StringIO()
        values = {"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"}
        with mock.patch("timecapsulesmb.cli.set_ssh.load_env_config", return_value=self.make_app_config(values)):
            with mock.patch("timecapsulesmb.cli.set_ssh.tcp_open", return_value=True):
                with mock.patch("timecapsulesmb.services.set_ssh.disable_ssh_over_ssh") as disable_mock:
                    with mock.patch("timecapsulesmb.services.set_ssh.runtime_service.wait_for_tcp_port_state") as wait_mock:
                        with redirect_stdout(output):
                            rc = set_ssh.main(["--disable", "--yes", "--no-wait"])

        self.assertEqual(rc, 0)
        disable_mock.assert_called_once()
        wait_mock.assert_not_called()
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
                        with mock.patch("timecapsulesmb.services.acp_ssh.enable_ssh") as enable_mock:
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
                    with mock.patch("timecapsulesmb.services.acp_ssh.enable_ssh") as enable_mock:
                        with mock.patch("timecapsulesmb.services.set_ssh.runtime_service.wait_for_tcp_port_state", return_value=True):
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

    def test_set_ssh_disable_fails_when_ssh_never_goes_down(self) -> None:
        output = io.StringIO()
        values = {"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"}
        with mock.patch("timecapsulesmb.cli.set_ssh.load_env_config", return_value=self.make_app_config(values)):
            with mock.patch("timecapsulesmb.cli.set_ssh.tcp_open", return_value=True):
                with mock.patch("builtins.input", return_value="y"):
                    with mock.patch("timecapsulesmb.services.set_ssh.disable_ssh_over_ssh"):
                        with mock.patch("timecapsulesmb.services.set_ssh.runtime_service.wait_for_tcp_port_state", return_value=False) as wait_port_mock:
                            with mock.patch("timecapsulesmb.services.set_ssh.wait_for_device_up") as wait_up_mock:
                                with redirect_stdout(output):
                                    rc = set_ssh.main([])
        self.assertEqual(rc, 1)
        wait_port_mock.assert_called_once_with("10.0.0.2", 22, expected_state=False, log=print, service_name="SSH port")
        wait_up_mock.assert_not_called()
        self.assertIn("SSH did not close after disable/reboot request; disable could not be verified.", output.getvalue())
        finished = self.telemetry_payload("set_ssh_finished")
        self.assertEqual(finished["result"], "failure")
        self.assertEqual(finished["set_ssh_action"], "disable_ssh")
        self.assertEqual(finished["ssh_final_reachable"], True)
        self.assertEqual(finished["ssh_disable_persisted"], False)
        self.assertEqual(finished["ssh_reboot_observed_down"], False)
        self.assertIn("stage=wait_for_ssh_down", finished["error"])

    def test_set_ssh_disable_fails_when_device_does_not_come_back(self) -> None:
        output = io.StringIO()
        values = {"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"}
        with mock.patch("timecapsulesmb.cli.set_ssh.load_env_config", return_value=self.make_app_config(values)):
            with mock.patch("timecapsulesmb.cli.set_ssh.tcp_open", return_value=True):
                with mock.patch("builtins.input", return_value="y"):
                    with mock.patch("timecapsulesmb.services.set_ssh.disable_ssh_over_ssh"):
                        with mock.patch("timecapsulesmb.services.set_ssh.runtime_service.wait_for_tcp_port_state", return_value=True) as wait_port_mock:
                            with mock.patch("timecapsulesmb.services.set_ssh.wait_for_device_up", return_value=False) as wait_up_mock:
                                with redirect_stdout(output):
                                    rc = set_ssh.main([])
        self.assertEqual(rc, 1)
        wait_port_mock.assert_called_once_with("10.0.0.2", 22, expected_state=False, log=print, service_name="SSH port")
        wait_up_mock.assert_called_once_with("10.0.0.2")
        self.assertIn("Device went down after disable request but did not come back within timeout.", output.getvalue())
        finished = self.telemetry_payload("set_ssh_finished")
        self.assertEqual(finished["result"], "failure")
        self.assertEqual(finished["set_ssh_action"], "disable_ssh")
        self.assertEqual(finished["ssh_reboot_observed_down"], True)
        self.assertEqual(finished["device_recovered"], False)
        self.assertIn("stage=wait_for_device_up", finished["error"])

    def test_set_ssh_disable_fails_when_ssh_reopens(self) -> None:
        output = io.StringIO()
        values = {"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw", "TC_SSH_OPTS": "-o ServerAliveInterval=5"}
        with mock.patch("timecapsulesmb.cli.set_ssh.load_env_config", return_value=self.make_app_config(values)):
            with mock.patch("timecapsulesmb.cli.set_ssh.tcp_open", return_value=True):
                with mock.patch("builtins.input", return_value="y"):
                    with mock.patch("timecapsulesmb.services.set_ssh.disable_ssh_over_ssh") as disable_ssh_mock:
                        with mock.patch("timecapsulesmb.services.set_ssh.runtime_service.wait_for_tcp_port_state", side_effect=[True, False]):
                            with mock.patch("timecapsulesmb.services.set_ssh.wait_for_device_up", return_value=True):
                                with redirect_stdout(output):
                                    rc = set_ssh.main([])
        self.assertEqual(rc, 1)
        disable_ssh_mock.assert_called_once_with(
            SshConnection("root@10.0.0.2", "pw", "-o ServerAliveInterval=5"),
            reboot_device=True,
            log=print,
        )
        self.assertIn("SSH reopened after reboot. Disable did not persist.", output.getvalue())
        finished = self.telemetry_payload("set_ssh_finished")
        self.assertEqual(finished["result"], "failure")
        self.assertEqual(finished["set_ssh_action"], "disable_ssh")
        self.assertEqual(finished["ssh_initially_reachable"], True)
        self.assertEqual(finished["ssh_reboot_observed_down"], True)
        self.assertEqual(finished["device_recovered"], True)
        self.assertEqual(finished["ssh_final_reachable"], True)
        self.assertEqual(finished["ssh_disable_persisted"], False)
        self.assertIn("stage=verify_ssh_disabled", finished["error"])

    def test_set_ssh_disable_flow_confirms_ssh_disabled(self) -> None:
        output = io.StringIO()
        values = {"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"}
        with mock.patch("timecapsulesmb.cli.set_ssh.load_env_config", return_value=self.make_app_config(values)):
            with mock.patch("timecapsulesmb.cli.set_ssh.tcp_open", return_value=True):
                with mock.patch("builtins.input", return_value="y"):
                    with mock.patch("timecapsulesmb.services.set_ssh.disable_ssh_over_ssh"):
                        with mock.patch("timecapsulesmb.services.set_ssh.runtime_service.wait_for_tcp_port_state", side_effect=[True, True]):
                            with mock.patch("timecapsulesmb.services.set_ssh.wait_for_device_up", return_value=True):
                                with redirect_stdout(output):
                                    rc = set_ssh.main([])
        self.assertEqual(rc, 0)
        self.assertIn("SSH disabled (remains closed after reboot)", output.getvalue())
        finished = self.telemetry_payload("set_ssh_finished")
        self.assertEqual(finished["result"], "success")
        self.assertEqual(finished["set_ssh_action"], "disable_ssh")
        self.assertEqual(finished["ssh_reboot_observed_down"], True)
        self.assertEqual(finished["device_recovered"], True)
        self.assertEqual(finished["ssh_final_reachable"], False)
        self.assertEqual(finished["ssh_disable_persisted"], True)

    def test_set_ssh_yes_disables_legacy_enabled_state_without_prompt(self) -> None:
        output = io.StringIO()
        values = {"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"}
        with mock.patch("timecapsulesmb.cli.set_ssh.load_env_config", return_value=self.make_app_config(values)):
            with mock.patch("timecapsulesmb.cli.set_ssh.tcp_open", return_value=True):
                with mock.patch("builtins.input", side_effect=AssertionError("--yes should skip prompt")) as input_mock:
                    with mock.patch("timecapsulesmb.services.set_ssh.disable_ssh_over_ssh") as disable_mock:
                        with mock.patch("timecapsulesmb.services.set_ssh.runtime_service.wait_for_tcp_port_state", side_effect=[True, True]):
                            with mock.patch("timecapsulesmb.services.set_ssh.wait_for_device_up", return_value=True):
                                with redirect_stdout(output):
                                    rc = set_ssh.main(["--yes"])
        self.assertEqual(rc, 0)
        input_mock.assert_not_called()
        disable_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()
