from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from timecapsulesmb.app.events import AppEvent, EventSink
from timecapsulesmb import repair_xattrs as repair_xattrs_domain
from timecapsulesmb.app import contracts, helper, operations, service
from timecapsulesmb.cli import main as cli_main
from timecapsulesmb.checks.models import CheckResult
from timecapsulesmb.core.config import AppConfig, ConfigError, parse_env_file
from timecapsulesmb.device.compat import DeviceCompatibility
from timecapsulesmb.device.probe import ProbeResult, ProbedDeviceState
from timecapsulesmb.discovery.bonjour import BonjourDiscoverySnapshot, BonjourResolvedService, BonjourServiceInstance
from timecapsulesmb.integrations.acp import ACPAuthError
from timecapsulesmb.transport.errors import TransportError
from timecapsulesmb.transport.ssh import SshConnection


class CollectingSink:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []
        self.sink = EventSink(lambda event: self.events.append(event.to_jsonable()))

    def events_of_type(self, event_type: str) -> list[dict[str, object]]:
        return [event for event in self.events if event["type"] == event_type]


def supported_compatibility(payload_family: str = "netbsd6_samba4") -> DeviceCompatibility:
    return DeviceCompatibility(
        os_name="NetBSD",
        os_release="6.0",
        arch="earmv4",
        elf_endianness="little",
        payload_family=payload_family,
        device_generation="gen5",
        supported=True,
        reason_code="supported_netbsd6",
        syap_candidates=("119",),
        model_candidates=("TimeCapsule8,119",),
    )


def unsupported_compatibility() -> DeviceCompatibility:
    return DeviceCompatibility(
        os_name="NetBSD",
        os_release="3.0",
        arch="i386",
        elf_endianness="little",
        payload_family=None,
        device_generation=None,
        supported=False,
        reason_code="unsupported_os",
        syap_candidates=(),
        model_candidates=(),
    )


def probed_state() -> ProbedDeviceState:
    return ProbedDeviceState(
        probe_result=ProbeResult(
            ssh_port_reachable=True,
            ssh_authenticated=True,
            error=None,
            os_name="NetBSD",
            os_release="6.0",
            arch="earmv4",
            elf_endianness="little",
            airport_model="TimeCapsule8,119",
            airport_syap="119",
        ),
        compatibility=supported_compatibility(),
    )


def netbsd4_probed_state() -> ProbedDeviceState:
    return ProbedDeviceState(
        probe_result=ProbeResult(
            ssh_port_reachable=True,
            ssh_authenticated=True,
            error=None,
            os_name="NetBSD",
            os_release="4.0",
            arch="powerpc",
            elf_endianness="big",
            airport_model="TimeCapsule6,116",
            airport_syap="116",
        ),
        compatibility=supported_compatibility("netbsd4be_samba4"),
    )


def unreachable_probed_state() -> ProbedDeviceState:
    return ProbedDeviceState(
        probe_result=ProbeResult(
            ssh_port_reachable=False,
            ssh_authenticated=False,
            error="connection refused",
            os_name="",
            os_release="",
            arch="",
            elf_endianness="",
        ),
        compatibility=None,
    )


