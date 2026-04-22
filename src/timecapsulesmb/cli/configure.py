from __future__ import annotations

import getpass
import uuid
from dataclasses import dataclass
from typing import Optional

from timecapsulesmb.core.config import (
    CONFIG_VALIDATORS,
    CONFIG_FIELDS,
    DEFAULTS,
    ENV_PATH,
    extract_host,
    parse_env_values,
    upsert_env_key,
    write_env_file,
)
from timecapsulesmb.cli.context import CommandContext
from timecapsulesmb.identity import ensure_install_id
from timecapsulesmb.device.compat import infer_mdns_device_model_hint
from timecapsulesmb.discovery.bonjour import Discovered, discover, prefer_routable_ipv4, preferred_host
from timecapsulesmb.telemetry import TelemetryClient
from timecapsulesmb.transport.local import tcp_open
from timecapsulesmb.transport.ssh import run_ssh

AIRPORT_SYAP_TO_MODEL = {
    "106": "TimeCapsule6,106",
    "109": "TimeCapsule6,109",
    "113": "TimeCapsule6,113",
    "116": "TimeCapsule6,116",
    "119": "TimeCapsule8,119",
}

HIDDEN_CONFIG_KEYS = {"TC_SSH_OPTS", "TC_CONFIGURE_ID"}
NO_SAVED_VALUE_HINT_KEYS = {"TC_PASSWORD", *HIDDEN_CONFIG_KEYS}


@dataclass(frozen=True)
class ConfigureValueChoice:
    value: str
    source: str


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


def _discovered_root_host(record: Discovered) -> Optional[str]:
    chosen_host = prefer_routable_ipv4(record) or preferred_host(record)
    return f"root@{chosen_host}" if chosen_host else None


def discover_default_record(existing: dict[str, str]) -> Optional[Discovered]:
    print("Attempting to discover Time Capsules on the local network via mDNS...", flush=True)
    records = discover(timeout=5.0)
    if not records:
        print("No Time Capsules discovered. Falling back to manual SSH target entry.\n", flush=True)
        return None
    list_devices(records)
    selected = choose_device(records)
    if selected is None:
        existing_target = existing.get("TC_HOST", DEFAULTS["TC_HOST"])
        print(f"Discovery skipped. Falling back to {existing_target}.\n", flush=True)
        return None

    chosen_host = prefer_routable_ipv4(selected) or preferred_host(selected)
    print(f"Selected: {selected.name} ({chosen_host})\n", flush=True)
    return selected


def prompt_host_and_password(existing: dict[str, str], values: dict[str, str], discovered_host: Optional[str]) -> None:
    host_default = values.get("TC_HOST", discovered_host or existing.get("TC_HOST", DEFAULTS["TC_HOST"]))
    password_default = values.get("TC_PASSWORD", existing.get("TC_PASSWORD", ""))
    values["TC_HOST"] = prompt("Time Capsule SSH target", host_default, False)
    values["TC_PASSWORD"] = prompt("Time Capsule root password", password_default, True)


def validate_ssh_target_if_reachable(host: str, password: str, ssh_opts: str) -> Optional[bool]:
    if not tcp_open(extract_host(host), 22):
        return None
    return validate_ssh_target(host, password, ssh_opts)


def infer_mdns_device_model_from_syap(syap: str) -> Optional[str]:
    return AIRPORT_SYAP_TO_MODEL.get(syap)


def validated_value_or_empty(key: str, value: str, label: str) -> str:
    validator = CONFIG_VALIDATORS.get(key)
    if not value or validator is None:
        return value
    if validator(value, label):
        return ""
    return value


def valid_existing_config_value(existing: dict[str, str], key: str, label: str) -> str:
    return validated_value_or_empty(key, existing.get(key, ""), label)


def saved_value_choice(existing: dict[str, str], key: str, label: str) -> Optional[ConfigureValueChoice]:
    value = valid_existing_config_value(existing, key, label)
    if not value:
        return None
    return ConfigureValueChoice(value=value, source="saved")


def print_syap_prompt_help() -> None:
    print("\nWarning: configure could not discover Airport Utility syAP from _airport._tcp.")
    print("Enter the device's syAP code so _airport._tcp can be cloned accurately.")
    print("")
    print("Generation                Model identifier    syAP")
    print("------------------------  ------------------  ----")
    print("1st gen (early 2008)      TimeCapsule6,106    106")
    print("2nd gen (early 2009)      TimeCapsule6,109    109")
    print("3rd gen (late 2009)       TimeCapsule6,113    113")
    print("4th gen (mid 2011)        TimeCapsule6,116    116")
    print("5th gen (mid 2013)        TimeCapsule8,119    119")


def prompt_valid_config_value(key: str, label: str, current: str, secret: bool = False) -> str:
    validator = CONFIG_VALIDATORS.get(key)
    while True:
        candidate = prompt(label, current, secret)
        if validator is not None:
            error = validator(candidate, label)
            if error:
                print(error)
                continue
        return candidate


def print_saved_value_hint(value: str) -> None:
    print(f"Found saved value: {value}")


def print_reused_env_value(key: str, value: str) -> None:
    print(f"Using {key} from .env: {value}")


def print_automatic_value_choice(key: str, choice: ConfigureValueChoice) -> None:
    if choice.source == "saved":
        print_reused_env_value(key, choice.value)
    elif choice.source == "discovered":
        print(f"Using discovered {key}: {choice.value}")
    elif choice.source == "inferred":
        print(f"Using inferred {key}: {choice.value}")
    elif choice.source == "derived":
        print(f"Using {key} derived from TC_AIRPORT_SYAP: {choice.value}")


