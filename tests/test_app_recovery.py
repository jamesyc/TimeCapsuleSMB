from __future__ import annotations

import unittest

from timecapsulesmb.app.recovery import recovery_for


class AppRecoveryTests(unittest.TestCase):
    def test_deploy_reboot_up_timeout_recovery_carries_detailed_guidance(self) -> None:
        recovery = recovery_for("deploy", "remote_error", stage="wait_for_reboot_up")

        self.assertEqual(recovery["title"], "Reboot did not finish")
        self.assertEqual(recovery["localization_key"], "deploy.remote_error.wait_for_reboot_up")
        self.assertEqual(recovery["retryable"], True)
        self.assertEqual(recovery["suggested_operation"], "doctor")
        self.assertEqual(recovery["action_ids"], ["run_checkup"])
        self.assertIn("payload was uploaded", recovery["message"])
        self.assertIn("4 minute timeout", recovery["message"])
        self.assertEqual(
            recovery["actions"],
            [
                "Wait a few more minutes.",
                "If the device is reachable at a new IP, update TC_HOST or rerun configure.",
                "Make sure you are connected to the same network or Wi-Fi as the device.",
                (
                    "On NetBSD 4 devices, run tcapsule activate once SSH is reachable; deploy did not get far "
                    "enough to activate Samba after reboot."
                ),
            ],
        )


if __name__ == "__main__":
    unittest.main()
