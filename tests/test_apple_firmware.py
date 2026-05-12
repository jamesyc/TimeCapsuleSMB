from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from timecapsulesmb.apple_firmware import firmware_template_cache_path


class AppleFirmwareTests(unittest.TestCase):
    def test_cache_path_sanitizes_dot_dot_components(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)

            path = firmware_template_cache_path(
                cache_dir=cache_dir,
                product_id="..",
                version="..",
                url="https://example.invalid/../firmware.basebinary",
            )

        self.assertNotIn("..", path.relative_to(cache_dir).parts)
        self.assertEqual(path.parent.name, "device")
        self.assertTrue(path.name.startswith("device-"))


if __name__ == "__main__":
    unittest.main()
