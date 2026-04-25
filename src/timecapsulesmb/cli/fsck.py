from __future__ import annotations

import argparse
import shlex
from typing import Optional

from timecapsulesmb.cli.runtime import load_env_values, resolve_env_connection
from timecapsulesmb.core.config import require_valid_config
from timecapsulesmb.device.probe import discover_mounted_volume_conn, wait_for_ssh_state_conn
from timecapsulesmb.transport.ssh import run_ssh_conn


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
    parser = argparse.ArgumentParser(description="Run fsck_hfs on the mounted Time Capsule data disk and reboot by default.")
    parser.add_argument("--yes", action="store_true", help="Do not prompt before running fsck")
    parser.add_argument("--no-reboot", action="store_true", help="Run fsck only; do not reboot afterward")
    parser.add_argument("--no-wait", action="store_true", help="Do not wait for SSH to go down and come back after reboot")
    args = parser.parse_args(argv)

    print("Running fsck...")

    values = load_env_values()
    require_valid_config(values, profile="fsck")
    connection = resolve_env_connection(values, allow_empty_password=True)

    mounted = discover_mounted_volume_conn(connection)
    print(f"Target host: {connection.host}")
    print(f"Mounted HFS volume: {mounted.device} on {mounted.mountpoint}")

    if not args.yes:
        answer = input("This will stop file sharing, unmount the disk, run fsck_hfs, and reboot the Time Capsule. Continue? [Y/n]: ").strip().lower()
        if answer not in {"", "y", "yes"}:
            print("fsck cancelled.")
            return 0

    script = build_remote_fsck_script(mounted.device, mounted.mountpoint, reboot=not args.no_reboot)
    proc = run_ssh_conn(connection, f"/bin/sh -c {shlex.quote(script)}", check=False, timeout=240)
    if proc.stdout:
        print(proc.stdout, end="" if proc.stdout.endswith("\n") else "\n")

    if args.no_reboot:
        return 0 if proc.returncode == 0 else 1

    if args.no_wait:
        return 0

    print("Waiting for the device to go down...")
    wait_for_ssh_state_conn(connection, expected_up=False, timeout_seconds=90)
    print("Waiting for the device to come back up...")
    if not wait_for_ssh_state_conn(connection, expected_up=True, timeout_seconds=420):
        print("Timed out waiting for SSH after reboot.")
        return 1

    print("Device is back online.")
    return 0
