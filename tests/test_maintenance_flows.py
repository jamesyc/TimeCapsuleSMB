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
    OLDEST_ACTIVATABLE_RELEASE_TAG,
    OLDEST_ACTIVATABLE_VERSION_CODE,
    ActivationInstallError,
    activate_runtime,
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
from timecapsulesmb.transport.ssh import SshConnection
from tests.reboot_support import FakeAcpDevice


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


class RunFsckTests(unittest.TestCase):
    """run_fsck against a fake SSH session and a simulated device.

    Only the transport is faked: the SSH session and the device's ACP, so the
    reboot decisions run as in production.
    """

    def run_fsck(self, stdout: str, *, reboot: bool = True, wait: bool = True, returncode: int = 0,
                 device: FakeAcpDevice | None = None):
        recorder = RecordingCallbacks()
        device = device or FakeAcpDevice()
        proc = SimpleNamespace(stdout=stdout, returncode=returncode)

        def remote(*_args, **_kwargs):
            device.calls.append("run_ssh")
            return proc

        with mock.patch("timecapsulesmb.services.maintenance.run_ssh", side_effect=remote) as run_ssh:
            with device.patched():
                try:
                    outcome = run_fsck(CONNECTION, FSCK_TARGET, reboot=reboot, wait=wait, callbacks=recorder.callbacks)
                except RebootFlowError as exc:
                    outcome = exc
        return outcome, recorder, run_ssh, device

    def test_clean_fsck_then_requests_the_reboot_and_waits(self) -> None:
        outcome, recorder, run_ssh, device = self.run_fsck(
            "--- fsck_hfs /dev/dk2 ---\nOK\ntcapsule-fsck: fsck_hfs exit status 0\n")

        self.assertEqual((outcome.status, outcome.failure, outcome.reboot_requested, outcome.waited), (0, None, True, True))
        self.assertEqual(recorder.stages, ["run_fsck", "reboot", "wait_for_reboot_down", "wait_for_reboot_up"])
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
                        with mock.patch("timecapsulesmb.services.activation.wait_for_activation_settle") as settle:
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
        settle.assert_called_once()
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
