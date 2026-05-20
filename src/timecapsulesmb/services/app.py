from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path


class AppOperationError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "operation_failed",
        debug: object | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.debug = debug


@dataclass(frozen=True)
class OperationResult:
    ok: bool
    payload: object | None = None


def jsonable(value: object) -> object:
    if is_dataclass(value):
        return jsonable(asdict(value))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [jsonable(item) for item in value]
    return value


def config_path(params: dict[str, object]) -> Path | None:
    value = params.get("config")
    if value in (None, ""):
        return None
    return Path(str(value))


def bool_param(params: dict[str, object], name: str, default: bool = False) -> bool:
    value = params.get(name, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return bool(value)


def confirm_param(params: dict[str, object], name: str) -> bool:
    if name in params:
        return bool_param(params, name)
    return bool_param(params, "yes")


def int_param(params: dict[str, object], name: str, default: int) -> int:
    value = params.get(name, default)
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise AppOperationError(f"{name} must be an integer", code="validation_failed") from exc
    if parsed < 0:
        raise AppOperationError(f"{name} must be 0 or greater", code="validation_failed")
    return parsed


def string_param(params: dict[str, object], name: str, default: str = "") -> str:
    value = params.get(name, default)
    return "" if value is None else str(value)


def require_string_param(params: dict[str, object], name: str) -> str:
    value = string_param(params, name).strip()
    if not value:
        raise AppOperationError(f"missing required parameter: {name}", code="validation_failed")
    return value
