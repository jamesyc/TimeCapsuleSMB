from __future__ import annotations

import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from timecapsulesmb.transport.local import run_local_capture


class LocalTransportTests(unittest.TestCase):
    def test_run_local_capture_returns_stdout(self) -> None:
        proc = run_local_capture(["/bin/sh", "-c", "printf 'ok'"])
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "ok")


if __name__ == "__main__":
    unittest.main()
