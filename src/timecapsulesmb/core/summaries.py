"""Stable keys for helper result summaries.

Every result payload carries its English ``summary`` (for the CLI, telemetry
and older consumers) plus a ``summary_key`` and positional ``summary_args``.
The macOS app translates ``backend.summary.<summary_key>`` from its
``Localizable`` catalogs and falls back to the English text.

This registry is a contract with those files: each key lists its argument
types, which must match the placeholders of every translation (``"int"`` for
``%d``/``%ld``/``%lld`` and ``%#@plural@`` variables, ``"str"`` for ``%@``). To
add or rename a key, update this registry, all ten ``Localizable.strings`` (or,
for a count sentence, ``Localizable.stringsdict``) files and the summary contract
fixture (``python -m tests.fixtures.summary_payloads --write``) together;
``tests/test_summaries.py`` checks all three.
"""
from __future__ import annotations

from dataclasses import dataclass


SUMMARY_KEYS: dict[str, tuple[str, ...]] = {
    "helper_capabilities_resolved": (),
    "operation_exited": (),
    "discovered_devices": ("int",),
    "install_validation_passed": (),
    "install_validation_failed": (),
    "telemetry_enabled": (),
    "telemetry_disabled": (),
    "update_required": (),
    "update_available": (),
    "up_to_date": (),
    "version_metadata_unavailable": (),
    "configuration_saved": (),
    "settings_synchronized": (),
    "reachability.all_reachable": (),
    "reachability.ssh_only": (),
    "reachability.smb_only": (),
    "reachability.unreachable": (),
    "reachability.auth_failed": (),
    "reachability.no_candidates": (),
    "ssh.reachable": (),
    "ssh.acp_reachable_ssh_closed": (),
    "ssh.unreachable": (),
    "ssh.already_enabled": (),
    "ssh.enable_requested": (),
    "ssh.configured": (),
    "ssh.already_disabled": (),
    "ssh.disable_requested": (),
    "ssh.disabled": (),
    "deploy_completed": (),
    "activation_already_active": (),
    "activation_completed": (),
    "activation_completed_followup": (),
    "waiting_device_boot": (),
    "waiting_device_activate": (),
    "uninstall_completed": (),
    "uninstall_unverified": (),
    "hfs_volumes_found": ("int",),
    "fsck_plan_generated": (),
    "fsck_completed": (),
    "fsck_failed": ("int",),
    "repair_xattrs_found": ("int", "int"),
    "repair_xattrs_no_safe_repairs": ("int",),
    "repair_xattrs_approval_required": (),
    "repair_xattrs_unresolved": ("int",),
    "doctor_checks_passed": (),
    "doctor_found_fatal": (),
    "flash_backup_saved": ("str",),
    "flash.apple_all_match": (),
    "flash.apple_all_match_version": ("str",),
    "flash.apple_none_match": (),
    "flash.apple_none_match_version": ("str",),
    "flash.apple_some_match": ("int", "int"),
    "flash.apple_some_match_version": ("int", "int", "str"),
    "flash.apple_stock_match": (),
    "flash.apple_stock_match_version": ("str",),
    "flash.apple_stock_mismatch": (),
    "flash.apple_stock_mismatch_version": ("str",),
    "flash.apple_restore_validated": (),
    "flash.apple_restore_validated_version": ("str",),
    "flash.apple_restore_validated_product": ("str",),
    "flash.apple_restore_validated_version_product": ("str", "str"),
    "flash.patch_plan_generated": (),
    "flash.restore_plan_generated": (),
    "flash.patch_write_plan_generated": (),
    "flash.restore_write_plan_generated": (),
    "flash_plan_already_satisfied": (),
    "flash_write_not_needed": (),
    "flash_patch_write_validated_power_cycle": (),
    "flash_restore_write_validated_rebooted": (),
    "flash_restore_write_validated_reboot_requested": (),
    "flash_restore_write_validated_manual_reboot": (),
    "flash_write_completed": (),
}


def english_count(count: int, singular: str, plural: str) -> str:
    """The count and its noun in English: "1 device", "0 devices", "2 devices".

    Only the English ``text`` uses this; translations choose their own plural
    forms from the count argument (see ``Localizable.stringsdict``).
    """
    return f"{count} {singular if count == 1 else plural}"


def _arg_type(value: object) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return "int"
    if isinstance(value, str):
        return "str"
    return None


@dataclass(frozen=True)
class Summary:
    """An English summary with the stable key and arguments that translate it."""

    key: str
    text: str
    args: tuple[object, ...] = ()

    def __post_init__(self) -> None:
        expected = SUMMARY_KEYS.get(self.key)
        if expected is None:
            raise ValueError(f"unregistered summary key: {self.key}")
        actual = tuple(_arg_type(arg) for arg in self.args)
        if actual != expected:
            raise ValueError(f"summary key {self.key} expects {expected} arguments, got {actual}")

    def fields(self) -> dict[str, object]:
        return {"summary": self.text, "summary_key": self.key, "summary_args": list(self.args)}

    def message_fields(self) -> dict[str, object]:
        return {"message": self.text, "message_key": self.key, "message_args": list(self.args)}
