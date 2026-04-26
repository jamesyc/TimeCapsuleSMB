from __future__ import annotations

import argparse
import getpass
import ipaddress
import uuid
from dataclasses import dataclass
from typing import Optional

from timecapsulesmb.core.config import (
    CONFIG_VALIDATORS,
    CONFIG_FIELDS,
    DEFAULTS,
    ENV_PATH,
    extract_host,
    infer_mdns_device_model_from_airport_syap,
    parse_env_values,
    parse_bool,
    upsert_env_key,
    write_env_file,
)
from timecapsulesmb.cli.context import CommandContext, missing_dependency_message, missing_required_python_module
from timecapsulesmb.cli.runtime import probe_connection_state
from timecapsulesmb.identity import ensure_install_id
from timecapsulesmb.device.compat import DeviceCompatibility, render_compatibility_message
from timecapsulesmb.device.probe import (
    RemoteInterfaceCandidatesProbeResult,
    preferred_interface_name,
    probe_remote_interface_candidates_conn,
)
from timecapsulesmb.discovery.bonjour import (
    BonjourResolvedService,
    AIRPORT_SERVICE,
    discover_resolved_records,
    discovered_record_root_host,
    record_has_service,
)
from timecapsulesmb.telemetry import TelemetryClient
from timecapsulesmb.transport.ssh import SshConnection
from timecapsulesmb.cli.util import color_cyan

HIDDEN_CONFIG_KEYS = {"TC_SSH_OPTS", "TC_CONFIGURE_ID"}
NO_SAVED_VALUE_HINT_KEYS = {"TC_PASSWORD", *HIDDEN_CONFIG_KEYS}
REQUIRED_PYTHON_MODULES = ("zeroconf", "pexpect", "ifaddr")


@dataclass(frozen=True)
class ConfigureValueChoice:
    value: str
    source: str


@dataclass(frozen=True)
class InterfaceIpMatch:
    iface: str
    ip: str


@dataclass(frozen=True)
class DerivedNameDefaults:
    netbios_name: str
    mdns_instance_name: str
    mdns_host_label: str


def prompt(label: str, default: str, secret: bool) -> str:
    suffix = f" [{color_cyan(default)}]" if default and not secret else ""
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


def confirm(prompt_text: str, default_no: bool = False) -> bool:
    if default_no:
        answer = input(f"{prompt_text} [y/N]: ").strip().lower()
        return answer in {"Y", "y", "yes"}
    answer = input(f"{prompt_text} [Y/n]: ").strip().lower()
    return answer in {"", "Y", "y", "yes"}


def list_devices(records) -> None:
    print("Found devices:")
    for i, record in enumerate(records, start=1):
        pref = record.prefer_host()
        ipv4 = ",".join(record.ipv4) if record.ipv4 else "-"
        print(f"  {i}. {record.name} | host: {pref} | IPv4: {ipv4}")


def choose_device(records):
    while True:
        try:
            raw = input("Select a device by number (q to skip discovery): ").strip()
        except EOFError:
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


def discover_default_record(existing: dict[str, str]) -> Optional[BonjourResolvedService]:
    print("Attempting to discover Time Capsules on the local network via mDNS...", flush=True)
    records = discover_resolved_records(AIRPORT_SERVICE, timeout=5.0)
    if not records:
        print("No Time Capsules discovered. Falling back to manual SSH target entry.\n", flush=True)
        return None
    list_devices(records)
    selected = choose_device(records)
    if selected is None:
        existing_target = valid_existing_config_value(existing, "TC_HOST", "Time Capsule SSH target") or DEFAULTS["TC_HOST"]
        print(f"Discovery skipped. Falling back to {existing_target}.\n", flush=True)
        return None

    chosen_host = discovered_record_root_host(selected)
    selected_host = chosen_host.removeprefix("root@") if chosen_host else selected.prefer_host()
    print(f"Selected: {selected.name} ({selected_host})\n", flush=True)
    return selected


def prompt_host_and_password(existing: dict[str, str], values: dict[str, str], discovered_host: Optional[str]) -> None:
    host_default = values.get("TC_HOST") or discovered_host or valid_existing_config_value(
        existing,
        "TC_HOST",
        "Time Capsule SSH target",
    ) or DEFAULTS["TC_HOST"]
    password_default = values.get("TC_PASSWORD", existing.get("TC_PASSWORD", ""))
    values["TC_HOST"] = prompt_valid_config_value("TC_HOST", "Time Capsule SSH target", host_default)
    values["TC_PASSWORD"] = prompt("Time Capsule root password", password_default, True)


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


