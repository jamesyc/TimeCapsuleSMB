"""The host's side of the disk-owner handoff (build/native/README.md, "Disk
owner handoff"): reading the manager's diskd= word, the runtime wait (which
needs listeners and a manager behind them, not the handoff), the errors at
the end of a wait that failed, Activate's decisions, and Checkup's report."""
from __future__ import annotations

import subprocess
from dataclasses import replace
import unittest
from types import SimpleNamespace
from unittest import mock

from timecapsulesmb.checks.doctor_state import DoctorSink, DoctorTarget, RemoteAccess
from timecapsulesmb.checks.doctor_steps import _doctor_check_diskd_handoff
from timecapsulesmb.checks.models import CheckResult
from timecapsulesmb.core.error_codes import ErrorCode
from timecapsulesmb.device.diskd import (
    PENDING_MESSAGE,
    RESTART_AFTER_SECONDS,
    STUCK_DISKD_SECONDS,
    DiskdHandoff,
    StuckDiskd,
    parse_diskd_handoff,
    stuck_diskd,
)
from timecapsulesmb.device.disk_errors import DiskNotRespondingError
from timecapsulesmb.device.errors import DeviceError, RsyncNotRunningError
from timecapsulesmb.device.processes import PS_LISTING_END, disk_io_stalls
from timecapsulesmb.device.probe import (
    MANAGER_NOT_RUNNING_STEP,
    ManagedRuntimeProbeResult,
    ManagerStartupAgeProbeResult,
    ManagerState,
    ManagerVerdicts,
    ProbeStepResult,
    ReadinessProbeResult,
    probe_managed_runtime_conn,
    read_manager_state,
)
from timecapsulesmb.services.activation import decide_manual_activation
from timecapsulesmb.services.callbacks import OperationCallbacks
from timecapsulesmb.services.runtime_verification import runtime_failure_error, verify_managed_runtime_ready
from timecapsulesmb.transport.ssh import SshConnection

CONNECTION = SshConnection("root@10.0.0.2", "pw", "")
READY = DiskdHandoff("ready", 40, "none")

# A TimeCapsule6,116 five minutes after the manager's TERM wedged Apple's
# diskd (README F19): diskd asleep on netio since, beside ACPd.
WEDGED_ACPD = "  190     1    15 Ia   127 netio    0:02.46 ACPd"
WEDGED_DISKD = "  228   190    15 I    127 netio    0:00.02 diskd           /sbin/diskd -i  -d local. "
WEDGED_MANAGER = ("  417     1   375 S      1 select   0:00.37 service         "
                  "service: role=manager started=25 diskd=stopping:25:none ")
WEDGED_SSHD = "  107   190    15 S      0 select   0:00.16 sshd"
WEDGED_NETBSD4 = "\n".join((WEDGED_SSHD, WEDGED_ACPD, WEDGED_DISKD, WEDGED_MANAGER, PS_LISTING_END))


def diskd_row(*, sl: int = 127, wchan: str = "netio", stat: str = "I", ppid: int = 190,
              argv: str = "/sbin/diskd -i  -d local.") -> str:
    return f"  228 {ppid} 15 {stat} {sl} {wchan} 0:00.02 diskd {argv}"

STARTING = DiskdHandoff("starting", 40, "none")


def listener_result(ready: bool, *, samba_ready: bool | None = None) -> ManagedRuntimeProbeResult:
    samba = ready if samba_ready is None else samba_ready
    smbd = ReadinessProbeResult(ready=samba, detail="managed smbd ready" if samba else "managed smbd not ready")
    mdns = ReadinessProbeResult(ready=samba, detail="managed mDNS registrant active")
    return ManagedRuntimeProbeResult(ready=ready, detail="listeners", smbd=smbd, mdns=mdns, samba_ready=samba)


def state(present: bool | None) -> ManagerState:
    return ManagerState((), True, present)


def wait(listeners: bool, manager: ManagerState | None) -> ManagedRuntimeProbeResult:
    """One whole runtime wait on these listeners and this listing, every attempt alike."""
    with (
        mock.patch("timecapsulesmb.device.probe.probe_managed_runtime_once_conn", return_value=listener_result(listeners)),
        mock.patch("timecapsulesmb.device.probe.read_manager_state", return_value=manager),
        mock.patch("timecapsulesmb.device.probe.time.sleep"),
    ):
        return probe_managed_runtime_conn(CONNECTION, timeout_seconds=0, poll_interval_seconds=0,
                                          smbd_mdns_stagger_seconds=0, mdns_settle_seconds=0)


def manager_read(handoff: DiskdHandoff | None, now_ms: int | None = 10**6) -> ManagerStartupAgeProbeResult:
    return ManagerStartupAgeProbeResult(1.0, "manager started", now_monotonic_ms=now_ms, handoff=handoff)


