from __future__ import annotations

import unittest

from timecapsulesmb.app.ops import MANAGED_SETTING_PARAMS, OPERATION_SPECS
from timecapsulesmb.app.ops.common import managed_setting_params, resolve_managed_settings
from timecapsulesmb.core.config import CONFIG_VALIDATORS, AppConfig, device_ata_idle_seconds
from timecapsulesmb.core.runtime_settings import (
    COMPARED_FLASH_KEYS,
    COMPARED_SETTINGS,
    ManagedSettings,
    SettingsSource,
    managed_flash_values,
    settings_not_applied,
)
from timecapsulesmb.services.app import AppOperationError
from timecapsulesmb.services.configure import build_managed_config_env_values
from timecapsulesmb.services.deploy import DeployRuntimeConfig, render_flash_runtime_config


def written(values: dict[str, str], **settings: object) -> dict[str, str]:
    """The compared keys of the Flash config deploy writes, as the device's
    shell reads them back."""
    text = render_flash_runtime_config(AppConfig.from_values(values), None, **settings)
    pairs = (line.partition("=") for line in text.splitlines())
    return {key: value.strip("'") for key, _, value in pairs if key in COMPARED_FLASH_KEYS}


class SettingsNotAppliedTests(unittest.TestCase):
    def test_nothing_differs_right_after_a_deploy_from_the_same_settings(self) -> None:
        values = {"TC_MDNS_ADVERTISE_AFP": "true", "TC_ATA_STANDBY": "", "TC_ATA_IDLE_SECONDS": "600"}
        wanted = managed_flash_values(AppConfig.from_values(values))
        self.assertEqual(settings_not_applied(wanted, written(values)), [])

    def test_a_setting_changed_after_the_deploy_is_reported(self) -> None:
        installed = written({"TC_MDNS_ADVERTISE_AFP": "false"})
        wanted = managed_flash_values(AppConfig.from_values({}), ManagedSettings(mdns_advertise_afp=True))
        self.assertEqual([setting.setting_id for setting in settings_not_applied(wanted, installed)],
                         ["mdns_advertise_afp"])

    def test_disk_timers_compare_as_the_device_stores_them(self) -> None:
        installed = written({"TC_ATA_STANDBY": ""})
        for standby, changed in (("", False), (0, True), ("120", True)):
            with self.subTest(standby=standby):
                wanted = managed_flash_values(AppConfig.from_values({}), ManagedSettings(ata_standby=standby))
                self.assertEqual(bool(settings_not_applied(wanted, installed)), changed)

    def test_a_key_the_device_lacks_or_one_not_compared_is_not_reported(self) -> None:
        wanted = managed_flash_values(AppConfig.from_values({}), ManagedSettings(mdns_advertise_afp=True, debug_logging=True))
        installed = written({})
        del installed["MDNS_ADVERTISE_AFP"]
        # The CLI does not compare debug logging, which only a deploy flag
        # sets; the app's profile does.
        installed = {**installed, "SMBD_DEBUG_LOGGING": "0"}
        self.assertEqual(settings_not_applied(wanted, installed, SettingsSource.env().compared), [])
        self.assertEqual([setting.setting_id for setting in settings_not_applied(wanted, installed)], ["debug_logging"])


