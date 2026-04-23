from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
import uuid
from contextlib import ExitStack
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from timecapsulesmb.cli import activate, bootstrap, configure, deploy, discover, doctor, fsck, prep_device, uninstall
from timecapsulesmb.cli.main import main
from timecapsulesmb.cli.context import CommandContext
from timecapsulesmb.core.config import DEFAULTS
from timecapsulesmb.device.compat import DeviceCompatibility, compatibility_from_probe_result
from timecapsulesmb.device.probe import MountedVolume, ProbeResult, ProbedDeviceState, RemoteInterfaceProbeResult
from timecapsulesmb.transport.ssh import SshConnection
from timecapsulesmb.discovery.bonjour import Discovered


class FakeCommandContext:
    def __init__(
        self,
        *,
        connection: SshConnection | None = None,
        compatibility: DeviceCompatibility | None = None,
    ) -> None:
        self.result = "failure"
        self.finish_fields: dict[str, object] = {}
        self.error_lines: list[str] = []
        self.debug_context_added = False
        self.finish = mock.Mock()
        self.connection = connection or SshConnection("root@10.0.0.2", "pw", "-o foo")
        self.probe_state = None
        self.compatibility = compatibility or DeviceCompatibility(
            os_name="NetBSD",
            os_release="6.0",
            arch="earmv4",
            elf_endianness="little",
            payload_family="netbsd6_samba4",
            device_generation="gen5",
            supported=True,
            reason_code="supported_netbsd6",
        )

    def __enter__(self) -> "FakeCommandContext":
        return self

    def __exit__(self, exc_type, _exc, _tb) -> bool:
        if exc_type is KeyboardInterrupt and self.result != "cancelled":
            self.result = "cancelled"
            if not self.error_lines:
                self.set_error("Cancelled by user")
        self.finish(result=self.result, error=None if self.result == "success" else "\n".join(self.error_lines) if self.error_lines else None, **self.finish_fields)
        return False

    def set_result(self, result: str) -> None:
        self.result = result

    def succeed(self) -> None:
        self.result = "success"

    def cancel(self) -> None:
        self.result = "cancelled"

    def cancel_with_error(self, message: str = "Cancelled by user") -> None:
        self.result = "cancelled"
        self.set_error(message)

    def fail(self) -> None:
        self.result = "failure"

    def fail_with_error(self, message: str) -> None:
        self.result = "failure"
        self.set_error(message)

    def update_fields(self, **fields: object) -> None:
        for key, value in fields.items():
            if value is not None:
                self.finish_fields[key] = value

    def set_error(self, message: str) -> None:
        self.error_lines = [line.rstrip() for line in message.splitlines() if line.strip()]

    def add_error_line(self, message: str) -> None:
        line = message.strip()
        if line:
            self.error_lines.append(line)

    def add_debug_context(self, *, extra_fields: dict[str, object] | None = None) -> None:
        self.debug_context_added = True
        if self.error_lines:
            self.error_lines.append("")
        self.error_lines.append("Debug context:")
        self.error_lines.append("command=fake")
        self.error_lines.append(f"host={self.connection.host}")
        self.error_lines.append(f"ssh_opts={self.connection.ssh_opts}")
        if extra_fields:
            for key, value in extra_fields.items():
                if value is not None:
                    self.error_lines.append(f"{key}={value}")

    def resolve_env_connection(self, **_kwargs):
        return self.connection

    def resolve_validated_managed_connection(self, **_kwargs):
        return self.connection

    def resolve_validated_managed_target(self, **_kwargs):
        return mock.Mock(connection=self.connection, probe_state=None)

    def require_compatibility(self):
        return self.compatibility


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._exit_stack = ExitStack()
        self._telemetry_client = mock.Mock()
        for target in (
            "timecapsulesmb.cli.configure.TelemetryClient.from_values",
            "timecapsulesmb.cli.deploy.TelemetryClient.from_values",
            "timecapsulesmb.cli.activate.TelemetryClient.from_values",
            "timecapsulesmb.cli.doctor.TelemetryClient.from_values",
        ):
            self._exit_stack.enter_context(mock.patch(target, return_value=self._telemetry_client))
        self._exit_stack.enter_context(
            mock.patch(
                "timecapsulesmb.cli.runtime.probe_remote_interface",
                return_value=RemoteInterfaceProbeResult(iface="bridge0", exists=True, detail="interface bridge0 exists"),
            )
        )

    def tearDown(self) -> None:
        self._exit_stack.close()

    def make_supported_compatibility(self) -> DeviceCompatibility:
        return DeviceCompatibility(
            os_name="NetBSD",
            os_release="6.0",
            arch="earmv4",
            elf_endianness="little",
            payload_family="netbsd6_samba4",
            device_generation="gen5",
            supported=True,
            reason_code="supported_netbsd6",
        )

    def make_supported_netbsd4_compatibility(self) -> DeviceCompatibility:
        return DeviceCompatibility(
            os_name="NetBSD",
            os_release="4.0",
            arch="earmv4",
            elf_endianness="little",
            payload_family="netbsd4le_samba4",
            device_generation="gen1-4",
            supported=True,
            reason_code="supported_netbsd4",
        )

    def make_valid_env(self, **overrides: str) -> dict[str, str]:
        values = dict(DEFAULTS)
        values.update({
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
            "TC_AIRPORT_SYAP": "119",
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule8,119",
        })
        values.update(overrides)
        return values

    def make_probe_result_unreachable(self) -> ProbeResult:
        return ProbeResult(
            ssh_port_reachable=False,
            ssh_authenticated=False,
            error="SSH is not reachable yet.",
            os_name="",
            os_release="",
            arch="",
            elf_endianness="unknown",
        )

    def make_probe_result_auth_failed(self) -> ProbeResult:
        return ProbeResult(
            ssh_port_reachable=True,
            ssh_authenticated=False,
            error="SSH authentication failed.",
            os_name="",
            os_release="",
            arch="",
            elf_endianness="unknown",
        )

    def make_probe_result_netbsd6(self) -> ProbeResult:
        return ProbeResult(
            ssh_port_reachable=True,
            ssh_authenticated=True,
            error=None,
            os_name="NetBSD",
            os_release="6.0",
            arch="earmv4",
            elf_endianness="little",
        )

    def make_probe_result_netbsd6_unknown(self) -> ProbeResult:
        return ProbeResult(
            ssh_port_reachable=True,
            ssh_authenticated=True,
            error=None,
            os_name="NetBSD",
            os_release="6.0",
            arch="earmv4",
            elf_endianness="unknown",
        )

    def make_probe_result_netbsd6_big(self) -> ProbeResult:
        return ProbeResult(
            ssh_port_reachable=True,
            ssh_authenticated=True,
            error=None,
            os_name="NetBSD",
            os_release="6.0",
            arch="earmv4",
            elf_endianness="big",
        )

    def make_probe_result_netbsd4le(self) -> ProbeResult:
        return ProbeResult(
            ssh_port_reachable=True,
            ssh_authenticated=True,
            error=None,
            os_name="NetBSD",
            os_release="4.0",
            arch="earmv4",
            elf_endianness="little",
        )

    def make_probe_result_netbsd4be(self) -> ProbeResult:
        return ProbeResult(
            ssh_port_reachable=True,
            ssh_authenticated=True,
            error=None,
            os_name="NetBSD",
            os_release="4.0",
            arch="earmv4",
            elf_endianness="big",
        )

    def make_probe_result_netbsd4_unknown(self) -> ProbeResult:
        return ProbeResult(
            ssh_port_reachable=True,
            ssh_authenticated=True,
            error=None,
            os_name="NetBSD",
            os_release="4.0_STABLE",
            arch="evbarm",
            elf_endianness="unknown",
        )

    def make_probe_result_netbsd5(self) -> ProbeResult:
        return ProbeResult(
            ssh_port_reachable=True,
            ssh_authenticated=True,
            error=None,
            os_name="NetBSD",
            os_release="5.0",
            arch="earmv4",
            elf_endianness="little",
        )

    def make_probe_state(self, probe_result: ProbeResult) -> ProbedDeviceState:
        compatibility = compatibility_from_probe_result(probe_result) if probe_result.ssh_authenticated else None
        return ProbedDeviceState(probe_result=probe_result, compatibility=compatibility)

    def test_dispatches_to_command_handler(self) -> None:
        with mock.patch("timecapsulesmb.cli.main.COMMANDS", {"doctor": mock.Mock(return_value=7)}):
            rc = main(["doctor", "--skip-smb"])
        self.assertEqual(rc, 7)

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

    def test_activate_command_is_registered(self) -> None:
        with mock.patch("timecapsulesmb.cli.main.COMMANDS", {"activate": mock.Mock(return_value=0)}) as commands:
            rc = main(["activate", "--dry-run"])
        self.assertEqual(rc, 0)
        commands["activate"].assert_called_once_with(["--dry-run"])

    def test_fsck_command_is_registered(self) -> None:
        with mock.patch("timecapsulesmb.cli.main.COMMANDS", {"fsck": mock.Mock(return_value=0)}) as commands:
            rc = main(["fsck", "--yes", "--no-reboot"])
        self.assertEqual(rc, 0)
        commands["fsck"].assert_called_once_with(["--yes", "--no-reboot"])

    def test_bootstrap_prints_full_next_steps(self) -> None:
        output = io.StringIO()
        with mock.patch("pathlib.Path.exists", return_value=True):
            with mock.patch("timecapsulesmb.cli.bootstrap.ensure_venv", return_value=bootstrap.VENVDIR / "bin" / "python"):
                with mock.patch("timecapsulesmb.cli.bootstrap.install_python_requirements"):
                    with mock.patch("timecapsulesmb.cli.bootstrap.maybe_install_smbclient"):
                        with mock.patch("timecapsulesmb.cli.bootstrap.maybe_install_airpyrt", return_value=True):
                            with mock.patch("timecapsulesmb.cli.bootstrap.ensure_install_id"):
                                with mock.patch("timecapsulesmb.cli.bootstrap.current_platform_label", return_value="macOS"):
                                    with redirect_stdout(output):
                                        rc = bootstrap.main(["--skip-airpyrt"])
        self.assertEqual(rc, 0)
        text = output.getvalue()
        self.assertIn("Detected host platform", text)
        self.assertIn("prep-device", text)
        self.assertIn("configure", text)
        self.assertIn("deploy", text)
        self.assertIn("doctor", text)

    def test_bootstrap_prints_linux_next_steps_without_prep_device_when_airpyrt_unavailable(self) -> None:
        output = io.StringIO()
        with mock.patch("timecapsulesmb.cli.bootstrap.current_platform_label", return_value="Linux"):
            with mock.patch("pathlib.Path.exists", return_value=True):
                with mock.patch("timecapsulesmb.cli.bootstrap.ensure_venv", return_value=bootstrap.VENVDIR / "bin" / "python"):
                    with mock.patch("timecapsulesmb.cli.bootstrap.install_python_requirements"):
                        with mock.patch("timecapsulesmb.cli.bootstrap.maybe_install_smbclient"):
                            with mock.patch("timecapsulesmb.cli.bootstrap.maybe_install_airpyrt", return_value=False):
                                with mock.patch("timecapsulesmb.cli.bootstrap.ensure_install_id"):
                                    with redirect_stdout(output):
                                        rc = bootstrap.main(["--skip-airpyrt"])
        self.assertEqual(rc, 0)
        text = output.getvalue()
        self.assertIn("Detected host platform: Linux", text)
        self.assertNotIn("  2. /Users", text)
        self.assertIn("  2. If SSH is already enabled on the Time Capsule, continue to deploy. ", text)
        self.assertIn("\033[31mOtherwise enable SSH manually with `prep-device` from a Mac.\033[0m", text)
        self.assertIn("  3. ", text)
        self.assertIn("  4. ", text)

    def test_bootstrap_explains_long_running_airpyrt_step(self) -> None:
        output = io.StringIO()
        with mock.patch("pathlib.Path.exists", return_value=True):
            with mock.patch("timecapsulesmb.cli.bootstrap.ensure_venv", return_value=bootstrap.VENVDIR / "bin" / "python"):
                with mock.patch("timecapsulesmb.cli.bootstrap.install_python_requirements"):
                    with mock.patch("timecapsulesmb.cli.bootstrap.maybe_install_smbclient"):
                        with mock.patch("timecapsulesmb.cli.bootstrap.run") as run_mock:
                            with mock.patch("timecapsulesmb.cli.bootstrap.shutil.which", return_value="/usr/bin/make"):
                                with mock.patch("timecapsulesmb.cli.bootstrap.confirm", return_value=True):
                                    with mock.patch("timecapsulesmb.cli.bootstrap.ensure_install_id"):
                                        with mock.patch("timecapsulesmb.cli.bootstrap.current_platform_label", return_value="macOS"):
                                            with redirect_stdout(output):
                                                rc = bootstrap.main([])
        self.assertEqual(rc, 0)
        text = output.getvalue()
        self.assertIn("Provisioning AirPyrt via 'make airpyrt'", text)
        self.assertIn("may take several minutes", text)
        self.assertIn("--skip-airpyrt", text)
        run_mock.assert_called_once_with(["/usr/bin/make", "airpyrt"], cwd=bootstrap.REPO_ROOT)

    def test_bootstrap_can_skip_optional_airpyrt_after_prompt(self) -> None:
        output = io.StringIO()
        with mock.patch("pathlib.Path.exists", return_value=True):
            with mock.patch("timecapsulesmb.cli.bootstrap.ensure_venv", return_value=bootstrap.VENVDIR / "bin" / "python"):
                with mock.patch("timecapsulesmb.cli.bootstrap.install_python_requirements"):
                    with mock.patch("timecapsulesmb.cli.bootstrap.maybe_install_smbclient"):
                        with mock.patch("timecapsulesmb.cli.bootstrap.run") as run_mock:
                            with mock.patch("timecapsulesmb.cli.bootstrap.shutil.which", return_value="/usr/bin/make"):
                                with mock.patch("timecapsulesmb.cli.bootstrap.confirm", return_value=False):
                                    with mock.patch("timecapsulesmb.cli.bootstrap.ensure_install_id"):
                                        with mock.patch("timecapsulesmb.cli.bootstrap.current_platform_label", return_value="macOS"):
                                            with redirect_stdout(output):
                                                rc = bootstrap.main([])
        self.assertEqual(rc, 0)
        text = output.getvalue()
        self.assertIn("AirPyrt support is optional", text)
        self.assertIn("Skipping AirPyrt setup", text)
        run_mock.assert_not_called()

    def test_bootstrap_returns_error_when_requirements_missing(self) -> None:
        stderr = io.StringIO()
        with mock.patch("pathlib.Path.exists", return_value=False):
            with redirect_stderr(stderr):
                rc = bootstrap.main([])
        self.assertEqual(rc, 1)
        self.assertIn("Missing", stderr.getvalue())

    def test_bootstrap_continues_when_airpyrt_setup_fails(self) -> None:
        output = io.StringIO()
        with mock.patch("pathlib.Path.exists", return_value=True):
            with mock.patch("timecapsulesmb.cli.bootstrap.ensure_venv", return_value=bootstrap.VENVDIR / "bin" / "python"):
                with mock.patch("timecapsulesmb.cli.bootstrap.install_python_requirements"):
                    with mock.patch("timecapsulesmb.cli.bootstrap.maybe_install_smbclient"):
                        with mock.patch(
                            "timecapsulesmb.cli.bootstrap.run",
                            side_effect=subprocess.CalledProcessError(2, ["make", "airpyrt"]),
                        ):
                            with mock.patch("timecapsulesmb.cli.bootstrap.shutil.which", return_value="/usr/bin/make"):
                                with mock.patch("timecapsulesmb.cli.bootstrap.confirm", return_value=True):
                                    with mock.patch("timecapsulesmb.cli.bootstrap.ensure_install_id"):
                                        with mock.patch("timecapsulesmb.cli.bootstrap.current_platform_label", return_value="macOS"):
                                            with redirect_stdout(output):
                                                rc = bootstrap.main([])
        self.assertEqual(rc, 0)
        text = output.getvalue()
        self.assertIn("Warning: AirPyrt setup failed", text)
        self.assertIn("Host setup complete.", text)

    def test_bootstrap_installs_smbclient_via_homebrew_on_macos(self) -> None:
        output = io.StringIO()
        with mock.patch("timecapsulesmb.cli.bootstrap.current_platform_label", return_value="macOS"):
            with mock.patch("timecapsulesmb.cli.bootstrap.shutil.which", side_effect=lambda name: None if name == "smbclient" else "/opt/homebrew/bin/brew"):
                with mock.patch("timecapsulesmb.cli.bootstrap.confirm", return_value=True):
                    with mock.patch(
                        "timecapsulesmb.cli.bootstrap.run",
                        side_effect=lambda cmd, cwd=None: None,
                    ) as run_mock:
                        with redirect_stdout(output):
                            bootstrap.maybe_install_smbclient()
        text = output.getvalue()
        self.assertIn("brew install samba", text)
        self.assertEqual(run_mock.call_args_list, [mock.call(["/opt/homebrew/bin/brew", "install", "samba"])])

    def test_bootstrap_prints_linux_smbclient_instructions_when_missing(self) -> None:
        output = io.StringIO()
        with mock.patch("timecapsulesmb.cli.bootstrap.current_platform_label", return_value="Linux"):
            def fake_which(name: str):
                if name == "smbclient":
                    return None
                if name == "apt-get":
                    return "/usr/bin/apt-get"
                return None
            with mock.patch("timecapsulesmb.cli.bootstrap.shutil.which", side_effect=fake_which):
                with redirect_stdout(output):
                    bootstrap.maybe_install_smbclient()
        text = output.getvalue()
        self.assertIn("smbclient is required", text)
        self.assertIn("sudo apt-get update && sudo apt-get install -y smbclient", text)
        self.assertIn("After installing smbclient", text)

    def test_bootstrap_prints_linux_airpyrt_guidance(self) -> None:
        output = io.StringIO()
        with mock.patch("timecapsulesmb.cli.bootstrap.current_platform_label", return_value="Linux"):
            with redirect_stdout(output):
                ready = bootstrap.maybe_install_airpyrt(skip_airpyrt=False)
        self.assertFalse(ready)
        text = output.getvalue()
        self.assertIn("Automatic AirPyrt setup is not implemented for Linux", text)
        self.assertIn("If SSH is already enabled", text)
        self.assertIn("use a Mac for 'prep-device'.", text)
        self.assertIn("\033[31m", text)

    def test_configure_writes_values_from_prompts(self) -> None:
        output = io.StringIO()
        fake_values = {}
        prompt_values = iter([
            "root@10.0.0.2",
            "pw",
            "bridge0",
            "Data",
            "admin",
            "TimeCapsule",
            "samba4",
            "Time Capsule Samba 4",
            "timecapsulesamba4",
            "119",
        ])

        def fake_write_env_file(path, values):
            fake_values.update(values)

        command_context = FakeCommandContext()
        with mock.patch("timecapsulesmb.cli.configure.parse_env_values", return_value={}):
            with mock.patch("timecapsulesmb.cli.configure.discover_time_capsule_candidates", return_value=[]):
                with mock.patch(
                    "timecapsulesmb.cli.configure.prompt",
                    side_effect=lambda _l, _d, _s: _d if _l == "mDNS device model hint" else next(prompt_values),
                ):
                        with mock.patch("timecapsulesmb.cli.configure.probe_device_state", return_value=self.make_probe_state(self.make_probe_result_unreachable())):
                            with mock.patch("timecapsulesmb.cli.configure.confirm", return_value=True):
                                with mock.patch("timecapsulesmb.cli.configure.write_env_file", side_effect=fake_write_env_file):
                                    with mock.patch("timecapsulesmb.cli.configure.TelemetryClient.from_values") as telemetry_factory:
                                        with mock.patch("timecapsulesmb.cli.configure.CommandContext", return_value=command_context) as command_context_factory:
                                            with redirect_stdout(output):
                                                rc = configure.main([])
        self.assertEqual(rc, 0)
        self.assertEqual(fake_values["TC_SAMBA_USER"], "admin")
        uuid.UUID(fake_values["TC_CONFIGURE_ID"])
        telemetry_values = telemetry_factory.call_args.args[0]
        self.assertEqual(telemetry_values["TC_CONFIGURE_ID"], fake_values["TC_CONFIGURE_ID"])
        self.assertEqual(command_context_factory.call_args.kwargs["configure_id"], fake_values["TC_CONFIGURE_ID"])
        command_context.finish.assert_called_once()
        self.assertEqual(command_context.finish.call_args.kwargs["configure_id"], fake_values["TC_CONFIGURE_ID"])
        self.assertEqual(command_context.finish.call_args.kwargs["device_syap"], fake_values["TC_AIRPORT_SYAP"])
        self.assertEqual(command_context.finish.call_args.kwargs["device_model"], fake_values["TC_MDNS_DEVICE_MODEL"])
        text = output.getvalue()
        self.assertIn("This writes a local .env configuration file", text)
        self.assertIn(f"Review the .env file configuration: wrote {configure.ENV_PATH}", text)
        self.assertIn("  - Prep your device to enable SSH on it:", text)
        self.assertIn("      Run .venv/bin/tcapsule prep-device", text)
        self.assertIn("  - Deploy this configuration to your Time Capsule:", text)
        self.assertIn("      Run .venv/bin/tcapsule deploy", text)

    def test_configure_ensures_install_id_before_telemetry(self) -> None:
        output = io.StringIO()
        prompt_values = iter([
            "root@10.0.0.2",
            "pw",
            "bridge0",
            "Data",
            "admin",
            "TimeCapsule",
            "samba4",
            "Time Capsule Samba 4",
            "timecapsulesamba4",
            "119",
        ])
        with mock.patch("timecapsulesmb.cli.configure.ensure_install_id") as ensure_mock:
            with mock.patch("timecapsulesmb.cli.configure.parse_env_values", return_value={}):
                with mock.patch("timecapsulesmb.cli.configure.discover_time_capsule_candidates", return_value=[]):
                    with mock.patch(
                        "timecapsulesmb.cli.configure.prompt",
                        side_effect=lambda label, default, _secret: default if label == "mDNS device model hint" else next(prompt_values),
                    ):
                        with mock.patch("timecapsulesmb.cli.configure.probe_device_state", return_value=self.make_probe_state(self.make_probe_result_unreachable())):
                            with mock.patch("timecapsulesmb.cli.configure.confirm", return_value=True):
                                with mock.patch("timecapsulesmb.cli.configure.write_env_file"):
                                    with mock.patch("timecapsulesmb.cli.configure.CommandContext", return_value=FakeCommandContext()):
                                        with redirect_stdout(output):
                                            rc = configure.main([])
        self.assertEqual(rc, 0)
        ensure_mock.assert_called_once_with()

    def test_configure_persists_configure_id_before_prompting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text("TC_HOST=root@10.0.0.2\n")
            with mock.patch("timecapsulesmb.cli.configure.ENV_PATH", env_path):
                with mock.patch("timecapsulesmb.cli.configure.parse_env_values", return_value={"TC_HOST": "root@10.0.0.2"}):
                    with mock.patch("timecapsulesmb.cli.configure.discover_time_capsule_candidates", return_value=[]):
                        with mock.patch("timecapsulesmb.cli.configure.prompt", side_effect=KeyboardInterrupt):
                            with mock.patch("timecapsulesmb.cli.configure.TelemetryClient.from_values"):
                                with self.assertRaises(KeyboardInterrupt):
                                    configure.main([])
            text = env_path.read_text()
            values = {}
            for line in text.splitlines():
                if "=" not in line or line.startswith("#"):
                    continue
                key, value = line.split("=", 1)
                values[key] = value
        self.assertIn("TC_HOST=root@10.0.0.2", text)
        self.assertIn("TC_CONFIGURE_ID=", text)
        self.assertEqual(text.count("TC_CONFIGURE_ID="), 1)

    def test_configure_uses_discovered_host_when_available(self) -> None:
        output = io.StringIO()
        fake_values = {}
        record = mock.Mock(name="Time Capsule", hostname="capsule.local", ipv4=["10.0.0.2"], ipv6=[])

        def fake_write_env_file(_path, values):
            fake_values.update(values)

        prompt_values = iter([
            "pw",
            "bridge0",
            "Data",
            "admin",
            "TimeCapsule",
            "samba4",
            "Time Capsule Samba 4",
            "timecapsulesamba4",
            "119",
        ])

        def fake_prompt(label, default, _secret):
            if label == "Time Capsule SSH target":
                return default
            if label == "mDNS device model hint":
                return default
            return next(prompt_values)

        with mock.patch("timecapsulesmb.cli.configure.parse_env_values", return_value={}):
            with mock.patch("timecapsulesmb.cli.configure.discover_time_capsule_candidates", return_value=[record]):
                with mock.patch("timecapsulesmb.cli.configure.discovered_record_root_host", return_value="root@10.0.0.2"):
                    with mock.patch("builtins.input", side_effect=["1"]):
                        with mock.patch("timecapsulesmb.cli.configure.prompt", side_effect=fake_prompt):
                            with mock.patch("timecapsulesmb.cli.configure.probe_device_state", return_value=self.make_probe_state(self.make_probe_result_unreachable())):
                                with mock.patch("timecapsulesmb.cli.configure.confirm", return_value=True):
                                    with mock.patch("timecapsulesmb.cli.configure.write_env_file", side_effect=fake_write_env_file):
                                        with redirect_stdout(output):
                                            rc = configure.main([])
        self.assertEqual(rc, 0)
        self.assertEqual(fake_values["TC_HOST"], "root@10.0.0.2")

    def test_configure_prefills_mdns_device_model_from_detected_device(self) -> None:
        output = io.StringIO()
        fake_values = {}
        prompt_values = iter([
            "rootpw",
            "bridge0",
            "Data",
            "admin",
            "TimeCapsule",
            "samba4",
            "Time Capsule Samba 4",
            "timecapsulesamba4",
            "119",
        ])
        seen_defaults = {}

        def fake_prompt(label, default, _secret):
            seen_defaults[label] = default
            if label == "Time Capsule SSH target":
                return "root@10.0.0.2"
            if label == "mDNS device model hint":
                return default
            return next(prompt_values)

        def fake_write_env_file(_path, values):
            fake_values.update(values)

        with mock.patch("timecapsulesmb.cli.configure.parse_env_values", return_value={}):
            with mock.patch("timecapsulesmb.cli.configure.discover_time_capsule_candidates", return_value=[]):
                with mock.patch("timecapsulesmb.cli.configure.prompt", side_effect=fake_prompt):
                    with mock.patch("timecapsulesmb.cli.configure.probe_device_state", return_value=self.make_probe_state(self.make_probe_result_netbsd6())):
                        with mock.patch("timecapsulesmb.cli.configure.probe_device_state", return_value=self.make_probe_state(self.make_probe_result_netbsd6())):
                            with mock.patch("timecapsulesmb.cli.configure.write_env_file", side_effect=fake_write_env_file):
                                with redirect_stdout(output):
                                    rc = configure.main([])
        self.assertEqual(rc, 0)
        self.assertEqual(fake_values["TC_AIRPORT_SYAP"], "119")
        self.assertNotIn("mDNS device model hint", seen_defaults)
        self.assertEqual(fake_values["TC_MDNS_DEVICE_MODEL"], "TimeCapsule8,119")

    def test_configure_skipped_mdns_netbsd6_little_autofills_syap_and_model(self) -> None:
        output = io.StringIO()
        fake_values = {}
        record = Discovered(
            name="AirPort Time Capsule",
            hostname="AirPort-Time-Capsule.local",
            ipv4=["192.168.1.72"],
            services={"_airport._tcp.local."},
            properties={"syAP": "106"},
        )
        prompt_values = iter([
            "root@10.0.0.2",
            "rootpw",
            "bridge0",
            "Data",
            "admin",
            "TimeCapsule",
            "samba4",
            "Time Capsule Samba 4",
            "timecapsulesamba4",
        ])

        def fake_prompt(label, default, _secret):
            if label == "Airport Utility syAP code":
                raise AssertionError("NetBSD6 little-endian should autofill syAP")
            if label == "mDNS device model hint":
                raise AssertionError("NetBSD6 little-endian should autofill mDNS model")
            return next(prompt_values)

        def fake_write_env_file(_path, values):
            fake_values.update(values)

        with mock.patch("timecapsulesmb.cli.configure.parse_env_values", return_value={}):
            with mock.patch("timecapsulesmb.cli.configure.discover_time_capsule_candidates", return_value=[record]):
                with mock.patch("builtins.input", side_effect=["q"]):
                    with mock.patch("timecapsulesmb.cli.configure.prompt", side_effect=fake_prompt):
                        with mock.patch("timecapsulesmb.cli.configure.probe_device_state", return_value=self.make_probe_state(self.make_probe_result_netbsd6())):
                            with mock.patch("timecapsulesmb.cli.configure.probe_device_state", return_value=self.make_probe_state(self.make_probe_result_netbsd6())):
                                with mock.patch("timecapsulesmb.cli.configure.write_env_file", side_effect=fake_write_env_file):
                                    with redirect_stdout(output):
                                        rc = configure.main([])
        self.assertEqual(rc, 0)
        self.assertEqual(fake_values["TC_AIRPORT_SYAP"], "119")
        self.assertEqual(fake_values["TC_MDNS_DEVICE_MODEL"], "TimeCapsule8,119")
        text = output.getvalue()
        self.assertIn("Discovery skipped.", text)
        self.assertIn("Using probed TC_AIRPORT_SYAP: 119", text)
        self.assertIn("Using probed TC_MDNS_DEVICE_MODEL: TimeCapsule8,119", text)

    def test_configure_fails_when_probe_returns_unsupported_device(self) -> None:
        output = io.StringIO()
        prompt_values = iter([
            "rootpw",
        ])

        def fake_prompt(label, default, _secret):
            if label == "Time Capsule SSH target":
                return "root@10.0.0.2"
            return next(prompt_values)

        with mock.patch("timecapsulesmb.cli.configure.parse_env_values", return_value={}):
            with mock.patch("timecapsulesmb.cli.configure.discover_time_capsule_candidates", return_value=[]):
                with mock.patch("timecapsulesmb.cli.configure.prompt", side_effect=fake_prompt):
                    with mock.patch("timecapsulesmb.cli.configure.probe_device_state", return_value=self.make_probe_state(self.make_probe_result_netbsd6())):
                        with mock.patch("timecapsulesmb.cli.configure.probe_device_state", return_value=self.make_probe_state(self.make_probe_result_netbsd6_unknown())):
                            with self.assertRaises(SystemExit) as ctx:
                                with redirect_stdout(output):
                                    configure.main([])
        self.assertIn("unknown-endian", str(ctx.exception))
        self.assertNotIn("Using probed TC_AIRPORT_SYAP: 119", output.getvalue())

    def test_configure_skipped_mdns_netbsd6_big_fails_fast(self) -> None:
        output = io.StringIO()
        record = Discovered(
            name="AirPort Time Capsule",
            hostname="AirPort-Time-Capsule.local",
            ipv4=["192.168.1.72"],
            services={"_airport._tcp.local."},
            properties={"syAP": "106"},
        )
        prompt_values = iter([
            "root@10.0.0.2",
            "rootpw",
        ])

        def fake_prompt(_label, _default, _secret):
            return next(prompt_values)

        with mock.patch("timecapsulesmb.cli.configure.parse_env_values", return_value={}):
            with mock.patch("timecapsulesmb.cli.configure.discover_time_capsule_candidates", return_value=[record]):
                with mock.patch("builtins.input", side_effect=["q"]):
                    with mock.patch("timecapsulesmb.cli.configure.prompt", side_effect=fake_prompt):
                        with mock.patch("timecapsulesmb.cli.configure.probe_device_state", return_value=self.make_probe_state(self.make_probe_result_netbsd6())):
                            with mock.patch("timecapsulesmb.cli.configure.probe_device_state", return_value=self.make_probe_state(self.make_probe_result_netbsd6_big())):
                                with self.assertRaises(SystemExit) as ctx:
                                    with redirect_stdout(output):
                                        configure.main([])
        self.assertIn("big-endian", str(ctx.exception))
        self.assertNotIn("Using probed TC_AIRPORT_SYAP", output.getvalue())

    def test_configure_skipped_mdns_netbsd6_unknown_fails_fast(self) -> None:
        output = io.StringIO()
        record = Discovered(
            name="AirPort Time Capsule",
            hostname="AirPort-Time-Capsule.local",
            ipv4=["192.168.1.72"],
            services={"_airport._tcp.local."},
            properties={"syAP": "106"},
        )
        prompt_values = iter([
            "root@10.0.0.2",
            "rootpw",
        ])

        def fake_prompt(_label, _default, _secret):
            return next(prompt_values)

        with mock.patch("timecapsulesmb.cli.configure.parse_env_values", return_value={}):
            with mock.patch("timecapsulesmb.cli.configure.discover_time_capsule_candidates", return_value=[record]):
                with mock.patch("builtins.input", side_effect=["q"]):
                    with mock.patch("timecapsulesmb.cli.configure.prompt", side_effect=fake_prompt):
                        with mock.patch("timecapsulesmb.cli.configure.probe_device_state", return_value=self.make_probe_state(self.make_probe_result_netbsd6())):
                            with mock.patch("timecapsulesmb.cli.configure.probe_device_state", return_value=self.make_probe_state(self.make_probe_result_netbsd6_unknown())):
                                with self.assertRaises(SystemExit) as ctx:
                                    with redirect_stdout(output):
                                        configure.main([])
        self.assertIn("unknown-endian", str(ctx.exception))
        self.assertNotIn("Using probed TC_AIRPORT_SYAP", output.getvalue())

    def test_configure_skipped_mdns_netbsd_other_fails_fast(self) -> None:
        output = io.StringIO()
        record = Discovered(
            name="AirPort Time Capsule",
            hostname="AirPort-Time-Capsule.local",
            ipv4=["192.168.1.72"],
            services={"_airport._tcp.local."},
            properties={"syAP": "106"},
        )
        prompt_values = iter([
            "root@10.0.0.2",
            "rootpw",
        ])

        def fake_prompt(_label, _default, _secret):
            return next(prompt_values)

        with mock.patch("timecapsulesmb.cli.configure.parse_env_values", return_value={}):
            with mock.patch("timecapsulesmb.cli.configure.discover_time_capsule_candidates", return_value=[record]):
                with mock.patch("builtins.input", side_effect=["q"]):
                    with mock.patch("timecapsulesmb.cli.configure.prompt", side_effect=fake_prompt):
                        with mock.patch("timecapsulesmb.cli.configure.probe_device_state", return_value=self.make_probe_state(self.make_probe_result_netbsd6())):
                            with mock.patch("timecapsulesmb.cli.configure.probe_device_state", return_value=self.make_probe_state(self.make_probe_result_netbsd5())):
                                with self.assertRaises(SystemExit) as ctx:
                                    with redirect_stdout(output):
                                        configure.main([])
        self.assertIn("NetBSD 5.0", str(ctx.exception))
        self.assertNotIn("Using probed TC_AIRPORT_SYAP", output.getvalue())

    def test_configure_skipped_mdns_netbsd4le_shows_syap_table_and_restricts_candidates(self) -> None:
        output = io.StringIO()
        fake_values = {}
        record = Discovered(
            name="AirPort Time Capsule",
            hostname="AirPort-Time-Capsule.local",
            ipv4=["192.168.1.72"],
            services={"_airport._tcp.local."},
            properties={"syAP": "106"},
        )
        prompt_values = iter([
            "root@10.0.0.2",
            "rootpw",
            "bridge0",
            "Data",
            "admin",
            "TimeCapsule",
            "samba4",
            "Time Capsule Samba 4",
            "timecapsulesamba4",
            "119",
            "113",
        ])

        def fake_prompt(label, default, _secret):
            if label == "mDNS device model hint":
                raise AssertionError("mDNS device model should be derived from the final syAP")
            return next(prompt_values)

        def fake_write_env_file(_path, values):
            fake_values.update(values)

        with mock.patch("timecapsulesmb.cli.configure.parse_env_values", return_value={}):
            with mock.patch("timecapsulesmb.cli.configure.discover_time_capsule_candidates", return_value=[record]):
                with mock.patch("builtins.input", side_effect=["q"]):
                    with mock.patch("timecapsulesmb.cli.configure.prompt", side_effect=fake_prompt):
                        with mock.patch("timecapsulesmb.cli.configure.probe_device_state", return_value=self.make_probe_state(self.make_probe_result_netbsd6())):
                            with mock.patch("timecapsulesmb.cli.configure.probe_device_state", return_value=self.make_probe_state(self.make_probe_result_netbsd4le())):
                                with mock.patch("timecapsulesmb.cli.configure.write_env_file", side_effect=fake_write_env_file):
                                    with redirect_stdout(output):
                                        rc = configure.main([])
        self.assertEqual(rc, 0)
        self.assertEqual(fake_values["TC_AIRPORT_SYAP"], "113")
        self.assertEqual(fake_values["TC_MDNS_DEVICE_MODEL"], "TimeCapsule6,113")
        text = output.getvalue()
        self.assertIn("Generation                Model identifier    syAP", text)
        self.assertIn("3rd gen (late 2009)       TimeCapsule6,113    113", text)
        self.assertIn("4th gen (mid 2011)        TimeCapsule6,116    116", text)
        self.assertIn("From detected connection, syAP code should be one of: 113, 116", text)

    def test_configure_probed_netbsd4be_shows_syap_table_and_restricts_candidates(self) -> None:
        output = io.StringIO()
        fake_values = {}
        syap_defaults: list[str] = []
        record = Discovered(
            name="AirPort Time Capsule",
            hostname="AirPort-Time-Capsule.local",
            ipv4=["192.168.1.72"],
            services={"_airport._tcp.local."},
            properties={"syAP": "113"},
        )
        prompt_values = iter([
            "root@10.0.0.2",
            "rootpw",
            "bridge0",
            "Data",
            "admin",
            "TimeCapsule",
            "samba4",
            "Time Capsule Samba 4",
            "timecapsulesamba4",
            "119",
            "106",
        ])

        def fake_prompt(label, default, _secret):
            if label == "Airport Utility syAP code":
                syap_defaults.append(default)
            if label == "mDNS device model hint":
                raise AssertionError("mDNS device model should be derived from the final syAP")
            return next(prompt_values)

        def fake_write_env_file(_path, values):
            fake_values.update(values)

        with mock.patch("timecapsulesmb.cli.configure.parse_env_values", return_value={}):
            with mock.patch("timecapsulesmb.cli.configure.discover_time_capsule_candidates", return_value=[record]):
                with mock.patch("builtins.input", side_effect=["q"]):
                    with mock.patch("timecapsulesmb.cli.configure.prompt", side_effect=fake_prompt):
                        with mock.patch("timecapsulesmb.cli.configure.probe_device_state", return_value=self.make_probe_state(self.make_probe_result_netbsd6())):
                            with mock.patch("timecapsulesmb.cli.configure.probe_device_state", return_value=self.make_probe_state(self.make_probe_result_netbsd4be())):
                                with mock.patch("timecapsulesmb.cli.configure.write_env_file", side_effect=fake_write_env_file):
                                    with redirect_stdout(output):
                                        rc = configure.main([])
        self.assertEqual(rc, 0)
        self.assertEqual(syap_defaults, ["", ""])
        self.assertEqual(fake_values["TC_AIRPORT_SYAP"], "106")
        self.assertEqual(fake_values["TC_MDNS_DEVICE_MODEL"], "TimeCapsule6,106")
        text = output.getvalue()
        self.assertIn("Generation                Model identifier    syAP", text)
        self.assertIn("1st gen (early 2008)      TimeCapsule6,106    106", text)
        self.assertIn("2nd gen (early 2009)      TimeCapsule6,109    109", text)
        self.assertIn("From detected connection, syAP code should be one of: 106, 109", text)

    def test_configure_saves_airport_syap_from_discovery_without_prompting(self) -> None:
        output = io.StringIO()
        fake_values = {}
        record = Discovered(
            name="Time Capsule Samba 4",
            hostname="timecapsulesamba4.local",
            ipv4=["192.168.1.217"],
            services={"_airport._tcp.local.", "_smb._tcp.local."},
            properties={"syAP": "119"},
        )
        prompt_values = iter([
            "rootpw",
            "bridge0",
            "Data",
            "admin",
            "TimeCapsule",
            "samba4",
            "Time Capsule Samba 4",
            "timecapsulesamba4",
        ])

        def fake_prompt(label, default, _secret):
            if label == "Time Capsule SSH target":
                return default
            if label == "mDNS device model hint":
                return default
            return next(prompt_values)

        def fake_write_env_file(_path, values):
            fake_values.update(values)

        with mock.patch("timecapsulesmb.cli.configure.parse_env_values", return_value={}):
            with mock.patch("timecapsulesmb.cli.configure.discover_time_capsule_candidates", return_value=[record]):
                with mock.patch("builtins.input", side_effect=["1"]):
                    with mock.patch("timecapsulesmb.cli.configure.prompt", side_effect=fake_prompt):
                        with mock.patch("timecapsulesmb.cli.configure.probe_device_state", return_value=self.make_probe_state(self.make_probe_result_unreachable())):
                            with mock.patch("timecapsulesmb.cli.configure.confirm", return_value=True):
                                with mock.patch("timecapsulesmb.cli.configure.write_env_file", side_effect=fake_write_env_file):
                                    with redirect_stdout(output):
                                        rc = configure.main([])
        self.assertEqual(rc, 0)
        self.assertEqual(fake_values["TC_AIRPORT_SYAP"], "119")
        self.assertEqual(fake_values["TC_MDNS_DEVICE_MODEL"], "TimeCapsule8,119")
        self.assertIn("Using discovered TC_AIRPORT_SYAP: 119", output.getvalue())

    def test_configure_discovered_syap_beats_invalid_existing_syap(self) -> None:
        output = io.StringIO()
        fake_values = {}
        seen_labels: list[str] = []
        record = Discovered(
            name="Time Capsule Samba 4",
            hostname="timecapsulesamba4.local",
            ipv4=["192.168.1.217"],
            services={"_airport._tcp.local.", "_smb._tcp.local."},
            properties={"syAP": "119"},
        )
        prompt_values = iter([
            "rootpw",
            "bridge0",
            "Data",
            "admin",
            "TimeCapsule",
            "samba4",
            "Time Capsule Samba 4",
            "timecapsulesamba4",
        ])

        def fake_prompt(label, default, _secret):
            seen_labels.append(label)
            if label == "Time Capsule SSH target":
                return default
            return next(prompt_values)

        def fake_write_env_file(_path, values):
            fake_values.update(values)

        with mock.patch("timecapsulesmb.cli.configure.parse_env_values", return_value={"TC_AIRPORT_SYAP": "999"}):
            with mock.patch("timecapsulesmb.cli.configure.discover_time_capsule_candidates", return_value=[record]):
                with mock.patch("builtins.input", side_effect=["1"]):
                    with mock.patch("timecapsulesmb.cli.configure.prompt", side_effect=fake_prompt):
                        with mock.patch("timecapsulesmb.cli.configure.probe_device_state", return_value=self.make_probe_state(self.make_probe_result_unreachable())):
                            with mock.patch("timecapsulesmb.cli.configure.confirm", return_value=True):
                                with mock.patch("timecapsulesmb.cli.configure.write_env_file", side_effect=fake_write_env_file):
                                    with redirect_stdout(output):
                                        rc = configure.main([])
        self.assertEqual(rc, 0)
        self.assertEqual(fake_values["TC_AIRPORT_SYAP"], "119")
        self.assertEqual(fake_values["TC_MDNS_DEVICE_MODEL"], "TimeCapsule8,119")
        self.assertNotIn("Airport Utility syAP code", seen_labels)
        self.assertNotIn("mDNS device model hint", seen_labels)

    def test_configure_discovered_missing_syap_prompts_with_valid_existing_syap_default(self) -> None:
        output = io.StringIO()
        fake_values = {}
        seen_defaults = {}
        record = Discovered(
            name="Time Capsule Samba 4",
            hostname="timecapsulesamba4.local",
            ipv4=["192.168.1.217"],
            services={"_airport._tcp.local.", "_smb._tcp.local."},
            properties={},
        )
        prompt_values = iter([
            "rootpw",
            "bridge0",
            "Data",
            "admin",
            "TimeCapsule",
            "samba4",
            "Time Capsule Samba 4",
            "timecapsulesamba4",
        ])

        def fake_prompt(label, default, _secret):
            seen_defaults[label] = default
            if label == "Time Capsule SSH target":
                return default
            if label == "Airport Utility syAP code":
                return default
            return next(prompt_values)

        def fake_write_env_file(_path, values):
            fake_values.update(values)

        with mock.patch("timecapsulesmb.cli.configure.parse_env_values", return_value={"TC_AIRPORT_SYAP": "116"}):
            with mock.patch("timecapsulesmb.cli.configure.discover_time_capsule_candidates", return_value=[record]):
                with mock.patch("builtins.input", side_effect=["1"]):
                    with mock.patch("timecapsulesmb.cli.configure.prompt", side_effect=fake_prompt):
                        with mock.patch("timecapsulesmb.cli.configure.probe_device_state", return_value=self.make_probe_state(self.make_probe_result_unreachable())):
                            with mock.patch("timecapsulesmb.cli.configure.confirm", return_value=True):
                                with mock.patch("timecapsulesmb.cli.configure.write_env_file", side_effect=fake_write_env_file):
                                    with redirect_stdout(output):
                                        rc = configure.main([])
        self.assertEqual(rc, 0)
        self.assertEqual(seen_defaults["Airport Utility syAP code"], "116")
        self.assertEqual(fake_values["TC_AIRPORT_SYAP"], "116")
        self.assertEqual(fake_values["TC_MDNS_DEVICE_MODEL"], "TimeCapsule6,116")
        self.assertNotIn("mDNS device model hint", seen_defaults)
        self.assertIn("Found saved value: 116", output.getvalue())

    def test_configure_discovered_invalid_syap_prompts_with_valid_existing_syap_default(self) -> None:
        output = io.StringIO()
        fake_values = {}
        seen_defaults = {}
        record = Discovered(
            name="Time Capsule Samba 4",
            hostname="timecapsulesamba4.local",
            ipv4=["192.168.1.217"],
            services={"_airport._tcp.local.", "_smb._tcp.local."},
            properties={"syAP": "999"},
        )
        prompt_values = iter([
            "rootpw",
            "bridge0",
            "Data",
            "admin",
            "TimeCapsule",
            "samba4",
            "Time Capsule Samba 4",
            "timecapsulesamba4",
        ])

        def fake_prompt(label, default, _secret):
            seen_defaults[label] = default
            if label == "Time Capsule SSH target":
                return default
            if label == "Airport Utility syAP code":
                return default
            return next(prompt_values)

        def fake_write_env_file(_path, values):
            fake_values.update(values)

        with mock.patch("timecapsulesmb.cli.configure.parse_env_values", return_value={"TC_AIRPORT_SYAP": "109"}):
            with mock.patch("timecapsulesmb.cli.configure.discover_time_capsule_candidates", return_value=[record]):
                with mock.patch("builtins.input", side_effect=["1"]):
                    with mock.patch("timecapsulesmb.cli.configure.prompt", side_effect=fake_prompt):
                        with mock.patch("timecapsulesmb.cli.configure.probe_device_state", return_value=self.make_probe_state(self.make_probe_result_unreachable())):
                            with mock.patch("timecapsulesmb.cli.configure.confirm", return_value=True):
                                with mock.patch("timecapsulesmb.cli.configure.write_env_file", side_effect=fake_write_env_file):
                                    with redirect_stdout(output):
                                        rc = configure.main([])
        self.assertEqual(rc, 0)
        self.assertEqual(seen_defaults["Airport Utility syAP code"], "109")
        self.assertEqual(fake_values["TC_AIRPORT_SYAP"], "109")
        self.assertEqual(fake_values["TC_MDNS_DEVICE_MODEL"], "TimeCapsule6,109")
        self.assertNotIn("mDNS device model hint", seen_defaults)
        self.assertIn("Found saved value: 109", output.getvalue())
        self.assertIn("could not discover Airport Utility syAP", output.getvalue())

    def test_configure_discovered_invalid_syap_reprompts_until_valid_when_existing_syap_invalid(self) -> None:
        output = io.StringIO()
        fake_values = {}
        syap_defaults: list[str] = []
        syap_attempts = iter(["999", "113"])
        record = Discovered(
            name="Time Capsule Samba 4",
            hostname="timecapsulesamba4.local",
            ipv4=["192.168.1.217"],
            services={"_airport._tcp.local.", "_smb._tcp.local."},
            properties={"syAP": "bad"},
        )
        prompt_values = iter([
            "rootpw",
            "bridge0",
            "Data",
            "admin",
            "TimeCapsule",
            "samba4",
            "Time Capsule Samba 4",
            "timecapsulesamba4",
        ])

        def fake_prompt(label, default, _secret):
            if label == "Time Capsule SSH target":
                return default
            if label == "Airport Utility syAP code":
                syap_defaults.append(default)
                return next(syap_attempts)
            if label == "mDNS device model hint":
                raise AssertionError("mDNS device model should be derived from the final syAP")
            return next(prompt_values)

        def fake_write_env_file(_path, values):
            fake_values.update(values)

        with mock.patch("timecapsulesmb.cli.configure.parse_env_values", return_value={"TC_AIRPORT_SYAP": "998"}):
            with mock.patch("timecapsulesmb.cli.configure.discover_time_capsule_candidates", return_value=[record]):
                with mock.patch("builtins.input", side_effect=["1"]):
                    with mock.patch("timecapsulesmb.cli.configure.prompt", side_effect=fake_prompt):
                        with mock.patch("timecapsulesmb.cli.configure.probe_device_state", return_value=self.make_probe_state(self.make_probe_result_unreachable())):
                            with mock.patch("timecapsulesmb.cli.configure.confirm", return_value=True):
                                with mock.patch("timecapsulesmb.cli.configure.write_env_file", side_effect=fake_write_env_file):
                                    with redirect_stdout(output):
                                        rc = configure.main([])
        self.assertEqual(rc, 0)
        self.assertEqual(syap_defaults, ["", ""])
        self.assertEqual(fake_values["TC_AIRPORT_SYAP"], "113")
        self.assertEqual(fake_values["TC_MDNS_DEVICE_MODEL"], "TimeCapsule6,113")
        self.assertIn("The configured syAP is invalid.", output.getvalue())

    def test_configure_can_skip_single_discovered_device(self) -> None:
        output = io.StringIO()
        fake_values = {}
        record = Discovered(
            name="Time Capsule Samba 4",
            hostname="timecapsulesamba4.local",
            ipv4=["192.168.1.217"],
            services={"_airport._tcp.local.", "_smb._tcp.local."},
            properties={"syAP": "119"},
        )
        prompt_values = iter([
            "rootpw",
            "bridge0",
            "Data",
            "admin",
            "TimeCapsule",
            "samba4",
            "Time Capsule Samba 4",
            "timecapsulesamba4",
            "119",
        ])

        def fake_prompt(label, default, _secret):
            if label == "Time Capsule SSH target":
                return default
            if label == "mDNS device model hint":
                return default
            return next(prompt_values)

        def fake_write_env_file(_path, values):
            fake_values.update(values)

        with mock.patch("timecapsulesmb.cli.configure.parse_env_values", return_value={}):
            with mock.patch("timecapsulesmb.cli.configure.discover_time_capsule_candidates", return_value=[record]):
                with mock.patch("builtins.input", side_effect=["q"]):
                    with mock.patch("timecapsulesmb.cli.configure.prompt", side_effect=fake_prompt):
                        with mock.patch("timecapsulesmb.cli.configure.probe_device_state", return_value=self.make_probe_state(self.make_probe_result_unreachable())):
                            with mock.patch("timecapsulesmb.cli.configure.confirm", return_value=True):
                                with mock.patch("timecapsulesmb.cli.configure.write_env_file", side_effect=fake_write_env_file):
                                    with redirect_stdout(output):
                                        rc = configure.main([])
        self.assertEqual(rc, 0)
        self.assertEqual(fake_values["TC_HOST"], DEFAULTS["TC_HOST"])
        self.assertEqual(fake_values["TC_AIRPORT_SYAP"], "119")
        self.assertIn("Found devices:", output.getvalue())
        self.assertIn(f"Discovery skipped. Falling back to {DEFAULTS['TC_HOST']}.", output.getvalue())

    def test_configure_skipped_discovery_reprompts_invalid_existing_syap(self) -> None:
        output = io.StringIO()
        fake_values = {}
        syap_defaults: list[str] = []
        syap_attempts = iter(["999", "116"])
        prompt_values = iter([
            "root@10.0.0.2",
            "goodpw",
            "bridge0",
            "Data",
            "admin",
            "TimeCapsule",
            "samba4",
            "Time Capsule Samba 4",
            "timecapsulesamba4",
        ])

        def fake_prompt(label, default, _secret):
            if label == "Airport Utility syAP code":
                syap_defaults.append(default)
                return next(syap_attempts)
            if label == "mDNS device model hint":
                raise AssertionError("mDNS device model should be derived from the final syAP")
            return next(prompt_values)

        def fake_write_env_file(_path, values):
            fake_values.update(values)

        with mock.patch("timecapsulesmb.cli.configure.parse_env_values", return_value={"TC_AIRPORT_SYAP": "999"}):
            with mock.patch("timecapsulesmb.cli.configure.discover_time_capsule_candidates", return_value=[]):
                with mock.patch("timecapsulesmb.cli.configure.prompt", side_effect=fake_prompt):
                    with mock.patch("timecapsulesmb.cli.configure.probe_device_state", return_value=self.make_probe_state(self.make_probe_result_unreachable())):
                        with mock.patch("timecapsulesmb.cli.configure.confirm", return_value=True):
                            with mock.patch("timecapsulesmb.cli.configure.write_env_file", side_effect=fake_write_env_file):
                                with redirect_stdout(output):
                                    rc = configure.main([])
        self.assertEqual(rc, 0)
        self.assertEqual(syap_defaults, ["", ""])
        self.assertEqual(fake_values["TC_AIRPORT_SYAP"], "116")
        self.assertEqual(fake_values["TC_MDNS_DEVICE_MODEL"], "TimeCapsule6,116")
        self.assertIn("The configured syAP is invalid.", output.getvalue())

    def test_configure_skipped_discovery_prints_when_reusing_existing_syap(self) -> None:
        output = io.StringIO()
        fake_values = {}
        existing = {
            "TC_AIRPORT_SYAP": "116",
        }
        prompt_values = iter([
            "root@10.0.0.2",
            "goodpw",
            "bridge0",
            "Data",
            "admin",
            "TimeCapsule",
            "samba4",
            "Time Capsule Samba 4",
            "timecapsulesamba4",
        ])

        def fake_prompt(_label, _default, _secret):
            return next(prompt_values)

        def fake_write_env_file(_path, values):
            fake_values.update(values)

        with mock.patch("timecapsulesmb.cli.configure.parse_env_values", return_value=existing):
            with mock.patch("timecapsulesmb.cli.configure.discover_time_capsule_candidates", return_value=[]):
                with mock.patch("timecapsulesmb.cli.configure.prompt", side_effect=fake_prompt):
                    with mock.patch("timecapsulesmb.cli.configure.probe_device_state", return_value=self.make_probe_state(self.make_probe_result_unreachable())):
                        with mock.patch("timecapsulesmb.cli.configure.confirm", return_value=True):
                            with mock.patch("timecapsulesmb.cli.configure.write_env_file", side_effect=fake_write_env_file):
                                with redirect_stdout(output):
                                    rc = configure.main([])
        self.assertEqual(rc, 0)
        self.assertEqual(fake_values["TC_AIRPORT_SYAP"], "116")
        self.assertIn("Using TC_AIRPORT_SYAP from .env: 116", output.getvalue())

    def test_configure_prints_found_saved_value_for_valid_existing_share_name(self) -> None:
        output = io.StringIO()
        fake_values = {}
        share_defaults: list[str] = []
        existing = {
            "TC_SHARE_NAME": "Archive Data",
        }
        prompt_values = iter([
            "root@10.0.0.2",
            "goodpw",
            "bridge0",
            "Archive Data",
            "admin",
            "TimeCapsule",
            "samba4",
            "Time Capsule Samba 4",
            "timecapsulesamba4",
            "119",
        ])

        def fake_prompt(_label, default, _secret):
            if _label == "SMB share name":
                share_defaults.append(default)
            return next(prompt_values)

        def fake_write_env_file(_path, values):
            fake_values.update(values)

        with mock.patch("timecapsulesmb.cli.configure.parse_env_values", return_value=existing):
            with mock.patch("timecapsulesmb.cli.configure.discover_time_capsule_candidates", return_value=[]):
                with mock.patch("timecapsulesmb.cli.configure.prompt", side_effect=fake_prompt):
                    with mock.patch("timecapsulesmb.cli.configure.probe_device_state", return_value=self.make_probe_state(self.make_probe_result_unreachable())):
                        with mock.patch("timecapsulesmb.cli.configure.confirm", return_value=True):
                            with mock.patch("timecapsulesmb.cli.configure.write_env_file", side_effect=fake_write_env_file):
                                with redirect_stdout(output):
                                    rc = configure.main([])
        self.assertEqual(rc, 0)
        self.assertEqual(share_defaults, ["Archive Data"])
        self.assertEqual(fake_values["TC_SHARE_NAME"], "Archive Data")
        self.assertIn("Found saved value: Archive Data", output.getvalue())

    def test_configure_invalid_ssh_inferred_model_falls_back_to_existing_syap_model(self) -> None:
        output = io.StringIO()
        fake_values = {}
        existing = {
            "TC_AIRPORT_SYAP": "116",
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule8,119",
            "TC_SSH_OPTS": "-o foo",
        }
        prompt_values = iter([
            "rootpw",
            "bridge0",
            "Data",
            "admin",
            "TimeCapsule",
            "samba4",
            "Time Capsule Samba 4",
            "timecapsulesamba4",
        ])
        seen_defaults = {}

        def fake_prompt(label, default, _secret):
            seen_defaults[label] = default
            if label == "Time Capsule SSH target":
                return "root@10.0.0.2"
            return next(prompt_values)

        def fake_write_env_file(_path, values):
            fake_values.update(values)

        with mock.patch("timecapsulesmb.cli.configure.parse_env_values", return_value=existing):
            with mock.patch("timecapsulesmb.cli.configure.discover_time_capsule_candidates", return_value=[]):
                with mock.patch("timecapsulesmb.cli.configure.prompt", side_effect=fake_prompt):
                    with mock.patch("timecapsulesmb.cli.configure.probe_device_state", return_value=self.make_probe_state(self.make_probe_result_netbsd6())):
                        with mock.patch("timecapsulesmb.cli.configure.probe_device_state", return_value=self.make_probe_state(self.make_probe_result_netbsd6())):
                            with mock.patch("timecapsulesmb.cli.configure.write_env_file", side_effect=fake_write_env_file):
                                with redirect_stdout(output):
                                    rc = configure.main([])
        self.assertEqual(rc, 0)
        self.assertEqual(fake_values["TC_AIRPORT_SYAP"], "119")
        self.assertEqual(fake_values["TC_MDNS_DEVICE_MODEL"], "TimeCapsule8,119")
        self.assertNotIn("mDNS device model hint", seen_defaults)
        self.assertIn("Using probed TC_AIRPORT_SYAP: 119", output.getvalue())

    def test_configure_ssh_inferred_mdns_device_model_overrides_existing_model(self) -> None:
        output = io.StringIO()
        fake_values = {}
        existing = {
            "TC_AIRPORT_SYAP": "119",
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule6,113",
            "TC_SSH_OPTS": "-o foo",
        }
        prompt_values = iter([
            "rootpw",
            "bridge0",
            "Data",
            "admin",
            "TimeCapsule",
            "samba4",
            "Time Capsule Samba 4",
            "timecapsulesamba4",
        ])
        seen_defaults = {}

        def fake_prompt(label, default, _secret):
            seen_defaults[label] = default
            if label == "Time Capsule SSH target":
                return "root@10.0.0.2"
            if label == "mDNS device model hint":
                return default
            return next(prompt_values)

        def fake_write_env_file(_path, values):
            fake_values.update(values)

        with mock.patch("timecapsulesmb.cli.configure.parse_env_values", return_value=existing):
            with mock.patch("timecapsulesmb.cli.configure.discover_time_capsule_candidates", return_value=[]):
                with mock.patch("timecapsulesmb.cli.configure.prompt", side_effect=fake_prompt):
                    with mock.patch("timecapsulesmb.cli.configure.probe_device_state", return_value=self.make_probe_state(self.make_probe_result_netbsd6())):
                        with mock.patch("timecapsulesmb.cli.configure.probe_device_state", return_value=self.make_probe_state(self.make_probe_result_netbsd6())):
                            with mock.patch("timecapsulesmb.cli.configure.write_env_file", side_effect=fake_write_env_file):
                                with redirect_stdout(output):
                                    rc = configure.main([])
        self.assertEqual(rc, 0)
        self.assertNotIn("mDNS device model hint", seen_defaults)
        self.assertEqual(fake_values["TC_AIRPORT_SYAP"], "119")
        self.assertEqual(fake_values["TC_MDNS_DEVICE_MODEL"], "TimeCapsule8,119")
        self.assertIn("Using probed TC_AIRPORT_SYAP: 119", output.getvalue())
        self.assertIn("Using probed TC_MDNS_DEVICE_MODEL: TimeCapsule8,119", output.getvalue())

    def test_configure_skipped_discovery_uses_generic_model_default_when_syap_has_no_model_mapping(self) -> None:
        output = io.StringIO()
        fake_values = {}
        existing = {
            "TC_AIRPORT_SYAP": "119",
            "TC_MDNS_DEVICE_MODEL": "NotATimeCapsule",
        }
        prompt_values = iter([
            "root@10.0.0.2",
            "goodpw",
            "bridge0",
            "Data",
            "admin",
            "TimeCapsule",
            "samba4",
            "Time Capsule Samba 4",
            "timecapsulesamba4",
            "TimeCapsule",
        ])
        seen_defaults = {}

        def fake_prompt(label, default, _secret):
            seen_defaults[label] = default
            return next(prompt_values)

        def fake_write_env_file(_path, values):
            fake_values.update(values)

        with mock.patch("timecapsulesmb.cli.configure.parse_env_values", return_value=existing):
            with mock.patch("timecapsulesmb.cli.configure.discover_time_capsule_candidates", return_value=[]):
                with mock.patch("timecapsulesmb.cli.configure.prompt", side_effect=fake_prompt):
                    with mock.patch("timecapsulesmb.cli.configure.probe_device_state", return_value=self.make_probe_state(self.make_probe_result_unreachable())):
                        with mock.patch("timecapsulesmb.cli.configure.confirm", return_value=True):
                            with mock.patch("timecapsulesmb.cli.configure.infer_mdns_device_model_from_airport_syap", return_value=None):
                                with mock.patch("timecapsulesmb.cli.configure.write_env_file", side_effect=fake_write_env_file):
                                    with redirect_stdout(output):
                                        rc = configure.main([])
        self.assertEqual(rc, 0)
        self.assertEqual(fake_values["TC_AIRPORT_SYAP"], "119")
        self.assertEqual(seen_defaults["mDNS device model hint"], "TimeCapsule")
        self.assertEqual(fake_values["TC_MDNS_DEVICE_MODEL"], "TimeCapsule")
        self.assertIn("Using TC_AIRPORT_SYAP from .env: 119", output.getvalue())

    def test_configure_existing_syap_autofills_mdns_device_model_when_undetected(self) -> None:
        output = io.StringIO()
        fake_values = {}
        existing = {
            "TC_AIRPORT_SYAP": "116",
        }
        prompt_values = iter([
            "root@10.0.0.2",
            "goodpw",
            "bridge0",
            "Data",
            "admin",
            "TimeCapsule",
            "samba4",
            "Time Capsule Samba 4",
            "timecapsulesamba4",
        ])
        seen_defaults = {}

        def fake_prompt(label, default, _secret):
            seen_defaults[label] = default
            if label == "mDNS device model hint":
                return default
            return next(prompt_values)

        def fake_write_env_file(_path, values):
            fake_values.update(values)

        with mock.patch("timecapsulesmb.cli.configure.parse_env_values", return_value=existing):
            with mock.patch("timecapsulesmb.cli.configure.discover_time_capsule_candidates", return_value=[]):
                with mock.patch("timecapsulesmb.cli.configure.prompt", side_effect=fake_prompt):
                    with mock.patch("timecapsulesmb.cli.configure.probe_device_state", return_value=self.make_probe_state(self.make_probe_result_unreachable())):
                        with mock.patch("timecapsulesmb.cli.configure.confirm", return_value=True):
                            with mock.patch("timecapsulesmb.cli.configure.write_env_file", side_effect=fake_write_env_file):
                                with redirect_stdout(output):
                                    rc = configure.main([])
        self.assertEqual(rc, 0)
        self.assertEqual(fake_values["TC_AIRPORT_SYAP"], "116")
        self.assertNotIn("mDNS device model hint", seen_defaults)
        self.assertEqual(fake_values["TC_MDNS_DEVICE_MODEL"], "TimeCapsule6,116")
        self.assertIn("Using TC_AIRPORT_SYAP from .env: 116", output.getvalue())
        self.assertIn("Using TC_MDNS_DEVICE_MODEL derived from TC_AIRPORT_SYAP: TimeCapsule6,116", output.getvalue())

    def test_configure_prompted_syap_overrides_existing_mdns_device_model(self) -> None:
        output = io.StringIO()
        fake_values = {}
        existing = {
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule6,113",
        }
        prompt_values = iter([
            "root@10.0.0.2",
            "goodpw",
            "bridge0",
            "Data",
            "admin",
            "TimeCapsule",
            "samba4",
            "Time Capsule Samba 4",
            "timecapsulesamba4",
            "116",
            "TimeCapsule",
        ])
        seen_defaults = {}

        def fake_prompt(label, default, _secret):
            seen_defaults[label] = default
            if label == "mDNS device model hint":
                return default
            return next(prompt_values)

        def fake_write_env_file(_path, values):
            fake_values.update(values)

        with mock.patch("timecapsulesmb.cli.configure.parse_env_values", return_value=existing):
            with mock.patch("timecapsulesmb.cli.configure.discover_time_capsule_candidates", return_value=[]):
                with mock.patch("timecapsulesmb.cli.configure.prompt", side_effect=fake_prompt):
                    with mock.patch("timecapsulesmb.cli.configure.probe_device_state", return_value=self.make_probe_state(self.make_probe_result_unreachable())):
                        with mock.patch("timecapsulesmb.cli.configure.confirm", return_value=True):
                            with mock.patch("timecapsulesmb.cli.configure.write_env_file", side_effect=fake_write_env_file):
                                with redirect_stdout(output):
                                    rc = configure.main([])
        self.assertEqual(rc, 0)
        self.assertEqual(fake_values["TC_AIRPORT_SYAP"], "116")
        self.assertNotIn("mDNS device model hint", seen_defaults)
        self.assertEqual(fake_values["TC_MDNS_DEVICE_MODEL"], "TimeCapsule6,116")
        self.assertIn("Using TC_MDNS_DEVICE_MODEL derived from TC_AIRPORT_SYAP: TimeCapsule6,116", output.getvalue())

    def test_configure_skipped_discovery_prints_when_reusing_existing_mdns_device_model(self) -> None:
        output = io.StringIO()
        fake_values = {}
        existing = {
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule",
        }
        prompt_values = iter([
            "root@10.0.0.2",
            "goodpw",
            "bridge0",
            "Data",
            "admin",
            "TimeCapsule",
            "samba4",
            "Time Capsule Samba 4",
            "timecapsulesamba4",
            "116",
            "TimeCapsule",
        ])

        def fake_prompt(_label, _default, _secret):
            return next(prompt_values)

        def fake_write_env_file(_path, values):
            fake_values.update(values)

        with mock.patch("timecapsulesmb.cli.configure.parse_env_values", return_value=existing):
            with mock.patch("timecapsulesmb.cli.configure.discover_time_capsule_candidates", return_value=[]):
                with mock.patch("timecapsulesmb.cli.configure.prompt", side_effect=fake_prompt):
                    with mock.patch("timecapsulesmb.cli.configure.probe_device_state", return_value=self.make_probe_state(self.make_probe_result_unreachable())):
                        with mock.patch("timecapsulesmb.cli.configure.confirm", return_value=True):
                            with mock.patch("timecapsulesmb.cli.configure.infer_mdns_device_model_from_airport_syap", return_value=None):
                                with mock.patch("timecapsulesmb.cli.configure.write_env_file", side_effect=fake_write_env_file):
                                    with redirect_stdout(output):
                                        rc = configure.main([])
        self.assertEqual(rc, 0)
        self.assertEqual(fake_values["TC_MDNS_DEVICE_MODEL"], "TimeCapsule")
        self.assertIn("Using TC_MDNS_DEVICE_MODEL from .env: TimeCapsule", output.getvalue())

    def test_configure_invalid_saved_mdns_device_model_stays_silent_when_prompted(self) -> None:
        output = io.StringIO()
        fake_values = {}
        existing = {
            "TC_MDNS_DEVICE_MODEL": "NotATimeCapsule",
        }
        prompt_values = iter([
            "root@10.0.0.2",
            "goodpw",
            "bridge0",
            "Data",
            "admin",
            "TimeCapsule",
            "samba4",
            "Time Capsule Samba 4",
            "timecapsulesamba4",
            "116",
            "TimeCapsule",
        ])
        seen_defaults = {}

        def fake_prompt(label, default, _secret):
            seen_defaults[label] = default
            return next(prompt_values)

        def fake_write_env_file(_path, values):
            fake_values.update(values)

        with mock.patch("timecapsulesmb.cli.configure.parse_env_values", return_value=existing):
            with mock.patch("timecapsulesmb.cli.configure.discover_time_capsule_candidates", return_value=[]):
                with mock.patch("timecapsulesmb.cli.configure.prompt", side_effect=fake_prompt):
                    with mock.patch("timecapsulesmb.cli.configure.probe_device_state", return_value=self.make_probe_state(self.make_probe_result_unreachable())):
                        with mock.patch("timecapsulesmb.cli.configure.confirm", return_value=True):
                            with mock.patch("timecapsulesmb.cli.configure.infer_mdns_device_model_from_airport_syap", return_value=None):
                                with mock.patch("timecapsulesmb.cli.configure.write_env_file", side_effect=fake_write_env_file):
                                    with redirect_stdout(output):
                                        rc = configure.main([])
        self.assertEqual(rc, 0)
        self.assertEqual(seen_defaults["mDNS device model hint"], "TimeCapsule")
        self.assertEqual(fake_values["TC_MDNS_DEVICE_MODEL"], "TimeCapsule")
        self.assertNotIn("Found saved value: NotATimeCapsule", output.getvalue())
        self.assertNotIn("Using TC_MDNS_DEVICE_MODEL from .env: NotATimeCapsule", output.getvalue())

    def test_configure_rejects_blank_password_when_no_existing_password(self) -> None:
        output = io.StringIO()
        fake_values = {}
        input_values = iter([
            "root@10.0.0.2",
            "bridge0",
            "Data",
            "admin",
            "TimeCapsule",
            "samba4",
            "Time Capsule Samba 4",
            "timecapsulesamba4",
            "119",
            "",
        ])
        password_values = iter(["", "goodpw"])

        def fake_write_env_file(_path, values):
            fake_values.update(values)

        with mock.patch("timecapsulesmb.cli.configure.parse_env_values", return_value={}):
            with mock.patch("timecapsulesmb.cli.configure.discover_time_capsule_candidates", return_value=[]):
                with mock.patch("builtins.input", side_effect=lambda _prompt: next(input_values)):
                    with mock.patch("timecapsulesmb.cli.configure.getpass.getpass", side_effect=lambda _prompt: next(password_values)):
                        with mock.patch("timecapsulesmb.cli.configure.probe_device_state", return_value=self.make_probe_state(self.make_probe_result_unreachable())):
                            with mock.patch("timecapsulesmb.cli.configure.confirm", return_value=True):
                                with mock.patch("timecapsulesmb.cli.configure.write_env_file", side_effect=fake_write_env_file):
                                    with redirect_stdout(output):
                                        rc = configure.main([])
        self.assertEqual(rc, 0)
        self.assertEqual(fake_values["TC_PASSWORD"], "goodpw")
        self.assertIn("Time Capsule root password cannot be blank", output.getvalue())

    def test_configure_does_not_print_found_saved_value_for_password(self) -> None:
        output = io.StringIO()
        fake_values = {}
        input_values = iter([
            "root@10.0.0.2",
            "bridge0",
            "Data",
            "admin",
            "TimeCapsule",
            "samba4",
            "Time Capsule Samba 4",
            "timecapsulesamba4",
            "119",
        ])
        password_values = iter(["savedpw"])

        def fake_write_env_file(_path, values):
            fake_values.update(values)

        with mock.patch("timecapsulesmb.cli.configure.parse_env_values", return_value={"TC_PASSWORD": "savedpw"}):
            with mock.patch("timecapsulesmb.cli.configure.discover_time_capsule_candidates", return_value=[]):
                with mock.patch("builtins.input", side_effect=lambda _prompt: next(input_values)):
                    with mock.patch("timecapsulesmb.cli.configure.getpass.getpass", side_effect=lambda _prompt: next(password_values)):
                        with mock.patch("timecapsulesmb.cli.configure.probe_device_state", return_value=self.make_probe_state(self.make_probe_result_unreachable())):
                            with mock.patch("timecapsulesmb.cli.configure.confirm", return_value=True):
                                with mock.patch("timecapsulesmb.cli.configure.write_env_file", side_effect=fake_write_env_file):
                                    with redirect_stdout(output):
                                        rc = configure.main([])
        self.assertEqual(rc, 0)
        self.assertEqual(fake_values["TC_PASSWORD"], "savedpw")
        self.assertNotIn("Found saved value: savedpw", output.getvalue())

    def test_configure_reprompts_host_and_password_when_validation_fails(self) -> None:
        output = io.StringIO()
        fake_values = {}
        prompt_values = iter([
            "root@10.0.0.2",
            "badpw",
            "root@10.0.0.3",
            "goodpw",
            "bridge0",
            "Data",
            "admin",
            "TimeCapsule",
            "samba4",
            "Time Capsule Samba 4",
            "timecapsulesamba4",
            "119",
        ])

        def fake_prompt(_label, _default, _secret):
            label = _label
            default = _default
            if label == "mDNS device model hint":
                return default
            return next(prompt_values)

        def fake_write_env_file(_path, values):
            fake_values.update(values)

        with mock.patch("timecapsulesmb.cli.configure.parse_env_values", return_value={}):
            with mock.patch("timecapsulesmb.cli.configure.discover_time_capsule_candidates", return_value=[]):
                with mock.patch("timecapsulesmb.cli.configure.prompt", side_effect=fake_prompt):
                    with mock.patch("timecapsulesmb.cli.configure.probe_device_state", return_value=self.make_probe_state(self.make_probe_result_netbsd6())):
                        with mock.patch("timecapsulesmb.cli.configure.probe_device_state", side_effect=[self.make_probe_state(self.make_probe_result_auth_failed()), self.make_probe_state(self.make_probe_result_netbsd6())]):
                            with mock.patch("timecapsulesmb.cli.configure.confirm", return_value=False):
                                with mock.patch("timecapsulesmb.cli.configure.write_env_file", side_effect=fake_write_env_file):
                                    with redirect_stdout(output):
                                        rc = configure.main([])
        self.assertEqual(rc, 0)
        self.assertEqual(fake_values["TC_HOST"], "root@10.0.0.3")
        self.assertEqual(fake_values["TC_PASSWORD"], "goodpw")
        self.assertIn("did not work", output.getvalue())

    def test_configure_reprompts_bare_ssh_target_before_password(self) -> None:
        output = io.StringIO()
        fake_values = {}
        password_prompts = 0
        prompt_values = iter([
            "10.0.0.2",
            "root@10.0.0.2",
            "goodpw",
            "bridge0",
            "Data",
            "admin",
            "TimeCapsule",
            "samba4",
            "Time Capsule Samba 4",
            "timecapsulesamba4",
        ])

        def fake_prompt(label, default, _secret):
            nonlocal password_prompts
            if label == "Time Capsule root password":
                password_prompts += 1
            if label == "mDNS device model hint":
                return default
            return next(prompt_values)

        def fake_write_env_file(_path, values):
            fake_values.update(values)

        with mock.patch("timecapsulesmb.cli.configure.parse_env_values", return_value={}):
            with mock.patch("timecapsulesmb.cli.configure.discover_time_capsule_candidates", return_value=[]):
                with mock.patch("timecapsulesmb.cli.configure.prompt", side_effect=fake_prompt):
                    with mock.patch("timecapsulesmb.cli.configure.probe_device_state", return_value=self.make_probe_state(self.make_probe_result_netbsd6())):
                        with mock.patch("timecapsulesmb.cli.configure.probe_device_state", return_value=self.make_probe_state(self.make_probe_result_netbsd6())):
                            with mock.patch("timecapsulesmb.cli.configure.write_env_file", side_effect=fake_write_env_file):
                                with redirect_stdout(output):
                                    rc = configure.main([])
        self.assertEqual(rc, 0)
        self.assertEqual(fake_values["TC_HOST"], "root@10.0.0.2")
        self.assertEqual(password_prompts, 1)
        self.assertIn("Time Capsule SSH target must include a username", output.getvalue())

    def test_configure_can_save_even_when_validation_fails(self) -> None:
        output = io.StringIO()
        fake_values = {}
        prompt_values = iter([
            "root@10.0.0.2",
            "badpw",
            "bridge0",
            "Data",
            "admin",
            "TimeCapsule",
            "samba4",
            "Time Capsule Samba 4",
            "timecapsulesamba4",
            "119",
        ])

        def fake_prompt(_label, _default, _secret):
            label = _label
            default = _default
            if label == "mDNS device model hint":
                return default
            return next(prompt_values)

        def fake_write_env_file(_path, values):
            fake_values.update(values)

        with mock.patch("timecapsulesmb.cli.configure.parse_env_values", return_value={}):
            with mock.patch("timecapsulesmb.cli.configure.discover_time_capsule_candidates", return_value=[]):
                with mock.patch("timecapsulesmb.cli.configure.prompt", side_effect=fake_prompt):
                    with mock.patch("timecapsulesmb.cli.configure.probe_device_state", return_value=self.make_probe_state(self.make_probe_result_netbsd6())):
                        with mock.patch("timecapsulesmb.cli.configure.probe_device_state", return_value=self.make_probe_state(self.make_probe_result_auth_failed())):
                            with mock.patch("timecapsulesmb.cli.configure.confirm", return_value=True):
                                with mock.patch("timecapsulesmb.cli.configure.write_env_file", side_effect=fake_write_env_file):
                                    with redirect_stdout(output):
                                        rc = configure.main([])
        self.assertEqual(rc, 0)
        self.assertEqual(fake_values["TC_HOST"], "root@10.0.0.2")
        self.assertEqual(fake_values["TC_PASSWORD"], "badpw")

    def test_configure_can_reprompt_when_ssh_is_not_reachable_yet(self) -> None:
        output = io.StringIO()
        fake_values = {}
        prompt_values = iter([
            "root@10.0.0.2",
            "badpw",
            "root@10.0.0.3",
            "goodpw",
            "bridge0",
            "Data",
            "admin",
            "TimeCapsule",
            "samba4",
            "Time Capsule Samba 4",
            "timecapsulesamba4",
            "119",
        ])

        def fake_prompt(_label, _default, _secret):
            label = _label
            default = _default
            if label == "mDNS device model hint":
                return default
            return next(prompt_values)

        def fake_write_env_file(_path, values):
            fake_values.update(values)

        with mock.patch("timecapsulesmb.cli.configure.parse_env_values", return_value={}):
            with mock.patch("timecapsulesmb.cli.configure.discover_time_capsule_candidates", return_value=[]):
                with mock.patch("timecapsulesmb.cli.configure.prompt", side_effect=fake_prompt):
                    with mock.patch("timecapsulesmb.cli.configure.probe_device_state", return_value=self.make_probe_state(self.make_probe_result_unreachable())):
                        with mock.patch("timecapsulesmb.cli.configure.confirm", side_effect=[False, True]):
                            with mock.patch("timecapsulesmb.cli.configure.write_env_file", side_effect=fake_write_env_file):
                                with redirect_stdout(output):
                                    rc = configure.main([])
        self.assertEqual(rc, 0)
        self.assertEqual(fake_values["TC_HOST"], "root@10.0.0.3")
        self.assertEqual(fake_values["TC_PASSWORD"], "goodpw")
        self.assertIn("cannot validate this password", output.getvalue())

    def test_configure_reprompts_invalid_mdns_labels(self) -> None:
        output = io.StringIO()
        fake_values = {}
        prompt_values = iter([
            "root@10.0.0.2",
            "goodpw",
            "bridge0",
            "Data",
            "admin",
            "TimeCapsule",
            "samba4",
            "Time.Capsule",
            "Time Capsule Samba 4",
            "time.capsule",
            "timecapsulesamba4",
            "119",
        ])

        def fake_prompt(_label, _default, _secret):
            label = _label
            default = _default
            if label == "mDNS device model hint":
                return default
            return next(prompt_values)

        def fake_write_env_file(_path, values):
            fake_values.update(values)

        with mock.patch("timecapsulesmb.cli.configure.parse_env_values", return_value={}):
            with mock.patch("timecapsulesmb.cli.configure.discover_time_capsule_candidates", return_value=[]):
                with mock.patch("timecapsulesmb.cli.configure.prompt", side_effect=fake_prompt):
                    with mock.patch("timecapsulesmb.cli.configure.probe_device_state", return_value=self.make_probe_state(self.make_probe_result_netbsd6())):
                        with mock.patch("timecapsulesmb.cli.configure.probe_device_state", return_value=self.make_probe_state(self.make_probe_result_netbsd6())):
                            with mock.patch("timecapsulesmb.cli.configure.write_env_file", side_effect=fake_write_env_file):
                                with redirect_stdout(output):
                                    rc = configure.main([])
        self.assertEqual(rc, 0)
        self.assertEqual(fake_values["TC_MDNS_INSTANCE_NAME"], "Time Capsule Samba 4")
        self.assertEqual(fake_values["TC_MDNS_HOST_LABEL"], "timecapsulesamba4")
        self.assertIn("mDNS SMB instance name must not contain dots.", output.getvalue())
        self.assertIn("mDNS host label must not contain dots.", output.getvalue())

    def test_configure_invalid_existing_mdns_host_label_falls_back_to_default_prompt(self) -> None:
        output = io.StringIO()
        fake_values = {}
        host_label_defaults: list[str] = []
        existing = {
            "TC_MDNS_HOST_LABEL": "time capsule",
        }
        prompt_values = iter([
            "root@10.0.0.2",
            "goodpw",
            "bridge0",
            "Data",
            "admin",
            "TimeCapsule",
            "samba4",
            "Time Capsule Samba 4",
            "timecapsulesamba4",
            "119",
        ])

        def fake_prompt(_label, default, _secret):
            if _label == "mDNS host label":
                host_label_defaults.append(default)
            return next(prompt_values)

        def fake_write_env_file(_path, values):
            fake_values.update(values)

        with mock.patch("timecapsulesmb.cli.configure.parse_env_values", return_value=existing):
            with mock.patch("timecapsulesmb.cli.configure.discover_time_capsule_candidates", return_value=[]):
                with mock.patch("timecapsulesmb.cli.configure.prompt", side_effect=fake_prompt):
                    with mock.patch("timecapsulesmb.cli.configure.probe_device_state", return_value=self.make_probe_state(self.make_probe_result_unreachable())):
                        with mock.patch("timecapsulesmb.cli.configure.confirm", return_value=True):
                            with mock.patch("timecapsulesmb.cli.configure.write_env_file", side_effect=fake_write_env_file):
                                with redirect_stdout(output):
                                    rc = configure.main([])
        self.assertEqual(rc, 0)
        self.assertEqual(host_label_defaults, ["timecapsulesamba4"])
        self.assertEqual(fake_values["TC_MDNS_HOST_LABEL"], "timecapsulesamba4")

    def test_configure_invalid_hidden_mdns_device_model_falls_back_to_inferred_value(self) -> None:
        output = io.StringIO()
        fake_values = {}
        existing = {
            "TC_MDNS_DEVICE_MODEL": "a" * 250,
        }
        prompt_values = iter([
            "root@10.0.0.2",
            "goodpw",
            "bridge0",
            "Data",
            "admin",
            "TimeCapsule",
            "samba4",
            "Time Capsule Samba 4",
            "timecapsulesamba4",
            "119",
        ])

        def fake_prompt(_label, _default, _secret):
            label = _label
            default = _default
            if label == "mDNS device model hint":
                return default
            return next(prompt_values)

        def fake_write_env_file(_path, values):
            fake_values.update(values)

        with mock.patch("timecapsulesmb.cli.configure.parse_env_values", return_value=existing):
            with mock.patch("timecapsulesmb.cli.configure.discover_time_capsule_candidates", return_value=[]):
                with mock.patch("timecapsulesmb.cli.configure.prompt", side_effect=fake_prompt):
                    with mock.patch("timecapsulesmb.cli.configure.probe_device_state", return_value=self.make_probe_state(self.make_probe_result_netbsd6())):
                        with mock.patch("timecapsulesmb.cli.configure.probe_device_state", return_value=self.make_probe_state(self.make_probe_result_netbsd6())):
                            with mock.patch("timecapsulesmb.cli.configure.write_env_file", side_effect=fake_write_env_file):
                                with redirect_stdout(output):
                                    rc = configure.main([])
        self.assertEqual(rc, 0)
        self.assertEqual(fake_values["TC_MDNS_DEVICE_MODEL"], "TimeCapsule8,119")

    def test_configure_uses_prompted_syap_to_fill_hidden_mdns_device_model_when_undetected(self) -> None:
        output = io.StringIO()
        fake_values = {}
        prompt_values = iter([
            "root@10.0.0.2",
            "goodpw",
            "bridge0",
            "Data",
            "admin",
            "TimeCapsule",
            "samba4",
            "Time Capsule Samba 4",
            "timecapsulesamba4",
            "116",
        ])

        def fake_prompt(_label, _default, _secret):
            label = _label
            default = _default
            if label == "mDNS device model hint":
                return default
            return next(prompt_values)

        def fake_write_env_file(_path, values):
            fake_values.update(values)

        with mock.patch("timecapsulesmb.cli.configure.parse_env_values", return_value={}):
            with mock.patch("timecapsulesmb.cli.configure.discover_time_capsule_candidates", return_value=[]):
                with mock.patch("timecapsulesmb.cli.configure.prompt", side_effect=fake_prompt):
                    with mock.patch("timecapsulesmb.cli.configure.probe_device_state", return_value=self.make_probe_state(self.make_probe_result_unreachable())):
                        with mock.patch("timecapsulesmb.cli.configure.confirm", return_value=True):
                            with mock.patch("timecapsulesmb.cli.configure.write_env_file", side_effect=fake_write_env_file):
                                with redirect_stdout(output):
                                    rc = configure.main([])
        self.assertEqual(rc, 0)
        self.assertEqual(fake_values["TC_AIRPORT_SYAP"], "116")
        self.assertEqual(fake_values["TC_MDNS_DEVICE_MODEL"], "TimeCapsule6,116")

    def test_configure_prompted_syap_autofills_mdns_device_model_from_lookup(self) -> None:
        output = io.StringIO()
        fake_values = {}
        prompt_values = iter([
            "root@10.0.0.2",
            "goodpw",
            "bridge0",
            "Data",
            "admin",
            "TimeCapsule",
            "samba4",
            "Time Capsule Samba 4",
            "timecapsulesamba4",
            "116",
        ])
        seen_defaults = {}

        def fake_prompt(label, default, _secret):
            seen_defaults[label] = default
            if label == "mDNS device model hint":
                return default
            return next(prompt_values)

        def fake_write_env_file(_path, values):
            fake_values.update(values)

        with mock.patch("timecapsulesmb.cli.configure.parse_env_values", return_value={}):
            with mock.patch("timecapsulesmb.cli.configure.discover_time_capsule_candidates", return_value=[]):
                with mock.patch("timecapsulesmb.cli.configure.prompt", side_effect=fake_prompt):
                    with mock.patch("timecapsulesmb.cli.configure.probe_device_state", return_value=self.make_probe_state(self.make_probe_result_unreachable())):
                        with mock.patch("timecapsulesmb.cli.configure.confirm", return_value=True):
                            with mock.patch("timecapsulesmb.cli.configure.write_env_file", side_effect=fake_write_env_file):
                                with redirect_stdout(output):
                                    rc = configure.main([])
        self.assertEqual(rc, 0)
        self.assertEqual(fake_values["TC_AIRPORT_SYAP"], "116")
        self.assertNotIn("mDNS device model hint", seen_defaults)
        self.assertEqual(fake_values["TC_MDNS_DEVICE_MODEL"], "TimeCapsule6,116")

    def test_configure_reprompts_invalid_share_name(self) -> None:
        output = io.StringIO()
        fake_values = {}
        share_defaults: list[str] = []
        prompt_values = iter([
            "root@10.0.0.2",
            "goodpw",
            "bridge0",
            "a" * 237,
            "Data",
            "admin",
            "TimeCapsule",
            "samba4",
            "Time Capsule Samba 4",
            "timecapsulesamba4",
            "119",
        ])

        def fake_prompt(_label, _default, _secret):
            label = _label
            default = _default
            if label == "SMB share name":
                share_defaults.append(default)
            if label == "mDNS device model hint":
                return default
            return next(prompt_values)

        def fake_write_env_file(_path, values):
            fake_values.update(values)

        with mock.patch("timecapsulesmb.cli.configure.parse_env_values", return_value={}):
            with mock.patch("timecapsulesmb.cli.configure.discover_time_capsule_candidates", return_value=[]):
                with mock.patch("timecapsulesmb.cli.configure.prompt", side_effect=fake_prompt):
                    with mock.patch("timecapsulesmb.cli.configure.probe_device_state", return_value=self.make_probe_state(self.make_probe_result_netbsd6())):
                        with mock.patch("timecapsulesmb.cli.configure.probe_device_state", return_value=self.make_probe_state(self.make_probe_result_netbsd6())):
                            with mock.patch("timecapsulesmb.cli.configure.write_env_file", side_effect=fake_write_env_file):
                                with redirect_stdout(output):
                                    rc = configure.main([])
        self.assertEqual(rc, 0)
        self.assertEqual(fake_values["TC_SHARE_NAME"], "Data")
        self.assertEqual(share_defaults, ["Data", "Data"])
        self.assertIn("SMB share name must be 192 bytes or fewer.", output.getvalue())
        self.assertNotIn("Found saved value:", output.getvalue())

    def test_configure_invalid_existing_share_name_falls_back_to_default_prompt(self) -> None:
        output = io.StringIO()
        fake_values = {}
        share_defaults: list[str] = []
        existing = {
            "TC_SHARE_NAME": "daara/..",
        }
        prompt_values = iter([
            "root@10.0.0.2",
            "goodpw",
            "bridge0",
            "Data",
            "admin",
            "TimeCapsule",
            "samba4",
            "Time Capsule Samba 4",
            "timecapsulesamba4",
            "119",
        ])

        def fake_prompt(_label, default, _secret):
            if _label == "SMB share name":
                share_defaults.append(default)
            return next(prompt_values)

        def fake_write_env_file(_path, values):
            fake_values.update(values)

        with mock.patch("timecapsulesmb.cli.configure.parse_env_values", return_value=existing):
            with mock.patch("timecapsulesmb.cli.configure.discover_time_capsule_candidates", return_value=[]):
                with mock.patch("timecapsulesmb.cli.configure.prompt", side_effect=fake_prompt):
                    with mock.patch("timecapsulesmb.cli.configure.probe_device_state", return_value=self.make_probe_state(self.make_probe_result_unreachable())):
                        with mock.patch("timecapsulesmb.cli.configure.confirm", return_value=True):
                            with mock.patch("timecapsulesmb.cli.configure.write_env_file", side_effect=fake_write_env_file):
                                with redirect_stdout(output):
                                    rc = configure.main([])
        self.assertEqual(rc, 0)
        self.assertEqual(share_defaults, ["Data"])
        self.assertEqual(fake_values["TC_SHARE_NAME"], "Data")
        self.assertNotIn("Found saved value: daara/..", output.getvalue())

    def test_configure_reprompts_invalid_netbios_name(self) -> None:
        output = io.StringIO()
        fake_values = {}
        prompt_values = iter([
            "root@10.0.0.2",
            "goodpw",
            "bridge0",
            "Data",
            "admin",
            "ABCDEFGHIJKLMNOP",
            "TimeCapsule",
            "samba4",
            "Time Capsule Samba 4",
            "timecapsulesamba4",
            "119",
        ])

        def fake_prompt(_label, _default, _secret):
            if _label == "mDNS device model hint":
                return _default
            return next(prompt_values)

        def fake_write_env_file(_path, values):
            fake_values.update(values)

        with mock.patch("timecapsulesmb.cli.configure.parse_env_values", return_value={}):
            with mock.patch("timecapsulesmb.cli.configure.discover_time_capsule_candidates", return_value=[]):
                with mock.patch("timecapsulesmb.cli.configure.prompt", side_effect=fake_prompt):
                    with mock.patch("timecapsulesmb.cli.configure.probe_device_state", return_value=self.make_probe_state(self.make_probe_result_netbsd6())):
                        with mock.patch("timecapsulesmb.cli.configure.probe_device_state", return_value=self.make_probe_state(self.make_probe_result_netbsd6())):
                            with mock.patch("timecapsulesmb.cli.configure.write_env_file", side_effect=fake_write_env_file):
                                with redirect_stdout(output):
                                    rc = configure.main([])
        self.assertEqual(rc, 0)
        self.assertEqual(fake_values["TC_NETBIOS_NAME"], "TimeCapsule")
        self.assertIn("Samba NetBIOS name must be 15 bytes or fewer.", output.getvalue())

    def test_configure_invalid_existing_netbios_name_falls_back_to_default_prompt(self) -> None:
        output = io.StringIO()
        fake_values = {}
        netbios_defaults: list[str] = []
        existing = {
            "TC_NETBIOS_NAME": "ABCDEFGHIJKLMNOP",
        }
        prompt_values = iter([
            "root@10.0.0.2",
            "goodpw",
            "bridge0",
            "Data",
            "admin",
            "TimeCapsule",
            "samba4",
            "Time Capsule Samba 4",
            "timecapsulesamba4",
            "119",
        ])

        def fake_prompt(_label, default, _secret):
            if _label == "Samba NetBIOS name":
                netbios_defaults.append(default)
            return next(prompt_values)

        def fake_write_env_file(_path, values):
            fake_values.update(values)

        with mock.patch("timecapsulesmb.cli.configure.parse_env_values", return_value=existing):
            with mock.patch("timecapsulesmb.cli.configure.discover_time_capsule_candidates", return_value=[]):
                with mock.patch("timecapsulesmb.cli.configure.prompt", side_effect=fake_prompt):
                    with mock.patch("timecapsulesmb.cli.configure.probe_device_state", return_value=self.make_probe_state(self.make_probe_result_unreachable())):
                        with mock.patch("timecapsulesmb.cli.configure.confirm", return_value=True):
                            with mock.patch("timecapsulesmb.cli.configure.write_env_file", side_effect=fake_write_env_file):
                                with redirect_stdout(output):
                                    rc = configure.main([])
        self.assertEqual(rc, 0)
        self.assertEqual(netbios_defaults, ["TimeCapsule"])
        self.assertEqual(fake_values["TC_NETBIOS_NAME"], "TimeCapsule")
        self.assertNotIn("Found saved value: ABCDEFGHIJKLMNOP", output.getvalue())

    def test_doctor_returns_failure_when_checks_fatal(self) -> None:
        output = io.StringIO()
        fake_result = doctor.CheckResult("FAIL", "broken")
        with mock.patch("timecapsulesmb.cli.doctor.load_env_values", return_value={}):
            with mock.patch("timecapsulesmb.cli.doctor.run_doctor_checks", return_value=([fake_result], True)):
                with redirect_stdout(output):
                    rc = doctor.main([])
        self.assertEqual(rc, 1)
        self.assertIn("doctor found one or more fatal problems", output.getvalue())
        self.assertIn("Doctor failures:", self._telemetry_client.emit.call_args_list[-1].kwargs["error"] if self._telemetry_client.emit.call_args_list else "")

    def test_doctor_includes_soft_preinspection_error_in_failure_telemetry(self) -> None:
        output = io.StringIO()
        values = self.make_valid_env()
        fake_result = doctor.CheckResult("FAIL", "SSH command works failed")
        with tempfile.NamedTemporaryFile() as env_file:
            env_path = Path(env_file.name)
            with mock.patch("timecapsulesmb.cli.doctor.ENV_PATH", env_path):
                with mock.patch("timecapsulesmb.cli.doctor.load_env_values", return_value=values):
                    with mock.patch("timecapsulesmb.cli.doctor.inspect_managed_connection", side_effect=SystemExit("Connecting to the device failed, SSH error: bind failed")):
                        with mock.patch("timecapsulesmb.cli.doctor.run_doctor_checks", return_value=([fake_result], True)):
                            with redirect_stdout(output):
                                rc = doctor.main([])
        self.assertEqual(rc, 1)
        telemetry_error = self._telemetry_client.emit.call_args_list[-1].kwargs["error"]
        self.assertIn("Doctor failures:", telemetry_error)
        self.assertIn("preflight_error=doctor pre-inspection failed: Connecting to the device failed, SSH error: bind failed", telemetry_error)

    def test_doctor_streams_results_in_human_mode(self) -> None:
        output = io.StringIO()
        streamed_result = doctor.CheckResult("PASS", "streamed")

        def fake_run_doctor_checks(*_args, **kwargs):
            kwargs["on_result"](streamed_result)
            return ([streamed_result], False)

        with mock.patch("timecapsulesmb.cli.doctor.load_env_values", return_value={}):
            with mock.patch("timecapsulesmb.cli.doctor.run_doctor_checks", side_effect=fake_run_doctor_checks):
                with redirect_stdout(output):
                    rc = doctor.main([])
        self.assertEqual(rc, 0)
        self.assertIn("PASS streamed", output.getvalue())

    def test_doctor_streams_info_results_in_human_mode(self) -> None:
        output = io.StringIO()
        streamed_result = doctor.CheckResult("INFO", "advertised Bonjour instance: Home-Samba")

        def fake_run_doctor_checks(*_args, **kwargs):
            kwargs["on_result"](streamed_result)
            return ([streamed_result], False)

        with mock.patch("timecapsulesmb.cli.doctor.load_env_values", return_value={}):
            with mock.patch("timecapsulesmb.cli.doctor.run_doctor_checks", side_effect=fake_run_doctor_checks):
                with redirect_stdout(output):
                    rc = doctor.main([])
        self.assertEqual(rc, 0)
        self.assertIn("INFO advertised Bonjour instance: Home-Samba", output.getvalue())

    def test_deploy_dry_run_prints_target_host(self) -> None:
        output = io.StringIO()
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_SHARE_NAME": "Data",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_NET_IFACE": "bridge0",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule8,119",
            "TC_AIRPORT_SYAP": "119",
            "TC_SAMBA_USER": "admin",
        }
        with mock.patch("timecapsulesmb.cli.deploy.load_env_values", return_value=values):
            with mock.patch("timecapsulesmb.cli.deploy.validate_artifacts", return_value=[("smbd", True, "ok"), ("mdns", True, "ok")]):
                with mock.patch("timecapsulesmb.cli.deploy.discover_volume_root", return_value="/Volumes/dk2"):
                    with mock.patch("timecapsulesmb.cli.context.CommandContext.require_compatibility", return_value=self.make_supported_compatibility()):
                        with mock.patch("timecapsulesmb.cli.deploy.run_remote_actions") as actions_mock:
                            with mock.patch("timecapsulesmb.cli.deploy.upload_deployment_payload") as upload_mock:
                                with mock.patch("timecapsulesmb.cli.deploy.remote_install_auth_files") as auth_mock:
                                    with redirect_stdout(output):
                                        rc = deploy.main(["--dry-run"])
        self.assertEqual(rc, 0)
        text = output.getvalue()
        self.assertIn("Dry run: deployment plan", text)
        self.assertIn("host: root@10.0.0.2", text)
        self.assertIn("volume root: /Volumes/dk2", text)
        self.assertIn("generated smbpasswd", text)
        self.assertIn("SSH goes down after reboot request", text)
        self.assertIn("SSH returns after reboot", text)
        self.assertIn("managed smbd becomes ready", text)
        self.assertIn("managed mDNS takeover becomes ready", text)
        self.assertIn("Bonjour _smb._tcp browse/resolve", text)
        self.assertIn("authenticated SMB listing", text)
        actions_mock.assert_not_called()
        upload_mock.assert_not_called()
        auth_mock.assert_not_called()

    def test_deploy_dry_run_no_reboot_matches_no_reboot_execution_path(self) -> None:
        output = io.StringIO()
        values = self.make_valid_env()
        with mock.patch("timecapsulesmb.cli.deploy.load_env_values", return_value=values):
            with mock.patch("timecapsulesmb.cli.deploy.validate_artifacts", return_value=[("smbd", True, "ok"), ("mdns", True, "ok"), ("nbns", True, "ok")]):
                with mock.patch("timecapsulesmb.cli.deploy.discover_volume_root", return_value="/Volumes/dk2"):
                    with mock.patch("timecapsulesmb.cli.context.CommandContext.require_compatibility", return_value=self.make_supported_compatibility()):
                        with redirect_stdout(output):
                            rc = deploy.main(["--dry-run", "--no-reboot"])
        self.assertEqual(rc, 0)
        text = output.getvalue()
        self.assertIn("Reboot:\n  no", text)
        self.assertIn("Post-deploy checks:\n  none", text)
        self.assertNotIn("SSH returns after reboot", text)
        self.assertNotIn("managed smbd becomes ready", text)

    def test_deploy_ensures_install_id_before_telemetry(self) -> None:
        output = io.StringIO()
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_SHARE_NAME": "Data",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_NET_IFACE": "bridge0",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule8,119",
            "TC_AIRPORT_SYAP": "119",
            "TC_SAMBA_USER": "admin",
        }
        with mock.patch("timecapsulesmb.cli.deploy.ensure_install_id") as ensure_mock:
            with mock.patch("timecapsulesmb.cli.deploy.load_env_values", return_value=values):
                with mock.patch("timecapsulesmb.cli.deploy.CommandContext", return_value=FakeCommandContext()):
                    with mock.patch("timecapsulesmb.cli.deploy.validate_artifacts", return_value=[("smbd", True, "ok"), ("mdns", True, "ok")]):
                        with mock.patch("timecapsulesmb.cli.deploy.discover_volume_root", return_value="/Volumes/dk2"):
                            with mock.patch("timecapsulesmb.cli.context.CommandContext.require_compatibility", return_value=self.make_supported_compatibility()):
                                with redirect_stdout(output):
                                    rc = deploy.main(["--dry-run"])
        self.assertEqual(rc, 0)
        ensure_mock.assert_called_once_with()

    def test_deploy_exits_on_artifact_validation_failure(self) -> None:
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_SHARE_NAME": "Data",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_NET_IFACE": "bridge0",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule8,119",
            "TC_AIRPORT_SYAP": "119",
            "TC_SAMBA_USER": "admin",
        }
        with mock.patch("timecapsulesmb.cli.deploy.load_env_values", return_value=values):
            with mock.patch("timecapsulesmb.cli.deploy.validate_artifacts", return_value=[("smbd", False, "checksum mismatch")]):
                with self.assertRaises(SystemExit):
                    deploy.main([])

    def test_deploy_requires_nonempty_airport_syap(self) -> None:
        for values in (
            {
                "TC_HOST": "root@10.0.0.2",
                "TC_PASSWORD": "pw",
                "TC_SSH_OPTS": "-o foo",
                "TC_PAYLOAD_DIR_NAME": "samba4",
                "TC_SHARE_NAME": "Data",
                "TC_NETBIOS_NAME": "TimeCapsule",
                "TC_NET_IFACE": "bridge0",
                "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
                "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
                "TC_MDNS_DEVICE_MODEL": "TimeCapsule",
                "TC_SAMBA_USER": "admin",
            },
            {
                "TC_HOST": "root@10.0.0.2",
                "TC_PASSWORD": "pw",
                "TC_SSH_OPTS": "-o foo",
                "TC_PAYLOAD_DIR_NAME": "samba4",
                "TC_SHARE_NAME": "Data",
                "TC_NETBIOS_NAME": "TimeCapsule",
                "TC_NET_IFACE": "bridge0",
                "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
                "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
                "TC_MDNS_DEVICE_MODEL": "TimeCapsule",
                "TC_AIRPORT_SYAP": "",
                "TC_SAMBA_USER": "admin",
            },
        ):
            with self.assertRaises(SystemExit) as ctx:
                with mock.patch("timecapsulesmb.cli.deploy.load_env_values", return_value=values):
                    deploy.main(["--dry-run"])
            self.assertEqual(
                str(ctx.exception),
                "Missing required setting in .env: TC_AIRPORT_SYAP\n"
                "Please run the `configure` command before running `deploy`.",
            )

    def test_deploy_rejects_invalid_airport_syap(self) -> None:
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_SHARE_NAME": "Data",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_NET_IFACE": "bridge0",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule",
            "TC_AIRPORT_SYAP": "999",
            "TC_SAMBA_USER": "admin",
        }
        with self.assertRaises(SystemExit) as ctx:
            with mock.patch("timecapsulesmb.cli.deploy.load_env_values", return_value=values):
                deploy.main(["--dry-run"])
        self.assertEqual(
            str(ctx.exception),
            "TC_AIRPORT_SYAP is invalid. Run the `configure` command again.\n"
            "The configured syAP is invalid.",
        )

    def test_deploy_rejects_device_model_that_does_not_match_airport_syap(self) -> None:
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_SHARE_NAME": "Data",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_NET_IFACE": "bridge0",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule",
            "TC_AIRPORT_SYAP": "119",
            "TC_SAMBA_USER": "admin",
        }
        with self.assertRaises(SystemExit) as ctx:
            with mock.patch("timecapsulesmb.cli.deploy.load_env_values", return_value=values):
                deploy.main(["--dry-run"])
        self.assertEqual(
            str(ctx.exception),
            "TC_MDNS_DEVICE_MODEL is invalid. Run the `configure` command again.\n"
            "TC_MDNS_DEVICE_MODEL must match the configured syAP.",
        )

    def test_deploy_rejects_missing_remote_interface(self) -> None:
        values = self.make_valid_env()
        with self.assertRaises(SystemExit) as ctx:
            with mock.patch("timecapsulesmb.cli.deploy.load_env_values", return_value=values):
                with mock.patch(
                    "timecapsulesmb.cli.runtime.probe_remote_interface",
                    return_value=RemoteInterfaceProbeResult(
                        iface="bridge0",
                        exists=False,
                        detail="interface bridge0 was not found on the device",
                    ),
                ):
                    deploy.main(["--dry-run"])
        self.assertIn("TC_NET_IFACE is invalid", str(ctx.exception))
        self.assertIn("bridge0 was not found", str(ctx.exception))

    def test_deploy_no_reboot_stops_after_upload_phase(self) -> None:
        output = io.StringIO()
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_SHARE_NAME": "Data",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_NET_IFACE": "bridge0",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule8,119",
            "TC_AIRPORT_SYAP": "119",
            "TC_SAMBA_USER": "admin",
        }
        with mock.patch("timecapsulesmb.cli.deploy.load_env_values", return_value=values):
            with mock.patch("timecapsulesmb.cli.deploy.validate_artifacts", return_value=[("smbd", True, "ok"), ("mdns", True, "ok")]):
                with mock.patch("timecapsulesmb.cli.deploy.discover_volume_root", return_value="/Volumes/dk2"):
                    with mock.patch("timecapsulesmb.cli.context.CommandContext.require_compatibility", return_value=self.make_supported_compatibility()):
                        with mock.patch("timecapsulesmb.cli.deploy.run_remote_actions"):
                            with mock.patch(
                                "timecapsulesmb.cli.deploy.remote_ensure_adisk_uuid",
                                return_value="12345678-1234-1234-1234-123456789012",
                            ):
                                with mock.patch("timecapsulesmb.cli.deploy.upload_deployment_payload"):
                                    with mock.patch("timecapsulesmb.cli.deploy.remote_install_auth_files"):
                                        with mock.patch("timecapsulesmb.cli.deploy.run_ssh_conn") as run_ssh_mock:
                                            with redirect_stdout(output):
                                                rc = deploy.main(["--no-reboot"])
        self.assertEqual(rc, 0)
        run_ssh_mock.assert_not_called()
        self.assertIn("Skipping reboot.", output.getvalue())

    def test_deploy_declined_reboot_returns_without_rebooting(self) -> None:
        output = io.StringIO()
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_SHARE_NAME": "Data",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_NET_IFACE": "bridge0",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule8,119",
            "TC_AIRPORT_SYAP": "119",
            "TC_SAMBA_USER": "admin",
        }
        with mock.patch("timecapsulesmb.cli.deploy.load_env_values", return_value=values):
            with mock.patch("timecapsulesmb.cli.deploy.validate_artifacts", return_value=[("smbd", True, "ok"), ("mdns", True, "ok")]):
                with mock.patch("timecapsulesmb.cli.deploy.discover_volume_root", return_value="/Volumes/dk2"):
                    with mock.patch("timecapsulesmb.cli.context.CommandContext.require_compatibility", return_value=self.make_supported_compatibility()):
                        with mock.patch("timecapsulesmb.cli.deploy.run_remote_actions"):
                            with mock.patch(
                                "timecapsulesmb.cli.deploy.remote_ensure_adisk_uuid",
                                return_value="12345678-1234-1234-1234-123456789012",
                            ):
                                with mock.patch("timecapsulesmb.cli.deploy.upload_deployment_payload"):
                                    with mock.patch("timecapsulesmb.cli.deploy.remote_install_auth_files"):
                                        with mock.patch("builtins.input", return_value="n"):
                                            with mock.patch("timecapsulesmb.cli.deploy.run_ssh_conn") as run_ssh_mock:
                                                with redirect_stdout(output):
                                                    rc = deploy.main([])
        self.assertEqual(rc, 0)
        run_ssh_mock.assert_not_called()
        self.assertIn("Deployment complete without reboot.", output.getvalue())

    def test_deploy_reboot_timeout_returns_failure(self) -> None:
        output = io.StringIO()
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_SHARE_NAME": "Data",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_NET_IFACE": "bridge0",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule8,119",
            "TC_AIRPORT_SYAP": "119",
            "TC_SAMBA_USER": "admin",
        }
        with mock.patch("timecapsulesmb.cli.deploy.load_env_values", return_value=values):
            with mock.patch("timecapsulesmb.cli.deploy.validate_artifacts", return_value=[("smbd", True, "ok"), ("mdns", True, "ok")]):
                with mock.patch("timecapsulesmb.cli.deploy.discover_volume_root", return_value="/Volumes/dk2"):
                    with mock.patch("timecapsulesmb.cli.context.CommandContext.require_compatibility", return_value=self.make_supported_compatibility()):
                        with mock.patch("timecapsulesmb.cli.deploy.run_remote_actions"):
                            with mock.patch(
                                "timecapsulesmb.cli.deploy.remote_ensure_adisk_uuid",
                                return_value="12345678-1234-1234-1234-123456789012",
                            ):
                                with mock.patch("timecapsulesmb.cli.deploy.upload_deployment_payload"):
                                    with mock.patch("timecapsulesmb.cli.deploy.remote_install_auth_files"):
                                        with mock.patch("builtins.input", return_value="y"):
                                            with mock.patch("timecapsulesmb.cli.deploy.run_ssh_conn"):
                                                with mock.patch("timecapsulesmb.cli.deploy.wait_for_ssh_state_conn", side_effect=[True, False]) as wait_mock:
                                                    with redirect_stdout(output):
                                                        rc = deploy.main([])
        self.assertEqual(rc, 1)
        self.assertEqual(wait_mock.call_args_list[0].args[0].host, "root@10.0.0.2")
        self.assertEqual(wait_mock.call_args_list[0].kwargs, {"expected_up": False, "timeout_seconds": 60})
        self.assertEqual(wait_mock.call_args_list[1].args[0].host, "root@10.0.0.2")
        self.assertEqual(wait_mock.call_args_list[1].kwargs, {"expected_up": True, "timeout_seconds": 240})
        self.assertIn("Timed out waiting for SSH after reboot.", output.getvalue())

    def test_deploy_waits_for_managed_smbd_before_verifying(self) -> None:
        output = io.StringIO()
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_SHARE_NAME": "Data",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_NET_IFACE": "bridge0",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule8,119",
            "TC_AIRPORT_SYAP": "119",
            "TC_SAMBA_USER": "admin",
        }
        with mock.patch("timecapsulesmb.cli.deploy.load_env_values", return_value=values):
            with mock.patch("timecapsulesmb.cli.deploy.validate_artifacts", return_value=[("smbd", True, "ok"), ("mdns", True, "ok"), ("nbns", True, "ok")]):
                with mock.patch("timecapsulesmb.cli.deploy.discover_volume_root", return_value="/Volumes/dk2"):
                    with mock.patch("timecapsulesmb.cli.context.CommandContext.require_compatibility", return_value=self.make_supported_compatibility()):
                        with mock.patch("timecapsulesmb.cli.deploy.run_remote_actions"):
                            with mock.patch(
                                "timecapsulesmb.cli.deploy.remote_ensure_adisk_uuid",
                                return_value="12345678-1234-1234-1234-123456789012",
                            ):
                                with mock.patch("timecapsulesmb.cli.deploy.upload_deployment_payload"):
                                    with mock.patch("timecapsulesmb.cli.deploy.remote_install_auth_files"):
                                        with mock.patch("timecapsulesmb.cli.deploy.run_ssh_conn"):
                                            with mock.patch("timecapsulesmb.cli.deploy.wait_for_ssh_state_conn", side_effect=[True, True]):
                                                with mock.patch("timecapsulesmb.cli.deploy.wait_for_post_reboot_smbd", return_value=True) as ready_mock:
                                                    with mock.patch("timecapsulesmb.cli.deploy.wait_for_post_reboot_mdns_takeover", return_value=True) as mdns_ready_mock:
                                                        with mock.patch("timecapsulesmb.cli.deploy.verify_post_deploy") as verify_mock:
                                                            with mock.patch("builtins.input", return_value="y"):
                                                                with redirect_stdout(output):
                                                                    rc = deploy.main([])
        self.assertEqual(rc, 0)
        self.assertEqual(ready_mock.call_args.args[0].host, "root@10.0.0.2")
        self.assertEqual(mdns_ready_mock.call_args.args[0].host, "root@10.0.0.2")
        verify_mock.assert_called_once_with(values)
        text = output.getvalue()
        self.assertIn("Device is back online.", text)
        self.assertIn("Waiting for managed smbd to finish starting...", text)
        self.assertIn("Waiting for managed mDNS takeover to finish...", text)
        self.assertNotIn("Bonjour visibility", text)

    def test_deploy_returns_failure_when_managed_smbd_never_becomes_ready(self) -> None:
        output = io.StringIO()
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_SHARE_NAME": "Data",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_NET_IFACE": "bridge0",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule8,119",
            "TC_AIRPORT_SYAP": "119",
            "TC_SAMBA_USER": "admin",
        }
        with mock.patch("timecapsulesmb.cli.deploy.load_env_values", return_value=values):
            with mock.patch("timecapsulesmb.cli.deploy.validate_artifacts", return_value=[("smbd", True, "ok"), ("mdns", True, "ok"), ("nbns", True, "ok")]):
                with mock.patch("timecapsulesmb.cli.deploy.discover_volume_root", return_value="/Volumes/dk2"):
                    with mock.patch("timecapsulesmb.cli.context.CommandContext.require_compatibility", return_value=self.make_supported_compatibility()):
                        with mock.patch("timecapsulesmb.cli.deploy.run_remote_actions"):
                            with mock.patch(
                                "timecapsulesmb.cli.deploy.remote_ensure_adisk_uuid",
                                return_value="12345678-1234-1234-1234-123456789012",
                            ):
                                with mock.patch("timecapsulesmb.cli.deploy.upload_deployment_payload"):
                                    with mock.patch("timecapsulesmb.cli.deploy.remote_install_auth_files"):
                                        with mock.patch("timecapsulesmb.cli.deploy.run_ssh_conn"):
                                            with mock.patch("timecapsulesmb.cli.deploy.wait_for_ssh_state_conn", side_effect=[True, True]):
                                                with mock.patch("timecapsulesmb.cli.deploy.wait_for_post_reboot_smbd", return_value=False) as ready_mock:
                                                    with mock.patch("timecapsulesmb.cli.deploy.wait_for_post_reboot_mdns_takeover") as mdns_ready_mock:
                                                        with mock.patch("timecapsulesmb.cli.deploy.verify_post_deploy") as verify_mock:
                                                            with mock.patch("builtins.input", return_value="y"):
                                                                with redirect_stdout(output):
                                                                    rc = deploy.main([])
        self.assertEqual(rc, 1)
        self.assertEqual(ready_mock.call_args.args[0].host, "root@10.0.0.2")
        mdns_ready_mock.assert_not_called()
        verify_mock.assert_not_called()
        self.assertIn("Managed smbd did not become ready after reboot.", output.getvalue())

    def test_deploy_returns_failure_when_managed_mdns_never_becomes_ready(self) -> None:
        output = io.StringIO()
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_SHARE_NAME": "Data",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_NET_IFACE": "bridge0",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule8,119",
            "TC_AIRPORT_SYAP": "119",
            "TC_SAMBA_USER": "admin",
        }
        with mock.patch("timecapsulesmb.cli.deploy.load_env_values", return_value=values):
            with mock.patch("timecapsulesmb.cli.deploy.validate_artifacts", return_value=[("smbd", True, "ok"), ("mdns", True, "ok"), ("nbns", True, "ok")]):
                with mock.patch("timecapsulesmb.cli.deploy.discover_volume_root", return_value="/Volumes/dk2"):
                    with mock.patch("timecapsulesmb.cli.context.CommandContext.require_compatibility", return_value=self.make_supported_compatibility()):
                        with mock.patch("timecapsulesmb.cli.deploy.run_remote_actions"):
                            with mock.patch(
                                "timecapsulesmb.cli.deploy.remote_ensure_adisk_uuid",
                                return_value="12345678-1234-1234-1234-123456789012",
                            ):
                                with mock.patch("timecapsulesmb.cli.deploy.upload_deployment_payload"):
                                    with mock.patch("timecapsulesmb.cli.deploy.remote_install_auth_files"):
                                        with mock.patch("timecapsulesmb.cli.deploy.run_ssh_conn"):
                                            with mock.patch("timecapsulesmb.cli.deploy.wait_for_ssh_state_conn", side_effect=[True, True]):
                                                with mock.patch("timecapsulesmb.cli.deploy.wait_for_post_reboot_smbd", return_value=True) as ready_mock:
                                                    with mock.patch("timecapsulesmb.cli.deploy.wait_for_post_reboot_mdns_takeover", return_value=False) as mdns_ready_mock:
                                                        with mock.patch("timecapsulesmb.cli.deploy.verify_post_deploy") as verify_mock:
                                                            with mock.patch("builtins.input", return_value="y"):
                                                                with redirect_stdout(output):
                                                                    rc = deploy.main([])
        self.assertEqual(rc, 1)
        self.assertEqual(ready_mock.call_args.args[0].host, "root@10.0.0.2")
        self.assertEqual(mdns_ready_mock.call_args.args[0].host, "root@10.0.0.2")
        verify_mock.assert_not_called()
        self.assertIn("Managed mDNS did not become ready after reboot.", output.getvalue())

    def test_deploy_install_nbns_touches_marker(self) -> None:
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_SHARE_NAME": "Data",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_NET_IFACE": "bridge0",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule8,119",
            "TC_AIRPORT_SYAP": "119",
            "TC_SAMBA_USER": "admin",
        }
        with mock.patch("timecapsulesmb.cli.deploy.load_env_values", return_value=values):
            with mock.patch("timecapsulesmb.cli.deploy.validate_artifacts", return_value=[("smbd", True, "ok"), ("mdns", True, "ok"), ("nbns", True, "ok")]):
                with mock.patch("timecapsulesmb.cli.deploy.discover_volume_root", return_value="/Volumes/dk2"):
                    with mock.patch("timecapsulesmb.cli.context.CommandContext.require_compatibility", return_value=self.make_supported_compatibility()):
                        with mock.patch("timecapsulesmb.cli.deploy.run_remote_actions") as actions_mock:
                            with mock.patch("timecapsulesmb.cli.deploy.remote_ensure_adisk_uuid", return_value="12345678-1234-1234-1234-123456789012"):
                                with mock.patch("timecapsulesmb.cli.deploy.upload_deployment_payload"):
                                    with mock.patch("timecapsulesmb.cli.deploy.remote_install_auth_files"):
                                        rc = deploy.main(["--install-nbns", "--no-reboot"])
        self.assertEqual(rc, 0)
        self.assertEqual(actions_mock.call_count, 2)
        pre_upload_action_kinds = [action.kind for action in actions_mock.call_args_list[0].args[1]]
        self.assertEqual(
            pre_upload_action_kinds,
            ["stop_process_full", "stop_process", "stop_process", "stop_process", "initialize_data_root", "prepare_dirs", "enable_nbns"],
        )

    def test_deploy_dry_run_includes_nbns_upload_without_marker_by_default(self) -> None:
        output = io.StringIO()
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_SHARE_NAME": "Data",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_NET_IFACE": "bridge0",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule8,119",
            "TC_AIRPORT_SYAP": "119",
            "TC_SAMBA_USER": "admin",
        }
        with mock.patch("timecapsulesmb.cli.deploy.load_env_values", return_value=values):
            with mock.patch("timecapsulesmb.cli.deploy.validate_artifacts", return_value=[("smbd", True, "ok"), ("mdns", True, "ok"), ("nbns", True, "ok")]):
                with mock.patch("timecapsulesmb.cli.deploy.discover_volume_root", return_value="/Volumes/dk2"):
                    with mock.patch("timecapsulesmb.cli.context.CommandContext.require_compatibility", return_value=self.make_supported_compatibility()):
                        with redirect_stdout(output):
                            rc = deploy.main(["--dry-run"])
        self.assertEqual(rc, 0)
        text = output.getvalue()
        payload_dir = f"/Volumes/dk2/{values['TC_PAYLOAD_DIR_NAME']}"
        self.assertIn(f"bin/nbns/nbns-advertiser -> {payload_dir}/nbns-advertiser", text)
        self.assertNotIn(f"generated nbns marker -> {payload_dir}/private/nbns.enabled", text)

    def test_deploy_dry_run_uses_netbsd4_artifact_set_for_netbsd4_device(self) -> None:
        output = io.StringIO()
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_SHARE_NAME": "Data",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_NET_IFACE": "bridge0",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule8,119",
            "TC_AIRPORT_SYAP": "119",
            "TC_SAMBA_USER": "admin",
        }
        with mock.patch("timecapsulesmb.cli.deploy.load_env_values", return_value=values):
            with mock.patch("timecapsulesmb.cli.deploy.validate_artifacts", return_value=[("smbd-netbsd4le", True, "ok")]):
                with mock.patch("timecapsulesmb.cli.deploy.discover_volume_root", return_value="/Volumes/dk2"):
                    with mock.patch("timecapsulesmb.cli.context.CommandContext.require_compatibility", return_value=self.make_supported_netbsd4_compatibility()):
                        with redirect_stdout(output):
                            rc = deploy.main(["--dry-run"])
        self.assertEqual(rc, 0)
        text = output.getvalue()
        payload_dir = f"/Volumes/dk2/{values['TC_PAYLOAD_DIR_NAME']}"
        self.assertIn("Detected supported device: NetBSD 4.0 (earmv4, little-endian).", text)
        self.assertIn("Using NetBSD 4 little-endian payload.", text)
        self.assertIn(f"bin/samba4-netbsd4le/smbd -> {payload_dir}/smbd", text)
        self.assertIn(f"bin/mdns-netbsd4le/mdns-advertiser -> {payload_dir}/mdns-advertiser", text)
        self.assertIn("bin/mdns-netbsd4le/mdns-advertiser -> /mnt/Flash/mdns-advertiser", text)
        self.assertIn(f"bin/nbns-netbsd4le/nbns-advertiser -> {payload_dir}/nbns-advertiser", text)
        self.assertIn("Remote actions (NetBSD4 activation):", text)
        self.assertIn("pkill -f '[w]atchdog.sh' >/dev/null 2>&1 || true", text)
        self.assertIn("pkill smbd >/dev/null 2>&1 || true", text)
        self.assertIn("pkill mdns-advertiser >/dev/null 2>&1 || true", text)
        self.assertIn("pkill nbns-advertiser >/dev/null 2>&1 || true", text)
        self.assertIn("pkill wcifsfs >/dev/null 2>&1 || true", text)
        self.assertIn("/bin/sh /mnt/Flash/rc.local", text)
        self.assertIn("Deploy will activate Samba immediately without rebooting.", text)
        self.assertIn("NetBSD 4 devices cannot auto-run Samba after a reboot.", text)
        self.assertIn("Run `activate` after a reboot if the device did not auto-start Samba.", text)

    def test_deploy_netbsd4_prompt_decline_cancels_before_remote_actions(self) -> None:
        output = io.StringIO()
        command_context = FakeCommandContext(compatibility=self.make_supported_netbsd4_compatibility())
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_SHARE_NAME": "Data",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_NET_IFACE": "bridge0",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule8,119",
            "TC_AIRPORT_SYAP": "119",
            "TC_SAMBA_USER": "admin",
        }
        with mock.patch("timecapsulesmb.cli.deploy.load_env_values", return_value=values):
            with mock.patch("timecapsulesmb.cli.deploy.validate_artifacts", return_value=[("smbd-netbsd4le", True, "ok")]):
                with mock.patch("timecapsulesmb.cli.deploy.discover_volume_root", return_value="/Volumes/dk2"):
                    with mock.patch("timecapsulesmb.cli.context.CommandContext.require_compatibility", return_value=self.make_supported_netbsd4_compatibility()):
                        with mock.patch("builtins.input", return_value="n"):
                            with mock.patch("timecapsulesmb.cli.deploy.run_remote_actions") as actions_mock:
                                with mock.patch("timecapsulesmb.cli.deploy.CommandContext", return_value=command_context):
                                    with redirect_stdout(output):
                                        rc = deploy.main([])
        self.assertEqual(rc, 0)
        actions_mock.assert_not_called()
        self.assertIn("Deployment cancelled.", output.getvalue())
        command_context.finish.assert_called_once()
        self.assertEqual(command_context.finish.call_args.kwargs["result"], "cancelled")
        self.assertIn("Cancelled by user at NetBSD4 deploy confirmation prompt.", command_context.finish.call_args.kwargs["error"])

    def test_deploy_netbsd4_prompt_accepts_uppercase_yes(self) -> None:
        output = io.StringIO()
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_SHARE_NAME": "Data",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_NET_IFACE": "bridge0",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule8,119",
            "TC_AIRPORT_SYAP": "119",
            "TC_SAMBA_USER": "admin",
        }
        with mock.patch("timecapsulesmb.cli.deploy.load_env_values", return_value=values):
            with mock.patch("timecapsulesmb.cli.deploy.validate_artifacts", return_value=[("smbd-netbsd4le", True, "ok")]):
                with mock.patch("timecapsulesmb.cli.deploy.discover_volume_root", return_value="/Volumes/dk2"):
                    with mock.patch("timecapsulesmb.cli.context.CommandContext.require_compatibility", return_value=self.make_supported_netbsd4_compatibility()):
                        with mock.patch("builtins.input", return_value="YES"):
                            with mock.patch("timecapsulesmb.cli.deploy.run_remote_actions") as actions_mock:
                                with mock.patch("timecapsulesmb.cli.deploy.remote_ensure_adisk_uuid", return_value=""):
                                    with mock.patch("timecapsulesmb.cli.deploy.upload_deployment_payload"):
                                        with mock.patch("timecapsulesmb.cli.deploy.remote_install_auth_files"):
                                            with mock.patch("timecapsulesmb.cli.deploy.verify_netbsd4_activation", return_value=True):
                                                with redirect_stdout(output):
                                                    rc = deploy.main([])
        self.assertEqual(rc, 0)
        self.assertEqual(actions_mock.call_count, 3)
        self.assertIn("Run `activate` after a reboot if the device did not auto-start Samba.", output.getvalue())

    def test_deploy_renders_templates_with_netbsd4_payload_family(self) -> None:
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_SHARE_NAME": "Data",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_NET_IFACE": "bridge0",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule8,119",
            "TC_AIRPORT_SYAP": "119",
            "TC_SAMBA_USER": "admin",
        }
        template_bundle = mock.Mock(
            start_script_replacements={},
            watchdog_replacements={},
            smbconf_replacements={},
        )
        with mock.patch("timecapsulesmb.cli.deploy.load_env_values", return_value=values):
            with mock.patch("timecapsulesmb.cli.deploy.validate_artifacts", return_value=[("smbd-netbsd4le", True, "ok")]):
                with mock.patch("timecapsulesmb.cli.deploy.discover_volume_root", return_value="/Volumes/dk2"):
                    with mock.patch("timecapsulesmb.cli.context.CommandContext.require_compatibility", return_value=self.make_supported_netbsd4_compatibility()):
                        with mock.patch("timecapsulesmb.cli.deploy.run_remote_actions"):
                            with mock.patch("timecapsulesmb.cli.deploy.remote_ensure_adisk_uuid", return_value=""):
                                with mock.patch("timecapsulesmb.cli.deploy.build_template_bundle", return_value=template_bundle) as template_mock:
                                    with mock.patch("timecapsulesmb.cli.deploy.upload_deployment_payload"):
                                        with mock.patch("timecapsulesmb.cli.deploy.remote_install_auth_files"):
                                            with mock.patch("timecapsulesmb.cli.deploy.verify_netbsd4_activation", return_value=True):
                                                rc = deploy.main(["--yes", "--no-reboot"])
        self.assertEqual(rc, 0)
        template_mock.assert_called_once_with(
            values,
            adisk_disk_key="dk2",
            adisk_uuid="",
            payload_family="netbsd4le_samba4",
            debug_logging=False,
            data_root="/Volumes/dk2/ShareRoot",
        )

    def test_deploy_debug_logging_renders_disk_logging_template(self) -> None:
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_SHARE_NAME": "Data",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_NET_IFACE": "bridge0",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule8,119",
            "TC_AIRPORT_SYAP": "119",
            "TC_SAMBA_USER": "admin",
        }
        template_bundle = mock.Mock(
            start_script_replacements={},
            watchdog_replacements={},
            smbconf_replacements={},
        )
        with mock.patch("timecapsulesmb.cli.deploy.load_env_values", return_value=values):
            with mock.patch("timecapsulesmb.cli.deploy.validate_artifacts", return_value=[("smbd", True, "ok")]):
                with mock.patch("timecapsulesmb.cli.deploy.discover_volume_root", return_value="/Volumes/dk2"):
                    with mock.patch("timecapsulesmb.cli.context.CommandContext.require_compatibility", return_value=self.make_supported_compatibility()):
                        with mock.patch("timecapsulesmb.cli.deploy.run_remote_actions") as actions_mock:
                            with mock.patch("timecapsulesmb.cli.deploy.remote_ensure_adisk_uuid", return_value=""):
                                with mock.patch("timecapsulesmb.cli.deploy.build_template_bundle", return_value=template_bundle) as template_mock:
                                    with mock.patch("timecapsulesmb.cli.deploy.upload_deployment_payload"):
                                        with mock.patch("timecapsulesmb.cli.deploy.remote_install_auth_files"):
                                            rc = deploy.main(["--yes", "--no-reboot", "--debug-logging"])
        self.assertEqual(rc, 0)
        template_mock.assert_called_once_with(
            values,
            adisk_disk_key="dk2",
            adisk_uuid="",
            payload_family="netbsd6_samba4",
            debug_logging=True,
            data_root="/Volumes/dk2/ShareRoot",
        )
        rendered_actions = [action.kind for action in actions_mock.call_args_list[0].args[1]]
        self.assertNotIn("prepare_log_dir", rendered_actions)

    def test_deploy_netbsd4_yes_runs_activation_and_skips_reboot(self) -> None:
        output = io.StringIO()
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_SHARE_NAME": "Data",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_NET_IFACE": "bridge0",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule8,119",
            "TC_AIRPORT_SYAP": "119",
            "TC_SAMBA_USER": "admin",
        }
        with mock.patch("timecapsulesmb.cli.deploy.load_env_values", return_value=values):
            with mock.patch("timecapsulesmb.cli.deploy.validate_artifacts", return_value=[("smbd-netbsd4le", True, "ok")]):
                with mock.patch("timecapsulesmb.cli.deploy.discover_volume_root", return_value="/Volumes/dk2"):
                    with mock.patch("timecapsulesmb.cli.context.CommandContext.require_compatibility", return_value=self.make_supported_netbsd4_compatibility()):
                        with mock.patch("timecapsulesmb.cli.deploy.run_remote_actions") as actions_mock:
                            with mock.patch("timecapsulesmb.cli.deploy.remote_ensure_adisk_uuid", return_value=""):
                                with mock.patch("timecapsulesmb.cli.deploy.upload_deployment_payload"):
                                    with mock.patch("timecapsulesmb.cli.deploy.remote_install_auth_files"):
                                        with mock.patch("timecapsulesmb.cli.deploy.verify_netbsd4_activation", return_value=True) as verify_mock:
                                            with mock.patch("timecapsulesmb.cli.deploy.run_ssh_conn") as run_ssh_mock:
                                                with redirect_stdout(output):
                                                    rc = deploy.main(["--yes"])
        self.assertEqual(rc, 0)
        self.assertEqual(actions_mock.call_count, 3)
        activation_action_kinds = [action.kind for action in actions_mock.call_args_list[2].args[1]]
        activation_action_args = [action.args[0] for action in actions_mock.call_args_list[2].args[1]]
        self.assertEqual(
            activation_action_kinds,
            ["stop_process_full", "stop_process", "stop_process", "stop_process", "stop_process", "run_script"],
        )
        self.assertEqual(
            activation_action_args,
            ["[w]atchdog.sh", "smbd", "mdns-advertiser", "nbns-advertiser", "wcifsfs", "/mnt/Flash/rc.local"],
        )
        self.assertEqual(actions_mock.call_args_list[2].kwargs, {})
        self.assertEqual(verify_mock.call_args.args[0].host, "root@10.0.0.2")
        run_ssh_mock.assert_not_called()
        self.assertIn("Activating NetBSD4 payload without reboot.", output.getvalue())

    def test_deploy_netbsd4_no_reboot_still_runs_activation(self) -> None:
        output = io.StringIO()
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_SHARE_NAME": "Data",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_NET_IFACE": "bridge0",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule8,119",
            "TC_AIRPORT_SYAP": "119",
            "TC_SAMBA_USER": "admin",
        }
        with mock.patch("timecapsulesmb.cli.deploy.load_env_values", return_value=values):
            with mock.patch("timecapsulesmb.cli.deploy.validate_artifacts", return_value=[("smbd-netbsd4le", True, "ok")]):
                with mock.patch("timecapsulesmb.cli.deploy.discover_volume_root", return_value="/Volumes/dk2"):
                    with mock.patch("timecapsulesmb.cli.context.CommandContext.require_compatibility", return_value=self.make_supported_netbsd4_compatibility()):
                        with mock.patch("timecapsulesmb.cli.deploy.run_remote_actions") as actions_mock:
                            with mock.patch("timecapsulesmb.cli.deploy.remote_ensure_adisk_uuid", return_value=""):
                                with mock.patch("timecapsulesmb.cli.deploy.upload_deployment_payload"):
                                    with mock.patch("timecapsulesmb.cli.deploy.remote_install_auth_files"):
                                        with mock.patch("timecapsulesmb.cli.deploy.verify_netbsd4_activation", return_value=True):
                                            with mock.patch("timecapsulesmb.cli.deploy.run_ssh_conn") as run_ssh_mock:
                                                with redirect_stdout(output):
                                                    rc = deploy.main(["--yes", "--no-reboot"])
        self.assertEqual(rc, 0)
        self.assertEqual(actions_mock.call_count, 3)
        run_ssh_mock.assert_not_called()
        self.assertNotIn("Skipping reboot.", output.getvalue())
        self.assertIn("Activating NetBSD4 payload without reboot.", output.getvalue())

    def test_deploy_netbsd4_activation_failure_returns_nonzero(self) -> None:
        output = io.StringIO()
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_SHARE_NAME": "Data",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_NET_IFACE": "bridge0",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule8,119",
            "TC_AIRPORT_SYAP": "119",
            "TC_SAMBA_USER": "admin",
        }
        with mock.patch("timecapsulesmb.cli.deploy.load_env_values", return_value=values):
            with mock.patch("timecapsulesmb.cli.deploy.validate_artifacts", return_value=[("smbd-netbsd4le", True, "ok")]):
                with mock.patch("timecapsulesmb.cli.deploy.discover_volume_root", return_value="/Volumes/dk2"):
                    with mock.patch("timecapsulesmb.cli.context.CommandContext.require_compatibility", return_value=self.make_supported_netbsd4_compatibility()):
                        with mock.patch("timecapsulesmb.cli.deploy.run_remote_actions"):
                            with mock.patch("timecapsulesmb.cli.deploy.remote_ensure_adisk_uuid", return_value=""):
                                with mock.patch("timecapsulesmb.cli.deploy.upload_deployment_payload"):
                                    with mock.patch("timecapsulesmb.cli.deploy.remote_install_auth_files"):
                                        with mock.patch("timecapsulesmb.cli.deploy.verify_netbsd4_activation", return_value=False):
                                            with redirect_stdout(output):
                                                rc = deploy.main(["--yes"])
        self.assertEqual(rc, 1)
        self.assertIn("NetBSD4 activation failed.", output.getvalue())

    def test_deploy_install_nbns_dry_run_mentions_marker_for_netbsd4(self) -> None:
        output = io.StringIO()
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_SHARE_NAME": "Data",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_NET_IFACE": "bridge0",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule8,119",
            "TC_AIRPORT_SYAP": "119",
            "TC_SAMBA_USER": "admin",
        }
        with mock.patch("timecapsulesmb.cli.deploy.load_env_values", return_value=values):
            with mock.patch("timecapsulesmb.cli.deploy.validate_artifacts", return_value=[("smbd-netbsd4le", True, "ok"), ("mdns-advertiser-netbsd4le", True, "ok"), ("nbns-advertiser-netbsd4le", True, "ok")]):
                with mock.patch("timecapsulesmb.cli.deploy.discover_volume_root", return_value="/Volumes/dk2"):
                    with mock.patch("timecapsulesmb.cli.context.CommandContext.require_compatibility", return_value=self.make_supported_netbsd4_compatibility()):
                        with redirect_stdout(output):
                            rc = deploy.main(["--dry-run", "--install-nbns"])
        self.assertEqual(rc, 0)
        text = output.getvalue()
        self.assertIn("bin/nbns-netbsd4le/nbns-advertiser -> /Volumes/dk2/samba4/nbns-advertiser", text)
        self.assertIn("generated nbns marker -> /Volumes/dk2/samba4/private/nbns.enabled", text)

    def test_deploy_install_nbns_dry_run_mentions_marker(self) -> None:
        output = io.StringIO()
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_SHARE_NAME": "Data",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_NET_IFACE": "bridge0",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule8,119",
            "TC_AIRPORT_SYAP": "119",
            "TC_SAMBA_USER": "admin",
        }
        with mock.patch("timecapsulesmb.cli.deploy.load_env_values", return_value=values):
            with mock.patch("timecapsulesmb.cli.deploy.validate_artifacts", return_value=[("smbd", True, "ok"), ("mdns", True, "ok"), ("nbns", True, "ok")]):
                with mock.patch("timecapsulesmb.cli.deploy.discover_volume_root", return_value="/Volumes/dk2"):
                    with mock.patch("timecapsulesmb.cli.context.CommandContext.require_compatibility", return_value=self.make_supported_compatibility()):
                        with redirect_stdout(output):
                            rc = deploy.main(["--dry-run", "--install-nbns"])
        self.assertEqual(rc, 0)
        self.assertIn("generated nbns marker -> /Volumes/dk2/samba4/private/nbns.enabled", output.getvalue())

    def test_deploy_rejects_unsupported_device(self) -> None:
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_SHARE_NAME": "Data",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_NET_IFACE": "bridge0",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule8,119",
            "TC_AIRPORT_SYAP": "119",
            "TC_SAMBA_USER": "admin",
        }
        unsupported = DeviceCompatibility(
            os_name="Linux",
            os_release="6.8",
            arch="armv7",
            elf_endianness="unknown",
            payload_family=None,
            device_generation="unknown",
            supported=False,
            reason_code="unsupported_os",
        )
        with mock.patch("timecapsulesmb.cli.deploy.load_env_values", return_value=values):
            with mock.patch("timecapsulesmb.cli.deploy.validate_artifacts", return_value=[("smbd", True, "ok"), ("mdns", True, "ok")]):
                with mock.patch("timecapsulesmb.cli.deploy.discover_volume_root", return_value="/Volumes/dk2"):
                    with mock.patch("timecapsulesmb.cli.context.CommandContext.require_compatibility", return_value=unsupported):
                        with self.assertRaises(SystemExit) as ctx:
                            deploy.main(["--dry-run"])
        self.assertIn("Linux", str(ctx.exception))

    def test_deploy_allow_unsupported_still_fails_without_payload_family(self) -> None:
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_SHARE_NAME": "Data",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_NET_IFACE": "bridge0",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule8,119",
            "TC_AIRPORT_SYAP": "119",
            "TC_SAMBA_USER": "admin",
        }
        unsupported = DeviceCompatibility(
            os_name="Linux",
            os_release="6.8",
            arch="armv7",
            elf_endianness="unknown",
            payload_family=None,
            device_generation="unknown",
            supported=False,
            reason_code="unsupported_os",
        )
        with mock.patch("timecapsulesmb.cli.deploy.load_env_values", return_value=values):
            with mock.patch("timecapsulesmb.cli.deploy.validate_artifacts", return_value=[("smbd", True, "ok"), ("mdns", True, "ok")]):
                with mock.patch("timecapsulesmb.cli.deploy.discover_volume_root", return_value="/Volumes/dk2"):
                    with mock.patch("timecapsulesmb.cli.context.CommandContext.require_compatibility", return_value=unsupported):
                        with self.assertRaises(SystemExit) as ctx:
                            deploy.main(["--dry-run", "--allow-unsupported"])
        text = str(ctx.exception)
        self.assertIn("Linux", text)
        self.assertIn("No deployable payload is available", text)

    def test_prep_device_returns_error_when_env_missing(self) -> None:
        output = io.StringIO()
        with mock.patch("timecapsulesmb.cli.prep_device.parse_env_values", return_value={}):
            with redirect_stdout(output):
                rc = prep_device.main([])
        self.assertEqual(rc, 1)
        self.assertIn("Run '.venv/bin/tcapsule configure' first", output.getvalue())

    def test_prep_device_enable_flow_succeeds(self) -> None:
        output = io.StringIO()
        values = {"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"}
        with mock.patch("timecapsulesmb.cli.prep_device.parse_env_values", return_value=values):
            with mock.patch("timecapsulesmb.cli.prep_device.tcp_open", return_value=False):
                with mock.patch("timecapsulesmb.cli.prep_device.enable_ssh") as enable_ssh_mock:
                    with mock.patch("timecapsulesmb.cli.prep_device.wait_for_ssh", return_value=True):
                        with redirect_stdout(output):
                            rc = prep_device.main([])
        self.assertEqual(rc, 0)
        enable_ssh_mock.assert_called_once()
        self.assertIn("SSH is configured", output.getvalue())

    def test_prep_device_disable_flow_warns_when_ssh_reopens(self) -> None:
        output = io.StringIO()
        values = {"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"}
        with mock.patch("timecapsulesmb.cli.prep_device.parse_env_values", return_value=values):
            with mock.patch("timecapsulesmb.cli.prep_device.tcp_open", return_value=True):
                with mock.patch("builtins.input", return_value="y"):
                    with mock.patch("timecapsulesmb.cli.prep_device.disable_ssh") as disable_ssh_mock:
                        with mock.patch("timecapsulesmb.cli.prep_device.wait_for_ssh", side_effect=[True, False]):
                            with mock.patch("timecapsulesmb.cli.prep_device.wait_for_device_up"):
                                with redirect_stdout(output):
                                    rc = prep_device.main([])
        self.assertEqual(rc, 0)
        disable_ssh_mock.assert_called_once()
        self.assertIn("Warning: SSH reopened after reboot", output.getvalue())

    def test_prep_device_disable_flow_confirms_ssh_disabled(self) -> None:
        output = io.StringIO()
        values = {"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"}
        with mock.patch("timecapsulesmb.cli.prep_device.parse_env_values", return_value=values):
            with mock.patch("timecapsulesmb.cli.prep_device.tcp_open", return_value=True):
                with mock.patch("builtins.input", return_value="y"):
                    with mock.patch("timecapsulesmb.cli.prep_device.disable_ssh"):
                        with mock.patch("timecapsulesmb.cli.prep_device.wait_for_ssh", side_effect=[True, True]):
                            with mock.patch("timecapsulesmb.cli.prep_device.wait_for_device_up"):
                                with redirect_stdout(output):
                                    rc = prep_device.main([])
        self.assertEqual(rc, 0)
        self.assertIn("SSH disabled (remains closed after reboot)", output.getvalue())

    def test_doctor_json_outputs_structured_results(self) -> None:
        output = io.StringIO()
        fake_result = doctor.CheckResult("PASS", "ok")
        with mock.patch("timecapsulesmb.cli.doctor.load_env_values", return_value={}):
            with mock.patch("timecapsulesmb.cli.doctor.run_doctor_checks", return_value=([fake_result], False)):
                with redirect_stdout(output):
                    rc = doctor.main(["--json"])
        self.assertEqual(rc, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["fatal"], False)
        self.assertEqual(payload["results"][0]["status"], "PASS")

    def test_doctor_ensures_install_id_before_telemetry(self) -> None:
        output = io.StringIO()
        fake_result = doctor.CheckResult("PASS", "ok")
        with mock.patch("timecapsulesmb.cli.doctor.ensure_install_id") as ensure_mock:
            with mock.patch("timecapsulesmb.cli.doctor.load_env_values", return_value={}):
                with mock.patch("timecapsulesmb.cli.doctor.CommandContext", return_value=FakeCommandContext()):
                    with mock.patch("timecapsulesmb.cli.doctor.run_doctor_checks", return_value=([fake_result], False)):
                        with redirect_stdout(output):
                            rc = doctor.main(["--json"])
        self.assertEqual(rc, 0)
        ensure_mock.assert_called_once_with()

    def test_deploy_dry_run_json_outputs_plan(self) -> None:
        output = io.StringIO()
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_SHARE_NAME": "Data",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_NET_IFACE": "bridge0",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule8,119",
            "TC_AIRPORT_SYAP": "119",
            "TC_SAMBA_USER": "admin",
        }
        with mock.patch("timecapsulesmb.cli.deploy.load_env_values", return_value=values):
            with mock.patch("timecapsulesmb.cli.deploy.validate_artifacts", return_value=[("smbd", True, "ok"), ("mdns", True, "ok"), ("nbns", True, "ok")]):
                with mock.patch("timecapsulesmb.cli.deploy.discover_volume_root", return_value="/Volumes/dk2"):
                    with mock.patch("timecapsulesmb.cli.context.CommandContext.require_compatibility", return_value=self.make_supported_compatibility()):
                        with redirect_stdout(output):
                            rc = deploy.main(["--dry-run", "--json"])
        self.assertEqual(rc, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["host"], "root@10.0.0.2")
        self.assertEqual(payload["volume_root"], "/Volumes/dk2")
        self.assertTrue(payload["nbns_path"].endswith("/bin/nbns/nbns-advertiser"))
        self.assertEqual(payload["payload_targets"]["nbns-advertiser"], f"/Volumes/dk2/{values['TC_PAYLOAD_DIR_NAME']}/nbns-advertiser")
        self.assertEqual(
            [check["id"] for check in payload["post_deploy_checks"]],
            [
                "ssh_goes_down_after_reboot",
                "ssh_returns_after_reboot",
                "managed_smbd_ready",
                "managed_mdns_takeover_ready",
                "bonjour_browse_resolve",
                "authenticated_smb_listing",
            ],
        )

    def test_deploy_netbsd4_dry_run_json_outputs_activation_plan(self) -> None:
        output = io.StringIO()
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_SHARE_NAME": "Data",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_NET_IFACE": "bridge0",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule8,119",
            "TC_AIRPORT_SYAP": "119",
            "TC_SAMBA_USER": "admin",
        }
        with mock.patch("timecapsulesmb.cli.deploy.load_env_values", return_value=values):
            with mock.patch("timecapsulesmb.cli.deploy.validate_artifacts", return_value=[("smbd-netbsd4le", True, "ok")]):
                with mock.patch("timecapsulesmb.cli.deploy.discover_volume_root", return_value="/Volumes/dk2"):
                    with mock.patch("timecapsulesmb.cli.context.CommandContext.require_compatibility", return_value=self.make_supported_netbsd4_compatibility()):
                        with redirect_stdout(output):
                            rc = deploy.main(["--dry-run", "--json"])
        self.assertEqual(rc, 0)
        payload = json.loads(output.getvalue())
        self.assertFalse(payload["reboot_required"])
        self.assertEqual(
            [action["kind"] for action in payload["activation_actions"]],
            ["stop_process_full", "stop_process", "stop_process", "stop_process", "stop_process", "run_script"],
        )
        self.assertEqual(
            [action["args"][0] for action in payload["activation_actions"]],
            ["[w]atchdog.sh", "smbd", "mdns-advertiser", "nbns-advertiser", "wcifsfs", "/mnt/Flash/rc.local"],
        )
        self.assertEqual(
            [check["id"] for check in payload["post_deploy_checks"]],
            [
                "netbsd4_runtime_smb_conf_present",
                "netbsd4_smbd_ready_marker_present",
                "netbsd4_smbd_bound_445",
                "netbsd4_mdns_bound_5353",
            ],
        )

    def test_activate_dry_run_prints_netbsd4_activation_plan(self) -> None:
        output = io.StringIO()
        values = self.make_valid_env()
        with mock.patch("timecapsulesmb.cli.activate.load_env_values", return_value=values):
            with mock.patch("timecapsulesmb.cli.context.CommandContext.require_compatibility", return_value=self.make_supported_netbsd4_compatibility()):
                with mock.patch("timecapsulesmb.cli.activate.run_remote_actions") as actions_mock:
                    with redirect_stdout(output):
                        rc = activate.main(["--dry-run"])
        self.assertEqual(rc, 0)
        actions_mock.assert_not_called()
        text = output.getvalue()
        self.assertIn("Dry run: NetBSD4 activation plan", text)
        self.assertIn("pkill -f '[w]atchdog.sh' >/dev/null 2>&1 || true", text)
        self.assertIn("pkill smbd >/dev/null 2>&1 || true", text)
        self.assertIn("pkill mdns-advertiser >/dev/null 2>&1 || true", text)
        self.assertIn("pkill nbns-advertiser >/dev/null 2>&1 || true", text)
        self.assertIn("pkill wcifsfs >/dev/null 2>&1 || true", text)
        self.assertIn("/bin/sh /mnt/Flash/rc.local", text)
        self.assertIn("skip rc.local if NetBSD4 payload is already healthy", text)
        self.assertIn("managed runtime smb.conf is present", text)
        self.assertIn("managed smbd ready marker is present", text)
        self.assertIn("smbd is bound to TCP 445", text)
        self.assertIn("mdns-advertiser is bound to UDP 5353", text)
        self.assertIn("This will start the deployed Samba payload on the Time Capsule.", text)
        self.assertIn("NetBSD 4 devices cannot auto-run Samba after a reboot.", text)

    def test_activate_ensures_install_id_before_telemetry(self) -> None:
        output = io.StringIO()
        values = self.make_valid_env()
        with mock.patch("timecapsulesmb.cli.activate.ensure_install_id") as ensure_mock:
            with mock.patch("timecapsulesmb.cli.activate.load_env_values", return_value=values):
                with mock.patch(
                    "timecapsulesmb.cli.activate.CommandContext",
                    return_value=FakeCommandContext(compatibility=self.make_supported_netbsd4_compatibility()),
                ):
                    with mock.patch("timecapsulesmb.cli.context.CommandContext.require_compatibility", return_value=self.make_supported_netbsd4_compatibility()):
                        with redirect_stdout(output):
                            rc = activate.main(["--dry-run"])
        self.assertEqual(rc, 0)
        ensure_mock.assert_called_once_with()

    def test_activate_rejects_non_netbsd4_device(self) -> None:
        values = self.make_valid_env()
        with mock.patch("timecapsulesmb.cli.activate.load_env_values", return_value=values):
            with mock.patch("timecapsulesmb.cli.context.CommandContext.require_compatibility", return_value=self.make_supported_compatibility()):
                with self.assertRaises(SystemExit) as cm:
                    activate.main(["--dry-run"])
        self.assertIn("only supported for NetBSD4", str(cm.exception))

    def test_activate_rejects_missing_remote_interface(self) -> None:
        values = self.make_valid_env()
        with self.assertRaises(SystemExit) as ctx:
            with mock.patch("timecapsulesmb.cli.activate.load_env_values", return_value=values):
                with mock.patch(
                    "timecapsulesmb.cli.runtime.probe_remote_interface",
                    return_value=RemoteInterfaceProbeResult(
                        iface="bridge0",
                        exists=False,
                        detail="interface bridge0 was not found on the device",
                    ),
                ):
                    activate.main(["--dry-run"])
        self.assertIn("TC_NET_IFACE is invalid", str(ctx.exception))
        self.assertIn("bridge0 was not found", str(ctx.exception))

    def test_activate_prompt_decline_cancels_before_remote_actions(self) -> None:
        output = io.StringIO()
        command_context = FakeCommandContext(compatibility=self.make_supported_netbsd4_compatibility())
        values = self.make_valid_env()
        with mock.patch("timecapsulesmb.cli.activate.load_env_values", return_value=values):
            with mock.patch("timecapsulesmb.cli.context.CommandContext.require_compatibility", return_value=self.make_supported_netbsd4_compatibility()):
                with mock.patch("builtins.input", return_value="n"):
                    with mock.patch("timecapsulesmb.cli.activate.run_remote_actions") as actions_mock:
                        with mock.patch("timecapsulesmb.cli.activate.CommandContext", return_value=command_context):
                            with redirect_stdout(output):
                                rc = activate.main([])
        self.assertEqual(rc, 0)
        actions_mock.assert_not_called()
        self.assertIn("Activation cancelled.", output.getvalue())
        command_context.finish.assert_called_once()
        self.assertEqual(command_context.finish.call_args.kwargs["result"], "cancelled")
        self.assertIn("Cancelled by user at NetBSD4 activation confirmation prompt.", command_context.finish.call_args.kwargs["error"])

    def test_activate_yes_runs_idempotent_actions_and_verifies(self) -> None:
        output = io.StringIO()
        values = self.make_valid_env()
        with mock.patch("timecapsulesmb.cli.activate.load_env_values", return_value=values):
            with mock.patch("timecapsulesmb.cli.context.CommandContext.require_compatibility", return_value=self.make_supported_netbsd4_compatibility()):
                with mock.patch("timecapsulesmb.cli.activate.netbsd4_activation_is_already_healthy", return_value=False):
                    with mock.patch("timecapsulesmb.cli.activate.run_remote_actions") as actions_mock:
                        with mock.patch("timecapsulesmb.cli.activate.verify_netbsd4_activation", return_value=True) as verify_mock:
                            with redirect_stdout(output):
                                rc = activate.main(["--yes"])
        self.assertEqual(rc, 0)
        actions_mock.assert_called_once()
        action_kinds = [action.kind for action in actions_mock.call_args.args[1]]
        action_args = [action.args[0] for action in actions_mock.call_args.args[1]]
        self.assertEqual(
            action_kinds,
            ["stop_process_full", "stop_process", "stop_process", "stop_process", "stop_process", "run_script"],
        )
        self.assertEqual(
            action_args,
            ["[w]atchdog.sh", "smbd", "mdns-advertiser", "nbns-advertiser", "wcifsfs", "/mnt/Flash/rc.local"],
        )
        self.assertEqual(actions_mock.call_args.kwargs, {})
        self.assertEqual(verify_mock.call_args.args[0].host, "root@10.0.0.2")
        self.assertIn("without file transfer", output.getvalue())

    def test_activate_skips_rc_local_when_payload_is_already_healthy(self) -> None:
        output = io.StringIO()
        values = self.make_valid_env()
        with mock.patch("timecapsulesmb.cli.activate.load_env_values", return_value=values):
            with mock.patch("timecapsulesmb.cli.context.CommandContext.require_compatibility", return_value=self.make_supported_netbsd4_compatibility()):
                with mock.patch("timecapsulesmb.cli.activate.netbsd4_activation_is_already_healthy", return_value=True):
                    with mock.patch("timecapsulesmb.cli.activate.run_remote_actions") as actions_mock:
                        with mock.patch("timecapsulesmb.cli.activate.verify_netbsd4_activation") as verify_mock:
                            with redirect_stdout(output):
                                rc = activate.main(["--yes"])
        self.assertEqual(rc, 0)
        actions_mock.assert_not_called()
        verify_mock.assert_not_called()
        self.assertIn("already active; skipping rc.local", output.getvalue())

    def test_activate_returns_nonzero_when_verification_fails(self) -> None:
        output = io.StringIO()
        values = self.make_valid_env()
        with mock.patch("timecapsulesmb.cli.activate.load_env_values", return_value=values):
            with mock.patch("timecapsulesmb.cli.context.CommandContext.require_compatibility", return_value=self.make_supported_netbsd4_compatibility()):
                with mock.patch("timecapsulesmb.cli.activate.netbsd4_activation_is_already_healthy", return_value=False):
                    with mock.patch("timecapsulesmb.cli.activate.run_remote_actions"):
                        with mock.patch("timecapsulesmb.cli.activate.verify_netbsd4_activation", return_value=False):
                            with redirect_stdout(output):
                                rc = activate.main(["--yes"])
        self.assertEqual(rc, 1)
        self.assertIn("NetBSD4 activation failed.", output.getvalue())

    def test_uninstall_dry_run_prints_target_host(self) -> None:
        output = io.StringIO()
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
            "TC_PAYLOAD_DIR_NAME": "samba4",
        }
        with mock.patch("timecapsulesmb.cli.uninstall.load_env_values", return_value=values):
            with mock.patch("timecapsulesmb.cli.uninstall.discover_volume_root", return_value="/Volumes/dk2"):
                with redirect_stdout(output):
                    rc = uninstall.main(["--dry-run"])
        self.assertEqual(rc, 0)
        text = output.getvalue()
        self.assertIn("Dry run: uninstall plan", text)
        self.assertIn("host: root@10.0.0.2", text)
        self.assertIn(f"payload dir: /Volumes/dk2/{values['TC_PAYLOAD_DIR_NAME']}", text)

    def test_uninstall_dry_run_no_reboot_matches_no_reboot_execution_path(self) -> None:
        output = io.StringIO()
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
            "TC_PAYLOAD_DIR_NAME": "samba4",
        }
        with mock.patch("timecapsulesmb.cli.uninstall.load_env_values", return_value=values):
            with mock.patch("timecapsulesmb.cli.uninstall.discover_volume_root", return_value="/Volumes/dk2"):
                with redirect_stdout(output):
                    rc = uninstall.main(["--dry-run", "--no-reboot"])
        self.assertEqual(rc, 0)
        text = output.getvalue()
        self.assertIn("Reboot:\n  no", text)
        self.assertIn("Post-uninstall checks:\n  none", text)
        self.assertNotIn("SSH returns after reboot", text)

    def test_uninstall_validates_only_host_and_payload_dir(self) -> None:
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "",
            "TC_SSH_OPTS": "-o foo",
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_MDNS_HOST_LABEL": "bad host label",
        }
        with mock.patch("timecapsulesmb.cli.uninstall.load_env_values", return_value=values):
            with mock.patch("timecapsulesmb.cli.uninstall.discover_volume_root", return_value="/Volumes/dk2"):
                with redirect_stdout(io.StringIO()):
                    rc = uninstall.main(["--dry-run"])
        self.assertEqual(rc, 0)

    def test_uninstall_rejects_unsafe_payload_dir(self) -> None:
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "",
            "TC_SSH_OPTS": "-o foo",
            "TC_PAYLOAD_DIR_NAME": "../samba4",
        }
        with self.assertRaises(SystemExit) as ctx:
            with mock.patch("timecapsulesmb.cli.uninstall.load_env_values", return_value=values):
                uninstall.main(["--dry-run"])
        self.assertIn("TC_PAYLOAD_DIR_NAME is invalid", str(ctx.exception))

    def test_uninstall_json_outputs_plan(self) -> None:
        output = io.StringIO()
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
            "TC_PAYLOAD_DIR_NAME": "samba4",
        }
        with mock.patch("timecapsulesmb.cli.uninstall.load_env_values", return_value=values):
            with mock.patch("timecapsulesmb.cli.uninstall.discover_volume_root", return_value="/Volumes/dk2"):
                with redirect_stdout(output):
                    rc = uninstall.main(["--dry-run", "--json"])
        self.assertEqual(rc, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["host"], "root@10.0.0.2")
        self.assertEqual(payload["volume_root"], "/Volumes/dk2")
        self.assertEqual(
            [check["id"] for check in payload["post_uninstall_checks"]],
            [
                "ssh_goes_down_after_reboot",
                "ssh_returns_after_reboot",
                "managed_files_absent",
            ],
        )

    def test_uninstall_yes_reboots_and_verifies(self) -> None:
        output = io.StringIO()
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
            "TC_PAYLOAD_DIR_NAME": "samba4",
        }
        with mock.patch("timecapsulesmb.cli.uninstall.load_env_values", return_value=values):
            with mock.patch("timecapsulesmb.cli.uninstall.discover_volume_root", return_value="/Volumes/dk2"):
                with mock.patch("timecapsulesmb.cli.uninstall.remote_uninstall_payload") as uninstall_mock:
                    with mock.patch("timecapsulesmb.cli.uninstall.run_ssh_conn") as run_ssh_mock:
                        with mock.patch("timecapsulesmb.cli.uninstall.wait_for_ssh_state_conn", side_effect=[True, True]) as wait_mock:
                            with mock.patch("timecapsulesmb.cli.uninstall.verify_post_uninstall", return_value=True) as verify_mock:
                                with redirect_stdout(output):
                                    rc = uninstall.main(["--yes"])
        self.assertEqual(rc, 0)
        uninstall_mock.assert_called_once()
        run_ssh_mock.assert_called_once()
        self.assertEqual(wait_mock.call_args_list[0].args[0].host, "root@10.0.0.2")
        self.assertEqual(wait_mock.call_args_list[0].kwargs, {"expected_up": False, "timeout_seconds": 60})
        self.assertEqual(wait_mock.call_args_list[1].args[0].host, "root@10.0.0.2")
        self.assertEqual(wait_mock.call_args_list[1].kwargs, {"expected_up": True, "timeout_seconds": 240})
        verify_mock.assert_called_once()
        self.assertIn("Device is back online.", output.getvalue())

    def test_uninstall_no_reboot_skips_reboot_and_returns_success(self) -> None:
        output = io.StringIO()
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
            "TC_PAYLOAD_DIR_NAME": "samba4",
        }
        with mock.patch("timecapsulesmb.cli.uninstall.load_env_values", return_value=values):
            with mock.patch("timecapsulesmb.cli.uninstall.discover_volume_root", return_value="/Volumes/dk2"):
                with mock.patch("timecapsulesmb.cli.uninstall.remote_uninstall_payload") as uninstall_mock:
                    with mock.patch("timecapsulesmb.cli.uninstall.run_ssh_conn") as run_ssh_mock:
                        with mock.patch("timecapsulesmb.cli.uninstall.verify_post_uninstall") as verify_mock:
                            with redirect_stdout(output):
                                rc = uninstall.main(["--no-reboot"])
        self.assertEqual(rc, 0)
        uninstall_mock.assert_called_once()
        run_ssh_mock.assert_not_called()
        verify_mock.assert_not_called()
        self.assertIn("Skipping reboot.", output.getvalue())

    def test_uninstall_declined_reboot_skips_reboot_and_returns_success(self) -> None:
        output = io.StringIO()
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
            "TC_PAYLOAD_DIR_NAME": "samba4",
        }
        with mock.patch("timecapsulesmb.cli.uninstall.load_env_values", return_value=values):
            with mock.patch("timecapsulesmb.cli.uninstall.discover_volume_root", return_value="/Volumes/dk2"):
                with mock.patch("timecapsulesmb.cli.uninstall.remote_uninstall_payload"):
                    with mock.patch("builtins.input", return_value="n"):
                        with mock.patch("timecapsulesmb.cli.uninstall.run_ssh_conn") as run_ssh_mock:
                            with redirect_stdout(output):
                                rc = uninstall.main([])
        self.assertEqual(rc, 0)
        run_ssh_mock.assert_not_called()
        self.assertIn("Skipped reboot.", output.getvalue())

    def test_fsck_yes_reboots_and_waits_by_default(self) -> None:
        output = io.StringIO()
        values = self.make_valid_env()
        mounted = MountedVolume(device="/dev/dk2", mountpoint="/Volumes/dk2")
        run_result = mock.Mock(stdout="--- fsck_hfs /dev/dk2 ---\nOK\n--- reboot ---\n", returncode=255)
        with mock.patch("timecapsulesmb.cli.fsck.load_env_values", return_value=values):
            with mock.patch("timecapsulesmb.cli.fsck.discover_mounted_volume", return_value=mounted):
                with mock.patch("timecapsulesmb.cli.fsck.run_ssh_conn", return_value=run_result) as run_ssh_mock:
                    with mock.patch("timecapsulesmb.cli.fsck.wait_for_ssh_state_conn", side_effect=[True, True]) as wait_mock:
                        with redirect_stdout(output):
                            rc = fsck.main(["--yes"])
        self.assertEqual(rc, 0)
        run_ssh_mock.assert_called_once()
        remote_cmd = run_ssh_mock.call_args.args[1]
        self.assertIn("pkill -f [w]atchdog.sh", remote_cmd)
        self.assertIn("pkill smbd", remote_cmd)
        self.assertIn("pkill afpserver", remote_cmd)
        self.assertIn("pkill wcifsnd", remote_cmd)
        self.assertIn("pkill wcifsfs", remote_cmd)
        self.assertIn("umount -f /Volumes/dk2", remote_cmd)
        self.assertIn("fsck_hfs -fy /dev/dk2", remote_cmd)
        self.assertIn("/sbin/reboot", remote_cmd)
        self.assertEqual(wait_mock.call_args_list[0].kwargs, {"expected_up": False, "timeout_seconds": 90})
        self.assertEqual(wait_mock.call_args_list[1].kwargs, {"expected_up": True, "timeout_seconds": 420})
        text = output.getvalue()
        self.assertIn("Mounted HFS volume: /dev/dk2 on /Volumes/dk2", text)
        self.assertIn("--- fsck_hfs /dev/dk2 ---", text)
        self.assertIn("Device is back online.", text)

    def test_fsck_validates_only_host(self) -> None:
        output = io.StringIO()
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "",
            "TC_SSH_OPTS": "-o foo",
            "TC_PAYLOAD_DIR_NAME": "../bad",
        }
        mounted = MountedVolume(device="/dev/dk2", mountpoint="/Volumes/dk2")
        run_result = mock.Mock(stdout="--- fsck_hfs /dev/dk2 ---\nOK\n", returncode=0)
        with mock.patch("timecapsulesmb.cli.fsck.load_env_values", return_value=values):
            with mock.patch("timecapsulesmb.cli.fsck.discover_mounted_volume", return_value=mounted):
                with mock.patch("timecapsulesmb.cli.fsck.run_ssh_conn", return_value=run_result):
                    with redirect_stdout(output):
                        rc = fsck.main(["--yes", "--no-reboot"])
        self.assertEqual(rc, 0)

    def test_fsck_no_wait_skips_ssh_waits(self) -> None:
        output = io.StringIO()
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
        }
        mounted = MountedVolume(device="/dev/dk2", mountpoint="/Volumes/dk2")
        run_result = mock.Mock(stdout="--- fsck_hfs /dev/dk2 ---\nOK\n--- reboot ---\n", returncode=255)
        with mock.patch("timecapsulesmb.cli.fsck.load_env_values", return_value=values):
            with mock.patch("timecapsulesmb.cli.fsck.discover_mounted_volume", return_value=mounted):
                with mock.patch("timecapsulesmb.cli.fsck.run_ssh_conn", return_value=run_result):
                    with mock.patch("timecapsulesmb.cli.fsck.wait_for_ssh_state_conn") as wait_mock:
                        with redirect_stdout(output):
                            rc = fsck.main(["--yes", "--no-wait"])
        self.assertEqual(rc, 0)
        wait_mock.assert_not_called()

    def test_fsck_no_reboot_omits_reboot_and_waits(self) -> None:
        output = io.StringIO()
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
        }
        mounted = MountedVolume(device="/dev/dk2", mountpoint="/Volumes/dk2")
        run_result = mock.Mock(stdout="--- fsck_hfs /dev/dk2 ---\nOK\n", returncode=0)
        with mock.patch("timecapsulesmb.cli.fsck.load_env_values", return_value=values):
            with mock.patch("timecapsulesmb.cli.fsck.discover_mounted_volume", return_value=mounted):
                with mock.patch("timecapsulesmb.cli.fsck.run_ssh_conn", return_value=run_result) as run_ssh_mock:
                    with mock.patch("timecapsulesmb.cli.fsck.wait_for_ssh_state_conn") as wait_mock:
                        with redirect_stdout(output):
                            rc = fsck.main(["--yes", "--no-reboot"])
        self.assertEqual(rc, 0)
        wait_mock.assert_not_called()
        self.assertNotIn("/sbin/reboot", run_ssh_mock.call_args.args[1])

    def test_fsck_prompt_decline_cancels_before_remote_actions(self) -> None:
        output = io.StringIO()
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
        }
        mounted = MountedVolume(device="/dev/dk2", mountpoint="/Volumes/dk2")
        with mock.patch("timecapsulesmb.cli.fsck.load_env_values", return_value=values):
            with mock.patch("timecapsulesmb.cli.fsck.discover_mounted_volume", return_value=mounted):
                with mock.patch("builtins.input", return_value="n"):
                    with mock.patch("timecapsulesmb.cli.fsck.run_ssh_conn") as run_ssh_mock:
                        with redirect_stdout(output):
                            rc = fsck.main([])
        self.assertEqual(rc, 0)
        run_ssh_mock.assert_not_called()
        self.assertIn("fsck cancelled.", output.getvalue())

    def test_discover_json_outputs_records(self) -> None:
        output = io.StringIO()
        record = Discovered(
            name="Time Capsule",
            hostname="capsule.local",
            ipv4=["10.0.0.2"],
            ipv6=[],
            services={"_airport._tcp.local."},
            properties={"model": "AirPort Time Capsule"},
        )
        with mock.patch("timecapsulesmb.discovery.bonjour.discover", return_value=[record]):
            with redirect_stdout(output):
                rc = discover.main(["--json"])
        self.assertEqual(rc, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload[0]["name"], "Time Capsule")


if __name__ == "__main__":
    unittest.main()
