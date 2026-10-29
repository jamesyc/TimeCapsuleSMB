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
    APPCAST_URL,
    DEFAULT_DOWNLOAD_URL,
    DEFAULT_UNSUPPORTED_MESSAGE,
    MAX_VERSION_RESPONSE_BYTES,
    VERSION_CHECK_CACHE_SECONDS,
    VERSION_CHECK_TIMEOUT_SECONDS,
    VERSION_CHECK_URL,
    VersionCheckResult,
    check_client_version,
    fetch_appcast_version,
    render_version_block_message,
    save_cached_appcast,
    save_cached_payload,
)
from timecapsulesmb.transport.http import HttpError


Calls = list[tuple[str, dict[str, object]]]


def serving(responses: dict[str, object], calls: Calls):
    """Patch the version check's GET: answer each URL from responses
    (JSON-encoded unless bytes) or raise it, recording each URL and its
    arguments. A URL not in responses fails the request."""

    def fake_get(url: str, **kwargs: object) -> bytes:
        calls.append((url, kwargs))
        response = responses[url]
        if isinstance(response, BaseException):
            raise response
        return response if isinstance(response, bytes) else json.dumps(response).encode("utf-8")

    return mock.patch("timecapsulesmb.services.version_check.http_get", side_effect=fake_get)


