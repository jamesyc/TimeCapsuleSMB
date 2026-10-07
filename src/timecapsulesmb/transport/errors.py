from __future__ import annotations

import sys


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


class SshLocalNetworkFilteredError(SshNetworkError):
    """Raised when this computer killed SSH's connection before it reached the device.

    ssh prints "connect to host ... Bad file descriptor" when its connecting
    socket's error is EBADF. macOS sets that only on a socket it marks defunct:
    a network content filter's drop verdict, or a VPN tunnel or drop policy
    applied mid-connect (XNU sodefunct()). Local Network privacy fails with
    "No route to host" instead. Seen in telemetry with a VPN on the Mac,
    while the app's own Python connection to port 22 succeeded.
    """


def local_network_filtered_message(*, platform: str = sys.platform) -> str:
    computer = "This Mac" if platform == "darwin" else "This computer"
    return (
        f"{computer} dropped the connection before it reached the device. "
        "A VPN, firewall or security app is filtering local network traffic."
    )


LOCAL_NETWORK_FILTERED_MESSAGE = local_network_filtered_message(platform="darwin")


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
