from __future__ import annotations

import argparse
import ipaddress
import json
from dataclasses import dataclass
from pathlib import Path

from timecapsulesmb.core.config import (
    DEFAULTS,
    AppConfig,
    ConfigError,
    extract_host,
    load_app_config,
    require_valid_app_config,
)
from timecapsulesmb.core.paths import resolve_app_paths
from timecapsulesmb.device.compat import (
    DeviceCompatibility,
    is_netbsd4_payload_family,
    render_compatibility_message,
    require_compatibility,
)
from timecapsulesmb.device.probe import (
    ProbedDeviceState,
    RemoteInterfaceCandidate,
    RemoteInterfaceCandidatesProbeResult,
    RemoteInterfaceProbeResult,
    probe_connection_state,
    probe_remote_interface_candidates_conn,
    probe_remote_interface_conn,
)
from timecapsulesmb.transport.ssh import SshConnection


class NonInteractivePromptError(RuntimeError):
    """Raised when a required confirmation cannot be read from stdin."""


@dataclass(frozen=True)
class ManagedTargetState:
    connection: SshConnection
    interface_probe: RemoteInterfaceProbeResult | None
    probe_state: ProbedDeviceState | None


def add_config_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to the TimeCapsuleSMB config file. Overrides TCAPSULE_CONFIG and the repo-local .env.",
    )


def config_path_from_args(args: argparse.Namespace) -> Path | None:
    return getattr(args, "config", None)


def json_text(data: object) -> str:
    return json.dumps(data, indent=2, sort_keys=True)


def print_json(data: object) -> None:
    print(json_text(data))


def write_json_file(path: Path, data: object) -> None:
    path.write_text(json_text(data) + "\n")


def _confirm_suffix(default: bool) -> str:
    return "[Y/n]" if default else "[y/N]"


def confirm(
    prompt_text: str,
    *,
    default: bool,
    eof_default: bool | None = None,
    interrupt_default: bool | None = None,
    noninteractive_message: str | None = None,
) -> bool:
    while True:
        try:
            answer = input(f"{prompt_text} {_confirm_suffix(default)}: ").strip().lower()
        except EOFError as exc:
            if eof_default is not None:
                return eof_default
            raise NonInteractivePromptError(noninteractive_message or "Confirmation requires interactive stdin.") from exc
        except KeyboardInterrupt:
            if interrupt_default is not None:
                print()
                return interrupt_default
            raise

        if not answer:
            return default
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False
        print("Please answer 'y' or 'n'.")


def load_config_from_args(
    args: argparse.Namespace,
    *,
    defaults: dict[str, str] | None = None,
) -> AppConfig:
    return load_env_config(env_path=config_path_from_args(args), defaults=defaults)


def load_env_config(*, env_path: Path | None = None, defaults: dict[str, str] | None = None) -> AppConfig:
    resolved_path = resolve_app_paths(config_path=env_path).config_path
    return load_app_config(resolved_path, defaults=defaults)


def load_optional_env_config(
    *,
    env_path: Path | None = None,
    defaults: dict[str, str] | None = None,
) -> AppConfig:
    try:
        resolved_path = resolve_app_paths(config_path=env_path).config_path
    except Exception:
        return AppConfig.missing(path=env_path or Path.cwd() / ".env")
    if not resolved_path.exists():
        return AppConfig.missing(path=resolved_path)
    try:
        return load_app_config(resolved_path, defaults=defaults)
    except OSError:
        return AppConfig.missing(path=resolved_path)


def resolve_ssh_credentials(
    config: AppConfig,
    *,
    allow_empty_password: bool = False,
) -> tuple[str, str]:
    host = config.require("TC_HOST")
    password = config.get("TC_PASSWORD")
    if not password and not allow_empty_password:
        import getpass
        password = getpass.getpass("Device root password: ")
    return host, password


def resolve_env_connection(
    config: AppConfig,
    *,
    required_keys: tuple[str, ...] = (),
    allow_empty_password: bool = False,
) -> SshConnection:
    for key in required_keys:
        config.require(key)
    host, password = resolve_ssh_credentials(config, allow_empty_password=allow_empty_password)
    return SshConnection(host=host, password=password, ssh_opts=config.get("TC_SSH_OPTS", DEFAULTS["TC_SSH_OPTS"]))


