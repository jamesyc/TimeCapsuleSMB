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

    def test_deploy_reboot_up_timeout_recovery_carries_detailed_guidance(self) -> None:
        recovery = recovery_for("deploy", "reboot_not_finished", stage="wait_for_reboot_up")

        self.assertEqual(recovery["title"], "Reboot did not finish")
        self.assertEqual(recovery["localization_key"], "deploy.reboot_not_finished")
        self.assertEqual(recovery["retryable"], True)
        self.assertEqual(recovery["suggested_operation"], "doctor")
        self.assertEqual(recovery["action_ids"], ["run_checkup"])
        self.assertIn("payload was uploaded", recovery["message"])
        self.assertIn("4 minute timeout", recovery["message"])
        self.assertEqual(
            recovery["actions"],
            [
                "Wait a few more minutes.",
                "The device may have a new IP address. Run Discover and reselect it.",
                "Make sure you are connected to the same network or Wi-Fi as the device.",
                (
                    "On NetBSD 4 devices, run tcapsule activate once SSH is reachable; deploy did not get far "
                    "enough to activate Samba after reboot."
                ),
                (
                    "If your device resets itself, see "
                    "https://github.com/jamesyc/TimeCapsuleSMB/issues/177."
                ),
            ],
        )


if __name__ == "__main__":
    unittest.main()
