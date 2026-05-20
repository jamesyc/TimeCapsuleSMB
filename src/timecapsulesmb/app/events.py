from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


SENSITIVE_KEY_PARTS = ("password", "secret", "token")
REDACTED = "<redacted>"


def redact(value: object) -> object:
    if isinstance(value, dict):
        redacted: dict[str, object] = {}
        for key, item in value.items():
            if any(part in str(key).lower() for part in SENSITIVE_KEY_PARTS):
                redacted[str(key)] = REDACTED
            else:
                redacted[str(key)] = redact(item)
        return redacted
    if isinstance(value, (list, tuple, set)):
        return [redact(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


@dataclass(frozen=True)
class AppEvent:
    type: str
    operation: str
    fields: dict[str, object] = field(default_factory=dict)

    def to_jsonable(self) -> dict[str, object]:
        data = {"type": self.type, "operation": self.operation}
        data.update(redact(self.fields))
        return data

    def to_json_line(self) -> str:
        return json.dumps(self.to_jsonable(), sort_keys=True) + "\n"


class EventSink:
    def __init__(self, emit: Callable[[AppEvent], None]) -> None:
        self._emit = emit

    def emit(self, event: AppEvent) -> None:
        self._emit(event)

    def stage(self, operation: str, stage: str) -> None:
        self.emit(AppEvent("stage", operation, {"stage": stage}))

    def log(self, operation: str, message: str, *, level: str = "info") -> None:
        self.emit(AppEvent("log", operation, {"level": level, "message": message}))

    def check(
        self,
        operation: str,
        *,
        status: str,
        message: str,
        details: dict[str, object] | None = None,
    ) -> None:
        self.emit(AppEvent("check", operation, {
            "status": status,
            "message": message,
            "details": details or {},
        }))

    def result(self, operation: str, *, ok: bool, payload: object | None = None) -> None:
        self.emit(AppEvent("result", operation, {"ok": ok, "payload": payload if payload is not None else {}}))

    def error(self, operation: str, message: str, *, debug: object | None = None) -> None:
        fields: dict[str, object] = {"message": message}
        if debug is not None:
            fields["debug"] = debug
        self.emit(AppEvent("error", operation, fields))
