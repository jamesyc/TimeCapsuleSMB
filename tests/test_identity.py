from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import sys


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from timecapsulesmb.identity import ensure_install_id, load_install_identity, parse_bootstrap_values


class IdentityTests(unittest.TestCase):
    def test_ensure_install_id_creates_bootstrap_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".bootstrap"
            install_id = ensure_install_id(path)
            self.assertTrue(install_id)
            values = parse_bootstrap_values(path)
            self.assertEqual(values["INSTALL_ID"], install_id)
            self.assertNotIn("TELEMETRY", values)

    def test_ensure_install_id_preserves_telemetry_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".bootstrap"
            path.write_text("TELEMETRY=false\n")
            install_id = ensure_install_id(path)
            self.assertTrue(install_id)
            identity = load_install_identity(path)
            self.assertEqual(identity.install_id, install_id)
            self.assertFalse(identity.telemetry_enabled)


if __name__ == "__main__":
    unittest.main()
