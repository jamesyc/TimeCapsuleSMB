from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from timecapsulesmb.app.context import AppOperationContext
from timecapsulesmb.app.ops.configure import configure_operation
from timecapsulesmb.app.ops.deploy import deploy_operation
from timecapsulesmb.app.ops.discovery import discover_operation
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
    set_telemetry_operation,
    validate_install_operation,
    version_check_operation,
)
from timecapsulesmb.app.ops.set_ssh import set_ssh_operation
from timecapsulesmb.services.app import OperationResult


OperationHandler = Callable[[dict[str, object], AppOperationContext], OperationResult]


@dataclass(frozen=True)
class OperationSpec:
    name: str
    handler: OperationHandler
    telemetry: bool = False
    public: bool = True


OPERATION_SPECS: tuple[OperationSpec, ...] = (
    OperationSpec("activate", activate_operation, telemetry=True),
    OperationSpec("capabilities", capabilities_operation),
    OperationSpec("configure", configure_operation, telemetry=True),
    OperationSpec("deploy", deploy_operation, telemetry=True),
    OperationSpec("discover", discover_operation, telemetry=True),
    OperationSpec("doctor", doctor_operation, telemetry=True),
    OperationSpec("flash", flash_operation, telemetry=True),
    OperationSpec("fsck", fsck_operation, telemetry=True),
    OperationSpec("reachability", reachability_operation),
    OperationSpec("repair-xattrs", repair_xattrs_operation, telemetry=True),
    OperationSpec("set-ssh", set_ssh_operation, telemetry=True),
    OperationSpec("set-telemetry", set_telemetry_operation),
    OperationSpec("uninstall", uninstall_operation, telemetry=True),
    OperationSpec("validate-install", validate_install_operation),
    OperationSpec("version-check", version_check_operation),
)


OPERATIONS: dict[str, OperationHandler] = {spec.name: spec.handler for spec in OPERATION_SPECS}
TELEMETRY_OPERATIONS = frozenset(spec.name for spec in OPERATION_SPECS if spec.telemetry)


def public_operation_names() -> list[str]:
    return [spec.name for spec in OPERATION_SPECS if spec.public]
