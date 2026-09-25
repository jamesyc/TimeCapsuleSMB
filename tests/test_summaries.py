from __future__ import annotations

import json
import re
import unittest
from pathlib import Path
from unittest import mock

from tests.fixtures import summary_payloads
from timecapsulesmb.app.context import AppOperationContext
from timecapsulesmb.app.events import AppEvent, EventSink
from timecapsulesmb.core.summaries import SUMMARY_KEYS, Summary
from timecapsulesmb.services.callbacks import OperationCallbacks
from timecapsulesmb.services.set_ssh import SetSshStatusResult, disable_set_ssh
from timecapsulesmb.transport.ssh import SshConnection


RESOURCES = Path(__file__).resolve().parents[1] / "macos/TimeCapsuleSMB/Sources/TimeCapsuleSMBApp/Resources"
LANGUAGES = ("en", "de", "es", "fr", "it", "lt", "nl", "pt", "ru", "zh-Hans")
STRING_LINE = re.compile(r'^"([^"]+)"\s*=\s*"((?:[^"\\]|\\.)*)";\s*$')
# %d/%ld/%lld/%i/%u are integers and %@ is a string. A plural variable
# (%#@name@) is always an integer; when .stringsdict files arrive, read the
# key and its NSStringFormatValueTypeKey from them as well.
PLACEHOLDER = re.compile(r"%(?:(\d+)\$)?(#@\w+@|l{0,2}[diu]|@)")


def catalog(language: str) -> dict[str, str]:
    path = RESOURCES / f"{language}.lproj" / "Localizable.strings"
    return {m.group(1): m.group(2) for line in path.read_text().splitlines() if (m := STRING_LINE.match(line))}


def placeholder_types(template: str) -> tuple[str, ...]:
    positional: dict[int, str] = {}
    sequential: list[str] = []
    for match in PLACEHOLDER.finditer(template.replace("%%", "")):
        kind = "str" if match.group(2) == "@" else "int"
        if match.group(1):
            positional[int(match.group(1))] = kind
        else:
            sequential.append(kind)
    if positional and sequential:
        raise AssertionError(f"mixes positional and sequential placeholders: {template}")
    if positional:
        if sorted(positional) != list(range(1, len(positional) + 1)):
            raise AssertionError(f"positional placeholders skip a position: {template}")
        return tuple(positional[i] for i in sorted(positional))
    return tuple(sequential)


def fixture_events() -> list[dict[str, object]]:
    return json.loads(summary_payloads.FIXTURE_PATH.read_text())


def event_key(event: dict[str, object]) -> tuple[str | None, list[object]]:
    if event["type"] == "log":
        return event.get("message_key"), event.get("message_args", [])  # type: ignore[return-value]
    payload = event["payload"]
    assert isinstance(payload, dict)
    return payload.get("summary_key"), payload.get("summary_args", [])


class SummaryTests(unittest.TestCase):
    def test_fields_carry_text_key_and_args(self) -> None:
        summary = Summary("hfs_volumes_found", "Found 2 mounted HFS volume(s).", (2,))

        self.assertEqual(summary.fields(), {
            "summary": "Found 2 mounted HFS volume(s).", "summary_key": "hfs_volumes_found", "summary_args": [2],
        })
        self.assertEqual(summary.message_fields(), {
            "message": "Found 2 mounted HFS volume(s).", "message_key": "hfs_volumes_found", "message_args": [2],
        })

    def test_rejects_unregistered_keys_and_mismatched_arguments(self) -> None:
        with self.assertRaisesRegex(ValueError, "unregistered summary key"):
            Summary("not_a_key", "text")
        for args in ((), ("2",), (2, 3), (True,), (2.0,)):
            with self.subTest(args=args):
                with self.assertRaisesRegex(ValueError, "expects"):
                    Summary("hfs_volumes_found", "text", args)

    def test_callbacks_send_keyed_messages_to_the_app_and_text_elsewhere(self) -> None:
        summary = Summary("waiting_device_boot", "Waiting a few seconds for device to boot...")
        plain: list[str] = []
        keyed: list[Summary] = []

        OperationCallbacks(log=plain.append).message(summary)
        OperationCallbacks(log=plain.append, log_summary=keyed.append).message(summary)
        OperationCallbacks(log=plain.append, log_summary=keyed.append).message("free text")

        self.assertEqual(plain, ["Waiting a few seconds for device to boot...", "free text"])
        self.assertEqual(keyed, [summary])

    def test_app_context_emits_keyed_log_events(self) -> None:
        events: list[AppEvent] = []
        context = AppOperationContext("deploy", EventSink(events.append, request_id="r"))

        context.to_operation_callbacks().message(Summary("waiting_device_activate", "Waiting a few seconds for device to activate..."))
        context.to_operation_callbacks().message("free text")

        keyed, plain = (event.to_jsonable() for event in events)
        self.assertEqual(keyed["type"], "log")
        self.assertEqual(keyed["message"], "Waiting a few seconds for device to activate...")
        self.assertEqual(keyed["message_key"], "waiting_device_activate")
        self.assertEqual(keyed["message_args"], [])
        self.assertNotIn("message_key", plain)


