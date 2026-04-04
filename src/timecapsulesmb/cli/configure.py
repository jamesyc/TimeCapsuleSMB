from __future__ import annotations

import getpass

from timecapsulesmb.core.config import CONFIG_FIELDS, ENV_PATH, parse_env_values, write_env_file


def prompt(label: str, default: str, secret: bool) -> str:
    suffix = f" [{default}]" if default and not secret else ""
    text = f"{label}{suffix}: "
    value = getpass.getpass(text) if secret else input(text)
    if value == "":
        return default
    return value


def main(argv: list[str] | None = None) -> int:
    existing = parse_env_values(ENV_PATH, defaults={})
    values: dict[str, str] = {}

    print(f"Writing {ENV_PATH}")
    print("\033[31mPress Enter\033[0m to accept the current/default value.\n")

    for key, label, default, secret in CONFIG_FIELDS:
        current = existing.get(key, default)
        values[key] = prompt(label, current, secret)

    write_env_file(ENV_PATH, values)
    print(f"\nWrote {ENV_PATH}")
    print("Next steps:")
    print("  1. Review .env")
    print("  2. If you are doing build work, configure build/.env separately from build/.env.example")
    return 0
