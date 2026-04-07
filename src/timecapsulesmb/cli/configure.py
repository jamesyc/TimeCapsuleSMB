from __future__ import annotations

import getpass
from typing import Optional

from timecapsulesmb.core.config import CONFIG_FIELDS, DEFAULTS, ENV_PATH, parse_env_values, write_env_file
from timecapsulesmb.transport.ssh import run_ssh


def prompt(label: str, default: str, secret: bool) -> str:
    suffix = f" [{default}]" if default and not secret else ""
    text = f"{label}{suffix}: "
    value = getpass.getpass(text) if secret else input(text)
    if value == "":
        return default
    return value


def confirm(prompt_text: str) -> bool:
    answer = input(f"{prompt_text} [Y/n]: ").strip().lower()
    return answer in {"", "y", "yes"}


def validate_ssh_target(host: str, password: str, ssh_opts: str) -> bool:
    try:
        proc = run_ssh(host, password, ssh_opts, "/bin/echo ok", check=False, timeout=15)
    except SystemExit:
        return False
    return proc.returncode == 0


def prompt_host_and_password(existing: dict[str, str], values: dict[str, str]) -> None:
    host_default = values.get("TC_HOST", existing.get("TC_HOST", DEFAULTS["TC_HOST"]))
    password_default = values.get("TC_PASSWORD", existing.get("TC_PASSWORD", ""))
    values["TC_HOST"] = prompt("Time Capsule SSH target", host_default, False)
    values["TC_PASSWORD"] = prompt("Time Capsule root password", password_default, True)


def main(argv: Optional[list[str]] = None) -> int:
    existing = parse_env_values(ENV_PATH, defaults={})
    values: dict[str, str] = {}

    print(f"Writing {ENV_PATH}")
    print("\033[31mPress Enter\033[0m to accept the current/default value.\n")

    ssh_opts = existing.get("TC_SSH_OPTS", DEFAULTS["TC_SSH_OPTS"])
    prompt_host_and_password(existing, values)
    while not validate_ssh_target(values["TC_HOST"], values["TC_PASSWORD"], ssh_opts):
        print("\nThe provided Time Capsule SSH target and password did not work.")
        if confirm("Save this information still?"):
            break
        print("Please enter the SSH target and password again.\n")
        prompt_host_and_password(existing, values)

    for key, label, default, secret in CONFIG_FIELDS[2:]:
        current = existing.get(key, default)
        values[key] = prompt(label, current, secret)

    write_env_file(ENV_PATH, values)
    print(f"\nWrote {ENV_PATH}")
    print("Next steps:")
    print("  1. Review .env")
    print("  2. If you are doing build work, configure build/.env separately from build/.env.example")
    return 0
