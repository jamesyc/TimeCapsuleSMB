from __future__ import annotations

import argparse
import getpass
from typing import Optional

from timecapsulesmb.core.config import ENV_PATH, parse_env_values
from timecapsulesmb.deploy.commands import render_remote_actions
from timecapsulesmb.deploy.executor import run_remote_actions
from timecapsulesmb.deploy.planner import build_netbsd4_activation_actions
from timecapsulesmb.deploy.verify import verify_netbsd4_activation
from timecapsulesmb.device.compat import probe_device_compatibility


def require(values: dict[str, str], key: str) -> str:
    value = values.get(key, "")
    if not value:
        raise SystemExit(f"Missing required setting in .env: {key}")
    return value


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Manually activate an already-deployed NetBSD4 Time Capsule payload.")
    parser.add_argument("--yes", action="store_true", help="Do not prompt before stopping Apple SMB/mDNS")
    parser.add_argument("--dry-run", action="store_true", help="Print activation actions without making changes")
    args = parser.parse_args(argv)

    values = parse_env_values(ENV_PATH)
    host = require(values, "TC_HOST")
    password = values.get("TC_PASSWORD", "")
    if not password:
        password = getpass.getpass("Time Capsule root password: ")
    ssh_opts = values["TC_SSH_OPTS"]

    compatibility = probe_device_compatibility(host, password, ssh_opts)
    if not compatibility.supported:
        raise SystemExit(compatibility.message)
    print(compatibility.message)
    if compatibility.payload_family != "netbsd4_samba4":
        raise SystemExit("activate is only supported for NetBSD4 Time Capsules; use deploy for persistent NetBSD6 installs.")

    actions = build_netbsd4_activation_actions()

    if args.dry_run:
        print("Dry run: NetBSD4 activation plan")
        print("")
        print("Remote actions:")
        for command in render_remote_actions(actions):
            print(f"  {command}")
        print("")
        print("Post-activation checks:")
        print("  fstat shows smbd bound to TCP 445")
        print("  fstat shows mdns-advertiser bound to UDP 5353")
        print("")
        print("Note: activation is immediate. Tested NetBSD4 devices need this after reboot;")
        print("      other NetBSD4 generations may auto-start if their firmware runs rc.local.")
        return 0

    if not args.yes:
        print("This will stop Apple SMB/mDNS and start the already-deployed Samba payload.")
        print("Tested NetBSD4 devices need to run `activate` after reboot; other NetBSD4 generations may auto-start if their firmware runs /mnt/Flash/rc.local after a reboot.")
        answer = input("Continue with NetBSD4 activation? [y/N]: ").strip().lower()
        if answer not in {"y", "yes"}:
            print("Activation cancelled.")
            return 0

    print("Activating NetBSD4 payload without file transfer.")
    run_remote_actions(host, password, ssh_opts, actions)
    if not verify_netbsd4_activation(host, password, ssh_opts):
        print("NetBSD4 activation failed.")
        return 1
    print("NetBSD4 activation complete. Run activate again after reboot if the device did not auto-start Samba.")
    return 0
