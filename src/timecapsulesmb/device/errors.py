from __future__ import annotations

from timecapsulesmb.core.error_codes import CodedError, ErrorCode


class DeviceError(CodedError):
    """Base class for recoverable device probing/runtime failures."""


class DiskPayloadError(DeviceError):
    """The manager cannot run the payload on the disk as installed, however
    long anything waits (device.probe.DISK_PAYLOAD_PROBLEMS): a reinstall
    fixes the missing and damaged ones; the unreadable ones need a new disk.
    samba_off: file sharing is off for it; otherwise Samba serves and only
    rsync is off. replaces_disk: the disk could not read the files, so a new
    disk is the fix."""

    samba_off = True
    replaces_disk = False


class DiskPayloadMissingError(DiskPayloadError):
    """The manager inspected every disk and none holds a payload, as after an
    Erase in AirPort Utility."""

    code = ErrorCode.PAYLOAD_MISSING_FROM_DISK


class DiskPayloadDamagedError(DiskPayloadError):
    """A payload binary on the disk does not match the checksum deploy
    recorded, so the disk returned other bytes than were installed."""

    code = ErrorCode.PAYLOAD_DAMAGED_ON_DISK


class DiskPayloadUnreadableError(DiskPayloadError):
    """The disk could not return smbd or its checksums, or only too slowly
    for the manager's read budget: the disk is probably failing, which a
    reinstall does not fix. Its own cause, apart from the kernel's disk
    errors (device.disk_errors.DiskFailingError), with the same way out."""

    code = ErrorCode.PAYLOAD_UNREADABLE_ON_DISK
    replaces_disk = True


class RsyncDamagedError(DiskPayloadError):
    """rsync on the disk does not match the checksum deploy recorded: the
    manager keeps rsync off while Samba serves without it."""

    code = ErrorCode.RSYNC_DAMAGED_ON_DISK
    samba_off = False


class RsyncMissingError(DiskPayloadError):
    """The payload has no rsync or no rsyncd.conf: the manager keeps rsync off
    while Samba serves without it."""

    code = ErrorCode.RSYNC_MISSING_FROM_DISK
    samba_off = False


class RsyncUnreadableError(DiskPayloadError):
    """The disk could not return rsync, or only too slowly: rsync is off while
    Samba serves without it, and the disk is probably failing."""

    code = ErrorCode.RSYNC_UNREADABLE_ON_DISK
    samba_off = False
    replaces_disk = True


class RsyncNotRunningError(DeviceError):
    """File sharing serves, but rsync, which is optional, did not start, with
    no verdict from the manager saying why (as rsync missing from the payload)."""

    code = ErrorCode.RSYNC_NOT_RUNNING
    samba_off = False
