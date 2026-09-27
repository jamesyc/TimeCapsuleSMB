"""The activate, uninstall and fsck commands."""
from __future__ import annotations

import io
import shlex
import json
import unittest
from contextlib import ExitStack
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace
from unittest import mock
from timecapsulesmb.cli import activate, fsck, uninstall
from timecapsulesmb.cli.main import main
from timecapsulesmb.services import maintenance as maintenance_service
from timecapsulesmb.services.runtime_verification import (
    ACTIVATION_SETTLE_MESSAGE,
    ACTIVATION_SETTLE_SECONDS,
)
from timecapsulesmb.core.config import MANAGED_PAYLOAD_DIR_NAME
from timecapsulesmb.device.storage import MaStVolume
from timecapsulesmb.deploy.commands import (
    RunScriptAction,
    render_remote_action,
    StopProcessAction,
    managed_stop_actions,
    render_remote_actions,
)
from timecapsulesmb.deploy.verify import VerificationResult
from timecapsulesmb.transport.ssh import SshCommandTimeout, SshError

from tests.cli_support import CliTestCase, FakeCommandContext


class CliMaintenanceTests(CliTestCase):
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
            with mock.patch("timecapsulesmb.cli.context.CommandContext.require_compatibility", return_value=self.make_supported_netbsd4_compatibility()):
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
            with mock.patch("timecapsulesmb.cli.context.CommandContext.require_compatibility", return_value=self.make_supported_compatibility()):
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
                                rc = activate.main([])
        self.assertEqual(rc, 1)
        actions_mock.assert_not_called()
        message = "Running `activate` requires confirmation when stdin is not interactive. Use `activate --yes` in a non-interactive environment."
        self.assertIn(message, output.getvalue())
        command_context.finish.assert_called_once()
        self.assertEqual(command_context.finish.call_args.kwargs["result"], "failure")
        self.assertEqual(command_context.finish.call_args.kwargs["error"], message)

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
            with mock.patch("timecapsulesmb.cli.context.CommandContext.require_compatibility", return_value=self.make_supported_netbsd4_compatibility()):
                with mock.patch("timecapsulesmb.services.activation.probe_managed_runtime_conn", return_value=self.managed_runtime_probe(False)):
                    with mock.patch("timecapsulesmb.services.activation.run_remote_actions") as actions_mock:
                        with mock.patch("timecapsulesmb.services.runtime_verification.probe_managed_runtime_conn", return_value=self.managed_runtime_probe(True)) as verify_mock:
                            with mock.patch("timecapsulesmb.services.runtime_verification.sleep") as sleep_mock:
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
        sleep_mock.assert_called_once_with(ACTIVATION_SETTLE_SECONDS)
        self.assertIn("without file transfer", output.getvalue())
        self.assertIn(ACTIVATION_SETTLE_MESSAGE.text, output.getvalue())

    def test_activate_skips_rc_local_when_payload_is_already_healthy(self) -> None:
        output = io.StringIO()
        values = self.make_valid_env()
        with mock.patch("timecapsulesmb.cli.activate.load_env_config", return_value=self.make_app_config(values)):
            with mock.patch("timecapsulesmb.cli.context.CommandContext.require_compatibility", return_value=self.make_supported_netbsd4_compatibility()):
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
            with mock.patch("timecapsulesmb.cli.context.CommandContext.require_compatibility", return_value=self.make_supported_netbsd4_compatibility()):
                with mock.patch("timecapsulesmb.services.activation.probe_managed_runtime_conn", return_value=self.managed_runtime_probe(False)):
                    with mock.patch("timecapsulesmb.services.activation.run_remote_actions"):
                        with mock.patch("timecapsulesmb.services.runtime_verification.probe_managed_runtime_conn", return_value=self.managed_runtime_probe(False)):
                            with mock.patch("timecapsulesmb.services.runtime_verification.sleep") as sleep_mock:
                                with redirect_stdout(output):
                                    rc = activate.main(["--yes"])
        self.assertEqual(rc, 1)
        sleep_mock.assert_called_once_with(ACTIVATION_SETTLE_SECONDS)
        self.assertIn("NetBSD4 activation failed.", output.getvalue())

    def test_activate_dry_run_json_outputs_activation_plan(self) -> None:
        output = io.StringIO()
        values = self.make_valid_env()
        with mock.patch("timecapsulesmb.cli.activate.load_env_config", return_value=self.make_app_config(values)):
            with mock.patch("timecapsulesmb.cli.context.CommandContext.require_compatibility", return_value=self.make_supported_netbsd4_compatibility()):
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
        self.assertIn("request: attempt device reboot", text)
        self.assertIn("follow-up: wait for SSH down, then SSH up", text)
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
        self.assertNotIn("wait for SSH down, then SSH up", text)

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
                "strategy": "acp_then_ssh",
                "follow_up": ["wait_for_ssh_down", "wait_for_ssh_up"],
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
            run_ssh_mock = stack.enter_context(mock.patch("timecapsulesmb.services.reboot.remote_request_reboot"))
            wait_mock = stack.enter_context(mock.patch("timecapsulesmb.services.reboot.wait_for_ssh_state_conn", side_effect=[True, True]))
            verify_mock = stack.enter_context(mock.patch("timecapsulesmb.services.maintenance.verify_post_uninstall", return_value=VerificationResult(True, ())))
            with redirect_stdout(output):
                rc = uninstall.main(["--yes"])
        self.assertEqual(rc, 0)
        uninstall_mock.assert_called_once()
        run_ssh_mock.assert_called_once()
        self.assertEqual(wait_mock.call_args_list[0].args[0].host, "root@10.0.0.2")
        self.assertEqual(wait_mock.call_args_list[0].kwargs, {"expected_up": False, "timeout_seconds": 60})
        self.assertEqual(wait_mock.call_args_list[1].args[0].host, "root@10.0.0.2")
        self.assertEqual(wait_mock.call_args_list[1].kwargs, {"expected_up": True, "timeout_seconds": 240})
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
            reboot_mock = stack.enter_context(mock.patch("timecapsulesmb.services.reboot.remote_request_reboot"))
            wait_mock = stack.enter_context(mock.patch("timecapsulesmb.services.reboot.wait_for_ssh_state_conn"))
            verify_mock = stack.enter_context(mock.patch("timecapsulesmb.services.maintenance.verify_post_uninstall"))
            with redirect_stdout(output):
                rc = uninstall.main(["--yes", "--mount-wait", "17", "--no-wait"])

        self.assertEqual(rc, 0)
        self.assertEqual(mast_mocks.mounted_mast_volumes_conn.call_args.kwargs["wait_seconds"], 17)
        reboot_mock.assert_called_once()
        wait_mock.assert_not_called()
        verify_mock.assert_not_called()
        self.assertIn("Post-uninstall verification skipped.", output.getvalue())

    def test_uninstall_no_wait_fails_when_reboot_request_fails(self) -> None:
        output = io.StringIO()
        values = self.make_valid_env()
        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.uninstall.load_env_config", return_value=self.make_app_config(values)))
            self._patch_mast_volume_flow(stack, "uninstall")
            stack.enter_context(mock.patch("timecapsulesmb.cli.uninstall.remote_uninstall_payload"))
            reboot_mock = stack.enter_context(
                mock.patch("timecapsulesmb.services.reboot.remote_request_reboot", side_effect=SshError("ssh command failed with rc=255"))
            )
            wait_mock = stack.enter_context(mock.patch("timecapsulesmb.services.reboot.wait_for_ssh_state_conn"))
            verify_mock = stack.enter_context(mock.patch("timecapsulesmb.services.maintenance.verify_post_uninstall"))
            with redirect_stdout(output):
                rc = uninstall.main(["--yes", "--no-wait"])

        self.assertEqual(rc, 1)
        self.assertIn("ssh command failed with rc=255", output.getvalue())
        reboot_mock.assert_called_once()
        wait_mock.assert_not_called()
        verify_mock.assert_not_called()
        finished = self.telemetry_payload("uninstall_finished")
        self.assertEqual(finished["result"], "failure")

    def test_uninstall_reboot_request_timeout_continues_when_device_reboots(self) -> None:
        output = io.StringIO()
        values = self.make_valid_env()
        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.uninstall.load_env_config", return_value=self.make_app_config(values)))
            self._patch_mast_volume_flow(stack, "uninstall")
            stack.enter_context(mock.patch("timecapsulesmb.cli.uninstall.remote_uninstall_payload"))
            stack.enter_context(
                mock.patch(
                    "timecapsulesmb.services.reboot.remote_request_reboot",
                    side_effect=SshCommandTimeout("Timed out waiting for ssh command to finish: reboot"),
                )
            )
            wait_mock = stack.enter_context(mock.patch("timecapsulesmb.services.reboot.wait_for_ssh_state_conn", side_effect=[True, True]))
            verify_mock = stack.enter_context(mock.patch("timecapsulesmb.services.maintenance.verify_post_uninstall", return_value=VerificationResult(True, ())))
            with redirect_stdout(output):
                rc = uninstall.main(["--yes"])

        self.assertEqual(rc, 0)
        self.assertEqual(wait_mock.call_args_list[0].kwargs, {"expected_up": False, "timeout_seconds": 60})
        self.assertEqual(wait_mock.call_args_list[1].kwargs, {"expected_up": True, "timeout_seconds": 240})
        verify_mock.assert_called_once()
        text = output.getvalue()
        self.assertIn("SSH reboot request timed out; checking whether the device is rebooting...", text)
        self.assertIn("Device is back online.", text)
        finished = self.telemetry_payload("uninstall_finished")
        self.assertEqual(finished["result"], "success")
        self.assertEqual(finished["reboot_was_attempted"], True)
        self.assertEqual(finished["device_came_back_after_reboot"], True)
        self.assertEqual(finished["post_uninstall_verified"], True)

    def test_uninstall_reboot_request_timeout_fails_when_device_never_goes_down(self) -> None:
        output = io.StringIO()
        values = self.make_valid_env()
        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.uninstall.load_env_config", return_value=self.make_app_config(values)))
            self._patch_mast_volume_flow(stack, "uninstall")
            stack.enter_context(mock.patch("timecapsulesmb.cli.uninstall.remote_uninstall_payload"))
            stack.enter_context(
                mock.patch(
                    "timecapsulesmb.services.reboot.remote_request_reboot",
                    side_effect=SshCommandTimeout("Timed out waiting for ssh command to finish: reboot"),
                )
            )
            wait_mock = stack.enter_context(mock.patch("timecapsulesmb.services.reboot.wait_for_ssh_state_conn", return_value=False))
            verify_mock = stack.enter_context(mock.patch("timecapsulesmb.services.maintenance.verify_post_uninstall"))
            with redirect_stdout(output):
                rc = uninstall.main(["--yes"])

        self.assertEqual(rc, 1)
        wait_mock.assert_called_once()
        verify_mock.assert_not_called()
        text = output.getvalue()
        self.assertIn("Reboot was requested but the device did not go down.", text)
        self.assertIn("The uninstall removed managed TimeCapsuleSMB files before reboot; power-cycle or rerun uninstall.", text)
        finished = self.telemetry_payload("uninstall_finished")
        self.assertEqual(finished["result"], "failure")
        self.assertEqual(finished["reboot_was_attempted"], True)
        self.assertEqual(finished["device_came_back_after_reboot"], False)
        self.assertEqual(finished["post_uninstall_verified"], False)
        self.assertIn("stage=wait_for_reboot_down", finished["error"])
        self.assertIn("ssh_reboot_timed_out=true", finished["error"])

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
            run_ssh_mock = stack.enter_context(mock.patch("timecapsulesmb.services.reboot.remote_request_reboot"))
            verify_mock = stack.enter_context(mock.patch("timecapsulesmb.services.maintenance.verify_post_uninstall"))
            with redirect_stdout(output):
                rc = uninstall.main(["--no-reboot"])
        self.assertEqual(rc, 0)
        uninstall_mock.assert_called_once()
        run_ssh_mock.assert_not_called()
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
            run_ssh_mock = stack.enter_context(mock.patch("timecapsulesmb.services.reboot.remote_request_reboot"))
            with redirect_stdout(output):
                rc = uninstall.main([])
        self.assertEqual(rc, 0)
        run_ssh_mock.assert_not_called()
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
            stack.enter_context(mock.patch("timecapsulesmb.services.reboot.remote_request_reboot"))
            stack.enter_context(mock.patch("timecapsulesmb.services.reboot.wait_for_ssh_state_conn", side_effect=[True, True]))
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
        run_result = mock.Mock(stdout="--- fsck_hfs /dev/dk2 ---\nOK\ntcapsule-fsck: fsck_hfs exit status 0\n--- reboot ---\n", returncode=255)
        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.fsck.load_env_config", return_value=self.make_app_config(values)))
            self._patch_mast_volume_flow(stack, "fsck", mounted_volumes=(self._mast_volume("dk2"),))
            run_ssh_mock = stack.enter_context(mock.patch("timecapsulesmb.services.maintenance.run_ssh", return_value=run_result))
            wait_mock = stack.enter_context(mock.patch("timecapsulesmb.services.reboot.wait_for_ssh_state_conn", side_effect=[True, True]))
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
        self.assertIn("exec </dev/null >/dev/null 2>&1", remote_cmd)
        self.assertIn("/bin/sync; /bin/sleep 1;", remote_cmd)
        self.assertIn("/sbin/shutdown -r now", remote_cmd)
        self.assertIn("/sbin/reboot", remote_cmd)
        self.assertIn(") & exit 0", remote_cmd)
        self.assertEqual(wait_mock.call_args_list[0].kwargs, {"expected_up": False, "timeout_seconds": 90})
        self.assertEqual(wait_mock.call_args_list[1].kwargs, {"expected_up": True, "timeout_seconds": 420})
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

    def test_fsck_no_wait_skips_ssh_waits(self) -> None:
        output = io.StringIO()
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
        }
        run_result = mock.Mock(stdout="--- fsck_hfs /dev/dk2 ---\nOK\ntcapsule-fsck: fsck_hfs exit status 0\n--- reboot ---\n", returncode=255)
        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.fsck.load_env_config", return_value=self.make_app_config(values)))
            self._patch_mast_volume_flow(stack, "fsck", mounted_volumes=(self._mast_volume("dk2"),))
            stack.enter_context(mock.patch("timecapsulesmb.services.maintenance.run_ssh", return_value=run_result))
            observe_mock = stack.enter_context(mock.patch("timecapsulesmb.services.maintenance.observe_reboot_cycle"))
            with redirect_stdout(output):
                rc = fsck.main(["--yes", "--no-wait"])
        self.assertEqual(rc, 0)
        observe_mock.assert_not_called()

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
            observe_mock = stack.enter_context(mock.patch("timecapsulesmb.services.maintenance.observe_reboot_cycle"))
            with redirect_stdout(output):
                rc = fsck.main(["--yes", "--no-reboot"])
        self.assertEqual(rc, 0)
        observe_mock.assert_not_called()
        self.assertEqual(run_ssh_mock.call_args.kwargs["timeout"], maintenance_service.FSCK_REMOTE_COMMAND_TIMEOUT_SECONDS)
        self.assertNotIn("/sbin/reboot", run_ssh_mock.call_args.args[1])

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
        run_result = mock.Mock(stdout="--- fsck_hfs /dev/dk2 ---\nOK\ntcapsule-fsck: fsck_hfs exit status 0\n--- reboot ---\n", returncode=255)
        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.fsck.load_env_config", return_value=self.make_app_config(values)))
            self._patch_mast_volume_flow(stack, "fsck", mounted_volumes=(self._mast_volume("dk2"),))
            stack.enter_context(mock.patch("timecapsulesmb.services.maintenance.run_ssh", return_value=run_result))
            wait_mock = stack.enter_context(mock.patch("timecapsulesmb.services.reboot.wait_for_ssh_state_conn", return_value=False))
            with redirect_stdout(output):
                rc = fsck.main(["--yes"])
        self.assertEqual(rc, 1)
        wait_mock.assert_called_once()
        self.assertIn("fsck requested reboot from the device, but SSH did not go down.", output.getvalue())
        finished = self.telemetry_payload("fsck_finished")
        self.assertEqual(finished["result"], "failure")
        self.assertEqual(finished["reboot_was_attempted"], True)
        self.assertEqual(finished["device_came_back_after_reboot"], False)
        self.assertIn("stage=wait_for_reboot_down", finished["error"])

    def test_fsck_reboot_timeout_emits_failure_stage(self) -> None:
        output = io.StringIO()
        values = self.make_valid_env()
        run_result = mock.Mock(stdout="--- fsck_hfs /dev/dk2 ---\nOK\ntcapsule-fsck: fsck_hfs exit status 0\n--- reboot ---\n", returncode=255)
        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.fsck.load_env_config", return_value=self.make_app_config(values)))
            self._patch_mast_volume_flow(stack, "fsck", mounted_volumes=(self._mast_volume("dk2"),))
            stack.enter_context(mock.patch("timecapsulesmb.services.maintenance.run_ssh", return_value=run_result))
            stack.enter_context(mock.patch("timecapsulesmb.services.reboot.wait_for_ssh_state_conn", side_effect=[True, False]))
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
            wait_mock = stack.enter_context(mock.patch("timecapsulesmb.services.reboot.wait_for_ssh_state_conn", side_effect=[True, True]))
            with redirect_stdout(output):
                rc = fsck.main(argv)
        return rc, output.getvalue(), wait_mock

    def test_fsck_failed_status_still_reboots_and_waits_then_fails(self) -> None:
        rc, text, wait_mock = self._run_fsck_with_remote_output(
            "--- fsck_hfs /dev/dk2 ---\n** The volume could not be repaired.\n"
            "tcapsule-fsck: fsck_hfs exit status 8\n--- reboot ---\n",
            255,
            ["--yes"],
        )

        self.assertEqual(rc, 1)
        # Rebooting is what brings file sharing back, so the wait still runs.
        self.assertEqual(wait_mock.call_count, 2)
        self.assertIn("fsck_hfs exited with status 8; the disk may still need repair.", text)
        finished = self.telemetry_payload("fsck_finished")
        self.assertEqual(finished["result"], "failure")
        self.assertEqual(finished["reboot_was_attempted"], True)
        self.assertEqual(finished["device_came_back_after_reboot"], True)
        self.assertIn("fsck_hfs exited with status 8", finished["error"])

    def test_fsck_failed_status_fails_without_reboot_or_wait(self) -> None:
        for argv in (["--yes", "--no-reboot"], ["--yes", "--no-wait"]):
            with self.subTest(argv=argv):
                rc, text, wait_mock = self._run_fsck_with_remote_output(
                    "tcapsule-fsck: fsck_hfs exit status 8\n", 8, argv,
                )

                self.assertEqual(rc, 1)
                wait_mock.assert_not_called()
                self.assertIn("fsck_hfs exited with status 8", text)
                self.assertEqual(self.telemetry_payload("fsck_finished")["result"], "failure")

    def test_fsck_without_status_line_fails_and_skips_reboot_wait(self) -> None:
        # A process that would not stop aborts the script before fsck and
        # before the reboot command, so waiting for SSH to drop would only
        # time out.
        rc, text, wait_mock = self._run_fsck_with_remote_output("process smbd did not stop\n", 1, ["--yes"])

        self.assertEqual(rc, 1)
        wait_mock.assert_not_called()
        self.assertIn("fsck did not run", text)
        finished = self.telemetry_payload("fsck_finished")
        self.assertEqual(finished["result"], "failure")
        self.assertEqual(finished["reboot_was_attempted"], False)

    def test_fsck_on_a_volume_that_stayed_mounted_names_the_unmount(self) -> None:
        # The volume was still mounted after umount, so the script stopped
        # before fsck_hfs and before its reboot.
        rc, text, wait_mock = self._run_fsck_with_remote_output(
            "umount: /Volumes/Data: Device busy\ntcapsule-fsck: volume not unmounted\n", 1, ["--yes"])

        self.assertEqual(rc, 1)
        wait_mock.assert_not_called()
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
