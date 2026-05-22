from __future__ import annotations

import argparse
from typing import Optional

from timecapsulesmb.cli.context import CommandContext
from timecapsulesmb.cli.flows import activate_deployed_runtime_flow
from timecapsulesmb.cli.runtime import (
    add_config_argument,
    load_env_config,
    require_netbsd4_device_compatibility,
)
from timecapsulesmb.core.config import airport_exact_display_name_from_identity
from timecapsulesmb.identity import ensure_install_id
from timecapsulesmb.deploy.dry_run import format_activation_plan
from timecapsulesmb.deploy.executor import run_remote_actions
from timecapsulesmb.deploy.planner import build_runtime_activation_plan
from timecapsulesmb.telemetry import TelemetryClient
from timecapsulesmb.cli.util import color_red
from timecapsulesmb.core.messages import NETBSD4_REBOOT_FOLLOWUP, NETBSD4_REBOOT_GUIDANCE


def _target_device_display_name(target) -> str:
    probe_state = target.probe_state
    if probe_state is None:
        return "AirPort storage device"
    probe_result = probe_state.probe_result
    return airport_exact_display_name_from_identity(
        model=probe_result.airport_model,
        syap=probe_result.airport_syap,
    )


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
        require_netbsd4_device_compatibility(
            command_context,
            command_name="activate",
            unsupported_message="activate is only supported for NetBSD4 AirPort storage devices; use deploy for persistent NetBSD6 installs.",
        )

        command_context.set_stage("build_activation_plan")
        plan = build_runtime_activation_plan()
        device_name = _target_device_display_name(target)
        command_context.update_fields(activation_action_count=len(plan.actions))

        if args.dry_run:
            print(format_activation_plan(plan, device_name=device_name))
            command_context.succeed()
            return 0

        if not args.yes:
            command_context.set_stage("confirm_activation")
            print(f"This will start the deployed Samba payload on the {device_name}.")
            print(color_red(NETBSD4_REBOOT_GUIDANCE))
            proceed = command_context.confirm_or_fail(
                "Continue with NetBSD4 activation?",
                default=False,
                noninteractive_message="Running `activate` requires confirmation when stdin is not interactive. Use `activate --yes` in a non-interactive environment.",
            )
            if proceed is None:
                return 1
            if not proceed:
                print("Activation cancelled.")
                command_context.cancel_with_error("Cancelled by user at NetBSD4 activation confirmation prompt.")
                return 0

        if not activate_deployed_runtime_flow(
            connection,
            command_context,
            plan.actions,
            run_actions=run_remote_actions,
            skip_if_ready=True,
            already_active_message="NetBSD4 payload already active; skipping rc.local.",
            startup_in_progress_message="NetBSD4 payload startup is already in progress; waiting for it to finish.",
            activation_message="Activating NetBSD4 payload without file transfer.",
            activation_stage="run_activation",
            verification_stage="verify_runtime_activation",
            verification_timeout_seconds=180,
            verification_heading="Waiting for NetBSD 4 device activation, this can take a few minutes for Samba to start up...",
            failure_message="NetBSD4 activation failed.",
        ):
            return 1
        print(f"NetBSD4 activation complete. {NETBSD4_REBOOT_FOLLOWUP}")
        command_context.succeed()
        return 0
    return 1
