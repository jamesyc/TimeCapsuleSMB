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


def MAC_SSH_ONLY(name: str) -> str | None:
    """Every Mac has /usr/bin/ssh; nothing else is installed."""
    return "/usr/bin/ssh" if name == "ssh" else None


class CliBootstrapTests(CliTestCase):
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

    def _bootstrap_with_fake_python(self, script: str) -> int:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            requirements = root / "requirements.txt"
            requirements.write_text("zeroconf\n")
            fake_python = root / "python3"
            fake_python.write_text(f"#!/bin/sh\n{script}\n")
            fake_python.chmod(0o755)

            with mock.patch("timecapsulesmb.cli.bootstrap.REPO_ROOT", root):
                with mock.patch("timecapsulesmb.cli.bootstrap.REQUIREMENTS", requirements):
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

    def test_bootstrap_installs_missing_host_tools_via_apt_on_linux(self) -> None:
        installed: set[str] = set()

        def fake_which(name: str):
            if name in installed:
                return f"/usr/bin/{name}"
            if name == "apt-get":
                return "/usr/bin/apt-get"
            return None

        def fake_run(cmd):
            if cmd[:3] == ["sudo", "/usr/bin/apt-get", "install"]:
                installed.update("ssh" if package == "openssh-client" else package for package in cmd[4:])

        with mock.patch("timecapsulesmb.cli.bootstrap.current_platform_label", return_value="Linux"):
            with mock.patch("timecapsulesmb.cli.bootstrap.find_command", side_effect=fake_which):
                with mock.patch("timecapsulesmb.cli.bootstrap.run", side_effect=fake_run) as run_mock:
                    bootstrap.install_required_host_tools()
        self.assertEqual(
            run_mock.call_args_list,
            [
                mock.call(["sudo", "/usr/bin/apt-get", "update"]),
                mock.call(["sudo", "/usr/bin/apt-get", "install", "-y", "openssh-client", "smbclient"]),
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

        def fake_run(cmd):
            if cmd[:3] == ["sudo", "/usr/bin/zypper", "install"]:
                names = {"openssh-clients": "ssh", "samba-client": "smbclient"}
                installed.update(names[package] for package in cmd[4:])

        with mock.patch("timecapsulesmb.cli.bootstrap.current_platform_label", return_value="Linux"):
            with mock.patch("timecapsulesmb.cli.bootstrap.find_command", side_effect=fake_which):
                with mock.patch("timecapsulesmb.cli.bootstrap.run", side_effect=fake_run) as run_mock:
                    bootstrap.install_required_host_tools()
        self.assertEqual(
            run_mock.call_args_list,
            [mock.call(["sudo", "/usr/bin/zypper", "install", "-y", "openssh-clients", "samba-client"])],
        )

    def test_bootstrap_installs_missing_host_tools_via_pacman_on_linux(self) -> None:
        installed: set[str] = set()

        def fake_which(name: str):
            if name in installed:
                return f"/usr/bin/{name}"
            if name == "pacman":
                return "/usr/bin/pacman"
            return None

        def fake_run(cmd):
            if cmd[:3] == ["sudo", "/usr/bin/pacman", "-S"]:
                installed.update("ssh" if package == "openssh" else package for package in cmd[4:])

        with mock.patch("timecapsulesmb.cli.bootstrap.current_platform_label", return_value="Linux"):
            with mock.patch("timecapsulesmb.cli.bootstrap.find_command", side_effect=fake_which):
                with mock.patch("timecapsulesmb.cli.bootstrap.run", side_effect=fake_run) as run_mock:
                    bootstrap.install_required_host_tools()
        self.assertEqual(
            run_mock.call_args_list,
            [mock.call(["sudo", "/usr/bin/pacman", "-S", "--needed", "openssh", "smbclient"])],
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
        self.assertIn("sudo apt-get update && sudo apt-get install -y openssh-client smbclient", text)
        self.assertIn("\033[31m", text)

    def test_bootstrap_host_tool_install_error_keeps_command_output(self) -> None:
        output = io.StringIO()
        command_error = bootstrap.BootstrapCommandError(
            ["sudo", "/usr/bin/apt-get", "update"],
            100,
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
        self.assertIn("output:", message)
        self.assertIn("apt repository failure", message)

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
