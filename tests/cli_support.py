"""Shared fixtures for the per-command CLI tests (tests/test_cli_*.py)."""
from __future__ import annotations

import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

from tests.reboot_support import FakeAcpDevice
from timecapsulesmb.device.migration_jobs import MigrationActivity
from timecapsulesmb.cli.context import CommandContext
from timecapsulesmb.services.callbacks import OperationCallbacks
from timecapsulesmb.core.config import AppConfig, DEFAULTS
from timecapsulesmb.device.compat import DeviceCompatibility, compatibility_from_probe_result
from timecapsulesmb.core.release import CLI_VERSION_CODE, RELEASE_TAG
from timecapsulesmb.deploy.executor import XattrMigrationResult
from timecapsulesmb.device.probe import (
    DeployedVersionProbeResult,
    ManagedRuntimeProbeResult,
    ProbeResult,
    ProbeStepResult,
    ProbedDeviceState,
    ReadinessProbeResult,
    SshAccessStatus,
)
from timecapsulesmb.device.storage import MaStVolume
from timecapsulesmb.transport.ssh import SshConnection
from timecapsulesmb.services.version_check import VersionCheckResult


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))


def valid_env(**overrides: str) -> dict[str, str]:
    """A complete, valid .env for a NetBSD 6 Time Capsule, with overrides."""
    values = dict(DEFAULTS)
    values.update({
        "TC_HOST": "root@10.0.0.2",
        "TC_PASSWORD": "pw",
        "TC_SSH_OPTS": "-o foo",
        "TC_AIRPORT_SYAP": "119",
        "TC_MDNS_DEVICE_MODEL": "TimeCapsule8,119",
    })
    values.update(overrides)
    return values


def app_config(values: dict[str, str] | None = None, *, exists: bool = True, path: Path | None = None) -> AppConfig:
    """An AppConfig holding these values, as if read from path (default: the repo's .env)."""
    config_values = dict(values or {})
    return AppConfig.from_values(
        config_values,
        path=path or REPO_ROOT / ".env",
        exists=exists,
        file_values=config_values if exists else {},
    )


def readiness_result(ready: bool, detail: str, lines: tuple[str, ...]) -> ReadinessProbeResult:
    steps = []
    for index, line in enumerate(lines):
        if line.startswith("PASS:"):
            steps.append(ProbeStepResult(f"test_{index}", "pass", line.removeprefix("PASS:")))
        elif line.startswith("FAIL:"):
            steps.append(ProbeStepResult(f"test_{index}", "fail", line.removeprefix("FAIL:")))
        else:
            steps.append(ProbeStepResult(f"test_{index}", "fail", line))
    return ReadinessProbeResult(ready=ready, detail=detail, steps=tuple(steps))


class FakeCommandContext:
    def __init__(
        self,
        *,
        connection: SshConnection | None = None,
        compatibility: DeviceCompatibility | None = None,
    ) -> None:
        self.result = "failure"
        self.finish_fields: dict[str, object] = {}
        self.error_lines: list[str] = []
        self.stages: list[str] = []
        self.debug_fields: dict[str, object] = {}
        self.error: str | None = None
        self.finish = mock.Mock()
        self.connection = connection or SshConnection("root@10.0.0.2", "pw", "-o foo")
        self.probe_state = None
        self.compatibility = compatibility or DeviceCompatibility(
            os_name="NetBSD",
            os_release="6.0",
            arch="earmv4",
            elf_endianness="little",
            payload_family="netbsd6_samba4",
            device_generation="gen5",
            supported=True,
            reason_code="supported_netbsd6",
        )

    def __enter__(self) -> "FakeCommandContext":
        return self

    def __exit__(self, exc_type, _exc, _tb) -> bool:
        if exc_type is KeyboardInterrupt and self.result != "cancelled":
            self.result = "cancelled"
            if not self.error_lines:
                self.set_error("Cancelled by user")
        self.finish(result=self.result, error=None if self.result == "success" else "\n".join(self.error_lines) if self.error_lines else None, **self.finish_fields)
        return False

    def succeed(self) -> None:
        self.result = "success"

    def cancel(self) -> None:
        self.result = "cancelled"

    def cancel_with_error(self, message: str = "Cancelled by user") -> None:
        self.result = "cancelled"
        self.set_error(message)

    def fail(self) -> None:
        self.result = "failure"

    def fail_with_error(self, message: str) -> None:
        self.result = "failure"
        self.set_error(message)

    def update_fields(self, **fields: object) -> None:
        for key, value in fields.items():
            if value is not None:
                self.finish_fields[key] = value

    def start_optional_airport_identity_probe(self, _connection=None) -> None:
        pass

    def harvest_optional_airport_identity_probe(self, *, timeout_seconds: float = 0.0) -> None:
        pass

    def optional_airport_display_name(self, *, timeout_seconds: float = 0.0) -> str:
        model = self.finish_fields.get("device_model")
        syap = self.finish_fields.get("device_syap")
        from timecapsulesmb.core.config import airport_exact_display_name_from_identity

        return airport_exact_display_name_from_identity(
            model=model if isinstance(model, str) else None,
            syap=syap if isinstance(syap, str) else None,
        )

    def require_local_sshpass(self) -> bool:
        return CommandContext.require_local_sshpass(self)  # type: ignore[arg-type]

    def set_stage(self, stage: str) -> None:
        self.stages.append(stage)

    def add_debug_fields(self, **fields: object) -> None:
        for key, value in fields.items():
            if value is not None:
                self.debug_fields[key] = value

    def to_operation_callbacks(self) -> OperationCallbacks:
        return OperationCallbacks(
            set_stage=self.set_stage,
            log=print,
            add_debug_fields=self.add_debug_fields,
            update_fields=self.update_fields,
        )

    def set_error(self, message: str) -> None:
        self.error = message
        self.error_lines = [line.rstrip() for line in message.splitlines() if line.strip()]

    def resolve_env_connection(self, **_kwargs):
        return self.connection

    def resolve_validated_managed_target(self, **_kwargs):
        return mock.Mock(connection=self.connection, probe_state=None)

    def require_compatibility(self):
        return self.compatibility


