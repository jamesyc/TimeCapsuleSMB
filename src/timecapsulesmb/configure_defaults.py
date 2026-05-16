from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

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


def saved_value_choice(existing: dict[str, str], key: str, label: str) -> Optional[ConfigureValueChoice]:
    value = valid_existing_config_value(existing, key, label)
    if not value:
        return None
    return ConfigureValueChoice(value=value, source="saved")


def saved_syap_value_for_candidates(
    saved_syap_choice: ConfigureValueChoice | None,
    candidate_syaps: tuple[str, ...],
) -> str | None:
    if saved_syap_choice is None:
        return None
    if candidate_syaps and saved_syap_choice.value not in candidate_syaps:
        return None
    return saved_syap_choice.value