def urls(calls: Calls) -> list[str]:
    return [url for url, _ in calls]


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

    def test_supported_client_fetches_and_caches_successful_response(self) -> None:
        self.assertEqual(VERSION_CHECK_CACHE_SECONDS, 3 * 60 * 60)
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "version-cache.json"
            calls: Calls = []

            with serving({VERSION_CHECK_URL: self.metadata()}, calls):
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
            calls: Calls = []
            metadata = self.metadata(
                current_version=20005,
                min_supported_version=20005,
                download_url=download_url,
                message=message,
            )

            with serving({VERSION_CHECK_URL: metadata}, calls):
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
                    calls: Calls = []

                    with serving({VERSION_CHECK_URL: case}, calls):
                        result = check_client_version(local_version_code=20004, cache_path=cache_path, now=1000.0)

                    self.assertFalse(result.should_block)
                    self.assertEqual(len(calls), 1)
                    self.assertFalse(cache_path.exists())

    def test_blocking_result_uses_defaults_for_missing_text_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "version-cache.json"
            calls: Calls = []
            payload = {
                "schema": 1,
                "current_version": 20005,
                "min_supported_version": 20005,
            }

            with serving({VERSION_CHECK_URL: payload}, calls):
                result = check_client_version(local_version_code=20004, cache_path=cache_path, now=1000.0)

            self.assertTrue(result.should_block)
            self.assertEqual(result.message, DEFAULT_UNSUPPORTED_MESSAGE)
            self.assertEqual(result.download_url, DEFAULT_DOWNLOAD_URL)

    def test_fresh_supported_cache_skips_fetch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "version-cache.json"
            save_cached_payload(self.metadata(min_supported_version=20003), cache_path=cache_path, now=1000.0)
            calls: Calls = []

            with serving({VERSION_CHECK_URL: self.metadata(current_version=20005, min_supported_version=20005)}, calls):
                result = check_client_version(local_version_code=20004, cache_path=cache_path, now=1000.0 + 60)

            self.assertFalse(result.should_block)
            self.assertEqual(result.source, "cache")
            self.assertEqual(result.current_version, 20004)
            self.assertEqual(calls, [])

    def test_stale_cache_fetches_remote_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "version-cache.json"
            save_cached_payload(self.metadata(min_supported_version=20003), cache_path=cache_path, now=1000.0)
            calls: Calls = []

            with serving({VERSION_CHECK_URL: self.metadata(current_version=20005, min_supported_version=20005)}, calls):
                result = check_client_version(
                    local_version_code=20004,
                    cache_path=cache_path,
                    now=1000.0 + VERSION_CHECK_CACHE_SECONDS + 1,
                )

            self.assertTrue(result.should_block)
            self.assertEqual(len(calls), 1)

    def test_future_dated_cache_is_fetched_again_and_can_block(self) -> None:
        # A cache written while the clock ran ahead must not stand in for
        # version.json: that would hide a raised minimum until the clock caught up.
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "version-cache.json"
            save_cached_payload(self.metadata(min_supported_version=20003), cache_path=cache_path, now=5000.0)
            calls: Calls = []

            with serving({VERSION_CHECK_URL: self.metadata(current_version=20005, min_supported_version=20005)}, calls):
                result = check_client_version(local_version_code=20004, cache_path=cache_path, now=1000.0)

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
            calls: Calls = []

            with serving({VERSION_CHECK_URL: HttpError("offline")}, calls):
                result = check_client_version(local_version_code=20004, cache_path=cache_path, now=1000.0 + 60)

            self.assertFalse(result.should_block)
            self.assertEqual(len(calls), 1)

    def test_cache_write_failure_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp)
            calls: Calls = []

            with serving({VERSION_CHECK_URL: self.metadata()}, calls):
                result = check_client_version(local_version_code=20004, cache_path=cache_path, now=1000.0)

            self.assertFalse(result.should_block)
            self.assertEqual(len(calls), 1)
            # The temp file beside an unwritable cache path is removed again.
            self.assertEqual(list(cache_path.parent.glob(f"{cache_path.name}.*.tmp")), [])

    def test_cache_is_replaced_whole_and_leaves_no_temp_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "version-cache.json"
            save_cached_payload(self.metadata(), cache_path=cache_path, now=1000.0)
            save_cached_appcast((30900, "3.9.0"), cache_path=cache_path, now=1000.0)

            self.assertEqual(sorted(path.name for path in Path(tmp).iterdir()), ["version-cache.json"])
            cache = json.loads(cache_path.read_text())
            self.assertEqual(set(cache), {"fetched_at", "payload", "appcast"})

    def test_failed_cache_replace_keeps_the_old_cache(self) -> None:
        # A run reading the cache while another writes it must see the old
        # file or the new one; a failed write leaves the old one untouched.
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "version-cache.json"
            save_cached_payload(self.metadata(), cache_path=cache_path, now=1000.0)
            before = cache_path.read_text()

            with mock.patch("timecapsulesmb.services.version_check.os.replace", side_effect=OSError("disk full")):
                save_cached_payload(self.metadata(current_version=20009), cache_path=cache_path, now=2000.0)

            self.assertEqual(cache_path.read_text(), before)
            self.assertEqual(sorted(path.name for path in Path(tmp).iterdir()), ["version-cache.json"])

    def test_unexpected_internal_exception_fails_open(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "version-cache.json"
            with mock.patch("timecapsulesmb.services.version_check.load_fresh_cached_payload", side_effect=RuntimeError("boom")):
                with serving({VERSION_CHECK_URL: self.metadata(current_version=20005, min_supported_version=20005)}, []):
                    result = check_client_version(local_version_code=20004, cache_path=cache_path, now=1000.0)

            self.assertFalse(result.should_block)

    def test_render_version_block_message_exposes_version_url_and_download_url(self) -> None:
        result = VersionCheckResult(
            should_block=True,
            message="Update required.",
            download_url=DEFAULT_DOWNLOAD_URL,
        )

        text = render_version_block_message(result)

        self.assertIn(f"Checking current version from: {VERSION_CHECK_URL}", text)
        self.assertIn("Update required.", text)
        self.assertIn(f"Client version is out of date, download the latest version from: {DEFAULT_DOWNLOAD_URL}", text)



def appcast(*items: tuple[str | None, str | None]) -> bytes:
    """An appcast with one item per (sparkle:version, sparkle:shortVersionString);
    None leaves that element out."""
    body = ""
    for code, version in items:
        body += "<item><title>release</title>"
        if code is not None:
            body += f"<sparkle:version>{code}</sparkle:version>"
        if version is not None:
            body += f"<sparkle:shortVersionString>{version}</sparkle:shortVersionString>"
        body += "</item>"
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<rss version="2.0" xmlns:sparkle="http://www.andymatuschak.org/xml-namespaces/sparkle">'
        f"<channel><title>TimeCapsuleSMB</title>{body}</channel></rss>"
    ).encode("utf-8")


SUPPORTED = {
    "schema": 1,
    "current_version": 20004,
    "min_supported_version": 20004,
    "latest_tag": "v2.0.4",
    "download_url": DEFAULT_DOWNLOAD_URL,
    "message": DEFAULT_UNSUPPORTED_MESSAGE,
}


