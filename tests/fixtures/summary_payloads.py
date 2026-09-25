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

from timecapsulesmb.app import contracts
from timecapsulesmb.app.events import AppEvent, EventSink
from timecapsulesmb.checks.models import CheckResult
from timecapsulesmb.core.summaries import Summary
from timecapsulesmb.services.maintenance import fsck_failure_message, fsck_plan_to_jsonable, FsckTarget
from timecapsulesmb.services.reachability import ReachabilityResult
from timecapsulesmb.services.runtime_verification import ACTIVATION_SETTLE_MESSAGE, BOOT_SETTLE_MESSAGE
from timecapsulesmb.services.set_ssh import SetSshResult, SetSshStatusResult
from timecapsulesmb.services.version_check import VersionCheckResult


FIXTURE_PATH = (
    Path(__file__).resolve().parents[2]
    / "macos/TimeCapsuleSMB/Tests/TimeCapsuleSMBAppTests/Fixtures/summary_payloads.json"
)

NETBSD4_FOLLOWUP = "NetBSD4 activation complete. Run `activate` after a reboot if the device did not auto-start Samba."
FSCK_TARGET = FsckTarget(device="/dev/dk2", mountpoint="/Volumes/dk2", name="Data", builtin=True)


def _set_ssh(key: str, text: str) -> SetSshResult:
    return SetSshResult(
        host="10.0.0.2",
        action="enable",
        ssh_initially_reachable=False,
        ssh_final_reachable=True,
        acp_port_reachable=True,
        reboot_requested=True,
        waited=True,
        summary=text,
        summary_key=key,
    )


def _reachability(status: str, key: str, text: str) -> ReachabilityResult:
    return ReachabilityResult(status=status, summary=text, ssh_host="root@10.0.0.2", smb_host="10.0.0.2", summary_key=key)


def _apple_match(matched: bool, version: str | None) -> dict[str, object]:
    return {"matched": matched, "template_version": version}


def _flash_plan(mode: str, **plan: object) -> dict[str, object]:
    return contracts.flash_plan_payload({"flash_plan": {"mode": mode, **plan}})


def _check_apple(matched: list[bool], version: str | None) -> dict[str, object]:
    matches = [{"bank": f"bank{i}", "match": _apple_match(m, version)} for i, m in enumerate(matched)]
    return _flash_plan("check_apple", apple_match=_apple_match(matched[0], version),
                       apple_matches=matches if len(matches) > 1 else [])


def _flash_write(**outcome: object) -> dict[str, object]:
    return contracts.flash_write_payload({"write_outcome": outcome})


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
        ("operation_exited", result, "doctor", True, Summary("operation_exited", "Operation exited.").fields()),
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
         {"config_path": "/tmp/.env", **Summary("settings_synchronized", "Device profile settings synchronized.").fields()}),
        ("reachability_all", result, "reachability", True, contracts.reachability_payload(
            _reachability("reachable", "reachability.all_reachable", "SSH reachable; SMB port reachable."))),
        ("reachability_ssh_only", result, "reachability", True, contracts.reachability_payload(
            _reachability("partial", "reachability.ssh_only", "SSH reachable, SMB port closed."))),
        ("reachability_smb_only", result, "reachability", True, contracts.reachability_payload(
            _reachability("partial", "reachability.smb_only", "SMB port reachable, SSH closed."))),
        ("reachability_unreachable", result, "reachability", True, contracts.reachability_payload(
            _reachability("unreachable", "reachability.unreachable", "Could not reach SSH or SMB."))),
        ("reachability_auth_failed", result, "reachability", True, contracts.reachability_payload(
            _reachability("partial", "reachability.auth_failed", "SSH authentication failed."))),
        ("reachability_no_candidates", result, "reachability", True, contracts.reachability_payload(
            _reachability("skipped", "reachability.no_candidates", "No saved host candidates were available."))),
        ("ssh_status_reachable", result, "set-ssh", True, contracts.set_ssh_payload(SetSshStatusResult(
            host="10.0.0.2", acp_port_reachable=True, ssh_port_reachable=True))),
        ("ssh_status_acp_only", result, "set-ssh", True, contracts.set_ssh_payload(SetSshStatusResult(
            host="10.0.0.2", acp_port_reachable=True, ssh_port_reachable=False))),
        ("ssh_status_unreachable", result, "set-ssh", True, contracts.set_ssh_payload(SetSshStatusResult(
            host="10.0.0.2", acp_port_reachable=False, ssh_port_reachable=False))),
        ("deploy_completed", result, "deploy", True, contracts.deploy_result_payload(payload_dir="/Volumes/dk2/.samba4")),
        ("deploy_runtime_activated", result, "deploy", True, contracts.deploy_result_payload(
            payload_dir="/Volumes/dk2/.samba4", message="Runtime activation complete.")),
        ("deploy_netbsd4_followup", result, "deploy", True, contracts.deploy_result_payload(
            payload_dir="/Volumes/dk2/.samba4", netbsd4=True, message=NETBSD4_FOLLOWUP)),
        ("activation_already_active", result, "activate", True, contracts.activation_result_payload(already_active=True)),
        ("activation_completed", result, "activate", True, contracts.activation_result_payload(already_active=False)),
        ("activation_followup", result, "activate", True, contracts.activation_result_payload(
            already_active=False, message=NETBSD4_FOLLOWUP)),
        ("uninstall_completed", result, "uninstall", True, contracts.uninstall_result_payload(rebooted=True, verified=True)),
        ("uninstall_unverified", result, "uninstall", True, contracts.uninstall_result_payload(rebooted=True, verified=False)),
        ("fsck_volumes", result, "fsck", True, contracts.fsck_volume_list_payload({"targets": [{"device": "/dev/dk2"}]})),
        ("fsck_plan", result, "fsck", True, contracts.fsck_plan_payload(fsck_plan_to_jsonable(FSCK_TARGET, reboot=True, wait=True))),
        ("fsck_completed", result, "fsck", True, contracts.fsck_result_payload(
            device="/dev/dk2", mountpoint="/Volumes/dk2", returncode=0, reboot_requested=True, waited=True, verified=True)),
        ("fsck_failed", result, "fsck", False, contracts.fsck_result_payload(
            device="/dev/dk2", mountpoint="/Volumes/dk2", returncode=8, reboot_requested=True, waited=True,
            verified=True, error=fsck_failure_message(8))),
        ("repair_xattrs", result, "repair-xattrs", True, contracts.repair_xattrs_payload(
            {"returncode": 0, "root": "/Volumes/Data", "finding_count": 3, "repairable_count": 2})),
        ("doctor_passed", result, "doctor", True, contracts.doctor_payload(fatal=False, results=[])),
        ("doctor_fatal", result, "doctor", False, contracts.doctor_payload(
            fatal=True, results=doctor_fail, error="Doctor failures:\nFAIL smbd is not running")),
        ("flash_backup", result, "flash", True, contracts.flash_backup_payload({"backup_dir": "/tmp/flash-backup", "banks": []})),
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
    for key, text in (("ssh.already_enabled", "SSH is already enabled."),
                      ("ssh.enable_requested", "SSH enable requested; not waiting for SSH to open."),
                      ("ssh.configured", "SSH is configured."),
                      ("ssh.already_disabled", "SSH already disabled."),
                      ("ssh.disable_requested", "SSH disable requested; not waiting for reboot or verifying SSH stays closed."),
                      ("ssh.disabled", "SSH disabled (remains closed after reboot).")):
        rows.append((key.replace(".", "_"), result, "set-ssh", True, contracts.set_ssh_payload(_set_ssh(key, text))))
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
