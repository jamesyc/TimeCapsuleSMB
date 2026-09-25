from __future__ import annotations

from typing import Mapping

from timecapsulesmb.checks.models import CheckResult
from timecapsulesmb.core.summaries import Summary, english_count
from timecapsulesmb.services.app import jsonable
from timecapsulesmb.services.doctor import doctor_status_counts
from timecapsulesmb.services.reachability import ReachabilityResult
from timecapsulesmb.services.set_ssh import SetSshResult, SetSshStatusResult
from timecapsulesmb.services.version_check import VersionCheckResult


SCHEMA_VERSION = 1


def _with_schema(payload: Mapping[str, object]) -> dict[str, object]:
    data = dict(payload)
    data.setdefault("schema_version", SCHEMA_VERSION)
    return data


def capabilities_payload(
    *,
    helper_version: str,
    helper_version_code: int,
    operations: list[str],
    distribution_root: str,
    artifact_manifest_sha256: str | None,
) -> dict[str, object]:
    return _with_schema({
        "api_schema_version": SCHEMA_VERSION,
        "helper_version": helper_version,
        "helper_version_code": helper_version_code,
        "operations": operations,
        "distribution_root": distribution_root,
        "artifact_manifest_sha256": artifact_manifest_sha256,
        "confirmation_schema_version": 1,
        **Summary("helper_capabilities_resolved", "Helper capabilities resolved.").fields(),
    })


def _device_payload(*, host: str | None = None, syap: str | None = None, model: str | None = None) -> dict[str, object]:
    return {
        "host": host,
        "syap": syap,
        "model": model,
    }


def discover_payload(raw: Mapping[str, object]) -> dict[str, object]:
    instances = list(raw.get("instances", [])) if isinstance(raw.get("instances"), list) else []
    resolved = list(raw.get("resolved", [])) if isinstance(raw.get("resolved"), list) else []
    devices = list(raw.get("devices", [])) if isinstance(raw.get("devices"), list) else []
    return _with_schema({
        **raw,
        "counts": {
            "instances": len(instances),
            "resolved": len(resolved),
            "devices": len(devices),
        },
        **Summary("discovered_devices", f"Discovered {english_count(len(devices), 'device', 'devices')}.", (len(devices),)).fields(),
    })


def install_validation_payload(*, ok: bool, checks: list[object]) -> dict[str, object]:
    checks_payload = jsonable(checks)
    checks_list = checks_payload if isinstance(checks_payload, list) else []
    pass_count = sum(1 for check in checks_list if isinstance(check, dict) and check.get("ok") is True)
    fail_count = sum(1 for check in checks_list if isinstance(check, dict) and check.get("ok") is False)
    return _with_schema({
        "ok": ok,
        "checks": checks_list,
        "counts": {
            "checks": len(checks_list),
            "pass": pass_count,
            "fail": fail_count,
        },
        **(Summary("install_validation_passed", "Install validation passed.") if ok
           else Summary("install_validation_failed", "Install validation failed.")).fields(),
    })


def telemetry_preference_payload(*, install_id: str, telemetry_enabled: bool, bootstrap_path: str) -> dict[str, object]:
    return _with_schema({
        "install_id": install_id,
        "telemetry_enabled": telemetry_enabled,
        "bootstrap_path": bootstrap_path,
        **(Summary("telemetry_enabled", "Telemetry is enabled.") if telemetry_enabled
           else Summary("telemetry_disabled", "Telemetry is disabled.")).fields(),
    })


def version_check_payload(result: VersionCheckResult) -> dict[str, object]:
    update_available = (
        result.current_version is not None
        and result.current_version > result.local_version_code
    )
    if result.source == "unavailable":
        summary = Summary("version_metadata_unavailable", "Version metadata is unavailable.")
    elif result.should_block:
        summary = Summary("update_required", "Update required.")
    elif update_available:
        summary = Summary("update_available", "Update available.")
    else:
        summary = Summary("up_to_date", "TimeCapsuleSMB is up to date.")
    return _with_schema({
        "should_block": result.should_block,
        "update_available": update_available,
        "checked_url": result.checked_url,
        "message": result.message,
        "download_url": result.download_url,
        "local_version_code": result.local_version_code,
        "current_version": result.current_version,
        "min_supported_version": result.min_supported_version,
        "latest_tag": result.latest_tag,
        "source": result.source,
        **summary.fields(),
    })


