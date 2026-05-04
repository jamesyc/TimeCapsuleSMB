from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from timecapsulesmb.integrations.airpyrt import (
    AIRPYRT_NOT_FOUND_ERROR,
    acp_run_check,
    candidate_interpreters,
    disable_ssh,
    enable_ssh,
    ensure_airpyrt_available,
    set_dbug,
)
from timecapsulesmb.transport.ssh import SshCommandTimeout, SshConnection


def ssh_result(returncode: int, stdout: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["ssh"], returncode, stdout=stdout, stderr="")


class AirPyrtTests(unittest.TestCase):
    def test_candidate_interpreters_prefers_env_override(self) -> None:
        with mock.patch.dict(os.environ, {"AIRPYRT_PY": "/tmp/custom-python"}, clear=False):
            self.assertEqual(candidate_interpreters(), ["/tmp/custom-python"])

    def test_ensure_airpyrt_available_raises_when_missing(self) -> None:
        with mock.patch("timecapsulesmb.integrations.airpyrt.find_acp_executable", return_value=None):
            with mock.patch("timecapsulesmb.integrations.airpyrt.find_airpyrt_python", return_value=None):
                with self.assertRaises(RuntimeError) as exc:
                    ensure_airpyrt_available()
        message = str(exc.exception)
        self.assertEqual(message, AIRPYRT_NOT_FOUND_ERROR)

    def test_acp_run_check_raises_on_embedded_error_code(self) -> None:
        proc = mock.Mock(returncode=0, stdout="error code: -0x1234")
        with mock.patch("timecapsulesmb.integrations.airpyrt.run", return_value=proc):
            with self.assertRaises(RuntimeError):
                acp_run_check(["acp"])

    def test_enable_ssh_skips_reboot_when_requested(self) -> None:
        with mock.patch("timecapsulesmb.integrations.airpyrt.set_dbug") as set_dbug_mock:
            with mock.patch("timecapsulesmb.integrations.airpyrt.reboot") as reboot_mock:
                enable_ssh("10.0.0.2", "pw", reboot_device=False)
        set_dbug_mock.assert_called_once()
        reboot_mock.assert_not_called()

    def test_set_dbug_reports_command_through_log_callback(self) -> None:
        messages: list[str] = []
        with mock.patch("timecapsulesmb.integrations.airpyrt._acp_command", return_value=["acp", "-t", "10.0.0.2"]):
            with mock.patch("timecapsulesmb.integrations.airpyrt.acp_run_check"):
                set_dbug("10.0.0.2", "pw", "0x3000", log=messages.append)

        self.assertEqual(messages, ["Running: acp -t 10.0.0.2"])

    def test_disable_ssh_retries_and_reboots_over_ssh_on_success(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "-o ProxyJump=bastion")
        with mock.patch(
            "timecapsulesmb.integrations.airpyrt.run_ssh",
            side_effect=[ssh_result(1, "nope"), ssh_result(0, "ok")],
        ) as run_ssh_mock:
            with mock.patch("timecapsulesmb.integrations.airpyrt.remote_request_reboot") as reboot_mock:
                disable_ssh(connection, reboot_device=True, verbose=False)

        self.assertEqual(run_ssh_mock.call_count, 2)
        run_ssh_mock.assert_has_calls([
            mock.call(connection, "acp remove dbug", check=False, timeout=30),
            mock.call(connection, "/usr/sbin/acp remove dbug", check=False, timeout=30),
        ])
        reboot_mock.assert_called_once_with(connection)

    def test_disable_ssh_reports_success_through_log_callback(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")
        messages: list[str] = []
        with mock.patch(
            "timecapsulesmb.integrations.airpyrt.run_ssh",
            side_effect=[ssh_result(1, "nope"), ssh_result(0, "ok")],
        ):
            disable_ssh(connection, reboot_device=False, log=messages.append)

        self.assertEqual(messages, ["Removed 'dbug' via: /usr/sbin/acp remove dbug"])

    def test_disable_ssh_treats_missing_dbug_property_as_success(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")
        messages: list[str] = []
        with mock.patch(
            "timecapsulesmb.integrations.airpyrt.run_ssh",
            return_value=ssh_result(22, "### remove property error: -10"),
        ) as run_ssh_mock:
            with mock.patch("timecapsulesmb.integrations.airpyrt.remote_request_reboot") as reboot_mock:
                disable_ssh(connection, reboot_device=True, log=messages.append)

        run_ssh_mock.assert_called_once_with(connection, "acp remove dbug", check=False, timeout=30)
        reboot_mock.assert_called_once_with(connection)
        self.assertEqual(messages, ["'dbug' already absent via: acp remove dbug"])

    def test_disable_ssh_continues_after_detached_reboot_request_timeout(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")
        messages: list[str] = []
        with mock.patch(
            "timecapsulesmb.integrations.airpyrt.run_ssh",
            return_value=ssh_result(0, "ok"),
        ):
            with mock.patch(
                "timecapsulesmb.integrations.airpyrt.remote_request_reboot",
                side_effect=SshCommandTimeout("Timed out waiting for ssh command to finish: reboot"),
            ):
                disable_ssh(connection, reboot_device=True, log=messages.append)

        self.assertIn("Reboot request timed out; checking whether the device is rebooting...", messages[-1])

    def test_disable_ssh_propagates_non_timeout_reboot_request_failure(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")
        with mock.patch(
            "timecapsulesmb.integrations.airpyrt.run_ssh",
            return_value=ssh_result(0, "ok"),
        ):
            with mock.patch(
                "timecapsulesmb.integrations.airpyrt.remote_request_reboot",
                side_effect=SystemExit("ssh failed"),
            ):
                with self.assertRaises(SystemExit) as exc:
                    disable_ssh(connection, reboot_device=True)

        self.assertEqual(str(exc.exception), "ssh failed")


if __name__ == "__main__":
    unittest.main()
