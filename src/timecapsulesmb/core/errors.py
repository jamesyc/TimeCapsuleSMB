from __future__ import annotations


def system_exit_message(exc: object) -> str:
    code = getattr(exc, "code", None)
    if isinstance(code, int):
        if code == 0:
            return "0"
        return f"SystemExit: {code}"
    return str(exc)
