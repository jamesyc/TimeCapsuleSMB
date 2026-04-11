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

from timecapsulesmb.cli import bootstrap, configure, deploy, discover, doctor, prep_device, uninstall
from timecapsulesmb.cli.main import main
from timecapsulesmb.device.compat import DeviceCompatibility
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

    def test_dispatches_to_command_handler(self) -> None:
        with mock.patch("timecapsulesmb.cli.main.COMMANDS", {"doctor": mock.Mock(return_value=7)}):
            rc = main(["doctor", "--skip-smb"])
        self.assertEqual(rc, 7)

    def test_bootstrap_prints_full_next_steps(self) -> None:
        output = io.StringIO()
        with mock.patch("pathlib.Path.exists", return_value=True):
            with mock.patch("timecapsulesmb.cli.bootstrap.ensure_venv", return_value=bootstrap.VENVDIR / "bin" / "python"):
                with mock.patch("timecapsulesmb.cli.bootstrap.install_python_requirements"):
                    with mock.patch("timecapsulesmb.cli.bootstrap.maybe_install_airpyrt"):
                        with redirect_stdout(output):
                            rc = bootstrap.main(["--skip-airpyrt"])
        self.assertEqual(rc, 0)
        text = output.getvalue()
        self.assertIn("prep-device", text)
        self.assertIn("configure", text)
        self.assertIn("deploy", text)
        self.assertIn("doctor", text)

    def test_bootstrap_explains_long_running_airpyrt_step(self) -> None:
        output = io.StringIO()
        with mock.patch("pathlib.Path.exists", return_value=True):
            with mock.patch("timecapsulesmb.cli.bootstrap.ensure_venv", return_value=bootstrap.VENVDIR / "bin" / "python"):
                with mock.patch("timecapsulesmb.cli.bootstrap.install_python_requirements"):
                    with mock.patch("timecapsulesmb.cli.bootstrap.run") as run_mock:
                        with mock.patch("timecapsulesmb.cli.bootstrap.shutil.which", return_value="/usr/bin/make"):
                            with mock.patch("timecapsulesmb.cli.bootstrap.confirm", return_value=True):
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
                    with mock.patch("timecapsulesmb.cli.bootstrap.run") as run_mock:
                        with mock.patch("timecapsulesmb.cli.bootstrap.shutil.which", return_value="/usr/bin/make"):
                            with mock.patch("timecapsulesmb.cli.bootstrap.confirm", return_value=False):
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
                    with mock.patch(
                        "timecapsulesmb.cli.bootstrap.run",
                        side_effect=subprocess.CalledProcessError(2, ["make", "airpyrt"]),
                    ):
                        with mock.patch("timecapsulesmb.cli.bootstrap.shutil.which", return_value="/usr/bin/make"):
                            with mock.patch("timecapsulesmb.cli.bootstrap.confirm", return_value=True):
                                with redirect_stdout(output):
                                    rc = bootstrap.main([])
        self.assertEqual(rc, 0)
        text = output.getvalue()
        self.assertIn("Warning: AirPyrt setup failed", text)
        self.assertIn("Host setup complete.", text)

    def test_configure_writes_values_from_prompts(self) -> None:
        output = io.StringIO()
        fake_values = {}

        def fake_write_env_file(path, values):
            fake_values.update(values)

        with mock.patch("timecapsulesmb.cli.configure.parse_env_values", return_value={}):
            with mock.patch("timecapsulesmb.cli.configure.discover", return_value=[]):
                with mock.patch("timecapsulesmb.cli.configure.prompt", side_effect=lambda _l, default, _s: default or "pw"):
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
            "TimeCapsule",
        ])

        def fake_prompt(label, default, _secret):
            if label == "Time Capsule SSH target":
                return default
            return next(prompt_values)

        with mock.patch("timecapsulesmb.cli.configure.parse_env_values", return_value={}):
            with mock.patch("timecapsulesmb.cli.configure.discover", return_value=[record]):
                with mock.patch("timecapsulesmb.cli.configure.prefer_routable_ipv4", return_value="10.0.0.2"):
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

    def test_configure_preserves_existing_mdns_device_model_override(self) -> None:
        output = io.StringIO()
        fake_values = {}
        existing = {
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
            "TimeCapsule",
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
            "TimeCapsule",
        ])

        def fake_prompt(_label, _default, _secret):
            return next(prompt_values)

        def fake_write_env_file(_path, values):
            fake_values.update(values)

        with mock.patch("timecapsulesmb.cli.configure.parse_env_values", return_value={}):
            with mock.patch("timecapsulesmb.cli.configure.discover", return_value=[]):
                with mock.patch("timecapsulesmb.cli.configure.prompt", side_effect=fake_prompt):
                    with mock.patch("timecapsulesmb.cli.configure.tcp_open", return_value=True):
                        with mock.patch("timecapsulesmb.cli.configure.validate_ssh_target", side_effect=[False, True]):
                            with mock.patch("timecapsulesmb.cli.configure.confirm", return_value=False):
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
            "TimeCapsule",
        ])

        def fake_prompt(_label, _default, _secret):
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
            "TimeCapsule",
        ])

        def fake_prompt(_label, _default, _secret):
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
                        with mock.patch("timecapsulesmb.cli.deploy.remote_prepare_dirs") as prepare_mock:
                            with mock.patch("timecapsulesmb.cli.deploy.upload_deployment_payload") as upload_mock:
                                with mock.patch("timecapsulesmb.cli.deploy.remote_install_auth_files") as auth_mock:
                                    with mock.patch("timecapsulesmb.cli.deploy.remote_install_permissions") as perm_mock:
                                        with redirect_stdout(output):
                                            rc = deploy.main(["--dry-run"])
        self.assertEqual(rc, 0)
        text = output.getvalue()
        self.assertIn("Dry run: deployment plan", text)
        self.assertIn("host: root@10.0.0.2", text)
        self.assertIn("volume root: /Volumes/dk2", text)
        self.assertIn("generated smbpasswd", text)
        prepare_mock.assert_not_called()
        upload_mock.assert_not_called()
        auth_mock.assert_not_called()
        perm_mock.assert_not_called()

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
                        with mock.patch("timecapsulesmb.cli.deploy.remote_prepare_dirs"):
                            with mock.patch("timecapsulesmb.cli.deploy.upload_deployment_payload"):
                                with mock.patch("timecapsulesmb.cli.deploy.remote_install_auth_files"):
                                    with mock.patch("timecapsulesmb.cli.deploy.remote_install_permissions"):
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
                        with mock.patch("timecapsulesmb.cli.deploy.remote_prepare_dirs"):
                            with mock.patch("timecapsulesmb.cli.deploy.upload_deployment_payload"):
                                with mock.patch("timecapsulesmb.cli.deploy.remote_install_auth_files"):
                                    with mock.patch("timecapsulesmb.cli.deploy.remote_install_permissions"):
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
                        with mock.patch("timecapsulesmb.cli.deploy.remote_prepare_dirs"):
                            with mock.patch("timecapsulesmb.cli.deploy.upload_deployment_payload"):
                                with mock.patch("timecapsulesmb.cli.deploy.remote_install_auth_files"):
                                    with mock.patch("timecapsulesmb.cli.deploy.remote_install_permissions"):
                                        with mock.patch("builtins.input", return_value="y"):
                                            with mock.patch("timecapsulesmb.cli.deploy.run_ssh"):
                                                with mock.patch("timecapsulesmb.cli.deploy.wait_for_ssh_state", side_effect=[True, False]):
                                                    with redirect_stdout(output):
                                                        rc = deploy.main([])
        self.assertEqual(rc, 1)
        self.assertIn("Timed out waiting for SSH after reboot.", output.getvalue())

    def test_deploy_rejects_unsupported_netbsd4_device(self) -> None:
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
            os_name="NetBSD",
            os_release="4.0",
            arch="earmv4",
            payload_family=None,
            device_generation="gen1-4",
            mdns_device_model_hint="TimeCapsule6,106",
            supported=False,
            message="This Time Capsule is running NetBSD 4, which is an older 4th gen or earlier model. The checked-in Samba payload only supports NetBSD 6 (5th gen) devices right now.",
        )
        with mock.patch("timecapsulesmb.cli.deploy.parse_env_values", return_value=values):
            with mock.patch("timecapsulesmb.cli.deploy.validate_artifacts", return_value=[("smbd", True, "ok"), ("mdns", True, "ok")]):
                with mock.patch("timecapsulesmb.cli.deploy.discover_volume_root", return_value="/Volumes/dk2"):
                    with mock.patch("timecapsulesmb.cli.deploy.probe_device_compatibility", return_value=unsupported):
                        with self.assertRaises(SystemExit) as ctx:
                            deploy.main(["--dry-run"])
        self.assertIn("NetBSD 4", str(ctx.exception))

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
            with mock.patch("timecapsulesmb.cli.deploy.validate_artifacts", return_value=[("smbd", True, "ok"), ("mdns", True, "ok")]):
                with mock.patch("timecapsulesmb.cli.deploy.discover_volume_root", return_value="/Volumes/dk2"):
                    with mock.patch("timecapsulesmb.cli.deploy.probe_device_compatibility", return_value=self.make_supported_compatibility()):
                        with redirect_stdout(output):
                            rc = deploy.main(["--dry-run", "--json"])
        self.assertEqual(rc, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["host"], "root@10.0.0.2")
        self.assertEqual(payload["volume_root"], "/Volumes/dk2")
        self.assertIn("post_deploy_checks", payload)

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
        self.assertIn("payload dir: /Volumes/dk2/samba4", text)

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
                        with mock.patch("timecapsulesmb.cli.uninstall.wait_for_ssh_state", side_effect=[True, True]):
                            with mock.patch("timecapsulesmb.cli.uninstall.verify_post_uninstall", return_value=True) as verify_mock:
                                with redirect_stdout(output):
                                    rc = uninstall.main(["--yes"])
        self.assertEqual(rc, 0)
        uninstall_mock.assert_called_once()
        run_ssh_mock.assert_called_once()
        verify_mock.assert_called_once()
        self.assertIn("Device is back online.", output.getvalue())

    def test_uninstall_declined_reboot_returns_failure(self) -> None:
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
        self.assertEqual(rc, 1)
        run_ssh_mock.assert_not_called()
        self.assertIn("Uninstall requires a reboot to complete.", output.getvalue())

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
