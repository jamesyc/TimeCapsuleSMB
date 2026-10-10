from __future__ import annotations

from dataclasses import dataclass, field


# Where doctor measured a result: on or from this computer, or on the device
# over SSH. The macOS app shows the computer section as "This Mac" and keeps
# the device's health apart from it.
DOMAIN_COMPUTER = "computer"
DOMAIN_DEVICE = "device"
# SSH login failed, so nothing on the device was checked.
DEVICE_NOT_CHECKED_CODE = "device_not_checked"


@dataclass
class CheckResult:
    status: str
    message: str
    details: dict[str, object] = field(default_factory=dict)
    # Set by the doctor run's section; a result's identity does not include it.
    domain: str | None = field(default=None, compare=False)


def is_fatal(result: CheckResult) -> bool:
    return result.status == "FAIL"
