from __future__ import annotations

import io
import json
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from timecapsulesmb.cli import activate, bootstrap, configure, deploy, discover, doctor, fsck, prep_device, uninstall
from timecapsulesmb.cli.main import main
from timecapsulesmb.core.config import DEFAULTS
from timecapsulesmb.device.compat import DeviceCompatibility
from timecapsulesmb.device.probe import MountedVolume
from timecapsulesmb.discovery.bonjour import Discovered


class CliTests(unittest.TestCase):
    def make_supported_compatibility(self) -> DeviceCompatibility:
        return DeviceCompatibility(
            os_name="NetBSD",
            os_release="6.0",
            arch="earmv4",
            payload_family="netbsd6_samba4",
            device_generation="gen5",
            mdns_device_model_hint="TimeCapsule8,119",
            supported=True,
            message="Detected supported device: NetBSD 6.0 (earmv4).",
        )

    def make_supported_netbsd4_compatibility(self) -> DeviceCompatibility:
        return DeviceCompatibility(
            os_name="NetBSD",
            os_release="4.0",
            arch="earmv4",
            payload_family="netbsd4_samba4",
            device_generation="gen1-4",
            mdns_device_model_hint="TimeCapsule6,106",
            supported=True,
            message="Detected supported older device: NetBSD 4.0 (earmv4).",
        )

    def test_dispatches_to_command_handler(self) -> None:
        with mock.patch("timecapsulesmb.cli.main.COMMANDS", {"doctor": mock.Mock(return_value=7)}):
            rc = main(["doctor", "--skip-smb"])
        self.assertEqual(rc, 7)

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

        with mock.patch("timecapsulesmb.cli.configure.parse_env_values", return_value={}):
            with mock.patch("timecapsulesmb.cli.configure.discover", return_value=[]):
                with mock.patch(
                    "timecapsulesmb.cli.configure.prompt",
                    side_effect=lambda _l, _d, _s: _d if _l == "mDNS device model hint" else next(prompt_values),
                ):
                    with mock.patch("timecapsulesmb.cli.configure.tcp_open", return_value=False):
                        with mock.patch("timecapsulesmb.cli.configure.confirm", return_value=True):
                            with mock.patch("timecapsulesmb.cli.configure.write_env_file", side_effect=fake_write_env_file):
                                with redirect_stdout(output):
                                    rc = configure.main([])
        self.assertEqual(rc, 0)
        self.assertEqual(fake_values["TC_SAMBA_USER"], "admin")
        self.assertIn("Wrote", output.getvalue())
        self.assertIn("This writes a local .env configuration file", output.getvalue())

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
            with mock.patch("timecapsulesmb.cli.configure.discover", return_value=[record]):
                with mock.patch("timecapsulesmb.cli.configure.prefer_routable_ipv4", return_value="10.0.0.2"):
                    with mock.patch("builtins.input", side_effect=["1"]):
                        with mock.patch("timecapsulesmb.cli.configure.prompt", side_effect=fake_prompt):
                            with mock.patch("timecapsulesmb.cli.configure.tcp_open", return_value=False):
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
            with mock.patch("timecapsulesmb.cli.configure.discover", return_value=[]):
                with mock.patch("timecapsulesmb.cli.configure.prompt", side_effect=fake_prompt):
                    with mock.patch("timecapsulesmb.cli.configure.tcp_open", return_value=True):
                        with mock.patch("timecapsulesmb.cli.configure.validate_ssh_target", return_value=True):
                            with mock.patch("timecapsulesmb.cli.configure.infer_mdns_device_model_hint", return_value="TimeCapsule8,119"):
                                with mock.patch("timecapsulesmb.cli.configure.write_env_file", side_effect=fake_write_env_file):
                                    with redirect_stdout(output):
                                        rc = configure.main([])
        self.assertEqual(rc, 0)
        self.assertEqual(seen_defaults["mDNS device model hint"], "TimeCapsule8,119")
        self.assertEqual(fake_values["TC_MDNS_DEVICE_MODEL"], "TimeCapsule8,119")

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
            with mock.patch("timecapsulesmb.cli.configure.discover", return_value=[record]):
                with mock.patch("builtins.input", side_effect=["1"]):
                    with mock.patch("timecapsulesmb.cli.configure.prompt", side_effect=fake_prompt):
                        with mock.patch("timecapsulesmb.cli.configure.tcp_open", return_value=False):
                            with mock.patch("timecapsulesmb.cli.configure.confirm", return_value=True):
                                with mock.patch("timecapsulesmb.cli.configure.write_env_file", side_effect=fake_write_env_file):
                                    with redirect_stdout(output):
                                        rc = configure.main([])
        self.assertEqual(rc, 0)
        self.assertEqual(fake_values["TC_AIRPORT_SYAP"], "119")
        self.assertEqual(fake_values["TC_MDNS_DEVICE_MODEL"], "TimeCapsule8,119")

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
            with mock.patch("timecapsulesmb.cli.configure.discover", return_value=[record]):
                with mock.patch("builtins.input", side_effect=["q"]):
                    with mock.patch("timecapsulesmb.cli.configure.prompt", side_effect=fake_prompt):
                        with mock.patch("timecapsulesmb.cli.configure.tcp_open", return_value=False):
                            with mock.patch("timecapsulesmb.cli.configure.confirm", return_value=True):
                                with mock.patch("timecapsulesmb.cli.configure.write_env_file", side_effect=fake_write_env_file):
                                    with redirect_stdout(output):
                                        rc = configure.main([])
        self.assertEqual(rc, 0)
        self.assertEqual(fake_values["TC_HOST"], DEFAULTS["TC_HOST"])
        self.assertEqual(fake_values["TC_AIRPORT_SYAP"], "119")
        self.assertIn("Found devices:", output.getvalue())
        self.assertIn(f"Discovery skipped. Falling back to {DEFAULTS['TC_HOST']}.", output.getvalue())

    def test_configure_preserves_existing_mdns_device_model_override(self) -> None:
        output = io.StringIO()
        fake_values = {}
        existing = {
            "TC_AIRPORT_SYAP": "119",
            "TC_MDNS_DEVICE_MODEL": "CustomCapsuleModel",
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
            with mock.patch("timecapsulesmb.cli.configure.discover", return_value=[]):
                with mock.patch("timecapsulesmb.cli.configure.prompt", side_effect=fake_prompt):
                    with mock.patch("timecapsulesmb.cli.configure.tcp_open", return_value=True):
                        with mock.patch("timecapsulesmb.cli.configure.validate_ssh_target", return_value=True):
                            with mock.patch("timecapsulesmb.cli.configure.infer_mdns_device_model_hint", return_value="TimeCapsule8,119"):
                                with mock.patch("timecapsulesmb.cli.configure.write_env_file", side_effect=fake_write_env_file):
                                    with redirect_stdout(output):
                                        rc = configure.main([])
        self.assertEqual(rc, 0)
        self.assertEqual(seen_defaults["mDNS device model hint"], "CustomCapsuleModel")
        self.assertEqual(fake_values["TC_MDNS_DEVICE_MODEL"], "CustomCapsuleModel")

    def test_configure_existing_syap_prefills_visible_mdns_device_model_when_undetected(self) -> None:
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
            with mock.patch("timecapsulesmb.cli.configure.discover", return_value=[]):
                with mock.patch("timecapsulesmb.cli.configure.prompt", side_effect=fake_prompt):
                    with mock.patch("timecapsulesmb.cli.configure.tcp_open", return_value=False):
                        with mock.patch("timecapsulesmb.cli.configure.confirm", return_value=True):
                            with mock.patch("timecapsulesmb.cli.configure.write_env_file", side_effect=fake_write_env_file):
                                with redirect_stdout(output):
                                    rc = configure.main([])
        self.assertEqual(rc, 0)
        self.assertEqual(fake_values["TC_AIRPORT_SYAP"], "116")
        self.assertEqual(seen_defaults["mDNS device model hint"], "TimeCapsule6,116")
        self.assertEqual(fake_values["TC_MDNS_DEVICE_MODEL"], "TimeCapsule6,116")

    def test_configure_prompted_syap_prefills_visible_mdns_device_model_from_existing_env(self) -> None:
        output = io.StringIO()
        fake_values = {}
        existing = {
            "TC_MDNS_DEVICE_MODEL": "CustomCapsuleModel",
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
            with mock.patch("timecapsulesmb.cli.configure.discover", return_value=[]):
                with mock.patch("timecapsulesmb.cli.configure.prompt", side_effect=fake_prompt):
                    with mock.patch("timecapsulesmb.cli.configure.tcp_open", return_value=False):
                        with mock.patch("timecapsulesmb.cli.configure.confirm", return_value=True):
                            with mock.patch("timecapsulesmb.cli.configure.write_env_file", side_effect=fake_write_env_file):
                                with redirect_stdout(output):
                                    rc = configure.main([])
        self.assertEqual(rc, 0)
        self.assertEqual(fake_values["TC_AIRPORT_SYAP"], "116")
        self.assertEqual(seen_defaults["mDNS device model hint"], "CustomCapsuleModel")
        self.assertEqual(fake_values["TC_MDNS_DEVICE_MODEL"], "CustomCapsuleModel")

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
            with mock.patch("timecapsulesmb.cli.configure.discover", return_value=[]):
                with mock.patch("builtins.input", side_effect=lambda _prompt: next(input_values)):
                    with mock.patch("timecapsulesmb.cli.configure.getpass.getpass", side_effect=lambda _prompt: next(password_values)):
                        with mock.patch("timecapsulesmb.cli.configure.tcp_open", return_value=False):
                            with mock.patch("timecapsulesmb.cli.configure.confirm", return_value=True):
                                with mock.patch("timecapsulesmb.cli.configure.write_env_file", side_effect=fake_write_env_file):
                                    with redirect_stdout(output):
                                        rc = configure.main([])
        self.assertEqual(rc, 0)
        self.assertEqual(fake_values["TC_PASSWORD"], "goodpw")
        self.assertIn("Time Capsule root password cannot be blank", output.getvalue())

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
            with mock.patch("timecapsulesmb.cli.configure.discover", return_value=[]):
                with mock.patch("timecapsulesmb.cli.configure.prompt", side_effect=fake_prompt):
                    with mock.patch("timecapsulesmb.cli.configure.tcp_open", return_value=True):
                        with mock.patch("timecapsulesmb.cli.configure.validate_ssh_target", side_effect=[False, True]):
                            with mock.patch("timecapsulesmb.cli.configure.confirm", return_value=False):
                                with mock.patch("timecapsulesmb.cli.configure.infer_mdns_device_model_hint", return_value="TimeCapsule"):
                                    with mock.patch("timecapsulesmb.cli.configure.write_env_file", side_effect=fake_write_env_file):
                                        with redirect_stdout(output):
                                            rc = configure.main([])
        self.assertEqual(rc, 0)
        self.assertEqual(fake_values["TC_HOST"], "root@10.0.0.3")
        self.assertEqual(fake_values["TC_PASSWORD"], "goodpw")
        self.assertIn("did not work", output.getvalue())

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
            with mock.patch("timecapsulesmb.cli.configure.discover", return_value=[]):
                with mock.patch("timecapsulesmb.cli.configure.prompt", side_effect=fake_prompt):
                    with mock.patch("timecapsulesmb.cli.configure.tcp_open", return_value=True):
                        with mock.patch("timecapsulesmb.cli.configure.validate_ssh_target", return_value=False):
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
            with mock.patch("timecapsulesmb.cli.configure.discover", return_value=[]):
                with mock.patch("timecapsulesmb.cli.configure.prompt", side_effect=fake_prompt):
                    with mock.patch("timecapsulesmb.cli.configure.tcp_open", return_value=False):
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
            with mock.patch("timecapsulesmb.cli.configure.discover", return_value=[]):
                with mock.patch("timecapsulesmb.cli.configure.prompt", side_effect=fake_prompt):
                    with mock.patch("timecapsulesmb.cli.configure.tcp_open", return_value=True):
                        with mock.patch("timecapsulesmb.cli.configure.validate_ssh_target", return_value=True):
                            with mock.patch("timecapsulesmb.cli.configure.infer_mdns_device_model_hint", return_value="TimeCapsule"):
                                with mock.patch("timecapsulesmb.cli.configure.write_env_file", side_effect=fake_write_env_file):
                                    with redirect_stdout(output):
                                        rc = configure.main([])
        self.assertEqual(rc, 0)
        self.assertEqual(fake_values["TC_MDNS_INSTANCE_NAME"], "Time Capsule Samba 4")
        self.assertEqual(fake_values["TC_MDNS_HOST_LABEL"], "timecapsulesamba4")
        self.assertIn("mDNS SMB instance name must not contain dots.", output.getvalue())
        self.assertIn("mDNS host label must not contain dots.", output.getvalue())

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
            with mock.patch("timecapsulesmb.cli.configure.discover", return_value=[]):
                with mock.patch("timecapsulesmb.cli.configure.prompt", side_effect=fake_prompt):
                    with mock.patch("timecapsulesmb.cli.configure.tcp_open", return_value=True):
                        with mock.patch("timecapsulesmb.cli.configure.validate_ssh_target", return_value=True):
                            with mock.patch("timecapsulesmb.cli.configure.infer_mdns_device_model_hint", return_value="TimeCapsule8,119"):
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
            with mock.patch("timecapsulesmb.cli.configure.discover", return_value=[]):
                with mock.patch("timecapsulesmb.cli.configure.prompt", side_effect=fake_prompt):
                    with mock.patch("timecapsulesmb.cli.configure.tcp_open", return_value=False):
                        with mock.patch("timecapsulesmb.cli.configure.confirm", return_value=True):
                            with mock.patch("timecapsulesmb.cli.configure.write_env_file", side_effect=fake_write_env_file):
                                with redirect_stdout(output):
                                    rc = configure.main([])
        self.assertEqual(rc, 0)
        self.assertEqual(fake_values["TC_AIRPORT_SYAP"], "116")
        self.assertEqual(fake_values["TC_MDNS_DEVICE_MODEL"], "TimeCapsule6,116")

    def test_configure_prompted_syap_prefills_visible_mdns_device_model_from_lookup(self) -> None:
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
            with mock.patch("timecapsulesmb.cli.configure.discover", return_value=[]):
                with mock.patch("timecapsulesmb.cli.configure.prompt", side_effect=fake_prompt):
                    with mock.patch("timecapsulesmb.cli.configure.tcp_open", return_value=False):
                        with mock.patch("timecapsulesmb.cli.configure.confirm", return_value=True):
                            with mock.patch("timecapsulesmb.cli.configure.write_env_file", side_effect=fake_write_env_file):
                                with redirect_stdout(output):
                                    rc = configure.main([])
        self.assertEqual(rc, 0)
        self.assertEqual(fake_values["TC_AIRPORT_SYAP"], "116")
        self.assertEqual(seen_defaults["mDNS device model hint"], "TimeCapsule6,116")
        self.assertEqual(fake_values["TC_MDNS_DEVICE_MODEL"], "TimeCapsule6,116")

    def test_configure_reprompts_invalid_share_name(self) -> None:
        output = io.StringIO()
        fake_values = {}
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
            if label == "mDNS device model hint":
                return default
            return next(prompt_values)

        def fake_write_env_file(_path, values):
            fake_values.update(values)

        with mock.patch("timecapsulesmb.cli.configure.parse_env_values", return_value={}):
            with mock.patch("timecapsulesmb.cli.configure.discover", return_value=[]):
                with mock.patch("timecapsulesmb.cli.configure.prompt", side_effect=fake_prompt):
                    with mock.patch("timecapsulesmb.cli.configure.tcp_open", return_value=True):
                        with mock.patch("timecapsulesmb.cli.configure.validate_ssh_target", return_value=True):
                            with mock.patch("timecapsulesmb.cli.configure.infer_mdns_device_model_hint", return_value="TimeCapsule"):
                                with mock.patch("timecapsulesmb.cli.configure.write_env_file", side_effect=fake_write_env_file):
                                    with redirect_stdout(output):
                                        rc = configure.main([])
        self.assertEqual(rc, 0)
        self.assertEqual(fake_values["TC_SHARE_NAME"], "Data")
        self.assertIn("SMB share name must be 192 bytes or fewer.", output.getvalue())

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
            with mock.patch("timecapsulesmb.cli.configure.discover", return_value=[]):
                with mock.patch("timecapsulesmb.cli.configure.prompt", side_effect=fake_prompt):
                    with mock.patch("timecapsulesmb.cli.configure.tcp_open", return_value=True):
                        with mock.patch("timecapsulesmb.cli.configure.validate_ssh_target", return_value=True):
                            with mock.patch("timecapsulesmb.cli.configure.infer_mdns_device_model_hint", return_value="TimeCapsule"):
                                with mock.patch("timecapsulesmb.cli.configure.write_env_file", side_effect=fake_write_env_file):
                                    with redirect_stdout(output):
                                        rc = configure.main([])
        self.assertEqual(rc, 0)
        self.assertEqual(fake_values["TC_NETBIOS_NAME"], "TimeCapsule")
        self.assertIn("Samba NetBIOS name must be 15 bytes or fewer.", output.getvalue())

    def test_doctor_returns_failure_when_checks_fatal(self) -> None:
        output = io.StringIO()
        fake_result = doctor.CheckResult("FAIL", "broken")
        with mock.patch("timecapsulesmb.cli.doctor.parse_env_values", return_value={}):
            with mock.patch("timecapsulesmb.cli.doctor.run_doctor_checks", return_value=([fake_result], True)):
                with redirect_stdout(output):
                    rc = doctor.main([])
        self.assertEqual(rc, 1)
        self.assertIn("doctor found one or more fatal problems", output.getvalue())

    def test_doctor_streams_results_in_human_mode(self) -> None:
        output = io.StringIO()
        streamed_result = doctor.CheckResult("PASS", "streamed")

        def fake_run_doctor_checks(*_args, **kwargs):
            kwargs["on_result"](streamed_result)
            return ([streamed_result], False)

        with mock.patch("timecapsulesmb.cli.doctor.parse_env_values", return_value={}):
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

        with mock.patch("timecapsulesmb.cli.doctor.parse_env_values", return_value={}):
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
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule",
            "TC_SAMBA_USER": "admin",
        }
        with mock.patch("timecapsulesmb.cli.deploy.parse_env_values", return_value=values):
            with mock.patch("timecapsulesmb.cli.deploy.validate_artifacts", return_value=[("smbd", True, "ok"), ("mdns", True, "ok")]):
                with mock.patch("timecapsulesmb.cli.deploy.discover_volume_root", return_value="/Volumes/dk2"):
                    with mock.patch("timecapsulesmb.cli.deploy.probe_device_compatibility", return_value=self.make_supported_compatibility()):
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
        actions_mock.assert_not_called()
        upload_mock.assert_not_called()
        auth_mock.assert_not_called()

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
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule",
            "TC_SAMBA_USER": "admin",
        }
        with mock.patch("timecapsulesmb.cli.deploy.parse_env_values", return_value=values):
            with mock.patch("timecapsulesmb.cli.deploy.validate_artifacts", return_value=[("smbd", False, "checksum mismatch")]):
                with self.assertRaises(SystemExit):
                    deploy.main([])

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
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule",
            "TC_SAMBA_USER": "admin",
        }
        with mock.patch("timecapsulesmb.cli.deploy.parse_env_values", return_value=values):
            with mock.patch("timecapsulesmb.cli.deploy.validate_artifacts", return_value=[("smbd", True, "ok"), ("mdns", True, "ok")]):
                with mock.patch("timecapsulesmb.cli.deploy.discover_volume_root", return_value="/Volumes/dk2"):
                    with mock.patch("timecapsulesmb.cli.deploy.probe_device_compatibility", return_value=self.make_supported_compatibility()):
                        with mock.patch("timecapsulesmb.cli.deploy.run_remote_actions"):
                            with mock.patch(
                                "timecapsulesmb.cli.deploy.remote_ensure_adisk_uuid",
                                return_value="12345678-1234-1234-1234-123456789012",
                            ):
                                with mock.patch("timecapsulesmb.cli.deploy.upload_deployment_payload"):
                                    with mock.patch("timecapsulesmb.cli.deploy.remote_install_auth_files"):
                                        with mock.patch("timecapsulesmb.cli.deploy.run_ssh") as run_ssh_mock:
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
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule",
            "TC_SAMBA_USER": "admin",
        }
        with mock.patch("timecapsulesmb.cli.deploy.parse_env_values", return_value=values):
            with mock.patch("timecapsulesmb.cli.deploy.validate_artifacts", return_value=[("smbd", True, "ok"), ("mdns", True, "ok")]):
                with mock.patch("timecapsulesmb.cli.deploy.discover_volume_root", return_value="/Volumes/dk2"):
                    with mock.patch("timecapsulesmb.cli.deploy.probe_device_compatibility", return_value=self.make_supported_compatibility()):
                        with mock.patch("timecapsulesmb.cli.deploy.run_remote_actions"):
                            with mock.patch(
                                "timecapsulesmb.cli.deploy.remote_ensure_adisk_uuid",
                                return_value="12345678-1234-1234-1234-123456789012",
                            ):
                                with mock.patch("timecapsulesmb.cli.deploy.upload_deployment_payload"):
                                    with mock.patch("timecapsulesmb.cli.deploy.remote_install_auth_files"):
                                        with mock.patch("builtins.input", return_value="n"):
                                            with mock.patch("timecapsulesmb.cli.deploy.run_ssh") as run_ssh_mock:
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
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule",
            "TC_SAMBA_USER": "admin",
        }
        with mock.patch("timecapsulesmb.cli.deploy.parse_env_values", return_value=values):
            with mock.patch("timecapsulesmb.cli.deploy.validate_artifacts", return_value=[("smbd", True, "ok"), ("mdns", True, "ok")]):
                with mock.patch("timecapsulesmb.cli.deploy.discover_volume_root", return_value="/Volumes/dk2"):
                    with mock.patch("timecapsulesmb.cli.deploy.probe_device_compatibility", return_value=self.make_supported_compatibility()):
                        with mock.patch("timecapsulesmb.cli.deploy.run_remote_actions"):
                            with mock.patch(
                                "timecapsulesmb.cli.deploy.remote_ensure_adisk_uuid",
                                return_value="12345678-1234-1234-1234-123456789012",
                            ):
                                with mock.patch("timecapsulesmb.cli.deploy.upload_deployment_payload"):
                                    with mock.patch("timecapsulesmb.cli.deploy.remote_install_auth_files"):
                                        with mock.patch("builtins.input", return_value="y"):
                                            with mock.patch("timecapsulesmb.cli.deploy.run_ssh"):
                                                with mock.patch("timecapsulesmb.cli.deploy.wait_for_ssh_state", side_effect=[True, False]) as wait_mock:
                                                    with redirect_stdout(output):
                                                        rc = deploy.main([])
        self.assertEqual(rc, 1)
        self.assertEqual(
            wait_mock.call_args_list[0].args,
            ("root@10.0.0.2", "pw", "-o foo"),
        )
        self.assertEqual(wait_mock.call_args_list[0].kwargs, {"expected_up": False, "timeout_seconds": 60})
        self.assertEqual(
            wait_mock.call_args_list[1].args,
            ("root@10.0.0.2", "pw", "-o foo"),
        )
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
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule",
            "TC_SAMBA_USER": "admin",
        }
        with mock.patch("timecapsulesmb.cli.deploy.parse_env_values", return_value=values):
            with mock.patch("timecapsulesmb.cli.deploy.validate_artifacts", return_value=[("smbd", True, "ok"), ("mdns", True, "ok"), ("nbns", True, "ok")]):
                with mock.patch("timecapsulesmb.cli.deploy.discover_volume_root", return_value="/Volumes/dk2"):
                    with mock.patch("timecapsulesmb.cli.deploy.probe_device_compatibility", return_value=self.make_supported_compatibility()):
                        with mock.patch("timecapsulesmb.cli.deploy.run_remote_actions"):
                            with mock.patch(
                                "timecapsulesmb.cli.deploy.remote_ensure_adisk_uuid",
                                return_value="12345678-1234-1234-1234-123456789012",
                            ):
                                with mock.patch("timecapsulesmb.cli.deploy.upload_deployment_payload"):
                                    with mock.patch("timecapsulesmb.cli.deploy.remote_install_auth_files"):
                                        with mock.patch("timecapsulesmb.cli.deploy.run_ssh"):
                                            with mock.patch("timecapsulesmb.cli.deploy.wait_for_ssh_state", side_effect=[True, True]):
                                                with mock.patch("timecapsulesmb.cli.deploy.wait_for_post_reboot_smbd", return_value=True) as ready_mock:
                                                    with mock.patch("timecapsulesmb.cli.deploy.wait_for_post_reboot_mdns_takeover", return_value=True) as mdns_ready_mock:
                                                        with mock.patch("timecapsulesmb.cli.deploy.verify_post_deploy") as verify_mock:
                                                            with mock.patch("builtins.input", return_value="y"):
                                                                with redirect_stdout(output):
                                                                    rc = deploy.main([])
        self.assertEqual(rc, 0)
        ready_mock.assert_called_once_with("root@10.0.0.2", "pw", "-o foo")
        mdns_ready_mock.assert_called_once_with("root@10.0.0.2", "pw", "-o foo")
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
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule",
            "TC_SAMBA_USER": "admin",
        }
        with mock.patch("timecapsulesmb.cli.deploy.parse_env_values", return_value=values):
            with mock.patch("timecapsulesmb.cli.deploy.validate_artifacts", return_value=[("smbd", True, "ok"), ("mdns", True, "ok"), ("nbns", True, "ok")]):
                with mock.patch("timecapsulesmb.cli.deploy.discover_volume_root", return_value="/Volumes/dk2"):
                    with mock.patch("timecapsulesmb.cli.deploy.probe_device_compatibility", return_value=self.make_supported_compatibility()):
                        with mock.patch("timecapsulesmb.cli.deploy.run_remote_actions"):
                            with mock.patch(
                                "timecapsulesmb.cli.deploy.remote_ensure_adisk_uuid",
                                return_value="12345678-1234-1234-1234-123456789012",
                            ):
                                with mock.patch("timecapsulesmb.cli.deploy.upload_deployment_payload"):
                                    with mock.patch("timecapsulesmb.cli.deploy.remote_install_auth_files"):
                                        with mock.patch("timecapsulesmb.cli.deploy.run_ssh"):
                                            with mock.patch("timecapsulesmb.cli.deploy.wait_for_ssh_state", side_effect=[True, True]):
                                                with mock.patch("timecapsulesmb.cli.deploy.wait_for_post_reboot_smbd", return_value=False) as ready_mock:
                                                    with mock.patch("timecapsulesmb.cli.deploy.wait_for_post_reboot_mdns_takeover") as mdns_ready_mock:
                                                        with mock.patch("timecapsulesmb.cli.deploy.verify_post_deploy") as verify_mock:
                                                            with mock.patch("builtins.input", return_value="y"):
                                                                with redirect_stdout(output):
                                                                    rc = deploy.main([])
        self.assertEqual(rc, 1)
        ready_mock.assert_called_once_with("root@10.0.0.2", "pw", "-o foo")
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
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule",
            "TC_SAMBA_USER": "admin",
        }
        with mock.patch("timecapsulesmb.cli.deploy.parse_env_values", return_value=values):
            with mock.patch("timecapsulesmb.cli.deploy.validate_artifacts", return_value=[("smbd", True, "ok"), ("mdns", True, "ok"), ("nbns", True, "ok")]):
                with mock.patch("timecapsulesmb.cli.deploy.discover_volume_root", return_value="/Volumes/dk2"):
                    with mock.patch("timecapsulesmb.cli.deploy.probe_device_compatibility", return_value=self.make_supported_compatibility()):
                        with mock.patch("timecapsulesmb.cli.deploy.run_remote_actions"):
                            with mock.patch(
                                "timecapsulesmb.cli.deploy.remote_ensure_adisk_uuid",
                                return_value="12345678-1234-1234-1234-123456789012",
                            ):
                                with mock.patch("timecapsulesmb.cli.deploy.upload_deployment_payload"):
                                    with mock.patch("timecapsulesmb.cli.deploy.remote_install_auth_files"):
                                        with mock.patch("timecapsulesmb.cli.deploy.run_ssh"):
                                            with mock.patch("timecapsulesmb.cli.deploy.wait_for_ssh_state", side_effect=[True, True]):
                                                with mock.patch("timecapsulesmb.cli.deploy.wait_for_post_reboot_smbd", return_value=True) as ready_mock:
                                                    with mock.patch("timecapsulesmb.cli.deploy.wait_for_post_reboot_mdns_takeover", return_value=False) as mdns_ready_mock:
                                                        with mock.patch("timecapsulesmb.cli.deploy.verify_post_deploy") as verify_mock:
                                                            with mock.patch("builtins.input", return_value="y"):
                                                                with redirect_stdout(output):
                                                                    rc = deploy.main([])
        self.assertEqual(rc, 1)
        ready_mock.assert_called_once_with("root@10.0.0.2", "pw", "-o foo")
        mdns_ready_mock.assert_called_once_with("root@10.0.0.2", "pw", "-o foo")
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
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule",
            "TC_SAMBA_USER": "admin",
        }
        with mock.patch("timecapsulesmb.cli.deploy.parse_env_values", return_value=values):
            with mock.patch("timecapsulesmb.cli.deploy.validate_artifacts", return_value=[("smbd", True, "ok"), ("mdns", True, "ok"), ("nbns", True, "ok")]):
                with mock.patch("timecapsulesmb.cli.deploy.discover_volume_root", return_value="/Volumes/dk2"):
                    with mock.patch("timecapsulesmb.cli.deploy.probe_device_compatibility", return_value=self.make_supported_compatibility()):
                        with mock.patch("timecapsulesmb.cli.deploy.run_remote_actions") as actions_mock:
                            with mock.patch("timecapsulesmb.cli.deploy.remote_ensure_adisk_uuid", return_value="12345678-1234-1234-1234-123456789012"):
                                with mock.patch("timecapsulesmb.cli.deploy.upload_deployment_payload"):
                                    with mock.patch("timecapsulesmb.cli.deploy.remote_install_auth_files"):
                                        rc = deploy.main(["--install-nbns", "--no-reboot"])
        self.assertEqual(rc, 0)
        self.assertEqual(actions_mock.call_count, 2)
        pre_upload_action_kinds = [action.kind for action in actions_mock.call_args_list[0].args[3]]
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
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule",
            "TC_SAMBA_USER": "admin",
        }
        with mock.patch("timecapsulesmb.cli.deploy.parse_env_values", return_value=values):
            with mock.patch("timecapsulesmb.cli.deploy.validate_artifacts", return_value=[("smbd", True, "ok"), ("mdns", True, "ok"), ("nbns", True, "ok")]):
                with mock.patch("timecapsulesmb.cli.deploy.discover_volume_root", return_value="/Volumes/dk2"):
                    with mock.patch("timecapsulesmb.cli.deploy.probe_device_compatibility", return_value=self.make_supported_compatibility()):
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
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule",
            "TC_SAMBA_USER": "admin",
        }
        with mock.patch("timecapsulesmb.cli.deploy.parse_env_values", return_value=values):
            with mock.patch("timecapsulesmb.cli.deploy.validate_artifacts", return_value=[("smbd-netbsd4", True, "ok")]):
                with mock.patch("timecapsulesmb.cli.deploy.discover_volume_root", return_value="/Volumes/dk2"):
                    with mock.patch("timecapsulesmb.cli.deploy.probe_device_compatibility", return_value=self.make_supported_netbsd4_compatibility()):
                        with redirect_stdout(output):
                            rc = deploy.main(["--dry-run"])
        self.assertEqual(rc, 0)
        text = output.getvalue()
        payload_dir = f"/Volumes/dk2/{values['TC_PAYLOAD_DIR_NAME']}"
        self.assertIn("Detected supported older device: NetBSD 4.0", text)
        self.assertIn(f"bin/samba4-netbsd4/smbd -> {payload_dir}/smbd", text)
        self.assertIn(f"bin/mdns-netbsd4/mdns-advertiser -> {payload_dir}/mdns-advertiser", text)
        self.assertIn("bin/mdns-netbsd4/mdns-advertiser -> /mnt/Flash/mdns-advertiser", text)
        self.assertIn(f"bin/nbns-netbsd4/nbns-advertiser -> {payload_dir}/nbns-advertiser", text)
        self.assertIn("Remote actions (NetBSD4 activation):", text)
        self.assertIn("pkill -f '[w]atchdog.sh' >/dev/null 2>&1 || true", text)
        self.assertIn("pkill smbd >/dev/null 2>&1 || true", text)
        self.assertIn("pkill mdns-advertiser >/dev/null 2>&1 || true", text)
        self.assertIn("pkill nbns-advertiser >/dev/null 2>&1 || true", text)
        self.assertIn("pkill wcifsfs >/dev/null 2>&1 || true", text)
        self.assertIn("/bin/sh /mnt/Flash/rc.local", text)
        self.assertIn("NetBSD4 activation is immediate.", text)
        self.assertIn("other generations may auto-start rc.local", text)

    def test_deploy_netbsd4_prompt_decline_cancels_before_remote_actions(self) -> None:
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
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule",
            "TC_SAMBA_USER": "admin",
        }
        with mock.patch("timecapsulesmb.cli.deploy.parse_env_values", return_value=values):
            with mock.patch("timecapsulesmb.cli.deploy.validate_artifacts", return_value=[("smbd-netbsd4", True, "ok")]):
                with mock.patch("timecapsulesmb.cli.deploy.discover_volume_root", return_value="/Volumes/dk2"):
                    with mock.patch("timecapsulesmb.cli.deploy.probe_device_compatibility", return_value=self.make_supported_netbsd4_compatibility()):
                        with mock.patch("builtins.input", return_value="n"):
                            with mock.patch("timecapsulesmb.cli.deploy.run_remote_actions") as actions_mock:
                                with redirect_stdout(output):
                                    rc = deploy.main([])
        self.assertEqual(rc, 0)
        actions_mock.assert_not_called()
        self.assertIn("Deployment cancelled.", output.getvalue())

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
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule",
            "TC_SAMBA_USER": "admin",
        }
        with mock.patch("timecapsulesmb.cli.deploy.parse_env_values", return_value=values):
            with mock.patch("timecapsulesmb.cli.deploy.validate_artifacts", return_value=[("smbd-netbsd4", True, "ok")]):
                with mock.patch("timecapsulesmb.cli.deploy.discover_volume_root", return_value="/Volumes/dk2"):
                    with mock.patch("timecapsulesmb.cli.deploy.probe_device_compatibility", return_value=self.make_supported_netbsd4_compatibility()):
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
        self.assertIn("Run activate after reboot if the device did not auto-start Samba.", output.getvalue())

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
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule",
            "TC_SAMBA_USER": "admin",
        }
        template_bundle = mock.Mock(
            start_script_replacements={},
            watchdog_replacements={},
            smbconf_replacements={},
        )
        with mock.patch("timecapsulesmb.cli.deploy.parse_env_values", return_value=values):
            with mock.patch("timecapsulesmb.cli.deploy.validate_artifacts", return_value=[("smbd-netbsd4", True, "ok")]):
                with mock.patch("timecapsulesmb.cli.deploy.discover_volume_root", return_value="/Volumes/dk2"):
                    with mock.patch("timecapsulesmb.cli.deploy.probe_device_compatibility", return_value=self.make_supported_netbsd4_compatibility()):
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
            payload_family="netbsd4_samba4",
        )

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
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule",
            "TC_SAMBA_USER": "admin",
        }
        with mock.patch("timecapsulesmb.cli.deploy.parse_env_values", return_value=values):
            with mock.patch("timecapsulesmb.cli.deploy.validate_artifacts", return_value=[("smbd-netbsd4", True, "ok")]):
                with mock.patch("timecapsulesmb.cli.deploy.discover_volume_root", return_value="/Volumes/dk2"):
                    with mock.patch("timecapsulesmb.cli.deploy.probe_device_compatibility", return_value=self.make_supported_netbsd4_compatibility()):
                        with mock.patch("timecapsulesmb.cli.deploy.run_remote_actions") as actions_mock:
                            with mock.patch("timecapsulesmb.cli.deploy.remote_ensure_adisk_uuid", return_value=""):
                                with mock.patch("timecapsulesmb.cli.deploy.upload_deployment_payload"):
                                    with mock.patch("timecapsulesmb.cli.deploy.remote_install_auth_files"):
                                        with mock.patch("timecapsulesmb.cli.deploy.verify_netbsd4_activation", return_value=True) as verify_mock:
                                            with mock.patch("timecapsulesmb.cli.deploy.run_ssh") as run_ssh_mock:
                                                with redirect_stdout(output):
                                                    rc = deploy.main(["--yes"])
        self.assertEqual(rc, 0)
        self.assertEqual(actions_mock.call_count, 3)
        activation_action_kinds = [action.kind for action in actions_mock.call_args_list[2].args[3]]
        activation_action_args = [action.args[0] for action in actions_mock.call_args_list[2].args[3]]
        self.assertEqual(
            activation_action_kinds,
            ["stop_process_full", "stop_process", "stop_process", "stop_process", "stop_process", "run_script"],
        )
        self.assertEqual(
            activation_action_args,
            ["[w]atchdog.sh", "smbd", "mdns-advertiser", "nbns-advertiser", "wcifsfs", "/mnt/Flash/rc.local"],
        )
        self.assertEqual(actions_mock.call_args_list[2].kwargs, {})
        verify_mock.assert_called_once_with("root@10.0.0.2", "pw", "-o foo")
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
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule",
            "TC_SAMBA_USER": "admin",
        }
        with mock.patch("timecapsulesmb.cli.deploy.parse_env_values", return_value=values):
            with mock.patch("timecapsulesmb.cli.deploy.validate_artifacts", return_value=[("smbd-netbsd4", True, "ok")]):
                with mock.patch("timecapsulesmb.cli.deploy.discover_volume_root", return_value="/Volumes/dk2"):
                    with mock.patch("timecapsulesmb.cli.deploy.probe_device_compatibility", return_value=self.make_supported_netbsd4_compatibility()):
                        with mock.patch("timecapsulesmb.cli.deploy.run_remote_actions") as actions_mock:
                            with mock.patch("timecapsulesmb.cli.deploy.remote_ensure_adisk_uuid", return_value=""):
                                with mock.patch("timecapsulesmb.cli.deploy.upload_deployment_payload"):
                                    with mock.patch("timecapsulesmb.cli.deploy.remote_install_auth_files"):
                                        with mock.patch("timecapsulesmb.cli.deploy.verify_netbsd4_activation", return_value=True):
                                            with mock.patch("timecapsulesmb.cli.deploy.run_ssh") as run_ssh_mock:
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
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule",
            "TC_SAMBA_USER": "admin",
        }
        with mock.patch("timecapsulesmb.cli.deploy.parse_env_values", return_value=values):
            with mock.patch("timecapsulesmb.cli.deploy.validate_artifacts", return_value=[("smbd-netbsd4", True, "ok")]):
                with mock.patch("timecapsulesmb.cli.deploy.discover_volume_root", return_value="/Volumes/dk2"):
                    with mock.patch("timecapsulesmb.cli.deploy.probe_device_compatibility", return_value=self.make_supported_netbsd4_compatibility()):
                        with mock.patch("timecapsulesmb.cli.deploy.run_remote_actions"):
                            with mock.patch("timecapsulesmb.cli.deploy.remote_ensure_adisk_uuid", return_value=""):
                                with mock.patch("timecapsulesmb.cli.deploy.upload_deployment_payload"):
                                    with mock.patch("timecapsulesmb.cli.deploy.remote_install_auth_files"):
                                        with mock.patch("timecapsulesmb.cli.deploy.verify_netbsd4_activation", return_value=False):
                                            with redirect_stdout(output):
                                                rc = deploy.main(["--yes"])
        self.assertEqual(rc, 1)
        self.assertIn("NetBSD4 activation failed.", output.getvalue())

    def test_deploy_install_nbns_rejects_netbsd4_device(self) -> None:
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
            "TC_SAMBA_USER": "admin",
        }
        with mock.patch("timecapsulesmb.cli.deploy.parse_env_values", return_value=values):
            with mock.patch("timecapsulesmb.cli.deploy.validate_artifacts", return_value=[("smbd-netbsd4", True, "ok")]):
                with mock.patch("timecapsulesmb.cli.deploy.discover_volume_root", return_value="/Volumes/dk2"):
                    with mock.patch("timecapsulesmb.cli.deploy.probe_device_compatibility", return_value=self.make_supported_netbsd4_compatibility()):
                        with self.assertRaises(SystemExit) as cm:
                            deploy.main(["--install-nbns", "--no-reboot"])
        self.assertIn("NBNS responder cannot be enabled on NetBSD4", str(cm.exception))

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
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule",
            "TC_SAMBA_USER": "admin",
        }
        with mock.patch("timecapsulesmb.cli.deploy.parse_env_values", return_value=values):
            with mock.patch("timecapsulesmb.cli.deploy.validate_artifacts", return_value=[("smbd", True, "ok"), ("mdns", True, "ok"), ("nbns", True, "ok")]):
                with mock.patch("timecapsulesmb.cli.deploy.discover_volume_root", return_value="/Volumes/dk2"):
                    with mock.patch("timecapsulesmb.cli.deploy.probe_device_compatibility", return_value=self.make_supported_compatibility()):
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
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule",
            "TC_SAMBA_USER": "admin",
        }
        unsupported = DeviceCompatibility(
            os_name="Linux",
            os_release="6.8",
            arch="armv7",
            payload_family=None,
            device_generation="unknown",
            mdns_device_model_hint="TimeCapsule",
            supported=False,
            message="Unsupported device OS: Linux 6.8.",
        )
        with mock.patch("timecapsulesmb.cli.deploy.parse_env_values", return_value=values):
            with mock.patch("timecapsulesmb.cli.deploy.validate_artifacts", return_value=[("smbd", True, "ok"), ("mdns", True, "ok")]):
                with mock.patch("timecapsulesmb.cli.deploy.discover_volume_root", return_value="/Volumes/dk2"):
                    with mock.patch("timecapsulesmb.cli.deploy.probe_device_compatibility", return_value=unsupported):
                        with self.assertRaises(SystemExit) as ctx:
                            deploy.main(["--dry-run"])
        self.assertIn("Linux", str(ctx.exception))

    def test_deploy_can_allow_unsupported_device(self) -> None:
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
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule",
            "TC_SAMBA_USER": "admin",
        }
        unsupported = DeviceCompatibility(
            os_name="Linux",
            os_release="6.8",
            arch="armv7",
            payload_family=None,
            device_generation="unknown",
            mdns_device_model_hint="TimeCapsule",
            supported=False,
            message="Unsupported device OS: Linux 6.8.",
        )
        with mock.patch("timecapsulesmb.cli.deploy.parse_env_values", return_value=values):
            with mock.patch("timecapsulesmb.cli.deploy.validate_artifacts", return_value=[("smbd", True, "ok"), ("mdns", True, "ok")]):
                with mock.patch("timecapsulesmb.cli.deploy.discover_volume_root", return_value="/Volumes/dk2"):
                    with mock.patch("timecapsulesmb.cli.deploy.probe_device_compatibility", return_value=unsupported):
                        with mock.patch("timecapsulesmb.cli.deploy.build_device_paths", return_value=mock.Mock()):
                            with mock.patch("timecapsulesmb.cli.deploy.build_deployment_plan", return_value={"plan": "ok"}):
                                with mock.patch("timecapsulesmb.cli.deploy.format_deployment_plan", return_value="plan text"):
                                    with redirect_stdout(output):
                                        rc = deploy.main(["--dry-run", "--allow-unsupported"])
        self.assertEqual(rc, 0)
        text = output.getvalue()
        self.assertIn("Warning:", text)
        self.assertIn("--allow-unsupported", text)
        self.assertIn("Linux", text)

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
        with mock.patch("timecapsulesmb.cli.doctor.parse_env_values", return_value={}):
            with mock.patch("timecapsulesmb.cli.doctor.run_doctor_checks", return_value=([fake_result], False)):
                with redirect_stdout(output):
                    rc = doctor.main(["--json"])
        self.assertEqual(rc, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["fatal"], False)
        self.assertEqual(payload["results"][0]["status"], "PASS")

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
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule",
            "TC_SAMBA_USER": "admin",
        }
        with mock.patch("timecapsulesmb.cli.deploy.parse_env_values", return_value=values):
            with mock.patch("timecapsulesmb.cli.deploy.validate_artifacts", return_value=[("smbd", True, "ok"), ("mdns", True, "ok"), ("nbns", True, "ok")]):
                with mock.patch("timecapsulesmb.cli.deploy.discover_volume_root", return_value="/Volumes/dk2"):
                    with mock.patch("timecapsulesmb.cli.deploy.probe_device_compatibility", return_value=self.make_supported_compatibility()):
                        with redirect_stdout(output):
                            rc = deploy.main(["--dry-run", "--json"])
        self.assertEqual(rc, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["host"], "root@10.0.0.2")
        self.assertEqual(payload["volume_root"], "/Volumes/dk2")
        self.assertTrue(payload["nbns_path"].endswith("/bin/nbns/nbns-advertiser"))
        self.assertEqual(payload["payload_targets"]["nbns-advertiser"], f"/Volumes/dk2/{values['TC_PAYLOAD_DIR_NAME']}/nbns-advertiser")
        self.assertIn("post_deploy_checks", payload)

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
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule",
            "TC_SAMBA_USER": "admin",
        }
        with mock.patch("timecapsulesmb.cli.deploy.parse_env_values", return_value=values):
            with mock.patch("timecapsulesmb.cli.deploy.validate_artifacts", return_value=[("smbd-netbsd4", True, "ok")]):
                with mock.patch("timecapsulesmb.cli.deploy.discover_volume_root", return_value="/Volumes/dk2"):
                    with mock.patch("timecapsulesmb.cli.deploy.probe_device_compatibility", return_value=self.make_supported_netbsd4_compatibility()):
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
            payload["post_deploy_checks"],
            ["netbsd4_smbd_bound_445", "netbsd4_mdns_bound_5353"],
        )

    def test_activate_dry_run_prints_netbsd4_activation_plan(self) -> None:
        output = io.StringIO()
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
        }
        with mock.patch("timecapsulesmb.cli.activate.parse_env_values", return_value=values):
            with mock.patch("timecapsulesmb.cli.activate.probe_device_compatibility", return_value=self.make_supported_netbsd4_compatibility()):
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
        self.assertIn("Tested NetBSD4 devices need this after reboot", text)
        self.assertIn("other NetBSD4 generations may auto-start", text)

    def test_activate_rejects_non_netbsd4_device(self) -> None:
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
        }
        with mock.patch("timecapsulesmb.cli.activate.parse_env_values", return_value=values):
            with mock.patch("timecapsulesmb.cli.activate.probe_device_compatibility", return_value=self.make_supported_compatibility()):
                with self.assertRaises(SystemExit) as cm:
                    activate.main(["--dry-run"])
        self.assertIn("only supported for NetBSD4", str(cm.exception))

    def test_activate_prompt_decline_cancels_before_remote_actions(self) -> None:
        output = io.StringIO()
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
        }
        with mock.patch("timecapsulesmb.cli.activate.parse_env_values", return_value=values):
            with mock.patch("timecapsulesmb.cli.activate.probe_device_compatibility", return_value=self.make_supported_netbsd4_compatibility()):
                with mock.patch("builtins.input", return_value="n"):
                    with mock.patch("timecapsulesmb.cli.activate.run_remote_actions") as actions_mock:
                        with redirect_stdout(output):
                            rc = activate.main([])
        self.assertEqual(rc, 0)
        actions_mock.assert_not_called()
        self.assertIn("Activation cancelled.", output.getvalue())

    def test_activate_yes_runs_idempotent_actions_and_verifies(self) -> None:
        output = io.StringIO()
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
        }
        with mock.patch("timecapsulesmb.cli.activate.parse_env_values", return_value=values):
            with mock.patch("timecapsulesmb.cli.activate.probe_device_compatibility", return_value=self.make_supported_netbsd4_compatibility()):
                with mock.patch("timecapsulesmb.cli.activate.netbsd4_activation_is_already_healthy", return_value=False):
                    with mock.patch("timecapsulesmb.cli.activate.run_remote_actions") as actions_mock:
                        with mock.patch("timecapsulesmb.cli.activate.verify_netbsd4_activation", return_value=True) as verify_mock:
                            with redirect_stdout(output):
                                rc = activate.main(["--yes"])
        self.assertEqual(rc, 0)
        actions_mock.assert_called_once()
        action_kinds = [action.kind for action in actions_mock.call_args.args[3]]
        action_args = [action.args[0] for action in actions_mock.call_args.args[3]]
        self.assertEqual(
            action_kinds,
            ["stop_process_full", "stop_process", "stop_process", "stop_process", "stop_process", "run_script"],
        )
        self.assertEqual(
            action_args,
            ["[w]atchdog.sh", "smbd", "mdns-advertiser", "nbns-advertiser", "wcifsfs", "/mnt/Flash/rc.local"],
        )
        self.assertEqual(actions_mock.call_args.kwargs, {})
        verify_mock.assert_called_once_with("root@10.0.0.2", "pw", "-o foo")
        self.assertIn("without file transfer", output.getvalue())

    def test_activate_skips_rc_local_when_payload_is_already_healthy(self) -> None:
        output = io.StringIO()
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
        }
        with mock.patch("timecapsulesmb.cli.activate.parse_env_values", return_value=values):
            with mock.patch("timecapsulesmb.cli.activate.probe_device_compatibility", return_value=self.make_supported_netbsd4_compatibility()):
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
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
        }
        with mock.patch("timecapsulesmb.cli.activate.parse_env_values", return_value=values):
            with mock.patch("timecapsulesmb.cli.activate.probe_device_compatibility", return_value=self.make_supported_netbsd4_compatibility()):
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
        with mock.patch("timecapsulesmb.cli.uninstall.parse_env_values", return_value=values):
            with mock.patch("timecapsulesmb.cli.uninstall.discover_volume_root", return_value="/Volumes/dk2"):
                with redirect_stdout(output):
                    rc = uninstall.main(["--dry-run"])
        self.assertEqual(rc, 0)
        text = output.getvalue()
        self.assertIn("Dry run: uninstall plan", text)
        self.assertIn("host: root@10.0.0.2", text)
        self.assertIn(f"payload dir: /Volumes/dk2/{values['TC_PAYLOAD_DIR_NAME']}", text)

    def test_uninstall_json_outputs_plan(self) -> None:
        output = io.StringIO()
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
            "TC_PAYLOAD_DIR_NAME": "samba4",
        }
        with mock.patch("timecapsulesmb.cli.uninstall.parse_env_values", return_value=values):
            with mock.patch("timecapsulesmb.cli.uninstall.discover_volume_root", return_value="/Volumes/dk2"):
                with redirect_stdout(output):
                    rc = uninstall.main(["--dry-run", "--json"])
        self.assertEqual(rc, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["host"], "root@10.0.0.2")
        self.assertEqual(payload["volume_root"], "/Volumes/dk2")
        self.assertIn("post_uninstall_checks", payload)

    def test_uninstall_yes_reboots_and_verifies(self) -> None:
        output = io.StringIO()
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
            "TC_PAYLOAD_DIR_NAME": "samba4",
        }
        with mock.patch("timecapsulesmb.cli.uninstall.parse_env_values", return_value=values):
            with mock.patch("timecapsulesmb.cli.uninstall.discover_volume_root", return_value="/Volumes/dk2"):
                with mock.patch("timecapsulesmb.cli.uninstall.remote_uninstall_payload") as uninstall_mock:
                    with mock.patch("timecapsulesmb.cli.uninstall.run_ssh") as run_ssh_mock:
                        with mock.patch("timecapsulesmb.cli.uninstall.wait_for_ssh_state", side_effect=[True, True]) as wait_mock:
                            with mock.patch("timecapsulesmb.cli.uninstall.verify_post_uninstall", return_value=True) as verify_mock:
                                with redirect_stdout(output):
                                    rc = uninstall.main(["--yes"])
        self.assertEqual(rc, 0)
        uninstall_mock.assert_called_once()
        run_ssh_mock.assert_called_once()
        self.assertEqual(
            wait_mock.call_args_list[0].args,
            ("root@10.0.0.2", "pw", "-o foo"),
        )
        self.assertEqual(wait_mock.call_args_list[0].kwargs, {"expected_up": False, "timeout_seconds": 60})
        self.assertEqual(
            wait_mock.call_args_list[1].args,
            ("root@10.0.0.2", "pw", "-o foo"),
        )
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
        with mock.patch("timecapsulesmb.cli.uninstall.parse_env_values", return_value=values):
            with mock.patch("timecapsulesmb.cli.uninstall.discover_volume_root", return_value="/Volumes/dk2"):
                with mock.patch("timecapsulesmb.cli.uninstall.remote_uninstall_payload") as uninstall_mock:
                    with mock.patch("timecapsulesmb.cli.uninstall.run_ssh") as run_ssh_mock:
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
        with mock.patch("timecapsulesmb.cli.uninstall.parse_env_values", return_value=values):
            with mock.patch("timecapsulesmb.cli.uninstall.discover_volume_root", return_value="/Volumes/dk2"):
                with mock.patch("timecapsulesmb.cli.uninstall.remote_uninstall_payload"):
                    with mock.patch("builtins.input", return_value="n"):
                        with mock.patch("timecapsulesmb.cli.uninstall.run_ssh") as run_ssh_mock:
                            with redirect_stdout(output):
                                rc = uninstall.main([])
        self.assertEqual(rc, 0)
        run_ssh_mock.assert_not_called()
        self.assertIn("Skipped reboot.", output.getvalue())

    def test_fsck_yes_reboots_and_waits_by_default(self) -> None:
        output = io.StringIO()
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
        }
        mounted = MountedVolume(device="/dev/dk2", mountpoint="/Volumes/dk2")
        run_result = mock.Mock(stdout="--- fsck_hfs /dev/dk2 ---\nOK\n--- reboot ---\n", returncode=255)
        with mock.patch("timecapsulesmb.cli.fsck.parse_env_values", return_value=values):
            with mock.patch("timecapsulesmb.cli.fsck.discover_mounted_volume", return_value=mounted):
                with mock.patch("timecapsulesmb.cli.fsck.run_ssh", return_value=run_result) as run_ssh_mock:
                    with mock.patch("timecapsulesmb.cli.fsck.wait_for_ssh_state", side_effect=[True, True]) as wait_mock:
                        with redirect_stdout(output):
                            rc = fsck.main(["--yes"])
        self.assertEqual(rc, 0)
        run_ssh_mock.assert_called_once()
        remote_cmd = run_ssh_mock.call_args.args[3]
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

    def test_fsck_no_wait_skips_ssh_waits(self) -> None:
        output = io.StringIO()
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
        }
        mounted = MountedVolume(device="/dev/dk2", mountpoint="/Volumes/dk2")
        run_result = mock.Mock(stdout="--- fsck_hfs /dev/dk2 ---\nOK\n--- reboot ---\n", returncode=255)
        with mock.patch("timecapsulesmb.cli.fsck.parse_env_values", return_value=values):
            with mock.patch("timecapsulesmb.cli.fsck.discover_mounted_volume", return_value=mounted):
                with mock.patch("timecapsulesmb.cli.fsck.run_ssh", return_value=run_result):
                    with mock.patch("timecapsulesmb.cli.fsck.wait_for_ssh_state") as wait_mock:
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
        with mock.patch("timecapsulesmb.cli.fsck.parse_env_values", return_value=values):
            with mock.patch("timecapsulesmb.cli.fsck.discover_mounted_volume", return_value=mounted):
                with mock.patch("timecapsulesmb.cli.fsck.run_ssh", return_value=run_result) as run_ssh_mock:
                    with mock.patch("timecapsulesmb.cli.fsck.wait_for_ssh_state") as wait_mock:
                        with redirect_stdout(output):
                            rc = fsck.main(["--yes", "--no-reboot"])
        self.assertEqual(rc, 0)
        wait_mock.assert_not_called()
        self.assertNotIn("/sbin/reboot", run_ssh_mock.call_args.args[3])

    def test_fsck_prompt_decline_cancels_before_remote_actions(self) -> None:
        output = io.StringIO()
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
        }
        mounted = MountedVolume(device="/dev/dk2", mountpoint="/Volumes/dk2")
        with mock.patch("timecapsulesmb.cli.fsck.parse_env_values", return_value=values):
            with mock.patch("timecapsulesmb.cli.fsck.discover_mounted_volume", return_value=mounted):
                with mock.patch("builtins.input", return_value="n"):
                    with mock.patch("timecapsulesmb.cli.fsck.run_ssh") as run_ssh_mock:
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
