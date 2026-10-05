"""The fsck, uninstall and activation steps the CLI and the app share."""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from timecapsulesmb.deploy.verify import VerificationResult
from timecapsulesmb.device.errors import DeviceError
from timecapsulesmb.device.storage import UNINSTALL_DRY_RUN_VOLUME_ROOT_PLACEHOLDER
from timecapsulesmb.core.release import CLI_VERSION_CODE, RELEASE_TAG, release_major
from timecapsulesmb.device.probe import DeployedVersionProbeResult
from timecapsulesmb.services.activation import (
    MANUAL_START_AFTER_REBOOT_MESSAGE,
    OLDEST_ACTIVATABLE_RELEASE_TAG,
    OLDEST_ACTIVATABLE_VERSION_CODE,
    RUNTIME_RESTART_FAILURE_MESSAGE,
    ActivationInstallError,
    activate_runtime,
    installed_netbsd4_autostart,
)
from timecapsulesmb.services.callbacks import OperationCallbacks
from timecapsulesmb.services.maintenance import (
    FSCK_DID_NOT_RUN_MESSAGE,
    FSCK_NOT_UNMOUNTED_LINE,
    FSCK_NOT_UNMOUNTED_MESSAGE,
    FSCK_REMOTE_COMMAND_TIMEOUT_SECONDS,
    UNINSTALL_FILES_REMAIN_MESSAGE,
    UNINSTALL_REBOOT_NO_DOWN_MESSAGE,
    FsckTarget,
    prepare_uninstall,
    reboot_after_uninstall,
    run_fsck,
)
from timecapsulesmb.integrations.acp import ACPConnectionError
from timecapsulesmb.services.reboot import REBOOT_NO_DOWN_MESSAGE, REBOOT_UP_TIMEOUT_MESSAGE, RebootFlowError
from timecapsulesmb.transport.errors import SshNetworkError
from timecapsulesmb.transport.ssh import SshConnection
from tests.reboot_support import FakeAcpDevice, FakeInstalledRuntime


class RecordingCallbacks:
    def __init__(self) -> None:
        self.stages: list[str] = []
        self.messages: list[str] = []
        self.fields: dict[str, object] = {}
        self.debug: dict[str, object] = {}
        self.callbacks = OperationCallbacks(
            set_stage=self.stages.append,
            log=self.messages.append,
            update_fields=self.fields.update,
            add_debug_fields=self.debug.update,
        )


CONNECTION = SshConnection("root@10.0.0.2", "pw", "-o foo")
FSCK_TARGET = FsckTarget(device="/dev/dk2", mountpoint="/Volumes/dk2", name="Data", builtin=True)


class FsckHarness:
    """run_fsck against a fake SSH session and a simulated device.

    Only the transport is faked: the SSH session and the device's ACP, so the
    reboot decisions run as in production.
    """

    def run_fsck(self, stdout: str, *, reboot: bool = True, wait: bool = True, returncode: int = 0,
                 device: FakeAcpDevice | None = None, runtime: FakeInstalledRuntime | None = None,
                 netbsd4_autostart: bool | None = None):
        recorder = RecordingCallbacks()
        device = device or FakeAcpDevice()
        runtime = runtime or FakeInstalledRuntime()
        proc = SimpleNamespace(stdout=stdout, returncode=returncode)

        def remote(*_args, **_kwargs):
            device.calls.append("run_ssh")
            return proc

        with mock.patch("timecapsulesmb.services.maintenance.run_ssh", side_effect=remote) as run_ssh:
            with device.patched(), runtime.patched():
                try:
                    outcome = run_fsck(
                        CONNECTION, FSCK_TARGET, reboot=reboot, wait=wait, callbacks=recorder.callbacks,
                        netbsd4_autostart=netbsd4_autostart,
                    )
                except RebootFlowError as exc:
                    outcome = exc
        return outcome, recorder, run_ssh, device


