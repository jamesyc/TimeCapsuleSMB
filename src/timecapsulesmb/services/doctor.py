from __future__ import annotations

from timecapsulesmb.checks.models import CheckResult


def doctor_status_counts(results: list[CheckResult]) -> dict[str, int]:
    return {
        status: sum(1 for result in results if result.status == status)
        for status in ("PASS", "WARN", "FAIL", "INFO")
    }
