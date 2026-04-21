from __future__ import annotations

import argparse
from typing import Optional

from timecapsulesmb.cli.context import CommandContext
from timecapsulesmb.cli.runtime import load_env_values, probe_compatibility
from timecapsulesmb.identity import ensure_install_id
from timecapsulesmb.deploy.commands import render_remote_actions
from timecapsulesmb.deploy.executor import run_remote_actions
from timecapsulesmb.deploy.planner import build_netbsd4_activation_actions
from timecapsulesmb.deploy.verify import netbsd4_activation_is_already_healthy, verify_netbsd4_activation
from timecapsulesmb.telemetry import TelemetryClient
from timecapsulesmb.cli.util import NETBSD4_REBOOT_FOLLOWUP, NETBSD4_REBOOT_GUIDANCE, color_red


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Manually activate an already-deployed NetBSD4 Time Capsule payload.")
    parser.add_argument("--yes", action="store_true", help="Do not prompt before restarting the deployed Samba services")
    parser.add_argument("--dry-run", action="store_true", help="Print activation actions without making changes")
    args = parser.parse_args(argv)

    ensure_install_id()
    values = load_env_values()
    telemetry = TelemetryClient.from_values(values)
    with CommandContext(telemetry, "activate", "activate_started", "activate_finished", values=values, args=args) as command_context:
        connection = command_context.resolve_validated_managed_connection(profile="activate")
        compatibility = command_context.probe_compatibility(probe_compatibility)
        host, password, ssh_opts = connection.host, connection.password, connection.ssh_opts
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
            command_context.succeed()
            return 0

        if not args.yes:
            print("This will start the deployed Samba payload on the Time Capsule.")
            print(color_red(NETBSD4_REBOOT_GUIDANCE))
            answer = input("Continue with NetBSD4 activation? [y/N]: ").strip().lower()
            if answer not in {"y", "yes"}:
                print("Activation cancelled.")
                command_context.cancel()
                return 0

        if netbsd4_activation_is_already_healthy(host, password, ssh_opts):
            print("NetBSD4 payload already active; skipping rc.local.")
            command_context.succeed()
            return 0

        print("Activating NetBSD4 payload without file transfer.")
        run_remote_actions(host, password, ssh_opts, actions)
        if not verify_netbsd4_activation(host, password, ssh_opts):
            print("NetBSD4 activation failed.")
            return 1
        print(f"NetBSD4 activation complete. {NETBSD4_REBOOT_FOLLOWUP}")
        command_context.succeed()
        return 0
