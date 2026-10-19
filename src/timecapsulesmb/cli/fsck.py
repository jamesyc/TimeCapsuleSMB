from __future__ import annotations

import argparse
import dataclasses
import threading
import time
from collections.abc import Callable
from typing import Optional

from timecapsulesmb.cli.context import CommandContext
from timecapsulesmb.cli.runtime import (
    add_config_argument,
    add_no_input_argument,
    confirm,
    confirm_despite_failing_disk,
    no_input_enabled,
    read_terminal_line,
)
from timecapsulesmb.deploy.planner import DEFAULT_APPLE_MOUNT_WAIT_SECONDS
from timecapsulesmb.identity import ensure_install_id
from timecapsulesmb.services import storage as storage_service
from timecapsulesmb.services.activation import installed_netbsd4_autostart
from timecapsulesmb.services.maintenance import (
    FSCK_DURATION_WARNING,
    FSCK_STARTED_LINE_PREFIX,
    FSCK_STATUS_PREFIX,
    format_fsck_targets,
    fsck_target_from_volume,
    FsckTarget,
    run_fsck,
    select_fsck_target,
)
from timecapsulesmb.services.reboot import RebootFlowError
from timecapsulesmb.services.runtime import load_env_config, require_acpd_ready
from timecapsulesmb.telemetry import TelemetryClient


FSCK_STILL_RUNNING_SECONDS = 60


class FsckStillRunningNotice:
    """Prints a line for each minute fsck_hfs runs without printing one.

    One step of a large disk can take most of an hour, and a silent terminal
    looks like a hang. Pass every log line through `log`; a thread calls
    `tick` until the context exits.
    """

    def __init__(
        self,
        log: Callable[[str], None],
        *,
        interval: float = FSCK_STILL_RUNNING_SECONDS,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._log = log
        self._interval = interval
        self._now = now
        self._lock = threading.Lock()
        # When fsck_hfs started; None before it starts and after it reports.
        self._started_at: float | None = None
        self._last_line = 0.0
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._run, name="fsck-still-running", daemon=True)

    def __enter__(self) -> FsckStillRunningNotice:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._done.set()
        self._thread.join()

    def log(self, message: str) -> None:
        with self._lock:
            current = self._now()
            if message.startswith(FSCK_STARTED_LINE_PREFIX):
                self._started_at = current
            elif message.startswith(FSCK_STATUS_PREFIX):
                self._started_at = None
            self._last_line = current
            self._log(message)

    def tick(self) -> None:
        with self._lock:
            if self._started_at is None:
                return
            current = self._now()
            if current - self._last_line < self._interval:
                return
            minutes = int((current - self._started_at) // 60)
            self._log(f"fsck_hfs is still running ({minutes} min).")
            self._last_line = current

    def _run(self) -> None:
        while not self._done.wait(1.0):
            self.tick()


def prompt_fsck_target(targets: tuple[FsckTarget, ...]) -> FsckTarget:
    print(format_fsck_targets(targets))
    while True:
        answer = read_terminal_line(
            "Select a volume to fsck by number: ",
            noninteractive_message="No volume was chosen because no input was read. Rerun with --volume, for example: tcapsule fsck --volume dk2",
        ).strip()
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
            # The reboot goes through ACP: check ACPd and the password before the disk is touched.
            require_acpd_ready(connection, command_context.to_operation_callbacks(),
                               reading=command_context.reading_at(connection))

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
        print(FSCK_DURATION_WARNING)

        # A failing disk's warning asks instead of the usual question.
        proceed = confirm_despite_failing_disk(
            connection, "repair", yes=args.yes, command="fsck", update_fields=command_context.update_fields,
        )
        if proceed is False:
            print("fsck cancelled.")
            command_context.cancel_with_error("Cancelled by user at the failing-disk warning.")
            return 0
        if proceed is None and not args.yes:
            command_context.set_stage("confirm_fsck")
            device_name = command_context.optional_airport_display_name(timeout_seconds=0.1)
            if args.no_reboot:
                confirmation = (
                    "This will stop file sharing, unmount the disk, and run fsck_hfs. "
                    f"File sharing stays off until the {device_name} restarts. Continue?"
                )
            else:
                confirmation = f"This will stop file sharing, unmount the disk, run fsck_hfs, and reboot the {device_name}. Continue?"
            proceed = confirm(
                confirmation,
                default=True,
                noninteractive_message="No answer was read for the fsck confirmation. Use `fsck --yes` to skip the prompt.",
            )
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
        callbacks = command_context.to_operation_callbacks()
        try:
            with FsckStillRunningNotice(print) as notice:
                outcome = run_fsck(
                    connection,
                    target,
                    reboot=not args.no_reboot,
                    wait=not args.no_wait,
                    callbacks=dataclasses.replace(callbacks, log=notice.log),
                    netbsd4_autostart=netbsd4_autostart,
                )
        except RebootFlowError as exc:
            print(command_context.with_next_steps(str(exc), exc))
            command_context.fail_with_error(str(exc))
            return 1
        if outcome.failure is not None:
            print(outcome.failure)
            command_context.fail_with_error(outcome.failure)
            return 1
        command_context.succeed()
        return 0
    return 1