def prompt_config_value_from_candidates(
    key: str,
    label: str,
    current: str,
    allowed_values: tuple[str, ...],
    *,
    secret: bool = False,
    invalid_message: str | None = None,
) -> str:
    allowed = set(allowed_values)
    while True:
        candidate = prompt_valid_config_value(key, label, current, secret=secret)
        if candidate in allowed:
            return candidate
        print(invalid_message or f"{label} must be one of: {', '.join(allowed_values)}")


def print_saved_value_hint(value: str) -> None:
    print(f"Found saved value: {value}")


def print_reused_env_value(key: str, value: str) -> None:
    print(f"Using {key} from .env: {value}")


def print_automatic_value_choice(key: str, choice: ConfigureValueChoice) -> None:
    if choice.source == "saved":
        print_reused_env_value(key, choice.value)
    elif choice.source == "discovered":
        print(f"Using discovered {key}: {choice.value}")
    elif choice.source == "probed":
        print(f"Using probed {key}: {choice.value}")
    elif choice.source == "derived":
        print(f"Using {key} derived from TC_AIRPORT_SYAP: {choice.value}")


def _ipv4_literal(value: str) -> str | None:
    value = value.strip()
    try:
        parsed = ipaddress.ip_address(value)
    except ValueError:
        parts = value.split(".")
        if len(parts) != 4 or any(not part.isdigit() for part in parts):
            return None
        octets: list[str] = []
        for part in parts:
            octet = int(part, 10)
            if octet < 0 or octet > 255:
                return None
            octets.append(str(octet))
        return ".".join(octets)
    if parsed.version != 4:
        return None
    return str(parsed)


def interface_target_ips(values: dict[str, str], discovered_record: BonjourResolvedService | None) -> tuple[str, ...]:
    ordered: list[str] = []
    host_ip = _ipv4_literal(extract_host(values.get("TC_HOST", "")))
    if host_ip:
        ordered.append(host_ip)
    if discovered_record is not None:
        for value in discovered_record.ipv4:
            ip_value = _ipv4_literal(value)
            if ip_value and ip_value not in ordered:
                ordered.append(ip_value)
    return tuple(ordered)


def _is_link_local_ipv4(value: str) -> bool:
    return value.startswith("169.254.")


def interface_candidate_for_ip(result: RemoteInterfaceCandidatesProbeResult, target_ips: tuple[str, ...]) -> InterfaceIpMatch | None:
    # Prefer exact non-link-local matches first. Link-local can still be used,
    # but only when it is the only exact address we can match to an interface.
    ordered_target_ips = tuple(ip for ip in target_ips if not _is_link_local_ipv4(ip)) + tuple(
        ip for ip in target_ips if _is_link_local_ipv4(ip)
    )
    for target_ip in ordered_target_ips:
        for candidate in result.candidates:
            if candidate.loopback:
                continue
            if target_ip in candidate.ipv4_addrs:
                return InterfaceIpMatch(iface=candidate.name, ip=target_ip)
    return None


def print_probed_interface_default(result: RemoteInterfaceCandidatesProbeResult, preferred_iface: str) -> None:
    candidate_names = [candidate.name for candidate in result.candidates if candidate.ipv4_addrs and not candidate.loopback]
    if candidate_names:
        print("Found network interfaces with IPv4 on the device:")
        for candidate in result.candidates:
            if not candidate.ipv4_addrs or candidate.loopback:
                continue
            marker = " (suggested)" if candidate.name == preferred_iface else ""
            print(f"  {candidate.name}: {', '.join(candidate.ipv4_addrs)}{marker}")
    print(f"Using probed default for TC_NET_IFACE: {preferred_iface}")


def _best_non_link_local_ipv4(
    values: dict[str, str],
    discovered_record: BonjourResolvedService | None,
    probed_interfaces: RemoteInterfaceCandidatesProbeResult | None,
) -> str | None:
    host_ip = _ipv4_literal(extract_host(values.get("TC_HOST", "")))
    if host_ip and not _is_link_local_ipv4(host_ip):
        return host_ip

    if discovered_record is not None:
        for value in discovered_record.ipv4:
            ip_value = _ipv4_literal(value)
            if ip_value and not _is_link_local_ipv4(ip_value):
                return ip_value

    if probed_interfaces is not None and probed_interfaces.candidates:
        target_ips = interface_target_ips(values, discovered_record)
        preferred_iface = preferred_interface_name(probed_interfaces.candidates, target_ips=target_ips)
        if preferred_iface is None:
            preferred_iface = probed_interfaces.preferred_iface
        if preferred_iface:
            for candidate in probed_interfaces.candidates:
                if candidate.name != preferred_iface or candidate.loopback:
                    continue
                for ip_value in candidate.ipv4_addrs:
                    if not _is_link_local_ipv4(ip_value):
                        return ip_value
    return None


