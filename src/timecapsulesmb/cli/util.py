from __future__ import annotations


NETBSD4_REBOOT_GUIDANCE = (
    "NetBSD 4 devices cannot auto-run Samba after a reboot."
)

NETBSD4_REBOOT_FOLLOWUP = "Run `activate` after a reboot if the device did not auto-start Samba."
# Update this version info for each release, including beta releases
CLI_VERSION = "2.1-beta1"
RELEASE_TAG = "v2.1-beta1"
SAMBA_VERSION = "4.8.12"

ANSI_RED = "\033[31m"
ANSI_GREEN = "\033[32m"
ANSI_CYAN = "\033[36m"
ANSI_RESET = "\033[0m"


def color_red(text: str) -> str:
    return f"{ANSI_RED}{text}{ANSI_RESET}"


def color_green(text: str) -> str:
    return f"{ANSI_GREEN}{text}{ANSI_RESET}"


def color_cyan(text: str) -> str:
    return f"{ANSI_CYAN}{text}{ANSI_RESET}"
