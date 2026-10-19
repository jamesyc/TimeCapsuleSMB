"""The codes the helper's errors carry.

The app chooses its guidance for a failure by its code (app/recovery.py has an
entry for each), and telemetry counts failures by code instead of by the wording
of their messages. A code travels as its string value; every raise site names a
member, so a misspelled code fails where it is raised instead of reaching the
app as one it has no guidance for.
"""
from __future__ import annotations

from enum import Enum


class ErrorCode(str, Enum):
    # Formats as its value everywhere (f-strings, str(), JSON), as Python
    # 3.11's StrEnum does; plain str mixins print "ErrorCode.X" from 3.12.
    def __str__(self) -> str:
        return self.value

    def __format__(self, format_spec: str) -> str:
        return self.value.__format__(format_spec)

    # The app and its helper disagree about the request.
    INVALID_REQUEST = "invalid_request"
    UNKNOWN_OPERATION = "unknown_operation"
    UNKNOWN_PARAM = "unknown_param"
    VALIDATION_FAILED = "validation_failed"
    CONFIG_ERROR = "config_error"
    CONFIRMATION_REQUIRED = "confirmation_required"
    CANCELLED = "cancelled"
    # The app closed its connection to the helper; only telemetry records it.
    CLIENT_DISCONNECTED = "client_disconnected"

    # Generic: something failed, with nothing more specific known.
    OPERATION_FAILED = "operation_failed"
    REMOTE_ERROR = "remote_error"

    # Reaching the device.
    AUTH_FAILED = "auth_failed"
    DEVICE_UNREACHABLE = "device_unreachable"
    DEVICE_OFF_NETWORK = "device_off_network"
    DEVICE_IDENTITY_MISMATCH = "device_identity_mismatch"
    DEVICE_PROBE_FAILED = "device_probe_failed"
    ACP_UNREACHABLE = "acp_unreachable"
    ACP_NO_ROUTE = "acp_no_route"
    ACPD_BROKEN = "acpd_broken"
    LOCAL_NETWORK_FILTERED = "local_network_filtered"
    LOCAL_NETWORK_PERMISSION_DENIED = "local_network_permission_denied"
    DISCOVERY_TIMEOUT_TOO_SHORT = "discovery_timeout_too_short"
    SSH_CLIENT_FAILED = "ssh_client_failed"
    SSH_COMPATIBILITY_FAILED = "ssh_compatibility_failed"
    SSH_TRANSPORT_FAILED = "ssh_transport_failed"
    SSH_DISABLED = "ssh_disabled"
    # configure turns it into another code before the app sees it.
    SSH_UNREACHABLE = "ssh_unreachable"
    SSH_ENABLE_TIMEOUT = "ssh_enable_timeout"
    SSH_STILL_ENABLED = "ssh_still_enabled"
    UNSUPPORTED_DEVICE = "unsupported_device"

    # Restarting the device.
    REBOOT_NOT_STARTED = "reboot_not_started"
    REBOOT_NOT_FINISHED = "reboot_not_finished"

    # The installed runtime.
    RUNTIME_NOT_INSTALLED = "runtime_not_installed"
    RUNTIME_OUTDATED = "runtime_outdated"
    RUNTIME_NOT_RESTARTED = "runtime_not_restarted"
    CLIENT_OUTDATED = "client_outdated"
    PAYLOAD_MISSING_FROM_DISK = "payload_missing_from_disk"
    PAYLOAD_DAMAGED_ON_DISK = "payload_damaged_on_disk"
    RSYNC_MISSING_FROM_DISK = "rsync_missing_from_disk"
    RSYNC_DAMAGED_ON_DISK = "rsync_damaged_on_disk"
    RSYNC_NOT_RUNNING = "rsync_not_running"

    # The disk.
    DISK_FAILING = "disk_failing"
    DISK_WRITE_TEST_UNRESPONSIVE = "disk_write_test_unresponsive"
    FSCK_ALREADY_RUNNING = "fsck_already_running"
    FSCK_SESSION_DROPPED = "fsck_session_dropped"

    # Installing.
    DEPLOY_NO_DISK_DETECTED = "deploy_no_disk_detected"
    DEPLOY_NO_USB_DISK_DETECTED = "deploy_no_usb_disk_detected"
    DEPLOY_NO_HFS_PARTITION = "deploy_no_hfs_partition"
    DEPLOY_DISK_NOT_WRITABLE = "deploy_disk_not_writable"
    DEPLOY_DISK_NOT_CONFIRMED = "deploy_disk_not_confirmed"
    DEPLOY_DISK_NOT_MOUNTED = "deploy_disk_not_mounted"
    FLASH_CAPACITY_PROBE_FAILED = "flash_capacity_probe_failed"
    INSUFFICIENT_FLASH_SPACE = "insufficient_flash_space"
    MANAGER_STOP_TIMEOUT = "manager_stop_timeout"
    PAYLOAD_UPLOAD_TIMEOUT = "payload_upload_timeout"
    INSTALL_INTERRUPTED = "install_interrupted"
    PREVIOUS_MIGRATION_STALLED = "previous_migration_stalled"
    XATTR_MIGRATION_TIMEOUT = "xattr_migration_timeout"
    XATTR_MIGRATION_STALLED = "xattr_migration_stalled"
    XATTR_MIGRATION_CRASHED = "xattr_migration_crashed"
    XATTR_MIGRATION_STOPPED = "xattr_migration_stopped"
    XATTR_MIGRATION_CONNECTION_LOST = "xattr_migration_connection_lost"
    XATTR_MIGRATION_UNREADABLE_FOLDER = "xattr_migration_unreadable_folder"
    XATTR_MIGRATION_UNREADABLE_FOLDER_REPAIR_ONLY = "xattr_migration_unreadable_folder_repair_only"
    XATTR_MIGRATION_FAILED = "xattr_migration_failed"

    # Flash firmware.
    SECONDARY_BANK_INVALID = "secondary_bank_invalid"
    SECONDARY_BANK_READ_MISMATCH = "secondary_bank_read_mismatch"


# Codes that only say something failed, so their guidance cannot be specific.
GENERIC_CODES = frozenset({ErrorCode.OPERATION_FAILED, ErrorCode.REMOTE_ERROR})


class CodedError(Exception):
    """An error that names its cause with an ErrorCode.

    A subclass may set `code` for every instance; None means the error names no
    cause, and whoever reports it chooses the generic code that fits.
    """

    code: ErrorCode | None = None

    def __init__(self, message: str, *, code: ErrorCode | str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            # A code passed on from another error arrives as its string.
            self.code = ErrorCode(code)
