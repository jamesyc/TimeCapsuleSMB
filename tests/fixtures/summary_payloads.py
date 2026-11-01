"""Generate the helper summary contract fixture shared with the macOS app.

Every branch of every result summary (and each keyed log message) is built by
the real payload builders and serialized through the real event sink, so the
Swift tests decode exactly what the helper emits.

    python -m tests.fixtures.summary_payloads --write
"""
from __future__ import annotations

import argparse
import contextlib
import functools
import json
import sys
from pathlib import Path

from types import SimpleNamespace
from unittest import mock

from tests import flash_fixtures
from tests.reboot_support import FakeAcpDevice
from timecapsulesmb.app import contracts
from timecapsulesmb.app.events import AppEvent, EventSink
from timecapsulesmb.app.ops.configure import SETTINGS_SYNCHRONIZED
from timecapsulesmb.app.service import OPERATION_EXITED
from timecapsulesmb.checks.models import CheckResult
from timecapsulesmb.core.config import AppConfig
from timecapsulesmb.core.messages import netbsd4_activation_summary
from timecapsulesmb.core.summaries import Summary
from timecapsulesmb.deploy.migration import OversizedSummary, OversizedValue, SkippedEntry, SkippedSummary
from timecapsulesmb.flash import PATCHED_LOGIN_SCRIPT, STOCK_LOGIN_NETBSD4_DUMMY, FlashInspection, inspect_flash_banks
from timecapsulesmb.flash_payloads import AcpFlashPayload, AppleFirmwareMatch
from timecapsulesmb.flash_workflow import (
    BankAppleFirmwareMatch,
    FlashPlan,
    plan_check_apple,
    plan_download_only,
    plan_patch_primary,
    plan_restore_apple,
)
from timecapsulesmb.services.flash import (
    FlashInputs,
    manifest_from_inspection,
    record_post_write_action,
    record_write_outcome,
    save_acp_flash_payload,
)
from timecapsulesmb.services.maintenance import fsck_failure_message, fsck_plan_to_jsonable, FsckTarget
from timecapsulesmb.services.reachability import ReachabilityCheck, ReachabilityResult, result_from_checks, run_reachability
from timecapsulesmb.services.deploy import (
    WAITING_FOR_PREVIOUS_MIGRATION,
    _oversized_summaries,
    _skipped_summaries,
    unreadable_folder_summary,
)
from timecapsulesmb.services.set_ssh import SetSshResult, SetSshStatusResult, disable_set_ssh, enable_set_ssh
from timecapsulesmb.services.version_check import VersionCheckResult
from timecapsulesmb.transport.ssh import SshConnection


FIXTURE_PATH = (
    Path(__file__).resolve().parents[2]
    / "macos/TimeCapsuleSMB/Tests/TimeCapsuleSMBAppTests/Fixtures/summary_payloads.json"
)

FSCK_TARGET = FsckTarget(device="/dev/dk2", mountpoint="/Volumes/dk2", name="Data", builtin=True)


def _set_ssh_results() -> list[tuple[str, SetSshResult]]:
    """Every set-ssh result branch, from the real enable/disable flows with the
    device calls mocked."""
    connection = SshConnection("root@10.0.0.2", "pw", "")
    ssh_open = SetSshStatusResult(host="10.0.0.2", acp_port_reachable=True, ssh_port_reachable=True)
    ssh_closed = SetSshStatusResult(host="10.0.0.2", acp_port_reachable=True, ssh_port_reachable=False)
    with (
        mock.patch("timecapsulesmb.services.set_ssh.enable_ssh_with_port_preflight", side_effect=lambda host, *_args, **_kwargs: host),
        mock.patch("timecapsulesmb.services.set_ssh.disable_ssh_over_ssh"),
        # Disabling restarts the device, so it runs the restart guard (its own
        # tests are in tests/test_wipe_disk.py): here it finds nothing, without
        # an SSH read of the made-up address.
        mock.patch("timecapsulesmb.services.storage.require_no_in_memory_volumes"),
    ):
        results = [
            ("ssh_already_enabled", enable_set_ssh(connection, no_wait=False, initial=ssh_open)),
            ("ssh_already_disabled", disable_set_ssh(connection, no_wait=False, initial=ssh_closed)),
        ]
        with FakeAcpDevice().patched():
            results.append(("ssh_enable_requested", enable_set_ssh(connection, no_wait=True, initial=ssh_closed)))
            results.append(("ssh_disable_requested", disable_set_ssh(connection, no_wait=True, initial=ssh_open)))
        with FakeAcpDevice(ssh_open=False).patched():
            results.append(("ssh_configured", enable_set_ssh(connection, no_wait=False, initial=ssh_closed)))
        with FakeAcpDevice(ssh_up_after_boot=None).patched():
            results.append(("ssh_disabled", disable_set_ssh(connection, no_wait=False, initial=ssh_open)))
    order = ("ssh_already_enabled", "ssh_enable_requested", "ssh_configured",
             "ssh_already_disabled", "ssh_disable_requested", "ssh_disabled")
    return sorted(results, key=lambda item: order.index(item[0]))


