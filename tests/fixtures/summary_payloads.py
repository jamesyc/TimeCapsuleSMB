"""Generate the helper summary contract fixture shared with the macOS app.

Every branch of every result summary (and each keyed log message) is built by
the real payload builders and serialized through the real event sink, so the
Swift tests decode exactly what the helper emits.

    python -m tests.fixtures.summary_payloads --write
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from unittest import mock

from timecapsulesmb.app import contracts
from timecapsulesmb.app.events import AppEvent, EventSink
from timecapsulesmb.app.ops.configure import SETTINGS_SYNCHRONIZED
from timecapsulesmb.app.service import OPERATION_EXITED
from timecapsulesmb.checks.models import CheckResult
from timecapsulesmb.core.config import AppConfig
from timecapsulesmb.core.messages import netbsd4_activation_summary
from timecapsulesmb.core.summaries import Summary
from timecapsulesmb.services.maintenance import fsck_failure_message, fsck_plan_to_jsonable, FsckTarget
from timecapsulesmb.services.reachability import ReachabilityCheck, ReachabilityResult, result_from_checks, run_reachability
from timecapsulesmb.services.runtime_verification import ACTIVATION_SETTLE_MESSAGE, BOOT_SETTLE_MESSAGE
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
    wait = mock.Mock(return_value=True)
    disable = mock.Mock()
    with mock.patch("timecapsulesmb.services.set_ssh.enable_ssh_with_port_preflight"):
        return [
            ("ssh_already_enabled", enable_set_ssh(connection, no_wait=False, initial=ssh_open)),
            ("ssh_enable_requested", enable_set_ssh(connection, no_wait=True, initial=ssh_closed)),
            ("ssh_configured", enable_set_ssh(
                connection, no_wait=False, initial=ssh_closed, wait_for_tcp_port_state=wait)),
            ("ssh_already_disabled", disable_set_ssh(
                connection, no_wait=False, initial=ssh_closed, disable_func=disable)),
            ("ssh_disable_requested", disable_set_ssh(
                connection, no_wait=True, initial=ssh_open, disable_func=disable)),
            ("ssh_disabled", disable_set_ssh(
                connection, no_wait=False, initial=ssh_open, disable_func=disable,
                wait_for_tcp_port_state=wait, wait_for_device_up_func=wait)),
        ]


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


def _apple_match(matched: bool, version: str | None) -> dict[str, object]:
    return {"matched": matched, "template_version": version}


def _flash_plan(mode: str, **plan: object) -> dict[str, object]:
    return contracts.flash_plan_payload({"backup_dir": BACKUP_DIR, "flash_plan": {"mode": mode, **plan}})


def _check_apple(matched: list[bool], version: str | None) -> dict[str, object]:
    matches = [{"bank": f"bank{i}", "match": _apple_match(m, version)} for i, m in enumerate(matched)]
    return _flash_plan("check_apple", apple_match=_apple_match(matched[0], version),
                       apple_matches=matches if len(matches) > 1 else [])


def _flash_write(**outcome: object) -> dict[str, object]:
    return contracts.flash_write_payload({"backup_dir": BACKUP_DIR, "write_outcome": outcome})


def cases() -> list[tuple[str, str, str, bool, object]]:
    """(name, event type, operation, ok, payload or Summary) for every branch."""
    version_blocked = VersionCheckResult(should_block=True, source="remote", current_version=10, local_version_code=5)
    version_update = VersionCheckResult(should_block=False, source="remote", current_version=10, local_version_code=5)
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
        ("version_available", result, "version-check", True, contracts.version_check_payload(version_update)),
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
        ("repair_xattrs", result, "repair-xattrs", True, contracts.repair_xattrs_payload(
            {"returncode": 0, "root": "/Volumes/Data", "finding_count": 3, "repairable_count": 2})),
        ("repair_xattrs_no_safe_repairs", result, "repair-xattrs", False, contracts.repair_xattrs_payload(
            {"returncode": 1, "root": "/Volumes/Data", "finding_count": 3, "repairable_count": 0,
             "failure": "no_safe_repairs", "error": "report"})),
        ("repair_xattrs_approval_required", result, "repair-xattrs", False, contracts.repair_xattrs_payload(
            {"returncode": 1, "root": "/Volumes/Data", "finding_count": 3, "repairable_count": 3,
             "failure": "approval_required", "error": "needs --yes"})),
        ("repair_xattrs_unresolved", result, "repair-xattrs", False, contracts.repair_xattrs_payload(
            {"returncode": 1, "root": "/Volumes/Data", "finding_count": 3, "repairable_count": 3,
             "failure": "unresolved", "unresolved_count": 2, "error": "report"})),
        ("doctor_passed", result, "doctor", True, contracts.doctor_payload(fatal=False, results=[])),
        ("doctor_fatal", result, "doctor", False, contracts.doctor_payload(
            fatal=True, results=doctor_fail, error="Doctor failures:\nFAIL smbd is not running")),
        ("flash_backup", result, "flash", True, contracts.flash_backup_payload({"backup_dir": BACKUP_DIR, "banks": []})),
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
        ("flash_restore_validated", result, "flash", True, _flash_plan("download_only", payload={})),
        ("flash_restore_validated_version", result, "flash", True, _flash_plan(
            "download_only", payload={"template_version": "7.8.1"})),
        ("flash_restore_validated_product", result, "flash", True, _flash_plan(
            "download_only", payload={"template_product_id": "119"})),
        ("flash_restore_validated_version_product", result, "flash", True, _flash_plan(
            "download_only", payload={"template_version": "7.8.1", "template_product_id": "119"})),
        ("flash_plan_satisfied", result, "flash", True, _flash_plan("patch", already_satisfied=True)),
        ("flash_patch_plan", result, "flash", True, _flash_plan("patch")),
        ("flash_restore_plan", result, "flash", True, _flash_plan("restore")),
        ("flash_patch_write_plan", result, "flash", True, _flash_plan("patch", write_requested=True)),
        ("flash_restore_write_plan", result, "flash", True, _flash_plan("restore", write_requested=True)),
        ("flash_write_not_needed", result, "flash", True, _flash_write(status="not_needed", mode="patch")),
        ("flash_patch_write_validated", result, "flash", True, _flash_write(
            status="written", mode="patch", write_validated=True, post_write_action="manual_power_cycle")),
        ("flash_restore_write_rebooted", result, "flash", True, _flash_write(
            status="written", mode="restore", write_validated=True, post_write_action="ssh_reboot",
            reboot_requested=True, rebooted=True)),
        ("flash_restore_write_reboot_requested", result, "flash", True, _flash_write(
            status="written", mode="restore", write_validated=True, post_write_action="ssh_reboot", reboot_requested=True)),
        ("flash_restore_write_manual_reboot", result, "flash", True, _flash_write(
            status="written", mode="restore", write_validated=True, post_write_action="manual_reboot")),
        ("flash_write_completed", result, "flash", True, _flash_write(status="written", mode="patch")),
        ("log_waiting_boot", "log", "deploy", True, BOOT_SETTLE_MESSAGE),
        ("log_waiting_activate", "log", "deploy", True, ACTIVATION_SETTLE_MESSAGE),
    ]
    for name, ssh_result in _set_ssh_results():
        rows.append((name, result, "set-ssh", True, contracts.set_ssh_payload(ssh_result)))
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