class AppcastTests(unittest.TestCase):
    def check(self, responses: dict[str, object], calls: Calls, cache_path: Path, *,
              now: float = 1000.0, appcast: bool = True) -> VersionCheckResult:
        with serving(responses, calls):
            return check_client_version(local_version_code=20004, cache_path=cache_path, now=now, appcast=appcast)

    def test_appcast_first_item_names_the_newest_release(self) -> None:
        cases = {
            "one_item": (appcast(("30900", "3.9.0")), (30900, "3.9.0")),
            "first_of_two": (appcast(("30900", "3.9.0"), ("30800", "3.8.0")), (30900, "3.9.0")),
            "spaces_around_values": (appcast((" 30900 ", " 3.9.0 ")), (30900, "3.9.0")),
            "malformed_xml": (b"<rss><channel>", None),
            "no_item": (appcast(), None),
            "missing_version_code": (appcast((None, "3.9.0")), None),
            "non_int_version_code": (appcast(("3.9.0", "3.9.0")), None),
            "signed_version_code": (appcast(("+30900", "3.9.0")), None),
            "superscript_version_code": (appcast(("30900\u00b9", "3.9.0")), None),
            "missing_short_version": (appcast(("30900", None)), None),
            "empty_short_version": (appcast(("30900", "")), None),
            "unreachable": (HttpError("timed out"), None),
            "entity_declarations": (
                b'<?xml version="1.0"?><!DOCTYPE rss [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;">]>'
                + appcast(("30900", "3.9.0")).split(b"?>", 1)[1],
                None,
            ),
        }
        for name, (response, expected) in cases.items():
            with self.subTest(name=name):
                calls: Calls = []
                with serving({APPCAST_URL: response}, calls):
                    self.assertEqual(fetch_appcast_version(), expected)
                self.assertEqual(urls(calls), [APPCAST_URL])

    def test_supported_client_gets_the_appcast_release_and_caches_it_beside_the_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "version-cache.json"
            calls: Calls = []

            result = self.check({VERSION_CHECK_URL: SUPPORTED, APPCAST_URL: appcast(("30900", "3.9.0"))}, calls, cache_path)

            self.assertEqual((result.available_version_code, result.available_version), (30900, "3.9.0"))
            self.assertEqual(urls(calls), [VERSION_CHECK_URL, APPCAST_URL])
            cache = json.loads(cache_path.read_text())
            self.assertEqual(cache["payload"], SUPPORTED)
            self.assertEqual(cache["appcast"], {"fetched_at": 1000.0, "version_code": 30900, "version": "3.9.0"})

    def test_offline_version_check_never_asks_the_appcast(self) -> None:
        # Failures are not cached, so offline every run already waits for
        # version.json; the appcast must not add a second wait.
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "version-cache.json"
            calls: Calls = []

            result = self.check({VERSION_CHECK_URL: HttpError("offline")}, calls, cache_path)

            self.assertFalse(result.should_block)
            self.assertIsNone(result.available_version_code)
            self.assertEqual(urls(calls), [VERSION_CHECK_URL])

    def test_offline_version_check_still_uses_a_fresh_cached_appcast_answer(self) -> None:
        # Reading the cache costs no network time, so an offline run still
        # names the newer release it already knows about.
        cases = {
            "fresh": (1000.0 + 60, 30900),
            "stale": (1000.0 + VERSION_CHECK_CACHE_SECONDS + 1, None),
        }
        for name, (now, expected_code) in cases.items():
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory() as tmp:
                    cache_path = Path(tmp) / "version-cache.json"
                    save_cached_appcast((30900, "3.9.0"), cache_path=cache_path, now=1000.0)
                    calls: Calls = []

                    result = self.check({VERSION_CHECK_URL: HttpError("offline")}, calls, cache_path, now=now)

                    self.assertFalse(result.should_block)
                    self.assertEqual(result.available_version_code, expected_code)
                    self.assertEqual(urls(calls), [VERSION_CHECK_URL])

    def test_fresh_payload_without_an_appcast_entry_asks_only_the_appcast(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "version-cache.json"
            save_cached_payload(SUPPORTED, cache_path=cache_path, now=1000.0)
            calls: Calls = []

            result = self.check({APPCAST_URL: appcast(("30900", "3.9.0"))}, calls, cache_path, now=1060.0)

            self.assertEqual(result.source, "cache")
            self.assertEqual(result.available_version_code, 30900)
            self.assertEqual(urls(calls), [APPCAST_URL])
            cache = json.loads(cache_path.read_text())
            self.assertEqual((cache["fetched_at"], cache["payload"]), (1000.0, SUPPORTED))
            self.assertEqual(cache["appcast"]["fetched_at"], 1060.0)

    def test_fresh_appcast_entry_answers_without_a_request(self) -> None:
        cases = {
            "release": ((30900, "3.9.0"), 30900),
            "cached_failure": (None, None),
        }
        for name, (release, expected_code) in cases.items():
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory() as tmp:
                    cache_path = Path(tmp) / "version-cache.json"
                    save_cached_payload(SUPPORTED, cache_path=cache_path, now=1000.0)
                    save_cached_appcast(release, cache_path=cache_path, now=1000.0)
                    calls: Calls = []

                    result = self.check({}, calls, cache_path, now=1000.0 + VERSION_CHECK_CACHE_SECONDS)

                    self.assertEqual(urls(calls), [])
                    self.assertEqual(result.available_version_code, expected_code)

    def test_stale_appcast_entry_is_asked_again(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "version-cache.json"
            later = 1000.0 + VERSION_CHECK_CACHE_SECONDS + 1
            save_cached_payload(SUPPORTED, cache_path=cache_path, now=later)
            save_cached_appcast((30800, "3.8.0"), cache_path=cache_path, now=1000.0)
            calls: Calls = []

            result = self.check({APPCAST_URL: appcast(("30900", "3.9.0"))}, calls, cache_path, now=later)

            self.assertEqual(urls(calls), [APPCAST_URL])
            self.assertEqual(result.available_version_code, 30900)

    def test_failed_appcast_fetch_is_cached_and_not_retried_within_the_period(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "version-cache.json"
            calls: Calls = []

            first = self.check({VERSION_CHECK_URL: SUPPORTED, APPCAST_URL: HttpError("not found")}, calls, cache_path)
            second = self.check({}, calls, cache_path, now=1000.0 + 60)

            self.assertIsNone(first.available_version_code)
            self.assertIsNone(second.available_version_code)
            self.assertEqual(urls(calls), [VERSION_CHECK_URL, APPCAST_URL])
            cache = json.loads(cache_path.read_text())
            self.assertEqual(cache["appcast"], {"fetched_at": 1000.0, "version_code": None, "version": None})

    def test_blocked_client_is_blocked_whatever_the_appcast_says(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "version-cache.json"
            calls: Calls = []
            unsupported = {**SUPPORTED, "current_version": 20005, "min_supported_version": 20005}

            result = self.check({VERSION_CHECK_URL: unsupported, APPCAST_URL: appcast(("30900", "3.9.0"))}, calls, cache_path)

            self.assertTrue(result.should_block)
            self.assertIsNone(result.available_version_code)
            self.assertEqual(urls(calls), [VERSION_CHECK_URL])

    def test_without_appcast_the_appcast_is_never_asked(self) -> None:
        for name, cached in (("network", False), ("cache", True)):
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory() as tmp:
                    cache_path = Path(tmp) / "version-cache.json"
                    if cached:
                        save_cached_payload(SUPPORTED, cache_path=cache_path, now=1000.0)
                    calls: Calls = []

                    result = self.check({VERSION_CHECK_URL: SUPPORTED}, calls, cache_path, now=1060.0, appcast=False)

                    self.assertIsNone(result.available_version_code)
                    self.assertNotIn(APPCAST_URL, urls(calls))

    def test_unexpected_appcast_failure_never_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "version-cache.json"
            calls: Calls = []
            with mock.patch(
                "timecapsulesmb.services.version_check.fetch_appcast_version",
                side_effect=RuntimeError("boom"),
            ):
                result = self.check({VERSION_CHECK_URL: SUPPORTED}, calls, cache_path)

            self.assertFalse(result.should_block)
            self.assertIsNone(result.available_version_code)

    def test_saving_the_payload_keeps_the_appcast_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "version-cache.json"
            save_cached_appcast((30900, "3.9.0"), cache_path=cache_path, now=1000.0)

            save_cached_payload(SUPPORTED, cache_path=cache_path, now=2000.0)

            cache = json.loads(cache_path.read_text())
            self.assertEqual(cache["payload"], SUPPORTED)
            self.assertEqual(cache["appcast"], {"fetched_at": 1000.0, "version_code": 30900, "version": "3.9.0"})

    def test_future_dated_appcast_entry_is_asked_again(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "version-cache.json"
            save_cached_payload(SUPPORTED, cache_path=cache_path, now=1000.0)
            save_cached_appcast((30800, "3.8.0"), cache_path=cache_path, now=1000.0 + 60)
            calls: Calls = []

            result = self.check({APPCAST_URL: appcast(("30900", "3.9.0"))}, calls, cache_path, now=1000.0)

            self.assertEqual(urls(calls), [APPCAST_URL])
            self.assertEqual(result.available_version_code, 30900)

    def test_invalid_cached_appcast_entry_is_asked_again(self) -> None:
        for name, entry in (
            ("not_an_object", "3.9.0"),
            ("no_timestamp", {"version_code": 30900, "version": "3.9.0"}),
            ("boolean_timestamp", {"fetched_at": True, "version_code": 30900, "version": "3.9.0"}),
        ):
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory() as tmp:
                    cache_path = Path(tmp) / "version-cache.json"
                    cache_path.write_text(json.dumps({"fetched_at": 1000.0, "payload": SUPPORTED, "appcast": entry}))
                    calls: Calls = []

                    result = self.check({APPCAST_URL: appcast(("30900", "3.9.0"))}, calls, cache_path, now=1060.0)

                    self.assertEqual(urls(calls), [APPCAST_URL])
                    self.assertEqual(result.available_version_code, 30900)


if __name__ == "__main__":
    unittest.main()
