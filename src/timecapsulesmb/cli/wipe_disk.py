from __future__ import annotations

import argparse
from typing import Optional

from timecapsulesmb.cli.context import CommandContext
from timecapsulesmb.cli.runtime import (
    add_config_argument,
    add_no_input_argument,
    confirm,
    confirm_despite_failing_disk,
    no_input_enabled,
)
from timecapsulesmb.device.wipe import gpt_verdict_description
from timecapsulesmb.identity import ensure_install_id
from timecapsulesmb.services.activation import installed_netbsd4_autostart
from timecapsulesmb.services.reboot import RebootFlowError
from timecapsulesmb.services.runtime import load_env_config, require_acpd_ready
from timecapsulesmb.services.wipe_disk import (
    WipeDiskError,
    WipePlan,
    describe_disk,
    format_wipe_plan,
    prepare_wipe,
    run_wipe,
    volume_summaries,
    wipe_failing_evidence,
)
from timecapsulesmb.telemetry import TelemetryClient


def confirmation_text(plan: WipePlan, device_name: str) -> str:
    """The disk, then what is on it: its named volumes, else what its
    partition table holds, in words."""
    lost = volume_summaries(plan.disk)
    found = None if lost else gpt_verdict_description(plan.gpt_verdict)
    return (
        f"Wipe Internal Disk deletes everything on {describe_disk(plan.disk)}"
        + (f": {'; '.join(lost)}" if lost else "")
        + "."
        + (f" {found}" if found else "")
        + f" Any backup in progress will be interrupted. It then formats the disk as \"{plan.volume_name}\" "
        f"and restarts the {device_name}. Wipe the disk?"
    )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Delete everything on the device's internal disk and format it for the device, as AirPort "
        "Utility's Erase Disk does, also when Erase Disk cannot replace the disk's partition table.",
    )
    add_config_argument(parser)
    parser.add_argument("--dry-run", action="store_true", help="Read the disk and print what would be done; change nothing")
    parser.add_argument("--yes", action="store_true", help="Do not prompt before wiping")
    add_no_input_argument(parser)
    args = parser.parse_args(argv)

    ensure_install_id()
    config = load_env_config(env_path=args.config)
    telemetry = TelemetryClient.from_config(config)
    with CommandContext(telemetry, "wipe-disk", "wipe_disk_started", "wipe_disk_finished", config=config, args=args) as command_context:
        command_context.update_fields(reboot_was_attempted=False, device_came_back_after_reboot=False)
        command_context.set_stage("validate_config")
        command_context.require_valid_config(profile="fsck")
        if no_input_enabled(args) and not args.dry_run and not args.yes:
            command_context.set_stage("noninteractive_confirmation")
            message = "Running `wipe-internal-disk` in non-interactive mode requires `--yes`."
            print(message)
            command_context.fail_with_error(message)
            return 1
        command_context.set_stage("resolve_connection")
        # The reboot goes through AirPort ACP, which needs the password.
        connection = command_context.resolve_env_connection(allow_empty_password=args.dry_run)
        if connection.password:
            command_context.start_optional_airport_identity_probe(connection)
        if not args.dry_run:
            require_acpd_ready(connection, command_context.to_operation_callbacks(),
                               reading=command_context.reading_at(connection))

        callbacks = command_context.to_operation_callbacks()
        try:
            plan = prepare_wipe(connection, callbacks=callbacks)
            if args.dry_run:
                print(format_wipe_plan(plan))
                command_context.succeed()
                return 0

            print(f"Target host: {connection.host}")
            # A failing disk's warning asks first; then the wipe's own question.
            proceed = confirm_despite_failing_disk(
                connection, "wipe", yes=args.yes, command="wipe-internal-disk", update_fields=command_context.update_fields,
                read_evidence=lambda conn: wipe_failing_evidence(conn, plan),
            )
            if proceed is False:
                print("Wipe Internal Disk cancelled.")
                command_context.cancel_with_error("Cancelled by user at the failing-disk warning.")
                return 0
            if not args.yes:
                command_context.set_stage("confirm_wipe")
                device_name = command_context.optional_airport_display_name(timeout_seconds=0.1)
                if not confirm(
                    confirmation_text(plan, device_name),
                    default=False,
                    noninteractive_message="No answer was read for the Wipe Internal Disk confirmation. Use `wipe-internal-disk --yes` to skip the prompt.",
                ):
                    print("Wipe Internal Disk cancelled.")
                    command_context.cancel_with_error("Cancelled by user at the Wipe Internal Disk confirmation prompt.")
                    return 0

            netbsd4_autostart = None
            if not plan.offer_install:
                # Whether file sharing must be started after the reboot, read
                # before the reboot as deploy does.
                command_context.require_compatibility()
                netbsd4_autostart = installed_netbsd4_autostart(
                    connection, command_context.probe_state, command_context.to_operation_callbacks(),
                )
            outcome = run_wipe(connection, plan, netbsd4_autostart=netbsd4_autostart, callbacks=callbacks)
        except WipeDiskError as exc:
            command_context.update_fields(wipe_reason=exc.reason)
            raise
        except RebootFlowError as exc:
            print(command_context.with_next_steps(str(exc), exc))
            command_context.fail_with_error(str(exc))
            return 1
        print(f"The disk is formatted as \"{outcome.volume_name}\".")
        if outcome.runtime_restart_error is not None:
            print(outcome.runtime_restart_error)
            command_context.fail_with_error(outcome.runtime_restart_error)
            return 1
        if outcome.offer_install:
            print("Run `tcapsule deploy` to install Samba on it.")
        command_context.succeed()
        return 0
    return 1
