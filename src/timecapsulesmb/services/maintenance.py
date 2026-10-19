from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import re
import shlex
import time

from timecapsulesmb.core.config import MANAGED_PAYLOAD_DIR_NAME
from timecapsulesmb.core.error_codes import ErrorCode
from timecapsulesmb.deploy.commands import managed_stop_actions, render_remote_actions
from timecapsulesmb.deploy.planner import UninstallPlan, build_runtime_start_actions, build_uninstall_plan
from timecapsulesmb.deploy.verify import render_post_uninstall_verification, verify_post_uninstall
from timecapsulesmb.device.disk_errors import DiskFailingError
from timecapsulesmb.device.errors import DeviceError, DiskPayloadError, RsyncNotRunningError
from timecapsulesmb.device.processes import render_process_present_by_ucomm
from timecapsulesmb.device.storage import UNINSTALL_DRY_RUN_VOLUME_ROOT_PLACEHOLDER, MaStVolume
from timecapsulesmb.services import storage as storage_service
from timecapsulesmb.services.activation import (
    MANUAL_START_AFTER_REBOOT_MESSAGE,
    RESTART_AFTER_REBOOT_MESSAGES,
    RUNTIME_RESTART_FAILURE_MESSAGE,
    start_netbsd4_runtime_after_reboot,
)
from timecapsulesmb.services.callbacks import OperationCallbacks
from timecapsulesmb.services.reboot import RebootFlowError, followed, reboot_device
from timecapsulesmb.transport.errors import TransportError
from timecapsulesmb.transport.ssh import SshConnection, run_ssh


FSCK_REMOTE_COMMAND_TIMEOUT_SECONDS = 3 * 60 * 60
UNINSTALL_REBOOT_NO_DOWN_MESSAGE = (
    "Reboot was requested but the device did not restart. "
    "The uninstall removed managed TimeCapsuleSMB files before the reboot."
)
UNINSTALL_FILES_REMAIN_MESSAGE = "Managed TimeCapsuleSMB files are still present after reboot."
FSCK_STATUS_PREFIX = "tcapsule-fsck: fsck_hfs exit status "
FSCK_DID_NOT_RUN_MESSAGE = (
    "fsck did not run: file sharing could not be stopped, or the connection "
    "ended before fsck_hfs finished."
)
FSCK_NOT_UNMOUNTED_LINE = "tcapsule-fsck: volume not unmounted"
FSCK_NOT_UNMOUNTED_MESSAGE = (
    "fsck did not run: the volume could not be confirmed unmounted. "
    "File sharing stays off until the device restarts."
)
FSCK_ALREADY_RUNNING_LINE = "tcapsule-fsck: fsck_hfs already running"
FSCK_ALREADY_RUNNING_MESSAGE = (
    "fsck did not run: fsck_hfs from an earlier disk repair is still running on the device."
)
FSCK_SESSION_DROPPED_MESSAGE = (
    "The connection to the device ended while fsck_hfs was running. The repair keeps running on the device."
)
FSCK_STARTED_LINE_PREFIX = "--- fsck_hfs "
FSCK_DURATION_WARNING = (
    "This can take an hour or more on a large disk. "
    "Keep the Time Capsule plugged in until it finishes."
)
# fsck_hfs prints a line for each damaged file; past this many, the log
# keeps only its "** " step lines and the script's own.
FSCK_DETAIL_LINE_LIMIT = 200
# fsck_hfs's "** " lines that start a step, as the progress phase they report.
# A repair runs every check again after "Rechecking volume.", and may repair
# and recheck more than once.
FSCK_PHASES = (
    (re.compile(r"Checking (Journaled )?HFS.* volume\.", re.IGNORECASE), "start"),
    (re.compile(r"Checking Extents Overflow file\.", re.IGNORECASE), "extents"),
    (re.compile(r"Checking Catalog file\.", re.IGNORECASE), "catalog"),
    (re.compile(r"Checking multi-linked (files|directories)\.", re.IGNORECASE), "multilinked"),
    (re.compile(r"Checking Catalog hierarchy\.", re.IGNORECASE), "hierarchy"),
    (re.compile(r"Checking Extended Attributes file\.", re.IGNORECASE), "attributes"),
    (re.compile(r"Checking volume bitmap\.", re.IGNORECASE), "bitmap"),
    (re.compile(r"Checking volume information\.", re.IGNORECASE), "volume_info"),
    (re.compile(r"Repairing volume\.", re.IGNORECASE), "repairing"),
    (re.compile(r"Rechecking volume\.", re.IGNORECASE), "rechecking"),
)

