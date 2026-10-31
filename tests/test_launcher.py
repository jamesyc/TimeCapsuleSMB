from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = REPO_ROOT / "tcapsule"


class LauncherTests(unittest.TestCase):
    def _copy_launcher(self, root: Path) -> Path:
        launcher = root / "tcapsule"
        shutil.copy(LAUNCHER, launcher)
        return launcher

    def _run_launcher(self, root: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(root / "tcapsule"), *args],
            cwd=root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def _write_fake_venv_launcher(self, root: Path) -> Path:
        launcher = root / ".venv" / "bin" / "tcapsule"
        launcher.parent.mkdir(parents=True)
        launcher.write_text(
            "#!/usr/bin/env python3\n"
            "import json\n"
            "import sys\n"
            "print(json.dumps(sys.argv))\n"
        )
        launcher.chmod(0o755)
        return launcher

    def _write_source_main(self, root: Path) -> None:
        package = root / "src" / "timecapsulesmb" / "cli"
        package.mkdir(parents=True)
        (root / "src" / "timecapsulesmb" / "__init__.py").write_text("")
        (package / "__init__.py").write_text("")
        (package / "main.py").write_text(
            "import sys\n"
            "def main():\n"
            "    print('source-main:' + ' '.join(sys.argv[1:]))\n"
            "    return 0\n"
        )

    def test_non_bootstrap_command_requires_bootstrap_before_importing_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._copy_launcher(root)

            result = self._run_launcher(root, "configure")

        self.assertEqual(result.returncode, 1)
        self.assertIn("Run `./tcapsule bootstrap` first", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_python_older_than_3_9_is_refused_before_importing_source(self) -> None:
        cases = {(3, 8, 20, "final", 0): False, (3, 9, 0, "final", 0): True}
        for version, runs in cases.items():
            with self.subTest(version=version), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                launcher = self._copy_launcher(root)
                self._write_source_main(root)
                # The launcher reads sys.version_info when it runs.
                script = (
                    "import runpy, sys\n"
                    f"sys.version_info = {version!r}\n"
                    f"sys.argv = [{str(launcher)!r}, 'bootstrap']\n"
                    f"runpy.run_path({str(launcher)!r}, run_name='__main__')\n"
                )
                result = subprocess.run(
                    [sys.executable, "-c", script], cwd=root, text=True, capture_output=True, check=False,
                )

                if runs:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout.strip(), "source-main:bootstrap")
                else:
                    self.assertEqual(result.returncode, 1)
                    self.assertEqual(result.stdout, "")
                    self.assertIn("TimeCapsuleSMB needs Python 3.9 or later; this is Python 3.8.", result.stderr)
                    self.assertIn("`python3.13 ./tcapsule bootstrap`", result.stderr)

    def test_old_python_still_hands_off_to_a_bootstrapped_virtualenv(self) -> None:
        # Bootstrapped with a newer Python, a checkout runs its commands with
        # .venv's own Python whatever python3 started the launcher.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            launcher = self._copy_launcher(root)
            self._write_fake_venv_launcher(root)
            script = (
                "import runpy, sys\n"
                "sys.version_info = (3, 8, 20, 'final', 0)\n"
                f"sys.argv = [{str(launcher)!r}, 'configure', '--name', 'Capsule']\n"
                f"runpy.run_path({str(launcher)!r}, run_name='__main__')\n"
            )
            result = subprocess.run(
                [sys.executable, "-c", script], cwd=root, text=True, capture_output=True, check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)[1:], ["configure", "--name", "Capsule"])
        self.assertNotIn("needs Python 3.9", result.stderr)

    def test_top_level_help_runs_before_bootstrap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._copy_launcher(root)
            self._write_source_main(root)

            result = self._run_launcher(root, "--help")

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "source-main:--help")

    def test_no_command_runs_source_usage_before_bootstrap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._copy_launcher(root)
            self._write_source_main(root)

            result = self._run_launcher(root)

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "source-main:")

    def test_incomplete_virtualenv_reports_repair_guidance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._copy_launcher(root)
            (root / ".venv").mkdir()
            (root / ".venv" / "pyvenv.cfg").write_text("")

            result = self._run_launcher(root, "doctor")

        self.assertEqual(result.returncode, 1)
        self.assertIn("bootstrap is incomplete", result.stderr)
        self.assertIn("Run `./tcapsule bootstrap` again", result.stderr)

    def test_non_executable_virtualenv_launcher_reports_repair_guidance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._copy_launcher(root)
            self._write_fake_venv_launcher(root).chmod(0o644)

            result = self._run_launcher(root, "doctor")

        self.assertEqual(result.returncode, 1)
        self.assertIn("bootstrap is incomplete", result.stderr)
        self.assertIn("missing or not executable", result.stderr)

    def test_non_bootstrap_command_execs_bootstrapped_launcher_with_original_args(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._copy_launcher(root)
            fake_launcher = self._write_fake_venv_launcher(root)

            result = self._run_launcher(root, "doctor", "--json")

        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout), [str(fake_launcher.resolve()), "doctor", "--json"])
        self.assertEqual(result.stderr, "")

    def test_virtualenv_handoff_preserves_shell_sensitive_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._copy_launcher(root)
            fake_launcher = self._write_fake_venv_launcher(root)

            result = self._run_launcher(
                root,
                "configure",
                "--name",
                "James's Capsule",
                "--pattern",
                "$(reboot) & done",
                "--literal",
                "*.sparsebundle",
            )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            json.loads(result.stdout),
            [
                str(fake_launcher.resolve()),
                "configure",
                "--name",
                "James's Capsule",
                "--pattern",
                "$(reboot) & done",
                "--literal",
                "*.sparsebundle",
            ],
        )
        self.assertEqual(result.stderr, "")

    def test_launcher_does_not_handoff_when_already_running_in_target_virtualenv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._copy_launcher(root)
            self._write_source_main(root)
            (root / ".venv").symlink_to(Path(sys.prefix).resolve(), target_is_directory=True)

            result = self._run_launcher(root, "doctor")

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "source-main:doctor")
        self.assertEqual(result.stderr, "")

    def test_bootstrap_command_runs_source_entrypoint_without_virtualenv_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._copy_launcher(root)
            self._write_fake_venv_launcher(root)
            self._write_source_main(root)

            result = self._run_launcher(root, "bootstrap")

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "source-main:bootstrap")


if __name__ == "__main__":
    unittest.main()
