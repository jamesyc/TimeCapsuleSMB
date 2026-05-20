from __future__ import annotations

import argparse
import shlex
from typing import Optional

from timecapsulesmb.cli.context import CommandContext
from timecapsulesmb.cli.flows import observe_reboot_cycle
from timecapsulesmb.cli.runtime import add_config_argument, add_mount_wait_argument, add_no_wait_argument, load_env_config
from timecapsulesmb.identity import ensure_install_id
from timecapsulesmb.services.maintenance import (
    FSCK_REMOTE_COMMAND_TIMEOUT_SECONDS,
    FSCK_REBOOT_NO_DOWN_MESSAGE,
    build_remote_fsck_script,
    format_fsck_plan,
    format_fsck_targets,
    fsck_target_from_volume,
    select_fsck_target,
)
from timecapsulesmb.telemetry import TelemetryClient
from timecapsulesmb.transport.ssh import run_ssh


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run fsck_hfs on a mounted HFS volume and reboot by default.")
    add_config_argument(parser)
    add_mount_wait_argument(parser)
    parser.add_argument("--yes", action="store_true", help="Do not prompt before running fsck")
    parser.add_argument("--dry-run", action="store_true", help="Print the selected fsck target and actions without making changes")
    parser.add_argument("--list-volumes", action="store_true", help="List mounted HFS volumes that can be selected for fsck")
    parser.add_argument("--no-reboot", action="store_true", help="Run fsck only; do not reboot afterward")
    add_no_wait_argument(parser)
    parser.add_argument("--volume", help="HFS volume device to repair, for example dk2 or /dev/dk2")
    args = parser.parse_args(argv)

    if args.dry_run and args.list_volumes:
        parser.error("--dry-run and --list-volumes are mutually exclusive")

    if not args.dry_run and not args.list_volumes:
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
        command_context.set_stage("resolve_connection")
        connection = command_context.resolve_env_connection(allow_empty_password=True)
        if connection.password:
            command_context.start_optional_airport_identity_probe(connection)

        mounted_volumes = command_context.mount_mast_volumes(
            connection,
            wait_seconds=args.mount_wait,
            mount_stage="mount_hfs_volumes",
        )
        targets = tuple(fsck_target_from_volume(volume) for volume in mounted_volumes)
        if args.list_volumes:
            command_context.set_stage("list_fsck_volumes")
            print(format_fsck_targets(targets))
            command_context.succeed()
            return 0

        command_context.set_stage("select_fsck_volume")
        try:
            target = select_fsck_target(
                targets,
                args.volume,
                prompt=not args.yes and not args.dry_run,
            )
        except RuntimeError as exc:
            raise SystemExit(str(exc)) from exc
        command_context.update_fields(fsck_device=target.device, fsck_mountpoint=target.mountpoint)
        print(f"Target host: {connection.host}")
        print(f"Mounted HFS volume: {target.device} on {target.mountpoint}")

        if args.dry_run:
            print(format_fsck_plan(target, reboot=not args.no_reboot, wait=not args.no_wait))
            command_context.succeed()
            return 0

        if not args.yes:
            command_context.set_stage("confirm_fsck")
            device_name = command_context.optional_airport_display_name(timeout_seconds=0.1)
            proceed = command_context.confirm_or_fail(
                f"This will stop file sharing, unmount the disk, run fsck_hfs, and reboot the {device_name}. Continue?",
                default=True,
                noninteractive_message="Running `fsck` requires confirmation when stdin is not interactive. Use `fsck --yes` in a non-interactive environment.",
            )
            if proceed is None:
                return 1
            if not proceed:
                print("fsck cancelled.")
                command_context.cancel_with_error("Cancelled by user at fsck confirmation prompt.")
                return 0

        command_context.set_stage("run_fsck")
        script = build_remote_fsck_script(target.device, target.mountpoint, reboot=not args.no_reboot)
        proc = run_ssh(connection, f"/bin/sh -c {shlex.quote(script)}", check=False, timeout=FSCK_REMOTE_COMMAND_TIMEOUT_SECONDS)
        if proc.stdout:
            print(proc.stdout, end="" if proc.stdout.endswith("\n") else "\n")

        if args.no_reboot:
            if proc.returncode == 0:
                command_context.succeed()
                return 0
            command_context.fail_with_error("fsck_hfs command failed.")
            return 1

        command_context.update_fields(reboot_was_attempted=True)
        if args.no_wait:
            print("Reboot requested; not waiting for the device to go down or come back.")
            command_context.succeed()
            return 0

        if not observe_reboot_cycle(
            connection,
            command_context,
            reboot_no_down_message=FSCK_REBOOT_NO_DOWN_MESSAGE,
            down_timeout_seconds=90,
            up_timeout_seconds=420,
        ):
            return 1

        command_context.succeed()
        return 0
    return 1