class RunFsckTests(FsckHarness, unittest.TestCase):
    def test_clean_fsck_then_requests_the_reboot_and_waits(self) -> None:
        outcome, recorder, run_ssh, device = self.run_fsck(
            "--- fsck_hfs /dev/dk2 ---\nOK\ntcapsule-fsck: fsck_hfs exit status 0\n")

        self.assertEqual((outcome.status, outcome.failure, outcome.reboot_requested, outcome.waited), (0, None, True, True))
        # Nothing to start afterwards (NetBSD6 starts file sharing at boot), so
        # nothing is asked of the device once SSH is back.
        self.assertEqual(recorder.stages, ["run_fsck", "reboot", "wait_for_reboot_down", "wait_for_reboot_up"])
        self.assertIsNone(outcome.runtime_restart_error)
        self.assertNotIn("runtime_restarted", recorder.fields)
        self.assertEqual(recorder.messages[:3], ["--- fsck_hfs /dev/dk2 ---", "OK", "tcapsule-fsck: fsck_hfs exit status 0"])
        self.assertEqual(recorder.fields["returncode"], 0)
        self.assertTrue(recorder.fields["reboot_was_attempted"])
        self.assertTrue(recorder.fields["device_came_back_after_reboot"])
        self.assertEqual(run_ssh.call_args.kwargs, {"check": False, "timeout": FSCK_REMOTE_COMMAND_TIMEOUT_SECONDS})
        # The reboot is one ACP request from the host, after fsck has reported.
        self.assertEqual(device.calls[:4], ["run_ssh", "read", "sleep 1", "request"])
        self.assertEqual(device.calls.count("request"), 1)
        self.assertTrue(device.served_new_boot)

    def test_reboot_gets_two_minutes_to_start_and_seven_to_return(self) -> None:
        for kernel_after, ok in ((110.0, True), (10_000.0, False)):
            with self.subTest(kernel_after=kernel_after):
                device = FakeAcpDevice(shutdown_after=100.0, kernel_after=kernel_after)
                outcome, _recorder, _run_ssh, _device = self.run_fsck("tcapsule-fsck: fsck_hfs exit status 0\n", device=device)
                if ok:
                    # ACP kept answering for 100 s: past deploy's 90 s, inside fsck's 120 s.
                    self.assertTrue(outcome.waited)
                else:
                    self.assertEqual(outcome.code, "reboot_not_finished")
                    self.assertGreaterEqual(device.now - 1000.0, 100 + 420)

    def test_failed_fsck_still_reboots_and_reports_the_failure(self) -> None:
        outcome, recorder, _run_ssh, device = self.run_fsck("tcapsule-fsck: fsck_hfs exit status 8\n")

        self.assertEqual(outcome.status, 8)
        self.assertEqual(outcome.failure, "fsck_hfs exited with status 8; the disk may still need repair.")
        self.assertTrue(outcome.waited)
        self.assertEqual(recorder.fields["returncode"], 8)
        self.assertEqual(device.calls.count("request"), 1)
        # The app marks the last step failed and telemetry records it: that is
        # the repair, not the reboot that followed it.
        self.assertEqual(recorder.stages[-1], "run_fsck")

    def test_no_reboot_requests_nothing_and_does_not_wait(self) -> None:
        outcome, recorder, _run_ssh, device = self.run_fsck(
            "tcapsule-fsck: fsck_hfs exit status 0\n", reboot=False)

        self.assertEqual((outcome.reboot_requested, outcome.waited), (False, False))
        self.assertNotIn("reboot_was_attempted", recorder.fields)
        self.assertEqual(device.calls, ["run_ssh"])

    def test_no_wait_requests_the_reboot_without_observing_it(self) -> None:
        outcome, recorder, _run_ssh, device = self.run_fsck(
            "tcapsule-fsck: fsck_hfs exit status 0\n", wait=False)

        self.assertEqual((outcome.reboot_requested, outcome.waited), (True, False))
        self.assertTrue(recorder.fields["reboot_was_attempted"])
        self.assertEqual(device.calls, ["run_ssh", "request"])

    def test_no_wait_reports_a_rejected_reboot_request(self) -> None:
        outcome, _recorder, _run_ssh, device = self.run_fsck(
            "tcapsule-fsck: fsck_hfs exit status 0\n", wait=False,
            device=FakeAcpDevice(request_error=ACPConnectionError("refused")))

        self.assertIsInstance(outcome, RebootFlowError)
        self.assertEqual(outcome.code, "remote_error")
        # A clean repair has nothing to add to the reboot error.
        self.assertEqual(str(outcome), "ACP reboot request failed: refused")
        self.assertEqual(device.calls, ["run_ssh", "request"])

    def test_rejected_no_wait_request_keeps_the_failed_repair(self) -> None:
        outcome, _recorder, _run_ssh, device = self.run_fsck(
            "tcapsule-fsck: fsck_hfs exit status 8\n", wait=False,
            device=FakeAcpDevice(request_error=ACPConnectionError("refused")))

        self.assertIsInstance(outcome, RebootFlowError)
        self.assertEqual(outcome.code, "remote_error")
        # The reboot error comes first; the repair failure is not lost behind it.
        self.assertEqual(
            str(outcome),
            "ACP reboot request failed: refused\n"
            "fsck_hfs exited with status 8; the disk may still need repair.",
        )
        self.assertIsInstance(outcome.__cause__, RebootFlowError)
        self.assertEqual(device.calls.count("request"), 1)

    def test_waited_request_error_is_observed_without_a_second_request(self) -> None:
        outcome, _recorder, _run_ssh, device = self.run_fsck(
            "tcapsule-fsck: fsck_hfs exit status 0\n",
            device=FakeAcpDevice(request_error=ACPConnectionError("ACP receive failed: timed out")))

        self.assertEqual(outcome.status, 0)
        self.assertTrue(outcome.waited)
        self.assertEqual(device.calls.count("request"), 1)
        self.assertTrue(device.served_new_boot)

    def test_fsck_that_never_ran_does_not_reboot_and_keeps_the_ssh_status(self) -> None:
        outcome, recorder, _run_ssh, device = self.run_fsck("stopping file sharing failed\n", returncode=1)

        self.assertIsNone(outcome.status)
        self.assertEqual(outcome.failure, FSCK_DID_NOT_RUN_MESSAGE)
        self.assertEqual((outcome.reboot_requested, outcome.waited), (False, False))
        self.assertEqual(recorder.fields, {"returncode": 1})
        self.assertEqual(device.calls, ["run_ssh"])

    def test_volume_still_mounted_explains_why_fsck_did_not_run(self) -> None:
        outcome, _recorder, _run_ssh, device = self.run_fsck(f"{FSCK_NOT_UNMOUNTED_LINE}\n", returncode=1)

        self.assertIsNone(outcome.status)
        self.assertEqual(outcome.failure, FSCK_NOT_UNMOUNTED_MESSAGE)
        self.assertNotIn("request", device.calls)

    def test_device_that_never_goes_down_fails_the_reboot(self) -> None:
        outcome, recorder, _run_ssh, device = self.run_fsck(
            "tcapsule-fsck: fsck_hfs exit status 0\n", device=FakeAcpDevice(reboots=False))

        self.assertIsInstance(outcome, RebootFlowError)
        self.assertEqual(outcome.code, "reboot_not_started")
        self.assertEqual(str(outcome), REBOOT_NO_DOWN_MESSAGE)
        self.assertTrue(recorder.fields["reboot_was_attempted"])
        self.assertEqual(device.calls.count("request"), 1)

    def test_device_that_never_goes_down_keeps_the_failed_repair(self) -> None:
        outcome, recorder, _run_ssh, _device = self.run_fsck(
            "tcapsule-fsck: fsck_hfs exit status 8\n", device=FakeAcpDevice(reboots=False))

        self.assertIsInstance(outcome, RebootFlowError)
        self.assertEqual(outcome.code, "reboot_not_started")
        self.assertEqual(
            str(outcome),
            f"{REBOOT_NO_DOWN_MESSAGE}\nfsck_hfs exited with status 8; the disk may still need repair.",
        )
        self.assertEqual(recorder.fields["returncode"], 8)

    def test_device_that_never_comes_back_keeps_the_failed_repair(self) -> None:
        outcome, _recorder, _run_ssh, _device = self.run_fsck(
            "tcapsule-fsck: fsck_hfs exit status 8\n", device=FakeAcpDevice(kernel_after=10_000))

        self.assertIsInstance(outcome, RebootFlowError)
        self.assertEqual(outcome.code, "reboot_not_finished")
        self.assertEqual(
            str(outcome),
            f"{REBOOT_UP_TIMEOUT_MESSAGE}\nfsck_hfs exited with status 8; the disk may still need repair.",
        )


