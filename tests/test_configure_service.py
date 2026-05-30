from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from timecapsulesmb.device.probe import ProbeResult, ProbedDeviceState
from timecapsulesmb.integrations.acp import ACPAuthError, ACPConnectionError
from timecapsulesmb.services.configure import ConfigureEnableSshCallbacks, enable_ssh_and_reprobe
from timecapsulesmb.transport.ssh import SshConnection


class ConfigureServiceTests(unittest.TestCase):
    def make_connection(self) -> SshConnection:
        return SshConnection("root@10.0.0.2", "pw", "-o foo")

    def make_probe_state(self) -> ProbedDeviceState:
        return ProbedDeviceState(
            probe_result=ProbeResult(
                ssh_port_reachable=True,
                ssh_authenticated=True,
                error=None,
                os_name="NetBSD",
                os_release="6.0",
                arch="evbarm",
                elf_endianness="little",
            ),
            compatibility=None,
        )

    def callbacks(self) -> tuple[ConfigureEnableSshCallbacks, list[str], list[str], list[dict[str, object]], list[dict[str, object]]]:
        stages: list[str] = []
        logs: list[str] = []
        debug_fields: list[dict[str, object]] = []
        update_fields: list[dict[str, object]] = []
        return (
            ConfigureEnableSshCallbacks(
                set_stage=stages.append,
                log=logs.append,
                add_debug_fields=lambda **fields: debug_fields.append(fields),
                update_fields=lambda **fields: update_fields.append(fields),
            ),
            stages,
            logs,
            debug_fields,
            update_fields,
        )

    def test_enable_ssh_and_reprobe_enables_waits_and_reprobes(self) -> None:
        connection = self.make_connection()
        probe_state = self.make_probe_state()
        callbacks, stages, logs, debug_fields, update_fields = self.callbacks()
        with mock.patch("timecapsulesmb.services.configure.enable_ssh") as enable_ssh:
            with mock.patch("timecapsulesmb.services.configure.wait_for_tcp_port_state", return_value=True) as wait:
                with mock.patch("timecapsulesmb.services.configure.probe_connection_state", return_value=probe_state) as probe:
                    result = enable_ssh_and_reprobe(connection, timeout_seconds=12, callbacks=callbacks)

        self.assertIs(result, probe_state)
        enable_ssh.assert_called_once_with("10.0.0.2", "pw", reboot_device=True, log=callbacks.log)
        wait.assert_called_once_with(
            "10.0.0.2",
            22,
            expected_state=True,
            timeout_seconds=12,
            service_name="SSH port",
            log=callbacks.log,
        )
        probe.assert_called_once_with(connection)
        self.assertEqual(stages, ["acp_enable_ssh", "wait_for_ssh_after_acp", "ssh_probe_after_acp"])
        self.assertEqual(
            debug_fields,
            [
                {"configure_acp_enable_attempted": True, "ssh_initially_reachable": False},
                {"configure_acp_enable_succeeded": True},
            ],
        )
        self.assertEqual(update_fields, [{"ssh_final_reachable": True}])
        self.assertIn("Attempting to enable SSH", logs[0])

    def test_enable_ssh_and_reprobe_records_auth_failure_and_propagates(self) -> None:
        callbacks, _stages, _logs, debug_fields, update_fields = self.callbacks()
        with mock.patch("timecapsulesmb.services.configure.enable_ssh", side_effect=ACPAuthError("bad password")):
            with mock.patch("timecapsulesmb.services.configure.wait_for_tcp_port_state") as wait:
                with mock.patch("timecapsulesmb.services.configure.probe_connection_state") as probe:
                    with self.assertRaises(ACPAuthError):
                        enable_ssh_and_reprobe(self.make_connection(), callbacks=callbacks)

        wait.assert_not_called()
        probe.assert_not_called()
        self.assertEqual(
            debug_fields[-1],
            {
                "configure_acp_enable_succeeded": False,
                "configure_retry_reason": "acp_authentication_failed",
            },
        )
        self.assertEqual(update_fields, [])

    def test_enable_ssh_and_reprobe_records_generic_acp_failure_and_propagates(self) -> None:
        callbacks, _stages, _logs, debug_fields, update_fields = self.callbacks()
        with mock.patch("timecapsulesmb.services.configure.enable_ssh", side_effect=ACPConnectionError("connection failed")):
            with mock.patch("timecapsulesmb.services.configure.wait_for_tcp_port_state") as wait:
                with mock.patch("timecapsulesmb.services.configure.probe_connection_state") as probe:
                    with self.assertRaises(ACPConnectionError):
                        enable_ssh_and_reprobe(self.make_connection(), callbacks=callbacks)

        wait.assert_not_called()
        probe.assert_not_called()
        self.assertEqual(debug_fields[-1], {"configure_acp_enable_succeeded": False})
        self.assertEqual(update_fields, [])

    def test_enable_ssh_and_reprobe_returns_none_when_ssh_does_not_open(self) -> None:
        callbacks, stages, _logs, _debug_fields, update_fields = self.callbacks()
        with mock.patch("timecapsulesmb.services.configure.enable_ssh"):
            with mock.patch("timecapsulesmb.services.configure.wait_for_tcp_port_state", return_value=False):
                with mock.patch("timecapsulesmb.services.configure.probe_connection_state") as probe:
                    result = enable_ssh_and_reprobe(self.make_connection(), callbacks=callbacks)

        self.assertIsNone(result)
        probe.assert_not_called()
        self.assertEqual(stages, ["acp_enable_ssh", "wait_for_ssh_after_acp"])
        self.assertEqual(update_fields, [{"ssh_final_reachable": False}])


if __name__ == "__main__":
    unittest.main()
