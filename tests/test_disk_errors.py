"""This boot's disk errors (device.disk_errors): what counts, what they
outrank, and how the runtime check, Activate and the command-line preflight
use them."""
from __future__ import annotations

import io
import re
import unittest
from contextlib import redirect_stderr
from unittest import mock

from timecapsulesmb.cli.runtime import confirm_despite_failing_disk
from timecapsulesmb.core.error_codes import ErrorCode
from timecapsulesmb.device.disk_errors import (
    DISK_NOT_RESPONDING_MESSAGE,
    DiskNotRespondingError,
    DISK_FAILING_MESSAGE,
    KERNEL_DISK_MESSAGES_COMMAND,
    UNREADABLE_EVIDENCE,
    DiskFailingError,
    disk_errors_explain,
    disk_errors_in,
    disk_failing_fields,
    disk_failing_message,
    disk_failing_warning,
    kernel_evidence,
    read_disk_errors_conn,
    read_disk_failing_evidence_conn,
    read_disk_inventory_conn,
)
from timecapsulesmb.device.errors import (
    AcpdQuitError,
    DeviceError,
    DiskPayloadMissingError,
    DiskPayloadUnreadableError,
    RsyncNotRunningError,
    RsyncUnreadableError,
)
from timecapsulesmb.device import probe
from timecapsulesmb.device.processes import disk_io_stalls
from timecapsulesmb.device.storage import MaStDiskSnapshot, MaStPartitionSnapshot
from timecapsulesmb.device.probe import (
    DISK_PAYLOAD_PROBLEMS,
    ManagedRuntimeProbeResult,
    ProbeStepResult,
    ReadinessProbeResult,
)
from timecapsulesmb.services.activation import activate_runtime
from timecapsulesmb.services.callbacks import OperationCallbacks
from timecapsulesmb.services.runtime import DiskCheckRunningError
from timecapsulesmb.services.runtime_verification import runtime_failure_error, verify_managed_runtime_ready
from timecapsulesmb.transport.errors import SshCommandTimeout, SshNetworkError
from timecapsulesmb.transport.ssh import SshConnection
from timecapsulesmb.device.acpd import RestartVerdict


NETBSD6_BANNER = "NetBSD 6.0 (build.kernel-target.conf) #0: Mon Apr 29 18:35:13 PDT 2019"
# A NetBSD 6 disk that never read one sector: each attempt but the last ends
# in ", retrying".
NETBSD6_UNRECOVERED = (
    "wd0c: error reading fsbn 452695984 of 452695984-452695999 (wd0 bn 452695984; cn 449103 tn 2 sn 34), retrying",
    "wd0: (uncorrectable data error)",
    "wd0c: error reading fsbn 452695984 of 452695984-452695999 (wd0 bn 452695984; cn 449103 tn 2 sn 34)",
    "wd0: (uncorrectable data error)",
)
FINAL = NETBSD6_UNRECOVERED[2]
# Errors a disk recovered from: a retried timeout, a corrected read.
NETBSD6_RECOVERED = (
    "wd0c: device timeout writing fsbn 604983808 of 604983808-604983815 (wd0 bn 604983808; cn 600182 tn 5 sn 37), retrying",
    "wd0: soft error (corrected)",
)
# A NetBSD 4 internal disk failing a command (Marvell SATA driver).
NETBSD4_DEVICE_ERROR = (
    "Core Driver (ERROR) 0 0: EDMA device error in NCQ mode",
    "NetBSD IAL (ERROR)  [0 0] device error occured",
    "sd0(mv_sata_integ0:0:0:0): Sense Error Code 0x72",
)
# Healthy units' disk lines: an empty second port, a USB disk spinning up,
# HFS catalog lookups, journal replays.
HEALTHY = (
    "ahcisata0: port 1 reset failed",
    "sd0(umass0:0:0:0): not ready, data = 00 00 00 00 04 01 00 00 00 00",
    "sd0: drive offline",
    "GetNode: getNodeProc returned error: 12",
    "jnl: journal start/end pointers reset! (jnl 0x8eeeba08; s 0x200 e 0x200)",
    "HFS: Removed 2650 orphaned unlinked files",
)
DAMAGED, MISSING = (next(p for p in DISK_PAYLOAD_PROBLEMS if p.token == token)
                    for token in ("payload=damaged", "payload=missing"))


def lines(*groups: tuple[str, ...] | str) -> str:
    return "\n".join(line for group in groups for line in ((group,) if isinstance(group, str) else group)) + "\n"


class DiskErrorsInTests(unittest.TestCase):
    def test_a_transfer_the_disk_never_completed_counts_and_its_retries_do_not(self) -> None:
        self.assertEqual(disk_errors_in(lines(NETBSD6_BANNER, NETBSD6_UNRECOVERED)), (FINAL,))

    def test_errors_the_disk_recovered_from_do_not_count(self) -> None:
        self.assertEqual(disk_errors_in(lines(NETBSD6_BANNER, NETBSD6_RECOVERED)), ())

    def test_the_netbsd4_driver_reporting_a_failed_command_counts(self) -> None:
        self.assertEqual(disk_errors_in(lines(NETBSD4_DEVICE_ERROR)), (NETBSD4_DEVICE_ERROR[1],))

    def test_healthy_units_log_nothing_that_counts(self) -> None:
        self.assertEqual(disk_errors_in(lines(NETBSD6_BANNER, HEALTHY)), ())
        self.assertEqual(disk_errors_in(lines(HEALTHY)), ())

    def test_only_this_boots_lines_count(self) -> None:
        # NetBSD 6 keeps an earlier boot's lines across a soft restart; a
        # buffer that wrapped past this boot's banner holds only this boot's.
        cases = (
            ("an earlier boot's", lines(NETBSD6_UNRECOVERED, NETBSD6_BANNER, HEALTHY), ()),
            ("this boot's", lines(HEALTHY, NETBSD6_BANNER, NETBSD6_UNRECOVERED), (FINAL,)),
            ("two boots ago", lines(NETBSD6_UNRECOVERED, NETBSD6_BANNER, NETBSD6_BANNER, HEALTHY), ()),
            ("a wrapped buffer's", lines(NETBSD6_UNRECOVERED), (FINAL,)),
        )
        for name, text, expected in cases:
            with self.subTest(name):
                self.assertEqual(disk_errors_in(text), expected)

    def test_the_evidence_filter_keeps_every_line_the_rules_read(self) -> None:
        # The lines come through doctor's disk evidence command, whose sed
        # prints a line any of its patterns match (basic regexes, here only
        # ^ and [0-9], which Python reads alike). Filtering a whole log the
        # same way must not change what counts.
        patterns = [re.compile(p) for p in re.findall(r"-e '/(.*?)/p'", KERNEL_DISK_MESSAGES_COMMAND)]
        log = [NETBSD6_BANNER, *HEALTHY, "bwl0: 11g rates: 1Mbps", *NETBSD6_UNRECOVERED, *NETBSD4_DEVICE_ERROR]
        kept = [line for line in log if any(pattern.search(line) for pattern in patterns)]
        self.assertNotIn("bwl0: 11g rates: 1Mbps", kept)
        self.assertEqual(disk_errors_in("\n".join(kept)), disk_errors_in("\n".join(log)))
        self.assertEqual(disk_errors_in("\n".join(kept)), (FINAL, NETBSD4_DEVICE_ERROR[1]))