def reachability_payload(result: ReachabilityResult) -> dict[str, object]:
    checks = jsonable(result.checks)
    if not isinstance(checks, list):
        checks = []
    counts: dict[str, int] = {}
    for check in checks:
        if not isinstance(check, dict):
            continue
        status = str(check.get("status") or "").upper()
        if status:
            counts[status] = counts.get(status, 0) + 1
    return _with_schema({
        "status": result.status,
        "ssh_host": result.ssh_host,
        "smb_host": result.smb_host,
        "checks": checks,
        "counts": counts,
        **Summary(result.summary_key, result.summary).fields(),
    })


def set_ssh_payload(result: SetSshStatusResult | SetSshResult) -> dict[str, object]:
    payload = jsonable(result)
    if not isinstance(payload, dict):
        payload = {}
    if "ssh_port_reachable" not in payload:
        payload["ssh_port_reachable"] = bool(getattr(result, "ssh_final_reachable", False))
    if "acp_port_error" not in payload:
        payload["acp_port_error"] = None
    if "ssh_port_error" not in payload:
        payload["ssh_port_error"] = None
    payload["ssh_disabled_likely"] = bool(payload.get("acp_port_reachable")) and not bool(payload.get("ssh_port_reachable"))
    payload.update(Summary(result.summary_key, result.summary).fields())
    return _with_schema(payload)


def configure_payload(
    *,
    config_path: str,
    host: str,
    configure_id: str,
    ssh_authenticated: bool,
    device_syap: str | None,
    device_model: str | None,
    compatibility: object | None,
) -> dict[str, object]:
    return _with_schema({
        "config_path": config_path,
        "host": host,
        "configure_id": configure_id,
        "ssh_authenticated": ssh_authenticated,
        "device_syap": device_syap,
        "device_model": device_model,
        "compatibility": jsonable(compatibility),
        "device": _device_payload(host=host, syap=device_syap, model=device_model),
        **Summary("configuration_saved", "Configuration saved and SSH authentication verified.").fields(),
    })


def deploy_plan_payload(raw: Mapping[str, object], *, payload_family: str | None, netbsd4: bool) -> dict[str, object]:
    requires_reboot = bool(raw.get("reboot_required"))
    return _with_schema({
        **raw,
        "requires_reboot": requires_reboot,
        "payload_family": payload_family,
        "netbsd4": netbsd4,
        "summary": "Deployment dry-run plan generated.",
    })


def deploy_result_payload(
    *,
    payload_dir: str,
    rebooted: bool | None = None,
    reboot_requested: bool | None = None,
    waited: bool | None = None,
    verified: bool | None = None,
    netbsd4: bool = False,
    message: str | None = None,
    payload_family: str | None = None,
) -> dict[str, object]:
    # The only message a deploy result carries is the activation outcome, and
    # only NetBSD 4's needs a follow-up; every other completion is generic.
    if netbsd4 and message is not None:
        summary = Summary("activation_completed_followup", message)
    else:
        summary = Summary("deploy_completed", message or "Deployment completed.")
    payload: dict[str, object] = {
        "payload_dir": payload_dir,
        "netbsd4": netbsd4,
        "payload_family": payload_family,
        "requires_reboot": bool(rebooted or reboot_requested),
        **summary.fields(),
    }
    if rebooted is not None:
        payload["rebooted"] = rebooted
    if reboot_requested is not None:
        payload["reboot_requested"] = reboot_requested
    if waited is not None:
        payload["waited"] = waited
    if verified is not None:
        payload["verified"] = verified
    if message is not None:
        payload["message"] = message
    return _with_schema(payload)


def activation_plan_payload(raw: object) -> dict[str, object]:
    payload = jsonable(raw)
    if not isinstance(payload, dict):
        payload = {"plan": payload}
    actions = payload.get("actions")
    action_count = len(actions) if isinstance(actions, list) else 0
    return _with_schema({
        **payload,
        "counts": {"actions": action_count},
        "summary": "NetBSD4 activation dry-run plan generated.",
    })


def activation_result_payload(*, already_active: bool, message: str | None = None) -> dict[str, object]:
    if already_active:
        summary = Summary("activation_already_active", "NetBSD4 payload was already active.")
    elif message is not None:
        # The only activation message is NetBSD 4's reboot follow-up.
        summary = Summary("activation_completed_followup", message)
    else:
        summary = Summary("activation_completed", "NetBSD4 activation completed.")
    payload: dict[str, object] = {
        "already_active": already_active,
        **summary.fields(),
    }
    if message is not None:
        payload["message"] = message
    return _with_schema(payload)


