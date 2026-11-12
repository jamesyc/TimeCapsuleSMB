"""The activate, uninstall and fsck commands."""
from __future__ import annotations

import io
from dataclasses import replace
import shlex
import json
import time
import unittest
from contextlib import ExitStack
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace
from unittest import mock
from timecapsulesmb.cli import activate, fsck, uninstall
from timecapsulesmb.cli.context import CommandContext
from timecapsulesmb.cli.main import main
from timecapsulesmb.cli.runtime import DEVICE_PASSWORD_NONINTERACTIVE_MESSAGE, NonInteractivePromptError
from timecapsulesmb.integrations.acp import ACPConnectionError
from timecapsulesmb.services import maintenance as maintenance_service
from timecapsulesmb.services import reboot as reboot_service
from timecapsulesmb.services.runtime import AIRPORT_ADMIN_PASSWORD_REJECTED_MESSAGE
from timecapsulesmb.core.config import MANAGED_PAYLOAD_DIR_NAME
from timecapsulesmb.device.probe import ProbeResult, SshAccessStatus
from timecapsulesmb.device.storage import MaStDiskSnapshot, MaStPartitionSnapshot, MaStVolume
from timecapsulesmb.deploy.commands import (
    RunScriptAction,
    render_remote_action,
    StopProcessAction,
    managed_stop_actions,
    render_remote_actions,
)
from timecapsulesmb.deploy.verify import VerificationResult

from tests.cli_support import AUTH_FAILED_CLI_STEPS, CliTestCase, FakeCommandContext
from tests.device_ps import stuck_stop_output
from tests.reboot_support import FakeInstalledRuntime, acp_password_answer



CLEAN_FSCK_OUTPUT = "--- fsck_hfs /dev/dk2 ---\nOK\ntcapsule-fsck: fsck_hfs exit status 0\n"
DK5_FSCK_OUTPUT = CLEAN_FSCK_OUTPUT.replace("dk2", "dk5")


def streamed_fsck(stdout: str, returncode: int = 0):
    """A fake run_ssh for the fsck script: passes each line on as it arrives, as the transport does."""
    result = mock.Mock(stdout=stdout, returncode=returncode)

    def remote(*_args, on_line, **_kwargs):
        for line in stdout.splitlines():
            on_line(line)
        return result

    return remote

