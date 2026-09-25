from __future__ import annotations

from dataclasses import dataclass
import shlex

from timecapsulesmb.deploy.commands import managed_stop_actions, render_remote_actions
from timecapsulesmb.deploy.executor import DETACHED_SHUTDOWN_REBOOT_COMMAND
from timecapsulesmb.device.storage import MaStVolume


FSCK_REMOTE_COMMAND_TIMEOUT_SECONDS = 3 * 60 * 60
UNINSTALL_REBOOT_NO_DOWN_MESSAGE = (
    "Reboot was requested but the device did not go down.\n"
    "The uninstall removed managed TimeCapsuleSMB files before reboot; power-cycle or rerun uninstall."
)
FSCK_REBOOT_NO_DOWN_MESSAGE = "fsck requested reboot from the device, but SSH did not go down."
FSCK_STATUS_PREFIX = "tcapsule-fsck: fsck_hfs exit status "
FSCK_DID_NOT_RUN_MESSAGE = (
    "fsck did not run: file sharing could not be stopped, or the connection "
    "ended before fsck_hfs finished."
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
        lines.append(f"  follow-up: {'wait for SSH down, then SSH up' if wait else 'do not wait'}")
    return "\n".join(lines)


def build_remote_fsck_script(device: str, mountpoint: str, *, reboot: bool) -> str:
    # Never repair a volume the manager could remount or smbd could write:
    # abort unless every managed process has stopped. AFP could write it too,
    # so afpserver stops even without a reboot; file sharing then stays off
    # until the next reboot either way.
    lines = [
        f"( {command} ) || exit 1"
        for command in render_remote_actions(managed_stop_actions(stop_afpserver=True))
    ]
    # The reboot drops SSH, so the session's exit status is unreliable on that
    # path; the status line in the output is what reports fsck's result.
    lines += [
        f"/sbin/umount -f {shlex.quote(mountpoint)} >/dev/null 2>&1 || true",
        f"echo '--- fsck_hfs {device} ---'",
        f"/sbin/fsck_hfs -fy {shlex.quote(device)} 2>&1",
        "fsck_status=$?",
        f'echo "{FSCK_STATUS_PREFIX}$fsck_status"',
    ]
    # A failed repair still reboots: that is what restarts file sharing.
    if reboot:
        lines.extend([
            "echo '--- reboot ---'",
            DETACHED_SHUTDOWN_REBOOT_COMMAND,
        ])
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


def fsck_failure_message(status: int | None) -> str | None:
    if status is None:
        return FSCK_DID_NOT_RUN_MESSAGE
    if status != 0:
        return f"fsck_hfs exited with status {status}; the disk may still need repair."
    return None
