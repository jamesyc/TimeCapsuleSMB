from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from timecapsulesmb.core.summaries import Summary
from timecapsulesmb.integrations.acp import ACP_PORT
from timecapsulesmb.services.acp_ssh import SSH_ENABLE_TIMEOUT_MESSAGE
from timecapsulesmb.services.reboot import RebootFlowError
from timecapsulesmb.services.set_ssh import (
    SSH_PORT,
    SetSshStatusResult,
    disable_set_ssh,
    enable_set_ssh,
    probe_set_ssh_status,
)
from timecapsulesmb.transport.ssh import SshConnection
from tests.reboot_support import FakeAcpDevice, RecordingCallbacks


CONNECTION = SshConnection("root@10.0.0.2", "pw", "-o foo")
SSH_CLOSED = SetSshStatusResult(host="10.0.0.2", acp_port_reachable=True, ssh_port_reachable=False)
SSH_OPEN = SetSshStatusResult(host="10.0.0.2", acp_port_reachable=True, ssh_port_reachable=True)


class SetSshServiceTests(unittest.TestCase):
    def test_probe_reports_likely_disabled_when_acp_open_and_ssh_closed(self) -> None:
        calls: list[tuple[str, int, float]] = []

        def tcp_error(host: str, port: int, timeout: float) -> str | None:
            calls.append((host, port, timeout))
            return None if port == ACP_PORT else "Connection refused"

        result = probe_set_ssh_status("root@10.0.0.2", timeout=1.5, tcp_connect_error_func=tcp_error)

        self.assertEqual(calls, [("10.0.0.2", ACP_PORT, 1.5), ("10.0.0.2", SSH_PORT, 1.5)])
        self.assertEqual(result.host, "10.0.0.2")
        self.assertTrue(result.acp_port_reachable)
        self.assertFalse(result.ssh_port_reachable)
        self.assertTrue(result.ssh_disabled_likely)
        self.assertEqual(result.summary, Summary("ssh.acp_reachable_ssh_closed", "AirPort ACP is reachable, but SSH is closed."))

    def enable(self, device: FakeAcpDevice, *, no_wait: bool = False):
        recorder = RecordingCallbacks()
        with mock.patch("timecapsulesmb.services.set_ssh.enable_ssh_with_port_preflight") as enable:
            with device.patched():
                try:
                    result = enable_set_ssh(CONNECTION, no_wait=no_wait, callbacks=recorder.callbacks(), initial=SSH_CLOSED)
                except RebootFlowError as exc:
                    result = exc
        return result, recorder, enable

    def test_enable_noops_when_ssh_is_already_open(self) -> None:
        with mock.patch("timecapsulesmb.services.set_ssh.enable_ssh_with_port_preflight") as enable:
            result = enable_set_ssh(CONNECTION, no_wait=False, initial=SSH_OPEN)

        enable.assert_not_called()
        self.assertEqual(result.action, "enable_noop")
        self.assertTrue(result.ssh_final_reachable)
        self.assertFalse(result.reboot_requested)
        self.assertEqual(result.summary.key, "ssh.already_enabled")

    def test_enable_sets_dbug_then_reboots_and_waits_for_ssh(self) -> None:
        device = FakeAcpDevice(ssh_open=False, ssh_up_after_boot=120.0)
        result, recorder, enable = self.enable(device)

        enable.assert_called_once()
        self.assertEqual(enable.call_args.args, ("10.0.0.2", "pw"))
        self.assertEqual(device.calls[:3], ["read", "sleep 1", "request"])
        self.assertEqual(recorder.stages, ["reboot", "wait_for_reboot_down", "wait_for_reboot_up"])
        self.assertTrue(recorder.measurement("reboot_cycle")["expect_ssh"])
        self.assertEqual(result.action, "enable_ssh")
        self.assertTrue(result.ssh_final_reachable)
        self.assertTrue(result.reboot_requested)
        self.assertEqual(result.summary.key, "ssh.configured")

    def test_enable_no_wait_requests_the_reboot_and_skips_ssh_verification(self) -> None:
        device = FakeAcpDevice(ssh_open=False)
        result, _recorder, _enable = self.enable(device, no_wait=True)

        self.assertEqual(device.calls, ["request"])
        self.assertTrue(result.ssh_verification_skipped)
        self.assertFalse(result.ssh_final_reachable)
        self.assertEqual(result.summary, Summary("ssh.enable_requested", "SSH enable requested; not waiting for SSH to open."))

    def test_enable_fails_with_the_enable_message_when_ssh_does_not_open(self) -> None:
        result, _recorder, _enable = self.enable(FakeAcpDevice(ssh_open=False, ssh_up_after_boot=None))

        self.assertIsInstance(result, RebootFlowError)
        self.assertEqual(result.code, "reboot_not_finished")
        self.assertEqual(str(result), SSH_ENABLE_TIMEOUT_MESSAGE)

    def test_enable_reports_a_device_that_never_restarted(self) -> None:
        result, _recorder, _enable = self.enable(FakeAcpDevice(ssh_open=False, reboots=False))

        self.assertEqual(result.code, "reboot_not_started")

    def disable(self, device: FakeAcpDevice, *, no_wait: bool = False):
        recorder = RecordingCallbacks()
        with mock.patch("timecapsulesmb.services.set_ssh.disable_ssh_over_ssh") as remove_dbug:
            with device.patched():
                try:
                    result = disable_set_ssh(CONNECTION, no_wait=no_wait, callbacks=recorder.callbacks(), initial=SSH_OPEN)
                except RebootFlowError as exc:
                    result = exc
        return result, recorder, remove_dbug

    def test_disable_noops_when_ssh_is_already_closed(self) -> None:
        with mock.patch("timecapsulesmb.services.set_ssh.disable_ssh_over_ssh") as remove_dbug:
            result = disable_set_ssh(CONNECTION, no_wait=False, initial=SSH_CLOSED)

        remove_dbug.assert_not_called()
        self.assertEqual(result.action, "disable_noop")
        self.assertEqual(result.summary.key, "ssh.already_disabled")

    def test_disable_proves_the_reboot_then_finds_ssh_still_closed(self) -> None:
        device = FakeAcpDevice(ssh_up_after_boot=None)
        result, recorder, remove_dbug = self.disable(device)

        remove_dbug.assert_called_once_with(CONNECTION, log=mock.ANY)
        self.assertEqual(device.calls[:3], ["read", "sleep 1", "request"])
        # Port 22 is checked twice, a poll apart, after sshd would have started.
        self.assertEqual(device.calls.count("tcp 22"), 2)
        self.assertGreaterEqual(device.device_uptime(), 60)
        self.assertEqual(recorder.stages, ["disable_ssh", "reboot", "wait_for_reboot_down", "wait_for_reboot_up"])
        self.assertFalse(recorder.measurement("reboot_cycle")["expect_ssh"])
        self.assertEqual(result.action, "disable_ssh")
        self.assertFalse(result.ssh_final_reachable)
        self.assertTrue(result.waited)
        self.assertEqual(result.summary.key, "ssh.disabled")

    def test_disable_never_claims_success_before_the_device_restarted(self) -> None:
        # The old check took a closed port 22 plus an open ACP port as a
        # finished reboot, which ACPd still answering during shutdown satisfied.
        device = FakeAcpDevice(reboots=False, ssh_open=False)
        result, _recorder, _remove = self.disable(device)

        self.assertEqual(result.code, "reboot_not_started")
        self.assertNotIn("tcp 22", device.calls)

    def test_disable_that_did_not_persist_fails(self) -> None:
        result, _recorder, _remove = self.disable(FakeAcpDevice(ssh_up_after_boot=10.0))

        self.assertEqual(result.code, "ssh_still_enabled")
        self.assertEqual(str(result), "SSH reopened after reboot. Disable did not persist.")

    def test_disable_device_that_never_comes_back(self) -> None:
        result, _recorder, _remove = self.disable(FakeAcpDevice(kernel_after=10_000))

        self.assertEqual(result.code, "reboot_not_finished")
        self.assertEqual(str(result), "Device went down after disable request but did not come back within timeout.")

    def test_disable_no_wait_requests_the_reboot_only(self) -> None:
        device = FakeAcpDevice()
        result, _recorder, remove_dbug = self.disable(device, no_wait=True)

        remove_dbug.assert_called_once()
        self.assertEqual(device.calls, ["request"])
        self.assertTrue(result.ssh_verification_skipped)
        self.assertEqual(result.summary.key, "ssh.disable_requested")


if __name__ == "__main__":
    unittest.main()
