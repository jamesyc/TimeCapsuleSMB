from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Optional

from timecapsulesmb.cli.runtime import load_env_values, resolve_env_connection
from timecapsulesmb.core.config import require_valid_config
from timecapsulesmb.deploy.dry_run import format_uninstall_plan, uninstall_plan_to_jsonable
from timecapsulesmb.deploy.executor import remote_uninstall_payload
from timecapsulesmb.deploy.planner import build_uninstall_plan
from timecapsulesmb.deploy.verify import verify_post_uninstall
from timecapsulesmb.device.probe import build_device_paths, discover_volume_root_conn, wait_for_ssh_state_conn
from timecapsulesmb.transport.ssh import run_ssh


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Remove the managed TimeCapsuleSMB payload from a Time Capsule.")
    parser.add_argument("--yes", action="store_true", help="Do not prompt before reboot")
    parser.add_argument("--no-reboot", action="store_true", help="Remove files but do not reboot the device")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without making changes")
    parser.add_argument("--json", action="store_true", help="Output the dry-run uninstall plan as JSON")
    args = parser.parse_args(argv)

    if args.json and not args.dry_run:
        parser.error("--json currently requires --dry-run")

    if not args.json:
        print("Uninstalling...")

    values = load_env_values()
    require_valid_config(values, profile="uninstall")
    connection = resolve_env_connection(values, allow_empty_password=True)

    volume_root = discover_volume_root_conn(connection)
    device_paths = build_device_paths(volume_root, values["TC_PAYLOAD_DIR_NAME"])
    plan = build_uninstall_plan(connection.host, device_paths, reboot_after_uninstall=not args.no_reboot)

    if args.dry_run:
        if args.json:
            print(json.dumps(uninstall_plan_to_jsonable(plan), indent=2, sort_keys=True))
        else:
            print(format_uninstall_plan(plan))
        return 0

    print(f"Removing managed TimeCapsuleSMB payload from {plan.payload_dir}")
    remote_uninstall_payload(connection, plan)
    print("Removed managed payload, flash hooks, and runtime state.")

    if args.no_reboot:
        print("Skipping reboot.")
        return 0

    if not args.yes:
        answer = input("This will reboot the Time Capsule now. Continue? [Y/n]: ").strip().lower()
        if answer not in {"", "y", "yes"}:
            print("Skipped reboot. The Time Capsule may need a manual reboot to fully clear running processes.")
            return 0

    run_ssh(connection, "/sbin/reboot", check=False)
    print("Reboot requested. Waiting for the device to go down...")
    wait_for_ssh_state_conn(connection, expected_up=False, timeout_seconds=60)
    print("Waiting for the device to come back up...")
    if not wait_for_ssh_state_conn(connection, expected_up=True, timeout_seconds=240):
        print("Timed out waiting for SSH after reboot.")
        return 1

    print("Device is back online.")
    if verify_post_uninstall(connection, plan):
        return 0

    print("Managed TimeCapsuleSMB files are still present after reboot.")
    return 1
