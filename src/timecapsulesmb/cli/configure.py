from __future__ import annotations

import getpass
from typing import Optional

from timecapsulesmb.core.config import CONFIG_FIELDS, DEFAULTS, ENV_PATH, extract_host, parse_env_values, write_env_file
from timecapsulesmb.device.compat import infer_mdns_device_model_hint
from timecapsulesmb.discovery.bonjour import discover, prefer_routable_ipv4, preferred_host
from timecapsulesmb.transport.local import tcp_open
from timecapsulesmb.transport.ssh import run_ssh


def prompt(label: str, default: str, secret: bool) -> str:
    suffix = f" [{default}]" if default and not secret else ""
    text = f"{label}{suffix}: "
    while True:
        value = getpass.getpass(text) if secret else input(text)
        if value != "":
            return value
        if default != "":
            return default
        if secret:
            print(f"{label} cannot be blank.")
            continue
        return default


def confirm(prompt_text: str) -> bool:
    answer = input(f"{prompt_text} [Y/n]: ").strip().lower()
    return answer in {"", "y", "yes"}


def validate_ssh_target(host: str, password: str, ssh_opts: str) -> bool:
    try:
        proc = run_ssh(host, password, ssh_opts, "/bin/echo ok", check=False, timeout=15)
    except SystemExit:
        return False
    return proc.returncode == 0


def list_devices(records) -> None:
    print("Found devices:")
    for i, record in enumerate(records, start=1):
        pref = preferred_host(record)
        ipv4 = ",".join(record.ipv4) if record.ipv4 else "-"
        print(f"  {i}. {record.name} | host: {pref} | IPv4: {ipv4}")


def choose_device(records):
    while True:
        try:
            raw = input("Select a device by number (q to skip discovery): ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if raw.lower() in {"q", "quit", "exit"}:
            return None
        if not raw.isdigit():
            print("Please enter a valid number.")
            continue
        idx = int(raw)
        if not (1 <= idx <= len(records)):
            print("Out of range.")
            continue
        return records[idx - 1]


def discover_default_host(existing: dict[str, str]) -> Optional[str]:
    print("Attempting to discover Time Capsules on the local network via mDNS...", flush=True)
    records = discover(timeout=5.0)
    if not records:
        print("No Time Capsules discovered. Falling back to manual SSH target entry.\n", flush=True)
        return None
    if len(records) == 1:
        selected = records[0]
        chosen_host = prefer_routable_ipv4(selected) or preferred_host(selected)
        print(f"Discovered: {selected.name} ({chosen_host})\n", flush=True)
        return f"root@{chosen_host}" if chosen_host else None

    list_devices(records)
    selected = choose_device(records)
    if selected is None:
        existing_target = existing.get("TC_HOST", DEFAULTS["TC_HOST"])
        print(f"Discovery skipped. Falling back to {existing_target}.\n", flush=True)
        return None

    chosen_host = prefer_routable_ipv4(selected) or preferred_host(selected)
    print(f"Selected: {selected.name} ({chosen_host})\n", flush=True)
    return f"root@{chosen_host}" if chosen_host else None


def prompt_host_and_password(existing: dict[str, str], values: dict[str, str], discovered_host: Optional[str]) -> None:
    host_default = values.get("TC_HOST", discovered_host or existing.get("TC_HOST", DEFAULTS["TC_HOST"]))
    password_default = values.get("TC_PASSWORD", existing.get("TC_PASSWORD", ""))
    values["TC_HOST"] = prompt("Time Capsule SSH target", host_default, False)
    values["TC_PASSWORD"] = prompt("Time Capsule root password", password_default, True)


def validate_ssh_target_if_reachable(host: str, password: str, ssh_opts: str) -> Optional[bool]:
    if not tcp_open(extract_host(host), 22):
        return None
    return validate_ssh_target(host, password, ssh_opts)


def main(argv: Optional[list[str]] = None) -> int:
    existing = parse_env_values(ENV_PATH, defaults={})
    values: dict[str, str] = {}
    inferred_mdns_device_model: Optional[str] = None

    print("This writes a local .env configuration file in this folder. The other tcapsule commands use that file.")
    print(f"Writing {ENV_PATH}")
    print("\033[31mPress Enter\033[0m to accept the current/default value.\n")

    ssh_opts = existing.get("TC_SSH_OPTS", DEFAULTS["TC_SSH_OPTS"])
    discovered_host = discover_default_host(existing)
    prompt_host_and_password(existing, values, discovered_host)
    while True:
        validation_result = validate_ssh_target_if_reachable(values["TC_HOST"], values["TC_PASSWORD"], ssh_opts)
        if validation_result is None:
            print("\nSSH is not reachable yet, so configure cannot validate this password.")
            print("That is okay if you have not run 'tcapsule prep-device' yet.")
            if confirm("Save this information still?"):
                break
            print("Please enter the SSH target and password again.\n")
            prompt_host_and_password(existing, values, discovered_host)
            continue
        if validation_result:
            break
        print("\nThe provided Time Capsule SSH target and password did not work.")
        if confirm("Save this information still?"):
            break
        print("Please enter the SSH target and password again.\n")
        prompt_host_and_password(existing, values, discovered_host)

    if validation_result:
        try:
            inferred_mdns_device_model = infer_mdns_device_model_hint(values["TC_HOST"], values["TC_PASSWORD"], ssh_opts)
        except SystemExit:
            inferred_mdns_device_model = None

    for key, label, default, secret in CONFIG_FIELDS[2:]:
        current = existing.get(key, default)
        if key == "TC_MDNS_DEVICE_MODEL" and inferred_mdns_device_model:
            if not current or current == DEFAULTS["TC_MDNS_DEVICE_MODEL"]:
                current = inferred_mdns_device_model
        values[key] = prompt(label, current, secret)

    write_env_file(ENV_PATH, values)
    print(f"\nWrote {ENV_PATH}")
    print("Next steps:")
    print("  1. Review .env")
    print("  2. Run .venv/bin/tcapsule prep-device")
    print("  3. If you are doing build work, configure build/.env separately from build/.env.example")
    return 0
