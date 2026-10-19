"""Disk errors the kernel logged in this boot that the disk did not recover from.

NetBSD's `wd`/`sd` drivers print `wd0c: error reading fsbn ...` once per
attempt at a transfer, with `, retrying` on all but the last, so only a line
without it is a transfer that failed for good. NetBSD 4's Marvell SATA driver
(the internal disk) prints `NetBSD IAL (ERROR) [0 0] device error occured` for
a command the drive failed. Recovered errors (`soft error (corrected)`, a
retried timeout) and the rest of the disk lines do not count.

NetBSD 6 keeps the message buffer across a soft restart, so only lines after
the last kernel banner (`NetBSD 6.0 (...)`) count. A buffer without a banner
has wrapped since this boot's, so every line in it is this boot's; NetBSD 4's
4 KB buffer wraps during every boot.

The lines come from doctor's kernel disk evidence
(probe.DISK_EVIDENCE_COMMANDS), which never reads a data disk.
"""
from __future__ import annotations

import re
from collections.abc import Iterable

from timecapsulesmb.core.error_codes import ErrorCode
from timecapsulesmb.device.errors import DeviceError, DiskPayloadMissingError
from timecapsulesmb.device.probe import DISK_EVIDENCE_COMMANDS, DiskPayloadProblem, read_remote_command_conn
from timecapsulesmb.transport.errors import TransportError
from timecapsulesmb.transport.ssh import SshConnection


KERNEL_DISK_MESSAGES_COMMAND = DISK_EVIDENCE_COMMANDS["remote_kernel_disk_messages"]

_KERNEL_BANNER = re.compile(r"^NetBSD \d")
_DISK_TRANSFER_ERROR = re.compile(r"^(?:wd|sd)\d+[a-p]?: (?:error|device timeout) (?:reading|writing) fsbn ")
_MARVELL_DEVICE_ERROR = re.compile(r"^NetBSD IAL \(ERROR\).* device error")


def disk_errors_in(kernel_messages: str) -> tuple[str, ...]:
    """This boot's lines for transfers a disk did not recover from."""
    lines = [line.strip() for line in kernel_messages.splitlines()]
    banners = [i for i, line in enumerate(lines) if _KERNEL_BANNER.match(line)]
    if banners:
        lines = lines[banners[-1] + 1:]
    return tuple(
        line for line in lines
        if (_DISK_TRANSFER_ERROR.match(line) and not line.endswith(", retrying")) or _MARVELL_DEVICE_ERROR.match(line)
    )


def read_disk_errors_conn(connection: SshConnection) -> tuple[str, ...] | None:
    """This boot's disk errors; None when the device could not be asked (it
    may have just dropped off the network), which says nothing either way."""
    try:
        return disk_errors_in(read_remote_command_conn(connection, KERNEL_DISK_MESSAGES_COMMAND))
    except TransportError:
        return None


def disk_errors_explain(
    errors: tuple[str, ...] | None, problems: Iterable[DiskPayloadProblem] = (), *, samba_ready: bool = False,
) -> bool:
    """Whether this boot's disk errors are why Samba is off. They outrank the
    manager's verdicts, except files missing from every disk (an Erase, which
    errors from before it do not explain), and say nothing while Samba serves."""
    if not errors or samba_ready:
        return False
    return not any(problem.error_type is DiskPayloadMissingError for problem in problems)


# Checkup's result says the way out; an error's next steps come from its code.
DISK_FAILING_WAY_OUT = (
    "Copy off the files you need (if file sharing is off, take the disk out and connect it to a Mac with a "
    "SATA-to-USB adapter), replace the disk, then erase the new disk in AirPort Utility and run "
    '"Install / Update Samba" in the macOS app, or tcapsule deploy from the command line.'
)


DISK_FAILING_MESSAGE = (
    "The device's disk reported errors it could not recover from, so it is probably failing. "
    "File sharing and Time Machine will not work reliably on it, and reinstalling will not fix the disk. "
    "AirPort Utility may still show the disk as OK: its own check does not catch these errors."
)


def disk_failing_message(errors: tuple[str, ...]) -> str:
    """Checkup's line for why Samba is off, naming the kernel's last disk error."""
    return DISK_FAILING_MESSAGE.replace("from,", f"from ({errors[-1]}),", 1)


_PREFLIGHT_EFFECTS = {
    "install": "Installing will not fix that.",
    "uninstall": "Uninstalling will not fix that.",
    "repair": "A disk repair cannot fix a failing disk, and works it hard.",
}


def disk_failing_warning(errors: tuple[str, ...], operation: str) -> str:
    """A preflight's warning before an operation that cannot fix the disk:
    "install", "uninstall" or "repair"."""
    return (
        f"The device's disk reported errors it could not recover from ({errors[-1]}), so it is probably failing. "
        f"{_PREFLIGHT_EFFECTS[operation]} Copy off the files you need first."
    )


class DiskFailingError(DeviceError):
    """Samba is off, and this boot's disk errors say why."""

    code = ErrorCode.DISK_FAILING
    samba_off = True

    def __init__(self, errors: tuple[str, ...]) -> None:
        super().__init__(DISK_FAILING_MESSAGE, detail=errors[-1])
        self.errors = errors
