from __future__ import annotations

import getpass


def require_env_setting(values: dict[str, str], key: str) -> str:
    value = values.get(key, "")
    if not value:
        raise SystemExit(f"Missing required setting in .env: {key}")
    return value


def resolve_ssh_credentials(values: dict[str, str], *, password_prompt: str = "Time Capsule root password: ") -> tuple[str, str]:
    host = require_env_setting(values, "TC_HOST")
    password = values.get("TC_PASSWORD", "")
    if not password:
        password = getpass.getpass(password_prompt)
    return host, password
