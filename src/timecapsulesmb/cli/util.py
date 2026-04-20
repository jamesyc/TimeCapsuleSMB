from __future__ import annotations

import getpass

from timecapsulesmb.core.config import AppConfig


NETBSD4_REBOOT_GUIDANCE = (
    "Tested NetBSD4 devices cannot auto-run Samba after a reboot; "
    "other NetBSD4 generations may auto-start Samba if their firmware runs /mnt/Flash/rc.local after a reboot."
)

NETBSD4_REBOOT_FOLLOWUP = "Run `activate` after a reboot if the device did not auto-start Samba."
CLI_VERSION = "2.0.0-beta7"
RELEASE_TAG = "v2.0.0-beta7"
SAMBA_VERSION = "4.8.12"

ANSI_RED = "\033[31m"
ANSI_RESET = "\033[0m"


def color_red(text: str) -> str:
    return f"{ANSI_RED}{text}{ANSI_RESET}"

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
    ssh_opts = config.get("TC_SSH_OPTS")
    return host, password, ssh_opts