class SummaryProducerTests(unittest.TestCase):
    """Every branch emits the expected key; the English text is unchanged."""

    EXPECTED = {
        "capabilities": ("helper_capabilities_resolved", [], "Helper capabilities resolved."),
        "discover": ("discovered_devices", [2], "Discovered 2 device(s)."),
        "validate_install_failed": ("install_validation_failed", [], "Install validation failed."),
        "version_required": ("update_required", [], "Update required."),
        "version_available": ("update_available", [], "Update available."),
        "version_current": ("up_to_date", [], "TimeCapsuleSMB is up to date."),
        "version_unavailable": ("version_metadata_unavailable", [], "Version metadata is unavailable."),
        "ssh_status_reachable": ("ssh.reachable", [], "SSH is reachable."),
        "ssh_status_acp_only": ("ssh.acp_reachable_ssh_closed", [], "AirPort ACP is reachable, but SSH is closed."),
        "ssh_status_unreachable": ("ssh.unreachable", [], "AirPort ACP and SSH are not reachable."),
        "deploy_completed": ("deploy_completed", [], "Deployment completed."),
        "deploy_runtime_activated": ("deploy_completed", [], "Runtime activation complete."),
        "deploy_netbsd4_followup": ("activation_completed_followup", [], summary_payloads.NETBSD4_FOLLOWUP),
        "activation_already_active": ("activation_already_active", [], "NetBSD4 payload was already active."),
        "activation_completed": ("activation_completed", [], "NetBSD4 activation completed."),
        "activation_followup": ("activation_completed_followup", [], summary_payloads.NETBSD4_FOLLOWUP),
        "uninstall_unverified": ("uninstall_unverified", [], "Uninstall completed without post-reboot verification."),
        "fsck_volumes": ("hfs_volumes_found", [1], "Found 1 mounted HFS volume(s)."),
        "fsck_plan": ("fsck_plan_generated", [], "Dry-run plan generated for fsck."),
        "fsck_completed": ("fsck_completed", [], "Disk repair completed with fsck."),
        "fsck_failed": ("fsck_failed", [8], "fsck_hfs exited with status 8; the disk may still need repair."),
        "repair_xattrs": ("repair_xattrs_found", [3, 2], "Found 3 metadata issue(s), 2 repairable."),
        "doctor_fatal": ("doctor_found_fatal", [], "Doctor found one or more fatal problems."),
        "flash_backup": ("flash_backup_saved", ["/tmp/flash-backup"], "Flash backup saved to /tmp/flash-backup."),
        "flash_apple_stock_match": ("flash.apple_stock_match", [], "Active firmware bank matches Apple stock firmware."),
        "flash_apple_stock_mismatch_version": (
            "flash.apple_stock_mismatch_version", ["7.8.1"], "Active firmware bank does not match Apple stock firmware 7.8.1."),
        "flash_apple_all_match": ("flash.apple_all_match", [], "All candidate firmware banks match Apple stock firmware."),
        "flash_apple_none_match_version": (
            "flash.apple_none_match_version", ["7.8.1"], "No candidate firmware banks match Apple stock firmware 7.8.1."),
        "flash_apple_some_match": ("flash.apple_some_match", [1, 2], "1 of 2 candidate firmware banks match Apple stock firmware."),
        "flash_apple_some_match_version": (
            "flash.apple_some_match_version", [1, 2, "7.8.1"], "1 of 2 candidate firmware banks match Apple stock firmware 7.8.1."),
        "flash_restore_validated": ("flash.apple_restore_validated", [], "Apple restore firmware validated."),
        "flash_restore_validated_version": (
            "flash.apple_restore_validated_version", ["7.8.1"], "Apple restore firmware validated (version 7.8.1)."),
        "flash_restore_validated_product": (
            "flash.apple_restore_validated_product", ["119"], "Apple restore firmware validated (product 119)."),
        "flash_restore_validated_version_product": (
            "flash.apple_restore_validated_version_product", ["7.8.1", "119"],
            "Apple restore firmware validated (version 7.8.1, product 119)."),
        "flash_plan_satisfied": ("flash_plan_already_satisfied", [], "Flash plan is already satisfied; no write is needed."),
        "flash_patch_plan": ("flash.patch_plan_generated", [], "Flash patch plan generated."),
        "flash_restore_write_plan": ("flash.restore_write_plan_generated", [], "Flash restore write plan generated."),
        "flash_write_not_needed": ("flash_write_not_needed", [], "Flash write was not needed."),
        "flash_restore_write_rebooted": (
            "flash_restore_write_validated_rebooted", [], "Flash restore write validated; device rebooted."),
        "flash_restore_write_reboot_requested": (
            "flash_restore_write_validated_reboot_requested", [], "Flash restore write validated; reboot requested."),
        "flash_restore_write_manual_reboot": (
            "flash_restore_write_validated_manual_reboot", [], "Flash restore write validated; manual reboot required."),
        "flash_write_completed": ("flash_write_completed", [], "Flash write completed."),
        "log_waiting_boot": ("waiting_device_boot", [], "Waiting a few seconds for device to boot..."),
    }

    def test_each_branch_emits_its_key_arguments_and_unchanged_english(self) -> None:
        events = {row["name"]: row["event"] for row in summary_payloads.build()}
        for name, (key, args, text) in self.EXPECTED.items():
            with self.subTest(name=name):
                event = events[name]
                self.assertEqual(event_key(event), (key, args))
                self.assertEqual(event["message"] if event["type"] == "log" else event["payload"]["summary"], text)

    def test_unknown_flash_modes_keep_english_without_a_key(self) -> None:
        from timecapsulesmb.app import contracts

        plan = contracts.flash_plan_payload({"flash_plan": {"mode": "mystery"}})
        write = contracts.flash_write_payload({"write_outcome": {"status": "written", "mode": "mystery", "write_validated": True}})

        self.assertEqual(plan["summary"], "Flash mystery plan generated.")
        self.assertNotIn("summary_key", plan)
        self.assertEqual(write["summary"], "Flash mystery write validated.")
        self.assertNotIn("summary_key", write)

    def test_failed_fsck_result_requires_its_exit_status(self) -> None:
        from timecapsulesmb.app import contracts

        with self.assertRaisesRegex(ValueError, "exit status"):
            contracts.fsck_result_payload(device="/dev/dk2", mountpoint="/Volumes/dk2", error="failed")

    def test_ssh_status_summary_follows_port_state(self) -> None:
        cases = {
            (True, True): "ssh.reachable",
            (False, True): "ssh.reachable",
            (True, False): "ssh.acp_reachable_ssh_closed",
            (False, False): "ssh.unreachable",
        }
        for (acp, ssh), key in cases.items():
            with self.subTest(acp=acp, ssh=ssh):
                status = SetSshStatusResult(host="10.0.0.2", acp_port_reachable=acp, ssh_port_reachable=ssh)
                self.assertEqual(status.summary_key, key)
                Summary(status.summary_key, status.summary)

    def test_disable_paths_emit_their_keys(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")
        open_ssh = SetSshStatusResult(host="10.0.0.2", acp_port_reachable=True, ssh_port_reachable=True)
        closed_ssh = SetSshStatusResult(host="10.0.0.2", acp_port_reachable=True, ssh_port_reachable=False)
        disable = mock.Mock()

        noop = disable_set_ssh(connection, no_wait=False, initial=closed_ssh, disable_func=disable)
        requested = disable_set_ssh(connection, no_wait=True, initial=open_ssh, disable_func=disable)
        verified = disable_set_ssh(
            connection, no_wait=False, initial=open_ssh, disable_func=disable,
            wait_for_tcp_port_state=mock.Mock(return_value=True), wait_for_device_up_func=mock.Mock(return_value=True),
        )

        self.assertEqual(noop.summary_key, "ssh.already_disabled")
        self.assertEqual(requested.summary_key, "ssh.disable_requested")
        self.assertEqual(verified.summary_key, "ssh.disabled")
        self.assertEqual(disable.call_count, 2)
        for result in (noop, requested, verified):
            Summary(result.summary_key, result.summary)


class SummaryCatalogTests(unittest.TestCase):
    def test_every_key_is_translated_with_matching_placeholders(self) -> None:
        for language in LANGUAGES:
            strings = catalog(language)
            for key, types in SUMMARY_KEYS.items():
                with self.subTest(language=language, key=key):
                    template = strings.get(f"backend.summary.{key}")
                    self.assertIsNotNone(template, "missing translation")
                    self.assertEqual(placeholder_types(template or ""), types, template)

    def test_catalogs_hold_no_summary_keys_the_helper_never_sends(self) -> None:
        # The app resolves backend.summary.* only through keys the helper
        # sends, so any other such entry is dead text left for translators.
        for language in LANGUAGES:
            with self.subTest(language=language):
                keys = {key.removeprefix("backend.summary.") for key in catalog(language) if key.startswith("backend.summary.")}
                self.assertEqual(keys - set(SUMMARY_KEYS), set())

    def test_placeholder_parser_accepts_reordered_and_plural_forms(self) -> None:
        self.assertEqual(placeholder_types("%2$@ then %1$lld"), ("int", "str"))
        self.assertEqual(placeholder_types("%#@devices@ in %@"), ("int", "str"))
        self.assertEqual(placeholder_types("100%% of %d"), ("int",))
        with self.assertRaises(AssertionError):
            placeholder_types("%1$@ and %d")


class SummaryFixtureTests(unittest.TestCase):
    def test_fixture_matches_the_payload_builders(self) -> None:
        self.assertEqual(summary_payloads.FIXTURE_PATH.read_text(), summary_payloads.render())

    def test_fixture_covers_every_registered_key(self) -> None:
        keys = {event_key(row["event"])[0] for row in fixture_events()}
        self.assertEqual(set(SUMMARY_KEYS) - keys, set())

    def test_fixture_events_carry_registered_keys_with_typed_arguments(self) -> None:
        for row in fixture_events():
            with self.subTest(name=row["name"]):
                key, args = event_key(row["event"])
                self.assertIn(key, SUMMARY_KEYS)
                Summary(key, "text", tuple(args))  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
