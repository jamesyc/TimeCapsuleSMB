from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CheckResult:
    status: str
    message: str


def is_fatal(result: CheckResult) -> bool:
    return result.status == "FAIL"
