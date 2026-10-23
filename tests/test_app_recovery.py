from __future__ import annotations

import unittest

from timecapsulesmb.app import recovery
from timecapsulesmb.app.recovery import cli_steps, recovery_for
from timecapsulesmb.core.error_codes import ErrorCode


class AppRecoveryTests(unittest.TestCase):
    def test_configure_acp_port_probe_recovery_warns_about_vpns(self) -> None:
        recovery = recovery_for("configure", "remote_error", stage="acp_port_probe")

        self.assertEqual(recovery["title"], "AirPort not reachable at this address")
        self.assertEqual(recovery["localization_key"], "configure.remote_error.acp_port_probe")
        self.assertEqual(recovery["retryable"], True)
        self.assertEqual(recovery["suggested_operation"], "configure")
        self.assertIn("AirPort ACP service", recovery["message"])
        self.assertIn("ACP is blocked", recovery["message"])
        self.assertEqual(
            recovery["actions"],
            [
                "Disable VPN or security software that routes local network traffic, then try again.",
                "Check that the IP address is the Time Capsule or AirPort address.",
                "Confirm you are on the same network as the device.",
                "Use discovery or enter the current LAN IP address.",
            ],
        )

    def test_an_unreadable_folder_offers_disk_repair_then_uninstall_and_no_plain_retry(self) -> None:
        recovery = recovery_for("deploy", "xattr_migration_unreadable_folder", stage="migrate_xattrs_copy")

        self.assertEqual(recovery["localization_key"], "deploy.xattr_migration_unreadable_folder")
        self.assertEqual(recovery["action_ids"], ["disk_repair", "uninstall"])
        # Running deploy again unchanged fails the same way.
        self.assertFalse(recovery["retryable"])
        # Disk Repair can start an older install again, so only the present is claimed.
        self.assertIn("is not running now", recovery["message"])
        self.assertIn("Finder tags", recovery["actions"][1])
        # What a migration already moved stays on the files.
        self.assertIn("not migrated yet", recovery["actions"][1])
        # A backup's own metadata can be among what Uninstall deletes.
        self.assertNotIn("Time Machine", recovery["actions"][1])

    def test_an_unreadable_folder_uninstall_would_not_clear_offers_disk_repair_only(self) -> None:
        recovery = recovery_for("deploy", "xattr_migration_unreadable_folder_repair_only", stage="migrate_xattrs_copy")
        full = recovery_for("deploy", "xattr_migration_unreadable_folder", stage="migrate_xattrs_copy")

        self.assertEqual(recovery["localization_key"], "deploy.xattr_migration_unreadable_folder_repair_only")
        # Uninstall would keep a legacy database, so the next deploy would fail the same way.
        self.assertEqual(recovery["action_ids"], ["disk_repair"])
        self.assertFalse(recovery["retryable"])
        self.assertEqual((recovery["title"], recovery["message"], recovery["actions"][0]),
                         (full["title"], full["message"], full["actions"][0]))
        # Past a folder Disk Repair cannot fix, the card's Copy Diagnostics and a report are the next step.
        self.assertNotIn("Uninstall", recovery["actions"][1])
        self.assertIn("Copy Diagnostics", recovery["actions"][1])
        self.assertIn("https://github.com/jamesyc/TimeCapsuleSMB/issues", recovery["actions"][1])
        # Issues there are public, and the report names folders.
        self.assertIn("Reports there are public", recovery["actions"][1])

    def test_unsupported_device_recovery_names_express_only_where_the_model_is_the_cause(self) -> None:
        # configure and deploy fail on unsupported_device only for an unsupported model.
        for operation in ("configure", "deploy"):
            with self.subTest(operation=operation):
                recovery = recovery_for(operation, "unsupported_device")
                self.assertEqual(recovery["localization_key"], f"{operation}.unsupported_device")
                self.assertEqual(recovery["message"], "This AirPort model cannot run TimeCapsuleSMB.")
                self.assertFalse(recovery["retryable"])
        self.assertIn("Forget this device", recovery_for("deploy", "unsupported_device")["actions"][1])
        self.assertEqual(
            recovery_for("configure", "unsupported_device")["actions"][1],
            "Add your Time Capsule or AirPort Extreme instead.",
        )
        # flash and activate also use the code for operations a supported device
        # cannot do (NetBSD 6), so they get the neutral entry and no "Forget" advice.
        for operation in ("flash", "activate"):
            with self.subTest(operation=operation):
                recovery = recovery_for(operation, "unsupported_device")
                self.assertEqual(recovery["localization_key"], "unsupported_device")
                self.assertEqual(recovery["message"], "This operation is not supported on the detected AirPort model or OS.")
                self.assertFalse(any("Forget" in action for action in recovery["actions"]))

    def test_ssh_enable_timeout_recovery_says_to_wait_and_retry(self) -> None:
        # configure and set-ssh both report this code once ACP took the request
        # but SSH stayed closed for the whole wait.
        for operation in ("configure", "set-ssh"):
            with self.subTest(operation=operation):
                recovery = recovery_for(operation, "ssh_enable_timeout")
                self.assertEqual(recovery["localization_key"], "ssh_enable_timeout")
                self.assertEqual(recovery["title"], "SSH has not opened yet")
                self.assertIn("restarts the device", recovery["message"])
                self.assertTrue(recovery["retryable"])
                self.assertEqual(recovery["actions"][0], "Wait a few minutes, then try again.")

    def test_file_sharing_that_did_not_restart_offers_activate_at_every_restart_stage(self) -> None:
        # fsck fails with this code once its reboot is done, whether starting
        # the runtime or waiting for it failed.
        for stage in ("post_reboot_activation", "verify_runtime_activation"):
            with self.subTest(stage=stage):
                recovery = recovery_for("fsck", "runtime_not_restarted", stage=stage)
                self.assertEqual(recovery["localization_key"], "runtime_not_restarted")
                self.assertEqual(recovery["title"], "File sharing did not restart")
                self.assertEqual(recovery["suggested_operation"], "activate")
                self.assertEqual(recovery["action_ids"], ["start_smb", "run_checkup"])
                self.assertTrue(recovery["retryable"])

    def test_every_rebooting_operation_gets_the_shared_reboot_guidance(self) -> None:
        # One reboot path, so one entry per failure for every operation that
        # reboots; only deploy adds its NetBSD 4 and issue-177 steps.
        for operation in ("configure", "set-ssh", "uninstall", "fsck", "flash"):
            with self.subTest(operation=operation):
                started = recovery_for(operation, "reboot_not_started", stage="wait_for_reboot_down")
                finished = recovery_for(operation, "reboot_not_finished", stage="wait_for_reboot_up")
                self.assertEqual((started["localization_key"], started["title"]), ("reboot_not_started", "Reboot did not start"))
                self.assertEqual((finished["localization_key"], finished["title"]), ("reboot_not_finished", "Reboot did not finish"))
                self.assertEqual(finished["action_ids"], ["run_checkup"])
                self.assertTrue(started["retryable"] and finished["retryable"])
        self.assertEqual(recovery_for("deploy", "reboot_not_started")["localization_key"], "reboot_not_started")
        self.assertEqual(recovery_for("deploy", "reboot_not_finished")["localization_key"], "deploy.reboot_not_finished")

    def test_ssh_still_enabled_points_back_to_ssh_access(self) -> None:
        recovery = recovery_for("set-ssh", "ssh_still_enabled", stage="wait_for_reboot_up")

        self.assertEqual(recovery["localization_key"], "ssh_still_enabled")
        self.assertEqual(recovery["action_ids"], ["open_ssh_access"])
        self.assertEqual(recovery["actions"][0], "Disable SSH again in “SSH Access”.")

    def test_broken_acpd_says_try_again_only_where_nothing_was_installed(self) -> None:
        # The check before any change refuses with nothing installed.
        for operation, stage in (("deploy", "check_compatibility"), ("fsck", "wait_for_reboot_up"), ("uninstall", None)):
            with self.subTest(operation=operation, stage=stage):
                recovery = recovery_for(operation, "acpd_broken", stage=stage)
                self.assertEqual(recovery["localization_key"], "acpd_broken")
                self.assertIn("then try again", recovery["actions"][1])
        # Deploy's reboot: the update is on the device; the user checks it instead.
        recovery = recovery_for("deploy", "acpd_broken", stage="wait_for_reboot_up")
        self.assertEqual(recovery["localization_key"], "deploy.acpd_broken.wait_for_reboot_up")
        self.assertIn("The update is installed", recovery["message"])
        self.assertIn("run “Activate”", recovery["actions"][2])
        self.assertFalse(recovery["retryable"])

    def test_an_acpd_that_quit_points_to_a_restart_and_then_the_checkup(self) -> None:
        # One card wherever it is found: the deploy's start, Activate, the
        # restart after a disk repair, and every command's check before it
        # changes anything. Retrying first would fail the same way.
        for operation, stage in (("activate", "probe_runtime"),
                                 ("activate", "verify_runtime_activation"), ("fsck", "verify_runtime_activation"),
                                 ("deploy", None), ("uninstall", None), ("fsck", None), ("set-ssh", None)):
            with self.subTest(operation=operation, stage=stage):
                recovery = recovery_for(operation, "acpd_quit", stage=stage)
                self.assertEqual(recovery["localization_key"], "acpd_quit")
                self.assertIn("quit", recovery["message"])
                # A check before a command can find Samba still serving: the
                # card says what cannot happen, not that sharing is off.
                self.assertIn("file sharing cannot start", recovery["message"])
                self.assertNotIn("cannot run", recovery["message"])
                self.assertTrue(recovery["actions"][0].startswith("Unplug the device"))
                self.assertIn("run “Checkup”", recovery["actions"][1])
                self.assertIn("run “Activate”", recovery["actions"][2])
                self.assertFalse(recovery["retryable"])
                self.assertEqual(recovery["suggested_operation"], "doctor")

    def test_an_acpd_that_quit_at_the_deploys_start_says_the_update_is_installed(self) -> None:
        # After the restart, with the boot hook or by NetBSD 4's activation:
        # the update is on the device, so the user restarts it, not reinstalls.
        default = recovery_for("activate", "acpd_quit")
        for stage in ("verify_runtime_reboot", "verify_runtime_activation"):
            with self.subTest(stage=stage):
                recovery = recovery_for("deploy", "acpd_quit", stage=stage)
                self.assertEqual(recovery["localization_key"], "deploy.acpd_quit.after_update")
                self.assertTrue(recovery["message"].startswith("The update is installed, but"))
                self.assertEqual(recovery["title"], default["title"])
                self.assertEqual(recovery["actions"], default["actions"])
                self.assertFalse(recovery["retryable"])
                self.assertEqual(recovery["suggested_operation"], "doctor")
        # Refused before the upload: nothing is installed, so the general card.
        for stage in (None, "check_acpd", "upload_payload"):
            with self.subTest(stage=stage):
                self.assertEqual(recovery_for("deploy", "acpd_quit", stage=stage)["localization_key"], "acpd_quit")

    def test_configure_on_a_device_whose_acpd_quit_says_the_device_cannot_be_identified(self) -> None:
        recovery = recovery_for("configure", "acpd_quit")
        self.assertEqual(recovery["localization_key"], "configure.acpd_quit")
        self.assertIn("cannot be identified until it restarts", recovery["message"])
        self.assertTrue(recovery["actions"][0].startswith("Unplug the device"))
        self.assertIn("then try again", recovery["actions"][1])
        self.assertTrue(recovery["retryable"])
        self.assertEqual(recovery["suggested_operation"], "configure")

    def test_deploy_reboot_up_timeout_recovery_carries_detailed_guidance(self) -> None:
        recovery = recovery_for("deploy", "reboot_not_finished", stage="wait_for_reboot_up")

        self.assertEqual(recovery["title"], "Reboot did not finish")
        self.assertEqual(recovery["localization_key"], "deploy.reboot_not_finished")
        self.assertEqual(recovery["retryable"], True)
        self.assertEqual(recovery["suggested_operation"], "doctor")
        self.assertEqual(recovery["action_ids"], ["run_checkup"])
        self.assertIn("installation files were copied", recovery["message"])
        self.assertIn("did not answer over SSH again in time", recovery["message"])
        self.assertEqual(
            recovery["actions"],
            [
                "Wait a few more minutes.",
                "Make sure you are connected to the same network or Wi-Fi as the device.",
                (
                    "On NetBSD 4 devices, run “Activate” once SSH is reachable; the install did not get far "
                    "enough to activate Samba after the reboot."
                ),
                (
                    "If your device resets itself, see "
                    "https://github.com/jamesyc/TimeCapsuleSMB/issues/177."
                ),
                "If the device still doesn't answer, unplug it, wait 10 seconds and plug it back in.",
            ],
        )



