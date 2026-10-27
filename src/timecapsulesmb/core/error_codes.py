"""The codes the helper's errors carry.

The app chooses its guidance for a failure by its code (app/recovery.py has an
entry for each), and telemetry counts failures by code instead of by the wording
of their messages. A code travels as its string value; every raise site names a
member, so a misspelled code fails where it is raised instead of reaching the
app as one it has no guidance for.
"""
from __future__ import annotations

from collections.abc import Iterator
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
    ACPD_QUIT = "acpd_quit"
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
    PAYLOAD_UNREADABLE_ON_DISK = "payload_unreadable_on_disk"
    RSYNC_MISSING_FROM_DISK = "rsync_missing_from_disk"
    RSYNC_DAMAGED_ON_DISK = "rsync_damaged_on_disk"
    RSYNC_UNREADABLE_ON_DISK = "rsync_unreadable_on_disk"
    RSYNC_NOT_RUNNING = "rsync_not_running"

    # The disk.
    DISK_FAILING = "disk_failing"
    DISK_WRITE_TEST_UNRESPONSIVE = "disk_write_test_unresponsive"
    FSCK_ALREADY_RUNNING = "fsck_already_running"
    FSCK_SESSION_DROPPED = "fsck_session_dropped"
    FSCK_VOLUME_NOT_UNMOUNTED = "fsck_volume_not_unmounted"
    DISK_SIZE_MISREPORTED = "deploy_disk_size_misreported"
    WIPE_DISK_RESTART_NEEDED = "wipe_disk_restart_needed"
    WIPE_DISK_ERASE_RUNNING = "wipe_disk_erase_running"
    WIPE_DISK_JOB_RUNNING = "wipe_disk_job_running"
    WIPE_DISK_OTHER_DISK = "wipe_disk_other_disk"
    WIPE_DISK_BUSY = "wipe_disk_busy"
    WIPE_DISK_CHANGED = "wipe_disk_changed"
    WIPE_DISK_ERASE_FAILED = "wipe_disk_erase_failed"

    # Installing.
    DEPLOY_NO_DISK_DETECTED = "deploy_no_disk_detected"
    DEPLOY_NO_USB_DISK_DETECTED = "deploy_no_usb_disk_detected"
    DEPLOY_NO_HFS_PARTITION = "deploy_no_hfs_partition"
    # A volume exists only until the next restart (AirPort Utility's erase
    # formatted it on a partition table Apple's gpt rejected), and the command
    # would restart the device.
    DISK_VOLUMES_IN_MEMORY = "disk_volumes_in_memory"
    USB_DISK_VOLUMES_IN_MEMORY = "usb_disk_volumes_in_memory"
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
    FLASH_PATCH_REFUSED = "flash_patch_refused"


# Codes that only say something failed, so their guidance cannot be specific.
GENERIC_CODES = frozenset({ErrorCode.OPERATION_FAILED, ErrorCode.REMOTE_ERROR})


class CodedError(Exception):
    """An error that names its cause with an ErrorCode.

    `message` says what happened and why, and never what to do: the app and the
    CLI each take the next steps for the code from app/recovery.py, in their own
    words. `detail` holds the facts of this failure (an address, a file, an exit
    status), or None. The error reads as both, each on its line, wherever its
    text is shown whole: the CLI, the app's English details, telemetry.
    A wrapper that names its step before the cause ("Failed to enable SSH via
    ACP: <cause>") keeps that form: the telemetry dashboard sorts failures by
    such prefixes, whitespace collapsed.

    A subclass may set `code` for every instance; None means the error names no
    cause, and whoever reports it chooses the generic code that fits.
    """

    code: ErrorCode | None = None

    def __init__(
        self,
        message: str,
        *,
        code: ErrorCode | str | None = None,
        detail: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail
        if code is not None:
            # A code passed on from another error arrives as its string.
            self.code = ErrorCode(code)

    def __str__(self) -> str:
        return f"{self.message}\n{self.detail}" if self.detail else self.message


def exception_chain(exc: BaseException | None) -> Iterator[BaseException]:
    """The error, then each error it was raised from or while handling, as
    Python's traceback walks them: `raise ... from None` ends the chain."""
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        yield exc
        seen.add(id(exc))
        if exc.__cause__ is not None:
            exc = exc.__cause__
        elif exc.__suppress_context__:
            exc = None
        else:
            exc = exc.__context__


def coded_error_in(exc: BaseException | None) -> CodedError | None:
    """The error itself or the first in its chain of causes that names a code,
    as when a command turned it into a SystemExit."""
    return next(
        (error for error in exception_chain(exc) if isinstance(error, CodedError) and error.code is not None),
        None,
    )
