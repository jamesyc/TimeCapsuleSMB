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
from typing import TYPE_CHECKING, Literal

from timecapsulesmb.core.error_codes import ErrorCode
from timecapsulesmb.device.errors import DeviceError, DiskPayloadMissingError
from timecapsulesmb.device.processes import Process, StuckProcess, is_disk_check
from timecapsulesmb.device.probe import (
    DISK_EVIDENCE_COMMANDS,
    DISK_REPLACE,
    DISK_STALL_REPLACE,
    DISK_UNREADABLE_PROBLEMS,
    DiskPayloadProblem,
    manager_payload_problems,
    read_remote_command_conn,
)
from timecapsulesmb.transport.errors import TransportError
from timecapsulesmb.transport.ssh import SshConnection

if TYPE_CHECKING:
    from timecapsulesmb.device.storage import MaStDiskSnapshot


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


# A process stuck on the disk for minutes (processes.disk_io_stalls): the disk
# has stopped answering. Apple's ACPd waits on diskd, which waits on the disk,
# so ACPd stops answering MaSt and the network as well. A restart clears a
# stall a passing fault caused; one that comes back may be a failing disk, but
# a wait like this is not proof of one (a diskd and ACPd race, F19, looked
# the same). The stuck processes do not say which disk, internal or USB, so
# the words fit either.
DISK_NOT_RESPONDING_MESSAGE = (
    "A disk on the device has stopped responding: a disk operation has waited for minutes without finishing. "
    "Apple's AirPort service waits for the disk too, so the device's disks cannot be read, and it cannot be "
    "updated or restarted over the network."
)
# Checkup's result says the way out; an error's next steps come from its code.
DISK_NOT_RESPONDING_WAY_OUT = (
    "Unplug the device, wait 10 seconds, plug it back in and wait 2 minutes, then check again. If the disk "
    f"stops responding again, it may be failing: {DISK_STALL_REPLACE}."
)


# A stuck disk checker's first step, before the unplug: a repair on a failing
# disk can wait minutes on one read and still finish.
DISK_CHECK_STALL_WAIT = "If a disk repair is running, give it a few more minutes first: unplugging the device stops it."


def disk_stall_facts(stalls: Iterable[StuckProcess]) -> str:
    """The stuck disk processes, as an error's detail."""
    return f"Waiting for the disk: {'; '.join(stall.describe() for stall in stalls)}."


def disk_stall_code(stalls: Iterable[StuckProcess]) -> ErrorCode:
    """The code for stuck disk processes: disk_check_not_responding when a
    disk checker (processes.is_disk_check) is among them, as a repair on a
    failing disk can wait that long on one read and still finish, and its way
    out gives it time before the unplug; disk_not_responding otherwise."""
    if any(is_disk_check(stall.name) for stall in stalls):
        return ErrorCode.DISK_CHECK_NOT_RESPONDING
    return ErrorCode.DISK_NOT_RESPONDING


# A strict check (services.runtime.require_no_disk_check) refuses a process
# list that did not finish: nothing in it shows a disk job, nor rules one out.
PROCESS_LIST_UNREADABLE_MESSAGE = (
    "The device did not list its processes, so TimeCapsuleSMB cannot tell whether a disk job is running."
)


def disk_check_running_message(
    checks: Iterable[Process], *, unanswered: bool = False, interrupted_by: str = "Restarting or unplugging the device",
) -> str:
    """The error for a disk checker still running (processes.disk_jobs_running).
    A Disk Repair's fsck_hfs goes on after the connection that started it ends,
    and its volume stays unmounted until it finishes. `unanswered`: the device
    stopped answering after the checker was seen, so it was running then and
    may be still. `interrupted_by`: what the refused work would do to it."""
    programs = ", ".join(sorted({check.ucomm for check in checks}))
    if unanswered:
        return (
            f"A disk check ({programs}) was running on the device, and the device stopped answering while "
            f"TimeCapsuleSMB waited for it. {interrupted_by} now could interrupt it."
        )
    return f"A disk check ({programs}) is still running on the device. {interrupted_by} now would interrupt it."


def disk_check_facts(checks: Iterable[Process]) -> str:
    """The running disk checkers, as an error's detail."""
    return f"Running: {'; '.join(check.describe() for check in checks)}."


# The failing-disk preflight's last sentence, for each operation that cannot
# fix the disk. A wipe may get a disk with a bad sector working again, so it
# has none: its bad-sector sentence (_WIPE_BAD_SECTOR) ends its warning.
_PREFLIGHT_EFFECTS = {
    "install": "Installing will not fix this.",
    "uninstall": "Uninstalling will not fix this.",
    "repair": "A disk repair may not fix this, and works the disk hard.",
}
# The wipe zeroes only the disk's ends, then Apple's quick erase writes the new
# volume's metadata, so it does not rewrite a bad sector in the middle. What can
# help: the new volume no longer needs the data that cannot be read, and the
# drive swaps in a spare sector the next time one is written there. Hence only
# "sometimes".
_WIPE_BAD_SECTOR = "If a bad sector is the cause, wiping sometimes gets the disk working again, but the new volume may fail too."
_OTHER_BAD_SECTOR = "If a bad sector is the cause, {wipe} sometimes gets the disk working again, but it deletes everything on it."
# The app's names for the steps the warning gives, and the CLI's.
_PREFLIGHT_WORDS = {
    "app": {"wipe": "“Wipe Internal Disk”", "checkup": "“Checkup”"},
    "cli": {"wipe": "`tcapsule wipe-internal-disk`", "checkup": "`tcapsule doctor`"},
}


