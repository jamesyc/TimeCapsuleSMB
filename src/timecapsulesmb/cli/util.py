from __future__ import annotations

import getpass


NETBSD4_REBOOT_GUIDANCE = (
    "Tested NetBSD4 devices need to run `activate` after reboot; "
    "other NetBSD4 generations may auto-start if their firmware runs /mnt/Flash/rc.local after a reboot."
)


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


def resolve_env_connection(values: dict[str, str], *, required_keys: tuple[str, ...] = ()) -> tuple[str, str, str]:
    for key in required_keys:
        require_env_setting(values, key)
    host, password = resolve_ssh_credentials(values)
    ssh_opts = values["TC_SSH_OPTS"]
    return host, password, ssh_opts
