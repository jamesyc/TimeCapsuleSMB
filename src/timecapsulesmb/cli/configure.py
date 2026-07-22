from __future__ import annotations

import argparse
import getpass
import sys
import uuid
from collections.abc import Callable, Sequence
from typing import Optional

from timecapsulesmb.configure_defaults import (
    ConfigureValueChoice,
    validated_value_or_empty,
)
from timecapsulesmb.core.config import (
    AppConfig,
    CONFIG_VALIDATORS,
    ConfigError,
    DEFAULTS,
    infer_mdns_device_model_from_airport_syap,
    parse_bool,
    parse_env_file,
)
from timecapsulesmb.core.smb_policy import validate_smb_protocol_options
from timecapsulesmb.cli.context import CommandContext
from timecapsulesmb.cli.runtime import (
    add_config_argument,
    add_no_input_argument,
    add_password_source_arguments,
    confirm as confirm_prompt,
    no_input_enabled,
    print_json,
    read_password_source_args,
)
from timecapsulesmb.core.errors import missing_dependency_message, missing_required_python_module
from timecapsulesmb.core.paths import resolve_app_paths
from timecapsulesmb.identity import ensure_install_id
from timecapsulesmb.services import configure as configure_service
from timecapsulesmb.services.callbacks import OperationCallbacks
from timecapsulesmb.services.configure import build_configure_env_values, write_configure_env_file
from timecapsulesmb.services.configure_target import resolve_configure_target
from timecapsulesmb.device.probe import (
    ProbedDeviceState,
    probe_connection_state,
)
from timecapsulesmb.discovery.bonjour import (
    BonjourResolvedService,
    AIRPORT_SERVICE,
    DEFAULT_BROWSE_TIMEOUT_SEC,
    BonjourMergedDiscoveryDiagnostics,
    discover_snapshot_merged_detailed,
    discovered_record_has_only_link_local_ips,
    discovered_record_root_host,
)
from timecapsulesmb.telemetry import TelemetryClient
from timecapsulesmb.transport.ssh import SshConnection
from timecapsulesmb.integrations.acp import ACPError
from timecapsulesmb.cli.util import color_cyan, color_red

REQUIRED_PYTHON_MODULES = ("zeroconf", "pexpect")


class ScriptedConfigureInputError(RuntimeError):
    pass


def non_negative_integer_arg(value: str) -> str:
    if not value.isdigit():
        raise argparse.ArgumentTypeError("must be a non-negative integer")
    return str(int(value))


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


def list_devices(records: Sequence[BonjourResolvedService]) -> None:
    print("Found devices:")
    for i, record in enumerate(records, start=1):
        pref = record.display_host() or "-"
        ipv4 = ",".join(record.ipv4) if record.ipv4 else "-"
        ipv6 = ",".join(record.ipv6) if record.ipv6 else "-"
        print(f"  {i}. {record.name} | host: {pref} | IPv4: {ipv4} | IPv6: {ipv6}")


def choose_device(records: Sequence[BonjourResolvedService]) -> Optional[BonjourResolvedService]:
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


def discover_default_record(
    existing: dict[str, str],
    *,
    on_diagnostics: Callable[[BonjourMergedDiscoveryDiagnostics], None] | None = None,
) -> Optional[BonjourResolvedService]:
    print("Attempting to discover Time Capsule/Airport Extreme devices on the local network via mDNS...", flush=True)
    snapshot, diagnostics = discover_snapshot_merged_detailed(AIRPORT_SERVICE, timeout=DEFAULT_BROWSE_TIMEOUT_SEC)
    if on_diagnostics is not None:
        on_diagnostics(diagnostics)
    records = snapshot.resolved
    if not records:
        print("No Time Capsule/Airport Extreme devices discovered. Falling back to manual SSH target entry.\n", flush=True)
        return None
    list_devices(records)
    selected = choose_device(records)
    if selected is None:
        existing_target = validated_value_or_empty(
            "TC_HOST",
            existing.get("TC_HOST", ""),
            "Device SSH target",
        ) or DEFAULTS["TC_HOST"]
        print(f"Discovery skipped. Falling back to {existing_target}.\n", flush=True)
        return None

    chosen_host = discovered_record_root_host(selected)
    selected_host = selected.display_host() or "manual SSH target required"
    print(f"Selected: {selected.name} ({selected_host})\n", flush=True)
    if chosen_host is None and discovered_record_has_only_link_local_ips(selected):
        print(
            "Selected device only advertised link-local addresses. "
            "Enter the device's LAN IP or LAN-resolving hostname manually.\n",
            flush=True,
        )
    return selected


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
    host_default = values.get("TC_HOST") or discovered_host or validated_value_or_empty(
        "TC_HOST",
        existing.get("TC_HOST", ""),
        "Device SSH target",
    ) or DEFAULTS["TC_HOST"]
    while True:
        candidate = prompt_valid_config_value("TC_HOST", "Device SSH target", host_default)
        try:
            return configure_service.configure_ssh_target(candidate, ssh_opts)
        except ValueError as exc:
            print(str(exc))
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


