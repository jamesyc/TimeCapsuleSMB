from __future__ import annotations

import getpass

from timecapsulesmb.core.config import AppConfig, require_valid_config
from timecapsulesmb.device.probe import remote_interface_exists


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

def resolve_ssh_credentials(
    values: dict[str, str],
    *,
    password_prompt: str = "Time Capsule root password: ",
    allow_empty_password: bool = False,
) -> tuple[str, str]:
    config = AppConfig(values)
    host = config.require("TC_HOST")
    password = config.get("TC_PASSWORD")
    if not password and not allow_empty_password:
        password = getpass.getpass(password_prompt)
    return host, password


def resolve_env_connection(
    values: dict[str, str],
    *,
    required_keys: tuple[str, ...] = (),
    allow_empty_password: bool = False,
) -> tuple[str, str, str]:
    config = AppConfig(values)
    for key in required_keys:
        config.require(key)
    host, password = resolve_ssh_credentials(values, allow_empty_password=allow_empty_password)
    ssh_opts = config.get("TC_SSH_OPTS")
    return host, password, ssh_opts


def resolve_validated_managed_connection(
    values: dict[str, str],
    *,
    command_name: str,
    profile: str,
) -> tuple[str, str, str]:
    AppConfig(values).require(
        "TC_AIRPORT_SYAP",
        messageafter=f"\nPlease run the `configure` command before running `{command_name}`.",
    )
    require_valid_config(values, profile=profile)
    host, password, ssh_opts = resolve_env_connection(values)
    if not remote_interface_exists(host, password, ssh_opts, values["TC_NET_IFACE"]):
        raise SystemExit(
            "TC_NET_IFACE is invalid. Run the `configure` command again.\n"
            "The configured network interface was not found on the device."
        )
    return host, password, ssh_opts