def _reachability(ssh: str | None, smb: str | None, auth: str | None = None) -> ReachabilityResult:
    """A reachability result from the real check classifier."""
    checks = [ReachabilityCheck(id=check_id, status=status, message="checked", host="10.0.0.2")
              for check_id, status in (("ssh_port", ssh), ("smb_port", smb), ("ssh_auth", auth)) if status]
    return result_from_checks(ssh_target="root@10.0.0.2", smb_hosts=["10.0.0.2"], checks=checks)


def _no_reachability_candidates() -> ReachabilityResult:
    config = AppConfig.from_values({}, path=Path("/nonexistent/.env"), exists=False, file_values={})
    return run_reachability(config, {})


# The flash operation passes its backup directory through every flash payload.
BACKUP_DIR = "/tmp/flash-backup"
FLASH_OS_RELEASE = "4.0_STABLE"
FLASH_SYAP = "119"


def _flash_bank(release: bytes = b"current", *, login: bytes = STOCK_LOGIN_NETBSD4_DUMMY) -> bytes:
    """A small kernel bank; its release decides whether it is an active
    candidate, its LOGIN how the helper classifies it."""
    return flash_fixtures.make_bank(release=b"NetBSD " + FLASH_OS_RELEASE.encode() + b" #0: " + release, login=login)


def _old_flash_bank() -> bytes:
    return flash_fixtures.make_bank(release=b"NetBSD 4.0_BETA2 #0: old")


def _flash_inputs(primary: bytes, secondary: bytes, *, live_login: bytes = STOCK_LOGIN_NETBSD4_DUMMY) -> FlashInputs:
    """What reading these banks returns, with the checksums ACPd reports and
    the LOGIN the running system booted with."""
    return flash_fixtures.flash_inputs(primary, secondary, syap=FLASH_SYAP, cks1=flash_fixtures.acpd_checksum(primary),
                                       cks2=flash_fixtures.acpd_checksum(secondary), live_login=live_login)


def _flash_inspection(inputs: FlashInputs, *, patch_candidate: bool = False) -> FlashInspection:
    # As save_flash_backup inspects what it read; planning a patch also builds
    # the patched primary, which needs zopfli, here stubbed.
    with flash_fixtures.zopfli_available() if patch_candidate else contextlib.nullcontext():
        return inspect_flash_banks(primary_data=inputs.primary, secondary_data=inputs.secondary, cks1=inputs.cks1,
                                   cks2=inputs.cks2, os_release=FLASH_OS_RELEASE, live_login=inputs.live_login,
                                   build_primary_patch_candidate=patch_candidate)


def _flash_backup(primary: bytes, secondary: bytes, *, live_login: bytes = STOCK_LOGIN_NETBSD4_DUMMY) -> dict[str, object]:
    """A backup result as save_flash_backup builds it from these two banks."""
    inputs = _flash_inputs(primary, secondary, live_login=live_login)
    target = SimpleNamespace(acp_host="10.0.0.2", compatibility=SimpleNamespace(os_release=FLASH_OS_RELEASE))
    return contracts.flash_backup_payload(manifest_from_inspection(
        operation="read_only", inspection=_flash_inspection(inputs), target=target, inputs=inputs,
        backup_dir=Path(BACKUP_DIR)))


def _apple_firmware_match(matched: bool, version: str | None) -> AppleFirmwareMatch:
    return AppleFirmwareMatch(
        matched=matched, template_source="catalog", template_path=None, template_product_id=FLASH_SYAP,
        template_version=version, template_sha256="template-sha", inner_sha256="inner-sha", inner_size=123,
        key_id="key-one", inner_model=int(FLASH_SYAP), inner_version=0x00070801,
    )


