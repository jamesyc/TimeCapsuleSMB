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
