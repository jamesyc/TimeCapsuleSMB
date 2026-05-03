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

from timecapsulesmb.cli.version_check import (
    DEFAULT_DOWNLOAD_URL,
    DEFAULT_UNSUPPORTED_MESSAGE,
    VERSION_CHECK_CACHE_SECONDS,
    VERSION_CHECK_TIMEOUT_SECONDS,
    VERSION_CHECK_URL,
    VersionCheckResult,
    check_client_version,
    render_version_block_message,
    save_cached_payload,
)


class FakeResponse:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> bool:
        return False

    def read(self, _size: int = -1) -> bytes:
        return self.body


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

    def opener_for_payload(self, payload: object, calls: list[tuple[object, float]]) -> object:
        body = json.dumps(payload).encode("utf-8")

        def fake_opener(request, timeout):
            calls.append((request, timeout))
            return FakeResponse(body)

        return fake_opener

    def test_supported_client_fetches_and_caches_successful_response(self) -> None:
        self.assertEqual(VERSION_CHECK_CACHE_SECONDS, 3 * 60 * 60)
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "version-cache.json"
            calls: list[tuple[object, float]] = []

            result = check_client_version(
                local_version_code=20004,
                cache_path=cache_path,
                now=1000.0,
                opener=self.opener_for_payload(self.metadata(), calls),
            )

            self.assertFalse(result.should_block)
            self.assertEqual(len(calls), 1)
            request, timeout = calls[0]
            self.assertEqual(request.full_url, VERSION_CHECK_URL)
            self.assertEqual(timeout, VERSION_CHECK_TIMEOUT_SECONDS)
            cache = json.loads(cache_path.read_text())
            self.assertEqual(cache["fetched_at"], 1000.0)
            self.assertEqual(cache["payload"]["min_supported_version"], 20004)

    def test_outdated_client_blocks_with_remote_message_and_download_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "version-cache.json"
            download_url = "https://example.invalid/releases/latest"
            message = "Please update before continuing."
            calls: list[tuple[object, float]] = []

            result = check_client_version(
                local_version_code=20004,
                cache_path=cache_path,
                now=1000.0,
                opener=self.opener_for_payload(
                    self.metadata(
                        current_version=20005,
                        min_supported_version=20005,
                        download_url=download_url,
                        message=message,
                    ),
                    calls,
                ),
            )

            self.assertTrue(result.should_block)
            self.assertEqual(result.message, message)
            self.assertEqual(result.download_url, download_url)
            self.assertEqual(len(calls), 1)

    def test_invalid_or_unreachable_version_metadata_fails_open(self) -> None:
        cases = (
            ("timeout", TimeoutError("timed out")),
            ("invalid_json", b"{"),
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
                    calls: list[tuple[object, float]] = []
                    if isinstance(case, BaseException):

                        def opener(_request, timeout, exc=case):
                            calls.append((_request, timeout))
                            raise exc

                    elif isinstance(case, bytes):

                        def opener(_request, timeout, body=case):
                            calls.append((_request, timeout))
                            return FakeResponse(body)

                    else:
                        opener = self.opener_for_payload(case, calls)

                    result = check_client_version(
                        local_version_code=20004,
                        cache_path=cache_path,
                        now=1000.0,
                        opener=opener,
                    )

                    self.assertFalse(result.should_block)
                    self.assertEqual(len(calls), 1)
                    self.assertFalse(cache_path.exists())

    def test_blocking_result_uses_defaults_for_missing_text_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "version-cache.json"
            calls: list[tuple[object, float]] = []
            payload = {
                "schema": 1,
                "current_version": 20005,
                "min_supported_version": 20005,
            }

            result = check_client_version(
                local_version_code=20004,
                cache_path=cache_path,
                now=1000.0,
                opener=self.opener_for_payload(payload, calls),
            )

            self.assertTrue(result.should_block)
            self.assertEqual(result.message, DEFAULT_UNSUPPORTED_MESSAGE)
            self.assertEqual(result.download_url, DEFAULT_DOWNLOAD_URL)

    def test_fresh_supported_cache_skips_fetch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "version-cache.json"
            save_cached_payload(self.metadata(min_supported_version=20003), cache_path=cache_path, now=1000.0)
            calls: list[tuple[object, float]] = []

            def opener(_request, timeout):
                calls.append((_request, timeout))
                body = json.dumps(self.metadata(current_version=20005, min_supported_version=20005)).encode("utf-8")
                return FakeResponse(body)

            result = check_client_version(
                local_version_code=20004,
                cache_path=cache_path,
                now=1000.0 + 60,
                opener=opener,
            )

            self.assertFalse(result.should_block)
            self.assertEqual(calls, [])

    def test_stale_cache_fetches_remote_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "version-cache.json"
            save_cached_payload(self.metadata(min_supported_version=20003), cache_path=cache_path, now=1000.0)
            calls: list[tuple[object, float]] = []

            result = check_client_version(
                local_version_code=20004,
                cache_path=cache_path,
                now=1000.0 + VERSION_CHECK_CACHE_SECONDS + 1,
                opener=self.opener_for_payload(
                    self.metadata(current_version=20005, min_supported_version=20005),
                    calls,
                ),
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
            calls: list[tuple[object, float]] = []

            def opener(_request, timeout):
                calls.append((_request, timeout))
                raise TimeoutError("offline")

            result = check_client_version(
                local_version_code=20004,
                cache_path=cache_path,
                now=1000.0 + 60,
                opener=opener,
            )

            self.assertFalse(result.should_block)
            self.assertEqual(len(calls), 1)

    def test_cache_write_failure_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp)
            calls: list[tuple[object, float]] = []

            result = check_client_version(
                local_version_code=20004,
                cache_path=cache_path,
                now=1000.0,
                opener=self.opener_for_payload(self.metadata(), calls),
            )

            self.assertFalse(result.should_block)
            self.assertEqual(len(calls), 1)

    def test_unexpected_internal_exception_fails_open(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "version-cache.json"
            with mock.patch("timecapsulesmb.cli.version_check.load_fresh_cached_payload", side_effect=RuntimeError("boom")):
                result = check_client_version(
                    local_version_code=20004,
                    cache_path=cache_path,
                    now=1000.0,
                    opener=self.opener_for_payload(self.metadata(current_version=20005, min_supported_version=20005), []),
                )

            self.assertFalse(result.should_block)

    def test_oversized_response_fails_open(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "version-cache.json"
            body = b"{" + (b" " * (64 * 1024)) + b"}"

            def opener(_request, timeout):
                return FakeResponse(body)

            result = check_client_version(
                local_version_code=20004,
                cache_path=cache_path,
                now=1000.0,
                opener=opener,
            )

            self.assertFalse(result.should_block)
            self.assertFalse(cache_path.exists())

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