class CliMaintenanceTests(CliTestCase):
    def setUp(self) -> None:
        super().setUp()
        # Before rebooting, fsck probes the device like deploy does; a test may
        # swap in a NetBSD4 answer. Tests that patch the probe themselves win.
        self.fsck_probe_state = self.make_logged_in_probe_state(self.make_supported_compatibility())
        self._exit_stack.enter_context(mock.patch(
            "timecapsulesmb.services.runtime.probe_managed_connection_state",
            side_effect=lambda *_args, **_kwargs: self.fsck_probe_state,
        ))

    def use_stock_netbsd4(self) -> None:
        self.fsck_probe_state = self.make_logged_in_probe_state(self.make_supported_netbsd4_compatibility())

    def _patch_mast_volume_flow(
        self,
        stack: ExitStack,
        module: str,
        *,
        mounted_volumes: tuple[MaStVolume, ...] | None = None,
        read_volumes: tuple[MaStVolume, ...] | None = None,
    ) -> SimpleNamespace:
        mounted = mounted_volumes if mounted_volumes is not None else (self._mast_volume("dk2"),)
        read = read_volumes if read_volumes is not None else mounted
        return SimpleNamespace(
            read_mast_volumes_conn=stack.enter_context(mock.patch("timecapsulesmb.services.storage.read_mast_volumes_conn", return_value=read)),
            mounted_mast_volumes_conn=stack.enter_context(mock.patch("timecapsulesmb.services.storage.mounted_mast_volumes_conn", return_value=mounted)),
        )

    def test_activate_command_is_registered(self) -> None:
        with mock.patch("timecapsulesmb.cli.main.COMMANDS", {"activate": mock.Mock(return_value=0)}) as commands:
            rc = main(["activate", "--dry-run"])
        self.assertEqual(rc, 0)
        commands["activate"].assert_called_once_with(["--dry-run"])

    def test_fsck_command_is_registered(self) -> None:
        with mock.patch("timecapsulesmb.cli.main.COMMANDS", {"fsck": mock.Mock(return_value=0)}) as commands:
            rc = main(["fsck", "--yes", "--no-reboot"])
        self.assertEqual(rc, 0)
        commands["fsck"].assert_called_once_with(["--yes", "--no-reboot"])

    def test_activate_dry_run_prints_netbsd4_activation_plan(self) -> None:
        output = io.StringIO()
        values = self.make_valid_env()
        with mock.patch("timecapsulesmb.cli.activate.load_env_config", return_value=self.make_app_config(values)):
            with mock.patch("timecapsulesmb.services.runtime.probe_managed_connection_state", return_value=self.make_logged_in_probe_state(self.make_supported_netbsd4_compatibility())):
                with mock.patch("timecapsulesmb.services.activation.run_remote_actions") as actions_mock:
                    with redirect_stdout(output):
                        rc = activate.main(["--dry-run"])
        self.assertEqual(rc, 0)
        actions_mock.assert_not_called()
        text = output.getvalue()
        self.assertIn("Dry run: NetBSD 4 activation plan", text)
        for action in managed_stop_actions(stop_afpserver=False):
            self.assertIn(render_remote_action(action), text)
        # Activation does not reboot, and ACPd never respawns afpserver.
        self.assertNotIn(render_remote_action(StopProcessAction("afpserver")), text)
        self.assertIn("/bin/sh /mnt/Flash/rc.local", text)
        self.assertIn("skip rc.local if the NetBSD 4 payload is already healthy", text)
        self.assertIn("managed runtime smb.conf is present", text)
        self.assertIn("managed smbd parent process is running", text)
        self.assertIn("smbd owns IPv4 and IPv6 wildcard TCP 445 listeners", text)
        self.assertIn("managed mDNS registrant becomes ready", text)
        self.assertIn("This will start the deployed Samba payload on the AirPort storage device.", text)
        self.assertIn("NetBSD 4 devices cannot auto-run Samba after a reboot.", text)

    def test_activate_says_netbsd4_cannot_auto_run_samba_only_without_the_boot_hook(self) -> None:
        warning = "NetBSD 4 devices cannot auto-run Samba after a reboot."
        for rc_local_autostart, shown in ((False, True), (True, False)):
            with self.subTest(rc_local_autostart=rc_local_autostart):
                state = self.make_logged_in_probe_state(self.make_supported_netbsd4_compatibility())
                state = replace(state, probe_result=replace(state.probe_result, rc_local_autostart=rc_local_autostart))
                output = io.StringIO()
                with mock.patch("timecapsulesmb.cli.activate.load_env_config", return_value=self.make_app_config(self.make_valid_env())):
                    with mock.patch("timecapsulesmb.services.runtime.probe_managed_connection_state", return_value=state):
                        with redirect_stdout(output):
                            self.assertEqual(activate.main(["--dry-run"]), 0)
                self.assertEqual(warning in output.getvalue(), shown)

    def test_activate_ensures_install_id_before_telemetry(self) -> None:
        output = io.StringIO()
        values = self.make_valid_env()
        with mock.patch("timecapsulesmb.cli.activate.ensure_install_id") as ensure_mock:
            with mock.patch("timecapsulesmb.cli.activate.load_env_config", return_value=self.make_app_config(values)):
                with mock.patch(
                    "timecapsulesmb.cli.activate.CommandContext",
                    return_value=FakeCommandContext(compatibility=self.make_supported_netbsd4_compatibility()),
                ):
                    with mock.patch("timecapsulesmb.cli.context.CommandContext.require_compatibility", return_value=self.make_supported_netbsd4_compatibility()):
                        with redirect_stdout(output):
                            rc = activate.main(["--dry-run"])
        self.assertEqual(rc, 0)
        ensure_mock.assert_called_once_with()

    def test_activate_rejects_non_netbsd4_device(self) -> None:
        values = self.make_valid_env()
        with mock.patch("timecapsulesmb.cli.activate.load_env_config", return_value=self.make_app_config(values)):
            with mock.patch("timecapsulesmb.services.runtime.probe_managed_connection_state", return_value=self.make_logged_in_probe_state(self.make_supported_compatibility())):
                with self.assertRaises(SystemExit) as cm:
                    activate.main(["--dry-run"])
        self.assertIn("only supported for NetBSD 4", str(cm.exception))

    def test_activate_prompt_decline_cancels_before_remote_actions(self) -> None:
        output = io.StringIO()
        command_context = FakeCommandContext(compatibility=self.make_supported_netbsd4_compatibility())
        values = self.make_valid_env()
        with mock.patch("timecapsulesmb.cli.activate.load_env_config", return_value=self.make_app_config(values)):
            with mock.patch("timecapsulesmb.cli.context.CommandContext.require_compatibility", return_value=self.make_supported_netbsd4_compatibility()):
                with mock.patch("builtins.input", return_value="n"):
                    with mock.patch("timecapsulesmb.services.activation.run_remote_actions") as actions_mock:
                        with mock.patch("timecapsulesmb.cli.activate.CommandContext", side_effect=command_context.opened_as):
                            with redirect_stdout(output):
                                rc = activate.main([])
        self.assertEqual(rc, 0)
        actions_mock.assert_not_called()
        text = output.getvalue()
        self.assertIn("This will start the deployed Samba payload on the AirPort storage device.", text)
        self.assertIn("Activation cancelled.", text)
        command_context.finish.assert_called_once()
        self.assertEqual(command_context.finish.call_args.kwargs["result"], "cancelled")
        self.assertIn("Cancelled by user at NetBSD 4 activation confirmation prompt.", command_context.finish.call_args.kwargs["error"])

    def test_activate_prompt_eof_reports_non_interactive_error(self) -> None:
        output = io.StringIO()
        command_context = FakeCommandContext(compatibility=self.make_supported_netbsd4_compatibility())
        values = self.make_valid_env()
        with mock.patch("timecapsulesmb.cli.activate.load_env_config", return_value=self.make_app_config(values)):
            with mock.patch("timecapsulesmb.cli.context.CommandContext.require_compatibility", return_value=self.make_supported_netbsd4_compatibility()):
                with mock.patch("builtins.input", side_effect=EOFError):
                    with mock.patch("timecapsulesmb.services.activation.run_remote_actions") as actions_mock:
                        with mock.patch("timecapsulesmb.cli.activate.CommandContext", side_effect=command_context.opened_as):
                            with redirect_stdout(output):
                                with self.assertRaises(NonInteractivePromptError) as raised:
                                    activate.main([])
        actions_mock.assert_not_called()
        self.assertEqual(
            str(raised.exception),
            "No answer was read for the activation confirmation. Use `activate --yes` to skip the prompt.",
        )
        command_context.finish.assert_called_once()
        self.assertEqual(command_context.finish.call_args.kwargs["result"], "failure")

    def test_activate_no_input_requires_yes_without_reading_stdin(self) -> None:
        output = io.StringIO()
        command_context = FakeCommandContext(compatibility=self.make_supported_netbsd4_compatibility())
        values = self.make_valid_env()
        with mock.patch("timecapsulesmb.cli.activate.load_env_config", return_value=self.make_app_config(values)):
            with mock.patch("timecapsulesmb.cli.context.CommandContext.require_compatibility", return_value=self.make_supported_netbsd4_compatibility()):
                with mock.patch("builtins.input", side_effect=AssertionError("activate --no-input must not prompt")) as input_mock:
                    with mock.patch("timecapsulesmb.services.activation.run_remote_actions") as actions_mock:
                        with mock.patch("timecapsulesmb.cli.activate.CommandContext", side_effect=command_context.opened_as):
                            with redirect_stdout(output):
                                rc = activate.main(["--no-input"])

        self.assertEqual(rc, 1)
        input_mock.assert_not_called()
        actions_mock.assert_not_called()
        self.assertIn("Running `activate` in non-interactive mode requires `--yes`", output.getvalue())
        self.assertEqual(command_context.finish.call_args.kwargs["result"], "failure")

    def test_activate_yes_runs_idempotent_actions_and_verifies(self) -> None:
        output = io.StringIO()
        values = self.make_valid_env()
        with mock.patch("timecapsulesmb.cli.activate.load_env_config", return_value=self.make_app_config(values)):
            with mock.patch("timecapsulesmb.services.runtime.probe_managed_connection_state", return_value=self.make_logged_in_probe_state(self.make_supported_netbsd4_compatibility())):
                with mock.patch("timecapsulesmb.services.activation.probe_managed_runtime_conn", return_value=self.managed_runtime_probe(False)):
                    with mock.patch("timecapsulesmb.services.activation.run_remote_actions") as actions_mock:
                        with mock.patch("timecapsulesmb.services.runtime_verification.probe_managed_runtime_conn", return_value=self.managed_runtime_probe(True)) as verify_mock:
                            with mock.patch("time.sleep") as sleep_mock:
                                with redirect_stdout(output):
                                    rc = activate.main(["--yes"])
        self.assertEqual(rc, 0)
        actions_mock.assert_called_once()
        self.assertEqual(
            actions_mock.call_args.args[1],
            [
                *managed_stop_actions(stop_afpserver=False),
                RunScriptAction("/mnt/Flash/rc.local"),
            ],
        )
        self.assertEqual(actions_mock.call_args.kwargs, {})
        self.assertEqual(verify_mock.call_args.args[0].host, "root@10.0.0.2")
        self.assertEqual(verify_mock.call_args.kwargs["timeout_seconds"], 200)
        # Verification polls on its own; nothing waits a fixed time first.
        sleep_mock.assert_not_called()
        self.assertIn("without file transfer", output.getvalue())
        self.assertNotIn("Waiting a few seconds", output.getvalue())

    def test_activate_runs_with_a_password_acp_would_reject(self) -> None:
        # Activate only uses SSH; on a NetBSD 4 device that is not flashed it is
        # what starts file sharing after a reboot, so the syPW check never stops it.
        compare = acp_password_answer(False)
        values = self.make_valid_env()
        with mock.patch("timecapsulesmb.device.probe.read_airport_acp", compare):
            with mock.patch("timecapsulesmb.cli.activate.load_env_config", return_value=self.make_app_config(values)):
                with mock.patch("timecapsulesmb.services.runtime.probe_managed_connection_state", return_value=self.make_logged_in_probe_state(self.make_supported_netbsd4_compatibility())):
                    with mock.patch("timecapsulesmb.services.activation.probe_managed_runtime_conn", return_value=self.managed_runtime_probe(False)):
                        with mock.patch("timecapsulesmb.services.activation.run_remote_actions") as actions_mock:
                            with mock.patch("timecapsulesmb.services.runtime_verification.probe_managed_runtime_conn", return_value=self.managed_runtime_probe(True)):
                                with redirect_stdout(io.StringIO()):
                                    rc = activate.main(["--yes"])
        self.assertEqual(rc, 0)
        actions_mock.assert_called_once()
        compare.assert_not_called()

    def test_activate_on_a_device_without_an_install_exits_before_running_anything(self) -> None:
        output = io.StringIO()
        values = self.make_valid_env()
        self._installed_config_present.return_value = False
        with mock.patch("timecapsulesmb.cli.activate.load_env_config", return_value=self.make_app_config(values)):
            with mock.patch("timecapsulesmb.services.runtime.probe_managed_connection_state", return_value=self.make_logged_in_probe_state(self.make_supported_netbsd4_compatibility())):
                with mock.patch("timecapsulesmb.services.activation.probe_managed_runtime_conn") as runtime_probe:
                    with mock.patch("timecapsulesmb.services.activation.run_remote_actions") as actions_mock:
                        with redirect_stdout(output):
                            rc = activate.main(["--yes"])

        self.assertEqual(rc, 1)
        self.assertIn("TimeCapsuleSMB is not installed on this device.", output.getvalue())
        # The next step for runtime_not_installed, in the CLI's words.
        self.assertIn("Next steps:\n  1. Run `tcapsule deploy`.", output.getvalue())
        self.assertNotIn("Activating NetBSD 4 payload", output.getvalue())
        runtime_probe.assert_not_called()
        actions_mock.assert_not_called()

    def test_activate_skips_rc_local_when_payload_is_already_healthy(self) -> None:
        output = io.StringIO()
        values = self.make_valid_env()
        with mock.patch("timecapsulesmb.cli.activate.load_env_config", return_value=self.make_app_config(values)):
            with mock.patch("timecapsulesmb.services.runtime.probe_managed_connection_state", return_value=self.make_logged_in_probe_state(self.make_supported_netbsd4_compatibility())):
                with mock.patch("timecapsulesmb.services.activation.probe_managed_runtime_conn", return_value=self.managed_runtime_probe(True)):
                    with mock.patch("timecapsulesmb.services.activation.run_remote_actions") as actions_mock:
                        with mock.patch("timecapsulesmb.services.runtime_verification.probe_managed_runtime_conn") as verify_mock:
                            with redirect_stdout(output):
                                rc = activate.main(["--yes"])
        self.assertEqual(rc, 0)
        actions_mock.assert_not_called()
        verify_mock.assert_not_called()
        self.assertIn("already active; skipping rc.local", output.getvalue())

    def test_activate_returns_nonzero_when_verification_fails(self) -> None:
        output = io.StringIO()
        values = self.make_valid_env()
        with mock.patch("timecapsulesmb.cli.activate.load_env_config", return_value=self.make_app_config(values)):
            with mock.patch("timecapsulesmb.services.runtime.probe_managed_connection_state", return_value=self.make_logged_in_probe_state(self.make_supported_netbsd4_compatibility())):
                with mock.patch("timecapsulesmb.services.activation.probe_managed_runtime_conn", return_value=self.managed_runtime_probe(False)):
                    with mock.patch("timecapsulesmb.services.activation.run_remote_actions"):
                        with mock.patch("timecapsulesmb.services.runtime_verification.probe_managed_runtime_conn", return_value=self.managed_runtime_probe(False)), \
                                mock.patch("timecapsulesmb.services.runtime_verification.read_runtime_log_tails_conn",
                                           return_value={}) as read_logs:
                            with mock.patch("time.sleep") as sleep_mock:
                                with redirect_stdout(output):
                                    rc = activate.main(["--yes"])
        self.assertEqual(rc, 1)
        # The failure sends the device's logs with it.
        read_logs.assert_called_once()
        # Only the verification's own polling sleeps; nothing waits a fixed time first.
        self.assertTrue(all(call.args[0] < 20 for call in sleep_mock.call_args_list))
        self.assertIn("NetBSD 4 activation failed.", output.getvalue())

    def test_activate_dry_run_json_outputs_activation_plan(self) -> None:
        output = io.StringIO()
        values = self.make_valid_env()
        with mock.patch("timecapsulesmb.cli.activate.load_env_config", return_value=self.make_app_config(values)):
            with mock.patch("timecapsulesmb.services.runtime.probe_managed_connection_state", return_value=self.make_logged_in_probe_state(self.make_supported_netbsd4_compatibility())):
                with redirect_stdout(output):
                    rc = activate.main(["--dry-run", "--json"])
        self.assertEqual(rc, 0)
        payload = json.loads(output.getvalue())
        self.assertIn("actions", payload)
        self.assertTrue(all("kind" in action for action in payload["actions"]))
        self.assertEqual(
            payload["pre_activation_probe"],
            {
                "kind": "managed_runtime_ready",
                "if_ready": ["skip_activation_actions"],
                "if_not_ready": ["run_activation_actions", "verify_managed_runtime"],
            },
        )

    def test_activate_json_requires_dry_run(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as raised:
                activate.main(["--json"])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("--json currently requires --dry-run", stderr.getvalue())

    def test_uninstall_dry_run_prints_target_host(self) -> None:
        output = io.StringIO()
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
            "TC_PAYLOAD_DIR_NAME": "samba4",
        }
        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.uninstall.load_env_config", return_value=self.make_app_config(values)))
            mast_mocks = self._patch_mast_volume_flow(stack, "uninstall")
            with redirect_stdout(output):
                rc = uninstall.main(["--dry-run"])
        self.assertEqual(rc, 0)
        text = output.getvalue()
        self.assertIn("Dry run: uninstall plan", text)
        self.assertIn("host: root@10.0.0.2", text)
        self.assertIn("volume roots:\n    resolved from MaSt at uninstall time", text)
        self.assertIn(f"payload dirs:\n    resolved from MaSt at uninstall time/{MANAGED_PAYLOAD_DIR_NAME}", text)
        self.assertIn("request: AirPort ACP reboot (acRB)", text)
        self.assertIn("strategy: network_acp", text)
        self.assertIn("follow-up: wait for the ACP uptime (syUT) to restart, then SSH up", text)
        # The sync after the removal is listed with the remote actions.
        self.assertIn("  rm -rf ", text)
        self.assertLess(text.rindex("  rm -rf "), text.index("  sync\n"))
        self.assertLess(text.index("  sync\n"), text.index("Reboot:"))
        started = self.telemetry_payload("uninstall_started")
        finished = self.telemetry_payload("uninstall_finished")
        self.assertEqual(started["command_id"], finished["command_id"])
        self.assertEqual(finished["result"], "success")
        self.assertEqual(finished["volume_roots"], ["resolved from MaSt at uninstall time"])
        self.assertEqual(finished["payload_dirs"], [f"resolved from MaSt at uninstall time/{MANAGED_PAYLOAD_DIR_NAME}"])
        self.assertEqual(finished["reboot_was_attempted"], False)
        mast_mocks.read_mast_volumes_conn.assert_not_called()
        mast_mocks.mounted_mast_volumes_conn.assert_not_called()

    def test_uninstall_dry_run_no_reboot_matches_no_reboot_execution_path(self) -> None:
        output = io.StringIO()
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
            "TC_PAYLOAD_DIR_NAME": "samba4",
        }
        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.uninstall.load_env_config", return_value=self.make_app_config(values)))
            self._patch_mast_volume_flow(stack, "uninstall")
            with redirect_stdout(output):
                rc = uninstall.main(["--dry-run", "--no-reboot"])
        self.assertEqual(rc, 0)
        text = output.getvalue()
        self.assertIn("Reboot:\n  no", text)
        self.assertIn("Post-uninstall checks:\n  none", text)
        self.assertNotIn("SSH returns after reboot", text)

    def test_uninstall_dry_run_no_wait_matches_no_wait_execution_path(self) -> None:
        output = io.StringIO()
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
        }
        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.uninstall.load_env_config", return_value=self.make_app_config(values)))
            self._patch_mast_volume_flow(stack, "uninstall")
            with redirect_stdout(output):
                rc = uninstall.main(["--dry-run", "--no-wait"])

        self.assertEqual(rc, 0)
        text = output.getvalue()
        self.assertIn("Reboot:\n  yes", text)
        self.assertIn("follow-up: return immediately after reboot request", text)
        self.assertIn("Post-uninstall checks:\n  none", text)
        self.assertNotIn("wait for the ACP uptime", text)

    def test_uninstall_validates_only_host_and_ignores_legacy_payload_dir(self) -> None:
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "",
            "TC_SSH_OPTS": "-o foo",
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_MDNS_HOST_LABEL": "bad host label",
        }
        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.uninstall.load_env_config", return_value=self.make_app_config(values)))
            self._patch_mast_volume_flow(stack, "uninstall")
            with redirect_stdout(io.StringIO()):
                rc = uninstall.main(["--dry-run"])
        self.assertEqual(rc, 0)

    def test_uninstall_without_password_needs_one_only_to_reboot(self) -> None:
        # Key-only SSH can remove the files, but the reboot needs the AirPort password.
        values = {"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "", "TC_SSH_OPTS": "-o foo"}
        for reboot in (True, False):
            with self.subTest(reboot=reboot):
                self.device.calls.clear()
                with ExitStack() as stack:
                    stack.enter_context(mock.patch("timecapsulesmb.cli.uninstall.load_env_config", return_value=self.make_app_config(values)))
                    flow = self._patch_mast_volume_flow(stack, "uninstall")
                    remove = stack.enter_context(mock.patch("timecapsulesmb.cli.uninstall.remote_uninstall_payload"))
                    argv = ["--yes", "--no-input"] + ([] if reboot else ["--no-reboot"])
                    with redirect_stdout(io.StringIO()):
                        if reboot:
                            with self.assertRaises(SystemExit) as raised:
                                uninstall.main(argv)
                        else:
                            rc = uninstall.main(argv)

                if reboot:
                    self.assertIn("TC_PASSWORD is required", str(raised.exception.code))
                    flow.mounted_mast_volumes_conn.assert_not_called()
                    remove.assert_not_called()
                else:
                    self.assertEqual(rc, 0)
                    remove.assert_called_once()
                self.assertEqual(self.device.calls, [])

    def test_uninstall_on_a_device_with_ssh_turned_off_says_so_before_removing_anything(self) -> None:
        # SSH Access turned off, or a reset to factory settings that kept the
        # password: the CLI checks ACPd before the removal without probing SSH
        # first, and says SSH is off instead of failing on an SSH read.
        values = {"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw", "TC_SSH_OPTS": "-o foo"}
        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.uninstall.load_env_config", return_value=self.make_app_config(values)))
            flow = self._patch_mast_volume_flow(stack, "uninstall")
            remove = stack.enter_context(mock.patch("timecapsulesmb.cli.uninstall.remote_uninstall_payload"))
            stack.enter_context(mock.patch("timecapsulesmb.services.runtime.ssh_port_error", return_value="Connection refused"))
            stack.enter_context(mock.patch("timecapsulesmb.checks.network.tcp_connect_error", return_value=None))
            with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as raised:
                uninstall.main(["--yes"])

        message = str(raised.exception.code)
        self.assertTrue(message.startswith("SSH is turned off on the device."), message)
        self.assertIn("tcapsule set-ssh --enable", message)
        flow.mounted_mast_volumes_conn.assert_not_called()
        remove.assert_not_called()

    def test_uninstall_refuses_a_password_the_device_would_reject_before_removing_anything(self) -> None:
        values = {"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw-secretzz", "TC_SSH_OPTS": "-o foo"}
        compare = acp_password_answer(False)
        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.uninstall.load_env_config", return_value=self.make_app_config(values)))
            flow = self._patch_mast_volume_flow(stack, "uninstall")
            remove = stack.enter_context(mock.patch("timecapsulesmb.cli.uninstall.remote_uninstall_payload"))
            stack.enter_context(mock.patch("timecapsulesmb.device.probe.read_airport_acp", compare))
            with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as raised:
                uninstall.main(["--yes"])

        self.assertEqual(str(raised.exception.code), AIRPORT_ADMIN_PASSWORD_REJECTED_MESSAGE + AUTH_FAILED_CLI_STEPS)
        compare.assert_called_once()
        flow.mounted_mast_volumes_conn.assert_not_called()
        remove.assert_not_called()
        self.assertEqual(self.device.calls, [])

    def test_uninstall_without_a_reboot_does_not_compare_the_password(self) -> None:
        values = {"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw-secretzz", "TC_SSH_OPTS": "-o foo"}
        for argv in (["--yes", "--no-reboot"], ["--dry-run"]):
            with self.subTest(argv=argv):
                compare = acp_password_answer(False)
                with ExitStack() as stack:
                    stack.enter_context(mock.patch("timecapsulesmb.cli.uninstall.load_env_config", return_value=self.make_app_config(values)))
                    self._patch_mast_volume_flow(stack, "uninstall")
                    stack.enter_context(mock.patch("timecapsulesmb.cli.uninstall.remote_uninstall_payload"))
                    stack.enter_context(mock.patch("timecapsulesmb.device.probe.read_airport_acp", compare))
                    with redirect_stdout(io.StringIO()):
                        rc = uninstall.main(argv)

                self.assertEqual(rc, 0)
                compare.assert_not_called()

    def test_uninstall_without_a_reboot_waits_for_a_running_disk_check_before_removing_anything(self) -> None:
        # A repair's volume is unmounted: the uninstall would report success
        # with its files still on the disk. No password is needed to see it.
        values = {"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw-secretzz", "TC_SSH_OPTS": "-o foo"}
        compare = acp_password_answer(True)
        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.uninstall.load_env_config", return_value=self.make_app_config(values)))
            flow = self._patch_mast_volume_flow(stack, "uninstall")
            remove = stack.enter_context(mock.patch("timecapsulesmb.cli.uninstall.remote_uninstall_payload"))
            stack.enter_context(mock.patch("timecapsulesmb.device.probe.read_airport_acp", compare))
            stack.enter_context(mock.patch("timecapsulesmb.device.acpd.read_processes",
                                           return_value="  4211 4200 4200 R 0 - 1:12.04 fsck_hfs\ntcapsule-ps: end\n"))
            pause = stack.enter_context(mock.patch("timecapsulesmb.device.acpd.pause"))
            with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as raised:
                uninstall.main(["--yes", "--no-reboot"])

        self.assertTrue(str(raised.exception.code).startswith("A disk check (fsck_hfs) is still running on the device."))
        self.assertIn("Keep the device plugged in and wait.", str(raised.exception.code))
        pause.assert_called_once()
        compare.assert_not_called()
        flow.mounted_mast_volumes_conn.assert_not_called()
        remove.assert_not_called()

    def test_uninstall_without_a_reboot_refuses_a_disk_that_stopped_responding(self) -> None:
        # A repair whose connection dropped, stuck on the disk: the disk check
        # leaves it out, so without this look the uninstall would skip its
        # unmounted volume and report success with the files still there.
        values = {"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw-secretzz", "TC_SSH_OPTS": "-o foo"}
        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.uninstall.load_env_config", return_value=self.make_app_config(values)))
            flow = self._patch_mast_volume_flow(stack, "uninstall")
            remove = stack.enter_context(mock.patch("timecapsulesmb.cli.uninstall.remote_uninstall_payload"))
            stack.enter_context(mock.patch("timecapsulesmb.device.acpd.read_processes",
                                           return_value="  4211 4200 4200 D 127 biowait 1:12.04 fsck_hfs\ntcapsule-ps: end\n"))
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                uninstall.main(["--yes", "--no-reboot"])

        self.assertIn("A disk on the device has stopped responding", str(raised.exception.code))
        self.assertIn("Nothing was changed.", str(raised.exception.code))
        flow.mounted_mast_volumes_conn.assert_not_called()
        remove.assert_not_called()

    def test_uninstall_without_a_reboot_names_a_link_local_interface_that_is_gone(self) -> None:
        # No ACP step looks at port 22 first: the uninstall's own look gives
        # the verdict deploy gives, not ssh's raw "No route to host".
        values = {"TC_HOST": "root@fe80::6e70:9fff:fed0:5f7c%en7", "TC_PASSWORD": "pw-secretzz", "TC_SSH_OPTS": "-o foo"}
        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.uninstall.load_env_config", return_value=self.make_app_config(values)))
            flow = self._patch_mast_volume_flow(stack, "uninstall")
            remove = stack.enter_context(mock.patch("timecapsulesmb.cli.uninstall.remote_uninstall_payload"))
            stack.enter_context(mock.patch("timecapsulesmb.services.runtime.ssh_port_error",
                                           return_value="[Errno 65] No route to host"))
            stack.enter_context(mock.patch("timecapsulesmb.services.runtime.link_local_interface_gone", return_value="en7"))
            # Run as on a Mac, whose messages name "this Mac".
            stack.enter_context(mock.patch("timecapsulesmb.core.install.sys.platform", "darwin"))
            with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as raised:
                uninstall.main(["--yes", "--no-reboot"])

        message = str(raised.exception.code)
        self.assertIn("a network connection this Mac no longer has (en7)", message)
        self.assertNotIn("ssh: connect to host", message)
        flow.mounted_mast_volumes_conn.assert_not_called()
        remove.assert_not_called()

    def test_uninstall_ignores_unsafe_legacy_payload_dir(self) -> None:
        output = io.StringIO()
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "",
            "TC_SSH_OPTS": "-o foo",
            "TC_PAYLOAD_DIR_NAME": "../samba4",
        }
        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.uninstall.load_env_config", return_value=self.make_app_config(values)))
            self._patch_mast_volume_flow(stack, "uninstall")
            with redirect_stdout(output):
                rc = uninstall.main(["--dry-run"])
        self.assertEqual(rc, 0)
        self.assertIn(f"resolved from MaSt at uninstall time/{MANAGED_PAYLOAD_DIR_NAME}", output.getvalue())

    def test_uninstall_json_outputs_plan(self) -> None:
        output = io.StringIO()
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
            "TC_PAYLOAD_DIR_NAME": "samba4",
        }
        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.uninstall.load_env_config", return_value=self.make_app_config(values)))
            self._patch_mast_volume_flow(stack, "uninstall")
            with redirect_stdout(output):
                rc = uninstall.main(["--dry-run", "--json"])
        self.assertEqual(rc, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["host"], "root@10.0.0.2")
        self.assertEqual(payload["volume_roots"], ["resolved from MaSt at uninstall time"])
        self.assertEqual(payload["payload_dirs"], [f"resolved from MaSt at uninstall time/{MANAGED_PAYLOAD_DIR_NAME}"])
        self.assertEqual(
            payload["reboot_request"],
            {
                "mode": "device_reboot",
                "strategy": "network_acp",
                "follow_up": ["wait_for_uptime_reset", "wait_for_ssh_up"],
            },
        )
        self.assertEqual(
            [check["id"] for check in payload["post_uninstall_checks"]],
            [
                "ssh_goes_down_after_reboot",
                "ssh_returns_after_reboot",
                "managed_files_absent",
            ],
        )

    def test_uninstall_no_wait_json_outputs_request_only_plan(self) -> None:
        output = io.StringIO()
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
        }
        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.uninstall.load_env_config", return_value=self.make_app_config(values)))
            self._patch_mast_volume_flow(stack, "uninstall")
            with redirect_stdout(output):
                rc = uninstall.main(["--dry-run", "--json", "--no-wait"])

        self.assertEqual(rc, 0)
        payload = json.loads(output.getvalue())
        self.assertTrue(payload["reboot_required"])
        self.assertFalse(payload["wait_after_reboot"])
        self.assertEqual(payload["reboot_request"]["follow_up"], ["return_after_reboot_request"])
        self.assertEqual(payload["post_uninstall_checks"], [])

    def test_uninstall_yes_reboots_and_verifies(self) -> None:
        output = io.StringIO()
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
            "TC_PAYLOAD_DIR_NAME": "samba4",
        }
        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.uninstall.load_env_config", return_value=self.make_app_config(values)))
            self._patch_mast_volume_flow(stack, "uninstall")
            uninstall_mock = stack.enter_context(mock.patch("timecapsulesmb.cli.uninstall.remote_uninstall_payload"))
            reboot_spy = stack.enter_context(mock.patch("timecapsulesmb.services.maintenance.reboot_device", wraps=reboot_service.reboot_device))
            verify_mock = stack.enter_context(mock.patch("timecapsulesmb.services.maintenance.verify_post_uninstall", return_value=VerificationResult(True, ())))
            with redirect_stdout(output):
                rc = uninstall.main(["--yes"])
        self.assertEqual(rc, 0)
        uninstall_mock.assert_called_once()
        self.assertEqual(reboot_spy.call_args.args, ("root@10.0.0.2", "pw"))
        self.assertTrue(reboot_spy.call_args.kwargs["wait"])
        self.assertNotIn("start_timeout_seconds", reboot_spy.call_args.kwargs)  # the 90 s default
        self.assertNotIn("up_timeout_seconds", reboot_spy.call_args.kwargs)  # the 600 s default
        self.assertEqual(self.device.calls[:3], ["read", "sleep 1", "request"])
        verify_mock.assert_called_once()
        self.assertIn("Device is back online.", output.getvalue())
        finished = self.telemetry_payload("uninstall_finished")
        self.assertEqual(finished["result"], "success")
        self.assertEqual(finished["reboot_was_attempted"], True)
        self.assertEqual(finished["device_came_back_after_reboot"], True)
        self.assertEqual(finished["post_uninstall_verified"], True)

    def test_uninstall_mount_wait_and_no_wait_skip_reboot_observation_and_verify(self) -> None:
        output = io.StringIO()
        values = self.make_valid_env()
        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.uninstall.load_env_config", return_value=self.make_app_config(values)))
            mast_mocks = self._patch_mast_volume_flow(stack, "uninstall")
            stack.enter_context(mock.patch("timecapsulesmb.cli.uninstall.remote_uninstall_payload"))
            verify_mock = stack.enter_context(mock.patch("timecapsulesmb.services.maintenance.verify_post_uninstall"))
            with redirect_stdout(output):
                rc = uninstall.main(["--yes", "--mount-wait", "17", "--no-wait"])

        self.assertEqual(rc, 0)
        self.assertEqual(mast_mocks.mounted_mast_volumes_conn.call_args.kwargs["wait_seconds"], 17)
        self.assertEqual(self.device.calls, ["request"])
        verify_mock.assert_not_called()
        self.assertIn("Post-uninstall verification skipped.", output.getvalue())

    def test_uninstall_no_wait_fails_when_reboot_request_fails(self) -> None:
        output = io.StringIO()
        values = self.make_valid_env()
        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.uninstall.load_env_config", return_value=self.make_app_config(values)))
            self._patch_mast_volume_flow(stack, "uninstall")
            stack.enter_context(mock.patch("timecapsulesmb.cli.uninstall.remote_uninstall_payload"))
            self.device.request_error = ACPConnectionError("Could not connect to ACP on 10.0.0.2:5009: refused")
            verify_mock = stack.enter_context(mock.patch("timecapsulesmb.services.maintenance.verify_post_uninstall"))
            with redirect_stdout(output):
                rc = uninstall.main(["--yes", "--no-wait"])

        self.assertEqual(rc, 1)
        self.assertIn("ACP reboot request failed: Could not connect to ACP on 10.0.0.2:5009: refused", output.getvalue())
        self.assertEqual(self.device.calls, ["request"])
        verify_mock.assert_not_called()
        finished = self.telemetry_payload("uninstall_finished")
        self.assertEqual(finished["result"], "failure")

    def test_uninstall_lost_reboot_reply_continues_when_device_reboots(self) -> None:
        output = io.StringIO()
        values = self.make_valid_env()
        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.uninstall.load_env_config", return_value=self.make_app_config(values)))
            self._patch_mast_volume_flow(stack, "uninstall")
            stack.enter_context(mock.patch("timecapsulesmb.cli.uninstall.remote_uninstall_payload"))
            self.device.request_error = ACPConnectionError("ACP receive failed: timed out")
            verify_mock = stack.enter_context(mock.patch("timecapsulesmb.services.maintenance.verify_post_uninstall", return_value=VerificationResult(True, ())))
            with redirect_stdout(output):
                rc = uninstall.main(["--yes"])

        self.assertEqual(rc, 0)
        self.assertEqual(self.device.calls.count("request"), 1)
        verify_mock.assert_called_once()
        text = output.getvalue()
        self.assertIn("ACP reboot request failed; checking whether the device is restarting anyway...", text)
        self.assertIn("Device is back online.", text)
        finished = self.telemetry_payload("uninstall_finished")
        self.assertEqual(finished["result"], "success")
        self.assertEqual(finished["reboot_was_attempted"], True)
        self.assertEqual(finished["device_came_back_after_reboot"], True)
        self.assertEqual(finished["post_uninstall_verified"], True)

    def test_uninstall_lost_reboot_reply_fails_when_device_never_restarts(self) -> None:
        output = io.StringIO()
        values = self.make_valid_env()
        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.uninstall.load_env_config", return_value=self.make_app_config(values)))
            self._patch_mast_volume_flow(stack, "uninstall")
            stack.enter_context(mock.patch("timecapsulesmb.cli.uninstall.remote_uninstall_payload"))
            self.device.reboots = False
            self.device.request_error = ACPConnectionError("ACP receive failed: timed out")
            verify_mock = stack.enter_context(mock.patch("timecapsulesmb.services.maintenance.verify_post_uninstall"))
            with redirect_stdout(output):
                rc = uninstall.main(["--yes"])

        self.assertEqual(rc, 1)
        self.assertEqual(self.device.calls.count("request"), 1)
        verify_mock.assert_not_called()
        text = output.getvalue()
        self.assertIn("Reboot was requested but the device did not restart.", text)
        self.assertIn(
            "The uninstall removed managed TimeCapsuleSMB files before the reboot.\n\n"
            "Next steps:\n"
            "  1. Power-cycle the device.\n"
            "  2. Try again once the device is reachable.",
            text,
        )
        finished = self.telemetry_payload("uninstall_finished")
        self.assertEqual(finished["result"], "failure")
        self.assertEqual(finished["reboot_was_attempted"], True)
        self.assertEqual(finished["device_came_back_after_reboot"], False)
        self.assertEqual(finished["post_uninstall_verified"], False)
        self.assertIn("stage=wait_for_reboot_down", finished["error"])
        self.assertIn("acp_reboot_succeeded=false", finished["error"])

    def test_uninstall_no_reboot_skips_reboot_and_returns_success(self) -> None:
        output = io.StringIO()
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
            "TC_PAYLOAD_DIR_NAME": "samba4",
        }
        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.uninstall.load_env_config", return_value=self.make_app_config(values)))
            self._patch_mast_volume_flow(stack, "uninstall")
            uninstall_mock = stack.enter_context(mock.patch("timecapsulesmb.cli.uninstall.remote_uninstall_payload"))
            verify_mock = stack.enter_context(mock.patch("timecapsulesmb.services.maintenance.verify_post_uninstall"))
            with redirect_stdout(output):
                rc = uninstall.main(["--no-reboot"])
        self.assertEqual(rc, 0)
        uninstall_mock.assert_called_once()
        self.assertEqual(self.device.calls, [])
        verify_mock.assert_not_called()
        self.assertIn("Skipping reboot.", output.getvalue())

    def test_uninstall_without_mounted_hfs_volumes_removes_flash_and_runtime_only(self) -> None:
        output = io.StringIO()
        values = self.make_valid_env(TC_PAYLOAD_DIR_NAME="samba4")

        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.uninstall.load_env_config", return_value=self.make_app_config(values)))
            self._patch_mast_volume_flow(stack, "uninstall", mounted_volumes=(), read_volumes=(self._mast_volume("dk5", builtin=False),))
            uninstall_mock = stack.enter_context(mock.patch("timecapsulesmb.cli.uninstall.remote_uninstall_payload"))
            with redirect_stdout(output):
                rc = uninstall.main(["--no-reboot"])

        self.assertEqual(rc, 0)
        plan = uninstall_mock.call_args.args[1]
        self.assertEqual(plan.volume_roots, [])
        self.assertEqual(plan.payload_dirs, [])
        self.assertIn("No mounted HFS volumes found; removing flash hooks and runtime state only.", output.getvalue())

    def test_uninstall_declined_reboot_skips_reboot_and_returns_success(self) -> None:
        output = io.StringIO()
        prompt_text = []
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_AIRPORT_SYAP": "120",
            "TC_MDNS_DEVICE_MODEL": "AirPort7,120",
        }

        def fake_input(prompt: str) -> str:
            prompt_text.append(prompt)
            return "n"

        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.uninstall.load_env_config", return_value=self.make_app_config(values)))
            self._patch_mast_volume_flow(stack, "uninstall")
            stack.enter_context(mock.patch("timecapsulesmb.cli.uninstall.remote_uninstall_payload"))
            stack.enter_context(mock.patch("builtins.input", side_effect=fake_input))
            stack.enter_context(
                mock.patch(
                    "timecapsulesmb.cli.context.probe_remote_airport_identity_conn",
                    return_value=SimpleNamespace(model="AirPort7,120", syap="120"),
                )
            )
            with redirect_stdout(output):
                rc = uninstall.main([])
        self.assertEqual(rc, 0)
        self.assertEqual(self.device.calls, [])
        self.assertEqual(prompt_text, ["This will reboot the AirPort Extreme 6th generation now. Continue? [Y/n]: "])
        self.assertIn("Skipped reboot. The AirPort Extreme 6th generation may need a manual reboot", output.getvalue())
        finished = self.telemetry_payload("uninstall_finished")
        self.assertEqual(finished["result"], "success")
        self.assertEqual(finished["reboot_was_attempted"], False)

    def test_uninstall_verify_failure_emits_failure_stage(self) -> None:
        output = io.StringIO()
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
            "TC_PAYLOAD_DIR_NAME": "samba4",
        }
        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.uninstall.load_env_config", return_value=self.make_app_config(values)))
            self._patch_mast_volume_flow(stack, "uninstall")
            # A sync that timed out does not stop the reboot; files left
            # after it carry the timeout in their report.
            stack.enter_context(mock.patch(
                "timecapsulesmb.cli.uninstall.remote_uninstall_payload",
                side_effect=lambda _connection, _plan, *, callbacks: callbacks.measurement(
                    "uninstall_sync", result="timeout", duration_sec=300.0,
                ),
            ))
            stack.enter_context(mock.patch("timecapsulesmb.services.maintenance.verify_post_uninstall", return_value=VerificationResult(False, ())))
            with redirect_stdout(output):
                rc = uninstall.main(["--yes"])
        self.assertEqual(rc, 1)
        self.assertIn("Managed TimeCapsuleSMB files are still present after reboot.", output.getvalue())
        finished = self.telemetry_payload("uninstall_finished")
        self.assertEqual(finished["result"], "failure")
        self.assertEqual(finished["reboot_was_attempted"], True)
        self.assertEqual(finished["device_came_back_after_reboot"], True)
        self.assertEqual(finished["post_uninstall_verified"], False)
        self.assertIn("stage=verify_post_uninstall", finished["error"])
        self.assertEqual(
            finished["execution"]["measurements"]["uninstall_sync"], [{"result": "timeout", "duration_sec": 300.0}],
        )

    def test_fsck_yes_reboots_and_waits_by_default(self) -> None:
        output = io.StringIO()
        values = self.make_valid_env()
        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.fsck.load_env_config", return_value=self.make_app_config(values)))
            self._patch_mast_volume_flow(stack, "fsck", mounted_volumes=(self._mast_volume("dk2"),))
            run_ssh_mock = stack.enter_context(mock.patch("timecapsulesmb.services.maintenance.run_ssh", side_effect=streamed_fsck(CLEAN_FSCK_OUTPUT)))
            reboot_spy = stack.enter_context(mock.patch("timecapsulesmb.services.maintenance.reboot_device", wraps=reboot_service.reboot_device))
            with redirect_stdout(output):
                rc = fsck.main(["--yes"])
        self.assertEqual(rc, 0)
        run_ssh_mock.assert_called_once()
        self.assertEqual(run_ssh_mock.call_args.kwargs["timeout"], maintenance_service.FSCK_REMOTE_COMMAND_TIMEOUT_SECONDS)
        self.assertEqual(maintenance_service.FSCK_REMOTE_COMMAND_TIMEOUT_SECONDS, 10800)
        remote_cmd = run_ssh_mock.call_args.args[1]
        # fsck stops the stack through the same actions deploy uses.
        stop_lines = [
            f"( {command} ) || exit 1"
            for command in render_remote_actions(managed_stop_actions(stop_afpserver=True))
        ]
        script = shlex.split(remote_cmd)[-1]
        # A dropped session must not stop fsck_hfs, so the terminal's hangup
        # is ignored first, and an fsck_hfs still running stops it all.
        self.assertEqual(script.splitlines()[0], "trap '' HUP")
        self.assertIn("\n".join(stop_lines) + "\n", script)
        self.assertLess(script.index(maintenance_service.FSCK_ALREADY_RUNNING_LINE), script.index(stop_lines[0]))
        self.assertTrue(run_ssh_mock.call_args.kwargs["tty"])
        self.assertIn("umount -f /Volumes/dk2", remote_cmd)
        self.assertIn("fsck_hfs -fy /dev/dk2", remote_cmd)
        # The host sends the one ACP reboot request after fsck has reported.
        self.assertEqual(reboot_spy.call_args.args, ("root@10.0.0.2", "pw"))
        self.assertEqual(reboot_spy.call_args.kwargs["start_timeout_seconds"], 120)
        self.assertNotIn("up_timeout_seconds", reboot_spy.call_args.kwargs)  # the 600 s default
        self.assertEqual(self.device.calls.count("request"), 1)
        text = output.getvalue()
        self.assertIn("Mounted HFS volume: /dev/dk2 on /Volumes/dk2", text)
        # The warning comes before the repair starts.
        self.assertLess(text.index(maintenance_service.FSCK_DURATION_WARNING), text.index("--- fsck_hfs /dev/dk2 ---"))
        self.assertIn("Device is back online.", text)
        started = self.telemetry_payload("fsck_started")
        finished = self.telemetry_payload("fsck_finished")
        self.assertEqual(started["command_id"], finished["command_id"])
        self.assertEqual(finished["result"], "success")
        self.assertEqual(finished["reboot_was_attempted"], True)
        self.assertEqual(finished["device_came_back_after_reboot"], True)
        self.assertEqual(finished["fsck_device"], "/dev/dk2")
        self.assertEqual(finished["fsck_mountpoint"], "/Volumes/dk2")

    def test_fsck_warns_how_long_it_can_take_before_asking_to_start(self) -> None:
        output = io.StringIO()
        shown_at_prompt: list[str] = []

        def answer(prompt: str = "") -> str:
            shown_at_prompt.append(output.getvalue() + prompt)
            return "y"

        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.fsck.load_env_config", return_value=self.make_app_config(self.make_valid_env())))
            self._patch_mast_volume_flow(stack, "fsck", mounted_volumes=(self._mast_volume("dk2"),))
            stack.enter_context(mock.patch("builtins.input", side_effect=answer))
            stack.enter_context(mock.patch("timecapsulesmb.services.maintenance.run_ssh", side_effect=streamed_fsck(CLEAN_FSCK_OUTPUT)))
            with redirect_stdout(output):
                rc = fsck.main(["--no-reboot"])

        self.assertEqual(rc, 0)
        [at_prompt] = shown_at_prompt
        self.assertIn(maintenance_service.FSCK_DURATION_WARNING, at_prompt)
        self.assertIn("Continue?", at_prompt)

    # MaSt's disks for the failing-disk warning: dk2 on the internal wd0, a
    # USB disk sd0 beside it.
    FSCK_DISKS = (
        MaStDiskSnapshot("wd0", "", 1907729, True, (MaStPartitionSnapshot("dk2", "Data", "hfs"),)),
        MaStDiskSnapshot("sd0", "", 1907729, False, (MaStPartitionSnapshot("dk5", "USB", "hfs"),)),
    )

    def test_fsck_of_one_disk_is_not_warned_about_anothers_errors(self) -> None:
        # The USB disk's errors before repairing the internal volume: the
        # usual question, not the failing-disk warning.
        errors = ("sd0a: error writing fsbn 8 of 8-15 (sd0 bn 8; cn 0 tn 0 sn 8)",)
        prompts: list[str] = []
        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.fsck.load_env_config", return_value=self.make_app_config(self.make_valid_env())))
            self._patch_mast_volume_flow(stack, "fsck", mounted_volumes=(self._mast_volume("dk2"),))
            stack.enter_context(mock.patch("timecapsulesmb.device.disk_errors.read_disk_errors_conn", return_value=errors))
            stack.enter_context(mock.patch("timecapsulesmb.device.disk_errors.read_disk_inventory_conn",
                                           return_value=self.FSCK_DISKS))
            stack.enter_context(mock.patch("builtins.input", side_effect=lambda prompt="": prompts.append(prompt) or "n"))
            stack.enter_context(mock.patch("timecapsulesmb.services.maintenance.run_ssh"))
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as warning:
                rc = fsck.main(["--no-reboot"])

        self.assertEqual(rc, 0)
        self.assertNotIn("Run fsck anyway? [y/N]: ", prompts)
        self.assertEqual(warning.getvalue(), "")

    def test_fsck_on_a_failing_disk_asks_with_its_warning_instead(self) -> None:
        errors = ("wd0c: error reading fsbn 4 of 4-11 (wd0 bn 4; cn 0 tn 0 sn 4)",)
        prompts: list[str] = []
        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.fsck.load_env_config", return_value=self.make_app_config(self.make_valid_env())))
            self._patch_mast_volume_flow(stack, "fsck", mounted_volumes=(self._mast_volume("dk2"),))
            stack.enter_context(mock.patch("timecapsulesmb.device.disk_errors.read_disk_errors_conn", return_value=errors))
            # MaSt names wd0 as the built-in disk: the errors are its own.
            stack.enter_context(mock.patch("timecapsulesmb.device.disk_errors.read_disk_inventory_conn",
                                           return_value=self.FSCK_DISKS))
            stack.enter_context(mock.patch("builtins.input", side_effect=lambda prompt="": prompts.append(prompt) or "n"))
            run_ssh = stack.enter_context(mock.patch("timecapsulesmb.services.maintenance.run_ssh"))
            with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as warning:
                rc = fsck.main(["--no-reboot"])

        self.assertEqual(rc, 0)
        self.assertEqual(prompts, ["Run fsck anyway? [y/N]: "])
        self.assertIn(f"The internal disk reported errors it could not recover from ({errors[0]}), so it may be failing.",
                      warning.getvalue())
        self.assertIn("`tcapsule wipe-internal-disk` sometimes gets the disk working again", warning.getvalue())
        self.assertIn("A disk repair may not fix this, and works the disk hard.", warning.getvalue())
        self.assertIn("fsck cancelled.", output.getvalue())
        run_ssh.assert_not_called()

    def test_uninstall_on_a_failing_disk_asks_before_removing_anything(self) -> None:
        errors = ("NetBSD IAL (ERROR)  [0 0] device error occured",)
        values = {"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw", "TC_SSH_OPTS": "-o foo"}
        for answer, removed in (("", False), ("y", True)):
            with self.subTest(answer=answer):
                with ExitStack() as stack:
                    stack.enter_context(mock.patch("timecapsulesmb.cli.uninstall.load_env_config", return_value=self.make_app_config(values)))
                    self._patch_mast_volume_flow(stack, "uninstall")
                    stack.enter_context(mock.patch("timecapsulesmb.device.disk_errors.read_disk_errors_conn", return_value=errors))
                    stack.enter_context(mock.patch("timecapsulesmb.device.disk_errors.read_disk_inventory_conn",
                                                   return_value=(MaStDiskSnapshot("sd0", "", 1907729, True, ()),)))
                    stack.enter_context(mock.patch("builtins.input", return_value=answer))
                    remove = stack.enter_context(mock.patch("timecapsulesmb.cli.uninstall.remote_uninstall_payload"))
                    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                        rc = uninstall.main(["--no-reboot"])
                self.assertEqual(rc, 0)
                self.assertEqual(remove.called, removed)

    def test_fsck_validates_only_host(self) -> None:
        output = io.StringIO()
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "",
            "TC_SSH_OPTS": "-o foo",
            "TC_PAYLOAD_DIR_NAME": "../bad",
        }
        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.fsck.load_env_config", return_value=self.make_app_config(values)))
            self._patch_mast_volume_flow(stack, "fsck", mounted_volumes=(self._mast_volume("dk2"),))
            stack.enter_context(mock.patch("timecapsulesmb.services.maintenance.run_ssh", side_effect=streamed_fsck(CLEAN_FSCK_OUTPUT)))
            with redirect_stdout(output):
                rc = fsck.main(["--yes", "--no-reboot"])
        self.assertEqual(rc, 0)

    def test_fsck_reboot_without_password_fails_before_touching_the_disk(self) -> None:
        # Key-only SSH could run fsck, but the reboot needs the AirPort password.
        output = io.StringIO()
        values = {"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "", "TC_SSH_OPTS": "-o foo"}
        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.fsck.load_env_config", return_value=self.make_app_config(values)))
            flow = self._patch_mast_volume_flow(stack, "fsck")
            run_ssh = stack.enter_context(mock.patch("timecapsulesmb.services.maintenance.run_ssh"))
            with redirect_stdout(output), self.assertRaises(SystemExit) as raised:
                fsck.main(["--yes", "--no-input"])

        self.assertIn("TC_PASSWORD is required", str(raised.exception.code))
        flow.mounted_mast_volumes_conn.assert_not_called()
        run_ssh.assert_not_called()
        self.assertEqual(self.device.calls, [])

    def test_fsck_reboot_without_password_prompts_and_reboots_with_it(self) -> None:
        values = {"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "", "TC_SSH_OPTS": "-o foo"}
        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.fsck.load_env_config", return_value=self.make_app_config(values)))
            self._patch_mast_volume_flow(stack, "fsck")
            stack.enter_context(mock.patch("timecapsulesmb.services.maintenance.run_ssh", side_effect=streamed_fsck(CLEAN_FSCK_OUTPUT)))
            prompt = stack.enter_context(mock.patch("timecapsulesmb.cli.runtime.prompt_device_password", return_value="typed"))
            reboot = stack.enter_context(mock.patch("timecapsulesmb.integrations.acp.reboot"))
            with redirect_stdout(io.StringIO()):
                rc = fsck.main(["--yes", "--no-wait"])

        self.assertEqual(rc, 0)
        prompt.assert_called_once()
        reboot.assert_called_once()
        self.assertEqual(reboot.call_args.args[1], "typed")

    def test_fsck_without_a_saved_password_or_input_says_how_to_save_it(self) -> None:
        values = {"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "", "TC_SSH_OPTS": "-o foo"}
        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.fsck.load_env_config", return_value=self.make_app_config(values)))
            flow = self._patch_mast_volume_flow(stack, "fsck")
            stack.enter_context(mock.patch("getpass.getpass", side_effect=EOFError("EOF when reading a line")))
            run_ssh = stack.enter_context(mock.patch("timecapsulesmb.services.maintenance.run_ssh"))
            reboot = stack.enter_context(mock.patch("timecapsulesmb.integrations.acp.reboot"))
            with self.assertRaises(SystemExit) as ctx:
                with redirect_stdout(io.StringIO()):
                    fsck.main(["--yes", "--no-wait"])

        self.assertEqual(ctx.exception.code, DEVICE_PASSWORD_NONINTERACTIVE_MESSAGE)
        flow.mounted_mast_volumes_conn.assert_not_called()
        run_ssh.assert_not_called()
        reboot.assert_not_called()

    def test_fsck_refuses_a_password_the_device_would_reject_before_touching_the_disk(self) -> None:
        # SSH accepts a password right in its first 8 characters; the ACP
        # reboot would not, so the repair never starts.
        values = {"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw-secretzz", "TC_SSH_OPTS": "-o foo"}
        compare = acp_password_answer(False)
        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.fsck.load_env_config", return_value=self.make_app_config(values)))
            flow = self._patch_mast_volume_flow(stack, "fsck")
            run_ssh = stack.enter_context(mock.patch("timecapsulesmb.services.maintenance.run_ssh"))
            stack.enter_context(mock.patch("timecapsulesmb.device.probe.read_airport_acp", compare))
            with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as raised:
                fsck.main(["--yes"])

        self.assertEqual(str(raised.exception.code), AIRPORT_ADMIN_PASSWORD_REJECTED_MESSAGE + AUTH_FAILED_CLI_STEPS)
        self.assertEqual(compare.call_args.args[1], "pw-secretzz")
        flow.mounted_mast_volumes_conn.assert_not_called()
        run_ssh.assert_not_called()
        self.assertEqual(self.device.calls, [])

    def test_fsck_reads_the_password_once_when_it_follows_the_saved_device(self) -> None:
        # The read that looks for the device by its MAC is also the password
        # check before the reboot: a second read would repeat its retries.
        from timecapsulesmb.services.locate import LocateResult

        values = {"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw-secretzz", "TC_SSH_OPTS": "-o foo",
                  "TC_AIRPORT_MAC": "02:00:00:00:00:0a"}
        compare = acp_password_answer(False)
        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.fsck.load_env_config", return_value=self.make_app_config(values)))
            self._patch_mast_volume_flow(stack, "fsck")
            stack.enter_context(mock.patch("timecapsulesmb.services.maintenance.run_ssh"))
            stack.enter_context(mock.patch("timecapsulesmb.device.probe.read_airport_acp", compare))
            stack.enter_context(mock.patch("timecapsulesmb.services.runtime.locate_airport", return_value=LocateResult("not_found")))
            with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as raised:
                fsck.main(["--yes"])

        self.assertEqual(str(raised.exception.code), AIRPORT_ADMIN_PASSWORD_REJECTED_MESSAGE + AUTH_FAILED_CLI_STEPS)
        self.assertEqual(compare.call_count, 1)

    def test_fsck_without_reboot_does_not_compare_the_password(self) -> None:
        values = {"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw-secretzz", "TC_SSH_OPTS": "-o foo"}
        compare = acp_password_answer(False)
        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.fsck.load_env_config", return_value=self.make_app_config(values)))
            self._patch_mast_volume_flow(stack, "fsck")
            stack.enter_context(mock.patch("timecapsulesmb.services.maintenance.run_ssh", side_effect=streamed_fsck(CLEAN_FSCK_OUTPUT)))
            stack.enter_context(mock.patch("timecapsulesmb.device.probe.read_airport_acp", compare))
            with redirect_stdout(io.StringIO()):
                rc = fsck.main(["--yes", "--no-reboot"])

        self.assertEqual(rc, 0)
        compare.assert_not_called()

    def test_fsck_no_wait_requests_reboot_without_waiting(self) -> None:
        output = io.StringIO()
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
        }
        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.fsck.load_env_config", return_value=self.make_app_config(values)))
            self._patch_mast_volume_flow(stack, "fsck", mounted_volumes=(self._mast_volume("dk2"),))
            stack.enter_context(mock.patch("timecapsulesmb.services.maintenance.run_ssh", side_effect=streamed_fsck(CLEAN_FSCK_OUTPUT)))
            with redirect_stdout(output):
                rc = fsck.main(["--yes", "--no-wait"])
        self.assertEqual(rc, 0)
        self.assertEqual(self.device.calls, ["request"])
        self.assertNotIn("File sharing will not start by itself", output.getvalue())

    def test_fsck_no_reboot_omits_reboot_and_waits(self) -> None:
        output = io.StringIO()
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
        }
        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.fsck.load_env_config", return_value=self.make_app_config(values)))
            self._patch_mast_volume_flow(stack, "fsck", mounted_volumes=(self._mast_volume("dk2"),))
            run_ssh_mock = stack.enter_context(mock.patch("timecapsulesmb.services.maintenance.run_ssh", side_effect=streamed_fsck(CLEAN_FSCK_OUTPUT)))
            with redirect_stdout(output):
                rc = fsck.main(["--yes", "--no-reboot"])
        self.assertEqual(rc, 0)
        self.assertEqual(self.device.calls, [])
        self.assertEqual(run_ssh_mock.call_args.kwargs["timeout"], maintenance_service.FSCK_REMOTE_COMMAND_TIMEOUT_SECONDS)

    def test_fsck_prompt_decline_cancels_before_remote_actions(self) -> None:
        output = io.StringIO()
        prompt_text = []
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
            "TC_AIRPORT_SYAP": "120",
            "TC_MDNS_DEVICE_MODEL": "AirPort7,120",
        }
        def fake_input(prompt: str) -> str:
            prompt_text.append(prompt)
            return "n"

        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.fsck.load_env_config", return_value=self.make_app_config(values)))
            self._patch_mast_volume_flow(stack, "fsck", mounted_volumes=(self._mast_volume("dk2"),))
            stack.enter_context(mock.patch("builtins.input", side_effect=fake_input))
            stack.enter_context(
                mock.patch(
                    "timecapsulesmb.cli.context.probe_remote_airport_identity_conn",
                    return_value=SimpleNamespace(model="AirPort7,120", syap="120"),
                )
            )
            run_ssh_mock = stack.enter_context(mock.patch("timecapsulesmb.services.maintenance.run_ssh"))
            with redirect_stdout(output):
                rc = fsck.main([])
        self.assertEqual(rc, 0)
        run_ssh_mock.assert_not_called()
        self.assertEqual(
            prompt_text,
            ["This will stop file sharing, unmount the disk, run fsck_hfs, and reboot the AirPort Extreme 6th generation. Continue? [Y/n]: "],
        )
        self.assertIn("fsck cancelled.", output.getvalue())
        finished = self.telemetry_payload("fsck_finished")
        self.assertEqual(finished["result"], "cancelled")
        self.assertIn("Cancelled by user at fsck confirmation prompt.", finished["error"])

    def test_fsck_no_mounted_hfs_volumes_exits_with_clear_message(self) -> None:
        output = io.StringIO()
        values = self.make_valid_env()
        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.fsck.load_env_config", return_value=self.make_app_config(values)))
            self._patch_mast_volume_flow(stack, "fsck", mounted_volumes=())
            run_ssh_mock = stack.enter_context(mock.patch("timecapsulesmb.services.maintenance.run_ssh"))
            with self.assertRaises(SystemExit) as ctx:
                with redirect_stdout(output):
                    fsck.main(["--yes"])

        self.assertEqual(str(ctx.exception), "no mounted HFS volumes found")
        run_ssh_mock.assert_not_called()
        self.assertNotIn("MaSt", str(ctx.exception))

    def test_fsck_no_input_requires_yes_before_mounting_volumes(self) -> None:
        output = io.StringIO()
        values = self.make_valid_env()
        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.fsck.load_env_config", return_value=self.make_app_config(values)))
            mast_mocks = self._patch_mast_volume_flow(stack, "fsck", mounted_volumes=(self._mast_volume("dk2"),))
            run_ssh_mock = stack.enter_context(mock.patch("timecapsulesmb.services.maintenance.run_ssh"))
            with redirect_stdout(output):
                rc = fsck.main(["--no-input"])

        self.assertEqual(rc, 1)
        self.assertIn("Running `fsck` in non-interactive mode requires `--yes`", output.getvalue())
        mast_mocks.mounted_mast_volumes_conn.assert_not_called()
        run_ssh_mock.assert_not_called()

    def test_fsck_prompts_for_volume_when_multiple_hfs_volumes_are_mounted(self) -> None:
        output = io.StringIO()
        values = self.make_valid_env()
        internal = self._mast_volume("dk2", name="Internal", builtin=True)
        external = self._mast_volume("dk5", disk_device="sd0", name="External", builtin=False)
        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.fsck.load_env_config", return_value=self.make_app_config(values)))
            self._patch_mast_volume_flow(stack, "fsck", mounted_volumes=(internal, external))
            stack.enter_context(mock.patch("builtins.input", side_effect=["2", "y"]))
            run_ssh_mock = stack.enter_context(mock.patch("timecapsulesmb.services.maintenance.run_ssh", side_effect=streamed_fsck(DK5_FSCK_OUTPUT)))
            with redirect_stdout(output):
                rc = fsck.main(["--no-reboot"])

        self.assertEqual(rc, 0)
        remote_cmd = run_ssh_mock.call_args.args[1]
        self.assertIn("umount -f /Volumes/dk5", remote_cmd)
        self.assertIn("fsck_hfs -fy /dev/dk5", remote_cmd)
        text = output.getvalue()
        self.assertIn("Mounted HFS volumes:", text)
        self.assertIn("2. /dev/dk5 on /Volumes/dk5 (External, external)", text)
        self.assertIn("Mounted HFS volume: /dev/dk5 on /Volumes/dk5", text)

    def test_fsck_volume_prompt_without_input_names_the_volume_option(self) -> None:
        values = self.make_valid_env()
        internal = self._mast_volume("dk2", name="Internal", builtin=True)
        external = self._mast_volume("dk5", disk_device="sd0", name="External", builtin=False)

        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.fsck.load_env_config", return_value=self.make_app_config(values)))
            self._patch_mast_volume_flow(stack, "fsck", mounted_volumes=(internal, external))
            stack.enter_context(mock.patch("builtins.input", side_effect=EOFError("EOF when reading a line")))
            run_ssh_mock = stack.enter_context(mock.patch("timecapsulesmb.services.maintenance.run_ssh"))
            reboot = stack.enter_context(mock.patch("timecapsulesmb.integrations.acp.reboot"))
            with self.assertRaises(SystemExit) as ctx:
                with redirect_stdout(io.StringIO()):
                    fsck.main([])

        self.assertEqual(
            ctx.exception.code,
            "No volume was chosen because no input was read. Rerun with --volume, for example: tcapsule fsck --volume dk2",
        )
        run_ssh_mock.assert_not_called()
        reboot.assert_not_called()

    def test_fsck_yes_with_multiple_hfs_volumes_requires_selector(self) -> None:
        output = io.StringIO()
        values = self.make_valid_env()
        internal = self._mast_volume("dk2", name="Internal", builtin=True)
        external = self._mast_volume("dk5", disk_device="sd0", name="External", builtin=False)

        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.fsck.load_env_config", return_value=self.make_app_config(values)))
            self._patch_mast_volume_flow(stack, "fsck", mounted_volumes=(internal, external))
            input_mock = stack.enter_context(mock.patch("builtins.input", side_effect=AssertionError("fsck --yes should not prompt")))
            run_ssh_mock = stack.enter_context(mock.patch("timecapsulesmb.services.maintenance.run_ssh"))
            with self.assertRaises(SystemExit) as ctx:
                with redirect_stdout(output):
                    fsck.main(["--yes", "--no-reboot"])

        self.assertEqual(str(ctx.exception), "multiple mounted HFS volumes found; specify --volume to select one")
        input_mock.assert_not_called()
        run_ssh_mock.assert_not_called()
        self.assertNotIn("Mounted HFS volumes:", output.getvalue())

    def test_fsck_volume_selector_skips_multiple_volume_prompt(self) -> None:
        output = io.StringIO()
        values = self.make_valid_env()
        internal = self._mast_volume("dk2", name="Internal", builtin=True)
        external = self._mast_volume("dk5", disk_device="sd0", name="External", builtin=False)
        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.fsck.load_env_config", return_value=self.make_app_config(values)))
            self._patch_mast_volume_flow(stack, "fsck", mounted_volumes=(internal, external))
            stack.enter_context(mock.patch("builtins.input", side_effect=AssertionError("volume prompt should not run")))
            run_ssh_mock = stack.enter_context(mock.patch("timecapsulesmb.services.maintenance.run_ssh", side_effect=streamed_fsck(DK5_FSCK_OUTPUT)))
            with redirect_stdout(output):
                rc = fsck.main(["--yes", "--no-reboot", "--volume", "dk5"])

        self.assertEqual(rc, 0)
        self.assertIn("fsck_hfs -fy /dev/dk5", run_ssh_mock.call_args.args[1])
        self.assertNotIn("Mounted HFS volumes:", output.getvalue())

    def test_fsck_reboot_no_down_emits_failure_stage(self) -> None:
        output = io.StringIO()
        values = self.make_valid_env()
        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.fsck.load_env_config", return_value=self.make_app_config(values)))
            self._patch_mast_volume_flow(stack, "fsck", mounted_volumes=(self._mast_volume("dk2"),))
            stack.enter_context(mock.patch("timecapsulesmb.services.maintenance.run_ssh", side_effect=streamed_fsck(CLEAN_FSCK_OUTPUT)))
            self.device.reboots = False
            started = self.device.now
            with redirect_stdout(output):
                rc = fsck.main(["--yes"])
        self.assertEqual(rc, 1)
        # fsck gives the reboot two minutes to start.
        self.assertGreaterEqual(self.device.now - started, 120)
        self.assertLess(self.device.now - started, 130)
        self.assertIn("Reboot was requested but the device did not restart.", output.getvalue())
        self.assertEqual(self.device.calls.count("request"), 1)
        finished = self.telemetry_payload("fsck_finished")
        self.assertEqual(finished["result"], "failure")
        self.assertEqual(finished["reboot_was_attempted"], True)
        self.assertEqual(finished["device_came_back_after_reboot"], False)
        self.assertIn("stage=wait_for_reboot_down", finished["error"])

    def test_fsck_reboot_timeout_emits_failure_stage(self) -> None:
        output = io.StringIO()
        values = self.make_valid_env()
        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.fsck.load_env_config", return_value=self.make_app_config(values)))
            self._patch_mast_volume_flow(stack, "fsck", mounted_volumes=(self._mast_volume("dk2"),))
            stack.enter_context(mock.patch("timecapsulesmb.services.maintenance.run_ssh", side_effect=streamed_fsck(CLEAN_FSCK_OUTPUT)))
            self.device.kernel_after = 10_000
            with redirect_stdout(output):
                rc = fsck.main(["--yes"])
        self.assertEqual(rc, 1)
        self.assertIn("Timed out waiting for SSH after reboot.", output.getvalue())
        finished = self.telemetry_payload("fsck_finished")
        self.assertEqual(finished["result"], "failure")
        self.assertEqual(finished["reboot_was_attempted"], True)
        self.assertEqual(finished["device_came_back_after_reboot"], False)
        self.assertIn("stage=wait_for_reboot_up", finished["error"])

    def _run_fsck_with_remote_output(self, stdout: str, returncode: int, argv: list[str]):
        output = io.StringIO()
        values = self.make_valid_env()
        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.fsck.load_env_config", return_value=self.make_app_config(values)))
            self._patch_mast_volume_flow(stack, "fsck", mounted_volumes=(self._mast_volume("dk2"),))
            stack.enter_context(mock.patch("timecapsulesmb.services.maintenance.run_ssh", side_effect=streamed_fsck(stdout, returncode)))
            with redirect_stdout(output):
                rc = fsck.main(argv)
        return rc, output.getvalue(), self.device

    def test_fsck_whose_stop_fails_names_the_stuck_service_and_does_not_restart(self) -> None:
        from timecapsulesmb.services.maintenance import FSCK_STOP_TIMEOUT_MESSAGE

        from timecapsulesmb.app.recovery import cli_steps

        rc, text, device = self._run_fsck_with_remote_output(stuck_stop_output(), 1, ["--yes"])

        self.assertEqual(rc, 1)
        self.assertIn(FSCK_STOP_TIMEOUT_MESSAGE, text)
        # The card's steps, as the app shows them.
        self.assertIn("Next steps:", text)
        self.assertIn(cli_steps("fsck", "manager_stop_timeout")[0], text)
        self.assertNotIn("request", device.calls)

    def test_fsck_prints_next_steps_only_for_a_failure_with_a_code(self) -> None:
        # A session lost while fsck_hfs ran has a card; a plain fsck_hfs status
        # does not, and prints as before.
        from timecapsulesmb.app.recovery import cli_steps

        _rc, dropped, _device = self._run_fsck_with_remote_output("--- fsck_hfs /dev/dk2 ---\n", 255, ["--yes"])
        self.assertIn(cli_steps("fsck", "fsck_session_dropped")[0], dropped)
        _rc, status, _device = self._run_fsck_with_remote_output(
            "--- fsck_hfs /dev/dk2 ---\ntcapsule-fsck: fsck_hfs exit status 8\n", 8, ["--yes", "--no-reboot"])
        self.assertIn("fsck_hfs exited with status 8", status)
        self.assertNotIn("Next steps:", status)

    def test_fsck_without_a_restart_refuses_a_stuck_disk_before_anything_stops(self) -> None:
        from timecapsulesmb.app.recovery import cli_steps
        from timecapsulesmb.device.processes import PS_LISTING_END

        stuck = f"  8115 1 8115 D 127 biowait 0:04.00 smbd\n{PS_LISTING_END}"
        with mock.patch("timecapsulesmb.device.acpd.read_processes", return_value=stuck), \
                self.assertRaises(SystemExit) as raised:
            self._run_fsck_with_remote_output("tcapsule-fsck: fsck_hfs exit status 0\n", 0, ["--yes", "--no-reboot"])

        message = str(raised.exception.code)
        self.assertTrue(message.startswith("A disk on the device has stopped responding"))
        self.assertIn("Nothing was changed.", message)
        self.assertIn(cli_steps("fsck", "disk_not_responding")[0], message)

    def test_uninstall_whose_stop_fails_names_the_stuck_service(self) -> None:
        from timecapsulesmb.app.recovery import cli_steps
        from timecapsulesmb.deploy.executor import UNINSTALL_STOP_TIMEOUT_MESSAGE
        from timecapsulesmb.transport.errors import SshError

        def run_ssh(_connection, command, **_kwargs):
            if "pkill" in command and "mdns-advertiser" in command:
                raise SshError(stuck_stop_output("mdns-advertiser").strip())

        values = {"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw-secretzz", "TC_SSH_OPTS": "-o foo"}
        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.uninstall.load_env_config", return_value=self.make_app_config(values)))
            self._patch_mast_volume_flow(stack, "uninstall")
            stack.enter_context(mock.patch("timecapsulesmb.deploy.executor.run_ssh", side_effect=run_ssh))
            with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as raised:
                uninstall.main(["--yes", "--no-reboot"])

        message = str(raised.exception.code)
        self.assertTrue(message.startswith(UNINSTALL_STOP_TIMEOUT_MESSAGE))
        self.assertIn(cli_steps("uninstall", "manager_stop_timeout")[-1], message)

    def test_uninstall_whose_ssh_login_is_rejected_says_why_by_the_acp_answer(self) -> None:
        # The uninstall's first SSH command is its login: SSH's refusal gets
        # the verdict a probe's would, with that verdict's next steps.
        from timecapsulesmb.app.recovery import cli_steps
        from timecapsulesmb.services.runtime import SSH_PASSWORD_NOT_APPLIED_MESSAGE
        from timecapsulesmb.transport.errors import SshAuthenticationError

        cases = (
            (True, "ssh_password_not_applied", SSH_PASSWORD_NOT_APPLIED_MESSAGE),
            (False, "auth_failed", "AirPort admin password"),
        )
        values = {"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw-secretzz", "TC_SSH_OPTS": "-o foo"}
        for matches, code, wording in cases:
            with self.subTest(acp_accepts=matches), ExitStack() as stack:
                stack.enter_context(mock.patch("timecapsulesmb.cli.uninstall.load_env_config",
                                               return_value=self.make_app_config(values)))
                self._patch_mast_volume_flow(stack, "uninstall")
                acp = stack.enter_context(mock.patch("timecapsulesmb.device.probe.read_airport_acp",
                                                     acp_password_answer(matches)))
                stack.enter_context(mock.patch(
                    "timecapsulesmb.deploy.executor.run_ssh",
                    side_effect=SshAuthenticationError("Permission denied (publickey,password)."),
                ))
                with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as raised:
                    uninstall.main(["--yes", "--no-reboot"])

                message = str(raised.exception.code)
                self.assertIn(wording, message.splitlines()[0])
                self.assertIn(cli_steps("uninstall", code)[0], message)
                self.assertEqual(acp.call_count, 1)

    def test_fsck_failed_status_still_reboots_and_waits_then_fails(self) -> None:
        rc, text, device = self._run_fsck_with_remote_output(
            "--- fsck_hfs /dev/dk2 ---\n** The volume could not be repaired.\n"
            "tcapsule-fsck: fsck_hfs exit status 8\n",
            8,
            ["--yes"],
        )

        self.assertEqual(rc, 1)
        # Rebooting is what brings file sharing back, so the wait still runs.
        self.assertEqual(device.calls[:3], ["read", "sleep 1", "request"])
        self.assertTrue(device.served_new_boot)
        self.assertIn("fsck_hfs exited with status 8; the disk may still need repair.", text)
        finished = self.telemetry_payload("fsck_finished")
        self.assertEqual(finished["result"], "failure")
        self.assertEqual(finished["reboot_was_attempted"], True)
        self.assertEqual(finished["device_came_back_after_reboot"], True)
        self.assertIn("fsck_hfs exited with status 8", finished["error"])

    def test_fsck_that_crashes_names_the_signal_still_reboots_and_fails(self) -> None:
        # Apple's fsck_hfs crashed: the CLI says so and where to repair the
        # volume instead, and the reboot still brings file sharing back.
        rc, text, device = self._run_fsck_with_remote_output(
            "--- fsck_hfs /dev/dk2 ---\n** Checking Catalog file.\n"
            "tcapsule-fsck: fsck_hfs exit status 138\n",
            138,
            ["--yes"],
        )

        self.assertEqual(rc, 1)
        self.assertTrue(device.served_new_boot)
        self.assertIn("fsck_hfs crashed with SIGBUS (exit status 138) before it finished.", text)
        self.assertIn("run First Aid in Disk Utility", text)
        self.assertNotIn("the disk may still need repair", text)
        finished = self.telemetry_payload("fsck_finished")
        self.assertEqual(finished["result"], "failure")
        self.assertIn("fsck_hfs crashed with SIGBUS", finished["error"])

    def test_fsck_on_netbsd4_starts_file_sharing_after_the_reboot(self) -> None:
        self.use_stock_netbsd4()
        runtime = FakeInstalledRuntime()
        with runtime.patched():
            rc, text, _device = self._run_fsck_with_remote_output("tcapsule-fsck: fsck_hfs exit status 0\n", 0, ["--yes"])

        self.assertEqual(rc, 0)
        self.assertIn("run /mnt/Flash/rc.local", runtime.calls)
        self.assertIn("File sharing is running again after the reboot.", text)
        finished = self.telemetry_payload("fsck_finished")
        self.assertEqual(finished["result"], "success")
        self.assertEqual(finished["runtime_start_after_reboot"], "rc_local")
        self.assertEqual(finished["runtime_restarted"], True)
        # The probe before the reboot gives fsck telemetry the device family too.
        self.assertEqual(finished["device_family"], "netbsd4le_samba4")

    def test_fsck_no_wait_on_stock_netbsd4_says_to_activate_once_it_is_back(self) -> None:
        self.use_stock_netbsd4()
        with FakeInstalledRuntime().patched():
            rc, text, device = self._run_fsck_with_remote_output("tcapsule-fsck: fsck_hfs exit status 0\n", 0, ["--yes", "--no-wait"])

        self.assertEqual(rc, 0)
        self.assertEqual(device.calls, ["request"])
        self.assertIn("File sharing will not start by itself after this restart.", text)
        self.assertIn("tcapsule activate", text)

    def test_fsck_on_netbsd4_fails_when_file_sharing_does_not_restart(self) -> None:
        self.use_stock_netbsd4()
        with FakeInstalledRuntime(becomes_ready=False).patched():
            rc, text, _device = self._run_fsck_with_remote_output("tcapsule-fsck: fsck_hfs exit status 0\n", 0, ["--yes"])

        self.assertEqual(rc, 1)
        self.assertIn("Disk repair completed. File sharing did not restart after the reboot.", text)
        # The app's runtime_not_restarted steps.
        self.assertIn("Run `tcapsule activate`.", text)
        finished = self.telemetry_payload("fsck_finished")
        self.assertEqual(finished["result"], "failure")
        self.assertIn("stage=verify_runtime_activation", finished["error"])

    def test_fsck_on_netbsd4_whose_start_meets_a_stuck_service_says_to_unplug_then_activate(self) -> None:
        from timecapsulesmb.app.recovery import cli_steps

        self.use_stock_netbsd4()
        with FakeInstalledRuntime(stuck_stop=True).patched():
            rc, text, _device = self._run_fsck_with_remote_output("tcapsule-fsck: fsck_hfs exit status 0\n", 0, ["--yes"])

        self.assertEqual(rc, 1)
        self.assertIn("Disk repair completed.", text)
        for step in cli_steps("fsck", "manager_stop_timeout", stage="post_reboot_activation"):
            self.assertIn(step, text)
        self.assertIn("`tcapsule activate`", text)

    def test_fsck_that_cannot_probe_the_device_stops_before_touching_the_disk(self) -> None:
        # Without the probe fsck cannot know whether file sharing must be
        # started after the reboot; it fails before the repair, not after.
        self.fsck_probe_state = self.make_probe_state(ProbeResult(
            ssh_status=SshAccessStatus.AUTH_REJECTED,
            error="SSH authentication failed.",
            os_name="",
            os_release="",
            arch="",
            elf_endianness="unknown",
        ))
        output = io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(mock.patch(
                "timecapsulesmb.cli.fsck.load_env_config", return_value=self.make_app_config(self.make_valid_env())))
            self._patch_mast_volume_flow(stack, "fsck", mounted_volumes=(self._mast_volume("dk2"),))
            run_ssh = stack.enter_context(mock.patch("timecapsulesmb.services.maintenance.run_ssh"))
            with redirect_stdout(output), self.assertRaises(SystemExit) as raised:
                fsck.main(["--yes"])

        # The device's AirPort service takes the password SSH rejected: the
        # device has not applied its settings, with ssh's text as the facts.
        from timecapsulesmb.services.runtime import SSH_PASSWORD_NOT_APPLIED_MESSAGE

        self.assertTrue(str(raised.exception).startswith(f"{SSH_PASSWORD_NOT_APPLIED_MESSAGE}\nSSH authentication failed."))
        self.assertIn("Try restarting the device in AirPort Utility", str(raised.exception))
        run_ssh.assert_not_called()
        self.assertNotIn("request", self.device.calls)
        self.assertEqual(self.telemetry_payload("fsck_finished")["result"], "failure")

    def test_fsck_failed_status_fails_without_reboot_or_wait(self) -> None:
        for argv, requested in ((["--yes", "--no-reboot"], False), (["--yes", "--no-wait"], True)):
            with self.subTest(argv=argv):
                self.device.calls.clear()
                rc, text, device = self._run_fsck_with_remote_output(
                    "tcapsule-fsck: fsck_hfs exit status 8\n", 8, argv,
                )

                self.assertEqual(rc, 1)
                self.assertEqual(device.calls, ["request"] if requested else [])
                self.assertIn("fsck_hfs exited with status 8", text)
                self.assertEqual(self.telemetry_payload("fsck_finished")["result"], "failure")

    def test_fsck_failed_status_survives_a_rejected_no_wait_reboot(self) -> None:
        self.device.request_error = ACPConnectionError("refused")
        rc, text, device = self._run_fsck_with_remote_output(
            "tcapsule-fsck: fsck_hfs exit status 8\n", 8, ["--yes", "--no-wait"],
        )

        self.assertEqual(rc, 1)
        self.assertEqual(device.calls, ["request"])
        # Both the reboot failure and the repair failure reach the user.
        self.assertIn("ACP reboot request failed: refused", text)
        self.assertIn("fsck_hfs exited with status 8; the disk may still need repair.", text)
        finished = self.telemetry_payload("fsck_finished")
        self.assertEqual(finished["result"], "failure")
        self.assertIn("ACP reboot request failed", finished["error"])
        self.assertIn("fsck_hfs exited with status 8", finished["error"])

    def test_fsck_without_status_line_fails_and_skips_reboot(self) -> None:
        # A process that would not stop aborts the script before fsck, so no
        # status line arrives and the host requests no reboot.
        rc, text, device = self._run_fsck_with_remote_output("process smbd did not stop\n", 1, ["--yes"])

        self.assertEqual(rc, 1)
        self.assertEqual(device.calls, [])
        self.assertIn("fsck did not run", text)
        finished = self.telemetry_payload("fsck_finished")
        self.assertEqual(finished["result"], "failure")
        self.assertEqual(finished["reboot_was_attempted"], False)

    def test_fsck_on_a_volume_that_stayed_mounted_names_the_unmount(self) -> None:
        # The volume was still mounted after umount, so the script stopped
        # before fsck_hfs and the host requests no reboot.
        rc, text, device = self._run_fsck_with_remote_output(
            "umount: /Volumes/Data: Device busy\ntcapsule-fsck: volume not unmounted\n", 1, ["--yes"])

        self.assertEqual(rc, 1)
        self.assertEqual(device.calls, [])
        self.assertIn("could not be confirmed unmounted", text)
        finished = self.telemetry_payload("fsck_finished")
        self.assertEqual(finished["result"], "failure")
        self.assertEqual(finished["reboot_was_attempted"], False)

    def test_fsck_status_line_wins_over_ssh_exit_status(self) -> None:
        # With --no-reboot the session ends normally, but a stale nonzero SSH
        # status must not override a clean fsck status line, nor vice versa.
        rc, _, _ = self._run_fsck_with_remote_output("tcapsule-fsck: fsck_hfs exit status 0\r\n", 255, ["--yes", "--no-reboot"])
        self.assertEqual(rc, 0)
        rc, _, _ = self._run_fsck_with_remote_output("tcapsule-fsck: fsck_hfs exit status 3\n", 0, ["--yes", "--no-reboot"])
        self.assertEqual(rc, 1)

    def test_fsck_no_reboot_confirmation_says_file_sharing_stays_off(self) -> None:
        prompts = []
        values = self.make_valid_env()
        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.fsck.load_env_config", return_value=self.make_app_config(values)))
            self._patch_mast_volume_flow(stack, "fsck", mounted_volumes=(self._mast_volume("dk2"),))
            stack.enter_context(mock.patch("builtins.input", side_effect=lambda prompt: prompts.append(prompt) or "n"))
            run_ssh_mock = stack.enter_context(mock.patch("timecapsulesmb.services.maintenance.run_ssh"))
            with redirect_stdout(io.StringIO()):
                rc = fsck.main(["--no-reboot"])

        self.assertEqual(rc, 0)
        run_ssh_mock.assert_not_called()
        self.assertEqual(len(prompts), 1)
        self.assertIn("run fsck_hfs. File sharing stays off until the", prompts[0])
        self.assertNotIn("reboot", prompts[0])


