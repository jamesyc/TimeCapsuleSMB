from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from timecapsulesmb.core.config import (
    DEFAULTS,
    AppConfig,
    ConfigError,
    load_app_config,
    require_valid_app_config,
)
from timecapsulesmb.core.net import extract_host, ipv4_literal, is_link_local_ipv4, resolve_host_ipv4s
from timecapsulesmb.core.paths import resolve_app_paths
from timecapsulesmb.device.compat import (
    DeviceCompatibility,
    is_netbsd4_payload_family,
    render_compatibility_message,
)
from timecapsulesmb.device.probe import (
    ProbedDeviceState,
    RemoteInterfaceProbeResult,
    probe_connection_state,
    probe_remote_interface_conn,
    read_interface_ipv4_addrs_conn,
)
from timecapsulesmb.deploy.planner import DEFAULT_APPLE_MOUNT_WAIT_SECONDS
from timecapsulesmb.services import runtime as service_runtime
from timecapsulesmb.transport.ssh import SshConnection, ssh_opts_use_proxy


LogCallback = Optional[Callable[[str], None]]


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


def add_no_input_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--no-input",
        action="store_true",
        help="Do not prompt; fail if required input or confirmation is missing",
    )


def no_input_enabled(args: argparse.Namespace | object) -> bool:
    return bool(getattr(args, "no_input", False))


def add_password_source_arguments(parser: argparse.ArgumentParser) -> None:
    password_group = parser.add_mutually_exclusive_group()
    password_group.add_argument(
        "--password-env",
        metavar="NAME",
        help="Read the device password from environment variable NAME",
    )
    password_group.add_argument(
        "--password-file",
        type=Path,
        metavar="PATH",
        help="Read the device password from PATH",
    )
    password_group.add_argument(
        "--password-stdin",
        action="store_true",
        help="Read the device password from stdin",
    )


def read_password_source_args(args: argparse.Namespace) -> str | None:
    env_name = getattr(args, "password_env", None)
    if env_name:
        if env_name not in os.environ:
            raise ConfigError(f"Password environment variable is not set: {env_name}")
        return os.environ[env_name]

    password_file = getattr(args, "password_file", None)
    if password_file is not None:
        try:
            return Path(password_file).read_text(encoding="utf-8").rstrip("\r\n")
        except OSError as exc:
            raise ConfigError(f"Failed to read password file {password_file}: {exc}") from exc

    if getattr(args, "password_stdin", False):
        return sys.stdin.read().rstrip("\r\n")

    return None


def non_negative_int_arg(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be 0 or greater")
    return parsed


def add_mount_wait_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--mount-wait",
        type=non_negative_int_arg,
        default=DEFAULT_APPLE_MOUNT_WAIT_SECONDS,
        metavar="SECONDS",
        help=f"Seconds for diskd.useVolume mount guards to wait before their manual fallback (default: {DEFAULT_APPLE_MOUNT_WAIT_SECONDS})",
    )


def add_no_wait_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--no-wait", action="store_true", help="Do not wait for the device to go down and come back after reboot")


def config_path_from_args(args: argparse.Namespace) -> Path | None:
    return getattr(args, "config", None)


def json_text(data: object) -> str:
    return json.dumps(data, indent=2, sort_keys=True)


def print_json(data: object) -> None:
    print(json_text(data))


def write_json_file(path: Path, data: object) -> None:
    path.write_text(json_text(data) + "\n")


def prefixed_logger(prefix: str, *, enabled: bool) -> LogCallback:
    if not enabled:
        return None

    def emit(message: str) -> None:
        print(f"[{prefix}] {message}", flush=True)

    return emit


def emit_progress(log: LogCallback, message: str) -> None:
    if log is not None:
        log(message)


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


def load_env_config(*, env_path: Path | None = None, defaults: dict[str, str] | None = None) -> AppConfig:
    return service_runtime.load_env_config(env_path=env_path, defaults=defaults, resolve_paths=resolve_app_paths)


def load_optional_env_config(
    *,
    env_path: Path | None = None,
    defaults: dict[str, str] | None = None,
) -> AppConfig:
    return service_runtime.load_optional_env_config(env_path=env_path, defaults=defaults, resolve_paths=resolve_app_paths)


def resolve_ssh_credentials(
    config: AppConfig,
    *,
    allow_empty_password: bool = False,
    allow_password_prompt: bool = True,
) -> tuple[str, str]:
    return service_runtime.resolve_ssh_credentials(
        config,
        allow_empty_password=allow_empty_password,
        allow_password_prompt=allow_password_prompt,
    )


def resolve_env_connection(
    config: AppConfig,
    *,
    required_keys: tuple[str, ...] = (),
    allow_empty_password: bool = False,
    allow_password_prompt: bool = True,
) -> SshConnection:
    return service_runtime.resolve_env_connection(
        config,
        required_keys=required_keys,
        allow_empty_password=allow_empty_password,
        allow_password_prompt=allow_password_prompt,
    )


def inspect_managed_connection(
    connection: SshConnection,
    iface: str,
    *,
    include_probe: bool = False,
) -> ManagedTargetState:
    return service_runtime.inspect_managed_connection(connection, iface, include_probe=include_probe)


def ssh_target_link_local_resolution_error(
    target: str,
    ssh_opts: str,
    *,
    field_name: str = "Device SSH target",
) -> str | None:
    return service_runtime.ssh_target_link_local_resolution_error(target, ssh_opts, field_name=field_name)


def resolve_validated_managed_target(
    config: AppConfig,
    *,
    command_name: str,
    profile: str,
    include_probe: bool = False,
    allow_password_prompt: bool = True,
) -> ManagedTargetState:
    return service_runtime.resolve_validated_managed_target(
        config,
        command_name=command_name,
        profile=profile,
        include_probe=include_probe,
        allow_password_prompt=allow_password_prompt,
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
