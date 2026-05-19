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
