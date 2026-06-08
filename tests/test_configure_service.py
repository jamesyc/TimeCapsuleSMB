from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from typing import Mapping
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from timecapsulesmb.device.compat import compatibility_from_probe_result
from timecapsulesmb.device.probe import ProbeResult, ProbedDeviceState
from timecapsulesmb.integrations.acp import ACP_PORT, ACPAuthError, ACPConnectionError
from timecapsulesmb.services.configure import (
    ConfigureFlowError,
    ConfigureFlowHooks,
    ConfigureFlowRequest,
    build_configure_env_values,
    enable_ssh_and_reprobe,
    run_configure_flow,
)
from timecapsulesmb.services.callbacks import OperationCallbacks
from timecapsulesmb.transport.ssh import SshConnection


class ConfigureServiceTests(unittest.TestCase):
    def test_build_configure_env_values_handles_smb_browse_compatibility(self) -> None:
        preserved = build_configure_env_values(
            {"TC_SMB_BROWSE_COMPATIBILITY": "true"},
            host="root@10.0.0.2",
            password="pw",
            ssh_opts="-o foo",
            configure_id="config-id",
        )
        enabled = build_configure_env_values(
            {},
            host="root@10.0.0.2",
            password="pw",
            ssh_opts="-o foo",
            configure_id="config-id",
            smb_browse_compatibility=True,
        )

        self.assertEqual(preserved["TC_SMB_BROWSE_COMPATIBILITY"], "true")
        self.assertEqual(enabled["TC_SMB_BROWSE_COMPATIBILITY"], "true")

    def make_connection(self) -> SshConnection:
        return SshConnection("root@10.0.0.2", "pw", "-o foo")

    def make_probe_state(self) -> ProbedDeviceState:
        probe_result = ProbeResult(
            ssh_port_reachable=True,
            ssh_authenticated=True,
            error=None,
            os_name="NetBSD",
            os_release="6.0",
            arch="evbarm",
            elf_endianness="little",
            airport_model="TimeCapsule8,119",
            airport_syap="119",
        )
        return ProbedDeviceState(
            probe_result=probe_result,
            compatibility=compatibility_from_probe_result(probe_result),
        )

    def make_auth_failed_probe_state(self) -> ProbedDeviceState:
        return ProbedDeviceState(
            probe_result=ProbeResult(
                ssh_port_reachable=True,
                ssh_authenticated=False,
                error="SSH authentication failed.",
                os_name=None,
                os_release=None,
                arch=None,
                elf_endianness=None,
            ),
            compatibility=None,
        )

    def make_unsupported_probe_state(self) -> ProbedDeviceState:
        probe_result = ProbeResult(
            ssh_port_reachable=True,
            ssh_authenticated=True,
            error=None,
            os_name="NetBSD",
            os_release="5.0",
            arch="evbarm",
            elf_endianness="little",
        )
        return ProbedDeviceState(
            probe_result=probe_result,
            compatibility=compatibility_from_probe_result(probe_result),
        )

    def callbacks(self) -> tuple[OperationCallbacks, list[str], list[str], list[dict[str, object]], list[dict[str, object]]]:
        stages: list[str] = []
        logs: list[str] = []
        debug_fields: list[dict[str, object]] = []
        update_fields: list[dict[str, object]] = []
        return (
            OperationCallbacks(
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
        with mock.patch("timecapsulesmb.services.acp_ssh.tcp_open", return_value=True) as tcp_open:
            with mock.patch("timecapsulesmb.services.acp_ssh.enable_ssh") as enable_ssh:
                with mock.patch("timecapsulesmb.services.configure.wait_for_tcp_port_state", return_value=True) as wait:
                    with mock.patch("timecapsulesmb.services.configure.probe_connection_state", return_value=probe_state) as probe:
                        result = enable_ssh_and_reprobe(connection, timeout_seconds=12, callbacks=callbacks)

        self.assertIs(result, probe_state)
        tcp_open.assert_called_once_with("10.0.0.2", ACP_PORT)
        enable_ssh.assert_called_once_with("10.0.0.2", "pw", reboot_device=True, log=callbacks.log, timeout=25.0)
        wait.assert_called_once_with(
            "10.0.0.2",
            22,
            expected_state=True,
            timeout_seconds=12,
            service_name="SSH port",
            log=callbacks.log,
        )
        probe.assert_called_once_with(connection)
        self.assertEqual(stages, ["acp_port_probe", "acp_enable_ssh", "wait_for_ssh_after_acp", "ssh_probe_after_acp"])
        self.assertEqual(
            debug_fields,
            [
                {"configure_acp_enable_attempted": True, "ssh_initially_reachable": False},
                {"acp_port_probe_attempted": True},
                {"acp_port_probe_succeeded": True},
                {"acp_ssh_enable_attempted": True},
                {"acp_ssh_enable_succeeded": True},
                {"configure_acp_enable_succeeded": True},
            ],
        )
        self.assertEqual(update_fields, [{"ssh_final_reachable": True}])
        self.assertIn("Attempting to enable SSH", logs[0])

    def test_enable_ssh_and_reprobe_port_preflight_fails_fast_when_acp_port_is_closed(self) -> None:
        callbacks, stages, _logs, debug_fields, update_fields = self.callbacks()
        with mock.patch("timecapsulesmb.services.acp_ssh.tcp_open", return_value=False) as tcp_open:
            with mock.patch("timecapsulesmb.services.acp_ssh.enable_ssh") as enable_ssh:
                with mock.patch("timecapsulesmb.services.configure.wait_for_tcp_port_state") as wait:
                    with mock.patch("timecapsulesmb.services.configure.probe_connection_state") as probe:
                        with self.assertRaises(ACPConnectionError) as raised:
                            enable_ssh_and_reprobe(self.make_connection(), callbacks=callbacks)

        self.assertIn("Could not connect to ACP on 10.0.0.2:5009", str(raised.exception))
        tcp_open.assert_called_once_with("10.0.0.2", ACP_PORT)
        enable_ssh.assert_not_called()
        wait.assert_not_called()
        probe.assert_not_called()
        self.assertEqual(stages, ["acp_port_probe"])
        self.assertEqual(
            debug_fields,
            [
                {"configure_acp_enable_attempted": True, "ssh_initially_reachable": False},
                {"acp_port_probe_attempted": True},
                {"acp_port_probe_succeeded": False},
                {"configure_acp_enable_succeeded": False},
            ],
        )
        self.assertEqual(update_fields, [])

    def test_enable_ssh_and_reprobe_records_auth_failure_and_propagates(self) -> None:
        callbacks, _stages, _logs, debug_fields, update_fields = self.callbacks()
        with mock.patch("timecapsulesmb.services.acp_ssh.tcp_open", return_value=True):
            with mock.patch("timecapsulesmb.services.acp_ssh.enable_ssh", side_effect=ACPAuthError("bad password")) as enable_ssh:
                with mock.patch("timecapsulesmb.services.configure.wait_for_tcp_port_state") as wait:
                    with mock.patch("timecapsulesmb.services.configure.probe_connection_state") as probe:
                        with self.assertRaises(ACPAuthError):
                            enable_ssh_and_reprobe(self.make_connection(), callbacks=callbacks)

        enable_ssh.assert_called_once()
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
        with mock.patch("timecapsulesmb.services.acp_ssh.tcp_open", return_value=True):
            with mock.patch("timecapsulesmb.services.acp_ssh.enable_ssh", side_effect=ACPConnectionError("connection failed")) as enable_ssh:
                with mock.patch("timecapsulesmb.services.configure.wait_for_tcp_port_state") as wait:
                    with mock.patch("timecapsulesmb.services.configure.probe_connection_state") as probe:
                        with self.assertRaises(ACPConnectionError):
                            enable_ssh_and_reprobe(self.make_connection(), callbacks=callbacks)

        enable_ssh.assert_called_once()
        wait.assert_not_called()
        probe.assert_not_called()
        self.assertEqual(debug_fields[-1], {"configure_acp_enable_succeeded": False})
        self.assertEqual(update_fields, [])

    def test_enable_ssh_and_reprobe_returns_none_when_ssh_does_not_open(self) -> None:
        callbacks, stages, _logs, _debug_fields, update_fields = self.callbacks()
        with mock.patch("timecapsulesmb.services.acp_ssh.tcp_open", return_value=True):
            with mock.patch("timecapsulesmb.services.acp_ssh.enable_ssh"):
                with mock.patch("timecapsulesmb.services.configure.wait_for_tcp_port_state", return_value=False):
                    with mock.patch("timecapsulesmb.services.configure.probe_connection_state") as probe:
                        result = enable_ssh_and_reprobe(self.make_connection(), callbacks=callbacks)

        self.assertIsNone(result)
        probe.assert_not_called()
        self.assertEqual(stages, ["acp_port_probe", "acp_enable_ssh", "wait_for_ssh_after_acp"])
        self.assertEqual(update_fields, [{"ssh_final_reachable": False}])

    def test_run_configure_flow_probes_writes_identity_and_reports_context(self) -> None:
        probe_state = self.make_probe_state()
        written: dict[str, str] = {}
        callbacks, stages, _logs, debug_fields, update_fields = self.callbacks()
        seen_probe_states: list[ProbedDeviceState] = []

        def write_env(_path: Path, values: Mapping[str, str]) -> None:
            written.update(values)

        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            result = run_configure_flow(
                ConfigureFlowRequest(
                    existing={},
                    env_path=env_path,
                    host="root@10.0.0.2",
                    password="pw",
                    ssh_opts="-o foo",
                    configure_id="config-id",
                    persist_password=False,
                    probe=mock.Mock(return_value=probe_state),
                    write_env=write_env,
                ),
                callbacks=callbacks,
                hooks=ConfigureFlowHooks(after_probe=lambda _connection, state: seen_probe_states.append(state)),
            )

        self.assertIs(result.probe_state, probe_state)
        self.assertEqual(seen_probe_states, [probe_state])
        self.assertEqual(result.identity.syap, "119")
        self.assertEqual(result.identity.model, "TimeCapsule8,119")
        self.assertEqual(written["TC_HOST"], "root@10.0.0.2")
        self.assertEqual(written["TC_SMB_BROWSE_COMPATIBILITY"], "false")
        self.assertNotIn("TC_PASSWORD", written)
        self.assertEqual(stages, ["ssh_probe", "write_env"])
        self.assertIn({"ssh_final_reachable": True}, debug_fields)
        self.assertIn({"ssh_final_reachable": True}, update_fields)
        self.assertIn({"configure_id": "config-id", "device_syap": "119", "device_model": "TimeCapsule8,119"}, update_fields)

    def test_run_configure_flow_can_save_reachable_target_without_authentication(self) -> None:
        probe_state = self.make_auth_failed_probe_state()
        written: dict[str, str] = {}

        with tempfile.TemporaryDirectory() as tmp:
            result = run_configure_flow(
                ConfigureFlowRequest(
                    existing={},
                    env_path=Path(tmp) / ".env",
                    host="root@10.0.0.2",
                    password="badpw",
                    ssh_opts="-o foo",
                    configure_id="config-id",
                    persist_password=True,
                    discovered_airport_syap="119",
                    probe=mock.Mock(return_value=probe_state),
                    write_env=lambda _path, values: written.update(values),
                ),
                hooks=ConfigureFlowHooks(save_without_authentication=lambda _state: True),
            )

        self.assertIs(result.probe_state, probe_state)
        self.assertEqual(written["TC_PASSWORD"], "badpw")
        self.assertEqual(written["TC_AIRPORT_SYAP"], "119")
        self.assertEqual(written["TC_MDNS_DEVICE_MODEL"], "TimeCapsule8,119")

    def test_run_configure_flow_rejects_unsupported_compatible_probe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ConfigureFlowError) as raised:
                run_configure_flow(
                    ConfigureFlowRequest(
                        existing={},
                        env_path=Path(tmp) / ".env",
                        host="root@10.0.0.2",
                        password="pw",
                        ssh_opts="-o foo",
                        configure_id="config-id",
                        persist_password=True,
                        probe=mock.Mock(return_value=self.make_unsupported_probe_state()),
                    )
                )

        self.assertEqual(raised.exception.code, "unsupported_device")


if __name__ == "__main__":
    unittest.main()
