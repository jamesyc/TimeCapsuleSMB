from __future__ import annotations


NETBSD4_REBOOT_GUIDANCE = (
    "NetBSD 4 devices cannot auto-run Samba after a reboot."
)

NETBSD4_REBOOT_FOLLOWUP = "Run `activate` after a reboot if the device did not auto-start Samba."
CLI_VERSION = "2.0.0-beta9"
RELEASE_TAG = "v2.0.0-beta9"
SAMBA_VERSION = "4.8.12"

ANSI_RED = "\033[31m"
ANSI_RESET = "\033[0m"


def color_red(text: str) -> str:
    return f"{ANSI_RED}{text}{ANSI_RESET}"