class ParseTests(unittest.TestCase):
    def test_reads_the_word_anywhere_in_the_title(self) -> None:
        title = ["started=12", "diskd=quiesce:55:maintenance", "waiting=hostname"]
        self.assertEqual(parse_diskd_handoff(title), DiskdHandoff("quiesce", 55, "maintenance"))

    def test_a_title_without_the_word_is_a_manager_before_v3_8_5(self) -> None:
        self.assertIsNone(parse_diskd_handoff(["started=12", "payload=missing"]))

    def test_a_word_without_its_shape_says_nothing(self) -> None:
        for word in ("diskd=ready:5", "diskd=ready:-1:none", "diskd=ready:٣:none", "diskd=ready::none",
                     "diskd=ready:5:none:x", "diskd="):
            with self.subTest(word):
                self.assertIsNone(parse_diskd_handoff(["started=1", word]))

    def test_a_phase_or_reason_from_a_newer_release_is_kept_as_written(self) -> None:
        handoff = parse_diskd_handoff(["diskd=reconciling:5:somewhere_new"])
        self.assertEqual(handoff, DiskdHandoff("reconciling", 5, "somewhere_new"))
        self.assertFalse(handoff.ready)
        # Never a restart: only reasons this release knows can be judged stuck.
        self.assertEqual(handoff.verdict(10**12), (ErrorCode.DISKD_HANDOFF_PENDING, PENDING_MESSAGE))


class VerdictTests(unittest.TestCase):
    def test_ready_needs_no_verdict(self) -> None:
        self.assertIsNone(READY.verdict(10**9))

    def test_only_a_stuck_reason_held_ten_minutes_asks_for_a_restart(self) -> None:
        for phase, reason in (("inspect", "conflict"), ("starting", "conflict"), ("starting", "no_answer"),
                              ("inspect", "residual_mounts"), ("stopping", "none"), ("quiesce", "busy")):
            handoff = DiskdHandoff(phase, 100, reason)
            with self.subTest(phase=phase, reason=reason):
                before = handoff.verdict((100 + RESTART_AFTER_SECONDS - 1) * 1000 + 999)
                self.assertEqual(before, (ErrorCode.DISKD_HANDOFF_PENDING, PENDING_MESSAGE))
                code, message = handoff.verdict((100 + RESTART_AFTER_SECONDS) * 1000)
                self.assertEqual(code, ErrorCode.DISKD_RESTART_NEEDED)
                self.assertIn('restart the device, then run "Checkup" again', message)

    def test_no_answer_names_every_way_a_request_goes_unanswered(self) -> None:
        _code, message = DiskdHandoff("starting", 0, "no_answer").verdict(RESTART_AFTER_SECONDS * 1000)
        self.assertIn("has not answered", message)
        for way in ("failed", "timed out", "could not be sent"):
            self.assertIn(way, message)

    def test_a_stock_diskd_or_acpd_that_never_idles_is_named_as_the_cause(self) -> None:
        # The manager holds the TERM until Apple's diskd idles in its event
        # loop and ACPd is quiet (README F19); one that never does needs the
        # restart. busy covers both, so the message names both.
        code, message = DiskdHandoff("quiesce", 0, "busy").verdict(RESTART_AFTER_SECONDS * 1000)
        self.assertEqual(code, ErrorCode.DISKD_RESTART_NEEDED)
        self.assertTrue(message.startswith("Apple's own disk service could not be asked to hand over the disks"))
        self.assertIn("Apple's AirPort service, never became idle", message)

    def test_the_pending_message_names_no_single_cause(self) -> None:
        # Pending also covers no answer yet, a conflict and leftover mounts.
        self.assertEqual(PENDING_MESSAGE, "Apple's disk service is still preparing file sharing.")

    def test_a_long_disk_check_or_shutdown_only_waits(self) -> None:
        # A stock diskd whose TERM waits for its disk check, a diskd checking
        # disks before it answers, a launch backing off: none is stuck.
        for handoff in (DiskdHandoff("quiesce", 0, "maintenance"), DiskdHandoff("stopping", 0, "maintenance"),
                        DiskdHandoff("starting", 0, "maintenance"), DiskdHandoff("launching", 0, "none"),
                        DiskdHandoff("inspect", 0, "relaunch_wait"), DiskdHandoff("inspect", 0, "observation")):
            with self.subTest(handoff):
                self.assertEqual(handoff.verdict(10 * 3600 * 1000)[0], ErrorCode.DISKD_HANDOFF_PENDING)

    def test_without_a_clock_reading_it_cannot_time_the_reason(self) -> None:
        self.assertEqual(DiskdHandoff("inspect", 0, "conflict").verdict(None)[0], ErrorCode.DISKD_HANDOFF_PENDING)


class ReadManagerStateTests(unittest.TestCase):
    MANAGER = " 250 1 250 Ss 0 - 0:41 service service: role=manager started=19 diskd=starting:20:none"
    SSHD = " 107 1 15 S 0 select 0:00.32 sshd"

    def read(self, *rows: str) -> ManagerState | None:
        proc = subprocess.CompletedProcess([], 0, stdout="\n".join(rows) + "\n", stderr="")
        with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=proc):
            return read_manager_state(CONNECTION)

    def test_only_a_finished_listing_shows_the_manager_absent(self) -> None:
        self.assertIs(self.read(self.MANAGER, self.SSHD, PS_LISTING_END).present, True)
        self.assertIs(self.read(self.SSHD, PS_LISTING_END).present, False)
        self.assertIsNone(self.read(self.SSHD).present)
        second = self.MANAGER.replace(" 250 1 250", " 251 1 251")
        self.assertIsNone(self.read(self.MANAGER, second, PS_LISTING_END).present)


