from __future__ import annotations

import argparse
from typing import Optional

from timecapsulesmb.cli.context import CommandContext
from timecapsulesmb.cli.flows import wait_for_device_up, wait_for_tcp_port_state
from timecapsulesmb.cli.runtime import LogCallback, add_config_argument, confirm, emit_progress, load_env_config
from timecapsulesmb.cli.util import color_red
from timecapsulesmb.core.config import ConfigError, extract_host
from timecapsulesmb.deploy.executor import remote_request_reboot
from timecapsulesmb.identity import ensure_install_id
from timecapsulesmb.integrations.acp import enable_ssh
from timecapsulesmb.telemetry import TelemetryClient
from timecapsulesmb.transport.ssh import SshCommandTimeout, SshConnection, run_ssh
from timecapsulesmb.transport.local import tcp_open


def _dbug_property_already_absent(output: str) -> bool:
    # Verified on NetBSD 6 and NetBSD 4 Time Capsules: removing an absent
    # dbug property returns rc=22 and this message.
    return "remove property error: -10" in output


def _looks_like_ssh_auth_failure(output: str) -> bool:
    lowered = output.lower()
    return "permission denied" in lowered or "please try again" in lowered


def disable_ssh_over_ssh(
    connection: SshConnection,
    *,
    reboot_device: bool = True,
    log: LogCallback = None,
) -> None:
    cmds = [
        "acp remove dbug",
        "/usr/sbin/acp remove dbug",
        "/usr/bin/acp remove dbug",
    ]
    last_err: tuple[int, str] | None = None
    for command in cmds:
        proc = run_ssh(connection, command, check=False, timeout=30)
        rc = proc.returncode
        out = proc.stdout or ""
        if rc == 0:
            emit_progress(log, f"Removed 'dbug' via: {command}")
            break
        if _dbug_property_already_absent(out):
            emit_progress(log, f"SSH debug flag 'dbug' already absent via: {command}")
            break
        if _looks_like_ssh_auth_failure(out):
            raise RuntimeError("SSH authentication failed while trying to disable SSH over SSH.")
        last_err = (rc, out)
    else:
        code, out = last_err or (1, "unknown error")
        raise RuntimeError(f"Failed to remove 'dbug' via on-device acp (rc={code}). Output: {out}")

    if reboot_device:
        try:
            remote_request_reboot(connection)
        except SshCommandTimeout as exc:
            emit_progress(log, f"Reboot request timed out; continuing to observe whether the device is rebooting... ({exc})")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Use the configured device target from .env to enable SSH via ACP or disable SSH over SSH.")
    add_config_argument(parser)
    args = parser.parse_args(argv)

    ensure_install_id()
    config = load_env_config(env_path=args.config, defaults={})
    telemetry = TelemetryClient.from_config(config)
    with CommandContext(telemetry, "set-ssh", "set_ssh_started", "set_ssh_finished", config=config, args=args) as command_context:
        command_context.set_stage("load_config")
        try:
            command_context.require_valid_config(profile="set_ssh")
        except ConfigError as exc:
            message = str(exc) or f"Missing {config.path} settings. Run '.venv/bin/tcapsule configure' first."
            command_context.update_fields(set_ssh_action="missing_config")
            print(message)
            command_context.fail_with_error(message)
            return 1
        connection = command_context.resolve_env_connection()
        acp_host = extract_host(connection.host)
        password = connection.password

        print(f"Using configured target from {config.path}: {connection.host}")
        print(f"Probing SSH on {acp_host}:22 ...")
        command_context.set_stage("probe_ssh")
        ssh_open = tcp_open(acp_host, 22)
        command_context.update_fields(ssh_initially_reachable=ssh_open)
        if not ssh_open:
            command_context.update_fields(set_ssh_action="enable_ssh")
            print("SSH not reachable. Attempting to enable via ACP...")
            try:
                command_context.set_stage("enable_ssh")
                enable_ssh(acp_host, password, reboot_device=True, log=print)
            except Exception as e:
                error_text = str(e)
                message = f"Failed to enable SSH via ACP: {error_text}"
                print(color_red("Failed to enable SSH via ACP:"))
                print("\n".join(error_text.splitlines()))
                command_context.fail_with_error(message)
                return 1

            command_context.set_stage("wait_for_ssh_enabled")
            if not wait_for_tcp_port_state(acp_host, 22, expected_state=True, service_name="SSH port"):
                command_context.update_fields(ssh_final_reachable=False)
                command_context.fail_with_error("SSH did not open after enabling via ACP.")
                return 1
            command_context.update_fields(ssh_final_reachable=True)
        else:
            command_context.set_stage("prompt_disable_ssh")
            should_disable = confirm(
                "SSH already enabled. Disable?",
                default=False,
                eof_default=False,
                interrupt_default=False,
            )
            if not should_disable:
                command_context.update_fields(set_ssh_action="leave_enabled", ssh_final_reachable=True)
                print("Leaving SSH enabled.")

            if should_disable:
                command_context.update_fields(set_ssh_action="disable_ssh")
                try:
                    command_context.set_stage("disable_ssh")
                    disable_ssh_over_ssh(connection, reboot_device=True, log=print)
                except Exception as e:
                    error_text = str(e)
                    message = f"Failed to disable SSH over SSH: {error_text}"
                    print(color_red("Failed to disable SSH over SSH:"))
                    print(error_text)
                    command_context.fail_with_error(message)
                    return 1

                print("Device is starting reboot now, waiting for it to shut down...")
                command_context.set_stage("wait_for_ssh_down")
                if not wait_for_tcp_port_state(acp_host, 22, expected_state=False, service_name="SSH port"):
                    message = "SSH did not close after disable/reboot request; disable could not be verified."
                    command_context.update_fields(
                        ssh_final_reachable=True,
                        ssh_disable_persisted=False,
                        ssh_reboot_observed_down=False,
                    )
                    print(color_red("Failed to verify SSH disable:"))
                    print(message)
                    command_context.fail_with_error(message)
                    return 1
                print("Device is down now, verifying persistence after reboot...")
                command_context.update_fields(ssh_reboot_observed_down=True)
                command_context.set_stage("wait_for_device_up")
                if not wait_for_device_up(acp_host):
                    message = "Device went down after disable request but did not come back within timeout."
                    command_context.update_fields(device_recovered=False)
                    print(color_red("Failed to verify SSH disable:"))
                    print(message)
                    command_context.fail_with_error(message)
                    return 1
                command_context.update_fields(device_recovered=True)
                print("Device successfully rebooted. Checking if SSH is still disabled...")
                command_context.set_stage("verify_ssh_disabled")
                if not wait_for_tcp_port_state(acp_host, 22, expected_state=False, timeout_seconds=30, service_name="SSH port"):
                    command_context.update_fields(ssh_final_reachable=True, ssh_disable_persisted=False)
                    message = "SSH reopened after reboot. Disable did not persist."
                    print(color_red("Failed to verify SSH disable:"))
                    print(message)
                    command_context.fail_with_error(message)
                    return 1
                else:
                    command_context.update_fields(ssh_final_reachable=False, ssh_disable_persisted=True)
                    print("SSH disabled (remains closed after reboot). Enable SSH again if this was not intended.")
                    command_context.succeed()
                    return 0

        print("SSH is configured. You can connect as 'root' using the AirPort admin password.")
        command_context.succeed()
        return 0
    return 1
