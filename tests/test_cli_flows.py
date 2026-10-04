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

from timecapsulesmb.device.errors import DeviceError
from timecapsulesmb.device.probe import (
    ManagedRuntimeProbeResult,
    ProbeStepResult,
)
from timecapsulesmb.services.runtime_verification import verify_managed_runtime_ready
from timecapsulesmb.transport.ssh import SshConnection

from tests.cli_support import FakeCommandContext, readiness_result


REBOOT_UP_TIMEOUT_MESSAGE = "Timed out waiting for SSH after reboot."


class CliFlowTests(unittest.TestCase):
    def make_connection(self) -> SshConnection:
        return SshConnection("root@10.0.0.2", "pw", "-o foo")

    def managed_runtime_probe(self, ready: bool) -> ManagedRuntimeProbeResult:
        status = "PASS" if ready else "FAIL"
        detail = "managed runtime is ready" if ready else "managed runtime is not ready"
        smbd = readiness_result(ready, detail, (f"{status}:managed smbd ready",))
        mdns = readiness_result(ready, detail, (f"{status}:managed mDNS registrant active",))
        return ManagedRuntimeProbeResult(
            ready=ready,
            detail=detail,
            smbd=smbd,
            mdns=mdns,
        )

    def verify_runtime(self, command_context: FakeCommandContext, *, timeout_seconds: int = 123, failure_message: str = "runtime failed"):
        return verify_managed_runtime_ready(
            self.make_connection(),
            callbacks=command_context.to_operation_callbacks(),
            stage="verify_runtime",
            timeout_seconds=timeout_seconds,
            heading="Checking runtime",
            failure_message=failure_message,
        )

    def test_verify_managed_runtime_ready_succeeds_when_runtime_ready(self) -> None:
        command_context = FakeCommandContext()
        with (
            mock.patch("timecapsulesmb.services.runtime_verification.probe_managed_runtime_conn", return_value=self.managed_runtime_probe(True)) as verify_mock,
            mock.patch("timecapsulesmb.services.runtime_verification.read_runtime_log_tails_conn") as log_tail_mock,
        ):
            result = self.verify_runtime(command_context)

        self.assertTrue(result.ready)
        self.assertEqual(command_context.stages, ["verify_runtime"])
        self.assertEqual(verify_mock.call_args.kwargs, {"timeout_seconds": 123})
        log_tail_mock.assert_not_called()

    def test_verify_managed_runtime_ready_fails_when_runtime_not_ready(self) -> None:
        command_context = FakeCommandContext()
        with (
            mock.patch("timecapsulesmb.services.runtime_verification.probe_managed_runtime_conn", return_value=self.managed_runtime_probe(False)),
            mock.patch(
                "timecapsulesmb.services.runtime_verification.read_runtime_log_tails_conn",
                return_value={
                    "remote_rc_local_log_tail": "rc log",
                    "remote_discovery_log_tail": "mdns log",
                },
            ),
        ):
            with redirect_stdout(io.StringIO()):
                with self.assertRaises(DeviceError) as raised:
                    self.verify_runtime(command_context)

        self.assertEqual(str(raised.exception), "runtime failed managed runtime is not ready")
        self.assertEqual(command_context.stages, ["verify_runtime"])
        self.assertEqual(command_context.debug_fields["remote_rc_local_log_tail"], "rc log")
        self.assertEqual(command_context.debug_fields["remote_discovery_log_tail"], "mdns log")

    def test_verify_managed_runtime_ready_keeps_original_failure_when_log_tail_fails(self) -> None:
        command_context = FakeCommandContext()
        with (
            mock.patch("timecapsulesmb.services.runtime_verification.probe_managed_runtime_conn", return_value=self.managed_runtime_probe(False)),
            mock.patch("timecapsulesmb.services.runtime_verification.read_runtime_log_tails_conn", side_effect=RuntimeError("tail failed")),
        ):
            with redirect_stdout(io.StringIO()):
                with self.assertRaises(DeviceError) as raised:
                    self.verify_runtime(command_context)

        self.assertEqual(str(raised.exception), "runtime failed managed runtime is not ready")
        self.assertEqual(command_context.debug_fields["remote_runtime_log_tail_error"], "tail failed")

    def test_verify_managed_runtime_ready_includes_runtime_timeout_detail(self) -> None:
        command_context = FakeCommandContext()
        smbd = readiness_result(False, "managed smbd readiness probe timed out", ("FAIL:managed smbd readiness probe timed out",))
        mdns = readiness_result(False, "managed mDNS takeover probe timed out", ("FAIL:managed mDNS takeover probe timed out",))
        result = ManagedRuntimeProbeResult(
            ready=False,
            detail="runtime verification timed out after 200s; managed smbd readiness probe timed out; managed mDNS takeover probe timed out",
            smbd=smbd,
            mdns=mdns,
            extra_steps=(ProbeStepResult("runtime_timeout", "fail", "runtime verification timed out after 200s"),),
        )
        output = io.StringIO()
        with mock.patch("timecapsulesmb.services.runtime_verification.probe_managed_runtime_conn", return_value=result):
            with redirect_stdout(output):
                with self.assertRaises(DeviceError) as raised:
                    self.verify_runtime(command_context, timeout_seconds=200, failure_message="NetBSD4 activation failed.")

        self.assertEqual(
            str(raised.exception),
            "NetBSD4 activation failed. runtime verification timed out after 200s; managed smbd readiness probe timed out; managed mDNS takeover probe timed out",
        )
        self.assertIn("failed: runtime verification timed out after 200s", output.getvalue())

if __name__ == "__main__":
    unittest.main()
