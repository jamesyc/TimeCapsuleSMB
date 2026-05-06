from __future__ import annotations


class TransportError(Exception):
    """Base class for recoverable transport-layer failures."""


class SshError(TransportError):
    """Raised when an SSH command or tunnel operation fails."""


class ScpError(TransportError):
    """Raised when an SCP or upload operation fails."""


class SshCommandTimeout(SshError):
    """Raised when the local SSH client times out waiting for command completion."""
