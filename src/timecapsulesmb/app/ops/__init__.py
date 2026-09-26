from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from timecapsulesmb.app.context import AppOperationContext
from timecapsulesmb.app.ops.configure import (
    LOCAL_NETWORK_PREFLIGHT_PARAM_KEYS,
    configure_operation,
    update_config_settings_operation,
)
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
    # Request params the operation reads. Any other name is rejected, so a
    # misspelled param fails loudly instead of silently taking its default.
    params: frozenset[str] = frozenset()
    telemetry: bool = False
    public: bool = True


# Read by the shared request helpers for every operation: the config path,
# request-scoped credentials, and the answer to a confirmation.
COMMON_PARAMS = frozenset({"config", "credentials", "password", "confirmation_id", "confirmation"})
MANAGED_SETTING_PARAMS = frozenset({
    "any_protocol",
    "ata_idle_seconds",
    "ata_standby",
    "debug_logging",
    "force_disable_smb_signing_and_encryption",
    "fruit_metadata_netatalk",
    "internal_share_use_disk_root",
    "mdns_advertise_afp",
    "require_smb_encryption",
    "smb_browse_compatibility",
    "vfs_aio_fork_enabled",
})
REBOOT_PARAMS = frozenset({"dry_run", "mount_wait", "no_reboot", "no_wait"})


OPERATION_SPECS: tuple[OperationSpec, ...] = (
    OperationSpec("activate", activate_operation, frozenset({"dry_run"}), telemetry=True),
    OperationSpec("capabilities", capabilities_operation),
    OperationSpec(
        "configure",
        configure_operation,
        MANAGED_SETTING_PARAMS | frozenset(LOCAL_NETWORK_PREFLIGHT_PARAM_KEYS) | frozenset({
            "enable_ssh",
            "host",
            "persist_password",
            "selected_record",
            "ssh_opts",
            "ssh_wait_timeout",
        }),
        telemetry=True,
    ),
    OperationSpec("update-config-settings", update_config_settings_operation, MANAGED_SETTING_PARAMS, public=False),
    OperationSpec(
        "deploy",
        deploy_operation,
        # nbns_enabled is read only to refuse it with its own message.
        MANAGED_SETTING_PARAMS | REBOOT_PARAMS | frozenset({"allow_unsupported", "nbns_enabled", "rsync_enabled"}),
        telemetry=True,
    ),
    OperationSpec("discover", discover_operation, frozenset({"timeout"}), telemetry=True),
    OperationSpec(
        "doctor",
        doctor_operation,
        # bonjour_timeout is retired and accepted only to be ignored.
        frozenset({"bonjour_timeout", "skip_bonjour", "skip_smb", "skip_ssh", "startup_grace"}),
        telemetry=True,
    ),
    OperationSpec(
        "flash",
        flash_operation,
        frozenset({
            "action",
            "backup_dir",
            "firmware_template",
            "firmware_version",
            "force",
            "mode",
            "reboot_after_write",
            "wait_after_reboot",
        }),
        telemetry=True,
    ),
    OperationSpec("fsck", fsck_operation, REBOOT_PARAMS | frozenset({"list_volumes", "volume"}), telemetry=True),
    OperationSpec(
        "reachability",
        reachability_operation,
        frozenset({"host", "hosts", "smb_host", "smb_hosts", "ssh_host", "ssh_timeout", "tcp_timeout"}),
    ),
    OperationSpec(
        "repair-xattrs",
        repair_xattrs_operation,
        frozenset({
            "dry_run",
            "fix_permissions",
            "include_hidden",
            "include_time_machine",
            "max_depth",
            "path",
            "recursive",
            "verbose",
        }),
        telemetry=True,
    ),
    OperationSpec("set-ssh", set_ssh_operation, frozenset({"action", "no_wait"}), telemetry=True),
    OperationSpec("set-telemetry", set_telemetry_operation, frozenset({"enabled"})),
    OperationSpec("uninstall", uninstall_operation, REBOOT_PARAMS, telemetry=True),
    OperationSpec("validate-install", validate_install_operation),
    OperationSpec("version-check", version_check_operation, frozenset({"url"})),
)


OPERATIONS: dict[str, OperationHandler] = {spec.name: spec.handler for spec in OPERATION_SPECS}
OPERATION_PARAMS: dict[str, frozenset[str]] = {spec.name: spec.params | COMMON_PARAMS for spec in OPERATION_SPECS}
TELEMETRY_OPERATIONS = frozenset(spec.name for spec in OPERATION_SPECS if spec.telemetry)


def unknown_params(operation: str, params: dict[str, object]) -> list[str]:
    accepted = OPERATION_PARAMS.get(operation)
    if accepted is None:
        return []
    return sorted(set(params) - accepted)


def public_operation_names() -> list[str]:
    return [spec.name for spec in OPERATION_SPECS if spec.public]
