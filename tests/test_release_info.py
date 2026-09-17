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

from timecapsulesmb.services.release_info import (
    RELEASE_API_URL,
    RELEASE_ASSET_NAME,
    RELEASE_PAGE_URL,
    ReleaseAsset,
    fetch_release_payload,
    load_release_info,
    parse_release_payload,
    release_api_url,
    release_cache_seconds,
    release_info_to_jsonable,
    release_timeout_seconds,
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


def release_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "tag_name": "v3.0.0",
        "name": "v3.0.0-1",
        "published_at": "2026-09-15T23:10:17Z",
        "body": "## What's Changed\n- Native metadata",
        "html_url": "https://github.com/jamesyc/TimeCapsuleSMB/releases/tag/v3.0.0",
        "prerelease": False,
        "assets": [
            {
                "name": "TimeCapsuleSMB.app.zip",
                "size": 97211953,
                "browser_download_url": (
                    "https://github.com/jamesyc/TimeCapsuleSMB/releases/download/v3.0.0/TimeCapsuleSMB.app.zip"
                ),
                "digest": "sha256:" + "ab" * 32,
            },
            {"name": "other.txt", "size": 1, "browser_download_url": "https://example.invalid/o"},
        ],
    }
    payload.update(overrides)
    return payload


class ParseReleasePayloadTests(unittest.TestCase):
    def test_parses_release_and_app_asset(self) -> None:
        info = parse_release_payload(release_payload())
        assert info is not None
        self.assertEqual(info.tag, "v3.0.0")
        self.assertEqual(info.name, "v3.0.0-1")
        self.assertEqual(info.published_at, "2026-09-15T23:10:17Z")
        self.assertEqual(info.notes, "## What's Changed\n- Native metadata")
        self.assertFalse(info.prerelease)
        self.assertEqual(
            info.app_asset,
            ReleaseAsset(
                name=RELEASE_ASSET_NAME,
                size=97211953,
                download_url=(
                    "https://github.com/jamesyc/TimeCapsuleSMB/releases/download/v3.0.0/TimeCapsuleSMB.app.zip"
                ),
                sha256="ab" * 32,
            ),
        )

    def test_missing_app_asset_yields_none_asset(self) -> None:
        info = parse_release_payload(release_payload(assets=[]))
        assert info is not None
        self.assertIsNone(info.app_asset)

    def test_bad_digest_is_dropped_but_asset_kept(self) -> None:
        payload = release_payload()
        payload["assets"][0]["digest"] = "md5:abc"  # type: ignore[index]
        info = parse_release_payload(payload)
        assert info is not None and info.app_asset is not None
        self.assertIsNone(info.app_asset.sha256)

    def test_uppercase_digest_is_normalised(self) -> None:
        payload = release_payload()
        payload["assets"][0]["digest"] = "sha256:" + "AB" * 32  # type: ignore[index]
        info = parse_release_payload(payload)
        assert info is not None and info.app_asset is not None
        self.assertEqual(info.app_asset.sha256, "ab" * 32)

    def test_negative_or_bool_size_is_dropped(self) -> None:
        payload = release_payload()
        payload["assets"][0]["size"] = True  # type: ignore[index]
        info = parse_release_payload(payload)
        assert info is not None and info.app_asset is not None
        self.assertIsNone(info.app_asset.size)

    def test_missing_tag_or_wrong_shape_returns_none(self) -> None:
        self.assertIsNone(parse_release_payload(release_payload(tag_name="")))
        self.assertIsNone(parse_release_payload(release_payload(tag_name=3)))
        self.assertIsNone(parse_release_payload([]))
        self.assertIsNone(parse_release_payload(None))

    def test_optional_fields_default(self) -> None:
        payload = release_payload()
        for key in ("name", "published_at", "body", "html_url", "prerelease"):
            payload.pop(key)
        info = parse_release_payload(payload)
        assert info is not None
        self.assertEqual(info.name, "v3.0.0")
        self.assertIsNone(info.published_at)
        self.assertEqual(info.notes, "")
        self.assertEqual(info.html_url, RELEASE_PAGE_URL)
        self.assertFalse(info.prerelease)

    def test_jsonable_shape(self) -> None:
        info = parse_release_payload(release_payload())
        assert info is not None
        data = release_info_to_jsonable(info)
        self.assertEqual(
            sorted(data),
            ["asset", "html_url", "name", "notes", "prerelease", "published_at", "tag"],
        )
        asset = data["asset"]
        assert isinstance(asset, dict)
        self.assertEqual(sorted(asset), ["download_url", "name", "sha256", "size"])

    def test_jsonable_without_asset(self) -> None:
        info = parse_release_payload(release_payload(assets=[]))
        assert info is not None
        self.assertIsNone(release_info_to_jsonable(info)["asset"])


