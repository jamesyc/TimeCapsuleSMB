from __future__ import annotations

import random
import unittest
from unittest import mock

from timecapsulesmb.core.keep_awake import keep_system_awake
from timecapsulesmb.device import acpd
from timecapsulesmb.integrations.acp import ACPAuthError, ACPConnectionError
from timecapsulesmb.services import reboot as reboot_service
from timecapsulesmb.services.reboot import (
    RESTART_STUCK_HINT,
    RebootFlowError,
    reboot_device,
    restart_stuck_message,
    restart_stuck_offline_hint,
    unplug_if_on_network_note,
)
from timecapsulesmb.transport.errors import SshError
from timecapsulesmb.transport.ssh import SshConnection
from tests.reboot_support import NETWORK_CONTEXT, FakeAcpDevice, RecordingCallbacks

UP_LIMIT = reboot_service.REBOOT_UP_TIMEOUT_SECONDS
SSH_LIMIT = reboot_service.REBOOT_SSH_TIMEOUT_SECONDS


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
    def test_polls_acp_and_ssh_on_the_target_host_as_saved(self) -> None:
        for target, host in (("root@10.0.0.2", "10.0.0.2"), ("root@fe80::1%en0", "fe80::1%en0")):
            with self.subTest(target=target):
                device = FakeAcpDevice()
                seen: list[str] = []
                read_property, open_port = device.get_property_int, device.tcp_open

                def recording_read(polled: str, *args, **kwargs):
                    seen.append(polled)
                    return read_property(polled, *args, **kwargs)

                def recording_open(polled: str, *args, **kwargs):
                    seen.append(polled)
                    return open_port(polled, *args, **kwargs)

                device.get_property_int = recording_read
                device.tcp_open = recording_open
                with device.patched():
                    reboot_device(target, "pw", wait=True, callbacks=RecordingCallbacks().callbacks())

                self.assertTrue(seen)
                self.assertEqual(set(seen), {host})

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
        self.assertEqual(cycle["up_timeout_sec"], UP_LIMIT)
        self.assertEqual(cycle["ssh_timeout_sec"], SSH_LIMIT)
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
        device = unanswered_baseline_device()
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
        # down at 60 s and its new boot answers ACP inside the up limit of
        # that, but after the limit counted from the blip.
        device = FakeAcpDevice(shutdown_after=60.0, kernel_after=UP_LIMIT + 10)
        device.flaky_reads_at = ((device.now + 5, device.now + 12),)
        error, recorder = run(device)

        self.assertIsNone(error)
        self.assertGreaterEqual(recorder.measurement("reboot_cycle")["down_seen_after_sec"], 5)

    def test_device_that_never_comes_back_fails_up_timeout_after_going_down(self) -> None:
        device = FakeAcpDevice(kernel_after=10_000)
        started = device.now
        error, recorder = run(device, up_timeout_message="Timed out waiting for SSH after reboot.")

        self.assertEqual(error.code, "reboot_not_finished")
        # Nothing showed where this Mac is, so the unplug is only conditional.
        self.assertEqual(str(error), f"Timed out waiting for SSH after reboot. {unplug_if_on_network_note(None)}")
        cycle = recorder.measurement("reboot_cycle")
        self.assertEqual(cycle["result"], "did_not_come_back_up")
        # The up limit counts from the first unanswered read, not the request.
        self.assertGreaterEqual(device.now - started, cycle["down_seen_after_sec"] + UP_LIMIT)
        self.assertIn("timed out", cycle["last_read_error"])
        self.assertEqual(recorder.stages, ["reboot", "wait_for_reboot_down", "wait_for_reboot_up"])

    def test_return_just_inside_and_just_outside_the_up_limit(self) -> None:
        # Down is seen at the 10 s read (ACP stops at 9 s), so the last read
        # allowed to find the new boot is at 10 s + the up limit. The new
        # boot's ACP answers at kernel + 5; SSH, with its own limit, at kernel + 10.
        for kernel_after, ok in ((UP_LIMIT - 5, True), (UP_LIMIT + 10, False)):
            with self.subTest(kernel_after=kernel_after):
                device = FakeAcpDevice(shutdown_after=9.0, kernel_after=kernel_after)
                error, _recorder = run(device)
                self.assertEqual(error is None, ok)

    def test_restart_with_ssh_that_never_opens(self) -> None:
        device = FakeAcpDevice(ssh_up_after_boot=None)
        started = device.now
        error, recorder = run(device, up_timeout_message="SSH did not open after enabling via ACP.")

        self.assertEqual(error.code, "reboot_not_finished")
        self.assertEqual(str(error), "SSH did not open after enabling via ACP.")
        cycle = recorder.measurement("reboot_cycle")
        self.assertEqual(cycle["result"], "ssh_not_open")
        self.assertEqual(cycle["ssh_timeout_sec"], SSH_LIMIT)
        # The SSH limit counts from the new boot, not from going down: a
        # device back in 45 s with SSH closed fails minutes before the up limit.
        waited = device.now - started
        self.assertGreaterEqual(waited, cycle["reset_seen_after_sec"] + SSH_LIMIT)
        self.assertLess(waited, cycle["reset_seen_after_sec"] + SSH_LIMIT + reboot_service.REBOOT_POLL_SECONDS)
        self.assertLess(waited, cycle["down_seen_after_sec"] + UP_LIMIT)

    def test_slow_ssh_after_enabling_it_is_waited_for(self) -> None:
        # The first boot with SSH on can take minutes before sshd listens.
        # The new boot is seen at kernel + 5; the SSH limit counts from there.
        for ssh_up_after_boot, ok in ((SSH_LIMIT - 10, True), (SSH_LIMIT + 20, False)):
            with self.subTest(ssh_up_after_boot=ssh_up_after_boot):
                device = FakeAcpDevice(ssh_open=False, ssh_up_after_boot=ssh_up_after_boot)
                error, _recorder = run(device)
                self.assertEqual(error is None, ok)

    def test_slow_restart_still_gets_the_whole_ssh_limit(self) -> None:
        # A new boot just inside the up limit, then SSH just inside its own
        # limit: together longer than the up limit from going down.
        device = FakeAcpDevice(kernel_after=UP_LIMIT - 50, ssh_up_after_boot=SSH_LIMIT - 30)
        error, recorder = run(device)

        self.assertIsNone(error)
        cycle = recorder.measurement("reboot_cycle")
        self.assertGreater(cycle["ssh_ready_after_sec"], cycle["down_seen_after_sec"] + UP_LIMIT)

    def test_ssh_kept_closed_passes_at_sixty_seconds_uptime(self) -> None:
        device = FakeAcpDevice(ssh_up_after_boot=None)
        error, recorder = run(device, expect_ssh=False)

        self.assertIsNone(error)
        # Two checks a poll apart, the first once sshd would have started.
        self.assertEqual(device.calls.count("tcp 22"), 2)
        self.assertGreaterEqual(device.device_uptime(), 65)
        self.assertLess(device.device_uptime(), 66)
        self.assertFalse(recorder.measurement("reboot_cycle")["expect_ssh"])
        # Disabling never waits for SSH, so it has no SSH limit.
        self.assertNotIn("ssh_timeout_sec", recorder.measurement("reboot_cycle"))

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

    def test_an_unanswered_baseline_uptime_is_read_twice_then_requests_nothing(self) -> None:
        # Both reads unanswered: no restart is requested, and the error says
        # so rather than that the device cannot be reached (SSH just was).
        device = unanswered_baseline_device()
        error, recorder = run(device)
        self.assertEqual(error.code, "reboot_not_requested")
        self.assertIn("so the restart was not requested", str(error))
        self.assertNotIn("request", device.calls)
        self.assertEqual(device.calls.count("sleep 5"), 1)
        self.assertEqual(recorder.stages, [])

    def test_a_rejected_password_stops_at_the_first_read(self) -> None:
        device = FakeAcpDevice(first_read_error=ACPAuthError("ACP command failed (likely wrong AirPort admin password)"))
        error, recorder = run(device)
        self.assertEqual(error.code, "auth_failed")
        self.assertIn("before rebooting", str(error))
        self.assertNotIn("request", device.calls)
        self.assertNotIn("sleep 5", device.calls)
        self.assertEqual(recorder.stages, [])

    def test_one_missed_baseline_read_is_read_again_and_the_restart_goes_on(self) -> None:
        # A busy ACPd or a lossy link can miss one read.
        device = FakeAcpDevice(first_read_error=ACPConnectionError("Could not connect to ACP on 10.0.0.2:5009: timed out"))
        error, _recorder = run(device)
        self.assertIsNone(error)
        self.assertIn("request", device.calls)

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
                kernel_after=rng.uniform(31, UP_LIMIT + 200),
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
                if error is not None and error.code == "reboot_not_requested":
                    # Two flaky baseline reads stop everything before the request.
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


def unanswered_baseline_device() -> FakeAcpDevice:
    """A device up all along whose ACP reads go unanswered for the first minute."""
    device = FakeAcpDevice()
    device.flaky_reads_at = ((device.now, device.now + 60),)
    return device


def run_returning(device: FakeAcpDevice, **kwargs):
    """Like run, but also returns what reboot_device returned."""
    recorder = RecordingCallbacks()
    kwargs.setdefault("wait", True)
    with device.patched():
        try:
            moved = reboot_device("root@10.0.0.2", "pw", callbacks=recorder.callbacks(), **kwargs)
        except RebootFlowError as exc:
            return exc, recorder
    return moved, recorder


