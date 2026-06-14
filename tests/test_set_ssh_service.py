from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from timecapsulesmb.integrations.acp import ACP_PORT
from timecapsulesmb.services.callbacks import OperationCallbacks
from timecapsulesmb.services.set_ssh import (
    SSH_PORT,
    SetSshStatusResult,
    enable_set_ssh,
    probe_set_ssh_status,
)
from timecapsulesmb.transport.ssh import SshConnection


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
        self.assertEqual(result.summary, "AirPort ACP is reachable, but SSH is closed.")

    def test_enable_noops_when_ssh_is_already_open(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")
        with mock.patch("timecapsulesmb.services.set_ssh.enable_ssh_with_port_preflight") as enable:
            result = enable_set_ssh(
                connection,
                no_wait=False,
                initial=SetSshStatusResult(
                    host="10.0.0.2",
                    acp_port_reachable=True,
                    ssh_port_reachable=True,
                ),
            )

        enable.assert_not_called()
        self.assertEqual(result.action, "enable_noop")
        self.assertTrue(result.ssh_final_reachable)
        self.assertFalse(result.reboot_requested)

    def test_enable_requests_acp_reboot_and_waits_for_ssh(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")
        stages: list[str] = []
        with mock.patch("timecapsulesmb.services.set_ssh.enable_ssh_with_port_preflight") as enable:
            wait = mock.Mock(return_value=True)
            result = enable_set_ssh(
                connection,
                no_wait=False,
                callbacks=OperationCallbacks(set_stage=stages.append),
                wait_for_tcp_port_state=wait,
                initial=SetSshStatusResult(
                    host="10.0.0.2",
                    acp_port_reachable=True,
                    ssh_port_reachable=False,
                ),
            )

        enable.assert_called_once()
        self.assertEqual(enable.call_args.args, ("10.0.0.2", "pw"))
        self.assertEqual(enable.call_args.kwargs["reboot_device"], True)
        wait.assert_called_once_with(
            "10.0.0.2",
            SSH_PORT,
            expected_state=True,
            log=mock.ANY,
            service_name="SSH port",
        )
        self.assertEqual(stages, ["wait_for_ssh_enabled"])
        self.assertEqual(result.action, "enable_ssh")
        self.assertTrue(result.ssh_final_reachable)
        self.assertTrue(result.reboot_requested)

    def test_enable_no_wait_skips_ssh_verification(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")
        with mock.patch("timecapsulesmb.services.set_ssh.enable_ssh_with_port_preflight"):
            wait = mock.Mock()
            result = enable_set_ssh(
                connection,
                no_wait=True,
                wait_for_tcp_port_state=wait,
                initial=SetSshStatusResult(
                    host="10.0.0.2",
                    acp_port_reachable=True,
                    ssh_port_reachable=False,
                ),
            )

        wait.assert_not_called()
        self.assertTrue(result.ssh_verification_skipped)
        self.assertFalse(result.ssh_final_reachable)
        self.assertEqual(result.summary, "SSH enable requested; not waiting for SSH to open.")


if __name__ == "__main__":
    unittest.main()
