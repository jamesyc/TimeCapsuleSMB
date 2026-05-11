from __future__ import annotations

import importlib
from collections.abc import Iterable


def missing_dependency_message(
    module_name: str,
    error: BaseException | None = None,
    *,
    install_command: str = "./tcapsule bootstrap",
    rerun_command: str | None = None,
) -> str:
    error_suffix = f" {type(error).__name__}: {error}" if error is not None else ""
    message = (
        f"Failed to load {module_name}. Install the Python package {module_name}. "
        f"Run `{install_command}` first to set up the required dependencies."
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
