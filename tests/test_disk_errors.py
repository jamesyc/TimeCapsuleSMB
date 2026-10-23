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
    DISK_FAILING_MESSAGE,
    KERNEL_DISK_MESSAGES_COMMAND,
    UNREADABLE_EVIDENCE,
    DiskFailingError,
    disk_errors_explain,
    disk_errors_in,
    disk_failing_message,
    disk_failing_warning,
    kernel_evidence,
    read_disk_errors_conn,
    read_disk_failing_evidence_conn,
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
from timecapsulesmb.device.probe import (
    DISK_PAYLOAD_PROBLEMS,
    ManagedRuntimeProbeResult,
    ProbeStepResult,
    ReadinessProbeResult,
)
from timecapsulesmb.services.activation import activate_runtime
from timecapsulesmb.services.callbacks import OperationCallbacks
from timecapsulesmb.services.runtime_verification import runtime_failure_error, verify_managed_runtime_ready
from timecapsulesmb.transport.errors import SshCommandTimeout, SshNetworkError
from timecapsulesmb.transport.ssh import SshConnection


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

    def evidence(self, errors, problems):
        title = problems if callable(problems) else mock.Mock(return_value=problems)
        with mock.patch("timecapsulesmb.device.disk_errors.read_disk_errors_conn", return_value=errors):
            with mock.patch("timecapsulesmb.device.disk_errors.manager_payload_problems", title):
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

    def test_the_warning_says_what_the_evidence_is(self) -> None:
        self.assertEqual(
            disk_failing_warning(UNREADABLE_EVIDENCE, "install"),
            "The device's disk could not read the TimeCapsuleSMB files, or only too slowly, so it is probably failing. Installing will not fix that. "
            "Copy off the files you need first.",
        )
        self.assertIn(f"({FINAL}), so it is probably failing. Uninstalling will not fix that.",
                      disk_failing_warning(kernel_evidence((FINAL,)), "uninstall"))


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
                error = runtime_failure_error(runtime(problem=problem), "NetBSD4 activation failed.",
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
                error = runtime_failure_error(runtime(), "NetBSD4 activation failed.", disk_errors=errors)
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
                                              "NetBSD4 activation failed.", disk_errors=errors)
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

    def verify(self, verification, read):
        debug: dict[str, object] = {}
        callbacks = OperationCallbacks(add_debug_fields=lambda **fields: debug.update(fields))
        verify_managed_runtime_ready(
            SshConnection("host", "pw", ""), callbacks=callbacks, stage="verify_runtime_activation",
            timeout_seconds=200, heading="Runtime verification:", failure_message="NetBSD4 activation failed.",
            probe_runtime=mock.Mock(return_value=verification), read_runtime_logs=mock.Mock(return_value={}),
            read_disk_errors=read,
        )
        return debug

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
                        mock.patch("timecapsulesmb.device.acpd.restart_never_happened", return_value=restart), \
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


class CommandLinePreflightTests(unittest.TestCase):
    CONNECTION = SshConnection("root@10.0.0.2", "pw", "")

    def preflight(self, errors, *, problems=(), yes=False, answer="", operation="install"):
        fields: dict[str, object] = {}
        stderr = io.StringIO()
        prompts: list[str] = []

        def ask(prompt=""):
            prompts.append(prompt)
            return answer

        with mock.patch("timecapsulesmb.device.disk_errors.read_disk_errors_conn", return_value=errors):
            with mock.patch("timecapsulesmb.device.disk_errors.manager_payload_problems", return_value=problems):
                with mock.patch("builtins.input", side_effect=ask), redirect_stderr(stderr):
                    result = confirm_despite_failing_disk(self.CONNECTION, operation, yes=yes, command="deploy",
                                                          update_fields=lambda **f: fields.update(f))
        return result, stderr.getvalue(), prompts, fields

    def test_a_healthy_disk_asks_nothing(self) -> None:
        for errors, problems in ((None, ()), ((), ()), ((), (probe.DISK_PAYLOAD_DAMAGED,))):
            with self.subTest(errors=errors, problems=problems):
                result, warned, prompts, fields = self.preflight(errors, problems=problems)
                self.assertIsNone(result)
                self.assertEqual((warned, prompts, fields), ("", [], {}))

    def test_a_failing_disk_warns_and_defaults_to_no(self) -> None:
        # From the kernel's lines, or the manager's title where it kept none.
        for errors, problems, evidence in (((FINAL,), (), kernel_evidence((FINAL,))),
                                           ((), (probe.DISK_PAYLOAD_UNREADABLE,), UNREADABLE_EVIDENCE)):
            for answer, expected in (("", False), ("n", False), ("y", True)):
                with self.subTest(evidence=evidence, answer=answer):
                    result, warned, prompts, fields = self.preflight(errors, problems=problems, answer=answer)
                    self.assertIs(result, expected)
                    self.assertIn(disk_failing_warning(evidence, "install"), warned)
                    self.assertEqual(prompts, ["Install anyway? [y/N]: "])
                    self.assertEqual(fields, {"disk_failing_warned": True})

    def test_yes_goes_on_after_printing_the_warning(self) -> None:
        result, warned, prompts, _fields = self.preflight((FINAL,), yes=True, operation="repair")

        self.assertTrue(result)
        self.assertIn("A disk repair cannot fix a failing disk", warned)
        self.assertEqual(prompts, [])


if __name__ == "__main__":
    unittest.main()
