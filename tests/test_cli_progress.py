from __future__ import annotations

import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from timecapsulesmb.cli.runtime import emit_progress, prefixed_logger


class CliProgressTests(unittest.TestCase):
    def test_prefixed_logger_emits_when_enabled(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            log = prefixed_logger("flash", enabled=True)
            emit_progress(log, "Reading bank")

        self.assertEqual(output.getvalue(), "[flash] Reading bank\n")

    def test_progress_noops_when_disabled_or_missing(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            emit_progress(prefixed_logger("flash", enabled=False), "hidden")
            emit_progress(None, "hidden")

        self.assertEqual(output.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
