from __future__ import annotations


def missing_dependency_message(module_name: str, error: BaseException | None = None) -> str:
    error_suffix = f" {type(error).__name__}: {error}" if error is not None else ""
    return (
        f"Failed to load {module_name}. Install the Python package {module_name}. "
        f"Run `./tcapsule bootstrap` first to set up the required dependencies.{error_suffix}"
    )


def system_exit_message(exc: object) -> str:
    code = getattr(exc, "code", None)
    if isinstance(code, int):
        if code == 0:
            return "0"
        return f"SystemExit: {code}"
    return str(exc)
