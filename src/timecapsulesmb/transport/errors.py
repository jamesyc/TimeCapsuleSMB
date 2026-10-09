from __future__ import annotations

import signal
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


# The signals a program raises on itself when it crashes.
CRASH_SIGNALS = frozenset({signal.SIGSEGV, signal.SIGBUS, signal.SIGILL, signal.SIGABRT, signal.SIGFPE, signal.SIGTRAP})


class SshClientCrashedError(SshLocalNetworkFilteredError):
    """Raised when the local ssh program died from a signal.

    The one seen cause is a Network Extension on the Mac: our forked child
    crashed in Apple's fork handler before it could exec ssh (issue #371).
    Spawns no longer fork, but ssh itself forks and uses the same frameworks.
    It is reported like a filtered connection, whose guidance names the same
    apps.
    """

    def __init__(self, signal_number: int, *, platform: str = sys.platform) -> None:
        try:
            name = signal.Signals(signal_number).name
        except ValueError:
            name = f"signal {signal_number}"
        computer = "this Mac" if platform == "darwin" else "this computer"
        if signal_number in CRASH_SIGNALS:
            message = (
                f"The ssh program on {computer} crashed ({name}) before it finished. "
                "A VPN, firewall or security app can cause this; try again, or turn that app off and retry."
            )
        else:
            # Something outside this process ended it: kill, a shutdown, memory pressure.
            message = f"The ssh program on {computer} was stopped by {name} before it finished."
        super().__init__(message)
        self.signal_number = signal_number


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