def _acp_payload(prefix: bytes, *, version: str | None = "7.8.1", product: str | None = FLASH_SYAP,
                 login: str = "stock") -> AcpFlashPayload:
    """A firmware payload as the helper builds one from Apple's firmware,
    which these fixtures never download."""
    return AcpFlashPayload(
        data=b"basebinary", expected_prefix=prefix, expected_login_classification=login,
        template_source="catalog", template_path=Path("/tmp/firmware.basebinary"), template_product_id=product,
        template_version=version, template_sha256="template-sha", payload_sha256="payload-sha", key_id="key-one",
        inner_model=int(product or FLASH_SYAP), inner_version=0x00070801, inner_payload_size=len(prefix),
    )


def _flash_plan(mode: str, **plan: object) -> dict[str, object]:
    return contracts.flash_plan_payload({"backup_dir": BACKUP_DIR, "flash_plan": {"mode": mode, **plan}})


def _plan_result(plan: FlashPlan) -> dict[str, object]:
    """The parts of a plan result the app reads, as plan_flash_from_backup
    records them: the plan, and where save_acp_flash_payload saves its payload,
    under the key it files it as. The helper's record also carries the rest of
    the backup manifest, such as its banks, which the app does not read."""
    with mock.patch.object(Path, "write_bytes"):
        path = save_acp_flash_payload(backup_dir=Path(BACKUP_DIR), plan=plan)
    key = plan.mode if plan.target_name is None else f"{plan.target_name}_{plan.mode}"
    files = {} if path is None else {f"{key}_basebinary_payload": str(path)}
    return contracts.flash_plan_payload({"backup_dir": BACKUP_DIR, "files": files, "flash_plan": plan.to_jsonable()})


@functools.cache
def _patch_plan() -> FlashPlan:
    """Plan Patch for a stock primary bank."""
    inspection = _flash_inspection(_flash_inputs(_flash_bank(), _old_flash_bank()), patch_candidate=True)
    patched = _acp_payload(b"PATCHED!" * 8, login="already_patched")
    with mock.patch("timecapsulesmb.flash_workflow.build_patch_payload_for_bank", return_value=patched):
        return plan_patch_primary(inspection, syap=FLASH_SYAP, firmware_template=None)


@functools.cache
def _satisfied_patch_plan() -> FlashPlan:
    """Plan Patch for a primary bank that is already patched: nothing to write."""
    patched = _flash_bank(login=PATCHED_LOGIN_SCRIPT)
    inputs = _flash_inputs(patched, _old_flash_bank(), live_login=PATCHED_LOGIN_SCRIPT)
    return plan_patch_primary(_flash_inspection(inputs), syap=FLASH_SYAP, firmware_template=None)


@functools.cache
def _restore_plan() -> FlashPlan:
    """Plan Restore for a primary bank that differs from Apple's firmware."""
    inspection = _flash_inspection(_flash_inputs(_flash_bank(), _old_flash_bank()))
    with mock.patch("timecapsulesmb.flash_workflow.build_restore_payload_for_bank",
                    return_value=_acp_payload(b"APPLE!!!" * 8)):
        return plan_restore_apple(inspection, syap=FLASH_SYAP, firmware_template=None)


@functools.cache
def _secondary_refresh_plan() -> FlashPlan:
    """Plan Restore when the secondary bank is not a valid backup: the helper
    rewrites it from Apple's newest firmware. It does that only for the models
    in SECONDARY_REFRESH_SYAPS, which full-size banks of 116 stand in for."""
    prefix = flash_fixtures.newest_firmware_prefix()
    payload = _acp_payload(prefix, product="116")
    with (
        mock.patch("timecapsulesmb.flash_workflow.build_download_payload_for_syap", return_value=payload),
        mock.patch("timecapsulesmb.flash_workflow.build_restore_payload_for_bank", return_value=payload),
    ):
        return plan_restore_apple(flash_fixtures.inspect_full_banks(), syap="116", firmware_template=None)


