from __future__ import annotations

import argparse
import time
from typing import Iterable, Optional

from timecapsulesmb.cli.context import CommandContext
from timecapsulesmb.cli.util import color_red
from timecapsulesmb.core.config import ENV_PATH, extract_host, parse_env_values
from timecapsulesmb.identity import ensure_install_id
from timecapsulesmb.integrations.airpyrt import AIRPYRT_NOT_FOUND_ERROR, disable_ssh, enable_ssh
from timecapsulesmb.telemetry import TelemetryClient
from timecapsulesmb.transport.local import tcp_open


AIRPYRT_CLI_INSTALL_GUIDANCE = (
    color_red(AIRPYRT_NOT_FOUND_ERROR),
    "In order to run prep-device to enable SSH on the device, AirPyrt must be installed.",
    color_red("To automatically install AirPyrt, run:"),
    color_red("  ./tcapsule bootstrap"),
    "Or you can manually enable SSH on your device with any other method.",
    "To manually install AirPyrt, see https://github.com/samuelthomas2774/airport/wiki/AirPyrt#installation and make sure 'acp' is on PATH or set AIRPYRT_PY to that interpreter.",
)


def render_airpyrt_error_for_cli(error_text: str) -> str:
    if error_text.strip() == AIRPYRT_NOT_FOUND_ERROR:
        return "\n".join(AIRPYRT_CLI_INSTALL_GUIDANCE)
    return "\n".join(error_text.splitlines())


def print_airpyrt_failure_for_cli(prefix: str, error_text: str) -> None:
    print(color_red(f"{prefix}:"))
    print(render_airpyrt_error_for_cli(error_text))


def wait_for_ssh(
    host: str,
    *,
    expected_state: bool,
    timeout_seconds: int = 120,
    interval_seconds: int = 5,
    verbose: bool = True,
) -> bool:
    expected_state_string = "open" if expected_state else "closed"
    if verbose:
        print(f"Waiting for SSH port to be {expected_state_string}...")
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        time.sleep(interval_seconds)
        is_open = tcp_open(host, 22)
        if (is_open and expected_state) or (not is_open and not expected_state):
            if verbose:
                print(f"SSH is {expected_state_string}.")
            return True
    if verbose:
        print(f"SSH did not {expected_state_string} within {timeout_seconds}s.")
    return False


def wait_for_device_up(
    host: str,
    *,
    probe_ports: Iterable[int] = (5009, 445, 139),
    timeout_seconds: int = 180,
    interval_seconds: int = 5,
) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        time.sleep(interval_seconds)
        if any(tcp_open(host, port) for port in probe_ports):
            return True
    return False


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Use the configured device target from .env to enable SSH via AirPyrt or disable SSH over SSH.")
    args = parser.parse_args(argv)

    ensure_install_id()
    values = parse_env_values(ENV_PATH, defaults={})
    telemetry = TelemetryClient.from_values(values)
    with CommandContext(telemetry, "prep-device", "prep_device_started", "prep_device_finished", values=values, args=args) as command_context:
        command_context.set_stage("load_config")
        host_target = values.get("TC_HOST", "")
        password = values.get("TC_PASSWORD", "")
        if not host_target or not password:
            message = f"Missing {ENV_PATH} settings. Run '.venv/bin/tcapsule configure' first."
            command_context.update_fields(prep_device_action="missing_config")
            print(message)
            command_context.fail_with_error(message)
            return 1
        connection = command_context.resolve_env_connection()
        airpyrt_host = extract_host(connection.host)
        password = connection.password

        print(f"Using configured target from {ENV_PATH}: {connection.host}")
        print(f"Probing SSH on {airpyrt_host}:22 ...")
        command_context.set_stage("probe_ssh")
        ssh_open = tcp_open(airpyrt_host, 22)
        command_context.update_fields(ssh_initially_reachable=ssh_open)
        if not ssh_open:
            command_context.update_fields(prep_device_action="enable_ssh")
            print("SSH not reachable. Attempting to enable via AirPyrt...")
            try:
                command_context.set_stage("enable_ssh")
                enable_ssh(airpyrt_host, password, reboot_device=True, log=print)
            except Exception as e:
                error_text = str(e)
                message = f"Failed to enable SSH via AirPyrt: {error_text}"
                print_airpyrt_failure_for_cli("Failed to enable SSH via AirPyrt", error_text)
                command_context.fail_with_error(message)
                return 1

            command_context.set_stage("wait_for_ssh_enabled")
            if not wait_for_ssh(airpyrt_host, expected_state=True):
                command_context.update_fields(ssh_final_reachable=False)
                command_context.fail_with_error("SSH did not open after enabling via AirPyrt.")
                return 1
            command_context.update_fields(ssh_final_reachable=True)
        else:
            command_context.set_stage("prompt_disable_ssh")
            should_disable = False
            while True:
                try:
                    resp = input("SSH already enabled. Disable? [y/N]: ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    print()
                    resp = ""
                if resp in {"", "n", "no"}:
                    command_context.update_fields(prep_device_action="leave_enabled", ssh_final_reachable=True)
                    print("Leaving SSH enabled.")
                    break
                if resp in {"y", "yes"}:
                    should_disable = True
                    break
                print("Please answer 'y' or 'n'.")

            if should_disable:
                command_context.update_fields(prep_device_action="disable_ssh")
                try:
                    command_context.set_stage("disable_ssh")
                    disable_ssh(connection, reboot_device=True, log=print)
                except Exception as e:
                    error_text = str(e)
                    message = f"Failed to disable SSH over SSH: {error_text}"
                    print(color_red("Failed to disable SSH over SSH:"))
                    print(error_text)
                    command_context.fail_with_error(message)
                    return 1

                print("Device is starting reboot now, waiting for it to shut down...")
                command_context.set_stage("wait_for_ssh_down")
                if not wait_for_ssh(airpyrt_host, expected_state=False):
                    command_context.succeed()
                    return 0
                print("Device is down now, verifying persistence after reboot...")
                command_context.set_stage("wait_for_device_up")
                wait_for_device_up(airpyrt_host)
                print("Device successfully rebooted. Checking if SSH is still disabled...")
                command_context.set_stage("verify_ssh_disabled")
                if not wait_for_ssh(airpyrt_host, expected_state=False, timeout_seconds=30):
                    command_context.update_fields(ssh_final_reachable=True, ssh_disable_persisted=False)
                    print("Warning: SSH reopened after reboot. Disable may not have persisted.")
                else:
                    command_context.update_fields(ssh_final_reachable=False, ssh_disable_persisted=True)
                    print("SSH disabled (remains closed after reboot). Enable SSH again if this was not intended.")
                    command_context.succeed()
                    return 0

        print("SSH is configured. You can connect as 'root' using the AirPort admin password.")
        command_context.succeed()
        return 0
    return 1
