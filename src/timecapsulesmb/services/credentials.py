from __future__ import annotations

from typing import Mapping

from timecapsulesmb.core.config import AppConfig


def request_password(params: Mapping[str, object]) -> str:
    value = params.get("password")
    if isinstance(value, str) and value:
        return value
    credentials = params.get("credentials")
    if isinstance(credentials, Mapping):
        nested = credentials.get("password")
        if isinstance(nested, str) and nested:
            return nested
    return ""


def overlay_request_credentials(config: AppConfig, params: Mapping[str, object]) -> AppConfig:
    password = request_password(params)
    if not password:
        return config
    values = dict(config.values)
    values["TC_PASSWORD"] = password
    return AppConfig.from_values(
        values,
        path=config.path,
        exists=config.exists,
        file_values=config.file_values,
    )
