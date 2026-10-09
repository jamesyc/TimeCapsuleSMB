from __future__ import annotations

import io
import json
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from timecapsulesmb.cli.context import CommandContext
from timecapsulesmb.cli.runtime import confirm, print_json
from timecapsulesmb.core.config import ConfigError
from timecapsulesmb.device.compat import DeviceCompatibility
from timecapsulesmb.device.errors import DeviceError
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

    def test_prompt_without_input_ends_the_command_with_its_message(self) -> None:
        telemetry = mock.Mock()
        with mock.patch("builtins.input", side_effect=EOFError("EOF when reading a line")):
            with self.assertRaises(SystemExit) as raised:
                with CommandContext(telemetry, "test", "test_started", "test_finished") as context:
                    context.set_stage("confirm_reboot")
                    confirm("Continue?", default=True, noninteractive_message="Use `test --yes` to skip the prompt.")

        self.assertEqual(raised.exception.code, "Use `test --yes` to skip the prompt.")
        finished = telemetry.emit.call_args_list[-1].kwargs
        self.assertEqual(finished["result"], "failure")
        self.assertEqual(finished["stage"], "confirm_reboot")
        self.assertTrue(finished["error"].startswith("Use `test --yes` to skip the prompt.\nCaused by: EOF when reading a line"))

    def test_to_operation_callbacks_updates_command_context(self) -> None:
        context = self.make_context()

        with mock.patch("builtins.print") as print_mock:
            callbacks = context.to_operation_callbacks()
            callbacks.set_stage("reboot")
            callbacks.update_fields(reboot_was_attempted=True)
            callbacks.add_debug_fields(reboot_request_strategy="native_acp")
            callbacks.log("reboot requested")

        self.assertEqual(context.debug_stage, "reboot")
        self.assertEqual(context.finish_fields["reboot_was_attempted"], True)
        self.assertEqual(context.debug_fields["reboot_request_strategy"], "native_acp")
        print_mock.assert_called_once_with("reboot requested")

    def test_a_device_found_at_another_address_moves_the_connection(self) -> None:
        context = self.make_context()
        context.connection = SshConnection("root@10.0.0.2", "pw", "-o foo")

        with redirect_stdout(io.StringIO()) as output:
            context.to_operation_callbacks().update_fields(current_host="root@10.0.0.9")

        self.assertEqual(context.connection, SshConnection("root@10.0.0.9", "pw", "-o foo"))
        self.assertEqual(context.finish_fields["current_host"], "root@10.0.0.9")
        self.assertIn("Run `tcapsule configure` to save the device's new address (root@10.0.0.9).", output.getvalue())

    def test_configure_saves_the_new_address_so_it_prints_no_hint(self) -> None:
        context = CommandContext(mock.Mock(), "configure", "configure_started", "configure_finished")
        with redirect_stdout(io.StringIO()) as output:
            context.update_fields(current_host="root@10.0.0.9")

        self.assertEqual(output.getvalue(), "")

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

    def test_require_compatibility_says_why_ssh_did_not_log_in(self) -> None:
        context = self.make_context()
        context.connection = self.make_connection()
        context.probe_state = ProbedDeviceState(
            probe_result=ProbeResult(SshAccessStatus.CLOSED, "SSH is not reachable yet.", "", "", "", "unknown"),
            compatibility=None,
        )

        with mock.patch("timecapsulesmb.services.runtime.tcp_connect_error", return_value=None):
            with self.assertRaises(DeviceError) as raised:
                context.require_compatibility()

        self.assertEqual(raised.exception.code, "ssh_disabled")
        self.assertIn("SSH is turned off", str(raised.exception))
        self.assertIsNone(context.compatibility)


class JsonModeStdoutTests(unittest.TestCase):
    def make_context(self, *, json_output: bool) -> CommandContext:
        return CommandContext(mock.Mock(), "test", "test_started", "test_finished", args=SimpleNamespace(json=json_output))

    def run_in_context(self, body, *, json_output: bool) -> tuple[str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            with self.make_context(json_output=json_output):
                body()
        return stdout.getvalue(), stderr.getvalue()

    def test_json_mode_keeps_stdout_for_the_document(self) -> None:
        def body() -> None:
            print("Resolving deployment target...")
            print_json({"ok": True})
            print("local tool ssh is missing")

        stdout, stderr = self.run_in_context(body, json_output=True)

        self.assertEqual(json.loads(stdout), {"ok": True})
        self.assertEqual(stderr, "Resolving deployment target...\nlocal tool ssh is missing\n")

    def test_json_mode_sends_prompts_to_stderr(self) -> None:
        answers: list[str] = []
        with mock.patch("sys.stdin", io.StringIO("yes\n")):
            stdout, stderr = self.run_in_context(lambda: answers.append(input("Continue? ")), json_output=True)

        self.assertEqual(answers, ["yes"])
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "Continue? ")

    def test_without_json_everything_stays_on_stdout(self) -> None:
        def body() -> None:
            print("progress")
            print_json({"ok": True})

        stdout, stderr = self.run_in_context(body, json_output=False)

        self.assertEqual(stdout, 'progress\n{\n  "ok": true\n}\n')
        self.assertEqual(stderr, "")

    def test_context_without_args_leaves_stdout_alone(self) -> None:
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            with CommandContext(mock.Mock(), "test", "test_started", "test_finished"):
                print("progress")
        self.assertEqual(stdout.getvalue(), "progress\n")

    def test_json_mode_restores_stdout_when_the_command_fails(self) -> None:
        stdout = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                with self.make_context(json_output=True):
                    raise ConfigError("missing TC_HOST")
            self.assertIs(sys.stdout, stdout)
            print("after")
        self.assertEqual(stdout.getvalue(), "after\n")

    def test_nested_json_contexts_write_the_document_to_the_real_stdout(self) -> None:
        def body() -> None:
            with self.make_context(json_output=True):
                print("inner progress")
                print_json({"inner": 1})

        stdout, stderr = self.run_in_context(body, json_output=True)

        self.assertEqual(json.loads(stdout), {"inner": 1})
        self.assertEqual(stderr, "inner progress\n")


if __name__ == "__main__":
    unittest.main()
