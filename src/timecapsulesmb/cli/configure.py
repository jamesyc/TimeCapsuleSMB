from __future__ import annotations

import argparse
import getpass
import uuid
from typing import Optional

from timecapsulesmb.configure_defaults import (
    ConfigureValueChoice,
    derived_name_defaults,
    derived_prompt_defaults,
    interface_candidate_for_ip,
    interface_target_ips,
    saved_syap_value_for_candidates,
    saved_value_choice,
    valid_existing_config_value,
    validated_value_or_empty,
)
from timecapsulesmb.core.config import (
    AIRPORT_DEVICE_IDENTITIES,
    AppConfig,
    CONFIG_VALIDATORS,
    DEFAULTS,
    ENV_PATH,
    extract_host,
    infer_mdns_device_model_from_airport_syap,
    parse_env_file,
    parse_bool,
    write_env_file,
)
from timecapsulesmb.cli.context import CommandContext, missing_dependency_message, missing_required_python_module
from timecapsulesmb.cli.flows import wait_for_tcp_port_state
from timecapsulesmb.cli.runtime import add_config_argument, probe_connection_state
from timecapsulesmb.core.paths import resolve_app_paths
from timecapsulesmb.identity import ensure_install_id
from timecapsulesmb.device.compat import DeviceCompatibility, render_compatibility_message
from timecapsulesmb.device.probe import (
    ProbedDeviceState,
    RemoteInterfaceCandidatesProbeResult,
    preferred_interface_name,
    probe_remote_interface_candidates_conn,
)
from timecapsulesmb.discovery.bonjour import (
    BonjourResolvedService,
    AIRPORT_SERVICE,
    DEFAULT_BROWSE_TIMEOUT_SEC,
    discover_resolved_records,
    discovered_record_root_host,
    record_has_service,
)
from timecapsulesmb.telemetry import TelemetryClient
from timecapsulesmb.transport.ssh import SshConnection
from timecapsulesmb.integrations.acp import ACPAuthError, ACPError, enable_ssh
from timecapsulesmb.cli.util import color_cyan, color_red

HIDDEN_CONFIG_KEYS = {"TC_SSH_OPTS", "TC_CONFIGURE_ID"}
NO_SAVED_VALUE_HINT_KEYS = {"TC_PASSWORD", *HIDDEN_CONFIG_KEYS}
REQUIRED_PYTHON_MODULES = ("zeroconf", "pexpect", "ifaddr")
CONFIGURE_DETAIL_FIELDS = [
    ("TC_NET_IFACE", "Network interface on the device", DEFAULTS["TC_NET_IFACE"], False),
    ("TC_SAMBA_USER", "Samba username", DEFAULTS["TC_SAMBA_USER"], False),
    ("TC_NETBIOS_NAME", "Samba NetBIOS name", DEFAULTS["TC_NETBIOS_NAME"], False),
    ("TC_PAYLOAD_DIR_NAME", "Persistent payload directory name", DEFAULTS["TC_PAYLOAD_DIR_NAME"], False),
    ("TC_MDNS_INSTANCE_NAME", "mDNS SMB instance name", DEFAULTS["TC_MDNS_INSTANCE_NAME"], False),
    ("TC_MDNS_HOST_LABEL", "mDNS host label", DEFAULTS["TC_MDNS_HOST_LABEL"], False),
    ("TC_AIRPORT_SYAP", "Airport Utility syAP code", DEFAULTS["TC_AIRPORT_SYAP"], False),
    ("TC_MDNS_DEVICE_MODEL", "mDNS device model hint", DEFAULTS["TC_MDNS_DEVICE_MODEL"], False),
]


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
    print("Attempting to discover Time Capsule/Airport Extreme devices on the local network via mDNS...", flush=True)
    records = discover_resolved_records(AIRPORT_SERVICE, timeout=DEFAULT_BROWSE_TIMEOUT_SEC)
    if not records:
        print("No Time Capsule/Airport Extreme devices discovered. Falling back to manual SSH target entry.\n", flush=True)
        return None
    list_devices(records)
    selected = choose_device(records)
    if selected is None:
        existing_target = valid_existing_config_value(existing, "TC_HOST", "Device SSH target") or DEFAULTS["TC_HOST"]
        print(f"Discovery skipped. Falling back to {existing_target}.\n", flush=True)
        return None

    chosen_host = discovered_record_root_host(selected)
    selected_host = chosen_host.removeprefix("root@") if chosen_host else selected.prefer_host()
    print(f"Selected: {selected.name} ({selected_host})\n", flush=True)
    return selected


def exception_summary(exc: BaseException) -> str:
    message = str(exc)
    name = type(exc).__name__
    return f"{name}: {message}" if message else name


def prompt_host_and_password(existing: dict[str, str], values: dict[str, str], discovered_host: Optional[str]) -> None:
    host_default = values.get("TC_HOST") or discovered_host or valid_existing_config_value(
        existing,
        "TC_HOST",
        "Device SSH target",
    ) or DEFAULTS["TC_HOST"]
    password_default = values.get("TC_PASSWORD", existing.get("TC_PASSWORD", ""))
    values["TC_HOST"] = prompt_valid_config_value("TC_HOST", "Device SSH target", host_default)
    values["TC_PASSWORD"] = prompt("Device root password", password_default, True)


