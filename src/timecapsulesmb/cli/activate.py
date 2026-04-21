from __future__ import annotations

import argparse
from typing import Optional

from timecapsulesmb.core.config import ENV_PATH, parse_env_values
from timecapsulesmb.cli.context import CommandContext
from timecapsulesmb.identity import ensure_install_id
from timecapsulesmb.deploy.commands import render_remote_actions
from timecapsulesmb.deploy.executor import run_remote_actions
from timecapsulesmb.deploy.planner import build_netbsd4_activation_actions
from timecapsulesmb.deploy.verify import netbsd4_activation_is_already_healthy, verify_netbsd4_activation
from timecapsulesmb.device.compat import probe_device_compatibility
from timecapsulesmb.telemetry import TelemetryClient, build_device_os_version, detect_device_family
from timecapsulesmb.cli.util import NETBSD4_REBOOT_FOLLOWUP, NETBSD4_REBOOT_GUIDANCE, color_red, resolve_validated_managed_connection


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Manually activate an already-deployed NetBSD4 Time Capsule payload.")
    parser.add_argument("--yes", action="store_true", help="Do not prompt before restarting the deployed Samba services")
    parser.add_argument("--dry-run", action="store_true", help="Print activation actions without making changes")
    args = parser.parse_args(argv)

    ensure_install_id()
    values = parse_env_values(ENV_PATH)
    telemetry = TelemetryClient.from_values(values)
    with CommandContext(telemetry, "activate", "activate_started", "activate_finished") as command_context:
        host, password, ssh_opts = resolve_validated_managed_connection(
            values,
            command_name="activate",
            profile="activate",
        )

        compatibility = probe_device_compatibility(host, password, ssh_opts)
        command_context.update_fields(device_os_version=build_device_os_version(
            compatibility.os_name,
            compatibility.os_release,
            compatibility.arch,
        ))
        command_context.update_fields(device_family=detect_device_family(compatibility.payload_family))
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
            print("This will start the deployed Samba payload on the Time Capsule.")
            print(color_red(NETBSD4_REBOOT_GUIDANCE))
            command_context.set_result("success")
            return 0

        if not args.yes:
            print("This will start the deployed Samba payload on the Time Capsule.")
            print(color_red(NETBSD4_REBOOT_GUIDANCE))
            answer = input("Continue with NetBSD4 activation? [y/N]: ").strip().lower()
            if answer not in {"y", "yes"}:
                print("Activation cancelled.")
                command_context.set_result("cancelled")
                return 0

        if netbsd4_activation_is_already_healthy(host, password, ssh_opts):
            print("NetBSD4 payload already active; skipping rc.local.")
            command_context.set_result("success")
            return 0

        print("Activating NetBSD4 payload without file transfer.")
        run_remote_actions(host, password, ssh_opts, actions)
        if not verify_netbsd4_activation(host, password, ssh_opts):
            print("NetBSD4 activation failed.")
            return 1
        print(f"NetBSD4 activation complete. {NETBSD4_REBOOT_FOLLOWUP}")
        command_context.set_result("success")
        return 0