class AppApiTests(unittest.TestCase):
    def assert_single_terminal_event(self, collector: CollectingSink, event_type: str) -> dict[str, object]:
        terminals = collector.events_of_type("result") + collector.events_of_type("error")
        self.assertEqual([event["type"] for event in terminals], [event_type])
        return terminals[0]

    def test_event_redacts_password_fields(self) -> None:
        event = AppEvent("result", "configure", {
            "ok": True,
            "payload": {
                "password": "secret",
                "nested": {"TC_PASSWORD": "secret"},
            },
        })

        data = event.to_jsonable()

        self.assertEqual(data["payload"]["password"], "<redacted>")
        self.assertEqual(data["payload"]["nested"]["TC_PASSWORD"], "<redacted>")

    def test_result_event_preserves_falsey_payloads(self) -> None:
        collector = CollectingSink()

        collector.sink.result("paths", ok=True, payload=[])

        result = collector.events_of_type("result")[0]
        self.assertEqual(result["payload"], [])
        self.assertEqual(result["schema_version"], 1)
        self.assertTrue(result["request_id"])

    def test_stage_events_include_policy_metadata(self) -> None:
        collector = CollectingSink()

        collector.sink.stage("paths", "resolve_paths")
        collector.sink.stage("deploy", "upload_payload")
        collector.sink.stage("uninstall", "uninstall_payload")
        collector.sink.stage("deploy", "reboot")

        stages = collector.events_of_type("stage")
        self.assertEqual(stages[0]["risk"], "local_read")
        self.assertTrue(stages[0]["cancellable"])
        self.assertEqual(stages[1]["risk"], "remote_write")
        self.assertEqual(stages[2]["risk"], "destructive")
        self.assertEqual(stages[3]["risk"], "reboot")
        self.assertIn("description", stages[3])

    def test_contract_builders_keep_stable_representative_shapes(self) -> None:
        deploy_plan = contracts.deploy_plan_payload(
            {"host": "root@10.0.0.2", "reboot_required": True},
            payload_family="netbsd6_samba4",
            netbsd4=False,
        )
        self.assertEqual(deploy_plan, {
            "host": "root@10.0.0.2",
            "reboot_required": True,
            "requires_reboot": True,
            "payload_family": "netbsd6_samba4",
            "netbsd4": False,
            "summary": "deployment dry-run plan generated.",
            "schema_version": 1,
        })

        doctor = contracts.doctor_payload(
            fatal=True,
            results=[
                CheckResult("PASS", "ok"),
                CheckResult("WARN", "slow"),
                CheckResult("FAIL", "bad"),
            ],
            error="Doctor failures:\nFAIL bad",
        )
        self.assertEqual(doctor["counts"], {"PASS": 1, "WARN": 1, "FAIL": 1, "INFO": 0})
        self.assertEqual(doctor["summary"], "doctor found one or more fatal problems.")
        self.assertEqual(doctor["schema_version"], 1)

        repair = contracts.repair_xattrs_payload({
            "returncode": 0,
            "root": "/Volumes/Data",
            "finding_count": 2,
            "repairable_count": 1,
            "summary": {"scanned": 3},
        })
        self.assertEqual(repair["summary"], {"scanned": 3})
        self.assertEqual(repair["summary_text"], "repair-xattrs found 2 issue(s), 1 repairable.")

    def test_request_id_propagates_to_every_event(self) -> None:
        collector = CollectingSink()

        rc = service.run_api_request({"request_id": "req-123", "operation": "paths", "params": {}}, collector.sink)

        self.assertEqual(rc, 0)
        self.assertTrue(collector.events)
        self.assertEqual({event["request_id"] for event in collector.events}, {"req-123"})
        self.assert_single_terminal_event(collector, "result")

    def test_missing_params_defaults_to_empty_object(self) -> None:
        collector = CollectingSink()

        rc = service.run_api_request({"operation": "paths"}, collector.sink)

        self.assertEqual(rc, 0)
        result = self.assert_single_terminal_event(collector, "result")
        self.assertEqual(result["operation"], "paths")

    def test_missing_operation_emits_invalid_request_error(self) -> None:
        collector = CollectingSink()

        rc = service.run_api_request({"params": {}}, collector.sink)

        self.assertEqual(rc, 1)
        error = self.assert_single_terminal_event(collector, "error")
        self.assertEqual(error["operation"], "api")
        self.assertEqual(error["code"], "invalid_request")
        self.assertEqual(error["recovery"]["title"], "Invalid request")
        self.assertTrue(error["recovery"]["retryable"])

    def test_unknown_operation_emits_error_without_result(self) -> None:
        collector = CollectingSink()

        rc = service.run_api_request({"operation": "nope", "params": {}}, collector.sink)

        self.assertEqual(rc, 1)
        error = self.assert_single_terminal_event(collector, "error")
        self.assertEqual(error["code"], "unknown_operation")
        self.assertEqual(error["recovery"]["title"], "Unknown operation")

    def test_non_object_params_emits_invalid_request_error(self) -> None:
        collector = CollectingSink()

        rc = service.run_api_request({"operation": "paths", "params": []}, collector.sink)

        self.assertEqual(rc, 1)
        error = self.assert_single_terminal_event(collector, "error")
        self.assertEqual(error["code"], "invalid_request")

    def test_dispatcher_maps_recoverable_and_unexpected_error_states(self) -> None:
        cases = (
            ("config-error", ConfigError("bad config"), "config_error"),
            ("transport-error", TransportError("remote failed"), "remote_error"),
            ("unexpected-error", RuntimeError("boom"), "operation_failed"),
        )
        for operation, exception, code in cases:
            with self.subTest(code=code):
                collector = CollectingSink()

                def fail(_params, _sink, exc=exception):
                    raise exc

                with mock.patch.dict(service.OPERATIONS, {operation: fail}):
                    rc = service.run_api_request({"operation": operation, "params": {}}, collector.sink)

                self.assertEqual(rc, 1)
                error = self.assert_single_terminal_event(collector, "error")
                self.assertEqual(error["code"], code)
                self.assertIn("recovery", error)

    def test_dispatcher_includes_traceback_for_unexpected_errors(self) -> None:
        collector = CollectingSink()

        def fail(_params, _sink):
            raise RuntimeError("boom")

        with mock.patch.dict(service.OPERATIONS, {"boom": fail}):
            rc = service.run_api_request({"operation": "boom", "params": {}}, collector.sink)

        self.assertEqual(rc, 1)
        error = self.assert_single_terminal_event(collector, "error")
        self.assertEqual(error["code"], "operation_failed")
        self.assertIn("Traceback", error["debug"]["traceback"])
        self.assertIn("RuntimeError: boom", error["debug"]["traceback"])

    def test_discover_operation_returns_snapshot_payload(self) -> None:
        collector = CollectingSink()
        snapshot = BonjourDiscoverySnapshot(
            instances=[BonjourServiceInstance("_airport._tcp.local.", "TC", "TC._airport._tcp.local.")],
            resolved=[
                BonjourResolvedService(
                    name="TC",
                    hostname="tc.local.",
                    service_type="_airport._tcp.local.",
                    port=5009,
                    ipv4=("10.0.0.2",),
                    properties={"syAP": "119"},
                )
            ],
        )

        with mock.patch("timecapsulesmb.app.operations.discover_snapshot", return_value=snapshot):
            rc = service.run_api_request({"operation": "discover", "params": {"timeout": 0.1}}, collector.sink)

        self.assertEqual(rc, 0)
        result = collector.events_of_type("result")[0]
        self.assertEqual(result["payload"]["resolved"][0]["name"], "TC")
        self.assertEqual(result["payload"]["resolved"][0]["ipv4"], ["10.0.0.2"])
        self.assertEqual(result["payload"]["schema_version"], 1)
        self.assertEqual(result["payload"]["counts"], {"instances": 1, "resolved": 1})
        self.assertEqual(result["payload"]["summary"], "discovered 1 resolved AirPort service(s).")

    def test_discover_rejects_invalid_timeout_values(self) -> None:
        for timeout in ("bad", "nan", -1, True):
            with self.subTest(timeout=timeout):
                collector = CollectingSink()
                with mock.patch("timecapsulesmb.app.operations.discover_snapshot") as discover:
                    rc = service.run_api_request(
                        {"operation": "discover", "params": {"timeout": timeout}},
                        collector.sink,
                    )

                self.assertEqual(rc, 1)
                error = self.assert_single_terminal_event(collector, "error")
                self.assertEqual(error["code"], "validation_failed")
                self.assertEqual(error["recovery"]["title"], "Request validation failed")
                discover.assert_not_called()

    def test_discover_accepts_numeric_timeout_string(self) -> None:
        collector = CollectingSink()
        snapshot = BonjourDiscoverySnapshot(instances=[], resolved=[])

        with mock.patch("timecapsulesmb.app.operations.discover_snapshot", return_value=snapshot) as discover:
            rc = service.run_api_request(
                {"operation": "discover", "params": {"timeout": "0.25"}},
                collector.sink,
            )

        self.assertEqual(rc, 0)
        discover.assert_called_once_with(timeout=0.25)

    def test_configure_writes_env_without_leaking_password_to_events(self) -> None:
        collector = CollectingSink()
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / ".env"
            with mock.patch("timecapsulesmb.app.operations.probe_connection_state", return_value=probed_state()):
                rc = service.run_api_request(
                    {
                        "operation": "configure",
                        "params": {
                            "config": str(config_path),
                            "host": "root@10.0.0.2",
                            "password": "goodpw",
                        },
                    },
                    collector.sink,
                )

            self.assertEqual(rc, 0)
            self.assertIn("TC_HOST=root@10.0.0.2", config_path.read_text())
            self.assertIn("TC_PASSWORD=goodpw", config_path.read_text())
            serialized_events = json.dumps(collector.events)
            self.assertNotIn("goodpw", serialized_events)

    def test_configure_preserves_custom_env_keys_and_drops_deprecated_runtime_keys(self) -> None:
        collector = CollectingSink()
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / ".env"
            config_path.write_text(
                "TC_HOST=root@10.0.0.1\n"
                "TC_PASSWORD=oldpw\n"
                "TC_CUSTOM_SETTING='keep me'\n"
                "TC_SAMBA_USER=old-admin\n"
                "TC_PAYLOAD_DIR_NAME=old-payload\n"
            )
            with mock.patch("timecapsulesmb.app.operations.probe_connection_state", return_value=probed_state()):
                rc = service.run_api_request(
                    {
                        "operation": "configure",
                        "params": {
                            "config": str(config_path),
                            "host": "root@10.0.0.2",
                            "password": "newpw",
                        },
                    },
                    collector.sink,
                )

            values = parse_env_file(config_path)

        self.assertEqual(rc, 0)
        self.assertEqual(values["TC_HOST"], "root@10.0.0.2")
        self.assertEqual(values["TC_PASSWORD"], "newpw")
        self.assertEqual(values["TC_CUSTOM_SETTING"], "keep me")
        self.assertNotIn("TC_SAMBA_USER", values)
        self.assertNotIn("TC_PAYLOAD_DIR_NAME", values)

    def test_configure_reports_acp_auth_failure_without_writing_env(self) -> None:
        collector = CollectingSink()
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / ".env"
            with mock.patch("timecapsulesmb.app.operations.probe_connection_state", return_value=unreachable_probed_state()):
                with mock.patch("timecapsulesmb.app.operations.enable_ssh", side_effect=ACPAuthError("bad password")):
                    rc = service.run_api_request(
                        {
                            "operation": "configure",
                            "params": {
                                "config": str(config_path),
                                "host": "root@10.0.0.2",
                                "password": "badpw",
                            },
                        },
                        collector.sink,
                    )

        self.assertEqual(rc, 1)
        self.assertFalse(config_path.exists())
        self.assertEqual(collector.events_of_type("error")[0]["code"], "auth_failed")
        self.assertEqual(collector.events_of_type("error")[0]["recovery"]["suggested_operation"], "configure")
        self.assertNotIn("badpw", json.dumps(collector.events))

    def test_configure_reports_unsupported_device(self) -> None:
        collector = CollectingSink()
        unsupported_state = ProbedDeviceState(
            probe_result=probed_state().probe_result,
            compatibility=unsupported_compatibility(),
        )
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / ".env"
            with mock.patch("timecapsulesmb.app.operations.probe_connection_state", return_value=unsupported_state):
                rc = service.run_api_request(
                    {
                        "operation": "configure",
                        "params": {
                            "config": str(config_path),
                            "host": "root@10.0.0.2",
                            "password": "pw",
                        },
                    },
                    collector.sink,
                )

        self.assertEqual(rc, 1)
        self.assertFalse(config_path.exists())
        self.assertEqual(collector.events_of_type("error")[0]["code"], "unsupported_device")

    def test_doctor_streams_check_events(self) -> None:
        collector = CollectingSink()
        config = AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"})

        def fake_run_doctor_checks(*_args, **kwargs):
            kwargs["on_result"](CheckResult("PASS", "smbd is bound to TCP 445", {"port": 445}))
            return [CheckResult("PASS", "smbd is bound to TCP 445", {"port": 445})], False

        with mock.patch("timecapsulesmb.app.operations.load_env_config", return_value=config):
            with mock.patch("timecapsulesmb.app.operations.resolve_app_paths", return_value=SimpleNamespace(distribution_root=REPO_ROOT)):
                with mock.patch("timecapsulesmb.app.operations.resolve_env_connection", return_value=SshConnection("root@10.0.0.2", "pw", "-o foo")):
                    with mock.patch("timecapsulesmb.app.operations.run_doctor_checks", side_effect=fake_run_doctor_checks):
                        rc = service.run_api_request({"operation": "doctor", "params": {}}, collector.sink)

        self.assertEqual(rc, 0)
        checks = collector.events_of_type("check")
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0]["status"], "PASS")
        self.assertEqual(checks[0]["details"], {"port": 445})

    def test_doctor_fatal_returns_nonzero_result_without_error_event(self) -> None:
        collector = CollectingSink()
        config = AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"})

        def fake_run_doctor_checks(*_args, **kwargs):
            kwargs["on_result"](CheckResult("FAIL", "SMB is not reachable", {"password": "pw"}))
            return [CheckResult("FAIL", "SMB is not reachable", {"password": "pw"})], True

        with mock.patch("timecapsulesmb.app.operations.load_env_config", return_value=config):
            with mock.patch("timecapsulesmb.app.operations.resolve_app_paths", return_value=SimpleNamespace(distribution_root=REPO_ROOT)):
                with mock.patch("timecapsulesmb.app.operations.resolve_env_connection", return_value=SshConnection("root@10.0.0.2", "pw", "-o foo")):
                    with mock.patch("timecapsulesmb.app.operations.run_doctor_checks", side_effect=fake_run_doctor_checks):
                        rc = service.run_api_request({"operation": "doctor", "params": {}}, collector.sink)

        self.assertEqual(rc, 1)
        self.assertEqual(collector.events_of_type("error"), [])
        result = collector.events_of_type("result")[0]
        self.assertEqual(result["ok"], False)
        self.assertTrue(result["payload"]["fatal"])
        self.assertNotIn("pw", json.dumps(collector.events))

    def test_deploy_dry_run_returns_structured_plan_without_remote_actions(self) -> None:
        collector = CollectingSink()
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")
        target = SimpleNamespace(connection=connection, probe_state=probed_state())
        artifacts = {
            "smbd": SimpleNamespace(absolute_path=REPO_ROOT / "bin/samba4/smbd"),
            "mdns-advertiser": SimpleNamespace(absolute_path=REPO_ROOT / "bin/mdns/mdns-advertiser"),
            "nbns-advertiser": SimpleNamespace(absolute_path=REPO_ROOT / "bin/nbns/nbns-advertiser"),
        }

        with mock.patch("timecapsulesmb.app.operations.load_env_config", return_value=AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"})):
            with mock.patch("timecapsulesmb.app.operations.resolve_validated_managed_target", return_value=target):
                with mock.patch("timecapsulesmb.app.operations.resolve_app_paths", return_value=SimpleNamespace(distribution_root=REPO_ROOT)):
                    with mock.patch("timecapsulesmb.app.operations.validate_artifacts", return_value=[("smbd", True, "ok")]):
                        with mock.patch("timecapsulesmb.app.operations.resolve_payload_artifacts", return_value=artifacts):
                            with mock.patch("timecapsulesmb.app.operations.run_remote_actions", side_effect=AssertionError("dry run should not run remote actions")):
                                rc = service.run_api_request(
                                    {"operation": "deploy", "params": {"dry_run": True, "yes": True}},
                                    collector.sink,
                                )

        self.assertEqual(rc, 0)
        result = collector.events_of_type("result")[0]
        self.assertEqual(result["payload"]["host"], "root@10.0.0.2")
        self.assertEqual(result["payload"]["reboot_required"], True)
        self.assertEqual(result["payload"]["requires_reboot"], True)
        self.assertEqual(result["payload"]["payload_family"], "netbsd6_samba4")
        self.assertEqual(result["payload"]["schema_version"], 1)

    def test_deploy_requires_reboot_confirmation_before_remote_actions(self) -> None:
        collector = CollectingSink()
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")
        target = SimpleNamespace(connection=connection, probe_state=probed_state())
        artifacts = {
            "smbd": SimpleNamespace(absolute_path=REPO_ROOT / "bin/samba4/smbd"),
            "mdns-advertiser": SimpleNamespace(absolute_path=REPO_ROOT / "bin/mdns/mdns-advertiser"),
            "nbns-advertiser": SimpleNamespace(absolute_path=REPO_ROOT / "bin/nbns/nbns-advertiser"),
        }

        with mock.patch("timecapsulesmb.app.operations.load_env_config", return_value=AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"})):
            with mock.patch("timecapsulesmb.app.operations.resolve_validated_managed_target", return_value=target):
                with mock.patch("timecapsulesmb.app.operations.resolve_app_paths", return_value=SimpleNamespace(distribution_root=REPO_ROOT)):
                    with mock.patch("timecapsulesmb.app.operations.validate_artifacts", return_value=[("smbd", True, "ok")]):
                        with mock.patch("timecapsulesmb.app.operations.resolve_payload_artifacts", return_value=artifacts):
                            with mock.patch("timecapsulesmb.app.operations.run_remote_actions") as remote_actions:
                                rc = service.run_api_request(
                                    {"operation": "deploy", "params": {"dry_run": False, "confirm_deploy": True}},
                                    collector.sink,
                                )

        self.assertEqual(rc, 1)
        self.assertEqual(collector.events_of_type("error")[0]["code"], "confirmation_required")
        remote_actions.assert_not_called()

    def test_deploy_requires_netbsd4_activation_confirmation_before_remote_actions(self) -> None:
        collector = CollectingSink()
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")
        target = SimpleNamespace(connection=connection, probe_state=netbsd4_probed_state())
        artifacts = {
            "smbd": SimpleNamespace(absolute_path=REPO_ROOT / "bin/samba4-netbsd4be/smbd"),
            "mdns-advertiser": SimpleNamespace(absolute_path=REPO_ROOT / "bin/mdns-netbsd4be/mdns-advertiser"),
            "nbns-advertiser": SimpleNamespace(absolute_path=REPO_ROOT / "bin/nbns-netbsd4be/nbns-advertiser"),
        }

        with mock.patch("timecapsulesmb.app.operations.load_env_config", return_value=AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"})):
            with mock.patch("timecapsulesmb.app.operations.resolve_validated_managed_target", return_value=target):
                with mock.patch("timecapsulesmb.app.operations.resolve_app_paths", return_value=SimpleNamespace(distribution_root=REPO_ROOT)):
                    with mock.patch("timecapsulesmb.app.operations.validate_artifacts", return_value=[("smbd", True, "ok")]):
                        with mock.patch("timecapsulesmb.app.operations.resolve_payload_artifacts", return_value=artifacts):
                            with mock.patch("timecapsulesmb.app.operations.wait_for_mast_volumes_conn") as read_mast:
                                with mock.patch("timecapsulesmb.app.operations.run_remote_actions") as remote_actions:
                                    rc = service.run_api_request(
                                        {"operation": "deploy", "params": {"dry_run": False, "confirm_deploy": True}},
                                        collector.sink,
                                    )

        self.assertEqual(rc, 1)
        self.assertEqual(collector.events_of_type("error")[0]["code"], "confirmation_required")
        read_mast.assert_not_called()
        remote_actions.assert_not_called()

    def test_deploy_requires_deploy_confirmation_even_without_reboot(self) -> None:
        collector = CollectingSink()

        with mock.patch("timecapsulesmb.app.operations.load_env_config") as load_config:
            rc = service.run_api_request(
                {"operation": "deploy", "params": {"dry_run": False, "no_reboot": True}},
                collector.sink,
            )

        self.assertEqual(rc, 1)
        error = self.assert_single_terminal_event(collector, "error")
        self.assertEqual(error["code"], "confirmation_required")
        load_config.assert_not_called()

    def test_deploy_no_reboot_uploads_and_skips_reboot_wait(self) -> None:
        collector = CollectingSink()
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")
        target = SimpleNamespace(connection=connection, probe_state=probed_state())
        artifacts = {
            "smbd": SimpleNamespace(absolute_path=REPO_ROOT / "bin/samba4/smbd"),
            "mdns-advertiser": SimpleNamespace(absolute_path=REPO_ROOT / "bin/mdns/mdns-advertiser"),
            "nbns-advertiser": SimpleNamespace(absolute_path=REPO_ROOT / "bin/nbns/nbns-advertiser"),
        }
        payload_home = operations.build_dry_run_payload_home(operations.MANAGED_PAYLOAD_DIR_NAME)

        with mock.patch("timecapsulesmb.app.operations.load_env_config", return_value=AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"})):
            with mock.patch("timecapsulesmb.app.operations.resolve_validated_managed_target", return_value=target):
                with mock.patch("timecapsulesmb.app.operations.resolve_app_paths", return_value=SimpleNamespace(distribution_root=REPO_ROOT)):
                    with mock.patch("timecapsulesmb.app.operations.validate_artifacts", return_value=[("smbd", True, "ok")]):
                        with mock.patch("timecapsulesmb.app.operations.resolve_payload_artifacts", return_value=artifacts):
                            with mock.patch("timecapsulesmb.app.operations.wait_for_mast_volumes_conn", return_value=SimpleNamespace(volumes=("dk2",), attempts=1, raw_output="")):
                                with mock.patch("timecapsulesmb.app.operations.select_payload_home_with_diagnostics_conn", return_value=SimpleNamespace(payload_home=payload_home)):
                                    with mock.patch("timecapsulesmb.app.operations.verify_payload_home_conn", return_value=SimpleNamespace(ok=True, detail="ok")):
                                        with mock.patch("timecapsulesmb.app.operations.upload_deployment_payload") as upload:
                                            with mock.patch("timecapsulesmb.app.operations.run_remote_actions"):
                                                with mock.patch("timecapsulesmb.app.operations.flush_remote_filesystem_writes"):
                                                    with mock.patch("timecapsulesmb.app.operations.wait_for_ssh_state_conn") as wait:
                                                        rc = service.run_api_request(
                                                            {
                                                                "operation": "deploy",
                                                                "params": {
                                                                    "dry_run": False,
                                                                    "no_reboot": True,
                                                                    "confirm_deploy": True,
                                                                },
                                                            },
                                                            collector.sink,
                                                        )

        self.assertEqual(rc, 0)
        upload.assert_called_once()
        wait.assert_not_called()
        self.assertEqual(collector.events_of_type("result")[0]["payload"]["rebooted"], False)

    def test_deploy_reports_no_mast_volumes_as_remote_error(self) -> None:
        collector = CollectingSink()
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")
        target = SimpleNamespace(connection=connection, probe_state=probed_state())
        artifacts = {
            "smbd": SimpleNamespace(absolute_path=REPO_ROOT / "bin/samba4/smbd"),
            "mdns-advertiser": SimpleNamespace(absolute_path=REPO_ROOT / "bin/mdns/mdns-advertiser"),
            "nbns-advertiser": SimpleNamespace(absolute_path=REPO_ROOT / "bin/nbns/nbns-advertiser"),
        }

        with mock.patch("timecapsulesmb.app.operations.load_env_config", return_value=AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"})):
            with mock.patch("timecapsulesmb.app.operations.resolve_validated_managed_target", return_value=target):
                with mock.patch("timecapsulesmb.app.operations.resolve_app_paths", return_value=SimpleNamespace(distribution_root=REPO_ROOT)):
                    with mock.patch("timecapsulesmb.app.operations.validate_artifacts", return_value=[("smbd", True, "ok")]):
                        with mock.patch("timecapsulesmb.app.operations.resolve_payload_artifacts", return_value=artifacts):
                            with mock.patch("timecapsulesmb.app.operations.wait_for_mast_volumes_conn", return_value=SimpleNamespace(volumes=(), attempts=1, raw_output="")):
                                rc = service.run_api_request(
                                    {
                                        "operation": "deploy",
                                        "params": {
                                            "dry_run": False,
                                            "confirm_deploy": True,
                                            "confirm_reboot": True,
                                        },
                                    },
                                    collector.sink,
                                )

        self.assertEqual(rc, 1)
        error = collector.events_of_type("error")[0]
        self.assertEqual(error["code"], "remote_error")
        self.assertEqual(error["recovery"]["title"], "No HFS volumes found")

    def test_activate_requires_explicit_confirmation(self) -> None:
        collector = CollectingSink()
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")
        target = SimpleNamespace(
            connection=connection,
            probe_state=ProbedDeviceState(
                probe_result=probed_state().probe_result,
                compatibility=supported_compatibility("netbsd4le_samba4"),
            ),
        )

        with mock.patch("timecapsulesmb.app.operations.load_env_config", return_value=AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"})):
            with mock.patch("timecapsulesmb.app.operations.resolve_validated_managed_target", return_value=target):
                with mock.patch("timecapsulesmb.app.operations.run_remote_actions") as remote_actions:
                    rc = service.run_api_request({"operation": "activate", "params": {}}, collector.sink)

        self.assertEqual(rc, 1)
        self.assertEqual(collector.events_of_type("error")[0]["code"], "confirmation_required")
        remote_actions.assert_not_called()

    def test_activate_accepts_yes_alias_for_confirmation(self) -> None:
        collector = CollectingSink()
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")
        target = SimpleNamespace(connection=connection, probe_state=netbsd4_probed_state())

        with mock.patch("timecapsulesmb.app.operations.load_env_config", return_value=AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"})):
            with mock.patch("timecapsulesmb.app.operations.resolve_validated_managed_target", return_value=target):
                with mock.patch("timecapsulesmb.app.operations.probe_managed_runtime_conn", return_value=SimpleNamespace(ready=True)):
                    with mock.patch("timecapsulesmb.app.operations.run_remote_actions") as remote_actions:
                        rc = service.run_api_request(
                            {"operation": "activate", "params": {"yes": True}},
                            collector.sink,
                        )

        self.assertEqual(rc, 0)
        result = self.assert_single_terminal_event(collector, "result")
        self.assertEqual(result["payload"]["already_active"], True)
        self.assertEqual(result["payload"]["schema_version"], 1)
        self.assertEqual(result["payload"]["summary"], "NetBSD4 payload was already active.")
        remote_actions.assert_not_called()

    def test_uninstall_requires_confirmation_before_remote_removal(self) -> None:
        collector = CollectingSink()
        config = AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"})

        with mock.patch("timecapsulesmb.app.operations.load_env_config", return_value=config):
            with mock.patch("timecapsulesmb.app.operations.resolve_env_connection") as resolve_connection:
                with mock.patch("timecapsulesmb.app.operations.remote_uninstall_payload") as uninstall:
                    rc = service.run_api_request({"operation": "uninstall", "params": {}}, collector.sink)

        self.assertEqual(rc, 1)
        self.assertEqual(collector.events_of_type("error")[0]["code"], "confirmation_required")
        resolve_connection.assert_not_called()
        uninstall.assert_not_called()

    def test_uninstall_requires_reboot_confirmation_before_remote_connection(self) -> None:
        collector = CollectingSink()

        with mock.patch("timecapsulesmb.app.operations.load_env_config") as load_config:
            rc = service.run_api_request(
                {"operation": "uninstall", "params": {"confirm_uninstall": True}},
                collector.sink,
            )

        self.assertEqual(rc, 1)
        self.assertEqual(collector.events_of_type("error")[0]["code"], "confirmation_required")
        load_config.assert_not_called()

    def test_uninstall_dry_run_bypasses_confirmation_and_returns_plan(self) -> None:
        collector = CollectingSink()
        config = AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"})
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")

        with mock.patch("timecapsulesmb.app.operations.load_env_config", return_value=config):
            with mock.patch("timecapsulesmb.app.operations.resolve_env_connection", return_value=connection):
                with mock.patch("timecapsulesmb.app.operations.remote_uninstall_payload") as uninstall:
                    rc = service.run_api_request(
                        {"operation": "uninstall", "params": {"dry_run": True}},
                        collector.sink,
                    )

        self.assertEqual(rc, 0)
        result = self.assert_single_terminal_event(collector, "result")
        self.assertIn("remote_actions", result["payload"])
        self.assertEqual(result["payload"]["requires_reboot"], True)
        self.assertEqual(result["payload"]["schema_version"], 1)
        uninstall.assert_not_called()

    def test_fsck_requires_confirmation_before_remote_connection(self) -> None:
        collector = CollectingSink()
        config = AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"})

        with mock.patch("timecapsulesmb.app.operations.load_env_config", return_value=config):
            with mock.patch("timecapsulesmb.app.operations.resolve_env_connection") as resolve_connection:
                rc = service.run_api_request({"operation": "fsck", "params": {}}, collector.sink)

        self.assertEqual(rc, 1)
        self.assertEqual(collector.events_of_type("error")[0]["code"], "confirmation_required")
        resolve_connection.assert_not_called()

    def test_repair_xattrs_uses_structured_runner(self) -> None:
        collector = CollectingSink()
        summary = repair_xattrs_domain.RepairSummary(scanned=1, scanned_files=1, unreadable=1, repairable=1)
        repair_result = SimpleNamespace(
            returncode=0,
            root=Path("/Volumes/Data"),
            findings=[SimpleNamespace(path=Path("/Volumes/Data/broken"))],
            candidates=[SimpleNamespace(path=Path("/Volumes/Data/broken"))],
            summary=summary,
            report="detected issues",
        )

        with mock.patch("timecapsulesmb.app.operations.sys.platform", "darwin"):
            with mock.patch("timecapsulesmb.app.operations.load_optional_env_config", return_value=AppConfig.missing()):
                with mock.patch("timecapsulesmb.app.operations.repair_xattrs_cli.run_repair_structured", return_value=repair_result) as runner:
                    rc = service.run_api_request(
                        {
                            "operation": "repair-xattrs",
                            "params": {"path": "/Volumes/Data", "dry_run": True},
                        },
                        collector.sink,
                    )

        self.assertEqual(rc, 0)
        runner.assert_called_once()
        self.assertEqual(collector.events_of_type("result")[0]["payload"]["finding_count"], 1)

    def test_repair_xattrs_captures_direct_stdout_and_stderr_logs(self) -> None:
        collector = CollectingSink()
        summary = repair_xattrs_domain.RepairSummary(scanned=1)
        repair_result = SimpleNamespace(
            returncode=0,
            root=Path("/Volumes/Data"),
            findings=[],
            candidates=[],
            summary=summary,
            report=None,
        )

        def fake_runner(*_args, **_kwargs):
            print("stdout detail")
            print("stderr detail", file=sys.stderr)
            return repair_result

        with mock.patch("timecapsulesmb.app.operations.sys.platform", "darwin"):
            with mock.patch("timecapsulesmb.app.operations.load_optional_env_config", return_value=AppConfig.missing()):
                with mock.patch("timecapsulesmb.app.operations.repair_xattrs_cli.run_repair_structured", side_effect=fake_runner):
                    rc = service.run_api_request(
                        {
                            "operation": "repair-xattrs",
                            "params": {"path": "/Volumes/Data", "dry_run": True},
                        },
                        collector.sink,
                    )

        logs = collector.events_of_type("log")
        self.assertEqual(rc, 0)
        self.assertIn({"info": "stdout detail"}, [{log["level"]: log["message"]} for log in logs])
        self.assertIn({"warning": "stderr detail"}, [{log["level"]: log["message"]} for log in logs])

    def test_repair_xattrs_rejects_invalid_max_depth_before_runner(self) -> None:
        for max_depth in ("bad", -1, True):
            with self.subTest(max_depth=max_depth):
                collector = CollectingSink()
                with mock.patch("timecapsulesmb.app.operations.sys.platform", "darwin"):
                    with mock.patch("timecapsulesmb.app.operations.load_optional_env_config", return_value=AppConfig.missing()):
                        with mock.patch("timecapsulesmb.app.operations.repair_xattrs_cli.run_repair_structured") as runner:
                            rc = service.run_api_request(
                                {
                                    "operation": "repair-xattrs",
                                    "params": {
                                        "path": "/Volumes/Data",
                                        "dry_run": True,
                                        "max_depth": max_depth,
                                    },
                                },
                                collector.sink,
                            )

                self.assertEqual(rc, 1)
                error = self.assert_single_terminal_event(collector, "error")
                self.assertEqual(error["code"], "validation_failed")
                self.assertEqual(error["recovery"]["title"], "Invalid repair options")
                runner.assert_not_called()

    def test_repair_xattrs_passes_valid_max_depth_as_int(self) -> None:
        collector = CollectingSink()
        summary = repair_xattrs_domain.RepairSummary(scanned=1)
        repair_result = SimpleNamespace(
            returncode=0,
            root=Path("/Volumes/Data"),
            findings=[],
            candidates=[],
            summary=summary,
            report=None,
        )

        with mock.patch("timecapsulesmb.app.operations.sys.platform", "darwin"):
            with mock.patch("timecapsulesmb.app.operations.load_optional_env_config", return_value=AppConfig.missing()):
                with mock.patch("timecapsulesmb.app.operations.repair_xattrs_cli.run_repair_structured", return_value=repair_result) as runner:
                    rc = service.run_api_request(
                        {
                            "operation": "repair-xattrs",
                            "params": {
                                "path": "/Volumes/Data",
                                "dry_run": True,
                                "max_depth": "2",
                            },
                        },
                        collector.sink,
                    )

        self.assertEqual(rc, 0)
        args = runner.call_args.args[0]
        self.assertEqual(args.max_depth, 2)

    def test_repair_xattrs_requires_confirmation_for_non_dry_run(self) -> None:
        collector = CollectingSink()

        with mock.patch("timecapsulesmb.app.operations.repair_xattrs_cli.run_repair_structured") as runner:
            rc = service.run_api_request(
                {
                    "operation": "repair-xattrs",
                    "params": {"path": "/Volumes/Data", "dry_run": False},
                },
                collector.sink,
            )

        self.assertEqual(rc, 1)
        error = self.assert_single_terminal_event(collector, "error")
        self.assertEqual(error["code"], "confirmation_required")
        self.assertEqual(error["recovery"]["title"], "Repair confirmation required")
        runner.assert_not_called()

    def test_helper_reads_request_and_writes_ndjson(self) -> None:
        output = io.StringIO()
        fake_stdin = io.StringIO('{"operation":"paths","params":{}}')
        with mock.patch.object(sys, "stdin", fake_stdin):
            with mock.patch("timecapsulesmb.app.helper.run_api_request") as run_mock:
                run_mock.side_effect = lambda request, sink: (sink.result(request["operation"], ok=True, payload={"ok": True}) or 0)
                with redirect_stdout(output):
                    rc = helper.main([])

        self.assertEqual(rc, 0)
        line = json.loads(output.getvalue())
        self.assertEqual(line["type"], "result")
        self.assertEqual(line["operation"], "paths")
        self.assertEqual(line["schema_version"], 1)
        self.assertTrue(line["request_id"])

    def test_helper_rejects_invalid_json_without_leaking_pretty_error_details(self) -> None:
        output = io.StringIO()
        error_output = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO('{"operation":"paths","password":"secret"')):
            with redirect_stdout(output):
                with mock.patch.object(sys, "stderr", error_output):
                    rc = helper.main(["--pretty-error"])

        self.assertEqual(rc, 1)
        event = json.loads(output.getvalue())
        self.assertEqual(event["type"], "error")
        self.assertEqual(event["code"], "invalid_request")
        self.assertNotIn("secret", error_output.getvalue())

    def test_helper_rejects_top_level_non_object_json(self) -> None:
        output = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO('["paths"]')):
            with redirect_stdout(output):
                rc = helper.main([])

        self.assertEqual(rc, 1)
        event = json.loads(output.getvalue())
        self.assertEqual(event["type"], "error")
        self.assertEqual(event["operation"], "api")
        self.assertEqual(event["code"], "invalid_request")
        self.assertEqual(event["schema_version"], 1)
        self.assertTrue(event["request_id"])

    def test_api_command_is_registered(self) -> None:
        self.assertIs(cli_main.COMMANDS["api"], helper.main)


if __name__ == "__main__":
    unittest.main()
