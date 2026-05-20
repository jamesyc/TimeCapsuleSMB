from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from timecapsulesmb.app.stage_policy import stage_policy


SENSITIVE_KEY_PARTS = ("password", "secret", "token", "key")
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
    request_id: str | None = None
    schema_version: int = 1

    def to_jsonable(self) -> dict[str, object]:
        data = {"schema_version": self.schema_version, "type": self.type, "operation": self.operation}
        if self.request_id:
            data["request_id"] = self.request_id
        data.update(redact(self.fields))
        return data

    def to_json_line(self) -> str:
        return json.dumps(self.to_jsonable(), sort_keys=True) + "\n"


class EventSink:
    def __init__(
        self,
        emit: Callable[[AppEvent], None],
        *,
        request_id: str | None = None,
        schema_version: int = 1,
    ) -> None:
        self._emit = emit
        self.request_id = request_id or str(uuid.uuid4())
        self.schema_version = schema_version
        self._current_stage_by_operation: dict[str, str] = {}

    def with_request_id(self, request_id: str) -> "EventSink":
        return EventSink(self._emit, request_id=request_id, schema_version=self.schema_version)

    def emit(self, event: AppEvent) -> None:
        if event.request_id is None:
            event = AppEvent(
                event.type,
                event.operation,
                event.fields,
                request_id=self.request_id,
                schema_version=self.schema_version,
            )
        self._emit(event)

    def current_stage(self, operation: str) -> str | None:
        return self._current_stage_by_operation.get(operation)

    def stage(self, operation: str, stage: str) -> None:
        self._current_stage_by_operation[operation] = stage
        fields: dict[str, object] = {"stage": stage}
        policy = stage_policy(operation, stage)
        if policy is not None:
            fields.update(policy.to_jsonable())
        self.emit(AppEvent("stage", operation, fields))

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

    def error(
        self,
        operation: str,
        message: str,
        *,
        code: str = "operation_failed",
        debug: object | None = None,
        recovery: object | None = None,
    ) -> None:
        fields: dict[str, object] = {"code": code, "message": message}
        if debug is not None:
            fields["debug"] = debug
        if recovery is not None:
            fields["recovery"] = recovery
        self.emit(AppEvent("error", operation, fields))