class ReadDiskErrorsTests(unittest.TestCase):
    CONNECTION = SshConnection("root@10.0.0.2", "pw", "")

    def test_reads_doctors_kernel_disk_evidence(self) -> None:
        read = mock.Mock(return_value=lines(NETBSD6_BANNER, NETBSD6_UNRECOVERED))
        with mock.patch("timecapsulesmb.device.disk_errors.read_remote_command_conn", read):
            self.assertEqual(read_disk_errors_conn(self.CONNECTION), (FINAL,))
        read.assert_called_once_with(self.CONNECTION, KERNEL_DISK_MESSAGES_COMMAND)

    def test_a_device_that_does_not_answer_says_nothing_either_way(self) -> None:
        for failure in (SshNetworkError("Connection reset"), SshCommandTimeout("timed out")):
            with self.subTest(type(failure).__name__):
                with mock.patch("timecapsulesmb.device.disk_errors.read_remote_command_conn", side_effect=failure):
                    self.assertIsNone(read_disk_errors_conn(self.CONNECTION))


class DiskFailingEvidenceTests(unittest.TestCase):
    """What a preflight warns with: this boot's kernel lines, else the
    manager's title saying the disk could not read its files. NetBSD 4's
    kernel log wraps during boot and keeps the lines only now and then; the
    title keeps the verdict while the manager runs."""

    CONNECTION = SshConnection("root@10.0.0.2", "pw", "")

    def evidence(self, errors, problems, *, disks=None):
        title = problems if callable(problems) else mock.Mock(return_value=problems)
        self.mast = mock.Mock(return_value=disks)
        with mock.patch("timecapsulesmb.device.disk_errors.read_disk_errors_conn", return_value=errors):
            with mock.patch("timecapsulesmb.device.disk_errors.manager_payload_problems", title):
                with mock.patch("timecapsulesmb.device.disk_errors.read_disk_inventory_conn", self.mast):
                    return read_disk_failing_evidence_conn(self.CONNECTION), title

    def test_kernel_lines_come_first_and_name_the_last_error(self) -> None:
        found, title = self.evidence((NETBSD4_DEVICE_ERROR[1], FINAL), (probe.DISK_PAYLOAD_UNREADABLE,))
        self.assertEqual(found, kernel_evidence((FINAL,)))
        self.assertEqual(found.cause, "disk_failing")
        self.assertIn(FINAL, found.text)
        title.assert_not_called()

    def test_without_kernel_lines_an_unreadable_verdict_is_the_evidence(self) -> None:
        for errors in ((), None):
            for problems in ((probe.DISK_PAYLOAD_UNREADABLE,), (probe.RSYNC_UNREADABLE,),
                             (probe.DISK_PAYLOAD_DAMAGED, probe.RSYNC_UNREADABLE)):
                with self.subTest(errors=errors, problems=[p.token for p in problems]):
                    self.assertEqual(self.evidence(errors, problems)[0], UNREADABLE_EVIDENCE)
                    # No kernel lines, so no disk to tell apart: MaSt is not read.
                    self.mast.assert_not_called()

    def test_other_verdicts_and_a_quiet_title_say_nothing(self) -> None:
        # Damaged or missing files are fixed by a reinstall: no disk warning.
        for problems in ((), None, (probe.DISK_PAYLOAD_DAMAGED,), (probe.DISK_PAYLOAD_MISSING,),
                         (probe.RSYNC_DAMAGED, probe.RSYNC_MISSING)):
            with self.subTest(problems=problems):
                self.assertIsNone(self.evidence((), problems)[0])

    def test_a_title_that_cannot_be_read_says_nothing(self) -> None:
        def unreadable(_connection):
            raise SshNetworkError("Connection reset")
        self.assertIsNone(self.evidence((), unreadable)[0])

    def test_mast_says_whose_errors_they_are_and_whether_wipe_accepts_the_disk(self) -> None:
        # NetBSD 6's internal disk is wd0 and NetBSD 4's is sd0, where a USB
        # disk is sd1, or sd0 itself on an AirPort Extreme (no built-in disk).
        sd1 = "sd1a: error writing fsbn 8 of 8-15 (sd1 bn 8; cn 0 tn 0 sn 8)"
        sd0 = "sd0c: error reading fsbn 64 of 64-71 (sd0 bn 64; cn 0 tn 1 sn 1)"
        marvell = NETBSD4_DEVICE_ERROR[1]
        usb = mast_disk("sd1", builtin=False)
        cases = (
            ("NetBSD 6 internal disk", (FINAL,), (mast_disk("wd0"),), "internal", True, FINAL),
            ("NetBSD 4 internal disk", (sd0,), (mast_disk("sd0"), usb), "internal", True, sd0),
            ("Marvell controller", (marvell,), (mast_disk("sd0"),), "internal", True, marvell),
            ("USB disk beside the internal one", (sd1,), (mast_disk("sd0"), usb), "usb", False, sd1),
            ("AirPort Extreme's USB sd0", (sd0,), (mast_disk("sd0", builtin=False),), "usb", False, sd0),
            # Both: the warning quotes the internal disk's line, which the
            # wipe sentence is about, though the USB line came last.
            ("both disks", (sd0, sd1), (mast_disk("sd0"), usb), "internal", True, sd0),
            # Wipe Internal Disk refuses these, so the warning must not
            # suggest it: a failed Seagate, a misreported size, a lost disk.
            ("failed Seagate", (FINAL,), (mast_disk("wd0", size=3950, partitions=()),), "internal", False, FINAL),
            ("misreported size", (FINAL,), (mast_disk("wd0", size=4000, partitions=()),), "internal", False, FINAL),
            ("soft-disconnected", (FINAL,), (mast_disk("wd0", soft_disconnected=True),), "internal", False, FINAL),
            # The Marvell lines are the internal disk's, but MaSt lists none:
            # it cannot tell, so it names no disk.
            ("Marvell, no built-in disk", (marvell,), (usb,), "unknown", False, marvell),
            # A USB disk's line after it: the Marvell line is still quoted,
            # as it is what says the internal disk may be gone.
            ("Marvell then USB, no built-in disk", (marvell, sd1), (usb,), "unknown", False, marvell),
            # wd is NetBSD 6's internal SATA disk, never a USB one (those
            # attach as sd): its lines with no built-in disk listed are a lost
            # internal disk's, not a USB disk's.
            ("wd, no built-in disk", (FINAL,), (usb,), "unknown", False, FINAL),
            ("wd then USB, no built-in disk", (FINAL, sd1), (usb,), "unknown", False, FINAL),
            ("wd, no disk at all", (FINAL,), (), "unknown", False, FINAL),
            # MaSt did not answer.
            ("MaSt unread", (FINAL,), None, "unknown", False, FINAL),
        )
        for name, errors, disks, disk, wipeable, quoted in cases:
            with self.subTest(name):
                found, _title = self.evidence(errors, (), disks=disks)
                self.assertEqual((found.disk, found.wipeable), (disk, wipeable))
                self.assertIn(f"({quoted})", found.text)
                self.mast.assert_called_once_with(self.CONNECTION)

    def test_a_disk_named_on_the_call_is_not_looked_up_again(self) -> None:
        # Wipe Internal Disk passes the disk it wipes, which it has already
        # checked it accepts: its lines only, and every one is that disk's.
        self.mast = mock.Mock()
        with mock.patch("timecapsulesmb.device.disk_errors.read_disk_errors_conn", return_value=(FINAL,)):
            with mock.patch("timecapsulesmb.device.disk_errors.read_disk_inventory_conn", self.mast):
                found = read_disk_failing_evidence_conn(self.CONNECTION, internal_device="wd0")
        self.assertEqual((found.disk, found.wipeable), ("internal", True))
        self.mast.assert_not_called()


