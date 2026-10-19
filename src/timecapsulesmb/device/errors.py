from __future__ import annotations

from timecapsulesmb.core.error_codes import CodedError, ErrorCode


class DeviceError(CodedError):
    """Base class for recoverable device probing/runtime failures."""


class DiskPayloadError(DeviceError):
    """The manager cannot run the payload on the disk as installed: only a
    reinstall fixes it (device.probe.DISK_PAYLOAD_PROBLEMS). samba_off: file
    sharing is off for it; otherwise Samba serves and only rsync is off."""

    samba_off = True


class DiskPayloadMissingError(DiskPayloadError):
    """The manager inspected every disk and none holds a payload, as after an
    Erase in AirPort Utility."""

    code = ErrorCode.PAYLOAD_MISSING_FROM_DISK


class DiskPayloadDamagedError(DiskPayloadError):
    """A payload binary on the disk does not match the checksum deploy
    recorded, so the disk returned other bytes than were installed."""

    code = ErrorCode.PAYLOAD_DAMAGED_ON_DISK


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


class RsyncNotRunningError(DeviceError):
    """File sharing serves, but rsync, which is optional, did not start, with
    no verdict from the manager saying why (as rsync missing from the payload)."""

    code = ErrorCode.RSYNC_NOT_RUNNING
    samba_off = False