DiskFailingDisk = Literal["internal", "usb", "unknown"]


@dataclass(frozen=True)
class DiskFailingEvidence:
    """What says the disk may be failing, for a preflight's warning.
    cause names its confirmation (confirm.<operation>.<cause> in the app),
    one for each cause: this boot's kernel disk errors (disk_failing, as
    their error code), or the manager's title saying the disk could not read
    its files (disk_unreadable). text is how the warning words it.

    disk: whose kernel lines they are, from MaSt's built-in disk: the
    internal disk's (any line of it counts), only other disks' ("usb": a USB
    disk, or an AirPort Extreme's only disk), or "unknown" (MaSt unread, or
    the manager's verdict, which names no disk). wipeable: the internal disk
    is one Wipe Internal Disk accepts (storage.wipe_refusal), so the warning
    may suggest it."""

    cause: Literal["disk_failing", "disk_unreadable"]
    text: str
    disk: DiskFailingDisk = "unknown"
    wipeable: bool = False


def _kernel_text(line: str) -> str:
    return f"reported errors it could not recover from ({line})"


def kernel_evidence(errors: tuple[str, ...], *, disks: tuple[MaStDiskSnapshot, ...] | None = None) -> DiskFailingEvidence:
    """This boot's disk errors as evidence. `disks`: MaSt's inventory, None
    when it was not read. The internal disk's last line is the one quoted
    when there is one, as the warning's wipe sentence is about it; else the
    last line."""
    # storage imports this module.
    from timecapsulesmb.device.storage import wipe_refusal
    if disks is None:
        return DiskFailingEvidence("disk_failing", _kernel_text(errors[-1]))
    builtin = next((disk for disk in disks if disk.builtin), None)
    if builtin is None:
        # The Marvell controller is the internal disk's: a device that lists
        # none has lost it, which this cannot tell from a USB disk's errors.
        if any(_MARVELL_DEVICE_ERROR.match(line) for line in errors):
            return DiskFailingEvidence("disk_failing", _kernel_text(errors[-1]))
        return DiskFailingEvidence("disk_failing", _kernel_text(errors[-1]), disk="usb")
    own = internal_disk_errors(errors, builtin.device)
    if not own:
        return DiskFailingEvidence("disk_failing", _kernel_text(errors[-1]), disk="usb")
    return DiskFailingEvidence(
        "disk_failing", _kernel_text(own[-1]), disk="internal", wipeable=wipe_refusal(builtin) is None,
    )


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


# MaSt for the warning's disk wording only: a normal read takes well under a
# second (the deploy's read_mast stage: 0.07 s), so this is generous, and still
# a quarter of the 60 s a read that matters waits. Kernel disk errors are when
# MaSt is likeliest to hang, and the warning should not wait a minute for it.
DISK_INVENTORY_TIMEOUT_SECONDS = 15


def read_disk_inventory_conn(connection: SshConnection) -> tuple[MaStDiskSnapshot, ...] | None:
    """MaSt's disks, for which disk the warning names; None when MaSt did not
    answer (timed out, a failed get, the connection), which leaves the disk
    unnamed. Nothing after a failure is read: no ACPd or process checks, as
    the warning only words what was seen and the operation reads MaSt again."""
    # storage imports this module.
    from timecapsulesmb.device.storage import parse_mast_inventory, read_mast_volumes_with_output_conn
    try:
        read = read_mast_volumes_with_output_conn(
            connection, check_acpd=False, timeout=DISK_INVENTORY_TIMEOUT_SECONDS, explain_failure=False,
        )
    except TransportError:
        return None
    if read.get_failed:
        return None
    return parse_mast_inventory(read.raw_output)


def read_disk_failing_evidence_conn(
    connection: SshConnection,
    *,
    internal_device: str | None = None,
    holds_payload: bool = True,
) -> DiskFailingEvidence | None:
    """Why the disk may be failing: this boot's kernel disk errors, else
    the manager's title saying the disk could not read its files (NetBSD 4's
    kernel log keeps too little to rely on); None for neither, or when the
    device could not be asked.

    With `internal_device`, only about the internal disk (Wipe Internal
    Disk's, which it has already checked it accepts): its own kernel lines
    (internal_disk_errors), and the manager's title only when the disk
    holds, or may hold, the files (`holds_payload`). Without it, any disk's
    lines count, and MaSt says whose they are (read only when there are
    lines)."""
    errors = read_disk_errors_conn(connection)
    if errors and internal_device is not None:
        errors = internal_disk_errors(errors, internal_device)
        if errors:
            return DiskFailingEvidence("disk_failing", _kernel_text(errors[-1]), disk="internal", wipeable=True)
    if errors:
        return kernel_evidence(errors, disks=read_disk_inventory_conn(connection))
    if not holds_payload:
        return None
    try:
        problems = manager_payload_problems(connection)
    except TransportError:
        return None
    return UNREADABLE_EVIDENCE if any(problem in DISK_UNREADABLE_PROBLEMS for problem in problems or ()) else None


