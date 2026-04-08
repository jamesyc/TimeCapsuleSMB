from __future__ import annotations

import argparse
import getpass
import json
from pathlib import Path
from typing import Optional

from timecapsulesmb.core.config import ENV_PATH, extract_host, parse_env_values
from timecapsulesmb.deploy.dry_run import format_uninstall_plan, uninstall_plan_to_jsonable
from timecapsulesmb.deploy.executor import remote_uninstall_payload
from timecapsulesmb.deploy.planner import build_uninstall_plan
from timecapsulesmb.deploy.verify import verify_post_uninstall
from timecapsulesmb.device.probe import build_device_paths, discover_volume_root, wait_for_ssh_state
from timecapsulesmb.transport.ssh import run_ssh


def require(values: dict[str, str], key: str) -> str:
    value = values.get(key, "")
    if not value:
        raise SystemExit(f"Missing required setting in .env: {key}")
    return value


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Remove the managed TimeCapsuleSMB payload from a Time Capsule.")
    parser.add_argument("--yes", action="store_true", help="Do not prompt before reboot")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without making changes")
    parser.add_argument("--json", action="store_true", help="Output the dry-run uninstall plan as JSON")
    args = parser.parse_args(argv)

    if args.json and not args.dry_run:
        parser.error("--json currently requires --dry-run")

    values = parse_env_values(ENV_PATH)
    host = require(values, "TC_HOST")
    password = values.get("TC_PASSWORD", "")
    if not password:
        password = getpass.getpass("Time Capsule root password: ")
    ssh_opts = values["TC_SSH_OPTS"]

    volume_root = discover_volume_root(host, password, ssh_opts)
    device_paths = build_device_paths(volume_root, values["TC_PAYLOAD_DIR_NAME"])
    plan = build_uninstall_plan(host, device_paths)

    if args.dry_run:
        if args.json:
            print(json.dumps(uninstall_plan_to_jsonable(plan), indent=2, sort_keys=True))
        else:
            print(format_uninstall_plan(plan))
        return 0

    print(f"Removing managed TimeCapsuleSMB payload from {plan.payload_dir}")
    remote_uninstall_payload(host, password, ssh_opts, plan)
    print("Removed managed payload, flash hooks, and runtime state.")

    if not args.yes:
        answer = input("This will reboot the Time Capsule now. Continue? [Y/n]: ").strip().lower()
        if answer not in {"", "y", "yes"}:
            print("Uninstall requires a reboot to complete.")
            return 1

    run_ssh(host, password, ssh_opts, "/sbin/reboot", check=False)
    hostname = extract_host(host)
    print("Reboot requested. Waiting for the device to go down...")
    wait_for_ssh_state(hostname, expected_up=False, timeout_seconds=60)
    print("Waiting for the device to come back up...")
    if not wait_for_ssh_state(hostname, expected_up=True, timeout_seconds=240):
        print("Timed out waiting for SSH after reboot.")
        return 1

    print("Device is back online.")
    if verify_post_uninstall(host, password, ssh_opts, plan):
        return 0

    print("Managed TimeCapsuleSMB files are still present after reboot.")
    return 1
