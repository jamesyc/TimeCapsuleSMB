from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from pathlib import Path

from timecapsulesmb.core.config import AppConfig, DEFAULTS
from timecapsulesmb.core.paths import (
    AppPaths,
    artifact_manifest_source,
    resolve_app_paths,
    validate_distribution_root,
)
from timecapsulesmb.deploy.artifacts import load_artifact_manifest, validate_artifacts
from timecapsulesmb.deploy.templates import (
    SMBCONF_RUNTIME_TOKENS,
    build_template_bundle,
    load_boot_asset_text,
    render_checked_template,
    render_runtime_smbconf_text,
)


REQUIRED_PYTHON_MODULES = ("zeroconf", "pexpect", "ifaddr")
BOOT_ASSET_NAMES = (
    "rc.local",
    "common.sh",
    "start-samba.sh",
    "watchdog.sh",
    "dfree.sh",
    "smb.conf.template",
)


@dataclass(frozen=True)
class InstallCheckResult:
    id: str
    ok: bool
    message: str
    details: dict[str, object] = field(default_factory=dict)


def check_to_jsonable(check: InstallCheckResult) -> dict[str, object]:
    data: dict[str, object] = {
        "id": check.id,
        "ok": check.ok,
        "message": check.message,
    }
    if check.details:
        data["details"] = check.details
    return data


def paths_to_jsonable(app_paths: AppPaths) -> dict[str, object]:
    artifacts = []
    manifest = load_artifact_manifest()
    validation = {name: (ok, message) for name, ok, message in validate_artifacts(app_paths.distribution_root)}
    for name, record in sorted(manifest.items()):
        ok, message = validation.get(name, (False, "not validated"))
        artifacts.append({
            "name": name,
            "repo_relative_path": record.path,
            "absolute_path": str(app_paths.distribution_root / record.path),
            "sha256": record.sha256,
            "ok": ok,
            "message": message,
        })
    return {
        "distribution_root": str(app_paths.distribution_root),
        "config_path": str(app_paths.config_path),
        "state_dir": str(app_paths.state_dir),
        "package_root": str(app_paths.package_root),
        "artifact_manifest": artifact_manifest_source(),
        "artifacts": artifacts,
    }


def validate_python_modules() -> InstallCheckResult:
    missing: list[str] = []
    for module_name in REQUIRED_PYTHON_MODULES:
        try:
            importlib.import_module(module_name)
        except Exception as exc:
            missing.append(f"{module_name}: {type(exc).__name__}: {exc}")
    if missing:
        return InstallCheckResult(
            "python_modules",
            False,
            "missing required Python module(s)",
            {"missing": missing},
        )
    return InstallCheckResult("python_modules", True, "required Python modules import")


def validate_boot_assets() -> InstallCheckResult:
    missing_or_empty: list[str] = []
    for name in BOOT_ASSET_NAMES:
        try:
            text = load_boot_asset_text(name)
        except Exception as exc:
            missing_or_empty.append(f"{name}: {type(exc).__name__}: {exc}")
            continue
        if not text.strip():
            missing_or_empty.append(f"{name}: empty")
    if missing_or_empty:
        return InstallCheckResult(
            "boot_assets",
            False,
            "boot asset validation failed",
            {"failures": missing_or_empty},
        )
    return InstallCheckResult("boot_assets", True, "boot assets are readable")


def validate_distribution(app_paths: AppPaths) -> InstallCheckResult:
    validation = validate_distribution_root(app_paths.distribution_root)
    if validation.ok:
        return InstallCheckResult("distribution_root", True, f"distribution root is valid: {validation.root}")
    return InstallCheckResult(
        "distribution_root",
        False,
        validation.error,
        {"missing_artifacts": list(validation.missing_artifacts)},
    )


def validate_artifact_hashes(app_paths: AppPaths) -> InstallCheckResult:
    failures = [
        message
        for _name, ok, message in validate_artifacts(app_paths.distribution_root)
        if not ok
    ]
    if failures:
        return InstallCheckResult("artifact_hashes", False, "artifact validation failed", {"failures": failures})
    return InstallCheckResult("artifact_hashes", True, "all payload artifact hashes match")


def validate_templates(app_paths: AppPaths) -> InstallCheckResult:
    config = AppConfig.from_values(dict(DEFAULTS), path=app_paths.config_path)
    bundle = build_template_bundle(config)
    try:
        render_checked_template("start-samba.sh", bundle.start_script_replacements)
        render_checked_template("watchdog.sh", bundle.watchdog_replacements)
        smbconf_text = render_checked_template(
            "smb.conf.template",
            bundle.smbconf_replacements,
            allowed_unresolved_tokens=SMBCONF_RUNTIME_TOKENS,
        )
        render_runtime_smbconf_text(smbconf_text)
    except Exception as exc:
        return InstallCheckResult("templates", False, f"template validation failed: {exc}")
    return InstallCheckResult("templates", True, "deployment templates render without unexpected tokens")


def validate_install(app_paths: AppPaths | None = None) -> list[InstallCheckResult]:
    resolved_paths = app_paths or resolve_app_paths()
    return [
        validate_python_modules(),
        validate_boot_assets(),
        validate_distribution(resolved_paths),
        validate_artifact_hashes(resolved_paths),
        validate_templates(resolved_paths),
    ]


def install_checks_to_jsonable(checks: list[InstallCheckResult]) -> list[dict[str, object]]:
    return [check_to_jsonable(check) for check in checks]


def install_ok(checks: list[InstallCheckResult]) -> bool:
    return all(check.ok for check in checks)
