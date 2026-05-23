from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from pathlib import Path

from timecapsulesmb.core.paths import (
    AppPaths,
    artifact_manifest_source,
    resolve_app_paths,
    validate_distribution_root,
)
from timecapsulesmb.deploy.artifacts import load_artifact_manifest, validate_artifacts
from timecapsulesmb.deploy.boot_assets import (
    load_boot_asset_text,
    require_no_unresolved_asset_tokens,
)


REQUIRED_PYTHON_MODULES = ("zeroconf", "pexpect", "ifaddr")
BOOT_ASSET_NAMES = (
    "rc.local",
    "common.sh",
    "boot.sh",
    "manager.sh",
    "start-samba.sh",
    "watchdog.sh",
    "dfree.sh",
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


def validate_boot_script_tokens(app_paths: AppPaths) -> InstallCheckResult:
    try:
        require_no_unresolved_asset_tokens(load_boot_asset_text("boot.sh"))
        require_no_unresolved_asset_tokens(load_boot_asset_text("manager.sh"))
        require_no_unresolved_asset_tokens(load_boot_asset_text("start-samba.sh"))
        require_no_unresolved_asset_tokens(load_boot_asset_text("watchdog.sh"))
    except Exception as exc:
        return InstallCheckResult("boot_script_tokens", False, f"boot script validation failed: {exc}")
    return InstallCheckResult("boot_script_tokens", True, "managed boot scripts have no unresolved tokens")


def validate_install(app_paths: AppPaths | None = None) -> list[InstallCheckResult]:
    resolved_paths = app_paths or resolve_app_paths()
    return [
        validate_python_modules(),
        validate_boot_assets(),
        validate_distribution(resolved_paths),
        validate_artifact_hashes(resolved_paths),
        validate_boot_script_tokens(resolved_paths),
    ]


def install_checks_to_jsonable(checks: list[InstallCheckResult]) -> list[dict[str, object]]:
    return [check_to_jsonable(check) for check in checks]


def install_ok(checks: list[InstallCheckResult]) -> bool:
    return all(check.ok for check in checks)