def mast_disk(device: str, *, builtin: bool = True, size: int = 1907729, partitions=None,
              soft_disconnected: bool = False) -> MaStDiskSnapshot:
    if partitions is None:
        partitions = (MaStPartitionSnapshot("dk2", "Data", "hfs", 1905681, 8977, 1896704),)
    return MaStDiskSnapshot(device, "", size, builtin, partitions, soft_disconnected=soft_disconnected)


class UnreadKernelLogTests(unittest.TestCase):
    """A kernel log that could not be read leaves the warning to the
    manager's verdict, or shows none; telemetry records it, so how often a
    warning goes missing this way can be counted."""

    CONNECTION = SshConnection("root@10.0.0.2", "pw", "")

    def evidence(self, errors, problems=()):
        fields: dict[str, object] = {}
        with mock.patch("timecapsulesmb.device.disk_errors.read_disk_errors_conn", return_value=errors):
            with mock.patch("timecapsulesmb.device.disk_errors.manager_payload_problems", return_value=problems):
                with mock.patch("timecapsulesmb.device.disk_errors.read_disk_inventory_conn", return_value=None):
                    found = read_disk_failing_evidence_conn(self.CONNECTION, update_fields=lambda **f: fields.update(f))
        return found, fields

    def test_an_unread_log_is_recorded_whatever_the_verdict(self) -> None:
        for problems, expected in (((), None), ((probe.DISK_PAYLOAD_UNREADABLE,), UNREADABLE_EVIDENCE)):
            with self.subTest(problems=problems):
                found, fields = self.evidence(None, problems)
                self.assertEqual(found, expected)
                self.assertEqual(fields, {"disk_failing_kernel_log": "unread"})

    def test_a_log_that_was_read_records_nothing(self) -> None:
        # An empty log and one with errors were both read.
        for errors in ((), (FINAL,)):
            with self.subTest(errors=errors):
                _found, fields = self.evidence(errors)
                self.assertEqual(fields, {})


class RepairTargetTests(unittest.TestCase):
    """Disk Repair warns only about the disk it repairs: a failing USB stick
    is no reason to warn before repairing the internal volume, nor the
    reverse."""

    CONNECTION = SshConnection("root@10.0.0.2", "pw", "")
    INTERNAL = "sd0c: error reading fsbn 64 of 64-71 (sd0 bn 64; cn 0 tn 1 sn 1)"
    USB = "sd1a: error writing fsbn 8 of 8-15 (sd1 bn 8; cn 0 tn 0 sn 8)"
    DISKS = (
        mast_disk("sd0", partitions=(MaStPartitionSnapshot("dk2", "Data", "hfs"),)),
        mast_disk("sd1", builtin=False, partitions=(MaStPartitionSnapshot("dk5", "USB", "hfs"),)),
    )

    def evidence(self, errors, volume, *, disks=DISKS, problems=()):
        with mock.patch("timecapsulesmb.device.disk_errors.read_disk_errors_conn", return_value=errors):
            with mock.patch("timecapsulesmb.device.disk_errors.manager_payload_problems", return_value=problems):
                with mock.patch("timecapsulesmb.device.disk_errors.read_disk_inventory_conn", return_value=disks):
                    return read_disk_failing_evidence_conn(self.CONNECTION, volume=volume)

    def test_another_disks_errors_do_not_warn(self) -> None:
        for volume, errors in (("/dev/dk2", (self.USB,)), ("dk5", (self.INTERNAL,)),
                               # The Marvell controller is the internal disk's.
                               ("dk5", (NETBSD4_DEVICE_ERROR[1],))):
            with self.subTest(volume=volume, errors=errors):
                self.assertIsNone(self.evidence(errors, volume))

    def test_the_repaired_disks_own_errors_warn_and_name_it(self) -> None:
        for volume, errors, disk, quoted in (("/dev/dk2", (self.INTERNAL, self.USB), "internal", self.INTERNAL),
                                             ("dk5", (self.USB, self.INTERNAL), "usb", self.USB)):
            with self.subTest(volume=volume):
                found = self.evidence(errors, volume)
                self.assertEqual(found.disk, disk)
                self.assertIn(f"({quoted})", found.text)

    def test_another_disks_errors_leave_the_managers_verdict_to_decide(self) -> None:
        # No line of this disk: the manager's unreadable verdict still warns.
        found = self.evidence((self.USB,), "dk2", problems=(probe.DISK_PAYLOAD_UNREADABLE,))
        self.assertEqual(found, UNREADABLE_EVIDENCE)

    def test_without_mast_every_line_counts_as_before(self) -> None:
        # MaSt unread, or a volume it does not list: which disk holds the
        # volume is unknown, so no line is left out.
        for disks, volume in ((None, "dk2"), (self.DISKS, "dk9")):
            with self.subTest(disks=disks is not None, volume=volume):
                self.assertIsNotNone(self.evidence((self.USB,), volume, disks=disks))


