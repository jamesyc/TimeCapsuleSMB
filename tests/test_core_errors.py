from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from timecapsulesmb.core.errors import missing_dependency_message


class MissingDependencyMessageTests(unittest.TestCase):
    def _write_source_checkout_markers(self, root: Path) -> None:
        (root / "tcapsule").write_text("#!/usr/bin/env python3\n")
        (root / "src" / "timecapsulesmb").mkdir(parents=True)

    def _message_for_root(self, root: Path) -> str:
        with mock.patch("timecapsulesmb.core.errors.package_project_root", return_value=root):
            return missing_dependency_message("pexpect", ModuleNotFoundError("No module named 'pexpect'"))

    def test_unbootstrapped_source_checkout_reports_bootstrap_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_source_checkout_markers(root)

            message = self._message_for_root(root)

        self.assertIn("Failed to load pexpect", message)
        self.assertIn("Run `./tcapsule bootstrap` first", message)

    def test_incomplete_source_virtualenv_reports_repair_guidance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_source_checkout_markers(root)
            (root / ".venv").mkdir()
            (root / ".venv" / "pyvenv.cfg").write_text("")

            message = self._message_for_root(root)

        self.assertIn("virtualenv at `.venv` is incomplete", message)
        self.assertIn("Rerun `./tcapsule bootstrap` to repair it", message)

    def test_bootstrapped_source_checkout_running_wrong_python_reports_venv_launcher(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_source_checkout_markers(root)
            launcher = root / ".venv" / "bin" / "tcapsule"
            launcher.parent.mkdir(parents=True)
            launcher.write_text("")

            with mock.patch("timecapsulesmb.core.errors.package_project_root", return_value=root):
                with mock.patch("timecapsulesmb.core.errors.sys.prefix", str(root / "other-venv")):
                    with mock.patch("timecapsulesmb.core.errors.sys.executable", "/usr/bin/python3"):
                        with mock.patch(
                            "timecapsulesmb.core.errors.sys.argv",
                            ["tcapsule", "configure", "--name", "My Capsule", "--pattern", "disk; reboot"],
                        ):
                            message = missing_dependency_message("pexpect", ModuleNotFoundError("No module named 'pexpect'"))

        self.assertIn("already has a bootstrapped virtualenv", message)
        self.assertIn("running with Python /usr/bin/python3", message)
        self.assertIn(
            "Run `.venv/bin/tcapsule configure --name 'My Capsule' --pattern 'disk; reboot'` instead",
            message,
        )

    def test_wrong_python_guidance_quotes_shell_sensitive_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_source_checkout_markers(root)
            launcher = root / ".venv" / "bin" / "tcapsule"
            launcher.parent.mkdir(parents=True)
            launcher.write_text("")

            with mock.patch("timecapsulesmb.core.errors.package_project_root", return_value=root):
                with mock.patch("timecapsulesmb.core.errors.sys.prefix", str(root / "other-venv")):
                    with mock.patch("timecapsulesmb.core.errors.sys.executable", "/usr/bin/python3"):
                        with mock.patch(
                            "timecapsulesmb.core.errors.sys.argv",
                            [
                                "tcapsule",
                                "configure",
                                "--name",
                                "James's Capsule",
                                "--pattern",
                                "$(reboot) & done",
                                "--literal",
                                "*.sparsebundle",
                            ],
                        ):
                            message = missing_dependency_message("pexpect", ModuleNotFoundError("No module named 'pexpect'"))

        self.assertIn(
            "Run `.venv/bin/tcapsule configure --name 'James'\"'\"'s Capsule' "
            "--pattern '$(reboot) & done' --literal '*.sparsebundle'` instead",
            message,
        )

    def test_active_source_virtualenv_missing_dependency_reports_repair_guidance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_source_checkout_markers(root)
            launcher = root / ".venv" / "bin" / "tcapsule"
            launcher.parent.mkdir(parents=True)
            launcher.write_text("")

            with mock.patch("timecapsulesmb.core.errors.package_project_root", return_value=root):
                with mock.patch("timecapsulesmb.core.errors.sys.prefix", str(root / ".venv")):
                    message = missing_dependency_message("pexpect", ModuleNotFoundError("No module named 'pexpect'"))

        self.assertIn("virtualenv at `.venv` is active but missing required packages", message)
        self.assertIn("Rerun `./tcapsule bootstrap` to repair it", message)

    def test_path_detection_failure_preserves_generic_bootstrap_guidance(self) -> None:
        with mock.patch("timecapsulesmb.core.errors.package_project_root", side_effect=RuntimeError("boom")):
            message = missing_dependency_message("pexpect", ModuleNotFoundError("No module named 'pexpect'"))

        self.assertIn("Run `./tcapsule bootstrap` first to set up the required dependencies", message)

    def test_filesystem_detection_failure_preserves_generic_bootstrap_guidance(self) -> None:
        with mock.patch("timecapsulesmb.core.errors.package_project_root", side_effect=OSError("permission denied")):
            message = missing_dependency_message("pexpect", ModuleNotFoundError("No module named 'pexpect'"))

        self.assertIn("Run `./tcapsule bootstrap` first to set up the required dependencies", message)

    def test_unexpected_path_detection_error_is_not_swallowed(self) -> None:
        with mock.patch("timecapsulesmb.core.errors.package_project_root", side_effect=ValueError("bad root")):
            with self.assertRaises(ValueError):
                missing_dependency_message("pexpect", ModuleNotFoundError("No module named 'pexpect'"))


if __name__ == "__main__":
    unittest.main()