if __name__ == "__main__":
    unittest.main()


class FsckStillRunningNoticeTests(unittest.TestCase):
    """The CLI's line for each silent minute of fsck_hfs."""

    def notice(self):
        printed: list[str] = []
        clock = [0.0]
        notice = fsck.FsckStillRunningNotice(printed.append, now=lambda: clock[0])
        return notice, printed, clock

    def test_a_silent_minute_of_fsck_prints_how_long_it_has_run(self) -> None:
        notice, printed, clock = self.notice()
        notice.log("--- fsck_hfs /dev/dk2 ---")
        notice.log("** Checking Catalog file.")
        for clock[0] in (30.0, 59.0):
            notice.tick()
        self.assertEqual(printed, ["--- fsck_hfs /dev/dk2 ---", "** Checking Catalog file."])
        clock[0] = 60.0
        notice.tick()
        clock[0] = 90.0
        notice.tick()
        clock[0] = 125.0
        notice.tick()
        self.assertEqual(printed[2:], ["fsck_hfs is still running (1 min).", "fsck_hfs is still running (2 min)."])

    def test_a_new_line_restarts_the_silent_minute(self) -> None:
        notice, printed, clock = self.notice()
        notice.log("--- fsck_hfs /dev/dk2 ---")
        clock[0] = 50.0
        notice.log("** Checking volume bitmap.")
        clock[0] = 100.0
        notice.tick()
        self.assertEqual(printed[-1], "** Checking volume bitmap.")
        clock[0] = 110.0
        notice.tick()
        # Minutes count from when fsck_hfs started, not from the last line.
        self.assertEqual(printed[-1], "fsck_hfs is still running (1 min).")

    def test_nothing_is_printed_before_fsck_starts_or_after_it_reports(self) -> None:
        notice, printed, clock = self.notice()
        notice.log("Stopping file sharing")
        clock[0] = 600.0
        notice.tick()
        notice.log("--- fsck_hfs /dev/dk2 ---")
        notice.log("tcapsule-fsck: fsck_hfs exit status 0")
        clock[0] = 1200.0
        notice.tick()
        self.assertEqual(printed, [
            "Stopping file sharing", "--- fsck_hfs /dev/dk2 ---", "tcapsule-fsck: fsck_hfs exit status 0",
        ])

    def test_its_thread_stops_with_the_context(self) -> None:
        notice, _printed, _clock = self.notice()
        started = time.monotonic()
        with notice:
            self.assertTrue(notice._thread.is_alive())
        self.assertFalse(notice._thread.is_alive())
        self.assertLess(time.monotonic() - started, 5)

    def test_cli_progress_prints_only_file_counts(self) -> None:
        context = mock.Mock(_last_progress_print=None)
        output = io.StringIO()
        with redirect_stdout(output):
            CommandContext.print_progress(context, "run_fsck", phase="catalog", recheck=False)
            CommandContext.print_progress(context, "migrate_xattrs_copy", entries=1200, now=lambda: 5.0)
        self.assertEqual(output.getvalue(), "  1,200 files checked\n")