class DiskInventoryLookupTests(unittest.TestCase):
    """MaSt for the warning's wording only, so a read that fails names no disk
    instead of failing the preflight, and stops there."""

    CONNECTION = SshConnection("root@10.0.0.2", "pw", "")
    DISK = (
        "<dict><key>deviceName</key><string>sd0</string><key>builtin</key><true/>"
        "<key>partitions</key><array/></dict>"
    )
    MAST = f'<?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><array>{DISK}</array></plist>'

    def lookup(self, run_ssh):
        acpd = mock.Mock(side_effect=AssertionError("a failed lookup must not check ACPd"))
        with mock.patch("timecapsulesmb.device.storage.run_ssh", run_ssh):
            with mock.patch("timecapsulesmb.device.storage.raise_if_acpd_silent", acpd):
                return read_disk_inventory_conn(self.CONNECTION)

    def test_an_answer_lists_the_disks_with_a_short_wait(self) -> None:
        from timecapsulesmb.device.disk_errors import DISK_INVENTORY_TIMEOUT_SECONDS
        from timecapsulesmb.device.storage import MAST_ACP_COMMAND
        run_ssh = mock.Mock(return_value=mock.Mock(stdout=self.MAST, returncode=0))
        disks = self.lookup(run_ssh)
        self.assertEqual([(disk.device, disk.builtin) for disk in disks], [("sd0", True)])
        run_ssh.assert_called_once_with(self.CONNECTION, MAST_ACP_COMMAND, timeout=DISK_INVENTORY_TIMEOUT_SECONDS)
        # Short against the 60 s a read that matters waits.
        self.assertLess(DISK_INVENTORY_TIMEOUT_SECONDS, 60)

    def test_no_answer_names_no_disk_and_reads_nothing_more(self) -> None:
        # One SSH call each: no syUT read, no process list after it.
        for name, run_ssh in (
            ("timeout", mock.Mock(side_effect=SshCommandTimeout("timed out"))),
            ("connection", mock.Mock(side_effect=SshNetworkError("reset"))),
            ("failed get", mock.Mock(return_value=mock.Mock(
                stdout="### get 'MaSt' failed: <<UNKNOWN FORMAT CONVERSION CODE %m>>", returncode=0))),
        ):
            with self.subTest(name):
                self.assertIsNone(self.lookup(run_ssh))
                run_ssh.assert_called_once()


class DiskFailingWarningTests(unittest.TestCase):
    """The preflight's words: which disk and that it may be failing, then the
    steps in order, and what the operation will not do."""

    INTERNAL = kernel_evidence((FINAL,), disks=(mast_disk("wd0"),))
    REFUSED = kernel_evidence((FINAL,), disks=(mast_disk("wd0", soft_disconnected=True),))
    USB = kernel_evidence(("sd1a: error writing fsbn 8",), disks=(mast_disk("sd0"), mast_disk("sd1", builtin=False)))
    UNKNOWN = kernel_evidence((FINAL,))
    STEPS = (
        "Copy off the files you need first. "
        "Then try restarting the device in AirPort Utility, or unplug it for 10 seconds and plug it back in. "
        "Run “Checkup” afterwards to see whether the errors come back. "
        "AirPort Utility may still show the disk as OK."
    )

    def test_install_on_an_internal_disk_with_errors_names_it_and_suggests_the_wipe(self) -> None:
        self.assertEqual(
            disk_failing_warning(self.INTERNAL, "install"),
            f"The internal disk reported errors it could not recover from ({FINAL}), so it may be failing. "
            f"{self.STEPS} If a bad sector is the cause, “Wipe Internal Disk” sometimes gets the disk working "
            "again, but it deletes everything on it. Installing will not fix this.",
        )

    def test_the_subject_names_the_disk_only_when_mast_did(self) -> None:
        # An internal disk Wipe refuses gets the plain wording, as the app
        # shows it under the plain id.
        for evidence, subject in ((self.INTERNAL, "The internal disk"), (self.USB, "A USB disk on the device"),
                                  (self.REFUSED, "The device's disk"), (self.UNKNOWN, "The device's disk"),
                                  (UNREADABLE_EVIDENCE, "The device's disk")):
            with self.subTest(disk=evidence.disk, cause=evidence.cause, wipeable=evidence.wipeable):
                self.assertTrue(disk_failing_warning(evidence, "uninstall").startswith(f"{subject} "))
                self.assertEqual(disk_failing_fields(evidence, "uninstall")["disk_failing_disk"], evidence.disk)

    def test_each_operation_ends_with_what_it_will_not_do(self) -> None:
        for operation, last in (("install", "Installing will not fix this."),
                                ("uninstall", "Uninstalling will not fix this."),
                                ("repair", "A disk repair may not fix this, and works the disk hard.")):
            for evidence in (self.INTERNAL, self.USB, self.REFUSED, UNREADABLE_EVIDENCE):
                with self.subTest(operation=operation, disk=evidence.disk, cause=evidence.cause):
                    warning = disk_failing_warning(evidence, operation)
                    self.assertTrue(warning.endswith(f" {last}"))
                    self.assertIn(", so it may be failing. " + self.STEPS, warning)
                    self.assertNotIn("probably", warning)

    def test_only_errors_on_an_internal_disk_wipe_accepts_suggest_the_wipe(self) -> None:
        # A USB disk's errors: Wipe Internal Disk formats another disk. An
        # internal disk it refuses: it would only refuse. Files read too
        # slowly say too little about a bad sector to suggest deleting
        # everything.
        for evidence, suggested in ((self.INTERNAL, True), (self.USB, False), (self.REFUSED, False),
                                    (self.UNKNOWN, False), (UNREADABLE_EVIDENCE, False)):
            for operation in ("install", "uninstall", "repair"):
                with self.subTest(disk=evidence.disk, cause=evidence.cause, operation=operation):
                    self.assertEqual("Wipe Internal Disk" in disk_failing_warning(evidence, operation), suggested)
                    self.assertEqual(disk_failing_fields(evidence, operation)["disk_failing_wipe_suggested"], suggested)

    def test_a_wipes_own_warning_ends_with_its_bad_sector_sentence(self) -> None:
        for evidence, first in ((self.INTERNAL, f"This disk reported errors it could not recover from ({FINAL})"),
                                (UNREADABLE_EVIDENCE, "This disk could not read the TimeCapsuleSMB files, or only too slowly")):
            with self.subTest(evidence.cause):
                self.assertEqual(
                    disk_failing_warning(evidence, "wipe"),
                    f"{first}, so it may be failing. {self.STEPS} If a bad sector is the cause, wiping sometimes "
                    "gets the disk working again, but the new volume may fail too.",
                )
                self.assertFalse(disk_failing_fields(evidence, "wipe")["disk_failing_wipe_suggested"])

    def test_the_command_line_names_its_commands(self) -> None:
        warning = disk_failing_warning(self.INTERNAL, "install", client="cli")
        self.assertIn("Run `tcapsule doctor` afterwards", warning)
        self.assertIn("If a bad sector is the cause, `tcapsule wipe-internal-disk` sometimes", warning)
        self.assertNotIn("“", warning)

    def test_telemetry_says_what_the_warning_was_based_on(self) -> None:
        self.assertEqual(disk_failing_fields(self.INTERNAL, "install"), {
            "disk_failing_warned": True, "disk_failing_cause": "disk_failing",
            "disk_failing_disk": "internal", "disk_failing_wipe_suggested": True,
        })
        self.assertEqual(disk_failing_fields(UNREADABLE_EVIDENCE, "uninstall"), {
            "disk_failing_warned": True, "disk_failing_cause": "disk_unreadable",
            "disk_failing_disk": "unknown", "disk_failing_wipe_suggested": False,
        })