def uninstall_plan_payload(raw: Mapping[str, object]) -> dict[str, object]:
    requires_reboot = bool(raw.get("reboot_required"))
    payload_dirs = raw.get("payload_dirs")
    payload_dir_count = len(payload_dirs) if isinstance(payload_dirs, list) else 0
    return _with_schema({
        **raw,
        "requires_reboot": requires_reboot,
        "counts": {"payload_dirs": payload_dir_count},
        "summary": "Uninstall dry-run plan generated.",
    })


def uninstall_result_payload(
    *,
    rebooted: bool,
    verified: bool,
    reboot_requested: bool | None = None,
    waited: bool | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "rebooted": rebooted,
        "verified": verified,
        "requires_reboot": bool(rebooted or reboot_requested),
        **(Summary("uninstall_completed", "Uninstall completed.") if verified
           else Summary("uninstall_unverified", "Uninstall completed without post-reboot verification.")).fields(),
    }
    if reboot_requested is not None:
        payload["reboot_requested"] = reboot_requested
    if waited is not None:
        payload["waited"] = waited
    return _with_schema(payload)


def fsck_volume_list_payload(raw: Mapping[str, object]) -> dict[str, object]:
    targets = raw.get("targets")
    target_count = len(targets) if isinstance(targets, list) else 0
    return _with_schema({
        **raw,
        "counts": {"targets": target_count},
        **Summary("hfs_volumes_found", f"Found {english_count(target_count, 'mounted HFS volume', 'mounted HFS volumes')}.", (target_count,)).fields(),
    })


def fsck_plan_payload(raw: Mapping[str, object]) -> dict[str, object]:
    return _with_schema({
        **raw,
        **Summary("fsck_plan_generated", "Dry-run plan generated for fsck.").fields(),
    })


def fsck_result_payload(
    *,
    device: str,
    mountpoint: str,
    returncode: int | None = None,
    reboot_requested: bool | None = None,
    waited: bool | None = None,
    verified: bool | None = None,
    error: str | None = None,
) -> dict[str, object]:
    if error is not None:
        if not isinstance(returncode, int):
            raise ValueError("a failed fsck result needs fsck_hfs's exit status")
        summary = Summary("fsck_failed", error, (returncode,))
    else:
        summary = Summary("fsck_completed", "Disk repair completed with fsck.")
    payload: dict[str, object] = {
        "device": device,
        "mountpoint": mountpoint,
        **summary.fields(),
    }
    if error is not None:
        payload["error"] = error
    if returncode is not None:
        payload["returncode"] = returncode
    if reboot_requested is not None:
        payload["reboot_requested"] = reboot_requested
    if waited is not None:
        payload["waited"] = waited
    if verified is not None:
        payload["verified"] = verified
    return _with_schema(payload)


def _repair_xattrs_summary(raw: Mapping[str, object], finding_count: int, repairable_count: int) -> Summary:
    # A failed run still reports its finding counts; summarizing it from them
    # ("Found 3 issues, 3 repairable.") would hide why it failed.
    failure = raw.get("failure")
    if failure == "no_safe_repairs":
        return Summary(
            "repair_xattrs_no_safe_repairs",
            f"Found {english_count(finding_count, 'metadata issue', 'metadata issues')}, "
            "but no known-safe repair is available.",
            (finding_count,),
        )
    if failure == "approval_required":
        return Summary("repair_xattrs_approval_required", "No changes made; repairs need confirmation.")
    if failure == "unresolved":
        unresolved_count = int(raw.get("unresolved_count") or 0)
        return Summary(
            "repair_xattrs_unresolved",
            f"{english_count(unresolved_count, 'metadata issue remains', 'metadata issues remain')} after repair.",
            (unresolved_count,),
        )
    return Summary(
        "repair_xattrs_found",
        f"Found {english_count(finding_count, 'metadata issue', 'metadata issues')}, {repairable_count} repairable.",
        (finding_count, repairable_count),
    )


def repair_xattrs_payload(raw: Mapping[str, object]) -> dict[str, object]:
    finding_count = int(raw.get("finding_count") or 0)
    repairable_count = int(raw.get("repairable_count") or 0)
    stats = raw.get("stats")
    summary = _repair_xattrs_summary(raw, finding_count, repairable_count)
    payload = {
        **raw,
        "counts": {
            "findings": finding_count,
            "repairable": repairable_count,
        },
        **summary.fields(),
        "summary_text": summary.text,
    }
    if stats is not None:
        payload["stats"] = jsonable(stats)
    return _with_schema(payload)


