from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Mapping
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from timecapsulesmb.device.compat import compatibility_from_probe_result
from timecapsulesmb.device.probe import AirportAcpReading, ProbeResult, ProbedDeviceState, SshAccessStatus
from timecapsulesmb.discovery.bonjour import BonjourResolvedService
from timecapsulesmb.integrations.acp import ACP_PORT, DBUG_SSH_VALUE, ACPAuthError, ACPConnectionError
from timecapsulesmb.services.acp_ssh import SSH_ENABLE_TIMEOUT_MESSAGE, AirportIdentityMismatchError, enable_ssh_with_port_preflight
from timecapsulesmb.services.reboot import RebootFlowError
from timecapsulesmb.services.configure import (
    AIRPORT_ADMIN_PASSWORD_REJECTED_MESSAGE,
    SSH_ONLY_PASSWORD_DEBUG,
    ConfigureFlowError,
    ConfigureFlowHooks,
    ConfigureFlowRequest,
    build_configure_env_values,
    enable_ssh_and_reprobe,
    run_configure_flow,
)
from timecapsulesmb.services.callbacks import OperationCallbacks
from timecapsulesmb.transport.ssh import SshConnection
from timecapsulesmb.services.locate import LocateResult
from tests.reboot_support import DEVICE_AIRPORT_MAC, acp_password_answer, acp_reading


# Stand-ins for the ACP network diagnostics, which read this computer's
# interfaces; tests/test_acp_diagnostics.py covers what they contain.
PROBE_CONTEXT = {"acp_target_addresses": [{"role": "target", "link": "on_link"}], "local_networks": []}
PROBE_SUCCEEDED = {"acp_port_probe_succeeded": True, "acp_port_probe_error_kinds": []}
RECORD_WITH_MAC = BonjourResolvedService(
    name="Office Capsule",
    hostname="Office-Capsule.local",
    service_type="_airport._tcp.local.",
    port=5009,
    ipv4=["192.168.1.218"],
    properties={"syAP": "119", "waMA": DEVICE_AIRPORT_MAC.upper().replace(":", "-")},
    fullname="Office Capsule._airport._tcp.local.",
)
SELECTED_RECORD = BonjourResolvedService(
    name="Time Capsule b67fdb",
    hostname="Time-Capsule-b67fdb.local",
    service_type="_airport._tcp.local.",
    port=5009,
    ipv4=["10.0.1.1"],
    properties={"raNm": "Apple Network b67fdb", "raNA": "1"},
    fullname="Time Capsule b67fdb._airport._tcp.local.",
)


class ConfigureServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        context = mock.patch(
            "timecapsulesmb.services.acp_diagnostics.probe_context_fields",
            side_effect=lambda *_args, **_kwargs: dict(PROBE_CONTEXT),
        )
        self.probe_context = context.start()
        self.addCleanup(context.stop)

    def test_build_configure_env_values_handles_advanced_metadata_settings(self) -> None:
        preserved = build_configure_env_values(
            {
                "TC_SMB_BROWSE_COMPATIBILITY": "true",
                "TC_MDNS_ADVERTISE_AFP": "true",
                "TC_REQUIRE_SMB_ENCRYPTION": "true",
                "TC_FRUIT_METADATA_NETATALK": "true",
                "TC_VFS_AIO_FORK_ENABLED": "true",
            },
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
            mdns_advertise_afp=True,
            require_smb_encryption=True,
            fruit_metadata_netatalk=True,
            vfs_aio_fork_enabled=True,
        )

        self.assertEqual(preserved["TC_SMB_BROWSE_COMPATIBILITY"], "true")
        self.assertEqual(preserved["TC_MDNS_ADVERTISE_AFP"], "true")
        self.assertEqual(preserved["TC_REQUIRE_SMB_ENCRYPTION"], "true")
        self.assertEqual(preserved["TC_FRUIT_METADATA_NETATALK"], "true")
        self.assertEqual(preserved["TC_VFS_AIO_FORK_ENABLED"], "true")
        self.assertEqual(enabled["TC_SMB_BROWSE_COMPATIBILITY"], "true")
        self.assertEqual(enabled["TC_MDNS_ADVERTISE_AFP"], "true")
        self.assertEqual(enabled["TC_REQUIRE_SMB_ENCRYPTION"], "true")
        self.assertEqual(enabled["TC_FRUIT_METADATA_NETATALK"], "true")
        self.assertEqual(enabled["TC_VFS_AIO_FORK_ENABLED"], "true")

    def test_build_configure_env_values_rejects_smb_encryption_with_any_protocol(self) -> None:
        with self.assertRaisesRegex(ValueError, "SMB encryption requires SMB3-only"):
            build_configure_env_values(
                {},
                host="root@10.0.0.2",
                password="pw",
                ssh_opts="-o foo",
                configure_id="config-id",
                any_protocol=True,
                require_smb_encryption=True,
            )

    def test_build_configure_env_values_preserves_and_enables_forced_smb_security_disable(self) -> None:
        preserved = build_configure_env_values(
            {"TC_FORCE_DISABLE_SMB_SIGNING_AND_ENCRYPTION": "true"},
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
            force_disable_smb_signing_and_encryption=True,
        )

        self.assertEqual(preserved["TC_FORCE_DISABLE_SMB_SIGNING_AND_ENCRYPTION"], "true")
        self.assertEqual(enabled["TC_FORCE_DISABLE_SMB_SIGNING_AND_ENCRYPTION"], "true")

    def test_build_configure_env_values_rejects_required_and_disabled_smb_encryption(self) -> None:
        with self.assertRaisesRegex(ValueError, "cannot be used with Force Disable"):
            build_configure_env_values(
                {},
                host="root@10.0.0.2",
                password="pw",
                ssh_opts="-o foo",
                configure_id="config-id",
                require_smb_encryption=True,
                force_disable_smb_signing_and_encryption=True,
            )

    def make_connection(self) -> SshConnection:
        return SshConnection("root@10.0.0.2", "pw", "-o foo")

    def make_probe_state(self) -> ProbedDeviceState:
        probe_result = ProbeResult(
            ssh_status=SshAccessStatus.OPEN_AUTHENTICATED,
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
                ssh_status=SshAccessStatus.AUTH_REJECTED,
                error="SSH authentication failed.",
                os_name="",
                os_release="",
                arch="",
                elf_endianness="unknown",
            ),
            compatibility=None,
        )

    def make_ssh_algorithm_failed_probe_state(self) -> ProbedDeviceState:
        return ProbedDeviceState(
            probe_result=ProbeResult(
                ssh_status=SshAccessStatus.ALGORITHM_NEGOTIATION_FAILED,
                error=(
                    "Unable to negotiate with 192.168.200.214 port 22: no matching MAC found. "
                    "Their offer: hmac-md5,hmac-sha1,hmac-md5-96"
                ),
                os_name="",
                os_release="",
                arch="",
                elf_endianness="unknown",
            ),
            compatibility=None,
        )

    def make_unsupported_probe_state(self) -> ProbedDeviceState:
        probe_result = ProbeResult(
            ssh_status=SshAccessStatus.OPEN_AUTHENTICATED,
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

    def make_airport_express_probe_state(self) -> ProbedDeviceState:
        # What an AirPort Express answers over SSH (v3.1.2 telemetry: NetBSD 4.0_STABLE (ar7240)).
        probe_result = ProbeResult(
            ssh_status=SshAccessStatus.OPEN_AUTHENTICATED,
            error=None,
            os_name="NetBSD",
            os_release="4.0_STABLE",
            arch="ar7240",
            elf_endianness="big",
        )
        return ProbedDeviceState(
            probe_result=probe_result,
            compatibility=compatibility_from_probe_result(probe_result),
        )

    def make_ssh_closed_probe_state(self) -> ProbedDeviceState:
        return ProbedDeviceState(
            probe_result=ProbeResult(
                ssh_status=SshAccessStatus.CLOSED,
                error="SSH is not reachable yet.",
                os_name="",
                os_release="",
                arch="",
                elf_endianness="unknown",
            ),
            compatibility=None,
        )

    def configure_request(self, env_path: Path, probe: mock.Mock, **overrides: object) -> ConfigureFlowRequest:
        fields: dict[str, object] = {
            "existing": {},
            "env_path": env_path,
            "host": "root@10.0.0.2",
            "password": "pw",
            "ssh_opts": "-o foo",
            "configure_id": "config-id",
            "persist_password": True,
            "probe": probe,
        }
        fields.update(overrides)
        return ConfigureFlowRequest(**fields)  # type: ignore[arg-type]

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
        with mock.patch("timecapsulesmb.services.acp_ssh.tcp_connect_error", return_value=None) as tcp_connect_error:
            with mock.patch("timecapsulesmb.services.acp_ssh.set_dbug") as enable_ssh:
                with mock.patch("timecapsulesmb.services.configure.reboot_device", return_value=None) as wait:
                    with mock.patch("timecapsulesmb.services.configure.probe_connection_state", return_value=probe_state) as probe:
                        result = enable_ssh_and_reprobe(connection, callbacks=callbacks)

        self.assertEqual(result, (connection, probe_state))
        tcp_connect_error.assert_called_once_with("10.0.0.2", ACP_PORT)
        enable_ssh.assert_called_once_with("10.0.0.2", "pw", DBUG_SSH_VALUE, log=callbacks.log, timeout=25.0)
        # SSH turns on at the next boot: the shared reboot path restarts the
        # device and waits until SSH answers.
        wait.assert_called_once_with(
            "10.0.0.2",
            "pw",
            wait=True,
            callbacks=callbacks,
            up_timeout_message=SSH_ENABLE_TIMEOUT_MESSAGE,
        )
        probe.assert_called_once_with(connection)
        self.assertEqual(stages, ["acp_port_probe", "acp_enable_ssh", "ssh_probe_after_acp"])
        self.assertEqual(
            debug_fields,
            [
                {"configure_acp_enable_attempted": True, "ssh_initially_reachable": False},
                {"acp_port_probe_attempted": True},
                {"acp_port_probe_succeeded": True, "acp_port_probe_attempts": 1},
                {"acp_ssh_enable_attempted": True},
                {"acp_ssh_enable_succeeded": True},
                {"configure_acp_enable_succeeded": True},
            ],
        )
        self.assertEqual(update_fields, [PROBE_SUCCEEDED, PROBE_CONTEXT, {"ssh_final_reachable": True}])
        self.probe_context.assert_called_once_with("10.0.0.2", None)
        self.assertIn("Attempting to enable SSH", logs[0])

    def test_enable_ssh_with_port_preflight_happy_path_runs_enable_without_retry(self) -> None:
        callbacks, stages, _logs, debug_fields, _update_fields = self.callbacks()
        tcp_connect_error = mock.Mock(return_value=None)
        sleep = mock.Mock()
        with mock.patch("timecapsulesmb.services.acp_ssh.set_dbug") as enable_ssh:
            enable_ssh_with_port_preflight(
                "10.0.0.2",
                "pw",
                callbacks=callbacks,
                tcp_connect_error_func=tcp_connect_error,
                sleep_func=sleep,
            )

        tcp_connect_error.assert_called_once_with("10.0.0.2", ACP_PORT)
        sleep.assert_not_called()
        enable_ssh.assert_called_once_with("10.0.0.2", "pw", DBUG_SSH_VALUE, log=callbacks.log, timeout=25.0)
        self.assertEqual(stages, ["acp_port_probe", "acp_enable_ssh"])
        self.assertEqual(
            debug_fields,
            [
                {"acp_port_probe_attempted": True},
                {"acp_port_probe_succeeded": True, "acp_port_probe_attempts": 1},
                {"acp_ssh_enable_attempted": True},
                {"acp_ssh_enable_succeeded": True},
            ],
        )

    def test_enable_ssh_and_reprobe_port_preflight_retries_before_failing_when_acp_port_is_closed(self) -> None:
        callbacks, stages, _logs, debug_fields, update_fields = self.callbacks()
        with mock.patch("timecapsulesmb.services.acp_ssh.tcp_connect_error", return_value="Connection refused") as tcp_connect_error:
            with mock.patch("timecapsulesmb.services.acp_ssh.time.sleep") as sleep:
                with mock.patch("timecapsulesmb.services.acp_ssh.set_dbug") as enable_ssh:
                    with mock.patch("timecapsulesmb.services.configure.reboot_device", return_value=None) as wait:
                        with mock.patch("timecapsulesmb.services.configure.probe_connection_state") as probe:
                            with self.assertRaises(ACPConnectionError) as raised:
                                enable_ssh_and_reprobe(self.make_connection(), callbacks=callbacks)

        self.assertIn("Could not connect to ACP on 10.0.0.2:5009", str(raised.exception))
        self.assertEqual(tcp_connect_error.call_args_list, [
            mock.call("10.0.0.2", ACP_PORT),
            mock.call("10.0.0.2", ACP_PORT),
            mock.call("10.0.0.2", ACP_PORT),
        ])
        self.assertEqual(sleep.call_args_list, [mock.call(2.0), mock.call(2.0)])
        enable_ssh.assert_not_called()
        wait.assert_not_called()
        probe.assert_not_called()
        self.assertEqual(stages, ["acp_port_probe"])
        self.assertEqual(
            debug_fields,
            [
                {"configure_acp_enable_attempted": True, "ssh_initially_reachable": False},
                {"acp_port_probe_attempted": True},
                {
                    "acp_port_probe_succeeded": False,
                    "acp_port_probe_attempts": 3,
                    "acp_port_probe_errors": [
                        {"attempt": 1, "error": "Connection refused", "kind": "refused"},
                        {"attempt": 2, "error": "Connection refused", "kind": "refused"},
                        {"attempt": 3, "error": "Connection refused", "kind": "refused"},
                    ],
                    "acp_port_probe_last_error": "Connection refused",
                },
                {"configure_acp_enable_succeeded": False},
            ],
        )
        self.assertEqual(update_fields, [
            {"acp_port_probe_succeeded": False, "acp_port_probe_error_kinds": ["refused", "refused", "refused"]},
            PROBE_CONTEXT,
        ])

    def test_enable_ssh_with_port_preflight_retries_transient_acp_failures_before_success(self) -> None:
        callbacks, stages, _logs, debug_fields, _update_fields = self.callbacks()
        tcp_connect_error = mock.Mock(side_effect=["Connection refused", "timed out", None])
        sleep = mock.Mock()
        with mock.patch("timecapsulesmb.services.acp_ssh.set_dbug") as enable_ssh:
            enable_ssh_with_port_preflight(
                "10.0.0.2",
                "pw",
                callbacks=callbacks,
                tcp_connect_error_func=tcp_connect_error,
                sleep_func=sleep,
            )

        self.assertEqual(tcp_connect_error.call_args_list, [
            mock.call("10.0.0.2", ACP_PORT),
            mock.call("10.0.0.2", ACP_PORT),
            mock.call("10.0.0.2", ACP_PORT),
        ])
        self.assertEqual(sleep.call_args_list, [mock.call(2.0), mock.call(2.0)])
        enable_ssh.assert_called_once_with("10.0.0.2", "pw", DBUG_SSH_VALUE, log=callbacks.log, timeout=25.0)
        self.assertEqual(stages, ["acp_port_probe", "acp_enable_ssh"])
        self.assertEqual(
            debug_fields,
            [
                {"acp_port_probe_attempted": True},
                {
                    "acp_port_probe_succeeded": True,
                    "acp_port_probe_attempts": 3,
                    "acp_port_probe_errors": [
                        {"attempt": 1, "error": "Connection refused", "kind": "refused"},
                        {"attempt": 2, "error": "timed out", "kind": "timeout"},
                    ],
                    "acp_port_probe_last_error": "timed out",
                },
                {"acp_ssh_enable_attempted": True},
                {"acp_ssh_enable_succeeded": True},
            ],
        )

    def test_port_probe_records_its_context_with_the_selected_record(self) -> None:
        callbacks, _stages, _logs, _debug_fields, update_fields = self.callbacks()
        with mock.patch("timecapsulesmb.services.acp_ssh.set_dbug") as enable_ssh:
            enable_ssh_with_port_preflight(
                "10.0.1.1",
                "pw",
                callbacks=callbacks,
                record=SELECTED_RECORD,
                tcp_connect_error_func=mock.Mock(side_effect=["timed out", None]),
                sleep_func=mock.Mock(),
            )

        enable_ssh.assert_called_once()
        self.probe_context.assert_called_once_with("10.0.1.1", SELECTED_RECORD)
        self.assertEqual(update_fields, [
            {"acp_port_probe_succeeded": True, "acp_port_probe_error_kinds": ["timeout"]},
            PROBE_CONTEXT,
        ])

    def preflight(
        self,
        *,
        port_answers: bool,
        located: LocateResult | None = None,
        reading: AirportAcpReading | None = None,
        record: BonjourResolvedService = RECORD_WITH_MAC,
    ):
        """enable_ssh_with_port_preflight against the record's 192.168.1.218.

        `port_answers` is whether ACP's port answers there, `reading` the
        network ACP read of it, `located` where locate_airport finds the device."""
        callbacks, _stages, logs, _debug, update_fields = self.callbacks()
        locate = mock.Mock(return_value=located or LocateResult("not_found"))
        read = mock.Mock(return_value=reading or acp_reading(True))
        outcome: object
        with mock.patch("timecapsulesmb.services.acp_ssh.set_dbug") as set_dbug, \
                mock.patch("timecapsulesmb.services.acp_ssh.locate_airport", locate), \
                mock.patch("timecapsulesmb.device.probe.read_airport_acp", read), \
                mock.patch("timecapsulesmb.services.acp_ssh.local_lan_networks", return_value=()):
            try:
                outcome = enable_ssh_with_port_preflight(
                    "192.168.1.218",
                    "pw",
                    callbacks=callbacks,
                    record=record,
                    tcp_connect_error_func=mock.Mock(return_value=None if port_answers else "timed out"),
                    sleep_func=mock.Mock(),
                )
            except Exception as exc:
                outcome = exc
        return SimpleNamespace(outcome=outcome, set_dbug=set_dbug, locate=locate, read=read, logs=logs, updates=update_fields)

    def test_a_device_that_moved_gets_ssh_turned_on_at_its_new_address(self) -> None:
        run = self.preflight(port_answers=False, located=LocateResult("found", host="root@192.168.1.40", address="192.168.1.40"))

        self.assertEqual(run.outcome, "192.168.1.40")
        run.locate.assert_called_once_with(
            DEVICE_AIRPORT_MAC, "pw", current_host="192.168.1.218", trigger="acp_unreachable", callbacks=mock.ANY,
        )
        self.assertEqual(run.set_dbug.call_args.args[:2], ("192.168.1.40", "pw"))
        self.assertIn({"current_host": "root@192.168.1.40"}, run.updates)
        self.assertIn("The device now answers at 192.168.1.40.", run.logs)

    def test_a_device_found_nowhere_else_keeps_the_original_connection_error(self) -> None:
        run = self.preflight(port_answers=False)

        self.assertIsInstance(run.outcome, ACPConnectionError)
        self.assertIn("Could not connect to ACP on 192.168.1.218:5009", str(run.outcome))
        run.set_dbug.assert_not_called()

    def test_a_device_found_with_another_password_is_reported_and_keeps_the_connection_error(self) -> None:
        # The record's MAC may be a stale cache entry for an address another
        # AirPort took, so it is only reported.
        run = self.preflight(port_answers=False, located=LocateResult("password_rejected", address="10.0.1.1"))

        self.assertIsInstance(run.outcome, ACPConnectionError)
        self.assertIn("Could not connect to ACP on 192.168.1.218:5009", str(run.outcome))
        self.assertIn(LocateResult("password_rejected", address="10.0.1.1").rejected_note, run.logs)
        run.set_dbug.assert_not_called()

    def test_another_airport_at_the_address_is_never_changed(self) -> None:
        other = AirportAcpReading(password_matches=True, airport_mac="02:00:00:00:00:02")
        cases = {
            "selected one found elsewhere": (LocateResult("found", host="root@192.168.1.40", address="192.168.1.40"), "192.168.1.40"),
            "selected one not found": (LocateResult("not_found"), None),
        }
        for case, (located, enabled_at) in cases.items():
            with self.subTest(case=case):
                run = self.preflight(port_answers=True, reading=other, located=located)

                self.assertEqual(run.locate.call_args.kwargs["trigger"], "identity_mismatch")
                if enabled_at is None:
                    self.assertIsInstance(run.outcome, AirportIdentityMismatchError)
                    self.assertIn("A different AirPort answers at 192.168.1.218", str(run.outcome))
                    run.set_dbug.assert_not_called()
                else:
                    self.assertEqual(run.outcome, enabled_at)
                    self.assertEqual(run.set_dbug.call_args.args[0], enabled_at)

    def test_a_rejected_password_stops_before_the_acp_write(self) -> None:
        run = self.preflight(port_answers=True, reading=acp_reading(False))

        self.assertIsInstance(run.outcome, ACPAuthError)
        run.set_dbug.assert_not_called()
        run.locate.assert_not_called()

    def test_the_selected_device_or_an_unanswered_read_goes_on_to_the_acp_write(self) -> None:
        for case, reading in {"same MAC": acp_reading(True), "no answer": acp_reading(None)}.items():
            with self.subTest(case=case):
                run = self.preflight(port_answers=True, reading=reading)

                self.assertEqual(run.outcome, "192.168.1.218")
                run.read.assert_called_once_with("192.168.1.218", "pw")
                run.locate.assert_not_called()
                self.assertEqual(run.set_dbug.call_args.args[0], "192.168.1.218")

    def test_without_the_devices_mac_nothing_is_read_or_looked_for(self) -> None:
        for port_answers in (True, False):
            with self.subTest(port_answers=port_answers):
                run = self.preflight(port_answers=port_answers, record=SELECTED_RECORD)

                run.read.assert_not_called()
                run.locate.assert_not_called()
                self.assertEqual(run.set_dbug.called, port_answers)

    def test_diagnostics_errors_are_recorded_without_changing_the_outcome(self) -> None:
        for connect_errors, outcome in ((None, "enabled"), ("Connection refused", "acp_unreachable")):
            with self.subTest(outcome=outcome):
                callbacks, _stages, _logs, _debug_fields, update_fields = self.callbacks()
                self.probe_context.side_effect = RuntimeError("no interfaces")
                with mock.patch("timecapsulesmb.services.acp_ssh.set_dbug") as enable_ssh:
                    try:
                        enable_ssh_with_port_preflight(
                            "10.0.0.2",
                            "pw",
                            callbacks=callbacks,
                            record=SELECTED_RECORD,
                            tcp_connect_error_func=mock.Mock(return_value=connect_errors),
                            sleep_func=mock.Mock(),
                        )
                        result = "enabled"
                    except ACPConnectionError:
                        result = "acp_unreachable"

                self.assertEqual(result, outcome)
                self.assertEqual(enable_ssh.called, outcome == "enabled")
                self.assertEqual(update_fields[-1], {"acp_diagnostics_error": "RuntimeError: no interfaces"})

    def test_run_configure_flow_hands_the_selected_record_to_the_acp_probe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch(
                "timecapsulesmb.services.configure.enable_ssh_and_reprobe",
                side_effect=ACPConnectionError("no ACP"),
            ) as enable:
                with self.assertRaises(ACPConnectionError):
                    run_configure_flow(
                        ConfigureFlowRequest(
                            existing={},
                            env_path=Path(tmp) / ".env",
                            host="root@10.0.1.1",
                            password="pw",
                            ssh_opts="-o foo",
                            configure_id="config-id",
                            persist_password=True,
                            selected_record=SELECTED_RECORD,
                            probe=mock.Mock(return_value=self.make_ssh_closed_probe_state()),
                        )
                    )

        self.assertIs(enable.call_args.kwargs["record"], SELECTED_RECORD)

    def test_enable_ssh_with_port_preflight_normalizes_blank_connect_errors(self) -> None:
        callbacks, _stages, _logs, debug_fields, _update_fields = self.callbacks()
        tcp_connect_error = mock.Mock(return_value="")
        sleep = mock.Mock()
        with mock.patch("timecapsulesmb.services.acp_ssh.set_dbug") as enable_ssh:
            with self.assertRaises(ACPConnectionError):
                enable_ssh_with_port_preflight(
                    "10.0.0.2",
                    "pw",
                    callbacks=callbacks,
                    tcp_connect_error_func=tcp_connect_error,
                    sleep_func=sleep,
                )

        enable_ssh.assert_not_called()
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(
            debug_fields[-1],
            {
                "acp_port_probe_succeeded": False,
                "acp_port_probe_attempts": 3,
                "acp_port_probe_errors": [
                    {"attempt": 1, "error": "connection failed", "kind": "other"},
                    {"attempt": 2, "error": "connection failed", "kind": "other"},
                    {"attempt": 3, "error": "connection failed", "kind": "other"},
                ],
                "acp_port_probe_last_error": "connection failed",
            },
        )

    def test_enable_ssh_and_reprobe_records_auth_failure_and_propagates(self) -> None:
        callbacks, _stages, _logs, debug_fields, update_fields = self.callbacks()
        with mock.patch("timecapsulesmb.services.acp_ssh.tcp_connect_error", return_value=None):
            with mock.patch("timecapsulesmb.services.acp_ssh.set_dbug", side_effect=ACPAuthError("bad password")) as enable_ssh:
                with mock.patch("timecapsulesmb.services.configure.reboot_device", return_value=None) as wait:
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
        self.assertEqual(update_fields, [PROBE_SUCCEEDED, PROBE_CONTEXT])

    def test_enable_ssh_and_reprobe_records_generic_acp_failure_and_propagates(self) -> None:
        callbacks, _stages, _logs, debug_fields, update_fields = self.callbacks()
        with mock.patch("timecapsulesmb.services.acp_ssh.tcp_connect_error", return_value=None):
            with mock.patch("timecapsulesmb.services.acp_ssh.set_dbug", side_effect=ACPConnectionError("connection failed")) as enable_ssh:
                with mock.patch("timecapsulesmb.services.configure.reboot_device", return_value=None) as wait:
                    with mock.patch("timecapsulesmb.services.configure.probe_connection_state") as probe:
                        with self.assertRaises(ACPConnectionError):
                            enable_ssh_and_reprobe(self.make_connection(), callbacks=callbacks)

        enable_ssh.assert_called_once()
        wait.assert_not_called()
        probe.assert_not_called()
        self.assertEqual(debug_fields[-1], {"configure_acp_enable_succeeded": False})
        self.assertEqual(update_fields, [PROBE_SUCCEEDED, PROBE_CONTEXT])

    def test_enable_ssh_and_reprobe_probes_where_the_device_came_back(self) -> None:
        probe_state = self.make_probe_state()
        with mock.patch("timecapsulesmb.services.acp_ssh.tcp_connect_error", return_value=None), \
                mock.patch("timecapsulesmb.services.acp_ssh.set_dbug"), \
                mock.patch("timecapsulesmb.services.configure.reboot_device", return_value="root@10.0.0.9"), \
                mock.patch("timecapsulesmb.services.configure.probe_connection_state", return_value=probe_state) as probe:
            connection, state = enable_ssh_and_reprobe(self.make_connection(), callbacks=self.callbacks()[0])

        self.assertEqual(connection.host, "root@10.0.0.9")
        self.assertIs(state, probe_state)
        probe.assert_called_once_with(connection)

    def test_enable_ssh_and_reprobe_returns_none_when_ssh_does_not_open(self) -> None:
        callbacks, stages, _logs, _debug_fields, update_fields = self.callbacks()
        with mock.patch("timecapsulesmb.services.acp_ssh.tcp_connect_error", return_value=None):
            with mock.patch("timecapsulesmb.services.acp_ssh.set_dbug"):
                with mock.patch(
                    "timecapsulesmb.services.configure.reboot_device",
                    side_effect=RebootFlowError(SSH_ENABLE_TIMEOUT_MESSAGE, "reboot_not_finished"),
                ):
                    with mock.patch("timecapsulesmb.services.configure.probe_connection_state") as probe:
                        result = enable_ssh_and_reprobe(self.make_connection(), callbacks=callbacks)

        self.assertEqual(result, (self.make_connection(), None))
        probe.assert_not_called()
        self.assertEqual(stages, ["acp_port_probe", "acp_enable_ssh"])
        self.assertEqual(update_fields, [PROBE_SUCCEEDED, PROBE_CONTEXT, {"ssh_final_reachable": False}])

    def test_device_that_did_not_restart_fails_configure_with_its_own_code(self) -> None:
        # Only SSH not opening in time is the soft "try again" outcome; a reboot
        # that never started is a different failure.
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            with mock.patch("timecapsulesmb.services.acp_ssh.tcp_connect_error", return_value=None):
                with mock.patch("timecapsulesmb.services.acp_ssh.set_dbug"):
                    with mock.patch(
                        "timecapsulesmb.services.configure.reboot_device",
                        side_effect=RebootFlowError("Reboot was requested but the device did not restart.", "reboot_not_started"),
                    ):
                        with self.assertRaises(ConfigureFlowError) as raised:
                            run_configure_flow(self.configure_request(env_path, mock.Mock(return_value=self.make_ssh_closed_probe_state())))
            self.assertFalse(env_path.exists())

        self.assertEqual(raised.exception.code, "reboot_not_started")
        self.assertEqual(str(raised.exception), "Reboot was requested but the device did not restart.")

    def test_ssh_that_never_opens_fails_configure_as_an_enable_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            with mock.patch("timecapsulesmb.services.acp_ssh.tcp_connect_error", return_value=None):
                with mock.patch("timecapsulesmb.services.acp_ssh.set_dbug"):
                    with mock.patch(
                        "timecapsulesmb.services.configure.reboot_device",
                        side_effect=RebootFlowError(SSH_ENABLE_TIMEOUT_MESSAGE, "reboot_not_finished"),
                    ):
                        with self.assertRaises(ConfigureFlowError) as raised:
                            run_configure_flow(self.configure_request(env_path, mock.Mock(return_value=self.make_ssh_closed_probe_state())))

        self.assertEqual(raised.exception.code, "ssh_enable_timeout")
        self.assertEqual(str(raised.exception), SSH_ENABLE_TIMEOUT_MESSAGE)

    def test_confirmed_mac_is_returned_and_mismatched_bonjour_never_writes_config(self) -> None:
        from dataclasses import replace
        base = self.make_probe_state()
        state = replace(base, probe_result=replace(base.probe_result, airport_mac="02:aa:bb:cc:dd:ee"))
        for advertised in (None, "02-AA-BB-CC-DD-EE", "02:aa:bb:cc:dd:ff"):
            with self.subTest(advertised=advertised), tempfile.TemporaryDirectory() as tmp:
                env_path = Path(tmp) / ".env"
                env_path.write_text("original configuration")
                writer = mock.Mock()
                record = BonjourResolvedService("Office", "office.local", "_airport._tcp.local.",
                    properties={"waMA": advertised} if advertised else {})
                request = self.configure_request(env_path, mock.Mock(return_value=state),
                    selected_record=record, write_env=writer)
                if advertised == "02:aa:bb:cc:dd:ff":
                    with self.assertRaises(ConfigureFlowError) as raised:
                        run_configure_flow(request)
                    self.assertEqual(raised.exception.code, "device_identity_mismatch")
                    writer.assert_not_called()
                    self.assertEqual(env_path.read_text(), "original configuration")
                else:
                    result = run_configure_flow(request)
                    self.assertEqual(result.airport_mac, "02:aa:bb:cc:dd:ee")
                    writer.assert_called_once()

    def test_absent_confirmation_does_not_promote_bonjour_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            record = BonjourResolvedService("Office", "office.local", properties={"waMA": "02:aa:bb:cc:dd:ee"})
            request = self.configure_request(Path(tmp) / ".env", mock.Mock(return_value=self.make_probe_state()),
                selected_record=record, write_env=mock.Mock())
            self.assertIsNone(run_configure_flow(request).airport_mac)

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
        self.assertNotIn("TC_SMB_BIND_LAN_ONLY", written)
        self.assertEqual(written["TC_SMB_BROWSE_COMPATIBILITY"], "false")
        self.assertEqual(written["TC_MDNS_ADVERTISE_AFP"], "false")
        self.assertEqual(written["TC_REQUIRE_SMB_ENCRYPTION"], "false")
        self.assertEqual(written["TC_FORCE_DISABLE_SMB_SIGNING_AND_ENCRYPTION"], "false")
        self.assertEqual(written["TC_FRUIT_METADATA_NETATALK"], "true")
        self.assertEqual(written["TC_VFS_AIO_FORK_ENABLED"], "false")
        self.assertNotIn("TC_PASSWORD", written)
        self.assertEqual(stages, ["ssh_probe", "write_env"])
        self.assertIn({"ssh_final_reachable": True}, debug_fields)
        self.assertIn({"ssh_final_reachable": True}, update_fields)
        self.assertIn({"configure_id": "config-id", "device_syap": "119", "device_model": "TimeCapsule8,119"}, update_fields)

    def test_run_configure_flow_strips_removed_settings_with_one_notice_each(self) -> None:
        probe_state = self.make_probe_state()
        written: dict[str, str] = {}
        callbacks, _stages, logs, _debug_fields, _update_fields = self.callbacks()

        def write_env(_path: Path, values: Mapping[str, str]) -> None:
            written.update(values)

        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            run_configure_flow(
                ConfigureFlowRequest(
                    existing={
                        "TC_SMB_BIND_LAN_ONLY": "true",
                        "TC_MDNS_HOST_LABEL": "old-label",
                        "TC_MDNS_DEVICE_MODEL": "TimeCapsule6,113",
                        "TC_CUSTOM": "kept",
                    },
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
            )

        # Removed settings leave the file; unrelated custom values survive.
        self.assertNotIn("TC_SMB_BIND_LAN_ONLY", written)
        self.assertNotIn("TC_MDNS_HOST_LABEL", written)
        self.assertNotIn("TC_MDNS_DEVICE_MODEL", written)
        self.assertEqual(written["TC_CUSTOM"], "kept")
        notices = [line for line in logs if line.startswith("Removing ")]
        self.assertEqual(len(notices), 2, logs)
        self.assertTrue(any("TC_MDNS_HOST_LABEL" in line and "AirPort name" in line for line in notices))
        self.assertTrue(any("TC_MDNS_DEVICE_MODEL" in line and "observed from the device" in line for line in notices))

    def test_run_configure_flow_is_silent_when_no_removed_settings_exist(self) -> None:
        probe_state = self.make_probe_state()
        callbacks, _stages, logs, _debug_fields, _update_fields = self.callbacks()
        with tempfile.TemporaryDirectory() as tmp:
            run_configure_flow(
                ConfigureFlowRequest(
                    existing={"TC_CUSTOM": "kept"},
                    env_path=Path(tmp) / ".env",
                    host="root@10.0.0.2",
                    password="pw",
                    ssh_opts="-o foo",
                    configure_id="config-id",
                    persist_password=False,
                    probe=mock.Mock(return_value=probe_state),
                    write_env=lambda _path, _values: None,
                ),
                callbacks=callbacks,
            )
        self.assertFalse(any(line.startswith("Removing ") for line in logs), logs)

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
        self.assertNotIn("TC_MDNS_DEVICE_MODEL", written)

    def test_run_configure_flow_normalizes_acp_authentication_failure(self) -> None:
        probe_state = ProbedDeviceState(
            probe_result=ProbeResult(
                ssh_status=SshAccessStatus.CLOSED,
                error="SSH is not reachable yet.",
                os_name="",
                os_release="",
                arch="",
                elf_endianness="unknown",
            ),
            compatibility=None,
        )
        acp_error = ACPAuthError("ACP command failed with error_code -0x10")

        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            with mock.patch(
                "timecapsulesmb.services.configure.enable_ssh_and_reprobe",
                side_effect=acp_error,
            ):
                with self.assertRaises(ConfigureFlowError) as raised:
                    run_configure_flow(
                        ConfigureFlowRequest(
                            existing={},
                            env_path=env_path,
                            host="root@10.0.0.2",
                            password="badpw",
                            ssh_opts="-o foo",
                            configure_id="config-id",
                            persist_password=True,
                            probe=mock.Mock(return_value=probe_state),
                        )
                    )

        self.assertEqual(raised.exception.code, "auth_failed")
        self.assertEqual(str(raised.exception), AIRPORT_ADMIN_PASSWORD_REJECTED_MESSAGE)
        self.assertEqual(raised.exception.debug, str(acp_error))
        self.assertIs(raised.exception.__cause__, acp_error)
        self.assertFalse(env_path.exists())

    def test_password_ssh_accepts_but_the_device_rejects_is_not_saved(self) -> None:
        # SSH compares only 8 characters, so "pw-secretzz" logs in where the
        # admin password is "pw-secret"; every later ACP reboot would refuse it.
        callbacks, _stages, _logs, debug_fields, _updates = self.callbacks()
        compare = acp_password_answer(False)
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            with mock.patch("timecapsulesmb.device.probe.read_airport_acp", compare):
                with self.assertRaises(ConfigureFlowError) as raised:
                    run_configure_flow(
                        self.configure_request(env_path, mock.Mock(return_value=self.make_probe_state()),
                                               password="pw-secretzz"),
                        callbacks=callbacks,
                    )
            self.assertFalse(env_path.exists())

        self.assertEqual(raised.exception.code, "auth_failed")
        self.assertEqual(str(raised.exception), AIRPORT_ADMIN_PASSWORD_REJECTED_MESSAGE)
        self.assertEqual(raised.exception.debug, SSH_ONLY_PASSWORD_DEBUG)
        self.assertIn({"sypw_check": "mismatch"}, debug_fields)
        self.assertEqual(compare.call_args.args[1], "pw-secretzz")

    def test_device_rejected_password_can_still_be_saved_when_the_user_asks(self) -> None:
        written: dict[str, str] = {}
        seen: list[ProbedDeviceState] = []
        probe_state = self.make_probe_state()
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("timecapsulesmb.device.probe.read_airport_acp", acp_password_answer(False)):
                run_configure_flow(
                    self.configure_request(
                        Path(tmp) / ".env", mock.Mock(return_value=probe_state),
                        password="pw-secretzz", write_env=lambda _path, values: written.update(values),
                    ),
                    hooks=ConfigureFlowHooks(save_without_authentication=lambda state: seen.append(state) or True),
                )
        self.assertEqual(seen, [probe_state])
        self.assertEqual(written["TC_PASSWORD"], "pw-secretzz")

    def test_password_is_saved_when_the_device_matches_or_cannot_tell(self) -> None:
        for matches, result in ((True, "match"), (None, "unknown")):
            with self.subTest(result=result):
                callbacks, _stages, _logs, debug_fields, _updates = self.callbacks()
                writer = mock.Mock()
                with tempfile.TemporaryDirectory() as tmp:
                    with mock.patch("timecapsulesmb.device.probe.read_airport_acp", acp_password_answer(matches)):
                        run_configure_flow(
                            self.configure_request(Path(tmp) / ".env", mock.Mock(return_value=self.make_probe_state()),
                                                   write_env=writer),
                            callbacks=callbacks,
                        )
                writer.assert_called_once()
                self.assertIn({"sypw_check": result}, debug_fields)
                self.assertIn({"ssh_final_reachable": True}, debug_fields)

    def test_password_is_compared_once_after_acp_turns_ssh_on(self) -> None:
        closed = ProbedDeviceState(
            probe_result=ProbeResult(
                ssh_status=SshAccessStatus.CLOSED, error="SSH is not reachable yet.",
                os_name="", os_release="", arch="", elf_endianness="unknown",
            ),
            compatibility=None,
        )
        compare = acp_password_answer(True)
        writer = mock.Mock()
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("timecapsulesmb.services.configure.enable_ssh_and_reprobe",
                            side_effect=lambda connection, **_kwargs: (connection, self.make_probe_state())):
                with mock.patch("timecapsulesmb.device.probe.read_airport_acp", compare):
                    run_configure_flow(self.configure_request(Path(tmp) / ".env", mock.Mock(return_value=closed),
                                                              write_env=writer))
        compare.assert_called_once()
        writer.assert_called_once()

    def closed_ssh_state(self) -> ProbedDeviceState:
        return ProbedDeviceState(
            probe_result=ProbeResult(
                ssh_status=SshAccessStatus.CLOSED, error="SSH is not reachable yet.",
                os_name="", os_release="", arch="", elf_endianness="unknown",
            ),
            compatibility=None,
        )

    def test_configure_saves_the_address_ssh_was_turned_on_at(self) -> None:
        # The device answered at a new address (found by its MAC); the reboot,
        # the probe after it and the saved TC_HOST all use that address.
        probed_hosts: list[str] = []

        def probe(connection: SshConnection) -> ProbedDeviceState:
            probed_hosts.append(connection.host)
            return self.closed_ssh_state() if len(probed_hosts) == 1 else self.make_probe_state()

        writer = mock.Mock()
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("timecapsulesmb.services.configure.enable_ssh_with_port_preflight", return_value="192.168.1.40"), \
                    mock.patch("timecapsulesmb.services.configure.reboot_device", return_value=None) as reboot:
                result = run_configure_flow(self.configure_request(Path(tmp) / ".env", mock.Mock(side_effect=probe), write_env=writer))

        self.assertEqual(probed_hosts, ["root@10.0.0.2", "root@192.168.1.40"])
        self.assertEqual(reboot.call_args.args[0], "192.168.1.40")
        self.assertEqual(result.host, "root@192.168.1.40")
        self.assertEqual(result.connection.host, "root@192.168.1.40")
        self.assertEqual(writer.call_args.args[1]["TC_HOST"], "root@192.168.1.40")

    def test_another_airport_at_the_address_fails_configure_as_an_identity_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            with mock.patch(
                "timecapsulesmb.services.configure.enable_ssh_with_port_preflight",
                side_effect=AirportIdentityMismatchError("A different AirPort answers at 10.0.0.2."),
            ):
                with self.assertRaises(ConfigureFlowError) as raised:
                    run_configure_flow(self.configure_request(env_path, mock.Mock(return_value=self.closed_ssh_state())))
            self.assertFalse(env_path.exists())

        self.assertEqual(raised.exception.code, "device_identity_mismatch")
        self.assertEqual(str(raised.exception), "A different AirPort answers at 10.0.0.2.")

    def test_password_ssh_rejects_is_not_compared_with_the_device(self) -> None:
        compare = acp_password_answer(True)
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("timecapsulesmb.device.probe.read_airport_acp", compare):
                with self.assertRaises(ConfigureFlowError):
                    run_configure_flow(self.configure_request(
                        Path(tmp) / ".env", mock.Mock(return_value=self.make_auth_failed_probe_state())))
        compare.assert_not_called()

    def test_run_configure_flow_normalizes_ssh_authentication_failure(self) -> None:
        probe_state = self.make_auth_failed_probe_state()

        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            with self.assertRaises(ConfigureFlowError) as raised:
                run_configure_flow(
                    ConfigureFlowRequest(
                        existing={},
                        env_path=env_path,
                        host="root@10.0.0.2",
                        password="badpw",
                        ssh_opts="-o foo",
                        configure_id="config-id",
                        persist_password=True,
                        probe=mock.Mock(return_value=probe_state),
                    )
                )

        self.assertEqual(raised.exception.code, "auth_failed")
        self.assertEqual(str(raised.exception), AIRPORT_ADMIN_PASSWORD_REJECTED_MESSAGE)
        self.assertEqual(raised.exception.debug, "SSH authentication failed.")
        self.assertFalse(env_path.exists())

    def test_run_configure_flow_reports_ssh_algorithm_failure_without_auth_error(self) -> None:
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
                        probe=mock.Mock(return_value=self.make_ssh_algorithm_failed_probe_state()),
                    )
                )

        self.assertEqual(raised.exception.code, "ssh_compatibility_failed")
        self.assertIn("no matching MAC found", str(raised.exception))

    def test_run_configure_flow_reports_a_connection_this_mac_dropped_with_its_own_code(self) -> None:
        message = "This Mac dropped the connection before it reached the device. (ssh: ...)"
        probe_state = ProbedDeviceState(
            probe_result=ProbeResult(
                ssh_status=SshAccessStatus.LOCAL_NETWORK_FILTERED,
                error=message,
                os_name="",
                os_release="",
                arch="",
                elf_endianness="unknown",
            ),
            compatibility=None,
        )
        updates: list[dict[str, object]] = []
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            with self.assertRaises(ConfigureFlowError) as raised:
                run_configure_flow(
                    ConfigureFlowRequest(
                        existing={},
                        env_path=env_path,
                        host="root@10.0.0.2",
                        password="pw",
                        ssh_opts="-o foo",
                        configure_id="config-id",
                        persist_password=True,
                        probe=mock.Mock(return_value=probe_state),
                    ),
                    callbacks=OperationCallbacks(update_fields=lambda **fields: updates.append(fields)),
                )
            self.assertFalse(env_path.exists())

        self.assertEqual(raised.exception.code, "local_network_filtered")
        self.assertEqual(str(raised.exception), message)
        self.assertIn({"ssh_final_reachable": True}, updates)

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

    def test_run_configure_flow_rejects_unsupported_selected_record_syap_before_ssh_or_acp(self) -> None:
        probe = mock.Mock(return_value=self.make_probe_state())
        callbacks, stages, _logs, debug_fields, _update_fields = self.callbacks()
        before_enable_ssh = mock.Mock()
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            with mock.patch("timecapsulesmb.services.configure.enable_ssh_with_port_preflight", side_effect=lambda host, *_args, **_kwargs: host) as enable_ssh:
                with self.assertRaises(ConfigureFlowError) as raised:
                    run_configure_flow(
                        self.configure_request(env_path, probe, selected_record_airport_syap="115"),
                        callbacks=callbacks,
                        hooks=ConfigureFlowHooks(before_enable_ssh=before_enable_ssh),
                    )
            self.assertFalse(env_path.exists())

        self.assertEqual(raised.exception.code, "unsupported_device")
        self.assertIn("syAP 115", str(raised.exception))
        self.assertIn("AirPort Express is not supported", str(raised.exception))
        probe.assert_not_called()
        enable_ssh.assert_not_called()
        before_enable_ssh.assert_not_called()
        self.assertEqual(stages, ["check_device_model"])
        self.assertIn(
            {"configure_failure_reason": "unsupported_device", "discovered_airport_syap": "115"},
            debug_fields,
        )

    def test_run_configure_flow_continues_for_supported_or_unusable_selected_record_syap(self) -> None:
        for syap in ("119", None, "", "bad"):
            with self.subTest(syap=syap):
                probe = mock.Mock(return_value=self.make_probe_state())
                with tempfile.TemporaryDirectory() as tmp:
                    result = run_configure_flow(
                        self.configure_request(
                            Path(tmp) / ".env",
                            probe,
                            selected_record_airport_syap=syap,
                            write_env=lambda _path, _values: None,
                        )
                    )
                probe.assert_called_once()
                self.assertEqual(result.identity.syap, "119")

    def test_run_configure_flow_ignores_unsupported_syap_from_record_the_host_did_not_come_from(self) -> None:
        # The record's syAP is still the identity fallback, but a typed host may be
        # another device, so only selected_record_airport_syap may reject it.
        probe = mock.Mock(return_value=self.make_probe_state())
        with tempfile.TemporaryDirectory() as tmp:
            result = run_configure_flow(
                self.configure_request(
                    Path(tmp) / ".env",
                    probe,
                    discovered_airport_syap="115",
                    selected_record_airport_syap=None,
                    write_env=lambda _path, _values: None,
                )
            )
        probe.assert_called_once()
        self.assertEqual(result.identity.syap, "119")

    def test_run_configure_flow_rejects_airport_express_found_by_ssh_probe(self) -> None:
        written: list[Mapping[str, str]] = []
        callbacks, stages, _logs, debug_fields, _update_fields = self.callbacks()
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            with self.assertRaises(ConfigureFlowError) as raised:
                run_configure_flow(
                    self.configure_request(
                        env_path,
                        mock.Mock(return_value=self.make_airport_express_probe_state()),
                        write_env=lambda _path, values: written.append(values),
                    ),
                    callbacks=callbacks,
                )
            self.assertFalse(env_path.exists())

        self.assertEqual(raised.exception.code, "unsupported_device")
        self.assertIn("ar7240 processor", str(raised.exception))
        self.assertEqual(written, [])
        self.assertEqual(stages, ["ssh_probe"])
        self.assertIn({"configure_failure_reason": "unsupported_device"}, debug_fields)

    def test_run_configure_flow_rejects_airport_express_after_enabling_ssh_without_syap(self) -> None:
        # No Bonjour syAP (a typed IP): ACP still enables SSH, then the probe stops it.
        probe = mock.Mock(side_effect=[self.make_ssh_closed_probe_state(), self.make_airport_express_probe_state()])
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            with mock.patch("timecapsulesmb.services.configure.enable_ssh_with_port_preflight", side_effect=lambda host, *_args, **_kwargs: host) as enable_ssh:
                with mock.patch("timecapsulesmb.services.configure.reboot_device", return_value=None):
                    with self.assertRaises(ConfigureFlowError) as raised:
                        run_configure_flow(self.configure_request(env_path, probe))
            self.assertFalse(env_path.exists())

        self.assertEqual(raised.exception.code, "unsupported_device")
        self.assertIn("ar7240", str(raised.exception))
        enable_ssh.assert_called_once()
        self.assertEqual(probe.call_count, 2)


if __name__ == "__main__":
    unittest.main()