NO_MOUNTED_HFS_VOLUMES_MESSAGE = "no mounted HFS volumes found"
MULTIPLE_MOUNTED_HFS_VOLUMES_MESSAGE = "multiple mounted HFS volumes found; specify --volume to select one"


@dataclass(frozen=True)
class FsckTarget:
    device: str
    mountpoint: str
    name: str
    builtin: bool


def fsck_target_from_volume(volume: MaStVolume) -> FsckTarget:
    return FsckTarget(
        device=volume.device_path,
        mountpoint=volume.volume_root,
        name=volume.name,
        builtin=volume.builtin,
    )


def normalize_volume_selector(selector: str) -> str:
    selector = selector.strip()
    if selector.startswith("/dev/"):
        return selector.removeprefix("/dev/")
    return selector


def select_fsck_target(targets: tuple[FsckTarget, ...], selector: str | None) -> FsckTarget:
    if not targets:
        raise RuntimeError(NO_MOUNTED_HFS_VOLUMES_MESSAGE)
    if selector:
        selected_device = normalize_volume_selector(selector)
        for target in targets:
            if target.device == selector or target.device.removeprefix("/dev/") == selected_device:
                return target
        raise RuntimeError(f"HFS volume not found: {selector}")
    if len(targets) == 1:
        return targets[0]
    raise RuntimeError(MULTIPLE_MOUNTED_HFS_VOLUMES_MESSAGE)


def fsck_target_to_jsonable(target: FsckTarget) -> dict[str, object]:
    return {
        "device": target.device,
        "mountpoint": target.mountpoint,
        "name": target.name,
        "builtin": target.builtin,
    }


def format_fsck_targets(targets: tuple[FsckTarget, ...]) -> str:
    lines = ["Mounted HFS volumes:"]
    if not targets:
        lines.append("  none")
        return "\n".join(lines)
    for index, target in enumerate(targets, start=1):
        kind = "internal" if target.builtin else "external"
        lines.append(f"  {index}. {target.device} on {target.mountpoint} ({target.name}, {kind})")
    return "\n".join(lines)


def fsck_plan_to_jsonable(target: FsckTarget, *, reboot: bool, wait: bool) -> dict[str, object]:
    return {
        "target": fsck_target_to_jsonable(target),
        "device": target.device,
        "mountpoint": target.mountpoint,
        "reboot_required": reboot,
        "wait_after_reboot": bool(reboot and wait),
    }


def format_fsck_plan(target: FsckTarget, *, reboot: bool, wait: bool) -> str:
    lines = [
        "Dry run: fsck plan",
        "",
        "Target:",
        f"  device: {target.device}",
        f"  mountpoint: {target.mountpoint}",
        f"  name: {target.name}",
        f"  type: {'internal' if target.builtin else 'external'}",
        "",
        "Actions:",
        "  stop managed file sharing processes",
        f"  unmount: {target.mountpoint}",
        f"  run: /sbin/fsck_hfs -fy {target.device}",
        "",
        "Reboot:",
        f"  {'yes' if reboot else 'no'}",
    ]
    if reboot:
        lines.append(f"  follow-up: {'wait for the ACP uptime (syUT) to restart, then SSH up' if wait else 'do not wait'}")
    return "\n".join(lines)