class RuntimeWaitTests(unittest.TestCase):
    def test_listeners_with_a_manager_are_ready_whatever_its_handoff(self) -> None:
        # Samba and rsync start only once the handoff is ready, so listeners
        # from this manager already mean it was.
        result = wait(True, state(True))
        self.assertTrue(result.ready)
        self.assertIs(result.manager_present, True)

    def test_listeners_without_a_manager_are_not_ready(self) -> None:
        result = wait(True, state(False))
        self.assertFalse(result.ready)
        self.assertIn(MANAGER_NOT_RUNNING_STEP, {step.id for step in result.steps})
        self.assertIs(result.manager_present, False)

    def test_a_listing_that_cannot_tell_leaves_the_listeners_to_decide(self) -> None:
        for manager in (None, state(None)):
            with self.subTest(manager):
                self.assertTrue(wait(True, manager).ready)

    def test_listeners_down_without_a_manager_end_on_the_missing_manager(self) -> None:
        result = wait(False, state(False))
        self.assertIn(MANAGER_NOT_RUNNING_STEP, {step.id for step in result.steps})
        self.assertNotIn("runtime_timeout", {step.id for step in result.steps})

    def test_listeners_down_with_a_manager_time_out_as_before(self) -> None:
        result = wait(False, state(True))
        self.assertIn("runtime_timeout", {step.id for step in result.steps})

    def test_the_soft_window_keeps_the_last_presence_it_read(self) -> None:
        verdicts = ManagerVerdicts()
        verdicts.update_state(state(False))
        verdicts.update_state(None)  # a listing that failed says nothing
        verdicts.update_state(state(None))  # nor does one that cannot tell
        self.assertIs(verdicts.lasting().manager_present, False)


class StuckDiskdTests(unittest.TestCase):
    """Apple's diskd asleep on netio for good (diskd.stuck_diskd), from a listing alone."""

    def test_the_field_wedge_is_stuck(self) -> None:
        stuck = stuck_diskd(WEDGED_NETBSD4)
        self.assertEqual(stuck, StuckDiskd(228, 127, capped=True))
        self.assertEqual(stuck.describe(), "diskd (pid 228) waiting on netio for 127+ s")
        self.assertIn("more than 127 seconds", stuck.message)

    def test_it_needs_the_threshold_on_netio(self) -> None:
        cases = (
            ("at the threshold", diskd_row(sl=STUCK_DISKD_SECONDS), StuckDiskd(228, STUCK_DISKD_SECONDS, False)),
            ("a second short", diskd_row(sl=STUCK_DISKD_SECONDS - 1), None),
            # Idle in its event loop: a healthy stock diskd.
            ("idle in kevent", diskd_row(wchan="kqread"), None),
            # Waiting on the disk: processes.disk_io_stalls names that.
            ("on the disk", diskd_row(stat="D", wchan="biowait"), None),
            ("an exited diskd", diskd_row(stat="Z"), None),
        )
        for name, row, expected in cases:
            with self.subTest(name):
                self.assertEqual(stuck_diskd(f"{WEDGED_ACPD}\n{row}"), expected)

    def test_only_apples_own_diskd_counts(self) -> None:
        cases = (
            ("the manager's loopback diskd", diskd_row(ppid=417, argv="/usr/libexec/diskd -i lo0 -d local."), None),
            ("stock on bridge0", diskd_row(argv="/sbin/diskd -i bridge0 -d local."), StuckDiskd(228, 127, True)),
            # A command line that was not read says nothing: ps reads every
            # one but a process's in uninterruptible sleep, which netio is not.
            ("unreadable", diskd_row(argv="(unreadable)"), None),
            ("no command line", diskd_row(argv=""), None),
        )
        for name, row, expected in cases:
            with self.subTest(name):
                self.assertEqual(stuck_diskd(f"{WEDGED_ACPD}\n{row}"), expected)

    def test_no_manager_is_needed(self) -> None:
        # A deploy stops the manager and leaves the wedged diskd behind.
        self.assertIsNotNone(stuck_diskd(f"{WEDGED_ACPD}\n{WEDGED_DISKD}"))
        self.assertIsNone(stuck_diskd(f"{WEDGED_ACPD}\n{WEDGED_MANAGER}"))
        self.assertIsNone(stuck_diskd(""))


