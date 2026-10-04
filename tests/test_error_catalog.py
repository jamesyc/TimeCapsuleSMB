"""The app shows errors from its own catalogs: the error line from
backend.error.<operation>.<code> (else backend.error.<code>), and the
recovery guidance from backend.recovery.<localization_key>.title, .message and
.action.N, falling back to the helper's English for anything missing. These
tests keep every catalog complete and the English catalog in the helper's words.
"""
from __future__ import annotations

import json
import re
import unittest
from unittest import mock

from tests.test_summaries import LANGUAGES, catalog, placeholder_types
from timecapsulesmb.app import recovery
from timecapsulesmb.core.release import CLI_VERSION_CODE, RELEASE_TAG, release_major
from timecapsulesmb.device.probe import DeployedVersionProbeResult
from timecapsulesmb.device.storage import (
    MaStDiscoveryResult,
    MaStVolume,
    PayloadCandidateCheck,
    PayloadHomeSelection,
    VolumeMountResult,
)
from timecapsulesmb.services.activation import (
    OLDEST_ACTIVATABLE_VERSION_CODE,
    ActivationInstallError,
    require_compatible_install,
)
from timecapsulesmb.services.callbacks import OperationCallbacks
from timecapsulesmb.services.deploy import DeployDeviceError, select_deploy_payload_home
from timecapsulesmb.transport.errors import ssh_timeout_slow_device_message

RECOVERY_PART = re.compile(r"\.(title|message|action\.\d+)$")
APP_ERROR_PREFIXES = ("backend.error.", "backend.recovery.")


def text(raw: str) -> str:
    """A catalog value as the app shows it (the .strings escapes undone)."""
    return json.loads(f'"{raw}"')


def recovery_entries() -> list[recovery.RecoveryInfo]:
    tables = (recovery._DEFAULTS, recovery._OPERATION_CODE_RECOVERY, recovery._STAGE_RECOVERY)
    return [info for table in tables for info in table.values()] + [recovery._SSH_TIMEOUT_SLOW_DEVICE_RECOVERY]


def app_error_text(strings: dict[str, str], operation: str, code: str) -> str | None:
    """BackendErrorLocalization.message: the operation's own text, else the code's."""
    return strings.get(f"backend.error.{operation}.{code}") or strings.get(f"backend.error.{code}")


class RecoveryCatalogTests(unittest.TestCase):
    def test_every_recovery_entry_is_translated_in_the_helpers_english(self) -> None:
        english = catalog("en")
        for info in recovery_entries():
            key = info.localization_key
            with self.subTest(key=key):
                self.assertTrue(key)
                prefix = f"backend.recovery.{key}"
                self.assertEqual(text(english[f"{prefix}.title"]), info.title)
                message = text(english[f"{prefix}.message"])
                if "%@" in message:
                    # The app fills in the device name the helper sends.
                    self.assertEqual(message.replace("%@", "Office"), ssh_timeout_slow_device_message("Office"))
                else:
                    self.assertEqual(message, info.message)
                actions = [text(english[f"{prefix}.action.{i}"]) for i in range(1, len(info.actions) + 1)]
                self.assertEqual(actions, list(info.actions))
                # The app maps actions by position: an extra one would never show.
                self.assertNotIn(f"{prefix}.action.{len(info.actions) + 1}", english)

    def test_recovery_keys_name_one_entry_each(self) -> None:
        keys = [info.localization_key for info in recovery_entries()]
        self.assertEqual(len(keys), len(set(keys)))

    def test_recovery_for_sends_the_key_of_the_most_specific_entry(self) -> None:
        english = catalog("en")
        cases = (
            (("deploy", "remote_error", "verify_runtime_reboot"), "deploy.remote_error.verify_runtime_reboot"),
            (("deploy", "reboot_not_finished", "wait_for_reboot_up"), "deploy.reboot_not_finished"),
            (("fsck", "reboot_not_finished", "wait_for_reboot_up"), "reboot_not_finished"),
            (("set-ssh", "ssh_still_enabled", "wait_for_reboot_up"), "ssh_still_enabled"),
            (("deploy", "remote_error", "not_a_stage"), "remote_error"),
            (("deploy", "deploy_disk_not_confirmed", None), "deploy.deploy_disk_not_confirmed"),
            (("configure", "auth_failed", None), "configure.auth_failed"),
            (("deploy", "auth_failed", None), "auth_failed"),
            (("deploy", "an_unknown_code", None), "operation_failed"),
        )
        for (operation, code, stage), key in cases:
            with self.subTest(key=key):
                payload = recovery.recovery_for(operation, code, stage=stage)
                self.assertEqual(payload["localization_key"], key)
                self.assertEqual(text(english[f"backend.recovery.{key}.title"]), payload["title"])

    def test_every_language_has_every_error_and_recovery_string(self) -> None:
        english = {key: value for key, value in catalog("en").items() if key.startswith(APP_ERROR_PREFIXES)}
        for language in LANGUAGES:
            localized = catalog(language)
            for key, value in english.items():
                with self.subTest(language=language, key=key):
                    self.assertTrue(localized.get(key, "").strip(), "missing translation")
                    self.assertEqual(placeholder_types(localized[key]), placeholder_types(value))
            with self.subTest(language=language):
                extra = {key for key in localized if key.startswith(APP_ERROR_PREFIXES)} - set(english)
                self.assertEqual(extra, set())

    def test_catalogs_hold_no_recovery_text_the_helper_never_sends(self) -> None:
        keys = {info.localization_key for info in recovery_entries()}
        for key in catalog("en"):
            if key.startswith("backend.recovery."):
                with self.subTest(key=key):
                    self.assertIn(RECOVERY_PART.sub("", key.removeprefix("backend.recovery.")), keys)


