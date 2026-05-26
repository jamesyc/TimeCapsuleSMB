from __future__ import annotations

from collections.abc import Callable

from timecapsulesmb.app.events import EventSink
from timecapsulesmb.app.ops.configure import configure_operation
from timecapsulesmb.app.ops.deploy import deploy_operation
from timecapsulesmb.app.ops.doctor import doctor_operation
from timecapsulesmb.app.ops.flash import flash_operation
from timecapsulesmb.app.ops.maintenance import (
    activate_operation,
    fsck_operation,
    repair_xattrs_operation,
    uninstall_operation,
)
from timecapsulesmb.app.ops.reachability import reachability_operation
from timecapsulesmb.app.ops.readiness import (
    capabilities_operation,
    discover_operation,
    paths_operation,
    set_telemetry_operation,
    telemetry_identity_operation,
    validate_install_operation,
    version_check_operation,
)
from timecapsulesmb.services.app import OperationResult


OPERATIONS: dict[str, Callable[[dict[str, object], EventSink], OperationResult]] = {
    "activate": activate_operation,
    "capabilities": capabilities_operation,
    "configure": configure_operation,
    "deploy": deploy_operation,
    "discover": discover_operation,
    "doctor": doctor_operation,
    "flash": flash_operation,
    "fsck": fsck_operation,
    "paths": paths_operation,
    "reachability": reachability_operation,
    "repair-xattrs": repair_xattrs_operation,
    "set-telemetry": set_telemetry_operation,
    "telemetry-identity": telemetry_identity_operation,
    "uninstall": uninstall_operation,
    "validate-install": validate_install_operation,
    "version-check": version_check_operation,
}


TELEMETRY_OPERATIONS = frozenset({
    "activate",
    "configure",
    "deploy",
    "doctor",
    "flash",
    "fsck",
    "repair-xattrs",
    "uninstall",
})