class CliStepsTests(unittest.TestCase):
    """The CLI prints the app's next steps, with each control named by its command."""

    def entries(self) -> list[tuple[str, ErrorCode, str | None, recovery.RecoveryInfo]]:
        return (
            # doctor has no entries of its own, so each code reads its default.
            [("doctor", code, None, info) for code, info in recovery._DEFAULTS.items()]
            + [(operation, code, None, info) for (operation, code), info in recovery._OPERATION_CODE_RECOVERY.items()]
            + [(operation, code, stage, info) for (operation, code, stage), info in recovery._STAGE_RECOVERY.items()]
        )

    def test_app_controls_become_commands(self) -> None:
        self.assertEqual(
            cli_steps("deploy", ErrorCode.PAYLOAD_UPLOAD_TIMEOUT),
            (
                "Run `tcapsule fsck`.",
                "Connect this computer or the device by Ethernet, then run `tcapsule deploy` again.",
                "If this keeps happening, the disk may need replacing.",
            ),
        )
        self.assertEqual(
            cli_steps("deploy", ErrorCode.OPERATION_FAILED)[1],
            "If it keeps failing, report it, with this command's output, at https://github.com/jamesyc/TimeCapsuleSMB/issues.",
        )

    def test_a_step_about_a_control_only_the_app_has_is_left_out(self) -> None:
        app_steps = recovery_for("deploy", ErrorCode.INVALID_REQUEST)["actions"]
        self.assertIn("If “Helper path” is set in “Settings”, clear it.", app_steps)
        self.assertEqual(cli_steps("deploy", ErrorCode.INVALID_REQUEST), ("Update or reinstall TimeCapsuleSMB.",))

    def test_an_entry_s_own_cli_steps_replace_the_app_s(self) -> None:
        self.assertEqual(cli_steps("set-ssh", ErrorCode.SSH_DISABLED), ("Run `tcapsule set-ssh --enable`, then try again.",))
        # The terminal app needs Local Network access, not TimeCapsuleSMB.
        self.assertIn("Turn on the app this command runs in", " ".join(cli_steps("discover", ErrorCode.LOCAL_NETWORK_PERMISSION_DENIED)))

    def test_the_stage_and_operation_choose_the_steps_as_in_the_app(self) -> None:
        self.assertEqual(cli_steps("deploy", ErrorCode.REMOTE_ERROR, stage="verify_payload_upload")[0], "Wake the disk, then try again.")
        self.assertEqual(cli_steps("deploy", ErrorCode.REMOTE_ERROR)[1], "If it keeps failing, run `tcapsule doctor`.")

    def test_the_app_never_receives_the_cli_steps(self) -> None:
        self.assertNotIn("cli_extra", recovery_for("set-ssh", ErrorCode.DEVICE_UNREACHABLE))

    def test_every_entry_gives_the_cli_steps_in_its_own_words(self) -> None:
        for operation, code, stage, info in self.entries():
            with self.subTest(operation=operation, code=code, stage=stage):
                steps = cli_steps(operation, code, stage=stage)
                for step in steps:
                    # No app control is left unnamed for the CLI.
                    self.assertNotIn("“", step)
                    self.assertNotIn("this Mac", step)
                # Guidance the app gives is never lost on the CLI.
                self.assertEqual(bool(steps), bool(info.actions) or bool(info.cli_extra), (info.actions, steps))

    def test_every_cli_phrase_matches_a_step_the_app_shows(self) -> None:
        # A phrase that matches nothing means an app step was reworded and the
        # CLI now prints the app's words, or leaves the step out.
        steps = " ".join(step for *_rest, info in self.entries() for step in info.actions)
        for app_words, _cli_words in recovery._CLI_WORDS:
            with self.subTest(app_words=app_words):
                self.assertIn(app_words, steps)

    def test_every_app_only_step_is_one_the_app_shows(self) -> None:
        steps = {step for *_rest, info in self.entries() for step in info.actions}
        for step in recovery._APP_ONLY_STEPS:
            with self.subTest(step=step):
                self.assertIn(step, steps)

    def test_no_cli_step_speaks_of_the_app_s_screens(self) -> None:
        # Words for what only the app has: a CLI user cannot follow them.
        app_words = ("Forget", "highlighted", "in the app", "the saved device", "from the CLI", "configuration step",
                     "volume list", "Select ")
        for operation, code, stage, _info in self.entries():
            for step in cli_steps(operation, code, stage=stage):
                with self.subTest(operation=operation, code=code, stage=stage, step=step):
                    self.assertFalse([word for word in app_words if word in step])

    def test_an_entry_adds_its_cli_only_steps_after_the_app_s(self) -> None:
        self.assertEqual(
            cli_steps("deploy", ErrorCode.DEVICE_UNREACHABLE),
            (
                "Make sure the device is turned on and connected to the same network or Wi-Fi as this computer.",
                "If the device is restarting, wait a few minutes, then try again.",
                "Run `tcapsule doctor` to see which AirPorts answer on this network.",
            ),
        )
        # An app step with no words of its own for the CLI never reaches it.
        self.assertEqual(cli_steps("deploy", ErrorCode.INSTALL_INTERRUPTED), ("Run `tcapsule deploy` again to complete the install.",))


if __name__ == "__main__":
    unittest.main()
