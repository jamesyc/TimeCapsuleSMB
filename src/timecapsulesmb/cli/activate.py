from __future__ import annotations

import argparse
from typing import Optional

from timecapsulesmb.cli.context import CommandContext
from timecapsulesmb.cli.flows import verify_managed_runtime_flow
from timecapsulesmb.cli.runtime import add_config_argument, load_env_config
from timecapsulesmb.core.config import airport_exact_display_name_from_config
from timecapsulesmb.identity import ensure_install_id
from timecapsulesmb.deploy.dry_run import format_activation_plan
from timecapsulesmb.deploy.executor import run_remote_actions
from timecapsulesmb.deploy.planner import build_netbsd4_activation_plan
from timecapsulesmb.device.compat import is_netbsd4_payload_family, render_compatibility_message
from timecapsulesmb.device.probe import probe_managed_runtime_conn
from timecapsulesmb.telemetry import TelemetryClient
from timecapsulesmb.cli.util import NETBSD4_REBOOT_FOLLOWUP, NETBSD4_REBOOT_GUIDANCE, color_red


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Manually activate an already-deployed NetBSD4 AirPort storage device payload.")
    add_config_argument(parser)
    parser.add_argument("--yes", action="store_true", help="Do not prompt before restarting the deployed Samba services")
    parser.add_argument("--dry-run", action="store_true", help="Print activation actions without making changes")
    args = parser.parse_args(argv)

    ensure_install_id()
    config = load_env_config(env_path=args.config)
    telemetry = TelemetryClient.from_config(config)
    with CommandContext(telemetry, "activate", "activate_started", "activate_finished", config=config, args=args) as command_context:
        command_context.update_fields(dry_run=args.dry_run, yes=args.yes, runtime_already_ready=False)
        command_context.set_stage("resolve_managed_target")
        target = command_context.resolve_validated_managed_target(profile="activate", include_probe=True)
        connection = target.connection
        command_context.set_stage("check_compatibility")
        compatibility = command_context.require_compatibility()
        compatibility_message = render_compatibility_message(compatibility)
        netbsd4_payload = is_netbsd4_payload_family(compatibility.payload_family)
        command_context.update_fields(
            compatibility_supported=compatibility.supported,
            netbsd4_payload=netbsd4_payload,
        )
        if not compatibility.supported:
            raise SystemExit(compatibility_message)
        print(compatibility_message)
        if not netbsd4_payload:
            raise SystemExit("activate is only supported for NetBSD4 AirPort storage devices; use deploy for persistent NetBSD6 installs.")

        command_context.set_stage("build_activation_plan")
        plan = build_netbsd4_activation_plan()
        device_name = airport_exact_display_name_from_config(config)
        command_context.update_fields(activation_action_count=len(plan.actions))

        if args.dry_run:
            print(format_activation_plan(plan, device_name=device_name))
            command_context.succeed()
            return 0

        if not args.yes:
            command_context.set_stage("confirm_activation")
            print(f"This will start the deployed Samba payload on the {device_name}.")
            print(color_red(NETBSD4_REBOOT_GUIDANCE))
            try:
                answer = input("Continue with NetBSD4 activation? [y/N]: ").strip().lower()
            except EOFError:
                message = "Running `activate` requires confirmation when stdin is not interactive. Use `activate --yes` in a non-interactive environment."
                print(message)
                command_context.fail_with_error(message)
                return 1
            if answer not in {"y", "yes"}:
                print("Activation cancelled.")
                command_context.cancel_with_error("Cancelled by user at NetBSD4 activation confirmation prompt.")
                return 0

        command_context.set_stage("probe_runtime")
        if probe_managed_runtime_conn(connection, timeout_seconds=20).ready:
            print("NetBSD4 payload already active; skipping rc.local.")
            command_context.update_fields(runtime_already_ready=True)
            command_context.succeed()
            return 0

        command_context.set_stage("run_activation")
        print("Activating NetBSD4 payload without file transfer.")
        run_remote_actions(connection, plan.actions)
        if not verify_managed_runtime_flow(
            connection,
            command_context,
            stage="verify_runtime_activation",
            timeout_seconds=180,
            heading="Waiting for NetBSD 4 device activation, this can take a few minutes for Samba to start up...",
            failure_message="NetBSD4 activation failed.",
        ):
            return 1
        print(f"NetBSD4 activation complete. {NETBSD4_REBOOT_FOLLOWUP}")
        command_context.succeed()
        return 0
    return 1