class PrecedenceTests(unittest.TestCase):
    ERRORS = (FINAL,)

    def test_disk_errors_outrank_every_verdict_but_an_erase(self) -> None:
        self.assertTrue(disk_errors_explain(self.ERRORS))
        self.assertTrue(disk_errors_explain(self.ERRORS, (DAMAGED,)))
        # Files missing from every disk follow an Erase: errors from before
        # it explain nothing now.
        self.assertFalse(disk_errors_explain(self.ERRORS, (MISSING,)))

    def test_no_errors_or_a_serving_samba_explain_nothing(self) -> None:
        self.assertFalse(disk_errors_explain(None))
        self.assertFalse(disk_errors_explain(()))
        self.assertFalse(disk_errors_explain(self.ERRORS, samba_ready=True))


def runtime(*, samba_ready: bool = False, problem=None) -> ManagedRuntimeProbeResult:
    steps = (ProbeStepResult(problem.step_id, "fail", problem.message),) if problem is not None else ()
    return ManagedRuntimeProbeResult(
        ready=False,
        detail="managed smbd parent process is not running",
        smbd=ReadinessProbeResult(ready=samba_ready, detail="smbd"),
        mdns=ReadinessProbeResult(ready=samba_ready, detail="mdns"),
        extra_steps=steps,
        samba_ready=samba_ready,
    )


