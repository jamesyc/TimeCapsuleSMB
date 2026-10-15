from __future__ import annotations


class DeviceError(Exception):
    """Base class for recoverable device probing/runtime failures."""


class DiskPayloadMissingError(DeviceError):
    """The manager inspected every disk and none holds a payload, as after an
    Erase in AirPort Utility: only a reinstall starts Samba again."""

    code = "payload_missing_from_disk"
