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
from dataclasses import dataclass
from typing import Literal

from timecapsulesmb.core.error_codes import ErrorCode
from timecapsulesmb.device.errors import DeviceError, DiskPayloadMissingError
from timecapsulesmb.device.probe import (
    DISK_EVIDENCE_COMMANDS,
    DISK_REPLACE,
    DISK_UNREADABLE_PROBLEMS,
    DiskPayloadProblem,
    manager_payload_problems,
    read_remote_command_conn,
)
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
DISK_FAILING_WAY_OUT = f"{DISK_REPLACE[:1].upper()}{DISK_REPLACE[1:]}."


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
    "wipe": "Wiping it does not fix that, and the new volume may fail too.",
}


@dataclass(frozen=True)
class DiskFailingEvidence:
    """What says the disk is probably failing, for a preflight's warning.
    cause names its confirmation (confirm.<operation>.<cause> in the app),
    one for each cause: this boot's kernel disk errors (disk_failing, as
    their error code), or the manager's title saying the disk could not read
    its files (disk_unreadable). text is how the warning words it."""

    cause: Literal["disk_failing", "disk_unreadable"]
    text: str


def kernel_evidence(errors: tuple[str, ...]) -> DiskFailingEvidence:
    """This boot's disk errors as evidence, naming the last."""
    return DiskFailingEvidence("disk_failing", f"reported errors it could not recover from ({errors[-1]})")


# The manager's title saying it could not read its files from the disk
# (probe.DISK_UNREADABLE_PROBLEMS), where the kernel kept no line.
UNREADABLE_EVIDENCE = DiskFailingEvidence(
    "disk_unreadable", "could not read the TimeCapsuleSMB files, or only too slowly",
)


def internal_disk_errors(errors: tuple[str, ...], device: str) -> tuple[str, ...]:
    """The lines about the internal disk `device`: its own `wd`/`sd` lines,
    and the Marvell driver's, which only the internal disk prints."""
    own = re.compile(rf"^{re.escape(device)}[a-p]?: ")
    return tuple(line for line in errors if own.match(line) or _MARVELL_DEVICE_ERROR.match(line))


def read_disk_failing_evidence_conn(
    connection: SshConnection,
    *,
    internal_device: str | None = None,
    holds_payload: bool = True,
) -> DiskFailingEvidence | None:
    """Why the disk is probably failing: this boot's kernel disk errors, else
    the manager's title saying the disk could not read its files (NetBSD 4's
    kernel log keeps too little to rely on); None for neither, or when the
    device could not be asked.

    With `internal_device`, only about the internal disk (Wipe Internal
    Disk's): its own kernel lines (internal_disk_errors), and the manager's
    title only when the disk holds, or may hold, the files (`holds_payload`)."""
    errors = read_disk_errors_conn(connection)
    if errors and internal_device is not None:
        errors = internal_disk_errors(errors, internal_device)
    if errors:
        return kernel_evidence(errors)
    if not holds_payload:
        return None
    try:
        problems = manager_payload_problems(connection)
    except TransportError:
        return None
    return UNREADABLE_EVIDENCE if any(problem in DISK_UNREADABLE_PROBLEMS for problem in problems or ()) else None


def disk_failing_warning(evidence: DiskFailingEvidence, operation: str) -> str:
    """A preflight's warning before an operation that cannot fix the disk
    ("install", "uninstall", "repair" or "wipe"), from read_disk_failing_evidence_conn.
    A wipe's evidence is about the disk it wipes, so it says "This disk"."""
    subject = "This disk" if operation == "wipe" else "The device's disk"
    return (
        f"{subject} {evidence.text}, so it is probably failing. "
        f"{_PREFLIGHT_EFFECTS[operation]} Copy off the files you need first."
    )


class DiskFailingError(DeviceError):
    """Samba is off, and this boot's disk errors say why."""

    code = ErrorCode.DISK_FAILING
    samba_off = True

    def __init__(self, errors: tuple[str, ...]) -> None:
        super().__init__(DISK_FAILING_MESSAGE, detail=errors[-1])
        self.errors = errors