def inspect_managed_connection(
    connection: SshConnection,
    iface: str,
    *,
    include_probe: bool = False,
) -> ManagedTargetState:
    interface_probe = probe_remote_interface_conn(connection, iface)
    probe_state = probe_connection_state(connection) if include_probe else None
    return ManagedTargetState(connection=connection, interface_probe=interface_probe, probe_state=probe_state)


def _ipv4_literal(value: str) -> str | None:
    value = value.strip()
    try:
        parsed = ipaddress.ip_address(value)
    except ValueError:
        return None
    if parsed.version != 4:
        return None
    return str(parsed)


def _format_remote_interface(candidate: RemoteInterfaceCandidate) -> str:
    ipv4_text = "/".join(candidate.ipv4_addrs) if candidate.ipv4_addrs else "no IPv4"
    return f"{candidate.name}={ipv4_text}"


def _format_remote_interface_candidates(result: RemoteInterfaceCandidatesProbeResult) -> str:
    candidates = tuple(candidate for candidate in result.candidates if not candidate.loopback)
    if not candidates:
        return "Found remote interfaces: none."
    return "Found remote interfaces: " + ", ".join(_format_remote_interface(candidate) for candidate in candidates) + "."


def _invalid_interface_message(
    *,
    connection: SshConnection,
    detail: str,
) -> str:
    target_ip = _ipv4_literal(extract_host(connection.host))
    target_ips = (target_ip,) if target_ip is not None else ()
    candidates_probe = probe_remote_interface_candidates_conn(connection, target_ips=target_ips)
    return (
        "TC_NET_IFACE is invalid. Run the `configure` command again.\n"
        f"{detail}.\n"
        f"{_format_remote_interface_candidates(candidates_probe)}"
    )


def resolve_validated_managed_target(
    config: AppConfig,
    *,
    command_name: str,
    profile: str,
    include_probe: bool = False,
) -> ManagedTargetState:
    require_valid_app_config(config, profile=profile, command_name=command_name)
    connection = resolve_env_connection(config)
    if profile == "flash":
        return ManagedTargetState(connection=connection, interface_probe=None, probe_state=None)
    target = inspect_managed_connection(connection, config.require("TC_NET_IFACE"), include_probe=include_probe)
    if not target.interface_probe.exists:
        raise ConfigError(
            _invalid_interface_message(
                connection=connection,
                detail=target.interface_probe.detail,
            )
        )
    return target


def require_connection_compatibility(connection: SshConnection) -> DeviceCompatibility:
    state = probe_connection_state(connection)
    return require_compatibility(
        state.compatibility,
        fallback_error=state.probe_result.error or "Failed to determine remote device OS compatibility.",
    )


def require_supported_device_compatibility(
    command_context,
    *,
    allow_unsupported: bool = False,
    json_output: bool = False,
) -> tuple[DeviceCompatibility, str]:
    compatibility = command_context.require_compatibility()
    compatibility_message = render_compatibility_message(compatibility)
    if not compatibility.supported:
        if not allow_unsupported:
            raise SystemExit(compatibility_message)
        if not json_output:
            print(f"Warning: {compatibility_message}")
            print("Continuing because --allow-unsupported was provided.")
    elif not json_output:
        print(compatibility_message)
    return compatibility, compatibility_message


def require_netbsd4_device_compatibility(
    command_context,
    *,
    command_name: str,
    json_output: bool = False,
    unsupported_message: str | None = None,
) -> tuple[DeviceCompatibility, str]:
    compatibility, compatibility_message = require_supported_device_compatibility(
        command_context,
        json_output=json_output,
    )
    netbsd4_payload = is_netbsd4_payload_family(compatibility.payload_family)
    command_context.update_fields(
        compatibility_supported=compatibility.supported,
        netbsd4_payload=netbsd4_payload,
    )
    if not netbsd4_payload:
        message = unsupported_message or f"{command_name} is only supported for NetBSD4 AirPort storage devices."
        raise SystemExit(message)
    return compatibility, compatibility_message
