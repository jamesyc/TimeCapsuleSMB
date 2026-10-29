"""The app shows errors from its own catalogs: an error box's guidance from
backend.recovery.<localization_key>.title, .message and .action.N (the
helper's English error text below it is diagnostics), and one-line errors as
that guidance's title. These tests keep every catalog complete, the English
catalog in the helper's words, and every ErrorCode with guidance of its own.
"""
from __future__ import annotations

import ast
import json
import pathlib
import re
import unittest
from unittest import mock

from tests.test_summaries import LANGUAGES, catalog, placeholder_types
from timecapsulesmb.app import recovery
from timecapsulesmb.app.ops import OPERATION_SPECS
from timecapsulesmb.core.error_codes import GENERIC_CODES, ErrorCode
from timecapsulesmb.core.release import CLI_VERSION_CODE, RELEASE_TAG, release_major
from timecapsulesmb.device.probe import DISK_PAYLOAD_PROBLEMS, DISK_UNREADABLE_PROBLEMS, DeployedVersionProbeResult
from timecapsulesmb.device.storage import (
    VolumeClaims,
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
APP_ERROR_PREFIXES = ("backend.recovery.",)


def text(raw: str) -> str:
    """A catalog value as the app shows it (the .strings escapes undone)."""
    return json.loads(f'"{raw}"')


def recovery_entries() -> list[recovery.RecoveryInfo]:
    tables = (recovery._DEFAULTS, recovery._OPERATION_CODE_RECOVERY, recovery._STAGE_RECOVERY)
    # One entry can serve several stages.
    entries = {id(info): info for table in tables for info in table.values()}
    return [*entries.values(), recovery._SSH_TIMEOUT_SLOW_DEVICE_RECOVERY]


def app_error_line(strings: dict[str, str], operation: str, code: str) -> str | None:
    """BackendErrorViewModel.message: the title of the guidance the helper sends."""
    return strings.get(f"backend.recovery.{recovery.recovery_for(operation, code)['localization_key']}.title")


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
            # Adding a device and turning SSH on show the same off-network guidance.
            (("configure", "device_off_network", None), "device_off_network"),
            (("set-ssh", "device_off_network", None), "device_off_network"),
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


# configure turns ssh_unreachable into another code before the app sees it,
# only telemetry records a client that disconnected, and the app answers
# device_already_saved by setting up the saved device instead, dropping the
# stopped run from Activity.
CODES_THE_APP_NEVER_SHOWS = frozenset({
    ErrorCode.SSH_UNREACHABLE, ErrorCode.CLIENT_DISCONNECTED, ErrorCode.DEVICE_ALREADY_SAVED,
})


class RecoveryCoverageTests(unittest.TestCase):
    def test_every_error_code_has_guidance_of_its_own(self) -> None:
        # The generic entries say only that something failed; a code the
        # helper chose for a known cause must say what to do about it.
        operations = [spec.name for spec in OPERATION_SPECS]
        generic_keys = {code.value for code in GENERIC_CODES}
        for code in ErrorCode:
            if code in GENERIC_CODES or code in CODES_THE_APP_NEVER_SHOWS:
                continue
            with self.subTest(code=code):
                keys = {recovery.recovery_for(operation, code)["localization_key"] for operation in operations}
                self.assertFalse(keys <= generic_keys, keys)

    def test_codes_the_app_never_shows_have_no_guidance(self) -> None:
        # Guidance for one of them would be text no user can reach.
        for code in CODES_THE_APP_NEVER_SHOWS:
            with self.subTest(code=code):
                self.assertEqual(recovery.recovery_for("configure", code)["localization_key"], ErrorCode.OPERATION_FAILED)

    def test_every_error_code_is_named_outside_the_enum_and_recovery_table(self) -> None:
        # A code nothing else names is guidance no user can reach. The parse
        # reads each ErrorCode.X the helper's code uses, in a raise, a map or
        # a comparison: it catches a code nothing uses at all.
        source = pathlib.Path(recovery.__file__).resolve().parents[1]
        used: set[str] = set()
        for path in source.rglob("*.py"):
            if path.name in ("error_codes.py", "recovery.py"):
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "ErrorCode":
                    used.add(node.attr)
        self.assertEqual({code.name for code in ErrorCode} - used, set())

    def test_every_recovery_entry_is_keyed_by_an_error_code(self) -> None:
        codes = (
            list(recovery._DEFAULTS)
            + [code for _, code in recovery._OPERATION_CODE_RECOVERY]
            + [code for _, code, _ in recovery._STAGE_RECOVERY]
        )
        for code in codes:
            with self.subTest(code=code):
                self.assertIsInstance(code, ErrorCode)


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
                claims=VolumeClaims(),
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
            texts = [app_error_line(strings, "deploy", code) for code in codes]
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
                        self.assertTrue(app_error_line(catalog(language), "activate", code))

    def test_every_payload_verdict_points_to_its_fix_everywhere(self) -> None:
        # Raised by activation, a deploy's runtime check and the restart
        # after a disk repair alike, for every verdict in the manager's
        # title. Each cause has its own line. A reinstall fixes the missing
        # and damaged ones; one the disk could not read needs a new disk, and
        # its guidance must not send the user to Install.
        codes = [problem.code for problem in DISK_PAYLOAD_PROBLEMS]
        unreadable = {problem.code for problem in DISK_UNREADABLE_PROBLEMS}
        self.assertEqual(len(set(codes)), len(codes))
        self.assertEqual(unreadable, {ErrorCode.PAYLOAD_UNREADABLE_ON_DISK, ErrorCode.RSYNC_UNREADABLE_ON_DISK})
        for code in codes:
            for operation in ("activate", "deploy", "fsck"):
                with self.subTest(code=code, operation=operation):
                    payload = recovery.recovery_for(operation, code)
                    self.assertEqual(payload["localization_key"], code)
                    self.assertEqual(payload["suggested_operation"], None if code in unreadable else "deploy")
                    for language in LANGUAGES:
                        with self.subTest(language=language):
                            strings = catalog(language)
                            texts = [app_error_line(strings, operation, other) for other in codes]
                            self.assertTrue(app_error_line(strings, operation, code))
                            self.assertEqual(len(set(texts)), len(texts))

    def test_a_failing_disk_has_one_card_and_line_wherever_it_is_found(self) -> None:
        # Raised by a deploy, Activate and the restart after a disk repair: the
        # way out is the same, and the line, a title, suggests no reinstall.
        for operation in ("activate", "deploy", "fsck"):
            with self.subTest(operation=operation):
                payload = recovery.recovery_for(operation, "disk_failing")
                self.assertEqual(payload["localization_key"], "disk_failing")
                self.assertIsNone(payload["suggested_operation"])
                self.assertEqual(payload["action_ids"], [])
        for language in LANGUAGES:
            with self.subTest(language=language):
                strings = catalog(language)
                line = app_error_line(strings, "activate", "disk_failing")
                self.assertTrue(line)
                self.assertEqual(app_error_line(strings, "deploy", "disk_failing"), line)
                self.assertEqual(app_error_line(strings, "fsck", "disk_failing"), line)

    def test_file_sharing_that_did_not_restart_after_fsck_has_its_own_line(self) -> None:
        # The line says what failed: file sharing, not the disk repair.
        for language in LANGUAGES:
            with self.subTest(language=language):
                strings = catalog(language)
                line = app_error_line(strings, "fsck", "runtime_not_restarted")
                self.assertTrue(line)
                self.assertNotEqual(line, app_error_line(strings, "fsck", "remote_error"))

    def test_each_disk_repair_without_a_result_has_its_own_app_text(self) -> None:
        # An earlier repair still running and a session lost mid-repair need
        # different advice from "fsck did not run".
        for code in (ErrorCode.FSCK_ALREADY_RUNNING, ErrorCode.FSCK_SESSION_DROPPED):
            with self.subTest(code=code):
                self.assertEqual(recovery.recovery_for("fsck", code)["localization_key"], f"fsck.{code}")
                for language in LANGUAGES:
                    with self.subTest(language=language):
                        strings = catalog(language)
                        line = app_error_line(strings, "fsck", code)
                        self.assertTrue(line)
                        self.assertNotEqual(line, app_error_line(strings, "fsck", "remote_error"))

    def test_current_release_is_accepted_without_an_error(self) -> None:
        version = DeployedVersionProbeResult(RELEASE_TAG, CLI_VERSION_CODE, "ok")
        with mock.patch("timecapsulesmb.services.activation.flash_runtime_config_present_conn", return_value=True):
            with mock.patch("timecapsulesmb.services.activation.read_deployed_version_conn", return_value=version):
                require_compatible_install(mock.Mock(), OperationCallbacks())


if __name__ == "__main__":
    unittest.main()
