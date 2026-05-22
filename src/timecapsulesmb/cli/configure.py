from __future__ import annotations

import argparse
import getpass
import uuid
from typing import Optional

from timecapsulesmb.configure_defaults import (
    ConfigureValueChoice,
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
    infer_mdns_device_model_from_airport_syap,
    parse_env_file,
    parse_bool,
    write_env_file,
)
from timecapsulesmb.cli.context import CommandContext
from timecapsulesmb.cli.flows import wait_for_tcp_port_state
from timecapsulesmb.cli.runtime import (
    add_bonjour_timeout_argument,
    add_config_argument,
    confirm as confirm_prompt,
    ssh_target_link_local_resolution_error,
)
from timecapsulesmb.core.errors import missing_dependency_message, missing_required_python_module
from timecapsulesmb.core.net import extract_host
from timecapsulesmb.core.paths import resolve_app_paths
from timecapsulesmb.identity import ensure_install_id
from timecapsulesmb.device.compat import DeviceCompatibility, render_compatibility_message
from timecapsulesmb.device.probe import (
    ProbedDeviceState,
    RemoteInterfaceCandidatesProbeResult,
    probe_connection_state,
    probe_remote_interface_candidates_conn,
)
from timecapsulesmb.discovery.bonjour import (
    BonjourResolvedService,
    AIRPORT_SERVICE,
    discover_resolved_records,
    discovered_record_root_host,
)
from timecapsulesmb.discovery.devices import DiscoveredDeviceCandidate, device_candidates_from_records
from timecapsulesmb.telemetry import TelemetryClient
from timecapsulesmb.transport.ssh import SshConnection
from timecapsulesmb.integrations.acp import ACPAuthError, ACPError, enable_ssh
from timecapsulesmb.cli.util import color_cyan, color_red

HIDDEN_CONFIG_KEYS = {"TC_SSH_OPTS", "TC_CONFIGURE_ID"}
NO_SAVED_VALUE_HINT_KEYS = {"TC_PASSWORD", *HIDDEN_CONFIG_KEYS}
REQUIRED_PYTHON_MODULES = ("zeroconf", "pexpect", "ifaddr")
CONFIGURE_DETAIL_FIELDS = [
]


def non_negative_integer_arg(value: str) -> str:
    if not value.isdigit():
        raise argparse.ArgumentTypeError("must be a non-negative integer")
    return str(int(value))


def existing_config_value_or_default(existing: dict[str, str], key: str, label: str) -> str:
    return valid_existing_config_value(existing, key, label) or DEFAULTS[key]


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
    return confirm_prompt(prompt_text, default=not default_no, eof_default=False)


def list_devices(candidates: list[DiscoveredDeviceCandidate]) -> None:
    print("Found devices:")
    for i, candidate in enumerate(candidates, start=1):
        pref = candidate.host or "-"
        ipv4 = ",".join(candidate.ipv4) if candidate.ipv4 else "-"
        print(f"  {i}. {candidate.name} | host: {pref} | IPv4: {ipv4}")


def choose_device(candidates: list[DiscoveredDeviceCandidate]) -> DiscoveredDeviceCandidate | None:
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
        if not (1 <= idx <= len(candidates)):
            print("Out of range.")
            continue
        return candidates[idx - 1]


def discover_default_record(existing: dict[str, str], *, timeout: float) -> Optional[BonjourResolvedService]:
    print("Attempting to discover Time Capsule/Airport Extreme devices on the local network via mDNS...", flush=True)
    records = discover_resolved_records(AIRPORT_SERVICE, timeout=timeout)
    candidates = device_candidates_from_records(records, airport_only=False)
    if not candidates:
        print("No Time Capsule/Airport Extreme devices discovered. Falling back to manual SSH target entry.\n", flush=True)
        return None
    list_devices(candidates)
    selected = choose_device(candidates)
    if selected is None:
        existing_target = valid_existing_config_value(existing, "TC_HOST", "Device SSH target") or DEFAULTS["TC_HOST"]
        print(f"Discovery skipped. Falling back to {existing_target}.\n", flush=True)
        return None

    chosen_host = selected.ssh_host
    selected_host = (
        chosen_host.removeprefix("root@")
        if chosen_host
        else selected.hostname or "manual SSH target required"
    )
    print(f"Selected: {selected.name} ({selected_host})\n", flush=True)
    if chosen_host is None and selected.link_local_only:
        print(
            "Selected device only advertised 169.254.x.x link-local IPv4. "
            "Enter the device's LAN IP or LAN-resolving hostname manually.\n",
            flush=True,
        )
    return selected.selected_record


def exception_summary(exc: BaseException) -> str:
    message = str(exc)
    name = type(exc).__name__
    return f"{name}: {message}" if message else name


def prompt_ssh_target_value(
    existing: dict[str, str],
    values: dict[str, str],
    discovered_host: Optional[str],
    ssh_opts: str,
) -> str:
    host_default = values.get("TC_HOST") or discovered_host or valid_existing_config_value(
        existing,
        "TC_HOST",
        "Device SSH target",
    ) or DEFAULTS["TC_HOST"]
    while True:
        candidate = prompt_valid_config_value("TC_HOST", "Device SSH target", host_default)
        resolution_error = ssh_target_link_local_resolution_error(candidate, ssh_opts)
        if resolution_error is None:
            return candidate
        print(resolution_error)
        host_default = candidate


