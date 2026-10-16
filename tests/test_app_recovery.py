from __future__ import annotations

import unittest

from timecapsulesmb.app.recovery import recovery_for


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
        self.assertEqual(recovery["actions"][0], "Disable SSH again in SSH Access.")

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
        self.assertIn("run Activate", recovery["actions"][2])
        self.assertFalse(recovery["retryable"])

    def test_deploy_reboot_up_timeout_recovery_carries_detailed_guidance(self) -> None:
        recovery = recovery_for("deploy", "reboot_not_finished", stage="wait_for_reboot_up")

        self.assertEqual(recovery["title"], "Reboot did not finish")
        self.assertEqual(recovery["localization_key"], "deploy.reboot_not_finished")
        self.assertEqual(recovery["retryable"], True)
        self.assertEqual(recovery["suggested_operation"], "doctor")
        self.assertEqual(recovery["action_ids"], ["run_checkup"])
        self.assertIn("payload was uploaded", recovery["message"])
        self.assertIn("did not accept SSH again in time", recovery["message"])
        self.assertEqual(
            recovery["actions"],
            [
                "Wait a few more minutes.",
                "Make sure you are connected to the same network or Wi-Fi as the device.",
                (
                    "On NetBSD 4 devices, run Activate once SSH is reachable; deploy did not get far "
                    "enough to activate Samba after reboot."
                ),
                (
                    "If your device resets itself, see "
                    "https://github.com/jamesyc/TimeCapsuleSMB/issues/177."
                ),
                "If the device still doesn't answer, unplug it, wait 10 seconds and plug it back in.",
            ],
        )


if __name__ == "__main__":
    unittest.main()
