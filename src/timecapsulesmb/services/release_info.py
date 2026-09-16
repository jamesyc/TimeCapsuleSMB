"""GitHub Releases metadata for the macOS app updater (release notes and app asset).

`version.json` (see `version_check.py`) stays the authority for whether an update exists or is
required. This module only supplies the human-facing release notes and the downloadable
`TimeCapsuleSMB.app.zip` asset with its published SHA256 digest.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from timecapsulesmb.core.release import CLI_VERSION
from timecapsulesmb.services.version_check import load_fresh_cached_payload, save_cached_payload


RELEASE_API_URL = "https://api.github.com/repos/jamesyc/TimeCapsuleSMB/releases/latest"
RELEASE_PAGE_URL = "https://github.com/jamesyc/TimeCapsuleSMB/releases/latest"
RELEASE_ASSET_NAME = "TimeCapsuleSMB.app.zip"
MAX_RELEASE_RESPONSE_BYTES = 512 * 1024

# Environment knobs, read at call time with defaults and clamps.
RELEASE_API_URL_ENV = "TCAPSULE_RELEASE_API_URL"
RELEASE_TIMEOUT_ENV = "TCAPSULE_RELEASE_TIMEOUT_SECONDS"
RELEASE_CACHE_ENV = "TCAPSULE_RELEASE_CACHE_SECONDS"
DEFAULT_RELEASE_TIMEOUT_SECONDS = 5.0
MIN_RELEASE_TIMEOUT_SECONDS = 1.0
MAX_RELEASE_TIMEOUT_SECONDS = 60.0
DEFAULT_RELEASE_CACHE_SECONDS = 3 * 60 * 60
MAX_RELEASE_CACHE_SECONDS = 7 * 24 * 60 * 60

_SHA256_DIGEST = re.compile(r"^sha256:([0-9a-fA-F]{64})$")

UrlOpen = Callable[..., Any]


@dataclass(frozen=True)
class ReleaseAsset:
    name: str
    size: int | None
    download_url: str
    sha256: str | None


@dataclass(frozen=True)
class ReleaseInfo:
    tag: str
    name: str
    published_at: str | None
    notes: str
    html_url: str
    prerelease: bool
    app_asset: ReleaseAsset | None


def release_api_url() -> str:
    return os.getenv(RELEASE_API_URL_ENV, "").strip() or RELEASE_API_URL


def _env_number(name: str, default: float, low: float, high: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return min(max(value, low), high)


def release_timeout_seconds() -> float:
    return _env_number(
        RELEASE_TIMEOUT_ENV,
        DEFAULT_RELEASE_TIMEOUT_SECONDS,
        MIN_RELEASE_TIMEOUT_SECONDS,
        MAX_RELEASE_TIMEOUT_SECONDS,
    )


def release_cache_seconds() -> int:
    return int(_env_number(RELEASE_CACHE_ENV, DEFAULT_RELEASE_CACHE_SECONDS, 0, MAX_RELEASE_CACHE_SECONDS))


def _non_empty_str(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _parse_asset(raw: object) -> ReleaseAsset | None:
    if not isinstance(raw, dict):
        return None
    name = _non_empty_str(raw.get("name"))
    download_url = _non_empty_str(raw.get("browser_download_url"))
    if name != RELEASE_ASSET_NAME or download_url is None:
        return None
    size: int | None = raw.get("size")  # type: ignore[assignment]
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        size = None
    sha256: str | None = None
    digest = raw.get("digest")
    if isinstance(digest, str):
        match = _SHA256_DIGEST.match(digest.strip())
        if match:
            sha256 = match.group(1).lower()
    return ReleaseAsset(name=name, size=size, download_url=download_url, sha256=sha256)


def parse_release_payload(payload: object) -> ReleaseInfo | None:
    if not isinstance(payload, dict):
        return None
    tag = _non_empty_str(payload.get("tag_name"))
    if tag is None:
        return None
    name = _non_empty_str(payload.get("name")) or tag
    published_at = _non_empty_str(payload.get("published_at"))
    body = payload.get("body")
    notes = body.strip() if isinstance(body, str) else ""
    html_url = _non_empty_str(payload.get("html_url")) or RELEASE_PAGE_URL
    prerelease = payload.get("prerelease") is True
    app_asset: ReleaseAsset | None = None
    assets = payload.get("assets")
    if isinstance(assets, list):
        for raw_asset in assets:
            app_asset = _parse_asset(raw_asset)
            if app_asset is not None:
                break
    return ReleaseInfo(
        tag=tag,
        name=name,
        published_at=published_at,
        notes=notes,
        html_url=html_url,
        prerelease=prerelease,
        app_asset=app_asset,
    )


def release_info_to_jsonable(info: ReleaseInfo) -> dict[str, object]:
    asset: dict[str, object] | None = None
    if info.app_asset is not None:
        asset = {
            "name": info.app_asset.name,
            "size": info.app_asset.size,
            "download_url": info.app_asset.download_url,
            "sha256": info.app_asset.sha256,
        }
    return {
        "tag": info.tag,
        "name": info.name,
        "published_at": info.published_at,
        "notes": info.notes,
        "html_url": info.html_url,
        "prerelease": info.prerelease,
        "asset": asset,
    }


def fetch_release_payload(
    *,
    url: str,
    timeout: float,
    opener: UrlOpen = urllib.request.urlopen,
) -> object | None:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": f"TimeCapsuleSMB/{CLI_VERSION}",
        },
    )
    try:
        with opener(request, timeout=timeout) as response:
            raw = response.read(MAX_RELEASE_RESPONSE_BYTES + 1)
    except Exception:
        return None
    if not isinstance(raw, bytes) or len(raw) > MAX_RELEASE_RESPONSE_BYTES:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def load_release_info(
    *,
    url: str,
    cache_path: Path,
    now: float | None = None,
    opener: UrlOpen = urllib.request.urlopen,
) -> ReleaseInfo | None:
    """Return the latest release from cache or GitHub. Never raises; None when unavailable."""
    try:
        timestamp = time.time() if now is None else now
        cached = load_fresh_cached_payload(
            cache_path=cache_path,
            now=timestamp,
            max_age_seconds=release_cache_seconds(),
            url=url,
        )
        info = parse_release_payload(cached)
        if info is not None:
            return info
        fetched = fetch_release_payload(url=url, timeout=release_timeout_seconds(), opener=opener)
        info = parse_release_payload(fetched)
        if info is None:
            return None
        save_cached_payload(fetched, cache_path=cache_path, now=timestamp, url=url)
        return info
    except Exception:
        return None
