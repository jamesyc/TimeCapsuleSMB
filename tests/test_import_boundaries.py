from __future__ import annotations

import ast
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src" / "timecapsulesmb"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def _matches_prefix(module: str, prefixes: tuple[str, ...]) -> bool:
    return any(module == prefix or module.startswith(f"{prefix}.") for prefix in prefixes)


def _import_violations(root: Path, forbidden_prefixes: tuple[str, ...]) -> list[str]:
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        for module in _imports(path):
            if _matches_prefix(module, forbidden_prefixes):
                offenders.append(f"{path.relative_to(REPO_ROOT)} imports {module}")
    return sorted(offenders)


def test_app_layer_does_not_import_cli_layer() -> None:
    assert _import_violations(
        SRC_ROOT / "app",
        ("timecapsulesmb.cli",),
    ) == []


def test_services_layer_does_not_import_adapters() -> None:
    assert _import_violations(
        SRC_ROOT / "services",
        (
            "timecapsulesmb.app",
            "timecapsulesmb.cli",
        ),
    ) == []


def test_domain_layers_do_not_import_adapters() -> None:
    offenders: list[str] = []
    for package in ("core", "device", "deploy"):
        offenders.extend(
            _import_violations(
                SRC_ROOT / package,
                (
                    "timecapsulesmb.app",
                    "timecapsulesmb.cli",
                ),
            )
        )

    assert sorted(offenders) == []


def test_deploy_adapters_do_not_import_low_level_deploy_dependencies() -> None:
    forbidden_prefixes = (
        "timecapsulesmb.deploy.",
        "timecapsulesmb.device.compat",
        "timecapsulesmb.device.probe",
        "timecapsulesmb.device.storage",
    )
    offenders: list[str] = []
    for path in (
        SRC_ROOT / "cli" / "deploy.py",
        SRC_ROOT / "app" / "ops" / "deploy.py",
    ):
        for module in _imports(path):
            if any(module == prefix.rstrip(".") or module.startswith(prefix) for prefix in forbidden_prefixes):
                offenders.append(f"{path.relative_to(REPO_ROOT)} imports {module}")

    assert offenders == []
