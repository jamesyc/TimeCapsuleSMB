from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path
from xml.etree import ElementTree

from timecapsulesmb.core.paths import resolve_app_paths
from timecapsulesmb.core.release import CLI_VERSION_CODE
from timecapsulesmb.transport.http import HttpError, http_get


VERSION_CHECK_URL = "https://raw.githubusercontent.com/jamesyc/TimeCapsuleSMB/main/version.json"
DEFAULT_DOWNLOAD_URL = "https://github.com/jamesyc/TimeCapsuleSMB/releases/latest"
DEFAULT_UNSUPPORTED_MESSAGE = "This version is no longer supported. Please update before continuing."
VERSION_CHECK_TIMEOUT_SECONDS = 3.0
VERSION_CHECK_CACHE_SECONDS = 3 * 60 * 60
VERSION_CHECK_SCHEMA = 1
MAX_VERSION_RESPONSE_BYTES = 64 * 1024
# The app's Sparkle feed. Its newest item names the newest release, which the
# CLI mentions; version.json only blocks unsupported versions.
APPCAST_URL = "https://github.com/jamesyc/TimeCapsuleSMB/releases/latest/download/appcast.xml"
SPARKLE_NAMESPACE = "http://www.andymatuschak.org/xml-namespaces/sparkle"


@dataclass(frozen=True)
class VersionMetadata:
    current_version: int
    min_supported_version: int
    latest_tag: str | None
    download_url: str
    message: str


@dataclass(frozen=True)
class VersionCheckResult:
    should_block: bool
    message: str = DEFAULT_UNSUPPORTED_MESSAGE
    download_url: str = DEFAULT_DOWNLOAD_URL
    local_version_code: int = CLI_VERSION_CODE
    current_version: int | None = None
    min_supported_version: int | None = None
    latest_tag: str | None = None
    source: str = "unavailable"
    # The newest release the appcast names, when the caller asked for it.
    available_version_code: int | None = None
    available_version: str | None = None


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
    latest_tag = payload.get("latest_tag")
    if not isinstance(latest_tag, str) or not latest_tag.strip():
        latest_tag = None
    return VersionMetadata(
        current_version=current_version,
        min_supported_version=min_supported_version,
        latest_tag=latest_tag.strip() if latest_tag else None,
        download_url=download_url.strip(),
        message=message.strip(),
    )


