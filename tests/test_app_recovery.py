from __future__ import annotations

import unittest

from timecapsulesmb.app import recovery
from timecapsulesmb.app.recovery import cli_steps, recovery_for, ssh_timeout_slow_device_recovery
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
                # A typed address has nothing that could prove a Local Network block.
                "If AirPort Utility can see the device but TimeCapsuleSMB cannot, macOS is blocking TimeCapsuleSMB: "
                "open “System Settings” > “Privacy & Security” > “Local Network”, then turn TimeCapsuleSMB off and on.",
            ],
        )
        # Terminal.app is exempt from Local Network access; another terminal
        # app is not. The step has no use on Linux.
        mac = cli_steps("configure", ErrorCode.REMOTE_ERROR, stage="acp_port_probe", platform="darwin")
        self.assertEqual(
            mac[-1],
            "If AirPort Utility can see the device but this command cannot, and it runs in a terminal app other than "
            "Terminal, such as iTerm2 or Visual Studio Code, macOS is blocking that app: turn it on in System "
            "Settings > Privacy & Security > Local Network.",
        )
        linux = cli_steps("configure", ErrorCode.REMOTE_ERROR, stage="acp_port_probe", platform="linux")
        self.assertEqual(linux, mac[:-1])

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

    def test_ssh_enable_timeout_recovery_says_to_unplug_only_on_the_devices_network(self) -> None:
        # configure and set-ssh both report this code once ACP took the request
        # but SSH stayed closed for the whole wait, when nothing showed where
        # this Mac was (a device that stays silent while this Mac stays on its
        # network gets restart_stuck instead). A restart that long has stopped
        # partway and ends only when the device is unplugged, which helps only
        # a Mac on its network: the network comes first, then the unplug,
        # never waiting longer.
        for operation in ("configure", "set-ssh"):
            with self.subTest(operation=operation):
                recovery = recovery_for(operation, "ssh_enable_timeout")
                self.assertEqual(recovery["localization_key"], "ssh_enable_timeout")
                self.assertEqual(recovery["title"], "The device has not come back")
                self.assertIn("restarts the device", recovery["message"])
                self.assertIn("until they are unplugged", recovery["message"])
                self.assertTrue(recovery["retryable"])
                self.assertEqual(recovery["actions"], [
                    "Make sure you are connected to the same network or Wi-Fi as the device.",
                    "If this Mac is on the device's network, unplug the device, wait 10 seconds and plug it back in.",
                    "Wait 2 minutes for the device to start up, then try again.",
                ])
                self.assertFalse(any("Wait a few minutes" in action for action in recovery["actions"]))

    def test_a_restart_that_stayed_silent_on_the_same_network_unplugs_first(self) -> None:
        # The wait saw this Mac stay on the device's network (by its router)
        # while nothing answered: the unplug is the first step, without an if.
        for operation, retry in (
            ("configure", "Wait 2 minutes for the device to start up, then try again."),
            ("set-ssh", "Wait 2 minutes for the device to start up, then run “Checkup”."),
            ("uninstall", "Wait 2 minutes for the device to start up, then run “Checkup”."),
            ("deploy", "Wait 2 minutes for the device to start up, then run “Checkup”."),
        ):
            with self.subTest(operation=operation):
                recovery = recovery_for(operation, "restart_stuck")
                self.assertEqual(recovery["title"], "The device has not come back")
                self.assertIn("while this Mac stayed on that network", recovery["message"])
                self.assertEqual(recovery["actions"][:2], ["Unplug the device, wait 10 seconds and plug it back in.", retry])
        # A reset to factory settings looks the same from here, in every flow.
        for operation in ("configure", "set-ssh", "deploy"):
            with self.subTest(reset_step=operation):
                self.assertIn(
                    "If your device resets itself, see https://github.com/jamesyc/TimeCapsuleSMB/issues/177.",
                    recovery_for(operation, "restart_stuck")["actions"],
                )
        deploy = recovery_for("deploy", "restart_stuck")
        self.assertEqual(deploy["localization_key"], "deploy.restart_stuck")
        self.assertIn("installation files were copied", deploy["message"])
        self.assertIn("On NetBSD 4 devices, run “Activate” if the checkup says Samba is not running.", deploy["actions"])
        self.assertEqual(deploy["action_ids"], ["run_checkup"])
        # Add Device has no saved device to run a Checkup on.
        self.assertNotIn("action_ids", {k: v for k, v in recovery_for("configure", "restart_stuck").items() if v})

    def test_a_silent_device_is_told_apart_by_where_this_mac_is(self) -> None:
        # Same network: off, unplugged or stuck, so the unplug comes without an
        # if. Unknown: only conditionally. Another network: reconnect, no unplug.
        same = recovery_for("deploy", "device_silent")
        self.assertEqual(same["title"], "Device not answering")
        self.assertIn("same network as when it last reached the device", same["message"])
        self.assertIn("Unplug the device, wait 10 seconds and plug it back in.", same["actions"])
        unknown = recovery_for("deploy", "device_unreachable")
        self.assertIn(
            "If this Mac is on the device's network, unplug the device, wait 10 seconds and plug it back in.",
            unknown["actions"],
        )
        moved = recovery_for("deploy", "device_off_network")
        self.assertIn("another network with the same addresses", moved["message"])
        self.assertFalse(any("nplug" in step for step in [moved["message"], *moved["actions"]]))

    def test_ssh_that_stayed_closed_after_a_finished_restart_says_to_try_again_first(self) -> None:
        # The device came back on a new boot and is on the network, so the
        # first step enables SSH again; unplugging comes only after that.
        for operation in ("configure", "set-ssh"):
            with self.subTest(operation=operation):
                recovery = recovery_for(operation, "ssh_closed_after_restart")
                self.assertEqual(recovery["localization_key"], "ssh_closed_after_restart")
                self.assertEqual(recovery["title"], "SSH did not turn on")
                self.assertIn("it came back", recovery["message"])
                self.assertNotIn("off the network", recovery["message"])
                self.assertTrue(recovery["retryable"])
                self.assertEqual(recovery["actions"][0], "Try again.")
                self.assertIn("unplug the device", recovery["actions"][1])

    def test_ssh_enable_guidance_names_the_waits_the_restart_wait_uses(self) -> None:
        # The guidance states each wait in minutes: it must say what
        # services.reboot actually waits.
        from timecapsulesmb.services.reboot import REBOOT_SSH_TIMEOUT_SECONDS, REBOOT_UP_TIMEOUT_SECONDS
        for code, seconds in (("ssh_enable_timeout", REBOOT_UP_TIMEOUT_SECONDS),
                              ("ssh_closed_after_restart", REBOOT_SSH_TIMEOUT_SECONDS)):
            with self.subTest(code=code):
                self.assertEqual(seconds % 60, 0)
                self.assertIn(f"{seconds // 60} minutes", recovery_for("configure", code)["message"])

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

    def test_a_stale_link_local_address_in_add_device_is_not_called_saved(self) -> None:
        # Add Device's address comes from discovery or is typed: nothing is saved yet.
        recovery = recovery_for("configure", "link_local_interface_gone")
        self.assertEqual(recovery["localization_key"], "configure.link_local_interface_gone")
        self.assertEqual(recovery["title"], "This address no longer works")
        self.assertNotIn("saved", " ".join((recovery["message"], *recovery["actions"])))
        self.assertEqual(recovery["actions"][1], "Refresh discovery and choose the device again.")
        self.assertTrue(recovery["retryable"])
        self.assertEqual(recovery["suggested_operation"], "configure")
        self.assertEqual(
            cli_steps("configure", ErrorCode.LINK_LOCAL_INTERFACE_GONE)[1],
            "Run `tcapsule configure` again and choose the device it finds.",
        )
        # Every command after Add Device uses the saved address.
        for operation in ("deploy", "set-ssh", "doctor"):
            with self.subTest(operation=operation):
                recovery = recovery_for(operation, "link_local_interface_gone")
                self.assertEqual(recovery["localization_key"], "link_local_interface_gone")
                self.assertEqual(recovery["title"], "The saved address no longer works")

    def test_the_helper_marks_the_cards_where_macos_may_block_the_app(self) -> None:
        entries = (
            [(code, info) for code, info in recovery._DEFAULTS.items()]
            + [(code, info) for (_, code), info in recovery._OPERATION_CODE_RECOVERY.items()]
            + [(code, info) for (_, code, _), info in recovery._STAGE_RECOVERY.items()]
        )
        for code, info in entries:
            with self.subTest(key=info.localization_key):
                payload = info.to_jsonable()
                if code == ErrorCode.LOCAL_NETWORK_PERMISSION_DENIED:
                    # A block's card: what the app sees about itself goes first.
                    self.assertEqual(payload.get("local_network_block"), "proven")
                elif recovery._BLOCKED_IF_AIRPORT_UTILITY_SEES_IT in info.actions:
                    # A block nothing proved: the app's steps only as a possibility.
                    self.assertEqual(payload.get("local_network_block"), "possible")
                else:
                    self.assertNotIn("local_network_block", payload)
        # A proven block on any operation reads the block's card.
        self.assertEqual(recovery_for("deploy", ErrorCode.ACP_NO_ROUTE)["local_network_block"], "proven")
        self.assertEqual(recovery_for("discover", ErrorCode.LOCAL_NETWORK_PERMISSION_DENIED)["local_network_block"], "proven")
        self.assertEqual(recovery_for("set-ssh", ErrorCode.DEVICE_UNREACHABLE)["local_network_block"], "possible")
        self.assertEqual(
            recovery_for("configure", ErrorCode.REMOTE_ERROR, stage="acp_port_probe")["local_network_block"], "possible",
        )
        self.assertNotIn("local_network_block", recovery_for("deploy", ErrorCode.LOCAL_NETWORK_FILTERED))

    def test_a_mac_that_left_the_devices_network_gets_a_checkup_not_an_unplug(self) -> None:
        # The device may well be back: reconnect, then a Checkup says, without
        # restarting it again.
        generic = recovery_for("fsck", ErrorCode.CLIENT_LEFT_NETWORK)
        self.assertEqual(generic["localization_key"], "client_left_network")
        self.assertEqual(generic["action_ids"], ["run_checkup"])
        self.assertIn("by Wi-Fi or one of its LAN ports", generic["actions"][0])
        self.assertFalse(any("unplug" in step.lower() for step in generic["actions"]))
        # An install is done once its files are copied; NetBSD 4 may need Activate.
        deploy = recovery_for("deploy", ErrorCode.CLIENT_LEFT_NETWORK, stage="wait_for_reboot_up")
        self.assertEqual(deploy["localization_key"], "deploy.client_left_network")
        self.assertIn("installation files were copied", deploy["message"])
        self.assertIn("Activate", deploy["actions"][-1])
        # Add Device has no saved device for a Checkup: reconnect and try again,
        # which finds SSH on and does not restart the device.
        configure = recovery_for("configure", ErrorCode.CLIENT_LEFT_NETWORK, stage="wait_for_reboot_up")
        self.assertEqual(configure["localization_key"], "configure.client_left_network")
        self.assertTrue(configure["retryable"])
        self.assertNotIn("run_checkup", configure.get("action_ids", []))
        self.assertFalse(any("Checkup" in step for step in configure["actions"]))
        self.assertIn("try again", configure["actions"][-1])

    def test_a_disk_apple_will_not_mount_offers_the_wipe(self) -> None:
        # Apple's disk service lists no volume of it, so Disk Repair, which
        # repairs mounted volumes, finds nothing: the wipe is the way out.
        recovery = recovery_for("deploy", "deploy_disk_not_mounted")
        self.assertEqual(recovery["action_ids"], ["wipe_disk"])
        self.assertIn("“Wipe Internal Disk”", recovery["actions"][2])
        self.assertIn("holds nothing you need", recovery["actions"][2])
        self.assertEqual(recovery["actions"][3], "Run “Install / Update Samba” again after the wipe.")

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
                # Ten minutes have passed: a restart that long ends only with an
                # unplug, which helps only a Mac on the device's network.
                "Make sure you are connected to the same network or Wi-Fi as the device.",
                "If this Mac is on the device's network, unplug the device, wait 10 seconds and plug it back in.",
                (
                    "On NetBSD 4 devices, run “Activate” once SSH is reachable; the install did not get far "
                    "enough to activate Samba after the reboot."
                ),
                (
                    "If your device resets itself, see "
                    "https://github.com/jamesyc/TimeCapsuleSMB/issues/177."
                ),
            ],
        )



