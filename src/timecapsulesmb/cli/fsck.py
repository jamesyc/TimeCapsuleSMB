from __future__ import annotations

import argparse
import shlex
from dataclasses import dataclass
from typing import Optional

from timecapsulesmb.cli.context import CommandContext
from timecapsulesmb.cli.flows import observe_reboot_cycle
from timecapsulesmb.cli.runtime import add_config_argument, load_env_config
from timecapsulesmb.core.config import airport_exact_display_name_from_config
from timecapsulesmb.deploy.planner import DEFAULT_APPLE_MOUNT_WAIT_SECONDS
from timecapsulesmb.device.processes import render_direct_pkill9_by_ucomm, render_direct_pkill9_watchdog
from timecapsulesmb.identity import ensure_install_id
from timecapsulesmb.device.storage import MaStVolume, mounted_mast_volumes_conn, read_mast_volumes_conn
from timecapsulesmb.telemetry import TelemetryClient
from timecapsulesmb.transport.ssh import run_ssh


FSCK_REBOOT_NO_DOWN_MESSAGE = "fsck requested reboot from the device, but SSH did not go down."
NO_MOUNTED_HFS_VOLUMES_MESSAGE = "no mounted HFS volumes found"


@dataclass(frozen=True)
class FsckTarget:
    device: str
    mountpoint: str
    name: str
    builtin: bool


def _target_from_volume(volume: MaStVolume) -> FsckTarget:
    return FsckTarget(
        device=volume.device_path,
        mountpoint=volume.volume_root,
        name=volume.name,
        builtin=volume.builtin,
    )


def _normalize_volume_selector(selector: str) -> str:
    selector = selector.strip()
    if selector.startswith("/dev/"):
        return selector.removeprefix("/dev/")
    return selector


def select_fsck_target(targets: tuple[FsckTarget, ...], selector: str | None) -> FsckTarget:
    if not targets:
        raise RuntimeError(NO_MOUNTED_HFS_VOLUMES_MESSAGE)
    if selector:
        selected_device = _normalize_volume_selector(selector)
        for target in targets:
            if target.device == selector or target.device.removeprefix("/dev/") == selected_device:
                return target
        raise RuntimeError(f"HFS volume not found: {selector}")
    if len(targets) == 1:
        return targets[0]

    print("Mounted HFS volumes:")
    for index, target in enumerate(targets, start=1):
        kind = "internal" if target.builtin else "external"
        print(f"  {index}. {target.device} on {target.mountpoint} ({target.name}, {kind})")
    while True:
        answer = input("Select a volume to fsck by number: ").strip()
        if answer.isdigit():
            index = int(answer)
            if 1 <= index <= len(targets):
                return targets[index - 1]
        print("Please enter a valid volume number.")


def build_remote_fsck_script(device: str, mountpoint: str, *, reboot: bool) -> str:
    lines = [
        render_direct_pkill9_watchdog(),
        render_direct_pkill9_by_ucomm("smbd"),
        render_direct_pkill9_by_ucomm("afpserver"),
        render_direct_pkill9_by_ucomm("wcifsnd"),
        render_direct_pkill9_by_ucomm("wcifsfs"),
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
    parser = argparse.ArgumentParser(description="Run fsck_hfs on a mounted HFS volume and reboot by default.")
    add_config_argument(parser)
    parser.add_argument("--yes", action="store_true", help="Do not prompt before running fsck")
    parser.add_argument("--no-reboot", action="store_true", help="Run fsck only; do not reboot afterward")
    parser.add_argument("--no-wait", action="store_true", help="Do not wait for SSH to go down and come back after reboot")
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
        device_name = airport_exact_display_name_from_config(config)
        command_context.set_stage("resolve_connection")
        connection = command_context.resolve_env_connection(allow_empty_password=True)

        command_context.set_stage("read_mast")
        mast_volumes = read_mast_volumes_conn(connection)
        command_context.set_stage("mount_hfs_volumes")
        mounted_volumes = mounted_mast_volumes_conn(
            connection,
            mast_volumes,
            wait_seconds=DEFAULT_APPLE_MOUNT_WAIT_SECONDS,
        )
        command_context.set_stage("select_fsck_volume")
        try:
            target = select_fsck_target(tuple(_target_from_volume(volume) for volume in mounted_volumes), args.volume)
        except RuntimeError as exc:
            raise SystemExit(str(exc)) from exc
        command_context.update_fields(fsck_device=target.device, fsck_mountpoint=target.mountpoint)
        print(f"Target host: {connection.host}")
        print(f"Mounted HFS volume: {target.device} on {target.mountpoint}")

        if not args.yes:
            command_context.set_stage("confirm_fsck")
            answer = input(f"This will stop file sharing, unmount the disk, run fsck_hfs, and reboot the {device_name}. Continue? [Y/n]: ").strip().lower()
            if answer not in {"", "y", "yes"}:
                print("fsck cancelled.")
                command_context.cancel_with_error("Cancelled by user at fsck confirmation prompt.")
                return 0

        command_context.set_stage("run_fsck")
        script = build_remote_fsck_script(target.device, target.mountpoint, reboot=not args.no_reboot)
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
