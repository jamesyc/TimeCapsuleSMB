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

# setUp stubs the ensurepip check so main() never probes this host's Python;
# its own tests call the real one.
REAL_PYTHON_VENV_PACKAGE = bootstrap._python_venv_package


def MAC_SSH_ONLY(name: str) -> str | None:
    """Every Mac has /usr/bin/ssh; nothing else is installed."""
    return "/usr/bin/ssh" if name == "ssh" else None


class CliBootstrapTests(CliTestCase):
    def setUp(self) -> None:
        super().setUp()
        client = mock.patch.object(bootstrap, "require_local_ssh", return_value="/tested/ssh")
        self.client = client.start()
        self.addCleanup(client.stop)
        venv_package = mock.patch.object(bootstrap, "_python_venv_package", return_value=None)
        venv_package.start()
        self.addCleanup(venv_package.stop)

    def test_bootstrap_prints_full_next_steps(self) -> None:
        output = io.StringIO()
        with mock.patch("pathlib.Path.exists", return_value=True):
            with mock.patch("timecapsulesmb.cli.bootstrap.ensure_venv", return_value=bootstrap.VENVDIR / "bin" / "python"):
                with mock.patch("timecapsulesmb.cli.bootstrap.install_python_requirements"):
                    with mock.patch("timecapsulesmb.cli.bootstrap.install_required_host_tools", return_value=[]):
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
                        with mock.patch("timecapsulesmb.cli.bootstrap.install_required_host_tools", return_value=[]):
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

    def test_bootstrap_returns_error_when_pyproject_missing(self) -> None:
        stderr = io.StringIO()
        with mock.patch("pathlib.Path.exists", return_value=False):
            with mock.patch("timecapsulesmb.cli.bootstrap.ensure_install_id"):
                with redirect_stderr(stderr):
                    rc = bootstrap.main([])
        self.assertEqual(rc, 1)
        self.assertIn("Missing", stderr.getvalue())
        finished = self.telemetry_payload("bootstrap_finished")
        self.assertEqual(finished["result"], "failure")
        self.assertEqual(finished["pyproject_present"], False)
        self.assertIn("stage=validate_project", finished["error"])

    def _bootstrap_with_fake_python(self, script: str) -> int:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pyproject = root / "pyproject.toml"
            pyproject.write_text("[project]\nname = \"timecapsulesmb\"\n")
            fake_python = root / "python3"
            fake_python.write_text(f"#!/bin/sh\n{script}\n")
            fake_python.chmod(0o755)

            with mock.patch("timecapsulesmb.cli.bootstrap.REPO_ROOT", root):
                with mock.patch("timecapsulesmb.cli.bootstrap.PYPROJECT", pyproject):
                    with mock.patch("timecapsulesmb.cli.bootstrap.VENVDIR", root / ".venv"):
                        with mock.patch("timecapsulesmb.cli.bootstrap.ensure_install_id"):
                            with mock.patch("timecapsulesmb.cli.bootstrap.current_platform_label", return_value="Linux"):
                                with mock.patch("timecapsulesmb.cli.bootstrap.validate_selected_python", return_value="3.11.9"):
                                    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                                        return bootstrap.main(["--python", str(fake_python)])

    def test_bootstrap_telemetry_error_includes_command_output(self) -> None:
        rc = self._bootstrap_with_fake_python(
            "echo 'The virtual environment was not created successfully because ensurepip is not available.' >&2\nexit 1"
        )

        self.assertEqual(rc, 1)
        error = self.telemetry_payload("bootstrap_finished")["error"]
        self.assertIn("Command failed with exit code 1", error)
        self.assertIn("output:", error)
        self.assertIn("ensurepip is not available", error)
        self.assertIn("Debug context:", error)
        self.assertIn("stage=ensure_venv", error)

    def test_bootstrap_telemetry_error_keeps_stdout_and_stderr_in_order(self) -> None:
        rc = self._bootstrap_with_fake_python("echo 'stdout first'\necho 'stderr second' >&2\nexit 3")

        self.assertEqual(rc, 3)
        error = self.telemetry_payload("bootstrap_finished")["error"]
        self.assertIn("output:\nstdout first\nstderr second", error)
        self.assertIn("stage=ensure_venv", error)

    def test_bootstrap_command_error_output_keeps_head_and_tail(self) -> None:
        head = "h" * bootstrap.COMMAND_OUTPUT_HEAD_LIMIT
        tail = "t" * bootstrap.COMMAND_OUTPUT_TAIL_LIMIT
        message = bootstrap._format_command_error(
            bootstrap.BootstrapCommandError(["brew", "install", "samba"], 1, f"{head}{'m' * 7}{tail}")
        )

        self.assertIn(f"output:\n{head}\n...<truncated 7 chars>...\n{tail}", message)
        self.assertNotIn("m", message.split("output:", 1)[1].replace("...<truncated 7 chars>...", ""))

    def test_bootstrap_command_output_short_enough_is_kept_whole(self) -> None:
        text = "x" * (bootstrap.COMMAND_OUTPUT_HEAD_LIMIT + bootstrap.COMMAND_OUTPUT_TAIL_LIMIT)
        self.assertEqual(bootstrap._truncate_command_output(text), text)

    def test_run_shows_output_before_the_command_exits(self) -> None:
        # The child prints, then waits until the test has seen that line. If
        # run() held output until exit, the child would give up and say so.
        with tempfile.TemporaryDirectory() as tmp:
            release = Path(tmp) / "release"
            script = (
                "echo first; i=0; "
                f"while [ ! -e {release} ] && [ $i -lt 50 ]; do sleep 0.1; i=$((i+1)); done; "
                f"if [ -e {release} ]; then echo released; else echo held; fi"
            )
            seen: list[bytes] = []

            def echo(chunk: bytes) -> None:
                seen.append(chunk)
                if b"first" in chunk:
                    release.touch()

            with mock.patch("timecapsulesmb.cli.bootstrap._echo", side_effect=echo):
                bootstrap.run(["/bin/sh", "-c", script])

        self.assertEqual(b"".join(seen), b"first\nreleased\n")

    def test_run_echoes_progress_frames_but_reports_their_final_state(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            with self.assertRaises(bootstrap.BootstrapCommandError) as raised:
                bootstrap.run(["/bin/sh", "-c", r"printf '#  10%%\r##  20%%\r###  30%%\nError: samba: no bottle available!\r\n'; exit 1"])

        self.assertIn("#  10%\r##  20%\r###  30%\n", output.getvalue())
        self.assertEqual(raised.exception.output, "###  30%\nError: samba: no bottle available!\n")
        self.assertEqual(raised.exception.returncode, 1)

    def test_run_succeeds_quietly_for_zero_exit(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            bootstrap.run(["/bin/sh", "-c", "echo hello"])
        self.assertEqual(output.getvalue(), "hello\n")

    def test_run_records_output_and_reaps_child_on_ctrl_c(self) -> None:
        children: list[subprocess.Popen] = []
        real_popen = subprocess.Popen

        def popen(*args, **kwargs):
            children.append(real_popen(*args, **kwargs))
            return children[-1]

        def echo(chunk: bytes) -> None:
            raise KeyboardInterrupt

        # The test sends no SIGINT to the child, so it outlives the grace period.
        with mock.patch("timecapsulesmb.cli.bootstrap.subprocess.Popen", side_effect=popen):
            with mock.patch("timecapsulesmb.cli.bootstrap._echo", side_effect=echo):
                with mock.patch("timecapsulesmb.cli.bootstrap.INTERRUPTED_CHILD_GRACE_SECONDS", 0.1):
                    with self.assertRaises(KeyboardInterrupt) as raised:
                        bootstrap.run(["/bin/sh", "-c", "echo '==> Building samba from source'; exec sleep 30"])

        self.assertEqual(raised.exception.command_output, "==> Building samba from source\n")
        self.assertIsNotNone(children[0].returncode)

    def test_bootstrap_cancel_records_command_output(self) -> None:
        interrupt = KeyboardInterrupt()
        interrupt.command_output = "==> Fetching downloads for: samba\n"
        with mock.patch("pathlib.Path.exists", return_value=True):
            with mock.patch("timecapsulesmb.cli.bootstrap.ensure_venv", return_value=bootstrap.VENVDIR / "bin" / "python"):
                with mock.patch("timecapsulesmb.cli.bootstrap.install_python_requirements"):
                    with mock.patch("timecapsulesmb.cli.bootstrap.install_required_host_tools", side_effect=interrupt):
                        with mock.patch("timecapsulesmb.cli.bootstrap.ensure_install_id"):
                            with mock.patch("timecapsulesmb.cli.bootstrap.current_platform_label", return_value="Linux"):
                                with mock.patch("timecapsulesmb.cli.bootstrap.validate_selected_python", return_value="3.11.9"):
                                    with redirect_stdout(io.StringIO()):
                                        with self.assertRaises(KeyboardInterrupt):
                                            bootstrap.main([])

        finished = self.telemetry_payload("bootstrap_finished")
        self.assertEqual(finished["result"], "cancelled")
        self.assertIn("Cancelled by user\noutput:\n==> Fetching downloads for: samba", finished["error"])
        self.assertIn("stage=install_host_tools", finished["error"])

    def test_bootstrap_cancel_outside_a_command_keeps_plain_message(self) -> None:
        with mock.patch("pathlib.Path.exists", return_value=True):
            with mock.patch("timecapsulesmb.cli.bootstrap.ensure_venv", side_effect=KeyboardInterrupt):
                with mock.patch("timecapsulesmb.cli.bootstrap.ensure_install_id"):
                    with mock.patch("timecapsulesmb.cli.bootstrap.current_platform_label", return_value="Linux"):
                        with mock.patch("timecapsulesmb.cli.bootstrap.validate_selected_python", return_value="3.11.9"):
                            with redirect_stdout(io.StringIO()):
                                with self.assertRaises(KeyboardInterrupt):
                                    bootstrap.main([])

        finished = self.telemetry_payload("bootstrap_finished")
        self.assertEqual(finished["result"], "cancelled")
        self.assertTrue(finished["error"].startswith("Cancelled by user\n\nDebug context:"), finished["error"][:80])

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

        with mock.patch("pathlib.Path.exists", return_value=True):
            with mock.patch("timecapsulesmb.cli.bootstrap.ensure_install_id"):
                with mock.patch("timecapsulesmb.cli.bootstrap.current_platform_label", return_value="macOS"):
                    with mock.patch("timecapsulesmb.cli.bootstrap.validate_selected_python", return_value="3.11.9"):
                        with mock.patch("timecapsulesmb.cli.bootstrap.detect_macos_product_version", return_value="10.15.7"):
                            with mock.patch("timecapsulesmb.cli.bootstrap.find_command", side_effect=MAC_SSH_ONLY):
                                with mock.patch("timecapsulesmb.cli.bootstrap.ensure_venv") as ensure_venv:
                                    with mock.patch("timecapsulesmb.cli.bootstrap.install_required_host_tools", return_value=[]) as install_tools:
                                        with redirect_stdout(output), redirect_stderr(stderr):
                                            rc = bootstrap.main([])

        self.assertEqual(rc, 1)
        ensure_venv.assert_not_called()
        install_tools.assert_not_called()
        text = output.getvalue()
        self.assertIn("Detected macOS version: 10.15.7", text)
        self.assertIn("Found ssh: /usr/bin/ssh", text)
        self.assertIn("Missing smbclient", text)
        self.assertIn("requires macOS 14.0 or newer", text)
        self.assertIn("\033[31m", text)
        self.assertIn("missing host tools (smbclient)", stderr.getvalue())
        finished = self.telemetry_payload("bootstrap_finished")
        self.assertEqual(finished["host_os_version"], "10.15.7")
        self.assertEqual(finished["macos_auto_host_tool_install_supported"], False)
        self.assertEqual(finished["missing_host_tools"], "smbclient")
        self.assertIsNone(finished.get("smbclient_path"))
        self.assertIn("stage=check_host_support", finished["error"])

    def test_bootstrap_old_macos_continues_when_required_tools_exist(self) -> None:
        output = io.StringIO()

        def fake_which(name: str):
            if name in {"ssh", "smbclient"}:
                return f"/usr/local/bin/{name}"
            return None

        with mock.patch("pathlib.Path.exists", return_value=True):
            with mock.patch("timecapsulesmb.cli.bootstrap.ensure_venv", return_value=bootstrap.VENVDIR / "bin" / "python") as ensure_venv:
                with mock.patch("timecapsulesmb.cli.bootstrap.install_python_requirements"):
                    with mock.patch("timecapsulesmb.cli.bootstrap.install_required_host_tools", return_value=[]):
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
        self.assertIn("Found smbclient: /usr/local/bin/smbclient", text)
        finished = self.telemetry_payload("bootstrap_finished")
        self.assertEqual(finished["host_os_version"], "10.15.7")
        self.assertEqual(finished["missing_host_tools"], "")

    def test_bootstrap_macos_14_allows_missing_tools_to_reach_installer(self) -> None:
        output = io.StringIO()
        with mock.patch("pathlib.Path.exists", return_value=True):
            with mock.patch("timecapsulesmb.cli.bootstrap.ensure_venv", return_value=bootstrap.VENVDIR / "bin" / "python"):
                with mock.patch("timecapsulesmb.cli.bootstrap.install_python_requirements"):
                    with mock.patch("timecapsulesmb.cli.bootstrap.install_required_host_tools", return_value=[]) as install_tools:
                        with mock.patch("timecapsulesmb.cli.bootstrap.ensure_install_id"):
                            with mock.patch("timecapsulesmb.cli.bootstrap.current_platform_label", return_value="macOS"):
                                with mock.patch("timecapsulesmb.cli.bootstrap.validate_selected_python", return_value="3.11.9"):
                                    with mock.patch("timecapsulesmb.cli.bootstrap.detect_macos_product_version", return_value="14.0"):
                                        with mock.patch("timecapsulesmb.cli.bootstrap.find_command", side_effect=MAC_SSH_ONLY):
                                            with redirect_stdout(output):
                                                rc = bootstrap.main([])

        self.assertEqual(rc, 0)
        install_tools.assert_called_once()
        self.assertNotIn("requires macOS 14.0 or newer", output.getvalue())
        finished = self.telemetry_payload("bootstrap_finished")
        self.assertEqual(finished["macos_auto_host_tool_install_supported"], True)
        self.assertEqual(finished["missing_host_tools"], "smbclient")

    def test_bootstrap_unknown_macos_version_missing_tools_fails_safely(self) -> None:
        output = io.StringIO()
        with mock.patch("timecapsulesmb.cli.bootstrap.detect_macos_product_version", return_value=None):
            with mock.patch("timecapsulesmb.cli.bootstrap.find_command", return_value=None):
                with self.assertRaises(bootstrap.BootstrapError):
                    with redirect_stdout(output):
                        bootstrap.check_macos_host_tool_install_support("macOS")

        text = output.getvalue()
        self.assertIn("Detected macOS version: unknown", text)
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
                mock.call([
                    str(venv_python), "-m", "pip", "install", "-U", "--upgrade-strategy", "eager",
                    "-e", str(bootstrap.REPO_ROOT),
                ]),
            ],
        )

    def test_bootstrap_install_python_requirements_skips_ensurepip_when_pip_exists(self) -> None:
        venv_python = Path("/tmp/tcapsule-venv/bin/python")
        with mock.patch("timecapsulesmb.cli.bootstrap.venv_has_pip", return_value=True):
            with mock.patch("timecapsulesmb.cli.bootstrap.run") as run_mock:
                bootstrap.install_python_requirements(venv_python)

        # A rerun upgrades pip, then moves the project and every dependency
        # already in .venv to the newest releases.
        commands = [call.args[0] for call in run_mock.call_args_list]
        self.assertEqual(
            commands,
            [
                [str(venv_python), "-m", "pip", "install", "-U", "pip"],
                [
                    str(venv_python), "-m", "pip", "install", "-U", "--upgrade-strategy", "eager",
                    "-e", str(bootstrap.REPO_ROOT),
                ],
            ],
        )

    def test_bootstrap_skips_required_host_tools_when_present(self) -> None:
        output = io.StringIO()

        def fake_which(name: str):
            if name in {"ssh", "smbclient"}:
                return f"/usr/bin/{name}"
            return None

        with mock.patch("timecapsulesmb.cli.bootstrap.find_command", side_effect=fake_which):
            with mock.patch("timecapsulesmb.cli.bootstrap.run") as run_mock:
                with redirect_stdout(output):
                    bootstrap.install_required_host_tools()

        self.assertIn("Found required host tools: ssh, smbclient", output.getvalue())
        run_mock.assert_not_called()

    def test_bootstrap_installs_missing_host_tools_via_homebrew_on_macos(self) -> None:
        output = io.StringIO()
        installed: set[str] = set()

        def fake_which(name: str):
            if name == "brew":
                return "/opt/homebrew/bin/brew"
            if name == "ssh":
                return "/usr/bin/ssh"
            if name in installed:
                return f"/opt/homebrew/bin/{name}"
            return None

        def fake_run(cmd):
            if cmd[:2] == ["/opt/homebrew/bin/brew", "install"] and "samba" in cmd[2:]:
                installed.add("smbclient")

        with mock.patch("timecapsulesmb.cli.bootstrap.current_platform_label", return_value="macOS"):
            with mock.patch("timecapsulesmb.cli.bootstrap._macos_intel_host", return_value=False):
                with mock.patch("timecapsulesmb.cli.bootstrap.find_command", side_effect=fake_which):
                    with mock.patch("timecapsulesmb.cli.bootstrap.run", side_effect=fake_run) as run_mock:
                        with redirect_stdout(output):
                            skipped = bootstrap.install_required_host_tools()
        self.assertEqual(skipped, [])
        text = output.getvalue()
        self.assertNotIn("Intel", text)
        self.assertIn("Missing required host tools: smbclient", text)
        self.assertIn("Installing missing host tools via Homebrew", text)
        self.assertEqual(
            run_mock.call_args_list,
            [
                mock.call(["/opt/homebrew/bin/brew", "install", "samba"]),
            ],
        )

    def test_bootstrap_fails_when_homebrew_missing_for_required_host_tools_on_macos(self) -> None:
        output = io.StringIO()

        with mock.patch("timecapsulesmb.cli.bootstrap.current_platform_label", return_value="macOS"):
            with mock.patch("timecapsulesmb.cli.bootstrap._macos_intel_host", return_value=False):
                with mock.patch("timecapsulesmb.cli.bootstrap.find_command", side_effect=MAC_SSH_ONLY):
                    with self.assertRaises(bootstrap.BootstrapError) as raised:
                        with redirect_stdout(output):
                            bootstrap.install_required_host_tools()
        text = output.getvalue()
        app_url = "https://github.com/jamesyc/TimeCapsuleSMB/releases"
        # main prints the error last; the hint lives there, not in this output.
        self.assertNotIn(app_url, text)
        self.assertIn(f"Mac app, which includes these tools and needs no Homebrew: {app_url}", str(raised.exception))
        self.assertIn("Install Homebrew", text)
        self.assertIn("or manually install the missing tools on macOS: smbclient", text)
        self.assertIn("Then rerun './tcapsule bootstrap'.", text)
        self.assertIn("Missing host tools: smbclient", text)
        self.assertIn("https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh", text)
        self.assertIn("\033[31m", text)

    def _install_on_intel_mac(self, *, present: set[str], brew: bool):
        installed = {"ssh", *present}

        def fake_which(name: str):
            if name == "brew":
                return "/usr/local/bin/brew" if brew else None
            if name in installed:
                return f"/usr/local/bin/{name}"
            return None

        output = io.StringIO()
        with mock.patch("timecapsulesmb.cli.bootstrap.current_platform_label", return_value="macOS"):
            with mock.patch("timecapsulesmb.cli.bootstrap._macos_intel_host", return_value=True):
                with mock.patch("timecapsulesmb.cli.bootstrap.find_command", side_effect=fake_which):
                    with mock.patch("timecapsulesmb.cli.bootstrap.run") as run_mock:
                        with redirect_stdout(output):
                            try:
                                result: object = bootstrap.install_required_host_tools()
                            except bootstrap.BootstrapError as exc:
                                result = exc
        return result, run_mock, output.getvalue()

    def test_bootstrap_intel_mac_skips_smbclient_and_runs_no_brew(self) -> None:
        # smbclient is the only host tool and Homebrew cannot build it for
        # Intel, so an Intel Mac needs no Homebrew at all.
        for brew in (True, False):
            with self.subTest(brew=brew):
                skipped, run_mock, text = self._install_on_intel_mac(present=set(), brew=brew)

                self.assertEqual(skipped, ["smbclient"])
                run_mock.assert_not_called()
                self.assertIn("Homebrew no longer builds smbclient for Intel Macs", text)
                self.assertNotIn("Install Homebrew", text)
                self.assertNotIn("https://github.com/jamesyc/TimeCapsuleSMB/releases", text)

    def test_bootstrap_intel_mac_with_smbclient_present_skips_nothing(self) -> None:
        skipped, run_mock, text = self._install_on_intel_mac(present={"smbclient"}, brew=False)

        self.assertEqual(skipped, [])
        run_mock.assert_not_called()
        self.assertNotIn("Homebrew no longer builds smbclient", text)

    def test_bootstrap_records_skipped_smbclient_and_repeats_note(self) -> None:
        output = io.StringIO()
        with mock.patch("pathlib.Path.exists", return_value=True):
            with mock.patch("timecapsulesmb.cli.bootstrap.ensure_venv", return_value=bootstrap.VENVDIR / "bin" / "python"):
                with mock.patch("timecapsulesmb.cli.bootstrap.install_python_requirements"):
                    with mock.patch("timecapsulesmb.cli.bootstrap.install_required_host_tools", return_value=["smbclient"]):
                        with mock.patch("timecapsulesmb.cli.bootstrap.ensure_install_id"):
                            with mock.patch("timecapsulesmb.cli.bootstrap.current_platform_label", return_value="macOS"):
                                with mock.patch("timecapsulesmb.cli.bootstrap.validate_selected_python", return_value="3.11.9"):
                                    with mock.patch("timecapsulesmb.cli.bootstrap.check_macos_host_tool_install_support", return_value={}):
                                        with redirect_stdout(output):
                                            rc = bootstrap.main([])

        self.assertEqual(rc, 0)
        self.assertIn("Not installed: smbclient. Homebrew no longer builds smbclient", output.getvalue())
        finished = self.telemetry_payload("bootstrap_finished")
        self.assertEqual(finished["result"], "success")
        self.assertEqual(finished["skipped_host_tools"], "smbclient")

    def test_bootstrap_success_without_skipped_tools_has_no_skip_field(self) -> None:
        with mock.patch("pathlib.Path.exists", return_value=True):
            with mock.patch("timecapsulesmb.cli.bootstrap.ensure_venv", return_value=bootstrap.VENVDIR / "bin" / "python"):
                with mock.patch("timecapsulesmb.cli.bootstrap.install_python_requirements"):
                    with mock.patch("timecapsulesmb.cli.bootstrap.install_required_host_tools", return_value=[]):
                        with mock.patch("timecapsulesmb.cli.bootstrap.ensure_install_id"):
                            with mock.patch("timecapsulesmb.cli.bootstrap.current_platform_label", return_value="macOS"):
                                with mock.patch("timecapsulesmb.cli.bootstrap.validate_selected_python", return_value="3.11.9"):
                                    with mock.patch("timecapsulesmb.cli.bootstrap.check_macos_host_tool_install_support", return_value={}):
                                        with redirect_stdout(io.StringIO()) as output:
                                            rc = bootstrap.main([])

        self.assertEqual(rc, 0)
        self.assertNotIn("Not installed", output.getvalue())
        self.assertNotIn("skipped_host_tools", self.telemetry_payload("bootstrap_finished"))

    def _bootstrap_on_mac(self, *, intel: bool, present: set[str], brew: bool) -> tuple[int, str]:
        """Run all of bootstrap with the real host-tool step; return its status and terminal text."""
        prefix = "/usr/local" if intel else "/opt/homebrew"
        installed = {"ssh", *present}

        def fake_which(name: str):
            if name == "brew":
                return f"{prefix}/bin/brew" if brew else None
            return f"{prefix}/bin/{name}" if name in installed else None

        def fake_run(cmd):
            for package in cmd[2:]:
                installed.add("smbclient" if package == "samba" else package)

        terminal = io.StringIO()
        with mock.patch("pathlib.Path.exists", return_value=True):
            with mock.patch("timecapsulesmb.cli.bootstrap.ensure_venv", return_value=bootstrap.VENVDIR / "bin" / "python"):
                with mock.patch("timecapsulesmb.cli.bootstrap.install_python_requirements"):
                    with mock.patch("timecapsulesmb.cli.bootstrap.ensure_install_id"):
                        with mock.patch("timecapsulesmb.cli.bootstrap.current_platform_label", return_value="macOS"):
                            with mock.patch("timecapsulesmb.cli.bootstrap.validate_selected_python", return_value="3.11.9"):
                                with mock.patch("timecapsulesmb.cli.bootstrap.check_macos_host_tool_install_support", return_value={}):
                                    with mock.patch("timecapsulesmb.cli.bootstrap._macos_intel_host", return_value=intel):
                                        with mock.patch("timecapsulesmb.cli.bootstrap.find_command", side_effect=fake_which):
                                            with mock.patch("timecapsulesmb.cli.bootstrap.run", side_effect=fake_run):
                                                with redirect_stdout(terminal), redirect_stderr(terminal):
                                                    rc = bootstrap.main([])
        return rc, terminal.getvalue()

    def test_bootstrap_shows_the_mac_app_hint_once_where_it_applies(self) -> None:
        cases = [
            # (intel, tools present, brew installed, status, app hints, Intel smbclient notes)
            (False, set(), False, 1, 1, 0),
            (False, set(), True, 0, 0, 0),
            # The Intel note shows when smbclient is skipped and again in the
            # summary; Homebrew is not needed there.
            (True, set(), False, 0, 1, 2),
            (True, set(), True, 0, 1, 2),
            (True, {"smbclient"}, False, 0, 0, 0),
        ]
        for intel, present, brew, status, hints, notes in cases:
            with self.subTest(intel=intel, present=sorted(present), brew=brew):
                rc, text = self._bootstrap_on_mac(intel=intel, present=present, brew=brew)

                self.assertEqual(rc, status)
                self.assertEqual(text.count(bootstrap.MAC_APP_HINT), hints, text)
                self.assertEqual(text.count("Homebrew no longer builds smbclient"), notes, text)

    def test_macos_intel_host_reads_hw_optional_arm64(self) -> None:
        cases = [
            (subprocess.CompletedProcess([], 0, "1\n", ""), False),
            (subprocess.CompletedProcess([], 0, "0\n", ""), True),
            (subprocess.CompletedProcess([], 1, "", "sysctl: unknown oid 'hw.optional.arm64'"), True),
            (OSError("no sysctl"), True),
        ]
        for answer, intel in cases:
            with self.subTest(answer=answer):
                with mock.patch("timecapsulesmb.cli.bootstrap.run_process", side_effect=[answer]) as run_mock:
                    self.assertEqual(bootstrap._macos_intel_host(), intel)
                self.assertEqual(run_mock.call_args.args[0], ["/usr/sbin/sysctl", "-n", "hw.optional.arm64"])

    def _linux_install(
        self,
        manager: str,
        *,
        euid: int,
        has_sudo: bool = True,
        run_error: BaseException | None = None,
        output: io.StringIO | None = None,
    ) -> tuple[list[list[str]], bootstrap.BootstrapError | None]:
        """install_required_host_tools on Linux with only `manager` installed.

        Returns the commands run and the BootstrapError raised, if any. Each
        install command puts the tools it names on the fake PATH, as a real
        one would.
        """
        tools_by_package = {
            package: tool for packages in bootstrap.LINUX_HOST_TOOL_PACKAGES.values() for tool, package in packages.items()
        }
        installed: set[str] = set()
        commands: list[list[str]] = []

        def fake_which(name: str):
            found = name == manager or name in installed or (has_sudo and name == "sudo")
            return f"/usr/bin/{name}" if found else None

        def fake_run(cmd):
            commands.append(cmd)
            if run_error is not None:
                raise run_error
            installed.update(tools_by_package[arg] for arg in cmd if arg in tools_by_package)

        with mock.patch("timecapsulesmb.cli.bootstrap.current_platform_label", return_value="Linux"), \
             mock.patch("timecapsulesmb.cli.bootstrap.find_command", side_effect=fake_which), \
             mock.patch("timecapsulesmb.cli.bootstrap.os.geteuid", return_value=euid), \
             mock.patch("timecapsulesmb.cli.bootstrap.run", side_effect=fake_run), \
             redirect_stdout(output or io.StringIO()):
            try:
                bootstrap.install_required_host_tools()
            except bootstrap.BootstrapError as exc:
                return commands, exc
        return commands, None

    def test_bootstrap_installs_linux_host_tools_with_sudo_unless_root(self) -> None:
        expected = {
            "apt-get": [["apt-get", "update"], ["apt-get", "install", "-y", "openssh-client", "smbclient"]],
            "dnf": [["dnf", "install", "-y", "openssh-clients", "samba-client"]],
            "zypper": [["zypper", "install", "-y", "openssh-clients", "samba-client"]],
            "pacman": [["pacman", "-S", "--needed", "openssh", "smbclient"]],
        }
        self.assertEqual(set(expected), set(bootstrap.LINUX_HOST_TOOL_PACKAGES))
        for manager, commands in expected.items():
            for euid, has_sudo, prefix in ((1000, True, ["sudo"]), (0, True, []), (0, False, [])):
                with self.subTest(manager=manager, euid=euid, has_sudo=has_sudo):
                    output = io.StringIO()
                    ran, error = self._linux_install(manager, euid=euid, has_sudo=has_sudo, output=output)
                    self.assertIsNone(error)
                    self.assertEqual(ran, [[*prefix, *command] for command in commands])
                    self.assertIn(f"Installing ssh, smbclient with {manager}", output.getvalue())
                    self.assertIn("Installed required host tools: ssh, smbclient", output.getvalue())

    def test_bootstrap_linux_install_failure_prints_the_manual_command_and_keeps_output(self) -> None:
        command_error = bootstrap.BootstrapCommandError(["apt-get", "update"], 100, "apt repository failure\n")
        for euid, manual in (
            (1000, "sudo apt-get update && sudo apt-get install -y openssh-client smbclient"),
            (0, "apt-get update && apt-get install -y openssh-client smbclient"),
        ):
            with self.subTest(euid=euid):
                output = io.StringIO()
                _, error = self._linux_install("apt-get", euid=euid, run_error=command_error, output=output)
                self.assertIn("Failed to install ssh, smbclient automatically (exit code 100)", str(error))
                self.assertIn("output:\napt repository failure", str(error))
                self.assertEqual(output.getvalue().splitlines()[-1], manual)
                self.assertIn("\033[31m", output.getvalue())

    def test_bootstrap_without_sudo_shows_the_root_command_and_runs_nothing(self) -> None:
        output = io.StringIO()
        ran, error = self._linux_install("apt-get", euid=1000, has_sudo=False, output=output)
        self.assertEqual(ran, [])
        self.assertIn("Installing ssh, smbclient needs root, and sudo is not installed", str(error))
        self.assertEqual(
            output.getvalue().splitlines()[-1],
            "apt-get update && apt-get install -y openssh-client smbclient",
        )

    def test_bootstrap_missing_package_manager_program_fails_cleanly(self) -> None:
        output = io.StringIO()
        _, error = self._linux_install(
            "apt-get",
            euid=0,
            run_error=FileNotFoundError(2, "No such file or directory", "apt-get"),
            output=output,
        )
        self.assertIn("Failed to install ssh, smbclient automatically", str(error))
        self.assertIn("No such file or directory: 'apt-get'", str(error))
        self.assertEqual(
            output.getvalue().splitlines()[-1],
            "apt-get update && apt-get install -y openssh-client smbclient",
        )

    def test_bootstrap_lets_a_closed_terminal_end_the_install_unreported(self) -> None:
        # Output piped to a reader that exited is not a failed install.
        with self.assertRaises(BrokenPipeError):
            self._linux_install("apt-get", euid=0, run_error=BrokenPipeError(32, "Broken pipe"))

    def _venv_package(
        self, *, apt: bool = True, probe_returncode: int = 1, venv_has_pip: bool | None = None,
    ) -> tuple[str | None, mock.Mock]:
        """The real _python_venv_package, with `.venv` absent (venv_has_pip None) or present."""
        with tempfile.TemporaryDirectory() as tmp:
            venv = Path(tmp) / ".venv"
            if venv_has_pip is not None:
                (venv / "bin").mkdir(parents=True)
                (venv / "bin" / "python").touch()
            with mock.patch("timecapsulesmb.cli.bootstrap.VENVDIR", venv), \
                 mock.patch(
                     "timecapsulesmb.cli.bootstrap.find_command",
                     side_effect=lambda name: "/usr/bin/apt-get" if apt and name == "apt-get" else None,
                 ), \
                 mock.patch("timecapsulesmb.cli.bootstrap.venv_has_pip", return_value=bool(venv_has_pip)), \
                 mock.patch(
                     "timecapsulesmb.cli.bootstrap.run_process",
                     return_value=subprocess.CompletedProcess([], probe_returncode),
                 ) as run_process:
                return REAL_PYTHON_VENV_PACKAGE("/usr/bin/python3", "3.12.3"), run_process

    def test_python_venv_package_names_the_versioned_package_when_ensurepip_is_missing(self) -> None:
        for venv_has_pip in (None, False):  # no .venv, or one a failed run left without pip
            with self.subTest(venv_has_pip=venv_has_pip):
                package, run_process = self._venv_package(venv_has_pip=venv_has_pip)
                self.assertEqual(package, "python3.12-venv")
                self.assertEqual(run_process.call_args.args[0], ["/usr/bin/python3", "-c", "import ensurepip"])

    def test_python_venv_package_is_none_when_ensurepip_imports(self) -> None:
        package, _ = self._venv_package(probe_returncode=0)
        self.assertIsNone(package)

    def test_python_venv_package_is_none_without_apt(self) -> None:
        package, run_process = self._venv_package(apt=False)
        self.assertIsNone(package)
        run_process.assert_not_called()

    def test_python_venv_package_is_none_when_the_venv_already_has_pip(self) -> None:
        package, run_process = self._venv_package(venv_has_pip=True)
        self.assertIsNone(package)
        run_process.assert_not_called()

    def _install_venv_package(
        self, *, euid: int, has_sudo: bool = True, run_error: BaseException | None = None,
    ) -> tuple[list[list[str]], str, bootstrap.BootstrapError | None]:
        """install_python_venv_package: the commands run, its output, and its error."""
        commands: list[list[str]] = []

        def fake_run(cmd):
            commands.append(cmd)
            if run_error is not None:
                raise run_error

        output = io.StringIO()
        with mock.patch(
                 "timecapsulesmb.cli.bootstrap.find_command",
                 side_effect=lambda name: f"/usr/bin/{name}" if name == "apt-get" or (has_sudo and name == "sudo") else None,
             ), \
             mock.patch("timecapsulesmb.cli.bootstrap.os.geteuid", return_value=euid), \
             mock.patch("timecapsulesmb.cli.bootstrap.run", side_effect=fake_run), \
             redirect_stdout(output):
            try:
                bootstrap.install_python_venv_package("python3.13-venv")
            except bootstrap.BootstrapError as exc:
                return commands, output.getvalue(), exc
        return commands, output.getvalue(), None

    def test_install_python_venv_package_installs_only_that_package(self) -> None:
        for euid, prefix in ((1000, ["sudo"]), (0, [])):
            with self.subTest(euid=euid):
                commands, text, error = self._install_venv_package(euid=euid)
                self.assertIsNone(error)
                self.assertEqual(
                    commands,
                    [[*prefix, "apt-get", "update"], [*prefix, "apt-get", "install", "-y", "python3.13-venv"]],
                )
                self.assertIn("Missing python3.13-venv, which Python needs to create or repair .venv", text)
                self.assertIn("Installing python3.13-venv with apt-get", text)
                self.assertIn("Installed python3.13-venv", text)
                self.assertNotIn("host tools", text)

    def test_install_python_venv_package_failures_show_the_manual_command(self) -> None:
        commands, text, error = self._install_venv_package(euid=1000, has_sudo=False)
        self.assertEqual(commands, [])
        self.assertIn("Installing python3.13-venv needs root, and sudo is not installed", str(error))
        self.assertEqual(text.splitlines()[-1], "apt-get update && apt-get install -y python3.13-venv")

        command_error = bootstrap.BootstrapCommandError(["sudo", "apt-get", "update"], 100, "no network\n")
        _, text, error = self._install_venv_package(euid=1000, run_error=command_error)
        self.assertIn("Failed to install python3.13-venv automatically (exit code 100)", str(error))
        self.assertEqual(text.splitlines()[-1], "sudo apt-get update && sudo apt-get install -y python3.13-venv")
        self.assertNotIn("Installed python3.13-venv", text)

    def test_bootstrap_installs_the_venv_package_first_and_host_tools_last(self) -> None:
        cases = (
            ("Linux", "python3.11-venv", ["install_python_venv_package", "ensure_venv", "install_python_requirements"]),
            ("Linux", None, ["ensure_venv", "install_python_requirements"]),
            ("macOS", None, ["ensure_venv", "install_python_requirements"]),
        )
        for label, venv_package, steps in cases:
            with self.subTest(label=label, venv_package=venv_package):
                calls = mock.Mock()
                calls.ensure_venv.return_value = bootstrap.VENVDIR / "bin" / "python"
                calls.install_required_host_tools.return_value = []
                with mock.patch("pathlib.Path.exists", return_value=True), \
                     mock.patch("timecapsulesmb.cli.bootstrap.ensure_install_id"), \
                     mock.patch("timecapsulesmb.cli.bootstrap.current_platform_label", return_value=label), \
                     mock.patch("timecapsulesmb.cli.bootstrap.validate_selected_python", return_value="3.11.9"), \
                     mock.patch("timecapsulesmb.cli.bootstrap.check_macos_host_tool_install_support", return_value={}), \
                     mock.patch("timecapsulesmb.cli.bootstrap._python_venv_package", return_value=venv_package) as probe, \
                     mock.patch("timecapsulesmb.cli.bootstrap.install_python_venv_package", calls.install_python_venv_package), \
                     mock.patch("timecapsulesmb.cli.bootstrap.ensure_venv", calls.ensure_venv), \
                     mock.patch("timecapsulesmb.cli.bootstrap.install_python_requirements", calls.install_python_requirements), \
                     mock.patch("timecapsulesmb.cli.bootstrap.install_required_host_tools", calls.install_required_host_tools), \
                     redirect_stdout(io.StringIO()):
                    self.assertEqual(bootstrap.main([]), 0)
                self.assertEqual(
                    [name for name, _args, _kwargs in calls.mock_calls],
                    [*steps, "install_required_host_tools"],
                )
                if venv_package:
                    calls.install_python_venv_package.assert_called_once_with(venv_package)
                self.assertEqual(probe.called, label == "Linux")
                finished = self.telemetry_payload("bootstrap_finished")
                self.assertEqual(finished.get("python_venv_package"), venv_package)

    def test_bootstrap_a_failed_venv_package_install_stops_before_the_venv(self) -> None:
        stderr = io.StringIO()
        with mock.patch("pathlib.Path.exists", return_value=True), \
             mock.patch("timecapsulesmb.cli.bootstrap.ensure_install_id"), \
             mock.patch("timecapsulesmb.cli.bootstrap.current_platform_label", return_value="Linux"), \
             mock.patch("timecapsulesmb.cli.bootstrap.validate_selected_python", return_value="3.11.9"), \
             mock.patch("timecapsulesmb.cli.bootstrap._python_venv_package", return_value="python3.11-venv"), \
             mock.patch(
                 "timecapsulesmb.cli.bootstrap.install_python_venv_package",
                 side_effect=bootstrap.BootstrapError("Failed to install python3.11-venv automatically (exit code 100)"),
             ), \
             mock.patch("timecapsulesmb.cli.bootstrap.ensure_venv") as ensure_venv, \
             redirect_stdout(io.StringIO()), redirect_stderr(stderr):
            self.assertEqual(bootstrap.main([]), 1)
        ensure_venv.assert_not_called()
        self.assertIn("stage=install_python_venv_package", self.telemetry_payload("bootstrap_finished")["error"])

    def test_bootstrap_reports_a_missing_program_without_a_traceback(self) -> None:
        stderr = io.StringIO()
        missing = FileNotFoundError(2, "No such file or directory", "python")
        with mock.patch("pathlib.Path.exists", return_value=True), \
             mock.patch("timecapsulesmb.cli.bootstrap.ensure_install_id"), \
             mock.patch("timecapsulesmb.cli.bootstrap.current_platform_label", return_value="Linux"), \
             mock.patch("timecapsulesmb.cli.bootstrap.validate_selected_python", return_value="3.11.9"), \
             mock.patch("timecapsulesmb.cli.bootstrap.ensure_venv", side_effect=missing), \
             redirect_stdout(io.StringIO()), redirect_stderr(stderr):
            rc = bootstrap.main([])
        self.assertEqual(rc, 1)
        self.assertIn("FileNotFoundError: [Errno 2] No such file or directory: 'python'", stderr.getvalue())
        finished = self.telemetry_payload("bootstrap_finished")
        self.assertEqual(finished["result"], "failure")
        self.assertIn("FileNotFoundError", finished["error"])
        self.assertIn("stage=ensure_venv", finished["error"])

    def test_bootstrap_fails_when_linux_package_manager_missing_for_required_host_tools(self) -> None:
        output = io.StringIO()
        with mock.patch("timecapsulesmb.cli.bootstrap.current_platform_label", return_value="Linux"):
            with mock.patch("timecapsulesmb.cli.bootstrap.find_command", return_value=None):
                with self.assertRaises(bootstrap.BootstrapError):
                    with redirect_stdout(output):
                        bootstrap.install_required_host_tools()
        self.assertIn("No supported Linux package manager found", output.getvalue())
        self.assertNotIn("Mac app", output.getvalue())


if __name__ == "__main__":
    unittest.main()
