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

from timecapsulesmb.cli.flows import (
    REBOOT_UP_TIMEOUT_MESSAGE,
    request_reboot_and_wait,
    verify_managed_runtime_flow,
)
from timecapsulesmb.device.probe import (
    ManagedMdnsTakeoverProbeResult,
    ManagedRuntimeProbeResult,
    ManagedSmbdProbeResult,
)
from timecapsulesmb.transport.ssh import SshCommandTimeout, SshConnection


class FakeCommandContext:
    def __init__(self) -> None:
        self.stages: list[str] = []
        self.finish_fields: dict[str, object] = {}
        self.debug_fields: dict[str, object] = {}
        self.error: str | None = None

    def set_stage(self, stage: str) -> None:
        self.stages.append(stage)

    def update_fields(self, **fields: object) -> None:
        for key, value in fields.items():
            if value is not None:
                self.finish_fields[key] = value

    def add_debug_fields(self, **fields: object) -> None:
        for key, value in fields.items():
            if value is not None:
                self.debug_fields[key] = value

    def fail_with_error(self, message: str) -> None:
        self.error = message


class CliFlowTests(unittest.TestCase):
    def make_connection(self) -> SshConnection:
        return SshConnection("root@10.0.0.2", "pw", "-o foo")

    def managed_runtime_probe(self, ready: bool) -> ManagedRuntimeProbeResult:
        status = "PASS" if ready else "FAIL"
        detail = "managed runtime is ready" if ready else "managed runtime is not ready"
        smbd = ManagedSmbdProbeResult(ready, detail, (f"{status}:managed smbd ready",))
        mdns = ManagedMdnsTakeoverProbeResult(ready, detail, (f"{status}:managed mDNS takeover active",))
        return ManagedRuntimeProbeResult(
            ready=ready,
            detail=detail,
            smbd=smbd,
            mdns=mdns,
            lines=smbd.lines + mdns.lines,
        )

    def test_request_reboot_and_wait_succeeds_after_normal_reboot_request(self) -> None:
        command_context = FakeCommandContext()
        output = io.StringIO()
        with mock.patch("timecapsulesmb.cli.flows.remote_request_reboot") as reboot_mock:
            with mock.patch("timecapsulesmb.cli.flows.wait_for_ssh_state_conn", side_effect=[True, True]) as wait_mock:
                with redirect_stdout(output):
                    ok = request_reboot_and_wait(
                        self.make_connection(),
                        command_context,
                        timeout_no_down_message="did not go down",
                    )

        self.assertTrue(ok)
        reboot_mock.assert_called_once()
        self.assertEqual(wait_mock.call_args_list[0].kwargs, {"expected_up": False, "timeout_seconds": 60})
        self.assertEqual(wait_mock.call_args_list[1].kwargs, {"expected_up": True, "timeout_seconds": 240})
        self.assertEqual(command_context.finish_fields["reboot_was_attempted"], True)
        self.assertEqual(command_context.finish_fields["device_came_back_after_reboot"], True)
        self.assertIsNone(command_context.error)
        self.assertIn("Reboot requested. Waiting for the device to go down...", output.getvalue())

    def test_request_reboot_and_wait_observes_device_state_after_request_timeout(self) -> None:
        command_context = FakeCommandContext()
        output = io.StringIO()
        with mock.patch(
            "timecapsulesmb.cli.flows.remote_request_reboot",
            side_effect=SshCommandTimeout("Timed out waiting for ssh command to finish: reboot"),
        ):
            with mock.patch("timecapsulesmb.cli.flows.wait_for_ssh_state_conn", side_effect=[True, True]) as wait_mock:
                with redirect_stdout(output):
                    ok = request_reboot_and_wait(
                        self.make_connection(),
                        command_context,
                        timeout_no_down_message="did not go down",
                    )

        self.assertTrue(ok)
        self.assertEqual(wait_mock.call_count, 2)
        self.assertEqual(command_context.debug_fields["reboot_request_timed_out"], True)
        self.assertEqual(command_context.debug_fields["reboot_request_error"], "Timed out waiting for ssh command to finish: reboot")
        self.assertEqual(command_context.finish_fields["device_came_back_after_reboot"], True)
        self.assertIn("Reboot request timed out; checking whether the device is rebooting...", output.getvalue())

    def test_request_reboot_and_wait_fails_after_timeout_when_device_never_goes_down(self) -> None:
        command_context = FakeCommandContext()
        output = io.StringIO()
        with mock.patch(
            "timecapsulesmb.cli.flows.remote_request_reboot",
            side_effect=SshCommandTimeout("Timed out waiting for ssh command to finish: reboot"),
        ):
            with mock.patch("timecapsulesmb.cli.flows.wait_for_ssh_state_conn", return_value=False) as wait_mock:
                with redirect_stdout(output):
                    ok = request_reboot_and_wait(
                        self.make_connection(),
                        command_context,
                        timeout_no_down_message="clear reboot failure",
                    )

        self.assertFalse(ok)
        wait_mock.assert_called_once()
        self.assertEqual(command_context.error, "clear reboot failure")
        self.assertNotIn("device_came_back_after_reboot", command_context.finish_fields)
        self.assertIn("clear reboot failure", output.getvalue())

    def test_request_reboot_and_wait_propagates_non_timeout_reboot_errors(self) -> None:
        command_context = FakeCommandContext()
        with mock.patch("timecapsulesmb.cli.flows.remote_request_reboot", side_effect=SystemExit("ssh failed")):
            with mock.patch("timecapsulesmb.cli.flows.wait_for_ssh_state_conn") as wait_mock:
                with self.assertRaises(SystemExit) as raised:
                    request_reboot_and_wait(
                        self.make_connection(),
                        command_context,
                        timeout_no_down_message="did not go down",
                    )

        self.assertEqual(str(raised.exception), "ssh failed")
        wait_mock.assert_not_called()

    def test_request_reboot_and_wait_fails_when_ssh_does_not_return(self) -> None:
        command_context = FakeCommandContext()
        output = io.StringIO()
        with mock.patch("timecapsulesmb.cli.flows.remote_request_reboot"):
            with mock.patch("timecapsulesmb.cli.flows.wait_for_ssh_state_conn", side_effect=[True, False]):
                with redirect_stdout(output):
                    ok = request_reboot_and_wait(
                        self.make_connection(),
                        command_context,
                        timeout_no_down_message="did not go down",
                    )

        self.assertFalse(ok)
        self.assertEqual(command_context.error, REBOOT_UP_TIMEOUT_MESSAGE)
        self.assertIn(REBOOT_UP_TIMEOUT_MESSAGE, output.getvalue())

    def test_verify_managed_runtime_flow_succeeds_when_runtime_ready(self) -> None:
        command_context = FakeCommandContext()
        with mock.patch("timecapsulesmb.cli.flows.verify_managed_runtime", return_value=self.managed_runtime_probe(True)) as verify_mock:
            ok = verify_managed_runtime_flow(
                self.make_connection(),
                command_context,
                stage="verify_runtime",
                timeout_seconds=123,
                heading="Checking runtime",
                failure_message="runtime failed",
            )

        self.assertTrue(ok)
        self.assertEqual(command_context.stages, ["verify_runtime"])
        self.assertIsNone(command_context.error)
        self.assertEqual(verify_mock.call_args.kwargs, {"timeout_seconds": 123})

    def test_verify_managed_runtime_flow_fails_when_runtime_not_ready(self) -> None:
        command_context = FakeCommandContext()
        output = io.StringIO()
        with mock.patch("timecapsulesmb.cli.flows.verify_managed_runtime", return_value=self.managed_runtime_probe(False)):
            with redirect_stdout(output):
                ok = verify_managed_runtime_flow(
                    self.make_connection(),
                    command_context,
                    stage="verify_runtime",
                    timeout_seconds=123,
                    heading="Checking runtime",
                    failure_message="runtime failed",
                )

        self.assertFalse(ok)
        self.assertEqual(command_context.stages, ["verify_runtime"])
        self.assertEqual(command_context.error, "runtime failed")
        self.assertIn("runtime failed", output.getvalue())


if __name__ == "__main__":
    unittest.main()
