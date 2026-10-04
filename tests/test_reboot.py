from __future__ import annotations

import random
import unittest
from unittest import mock

from timecapsulesmb.core.keep_awake import keep_system_awake
from timecapsulesmb.integrations.acp import ACPAuthError, ACPConnectionError
from timecapsulesmb.services import reboot as reboot_service
from timecapsulesmb.services.reboot import RebootFlowError, reboot_device
from tests.reboot_support import FakeAcpDevice, RecordingCallbacks


def run(device: FakeAcpDevice, **kwargs):
    recorder = RecordingCallbacks()
    kwargs.setdefault("wait", True)
    with device.patched():
        try:
            reboot_device("root@10.0.0.2", "pw", callbacks=recorder.callbacks(), **kwargs)
        except RebootFlowError as exc:
            return exc, recorder
    return None, recorder


class RebootDeviceTests(unittest.TestCase):
    def test_normal_reboot_goes_down_comes_back_and_opens_ssh(self) -> None:
        device = FakeAcpDevice()
        error, recorder = run(device)

        self.assertIsNone(error)
        self.assertEqual(recorder.stages, ["reboot", "wait_for_reboot_down", "wait_for_reboot_up"])
        self.assertEqual(recorder.fields["reboot_was_attempted"], True)
        self.assertEqual(recorder.fields["device_came_back_after_reboot"], True)
        cycle = recorder.measurement("reboot_cycle")
        self.assertEqual(cycle["result"], "success")
        self.assertEqual(cycle["u0_sec"], 3600)
        self.assertEqual(cycle["start_timeout_sec"], 90)
        self.assertEqual(cycle["up_timeout_sec"], 240)
        self.assertTrue(cycle["expect_ssh"])
        # The first read after the new kernel's ACP came up, 45 s after the request.
        self.assertLess(cycle["uptime_at_return_sec"], 15)
        self.assertGreaterEqual(cycle["ssh_ready_after_sec"], 50)
        self.assertEqual(recorder.measurement("reboot_request")["strategy"], "network_acp")
        self.assertEqual(recorder.measurement("reboot_request")["result"], "success")

    def test_baseline_read_then_one_second_then_one_request(self) -> None:
        device = FakeAcpDevice()
        run(device)

        self.assertEqual(device.calls[:3], ["read", "sleep 1", "request"])
        self.assertEqual(device.calls.count("request"), 1)

    def test_shared_ssh_connection_closes_before_the_request(self) -> None:
        for wait in (True, False):
            with self.subTest(wait=wait):
                device = FakeAcpDevice()
                run(device, wait=wait)
                self.assertEqual(device.ssh_master_closes, [("10.0.0.2", False)])

    def test_unreadable_baseline_keeps_the_shared_ssh_connection(self) -> None:
        device = FakeAcpDevice(first_read_error=ACPConnectionError("refused"))
        error, _recorder = run(device)
        self.assertIsNotNone(error)
        self.assertEqual(device.ssh_master_closes, [])

    def test_fast_reboot_between_two_reads_still_succeeds(self) -> None:
        # ACP is down for less than one poll interval, so no read sees it down.
        device = FakeAcpDevice(shutdown_after=2.0, kernel_after=2.5, acp_up_after_boot=0.5, ssh_up_after_boot=1.0)
        error, recorder = run(device)

        self.assertIsNone(error)
        self.assertNotIn("down_seen_after_sec", recorder.measurement("reboot_cycle"))
        # The timeline still shows the device coming back.
        self.assertEqual(recorder.stages[-1], "wait_for_reboot_up")

    def test_lost_request_fails_as_not_started_at_the_start_timeout(self) -> None:
        device = FakeAcpDevice(reboots=False)
        started = device.now
        error, recorder = run(device, no_down_message="did not restart")

        self.assertIsInstance(error, RebootFlowError)
        self.assertEqual((error.code, str(error)), ("reboot_not_started", "did not restart"))
        waited = device.now - started
        self.assertGreaterEqual(waited, 90)
        self.assertLess(waited, 90 + reboot_service.REBOOT_POLL_SECONDS + 1)
        self.assertEqual(recorder.measurement("reboot_cycle")["result"], "did_not_go_down")
        self.assertNotIn("device_came_back_after_reboot", recorder.fields)

    def test_brief_acp_failure_without_a_reboot_is_not_a_reboot(self) -> None:
        # The old SSH down/up wait took exactly this for a finished reboot.
        device = FakeAcpDevice(reboots=False)
        device.flaky_reads_at = ((device.now + 5, device.now + 20),)
        error, _recorder = run(device)

        self.assertEqual(error.code, "reboot_not_started")

    def test_device_alternating_between_answering_and_not_ends_as_not_started(self) -> None:
        device = FakeAcpDevice(reboots=False)
        device.flaky_reads_at = tuple((device.now + start, device.now + start + 6) for start in range(5, 200, 12))
        error, recorder = run(device)

        self.assertEqual(error.code, "reboot_not_started")
        self.assertIn("last_read_error", recorder.measurement("reboot_cycle"))

    def test_up_limit_restarts_when_an_early_failure_was_transient(self) -> None:
        # A blip 5-12 s in must not start the up clock: the device really goes
        # down at 60 s and opens SSH at 260 s, inside 240 s of that.
        device = FakeAcpDevice(shutdown_after=60.0, kernel_after=250.0)
        device.flaky_reads_at = ((device.now + 5, device.now + 12),)
        error, recorder = run(device)

        self.assertIsNone(error)
        self.assertGreaterEqual(recorder.measurement("reboot_cycle")["down_seen_after_sec"], 5)

    def test_device_that_never_comes_back_fails_up_timeout_after_going_down(self) -> None:
        device = FakeAcpDevice(kernel_after=10_000)
        started = device.now
        error, recorder = run(device, up_timeout_message="Timed out waiting for SSH after reboot.")

        self.assertEqual(error.code, "reboot_not_finished")
        self.assertEqual(str(error), "Timed out waiting for SSH after reboot.")
        cycle = recorder.measurement("reboot_cycle")
        self.assertEqual(cycle["result"], "did_not_come_back_up")
        # The up limit counts from the first unanswered read, not the request.
        self.assertGreaterEqual(device.now - started, cycle["down_seen_after_sec"] + 240)
        self.assertIn("timed out", cycle["last_read_error"])
        self.assertEqual(recorder.stages, ["reboot", "wait_for_reboot_down", "wait_for_reboot_up"])

    def test_return_just_inside_and_just_outside_the_up_limit(self) -> None:
        # Down is seen at the 10 s read (ACP stops at 9 s). SSH opens at kernel + 10.
        for kernel_after, ok in ((215.0, True), (245.0, False)):
            with self.subTest(kernel_after=kernel_after):
                device = FakeAcpDevice(shutdown_after=9.0, kernel_after=kernel_after)
                error, _recorder = run(device)
                self.assertEqual(error is None, ok)

    def test_restart_with_ssh_that_never_opens(self) -> None:
        device = FakeAcpDevice(ssh_up_after_boot=None)
        error, recorder = run(device, up_timeout_message="SSH did not open after enabling via ACP.")

        self.assertEqual(error.code, "reboot_not_finished")
        self.assertEqual(str(error), "SSH did not open after enabling via ACP.")
        cycle = recorder.measurement("reboot_cycle")
        self.assertEqual(cycle["result"], "ssh_not_open")
        self.assertIn("reset_seen_after_sec", cycle)

    def test_slow_ssh_after_enabling_it_is_waited_for(self) -> None:
        # The first boot with SSH on can take minutes before sshd listens.
        for ssh_up_after_boot, ok in ((150.0, True), (260.0, False)):
            with self.subTest(ssh_up_after_boot=ssh_up_after_boot):
                device = FakeAcpDevice(ssh_open=False, ssh_up_after_boot=ssh_up_after_boot)
                error, _recorder = run(device)
                self.assertEqual(error is None, ok)

    def test_ssh_kept_closed_passes_at_sixty_seconds_uptime(self) -> None:
        device = FakeAcpDevice(ssh_up_after_boot=None)
        error, recorder = run(device, expect_ssh=False)

        self.assertIsNone(error)
        # Two checks a poll apart, the first once sshd would have started.
        self.assertEqual(device.calls.count("tcp 22"), 2)
        self.assertGreaterEqual(device.device_uptime(), 65)
        self.assertLess(device.device_uptime(), 66)
        self.assertFalse(recorder.measurement("reboot_cycle")["expect_ssh"])

    def test_one_missed_check_does_not_hide_ssh_that_stayed_open(self) -> None:
        # A lost connection attempt reads as a closed port; the second check
        # must still find SSH open.
        device = FakeAcpDevice(ssh_up_after_boot=10.0)
        real_tcp_open = device.tcp_open
        checks: list[bool] = []

        def first_attempt_lost(host, port, timeout=2.0):
            is_open = real_tcp_open(host, port, timeout)
            checks.append(is_open)
            return is_open and len(checks) > 1

        device.tcp_open = first_attempt_lost
        error, recorder = run(device, expect_ssh=False)

        self.assertEqual(checks, [True, True])
        self.assertEqual(error.code, "ssh_still_enabled")
        self.assertEqual(recorder.measurement("reboot_cycle")["result"], "ssh_still_open")

    def test_ssh_that_opens_after_disabling_it_fails(self) -> None:
        device = FakeAcpDevice(ssh_up_after_boot=10.0)
        error, recorder = run(device, expect_ssh=False)

        self.assertEqual(error.code, "ssh_still_enabled")
        self.assertEqual(str(error), "SSH reopened after reboot. Disable did not persist.")
        self.assertEqual(recorder.measurement("reboot_cycle")["result"], "ssh_still_open")
        self.assertNotIn("device_came_back_after_reboot", recorder.fields)

    def test_unreadable_baseline_uptime_never_sends_the_request(self) -> None:
        for error_value, code in (
            (ACPConnectionError("Could not connect to ACP on 10.0.0.2:5009: refused"), "device_unreachable"),
            (ACPAuthError("ACP command failed (likely wrong AirPort admin password)"), "auth_failed"),
        ):
            with self.subTest(code=code):
                device = FakeAcpDevice(first_read_error=error_value)
                error, recorder = run(device)
                self.assertEqual(error.code, code)
                self.assertIn("before rebooting", str(error))
                self.assertNotIn("request", device.calls)
                self.assertEqual(recorder.stages, [])

    def test_request_error_after_the_device_acted_is_observed(self) -> None:
        device = FakeAcpDevice(request_error=ACPConnectionError("ACP connection closed while reading 128 bytes"))
        error, recorder = run(device)

        self.assertIsNone(error)
        self.assertEqual(recorder.debug["acp_reboot_succeeded"], False)
        self.assertEqual(recorder.measurement("reboot_request")["error_type"], "ACPConnectionError")
        self.assertIn("checking whether the device is restarting anyway", "\n".join(recorder.messages))

    def test_no_wait_sends_one_request_and_never_reads(self) -> None:
        device = FakeAcpDevice()
        error, recorder = run(device, wait=False)

        self.assertIsNone(error)
        self.assertEqual(device.calls, ["request"])
        self.assertEqual(recorder.stages, ["reboot"])
        self.assertEqual([kind for kind, _ in recorder.measurements], ["reboot_request"])

    def test_no_wait_raises_request_errors(self) -> None:
        for error_value, code in (
            (ACPConnectionError("refused"), "remote_error"),
            (ACPAuthError("wrong password"), "auth_failed"),
        ):
            with self.subTest(code=code):
                device = FakeAcpDevice(request_error=error_value)
                error, _recorder = run(device, wait=False)
                self.assertEqual(error.code, code)
                self.assertEqual(device.calls, ["request"])

    def test_uptime_margin_boundaries(self) -> None:
        # u0 = 100. After 1 s of sleep and the request, a reading below
        # u0 + elapsed - 2 is a new boot; anything at or above it is the old one.
        cases = (
            (100 + 6 - 2, False),  # exactly at the margin: same boot
            (100 + 6 - 3, True),  # one second inside it: new boot
        )
        for reading, rebooted in cases:
            with self.subTest(reading=reading):
                device = FakeAcpDevice(uptime=100, reboots=False)
                readings = iter([100, reading])

                def scripted(host, password, name, *, timeout=25.0):
                    device.calls.append("read")
                    return next(readings, 10_000)

                device.get_property_int = scripted
                error, _recorder = run(device, expect_ssh=True)
                self.assertEqual(error is None, rebooted)

    def test_slow_reads_do_not_turn_the_old_boot_into_a_new_one(self) -> None:
        device = FakeAcpDevice(reboots=False, read_latency=4.9)
        error, _recorder = run(device)

        self.assertEqual(error.code, "reboot_not_started")

    def test_device_clock_slightly_slower_than_the_host_is_not_a_reboot(self) -> None:
        device = FakeAcpDevice(reboots=False, device_rate=0.995)
        error, _recorder = run(device)

        self.assertEqual(error.code, "reboot_not_started")

    def test_reboot_shortly_after_a_previous_boot_is_still_detected(self) -> None:
        device = FakeAcpDevice(uptime=30)
        error, _recorder = run(device)

        self.assertIsNone(error)

    def test_host_sleep_through_the_reboot_never_reports_a_false_success(self) -> None:
        # The Mac's clock stops while the device reboots and runs for ten more
        # minutes. With a large u0 the reboot is still seen; with a small one
        # the result can only be a not-started failure, never a success claim.
        for uptime, expected in ((7200, None), (30, "reboot_not_started")):
            with self.subTest(uptime=uptime):
                device = FakeAcpDevice(uptime=uptime)
                original_reboot = device.reboot

                def reboot_then_sleep(host, password, *, timeout=25.0, **kwargs):
                    original_reboot(host, password, timeout=timeout)
                    device.pause_host_clock(600)

                device.reboot = reboot_then_sleep
                error, _recorder = run(device)
                self.assertEqual(None if error is None else error.code, expected)


