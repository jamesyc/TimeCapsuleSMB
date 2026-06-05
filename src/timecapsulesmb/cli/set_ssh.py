from __future__ import annotations

import argparse
from enum import Enum
from typing import Optional

from timecapsulesmb.cli.context import CommandContext
from timecapsulesmb.cli.flows import wait_for_device_up
from timecapsulesmb.cli.runtime import (
    LogCallback,
    add_config_argument,
    add_no_input_argument,
    add_no_wait_argument,
    confirm,
    emit_progress,
    no_input_enabled,
)
from timecapsulesmb.cli.util import color_red
from timecapsulesmb.core.config import ConfigError
from timecapsulesmb.core.net import endpoint_host
from timecapsulesmb.deploy.executor import remote_request_reboot
from timecapsulesmb.identity import ensure_install_id
from timecapsulesmb.services.acp_ssh import enable_ssh_with_port_preflight
from timecapsulesmb.services import runtime as runtime_service
from timecapsulesmb.services.callbacks import OperationCallbacks
from timecapsulesmb.services.runtime import load_env_config
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


class SetSshAction(Enum):
    ENABLE = "enable_ssh"
    ENABLE_NOOP = "enable_noop"
    DISABLE = "disable_ssh"
    DISABLE_NOOP = "disable_noop"
    PROMPT_DISABLE = "prompt_disable_ssh"