class RebootFollowTests(unittest.TestCase):
    """A device whose new boot gets another DHCP address is found by its MAC."""

    def test_a_device_back_at_a_new_address_is_followed_there(self) -> None:
        device = FakeAcpDevice(new_address="10.0.0.9", kernel_after=40)
        moved, recorder = run_returning(device)

        self.assertEqual(moved, "root@10.0.0.9")
        self.assertIn("locate", device.calls)
        self.assertEqual(recorder.fields["current_host"], "root@10.0.0.9")
        self.assertEqual(recorder.fields["device_came_back_after_reboot"], True)
        self.assertIn("The device came back at 10.0.0.9; the saved address is out of date.", recorder.messages)
        cycle = recorder.measurement("reboot_cycle")
        self.assertEqual(cycle["result"], "success")
        self.assertTrue(cycle["followed"])
        # Looked for only after a minute down, and SSH is checked at the new address.
        self.assertGreaterEqual(cycle["follow_after_sec"] - cycle["down_seen_after_sec"], reboot_service.FOLLOW_AFTER_DOWN_SECONDS)
        self.assertTrue(device.tcp_open("10.0.0.9", 22))

    def test_a_device_back_within_a_minute_is_never_looked_for(self) -> None:
        device = FakeAcpDevice(kernel_after=40)
        moved, recorder = run_returning(device)

        self.assertIsNone(moved)
        self.assertNotIn("locate", device.calls)
        self.assertNotIn("current_host", recorder.fields)
        self.assertNotIn("followed", recorder.measurement("reboot_cycle"))

    def test_a_slow_device_back_at_its_address_stays_there(self) -> None:
        device = FakeAcpDevice(kernel_after=180)
        moved, recorder = run_returning(device)

        self.assertIsNone(moved)
        self.assertIn("locate", device.calls)
        self.assertEqual(recorder.measurement("reboot_cycle")["result"], "success")
        self.assertNotIn("current_host", recorder.fields)

    def test_looks_repeat_every_interval_until_the_device_answers(self) -> None:
        # The new boot answers 300 s after the request; looks at 60 s down and
        # then every 30 s find nothing until then.
        device = FakeAcpDevice(new_address="10.0.0.9", kernel_after=300)
        moved, _recorder = run_returning(device)

        self.assertEqual(moved, "root@10.0.0.9")
        self.assertGreaterEqual(device.calls.count("locate"), 7)

    def test_without_the_mac_the_device_is_not_followed(self) -> None:
        device = FakeAcpDevice(new_address="10.0.0.9", kernel_after=40)
        answer = device.get_properties
        device.get_properties = lambda *args, **kwargs: {"syUT": answer(*args, **kwargs)["syUT"]}
        error, _recorder = run_returning(device)

        self.assertEqual(error.code, "reboot_not_finished")
        self.assertNotIn("locate", device.calls)

    def test_a_found_address_without_a_new_boot_is_not_followed(self) -> None:
        from timecapsulesmb.services.locate import LocateResult

        device = FakeAcpDevice(kernel_after=10_000)
        read = device.get_property_int
        # Something answers at .7 with the old boot's uptime.
        device.get_property_int = lambda host, *args, **kwargs: 999_999 if host == "10.0.0.7" else read(host, *args, **kwargs)
        device.locate_airport = lambda *_args, **_kwargs: LocateResult("found", host="root@10.0.0.7", address="10.0.0.7")
        error, recorder = run_returning(device)

        self.assertEqual(error.code, "reboot_not_finished")
        self.assertNotIn("current_host", recorder.fields)

    def test_another_airport_at_the_old_address_does_not_prove_the_reboot(self) -> None:
        # The device came back at .9; another AirPort that just booted took .2.
        device = FakeAcpDevice(new_address="10.0.0.9", kernel_after=40)
        answer = device.get_properties

        def reads(host, *args, **kwargs):
            kernel = device.requested_at + device.kernel_after if device.requested_at is not None else None
            if host == "10.0.0.2" and kernel is not None and device.now >= kernel:
                device.calls.append("read")
                return {"syUT": (5).to_bytes(4, "big"), "waMA": bytes.fromhex("020000000002")}
            return answer(host, *args, **kwargs)

        device.get_properties = reads
        moved, recorder = run_returning(device)

        self.assertEqual(moved, "root@10.0.0.9")
        self.assertTrue(recorder.measurement("reboot_cycle")["other_device_at_address"])

    def test_a_device_back_with_another_password_is_named_in_the_timeout(self) -> None:
        from timecapsulesmb.services.locate import LocateResult

        device = FakeAcpDevice(kernel_after=10_000)
        device.locate_airport = lambda *_args, **_kwargs: LocateResult("password_rejected", address="10.0.1.1")
        error, _recorder = run_returning(device)

        self.assertEqual(error.code, "reboot_not_finished")
        self.assertEqual(
            str(error),
            "Timed out waiting for SSH after reboot. An AirPort advertising this device's MAC answered at 10.0.1.1"
            " but rejected the AirPort admin password; it may have been reset.",
        )

    def test_ssh_kept_closed_is_checked_at_the_new_address(self) -> None:
        device = FakeAcpDevice(new_address="10.0.0.9", kernel_after=40, ssh_up_after_boot=None)
        moved, recorder = run_returning(device, expect_ssh=False)

        self.assertEqual(moved, "root@10.0.0.9")
        self.assertEqual(recorder.measurement("reboot_cycle")["result"], "success")

    def test_a_device_that_never_returns_records_what_failed_and_where(self) -> None:
        # Telemetry install 00094b92: ACP never answered again. The wait says how
        # each read failed, and how this computer reached the address at the end.
        device = FakeAcpDevice(kernel_after=10_000)
        error, recorder = run_returning(device)

        self.assertEqual(error.code, "reboot_not_finished")
        cycle = recorder.measurement("reboot_cycle")
        timeouts = cycle["read_errors"]["timeout"]
        self.assertEqual(set(cycle["read_errors"]), {"timeout"})
        self.assertGreater(timeouts["count"], 100)
        self.assertGreaterEqual(timeouts["first_sec"], 10)
        self.assertGreaterEqual(timeouts["last_sec"] - timeouts["first_sec"], UP_LIMIT - 5)
        self.assertEqual(cycle["local_networks"], NETWORK_CONTEXT["local_networks"])
        self.assertEqual(cycle["acp_target_addresses"], NETWORK_CONTEXT["acp_target_addresses"])
        self.assertEqual(cycle["context_host"], "10.0.0.2")

    def test_a_device_back_at_its_address_with_another_password_is_counted_as_such(self) -> None:
        device = FakeAcpDevice(kernel_after=40)
        answer = device.get_properties

        def reads(host, *args, **kwargs):
            values = answer(host, *args, **kwargs)
            if device.served_new_boot:
                raise ACPAuthError("ACP command failed with error_code -0x10 (likely wrong AirPort admin password)")
            return values

        device.get_properties = reads
        error, recorder = run_returning(device)

        self.assertEqual(error.code, "reboot_not_finished")
        read_errors = recorder.measurement("reboot_cycle")["read_errors"]
        self.assertGreater(read_errors["auth"]["count"], 0)
        self.assertLess(read_errors["timeout"]["first_sec"], read_errors["auth"]["first_sec"])

    def test_a_successful_wait_records_no_network_context(self) -> None:
        moved, recorder = run_returning(FakeAcpDevice(kernel_after=40))

        self.assertIsNone(moved)
        cycle = recorder.measurement("reboot_cycle")
        self.assertNotIn("local_networks", cycle)
        self.assertEqual(set(cycle["read_errors"]), {"timeout"})

    def test_followed_moves_only_a_moved_connection(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")

        self.assertIs(reboot_service.followed(connection, None), connection)
        self.assertEqual(reboot_service.followed(connection, "root@10.0.0.9"), SshConnection("root@10.0.0.9", "pw", "-o foo"))


if __name__ == "__main__":
    unittest.main()


class AcpdHangTests(unittest.TestCase):
    """Apple's ACPd takes the request, tears itself down and never restarts
    the device: ACP goes quiet as in a reboot, while the old kernel keeps
    running (build/native/README.md, "Apple ACPd"). The
    marker written on the RAM disk before the request outlives only a restart
    that never happened."""

    SSH = SshConnection("root@10.0.0.2", "pw", "")

    def test_a_kernel_that_never_restarted_is_reported_as_broken_acpd(self) -> None:
        for expect_ssh in (True, False):
            with self.subTest(expect_ssh=expect_ssh):
                device = FakeAcpDevice(acpd_hangs=True)
                started = device.now
                error, recorder = run(device, ssh=self.SSH, expect_ssh=expect_ssh)

                # Not "SSH still enabled": the marker shows the same kernel.
                self.assertEqual((error.code, str(error)), ("acpd_broken", reboot_service.ACPD_STOPPED_MESSAGE))
                cycle = recorder.measurement("reboot_cycle")
                self.assertEqual(cycle["result"], "acpd_stopped_without_reboot")
                # One look, once the marker is past the grace.
                self.assertEqual((cycle["restart_looks"], cycle["restart_look_last"]), (1, "hung"))
                self.assertGreaterEqual(cycle["restart_marker_age_sec"], acpd.RESTART_HANG_SECONDS)
                self.assertEqual(device.ssh_reads, [("mark", False), ("marker", True)])
                self.assertEqual(cycle["restart_marker"], "written")
                # The user was asked to unplug it once the marker was past the
                # grace, and the wait went on until its usual limit.
                self.assertLess(
                    cycle["acpd_hang_seen_after_sec"],
                    acpd.RESTART_HANG_SECONDS + reboot_service.HANG_CHECK_INTERVAL_SECONDS,
                )
                self.assertEqual(recorder.stages[-1], "wait_for_reboot_up")
                self.assertEqual([(stage, fields) for _at, stage, fields in recorder.progress],
                                 [("wait_for_reboot_up", hint_state(acpd_hung=True))])
                self.assertIn(reboot_service.UNPLUG_MESSAGE, recorder.messages)
                # The whole limit again from the request to unplug.
                self.assertGreaterEqual(
                    device.now - started, cycle["acpd_hang_seen_after_sec"] + reboot_service.REBOOT_UP_TIMEOUT_SECONDS,
                )
                self.assertEqual(device.calls.count("request"), 1)
                self.assertNotIn("device_came_back_after_reboot", recorder.fields)

    def test_the_marker_is_written_after_the_uptime_read_and_before_the_request(self) -> None:
        device = FakeAcpDevice(acpd_hangs=True)
        mark = device.mark_restart
        seen: list[tuple[int, float | None]] = []

        def recording_mark(connection) -> bool:
            seen.append((device.reads, device.requested_at))
            return mark(connection)

        device.mark_restart = recording_mark
        run(device, ssh=self.SSH)

        # After the one uptime read, before the request.
        self.assertEqual(seen, [(1, None)])

    def test_a_failed_uptime_read_marks_nothing(self) -> None:
        device = unanswered_baseline_device()
        error, _recorder = run(device, ssh=self.SSH)

        self.assertEqual(error.code, "reboot_not_requested")
        self.assertEqual(device.ssh_reads, [])
        self.assertIsNone(device.marked_at)

    def test_a_refused_request_to_an_acpd_that_stopped_is_the_hang(self) -> None:
        # ACPd went away with the request: it is refused, and ACP stays quiet.
        device = FakeAcpDevice(acpd_hangs=True, request_error=ACPConnectionError("[Errno 61] Connection refused"))
        error, recorder = run(device, ssh=self.SSH)

        self.assertEqual(error.code, "acpd_broken")
        self.assertEqual(recorder.debug["acp_reboot_succeeded"], False)
        self.assertEqual(recorder.measurement("reboot_cycle")["result"], "acpd_stopped_without_reboot")

    def test_an_old_kernel_that_stops_within_the_grace_is_never_looked_at(self) -> None:
        # A real restart closes SSH within seconds: no look reaches the device.
        device = FakeAcpDevice(old_ssh_until=acpd.RESTART_HANG_SECONDS - 5, kernel_after=200.0)
        error, recorder = run(device, ssh=self.SSH)

        self.assertIsNone(error)
        cycle = recorder.measurement("reboot_cycle")
        self.assertEqual(cycle["result"], "success")
        self.assertEqual(cycle["restart_look_last"], "ssh_closed")
        self.assertNotIn(("marker", True), device.ssh_reads)

    def test_an_old_kernel_alive_past_the_grace_gets_the_unplug_advice_and_still_succeeds(self) -> None:
        # Its teardown ran and its sshd still answers: the user is asked to
        # unplug it, and the wait goes on, so a restart that was only slow
        # ends it as any other.
        device = FakeAcpDevice(old_ssh_until=110.0, kernel_after=200.0)
        error, recorder = run(device, ssh=self.SSH)

        self.assertIsNone(error)
        cycle = recorder.measurement("reboot_cycle")
        self.assertEqual((cycle["result"], cycle["restart_look_last"]), ("success", "hung"))
        self.assertIn("acpd_hang_seen_after_sec", cycle)
        # Asked once the hang was seen, and cleared when the device came back.
        self.assertEqual([(stage, fields) for _at, stage, fields in recorder.progress],
                         [("wait_for_reboot_up", hint_state(acpd_hung=True)), ("wait_for_reboot_up", hint_state())])

    def test_a_hung_device_unplugged_during_the_wait_comes_back_as_a_restart(self) -> None:
        device = FakeAcpDevice(acpd_hangs=True, unplugged_after=180.0)
        error, recorder = run(device, ssh=self.SSH)

        self.assertIsNone(error)
        cycle = recorder.measurement("reboot_cycle")
        self.assertEqual(cycle["result"], "success")
        # One look: once the hang is seen, only the new boot is waited for.
        self.assertEqual((cycle["restart_looks"], cycle["restart_look_last"]), (1, "hung"))
        self.assertLess(cycle["acpd_hang_seen_after_sec"], cycle["reset_seen_after_sec"])
        self.assertEqual(recorder.fields.get("device_came_back_after_reboot"), True)
        self.assertEqual([fields for _at, _stage, fields in recorder.progress], [hint_state(acpd_hung=True), hint_state()])
        # The look opened a shared connection to the old kernel, which the
        # power-off left dead.
        self.assertIn(("10.0.0.2", True), device.ssh_master_closes)

    def test_an_unplug_late_in_the_wait_still_ends_it_as_a_restart(self) -> None:
        # Past the limit counted from when the device went down, but within
        # the one counted again from the request to unplug.
        device = FakeAcpDevice(acpd_hangs=True, unplugged_after=640.0)
        started = device.now
        error, recorder = run(device, ssh=self.SSH)

        self.assertIsNone(error)
        cycle = recorder.measurement("reboot_cycle")
        self.assertEqual(cycle["result"], "success")
        self.assertGreater(cycle["reset_seen_after_sec"], cycle["down_seen_after_sec"] + reboot_service.REBOOT_UP_TIMEOUT_SECONDS)
        self.assertLess(device.now - started, cycle["acpd_hang_seen_after_sec"] + reboot_service.REBOOT_UP_TIMEOUT_SECONDS)

    def test_a_request_the_device_ignored_keeps_its_marker(self) -> None:
        # The next command finds the marker past the grace beside a healthy
        # ACPd: a request never taken, which blames nothing.
        device = FakeAcpDevice(reboots=False)
        error, recorder = run(device, ssh=self.SSH)

        self.assertEqual(error.code, "reboot_not_started")
        self.assertEqual(recorder.measurement("reboot_cycle")["result"], "did_not_go_down")
        self.assertEqual(device.restart_verdict(self.SSH).state, "not_taken")

    def test_a_failed_request_without_waiting_keeps_its_marker(self) -> None:
        device = FakeAcpDevice(request_error=ACPAuthError("wrong password"))
        error, recorder = run(device, wait=False, ssh=self.SSH)

        self.assertEqual(error.code, "auth_failed")
        self.assertEqual(recorder.debug["restart_marker"], "written")
        # Commands right after it wait for a restart; once past the grace,
        # the marker beside a healthy ACPd blames nothing.
        self.assertEqual(device.restart_verdict(self.SSH).state, "restarting")
        device.now += acpd.RESTART_HANG_SECONDS
        self.assertEqual(device.restart_verdict(self.SSH).state, "not_taken")

    def test_the_caller_names_what_the_unplug_finishes(self) -> None:
        device = FakeAcpDevice(acpd_hangs=True)
        error, _recorder = run(device, ssh=self.SSH, stopped_message="unplug to finish the update")

        self.assertEqual((error.code, str(error)), ("acpd_broken", "unplug to finish the update"))

    def test_a_marker_that_could_not_be_written_leaves_the_usual_limit(self) -> None:
        for case, device in (
            ("RAM disk refused it", FakeAcpDevice(acpd_hangs=True, marker_writable=False)),
            ("SSH down before the request", FakeAcpDevice(acpd_hangs=True, ssh_open=False)),
        ):
            with self.subTest(case=case):
                error, recorder = run(device, ssh=self.SSH)

                self.assertEqual(error.code, "reboot_not_finished")
                cycle = recorder.measurement("reboot_cycle")
                self.assertEqual(cycle["result"], "did_not_come_back_up")
                self.assertNotIn("restart_looks", cycle)
                self.assertNotIn("restart_look_last", cycle)
                self.assertEqual(device.ssh_reads, [("mark", False)])
                # Recorded on every wait, so the share of unwritten markers is known.
                self.assertEqual(cycle["restart_marker"], "unwritten")
                self.assertEqual(device.calls.count("request"), 1)

    def test_without_ssh_the_hang_still_ends_at_the_up_limit(self) -> None:
        device = FakeAcpDevice(acpd_hangs=True)
        error, recorder = run(device)

        self.assertEqual(error.code, "reboot_not_finished")
        self.assertEqual(recorder.measurement("reboot_cycle")["result"], "did_not_come_back_up")
        self.assertEqual(device.ssh_reads, [])
        self.assertNotIn("restart_looks", recorder.measurement("reboot_cycle"))
        self.assertNotIn("restart_marker", recorder.measurement("reboot_cycle"))

    def test_an_ssh_failure_reading_the_marker_is_not_a_hang(self) -> None:
        device = FakeAcpDevice(acpd_hangs=True)

        def ssh_drops(connection) -> bool:
            raise SshError("Connection reset by peer")

        device.restart_verdict = ssh_drops
        error, recorder = run(device, ssh=self.SSH)

        self.assertEqual(error.code, "reboot_not_finished")
        cycle = recorder.measurement("reboot_cycle")
        self.assertGreater(cycle["restart_looks"], 1)
        self.assertEqual(cycle["restart_look_last"], "ssh_failed")

    def test_a_failed_read_is_looked_at_again(self) -> None:
        # The read shows nothing either way; the next look finds the hang.
        device = FakeAcpDevice(acpd_hangs=True)
        read = device.restart_verdict
        answers: list[str] = []

        def first_read_fails(connection):
            if not answers:
                answers.append("failed")
                raise SshError("Connection reset by peer")
            answers.append("read")
            return read(connection)

        device.restart_verdict = first_read_fails
        error, recorder = run(device, ssh=self.SSH)

        self.assertEqual(error.code, "acpd_broken")
        self.assertEqual(answers, ["failed", "read"])
        cycle = recorder.measurement("reboot_cycle")
        self.assertEqual((cycle["restart_looks"], cycle["restart_look_last"]), (2, "hung"))

    def test_an_acpd_that_still_serves_the_device_is_never_reported_as_a_hang(self) -> None:
        # This computer's reads go unanswered, but ACPd's teardown never ran.
        device = FakeAcpDevice(acpd_hangs=True, teardown_runs=False)
        error, recorder = run(device, ssh=self.SSH)

        self.assertEqual(error.code, "reboot_not_finished")
        cycle = recorder.measurement("reboot_cycle")
        self.assertEqual((cycle["result"], cycle["restart_look_last"]), ("did_not_come_back_up", "not_taken"))

    def test_a_device_whose_ssh_is_down_is_not_asked(self) -> None:
        # A slow real reboot: the old sshd is gone and the new kernel is minutes away.
        device = FakeAcpDevice(kernel_after=480.0)
        error, recorder = run(device, ssh=self.SSH)

        self.assertIsNone(error)
        cycle = recorder.measurement("reboot_cycle")
        self.assertGreater(cycle["restart_looks"], 1)
        self.assertEqual(cycle["restart_look_last"], "ssh_closed")
        self.assertNotIn(("marker", True), device.ssh_reads)
        # No look reached sshd, so no connection was opened to close again.
        self.assertEqual(device.ssh_master_closes, [("10.0.0.2", False)])

    def test_a_slow_real_reboot_is_never_reported_as_a_hang(self) -> None:
        # The new kernel takes 8 minutes, and its sshd answers before its ACPd:
        # its RAM disk has no marker.
        device = FakeAcpDevice(kernel_after=480.0, acp_up_after_boot=60.0, ssh_up_after_boot=10.0)
        error, recorder = run(device, ssh=self.SSH)

        self.assertIsNone(error)
        self.assertEqual(recorder.measurement("reboot_cycle")["result"], "success")
        self.assertIn(("marker", True), device.ssh_reads)
        # The new kernel's RAM disk has no marker.
        self.assertEqual(recorder.measurement("reboot_cycle")["restart_look_last"], "none")

    def test_a_successful_wait_records_the_marker(self) -> None:
        moved, recorder = run_returning(FakeAcpDevice(), ssh=self.SSH)

        self.assertIsNone(moved)
        cycle = recorder.measurement("reboot_cycle")
        self.assertEqual((cycle["result"], cycle["restart_marker"]), ("success", "written"))

    def test_a_reboot_without_waiting_still_marks_it(self) -> None:
        for ssh, reads, debug in ((self.SSH, [("mark", False)], {"restart_marker": "written"}), (None, [], {})):
            with self.subTest(ssh=ssh):
                device = FakeAcpDevice()
                error, recorder = run(device, wait=False, ssh=ssh)

                self.assertIsNone(error)
                self.assertEqual(device.ssh_reads, reads)
                self.assertEqual(device.calls, ["request"])
                self.assertEqual({key: recorder.debug[key] for key in debug}, debug)
                if not debug:
                    self.assertNotIn("restart_marker", recorder.debug)


HOTSPOT = ("172.20.10.0/28",)


class ClientLeftNetworkTests(unittest.TestCase):
    """macOS can join another known network (a neighbour's Wi-Fi, an iPhone's
    hotspot) while the device's restart drops its link, and stay there: the
    wait then hears nothing until its limit, through no fault of the device."""

    def wait(self, device: FakeAcpDevice, target: str = "root@10.0.0.2", **kwargs):
        recorder = RecordingCallbacks()
        recorder.clock = device.monotonic
        start = device.now
        with device.patched():
            try:
                reboot_device(target, "pw", wait=True, callbacks=recorder.callbacks(), **kwargs)
                error = None
            except RebootFlowError as exc:
                error = exc
        return error, recorder, start

    def test_a_mac_that_never_comes_back_ends_with_its_own_code_and_a_hint_after_three_minutes(self) -> None:
        device = FakeAcpDevice(mac_network_changes=((20, HOTSPOT),))
        error, recorder, start = self.wait(device, wifi_name="Apple Network 619b7d")

        self.assertEqual((error.code, error.result), ("client_left_network", "client_left_network"))
        self.assertIn("172.20.10.0/28 (an iPhone's Personal Hotspot)", error.message)
        cycle = recorder.measurement("reboot_cycle")
        self.assertEqual(cycle["result"], "client_left_network")
        self.assertTrue(cycle["client_watched"])
        self.assertTrue(cycle["client_hinted"])
        self.assertGreaterEqual(cycle["client_left_after_sec"], 20)
        self.assertNotIn("client_rejoined_after_sec", cycle)
        # One hint, no sooner than three minutes after the device went down,
        # naming the device's Wi-Fi in the app and the CLI.
        self.assertEqual(len(recorder.progress), 1)
        at, stage, fields = recorder.progress[0]
        self.assertEqual((stage, fields), ("wait_for_reboot_up", hint_state(client_away=True, network_name="Apple Network 619b7d")))
        self.assertGreaterEqual(at - start, cycle["down_seen_after_sec"] + reboot_service.CLIENT_AWAY_HINT_AFTER_DOWN_SECONDS)
        hint = [message for message in recorder.messages if " left the device's network and is now on " in message]
        self.assertEqual(len(hint), 1)
        self.assertIn("“Apple Network 619b7d”", hint[0])
        self.assertIn("172.20.10.0/28 (an iPhone's Personal Hotspot)", hint[0])

    def test_a_mac_back_before_three_minutes_gets_no_hint(self) -> None:
        # Most Macs are back within about 70 s: telemetry still records the leave.
        device = FakeAcpDevice(mac_network_changes=((20, HOTSPOT), (120, ("10.0.0.0/24",))))
        error, recorder, _start = self.wait(device)

        self.assertIsNone(error)
        cycle = recorder.measurement("reboot_cycle")
        self.assertEqual(cycle["result"], "success")
        self.assertGreaterEqual(cycle["client_left_after_sec"], 20)
        self.assertGreaterEqual(cycle["client_rejoined_after_sec"], 120)
        self.assertNotIn("client_hinted", cycle)
        self.assertEqual(recorder.progress, [])

    def test_the_hint_clears_when_the_mac_comes_back(self) -> None:
        for back in (300, reboot_service.REBOOT_UP_TIMEOUT_SECONDS - 60):
            with self.subTest(back=back):
                device = FakeAcpDevice(mac_network_changes=((20, HOTSPOT), (back, ("10.0.0.0/24",))))
                error, recorder, _start = self.wait(device)

                self.assertIsNone(error)
                self.assertEqual([fields["client_away"] for _at, _stage, fields in recorder.progress], [True, False])
                self.assertIn(reboot_service.client_back_message(), recorder.messages)
                self.assertEqual(recorder.measurement("reboot_cycle")["result"], "success")
                self.assertEqual(recorder.measurement("reboot_cycle")["client_leaves"], 1)

    def test_each_leave_is_counted_so_the_first_and_last_times_are_not_read_as_one(self) -> None:
        # Left, back, left again: the first-left and last-rejoined times span
        # both, and the count says so.
        device = FakeAcpDevice(
            kernel_after=10_000.0,
            mac_network_changes=((20, HOTSPOT), (60, ("10.0.0.0/24",)), (100, HOTSPOT), (300, ("10.0.0.0/24",))),
        )
        _error, recorder, _start = self.wait(device)

        cycle = recorder.measurement("reboot_cycle")
        self.assertEqual(cycle["client_leaves"], 2)
        self.assertLess(cycle["client_left_after_sec"], 60)
        self.assertGreaterEqual(cycle["client_rejoined_after_sec"], 300)

    def test_a_mac_back_on_the_network_while_the_device_stays_down_keeps_the_old_ending(self) -> None:
        # The Mac rejoined, so it would have seen the device: the device is the problem.
        device = FakeAcpDevice(kernel_after=10_000.0, mac_network_changes=((20, HOTSPOT), (300, ("10.0.0.0/24",))))
        error, recorder, _start = self.wait(device)

        self.assertEqual(error.code, "reboot_not_finished")
        cycle = recorder.measurement("reboot_cycle")
        self.assertEqual(cycle["result"], "did_not_come_back_up")
        self.assertGreaterEqual(cycle["client_rejoined_after_sec"], 300)

    def test_a_device_reached_through_a_router_is_never_watched(self) -> None:
        # This Mac's own networks say nothing about reaching it.
        device = FakeAcpDevice(
            mac_networks_before=("192.168.9.0/24",), kernel_after=10_000.0,
            mac_network_changes=((20, HOTSPOT),),
        )
        error, recorder, _start = self.wait(device)

        self.assertEqual(error.code, "reboot_not_finished")
        cycle = recorder.measurement("reboot_cycle")
        self.assertFalse(cycle["client_watched"])
        self.assertNotIn("client_left_after_sec", cycle)
        self.assertEqual(recorder.progress, [])

    def test_no_address_or_nothing_to_compare_is_not_a_leave(self) -> None:
        # A restart drops the link: no address, a link-local one only, or
        # another family only is the usual wait, not this Mac on another network.
        for networks in ((), ("169.254.0.0/16",), ("2001:db8:1::/64",)):
            with self.subTest(networks=networks):
                device = FakeAcpDevice(kernel_after=10_000.0, mac_network_changes=((20, networks),))
                error, recorder, _start = self.wait(device)

                self.assertEqual(error.code, "reboot_not_finished")
                self.assertNotIn("client_left_after_sec", recorder.measurement("reboot_cycle"))
                self.assertNotIn(hint_state(client_away=True), [
                    fields for _at, _stage, fields in recorder.progress
                ])

    def test_one_interface_still_on_the_devices_network_is_not_a_leave(self) -> None:
        device = FakeAcpDevice(
            mac_networks_before=("10.0.0.0/24", "192.168.5.0/24"),
            mac_network_changes=((20, ("10.0.0.0/24", "172.20.10.0/28")),),
        )
        error, recorder, _start = self.wait(device)

        self.assertIsNone(error)
        self.assertNotIn("client_left_after_sec", recorder.measurement("reboot_cycle"))

    def test_a_mac_left_only_on_a_network_it_already_had_is_not_a_leave(self) -> None:
        # On the device's Ethernet and on another Wi-Fi network: the restart
        # drops the Ethernet link, and it comes back with the device.
        device = FakeAcpDevice(
            mac_networks_before=("10.0.0.0/24", "192.168.5.0/24"), kernel_after=10_000.0,
            mac_network_changes=((20, ("192.168.5.0/24",)),),
        )
        error, recorder, _start = self.wait(device)

        self.assertEqual(error.code, "reboot_not_finished")
        cycle = recorder.measurement("reboot_cycle")
        self.assertTrue(cycle["client_watched"])
        self.assertNotIn("client_left_after_sec", cycle)
        self.assertEqual(recorder.progress, [])

    def test_a_new_network_beside_one_it_already_had_is_a_leave_named_by_the_new_one(self) -> None:
        device = FakeAcpDevice(
            mac_networks_before=("10.0.0.0/24", "192.168.5.0/24"),
            mac_network_changes=((20, ("192.168.5.0/24", "172.20.10.0/28")),),
        )
        error, recorder, _start = self.wait(device)

        self.assertEqual(error.code, "client_left_network")
        self.assertIn("172.20.10.0/28", error.message)
        self.assertNotIn("192.168.5.0/24", error.message)
        self.assertEqual(recorder.measurement("reboot_cycle")["client_leaves"], 1)

    def test_a_hostname_target_is_resolved_once_before_the_request(self) -> None:
        # A .local name does not resolve while the device is down.
        import ipaddress

        device = FakeAcpDevice()
        with mock.patch.object(reboot_service, "host_networks", return_value=[ipaddress.ip_network("10.0.0.2/32")]) as resolve:
            error, recorder, _start = self.wait(device, target="root@office.local")

        self.assertIsNone(error)
        resolve.assert_called_once_with("office.local")
        self.assertTrue(recorder.measurement("reboot_cycle")["client_watched"])

    def test_the_acpd_hang_check_does_not_fire_while_the_mac_is_away(self) -> None:
        # SSH to the device reads as closed from another network: a real
        # restart is never taken for ACPd's hang.
        device = FakeAcpDevice(mac_network_changes=((20, HOTSPOT), (400, ("10.0.0.0/24",))))
        ssh = SshConnection("root@10.0.0.2", "pw", "")
        error, recorder, _start = self.wait(device, ssh=ssh)

        self.assertIsNone(error)
        cycle = recorder.measurement("reboot_cycle")
        self.assertGreaterEqual(cycle["restart_looks"], 2)
        self.assertNotIn("acpd_hang_seen_after_sec", cycle)
        self.assertFalse(any(fields.get("acpd_hung") for _at, _stage, fields in recorder.progress))

    def test_a_mac_that_leaves_after_the_unplug_advice_keeps_it(self) -> None:
        # Unplugging a device that is this Mac's Wi-Fi sends the Mac to
        # another network: its hint says the device still needs the unplug.
        device = FakeAcpDevice(
            acpd_hangs=True, unplugged_after=300.0, mac_network_changes=((90, HOTSPOT), (420, ("10.0.0.0/24",))),
        )
        error, recorder, _start = self.wait(device, ssh=SshConnection("root@10.0.0.2", "pw", ""))

        self.assertIsNone(error)
        hints = [fields for _at, stage, fields in recorder.progress if stage == "wait_for_reboot_up"]
        self.assertEqual(hints[0], hint_state(acpd_hung=True))
        self.assertEqual(hints[1], hint_state(client_away=True, acpd_hung=True))

    def test_whether_the_device_is_this_macs_router_is_recorded_on_success(self) -> None:
        # Telemetry weighs how often a router-mode restart loses the Mac. It is
        # read before the request: a router-mode device's restart takes the
        # route away.
        for gateway, expected in (("10.0.0.2", True), ("10.0.0.1", False), (None, None)):
            with self.subTest(gateway=gateway):
                device = FakeAcpDevice(mac_gateway=gateway)
                recorder = RecordingCallbacks()
                with device.patched():
                    reboot_device("root@10.0.0.2", "pw", wait=True, callbacks=recorder.callbacks())

                # One read before the request; the device was back before any other.
                self.assertEqual(device.router_reads, [])
                self.assertEqual(recorder.measurement("reboot_cycle").get("device_is_gateway"), expected)

    def test_without_a_wifi_name_the_hint_names_the_devices_network(self) -> None:
        device = FakeAcpDevice(mac_network_changes=((20, ("192.168.50.0/24",)),))
        error, recorder, _start = self.wait(device)

        self.assertEqual(recorder.progress[0][2], hint_state(client_away=True))
        hint = next(message for message in recorder.messages if " left the device's network" in message)
        self.assertIn("Reconnect it to the device's network;", hint)
        self.assertIn("192.168.50.0/24", hint)
        # The ending's message reaches telemetry: never a Wi-Fi name.
        self.assertNotIn("“", error.message)



def hint_state(**shown: object) -> dict[str, object]:
    """A restart wait's progress event: every hint's state, as the app reads
    the latest event alone (services.reboot._wait's hints)."""
    return {
        "client_away": False, "network_name": "", "acpd_hung": False, "restart_stuck": False, "client_offline": False,
        **shown,
    }


ROUTER = "aa:bb:cc:00:00:01"
OTHER_ROUTER = "aa:bb:cc:00:00:02"
STUCK_AFTER = reboot_service.RESTART_STUCK_AFTER_DOWN_SECONDS


def stuck_device(**kwargs) -> FakeAcpDevice:
    """A device that took the request, went off the network and never starts
    again until it is unplugged (unplugged_after): ACP and SSH stay silent."""
    kwargs.setdefault("mac_router", ROUTER)
    kwargs.setdefault("old_ssh_until", 10.0)
    return FakeAcpDevice(acpd_hangs=True, **kwargs)


class RestartStuckTests(unittest.TestCase):
    """A device that stays silent long after it went down, while this Mac is
    still on its network (the same router toward it), may have stopped partway
    through the restart: the wait asks for an unplug and goes on."""

    def wait(self, device: FakeAcpDevice, **kwargs):
        recorder = RecordingCallbacks()
        recorder.clock = device.monotonic
        start = device.now
        with device.patched():
            try:
                reboot_device("root@10.0.0.2", "pw", wait=True, callbacks=recorder.callbacks(), **kwargs)
                error = None
            except RebootFlowError as exc:
                error = exc
        return error, recorder, start

    @staticmethod
    def hints(recorder: RecordingCallbacks) -> list[dict[str, object]]:
        return [fields for _at, stage, fields in recorder.progress if stage == "wait_for_reboot_up"]

    def test_silent_on_the_same_network_asks_for_an_unplug_then_ends_with_its_own_code(self) -> None:
        device = stuck_device()
        error, recorder, start = self.wait(device)

        self.assertEqual((error.code, error.result), ("restart_stuck", "restart_stuck"))
        self.assertEqual(error.message, restart_stuck_message())
        # Silence is all it saw: the device may be stuck, or back at a new
        # address that only Bonjour could have shown (services.locate). The
        # message states that, and the steps (app.recovery) say what to do.
        self.assertIn("may have stopped partway through the restart", error.message)
        self.assertIn("If the network blocks Bonjour, it may instead be back at a new address.", error.message)
        self.assertNotIn("nplug", error.message)
        cycle = recorder.measurement("reboot_cycle")
        self.assertEqual(cycle["result"], "restart_stuck")
        down = cycle["down_seen_after_sec"]
        stuck = cycle["stuck_hint_after_sec"]
        # Not before the threshold, and within one poll and a router read of it.
        self.assertGreaterEqual(stuck - down, STUCK_AFTER)
        self.assertLess(stuck - down, STUCK_AFTER + 2 * reboot_service.REBOOT_POLL_SECONDS)
        self.assertEqual(cycle["stuck_hint_place"], "same")
        # The whole limit again after the hint, for the unplug and the start-up.
        self.assertGreaterEqual(device.now - start, stuck + UP_LIMIT)
        self.assertEqual(self.hints(recorder), [hint_state(restart_stuck=True)])
        unplug_lines = [m for m in recorder.messages if "nplug" in m]
        self.assertEqual(unplug_lines, [RESTART_STUCK_HINT])
        # The same silence in the hint: the unplug is only something to try,
        # and the device may instead be back at an address Bonjour did not show.
        self.assertIn("Try unplugging the device, waiting 10 seconds and plugging it back in", unplug_lines[0])
        self.assertIn("If the network blocks Bonjour, the device may be at a new address instead.", unplug_lines[0])
        self.assertNotIn("Unplug the device", unplug_lines[0])
        # Telemetry carries the routers that decided it.
        self.assertEqual(cycle["routers_before"], [{"interface": "en0", "ip": "10.0.0.1", "mac": ROUTER}])
        self.assertEqual((cycle["client_place"], cycle["place_evidence"]), ("same", "router"))
        self.assertEqual((cycle["router_mac_before"], cycle["router_mac_now"]), (ROUTER, ROUTER))

    def test_an_unplug_after_the_hint_finishes_the_wait(self) -> None:
        device = stuck_device(unplugged_after=STUCK_AFTER + 120.0)
        error, recorder, _start = self.wait(device)

        self.assertIsNone(error)
        cycle = recorder.measurement("reboot_cycle")
        self.assertEqual(cycle["result"], "success")
        self.assertIn("stuck_hint_after_sec", cycle)
        self.assertEqual(
            self.hints(recorder), [hint_state(restart_stuck=True), hint_state()],
        )

    def test_a_restart_back_before_the_threshold_never_hints(self) -> None:
        device = FakeAcpDevice(kernel_after=STUCK_AFTER - 60.0, mac_router=ROUTER)
        error, recorder, _start = self.wait(device)

        self.assertIsNone(error)
        self.assertNotIn("stuck_hint_after_sec", recorder.measurement("reboot_cycle"))
        self.assertEqual(self.hints(recorder), [])

    def test_without_a_readable_router_the_ending_only_conditionally_asks_for_an_unplug(self) -> None:
        # Another Wi-Fi with the same addresses looks the same as this one:
        # only the router would tell them apart.
        device = stuck_device(mac_router=None)
        error, recorder, _start = self.wait(device, wifi_name="Home")

        self.assertEqual(error.code, "reboot_not_finished")
        self.assertTrue(error.message.endswith(unplug_if_on_network_note("Home")))
        self.assertEqual(self.hints(recorder), [])
        self.assertEqual(recorder.measurement("reboot_cycle")["client_place"], "unknown")

    def test_another_router_on_the_same_addresses_is_another_network(self) -> None:
        device = stuck_device(mac_router_changes=((20.0, OTHER_ROUTER),))
        error, recorder, _start = self.wait(device)

        self.assertEqual(error.code, "client_left_network")
        self.assertIn("is on another network and did not come back", error.message)
        cycle = recorder.measurement("reboot_cycle")
        self.assertNotIn("stuck_hint_after_sec", cycle)
        self.assertEqual(cycle["router_mac_away"], OTHER_ROUTER)
        self.assertEqual((cycle["client_place"], cycle["place_evidence"]), ("moved", "router"))
        self.assertIn(hint_state(client_away=True), self.hints(recorder))
        hint = next(m for m in recorder.messages if " left the device's network" in m)
        self.assertIn("is now on another network.", hint)

    def test_one_reading_of_another_router_is_not_a_leave(self) -> None:
        # A roam can show another router for a moment; two readings in a row decide.
        device = stuck_device(mac_router_changes=((190.0, OTHER_ROUTER), (200.0, ROUTER)))
        error, recorder, _start = self.wait(device)

        self.assertEqual(error.code, "restart_stuck")
        self.assertNotIn("client_left_after_sec", recorder.measurement("reboot_cycle"))

    def test_an_unreadable_router_mid_wait_is_not_a_leave(self) -> None:
        device = stuck_device(mac_router_changes=((20.0, None),))
        error, recorder, _start = self.wait(device)

        self.assertEqual(error.code, "reboot_not_finished")
        self.assertNotIn("client_left_after_sec", recorder.measurement("reboot_cycle"))
        self.assertEqual(recorder.measurement("reboot_cycle")["client_place"], "unknown")

    def test_acp_refused_all_along_is_an_acpd_hang_not_a_stuck_restart(self) -> None:
        # A refused port is a host that is up: never the stuck hint. Refused
        # for six minutes, the kernel is up with ACPd gone, the hang, and the
        # wait asks for the unplug as the marker look would.
        device = stuck_device(down_read_error="Connection refused")
        error, recorder, _start = self.wait(device, up_timeout_message="Timed out waiting for SSH after reboot.")

        self.assertEqual(error.code, "acpd_broken")
        self.assertEqual(self.hints(recorder), [hint_state(acpd_hung=True)])
        cycle = recorder.measurement("reboot_cycle")
        self.assertEqual(cycle["acpd_hang_evidence"], "acp_refused")
        self.assertNotIn("stuck_hint_after_sec", cycle)
        # Inferred, not seen: the words say what was seen, never "Apple ACPd
        # is broken", which telemetry files as the known firmware bug.
        self.assertEqual(error.message, reboot_service.ACPD_REFUSED_MESSAGE)
        self.assertIn(reboot_service.ACPD_REFUSED_UNPLUG_MESSAGE, recorder.messages)
        self.assertNotIn(reboot_service.UNPLUG_MESSAGE, recorder.messages)

    def test_an_inferred_hang_keeps_what_the_caller_adds(self) -> None:
        # Deploy's message adds what the hang means for the install; the
        # inferred hang swaps only the opening sentence.
        from timecapsulesmb.services.deploy import DEPLOY_ACPD_STOPPED_MESSAGE

        device = stuck_device(down_read_error="Connection refused")
        error, _recorder, _start = self.wait(device, stopped_message=DEPLOY_ACPD_STOPPED_MESSAGE)

        self.assertEqual(error.code, "acpd_broken")
        self.assertEqual(
            error.message,
            DEPLOY_ACPD_STOPPED_MESSAGE.replace(reboot_service.ACPD_STOPPED_MESSAGE, reboot_service.ACPD_REFUSED_MESSAGE),
        )
        self.assertTrue(error.message.startswith(reboot_service.ACPD_REFUSED_MESSAGE))
        self.assertIn("The update is installed", error.message)

    def test_in_the_app_no_route_reads_are_not_a_stuck_device(self) -> None:
        # macOS refusing the app's connections (Local Network access) fails
        # every read at once with no route, while the device is up: no unplug,
        # not even a conditional one.
        device = stuck_device(down_read_error="[Errno 65] No route to host",
                              ssh_port_error="[Errno 65] No route to host")
        with mock.patch.object(reboot_service, "macos_may_hold_this_process", return_value=True):
            error, recorder, _start = self.wait(device, up_timeout_message="Timed out waiting for SSH after reboot.")

        self.assertEqual(error.code, "reboot_not_finished")
        self.assertEqual(error.message, "Timed out waiting for SSH after reboot.")
        self.assertEqual(self.hints(recorder), [])

    def test_in_the_app_a_stray_unclear_read_keeps_the_unplug_request(self) -> None:
        # A device that stopped partway (9b5781b4) gives host down with now
        # and then one no route; a firewall can refuse one read. Neither is an
        # answer: the request stays up, even for a stray read just before the
        # limit.
        down, no_route = "[Errno 64] Host is down", "[Errno 65] No route to host"
        refused_here = "[Errno 1] Operation not permitted"
        limit = STUCK_AFTER + reboot_service.REBOOT_UP_TIMEOUT_SECONDS
        device = stuck_device(down_read_error=down, ssh_port_error=down, down_read_error_changes=(
            (236.0, no_route), (241.0, down), (541.0, no_route), (546.0, down),
            (limit - 15.0, refused_here), (limit - 10.0, down),
        ))
        with mock.patch.object(reboot_service, "macos_may_hold_this_process", return_value=True):
            error, recorder, _start = self.wait(device)

        cycle = recorder.measurement("reboot_cycle")
        # One read in each window, so the case exercises what it names.
        self.assertEqual((cycle["read_errors"]["no_route"]["count"], cycle["read_errors"]["not_permitted"]["count"]),
                         (2, 1))
        self.assertEqual(self.hints(recorder), [hint_state(restart_stuck=True)])
        self.assertNotIn("stuck_hint_cleared_after_sec", cycle)
        self.assertNotIn(reboot_service.RESTART_STUCK_WITHDRAWN_MESSAGE, recorder.messages)
        self.assertEqual(error.code, "restart_stuck")

    def test_unclear_reads_in_a_row_take_the_unplug_request_back(self) -> None:
        # Reads that keep failing in a way that says nothing about the device
        # (a firewall that starts refusing) no longer show it silent: the
        # request goes, and comes back once reads find nothing again, as many
        # in a row as made it silent at first.
        down = "[Errno 64] Host is down"
        device = stuck_device(down_read_error=down, ssh_port_error=down, down_read_error_changes=(
            (STUCK_AFTER + 30.0, "[Errno 1] Operation not permitted"), (STUCK_AFTER + 45.0, down),
        ))
        with mock.patch.object(reboot_service, "macos_may_hold_this_process", return_value=True):
            error, recorder, _start = self.wait(device)

        cycle = recorder.measurement("reboot_cycle")
        self.assertEqual(cycle["read_errors"]["not_permitted"]["count"], reboot_service.STUCK_QUIET_READS)
        self.assertEqual(
            self.hints(recorder), [hint_state(restart_stuck=True), hint_state(), hint_state(restart_stuck=True)],
        )
        self.assertIn(reboot_service.RESTART_STUCK_WITHDRAWN_MESSAGE, recorder.messages)
        self.assertEqual(error.code, "restart_stuck")

    def test_in_the_app_a_wait_ending_in_no_route_with_ssh_through_is_a_local_network_block(self) -> None:
        # Apple's ssh is not held by Local Network access: it reaching the
        # device proves macOS refused the app's reads, which an unplug does
        # not fix.
        from timecapsulesmb.device.probe import SshCommandProbeResult

        device = stuck_device(down_read_error="[Errno 65] No route to host")
        reached = SshCommandProbeResult(ok=True, detail="ok")
        connection = SshConnection("root@10.0.0.2", "pw", "")
        with mock.patch.object(reboot_service, "macos_may_hold_this_process", return_value=True), \
                mock.patch.object(reboot_service, "probe_ssh_command_conn", return_value=reached) as proof, \
                mock.patch("timecapsulesmb.checks.network.running_in_app", return_value=True), \
                mock.patch("timecapsulesmb.checks.network.local_lan_networks", side_effect=device.local_lan_networks), \
                mock.patch("timecapsulesmb.checks.network.tcp_connect_error",
                           return_value="[Errno 65] No route to host"):
            error, recorder, _start = self.wait(device, ssh=connection)

        proof.assert_called_once()
        self.assertEqual((error.code, error.result), ("acp_no_route", "local_network_blocked"))
        self.assertEqual(recorder.measurement("reboot_cycle")["local_network_block_proof"], "ssh")

    def test_in_the_app_without_ssh_to_prove_it_a_no_route_ending_gives_no_unplug(self) -> None:
        # Add Device's restart has no SSH yet: nothing proves the block, and
        # nothing shows the device is off either.
        device = stuck_device(down_read_error="[Errno 65] No route to host")
        with mock.patch.object(reboot_service, "macos_may_hold_this_process", return_value=True):
            error, _recorder, _start = self.wait(device, up_timeout_message="Timed out waiting for SSH after reboot.")

        self.assertEqual((error.code, error.message), ("reboot_not_finished", "Timed out waiting for SSH after reboot."))

    def test_where_nothing_holds_this_process_no_route_reads_are_a_silent_device(self) -> None:
        # Linux reports a host gone from its link as no route and has no Local
        # Network access, as Terminal and an SSH session on a Mac have none:
        # there no route is silence.
        device = stuck_device(down_read_error="[Errno 113] No route to host",
                              ssh_port_error="[Errno 113] No route to host")
        with mock.patch.object(reboot_service, "macos_may_hold_this_process", return_value=False):
            error, recorder, _start = self.wait(device)

        self.assertEqual(error.code, "restart_stuck")
        self.assertEqual(self.hints(recorder)[0], hint_state(restart_stuck=True))

    def test_an_answering_ssh_port_is_not_a_stuck_device(self) -> None:
        # A new boot whose ACPd quit at once, or an old kernel whose sshd still
        # takes connections: the device is up, so it did not go off the network.
        for name, device in (
            ("open", stuck_device(old_ssh_until=10_000.0)),
            ("refused", stuck_device(ssh_port_error="Connection refused")),
        ):
            with self.subTest(name):
                error, recorder, _start = self.wait(device, up_timeout_message="Timed out waiting for SSH after reboot.")
                self.assertEqual((error.code, error.message), ("reboot_not_finished", "Timed out waiting for SSH after reboot."))
                self.assertEqual(self.hints(recorder), [])
                self.assertEqual(recorder.measurement("reboot_cycle")["stuck_ssh_port"], name)

    def test_a_host_that_is_down_counts_as_silent(self) -> None:
        device = stuck_device(down_read_error="[Errno 64] Host is down")
        error, _recorder, _start = self.wait(device)

        self.assertEqual(error.code, "restart_stuck")

    def test_a_mac_left_without_a_network_gets_a_conditional_hint(self) -> None:
        # A Mac on the device's own Wi-Fi has no network while the device is
        # off; one that lost its Wi-Fi for another reason looks the same.
        device = stuck_device(mac_network_changes=((20.0, ()),))
        error, recorder, _start = self.wait(device, wifi_name="Home")

        self.assertEqual(error.code, "reboot_not_finished")
        self.assertEqual(
            self.hints(recorder), [hint_state(network_name="Home", restart_stuck=True, client_offline=True)],
        )
        self.assertIn(restart_stuck_offline_hint("Home"), recorder.messages)
        # A Mac on one of the device's LAN ports loses its link the same way.
        self.assertIn(
            "If it reached the network through the device (its Wi-Fi network \u201cHome\u201d or one of its LAN ports), "
            "unplug the device",
            restart_stuck_offline_hint("Home"),
        )
        self.assertEqual(recorder.measurement("reboot_cycle")["stuck_hint_place"], "offline")

    def test_a_router_mode_device_that_takes_the_router_away_is_silent_on_the_same_network(self) -> None:
        # The device is this Mac's router: while it is down there is none toward
        # it, and a Mac that stayed has no other.
        device = stuck_device(mac_gateway="10.0.0.2", mac_router_changes=((15.0, None),))
        error, recorder, _start = self.wait(device)

        self.assertEqual(error.code, "restart_stuck")
        self.assertTrue(recorder.measurement("reboot_cycle")["device_is_gateway"])

    def test_the_routers_are_read_at_most_every_fifteen_seconds(self) -> None:
        device = stuck_device()
        _error, recorder, _start = self.wait(device)

        cycle = recorder.measurement("reboot_cycle")
        stuck = cycle["stuck_hint_after_sec"]
        # The readings while the device is down, before the hint and the
        # ending's own. The fake times them from the request, which comes a
        # second after the wait's start.
        periodic = [at + 1.0 for at in device.router_reads if at + 1.0 < stuck]
        self.assertTrue(periodic)
        self.assertGreaterEqual(periodic[0], cycle["down_seen_after_sec"] + reboot_service.CLIENT_AWAY_HINT_AFTER_DOWN_SECONDS)
        gaps = [later - earlier for earlier, later in zip(periodic, periodic[1:])]
        self.assertTrue(all(gap >= reboot_service.PLACE_READ_INTERVAL_SECONDS for gap in gaps), gaps)

    def test_a_device_reached_through_a_router_is_not_judged(self) -> None:
        # This Mac's own networks say nothing about a device it reaches through
        # a router (another subnet, a VPN).
        device = stuck_device(mac_networks_before=("192.168.9.0/24",))
        error, recorder, _start = self.wait(device)

        self.assertEqual(error.code, "reboot_not_finished")
        self.assertEqual(self.hints(recorder), [])
        self.assertNotIn("client_place", recorder.measurement("reboot_cycle"))

    def test_the_enable_ssh_restart_keeps_the_stuck_code(self) -> None:
        # Configure and set-ssh convert only reboot_not_finished.
        device = stuck_device()
        recorder = RecordingCallbacks()
        with device.patched(), self.assertRaises(RebootFlowError) as caught:
            reboot_service.reboot_to_enable_ssh("root@10.0.0.2", "pw", wait=True, callbacks=recorder.callbacks())

        self.assertEqual(caught.exception.code, "restart_stuck")

    def test_a_saved_name_that_stops_resolving_counts_as_silent(self) -> None:
        # A device saved by its .local name: once it is off nothing answers
        # for the name, and every read fails the lookup.
        device = stuck_device(down_read_error="[Errno 8] nodename nor servname provided, or not known")
        error, recorder, _start = self.wait(device)

        self.assertEqual(error.code, "restart_stuck")
        self.assertEqual(self.hints(recorder)[0], hint_state(restart_stuck=True))

    def test_with_an_unreadable_router_the_routers_are_still_read_every_fifteen_seconds(self) -> None:
        # No verdict to give, so the hint never shows: the check must not read
        # the routers on every poll meanwhile.
        device = stuck_device(mac_router=None, mac_gateway="10.0.0.1")
        error, _recorder, _start = self.wait(device)

        self.assertEqual(error.code, "reboot_not_finished")
        reads = [at for at in device.router_reads]
        gaps = [later - earlier for earlier, later in zip(reads, reads[1:-1])]
        self.assertTrue(gaps)
        self.assertTrue(all(gap >= reboot_service.PLACE_READ_INTERVAL_SECONDS for gap in gaps), gaps)

    def test_a_device_that_answers_after_the_hint_does_not_end_as_stuck(self) -> None:
        # Unplugged and back on a new boot whose ACPd refuses: it came back,
        # so not "did not come back on this network". The stuck hint goes at
        # the first refusal, as an unplug then would interrupt a device that is
        # starting up; six minutes of refusals are ACPd that never came up.
        device = stuck_device(down_read_error_changes=((STUCK_AFTER + 120.0, "Connection refused"),))
        error, recorder, _start = self.wait(device, up_timeout_message="Timed out waiting for SSH after reboot.")

        cycle = recorder.measurement("reboot_cycle")
        self.assertEqual(error.code, "acpd_broken")
        self.assertEqual(
            self.hints(recorder), [hint_state(restart_stuck=True), hint_state(), hint_state(acpd_hung=True)],
        )
        self.assertGreater(cycle["stuck_hint_cleared_after_sec"], cycle["stuck_hint_after_sec"])
        self.assertGreaterEqual(
            cycle["acpd_hang_seen_after_sec"] - cycle["stuck_hint_cleared_after_sec"], STUCK_AFTER,
        )
        # The CLI says each change: its last line would otherwise still ask for an unplug.
        unplug_lines = [m for m in recorder.messages if "nplug" in m or "plugged in" in m]
        self.assertEqual(
            unplug_lines,
            [RESTART_STUCK_HINT, reboot_service.RESTART_STUCK_WITHDRAWN_MESSAGE, reboot_service.ACPD_REFUSED_UNPLUG_MESSAGE],
        )

    def test_withdrawing_the_stuck_hint_keeps_the_away_hint(self) -> None:
        # The stuck hint shows, then this Mac roams to another network with
        # the same addresses, where some other host holds the device's address
        # and refuses ACP. The refusal withdraws the stuck hint; the event that
        # says so still carries the away hint, which the app shows on its own.
        device = stuck_device(
            mac_router_changes=((STUCK_AFTER + 30.0, OTHER_ROUTER),),
            down_read_error_changes=((STUCK_AFTER + 120.0, "Connection refused"),),
        )
        error, recorder, _start = self.wait(device)

        self.assertEqual(error.code, "client_left_network")
        hints = self.hints(recorder)
        self.assertEqual(
            hints, [hint_state(restart_stuck=True), hint_state(client_away=True, restart_stuck=True),
                    hint_state(client_away=True)],
        )
        # That host's refusals say nothing about ACPd on the device.
        self.assertNotIn("acpd_hang_evidence", recorder.measurement("reboot_cycle"))

    def test_a_device_that_keeps_answering_and_falling_silent_ends_the_terminal_lines_with_one_that_stays_true(self) -> None:
        # Refused and silent in turns, a minute each: the app's progress line
        # follows every turn. A terminal prints each change until the second
        # withdrawal, then one line that holds whichever way the device turns,
        # so neither a flood nor a last line that contradicts the wait.
        changes = tuple(
            (STUCK_AFTER + 60.0 * index, "Connection refused" if index % 2 else "timed out") for index in range(1, 9)
        )
        device = stuck_device(down_read_error_changes=changes)
        _error, recorder, _start = self.wait(device)

        hints = self.hints(recorder)
        self.assertGreaterEqual(hints.count(hint_state(restart_stuck=True)), 4)
        withdrawn = reboot_service.RESTART_STUCK_WITHDRAWN_MESSAGE
        stuck_lines = [
            message for message in recorder.messages
            if message in (RESTART_STUCK_HINT, withdrawn, reboot_service.RESTART_STUCK_FLAPPING_MESSAGE)
        ]
        self.assertEqual(
            stuck_lines,
            [RESTART_STUCK_HINT, withdrawn, RESTART_STUCK_HINT, withdrawn, reboot_service.RESTART_STUCK_FLAPPING_MESSAGE],
        )

    def test_a_device_that_answers_then_falls_silent_again_is_hinted_again_within_the_same_limit(self) -> None:
        # ACPd refused for a while, then nothing answers again: the unplug is
        # asked for again, but the limit still counts from the first hint, so
        # a device that keeps answering and falling silent cannot hold the wait open.
        device = stuck_device(down_read_error_changes=(
            (STUCK_AFTER + 60.0, "Connection refused"), (STUCK_AFTER + 120.0, "timed out"),
        ))
        error, recorder, start = self.wait(device)

        self.assertEqual(error.code, "restart_stuck")
        shown = hint_state(restart_stuck=True)
        self.assertEqual(self.hints(recorder), [shown, hint_state(), shown])
        # A terminal's last line asks for the unplug again, as the wait does.
        withdrawn = reboot_service.RESTART_STUCK_WITHDRAWN_MESSAGE
        self.assertEqual(
            [m for m in recorder.messages if m in (RESTART_STUCK_HINT, withdrawn)],
            [RESTART_STUCK_HINT, withdrawn, RESTART_STUCK_HINT],
        )
        first = recorder.measurement("reboot_cycle")["stuck_hint_after_sec"]
        self.assertLess(device.now - start, first + UP_LIMIT + 2 * reboot_service.REBOOT_POLL_SECONDS)

    def test_the_hint_follows_this_mac_getting_its_network_back(self) -> None:
        # Offline when the hint showed (its Wi-Fi was the device's), then back
        # on the same router while the device stays silent: the hint drops the
        # offline wording, and the wait ends as a stuck restart.
        device = stuck_device(mac_network_changes=((20.0, ()), (STUCK_AFTER + 60.0, ("10.0.0.0/24",))))
        error, recorder, _start = self.wait(device, wifi_name="Home")

        self.assertEqual(error.code, "restart_stuck")
        self.assertEqual(
            self.hints(recorder),
            [
                hint_state(network_name="Home", restart_stuck=True, client_offline=True),
                hint_state(network_name="Home", restart_stuck=True),
            ],
        )
        self.assertEqual(
            [m for m in recorder.messages if "unplug" in m.lower()],
            [restart_stuck_offline_hint("Home"), RESTART_STUCK_HINT],
        )

    def test_the_away_hint_clears_when_this_mac_is_back_behind_its_router(self) -> None:
        device = stuck_device(mac_router_changes=((20.0, OTHER_ROUTER), (300.0, ROUTER)))
        error, recorder, _start = self.wait(device)

        self.assertEqual(error.code, "restart_stuck")
        hints = self.hints(recorder)
        self.assertIn(hint_state(client_away=True), hints)
        self.assertIn(hint_state(), hints)
        self.assertIn(reboot_service.client_back_message(), recorder.messages)

    def test_off_macos_the_words_name_this_computer(self) -> None:
        self.assertIn("while this computer stayed on it", restart_stuck_message(platform="linux"))
        self.assertTrue(restart_stuck_offline_hint(None, platform="linux").startswith("This computer has no network"))
        self.assertIn("(its Wi-Fi or one of its LAN ports)", restart_stuck_offline_hint(None, platform="linux"))
        self.assertTrue(unplug_if_on_network_note(None, platform="linux").startswith("If this computer is on"))
        self.assertIn("(its Wi-Fi network is \u201cHome\u201d)", unplug_if_on_network_note("Home", platform="darwin"))

class AcpRefusedHangTests(unittest.TestCase):
    """ACP refused for RESTART_STUCK_AFTER_DOWN_SECONDS: the kernel is up and
    ACPd is gone, the hang the marker look finds, also where there is no SSH
    to look with (the restart that turns SSH on)."""

    def enable_ssh(self, device: FakeAcpDevice):
        recorder = RecordingCallbacks()
        recorder.clock = device.monotonic
        with device.patched():
            try:
                reboot_service.reboot_to_enable_ssh("root@10.0.0.2", "pw", wait=True, callbacks=recorder.callbacks())
                error = None
            except RebootFlowError as exc:
                error = exc
        return error, recorder

    def test_the_enable_ssh_restart_tells_a_hang_from_a_timeout(self) -> None:
        # SSH is still off on the old kernel, so its port refuses too: before,
        # this ended as ssh_enable_timeout with only a conditional unplug.
        device = stuck_device(down_read_error="Connection refused", ssh_open=False, ssh_port_error="Connection refused")
        error, recorder = self.enable_ssh(device)

        self.assertEqual(error.code, "acpd_broken")
        self.assertEqual(error.message, reboot_service.ACPD_REFUSED_MESSAGE)
        cycle = recorder.measurement("reboot_cycle")
        self.assertEqual(cycle["acpd_hang_evidence"], "acp_refused")
        self.assertGreaterEqual(cycle["acpd_hang_seen_after_sec"] - cycle["down_seen_after_sec"], STUCK_AFTER)
        self.assertIn(reboot_service.ACPD_REFUSED_UNPLUG_MESSAGE, recorder.messages)

    def test_an_unplug_after_the_inferred_hang_finishes_the_wait(self) -> None:
        device = stuck_device(
            down_read_error="Connection refused", ssh_open=False, ssh_port_error="Connection refused",
            unplugged_after=STUCK_AFTER + 120.0,
        )
        error, recorder = self.enable_ssh(device)

        self.assertIsNone(error)
        hints = [fields for _at, stage, fields in recorder.progress if stage == "wait_for_reboot_up"]
        self.assertEqual(hints, [hint_state(acpd_hung=True), hint_state()])

    def test_refusals_while_a_restart_starts_up_are_no_hang(self) -> None:
        # ACPd refuses until it listens on the new boot: minutes at most on a
        # slow start, never six.
        device = FakeAcpDevice(kernel_after=STUCK_AFTER - 60.0, down_read_error="Connection refused", mac_router=ROUTER)
        recorder = RecordingCallbacks()
        recorder.clock = device.monotonic
        with device.patched():
            reboot_device("root@10.0.0.2", "pw", wait=True, callbacks=recorder.callbacks())

        self.assertNotIn("acpd_hang_evidence", recorder.measurement("reboot_cycle"))
        self.assertEqual(recorder.progress, [])

    def test_a_break_in_the_refusals_starts_the_count_again(self) -> None:
        # Refused, then silent for a while, then refused again: no run of six
        # minutes before the limit, so no hang.
        device = stuck_device(down_read_error="Connection refused", down_read_error_changes=(
            (200.0, "timed out"), (300.0, "Connection refused"),
        ), ssh_port_error="Connection refused")
        recorder = RecordingCallbacks()
        recorder.clock = device.monotonic
        with device.patched(), self.assertRaises(RebootFlowError) as caught:
            reboot_device("root@10.0.0.2", "pw", wait=True, callbacks=recorder.callbacks())

        self.assertEqual(caught.exception.code, "reboot_not_finished")
        self.assertNotIn("acpd_hang_evidence", recorder.measurement("reboot_cycle"))


    def wait(self, device: FakeAcpDevice, **kwargs):
        recorder = RecordingCallbacks()
        recorder.clock = device.monotonic
        start = device.now
        with device.patched():
            try:
                reboot_device("root@10.0.0.2", "pw", wait=True, callbacks=recorder.callbacks(), **kwargs)
                error = None
            except RebootFlowError as exc:
                error = exc
        return error, recorder, start

    def test_refusals_that_give_way_to_silence_end_as_a_silent_restart(self) -> None:
        # ACP refused long enough to ask for the unplug, then nothing answers:
        # the restart started after all, or the device was unplugged and
        # never came back. The wait withdraws the hang and ends as any silent
        # restart, not with the refused message.
        silent_at = STUCK_AFTER + 60.0
        device = stuck_device(down_read_error="Connection refused", down_read_error_changes=((silent_at, "timed out"),))
        error, recorder, start = self.wait(device)

        self.assertEqual(error.code, "restart_stuck")
        self.assertNotIn(reboot_service.ACPD_REFUSED_MESSAGE, error.message)
        hints = [fields for _at, stage, fields in recorder.progress if stage == "wait_for_reboot_up"]
        self.assertEqual(hints, [hint_state(acpd_hung=True), hint_state(), hint_state(restart_stuck=True)])
        cycle = recorder.measurement("reboot_cycle")
        self.assertEqual(cycle["acpd_hang_evidence"], "acp_refused")
        # Withdrawn once STUCK_QUIET_READS reads in a row went unanswered.
        self.assertGreaterEqual(
            cycle["acpd_hang_cleared_after_sec"] - (silent_at + 1.0),
            (reboot_service.STUCK_QUIET_READS - 1) * reboot_service.REBOOT_POLL_SECONDS,
        )
        self.assertLess(cycle["acpd_hang_cleared_after_sec"], cycle["stuck_hint_after_sec"] + 0.001)
        # The limit counts from the stuck hint, the latest request to unplug.
        self.assertGreaterEqual(device.now - start, cycle["stuck_hint_after_sec"] + UP_LIMIT)

    def test_without_the_stuck_hint_the_silent_ending_only_conditionally_asks_for_an_unplug(self) -> None:
        # No router to tell this network from another: the ordinary silent
        # ending, counted from the first request to unplug.
        device = stuck_device(
            mac_router=None, down_read_error="Connection refused",
            down_read_error_changes=((STUCK_AFTER + 60.0, "timed out"),),
        )
        error, recorder, start = self.wait(device, up_timeout_message="Timed out waiting for SSH after reboot.")

        self.assertEqual(error.code, "reboot_not_finished")
        self.assertEqual(error.message, f"Timed out waiting for SSH after reboot. {unplug_if_on_network_note(None)}")
        cycle = recorder.measurement("reboot_cycle")
        self.assertEqual(cycle["result"], "did_not_come_back_up")
        waited = device.now - start
        self.assertGreaterEqual(waited, cycle["acpd_hang_seen_after_sec"] + UP_LIMIT)
        self.assertLess(waited, cycle["acpd_hang_seen_after_sec"] + UP_LIMIT + 2 * reboot_service.REBOOT_POLL_SECONDS)

    def test_a_few_unanswered_reads_among_the_refusals_keep_the_hang(self) -> None:
        # Fewer than STUCK_QUIET_READS unanswered reads, then refused again
        # through the limit: still ACPd that never came back.
        blip = STUCK_AFTER + 60.0
        device = stuck_device(down_read_error="Connection refused", down_read_error_changes=(
            (blip, "timed out"), (blip + (reboot_service.STUCK_QUIET_READS - 1) * 5.0, "Connection refused"),
        ))
        error, recorder, _start = self.wait(device)

        self.assertEqual((error.code, error.message), ("acpd_broken", reboot_service.ACPD_REFUSED_MESSAGE))
        self.assertEqual(recorder.measurement("reboot_cycle")["read_errors"]["timeout"]["count"],
                         reboot_service.STUCK_QUIET_READS - 1)
        self.assertNotIn("acpd_hang_cleared_after_sec", recorder.measurement("reboot_cycle"))

    def test_refusals_and_silence_in_turns_cannot_hold_the_wait_open(self) -> None:
        # Refused six minutes, silent a while, refused six minutes again: the
        # second hang asks again, but the limit still counts from the first.
        first_silence = STUCK_AFTER + 60.0
        device = stuck_device(mac_router=None, down_read_error="Connection refused", down_read_error_changes=(
            (first_silence, "timed out"), (first_silence + 60.0, "Connection refused"),
        ))
        error, recorder, start = self.wait(device)

        hang_seen = [at - start for at, _stage, fields in recorder.progress if fields["acpd_hung"]]
        self.assertEqual(len(hang_seen), 2)
        self.assertEqual(error.code, "acpd_broken")
        self.assertLess(device.now - start, hang_seen[0] + UP_LIMIT + 2 * reboot_service.REBOOT_POLL_SECONDS)
        self.assertEqual(recorder.messages.count(reboot_service.ACPD_REFUSED_UNPLUG_MESSAGE), 2)

    def test_the_marker_hang_stays_through_silence(self) -> None:
        # The marker look saw the old kernel alive: reads that go unanswered
        # afterwards are that kernel's ACPd gone, not a restart.
        device = FakeAcpDevice(acpd_hangs=True)
        error, recorder, _start = self.wait(device, ssh=SshConnection("root@10.0.0.2", "pw", ""))

        self.assertEqual(error.code, "acpd_broken")
        self.assertNotIn("acpd_hang_cleared_after_sec", recorder.measurement("reboot_cycle"))


class DeviceSilenceTests(unittest.TestCase):
    """The restart wait's record of whether nothing answers at the device's
    address lately: answering, quiet, or (for a process macOS may hold, such
    as the app) maybe blocked."""

    def feed(self, held: bool, events) -> reboot_service.DeviceSilence:
        silence = reboot_service.DeviceSilence(held=held)
        for event in events:
            if event[0] == "ssh":
                silence.observe_ssh_port(event[1])
            else:
                silence.observe_read(event[0], f"error {event[0]}")
        return silence

    def test_each_read_kind_and_ssh_port_moves_the_record(self) -> None:
        three = lambda kind: [(kind,)] * 3  # noqa: E731
        cases = (
            # (name, held, events, quiet, maybe_blocked)
            ("timeouts", True, three("timeout"), True, False),
            ("host down", True, three("host_down"), True, False),
            ("a name that no longer resolves", True, three("name_lookup_failed"), True, False),
            ("this computer's network is down", True, three("network_unreachable"), True, False),
            ("each address silent its own way", True, three("timeout+host_down"), True, False),
            # Failures that say nothing about the device are unclear: neither
            # silence nor an answer. One leaves the count as it was; a run of
            # them ends the silence.
            ("an address this computer cannot use", True, three("address_unavailable"), False, False),
            ("a firewall on this computer", True, three("not_permitted"), False, False),
            ("an unknown failure", True, three("other"), False, False),
            ("an unknown failure where nothing holds it", False, three("other"), False, False),
            # Silence takes STUCK_QUIET_READS unanswered reads in a row: an
            # unknown failure before then starts the count again.
            ("one unknown failure starts the count again", True,
             [("timeout",), ("timeout",), ("other",), ("timeout",)], False, False),
            ("then an unbroken run", True, [("timeout",), ("timeout",), ("other",), *three("timeout")], True, False),
            ("one unknown failure after silence", True, [*three("timeout"), ("other",)], True, False),
            ("two after silence", True, [*three("timeout"), ("other",), ("other",)], True, False),
            ("unknown failures in a row end the silence", True, [*three("timeout"), *three("other")], False, False),
            # A run of unknown failures ended the silence, so one unanswered
            # read after it is not silence again: it takes a new run.
            ("one unanswered read after a run of unknown failures", True,
             [*three("timeout"), *three("other"), ("timeout",)], False, False),
            ("a long run of unknown failures, then one unanswered read", True,
             [*three("timeout"), *[("other",)] * 20, ("timeout",)], False, False),
            ("a new run after the unknown failures", True,
             [*three("timeout"), *three("other"), *three("timeout")], True, False),
            ("a held no route run, then one unanswered read", True,
             [*three("timeout"), *[("no_route",)] * 10, ("timeout",)], False, False),
            ("a held no route run, then a new run", True,
             [*three("timeout"), *[("no_route",)] * 10, *three("timeout")], True, False),
            ("a stray held no route inside the first run", True,
             [("timeout",), ("no_route",), ("timeout",), ("timeout",)], False, False),
            ("unclear reads that alternate with unanswered ones never make silence", True,
             [("timeout",), ("other",)] * 10, False, False),
            ("nor end a silence already seen", True, [*three("timeout"), *[("other",), ("timeout",)] * 10], True, False),
            ("silence after an unknown failure", True, [("other",), *three("timeout")], True, False),
            ("one address silent, one unknown", True, three("timeout+other"), False, False),
            ("one address refused", True, three("timeout+refused"), False, False),
            ("unknown after no route ends the maybe-block", True, [*three("no_route"), ("other",)], False, False),
            ("two reads are not enough", True, [("timeout",)] * 2, False, False),
            ("a refusal starts over", True, [("timeout",), ("timeout",), ("refused",), ("timeout",)], False, False),
            ("a rejected password starts over", True, [*three("timeout"), ("auth",)], False, False),
            ("a reset connection starts over", True, [*three("timeout"), ("reset",)], False, False),
            ("another AirPort at the address", True, [*three("timeout"), ("other_device",)], False, False),
            ("no route where macOS may hold this process", True, three("no_route"), False, True),
            ("no route where nothing holds it", False, three("no_route"), True, False),
            ("no route then a timeout", True, [*three("no_route"), ("timeout",)], False, False),
            # The stray no route a down device gives now and then (9b5781b4).
            ("one no route where macOS may hold this process", True, [*three("timeout"), ("no_route",)], True, False),
            ("no route in a row after silence", True, [*three("timeout"), *three("no_route")], False, True),
            ("no route not in a row", True, [("no_route",), ("no_route",), ("other",), ("no_route",)], False, False),
            ("an open SSH port", True, [*three("timeout"), ("ssh", None)], False, False),
            ("a refused SSH port", True, [*three("timeout"), ("ssh", "Connection refused")], False, False),
            # An answer at an earlier look does not outlast later silence.
            ("SSH answered, then silent again", True,
             [*three("timeout"), ("ssh", None), *three("timeout")], True, False),
            ("SSH silent at its next look", True,
             [*three("timeout"), ("ssh", None), *three("timeout"), ("ssh", "timed out")], True, False),
            ("one SSH no route where macOS may hold this process", True,
             [*three("timeout"), ("ssh", "No route to host")], True, False),
            ("SSH no route where nothing holds it", False, [*three("timeout"), ("ssh", "No route to host")], True, False),
        )
        for name, held, events, quiet, blocked in cases:
            with self.subTest(name):
                silence = self.feed(held, events)
                self.assertEqual((silence.quiet, silence.maybe_blocked), (quiet, blocked))


    def test_a_long_block_keeps_only_the_last_no_route_errors(self) -> None:
        # A blocked app gets no route for the whole wait: the block check
        # needs only the latest errors, not one per poll.
        silence = reboot_service.DeviceSilence(held=True)
        for index in range(200):
            silence.observe_read("no_route", f"no route {index}")
        self.assertTrue(silence.maybe_blocked)
        self.assertEqual(silence.no_route_errors, ["no route 197", "no route 198", "no route 199"])


class FakeAcpSocket:
    """A connected socket to ACP's port for integrations.acp, failing as told."""

    def __init__(self, *, send_error: OSError | None = None, recv_error: OSError | None = None) -> None:
        self.send_error = send_error
        self.recv_error = recv_error

    def settimeout(self, _timeout: float) -> None:
        pass

    def sendall(self, _data: bytes) -> None:
        if self.send_error is not None:
            raise self.send_error

    def recv(self, _size: int) -> bytes:
        if self.recv_error is not None:
            raise self.recv_error
        # The other end closed the connection in order.
        return b""

    def close(self) -> None:
        pass


class ReadErrorKindTests(unittest.TestCase):
    def read_kind(self, connect) -> str:
        """The kind of the restart wait's uptime read, through integrations.acp
        over a socket whose connect does `connect`."""
        import socket

        with mock.patch.object(socket, "create_connection", side_effect=connect):
            uptime, _mac, error = reboot_service._read_uptime("10.0.0.2", "pw")
        self.assertIsNone(uptime)
        return reboot_service._read_error_kind(error)

    def connected(self, **failures):
        return lambda *_args, **_kwargs: FakeAcpSocket(**failures)

    @staticmethod
    def not_connected(error: OSError):
        def connect(*_args, **_kwargs):
            raise error
        return connect

    def test_a_connection_the_other_end_reset_or_closed_is_an_answer(self) -> None:
        # ACPd closing a connection it had taken, as it stops or starts: the
        # device is there, so it is not silence.
        for name, connect in (
            ("reset", self.connected(recv_error=ConnectionResetError(54, "Connection reset by peer"))),
            ("broken pipe", self.connected(send_error=BrokenPipeError(32, "Broken pipe"))),
            ("closed in order", self.connected()),
        ):
            with self.subTest(name):
                kind = self.read_kind(connect)
                self.assertIn(kind, reboot_service._ANSWER_KINDS)
                silence = reboot_service.DeviceSilence(held=False)
                for _ in range(reboot_service.STUCK_QUIET_READS):
                    silence.observe_read("timeout", "timed out")
                silence.observe_read(kind, "")
                self.assertFalse(silence.quiet)

    def test_a_connection_made_that_gets_no_reply_is_not_silence(self) -> None:
        # The device's TCP stack took the connection, so something is there,
        # but nothing replied: unclear, neither silence nor an answer.
        import socket

        for name, connect in (
            ("receive timed out", self.connected(recv_error=socket.timeout("timed out"))),
            ("send timed out", self.connected(send_error=socket.timeout("timed out"))),
            ("receive failed otherwise", self.connected(recv_error=OSError(64, "Host is down"))),
        ):
            with self.subTest(name):
                kind = self.read_kind(connect)
                self.assertEqual(kind, "no_reply")
                self.assertNotIn(kind, reboot_service._ANSWER_KINDS)
                self.assertNotIn(kind, reboot_service._SILENT_KINDS)
                silence = reboot_service.DeviceSilence(held=False)
                for _ in range(reboot_service.STUCK_QUIET_READS):
                    silence.observe_read(kind, "")
                self.assertFalse(silence.quiet)

    def test_a_connect_that_fails_is_judged_by_how(self) -> None:
        import socket

        for error, kind in (
            (socket.timeout("timed out"), "timeout"),
            (OSError(64, "Host is down"), "host_down"),
            (OSError(65, "No route to host"), "no_route"),
            (ConnectionRefusedError(61, "Connection refused"), "refused"),
            (ConnectionResetError(54, "Connection reset by peer"), "reset"),
            (OSError(1, "Operation not permitted"), "not_permitted"),
            (OSError(99, "something odd"), "other"),
        ):
            with self.subTest(kind):
                self.assertEqual(self.read_kind(self.not_connected(error)), kind)


class ClientAwayMessageTests(unittest.TestCase):
    def test_off_macos_the_messages_name_this_computer(self) -> None:
        # The detection works wherever the CLI runs.
        import ipaddress

        joined = (ipaddress.ip_network("192.168.50.0/24"),)
        self.assertTrue(reboot_service.client_away_message(joined, None, platform="linux").startswith("This computer left"))
        self.assertTrue(reboot_service.client_left_message(joined, platform="linux").startswith("This computer is on"))
        self.assertEqual(reboot_service.client_back_message(platform="linux"), "This computer is back on the device's network.")
        self.assertTrue(reboot_service.client_away_message(joined, None, platform="darwin").startswith("This Mac left"))

    def test_a_public_network_is_named_by_family_and_size_only(self) -> None:
        import ipaddress

        message = reboot_service.client_left_message((ipaddress.ip_network("8.8.8.0/24"),))
        self.assertIn("ipv4-public/24", message)
        self.assertNotIn("8.8.8", message)