class RebootFlowErrorTests(unittest.TestCase):
    def test_error_leaves_a_contextmanager_block_unchanged(self) -> None:
        # Every long CLI command runs inside keep_system_awake(), whose
        # contextmanager sets __traceback__ on the error passing through it.
        device = FakeAcpDevice(reboots=False)
        with mock.patch("timecapsulesmb.core.keep_awake.sys.platform", "linux"):
            with self.assertRaises(RebootFlowError) as raised:
                with keep_system_awake(), device.patched():
                    reboot_device("root@10.0.0.2", "pw", wait=True)

        self.assertEqual(raised.exception.code, "reboot_not_started")
        self.assertEqual(str(raised.exception), "Reboot was requested but the device did not restart.")
        # add_note() needs Python 3.11; __notes__ is the attribute it fills.
        raised.exception.__notes__ = ["notes and tracebacks can be attached"]


class RandomTimelineTests(unittest.TestCase):
    def test_random_timelines_never_claim_a_reboot_that_did_not_happen(self) -> None:
        rng = random.Random(20261003)
        for case in range(500):
            reboots = rng.random() < 0.7
            device = FakeAcpDevice(
                uptime=rng.choice([20, 60, 600, 86_400]),
                reboots=reboots,
                shutdown_after=rng.uniform(0.5, 30),
                kernel_after=rng.uniform(31, 400),
                acp_up_after_boot=rng.uniform(1, 8),
                ssh_up_after_boot=rng.choice([None, rng.uniform(5, 300)]),
                read_latency=rng.choice([0.0, 0.5, 4.9]),
                device_rate=rng.uniform(0.995, 1.005),
            )
            start = device.now
            device.flaky_reads_at = tuple(
                (start + at, start + at + rng.uniform(1, 15)) for at in sorted(rng.uniform(0, 300) for _ in range(rng.randint(0, 3)))
            )
            expect_ssh = rng.random() < 0.8
            with self.subTest(case=case):
                error, _recorder = run(device, expect_ssh=expect_ssh)
                if error is not None and error.code == "device_unreachable":
                    # A flaky first read stops everything before the request.
                    self.assertNotIn("request", device.calls)
                    continue
                if not reboots:
                    self.assertIsNotNone(error)
                    self.assertEqual(error.code, "reboot_not_started")
                    continue
                if error is None:
                    # A success must be a real new boot, with SSH as expected.
                    self.assertTrue(device.served_new_boot)
                    self.assertEqual(device.tcp_open("10.0.0.2", 22), expect_ssh)
                else:
                    # A rebooting device is never reported as not started when
                    # it came back inside the start limit with time to spare.
                    if device.kernel_after + device.acp_up_after_boot + 15 < 90:
                        self.assertNotEqual(error.code, "reboot_not_started")


if __name__ == "__main__":
    unittest.main()
