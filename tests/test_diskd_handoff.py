"""The host's side of the disk-owner handoff (build/native/README.md, "Disk
owner handoff"): reading the manager's diskd= word, the runtime wait (which
needs listeners and a manager behind them, not the handoff), the errors at
the end of a wait that failed, Activate's decisions, and Checkup's report."""
from __future__ import annotations

import subprocess
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
    DiskdHandoff,
    parse_diskd_handoff,
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
from timecapsulesmb.services.runtime_verification import runtime_failure_error, verify_managed_runtime_ready
from timecapsulesmb.transport.ssh import SshConnection

CONNECTION = SshConnection("root@10.0.0.2", "pw", "")
READY = DiskdHandoff("ready", 40, "none")
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

    def test_a_stock_diskd_that_never_idles_is_named_as_the_cause(self) -> None:
        # The manager holds the TERM until Apple's diskd idles in its event
        # loop (README F19); one that never does needs the restart.
        _code, message = DiskdHandoff("quiesce", 0, "busy").verdict(RESTART_AFTER_SECONDS * 1000)
        self.assertTrue(message.startswith("Apple's own disk service never became idle"))

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

    def test_no_manager_is_its_own_error_even_while_samba_listens(self) -> None:
        error = runtime_failure_error(self.failed(MANAGER_NOT_RUNNING_STEP, samba_ready=True), "Failed.")
        self.assertIn("manager is not running", str(error))
        # Not rsync's error, and no Activate advice: NetBSD 6 has no Activate.
        self.assertNotEqual(error.code, ErrorCode.RSYNC_NOT_RUNNING)
        self.assertNotEqual(error.code, ErrorCode.RUNTIME_NOT_RESTARTED)
        self.assertNotIn("Activate", str(error))


class VerifyAfterWaitTests(unittest.TestCase):
    def verify(self, result: ManagedRuntimeProbeResult, start_states: list[ManagerStartupAgeProbeResult]):
        reads = mock.Mock(side_effect=start_states)
        with mock.patch("timecapsulesmb.services.runtime_verification.recheck_cached_adisk"):
            with self.assertRaises(DeviceError) as raised:
                verify_managed_runtime_ready(
                    CONNECTION, stage="s", timeout_seconds=1, heading="h", failure_message="Failed.",
                    probe_runtime=lambda *_args, **_kwargs: result,
                    read_runtime_logs=lambda _c: {}, read_disk_errors=lambda _c: None,
                    read_start_state=reads, read_processes=lambda _c: "",
                )
        return raised.exception, reads

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

    def test_ready_and_older_managers_need_no_second_read(self) -> None:
        for handoff in (READY, None):
            with self.subTest(handoff):
                results, read, _ = self.check(handoff)
                self.assertEqual(results, [])
                read.assert_not_called()


if __name__ == "__main__":
    unittest.main()