class RuntimeFailureTests(unittest.TestCase):
    @staticmethod
    def failed(step_id: str = "runtime_timeout", *, samba_ready: bool = False) -> ManagedRuntimeProbeResult:
        result = listener_result(False, samba_ready=samba_ready)
        step = ProbeStepResult(id=step_id, status="fail", detail="timed out")
        return ManagedRuntimeProbeResult(ready=False, detail="x", smbd=result.smbd, mdns=result.mdns,
                                         samba_ready=samba_ready, extra_steps=(step,))

    def test_a_handoff_read_after_the_wait_explains_it(self) -> None:
        stuck = DiskdHandoff("starting", 100, "no_answer")
        error = runtime_failure_error(self.failed(), "Failed.", manager=manager_read(stuck, 200 * 1000))
        self.assertEqual(error.code, ErrorCode.DISKD_HANDOFF_PENDING)
        self.assertIn("reason=no_answer", error.detail)
        error = runtime_failure_error(self.failed(), "Failed.",
                                      manager=manager_read(stuck, (100 + RESTART_AFTER_SECONDS) * 1000))
        self.assertEqual(error.code, ErrorCode.DISKD_RESTART_NEEDED)

    def test_it_explains_rsync_missing_beside_serving_samba_too(self) -> None:
        conflict = DiskdHandoff("starting", 100, "conflict")
        error = runtime_failure_error(self.failed(samba_ready=True), "Failed.",
                                      manager=manager_read(conflict, 200 * 1000))
        self.assertEqual(error.code, ErrorCode.DISKD_HANDOFF_PENDING)
        self.assertNotIsInstance(error, RsyncNotRunningError)

    def test_without_a_clock_reading_it_reports_a_wait(self) -> None:
        error = runtime_failure_error(self.failed(), "Failed.",
                                      manager=manager_read(DiskdHandoff("inspect", 0, "conflict"), None))
        self.assertEqual(error.code, ErrorCode.DISKD_HANDOFF_PENDING)

    def test_a_handoff_that_finished_or_cannot_be_read_gives_the_usual_error(self) -> None:
        for manager in (manager_read(READY), manager_read(None), None):
            with self.subTest(manager):
                error = runtime_failure_error(self.failed(), "Failed.", manager=manager)
                self.assertNotIn(error.code, (ErrorCode.DISKD_HANDOFF_PENDING, ErrorCode.DISKD_RESTART_NEEDED))
                self.assertIn("Failed.", str(error))

    def test_a_process_stuck_on_the_disk_comes_before_the_handoff(self) -> None:
        stalls = tuple(disk_io_stalls("  421 379 39 D 127 biolock 0:00.00 mount_hfs\n"))
        error = runtime_failure_error(self.failed(), "Failed.", disk_stalls=stalls, manager=manager_read(STARTING))
        self.assertIsInstance(error, DiskNotRespondingError)

    def test_a_stuck_diskd_comes_before_the_handoff_and_the_manager(self) -> None:
        stuck = StuckDiskd(228, 127, True)
        pending = DiskdHandoff("stopping", 25, "none")
        for failed, manager in ((self.failed(), manager_read(pending)),
                                (self.failed(), manager_read(pending, (25 + RESTART_AFTER_SECONDS) * 1000)),
                                (self.failed(MANAGER_NOT_RUNNING_STEP), None)):
            with self.subTest(failed.steps[-1].id, manager=manager):
                error = runtime_failure_error(failed, "Failed.", stuck=stuck, manager=manager)
                self.assertEqual(error.code, ErrorCode.DISKD_STUCK)
                self.assertEqual(error.message, stuck.message)
                self.assertIn("diskd (pid 228) waiting on netio for 127+ s", error.detail)
        error = runtime_failure_error(self.failed(), "Failed.", stuck=stuck, manager=manager_read(pending))
        self.assertIn(pending.detail, error.detail)

    def test_a_disk_stall_and_a_quit_acpd_come_before_a_stuck_diskd(self) -> None:
        stuck = StuckDiskd(228, 127, True)
        stalls = tuple(disk_io_stalls("  421 379 39 D 127 biolock 0:00.00 mount_hfs\n"))
        error = runtime_failure_error(self.failed(), "Failed.", disk_stalls=stalls, stuck=stuck)
        self.assertIsInstance(error, DiskNotRespondingError)
        from timecapsulesmb.device.probe import ACPD_QUIT_STEP
        error = runtime_failure_error(self.failed(ACPD_QUIT_STEP), "Failed.", stuck=stuck)
        self.assertEqual(error.code, ErrorCode.ACPD_QUIT)

    def test_no_manager_is_its_own_error_even_while_samba_listens(self) -> None:
        error = runtime_failure_error(self.failed(MANAGER_NOT_RUNNING_STEP, samba_ready=True), "Failed.")
        self.assertIn("manager is not running", str(error))
        # Not rsync's error, and no Activate advice: NetBSD 6 has no Activate.
        self.assertNotEqual(error.code, ErrorCode.RSYNC_NOT_RUNNING)
        self.assertNotEqual(error.code, ErrorCode.RUNTIME_NOT_RESTARTED)
        self.assertNotIn("Activate", str(error))


