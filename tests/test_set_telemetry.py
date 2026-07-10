from __future__ import annotations

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from timecapsulesmb.cli import bootstrap, set_telemetry
from timecapsulesmb.identity import load_install_identity


class TelemetryPromptTests(unittest.TestCase):
    def test_prompt_returns_false_under_no_input(self) -> None:
        self.assertFalse(bootstrap._prompt_telemetry_choice(no_input=True))

    def test_prompt_returns_false_when_stdin_not_tty(self) -> None:
        with mock.patch.object(sys.stdin, "isatty", return_value=False):
            self.assertFalse(bootstrap._prompt_telemetry_choice(no_input=False))

    def test_prompt_accepts_yes(self) -> None:
        with mock.patch.object(sys.stdin, "isatty", return_value=True):
            with mock.patch("builtins.input", return_value="y"):
                self.assertTrue(bootstrap._prompt_telemetry_choice(no_input=False))

    def test_first_run_leaves_telemetry_off_under_no_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".bootstrap"
            with mock.patch("timecapsulesmb.cli.bootstrap.default_bootstrap_path", return_value=path):
                bootstrap._apply_first_run_telemetry_choice(no_input=True)
            self.assertFalse(load_install_identity(path).telemetry_enabled)
            self.assertIsNotNone(load_install_identity(path).install_id)


class SetTelemetryCommandTests(unittest.TestCase):
    def _run(self, argv: list[str], path: Path) -> tuple[int, str]:
        buffer = io.StringIO()
        with mock.patch("timecapsulesmb.cli.set_telemetry.default_bootstrap_path", return_value=path):
            with redirect_stdout(buffer):
                rc = set_telemetry.main(argv)
        return rc, buffer.getvalue()

    def test_enable_then_disable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".bootstrap"
            path.write_text("INSTALL_ID=install\n")

            rc, out = self._run(["--enable"], path)
            self.assertEqual(rc, 0)
            self.assertIn("enabled", out)
            self.assertTrue(load_install_identity(path).telemetry_enabled)

            rc, out = self._run(["--disable"], path)
            self.assertEqual(rc, 0)
            self.assertIn("disabled", out)
            self.assertFalse(load_install_identity(path).telemetry_enabled)

    def test_status_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".bootstrap"
            path.write_text("INSTALL_ID=install\nTELEMETRY=true\n")
            rc, out = self._run(["--status"], path)
            self.assertEqual(rc, 0)
            self.assertIn("enabled", out)


if __name__ == "__main__":
    unittest.main()
