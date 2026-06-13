from __future__ import annotations


class TransportError(Exception):
    """Base class for recoverable transport-layer failures."""


class SshError(TransportError):
    """Raised when an SSH command or tunnel operation fails."""


class SshAuthenticationError(SshError):
    """Raised when SSH reaches the device but credentials are rejected."""


class SshAlgorithmNegotiationError(SshError):
    """Raised when SSH reaches the device but cannot agree on a legacy algorithm."""

    def __init__(self, message: str, *, algorithm: str, offered: tuple[str, ...]) -> None:
        super().__init__(message)
        self.algorithm = algorithm
        self.offered = offered


class SshClientConfigError(SshError):
    """Raised when the local SSH client rejects our options or user config."""


class SshNetworkError(SshError):
    """Raised when the SSH client reports a network-level failure."""


class ScpError(TransportError):
    """Raised when an SCP or upload operation fails."""


class SshCommandTimeout(SshError):
    """Raised when the local SSH client times out waiting for command completion."""


SSH_TIMEOUT_SLOW_DEVICE_FALLBACK_DEVICE_NAME = "device"


def ssh_timeout_slow_device_message(device_name: str | None = None) -> str:
    name = (device_name or "").strip() or SSH_TIMEOUT_SLOW_DEVICE_FALLBACK_DEVICE_NAME
    return f"The {name} is responding very slowly. Please reboot the device. Then wait for SSH to come back and retry."


SSH_TIMEOUT_SLOW_DEVICE_MESSAGE = ssh_timeout_slow_device_message()


def is_ssh_timeout_error(exc: BaseException | None) -> bool:
    seen: set[int] = set()
    current = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, SshCommandTimeout):
            return True
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return False


def format_ssh_timeout_slow_device_error(exc: BaseException, *, device_name: str | None = None) -> str:
    message = ssh_timeout_slow_device_message(device_name)
    detail = str(exc).strip()
    if not detail:
        return message
    return f"{message}\n{detail}"
