"""CLI entry point, shared config loading, paths, validate-install, discover, repair-xattrs and target resolution."""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import ExitStack
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock
import timecapsulesmb.cli.main as cli_main_module
from timecapsulesmb import repair_xattrs as repair_xattrs_domain
from timecapsulesmb.cli import (
    activate,
    discover,
    doctor,
    fsck,
    paths,
    repair_xattrs,
    set_ssh,
    uninstall,
    validate_install,
)
from timecapsulesmb.cli.main import main
from timecapsulesmb.services import repair_xattrs as repair_xattrs_service
from timecapsulesmb.core.config import AppConfig
from timecapsulesmb.core.paths import AppPaths
from timecapsulesmb.discovery.bonjour import (
    BonjourDiscoverySnapshot,
    BonjourMergedDiscoveryDiagnostics,
    BonjourServiceInstance,
    BonjourResolvedService,
)
from timecapsulesmb.services.version_check import (
    DEFAULT_DOWNLOAD_URL,
    VERSION_CHECK_URL,
    VersionCheckResult,
)
from timecapsulesmb.install_validation import InstallCheckResult

from tests.cli_support import CliTestCase, FakeCommandContext, REPO_ROOT, SRC_ROOT


class CliMainTests(CliTestCase):
    def test_dispatches_to_command_handler(self) -> None:
        with mock.patch("timecapsulesmb.cli.main.COMMANDS", {"doctor": mock.Mock(return_value=7)}):
            rc = main(["doctor", "--skip-smb"])
        self.assertEqual(rc, 7)
        self._version_check.assert_called_once_with()

    def test_main_blocks_outdated_client_before_dispatch(self) -> None:
        stderr = io.StringIO()
        command = mock.Mock(return_value=7)
        self._version_check.return_value = VersionCheckResult(
            should_block=True,
            checked_url=VERSION_CHECK_URL,
            message="This version is no longer supported. Please update before continuing.",
            download_url=DEFAULT_DOWNLOAD_URL,
        )

        with mock.patch("timecapsulesmb.cli.main.COMMANDS", {"doctor": command}):
            with redirect_stderr(stderr):
                rc = main(["doctor", "--skip-smb"])

        self.assertEqual(rc, 1)
        command.assert_not_called()
        output = stderr.getvalue()
        self.assertIn(f"Checking current version from: {VERSION_CHECK_URL}", output)
        self.assertIn(f"Client version is out of date, download the latest version from: {DEFAULT_DOWNLOAD_URL}", output)

    def test_main_skips_version_check_for_command_help(self) -> None:
        command = mock.Mock(return_value=0)
        with mock.patch("timecapsulesmb.cli.main.COMMANDS", {"doctor": command}):
            rc = main(["doctor", "--help"])

        self.assertEqual(rc, 0)
        self._version_check.assert_not_called()
        command.assert_called_once_with(["--help"])

    def test_main_dispatches_when_version_check_raises(self) -> None:
        command = mock.Mock(return_value=7)
        self._version_check.side_effect = RuntimeError("version check failed")

        with mock.patch("timecapsulesmb.cli.main.COMMANDS", {"doctor": command}):
            rc = main(["doctor", "--skip-smb"])

        self.assertEqual(rc, 7)
        command.assert_called_once_with(["--skip-smb"])

    def test_main_handles_keyboard_interrupt_cleanly(self) -> None:
        stderr = io.StringIO()
        with mock.patch("timecapsulesmb.cli.main.COMMANDS", {"doctor": mock.Mock(side_effect=KeyboardInterrupt)}):
            with redirect_stderr(stderr):
                rc = main(["doctor", "--skip-smb"])
        self.assertEqual(rc, 130)
        self.assertEqual(stderr.getvalue(), "\nCancelled.\n")

    def test_main_preserves_cancelled_telemetry_on_keyboard_interrupt(self) -> None:
        stderr = io.StringIO()
        command_context = FakeCommandContext(compatibility=self.make_supported_netbsd4_compatibility())

        def fake_command(_argv):
            with command_context:
                raise KeyboardInterrupt

        with mock.patch("timecapsulesmb.cli.main.COMMANDS", {"doctor": fake_command}):
            with redirect_stderr(stderr):
                rc = main(["doctor", "--skip-smb"])
        self.assertEqual(rc, 130)
        self.assertEqual(stderr.getvalue(), "\nCancelled.\n")
        command_context.finish.assert_called_once_with(result="cancelled", error="Cancelled by user")

    def test_paths_and_validate_install_commands_are_registered(self) -> None:
        self.assertIs(cli_main_module.COMMANDS["paths"], paths.main)
        self.assertIs(cli_main_module.COMMANDS["validate-install"], validate_install.main)

    def test_paths_json_command_prints_resolved_install_paths(self) -> None:
        app_paths = AppPaths(
            distribution_root=REPO_ROOT,
            config_path=REPO_ROOT / ".env",
            state_dir=REPO_ROOT,
            package_root=SRC_ROOT / "timecapsulesmb",
        )
        output = io.StringIO()
        data = {
            "distribution_root": str(app_paths.distribution_root),
            "config_path": str(app_paths.config_path),
            "state_dir": str(app_paths.state_dir),
            "package_root": str(app_paths.package_root),
            "artifact_manifest": "manifest.json",
            "artifacts": [
                {
                    "name": "smbd",
                    "absolute_path": str(REPO_ROOT / "bin" / "samba4" / "smbd"),
                    "ok": True,
                    "message": "validated bin/samba4/smbd",
                }
            ],
        }
        with mock.patch("timecapsulesmb.cli.paths.ensure_install_id"):
            with mock.patch("timecapsulesmb.cli.paths.resolve_app_paths", return_value=app_paths):
                with mock.patch("timecapsulesmb.cli.paths.paths_to_jsonable", return_value=data):
                    with redirect_stdout(output):
                        rc = paths.main(["--json"])

        self.assertEqual(rc, 0)
        rendered = json.loads(output.getvalue())
        self.assertEqual(rendered["distribution_root"], str(REPO_ROOT))
        self.assertEqual(rendered["config_path"], str(REPO_ROOT / ".env"))
        self.assertEqual(rendered["artifacts"][0]["name"], "smbd")
        started = self.telemetry_payload("paths_started")
        finished = self.telemetry_payload("paths_finished")
        self.assertTrue(started["json_output"])
        self.assertEqual(finished["result"], "success")
        self.assertEqual(finished["artifact_count"], 1)
        self.assertEqual(finished["missing_artifact_count"], 0)

    def test_paths_config_arg_is_passed_to_path_resolution(self) -> None:
        app_paths = AppPaths(
            distribution_root=REPO_ROOT,
            config_path=REPO_ROOT / "custom.env",
            state_dir=REPO_ROOT,
            package_root=SRC_ROOT / "timecapsulesmb",
        )
        data = {
            "distribution_root": str(app_paths.distribution_root),
            "config_path": str(app_paths.config_path),
            "state_dir": str(app_paths.state_dir),
            "package_root": str(app_paths.package_root),
            "artifact_manifest": "manifest.json",
            "artifacts": [],
        }
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / "custom.env"
            with mock.patch("timecapsulesmb.cli.paths.ensure_install_id"):
                with mock.patch("timecapsulesmb.cli.paths.load_optional_env_config", return_value=self.make_app_config({}, exists=False)) as load_mock:
                    with mock.patch("timecapsulesmb.cli.paths.resolve_app_paths", return_value=app_paths) as resolve_mock:
                        with mock.patch("timecapsulesmb.cli.paths.paths_to_jsonable", return_value=data):
                            with redirect_stdout(io.StringIO()):
                                rc = paths.main(["--json", "--config", str(env_path)])

        self.assertEqual(rc, 0)
        load_mock.assert_called_once_with(env_path=env_path)
        resolve_mock.assert_called_once_with(config_path=env_path)

    def test_validate_install_json_command_returns_failure_when_check_fails(self) -> None:
        app_paths = AppPaths(
            distribution_root=REPO_ROOT,
            config_path=REPO_ROOT / ".env",
            state_dir=REPO_ROOT,
            package_root=SRC_ROOT / "timecapsulesmb",
        )
        checks = [
            InstallCheckResult("python_modules", True, "required Python modules import"),
            InstallCheckResult("artifact_hashes", False, "artifact validation failed", {"failures": ["missing bin/smbd"]}),
        ]
        output = io.StringIO()
        with mock.patch("timecapsulesmb.cli.validate_install.ensure_install_id"):
            with mock.patch("timecapsulesmb.cli.validate_install.resolve_app_paths", return_value=app_paths):
                with mock.patch("timecapsulesmb.cli.validate_install.validate_install", return_value=checks):
                    with redirect_stdout(output):
                        rc = validate_install.main(["--json"])

        self.assertEqual(rc, 1)
        rendered = json.loads(output.getvalue())
        self.assertFalse(rendered["ok"])
        self.assertEqual(rendered["checks"][1]["id"], "artifact_hashes")
        self.assertEqual(rendered["checks"][1]["details"]["failures"], ["missing bin/smbd"])
        finished = self.telemetry_payload("validate_install_finished")
        self.assertEqual(finished["result"], "failure")
        self.assertEqual(finished["install_ok"], False)
        self.assertEqual(finished["failed_check_ids"], ["artifact_hashes"])
        self.assertIn("install validation failed", finished["error"])

    def test_validate_install_config_arg_is_passed_to_path_resolution(self) -> None:
        app_paths = AppPaths(
            distribution_root=REPO_ROOT,
            config_path=REPO_ROOT / "custom.env",
            state_dir=REPO_ROOT,
            package_root=SRC_ROOT / "timecapsulesmb",
        )
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / "custom.env"
            with mock.patch("timecapsulesmb.cli.validate_install.ensure_install_id"):
                with mock.patch("timecapsulesmb.cli.validate_install.load_optional_env_config", return_value=self.make_app_config({}, exists=False)) as load_mock:
                    with mock.patch("timecapsulesmb.cli.validate_install.resolve_app_paths", return_value=app_paths) as resolve_mock:
                        with mock.patch("timecapsulesmb.cli.validate_install.validate_install", return_value=[]):
                            with redirect_stdout(io.StringIO()):
                                rc = validate_install.main(["--json", "--config", str(env_path)])

        self.assertEqual(rc, 0)
        load_mock.assert_called_once_with(env_path=env_path)
        resolve_mock.assert_called_once_with(config_path=env_path)

    def test_validate_install_text_command_prints_summary(self) -> None:
        checks = [InstallCheckResult("boot_script_tokens", True, "managed boot scripts have no unresolved tokens")]
        output = io.StringIO()
        with mock.patch("timecapsulesmb.cli.validate_install.ensure_install_id"):
            with mock.patch("timecapsulesmb.cli.validate_install.resolve_app_paths"):
                with mock.patch("timecapsulesmb.cli.validate_install.validate_install", return_value=checks):
                    with redirect_stdout(output):
                        rc = validate_install.main([])

        self.assertEqual(rc, 0)
        self.assertIn("PASS managed boot scripts have no unresolved tokens", output.getvalue())
        self.assertIn("Summary: install validation passed.", output.getvalue())

    def test_config_arg_is_passed_to_shared_config_loaders(self) -> None:
        commands = [
            ("activate", activate, "load_env_config", None),
            ("doctor", doctor, "load_env_config", None),
            ("uninstall", uninstall, "load_env_config", None),
            ("fsck", fsck, "load_env_config", None),
            ("set_ssh", set_ssh, "load_env_config", {"defaults": {}}),
            ("discover", discover, "load_optional_env_config", None),
            ("paths", paths, "load_optional_env_config", None),
            ("validate_install", validate_install, "load_optional_env_config", None),
            ("repair_xattrs", repair_xattrs, "load_optional_env_config", None),
        ]
        sentinel = RuntimeError("stop after config load")
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / "shared.env"
            for _name, command_module, loader_name, extra_kwargs in commands:
                with self.subTest(command=command_module.__name__):
                    patches = [
                        mock.patch(f"{command_module.__name__}.{loader_name}", side_effect=sentinel),
                    ]
                    if hasattr(command_module, "ensure_install_id"):
                        patches.append(mock.patch(f"{command_module.__name__}.ensure_install_id"))
                    if command_module is repair_xattrs:
                        patches.append(mock.patch("sys.platform", "darwin"))
                    with ExitStack() as stack:
                        load_mock = stack.enter_context(patches[0])
                        for patcher in patches[1:]:
                            stack.enter_context(patcher)
                        with self.assertRaises(RuntimeError):
                            with redirect_stdout(io.StringIO()):
                                command_module.main(["--config", str(env_path)])
                    expected_kwargs = {"env_path": env_path}
                    if extra_kwargs is not None:
                        expected_kwargs.update(extra_kwargs)
                    load_mock.assert_called_once_with(**expected_kwargs)

    def test_repair_xattrs_non_macos_emits_platform_check_telemetry(self) -> None:
        with mock.patch("timecapsulesmb.cli.repair_xattrs.ensure_install_id"):
            with mock.patch(
                "timecapsulesmb.cli.repair_xattrs.load_optional_env_config",
                return_value=self.make_app_config({}, exists=False),
            ):
                with mock.patch("sys.platform", "linux"):
                    with self.assertRaises(SystemExit):
                        repair_xattrs.main(["--path", "/Volumes/Home"])

        finished = self.telemetry_payload("repair_xattrs_finished")
        self.assertEqual(finished["result"], "failure")
        self.assertEqual(finished["host_platform"], "linux")
        self.assertIn("stage=platform_check", finished["error"])

    def test_repair_xattrs_json_emits_ndjson_result(self) -> None:
        output = io.StringIO()
        result = repair_xattrs_service.RepairRunResult(
            returncode=0,
            root=Path("/Volumes/Data"),
            findings=[mock.Mock()],
            candidates=[mock.Mock()],
            summary=repair_xattrs_domain.RepairSummary(scanned=1, repairable=1),
            report="detected issues",
        )
        with mock.patch("timecapsulesmb.cli.repair_xattrs.sys.platform", "darwin"):
            with mock.patch("timecapsulesmb.cli.repair_xattrs.load_optional_env_config", return_value=AppConfig.missing()):
                with mock.patch("timecapsulesmb.cli.repair_xattrs.run_repair_service", return_value=result):
                    with redirect_stdout(output):
                        rc = repair_xattrs.main(["--path", "/Volumes/Data", "--dry-run", "--json"])

        self.assertEqual(rc, 0)
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(events[0]["type"], "stage")
        self.assertEqual(events[-1]["type"], "result")
        self.assertEqual(events[-1]["payload"]["finding_count"], 1)
        self.assertEqual(events[-1]["payload"]["summary"], "Found 1 metadata issue, 1 repairable.")
        self.assertEqual(events[-1]["payload"]["summary_text"], "Found 1 metadata issue, 1 repairable.")
        self.assertEqual(events[-1]["payload"]["stats"]["scanned"], 1)
        self.assertEqual(events[-1]["payload"]["repairable_count"], 1)

    def test_repair_xattrs_json_repair_requires_yes(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as raised:
                repair_xattrs.main(["--path", "/Volumes/Data", "--json"])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("--json repair requires --yes", stderr.getvalue())

    def test_discover_json_outputs_records(self) -> None:
        output = io.StringIO()
        record = BonjourResolvedService(
            name="Time Capsule",
            hostname="capsule.local",
            ipv4=["10.0.0.2"],
            ipv6=[],
            services={"_airport._tcp.local."},
            properties={"model": "AirPort Time Capsule"},
        )
        snapshot = BonjourDiscoverySnapshot(
            instances=[
                BonjourServiceInstance("_airport._tcp.local.", "Time Capsule", "Time Capsule._airport._tcp.local."),
            ],
            resolved=[record],
        )
        diagnostics = BonjourMergedDiscoveryDiagnostics(
            service=None,
            service_types=[],
            timeout_sec=6.0,
            elapsed_sec=0.0,
            instance_count=len(snapshot.instances),
            resolved_count=len(snapshot.resolved),
        )
        with mock.patch("timecapsulesmb.cli.discover.ensure_install_id"):
            with mock.patch("timecapsulesmb.cli.discover.discover_snapshot_merged_detailed", return_value=(snapshot, diagnostics)):
                with redirect_stdout(output):
                    rc = discover.main(["--json"])
        self.assertEqual(rc, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["instances"][0]["name"], "Time Capsule")
        self.assertEqual(payload["resolved"][0]["name"], "Time Capsule")
        started = self.telemetry_payload("discover_started")
        finished = self.telemetry_payload("discover_finished")
        self.assertTrue(started["json_output"])
        self.assertEqual(finished["result"], "success")
        self.assertEqual(finished["bonjour_instance_count"], 1)
        self.assertEqual(finished["bonjour_resolved_count"], 1)


if __name__ == "__main__":
    unittest.main()