class RunFsckOnNetbsd4Tests(FsckHarness, unittest.TestCase):
    """fsck's reboot stops file sharing on NetBSD4 without the autostart patch;
    run_fsck starts it again, as Apple's own reboot brings sharing back.

    `netbsd4_autostart` is what installed_netbsd4_autostart read before the
    repair; after the reboot nothing is asked, only started and waited for.
    """

    def netbsd4(self, stdout: str = "tcapsule-fsck: fsck_hfs exit status 0\n", *, autostart: bool = False,
                runtime=None, **kwargs):
        runtime = runtime or FakeInstalledRuntime()
        outcome, recorder, _run_ssh, device = self.run_fsck(
            stdout, runtime=runtime, netbsd4_autostart=autostart, **kwargs)
        return outcome, recorder, device, runtime

    def test_repair_then_reboot_then_starts_file_sharing_again(self) -> None:
        outcome, recorder, device, runtime = self.netbsd4()

        self.assertIsNone(outcome.failure)
        self.assertIsNone(outcome.runtime_restart_error)
        self.assertTrue(device.served_new_boot)
        self.assertEqual(runtime.calls, ["run /mnt/Flash/rc.local", "verify 200s"])
        self.assertEqual(recorder.stages[-3:], [
            "wait_for_reboot_up", "post_reboot_activation", "verify_runtime_activation",
        ])
        self.assertTrue(recorder.fields["runtime_restarted"])
        self.assertEqual(recorder.debug["activation_decision"], "firmware_autostart_missing")
        self.assertIn("File sharing is running again after the reboot.", recorder.messages)

    def test_firmware_autostart_only_waits_for_the_runtime(self) -> None:
        outcome, recorder, _device, runtime = self.netbsd4(autostart=True)

        self.assertIsNone(outcome.failure)
        self.assertEqual(runtime.calls, ["verify 200s"])
        self.assertEqual(recorder.debug["activation_decision"], "firmware_autostart_enabled")

    def test_runtime_that_does_not_start_fails_the_repair_after_saying_it_completed(self) -> None:
        outcome, recorder, _device, _runtime = self.netbsd4(runtime=FakeInstalledRuntime(becomes_ready=False))

        self.assertEqual(outcome.status, 0)
        error = f"{RUNTIME_RESTART_FAILURE_MESSAGE} {FakeInstalledRuntime.NOT_READY_DETAIL}"
        self.assertEqual(outcome.runtime_restart_error, error)
        self.assertEqual(outcome.failure, f"Disk repair completed. {error}")
        self.assertFalse(recorder.fields["runtime_restarted"])

    def test_lost_ssh_after_the_reboot_is_reported_not_skipped(self) -> None:
        # The device was known to need starting before the reboot, so an SSH
        # failure afterwards cannot leave file sharing silently off.
        runtime = FakeInstalledRuntime()
        with mock.patch.object(runtime, "_run_actions", side_effect=SshNetworkError("Connection timed out")):
            outcome, _recorder, _device, _runtime = self.netbsd4(runtime=runtime)

        self.assertEqual(
            outcome.failure, f"Disk repair completed. {RUNTIME_RESTART_FAILURE_MESSAGE} Connection timed out")

    def test_failed_repair_still_starts_file_sharing_and_keeps_its_own_error(self) -> None:
        outcome, _recorder, _device, runtime = self.netbsd4("tcapsule-fsck: fsck_hfs exit status 8\n")

        self.assertEqual(outcome.failure, "fsck_hfs exited with status 8; the disk may still need repair.")
        self.assertIsNone(outcome.runtime_restart_error)
        self.assertIn("run /mnt/Flash/rc.local", runtime.calls)

    def test_failed_repair_and_failed_start_report_the_repair_first(self) -> None:
        outcome, _recorder, _device, _runtime = self.netbsd4(
            "tcapsule-fsck: fsck_hfs exit status 8\n", runtime=FakeInstalledRuntime(becomes_ready=False))

        self.assertEqual(
            outcome.failure,
            "fsck_hfs exited with status 8; the disk may still need repair.\n"
            f"{RUNTIME_RESTART_FAILURE_MESSAGE} {FakeInstalledRuntime.NOT_READY_DETAIL}",
        )

    def test_failed_repair_is_reported_at_the_repair_step_after_a_good_restart(self) -> None:
        # Not at "Verify SMB Startup", which worked.
        _outcome, recorder, _device, _runtime = self.netbsd4("tcapsule-fsck: fsck_hfs exit status 8\n")

        self.assertEqual(recorder.stages[-3:], ["post_reboot_activation", "verify_runtime_activation", "run_fsck"])

    def test_failed_repair_and_failed_start_stay_at_the_start_step(self) -> None:
        # The app offers Activate for this step; the error text leads with the repair.
        _outcome, recorder, _device, _runtime = self.netbsd4(
            "tcapsule-fsck: fsck_hfs exit status 8\n", runtime=FakeInstalledRuntime(becomes_ready=False))

        self.assertEqual(recorder.stages[-1], "verify_runtime_activation")

    def test_clean_repair_ends_at_the_start(self) -> None:
        _outcome, recorder, _device, _runtime = self.netbsd4()

        self.assertEqual(recorder.stages.count("run_fsck"), 1)

    def test_no_wait_on_stock_netbsd4_says_file_sharing_will_stay_off(self) -> None:
        outcome, recorder, device, runtime = self.netbsd4(wait=False)

        self.assertIn(MANUAL_START_AFTER_REBOOT_MESSAGE, recorder.messages)
        self.assertEqual(device.calls, ["run_ssh", "request"])
        self.assertEqual(runtime.calls, [])
        self.assertIsNone(outcome.runtime_restart_error)

    def test_no_wait_says_nothing_where_file_sharing_starts_by_itself(self) -> None:
        for autostart in (True, None):
            with self.subTest(autostart=autostart):
                _outcome, recorder, _run_ssh, _device = self.run_fsck(
                    "tcapsule-fsck: fsck_hfs exit status 0\n", wait=False, netbsd4_autostart=autostart)

                self.assertNotIn(MANUAL_START_AFTER_REBOOT_MESSAGE, recorder.messages)

    def test_no_reboot_and_failed_reboots_never_start_the_runtime(self) -> None:
        cases = {
            "no reboot": dict(reboot=False),
            "never restarts": dict(device=FakeAcpDevice(reboots=False)),
            "never returns": dict(device=FakeAcpDevice(kernel_after=10_000)),
        }
        for name, kwargs in cases.items():
            with self.subTest(name):
                outcome, recorder, _device, runtime = self.netbsd4(**kwargs)

                self.assertEqual(runtime.calls, [])
                self.assertNotIn("post_reboot_activation", recorder.stages)
                if not isinstance(outcome, RebootFlowError):
                    self.assertIsNone(outcome.runtime_restart_error)

    def test_fsck_that_never_ran_never_touches_the_runtime(self) -> None:
        outcome, _recorder, _device, runtime = self.netbsd4("stopping file sharing failed\n", returncode=1)

        self.assertEqual(runtime.calls, [])
        self.assertFalse(outcome.reboot_requested)


