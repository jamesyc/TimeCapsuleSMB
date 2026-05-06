from __future__ import annotations

import json
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from timecapsulesmb.cli.util import CLI_VERSION, CLI_VERSION_CODE
from timecapsulesmb.core.paths import package_project_root, resolve_app_paths


VERSION_CHECK_URL = "https://raw.githubusercontent.com/jamesyc/TimeCapsuleSMB/main/version.json"
DEFAULT_DOWNLOAD_URL = "https://github.com/jamesyc/TimeCapsuleSMB/releases/latest"
DEFAULT_UNSUPPORTED_MESSAGE = "This version is no longer supported. Please update before continuing."
VERSION_CHECK_TIMEOUT_SECONDS = 3.0
VERSION_CHECK_CACHE_SECONDS = 3 * 60 * 60
VERSION_CHECK_CACHE_PATH = package_project_root() / ".version-check-cache.json"
VERSION_CHECK_SCHEMA = 1
MAX_VERSION_RESPONSE_BYTES = 64 * 1024


@dataclass(frozen=True)
class VersionMetadata:
    min_supported_version: int
    download_url: str
    message: str


@dataclass(frozen=True)
class VersionCheckResult:
    should_block: bool
    checked_url: str = VERSION_CHECK_URL
    message: str = DEFAULT_UNSUPPORTED_MESSAGE
    download_url: str = DEFAULT_DOWNLOAD_URL


UrlOpen = Callable[..., Any]


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def parse_version_metadata(payload: object) -> VersionMetadata | None:
    if not isinstance(payload, dict):
        return None
    if payload.get("schema") != VERSION_CHECK_SCHEMA:
        return None
    current_version = payload.get("current_version")
    if not _is_int(current_version):
        return None
    min_supported_version = payload.get("min_supported_version")
    if not _is_int(min_supported_version):
        return None
    if current_version < min_supported_version:
        return None
    download_url = payload.get("download_url")
    if not isinstance(download_url, str) or not download_url.strip():
        download_url = DEFAULT_DOWNLOAD_URL
    message = payload.get("message")
    if not isinstance(message, str) or not message.strip():
        message = DEFAULT_UNSUPPORTED_MESSAGE
    return VersionMetadata(
        min_supported_version=min_supported_version,
        download_url=download_url.strip(),
        message=message.strip(),
    )


def fetch_version_payload(
    *,
    url: str = VERSION_CHECK_URL,
    timeout: float = VERSION_CHECK_TIMEOUT_SECONDS,
    opener: UrlOpen = urllib.request.urlopen,
) -> object | None:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": f"TimeCapsuleSMB/{CLI_VERSION}",
        },
    )
    try:
        with opener(request, timeout=timeout) as response:
            raw = response.read(MAX_VERSION_RESPONSE_BYTES + 1)
    except Exception:
        return None
    if not isinstance(raw, bytes):
        return None
    if len(raw) > MAX_VERSION_RESPONSE_BYTES:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def load_fresh_cached_payload(
    *,
    cache_path: Path = VERSION_CHECK_CACHE_PATH,
    now: float | None = None,
    max_age_seconds: int = VERSION_CHECK_CACHE_SECONDS,
) -> object | None:
    timestamp = time.time() if now is None else now
    try:
        cache = json.loads(cache_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(cache, dict):
        return None
    fetched_at = cache.get("fetched_at")
    if not isinstance(fetched_at, (int, float)) or isinstance(fetched_at, bool):
        return None
    if timestamp - fetched_at > max_age_seconds:
        return None
    return cache.get("payload")


def save_cached_payload(
    payload: object,
    *,
    cache_path: Path = VERSION_CHECK_CACHE_PATH,
    now: float | None = None,
) -> None:
    if not isinstance(payload, dict):
        return
    timestamp = time.time() if now is None else now
    text = json.dumps({"fetched_at": timestamp, "payload": payload}, sort_keys=True) + "\n"
    try:
        cache_path.write_text(text)
    except OSError:
        return


def default_version_check_cache_path() -> Path:
    return resolve_app_paths().version_check_cache_path


def check_client_version(
    *,
    local_version_code: int = CLI_VERSION_CODE,
    url: str = VERSION_CHECK_URL,
    timeout: float = VERSION_CHECK_TIMEOUT_SECONDS,
    cache_path: Path | None = None,
    now: float | None = None,
    opener: UrlOpen = urllib.request.urlopen,
) -> VersionCheckResult:
    try:
        return _check_client_version(
            local_version_code=local_version_code,
            url=url,
            timeout=timeout,
            cache_path=cache_path or default_version_check_cache_path(),
            now=now,
            opener=opener,
        )
    except Exception:
        return VersionCheckResult(should_block=False, checked_url=url)


def _check_client_version(
    *,
    local_version_code: int,
    url: str,
    timeout: float,
    cache_path: Path,
    now: float | None,
    opener: UrlOpen,
) -> VersionCheckResult:
    timestamp = time.time() if now is None else now
    cached_payload = load_fresh_cached_payload(cache_path=cache_path, now=timestamp)
    cached_metadata = parse_version_metadata(cached_payload)
    if cached_metadata is not None and local_version_code >= cached_metadata.min_supported_version:
        return VersionCheckResult(should_block=False, checked_url=url)

    fetched_payload = fetch_version_payload(url=url, timeout=timeout, opener=opener)
    fetched_metadata = parse_version_metadata(fetched_payload)
    if fetched_metadata is None:
        return VersionCheckResult(should_block=False, checked_url=url)

    save_cached_payload(fetched_payload, cache_path=cache_path, now=timestamp)
    if local_version_code < fetched_metadata.min_supported_version:
        return VersionCheckResult(
            should_block=True,
            checked_url=url,
            message=fetched_metadata.message,
            download_url=fetched_metadata.download_url,
        )
    return VersionCheckResult(should_block=False, checked_url=url)


def render_version_block_message(result: VersionCheckResult) -> str:
    return "\n".join(
        [
            f"Checking current version from: {result.checked_url}",
            result.message,
            f"Client version is out of date, download the latest version from: {result.download_url}",
        ]
    )