def fetch_version_payload() -> object | None:
    try:
        raw = http_get(
            VERSION_CHECK_URL,
            timeout=VERSION_CHECK_TIMEOUT_SECONDS,
            max_bytes=MAX_VERSION_RESPONSE_BYTES,
            headers={"Accept": "application/json"},
        )
        return json.loads(raw.decode("utf-8"))
    except (HttpError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _load_cache(cache_path: Path) -> dict[str, object]:
    try:
        cache = json.loads(cache_path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return cache if isinstance(cache, dict) else {}


def _write_cache(cache: dict[str, object], cache_path: Path) -> None:
    """Replace the cache file whole, so a run reading it at the same moment
    sees the old file or the new one, never half of one."""
    temp_path: str | None = None
    try:
        fd, temp_path = tempfile.mkstemp(dir=cache_path.parent, prefix=f"{cache_path.name}.", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(cache, sort_keys=True) + "\n")
        os.replace(temp_path, cache_path)
    except OSError:
        if temp_path is not None:
            try:
                os.unlink(temp_path)
            except OSError:
                pass


def _is_fresh(fetched_at: object, now: float) -> bool:
    if not isinstance(fetched_at, (int, float)) or isinstance(fetched_at, bool):
        return False
    # An entry from the future was written under a wrong clock. Trusting it
    # would skip the minimum-version check until the clock caught up.
    return 0 <= now - fetched_at <= VERSION_CHECK_CACHE_SECONDS


def load_fresh_cached_payload(*, cache_path: Path, now: float) -> object | None:
    cache = _load_cache(cache_path)
    if not _is_fresh(cache.get("fetched_at"), now):
        return None
    return cache.get("payload")


def save_cached_payload(payload: object, *, cache_path: Path, now: float) -> None:
    if not isinstance(payload, dict):
        return
    cache = _load_cache(cache_path)
    cache.update({"fetched_at": now, "payload": payload})
    _write_cache(cache, cache_path)


def fetch_appcast_version() -> tuple[int, str] | None:
    """The version code and version of the appcast's first item, or None."""
    try:
        raw = http_get(APPCAST_URL, timeout=VERSION_CHECK_TIMEOUT_SECONDS, max_bytes=MAX_VERSION_RESPONSE_BYTES)
    except HttpError:
        return None
    # An appcast never declares a DTD. Refusing one keeps entity expansion
    # away from the expat in Python 3.9 on macOS (2.2.8), which predates the
    # limits that stop a small file from expanding to gigabytes.
    if b"<!DOCTYPE" in raw or b"<!ENTITY" in raw:
        return None
    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError:
        return None
    item = root.find("channel/item")
    if item is None:
        return None
    code = (item.findtext(f"{{{SPARKLE_NAMESPACE}}}version") or "").strip()
    version = (item.findtext(f"{{{SPARKLE_NAMESPACE}}}shortVersionString") or "").strip()
    # isdigit alone also accepts digits int() refuses, such as superscripts.
    if not (code.isascii() and code.isdigit()) or not version:
        return None
    return int(code), version


def load_fresh_cached_appcast(*, cache_path: Path, now: float) -> dict[str, object] | None:
    """The cached appcast entry while fresh. A failed fetch is cached too, as
    nulls, so the appcast is asked at most once per cache period."""
    entry = _load_cache(cache_path).get("appcast")
    if not isinstance(entry, dict) or not _is_fresh(entry.get("fetched_at"), now):
        return None
    return entry


def save_cached_appcast(release: tuple[int, str] | None, *, cache_path: Path, now: float) -> None:
    cache = _load_cache(cache_path)
    cache["appcast"] = {
        "fetched_at": now,
        "version_code": release[0] if release else None,
        "version": release[1] if release else None,
    }
    _write_cache(cache, cache_path)


def _cached_release(entry: dict[str, object]) -> tuple[int, str] | None:
    code = entry.get("version_code")
    version = entry.get("version")
    if isinstance(code, int) and not isinstance(code, bool) and isinstance(version, str) and version:
        return code, version
    return None


def _available_release(*, cache_path: Path, now: float, fetch: bool) -> tuple[int, str] | None:
    """The fresh cached release, else, when fetch is allowed, the appcast's
    (cached either way, failure included)."""
    entry = load_fresh_cached_appcast(cache_path=cache_path, now=now)
    if entry is not None:
        return _cached_release(entry)
    if not fetch:
        return None
    release = fetch_appcast_version()
    save_cached_appcast(release, cache_path=cache_path, now=now)
    return release


def default_version_check_cache_path() -> Path:
    return resolve_app_paths().version_check_cache_path


def check_client_version(
    *,
    local_version_code: int = CLI_VERSION_CODE,
    cache_path: Path | None = None,
    now: float | None = None,
    appcast: bool = False,
) -> VersionCheckResult:
    """Whether this client must be blocked, from version.json. With appcast,
    also the newest release the app's feed names. The appcast is fetched only
    when version.json did not fail: offline, that fetch already costs its
    timeout on every run, and the appcast must not add a second one. A fresh
    cached appcast answer is used either way."""
    try:
        timestamp = time.time() if now is None else now
        resolved_cache_path = cache_path or default_version_check_cache_path()
        result = _check_client_version(
            local_version_code=local_version_code,
            cache_path=resolved_cache_path,
            now=timestamp,
        )
        if not appcast or result.should_block:
            return result
        release = _available_release(
            cache_path=resolved_cache_path,
            now=timestamp,
            fetch=result.source != "unavailable",
        )
        if release is None:
            return result
        return replace(result, available_version_code=release[0], available_version=release[1])
    except Exception:
        return VersionCheckResult(should_block=False, local_version_code=local_version_code)


def _check_client_version(
    *,
    local_version_code: int,
    cache_path: Path,
    now: float,
) -> VersionCheckResult:
    cached_payload = load_fresh_cached_payload(cache_path=cache_path, now=now)
    cached_metadata = parse_version_metadata(cached_payload)
    if cached_metadata is not None and local_version_code >= cached_metadata.min_supported_version:
        return VersionCheckResult(
            should_block=False,
            local_version_code=local_version_code,
            current_version=cached_metadata.current_version,
            min_supported_version=cached_metadata.min_supported_version,
            latest_tag=cached_metadata.latest_tag,
            source="cache",
        )

    fetched_payload = fetch_version_payload()
    fetched_metadata = parse_version_metadata(fetched_payload)
    if fetched_metadata is None:
        return VersionCheckResult(should_block=False, local_version_code=local_version_code)

    save_cached_payload(fetched_payload, cache_path=cache_path, now=now)
    if local_version_code < fetched_metadata.min_supported_version:
        return VersionCheckResult(
            should_block=True,
            message=fetched_metadata.message,
            download_url=fetched_metadata.download_url,
            local_version_code=local_version_code,
            current_version=fetched_metadata.current_version,
            min_supported_version=fetched_metadata.min_supported_version,
            latest_tag=fetched_metadata.latest_tag,
            source="network",
        )
    return VersionCheckResult(
        should_block=False,
        local_version_code=local_version_code,
        current_version=fetched_metadata.current_version,
        min_supported_version=fetched_metadata.min_supported_version,
        latest_tag=fetched_metadata.latest_tag,
        source="network",
    )


def render_version_block_message(result: VersionCheckResult) -> str:
    return "\n".join(
        [
            f"Checking current version from: {VERSION_CHECK_URL}",
            result.message,
            f"Client version is out of date, download the latest version from: {result.download_url}",
        ]
    )
