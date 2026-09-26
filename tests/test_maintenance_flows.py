"""The fsck, uninstall and activation steps the CLI and the app share."""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from timecapsulesmb.deploy.verify import VerificationResult
from timecapsulesmb.device.errors import DeviceError
from timecapsulesmb.device.storage import UNINSTALL_DRY_RUN_VOLUME_ROOT_PLACEHOLDER
from timecapsulesmb.services.activation import activate_runtime
from timecapsulesmb.services.callbacks import OperationCallbacks
from timecapsulesmb.services.maintenance import (
    FSCK_DID_NOT_RUN_MESSAGE,
    FSCK_NOT_UNMOUNTED_LINE,
    FSCK_NOT_UNMOUNTED_MESSAGE,
    FSCK_REBOOT_NO_DOWN_MESSAGE,
    FSCK_REMOTE_COMMAND_TIMEOUT_SECONDS,
    UNINSTALL_FILES_REMAIN_MESSAGE,
    UNINSTALL_REBOOT_NO_DOWN_MESSAGE,
    FsckTarget,
    prepare_uninstall,
    reboot_after_uninstall,
    run_fsck,
)
from timecapsulesmb.services.reboot import RebootFlowError
from timecapsulesmb.transport.ssh import SshConnection


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
    def run_fsck(self, stdout: str, *, reboot: bool = True, wait: bool = True, returncode: int = 0, observe_error=None):
        recorder = RecordingCallbacks()
        proc = SimpleNamespace(stdout=stdout, returncode=returncode)
        with mock.patch("timecapsulesmb.services.maintenance.run_ssh", return_value=proc) as run_ssh:
            with mock.patch("timecapsulesmb.services.maintenance.observe_reboot_cycle", side_effect=observe_error) as observe:
                try:
                    outcome = run_fsck(CONNECTION, FSCK_TARGET, reboot=reboot, wait=wait, callbacks=recorder.callbacks)
                except RebootFlowError as exc:
                    outcome = exc
        return outcome, recorder, run_ssh, observe

    def test_clean_fsck_reboots_and_waits(self) -> None:
        outcome, recorder, run_ssh, observe = self.run_fsck("--- fsck_hfs /dev/dk2 ---\nOK\ntcapsule-fsck: fsck_hfs exit status 0\n")

        self.assertEqual((outcome.status, outcome.failure, outcome.reboot_requested, outcome.waited), (0, None, True, True))
        self.assertEqual(recorder.stages, ["run_fsck"])
        self.assertEqual(recorder.messages, ["--- fsck_hfs /dev/dk2 ---", "OK", "tcapsule-fsck: fsck_hfs exit status 0"])
        self.assertEqual(recorder.fields, {"returncode": 0, "reboot_was_attempted": True})
        self.assertEqual(run_ssh.call_args.kwargs, {"check": False, "timeout": FSCK_REMOTE_COMMAND_TIMEOUT_SECONDS})
        self.assertIn("/sbin/reboot", run_ssh.call_args.args[1])
        observe.assert_called_once()
        self.assertEqual(observe.call_args.kwargs["down_timeout_seconds"], 90)
        self.assertEqual(observe.call_args.kwargs["up_timeout_seconds"], 420)
        self.assertEqual(observe.call_args.kwargs["reboot_no_down_message"], FSCK_REBOOT_NO_DOWN_MESSAGE)

    def test_failed_fsck_still_reboots_and_reports_the_failure(self) -> None:
        outcome, recorder, _run_ssh, observe = self.run_fsck("tcapsule-fsck: fsck_hfs exit status 8\n")

        self.assertEqual(outcome.status, 8)
        self.assertEqual(outcome.failure, "fsck_hfs exited with status 8; the disk may still need repair.")
        self.assertTrue(outcome.waited)
        self.assertEqual(recorder.fields["returncode"], 8)
        observe.assert_called_once()

    def test_no_reboot_leaves_reboot_out_of_the_script_and_does_not_wait(self) -> None:
        outcome, recorder, run_ssh, observe = self.run_fsck("tcapsule-fsck: fsck_hfs exit status 0\n", reboot=False)

        self.assertEqual((outcome.reboot_requested, outcome.waited), (False, False))
        self.assertNotIn("/sbin/reboot", run_ssh.call_args.args[1])
        self.assertNotIn("reboot_was_attempted", recorder.fields)
        observe.assert_not_called()

    def test_no_wait_reports_the_reboot_without_observing_it(self) -> None:
        outcome, recorder, _run_ssh, observe = self.run_fsck("tcapsule-fsck: fsck_hfs exit status 0\n", wait=False)

        self.assertEqual((outcome.reboot_requested, outcome.waited), (True, False))
        self.assertTrue(recorder.fields["reboot_was_attempted"])
        observe.assert_not_called()

    def test_fsck_that_never_ran_skips_the_reboot_wait_and_keeps_the_ssh_status(self) -> None:
        outcome, recorder, _run_ssh, observe = self.run_fsck("stopping file sharing failed\n", returncode=1)

        self.assertIsNone(outcome.status)
        self.assertEqual(outcome.failure, FSCK_DID_NOT_RUN_MESSAGE)
        self.assertEqual((outcome.reboot_requested, outcome.waited), (False, False))
        self.assertEqual(recorder.fields, {"returncode": 1})
        observe.assert_not_called()

    def test_volume_still_mounted_explains_why_fsck_did_not_run(self) -> None:
        outcome, _recorder, _run_ssh, observe = self.run_fsck(f"{FSCK_NOT_UNMOUNTED_LINE}\n", returncode=1)

        self.assertIsNone(outcome.status)
        self.assertEqual(outcome.failure, FSCK_NOT_UNMOUNTED_MESSAGE)
        observe.assert_not_called()

    def test_reboot_wait_failure_propagates(self) -> None:
        error = RebootFlowError(FSCK_REBOOT_NO_DOWN_MESSAGE, "did_not_go_down")
        outcome, recorder, _run_ssh, _observe = self.run_fsck("tcapsule-fsck: fsck_hfs exit status 0\n", observe_error=error)

        self.assertIs(outcome, error)
        self.assertTrue(recorder.fields["reboot_was_attempted"])


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
        with mock.patch("timecapsulesmb.services.maintenance.request_reboot", side_effect=reboot_error) as request:
            with mock.patch("timecapsulesmb.services.maintenance.request_reboot_and_wait", side_effect=reboot_error) as request_and_wait:
                with mock.patch("timecapsulesmb.services.maintenance.verify_post_uninstall", return_value=verification) as verify:
                    try:
                        result = reboot_after_uninstall(CONNECTION, plan, callbacks=recorder.callbacks)
                    except (RebootFlowError, DeviceError) as exc:
                        result = exc
        return result, recorder, request, request_and_wait, verify

    def test_no_reboot_does_nothing(self) -> None:
        plan, _recorder, _mount = self.prepare(reboot=False)
        result, _recorder, request, request_and_wait, verify = self.reboot(plan)

        self.assertIs(result, False)
        request.assert_not_called()
        request_and_wait.assert_not_called()
        verify.assert_not_called()

    def test_no_wait_requests_the_reboot_and_skips_verification(self) -> None:
        plan, _recorder, _mount = self.prepare(wait=False)
        result, _recorder, request, request_and_wait, verify = self.reboot(plan)

        self.assertIs(result, False)
        self.assertEqual(request.call_args.kwargs["strategy"], "acp_then_ssh")
        self.assertTrue(request.call_args.kwargs["raise_on_request_error"])
        request_and_wait.assert_not_called()
        verify.assert_not_called()

    def test_waited_reboot_verifies_the_removal(self) -> None:
        plan, _recorder, _mount = self.prepare()
        verification = VerificationResult(ok=True, lines=("PASS:/Volumes/dk2/.samba4 absent",))
        result, recorder, request, request_and_wait, verify = self.reboot(plan, verification=verification)

        self.assertIs(result, True)
        request.assert_not_called()
        self.assertEqual(request_and_wait.call_args.kwargs["strategy"], "acp_then_ssh")
        self.assertEqual(request_and_wait.call_args.kwargs["reboot_no_down_message"], UNINSTALL_REBOOT_NO_DOWN_MESSAGE)
        verify.assert_called_once_with(CONNECTION, plan)
        self.assertEqual(recorder.stages, ["verify_post_uninstall"])
        self.assertTrue(recorder.messages)

    def test_files_left_after_the_reboot_fail_the_uninstall(self) -> None:
        plan, _recorder, _mount = self.prepare()
        result, _recorder, _request, _request_and_wait, _verify = self.reboot(
            plan, verification=VerificationResult(ok=False, lines=("FAIL:/mnt/Flash/rc.local present",)),
        )

        self.assertIsInstance(result, DeviceError)
        self.assertEqual(str(result), UNINSTALL_FILES_REMAIN_MESSAGE)

    def test_failed_reboot_skips_verification(self) -> None:
        plan, _recorder, _mount = self.prepare()
        error = RebootFlowError(UNINSTALL_REBOOT_NO_DOWN_MESSAGE, "did_not_go_down")
        result, _recorder, _request, _request_and_wait, verify = self.reboot(plan, reboot_error=error)

        self.assertIs(result, error)
        verify.assert_not_called()


