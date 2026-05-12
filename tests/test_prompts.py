from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from timecapsulesmb.cli.runtime import NonInteractivePromptError, confirm


class PromptTests(unittest.TestCase):
    def test_confirm_uses_default_for_blank_answer(self) -> None:
        with mock.patch("builtins.input", return_value=""):
            self.assertTrue(confirm("Continue?", default=True))
        with mock.patch("builtins.input", return_value=""):
            self.assertFalse(confirm("Continue?", default=False))

    def test_confirm_accepts_yes_and_no(self) -> None:
        with mock.patch("builtins.input", return_value="yes"):
            self.assertTrue(confirm("Continue?", default=False))
        with mock.patch("builtins.input", return_value="n"):
            self.assertFalse(confirm("Continue?", default=True))

    def test_confirm_retries_invalid_answer(self) -> None:
        with mock.patch("builtins.input", side_effect=["maybe", "y"]):
            self.assertTrue(confirm("Continue?", default=False))

    def test_confirm_uses_eof_default_when_provided(self) -> None:
        with mock.patch("builtins.input", side_effect=EOFError):
            self.assertFalse(confirm("Continue?", default=True, eof_default=False))

    def test_confirm_raises_noninteractive_error_without_eof_default(self) -> None:
        with mock.patch("builtins.input", side_effect=EOFError):
            with self.assertRaises(NonInteractivePromptError) as raised:
                confirm("Continue?", default=False, noninteractive_message="no stdin")
        self.assertEqual(str(raised.exception), "no stdin")


if __name__ == "__main__":
    unittest.main()