class ManagedSettingsTests(unittest.TestCase):
    def test_sent_settings_win_and_the_rest_come_from_the_env(self) -> None:
        config = AppConfig.from_values({"TC_MDNS_ADVERTISE_AFP": "false", "TC_REQUIRE_SMB_SIGNING": "true"})
        settings = resolve_managed_settings(managed_setting_params({"mdns_advertise_afp": True}), config)
        self.assertIs(settings.mdns_advertise_afp, True)
        self.assertIs(settings.require_smb_signing, True)
        self.assertIsNone(settings.debug_logging)
        values = managed_flash_values(config, settings)
        self.assertEqual((values["MDNS_ADVERTISE_AFP"], values["REQUIRE_SMB_SIGNING"]), (1, 1))

    def test_a_mistyped_or_null_setting_is_refused(self) -> None:
        for value in ("sometimes", None, ""):
            with self.subTest(value=value), self.assertRaises(AppOperationError) as raised:
                managed_setting_params({"vfs_aio_fork_enabled": value})
            self.assertEqual(raised.exception.code.value, "validation_failed")

    def test_a_null_debug_logging_is_left_out(self) -> None:
        self.assertIsNone(managed_setting_params({"debug_logging": None}).debug_logging)

    def test_every_compared_setting_is_one_the_app_sends_doctor(self) -> None:
        doctor_params = next(spec.params for spec in OPERATION_SPECS if spec.name == "doctor")
        self.assertLessEqual({setting.setting_id for setting in COMPARED_SETTINGS}, doctor_params)
        self.assertEqual(len({setting.setting_id for setting in COMPARED_SETTINGS}), len(COMPARED_SETTINGS))
        # The .env saves every setting the CLI compares.
        saved = MANAGED_SETTING_PARAMS
        self.assertLessEqual({setting.setting_id for setting in COMPARED_SETTINGS if not setting.app_only}, saved)


class DeployRuntimeConfigTests(unittest.TestCase):
    def test_a_setting_the_app_leaves_out_comes_from_the_env(self) -> None:
        config = AppConfig.from_values({"TC_REQUIRE_SMB_SIGNING": "true", "TC_MDNS_ADVERTISE_AFP": "true"})
        settings = resolve_managed_settings(managed_setting_params({"mdns_advertise_afp": False}), config)
        runtime = DeployRuntimeConfig.from_settings(settings, telemetry_enabled=True)
        self.assertIs(runtime.require_smb_signing, True)
        self.assertIs(runtime.mdns_advertise_afp, False)
        self.assertIs(runtime.rsync_enabled, False)
        self.assertIsNone(runtime.ata_idle_seconds)


class SettingsSourceTests(unittest.TestCase):
    def test_each_source_compares_what_its_deploy_reads(self) -> None:
        app_only = {setting.setting_id for setting in COMPARED_SETTINGS if setting.app_only}
        self.assertEqual(app_only, {"rsync_enabled", "debug_logging"})
        self.assertEqual(SettingsSource.app_profile(ManagedSettings()).compared, COMPARED_SETTINGS)
        self.assertFalse(app_only & {setting.setting_id for setting in SettingsSource.env().compared})
        self.assertEqual(SettingsSource.app_refused().compared, ())

    def test_only_the_app_profile_carries_settings(self) -> None:
        for kind in ("env", "app_refused"):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                SettingsSource(kind, ManagedSettings(mdns_advertise_afp=True))
        with self.assertRaises(ValueError):
            SettingsSource("profile")  # type: ignore[arg-type]
        self.assertEqual(SettingsSource.app_profile(ManagedSettings(rsync_enabled=True)).settings.rsync_enabled, True)

    def test_rsync_is_written_as_deploy_wrote_it(self) -> None:
        for rsync, written_value in ((None, "0"), (False, "0"), (True, "1")):
            with self.subTest(rsync=rsync):
                self.assertEqual(written({}, rsync_enabled=bool(rsync))["RSYNC_ENABLED"], written_value)
                self.assertEqual(str(managed_flash_values(AppConfig.from_values({}),
                                                          ManagedSettings(rsync_enabled=rsync))["RSYNC_ENABLED"]),
                                 written_value)

    def test_a_combination_the_device_refuses_is_refused(self) -> None:
        overrides = managed_setting_params({"any_protocol": True, "require_smb_encryption": True})
        with self.assertRaises(AppOperationError):
            resolve_managed_settings(overrides, AppConfig.from_values({}))


if __name__ == "__main__":
    unittest.main()


