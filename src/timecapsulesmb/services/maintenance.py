from __future__ import annotations

from dataclasses import dataclass
import shlex
from typing import Callable

from timecapsulesmb.device.processes import render_direct_pkill9_by_ucomm, render_direct_pkill9_watchdog
from timecapsulesmb.device.storage import MaStVolume


FSCK_REMOTE_COMMAND_TIMEOUT_SECONDS = 3 * 60 * 60
UNINSTALL_REBOOT_NO_DOWN_MESSAGE = (
    "Reboot was requested but the device did not go down.\n"
    "The uninstall removed managed TimeCapsuleSMB files before reboot; power-cycle or rerun uninstall."
)
FSCK_REBOOT_NO_DOWN_MESSAGE = "fsck requested reboot from the device, but SSH did not go down."

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


def select_fsck_target(targets: tuple[FsckTarget, ...], selector: str | None, *, prompt: bool = True) -> FsckTarget:
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
    if not prompt:
        raise RuntimeError(MULTIPLE_MOUNTED_HFS_VOLUMES_MESSAGE)

    print(format_fsck_targets(targets))
    while True:
        answer = input("Select a volume to fsck by number: ").strip()
        if answer.isdigit():
            index = int(answer)
            if 1 <= index <= len(targets):
                return targets[index - 1]
        print("Please enter a valid volume number.")


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
        lines.extend([
            "echo '--- reboot ---'",
            "/sbin/reboot >/dev/null 2>&1 || true",
        ])
    return "\n".join(lines)


class RepairExecutionContext:
    def __init__(self, stage_callback: Callable[[str], None]) -> None:
        self._stage_callback = stage_callback
        self.result = "failure"
        self.error: str | None = None

    def set_stage(self, stage: str) -> None:
        self._stage_callback(stage)

    def update_fields(self, **_fields: object) -> None:
        pass

    def succeed(self) -> None:
        self.result = "success"

    def fail_with_error(self, message: str) -> None:
        self.result = "failure"
        self.error = message


class LineLogCapture:
    def __init__(self, emit_line: Callable[[str], None]) -> None:
        self._emit_line = emit_line
        self._buffer = ""

    def write(self, text: str) -> int:
        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            self._emit(line)
        return len(text)

    def flush(self) -> None:
        if self._buffer:
            self._emit(self._buffer)
            self._buffer = ""

    def _emit(self, line: str) -> None:
        message = line.rstrip("\r")
        if message:
            self._emit_line(message)