class ActivateRuntimeTests(unittest.TestCase):
    ACTIONS = [SimpleNamespace(name="start runtime")]

    def activate(self, *, ready: bool, verify_error=None):
        recorder = RecordingCallbacks()
        runtime = SimpleNamespace(ready=ready, detail="managed runtime is ready" if ready else "managed runtime is not ready")
        with mock.patch("timecapsulesmb.services.activation.probe_managed_runtime_conn", return_value=runtime):
            with mock.patch("timecapsulesmb.services.activation.run_remote_actions") as run_actions:
                with mock.patch("timecapsulesmb.services.activation.wait_for_activation_settle") as settle:
                    with mock.patch(
                        "timecapsulesmb.services.activation.verify_managed_runtime_ready", side_effect=verify_error,
                    ) as verify:
                        try:
                            result = activate_runtime(CONNECTION, self.ACTIONS, callbacks=recorder.callbacks)
                        except DeviceError as exc:
                            result = exc
        return result, recorder, run_actions, settle, verify

    def test_running_runtime_is_left_alone(self) -> None:
        decision, recorder, run_actions, settle, verify = self.activate(ready=True)

        self.assertFalse(decision.run_actions)
        self.assertEqual(recorder.stages, ["probe_runtime"])
        self.assertEqual(recorder.messages, ["managed runtime is ready"])
        self.assertEqual(recorder.debug, {"activation_decision": "runtime_already_ready", "manual_activation_required": False})
        run_actions.assert_not_called()
        settle.assert_not_called()
        verify.assert_not_called()

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