class InstalledNetbsd4AutostartTests(unittest.TestCase):
    """What fsck learns before its reboot: does file sharing need starting after it?"""

    def plan(self, *, payload_family: str | None = "netbsd4le_samba4", autostart: bool = False,
             runtime: FakeInstalledRuntime | None = None):
        recorder = RecordingCallbacks()
        runtime = runtime or FakeInstalledRuntime()
        compatibility = None if payload_family is None else SimpleNamespace(payload_family=payload_family)
        state = SimpleNamespace(
            compatibility=compatibility,
            probe_result=SimpleNamespace(rc_local_autostart=autostart),
        )
        with runtime.patched():
            answer = installed_netbsd4_autostart(CONNECTION, state, recorder.callbacks)
        return answer, recorder, runtime

    def test_netbsd6_needs_nothing_and_is_not_asked_about_an_install(self) -> None:
        answer, recorder, runtime = self.plan(payload_family="netbsd6_samba4")

        self.assertIsNone(answer)
        self.assertEqual(runtime.calls, [])
        self.assertEqual(recorder.fields, {"runtime_start_after_reboot": "not_netbsd4"})

    def test_stock_netbsd4_with_an_install_needs_starting(self) -> None:
        for family in ("netbsd4le_samba4", "netbsd4be_samba4"):
            with self.subTest(family=family):
                answer, recorder, runtime = self.plan(payload_family=family)

                self.assertIs(answer, False)
                self.assertEqual(runtime.calls, ["read config", "read version"])
                self.assertEqual(recorder.fields["runtime_start_after_reboot"], "rc_local")

    def test_netbsd4_with_the_boot_hook_starts_by_itself(self) -> None:
        answer, recorder, _runtime = self.plan(autostart=True)

        self.assertIs(answer, True)
        self.assertEqual(recorder.fields["runtime_start_after_reboot"], "firmware_autostart")

    def test_netbsd4_without_an_install_needs_nothing_and_says_nothing(self) -> None:
        # fsck before the first deploy is normal; no install advice.
        answer, recorder, runtime = self.plan(runtime=FakeInstalledRuntime(installed=False))

        self.assertIsNone(answer)
        self.assertEqual(runtime.calls, ["read config"])
        self.assertEqual(recorder.messages, [])
        self.assertEqual(recorder.fields["runtime_start_after_reboot"], "runtime_not_installed")

    def test_netbsd4_install_this_version_cannot_start_is_left_off_with_the_reason(self) -> None:
        newer_major = (release_major(CLI_VERSION_CODE) + 1) * 10000
        cases = (
            ("v2.2.9", 20209, "runtime_outdated", "v2.2.9 is older than"),
            ("v9.0.0", newer_major, "client_outdated", "v9.0.0 is from a newer major version"),
        )
        for tag, code, reason, message in cases:
            with self.subTest(tag=tag):
                answer, recorder, _runtime = self.plan(runtime=FakeInstalledRuntime(release_tag=tag, version_code=code))

                self.assertIsNone(answer)
                self.assertEqual(recorder.fields["runtime_start_after_reboot"], reason)
                self.assertTrue(any(message in line for line in recorder.messages), recorder.messages)