DiskFailingVariant = Literal["wipe", "usb", "plain"]


def disk_failing_variant(evidence: DiskFailingEvidence, operation: str) -> DiskFailingVariant:
    """Which wording the warning before `operation` takes, one app
    confirmation each (`<operation>.<cause>` plus `_wipe` or `_usb`):
    "wipe" names the internal disk and suggests Wipe Internal Disk (kernel
    errors on an internal disk it accepts); "usb" names a USB disk; "plain"
    says "the device's disk": the disk is unknown, an internal one Wipe
    refuses, or the manager's verdict. A wipe's own warning is always plain
    ("This disk") and has its bad-sector sentence anyway. Unreadable files
    (slow reads included) say too little about a bad sector to suggest
    deleting everything."""
    if operation == "wipe" or evidence.cause != "disk_failing":
        return "plain"
    if evidence.disk == "internal" and evidence.wipeable:
        return "wipe"
    if evidence.disk == "usb":
        return "usb"
    return "plain"


_SUBJECTS = {"wipe": "The internal disk", "usb": "A USB disk on the device", "plain": "The device's disk"}


def disk_failing_warning(evidence: DiskFailingEvidence, operation: str, *, client: Literal["app", "cli"] = "app") -> str:
    """A preflight's warning before an operation that may not fix the disk
    ("install", "uninstall", "repair" or "wipe"), from read_disk_failing_evidence_conn.
    A wipe's evidence is about the disk it wipes, so it says "This disk".

    In this order: which disk and what was seen, and that it may be failing;
    save the files first, while they can still be read; a restart, then a
    Checkup to see whether the errors come back (a restart clears this boot's
    kernel lines, and only a read of the bad sector brings them back, so a
    quiet Checkup after one proves little); AirPort Utility's own check
    missing these errors; the wipe that sometimes gets past a bad sector;
    and what the operation will not do. `client` names the steps the app's
    way or the CLI's (`tcapsule ...`). The app shows its own translation of
    each confirmation, not this text."""
    words = _PREFLIGHT_WORDS[client]
    variant = disk_failing_variant(evidence, operation)
    subject = "This disk" if operation == "wipe" else _SUBJECTS[variant]
    sentences = [
        f"{subject} {evidence.text}, so it may be failing.",
        "Copy off the files you need first.",
        "Then try restarting the device in AirPort Utility, or unplug it for 10 seconds and plug it back in.",
        f"Run {words['checkup']} afterwards to see whether the errors come back.",
        "AirPort Utility may still show the disk as OK.",
    ]
    if operation == "wipe":
        sentences.append(_WIPE_BAD_SECTOR)
    else:
        if variant == "wipe":
            sentences.append(_OTHER_BAD_SECTOR.format(wipe=words["wipe"]))
        sentences.append(_PREFLIGHT_EFFECTS[operation])
    return " ".join(sentences)


def disk_failing_fields(evidence: DiskFailingEvidence, operation: str) -> dict[str, object]:
    """Telemetry for an operation the failing-disk warning applied to: what it
    was based on and whether it suggested a wipe. The confirmation's request
    and the confirmed one both record it; the confirmed one's outcome says
    whether the user went on."""
    return {
        "disk_failing_warned": True,
        "disk_failing_cause": evidence.cause,
        "disk_failing_disk": evidence.disk,
        "disk_failing_wipe_suggested": disk_failing_variant(evidence, operation) == "wipe",
    }


class DiskNotRespondingError(DeviceError):
    """Samba is off, and a process stuck on the disk says why
    (processes.disk_io_stalls). `prefix` and `suffix` frame the message for
    the command that stopped: what failed, and what it left as it was. A
    preflight passes disk_stall_code's `code`, so a stuck disk checker gets
    its own way out."""

    code = ErrorCode.DISK_NOT_RESPONDING
    samba_off = True

    def __init__(
        self, stalls: Iterable[StuckProcess], *, prefix: str = "", suffix: str = "", code: ErrorCode | None = None,
    ) -> None:
        self.stalls = tuple(stalls)
        message = " ".join(part for part in (prefix, DISK_NOT_RESPONDING_MESSAGE, suffix) if part)
        super().__init__(message, code=code, detail=disk_stall_facts(self.stalls))


class DiskFailingError(DeviceError):
    """Samba is off, and this boot's disk errors say why."""

    code = ErrorCode.DISK_FAILING
    samba_off = True

    def __init__(self, errors: tuple[str, ...]) -> None:
        super().__init__(DISK_FAILING_MESSAGE, detail=errors[-1])
        self.errors = errors
