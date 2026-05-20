from __future__ import annotations

from collections.abc import Callable

from timecapsulesmb.app.events import EventSink
from timecapsulesmb.app.ops.configure import configure_operation
from timecapsulesmb.app.ops.deploy import deploy_operation
from timecapsulesmb.app.ops.doctor import doctor_operation
from timecapsulesmb.app.ops.maintenance import (
    activate_operation,
    fsck_operation,
    repair_xattrs_operation,
    uninstall_operation,
)
from timecapsulesmb.app.ops.readiness import discover_operation, paths_operation, validate_install_operation
from timecapsulesmb.services.app import OperationResult


OPERATIONS: dict[str, Callable[[dict[str, object], EventSink], OperationResult]] = {
    "activate": activate_operation,
    "configure": configure_operation,
    "deploy": deploy_operation,
    "discover": discover_operation,
    "doctor": doctor_operation,
    "fsck": fsck_operation,
    "paths": paths_operation,
    "repair-xattrs": repair_xattrs_operation,
    "uninstall": uninstall_operation,
    "validate-install": validate_install_operation,
}