# A pending handoff's processes: Apple's diskd asleep in netio since its TERM,
# ACPd with a wcifsfs it has not reaped, and an unrelated process.
LISTING = ("  374 1 374 S 127 netio 0:01.00 diskd\n"
           "  190 1 190 Is 2 netio 0:09.00 ACPd\n"
           "  367 190 367 ZW 0 - 0:00.00 wcifsfs\n"
           "  612 1 612 S 3 select 0:00.10 sshd\n")


class VerifyAfterWaitTests(unittest.TestCase):
    def verify(self, result: ManagedRuntimeProbeResult, start_states: list[ManagerStartupAgeProbeResult],
               *, problems: object = (), network: object = True, listing: str | None = None):
        reads = mock.Mock(side_effect=start_states)
        listing = LISTING if listing is None else listing
        self.debug: dict[str, object] = {}
        callbacks = OperationCallbacks(add_debug_fields=lambda **fields: self.debug.update(fields))
        with (
            mock.patch("timecapsulesmb.services.runtime_verification.recheck_cached_adisk"),
            mock.patch("timecapsulesmb.device.acpd.acpd_problems",
                       side_effect=problems if isinstance(problems, Exception) else None,
                       return_value=list(problems) if not isinstance(problems, Exception) else None) as read_problems,
            mock.patch("timecapsulesmb.device.acpd.acpd_answers_network", return_value=network),
        ):
            with self.assertRaises(DeviceError) as raised:
                verify_managed_runtime_ready(
                    CONNECTION, callbacks=callbacks, stage="s", timeout_seconds=1, heading="h",
                    failure_message="Failed.",
                    probe_runtime=lambda *_args, **_kwargs: result,
                    read_runtime_logs=lambda _c: {}, read_disk_errors=lambda _c: None,
                    read_start_state=reads, read_processes=lambda _c: listing,
                )
        self.read_problems = read_problems
        return raised.exception, reads

    def test_a_pending_handoff_records_its_processes_and_acpd(self) -> None:
        stuck = DiskdHandoff("stopping", 5, "none")
        problem = SimpleNamespace(code="network_silent")
        error, _ = self.verify(RuntimeFailureTests.failed(),
                               [manager_read(stuck, 10_000), manager_read(stuck, 10_000)],
                               problems=[problem], network=False)
        self.assertEqual(error.code, ErrorCode.DISKD_HANDOFF_PENDING)
        # ACPd's children by parent, whatever their names; sshd left out.
        self.assertEqual(self.debug["diskd_processes"], [
            "374 1 S 127 netio diskd", "190 1 Is 2 netio ACPd", "367 190 ZW 0 - wcifsfs"])
        self.assertEqual(self.debug["acpd_problems"], ["network_silent"])
        self.assertIs(self.debug["acpd_network_answers"], False)
        self.read_problems.assert_called_once_with(CONNECTION, LISTING)

    def test_the_wedge_in_the_listing_is_the_error(self) -> None:
        stopping = DiskdHandoff("stopping", 25, "none")
        error, _ = self.verify(RuntimeFailureTests.failed(), [manager_read(stopping, 330_000)] * 2,
                               listing=WEDGED_NETBSD4)
        self.assertEqual(error.code, ErrorCode.DISKD_STUCK)
        self.assertIn("diskd (pid 228) waiting on netio for 127+ s", error.detail)
        # The pending handoff's own records still go with it.
        self.assertIn("228 190 I 127 netio diskd", self.debug["diskd_processes"])

    def test_an_unreadable_acpd_is_recorded_beside_the_processes(self) -> None:
        stuck = DiskdHandoff("stopping", 5, "none")
        self.verify(RuntimeFailureTests.failed(), [manager_read(stuck), manager_read(stuck)],
                    problems=OSError("ssh: connection reset"))
        self.assertIn("connection reset", self.debug["acpd_problems_error"])
        self.assertEqual(len(self.debug["diskd_processes"]), 3)
        self.assertNotIn("acpd_problems", self.debug)
        # The listener, F19's sign, is still read.
        self.assertIs(self.debug["acpd_network_answers"], True)

    def test_a_finished_handoff_reads_no_acpd(self) -> None:
        self.verify(RuntimeFailureTests.failed(), [manager_read(READY), manager_read(READY)])
        self.read_problems.assert_not_called()
        self.assertNotIn("diskd_processes", self.debug)

    def test_a_failed_wait_reads_the_handoff_once_more_and_reports_it(self) -> None:
        stuck = DiskdHandoff("quiesce", 5, "maintenance")
        error, reads = self.verify(RuntimeFailureTests.failed(), [manager_read(stuck), manager_read(stuck)])
        self.assertEqual(error.code, ErrorCode.DISKD_HANDOFF_PENDING)
        self.assertEqual(reads.call_count, 2)  # ACPd's check, then this one

    def test_a_missing_manager_needs_no_handoff_read(self) -> None:
        error, reads = self.verify(RuntimeFailureTests.failed(MANAGER_NOT_RUNNING_STEP), [manager_read(None)])
        self.assertIn("manager is not running", str(error))
        self.assertEqual(reads.call_count, 1)