def select_set_ssh_action(*, explicit_enable: bool, explicit_disable: bool, ssh_open: bool) -> SetSshAction:
    if explicit_enable:
        return SetSshAction.ENABLE_NOOP if ssh_open else SetSshAction.ENABLE
    if explicit_disable:
        return SetSshAction.DISABLE if ssh_open else SetSshAction.DISABLE_NOOP
    return SetSshAction.PROMPT_DISABLE if ssh_open else SetSshAction.ENABLE


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
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--enable", action="store_true", help="Enable SSH via ACP if it is not already reachable")
    mode_group.add_argument("--disable", action="store_true", help="Disable SSH over SSH if it is currently reachable")
    mode_group.add_argument("--status", action="store_true", help="Report whether SSH is reachable without changing device state")
    parser.add_argument("--yes", action="store_true", help="Skip the legacy prompt when SSH is already enabled")
    add_no_input_argument(parser)
    add_no_wait_argument(parser)
    args = parser.parse_args(argv)

    if args.status and args.no_wait:
        parser.error("--no-wait is not valid with --status")

    ensure_install_id()
    config = load_env_config(env_path=args.config, defaults={})
    telemetry = TelemetryClient.from_config(config)
    with CommandContext(telemetry, "set-ssh", "set_ssh_started", "set_ssh_finished", config=config, args=args) as command_context:
        command_context.set_stage("load_config")
        try:
            command_context.require_valid_config(profile="set_ssh_status" if args.status else "set_ssh")
        except ConfigError as exc:
            message = str(exc) or f"Missing {config.path} settings. Run '.venv/bin/tcapsule configure' first."
            command_context.update_fields(set_ssh_action="missing_config")
            print(message)
            command_context.fail_with_error(message)
            return 1
        connection = None if args.status else command_context.resolve_env_connection()
        target_host = config.require("TC_HOST") if args.status else connection.host
        acp_host = endpoint_host(target_host)
        password = "" if connection is None else connection.password

        print(f"Using configured target from {config.path}: {target_host}")
        print(f"Probing SSH on {acp_host}:22 ...")
        command_context.set_stage("probe_ssh")
        ssh_open = tcp_open(acp_host, 22)
        command_context.update_fields(ssh_initially_reachable=ssh_open)

        if args.status:
            command_context.update_fields(set_ssh_action="status", ssh_final_reachable=ssh_open)
            print("SSH enabled." if ssh_open else "SSH disabled.")
            command_context.succeed()
            return 0

        assert connection is not None
        action = select_set_ssh_action(
            explicit_enable=args.enable,
            explicit_disable=args.disable,
            ssh_open=ssh_open,
        )

        if action is SetSshAction.ENABLE_NOOP:
            command_context.update_fields(set_ssh_action=action.value, ssh_final_reachable=True)
            print("SSH already enabled.")
            command_context.succeed()
            return 0

        if action is SetSshAction.ENABLE:
            command_context.update_fields(set_ssh_action=action.value)
            print("SSH not reachable. Attempting to enable via ACP...")
            try:
                enable_ssh_with_port_preflight(
                    acp_host,
                    password,
                    reboot_device=True,
                    callbacks=OperationCallbacks(
                        set_stage=command_context.set_stage,
                        log=print,
                        add_debug_fields=command_context.add_debug_fields,
                        update_fields=command_context.update_fields,
                    ),
                )
            except Exception as e:
                error_text = str(e)
                if command_context.debug_stage == "acp_identity_probe":
                    label = "Failed to read AirPort identity via ACP"
                else:
                    label = "Failed to enable SSH via ACP"
                message = f"{label}: {error_text}"
                print(color_red(f"{label}:"))
                print("\n".join(error_text.splitlines()))
                command_context.fail_with_error(message)
                return 1

            if args.no_wait:
                command_context.update_fields(ssh_verification_skipped=True)
                print("SSH enable requested; not waiting for SSH to open.")
                command_context.succeed()
                return 0

            command_context.set_stage("wait_for_ssh_enabled")
            if not runtime_service.wait_for_tcp_port_state(
                acp_host,
                22,
                expected_state=True,
                log=print,
                service_name="SSH port",
            ):
                command_context.update_fields(ssh_final_reachable=False)
                command_context.fail_with_error("SSH did not open after enabling via ACP.")
                return 1
            command_context.update_fields(ssh_final_reachable=True)

            print("SSH is configured. You can connect as 'root' using the AirPort admin password.")
            command_context.succeed()
            return 0

        if action is SetSshAction.DISABLE_NOOP:
            command_context.update_fields(set_ssh_action=action.value, ssh_final_reachable=False)
            print("SSH already disabled.")
            command_context.succeed()
            return 0

        if action is SetSshAction.PROMPT_DISABLE:
            command_context.set_stage("prompt_disable_ssh")
            if not args.yes and no_input_enabled(args):
                message = (
                    "Running `set-ssh` in non-interactive legacy mode requires `--yes` "
                    "to disable SSH, or an explicit `--enable`, `--disable`, or `--status` mode."
                )
                print(message)
                command_context.fail_with_error(message)
                return 1
            if not args.yes:
                confirmed = confirm(
                    "SSH already enabled. Disable?",
                    default=False,
                    eof_default=False,
                    interrupt_default=False,
                )
            else:
                confirmed = True
            if not confirmed:
                command_context.update_fields(set_ssh_action="leave_enabled", ssh_final_reachable=True)
                print("Leaving SSH enabled.")
                command_context.succeed()
                return 0
            action = SetSshAction.DISABLE

        command_context.update_fields(set_ssh_action=action.value)
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

        if args.no_wait:
            command_context.update_fields(ssh_verification_skipped=True)
            print("SSH disable requested; not waiting for reboot or verifying SSH stays closed.")
            command_context.succeed()
            return 0

        print("Device is starting reboot now, waiting for it to shut down...")
        command_context.set_stage("wait_for_ssh_down")
        if not runtime_service.wait_for_tcp_port_state(
            acp_host,
            22,
            expected_state=False,
            log=print,
            service_name="SSH port",
        ):
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
        if not runtime_service.wait_for_tcp_port_state(
            acp_host,
            22,
            expected_state=False,
            timeout_seconds=30,
            log=print,
            service_name="SSH port",
        ):
            command_context.update_fields(ssh_final_reachable=True, ssh_disable_persisted=False)
            message = "SSH reopened after reboot. Disable did not persist."
            print(color_red("Failed to verify SSH disable:"))
            print(message)
            command_context.fail_with_error(message)
            return 1
        command_context.update_fields(ssh_final_reachable=False, ssh_disable_persisted=True)
        print("SSH disabled (remains closed after reboot). Enable SSH again if this was not intended.")
        command_context.succeed()
        return 0
