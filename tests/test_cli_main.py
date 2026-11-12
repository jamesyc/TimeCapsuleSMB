"""CLI entry point, shared config loading, paths, validate-install, discover and target resolution."""
from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock
import timecapsulesmb.cli.main as cli_main_module
from timecapsulesmb.cli import (
    activate,
    discover,
    doctor,
    fsck,
    paths,
    set_ssh,
    uninstall,
    validate_install,
)
from timecapsulesmb.cli.main import main
from timecapsulesmb.core.paths import AppPaths
from timecapsulesmb.core.release import CLI_VERSION
from timecapsulesmb.discovery.bonjour import (
    BonjourDiscoverySnapshot,
    BonjourQueryDiagnostics,
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
    def test_retired_repair_xattrs_command_is_rejected(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as raised:
                main(["repair-xattrs", "--path", "/Volumes/Data"])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("invalid choice: 'repair-xattrs'", stderr.getvalue())
        self._version_check.assert_not_called()

    def test_dispatches_to_command_handler(self) -> None:
        with mock.patch("timecapsulesmb.cli.main.COMMANDS", {"doctor": mock.Mock(return_value=7)}):
            rc = main(["doctor", "--skip-smb"])
        self.assertEqual(rc, 7)
        self._version_check.assert_called_once_with(appcast=True)

    def test_main_blocks_outdated_client_before_dispatch(self) -> None:
        stderr = io.StringIO()
        command = mock.Mock(return_value=7)
        self._version_check.return_value = VersionCheckResult(
            should_block=True,
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
        self.assertNotIn("is available", output)

    def test_main_names_a_newer_release_and_still_runs_the_command(self) -> None:
        stdout, stderr = io.StringIO(), io.StringIO()
        command = mock.Mock(return_value=7)
        self._version_check.return_value = VersionCheckResult(
            should_block=False,
            local_version_code=30800,
            available_version_code=30900,
            available_version="3.9.0",
            source="network",
        )

        with mock.patch("timecapsulesmb.cli.main.COMMANDS", {"doctor": command}):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                rc = main(["doctor", "--skip-smb"])

        self.assertEqual(rc, 7)
        command.assert_called_once_with(["--skip-smb"])
        self.assertEqual(stderr.getvalue(), f"TimeCapsuleSMB v3.9.0 is available: {DEFAULT_DOWNLOAD_URL}\n")
        self.assertEqual(stdout.getvalue(), "")

    def test_main_tells_a_homebrew_install_to_upgrade_with_brew(self) -> None:
        stderr = io.StringIO()
        command = mock.Mock(return_value=0)
        self._version_check.return_value = VersionCheckResult(
            should_block=False,
            local_version_code=30800,
            available_version_code=30900,
            available_version="3.9.0",
            source="network",
        )

        with mock.patch.dict("os.environ", {"TCAPSULE_CLIENT": "homebrew"}):
            with mock.patch("timecapsulesmb.cli.main.COMMANDS", {"doctor": command}):
                with redirect_stderr(stderr):
                    rc = main(["doctor"])

        self.assertEqual(rc, 0)
        self.assertEqual(stderr.getvalue(), "TimeCapsuleSMB v3.9.0 is available: run `brew update && brew upgrade tcapsule`\n")

    def test_version_flag_prints_the_cli_version_without_a_command(self) -> None:
        stdout = io.StringIO()
        command = mock.Mock(return_value=0)
        with mock.patch("timecapsulesmb.cli.main.COMMANDS", {"doctor": command}):
            with redirect_stdout(stdout):
                with self.assertRaises(SystemExit) as raised:
                    main(["--version"])

        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(stdout.getvalue(), f"tcapsule {CLI_VERSION}\n")
        command.assert_not_called()
        self._version_check.assert_not_called()

    def test_main_names_no_release_when_current_or_unknown(self) -> None:
        cases = {
            "same_release": (30800, "3.8.0"),
            "older_release": (30700, "3.7.0"),
            "unknown": (None, None),
        }
        for name, (code, version) in cases.items():
            with self.subTest(name=name):
                stderr = io.StringIO()
                command = mock.Mock(return_value=0)
                self._version_check.return_value = VersionCheckResult(
                    should_block=False,
                    local_version_code=30800,
                    available_version_code=code,
                    available_version=version,
                    source="network",
                )

                with mock.patch("timecapsulesmb.cli.main.COMMANDS", {"doctor": command}):
                    with redirect_stderr(stderr):
                        rc = main(["doctor"])

                self.assertEqual(rc, 0)
                self.assertEqual(stderr.getvalue(), "")

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

    def run_main_with_recorded_keep_awake(self, argv: list[str]) -> list[str]:
        timeline: list[str] = []

        @contextmanager
        def recording_keep_awake():
            timeline.append("awake")
            try:
                yield
            finally:
                timeline.append("released")

        def command(_argv):
            timeline.append("command")
            return 0

        with mock.patch("timecapsulesmb.cli.main.COMMANDS", {argv[0]: command}):
            with mock.patch("timecapsulesmb.cli.main.keep_system_awake", recording_keep_awake):
                self.assertEqual(main(argv), 0)
        return timeline

    def test_long_device_command_keeps_the_mac_awake_while_it_runs(self) -> None:
        self.assertEqual(self.run_main_with_recorded_keep_awake(["deploy", "--yes"]), ["awake", "command", "released"])

    def test_quick_command_does_not_keep_the_mac_awake(self) -> None:
        self.assertEqual(self.run_main_with_recorded_keep_awake(["discover"]), ["command"])

    def test_api_command_leaves_keeping_awake_to_each_operation(self) -> None:
        self.assertEqual(self.run_main_with_recorded_keep_awake(["api"]), ["command"])

    def test_cancelled_command_releases_keep_awake_before_reporting(self) -> None:
        timeline: list[str] = []

        @contextmanager
        def recording_keep_awake():
            timeline.append("awake")
            try:
                yield
            finally:
                timeline.append("released")

        stderr = io.StringIO()
        with mock.patch("timecapsulesmb.cli.main.COMMANDS", {"deploy": mock.Mock(side_effect=KeyboardInterrupt)}):
            with mock.patch("timecapsulesmb.cli.main.keep_system_awake", recording_keep_awake):
                with redirect_stderr(stderr):
                    rc = main(["deploy"])

        self.assertEqual(rc, 130)
        self.assertEqual(timeline, ["awake", "released"])
        self.assertEqual(stderr.getvalue(), "\nCancelled.\n")

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

    def test_discover_select_asks_again_after_input_the_terminal_encoding_cannot_decode(self) -> None:
        record = BonjourResolvedService(
            name="Time Capsule",
            hostname="capsule.local",
            ipv4=["10.0.0.2"],
            ipv6=[],
            services={"_airport._tcp.local."},
            properties={"model": "AirPort Time Capsule"},
        )
        snapshot = BonjourDiscoverySnapshot(
            instances=[BonjourServiceInstance("_airport._tcp.local.", "Time Capsule", "Time Capsule._airport._tcp.local.")],
            resolved=[record],
        )
        output = io.StringIO()
        bad = UnicodeDecodeError("utf-8", b"\xd0a", 0, 1, "invalid continuation byte")
        with mock.patch("timecapsulesmb.cli.discover.discover_snapshot_detailed", return_value=(snapshot, None)):
            with mock.patch("builtins.input", side_effect=[bad, "1"]) as input_mock:
                with redirect_stdout(output):
                    rc = discover.run_cli(["--select"])
        self.assertEqual(rc, 0)
        self.assertEqual(input_mock.call_count, 2)
        self.assertIn("could not be read as", output.getvalue())
        self.assertEqual(output.getvalue().splitlines()[-1], record.display_host())

    def test_discover_select_at_end_of_input_cancels_without_a_traceback(self) -> None:
        record = BonjourResolvedService(
            name="Time Capsule",
            hostname="capsule.local",
            ipv4=["10.0.0.2"],
            ipv6=[],
            services={"_airport._tcp.local."},
            properties={"model": "AirPort Time Capsule"},
        )
        snapshot = BonjourDiscoverySnapshot(
            instances=[BonjourServiceInstance("_airport._tcp.local.", "Time Capsule", "Time Capsule._airport._tcp.local.")],
            resolved=[record],
        )
        diagnostics = BonjourQueryDiagnostics(
            provider="zeroconf",
            service_types=[],
            timeout_sec=6.0,
            elapsed_sec=0.0,
            instance_count=1,
            resolved_count=1,
        )
        output = io.StringIO()
        with mock.patch("timecapsulesmb.cli.discover.ensure_install_id"):
            with mock.patch("timecapsulesmb.cli.discover.discover_snapshot_detailed", return_value=(snapshot, diagnostics)):
                with mock.patch("builtins.input", side_effect=EOFError) as input_mock:
                    with redirect_stdout(output):
                        rc = discover.main(["--select"])
        self.assertEqual(rc, 1)
        input_mock.assert_called_once()
        finished = self.telemetry_payload("discover_finished")
        self.assertEqual(finished["result"], "cancelled")
        self.assertIn("Cancelled during discovery selection.", finished["error"])

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
        diagnostics = BonjourQueryDiagnostics(
            provider="zeroconf",
            service_types=[],
            timeout_sec=6.0,
            elapsed_sec=0.0,
            instance_count=len(snapshot.instances),
            resolved_count=len(snapshot.resolved),
        )
        with mock.patch("timecapsulesmb.cli.discover.ensure_install_id"):
            with mock.patch("timecapsulesmb.cli.discover.discover_snapshot_detailed", return_value=(snapshot, diagnostics)):
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


class SilentDeviceCommandTests(CliTestCase):
    """Every command that talks to the device, against one that answers
    nothing at its saved address: each ends with where this computer is
    against the router configure saved (checks.network.silent_device), never
    a generic or raw SSH failure. The app operations have the same table
    (tests/test_app_api.py)."""

    COMMANDS = (
        ["deploy", "--yes"], ["doctor"], ["uninstall", "--yes"], ["fsck", "--yes"], ["wipe-internal-disk", "--yes"],
        ["flash", "--read-only"], ["set-ssh"], ["activate", "--yes"],
    )

    def run_against_silent_device(self, argv: list[str], router_now: str, silence: str, *, held: bool = False) -> str:
        import ipaddress

        from timecapsulesmb.checks.network import LocalInterfaceNetwork, NetworkPlace
        from timecapsulesmb.device.probe import AirportAcpReading, SshCommandProbeResult
        from timecapsulesmb.transport.errors import SshNetworkError
        from timecapsulesmb.transport.local import Router
        from tests.test_app_api import SshAccessStatus, failed_probe_state

        networks = (LocalInterfaceNetwork("en0", "10.0.0.50", ipaddress.ip_network("10.0.0.0/24")),)
        place = NetworkPlace(networks, (Router("en0", "10.0.0.1", router_now),))
        timed_out = SshNetworkError(f"ssh: connect to host 10.0.0.2 port 22: {silence}")
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / ".env"
            config.write_text("TC_HOST=root@10.0.0.2\nTC_PASSWORD=pw\nTC_LAN_ROUTER_MAC=aa:00:00:00:00:01\n")
            with mock.patch("timecapsulesmb.services.runtime.probe_managed_connection_state",
                            return_value=failed_probe_state(SshAccessStatus.CLOSED, "SSH is not reachable yet.")), \
                    mock.patch("timecapsulesmb.checks.network.tcp_connect_error", return_value=silence), \
                    mock.patch("timecapsulesmb.checks.network.probe_ssh_command_conn",
                               return_value=SshCommandProbeResult(ok=False, detail=str(timed_out), error=timed_out)), \
                    mock.patch("timecapsulesmb.services.acp_ssh.tcp_connect_error", return_value=silence), \
                    mock.patch("timecapsulesmb.services.acp_ssh.time.sleep"), \
                    mock.patch("timecapsulesmb.services.acp_ssh.local_lan_networks", return_value=networks), \
                    mock.patch("timecapsulesmb.services.runtime.ssh_port_error", return_value=silence), \
                    mock.patch("timecapsulesmb.services.set_ssh.tcp_connect_error", return_value=silence), \
                    mock.patch("timecapsulesmb.cli.set_ssh.tcp_open", return_value=False), \
                    mock.patch("timecapsulesmb.device.probe.read_airport_acp",
                               return_value=AirportAcpReading(password_matches=None, error=f"ACP receive failed: {silence}")), \
                    mock.patch("timecapsulesmb.checks.network.read_network_place", return_value=place), \
                    mock.patch("timecapsulesmb.checks.network.macos_may_hold_this_process", return_value=held), \
                    mock.patch("timecapsulesmb.discovery.bonjour.BonjourQuery.browse", return_value=(
                        BonjourDiscoverySnapshot([], []), BonjourQueryDiagnostics("zeroconf", [], 0, 0, 0, 0),
                    )), \
                    mock.patch("sys.platform", "darwin" if held else sys.platform), \
                    redirect_stdout(output), redirect_stderr(output):
                try:
                    rc = main([*argv, "--config", str(config)])
                except SystemExit as exc:
                    rc = 1
                    output.write(str(exc.code))
        self.assertNotEqual(rc, 0)
        return output.getvalue()

    def test_every_command_judges_a_silent_device_by_the_saved_router(self) -> None:
        # Silence is a timeout, the "Host is down" macOS gives for a device on
        # its network that is off, or no route, which Linux gives for one gone
        # from its link and nothing holds a process there.
        for silence in ("timed out", "[Errno 64] Host is down", "[Errno 113] No route to host"):
            for argv in self.COMMANDS:
                self.assert_judged(argv, silence)

    def test_a_terminal_app_macos_may_hold_is_not_told_the_device_is_silent(self) -> None:
        # iTerm2 or Visual Studio Code without Local Network access: every
        # connect gets no route and a .local name does not resolve, while the
        # device is up, and Bonjour sees nothing. No unplug as the verdict: the
        # generic card, or Checkup's Bonjour line, names the terminal app.
        for silence in ("[Errno 65] No route to host", "[Errno 8] nodename nor servname provided, or not known"):
            for argv in self.COMMANDS:
                with self.subTest(command=argv[0], silence=silence):
                    text = self.run_against_silent_device(argv, "aa:00:00:00:00:01", silence, held=True)
                    self.assertNotIn("same network as when it last reached the device", text)
                    self.assertIn("terminal app other than Terminal", text)

    def assert_judged(self, argv: list[str], silence: str) -> None:
        with self.subTest(command=argv[0], silence=silence, router="same"):
            text = self.run_against_silent_device(argv, "aa:00:00:00:00:01", silence)
            self.assertIn("is on the same network as when it last reached the device", text)
        with self.subTest(command=argv[0], silence=silence, router="another"):
            text = self.run_against_silent_device(argv, "aa:00:00:00:00:02", silence)
            self.assertIn("is on a different network than when it last reached the device", text)
            self.assertNotIn("is on the same network as when it last reached the device", text)


if __name__ == "__main__":
    unittest.main()
