from __future__ import annotations

import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from timecapsulesmb.cli.context import CommandContext
from timecapsulesmb.cli.runtime import NonInteractivePromptError
from timecapsulesmb.device.compat import DeviceCompatibility
from timecapsulesmb.device.probe import ProbedDeviceState, ProbeResult, SshAccessStatus
from timecapsulesmb.transport.ssh import SshConnection


class CommandContextHelperTests(unittest.TestCase):
    def make_context(self) -> CommandContext:
        return CommandContext(mock.Mock(), "test", "test_started", "test_finished")

    def make_connection(self) -> SshConnection:
        return SshConnection("root@10.0.0.2", "pw", "-o foo")

    def make_supported_compatibility(self) -> DeviceCompatibility:
        return DeviceCompatibility(
            os_name="NetBSD",
            os_release="6.0",
            arch="evbarm",
            elf_endianness="little",
            payload_family="netbsd6_samba4",
            device_generation="netbsd6",
            supported=True,
            reason_code="supported_netbsd6",
            syap_candidates=("119",),
            model_candidates=("TimeCapsule8,119",),
        )

    def make_probe_state(self, compatibility: DeviceCompatibility | None = None) -> ProbedDeviceState:
        return ProbedDeviceState(
            probe_result=ProbeResult(
                ssh_status=SshAccessStatus.OPEN_AUTHENTICATED,
                error=None,
                os_name="NetBSD",
                os_release="6.0",
                arch="evbarm",
                elf_endianness="little",
                airport_model="TimeCapsule8,119",
                airport_syap="119",
            ),
            compatibility=compatibility or self.make_supported_compatibility(),
        )

    def test_confirm_or_fail_returns_prompt_result(self) -> None:
        context = self.make_context()
        with mock.patch("timecapsulesmb.cli.context.cli_runtime.confirm", return_value=True) as confirm_mock:
            result = context.confirm_or_fail("Continue?", default=False, noninteractive_message="no stdin")

        self.assertTrue(result)
        confirm_mock.assert_called_once_with(
            "Continue?",
            default=False,
            eof_default=None,
            interrupt_default=None,
            noninteractive_message="no stdin",
        )
        self.assertEqual(context.result, "failure")
        self.assertEqual(context.error_lines, [])

    def test_confirm_or_fail_records_noninteractive_failure(self) -> None:
        context = self.make_context()
        output = io.StringIO()
        with mock.patch(
            "timecapsulesmb.cli.context.cli_runtime.confirm",
            side_effect=NonInteractivePromptError("no stdin"),
        ):
            with redirect_stdout(output):
                result = context.confirm_or_fail("Continue?", default=False, noninteractive_message="no stdin")

        self.assertIsNone(result)
        self.assertEqual(context.result, "failure")
        self.assertEqual(context.error_lines, ["no stdin"])
        self.assertIn("no stdin", output.getvalue())

    def test_to_operation_callbacks_updates_command_context(self) -> None:
        context = self.make_context()

        with mock.patch("builtins.print") as print_mock:
            callbacks = context.to_operation_callbacks()
            callbacks.set_stage("reboot")
            callbacks.update_fields(reboot_was_attempted=True)
            callbacks.add_debug_fields(reboot_request_strategy="ssh")
            callbacks.log("reboot requested")

        self.assertEqual(context.debug_stage, "reboot")
        self.assertEqual(context.finish_fields["reboot_was_attempted"], True)
        self.assertEqual(context.debug_fields["reboot_request_strategy"], "ssh")
        print_mock.assert_called_once_with("reboot requested")

    def test_to_operation_callbacks_updates_context(self) -> None:
        context = self.make_context()

        context.to_operation_callbacks().set_stage("reboot")

        self.assertEqual(context.debug_stage, "reboot")

    def test_require_compatibility_uses_probe_state_without_runtime_reexport(self) -> None:
        context = self.make_context()
        context.connection = self.make_connection()
        context.probe_state = self.make_probe_state()

        compatibility = context.require_compatibility()

        self.assertEqual(compatibility.payload_family, "netbsd6_samba4")
        self.assertEqual(context.finish_fields["device_syap"], "119")
        self.assertEqual(context.finish_fields["device_model"], "TimeCapsule8,119")
        self.assertEqual(context.finish_fields["device_os_version"], "NetBSD 6.0 (evbarm)")


if __name__ == "__main__":
    unittest.main()
