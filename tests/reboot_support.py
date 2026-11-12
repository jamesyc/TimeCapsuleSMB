"""A simulated AirPort for tests of services.reboot.reboot_device.

The device has a kernel uptime (ACP property syUT), an ACP port that answers
from a few seconds after boot until shutdown, and an SSH port. One clock drives
the host's time.monotonic/time.sleep and the device, so a test describes when
things happen and the real reboot loop observes them.
"""
from __future__ import annotations

import contextlib
import ipaddress
import struct
from dataclasses import dataclass, field
from types import SimpleNamespace
from unittest import mock

from timecapsulesmb.integrations.acp import ACPAuthError, ACPConnectionError, ACPError

# The AirPort MAC (waMA) every simulated device reports over network ACP.
DEVICE_AIRPORT_MAC = "02:00:00:00:00:01"
# What a failed reboot wait records about this computer's networks; the real
# fields read the host's interfaces and routes (tests/test_acp_diagnostics.py).
NETWORK_CONTEXT = {
    "acp_target_addresses": [{"role": "target", "family": "ipv4", "scope": "private", "address": "10.0.0.2", "link": "on_link"}],
    "local_networks": [{"interface": "en0", "kind": "lan", "networks": [{"family": "ipv4", "scope": "private", "prefixlen": 24}]}],
}


def acp_reading(matches: bool | None):
    """What device.probe.read_airport_acp returns: the password matches (True),
    ACPd rejected it (False), or ACP did not answer (None)."""
    from timecapsulesmb.device.probe import AirportAcpReading

    return AirportAcpReading(password_matches=matches, airport_mac=DEVICE_AIRPORT_MAC if matches else None)


def acp_password_answer(matches: bool | None) -> mock.Mock:
    """A stand-in for device.probe.read_airport_acp."""
    return mock.Mock(return_value=acp_reading(matches))