def build_remote_fsck_script(device: str, mountpoint: str) -> str:
    # The host gives this script a terminal so that fsck_hfs writes each step
    # as it starts. A dropped session would then hang up the terminal, so
    # ignore that: fsck_hfs, which inherits it, must not stop mid-repair.
    lines = ["trap '' HUP"]
    # That fsck_hfs then outlives the session that started it. A second one on
    # the same disk would repair it while the first still writes, so touch
    # nothing while any runs (or when the process list cannot be read).
    lines.append(
        f"if ( {render_process_present_by_ucomm('fsck_hfs')} ); then "
        f"echo '{FSCK_ALREADY_RUNNING_LINE}'; exit 1; fi"
    )
    # Never repair a volume the manager could remount or smbd could write:
    # abort unless every managed process has stopped. AFP could write it too,
    # so afpserver stops even without a reboot; file sharing then stays off
    # until the next reboot either way.
    lines += [
        f"( {command} ) || exit 1"
        for command in render_remote_actions(managed_stop_actions(stop_afpserver=True))
    ]
    # umount can fail harmlessly when Apple already unmounted the disk to save
    # power, so its status is not the test: the mount table is. A volume still
    # mounted (anywhere) must never be repaired. Stopping here skips the reboot
    # too, like the stop failure above; file sharing stays off until then.
    # The status line in the output is what reports fsck's result.
    lines += [
        f"/sbin/umount -f {shlex.quote(mountpoint)} 2>&1",
        f"mounts=$(/sbin/mount) || {{ echo '{FSCK_NOT_UNMOUNTED_LINE}'; exit 1; }}",
        'case "\n$mounts" in',
        f'    *"\n"{shlex.quote(device + " on ")}*) echo \'{FSCK_NOT_UNMOUNTED_LINE}\'; exit 1 ;;',
        "esac",
        f"echo '{FSCK_STARTED_LINE_PREFIX}{device} ---'",
        f"/sbin/fsck_hfs -fy {shlex.quote(device)} 2>&1",
        "fsck_status=$?",
        f'echo "{FSCK_STATUS_PREFIX}$fsck_status"',
    ]
    # The reboot that restarts file sharing is requested from the host once
    # this script has reported, through the same ACP request as every flow.
    lines.append('exit "$fsck_status"')
    return "\n".join(lines)


def fsck_phase(line: str) -> str | None:
    """The progress phase a fsck_hfs output line starts, if it starts one."""
    if not line.startswith("** "):
        return None
    text = line[3:].strip()
    for pattern, phase in FSCK_PHASES:
        if pattern.fullmatch(text):
            return phase
    return None


@dataclass
class FsckOutputLog:
    """Logs the remote fsck script's output as it arrives and reports fsck_hfs's steps.

    Each step is a progress event with its phase and whether it rechecks a
    repaired volume, and is timed for telemetry. A damaged disk
    prints a line per file: the log keeps the first FSCK_DETAIL_LINE_LIMIT.
    """

    callbacks: OperationCallbacks
    now: Callable[[], float] = time.monotonic
    started: bool = False
    recheck: bool = False
    phase_seconds: dict[str, float] = field(default_factory=dict)
    detail_lines: int = 0
    hidden_lines: int = 0
    _phase_key: str | None = None
    _phase_start: float = 0.0

    def line(self, text: str) -> None:
        if text.startswith(FSCK_STARTED_LINE_PREFIX):
            self.started = True
        phase = fsck_phase(text)
        if phase is not None:
            self._start_phase(phase)
        if not self.started or text.startswith(("** ", FSCK_STATUS_PREFIX, FSCK_STARTED_LINE_PREFIX)):
            self.callbacks.message(text)
            return
        self.detail_lines += 1
        if self.detail_lines <= FSCK_DETAIL_LINE_LIMIT:
            self.callbacks.message(text)
        else:
            self.hidden_lines += 1

    def finish(self, status: int | None) -> None:
        self._end_phase()
        if self.hidden_lines:
            self.callbacks.message(f"{self.hidden_lines} more fsck_hfs lines were not logged.")
        self.callbacks.measurement(
            "fsck_run",
            started=self.started,
            last_phase=self._phase_key,
            phase_seconds={key: round(seconds, 1) for key, seconds in self.phase_seconds.items()},
            # fsck_hfs started but its result never arrived: the connection
            # dropped or the device restarted while it ran.
            session_dropped=self.started and status is None,
            detail_lines=self.detail_lines,
        )

    def _start_phase(self, phase: str) -> None:
        self._end_phase()
        if phase == "rechecking":
            self.recheck = True
        self._phase_key = f"recheck_{phase}" if self.recheck and phase != "rechecking" else phase
        self._phase_start = self.now()
        self.callbacks.progress("run_fsck", phase=phase, recheck=self.recheck and phase != "rechecking")

    def _end_phase(self) -> None:
        if self._phase_key is not None:
            elapsed = self.now() - self._phase_start
            self.phase_seconds[self._phase_key] = self.phase_seconds.get(self._phase_key, 0.0) + elapsed


def fsck_exit_status(output: str) -> int | None:
    """Return fsck_hfs's exit status from the remote script output.

    None means fsck never ran: stopping file sharing failed (the script exits
    before unmounting or rebooting), or the session ended before fsck finished.
    """
    for line in reversed(output.splitlines()):
        line = line.strip()
        if line.startswith(FSCK_STATUS_PREFIX):
            value = line.removeprefix(FSCK_STATUS_PREFIX)
            return int(value) if value.isdigit() else None
    return None