def _validate_config_value(key: str, label: str, value: str) -> str | None:
    validator = CONFIG_VALIDATORS.get(key)
    if validator is None:
        return None
    return validator(value, label)


def _scripted_ssh_target_value(
    existing: dict[str, str],
    *,
    host_arg: str | None,
    ssh_opts: str,
) -> tuple[str | None, str | None]:
    candidate = host_arg or validated_value_or_empty(
        "TC_HOST",
        existing.get("TC_HOST", ""),
        "Device SSH target",
    )
    if not candidate:
        return None, "configure --no-input requires --host or an existing valid TC_HOST in the config file."
    validation_error = _validate_config_value("TC_HOST", "Device SSH target", candidate)
    if validation_error is not None:
        return None, validation_error
    try:
        return configure_service.configure_ssh_target(candidate, ssh_opts), None
    except ValueError as exc:
        return None, str(exc)


def _scripted_password_value(existing: dict[str, str], args: argparse.Namespace) -> str:
    try:
        password = read_password_source_args(args)
    except ConfigError as exc:
        raise ScriptedConfigureInputError(str(exc)) from exc
    if password is None:
        password = existing.get("TC_PASSWORD", "")
    if not password:
        raise ScriptedConfigureInputError(
            "configure --no-input requires a device password from --password-env, "
            "--password-file, --password-stdin, or an existing TC_PASSWORD."
        )
    return password


def populate_scripted_host_and_password(
    existing: dict[str, str],
    values: dict[str, str],
    args: argparse.Namespace,
    ssh_opts: str,
) -> None:
    host, host_error = _scripted_ssh_target_value(existing, host_arg=args.host, ssh_opts=ssh_opts)
    if host_error is not None:
        raise ScriptedConfigureInputError(host_error)
    assert host is not None
    password = _scripted_password_value(existing, args)
    values["TC_HOST"] = host
    values["TC_PASSWORD"] = password


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


