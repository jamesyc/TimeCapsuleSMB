from __future__ import annotations

import getpass

from timecapsulesmb.core.config import AppConfig


NETBSD4_REBOOT_GUIDANCE = (
    "Tested NetBSD4 devices need to run `activate` after reboot; "
    "other NetBSD4 generations may auto-start if their firmware runs /mnt/Flash/rc.local after a reboot."
)

def resolve_ssh_credentials(values: dict[str, str], *, password_prompt: str = "Time Capsule root password: ") -> tuple[str, str]:
    config = AppConfig(values)
    host = config.require("TC_HOST")
    password = config.get("TC_PASSWORD")
    if not password:
        password = getpass.getpass(password_prompt)
    return host, password


def resolve_env_connection(values: dict[str, str], *, required_keys: tuple[str, ...] = ()) -> tuple[str, str, str]:
    config = AppConfig(values)
    for key in required_keys:
        config.require(key)
    host, password = resolve_ssh_credentials(values)
    ssh_opts = values["TC_SSH_OPTS"]
    return host, password, ssh_opts
