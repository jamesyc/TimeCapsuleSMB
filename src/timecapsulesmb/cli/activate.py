from __future__ import annotations

import argparse
from typing import Optional

from timecapsulesmb.cli.context import CommandContext
from timecapsulesmb.cli.flows import verify_managed_runtime_flow
from timecapsulesmb.cli.runtime import (
    add_config_argument,
    add_no_input_argument,
    no_input_enabled,
    print_json,
    require_netbsd4_device_compatibility,
)
from timecapsulesmb.core.config import airport_exact_display_name_from_identity
from timecapsulesmb.identity import ensure_install_id
from timecapsulesmb.deploy.dry_run import activation_plan_to_jsonable, format_activation_plan
from timecapsulesmb.deploy.executor import run_remote_actions
from timecapsulesmb.deploy.planner import build_runtime_activation_plan
from timecapsulesmb.services.activation import decide_manual_activation
from timecapsulesmb.services.runtime import load_env_config
from timecapsulesmb.services.runtime_verification import wait_for_activation_settle
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
    add_no_input_argument(parser)
    parser.add_argument("--dry-run", action="store_true", help="Print activation actions without making changes")
    parser.add_argument("--json", action="store_true", help="Output the dry-run activation plan as JSON")
    args = parser.parse_args(argv)

    if args.json and not args.dry_run:
        parser.error("--json currently requires --dry-run")

    ensure_install_id()
    config = load_env_config(env_path=args.config)
    telemetry = TelemetryClient.from_config(config)
    with CommandContext(telemetry, "activate", "activate_started", "activate_finished", config=config, args=args) as command_context:
        command_context.update_fields(dry_run=args.dry_run, yes=args.yes, runtime_already_ready=False)
        if no_input_enabled(args) and not args.yes and not args.dry_run:
            command_context.set_stage("noninteractive_confirmation")
            message = "Running `activate` in non-interactive mode requires `--yes` to approve activation."
            print(message)
            command_context.fail_with_error(message)
            return 1
        command_context.set_stage("resolve_managed_target")
        target = command_context.resolve_validated_managed_target(profile="activate", include_probe=True)
        connection = target.connection
        command_context.set_stage("check_compatibility")
        require_netbsd4_device_compatibility(
            command_context,
            command_name="activate",
            json_output=args.json,
            unsupported_message="activate is only supported for NetBSD4 AirPort storage devices; use deploy for persistent NetBSD6 installs.",
        )

        command_context.set_stage("build_activation_plan")
        plan = build_runtime_activation_plan()
        device_name = _target_device_display_name(target)
        command_context.update_fields(activation_action_count=len(plan.actions))

        if args.dry_run:
            if args.json:
                print_json(activation_plan_to_jsonable(plan))
            else:
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
                allow_prompt=not no_input_enabled(args),
            )
            if proceed is None:
                return 1
            if not proceed:
                print("Activation cancelled.")
                command_context.cancel_with_error("Cancelled by user at NetBSD4 activation confirmation prompt.")
                return 0

        command_context.set_stage("probe_runtime")
        decision = decide_manual_activation(connection)
        command_context.add_debug_fields(
            activation_decision=decision.reason,
            manual_activation_required=decision.run_actions,
        )
        print(decision.detail)
        if not decision.run_actions:
            print("NetBSD4 payload already active; skipping rc.local.")
            command_context.update_fields(runtime_already_ready=True)
            print(f"NetBSD4 activation complete. {NETBSD4_REBOOT_FOLLOWUP}")
            command_context.succeed()
            return 0

        command_context.set_stage("run_activation")
        print("Activating NetBSD4 payload without file transfer.")
        run_remote_actions(connection, plan.actions)
        wait_for_activation_settle(command_context.to_operation_callbacks())
        if not verify_managed_runtime_flow(
            connection,
            command_context,
            stage="verify_runtime_activation",
            timeout_seconds=200,
            heading="Waiting for NetBSD 4 device activation, this can take a few minutes for Samba to start up...",
            failure_message="NetBSD4 activation failed.",
        ):
            return 1
        print(f"NetBSD4 activation complete. {NETBSD4_REBOOT_FOLLOWUP}")
        command_context.succeed()
        return 0
    return 1
