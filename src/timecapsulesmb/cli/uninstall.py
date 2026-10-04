from __future__ import annotations

import argparse
from typing import Optional

from timecapsulesmb.cli.context import CommandContext
from timecapsulesmb.cli.runtime import (
    add_config_argument,
    add_mount_wait_argument,
    add_no_input_argument,
    add_no_wait_argument,
    no_input_enabled,
    print_json,
)
from timecapsulesmb.deploy.dry_run import format_uninstall_plan, uninstall_plan_to_jsonable
from timecapsulesmb.deploy.executor import remote_uninstall_payload
from timecapsulesmb.device.errors import DeviceError
from timecapsulesmb.identity import ensure_install_id
from timecapsulesmb.services.maintenance import prepare_uninstall, reboot_after_uninstall
from timecapsulesmb.services.reboot import RebootFlowError
from timecapsulesmb.services.runtime import load_env_config
from timecapsulesmb.telemetry import TelemetryClient


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Remove the managed TimeCapsuleSMB payload from the configured device.")
    add_config_argument(parser)
    add_mount_wait_argument(parser)
    add_no_wait_argument(parser)
    parser.add_argument("--yes", action="store_true", help="Do not prompt before reboot")
    add_no_input_argument(parser)
    parser.add_argument("--no-reboot", action="store_true", help="Remove files but do not reboot the device")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without making changes")
    parser.add_argument("--json", action="store_true", help="Output the dry-run uninstall plan as JSON")
    args = parser.parse_args(argv)

    if args.json and not args.dry_run:
        parser.error("--json currently requires --dry-run")

    if not args.json:
        print("Uninstalling...")

    ensure_install_id()
    config = load_env_config(env_path=args.config)
    telemetry = TelemetryClient.from_config(config)
    with CommandContext(telemetry, "uninstall", "uninstall_started", "uninstall_finished", config=config, args=args) as command_context:
        command_context.update_fields(
            reboot_was_attempted=False,
            device_came_back_after_reboot=False,
            post_uninstall_verified=False,
        )
        command_context.set_stage("validate_config")
        command_context.require_valid_config(profile="uninstall")
        if no_input_enabled(args) and not args.yes and not args.no_reboot and not args.dry_run:
            command_context.set_stage("noninteractive_confirmation")
            message = (
                "Running `uninstall` with reboot in non-interactive mode requires `--yes` "
                "to approve the reboot or `--no-reboot` to avoid it."
            )
            print(message)
            command_context.fail_with_error(message)
            return 1
        command_context.set_stage("resolve_connection")
        # Key-only SSH can remove the files, but the reboot goes through AirPort
        # ACP, which needs the password: ask before anything is removed.
        connection = command_context.resolve_env_connection(allow_empty_password=args.no_reboot or args.dry_run)
        if connection.password:
            command_context.start_optional_airport_identity_probe(connection)

        plan = prepare_uninstall(
            connection,
            dry_run=args.dry_run,
            reboot=not args.no_reboot,
            wait=not args.no_wait,
            mount_wait=args.mount_wait,
            callbacks=command_context.to_operation_callbacks(),
        )

        if args.dry_run:
            if args.json:
                print_json(uninstall_plan_to_jsonable(plan))
            else:
                print(format_uninstall_plan(plan))
            command_context.succeed()
            return 0

        command_context.set_stage("uninstall_payload")
        if plan.payload_dirs:
            print("Removing managed TimeCapsuleSMB payload from:")
            for payload_dir in plan.payload_dirs:
                print(f"  {payload_dir}")
        else:
            print("No mounted HFS volumes found; removing flash hooks and runtime state only.")
        remote_uninstall_payload(connection, plan)
        print("Removed managed payload, flash hooks, and runtime state.")

        if args.no_reboot:
            print("Skipping reboot.")
            command_context.succeed()
            return 0

        if not args.yes:
            command_context.set_stage("confirm_reboot")
            device_name = command_context.optional_airport_display_name(timeout_seconds=0.1)
            proceed = command_context.confirm_or_fail(
                f"This will reboot the {device_name} now. Continue?",
                default=True,
                noninteractive_message="Running `uninstall` with reboot requires confirmation when stdin is not interactive. Use `uninstall --yes` to skip the prompt or `uninstall --no-reboot`.",
                allow_prompt=not no_input_enabled(args),
            )
            if proceed is None:
                return 1
            if not proceed:
                print(f"Skipped reboot. The {device_name} may need a manual reboot to fully clear running processes.")
                command_context.succeed()
                return 0

        try:
            verified = reboot_after_uninstall(connection, plan, callbacks=command_context.to_operation_callbacks())
        except (RebootFlowError, DeviceError) as exc:
            print(str(exc))
            command_context.fail_with_error(str(exc))
            return 1
        if verified:
            command_context.update_fields(post_uninstall_verified=True)
        else:
            print("Reboot requested; not waiting for the device to go down or come back.")
            print("Post-uninstall verification skipped.")
        command_context.succeed()
        return 0
    return 1