@dataclass
class FakeAcpDevice:
    # Device uptime when the test starts.
    uptime: float = 3600.0
    # Whether a reboot request makes the device reboot.
    reboots: bool = True
    # Apple's ACPd takes the request, tears itself down (ACP stops answering
    # after shutdown_after) and never runs shutdown: the old kernel and its
    # sshd keep running (build/native/README.md, "Apple ACPd").
    acpd_hangs: bool = False
    # Whether ACPd's teardown runs once ACP stops answering: its daemons stop
    # and its own acp gets no syUT. False: ACPd still serves the device while
    # this computer's ACP reads go unanswered.
    teardown_runs: bool = True
    # Whether the restart marker can be written on the RAM disk.
    marker_writable: bool = True
    # Host seconds each SSH read of device.acpd takes.
    ssh_read_seconds: float = 1.0
    # Seconds from the request until ACP stops answering, and until the new
    # kernel starts (uptime 0).
    shutdown_after: float = 10.0
    # Seconds from the request that the old kernel's sshd keeps answering in
    # a real restart, while ACPd tears down before shutdown; None: shutdown_after.
    old_ssh_until: float | None = None
    kernel_after: float = 40.0
    # Seconds from the request at which the user unplugs a device whose ACPd
    # hung and plugs it back in: the new kernel starts then, and the old one's
    # sshd answers until then. None: nobody unplugs it.
    unplugged_after: float | None = None
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
    # How a read fails while nothing answers ACP: a timeout, or (by setting
    # it) a refused port, a host that is down, no route.
    down_read_error: str = "timed out"
    # Other read failures from each time (host seconds after the request) on.
    down_read_error_changes: tuple[tuple[float, str], ...] = ()
    # How a connect to port 22 fails while SSH does not answer
    # (transport.local.tcp_connect_error): unanswered, or refused, no route.
    ssh_port_error: str = "timed out"
    # Host seconds during which ACP reads fail although the device is up.
    flaky_reads_at: tuple[tuple[float, float], ...] = ()
    # Device clock speed relative to the host's.
    device_rate: float = 1.0
    # This Mac's own networks (checks.network.local_lan_networks) before the
    # request, then from each time (host seconds after the request) on: a
    # restart can leave it on another network, or with no address at all. ACP
    # and SSH reach the device only from a network holding its address.
    mac_networks_before: tuple[str, ...] = ("10.0.0.0/24",)
    mac_network_changes: tuple[tuple[float, tuple[str, ...]], ...] = ()
    # This Mac's default router on en0 (transport.local.lan_routers): its
    # address, and its hardware address before the request, then from each
    # time (host seconds after the request) on. A router is listed only while
    # this Mac has a network; no address means the first host of that network.
    mac_gateway: str | None = None
    mac_router: str | None = None
    mac_router_changes: tuple[tuple[float, str | None], ...] = ()
    # Host seconds after the request at which each router read ran.
    router_reads: list[float] = field(default_factory=list)
    # The address the new boot answers at, when DHCP gave the device another
    # one; the old address then answers nothing.
    new_address: str | None = None
    now: float = 1000.0
    requested_at: float | None = None
    calls: list[str] = field(default_factory=list)
    reads: int = 0
    # Whether any read returned the new boot's uptime.
    served_new_boot: bool = False
    # Hosts whose shared SSH connections were closed, and whether the reboot
    # request had been sent at the time.
    ssh_master_closes: list[tuple[str, bool]] = field(default_factory=list)
    # The device.acpd calls over SSH ("mark", "marker"), each with whether a
    # reboot had been requested; calls keeps ACP traffic.
    ssh_reads: list[tuple[str, bool]] = field(default_factory=list)
    # When the restart marker was written on the RAM disk; a new kernel's
    # RAM disk starts empty.
    marked_at: float | None = None

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
        if self.requested_at is None:
            return None
        if not self.reboots or self.acpd_hangs:
            return None if self.unplugged_after is None else self.requested_at + self.unplugged_after
        return self.requested_at + self.kernel_after

    def _acp_up(self) -> bool:
        kernel = self._new_kernel_at()
        if kernel is None:
            return not (self.acpd_hangs and self.requested_at is not None
                        and self.now >= self.requested_at + self.shutdown_after)
        if self.now < self.requested_at + self.shutdown_after:
            return True
        return self.now >= kernel + self.acp_up_after_boot

    def device_uptime(self) -> int:
        kernel = self._new_kernel_at()
        if kernel is not None and self.now >= kernel:
            return int((self.now - kernel) * self.device_rate)
        return int((self.now - self._boot_at) * self.device_rate)

    def mac_networks(self) -> tuple[str, ...]:
        current = self.mac_networks_before
        if self.requested_at is not None:
            for at, networks in self.mac_network_changes:
                if self.now >= self.requested_at + at:
                    current = networks
        return current

    def current_router_mac(self) -> str | None:
        current = self.mac_router
        if self.requested_at is not None:
            for at, mac in self.mac_router_changes:
                if self.now >= self.requested_at + at:
                    current = mac
        return current

    def lan_routers(self):
        from timecapsulesmb.transport.local import Router

        if self.requested_at is not None:
            self.router_reads.append(self.now - self.requested_at)
        networks = [ipaddress.ip_network(network) for network in self.mac_networks()]
        networks = [network for network in networks if network.version == 4 and not network.network_address.is_link_local]
        mac = self.current_router_mac()
        if not networks or (self.mac_gateway is None and mac is None):
            return ()
        address = self.mac_gateway or str(next(networks[0].hosts()))
        return (Router("en0", address, mac),)

    def local_lan_networks(self):
        # As checks.network.local_interface_networks, without link-local ones.
        networks = (ipaddress.ip_network(network) for network in self.mac_networks())
        return tuple(SimpleNamespace(interface="en0", network=network)
                     for network in networks if not network.network_address.is_link_local)

    def _mac_reaches(self, host: str) -> bool:
        """Whether this Mac reaches `host`: always for a name, a link-local
        address, or a device that was on none of its networks before the
        request (reached through a router); otherwise only from a network
        holding the address."""
        try:
            address = ipaddress.ip_address(host.partition("%")[0])
        except ValueError:
            return True
        if address.is_link_local:
            return True
        if not any(address in ipaddress.ip_network(network) for network in self.mac_networks_before):
            return True
        return any(address in ipaddress.ip_network(network) for network in self.mac_networks())

    def _answers_at(self, host: str) -> bool:
        if not self._mac_reaches(host):
            return False
        kernel = self._new_kernel_at()
        if self.new_address is None or kernel is None or self.now < kernel:
            return True
        return host == self.new_address

    # Patched callables.
    def get_property_int(self, host: str, password: str, name: str, *, timeout: float = 25.0) -> int:
        assert name == "syUT", name
        self.calls.append("read")
        self.reads += 1
        if self.reads == 1 and self.first_read_error is not None:
            raise self.first_read_error
        self.now += self.read_latency
        if (not self._acp_up() or not self._answers_at(host)
                or any(start <= self.now < end for start, end in self.flaky_reads_at)):
            raise ACPConnectionError(f"Could not connect to ACP on {host}:5009: {self._down_read_error()}")
        kernel = self._new_kernel_at()
        if kernel is not None and self.now >= kernel:
            self.served_new_boot = True
        return self.device_uptime()

    def get_properties(self, host: str, password: str, names: tuple[str, ...], *, timeout: float = 25.0) -> dict[str, bytes]:
        # The reboot path reads the uptime and the AirPort MAC together.
        assert set(names) <= {"syUT", "waMA"}, names
        uptime = self.get_property_int(host, password, "syUT", timeout=timeout)
        return {"syUT": struct.pack(">I", uptime), "waMA": bytes.fromhex(DEVICE_AIRPORT_MAC.replace(":", ""))}

    def locate_airport(self, airport_mac: str, password: str, *, current_host: str, trigger: str, attempts: int = 2, **_kwargs: object):
        from timecapsulesmb.services.locate import LocateResult

        self.calls.append("locate")
        # The reboot wait reads each candidate once and looks again later.
        assert (airport_mac, trigger, attempts) == (DEVICE_AIRPORT_MAC, "reboot_wait", 1), (airport_mac, trigger, attempts)
        if self.new_address is not None and self._answers_at(self.new_address) and self._acp_up():
            return LocateResult("found", host=f"root@{self.new_address}", address=self.new_address)
        return LocateResult("not_found")

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
        if not self._answers_at(host):
            return False
        kernel = self._new_kernel_at()
        if kernel is None:
            # An old kernel told to stop answering SSH (old_ssh_until) closes it.
            if (self.old_ssh_until is not None and self.requested_at is not None
                    and self.now >= self.requested_at + self.old_ssh_until):
                return False
            return self.ssh_open
        if self.now < self.requested_at + self._old_ssh_until():
            return self.ssh_open
        if self.ssh_up_after_boot is None:
            return False
        return self.now >= kernel + self.ssh_up_after_boot

    def _down_read_error(self) -> str:
        current = self.down_read_error
        if self.requested_at is not None:
            for at, error in self.down_read_error_changes:
                if self.now >= self.requested_at + at:
                    current = error
        return current

    def tcp_connect_error(self, host: str, port: int, timeout: float = 2.0) -> str | None:
        """transport.local.tcp_connect_error for port 22, as tcp_open sees it."""
        return None if self.tcp_open(host, port, timeout) else self.ssh_port_error

    def _old_ssh_until(self) -> float:
        if self.old_ssh_until is not None:
            return self.old_ssh_until
        if self.acpd_hangs and self.unplugged_after is not None:
            return self.unplugged_after
        return self.shutdown_after

    def _ssh_answers(self, host: str) -> bool:
        kernel = self._new_kernel_at()
        if not self._answers_at(host):
            return False
        if kernel is None or self.requested_at is None or self.now < self.requested_at + self._old_ssh_until():
            return self.ssh_open
        return self.ssh_up_after_boot is not None and self.now >= kernel + self.ssh_up_after_boot

    def _marker_present(self) -> bool:
        kernel = self._new_kernel_at()
        return self.marked_at is not None and (kernel is None or self.now < kernel)

    def mark_restart(self, connection) -> bool:
        """device.acpd.mark_restart: the marker lands on the running kernel's RAM disk."""
        self.ssh_reads.append(("mark", self.requested_at is not None))
        written = self.marker_writable and self._ssh_answers(connection.host.removeprefix("root@"))
        if written:
            self.marked_at = self.now
        self.now += self.ssh_read_seconds
        return written

    def restart_verdict(self, connection):
        """device.acpd.restart_verdict, while SSH answers: the marker's age on
        the device's clock, and whether ACPd's teardown ran."""
        from timecapsulesmb.device.acpd import RESTART_HANG_SECONDS, RestartVerdict
        from timecapsulesmb.transport.errors import SshError

        self.ssh_reads.append(("marker", self.requested_at is not None))
        if not self._ssh_answers(connection.host.removeprefix("root@")):
            raise SshError("Connection refused")
        self.now += self.ssh_read_seconds
        if not self._marker_present():
            return RestartVerdict("none")
        age = (self.now - self.marked_at) * self.device_rate
        if age < RESTART_HANG_SECONDS:
            state = "restarting"
        else:
            # ACPd tore itself down only for a request it took.
            took = self.reboots or self.acpd_hangs
            torn_down = (took and self.teardown_runs and self.requested_at is not None
                         and self.now >= self.requested_at + self.shutdown_after)
            state = "hung" if torn_down else "not_taken"
        return RestartVerdict(state, age, {"restart_verdict": state, "restart_marker_age_sec": round(age, 1)})

    @contextlib.contextmanager
    def patched(self):
        clock = SimpleNamespace(monotonic=self.monotonic, sleep=self.sleep)
        with (
            mock.patch("timecapsulesmb.services.reboot.time", clock),
            mock.patch("timecapsulesmb.integrations.acp.get_property_int", side_effect=lambda *a, **k: self.get_property_int(*a, **k)),
            mock.patch("timecapsulesmb.integrations.acp.get_properties", side_effect=self.get_properties),
            mock.patch("timecapsulesmb.services.reboot.locate_airport", side_effect=self.locate_airport),
            mock.patch(
                "timecapsulesmb.services.acp_diagnostics.probe_context_fields",
                side_effect=lambda host, record: {**NETWORK_CONTEXT, "context_host": host},
            ),
            mock.patch("timecapsulesmb.integrations.acp.reboot", side_effect=self.reboot),
            mock.patch("timecapsulesmb.services.reboot.tcp_open", side_effect=self.tcp_open),
            mock.patch("timecapsulesmb.services.reboot.tcp_connect_error", side_effect=self.tcp_connect_error),
            mock.patch("timecapsulesmb.services.reboot.local_lan_networks", side_effect=self.local_lan_networks),
            mock.patch("timecapsulesmb.services.reboot.lan_routers", side_effect=self.lan_routers),
            mock.patch("timecapsulesmb.device.acpd.mark_restart", side_effect=self.mark_restart),
            mock.patch("timecapsulesmb.device.acpd.restart_verdict", side_effect=self.restart_verdict),
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
        # Progress events, each with `clock()` when it came (set clock to
        # the device's to time them).
        self.progress: list[tuple[float, str, dict[str, object]]] = []
        self.clock = lambda: 0.0

    def callbacks(self):
        from timecapsulesmb.services.callbacks import OperationCallbacks

        return OperationCallbacks(
            set_stage=self.stages.append,
            log=self.messages.append,
            add_debug_fields=self.debug.update,
            update_fields=self.fields.update,
            record_execution_measurement=lambda kind, **fields: self.measurements.append((kind, fields)),
            report_progress=lambda stage, **fields: self.progress.append((self.clock(), stage, fields)),
        )

    def measurement(self, kind: str) -> dict[str, object]:
        matches = [fields for name, fields in self.measurements if name == kind]
        assert len(matches) == 1, (kind, self.measurements)
        return matches[0]


@dataclass
class FakeInstalledRuntime:
    """The TimeCapsuleSMB install on a NetBSD4 device around an fsck reboot.

    Before the reboot, services.activation.installed_netbsd4_autostart reads
    the install (config and version); after it, run_fsck runs rc.local when
    the firmware does not and waits for the runtime. The device's answers come
    from these fields; calls records what ran.
    """

    installed: bool = True
    # Release tag and version code of the install; defaults to this release.
    release_tag: str | None = None
    version_code: int | None = None
    # Whether the runtime becomes ready once started.
    becomes_ready: bool = True
    # What the started manager finds wrong with the payload: a probe
    # DiskPayloadProblem (missing from every disk, or damaged), or None.
    payload_problem: object = None
    # File sharing comes up, but rsync does not, with no verdict saying why.
    rsync_not_running: bool = False
    # This boot's disk errors (device.disk_errors), which keep Samba off.
    disk_errors: tuple[str, ...] = ()
    # A process listing with a process stuck on the disk, which keeps Samba off.
    disk_stall_listing: str = ""
    # Apple's ACPd quit once the runtime started (device.acpd, "quit").
    acpd_quit: bool = False
    calls: list[str] = field(default_factory=list)
    # The SSH target each start and wait reached the device at.
    hosts: list[str] = field(default_factory=list)

    NOT_READY_DETAIL = "managed smbd parent process is not running"

    def _version(self, connection):
        from timecapsulesmb.core.release import CLI_VERSION_CODE, RELEASE_TAG
        from timecapsulesmb.device.probe import DeployedVersionProbeResult

        self.calls.append("read version")
        return DeployedVersionProbeResult(
            self.release_tag or RELEASE_TAG,
            self.version_code or CLI_VERSION_CODE,
            "ok",
        )

    def _run_actions(self, connection, actions, **_kwargs):
        self.hosts.append(connection.host)
        self.calls.append("run " + ", ".join(getattr(action, "path", repr(action)) for action in actions))

    def _verify(self, connection, *, callbacks, stage, timeout_seconds, heading, failure_message):
        from timecapsulesmb.device.errors import DeviceError, RsyncNotRunningError

        callbacks.stage(stage)
        self.hosts.append(connection.host)
        self.calls.append(f"verify {timeout_seconds}s")
        if self.disk_errors:
            from timecapsulesmb.device.disk_errors import DiskFailingError

            raise DiskFailingError(self.disk_errors)
        if self.disk_stall_listing:
            from timecapsulesmb.device.disk_errors import DiskNotRespondingError
            from timecapsulesmb.device.processes import disk_io_stalls

            raise DiskNotRespondingError(disk_io_stalls(self.disk_stall_listing))
        if self.acpd_quit:
            from timecapsulesmb.device.acpd import ACPD_QUIT_MESSAGE
            from timecapsulesmb.device.errors import AcpdQuitError

            raise AcpdQuitError(f"{failure_message} {ACPD_QUIT_MESSAGE}")
        if self.payload_problem is not None:
            raise self.payload_problem.error_type(self.payload_problem.error_after(failure_message))
        if self.rsync_not_running:
            raise RsyncNotRunningError("File sharing and Time Machine are running, but rsync is not: "
                                       "managed rsync process is not running.")
        if not self.becomes_ready:
            raise DeviceError(f"{failure_message} {self.NOT_READY_DETAIL}")

    @contextlib.contextmanager
    def patched(self):
        def config_present(connection):
            self.calls.append("read config")
            return self.installed

        with (
            mock.patch("timecapsulesmb.services.activation.flash_runtime_config_present_conn", side_effect=config_present),
            mock.patch("timecapsulesmb.services.activation.read_deployed_version_conn", side_effect=self._version),
            mock.patch("timecapsulesmb.services.activation.run_remote_actions", side_effect=self._run_actions),
            mock.patch("timecapsulesmb.services.activation.verify_managed_runtime_ready", side_effect=self._verify),
        ):
            yield self
