from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
import plistlib
import re
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import urlopen

from timecapsulesmb.core.paths import default_user_data_dir
from timecapsulesmb.flash import FlashAnalysisError, sha256_hex


APPLE_FIRMWARE_CATALOG_URL = "https://apsu.apple.com/version.xml"
FIRMWARE_KEY_ISSUE_URL = "https://github.com/jamesyc/TimeCapsuleSMB/issues"
UNSUPPORTED_FIRMWARE_KEY_MESSAGE = (
    "We do not have firmware encryption keys for this AirPort firmware product yet. "
    f"Please file an issue at {FIRMWARE_KEY_ISSUE_URL} so the key can be added."
)


@dataclass(frozen=True)
class FirmwareTemplateCandidate:
    data: bytes
    source: str
    path: Path | None
    product_id: str | None
    version: str | None
    expected_size: int | None = None
    from_cache: bool = False


def _safe_path_part(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip())
    return safe.strip("-") or "device"


def default_firmware_template_cache_root() -> Path:
    return default_user_data_dir() / "firmware-templates"


def normalize_syap(value: str | int | None) -> str:
    if value is None:
        raise FlashAnalysisError("cannot select firmware template because syAP is missing")
    text = str(value).strip()
    if not text:
        raise FlashAnalysisError("cannot select firmware template because syAP is empty")
    try:
        return str(int(text, 0))
    except ValueError as exc:
        raise FlashAnalysisError(f"cannot select firmware template because syAP is invalid: {text!r}") from exc


def download_url(url: str, *, timeout: int = 60) -> bytes:
    with urlopen(url, timeout=timeout) as response:
        return response.read()


def load_apple_firmware_catalog(*, cache_dir: Path) -> list[dict[str, object]]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    catalog_path = cache_dir / "version.xml"
    try:
        catalog_data = download_url(APPLE_FIRMWARE_CATALOG_URL)
        catalog_path.write_bytes(catalog_data)
    except Exception as exc:
        if not catalog_path.exists():
            raise FlashAnalysisError(f"failed to download Apple firmware catalog: {exc}") from exc
        catalog_data = catalog_path.read_bytes()

    try:
        catalog = plistlib.loads(catalog_data)
    except Exception as exc:
        raise FlashAnalysisError("failed to parse Apple firmware catalog") from exc
    updates = catalog.get("firmwareUpdates") if isinstance(catalog, dict) else None
    if not isinstance(updates, list):
        raise FlashAnalysisError("Apple firmware catalog did not contain firmwareUpdates")
    return [entry for entry in updates if isinstance(entry, dict)]


def firmware_template_cache_path(*, cache_dir: Path, product_id: str, version: str, url: str) -> Path:
    suffix = sha256_hex(url.encode("utf-8"))[:12]
    parsed_name = Path(urlparse(url).path).name or "firmware.basebinary"
    if not parsed_name.endswith(".basebinary"):
        parsed_name = f"{parsed_name}.basebinary"
    filename = f"{_safe_path_part(version)}-{suffix}-{_safe_path_part(parsed_name)}"
    return cache_dir / _safe_path_part(product_id) / filename


def read_cached_or_download_template(entry: dict[str, object], *, cache_dir: Path) -> FirmwareTemplateCandidate:
    product_id = str(entry.get("productID") or "")
    version = str(entry.get("version") or "")
    url = str(entry.get("location") or "")
    if not product_id or not version or not url:
        raise FlashAnalysisError("Apple firmware catalog entry is missing productID, version, or location")
    expected_size_raw = entry.get("sizeInBytes")
    expected_size = expected_size_raw if isinstance(expected_size_raw, int) else None
    path = firmware_template_cache_path(cache_dir=cache_dir, product_id=product_id, version=version, url=url)
    if path.exists():
        data = path.read_bytes()
        if expected_size is None or len(data) == expected_size:
            return FirmwareTemplateCandidate(
                data=data,
                source=url,
                path=path,
                product_id=product_id,
                version=version,
                expected_size=expected_size,
                from_cache=True,
            )

    return download_firmware_template_to_cache(
        url=url,
        path=path,
        product_id=product_id,
        version=version,
        expected_size=expected_size,
    )


def download_firmware_template_to_cache(
    *,
    url: str,
    path: Path,
    product_id: str,
    version: str,
    expected_size: int | None,
) -> FirmwareTemplateCandidate:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = download_url(url, timeout=120)
    except Exception as exc:
        raise FlashAnalysisError(f"failed to download Apple firmware template {url}: {exc}") from exc
    if expected_size is not None and len(data) != expected_size:
        raise FlashAnalysisError(
            f"downloaded Apple firmware template size mismatch for {url}: "
            f"got {len(data)}, expected {expected_size}"
        )
    path.write_bytes(data)
    return FirmwareTemplateCandidate(
        data=data,
        source=url,
        path=path,
        product_id=product_id,
        version=version,
        expected_size=expected_size,
        from_cache=False,
    )


def refresh_cached_firmware_template_candidate(candidate: FirmwareTemplateCandidate) -> FirmwareTemplateCandidate | None:
    if not candidate.from_cache or candidate.path is None or candidate.product_id is None or candidate.version is None:
        return None
    scheme = urlparse(candidate.source).scheme.lower()
    if scheme not in {"http", "https"}:
        return None
    return download_firmware_template_to_cache(
        url=candidate.source,
        path=candidate.path,
        product_id=candidate.product_id,
        version=candidate.version,
        expected_size=candidate.expected_size,
    )


def _version_key(version: str) -> tuple[int, ...]:
    parts = re.findall(r"\d+", version)
    return tuple(int(part) for part in parts)


def _sorted_firmware_entries(entries: list[dict[str, object]]) -> list[dict[str, object]]:
    return sorted(
        entries,
        key=lambda entry: (
            bool(entry.get("newest")),
            _version_key(str(entry.get("version") or "")),
            str(entry.get("version") or ""),
        ),
        reverse=True,
    )


def resolve_firmware_template_candidates(
    *,
    syap: str | int | None,
    firmware_template: Path | None,
    firmware_version: str | None = None,
    cache_dir: Path | None = None,
) -> Iterable[FirmwareTemplateCandidate]:
    normalized_syap = normalize_syap(syap)
    if firmware_template is not None:
        path = firmware_template.expanduser().resolve()
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise FlashAnalysisError(f"failed to read firmware template {path}: {exc}") from exc
        yield FirmwareTemplateCandidate(
            data=data,
            source=str(path),
            path=path,
            product_id=None,
            version=firmware_version,
            expected_size=None,
            from_cache=False,
        )
        return

    resolved_cache_dir = (cache_dir or default_firmware_template_cache_root()).expanduser().resolve()
    catalog = load_apple_firmware_catalog(cache_dir=resolved_cache_dir)
    entries = [entry for entry in catalog if str(entry.get("productID") or "") == normalized_syap]
    if firmware_version is not None:
        entries = [entry for entry in entries if str(entry.get("version") or "") == firmware_version]
    if not entries:
        version_detail = "" if firmware_version is None else f" version {firmware_version}"
        raise FlashAnalysisError(f"Apple firmware catalog has no basebinary templates for syAP {normalized_syap}{version_detail}")
    for entry in _sorted_firmware_entries(entries):
        yield read_cached_or_download_template(entry, cache_dir=resolved_cache_dir)


def is_missing_key_error(message: str) -> bool:
    return "no candidate basebinary key validated checksum" in message or "no candidate keys were provided" in message
