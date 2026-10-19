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
    DiskFailingError,
    disk_errors_explain,
    disk_errors_in,
    disk_failing_message,
    disk_failing_warning,
    read_disk_errors_conn,
)
from timecapsulesmb.device.errors import DeviceError, DiskPayloadMissingError, RsyncNotRunningError
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


class CommandLinePreflightTests(unittest.TestCase):
    CONNECTION = SshConnection("root@10.0.0.2", "pw", "")

    def preflight(self, errors, *, yes=False, answer="", operation="install"):
        fields: dict[str, object] = {}
        stderr = io.StringIO()
        prompts: list[str] = []

        def ask(prompt=""):
            prompts.append(prompt)
            return answer

        with mock.patch("timecapsulesmb.cli.runtime.read_disk_errors_conn", return_value=errors):
            with mock.patch("builtins.input", side_effect=ask), redirect_stderr(stderr):
                result = confirm_despite_failing_disk(self.CONNECTION, operation, yes=yes, command="deploy",
                                                      update_fields=lambda **f: fields.update(f))
        return result, stderr.getvalue(), prompts, fields

    def test_a_healthy_disk_asks_nothing(self) -> None:
        for errors in (None, ()):
            with self.subTest(errors=errors):
                result, warned, prompts, fields = self.preflight(errors)
                self.assertIsNone(result)
                self.assertEqual((warned, prompts, fields), ("", [], {}))

    def test_a_failing_disk_warns_and_defaults_to_no(self) -> None:
        for answer, expected in (("", False), ("n", False), ("y", True)):
            with self.subTest(answer=answer):
                result, warned, prompts, fields = self.preflight((FINAL,), answer=answer)
                self.assertIs(result, expected)
                self.assertIn(disk_failing_warning((FINAL,), "install"), warned)
                self.assertEqual(prompts, ["Install anyway? [y/N]: "])
                self.assertEqual(fields, {"disk_failing_warned": True})

    def test_yes_goes_on_after_printing_the_warning(self) -> None:
        result, warned, prompts, _fields = self.preflight((FINAL,), yes=True, operation="repair")

        self.assertTrue(result)
        self.assertIn("A disk repair cannot fix a failing disk", warned)
        self.assertEqual(prompts, [])


if __name__ == "__main__":
    unittest.main()
