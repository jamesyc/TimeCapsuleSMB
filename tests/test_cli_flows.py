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
    runtime_callbacks,
    wait_for_device_up,
    wait_for_tcp_port_state,
    verify_managed_runtime_flow,
)
from timecapsulesmb.device.probe import (
    ManagedRuntimeProbeResult,
    ProbeStepResult,
    ReadinessProbeResult,
)
from timecapsulesmb.integrations.acp import ACPConnectionError
from timecapsulesmb.services.deploy import DEPLOY_REBOOT_UP_TIMEOUT_MESSAGE
from timecapsulesmb.services.reboot import RebootFlowError, observe_reboot_cycle, request_reboot, request_reboot_and_wait
from timecapsulesmb.services.runtime import ACP_REBOOT_REQUEST_TIMEOUT_SECONDS, SSH_SHUTDOWN_REBOOT_PROGRESS_MESSAGE
from timecapsulesmb.transport.ssh import SshCommandTimeout, SshConnection, SshError


REBOOT_UP_TIMEOUT_MESSAGE = "Timed out waiting for SSH after reboot."


def readiness_result(ready: bool, detail: str, lines: tuple[str, ...]) -> ReadinessProbeResult:
    steps = []
    for index, line in enumerate(lines):
        if line.startswith("PASS:"):
            steps.append(ProbeStepResult(f"test_{index}", "pass", line.removeprefix("PASS:")))
        elif line.startswith("FAIL:"):
            steps.append(ProbeStepResult(f"test_{index}", "fail", line.removeprefix("FAIL:")))
        else:
            steps.append(ProbeStepResult(f"test_{index}", "fail", line))
    return ReadinessProbeResult(ready=ready, detail=detail, steps=tuple(steps))


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

    def reboot_callbacks(self, command_context: FakeCommandContext):
        return runtime_callbacks(command_context)

    def request_reboot_and_wait_default(self, command_context: FakeCommandContext, **kwargs) -> None:
        request_reboot_and_wait(
            self.make_connection(),
            strategy=kwargs.pop("strategy", "acp_then_ssh"),
            callbacks=self.reboot_callbacks(command_context),
            down_timeout_seconds=kwargs.pop("down_timeout_seconds", 60),
            up_timeout_seconds=kwargs.pop("up_timeout_seconds", 240),
            reboot_no_down_message=kwargs.pop("reboot_no_down_message", "did not go down"),
            reboot_up_timeout_message=kwargs.pop("reboot_up_timeout_message", REBOOT_UP_TIMEOUT_MESSAGE),
            **kwargs,
        )

    def managed_runtime_probe(self, ready: bool) -> ManagedRuntimeProbeResult:
        status = "PASS" if ready else "FAIL"
        detail = "managed runtime is ready" if ready else "managed runtime is not ready"
        smbd = readiness_result(ready, detail, (f"{status}:managed smbd ready",))
        mdns = readiness_result(ready, detail, (f"{status}:managed mDNS takeover active",))
        return ManagedRuntimeProbeResult(
            ready=ready,
            detail=detail,
            smbd=smbd,
            mdns=mdns,
        )

    def test_wait_for_tcp_port_state_checks_before_sleeping(self) -> None:
        with mock.patch("timecapsulesmb.cli.flows.tcp_open", return_value=True) as tcp_open_mock:
            with mock.patch("timecapsulesmb.cli.flows.time.sleep") as sleep_mock:
                ok = wait_for_tcp_port_state(
                    "10.0.0.2",
                    22,
                    expected_state=True,
                    timeout_seconds=30,
                    interval_seconds=5,
                    verbose=False,
                )

        self.assertTrue(ok)
        tcp_open_mock.assert_called_once_with("10.0.0.2", 22)
        sleep_mock.assert_not_called()

    def test_wait_for_device_up_checks_before_sleeping(self) -> None:
        with mock.patch("timecapsulesmb.cli.flows.tcp_open", return_value=True) as tcp_open_mock:
            with mock.patch("timecapsulesmb.cli.flows.time.sleep") as sleep_mock:
                ok = wait_for_device_up(
                    "10.0.0.2",
                    probe_ports=(5009, 445),
                    timeout_seconds=30,
                    interval_seconds=5,
                )

        self.assertTrue(ok)
        tcp_open_mock.assert_called_once_with("10.0.0.2", 5009)
        sleep_mock.assert_not_called()

    def test_observe_reboot_cycle_succeeds_without_requesting_reboot(self) -> None:
        command_context = FakeCommandContext()
        output = io.StringIO()
        reboot_mock = mock.Mock()
        wait_mock = mock.Mock(side_effect=[True, True])
        with redirect_stdout(output):
            observe_reboot_cycle(
                self.make_connection(),
                callbacks=self.reboot_callbacks(command_context),
                reboot_no_down_message="did not go down",
                reboot_up_timeout_message=REBOOT_UP_TIMEOUT_MESSAGE,
                down_timeout_seconds=90,
                up_timeout_seconds=420,
                wait_for_ssh_state=wait_mock,
            )

        reboot_mock.assert_not_called()
        self.assertEqual(wait_mock.call_args_list[0].kwargs, {"expected_up": False, "timeout_seconds": 90})
        self.assertEqual(wait_mock.call_args_list[1].kwargs, {"expected_up": True, "timeout_seconds": 420})
        self.assertEqual(command_context.finish_fields["device_came_back_after_reboot"], True)
        self.assertIn("Device is back online.", output.getvalue())

    def test_request_reboot_and_wait_succeeds_after_acp_reboot_request(self) -> None:
        command_context = FakeCommandContext()
        output = io.StringIO()
        acp_reboot_mock = mock.Mock()
        ssh_reboot_mock = mock.Mock()
        wait_mock = mock.Mock(side_effect=[True, True])
        with redirect_stdout(output):
            self.request_reboot_and_wait_default(
                command_context,
                request_acp_reboot=acp_reboot_mock,
                request_reboot_func=ssh_reboot_mock,
                wait_for_ssh_state=wait_mock,
            )

        acp_reboot_mock.assert_called_once_with("10.0.0.2", "pw", timeout=ACP_REBOOT_REQUEST_TIMEOUT_SECONDS)
        ssh_reboot_mock.assert_not_called()
        self.assertEqual(wait_mock.call_args_list[0].kwargs, {"expected_up": False, "timeout_seconds": 60})
        self.assertEqual(wait_mock.call_args_list[1].kwargs, {"expected_up": True, "timeout_seconds": 240})
        self.assertEqual(command_context.finish_fields["reboot_was_attempted"], True)
        self.assertEqual(command_context.finish_fields["device_came_back_after_reboot"], True)
        self.assertEqual(command_context.debug_fields["reboot_request_strategy"], "acp_then_ssh")
        self.assertEqual(command_context.debug_fields["acp_reboot_attempted"], True)
        self.assertEqual(command_context.debug_fields["acp_reboot_succeeded"], True)
        self.assertIsNone(command_context.error)
        self.assertIn("ACP reboot requested.", output.getvalue())
        self.assertIn("Waiting for the device to go down...", output.getvalue())

    def test_request_reboot_and_wait_can_use_ssh_reboot_request_only(self) -> None:
        command_context = FakeCommandContext()
        output = io.StringIO()
        reboot_mock = mock.Mock()
        acp_reboot_mock = mock.Mock(side_effect=AssertionError("SSH-only strategy should not use ACP reboot"))
        wait_mock = mock.Mock(side_effect=[True, True])
        with redirect_stdout(output):
            self.request_reboot_and_wait_default(
                command_context,
                strategy="ssh_shutdown_then_reboot",
                request_reboot_func=reboot_mock,
                request_acp_reboot=acp_reboot_mock,
                wait_for_ssh_state=wait_mock,
            )

        reboot_mock.assert_called_once()
        acp_reboot_mock.assert_not_called()
        self.assertEqual(wait_mock.call_args_list[0].kwargs, {"expected_up": False, "timeout_seconds": 60})
        self.assertEqual(wait_mock.call_args_list[1].kwargs, {"expected_up": True, "timeout_seconds": 240})
        self.assertEqual(command_context.finish_fields["reboot_was_attempted"], True)
        self.assertEqual(command_context.finish_fields["device_came_back_after_reboot"], True)
        self.assertEqual(command_context.debug_fields["reboot_request_strategy"], "ssh_shutdown_then_reboot")
        self.assertEqual(command_context.debug_fields["ssh_reboot_attempted"], True)
        self.assertEqual(command_context.debug_fields["ssh_reboot_succeeded"], True)
        self.assertIn("SSH reboot requested.", output.getvalue())
        self.assertIn("Waiting for the device to go down...", output.getvalue())

    def test_request_reboot_and_wait_uses_ssh_fallback_when_acp_fails(self) -> None:
        command_context = FakeCommandContext()
        output = io.StringIO()
        reboot_mock = mock.Mock()
        acp_reboot_mock = mock.Mock(side_effect=ACPConnectionError("ACP timed out"))
        wait_mock = mock.Mock(side_effect=[True, True])
        with redirect_stdout(output):
            self.request_reboot_and_wait_default(
                command_context,
                request_acp_reboot=acp_reboot_mock,
                request_reboot_func=reboot_mock,
                wait_for_ssh_state=wait_mock,
            )

        reboot_mock.assert_called_once()
        self.assertEqual(wait_mock.call_args_list[0].kwargs, {"expected_up": False, "timeout_seconds": 60})
        self.assertEqual(wait_mock.call_args_list[1].kwargs, {"expected_up": True, "timeout_seconds": 240})
        self.assertEqual(command_context.debug_fields["acp_reboot_succeeded"], False)
        self.assertEqual(command_context.debug_fields["acp_reboot_error"], "ACP timed out")
        self.assertEqual(command_context.debug_fields["ssh_reboot_attempted"], True)
        self.assertEqual(command_context.debug_fields["ssh_reboot_succeeded"], True)
        self.assertEqual(command_context.finish_fields["device_came_back_after_reboot"], True)
        self.assertIn("ACP reboot request failed; trying SSH reboot request.", output.getvalue())
        self.assertIn("SSH reboot requested.", output.getvalue())

    def test_request_reboot_and_wait_observes_device_state_after_ssh_fallback_timeout(self) -> None:
        command_context = FakeCommandContext()
        output = io.StringIO()
        wait_mock = mock.Mock(side_effect=[True, True])
        with redirect_stdout(output):
            self.request_reboot_and_wait_default(
                command_context,
                request_acp_reboot=mock.Mock(side_effect=ACPConnectionError("ACP timed out")),
                request_reboot_func=mock.Mock(side_effect=SshCommandTimeout("Timed out waiting for ssh command to finish: reboot")),
                wait_for_ssh_state=wait_mock,
            )

        self.assertEqual(wait_mock.call_count, 2)
        self.assertEqual(command_context.debug_fields["ssh_reboot_timed_out"], True)
        self.assertEqual(command_context.debug_fields["ssh_reboot_error"], "Timed out waiting for ssh command to finish: reboot")
        self.assertEqual(command_context.finish_fields["device_came_back_after_reboot"], True)
        self.assertIn("SSH reboot request timed out; checking whether the device is rebooting...", output.getvalue())

    def test_request_reboot_and_wait_observes_device_state_after_ssh_fallback_error(self) -> None:
        command_context = FakeCommandContext()
        output = io.StringIO()
        wait_mock = mock.Mock(side_effect=[True, True])
        with redirect_stdout(output):
            self.request_reboot_and_wait_default(
                command_context,
                request_acp_reboot=mock.Mock(side_effect=ACPConnectionError("ACP timed out")),
                request_reboot_func=mock.Mock(side_effect=SshError("ssh failed")),
                wait_for_ssh_state=wait_mock,
            )

        self.assertEqual(wait_mock.call_count, 2)
        self.assertEqual(command_context.debug_fields["ssh_reboot_succeeded"], False)
        self.assertEqual(command_context.debug_fields["ssh_reboot_error"], "ssh failed")
        self.assertEqual(command_context.finish_fields["device_came_back_after_reboot"], True)
        self.assertIn("SSH reboot request failed; checking whether the device is rebooting anyway...", output.getvalue())

    def test_request_ssh_reboot_uses_ssh_only_strategy_and_progress_log(self) -> None:
        command_context = FakeCommandContext()
        messages: list[str] = []
        output = io.StringIO()
        reboot_mock = mock.Mock()
        with redirect_stdout(output):
            request_reboot(
                self.make_connection(),
                strategy="ssh",
                callbacks=self.reboot_callbacks(command_context),
                progress_log=messages.append,
                request_reboot_func=reboot_mock,
            )

        reboot_mock.assert_called_once()
        self.assertEqual(command_context.stages, ["reboot"])
        self.assertEqual(command_context.finish_fields["reboot_was_attempted"], True)
        self.assertEqual(command_context.debug_fields["reboot_request_strategy"], "ssh")
        self.assertEqual(command_context.debug_fields["ssh_reboot_attempted"], True)
        self.assertEqual(command_context.debug_fields["ssh_reboot_succeeded"], True)
        self.assertEqual(messages, [SSH_SHUTDOWN_REBOOT_PROGRESS_MESSAGE])
        self.assertIn("SSH reboot requested.", output.getvalue())

    def test_request_ssh_reboot_records_timeout_without_raising(self) -> None:
        command_context = FakeCommandContext()
        output = io.StringIO()
        with redirect_stdout(output):
            request_reboot(
                self.make_connection(),
                strategy="ssh",
                callbacks=self.reboot_callbacks(command_context),
                request_reboot_func=mock.Mock(side_effect=SshCommandTimeout("Timed out waiting for ssh command to finish: reboot")),
            )

        self.assertEqual(command_context.debug_fields["reboot_request_strategy"], "ssh")
        self.assertEqual(command_context.debug_fields["ssh_reboot_succeeded"], False)
        self.assertEqual(command_context.debug_fields["ssh_reboot_timed_out"], True)
        self.assertEqual(command_context.debug_fields["ssh_reboot_error"], "Timed out waiting for ssh command to finish: reboot")
        self.assertIn("SSH reboot request timed out; checking whether the device is rebooting...", output.getvalue())

    def test_request_ssh_reboot_raises_timeout_when_request_error_is_required(self) -> None:
        command_context = FakeCommandContext()
        with self.assertRaisesRegex(RebootFlowError, "SSH reboot request timed out"):
            request_reboot(
                self.make_connection(),
                strategy="ssh",
                callbacks=self.reboot_callbacks(command_context),
                raise_on_request_error=True,
                request_reboot_func=mock.Mock(side_effect=SshCommandTimeout("Timed out waiting for ssh command to finish: reboot")),
            )

        self.assertEqual(command_context.debug_fields["reboot_request_strategy"], "ssh")
        self.assertEqual(command_context.debug_fields["ssh_reboot_succeeded"], False)
        self.assertEqual(command_context.debug_fields["ssh_reboot_timed_out"], True)
        self.assertEqual(command_context.debug_fields["ssh_reboot_error"], "Timed out waiting for ssh command to finish: reboot")

    def test_request_ssh_reboot_records_ssh_error_without_raising(self) -> None:
        command_context = FakeCommandContext()
        output = io.StringIO()
        with redirect_stdout(output):
            request_reboot(
                self.make_connection(),
                strategy="ssh",
                callbacks=self.reboot_callbacks(command_context),
                request_reboot_func=mock.Mock(side_effect=SshError("ssh failed")),
            )

        self.assertEqual(command_context.debug_fields["reboot_request_strategy"], "ssh")
        self.assertEqual(command_context.debug_fields["ssh_reboot_succeeded"], False)
        self.assertEqual(command_context.debug_fields["ssh_reboot_error"], "ssh failed")
        self.assertIn("SSH reboot request failed; checking whether the device is rebooting anyway...", output.getvalue())

    def test_request_ssh_reboot_raises_ssh_error_when_request_error_is_required(self) -> None:
        command_context = FakeCommandContext()
        with self.assertRaisesRegex(RebootFlowError, "SSH reboot request failed"):
            request_reboot(
                self.make_connection(),
                strategy="ssh",
                callbacks=self.reboot_callbacks(command_context),
                raise_on_request_error=True,
                request_reboot_func=mock.Mock(side_effect=SshError("ssh failed")),
            )

        self.assertEqual(command_context.debug_fields["reboot_request_strategy"], "ssh")
        self.assertEqual(command_context.debug_fields["ssh_reboot_succeeded"], False)
        self.assertEqual(command_context.debug_fields["ssh_reboot_error"], "ssh failed")

    def test_request_reboot_and_wait_fails_when_device_never_goes_down_after_acp_request(self) -> None:
        command_context = FakeCommandContext()
        wait_mock = mock.Mock(return_value=False)
        with self.assertRaisesRegex(RebootFlowError, "did not go down") as raised:
            self.request_reboot_and_wait_default(
                command_context,
                request_acp_reboot=mock.Mock(),
                wait_for_ssh_state=wait_mock,
            )

        wait_mock.assert_called_once()
        self.assertEqual(raised.exception.reason, "did_not_go_down")
        self.assertNotIn("device_came_back_after_reboot", command_context.finish_fields)

    def test_request_reboot_and_wait_fails_after_ssh_fallback_timeout_when_device_never_goes_down(self) -> None:
        command_context = FakeCommandContext()
        wait_mock = mock.Mock(return_value=False)
        with self.assertRaisesRegex(RebootFlowError, "clear reboot failure") as raised:
            self.request_reboot_and_wait_default(
                command_context,
                reboot_no_down_message="clear reboot failure",
                request_acp_reboot=mock.Mock(side_effect=ACPConnectionError("ACP timed out")),
                request_reboot_func=mock.Mock(side_effect=SshCommandTimeout("Timed out waiting for ssh command to finish: reboot")),
                wait_for_ssh_state=wait_mock,
            )

        wait_mock.assert_called_once()
        self.assertEqual(raised.exception.reason, "did_not_go_down")
        self.assertNotIn("device_came_back_after_reboot", command_context.finish_fields)

    def test_request_reboot_and_wait_fails_when_device_never_goes_down_after_all_request_errors(self) -> None:
        command_context = FakeCommandContext()
        output = io.StringIO()
        wait_mock = mock.Mock(return_value=False)
        with redirect_stdout(output):
            with self.assertRaisesRegex(RebootFlowError, "did not go down") as raised:
                self.request_reboot_and_wait_default(
                    command_context,
                    request_acp_reboot=mock.Mock(side_effect=ACPConnectionError("ACP timed out")),
                    request_reboot_func=mock.Mock(side_effect=SshError("ssh failed")),
                    wait_for_ssh_state=wait_mock,
                )

        wait_mock.assert_called_once()
        self.assertEqual(raised.exception.reason, "did_not_go_down")
        self.assertIn("SSH reboot request failed; checking whether the device is rebooting anyway...", output.getvalue())

    def test_request_reboot_and_wait_fails_when_ssh_does_not_return(self) -> None:
        command_context = FakeCommandContext()
        with self.assertRaisesRegex(RebootFlowError, REBOOT_UP_TIMEOUT_MESSAGE) as raised:
            self.request_reboot_and_wait_default(
                command_context,
                request_acp_reboot=mock.Mock(),
                wait_for_ssh_state=mock.Mock(side_effect=[True, False]),
            )

        self.assertEqual(raised.exception.reason, "did_not_come_back_up")

    def test_request_reboot_and_wait_uses_caller_timeout_message_when_ssh_does_not_return(self) -> None:
        command_context = FakeCommandContext()
        with self.assertRaisesRegex(RebootFlowError, "Timed out waiting for SSH after reboot") as raised:
            self.request_reboot_and_wait_default(
                command_context,
                strategy="ssh_shutdown_then_reboot",
                request_reboot_func=mock.Mock(),
                wait_for_ssh_state=mock.Mock(side_effect=[True, False]),
                reboot_up_timeout_message=DEPLOY_REBOOT_UP_TIMEOUT_MESSAGE,
            )

        self.assertEqual(str(raised.exception), DEPLOY_REBOOT_UP_TIMEOUT_MESSAGE)

    def test_verify_managed_runtime_flow_succeeds_when_runtime_ready(self) -> None:
        command_context = FakeCommandContext()
        with (
            mock.patch("timecapsulesmb.cli.flows.probe_managed_runtime_conn", return_value=self.managed_runtime_probe(True)) as verify_mock,
            mock.patch("timecapsulesmb.cli.flows.read_runtime_log_tails_conn") as log_tail_mock,
        ):
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
        log_tail_mock.assert_not_called()

    def test_verify_managed_runtime_flow_fails_when_runtime_not_ready(self) -> None:
        command_context = FakeCommandContext()
        output = io.StringIO()
        with (
            mock.patch("timecapsulesmb.cli.flows.probe_managed_runtime_conn", return_value=self.managed_runtime_probe(False)),
            mock.patch(
                "timecapsulesmb.cli.flows.read_runtime_log_tails_conn",
                return_value={
                    "remote_rc_local_log_tail": "rc log",
                    "remote_mdns_log_tail": "mdns log",
                },
            ),
        ):
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
        self.assertEqual(command_context.error, "runtime failed managed runtime is not ready")
        self.assertIn("runtime failed managed runtime is not ready", output.getvalue())
        self.assertEqual(command_context.debug_fields["remote_rc_local_log_tail"], "rc log")
        self.assertEqual(command_context.debug_fields["remote_mdns_log_tail"], "mdns log")

    def test_verify_managed_runtime_flow_collects_network_diagnostics_after_auto_ip_unavailable(self) -> None:
        command_context = FakeCommandContext()
        output = io.StringIO()
        with (
            mock.patch("timecapsulesmb.cli.flows.probe_managed_runtime_conn", return_value=self.managed_runtime_probe(False)),
            mock.patch(
                "timecapsulesmb.cli.flows.read_runtime_log_tails_conn",
                return_value={
                    "remote_manager_log_tail": "manager: mDNS startup deferred; no usable address has appeared yet",
                },
            ),
            mock.patch(
                "timecapsulesmb.cli.flows.read_remote_network_diagnostics_conn",
                return_value={
                    "remote_network_config": {"ssh_target_host": "169.254.44.9"},
                    "remote_network_target_ip_matches": [],
                },
            ) as network_mock,
        ):
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
        network_mock.assert_called_once()
        self.assertEqual(command_context.debug_fields["runtime_startup_failure"], "network_auto_ip_unavailable")
        self.assertTrue(command_context.debug_fields["runtime_startup_waiting_for_auto_ip"])
        self.assertEqual(command_context.debug_fields["remote_network_config"], {"ssh_target_host": "169.254.44.9"})
        self.assertEqual(command_context.debug_fields["remote_network_target_ip_matches"], [])

    def test_verify_managed_runtime_flow_keeps_original_failure_when_log_tail_fails(self) -> None:
        command_context = FakeCommandContext()
        output = io.StringIO()
        with (
            mock.patch("timecapsulesmb.cli.flows.probe_managed_runtime_conn", return_value=self.managed_runtime_probe(False)),
            mock.patch("timecapsulesmb.cli.flows.read_runtime_log_tails_conn", side_effect=RuntimeError("tail failed")),
        ):
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
        self.assertEqual(command_context.error, "runtime failed managed runtime is not ready")
        self.assertEqual(command_context.debug_fields["remote_runtime_log_tail_error"], "tail failed")

    def test_verify_managed_runtime_flow_includes_runtime_timeout_detail(self) -> None:
        command_context = FakeCommandContext()
        smbd = readiness_result(False, "managed smbd readiness probe timed out", ("FAIL:managed smbd readiness probe timed out",))
        mdns = readiness_result(False, "managed mDNS takeover probe timed out", ("FAIL:managed mDNS takeover probe timed out",))
        result = ManagedRuntimeProbeResult(
            ready=False,
            detail="runtime verification timed out after 180s; managed smbd readiness probe timed out; managed mDNS takeover probe timed out",
            smbd=smbd,
            mdns=mdns,
            extra_steps=(ProbeStepResult("runtime_timeout", "fail", "runtime verification timed out after 180s"),),
        )
        output = io.StringIO()
        with mock.patch("timecapsulesmb.cli.flows.probe_managed_runtime_conn", return_value=result):
            with redirect_stdout(output):
                ok = verify_managed_runtime_flow(
                    self.make_connection(),
                    command_context,
                    stage="verify_runtime",
                    timeout_seconds=180,
                    heading="Checking runtime",
                    failure_message="NetBSD4 activation failed.",
                )

        self.assertFalse(ok)
        self.assertEqual(
            command_context.error,
            "NetBSD4 activation failed. runtime verification timed out after 180s; managed smbd readiness probe timed out; managed mDNS takeover probe timed out",
        )
        self.assertIn("failed: runtime verification timed out after 180s", output.getvalue())


if __name__ == "__main__":
    unittest.main()