def _check_apple(matched: list[bool], version: str | None) -> dict[str, object]:
    """Check Apple Firmware from the real planner: one candidate bank, or two
    when several results are given, each matching as listed."""
    second = _flash_bank(b"other") if len(matched) > 1 else _old_flash_bank()
    inspection = _flash_inspection(_flash_inputs(_flash_bank(), second))

    def matches(candidates, **_kwargs):
        assert len(candidates) == len(matched), f"{len(candidates)} candidate banks for {len(matched)} results"
        return tuple(BankAppleFirmwareMatch(bank=bank.name, match=_apple_firmware_match(result, version))
                     for bank, result in zip(candidates, matched))

    with mock.patch("timecapsulesmb.flash_workflow._match_apple_firmware_for_candidates", side_effect=matches):
        return _plan_result(plan_check_apple(inspection, syap=FLASH_SYAP, firmware_template=None,
                                             firmware_version=version))


def _validate_apple_restore(version: str | None, product: str | None) -> dict[str, object]:
    """Validate Apple Restore Firmware from the real planner, with the download
    stubbed."""
    primary = _flash_bank()
    payload = _acp_payload(primary[:64], version=version, product=product)
    with mock.patch("timecapsulesmb.flash_workflow.build_download_payload_for_syap", return_value=payload):
        return _plan_result(plan_download_only(_flash_inspection(_flash_inputs(primary, _old_flash_bank())),
                                               syap=FLASH_SYAP, firmware_template=None, firmware_version=version))


def _flash_write(plan: FlashPlan, status: str = "validated", **post_write: object) -> dict[str, object]:
    """A write result as the write operation records it: record_write_outcome
    with `status`, then, after a validated write, record_post_write_action with
    `post_write`."""
    bundle = SimpleNamespace(manifest={"backup_dir": BACKUP_DIR}, backup_dir=Path(BACKUP_DIR))
    with mock.patch("timecapsulesmb.services.flash.save_flash_manifest"):
        record_write_outcome(bundle=bundle, plan=plan, status=status, write_validated=status == "validated",
                             write_may_have_modified_device=status != "not_needed")
        if post_write:
            record_post_write_action(bundle=bundle, **post_write)
    return contracts.flash_write_payload(bundle.manifest)


def _no_reboot(action: str) -> dict[str, object]:
    return {"post_write_action": action, "reboot_requested": False, "rebooted": False, "waited_after_reboot": False}


def _kept_values_logs() -> list[tuple[str, str, str, bool, object]]:
    """The metadata migration's kept-values report, for mixes that together
    use every sentence key with both a count of 1 and a larger count."""
    big = OversizedValue("tdb", "/Volumes/dk2/Music/song", "com.apple.big", 41409)
    folder = OversizedValue("appledouble", "/Volumes/dk2/pass.txt.rtfd", "com.apple.ResourceFork", 64, "folder_fork")
    tiny = OversizedValue("appledouble", "/Volumes/dk2/b.rtfd", "com.apple.ResourceFork", 1, "folder_fork")
    mixes = {
        "one_folder": OversizedSummary(appledouble=1, values=[tiny], folder_forks=1),
        "one_in_place": OversizedSummary(tdb=1, values=[big], database_outcome="in_place"),
        "mixed_quarantined": OversizedSummary(tdb=1, appledouble=2, values=[big, folder, tiny], folder_forks=2,
                                              database_outcome="quarantined"),
        "many_quarantined": OversizedSummary(tdb=12, values=[big], database_outcome="quarantined"),
        "two_in_place": OversizedSummary(tdb=2, values=[big], database_outcome="in_place"),
    }
    return [
        (f"log_kept_{name}_{index}", "log", "deploy", True, line)
        for name, summary in mixes.items()
        for index, line in enumerate(_oversized_summaries(summary))
    ]


def _skipped_logs() -> list[tuple[str, str, str, bool, object]]:
    """The metadata migration's skipped-entries report: every reason with a
    count of 1 and a larger count, and a list cut short by its examples."""
    long_name = SkippedEntry("name_too_long", "/Volumes/dk2/Music/Song#7A45B.tmp")
    mixes = {
        "one_each": SkippedSummary(dict.fromkeys(("name_too_long", "not_found", "malformed", "unsupported",
                                                  "not_a_file"), 1), [
            long_name, SkippedEntry("not_found", "/Volumes/dk2/Downloads/part.tmp"),
            SkippedEntry("malformed", "/Volumes/dk2/Photos/._x.jpg"),
            SkippedEntry("unsupported", "/Volumes/dk2/Docs/._report.pdf"),
            SkippedEntry("not_a_file", "/Volumes/dk2/._link")]),
        "many": SkippedSummary({"name_too_long": 12, "not_found": 2, "malformed": 3, "unsupported": 2,
                                "not_a_file": 2}, [long_name]),
    }
    return [
        (f"log_skipped_{name}_{index}", "log", "deploy", True, line)
        for name, summary in mixes.items()
        for index, line in enumerate(_skipped_summaries(summary))
    ]


