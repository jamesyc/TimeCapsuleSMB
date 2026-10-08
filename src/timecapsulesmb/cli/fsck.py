from __future__ import annotations

import argparse
from typing import Optional

from timecapsulesmb.cli.context import CommandContext
from timecapsulesmb.cli.runtime import (
    add_config_argument,
    add_no_input_argument,
    no_input_enabled,
    read_terminal_line,
)
from timecapsulesmb.deploy.planner import DEFAULT_APPLE_MOUNT_WAIT_SECONDS
from timecapsulesmb.identity import ensure_install_id
from timecapsulesmb.services import storage as storage_service
from timecapsulesmb.services.activation import installed_netbsd4_autostart
from timecapsulesmb.services.maintenance import (
    format_fsck_targets,
    fsck_target_from_volume,
    FsckTarget,
    run_fsck,
    select_fsck_target,
)
from timecapsulesmb.services.reboot import RebootFlowError
from timecapsulesmb.services.runtime import load_env_config, require_device_password
from timecapsulesmb.telemetry import TelemetryClient


def prompt_fsck_target(targets: tuple[FsckTarget, ...]) -> FsckTarget:
    print(format_fsck_targets(targets))
    while True:
        answer = read_terminal_line("Select a volume to fsck by number: ").strip()
        if answer.isdigit():
            index = int(answer)
            if 1 <= index <= len(targets):
                return targets[index - 1]
        print("Please enter a valid volume number.")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run fsck_hfs on a mounted HFS volume and reboot by default.")
    add_config_argument(parser)
    parser.add_argument("--yes", action="store_true", help="Do not prompt before running fsck")
    add_no_input_argument(parser)
    parser.add_argument("--no-reboot", action="store_true", help="Run fsck only; do not reboot afterward. File sharing stays off until the next reboot")
    parser.add_argument("--no-wait", action="store_true", help="Do not wait for the device to restart and SSH to come back")
    parser.add_argument("--volume", help="HFS volume device to repair, for example dk2 or /dev/dk2")
    args = parser.parse_args(argv)

    print("Running fsck...")

    ensure_install_id()
    config = load_env_config(env_path=args.config)
    telemetry = TelemetryClient.from_config(config)
    with CommandContext(telemetry, "fsck", "fsck_started", "fsck_finished", config=config, args=args) as command_context:
        command_context.update_fields(
            reboot_was_attempted=False,
            device_came_back_after_reboot=False,
        )
        command_context.set_stage("validate_config")
        command_context.require_valid_config(profile="fsck")
        if no_input_enabled(args) and not args.yes:
            command_context.set_stage("noninteractive_confirmation")
            message = "Running `fsck` in non-interactive mode requires `--yes` to approve disk repair."
            print(message)
            command_context.fail_with_error(message)
            return 1
        command_context.set_stage("resolve_connection")
        # Key-only SSH can run fsck, but the reboot goes through AirPort ACP,
        # which needs the password: ask before the disk is touched.
        connection = command_context.resolve_env_connection(allow_empty_password=args.no_reboot)
        if connection.password:
            command_context.start_optional_airport_identity_probe(connection)
        if not args.no_reboot:
            # The reboot goes through ACP: check the password before the disk is touched.
            require_device_password(connection, command_context.to_operation_callbacks())

        mounted_volumes = storage_service.mount_mast_volumes_with_diagnostics(
            connection,
            callbacks=command_context.to_operation_callbacks(),
            wait_seconds=DEFAULT_APPLE_MOUNT_WAIT_SECONDS,
            mount_stage="mount_hfs_volumes",
        )
        command_context.set_stage("select_fsck_volume")
        targets = tuple(fsck_target_from_volume(volume) for volume in mounted_volumes)
        try:
            if not args.volume and len(targets) > 1 and not args.yes and not no_input_enabled(args):
                target = prompt_fsck_target(targets)
            else:
                target = select_fsck_target(targets, args.volume)
        except RuntimeError as exc:
            raise SystemExit(str(exc)) from exc
        command_context.update_fields(fsck_device=target.device, fsck_mountpoint=target.mountpoint)
        print(f"Target host: {connection.host}")
        print(f"Mounted HFS volume: {target.device} on {target.mountpoint}")

        if not args.yes:
            command_context.set_stage("confirm_fsck")
            device_name = command_context.optional_airport_display_name(timeout_seconds=0.1)
            if args.no_reboot:
                confirmation = (
                    "This will stop file sharing, unmount the disk, and run fsck_hfs. "
                    f"File sharing stays off until the {device_name} restarts. Continue?"
                )
            else:
                confirmation = f"This will stop file sharing, unmount the disk, run fsck_hfs, and reboot the {device_name}. Continue?"
            proceed = command_context.confirm_or_fail(
                confirmation,
                default=True,
                noninteractive_message="Running `fsck` requires confirmation when stdin is not interactive. Use `fsck --yes` in a non-interactive environment.",
                allow_prompt=not no_input_enabled(args),
            )
            if proceed is None:
                return 1
            if not proceed:
                print("fsck cancelled.")
                command_context.cancel_with_error("Cancelled by user at fsck confirmation prompt.")
                return 0

        netbsd4_autostart = None
        if not args.no_reboot:
            # Whether file sharing must be started after the reboot, read
            # before the reboot as deploy does.
            command_context.require_compatibility()
            netbsd4_autostart = installed_netbsd4_autostart(
                connection, command_context.probe_state, command_context.to_operation_callbacks(),
            )
        try:
            outcome = run_fsck(
                connection,
                target,
                reboot=not args.no_reboot,
                wait=not args.no_wait,
                callbacks=command_context.to_operation_callbacks(),
                netbsd4_autostart=netbsd4_autostart,
            )
        except RebootFlowError as exc:
            print(str(exc))
            command_context.fail_with_error(str(exc))
            return 1
        if outcome.failure is not None:
            print(outcome.failure)
            command_context.fail_with_error(outcome.failure)
            return 1
        command_context.succeed()
        return 0
    return 1
