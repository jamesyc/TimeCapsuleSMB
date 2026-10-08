from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
import io
import ipaddress
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack, contextmanager, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from timecapsulesmb.device.migration_jobs import MigrationActivity
from timecapsulesmb.checks.network import LocalInterfaceNetwork
from timecapsulesmb.core.net import RouteSelection
from timecapsulesmb.core.messages import NETBSD4_ACTIVATION_COMPLETED
from timecapsulesmb.core.summaries import Summary
from timecapsulesmb.app.events import AppClient, AppEvent, EventSink
from timecapsulesmb.app.context import AppOperationContext
from timecapsulesmb.app.confirmations import build_confirmation
from timecapsulesmb.app import contracts, helper, service
from timecapsulesmb.flash_workflow import SecondaryBankInvalidError, SecondaryBankReadMismatchError
from tests.flash_fixtures import inspect_full_banks, plan_full_restore
from tests.reboot_support import DEVICE_AIRPORT_MAC
from timecapsulesmb.services.version_check import VersionCheckResult
from timecapsulesmb.cli import main as cli_main
from timecapsulesmb.checks.models import CheckResult
from timecapsulesmb.core.config import MANAGED_PAYLOAD_DIR_NAME, AppConfig, ConfigError, parse_env_file
from timecapsulesmb.device.compat import DeviceCompatibility
from timecapsulesmb.device.errors import DeviceError
from timecapsulesmb.core.release import CLI_VERSION_CODE, RELEASE_TAG
from timecapsulesmb.device.compat import compatibility_from_probe_result
from timecapsulesmb.device.probe import (
    DeployedVersionProbeResult,
    ManagedRuntimeProbeResult,
    ProbeResult,
    ProbeStepResult,
    ProbedDeviceState,
    ReadinessProbeResult,
    SshAccessStatus,
)
from timecapsulesmb.device.storage import (
    MaStDiscoveryResult,
    MaStVolume,
    PayloadCandidateCheck,
    PayloadHome,
    PayloadHomeSelection,
    StorageDeviceError,
    VolumeMountResult,
    build_dry_run_payload_home,
)
from timecapsulesmb.deploy.executor import XattrMigrationResult
from timecapsulesmb.deploy.planner import GENERATED_FLASH_CONFIG_SOURCE
from timecapsulesmb.discovery.bonjour import BonjourQueryDiagnostics, BonjourDiscoverySnapshot, BonjourResolvedService, BonjourServiceInstance
from timecapsulesmb.integrations.acp import ACPAuthError, ACPConnectionError
from timecapsulesmb.services.acp_ssh import SSH_ENABLE_TIMEOUT_MESSAGE
from timecapsulesmb.services.app import AppOperationError, jsonable
from timecapsulesmb.services.flash import (
    FLASH_UNSUPPORTED_DEVICE_MESSAGE,
    STALE_BACKUP_AFTER_WRITE_MESSAGE,
    require_backup_fresh_for_plan,
)
from timecapsulesmb.services.maintenance import FSCK_NOT_UNMOUNTED_MESSAGE
from timecapsulesmb.services.reboot import RebootFlowError, reboot_device
from tests.reboot_support import FakeAcpDevice, FakeInstalledRuntime, acp_password_answer
from timecapsulesmb.services.set_ssh import SetSshResult, SetSshStatusResult
from timecapsulesmb.transport.errors import (
    LOCAL_NETWORK_FILTERED_MESSAGE,
    SshCommandTimeout,
    SshError,
    SshLocalNetworkFilteredError,
    TransportError,
    ssh_timeout_slow_device_message,
)
from timecapsulesmb.services.runtime import AIRPORT_PASSWORD_MISMATCH_MESSAGE
from timecapsulesmb.transport.ssh import SshConnection


class SampleMode(Enum):
    FAST = "fast"


@dataclass(frozen=True)
class SamplePayload:
    mode: SampleMode


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
            ssh_status=SshAccessStatus.OPEN_AUTHENTICATED,
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
            ssh_status=SshAccessStatus.OPEN_AUTHENTICATED,
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


def airport_express_probed_state() -> ProbedDeviceState:
    # An AirPort Express over SSH: NetBSD 4.0_STABLE on a MIPS ar7240.
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


def unreachable_probed_state() -> ProbedDeviceState:
    return ProbedDeviceState(
        probe_result=ProbeResult(
            ssh_status=SshAccessStatus.CLOSED,
            error="connection refused",
            os_name="",
            os_release="",
            arch="",
            elf_endianness="",
        ),
        compatibility=None,
    )


def failed_probe_state(status: SshAccessStatus, error: str) -> ProbedDeviceState:
    return ProbedDeviceState(
        probe_result=ProbeResult(
            ssh_status=status,
            error=error,
            os_name="",
            os_release="",
            arch="",
            elf_endianness="unknown",
        ),
        compatibility=None,
    )


AUTH_REJECTED_ERROR = "root@10.0.0.2: Permission denied (publickey,password,keyboard-interactive)."
KEX_CLOSED_ERROR = "Connecting to the device failed, SSH error: kex_exchange_identification: Connection closed by remote host"


def managed_runtime_probe(ready: bool = True) -> ManagedRuntimeProbeResult:
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


class AppApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._exit_stack = ExitStack()
        self._telemetry_client = mock.Mock()
        # Every address of a selected record answers ACP unless a test says
        # otherwise, so configure keeps the record's preferred address.
        self._record_acp_probe = self._exit_stack.enter_context(
            mock.patch("timecapsulesmb.services.configure_target.tcp_connect_error", return_value=None)
        )
        # App API tests exercise GUI/backend telemetry-enabled operations.
        # Keep telemetry mocked here so unit tests never POST to the live telemetry service.
        self._telemetry_factory = self._exit_stack.enter_context(
            mock.patch("timecapsulesmb.app.service.TelemetryClient.from_config", return_value=self._telemetry_client)
        )
        # This tripwire catches future tests that accidentally bypass the app-service telemetry mock.
        self._telemetry_urlopen = self._exit_stack.enter_context(
            mock.patch("timecapsulesmb.telemetry.urllib.request.urlopen", side_effect=AssertionError("tests must not send telemetry"))
        )
        # activate first checks the device holds an install of this version;
        # tests that model a missing or other install set these return values.
        self._installed_config_present = self._exit_stack.enter_context(
            mock.patch("timecapsulesmb.services.activation.flash_runtime_config_present_conn", return_value=True)
        )
        self._installed_version = self._exit_stack.enter_context(
            mock.patch(
                "timecapsulesmb.services.activation.read_deployed_version_conn",
                return_value=DeployedVersionProbeResult(RELEASE_TAG, CLI_VERSION_CODE, "ok"),
            )
        )
        self._flash_capacity = self._exit_stack.enter_context(
            mock.patch("timecapsulesmb.services.deploy._probe_flash_capacity", return_value=(1024 * 1024, 128 * 1024))
        )
        self._exit_stack.enter_context(
            mock.patch("timecapsulesmb.services.deploy._flash_files_holding_new_bytes", return_value=set())
        )
        self._install_identity = self._exit_stack.enter_context(
            mock.patch(
                "timecapsulesmb.app.ops.deploy.load_install_identity",
                return_value=SimpleNamespace(telemetry_enabled=True),
            )
        )
        self._xattr_migration = self._exit_stack.enter_context(
            mock.patch(
                "timecapsulesmb.services.deploy.migrate_xattr_tdb_to_hfs",
                return_value=XattrMigrationResult("migration=complete", ()),
            )
        )

        from tests.test_xattr_migration import fake_inventory
        self._exit_stack.enter_context(mock.patch("timecapsulesmb.services.deploy.inventory_metadata", side_effect=lambda *_a: fake_inventory()))
        self._exit_stack.enter_context(mock.patch("timecapsulesmb.services.deploy.probe_migration_activity", return_value=MigrationActivity(())))
        self._exit_stack.enter_context(mock.patch("timecapsulesmb.services.deploy.inspect_sources"))

        self._exit_stack.enter_context(mock.patch("timecapsulesmb.services.deploy.flush_remote_filesystem_writes"))

    def tearDown(self) -> None:
        self._exit_stack.close()

    def assert_single_terminal_event(self, collector: CollectingSink, event_type: str) -> dict[str, object]:
        terminals = collector.events_of_type("result") + collector.events_of_type("error")
        self.assertEqual([event["type"] for event in terminals], [event_type])
        return terminals[0]

    def assert_confirmation(
        self,
        collector: CollectingSink,
        presentation_id: str,
        presentation_values: dict[str, object] | None = None,
    ) -> dict[str, object]:
        error = self.assert_single_terminal_event(collector, "error")
        self.assertEqual(error["code"], "confirmation_required")
        details = error["details"]
        self.assertEqual(details["presentation_id"], presentation_id)
        self.assertTrue(details["message"].endswith("?"), details["message"])
        self.assertIn("confirmation_id", details)
        for key, value in (presentation_values or {}).items():
            self.assertEqual(details["presentation_values"][key], value)
        return details

    def confirmation_id_for(
        self,
        operation: str,
        params: dict[str, object],
        context: dict[str, object],
    ) -> str:
        return build_confirmation(
            operation=operation,
            params=params,
            title="test",
            message="test?",
            action_title="test",
            risk="remote_write",
            summary="test",
            context=context,
            presentation_id="test",
        ).confirmation_id

    @staticmethod
    def fake_reboot_request(*_args, callbacks=None, **_kwargs) -> None:
        if callbacks is not None:
            if callbacks.set_stage is not None:
                callbacks.set_stage("reboot")
            if callbacks.update_fields is not None:
                callbacks.update_fields(reboot_was_attempted=True)
            if callbacks.add_debug_fields is not None:
                callbacks.add_debug_fields(reboot_request_strategy="network_acp", acp_reboot_succeeded=True)

    def test_event_redacts_sensitive_fields(self) -> None:
        event = AppEvent("result", "configure", {
            "ok": True,
            "payload": {
                "password": "secret",
                "nested": {
                    "TC_PASSWORD": "secret",
                    "credentials": {"password": "nested-secret"},
                    "session_token": "token-secret",
                    "localization_key": "remote_error.ssh_timeout_slow_device",
                    "key_id": "observed-k30a-78100",
                },
            },
        })

        data = event.to_jsonable()

        self.assertEqual(data["payload"]["password"], "<redacted>")
        self.assertEqual(data["payload"]["nested"]["TC_PASSWORD"], "<redacted>")
        self.assertEqual(data["payload"]["nested"]["credentials"], "<redacted>")
        self.assertEqual(data["payload"]["nested"]["session_token"], "<redacted>")
        self.assertEqual(data["payload"]["nested"]["localization_key"], "remote_error.ssh_timeout_slow_device")
        self.assertEqual(data["payload"]["nested"]["key_id"], "observed-k30a-78100")

    def test_result_event_preserves_falsey_payloads(self) -> None:
        collector = CollectingSink()

        collector.sink.result("capabilities", ok=True, payload=[])

        result = collector.events_of_type("result")[0]
        self.assertEqual(result["payload"], [])
        self.assertEqual(result["schema_version"], 1)
        self.assertTrue(result["request_id"])

    def test_app_operation_context_builds_operation_callbacks(self) -> None:
        collector = CollectingSink()
        context = AppOperationContext("deploy", collector.sink)
        callbacks = context.to_operation_callbacks()

        callbacks.set_stage("reboot")
        callbacks.update_fields(reboot_was_attempted=True)
        callbacks.add_debug_fields(reboot_request_strategy="network_acp")
        callbacks.measurement("reboot_request", strategy="network_acp")
        callbacks.log("reboot requested")

        self.assertEqual(context.current_stage, "reboot")
        self.assertEqual(context.finish_fields["reboot_was_attempted"], True)
        self.assertEqual(context.diagnostics.debug_fields["reboot_request_strategy"], "network_acp")
        self.assertEqual(
            context.execution_telemetry(result="success")["measurements"]["reboot_request"][0]["strategy"],
            "network_acp",
        )
        self.assertEqual(collector.events_of_type("log")[0]["message"], "reboot requested")

    def test_app_operation_context_runtime_callbacks_remain_compatible(self) -> None:
        collector = CollectingSink()
        context = AppOperationContext("deploy", collector.sink)

        context.to_operation_callbacks().set_stage("reboot")

        self.assertEqual(context.current_stage, "reboot")

    def test_jsonable_serializes_enum_values_inside_dataclasses(self) -> None:
        self.assertEqual(jsonable(SamplePayload(SampleMode.FAST)), {"mode": "fast"})

    def test_stage_events_include_policy_metadata(self) -> None:
        collector = CollectingSink()

        collector.sink.stage("capabilities", "resolve_paths")
        collector.sink.stage("deploy", "upload_payload")
        collector.sink.stage("uninstall", "uninstall_payload")
        collector.sink.stage("deploy", "reboot")
        collector.sink.stage("fsck", "list_fsck_volumes")

        stages = collector.events_of_type("stage")
        self.assertEqual(stages[0]["risk"], "local_read")
        self.assertTrue(stages[0]["cancellable"])
        self.assertEqual(stages[1]["risk"], "remote_write")
        self.assertEqual(stages[2]["risk"], "destructive")
        self.assertEqual(stages[3]["risk"], "reboot")
        self.assertIn("description", stages[3])
        self.assertEqual(stages[4]["risk"], "remote_read")
        self.assertTrue(stages[4]["cancellable"])
        self.assertIn("description", stages[4])

    def test_restart_stages_after_the_fsck_reboot_include_policy_metadata(self) -> None:
        collector = CollectingSink()
        for stage in ("post_reboot_activation", "verify_runtime_activation"):
            collector.sink.stage("fsck", stage)

        stages = collector.events_of_type("stage")
        self.assertEqual([stage["risk"] for stage in stages], ["remote_write", "remote_read"])
        self.assertEqual([stage["cancellable"] for stage in stages], [False, True])
        self.assertTrue(all(stage.get("description") for stage in stages))

    def test_legacy_metadata_and_software_replacement_stages_include_policy_metadata(self) -> None:
        collector = CollectingSink()

        collector.sink.stage("deploy", "inventory_legacy_metadata")
        collector.sink.stage("deploy", "replace_software")
        collector.sink.stage("deploy", "install_runtime_config")

        stages = collector.events_of_type("stage")
        self.assertEqual(stages[0]["risk"], "remote_read")
        self.assertTrue(stages[0]["cancellable"])
        self.assertEqual(stages[1]["risk"], "remote_write")
        self.assertFalse(stages[1]["cancellable"])
        self.assertEqual(stages[2]["risk"], "remote_write")
        self.assertFalse(stages[2]["cancellable"])
        for stage in stages:
            self.assertTrue(stage["description"])
        self.assertEqual(collector.sink.current_risk("deploy"), "remote_write")

    def test_unknown_stage_omits_policy_metadata_and_keeps_previous_risk(self) -> None:
        collector = CollectingSink()

        collector.sink.stage("deploy", "reboot")
        collector.sink.stage("deploy", "not_a_stage")

        unknown = collector.events_of_type("stage")[1]
        self.assertEqual(unknown["stage"], "not_a_stage")
        self.assertNotIn("risk", unknown)
        self.assertNotIn("cancellable", unknown)
        self.assertNotIn("description", unknown)
        self.assertEqual(collector.sink.current_risk("deploy"), "reboot")

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
            "summary": "Deployment dry-run plan generated.",
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
        self.assertEqual(doctor["summary"], "Doctor found one or more fatal problems.")
        self.assertEqual(doctor["schema_version"], 1)

    def test_request_id_propagates_to_every_event(self) -> None:
        collector = CollectingSink()

        rc = service.run_api_request({"request_id": "req-123", "operation": "capabilities", "params": {}}, collector.sink)

        self.assertEqual(rc, 0)
        self.assertTrue(collector.events)
        self.assertEqual({event["request_id"] for event in collector.events}, {"req-123"})
        self.assert_single_terminal_event(collector, "result")

    def test_capabilities_returns_helper_contract_details(self) -> None:
        collector = CollectingSink()

        rc = service.run_api_request({"operation": "capabilities", "params": {}}, collector.sink)

        self.assertEqual(rc, 0)
        payload = self.assert_single_terminal_event(collector, "result")["payload"]
        self.assertEqual(payload["api_schema_version"], 1)
        self.assertIn("deploy", payload["operations"])
        self.assertIn("capabilities", payload["operations"])
        self.assertIn("set-telemetry", payload["operations"])
        self.assertIn("version-check", payload["operations"])
        self.assertIn("flash", payload["operations"])
        self.assertIn("reachability", payload["operations"])
        self.assertIn("set-ssh", payload["operations"])
        self.assertNotIn("update-config-settings", payload["operations"])
        self.assertNotIn("ssh-access", payload["operations"])
        self.assertNotIn("telemetry-identity", payload["operations"])
        self.assertNotIn("paths", payload["operations"])
        self.assertIn("helper_version", payload)
        self.assertIn("artifact_manifest_sha256", payload)

    def test_flash_backup_operation_returns_manifest_payload(self) -> None:
        collector = CollectingSink()
        manifest = {
            "backup_dir": "/tmp/flash-backup",
            "banks": [{"name": "primary"}, {"name": "secondary"}],
        }
        bundle = SimpleNamespace(manifest=manifest)

        with mock.patch("timecapsulesmb.app.ops.flash.load_request_config", return_value=object()):
            with mock.patch("timecapsulesmb.app.ops.flash._resolve_flash_target", return_value=object()):
                with mock.patch("timecapsulesmb.app.ops.flash.backup_flash", return_value=bundle) as backup_mock:
                    rc = service.run_api_request(
                        {"operation": "flash", "params": {"action": "backup", "credentials": {"password": "pw"}}},
                        collector.sink,
                    )

        self.assertEqual(rc, 0)
        backup_mock.assert_called_once()
        self.assertIn("stage", backup_mock.call_args.kwargs)
        payload = self.assert_single_terminal_event(collector, "result")["payload"]
        self.assertEqual(payload["backup_dir"], "/tmp/flash-backup")
        self.assertEqual(payload["counts"], {"banks": 2})

    def test_flash_backup_accepts_request_scoped_password(self) -> None:
        collector = CollectingSink()
        config = AppConfig.from_values(
            {"TC_HOST": "root@10.0.0.2"},
            file_values={"TC_HOST": "root@10.0.0.2"},
        )
        manifest = {
            "backup_dir": "/tmp/flash-backup",
            "banks": [{"name": "primary"}],
        }
        bundle = SimpleNamespace(manifest=manifest)

        with mock.patch("timecapsulesmb.app.ops.common.load_env_config", return_value=config):
            with mock.patch(
                "timecapsulesmb.app.ops.flash.require_connection_compatibility",
                return_value=supported_compatibility("netbsd4be_samba4"),
            ):
                with mock.patch("timecapsulesmb.app.ops.flash.backup_flash", return_value=bundle) as backup_mock:
                    rc = service.run_api_request(
                        {
                            "operation": "flash",
                            "params": {"action": "backup", "credentials": {"password": "request-pw"}},
                        },
                        collector.sink,
                    )

        self.assertEqual(rc, 0)
        target = backup_mock.call_args.kwargs["target"]
        self.assertEqual(target.connection.password, "request-pw")
        self.assertFalse(config.has_file_value("TC_PASSWORD"))

    def test_flash_backup_reports_unsupported_device_message_to_app(self) -> None:
        collector = CollectingSink()
        config = AppConfig.from_values(
            {"TC_HOST": "root@10.0.0.2"},
            file_values={"TC_HOST": "root@10.0.0.2"},
        )

        with mock.patch("timecapsulesmb.app.ops.common.load_env_config", return_value=config):
            with mock.patch(
                "timecapsulesmb.app.ops.flash.require_connection_compatibility",
                return_value=supported_compatibility("netbsd6_samba4"),
            ):
                with mock.patch("timecapsulesmb.app.ops.flash.backup_flash") as backup_mock:
                    rc = service.run_api_request(
                        {
                            "operation": "flash",
                            "params": {"action": "backup", "credentials": {"password": "request-pw"}},
                        },
                        collector.sink,
                    )

        self.assertEqual(rc, 1)
        error = self.assert_single_terminal_event(collector, "error")
        self.assertEqual(error["code"], "unsupported_device")
        self.assertEqual(error["message"], FLASH_UNSUPPORTED_DEVICE_MESSAGE)
        self.assertIn("https://github.com/jamesyc/TimeCapsuleSMB/issues/160", error["message"])
        # A NetBSD 6 Time Capsule is a supported device; flash just is not offered for it.
        self.assert_neutral_unsupported_device_recovery(error)
        backup_mock.assert_not_called()

    def assert_neutral_unsupported_device_recovery(self, error: dict[str, object]) -> None:
        recovery = error["recovery"]
        self.assertEqual(recovery["localization_key"], "unsupported_device")
        self.assertEqual(recovery["message"], "This operation is not supported on the detected AirPort model or OS.")
        self.assertFalse(any("Forget" in action for action in recovery["actions"]))
        self.assertNotIn("cannot run TimeCapsuleSMB", recovery["message"])

    def test_flash_plan_operation_uses_saved_backup_without_device_config(self) -> None:
        collector = CollectingSink()
        manifest = {
            "backup_dir": "/tmp/flash-backup",
            "flash_plan": {
                "mode": "check_apple",
                "write_requested": False,
                "already_satisfied": True,
                "apple_match": {
                    "matched": True,
                    "template_source": "catalog",
                    "template_version": "7.8.1",
                    "template_product_id": "116",
                    "template_sha256": "template-sha",
                    "inner_sha256": "inner-sha",
                    "inner_size": 123,
                    "key_id": "key-one",
                    "inner_model": 116,
                    "inner_version": "0x00070801",
                },
            },
        }
        bundle = SimpleNamespace(manifest=manifest)

        with mock.patch("timecapsulesmb.app.ops.flash.plan_flash_from_backup", return_value=(bundle, object())) as plan_mock:
            with mock.patch("timecapsulesmb.app.ops.flash.load_request_config", side_effect=AssertionError("plan should not load device config")):
                rc = service.run_api_request(
                    {
                        "operation": "flash",
                        "params": {
                            "action": "plan",
                            "backup_dir": "/tmp/flash-backup",
                            "mode": "check_apple",
                        },
                    },
                    collector.sink,
                )

        self.assertEqual(rc, 0)
        plan_mock.assert_called_once()
        self.assertEqual(plan_mock.call_args.kwargs["operation"], "check_apple")
        payload = self.assert_single_terminal_event(collector, "result")["payload"]
        self.assertEqual(payload["mode"], "check_apple")
        self.assertFalse(payload["write_requested"])
        self.assertEqual(payload["summary"], "Active firmware bank matches Apple stock firmware 7.8.1.")
        self.assertEqual(payload["apple_firmware_match"]["matched"], True)
        self.assertEqual(payload["apple_firmware_match"]["template_version"], "7.8.1")
        self.assertIsNone(payload["firmware_payload"])

    def test_flash_plan_failure_is_recorded_in_the_saved_backup(self) -> None:
        from timecapsulesmb.flash import FlashAnalysisError
        from tests.test_flash import save_live_login_backup

        collector = CollectingSink()
        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp) / "backup"
            save_live_login_backup(backup_dir)
            with mock.patch(
                "timecapsulesmb.services.flash.plan_from_operation",
                side_effect=FlashAnalysisError("no candidate firmware bank matches Apple stock firmware"),
            ):
                rc = service.run_api_request(
                    {
                        "operation": "flash",
                        "params": {"action": "plan", "backup_dir": str(backup_dir), "mode": "check_apple"},
                    },
                    collector.sink,
                )
            saved = json.loads((backup_dir / "manifest.json").read_text())

        self.assertEqual(rc, 1)
        error = self.assert_single_terminal_event(collector, "error")
        self.assertEqual(error["code"], "validation_failed")
        self.assertEqual(error["message"], "no candidate firmware bank matches Apple stock firmware")
        self.assertEqual(saved["flash_plan_error"], {
            "stage": "plan_flash",
            "message": "no candidate firmware bank matches Apple stock firmware",
        })
        self.assertEqual(saved["operation"], "check_apple")

    def test_flash_plan_payload_promotes_download_payload_and_saved_path(self) -> None:
        payload = contracts.flash_plan_payload({
            "backup_dir": "/tmp/flash-backup",
            "files": {
                "secondary_download_only_basebinary_payload": "/tmp/flash-backup/secondary.download_only.basebinary",
            },
            "flash_plan": {
                "mode": "download_only",
                "target_bank": "secondary",
                "write_requested": False,
                "already_satisfied": False,
                "apple_match": {
                    "matched": False,
                    "template_source": "catalog",
                    "template_version": "7.8.1",
                },
                "payload": {
                    "template_source": "catalog",
                    "template_path": "/Users/example/Library/Application Support/TimeCapsuleSMB/firmware.basebinary",
                    "template_product_id": "116",
                    "template_version": "7.8.1",
                    "template_sha256": "template-sha",
                    "payload_sha256": "payload-sha",
                    "payload_size": 456,
                    "expected_prefix_sha256": "prefix-sha",
                    "expected_prefix_size": 123,
                    "key_id": "key-one",
                    "inner_model": 116,
                    "inner_version": "0x00070801",
                    "inner_payload_size": 123,
                },
            },
        })

        self.assertEqual(payload["summary"], "Apple restore firmware validated (version 7.8.1, product 116).")
        self.assertEqual(payload["firmware_payload"]["payload_sha256"], "payload-sha")
        self.assertEqual(
            payload["firmware_payload_path"],
            "/tmp/flash-backup/secondary.download_only.basebinary",
        )
        self.assertEqual(payload["apple_firmware_match"]["matched"], False)

    def test_flash_plan_payload_reports_multi_bank_apple_check(self) -> None:
        payload = contracts.flash_plan_payload({
            "backup_dir": "/tmp/flash-backup",
            "flash_plan": {
                "mode": "check_apple",
                "target_bank": None,
                "write_requested": False,
                "already_satisfied": True,
                "apple_match_status": "all_candidates_match",
                "apple_match": {
                    "matched": True,
                    "template_source": "catalog",
                    "template_version": "7.8.1",
                },
                "apple_matches": [
                    {
                        "bank": "primary",
                        "match": {
                            "matched": True,
                            "template_source": "catalog",
                            "template_version": "7.8.1",
                        },
                    },
                    {
                        "bank": "secondary",
                        "match": {
                            "matched": True,
                            "template_source": "catalog",
                            "template_version": "7.8.1",
                        },
                    },
                ],
            },
        })

        self.assertEqual(payload["summary"], "All candidate firmware banks match Apple stock firmware 7.8.1.")
        self.assertEqual(payload["apple_match_status"], "all_candidates_match")
        self.assertEqual(len(payload["apple_firmware_matches"]), 2)
        self.assertTrue(payload["apple_firmware_match"]["matched"])

    def test_flash_plan_payload_promotes_plan_warnings(self) -> None:
        payload = contracts.flash_plan_payload({
            "backup_dir": "/tmp/flash-backup",
            "flash_plan": {
                "mode": "restore",
                "target_bank": "primary",
                "write_requested": True,
                "already_satisfied": False,
                "warnings": [
                    "restore targets primary because multiple firmware banks passed active selection checks",
                ],
            },
        })

        self.assertEqual(payload["warnings"], [
            "restore targets primary because multiple firmware banks passed active selection checks",
        ])

    def test_flash_plan_payload_promotes_generic_download_payload_path(self) -> None:
        payload = contracts.flash_plan_payload({
            "backup_dir": "/tmp/flash-backup",
            "files": {
                "download_only_basebinary_payload": "/tmp/flash-backup/download_only.basebinary",
            },
            "flash_plan": {
                "mode": "download_only",
                "target_bank": None,
                "write_requested": False,
                "already_satisfied": False,
                "payload": {
                    "template_source": "catalog",
                    "template_product_id": "116",
                    "template_version": "7.8.1",
                    "payload_sha256": "payload-sha",
                },
            },
        })

        self.assertEqual(payload["summary"], "Apple restore firmware validated (version 7.8.1, product 116).")
        self.assertEqual(payload["firmware_payload_path"], "/tmp/flash-backup/download_only.basebinary")

    def test_flash_plan_rejects_backup_manifest_used_for_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp)
            (backup_dir / "manifest.json").write_text(json.dumps({
                "write_outcome": {
                    "status": "validated",
                    "mode": "patch",
                    "write_may_have_modified_device": True,
                },
            }))
            collector = CollectingSink()

            rc = service.run_api_request(
                {
                    "operation": "flash",
                    "params": {
                        "action": "plan",
                        "backup_dir": str(backup_dir),
                        "mode": "restore",
                    },
                },
                collector.sink,
            )

        self.assertEqual(rc, 1)
        error = self.assert_single_terminal_event(collector, "error")
        self.assertEqual(error["code"], "validation_failed")
        self.assertEqual(error["message"], STALE_BACKUP_AFTER_WRITE_MESSAGE)

    def test_flash_backup_freshness_allows_noop_or_cancelled_write_outcomes(self) -> None:
        for status in ("not_needed", "cancelled"):
            with self.subTest(status=status):
                require_backup_fresh_for_plan({
                    "write_outcome": {
                        "status": status,
                        "write_may_have_modified_device": False,
                    },
                })

    def test_flash_write_requires_confirmation_then_validates_and_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp)
            manifest = {
                "backup_dir": str(backup_dir),
                "write_outcome": {
                    "status": "validated",
                    "mode": "patch",
                    "write_validated": True,
                },
            }
            target_bank = SimpleNamespace(name="primary", sha256="bank-sha")
            plan = SimpleNamespace(
                already_satisfied=False, target_bank=target_bank, target_name="primary",
                secondary_refresh=None, mode="patch",
            )
            bundle = SimpleNamespace(manifest=manifest, backup_dir=backup_dir)
            target = SimpleNamespace(acp_host="10.0.0.2", connection=SshConnection("root@10.0.0.2", "pw", "-o foo"))

            def run(params: dict[str, object]) -> CollectingSink:
                collector = CollectingSink()
                with mock.patch("timecapsulesmb.app.ops.flash.plan_flash_from_backup", return_value=(bundle, plan)):
                    with mock.patch("timecapsulesmb.app.ops.flash.load_request_config", return_value=object()):
                        with mock.patch("timecapsulesmb.app.ops.flash._resolve_flash_target", return_value=target):
                            with mock.patch("timecapsulesmb.app.ops.flash.validate_live_target_matches_backup") as validate_mock:
                                with mock.patch("timecapsulesmb.app.ops.flash.write_flash_plan") as write_mock:
                                    with mock.patch("timecapsulesmb.services.flash.reboot_device") as reboot_mock:
                                        rc = service.run_api_request(
                                            {"operation": "flash", "params": params},
                                            collector.sink,
                                        )
                                        collector.rc = rc  # type: ignore[attr-defined]
                                        collector.validate_mock = validate_mock  # type: ignore[attr-defined]
                                        collector.write_mock = write_mock  # type: ignore[attr-defined]
                                        collector.reboot_mock = reboot_mock  # type: ignore[attr-defined]
                return collector

            first = run({"action": "write", "backup_dir": str(backup_dir), "mode": "patch"})

            self.assertEqual(first.rc, 1)  # type: ignore[attr-defined]
            details = self.assert_confirmation(first, "flash.patch_write", {"host": "10.0.0.2", "mode": "patch"})
            first.validate_mock.assert_not_called()  # type: ignore[attr-defined]
            first.write_mock.assert_not_called()  # type: ignore[attr-defined]

            second = run({
                "action": "write",
                "backup_dir": str(backup_dir),
                "mode": "patch",
                "confirmation_id": details["confirmation_id"],
            })

        self.assertEqual(second.rc, 0)  # type: ignore[attr-defined]
        second.validate_mock.assert_called_once()  # type: ignore[attr-defined]
        second.write_mock.assert_called_once()  # type: ignore[attr-defined]
        second.reboot_mock.assert_not_called()  # type: ignore[attr-defined]
        payload = self.assert_single_terminal_event(second, "result")["payload"]
        self.assertEqual(payload["write_status"], "validated")
        self.assertTrue(payload["write_validated"])
        self.assertEqual(payload["post_write_action"], "manual_power_cycle")
        self.assertFalse(payload["reboot_requested"])

    def test_flash_secondary_restore_confirms_its_own_text_and_never_reboots(self) -> None:
        plan, _download, _restore = plan_full_restore(inspect_full_banks())
        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp)
            manifest = {
                "backup_dir": str(backup_dir),
                "write_outcome": {"status": "validated", "mode": "restore", "write_validated": True},
            }
            bundle = SimpleNamespace(manifest=manifest, backup_dir=backup_dir)
            target = SimpleNamespace(acp_host="10.0.0.2", connection=SshConnection("root@10.0.0.2", "pw", "-o foo"))
            # The app sends its restore default: reboot after the write.
            params = {"action": "write", "backup_dir": str(backup_dir), "mode": "restore", "reboot_after_write": True}

            def run(request_params: dict[str, object], planned=plan) -> CollectingSink:
                collector = CollectingSink()
                with mock.patch("timecapsulesmb.app.ops.flash.plan_flash_from_backup", return_value=(bundle, planned)):
                    with mock.patch("timecapsulesmb.app.ops.flash.load_request_config", return_value=object()):
                        with mock.patch("timecapsulesmb.app.ops.flash._resolve_flash_target", return_value=target):
                            with mock.patch("timecapsulesmb.app.ops.flash.validate_live_target_matches_backup"):
                                with mock.patch("timecapsulesmb.app.ops.flash.write_flash_plan") as write_mock:
                                    with mock.patch("timecapsulesmb.services.flash.reboot_device") as reboot_mock:
                                        collector.rc = service.run_api_request(  # type: ignore[attr-defined]
                                            {"operation": "flash", "params": request_params}, collector.sink,
                                        )
                                        collector.write_mock = write_mock  # type: ignore[attr-defined]
                                        collector.reboot_mock = reboot_mock  # type: ignore[attr-defined]
                return collector

            first = run(params)
            details = self.assert_confirmation(
                first,
                "flash.restore_secondary_write",
                {"host": "10.0.0.2", "target_bank": "secondary", "reboot_after_write": False},
            )
            self.assertIn("backup (secondary) firmware bank on 10.0.0.2 is invalid", details["message"])
            first.write_mock.assert_not_called()  # type: ignore[attr-defined]
            # The confirmation approves this image: a plan that would write
            # another one (a newer firmware download, say) needs its own.
            assert plan.secondary_refresh is not None
            other_image = replace(plan, secondary_refresh=replace(plan.secondary_refresh, image=b"\x00" * 16))
            stale = run({**params, "confirmation_id": details["confirmation_id"]}, planned=other_image)
            self.assert_confirmation(stale, "flash.restore_secondary_write")
            stale.write_mock.assert_not_called()  # type: ignore[attr-defined]
            second = run({**params, "confirmation_id": details["confirmation_id"]})

        self.assertEqual(second.rc, 0)  # type: ignore[attr-defined]
        second.write_mock.assert_called_once()  # type: ignore[attr-defined]
        second.reboot_mock.assert_not_called()  # type: ignore[attr-defined]
        payload = self.assert_single_terminal_event(second, "result")["payload"]
        self.assertEqual(payload["post_write_action"], "none")
        self.assertFalse(payload["reboot_requested"])
        self.assertEqual(payload["summary_key"], "flash_restore_secondary_write_validated")

    def test_flash_patch_refused_for_a_bad_secondary_points_the_app_at_restore(self) -> None:
        collector = CollectingSink()
        refusal = SecondaryBankInvalidError("refusing to patch primary because the secondary (backup) firmware bank ...")
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("timecapsulesmb.app.ops.flash.plan_flash_from_backup", side_effect=refusal):
                service.run_api_request(
                    {"operation": "flash", "params": {"action": "plan", "backup_dir": tmp, "mode": "patch"}},
                    collector.sink,
                )

        error = self.assert_single_terminal_event(collector, "error")
        self.assertEqual(error["code"], "secondary_bank_invalid")
        self.assertEqual(error["recovery"]["localization_key"], "flash.secondary_bank_invalid")
        self.assertEqual(error["recovery"]["title"], "Backup firmware bank is damaged")

    def test_flash_secondary_read_disagreement_asks_the_app_for_a_fresh_backup(self) -> None:
        # Our read finds the secondary damaged, ACPd's own read finds it intact.
        refusal = SecondaryBankReadMismatchError(
            "refusing to rewrite the secondary bank because the secondary (backup) firmware bank read differently ..."
        )
        for action, mode in (("plan", "restore"), ("plan", "patch"), ("write", "restore")):
            with self.subTest(action=action, mode=mode):
                collector = CollectingSink()
                with tempfile.TemporaryDirectory() as tmp:
                    with mock.patch("timecapsulesmb.app.ops.flash.plan_flash_from_backup", side_effect=refusal):
                        with mock.patch("timecapsulesmb.app.ops.flash.write_flash_plan") as write_mock:
                            service.run_api_request(
                                {"operation": "flash", "params": {"action": action, "backup_dir": tmp, "mode": mode}},
                                collector.sink,
                            )
                error = self.assert_single_terminal_event(collector, "error")
                self.assertEqual(error["code"], "secondary_bank_read_mismatch")
                self.assertEqual(error["recovery"]["localization_key"], "flash.secondary_bank_read_mismatch")
                self.assertEqual(error["recovery"]["title"], "Backup firmware bank read inconsistently")
                write_mock.assert_not_called()

    def test_flash_write_payload_restore_summary_mentions_manual_reboot_without_reboot_request(self) -> None:
        payload = contracts.flash_write_payload({
            "backup_dir": "/tmp/flash-backup",
            "write_outcome": {
                "status": "validated",
                "mode": "restore",
                "write_validated": True,
                "post_write_action": "manual_reboot",
            },
        })

        self.assertEqual(payload["summary"], "Flash restore write validated; manual reboot required.")

    def test_flash_restore_write_defaults_to_reboot_and_wait(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp)
            manifest = {
                "backup_dir": str(backup_dir),
                "write_outcome": {
                    "status": "validated",
                    "mode": "restore",
                    "write_validated": True,
                    "write_may_have_modified_device": True,
                },
            }
            target_bank = SimpleNamespace(name="primary", sha256="bank-sha")
            plan = SimpleNamespace(
                already_satisfied=False, target_bank=target_bank, target_name="primary",
                secondary_refresh=None, mode="restore",
            )
            bundle = SimpleNamespace(manifest=manifest, backup_dir=backup_dir)
            connection = SshConnection("root@10.0.0.2", "pw", "-o foo")
            target = SimpleNamespace(acp_host="10.0.0.2", connection=connection)
            confirmation_collector = CollectingSink()
            collector = CollectingSink()
            params = {
                "action": "write",
                "backup_dir": str(backup_dir),
                "mode": "restore",
            }
            with mock.patch("timecapsulesmb.app.ops.flash.plan_flash_from_backup", return_value=(bundle, plan)):
                with mock.patch("timecapsulesmb.app.ops.flash.load_request_config", return_value=object()):
                    with mock.patch("timecapsulesmb.app.ops.flash._resolve_flash_target", return_value=target):
                        rc = service.run_api_request(
                            {
                                "operation": "flash",
                                "params": params,
                            },
                            confirmation_collector.sink,
                        )

            self.assertEqual(rc, 1)
            details = self.assert_confirmation(
                confirmation_collector,
                "flash.restore_write",
                {"host": "10.0.0.2", "mode": "restore", "target_bank": "primary"},
            )
            self.assertEqual(
                details["message"],
                "Restore Apple stock firmware to the primary firmware bank on 10.0.0.2 and reboot after validation?",
            )
            params["confirmation_id"] = self.confirmation_id_for(
                "flash",
                params,
                {
                    "host": "10.0.0.2",
                    "backup_dir": str(backup_dir),
                    "mode": "restore",
                    "target_bank": "primary",
                    "target_sha256": "bank-sha",
                    "reboot_after_write": True,
                    "wait_after_reboot": True,
                },
            )

            with mock.patch("timecapsulesmb.app.ops.flash.plan_flash_from_backup", return_value=(bundle, plan)):
                with mock.patch("timecapsulesmb.app.ops.flash.load_request_config", return_value=object()):
                    with mock.patch("timecapsulesmb.app.ops.flash._resolve_flash_target", return_value=target):
                        with mock.patch("timecapsulesmb.app.ops.flash.validate_live_target_matches_backup"):
                            with mock.patch("timecapsulesmb.app.ops.flash.write_flash_plan"):
                                with mock.patch("timecapsulesmb.services.flash.reboot_device") as reboot_wait:
                                    rc = service.run_api_request(
                                        {
                                            "operation": "flash",
                                            "params": params,
                                        },
                                        collector.sink,
                                    )

        self.assertEqual(rc, 0)
        reboot_wait.assert_called_once()
        self.assertEqual(reboot_wait.call_args.args, ("root@10.0.0.2", "pw"))
        self.assertIs(reboot_wait.call_args.kwargs["wait"], True)
        payload = self.assert_single_terminal_event(collector, "result")["payload"]
        self.assertEqual(payload["post_write_action"], "ssh_reboot")
        self.assertTrue(payload["reboot_requested"])
        self.assertTrue(payload["rebooted"])
        self.assertTrue(payload["waited_after_reboot"])
        self.assertEqual(payload["summary"], "Flash restore write validated; device rebooted.")

    def test_flash_restore_write_reports_a_reboot_that_never_started(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp)
            manifest = {
                "backup_dir": str(backup_dir),
                "write_outcome": {"status": "validated", "mode": "restore", "write_validated": True},
            }
            plan = SimpleNamespace(
                already_satisfied=False, target_bank=SimpleNamespace(name="primary", sha256="bank-sha"),
                target_name="primary", secondary_refresh=None, mode="restore",
            )
            bundle = SimpleNamespace(manifest=manifest, backup_dir=backup_dir)
            target = SimpleNamespace(acp_host="10.0.0.2", connection=SshConnection("root@10.0.0.2", "pw", "-o foo"))
            params: dict[str, object] = {"action": "write", "backup_dir": str(backup_dir), "mode": "restore"}
            params["confirmation_id"] = self.confirmation_id_for(
                "flash",
                params,
                {
                    "host": "10.0.0.2",
                    "backup_dir": str(backup_dir),
                    "mode": "restore",
                    "target_bank": "primary",
                    "target_sha256": "bank-sha",
                    "reboot_after_write": True,
                    "wait_after_reboot": True,
                },
            )
            collector = CollectingSink()
            failure = RebootFlowError("the device did not restart", "reboot_not_started")
            with mock.patch("timecapsulesmb.app.ops.flash.plan_flash_from_backup", return_value=(bundle, plan)):
                with mock.patch("timecapsulesmb.app.ops.flash.load_request_config", return_value=object()):
                    with mock.patch("timecapsulesmb.app.ops.flash._resolve_flash_target", return_value=target):
                        with mock.patch("timecapsulesmb.app.ops.flash.validate_live_target_matches_backup"):
                            with mock.patch("timecapsulesmb.app.ops.flash.write_flash_plan"):
                                with mock.patch("timecapsulesmb.services.flash.reboot_device", side_effect=failure):
                                    rc = service.run_api_request({"operation": "flash", "params": params}, collector.sink)
            saved = json.loads((backup_dir / "manifest.json").read_text())

        self.assertEqual(rc, 1)
        error = self.assert_single_terminal_event(collector, "error")
        self.assertEqual(error["code"], "reboot_not_started")
        self.assertEqual(error["message"], "the device did not restart")
        self.assertEqual(error["recovery"]["localization_key"], "reboot_not_started")
        self.assertEqual(saved["write_outcome"]["post_write_action"], "ssh_reboot")
        self.assertTrue(saved["write_outcome"]["reboot_requested"])
        self.assertFalse(saved["write_outcome"]["rebooted"])

    def test_flash_patch_write_rejects_reboot_request(self) -> None:
        collector = CollectingSink()

        with mock.patch("timecapsulesmb.app.ops.flash.plan_flash_from_backup") as plan_flash:
            rc = service.run_api_request(
                {
                    "operation": "flash",
                    "params": {
                        "action": "write",
                        "backup_dir": "/tmp/flash-backup",
                        "mode": "patch",
                        "reboot_after_write": True,
                    },
                },
                collector.sink,
            )

        self.assertEqual(rc, 1)
        plan_flash.assert_not_called()
        error = self.assert_single_terminal_event(collector, "error")
        self.assertEqual(error["code"], "validation_failed")
        self.assertIn("Flash patch cannot request reboot", error["message"])

    def test_set_telemetry_operation_updates_bootstrap_preference(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bootstrap_path = Path(tmp) / ".bootstrap"
            app_paths = SimpleNamespace(bootstrap_path=bootstrap_path)
            collector = CollectingSink()

            with mock.patch("timecapsulesmb.app.ops.readiness.resolve_app_paths", return_value=app_paths):
                rc = service.run_api_request(
                    {"operation": "set-telemetry", "params": {"enabled": False}},
                    collector.sink,
                )

            self.assertEqual(rc, 0)
            stages = collector.events_of_type("stage")
            self.assertEqual([stage["stage"] for stage in stages], ["resolve_paths", "write_bootstrap"])
            payload = self.assert_single_terminal_event(collector, "result")["payload"]
            self.assertFalse(payload["telemetry_enabled"])
            self.assertEqual(payload["bootstrap_path"], str(bootstrap_path))
            self.assertIn("TELEMETRY=false", bootstrap_path.read_text())

    def test_telemetry_identity_operation_is_not_exposed(self) -> None:
        collector = CollectingSink()

        rc = service.run_api_request({"operation": "telemetry-identity", "params": {}}, collector.sink)

        self.assertEqual(rc, 1)
        error = self.assert_single_terminal_event(collector, "error")
        self.assertEqual(error["code"], "unknown_operation")

    def test_version_check_operation_returns_structured_update_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app_paths = SimpleNamespace(version_check_cache_path=Path(tmp) / "version-cache.json")
            collector = CollectingSink()
            result = VersionCheckResult(
                should_block=True,
                checked_url="https://example.invalid/version.json",
                message="Please update.",
                download_url="https://example.invalid/download",
                local_version_code=20000,
                current_version=20005,
                min_supported_version=20005,
                latest_tag="v2.0.5",
                source="network",
            )

            with mock.patch("timecapsulesmb.app.ops.readiness.resolve_app_paths", return_value=app_paths):
                with mock.patch("timecapsulesmb.app.ops.readiness.check_client_version", return_value=result) as check:
                    rc = service.run_api_request(
                        {
                            "operation": "version-check",
                            "params": {"url": "https://example.invalid/version.json"},
                        },
                        collector.sink,
                    )

            self.assertEqual(rc, 0)
            check.assert_called_once_with(
                url="https://example.invalid/version.json",
                cache_path=app_paths.version_check_cache_path,
            )
            payload = self.assert_single_terminal_event(collector, "result")["payload"]
            self.assertTrue(payload["should_block"])
            self.assertTrue(payload["update_available"])
            self.assertEqual(payload["current_version"], 20005)
            self.assertEqual(payload["latest_tag"], "v2.0.5")
            self.assertEqual(payload["source"], "network")
            self.assertEqual(payload["summary"], "Update required.")

    def test_version_check_payload_reports_optional_update(self) -> None:
        payload = contracts.version_check_payload(
            VersionCheckResult(
                should_block=False,
                local_version_code=20000,
                current_version=20005,
                min_supported_version=19999,
                source="network",
            )
        )

        self.assertFalse(payload["should_block"])
        self.assertTrue(payload["update_available"])
        self.assertEqual(payload["summary"], "Update available.")

    def test_version_check_payload_reports_current_when_versions_match(self) -> None:
        payload = contracts.version_check_payload(
            VersionCheckResult(
                should_block=False,
                local_version_code=20005,
                current_version=20005,
                min_supported_version=19999,
                source="network",
            )
        )

        self.assertFalse(payload["should_block"])
        self.assertFalse(payload["update_available"])
        self.assertEqual(payload["summary"], "TimeCapsuleSMB is up to date.")

    def test_version_check_payload_preserves_unavailable_summary(self) -> None:
        payload = contracts.version_check_payload(
            VersionCheckResult(
                should_block=False,
                local_version_code=20000,
                current_version=None,
                min_supported_version=None,
                source="unavailable",
            )
        )

        self.assertFalse(payload["should_block"])
        self.assertFalse(payload["update_available"])
        self.assertEqual(payload["summary"], "Version metadata is unavailable.")

    def test_version_check_operation_rejects_non_http_url(self) -> None:
        collector = CollectingSink()

        rc = service.run_api_request(
            {"operation": "version-check", "params": {"url": "file:///tmp/version.json"}},
            collector.sink,
        )

        self.assertEqual(rc, 1)
        error = self.assert_single_terminal_event(collector, "error")
        self.assertEqual(error["code"], "validation_failed")

    def test_missing_params_defaults_to_empty_object(self) -> None:
        collector = CollectingSink()

        rc = service.run_api_request({"operation": "capabilities"}, collector.sink)

        self.assertEqual(rc, 0)
        result = self.assert_single_terminal_event(collector, "result")
        self.assertEqual(result["operation"], "capabilities")

    def test_missing_operation_emits_invalid_request_error(self) -> None:
        collector = CollectingSink()

        rc = service.run_api_request({"params": {}}, collector.sink)

        self.assertEqual(rc, 1)
        error = self.assert_single_terminal_event(collector, "error")
        self.assertEqual(error["operation"], "api")
        self.assertEqual(error["code"], "invalid_request")
        self.assertEqual(error["recovery"]["title"], "Invalid request")
        self.assertTrue(error["recovery"]["retryable"])

    def test_unknown_and_retired_operations_emit_error_without_result(self) -> None:
        for operation in ("nope", "repair-xattrs"):
            with self.subTest(operation=operation):
                collector = CollectingSink()

                rc = service.run_api_request({"operation": operation, "params": {}}, collector.sink)

                self.assertEqual(rc, 1)
                error = self.assert_single_terminal_event(collector, "error")
                self.assertEqual(error["code"], "unknown_operation")
                self.assertEqual(error["recovery"]["title"], "Unknown operation")

    def run_with_api_telemetry(self, request: dict[str, object], handlers: dict[str, object]) -> tuple[int, CollectingSink]:
        collector = CollectingSink()
        with mock.patch.dict(service.OPERATIONS, handlers):
            with mock.patch("timecapsulesmb.app.service.resolve_app_paths", return_value=SimpleNamespace(bootstrap_path=Path("/tmp/bootstrap"))):
                with mock.patch("timecapsulesmb.app.service.ensure_install_id"):
                    with mock.patch("timecapsulesmb.app.service.load_optional_env_config", return_value=AppConfig.from_values({})):
                        rc = service.run_api_request(request, collector.sink)
        return rc, collector

    def test_misspelled_param_is_rejected_before_the_operation_runs_and_recorded(self) -> None:
        handler = mock.Mock()

        rc, collector = self.run_with_api_telemetry(
            {"operation": "fsck", "params": {"no_reboot": True, "no_wiat": True, "volumee": "dk2"}},
            {"fsck": handler},
        )

        self.assertEqual(rc, 1)
        handler.assert_not_called()
        error = self.assert_single_terminal_event(collector, "error")
        self.assertEqual(error["operation"], "fsck")
        self.assertEqual(error["code"], "unknown_param")
        self.assertEqual(error["message"], "unknown parameters for fsck: no_wiat, volumee")
        self.assertIn("no_wait", error["debug"]["accepted_params"])
        self.assertIn("config", error["debug"]["accepted_params"])
        self.assertEqual(error["recovery"]["title"], "Unknown parameter")
        self.assertFalse(error["recovery"]["retryable"])
        self.assertIn("If Helper path is set in Settings, clear it.", error["recovery"]["actions"])
        # A started and a finished event, like any other failed operation.
        self.assertEqual([call.kwargs["phase"] for call in self._telemetry_client.emit.call_args_list], ["started", "finished"])
        finished = self._telemetry_client.emit.call_args_list[1].kwargs
        self.assertEqual(finished["operation"], "fsck")
        self.assertEqual(finished["result"], "failure")
        self.assertEqual(finished["error"], "unknown parameters for fsck: no_wiat, volumee")
        self.assertEqual(finished["details"], {"unknown_params": ["no_wiat", "volumee"]})

    def test_finished_telemetry_carries_the_error_code_the_app_was_sent(self) -> None:
        def raising(exc: BaseException):
            def handler(_params, context):
                context.stage("run_fsck")
                raise exc
            return handler

        cases = (
            ("operation error", raising(service.AppOperationError("Disk did not mount.", code="deploy_disk_not_mounted")),
             {"volume": "Data"}, "deploy_disk_not_mounted", "failure"),
            ("config error", raising(ConfigError("TC_HOST is missing.")), {"volume": "Data"}, "config_error", "failure"),
            ("transport error", raising(SshError("Connection reset by 10.0.0.2 port 22")), {"volume": "Data"},
             "remote_error", "failure"),
            ("cancel", raising(KeyboardInterrupt()), {"volume": "Data"}, "cancelled", "cancelled"),
            ("system exit", raising(SystemExit("fsck stopped early")), {"volume": "Data"}, "operation_failed", "failure"),
            ("unexpected error", raising(RuntimeError("boom")), {"volume": "Data"}, "operation_failed", "failure"),
            ("unknown param", mock.Mock(), {"volumee": "Data"}, "unknown_param", "failure"),
        )
        for label, handler, params, code, result in cases:
            with self.subTest(label):
                self._telemetry_client.emit.reset_mock()
                _rc, collector = self.run_with_api_telemetry({"operation": "fsck", "params": params}, {"fsck": handler})

                error = self.assert_single_terminal_event(collector, "error")
                finished = self._telemetry_client.emit.call_args_list[-1].kwargs
                self.assertEqual(error["code"], code)
                self.assertEqual(finished["result"], result)
                self.assertEqual(finished["error_code"], code)

    def test_finished_telemetry_has_no_error_code_without_an_error_event(self) -> None:
        for ok in (True, False):
            with self.subTest(ok=ok):
                self._telemetry_client.emit.reset_mock()
                rc, collector = self.run_with_api_telemetry(
                    {"operation": "fsck", "params": {"volume": "Data"}},
                    {"fsck": lambda _params, _context, ok=ok: service.OperationResult(ok, {"error": "fsck status 8"})},
                )

                self.assertEqual(rc, 0 if ok else 1)
                self.assert_single_terminal_event(collector, "result")
                self.assertIsNone(self._telemetry_client.emit.call_args_list[-1].kwargs.get("error_code"))

    def test_rejected_param_values_never_reach_telemetry(self) -> None:
        handler = mock.Mock()

        rc, _collector = self.run_with_api_telemetry(
            {"operation": "uninstall", "params": {"no_reboot": True, "credentails": {"password": "hunter2"}}},
            {"uninstall": handler},
        )

        self.assertEqual(rc, 1)
        handler.assert_not_called()
        self.assertEqual(self._telemetry_client.emit.call_count, 2)
        finished = self._telemetry_client.emit.call_args_list[1].kwargs
        self.assertEqual(finished["details"], {"unknown_params": ["credentails"]})
        self.assertNotIn("hunter2", repr(self._telemetry_client.emit.call_args_list))

    def test_rejected_request_for_an_operation_without_telemetry_records_nothing(self) -> None:
        for request in (
            {"operation": "reachability", "params": {"ssh_hots": "10.0.0.2"}},
            {"operation": "set-ssh", "params": {"action": "status", "no_wiat": True}},
        ):
            with self.subTest(operation=request["operation"]):
                self._telemetry_factory.reset_mock()
                self._telemetry_client.reset_mock()
                handler = mock.Mock()

                rc, collector = self.run_with_api_telemetry(request, {str(request["operation"]): handler})

                self.assertEqual(rc, 1)
                handler.assert_not_called()
                self.assertEqual(self.assert_single_terminal_event(collector, "error")["code"], "unknown_param")
                self._telemetry_factory.assert_not_called()
                self._telemetry_client.emit.assert_not_called()

    def test_operation_without_own_params_still_takes_the_shared_request_params(self) -> None:
        collector = CollectingSink()
        handler = mock.Mock(return_value=service.OperationResult(True, {"summary": "ok"}))
        params = {
            "config": "/tmp/tcapsule.env",
            "credentials": {"password": "pw"},
            "password": "pw",
            "confirmation_id": "abc",
            "confirmation": {"id": "abc"},
        }

        with mock.patch.dict(service.OPERATIONS, {"validate-install": handler}):
            rc = service.run_api_request({"operation": "validate-install", "params": params}, collector.sink)

        self.assertEqual(rc, 0)
        handler.assert_called_once()
        self.assertEqual(handler.call_args.args[0], params)

    def test_operation_rejects_a_param_only_another_operation_takes(self) -> None:
        collector = CollectingSink()
        handler = mock.Mock()

        with mock.patch.dict(service.OPERATIONS, {"uninstall": handler}):
            rc = service.run_api_request({"operation": "uninstall", "params": {"volume": "dk2"}}, collector.sink)

        self.assertEqual(rc, 1)
        handler.assert_not_called()
        error = self.assert_single_terminal_event(collector, "error")
        self.assertEqual(error["code"], "unknown_param")
        self.assertEqual(error["message"], "unknown parameter for uninstall: volume")

    def run_with_recorded_keep_awake(self, operation: str, handler) -> tuple[int, list[str]]:
        """Run one operation; the timeline interleaves keep-awake and sink events."""
        timeline: list[str] = []

        @contextmanager
        def recording_keep_awake():
            timeline.append("awake")
            try:
                yield
            finally:
                timeline.append("released")

        def recording_handler(params, context):
            timeline.append("handler")
            return handler(params, context)

        sink = EventSink(lambda event: timeline.append(str(event.to_jsonable()["type"])))
        with mock.patch.dict(service.OPERATIONS, {operation: recording_handler}):
            with mock.patch("timecapsulesmb.app.service.keep_system_awake", recording_keep_awake):
                with mock.patch("timecapsulesmb.app.service.resolve_app_paths", return_value=SimpleNamespace(bootstrap_path=Path("/tmp/bootstrap"))):
                    with mock.patch("timecapsulesmb.app.service.ensure_install_id"):
                        with mock.patch("timecapsulesmb.app.service.load_optional_env_config", return_value=AppConfig.from_values({})):
                            rc = service.run_api_request({"operation": operation, "params": {}}, sink)
        return rc, timeline

    def test_long_device_operation_keeps_the_mac_awake_only_while_its_handler_runs(self) -> None:
        rc, timeline = self.run_with_recorded_keep_awake(
            "deploy",
            lambda _params, _context: service.OperationResult(True, {"summary": "ok"}),
        )

        self.assertEqual(rc, 0)
        self.assertEqual(timeline, ["awake", "handler", "released", "result"])

    def test_quick_operation_does_not_keep_the_mac_awake(self) -> None:
        rc, timeline = self.run_with_recorded_keep_awake(
            "discover",
            lambda _params, _context: service.OperationResult(True, {"summary": "ok"}),
        )

        self.assertEqual(rc, 0)
        self.assertEqual(timeline, ["handler", "result"])

    def test_keep_awake_is_released_before_a_confirmation_request_is_reported(self) -> None:
        def ask_for_confirmation(params, _context):
            raise service.AppConfirmationRequired(build_confirmation(
                operation="deploy",
                params=params,
                title="Confirm deploy",
                message="Deploy and reboot?",
                action_title="Deploy",
                risk="reboot",
                summary="Deploy",
                context={},
                presentation_id="deploy.reboot",
            ))

        rc, timeline = self.run_with_recorded_keep_awake("deploy", ask_for_confirmation)

        self.assertEqual(rc, 1)
        self.assertEqual(timeline, ["awake", "handler", "released", "error"])

    def test_keep_awake_is_released_before_an_unexpected_failure_is_reported(self) -> None:
        def crash(_params, _context):
            raise RuntimeError("connection reset")

        rc, timeline = self.run_with_recorded_keep_awake("deploy", crash)

        self.assertEqual(rc, 1)
        self.assertEqual(timeline, ["awake", "handler", "released", "error"])

    def test_request_param_fixture_matches_the_operation_specs(self) -> None:
        from tests.fixtures import operation_params

        self.assertEqual(operation_params.FIXTURE_PATH.read_text(), operation_params.render())

    def test_non_object_params_emits_invalid_request_error(self) -> None:
        collector = CollectingSink()

        rc = service.run_api_request({"operation": "capabilities", "params": []}, collector.sink)

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

                def fail(_params, _context, exc=exception):
                    raise exc

                with mock.patch.dict(service.OPERATIONS, {operation: fail}):
                    rc = service.run_api_request({"operation": operation, "params": {}}, collector.sink)

                self.assertEqual(rc, 1)
                error = self.assert_single_terminal_event(collector, "error")
                self.assertEqual(error["code"], code)
                self.assertIn("recovery", error)

    def test_dispatcher_maps_ssh_timeout_to_slow_device_recovery_without_rewriting_telemetry(self) -> None:
        collector = CollectingSink()
        timeout = "Timed out waiting for ssh command to finish: /bin/sh -c 'wc -c < /mnt/Flash/.manager.sh.tmp'"

        def fail(_params, context):
            context.stage("upload_boot_files")
            context.update_fields(device_model="TimeCapsule6,113", device_syap="113")
            raise SshCommandTimeout(timeout)

        with mock.patch.dict(service.OPERATIONS, {"deploy": fail}):
            rc = service.run_api_request({"operation": "deploy", "params": {}}, collector.sink)

        self.assertEqual(rc, 1)
        error = self.assert_single_terminal_event(collector, "error")
        self.assertEqual(error["code"], "remote_error")
        self.assertEqual(error["message"], timeout)
        self.assertEqual(error["recovery"]["title"], "Device is responding very slowly")
        expected_message = ssh_timeout_slow_device_message("Time Capsule 3rd generation")
        self.assertEqual(error["recovery"]["message"], expected_message)
        self.assertEqual(error["recovery"]["localization_key"], "remote_error.ssh_timeout_slow_device")
        self.assertEqual(error["recovery"]["localization_values"], {"device_name": "Time Capsule 3rd generation"})
        self.assertEqual(error["recovery"]["action_ids"], ["run_checkup"])
        telemetry_error = self._telemetry_client.emit.call_args_list[-1].kwargs["error"]
        self.assertIn(timeout, telemetry_error)
        self.assertIn("stage=upload_boot_files", telemetry_error)
        self._telemetry_urlopen.assert_not_called()

    def test_dispatcher_gives_a_connection_this_mac_dropped_its_own_code_over_the_stages_advice(self) -> None:
        collector = CollectingSink()
        message = f"{LOCAL_NETWORK_FILTERED_MESSAGE} (ssh: connect to host 10.0.0.2 port 22: Bad file descriptor)"

        def fail(_params, context):
            # This stage has its own remote_error advice, about the device.
            context.stage("verify_runtime_reboot")
            raise SshLocalNetworkFilteredError(message)

        with mock.patch.dict(service.OPERATIONS, {"deploy": fail}):
            rc = service.run_api_request({"operation": "deploy", "params": {}}, collector.sink)

        self.assertEqual(rc, 1)
        error = self.assert_single_terminal_event(collector, "error")
        self.assertEqual((error["code"], error["message"]), ("local_network_filtered", message))
        self.assert_local_network_filtered_recovery(error)
        finished = self._telemetry_client.emit.call_args_list[-1].kwargs
        self.assertEqual(finished["error_code"], "local_network_filtered")
        self.assertIn("Bad file descriptor", finished["error"])

    def assert_local_network_filtered_recovery(self, error: dict[str, object]) -> None:
        recovery = error["recovery"]
        self.assertEqual(recovery["localization_key"], "local_network_filtered")
        self.assertEqual(recovery["title"], "Connection blocked on this Mac")
        self.assertEqual(recovery["message"], LOCAL_NETWORK_FILTERED_MESSAGE)
        # No step: the filtering app may be a VPN the user cannot turn off.
        self.assertEqual((recovery["actions"], recovery["action_ids"]), ([], []))
        self.assertTrue(recovery["retryable"])

    def test_dispatcher_includes_traceback_for_unexpected_errors(self) -> None:
        collector = CollectingSink()

        def fail(_params, _context):
            raise RuntimeError("boom")

        with mock.patch.dict(service.OPERATIONS, {"boom": fail}):
            rc = service.run_api_request({"operation": "boom", "params": {}}, collector.sink)

        self.assertEqual(rc, 1)
        error = self.assert_single_terminal_event(collector, "error")
        self.assertEqual(error["code"], "operation_failed")
        self.assertIn("Traceback", error["debug"]["traceback"])
        self.assertIn("RuntimeError: boom", error["debug"]["traceback"])

    def test_dispatcher_failure_event_includes_collected_redacted_debug_without_telemetry(self) -> None:
        collector = CollectingSink()

        def fail(_params, context):
            context.stage("verify_runtime")
            context.config = AppConfig.from_values({"TC_PASSWORD": "known-secret"})
            context.connection = SshConnection("root@10.0.0.2", "known-secret", "")
            context.add_debug_fields(remote_manager_log_tail="login failed with known-secret")
            context.record_execution_measurement("probe", detail="retried known-secret")
            raise AppOperationError(
                "Runtime verification failed.",
                code="remote_error",
                debug={"cause": "command rejected known-secret"},
            )

        with mock.patch.dict(service.OPERATIONS, {"deploy": fail}):
            with mock.patch("timecapsulesmb.app.service._should_emit_api_telemetry", return_value=False):
                rc = service.run_api_request({"operation": "deploy", "params": {}}, collector.sink)

        self.assertEqual(rc, 1)
        self._telemetry_factory.assert_not_called()
        error = self.assert_single_terminal_event(collector, "error")
        self.assertEqual(error["message"], "Runtime verification failed.")
        self.assertEqual(error["code"], "remote_error")
        self.assertEqual(error["debug"]["stage"], "verify_runtime")
        self.assertIn("<redacted>", error["debug"]["cause"])
        self.assertIn("<redacted>", error["debug"]["remote_manager_log_tail"])
        self.assertIn("execution", error["debug"])
        self.assertNotIn("known-secret", json.dumps(error["debug"]))

    def test_dispatcher_unsuccessful_result_includes_collected_redacted_debug(self) -> None:
        collector = CollectingSink()

        def fail(_params, context):
            context.stage("run_fsck")
            context.config = AppConfig.from_values({"TC_PASSWORD": "known-secret"})
            context.add_debug_fields(fsck_log="device rejected known-secret")
            return service.OperationResult(False, {"error": "Disk repair exited with fsck status 8"})

        with mock.patch.dict(service.OPERATIONS, {"fsck": fail}):
            with mock.patch("timecapsulesmb.app.service._should_emit_api_telemetry", return_value=False):
                rc = service.run_api_request({"operation": "fsck", "params": {}}, collector.sink)

        self.assertEqual(rc, 1)
        result = self.assert_single_terminal_event(collector, "result")
        self.assertFalse(result["ok"])
        self.assertEqual(result["debug"]["stage"], "run_fsck")
        self.assertIn("<redacted>", result["debug"]["fsck_log"])
        self.assertIn("execution", result["debug"])
        self.assertNotIn("known-secret", json.dumps(result["debug"]))

    def test_dispatcher_emits_api_operation_telemetry(self) -> None:
        collector = CollectingSink()

        def run_fsck(params, context):
            context.stage("run_fsck")
            return service.OperationResult(True, {
                "device": "/dev/dk2",
                "mountpoint": "/Volumes/Data",
                "returncode": 0,
                "reboot_requested": True,
                "waited": True,
                "verified": True,
            })

        with mock.patch.dict(service.OPERATIONS, {"fsck": run_fsck}):
            with mock.patch.dict(os.environ, {"TCAPSULE_CLIENT": "macos_gui"}, clear=False):
                with mock.patch("timecapsulesmb.app.service.resolve_app_paths", return_value=SimpleNamespace(bootstrap_path=Path("/tmp/bootstrap"))):
                    with mock.patch("timecapsulesmb.app.service.ensure_install_id"):
                        with mock.patch("timecapsulesmb.app.service.load_optional_env_config", return_value=AppConfig.from_values({})):
                            rc = service.run_api_request(
                                {
                                    "operation": "fsck",
                                    "params": {
                                        "volume": "Data",
                                        "dry_run": False,
                                        "no_reboot": False,
                                        "no_wait": False,
                                        "mount_wait": 30,
                                    },
                                },
                                collector.sink,
                            )

        self.assertEqual(rc, 0)
        self.assertEqual(self._telemetry_client.emit.call_count, 2)
        started = self._telemetry_client.emit.call_args_list[0]
        finished = self._telemetry_client.emit.call_args_list[1]
        self.assertEqual(started.args, ("fsck_started",))
        self.assertEqual(started.kwargs["operation"], "fsck")
        self.assertEqual(started.kwargs["phase"], "started")
        self.assertEqual(started.kwargs["entrypoint"], "api")
        self.assertEqual(started.kwargs["client"], "macos_gui")
        self.assertEqual(started.kwargs["options"], {
            "dry_run": False,
            "mount_wait": 30,
            "no_reboot": False,
            "no_wait": False,
        })
        self.assertEqual(finished.args, ("fsck_finished",))
        self.assertEqual(finished.kwargs["phase"], "finished")
        self.assertEqual(finished.kwargs["operation_id"], started.kwargs["operation_id"])
        self.assertEqual(finished.kwargs["result"], "success")
        self.assertEqual(finished.kwargs["stage"], "run_fsck")
        self.assertEqual(finished.kwargs["risk"], "destructive")
        self.assertEqual(finished.kwargs["details"]["volume"], "Data")
        self.assertEqual(finished.kwargs["details"]["fsck_device"], "/dev/dk2")
        self.assertEqual(finished.kwargs["details"]["fsck_mountpoint"], "/Volumes/Data")
        self.assertEqual(finished.kwargs["details"]["returncode"], 0)
        self.assertTrue(finished.kwargs["details"]["reboot_requested"])
        self.assertTrue(finished.kwargs["details"]["waited"])
        self.assertTrue(finished.kwargs["details"]["verified"])

    def test_dispatcher_defaults_api_telemetry_client_when_environment_is_unset(self) -> None:
        collector = CollectingSink()

        def run_fsck(_params, context):
            context.stage("run_fsck")
            return service.OperationResult(True, {"returncode": 0, "summary": "Disk repair completed with fsck."})

        with mock.patch.dict(service.OPERATIONS, {"fsck": run_fsck}):
            with mock.patch.dict(os.environ, {"TCAPSULE_CLIENT": ""}, clear=False):
                with mock.patch("timecapsulesmb.app.service.resolve_app_paths", return_value=SimpleNamespace(bootstrap_path=Path("/tmp/bootstrap"))):
                    with mock.patch("timecapsulesmb.app.service.ensure_install_id"):
                        with mock.patch("timecapsulesmb.app.service.load_optional_env_config", return_value=AppConfig.from_values({})):
                            rc = service.run_api_request({"operation": "fsck", "params": {}}, collector.sink)

        self.assertEqual(rc, 0)
        started = self._telemetry_client.emit.call_args_list[0]
        self.assertEqual(started.kwargs["entrypoint"], "api")
        self.assertEqual(started.kwargs["client"], "api")

    def test_dispatcher_emits_cancelled_telemetry_on_keyboard_interrupt(self) -> None:
        collector = CollectingSink()

        def run_fsck(_params, context):
            context.stage("run_fsck")
            raise KeyboardInterrupt

        with mock.patch.dict(service.OPERATIONS, {"fsck": run_fsck}):
            with mock.patch("timecapsulesmb.app.service.resolve_app_paths", return_value=SimpleNamespace(bootstrap_path=Path("/tmp/bootstrap"))):
                with mock.patch("timecapsulesmb.app.service.ensure_install_id"):
                    with mock.patch("timecapsulesmb.app.service.load_optional_env_config", return_value=AppConfig.from_values({})):
                        rc = service.run_api_request({"operation": "fsck", "params": {}}, collector.sink)

        self.assertEqual(rc, 130)
        error = self.assert_single_terminal_event(collector, "error")
        self.assertEqual(error["code"], "cancelled")
        finished = self._telemetry_client.emit.call_args_list[-1].kwargs
        self.assertEqual(finished["result"], "cancelled")
        self.assertEqual(finished["stage"], "run_fsck")
        self.assertIn("Cancelled by user", finished["error"])

    def _run_disconnecting(self, operation: str, run, *, break_on: str) -> tuple[int, list[dict[str, object]]]:
        """Run `run` as `operation` through an app whose pipe breaks at the first
        event of type `break_on`, or at `stage` events naming that stage."""
        delivered: list[dict[str, object]] = []

        def emit(event: AppEvent) -> None:
            data = event.to_jsonable()
            if data["type"] == break_on or data.get("stage") == break_on:
                raise BrokenPipeError(32, "Broken pipe")
            delivered.append(data)

        with mock.patch.dict(service.OPERATIONS, {operation: run}):
            with mock.patch("timecapsulesmb.app.service.resolve_app_paths", return_value=SimpleNamespace(bootstrap_path=Path("/tmp/bootstrap"))):
                with mock.patch("timecapsulesmb.app.service.ensure_install_id"):
                    with mock.patch("timecapsulesmb.app.service.load_optional_env_config", return_value=AppConfig.from_values({})):
                        rc = service.run_api_request({"operation": operation, "params": {}}, EventSink(emit))
        return rc, delivered

    def test_lost_app_stops_where_the_cancel_button_would_have(self) -> None:
        # Each case: the operation, its stages, the event the pipe breaks at,
        # the stages that still run, and where it stops. Stages the app cannot
        # cancel run to the next one it can: a Flash write reaches its flush,
        # the second firmware bank is written.
        cases = (
            ("deploy", ("migrate_xattrs_copy", "replace_software", "check_flash_capacity", "upload_payload"),
             "log", ("migrate_xattrs_copy", "replace_software"), "check_flash_capacity", "migrate_xattrs_copy"),
            ("deploy", ("upload_payload", "upload_smbd", "post_upload_actions", "verify_payload_upload", "flush_payload_upload"),
             "log", ("upload_payload", "upload_smbd", "post_upload_actions"), "verify_payload_upload", "upload_payload"),
            ("deploy", ("migrate_xattrs_cleanup", "install_runtime_config", "enable_boot", "flush_boot_hook", "reboot",
                        "wait_for_reboot_down", "post_reboot_activation"),
             "log", ("migrate_xattrs_cleanup", "install_runtime_config", "enable_boot", "flush_boot_hook", "reboot"),
             "wait_for_reboot_down", "migrate_xattrs_cleanup"),
            ("flash", ("write_primary_bank", "write_active_bank", "post_write_validation"),
             "log", ("write_primary_bank", "write_active_bank"), "post_write_validation", "write_primary_bank"),
            # A stage without a policy can be cancelled in the app.
            ("deploy", ("migrate_xattrs_copy", "unlisted_stage"),
             "log", ("migrate_xattrs_copy",), "unlisted_stage", "migrate_xattrs_copy"),
            # The pipe breaks at a stage event: the stage that was running then
            # is the one the app was lost during.
            ("deploy", ("migrate_xattrs_copy", "replace_software", "check_flash_capacity"),
             "replace_software", ("migrate_xattrs_copy", "replace_software"), "check_flash_capacity", "migrate_xattrs_copy"),
        )
        for operation, stages, break_on, ran, stopped_before, during in cases:
            with self.subTest(operation=operation, during=during, break_on=break_on):
                self._telemetry_client.emit.reset_mock()
                reached: list[str] = []

                def run(_params, context, stages=stages):
                    for stage in stages:
                        context.stage(stage)
                        reached.append(stage)
                        context.log(f"{stage} done")
                    return service.OperationResult(True, {})

                rc, delivered = self._run_disconnecting(operation, run, break_on=break_on)

                self.assertEqual(rc, 130)
                self.assertEqual(tuple(reached), ran)
                # Nothing reaches the app after the pipe broke.
                self.assertEqual([event["type"] for event in delivered],
                                 ["stage"] if break_on == "log" else ["stage", "log"])
                finished = self._telemetry_client.emit.call_args_list[-1].kwargs
                self.assertEqual(finished["phase"], "finished")
                self.assertEqual(finished["result"], "cancelled")
                self.assertEqual(finished["error_code"], "client_disconnected")
                self.assertEqual(finished["details"], {"stopped_before_stage": stopped_before,
                                                       "disconnected_during_stage": during})

    def test_lost_app_during_stages_that_cannot_be_cancelled_lets_the_operation_finish(self) -> None:
        def run_deploy(_params, context):
            for stage in ("enable_boot", "flush_boot_hook"):
                context.stage(stage)
                context.log(f"{stage} done")
                context.to_operation_callbacks().stop_if_disconnected()
            return service.OperationResult(True, {"summary": "Deployment completed."})

        rc, _delivered = self._run_disconnecting("deploy", run_deploy, break_on="log")

        self.assertEqual(rc, 0)
        finished = self._telemetry_client.emit.call_args_list[-1].kwargs
        self.assertEqual(finished["result"], "success")

    def test_lost_app_ends_a_long_wait_at_its_next_checkpoint(self) -> None:
        polls: list[int] = []

        def run_deploy(_params, context):
            context.stage("wait_for_previous_migration")
            callbacks = context.to_operation_callbacks()
            for attempt in range(3):
                polls.append(attempt)
                context.log("still waiting")
                callbacks.stop_if_disconnected()
            return service.OperationResult(True, {})

        rc, _delivered = self._run_disconnecting("deploy", run_deploy, break_on="log")

        self.assertEqual(rc, 130)
        self.assertEqual(polls, [0])
        finished = self._telemetry_client.emit.call_args_list[-1].kwargs
        self.assertEqual(finished["error_code"], "client_disconnected")
        # It stopped inside the wait, not before a stage.
        self.assertEqual(finished["details"], {"disconnected_during_stage": "wait_for_previous_migration"})

    def test_lost_app_before_any_stage_reports_no_stage_names(self) -> None:
        def run_deploy(_params, context):
            context.log("starting")
            context.to_operation_callbacks().stop_if_disconnected()
            return service.OperationResult(True, {})

        rc, _delivered = self._run_disconnecting("deploy", run_deploy, break_on="log")

        self.assertEqual(rc, 130)
        finished = self._telemetry_client.emit.call_args_list[-1].kwargs
        self.assertIsNone(finished["details"])

    def test_lost_app_still_gets_finished_telemetry_for_every_outcome(self) -> None:
        def succeed(_params, context):
            context.stage("run_fsck")
            return service.OperationResult(True, {"summary": "Disk repair completed."})

        def fail(_params, context):
            context.stage("run_fsck")
            raise AppOperationError("fsck failed", code="operation_failed")

        def interrupt(_params, context):
            context.stage("run_fsck")
            raise KeyboardInterrupt

        cases = (
            ("result", succeed, 0, "success"),
            ("error", fail, 1, "failure"),
            ("error", interrupt, 130, "cancelled"),
        )
        for break_on, run, expected_rc, expected_result in cases:
            with self.subTest(break_on=break_on, result=expected_result):
                self._telemetry_client.emit.reset_mock()
                rc, _delivered = self._run_disconnecting("fsck", run, break_on=break_on)
                self.assertEqual(rc, expected_rc)
                finished = self._telemetry_client.emit.call_args_list[-1].kwargs
                self.assertEqual(finished["phase"], "finished")
                self.assertEqual(finished["result"], expected_result)

    def test_event_sink_copies_share_the_app_connection(self) -> None:
        attempts: list[str] = []
        lost: list[bool] = []

        def emit(event: AppEvent) -> None:
            attempts.append(event.type)
            raise BrokenPipeError(32, "Broken pipe")

        sink = EventSink(emit, client=AppClient(on_disconnect=lambda: lost.append(True)))
        copy = sink.with_request_id("request")
        copy.log("deploy", "first")
        sink.log("deploy", "second")
        copy.log("deploy", "third")

        self.assertIs(copy.client, sink.client)
        self.assertTrue(sink.client.disconnected)
        self.assertEqual(attempts, ["log"])
        self.assertEqual(lost, [True])

    def test_event_sink_lets_other_write_errors_through(self) -> None:
        def emit(_event: AppEvent) -> None:
            raise OSError(28, "No space left on device")

        sink = EventSink(emit)
        with self.assertRaises(OSError):
            sink.log("deploy", "message")
        self.assertFalse(sink.client.disconnected)

    def test_helper_stream_writes_one_event_at_a_time_from_two_threads(self) -> None:
        # Deploy's migration progress poller sends events from its own thread.
        class Stream:
            def __init__(self) -> None:
                self.pending: str | None = None
                self.lines: list[str] = []
                self.interleaved = 0

            def write(self, text: str) -> None:
                if self.pending is not None:
                    self.interleaved += 1
                self.pending = text
                time.sleep(0.0005)

            def flush(self) -> None:
                if self.pending is not None:
                    self.lines.append(self.pending)
                self.pending = None

        stream = Stream()
        sink = helper._sink_for_stream(stream)

        def send(name: str) -> None:
            for index in range(200):
                sink.progress("deploy", "migrate_xattrs_copy", entries=index, sender=name)

        threads = [threading.Thread(target=send, args=(name,)) for name in ("poller", "main")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(stream.interleaved, 0)
        events = [json.loads(line) for line in stream.lines]
        self.assertEqual(len(events), 400)
        for name in ("poller", "main"):
            self.assertEqual([event["entries"] for event in events if event["sender"] == name], list(range(200)))

    def test_discarded_output_swallows_later_writes(self) -> None:
        saved = [os.dup(1), os.dup(2)]

        def restore() -> None:
            for fd, copy in zip((1, 2), saved):
                os.dup2(copy, fd)
                os.close(copy)

        self.addCleanup(restore)
        read_end, write_end = os.pipe()
        os.dup2(write_end, 1)
        os.close(write_end)
        os.close(read_end)
        with self.assertRaises(BrokenPipeError):
            os.write(1, b"x")
        helper._discard_output()
        self.assertEqual(os.write(1, b"event\n"), 6)
        self.assertEqual(os.write(2, b"warning\n"), 8)

    def test_dispatcher_emits_failure_telemetry_on_system_exit(self) -> None:
        collector = CollectingSink()

        def run_fsck(_params, context):
            context.stage("run_fsck")
            raise SystemExit("Disk repair stopped early during fsck")

        with mock.patch.dict(service.OPERATIONS, {"fsck": run_fsck}):
            with mock.patch("timecapsulesmb.app.service.resolve_app_paths", return_value=SimpleNamespace(bootstrap_path=Path("/tmp/bootstrap"))):
                with mock.patch("timecapsulesmb.app.service.ensure_install_id"):
                    with mock.patch("timecapsulesmb.app.service.load_optional_env_config", return_value=AppConfig.from_values({})):
                        rc = service.run_api_request({"operation": "fsck", "params": {}}, collector.sink)

        self.assertEqual(rc, 1)
        error = self.assert_single_terminal_event(collector, "error")
        self.assertEqual(error["code"], "operation_failed")
        finished = self._telemetry_client.emit.call_args_list[-1].kwargs
        self.assertEqual(finished["result"], "failure")
        self.assertEqual(finished["stage"], "run_fsck")
        self.assertIn("Disk repair stopped early during fsck", finished["error"])

    def test_deploy_rejects_removed_nbns_option_before_device_access(self) -> None:
        for value in (False, True):
            with self.subTest(value=value):
                collector = CollectingSink()
                with mock.patch("timecapsulesmb.app.ops.deploy.load_request_config") as load_config:
                    rc = service.run_api_request(
                        {"operation": "deploy", "params": {"nbns_enabled": value}}, collector.sink,
                    )
                self.assertEqual(rc, 1)
                error = self.assert_single_terminal_event(collector, "error")
                self.assertEqual(error["code"], "validation_failed")
                self.assertEqual(error["recovery"]["title"], "Deployment validation failed")
                self.assertIn("always enabled", error["message"])
                load_config.assert_not_called()

    def test_dispatcher_emits_app_operation_finish_fields_in_telemetry(self) -> None:
        collector = CollectingSink()

        def run_deploy(_params, context):
            context.stage("verify_runtime_reboot")
            context.update_fields(
                device_family="netbsd6_samba4",
                device_os_version="NetBSD 6.0 (earmv4)",
                device_model="TimeCapsule8,119",
                device_syap="119",
                reboot_was_attempted=True,
                device_came_back_after_reboot=True,
            )
            return service.OperationResult(True, contracts.deploy_result_payload(
                payload_dir="/Volumes/dk2/.samba4",
                rebooted=True,
                reboot_requested=True,
                waited=True,
                verified=True,
                payload_family="netbsd6_samba4",
            ))

        with mock.patch.dict(service.OPERATIONS, {"deploy": run_deploy}):
            with mock.patch.dict(os.environ, {"TCAPSULE_CLIENT": "macos_gui"}, clear=False):
                with mock.patch("timecapsulesmb.app.service.resolve_app_paths", return_value=SimpleNamespace(bootstrap_path=Path("/tmp/bootstrap"))):
                    with mock.patch("timecapsulesmb.app.service.ensure_install_id"):
                        with mock.patch("timecapsulesmb.app.service.load_optional_env_config", return_value=AppConfig.from_values({"TC_CONFIGURE_ID": "cfg-1"})):
                            rc = service.run_api_request(
                                {"operation": "deploy", "params": {}},
                                collector.sink,
                            )

        self.assertEqual(rc, 0)
        self.assertNotIn("nbns_enabled", self._telemetry_factory.call_args.kwargs)
        finished = self._telemetry_client.emit.call_args_list[1].kwargs
        self.assertEqual(finished["result"], "success")
        self.assertEqual(finished["stage"], "verify_runtime_reboot")
        self.assertEqual(finished["device_family"], "netbsd6_samba4")
        self.assertEqual(finished["device_os_version"], "NetBSD 6.0 (earmv4)")
        self.assertEqual(finished["device_model"], "TimeCapsule8,119")
        self.assertEqual(finished["device_syap"], "119")
        self.assertNotIn("nbns_enabled", finished)
        self.assertEqual(finished["reboot_was_attempted"], True)
        self.assertEqual(finished["device_came_back_after_reboot"], True)
        self.assertEqual(finished["details"]["payload_family"], "netbsd6_samba4")
        self.assertEqual(finished["details"]["rebooted"], True)
        self.assertEqual(finished["details"]["verified"], True)

    def test_dispatcher_emits_flash_operation_details_in_telemetry(self) -> None:
        collector = CollectingSink()

        def run_flash(_params, context):
            context.stage("post_write_validation")
            context.update_fields(
                flash_action="write",
                flash_mode="restore",
                target_bank="primary",
                reboot_after_write=True,
                wait_after_reboot=True,
            )
            return service.OperationResult(True, contracts.flash_write_payload({
                "backup_dir": "/tmp/flash-backup",
                "write_outcome": {
                    "status": "written",
                    "mode": "restore",
                    "target_bank": "primary",
                    "write_validated": True,
                    "write_may_have_modified_device": True,
                    "post_write_action": "ssh_reboot",
                    "reboot_requested": True,
                    "rebooted": True,
                    "waited_after_reboot": True,
                },
            }))

        with mock.patch.dict(service.OPERATIONS, {"flash": run_flash}):
            with mock.patch("timecapsulesmb.app.service.resolve_app_paths", return_value=SimpleNamespace(bootstrap_path=Path("/tmp/bootstrap"))):
                with mock.patch("timecapsulesmb.app.service.ensure_install_id"):
                    with mock.patch("timecapsulesmb.app.service.load_optional_env_config", return_value=AppConfig.from_values({})):
                        rc = service.run_api_request(
                            {
                                "operation": "flash",
                                "params": {
                                    "action": "write",
                                    "mode": "restore",
                                    "backup_dir": "/tmp/flash-backup",
                                    "reboot_after_write": True,
                                    "wait_after_reboot": True,
                                },
                            },
                            collector.sink,
                        )

        self.assertEqual(rc, 0)
        finished = self._telemetry_client.emit.call_args_list[-1].kwargs
        self.assertEqual(finished["result"], "success")
        self.assertEqual(finished["stage"], "post_write_validation")
        self.assertEqual(finished["details"]["flash_action"], "write")
        self.assertEqual(finished["details"]["flash_mode"], "restore")
        self.assertTrue(finished["details"]["backup_dir_provided"])
        self.assertEqual(finished["details"]["write_status"], "written")
        self.assertTrue(finished["details"]["write_validated"])
        self.assertEqual(finished["details"]["target_bank"], "primary")
        self.assertTrue(finished["details"]["reboot_requested"])
        self.assertTrue(finished["details"]["waited_after_reboot"])

    def test_dispatcher_emits_confirmation_required_telemetry(self) -> None:
        collector = CollectingSink()

        def run_fsck(params, context):
            context.stage("select_fsck_volume")
            raise service.AppConfirmationRequired(build_confirmation(
                operation="fsck",
                params=params,
                title="Confirm fsck",
                message="Run fsck on the selected HFS volume and reboot the device?",
                action_title="Run fsck",
                risk="destructive",
                summary="Filesystem check and repair",
                context={"volume": params.get("volume")},
                presentation_id="fsck.reboot",
                presentation_values={"volume": params.get("volume")},
            ))

        with mock.patch.dict(service.OPERATIONS, {"fsck": run_fsck}):
            with mock.patch("timecapsulesmb.app.service.resolve_app_paths", return_value=SimpleNamespace(bootstrap_path=Path("/tmp/bootstrap"))):
                with mock.patch("timecapsulesmb.app.service.ensure_install_id"):
                    with mock.patch("timecapsulesmb.app.service.load_optional_env_config", return_value=AppConfig.from_values({})):
                        rc = service.run_api_request(
                            {"operation": "fsck", "params": {"volume": "Data"}},
                            collector.sink,
                        )

        self.assertEqual(rc, 1)
        self.assertEqual(self._telemetry_client.emit.call_count, 2)
        finished_kwargs = self._telemetry_client.emit.call_args_list[1].kwargs
        self.assertEqual(finished_kwargs["result"], "confirmation_required")
        self.assertIsNone(finished_kwargs["error"])
        self.assertEqual(finished_kwargs["risk"], "destructive")
        self.assertEqual(finished_kwargs["details"]["presentation_id"], "fsck.reboot")
        self.assertEqual(finished_kwargs["details"]["presentation_values"]["volume"], "Data")

    def test_dispatcher_does_not_emit_readiness_operation_telemetry(self) -> None:
        collector = CollectingSink()
        self._telemetry_factory.reset_mock()

        rc = service.run_api_request({"operation": "capabilities", "params": {}}, collector.sink)

        self.assertEqual(rc, 0)
        self._telemetry_factory.assert_not_called()

    def test_app_api_telemetry_tests_do_not_open_network_connections(self) -> None:
        collector = CollectingSink()

        def run_fsck(_params, context):
            context.stage("run_fsck")
            return service.OperationResult(True, {"returncode": 0, "summary": "Disk repair completed with fsck."})

        with mock.patch.dict(service.OPERATIONS, {"fsck": run_fsck}):
            with mock.patch("timecapsulesmb.app.service.resolve_app_paths", return_value=SimpleNamespace(bootstrap_path=Path("/tmp/bootstrap"))):
                with mock.patch("timecapsulesmb.app.service.ensure_install_id"):
                    with mock.patch("timecapsulesmb.app.service.load_optional_env_config", return_value=AppConfig.from_values({})):
                        rc = service.run_api_request({"operation": "fsck", "params": {}}, collector.sink)

        self.assertEqual(rc, 0)
        self._telemetry_factory.assert_called_once()
        self.assertEqual(self._telemetry_client.emit.call_count, 2)
        self._telemetry_urlopen.assert_not_called()

    def test_dispatcher_failure_telemetry_uses_app_operation_context(self) -> None:
        collector = CollectingSink()

        def run_fsck(_params, context):
            context.stage("read_mast")
            context.config = AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"})
            context.connection = SshConnection("root@10.0.0.2", "pw", "-o foo")
            context.add_debug_fields(mast_candidates=[{"volume": "Data"}])
            raise service.AppOperationError("No writable MaSt volumes were found.", code="remote_error")

        with mock.patch.dict(service.OPERATIONS, {"fsck": run_fsck}):
            with mock.patch("timecapsulesmb.app.service.resolve_app_paths", return_value=SimpleNamespace(bootstrap_path=Path("/tmp/bootstrap"))):
                with mock.patch("timecapsulesmb.app.service.ensure_install_id"):
                    with mock.patch("timecapsulesmb.app.service.load_optional_env_config", return_value=AppConfig.from_values({})):
                        rc = service.run_api_request({"operation": "fsck", "params": {}}, collector.sink)

        self.assertEqual(rc, 1)
        telemetry_error = self._telemetry_client.emit.call_args_list[-1].kwargs["error"]
        self.assertIn("No writable MaSt volumes were found.", telemetry_error)
        self.assertIn("Debug context:", telemetry_error)
        self.assertIn("command=fsck", telemetry_error)
        self.assertIn("stage=read_mast", telemetry_error)
        self.assertIn("host=root@10.0.0.2", telemetry_error)
        self.assertIn("TC_HOST=root@10.0.0.2", telemetry_error)
        self.assertIn("mast_candidates=[{volume:Data}]", telemetry_error)
        self.assertNotIn("TC_PASSWORD=pw", telemetry_error)

    def test_dispatcher_unsuccessful_result_telemetry_uses_app_operation_context(self) -> None:
        collector = CollectingSink()

        def run_fsck(_params, context):
            context.stage("run_fsck")
            context.config = AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"})
            context.connection = SshConnection("root@10.0.0.2", "pw", "")
            return service.OperationResult(False, {"error": "Disk repair exited with fsck status 8"})

        with mock.patch.dict(service.OPERATIONS, {"fsck": run_fsck}):
            with mock.patch("timecapsulesmb.app.service.resolve_app_paths", return_value=SimpleNamespace(bootstrap_path=Path("/tmp/bootstrap"))):
                with mock.patch("timecapsulesmb.app.service.ensure_install_id"):
                    with mock.patch("timecapsulesmb.app.service.load_optional_env_config", return_value=AppConfig.from_values({})):
                        rc = service.run_api_request({"operation": "fsck", "params": {}}, collector.sink)

        self.assertEqual(rc, 1)
        result = self.assert_single_terminal_event(collector, "result")
        self.assertFalse(result["ok"])
        self.assertNotIn("Debug context:", result["payload"]["error"])
        telemetry_error = self._telemetry_client.emit.call_args_list[-1].kwargs["error"]
        self.assertIn("Disk repair exited with fsck status 8", telemetry_error)
        self.assertIn("Debug context:", telemetry_error)
        self.assertIn("command=fsck", telemetry_error)
        self.assertIn("stage=run_fsck", telemetry_error)
        self.assertNotIn("TC_PASSWORD=pw", telemetry_error)

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
                    ipv4=("169.254.44.9", "10.0.0.2"),
                    properties={"syAP": "119"},
                    fullname="TC._airport._tcp.local.",
                )
            ],
        )

        with mock.patch(
            "timecapsulesmb.app.ops.discovery.discover_snapshot_detailed",
            return_value=(snapshot, SimpleNamespace()),
        ):
            with mock.patch("timecapsulesmb.app.service.resolve_app_paths", return_value=SimpleNamespace(bootstrap_path=Path("/tmp/bootstrap"))):
                with mock.patch("timecapsulesmb.app.service.ensure_install_id"):
                    with mock.patch("timecapsulesmb.app.service.load_optional_env_config", return_value=AppConfig.from_values({})):
                        rc = service.run_api_request({"operation": "discover", "params": {"timeout": 6.0}}, collector.sink)

        self.assertEqual(rc, 0)
        result = collector.events_of_type("result")[0]
        self.assertEqual(result["payload"]["resolved"][0]["name"], "TC")
        self.assertEqual(result["payload"]["resolved"][0]["ipv4"], ["169.254.44.9", "10.0.0.2"])
        self.assertEqual(result["payload"]["devices"][0]["name"], "TC")
        self.assertEqual(result["payload"]["devices"][0]["host"], "10.0.0.2")
        self.assertEqual(result["payload"]["devices"][0]["ssh_host"], "root@10.0.0.2")
        # The app reads ssh_host; the IPv4-only hints it never read are gone.
        self.assertNotIn("preferred_ipv4", result["payload"]["devices"][0])
        self.assertNotIn("link_local_only", result["payload"]["devices"][0])
        self.assertEqual(result["payload"]["devices"][0]["selected_record"]["fullname"], "TC._airport._tcp.local.")
        self.assertEqual(result["payload"]["schema_version"], 1)
        self.assertEqual(result["payload"]["counts"], {"instances": 1, "resolved": 1, "devices": 1})
        self.assertEqual(result["payload"]["summary"], "Discovered 1 device.")
        self.assertEqual(self._telemetry_client.emit.call_count, 2)
        started = self._telemetry_client.emit.call_args_list[0].kwargs
        finished = self._telemetry_client.emit.call_args_list[1].kwargs
        self.assertEqual(started["operation"], "discover")
        self.assertEqual(started["entrypoint"], "api")
        self.assertEqual(started["options"], {"timeout": 6.0})
        self.assertEqual(finished["result"], "success")
        self.assertEqual(finished["stage"], "bonjour_discovery")
        self.assertEqual(finished["discovery_instance_count"], 1)
        self.assertEqual(finished["discovery_resolved_count"], 1)
        self.assertEqual(finished["discovery_device_count"], 1)
        self.assertNotIn("discovery_unsupported_syaps", finished)
        self.assertEqual(finished["details"]["instance_count"], 1)
        self.assertEqual(finished["details"]["resolved_count"], 1)
        self.assertEqual(finished["details"]["device_count"], 1)

    def test_discover_operation_exposes_deduped_devices_separately_from_raw_services(self) -> None:
        collector = CollectingSink()
        raw_records = [
            BonjourResolvedService(
                name=name,
                hostname=f"{name.lower()}.local.",
                service_type=service_type,
                port=5009,
                ipv4=ipv4,
                properties={"syAP": syap},
                fullname=f"{name}.{service_type}",
            )
            for name, ipv4, syap in (
                ("James", ("169.254.155.207", "192.168.1.217"), "119"),
                ("Office", ("10.0.0.9",), "116"),
            )
            for service_type in (
                "_adisk._tcp.local.",
                "_airport._tcp.local.",
                "_device-info._tcp.local.",
                "_smb._tcp.local.",
            )
        ]
        snapshot = BonjourDiscoverySnapshot(instances=[], resolved=raw_records)

        with mock.patch(
            "timecapsulesmb.app.ops.discovery.discover_snapshot_detailed",
            return_value=(snapshot, SimpleNamespace()),
        ):
            with mock.patch("timecapsulesmb.app.service.resolve_app_paths", return_value=SimpleNamespace(bootstrap_path=Path("/tmp/bootstrap"))):
                with mock.patch("timecapsulesmb.app.service.ensure_install_id"):
                    with mock.patch("timecapsulesmb.app.service.load_optional_env_config", return_value=AppConfig.from_values({})):
                        rc = service.run_api_request({"operation": "discover", "params": {"timeout": 6.0}}, collector.sink)

        self.assertEqual(rc, 0)
        payload = collector.events_of_type("result")[0]["payload"]
        self.assertEqual(payload["counts"], {"instances": 0, "resolved": 8, "devices": 2})
        self.assertEqual([device["name"] for device in payload["devices"]], ["James", "Office"])
        self.assertEqual(payload["devices"][0]["host"], "192.168.1.217")
        self.assertEqual(payload["devices"][0]["selected_record"]["service_type"], "_airport._tcp.local.")

    def test_discover_marks_whether_each_device_model_is_supported(self) -> None:
        collector = CollectingSink()
        snapshot = BonjourDiscoverySnapshot(
            instances=[],
            resolved=[
                BonjourResolvedService(
                    name=name,
                    hostname=f"{name.lower()}.local.",
                    service_type="_airport._tcp.local.",
                    port=5009,
                    ipv4=(ip,),
                    properties=properties,
                    fullname=f"{name}._airport._tcp.local.",
                )
                for name, ip, properties in (
                    ("Capsule", "10.0.0.2", {"syAP": "119"}),
                    ("Express", "10.0.0.3", {"syAP": "115"}),
                    ("Unknown", "10.0.0.4", {}),
                )
            ],
        )

        with mock.patch(
            "timecapsulesmb.app.ops.discovery.discover_snapshot_detailed",
            return_value=(snapshot, SimpleNamespace()),
        ):
            with mock.patch("timecapsulesmb.app.service.resolve_app_paths", return_value=SimpleNamespace(bootstrap_path=Path("/tmp/bootstrap"))):
                with mock.patch("timecapsulesmb.app.service.ensure_install_id"):
                    with mock.patch("timecapsulesmb.app.service.load_optional_env_config", return_value=AppConfig.from_values({})):
                        rc = service.run_api_request({"operation": "discover", "params": {"timeout": 6.0}}, collector.sink)

        self.assertEqual(rc, 0)
        devices = collector.events_of_type("result")[0]["payload"]["devices"]
        self.assertEqual(
            {device["name"]: device["supported_model"] for device in devices},
            {"Capsule": True, "Express": False, "Unknown": None},
        )
        # The picker stops on the Express before configure, so discovery telemetry
        # is where its advertised code is recorded.
        finished = self._telemetry_client.emit.call_args_list[-1].kwargs
        self.assertEqual(self._telemetry_client.emit.call_args_list[-1].args[0], "discover_finished")
        self.assertEqual(finished["discovery_unsupported_syaps"], ["115"])

    def test_discover_rejects_invalid_timeout_values(self) -> None:
        for timeout in ("bad", "nan", -1, True):
            with self.subTest(timeout=timeout):
                collector = CollectingSink()
                with mock.patch("timecapsulesmb.app.ops.discovery.discover_snapshot_detailed") as discover:
                    with mock.patch("timecapsulesmb.app.service.resolve_app_paths", return_value=SimpleNamespace(bootstrap_path=Path("/tmp/bootstrap"))):
                        with mock.patch("timecapsulesmb.app.service.ensure_install_id"):
                            with mock.patch("timecapsulesmb.app.service.load_optional_env_config", return_value=AppConfig.from_values({})):
                                rc = service.run_api_request(
                                    {"operation": "discover", "params": {"timeout": timeout}},
                                    collector.sink,
                                )

                self.assertEqual(rc, 1)
                error = self.assert_single_terminal_event(collector, "error")
                self.assertEqual(error["code"], "discovery_timeout_too_short")
                self.assertEqual(error["recovery"]["title"], "Request validation failed")
                discover.assert_not_called()

    def test_discover_accepts_numeric_timeout_string(self) -> None:
        collector = CollectingSink()
        snapshot = BonjourDiscoverySnapshot(instances=[], resolved=[])

        with mock.patch(
            "timecapsulesmb.app.ops.discovery.discover_snapshot_detailed",
            return_value=(snapshot, SimpleNamespace()),
        ) as discover:
            with mock.patch("timecapsulesmb.app.service.resolve_app_paths", return_value=SimpleNamespace(bootstrap_path=Path("/tmp/bootstrap"))):
                with mock.patch("timecapsulesmb.app.service.ensure_install_id"):
                    with mock.patch("timecapsulesmb.app.service.load_optional_env_config", return_value=AppConfig.from_values({})):
                        rc = service.run_api_request(
                            {"operation": "discover", "params": {"timeout": "5.5"}},
                            collector.sink,
                        )

        self.assertEqual(rc, 0)
        discover.assert_called_once_with(timeout=5.5)

    def test_configure_returns_confirmed_hardware_identity_and_rejects_a_different_record(self) -> None:
        from dataclasses import replace
        base = probed_state()
        state = replace(base, probe_result=replace(base.probe_result, airport_mac="02:aa:bb:cc:dd:ee"))
        for advertised in ("02-AA-BB-CC-DD-EE", "02:aa:bb:cc:dd:ff"):
            with self.subTest(advertised=advertised), tempfile.TemporaryDirectory() as tmp:
                collector = CollectingSink()
                path = Path(tmp) / ".env"
                params = {"config": str(path), "password": "pw", "selected_record": {
                    "name": "Office", "hostname": "office.local", "service_type": "_airport._tcp.local.",
                    "ipv4": ["10.0.0.2"], "properties": {"waMA": advertised},
                }}
                with mock.patch("timecapsulesmb.app.ops.configure.probe_connection_state", return_value=state):
                    rc = service.run_api_request({"operation": "configure", "params": params}, collector.sink)
                if advertised == "02:aa:bb:cc:dd:ff":
                    self.assertEqual(rc, 1)
                    self.assertFalse(path.exists())
                    self.assertEqual(self.assert_single_terminal_event(collector, "error")["code"], "device_identity_mismatch")
                else:
                    self.assertEqual(rc, 0)
                    self.assertEqual(self.assert_single_terminal_event(collector, "result")["payload"]["airport_mac"], "02:aa:bb:cc:dd:ee")

    def test_configure_refuses_a_password_ssh_accepts_but_the_device_rejects(self) -> None:
        # SSH checks only 8 characters; the app reports the device's syPW
        # mismatch as the AirPort password being rejected and saves nothing.
        collector = CollectingSink()
        compare = acp_password_answer(False)
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / ".env"
            with mock.patch("timecapsulesmb.app.ops.configure.probe_connection_state", return_value=probed_state()):
                with mock.patch("timecapsulesmb.device.probe.read_airport_acp", compare):
                    rc = service.run_api_request(
                        {"operation": "configure",
                         "params": {"config": str(config_path), "host": "root@10.0.0.2", "password": "goodpw-typo"}},
                        collector.sink,
                    )
            self.assertFalse(config_path.exists())

        self.assertEqual(rc, 1)
        error = self.assert_single_terminal_event(collector, "error")
        self.assertEqual(error["code"], "auth_failed")
        self.assertEqual(error["message"], "The AirPort admin password did not work.")
        self.assertEqual(error["recovery"]["action_ids"], ["replace_password"])
        self.assertEqual(compare.call_args.args[1], "goodpw-typo")
        self.assertNotIn("goodpw-typo", json.dumps(collector.events))

    def test_configure_writes_env_without_persisting_or_leaking_password_by_default(self) -> None:
        collector = CollectingSink()
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / ".env"
            with mock.patch("timecapsulesmb.app.ops.configure.probe_connection_state", return_value=probed_state()):
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
            self.assertNotIn("TC_PASSWORD=goodpw", config_path.read_text())
            self.assertEqual(parse_env_file(config_path)["TC_PASSWORD"], "")
            self.assertIn("TC_DEBUG_LOGGING=false", config_path.read_text())
            serialized_events = json.dumps(collector.events)
            self.assertNotIn("goodpw", serialized_events)

    def test_configure_rejects_link_local_host_before_writing_env(self) -> None:
        collector = CollectingSink()
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / ".env"
            with mock.patch(
                "timecapsulesmb.app.ops.configure.probe_connection_state",
                side_effect=AssertionError("link-local host should fail before probing"),
            ):
                rc = service.run_api_request(
                    {
                        "operation": "configure",
                        "params": {
                            "config": str(config_path),
                            "host": "root@169.254.189.7",
                            "password": "goodpw",
                        },
                    },
                    collector.sink,
                )

        self.assertEqual(rc, 1)
        error = self.assert_single_terminal_event(collector, "error")
        self.assertEqual(error["code"], "validation_failed")
        self.assertIn("Device SSH target host must not be a link-local address", error["message"])
        self.assertFalse(config_path.exists())

    def test_configure_link_local_only_record_reports_its_addresses(self) -> None:
        # v3.1.1 telemetry: 12 installs hit this rejection 99 times, and the
        # failure carried no addresses, so nobody could tell whether the device
        # lacked a LAN IPv4 address or discovery missed one.
        collector = CollectingSink()
        self._record_acp_probe.return_value = "timed out"
        record = {
            "name": "Beaulieu",
            "hostname": "Beaulieu.local.",
            "service_type": "_airport._tcp.local.",
            "port": 5009,
            "ipv4": ["169.254.44.113"],
            "ipv6": ["fe80::bac7:5dff:fecf:d8ea%en0"],
            "properties": {"syAP": "116"},
            "fullname": "Beaulieu._airport._tcp.local.",
        }
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / ".env"
            config_path.write_text("TC_HOST=root@10.0.0.2\n")
            with mock.patch(
                "timecapsulesmb.app.ops.configure.probe_connection_state",
                side_effect=AssertionError("a link-local-only record must fail before probing"),
            ):
                rc = service.run_api_request(
                    {
                        "operation": "configure",
                        "params": {"config": str(config_path), "selected_record": record, "password": "goodpw"},
                    },
                    collector.sink,
                )
            saved = config_path.read_text()

        self.assertEqual(rc, 1)
        error = self.assert_single_terminal_event(collector, "error")
        self.assertEqual(error["code"], "validation_failed")
        self.assertIn("only advertised link-local addresses", error["message"])
        self.assertEqual(error["debug"]["stage"], "select_target")
        self.assertIn("169.254.44.113", json.dumps(error["debug"]["selected_bonjour_record"]))
        self.assertIn("fe80::bac7:5dff:fecf:d8ea%en0", json.dumps(error["debug"]["selected_bonjour_record"]))
        self.assertNotIn("configure_target_source", error["debug"])
        telemetry_error = self._telemetry_client.emit.call_args_list[-1].kwargs["error"]
        self.assertIn("only advertised link-local addresses", telemetry_error)
        self.assertIn("selected_bonjour_record=", telemetry_error)
        self.assertIn("169.254.44.113", telemetry_error)
        self.assertIn("fe80::bac7:5dff:fecf:d8ea%en0", telemetry_error)
        self.assertNotIn("goodpw", repr(self._telemetry_client.emit.call_args_list))
        # The saved target of another device is neither used nor overwritten.
        self.assertEqual(saved, "TC_HOST=root@10.0.0.2\n")

    def test_configure_without_a_selected_record_reports_none(self) -> None:
        collector = CollectingSink()
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / ".env"
            with mock.patch(
                "timecapsulesmb.app.ops.configure.probe_connection_state",
                side_effect=AssertionError("a link-local host must fail before probing"),
            ):
                rc = service.run_api_request(
                    {
                        "operation": "configure",
                        "params": {
                            "config": str(config_path),
                            "host": "root@169.254.189.7",
                            # Not a record: ignored, as the resolver ignores it.
                            "selected_record": "Office Capsule",
                            "password": "goodpw",
                        },
                    },
                    collector.sink,
                )

        self.assertEqual(rc, 1)
        error = self.assert_single_terminal_event(collector, "error")
        self.assertEqual(error["code"], "validation_failed")
        self.assertNotIn("selected_bonjour_record", error["debug"])
        telemetry_error = self._telemetry_client.emit.call_args_list[-1].kwargs["error"]
        self.assertNotIn("selected_bonjour_record=", telemetry_error)

    def test_configure_selected_record_refreshes_stale_existing_host(self) -> None:
        collector = CollectingSink()
        captured_connections: list[SshConnection] = []

        def capture_probe(connection: SshConnection) -> ProbedDeviceState:
            captured_connections.append(connection)
            return probed_state()

        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / ".env"
            config_path.write_text("TC_HOST=root@10.0.0.2\n")
            with mock.patch("timecapsulesmb.app.ops.configure.probe_connection_state", side_effect=capture_probe):
                rc = service.run_api_request(
                    {
                        "operation": "configure",
                        "params": {
                            "config": str(config_path),
                            "selected_record": {
                                "name": "Office Capsule",
                                "hostname": "office-capsule.local.",
                                "service_type": "_airport._tcp.local.",
                                "port": 5009,
                                "ipv4": ["10.0.0.80"],
                                "ipv6": [],
                                "properties": {"syAP": "119"},
                                "fullname": "Office Capsule._airport._tcp.local.",
                            },
                            "password": "goodpw",
                        },
                    },
                    collector.sink,
                )

            values = parse_env_file(config_path)

        self.assertEqual(rc, 0)
        self.assertEqual(captured_connections[0].host, "root@10.0.0.80")
        self.assertEqual(values["TC_HOST"], "root@10.0.0.80")

    def test_configure_follows_a_device_that_took_a_new_dhcp_address(self) -> None:
        # Telemetry install 2e923635 (v3.3.0): the discovered record said .80,
        # the device had moved to .81, and every retry went back to .80.
        collector = CollectingSink()
        probed_hosts: list[str] = []
        states = iter([unreachable_probed_state(), probed_state()])

        def capture_probe(connection: SshConnection) -> ProbedDeviceState:
            probed_hosts.append(connection.host)
            return next(states)

        properties = {"syAP": "119", "waMA": DEVICE_AIRPORT_MAC.upper().replace(":", "-")}
        record = {
            "name": "Office Capsule",
            "hostname": "Office-Capsule.local",
            "service_type": "_airport._tcp.local.",
            "port": 5009,
            "ipv4": ["192.168.31.80"],
            "ipv6": [],
            "properties": properties,
            "fullname": "Office Capsule._airport._tcp.local.",
        }
        moved = BonjourResolvedService(
            name="Office Capsule", hostname="Office-Capsule.local", service_type="_airport._tcp.local.", port=5009,
            ipv4=["192.168.31.81"], properties=dict(properties), fullname="Office Capsule._airport._tcp.local.",
        )
        browse_diagnostics = BonjourQueryDiagnostics("zeroconf", ["_airport._tcp.local."], 2, 2, 0, 1)
        device = FakeAcpDevice(ssh_open=False)
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / ".env"
            params = {"config": str(config_path), "selected_record": record, "password": "pw"}
            params["confirmation_id"] = self.confirmation_id_for(
                "configure",
                params,
                {"host": "root@192.168.31.80", "device_name": "Office Capsule", "requires_reboot": True},
            )
            with mock.patch("timecapsulesmb.app.ops.configure.probe_connection_state", side_effect=capture_probe), \
                    mock.patch(
                        "timecapsulesmb.services.acp_ssh.tcp_connect_error",
                        side_effect=lambda address, _port: "timed out" if address == "192.168.31.80" else None,
                    ), \
                    mock.patch("timecapsulesmb.services.acp_ssh.time.sleep"), \
                    mock.patch("timecapsulesmb.services.acp_ssh.local_lan_networks", return_value=()), \
                    mock.patch("timecapsulesmb.services.acp_diagnostics.local_interface_networks", return_value=()), \
                    mock.patch("timecapsulesmb.services.acp_ssh.set_dbug") as set_dbug, \
                    mock.patch(
                        "timecapsulesmb.discovery.bonjour.BonjourQuery.browse",
                        return_value=(BonjourDiscoverySnapshot([], [moved]), browse_diagnostics),
                    ), \
                    device.patched():
                rc = service.run_api_request({"operation": "configure", "params": params}, collector.sink)
            values = parse_env_file(config_path)

        self.assertEqual(rc, 0)
        self.assertEqual(set_dbug.call_args.args[:2], ("192.168.31.81", "pw"))
        self.assertEqual(probed_hosts, ["root@192.168.31.80", "root@192.168.31.81"])
        self.assertEqual(values["TC_HOST"], "root@192.168.31.81")
        self.assertEqual(self.assert_single_terminal_event(collector, "result")["payload"]["host"], "root@192.168.31.81")
        finished = self._telemetry_client.emit.call_args_list[-1].kwargs
        follow = finished["execution"]["measurements"]["host_follow"][0]
        self.assertEqual((follow["trigger"], follow["result"], follow["candidates"]), ("acp_unreachable", "found", 1))
        self.assertEqual(finished["current_host"], "root@192.168.31.81")

    def test_configure_reaches_a_device_on_another_ipv4_subnet_over_fe80(self) -> None:
        # Discussion #368: same wire, two DHCP scopes. The record's IPv4 is off
        # this Mac's network; its link-local IPv6 answers, as AirPort Utility uses.
        collector = CollectingSink()
        captured_connections: list[SshConnection] = []
        self._record_acp_probe.side_effect = lambda address, port: None if address.lower().startswith("fe80") else "timed out"

        def capture_probe(connection: SshConnection) -> ProbedDeviceState:
            captured_connections.append(connection)
            return probed_state()

        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / ".env"
            with mock.patch("timecapsulesmb.app.ops.configure.probe_connection_state", side_effect=capture_probe):
                rc = service.run_api_request(
                    {
                        "operation": "configure",
                        "params": {
                            "config": str(config_path),
                            "selected_record": {
                                "name": "AirPort Time Capsule",
                                "hostname": "AirPort-Time-Capsule.local",
                                "service_type": "_airport._tcp.local.",
                                "port": 5009,
                                "ipv4": ["192.168.1.83", "169.254.205.45"],
                                "ipv6": ["FE80:0000:0000:0000:66A5:C3FF:FE60:FC22%en0"],
                                "properties": {"syAP": "119"},
                                "fullname": "AirPort Time Capsule._airport._tcp.local.",
                                "interface_index": 6,
                            },
                            "password": "goodpw",
                        },
                    },
                    collector.sink,
                )
            values = parse_env_file(config_path)

        self.assertEqual(rc, 0)
        self.assertEqual(captured_connections[0].host, "root@fe80::66a5:c3ff:fe60:fc22%en0")
        self.assertEqual(values["TC_HOST"], "root@fe80::66a5:c3ff:fe60:fc22%en0")
        # The probing has its own stage, ahead of the SSH probe.
        stages = [event["stage"] for event in collector.events_of_type("stage")]
        self.assertLess(stages.index("select_target"), stages.index("ssh_probe"))
        select_stage = collector.events_of_type("stage")[stages.index("select_target")]
        self.assertEqual(select_stage["risk"], "remote_read")
        result = self.assert_single_terminal_event(collector, "result")
        self.assertEqual(result["payload"]["host"], "root@fe80::66a5:c3ff:fe60:fc22%en0")

    def test_configure_defaults_bare_host_to_root_user(self) -> None:
        collector = CollectingSink()
        captured_connections: list[SshConnection] = []

        def capture_probe(connection: SshConnection) -> ProbedDeviceState:
            captured_connections.append(connection)
            return probed_state()

        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / ".env"
            with mock.patch("timecapsulesmb.app.ops.configure.probe_connection_state", side_effect=capture_probe):
                rc = service.run_api_request(
                    {
                        "operation": "configure",
                        "params": {
                            "config": str(config_path),
                            "host": " 10.0.0.2 ",
                            "password": "goodpw",
                        },
                    },
                    collector.sink,
                )

            values = parse_env_file(config_path)

        self.assertEqual(rc, 0)
        self.assertEqual(captured_connections[0].host, "root@10.0.0.2")
        self.assertEqual(values["TC_HOST"], "root@10.0.0.2")
        self.assertEqual(collector.events_of_type("result")[0]["payload"]["host"], "root@10.0.0.2")

    def test_configure_canonicalizes_default_ssh_port_before_probe_and_save(self) -> None:
        collector = CollectingSink()
        captured_connections: list[SshConnection] = []

        def capture_probe(connection: SshConnection) -> ProbedDeviceState:
            captured_connections.append(connection)
            return probed_state()

        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / ".env"
            with mock.patch("timecapsulesmb.app.ops.configure.probe_connection_state", side_effect=capture_probe):
                rc = service.run_api_request(
                    {
                        "operation": "configure",
                        "params": {
                            "config": str(config_path),
                            "host": "root@10.0.0.2:22",
                            "password": "goodpw",
                        },
                    },
                    collector.sink,
                )

            values = parse_env_file(config_path)

        self.assertEqual(rc, 0)
        self.assertEqual(captured_connections[0].host, "root@10.0.0.2")
        self.assertEqual(values["TC_HOST"], "root@10.0.0.2")
        self.assertEqual(collector.events_of_type("result")[0]["payload"]["host"], "root@10.0.0.2")

    def test_configure_can_persist_password_for_env_compatibility_when_requested(self) -> None:
        collector = CollectingSink()
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / ".env"
            with mock.patch("timecapsulesmb.app.ops.configure.probe_connection_state", return_value=probed_state()):
                rc = service.run_api_request(
                    {
                        "operation": "configure",
                        "params": {
                            "config": str(config_path),
                            "host": "root@10.0.0.2",
                            "password": "goodpw",
                            "persist_password": True,
                        },
                    },
                    collector.sink,
                )

            values = parse_env_file(config_path)

        self.assertEqual(rc, 0)
        self.assertEqual(values["TC_PASSWORD"], "goodpw")
        self.assertNotIn("goodpw", json.dumps(collector.events))

    def test_configure_preserves_custom_env_keys_and_drops_deprecated_runtime_keys(self) -> None:
        collector = CollectingSink()
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / ".env"
            config_path.write_text(
                "TC_HOST=root@10.0.0.1\n"
                "TC_PASSWORD=oldpw\n"
                "TC_CUSTOM_SETTING='keep me'\n"
                "TC_DEBUG_LOGGING=true\n"
                "TC_ATA_IDLE_SECONDS=42\n"
                "TC_ATA_STANDBY=0\n"
                "TC_SAMBA_USER=old-admin\n"
                "TC_PAYLOAD_DIR_NAME=old-payload\n"
            )
            with mock.patch("timecapsulesmb.app.ops.configure.probe_connection_state", return_value=probed_state()):
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
        self.assertEqual(values["TC_PASSWORD"], "")
        self.assertEqual(values["TC_CUSTOM_SETTING"], "keep me")
        self.assertEqual(values["TC_DEBUG_LOGGING"], "true")
        self.assertEqual(values["TC_ATA_IDLE_SECONDS"], "42")
        self.assertEqual(values["TC_ATA_STANDBY"], "0")
        self.assertNotIn("TC_SAMBA_USER", values)
        self.assertNotIn("TC_PAYLOAD_DIR_NAME", values)

    def test_configure_debug_logging_param_writes_true(self) -> None:
        collector = CollectingSink()
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / ".env"
            with mock.patch("timecapsulesmb.app.ops.configure.probe_connection_state", return_value=probed_state()):
                rc = service.run_api_request(
                    {
                        "operation": "configure",
                        "params": {
                            "config": str(config_path),
                            "host": "root@10.0.0.2",
                            "password": "goodpw",
                            "debug_logging": True,
                        },
                    },
                    collector.sink,
                )

            values = parse_env_file(config_path)

        self.assertEqual(rc, 0)
        self.assertEqual(values["TC_DEBUG_LOGGING"], "true")

    def test_configure_smb_browse_compatibility_param_writes_true(self) -> None:
        collector = CollectingSink()
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / ".env"
            with mock.patch("timecapsulesmb.app.ops.configure.probe_connection_state", return_value=probed_state()):
                rc = service.run_api_request(
                    {
                        "operation": "configure",
                        "params": {
                            "config": str(config_path),
                            "host": "root@10.0.0.2",
                            "password": "goodpw",
                            "smb_browse_compatibility": True,
                        },
                    },
                    collector.sink,
                )

            values = parse_env_file(config_path)

        self.assertEqual(rc, 0)
        self.assertEqual(values["TC_SMB_BROWSE_COMPATIBILITY"], "true")

    def test_configure_netatalk_metadata_param_writes_true(self) -> None:
        collector = CollectingSink()
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / ".env"
            with mock.patch("timecapsulesmb.app.ops.configure.probe_connection_state", return_value=probed_state()):
                rc = service.run_api_request(
                    {
                        "operation": "configure",
                        "params": {
                            "config": str(config_path),
                            "host": "root@10.0.0.2",
                            "password": "goodpw",
                            "fruit_metadata_netatalk": True,
                        },
                    },
                    collector.sink,
                )

            values = parse_env_file(config_path)

        self.assertEqual(rc, 0)
        self.assertEqual(values["TC_FRUIT_METADATA_NETATALK"], "true")

    def test_update_config_settings_is_local_and_preserves_credentials_and_custom_values(self) -> None:
        collector = CollectingSink()
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / ".env"
            config_path.write_text(
                "TC_HOST=root@10.0.0.2\n"
                "TC_CONFIGURE_ID=existing-id\n"
                "TC_CUSTOM_SETTING='kept value'\n"
            )

            with mock.patch("timecapsulesmb.app.ops.configure.probe_connection_state") as probe:
                rc = service.run_api_request(
                    {
                        "operation": "update-config-settings",
                        "params": {
                            "config": str(config_path),
                            "internal_share_use_disk_root": True,
                            "smb_browse_compatibility": True,
                            "mdns_advertise_afp": True,
                            "any_protocol": True,
                            "require_smb_encryption": False,
                            "force_disable_smb_signing_and_encryption": True,
                            "fruit_metadata_netatalk": False,
                            "vfs_aio_fork_enabled": True,
                            "debug_logging": True,
                            "ata_idle_seconds": 0,
                            "ata_standby": "",
                        },
                    },
                    collector.sink,
                )
                probe.assert_not_called()
            values = parse_env_file(config_path)

        self.assertEqual(rc, 0)
        self.assertEqual(values["TC_HOST"], "root@10.0.0.2")
        self.assertEqual(values["TC_CONFIGURE_ID"], "existing-id")
        self.assertEqual(values["TC_CUSTOM_SETTING"], "kept value")
        self.assertEqual(values["TC_PASSWORD"], "")
        self.assertEqual(values["TC_INTERNAL_SHARE_USE_DISK_ROOT"], "true")
        self.assertEqual(values["TC_SMB_BROWSE_COMPATIBILITY"], "true")
        self.assertEqual(values["TC_MDNS_ADVERTISE_AFP"], "true")
        self.assertEqual(values["TC_ANY_PROTOCOL"], "true")
        self.assertEqual(values["TC_REQUIRE_SMB_ENCRYPTION"], "false")
        self.assertEqual(values["TC_FORCE_DISABLE_SMB_SIGNING_AND_ENCRYPTION"], "true")
        self.assertEqual(values["TC_FRUIT_METADATA_NETATALK"], "false")
        self.assertEqual(values["TC_VFS_AIO_FORK_ENABLED"], "true")
        self.assertEqual(values["TC_DEBUG_LOGGING"], "true")
        self.assertEqual(values["TC_ATA_IDLE_SECONDS"], "0")
        self.assertEqual(values["TC_ATA_STANDBY"], "")
        self.assertEqual(
            [event["stage"] for event in collector.events_of_type("stage")],
            ["load_existing_config", "write_env"],
        )

    def test_update_config_settings_validation_failure_leaves_config_unchanged(self) -> None:
        collector = CollectingSink()
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / ".env"
            original = "TC_HOST=root@10.0.0.2\nTC_ANY_PROTOCOL=false\n"
            config_path.write_text(original)

            rc = service.run_api_request(
                {
                    "operation": "update-config-settings",
                    "params": {
                        "config": str(config_path),
                        "any_protocol": True,
                        "require_smb_encryption": True,
                    },
                },
                collector.sink,
            )

            self.assertEqual(config_path.read_text(), original)

        self.assertEqual(rc, 1)
        self.assertEqual(collector.events_of_type("error")[0]["code"], "validation_failed")

    def test_configure_vfs_aio_fork_param_writes_true(self) -> None:
        collector = CollectingSink()
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / ".env"
            with mock.patch("timecapsulesmb.app.ops.configure.probe_connection_state", return_value=probed_state()):
                rc = service.run_api_request(
                    {
                        "operation": "configure",
                        "params": {
                            "config": str(config_path),
                            "host": "root@10.0.0.2",
                            "password": "goodpw",
                            "vfs_aio_fork_enabled": True,
                        },
                    },
                    collector.sink,
                )

            values = parse_env_file(config_path)

        self.assertEqual(rc, 0)
        self.assertEqual(values["TC_VFS_AIO_FORK_ENABLED"], "true")

    def test_configure_mdns_advertise_afp_param_writes_true(self) -> None:
        collector = CollectingSink()
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / ".env"
            with mock.patch("timecapsulesmb.app.ops.configure.probe_connection_state", return_value=probed_state()):
                rc = service.run_api_request(
                    {
                        "operation": "configure",
                        "params": {
                            "config": str(config_path),
                            "host": "root@10.0.0.2",
                            "password": "goodpw",
                            "mdns_advertise_afp": True,
                        },
                    },
                    collector.sink,
                )

            values = parse_env_file(config_path)

        self.assertEqual(rc, 0)
        self.assertEqual(values["TC_MDNS_ADVERTISE_AFP"], "true")

    def test_configure_require_smb_encryption_param_writes_true(self) -> None:
        collector = CollectingSink()
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / ".env"
            with mock.patch("timecapsulesmb.app.ops.configure.probe_connection_state", return_value=probed_state()):
                rc = service.run_api_request(
                    {
                        "operation": "configure",
                        "params": {
                            "config": str(config_path),
                            "host": "root@10.0.0.2",
                            "password": "goodpw",
                            "require_smb_encryption": True,
                        },
                    },
                    collector.sink,
                )

            values = parse_env_file(config_path)

        self.assertEqual(rc, 0)
        self.assertEqual(values["TC_REQUIRE_SMB_ENCRYPTION"], "true")

    def test_configure_rejects_any_protocol_with_smb_encryption(self) -> None:
        collector = CollectingSink()
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / ".env"
            rc = service.run_api_request(
                {
                    "operation": "configure",
                    "params": {
                        "config": str(config_path),
                        "host": "root@10.0.0.2",
                        "password": "goodpw",
                        "any_protocol": True,
                        "require_smb_encryption": True,
                    },
                },
                collector.sink,
            )

        self.assertEqual(rc, 1)
        self.assertIn("SMB encryption requires SMB3-only", collector.events_of_type("error")[0]["message"])

    def test_configure_force_disable_smb_security_param_writes_true(self) -> None:
        collector = CollectingSink()
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / ".env"
            with mock.patch("timecapsulesmb.app.ops.configure.probe_connection_state", return_value=probed_state()):
                rc = service.run_api_request(
                    {
                        "operation": "configure",
                        "params": {
                            "config": str(config_path),
                            "host": "root@10.0.0.2",
                            "password": "goodpw",
                            "force_disable_smb_signing_and_encryption": True,
                        },
                    },
                    collector.sink,
                )
            values = parse_env_file(config_path)

        self.assertEqual(rc, 0)
        self.assertEqual(values["TC_FORCE_DISABLE_SMB_SIGNING_AND_ENCRYPTION"], "true")

    def test_configure_rejects_required_and_disabled_smb_encryption(self) -> None:
        collector = CollectingSink()
        with tempfile.TemporaryDirectory() as tmp:
            rc = service.run_api_request(
                {
                    "operation": "configure",
                    "params": {
                        "config": str(Path(tmp) / ".env"),
                        "host": "root@10.0.0.2",
                        "password": "goodpw",
                        "require_smb_encryption": True,
                        "force_disable_smb_signing_and_encryption": True,
                    },
                },
                collector.sink,
            )

        self.assertEqual(rc, 1)
        self.assertIn("cannot be used with Force Disable", collector.events_of_type("error")[0]["message"])

    def test_configure_ata_params_write_drive_timer_settings(self) -> None:
        collector = CollectingSink()
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / ".env"
            with mock.patch("timecapsulesmb.app.ops.configure.probe_connection_state", return_value=probed_state()):
                rc = service.run_api_request(
                    {
                        "operation": "configure",
                        "params": {
                            "config": str(config_path),
                            "host": "root@10.0.0.2",
                            "password": "goodpw",
                            "ata_idle_seconds": 0,
                            "ata_standby": 0,
                        },
                    },
                    collector.sink,
                )

            values = parse_env_file(config_path)

        self.assertEqual(rc, 0)
        self.assertEqual(values["TC_ATA_IDLE_SECONDS"], "0")
        self.assertEqual(values["TC_ATA_STANDBY"], "0")

    def test_configure_blank_ata_standby_clears_existing_timer_setting(self) -> None:
        collector = CollectingSink()
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / ".env"
            config_path.write_text("TC_HOST=root@10.0.0.2\nTC_ATA_IDLE_SECONDS=300\nTC_ATA_STANDBY=120\n")
            with mock.patch("timecapsulesmb.app.ops.configure.probe_connection_state", return_value=probed_state()):
                rc = service.run_api_request(
                    {
                        "operation": "configure",
                        "params": {
                            "config": str(config_path),
                            "host": "root@10.0.0.2",
                            "password": "goodpw",
                            "ata_standby": "",
                        },
                    },
                    collector.sink,
                )

            values = parse_env_file(config_path)

        self.assertEqual(rc, 0)
        self.assertEqual(values["TC_ATA_IDLE_SECONDS"], "300")
        self.assertEqual(values["TC_ATA_STANDBY"], "")

    def test_configure_requires_confirmation_before_enabling_ssh(self) -> None:
        collector = CollectingSink()
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / ".env"
            with mock.patch("timecapsulesmb.app.ops.configure.probe_connection_state", return_value=unreachable_probed_state()):
                with mock.patch("timecapsulesmb.services.acp_ssh.set_dbug") as enable_ssh:
                    rc = service.run_api_request(
                        {
                            "operation": "configure",
                            "params": {
                                "config": str(config_path),
                                "host": "root@10.0.0.2",
                                "password": "secret",
                            },
                        },
                        collector.sink,
                    )

        self.assertEqual(rc, 1)
        enable_ssh.assert_not_called()
        self.assertFalse(config_path.exists())
        details = self.assert_confirmation(
            collector,
            "configure.enable_ssh_reboot",
            {"device_name": "10.0.0.2", "requires_reboot": True},
        )
        self.assertEqual(details["context"]["host"], "root@10.0.0.2")
        self.assertNotIn("secret", json.dumps(collector.events))

    def test_configure_confirmed_ssh_enable_reprobes_and_writes_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / ".env"
            first_collector = CollectingSink()
            with mock.patch("timecapsulesmb.app.ops.configure.probe_connection_state", return_value=unreachable_probed_state()):
                service.run_api_request(
                    {
                        "operation": "configure",
                        "params": {
                            "config": str(config_path),
                            "host": "root@10.0.0.2",
                            "password": "secret",
                        },
                    },
                    first_collector.sink,
                )
            confirmation_id = self.assert_confirmation(first_collector, "configure.enable_ssh_reboot")["confirmation_id"]

            confirmed_collector = CollectingSink()
            with mock.patch(
                "timecapsulesmb.app.ops.configure.probe_connection_state",
                side_effect=[unreachable_probed_state(), probed_state()],
            ) as probe:
                with mock.patch("timecapsulesmb.services.acp_ssh.tcp_connect_error", return_value=None) as tcp_connect_error:
                    with mock.patch("timecapsulesmb.services.acp_ssh.set_dbug") as enable_ssh:
                        with mock.patch("timecapsulesmb.services.configure.reboot_device") as wait_for_ssh:
                            rc = service.run_api_request(
                                {
                                    "operation": "configure",
                                    "params": {
                                        "config": str(config_path),
                                        "host": "root@10.0.0.2",
                                        "password": "secret",
                                        "confirmation_id": confirmation_id,
                                    },
                                },
                                confirmed_collector.sink,
                            )

            values = parse_env_file(config_path)

        self.assertEqual(rc, 0)
        self.assertEqual(probe.call_count, 2)
        tcp_connect_error.assert_called_once_with("10.0.0.2", 5009)
        enable_ssh.assert_called_once()
        wait_for_ssh.assert_called_once_with(
            "10.0.0.2",
            "secret",
            wait=True,
            callbacks=mock.ANY,
            up_timeout_message=SSH_ENABLE_TIMEOUT_MESSAGE,
        )
        self.assertEqual(values["TC_HOST"], "root@10.0.0.2")
        stages = [event["stage"] for event in confirmed_collector.events_of_type("stage")]
        self.assertLess(stages.index("acp_port_probe"), stages.index("acp_enable_ssh"))
        self.assertNotIn("secret", json.dumps(confirmed_collector.events))

    def test_set_ssh_status_reports_acp_reachable_ssh_closed(self) -> None:
        collector = CollectingSink()
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / ".env"
            config_path.write_text("TC_HOST=root@10.0.0.2\n")
            with mock.patch(
                "timecapsulesmb.app.ops.set_ssh.probe_set_ssh_status",
                return_value=SetSshStatusResult(
                    host="10.0.0.2",
                    acp_port_reachable=True,
                    ssh_port_reachable=False,
                    ssh_port_error="Connection refused",
                ),
            ) as probe:
                rc = service.run_api_request(
                    {
                        "operation": "set-ssh",
                        "params": {
                            "config": str(config_path),
                            "action": "status",
                        },
                    },
                    collector.sink,
                )

        self.assertEqual(rc, 0)
        probe.assert_called_once_with("root@10.0.0.2")
        payload = self.assert_single_terminal_event(collector, "result")["payload"]
        self.assertEqual(payload["host"], "10.0.0.2")
        self.assertTrue(payload["acp_port_reachable"])
        self.assertFalse(payload["ssh_port_reachable"])
        self.assertTrue(payload["ssh_disabled_likely"])
        self.assertEqual(payload["summary"], "AirPort ACP is reachable, but SSH is closed.")
        self._telemetry_factory.assert_not_called()
        self._telemetry_client.emit.assert_not_called()

    def run_set_ssh_disable_with_password_answer(self, matches: bool | None, *, ssh_open: bool) -> tuple[CollectingSink, int, mock.Mock, mock.Mock]:
        collector = CollectingSink()
        compare = acp_password_answer(matches)
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / ".env"
            config_path.write_text("TC_HOST=root@10.0.0.2\n")
            with mock.patch(
                "timecapsulesmb.app.ops.set_ssh.probe_set_ssh_status",
                return_value=SetSshStatusResult(host="10.0.0.2", acp_port_reachable=True, ssh_port_reachable=ssh_open),
            ):
                with mock.patch("timecapsulesmb.app.ops.set_ssh.disable_set_ssh") as disable_ssh:
                    with mock.patch("timecapsulesmb.device.probe.read_airport_acp", compare):
                        rc = service.run_api_request(
                            {"operation": "set-ssh",
                             "params": {"config": str(config_path), "action": "disable", "password": "secret"}},
                            collector.sink,
                        )
        return collector, rc, compare, disable_ssh

    def test_set_ssh_disable_refuses_a_password_the_device_would_reject_before_asking(self) -> None:
        # Disabling SSH ends in an ACP reboot; refused before the confirmation
        # and before SSH is turned off in the saved settings.
        collector, rc, compare, disable_ssh = self.run_set_ssh_disable_with_password_answer(False, ssh_open=True)

        self.assertEqual(rc, 1)
        error = self.assert_single_terminal_event(collector, "error")
        self.assertEqual(error["code"], "auth_failed")
        self.assertEqual(compare.call_args.args[1], "secret")
        disable_ssh.assert_not_called()

    def test_set_ssh_disable_asks_for_confirmation_when_the_password_matches(self) -> None:
        collector, rc, compare, disable_ssh = self.run_set_ssh_disable_with_password_answer(True, ssh_open=True)

        self.assertEqual(rc, 1)
        details = self.assert_confirmation(
            collector,
            "ssh_access.disable_reboot",
            {"host": "10.0.0.2", "device_name": "10.0.0.2", "requires_reboot": True},
        )
        self.assertEqual(details["message"], "Disable SSH on 10.0.0.2 and reboot this AirPort device?")
        compare.assert_called_once()
        disable_ssh.assert_not_called()

    def test_set_ssh_disable_runs_once_confirmed(self) -> None:
        result = SetSshResult(
            host="10.0.0.2", action="disable_ssh", ssh_initially_reachable=True, ssh_final_reachable=False,
            acp_port_reachable=True, reboot_requested=True, waited=True,
            summary=Summary("ssh.disabled", "SSH disabled."),
        )
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / ".env"
            config_path.write_text("TC_HOST=root@10.0.0.2\n")
            params: dict[str, object] = {"config": str(config_path), "action": "disable", "password": "secret"}
            with mock.patch(
                "timecapsulesmb.app.ops.set_ssh.probe_set_ssh_status",
                return_value=SetSshStatusResult(host="10.0.0.2", acp_port_reachable=True, ssh_port_reachable=True),
            ):
                with mock.patch("timecapsulesmb.app.ops.set_ssh.disable_set_ssh", return_value=result) as disable_ssh:
                    asking = CollectingSink()
                    service.run_api_request({"operation": "set-ssh", "params": params}, asking.sink)
                    confirmation_id = self.assert_confirmation(asking, "ssh_access.disable_reboot")["confirmation_id"]
                    disable_ssh.assert_not_called()

                    collector = CollectingSink()
                    rc = service.run_api_request(
                        {"operation": "set-ssh", "params": {**params, "confirmation_id": confirmation_id}},
                        collector.sink,
                    )

        self.assertEqual(rc, 0, collector.events)
        disable_ssh.assert_called_once()

    def test_set_ssh_disable_with_ssh_already_off_does_not_compare_the_password(self) -> None:
        _collector, _rc, compare, _disable_ssh = self.run_set_ssh_disable_with_password_answer(False, ssh_open=False)

        compare.assert_not_called()

    def test_set_ssh_enable_requires_reboot_confirmation(self) -> None:
        collector = CollectingSink()
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / ".env"
            config_path.write_text("TC_HOST=root@10.0.0.2\n")
            with mock.patch(
                "timecapsulesmb.app.ops.set_ssh.probe_set_ssh_status",
                return_value=SetSshStatusResult(
                    host="10.0.0.2",
                    acp_port_reachable=True,
                    ssh_port_reachable=False,
                ),
            ):
                with mock.patch("timecapsulesmb.app.ops.set_ssh.enable_set_ssh") as enable_ssh:
                    rc = service.run_api_request(
                        {
                            "operation": "set-ssh",
                            "params": {
                                "config": str(config_path),
                                "action": "enable",
                                "password": "secret",
                            },
                        },
                        collector.sink,
                    )

        self.assertEqual(rc, 1)
        enable_ssh.assert_not_called()
        details = self.assert_confirmation(
            collector,
            "ssh_access.enable_reboot",
            {
                "host": "10.0.0.2",
                "device_name": "10.0.0.2",
                "requires_reboot": True,
            },
        )
        self.assertEqual(details["context"]["host"], "root@10.0.0.2")
        self.assertEqual(details["context"]["device_name"], "10.0.0.2")
        self.assertNotIn("secret", json.dumps(collector.events))
        self.assertEqual(self._telemetry_client.emit.call_count, 2)
        started = self._telemetry_client.emit.call_args_list[0]
        finished = self._telemetry_client.emit.call_args_list[1]
        self.assertEqual(started.args, ("set_ssh_started",))
        self.assertEqual(started.kwargs["options"]["action"], "enable")
        self.assertEqual(finished.args, ("set_ssh_finished",))
        self.assertEqual(finished.kwargs["result"], "confirmation_required")

    def test_set_ssh_confirmed_enable_returns_normalized_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / ".env"
            config_path.write_text("TC_HOST=root@10.0.0.2\n")
            initial_status = SetSshStatusResult(
                host="10.0.0.2",
                acp_port_reachable=True,
                ssh_port_reachable=False,
            )
            with mock.patch("timecapsulesmb.app.ops.set_ssh.probe_set_ssh_status", return_value=initial_status):
                first_collector = CollectingSink()
                rc = service.run_api_request(
                    {
                        "operation": "set-ssh",
                        "params": {
                            "config": str(config_path),
                            "action": "enable",
                            "password": "secret",
                        },
                    },
                    first_collector.sink,
                )
                self.assertEqual(rc, 1)
            confirmation_id = self.assert_confirmation(first_collector, "ssh_access.enable_reboot")["confirmation_id"]

            confirmed_collector = CollectingSink()
            result = SetSshResult(
                host="10.0.0.2",
                action="enable_ssh",
                ssh_initially_reachable=False,
                ssh_final_reachable=True,
                acp_port_reachable=True,
                reboot_requested=True,
                waited=True,
                summary=Summary("ssh.configured", "SSH is configured."),
            )
            with mock.patch("timecapsulesmb.app.ops.set_ssh.probe_set_ssh_status", return_value=initial_status):
                with mock.patch("timecapsulesmb.app.ops.set_ssh.enable_set_ssh", return_value=result) as enable_ssh:
                    rc = service.run_api_request(
                        {
                            "operation": "set-ssh",
                            "params": {
                                "config": str(config_path),
                                "action": "enable",
                                "password": "secret",
                                "confirmation_id": confirmation_id,
                            },
                        },
                        confirmed_collector.sink,
                    )

        self.assertEqual(rc, 0)
        enable_ssh.assert_called_once()
        self.assertEqual(enable_ssh.call_args.args[0].host, "root@10.0.0.2")
        self.assertFalse(enable_ssh.call_args.kwargs["no_wait"])
        payload = self.assert_single_terminal_event(confirmed_collector, "result")["payload"]
        self.assertEqual(payload["action"], "enable_ssh")
        self.assertTrue(payload["ssh_port_reachable"])
        self.assertTrue(payload["ssh_final_reachable"])
        self.assertFalse(payload["ssh_disabled_likely"])
        self.assertIsNone(payload["ssh_port_error"])
        self.assertEqual(payload["summary"], "SSH is configured.")
        self.assertEqual(payload["summary_key"], "ssh.configured")
        self.assertNotIn("secret", json.dumps(confirmed_collector.events))

    def test_set_ssh_enable_timeout_uses_specific_gui_error_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / ".env"
            config_path.write_text("TC_HOST=root@10.0.0.2\nTC_PASSWORD=secret\n")
            initial_status = SetSshStatusResult(
                host="10.0.0.2",
                acp_port_reachable=True,
                ssh_port_reachable=False,
            )
            first_collector = CollectingSink()
            with mock.patch("timecapsulesmb.app.ops.set_ssh.probe_set_ssh_status", return_value=initial_status):
                rc = service.run_api_request(
                    {
                        "operation": "set-ssh",
                        "params": {
                            "config": str(config_path),
                            "action": "enable",
                            "password": "secret",
                        },
                    },
                    first_collector.sink,
                )
            self.assertEqual(rc, 1)
            confirmation_id = self.assert_confirmation(first_collector, "ssh_access.enable_reboot")["confirmation_id"]

            collector = CollectingSink()
            with mock.patch("timecapsulesmb.app.ops.set_ssh.probe_set_ssh_status", return_value=initial_status):
                with mock.patch(
                    "timecapsulesmb.app.ops.set_ssh.enable_set_ssh",
                    side_effect=RebootFlowError(SSH_ENABLE_TIMEOUT_MESSAGE, "reboot_not_finished"),
                ):
                    rc = service.run_api_request(
                        {
                            "operation": "set-ssh",
                            "params": {
                                "config": str(config_path),
                                "action": "enable",
                                "password": "secret",
                                "confirmation_id": confirmation_id,
                            },
                        },
                        collector.sink,
                    )

        self.assertEqual(rc, 1)
        error = self.assert_single_terminal_event(collector, "error")
        self.assertEqual(error["code"], "ssh_enable_timeout")
        self.assertEqual(error["message"], "Failed to enable SSH via ACP: SSH did not open after enabling via ACP.")
        self.assertEqual(error["recovery"]["localization_key"], "ssh_enable_timeout")
        self.assertTrue(error["recovery"]["retryable"])
        self.assertNotIn("secret", json.dumps(collector.events))

    def test_set_ssh_enable_for_a_saved_address_off_this_macs_network_says_so(self) -> None:
        # The Mac moved to another network since the device was saved.
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / ".env"
            config_path.write_text("TC_HOST=root@10.0.0.2\n")
            initial_status = SetSshStatusResult(host="10.0.0.2", acp_port_reachable=False, ssh_port_reachable=False)
            params = {"config": str(config_path), "action": "enable", "password": "secret"}
            first_collector = CollectingSink()
            with mock.patch("timecapsulesmb.app.ops.set_ssh.probe_set_ssh_status", return_value=initial_status):
                service.run_api_request({"operation": "set-ssh", "params": params}, first_collector.sink)
            params["confirmation_id"] = self.assert_confirmation(first_collector, "ssh_access.enable_reboot")["confirmation_id"]

            collector = CollectingSink()
            elsewhere = (LocalInterfaceNetwork("en0", "192.168.20.5", ipaddress.ip_network("192.168.20.0/24")),)
            with mock.patch("timecapsulesmb.app.ops.set_ssh.probe_set_ssh_status", return_value=initial_status), \
                    mock.patch("timecapsulesmb.services.acp_ssh.tcp_connect_error", return_value="timed out"), \
                    mock.patch("timecapsulesmb.services.acp_ssh.time.sleep"), \
                    mock.patch("timecapsulesmb.services.acp_ssh._record_port_probe_context"), \
                    mock.patch("timecapsulesmb.services.acp_ssh.local_lan_networks", return_value=elsewhere), \
                    mock.patch("timecapsulesmb.services.acp_ssh.sys.platform", "darwin"), \
                    mock.patch("timecapsulesmb.services.set_ssh.reboot_device") as reboot:
                rc = service.run_api_request({"operation": "set-ssh", "params": params}, collector.sink)

        self.assertEqual(rc, 1)
        reboot.assert_not_called()
        error = self.assert_single_terminal_event(collector, "error")
        self.assertEqual(error["code"], "device_off_network")
        self.assertIn("10.0.0.2 is not on this Mac's network (192.168.20.0/24)", error["message"])
        self.assertEqual(error["recovery"]["localization_key"], "device_off_network")
        self.assertEqual(error["recovery"]["title"], "Device not on this Mac's network")

    def test_set_ssh_rejected_admin_password_uses_auth_failed_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / ".env"
            config_path.write_text("TC_HOST=root@10.0.0.2\n")
            initial_status = SetSshStatusResult(
                host="10.0.0.2",
                acp_port_reachable=True,
                ssh_port_reachable=False,
            )
            first_collector = CollectingSink()
            with mock.patch("timecapsulesmb.app.ops.set_ssh.probe_set_ssh_status", return_value=initial_status):
                rc = service.run_api_request(
                    {
                        "operation": "set-ssh",
                        "params": {
                            "config": str(config_path),
                            "action": "enable",
                            "password": "secret",
                        },
                    },
                    first_collector.sink,
                )
            self.assertEqual(rc, 1)
            confirmation_id = self.assert_confirmation(first_collector, "ssh_access.enable_reboot")["confirmation_id"]

            collector = CollectingSink()
            with mock.patch("timecapsulesmb.app.ops.set_ssh.probe_set_ssh_status", return_value=initial_status):
                with mock.patch(
                    "timecapsulesmb.app.ops.set_ssh.enable_set_ssh",
                    side_effect=ACPAuthError("ACP command failed with error_code -0x10"),
                ):
                    rc = service.run_api_request(
                        {
                            "operation": "set-ssh",
                            "params": {
                                "config": str(config_path),
                                "action": "enable",
                                "password": "secret",
                                "confirmation_id": confirmation_id,
                            },
                        },
                        collector.sink,
                    )

        self.assertEqual(rc, 1)
        error = self.assert_single_terminal_event(collector, "error")
        self.assertEqual(error["code"], "auth_failed")
        self.assertEqual(error["message"], "The AirPort admin password did not work.")
        self.assertEqual(error["recovery"]["action_ids"], ["replace_password"])
        self.assertNotIn("secret", json.dumps(collector.events))

    def test_ssh_access_operation_is_removed(self) -> None:
        collector = CollectingSink()

        rc = service.run_api_request(
            {
                "operation": "ssh-access",
                "params": {
                    "action": "status",
                },
            },
            collector.sink,
        )

        self.assertEqual(rc, 1)
        error = self.assert_single_terminal_event(collector, "error")
        self.assertEqual(error["code"], "unknown_operation")

    def test_configure_enable_ssh_false_fails_without_confirmation(self) -> None:
        collector = CollectingSink()
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / ".env"
            with mock.patch("timecapsulesmb.app.ops.configure.probe_connection_state", return_value=unreachable_probed_state()):
                with mock.patch("timecapsulesmb.services.acp_ssh.set_dbug") as enable_ssh:
                    rc = service.run_api_request(
                        {
                            "operation": "configure",
                            "params": {
                                "config": str(config_path),
                                "host": "root@10.0.0.2",
                                "password": "secret",
                                "enable_ssh": False,
                            },
                        },
                        collector.sink,
                    )

        self.assertEqual(rc, 1)
        enable_ssh.assert_not_called()
        self.assertFalse(config_path.exists())
        error = self.assert_single_terminal_event(collector, "error")
        self.assertEqual(error["code"], "remote_error")
        self.assertNotEqual(error.get("details", {}).get("presentation_id"), "configure.enable_ssh_reboot")

    def test_configure_reports_acp_auth_failure_without_writing_env(self) -> None:
        collector = CollectingSink()
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / ".env"
            params = {
                "config": str(config_path),
                "host": "root@10.0.0.2",
                "password": "badpw",
            }
            params["confirmation_id"] = self.confirmation_id_for(
                "configure",
                params,
                {
                    "host": "root@10.0.0.2",
                    "device_name": "10.0.0.2",
                    "requires_reboot": True,
                },
            )
            with mock.patch("timecapsulesmb.app.ops.configure.probe_connection_state", return_value=unreachable_probed_state()):
                with mock.patch("timecapsulesmb.services.acp_ssh.tcp_connect_error", return_value=None):
                    with mock.patch("timecapsulesmb.services.acp_ssh.set_dbug", side_effect=ACPAuthError("bad password")):
                        rc = service.run_api_request(
                            {
                                "operation": "configure",
                                "params": params,
                            },
                            collector.sink,
                        )

        self.assertEqual(rc, 1)
        self.assertFalse(config_path.exists())
        self.assertEqual(collector.events_of_type("error")[0]["code"], "auth_failed")
        self.assertEqual(collector.events_of_type("error")[0]["recovery"]["suggested_operation"], "configure")
        self.assertEqual(collector.events_of_type("error")[0]["recovery"]["action_ids"], ["replace_password"])
        self.assertNotIn("badpw", json.dumps(collector.events))

    def test_configure_reports_ssh_auth_rejection_as_password_failure_without_writing_env(self) -> None:
        collector = CollectingSink()
        ssh_error = "root@192.168.1.162: Permission denied (publickey,password,keyboard-interactive)."
        probe_state = ProbedDeviceState(
            probe_result=ProbeResult(
                ssh_status=SshAccessStatus.AUTH_REJECTED,
                error=ssh_error,
                os_name="",
                os_release="",
                arch="",
                elf_endianness="unknown",
            ),
            compatibility=None,
        )
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / ".env"
            with mock.patch("timecapsulesmb.app.ops.configure.probe_connection_state", return_value=probe_state):
                rc = service.run_api_request(
                    {
                        "operation": "configure",
                        "params": {
                            "config": str(config_path),
                            "host": "root@192.168.1.162",
                            "password": "badpw",
                        },
                    },
                    collector.sink,
                )

        self.assertEqual(rc, 1)
        self.assertFalse(config_path.exists())
        error = self.assert_single_terminal_event(collector, "error")
        self.assertEqual(error["code"], "auth_failed")
        self.assertEqual(error["message"], "The AirPort admin password did not work.")
        self.assertEqual(error["debug"]["exception"], ssh_error)
        self.assertEqual(error["recovery"]["title"], "AirPort password rejected")
        self.assertEqual(error["recovery"]["localization_key"], "configure.auth_failed")
        self.assertEqual(error["recovery"]["suggested_operation"], "configure")
        self.assertEqual(error["recovery"]["action_ids"], ["replace_password"])
        self.assertNotIn("badpw", json.dumps(collector.events))

    def test_configure_reports_ssh_algorithm_failure_without_password_rejection(self) -> None:
        collector = CollectingSink()
        ssh_error = (
            "Unable to negotiate with 192.168.200.214 port 22: no matching MAC found. "
            "Their offer: hmac-md5,hmac-sha1,hmac-ripemd160,hmac-ripemd160@openssh.com,hmac-sha1-96,hmac-md5-96"
        )
        probe_state = ProbedDeviceState(
            probe_result=ProbeResult(
                ssh_status=SshAccessStatus.ALGORITHM_NEGOTIATION_FAILED,
                error=ssh_error,
                os_name="",
                os_release="",
                arch="",
                elf_endianness="unknown",
            ),
            compatibility=None,
        )
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / ".env"
            with mock.patch("timecapsulesmb.app.ops.configure.probe_connection_state", return_value=probe_state):
                rc = service.run_api_request(
                    {
                        "operation": "configure",
                        "params": {
                            "config": str(config_path),
                            "host": "root@192.168.200.214",
                            "password": "pw",
                        },
                    },
                    collector.sink,
                )

        self.assertEqual(rc, 1)
        self.assertFalse(config_path.exists())
        error = collector.events_of_type("error")[0]
        self.assertEqual(error["code"], "ssh_compatibility_failed")
        self.assertEqual(error["recovery"]["title"], "SSH compatibility failed")
        self.assertNotEqual(error["recovery"]["title"], "AirPort password rejected")
        self.assertNotIn("replace_password", error["recovery"]["action_ids"])
        self.assertIn("no matching MAC found", error["message"])

    def test_configure_reports_a_connection_this_mac_dropped_and_what_could_filter_it(self) -> None:
        collector = CollectingSink()
        message = f"{LOCAL_NETWORK_FILTERED_MESSAGE} (ssh: connect to host 192.168.1.22 port 22: Bad file descriptor)"
        probe_state = ProbedDeviceState(
            probe_result=ProbeResult(
                ssh_status=SshAccessStatus.LOCAL_NETWORK_FILTERED,
                error=message,
                os_name="",
                os_release="",
                arch="",
                elf_endianness="unknown",
                mac_network_filters={"mac_vpn_services": ["(Connected) VPN (io.tailscale.ipn.macos)"]},
            ),
            compatibility=None,
        )
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / ".env"
            with mock.patch("timecapsulesmb.app.ops.configure.probe_connection_state", return_value=probe_state):
                rc = service.run_api_request(
                    {
                        "operation": "configure",
                        "params": {"config": str(config_path), "host": "root@192.168.1.22", "password": "pw"},
                    },
                    collector.sink,
                )

            self.assertEqual(rc, 1)
            self.assertFalse(config_path.exists())
        error = collector.events_of_type("error")[0]
        self.assertEqual((error["code"], error["message"]), ("local_network_filtered", message))
        self.assert_local_network_filtered_recovery(error)
        finished = self._telemetry_client.emit.call_args_list[-1].kwargs
        self.assertEqual(finished["error_code"], "local_network_filtered")
        self.assertIn("probe_ssh_status=local_network_filtered", finished["error"])
        self.assertIn("mac_vpn_services=[(Connected) VPN (io.tailscale.ipn.macos)]", finished["error"])

    def test_configure_reports_acp_port_preflight_connection_failure(self) -> None:
        collector = CollectingSink()
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / ".env"
            params = {
                "config": str(config_path),
                "host": "root@10.0.0.99",
                "password": "pw",
            }
            params["confirmation_id"] = self.confirmation_id_for(
                "configure",
                params,
                {
                    "host": "root@10.0.0.99",
                    "device_name": "10.0.0.99",
                    "requires_reboot": True,
                },
            )
            # This Mac is on the address's network, so the failure is not explained by where it is.
            with mock.patch("timecapsulesmb.app.ops.configure.probe_connection_state", return_value=unreachable_probed_state()), \
                    mock.patch("timecapsulesmb.services.acp_ssh.local_lan_networks", return_value=(LocalInterfaceNetwork("en0", "10.0.0.50", ipaddress.ip_network("10.0.0.0/24")),)):
                with mock.patch("timecapsulesmb.services.acp_ssh.tcp_connect_error", return_value="Connection refused"):
                    with mock.patch("timecapsulesmb.services.acp_ssh.time.sleep") as sleep:
                        with mock.patch("timecapsulesmb.services.acp_ssh.set_dbug") as enable_ssh:
                            rc = service.run_api_request(
                                {
                                    "operation": "configure",
                                    "params": params,
                                },
                                collector.sink,
                            )

        self.assertEqual(rc, 1)
        self.assertEqual(sleep.call_args_list, [mock.call(2.0), mock.call(2.0)])
        enable_ssh.assert_not_called()
        self.assertFalse(config_path.exists())
        error = collector.events_of_type("error")[0]
        self.assertEqual(error["code"], "remote_error")
        self.assertEqual(error["recovery"]["title"], "AirPort not reachable at this address")
        self.assertEqual(error["recovery"]["localization_key"], "configure.remote_error.acp_port_probe")
        self.assertEqual(
            error["recovery"]["actions"][0],
            "Disable VPN or security software that routes local network traffic, then try again.",
        )
        self.assertIn("No AirPort ACP service responded", error["message"])
        self.assertIn("Check the device IP address or hostname", error["message"])
        telemetry_error = self._telemetry_client.emit.call_args_list[-1].kwargs["error"]
        self.assertIn("acp_port_probe_attempts=3", telemetry_error)
        self.assertIn("acp_port_probe_last_error=Connection refused", telemetry_error)

    def test_configure_acp_probe_failure_telemetry_says_where_the_device_is(self) -> None:
        # A reset AirPort serving its own 10.0.1.0/24 while this Mac is on another LAN.
        record = {
            "name": "Time Capsule b67fdb",
            "hostname": "Time-Capsule-b67fdb.local",
            "service_type": "_airport._tcp.local.",
            "port": 5009,
            "ipv4": ["10.0.1.1", "169.254.155.34"],
            "ipv6": ["fe80::7273:cbff:feb2:71a2%en0"],
            "properties": {
                "syAP": "116", "raNm": "Apple Network b67fdb", "raNA": "1", "prob": "waCF;opNW;pubP;+",
                "waMA": "70-73-CB-B2-71-A2",
            },
            "fullname": "Time Capsule b67fdb._airport._tcp.local.",
        }
        mac_lan = (LocalInterfaceNetwork("en0", "192.168.1.170", ipaddress.ip_network("192.168.1.0/24")),)
        browse_diagnostics = BonjourQueryDiagnostics("zeroconf", ["_airport._tcp.local."], 2, 2, 0, 0)
        collector = CollectingSink()
        with tempfile.TemporaryDirectory() as tmp:
            params = {"config": str(Path(tmp) / ".env"), "selected_record": record, "password": "pw"}
            params["confirmation_id"] = self.confirmation_id_for(
                "configure",
                params,
                {"host": "root@10.0.1.1", "device_name": "Time Capsule b67fdb", "requires_reboot": True},
            )
            with mock.patch("timecapsulesmb.app.ops.configure.probe_connection_state", return_value=unreachable_probed_state()), \
                    mock.patch("timecapsulesmb.services.acp_ssh.tcp_connect_error", return_value="timed out"), \
                    mock.patch("timecapsulesmb.services.acp_ssh.time.sleep"), \
                    mock.patch("timecapsulesmb.services.acp_diagnostics.local_interface_networks", return_value=mac_lan), \
                    mock.patch("timecapsulesmb.services.acp_ssh.local_lan_networks", return_value=mac_lan), \
                    mock.patch("timecapsulesmb.services.acp_ssh.sys.platform", "darwin"), \
                    mock.patch(
                        "timecapsulesmb.services.acp_diagnostics.select_route_to_address",
                        return_value=RouteSelection("available", source="192.168.1.170"),
                    ), \
                    mock.patch(
                        "timecapsulesmb.discovery.bonjour.BonjourQuery.browse",
                        return_value=(BonjourDiscoverySnapshot([], []), browse_diagnostics),
                    ):
                rc = service.run_api_request({"operation": "configure", "params": params}, collector.sink)

        self.assertEqual(rc, 1)
        error = self.assert_single_terminal_event(collector, "error")
        self.assertEqual(error["code"], "device_off_network")
        self.assertEqual(
            error["message"],
            "Could not connect to ACP on 10.0.1.1:5009. 10.0.1.1 is not on this Mac's network (192.168.1.0/24). "
            "Check the address, or connect this Mac to the device's network by Wi-Fi or one of its LAN ports, "
            "then try again.",
        )
        self.assertEqual(error["recovery"]["localization_key"], "device_off_network")
        finished = self._telemetry_client.emit.call_args_list[-1].kwargs
        self.assertEqual(finished["error_code"], "device_off_network")
        self.assertEqual(finished["stage"], "acp_port_probe")
        self.assertFalse(finished["acp_port_probe_succeeded"])
        self.assertEqual(finished["acp_port_probe_error_kinds"], ["timeout"] * 3)
        target = finished["acp_target_addresses"][0]
        self.assertEqual(
            (target["role"], target["address"], target["link"], target["route"]),
            ("target", "10.0.1.1", "off_link", "gateway"),
        )
        self.assertEqual(finished["local_networks"][0]["networks"][0]["network"], "192.168.1.0/24")
        self.assertTrue(finished["acp_record_flags"]["default_network_name"])
        self.assertNotIn("Apple Network", json.dumps(finished["acp_record_flags"]))
        # The device was looked for by its AirPort MAC, and is not on this network.
        follow = finished["execution"]["measurements"]["host_follow"][0]
        self.assertEqual(
            {key: follow[key] for key in ("trigger", "result", "candidates", "from_scope")},
            {"trigger": "acp_unreachable", "result": "not_found", "candidates": 0, "from_scope": "private"},
        )
        self.assertNotIn("192.168.1.170", json.dumps(finished, default=str))

    def test_configure_aborts_on_denied_macos_local_network_preflight_and_emits_telemetry(self) -> None:
        collector = CollectingSink()
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / ".env"
            rc = service.run_api_request(
                {
                    "operation": "configure",
                    "params": {
                        "config": str(config_path),
                        "host": "root@10.0.0.99",
                        "password": "pw",
                        "macos_local_network_preflight_result": "denied",
                        "macos_local_network_preflight_duration_ms": 7,
                        "macos_local_network_preflight_service": "_airport._tcp",
                        "macos_local_network_preflight_error": "policy denied",
                    },
                },
                collector.sink,
            )

        self.assertEqual(rc, 1)
        self.assertFalse(config_path.exists())
        stages = collector.events_of_type("stage")
        self.assertEqual(stages[0]["stage"], "local_network_preflight")
        error = collector.events_of_type("error")[0]
        self.assertEqual(error["code"], "local_network_permission_denied")
        self.assertIn("open_system_settings", error["recovery"]["action_ids"])
        telemetry = self._telemetry_client.emit.call_args_list[-1].kwargs
        self.assertEqual(telemetry["stage"], "local_network_preflight")
        self.assertEqual(telemetry["options"]["macos_local_network_preflight_result"], "denied")
        telemetry_error = telemetry["error"]
        self.assertIn("macos_local_network_preflight_result=denied", telemetry_error)
        self.assertIn("macos_local_network_preflight_error=policy denied", telemetry_error)

    def test_configure_acp_port_errno65_on_macos_gui_marks_local_network_privacy_suspected(self) -> None:
        collector = CollectingSink()
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / ".env"
            params = {
                "config": str(config_path),
                "host": "root@10.0.0.99",
                "password": "pw",
            }
            params["confirmation_id"] = self.confirmation_id_for(
                "configure",
                params,
                {
                    "host": "root@10.0.0.99",
                    "device_name": "10.0.0.99",
                    "requires_reboot": True,
                },
            )
            with mock.patch("timecapsulesmb.app.ops.configure.probe_connection_state", return_value=unreachable_probed_state()), \
                    mock.patch("timecapsulesmb.services.acp_ssh.local_lan_networks", return_value=(LocalInterfaceNetwork("en0", "10.0.0.50", ipaddress.ip_network("10.0.0.0/24")),)):
                with mock.patch("timecapsulesmb.services.acp_ssh.sys.platform", "darwin"):
                    with mock.patch.dict(os.environ, {"TCAPSULE_CLIENT": "macos_gui"}):
                        with mock.patch("timecapsulesmb.services.acp_ssh.tcp_connect_error", return_value="[Errno 65] No route to host"):
                            with mock.patch("timecapsulesmb.services.acp_ssh.time.sleep"):
                                rc = service.run_api_request(
                                    {
                                        "operation": "configure",
                                        "params": params,
                                    },
                                    collector.sink,
                                )

        self.assertEqual(rc, 1)
        error = collector.events_of_type("error")[0]
        self.assertEqual(error["code"], "remote_error")
        telemetry_error = self._telemetry_client.emit.call_args_list[-1].kwargs["error"]
        self.assertIn("acp_port_probe_last_error=[Errno 65] No route to host", telemetry_error)
        self.assertIn("macos_local_network_privacy_suspected=true", telemetry_error)
        self.assertIn("macos_local_network_privacy_signal=errno65_no_route_to_host", telemetry_error)

    def test_configure_reports_unsupported_device(self) -> None:
        collector = CollectingSink()
        unsupported_state = ProbedDeviceState(
            probe_result=probed_state().probe_result,
            compatibility=unsupported_compatibility(),
        )
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / ".env"
            with mock.patch("timecapsulesmb.app.ops.configure.probe_connection_state", return_value=unsupported_state):
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

    def test_configure_rejects_unsupported_selected_record_syap_before_probe_or_confirmation(self) -> None:
        collector = CollectingSink()
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / ".env"
            with mock.patch("timecapsulesmb.app.ops.configure.probe_connection_state") as probe:
                with mock.patch("timecapsulesmb.services.acp_ssh.set_dbug") as enable_ssh:
                    rc = service.run_api_request(
                        {
                            "operation": "configure",
                            "params": {
                                "config": str(config_path),
                                "selected_record": {
                                    "name": "Living Room Express",
                                    "hostname": "living-room-express.local.",
                                    "service_type": "_airport._tcp.local.",
                                    "port": 5009,
                                    "ipv4": ["10.0.0.40"],
                                    "ipv6": [],
                                    "properties": {"syAP": "115"},
                                    "fullname": "Living Room Express._airport._tcp.local.",
                                },
                                "password": "pw",
                            },
                        },
                        collector.sink,
                    )

        self.assertEqual(rc, 1)
        self.assertFalse(config_path.exists())
        probe.assert_not_called()
        enable_ssh.assert_not_called()
        # The model check needs the resolved target, so the record's address
        # was tried first: a TCP connect to the ACP port, no ACP message.
        self._record_acp_probe.assert_called_once_with("10.0.0.40", 5009)
        self.assertEqual(collector.events_of_type("confirmation_required"), [])
        error = self.assert_single_terminal_event(collector, "error")
        self.assertEqual(error["code"], "unsupported_device")
        self.assertIn("syAP 115", error["message"])
        self.assertEqual(error["recovery"]["localization_key"], "configure.unsupported_device")
        self.assertFalse(error["recovery"]["retryable"])
        self.assertEqual(error["debug"]["discovered_airport_syap"], "115")
        self.assertEqual(error["debug"]["stage"], "check_device_model")

    def test_configure_rejects_airport_express_found_by_probe(self) -> None:
        collector = CollectingSink()
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / ".env"
            with mock.patch(
                "timecapsulesmb.app.ops.configure.probe_connection_state",
                return_value=airport_express_probed_state(),
            ):
                rc = service.run_api_request(
                    {
                        "operation": "configure",
                        "params": {"config": str(config_path), "host": "root@10.0.0.40", "password": "pw"},
                    },
                    collector.sink,
                )

        self.assertEqual(rc, 1)
        self.assertFalse(config_path.exists())
        error = self.assert_single_terminal_event(collector, "error")
        self.assertEqual(error["code"], "unsupported_device")
        self.assertIn("ar7240 processor", error["message"])

    def test_configure_ssh_enable_timeout_uses_specific_gui_error_code(self) -> None:
        collector = CollectingSink()
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / ".env"
            params = {
                "config": str(config_path),
                "host": "root@10.0.0.2",
                "password": "secret",
            }
            params["confirmation_id"] = self.confirmation_id_for(
                "configure",
                params,
                {
                    "host": "root@10.0.0.2",
                    "device_name": "10.0.0.2",
                    "requires_reboot": True,
                },
            )
            with mock.patch("timecapsulesmb.app.ops.configure.probe_connection_state", return_value=unreachable_probed_state()):
                with mock.patch("timecapsulesmb.services.configure.enable_ssh_and_reprobe", side_effect=lambda connection, **_kwargs: (connection, None)):
                    rc = service.run_api_request(
                        {
                            "operation": "configure",
                            "params": params,
                        },
                        collector.sink,
                    )

        self.assertEqual(rc, 1)
        self.assertFalse(config_path.exists())
        error = self.assert_single_terminal_event(collector, "error")
        self.assertEqual(error["code"], "ssh_enable_timeout")
        self.assertEqual(error["message"], "SSH did not open after enabling via ACP.")
        self.assertEqual(error["recovery"]["localization_key"], "ssh_enable_timeout")
        self.assertTrue(error["recovery"]["retryable"])
        self.assertNotIn("secret", json.dumps(collector.events))

    def test_configure_records_the_ssh_enable_reboot_cycle_in_telemetry(self) -> None:
        # The real enable-and-reboot runs against the simulated device; only the
        # ACP write is stubbed. SSH never opens on the new boot.
        collector = CollectingSink()
        device = FakeAcpDevice(ssh_open=False, ssh_up_after_boot=None)
        with tempfile.TemporaryDirectory() as tmp:
            params = {"config": str(Path(tmp) / ".env"), "host": "root@10.0.0.2", "password": "secret"}
            params["confirmation_id"] = self.confirmation_id_for(
                "configure",
                params,
                {"host": "root@10.0.0.2", "device_name": "10.0.0.2", "requires_reboot": True},
            )
            with mock.patch("timecapsulesmb.app.ops.configure.probe_connection_state", return_value=unreachable_probed_state()):
                with mock.patch("timecapsulesmb.services.configure.enable_ssh_with_port_preflight", side_effect=lambda host, *_args, **_kwargs: host):
                    with device.patched():
                        rc = service.run_api_request({"operation": "configure", "params": params}, collector.sink)

        self.assertEqual(rc, 1)
        self.assertEqual(self.assert_single_terminal_event(collector, "error")["code"], "ssh_enable_timeout")
        finished = self._telemetry_client.emit.call_args_list[-1].kwargs
        self.assertEqual(finished["phase"], "finished")
        cycle = finished["execution"]["measurements"]["reboot_cycle"][0]
        self.assertEqual(cycle["result"], "ssh_not_open")
        self.assertIn("reset_seen_after_sec", cycle)
        self.assertEqual(finished["execution"]["measurements"]["reboot_request"][0]["strategy"], "network_acp")

    def test_doctor_streams_check_events(self) -> None:
        collector = CollectingSink()
        config = AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"})

        def fake_run_doctor_checks(*_args, **kwargs):
            kwargs["on_result"](CheckResult("PASS", "smbd is bound to TCP 445", {"port": 445}))
            return [CheckResult("PASS", "smbd is bound to TCP 445", {"port": 445})], False

        with mock.patch("timecapsulesmb.app.ops.common.load_env_config", return_value=config):
            with mock.patch("timecapsulesmb.app.ops.doctor.resolve_app_paths", return_value=SimpleNamespace(distribution_root=REPO_ROOT)):
                with mock.patch("timecapsulesmb.app.ops.common.resolve_env_connection", return_value=SshConnection("root@10.0.0.2", "pw", "-o foo")):
                    with mock.patch("timecapsulesmb.app.ops.doctor.run_doctor_checks", side_effect=fake_run_doctor_checks):
                        rc = service.run_api_request({"operation": "doctor", "params": {}}, collector.sink)

        self.assertEqual(rc, 0)
        checks = collector.events_of_type("check")
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0]["status"], "PASS")
        self.assertEqual(checks[0]["details"], {"port": 445})

    def test_doctor_ignores_legacy_bonjour_timeout_param(self) -> None:
        collector = CollectingSink()
        config = AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"})

        with mock.patch("timecapsulesmb.app.ops.common.load_env_config", return_value=config):
            with mock.patch("timecapsulesmb.app.ops.doctor.resolve_app_paths", return_value=SimpleNamespace(distribution_root=REPO_ROOT)):
                with mock.patch("timecapsulesmb.app.ops.common.resolve_env_connection", return_value=SshConnection("root@10.0.0.2", "pw", "-o foo")):
                    with mock.patch("timecapsulesmb.app.ops.doctor.run_doctor_checks", return_value=([], False)) as checks:
                        rc = service.run_api_request(
                            {"operation": "doctor", "params": {"bonjour_timeout": "2.75"}},
                            collector.sink,
                        )

        self.assertEqual(rc, 0)
        self.assertNotIn("bonjour_timeout", checks.call_args.kwargs)
        self.assertIs(checks.call_args.kwargs["startup_grace"], True)

    def test_doctor_can_disable_startup_grace_from_request_params(self) -> None:
        collector = CollectingSink()
        config = AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"})

        with mock.patch("timecapsulesmb.app.ops.common.load_env_config", return_value=config):
            with mock.patch("timecapsulesmb.app.ops.doctor.resolve_app_paths", return_value=SimpleNamespace(distribution_root=REPO_ROOT)):
                with mock.patch("timecapsulesmb.app.ops.common.resolve_env_connection", return_value=SshConnection("root@10.0.0.2", "pw", "-o foo")):
                    with mock.patch("timecapsulesmb.app.ops.doctor.run_doctor_checks", return_value=([], False)) as checks:
                        rc = service.run_api_request(
                            {"operation": "doctor", "params": {"startup_grace": False}},
                            collector.sink,
                        )

        self.assertEqual(rc, 0)
        self.assertIs(checks.call_args.kwargs["startup_grace"], False)

    def test_doctor_uses_request_credentials_without_requiring_saved_password(self) -> None:
        collector = CollectingSink()
        config = AppConfig.from_values(
            {
                "TC_HOST": "root@10.0.0.2",
                "TC_SSH_OPTS": "-o foo",
            },
            file_values={
                "TC_HOST": "root@10.0.0.2",
                "TC_SSH_OPTS": "-o foo",
            },
        )

        def fake_run_doctor_checks(config_arg, **_kwargs):
            self.assertEqual(config_arg.get("TC_PASSWORD"), "keychain-pw")
            self.assertFalse(config_arg.has_file_value("TC_PASSWORD"))
            return [], False

        with mock.patch("timecapsulesmb.app.ops.common.load_env_config", return_value=config):
            with mock.patch("timecapsulesmb.app.ops.doctor.resolve_app_paths", return_value=SimpleNamespace(distribution_root=REPO_ROOT)):
                with mock.patch("timecapsulesmb.app.ops.doctor.run_doctor_checks", side_effect=fake_run_doctor_checks):
                    rc = service.run_api_request(
                        {
                            "operation": "doctor",
                            "params": {
                                "skip_ssh": True,
                                "credentials": {"password": "keychain-pw"},
                            },
                        },
                        collector.sink,
                    )

        self.assertEqual(rc, 0)
        result = self.assert_single_terminal_event(collector, "result")
        self.assertTrue(result["ok"])
        self.assertNotIn("keychain-pw", json.dumps(collector.events))

    def test_doctor_fatal_returns_nonzero_result_without_error_event(self) -> None:
        collector = CollectingSink()
        config = AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"})

        def fake_run_doctor_checks(*_args, **kwargs):
            kwargs["on_result"](CheckResult("FAIL", "SMB is not reachable", {"password": "pw"}))
            return [CheckResult("FAIL", "SMB is not reachable", {"password": "pw"})], True

        with mock.patch("timecapsulesmb.app.ops.common.load_env_config", return_value=config):
            with mock.patch("timecapsulesmb.app.ops.doctor.resolve_app_paths", return_value=SimpleNamespace(distribution_root=REPO_ROOT)):
                with mock.patch("timecapsulesmb.app.ops.common.resolve_env_connection", return_value=SshConnection("root@10.0.0.2", "pw", "-o foo")):
                    with mock.patch("timecapsulesmb.app.ops.doctor.run_doctor_checks", side_effect=fake_run_doctor_checks):
                        rc = service.run_api_request({"operation": "doctor", "params": {}}, collector.sink)

        self.assertEqual(rc, 1)
        self.assertEqual(collector.events_of_type("error"), [])
        result = collector.events_of_type("result")[0]
        self.assertEqual(result["ok"], False)
        self.assertTrue(result["payload"]["fatal"])
        self.assertNotIn("pw", json.dumps(collector.events))

    def test_doctor_telemetry_reports_nbns_subnet_outcome_on_a_passing_run(self) -> None:
        collector = CollectingSink()
        config = AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"})
        nbns_subnet = {
            "client_source": "192.168.24.102",
            "device_subnets": ["192.168.28.0/24"],
            "outcome": "off_subnet",
            "detail": None,
        }

        bonjour_link = {
            "verdict": "separate",
            "source": "device_ifconfig",
            "families": ["ipv4"],
            "device_networks": ["192.168.28.0/24"],
            "local_networks": ["192.168.24.0/24"],
            "detail": None,
            "skipped": ["bonjour"],
        }

        def fake_run_doctor_checks(*_args, **kwargs):
            kwargs["debug_fields"]["nbns_subnet"] = nbns_subnet
            kwargs["debug_fields"]["bonjour_link"] = bonjour_link
            return [], False

        with mock.patch("timecapsulesmb.app.ops.common.load_env_config", return_value=config):
            with mock.patch("timecapsulesmb.app.ops.doctor.resolve_app_paths", return_value=SimpleNamespace(distribution_root=REPO_ROOT)):
                with mock.patch("timecapsulesmb.app.ops.common.resolve_env_connection", return_value=SshConnection("root@10.0.0.2", "pw", "-o foo")):
                    with mock.patch("timecapsulesmb.app.ops.doctor.run_doctor_checks", side_effect=fake_run_doctor_checks):
                        rc = service.run_api_request({"operation": "doctor", "params": {}}, collector.sink)

        self.assertEqual(rc, 0)
        finished = self._telemetry_client.emit.call_args_list[-1].kwargs
        self.assertEqual(finished["nbns_subnet"], nbns_subnet)
        self.assertEqual(finished["bonjour_link"], bonjour_link)
        self.assertIsNone(finished.get("error"))

    def test_doctor_failure_telemetry_includes_shared_debug_context(self) -> None:
        collector = CollectingSink()
        config = AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"})

        def fake_run_doctor_checks(*_args, **kwargs):
            kwargs["debug_fields"]["bonjour_expected"] = {"instance_name": "Home"}
            kwargs["debug_fields"]["bonjour_zeroconf"] = {"instance_count": 0, "ip_version": "V4Only"}
            result = CheckResult("FAIL", "no discovered _smb._tcp instance matched expected device instance 'Home'")
            kwargs["on_result"](result)
            return [result], True

        with mock.patch("timecapsulesmb.app.ops.common.load_env_config", return_value=config):
            with mock.patch("timecapsulesmb.app.ops.doctor.resolve_app_paths", return_value=SimpleNamespace(distribution_root=REPO_ROOT)):
                with mock.patch("timecapsulesmb.app.ops.common.resolve_env_connection", return_value=SshConnection("root@10.0.0.2", "pw", "-o foo")):
                    with mock.patch("timecapsulesmb.app.ops.doctor.run_doctor_checks", side_effect=fake_run_doctor_checks):
                        rc = service.run_api_request({"operation": "doctor", "params": {}}, collector.sink)

        self.assertEqual(rc, 1)
        finished_kwargs = self._telemetry_client.emit.call_args_list[-1].kwargs
        telemetry_error = finished_kwargs["error"]
        self.assertIn("Doctor failures:", telemetry_error)
        self.assertIn("Discovery context:", telemetry_error)
        self.assertIn("Debug context:", telemetry_error)
        self.assertIn("command=doctor", telemetry_error)
        self.assertIn("stage=run_checks", telemetry_error)
        self.assertIn("host=root@10.0.0.2", telemetry_error)
        self.assertIn("TC_HOST=root@10.0.0.2", telemetry_error)
        self.assertIn("bonjour_zeroconf={instance_count:0,ip_version:V4Only}", telemetry_error)
        self.assertNotIn("TC_PASSWORD=pw", telemetry_error)

        payload_error = collector.events_of_type("result")[0]["payload"]["error"]
        self.assertIn("Doctor failures:", payload_error)
        self.assertNotIn("Debug context:", payload_error)

    def test_deploy_dry_run_returns_structured_plan_without_remote_actions(self) -> None:
        collector = CollectingSink()
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")
        target = SimpleNamespace(connection=connection, probe_state=probed_state())
        artifacts = {
            "smbd": SimpleNamespace(absolute_path=REPO_ROOT / "bin/samba4/smbd"),
            "xattr_migrator": SimpleNamespace(absolute_path=REPO_ROOT / "bin/xattr-migrate/xattr-hfs-migrate"),
            "service": SimpleNamespace(absolute_path=REPO_ROOT / "bin/service/service"),
            "rsync": SimpleNamespace(absolute_path=REPO_ROOT / "bin/rsync/rsync"),
        }

        with mock.patch("timecapsulesmb.app.ops.common.load_env_config", return_value=AppConfig.from_values({
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_INTERNAL_SHARE_USE_DISK_ROOT": "true",
            "TC_ANY_PROTOCOL": "true",
            "TC_DEBUG_LOGGING": "true",
        })):
            with mock.patch("timecapsulesmb.app.ops.common.resolve_validated_managed_target", return_value=target):
                with mock.patch("timecapsulesmb.app.ops.deploy.resolve_app_paths", return_value=SimpleNamespace(distribution_root=REPO_ROOT)):
                    with mock.patch("timecapsulesmb.services.deploy.validate_artifacts", return_value=[("smbd", True, "ok")]):
                        with mock.patch("timecapsulesmb.services.deploy.resolve_payload_artifacts", return_value=artifacts):
                            with mock.patch("timecapsulesmb.services.deploy.run_remote_actions", side_effect=AssertionError("dry run should not run remote actions")):
                                rc = service.run_api_request(
                                    {"operation": "deploy", "params": {"dry_run": True, "rsync_enabled": True}},
                                    collector.sink,
                                )

        self.assertEqual(rc, 0)
        result = collector.events_of_type("result")[0]
        self.assertEqual(result["payload"]["host"], "root@10.0.0.2")
        self.assertEqual(result["payload"]["reboot_required"], True)
        self.assertEqual(result["payload"]["requires_reboot"], True)
        self.assertEqual(result["payload"]["startup_mode"], "reboot_then_verify")
        self.assertEqual(result["payload"]["payload_family"], "netbsd6_samba4")
        self.assertEqual(result["payload"]["rsync_enabled"], True)
        self.assertEqual(result["payload"]["schema_version"], 1)

    def test_deploy_dry_run_no_wait_returns_request_only_plan(self) -> None:
        collector = CollectingSink()
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")
        target = SimpleNamespace(connection=connection, probe_state=probed_state())
        artifacts = {
            "smbd": SimpleNamespace(absolute_path=REPO_ROOT / "bin/samba4/smbd"),
            "xattr_migrator": SimpleNamespace(absolute_path=REPO_ROOT / "bin/xattr-migrate/xattr-hfs-migrate"),
            "service": SimpleNamespace(absolute_path=REPO_ROOT / "bin/service/service"),
            "rsync": SimpleNamespace(absolute_path=REPO_ROOT / "bin/rsync/rsync"),
        }

        with mock.patch("timecapsulesmb.app.ops.common.load_env_config", return_value=AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"})):
            with mock.patch("timecapsulesmb.app.ops.common.resolve_validated_managed_target", return_value=target):
                with mock.patch("timecapsulesmb.app.ops.deploy.resolve_app_paths", return_value=SimpleNamespace(distribution_root=REPO_ROOT)):
                    with mock.patch("timecapsulesmb.services.deploy.validate_artifacts", return_value=[("smbd", True, "ok")]):
                        with mock.patch("timecapsulesmb.services.deploy.resolve_payload_artifacts", return_value=artifacts):
                            with mock.patch("timecapsulesmb.services.deploy.run_remote_actions", side_effect=AssertionError("dry run should not run remote actions")):
                                rc = service.run_api_request(
                                    {"operation": "deploy", "params": {"dry_run": True, "no_wait": True}},
                                    collector.sink,
                                )

        self.assertEqual(rc, 0)
        payload = collector.events_of_type("result")[0]["payload"]
        self.assertTrue(payload["reboot_required"])
        self.assertFalse(payload["wait_after_reboot"])
        self.assertEqual(payload["reboot_request"]["follow_up"], ["return_after_reboot_request"])
        self.assertEqual(payload["post_deploy_checks"], [])

    def test_deploy_netbsd4_dry_run_no_wait_does_not_plan_activation(self) -> None:
        collector = CollectingSink()
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")
        target = SimpleNamespace(connection=connection, probe_state=netbsd4_probed_state())
        artifacts = {
            "smbd": SimpleNamespace(absolute_path=REPO_ROOT / "bin/samba4-netbsd4be/smbd"),
            "xattr_migrator": SimpleNamespace(absolute_path=REPO_ROOT / "bin/xattr-migrate/xattr-hfs-migrate"),
            "service": SimpleNamespace(absolute_path=REPO_ROOT / "bin/service-netbsd4be/service"),
            "rsync": SimpleNamespace(absolute_path=REPO_ROOT / "bin/rsync-netbsd4be/rsync"),
        }

        with mock.patch("timecapsulesmb.app.ops.common.load_env_config", return_value=AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"})):
            with mock.patch("timecapsulesmb.app.ops.common.resolve_validated_managed_target", return_value=target):
                with mock.patch("timecapsulesmb.app.ops.deploy.resolve_app_paths", return_value=SimpleNamespace(distribution_root=REPO_ROOT)):
                    with mock.patch("timecapsulesmb.services.deploy.validate_artifacts", return_value=[("smbd", True, "ok")]):
                        with mock.patch("timecapsulesmb.services.deploy.resolve_payload_artifacts", return_value=artifacts):
                            rc = service.run_api_request(
                                {"operation": "deploy", "params": {"dry_run": True, "no_wait": True}},
                                collector.sink,
                            )

        self.assertEqual(rc, 0)
        payload = collector.events_of_type("result")[0]["payload"]
        self.assertEqual(payload["startup_mode"], "reboot_then_activate")
        self.assertFalse(payload["wait_after_reboot"])
        self.assertEqual(payload["activation_actions"], [])
        self.assertEqual(payload["post_deploy_checks"], [])

    def test_deploy_requires_reboot_confirmation_before_remote_actions(self) -> None:
        collector = CollectingSink()
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")
        target = SimpleNamespace(connection=connection, probe_state=probed_state())
        artifacts = {
            "smbd": SimpleNamespace(absolute_path=REPO_ROOT / "bin/samba4/smbd"),
            "xattr_migrator": SimpleNamespace(absolute_path=REPO_ROOT / "bin/xattr-migrate/xattr-hfs-migrate"),
            "service": SimpleNamespace(absolute_path=REPO_ROOT / "bin/service/service"),
            "rsync": SimpleNamespace(absolute_path=REPO_ROOT / "bin/rsync/rsync"),
        }

        with mock.patch("timecapsulesmb.app.ops.common.load_env_config", return_value=AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"})):
            with mock.patch("timecapsulesmb.app.ops.common.resolve_validated_managed_target", return_value=target):
                with mock.patch("timecapsulesmb.app.ops.deploy.resolve_app_paths", return_value=SimpleNamespace(distribution_root=REPO_ROOT)):
                    with mock.patch("timecapsulesmb.services.deploy.validate_artifacts", return_value=[("smbd", True, "ok")]):
                        with mock.patch("timecapsulesmb.services.deploy.resolve_payload_artifacts", return_value=artifacts):
                            with mock.patch("timecapsulesmb.services.deploy.run_remote_actions") as remote_actions:
                                rc = service.run_api_request(
                                    {"operation": "deploy", "params": {"dry_run": False}},
                                    collector.sink,
                                )

        self.assertEqual(rc, 1)
        self.assert_confirmation(
            collector,
            "deploy.reboot",
            {
                "device_name": "Time Capsule",
                "requires_reboot": True,
                "no_wait": False,
                "startup_mode": "reboot_then_verify",
            },
        )
        remote_actions.assert_not_called()

    def run_deploy_with_password_answer(self, matches: bool | None, params: dict[str, object]) -> tuple[CollectingSink, int, mock.Mock, mock.Mock]:
        collector = CollectingSink()
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")
        target = SimpleNamespace(connection=connection, probe_state=probed_state())
        artifacts = {
            "smbd": SimpleNamespace(absolute_path=REPO_ROOT / "bin/samba4/smbd"),
            "xattr_migrator": SimpleNamespace(absolute_path=REPO_ROOT / "bin/xattr-migrate/xattr-hfs-migrate"),
            "service": SimpleNamespace(absolute_path=REPO_ROOT / "bin/service/service"),
            "rsync": SimpleNamespace(absolute_path=REPO_ROOT / "bin/rsync/rsync"),
        }
        compare = acp_password_answer(matches)
        with mock.patch("timecapsulesmb.app.ops.common.load_env_config", return_value=AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"})):
            with mock.patch("timecapsulesmb.app.ops.common.resolve_validated_managed_target", return_value=target):
                with mock.patch("timecapsulesmb.app.ops.deploy.resolve_app_paths", return_value=SimpleNamespace(distribution_root=REPO_ROOT)):
                    with mock.patch("timecapsulesmb.services.deploy.validate_artifacts", return_value=[("smbd", True, "ok")]):
                        with mock.patch("timecapsulesmb.services.deploy.resolve_payload_artifacts", return_value=artifacts):
                            with mock.patch("timecapsulesmb.services.deploy.run_remote_actions") as remote_actions:
                                with mock.patch("timecapsulesmb.device.probe.read_airport_acp", compare):
                                    rc = service.run_api_request({"operation": "deploy", "params": params}, collector.sink)
        return collector, rc, compare, remote_actions

    def test_deploy_refuses_a_password_the_device_would_reject_before_asking(self) -> None:
        # Deploy ends in an ACP reboot. A password SSH accepted on its first 8
        # characters is refused before the confirmation, not after the upload.
        collector, rc, compare, remote_actions = self.run_deploy_with_password_answer(False, {"dry_run": False})

        self.assertEqual(rc, 1)
        error = self.assert_single_terminal_event(collector, "error")
        self.assertEqual(error["code"], "auth_failed")
        self.assertEqual(error["message"], AIRPORT_PASSWORD_MISMATCH_MESSAGE)
        compare.assert_called_once()
        remote_actions.assert_not_called()

    def test_deploy_asks_for_confirmation_when_the_device_cannot_compare_the_password(self) -> None:
        collector, rc, _compare, remote_actions = self.run_deploy_with_password_answer(None, {"dry_run": False})

        self.assertEqual(rc, 1)
        self.assert_confirmation(collector, "deploy.reboot")
        remote_actions.assert_not_called()

    def test_deploy_dry_run_does_not_compare_the_password(self) -> None:
        collector, rc, compare, _remote_actions = self.run_deploy_with_password_answer(False, {"dry_run": True})

        self.assertEqual(rc, 0, collector.events)
        compare.assert_not_called()

    def test_deploy_requires_netbsd4_activation_confirmation_before_remote_actions(self) -> None:
        collector = CollectingSink()
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")
        target = SimpleNamespace(connection=connection, probe_state=netbsd4_probed_state())
        artifacts = {
            "smbd": SimpleNamespace(absolute_path=REPO_ROOT / "bin/samba4-netbsd4be/smbd"),
            "xattr_migrator": SimpleNamespace(absolute_path=REPO_ROOT / "bin/xattr-migrate/xattr-hfs-migrate"),
            "service": SimpleNamespace(absolute_path=REPO_ROOT / "bin/service-netbsd4be/service"),
            "rsync": SimpleNamespace(absolute_path=REPO_ROOT / "bin/rsync-netbsd4be/rsync"),
        }

        with mock.patch("timecapsulesmb.app.ops.common.load_env_config", return_value=AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"})):
            with mock.patch("timecapsulesmb.app.ops.common.resolve_validated_managed_target", return_value=target):
                with mock.patch("timecapsulesmb.app.ops.deploy.resolve_app_paths", return_value=SimpleNamespace(distribution_root=REPO_ROOT)):
                    with mock.patch("timecapsulesmb.services.deploy.validate_artifacts", return_value=[("smbd", True, "ok")]):
                        with mock.patch("timecapsulesmb.services.deploy.resolve_payload_artifacts", return_value=artifacts):
                            with mock.patch("timecapsulesmb.services.storage.wait_for_mast_volumes_conn") as read_mast:
                                with mock.patch("timecapsulesmb.services.deploy.run_remote_actions") as remote_actions:
                                    rc = service.run_api_request(
                                        {"operation": "deploy", "params": {"dry_run": False}},
                                        collector.sink,
                                    )

        self.assertEqual(rc, 1)
        self.assert_confirmation(
            collector,
            "deploy.netbsd4",
            {
                "device_name": "Time Capsule",
                "netbsd4": True,
                "no_wait": False,
                "startup_mode": "reboot_then_activate",
            },
        )
        read_mast.assert_not_called()
        remote_actions.assert_not_called()

    def test_deploy_no_wait_confirmation_uses_reboot_request_copy(self) -> None:
        collector = CollectingSink()
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")
        target = SimpleNamespace(connection=connection, probe_state=probed_state())
        artifacts = {
            "smbd": SimpleNamespace(absolute_path=REPO_ROOT / "bin/samba4/smbd"),
            "xattr_migrator": SimpleNamespace(absolute_path=REPO_ROOT / "bin/xattr-migrate/xattr-hfs-migrate"),
            "service": SimpleNamespace(absolute_path=REPO_ROOT / "bin/service/service"),
            "rsync": SimpleNamespace(absolute_path=REPO_ROOT / "bin/rsync/rsync"),
        }

        with mock.patch("timecapsulesmb.app.ops.common.load_env_config", return_value=AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"})):
            with mock.patch("timecapsulesmb.app.ops.common.resolve_validated_managed_target", return_value=target):
                with mock.patch("timecapsulesmb.app.ops.deploy.resolve_app_paths", return_value=SimpleNamespace(distribution_root=REPO_ROOT)):
                    with mock.patch("timecapsulesmb.services.deploy.validate_artifacts", return_value=[("smbd", True, "ok")]):
                        with mock.patch("timecapsulesmb.services.deploy.resolve_payload_artifacts", return_value=artifacts):
                            with mock.patch("timecapsulesmb.services.storage.wait_for_mast_volumes_conn") as read_mast:
                                with mock.patch("timecapsulesmb.services.deploy.run_remote_actions") as remote_actions:
                                    rc = service.run_api_request(
                                        {"operation": "deploy", "params": {"dry_run": False, "no_wait": True}},
                                        collector.sink,
                                    )

        self.assertEqual(rc, 1)
        self.assert_confirmation(
            collector,
            "deploy.reboot_no_wait",
            {
                "device_name": "Time Capsule",
                "requires_reboot": True,
                "no_wait": True,
                "startup_mode": "reboot_then_verify",
            },
        )
        read_mast.assert_not_called()
        remote_actions.assert_not_called()

    def test_deploy_netbsd4_no_wait_confirmation_does_not_promise_activation(self) -> None:
        collector = CollectingSink()
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")
        target = SimpleNamespace(connection=connection, probe_state=netbsd4_probed_state())
        artifacts = {
            "smbd": SimpleNamespace(absolute_path=REPO_ROOT / "bin/samba4-netbsd4be/smbd"),
            "xattr_migrator": SimpleNamespace(absolute_path=REPO_ROOT / "bin/xattr-migrate/xattr-hfs-migrate"),
            "service": SimpleNamespace(absolute_path=REPO_ROOT / "bin/service-netbsd4be/service"),
            "rsync": SimpleNamespace(absolute_path=REPO_ROOT / "bin/rsync-netbsd4be/rsync"),
        }

        with mock.patch("timecapsulesmb.app.ops.common.load_env_config", return_value=AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"})):
            with mock.patch("timecapsulesmb.app.ops.common.resolve_validated_managed_target", return_value=target):
                with mock.patch("timecapsulesmb.app.ops.deploy.resolve_app_paths", return_value=SimpleNamespace(distribution_root=REPO_ROOT)):
                    with mock.patch("timecapsulesmb.services.deploy.validate_artifacts", return_value=[("smbd", True, "ok")]):
                        with mock.patch("timecapsulesmb.services.deploy.resolve_payload_artifacts", return_value=artifacts):
                            with mock.patch("timecapsulesmb.services.storage.wait_for_mast_volumes_conn") as read_mast:
                                with mock.patch("timecapsulesmb.services.deploy.run_remote_actions") as remote_actions:
                                    rc = service.run_api_request(
                                        {"operation": "deploy", "params": {"dry_run": False, "no_wait": True}},
                                        collector.sink,
                                    )

        self.assertEqual(rc, 1)
        details = self.assert_confirmation(
            collector,
            "deploy.netbsd4_no_wait",
            {
                "device_name": "Time Capsule",
                "netbsd4": True,
                "requires_reboot": True,
                "no_wait": True,
                "startup_mode": "reboot_then_activate",
            },
        )
        self.assertIn("without running Samba activation", details["message"])
        read_mast.assert_not_called()
        remote_actions.assert_not_called()

    def test_deploy_rejects_legacy_no_reboot_before_target_access(self) -> None:
        for dry_run in (False, True):
            for no_wait in (False, True):
                with self.subTest(dry_run=dry_run, no_wait=no_wait):
                    collector = CollectingSink()
                    with mock.patch("timecapsulesmb.app.ops.common.load_env_config") as config:
                        rc = service.run_api_request(
                            {"operation": "deploy", "params": {
                                "dry_run": dry_run, "no_reboot": True, "no_wait": no_wait,
                            }}, collector.sink,
                        )
                    self.assertEqual(rc, 1)
                    error = self.assert_single_terminal_event(collector, "error")
                    self.assertEqual(error["code"], "validation_failed")
                    self.assertEqual(error["recovery"]["title"], "Deployment validation failed")
                    self.assertIn("requires a reboot", error["message"])
                    config.assert_not_called()

    def test_deploy_accepts_backend_confirmation_id_before_remote_writes(self) -> None:
        first = CollectingSink()
        second = CollectingSink()
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")
        target = SimpleNamespace(connection=connection, probe_state=probed_state())
        artifacts = {
            "smbd": SimpleNamespace(absolute_path=REPO_ROOT / "bin/samba4/smbd"),
            "xattr_migrator": SimpleNamespace(absolute_path=REPO_ROOT / "bin/xattr-migrate/xattr-hfs-migrate"),
            "service": SimpleNamespace(absolute_path=REPO_ROOT / "bin/service/service"),
            "rsync": SimpleNamespace(absolute_path=REPO_ROOT / "bin/rsync/rsync"),
        }
        payload_home = build_dry_run_payload_home(MANAGED_PAYLOAD_DIR_NAME)
        base_params = {"dry_run": False, "no_reboot": False, "mount_wait": 30}

        with mock.patch("timecapsulesmb.app.ops.common.load_env_config", return_value=AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"})):
            with mock.patch("timecapsulesmb.app.ops.common.resolve_validated_managed_target", return_value=target):
                with mock.patch("timecapsulesmb.app.ops.deploy.resolve_app_paths", return_value=SimpleNamespace(distribution_root=REPO_ROOT)):
                    with mock.patch("timecapsulesmb.services.deploy.validate_artifacts", return_value=[("smbd", True, "ok")]):
                        with mock.patch("timecapsulesmb.services.deploy.resolve_payload_artifacts", return_value=artifacts):
                            rc = service.run_api_request(
                                {"operation": "deploy", "params": dict(base_params)},
                                first.sink,
                            )

        self.assertEqual(rc, 1)
        confirmation_id = first.events_of_type("error")[0]["details"]["confirmation_id"]

        with mock.patch("timecapsulesmb.app.ops.common.load_env_config", return_value=AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"})):
            with mock.patch("timecapsulesmb.app.ops.common.resolve_validated_managed_target", return_value=target):
                with mock.patch("timecapsulesmb.app.ops.deploy.resolve_app_paths", return_value=SimpleNamespace(distribution_root=REPO_ROOT)):
                    with mock.patch("timecapsulesmb.services.deploy.validate_artifacts", return_value=[("smbd", True, "ok")]):
                        with mock.patch("timecapsulesmb.services.deploy.resolve_payload_artifacts", return_value=artifacts):
                            with mock.patch("timecapsulesmb.services.storage.wait_for_mast_volumes_conn", return_value=SimpleNamespace(volumes=("dk2",), attempts=1, raw_output="")):
                                with mock.patch("timecapsulesmb.services.deploy.select_payload_home_with_diagnostics_conn", return_value=PayloadHomeSelection(payload_home, ())):
                                    with mock.patch("timecapsulesmb.services.deploy.verify_payload_home_conn", return_value=SimpleNamespace(ok=True, detail="ok")):
                                        with mock.patch("timecapsulesmb.services.deploy.upload_deployment_payload") as upload:
                                            with mock.patch("timecapsulesmb.services.deploy.run_remote_actions"):
                                                with mock.patch("timecapsulesmb.services.deploy.flush_remote_filesystem_writes"), mock.patch("timecapsulesmb.services.deploy.reboot_device"):
                                                    with mock.patch("timecapsulesmb.services.runtime_verification.probe_managed_runtime_conn", return_value=managed_runtime_probe()):
                                                        confirmed = dict(base_params)
                                                        confirmed["confirmation_id"] = confirmation_id
                                                        rc = service.run_api_request(
                                                            {"operation": "deploy", "params": confirmed},
                                                            second.sink,
                                                        )

        self.assertEqual(rc, 0)
        self.assertEqual(upload.call_count, 4)
        self.assertEqual(second.events_of_type("error"), [])

    def test_deploy_rejects_boolean_mount_wait_before_remote_connection(self) -> None:
        collector = CollectingSink()

        with mock.patch("timecapsulesmb.app.ops.common.load_env_config") as load_config:
            rc = service.run_api_request(
                {
                    "operation": "deploy",
                    "params": {
                        "dry_run": True,
                        "mount_wait": True,
                    },
                },
                collector.sink,
            )

        self.assertEqual(rc, 1)
        load_config.assert_not_called()
        error = self.assert_single_terminal_event(collector, "error")
        self.assertEqual(error["code"], "validation_failed")
        self.assertIn("mount_wait must be an integer", error["message"])

    def test_deploy_rejects_invalid_ata_overrides_before_remote_connection(self) -> None:
        for field, value, expected in (
            ("ata_idle_seconds", "bad", "ata_idle_seconds must be an integer"),
            ("ata_standby", "bad", "ata_standby must be an integer"),
        ):
            with self.subTest(field=field):
                collector = CollectingSink()
                with mock.patch("timecapsulesmb.app.ops.common.load_env_config") as load_config:
                    rc = service.run_api_request(
                        {
                            "operation": "deploy",
                            "params": {
                                "dry_run": True,
                                field: value,
                            },
                        },
                        collector.sink,
                    )

                self.assertEqual(rc, 1)
                load_config.assert_not_called()
                error = self.assert_single_terminal_event(collector, "error")
                self.assertEqual(error["code"], "validation_failed")
                self.assertIn(expected, error["message"])

    def test_deploy_uploads_and_reboots_with_runtime_settings(self) -> None:
        collector = CollectingSink()
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")
        target = SimpleNamespace(connection=connection, probe_state=probed_state())
        artifacts = {
            "smbd": SimpleNamespace(absolute_path=REPO_ROOT / "bin/samba4/smbd"),
            "xattr_migrator": SimpleNamespace(absolute_path=REPO_ROOT / "bin/xattr-migrate/xattr-hfs-migrate"),
            "service": SimpleNamespace(absolute_path=REPO_ROOT / "bin/service/service"),
            "rsync": SimpleNamespace(absolute_path=REPO_ROOT / "bin/rsync/rsync"),
        }
        payload_home = build_dry_run_payload_home(MANAGED_PAYLOAD_DIR_NAME)
        params = {
            "dry_run": False,
            "no_reboot": False,
            "internal_share_use_disk_root": False,
            "smb_browse_compatibility": True,
            "any_protocol": False,
            "require_smb_encryption": True,
            "force_disable_smb_signing_and_encryption": False,
            "fruit_metadata_netatalk": True,
            "vfs_aio_fork_enabled": True,
            "debug_logging": False,
            "ata_idle_seconds": 0,
            "ata_standby": 0,
        }
        params["confirmation_id"] = self.confirmation_id_for(
            "deploy",
            params,
            {
                "host": "root@10.0.0.2",
                "payload_family": "netbsd6_samba4",
                "netbsd4": False,
                "requires_reboot": True,
                "no_wait": False,
                "startup_mode": "reboot_then_verify",
            },
        )

        with mock.patch("timecapsulesmb.app.ops.common.load_env_config", return_value=AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"})):
            with mock.patch("timecapsulesmb.app.ops.common.resolve_validated_managed_target", return_value=target):
                with mock.patch("timecapsulesmb.app.ops.deploy.resolve_app_paths", return_value=SimpleNamespace(distribution_root=REPO_ROOT)):
                    with mock.patch("timecapsulesmb.services.deploy.validate_artifacts", return_value=[("smbd", True, "ok")]):
                        with mock.patch("timecapsulesmb.services.deploy.resolve_payload_artifacts", return_value=artifacts):
                            with mock.patch("timecapsulesmb.services.storage.wait_for_mast_volumes_conn", return_value=SimpleNamespace(volumes=("dk2",), attempts=1, raw_output="")):
                                with mock.patch("timecapsulesmb.services.deploy.select_payload_home_with_diagnostics_conn", return_value=PayloadHomeSelection(payload_home, ())):
                                    with mock.patch("timecapsulesmb.services.deploy.verify_payload_home_conn", return_value=SimpleNamespace(ok=True, detail="ok")):
                                        with mock.patch("timecapsulesmb.services.deploy.upload_deployment_payload") as upload:
                                            with mock.patch("timecapsulesmb.services.deploy.run_remote_actions") as remote_actions:
                                                with mock.patch("timecapsulesmb.services.deploy.flush_remote_filesystem_writes"), mock.patch("timecapsulesmb.services.deploy.reboot_device") as wait:
                                                    with mock.patch("timecapsulesmb.services.runtime_verification.probe_managed_runtime_conn", return_value=managed_runtime_probe()) as verify_runtime:
                                                        with mock.patch("timecapsulesmb.services.deploy.render_flash_runtime_config", return_value="runtime\n") as render_runtime:
                                                            rc = service.run_api_request(
                                                                {
                                                                    "operation": "deploy",
                                                                    "params": params,
                                                                },
                                                                collector.sink,
                                                            )

        self.assertEqual(rc, 0)
        self.assertEqual(upload.call_count, 4)
        upload_sources = upload.call_args.kwargs["source_resolver"]
        self.assertIn("packaged:boot.sh", upload_sources)
        self.assertIn("binary:service", upload_sources)
        self.assertNotIn("packaged:manager.sh", upload_sources)
        self.assertNotIn("packaged:start-samba.sh", upload_sources)
        self.assertNotIn("packaged:watchdog.sh", upload_sources)
        self.assertEqual(remote_actions.call_count, 7)
        wait.assert_called_once()
        verify_runtime.assert_called_once()
        render_runtime.assert_called_once()
        self.assertEqual(render_runtime.call_args.kwargs["internal_share_use_disk_root"], False)
        self.assertEqual(render_runtime.call_args.kwargs["smb_browse_compatibility"], True)
        self.assertEqual(render_runtime.call_args.kwargs["mdns_advertise_afp"], False)
        self.assertEqual(render_runtime.call_args.kwargs["any_protocol"], False)
        self.assertEqual(render_runtime.call_args.kwargs["require_smb_encryption"], True)
        self.assertEqual(render_runtime.call_args.kwargs["force_disable_smb_signing_and_encryption"], False)
        self.assertEqual(render_runtime.call_args.kwargs["fruit_metadata_netatalk"], True)
        self.assertEqual(render_runtime.call_args.kwargs["vfs_aio_fork_enabled"], True)
        self.assertEqual(render_runtime.call_args.kwargs["debug_logging"], False)
        self.assertEqual(render_runtime.call_args.kwargs["ata_idle_seconds"], 0)
        self.assertEqual(render_runtime.call_args.kwargs["ata_standby"], 0)
        self.assertEqual(collector.events_of_type("result")[0]["payload"]["rebooted"], True)
        self.assertEqual(collector.events_of_type("result")[0]["payload"]["verified"], True)

    def test_deploy_emits_grouped_upload_stages_before_each_upload_group(self) -> None:
        collector = CollectingSink()
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")
        target = SimpleNamespace(connection=connection, probe_state=probed_state())
        artifacts = {
            "smbd": SimpleNamespace(absolute_path=REPO_ROOT / "bin/samba4/smbd"),
            "xattr_migrator": SimpleNamespace(absolute_path=REPO_ROOT / "bin/xattr-migrate/xattr-hfs-migrate"),
            "service": SimpleNamespace(absolute_path=REPO_ROOT / "bin/service/service"),
            "rsync": SimpleNamespace(absolute_path=REPO_ROOT / "bin/rsync/rsync"),
        }
        payload_home = build_dry_run_payload_home(MANAGED_PAYLOAD_DIR_NAME)
        params = {"dry_run": False, "no_reboot": False}
        params["confirmation_id"] = self.confirmation_id_for(
            "deploy",
            params,
            {
                "host": "root@10.0.0.2",
                "payload_family": "netbsd6_samba4",
                "netbsd4": False,
                "requires_reboot": True,
                "no_wait": False,
                "startup_mode": "reboot_then_verify",
            },
        )

        def fake_upload(plan, *, connection, source_resolver, on_uploading=None, on_uploaded=None):
            for transfer in plan.uploads:
                if on_uploading is not None:
                    on_uploading(transfer)

        with mock.patch("timecapsulesmb.app.ops.common.load_env_config", return_value=AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"})):
            with mock.patch("timecapsulesmb.app.ops.common.resolve_validated_managed_target", return_value=target):
                with mock.patch("timecapsulesmb.app.ops.deploy.resolve_app_paths", return_value=SimpleNamespace(distribution_root=REPO_ROOT)):
                    with mock.patch("timecapsulesmb.services.deploy.validate_artifacts", return_value=[("smbd", True, "ok")]):
                        with mock.patch("timecapsulesmb.services.deploy.resolve_payload_artifacts", return_value=artifacts):
                            with mock.patch("timecapsulesmb.services.storage.wait_for_mast_volumes_conn", return_value=SimpleNamespace(volumes=("dk2",), attempts=1, raw_output="")):
                                with mock.patch("timecapsulesmb.services.deploy.select_payload_home_with_diagnostics_conn", return_value=PayloadHomeSelection(payload_home, ())):
                                    with mock.patch("timecapsulesmb.services.deploy.verify_payload_home_conn", return_value=SimpleNamespace(ok=True, detail="ok")):
                                        with mock.patch("timecapsulesmb.services.deploy.upload_deployment_payload", side_effect=fake_upload):
                                            with mock.patch("timecapsulesmb.services.deploy.run_remote_actions"):
                                                with mock.patch("timecapsulesmb.services.deploy.flush_remote_filesystem_writes"), mock.patch("timecapsulesmb.services.deploy.reboot_device"):
                                                    with mock.patch("timecapsulesmb.services.runtime_verification.probe_managed_runtime_conn", return_value=managed_runtime_probe()):
                                                        rc = service.run_api_request(
                                                            {
                                                                "operation": "deploy",
                                                                "params": params,
                                                            },
                                                            collector.sink,
                                                        )

        self.assertEqual(rc, 0)
        stages = [event["stage"] for event in collector.events_of_type("stage")]
        upload_stages = [stage for stage in stages if str(stage).startswith("upload_")]
        self.assertEqual(
            upload_stages,
            [
                "upload_xattr_migrator",
                "upload_smbd",
                "upload_rsync",
                "upload_boot_files",
                "upload_runtime_config",
            ],
        )

        self.assertGreater(stages.index("enable_boot"), stages.index("migrate_xattrs_cleanup"))
        # Every deploy stage carries its policy, so the app always has a risk,
        # a cancel flag and a fallback description for the running stage.
        for event in collector.events_of_type("stage"):
            with self.subTest(stage=event["stage"]):
                self.assertIn(event["risk"], {"local_read", "local_write", "remote_read", "remote_write", "destructive", "reboot"})
                self.assertIsInstance(event["cancellable"], bool)
                self.assertTrue(event["description"])
        for stage in ("inventory_legacy_metadata", "replace_software", "install_runtime_config"):
            self.assertIn(stage, stages)

    def test_deploy_failure_reports_the_risk_of_the_failing_stage(self) -> None:
        # The stage before each of these has a different risk (build_deployment_plan
        # is local_read, migrate_xattrs_copy is remote_write and
        # migrate_xattrs_cleanup is destructive), so a stage without its own policy
        # would report the previous stage's risk.
        cases = {
            "inventory_legacy_metadata": "remote_read",
            "replace_software": "remote_write",
            "install_runtime_config": "remote_write",
        }
        for failing_stage, expected_risk in cases.items():
            with self.subTest(stage=failing_stage):
                self._telemetry_client.emit.reset_mock()
                collector = CollectingSink()
                connection = SshConnection("root@10.0.0.2", "pw", "-o foo")
                target = SimpleNamespace(connection=connection, probe_state=probed_state())
                artifacts = {
                    "smbd": SimpleNamespace(absolute_path=REPO_ROOT / "bin/samba4/smbd"),
                    "xattr_migrator": SimpleNamespace(absolute_path=REPO_ROOT / "bin/xattr-migrate/xattr-hfs-migrate"),
                    "service": SimpleNamespace(absolute_path=REPO_ROOT / "bin/service/service"),
                    "rsync": SimpleNamespace(absolute_path=REPO_ROOT / "bin/rsync/rsync"),
                }
                payload_home = build_dry_run_payload_home(MANAGED_PAYLOAD_DIR_NAME)
                params = {"dry_run": False, "no_reboot": False}
                params["confirmation_id"] = self.confirmation_id_for(
                    "deploy",
                    params,
                    {
                        "host": "root@10.0.0.2",
                        "payload_family": "netbsd6_samba4",
                        "netbsd4": False,
                        "requires_reboot": True,
                        "no_wait": False,
                        "startup_mode": "reboot_then_verify",
                    },
                )

                def fail_in_stage(*_args, **_kwargs):
                    if collector.sink.current_stage("deploy") == failing_stage:
                        raise RuntimeError(f"{failing_stage} failed")

                from tests.test_xattr_migration import fake_inventory

                def inventory(*_args):
                    fail_in_stage()
                    return fake_inventory()

                with mock.patch("timecapsulesmb.app.ops.common.load_env_config", return_value=AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"})):
                    with mock.patch("timecapsulesmb.app.ops.common.resolve_validated_managed_target", return_value=target):
                        with mock.patch("timecapsulesmb.app.ops.deploy.resolve_app_paths", return_value=SimpleNamespace(distribution_root=REPO_ROOT)):
                            with mock.patch("timecapsulesmb.services.deploy.validate_artifacts", return_value=[("smbd", True, "ok")]):
                                with mock.patch("timecapsulesmb.services.deploy.resolve_payload_artifacts", return_value=artifacts):
                                    with mock.patch("timecapsulesmb.services.storage.wait_for_mast_volumes_conn", return_value=SimpleNamespace(volumes=("dk2",), attempts=1, raw_output="")):
                                        with mock.patch("timecapsulesmb.services.deploy.select_payload_home_with_diagnostics_conn", return_value=PayloadHomeSelection(payload_home, ())):
                                            with mock.patch("timecapsulesmb.services.deploy.verify_payload_home_conn", return_value=SimpleNamespace(ok=True, detail="ok")):
                                                with mock.patch("timecapsulesmb.services.deploy.inventory_metadata", side_effect=inventory):
                                                    with mock.patch("timecapsulesmb.services.deploy.upload_deployment_payload", side_effect=fail_in_stage):
                                                        with mock.patch("timecapsulesmb.services.deploy.run_remote_actions", side_effect=fail_in_stage):
                                                            with mock.patch("timecapsulesmb.services.deploy.reboot_device") as reboot:
                                                                rc = service.run_api_request(
                                                                    {"operation": "deploy", "params": params},
                                                                    collector.sink,
                                                                )

                self.assertNotEqual(rc, 0)
                reboot.assert_not_called()
                error = self.assert_single_terminal_event(collector, "error")
                self.assertIn(f"{failing_stage} failed", json.dumps(error))
                self.assertEqual(collector.events_of_type("stage")[-1]["stage"], failing_stage)
                finished = self._telemetry_client.emit.call_args_list[-1]
                self.assertEqual(finished.args, ("deploy_finished",))
                self.assertEqual(finished.kwargs["stage"], failing_stage)
                self.assertEqual(finished.kwargs["risk"], expected_risk)

    def test_deploy_no_wait_requests_reboot_without_wait_or_runtime_verify(self) -> None:
        collector = CollectingSink()
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")
        target = SimpleNamespace(connection=connection, probe_state=probed_state())
        artifacts = {
            "smbd": SimpleNamespace(absolute_path=REPO_ROOT / "bin/samba4/smbd"),
            "xattr_migrator": SimpleNamespace(absolute_path=REPO_ROOT / "bin/xattr-migrate/xattr-hfs-migrate"),
            "service": SimpleNamespace(absolute_path=REPO_ROOT / "bin/service/service"),
            "rsync": SimpleNamespace(absolute_path=REPO_ROOT / "bin/rsync/rsync"),
        }
        payload_home = build_dry_run_payload_home(MANAGED_PAYLOAD_DIR_NAME)
        params = {"dry_run": False, "no_wait": True}
        params["confirmation_id"] = self.confirmation_id_for(
            "deploy",
            params,
            {
                "host": "root@10.0.0.2",
                "payload_family": "netbsd6_samba4",
                "netbsd4": False,
                "requires_reboot": True,
                "no_wait": True,
                "startup_mode": "reboot_then_verify",
            },
        )

        with mock.patch("timecapsulesmb.app.ops.common.load_env_config", return_value=AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"})):
            with mock.patch("timecapsulesmb.app.ops.common.resolve_validated_managed_target", return_value=target):
                with mock.patch("timecapsulesmb.app.ops.deploy.resolve_app_paths", return_value=SimpleNamespace(distribution_root=REPO_ROOT)):
                    with mock.patch("timecapsulesmb.services.deploy.validate_artifacts", return_value=[("smbd", True, "ok")]):
                        with mock.patch("timecapsulesmb.services.deploy.resolve_payload_artifacts", return_value=artifacts):
                            with mock.patch("timecapsulesmb.services.storage.wait_for_mast_volumes_conn", return_value=SimpleNamespace(volumes=("dk2",), attempts=1, raw_output="")):
                                with mock.patch("timecapsulesmb.services.deploy.select_payload_home_with_diagnostics_conn", return_value=PayloadHomeSelection(payload_home, ())):
                                    with mock.patch("timecapsulesmb.services.deploy.verify_payload_home_conn", return_value=SimpleNamespace(ok=True, detail="ok")):
                                        with mock.patch("timecapsulesmb.services.deploy.upload_deployment_payload"):
                                            with mock.patch("timecapsulesmb.services.deploy.run_remote_actions"):
                                                with mock.patch("timecapsulesmb.services.deploy.flush_remote_filesystem_writes"):
                                                    with mock.patch("timecapsulesmb.services.deploy.reboot_device", side_effect=self.fake_reboot_request) as reboot:
                                                        with mock.patch("timecapsulesmb.services.runtime_verification.probe_managed_runtime_conn") as verify_runtime:
                                                            rc = service.run_api_request(
                                                                {
                                                                    "operation": "deploy",
                                                                    "params": params,
                                                                },
                                                                collector.sink,
                                                            )

        self.assertEqual(rc, 0)
        reboot.assert_called_once()
        self.assertIs(reboot.call_args.kwargs["wait"], False)
        verify_runtime.assert_not_called()
        payload = collector.events_of_type("result")[0]["payload"]
        self.assertEqual(payload["reboot_requested"], True)
        self.assertEqual(payload["waited"], False)
        self.assertEqual(payload["verified"], False)
        finished = self._telemetry_client.emit.call_args_list[-1].kwargs
        self.assertEqual(finished["reboot_was_attempted"], True)
        self.assertEqual(finished["device_came_back_after_reboot"], False)

    def test_deploy_netbsd4_no_wait_requests_reboot_without_activation(self) -> None:
        collector = CollectingSink()
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")
        target = SimpleNamespace(connection=connection, probe_state=netbsd4_probed_state())
        artifacts = {
            "smbd": SimpleNamespace(absolute_path=REPO_ROOT / "bin/samba4-netbsd4be/smbd"),
            "xattr_migrator": SimpleNamespace(absolute_path=REPO_ROOT / "bin/xattr-migrate/xattr-hfs-migrate"),
            "service": SimpleNamespace(absolute_path=REPO_ROOT / "bin/service-netbsd4be/service"),
            "rsync": SimpleNamespace(absolute_path=REPO_ROOT / "bin/rsync-netbsd4be/rsync"),
        }
        payload_home = build_dry_run_payload_home(MANAGED_PAYLOAD_DIR_NAME)
        params = {"dry_run": False, "no_wait": True}
        params["confirmation_id"] = self.confirmation_id_for(
            "deploy",
            params,
            {
                "host": "root@10.0.0.2",
                "payload_family": "netbsd4be_samba4",
                "netbsd4": True,
                "requires_reboot": True,
                "no_wait": True,
                "startup_mode": "reboot_then_activate",
            },
        )

        with mock.patch("timecapsulesmb.app.ops.common.load_env_config", return_value=AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"})):
            with mock.patch("timecapsulesmb.app.ops.common.resolve_validated_managed_target", return_value=target):
                with mock.patch("timecapsulesmb.app.ops.deploy.resolve_app_paths", return_value=SimpleNamespace(distribution_root=REPO_ROOT)):
                    with mock.patch("timecapsulesmb.services.deploy.validate_artifacts", return_value=[("smbd", True, "ok")]):
                        with mock.patch("timecapsulesmb.services.deploy.resolve_payload_artifacts", return_value=artifacts):
                            with mock.patch("timecapsulesmb.services.storage.wait_for_mast_volumes_conn", return_value=SimpleNamespace(volumes=("dk2",), attempts=1, raw_output="")):
                                with mock.patch("timecapsulesmb.services.deploy.select_payload_home_with_diagnostics_conn", return_value=PayloadHomeSelection(payload_home, ())):
                                    with mock.patch("timecapsulesmb.services.deploy.verify_payload_home_conn", return_value=SimpleNamespace(ok=True, detail="ok")):
                                        with mock.patch("timecapsulesmb.services.deploy.upload_deployment_payload"):
                                            with mock.patch("timecapsulesmb.services.deploy.run_remote_actions"):
                                                with mock.patch("timecapsulesmb.services.deploy.flush_remote_filesystem_writes"):
                                                    with mock.patch("timecapsulesmb.services.deploy.reboot_device", side_effect=self.fake_reboot_request) as reboot:
                                                        with mock.patch("timecapsulesmb.services.deploy.start_netbsd4_runtime_after_reboot") as start_runtime:
                                                            rc = service.run_api_request(
                                                                {
                                                                    "operation": "deploy",
                                                                    "params": params,
                                                                },
                                                                collector.sink,
                                                            )

        self.assertEqual(rc, 0)
        self.assertEqual(set(vars(connection)), {"host", "password", "ssh_opts"})
        reboot.assert_called_once()
        start_runtime.assert_not_called()
        payload = collector.events_of_type("result")[0]["payload"]
        self.assertEqual(payload["reboot_requested"], True)
        self.assertEqual(payload["waited"], False)
        self.assertEqual(payload["verified"], False)

    def test_deploy_no_wait_reports_reboot_request_failure(self) -> None:
        collector = CollectingSink()
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")
        target = SimpleNamespace(connection=connection, probe_state=probed_state())
        artifacts = {
            "smbd": SimpleNamespace(absolute_path=REPO_ROOT / "bin/samba4/smbd"),
            "xattr_migrator": SimpleNamespace(absolute_path=REPO_ROOT / "bin/xattr-migrate/xattr-hfs-migrate"),
            "service": SimpleNamespace(absolute_path=REPO_ROOT / "bin/service/service"),
            "rsync": SimpleNamespace(absolute_path=REPO_ROOT / "bin/rsync/rsync"),
        }
        payload_home = build_dry_run_payload_home(MANAGED_PAYLOAD_DIR_NAME)
        params = {"dry_run": False, "no_wait": True}
        params["confirmation_id"] = self.confirmation_id_for(
            "deploy",
            params,
            {
                "host": "root@10.0.0.2",
                "payload_family": "netbsd6_samba4",
                "netbsd4": False,
                "requires_reboot": True,
                "no_wait": True,
                "startup_mode": "reboot_then_verify",
            },
        )

        with mock.patch("timecapsulesmb.app.ops.common.load_env_config", return_value=AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"})):
            with mock.patch("timecapsulesmb.app.ops.common.resolve_validated_managed_target", return_value=target):
                with mock.patch("timecapsulesmb.app.ops.deploy.resolve_app_paths", return_value=SimpleNamespace(distribution_root=REPO_ROOT)):
                    with mock.patch("timecapsulesmb.services.deploy.validate_artifacts", return_value=[("smbd", True, "ok")]):
                        with mock.patch("timecapsulesmb.services.deploy.resolve_payload_artifacts", return_value=artifacts):
                            with mock.patch("timecapsulesmb.services.storage.wait_for_mast_volumes_conn", return_value=SimpleNamespace(volumes=("dk2",), attempts=1, raw_output="")):
                                with mock.patch("timecapsulesmb.services.deploy.select_payload_home_with_diagnostics_conn", return_value=PayloadHomeSelection(payload_home, ())):
                                    with mock.patch("timecapsulesmb.services.deploy.verify_payload_home_conn", return_value=SimpleNamespace(ok=True, detail="ok")):
                                        with mock.patch("timecapsulesmb.services.deploy.upload_deployment_payload"):
                                            with mock.patch("timecapsulesmb.services.deploy.run_remote_actions"):
                                                with mock.patch("timecapsulesmb.services.deploy.flush_remote_filesystem_writes"):
                                                    with mock.patch("timecapsulesmb.services.deploy.reboot_device", side_effect=RebootFlowError("ACP reboot request failed: refused", "remote_error")) as reboot:
                                                        with mock.patch("timecapsulesmb.services.runtime_verification.probe_managed_runtime_conn") as verify_runtime:
                                                            rc = service.run_api_request(
                                                                {
                                                                    "operation": "deploy",
                                                                    "params": params,
                                                                },
                                                                collector.sink,
                                                            )

        self.assertEqual(rc, 1)
        reboot.assert_called_once()
        self.assertIs(reboot.call_args.kwargs["wait"], False)
        verify_runtime.assert_not_called()
        errors = collector.events_of_type("error")
        self.assertEqual(errors[0]["code"], "remote_error")
        self.assertIn("ACP reboot request failed: refused", errors[0]["message"])
        self.assertEqual(collector.events_of_type("result"), [])

    def test_deploy_reboot_up_timeout_uses_app_recovery_guidance(self) -> None:
        collector = CollectingSink()
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")
        target = SimpleNamespace(connection=connection, probe_state=probed_state())
        artifacts = {
            "smbd": SimpleNamespace(absolute_path=REPO_ROOT / "bin/samba4/smbd"),
            "xattr_migrator": SimpleNamespace(absolute_path=REPO_ROOT / "bin/xattr-migrate/xattr-hfs-migrate"),
            "service": SimpleNamespace(absolute_path=REPO_ROOT / "bin/service/service"),
            "rsync": SimpleNamespace(absolute_path=REPO_ROOT / "bin/rsync/rsync"),
        }
        payload_home = build_dry_run_payload_home(MANAGED_PAYLOAD_DIR_NAME)
        params = {"dry_run": False, "no_wait": False}
        params["confirmation_id"] = self.confirmation_id_for(
            "deploy",
            params,
            {
                "host": "root@10.0.0.2",
                "payload_family": "netbsd6_samba4",
                "netbsd4": False,
                "requires_reboot": True,
                "no_wait": False,
                "startup_mode": "reboot_then_verify",
            },
        )

        def reboot_timeout(*_args, callbacks=None, **_kwargs):
            if callbacks is not None:
                callbacks.stage("wait_for_reboot_up")
                callbacks.update(device_came_back_after_reboot=False)
            raise RebootFlowError("Timed out waiting for SSH after reboot.", "reboot_not_finished")

        with mock.patch("timecapsulesmb.app.ops.common.load_env_config", return_value=AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"})):
            with mock.patch("timecapsulesmb.app.ops.common.resolve_validated_managed_target", return_value=target):
                with mock.patch("timecapsulesmb.app.ops.deploy.resolve_app_paths", return_value=SimpleNamespace(distribution_root=REPO_ROOT)):
                    with mock.patch("timecapsulesmb.services.deploy.validate_artifacts", return_value=[("smbd", True, "ok")]):
                        with mock.patch("timecapsulesmb.services.deploy.resolve_payload_artifacts", return_value=artifacts):
                            with mock.patch("timecapsulesmb.services.storage.wait_for_mast_volumes_conn", return_value=SimpleNamespace(volumes=("dk2",), attempts=1, raw_output="")):
                                with mock.patch("timecapsulesmb.services.deploy.select_payload_home_with_diagnostics_conn", return_value=PayloadHomeSelection(payload_home, ())):
                                    with mock.patch("timecapsulesmb.services.deploy.verify_payload_home_conn", return_value=SimpleNamespace(ok=True, detail="ok")):
                                        with mock.patch("timecapsulesmb.services.deploy.upload_deployment_payload"):
                                            with mock.patch("timecapsulesmb.services.deploy.run_remote_actions"):
                                                with mock.patch("timecapsulesmb.services.deploy.flush_remote_filesystem_writes"):
                                                    with mock.patch("timecapsulesmb.services.deploy.reboot_device", side_effect=reboot_timeout) as reboot_wait:
                                                        rc = service.run_api_request(
                                                            {
                                                                "operation": "deploy",
                                                                "params": params,
                                                            },
                                                            collector.sink,
                                                        )

        self.assertEqual(rc, 1)
        reboot_wait.assert_called_once()
        errors = collector.events_of_type("error")
        self.assertEqual(errors[0]["code"], "reboot_not_finished")
        self.assertEqual(errors[0]["message"], "Timed out waiting for SSH after reboot.")
        self.assertNotIn("Run Discover and reselect it", errors[0]["message"])
        self.assertEqual(errors[0]["recovery"]["localization_key"], "deploy.reboot_not_finished")
        self.assertIn(
            "The device may have a new IP address. Run Discover and reselect it.",
            errors[0]["recovery"]["actions"],
        )
        self.assertEqual(collector.events_of_type("result"), [])

    def test_deploy_reboot_records_lifecycle_fields(self) -> None:
        collector = CollectingSink()
        context = AppOperationContext("deploy", collector.sink)
        context.update_fields(reboot_was_attempted=False, device_came_back_after_reboot=False)
        device = FakeAcpDevice()
        with device.patched():
            reboot_device("root@10.0.0.2", "pw", wait=True, callbacks=context.to_operation_callbacks())

        self.assertEqual(device.calls.count("request"), 1)
        self.assertEqual(context.finish_fields["reboot_was_attempted"], True)
        self.assertEqual(context.finish_fields["device_came_back_after_reboot"], True)
        self.assertEqual(context.diagnostics.debug_fields["reboot_request_strategy"], "network_acp")
        self.assertEqual(context.diagnostics.debug_fields["acp_reboot_succeeded"], True)
        execution = context.execution_telemetry(result="success")
        self.assertEqual(execution["measurements"]["reboot_request"][0]["strategy"], "network_acp")
        cycle = execution["measurements"]["reboot_cycle"][0]
        self.assertEqual(cycle["result"], "success")
        self.assertEqual(cycle["u0_sec"], 3600)
        self.assertIn("uptime_at_return_sec", cycle)

    def test_deploy_no_wait_reboot_request_error_is_raised(self) -> None:
        collector = CollectingSink()
        context = AppOperationContext("deploy", collector.sink)
        device = FakeAcpDevice(request_error=ACPConnectionError("ACP receive failed: timed out"))
        with device.patched():
            with self.assertRaises(RebootFlowError) as raised:
                reboot_device("root@10.0.0.2", "pw", wait=False, callbacks=context.to_operation_callbacks())

        self.assertIn("ACP receive failed: timed out", str(raised.exception))
        self.assertEqual(raised.exception.code, "remote_error")

    def run_confirmed_deploy_with_mast(
        self,
        mast_discovery,
        *,
        payload_home_selection: PayloadHomeSelection | None = None,
        select_payload_home_side_effect=None,
        run_remote_actions_side_effect=None,
        upload_side_effect=None,
        probe_state: ProbedDeviceState | None = None,
    ) -> tuple[int, CollectingSink]:
        collector = CollectingSink()
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")
        target = SimpleNamespace(connection=connection, probe_state=probe_state or probed_state())
        artifacts = {
            "smbd": SimpleNamespace(absolute_path=REPO_ROOT / "bin/samba4/smbd"),
            "xattr_migrator": SimpleNamespace(absolute_path=REPO_ROOT / "bin/xattr-migrate/xattr-hfs-migrate"),
            "service": SimpleNamespace(absolute_path=REPO_ROOT / "bin/service/service"),
            "rsync": SimpleNamespace(absolute_path=REPO_ROOT / "bin/rsync/rsync"),
        }
        params = {"dry_run": False}
        params["confirmation_id"] = self.confirmation_id_for(
            "deploy",
            params,
            {
                "host": "root@10.0.0.2",
                "payload_family": "netbsd6_samba4",
                "netbsd4": False,
                "requires_reboot": True,
                "no_wait": False,
                "startup_mode": "reboot_then_verify",
            },
        )

        with mock.patch("timecapsulesmb.app.ops.common.load_env_config", return_value=AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"})):
            with mock.patch("timecapsulesmb.app.ops.common.resolve_validated_managed_target", return_value=target):
                with mock.patch("timecapsulesmb.app.ops.deploy.resolve_app_paths", return_value=SimpleNamespace(distribution_root=REPO_ROOT)):
                    with mock.patch("timecapsulesmb.services.deploy.validate_artifacts", return_value=[("smbd", True, "ok")]):
                        with mock.patch("timecapsulesmb.services.deploy.resolve_payload_artifacts", return_value=artifacts):
                            with mock.patch("timecapsulesmb.services.storage.wait_for_mast_volumes_conn", return_value=mast_discovery):
                                with ExitStack() as stack:
                                    if select_payload_home_side_effect is not None:
                                        stack.enter_context(
                                            mock.patch(
                                                "timecapsulesmb.services.deploy.select_payload_home_with_diagnostics_conn",
                                                side_effect=select_payload_home_side_effect,
                                            )
                                        )
                                    elif payload_home_selection is not None:
                                        stack.enter_context(
                                            mock.patch(
                                                "timecapsulesmb.services.deploy.select_payload_home_with_diagnostics_conn",
                                                return_value=payload_home_selection,
                                            )
                                        )
                                    if run_remote_actions_side_effect is not None:
                                        stack.enter_context(
                                            mock.patch(
                                                "timecapsulesmb.services.deploy.run_remote_actions",
                                                side_effect=run_remote_actions_side_effect,
                                            )
                                        )
                                    if upload_side_effect is not None:
                                        stack.enter_context(
                                            mock.patch(
                                                "timecapsulesmb.services.deploy.upload_deployment_payload",
                                                side_effect=upload_side_effect,
                                            )
                                        )
                                    rc = service.run_api_request(
                                        {
                                            "operation": "deploy",
                                            "params": params,
                                        },
                                        collector.sink,
                                    )
        return rc, collector

    def test_deploy_reports_no_mast_volumes_as_no_disk_code(self) -> None:
        rc, collector = self.run_confirmed_deploy_with_mast(
            SimpleNamespace(volumes=(), attempts=1, raw_output="")
        )

        self.assertEqual(rc, 1)
        error = collector.events_of_type("error")[0]
        self.assertEqual(error["code"], "deploy_no_disk_detected")
        self.assertEqual(error["recovery"]["title"], "No internal disk detected")
        self.assertEqual(error["recovery"]["action_ids"], [])
        self.assertNotIn("nbns_enabled", self._telemetry_factory.call_args.kwargs)
        finished = self._telemetry_client.emit.call_args_list[-1].kwargs
        self.assertEqual(finished["result"], "failure")
        self.assertEqual(finished["stage"], "read_mast")
        self.assertEqual(finished["device_family"], "netbsd6_samba4")
        self.assertEqual(finished["device_os_version"], "NetBSD 6.0 (earmv4)")
        self.assertEqual(finished["device_model"], "TimeCapsule8,119")
        self.assertEqual(finished["device_syap"], "119")
        self.assertNotIn("nbns_enabled", finished)
        self.assertEqual(finished["reboot_was_attempted"], False)
        self.assertEqual(finished["device_came_back_after_reboot"], False)
        self.assertEqual(finished["deploy_startup_mode"], "reboot_then_verify")

    def airport_extreme_probed_state(self) -> ProbedDeviceState:
        state = probed_state()
        compatibility = replace(
            state.compatibility, syap_candidates=("120",), model_candidates=("AirPort7,120",),
        )
        return replace(state, compatibility=compatibility)

    def test_deploy_on_airport_extreme_without_usb_disk_asks_for_a_usb_disk(self) -> None:
        # An Extreme has no internal disk; "No internal disk detected" sent
        # AirPort Extreme owners to reseat a disk they do not have.
        rc, collector = self.run_confirmed_deploy_with_mast(
            SimpleNamespace(volumes=(), attempts=10, raw_output="MaSt=<plist><array/></plist>"),
            probe_state=self.airport_extreme_probed_state(),
        )

        self.assertEqual(rc, 1)
        error = collector.events_of_type("error")[0]
        self.assertEqual(error["code"], "deploy_no_usb_disk_detected")
        self.assertIn("An AirPort Extreme has no internal disk", error["message"])
        self.assertEqual(error["recovery"]["title"], "No USB disk detected")

    def test_deploy_reports_disk_without_hfs_as_no_hfs_partition_code(self) -> None:
        raw_mast_output = """
MaSt = (
    {
        deviceName = "wd0";
        model = "Seagate Expansion HDD";
        size = 8000000000000;
        builtin = true;
        partitions = (
        );
    }
);
"""
        rc, collector = self.run_confirmed_deploy_with_mast(
            MaStDiscoveryResult((), 1, raw_mast_output)
        )

        self.assertEqual(rc, 1)
        error = collector.events_of_type("error")[0]
        self.assertEqual(error["code"], "deploy_no_hfs_partition")
        self.assertEqual(error["recovery"]["title"], "No valid HFS partition")
        self.assertTrue(error["recovery"]["retryable"])
        self.assertEqual(error["recovery"]["actions"][0], "Retry deploy.")
        self.assertEqual(error["recovery"]["action_ids"], [])
        finished = self._telemetry_client.emit.call_args_list[-1].kwargs
        self.assertEqual(finished["result"], "failure")
        self.assertEqual(finished["stage"], "read_mast")

    def test_deploy_reports_each_unusable_volume_cause_with_its_own_code(self) -> None:
        # The app shows its own text for each code, so the cause decides it:
        # an unwritable volume, one the device would not keep mounted, and
        # one that never mounted.
        volume = MaStVolume(
            "wd0",
            "dk2",
            "/Volumes/dk2",
            "Data",
            "f42bdb83-c265-5522-a087-25606a4d0abf",
            True,
            "hfs",
        )
        cases = (
            (VolumeMountResult(True, "use_volume_rcs=0 mounted=yes"), False,
             "deploy_disk_not_writable", "No writable payload volume"),
            (VolumeMountResult(False, "use_volume_rcs=1,1 mounted=yes"), None,
             "deploy_disk_not_confirmed", "Disk not kept mounted"),
            (VolumeMountResult(False, "use_volume_rcs=1,1 mounted=no"), None,
             "deploy_disk_not_mounted", "HFS disk not mounted"),
        )
        for mount, writable, code, title in cases:
            with self.subTest(code=code):
                self._telemetry_client.emit.reset_mock()
                rc, collector = self.run_confirmed_deploy_with_mast(
                    MaStDiscoveryResult((volume,), 1, ""),
                    payload_home_selection=PayloadHomeSelection(None, (PayloadCandidateCheck(volume, mount, writable),)),
                )

                self.assertEqual(rc, 1)
                error = collector.events_of_type("error")[0]
                self.assertEqual(error["code"], code)
                self.assertEqual(error["recovery"]["title"], title)
                self.assertEqual(error["recovery"]["localization_key"], f"deploy.{code}")
                self.assertEqual(error["recovery"]["suggested_operation"], "deploy")
                finished = self._telemetry_client.emit.call_args_list[-1].kwargs
                self.assertEqual(finished["result"], "failure")
                self.assertEqual(finished["stage"], "select_payload_home")

    def test_deploy_reports_write_test_timeout_code(self) -> None:
        volume = MaStVolume(
            "wd0",
            "dk2",
            "/Volumes/dk2",
            "Data",
            "f42bdb83-c265-5522-a087-25606a4d0abf",
            True,
            "hfs",
        )
        rc, collector = self.run_confirmed_deploy_with_mast(
            MaStDiscoveryResult((volume,), 1, ""),
            select_payload_home_side_effect=StorageDeviceError(
                "The disk did not respond when tested. It may be failing or unable to spin up. "
                "Run Disk Repair; if this keeps happening, the disk may need replacing.",
                code="disk_write_test_unresponsive",
            ),
        )

        self.assertEqual(rc, 1)
        error = collector.events_of_type("error")[0]
        self.assertEqual(error["code"], "disk_write_test_unresponsive")
        self.assertIn("The disk did not respond when tested.", error["message"])
        finished = self._telemetry_client.emit.call_args_list[-1].kwargs
        self.assertEqual(finished["result"], "failure")
        self.assertEqual(finished["stage"], "select_payload_home")

    def test_deploy_reports_manager_stop_timeout_code(self) -> None:
        volume = MaStVolume(
            "wd0",
            "dk2",
            "/Volumes/dk2",
            "Data",
            "f42bdb83-c265-5522-a087-25606a4d0abf",
            True,
            "hfs",
        )
        rc, collector = self.run_confirmed_deploy_with_mast(
            MaStDiscoveryResult((volume,), 1, ""),
            payload_home_selection=PayloadHomeSelection(PayloadHome("/Volumes/dk2", "/dev/dk2", ".samba4"), ()),
            run_remote_actions_side_effect=SshError("process manager did not stop"),
        )

        self.assertEqual(rc, 1)
        error = collector.events_of_type("error")[0]
        self.assertEqual(error["code"], "manager_stop_timeout")
        self.assertIn("A service on the device is stuck", error["message"])
        finished = self._telemetry_client.emit.call_args_list[-1].kwargs
        self.assertEqual(finished["result"], "failure")
        self.assertEqual(finished["stage"], "pre_upload_actions")

    def test_deploy_reports_payload_upload_timeout_code(self) -> None:
        volume = MaStVolume(
            "wd0",
            "dk2",
            "/Volumes/dk2",
            "Data",
            "f42bdb83-c265-5522-a087-25606a4d0abf",
            True,
            "hfs",
        )

        def timeout_upload(plan, *, connection, source_resolver, on_uploading=None, on_uploaded=None):
            if plan.uploads == [plan.migration_upload]:
                return
            if on_uploading is not None:
                on_uploading(plan.uploads[0])
            raise SshCommandTimeout("Timed out copying smbd to remote path /Volumes/dk2/.samba4/smbd over SSH")

        rc, collector = self.run_confirmed_deploy_with_mast(
            MaStDiscoveryResult((volume,), 1, ""),
            payload_home_selection=PayloadHomeSelection(PayloadHome("/Volumes/dk2", "/dev/dk2", ".samba4"), ()),
            run_remote_actions_side_effect=lambda *args, **kwargs: None,
            upload_side_effect=timeout_upload,
        )

        self.assertEqual(rc, 1)
        error = collector.events_of_type("error")[0]
        self.assertEqual(error["code"], "payload_upload_timeout")
        self.assertIn("The disk did not respond while copying the SMB payload.", error["message"])
        self.assertEqual(error["debug"]["cause"], "Timed out copying smbd to remote path /Volumes/dk2/.samba4/smbd over SSH")
        finished = self._telemetry_client.emit.call_args_list[-1].kwargs
        self.assertEqual(finished["result"], "failure")
        self.assertEqual(finished["stage"], "upload_smbd")
        self.assertIn("Caused by: Timed out copying smbd to remote path /Volumes/dk2/.samba4/smbd over SSH", finished["error"])

    def test_deploy_writes_disabled_install_telemetry_preference_to_flash_config(self) -> None:
        volume = MaStVolume(
            "wd0",
            "dk2",
            "/Volumes/dk2",
            "Data",
            "f42bdb83-c265-5522-a087-25606a4d0abf",
            True,
            "hfs",
        )
        captured: dict[str, str] = {}

        def capture_then_timeout(plan, *, connection, source_resolver, on_uploading=None, on_uploaded=None):
            captured["flash_config"] = source_resolver[GENERATED_FLASH_CONFIG_SOURCE].read_text()
            raise SshCommandTimeout("Timed out copying deployment payload")

        self._install_identity.return_value = SimpleNamespace(telemetry_enabled=False)
        rc, _collector = self.run_confirmed_deploy_with_mast(
            MaStDiscoveryResult((volume,), 1, ""),
            payload_home_selection=PayloadHomeSelection(PayloadHome("/Volumes/dk2", "/dev/dk2", ".samba4"), ()),
            run_remote_actions_side_effect=lambda *args, **kwargs: None,
            upload_side_effect=capture_then_timeout,
        )

        self.assertEqual(rc, 1)
        self.assertIn("TELEMETRY=false\n", captured["flash_config"])

    def test_activate_requires_explicit_confirmation(self) -> None:
        collector = CollectingSink()

        with mock.patch("timecapsulesmb.app.ops.common.load_env_config", return_value=AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"})):
            with mock.patch("timecapsulesmb.app.ops.common.resolve_validated_managed_target") as resolve_target:
                with mock.patch("timecapsulesmb.services.activation.probe_managed_runtime_conn") as runtime_probe:
                    with mock.patch("timecapsulesmb.services.activation.run_remote_actions") as remote_actions:
                        rc = service.run_api_request({"operation": "activate", "params": {}}, collector.sink)

        self.assertEqual(rc, 1)
        self.assert_confirmation(collector, "activate.netbsd4", {"netbsd4": True})
        resolve_target.assert_not_called()
        runtime_probe.assert_not_called()
        remote_actions.assert_not_called()

    def test_activate_accepts_confirmation_id(self) -> None:
        collector = CollectingSink()
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")
        target = SimpleNamespace(connection=connection, probe_state=netbsd4_probed_state())
        params = {}
        params["confirmation_id"] = self.confirmation_id_for(
            "activate",
            params,
            {
                "host": "root@10.0.0.2",
                "netbsd4": True,
            },
        )

        with mock.patch("timecapsulesmb.app.ops.common.load_env_config", return_value=AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"})):
            with mock.patch("timecapsulesmb.app.ops.common.resolve_validated_managed_target", return_value=target):
                with mock.patch("timecapsulesmb.services.activation.probe_managed_runtime_conn", return_value=managed_runtime_probe(True)):
                    with mock.patch("timecapsulesmb.services.activation.run_remote_actions") as remote_actions:
                        rc = service.run_api_request(
                            {"operation": "activate", "params": params},
                            collector.sink,
                        )

        self.assertEqual(rc, 0)
        result = self.assert_single_terminal_event(collector, "result")
        self.assertEqual(result["payload"]["already_active"], True)
        self.assertEqual(result["payload"]["schema_version"], 1)
        self.assertEqual(result["payload"]["summary"], "NetBSD4 payload was already active.")
        remote_actions.assert_not_called()

    def test_activate_that_runs_actions_reports_the_netbsd4_followup(self) -> None:
        collector = CollectingSink()
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")
        target = SimpleNamespace(connection=connection, probe_state=netbsd4_probed_state())
        params = {}
        params["confirmation_id"] = self.confirmation_id_for(
            "activate", params, {"host": "root@10.0.0.2", "netbsd4": True})

        with mock.patch("timecapsulesmb.app.ops.common.load_env_config", return_value=AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"})):
            with mock.patch("timecapsulesmb.app.ops.common.resolve_validated_managed_target", return_value=target):
                with mock.patch("timecapsulesmb.services.activation.probe_managed_runtime_conn", return_value=managed_runtime_probe(False)):
                    with mock.patch("timecapsulesmb.services.activation.run_remote_actions") as remote_actions:
                        with mock.patch("timecapsulesmb.services.activation.verify_managed_runtime_ready"):
                            rc = service.run_api_request({"operation": "activate", "params": params}, collector.sink)

        self.assertEqual(rc, 0)
        remote_actions.assert_called_once()
        payload = self.assert_single_terminal_event(collector, "result")["payload"]
        self.assertEqual(payload["already_active"], False)
        self.assertEqual(payload["summary_key"], "activation_completed_followup")
        self.assertEqual(payload["summary_args"], [])
        self.assertEqual(payload["summary"], NETBSD4_ACTIVATION_COMPLETED)
        self.assertEqual(payload["message"], NETBSD4_ACTIVATION_COMPLETED)

    def test_activate_rejects_saved_airport_express_before_touching_runtime(self) -> None:
        collector = CollectingSink()
        connection = SshConnection("root@10.0.0.40", "pw", "-o foo")
        target = SimpleNamespace(connection=connection, probe_state=airport_express_probed_state())
        params = {}
        params["confirmation_id"] = self.confirmation_id_for(
            "activate", params, {"host": "root@10.0.0.40", "netbsd4": True})

        with mock.patch("timecapsulesmb.app.ops.common.load_env_config", return_value=AppConfig.from_values({"TC_HOST": "root@10.0.0.40", "TC_PASSWORD": "pw"})):
            with mock.patch("timecapsulesmb.app.ops.common.resolve_validated_managed_target", return_value=target):
                with mock.patch("timecapsulesmb.services.activation.probe_managed_runtime_conn") as runtime_probe:
                    with mock.patch("timecapsulesmb.services.activation.run_remote_actions") as remote_actions:
                        rc = service.run_api_request({"operation": "activate", "params": params}, collector.sink)

        self.assertEqual(rc, 1)
        runtime_probe.assert_not_called()
        remote_actions.assert_not_called()
        error = self.assert_single_terminal_event(collector, "error")
        self.assertEqual(error["code"], "unsupported_device")
        self.assertIn("ar7240 processor", error["message"])
        self.assertEqual(error["recovery"]["localization_key"], "unsupported_device")

    def test_activate_on_netbsd6_reports_unsupported_operation_without_forget_advice(self) -> None:
        collector = CollectingSink()
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")
        target = SimpleNamespace(connection=connection, probe_state=probed_state())
        params = {}
        params["confirmation_id"] = self.confirmation_id_for(
            "activate", params, {"host": "root@10.0.0.2", "netbsd4": True})

        with mock.patch("timecapsulesmb.app.ops.common.load_env_config", return_value=AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"})):
            with mock.patch("timecapsulesmb.app.ops.common.resolve_validated_managed_target", return_value=target):
                with mock.patch("timecapsulesmb.services.activation.run_remote_actions") as remote_actions:
                    rc = service.run_api_request({"operation": "activate", "params": params}, collector.sink)

        self.assertEqual(rc, 1)
        remote_actions.assert_not_called()
        error = self.assert_single_terminal_event(collector, "error")
        self.assertEqual(error["code"], "unsupported_device")
        self.assertIn("only supported for NetBSD4", error["message"])
        self.assert_neutral_unsupported_device_recovery(error)

    def run_deploy_dry_run_against(self, probe_state: ProbedDeviceState, *, acp_error: str | None = None):
        collector = CollectingSink()
        target = SimpleNamespace(connection=SshConnection("root@10.0.0.2", "pw", "-o foo"), probe_state=probe_state)
        config = AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"})
        with mock.patch("timecapsulesmb.app.ops.common.load_env_config", return_value=config):
            with mock.patch("timecapsulesmb.app.ops.common.resolve_validated_managed_target", return_value=target):
                with mock.patch("timecapsulesmb.app.ops.deploy.resolve_app_paths", return_value=SimpleNamespace(distribution_root=REPO_ROOT)):
                    with mock.patch("timecapsulesmb.services.deploy.validate_artifacts", return_value=[("smbd", True, "ok")]):
                        with mock.patch("timecapsulesmb.services.runtime.tcp_connect_error", return_value=acp_error) as acp_probe:
                            with mock.patch("timecapsulesmb.services.deploy.run_remote_actions", side_effect=AssertionError("no remote actions")):
                                rc = service.run_api_request({"operation": "deploy", "params": {"dry_run": True}}, collector.sink)
        return rc, collector, acp_probe

    def assert_not_unsupported_model_guidance(self, error: dict[str, object]) -> None:
        recovery = error["recovery"]
        self.assertTrue(recovery["retryable"])
        self.assertNotIn("cannot run TimeCapsuleSMB", recovery["message"])
        self.assertFalse(any("Forget" in action or "AirPort Express" in action for action in recovery["actions"]))

    def test_deploy_reports_ssh_turned_off_when_acp_still_answers(self) -> None:
        rc, collector, acp_probe = self.run_deploy_dry_run_against(
            failed_probe_state(SshAccessStatus.CLOSED, "SSH is not reachable yet."), acp_error=None,
        )

        self.assertEqual(rc, 1)
        acp_probe.assert_called_once_with("10.0.0.2", 5009, 2.0)
        error = self.assert_single_terminal_event(collector, "error")
        self.assertEqual(error["code"], "ssh_disabled")
        self.assertIn("SSH is turned off on 10.0.0.2", error["message"])
        self.assertEqual(error["recovery"]["localization_key"], "ssh_disabled")
        self.assertEqual(error["recovery"]["action_ids"], ["open_ssh_access"])
        self.assert_not_unsupported_model_guidance(error)
        finished = self._telemetry_client.emit.call_args_list[-1].kwargs
        self.assertEqual((finished["stage"], finished["error_code"]), ("check_compatibility", "ssh_disabled"))

    def test_deploy_reports_unreachable_device_when_neither_ssh_nor_acp_answers(self) -> None:
        rc, collector, _acp_probe = self.run_deploy_dry_run_against(
            failed_probe_state(SshAccessStatus.CLOSED, "SSH is not reachable yet."), acp_error="[Errno 64] Host is down",
        )

        self.assertEqual(rc, 1)
        error = self.assert_single_terminal_event(collector, "error")
        self.assertEqual(error["code"], "device_unreachable")
        self.assertIn("not answering at 10.0.0.2", error["message"])
        self.assertIn("Host is down", error["message"])
        self.assertEqual(error["recovery"]["localization_key"], "device_unreachable")
        self.assert_not_unsupported_model_guidance(error)

    def test_deploy_reports_each_ssh_login_failure_with_its_own_code(self) -> None:
        cases = (
            (SshAccessStatus.AUTH_REJECTED, AUTH_REJECTED_ERROR, "auth_failed"),
            (SshAccessStatus.ALGORITHM_NEGOTIATION_FAILED, "no matching key exchange method found", "ssh_compatibility_failed"),
            (SshAccessStatus.TRANSPORT_FAILED, KEX_CLOSED_ERROR, "ssh_transport_failed"),
            (SshAccessStatus.LOCAL_NETWORK_FILTERED, LOCAL_NETWORK_FILTERED_MESSAGE, "local_network_filtered"),
            (SshAccessStatus.DEVICE_PROBE_FAILED, "Failed to determine remote device OS compatibility.", "device_probe_failed"),
        )
        for status, message, code in cases:
            with self.subTest(code=code):
                self._telemetry_client.emit.reset_mock()
                rc, collector, acp_probe = self.run_deploy_dry_run_against(failed_probe_state(status, message))

                self.assertEqual(rc, 1)
                # Only a closed SSH port needs the ACP check to say why.
                acp_probe.assert_not_called()
                error = self.assert_single_terminal_event(collector, "error")
                self.assertEqual(error["code"], code)
                self.assertEqual(error["message"], message)
                self.assert_not_unsupported_model_guidance(error)

    def test_deploy_still_rejects_an_airport_express_it_logged_in_to(self) -> None:
        rc, collector, acp_probe = self.run_deploy_dry_run_against(airport_express_probed_state())

        self.assertEqual(rc, 1)
        acp_probe.assert_not_called()
        error = self.assert_single_terminal_event(collector, "error")
        self.assertEqual(error["code"], "unsupported_device")
        self.assertIn("ar7240 processor", error["message"])
        self.assertIn("AirPort Express", error["message"])
        self.assertEqual(error["recovery"]["localization_key"], "deploy.unsupported_device")
        self.assertIn("Forget this device", error["recovery"]["actions"][1])

    def test_deploy_reports_a_device_without_a_payload_as_unsupported(self) -> None:
        rc, collector, _acp_probe = self.run_deploy_dry_run_against(
            ProbedDeviceState(probe_result=probed_state().probe_result, compatibility=unsupported_compatibility()),
        )

        self.assertEqual(rc, 1)
        error = self.assert_single_terminal_event(collector, "error")
        self.assertEqual(error["code"], "unsupported_device")
        self.assertIn("Unsupported device OS", error["message"])

    def test_deploy_reports_an_uncoded_device_error_as_a_retryable_remote_error(self) -> None:
        collector = CollectingSink()
        target = SimpleNamespace(connection=SshConnection("root@10.0.0.2", "pw", "-o foo"), probe_state=probed_state())
        config = AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"})
        with mock.patch("timecapsulesmb.app.ops.common.load_env_config", return_value=config):
            with mock.patch("timecapsulesmb.app.ops.common.resolve_validated_managed_target", return_value=target):
                with mock.patch("timecapsulesmb.app.ops.deploy.resolve_app_paths", return_value=SimpleNamespace(distribution_root=REPO_ROOT)):
                    with mock.patch("timecapsulesmb.app.ops.deploy.prepare_deploy_preflight", side_effect=DeviceError("probe output was cut short")):
                        rc = service.run_api_request({"operation": "deploy", "params": {"dry_run": True}}, collector.sink)

        self.assertEqual(rc, 1)
        error = self.assert_single_terminal_event(collector, "error")
        self.assertEqual(error["code"], "remote_error")
        self.assertTrue(error["recovery"]["retryable"])

    def run_activate_against_probe(self, probe_state: ProbedDeviceState, *, acp_error: str | None = None):
        collector = CollectingSink()
        target = SimpleNamespace(connection=SshConnection("root@10.0.0.40", "pw", "-o foo"), probe_state=probe_state)
        params = {"confirmation_id": self.confirmation_id_for("activate", {}, {"host": "root@10.0.0.40", "netbsd4": True})}
        config = AppConfig.from_values({"TC_HOST": "root@10.0.0.40", "TC_PASSWORD": "pw"})
        with mock.patch("timecapsulesmb.app.ops.common.load_env_config", return_value=config):
            with mock.patch("timecapsulesmb.app.ops.common.resolve_validated_managed_target", return_value=target):
                with mock.patch("timecapsulesmb.services.runtime.tcp_connect_error", return_value=acp_error):
                    with mock.patch("timecapsulesmb.services.activation.run_remote_actions") as remote_actions:
                        rc = service.run_api_request({"operation": "activate", "params": params}, collector.sink)
        remote_actions.assert_not_called()
        return rc, collector

    def test_activate_reports_unreachable_device_instead_of_an_unsupported_model(self) -> None:
        rc, collector = self.run_activate_against_probe(
            failed_probe_state(SshAccessStatus.CLOSED, "SSH is not reachable yet."), acp_error="timed out",
        )

        self.assertEqual(rc, 1)
        error = self.assert_single_terminal_event(collector, "error")
        self.assertEqual(error["code"], "device_unreachable")
        self.assertFalse(error["message"].startswith("DeviceError"))
        self.assert_not_unsupported_model_guidance(error)

    def test_activate_reports_rejected_password_so_the_app_can_ask_for_it(self) -> None:
        rc, collector = self.run_activate_against_probe(failed_probe_state(SshAccessStatus.AUTH_REJECTED, AUTH_REJECTED_ERROR))

        self.assertEqual(rc, 1)
        error = self.assert_single_terminal_event(collector, "error")
        self.assertEqual(error["code"], "auth_failed")
        self.assertIn("replace_password", error["recovery"]["action_ids"])

    def run_flash_backup_against_probe(self, probe_state: ProbedDeviceState, *, acp_error: str | None = None):
        collector = CollectingSink()
        config = AppConfig.from_values({"TC_HOST": "root@10.0.0.2"}, file_values={"TC_HOST": "root@10.0.0.2"})
        with mock.patch("timecapsulesmb.app.ops.common.load_env_config", return_value=config):
            with mock.patch("timecapsulesmb.services.runtime.probe_connection_state", return_value=probe_state):
                # A closed port is rechecked first; it stays closed here.
                with mock.patch("timecapsulesmb.services.runtime.tcp_open", return_value=False), \
                        mock.patch("timecapsulesmb.services.runtime.CLOSED_SSH_RECHECK_WINDOW_SECONDS", 0.0):
                    with mock.patch("timecapsulesmb.services.runtime.tcp_connect_error", return_value=acp_error):
                        with mock.patch("timecapsulesmb.app.ops.flash.backup_flash") as backup_mock:
                            rc = service.run_api_request(
                                {"operation": "flash", "params": {"action": "backup", "credentials": {"password": "pw"}}},
                                collector.sink,
                            )
        backup_mock.assert_not_called()
        return rc, collector

    def test_flash_reports_ssh_failures_with_their_own_codes(self) -> None:
        cases = (
            (failed_probe_state(SshAccessStatus.CLOSED, "SSH is not reachable yet."), None, "ssh_disabled"),
            (failed_probe_state(SshAccessStatus.CLOSED, "SSH is not reachable yet."), "timed out", "device_unreachable"),
            (failed_probe_state(SshAccessStatus.TRANSPORT_FAILED, KEX_CLOSED_ERROR), None, "ssh_transport_failed"),
            (failed_probe_state(SshAccessStatus.AUTH_REJECTED, AUTH_REJECTED_ERROR), None, "auth_failed"),
        )
        for probe_state, acp_error, code in cases:
            with self.subTest(code=code, acp_error=acp_error):
                rc, collector = self.run_flash_backup_against_probe(probe_state, acp_error=acp_error)

                self.assertEqual(rc, 1)
                error = self.assert_single_terminal_event(collector, "error")
                self.assertEqual(error["code"], code)
                self.assert_not_unsupported_model_guidance(error)

    def test_flash_still_rejects_an_airport_express_it_logged_in_to(self) -> None:
        rc, collector = self.run_flash_backup_against_probe(airport_express_probed_state())

        self.assertEqual(rc, 1)
        error = self.assert_single_terminal_event(collector, "error")
        self.assertEqual(error["code"], "unsupported_device")
        self.assert_neutral_unsupported_device_recovery(error)

    def run_activate_against_install(self, *, config_present: bool, version: DeployedVersionProbeResult):
        collector = CollectingSink()
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")
        target = SimpleNamespace(connection=connection, probe_state=netbsd4_probed_state())
        params = {}
        params["confirmation_id"] = self.confirmation_id_for(
            "activate", params, {"host": "root@10.0.0.2", "netbsd4": True})
        self._installed_config_present.return_value = config_present
        self._installed_version.return_value = version

        with mock.patch("timecapsulesmb.app.ops.common.load_env_config", return_value=AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"})):
            with mock.patch("timecapsulesmb.app.ops.common.resolve_validated_managed_target", return_value=target):
                with mock.patch("timecapsulesmb.services.activation.probe_managed_runtime_conn") as runtime_probe:
                    with mock.patch("timecapsulesmb.services.activation.run_remote_actions") as remote_actions:
                        rc = service.run_api_request({"operation": "activate", "params": params}, collector.sink)

        runtime_probe.assert_not_called()
        remote_actions.assert_not_called()
        return rc, self.assert_single_terminal_event(collector, "error")

    def test_activate_without_an_install_offers_install_instead_of_running_rc_local(self) -> None:
        rc, error = self.run_activate_against_install(
            config_present=False, version=DeployedVersionProbeResult(None, None, "missing version metadata"),
        )

        self.assertEqual(rc, 1)
        self.assertEqual(error["code"], "runtime_not_installed")
        self.assertIn("not installed", error["message"])
        self.assertEqual(error["debug"]["stage"], "probe_runtime")
        self.assertEqual(error["recovery"]["suggested_operation"], "deploy")
        self.assertFalse(error["recovery"]["retryable"])

    def test_activate_against_an_older_install_offers_install(self) -> None:
        rc, error = self.run_activate_against_install(
            config_present=True, version=DeployedVersionProbeResult("v2.2.7", 20207, "ok"),
        )

        self.assertEqual(rc, 1)
        self.assertEqual(error["code"], "runtime_outdated")
        self.assertIn("v2.2.7 is older than v3.1.0", error["message"])
        self.assertEqual(error["recovery"]["suggested_operation"], "deploy")

    def test_activate_against_a_newer_install_asks_to_update_the_app(self) -> None:
        rc, error = self.run_activate_against_install(
            config_present=True, version=DeployedVersionProbeResult("v9.0.0", 90000, "ok"),
        )

        self.assertEqual(rc, 1)
        self.assertEqual(error["code"], "client_outdated")
        self.assertIsNone(error["recovery"]["suggested_operation"])

    def test_activate_runtime_failure_reports_remote_error_with_runtime_logs(self) -> None:
        collector = CollectingSink()
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")
        target = SimpleNamespace(connection=connection, probe_state=netbsd4_probed_state())
        params = {}
        params["confirmation_id"] = self.confirmation_id_for(
            "activate", params, {"host": "root@10.0.0.2", "netbsd4": True})
        smbd = readiness_result(True, "managed smbd ready", ("PASS:managed smbd ready",))
        mdns = readiness_result(False, "managed mDNS takeover probe timed out", ("FAIL:managed mDNS takeover probe timed out",))
        verification = ManagedRuntimeProbeResult(
            ready=False,
            detail="runtime verification timed out after 200s; managed smbd ready; managed mDNS takeover probe timed out",
            smbd=smbd,
            mdns=mdns,
            extra_steps=(ProbeStepResult("runtime_timeout", "fail", "runtime verification timed out after 200s"),),
        )

        with mock.patch("timecapsulesmb.app.ops.common.load_env_config", return_value=AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"})):
            with mock.patch("timecapsulesmb.app.ops.common.resolve_validated_managed_target", return_value=target):
                with mock.patch("timecapsulesmb.services.activation.probe_managed_runtime_conn", return_value=managed_runtime_probe(False)):
                    with mock.patch("timecapsulesmb.services.activation.run_remote_actions") as remote_actions:
                        with mock.patch("timecapsulesmb.services.runtime_verification.probe_managed_runtime_conn", return_value=verification) as verify_probe:
                            with mock.patch(
                                "timecapsulesmb.services.runtime_verification.read_runtime_log_tails_conn",
                                return_value={
                                    "remote_manager_log_tail": "manager: mDNS startup deferred; no usable address has appeared yet",
                                    "remote_discovery_log_tail": "mdns: before interface probe",
                                },
                            ):
                                rc = service.run_api_request({"operation": "activate", "params": params}, collector.sink)

        self.assertEqual(rc, 1)
        remote_actions.assert_called_once()
        self.assertEqual(verify_probe.call_args.kwargs, {"timeout_seconds": 200})
        error = self.assert_single_terminal_event(collector, "error")
        self.assertEqual(error["code"], "remote_error")
        self.assertTrue(error["message"].startswith("NetBSD4 activation failed. runtime verification timed out after 200s"))
        self.assertEqual(error["debug"]["stage"], "verify_runtime_activation")
        self.assertEqual(
            error["debug"]["remote_manager_log_tail"],
            "manager: mDNS startup deferred; no usable address has appeared yet",
        )
        self.assertEqual(error["debug"]["remote_discovery_log_tail"], "mdns: before interface probe")
        # The retired advertiser's auto-IP classification no longer runs.
        self.assertNotIn("runtime_startup_failure", error["debug"])

    def test_uninstall_requires_confirmation_before_remote_removal(self) -> None:
        collector = CollectingSink()
        config = AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"})

        with mock.patch("timecapsulesmb.app.ops.common.load_env_config", return_value=config):
            with mock.patch("timecapsulesmb.app.ops.common.resolve_env_connection") as resolve_connection:
                with mock.patch("timecapsulesmb.app.ops.maintenance.remote_uninstall_payload") as uninstall:
                    rc = service.run_api_request({"operation": "uninstall", "params": {}}, collector.sink)

        self.assertEqual(rc, 1)
        error = self.assert_confirmation(
            collector,
            "uninstall.reboot",
            {"requires_reboot": True, "no_reboot": False, "no_wait": False},
        )
        self.assertEqual(
            error["message"],
            "Remove managed TimeCapsuleSMB files from the device and reboot it?",
        )
        resolve_connection.assert_called_once()
        uninstall.assert_not_called()

    def test_uninstall_without_reboot_requires_question_form_confirmation(self) -> None:
        collector = CollectingSink()
        config = AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"})

        with mock.patch("timecapsulesmb.app.ops.common.load_env_config", return_value=config):
            with mock.patch("timecapsulesmb.app.ops.common.resolve_env_connection") as resolve_connection:
                with mock.patch("timecapsulesmb.app.ops.maintenance.remote_uninstall_payload") as uninstall:
                    rc = service.run_api_request(
                        {"operation": "uninstall", "params": {"no_reboot": True}},
                        collector.sink,
                    )

        self.assertEqual(rc, 1)
        error = self.assert_confirmation(
            collector,
            "uninstall.no_reboot",
            {"requires_reboot": False, "no_reboot": True, "no_wait": False},
        )
        self.assertEqual(error["message"], "Remove managed TimeCapsuleSMB files from the device?")
        resolve_connection.assert_called_once()
        uninstall.assert_not_called()

    def test_uninstall_requires_reboot_confirmation_before_remote_connection(self) -> None:
        collector = CollectingSink()

        config = AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"})
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")

        with mock.patch("timecapsulesmb.app.ops.common.load_env_config", return_value=config):
            with mock.patch("timecapsulesmb.app.ops.common.resolve_env_connection", return_value=connection):
                with mock.patch("timecapsulesmb.services.storage.read_mast_volumes_conn") as read_mast:
                    rc = service.run_api_request(
                        {"operation": "uninstall", "params": {}},
                        collector.sink,
                    )

        self.assertEqual(rc, 1)
        self.assertEqual(collector.events_of_type("error")[0]["code"], "confirmation_required")
        read_mast.assert_not_called()

    def run_uninstall_with_password_answer(self, matches: bool | None, params: dict[str, object]) -> tuple[CollectingSink, int, mock.Mock, mock.Mock]:
        collector = CollectingSink()
        config = AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"})
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")
        compare = acp_password_answer(matches)
        with mock.patch("timecapsulesmb.app.ops.common.load_env_config", return_value=config):
            with mock.patch("timecapsulesmb.app.ops.common.resolve_env_connection", return_value=connection):
                with mock.patch("timecapsulesmb.app.ops.maintenance.remote_uninstall_payload") as uninstall:
                    with mock.patch("timecapsulesmb.device.probe.read_airport_acp", compare):
                        rc = service.run_api_request({"operation": "uninstall", "params": params}, collector.sink)
        return collector, rc, compare, uninstall

    def test_uninstall_refuses_a_password_the_device_would_reject_before_asking(self) -> None:
        collector, rc, compare, uninstall = self.run_uninstall_with_password_answer(False, {})

        self.assertEqual(rc, 1)
        error = self.assert_single_terminal_event(collector, "error")
        self.assertEqual(error["code"], "auth_failed")
        self.assertEqual(error["message"], AIRPORT_PASSWORD_MISMATCH_MESSAGE)
        compare.assert_called_once()
        uninstall.assert_not_called()

    def test_uninstall_without_reboot_does_not_compare_the_password(self) -> None:
        # Without the ACP reboot the password is not needed for the removal.
        collector, rc, compare, _uninstall = self.run_uninstall_with_password_answer(False, {"no_reboot": True})

        self.assertEqual(rc, 1)
        self.assert_confirmation(collector, "uninstall.no_reboot")
        compare.assert_not_called()

    def test_uninstall_dry_run_bypasses_confirmation_and_returns_plan(self) -> None:
        collector = CollectingSink()
        config = AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"})
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")

        with mock.patch("timecapsulesmb.app.ops.common.load_env_config", return_value=config):
            with mock.patch("timecapsulesmb.app.ops.common.resolve_env_connection", return_value=connection):
                with mock.patch("timecapsulesmb.app.ops.maintenance.remote_uninstall_payload") as uninstall:
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

    def test_uninstall_dry_run_no_wait_returns_request_only_plan(self) -> None:
        collector = CollectingSink()
        config = AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"})
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")

        with mock.patch("timecapsulesmb.app.ops.common.load_env_config", return_value=config):
            with mock.patch("timecapsulesmb.app.ops.common.resolve_env_connection", return_value=connection):
                with mock.patch("timecapsulesmb.app.ops.maintenance.remote_uninstall_payload") as uninstall:
                    rc = service.run_api_request(
                        {"operation": "uninstall", "params": {"dry_run": True, "no_wait": True}},
                        collector.sink,
                    )

        self.assertEqual(rc, 0)
        payload = self.assert_single_terminal_event(collector, "result")["payload"]
        self.assertTrue(payload["reboot_required"])
        self.assertFalse(payload["wait_after_reboot"])
        self.assertEqual(payload["reboot_request"]["follow_up"], ["return_after_reboot_request"])
        self.assertEqual(payload["post_uninstall_checks"], [])
        uninstall.assert_not_called()

    def test_uninstall_no_wait_uses_mount_wait_and_skips_post_reboot_verification(self) -> None:
        collector = CollectingSink()
        config = AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"})
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")
        mounted = [SimpleNamespace(volume_root="/Volumes/dk2")]
        params = {
            "mount_wait": 13,
            "no_wait": True,
        }
        params["confirmation_id"] = self.confirmation_id_for(
            "uninstall",
            params,
            {
                "host": "root@10.0.0.2",
                "requires_reboot": True,
                "no_reboot": False,
                "no_wait": True,
            },
        )

        with mock.patch("timecapsulesmb.app.ops.common.load_env_config", return_value=config):
            with mock.patch("timecapsulesmb.app.ops.common.resolve_env_connection", return_value=connection):
                with mock.patch("timecapsulesmb.services.storage.read_mast_volumes_conn", return_value=[]):
                    with mock.patch("timecapsulesmb.services.storage.mounted_mast_volumes_conn", return_value=mounted) as mounted_mock:
                        with mock.patch("timecapsulesmb.app.ops.maintenance.remote_uninstall_payload"):
                            with mock.patch("timecapsulesmb.services.maintenance.reboot_device", side_effect=self.fake_reboot_request) as reboot:
                                with mock.patch("timecapsulesmb.services.maintenance.verify_post_uninstall") as verify:
                                    rc = service.run_api_request(
                                        {
                                            "operation": "uninstall",
                                            "params": params,
                                        },
                                        collector.sink,
                                    )

        self.assertEqual(rc, 0)
        self.assertEqual(mounted_mock.call_args.kwargs["wait_seconds"], 13)
        reboot.assert_called_once()
        self.assertIs(reboot.call_args.kwargs["wait"], False)
        verify.assert_not_called()
        payload = collector.events_of_type("result")[0]["payload"]
        self.assertEqual(payload["reboot_requested"], True)
        self.assertEqual(payload["waited"], False)
        self.assertEqual(payload["verified"], False)

    def test_fsck_checks_the_password_then_asks_before_touching_the_disk(self) -> None:
        # The repair reboots through ACP, so the password is compared with the
        # device's before the confirmation; the disk waits for the answer.
        collector = CollectingSink()
        config = AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"})
        connection = SshConnection("root@10.0.0.2", "pw", "")
        compare = acp_password_answer(True)

        with mock.patch("timecapsulesmb.app.ops.maintenance.load_env_config", return_value=config):
            with mock.patch("timecapsulesmb.app.ops.maintenance.resolve_env_connection", return_value=connection):
                with mock.patch("timecapsulesmb.device.probe.read_airport_acp", compare):
                    with mock.patch(
                        "timecapsulesmb.app.ops.maintenance.storage_service.mount_mast_volumes_with_diagnostics",
                    ) as mount:
                        rc = service.run_api_request({"operation": "fsck", "params": {}}, collector.sink)

        self.assertEqual(rc, 1)
        self.assert_confirmation(collector, "fsck.reboot", {"requires_reboot": True, "no_reboot": False})
        compare.assert_called_once()
        mount.assert_not_called()

    def test_fsck_refuses_a_password_the_device_would_reject_before_asking(self) -> None:
        collector = CollectingSink()
        config = AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw-typo"})
        connection = SshConnection("root@10.0.0.2", "pw-typo", "")
        mismatch = acp_password_answer(False)

        with mock.patch("timecapsulesmb.app.ops.maintenance.load_env_config", return_value=config):
            with mock.patch("timecapsulesmb.app.ops.maintenance.resolve_env_connection", return_value=connection):
                with mock.patch("timecapsulesmb.device.probe.read_airport_acp", mismatch):
                    with mock.patch(
                        "timecapsulesmb.app.ops.maintenance.storage_service.mount_mast_volumes_with_diagnostics",
                    ) as mount:
                        rc = service.run_api_request({"operation": "fsck", "params": {}}, collector.sink)

        self.assertEqual(rc, 1)
        error = self.assert_single_terminal_event(collector, "error")
        self.assertEqual(error["code"], "auth_failed")
        self.assertEqual(error["message"], AIRPORT_PASSWORD_MISMATCH_MESSAGE)
        mount.assert_not_called()

    def test_fsck_without_reboot_requires_question_form_confirmation(self) -> None:
        collector = CollectingSink()

        with mock.patch("timecapsulesmb.app.ops.maintenance.load_env_config") as load_config:
            rc = service.run_api_request(
                {"operation": "fsck", "params": {"no_reboot": True}},
                collector.sink,
            )

        self.assertEqual(rc, 1)
        self.assert_confirmation(collector, "fsck.no_reboot", {"requires_reboot": False, "no_reboot": True})
        load_config.assert_not_called()

    def test_fsck_rejects_non_integer_mount_wait_before_remote_connection(self) -> None:
        for value in (12.5, True):
            with self.subTest(value=value):
                collector = CollectingSink()
                with mock.patch("timecapsulesmb.app.ops.maintenance.load_env_config") as load_config:
                    rc = service.run_api_request(
                        {
                            "operation": "fsck",
                            "params": {"list_volumes": True, "mount_wait": value},
                        },
                        collector.sink,
                    )

                self.assertEqual(rc, 1)
                load_config.assert_not_called()
                error = collector.events_of_type("error")[0]
                self.assertEqual(error["code"], "validation_failed")
                self.assertIn("mount_wait must be an integer", error["message"])

    def test_fsck_list_volumes_returns_targets_without_confirmation_or_remote_fsck(self) -> None:
        collector = CollectingSink()
        config = AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"})
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")
        mounted = [MaStVolume("wd0", "dk2", "/Volumes/dk2", "Data", "uuid", True, "hfs")]

        with mock.patch("timecapsulesmb.app.ops.maintenance.load_env_config", return_value=config):
            with mock.patch("timecapsulesmb.app.ops.maintenance.resolve_env_connection", return_value=connection):
                with mock.patch("timecapsulesmb.services.storage.read_mast_volumes_conn", return_value=[]):
                    with mock.patch("timecapsulesmb.services.storage.mounted_mast_volumes_conn", return_value=mounted) as mounted_mock:
                        with mock.patch("timecapsulesmb.services.maintenance.run_ssh") as run_ssh:
                            rc = service.run_api_request(
                                {
                                    "operation": "fsck",
                                    "params": {"list_volumes": True, "mount_wait": 14},
                                },
                                collector.sink,
                            )

        self.assertEqual(rc, 0)
        self.assertEqual(mounted_mock.call_args.kwargs["wait_seconds"], 14)
        run_ssh.assert_not_called()
        payload = collector.events_of_type("result")[0]["payload"]
        self.assertEqual(payload["counts"], {"targets": 1})
        self.assertEqual(payload["targets"][0]["device"], "/dev/dk2")

    def test_fsck_and_uninstall_need_the_password_only_to_reboot(self) -> None:
        # Key-only SSH can list, plan or run without a reboot; the reboot goes
        # through AirPort ACP, so a run that reboots fails before mounting.
        config = AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": ""})
        mounted = [MaStVolume("wd0", "dk2", "/Volumes/dk2", "Data", "uuid", True, "hfs")]

        def fsck_params(**flags: bool) -> dict[str, object]:
            params: dict[str, object] = {"volume": "dk2", **flags}
            params["confirmation_id"] = self.confirmation_id_for("fsck", params, {
                "volume": "dk2",
                "requires_reboot": not flags.get("no_reboot", False),
                "no_reboot": flags.get("no_reboot", False),
                "no_wait": flags.get("no_wait", False),
            })
            return params

        uninstall_no_reboot: dict[str, object] = {"no_reboot": True}
        uninstall_no_reboot["confirmation_id"] = self.confirmation_id_for("uninstall", uninstall_no_reboot, {
            "host": "root@10.0.0.2",
            "requires_reboot": False,
            "no_reboot": True,
            "no_wait": False,
        })
        cases = (
            ("fsck", {"list_volumes": True}, True),
            ("fsck", {"dry_run": True}, True),
            ("fsck", fsck_params(no_reboot=True), True),
            ("fsck", fsck_params(), False),
            ("uninstall", {"dry_run": True}, True),
            ("uninstall", uninstall_no_reboot, True),
            ("uninstall", {}, False),
        )
        for operation, params, allowed in cases:
            with self.subTest(operation=operation, params=sorted(params)):
                collector = CollectingSink()
                with ExitStack() as stack:
                    stack.enter_context(mock.patch("timecapsulesmb.app.ops.maintenance.load_env_config", return_value=config))
                    stack.enter_context(mock.patch("timecapsulesmb.app.ops.common.load_env_config", return_value=config))
                    stack.enter_context(mock.patch("timecapsulesmb.services.storage.read_mast_volumes_conn", return_value=[]))
                    mounted_mock = stack.enter_context(mock.patch("timecapsulesmb.services.storage.mounted_mast_volumes_conn", return_value=mounted))
                    stack.enter_context(mock.patch(
                        "timecapsulesmb.services.maintenance.run_ssh",
                        return_value=subprocess.CompletedProcess(["ssh"], 0, stdout="tcapsule-fsck: fsck_hfs exit status 0\n", stderr=""),
                    ))
                    stack.enter_context(mock.patch("timecapsulesmb.app.ops.maintenance.remote_uninstall_payload"))
                    device = stack.enter_context(FakeAcpDevice().patched())
                    service.run_api_request({"operation": operation, "params": params}, collector.sink)

                if allowed:
                    self.assertEqual(collector.events_of_type("error"), [])
                else:
                    error = self.assert_single_terminal_event(collector, "error")
                    self.assertEqual(error["code"], "config_error")
                    self.assertIn("TC_PASSWORD is required", error["message"])
                    mounted_mock.assert_not_called()
                self.assertEqual(device.calls, [])

    def test_fsck_dry_run_returns_plan_without_remote_fsck(self) -> None:
        collector = CollectingSink()
        config = AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"})
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")
        mounted = [MaStVolume("wd0", "dk2", "/Volumes/dk2", "Data", "uuid", True, "hfs")]

        with mock.patch("timecapsulesmb.app.ops.maintenance.load_env_config", return_value=config):
            with mock.patch("timecapsulesmb.app.ops.maintenance.resolve_env_connection", return_value=connection):
                with mock.patch("timecapsulesmb.services.storage.read_mast_volumes_conn", return_value=[]):
                    with mock.patch("timecapsulesmb.services.storage.mounted_mast_volumes_conn", return_value=mounted):
                        with mock.patch("timecapsulesmb.services.maintenance.run_ssh") as run_ssh:
                            rc = service.run_api_request(
                                {
                                    "operation": "fsck",
                                    "params": {"dry_run": True, "no_wait": True},
                                },
                                collector.sink,
                            )

        self.assertEqual(rc, 0)
        run_ssh.assert_not_called()
        payload = collector.events_of_type("result")[0]["payload"]
        self.assertEqual(payload["device"], "/dev/dk2")
        self.assertEqual(payload["wait_after_reboot"], False)

    def _run_confirmed_fsck(self, stdout: str, *, ssh_returncode: int, device: FakeAcpDevice | None = None,
                            runtime: FakeInstalledRuntime | None = None,
                            probe_state: ProbedDeviceState | None = None, fsck_runs: bool = True, **flags: bool):
        collector = CollectingSink()
        config = AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"})
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")
        mounted = [MaStVolume("wd0", "dk2", "/Volumes/dk2", "Data", "uuid", True, "hfs")]
        params: dict[str, object] = {"volume": "dk2", **flags}
        params["confirmation_id"] = self.confirmation_id_for(
            "fsck",
            params,
            {
                "volume": "dk2",
                "requires_reboot": not flags.get("no_reboot", False),
                "no_reboot": flags.get("no_reboot", False),
                "no_wait": flags.get("no_wait", False),
            },
        )
        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.app.ops.maintenance.load_env_config", return_value=config))
            stack.enter_context(mock.patch("timecapsulesmb.app.ops.maintenance.resolve_env_connection", return_value=connection))
            stack.enter_context(mock.patch("timecapsulesmb.services.storage.read_mast_volumes_conn", return_value=[]))
            stack.enter_context(mock.patch("timecapsulesmb.services.storage.mounted_mast_volumes_conn", return_value=mounted))
            run_ssh = stack.enter_context(mock.patch(
                "timecapsulesmb.services.maintenance.run_ssh",
                return_value=subprocess.CompletedProcess(["ssh"], ssh_returncode, stdout=stdout, stderr=""),
            ))
            # Before rebooting, fsck probes the device like deploy does.
            stack.enter_context(mock.patch(
                "timecapsulesmb.app.ops.maintenance.probe_managed_connection_state",
                return_value=probe_state or probed_state(),
            ))
            reboot = stack.enter_context((device or FakeAcpDevice()).patched())
            stack.enter_context((runtime or FakeInstalledRuntime()).patched())
            rc = service.run_api_request({"operation": "fsck", "params": params}, collector.sink)
        self.assertEqual(run_ssh.call_count, 1 if fsck_runs else 0)
        return rc, collector, reboot

    def test_fsck_clean_status_reboots_waits_and_succeeds(self) -> None:
        rc, collector, reboot = self._run_confirmed_fsck(
            "--- fsck_hfs /dev/dk2 ---\ntcapsule-fsck: fsck_hfs exit status 0\r\n",
            ssh_returncode=0,
        )

        self.assertEqual(rc, 0)
        self.assertEqual(reboot.calls.count("request"), 1)
        self.assertTrue(reboot.served_new_boot)
        payload = self.assert_single_terminal_event(collector, "result")["payload"]
        self.assertEqual(payload["returncode"], 0)
        self.assertEqual(payload["summary"], "Disk repair completed with fsck.")
        self.assertNotIn("error", payload)
        self.assertTrue(payload["verified"])

    def test_fsck_on_netbsd4_starts_file_sharing_after_the_reboot(self) -> None:
        runtime = FakeInstalledRuntime()
        rc, collector, _reboot = self._run_confirmed_fsck(
            "tcapsule-fsck: fsck_hfs exit status 0\n",
            ssh_returncode=0,
            probe_state=netbsd4_probed_state(),
            runtime=runtime,
        )

        self.assertEqual(rc, 0)
        self.assertIn("run /mnt/Flash/rc.local", runtime.calls)
        stages = [event["stage"] for event in collector.events if event.get("type") == "stage"]
        self.assertEqual(stages[-3:], ["wait_for_reboot_up", "post_reboot_activation", "verify_runtime_activation"])
        payload = self.assert_single_terminal_event(collector, "result")["payload"]
        self.assertEqual(payload["summary"], "Disk repair completed with fsck.")

    def test_fsck_on_netbsd4_whose_file_sharing_does_not_restart_offers_activate(self) -> None:
        rc, collector, _reboot = self._run_confirmed_fsck(
            "tcapsule-fsck: fsck_hfs exit status 0\n",
            ssh_returncode=0,
            probe_state=netbsd4_probed_state(),
            runtime=FakeInstalledRuntime(becomes_ready=False),
        )

        self.assertEqual(rc, 1)
        error = self.assert_single_terminal_event(collector, "error")
        self.assertEqual(error["code"], "runtime_not_restarted")
        self.assertTrue(error["message"].startswith("Disk repair completed. File sharing did not restart"))
        self.assertEqual(error["recovery"]["localization_key"], "runtime_not_restarted")
        self.assertEqual(error["recovery"]["suggested_operation"], "activate")
        self.assertEqual(error["recovery"]["action_ids"], ["start_smb", "run_checkup"])

    def test_fsck_whose_device_probe_fails_stops_before_touching_the_disk(self) -> None:
        # Without the probe fsck cannot know whether file sharing must be
        # started after the reboot; it fails before the repair, not after.
        runtime = FakeInstalledRuntime()
        rc, collector, reboot = self._run_confirmed_fsck(
            "tcapsule-fsck: fsck_hfs exit status 0\n",
            ssh_returncode=0,
            probe_state=failed_probe_state(SshAccessStatus.AUTH_REJECTED, AUTH_REJECTED_ERROR),
            runtime=runtime,
            fsck_runs=False,
        )

        self.assertEqual(rc, 1)
        error = self.assert_single_terminal_event(collector, "error")
        self.assertEqual(error["code"], "auth_failed")
        self.assertNotIn("request", reboot.calls)
        self.assertEqual(runtime.calls, [])

    def test_fsck_without_reboot_does_not_probe_the_device(self) -> None:
        # Nothing restarts after a repair without a reboot, so a probe that
        # would fail does not stop it.
        rc, collector, _reboot = self._run_confirmed_fsck(
            "tcapsule-fsck: fsck_hfs exit status 0\n",
            ssh_returncode=0,
            probe_state=failed_probe_state(SshAccessStatus.AUTH_REJECTED, AUTH_REJECTED_ERROR),
            no_reboot=True,
        )

        self.assertEqual(rc, 0)
        self.assertTrue(self.assert_single_terminal_event(collector, "result")["ok"])

    def test_fsck_failed_status_still_waits_for_reboot_then_fails(self) -> None:
        rc, collector, reboot = self._run_confirmed_fsck(
            "--- fsck_hfs /dev/dk2 ---\ntcapsule-fsck: fsck_hfs exit status 8\n",
            ssh_returncode=8,
        )

        self.assertEqual(rc, 1)
        # The reboot is what restarts file sharing, so it is still requested and observed.
        self.assertEqual(reboot.calls.count("request"), 1)
        self.assertTrue(reboot.served_new_boot)
        result = self.assert_single_terminal_event(collector, "result")
        self.assertFalse(result["ok"])
        self.assertEqual(result["payload"]["returncode"], 8)
        self.assertIn("fsck_hfs exited with status 8", result["payload"]["error"])
        self.assertNotEqual(result["payload"]["summary"], "Disk repair completed with fsck.")

    def test_fsck_failed_status_without_wait_is_not_reported_as_success(self) -> None:
        rc, collector, reboot = self._run_confirmed_fsck(
            "tcapsule-fsck: fsck_hfs exit status 8\n",
            ssh_returncode=8,
            no_wait=True,
        )

        self.assertEqual(rc, 1)
        self.assertEqual(reboot.calls.count("request"), 1)
        self.assertNotIn("read", reboot.calls)
        result = self.assert_single_terminal_event(collector, "result")
        self.assertFalse(result["ok"])
        self.assertEqual(result["payload"]["returncode"], 8)
        self.assertTrue(result["payload"]["reboot_requested"])

    def test_fsck_failed_status_without_reboot_fails(self) -> None:
        rc, collector, reboot = self._run_confirmed_fsck(
            "tcapsule-fsck: fsck_hfs exit status 8\n",
            ssh_returncode=8,
            no_reboot=True,
        )

        self.assertEqual(rc, 1)
        self.assertNotIn("request", reboot.calls)
        self.assertNotIn("read", reboot.calls)
        result = self.assert_single_terminal_event(collector, "result")
        self.assertFalse(result["ok"])
        self.assertFalse(result["payload"]["reboot_requested"])

    def test_fsck_that_never_ran_fails_without_waiting_for_a_reboot(self) -> None:
        # A process that would not stop aborts the script before fsck, so no
        # status line arrives and no reboot is requested, even when asked for.
        for flags in ({}, {"no_wait": True}):
            with self.subTest(flags=flags):
                rc, collector, reboot = self._run_confirmed_fsck(
                    "process smbd did not stop\n",
                    ssh_returncode=1,
                    **flags,
                )

                self.assertEqual(rc, 1)
                self.assertNotIn("request", reboot.calls)
                self.assertNotIn("read", reboot.calls)
                error = self.assert_single_terminal_event(collector, "error")
                self.assertEqual(error["code"], "remote_error")
                self.assertIn("fsck did not run", error["message"])

    def test_fsck_on_a_volume_that_stayed_mounted_reports_it_without_waiting(self) -> None:
        # The script refuses to repair a volume still in the mount table, so
        # the error names the unmount and no reboot is requested.
        for flags in ({}, {"no_reboot": True}):
            with self.subTest(flags=flags):
                rc, collector, reboot = self._run_confirmed_fsck(
                    "umount: /Volumes/Data: Device busy\ntcapsule-fsck: volume not unmounted\n",
                    ssh_returncode=1,
                    **flags,
                )

                self.assertEqual(rc, 1)
                self.assertNotIn("request", reboot.calls)
                self.assertNotIn("read", reboot.calls)
                error = self.assert_single_terminal_event(collector, "error")
                self.assertEqual(error["code"], "remote_error")
                self.assertEqual(error["message"], FSCK_NOT_UNMOUNTED_MESSAGE)

    def test_helper_reads_request_and_writes_ndjson(self) -> None:
        output = io.StringIO()
        fake_stdin = io.StringIO('{"operation":"capabilities","params":{}}')
        with mock.patch.object(sys, "stdin", fake_stdin):
            with mock.patch("timecapsulesmb.app.helper.run_api_request") as run_mock:
                run_mock.side_effect = lambda request, sink: (sink.result(request["operation"], ok=True, payload={"ok": True}) or 0)
                with redirect_stdout(output):
                    rc = helper.main([])

        self.assertEqual(rc, 0)
        line = json.loads(output.getvalue())
        self.assertEqual(line["type"], "result")
        self.assertEqual(line["operation"], "capabilities")
        self.assertEqual(line["schema_version"], 1)
        self.assertTrue(line["request_id"])

    def test_helper_rejects_invalid_json_without_leaking_pretty_error_details(self) -> None:
        output = io.StringIO()
        error_output = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO('{"operation":"capabilities","password":"secret"')):
            with redirect_stdout(output):
                with mock.patch.object(sys, "stderr", error_output):
                    rc = helper.main(["--pretty-error"])

        self.assertEqual(rc, 1)
        event = json.loads(output.getvalue())
        self.assertEqual(event["type"], "error")
        self.assertEqual(event["code"], "invalid_request")
        self.assertNotIn("secret", error_output.getvalue())

    def test_helper_rejects_oversized_request_without_leaking_body(self) -> None:
        output = io.StringIO()
        error_output = io.StringIO()
        secret = "secret"
        oversized = secret + ("x" * (helper.MAX_REQUEST_CHARS + 1))
        with mock.patch.object(sys, "stdin", io.StringIO(oversized)):
            with redirect_stdout(output):
                with mock.patch.object(sys, "stderr", error_output):
                    rc = helper.main(["--pretty-error"])

        self.assertEqual(rc, 1)
        event = json.loads(output.getvalue())
        self.assertEqual(event["type"], "error")
        self.assertEqual(event["code"], "invalid_request")
        self.assertIn("maximum size", event["message"])
        self.assertNotIn(secret, error_output.getvalue())

    def test_helper_rejects_top_level_non_object_json(self) -> None:
        output = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO('["capabilities"]')):
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
        self.assertEqual(cli_main.COMMANDS["api"].__module__, "timecapsulesmb.cli.api")


if __name__ == "__main__":
    unittest.main()
