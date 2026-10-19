from __future__ import annotations


class DeviceError(Exception):
    """Base class for recoverable device probing/runtime failures."""


class DiskPayloadError(DeviceError):
    """The manager cannot run the payload on the disk as installed: only a
    reinstall fixes it (device.probe.DISK_PAYLOAD_PROBLEMS). samba_off: file
    sharing is off for it; otherwise Samba serves and only rsync is off."""

    code = ""
    samba_off = True


class DiskPayloadMissingError(DiskPayloadError):
    """The manager inspected every disk and none holds a payload, as after an
    Erase in AirPort Utility."""

    code = "payload_missing_from_disk"


class DiskPayloadDamagedError(DiskPayloadError):
    """A payload binary on the disk does not match the checksum deploy
    recorded, so the disk returned other bytes than were installed."""

    code = "payload_damaged_on_disk"


class RsyncDamagedError(DiskPayloadError):
    """rsync on the disk does not match the checksum deploy recorded: the
    manager keeps rsync off while Samba serves without it."""

    code = "rsync_damaged_on_disk"
    samba_off = False


class RsyncMissingError(DiskPayloadError):
    """The payload has no rsync or no rsyncd.conf: the manager keeps rsync off
    while Samba serves without it."""

    code = "rsync_missing_from_disk"
    samba_off = False


class RsyncNotRunningError(DeviceError):
    """File sharing serves, but rsync, which is optional, did not start, with
    no verdict from the manager saying why (as rsync missing from the payload)."""

    code = "rsync_not_running"
    samba_off = False
