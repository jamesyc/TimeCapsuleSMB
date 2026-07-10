from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path


SENSITIVE_KEY_PARTS = ("credentials", "password", "secret", "token")
REDACTED = "<redacted>"

TELEMETRY_DROP_KEYS = frozenset({
    "host",
    "ssh_host",
    "smb_host",
    "mountpoint",
    "fsck_mountpoint",
    "root",
    "path",
    "backup_dir",
})

_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_IPV6_RE = re.compile(r"\b(?:[0-9a-fA-F]{1,4}:){2,}[0-9a-fA-F]{1,4}\b")
_ABS_PATH_RE = re.compile(r"(?:/Volumes|/mnt|/Users|/home|/private)(?:/[^\s\"']*)?")


def scrub_network_and_paths(text: str) -> str:
    scrubbed = _ABS_PATH_RE.sub(REDACTED, text)
    scrubbed = _IPV6_RE.sub(REDACTED, scrubbed)
    scrubbed = _IPV4_RE.sub(REDACTED, scrubbed)
    return scrubbed


def scrub_telemetry_value(value: object) -> object:
    if isinstance(value, Mapping):
        scrubbed: dict[str, object] = {}
        for key, item in value.items():
            key_text = str(key)
            if key_text.lower() in TELEMETRY_DROP_KEYS:
                continue
            scrubbed[key_text] = scrub_telemetry_value(item)
        return scrubbed
    if isinstance(value, (list, tuple, set)):
        return [scrub_telemetry_value(item) for item in value]
    if isinstance(value, Path):
        return scrub_network_and_paths(str(value))
    if isinstance(value, str):
        return scrub_network_and_paths(value)
    return value


def scrub_telemetry_mapping(mapping: Mapping[str, object]) -> dict[str, object]:
    scrubbed = scrub_telemetry_value(dict(mapping))
    if isinstance(scrubbed, dict):
        return scrubbed
    return {}


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
