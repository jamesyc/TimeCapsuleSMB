from __future__ import annotations

import argparse
from typing import Optional

from timecapsulesmb.cli.context import CommandContext
from timecapsulesmb.cli.flows import runtime_callbacks
from timecapsulesmb.cli.runtime import (
    add_config_argument,
    add_mount_wait_argument,
    add_no_input_argument,
    add_no_wait_argument,
    no_input_enabled,
    print_json,
)
from timecapsulesmb.core.config import MANAGED_PAYLOAD_DIR_NAME
from timecapsulesmb.deploy.dry_run import format_uninstall_plan, uninstall_plan_to_jsonable
from timecapsulesmb.deploy.executor import remote_uninstall_payload
from timecapsulesmb.deploy.planner import build_uninstall_plan
from timecapsulesmb.deploy.verify import render_post_uninstall_verification, verify_post_uninstall
from timecapsulesmb.device.storage import UNINSTALL_DRY_RUN_VOLUME_ROOT_PLACEHOLDER
from timecapsulesmb.identity import ensure_install_id
from timecapsulesmb.services.maintenance import UNINSTALL_REBOOT_NO_DOWN_MESSAGE as REBOOT_NO_DOWN_MESSAGE
from timecapsulesmb.services.reboot import RebootFlowError, request_reboot, request_reboot_and_wait
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
        connection = command_context.resolve_env_connection(allow_empty_password=True)
        if connection.password:
            command_context.start_optional_airport_identity_probe(connection)

        if args.dry_run:
            volume_roots = [UNINSTALL_DRY_RUN_VOLUME_ROOT_PLACEHOLDER]
            payload_dirs = [f"{UNINSTALL_DRY_RUN_VOLUME_ROOT_PLACEHOLDER}/{MANAGED_PAYLOAD_DIR_NAME}"]
        else:
            mounted_volumes = command_context.mount_mast_volumes(
                connection,
                wait_seconds=args.mount_wait,
            )
            volume_roots = [volume.volume_root for volume in mounted_volumes]
            payload_dirs = [f"{volume_root}/{MANAGED_PAYLOAD_DIR_NAME}" for volume_root in volume_roots]
        command_context.update_fields(volume_roots=volume_roots, payload_dirs=payload_dirs)
        command_context.set_stage("build_uninstall_plan")
        plan = build_uninstall_plan(
            connection.host,
            volume_roots,
            payload_dirs,
            reboot_after_uninstall=not args.no_reboot,
            wait_after_reboot=not args.no_wait,
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

        if args.no_wait:
            try:
                request_reboot(
                    connection,
                    strategy="acp_then_ssh",
                    callbacks=runtime_callbacks(command_context),
                    raise_on_request_error=True,
                )
            except RebootFlowError as exc:
                print(str(exc))
                command_context.fail_with_error(str(exc))
                return 1
            print("Reboot requested; not waiting for the device to go down or come back.")
            print("Post-uninstall verification skipped.")
            command_context.succeed()
            return 0

        try:
            request_reboot_and_wait(
                connection,
                strategy="acp_then_ssh",
                callbacks=runtime_callbacks(command_context),
                down_timeout_seconds=60,
                up_timeout_seconds=240,
                reboot_no_down_message=REBOOT_NO_DOWN_MESSAGE,
                reboot_up_timeout_message="Timed out waiting for SSH after reboot.",
            )
        except RebootFlowError as exc:
            print(str(exc))
            command_context.fail_with_error(str(exc))
            return 1

        command_context.set_stage("verify_post_uninstall")
        verification = verify_post_uninstall(connection, plan)
        for line in render_post_uninstall_verification(verification):
            print(line)
        if verification:
            command_context.update_fields(post_uninstall_verified=True)
            command_context.succeed()
            return 0

        print("Managed TimeCapsuleSMB files are still present after reboot.")
        command_context.fail_with_error("Managed TimeCapsuleSMB files are still present after reboot.")
        return 1
    return 1
