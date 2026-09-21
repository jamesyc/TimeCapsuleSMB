from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path


SENSITIVE_KEY_PARTS = ("credentials", "password", "secret", "token")
REDACTED = "<redacted>"


def redact_sensitive_fields(value: object, *, sensitive_values: Iterable[str] = ()) -> object:
    known_values = tuple(sorted({item for item in sensitive_values if item}, key=len, reverse=True))
    return _redact_sensitive_fields(value, known_values)


def _redact_sensitive_fields(value: object, sensitive_values: tuple[str, ...]) -> object:
    if isinstance(value, Mapping):
        redacted: dict[str, object] = {}
        for key, item in value.items():
            key_text = str(key)
            normalized_key = key_text.lower()
            if any(part in normalized_key for part in SENSITIVE_KEY_PARTS):
                redacted[key_text] = REDACTED
            else:
                redacted[key_text] = _redact_sensitive_fields(item, sensitive_values)
        return redacted
    if isinstance(value, (list, tuple, set)):
        return [_redact_sensitive_fields(item, sensitive_values) for item in value]
    if isinstance(value, Path):
        value = str(value)
    if isinstance(value, str):
        for sensitive_value in sensitive_values:
            value = value.replace(sensitive_value, REDACTED)
    return value