def fsck_failure_code(status: int | None, output: str = "", *, started: bool = False) -> ErrorCode | None:
    """The error code of a run without a status line, when it has its own.

    `started` says fsck_hfs started: its result was lost, not never made.
    """
    if status is not None:
        return None
    if any(line.strip() == FSCK_ALREADY_RUNNING_LINE for line in output.splitlines()):
        return ErrorCode.FSCK_ALREADY_RUNNING
    if started:
        return ErrorCode.FSCK_SESSION_DROPPED
    if any(line.strip() == FSCK_NOT_UNMOUNTED_LINE for line in output.splitlines()):
        return ErrorCode.FSCK_VOLUME_NOT_UNMOUNTED
    return None


def fsck_failure_message(status: int | None, output: str = "", *, started: bool = False) -> str | None:
    if status is None:
        code = fsck_failure_code(status, output, started=started)
        if code == ErrorCode.FSCK_ALREADY_RUNNING:
            return FSCK_ALREADY_RUNNING_MESSAGE
        if code == ErrorCode.FSCK_SESSION_DROPPED:
            return FSCK_SESSION_DROPPED_MESSAGE
        if code == ErrorCode.FSCK_VOLUME_NOT_UNMOUNTED:
            return FSCK_NOT_UNMOUNTED_MESSAGE
        return FSCK_DID_NOT_RUN_MESSAGE
    if status != 0:
        return f"fsck_hfs exited with status {status}; the disk may still need repair."
    return None


@dataclass(frozen=True)
class FsckOutcome:
    # None when fsck_hfs never ran; see fsck_exit_status.
    status: int | None
    failure: str | None
    reboot_requested: bool
    waited: bool
    # Set when file sharing was to be started after the reboot and did not start.
    runtime_restart_error: str | None = None
    # The error's own code when one explains it better than "did not restart":
    # no payload on any disk, which only a reinstall fixes.
    runtime_restart_code: str | None = None
    # fsck_failure_code's: why a run without a status line has no result.
    failure_code: str | None = None


def run_fsck(
    connection: SshConnection,
    target: FsckTarget,
    *,
    reboot: bool,
    wait: bool,
    callbacks: OperationCallbacks,
    netbsd4_autostart: bool | None = None,
    now: Callable[[], float] = time.monotonic,
) -> FsckOutcome:
    """Run the remote fsck script, log its output as it runs, then reboot if asked.

    `netbsd4_autostart` is services.activation.installed_netbsd4_autostart,
    read before the repair: None when nothing needs starting after the reboot.
    Otherwise file sharing is started after a waited reboot as deploy starts
    it, so the repair leaves sharing on as Apple's own reboot does. Raises
    RebootFlowError when the reboot request fails or the device does not
    restart or come back.
    """
    callbacks.stage("run_fsck")
    script = build_remote_fsck_script(target.device, target.mountpoint)
    log = FsckOutputLog(callbacks, now=now)
    status = None
    returncode = None
    output = ""
    try:
        # fsck_hfs buffers its output on a pipe until it exits; on a terminal
        # each step arrives as it starts.
        proc = run_ssh(
            connection, f"/bin/sh -c {shlex.quote(script)}", check=False,
            timeout=FSCK_REMOTE_COMMAND_TIMEOUT_SECONDS, tty=True, on_line=log.line,
        )
        output = proc.stdout or ""
        returncode = proc.returncode
        status = fsck_exit_status(output)
    except TransportError as exc:
        # Once fsck_hfs has started, a lost session (or the command timeout)
        # does not stop it: report that, not a connection failure.
        if not log.started:
            raise
        callbacks.debug(fsck_transport_error=str(exc))
    finally:
        log.finish(status)
    callbacks.update(returncode=status if status is not None else returncode)
    # Without a status line fsck never ran (or its result was lost), so the
    # volume may still be mounted: do not reboot. A failed repair still
    # reboots: that is what restarts file sharing. If the session dropped
    # after fsck finished, sharing stays off until the user restarts, as with
    # --no-reboot.
    rebooting = reboot and status is not None
    failure = fsck_failure_message(status, output, started=log.started)
    try:
        if rebooting:
            if not wait and netbsd4_autostart is False:
                # Without the wait nothing starts file sharing after the reboot.
                callbacks.message(MANUAL_START_AFTER_REBOOT_MESSAGE)
            connection = followed(connection, reboot_device(
                connection.host,
                connection.password,
                wait=wait,
                callbacks=callbacks,
                start_timeout_seconds=120,
                ssh=connection,
            ))
    except RebootFlowError as exc:
        if failure is None:
            raise
        # A failed reboot must not hide a failed repair. The reboot error
        # stays first so its known message prefixes still match.
        raise RebootFlowError(f"{exc}\n{failure}", code=exc.code) from exc
    runtime_restart_error = None
    runtime_restart_code = None
    if rebooting and wait and netbsd4_autostart is not None:
        # A failed repair still starts file sharing: the reboot does on every
        # device that starts it by itself.
        runtime_restarted = False
        try:
            start_netbsd4_runtime_after_reboot(
                connection,
                build_runtime_start_actions(),
                rc_local_autostart=netbsd4_autostart,
                callbacks=callbacks,
                messages=RESTART_AFTER_REBOOT_MESSAGES,
            )
        except (DiskPayloadError, DiskFailingError, RsyncNotRunningError) as exc:
            runtime_restart_error = str(exc)
            runtime_restart_code = exc.code
            # File sharing did restart when only rsync is off.
            runtime_restarted = not exc.samba_off
        except DeviceError as exc:
            runtime_restart_error = str(exc)
        except TransportError as exc:
            runtime_restart_error = f"{RUNTIME_RESTART_FAILURE_MESSAGE} {exc}"
        else:
            runtime_restarted = True
            callbacks.message("File sharing is running again after the reboot.")
        callbacks.update(runtime_restarted=runtime_restarted)
    if runtime_restart_error is not None:
        failure = (
            f"{failure}\n{runtime_restart_error}"
            if failure is not None
            else f"Disk repair completed. {runtime_restart_error}"
        )
    elif failure is not None and rebooting:
        # The repair's failure is what this run reports, not the reboot or
        # the start that followed it.
        callbacks.stage("run_fsck")
    return FsckOutcome(
        status=status,
        failure=failure,
        reboot_requested=rebooting,
        waited=rebooting and wait,
        runtime_restart_error=runtime_restart_error,
        runtime_restart_code=runtime_restart_code,
        failure_code=fsck_failure_code(status, output, started=log.started),
    )