def prompt_host_and_password(
    existing: dict[str, str],
    values: dict[str, str],
    discovered_host: Optional[str],
    ssh_opts: str,
) -> None:
    password_default = values.get("TC_PASSWORD", existing.get("TC_PASSWORD", ""))
    values["TC_HOST"] = prompt_ssh_target_value(existing, values, discovered_host, ssh_opts)
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
    parser.add_argument("--any-protocol", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--debug-logging", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--ata-idle-seconds", type=non_negative_integer_arg, metavar="SECONDS", help=argparse.SUPPRESS)
    parser.add_argument("--ata-standby", type=non_negative_integer_arg, metavar="SECONDS", help=argparse.SUPPRESS)
    add_bonjour_timeout_argument(parser)
    args = parser.parse_args(argv)

    ensure_install_id()
    env_path = resolve_app_paths(config_path=args.config).config_path
    env_exists = env_path.exists()
    existing = parse_env_file(env_path)
    configure_id = str(uuid.uuid4())
    telemetry_values = dict(existing)
    telemetry_values["TC_CONFIGURE_ID"] = configure_id
    telemetry = TelemetryClient.from_config(
        AppConfig.from_values(
            telemetry_values,
            path=env_path,
            exists=env_exists,
            file_values=existing if env_exists else {},
        )
    )
    values: dict[str, str] = {}
    discovered_airport_syap: Optional[str] = None
    probed_device: DeviceCompatibility | None = None
    with CommandContext(
        telemetry,
        "configure",
        "configure_started",
        "configure_finished",
        values=values,
        args=args,
        configure_id=configure_id,
    ) as command_context:
        command_context.update_fields(configure_id=configure_id, bonjour_timeout=args.bonjour_timeout)
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
            existing.get("TC_INTERNAL_SHARE_USE_DISK_ROOT", DEFAULTS["TC_INTERNAL_SHARE_USE_DISK_ROOT"])
        )
        values["TC_INTERNAL_SHARE_USE_DISK_ROOT"] = (
            "true" if args.internal_share_use_disk_root or existing_internal_share_use_disk_root else "false"
        )
        existing_any_protocol = parse_bool(
            existing.get("TC_ANY_PROTOCOL", DEFAULTS["TC_ANY_PROTOCOL"])
        )
        values["TC_ANY_PROTOCOL"] = (
            "true" if args.any_protocol or existing_any_protocol else "false"
        )
        existing_debug_logging = parse_bool(
            existing.get("TC_DEBUG_LOGGING", DEFAULTS["TC_DEBUG_LOGGING"])
        )
        values["TC_DEBUG_LOGGING"] = (
            "true" if args.debug_logging or existing_debug_logging else "false"
        )
        existing_ata_idle_seconds = existing_config_value_or_default(
            existing,
            "TC_ATA_IDLE_SECONDS",
            "ATA idle seconds",
        )
        values["TC_ATA_IDLE_SECONDS"] = (
            args.ata_idle_seconds if args.ata_idle_seconds is not None else existing_ata_idle_seconds
        )
        existing_ata_standby = existing_config_value_or_default(
            existing,
            "TC_ATA_STANDBY",
            "ATA standby timer",
        )
        values["TC_ATA_STANDBY"] = args.ata_standby if args.ata_standby is not None else existing_ata_standby
        command_context.set_stage("bonjour_discovery")
        try:
            discovered_record = discover_default_record(existing, timeout=args.bonjour_timeout)
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
        prompt_host_and_password(existing, values, discovered_host, ssh_opts)
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
                    prompt_host_and_password(existing, values, discovered_host, ssh_opts)
                    continue
                except ACPError as exc:
                    message = f"Failed to enable SSH via ACP: {exc}"
                    print(color_red("Failed to enable SSH via ACP:"))
                    print(str(exc))
                    command_context.fail_with_error(message)
                    return 1
                if probed_state is None:
                    message = "SSH did not open after enabling via ACP. Reboot the device, wait 5 minutes, and try configure again."
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
            prompt_host_and_password(existing, values, discovered_host, ssh_opts)
            continue

        observed_syap_source = "probed"
        observed_syap = None if probed_device is None else probed_device.exact_syap
        if observed_syap is None:
            observed_syap = validated_value_or_empty(
                "TC_AIRPORT_SYAP",
                discovered_airport_syap or "",
                "Airport Utility syAP code",
            ) or None
            observed_syap_source = "discovered"
        observed_model_source = "probed"
        observed_model = None if probed_device is None else probed_device.exact_model
        if observed_model is None and observed_syap is not None:
            observed_model = infer_mdns_device_model_from_airport_syap(observed_syap)
            observed_model_source = "derived"
        if observed_syap is not None:
            values["TC_AIRPORT_SYAP"] = observed_syap
            print_automatic_value_choice(
                "TC_AIRPORT_SYAP",
                ConfigureValueChoice(value=observed_syap, source=observed_syap_source),
            )
        if observed_model is not None:
            values["TC_MDNS_DEVICE_MODEL"] = observed_model
            print_automatic_value_choice(
                "TC_MDNS_DEVICE_MODEL",
                ConfigureValueChoice(value=observed_model, source=observed_model_source),
            )

        command_context.set_stage("write_env")
        values["TC_CONFIGURE_ID"] = configure_id
        write_env_file(env_path, values)
        command_context.update_fields(
            configure_id=configure_id,
            device_syap=observed_syap,
            device_model=observed_model,
        )
        print(f"\nReview the .env file configuration: wrote {env_path}")
        print("Next steps:")
        print("- Deploy this configuration to your Time Capsule/Airport Extreme device, run:")
        print("    .venv/bin/tcapsule deploy")
        command_context.succeed()
        return 0
    return 1