class CliTestCase(unittest.TestCase):
    """Shared CLI fixtures: setUp patches telemetry and device probes for every command."""

    def _mast_volume(
        self,
        partition_device: str = "dk2",
        *,
        disk_device: str = "wd0",
        name: str = "Data",
        builtin: bool = True,
    ) -> MaStVolume:
        return MaStVolume(
            disk_device,
            partition_device,
            f"/Volumes/{partition_device}",
            name,
            "12345678-1234-1234-1234-123456789012",
            builtin,
            "hfs",
        )

    def managed_runtime_probe(self, ready: bool) -> ManagedRuntimeProbeResult:
        status = "PASS" if ready else "FAIL"
        detail = "managed runtime is ready" if ready else "managed runtime is not ready"
        smbd = readiness_result(ready, detail, (f"{status}:managed smbd ready",))
        mdns = readiness_result(ready, detail, (f"{status}:managed mDNS registrant active",))
        return ManagedRuntimeProbeResult(
            ready=ready,
            detail=detail,
            smbd=smbd,
            mdns=mdns,
        )

    def setUp(self) -> None:
        self._exit_stack = ExitStack()
        self._telemetry_client = mock.Mock()
        # Every address of a selected record answers ACP unless a test says
        # otherwise, so configure keeps the record's preferred address.
        self._record_acp_probe = self._exit_stack.enter_context(
            mock.patch("timecapsulesmb.services.configure_target.tcp_connect_error", return_value=None)
        )
        self._flash_capacity = self._exit_stack.enter_context(
            mock.patch("timecapsulesmb.services.deploy._probe_flash_capacity", return_value=(1024 * 1024, 128 * 1024))
        )
        self._exit_stack.enter_context(
            mock.patch("timecapsulesmb.services.deploy._flash_files_holding_new_bytes", return_value=set())
        )
        for target in (
            "timecapsulesmb.cli.configure.TelemetryClient.from_config",
            "timecapsulesmb.cli.deploy.TelemetryClient.from_config",
            "timecapsulesmb.cli.activate.TelemetryClient.from_config",
            "timecapsulesmb.cli.bootstrap.TelemetryClient.from_config",
            "timecapsulesmb.cli.discover.TelemetryClient.from_config",
            "timecapsulesmb.cli.doctor.TelemetryClient.from_config",
            "timecapsulesmb.cli.flash.TelemetryClient.from_config",
            "timecapsulesmb.cli.fsck.TelemetryClient.from_config",
            "timecapsulesmb.cli.paths.TelemetryClient.from_config",
            "timecapsulesmb.cli.set_ssh.TelemetryClient.from_config",
            "timecapsulesmb.cli.uninstall.TelemetryClient.from_config",
            "timecapsulesmb.cli.validate_install.TelemetryClient.from_config",
        ):
            self._exit_stack.enter_context(mock.patch(target, return_value=self._telemetry_client))
        self._exit_stack.enter_context(mock.patch("timecapsulesmb.device.probe.tcp_open", return_value=False))
        # activate first checks the device holds an install of this version;
        # tests that model a missing or other install set these return values.
        self._installed_config_present = self._exit_stack.enter_context(
            mock.patch("timecapsulesmb.services.activation.flash_runtime_config_present_conn", return_value=True)
        )
        self._installed_version = self._exit_stack.enter_context(
            mock.patch(
                "timecapsulesmb.services.activation.read_deployed_version_conn",
                return_value=DeployedVersionProbeResult(RELEASE_TAG, CLI_VERSION_CODE, "ok"),
            )
        )
        self._exit_stack.enter_context(mock.patch("timecapsulesmb.cli.configure.missing_required_python_module", return_value=None))
        def fake_configure_acp_probe(_connection, *, callbacks=None, **_kwargs):
            callbacks.add_debug_fields(
                configure_acp_enable_attempted=True,
                configure_acp_enable_succeeded=True,
                ssh_initially_reachable=False,
            )
            callbacks.update_fields(ssh_final_reachable=True)
            return self.make_probe_state(self.make_probe_result_netbsd6())

        self._configure_acp_probe_mock = self._exit_stack.enter_context(
            mock.patch(
                "timecapsulesmb.services.configure.enable_ssh_and_reprobe",
                side_effect=fake_configure_acp_probe,
            )
        )
        # No test may reach a real device's ACP. Every reboot goes to this
        # simulated device, which reboots normally unless a test reconfigures it.
        self.device = self._exit_stack.enter_context(FakeAcpDevice().patched())
        self._version_check = self._exit_stack.enter_context(
            mock.patch(
                "timecapsulesmb.cli.main.check_client_version",
                return_value=VersionCheckResult(should_block=False),
            )
        )
        self._exit_stack.enter_context(
            mock.patch(
                "timecapsulesmb.services.deploy.migrate_xattr_tdb_to_hfs",
                return_value=XattrMigrationResult("migration=complete", ()),
            )
        )

        from tests.test_xattr_migration import fake_inventory
        self._exit_stack.enter_context(mock.patch("timecapsulesmb.services.deploy.inventory_metadata", side_effect=lambda *_a: fake_inventory()))
        self._exit_stack.enter_context(mock.patch("timecapsulesmb.services.deploy.probe_migration_activity", return_value=MigrationActivity(())))
        self._exit_stack.enter_context(mock.patch("timecapsulesmb.services.deploy.inspect_sources"))

    def tearDown(self) -> None:
        self._exit_stack.close()

    def make_supported_compatibility(self) -> DeviceCompatibility:
        return DeviceCompatibility(
            os_name="NetBSD",
            os_release="6.0",
            arch="earmv4",
            elf_endianness="little",
            payload_family="netbsd6_samba4",
            device_generation="gen5",
            supported=True,
            reason_code="supported_netbsd6",
        )

    def make_supported_netbsd4_compatibility(self) -> DeviceCompatibility:
        return DeviceCompatibility(
            os_name="NetBSD",
            os_release="4.0",
            arch="earmv4",
            elf_endianness="little",
            payload_family="netbsd4le_samba4",
            device_generation="gen1-4",
            supported=True,
            reason_code="supported_netbsd4",
        )

    def make_valid_env(self, **overrides: str) -> dict[str, str]:
        return valid_env(**overrides)

    def make_app_config(self, values: dict[str, str] | None = None, *, exists: bool = True, path: Path | None = None) -> AppConfig:
        return app_config(values, exists=exists, path=path)

    def make_probe_result_netbsd6(self) -> ProbeResult:
        return ProbeResult(
            ssh_status=SshAccessStatus.OPEN_AUTHENTICATED,
            error=None,
            os_name="NetBSD",
            os_release="6.0",
            arch="earmv4",
            elf_endianness="little",
            airport_model="TimeCapsule8,119",
            airport_syap="119",
        )

    def make_logged_in_probe_state(self, compatibility: DeviceCompatibility) -> ProbedDeviceState:
        """What the managed-target probe returns for a device SSH logged in to."""
        probe_result = ProbeResult(
            ssh_status=SshAccessStatus.OPEN_AUTHENTICATED,
            error=None,
            os_name=compatibility.os_name,
            os_release=compatibility.os_release,
            arch=compatibility.arch,
            elf_endianness=compatibility.elf_endianness,
        )
        return ProbedDeviceState(probe_result=probe_result, compatibility=compatibility)

    def make_probe_state(self, probe_result: ProbeResult) -> ProbedDeviceState:
        compatibility = compatibility_from_probe_result(probe_result) if probe_result.ssh_authenticated else None
        return ProbedDeviceState(probe_result=probe_result, compatibility=compatibility)

    def telemetry_payload(self, event: str) -> dict[str, object]:
        for call in reversed(self._telemetry_client.emit.call_args_list):
            if call.args and call.args[0] == event:
                return call.kwargs
        self.fail(f"{event} telemetry was not emitted")