class UninstallTests(unittest.TestCase):
    def prepare(self, *, dry_run: bool = False, reboot: bool = True, wait: bool = True, volume_roots=("/Volumes/dk2",)):
        recorder = RecordingCallbacks()
        volumes = [SimpleNamespace(volume_root=root) for root in volume_roots]
        with mock.patch(
            "timecapsulesmb.services.storage.mount_mast_volumes_with_diagnostics", return_value=volumes,
        ) as mount:
            plan = prepare_uninstall(
                CONNECTION, dry_run=dry_run, reboot=reboot, wait=wait, mount_wait=13, callbacks=recorder.callbacks,
            )
        return plan, recorder, mount

    def test_prepare_mounts_the_volumes_and_plans_their_payload_dirs(self) -> None:
        plan, recorder, mount = self.prepare(volume_roots=("/Volumes/dk2", "/Volumes/dk3"))

        mount.assert_called_once()
        self.assertEqual(mount.call_args.kwargs["wait_seconds"], 13)
        self.assertEqual(plan.volume_roots, ["/Volumes/dk2", "/Volumes/dk3"])
        self.assertEqual(plan.payload_dirs, ["/Volumes/dk2/.samba4", "/Volumes/dk3/.samba4"])
        self.assertEqual(recorder.fields["payload_dirs"], plan.payload_dirs)
        self.assertEqual(recorder.stages, ["build_uninstall_plan"])
        self.assertTrue(plan.reboot_required)
        self.assertTrue(plan.wait_after_reboot)

    def test_prepare_dry_run_uses_placeholders_without_touching_the_disk(self) -> None:
        plan, _recorder, mount = self.prepare(dry_run=True)

        mount.assert_not_called()
        self.assertEqual(plan.volume_roots, [UNINSTALL_DRY_RUN_VOLUME_ROOT_PLACEHOLDER])

    def test_prepare_without_volumes_still_plans_the_flash_cleanup(self) -> None:
        plan, _recorder, _mount = self.prepare(volume_roots=())

        self.assertEqual(plan.payload_dirs, [])
        self.assertIn("/mnt/Flash/rc.local", plan.verify_absent_targets)

    def test_prepare_never_waits_without_a_reboot(self) -> None:
        plan, _recorder, _mount = self.prepare(reboot=False, wait=True)

        self.assertFalse(plan.reboot_required)
        self.assertFalse(plan.wait_after_reboot)

    def reboot(self, plan, *, verification=None, reboot_error=None):
        recorder = RecordingCallbacks()
        with mock.patch("timecapsulesmb.services.maintenance.reboot_device", side_effect=reboot_error) as reboot:
            with mock.patch("timecapsulesmb.services.maintenance.verify_post_uninstall", return_value=verification) as verify:
                try:
                    result = reboot_after_uninstall(CONNECTION, plan, callbacks=recorder.callbacks)
                except (RebootFlowError, DeviceError) as exc:
                    result = exc
        return result, recorder, reboot, verify

    def test_no_reboot_does_nothing(self) -> None:
        plan, _recorder, _mount = self.prepare(reboot=False)
        result, _recorder, reboot, verify = self.reboot(plan)

        self.assertIs(result, False)
        reboot.assert_not_called()
        verify.assert_not_called()

    def test_no_wait_requests_the_reboot_and_skips_verification(self) -> None:
        plan, _recorder, _mount = self.prepare(wait=False)
        result, _recorder, reboot, verify = self.reboot(plan)

        self.assertIs(result, False)
        reboot.assert_called_once()
        self.assertEqual(reboot.call_args.args, ("root@10.0.0.2", "pw"))
        self.assertIs(reboot.call_args.kwargs["wait"], False)
        verify.assert_not_called()

    def test_waited_reboot_verifies_the_removal(self) -> None:
        plan, _recorder, _mount = self.prepare()
        verification = VerificationResult(ok=True, lines=("PASS:/Volumes/dk2/.samba4 absent",))
        result, recorder, reboot, verify = self.reboot(plan, verification=verification)

        self.assertIs(result, True)
        self.assertIs(reboot.call_args.kwargs["wait"], True)
        self.assertEqual(reboot.call_args.kwargs["no_down_message"], UNINSTALL_REBOOT_NO_DOWN_MESSAGE)
        verify.assert_called_once_with(CONNECTION, plan)
        self.assertEqual(recorder.stages, ["verify_post_uninstall"])
        self.assertTrue(recorder.messages)

    def test_files_left_after_the_reboot_fail_the_uninstall(self) -> None:
        plan, _recorder, _mount = self.prepare()
        result, _recorder, _reboot, _verify = self.reboot(
            plan, verification=VerificationResult(ok=False, lines=("FAIL:/mnt/Flash/rc.local present",)),
        )

        self.assertIsInstance(result, DeviceError)
        self.assertEqual(str(result), UNINSTALL_FILES_REMAIN_MESSAGE)

    def test_failed_reboot_skips_verification(self) -> None:
        plan, _recorder, _mount = self.prepare()
        error = RebootFlowError(UNINSTALL_REBOOT_NO_DOWN_MESSAGE, "reboot_not_started")
        result, _recorder, _reboot, verify = self.reboot(plan, reboot_error=error)

        self.assertIs(result, error)
        verify.assert_not_called()


