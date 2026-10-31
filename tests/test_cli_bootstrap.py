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
from timecapsulesmb.transport.errors import SshClientConfigError

from tests.cli_support import CliTestCase

# setUp stubs the ensurepip check so main() never probes this host's Python;
# its own tests call the real one.
REAL_PYTHON_VENV_PACKAGE = bootstrap._python_venv_package


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
                    with mock.patch("timecapsulesmb.cli.bootstrap.install_required_host_tools"):
                        with mock.patch("timecapsulesmb.cli.bootstrap.ensure_install_id"):
                            with mock.patch("timecapsulesmb.cli.bootstrap.current_platform_label", return_value="macOS"):
                                with mock.patch("timecapsulesmb.cli.bootstrap.validate_selected_python", return_value="3.11.9"):
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

    def test_homebrew_install_has_nothing_to_bootstrap(self) -> None:
        output = io.StringIO()
        with mock.patch.dict("os.environ", {"TCAPSULE_CLIENT": "homebrew"}):
            with mock.patch("timecapsulesmb.cli.bootstrap.ensure_venv") as ensure_venv:
                with mock.patch("timecapsulesmb.cli.bootstrap.ensure_install_id") as ensure_install_id:
                    with redirect_stdout(output):
                        rc = bootstrap.main([])

        self.assertEqual(rc, 0)
        self.assertEqual(
            output.getvalue(),
            "Homebrew installed tcapsule and everything it needs, so there is nothing to bootstrap.\n"
            "Run `brew update && brew upgrade tcapsule` to update it.\n",
        )
        ensure_venv.assert_not_called()
        ensure_install_id.assert_not_called()
        self.client.assert_not_called()

    def test_bootstrap_prints_same_core_next_steps_on_linux(self) -> None:
        output = io.StringIO()
        smbclient = {"smbclient": "/usr/bin/smbclient"}
        with mock.patch("timecapsulesmb.cli.bootstrap.current_platform_label", return_value="Linux"), \
             mock.patch("timecapsulesmb.cli.bootstrap.find_command", side_effect=smbclient.get):
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
        finished = self.telemetry_payload("bootstrap_finished")
        self.assertIs(finished["smbclient_available_after"], True)

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
            bootstrap.BootstrapCommandError(["apt-get", "install", "-y", "smbclient"], 1, f"{head}{'m' * 7}{tail}")
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
        cases = (
            # macOS doctor uses Apple's SMB client, so smbclient is not required there.
            ("macOS", {"ssh"}, "Found required host tools: ssh\n"),
            ("Linux", {"ssh", "smbclient"}, "Found required host tools: ssh, smbclient\n"),
        )
        for label, present, found in cases:
            with self.subTest(label=label):
                output = io.StringIO()
                with mock.patch(
                         "timecapsulesmb.cli.bootstrap.find_command",
                         side_effect=lambda name, present=present: f"/usr/bin/{name}" if name in present else None,
                     ), \
                     mock.patch("timecapsulesmb.cli.bootstrap.run") as run_mock, \
                     redirect_stdout(output):
                    bootstrap.install_required_host_tools(label)

                self.assertIn(found, output.getvalue())
                self.assertNotIn("Missing", output.getvalue())
                run_mock.assert_not_called()
                self.client.assert_called_once()
                self.client.reset_mock()

    def _bootstrap_on(self, label: str, *, present: set[str]) -> tuple[int, str, list[list[str]], bool]:
        """Run all of bootstrap on `label` with the real host-tool steps;
        return its status, terminal text, the commands it ran and whether it
        made .venv. Homebrew is on PATH, no Linux package manager is, and the
        tools in `present` are in /usr/bin."""
        commands: list[list[str]] = []

        def fake_which(name: str):
            if name == "brew":
                return "/opt/homebrew/bin/brew"
            return f"/usr/bin/{name}" if name in present else None

        terminal = io.StringIO()
        with mock.patch("pathlib.Path.exists", return_value=True):
            with mock.patch("timecapsulesmb.cli.bootstrap.ensure_venv", return_value=bootstrap.VENVDIR / "bin" / "python") as ensure_venv:
                with mock.patch("timecapsulesmb.cli.bootstrap.install_python_requirements"):
                    with mock.patch("timecapsulesmb.cli.bootstrap.ensure_install_id"):
                        with mock.patch("timecapsulesmb.cli.bootstrap.current_platform_label", return_value=label):
                            with mock.patch("timecapsulesmb.cli.bootstrap.validate_selected_python", return_value="3.9.6"):
                                with mock.patch("timecapsulesmb.cli.bootstrap.find_command", side_effect=fake_which):
                                    with mock.patch("timecapsulesmb.cli.bootstrap.run", side_effect=commands.append):
                                        with redirect_stdout(terminal), redirect_stderr(terminal):
                                            rc = bootstrap.main([])
        return rc, terminal.getvalue(), commands, ensure_venv.called

    def test_bootstrap_on_macos_with_its_own_ssh_installs_nothing(self) -> None:
        rc, text, commands, made_venv = self._bootstrap_on("macOS", present={"ssh"})

        self.assertEqual(rc, 0)
        self.assertEqual(commands, [])
        self.assertTrue(made_venv)
        self.assertIn("Found required host tools: ssh\n", text)
        self.assertIn("Host setup complete.", text)
        self.assertNotIn("smbclient", text)
        self.assertNotIn("Homebrew", text)
        self.assertNotIn("brew install", text)
        self.client.assert_called()
        self.assertEqual(self.telemetry_payload("bootstrap_finished")["result"], "success")

    def test_bootstrap_on_macos_without_ssh_on_path_installs_nothing(self) -> None:
        # Homebrew is on PATH, but bootstrap never installs anything on a Mac.
        rc, text, commands, made_venv = self._bootstrap_on("macOS", present=set())

        self.assertEqual(rc, 1)
        self.assertEqual(commands, [])
        # Only an ssh that is there can be checked before .venv.
        self.assertTrue(made_venv)
        self.assertIn("Missing required host tools: ssh", text)
        self.assertIn("Missing ssh. macOS includes it in /usr/bin: add /usr/bin to PATH", text)
        self.assertNotIn("Homebrew", text)
        self.assertNotIn("brew install", text)
        self.client.assert_not_called()
        finished = self.telemetry_payload("bootstrap_finished")
        self.assertEqual(finished["result"], "failure")
        self.assertIn("stage=install_host_tools", finished["error"])

    def test_bootstrap_on_macos_rejects_an_ssh_older_than_8_4(self) -> None:
        # macOS 11 and older ship OpenSSH 8.1.
        self.client.side_effect = SshClientConfigError(
            "Local SSH client /usr/bin/ssh is OpenSSH_8.1; OpenSSH 8.4 or newer is required. "
            "Upgrade OpenSSH on your computer and retry."
        )

        rc, text, commands, made_venv = self._bootstrap_on("macOS", present={"ssh"})

        self.assertEqual(rc, 1)
        self.assertEqual(commands, [])
        self.assertFalse(made_venv)
        self.assertIn("OpenSSH 8.4 or newer is required", text)
        self.assertIn("The ssh that comes with macOS is new enough from macOS 12 on.", text)
        self.assertNotIn("Host setup complete.", text)
        self.assertEqual(self.telemetry_payload("bootstrap_finished")["result"], "failure")

    def test_bootstrap_on_linux_rejects_an_old_installed_ssh_before_venv(self) -> None:
        # Ubuntu 20.04 ships OpenSSH 8.2, and its package manager has no newer one.
        self.client.side_effect = SshClientConfigError(
            "Local SSH client /usr/bin/ssh is OpenSSH_8.2; OpenSSH 8.4 or newer is required. "
            "Upgrade OpenSSH on your computer and retry."
        )

        rc, text, commands, made_venv = self._bootstrap_on("Linux", present={"ssh", "smbclient"})

        self.assertEqual(rc, 1)
        self.assertEqual(commands, [])
        self.assertFalse(made_venv)
        self.assertIn("OpenSSH 8.4 or newer is required", text)
        self.assertIn("Debian 11, Ubuntu 22.04, RHEL 9 and later ship OpenSSH 8.4 or newer.", text)
        self.assertNotIn("comes with macOS", text)
        self.assertIn("stage=check_ssh", self.telemetry_payload("bootstrap_finished")["error"])

    def test_bootstrap_on_linux_without_ssh_makes_venv_before_installing_it(self) -> None:
        # A failed install still leaves a .venv; here no package manager is found.
        rc, text, commands, made_venv = self._bootstrap_on("Linux", present={"smbclient"})

        self.assertEqual(rc, 1)
        self.assertTrue(made_venv)
        self.assertEqual(commands, [])
        self.client.assert_not_called()
        self.assertIn("No supported Linux package manager found to install missing host tools: ssh", text)
        self.assertIn("stage=install_host_tools", self.telemetry_payload("bootstrap_finished")["error"])

    def _linux_install(
        self,
        manager: str,
        *,
        euid: int,
        has_sudo: bool = True,
        present: frozenset[str] = frozenset(),
        run_error: BaseException | None = None,
        output: io.StringIO | None = None,
    ) -> tuple[list[list[str]], bootstrap.BootstrapError | None]:
        """install_required_host_tools on Linux with `manager` and the `present` tools installed.

        Returns the commands run and the BootstrapError raised, if any. Each
        install command puts the tools it names on the fake PATH, as a real
        one would.
        """
        tools_by_package = {
            package: tool for packages in bootstrap.LINUX_HOST_TOOL_PACKAGES.values() for tool, package in packages.items()
        }
        installed: set[str] = set(present)
        commands: list[list[str]] = []

        def fake_which(name: str):
            found = name == manager or name in installed or (has_sudo and name == "sudo")
            return f"/usr/bin/{name}" if found else None

        def fake_run(cmd):
            commands.append(cmd)
            if run_error is not None:
                raise run_error
            installed.update(tools_by_package[arg] for arg in cmd if arg in tools_by_package)

        with mock.patch("timecapsulesmb.cli.bootstrap.find_command", side_effect=fake_which), \
             mock.patch("timecapsulesmb.cli.bootstrap.os.geteuid", return_value=euid), \
             mock.patch("timecapsulesmb.cli.bootstrap.run", side_effect=fake_run), \
             redirect_stdout(output or io.StringIO()):
            try:
                bootstrap.install_required_host_tools("Linux")
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

    def test_bootstrap_installs_only_smbclient_on_linux_with_ssh(self) -> None:
        # Linux doctor still runs its SMB checks through smbclient.
        output = io.StringIO()
        ran, error = self._linux_install("apt-get", euid=1000, present=frozenset({"ssh"}), output=output)
        self.assertIsNone(error)
        self.assertEqual(ran, [["sudo", "apt-get", "update"], ["sudo", "apt-get", "install", "-y", "smbclient"]])
        self.assertIn("Missing required host tools: smbclient", output.getvalue())
        self.assertIn("Installed required host tools: smbclient", output.getvalue())

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

    def test_bootstrap_orders_the_venv_package_venv_and_host_tools(self) -> None:
        # Host tools come last, so a failure there still leaves a .venv
        # that can deploy.
        venv = ["ensure_venv", "install_python_requirements", "install_required_host_tools"]
        cases = (
            ("Linux", "python3.11-venv", ["install_python_venv_package", *venv]),
            ("Linux", None, venv),
            ("macOS", None, venv),
        )
        for label, venv_package, steps in cases:
            with self.subTest(label=label, venv_package=venv_package):
                calls = mock.Mock()
                calls.ensure_venv.return_value = bootstrap.VENVDIR / "bin" / "python"
                with mock.patch("pathlib.Path.exists", return_value=True), \
                     mock.patch("timecapsulesmb.cli.bootstrap.ensure_install_id"), \
                     mock.patch("timecapsulesmb.cli.bootstrap.current_platform_label", return_value=label), \
                     mock.patch("timecapsulesmb.cli.bootstrap.validate_selected_python", return_value="3.11.9"), \
                     mock.patch("timecapsulesmb.cli.bootstrap._python_venv_package", return_value=venv_package) as probe, \
                     mock.patch("timecapsulesmb.cli.bootstrap.install_python_venv_package", calls.install_python_venv_package), \
                     mock.patch("timecapsulesmb.cli.bootstrap.ensure_venv", calls.ensure_venv), \
                     mock.patch("timecapsulesmb.cli.bootstrap.install_python_requirements", calls.install_python_requirements), \
                     mock.patch("timecapsulesmb.cli.bootstrap.install_required_host_tools", calls.install_required_host_tools), \
                     redirect_stdout(io.StringIO()):
                    self.assertEqual(bootstrap.main([]), 0)
                self.assertEqual([name for name, _args, _kwargs in calls.mock_calls], steps)
                calls.install_required_host_tools.assert_called_once_with(label)
                if venv_package:
                    calls.install_python_venv_package.assert_called_once_with(venv_package)
                self.assertEqual(probe.called, label == "Linux")
                finished = self.telemetry_payload("bootstrap_finished")
                self.assertEqual(finished.get("python_venv_package"), venv_package)

    def test_venv_python_version_reads_pyvenv_cfg_from_venv_and_uv(self) -> None:
        cases = (
            ("home = /usr/bin\ninclude-system-site-packages = false\nversion = 3.9.6\n", "3.9.6"),
            ("home = /opt/python/bin\nimplementation = CPython\nuv = 0.13.6\nversion_info = 3.15.1\n", "3.15.1"),
            ("home = /usr/bin\n", None),
            ("version =\n", None),
        )
        for text, expected in cases:
            with self.subTest(text=text), tempfile.TemporaryDirectory() as tmp:
                (Path(tmp) / "pyvenv.cfg").write_text(text, encoding="utf-8")
                self.assertEqual(bootstrap.venv_python_version(Path(tmp)), expected)
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(bootstrap.venv_python_version(Path(tmp)))

    def test_old_zeroconf_note_only_for_linux_without_dns_sd_below_python_3_10(self) -> None:
        cases = (
            ("Linux", "3.9.6", False, True),
            ("Linux", "3.9.25+", False, True),
            ("Linux", "3.10.0", False, False),
            ("Linux", "3.15.1", False, False),
            # Bonjour uses dns-sd when it is installed and never runs zeroconf.
            ("Linux", "3.9.6", True, False),
            ("macOS", "3.9.6", True, False),
            ("macOS", "3.9.6", False, False),
            ("Linux", "unknown", False, False),
        )
        for label, version, dns_sd, noted in cases:
            with self.subTest(label=label, version=version, dns_sd=dns_sd):
                note = bootstrap.old_zeroconf_note(label, version, dns_sd_installed=dns_sd)
                self.assertEqual(note is not None, noted)
                if noted:
                    self.assertIn("Python 3.10 or newer", note)

    def _bootstrap_finishing_with_venv(
        self, label: str, selected: str, pyvenv_cfg: str | None, *, dns_sd: str | None = None,
    ) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            venv = Path(tmp) / ".venv"
            (venv / "bin").mkdir(parents=True)
            if pyvenv_cfg is not None:
                (venv / "pyvenv.cfg").write_text(pyvenv_cfg, encoding="utf-8")
            output = io.StringIO()
            with mock.patch("pathlib.Path.exists", return_value=True), \
                 mock.patch("timecapsulesmb.cli.bootstrap.ensure_install_id"), \
                 mock.patch("timecapsulesmb.cli.bootstrap.current_platform_label", return_value=label), \
                 mock.patch("timecapsulesmb.cli.bootstrap.validate_selected_python", return_value=selected), \
                 mock.patch("timecapsulesmb.cli.bootstrap._python_venv_package", return_value=None), \
                 mock.patch("timecapsulesmb.cli.bootstrap.ensure_venv", return_value=venv / "bin" / "python"), \
                 mock.patch("timecapsulesmb.cli.bootstrap.install_python_requirements"), \
                 mock.patch("timecapsulesmb.cli.bootstrap.install_required_host_tools"), \
                 mock.patch(
                     "timecapsulesmb.cli.bootstrap.find_command",
                     side_effect=lambda name: dns_sd if name == "dns-sd" else None,
                 ), \
                 redirect_stdout(output):
                self.assertEqual(bootstrap.main([]), 0)
            return output.getvalue()

    def test_bootstrap_notes_old_zeroconf_from_the_python_venv_was_made_with(self) -> None:
        cases = (
            # An existing .venv keeps its Python, so pyvenv.cfg wins over --python.
            ("Linux", "3.12.3", "version = 3.9.6\n", True, "3.9.6"),
            ("Linux", "3.9.6", "version_info = 3.12.3\n", False, "3.12.3"),
            # Without a readable pyvenv.cfg the selected Python is the best guess.
            ("Linux", "3.9.6", None, True, "3.9.6"),
            ("macOS", "3.9.6", "version = 3.9.6\n", False, "3.9.6"),
        )
        for label, selected, cfg, noted, recorded in cases:
            with self.subTest(label=label, selected=selected, cfg=cfg):
                text = self._bootstrap_finishing_with_venv(label, selected, cfg)
                self.assertEqual(bootstrap.OLD_ZEROCONF_NOTE in text, noted)
                if noted:
                    # Printed before the closing summary, not after it.
                    self.assertLess(text.index(bootstrap.OLD_ZEROCONF_NOTE), text.index("Host setup complete."))
                self.assertEqual(self.telemetry_payload("bootstrap_finished")["venv_python_version"], recorded)

    def test_bootstrap_skips_the_zeroconf_note_when_dns_sd_is_installed(self) -> None:
        text = self._bootstrap_finishing_with_venv("Linux", "3.9.6", "version = 3.9.6\n", dns_sd="/usr/bin/dns-sd")
        self.assertNotIn(bootstrap.OLD_ZEROCONF_NOTE, text)
        self.assertIn("Host setup complete.", text)

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
        with mock.patch("timecapsulesmb.cli.bootstrap.find_command", return_value=None):
            with self.assertRaises(bootstrap.BootstrapError):
                with redirect_stdout(output):
                    bootstrap.install_required_host_tools("Linux")
        self.assertIn("No supported Linux package manager found", output.getvalue())
        self.assertNotIn("Mac app", output.getvalue())


if __name__ == "__main__":
    unittest.main()