class CliStepsTests(unittest.TestCase):
    """The CLI prints the app's next steps, with each control named by its command."""

    def entries(self) -> list[tuple[str, ErrorCode, str | None, recovery.RecoveryInfo]]:
        """Every entry the CLI can print. The Local Network cards are not
        among them: only the app raises their codes
        (network.local_network_block_possible)."""
        entries = (
            # doctor has no entries of its own, so each code reads its default.
            [("doctor", code, None, info) for code, info in recovery._DEFAULTS.items()]
            + [(operation, code, None, info) for (operation, code), info in recovery._OPERATION_CODE_RECOVERY.items()]
            + [(operation, code, stage, info) for (operation, code, stage), info in recovery._STAGE_RECOVERY.items()]
        )
        return [entry for entry in entries if not entry[-1].local_network_block]

    def test_timeouts_and_disk_waits_never_claim_a_probable_cause(self) -> None:
        # A timeout or a process waiting on the disk is not proof: a slow link
        # or a firmware race (F19) looks the same. Their guidance offers
        # causes with "may" or "can", and never says to replace the disk on
        # the first try.
        cards = {
            "payload upload": recovery_for("deploy", ErrorCode.PAYLOAD_UPLOAD_TIMEOUT),
            "write test": recovery_for("deploy", ErrorCode.DISK_WRITE_TEST_UNRESPONSIVE),
            "migration timeout": recovery_for("deploy", ErrorCode.XATTR_MIGRATION_TIMEOUT),
            "migration stalled": recovery_for("deploy", ErrorCode.XATTR_MIGRATION_STALLED),
            "stuck service": recovery_for("deploy", ErrorCode.MANAGER_STOP_TIMEOUT),
            "earlier migration": recovery_for("deploy", ErrorCode.PREVIOUS_MIGRATION_STALLED),
            "wifi link": recovery_for("deploy", ErrorCode.WIFI_LINK_SLOW),
            "unfinished wipe": recovery_for("wipe-disk", ErrorCode.WIPE_DISK_NO_RESPONSE),
            "disk wait": recovery_for("deploy", ErrorCode.DISK_NOT_RESPONDING),
            "disk check wait": recovery_for("deploy", ErrorCode.DISK_CHECK_NOT_RESPONDING),
            "wipe disk wait": recovery_for("wipe-disk", ErrorCode.DISK_NOT_RESPONDING, stage="erase_disk"),
            "no answer": ssh_timeout_slow_device_recovery(),
        }
        for name, card in cards.items():
            with self.subTest(name):
                text = " ".join([card["title"], card["message"], *card["actions"]])
                self.assertNotIn("probably", text)
                self.assertNotIn("often causes", text)
                self.assertNotIn("too slowly", text)
                self.assertFalse(any("replac" in step for step in card["actions"][:2]), card["actions"])
        # Evidence keeps its word: a write the device saw fail.
        self.assertIn("probably failing", recovery_for("wipe-disk", ErrorCode.DISK_FAILING, stage="wipe_disk")["message"])

    def test_a_command_that_did_not_answer_still_starts_with_a_reboot(self) -> None:
        # Devices up for years answer slowly until a restart; the connection
        # is the last thing to check.
        card = ssh_timeout_slow_device_recovery(device_name="Time Capsule 4th generation")
        self.assertEqual(card["actions"][0], "Reboot the device.")
        self.assertIn("did not answer in time", card["message"])
        self.assertNotIn("very slowly", card["message"])
        self.assertIn("check the network connection to the device", card["actions"][-1])

    def test_app_controls_become_commands(self) -> None:
        self.assertEqual(
            cli_steps("deploy", ErrorCode.PAYLOAD_UPLOAD_TIMEOUT),
            (
                # The connection first, Disk Repair only if it times out again:
                # a timeout alone never says to replace the disk.
                "If this computer or the device is on Wi-Fi, connect it by Ethernet, then run `tcapsule deploy` again.",
                "If it times out again, run `tcapsule fsck`.",
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
            cli_steps("deploy", ErrorCode.DEVICE_UNREACHABLE, platform="darwin"),
            (
                "Make sure the device is turned on and connected to the same network or Wi-Fi as this computer.",
                "If the device is restarting, wait a few minutes, then try again.",
                "If this computer is on the device's network, unplug the device, wait 10 seconds and plug it back in.",
                # A command's connections count as its terminal app's.
                "If AirPort Utility can see the device but this command cannot, and it runs in a terminal app other than "
                "Terminal, such as iTerm2 or Visual Studio Code, macOS is blocking that app: turn it on in System "
                "Settings > Privacy & Security > Local Network.",
                "Run `tcapsule doctor` to see which AirPorts answer on this network.",
            ),
        )
        # The CLI also runs on Linux, where a step about macOS has no use.
        linux = cli_steps("deploy", ErrorCode.DEVICE_UNREACHABLE, platform="linux")
        self.assertFalse(any("macOS" in step for step in linux))
        self.assertEqual(len(linux), 4)
        # A stale link-local address: the CLI finds the device again with configure.
        self.assertEqual(
            cli_steps("deploy", ErrorCode.LINK_LOCAL_INTERFACE_GONE),
            ("Reconnect the adapter or cable this computer used to reach the device, then try again.",
             "Run `tcapsule configure` to find the device again."),
        )
        # An app step with no words of its own for the CLI never reaches it.
        self.assertEqual(cli_steps("deploy", ErrorCode.INSTALL_INTERRUPTED), ("Run `tcapsule deploy` again to complete the install.",))


class FieldSituationCardTests(unittest.TestCase):
    """Each situation the field shows ends with the card written for it: its
    own key, the buttons it offers and whether trying again can help. The
    card's wording is free to change; the rows name the condition, not a
    device, and tests in each area check that the condition reaches the code."""

    # (situation, operation, code, stage, the card's localization key, action_ids, retryable)
    ROWS = (
        # Reaching the device.
        ('nothing answers and this Mac is where it reached the device', 'deploy', 'device_silent', None,
         'device_silent', ['run_checkup'], True),
        ("this Mac is on another network than the device's", 'deploy', 'device_off_network', None,
         'device_off_network', [], True),
        ('nothing answers and nothing says where this Mac is', 'deploy', 'device_unreachable', None,
         'device_unreachable', [], True),
        ('a saved link-local address on an interface this Mac no longer has', 'deploy', 'link_local_interface_gone', None,
         'link_local_interface_gone', [], True),
        ("macOS refuses this app's connections while SSH reaches the device", 'deploy', 'acp_no_route', None,
         'local_network_permission_denied', ['open_system_settings', 'retry'], True),
        ('macOS blocks discovery', 'discover', 'local_network_permission_denied', None,
         'discover.local_network_permission_denied', ['open_system_settings', 'retry'], True),
        ("ACP blocked from this computer while the device's own ACP works", 'deploy', 'acp_unreachable', None,
         'acp_unreachable', [], True),
        ("ACP's port silent while SSH's answers", 'configure', 'acpd_broken', None,
         'acpd_broken', ['run_checkup'], True),
        ('nothing answers at an address being added', 'configure', 'remote_error', 'acp_port_probe',
         'configure.remote_error.acp_port_probe', [], True),
        ('another AirPort answers at the saved address', 'deploy', 'device_identity_mismatch', None,
         'device_identity_mismatch', [], True),
        ('the AirPort admin password is wrong', 'configure', 'auth_failed', None,
         'configure.auth_failed', ['replace_password'], True),
        ('SSH rejects the password the AirPort service accepts', 'deploy', 'ssh_password_not_applied', None,
         'ssh_password_not_applied', [], True),
        ('SSH turned off', 'deploy', 'ssh_disabled', None,
         'ssh_disabled', ['open_ssh_access'], True),
        ('the device drops the SSH connection before login', 'configure', 'ssh_transport_failed', None,
         'ssh_transport_failed', [], True),
        ('Add Device logs in but cannot read the device', 'configure', 'device_probe_failed', None,
         'configure.device_probe_failed', [], True),
        ('copying the installation files outlasts its timeout', 'deploy', 'payload_upload_timeout', None,
         'deploy.payload_upload_timeout', ['disk_repair'], True),
        # Restarts.
        ('the restart went silent while this Mac stayed', 'deploy', 'restart_stuck', None,
         'deploy.restart_stuck', ['run_checkup'], True),
        ('the restart that turns SSH on went silent', 'configure', 'restart_stuck', None,
         'configure.restart_stuck', [], True),
        ("this Mac left the device's network during the restart", 'deploy', 'client_left_network', None,
         'deploy.client_left_network', ['run_checkup'], True),
        ('Apple ACPd stopped without restarting the device', 'deploy', 'acpd_broken', None,
         'acpd_broken', ['run_checkup'], True),
        ('the device did not answer just before the restart', 'deploy', 'reboot_not_requested', None,
         'deploy.reboot_not_requested', ['run_checkup'], True),
        ('the device did not restart when asked', 'configure', 'reboot_not_started', None,
         'reboot_not_started', [], True),
        ('no new start within the wait for SSH', 'configure', 'ssh_enable_timeout', None,
         'ssh_enable_timeout', [], True),
        ('SSH stayed off after the restart that turns it on', 'configure', 'ssh_closed_after_restart', None,
         'ssh_closed_after_restart', [], True),
        # Apple's services.
        ('Apple ACPd does not answer the network', 'deploy', 'acpd_network_silent', None,
         'acpd_network_silent', ['run_checkup'], True),
        ('Apple ACPd quit', 'deploy', 'acpd_quit', None,
         'acpd_quit', ['run_checkup'], False),
        ("Apple's disk service is still handing over", 'deploy', 'diskd_handoff_pending', None,
         'diskd_handoff_pending', ['run_checkup'], True),
        # Disks.
        ('MaSt lists no disk', 'deploy', 'deploy_no_disk_detected', None,
         'deploy.deploy_no_disk_detected', [], True),
        ('a process stuck on the disk', 'deploy', 'disk_not_responding', None,
         'disk_not_responding', [], True),
        ('a disk check stuck on the disk', 'deploy', 'disk_check_not_responding', None,
         'disk_check_not_responding', [], True),
        ('a disk reporting a few GB', 'deploy', 'disk_size_misreported', None,
         'disk_size_misreported', [], True),
        ('a failed Seagate disk', 'deploy', 'disk_seagate_failed', None,
         'disk_seagate_failed', [], False),
        ('an AirPort Extreme with no USB disk', 'deploy', 'deploy_no_usb_disk_detected', None,
         'deploy.deploy_no_usb_disk_detected', [], True),
        ('Wipe Internal Disk on a device with no internal disk', 'wipe-disk', 'wipe_disk_no_internal_disk', None,
         'wipe_disk_no_internal_disk', [], False),
        ('a disk with no HFS partition', 'deploy', 'deploy_no_hfs_partition', None,
         'deploy.deploy_no_hfs_partition', ['wipe_disk'], True),
        ('an HFS volume Apple will not mount', 'deploy', 'deploy_disk_not_mounted', None,
         'deploy.deploy_disk_not_mounted', ['wipe_disk'], True),
        ('a full, read-only or refusing volume', 'deploy', 'deploy_disk_not_writable', None,
         'deploy.deploy_disk_not_writable', [], True),
        ('kernel disk errors', 'deploy', 'disk_failing', None,
         'disk_failing', [], False),
        ('the test write does not finish', 'deploy', 'disk_write_test_unresponsive', None,
         'disk_write_test_unresponsive', ['disk_repair'], True),
        ('any process that will not stop for the install', 'deploy', 'manager_stop_timeout', None,
         'deploy.manager_stop_timeout', [], True),
        ('any process that will not stop for the uninstall', 'uninstall', 'manager_stop_timeout', None,
         'uninstall.manager_stop_timeout', [], True),
        ('any process that will not stop for a disk repair', 'fsck', 'manager_stop_timeout', None,
         'fsck.manager_stop_timeout', ['disk_repair'], True),
        ('any process that will not stop for Activate', 'activate', 'manager_stop_timeout', None,
         'activate.manager_stop_timeout', [], True),
        ("any process that will not stop for the start after the install's restart", 'deploy', 'manager_stop_timeout',
         'post_reboot_activation', 'deploy.manager_stop_timeout.post_reboot_activation', [], True),
        ('any process that will not stop for a wipe', 'wipe-disk', 'manager_stop_timeout', None,
         'wipe-disk.manager_stop_timeout', [], True),
        # fsck and Wipe Internal Disk start file sharing after their restart
        # as deploy does: the same card.
        ("any process that will not stop for the start after a disk repair's restart", 'fsck', 'manager_stop_timeout',
         'post_reboot_activation', 'deploy.manager_stop_timeout.post_reboot_activation', [], True),
        ("any process that will not stop for the start after a wipe's restart", 'wipe-disk', 'manager_stop_timeout',
         'post_reboot_activation', 'deploy.manager_stop_timeout.post_reboot_activation', [], True),
        ('the flash full when the install writes it', 'deploy', 'insufficient_flash_space', None,
         'deploy.insufficient_flash_space', [], False),
        ('the Samba files gone after an erase', 'deploy', 'payload_missing_from_disk', None,
         'payload_missing_from_disk', [], False),
        ('the Samba files damaged on the disk', 'deploy', 'payload_damaged_on_disk', None,
         'payload_damaged_on_disk', [], False),
        ('the session ends while fsck_hfs runs', 'fsck', 'fsck_session_dropped', None,
         'fsck.fsck_session_dropped', ['disk_repair'], True),
        ('the volume stays mounted for fsck', 'fsck', 'fsck_volume_not_unmounted', None,
         'fsck.fsck_volume_not_unmounted', ['disk_repair'], True),
        ('an earlier repair still runs', 'fsck', 'fsck_already_running', None,
         'fsck.fsck_already_running', ['disk_repair'], True),
        ("the wipe's format did not finish", 'wipe-disk', 'wipe_disk_erase_failed', None,
         'wipe_disk_erase_failed', [], True),
        ('a disk job runs when the wipe starts', 'wipe-disk', 'wipe_disk_job_running', None,
         'wipe_disk_job_running', [], True),
        ('the migration cannot read a folder', 'deploy', 'xattr_migration_unreadable_folder', None,
         'deploy.xattr_migration_unreadable_folder', ['disk_repair', 'uninstall'], False),
    )

    def test_each_situation_gets_the_card_that_says_what_to_do(self) -> None:
        for name, operation, code, stage, key, action_ids, retryable in self.ROWS:
            with self.subTest(name):
                card = recovery_for(operation, ErrorCode(code), stage=stage)
                self.assertEqual(card["localization_key"], key)
                self.assertEqual(card["action_ids"], action_ids)
                self.assertIs(card["retryable"], retryable)
                # Never the generic cards, which say only that something failed.
                self.assertNotIn(key, ("remote_error", "operation_failed", "validation_failed"))


if __name__ == "__main__":
    unittest.main()