class ActivateRuntimeTests(unittest.TestCase):
    ACTIONS = [SimpleNamespace(name="start runtime")]

    def activate(
        self,
        *,
        ready: bool,
        verify_error=None,
        config_present: bool = True,
        installed: tuple[str | None, int | None] = (RELEASE_TAG, CLI_VERSION_CODE),
    ):
        recorder = RecordingCallbacks()
        runtime = SimpleNamespace(ready=ready, detail="managed runtime is ready" if ready else "managed runtime is not ready")
        version = DeployedVersionProbeResult(installed[0], installed[1], "ok")
        with mock.patch("timecapsulesmb.services.activation.flash_runtime_config_present_conn", return_value=config_present):
            with mock.patch("timecapsulesmb.services.activation.read_deployed_version_conn", return_value=version):
                with mock.patch(
                    "timecapsulesmb.services.activation.probe_managed_runtime_conn", return_value=runtime,
                ) as probe:
                    with mock.patch("timecapsulesmb.services.activation.run_remote_actions") as run_actions:
                        with mock.patch("time.sleep") as settle:
                            with mock.patch(
                                "timecapsulesmb.services.activation.verify_managed_runtime_ready", side_effect=verify_error,
                            ) as verify:
                                try:
                                    result = activate_runtime(CONNECTION, self.ACTIONS, callbacks=recorder.callbacks)
                                except DeviceError as exc:
                                    result = exc
        self.probe = probe
        return result, recorder, run_actions, settle, verify

    def test_running_runtime_is_left_alone(self) -> None:
        decision, recorder, run_actions, settle, verify = self.activate(ready=True)

        self.assertFalse(decision.run_actions)
        self.assertEqual(recorder.stages, ["probe_runtime"])
        self.assertEqual(recorder.messages, ["managed runtime is ready"])
        self.assertEqual(recorder.debug, {
            "deployed_config_present": True,
            "deployed_release_tag": RELEASE_TAG,
            "deployed_cli_version_code": CLI_VERSION_CODE,
            "activation_decision": "runtime_already_ready",
            "manual_activation_required": False,
        })
        run_actions.assert_not_called()
        settle.assert_not_called()
        verify.assert_not_called()

    def assert_refused_before_touching_the_runtime(self, result, run_actions, settle, verify, *, code: str) -> None:
        self.assertIsInstance(result, ActivationInstallError)
        self.assertEqual(result.code, code)
        self.probe.assert_not_called()
        run_actions.assert_not_called()
        settle.assert_not_called()
        verify.assert_not_called()

    def test_device_without_an_install_is_refused_before_running_rc_local(self) -> None:
        # v3.1.1 ran the missing /mnt/Flash/rc.local and failed with a raw
        # "Can't open /mnt/Flash/rc.local" from the device shell.
        result, recorder, run_actions, settle, verify = self.activate(ready=False, config_present=False)

        self.assert_refused_before_touching_the_runtime(result, run_actions, settle, verify, code="runtime_not_installed")
        self.assertIn("not installed", str(result))
        self.assertIn("Install / Update Samba", str(result))
        self.assertEqual(recorder.debug, {"deployed_config_present": False})

    def test_install_without_version_information_is_refused_as_outdated(self) -> None:
        result, _recorder, run_actions, settle, verify = self.activate(ready=False, installed=(None, None))

        self.assert_refused_before_touching_the_runtime(result, run_actions, settle, verify, code="runtime_outdated")
        self.assertIn(f"older than {OLDEST_ACTIVATABLE_RELEASE_TAG}", str(result))

    def test_install_older_than_the_oldest_startable_release_is_refused(self) -> None:
        # v2.x and v3.0.x lack what this version verifies (v3.0.x stopped
        # Apple's mDNSResponder), so v3.1.1 waited the full 200 s and failed.
        for tag, code in (("v2.2.9", 20215), ("v3.0.0", 30000), ("v3.0.9", OLDEST_ACTIVATABLE_VERSION_CODE - 1)):
            with self.subTest(tag=tag):
                result, _recorder, run_actions, settle, verify = self.activate(ready=True, installed=(tag, code))

                self.assert_refused_before_touching_the_runtime(result, run_actions, settle, verify, code="runtime_outdated")
                self.assertIn(f"{tag} is older than {OLDEST_ACTIVATABLE_RELEASE_TAG}", str(result))
                self.assertIn("Install / Update Samba", str(result))

    def test_other_releases_of_this_major_version_are_started_and_verified(self) -> None:
        # Activation needs the same major version, not the same release: an
        # app update without a redeploy still starts the installed runtime.
        for tag, code in (("v3.1.0", OLDEST_ACTIVATABLE_VERSION_CODE), ("v3.9.0", 30900)):
            with self.subTest(tag=tag):
                decision, recorder, run_actions, _settle, verify = self.activate(ready=False, installed=(tag, code))

                self.assertTrue(decision.run_actions)
                self.assertEqual(recorder.debug["deployed_release_tag"], tag)
                run_actions.assert_called_once_with(CONNECTION, self.ACTIONS)
                verify.assert_called_once()

    def test_running_install_of_an_older_release_is_left_alone(self) -> None:
        decision, _recorder, run_actions, _settle, verify = self.activate(
            ready=True, installed=("v3.1.0", OLDEST_ACTIVATABLE_VERSION_CODE),
        )

        self.assertEqual(decision.reason, "runtime_already_ready")
        run_actions.assert_not_called()
        verify.assert_not_called()

    def test_install_from_a_newer_major_version_asks_for_an_update_of_this_tool(self) -> None:
        newer_major = (release_major(CLI_VERSION_CODE) + 1) * 10000
        result, _recorder, run_actions, settle, verify = self.activate(ready=False, installed=("v9.0.0", newer_major))

        self.assert_refused_before_touching_the_runtime(result, run_actions, settle, verify, code="client_outdated")
        self.assertIn("v9.0.0 is from a newer major version", str(result))
        self.assertIn("Update TimeCapsuleSMB", str(result))

    def test_oldest_startable_release_is_in_this_major_version(self) -> None:
        # A new major version must set its own floor; the gate compares majors
        # only above it.
        self.assertEqual(release_major(OLDEST_ACTIVATABLE_VERSION_CODE), release_major(CLI_VERSION_CODE))
        self.assertLessEqual(OLDEST_ACTIVATABLE_VERSION_CODE, CLI_VERSION_CODE)
        # The tag in the messages names the release the code gates on.
        code = OLDEST_ACTIVATABLE_VERSION_CODE
        self.assertEqual(OLDEST_ACTIVATABLE_RELEASE_TAG, f"v{code // 10000}.{code // 100 % 100}.{code % 100}")

    def test_stopped_runtime_is_started_then_verified(self) -> None:
        decision, recorder, run_actions, settle, verify = self.activate(ready=False)

        self.assertTrue(decision.run_actions)
        self.assertEqual(recorder.stages, ["probe_runtime", "run_activation"])
        self.assertEqual(recorder.debug["activation_decision"], "runtime_not_ready")
        self.assertIn("Activating NetBSD4 payload without file transfer.", recorder.messages)
        run_actions.assert_called_once_with(CONNECTION, self.ACTIONS)
        # Verification starts right away and polls; nothing sleeps a fixed time.
        settle.assert_not_called()
        self.assertEqual(verify.call_args.kwargs["stage"], "verify_runtime_activation")
        self.assertEqual(verify.call_args.kwargs["timeout_seconds"], 200)
        self.assertEqual(verify.call_args.kwargs["failure_message"], "NetBSD4 activation failed.")

    def test_runtime_that_never_becomes_ready_fails_the_activation(self) -> None:
        error = DeviceError("NetBSD4 activation failed. managed runtime is not ready")
        result, _recorder, run_actions, _settle, _verify = self.activate(ready=False, verify_error=error)

        self.assertIs(result, error)
        run_actions.assert_called_once()


if __name__ == "__main__":
    unittest.main()
