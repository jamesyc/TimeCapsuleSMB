from __future__ import annotations


NETBSD4_REBOOT_GUIDANCE = (
    "Tested NetBSD4 devices cannot auto-run Samba after a reboot; "
    "other NetBSD4 generations may auto-start Samba if their firmware runs /mnt/Flash/rc.local after a reboot."
)

NETBSD4_REBOOT_FOLLOWUP = "Run `activate` after a reboot if the device did not auto-start Samba."
CLI_VERSION = "2.0.0-beta8"
RELEASE_TAG = "v2.0.0-beta8"
SAMBA_VERSION = "4.8.12"

ANSI_RED = "\033[31m"
ANSI_RESET = "\033[0m"


def color_red(text: str) -> str:
    return f"{ANSI_RED}{text}{ANSI_RESET}"
