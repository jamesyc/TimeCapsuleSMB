from __future__ import annotations

import argparse
import shlex
from typing import Optional

from timecapsulesmb.cli.context import CommandContext
from timecapsulesmb.cli.runtime import load_env_config
from timecapsulesmb.core.config import airport_exact_display_name_from_config
from timecapsulesmb.identity import ensure_install_id
from timecapsulesmb.device.probe import discover_mounted_volume_conn, wait_for_ssh_state_conn
from timecapsulesmb.telemetry import TelemetryClient
from timecapsulesmb.transport.ssh import run_ssh


def build_remote_fsck_script(device: str, mountpoint: str, *, reboot: bool) -> str:
    lines = [
        "/usr/bin/pkill -f [w]atchdog.sh >/dev/null 2>&1 || true",
        "/usr/bin/pkill smbd >/dev/null 2>&1 || true",
        "/usr/bin/pkill afpserver >/dev/null 2>&1 || true",
        "/usr/bin/pkill wcifsnd >/dev/null 2>&1 || true",
        "/usr/bin/pkill wcifsfs >/dev/null 2>&1 || true",
        "sleep 2",
        f"/sbin/umount -f {shlex.quote(mountpoint)} >/dev/null 2>&1 || true",
        f"echo '--- fsck_hfs {device} ---'",
        f"/sbin/fsck_hfs -fy {shlex.quote(device)} 2>&1 || true",
    ]
    if reboot:
        lines.extend(
            [
                "echo '--- reboot ---'",
                "/sbin/reboot >/dev/null 2>&1 || true",
            ]
        )
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run fsck_hfs on the mounted device data disk and reboot by default.")
    parser.add_argument("--yes", action="store_true", help="Do not prompt before running fsck")
    parser.add_argument("--no-reboot", action="store_true", help="Run fsck only; do not reboot afterward")
    parser.add_argument("--no-wait", action="store_true", help="Do not wait for SSH to go down and come back after reboot")
    args = parser.parse_args(argv)

    print("Running fsck...")

    ensure_install_id()
    config = load_env_config()
    telemetry = TelemetryClient.from_config(config)
    with CommandContext(telemetry, "fsck", "fsck_started", "fsck_finished", config=config, args=args) as command_context:
        command_context.update_fields(
            reboot_was_attempted=False,
            device_came_back_after_reboot=False,
        )
        command_context.set_stage("validate_config")
        command_context.require_valid_config(profile="fsck")
        device_name = airport_exact_display_name_from_config(config)
        command_context.set_stage("resolve_connection")
        connection = command_context.resolve_env_connection(allow_empty_password=True)

        command_context.set_stage("discover_mounted_volume")
        mounted = discover_mounted_volume_conn(connection)
        command_context.update_fields(fsck_device=mounted.device, fsck_mountpoint=mounted.mountpoint)
        print(f"Target host: {connection.host}")
        print(f"Mounted HFS volume: {mounted.device} on {mounted.mountpoint}")

        if not args.yes:
            command_context.set_stage("confirm_fsck")
            answer = input(f"This will stop file sharing, unmount the disk, run fsck_hfs, and reboot the {device_name}. Continue? [Y/n]: ").strip().lower()
            if answer not in {"", "y", "yes"}:
                print("fsck cancelled.")
                command_context.cancel_with_error("Cancelled by user at fsck confirmation prompt.")
                return 0

        command_context.set_stage("run_fsck")
        script = build_remote_fsck_script(mounted.device, mounted.mountpoint, reboot=not args.no_reboot)
        proc = run_ssh(connection, f"/bin/sh -c {shlex.quote(script)}", check=False, timeout=240)
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
            command_context.succeed()
            return 0

        print("Waiting for the device to go down...")
        command_context.set_stage("wait_for_reboot_down")
        wait_for_ssh_state_conn(connection, expected_up=False, timeout_seconds=90)
        print("Waiting for the device to come back up...")
        command_context.set_stage("wait_for_reboot_up")
        if not wait_for_ssh_state_conn(connection, expected_up=True, timeout_seconds=420):
            print("Timed out waiting for SSH after reboot.")
            command_context.fail_with_error("Timed out waiting for SSH after reboot.")
            return 1

        command_context.update_fields(device_came_back_after_reboot=True)
        print("Device is back online.")
        command_context.succeed()
        return 0
    return 1
