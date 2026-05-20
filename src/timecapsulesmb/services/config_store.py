from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Protocol

from timecapsulesmb.core.config import AppConfig, load_app_config, write_env_file


class ConfigStore(Protocol):
    def load(self, path: Path, *, defaults: dict[str, str] | None = None) -> AppConfig:
        ...

    def save(self, path: Path, values: Mapping[str, str]) -> None:
        ...


@dataclass(frozen=True)
class EnvFileConfigStore:
    omit_keys: frozenset[str] = frozenset()

    def load(self, path: Path, *, defaults: dict[str, str] | None = None) -> AppConfig:
        return load_app_config(path, defaults=defaults)

    def save(self, path: Path, values: Mapping[str, str]) -> None:
        filtered = {
            key: value
            for key, value in values.items()
            if key not in self.omit_keys
        }
        write_env_file(path, filtered)