def flash_backup_payload(raw: Mapping[str, object]) -> dict[str, object]:
    banks = raw.get("banks")
    bank_count = len(banks) if isinstance(banks, list) else 0
    return _with_schema({
        **raw,
        "counts": {"banks": bank_count},
        **Summary("flash_backup_saved", f"Flash backup saved to {raw.get('backup_dir')}.", (str(raw.get("backup_dir")),)).fields(),
    })


def _flash_plan_dict(raw: Mapping[str, object]) -> dict[str, object]:
    plan = raw.get("flash_plan")
    return plan if isinstance(plan, dict) else {}


def _flash_plan_child(plan: Mapping[str, object], key: str) -> dict[str, object] | None:
    value = plan.get(key)
    return dict(value) if isinstance(value, dict) else None


def _firmware_payload_path(raw: Mapping[str, object], plan: Mapping[str, object]) -> str | None:
    target_bank = plan.get("target_bank")
    mode = plan.get("mode")
    if not isinstance(mode, str):
        return None
    files = raw.get("files")
    if not isinstance(files, dict):
        return None
    if isinstance(target_bank, str):
        value = files.get(f"{target_bank}_{mode}_basebinary_payload")
    else:
        value = files.get(f"{mode}_basebinary_payload")
    return value if isinstance(value, str) and value.strip() else None


def _apple_matches(plan: Mapping[str, object]) -> list[Mapping[str, object]]:
    matches = plan.get("apple_matches")
    if not isinstance(matches, list):
        return []
    return [match for match in matches if isinstance(match, dict)]


def _flash_plan_warnings(plan: Mapping[str, object]) -> list[str]:
    warnings = plan.get("warnings")
    if not isinstance(warnings, list):
        return []
    return [str(warning) for warning in warnings if isinstance(warning, str) and warning.strip()]


def _apple_match_count(matches: list[Mapping[str, object]], *, matched: bool) -> int:
    count = 0
    for result in matches:
        match = result.get("match")
        if isinstance(match, dict) and match.get("matched") is matched:
            count += 1
    return count


