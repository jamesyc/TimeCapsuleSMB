from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from timecapsulesmb.app.events import AppEvent, EventSink
from timecapsulesmb.app.context import AppOperationContext
from timecapsulesmb.app.confirmations import build_confirmation
from timecapsulesmb import repair_xattrs as repair_xattrs_domain
from timecapsulesmb.app import contracts, helper, service
from timecapsulesmb.cli.version_check import VersionCheckResult
from timecapsulesmb.cli import main as cli_main
from timecapsulesmb.checks.models import CheckResult
from timecapsulesmb.core.config import MANAGED_PAYLOAD_DIR_NAME, AppConfig, ConfigError, parse_env_file
from timecapsulesmb.device.compat import DeviceCompatibility
from timecapsulesmb.device.probe import (
    ManagedMdnsTakeoverProbeResult,
    ManagedRuntimeProbeResult,
    ManagedSmbdProbeResult,
    ProbeResult,
    ProbedDeviceState,
)
from timecapsulesmb.device.storage import MaStVolume, build_dry_run_payload_home
from timecapsulesmb.discovery.bonjour import BonjourDiscoverySnapshot, BonjourResolvedService, BonjourServiceInstance
from timecapsulesmb.integrations.acp import ACPAuthError
from timecapsulesmb.services.app import AppOperationError, jsonable
from timecapsulesmb.services.flash import STALE_BACKUP_AFTER_WRITE_MESSAGE, require_backup_fresh_for_plan
from timecapsulesmb.transport.errors import SshCommandTimeout, SshError, TransportError
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


def managed_runtime_probe(ready: bool = True) -> ManagedRuntimeProbeResult:
    status = "PASS" if ready else "FAIL"
    detail = "managed runtime is ready" if ready else "managed runtime is not ready"
    smbd = ManagedSmbdProbeResult(ready, detail, (f"{status}:managed smbd ready",))
    mdns = ManagedMdnsTakeoverProbeResult(ready, detail, (f"{status}:managed mDNS takeover active",))
    return ManagedRuntimeProbeResult(
        ready=ready,
        detail=detail,
        smbd=smbd,
        mdns=mdns,
        lines=smbd.lines + mdns.lines,
    )


class AppApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._exit_stack = ExitStack()
        self._telemetry_client = mock.Mock()
        # App API tests exercise GUI/backend telemetry-enabled operations.
        # Keep telemetry mocked here so unit tests never POST to the live telemetry service.
        self._telemetry_factory = self._exit_stack.enter_context(
            mock.patch("timecapsulesmb.app.service.TelemetryClient.from_config", return_value=self._telemetry_client)
        )
        # This tripwire catches future tests that accidentally bypass the app-service telemetry mock.
        self._telemetry_urlopen = self._exit_stack.enter_context(
            mock.patch("timecapsulesmb.telemetry.urllib.request.urlopen", side_effect=AssertionError("tests must not send telemetry"))
        )

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

    def test_event_redacts_sensitive_fields(self) -> None:
        event = AppEvent("result", "configure", {
            "ok": True,
            "payload": {
                "password": "secret",
                "nested": {
                    "TC_PASSWORD": "secret",
                    "api_key": "secret",
                    "ssh_private_key": "secret",
                },
            },
        })

        data = event.to_jsonable()

        self.assertEqual(data["payload"]["password"], "<redacted>")
        self.assertEqual(data["payload"]["nested"]["TC_PASSWORD"], "<redacted>")
        self.assertEqual(data["payload"]["nested"]["api_key"], "<redacted>")
        self.assertEqual(data["payload"]["nested"]["ssh_private_key"], "<redacted>")

    def test_result_event_preserves_falsey_payloads(self) -> None:
        collector = CollectingSink()

        collector.sink.result("capabilities", ok=True, payload=[])

        result = collector.events_of_type("result")[0]
        self.assertEqual(result["payload"], [])
        self.assertEqual(result["schema_version"], 1)
        self.assertTrue(result["request_id"])

    def test_jsonable_serializes_enum_values_inside_dataclasses(self) -> None:
        self.assertEqual(jsonable(SamplePayload(SampleMode.FAST)), {"mode": "fast"})

    def test_stage_events_include_policy_metadata(self) -> None:
        collector = CollectingSink()

        collector.sink.stage("capabilities", "resolve_paths")
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

        repair = contracts.repair_xattrs_payload({
            "returncode": 0,
            "root": "/Volumes/Data",
            "finding_count": 2,
            "repairable_count": 1,
            "stats": {"scanned": 3},
        })
        self.assertEqual(repair["summary"], "Found 2 metadata issue(s), 1 repairable.")
        self.assertEqual(repair["summary_text"], "Found 2 metadata issue(s), 1 repairable.")
        self.assertEqual(repair["stats"], {"scanned": 3})

    def test_repair_xattrs_payload_preserves_legacy_summary_stats_as_stats(self) -> None:
        repair = contracts.repair_xattrs_payload({
            "finding_count": 2,
            "repairable_count": 1,
            "summary": {"scanned": 3},
        })

        self.assertEqual(repair["summary"], "Found 2 metadata issue(s), 1 repairable.")
        self.assertEqual(repair["summary_text"], "Found 2 metadata issue(s), 1 repairable.")
        self.assertEqual(repair["stats"], {"scanned": 3})

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

        with mock.patch("timecapsulesmb.app.ops.flash._load_flash_config", return_value=object()):
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

        with mock.patch("timecapsulesmb.app.ops.flash.load_env_config", return_value=config):
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
            with mock.patch("timecapsulesmb.app.ops.flash._load_flash_config", side_effect=AssertionError("plan should not load device config")):
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
            plan = SimpleNamespace(already_satisfied=False, target_bank=target_bank)
            bundle = SimpleNamespace(manifest=manifest, backup_dir=backup_dir)
            target = SimpleNamespace(acp_host="10.0.0.2", connection=object())

            def run(params: dict[str, object]) -> CollectingSink:
                collector = CollectingSink()
                with mock.patch("timecapsulesmb.app.ops.flash.plan_flash_from_backup", return_value=(bundle, plan)):
                    with mock.patch("timecapsulesmb.app.ops.flash._load_flash_config", return_value=object()):
                        with mock.patch("timecapsulesmb.app.ops.flash._resolve_flash_target", return_value=target):
                            with mock.patch("timecapsulesmb.app.ops.flash.validate_live_target_matches_backup") as validate_mock:
                                with mock.patch("timecapsulesmb.app.ops.flash.write_flash_plan") as write_mock:
                                    with mock.patch("timecapsulesmb.app.ops.flash.request_reboot") as reboot_mock:
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
            plan = SimpleNamespace(already_satisfied=False, target_bank=target_bank)
            bundle = SimpleNamespace(manifest=manifest, backup_dir=backup_dir)
            connection = object()
            target = SimpleNamespace(acp_host="10.0.0.2", connection=connection)
            collector = CollectingSink()

            with mock.patch("timecapsulesmb.app.ops.flash.plan_flash_from_backup", return_value=(bundle, plan)):
                with mock.patch("timecapsulesmb.app.ops.flash._load_flash_config", return_value=object()):
                    with mock.patch("timecapsulesmb.app.ops.flash._resolve_flash_target", return_value=target):
                        with mock.patch("timecapsulesmb.app.ops.flash.validate_live_target_matches_backup"):
                            with mock.patch("timecapsulesmb.app.ops.flash.write_flash_plan"):
                                with mock.patch("timecapsulesmb.app.ops.flash.request_reboot_and_wait") as reboot_wait:
                                    rc = service.run_api_request(
                                        {
                                            "operation": "flash",
                                            "params": {
                                                "action": "write",
                                                "backup_dir": str(backup_dir),
                                                "mode": "restore",
                                                "confirm_flash": True,
                                            },
                                        },
                                        collector.sink,
                                    )

        self.assertEqual(rc, 0)
        reboot_wait.assert_called_once()
        self.assertIs(reboot_wait.call_args.args[1], connection)
        payload = self.assert_single_terminal_event(collector, "result")["payload"]
        self.assertEqual(payload["post_write_action"], "ssh_reboot")
        self.assertTrue(payload["reboot_requested"])
        self.assertTrue(payload["rebooted"])
        self.assertTrue(payload["waited_after_reboot"])
        self.assertEqual(payload["summary"], "Flash restore write validated; device rebooted.")

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
                        "confirm_flash": True,
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
            self.assertEqual(payload["current_version"], 20005)
            self.assertEqual(payload["latest_tag"], "v2.0.5")
            self.assertEqual(payload["source"], "network")

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

    def test_unknown_operation_emits_error_without_result(self) -> None:
        collector = CollectingSink()

        rc = service.run_api_request({"operation": "nope", "params": {}}, collector.sink)

        self.assertEqual(rc, 1)
        error = self.assert_single_terminal_event(collector, "error")
        self.assertEqual(error["code"], "unknown_operation")
        self.assertEqual(error["recovery"]["title"], "Unknown operation")

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

    def test_dispatcher_emits_app_operation_finish_fields_in_telemetry(self) -> None:
        collector = CollectingSink()

        def run_deploy(_params, context):
            context.stage("verify_runtime_reboot")
            context.update_fields(
                device_family="netbsd6_samba4",
                device_os_version="NetBSD 6.0 (earmv4)",
                device_model="TimeCapsule8,119",
                device_syap="119",
                nbns_enabled=False,
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
                                {"operation": "deploy", "params": {"nbns_enabled": False}},
                                collector.sink,
                            )

        self.assertEqual(rc, 0)
        self.assertEqual(self._telemetry_factory.call_args.kwargs["nbns_enabled"], False)
        finished = self._telemetry_client.emit.call_args_list[1].kwargs
        self.assertEqual(finished["result"], "success")
        self.assertEqual(finished["stage"], "verify_runtime_reboot")
        self.assertEqual(finished["device_family"], "netbsd6_samba4")
        self.assertEqual(finished["device_os_version"], "NetBSD 6.0 (earmv4)")
        self.assertEqual(finished["device_model"], "TimeCapsule8,119")
        self.assertEqual(finished["device_syap"], "119")
        self.assertEqual(finished["nbns_enabled"], False)
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
            "timecapsulesmb.app.ops.discovery.discover_snapshot_merged_detailed",
            return_value=(snapshot, SimpleNamespace()),
        ):
            with mock.patch("timecapsulesmb.app.service.resolve_app_paths", return_value=SimpleNamespace(bootstrap_path=Path("/tmp/bootstrap"))):
                with mock.patch("timecapsulesmb.app.service.ensure_install_id"):
                    with mock.patch("timecapsulesmb.app.service.load_optional_env_config", return_value=AppConfig.from_values({})):
                        rc = service.run_api_request({"operation": "discover", "params": {"timeout": 0.1}}, collector.sink)

        self.assertEqual(rc, 0)
        result = collector.events_of_type("result")[0]
        self.assertEqual(result["payload"]["resolved"][0]["name"], "TC")
        self.assertEqual(result["payload"]["resolved"][0]["ipv4"], ["169.254.44.9", "10.0.0.2"])
        self.assertEqual(result["payload"]["devices"][0]["name"], "TC")
        self.assertEqual(result["payload"]["devices"][0]["host"], "10.0.0.2")
        self.assertEqual(result["payload"]["devices"][0]["preferred_ipv4"], "10.0.0.2")
        self.assertEqual(result["payload"]["devices"][0]["selected_record"]["fullname"], "TC._airport._tcp.local.")
        self.assertEqual(result["payload"]["schema_version"], 1)
        self.assertEqual(result["payload"]["counts"], {"instances": 1, "resolved": 1, "devices": 1})
        self.assertEqual(result["payload"]["summary"], "Discovered 1 device(s).")
        self.assertEqual(self._telemetry_client.emit.call_count, 2)
        started = self._telemetry_client.emit.call_args_list[0].kwargs
        finished = self._telemetry_client.emit.call_args_list[1].kwargs
        self.assertEqual(started["operation"], "discover")
        self.assertEqual(started["entrypoint"], "api")
        self.assertEqual(started["options"], {"timeout": 0.1})
        self.assertEqual(finished["result"], "success")
        self.assertEqual(finished["stage"], "bonjour_discovery")
        self.assertEqual(finished["discovery_instance_count"], 1)
        self.assertEqual(finished["discovery_resolved_count"], 1)
        self.assertEqual(finished["discovery_device_count"], 1)
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
            "timecapsulesmb.app.ops.discovery.discover_snapshot_merged_detailed",
            return_value=(snapshot, SimpleNamespace()),
        ):
            with mock.patch("timecapsulesmb.app.service.resolve_app_paths", return_value=SimpleNamespace(bootstrap_path=Path("/tmp/bootstrap"))):
                with mock.patch("timecapsulesmb.app.service.ensure_install_id"):
                    with mock.patch("timecapsulesmb.app.service.load_optional_env_config", return_value=AppConfig.from_values({})):
                        rc = service.run_api_request({"operation": "discover", "params": {"timeout": 0.1}}, collector.sink)

        self.assertEqual(rc, 0)
        payload = collector.events_of_type("result")[0]["payload"]
        self.assertEqual(payload["counts"], {"instances": 0, "resolved": 8, "devices": 2})
        self.assertEqual([device["name"] for device in payload["devices"]], ["James", "Office"])
        self.assertEqual(payload["devices"][0]["host"], "192.168.1.217")
        self.assertEqual(payload["devices"][0]["selected_record"]["service_type"], "_airport._tcp.local.")

    def test_discover_rejects_invalid_timeout_values(self) -> None:
        for timeout in ("bad", "nan", -1, True):
            with self.subTest(timeout=timeout):
                collector = CollectingSink()
                with mock.patch("timecapsulesmb.app.ops.discovery.discover_snapshot_merged_detailed") as discover:
                    with mock.patch("timecapsulesmb.app.service.resolve_app_paths", return_value=SimpleNamespace(bootstrap_path=Path("/tmp/bootstrap"))):
                        with mock.patch("timecapsulesmb.app.service.ensure_install_id"):
                            with mock.patch("timecapsulesmb.app.service.load_optional_env_config", return_value=AppConfig.from_values({})):
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

        with mock.patch(
            "timecapsulesmb.app.ops.discovery.discover_snapshot_merged_detailed",
            return_value=(snapshot, SimpleNamespace()),
        ) as discover:
            with mock.patch("timecapsulesmb.app.service.resolve_app_paths", return_value=SimpleNamespace(bootstrap_path=Path("/tmp/bootstrap"))):
                with mock.patch("timecapsulesmb.app.service.ensure_install_id"):
                    with mock.patch("timecapsulesmb.app.service.load_optional_env_config", return_value=AppConfig.from_values({})):
                        rc = service.run_api_request(
                            {"operation": "discover", "params": {"timeout": "0.25"}},
                            collector.sink,
                        )

        self.assertEqual(rc, 0)
        discover.assert_called_once_with(timeout=0.25)

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
                with mock.patch("timecapsulesmb.app.ops.configure.enable_ssh") as enable_ssh:
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
                with mock.patch("timecapsulesmb.app.ops.configure.enable_ssh") as enable_ssh:
                    with mock.patch("timecapsulesmb.app.ops.configure.wait_for_ssh_port", return_value=True) as wait_for_ssh:
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
        enable_ssh.assert_called_once()
        wait_for_ssh.assert_called_once_with("root@10.0.0.2", timeout_seconds=180)
        self.assertEqual(values["TC_HOST"], "root@10.0.0.2")
        self.assertNotIn("secret", json.dumps(confirmed_collector.events))

    def test_configure_enable_ssh_false_fails_without_confirmation(self) -> None:
        collector = CollectingSink()
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / ".env"
            with mock.patch("timecapsulesmb.app.ops.configure.probe_connection_state", return_value=unreachable_probed_state()):
                with mock.patch("timecapsulesmb.app.ops.configure.enable_ssh") as enable_ssh:
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
            with mock.patch("timecapsulesmb.app.ops.configure.probe_connection_state", return_value=unreachable_probed_state()):
                with mock.patch("timecapsulesmb.app.ops.configure.enable_ssh", side_effect=ACPAuthError("bad password")):
                    rc = service.run_api_request(
                        {
                            "operation": "configure",
                            "params": {
                                "config": str(config_path),
                                "host": "root@10.0.0.2",
                                "password": "badpw",
                                "yes": True,
                            },
                        },
                        collector.sink,
                    )

        self.assertEqual(rc, 1)
        self.assertFalse(config_path.exists())
        self.assertEqual(collector.events_of_type("error")[0]["code"], "auth_failed")
        self.assertEqual(collector.events_of_type("error")[0]["recovery"]["suggested_operation"], "configure")
        self.assertEqual(collector.events_of_type("error")[0]["recovery"]["action_ids"], ["replace_password"])
        self.assertNotIn("badpw", json.dumps(collector.events))

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

    def test_configure_rejects_boolean_ssh_wait_timeout(self) -> None:
        collector = CollectingSink()
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / ".env"
            with mock.patch("timecapsulesmb.app.ops.configure.probe_connection_state", return_value=unreachable_probed_state()):
                with mock.patch("timecapsulesmb.app.ops.configure.enable_ssh") as enable_ssh:
                    rc = service.run_api_request(
                        {
                            "operation": "configure",
                            "params": {
                                "config": str(config_path),
                                "host": "root@10.0.0.2",
                                "password": "pw",
                                "ssh_wait_timeout": True,
                                "yes": True,
                            },
                        },
                        collector.sink,
                    )

        self.assertEqual(rc, 1)
        enable_ssh.assert_called_once()
        self.assertFalse(config_path.exists())
        error = self.assert_single_terminal_event(collector, "error")
        self.assertEqual(error["code"], "validation_failed")
        self.assertIn("ssh_wait_timeout must be an integer", error["message"])

    def test_doctor_streams_check_events(self) -> None:
        collector = CollectingSink()
        config = AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"})

        def fake_run_doctor_checks(*_args, **kwargs):
            kwargs["on_result"](CheckResult("PASS", "smbd is bound to TCP 445", {"port": 445}))
            return [CheckResult("PASS", "smbd is bound to TCP 445", {"port": 445})], False

        with mock.patch("timecapsulesmb.app.ops.doctor.load_env_config", return_value=config):
            with mock.patch("timecapsulesmb.app.ops.doctor.resolve_app_paths", return_value=SimpleNamespace(distribution_root=REPO_ROOT)):
                with mock.patch("timecapsulesmb.app.ops.doctor.resolve_env_connection", return_value=SshConnection("root@10.0.0.2", "pw", "-o foo")):
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

        with mock.patch("timecapsulesmb.app.ops.doctor.load_env_config", return_value=config):
            with mock.patch("timecapsulesmb.app.ops.doctor.resolve_app_paths", return_value=SimpleNamespace(distribution_root=REPO_ROOT)):
                with mock.patch("timecapsulesmb.app.ops.doctor.resolve_env_connection", return_value=SshConnection("root@10.0.0.2", "pw", "-o foo")):
                    with mock.patch("timecapsulesmb.app.ops.doctor.run_doctor_checks", return_value=([], False)) as checks:
                        rc = service.run_api_request(
                            {"operation": "doctor", "params": {"bonjour_timeout": "2.75"}},
                            collector.sink,
                        )

        self.assertEqual(rc, 0)
        self.assertNotIn("bonjour_timeout", checks.call_args.kwargs)

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

        with mock.patch("timecapsulesmb.app.ops.doctor.load_env_config", return_value=config):
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

        with mock.patch("timecapsulesmb.app.ops.doctor.load_env_config", return_value=config):
            with mock.patch("timecapsulesmb.app.ops.doctor.resolve_app_paths", return_value=SimpleNamespace(distribution_root=REPO_ROOT)):
                with mock.patch("timecapsulesmb.app.ops.doctor.resolve_env_connection", return_value=SshConnection("root@10.0.0.2", "pw", "-o foo")):
                    with mock.patch("timecapsulesmb.app.ops.doctor.run_doctor_checks", side_effect=fake_run_doctor_checks):
                        rc = service.run_api_request({"operation": "doctor", "params": {}}, collector.sink)

        self.assertEqual(rc, 1)
        self.assertEqual(collector.events_of_type("error"), [])
        result = collector.events_of_type("result")[0]
        self.assertEqual(result["ok"], False)
        self.assertTrue(result["payload"]["fatal"])
        self.assertNotIn("pw", json.dumps(collector.events))

    def test_doctor_failure_telemetry_includes_shared_debug_context(self) -> None:
        collector = CollectingSink()
        config = AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"})

        def fake_run_doctor_checks(*_args, **kwargs):
            kwargs["debug_fields"]["bonjour_expected"] = {"instance_name": "Home"}
            kwargs["debug_fields"]["bonjour_zeroconf"] = {"instance_count": 0, "ip_version": "V4Only"}
            result = CheckResult("FAIL", "no discovered _smb._tcp instance matched expected device instance 'Home'")
            kwargs["on_result"](result)
            return [result], True

        with mock.patch("timecapsulesmb.app.ops.doctor.load_env_config", return_value=config):
            with mock.patch("timecapsulesmb.app.ops.doctor.resolve_app_paths", return_value=SimpleNamespace(distribution_root=REPO_ROOT)):
                with mock.patch("timecapsulesmb.app.ops.doctor.resolve_env_connection", return_value=SshConnection("root@10.0.0.2", "pw", "-o foo")):
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
            "mdns-advertiser": SimpleNamespace(absolute_path=REPO_ROOT / "bin/mdns/mdns-advertiser"),
            "nbns-advertiser": SimpleNamespace(absolute_path=REPO_ROOT / "bin/nbns/nbns-advertiser"),
        }

        with mock.patch("timecapsulesmb.app.ops.deploy.load_env_config", return_value=AppConfig.from_values({
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_INTERNAL_SHARE_USE_DISK_ROOT": "true",
            "TC_ANY_PROTOCOL": "true",
            "TC_DEBUG_LOGGING": "true",
        })):
            with mock.patch("timecapsulesmb.app.ops.deploy.resolve_validated_managed_target", return_value=target):
                with mock.patch("timecapsulesmb.app.ops.deploy.resolve_app_paths", return_value=SimpleNamespace(distribution_root=REPO_ROOT)):
                    with mock.patch("timecapsulesmb.app.ops.deploy.validate_artifacts", return_value=[("smbd", True, "ok")]):
                        with mock.patch("timecapsulesmb.app.ops.deploy.resolve_payload_artifacts", return_value=artifacts):
                            with mock.patch("timecapsulesmb.app.ops.deploy.run_remote_actions", side_effect=AssertionError("dry run should not run remote actions")):
                                rc = service.run_api_request(
                                    {"operation": "deploy", "params": {"dry_run": True, "yes": True}},
                                    collector.sink,
                                )

        self.assertEqual(rc, 0)
        result = collector.events_of_type("result")[0]
        self.assertEqual(result["payload"]["host"], "root@10.0.0.2")
        self.assertEqual(result["payload"]["reboot_required"], True)
        self.assertEqual(result["payload"]["requires_reboot"], True)
        self.assertEqual(result["payload"]["startup_mode"], "reboot_then_verify")
        self.assertEqual(result["payload"]["payload_family"], "netbsd6_samba4")
        self.assertEqual(result["payload"]["schema_version"], 1)

    def test_deploy_dry_run_no_wait_returns_request_only_plan(self) -> None:
        collector = CollectingSink()
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")
        target = SimpleNamespace(connection=connection, probe_state=probed_state())
        artifacts = {
            "smbd": SimpleNamespace(absolute_path=REPO_ROOT / "bin/samba4/smbd"),
            "mdns-advertiser": SimpleNamespace(absolute_path=REPO_ROOT / "bin/mdns/mdns-advertiser"),
            "nbns-advertiser": SimpleNamespace(absolute_path=REPO_ROOT / "bin/nbns/nbns-advertiser"),
        }

        with mock.patch("timecapsulesmb.app.ops.deploy.load_env_config", return_value=AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"})):
            with mock.patch("timecapsulesmb.app.ops.deploy.resolve_validated_managed_target", return_value=target):
                with mock.patch("timecapsulesmb.app.ops.deploy.resolve_app_paths", return_value=SimpleNamespace(distribution_root=REPO_ROOT)):
                    with mock.patch("timecapsulesmb.app.ops.deploy.validate_artifacts", return_value=[("smbd", True, "ok")]):
                        with mock.patch("timecapsulesmb.app.ops.deploy.resolve_payload_artifacts", return_value=artifacts):
                            with mock.patch("timecapsulesmb.app.ops.deploy.run_remote_actions", side_effect=AssertionError("dry run should not run remote actions")):
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
            "mdns-advertiser": SimpleNamespace(absolute_path=REPO_ROOT / "bin/mdns-netbsd4be/mdns-advertiser"),
            "nbns-advertiser": SimpleNamespace(absolute_path=REPO_ROOT / "bin/nbns-netbsd4be/nbns-advertiser"),
        }

        with mock.patch("timecapsulesmb.app.ops.deploy.load_env_config", return_value=AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"})):
            with mock.patch("timecapsulesmb.app.ops.deploy.resolve_validated_managed_target", return_value=target):
                with mock.patch("timecapsulesmb.app.ops.deploy.resolve_app_paths", return_value=SimpleNamespace(distribution_root=REPO_ROOT)):
                    with mock.patch("timecapsulesmb.app.ops.deploy.validate_artifacts", return_value=[("smbd", True, "ok")]):
                        with mock.patch("timecapsulesmb.app.ops.deploy.resolve_payload_artifacts", return_value=artifacts):
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
            "mdns-advertiser": SimpleNamespace(absolute_path=REPO_ROOT / "bin/mdns/mdns-advertiser"),
            "nbns-advertiser": SimpleNamespace(absolute_path=REPO_ROOT / "bin/nbns/nbns-advertiser"),
        }

        with mock.patch("timecapsulesmb.app.ops.deploy.load_env_config", return_value=AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"})):
            with mock.patch("timecapsulesmb.app.ops.deploy.resolve_validated_managed_target", return_value=target):
                with mock.patch("timecapsulesmb.app.ops.deploy.resolve_app_paths", return_value=SimpleNamespace(distribution_root=REPO_ROOT)):
                    with mock.patch("timecapsulesmb.app.ops.deploy.validate_artifacts", return_value=[("smbd", True, "ok")]):
                        with mock.patch("timecapsulesmb.app.ops.deploy.resolve_payload_artifacts", return_value=artifacts):
                            with mock.patch("timecapsulesmb.app.ops.deploy.run_remote_actions") as remote_actions:
                                rc = service.run_api_request(
                                    {"operation": "deploy", "params": {"dry_run": False, "confirm_deploy": True}},
                                    collector.sink,
                                )

        self.assertEqual(rc, 1)
        self.assert_confirmation(
            collector,
            "deploy.reboot",
            {
                "device_name": "Time Capsule",
                "requires_reboot": True,
                "no_reboot": False,
                "no_wait": False,
                "startup_mode": "reboot_then_verify",
            },
        )
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

        with mock.patch("timecapsulesmb.app.ops.deploy.load_env_config", return_value=AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"})):
            with mock.patch("timecapsulesmb.app.ops.deploy.resolve_validated_managed_target", return_value=target):
                with mock.patch("timecapsulesmb.app.ops.deploy.resolve_app_paths", return_value=SimpleNamespace(distribution_root=REPO_ROOT)):
                    with mock.patch("timecapsulesmb.app.ops.deploy.validate_artifacts", return_value=[("smbd", True, "ok")]):
                        with mock.patch("timecapsulesmb.app.ops.deploy.resolve_payload_artifacts", return_value=artifacts):
                            with mock.patch("timecapsulesmb.app.ops.deploy.wait_for_mast_volumes_conn") as read_mast:
                                with mock.patch("timecapsulesmb.app.ops.deploy.run_remote_actions") as remote_actions:
                                    rc = service.run_api_request(
                                        {"operation": "deploy", "params": {"dry_run": False, "confirm_deploy": True}},
                                        collector.sink,
                                    )

        self.assertEqual(rc, 1)
        self.assert_confirmation(
            collector,
            "deploy.netbsd4",
            {
                "device_name": "Time Capsule",
                "netbsd4": True,
                "no_reboot": False,
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
            "mdns-advertiser": SimpleNamespace(absolute_path=REPO_ROOT / "bin/mdns/mdns-advertiser"),
            "nbns-advertiser": SimpleNamespace(absolute_path=REPO_ROOT / "bin/nbns/nbns-advertiser"),
        }

        with mock.patch("timecapsulesmb.app.ops.deploy.load_env_config", return_value=AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"})):
            with mock.patch("timecapsulesmb.app.ops.deploy.resolve_validated_managed_target", return_value=target):
                with mock.patch("timecapsulesmb.app.ops.deploy.resolve_app_paths", return_value=SimpleNamespace(distribution_root=REPO_ROOT)):
                    with mock.patch("timecapsulesmb.app.ops.deploy.validate_artifacts", return_value=[("smbd", True, "ok")]):
                        with mock.patch("timecapsulesmb.app.ops.deploy.resolve_payload_artifacts", return_value=artifacts):
                            with mock.patch("timecapsulesmb.app.ops.deploy.wait_for_mast_volumes_conn") as read_mast:
                                with mock.patch("timecapsulesmb.app.ops.deploy.run_remote_actions") as remote_actions:
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
                "no_reboot": False,
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
            "mdns-advertiser": SimpleNamespace(absolute_path=REPO_ROOT / "bin/mdns-netbsd4be/mdns-advertiser"),
            "nbns-advertiser": SimpleNamespace(absolute_path=REPO_ROOT / "bin/nbns-netbsd4be/nbns-advertiser"),
        }

        with mock.patch("timecapsulesmb.app.ops.deploy.load_env_config", return_value=AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"})):
            with mock.patch("timecapsulesmb.app.ops.deploy.resolve_validated_managed_target", return_value=target):
                with mock.patch("timecapsulesmb.app.ops.deploy.resolve_app_paths", return_value=SimpleNamespace(distribution_root=REPO_ROOT)):
                    with mock.patch("timecapsulesmb.app.ops.deploy.validate_artifacts", return_value=[("smbd", True, "ok")]):
                        with mock.patch("timecapsulesmb.app.ops.deploy.resolve_payload_artifacts", return_value=artifacts):
                            with mock.patch("timecapsulesmb.app.ops.deploy.wait_for_mast_volumes_conn") as read_mast:
                                with mock.patch("timecapsulesmb.app.ops.deploy.run_remote_actions") as remote_actions:
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
                "no_reboot": False,
                "no_wait": True,
                "startup_mode": "reboot_then_activate",
            },
        )
        self.assertIn("without running Samba activation", details["message"])
        read_mast.assert_not_called()
        remote_actions.assert_not_called()

    def test_deploy_requires_deploy_confirmation_even_without_reboot(self) -> None:
        collector = CollectingSink()
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")
        target = SimpleNamespace(connection=connection, probe_state=probed_state())
        artifacts = {
            "smbd": SimpleNamespace(absolute_path=REPO_ROOT / "bin/samba4/smbd"),
            "mdns-advertiser": SimpleNamespace(absolute_path=REPO_ROOT / "bin/mdns/mdns-advertiser"),
            "nbns-advertiser": SimpleNamespace(absolute_path=REPO_ROOT / "bin/nbns/nbns-advertiser"),
        }

        with mock.patch("timecapsulesmb.app.ops.deploy.load_env_config", return_value=AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"})):
            with mock.patch("timecapsulesmb.app.ops.deploy.resolve_validated_managed_target", return_value=target):
                with mock.patch("timecapsulesmb.app.ops.deploy.resolve_app_paths", return_value=SimpleNamespace(distribution_root=REPO_ROOT)):
                    with mock.patch("timecapsulesmb.app.ops.deploy.validate_artifacts", return_value=[("smbd", True, "ok")]):
                        with mock.patch("timecapsulesmb.app.ops.deploy.resolve_payload_artifacts", return_value=artifacts):
                            with mock.patch("timecapsulesmb.app.ops.deploy.wait_for_mast_volumes_conn") as read_mast:
                                rc = service.run_api_request(
                                    {"operation": "deploy", "params": {"dry_run": False, "no_reboot": True}},
                                    collector.sink,
                                )

        self.assertEqual(rc, 1)
        error = self.assert_confirmation(
            collector,
            "deploy.activate_now",
            {
                "device_name": "Time Capsule",
                "netbsd4": False,
                "no_reboot": True,
                "no_wait": False,
                "startup_mode": "activate_now",
            },
        )
        self.assertEqual(error["action_title"], "Deploy and start SMB")
        read_mast.assert_not_called()

    def test_deploy_no_reboot_no_wait_confirmation_treats_no_wait_as_inapplicable(self) -> None:
        collector = CollectingSink()
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")
        target = SimpleNamespace(connection=connection, probe_state=probed_state())
        artifacts = {
            "smbd": SimpleNamespace(absolute_path=REPO_ROOT / "bin/samba4/smbd"),
            "mdns-advertiser": SimpleNamespace(absolute_path=REPO_ROOT / "bin/mdns/mdns-advertiser"),
            "nbns-advertiser": SimpleNamespace(absolute_path=REPO_ROOT / "bin/nbns/nbns-advertiser"),
        }

        with mock.patch("timecapsulesmb.app.ops.deploy.load_env_config", return_value=AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"})):
            with mock.patch("timecapsulesmb.app.ops.deploy.resolve_validated_managed_target", return_value=target):
                with mock.patch("timecapsulesmb.app.ops.deploy.resolve_app_paths", return_value=SimpleNamespace(distribution_root=REPO_ROOT)):
                    with mock.patch("timecapsulesmb.app.ops.deploy.validate_artifacts", return_value=[("smbd", True, "ok")]):
                        with mock.patch("timecapsulesmb.app.ops.deploy.resolve_payload_artifacts", return_value=artifacts):
                            with mock.patch("timecapsulesmb.app.ops.deploy.wait_for_mast_volumes_conn") as read_mast:
                                rc = service.run_api_request(
                                    {
                                        "operation": "deploy",
                                        "params": {"dry_run": False, "no_reboot": True, "no_wait": True},
                                    },
                                    collector.sink,
                                )

        self.assertEqual(rc, 1)
        self.assert_confirmation(
            collector,
            "deploy.activate_now",
            {
                "device_name": "Time Capsule",
                "netbsd4": False,
                "no_reboot": True,
                "no_wait": False,
                "startup_mode": "activate_now",
            },
        )
        read_mast.assert_not_called()

    def test_deploy_netbsd4_no_reboot_uses_activate_now_confirmation(self) -> None:
        collector = CollectingSink()
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")
        target = SimpleNamespace(connection=connection, probe_state=netbsd4_probed_state())
        artifacts = {
            "smbd": SimpleNamespace(absolute_path=REPO_ROOT / "bin/samba4-netbsd4be/smbd"),
            "mdns-advertiser": SimpleNamespace(absolute_path=REPO_ROOT / "bin/mdns-netbsd4be/mdns-advertiser"),
            "nbns-advertiser": SimpleNamespace(absolute_path=REPO_ROOT / "bin/nbns-netbsd4be/nbns-advertiser"),
        }

        with mock.patch("timecapsulesmb.app.ops.deploy.load_env_config", return_value=AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"})):
            with mock.patch("timecapsulesmb.app.ops.deploy.resolve_validated_managed_target", return_value=target):
                with mock.patch("timecapsulesmb.app.ops.deploy.resolve_app_paths", return_value=SimpleNamespace(distribution_root=REPO_ROOT)):
                    with mock.patch("timecapsulesmb.app.ops.deploy.validate_artifacts", return_value=[("smbd", True, "ok")]):
                        with mock.patch("timecapsulesmb.app.ops.deploy.resolve_payload_artifacts", return_value=artifacts):
                            with mock.patch("timecapsulesmb.app.ops.deploy.wait_for_mast_volumes_conn") as read_mast:
                                with mock.patch("timecapsulesmb.app.ops.deploy.run_remote_actions") as remote_actions:
                                    rc = service.run_api_request(
                                        {"operation": "deploy", "params": {"dry_run": False, "no_reboot": True}},
                                        collector.sink,
                                    )

        self.assertEqual(rc, 1)
        self.assert_confirmation(
            collector,
            "deploy.activate_now",
            {
                "device_name": "Time Capsule",
                "netbsd4": True,
                "requires_reboot": False,
                "no_reboot": True,
                "no_wait": False,
                "startup_mode": "activate_now",
            },
        )
        read_mast.assert_not_called()
        remote_actions.assert_not_called()

    def test_deploy_accepts_backend_confirmation_id_before_remote_writes(self) -> None:
        first = CollectingSink()
        second = CollectingSink()
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")
        target = SimpleNamespace(connection=connection, probe_state=probed_state())
        artifacts = {
            "smbd": SimpleNamespace(absolute_path=REPO_ROOT / "bin/samba4/smbd"),
            "mdns-advertiser": SimpleNamespace(absolute_path=REPO_ROOT / "bin/mdns/mdns-advertiser"),
            "nbns-advertiser": SimpleNamespace(absolute_path=REPO_ROOT / "bin/nbns/nbns-advertiser"),
        }
        payload_home = build_dry_run_payload_home(MANAGED_PAYLOAD_DIR_NAME)
        base_params = {"dry_run": False, "no_reboot": True, "mount_wait": 30}

        with mock.patch("timecapsulesmb.app.ops.deploy.load_env_config", return_value=AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"})):
            with mock.patch("timecapsulesmb.app.ops.deploy.resolve_validated_managed_target", return_value=target):
                with mock.patch("timecapsulesmb.app.ops.deploy.resolve_app_paths", return_value=SimpleNamespace(distribution_root=REPO_ROOT)):
                    with mock.patch("timecapsulesmb.app.ops.deploy.validate_artifacts", return_value=[("smbd", True, "ok")]):
                        with mock.patch("timecapsulesmb.app.ops.deploy.resolve_payload_artifacts", return_value=artifacts):
                            rc = service.run_api_request(
                                {"operation": "deploy", "params": dict(base_params)},
                                first.sink,
                            )

        self.assertEqual(rc, 1)
        confirmation_id = first.events_of_type("error")[0]["details"]["confirmation_id"]

        with mock.patch("timecapsulesmb.app.ops.deploy.load_env_config", return_value=AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"})):
            with mock.patch("timecapsulesmb.app.ops.deploy.resolve_validated_managed_target", return_value=target):
                with mock.patch("timecapsulesmb.app.ops.deploy.resolve_app_paths", return_value=SimpleNamespace(distribution_root=REPO_ROOT)):
                    with mock.patch("timecapsulesmb.app.ops.deploy.validate_artifacts", return_value=[("smbd", True, "ok")]):
                        with mock.patch("timecapsulesmb.app.ops.deploy.resolve_payload_artifacts", return_value=artifacts):
                            with mock.patch("timecapsulesmb.app.ops.deploy.wait_for_mast_volumes_conn", return_value=SimpleNamespace(volumes=("dk2",), attempts=1, raw_output="")):
                                with mock.patch("timecapsulesmb.app.ops.deploy.select_payload_home_with_diagnostics_conn", return_value=SimpleNamespace(payload_home=payload_home)):
                                    with mock.patch("timecapsulesmb.app.ops.deploy.verify_payload_home_conn", return_value=SimpleNamespace(ok=True, detail="ok")):
                                        with mock.patch("timecapsulesmb.app.ops.deploy.upload_deployment_payload") as upload:
                                            with mock.patch("timecapsulesmb.app.ops.deploy.run_remote_actions"):
                                                with mock.patch("timecapsulesmb.app.ops.deploy.flush_remote_filesystem_writes"):
                                                    with mock.patch("timecapsulesmb.app.ops.deploy.verify_managed_runtime", return_value=managed_runtime_probe()):
                                                        confirmed = dict(base_params)
                                                        confirmed["confirmation_id"] = confirmation_id
                                                        rc = service.run_api_request(
                                                            {"operation": "deploy", "params": confirmed},
                                                            second.sink,
                                                        )

        self.assertEqual(rc, 0)
        upload.assert_called_once()
        self.assertEqual(second.events_of_type("error"), [])

    def test_deploy_rejects_boolean_mount_wait_before_remote_connection(self) -> None:
        collector = CollectingSink()

        with mock.patch("timecapsulesmb.app.ops.deploy.load_env_config") as load_config:
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
                with mock.patch("timecapsulesmb.app.ops.deploy.load_env_config") as load_config:
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

    def test_deploy_no_reboot_uploads_and_activates_without_reboot_wait(self) -> None:
        collector = CollectingSink()
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")
        target = SimpleNamespace(connection=connection, probe_state=probed_state())
        artifacts = {
            "smbd": SimpleNamespace(absolute_path=REPO_ROOT / "bin/samba4/smbd"),
            "mdns-advertiser": SimpleNamespace(absolute_path=REPO_ROOT / "bin/mdns/mdns-advertiser"),
            "nbns-advertiser": SimpleNamespace(absolute_path=REPO_ROOT / "bin/nbns/nbns-advertiser"),
        }
        payload_home = build_dry_run_payload_home(MANAGED_PAYLOAD_DIR_NAME)

        with mock.patch("timecapsulesmb.app.ops.deploy.load_env_config", return_value=AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"})):
            with mock.patch("timecapsulesmb.app.ops.deploy.resolve_validated_managed_target", return_value=target):
                with mock.patch("timecapsulesmb.app.ops.deploy.resolve_app_paths", return_value=SimpleNamespace(distribution_root=REPO_ROOT)):
                    with mock.patch("timecapsulesmb.app.ops.deploy.validate_artifacts", return_value=[("smbd", True, "ok")]):
                        with mock.patch("timecapsulesmb.app.ops.deploy.resolve_payload_artifacts", return_value=artifacts):
                            with mock.patch("timecapsulesmb.app.ops.deploy.wait_for_mast_volumes_conn", return_value=SimpleNamespace(volumes=("dk2",), attempts=1, raw_output="")):
                                with mock.patch("timecapsulesmb.app.ops.deploy.select_payload_home_with_diagnostics_conn", return_value=SimpleNamespace(payload_home=payload_home)):
                                    with mock.patch("timecapsulesmb.app.ops.deploy.verify_payload_home_conn", return_value=SimpleNamespace(ok=True, detail="ok")):
                                        with mock.patch("timecapsulesmb.app.ops.deploy.upload_deployment_payload") as upload:
                                            with mock.patch("timecapsulesmb.app.ops.deploy.run_remote_actions") as remote_actions:
                                                with mock.patch("timecapsulesmb.app.ops.deploy.flush_remote_filesystem_writes"):
                                                    with mock.patch("timecapsulesmb.app.ops.deploy.wait_for_ssh_state_conn") as wait:
                                                        with mock.patch("timecapsulesmb.app.ops.deploy.verify_managed_runtime", return_value=managed_runtime_probe()) as verify_runtime:
                                                            with mock.patch("timecapsulesmb.app.ops.deploy.render_flash_runtime_config", return_value="runtime\n") as render_runtime:
                                                                rc = service.run_api_request(
                                                                    {
                                                                        "operation": "deploy",
                                                                        "params": {
                                                                            "dry_run": False,
                                                                            "no_reboot": True,
                                                                            "confirm_deploy": True,
                                                                            "internal_share_use_disk_root": False,
                                                                            "any_protocol": False,
                                                                            "debug_logging": False,
                                                                            "ata_idle_seconds": 0,
                                                                            "ata_standby": 0,
                                                                        },
                                                                    },
                                                                    collector.sink,
                                                                )

        self.assertEqual(rc, 0)
        upload.assert_called_once()
        upload_sources = upload.call_args.kwargs["source_resolver"]
        self.assertIn("packaged:boot.sh", upload_sources)
        self.assertIn("packaged:manager.sh", upload_sources)
        self.assertNotIn("packaged:start-samba.sh", upload_sources)
        self.assertNotIn("packaged:watchdog.sh", upload_sources)
        self.assertEqual(remote_actions.call_count, 3)
        wait.assert_not_called()
        verify_runtime.assert_called_once()
        render_runtime.assert_called_once()
        self.assertEqual(render_runtime.call_args.kwargs["internal_share_use_disk_root"], False)
        self.assertEqual(render_runtime.call_args.kwargs["any_protocol"], False)
        self.assertEqual(render_runtime.call_args.kwargs["debug_logging"], False)
        self.assertEqual(render_runtime.call_args.kwargs["ata_idle_seconds"], 0)
        self.assertEqual(render_runtime.call_args.kwargs["ata_standby"], 0)
        self.assertEqual(collector.events_of_type("result")[0]["payload"]["rebooted"], False)
        self.assertEqual(collector.events_of_type("result")[0]["payload"]["verified"], True)

    def test_deploy_emits_grouped_upload_stages_before_each_upload_group(self) -> None:
        collector = CollectingSink()
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")
        target = SimpleNamespace(connection=connection, probe_state=probed_state())
        artifacts = {
            "smbd": SimpleNamespace(absolute_path=REPO_ROOT / "bin/samba4/smbd"),
            "mdns-advertiser": SimpleNamespace(absolute_path=REPO_ROOT / "bin/mdns/mdns-advertiser"),
            "nbns-advertiser": SimpleNamespace(absolute_path=REPO_ROOT / "bin/nbns/nbns-advertiser"),
        }
        payload_home = build_dry_run_payload_home(MANAGED_PAYLOAD_DIR_NAME)

        def fake_upload(plan, *, connection, source_resolver, on_uploading=None):
            for transfer in plan.uploads:
                if on_uploading is not None:
                    on_uploading(transfer)

        with mock.patch("timecapsulesmb.app.ops.deploy.load_env_config", return_value=AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"})):
            with mock.patch("timecapsulesmb.app.ops.deploy.resolve_validated_managed_target", return_value=target):
                with mock.patch("timecapsulesmb.app.ops.deploy.resolve_app_paths", return_value=SimpleNamespace(distribution_root=REPO_ROOT)):
                    with mock.patch("timecapsulesmb.app.ops.deploy.validate_artifacts", return_value=[("smbd", True, "ok")]):
                        with mock.patch("timecapsulesmb.app.ops.deploy.resolve_payload_artifacts", return_value=artifacts):
                            with mock.patch("timecapsulesmb.app.ops.deploy.wait_for_mast_volumes_conn", return_value=SimpleNamespace(volumes=("dk2",), attempts=1, raw_output="")):
                                with mock.patch("timecapsulesmb.app.ops.deploy.select_payload_home_with_diagnostics_conn", return_value=SimpleNamespace(payload_home=payload_home)):
                                    with mock.patch("timecapsulesmb.app.ops.deploy.verify_payload_home_conn", return_value=SimpleNamespace(ok=True, detail="ok")):
                                        with mock.patch("timecapsulesmb.app.ops.deploy.upload_deployment_payload", side_effect=fake_upload):
                                            with mock.patch("timecapsulesmb.app.ops.deploy.run_remote_actions"):
                                                with mock.patch("timecapsulesmb.app.ops.deploy.flush_remote_filesystem_writes"):
                                                    with mock.patch("timecapsulesmb.app.ops.deploy.verify_managed_runtime", return_value=managed_runtime_probe()):
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
        stages = [event["stage"] for event in collector.events_of_type("stage")]
        upload_stages = [stage for stage in stages if str(stage).startswith("upload_")]
        self.assertEqual(
            upload_stages,
            [
                "upload_smbd",
                "upload_mdns_advertiser",
                "upload_nbns_advertiser",
                "upload_boot_files",
                "upload_runtime_config",
                "upload_samba_accounts",
            ],
        )

    def test_deploy_no_wait_requests_reboot_without_wait_or_runtime_verify(self) -> None:
        collector = CollectingSink()
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")
        target = SimpleNamespace(connection=connection, probe_state=probed_state())
        artifacts = {
            "smbd": SimpleNamespace(absolute_path=REPO_ROOT / "bin/samba4/smbd"),
            "mdns-advertiser": SimpleNamespace(absolute_path=REPO_ROOT / "bin/mdns/mdns-advertiser"),
            "nbns-advertiser": SimpleNamespace(absolute_path=REPO_ROOT / "bin/nbns/nbns-advertiser"),
        }
        payload_home = build_dry_run_payload_home(MANAGED_PAYLOAD_DIR_NAME)

        with mock.patch("timecapsulesmb.app.ops.deploy.load_env_config", return_value=AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"})):
            with mock.patch("timecapsulesmb.app.ops.deploy.resolve_validated_managed_target", return_value=target):
                with mock.patch("timecapsulesmb.app.ops.deploy.resolve_app_paths", return_value=SimpleNamespace(distribution_root=REPO_ROOT)):
                    with mock.patch("timecapsulesmb.app.ops.deploy.validate_artifacts", return_value=[("smbd", True, "ok")]):
                        with mock.patch("timecapsulesmb.app.ops.deploy.resolve_payload_artifacts", return_value=artifacts):
                            with mock.patch("timecapsulesmb.app.ops.deploy.wait_for_mast_volumes_conn", return_value=SimpleNamespace(volumes=("dk2",), attempts=1, raw_output="")):
                                with mock.patch("timecapsulesmb.app.ops.deploy.select_payload_home_with_diagnostics_conn", return_value=SimpleNamespace(payload_home=payload_home)):
                                    with mock.patch("timecapsulesmb.app.ops.deploy.verify_payload_home_conn", return_value=SimpleNamespace(ok=True, detail="ok")):
                                        with mock.patch("timecapsulesmb.app.ops.deploy.upload_deployment_payload"):
                                            with mock.patch("timecapsulesmb.app.ops.deploy.run_remote_actions"):
                                                with mock.patch("timecapsulesmb.app.ops.deploy.flush_remote_filesystem_writes"):
                                                    with mock.patch("timecapsulesmb.app.ops.deploy.remote_request_reboot") as reboot:
                                                        with mock.patch("timecapsulesmb.app.ops.deploy.wait_for_ssh_state_conn") as wait:
                                                            with mock.patch("timecapsulesmb.app.ops.deploy.verify_managed_runtime") as verify_runtime:
                                                                rc = service.run_api_request(
                                                                    {
                                                                        "operation": "deploy",
                                                                        "params": {
                                                                            "dry_run": False,
                                                                            "confirm_deploy": True,
                                                                            "confirm_reboot": True,
                                                                            "no_wait": True,
                                                                        },
                                                                    },
                                                                    collector.sink,
                                                                )

        self.assertEqual(rc, 0)
        reboot.assert_called_once()
        wait.assert_not_called()
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
            "mdns-advertiser": SimpleNamespace(absolute_path=REPO_ROOT / "bin/mdns-netbsd4be/mdns-advertiser"),
            "nbns-advertiser": SimpleNamespace(absolute_path=REPO_ROOT / "bin/nbns-netbsd4be/nbns-advertiser"),
        }
        payload_home = build_dry_run_payload_home(MANAGED_PAYLOAD_DIR_NAME)

        with mock.patch("timecapsulesmb.app.ops.deploy.load_env_config", return_value=AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"})):
            with mock.patch("timecapsulesmb.app.ops.deploy.resolve_validated_managed_target", return_value=target):
                with mock.patch("timecapsulesmb.app.ops.deploy.resolve_app_paths", return_value=SimpleNamespace(distribution_root=REPO_ROOT)):
                    with mock.patch("timecapsulesmb.app.ops.deploy.validate_artifacts", return_value=[("smbd", True, "ok")]):
                        with mock.patch("timecapsulesmb.app.ops.deploy.resolve_payload_artifacts", return_value=artifacts):
                            with mock.patch("timecapsulesmb.app.ops.deploy.wait_for_mast_volumes_conn", return_value=SimpleNamespace(volumes=("dk2",), attempts=1, raw_output="")):
                                with mock.patch("timecapsulesmb.app.ops.deploy.select_payload_home_with_diagnostics_conn", return_value=SimpleNamespace(payload_home=payload_home)):
                                    with mock.patch("timecapsulesmb.app.ops.deploy.verify_payload_home_conn", return_value=SimpleNamespace(ok=True, detail="ok")):
                                        with mock.patch("timecapsulesmb.app.ops.deploy.upload_deployment_payload"):
                                            with mock.patch("timecapsulesmb.app.ops.deploy.run_remote_actions"):
                                                with mock.patch("timecapsulesmb.app.ops.deploy.flush_remote_filesystem_writes"):
                                                    with mock.patch("timecapsulesmb.app.ops.deploy.remote_request_reboot") as reboot:
                                                        with mock.patch("timecapsulesmb.services.activation.probe_netbsd4_rc_local_autostart_conn") as autostart_probe:
                                                            rc = service.run_api_request(
                                                                {
                                                                    "operation": "deploy",
                                                                    "params": {
                                                                        "dry_run": False,
                                                                        "confirm_deploy": True,
                                                                        "no_wait": True,
                                                                    },
                                                                },
                                                                collector.sink,
                                                            )

        self.assertEqual(rc, 0)
        self.assertFalse(connection.remote_has_scp)
        reboot.assert_called_once()
        autostart_probe.assert_not_called()
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
            "mdns-advertiser": SimpleNamespace(absolute_path=REPO_ROOT / "bin/mdns/mdns-advertiser"),
            "nbns-advertiser": SimpleNamespace(absolute_path=REPO_ROOT / "bin/nbns/nbns-advertiser"),
        }
        payload_home = build_dry_run_payload_home(MANAGED_PAYLOAD_DIR_NAME)

        with mock.patch("timecapsulesmb.app.ops.deploy.load_env_config", return_value=AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"})):
            with mock.patch("timecapsulesmb.app.ops.deploy.resolve_validated_managed_target", return_value=target):
                with mock.patch("timecapsulesmb.app.ops.deploy.resolve_app_paths", return_value=SimpleNamespace(distribution_root=REPO_ROOT)):
                    with mock.patch("timecapsulesmb.app.ops.deploy.validate_artifacts", return_value=[("smbd", True, "ok")]):
                        with mock.patch("timecapsulesmb.app.ops.deploy.resolve_payload_artifacts", return_value=artifacts):
                            with mock.patch("timecapsulesmb.app.ops.deploy.wait_for_mast_volumes_conn", return_value=SimpleNamespace(volumes=("dk2",), attempts=1, raw_output="")):
                                with mock.patch("timecapsulesmb.app.ops.deploy.select_payload_home_with_diagnostics_conn", return_value=SimpleNamespace(payload_home=payload_home)):
                                    with mock.patch("timecapsulesmb.app.ops.deploy.verify_payload_home_conn", return_value=SimpleNamespace(ok=True, detail="ok")):
                                        with mock.patch("timecapsulesmb.app.ops.deploy.upload_deployment_payload"):
                                            with mock.patch("timecapsulesmb.app.ops.deploy.run_remote_actions"):
                                                with mock.patch("timecapsulesmb.app.ops.deploy.flush_remote_filesystem_writes"):
                                                    with mock.patch("timecapsulesmb.app.ops.deploy.remote_request_reboot", side_effect=SshError("ssh command failed with rc=255")) as reboot:
                                                        with mock.patch("timecapsulesmb.app.ops.deploy.wait_for_ssh_state_conn") as wait:
                                                            with mock.patch("timecapsulesmb.app.ops.deploy.verify_managed_runtime") as verify_runtime:
                                                                rc = service.run_api_request(
                                                                    {
                                                                        "operation": "deploy",
                                                                        "params": {
                                                                            "dry_run": False,
                                                                            "confirm_deploy": True,
                                                                            "confirm_reboot": True,
                                                                            "no_wait": True,
                                                                        },
                                                                    },
                                                                    collector.sink,
                                                                )

        self.assertEqual(rc, 1)
        reboot.assert_called_once()
        wait.assert_not_called()
        verify_runtime.assert_not_called()
        errors = collector.events_of_type("error")
        self.assertEqual(errors[0]["code"], "remote_error")
        self.assertIn("ssh command failed with rc=255", errors[0]["message"])
        self.assertEqual(collector.events_of_type("result"), [])

    def test_deploy_request_reboot_and_wait_records_lifecycle_fields(self) -> None:
        from timecapsulesmb.app.ops import deploy as deploy_ops

        collector = CollectingSink()
        context = AppOperationContext("deploy", collector.sink)
        context.update_fields(reboot_was_attempted=False, device_came_back_after_reboot=False)
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")
        with mock.patch("timecapsulesmb.app.ops.deploy.remote_request_reboot") as reboot:
            with mock.patch("timecapsulesmb.app.ops.deploy.wait_for_ssh_state_conn", side_effect=[True, True]) as wait:
                deploy_ops.request_reboot_and_wait(
                    context,
                    connection,
                    strategy="ssh_shutdown_then_reboot",
                    reboot_no_down_message="device did not go down",
                )

        reboot.assert_called_once()
        self.assertEqual([call.kwargs["expected_up"] for call in wait.call_args_list], [False, True])
        self.assertEqual(context.finish_fields["reboot_was_attempted"], True)
        self.assertEqual(context.finish_fields["device_came_back_after_reboot"], True)
        self.assertEqual(context.diagnostics.debug_fields["reboot_request_strategy"], "ssh_shutdown_then_reboot")
        self.assertEqual(context.diagnostics.debug_fields["ssh_reboot_attempted"], True)
        self.assertEqual(context.diagnostics.debug_fields["ssh_reboot_succeeded"], True)

    def test_deploy_verify_runtime_failure_adds_runtime_logs_to_error_context(self) -> None:
        from timecapsulesmb.app.ops import deploy as deploy_ops

        collector = CollectingSink()
        context = AppOperationContext("deploy", collector.sink)
        connection = SshConnection("root@169.254.44.9", "pw", "-o foo")
        smbd = ManagedSmbdProbeResult(True, "managed smbd ready", ("PASS:managed smbd ready",))
        mdns = ManagedMdnsTakeoverProbeResult(
            False,
            "managed mDNS takeover probe timed out",
            ("FAIL:managed mDNS takeover probe timed out",),
        )
        verification = ManagedRuntimeProbeResult(
            ready=False,
            detail="runtime verification timed out after 180s; managed smbd ready; managed mDNS takeover probe timed out",
            smbd=smbd,
            mdns=mdns,
            lines=smbd.lines + mdns.lines + ("FAIL:runtime verification timed out after 180s",),
        )
        with mock.patch("timecapsulesmb.app.ops.deploy.verify_managed_runtime", return_value=verification):
            with mock.patch(
                "timecapsulesmb.app.ops.deploy.read_runtime_log_tails_conn",
                return_value={
                    "remote_manager_log_tail": "manager: mDNS startup deferred; no usable address has appeared yet",
                    "remote_mdns_log_tail": "mdns: before interface probe",
                },
            ):
                with mock.patch(
                    "timecapsulesmb.app.ops.deploy.read_remote_network_diagnostics_conn",
                    return_value={
                        "remote_network_config": {"ssh_target_host": "169.254.44.9"},
                        "remote_network_target_ip_matches": [],
                    },
                ):
                    with self.assertRaises(AppOperationError) as raised:
                        deploy_ops.verify_runtime(
                            context,
                            connection,
                            stage="verify_runtime_activation",
                            timeout_seconds=180,
                            failure_message="NetBSD4 activation failed.",
                        )

        self.assertEqual(raised.exception.code, "remote_error")
        self.assertEqual(
            context.diagnostics.debug_fields["remote_manager_log_tail"],
            "manager: mDNS startup deferred; no usable address has appeared yet",
        )
        self.assertEqual(context.diagnostics.debug_fields["remote_mdns_log_tail"], "mdns: before interface probe")
        self.assertEqual(context.diagnostics.debug_fields["runtime_startup_failure"], "network_auto_ip_unavailable")
        error = context.diagnostic_error(str(raised.exception))
        self.assertIn("remote_manager_log_tail=manager: mDNS startup deferred; no usable address has appeared yet", error)
        self.assertIn("remote_mdns_log_tail=mdns: before interface probe", error)
        self.assertIn("remote_network_target_ip_matches=[]", error)

    def test_deploy_request_ssh_reboot_reports_timeout_when_request_error_is_required(self) -> None:
        from timecapsulesmb.app.ops import deploy as deploy_ops

        collector = CollectingSink()
        context = AppOperationContext("deploy", collector.sink)
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")
        with mock.patch(
            "timecapsulesmb.app.ops.deploy.remote_request_reboot",
            side_effect=SshCommandTimeout("Timed out waiting for ssh command to finish: reboot"),
        ):
            with self.assertRaises(AppOperationError) as raised:
                deploy_ops.request_ssh_reboot(context, connection, raise_on_request_error=True)

        self.assertEqual(raised.exception.code, "remote_error")
        self.assertIn("Timed out waiting for ssh command to finish: reboot", str(raised.exception))

    def test_deploy_reports_no_mast_volumes_as_remote_error(self) -> None:
        collector = CollectingSink()
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")
        target = SimpleNamespace(connection=connection, probe_state=probed_state())
        artifacts = {
            "smbd": SimpleNamespace(absolute_path=REPO_ROOT / "bin/samba4/smbd"),
            "mdns-advertiser": SimpleNamespace(absolute_path=REPO_ROOT / "bin/mdns/mdns-advertiser"),
            "nbns-advertiser": SimpleNamespace(absolute_path=REPO_ROOT / "bin/nbns/nbns-advertiser"),
        }

        with mock.patch("timecapsulesmb.app.ops.deploy.load_env_config", return_value=AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"})):
            with mock.patch("timecapsulesmb.app.ops.deploy.resolve_validated_managed_target", return_value=target):
                with mock.patch("timecapsulesmb.app.ops.deploy.resolve_app_paths", return_value=SimpleNamespace(distribution_root=REPO_ROOT)):
                    with mock.patch("timecapsulesmb.app.ops.deploy.validate_artifacts", return_value=[("smbd", True, "ok")]):
                        with mock.patch("timecapsulesmb.app.ops.deploy.resolve_payload_artifacts", return_value=artifacts):
                            with mock.patch("timecapsulesmb.app.ops.deploy.wait_for_mast_volumes_conn", return_value=SimpleNamespace(volumes=(), attempts=1, raw_output="")):
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
        self.assertEqual(error["recovery"]["action_ids"], [])
        self.assertEqual(self._telemetry_factory.call_args.kwargs["nbns_enabled"], True)
        finished = self._telemetry_client.emit.call_args_list[-1].kwargs
        self.assertEqual(finished["result"], "failure")
        self.assertEqual(finished["stage"], "read_mast")
        self.assertEqual(finished["device_family"], "netbsd6_samba4")
        self.assertEqual(finished["device_os_version"], "NetBSD 6.0 (earmv4)")
        self.assertEqual(finished["device_model"], "TimeCapsule8,119")
        self.assertEqual(finished["device_syap"], "119")
        self.assertEqual(finished["nbns_enabled"], True)
        self.assertEqual(finished["reboot_was_attempted"], False)
        self.assertEqual(finished["device_came_back_after_reboot"], False)
        self.assertEqual(finished["deploy_startup_mode"], "reboot_then_verify")

    def test_activate_requires_explicit_confirmation(self) -> None:
        collector = CollectingSink()

        with mock.patch("timecapsulesmb.app.ops.maintenance.load_env_config", return_value=AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"})):
            with mock.patch("timecapsulesmb.app.ops.maintenance.resolve_validated_managed_target") as resolve_target:
                with mock.patch("timecapsulesmb.services.activation.probe_managed_runtime_conn") as runtime_probe:
                    with mock.patch("timecapsulesmb.app.ops.maintenance.run_remote_actions") as remote_actions:
                        rc = service.run_api_request({"operation": "activate", "params": {}}, collector.sink)

        self.assertEqual(rc, 1)
        self.assert_confirmation(collector, "activate.netbsd4", {"netbsd4": True})
        resolve_target.assert_not_called()
        runtime_probe.assert_not_called()
        remote_actions.assert_not_called()

    def test_activate_accepts_yes_alias_for_confirmation(self) -> None:
        collector = CollectingSink()
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")
        target = SimpleNamespace(connection=connection, probe_state=netbsd4_probed_state())

        with mock.patch("timecapsulesmb.app.ops.maintenance.load_env_config", return_value=AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"})):
            with mock.patch("timecapsulesmb.app.ops.maintenance.resolve_validated_managed_target", return_value=target):
                with mock.patch("timecapsulesmb.services.activation.probe_managed_runtime_conn", return_value=managed_runtime_probe(True)):
                    with mock.patch("timecapsulesmb.app.ops.maintenance.run_remote_actions") as remote_actions:
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

        with mock.patch("timecapsulesmb.app.ops.maintenance.load_env_config", return_value=config):
            with mock.patch("timecapsulesmb.app.ops.maintenance.resolve_env_connection") as resolve_connection:
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

        with mock.patch("timecapsulesmb.app.ops.maintenance.load_env_config", return_value=config):
            with mock.patch("timecapsulesmb.app.ops.maintenance.resolve_env_connection") as resolve_connection:
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

        with mock.patch("timecapsulesmb.app.ops.maintenance.load_env_config", return_value=config):
            with mock.patch("timecapsulesmb.app.ops.maintenance.resolve_env_connection", return_value=connection):
                with mock.patch("timecapsulesmb.app.ops.maintenance.read_mast_volumes_conn") as read_mast:
                    rc = service.run_api_request(
                        {"operation": "uninstall", "params": {"confirm_uninstall": True}},
                        collector.sink,
                    )

        self.assertEqual(rc, 1)
        self.assertEqual(collector.events_of_type("error")[0]["code"], "confirmation_required")
        read_mast.assert_not_called()

    def test_uninstall_dry_run_bypasses_confirmation_and_returns_plan(self) -> None:
        collector = CollectingSink()
        config = AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"})
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")

        with mock.patch("timecapsulesmb.app.ops.maintenance.load_env_config", return_value=config):
            with mock.patch("timecapsulesmb.app.ops.maintenance.resolve_env_connection", return_value=connection):
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

        with mock.patch("timecapsulesmb.app.ops.maintenance.load_env_config", return_value=config):
            with mock.patch("timecapsulesmb.app.ops.maintenance.resolve_env_connection", return_value=connection):
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

        with mock.patch("timecapsulesmb.app.ops.maintenance.load_env_config", return_value=config):
            with mock.patch("timecapsulesmb.app.ops.maintenance.resolve_env_connection", return_value=connection):
                with mock.patch("timecapsulesmb.app.ops.maintenance.read_mast_volumes_conn", return_value=[]):
                    with mock.patch("timecapsulesmb.app.ops.maintenance.mounted_mast_volumes_conn", return_value=mounted) as mounted_mock:
                        with mock.patch("timecapsulesmb.app.ops.maintenance.remote_uninstall_payload"):
                            with mock.patch("timecapsulesmb.app.ops.deploy.remote_request_reboot") as reboot:
                                with mock.patch("timecapsulesmb.app.ops.maintenance.wait_for_ssh_state_conn") as wait:
                                    with mock.patch("timecapsulesmb.app.ops.maintenance.verify_post_uninstall") as verify:
                                        rc = service.run_api_request(
                                            {
                                                "operation": "uninstall",
                                                "params": {
                                                    "confirm_uninstall": True,
                                                    "confirm_reboot": True,
                                                    "mount_wait": 13,
                                                    "no_wait": True,
                                                },
                                            },
                                            collector.sink,
                                        )

        self.assertEqual(rc, 0)
        self.assertEqual(mounted_mock.call_args.kwargs["wait_seconds"], 13)
        reboot.assert_called_once()
        wait.assert_not_called()
        verify.assert_not_called()
        payload = collector.events_of_type("result")[0]["payload"]
        self.assertEqual(payload["reboot_requested"], True)
        self.assertEqual(payload["waited"], False)
        self.assertEqual(payload["verified"], False)

    def test_fsck_requires_confirmation_before_remote_connection(self) -> None:
        collector = CollectingSink()
        config = AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"})

        with mock.patch("timecapsulesmb.app.ops.maintenance.load_env_config", return_value=config):
            with mock.patch("timecapsulesmb.app.ops.maintenance.resolve_env_connection") as resolve_connection:
                rc = service.run_api_request({"operation": "fsck", "params": {}}, collector.sink)

        self.assertEqual(rc, 1)
        self.assert_confirmation(collector, "fsck.reboot", {"requires_reboot": True, "no_reboot": False})
        resolve_connection.assert_not_called()

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
                with mock.patch("timecapsulesmb.app.ops.maintenance.read_mast_volumes_conn", return_value=[]):
                    with mock.patch("timecapsulesmb.app.ops.maintenance.mounted_mast_volumes_conn", return_value=mounted) as mounted_mock:
                        with mock.patch("timecapsulesmb.app.ops.maintenance.run_ssh") as run_ssh:
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

    def test_fsck_dry_run_returns_plan_without_remote_fsck(self) -> None:
        collector = CollectingSink()
        config = AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"})
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")
        mounted = [MaStVolume("wd0", "dk2", "/Volumes/dk2", "Data", "uuid", True, "hfs")]

        with mock.patch("timecapsulesmb.app.ops.maintenance.load_env_config", return_value=config):
            with mock.patch("timecapsulesmb.app.ops.maintenance.resolve_env_connection", return_value=connection):
                with mock.patch("timecapsulesmb.app.ops.maintenance.read_mast_volumes_conn", return_value=[]):
                    with mock.patch("timecapsulesmb.app.ops.maintenance.mounted_mast_volumes_conn", return_value=mounted):
                        with mock.patch("timecapsulesmb.app.ops.maintenance.run_ssh") as run_ssh:
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

        with mock.patch("timecapsulesmb.app.ops.maintenance.sys.platform", "darwin"):
            with mock.patch("timecapsulesmb.app.ops.maintenance.load_optional_env_config", return_value=AppConfig.missing()):
                with mock.patch("timecapsulesmb.app.ops.maintenance.repair_xattrs_service.run_repair_structured", return_value=repair_result) as runner:
                    rc = service.run_api_request(
                        {
                            "operation": "repair-xattrs",
                            "params": {"path": "/Volumes/Data", "dry_run": True},
                        },
                        collector.sink,
                    )

        self.assertEqual(rc, 0)
        runner.assert_called_once()
        payload = collector.events_of_type("result")[0]["payload"]
        self.assertEqual(payload["finding_count"], 1)
        self.assertEqual(payload["summary"], "Found 1 metadata issue(s), 1 repairable.")
        self.assertEqual(payload["summary_text"], "Found 1 metadata issue(s), 1 repairable.")
        self.assertEqual(payload["stats"]["scanned"], 1)
        self.assertNotIsInstance(payload["summary"], dict)

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

        with mock.patch("timecapsulesmb.app.ops.maintenance.sys.platform", "darwin"):
            with mock.patch("timecapsulesmb.app.ops.maintenance.load_optional_env_config", return_value=AppConfig.missing()):
                with mock.patch("timecapsulesmb.app.ops.maintenance.repair_xattrs_service.run_repair_structured", side_effect=fake_runner):
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

    def test_repair_xattrs_rejects_invalid_path_before_runner(self) -> None:
        cases = [
            ({}, "missing required parameter: path"),
            ({"path": ""}, "missing required parameter: path"),
            ({"path": "   "}, "missing required parameter: path"),
            ({"path": True}, "path must be a path string"),
        ]
        for extra_params, message in cases:
            with self.subTest(extra_params=extra_params):
                collector = CollectingSink()
                params = {"dry_run": True}
                params.update(extra_params)
                with mock.patch("timecapsulesmb.app.ops.maintenance.sys.platform", "darwin"):
                    with mock.patch("timecapsulesmb.app.ops.maintenance.load_optional_env_config") as load_config:
                        with mock.patch("timecapsulesmb.app.ops.maintenance.repair_xattrs_service.run_repair_structured") as runner:
                            rc = service.run_api_request(
                                {
                                    "operation": "repair-xattrs",
                                    "params": params,
                                },
                                collector.sink,
                            )

                self.assertEqual(rc, 1)
                error = self.assert_single_terminal_event(collector, "error")
                self.assertEqual(error["code"], "validation_failed")
                self.assertEqual(error["message"], message)
                self.assertEqual(error["recovery"]["title"], "Invalid repair options")
                load_config.assert_not_called()
                runner.assert_not_called()

    def test_repair_xattrs_rejects_invalid_max_depth_before_runner(self) -> None:
        for max_depth in ("bad", -1, True):
            with self.subTest(max_depth=max_depth):
                collector = CollectingSink()
                with mock.patch("timecapsulesmb.app.ops.maintenance.sys.platform", "darwin"):
                    with mock.patch("timecapsulesmb.app.ops.maintenance.load_optional_env_config", return_value=AppConfig.missing()):
                        with mock.patch("timecapsulesmb.app.ops.maintenance.repair_xattrs_service.run_repair_structured") as runner:
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

        with mock.patch("timecapsulesmb.app.ops.maintenance.sys.platform", "darwin"):
            with mock.patch("timecapsulesmb.app.ops.maintenance.load_optional_env_config", return_value=AppConfig.missing()):
                with mock.patch("timecapsulesmb.app.ops.maintenance.repair_xattrs_service.run_repair_structured", return_value=repair_result) as runner:
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

        with mock.patch("timecapsulesmb.app.ops.maintenance.sys.platform", "linux"):
            with mock.patch("timecapsulesmb.app.ops.maintenance.repair_xattrs_service.run_repair_structured") as runner:
                rc = service.run_api_request(
                    {
                        "operation": "repair-xattrs",
                        "params": {"path": "/Volumes/Data", "dry_run": False},
                    },
                    collector.sink,
                )

        self.assertEqual(rc, 1)
        error = self.assert_confirmation(collector, "repair_xattrs", {"path": "/Volumes/Data"})
        self.assertEqual(collector.events_of_type("error")[0]["recovery"]["title"], "Repair confirmation required")
        runner.assert_not_called()

    def test_repair_xattrs_checks_platform_after_confirmation(self) -> None:
        collector = CollectingSink()

        with mock.patch("timecapsulesmb.app.ops.maintenance.sys.platform", "linux"):
            with mock.patch("timecapsulesmb.app.ops.maintenance.load_optional_env_config") as load_config:
                with mock.patch("timecapsulesmb.app.ops.maintenance.repair_xattrs_service.run_repair_structured") as runner:
                    rc = service.run_api_request(
                        {
                            "operation": "repair-xattrs",
                            "params": {"path": "/Volumes/Data", "dry_run": False, "confirm_repair": True},
                        },
                        collector.sink,
                    )

        self.assertEqual(rc, 1)
        error = self.assert_single_terminal_event(collector, "error")
        self.assertEqual(error["code"], "validation_failed")
        self.assertEqual(error["recovery"]["title"], "repair-xattrs requires macOS")
        load_config.assert_not_called()
        runner.assert_not_called()

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
        self.assertIs(cli_main.COMMANDS["api"], helper.main)


if __name__ == "__main__":
    unittest.main()