def print_syap_prompt_help(syap_candidates: tuple[str, ...] | None = None) -> None:
    print("\nWarning: configure could not discover Airport Utility syAP from _airport._tcp.")
    print("Enter the device's syAP code so _airport._tcp can be cloned accurately.")
    print("")
    print("Device                           Model identifier    syAP")
    print("-------------------------------  ------------------  ----")
    identities = AIRPORT_DEVICE_IDENTITIES
    if syap_candidates is not None:
        allowed = set(syap_candidates)
        identities = tuple(identity for identity in identities if identity.syap in allowed)
    for identity in identities:
        print(f"{identity.display_name:<31}  {identity.mdns_model:<18}  {identity.syap}")
    print("")


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


def print_automatic_value_choice(key: str, choice: ConfigureValueChoice) -> None:
    if choice.source == "saved":
        print(f"Using {key} from .env: {choice.value}")
    elif choice.source == "discovered":
        print(f"Using discovered {key}: {choice.value}")
    elif choice.source == "probed":
        print(f"Using probed {key}: {choice.value}")
    elif choice.source == "derived":
        print(f"Using {key} derived from TC_AIRPORT_SYAP: {choice.value}")


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


def enable_ssh_and_reprobe_for_configure(
    connection: SshConnection,
    command_context: CommandContext,
    *,
    timeout_seconds: int = 180,
) -> ProbedDeviceState | None:
    host = extract_host(connection.host)
    command_context.add_debug_fields(
        configure_acp_enable_attempted=True,
        ssh_initially_reachable=False,
    )
    print("\nSSH is not reachable. Attempting to enable SSH on the device...")
    command_context.set_stage("acp_enable_ssh")
    try:
        enable_ssh(host, connection.password, reboot_device=True, log=print)
    except ACPAuthError:
        command_context.add_debug_fields(
            configure_acp_enable_succeeded=False,
            configure_retry_reason="acp_authentication_failed",
        )
        raise
    except ACPError:
        command_context.add_debug_fields(configure_acp_enable_succeeded=False)
        raise

    command_context.add_debug_fields(configure_acp_enable_succeeded=True)
    command_context.set_stage("wait_for_ssh_after_acp")
    if not wait_for_tcp_port_state(
        host,
        22,
        expected_state=True,
        timeout_seconds=timeout_seconds,
        service_name="SSH port",
    ):
        command_context.update_fields(ssh_final_reachable=False)
        return None

    command_context.update_fields(ssh_final_reachable=True)
    command_context.set_stage("ssh_probe_after_acp")
    return probe_connection_state(connection)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Create or update the local TimeCapsuleSMB .env configuration.")
    add_config_argument(parser)
    parser.add_argument("--internal-share-use-disk-root", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--share-use-disk-root", dest="internal_share_use_disk_root", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    ensure_install_id()
    env_path = resolve_app_paths(config_path=args.config).config_path
    existing = parse_env_file(env_path)
    configure_id = str(uuid.uuid4())
    telemetry_values = dict(existing)
    telemetry_values["TC_CONFIGURE_ID"] = configure_id
    telemetry = TelemetryClient.from_config(AppConfig.from_values(telemetry_values, path=env_path))
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
            module_name, error = missing_module
            message = missing_dependency_message(module_name, error)
            print(message)
            command_context.set_error(message)
            command_context.fail()
            return 1

        command_context.set_stage("startup")
        print("This writes a local .env configuration file in this folder. The other tcapsule commands use that file.")
        print(f"Writing {env_path}")
        print(f"Press Enter to accept the [{color_cyan('saved/suggested/default')}] value.")
        print("Most users can just keep the suggested values.\n")

        ssh_opts = existing.get("TC_SSH_OPTS", DEFAULTS["TC_SSH_OPTS"])
        values["TC_SSH_OPTS"] = ssh_opts
        existing_internal_share_use_disk_root = parse_bool(
            existing.get(
                "TC_INTERNAL_SHARE_USE_DISK_ROOT",
                existing.get("TC_SHARE_USE_DISK_ROOT", DEFAULTS["TC_INTERNAL_SHARE_USE_DISK_ROOT"]),
            )
        )
        values["TC_INTERNAL_SHARE_USE_DISK_ROOT"] = (
            "true" if args.internal_share_use_disk_root or existing_internal_share_use_disk_root else "false"
        )
        command_context.set_stage("bonjour_discovery")
        try:
            discovered_record = discover_default_record(existing)
        except Exception as exc:
            error_text = exception_summary(exc)
            print(f"Warning: mDNS discovery failed: {error_text}")
            print("This only affects automatic device discovery. Configure will continue with manual SSH target entry.")
            print("Falling back to manual SSH target entry.\n")
            command_context.update_fields(
                bonjour_discovery_failed=True,
                bonjour_discovery_fallback=True,
                bonjour_discovery_fallback_reason="discovery_exception",
                bonjour_discovery_error_type=type(exc).__name__,
                bonjour_discovery_error=error_text,
            )
            discovered_record = None
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
                try:
                    probed_state = enable_ssh_and_reprobe_for_configure(connection, command_context)
                except ACPAuthError as exc:
                    print("\nThe AirPort admin password did not work.")
                    print(str(exc))
                    print("Please enter the SSH target and password again.\n")
                    command_context.set_stage("prompt_host_password")
                    prompt_host_and_password(existing, values, discovered_host)
                    continue
                except ACPError as exc:
                    message = f"Failed to enable SSH via ACP: {exc}"
                    print(color_red("Failed to enable SSH via ACP:"))
                    print(str(exc))
                    command_context.fail_with_error(message)
                    return 1
                if probed_state is None:
                    message = "SSH did not open after enabling via ACP."
                    print(message)
                    command_context.fail_with_error(message)
                    return 1
                command_context.probe_state = probed_state
                probe_result = probed_state.probe_result
                if not probe_result.ssh_port_reachable:
                    message = "SSH did not become reachable after enabling via ACP."
                    print(message)
                    command_context.fail_with_error(message)
                    return 1
            if probe_result.ssh_authenticated:
                command_context.add_debug_fields(ssh_final_reachable=True)
                command_context.update_fields(ssh_final_reachable=True)
                probed_device = probed_state.compatibility
                command_context.compatibility = probed_device
                if probed_device is not None and not probed_device.supported:
                    command_context.add_debug_fields(configure_failure_reason="unsupported_device")
                    raise SystemExit(render_compatibility_message(probed_device))
                command_context.set_stage("interface_probe")
                probed_interfaces = probe_remote_interface_candidates_conn(connection)
                command_context.add_debug_fields(interface_candidates=probed_interfaces)
                break
            print("\nThe provided AirPort SSH target and password did not work.")
            if probe_result.ssh_port_reachable:
                command_context.update_fields(ssh_final_reachable=True)
            if confirm("Save this information still?", True):
                command_context.add_debug_fields(configure_saved_without_ssh_authentication=True)
                break
            print("Please enter the SSH target and password again.\n")
            command_context.add_debug_fields(configure_retry_reason="ssh_authentication_failed")
            command_context.set_stage("prompt_host_password")
            prompt_host_and_password(existing, values, discovered_host)
            continue

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
        prompt_defaults = derived_prompt_defaults(name_defaults)

        for key, label, default, secret in CONFIGURE_DETAIL_FIELDS:
            if key == "TC_AIRPORT_SYAP":
                candidate_syaps = probed_device.syap_candidates if probed_device is not None else ()
                saved_syap_value = saved_syap_value_for_candidates(saved_syap_choice, candidate_syaps)
                if discovered_syap_choice is not None:
                    print_automatic_value_choice(key, discovered_syap_choice)
                    values[key] = discovered_syap_choice.value
                    continue
                if inferred_syap_choice is not None:
                    print_automatic_value_choice(key, inferred_syap_choice)
                    values[key] = inferred_syap_choice.value
                    continue
                if discovered_airport_identity:
                    print_syap_prompt_help(candidate_syaps or None)
                    if saved_syap_choice is not None:
                        print_saved_value_hint(saved_syap_choice.value)
                    if candidate_syaps:
                        values[key] = prompt_config_value_from_candidates(
                            key,
                            label,
                            saved_syap_value or "",
                            candidate_syaps,
                            invalid_message=f"From detected connection, syAP code should be one of: {', '.join(candidate_syaps)}",
                        )
                    else:
                        values[key] = prompt_valid_config_value(key, label, saved_syap_choice.value if saved_syap_choice is not None else "")
                    continue
                if saved_syap_choice is not None and saved_syap_value is not None:
                    print_automatic_value_choice(key, saved_syap_choice)
                    values[key] = saved_syap_value
                    continue
                if candidate_syaps:
                    if saved_syap_choice is not None:
                        print_saved_value_hint(saved_syap_choice.value)
                    print_syap_prompt_help(candidate_syaps)
                    values[key] = prompt_config_value_from_candidates(
                        key,
                        label,
                        "",
                        candidate_syaps,
                        invalid_message=f"From detected connection, syAP code should be one of: {', '.join(candidate_syaps)}",
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
            if key in prompt_defaults:
                values[key] = prompt_config_value(
                    existing,
                    key,
                    label,
                    prompt_defaults[key],
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
        write_env_file(env_path, values)
        command_context.update_fields(
            configure_id=configure_id,
            device_syap=values.get("TC_AIRPORT_SYAP"),
            device_model=values.get("TC_MDNS_DEVICE_MODEL"),
        )
        print(f"\nReview the .env file configuration: wrote {env_path}")
        print("Next steps:")
        print("- Deploy this configuration to your Time Capsule/Airport Extreme device, run:")
        print("    .venv/bin/tcapsule deploy")
        command_context.succeed()
        return 0
    return 1