class RuntimeFailureTests(unittest.TestCase):
    ERRORS = (NETBSD4_DEVICE_ERROR[1],)

    def test_disk_errors_are_the_error_for_a_samba_that_did_not_start(self) -> None:
        for problem in (None, DAMAGED):
            with self.subTest(problem=problem and problem.token):
                error = runtime_failure_error(runtime(problem=problem), "NetBSD 4 activation failed.",
                                              disk_errors=self.ERRORS)
                self.assertIsInstance(error, DiskFailingError)
                # The error says why; its detail is the kernel's last disk error,
                # which Checkup's line names inline.
                self.assertEqual(
                    (error.code, error.message, error.detail),
                    (ErrorCode.DISK_FAILING, DISK_FAILING_MESSAGE, self.ERRORS[-1]),
                )
                self.assertIn(f"({self.ERRORS[-1]})", disk_failing_message(self.ERRORS))

    def test_without_them_the_error_is_what_it_was(self) -> None:
        for errors in (None, ()):
            with self.subTest(errors=errors):
                error = runtime_failure_error(runtime(), "NetBSD 4 activation failed.", disk_errors=errors)
                self.assertIs(type(error), DeviceError)
        self.assertIsInstance(runtime_failure_error(runtime(problem=MISSING), "x", disk_errors=self.ERRORS),
                              DiskPayloadMissingError)
        self.assertIsInstance(runtime_failure_error(runtime(samba_ready=True), "x", disk_errors=self.ERRORS),
                              RsyncNotRunningError)

    def test_a_disk_that_could_not_read_the_files_is_the_failing_disk_without_kernel_lines(self) -> None:
        # NetBSD 4's kernel log often kept no line: the manager's verdict is
        # the evidence, a cause with its own code whose guidance is a new
        # disk, not a reinstall. Kernel lines, when there are any, name the
        # error and come first.
        for errors in (None, ()):
            with self.subTest(errors=errors):
                error = runtime_failure_error(runtime(problem=probe.DISK_PAYLOAD_UNREADABLE),
                                              "NetBSD 4 activation failed.", disk_errors=errors)
                self.assertIsInstance(error, DiskPayloadUnreadableError)
                self.assertEqual(error.code, ErrorCode.PAYLOAD_UNREADABLE_ON_DISK)
                self.assertTrue(error.samba_off)
                self.assertIn("could not read the Samba files", str(error))
        error = runtime_failure_error(runtime(problem=probe.DISK_PAYLOAD_UNREADABLE), "x", disk_errors=self.ERRORS)
        self.assertIsInstance(error, DiskFailingError)
        # rsync's leaves Samba serving: its own code, which says so.
        error = runtime_failure_error(runtime(samba_ready=True, problem=probe.RSYNC_UNREADABLE), "x",
                                      disk_errors=self.ERRORS)
        self.assertIsInstance(error, RsyncUnreadableError)
        self.assertEqual(error.code, ErrorCode.RSYNC_UNREADABLE_ON_DISK)
        self.assertFalse(error.samba_off)
        self.assertTrue(str(error).startswith("File sharing and Time Machine are running, but the device's disk"))

    def verify(self, verification, read, *, listing: str = ""):
        debug: dict[str, object] = {}
        callbacks = OperationCallbacks(add_debug_fields=lambda **fields: debug.update(fields))
        verify_managed_runtime_ready(
            SshConnection("host", "pw", ""), callbacks=callbacks, stage="verify_runtime_activation",
            timeout_seconds=200, heading="Runtime verification:", failure_message="NetBSD 4 activation failed.",
            probe_runtime=mock.Mock(return_value=verification), read_runtime_logs=mock.Mock(return_value={}),
            read_disk_errors=read, read_processes=mock.Mock(return_value=listing),
        )
        return debug

    # Apple's mount_hfs asleep on a disk buffer: a disk that stopped
    # answering, which NetBSD 4's kernel log often keeps no line of.
    STALLED = "  421 379 39 D 127 biolock 0:00.00 mount_hfs"

    def test_a_process_stuck_on_the_disk_is_the_error_for_a_samba_that_did_not_start(self) -> None:
        for errors in (None, ()):
            with self.subTest(errors=errors):
                with self.assertRaises(DiskNotRespondingError) as raised:
                    self.verify(runtime(), mock.Mock(return_value=errors), listing=self.STALLED)
                error = raised.exception
                self.assertEqual(error.code, ErrorCode.DISK_NOT_RESPONDING)
                self.assertTrue(error.samba_off)
                self.assertEqual(error.message, DISK_NOT_RESPONDING_MESSAGE)
                self.assertEqual(error.detail, "Waiting for the disk: mount_hfs (pid 421) waiting on biolock for 127+ s.")

    def test_kernel_disk_errors_outrank_a_stuck_disk_and_a_serving_samba_ignores_it(self) -> None:
        stalls = tuple(disk_io_stalls(self.STALLED))
        self.assertIsInstance(runtime_failure_error(runtime(), "x", disk_errors=self.ERRORS, disk_stalls=stalls),
                              DiskFailingError)
        self.assertIsInstance(runtime_failure_error(runtime(samba_ready=True), "x", disk_stalls=stalls),
                              RsyncNotRunningError)
        self.assertIs(type(runtime_failure_error(runtime(), "x", disk_stalls=())), DeviceError)

    def test_the_stuck_processes_reach_telemetry(self) -> None:
        debug: dict[str, object] = {}
        callbacks = OperationCallbacks(add_debug_fields=lambda **fields: debug.update(fields))
        with self.assertRaises(DiskNotRespondingError):
            verify_managed_runtime_ready(
                SshConnection("host", "pw", ""), callbacks=callbacks, stage="verify_runtime_activation",
                timeout_seconds=200, heading="Runtime verification:", failure_message="failed.",
                probe_runtime=mock.Mock(return_value=runtime()), read_runtime_logs=mock.Mock(return_value={}),
                read_disk_errors=mock.Mock(return_value=None), read_processes=mock.Mock(return_value=self.STALLED),
            )
        self.assertEqual(debug["disk_io_stalls"], ["mount_hfs (pid 421) waiting on biolock for 127+ s"])

    def test_the_runtime_check_reads_them_only_when_samba_did_not_start(self) -> None:
        read = mock.Mock(return_value=self.ERRORS)
        with self.assertRaises(DiskFailingError):
            self.verify(runtime(), read)
        read.assert_called_once()

        ready = ManagedRuntimeProbeResult(
            ready=True, detail="managed runtime is ready", smbd=ReadinessProbeResult(ready=True, detail="smbd"),
            mdns=ReadinessProbeResult(ready=True, detail="mdns"), samba_ready=True,
        )
        read = mock.Mock(return_value=self.ERRORS)
        self.verify(ready, read)
        read.assert_not_called()

    def test_the_runtime_check_finds_its_reader_when_it_runs(self) -> None:
        # Resolved at call time like the other readers, so patching the
        # module's reader reaches a caller that passes none.
        with mock.patch("timecapsulesmb.services.runtime_verification.read_disk_errors_conn",
                        return_value=self.ERRORS) as read:
            with self.assertRaises(DiskFailingError):
                verify_managed_runtime_ready(
                    SshConnection("host", "pw", ""), callbacks=OperationCallbacks(), stage="verify_runtime_activation",
                    timeout_seconds=200, heading="Runtime verification:", failure_message="failed.",
                    probe_runtime=mock.Mock(return_value=runtime()), read_runtime_logs=mock.Mock(return_value={}),
                )
        read.assert_called_once()

    def test_the_lines_reach_telemetry(self) -> None:
        debug: dict[str, object] = {}
        callbacks = OperationCallbacks(add_debug_fields=lambda **fields: debug.update(fields))
        with self.assertRaises(DiskFailingError):
            verify_managed_runtime_ready(
                SshConnection("host", "pw", ""), callbacks=callbacks, stage="verify_runtime_activation",
                timeout_seconds=200, heading="Runtime verification:", failure_message="failed.",
                probe_runtime=mock.Mock(return_value=runtime()), read_runtime_logs=mock.Mock(return_value={}),
                read_disk_errors=mock.Mock(return_value=self.ERRORS),
            )
        self.assertEqual(debug["disk_error_lines"], list(self.ERRORS))

    def test_activate_names_the_disk_instead_of_the_manager_verdict(self) -> None:
        # The running manager says smbd is damaged; the disk says why.
        with mock.patch("timecapsulesmb.services.activation.require_compatible_install"):
            with mock.patch("timecapsulesmb.services.activation.probe_managed_runtime_conn",
                            return_value=runtime(problem=DAMAGED)):
                with mock.patch("timecapsulesmb.services.activation.run_activation_actions_and_verify") as run:
                    with mock.patch("timecapsulesmb.services.activation.read_disk_errors_conn",
                                    return_value=self.ERRORS):
                        with self.assertRaises(DiskFailingError):
                            activate_runtime(SshConnection("host", "pw", ""), [], callbacks=OperationCallbacks())
        run.assert_not_called()

    def test_activate_checks_disk_errors_before_an_acpd_refusal(self) -> None:
        from dataclasses import replace

        confirmed_quit = replace(runtime(), extra_steps=(
            ProbeStepResult(probe.ACPD_QUIT_STEP, "fail", probe.ACPD_QUIT_STEP_DETAIL),
        ))
        quit_listing = "107 1 15 S 0 select 0:00.32 sshd\ntcapsule-ps: end\n"
        cases = (
            ("probe confirmed quit", confirmed_quit, False, True, ErrorCode.ACPD_QUIT),
            ("quit after payload verdict", runtime(problem=DAMAGED), False, True, ErrorCode.ACPD_QUIT),
            # A missing-payload verdict cannot suppress kernel evidence once
            # ACPd is gone: its disk inventory is no longer authoritative.
            ("quit beside stale missing verdict", runtime(problem=MISSING), False, True, ErrorCode.ACPD_QUIT),
            ("restart never happened", runtime(), True, True, ErrorCode.ACPD_BROKEN),
            ("syUT silent", runtime(), False, False, ErrorCode.ACPD_BROKEN),
        )
        for name, verification, restart, answers, code in cases:
            for errors in (self.ERRORS, (), None):
                with self.subTest(case=name, errors=errors), \
                        mock.patch("timecapsulesmb.services.activation.require_compatible_install"), \
                        mock.patch("timecapsulesmb.services.activation.probe_managed_runtime_conn", return_value=verification), \
                        mock.patch("timecapsulesmb.services.activation.read_disk_errors_conn", return_value=errors) as read, \
                        mock.patch("timecapsulesmb.device.acpd.restart_verdict", return_value=RestartVerdict("hung" if restart else "none")), \
                        mock.patch("timecapsulesmb.device.acpd.acpd_answers", return_value=answers), \
                        mock.patch("timecapsulesmb.device.acpd.read_processes", return_value=quit_listing if answers else ""), \
                        mock.patch("timecapsulesmb.services.activation.run_activation_actions_and_verify") as start:
                    with self.assertRaises(DeviceError) as raised:
                        activate_runtime(SshConnection("host", "pw", ""), [], callbacks=OperationCallbacks())

                    read.assert_called_once()
                    start.assert_not_called()
                    error = raised.exception
                    self.assertEqual(error.code, ErrorCode.DISK_FAILING if errors else code)
                    if errors:
                        self.assertIsInstance(error, DiskFailingError)
                        self.assertEqual(error.detail, errors[-1])
                    elif code == ErrorCode.ACPD_QUIT:
                        self.assertIsInstance(error, AcpdQuitError)


    # ACPd running but silent behind a mount_hfs stuck on the disk.
    STALLED_ACPD_LISTING = (
        "107 1 15 S 0 select 0:00.32 sshd\n"
        "119 1 2 Sl 127 parked 0:00.83 ACPd\n"
        "421 379 39 D 127 biolock 0:00.00 mount_hfs\n"
        "tcapsule-ps: end\n"
    )

    def activate(self, listings, *, errors=None, answers=False):
        """activate_runtime with these process listings, read in turn."""
        debug: dict[str, object] = {}
        callbacks = OperationCallbacks(add_debug_fields=lambda **fields: debug.update(fields))
        with mock.patch("timecapsulesmb.services.activation.require_compatible_install"), \
                mock.patch("timecapsulesmb.services.activation.probe_managed_runtime_conn", return_value=runtime()), \
                mock.patch("timecapsulesmb.services.activation.read_disk_errors_conn", return_value=errors), \
                mock.patch("timecapsulesmb.device.acpd.restart_verdict", return_value=RestartVerdict("none")) as marker, \
                mock.patch("timecapsulesmb.device.acpd.acpd_answers", return_value=answers) as syut, \
                mock.patch("timecapsulesmb.device.acpd.read_processes", side_effect=listings) as listing, \
                mock.patch("timecapsulesmb.services.activation.run_activation_actions_and_verify") as start:
            with self.assertRaises(DeviceError) as raised:
                activate_runtime(SshConnection("host", "pw", ""), [], callbacks=callbacks)
        start.assert_not_called()
        self.marker = marker
        return raised.exception, debug, syut, listing

    def test_activate_refuses_a_stuck_disk_before_it_reads_acpd(self) -> None:
        # ACPd waits behind the disk: its silence is the disk's, said as such.
        error, debug, syut, listing = self.activate([self.STALLED_ACPD_LISTING])
        self.assertIsInstance(error, DiskNotRespondingError)
        self.assertEqual(error.code, ErrorCode.DISK_NOT_RESPONDING)
        self.assertEqual(error.message, f"NetBSD 4 activation failed. {DISK_NOT_RESPONDING_MESSAGE} Nothing was started.")
        self.assertNotIn("Apple ACPd is broken", str(error))
        self.assertEqual(error.detail, "Waiting for the disk: mount_hfs (pid 421) waiting on biolock for 127+ s.")
        self.assertEqual(debug["disk_io_stalls"], ["mount_hfs (pid 421) waiting on biolock for 127+ s"])
        # Neither the restart marker nor syUT is read.
        self.marker.assert_not_called()
        syut.assert_not_called()
        listing.assert_called_once()

    def test_activate_refuses_a_stuck_disk_check_with_its_own_code(self) -> None:
        # A repair on a failing disk can wait that long and still finish: its
        # way out gives it time before the unplug.
        stuck = "107 1 15 S 0 select 0:00.32 sshd\n4211 4200 4200 D 127 biowait 1:12.04 fsck_hfs\ntcapsule-ps: end\n"
        error, _debug, syut, listing = self.activate([stuck])
        self.assertIsInstance(error, DiskNotRespondingError)
        self.assertEqual(error.code, ErrorCode.DISK_CHECK_NOT_RESPONDING)
        self.assertEqual(error.message, f"NetBSD 4 activation failed. {DISK_NOT_RESPONDING_MESSAGE} Nothing was started.")
        syut.assert_not_called()
        listing.assert_called_once()

    def test_activate_refuses_while_a_disk_check_keeps_running(self) -> None:
        # Disk Repair stopped the managed processes: a manager started now
        # would ask diskd for the volume under repair.
        repair = "107 1 15 S 0 select 0:00.32 sshd\n4211 4200 4200 R 0 - 1:12.04 fsck_hfs\ntcapsule-ps: end\n"
        with mock.patch("timecapsulesmb.device.acpd.pause") as pause:
            error, debug, syut, listing = self.activate([repair, repair], answers=True)
        self.assertIsInstance(error, DiskCheckRunningError)
        self.assertEqual(error.code, ErrorCode.FSCK_ALREADY_RUNNING)
        self.assertEqual(
            error.message,
            "NetBSD 4 activation failed. A disk check (fsck_hfs) is still running on the device. Restarting or "
            "unplugging the device now would interrupt it. Nothing was started.",
        )
        self.assertEqual(error.detail, "Running: fsck_hfs (pid 4211).")
        self.assertEqual(debug["disk_checks_running"], ["fsck_hfs (pid 4211)"])
        pause.assert_called_once()
        self.marker.assert_not_called()
        syut.assert_not_called()
        self.assertEqual(listing.call_count, 2)

    def test_activate_reads_the_list_again_before_it_blames_a_silent_acpd(self) -> None:
        # mount_hfs passed the stall threshold during ACPd's slow reads.
        young = self.STALLED_ACPD_LISTING.replace(" D 127 biolock", " D 100 biolock")
        error, _debug, syut, listing = self.activate([young, self.STALLED_ACPD_LISTING])
        self.assertIsInstance(error, DiskNotRespondingError)
        syut.assert_called_once()
        self.assertEqual(listing.call_count, 2)

    def test_activate_reads_the_list_again_when_acpd_is_silent_beside_a_stuck_fork(self) -> None:
        # A stuck fork of ACPd does not explain its silence; the disk that
        # passed the stall threshold meanwhile does.
        fork = "352 119 2 S 1 nanoslp 0:00.00 ACPd\n"
        young = self.STALLED_ACPD_LISTING.replace(" D 127 biolock", " D 100 biolock").replace("421 ", fork + "421 ")
        stalled = self.STALLED_ACPD_LISTING.replace("421 ", fork + "421 ")
        error, debug, syut, listing = self.activate([young, stalled])
        self.assertIsInstance(error, DiskNotRespondingError)
        syut.assert_called_once()
        self.assertEqual(listing.call_count, 2)
        # The error names only the disk; what ACPd showed is still recorded.
        self.assertEqual(debug["acpd_problems"], ["syut_silent", "stuck_child"])
        self.assertEqual(debug["acpd_stuck_children"], [352])

    def test_activate_names_a_failing_disk_ahead_of_a_stuck_one(self) -> None:
        # Kernel disk errors say more than a stall: the disk is failing.
        error, _debug, _syut, _listing = self.activate([self.STALLED_ACPD_LISTING], errors=self.ERRORS)
        self.assertIsInstance(error, DiskFailingError)

    def test_activate_without_a_stall_keeps_the_acpd_refusal(self) -> None:
        healthy = self.STALLED_ACPD_LISTING.replace("421 379 39 D 127 biolock 0:00.00 mount_hfs\n", "")
        error, debug, _syut, listing = self.activate([healthy, healthy])
        self.assertEqual(error.code, ErrorCode.ACPD_BROKEN)
        self.assertIn("Apple ACPd is broken", str(error))
        self.assertEqual(debug["acpd_problems"], ["syut_silent"])
        self.assertEqual(listing.call_count, 2)


