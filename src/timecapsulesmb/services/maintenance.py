from __future__ import annotations

from dataclasses import dataclass
import shlex

from timecapsulesmb.core.config import MANAGED_PAYLOAD_DIR_NAME
from timecapsulesmb.deploy.commands import managed_stop_actions, render_remote_actions
from timecapsulesmb.deploy.planner import UninstallPlan, build_runtime_start_actions, build_uninstall_plan
from timecapsulesmb.deploy.verify import render_post_uninstall_verification, verify_post_uninstall
from timecapsulesmb.device.errors import DeviceError
from timecapsulesmb.device.storage import UNINSTALL_DRY_RUN_VOLUME_ROOT_PLACEHOLDER, MaStVolume
from timecapsulesmb.services import storage as storage_service
from timecapsulesmb.services.activation import (
    MANUAL_START_AFTER_REBOOT_MESSAGE,
    RESTART_AFTER_REBOOT_MESSAGES,
    RUNTIME_RESTART_FAILURE_MESSAGE,
    start_netbsd4_runtime_after_reboot,
)
from timecapsulesmb.services.callbacks import OperationCallbacks
from timecapsulesmb.services.reboot import RebootFlowError, reboot_device
from timecapsulesmb.transport.errors import TransportError
from timecapsulesmb.transport.ssh import SshConnection, run_ssh


FSCK_REMOTE_COMMAND_TIMEOUT_SECONDS = 3 * 60 * 60
UNINSTALL_REBOOT_NO_DOWN_MESSAGE = (
    "Reboot was requested but the device did not restart.\n"
    "The uninstall removed managed TimeCapsuleSMB files before reboot; power-cycle or rerun uninstall."
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
    "File sharing stays off until the device restarts; restart it, then retry fsck."
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
    # Never repair a volume the manager could remount or smbd could write:
    # abort unless every managed process has stopped. AFP could write it too,
    # so afpserver stops even without a reboot; file sharing then stays off
    # until the next reboot either way.
    lines = [
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
        f"echo '--- fsck_hfs {device} ---'",
        f"/sbin/fsck_hfs -fy {shlex.quote(device)} 2>&1",
        "fsck_status=$?",
        f'echo "{FSCK_STATUS_PREFIX}$fsck_status"',
    ]
    # The reboot that restarts file sharing is requested from the host once
    # this script has reported, through the same ACP request as every flow.
    lines.append('exit "$fsck_status"')
    return "\n".join(lines)


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


def fsck_failure_message(status: int | None, output: str = "") -> str | None:
    if status is None:
        if any(line.strip() == FSCK_NOT_UNMOUNTED_LINE for line in output.splitlines()):
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


def run_fsck(
    connection: SshConnection,
    target: FsckTarget,
    *,
    reboot: bool,
    wait: bool,
    callbacks: OperationCallbacks,
    netbsd4_autostart: bool | None = None,
) -> FsckOutcome:
    """Run the remote fsck script, log its output, then reboot if asked.

    `netbsd4_autostart` is services.activation.installed_netbsd4_autostart,
    read before the repair: None when nothing needs starting after the reboot.
    Otherwise file sharing is started after a waited reboot as deploy starts
    it, so the repair leaves sharing on as Apple's own reboot does. Raises
    RebootFlowError when the reboot request fails or the device does not
    restart or come back.
    """
    callbacks.stage("run_fsck")
    script = build_remote_fsck_script(target.device, target.mountpoint)
    proc = run_ssh(connection, f"/bin/sh -c {shlex.quote(script)}", check=False, timeout=FSCK_REMOTE_COMMAND_TIMEOUT_SECONDS)
    output = proc.stdout or ""
    for line in output.splitlines():
        callbacks.message(line)
    status = fsck_exit_status(output)
    callbacks.update(returncode=status if status is not None else proc.returncode)
    # Without a status line fsck never ran (or its result was lost), so the
    # volume may still be mounted: do not reboot. A failed repair still
    # reboots: that is what restarts file sharing. If the session dropped
    # after fsck finished, sharing stays off until the user restarts, as with
    # --no-reboot.
    rebooting = reboot and status is not None
    failure = fsck_failure_message(status, output)
    try:
        if rebooting:
            if not wait and netbsd4_autostart is False:
                # Without the wait nothing starts file sharing after the reboot.
                callbacks.message(MANUAL_START_AFTER_REBOOT_MESSAGE)
            reboot_device(
                connection.host,
                connection.password,
                wait=wait,
                callbacks=callbacks,
                start_timeout_seconds=120,
                up_timeout_seconds=420,
            )
    except RebootFlowError as exc:
        if failure is None:
            raise
        # A failed reboot must not hide a failed repair. The reboot error
        # stays first so its known message prefixes still match.
        raise RebootFlowError(f"{exc}\n{failure}", exc.code) from exc
    runtime_restart_error = None
    if rebooting and wait and netbsd4_autostart is not None:
        # A failed repair still starts file sharing: the reboot does on every
        # device that starts it by itself.
        try:
            start_netbsd4_runtime_after_reboot(
                connection,
                build_runtime_start_actions(),
                rc_local_autostart=netbsd4_autostart,
                callbacks=callbacks,
                messages=RESTART_AFTER_REBOOT_MESSAGES,
            )
        except DeviceError as exc:
            runtime_restart_error = str(exc)
        except TransportError as exc:
            runtime_restart_error = f"{RUNTIME_RESTART_FAILURE_MESSAGE} {exc}"
        else:
            callbacks.message("File sharing is running again after the reboot.")
        callbacks.update(runtime_restarted=runtime_restart_error is None)
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
    reboot_device(
        connection.host,
        connection.password,
        wait=plan.wait_after_reboot,
        callbacks=callbacks,
        no_down_message=UNINSTALL_REBOOT_NO_DOWN_MESSAGE,
    )
    if not plan.wait_after_reboot:
        return False
    callbacks.stage("verify_post_uninstall")
    verification = verify_post_uninstall(connection, plan)
    for line in render_post_uninstall_verification(verification):
        callbacks.message(line)
    if not verification:
        raise DeviceError(UNINSTALL_FILES_REMAIN_MESSAGE)
    return True
