from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from enum import Enum
import math
from pathlib import Path

from timecapsulesmb.core.error_codes import CodedError, ErrorCode


class AppOperationError(CodedError):
    code: ErrorCode

    def __init__(
        self,
        message: str,
        *,
        code: ErrorCode | str = ErrorCode.OPERATION_FAILED,
        detail: str | None = None,
        debug: object | None = None,
        recovery: object | None = None,
    ) -> None:
        super().__init__(message, code=code, detail=detail)
        self.debug = debug
        self.recovery = recovery


@dataclass(frozen=True)
class OperationResult:
    ok: bool
    payload: object | None = None
    diagnostic_error: object | None = None


def jsonable(value: object) -> object:
    if is_dataclass(value):
        return jsonable(asdict(value))
    if isinstance(value, Enum):
        return jsonable(value.value)
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
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "y"}:
            return True
        if normalized in {"0", "false", "no", "n"}:
            return False
    raise AppOperationError(f"{name} must be a boolean", code=ErrorCode.VALIDATION_FAILED)


def optional_bool_param(params: dict[str, object], name: str) -> bool | None:
    if name not in params or params.get(name) in (None, ""):
        return None
    return bool_param(params, name)


def int_param(params: dict[str, object], name: str, default: int) -> int:
    value = params.get(name, default)
    if isinstance(value, bool):
        raise AppOperationError(f"{name} must be an integer", code=ErrorCode.VALIDATION_FAILED)
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            raise AppOperationError(f"{name} must be an integer", code=ErrorCode.VALIDATION_FAILED)
        parsed = int(value)
    else:
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise AppOperationError(f"{name} must be an integer", code=ErrorCode.VALIDATION_FAILED) from exc
    if parsed < 0:
        raise AppOperationError(f"{name} must be 0 or greater", code=ErrorCode.VALIDATION_FAILED)
    return parsed


def string_param(params: dict[str, object], name: str, default: str = "") -> str:
    value = params.get(name, default)
    return "" if value is None else str(value)


def require_string_param(params: dict[str, object], name: str) -> str:
    value = string_param(params, name).strip()
    if not value:
        raise AppOperationError(f"missing required parameter: {name}", code=ErrorCode.VALIDATION_FAILED)
    return value


def required_path_param(params: dict[str, object], name: str) -> Path:
    value = params.get(name)
    if value is None:
        raise AppOperationError(f"missing required parameter: {name}", code=ErrorCode.VALIDATION_FAILED)
    if isinstance(value, Path):
        path_text = str(value).strip()
    elif isinstance(value, str):
        path_text = value.strip()
    else:
        raise AppOperationError(f"{name} must be a path string", code=ErrorCode.VALIDATION_FAILED)
    if not path_text:
        raise AppOperationError(f"missing required parameter: {name}", code=ErrorCode.VALIDATION_FAILED)
    return Path(path_text)