class ErrorCodeCatalogTests(unittest.TestCase):
    VOLUME = MaStVolume("wd0", "dk2", "/Volumes/dk2", "Data", "f42bdb83-c265-5522-a087-25606a4d0abf", True, "hfs")

    def deploy_error(self, check: PayloadCandidateCheck) -> DeployDeviceError:
        with self.assertRaises(DeployDeviceError) as raised:
            select_deploy_payload_home(
                mock.Mock(),
                dry_run=False,
                payload_dir_name=".samba4",
                mount_wait_seconds=1,
                callbacks=OperationCallbacks(),
                wait_for_mast_volumes=mock.Mock(return_value=MaStDiscoveryResult((self.VOLUME,), 1, "")),
                select_payload_home=mock.Mock(return_value=PayloadHomeSelection(None, (check,))),
            )
        return raised.exception

    def test_each_unusable_deploy_volume_cause_has_its_own_app_text(self) -> None:
        checks = (
            PayloadCandidateCheck(self.VOLUME, VolumeMountResult(True, "use_volume_rcs=0 mounted=yes"), False),
            PayloadCandidateCheck(self.VOLUME, VolumeMountResult(False, "use_volume_rcs=1,1 mounted=yes"), None),
            PayloadCandidateCheck(self.VOLUME, VolumeMountResult(False, "use_volume_rcs=1,1 mounted=no"), None),
        )
        codes = [self.deploy_error(check).code for check in checks]
        self.assertEqual(codes, ["deploy_disk_not_writable", "deploy_disk_not_confirmed", "deploy_disk_not_mounted"])
        for language in LANGUAGES:
            strings = catalog(language)
            texts = [app_error_text(strings, "deploy", code) for code in codes]
            with self.subTest(language=language):
                self.assertTrue(all(texts), texts)
                self.assertEqual(len(set(texts)), len(texts), "two causes would show the same text")

    def test_each_activation_refusal_has_app_text(self) -> None:
        newer_major = (release_major(CLI_VERSION_CODE) + 1) * 10000
        cases = (
            (False, DeployedVersionProbeResult(None, None, "missing"), "runtime_not_installed"),
            (True, DeployedVersionProbeResult(None, None, "missing"), "runtime_outdated"),
            (True, DeployedVersionProbeResult("v3.0.0", OLDEST_ACTIVATABLE_VERSION_CODE - 100, "ok"), "runtime_outdated"),
            (True, DeployedVersionProbeResult("v9.0.0", newer_major, "ok"), "client_outdated"),
        )
        for present, version, code in cases:
            with self.subTest(code=code, version=version.release_tag):
                with mock.patch("timecapsulesmb.services.activation.flash_runtime_config_present_conn", return_value=present):
                    with mock.patch("timecapsulesmb.services.activation.read_deployed_version_conn", return_value=version):
                        with self.assertRaises(ActivationInstallError) as raised:
                            require_compatible_install(mock.Mock(), OperationCallbacks())
                self.assertEqual(raised.exception.code, code)
                for language in LANGUAGES:
                    with self.subTest(language=language):
                        self.assertTrue(app_error_text(catalog(language), "activate", code))

    def test_current_release_is_accepted_without_an_error(self) -> None:
        version = DeployedVersionProbeResult(RELEASE_TAG, CLI_VERSION_CODE, "ok")
        with mock.patch("timecapsulesmb.services.activation.flash_runtime_config_present_conn", return_value=True):
            with mock.patch("timecapsulesmb.services.activation.read_deployed_version_conn", return_value=version):
                require_compatible_install(mock.Mock(), OperationCallbacks())


if __name__ == "__main__":
    unittest.main()
