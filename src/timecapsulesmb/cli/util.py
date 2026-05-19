from __future__ import annotations

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