def prepare_uninstall(
    connection: SshConnection,
    *,
    dry_run: bool,
    reboot: bool,
    wait: bool,
    mount_wait: int,
    callbacks: OperationCallbacks,
) -> UninstallPlan:
    """Mount the HFS volumes (placeholders for a dry run) and plan the removal."""
    if dry_run:
        volume_roots = [UNINSTALL_DRY_RUN_VOLUME_ROOT_PLACEHOLDER]
    else:
        mounted_volumes = storage_service.mount_mast_volumes_with_diagnostics(
            connection,
            callbacks=callbacks,
            wait_seconds=mount_wait,
        )
        volume_roots = [volume.volume_root for volume in mounted_volumes]
    payload_dirs = [f"{volume_root}/{MANAGED_PAYLOAD_DIR_NAME}" for volume_root in volume_roots]
    callbacks.update(volume_roots=volume_roots, payload_dirs=payload_dirs)
    callbacks.stage("build_uninstall_plan")
    return build_uninstall_plan(
        connection.host,
        volume_roots,
        payload_dirs,
        reboot_after_uninstall=reboot,
        wait_after_reboot=wait,
    )


def reboot_after_uninstall(connection: SshConnection, plan: UninstallPlan, *, callbacks: OperationCallbacks) -> bool:
    """Reboot as the plan asks and, after a waited reboot, verify the removal.

    Returns whether the removal was verified. Raises RebootFlowError when the
    reboot fails, and DeviceError when managed files survive the reboot.
    """
    if not plan.reboot_required:
        return False
    connection = followed(connection, reboot_device(
        connection.host,
        connection.password,
        wait=plan.wait_after_reboot,
        callbacks=callbacks,
        no_down_message=UNINSTALL_REBOOT_NO_DOWN_MESSAGE,
        ssh=connection,
    ))
    if not plan.wait_after_reboot:
        return False
    callbacks.stage("verify_post_uninstall")
    verification = verify_post_uninstall(connection, plan)
    for line in render_post_uninstall_verification(verification):
        callbacks.message(line)
    if not verification:
        raise DeviceError(UNINSTALL_FILES_REMAIN_MESSAGE)
    return True
