from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Callable

from timecapsulesmb.core.redaction import redact_sensitive_fields
from timecapsulesmb.core.summaries import Summary
from timecapsulesmb.app.stage_policy import stage_policy


def redact(value: object) -> object:
    return redact_sensitive_fields(value)


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


class ClientDisconnected(BaseException):
    """The app went away; the operation stops before a stage it could cancel.

    A BaseException like KeyboardInterrupt, so the broad `except Exception`
    handlers in the services cannot turn it into an ordinary failure.
    `stage` is the stage it did not start, `during` the one in progress when
    the helper learned the app was gone.
    """

    def __init__(self, stage: str | None, during: str | None = None) -> None:
        super().__init__(stage or "")
        self.stage = stage
        self.during = during


class AppClient:
    """Whether the app is still reading, shared by every copy of one sink.

    The app quitting leaves the helper running with a closed stdout, which
    the helper learns at its next write; every stage starts with one. The
    operation then stops where the app's Cancel button would have let the
    user stop it (AppOperationContext.stop_if_disconnected): the stage in
    progress and any later stages that cannot be cancelled, such as a Flash
    write and its flush, finish first.
    """

    def __init__(self, on_disconnect: Callable[[], None] | None = None) -> None:
        self.disconnected = False
        self.disconnected_during: str | None = None
        self._on_disconnect = on_disconnect

    def disconnect(self, during: str | None = None) -> None:
        if self.disconnected:
            return
        self.disconnected = True
        self.disconnected_during = during
        if self._on_disconnect is not None:
            self._on_disconnect()

    def stop_if_disconnected(self, stage: str | None = None) -> None:
        if self.disconnected:
            raise ClientDisconnected(stage, self.disconnected_during)


class EventSink:
    def __init__(
        self,
        emit: Callable[[AppEvent], None],
        *,
        request_id: str | None = None,
        schema_version: int = 1,
        client: AppClient | None = None,
    ) -> None:
        self._emit = emit
        self.request_id = request_id or str(uuid.uuid4())
        self.schema_version = schema_version
        self.client = client or AppClient()
        self._current_stage_by_operation: dict[str, str] = {}
        self._current_risk_by_operation: dict[str, str] = {}

    def with_request_id(self, request_id: str) -> "EventSink":
        return EventSink(self._emit, request_id=request_id, schema_version=self.schema_version, client=self.client)

    def emit(self, event: AppEvent) -> None:
        if self.client.disconnected:
            return
        if event.request_id is None:
            event = AppEvent(
                event.type,
                event.operation,
                event.fields,
                request_id=self.request_id,
                schema_version=self.schema_version,
            )
        try:
            self._emit(event)
        except BrokenPipeError:
            # A stage event is sent before the stage becomes current, so this
            # names the stage that was running when the app went away.
            self.client.disconnect(self._current_stage_by_operation.get(event.operation))

    def current_stage(self, operation: str) -> str | None:
        return self._current_stage_by_operation.get(operation)

    def current_risk(self, operation: str) -> str | None:
        return self._current_risk_by_operation.get(operation)

    def stage(self, operation: str, stage: str) -> None:
        fields: dict[str, object] = {"stage": stage}
        policy = stage_policy(operation, stage)
        if policy is not None:
            fields.update(policy.to_jsonable())
        self.emit(AppEvent("stage", operation, fields))
        self._current_stage_by_operation[operation] = stage
        risk = fields.get("risk")
        if isinstance(risk, str):
            self._current_risk_by_operation[operation] = risk

    def log(self, operation: str, message: str, *, level: str = "info", summary: Summary | None = None) -> None:
        fields: dict[str, object] = {"level": level, "message": message}
        if summary is not None:
            fields.update(summary.message_fields())
        self.emit(AppEvent("log", operation, fields))

    def progress(self, operation: str, stage: str, **fields: object) -> None:
        """How far a long stage has got; the app shows it in that stage's row."""
        self.emit(AppEvent("progress", operation, {"stage": stage, **fields}))

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

    def result(
        self,
        operation: str,
        *,
        ok: bool,
        payload: object | None = None,
        debug: object | None = None,
    ) -> None:
        fields: dict[str, object] = {"ok": ok, "payload": payload if payload is not None else {}}
        if debug is not None:
            fields["debug"] = debug
        self.emit(AppEvent("result", operation, fields))

    def error(
        self,
        operation: str,
        message: str,
        *,
        code: str = "operation_failed",
        details: object | None = None,
        debug: object | None = None,
        recovery: object | None = None,
    ) -> None:
        fields: dict[str, object] = {"code": code, "message": message}
        if details is not None:
            fields["details"] = details
        if debug is not None:
            fields["debug"] = debug
        if recovery is not None:
            fields["recovery"] = recovery
        self.emit(AppEvent("error", operation, fields))
