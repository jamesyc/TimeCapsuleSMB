from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path


EXECUTION_TELEMETRY_VERSION = 1
MAX_MEASUREMENTS_PER_KIND = 50
MAX_STRING_FIELD_CHARS = 1024
SENSITIVE_FIELD_PARTS = ("credentials", "password", "secret", "token")


class ExecutionTelemetryRecorder:
    """Records bounded, structured operation timing telemetry."""

    def __init__(self, *, monotonic: Callable[[], float] | None = None) -> None:
        self._monotonic = monotonic or time.monotonic
        self._started_at = self._monotonic()
        self._current_stage: dict[str, object] | None = None
        self._stages: list[dict[str, object]] = []
        self._measurements: dict[str, list[dict[str, object]]] = {}
        self._slow_flags: list[str] = []

    def set_stage(self, name: str) -> None:
        clean_name = str(name).strip()
        if not clean_name:
            return
        if self._current_stage is not None and self._current_stage["name"] == clean_name:
            return
        now = self._monotonic()
        self._close_current_stage(now, result="success")
        self._current_stage = {
            "name": clean_name,
            "index": len(self._stages) + 1,
            "start": now,
        }

    def record_measurement(self, kind: str, **fields: object) -> None:
        clean_kind = str(kind).strip()
        if not clean_kind:
            return
        values = self._measurements.setdefault(clean_kind, [])
        if len(values) >= MAX_MEASUREMENTS_PER_KIND:
            return
        values.append(_jsonable_mapping(fields))

    def add_slow_flag(self, name: str) -> None:
        clean_name = str(name).strip()
        if clean_name and clean_name not in self._slow_flags:
            self._slow_flags.append(clean_name)

    def to_jsonable(self, *, result: str, duration_sec: float | None = None) -> dict[str, object]:
        now = self._monotonic()
        self._close_current_stage(now, result=result if result != "success" else "success")
        effective_duration_sec = duration_sec if duration_sec is not None else now - self._started_at
        output: dict[str, object] = {
            "version": EXECUTION_TELEMETRY_VERSION,
            "duration_sec": _round_seconds(effective_duration_sec),
        }
        if self._stages:
            output["stages"] = list(self._stages)
            output["stage_totals"] = _stage_totals(self._stages)
            slowest_stage = max(self._stages, key=lambda stage: float(stage.get("duration_sec") or 0.0))
            output["slowest_stage"] = {
                "name": slowest_stage["name"],
                "duration_sec": slowest_stage["duration_sec"],
            }
        if self._measurements:
            output["measurements"] = {
                kind: list(values)
                for kind, values in sorted(self._measurements.items())
                if values
            }
        if self._slow_flags:
            output["slow_flags"] = list(self._slow_flags)
        return output

    def _close_current_stage(self, now: float, *, result: str) -> None:
        if self._current_stage is None:
            return
        start = float(self._current_stage["start"])
        self._stages.append({
            "name": self._current_stage["name"],
            "index": self._current_stage["index"],
            "start_offset_sec": _round_seconds(start - self._started_at),
            "duration_sec": _round_seconds(now - start),
            "result": result,
        })
        self._current_stage = None


def _stage_totals(stages: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    totals: dict[str, dict[str, object]] = {}
    for stage in stages:
        name = str(stage["name"])
        total = totals.setdefault(name, {"count": 0, "duration_sec": 0.0})
        total["count"] = int(total["count"]) + 1
        total["duration_sec"] = _round_seconds(float(total["duration_sec"]) + float(stage["duration_sec"]))
    return totals


def _round_seconds(value: float) -> float:
    return round(max(0.0, value), 3)


def _jsonable_mapping(fields: Mapping[str, object]) -> dict[str, object]:
    output: dict[str, object] = {}
    for key, value in fields.items():
        if value is None or _is_sensitive_field(key):
            continue
        output[key] = _jsonable(value)
    return output


def _is_sensitive_field(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in SENSITIVE_FIELD_PARTS)


def _jsonable(value: object) -> object:
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, str) and len(value) > MAX_STRING_FIELD_CHARS:
            return value[:MAX_STRING_FIELD_CHARS]
        return value
    if isinstance(value, Enum):
        return _jsonable(value.value)
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return _jsonable(asdict(value))
    if isinstance(value, Mapping):
        return {
            str(key): _jsonable(item)
            for key, item in value.items()
            if item is not None and not _is_sensitive_field(str(key))
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    return str(value)