class HeldTests(unittest.TestCase):
    """The title's held=: how long the stock diskd's TERM waited for it to idle
    (README F19), sent with every start's acpd_after_start, failed or not."""

    def read(self, title: str) -> ManagerStartupAgeProbeResult:
        from timecapsulesmb.device.probe import probe_manager_startup_age_conn
        output = f"ps=  142 1 142 S 0 - 0:00.10 service service: {title}\nnow_ms=60000\n"
        proc = subprocess.CompletedProcess(["ssh"], 0, stdout=output, stderr="")
        with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=proc):
            return probe_manager_startup_age_conn(CONNECTION)

    def test_reads_held_beside_the_handoff_word(self) -> None:
        self.assertEqual(self.read("role=manager started=5 diskd=ready:17:none held=4").handoff_held_s, 4)
        # Never held: the manager leaves the word out.
        self.assertEqual(self.read("role=manager started=5 diskd=ready:17:none").handoff_held_s, 0)
        # A manager before the handoff word says nothing about it.
        self.assertIsNone(self.read("role=manager started=5").handoff_held_s)

    def test_reads_the_term_and_acpd_reads_lines_from_the_log(self) -> None:
        from timecapsulesmb.device.probe import probe_manager_startup_age_conn
        output = ("ps=  142 1 142 S 0 - 0:00.10 service service: role=manager started=5 diskd=ready:17:none\n"
                  "now_ms=60000\n"
                  "term=TERM diskd=374:3:kqread:4:120 acpd=190:3:netio:0:9000 acpd_children=none acp=0 "
                  "disk_work_ago=3000 netbios_ago=-1 uptime=42\n"
                  "term=acpd reads ms=4,3,3,5,4,3,3,4\n")
        proc = subprocess.CompletedProcess(["ssh"], 0, stdout=output, stderr="")
        with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=proc):
            result = probe_manager_startup_age_conn(CONNECTION)
        self.assertEqual(result.handoff_log, (
            "TERM diskd=374:3:kqread:4:120 acpd=190:3:netio:0:9000 acpd_children=none acp=0 "
            "disk_work_ago=3000 netbios_ago=-1 uptime=42",
            "acpd reads ms=4,3,3,5,4,3,3,4"))
        self.assertEqual(self.read("role=manager started=5 diskd=ready:17:none").handoff_log, ())

    def test_the_log_sed_picks_out_only_the_term_and_reads_lines(self) -> None:
        # The command the device runs, on a runtime.log as the manager and
        # its jobs write it; this computer's sed stands in for the device's.
        import tempfile
        from timecapsulesmb.device.probe import HANDOFF_LOG_SED
        log = ("2026-11-19 00:20:01 manager: diskd: quiesce reason=busy owner=374 generation=1\n"
               "2026-11-19 00:20:04 manager: diskd: TERM diskd=374:3:kqread:4:120 acp=0 uptime=42\n"
               "2026-11-19 00:20:04 manager: diskd: stopping reason=none owner=374 generation=1\n"
               "2026-11-19 00:20:34 manager: diskd: stopping for 30 s: diskd=374:3:netio:30:120\n"
               "2026-11-19 00:20:05 manager: diskd: acpd reads start=+310 ms=4,3 timeout=3\n"
               "2026-11-19 00:20:05 manager: diskd: acpd reads not run: no password (status 1)\n"
               "manager: diskd: TERM\n")
        with tempfile.NamedTemporaryFile("w", suffix=".log") as file:
            file.write(log)
            file.flush()
            out = subprocess.run(["sh", "-c", f"{HANDOFF_LOG_SED} {file.name}"], capture_output=True,
                                 text=True, check=True).stdout
        self.assertEqual(out.splitlines(), [
            "term=TERM diskd=374:3:kqread:4:120 acp=0 uptime=42",
            "term=acpd reads start=+310 ms=4,3 timeout=3",
            "term=acpd reads not run: no password (status 1)",
        ])

    def test_every_start_sends_this_managers_term_and_reads(self) -> None:
        from timecapsulesmb.services.runtime_verification import acpd_after_start_measurement
        # This manager started at 50 s; runtime.log still has an earlier one's TERM.
        log = ("TERM diskd=1:3:kqread:4:0 acp=0 uptime=30", "acpd reads ms=9 timeout=2",
               "TERM diskd=2:3:kqread:5:0 acp=0 uptime=55", "acpd reads ms=4,3")
        state = ManagerStartupAgeProbeResult(1.0, "m", 50, 60000, acpd_running=True, handoff=READY,
                                             handoff_held_s=0, handoff_log=log)
        fields = acpd_after_start_measurement(CONNECTION, "s", lambda _c: state, quit_confirmed=False)
        self.assertEqual((fields["diskd_term"], fields["acpd_reads"]), (log[2], log[3]))
        # A manager that TERMed nothing sends neither, whatever an earlier one did.
        state = replace(state, handoff_log=log[:2])
        fields = acpd_after_start_measurement(CONNECTION, "s", lambda _c: state, quit_confirmed=False)
        self.assertNotIn("diskd_term", fields)
        self.assertNotIn("acpd_reads", fields)