def print_automatic_value_choice(key: str, choice: ConfigureValueChoice) -> None:
    if choice.source == "saved":
        print(f"Using {key} from .env: {choice.value}")
    elif choice.source == "discovered":
        print(f"Using discovered {key}: {choice.value}")
    elif choice.source == "probed":
        print(f"Using probed {key}: {choice.value}")
    elif choice.source == "derived":
        print(f"Using {key} derived from TC_AIRPORT_SYAP: {choice.value}")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Create or update the local TimeCapsuleSMB .env configuration.")
    add_config_argument(parser)
    add_no_input_argument(parser)
    add_password_source_arguments(parser)
    parser.add_argument("--host", help="Device SSH target, for example root@192.168.1.10")
    parser.add_argument("--skip-discovery", action="store_true", help="Skip Bonjour discovery and use the supplied or saved SSH target")
    parser.add_argument("--yes", action="store_true", help="Approve enabling SSH via ACP when SSH is closed")
    ssh_group = parser.add_mutually_exclusive_group()
    ssh_group.add_argument("--enable-ssh", action="store_true", help="Enable SSH via ACP if SSH is closed")
    ssh_group.add_argument("--no-enable-ssh", action="store_true", help="Fail instead of enabling SSH via ACP if SSH is closed")
    parser.add_argument("--json", action="store_true", help="Output a machine-readable configure result")
    # Python 3.9 argparse can fail while formatting usage when a visible option
    # follows fully suppressed mutually exclusive groups. Keep this public flag
    # before the internal-only groups below.
    force_disable_smb_security_group = parser.add_mutually_exclusive_group()
    force_disable_smb_security_group.add_argument(
        "--disable-smb-security",
        dest="force_disable_smb_signing_and_encryption",
        action="store_true",
        help="Force the Samba server to disable SMB signing and encryption",
    )
    force_disable_smb_security_group.add_argument(
        "--no-disable-smb-security",
        dest="no_force_disable_smb_signing_and_encryption",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--internal-share-use-disk-root", action="store_true", help=argparse.SUPPRESS)
    smb_bind_group = parser.add_mutually_exclusive_group()
    smb_bind_group.add_argument("--smb-bind-lan-only", action="store_true", help=argparse.SUPPRESS)
    smb_bind_group.add_argument("--no-smb-bind-lan-only", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--smb-browse-compatibility", action="store_true", help=argparse.SUPPRESS)
    mdns_afp_group = parser.add_mutually_exclusive_group()
    mdns_afp_group.add_argument("--mdns-advertise-afp", action="store_true", help=argparse.SUPPRESS)
    mdns_afp_group.add_argument("--no-mdns-advertise-afp", action="store_true", help=argparse.SUPPRESS)
    any_protocol_group = parser.add_mutually_exclusive_group()
    any_protocol_group.add_argument("--any-protocol", action="store_true", help=argparse.SUPPRESS)
    any_protocol_group.add_argument("--no-any-protocol", action="store_true", help=argparse.SUPPRESS)
    require_smb_encryption_group = parser.add_mutually_exclusive_group()
    require_smb_encryption_group.add_argument("--require-smb-encryption", action="store_true", help=argparse.SUPPRESS)
    require_smb_encryption_group.add_argument("--no-require-smb-encryption", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--netatalk", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--ata-idle-seconds", type=non_negative_integer_arg, metavar="SECONDS", help=argparse.SUPPRESS)
    parser.add_argument("--ata-standby", type=non_negative_integer_arg, metavar="SECONDS", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.json and not no_input_enabled(args):
        parser.error("--json requires --no-input")

    ensure_install_id()
    env_path = resolve_app_paths(config_path=args.config).config_path
    env_exists = env_path.exists()
    existing = parse_env_file(env_path)
    any_protocol = (
        True if args.any_protocol
        else False if args.no_any_protocol
        else None
    )
    require_smb_encryption = (
        True if args.require_smb_encryption
        else False if args.no_require_smb_encryption
        else None
    )
    force_disable_smb_signing_and_encryption = (
        True if args.force_disable_smb_signing_and_encryption
        else False if args.no_force_disable_smb_signing_and_encryption
        else None
    )
    try:
        validate_smb_protocol_options(
            any_protocol=(
                parse_bool(existing.get("TC_ANY_PROTOCOL", DEFAULTS["TC_ANY_PROTOCOL"]))
                if any_protocol is None
                else any_protocol
            ),
            require_smb_encryption=(
                parse_bool(existing.get("TC_REQUIRE_SMB_ENCRYPTION", DEFAULTS["TC_REQUIRE_SMB_ENCRYPTION"]))
                if require_smb_encryption is None
                else require_smb_encryption
            ),
            force_disable_smb_signing_and_encryption=(
                parse_bool(
                    existing.get(
                        "TC_FORCE_DISABLE_SMB_SIGNING_AND_ENCRYPTION",
                        DEFAULTS["TC_FORCE_DISABLE_SMB_SIGNING_AND_ENCRYPTION"],
                    )
                )
                if force_disable_smb_signing_and_encryption is None
                else force_disable_smb_signing_and_encryption
            ),
        )
    except ValueError as exc:
        parser.error(str(exc))
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

        def fail_configure(
            message: str,
            *,
            code: str | None = None,
            debug: str | None = None,
        ) -> int:
            if args.json:
                payload = {
                    "ok": False,
                    "configure_id": configure_id,
                    "path": str(env_path),
                    "error": message,
                }
                if code is not None:
                    payload["code"] = code
                print_json(payload)
            else:
                print(message)
            if code is not None:
                command_context.add_debug_fields(configure_error_code=code)
            if debug:
                command_context.add_debug_fields(configure_error_debug=debug)
            command_context.fail_with_error(message)
            return 1

        def progress(message: str = "") -> None:
            print(message, file=sys.stderr if args.json else sys.stdout, flush=True)

        command_context.set_stage("dependency_check")
        missing_module = missing_required_python_module(REQUIRED_PYTHON_MODULES)
        if missing_module is not None:
            module_name, error = missing_module
            message = missing_dependency_message(module_name, error)
            return fail_configure(message)

        command_context.set_stage("startup")
        if not args.json:
            print("This writes a local .env configuration file in this folder. The other tcapsule commands use that file.")
            print(f"Writing {env_path}")
            print(f"Press Enter to accept the [{color_cyan('saved/suggested/default')}] value.")
            print("Most users can just keep the suggested values.\n")

        ssh_opts = existing.get("TC_SSH_OPTS", DEFAULTS["TC_SSH_OPTS"])
        values.update(
            build_configure_env_values(
                existing,
                host="",
                password="",
                ssh_opts=ssh_opts,
                configure_id=configure_id,
            )
        )
        values.pop("TC_HOST", None)
        values.pop("TC_PASSWORD", None)
        if args.host:
            values["TC_HOST"] = args.host
        if not no_input_enabled(args):
            try:
                password_arg = read_password_source_args(args)
            except ConfigError as exc:
                return fail_configure(str(exc))
            if password_arg is not None:
                values["TC_PASSWORD"] = password_arg
        command_context.set_stage("bonjour_discovery")
        if args.skip_discovery or args.host or no_input_enabled(args):
            discovered_record = None
            command_context.add_debug_fields(bonjour_discovery_skipped=True)
            if args.skip_discovery and not args.json:
                print("Skipping mDNS discovery.\n")
        else:
            try:
                discovered_record = discover_default_record(
                    existing,
                    on_diagnostics=lambda diagnostics: command_context.add_debug_fields(
                        bonjour_discovery=diagnostics,
                    ),
                )
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
            command_context.add_debug_fields(discovered_airport_syap=discovered_record.properties.get("syAP") or None)
        command_context.set_stage("prompt_host_password")
        if no_input_enabled(args):
            try:
                populate_scripted_host_and_password(existing, values, args, ssh_opts)
            except ScriptedConfigureInputError as exc:
                return fail_configure(str(exc))
        else:
            prompt_host_and_password(existing, values, discovered_host, ssh_opts)

        class ConfigureRetry(Exception):
            pass

        def apply_probe_to_context(connection: SshConnection, probed_state: ProbedDeviceState) -> None:
            command_context.connection = connection
            command_context.probe_state = probed_state
            command_context.compatibility = probed_state.compatibility

        def probe_for_context(connection: SshConnection) -> ProbedDeviceState:
            probed_state = probe_connection_state(connection)
            apply_probe_to_context(connection, probed_state)
            return probed_state

        def before_enable_ssh(_connection: SshConnection, _probed_state: ProbedDeviceState) -> None:
            if args.no_enable_ssh:
                raise configure_service.ConfigureFlowError(
                    "SSH is not reachable and --no-enable-ssh was provided.",
                    code="ssh_unreachable",
                )
            if no_input_enabled(args) and not args.enable_ssh:
                raise configure_service.ConfigureFlowError(
                    "SSH is not reachable. In non-interactive mode, use --enable-ssh --yes to enable SSH via ACP.",
                    code="ssh_unreachable",
                )
            if no_input_enabled(args) and not args.yes:
                raise configure_service.ConfigureFlowError(
                    "configure --enable-ssh in non-interactive mode requires --yes.",
                    code="ssh_unreachable",
                )

        def save_without_authentication(probed_state: ProbedDeviceState) -> bool:
            if no_input_enabled(args):
                return False
            print("\nThe provided AirPort SSH target and password did not work.")
            if probed_state.probe_result.ssh_port_reachable:
                command_context.update_fields(ssh_final_reachable=True)
            if confirm("Save this information still?", True):
                command_context.add_debug_fields(configure_saved_without_ssh_authentication=True)
                return True
            print("Please enter the SSH target and password again.\n")
            command_context.add_debug_fields(configure_retry_reason="ssh_authentication_failed")
            command_context.set_stage("prompt_host_password")
            prompt_host_and_password(existing, values, discovered_host, ssh_opts)
            raise ConfigureRetry()

        while True:
            if not args.json:
                print("Checking login information...")
            explicit_host = values["TC_HOST"]
            if discovered_record is not None and discovered_host is not None and explicit_host == discovered_host:
                explicit_host = ""
            target = resolve_configure_target(
                explicit_host=explicit_host,
                selected_record=discovered_record,
                existing=existing,
                ssh_opts=ssh_opts,
            )
            command_context.add_debug_fields(configure_target_source=target.source)
            try:
                result = configure_service.run_configure_flow(
                    configure_service.ConfigureFlowRequest(
                        existing=existing,
                        env_path=env_path,
                        host=target.host,
                        password=values["TC_PASSWORD"],
                        ssh_opts=ssh_opts,
                        configure_id=configure_id,
                        persist_password=True,
                        discovered_airport_syap=target.discovered_airport_syap,
                        enable_ssh=True,
                        verbose_wait=not args.json,
                        internal_share_use_disk_root=True if args.internal_share_use_disk_root else None,
                        smb_bind_lan_only=(
                            True if args.smb_bind_lan_only
                            else False if args.no_smb_bind_lan_only
                            else None
                        ),
                        smb_browse_compatibility=True if args.smb_browse_compatibility else None,
                        mdns_advertise_afp=(
                            True if args.mdns_advertise_afp
                            else False if args.no_mdns_advertise_afp
                            else None
                        ),
                        any_protocol=any_protocol,
                        require_smb_encryption=require_smb_encryption,
                        force_disable_smb_signing_and_encryption=force_disable_smb_signing_and_encryption,
                        fruit_metadata_netatalk=True if args.netatalk else None,
                        ata_idle_seconds=args.ata_idle_seconds,
                        ata_standby=args.ata_standby,
                        probe=probe_for_context,
                        write_env=lambda path, output: write_configure_env_file(path, output, persist_password=True),
                        infer_model_from_syap=infer_mdns_device_model_from_airport_syap,
                    ),
                    callbacks=OperationCallbacks(
                        set_stage=command_context.set_stage,
                        add_debug_fields=command_context.add_debug_fields,
                        update_fields=command_context.update_fields,
                        log=progress,
                    ),
                    hooks=configure_service.ConfigureFlowHooks(
                        after_probe=apply_probe_to_context,
                        before_enable_ssh=before_enable_ssh,
                        save_without_authentication=save_without_authentication,
                    ),
                )
            except ConfigureRetry:
                continue
            except ACPError as exc:
                label = "Failed to enable SSH via ACP"
                message = f"{label}: {exc}"
                if not args.json:
                    print(color_red(f"{label}:"))
                    print(str(exc))
                return fail_configure(message)
            except configure_service.ConfigureFlowError as exc:
                if exc.code == "auth_failed":
                    if no_input_enabled(args):
                        return fail_configure(
                            str(exc),
                            code=exc.code,
                            debug=exc.debug,
                        )
                    print(f"\n{exc}")
                    if exc.debug:
                        print(exc.debug)
                    print("Please enter the SSH target and password again.\n")
                    command_context.set_stage("prompt_host_password")
                    prompt_host_and_password(existing, values, discovered_host, ssh_opts)
                    continue
                if exc.code == "unsupported_device":
                    raise SystemExit(str(exc))
                if exc.code == "ssh_enable_timeout":
                    return fail_configure(
                        "SSH did not open after enabling via ACP. Reboot the device, wait 5 minutes, and try configure again."
                    )
                return fail_configure(str(exc))
            values.clear()
            values.update(result.values)
            command_context.connection = result.connection
            command_context.probe_state = result.probe_state
            command_context.compatibility = result.compatibility
            if result.identity.syap is not None and not args.json:
                print_automatic_value_choice(
                    "TC_AIRPORT_SYAP",
                    ConfigureValueChoice(value=result.identity.syap, source=result.identity.syap_source or "probed"),
                )
            if result.identity.model is not None and not args.json:
                print_automatic_value_choice(
                    "TC_MDNS_DEVICE_MODEL",
                    ConfigureValueChoice(value=result.identity.model, source=result.identity.model_source or "probed"),
                )
            break
        if args.json:
            print_json({
                "ok": True,
                "configure_id": configure_id,
                "path": str(env_path),
                "host": values.get("TC_HOST"),
                "device_syap": result.identity.syap,
                "device_model": result.identity.model,
            })
        else:
            print(f"\nReview the .env file configuration: wrote {env_path}")
            print("Next steps:")
            print("- Deploy this configuration to your Time Capsule/Airport Extreme device, run:")
            print("    .venv/bin/tcapsule deploy")
        command_context.succeed()
        return 0
    return 1
