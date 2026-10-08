"""Device commands refuse to start without local sshpass."""
from __future__ import annotations

import io
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from unittest import mock

from timecapsulesmb.app import service
from timecapsulesmb.app.events import EventSink
from timecapsulesmb.cli import activate, deploy, flash, fsck, uninstall
from timecapsulesmb.core.config import AppConfig

from tests.cli_support import CliTestCase


class ReachedDevice(Exception):
    """Raised where a command would start resolving or contacting the device."""


# Each command, its arguments, and the finished telemetry event it emits.
CLI_COMMANDS = (
    ("deploy", deploy.main, ["--yes"]),
    ("deploy --dry-run", deploy.main, ["--dry-run"]),
    ("activate", activate.main, ["--yes"]),
    ("uninstall", uninstall.main, ["--yes"]),
    ("fsck", fsck.main, ["--yes"]),
    ("flash", flash.main, ["--read-only"]),
)


class CliSshpassRequirementTests(CliTestCase):
    def run_command(self, main, args: list[str], *, missing: bool, stderr: io.StringIO | None = None):
        device_steps = mock.Mock(side_effect=ReachedDevice)
        output = io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.transport.local.sshpass_missing", return_value=missing))
            for name in ("require_valid_config", "resolve_env_connection", "resolve_validated_managed_target"):
                stack.enter_context(mock.patch(f"timecapsulesmb.cli.context.CommandContext.{name}", device_steps))
            stack.enter_context(redirect_stdout(output))
            if stderr is not None:
                stack.enter_context(redirect_stderr(stderr))
            try:
                rc: int | None = main(args)
            except ReachedDevice:
                rc = None
        return rc, device_steps, output.getvalue()

    def test_json_commands_keep_stdout_clean_when_sshpass_is_missing(self) -> None:
        for label, main, args in (
            ("deploy", deploy.main, ["--dry-run", "--json"]),
            ("activate", activate.main, ["--dry-run", "--json"]),
            ("uninstall", uninstall.main, ["--dry-run", "--json"]),
            ("flash", flash.main, ["--read-only", "--json"]),
        ):
            with self.subTest(command=label):
                stderr = io.StringIO()
                rc, _device_steps, stdout = self.run_command(main, args, missing=True, stderr=stderr)

                self.assertEqual(rc, 1)
                self.assertEqual(stdout, "")
                self.assertIn("local tool sshpass is missing", stderr.getvalue())

    def finished_event(self, label: str) -> str:
        return f"{label.split()[0]}_finished"

    def test_commands_stop_before_the_device_when_sshpass_is_missing(self) -> None:
        for label, main, args in CLI_COMMANDS:
            with self.subTest(command=label):
                self._telemetry_client.reset_mock()
                rc, device_steps, text = self.run_command(main, args, missing=True)

                self.assertEqual(rc, 1)
                device_steps.assert_not_called()
                self.assertIn("local tool sshpass is missing; run `./tcapsule bootstrap` to install it", text)
                finished = self.telemetry_payload(self.finished_event(label))
                self.assertEqual(finished["result"], "failure")
                self.assertIn("local tool sshpass is missing", finished["error"])
                self.assertIn("stage=check_local_tools", finished["error"])

    def test_commands_continue_to_the_device_when_sshpass_is_present(self) -> None:
        for label, main, args in CLI_COMMANDS:
            with self.subTest(command=label):
                rc, device_steps, text = self.run_command(main, args, missing=False)

                self.assertIsNone(rc)
                device_steps.assert_called_once()
                self.assertNotIn("sshpass", text)


class AppSshpassRequirementTests(unittest.TestCase):
    # Each app operation that reaches the device, with parameters that get it there.
    OPERATIONS = (
        ("deploy", {}),
        ("deploy", {"dry_run": True}),
        ("activate", {}),
        ("uninstall", {}),
        ("fsck", {}),
        # A flash write reaches the device through the same target resolution
        # as a backup, after validating its saved backup and plan.
        ("flash", {"action": "backup"}),
    )

    def setUp(self) -> None:
        self._exit_stack = ExitStack()
        self.addCleanup(self._exit_stack.close)
        self._telemetry_client = mock.Mock()
        self._exit_stack.enter_context(
            mock.patch("timecapsulesmb.app.service.TelemetryClient.from_config", return_value=self._telemetry_client)
        )
        config = AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"})
        for target in ("timecapsulesmb.app.ops.common.load_env_config", "timecapsulesmb.app.ops.maintenance.load_env_config"):
            self._exit_stack.enter_context(mock.patch(target, return_value=config))
        self.device_steps = mock.Mock(side_effect=ReachedDevice)
        for target in (
            "timecapsulesmb.app.ops.common.resolve_validated_managed_target",
            "timecapsulesmb.app.ops.common.resolve_env_connection",
            "timecapsulesmb.app.ops.maintenance.resolve_env_connection",
        ):
            self._exit_stack.enter_context(mock.patch(target, self.device_steps))

    def run_operation(self, operation: str, params: dict[str, object], *, missing: bool) -> list[dict[str, object]]:
        events: list[dict[str, object]] = []
        self.device_steps.reset_mock()
        with mock.patch("timecapsulesmb.transport.local.sshpass_missing", return_value=missing):
            service.run_api_request(
                {"operation": operation, "params": params},
                EventSink(lambda event: events.append(event.to_jsonable())),
            )
        return events

    def test_operations_stop_before_the_device_when_sshpass_is_missing(self) -> None:
        for operation, params in self.OPERATIONS:
            with self.subTest(operation=operation, params=params):
                events = self.run_operation(operation, params, missing=True)

                self.device_steps.assert_not_called()
                errors = [event for event in events if event["type"] == "error"]
                self.assertEqual(len(errors), 1)
                self.assertEqual(errors[0]["code"], "validation_failed")
                self.assertEqual(errors[0]["message"], "Local tool sshpass is missing; reinstall TimeCapsuleSMB.")
                # The check adds no row to the app's operation timeline.
                self.assertNotIn("check_local_tools", [event.get("stage") for event in events])

    def test_operations_continue_to_the_device_when_sshpass_is_present(self) -> None:
        for operation, params in self.OPERATIONS:
            with self.subTest(operation=operation, params=params):
                events = self.run_operation(operation, params, missing=False)

                self.device_steps.assert_called_once()
                self.assertFalse([event for event in events if event["type"] == "error" and "sshpass" in str(event.get("message"))])

    def test_activate_dry_run_needs_no_sshpass(self) -> None:
        # A dry run only prints the activation plan.
        events = self.run_operation("activate", {"dry_run": True}, missing=True)
        self.assertEqual([event["type"] for event in events if event["type"] in ("result", "error")], ["result"])
        self.assertNotIn("check_local_tools", [event.get("stage") for event in events])


if __name__ == "__main__":
    unittest.main()
