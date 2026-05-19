from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from timecapsulesmb.checks.bonjour import BonjourServiceTarget
from timecapsulesmb.checks.models import CheckResult, is_fatal
from timecapsulesmb.core.config import AppConfig
from timecapsulesmb.device.probe import ProbedDeviceState, RemoteInterfaceProbeResult, RuntimeNamingIdentityProbeResult
from timecapsulesmb.transport.ssh import SshConnection


@dataclass(frozen=True)
class DoctorBonjourResult:
    instance: str | None
    target: BonjourServiceTarget | None
    service_targets: dict[str, tuple[str, ...]]
    reason: str
    debug_needed: bool
    expected_debug: dict[str, str | None] | None
    zeroconf_debug: object | None


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
    precomputed_interface_probe: RemoteInterfaceProbeResult | None
    precomputed_probe_state: ProbedDeviceState | None
    options: DoctorOptions


@dataclass
class DoctorSink:
    on_result: Callable[[CheckResult], None] | None
    debug_fields: dict[str, object] | None
    results: list[CheckResult] = field(default_factory=list)

    def add(self, result: CheckResult) -> None:
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
    proxied_ssh: bool


@dataclass(frozen=True)
class RemoteAccess:
    ssh_checked: bool
    ssh_ok: bool
    remote_checks_enabled: bool
    active_smb_conf_reason: str


@dataclass(frozen=True)
class SmbConfigState:
    text: str | None
    reason: str


@dataclass(frozen=True)
class RuntimeNamingState:
    identity: RuntimeNamingIdentityProbeResult | None


@dataclass(frozen=True)
class StepDecision:
    stop: bool = False
