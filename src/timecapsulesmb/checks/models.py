from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class CheckResult:
    status: str
    message: str
    details: dict[str, object] = field(default_factory=dict)


def is_fatal(result: CheckResult) -> bool:
    return result.status == "FAIL"