class HandoffLogFieldsTests(unittest.TestCase):
    """device.diskd.handoff_log_fields: the running manager's TERM line and
    the acpd reads after it, from a runtime.log that outlives managers."""

    def fields(self, *log: str, started: int | None = 50) -> dict[str, str]:
        from timecapsulesmb.device.diskd import handoff_log_fields
        return handoff_log_fields(log, started)

    def test_takes_the_last_term_since_this_manager_started_and_its_reads(self) -> None:
        self.assertEqual(self.fields("TERM a uptime=50", "acpd reads ms=1", "TERM b uptime=70", "acpd reads ms=2"),
                         {"diskd_term": "TERM b uptime=70", "acpd_reads": "acpd reads ms=2"})

    def test_an_earlier_managers_lines_count_for_nothing(self) -> None:
        self.assertEqual(self.fields("TERM a uptime=49", "acpd reads ms=1"), {})
        # Nor do its reads stand in for a TERM of ours that has none yet.
        self.assertEqual(self.fields("TERM a uptime=60", "acpd reads ms=1", "TERM b uptime=70"),
                         {"diskd_term": "TERM b uptime=70"})

    def test_reads_before_any_term_and_lines_without_uptime_count_for_nothing(self) -> None:
        self.assertEqual(self.fields("acpd reads ms=1"), {})
        self.assertEqual(self.fields("TERM a", "acpd reads ms=1"), {})
        self.assertEqual(self.fields("TERM a uptime=x", "acpd reads ms=1"), {})
        self.assertEqual(self.fields("TERM a uptime=60", "acpd reads ms=1", started=None), {})

    def test_cuts_long_lines(self) -> None:
        from timecapsulesmb.device.diskd import HANDOFF_LOG_CHARS
        term = "TERM " + "x" * 500 + " uptime=60"
        fields = self.fields(term, "acpd reads " + "1," * 300)
        self.assertEqual(len(fields["diskd_term"]), HANDOFF_LOG_CHARS)
        self.assertEqual(len(fields["acpd_reads"]), HANDOFF_LOG_CHARS)


    def test_every_start_records_it(self) -> None:
        from timecapsulesmb.services.runtime_verification import acpd_after_start_measurement
        state = ManagerStartupAgeProbeResult(1.0, "m", 5, 60000, acpd_running=True, acpd_pids=(190,),
                                             handoff=READY, handoff_held_s=4)
        fields = acpd_after_start_measurement(CONNECTION, "s", lambda _c: state, quit_confirmed=False)
        self.assertEqual(fields["diskd_held_sec"], 4)


class ActivationDecisionTests(unittest.TestCase):
    def decide(self, *, samba_ready: bool, present: bool | None):
        runtime = SimpleNamespace(ready=False, samba_ready=samba_ready, detail="d", steps=(), manager_present=present)
        with mock.patch("timecapsulesmb.services.activation.probe_managed_runtime_conn", return_value=runtime):
            return decide_manual_activation(CONNECTION)

    def test_listeners_without_a_manager_start_the_runtime(self) -> None:
        decision = self.decide(samba_ready=True, present=False)
        self.assertEqual((decision.run_actions, decision.reason), (True, "runtime_not_ready"))

    def test_a_serving_manager_waiting_on_rsync_is_only_verified(self) -> None:
        for present in (True, None):
            with self.subTest(present=present):
                decision = self.decide(samba_ready=True, present=present)
                self.assertEqual((decision.run_actions, decision.reason), (False, "samba_already_ready"))

    def test_nothing_listening_starts_the_runtime(self) -> None:
        # A manager still taking over the disks just keeps its lock: the
        # second one rc.local starts exits at once.
        decision = self.decide(samba_ready=False, present=True)
        self.assertEqual((decision.run_actions, decision.reason), (True, "runtime_not_ready"))


