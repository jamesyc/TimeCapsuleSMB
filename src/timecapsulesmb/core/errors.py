from __future__ import annotations

import importlib
import shlex
import sys
from collections.abc import Iterable
from pathlib import Path

from timecapsulesmb.core.paths import package_project_root


def _is_source_checkout(root: Path) -> bool:
    return (
        (root / "tcapsule").is_file()
        and (root / "src" / "timecapsulesmb").is_dir()
    )


def _active_venv_matches(expected_venv: Path) -> bool:
    try:
        return Path(sys.prefix).resolve() == expected_venv.resolve()
    except (OSError, RuntimeError):
        return False


def _current_tcapsule_command() -> str:
    if len(sys.argv) <= 1:
        return ".venv/bin/tcapsule <command>"
    return shlex.join([".venv/bin/tcapsule", *sys.argv[1:]])


def _dependency_recovery_message(install_command: str) -> str:
    generic_recovery = f"Run `{install_command}` first to set up the required dependencies."
    try:
        root = package_project_root()
        if not _is_source_checkout(root):
            return generic_recovery
        venv = root / ".venv"
        launcher = venv / "bin" / "tcapsule"
        if launcher.is_file():
            if not _active_venv_matches(venv):
                return (
                    "This source checkout already has a bootstrapped virtualenv at `.venv`, "
                    f"but this command is running with Python {sys.executable}. "
                    f"Run `{_current_tcapsule_command()}` instead."
                )
            return (
                "The source checkout virtualenv at `.venv` is active but missing required packages. "
                f"Rerun `{install_command}` to repair it."
            )
        if (venv / "pyvenv.cfg").is_file():
            return (
                "The source checkout virtualenv at `.venv` is incomplete because `.venv/bin/tcapsule` is missing. "
                f"Rerun `{install_command}` to repair it."
            )
    except (OSError, RuntimeError):
        return generic_recovery
    return generic_recovery


def missing_dependency_message(
    module_name: str,
    error: BaseException | None = None,
    *,
    install_command: str = "./tcapsule bootstrap",
    rerun_command: str | None = None,
) -> str:
    error_suffix = f" {type(error).__name__}: {error}" if error is not None else ""
    recovery = _dependency_recovery_message(install_command)
    message = (
        f"Failed to load {module_name}. Install the Python package {module_name}. "
        f"{recovery}"
    )
    if rerun_command:
        message += f" Then rerun `{rerun_command}`."
    return message + error_suffix


def missing_required_python_module(module_names: Iterable[str]) -> tuple[str, BaseException] | None:
    for module_name in module_names:
        try:
            importlib.import_module(module_name)
        except Exception as exc:
            return module_name, exc
    return None


def require_python_module(module_name: str, message: str) -> None:
    try:
        importlib.import_module(module_name)
    except Exception as exc:
        raise RuntimeError(message) from exc


def system_exit_message(exc: object) -> str:
    code = getattr(exc, "code", None)
    if isinstance(code, int):
        if code == 0:
            return "0"
        return f"SystemExit: {code}"
    return str(exc)