def _nonempty(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _apple_firmware_summary(
    mode: str,
    match: Mapping[str, object] | None,
    payload: Mapping[str, object] | None,
    matches: list[Mapping[str, object]],
) -> Summary | None:
    # Each variant is a whole sentence so translations never splice fragments.
    if mode == "check_apple":
        version = _nonempty(None if match is None else match.get("template_version"))
        suffix = f" {version}" if version else ""
        with_version = (version,) if version else ()
        variant = "_version" if version else ""
        if len(matches) > 1:
            matched_count = _apple_match_count(matches, matched=True)
            if matched_count == len(matches):
                return Summary(f"flash.apple_all_match{variant}",
                               f"All candidate firmware banks match Apple stock firmware{suffix}.", with_version)
            if matched_count == 0:
                return Summary(f"flash.apple_none_match{variant}",
                               f"No candidate firmware banks match Apple stock firmware{suffix}.", with_version)
            return Summary(
                f"flash.apple_some_match{variant}",
                f"{matched_count} of {len(matches)} candidate firmware banks {'matches' if matched_count == 1 else 'match'} "
                f"Apple stock firmware{suffix}.",
                (matched_count, len(matches), *with_version),
            )
        if match is not None and match.get("matched") is True:
            return Summary(f"flash.apple_stock_match{variant}",
                           f"Active firmware bank matches Apple stock firmware{suffix}.", with_version)
        return Summary(f"flash.apple_stock_mismatch{variant}",
                       f"Active firmware bank does not match Apple stock firmware{suffix}.", with_version)
    if mode == "download_only":
        version = _nonempty(None if payload is None else payload.get("template_version"))
        product = _nonempty(None if payload is None else payload.get("template_product_id"))
        if version and product:
            return Summary("flash.apple_restore_validated_version_product",
                           f"Apple restore firmware validated (version {version}, product {product}).", (version, product))
        if version:
            return Summary("flash.apple_restore_validated_version",
                           f"Apple restore firmware validated (version {version}).", (version,))
        if product:
            return Summary("flash.apple_restore_validated_product",
                           f"Apple restore firmware validated (product {product}).", (product,))
        return Summary("flash.apple_restore_validated", "Apple restore firmware validated.")
    return None


def _flash_summary_fields(key: str | None, text: str) -> dict[str, object]:
    # Modes outside patch/restore are not reachable here; they keep English text.
    return Summary(key, text).fields() if key is not None else {"summary": text}


def flash_plan_payload(raw: Mapping[str, object]) -> dict[str, object]:
    plan = _flash_plan_dict(raw)
    mode = "unknown"
    write_requested = False
    already_satisfied = False
    if plan:
        mode = str(plan.get("mode") or mode)
        write_requested = bool(plan.get("write_requested"))
        already_satisfied = bool(plan.get("already_satisfied"))
    apple_firmware_match = _flash_plan_child(plan, "apple_match")
    firmware_payload = _flash_plan_child(plan, "payload")
    firmware_payload_path = _firmware_payload_path(raw, plan)
    apple_firmware_matches = _apple_matches(plan)
    warnings = _flash_plan_warnings(plan)
    apple_summary = _apple_firmware_summary(mode, apple_firmware_match, firmware_payload, apple_firmware_matches)
    if apple_summary is not None:
        summary_fields = apple_summary.fields()
    elif already_satisfied:
        summary_fields = Summary("flash_plan_already_satisfied", "Flash plan is already satisfied; no write is needed.").fields()
    elif write_requested:
        key = f"flash.{mode}_write_plan_generated" if mode in ("patch", "restore") else None
        summary_fields = _flash_summary_fields(key, f"Flash {mode} write plan generated.")
    else:
        key = f"flash.{mode}_plan_generated" if mode in ("patch", "restore") else None
        summary_fields = _flash_summary_fields(key, f"Flash {mode} plan generated.")
    return _with_schema({
        **raw,
        "mode": mode,
        "write_requested": write_requested,
        "already_satisfied": already_satisfied,
        "apple_firmware_match": apple_firmware_match,
        "apple_firmware_matches": apple_firmware_matches,
        "apple_match_status": plan.get("apple_match_status") if isinstance(plan.get("apple_match_status"), str) else None,
        "firmware_payload": firmware_payload,
        "firmware_payload_path": firmware_payload_path,
        "warnings": warnings,
        **summary_fields,
    })


def flash_write_payload(raw: Mapping[str, object]) -> dict[str, object]:
    outcome = raw.get("write_outcome")
    status = "unknown"
    mode = "unknown"
    write_validated = False
    post_write_action = ""
    reboot_requested = False
    rebooted = False
    waited_after_reboot = False
    if isinstance(outcome, dict):
        status = str(outcome.get("status") or status)
        mode = str(outcome.get("mode") or mode)
        write_validated = bool(outcome.get("write_validated"))
        post_write_action = str(outcome.get("post_write_action") or "")
        reboot_requested = bool(outcome.get("reboot_requested"))
        rebooted = bool(outcome.get("rebooted"))
        waited_after_reboot = bool(outcome.get("waited_after_reboot"))
    if status == "not_needed":
        summary_fields = Summary("flash_write_not_needed", "Flash write was not needed.").fields()
    elif write_validated and mode == "patch":
        summary_fields = Summary("flash_patch_write_validated_power_cycle",
                                 "Flash patch write validated; manual power cycle required.").fields()
    elif write_validated and mode == "restore":
        if post_write_action == "ssh_reboot" and rebooted:
            summary = Summary("flash_restore_write_validated_rebooted", "Flash restore write validated; device rebooted.")
        elif post_write_action == "ssh_reboot" and reboot_requested:
            summary = Summary("flash_restore_write_validated_reboot_requested",
                              "Flash restore write validated; reboot requested.")
        else:
            summary = Summary("flash_restore_write_validated_manual_reboot",
                              "Flash restore write validated; manual reboot required.")
        summary_fields = summary.fields()
    elif write_validated:
        summary_fields = _flash_summary_fields(None, f"Flash {mode} write validated.")
    else:
        summary_fields = Summary("flash_write_completed", "Flash write completed.").fields()
    return _with_schema({
        **raw,
        "mode": mode,
        "write_status": status,
        "write_validated": write_validated,
        "post_write_action": post_write_action,
        "reboot_requested": reboot_requested,
        "rebooted": rebooted,
        "waited_after_reboot": waited_after_reboot,
        **summary_fields,
    })


def doctor_payload(
    *,
    fatal: bool,
    results: list[CheckResult],
    error: str | None = None,
) -> dict[str, object]:
    result_payload = [jsonable(result) for result in results]
    counts = doctor_status_counts(results)
    payload: dict[str, object] = {
        "fatal": fatal,
        "results": result_payload,
        "counts": counts,
        **(Summary("doctor_found_fatal", "Doctor found one or more fatal problems.") if fatal
           else Summary("doctor_checks_passed", "Doctor checks passed.")).fields(),
    }
    if error:
        payload["error"] = error
    return _with_schema(payload)