def cases() -> list[tuple[str, str, str, bool, object]]:
    """(name, event type, operation, ok, payload or Summary) for every branch."""
    version_blocked = VersionCheckResult(should_block=True, source="remote", current_version=10, local_version_code=5)
    version_current = VersionCheckResult(should_block=False, source="remote", current_version=5, local_version_code=5)
    version_unavailable = VersionCheckResult(should_block=False, source="unavailable")
    doctor_fail = [CheckResult("FAIL", "smbd is not running")]
    result = "result"
    rows: list[tuple[str, str, str, bool, object]] = [
        ("capabilities", result, "capabilities", True, contracts.capabilities_payload(
            helper_version="1.0", helper_version_code=1, operations=["doctor"],
            distribution_root="/tmp/dist", artifact_manifest_sha256=None)),
        ("operation_exited", result, "doctor", True, OPERATION_EXITED.fields()),
        ("discover", result, "discover", True, contracts.discover_payload({"devices": [{"name": "A"}, {"name": "B"}]})),
        ("validate_install_passed", result, "validate-install", True, contracts.install_validation_payload(ok=True, checks=[])),
        ("validate_install_failed", result, "validate-install", False, contracts.install_validation_payload(ok=False, checks=[])),
        ("telemetry_enabled", result, "set-telemetry", True, contracts.telemetry_preference_payload(
            install_id="id", telemetry_enabled=True, bootstrap_path="/tmp/b")),
        ("telemetry_disabled", result, "set-telemetry", True, contracts.telemetry_preference_payload(
            install_id="id", telemetry_enabled=False, bootstrap_path="/tmp/b")),
        ("version_required", result, "version-check", True, contracts.version_check_payload(version_blocked)),
        ("version_current", result, "version-check", True, contracts.version_check_payload(version_current)),
        ("version_unavailable", result, "version-check", True, contracts.version_check_payload(version_unavailable)),
        ("configure", result, "configure", True, contracts.configure_payload(
            config_path="/tmp/.env", host="root@10.0.0.2", configure_id="c", ssh_authenticated=True,
            device_syap="119", device_model="TimeCapsule8,119", compatibility=None)),
        ("settings_synchronized", result, "update-config-settings", True,
         {"config_path": "/tmp/.env", **SETTINGS_SYNCHRONIZED.fields()}),
        ("reachability_all", result, "reachability", True, contracts.reachability_payload(_reachability("PASS", "PASS"))),
        ("reachability_ssh_only", result, "reachability", True, contracts.reachability_payload(_reachability("PASS", "FAIL"))),
        ("reachability_smb_only", result, "reachability", True, contracts.reachability_payload(_reachability("FAIL", "PASS"))),
        ("reachability_unreachable", result, "reachability", True, contracts.reachability_payload(
            _reachability("FAIL", "FAIL"))),
        ("reachability_auth_failed", result, "reachability", True, contracts.reachability_payload(
            _reachability("PASS", "PASS", auth="FAIL"))),
        ("reachability_no_candidates", result, "reachability", True, contracts.reachability_payload(
            _no_reachability_candidates())),
        ("ssh_status_reachable", result, "set-ssh", True, contracts.set_ssh_payload(SetSshStatusResult(
            host="10.0.0.2", acp_port_reachable=True, ssh_port_reachable=True))),
        ("ssh_status_acp_only", result, "set-ssh", True, contracts.set_ssh_payload(SetSshStatusResult(
            host="10.0.0.2", acp_port_reachable=True, ssh_port_reachable=False))),
        ("ssh_status_unreachable", result, "set-ssh", True, contracts.set_ssh_payload(SetSshStatusResult(
            host="10.0.0.2", acp_port_reachable=False, ssh_port_reachable=False))),
        ("deploy_completed", result, "deploy", True, contracts.deploy_result_payload(payload_dir="/Volumes/dk2/.samba4")),
        ("deploy_netbsd4_followup", result, "deploy", True, contracts.deploy_result_payload(
            payload_dir="/Volumes/dk2/.samba4", netbsd4=True, message=netbsd4_activation_summary().text,
            summary=netbsd4_activation_summary())),
        ("activation_already_active", result, "activate", True, contracts.activation_result_payload(already_active=True)),
        ("activation_completed", result, "activate", True, contracts.activation_result_payload(already_active=False)),
        ("activation_followup", result, "activate", True, contracts.activation_result_payload(
            already_active=False, summary=netbsd4_activation_summary())),
        ("uninstall_completed", result, "uninstall", True, contracts.uninstall_result_payload(rebooted=True, verified=True)),
        ("uninstall_unverified", result, "uninstall", True, contracts.uninstall_result_payload(rebooted=True, verified=False)),
        ("fsck_volumes", result, "fsck", True, contracts.fsck_volume_list_payload({"targets": [{"device": "/dev/dk2", "mountpoint": "/Volumes/dk2"}]})),
        ("fsck_plan", result, "fsck", True, contracts.fsck_plan_payload(fsck_plan_to_jsonable(FSCK_TARGET, reboot=True, wait=True))),
        ("fsck_completed", result, "fsck", True, contracts.fsck_result_payload(
            device="/dev/dk2", mountpoint="/Volumes/dk2", returncode=0, reboot_requested=True, waited=True, verified=True)),
        ("fsck_failed", result, "fsck", False, contracts.fsck_result_payload(
            device="/dev/dk2", mountpoint="/Volumes/dk2", returncode=8, reboot_requested=True, waited=True,
            verified=True, error=fsck_failure_message(8))),
        ("fsck_crashed", result, "fsck", False, contracts.fsck_result_payload(
            device="/dev/dk2", mountpoint="/Volumes/dk2", returncode=138, reboot_requested=True, waited=True,
            verified=True, error=fsck_failure_message(138))),
        ("fsck_crashed_usb", result, "fsck", False, contracts.fsck_result_payload(
            device="/dev/dk5", mountpoint="/Volumes/dk5", returncode=138, reboot_requested=True, waited=True,
            verified=True, error=fsck_failure_message(138, builtin=False), builtin=False)),
        ("wipe_disk_completed", result, "wipe-disk", True, contracts.wipe_disk_result_payload(
            disk="wd0", volume_name="Data", new_volume_uuid="0a000000-0000-0000-0000-000000000003", offer_install=True)),
        ("doctor_passed", result, "doctor", True, contracts.doctor_payload(fatal=False, results=[])),
        ("doctor_fatal", result, "doctor", False, contracts.doctor_payload(
            fatal=True, results=doctor_fail, error="Doctor failures:\nFAIL smbd is not running")),
        # The Swift boot hook tests (BootHookTestSupport.swift) build their
        # helper replies from these flash rows by name; rename or drop one
        # there too.
        ("flash_backup", result, "flash", True, _flash_backup(_flash_bank(), _old_flash_bank())),
        # The running system booted the patched primary, so its LOGIN is patched.
        ("flash_backup_patched_primary", result, "flash", True, _flash_backup(
            _flash_bank(login=PATCHED_LOGIN_SCRIPT), _old_flash_bank(), live_login=PATCHED_LOGIN_SCRIPT)),
        # An unrecognized LOGIN in the primary bank; a secondary bank with no
        # valid footer, which the helper cannot analyze.
        ("flash_backup_unrecognized_banks", result, "flash", True, _flash_backup(
            _flash_bank(login=b""), flash_fixtures.without_footer(_old_flash_bank()))),
        ("flash_apple_stock_match", result, "flash", True, _check_apple([True], None)),
        ("flash_apple_stock_match_version", result, "flash", True, _check_apple([True], "7.8.1")),
        ("flash_apple_stock_mismatch", result, "flash", True, _check_apple([False], None)),
        ("flash_apple_stock_mismatch_version", result, "flash", True, _check_apple([False], "7.8.1")),
        ("flash_apple_all_match", result, "flash", True, _check_apple([True, True], None)),
        ("flash_apple_all_match_version", result, "flash", True, _check_apple([True, True], "7.8.1")),
        ("flash_apple_none_match", result, "flash", True, _check_apple([False, False], None)),
        ("flash_apple_none_match_version", result, "flash", True, _check_apple([False, False], "7.8.1")),
        ("flash_apple_some_match", result, "flash", True, _check_apple([True, False], None)),
        ("flash_apple_some_match_version", result, "flash", True, _check_apple([True, False], "7.8.1")),
        ("flash_restore_validated", result, "flash", True, _validate_apple_restore(None, None)),
        ("flash_restore_validated_version", result, "flash", True, _validate_apple_restore("7.8.1", None)),
        ("flash_restore_validated_product", result, "flash", True, _validate_apple_restore(None, FLASH_SYAP)),
        ("flash_restore_validated_version_product", result, "flash", True, _validate_apple_restore("7.8.1", FLASH_SYAP)),
        ("flash_plan_satisfied", result, "flash", True, _plan_result(_satisfied_patch_plan())),
        # The planners request a write for every patch or restore plan that is
        # not satisfied; these two cover the contract's remaining branch.
        ("flash_patch_plan", result, "flash", True, _flash_plan("patch")),
        ("flash_restore_plan", result, "flash", True, _flash_plan("restore")),
        ("flash_patch_write_plan", result, "flash", True, _plan_result(_patch_plan())),
        ("flash_restore_write_plan", result, "flash", True, _plan_result(_restore_plan())),
        ("flash_restore_secondary_write_plan", result, "flash", True, _plan_result(_secondary_refresh_plan())),
        ("flash_write_not_needed", result, "flash", True, _flash_write(_satisfied_patch_plan(), "not_needed")),
        ("flash_patch_write_validated", result, "flash", True, _flash_write(
            _patch_plan(), **_no_reboot("manual_power_cycle"))),
        ("flash_restore_write_rebooted", result, "flash", True, _flash_write(
            _restore_plan(), post_write_action="ssh_reboot", reboot_requested=True, rebooted=True,
            waited_after_reboot=True)),
        ("flash_restore_write_reboot_requested", result, "flash", True, _flash_write(
            _restore_plan(), post_write_action="ssh_reboot", reboot_requested=True, rebooted=False,
            waited_after_reboot=False)),
        ("flash_restore_write_manual_reboot", result, "flash", True, _flash_write(
            _restore_plan(), **_no_reboot("manual_reboot"))),
        ("flash_restore_secondary_write_validated", result, "flash", True, _flash_write(
            _secondary_refresh_plan(), **_no_reboot("none"))),
        # A write recorded as started and never validated. The app never gets
        # such a result (a failed write arrives as an error); this row gives
        # the contract's last branch a fixture.
        ("flash_write_completed", result, "flash", True, _flash_write(_patch_plan(), "attempting")),
    ]
    for name, ssh_result in _set_ssh_results():
        rows.append((name, result, "set-ssh", True, contracts.set_ssh_payload(ssh_result)))
    rows.extend(_kept_values_logs())
    rows.extend(_skipped_logs())
    rows.append(("log_waiting_for_previous_migration", "log", "deploy", True, WAITING_FOR_PREVIOUS_MIGRATION))
    rows.append(("log_unreadable_folder", "log", "deploy", True,
                 unreadable_folder_summary("Data/MacBook Pro.incomplete/bands")))
    return rows


def build() -> list[dict[str, object]]:
    events: list[AppEvent] = []
    sink = EventSink(events.append, request_id="fixture")
    names: list[str] = []
    for name, kind, operation, ok, value in cases():
        names.append(name)
        if kind == "log":
            assert isinstance(value, Summary)
            sink.log(operation, value.text, summary=value)
        else:
            sink.result(operation, ok=ok, payload=value)
    return [{"name": name, "event": event.to_jsonable()} for name, event in zip(names, events)]


def render() -> str:
    return json.dumps(build(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--write", action="store_true", help="rewrite the fixture instead of checking it")
    args = parser.parse_args(argv)
    text = render()
    if args.write:
        FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
        FIXTURE_PATH.write_text(text)
        return 0
    if not FIXTURE_PATH.exists() or FIXTURE_PATH.read_text() != text:
        print(f"{FIXTURE_PATH} is stale; run python -m tests.fixtures.summary_payloads --write", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