class FetchReleasePayloadTests(unittest.TestCase):
    def test_sends_github_headers_and_parses_json(self) -> None:
        opener = mock.Mock(return_value=FakeResponse(json.dumps(release_payload()).encode()))
        payload = fetch_release_payload(url="https://example.invalid/latest", timeout=2.0, opener=opener)
        self.assertIsInstance(payload, dict)
        request = opener.call_args.args[0]
        self.assertEqual(request.full_url, "https://example.invalid/latest")
        self.assertEqual(request.get_header("Accept"), "application/vnd.github+json")
        self.assertEqual(request.get_header("X-github-api-version"), "2022-11-28")
        self.assertTrue(request.get_header("User-agent").startswith("TimeCapsuleSMB/"))
        self.assertEqual(opener.call_args.kwargs["timeout"], 2.0)

    def test_failures_return_none(self) -> None:
        self.assertIsNone(
            fetch_release_payload(url="https://example.invalid/latest", timeout=1, opener=mock.Mock(side_effect=OSError("x")))
        )
        self.assertIsNone(
            fetch_release_payload(url="https://example.invalid/latest", timeout=1, opener=mock.Mock(return_value=FakeResponse(b"{")))
        )
        too_big = FakeResponse(b"x" * (512 * 1024 + 1))
        self.assertIsNone(fetch_release_payload(url="https://example.invalid/latest", timeout=1, opener=mock.Mock(return_value=too_big)))


class LoadReleaseInfoTests(unittest.TestCase):
    def test_uses_fresh_cache_without_fetching(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "cache.json"
            cache.write_text(json.dumps({
                "fetched_at": 1000.0,
                "url": "https://example.invalid/latest",
                "payload": release_payload(),
            }))
            opener = mock.Mock(side_effect=AssertionError("must not fetch"))
            info = load_release_info(url="https://example.invalid/latest", cache_path=cache, now=1000.0 + 60, opener=opener)
            assert info is not None
            self.assertEqual(info.tag, "v3.0.0")

    def test_cache_from_another_url_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "cache.json"
            cache.write_text(json.dumps({
                "fetched_at": 1000.0,
                "url": "https://api.github.com/repos/jamesyc/TimeCapsuleSMB/releases/latest",
                "payload": release_payload(tag_name="v-upstream"),
            }))
            opener = mock.Mock(
                return_value=FakeResponse(json.dumps(release_payload(tag_name="v-local")).encode())
            )
            info = load_release_info(url="http://127.0.0.1:8000/latest.json", cache_path=cache, now=1000.0 + 60, opener=opener)
            assert info is not None
            self.assertEqual(info.tag, "v-local")
            self.assertEqual(json.loads(cache.read_text())["url"], "http://127.0.0.1:8000/latest.json")

    def test_stale_cache_fetches_and_rewrites(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "cache.json"
            cache.write_text(json.dumps({"fetched_at": 0.0, "payload": release_payload(tag_name="v1")}))
            opener = mock.Mock(
                return_value=FakeResponse(json.dumps(release_payload(tag_name="v2")).encode())
            )
            info = load_release_info(url="https://example.invalid/latest", cache_path=cache, now=10**6, opener=opener)
            assert info is not None
            self.assertEqual(info.tag, "v2")
            self.assertEqual(json.loads(cache.read_text())["payload"]["tag_name"], "v2")

    def test_fetch_failure_returns_none_and_writes_no_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "c.json"
            info = load_release_info(
                url="https://example.invalid/latest", cache_path=cache, now=1.0, opener=mock.Mock(side_effect=OSError())
            )
            self.assertIsNone(info)
            self.assertFalse(cache.exists())

    def test_unparseable_fetched_payload_is_not_cached(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "c.json"
            opener = mock.Mock(return_value=FakeResponse(b'{"tag_name": ""}'))
            self.assertIsNone(load_release_info(url="https://example.invalid/latest", cache_path=cache, now=1.0, opener=opener))
            self.assertFalse(cache.exists())


class KnobTests(unittest.TestCase):
    def test_defaults(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(release_api_url(), RELEASE_API_URL)
            self.assertEqual(release_timeout_seconds(), 5.0)
            self.assertEqual(release_cache_seconds(), 3 * 60 * 60)

    def test_env_overrides_and_clamps(self) -> None:
        env = {
            "TCAPSULE_RELEASE_API_URL": "https://example.invalid/r",
            "TCAPSULE_RELEASE_TIMEOUT_SECONDS": "999",
            "TCAPSULE_RELEASE_CACHE_SECONDS": "-5",
        }
        with mock.patch.dict("os.environ", env, clear=True):
            self.assertEqual(release_api_url(), "https://example.invalid/r")
            self.assertEqual(release_timeout_seconds(), 60.0)
            self.assertEqual(release_cache_seconds(), 0)
        with mock.patch.dict("os.environ", {"TCAPSULE_RELEASE_TIMEOUT_SECONDS": "junk"}, clear=True):
            self.assertEqual(release_timeout_seconds(), 5.0)
        with mock.patch.dict("os.environ", {"TCAPSULE_RELEASE_CACHE_SECONDS": "99999999"}, clear=True):
            self.assertEqual(release_cache_seconds(), 7 * 24 * 60 * 60)


if __name__ == "__main__":
    unittest.main()
