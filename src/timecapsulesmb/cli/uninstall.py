from __future__ import annotations

import argparse
import json
from typing import Optional

from timecapsulesmb.cli.context import CommandContext
from timecapsulesmb.cli.runtime import load_env_values
from timecapsulesmb.core.config import airport_exact_display_name, require_valid_config
from timecapsulesmb.deploy.dry_run import format_uninstall_plan, uninstall_plan_to_jsonable
from timecapsulesmb.deploy.executor import remote_request_reboot, remote_uninstall_payload
from timecapsulesmb.deploy.planner import build_uninstall_plan
from timecapsulesmb.deploy.verify import verify_post_uninstall
from timecapsulesmb.device.probe import build_device_paths, discover_volume_root_conn, wait_for_ssh_state_conn
from timecapsulesmb.identity import ensure_install_id
from timecapsulesmb.telemetry import TelemetryClient


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Remove the managed TimeCapsuleSMB payload from the configured device.")
    parser.add_argument("--yes", action="store_true", help="Do not prompt before reboot")
    parser.add_argument("--no-reboot", action="store_true", help="Remove files but do not reboot the device")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without making changes")
    parser.add_argument("--json", action="store_true", help="Output the dry-run uninstall plan as JSON")
    args = parser.parse_args(argv)

    if args.json and not args.dry_run:
        parser.error("--json currently requires --dry-run")

    if not args.json:
        print("Uninstalling...")

    ensure_install_id()
    values = load_env_values()
    telemetry = TelemetryClient.from_values(values)
    with CommandContext(telemetry, "uninstall", "uninstall_started", "uninstall_finished", values=values, args=args) as command_context:
        command_context.update_fields(
            reboot_was_attempted=False,
            device_came_back_after_reboot=False,
            post_uninstall_verified=False,
        )
        command_context.set_stage("validate_config")
        require_valid_config(values, profile="uninstall")
        device_name = airport_exact_display_name(values)
        command_context.set_stage("resolve_connection")
        connection = command_context.resolve_env_connection(allow_empty_password=True)

        command_context.set_stage("discover_volume_root")
        volume_root = discover_volume_root_conn(connection)
        command_context.update_fields(volume_root=volume_root)
        device_paths = build_device_paths(volume_root, values["TC_PAYLOAD_DIR_NAME"])
        command_context.set_stage("build_uninstall_plan")
        plan = build_uninstall_plan(connection.host, device_paths, reboot_after_uninstall=not args.no_reboot)
        command_context.update_fields(payload_dir=plan.payload_dir)

        if args.dry_run:
            if args.json:
                print(json.dumps(uninstall_plan_to_jsonable(plan), indent=2, sort_keys=True))
            else:
                print(format_uninstall_plan(plan))
            command_context.succeed()
            return 0

        command_context.set_stage("uninstall_payload")
        print(f"Removing managed TimeCapsuleSMB payload from {plan.payload_dir}")
        remote_uninstall_payload(connection, plan)
        print("Removed managed payload, flash hooks, and runtime state.")

        if args.no_reboot:
            print("Skipping reboot.")
            command_context.succeed()
            return 0

        if not args.yes:
            command_context.set_stage("confirm_reboot")
            answer = input(f"This will reboot the {device_name} now. Continue? [Y/n]: ").strip().lower()
            if answer not in {"", "y", "yes"}:
                print(f"Skipped reboot. The {device_name} may need a manual reboot to fully clear running processes.")
                command_context.succeed()
                return 0

        command_context.set_stage("reboot")
        command_context.update_fields(reboot_was_attempted=True)
        remote_request_reboot(connection)
        print("Reboot requested. Waiting for the device to go down...")
        command_context.set_stage("wait_for_reboot_down")
        wait_for_ssh_state_conn(connection, expected_up=False, timeout_seconds=60)
        print("Waiting for the device to come back up...")
        command_context.set_stage("wait_for_reboot_up")
        if not wait_for_ssh_state_conn(connection, expected_up=True, timeout_seconds=240):
            print("Timed out waiting for SSH after reboot.")
            command_context.fail_with_error("Timed out waiting for SSH after reboot.")
            return 1

        command_context.update_fields(device_came_back_after_reboot=True)
        print("Device is back online.")
        command_context.set_stage("verify_post_uninstall")
        if verify_post_uninstall(connection, plan):
            command_context.update_fields(post_uninstall_verified=True)
            command_context.succeed()
            return 0

        print("Managed TimeCapsuleSMB files are still present after reboot.")
        command_context.fail_with_error("Managed TimeCapsuleSMB files are still present after reboot.")
        return 1
    return 1
