from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from timecapsulesmb.checks.models import CheckResult
from timecapsulesmb.cli.version_check import VersionCheckResult
from timecapsulesmb.identity import InstallIdentity
from timecapsulesmb.services.app import jsonable
from timecapsulesmb.services.doctor import doctor_status_counts


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
        "summary": "helper capabilities resolved.",
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
        "summary": f"discovered {len(devices)} Time Capsule device(s).",
    })


def paths_payload(raw: Mapping[str, object]) -> dict[str, object]:
    artifacts = raw.get("artifacts")
    artifact_count = len(artifacts) if isinstance(artifacts, list) else 0
    return _with_schema({
        **raw,
        "counts": {"artifacts": artifact_count},
        "summary": f"resolved app paths with {artifact_count} artifact path(s).",
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
        "summary": "install validation passed." if ok else "install validation failed.",
    })


def telemetry_identity_payload(*, identity: InstallIdentity, bootstrap_path: str) -> dict[str, object]:
    return _with_schema({
        "install_id": identity.install_id,
        "telemetry_enabled": identity.telemetry_enabled,
        "bootstrap_path": bootstrap_path,
        "summary": "telemetry is enabled." if identity.telemetry_enabled else "telemetry is disabled.",
    })


def version_check_payload(result: VersionCheckResult) -> dict[str, object]:
    summary = "update required." if result.should_block else "TimeCapsuleSMB is up to date."
    if result.source == "unavailable":
        summary = "version metadata is unavailable."
    return _with_schema({
        "should_block": result.should_block,
        "checked_url": result.checked_url,
        "message": result.message,
        "download_url": result.download_url,
        "local_version_code": result.local_version_code,
        "current_version": result.current_version,
        "min_supported_version": result.min_supported_version,
        "latest_tag": result.latest_tag,
        "source": result.source,
        "summary": summary,
    })


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
        "summary": "configuration saved and SSH authentication verified.",
    })


