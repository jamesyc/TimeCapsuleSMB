"""The activate, uninstall and fsck commands."""
from __future__ import annotations

import io
from dataclasses import replace
import shlex
import subprocess
import json
import unittest
from contextlib import ExitStack
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace
from unittest import mock
from timecapsulesmb.cli import activate, fsck, uninstall
from timecapsulesmb.cli.main import main
from timecapsulesmb.cli.runtime import DEVICE_PASSWORD_NONINTERACTIVE_MESSAGE, NonInteractivePromptError
from timecapsulesmb.integrations.acp import ACPConnectionError
from timecapsulesmb.services import maintenance as maintenance_service
from timecapsulesmb.services import reboot as reboot_service
from timecapsulesmb.services.runtime import AIRPORT_PASSWORD_MISMATCH_MESSAGE
from timecapsulesmb.core.config import MANAGED_PAYLOAD_DIR_NAME
from timecapsulesmb.device.probe import ProbeResult, SshAccessStatus
from timecapsulesmb.device.storage import MaStVolume
from timecapsulesmb.deploy.commands import (
    RunScriptAction,
    render_remote_action,
    StopProcessAction,
    managed_stop_actions,
    render_remote_actions,
)
from timecapsulesmb.deploy.verify import VerificationResult

from tests.cli_support import CliTestCase, FakeCommandContext
from tests.reboot_support import FakeInstalledRuntime


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
        self.assertIn("Dry run: NetBSD4 activation plan", text)
        for action in managed_stop_actions(stop_afpserver=False):
            self.assertIn(render_remote_action(action), text)
        # Activation does not reboot, and ACPd never respawns afpserver.
        self.assertNotIn(render_remote_action(StopProcessAction("afpserver")), text)
        self.assertIn("/bin/sh /mnt/Flash/rc.local", text)
        self.assertIn("skip rc.local if the NetBSD4 payload is already healthy", text)
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
        self.assertIn("only supported for NetBSD4", str(cm.exception))

    def test_activate_prompt_decline_cancels_before_remote_actions(self) -> None:
        output = io.StringIO()
        command_context = FakeCommandContext(compatibility=self.make_supported_netbsd4_compatibility())
        values = self.make_valid_env()
        with mock.patch("timecapsulesmb.cli.activate.load_env_config", return_value=self.make_app_config(values)):
            with mock.patch("timecapsulesmb.cli.context.CommandContext.require_compatibility", return_value=self.make_supported_netbsd4_compatibility()):
                with mock.patch("builtins.input", return_value="n"):
                    with mock.patch("timecapsulesmb.services.activation.run_remote_actions") as actions_mock:
                        with mock.patch("timecapsulesmb.cli.activate.CommandContext", return_value=command_context):
                            with redirect_stdout(output):
                                rc = activate.main([])
        self.assertEqual(rc, 0)
        actions_mock.assert_not_called()
        text = output.getvalue()
        self.assertIn("This will start the deployed Samba payload on the AirPort storage device.", text)
        self.assertIn("Activation cancelled.", text)
        command_context.finish.assert_called_once()
        self.assertEqual(command_context.finish.call_args.kwargs["result"], "cancelled")
        self.assertIn("Cancelled by user at NetBSD4 activation confirmation prompt.", command_context.finish.call_args.kwargs["error"])

    def test_activate_prompt_eof_reports_non_interactive_error(self) -> None:
        output = io.StringIO()
        command_context = FakeCommandContext(compatibility=self.make_supported_netbsd4_compatibility())
        values = self.make_valid_env()
        with mock.patch("timecapsulesmb.cli.activate.load_env_config", return_value=self.make_app_config(values)):
            with mock.patch("timecapsulesmb.cli.context.CommandContext.require_compatibility", return_value=self.make_supported_netbsd4_compatibility()):
                with mock.patch("builtins.input", side_effect=EOFError):
                    with mock.patch("timecapsulesmb.services.activation.run_remote_actions") as actions_mock:
                        with mock.patch("timecapsulesmb.cli.activate.CommandContext", return_value=command_context):
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
                        with mock.patch("timecapsulesmb.cli.activate.CommandContext", return_value=command_context):
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
        compare = mock.Mock(return_value=subprocess.CompletedProcess(["ssh"], 1, b"", b""))
        values = self.make_valid_env()
        with mock.patch("timecapsulesmb.device.probe.run_ssh_input", compare):
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
        self.assertIn("Install / Update Samba", output.getvalue())
        self.assertNotIn("Activating NetBSD4 payload", output.getvalue())
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
                        with mock.patch("timecapsulesmb.services.runtime_verification.probe_managed_runtime_conn", return_value=self.managed_runtime_probe(False)):
                            with mock.patch("time.sleep") as sleep_mock:
                                with redirect_stdout(output):
                                    rc = activate.main(["--yes"])
        self.assertEqual(rc, 1)
        # Only the verification's own polling sleeps; nothing waits a fixed time first.
        self.assertTrue(all(call.args[0] < 20 for call in sleep_mock.call_args_list))
        self.assertIn("NetBSD4 activation failed.", output.getvalue())

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

    def test_uninstall_refuses_a_password_the_device_would_reject_before_removing_anything(self) -> None:
        values = {"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw-secretzz", "TC_SSH_OPTS": "-o foo"}
        compare = self.sypw_answer(1)
        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.uninstall.load_env_config", return_value=self.make_app_config(values)))
            flow = self._patch_mast_volume_flow(stack, "uninstall")
            remove = stack.enter_context(mock.patch("timecapsulesmb.cli.uninstall.remote_uninstall_payload"))
            stack.enter_context(mock.patch("timecapsulesmb.device.probe.run_ssh_input", compare))
            with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as raised:
                uninstall.main(["--yes"])

        self.assertEqual(str(raised.exception.code), AIRPORT_PASSWORD_MISMATCH_MESSAGE)
        compare.assert_called_once()
        flow.mounted_mast_volumes_conn.assert_not_called()
        remove.assert_not_called()
        self.assertEqual(self.device.calls, [])

    def test_uninstall_without_a_reboot_does_not_compare_the_password(self) -> None:
        values = {"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw-secretzz", "TC_SSH_OPTS": "-o foo"}
        for argv in (["--yes", "--no-reboot"], ["--dry-run"]):
            with self.subTest(argv=argv):
                compare = self.sypw_answer(1)
                with ExitStack() as stack:
                    stack.enter_context(mock.patch("timecapsulesmb.cli.uninstall.load_env_config", return_value=self.make_app_config(values)))
                    self._patch_mast_volume_flow(stack, "uninstall")
                    stack.enter_context(mock.patch("timecapsulesmb.cli.uninstall.remote_uninstall_payload"))
                    stack.enter_context(mock.patch("timecapsulesmb.device.probe.run_ssh_input", compare))
                    with redirect_stdout(io.StringIO()):
                        rc = uninstall.main(argv)

                self.assertEqual(rc, 0)
                compare.assert_not_called()

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
        self.assertIn("The uninstall removed managed TimeCapsuleSMB files before reboot; power-cycle or rerun uninstall.", text)
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
            stack.enter_context(mock.patch("timecapsulesmb.cli.uninstall.remote_uninstall_payload"))
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

    def test_fsck_yes_reboots_and_waits_by_default(self) -> None:
        output = io.StringIO()
        values = self.make_valid_env()
        run_result = mock.Mock(stdout="--- fsck_hfs /dev/dk2 ---\nOK\ntcapsule-fsck: fsck_hfs exit status 0\n", returncode=0)
        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.fsck.load_env_config", return_value=self.make_app_config(values)))
            self._patch_mast_volume_flow(stack, "fsck", mounted_volumes=(self._mast_volume("dk2"),))
            run_ssh_mock = stack.enter_context(mock.patch("timecapsulesmb.services.maintenance.run_ssh", return_value=run_result))
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
        self.assertTrue(shlex.split(remote_cmd)[-1].startswith("\n".join(stop_lines) + "\n"))
        self.assertIn("umount -f /Volumes/dk2", remote_cmd)
        self.assertIn("fsck_hfs -fy /dev/dk2", remote_cmd)
        # The host sends the one ACP reboot request after fsck has reported.
        self.assertEqual(reboot_spy.call_args.args, ("root@10.0.0.2", "pw"))
        self.assertEqual(reboot_spy.call_args.kwargs["start_timeout_seconds"], 120)
        self.assertNotIn("up_timeout_seconds", reboot_spy.call_args.kwargs)  # the 600 s default
        self.assertEqual(self.device.calls.count("request"), 1)
        text = output.getvalue()
        self.assertIn("Mounted HFS volume: /dev/dk2 on /Volumes/dk2", text)
        self.assertIn("--- fsck_hfs /dev/dk2 ---", text)
        self.assertIn("Device is back online.", text)
        started = self.telemetry_payload("fsck_started")
        finished = self.telemetry_payload("fsck_finished")
        self.assertEqual(started["command_id"], finished["command_id"])
        self.assertEqual(finished["result"], "success")
        self.assertEqual(finished["reboot_was_attempted"], True)
        self.assertEqual(finished["device_came_back_after_reboot"], True)
        self.assertEqual(finished["fsck_device"], "/dev/dk2")
        self.assertEqual(finished["fsck_mountpoint"], "/Volumes/dk2")

    def test_fsck_validates_only_host(self) -> None:
        output = io.StringIO()
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "",
            "TC_SSH_OPTS": "-o foo",
            "TC_PAYLOAD_DIR_NAME": "../bad",
        }
        run_result = mock.Mock(stdout="--- fsck_hfs /dev/dk2 ---\nOK\ntcapsule-fsck: fsck_hfs exit status 0\n", returncode=0)
        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.fsck.load_env_config", return_value=self.make_app_config(values)))
            self._patch_mast_volume_flow(stack, "fsck", mounted_volumes=(self._mast_volume("dk2"),))
            stack.enter_context(mock.patch("timecapsulesmb.services.maintenance.run_ssh", return_value=run_result))
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
        run_result = mock.Mock(stdout="--- fsck_hfs /dev/dk2 ---\nOK\ntcapsule-fsck: fsck_hfs exit status 0\n", returncode=0)
        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.fsck.load_env_config", return_value=self.make_app_config(values)))
            self._patch_mast_volume_flow(stack, "fsck")
            stack.enter_context(mock.patch("timecapsulesmb.services.maintenance.run_ssh", return_value=run_result))
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

    def sypw_answer(self, returncode: int) -> mock.Mock:
        # The device's comparison of the saved password with syPW: 0 match, 1 mismatch.
        return mock.Mock(return_value=subprocess.CompletedProcess(["ssh"], returncode, b"", b""))

    def test_fsck_refuses_a_password_the_device_would_reject_before_touching_the_disk(self) -> None:
        # SSH accepts a password right in its first 8 characters; the ACP
        # reboot would not, so the repair never starts.
        values = {"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw-secretzz", "TC_SSH_OPTS": "-o foo"}
        compare = self.sypw_answer(1)
        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.fsck.load_env_config", return_value=self.make_app_config(values)))
            flow = self._patch_mast_volume_flow(stack, "fsck")
            run_ssh = stack.enter_context(mock.patch("timecapsulesmb.services.maintenance.run_ssh"))
            stack.enter_context(mock.patch("timecapsulesmb.device.probe.run_ssh_input", compare))
            with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as raised:
                fsck.main(["--yes"])

        self.assertEqual(str(raised.exception.code), AIRPORT_PASSWORD_MISMATCH_MESSAGE)
        self.assertEqual(compare.call_args.kwargs["input_bytes"], b"pw-secretzz")
        flow.mounted_mast_volumes_conn.assert_not_called()
        run_ssh.assert_not_called()
        self.assertEqual(self.device.calls, [])

    def test_fsck_without_reboot_does_not_compare_the_password(self) -> None:
        values = {"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw-secretzz", "TC_SSH_OPTS": "-o foo"}
        run_result = mock.Mock(stdout="--- fsck_hfs /dev/dk2 ---\nOK\ntcapsule-fsck: fsck_hfs exit status 0\n", returncode=0)
        compare = self.sypw_answer(1)
        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.fsck.load_env_config", return_value=self.make_app_config(values)))
            self._patch_mast_volume_flow(stack, "fsck")
            stack.enter_context(mock.patch("timecapsulesmb.services.maintenance.run_ssh", return_value=run_result))
            stack.enter_context(mock.patch("timecapsulesmb.device.probe.run_ssh_input", compare))
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
        run_result = mock.Mock(stdout="--- fsck_hfs /dev/dk2 ---\nOK\ntcapsule-fsck: fsck_hfs exit status 0\n", returncode=0)
        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.fsck.load_env_config", return_value=self.make_app_config(values)))
            self._patch_mast_volume_flow(stack, "fsck", mounted_volumes=(self._mast_volume("dk2"),))
            stack.enter_context(mock.patch("timecapsulesmb.services.maintenance.run_ssh", return_value=run_result))
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
        run_result = mock.Mock(stdout="--- fsck_hfs /dev/dk2 ---\nOK\ntcapsule-fsck: fsck_hfs exit status 0\n", returncode=0)
        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.fsck.load_env_config", return_value=self.make_app_config(values)))
            self._patch_mast_volume_flow(stack, "fsck", mounted_volumes=(self._mast_volume("dk2"),))
            run_ssh_mock = stack.enter_context(mock.patch("timecapsulesmb.services.maintenance.run_ssh", return_value=run_result))
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
        run_result = mock.Mock(stdout="--- fsck_hfs /dev/dk5 ---\nOK\ntcapsule-fsck: fsck_hfs exit status 0\n", returncode=0)

        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.fsck.load_env_config", return_value=self.make_app_config(values)))
            self._patch_mast_volume_flow(stack, "fsck", mounted_volumes=(internal, external))
            stack.enter_context(mock.patch("builtins.input", side_effect=["2", "y"]))
            run_ssh_mock = stack.enter_context(mock.patch("timecapsulesmb.services.maintenance.run_ssh", return_value=run_result))
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
        run_result = mock.Mock(stdout="--- fsck_hfs /dev/dk5 ---\nOK\ntcapsule-fsck: fsck_hfs exit status 0\n", returncode=0)

        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.fsck.load_env_config", return_value=self.make_app_config(values)))
            self._patch_mast_volume_flow(stack, "fsck", mounted_volumes=(internal, external))
            stack.enter_context(mock.patch("builtins.input", side_effect=AssertionError("volume prompt should not run")))
            run_ssh_mock = stack.enter_context(mock.patch("timecapsulesmb.services.maintenance.run_ssh", return_value=run_result))
            with redirect_stdout(output):
                rc = fsck.main(["--yes", "--no-reboot", "--volume", "dk5"])

        self.assertEqual(rc, 0)
        self.assertIn("fsck_hfs -fy /dev/dk5", run_ssh_mock.call_args.args[1])
        self.assertNotIn("Mounted HFS volumes:", output.getvalue())

    def test_fsck_reboot_no_down_emits_failure_stage(self) -> None:
        output = io.StringIO()
        values = self.make_valid_env()
        run_result = mock.Mock(stdout="--- fsck_hfs /dev/dk2 ---\nOK\ntcapsule-fsck: fsck_hfs exit status 0\n", returncode=0)
        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.fsck.load_env_config", return_value=self.make_app_config(values)))
            self._patch_mast_volume_flow(stack, "fsck", mounted_volumes=(self._mast_volume("dk2"),))
            stack.enter_context(mock.patch("timecapsulesmb.services.maintenance.run_ssh", return_value=run_result))
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
        run_result = mock.Mock(stdout="--- fsck_hfs /dev/dk2 ---\nOK\ntcapsule-fsck: fsck_hfs exit status 0\n", returncode=0)
        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.fsck.load_env_config", return_value=self.make_app_config(values)))
            self._patch_mast_volume_flow(stack, "fsck", mounted_volumes=(self._mast_volume("dk2"),))
            stack.enter_context(mock.patch("timecapsulesmb.services.maintenance.run_ssh", return_value=run_result))
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
        run_result = mock.Mock(stdout=stdout, returncode=returncode)
        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.fsck.load_env_config", return_value=self.make_app_config(values)))
            self._patch_mast_volume_flow(stack, "fsck", mounted_volumes=(self._mast_volume("dk2"),))
            stack.enter_context(mock.patch("timecapsulesmb.services.maintenance.run_ssh", return_value=run_result))
            with redirect_stdout(output):
                rc = fsck.main(argv)
        return rc, output.getvalue(), self.device

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
        finished = self.telemetry_payload("fsck_finished")
        self.assertEqual(finished["result"], "failure")
        self.assertIn("stage=verify_runtime_activation", finished["error"])

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

        self.assertEqual(str(raised.exception), "SSH authentication failed.")
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
