from __future__ import annotations

import argparse
from typing import Optional

from timecapsulesmb.cli.context import CommandContext
from timecapsulesmb.cli.runtime import add_config_argument, add_no_input_argument, confirm, no_input_enabled
from timecapsulesmb.core.paths import resolve_app_paths
from timecapsulesmb.identity import ensure_install_id
from timecapsulesmb.services.migrate_xattr import (
    MigrateXattrError,
    cancel_running_xattr_migration,
    inspect_xattr_migration,
    run_xattr_migration,
)
from timecapsulesmb.services.runtime import load_env_config
from timecapsulesmb.telemetry import TelemetryClient


WARNING = (
    "Metadata migration will stop TimeCapsuleSMB file sharing and disable its legacy automatic startup. "
    "Sharing stays unavailable until a new deployment succeeds. Connect external disks whose legacy metadata "
    "you need migrated; disconnected disks will not be migrated automatically later."
)


def _print_result(result) -> None:
    status = result.status
    print(f"Migration state: {status.state}")
    if status.operation_id:
        print(f"Operation: {status.operation_id}")
    if status.phase:
        print(f"Phase: {status.phase}")
    if status.entries or status.conversions or status.warnings or status.errors:
        print(
            f"Progress: entries={status.entries} conversions={status.conversions} "
            f"warnings={status.warnings} errors={status.errors}"
        )
    if status.detail:
        print(status.detail)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Explicitly migrate legacy Samba metadata into native HFS attributes.")
    add_config_argument(parser)
    add_no_input_argument(parser)
    parser.add_argument("--yes", action="store_true", help="Approve the offline transition without prompting")
    parser.add_argument("--detach", action="store_true", help="Start migration and return while it continues on the device")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--status", action="store_true", help="Show the current or durable migration state")
    group.add_argument("--cancel", action="store_true", help="Request cancellation at a safe per-file boundary")
    parser.add_argument("--mount-wait", type=int, default=30, metavar="SECONDS")
    args = parser.parse_args(argv)
    if args.mount_wait < 0:
        parser.error("--mount-wait must be non-negative")

    ensure_install_id()
    config = load_env_config(env_path=args.config)
    telemetry = TelemetryClient.from_config(config)
    paths = resolve_app_paths(config_path=args.config)
    with CommandContext(
        telemetry,
        "migrate-xattr",
        "migrate_xattr_started",
        "migrate_xattr_finished",
        config=config,
        args=args,
    ) as context:
        context.set_stage("resolve_managed_target")
        target = context.resolve_validated_managed_target(profile="deploy", include_probe=True)
        if args.status:
            context.set_stage("migration_status")
            _print_result(inspect_xattr_migration(target))
            context.succeed()
            return 0
        if args.cancel:
            context.set_stage("migration_cancel")
            _print_result(cancel_running_xattr_migration(target))
            context.succeed()
            return 0
        if not args.yes:
            if no_input_enabled(args):
                raise SystemExit("migrate-xattr requires --yes in non-interactive mode")
            if not confirm(f"{WARNING}\n\nContinue?", default=False, eof_default=False, interrupt_default=False):
                context.fail_with_error("Migration cancelled before the offline transition.")
                return 130
        try:
            result = run_xattr_migration(
                target,
                paths.distribution_root,
                callbacks=context.to_operation_callbacks(),
                follow=not args.detach,
                mount_wait_seconds=args.mount_wait,
            )
        except MigrateXattrError as exc:
            context.fail_with_error(str(exc))
            print(str(exc))
            return 1
        _print_result(result)
        if result.status.state == "complete":
            print("Metadata migration completed for the selected volumes.")
            print("Legacy TimeCapsuleSMB startup remains disabled.")
            print("The device is ready for deployment of the new runtime.")
        context.succeed()
        return 0

