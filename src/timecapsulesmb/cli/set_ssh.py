from __future__ import annotations

import argparse
from typing import Optional

from timecapsulesmb.cli.context import CommandContext
from timecapsulesmb.cli.runtime import (
    add_config_argument,
    add_no_input_argument,
    add_no_wait_argument,
    confirm,
    no_input_enabled,
)
from timecapsulesmb.cli.util import color_red
from timecapsulesmb.core.config import ConfigError
from timecapsulesmb.core.net import endpoint_host
from timecapsulesmb.identity import ensure_install_id
from timecapsulesmb.services.callbacks import OperationCallbacks
from timecapsulesmb.services.runtime import load_env_config
from timecapsulesmb.services.set_ssh import (
    SetSshAction,
    SetSshResult,
    SetSshStatusResult,
    SetSshVerificationError,
    disable_set_ssh,
    disable_ssh_over_ssh,
    enable_set_ssh,
    probe_set_ssh_status,
    select_set_ssh_action,
)
from timecapsulesmb.telemetry import TelemetryClient
from timecapsulesmb.transport.local import tcp_open


def _tcp_connect_error_from_tcp_open(host: str, port: int, _timeout: float) -> str | None:
    return None if tcp_open(host, port) else "Connection refused"


def _update_fields_from_status(command_context: CommandContext, status: SetSshStatusResult) -> None:
    command_context.update_fields(
        acp_port_reachable=status.acp_port_reachable,
        ssh_port_reachable=status.ssh_port_reachable,
        ssh_disabled_likely=status.ssh_disabled_likely,
        ssh_initially_reachable=status.ssh_port_reachable,
    )


def _update_fields_from_result(command_context: CommandContext, result: SetSshResult) -> None:
    command_context.update_fields(
        set_ssh_action=result.action,
        ssh_final_reachable=result.ssh_final_reachable,
        ssh_verification_skipped=result.ssh_verification_skipped,
        ssh_disable_persisted=result.ssh_disable_persisted,
        ssh_reboot_observed_down=result.ssh_reboot_observed_down,
        device_recovered=result.device_recovered,
    )


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

        print(f"Using configured target from {config.path}: {target_host}")
        print(f"Probing SSH on {acp_host}:22 ...")
        command_context.set_stage("probe_ssh")
        status = probe_set_ssh_status(
            target_host,
            tcp_connect_error_func=_tcp_connect_error_from_tcp_open,
        )
        ssh_open = status.ssh_port_reachable
        _update_fields_from_status(command_context, status)

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
                result = enable_set_ssh(
                    connection,
                    no_wait=args.no_wait,
                    callbacks=_callbacks(command_context),
                    initial=status,
                )
            except Exception as e:
                error_text = str(e)
                label = "Failed to enable SSH via ACP"
                message = f"{label}: {error_text}"
                print(color_red(f"{label}:"))
                print("\n".join(error_text.splitlines()))
                command_context.fail_with_error(message)
                return 1

            _update_fields_from_result(command_context, result)
            if args.no_wait:
                print(result.summary)
            else:
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
            result = disable_set_ssh(
                connection,
                no_wait=args.no_wait,
                callbacks=_callbacks(command_context),
                initial=status,
            )
        except SetSshVerificationError as e:
            error_text = str(e)
            print(color_red("Failed to verify SSH disable:"))
            print(error_text)
            command_context.fail_with_error(error_text)
            return 1
        except Exception as e:
            error_text = str(e)
            message = f"Failed to disable SSH over SSH: {error_text}"
            print(color_red("Failed to disable SSH over SSH:"))
            print(error_text)
            command_context.fail_with_error(message)
            return 1

        _update_fields_from_result(command_context, result)
        if args.no_wait:
            print(result.summary)
        command_context.succeed()
        return 0


def _callbacks(command_context: CommandContext) -> OperationCallbacks:
    return OperationCallbacks(
        set_stage=command_context.set_stage,
        log=print,
        add_debug_fields=command_context.add_debug_fields,
        update_fields=command_context.update_fields,
    )
