"""The bootstrap command."""
from __future__ import annotations

import io
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock
from timecapsulesmb.cli import bootstrap

from tests.cli_support import CliTestCase


class CliBootstrapTests(CliTestCase):
    def test_bootstrap_prints_full_next_steps(self) -> None:
        output = io.StringIO()
        with mock.patch("pathlib.Path.exists", return_value=True):
            with mock.patch("timecapsulesmb.cli.bootstrap.ensure_venv", return_value=bootstrap.VENVDIR / "bin" / "python"):
                with mock.patch("timecapsulesmb.cli.bootstrap.install_python_requirements"):
                    with mock.patch("timecapsulesmb.cli.bootstrap.install_required_host_tools"):
                        with mock.patch("timecapsulesmb.cli.bootstrap.ensure_install_id"):
                            with mock.patch("timecapsulesmb.cli.bootstrap.current_platform_label", return_value="macOS"):
                                with mock.patch("timecapsulesmb.cli.bootstrap.validate_selected_python", return_value="3.11.9"):
                                    with mock.patch("timecapsulesmb.cli.bootstrap.check_macos_host_tool_install_support", return_value={}):
                                        with redirect_stdout(output):
                                            rc = bootstrap.main([])
        self.assertEqual(rc, 0)
        text = output.getvalue()
        self.assertIn("Detected host platform", text)
        self.assertIn("configure", text)
        self.assertIn("deploy", text)
        self.assertIn("doctor", text)
        self.assertIn("activate", text)
        self.assertNotIn("set-ssh", text)
        started = self.telemetry_payload("bootstrap_started")
        finished = self.telemetry_payload("bootstrap_finished")
        self.assertEqual(started["python_executable"], sys.executable)
        self.assertEqual(finished["result"], "success")
        self.assertEqual(finished["host_platform_label"], "macOS")
        self.assertEqual(finished["selected_python_version"], "3.11.9")

    def test_bootstrap_prints_same_core_next_steps_on_linux(self) -> None:
        output = io.StringIO()
        with mock.patch("timecapsulesmb.cli.bootstrap.current_platform_label", return_value="Linux"):
            with mock.patch("pathlib.Path.exists", return_value=True):
                with mock.patch("timecapsulesmb.cli.bootstrap.ensure_venv", return_value=bootstrap.VENVDIR / "bin" / "python"):
                    with mock.patch("timecapsulesmb.cli.bootstrap.install_python_requirements"):
                        with mock.patch("timecapsulesmb.cli.bootstrap.install_required_host_tools"):
                            with mock.patch("timecapsulesmb.cli.bootstrap.ensure_install_id"):
                                with mock.patch("timecapsulesmb.cli.bootstrap.validate_selected_python", return_value="3.11.9"):
                                    with redirect_stdout(output):
                                        rc = bootstrap.main([])
        self.assertEqual(rc, 0)
        text = output.getvalue()
        self.assertIn("Detected host platform: Linux", text)
        self.assertIn("configure", text)
        self.assertIn("deploy", text)
        self.assertIn("doctor", text)
        self.assertIn("activate", text)
        self.assertNotIn("set-ssh", text)
        self.assertNotIn("AirPyrt", text)

    def test_bootstrap_rejects_removed_skip_airpyrt_flag(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as ctx:
                bootstrap.main(["--skip-airpyrt"])
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("unrecognized arguments: --skip-airpyrt", stderr.getvalue())

    def test_bootstrap_returns_error_when_requirements_missing(self) -> None:
        stderr = io.StringIO()
        with mock.patch("pathlib.Path.exists", return_value=False):
            with mock.patch("timecapsulesmb.cli.bootstrap.ensure_install_id"):
                with redirect_stderr(stderr):
                    rc = bootstrap.main([])
        self.assertEqual(rc, 1)
        self.assertIn("Missing", stderr.getvalue())
        finished = self.telemetry_payload("bootstrap_finished")
        self.assertEqual(finished["result"], "failure")
        self.assertEqual(finished["requirements_present"], False)
        self.assertIn("stage=validate_requirements", finished["error"])

    def test_bootstrap_telemetry_error_includes_command_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            requirements = root / "requirements.txt"
            requirements.write_text("zeroconf\n")
            venv = root / ".venv"
            command_stderr = "The virtual environment was not created successfully because ensurepip is not available.\n"
            failed = subprocess.CompletedProcess(["/usr/bin/python3", "-m", "venv", str(venv)], 1, "", command_stderr)

            with mock.patch("timecapsulesmb.cli.bootstrap.REPO_ROOT", root):
                with mock.patch("timecapsulesmb.cli.bootstrap.REQUIREMENTS", requirements):
                    with mock.patch("timecapsulesmb.cli.bootstrap.VENVDIR", venv):
                        with mock.patch("timecapsulesmb.cli.bootstrap.ensure_install_id"):
                            with mock.patch("timecapsulesmb.cli.bootstrap.current_platform_label", return_value="Linux"):
                                with mock.patch("timecapsulesmb.cli.bootstrap.validate_selected_python", return_value="3.11.9"):
                                    with mock.patch("timecapsulesmb.cli.bootstrap.subprocess.run", return_value=failed):
                                        rc = bootstrap.main(["--python", "/usr/bin/python3"])

        self.assertEqual(rc, 1)
        finished = self.telemetry_payload("bootstrap_finished")
        error = finished["error"]
        self.assertIn("Command failed with exit code 1", error)
        self.assertIn("stderr:", error)
        self.assertIn("ensurepip is not available", error)
        self.assertIn("Debug context:", error)
        self.assertIn("stage=ensure_venv", error)

    def test_bootstrap_telemetry_error_uses_stdout_when_stderr_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            requirements = root / "requirements.txt"
            requirements.write_text("zeroconf\n")
            venv = root / ".venv"
            failed = subprocess.CompletedProcess(["python3", "-m", "venv", str(venv)], 1, "stdout failure\n", "")

            with mock.patch("timecapsulesmb.cli.bootstrap.REPO_ROOT", root):
                with mock.patch("timecapsulesmb.cli.bootstrap.REQUIREMENTS", requirements):
                    with mock.patch("timecapsulesmb.cli.bootstrap.VENVDIR", venv):
                        with mock.patch("timecapsulesmb.cli.bootstrap.ensure_install_id"):
                            with mock.patch("timecapsulesmb.cli.bootstrap.current_platform_label", return_value="Linux"):
                                with mock.patch("timecapsulesmb.cli.bootstrap.validate_selected_python", return_value="3.11.9"):
                                    with mock.patch("timecapsulesmb.cli.bootstrap.subprocess.run", return_value=failed):
                                        rc = bootstrap.main(["--python", "python3"])

        self.assertEqual(rc, 1)
        error = self.telemetry_payload("bootstrap_finished")["error"]
        self.assertIn("stdout:", error)
        self.assertIn("stdout failure", error)
        self.assertIn("stage=ensure_venv", error)

    def test_bootstrap_command_error_output_is_truncated(self) -> None:
        message = bootstrap._format_command_error(
            bootstrap.BootstrapCommandError(
                ["python3", "-m", "venv", ".venv"],
                1,
                "",
                "x" * (bootstrap.COMMAND_OUTPUT_ERROR_LIMIT + 7),
            )
        )

        self.assertIn("stderr:", message)
        self.assertIn("...<truncated 7 chars>", message)
        self.assertLess(len(message), bootstrap.COMMAND_OUTPUT_ERROR_LIMIT + 200)

    def test_bootstrap_rejects_selected_python_older_than_minimum_before_venv(self) -> None:
        stderr = io.StringIO()
        with mock.patch("pathlib.Path.exists", return_value=True):
            with mock.patch("timecapsulesmb.cli.bootstrap.ensure_install_id"):
                with mock.patch("timecapsulesmb.cli.bootstrap.current_platform_label", return_value="Linux"):
                    with mock.patch("timecapsulesmb.cli.bootstrap.detect_selected_python_version", return_value="3.8.18"):
                        with mock.patch("timecapsulesmb.cli.bootstrap.ensure_venv") as ensure_venv:
                            with redirect_stderr(stderr):
                                rc = bootstrap.main(["--python", "/usr/local/bin/python3"])

        self.assertEqual(rc, 1)
        ensure_venv.assert_not_called()
        self.assertIn("requires Python 3.9 or newer", stderr.getvalue())
        finished = self.telemetry_payload("bootstrap_finished")
        self.assertEqual(finished["result"], "failure")
        self.assertIn("stage=check_python", finished["error"])

    def test_bootstrap_accepts_selected_python_at_minimum(self) -> None:
        output = io.StringIO()
        with mock.patch("timecapsulesmb.cli.bootstrap.detect_selected_python_version", return_value="3.9.0"):
            with redirect_stdout(output):
                version = bootstrap.validate_selected_python("/usr/local/bin/python3.9")

        self.assertEqual(version, "3.9.0")
        self.assertIn("Selected Python: /usr/local/bin/python3.9 (3.9.0)", output.getvalue())

    def test_bootstrap_blocks_old_macos_missing_tools_before_venv(self) -> None:
        output = io.StringIO()
        stderr = io.StringIO()

        def fake_which(name: str):
            if name == "sshpass":
                return "/usr/local/bin/sshpass"
            return None

        with mock.patch("pathlib.Path.exists", return_value=True):
            with mock.patch("timecapsulesmb.cli.bootstrap.ensure_install_id"):
                with mock.patch("timecapsulesmb.cli.bootstrap.current_platform_label", return_value="macOS"):
                    with mock.patch("timecapsulesmb.cli.bootstrap.validate_selected_python", return_value="3.11.9"):
                        with mock.patch("timecapsulesmb.cli.bootstrap.detect_macos_product_version", return_value="10.15.7"):
                            with mock.patch("timecapsulesmb.cli.bootstrap.find_command", side_effect=fake_which):
                                with mock.patch("timecapsulesmb.cli.bootstrap.ensure_venv") as ensure_venv:
                                    with mock.patch("timecapsulesmb.cli.bootstrap.install_required_host_tools") as install_tools:
                                        with redirect_stdout(output), redirect_stderr(stderr):
                                            rc = bootstrap.main([])

        self.assertEqual(rc, 1)
        ensure_venv.assert_not_called()
        install_tools.assert_not_called()
        text = output.getvalue()
        self.assertIn("Detected macOS version: 10.15.7", text)
        self.assertIn("Found sshpass: /usr/local/bin/sshpass", text)
        self.assertIn("Missing smbclient", text)
        self.assertIn("requires macOS 14.0 or newer", text)
        self.assertIn("\033[31m", text)
        self.assertIn("missing host tools (smbclient)", stderr.getvalue())
        finished = self.telemetry_payload("bootstrap_finished")
        self.assertEqual(finished["host_os_version"], "10.15.7")
        self.assertEqual(finished["macos_auto_host_tool_install_supported"], False)
        self.assertEqual(finished["missing_host_tools"], "smbclient")
        self.assertEqual(finished["sshpass_path"], "/usr/local/bin/sshpass")
        self.assertIn("stage=check_host_support", finished["error"])

    def test_bootstrap_old_macos_continues_when_required_tools_exist(self) -> None:
        output = io.StringIO()

        def fake_which(name: str):
            if name in {"sshpass", "smbclient"}:
                return f"/usr/local/bin/{name}"
            return None

        with mock.patch("pathlib.Path.exists", return_value=True):
            with mock.patch("timecapsulesmb.cli.bootstrap.ensure_venv", return_value=bootstrap.VENVDIR / "bin" / "python") as ensure_venv:
                with mock.patch("timecapsulesmb.cli.bootstrap.install_python_requirements"):
                    with mock.patch("timecapsulesmb.cli.bootstrap.install_required_host_tools"):
                        with mock.patch("timecapsulesmb.cli.bootstrap.ensure_install_id"):
                            with mock.patch("timecapsulesmb.cli.bootstrap.current_platform_label", return_value="macOS"):
                                with mock.patch("timecapsulesmb.cli.bootstrap.validate_selected_python", return_value="3.11.9"):
                                    with mock.patch("timecapsulesmb.cli.bootstrap.detect_macos_product_version", return_value="10.15.7"):
                                        with mock.patch("timecapsulesmb.cli.bootstrap.find_command", side_effect=fake_which):
                                            with redirect_stdout(output):
                                                rc = bootstrap.main([])

        self.assertEqual(rc, 0)
        ensure_venv.assert_called_once()
        text = output.getvalue()
        self.assertIn("Detected macOS version: 10.15.7", text)
        self.assertIn("Found sshpass: /usr/local/bin/sshpass", text)
        self.assertIn("Found smbclient: /usr/local/bin/smbclient", text)
        finished = self.telemetry_payload("bootstrap_finished")
        self.assertEqual(finished["host_os_version"], "10.15.7")
        self.assertEqual(finished["missing_host_tools"], "")

    def test_bootstrap_macos_14_allows_missing_tools_to_reach_installer(self) -> None:
        output = io.StringIO()
        with mock.patch("pathlib.Path.exists", return_value=True):
            with mock.patch("timecapsulesmb.cli.bootstrap.ensure_venv", return_value=bootstrap.VENVDIR / "bin" / "python"):
                with mock.patch("timecapsulesmb.cli.bootstrap.install_python_requirements"):
                    with mock.patch("timecapsulesmb.cli.bootstrap.install_required_host_tools") as install_tools:
                        with mock.patch("timecapsulesmb.cli.bootstrap.ensure_install_id"):
                            with mock.patch("timecapsulesmb.cli.bootstrap.current_platform_label", return_value="macOS"):
                                with mock.patch("timecapsulesmb.cli.bootstrap.validate_selected_python", return_value="3.11.9"):
                                    with mock.patch("timecapsulesmb.cli.bootstrap.detect_macos_product_version", return_value="14.0"):
                                        with mock.patch("timecapsulesmb.cli.bootstrap.find_command", return_value=None):
                                            with redirect_stdout(output):
                                                rc = bootstrap.main([])

        self.assertEqual(rc, 0)
        install_tools.assert_called_once()
        self.assertNotIn("requires macOS 14.0 or newer", output.getvalue())
        finished = self.telemetry_payload("bootstrap_finished")
        self.assertEqual(finished["macos_auto_host_tool_install_supported"], True)
        self.assertEqual(finished["missing_host_tools"], "sshpass, smbclient")

    def test_bootstrap_unknown_macos_version_missing_tools_fails_safely(self) -> None:
        output = io.StringIO()
        with mock.patch("timecapsulesmb.cli.bootstrap.detect_macos_product_version", return_value=None):
            with mock.patch("timecapsulesmb.cli.bootstrap.find_command", return_value=None):
                with self.assertRaises(bootstrap.BootstrapError):
                    with redirect_stdout(output):
                        bootstrap.check_macos_host_tool_install_support("macOS")

        text = output.getvalue()
        self.assertIn("Detected macOS version: unknown", text)
        self.assertIn("Missing sshpass", text)
        self.assertIn("Missing smbclient", text)

    def test_bootstrap_install_python_requirements_repairs_venv_without_pip(self) -> None:
        output = io.StringIO()
        venv_python = Path("/tmp/tcapsule-venv/bin/python")
        with mock.patch("timecapsulesmb.cli.bootstrap.venv_has_pip", return_value=False):
            with mock.patch("timecapsulesmb.cli.bootstrap.run") as run_mock:
                with redirect_stdout(output):
                    bootstrap.install_python_requirements(venv_python)

        self.assertIn("bootstrapping pip with ensurepip", output.getvalue())
        self.assertEqual(
            run_mock.call_args_list,
            [
                mock.call([str(venv_python), "-m", "ensurepip", "--upgrade"]),
                mock.call([str(venv_python), "-m", "pip", "install", "-U", "pip"]),
                mock.call([str(venv_python), "-m", "pip", "install", "-r", str(bootstrap.REQUIREMENTS)]),
                mock.call([str(venv_python), "-m", "pip", "install", "-e", str(bootstrap.REPO_ROOT)]),
            ],
        )

    def test_bootstrap_install_python_requirements_skips_ensurepip_when_pip_exists(self) -> None:
        venv_python = Path("/tmp/tcapsule-venv/bin/python")
        with mock.patch("timecapsulesmb.cli.bootstrap.venv_has_pip", return_value=True):
            with mock.patch("timecapsulesmb.cli.bootstrap.run") as run_mock:
                bootstrap.install_python_requirements(venv_python)

        commands = [call.args[0] for call in run_mock.call_args_list]
        self.assertNotIn([str(venv_python), "-m", "ensurepip", "--upgrade"], commands)
        self.assertEqual(commands[0], [str(venv_python), "-m", "pip", "install", "-U", "pip"])

    def test_bootstrap_skips_required_host_tools_when_present(self) -> None:
        output = io.StringIO()

        def fake_which(name: str):
            if name in {"sshpass", "smbclient"}:
                return f"/usr/bin/{name}"
            return None

        with mock.patch("timecapsulesmb.cli.bootstrap.find_command", side_effect=fake_which):
            with mock.patch("timecapsulesmb.cli.bootstrap.run") as run_mock:
                with redirect_stdout(output):
                    bootstrap.install_required_host_tools()

        self.assertIn("Found required host tools: sshpass, smbclient", output.getvalue())
        run_mock.assert_not_called()

    def test_bootstrap_installs_missing_host_tools_via_homebrew_on_macos(self) -> None:
        output = io.StringIO()
        installed: set[str] = set()

        def fake_which(name: str):
            if name == "brew":
                return "/opt/homebrew/bin/brew"
            if name in installed:
                return f"/opt/homebrew/bin/{name}"
            return None

        def fake_run(cmd, cwd=None):
            if cmd[:2] == ["/opt/homebrew/bin/brew", "install"]:
                for package in cmd[2:]:
                    if package == "sshpass":
                        installed.add("sshpass")
                    elif package == "samba":
                        installed.add("smbclient")

        with mock.patch("timecapsulesmb.cli.bootstrap.current_platform_label", return_value="macOS"):
            with mock.patch("timecapsulesmb.cli.bootstrap.find_command", side_effect=fake_which):
                with mock.patch("timecapsulesmb.cli.bootstrap.run", side_effect=fake_run) as run_mock:
                    with redirect_stdout(output):
                        bootstrap.install_required_host_tools()
        text = output.getvalue()
        self.assertIn("Missing required host tools: sshpass, smbclient", text)
        self.assertIn("Installing missing host tools via Homebrew", text)
        self.assertEqual(
            run_mock.call_args_list,
            [
                mock.call(["/opt/homebrew/bin/brew", "install", "sshpass", "samba"]),
            ],
        )

    def test_bootstrap_fails_when_homebrew_missing_for_required_host_tools_on_macos(self) -> None:
        output = io.StringIO()

        with mock.patch("timecapsulesmb.cli.bootstrap.current_platform_label", return_value="macOS"):
            with mock.patch("timecapsulesmb.cli.bootstrap.find_command", return_value=None):
                with self.assertRaises(bootstrap.BootstrapError):
                    with redirect_stdout(output):
                        bootstrap.install_required_host_tools()
        text = output.getvalue()
        self.assertIn("Install Homebrew", text)
        self.assertIn("or manually install the missing tools on macOS: sshpass, smbclient", text)
        self.assertIn("Then rerun './tcapsule bootstrap'.", text)
        self.assertIn("Missing host tools: sshpass, smbclient", text)
        self.assertIn("https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh", text)
        self.assertIn("\033[31m", text)

    def test_bootstrap_installs_missing_host_tools_via_apt_on_linux(self) -> None:
        installed: set[str] = set()

        def fake_which(name: str):
            if name in installed:
                return f"/usr/bin/{name}"
            if name == "apt-get":
                return "/usr/bin/apt-get"
            return None

        def fake_run(cmd, cwd=None):
            if cmd[:3] == ["sudo", "/usr/bin/apt-get", "install"]:
                installed.update(cmd[4:])

        with mock.patch("timecapsulesmb.cli.bootstrap.current_platform_label", return_value="Linux"):
            with mock.patch("timecapsulesmb.cli.bootstrap.find_command", side_effect=fake_which):
                with mock.patch("timecapsulesmb.cli.bootstrap.run", side_effect=fake_run) as run_mock:
                    bootstrap.install_required_host_tools()
        self.assertEqual(
            run_mock.call_args_list,
            [
                mock.call(["sudo", "/usr/bin/apt-get", "update"]),
                mock.call(["sudo", "/usr/bin/apt-get", "install", "-y", "sshpass", "smbclient"]),
            ],
        )

    def test_bootstrap_installs_missing_host_tools_via_zypper_on_linux(self) -> None:
        installed: set[str] = set()

        def fake_which(name: str):
            if name in installed:
                return f"/usr/bin/{name}"
            if name == "zypper":
                return "/usr/bin/zypper"
            return None

        def fake_run(cmd, cwd=None):
            if cmd[:3] == ["sudo", "/usr/bin/zypper", "install"]:
                for package in cmd[4:]:
                    if package == "sshpass":
                        installed.add("sshpass")
                    elif package == "samba-client":
                        installed.add("smbclient")

        with mock.patch("timecapsulesmb.cli.bootstrap.current_platform_label", return_value="Linux"):
            with mock.patch("timecapsulesmb.cli.bootstrap.find_command", side_effect=fake_which):
                with mock.patch("timecapsulesmb.cli.bootstrap.run", side_effect=fake_run) as run_mock:
                    bootstrap.install_required_host_tools()
        self.assertEqual(
            run_mock.call_args_list,
            [mock.call(["sudo", "/usr/bin/zypper", "install", "-y", "sshpass", "samba-client"])],
        )

    def test_bootstrap_installs_missing_host_tools_via_pacman_on_linux(self) -> None:
        installed: set[str] = set()

        def fake_which(name: str):
            if name in installed:
                return f"/usr/bin/{name}"
            if name == "pacman":
                return "/usr/bin/pacman"
            return None

        def fake_run(cmd, cwd=None):
            if cmd[:3] == ["sudo", "/usr/bin/pacman", "-S"]:
                installed.update(cmd[4:])

        with mock.patch("timecapsulesmb.cli.bootstrap.current_platform_label", return_value="Linux"):
            with mock.patch("timecapsulesmb.cli.bootstrap.find_command", side_effect=fake_which):
                with mock.patch("timecapsulesmb.cli.bootstrap.run", side_effect=fake_run) as run_mock:
                    bootstrap.install_required_host_tools()
        self.assertEqual(
            run_mock.call_args_list,
            [mock.call(["sudo", "/usr/bin/pacman", "-S", "--needed", "sshpass", "smbclient"])],
        )

    def test_bootstrap_prints_manual_install_when_linux_host_tool_install_fails(self) -> None:
        output = io.StringIO()

        with mock.patch("timecapsulesmb.cli.bootstrap.current_platform_label", return_value="Linux"):
            with mock.patch("timecapsulesmb.cli.bootstrap.find_command", side_effect=lambda name: "/usr/bin/apt-get" if name == "apt-get" else None):
                with mock.patch(
                    "timecapsulesmb.cli.bootstrap.run",
                    side_effect=subprocess.CalledProcessError(100, ["sudo", "/usr/bin/apt-get", "update"]),
                ):
                    with self.assertRaises(bootstrap.BootstrapError):
                        with redirect_stdout(output):
                            bootstrap.install_required_host_tools()
        text = output.getvalue()
        self.assertIn("Failed to install missing host tools automatically", text)
        self.assertIn("sudo apt-get update && sudo apt-get install -y sshpass smbclient", text)
        self.assertIn("\033[31m", text)

    def test_bootstrap_host_tool_install_error_keeps_command_stderr(self) -> None:
        output = io.StringIO()
        command_error = bootstrap.BootstrapCommandError(
            ["sudo", "/usr/bin/apt-get", "update"],
            100,
            "",
            "apt repository failure\n",
        )

        with mock.patch("timecapsulesmb.cli.bootstrap.current_platform_label", return_value="Linux"):
            with mock.patch("timecapsulesmb.cli.bootstrap.find_command", side_effect=lambda name: "/usr/bin/apt-get" if name == "apt-get" else None):
                with mock.patch("timecapsulesmb.cli.bootstrap.run", side_effect=command_error):
                    with self.assertRaises(bootstrap.BootstrapError) as raised:
                        with redirect_stdout(output):
                            bootstrap.install_required_host_tools()

        self.assertIn("Failed to install missing host tools automatically", output.getvalue())
        message = str(raised.exception)
        self.assertIn("Command failed with exit code 100", message)
        self.assertIn("stderr:", message)
        self.assertIn("apt repository failure", message)

    def test_bootstrap_fails_when_linux_package_manager_missing_for_required_host_tools(self) -> None:
        output = io.StringIO()
        with mock.patch("timecapsulesmb.cli.bootstrap.current_platform_label", return_value="Linux"):
            with mock.patch("timecapsulesmb.cli.bootstrap.find_command", return_value=None):
                with self.assertRaises(bootstrap.BootstrapError):
                    with redirect_stdout(output):
                        bootstrap.install_required_host_tools()
        self.assertIn("No supported Linux package manager found", output.getvalue())


if __name__ == "__main__":
    unittest.main()
