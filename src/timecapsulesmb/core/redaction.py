from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path


SENSITIVE_KEY_PARTS = ("credentials", "password", "secret", "token")
REDACTED = "<redacted>"


def redact_sensitive_fields(value: object) -> object:
    if isinstance(value, Mapping):
        redacted: dict[str, object] = {}
        for key, item in value.items():
            key_text = str(key)
            normalized_key = key_text.lower()
            if any(part in normalized_key for part in SENSITIVE_KEY_PARTS):
                redacted[key_text] = REDACTED
            else:
                redacted[key_text] = redact_sensitive_fields(item)
        return redacted
    if isinstance(value, (list, tuple, set)):
        return [redact_sensitive_fields(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value