def prompt_config_value(
    existing: dict[str, str],
    key: str,
    label: str,
    default: str,
    *,
    secret: bool = False,
) -> str:
    saved_choice = saved_value_choice(existing, key, label)
    current = default
    if saved_choice is not None:
        current = saved_choice.value
        if key not in NO_SAVED_VALUE_HINT_KEYS and not secret:
            print_saved_value_hint(saved_choice.value)
    return prompt_valid_config_value(key, label, current, secret)


def main(argv: Optional[list[str]] = None) -> int:
    ensure_install_id()
    existing = parse_env_values(ENV_PATH, defaults={})
    configure_id = str(uuid.uuid4())
    upsert_env_key(ENV_PATH, "TC_CONFIGURE_ID", configure_id)
    telemetry_values = dict(existing)
    telemetry_values["TC_CONFIGURE_ID"] = configure_id
    telemetry = TelemetryClient.from_values(telemetry_values)
    values: dict[str, str] = {}
    inferred_mdns_device_model: Optional[str] = None
    discovered_airport_syap: Optional[str] = None
    with CommandContext(
        telemetry,
        "configure",
        "configure_started",
        "configure_finished",
        configure_id=configure_id,
    ) as command_context:
        command_context.update_fields(configure_id=configure_id)
        print("This writes a local .env configuration file in this folder. The other tcapsule commands use that file.")
        print(f"Writing {ENV_PATH}")
        print("\033[31mPress Enter\033[0m to accept the current/default value.\n")

        ssh_opts = existing.get("TC_SSH_OPTS", DEFAULTS["TC_SSH_OPTS"])
        values["TC_SSH_OPTS"] = ssh_opts
        discovered_record = discover_default_record(existing)
        discovered_host = _discovered_root_host(discovered_record) if discovered_record else None
        if discovered_record is not None:
            discovered_airport_syap = discovered_record.properties.get("syAP") or None
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
                inferred_model = infer_mdns_device_model_hint(values["TC_HOST"], values["TC_PASSWORD"], ssh_opts)
                inferred_mdns_device_model = validated_value_or_empty(
                    "TC_MDNS_DEVICE_MODEL",
                    inferred_model or "",
                    "mDNS device model hint",
                ) or None
            except SystemExit:
                inferred_mdns_device_model = None

        discovered_airport_identity = discovered_record is not None
        valid_discovered_syap = validated_value_or_empty(
            "TC_AIRPORT_SYAP",
            discovered_airport_syap or "",
            "Airport Utility syAP code",
        )
        discovered_syap_choice = (
            ConfigureValueChoice(value=valid_discovered_syap, source="discovered")
            if valid_discovered_syap
            else None
        )
        inferred_model_choice = (
            ConfigureValueChoice(value=inferred_mdns_device_model, source="inferred")
            if inferred_mdns_device_model
            else None
        )
        saved_syap_choice = saved_value_choice(existing, "TC_AIRPORT_SYAP", "Airport Utility syAP code")
        saved_model_choice = saved_value_choice(existing, "TC_MDNS_DEVICE_MODEL", "mDNS device model hint")

        for key, label, default, secret in CONFIG_FIELDS[2:]:
            if key == "TC_AIRPORT_SYAP":
                if discovered_syap_choice is not None:
                    print_automatic_value_choice(key, discovered_syap_choice)
                    values[key] = discovered_syap_choice.value
                    continue
                if discovered_airport_identity:
                    print_syap_prompt_help()
                    if saved_syap_choice is not None:
                        print_saved_value_hint(saved_syap_choice.value)
                    values[key] = prompt_valid_config_value(key, label, saved_syap_choice.value if saved_syap_choice is not None else "")
                    continue
                if saved_syap_choice is not None:
                    print_automatic_value_choice(key, saved_syap_choice)
                    values[key] = saved_syap_choice.value
                    continue
                print_syap_prompt_help()
                values[key] = prompt_valid_config_value(key, label, DEFAULTS["TC_AIRPORT_SYAP"])
                continue
            if key == "TC_MDNS_DEVICE_MODEL":
                syap_derived_model = infer_mdns_device_model_from_syap(values.get("TC_AIRPORT_SYAP", ""))
                derived_model_choice = (
                    ConfigureValueChoice(value=syap_derived_model, source="derived")
                    if syap_derived_model
                    else None
                )
                automatic_model_choice = inferred_model_choice or derived_model_choice
                if automatic_model_choice is not None:
                    print_automatic_value_choice(key, automatic_model_choice)
                    values[key] = automatic_model_choice.value
                    continue
                if discovered_airport_identity:
                    if saved_model_choice is not None:
                        print_saved_value_hint(saved_model_choice.value)
                        values[key] = prompt_valid_config_value(key, label, saved_model_choice.value)
                    else:
                        values[key] = DEFAULTS["TC_MDNS_DEVICE_MODEL"]
                    continue
                if saved_model_choice is not None:
                    print_automatic_value_choice(key, saved_model_choice)
                    values[key] = saved_model_choice.value
                    continue
                values[key] = prompt_valid_config_value(key, label, DEFAULTS["TC_MDNS_DEVICE_MODEL"])
                continue
            values[key] = prompt_config_value(existing, key, label, default, secret=secret)

        values["TC_CONFIGURE_ID"] = configure_id
        write_env_file(ENV_PATH, values)
        command_context.update_fields(
            configure_id=configure_id,
            device_syap=values.get("TC_AIRPORT_SYAP"),
            device_model=values.get("TC_MDNS_DEVICE_MODEL"),
        )
        print(f"\nReview the .env file configuration: wrote {ENV_PATH}")
        print("Next steps:")
        print("  - Prep your device to enable SSH on it:")
        print("      Run .venv/bin/tcapsule prep-device")
        print("  - Deploy this configuration to your Time Capsule:")
        print("      Run .venv/bin/tcapsule deploy")
        command_context.succeed()
        return 0