class DoctorHandoffTests(unittest.TestCase):
    TARGET = DoctorTarget(CONNECTION, "10.0.0.2", "pw")
    REMOTE = RemoteAccess(ssh_checked=True, ssh_ok=True, remote_checks_enabled=True, active_smb_conf_reason="")

    def check(self, before: DiskdHandoff | None, after: DiskdHandoff | None = None, *, now_ms: int = 10**6,
              earlier: tuple[CheckResult, ...] = ()) -> tuple[list[CheckResult], mock.MagicMock, dict]:
        sink = DoctorSink(on_result=None, debug_fields={})
        sink.results.extend(earlier)
        manager = ManagerStartupAgeProbeResult(1.0, "m", now_monotonic_ms=now_ms, handoff=before)
        fresh = ManagerStartupAgeProbeResult(1.0, "m", now_monotonic_ms=now_ms, handoff=after)
        with mock.patch("timecapsulesmb.checks.doctor_steps.probe_manager_startup_age_conn", return_value=fresh) as read:
            _doctor_check_diskd_handoff(self.TARGET, self.REMOTE, manager, sink)
        return sink.results[len(earlier):], read, sink.debug_fields

    def test_a_handoff_still_pending_after_the_startup_wait_fails(self) -> None:
        results, _, debug = self.check(STARTING, STARTING)
        self.assertEqual(len(results), 1)
        self.assertEqual((results[0].status, results[0].details["code"]), ("FAIL", ErrorCode.DISKD_HANDOFF_PENDING))
        self.assertEqual(results[0].message, PENDING_MESSAGE)
        self.assertEqual(debug["diskd_handoff"], STARTING.detail)

    def test_a_pending_handoff_records_this_managers_term_and_reads(self) -> None:
        sink = DoctorSink(on_result=None, debug_fields={})
        manager = ManagerStartupAgeProbeResult(1.0, "m", now_monotonic_ms=10**6, handoff=STARTING)
        fresh = ManagerStartupAgeProbeResult(1.0, "m", 50, 10**6, handoff=STARTING, handoff_log=(
            "TERM old uptime=20", "acpd reads ms=1", "TERM new uptime=60", "acpd reads ms= timeout=1"))
        with mock.patch("timecapsulesmb.checks.doctor_steps.probe_manager_startup_age_conn", return_value=fresh):
            _doctor_check_diskd_handoff(self.TARGET, self.REMOTE, manager, sink)
        self.assertEqual(sink.debug_fields["diskd_term"], "TERM new uptime=60")
        self.assertEqual(sink.debug_fields["acpd_reads"], "acpd reads ms= timeout=1")

    def test_a_ready_handoff_reads_nothing_more(self) -> None:
        results, read, debug = self.check(READY)
        self.assertEqual(results, [])
        read.assert_not_called()
        self.assertNotIn("diskd_term", debug)

    def test_a_handoff_that_finished_during_the_wait_reports_nothing(self) -> None:
        results, _, _ = self.check(STARTING, READY)
        self.assertEqual(results, [])

    def test_a_word_gone_unreadable_on_the_second_read_reports_nothing(self) -> None:
        results, _, debug = self.check(STARTING, None)
        self.assertEqual(results, [])
        self.assertEqual(debug["diskd_handoff"], "no readable diskd= word")

    def test_a_stuck_reason_held_ten_minutes_asks_for_a_restart(self) -> None:
        stuck = DiskdHandoff("inspect", 0, "residual_mounts")
        results, _, _ = self.check(stuck, stuck, now_ms=RESTART_AFTER_SECONDS * 1000)
        self.assertEqual(results[0].details["code"], ErrorCode.DISKD_RESTART_NEEDED)

    def test_a_disk_that_stopped_responding_is_the_explanation(self) -> None:
        stall = CheckResult("FAIL", "A disk has stopped responding.", {"code": ErrorCode.DISK_NOT_RESPONDING})
        results, _, _ = self.check(STARTING, STARTING, earlier=(stall,))
        self.assertEqual(results, [])

    def test_a_broken_acpd_is_the_explanation(self) -> None:
        # Unplugging, ACPd's fix, restarts diskd too; a handoff stuck on a
        # TERMed diskd silences ACPd's network listener as well (README F19).
        stuck = DiskdHandoff("stopping", 0, "none")
        for problem in ("network_silent", "syut_silent", "stuck_child"):
            broken = CheckResult("FAIL", "Apple ACPd is broken", {"code": "apple_acpd_broken", "acpd_problem": problem})
            for now_ms in (10**6, RESTART_AFTER_SECONDS * 1000):
                with self.subTest(problem=problem, now_ms=now_ms):
                    results, _, debug = self.check(stuck, stuck, now_ms=now_ms, earlier=(broken,))
                    self.assertEqual(results, [])
                    self.assertEqual(debug["diskd_handoff"], stuck.detail)

    def test_other_failures_leave_the_handoff_to_report(self) -> None:
        # A healthy ACPd adds no result; an unrelated FAIL explains nothing.
        other = CheckResult("FAIL", "managed smbd parent process is not running", {"code": "smbd_not_running"})
        results, _, _ = self.check(STARTING, STARTING, earlier=(other,))
        self.assertEqual([r.details["code"] for r in results], [ErrorCode.DISKD_HANDOFF_PENDING])

    def test_a_stuck_diskd_the_acpd_check_named_is_the_explanation(self) -> None:
        # The ACPd check reports diskd_stuck from the same listing first: the
        # handoff's pending verdict, which would say to wait, is left out.
        named = CheckResult("FAIL", "Apple's disk service has waited", {"code": ErrorCode.DISKD_STUCK})
        stuck = DiskdHandoff("stopping", 25, "none")
        results, _, _ = self.check(stuck, stuck, earlier=(named,))
        self.assertEqual(results, [])

    def test_ready_and_older_managers_need_no_second_read(self) -> None:
        for handoff in (READY, None):
            with self.subTest(handoff):
                results, read, _ = self.check(handoff)
                self.assertEqual(results, [])
                read.assert_not_called()


if __name__ == "__main__":
    unittest.main()
