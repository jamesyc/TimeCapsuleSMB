from __future__ import annotations

from dataclasses import dataclass

from timecapsulesmb.core.config import (
    CONFIG_VALIDATORS,
)


@dataclass(frozen=True)
class ConfigureValueChoice:
    value: str
    source: str


def validated_value_or_empty(key: str, value: str, label: str) -> str:
    validator = CONFIG_VALIDATORS.get(key)
    if not value or validator is None:
        return value
    if validator(value, label):
        return ""
    return value


def valid_existing_config_value(existing: dict[str, str], key: str, label: str) -> str:
    return validated_value_or_empty(key, existing.get(key, ""), label)
