from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from timecapsulesmb.core.summaries import Summary


@dataclass(frozen=True)
class OperationCallbacks:
    """Entrypoint-neutral hooks for long-running service operations."""

    set_stage: Callable[[str], None] | None = None
    log: Callable[[str], None] | None = None
    # Receives keyed messages the app can translate; plain `log` gets their text.
    log_summary: Callable[[Summary], None] | None = None
    add_debug_fields: Callable[..., None] | None = None
    update_fields: Callable[..., None] | None = None
    record_execution_measurement: Callable[..., None] | None = None

    def stage(self, stage: str) -> None:
        if self.set_stage is not None:
            self.set_stage(stage)

    def message(self, message: str | Summary) -> None:
        if isinstance(message, Summary):
            if self.log_summary is not None:
                self.log_summary(message)
            elif self.log is not None:
                self.log(message.text)
            return
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