class AtaIdleLimitTests(unittest.TestCase):
    """samba/config.c runs an idle time past 86400 as 300, so nothing accepts a
    new one: doctor would warn after every install. One saved by an older
    release is read the way the device reads it, so it blocks nothing."""

    def test_the_env_check_accepts_an_idle_time_an_older_release_saved(self) -> None:
        validate = CONFIG_VALIDATORS["TC_ATA_IDLE_SECONDS"]
        for value in ("0", "300", "86400", "86401", "100000"):
            self.assertIsNone(validate(value, "TC_ATA_IDLE_SECONDS"), value)
        for value in ("", "-1", "soon"):
            self.assertIsNotNone(validate(value, "TC_ATA_IDLE_SECONDS"), value)

    def test_the_device_reading_of_an_idle_time(self) -> None:
        for value, expected in (
            ("0", "0"), ("0300", "300"), ("86400", "86400"), (" 600 ", "600"),
            ("86401", "300"), ("100000", "300"), ("", "300"), ("soon", "300"), ("-1", "300"),
        ):
            self.assertEqual(device_ata_idle_seconds(value), expected, value)

    def test_saving_settings_refuses_a_larger_idle_time(self) -> None:
        existing = {"TC_HOST": "root@10.0.0.2"}
        self.assertEqual(build_managed_config_env_values(existing, ata_idle_seconds=86400)["TC_ATA_IDLE_SECONDS"], "86400")
        for value in (86401, "86401", 86401.0):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "86400"):
                build_managed_config_env_values(existing, ata_idle_seconds=value)

    def test_saving_other_settings_keeps_an_old_larger_idle_time_as_300(self) -> None:
        for saved, expected in (("100000", "300"), ("86401", "300"), ("42", "42"), ("bad", "300"), ("", "300")):
            with self.subTest(saved=saved):
                existing = {"TC_HOST": "root@10.0.0.2", "TC_ATA_IDLE_SECONDS": saved}
                self.assertEqual(build_managed_config_env_values(existing)["TC_ATA_IDLE_SECONDS"], expected)
        # A value sent with the save replaces the old one.
        existing = {"TC_HOST": "root@10.0.0.2", "TC_ATA_IDLE_SECONDS": "100000"}
        self.assertEqual(build_managed_config_env_values(existing, ata_idle_seconds=600)["TC_ATA_IDLE_SECONDS"], "600")

    def test_an_app_request_refuses_a_larger_idle_time(self) -> None:
        self.assertEqual(managed_setting_params({"ata_idle_seconds": 86400}).ata_idle_seconds, 86400)
        with self.assertRaisesRegex(AppOperationError, "86400"):
            managed_setting_params({"ata_idle_seconds": 86401})

    def test_deploy_installs_an_old_larger_idle_time_as_300(self) -> None:
        for saved, expected in (("100000", "300"), ("86401", "300"), ("86400", "86400"), ("0", "0"), ("0600", "600")):
            with self.subTest(saved=saved):
                self.assertEqual(written({"TC_ATA_IDLE_SECONDS": saved})["ATA_IDLE_SECONDS"], expected)
        # A value passed for this deploy wins over the .env.
        self.assertEqual(written({"TC_ATA_IDLE_SECONDS": "100000"}, ata_idle_seconds=600)["ATA_IDLE_SECONDS"], "600")

    def test_deploy_refuses_a_larger_idle_time_passed_for_this_deploy(self) -> None:
        self.assertEqual(written({}, ata_idle_seconds=86400)["ATA_IDLE_SECONDS"], "86400")
        for override in (86401, "100000"):
            with self.subTest(override=override), self.assertRaisesRegex(ValueError, "86400"):
                written({}, ata_idle_seconds=override)

    def test_doctor_sees_nothing_to_install_for_an_old_larger_idle_time(self) -> None:
        # The CLI compares the .env's value, read as deploy writes it, with the
        # device's, read as samba/config.c does.
        config = AppConfig.from_values({"TC_ATA_IDLE_SECONDS": "100000"})
        installed = written({"TC_ATA_IDLE_SECONDS": "100000"})
        wanted = managed_flash_values(config)
        self.assertEqual(settings_not_applied(wanted, installed, SettingsSource.env().compared), [])
        installed["ATA_IDLE_SECONDS"] = "600"
        changed = settings_not_applied(wanted, installed, SettingsSource.env().compared)
        self.assertEqual([setting.setting_id for setting in changed], ["ata_idle_seconds"])
