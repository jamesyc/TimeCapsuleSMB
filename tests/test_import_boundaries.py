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


def test_app_layer_does_not_import_cli_layer() -> None:
    offenders: list[str] = []
    for path in (SRC_ROOT / "app").rglob("*.py"):
        for module in _imports(path):
            if module == "timecapsulesmb.cli" or module.startswith("timecapsulesmb.cli."):
                offenders.append(f"{path.relative_to(REPO_ROOT)} imports {module}")

    assert offenders == []


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
