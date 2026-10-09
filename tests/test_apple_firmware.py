from __future__ import annotations

import plistlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from timecapsulesmb.transport.http import HttpError

from timecapsulesmb.apple_firmware import (
    FlashAnalysisError,
    download_firmware_template_to_cache,
    firmware_template_cache_path,
    load_apple_firmware_catalog,
)


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

    def test_catalog_cache_write_failure_keeps_existing_catalog(self) -> None:
        old_catalog = plistlib.dumps({
            "firmwareUpdates": [
                {
                    "productID": "113",
                    "version": "7.8.1",
                    "location": "https://example.invalid/old.basebinary",
                }
            ]
        })
        new_catalog = plistlib.dumps({
            "firmwareUpdates": [
                {
                    "productID": "113",
                    "version": "7.8.2",
                    "location": "https://example.invalid/new.basebinary",
                }
            ]
        })

        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            catalog_path = cache_dir / "version.xml"
            catalog_path.write_bytes(old_catalog)

            with mock.patch("timecapsulesmb.apple_firmware.http_get", return_value=new_catalog):
                with mock.patch("timecapsulesmb.apple_firmware.os.replace", side_effect=OSError("disk full")):
                    entries = load_apple_firmware_catalog(cache_dir=cache_dir)

            leftovers = list(cache_dir.glob(".version.xml.*.tmp"))
            cached_catalog = catalog_path.read_bytes()

        self.assertEqual(entries[0]["version"], "7.8.1")
        self.assertEqual(cached_catalog, old_catalog)
        self.assertEqual(leftovers, [])

    def test_bad_download_preserves_and_uses_a_good_catalog(self) -> None:
        valid = plistlib.dumps({"firmwareUpdates": [{"version": "old"}]})
        for download in (b"<partial", plistlib.dumps({}), plistlib.dumps({"firmwareUpdates": {}}), HttpError("short body")):
            with self.subTest(download=download), tempfile.TemporaryDirectory() as tmp:
                cache = Path(tmp)
                path = cache / "version.xml"
                path.write_bytes(valid)
                kwargs = {"side_effect": download} if isinstance(download, Exception) else {"return_value": download}
                with mock.patch("timecapsulesmb.apple_firmware.http_get", **kwargs):
                    self.assertEqual(load_apple_firmware_catalog(cache_dir=cache), [{"version": "old"}])
                self.assertEqual(path.read_bytes(), valid)
                self.assertEqual(list(cache.iterdir()), [path])

    def test_bad_download_without_a_good_cache_fails(self) -> None:
        for cached in (None, b"<broken", plistlib.dumps({})):
            for downloaded in (b"<partial", plistlib.dumps({}), HttpError("short body")):
                with self.subTest(cached=cached, downloaded=downloaded), tempfile.TemporaryDirectory() as tmp:
                    cache = Path(tmp)
                    path = cache / "version.xml"
                    if cached is not None:
                        path.write_bytes(cached)
                    kwargs = {"side_effect": downloaded} if isinstance(downloaded, Exception) else {"return_value": downloaded}
                    with mock.patch("timecapsulesmb.apple_firmware.http_get", **kwargs):
                        with self.assertRaises(FlashAnalysisError):
                            load_apple_firmware_catalog(cache_dir=cache)
                    self.assertEqual(path.read_bytes() if path.exists() else None, cached)

    def test_valid_catalog_replaces_an_absent_or_bad_or_old_cache(self) -> None:
        valid = plistlib.dumps({"firmwareUpdates": [{"version": "new"}]})
        for cached in (None, b"<broken", plistlib.dumps({"firmwareUpdates": [{"version": "old"}]})):
            with self.subTest(cached=cached), tempfile.TemporaryDirectory() as tmp:
                cache = Path(tmp)
                path = cache / "version.xml"
                if cached is not None:
                    path.write_bytes(cached)
                with mock.patch("timecapsulesmb.apple_firmware.http_get", return_value=valid):
                    self.assertEqual(load_apple_firmware_catalog(cache_dir=cache), [{"version": "new"}])
                self.assertEqual(path.read_bytes(), valid)

    def test_template_download_error_includes_its_url_once(self) -> None:
        url = "https://example.invalid/firmware"
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch("timecapsulesmb.apple_firmware.http_get", side_effect=HttpError(f"{url}: short body")):
            with self.assertRaises(FlashAnalysisError) as raised:
                download_firmware_template_to_cache(
                    url=url, path=Path(tmp) / "firmware", product_id="113", version="7.8.1", expected_size=10,
                )
        self.assertEqual(str(raised.exception).count(url), 1)

    def test_template_cache_write_failure_does_not_leave_target_or_temp_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            path = cache_dir / "113" / "7.8.1.basebinary"

            with mock.patch("timecapsulesmb.apple_firmware.http_get", return_value=b"template"):
                with mock.patch("timecapsulesmb.apple_firmware.os.replace", side_effect=OSError("disk full")):
                    with self.assertRaises(FlashAnalysisError) as raised:
                        download_firmware_template_to_cache(
                            url="https://example.invalid/7.8.1.basebinary",
                            path=path,
                            product_id="113",
                            version="7.8.1",
                            expected_size=len(b"template"),
                        )

            leftovers = list(path.parent.glob(f".{path.name}.*.tmp"))
            target_exists = path.exists()

        self.assertIn("failed to write Apple firmware template cache", str(raised.exception))
        self.assertFalse(target_exists)
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