def derived_name_defaults(
    values: dict[str, str],
    discovered_record: BonjourResolvedService | None,
    probed_interfaces: RemoteInterfaceCandidatesProbeResult | None,
) -> DerivedNameDefaults | None:
    source_ip = _best_non_link_local_ipv4(values, discovered_record, probed_interfaces)
    if source_ip is None:
        return None
    last_octet = source_ip.rsplit(".", 1)[-1]
    suffix = f"{int(last_octet):03d}"
    return DerivedNameDefaults(
        netbios_name=f"TimeCapsule{suffix}",
        mdns_instance_name=f"Time Capsule Samba {suffix}",
        mdns_host_label=f"timecapsulesamba{suffix}",
    )


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
    parser = argparse.ArgumentParser(description="Create or update the local TimeCapsuleSMB .env configuration.")
    parser.add_argument("--share-use-disk-root", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    ensure_install_id()
    existing = parse_env_values(ENV_PATH, defaults={})
    configure_id = str(uuid.uuid4())
    upsert_env_key(ENV_PATH, "TC_CONFIGURE_ID", configure_id)
    telemetry_values = dict(existing)
    telemetry_values["TC_CONFIGURE_ID"] = configure_id
    telemetry = TelemetryClient.from_values(telemetry_values)
    values: dict[str, str] = {}
    discovered_airport_syap: Optional[str] = None
    probed_device: DeviceCompatibility | None = None
    probed_interfaces: RemoteInterfaceCandidatesProbeResult | None = None
    with CommandContext(
        telemetry,
        "configure",
        "configure_started",
        "configure_finished",
        values=values,
        args=args,
        configure_id=configure_id,
    ) as command_context:
        command_context.update_fields(configure_id=configure_id)
        command_context.set_stage("dependency_check")
        missing_module = missing_required_python_module(REQUIRED_PYTHON_MODULES)
        if missing_module is not None:
            message = missing_dependency_message(missing_module)
            print(message)
            command_context.set_error(message)
            command_context.fail()
            return 1

        command_context.set_stage("startup")
        print("This writes a local .env configuration file in this folder. The other tcapsule commands use that file.")
        print(f"Writing {ENV_PATH}")
        print(f"Press Enter to accept the [{color_cyan('saved/suggested/default')}] value.")
        print("Most users can just keep the suggested values.\n")

        ssh_opts = existing.get("TC_SSH_OPTS", DEFAULTS["TC_SSH_OPTS"])
        values["TC_SSH_OPTS"] = ssh_opts
        existing_share_use_disk_root = parse_bool(existing.get("TC_SHARE_USE_DISK_ROOT", DEFAULTS["TC_SHARE_USE_DISK_ROOT"]))
        values["TC_SHARE_USE_DISK_ROOT"] = "true" if args.share_use_disk_root or existing_share_use_disk_root else "false"
        command_context.set_stage("bonjour_discovery")
        discovered_record = discover_default_record(existing)
        command_context.add_debug_fields(selected_bonjour_record=discovered_record)
        discovered_host = discovered_record_root_host(discovered_record) if discovered_record else None
        command_context.add_debug_fields(discovered_host=discovered_host)
        if discovered_record is not None:
            discovered_airport_syap = discovered_record.properties.get("syAP") or None
            command_context.add_debug_fields(discovered_airport_syap=discovered_airport_syap)
        command_context.set_stage("prompt_host_password")
        prompt_host_and_password(existing, values, discovered_host)
        while True:
            command_context.set_stage("ssh_probe")
            print("Checking login information...")
            connection = SshConnection(values["TC_HOST"], values["TC_PASSWORD"], ssh_opts)
            command_context.connection = connection
            probed_state = probe_connection_state(connection)
            command_context.probe_state = probed_state
            probe_result = probed_state.probe_result
            if not probe_result.ssh_port_reachable:
                print("\nSSH is not reachable yet, so configure cannot validate this password.")
                print("That is okay if you have not run 'tcapsule prep-device' yet.")
                if confirm("Save this information still?", True):
                    command_context.add_debug_fields(configure_saved_without_ssh_reachability=True)
                    break
                print("Please enter the SSH target and password again.\n")
                command_context.add_debug_fields(configure_retry_reason="ssh_not_reachable")
                command_context.set_stage("prompt_host_password")
                prompt_host_and_password(existing, values, discovered_host)
                continue
            if probe_result.ssh_authenticated:
                probed_device = probed_state.compatibility
                command_context.compatibility = probed_device
                if probed_device is not None and not probed_device.supported:
                    command_context.add_debug_fields(configure_failure_reason="unsupported_device")
                    raise SystemExit(render_compatibility_message(probed_device))
                command_context.set_stage("interface_probe")
                probed_interfaces = probe_remote_interface_candidates_conn(connection)
                command_context.add_debug_fields(interface_candidates=probed_interfaces)
                break
            print("\nThe provided Time Capsule SSH target and password did not work.")
            if confirm("Save this information still?", True):
                command_context.add_debug_fields(configure_saved_without_ssh_authentication=True)
                break
            print("Please enter the SSH target and password again.\n")
            command_context.add_debug_fields(configure_retry_reason="ssh_authentication_failed")
            command_context.set_stage("prompt_host_password")
            prompt_host_and_password(existing, values, discovered_host)

        command_context.set_stage("prompt_config_fields")
        discovered_airport_identity = (
            record_has_service(discovered_record, AIRPORT_SERVICE)
            if discovered_record is not None
            else False
        )
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
        inferred_syap_choice = None
        if probed_device and probed_device.exact_syap:
            inferred_syap_choice = ConfigureValueChoice(value=probed_device.exact_syap, source="probed")
        inferred_model_choice = None
        if probed_device and probed_device.exact_model:
            inferred_model_choice = ConfigureValueChoice(value=probed_device.exact_model, source="probed")
        saved_syap_choice = saved_value_choice(existing, "TC_AIRPORT_SYAP", "Airport Utility syAP code")
        saved_model_choice = saved_value_choice(existing, "TC_MDNS_DEVICE_MODEL", "mDNS device model hint")
        name_defaults = derived_name_defaults(values, discovered_record, probed_interfaces)
        if name_defaults is not None:
            command_context.add_debug_fields(
                derived_netbios_name=name_defaults.netbios_name,
                derived_mdns_instance_name=name_defaults.mdns_instance_name,
                derived_mdns_host_label=name_defaults.mdns_host_label,
            )
        derived_prompt_defaults = {
            "TC_NETBIOS_NAME": name_defaults.netbios_name if name_defaults is not None else DEFAULTS["TC_NETBIOS_NAME"],
            "TC_MDNS_INSTANCE_NAME": (
                name_defaults.mdns_instance_name if name_defaults is not None else DEFAULTS["TC_MDNS_INSTANCE_NAME"]
            ),
            "TC_MDNS_HOST_LABEL": name_defaults.mdns_host_label if name_defaults is not None else DEFAULTS["TC_MDNS_HOST_LABEL"],
        }

        for key, label, default, secret in CONFIG_FIELDS[2:]:
            if key == "TC_AIRPORT_SYAP":
                if discovered_syap_choice is not None:
                    print_automatic_value_choice(key, discovered_syap_choice)
                    values[key] = discovered_syap_choice.value
                    continue
                if inferred_syap_choice is not None:
                    print_automatic_value_choice(key, inferred_syap_choice)
                    values[key] = inferred_syap_choice.value
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
                if probed_device and probed_device.syap_candidates:
                    print_syap_prompt_help()
                    values[key] = prompt_config_value_from_candidates(
                        key,
                        label,
                        "",
                        probed_device.syap_candidates,
                        invalid_message=f"From detected connection, syAP code should be one of: {', '.join(probed_device.syap_candidates)}",
                    )
                    continue
                print_syap_prompt_help()
                values[key] = prompt_valid_config_value(key, label, DEFAULTS["TC_AIRPORT_SYAP"])
                continue
            if key == "TC_NET_IFACE":
                saved_iface_choice = saved_value_choice(existing, key, label)
                candidate_names = {
                    candidate.name
                    for candidate in (probed_interfaces.candidates if probed_interfaces is not None else ())
                    if candidate.ipv4_addrs and not candidate.loopback
                }
                target_ips = interface_target_ips(values, discovered_record)
                exact_target_match = (
                    interface_candidate_for_ip(probed_interfaces, target_ips)
                    if probed_interfaces is not None
                    else None
                )
                if saved_iface_choice is not None and (not candidate_names or saved_iface_choice.value in candidate_names):
                    if exact_target_match and exact_target_match.iface != saved_iface_choice.value:
                        print_saved_value_hint(saved_iface_choice.value)
                        print(
                            f"Probed target IP {exact_target_match.ip} is on {exact_target_match.iface}, "
                            f"so {exact_target_match.iface} is suggested instead."
                        )
                    else:
                        print_saved_value_hint(saved_iface_choice.value)
                        command_context.add_debug_fields(selected_net_iface=saved_iface_choice.value, selected_net_iface_source="saved")
                        values[key] = prompt_valid_config_value(key, label, saved_iface_choice.value)
                        continue
                if exact_target_match and probed_interfaces is not None:
                    print_probed_interface_default(probed_interfaces, exact_target_match.iface)
                    command_context.add_debug_fields(selected_net_iface=exact_target_match.iface, selected_net_iface_source="target_ip_match")
                    values[key] = prompt_valid_config_value(key, label, exact_target_match.iface)
                    continue
                if saved_iface_choice is not None and not candidate_names:
                    print_saved_value_hint(saved_iface_choice.value)
                    command_context.add_debug_fields(selected_net_iface=saved_iface_choice.value, selected_net_iface_source="saved_no_probe_candidates")
                    values[key] = prompt_valid_config_value(key, label, saved_iface_choice.value)
                    continue
                if probed_interfaces is not None and probed_interfaces.candidates:
                    preferred_iface = preferred_interface_name(probed_interfaces.candidates, target_ips=target_ips)
                    if preferred_iface:
                        print_probed_interface_default(probed_interfaces, preferred_iface)
                        command_context.add_debug_fields(selected_net_iface=preferred_iface, selected_net_iface_source="probed_preferred_for_target_ips")
                        values[key] = prompt_valid_config_value(key, label, preferred_iface)
                        continue
                if probed_interfaces is not None and probed_interfaces.preferred_iface:
                    print_probed_interface_default(probed_interfaces, probed_interfaces.preferred_iface)
                    command_context.add_debug_fields(selected_net_iface=probed_interfaces.preferred_iface, selected_net_iface_source="probed_preferred")
                    values[key] = prompt_valid_config_value(key, label, probed_interfaces.preferred_iface)
                    continue
                command_context.add_debug_fields(selected_net_iface_source="manual_or_default")
                values[key] = prompt_config_value(existing, key, label, default, secret=secret)
                continue
            if key in derived_prompt_defaults:
                values[key] = prompt_config_value(
                    existing,
                    key,
                    label,
                    derived_prompt_defaults[key],
                    secret=secret,
                )
                continue
            if key == "TC_MDNS_DEVICE_MODEL":
                syap_derived_model = infer_mdns_device_model_from_airport_syap(values.get("TC_AIRPORT_SYAP", ""))
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
                if probed_device and probed_device.model_candidates:
                    values[key] = prompt_config_value_from_candidates(
                        key,
                        label,
                        DEFAULTS["TC_MDNS_DEVICE_MODEL"] if DEFAULTS["TC_MDNS_DEVICE_MODEL"] in probed_device.model_candidates else "",
                        probed_device.model_candidates,
                    )
                    continue
                values[key] = prompt_valid_config_value(key, label, DEFAULTS["TC_MDNS_DEVICE_MODEL"])
                continue
            values[key] = prompt_config_value(existing, key, label, default, secret=secret)

        values["TC_CONFIGURE_ID"] = configure_id
        command_context.set_stage("write_env")
        write_env_file(ENV_PATH, values)
        command_context.update_fields(
            configure_id=configure_id,
            device_syap=values.get("TC_AIRPORT_SYAP"),
            device_model=values.get("TC_MDNS_DEVICE_MODEL"),
        )
        print(f"\nReview the .env file configuration: wrote {ENV_PATH}")
        print("Next steps:")
        print("- Prep your device to enable SSH on it, run:")
        print("    .venv/bin/tcapsule prep-device")
        print("")
        print("- Deploy this configuration to your Time Capsule, run:")
        print("    .venv/bin/tcapsule deploy")
        command_context.succeed()
        return 0
    return 1
