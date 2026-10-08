from __future__ import annotations

import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from timecapsulesmb.cli.fsck import prompt_fsck_target
from timecapsulesmb.cli.runtime import TERMINAL_INPUT_ATTEMPTS, NonInteractivePromptError, confirm
from timecapsulesmb.core.config import ConfigError
from timecapsulesmb.services.maintenance import FsckTarget


def undecodable() -> UnicodeDecodeError:
    # What a UTF-8 stdin raises for a KOI8/CP1251 byte (telemetry, v3.1.1).
    return UnicodeDecodeError("utf-8", b"\xd0a", 0, 1, "invalid continuation byte")


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
        with mock.patch("builtins.input", side_effect=EOFError("EOF when reading a line")):
            with self.assertRaises(NonInteractivePromptError) as raised:
                confirm("Continue?", default=False, noninteractive_message="no stdin")
        self.assertEqual(str(raised.exception), "no stdin")
        self.assertIsInstance(raised.exception.__cause__, EOFError)

    def test_confirm_asks_again_after_input_the_terminal_encoding_cannot_decode(self) -> None:
        output = io.StringIO()
        with mock.patch("builtins.input", side_effect=[undecodable(), "y"]) as input_mock:
            with redirect_stdout(output):
                self.assertTrue(confirm("Continue?", default=False))
        self.assertEqual(input_mock.call_count, 2)
        self.assertIn("could not be read as", output.getvalue())

    def test_confirm_gives_up_with_a_config_error_not_a_traceback(self) -> None:
        with mock.patch("builtins.input", side_effect=undecodable()) as input_mock:
            with redirect_stdout(io.StringIO()):
                with self.assertRaises(ConfigError) as raised:
                    confirm("Continue?", default=False)
        self.assertEqual(input_mock.call_count, TERMINAL_INPUT_ATTEMPTS)
        self.assertIn("could not be read as", str(raised.exception))

    def test_fsck_volume_prompt_asks_again_until_it_gets_a_listed_number(self) -> None:
        targets = (
            FsckTarget("dk2", "/Volumes/dk2", "Data", True),
            FsckTarget("dk3", "/Volumes/dk3", "USB", False),
        )
        output = io.StringIO()
        with mock.patch("builtins.input", side_effect=[undecodable(), "3", "2"]) as input_mock:
            with redirect_stdout(output):
                self.assertEqual(prompt_fsck_target(targets), targets[1])
        self.assertEqual(input_mock.call_count, 3)
        self.assertIn("could not be read as", output.getvalue())
        self.assertIn("Please enter a valid volume number.", output.getvalue())


if __name__ == "__main__":
    unittest.main()