class CommandLinePreflightTests(unittest.TestCase):
    CONNECTION = SshConnection("root@10.0.0.2", "pw", "")

    def preflight(self, errors, *, problems=(), yes=False, answer="", operation="install", disks=None):
        fields: dict[str, object] = {}
        stderr = io.StringIO()
        prompts: list[str] = []

        def ask(prompt=""):
            prompts.append(prompt)
            return answer

        with mock.patch("timecapsulesmb.device.disk_errors.read_disk_errors_conn", return_value=errors):
            with mock.patch("timecapsulesmb.device.disk_errors.manager_payload_problems", return_value=problems):
                with mock.patch("timecapsulesmb.device.disk_errors.read_disk_inventory_conn",
                                return_value=disks):
                    with mock.patch("builtins.input", side_effect=ask), redirect_stderr(stderr):
                        result = confirm_despite_failing_disk(self.CONNECTION, operation, yes=yes, command="deploy",
                                                              update_fields=lambda **f: fields.update(f))
        return result, stderr.getvalue(), prompts, fields

    def test_a_healthy_disk_asks_nothing(self) -> None:
        # An unread kernel log asks nothing either, and telemetry says so.
        for errors, problems, recorded in ((None, (), {"disk_failing_kernel_log": "unread"}), ((), (), {}),
                                           ((), (probe.DISK_PAYLOAD_DAMAGED,), {})):
            with self.subTest(errors=errors, problems=problems):
                result, warned, prompts, fields = self.preflight(errors, problems=problems)
                self.assertIsNone(result)
                self.assertEqual((warned, prompts, fields), ("", [], recorded))

    def test_a_failing_disk_warns_and_defaults_to_no(self) -> None:
        # From the kernel's lines, or the manager's title where it kept none.
        for errors, problems, evidence in (((FINAL,), (), kernel_evidence((FINAL,))),
                                           ((), (probe.DISK_PAYLOAD_UNREADABLE,), UNREADABLE_EVIDENCE)):
            for answer, expected in (("", False), ("n", False), ("y", True)):
                with self.subTest(evidence=evidence, answer=answer):
                    result, warned, prompts, fields = self.preflight(errors, problems=problems, answer=answer)
                    self.assertIs(result, expected)
                    self.assertIn(f"Warning: {disk_failing_warning(evidence, 'install', client='cli')}", warned)
                    self.assertEqual(prompts, ["Install anyway? [y/N]: "])
                    self.assertEqual(fields, disk_failing_fields(evidence, "install"))

    def test_yes_goes_on_after_printing_the_warning(self) -> None:
        result, warned, prompts, fields = self.preflight((FINAL,), yes=True, operation="repair",
                                                         disks=(mast_disk("wd0"),))

        self.assertTrue(result)
        self.assertIn("A disk repair may not fix this, and works the disk hard.", warned)
        # The internal disk's errors: the CLI names its wipe command.
        self.assertIn("`tcapsule wipe-internal-disk`", warned)
        self.assertTrue(fields["disk_failing_wipe_suggested"])
        self.assertEqual(prompts, [])


if __name__ == "__main__":
    unittest.main()
