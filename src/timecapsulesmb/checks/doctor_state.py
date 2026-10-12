from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from timecapsulesmb.checks.bonjour import BonjourServiceTarget
from timecapsulesmb.checks.models import DOMAIN_COMPUTER, DOMAIN_DEVICE, CheckResult, is_fatal
from timecapsulesmb.core.config import AppConfig
from timecapsulesmb.device.probe import AirportAcpReading, ProbedDeviceState, RuntimeNamingIdentityProbeResult
from timecapsulesmb.device.processes import StuckProcess
from timecapsulesmb.transport.ssh import SshConnection


@dataclass(frozen=True)
class DoctorBonjourResult:
    instance: str | None
    target: BonjourServiceTarget | None
    service_targets: dict[str, tuple[str, ...]]
    reason: str
    debug_needed: bool
    expected_debug: dict[str, str | None] | None
    discovery_debug: object | None
    addresses: tuple[str, ...] = ()


@dataclass(frozen=True)
class DoctorOptions:
    skip_ssh: bool
    skip_bonjour: bool
    skip_smb: bool


@dataclass(frozen=True)
class DoctorInputs:
    config: AppConfig
    repo_root: Path
    connection: SshConnection | None
    precomputed_probe_state: ProbedDeviceState | None
    options: DoctorOptions
    # The app's start-of-command read of the device at this connection's
    # address; the password check uses it instead of reading again.
    device_reading: AirportAcpReading | None = None
    # The macOS app's Local Network permission preflight ("allowed", "denied"
    # or "unknown"); None from the CLI, which has no such permission to ask.
    local_network_permission: str | None = None


@dataclass
class DoctorSink:
    on_result: Callable[[CheckResult], None] | None
    debug_fields: dict[str, object] | None
    results: list[CheckResult] = field(default_factory=list)
    section: str = DOMAIN_DEVICE
    # Failure codes of this computer's checks that only repeat a device
    # service the device itself reported down, and that service: such a
    # failure is a SKIP that names the device, not this computer's fault.
    blocked_by_device: dict[str, str] = field(default_factory=dict)

    @contextmanager
    def in_section(self, section: str) -> Iterator[None]:
        previous, self.section = self.section, section
        try:
            yield
        finally:
            self.section = previous

    def add(self, result: CheckResult) -> None:
        if result.domain is None:
            result.domain = self.section
        reason = self.blocked_by_device.get(result.details.get("code")) if (
            result.status == "FAIL" and result.domain == DOMAIN_COMPUTER
        ) else None
        if reason is not None:
            result = CheckResult(
                "SKIP",
                f"{result.message}; not counted against this computer: {reason} (see Device)",
                {**result.details, "blocked_by_device": True},
                domain=result.domain,
            )
        self.results.append(result)
        if self.on_result is not None:
            self.on_result(result)

    def fatal(self) -> bool:
        return any(is_fatal(result) for result in self.results)

    def result_count(self) -> int:
        return len(self.results)

    def new_results_since(self, index: int) -> list[CheckResult]:
        return self.results[index:]


@dataclass(frozen=True)
class DoctorTarget:
    connection: SshConnection
    host: str
    smb_password: str


@dataclass(frozen=True)
class RemoteAccess:
    ssh_checked: bool
    ssh_ok: bool
    remote_checks_enabled: bool
    active_smb_conf_reason: str
    # Neither SSH nor ACP answered at the saved address (network.check_ssh_login).
    device_unreachable: bool = False


@dataclass(frozen=True)
class ProcessSnapshotState:
    stuck: tuple[StuckProcess, ...] = ()
    # ps did not answer: the device may be out of processes, so doctor starts
    # as few new ones as it can.
    timed_out: bool = False
    # The listing itself, when ps answered.
    snapshot: str | None = None

    @property
    def smbd_stuck(self) -> bool:
        return any(process.name == "smbd" for process in self.stuck)


@dataclass(frozen=True)
class SmbConfigState:
    text: str | None
    reason: str


@dataclass(frozen=True)
class RuntimeNamingState:
    identity: RuntimeNamingIdentityProbeResult | None


@dataclass(frozen=True)
class DirectSmbState:
    observed_addresses: tuple[str, ...] = ()
    testable_addresses: tuple[str, ...] = ()
    reachable_addresses: tuple[str, ...] = ()
    # (address, this host's source address) for each route the kernel chose.
    route_sources: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class StepDecision:
    stop: bool = False