def deploy_plan_payload(raw: Mapping[str, object], *, payload_family: str | None, netbsd4: bool) -> dict[str, object]:
    requires_reboot = bool(raw.get("reboot_required"))
    return _with_schema({
        **raw,
        "requires_reboot": requires_reboot,
        "payload_family": payload_family,
        "netbsd4": netbsd4,
        "summary": "deployment dry-run plan generated.",
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
    payload: dict[str, object] = {
        "payload_dir": payload_dir,
        "netbsd4": netbsd4,
        "payload_family": payload_family,
        "requires_reboot": bool(rebooted or reboot_requested),
        "summary": "deployment completed.",
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
        payload["summary"] = message
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
    payload: dict[str, object] = {
        "already_active": already_active,
        "summary": "NetBSD4 payload was already active." if already_active else "NetBSD4 activation completed.",
    }
    if message is not None:
        payload["message"] = message
        payload["summary"] = message
    return _with_schema(payload)


def uninstall_plan_payload(raw: Mapping[str, object]) -> dict[str, object]:
    requires_reboot = bool(raw.get("reboot_required"))
    payload_dirs = raw.get("payload_dirs")
    payload_dir_count = len(payload_dirs) if isinstance(payload_dirs, list) else 0
    return _with_schema({
        **raw,
        "requires_reboot": requires_reboot,
        "counts": {"payload_dirs": payload_dir_count},
        "summary": "uninstall dry-run plan generated.",
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
        "summary": "uninstall completed." if verified else "uninstall completed without post-reboot verification.",
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
        "summary": f"found {target_count} mounted HFS volume(s).",
    })


def fsck_plan_payload(raw: Mapping[str, object]) -> dict[str, object]:
    return _with_schema({
        **raw,
        "summary": "fsck dry-run plan generated.",
    })


def fsck_result_payload(
    *,
    device: str,
    mountpoint: str,
    returncode: int | None = None,
    reboot_requested: bool | None = None,
    waited: bool | None = None,
    verified: bool | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "device": device,
        "mountpoint": mountpoint,
        "summary": "fsck completed.",
    }
    if returncode is not None:
        payload["returncode"] = returncode
    if reboot_requested is not None:
        payload["reboot_requested"] = reboot_requested
    if waited is not None:
        payload["waited"] = waited
    if verified is not None:
        payload["verified"] = verified
    return _with_schema(payload)


def repair_xattrs_payload(raw: Mapping[str, object]) -> dict[str, object]:
    finding_count = int(raw.get("finding_count") or 0)
    repairable_count = int(raw.get("repairable_count") or 0)
    legacy_summary = raw.get("summary")
    stats = raw.get("stats", legacy_summary if not isinstance(legacy_summary, str) else None)
    summary = legacy_summary if isinstance(legacy_summary, str) and legacy_summary.strip() else (
        f"repair-xattrs found {finding_count} issue(s), {repairable_count} repairable."
    )
    payload = {
        **raw,
        "counts": {
            "findings": finding_count,
            "repairable": repairable_count,
        },
        "summary": summary,
        "summary_text": summary,
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
        "summary": f"flash backup saved to {raw.get('backup_dir')}.",
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
    if not isinstance(target_bank, str) or not isinstance(mode, str):
        return None
    files = raw.get("files")
    if not isinstance(files, dict):
        return None
    value = files.get(f"{target_bank}_{mode}_basebinary_payload")
    return value if isinstance(value, str) and value.strip() else None


def _apple_firmware_summary(mode: str, match: Mapping[str, object] | None, payload: Mapping[str, object] | None) -> str | None:
    if mode == "check_apple":
        version = None if match is None else match.get("template_version")
        version_text = f" {version}" if isinstance(version, str) and version.strip() else ""
        if match is not None and match.get("matched") is True:
            return f"Active firmware bank matches Apple stock firmware{version_text}."
        return f"Active firmware bank does not match Apple stock firmware{version_text}."
    if mode == "download_only":
        version = None if payload is None else payload.get("template_version")
        product = None if payload is None else payload.get("template_product_id")
        detail_parts = []
        if isinstance(version, str) and version.strip():
            detail_parts.append(f"version {version}")
        if isinstance(product, str) and product.strip():
            detail_parts.append(f"product {product}")
        detail = f" ({', '.join(detail_parts)})" if detail_parts else ""
        return f"Apple restore firmware validated{detail}."
    return None


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
    apple_summary = _apple_firmware_summary(mode, apple_firmware_match, firmware_payload)
    if apple_summary is not None:
        summary = apple_summary
    elif already_satisfied:
        summary = "flash plan is already satisfied; no write is needed."
    elif write_requested:
        summary = f"flash {mode} write plan generated."
    else:
        summary = f"flash {mode} plan generated."
    return _with_schema({
        **raw,
        "mode": mode,
        "write_requested": write_requested,
        "already_satisfied": already_satisfied,
        "apple_firmware_match": apple_firmware_match,
        "firmware_payload": firmware_payload,
        "firmware_payload_path": firmware_payload_path,
        "summary": summary,
    })


def flash_write_payload(raw: Mapping[str, object]) -> dict[str, object]:
    outcome = raw.get("write_outcome")
    status = "unknown"
    mode = "unknown"
    write_validated = False
    if isinstance(outcome, dict):
        status = str(outcome.get("status") or status)
        mode = str(outcome.get("mode") or mode)
        write_validated = bool(outcome.get("write_validated"))
    if status == "not_needed":
        summary = "flash write was not needed."
    elif write_validated and mode == "patch":
        summary = "flash patch write validated; manual power cycle required."
    elif write_validated:
        summary = f"flash {mode} write validated; manual power cycle required."
    else:
        summary = "flash write completed."
    return _with_schema({
        **raw,
        "mode": mode,
        "write_status": status,
        "write_validated": write_validated,
        "summary": summary,
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
        "summary": "doctor found one or more fatal problems." if fatal else "doctor checks passed.",
    }
    if error:
        payload["error"] = error
    return _with_schema(payload)
