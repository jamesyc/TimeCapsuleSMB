from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class OperationCallbacks:
    """Entrypoint-neutral hooks for long-running service operations."""

    set_stage: Callable[[str], None] | None = None
    log: Callable[[str], None] | None = None
    add_debug_fields: Callable[..., None] | None = None
    update_fields: Callable[..., None] | None = None
    record_execution_measurement: Callable[..., None] | None = None

    def stage(self, stage: str) -> None:
        if self.set_stage is not None:
            self.set_stage(stage)

    def message(self, message: str) -> None:
        if self.log is not None:
            self.log(message)

    def debug(self, **fields: object) -> None:
        if self.add_debug_fields is not None:
            self.add_debug_fields(**fields)

    def update(self, **fields: object) -> None:
        if self.update_fields is not None:
            self.update_fields(**fields)

    def measurement(self, kind: str, **fields: object) -> None:
        if self.record_execution_measurement is not None:
            self.record_execution_measurement(kind, **fields)
