"""A simulated AirPort for tests of services.reboot.reboot_device.

The device has a kernel uptime (ACP property syUT), an ACP port that answers
from a few seconds after boot until shutdown, and an SSH port. One clock drives
the host's time.monotonic/time.sleep and the device, so a test describes when
things happen and the real reboot loop observes them.
"""
from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from types import SimpleNamespace
from unittest import mock

from timecapsulesmb.integrations.acp import ACPAuthError, ACPConnectionError, ACPError


@dataclass
class FakeAcpDevice:
    # Device uptime when the test starts.
    uptime: float = 3600.0
    # Whether a reboot request makes the device reboot.
    reboots: bool = True
    # Seconds from the request until ACP stops answering, and until the new
    # kernel starts (uptime 0).
    shutdown_after: float = 10.0
    kernel_after: float = 40.0
    # Seconds after the new kernel starts that ACP and SSH answer (measured on
    # the devices: ACPd at 4-5 s, sshd at 7-10 s). None: SSH never opens.
    acp_up_after_boot: float = 5.0
    ssh_up_after_boot: float | None = 10.0
    # SSH state before any reboot.
    ssh_open: bool = True
    # The reboot request's own failure, raised after the device acted on it.
    request_error: ACPError | None = None
    # Failure for the reading taken before the request (u0).
    first_read_error: ACPError | None = None
    # Host clock seconds that elapse during each ACP read.
    read_latency: float = 0.0
    # Host seconds during which ACP reads fail although the device is up.
    flaky_reads_at: tuple[tuple[float, float], ...] = ()
    # Device clock speed relative to the host's.
    device_rate: float = 1.0
    now: float = 1000.0
    requested_at: float | None = None
    calls: list[str] = field(default_factory=list)
    reads: int = 0
    # Whether any read returned the new boot's uptime.
    served_new_boot: bool = False
    # Hosts whose shared SSH connections were closed, and whether the reboot
    # request had been sent at the time.
    ssh_master_closes: list[tuple[str, bool]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._boot_at = self.now - self.uptime / self.device_rate

    # The host side.
    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.calls.append(f"sleep {seconds:g}")
        self.now += max(0.0, seconds)

    def pause_host_clock(self, device_seconds: float) -> None:
        """The Mac sleeps: the device runs on while time.monotonic stands still."""
        self._boot_at -= device_seconds / self.device_rate
        if self.requested_at is not None:
            self.requested_at -= device_seconds

    # Device state.
    def _new_kernel_at(self) -> float | None:
        if self.requested_at is None or not self.reboots:
            return None
        return self.requested_at + self.kernel_after

    def _acp_up(self) -> bool:
        kernel = self._new_kernel_at()
        if kernel is None:
            return True
        if self.now < self.requested_at + self.shutdown_after:
            return True
        return self.now >= kernel + self.acp_up_after_boot

    def device_uptime(self) -> int:
        kernel = self._new_kernel_at()
        if kernel is not None and self.now >= kernel:
            return int((self.now - kernel) * self.device_rate)
        return int((self.now - self._boot_at) * self.device_rate)

    # Patched callables.
    def get_property_int(self, host: str, password: str, name: str, *, timeout: float = 25.0) -> int:
        assert name == "syUT", name
        self.calls.append("read")
        self.reads += 1
        if self.reads == 1 and self.first_read_error is not None:
            raise self.first_read_error
        self.now += self.read_latency
        if not self._acp_up() or any(start <= self.now < end for start, end in self.flaky_reads_at):
            raise ACPConnectionError(f"Could not connect to ACP on {host}:5009: timed out")
        kernel = self._new_kernel_at()
        if kernel is not None and self.now >= kernel:
            self.served_new_boot = True
        return self.device_uptime()

    def reboot(self, host: str, password: str, *, timeout: float = 25.0, **_kwargs: object) -> None:
        self.calls.append("request")
        if isinstance(self.request_error, ACPAuthError):
            raise self.request_error
        self.requested_at = self.now
        if self.request_error is not None:
            raise self.request_error

    def tcp_open(self, host: str, port: int, timeout: float = 2.0) -> bool:
        assert port == 22, port
        self.calls.append("tcp 22")
        kernel = self._new_kernel_at()
        if kernel is None:
            return self.ssh_open
        if self.now < self.requested_at + self.shutdown_after:
            return self.ssh_open
        if self.ssh_up_after_boot is None:
            return False
        return self.now >= kernel + self.ssh_up_after_boot

    @contextlib.contextmanager
    def patched(self):
        clock = SimpleNamespace(monotonic=self.monotonic, sleep=self.sleep)
        with (
            mock.patch("timecapsulesmb.services.reboot.time", clock),
            mock.patch("timecapsulesmb.integrations.acp.get_property_int", side_effect=self.get_property_int),
            mock.patch("timecapsulesmb.integrations.acp.reboot", side_effect=self.reboot),
            mock.patch("timecapsulesmb.services.reboot.tcp_open", side_effect=self.tcp_open),
            mock.patch(
                "timecapsulesmb.services.reboot.close_ssh_masters",
                side_effect=lambda host: self.ssh_master_closes.append((host, self.requested_at is not None)),
            ),
        ):
            yield self


class RecordingCallbacks:
    """OperationCallbacks hooks that keep what the reboot path reported."""

    def __init__(self) -> None:
        self.stages: list[str] = []
        self.messages: list[str] = []
        self.debug: dict[str, object] = {}
        self.fields: dict[str, object] = {}
        self.measurements: list[tuple[str, dict[str, object]]] = []

    def callbacks(self):
        from timecapsulesmb.services.callbacks import OperationCallbacks

        return OperationCallbacks(
            set_stage=self.stages.append,
            log=self.messages.append,
            add_debug_fields=self.debug.update,
            update_fields=self.fields.update,
            record_execution_measurement=lambda kind, **fields: self.measurements.append((kind, fields)),
        )

    def measurement(self, kind: str) -> dict[str, object]:
        matches = [fields for name, fields in self.measurements if name == kind]
        assert len(matches) == 1, (kind, self.measurements)
        return matches[0]
