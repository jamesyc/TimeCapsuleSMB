from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from timecapsulesmb.services.version_check import (
    DEFAULT_DOWNLOAD_URL,
    DEFAULT_UNSUPPORTED_MESSAGE,
    MAX_VERSION_RESPONSE_BYTES,
    VERSION_CHECK_CACHE_SECONDS,
    VERSION_CHECK_TIMEOUT_SECONDS,
    VERSION_CHECK_URL,
    VersionCheckResult,
    check_client_version,
    render_version_block_message,
    save_cached_payload,
)
from timecapsulesmb.transport.http import HttpError


class VersionCheckTests(unittest.TestCase):
    def metadata(
        self,
        *,
        current_version: int = 20004,
        min_supported_version: int = 20004,
        download_url: str = DEFAULT_DOWNLOAD_URL,
        message: str = DEFAULT_UNSUPPORTED_MESSAGE,
    ) -> dict[str, object]:
        return {
            "schema": 1,
            "current_version": current_version,
            "min_supported_version": min_supported_version,
            "latest_tag": "v2.0.4",
            "download_url": download_url,
            "message": message,
        }

    def serving(self, response: object, calls: list[tuple[str, dict[str, object]]]):
        """Patch the version check's GET: return response (JSON-encoded unless
        bytes) or raise it, recording each call."""

        def fake_get(url: str, **kwargs: object) -> bytes:
            calls.append((url, kwargs))
            if isinstance(response, BaseException):
                raise response
            return response if isinstance(response, bytes) else json.dumps(response).encode("utf-8")

        return mock.patch("timecapsulesmb.services.version_check.http_get", side_effect=fake_get)

    def test_supported_client_fetches_and_caches_successful_response(self) -> None:
        self.assertEqual(VERSION_CHECK_CACHE_SECONDS, 3 * 60 * 60)
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "version-cache.json"
            calls: list[tuple[str, dict[str, object]]] = []

            with self.serving(self.metadata(), calls):
                result = check_client_version(local_version_code=20004, cache_path=cache_path, now=1000.0)

            self.assertFalse(result.should_block)
            self.assertEqual(result.source, "network")
            self.assertEqual(result.current_version, 20004)
            self.assertEqual(result.min_supported_version, 20004)
            self.assertEqual(result.latest_tag, "v2.0.4")
            self.assertEqual(
                calls,
                [(VERSION_CHECK_URL, {
                    "timeout": VERSION_CHECK_TIMEOUT_SECONDS,
                    "max_bytes": MAX_VERSION_RESPONSE_BYTES,
                    "headers": {"Accept": "application/json"},
                })],
            )
            cache = json.loads(cache_path.read_text())
            self.assertEqual(cache["fetched_at"], 1000.0)
            self.assertEqual(cache["payload"]["min_supported_version"], 20004)

    def test_outdated_client_blocks_with_remote_message_and_download_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "version-cache.json"
            download_url = "https://example.invalid/releases/latest"
            message = "Please update before continuing."
            calls: list[tuple[str, dict[str, object]]] = []
            metadata = self.metadata(
                current_version=20005,
                min_supported_version=20005,
                download_url=download_url,
                message=message,
            )

            with self.serving(metadata, calls):
                result = check_client_version(local_version_code=20004, cache_path=cache_path, now=1000.0)

            self.assertTrue(result.should_block)
            self.assertEqual(result.message, message)
            self.assertEqual(result.download_url, download_url)
            self.assertEqual(result.source, "network")
            self.assertEqual(result.current_version, 20005)
            self.assertEqual(result.min_supported_version, 20005)
            self.assertEqual(len(calls), 1)

    def test_invalid_or_unreachable_version_metadata_fails_open(self) -> None:
        cases = (
            # http_get raises HttpError for a timeout, an error status and an
            # oversized response alike.
            ("unreachable_or_oversized", HttpError("timed out")),
            ("invalid_json", b"{"),
            ("invalid_utf8", b"\xff"),
            ("non_object", []),
            ("unsupported_schema", {**self.metadata(), "schema": 2}),
            ("missing_current_version", {"schema": 1, "min_supported_version": 20005}),
            ("missing_min_supported_version", {"schema": 1, "current_version": 20005}),
            ("boolean_min_supported_version", {**self.metadata(), "min_supported_version": True}),
            ("current_version_below_min_supported", self.metadata(current_version=20004, min_supported_version=20005)),
        )
        for name, case in cases:
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory() as tmp:
                    cache_path = Path(tmp) / "version-cache.json"
                    calls: list[tuple[str, dict[str, object]]] = []

                    with self.serving(case, calls):
                        result = check_client_version(local_version_code=20004, cache_path=cache_path, now=1000.0)

                    self.assertFalse(result.should_block)
                    self.assertEqual(len(calls), 1)
                    self.assertFalse(cache_path.exists())

    def test_blocking_result_uses_defaults_for_missing_text_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "version-cache.json"
            calls: list[tuple[str, dict[str, object]]] = []
            payload = {
                "schema": 1,
                "current_version": 20005,
                "min_supported_version": 20005,
            }

            with self.serving(payload, calls):
                result = check_client_version(local_version_code=20004, cache_path=cache_path, now=1000.0)

            self.assertTrue(result.should_block)
            self.assertEqual(result.message, DEFAULT_UNSUPPORTED_MESSAGE)
            self.assertEqual(result.download_url, DEFAULT_DOWNLOAD_URL)

    def test_fresh_supported_cache_skips_fetch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "version-cache.json"
            save_cached_payload(self.metadata(min_supported_version=20003), cache_path=cache_path, now=1000.0)
            calls: list[tuple[str, dict[str, object]]] = []

            with self.serving(self.metadata(current_version=20005, min_supported_version=20005), calls):
                result = check_client_version(local_version_code=20004, cache_path=cache_path, now=1000.0 + 60)

            self.assertFalse(result.should_block)
            self.assertEqual(result.source, "cache")
            self.assertEqual(result.current_version, 20004)
            self.assertEqual(calls, [])

    def test_stale_cache_fetches_remote_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "version-cache.json"
            save_cached_payload(self.metadata(min_supported_version=20003), cache_path=cache_path, now=1000.0)
            calls: list[tuple[str, dict[str, object]]] = []

            with self.serving(self.metadata(current_version=20005, min_supported_version=20005), calls):
                result = check_client_version(
                    local_version_code=20004,
                    cache_path=cache_path,
                    now=1000.0 + VERSION_CHECK_CACHE_SECONDS + 1,
                )

            self.assertTrue(result.should_block)
            self.assertEqual(len(calls), 1)

    def test_fresh_cached_block_is_confirmed_before_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "version-cache.json"
            save_cached_payload(
                self.metadata(current_version=20005, min_supported_version=20005),
                cache_path=cache_path,
                now=1000.0,
            )
            calls: list[tuple[str, dict[str, object]]] = []

            with self.serving(HttpError("offline"), calls):
                result = check_client_version(local_version_code=20004, cache_path=cache_path, now=1000.0 + 60)

            self.assertFalse(result.should_block)
            self.assertEqual(len(calls), 1)

    def test_cache_write_failure_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp)
            calls: list[tuple[str, dict[str, object]]] = []

            with self.serving(self.metadata(), calls):
                result = check_client_version(local_version_code=20004, cache_path=cache_path, now=1000.0)

            self.assertFalse(result.should_block)
            self.assertEqual(len(calls), 1)

    def test_unexpected_internal_exception_fails_open(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "version-cache.json"
            with mock.patch("timecapsulesmb.services.version_check.load_fresh_cached_payload", side_effect=RuntimeError("boom")):
                with self.serving(self.metadata(current_version=20005, min_supported_version=20005), []):
                    result = check_client_version(local_version_code=20004, cache_path=cache_path, now=1000.0)

            self.assertFalse(result.should_block)

    def test_render_version_block_message_exposes_checked_url_and_download_url(self) -> None:
        result = VersionCheckResult(
            should_block=True,
            checked_url=VERSION_CHECK_URL,
            message="Update required.",
            download_url=DEFAULT_DOWNLOAD_URL,
        )

        text = render_version_block_message(result)

        self.assertIn(f"Checking current version from: {VERSION_CHECK_URL}", text)
        self.assertIn("Update required.", text)
        self.assertIn(f"Client version is out of date, download the latest version from: {DEFAULT_DOWNLOAD_URL}", text)


if __name__ == "__main__":
    unittest.main()
