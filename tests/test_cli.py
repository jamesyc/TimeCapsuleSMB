from __future__ import annotations

import errno
import io
import json
import plistlib
import socket
import struct
import sys
import tempfile
import unittest
import uuid
from contextlib import ExitStack, contextmanager
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import zlib


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

import timecapsulesmb.cli.main as cli_main_module
from timecapsulesmb import apple_firmware
from timecapsulesmb.basebinary import (
    BasebinaryHeader,
    BasebinaryKey,
    DEFAULT_BASEBINARY_KEYS,
    compose_basebinary,
    parse_nested_basebinary,
)
from timecapsulesmb.cli import (
    activate,
    bootstrap,
    configure,
    deploy,
    discover,
    doctor,
    flash as cli_flash,
    fsck,
    paths,
    repair_xattrs,
    set_ssh,
    uninstall,
    validate_install,
)
from timecapsulesmb.cli import runtime as cli_runtime
from timecapsulesmb.cli.main import main
from timecapsulesmb.cli.context import CommandContext
from timecapsulesmb.core.config import (
    AppConfig,
    ConfigError,
    DEFAULTS,
    airport_exact_display_name_from_config,
    airport_family_display_name_from_config,
)
from timecapsulesmb.core.paths import AppPaths
from timecapsulesmb.device.compat import DeviceCompatibility, compatibility_from_probe_result
from timecapsulesmb.device.probe import (
    ManagedMdnsTakeoverProbeResult,
    ManagedRuntimeProbeResult,
    ManagedSmbdProbeResult,
    ProbeResult,
    ProbedDeviceState,
    RemoteInterfaceCandidate,
    RemoteInterfaceCandidatesProbeResult,
    RemoteInterfaceProbeResult,
)
from timecapsulesmb.device.storage import (
    MaStDiscoveryResult,
    MaStVolume,
    PayloadCandidateCheck,
    PayloadHome,
    PayloadHomeSelection,
    PayloadVerificationResult,
)
from timecapsulesmb.deploy.commands import (
    RunScriptAction,
    StopProcessAction,
    StopWatchdogAction,
)
from timecapsulesmb.deploy.planner import (
    DEFAULT_APPLE_MOUNT_WAIT_SECONDS,
    GENERATED_FLASH_CONFIG_SOURCE,
    GENERATED_SMBPASSWD_SOURCE,
    GENERATED_USERNAME_MAP_SOURCE,
)
from timecapsulesmb.deploy.verify import VerificationResult
from timecapsulesmb.flash_payloads import find_apple_firmware_match
from timecapsulesmb.flash import PATCHED_LOGIN_SCRIPT, STOCK_LOGIN_NETBSD4_DUMMY, sha256_hex
from timecapsulesmb.transport.ssh import SshCommandTimeout, SshConnection, SshError
from timecapsulesmb.discovery.bonjour import BonjourDiscoverySnapshot, BonjourServiceInstance, Discovered
from timecapsulesmb.cli.version_check import DEFAULT_DOWNLOAD_URL, VERSION_CHECK_URL, VersionCheckResult
from timecapsulesmb.cli.util import ANSI_RED, ANSI_RESET
from timecapsulesmb.integrations.acp import ACPAuthError, ACPConnectionError
from timecapsulesmb.install_validation import InstallCheckResult


def make_test_gzip_member(data: bytes) -> bytes:
    compressor = zlib.compressobj(level=1, wbits=16 + zlib.MAX_WBITS)
    return compressor.compress(data) + compressor.flush()


class FastFakeZopfliGzipForCli:
    @staticmethod
    def compress(data: bytes, **_kwargs) -> bytes:
        return make_test_gzip_member(data)


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
        self.finish = mock.Mock()
        self.connection = connection or SshConnection("root@10.0.0.2", "pw", "-o foo")
        self.interface_probe = None
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

    def set_result(self, result: str) -> None:
        self.result = result

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

    def set_stage(self, stage: str) -> None:
        self.stages.append(stage)

    def add_debug_fields(self, **_fields: object) -> None:
        pass

    def set_error(self, message: str) -> None:
        self.error_lines = [line.rstrip() for line in message.splitlines() if line.strip()]

    def add_error_line(self, message: str) -> None:
        line = message.strip()
        if line:
            self.error_lines.append(line)

    def resolve_env_connection(self, **_kwargs):
        return self.connection

    def inspect_managed_connection(self, **_kwargs):
        self.interface_probe = RemoteInterfaceProbeResult(
            iface="bridge0",
            exists=True,
            detail="interface bridge0 exists",
        )
        return mock.Mock(
            connection=self.connection,
            interface_probe=self.interface_probe,
            probe_state=self.probe_state,
        )

    def resolve_validated_managed_target(self, **_kwargs):
        return mock.Mock(connection=self.connection, probe_state=None)

    def require_compatibility(self):
        return self.compatibility

    def confirm_or_fail(
        self,
        prompt_text: str,
        *,
        default: bool,
        noninteractive_message: str,
        eof_default: bool | None = None,
        interrupt_default: bool | None = None,
    ) -> bool | None:
        try:
            return cli_runtime.confirm(
                prompt_text,
                default=default,
                eof_default=eof_default,
                interrupt_default=interrupt_default,
                noninteractive_message=noninteractive_message,
            )
        except cli_runtime.NonInteractivePromptError as exc:
            message = str(exc)
            print(message)
            self.fail_with_error(message)
            return None


class CliTests(unittest.TestCase):
    def _payload_home(self, volume_root: str = "/Volumes/dk2", payload_dir_name: str = "samba4") -> PayloadHome:
        disk_key = volume_root.rstrip("/").rsplit("/", 1)[-1]
        return PayloadHome(volume_root, f"/dev/{disk_key}", payload_dir_name)

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

    def _patch_mast_volume_flow(
        self,
        stack: ExitStack,
        module: str,
        *,
        mounted_volumes: tuple[MaStVolume, ...] | None = None,
        read_volumes: tuple[MaStVolume, ...] | None = None,
    ) -> SimpleNamespace:
        mounted = mounted_volumes if mounted_volumes is not None else (self._mast_volume("dk2"),)
        read = read_volumes if read_volumes is not None else mounted
        return SimpleNamespace(
            read_mast_volumes_conn=stack.enter_context(mock.patch("timecapsulesmb.cli.context.read_mast_volumes_conn", return_value=read)),
            mounted_mast_volumes_conn=stack.enter_context(mock.patch("timecapsulesmb.cli.context.mounted_mast_volumes_conn", return_value=mounted)),
        )

    def managed_runtime_probe(self, ready: bool) -> ManagedRuntimeProbeResult:
        status = "PASS" if ready else "FAIL"
        detail = "managed runtime is ready" if ready else "managed runtime is not ready"
        smbd = ManagedSmbdProbeResult(ready, detail, (f"{status}:managed smbd ready",))
        mdns = ManagedMdnsTakeoverProbeResult(ready, detail, (f"{status}:managed mDNS takeover active",))
        return ManagedRuntimeProbeResult(
            ready=ready,
            detail=detail,
            smbd=smbd,
            mdns=mdns,
            lines=smbd.lines + mdns.lines,
        )

    def setUp(self) -> None:
        self._exit_stack = ExitStack()
        self._telemetry_client = mock.Mock()
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
            "timecapsulesmb.cli.repair_xattrs.TelemetryClient.from_config",
            "timecapsulesmb.cli.set_ssh.TelemetryClient.from_config",
            "timecapsulesmb.cli.uninstall.TelemetryClient.from_config",
            "timecapsulesmb.cli.validate_install.TelemetryClient.from_config",
        ):
            self._exit_stack.enter_context(mock.patch(target, return_value=self._telemetry_client))
        self._exit_stack.enter_context(
            mock.patch(
                "timecapsulesmb.cli.runtime.probe_remote_interface_conn",
                return_value=RemoteInterfaceProbeResult(iface="bridge0", exists=True, detail="interface bridge0 exists"),
            )
        )
        self._exit_stack.enter_context(
            mock.patch(
                "timecapsulesmb.cli.runtime.read_interface_ipv4_addrs_conn",
                return_value=("192.168.1.217",),
            )
        )
        self._exit_stack.enter_context(
            mock.patch(
                "timecapsulesmb.cli.deploy.read_interface_ipv4_addrs_conn",
                return_value=("192.168.1.217",),
            )
        )
        self._exit_stack.enter_context(mock.patch("timecapsulesmb.device.probe.tcp_open", return_value=False))
        self._exit_stack.enter_context(mock.patch("timecapsulesmb.cli.configure.missing_required_python_module", return_value=None))
        self._exit_stack.enter_context(
            mock.patch(
                "timecapsulesmb.cli.configure.probe_remote_interface_candidates_conn",
                return_value=RemoteInterfaceCandidatesProbeResult(
                    candidates=(
                        RemoteInterfaceCandidate(
                            name="bridge0",
                            ipv4_addrs=("192.168.1.217",),
                            up=True,
                            active=True,
                            loopback=False,
                        ),
                    ),
                    preferred_iface="bridge0",
                    detail="preferred interface bridge0",
                ),
            )
        )
        def fake_configure_acp_probe(_connection, command_context, **_kwargs):
            command_context.add_debug_fields(
                configure_acp_enable_attempted=True,
                configure_acp_enable_succeeded=True,
                ssh_initially_reachable=False,
            )
            command_context.update_fields(ssh_final_reachable=True)
            return self.make_probe_state(self.make_probe_result_netbsd6())

        self._configure_acp_probe_mock = self._exit_stack.enter_context(
            mock.patch(
                "timecapsulesmb.cli.configure.enable_ssh_and_reprobe_for_configure",
                side_effect=fake_configure_acp_probe,
            )
        )
        self._exit_stack.enter_context(
            mock.patch(
                "timecapsulesmb.cli.flows.acp_reboot",
                side_effect=ACPConnectionError("ACP unavailable in tests"),
            )
        )
        self._version_check = self._exit_stack.enter_context(
            mock.patch(
                "timecapsulesmb.cli.main.check_client_version",
                return_value=VersionCheckResult(should_block=False),
            )
        )

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

    def make_supported_netbsd4_stable_compatibility(self) -> DeviceCompatibility:
        return DeviceCompatibility(
            os_name="NetBSD",
            os_release="4.0_STABLE",
            arch="earmv4",
            elf_endianness="big",
            payload_family="netbsd4be_samba4",
            device_generation="gen1-4",
            supported=True,
            reason_code="supported_netbsd4",
        )

    def make_valid_env(self, **overrides: str) -> dict[str, str]:
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

    def make_flash_bank(
        self,
        *,
        release: bytes = b"NetBSD 4.0 #0: test",
        login: bytes = STOCK_LOGIN_NETBSD4_DUMMY,
    ) -> bytes:
        decompressed = b"kernel " + release + b"\n" + login + (b"\x00" * 64)
        gz = make_test_gzip_member(decompressed)
        body = b"BOOT" + (b"\x00" * 16) + gz + b"\x00\x00"
        end_offset = len(body)
        checksum = zlib.adler32(body) & 0xFFFFFFFF
        return body + (b"\xff" * 16) + struct.pack(">II", checksum, end_offset) + (b"\xff" * 24)

    def flash_bank_checksum(self, bank: bytes) -> int:
        for offset in range(max(0, len(bank) - 4096), len(bank) - 7):
            checksum, end_offset = struct.unpack(">II", bank[offset : offset + 8])
            if end_offset < offset and end_offset <= len(bank) and zlib.adler32(bank[:end_offset]) & 0xFFFFFFFF == checksum:
                return checksum
        self.fail("synthetic flash bank footer not found")

    def flash_bank_end_offset(self, bank: bytes) -> int:
        for offset in range(max(0, len(bank) - 4096), len(bank) - 7):
            checksum, end_offset = struct.unpack(">II", bank[offset : offset + 8])
            if end_offset < offset and end_offset <= len(bank) and zlib.adler32(bank[:end_offset]) & 0xFFFFFFFF == checksum:
                return end_offset
        self.fail("synthetic flash bank footer not found")

    def make_firmware_template(
        self,
        bank: bytes,
        *,
        product_id: int = 113,
        version: int = 0x07818000,
        key: BasebinaryKey | None = None,
    ) -> bytes:
        selected_key = key or next(key for key in DEFAULT_BASEBINARY_KEYS if key.key_id == "observed-k30a-78100")
        inner_header = BasebinaryHeader(
            iv_suffix=0x2E,
            model=product_id,
            version=version,
            byte_0x18=0,
            byte_0x19=0,
            byte_0x1a=0,
            flags=0x02,
            unk_0x1c=0,
        )
        outer_header = BasebinaryHeader(
            iv_suffix=0x2E,
            model=product_id,
            version=version,
            byte_0x18=0,
            byte_0x19=0,
            byte_0x1a=0,
            flags=0,
            unk_0x1c=0,
        )
        inner = compose_basebinary(inner_header, bank[: self.flash_bank_end_offset(bank)], key=selected_key)
        return compose_basebinary(outer_header, inner)

    def make_patched_flash_bank(self, bank: bytes, secondary: bytes | None = None) -> bytes:
        fallback_secondary = secondary or self.make_flash_bank(release=b"NetBSD 4.0_BETA2 #0: old")
        with self.flash_zopfli_available():
            analysis = cli_flash.analyze_flash_banks(
                primary_data=bank,
                secondary_data=fallback_secondary,
                cks1=self.flash_bank_checksum(bank),
                cks2=self.flash_bank_checksum(fallback_secondary),
                os_release="4.0_STABLE",
            )
        active = cli_flash.require_write_ready(analysis)
        assert active.patch is not None
        return active.patch.target_bank

    @contextmanager
    def flash_zopfli_available(self):
        with mock.patch("timecapsulesmb.flash.require_python_module", return_value=None):
            with mock.patch("timecapsulesmb.flash._load_zopfli_gzip", return_value=FastFakeZopfliGzipForCli):
                yield

    def make_app_config(self, values: dict[str, str] | None = None, *, exists: bool = True, path: Path | None = None) -> AppConfig:
        config_values = dict(values or {})
        return AppConfig.from_values(
            config_values,
            path=path or REPO_ROOT / ".env",
            exists=exists,
            file_values=config_values if exists else {},
        )

    def make_probe_result_unreachable(self) -> ProbeResult:
        return ProbeResult(
            ssh_port_reachable=False,
            ssh_authenticated=False,
            error="SSH is not reachable yet.",
            os_name="",
            os_release="",
            arch="",
            elf_endianness="unknown",
        )

    def make_probe_result_auth_failed(self) -> ProbeResult:
        return ProbeResult(
            ssh_port_reachable=True,
            ssh_authenticated=False,
            error="SSH authentication failed.",
            os_name="",
            os_release="",
            arch="",
            elf_endianness="unknown",
        )

    def make_probe_result_netbsd6(self) -> ProbeResult:
        return ProbeResult(
            ssh_port_reachable=True,
            ssh_authenticated=True,
            error=None,
            os_name="NetBSD",
            os_release="6.0",
            arch="earmv4",
            elf_endianness="little",
            airport_model="TimeCapsule8,119",
            airport_syap="119",
        )

    def make_probe_result_netbsd6_no_identity(self) -> ProbeResult:
        return ProbeResult(
            ssh_port_reachable=True,
            ssh_authenticated=True,
            error=None,
            os_name="NetBSD",
            os_release="6.0",
            arch="earmv4",
            elf_endianness="little",
        )

    def make_probe_result_netbsd6_unknown(self) -> ProbeResult:
        return ProbeResult(
            ssh_port_reachable=True,
            ssh_authenticated=True,
            error=None,
            os_name="NetBSD",
            os_release="6.0",
            arch="earmv4",
            elf_endianness="unknown",
        )

    def make_probe_result_netbsd6_big(self) -> ProbeResult:
        return ProbeResult(
            ssh_port_reachable=True,
            ssh_authenticated=True,
            error=None,
            os_name="NetBSD",
            os_release="6.0",
            arch="earmv4",
            elf_endianness="big",
        )

    def make_probe_result_netbsd4le(self) -> ProbeResult:
        return ProbeResult(
            ssh_port_reachable=True,
            ssh_authenticated=True,
            error=None,
            os_name="NetBSD",
            os_release="4.0",
            arch="earmv4",
            elf_endianness="little",
        )

    def make_probe_result_netbsd4le_airport_identity_113(self) -> ProbeResult:
        return ProbeResult(
            ssh_port_reachable=True,
            ssh_authenticated=True,
            error=None,
            os_name="NetBSD",
            os_release="4.0",
            arch="earmv4",
            elf_endianness="little",
            airport_model="TimeCapsule6,113",
            airport_syap="113",
        )

    def make_probe_result_netbsd4be(self) -> ProbeResult:
        return ProbeResult(
            ssh_port_reachable=True,
            ssh_authenticated=True,
            error=None,
            os_name="NetBSD",
            os_release="4.0",
            arch="earmv4",
            elf_endianness="big",
        )

    def make_probe_result_netbsd4be_airport_identity_106(self) -> ProbeResult:
        return ProbeResult(
            ssh_port_reachable=True,
            ssh_authenticated=True,
            error=None,
            os_name="NetBSD",
            os_release="4.0",
            arch="earmv4",
            elf_endianness="big",
            airport_model="TimeCapsule6,106",
            airport_syap="106",
        )

    def make_probe_result_netbsd4_unknown(self) -> ProbeResult:
        return ProbeResult(
            ssh_port_reachable=True,
            ssh_authenticated=True,
            error=None,
            os_name="NetBSD",
            os_release="4.0_STABLE",
            arch="evbarm",
            elf_endianness="unknown",
        )

    def make_probe_result_netbsd5(self) -> ProbeResult:
        return ProbeResult(
            ssh_port_reachable=True,
            ssh_authenticated=True,
            error=None,
            os_name="NetBSD",
            os_release="5.0",
            arch="earmv4",
            elf_endianness="little",
        )

    def make_probe_state(self, probe_result: ProbeResult) -> ProbedDeviceState:
        compatibility = compatibility_from_probe_result(probe_result) if probe_result.ssh_authenticated else None
        return ProbedDeviceState(probe_result=probe_result, compatibility=compatibility)

    def configure_finished_error(self) -> str:
        for call in reversed(self._telemetry_client.emit.call_args_list):
            if call.args and call.args[0] == "configure_finished":
                return call.kwargs["error"]
        self.fail("configure_finished telemetry was not emitted")

    def configure_finished_result(self) -> str:
        for call in reversed(self._telemetry_client.emit.call_args_list):
            if call.args and call.args[0] == "configure_finished":
                return call.kwargs["result"]
        self.fail("configure_finished telemetry was not emitted")

    def telemetry_payload(self, event: str) -> dict[str, object]:
        for call in reversed(self._telemetry_client.emit.call_args_list):
            if call.args and call.args[0] == event:
                return call.kwargs
        self.fail(f"{event} telemetry was not emitted")

    def configure_prompt_defaults(self, *, host: str = "root@10.0.0.2", password: str = "pw"):
        def fake_prompt(label, default, _secret):
            if label == "Device SSH target":
                return host
            if label == "Device root password":
                return password
            if label == "Airport Utility syAP code":
                return "119"
            if label == "mDNS device model hint":
                return "TimeCapsule8,119"
            return default

        return fake_prompt

    def run_configure_cli(
        self,
        argv: list[str] | None = None,
        *,
        existing_values: dict[str, str] | None = None,
        discovered_records: list[Discovered] | None = None,
        discovery_side_effect=None,
        discovered_root_host: str | None = None,
        input_side_effect=None,
        prompt_side_effect=None,
        probe_state: ProbedDeviceState | None = None,
        interface_probe: RemoteInterfaceCandidatesProbeResult | None = None,
        confirm: bool | None = None,
        write_side_effect=None,
        command_context=None,
        patch_telemetry: bool = False,
        ensure_install_id: bool = False,
        extra_patches: dict[str, object] | None = None,
        raises=None,
    ):
        output = io.StringIO()
        written_values: dict[str, str] = {}
        mocks = SimpleNamespace()
        raised = None

        def capture_write_env(_path, values):
            written_values.update(values)

        with ExitStack() as stack:
            if ensure_install_id:
                mocks.ensure_install_id = stack.enter_context(mock.patch("timecapsulesmb.cli.configure.ensure_install_id"))
            mocks.parse_env_file = stack.enter_context(
                mock.patch("timecapsulesmb.cli.configure.parse_env_file", return_value=dict(existing_values or {}))
            )
            if discovery_side_effect is not None:
                mocks.discover_resolved_records = stack.enter_context(
                    mock.patch("timecapsulesmb.cli.configure.discover_resolved_records", side_effect=discovery_side_effect)
                )
            else:
                mocks.discover_resolved_records = stack.enter_context(
                    mock.patch("timecapsulesmb.cli.configure.discover_resolved_records", return_value=list(discovered_records or []))
                )
            if discovered_root_host is not None:
                mocks.discovered_record_root_host = stack.enter_context(
                    mock.patch("timecapsulesmb.cli.configure.discovered_record_root_host", return_value=discovered_root_host)
                )
            if input_side_effect is not None:
                mocks.input = stack.enter_context(mock.patch("builtins.input", side_effect=input_side_effect))
            if prompt_side_effect is not None:
                mocks.prompt = stack.enter_context(mock.patch("timecapsulesmb.cli.configure.prompt", side_effect=prompt_side_effect))
            if probe_state is not None:
                mocks.probe_connection_state = stack.enter_context(
                    mock.patch("timecapsulesmb.cli.configure.probe_connection_state", return_value=probe_state)
                )
            if interface_probe is not None:
                mocks.probe_remote_interface_candidates_conn = stack.enter_context(
                    mock.patch("timecapsulesmb.cli.configure.probe_remote_interface_candidates_conn", return_value=interface_probe)
                )
            if confirm is not None:
                mocks.confirm = stack.enter_context(mock.patch("timecapsulesmb.cli.configure.confirm", return_value=confirm))
            mocks.write_env_file = stack.enter_context(
                mock.patch(
                    "timecapsulesmb.cli.configure.write_env_file",
                    side_effect=write_side_effect if write_side_effect is not None else capture_write_env,
                )
            )
            if patch_telemetry:
                mocks.telemetry_factory = stack.enter_context(mock.patch("timecapsulesmb.cli.configure.TelemetryClient.from_config"))
            if command_context is not None:
                mocks.command_context_factory = stack.enter_context(
                    mock.patch("timecapsulesmb.cli.configure.CommandContext", return_value=command_context)
                )
            for index, (target, replacement) in enumerate((extra_patches or {}).items()):
                setattr(mocks, f"extra_{index}", stack.enter_context(mock.patch(target, replacement)))
            if raises is None:
                with redirect_stdout(output):
                    rc = configure.main(argv or [])
            else:
                with self.assertRaises(raises) as raised_context:
                    with redirect_stdout(output):
                        configure.main(argv or [])
                rc = None
                raised = raised_context.exception

        return SimpleNamespace(rc=rc, output=output, text=output.getvalue(), values=written_values, mocks=mocks, exception=raised)

    def run_configure_after_bonjour_error(self, error: BaseException):
        def fake_prompt(label, default, _secret):
            if label == "Device SSH target":
                return default
            if label == "Device root password":
                return "pw"
            if label == "Airport Utility syAP code":
                return "119"
            if label == "mDNS device model hint":
                return default or "TimeCapsule8,119"
            return default

        result = self.run_configure_cli(
            discovery_side_effect=error,
            prompt_side_effect=fake_prompt,
            probe_state=self.make_probe_state(self.make_probe_result_netbsd6_no_identity()),
        )
        return result.rc, result.text, result.values

    def run_deploy_cli(
        self,
        argv: list[str] | None = None,
        *,
        values: dict[str, str] | None = None,
        artifacts: list[tuple[str, bool, str]] | None = None,
        compatibility: DeviceCompatibility | None = None,
        mount_root: str = "/Volumes/dk2",
        command_context=None,
        ensure_install_id: bool = False,
        patch_actions: bool = False,
        patch_upload: bool = False,
        upload_side_effect=None,
        mast_volumes: tuple[MaStVolume, ...] | None = None,
        mast_discovery: MaStDiscoveryResult | None = None,
        payload_home_selection: PayloadHomeSelection | None = None,
        select_payload_home_side_effect=None,
        payload_verification: PayloadVerificationResult | None = None,
        payload_verification_side_effect=None,
        verify_runtime=None,
        reboot_side_effect=None,
        wait_side_effect=None,
        input_side_effect=None,
        raises=None,
    ):
        output = io.StringIO()
        mocks = SimpleNamespace()
        raised = None
        if artifacts is None:
            artifacts = [("smbd", True, "ok"), ("mdns", True, "ok"), ("nbns", True, "ok")]
        config_values = values or self.make_valid_env()
        payload_home = self._payload_home(mount_root, config_values.get("TC_PAYLOAD_DIR_NAME", DEFAULTS["TC_PAYLOAD_DIR_NAME"]))
        if mast_volumes is None:
            mast_volumes = (self._mast_volume(mount_root.rstrip("/").rsplit("/", 1)[-1]),)
        if mast_discovery is None:
            mast_discovery = MaStDiscoveryResult(mast_volumes, 1)
        if payload_home_selection is None:
            checks = (PayloadCandidateCheck(mast_volumes[0], True, True),) if mast_volumes else ()
            payload_home_selection = PayloadHomeSelection(payload_home, checks)
        with ExitStack() as stack:
            if ensure_install_id:
                mocks.ensure_install_id = stack.enter_context(mock.patch("timecapsulesmb.cli.deploy.ensure_install_id"))
            mocks.load_env_config = stack.enter_context(
                mock.patch("timecapsulesmb.cli.deploy.load_env_config", return_value=self.make_app_config(config_values))
            )
            if command_context is not None:
                mocks.command_context = stack.enter_context(mock.patch("timecapsulesmb.cli.deploy.CommandContext", return_value=command_context))
            mocks.validate_artifacts = stack.enter_context(mock.patch("timecapsulesmb.cli.deploy.validate_artifacts", return_value=artifacts))
            mocks.wait_for_mast_volumes_conn = stack.enter_context(
                mock.patch("timecapsulesmb.cli.context.wait_for_mast_volumes_conn", return_value=mast_discovery)
            )
            if select_payload_home_side_effect is None:
                mocks.select_payload_home_with_diagnostics_conn = stack.enter_context(
                    mock.patch(
                        "timecapsulesmb.cli.context.select_payload_home_with_diagnostics_conn",
                        return_value=payload_home_selection,
                    )
                )
            else:
                mocks.select_payload_home_with_diagnostics_conn = stack.enter_context(
                    mock.patch(
                        "timecapsulesmb.cli.context.select_payload_home_with_diagnostics_conn",
                        side_effect=select_payload_home_side_effect,
                    )
                )
            mocks.require_compatibility = stack.enter_context(
                mock.patch(
                    "timecapsulesmb.cli.context.CommandContext.require_compatibility",
                    return_value=compatibility or self.make_supported_compatibility(),
                )
            )
            if patch_actions:
                mocks.run_remote_actions = stack.enter_context(mock.patch("timecapsulesmb.cli.deploy.run_remote_actions"))
            if patch_upload:
                mocks.upload_deployment_payload = stack.enter_context(
                    mock.patch("timecapsulesmb.cli.deploy.upload_deployment_payload", side_effect=upload_side_effect)
                )
            mocks.flush_remote_filesystem_writes = stack.enter_context(
                mock.patch("timecapsulesmb.cli.deploy.flush_remote_filesystem_writes")
            )
            payload_verification_patch_kwargs = (
                {"side_effect": payload_verification_side_effect}
                if payload_verification_side_effect is not None
                else {"return_value": payload_verification or PayloadVerificationResult(True, "ok")}
            )
            mocks.verify_payload_home_conn = stack.enter_context(
                mock.patch(
                    "timecapsulesmb.cli.deploy.verify_payload_home_conn",
                    **payload_verification_patch_kwargs,
                )
            )
            if verify_runtime is not None:
                mocks.verify_managed_runtime = stack.enter_context(
                    mock.patch("timecapsulesmb.cli.flows.verify_managed_runtime", return_value=verify_runtime)
                )
            if reboot_side_effect is not None:
                mocks.remote_request_reboot = stack.enter_context(
                    mock.patch("timecapsulesmb.cli.flows.remote_request_reboot", side_effect=reboot_side_effect)
                )
            if wait_side_effect is not None:
                mocks.wait_for_ssh_state_conn = stack.enter_context(
                    mock.patch("timecapsulesmb.cli.flows.wait_for_ssh_state_conn", side_effect=wait_side_effect)
                )
            if input_side_effect is not None:
                mocks.input = stack.enter_context(mock.patch("builtins.input", side_effect=input_side_effect))
            if raises is None:
                with redirect_stdout(output):
                    rc = deploy.main(argv or [])
            else:
                with self.assertRaises(raises) as raised_context:
                    with redirect_stdout(output):
                        deploy.main(argv or [])
                rc = None
                raised = raised_context.exception
        return SimpleNamespace(rc=rc, output=output, text=output.getvalue(), mocks=mocks, exception=raised)

    def force_configure_acp_reprobe_auth_failed(self) -> None:
        self._configure_acp_probe_mock.side_effect = [self.make_probe_state(self.make_probe_result_auth_failed())]

    def test_dispatches_to_command_handler(self) -> None:
        with mock.patch("timecapsulesmb.cli.main.COMMANDS", {"doctor": mock.Mock(return_value=7)}):
            rc = main(["doctor", "--skip-smb"])
        self.assertEqual(rc, 7)
        self._version_check.assert_called_once_with()

    def test_main_blocks_outdated_client_before_dispatch(self) -> None:
        stderr = io.StringIO()
        command = mock.Mock(return_value=7)
        self._version_check.return_value = VersionCheckResult(
            should_block=True,
            checked_url=VERSION_CHECK_URL,
            message="This version is no longer supported. Please update before continuing.",
            download_url=DEFAULT_DOWNLOAD_URL,
        )

        with mock.patch("timecapsulesmb.cli.main.COMMANDS", {"doctor": command}):
            with redirect_stderr(stderr):
                rc = main(["doctor", "--skip-smb"])

        self.assertEqual(rc, 1)
        command.assert_not_called()
        output = stderr.getvalue()
        self.assertIn(f"Checking current version from: {VERSION_CHECK_URL}", output)
        self.assertIn(f"Client version is out of date, download the latest version from: {DEFAULT_DOWNLOAD_URL}", output)

    def test_main_skips_version_check_for_command_help(self) -> None:
        command = mock.Mock(return_value=0)
        with mock.patch("timecapsulesmb.cli.main.COMMANDS", {"doctor": command}):
            rc = main(["doctor", "--help"])

        self.assertEqual(rc, 0)
        self._version_check.assert_not_called()
        command.assert_called_once_with(["--help"])

    def test_main_dispatches_when_version_check_raises(self) -> None:
        command = mock.Mock(return_value=7)
        self._version_check.side_effect = RuntimeError("version check failed")

        with mock.patch("timecapsulesmb.cli.main.COMMANDS", {"doctor": command}):
            rc = main(["doctor", "--skip-smb"])

        self.assertEqual(rc, 7)
        command.assert_called_once_with(["--skip-smb"])

    def test_main_handles_keyboard_interrupt_cleanly(self) -> None:
        stderr = io.StringIO()
        with mock.patch("timecapsulesmb.cli.main.COMMANDS", {"doctor": mock.Mock(side_effect=KeyboardInterrupt)}):
            with redirect_stderr(stderr):
                rc = main(["doctor", "--skip-smb"])
        self.assertEqual(rc, 130)
        self.assertEqual(stderr.getvalue(), "\nCancelled.\n")

    def test_main_preserves_cancelled_telemetry_on_keyboard_interrupt(self) -> None:
        stderr = io.StringIO()
        command_context = FakeCommandContext(compatibility=self.make_supported_netbsd4_compatibility())

        def fake_command(_argv):
            with command_context:
                raise KeyboardInterrupt

        with mock.patch("timecapsulesmb.cli.main.COMMANDS", {"doctor": fake_command}):
            with redirect_stderr(stderr):
                rc = main(["doctor", "--skip-smb"])
        self.assertEqual(rc, 130)
        self.assertEqual(stderr.getvalue(), "\nCancelled.\n")
        command_context.finish.assert_called_once_with(result="cancelled", error="Cancelled by user")

    def test_activate_command_is_registered(self) -> None:
        with mock.patch("timecapsulesmb.cli.main.COMMANDS", {"activate": mock.Mock(return_value=0)}) as commands:
            rc = main(["activate", "--dry-run"])
        self.assertEqual(rc, 0)
        commands["activate"].assert_called_once_with(["--dry-run"])

    def test_fsck_command_is_registered(self) -> None:
        with mock.patch("timecapsulesmb.cli.main.COMMANDS", {"fsck": mock.Mock(return_value=0)}) as commands:
            rc = main(["fsck", "--yes", "--no-reboot"])
        self.assertEqual(rc, 0)
        commands["fsck"].assert_called_once_with(["--yes", "--no-reboot"])

    def test_set_ssh_command_replaces_prep_device(self) -> None:
        self.assertIs(cli_main_module.COMMANDS["set-ssh"], set_ssh.main)
        self.assertNotIn("prep-device", cli_main_module.COMMANDS)

    def test_paths_and_validate_install_commands_are_registered(self) -> None:
        self.assertIs(cli_main_module.COMMANDS["paths"], paths.main)
        self.assertIs(cli_main_module.COMMANDS["validate-install"], validate_install.main)

    def test_paths_json_command_prints_resolved_install_paths(self) -> None:
        app_paths = AppPaths(
            distribution_root=REPO_ROOT,
            config_path=REPO_ROOT / ".env",
            state_dir=REPO_ROOT,
            package_root=SRC_ROOT / "timecapsulesmb",
        )
        output = io.StringIO()
        data = {
            "distribution_root": str(app_paths.distribution_root),
            "config_path": str(app_paths.config_path),
            "state_dir": str(app_paths.state_dir),
            "package_root": str(app_paths.package_root),
            "artifact_manifest": "manifest.json",
            "artifacts": [
                {
                    "name": "smbd",
                    "absolute_path": str(REPO_ROOT / "bin" / "samba4" / "smbd"),
                    "ok": True,
                    "message": "validated bin/samba4/smbd",
                }
            ],
        }
        with mock.patch("timecapsulesmb.cli.paths.ensure_install_id"):
            with mock.patch("timecapsulesmb.cli.paths.resolve_app_paths", return_value=app_paths):
                with mock.patch("timecapsulesmb.cli.paths.paths_to_jsonable", return_value=data):
                    with redirect_stdout(output):
                        rc = paths.main(["--json"])

        self.assertEqual(rc, 0)
        rendered = json.loads(output.getvalue())
        self.assertEqual(rendered["distribution_root"], str(REPO_ROOT))
        self.assertEqual(rendered["config_path"], str(REPO_ROOT / ".env"))
        self.assertEqual(rendered["artifacts"][0]["name"], "smbd")
        started = self.telemetry_payload("paths_started")
        finished = self.telemetry_payload("paths_finished")
        self.assertTrue(started["json_output"])
        self.assertEqual(finished["result"], "success")
        self.assertEqual(finished["artifact_count"], 1)
        self.assertEqual(finished["missing_artifact_count"], 0)

    def test_paths_config_arg_is_passed_to_path_resolution(self) -> None:
        app_paths = AppPaths(
            distribution_root=REPO_ROOT,
            config_path=REPO_ROOT / "custom.env",
            state_dir=REPO_ROOT,
            package_root=SRC_ROOT / "timecapsulesmb",
        )
        data = {
            "distribution_root": str(app_paths.distribution_root),
            "config_path": str(app_paths.config_path),
            "state_dir": str(app_paths.state_dir),
            "package_root": str(app_paths.package_root),
            "artifact_manifest": "manifest.json",
            "artifacts": [],
        }
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / "custom.env"
            with mock.patch("timecapsulesmb.cli.paths.ensure_install_id"):
                with mock.patch("timecapsulesmb.cli.paths.load_optional_env_config", return_value=self.make_app_config({}, exists=False)) as load_mock:
                    with mock.patch("timecapsulesmb.cli.paths.resolve_app_paths", return_value=app_paths) as resolve_mock:
                        with mock.patch("timecapsulesmb.cli.paths.paths_to_jsonable", return_value=data):
                            with redirect_stdout(io.StringIO()):
                                rc = paths.main(["--json", "--config", str(env_path)])

        self.assertEqual(rc, 0)
        load_mock.assert_called_once_with(env_path=env_path)
        resolve_mock.assert_called_once_with(config_path=env_path)

    def test_validate_install_json_command_returns_failure_when_check_fails(self) -> None:
        app_paths = AppPaths(
            distribution_root=REPO_ROOT,
            config_path=REPO_ROOT / ".env",
            state_dir=REPO_ROOT,
            package_root=SRC_ROOT / "timecapsulesmb",
        )
        checks = [
            InstallCheckResult("python_modules", True, "required Python modules import"),
            InstallCheckResult("artifact_hashes", False, "artifact validation failed", {"failures": ["missing bin/smbd"]}),
        ]
        output = io.StringIO()
        with mock.patch("timecapsulesmb.cli.validate_install.ensure_install_id"):
            with mock.patch("timecapsulesmb.cli.validate_install.resolve_app_paths", return_value=app_paths):
                with mock.patch("timecapsulesmb.cli.validate_install.validate_install", return_value=checks):
                    with redirect_stdout(output):
                        rc = validate_install.main(["--json"])

        self.assertEqual(rc, 1)
        rendered = json.loads(output.getvalue())
        self.assertFalse(rendered["ok"])
        self.assertEqual(rendered["checks"][1]["id"], "artifact_hashes")
        self.assertEqual(rendered["checks"][1]["details"]["failures"], ["missing bin/smbd"])
        finished = self.telemetry_payload("validate_install_finished")
        self.assertEqual(finished["result"], "failure")
        self.assertEqual(finished["install_ok"], False)
        self.assertEqual(finished["failed_check_ids"], ["artifact_hashes"])
        self.assertIn("install validation failed", finished["error"])

    def test_validate_install_config_arg_is_passed_to_path_resolution(self) -> None:
        app_paths = AppPaths(
            distribution_root=REPO_ROOT,
            config_path=REPO_ROOT / "custom.env",
            state_dir=REPO_ROOT,
            package_root=SRC_ROOT / "timecapsulesmb",
        )
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / "custom.env"
            with mock.patch("timecapsulesmb.cli.validate_install.ensure_install_id"):
                with mock.patch("timecapsulesmb.cli.validate_install.load_optional_env_config", return_value=self.make_app_config({}, exists=False)) as load_mock:
                    with mock.patch("timecapsulesmb.cli.validate_install.resolve_app_paths", return_value=app_paths) as resolve_mock:
                        with mock.patch("timecapsulesmb.cli.validate_install.validate_install", return_value=[]):
                            with redirect_stdout(io.StringIO()):
                                rc = validate_install.main(["--json", "--config", str(env_path)])

        self.assertEqual(rc, 0)
        load_mock.assert_called_once_with(env_path=env_path)
        resolve_mock.assert_called_once_with(config_path=env_path)

    def test_validate_install_text_command_prints_summary(self) -> None:
        checks = [InstallCheckResult("boot_script_tokens", True, "managed boot scripts have no unresolved tokens")]
        output = io.StringIO()
        with mock.patch("timecapsulesmb.cli.validate_install.ensure_install_id"):
            with mock.patch("timecapsulesmb.cli.validate_install.resolve_app_paths"):
                with mock.patch("timecapsulesmb.cli.validate_install.validate_install", return_value=checks):
                    with redirect_stdout(output):
                        rc = validate_install.main([])

        self.assertEqual(rc, 0)
        self.assertIn("PASS managed boot scripts have no unresolved tokens", output.getvalue())
        self.assertIn("Summary: install validation passed.", output.getvalue())

    def test_config_arg_is_passed_to_shared_config_loaders(self) -> None:
        commands = [
            ("activate", activate, "load_env_config", None),
            ("doctor", doctor, "load_env_config", None),
            ("uninstall", uninstall, "load_env_config", None),
            ("fsck", fsck, "load_env_config", None),
            ("set_ssh", set_ssh, "load_env_config", {"defaults": {}}),
            ("discover", discover, "load_optional_env_config", None),
            ("paths", paths, "load_optional_env_config", None),
            ("validate_install", validate_install, "load_optional_env_config", None),
            ("repair_xattrs", repair_xattrs, "load_optional_env_config", None),
        ]
        sentinel = RuntimeError("stop after config load")
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / "shared.env"
            for _name, command_module, loader_name, extra_kwargs in commands:
                with self.subTest(command=command_module.__name__):
                    patches = [
                        mock.patch(f"{command_module.__name__}.{loader_name}", side_effect=sentinel),
                    ]
                    if hasattr(command_module, "ensure_install_id"):
                        patches.append(mock.patch(f"{command_module.__name__}.ensure_install_id"))
                    if command_module is repair_xattrs:
                        patches.append(mock.patch("sys.platform", "darwin"))
                    with ExitStack() as stack:
                        load_mock = stack.enter_context(patches[0])
                        for patcher in patches[1:]:
                            stack.enter_context(patcher)
                        with self.assertRaises(RuntimeError):
                            with redirect_stdout(io.StringIO()):
                                command_module.main(["--config", str(env_path)])
                    expected_kwargs = {"env_path": env_path}
                    if extra_kwargs is not None:
                        expected_kwargs.update(extra_kwargs)
                    load_mock.assert_called_once_with(**expected_kwargs)

    def test_optional_env_config_uses_missing_config_when_env_is_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            app_paths = AppPaths(
                distribution_root=Path(tmp),
                config_path=env_path,
                state_dir=Path(tmp),
                package_root=SRC_ROOT / "timecapsulesmb",
            )
            with mock.patch("timecapsulesmb.cli.runtime.resolve_app_paths", return_value=app_paths):
                config = cli_runtime.load_optional_env_config()

        self.assertFalse(config.exists)
        self.assertEqual(config.path, env_path)
        self.assertEqual(config.values, {})

    def test_optional_env_config_reads_env_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text("TC_HOST='root@10.0.0.2'\nTC_CONFIGURE_ID='cfg-1'\n")
            app_paths = AppPaths(
                distribution_root=Path(tmp),
                config_path=env_path,
                state_dir=Path(tmp),
                package_root=SRC_ROOT / "timecapsulesmb",
            )
            with mock.patch("timecapsulesmb.cli.runtime.resolve_app_paths", return_value=app_paths):
                config = cli_runtime.load_optional_env_config()

        self.assertTrue(config.exists)
        self.assertEqual(config.path, env_path)
        self.assertEqual(config.get("TC_HOST"), "root@10.0.0.2")
        self.assertEqual(config.get("TC_CONFIGURE_ID"), "cfg-1")

    def test_repair_xattrs_non_macos_emits_platform_check_telemetry(self) -> None:
        with mock.patch("timecapsulesmb.cli.repair_xattrs.ensure_install_id"):
            with mock.patch(
                "timecapsulesmb.cli.repair_xattrs.load_optional_env_config",
                return_value=self.make_app_config({}, exists=False),
            ):
                with mock.patch("sys.platform", "linux"):
                    with self.assertRaises(SystemExit):
                        repair_xattrs.main(["--path", "/Volumes/Home"])

        finished = self.telemetry_payload("repair_xattrs_finished")
        self.assertEqual(finished["result"], "failure")
        self.assertEqual(finished["host_platform"], "linux")
        self.assertIn("stage=platform_check", finished["error"])

    def test_bootstrap_prints_full_next_steps(self) -> None:
        output = io.StringIO()
        with mock.patch("pathlib.Path.exists", return_value=True):
            with mock.patch("timecapsulesmb.cli.bootstrap.ensure_venv", return_value=bootstrap.VENVDIR / "bin" / "python"):
                with mock.patch("timecapsulesmb.cli.bootstrap.install_python_requirements"):
                    with mock.patch("timecapsulesmb.cli.bootstrap.maybe_install_smbclient"):
                        with mock.patch("timecapsulesmb.cli.bootstrap.maybe_install_sshpass"):
                            with mock.patch("timecapsulesmb.cli.bootstrap.ensure_install_id"):
                                with mock.patch("timecapsulesmb.cli.bootstrap.current_platform_label", return_value="macOS"):
                                    with redirect_stdout(output):
                                        rc = bootstrap.main([])
        self.assertEqual(rc, 0)
        text = output.getvalue()
        self.assertIn("Detected host platform", text)
        self.assertIn("configure", text)
        self.assertIn("deploy", text)
        self.assertIn("doctor", text)
        self.assertIn("activate", text)
        self.assertNotIn("set-ssh", text)
        started = self.telemetry_payload("bootstrap_started")
        finished = self.telemetry_payload("bootstrap_finished")
        self.assertEqual(started["python_executable"], sys.executable)
        self.assertEqual(finished["result"], "success")
        self.assertEqual(finished["host_platform_label"], "macOS")

    def test_bootstrap_prints_same_core_next_steps_on_linux(self) -> None:
        output = io.StringIO()
        with mock.patch("timecapsulesmb.cli.bootstrap.current_platform_label", return_value="Linux"):
            with mock.patch("pathlib.Path.exists", return_value=True):
                with mock.patch("timecapsulesmb.cli.bootstrap.ensure_venv", return_value=bootstrap.VENVDIR / "bin" / "python"):
                    with mock.patch("timecapsulesmb.cli.bootstrap.install_python_requirements"):
                        with mock.patch("timecapsulesmb.cli.bootstrap.maybe_install_smbclient"):
                            with mock.patch("timecapsulesmb.cli.bootstrap.maybe_install_sshpass"):
                                with mock.patch("timecapsulesmb.cli.bootstrap.ensure_install_id"):
                                    with redirect_stdout(output):
                                        rc = bootstrap.main([])
        self.assertEqual(rc, 0)
        text = output.getvalue()
        self.assertIn("Detected host platform: Linux", text)
        self.assertIn("configure", text)
        self.assertIn("deploy", text)
        self.assertIn("doctor", text)
        self.assertIn("activate", text)
        self.assertNotIn("set-ssh", text)
        self.assertNotIn("AirPyrt", text)

    def test_bootstrap_rejects_removed_skip_airpyrt_flag(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as ctx:
                bootstrap.main(["--skip-airpyrt"])
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("unrecognized arguments: --skip-airpyrt", stderr.getvalue())

    def test_bootstrap_returns_error_when_requirements_missing(self) -> None:
        stderr = io.StringIO()
        with mock.patch("pathlib.Path.exists", return_value=False):
            with mock.patch("timecapsulesmb.cli.bootstrap.ensure_install_id"):
                with redirect_stderr(stderr):
                    rc = bootstrap.main([])
        self.assertEqual(rc, 1)
        self.assertIn("Missing", stderr.getvalue())
        finished = self.telemetry_payload("bootstrap_finished")
        self.assertEqual(finished["result"], "failure")
        self.assertEqual(finished["requirements_present"], False)
        self.assertIn("stage=validate_requirements", finished["error"])

    def test_bootstrap_install_python_requirements_repairs_venv_without_pip(self) -> None:
        output = io.StringIO()
        venv_python = Path("/tmp/tcapsule-venv/bin/python")
        with mock.patch("timecapsulesmb.cli.bootstrap.venv_has_pip", return_value=False):
            with mock.patch("timecapsulesmb.cli.bootstrap.run") as run_mock:
                with redirect_stdout(output):
                    bootstrap.install_python_requirements(venv_python)

        self.assertIn("bootstrapping pip with ensurepip", output.getvalue())
        self.assertEqual(
            run_mock.call_args_list,
            [
                mock.call([str(venv_python), "-m", "ensurepip", "--upgrade"]),
                mock.call([str(venv_python), "-m", "pip", "install", "-U", "pip"]),
                mock.call([str(venv_python), "-m", "pip", "install", "-r", str(bootstrap.REQUIREMENTS)]),
                mock.call([str(venv_python), "-m", "pip", "install", "-e", str(bootstrap.REPO_ROOT)]),
            ],
        )

    def test_bootstrap_install_python_requirements_skips_ensurepip_when_pip_exists(self) -> None:
        venv_python = Path("/tmp/tcapsule-venv/bin/python")
        with mock.patch("timecapsulesmb.cli.bootstrap.venv_has_pip", return_value=True):
            with mock.patch("timecapsulesmb.cli.bootstrap.run") as run_mock:
                bootstrap.install_python_requirements(venv_python)

        commands = [call.args[0] for call in run_mock.call_args_list]
        self.assertNotIn([str(venv_python), "-m", "ensurepip", "--upgrade"], commands)
        self.assertEqual(commands[0], [str(venv_python), "-m", "pip", "install", "-U", "pip"])

    def test_bootstrap_installs_smbclient_via_homebrew_on_macos(self) -> None:
        output = io.StringIO()
        with mock.patch("timecapsulesmb.cli.bootstrap.current_platform_label", return_value="macOS"):
            with mock.patch("timecapsulesmb.cli.bootstrap.find_command", side_effect=lambda name: None if name == "smbclient" else "/opt/homebrew/bin/brew"):
                with mock.patch("timecapsulesmb.cli.bootstrap.confirm", return_value=True):
                    with mock.patch(
                        "timecapsulesmb.cli.bootstrap.run",
                        side_effect=lambda cmd, cwd=None: None,
                    ) as run_mock:
                        with redirect_stdout(output):
                            bootstrap.maybe_install_smbclient()
        text = output.getvalue()
        self.assertIn("brew install samba", text)
        self.assertEqual(run_mock.call_args_list, [mock.call(["/opt/homebrew/bin/brew", "install", "samba"])])

    def test_bootstrap_prints_linux_smbclient_instructions_when_missing(self) -> None:
        output = io.StringIO()
        with mock.patch("timecapsulesmb.cli.bootstrap.current_platform_label", return_value="Linux"):
            def fake_which(name: str):
                if name == "smbclient":
                    return None
                if name == "apt-get":
                    return "/usr/bin/apt-get"
                return None
            with mock.patch("timecapsulesmb.cli.bootstrap.find_command", side_effect=fake_which):
                with redirect_stdout(output):
                    bootstrap.maybe_install_smbclient()
        text = output.getvalue()
        self.assertIn("smbclient is required", text)
        self.assertIn("sudo apt-get update && sudo apt-get install -y smbclient", text)
        self.assertIn("After installing smbclient", text)

    def test_bootstrap_installs_sshpass_via_homebrew_on_macos(self) -> None:
        output = io.StringIO()

        def fake_which(name: str):
            if name == "sshpass":
                return None
            if name == "brew":
                return "/opt/homebrew/bin/brew"
            return None

        with mock.patch("timecapsulesmb.cli.bootstrap.current_platform_label", return_value="macOS"):
            with mock.patch("timecapsulesmb.cli.bootstrap.find_command", side_effect=fake_which):
                with mock.patch("timecapsulesmb.cli.bootstrap.run") as run_mock:
                    with redirect_stdout(output):
                        bootstrap.maybe_install_sshpass()
        self.assertIn("Installing sshpass", output.getvalue())
        self.assertEqual(
            run_mock.call_args_list,
            [
                mock.call(["/opt/homebrew/bin/brew", "tap", "hudochenkov/sshpass"]),
                mock.call(["/opt/homebrew/bin/brew", "install", "sshpass"]),
            ],
        )

    def test_bootstrap_fails_when_homebrew_missing_for_sshpass_on_macos(self) -> None:
        output = io.StringIO()

        def fake_which(_name: str):
            return None

        with mock.patch("timecapsulesmb.cli.bootstrap.current_platform_label", return_value="macOS"):
            with mock.patch("timecapsulesmb.cli.bootstrap.find_command", side_effect=fake_which):
                with self.assertRaises(bootstrap.BootstrapError):
                    with redirect_stdout(output):
                        bootstrap.maybe_install_sshpass()
        text = output.getvalue()
        self.assertIn("Homebrew is missing, please install Homebrew", text)
        self.assertIn("https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh", text)
        self.assertIn("\033[31m", text)

    def test_bootstrap_installs_sshpass_via_apt_on_linux(self) -> None:
        def fake_which(name: str):
            if name == "sshpass":
                return None
            if name == "apt-get":
                return "/usr/bin/apt-get"
            return None

        with mock.patch("timecapsulesmb.cli.bootstrap.current_platform_label", return_value="Linux"):
            with mock.patch("timecapsulesmb.cli.bootstrap.find_command", side_effect=fake_which):
                with mock.patch("timecapsulesmb.cli.bootstrap.run") as run_mock:
                    bootstrap.maybe_install_sshpass()
        self.assertEqual(
            run_mock.call_args_list,
            [
                mock.call(["sudo", "/usr/bin/apt-get", "update"]),
                mock.call(["sudo", "/usr/bin/apt-get", "install", "-y", "sshpass"]),
            ],
        )

    def test_bootstrap_fails_when_linux_package_manager_missing_for_sshpass(self) -> None:
        with mock.patch("timecapsulesmb.cli.bootstrap.current_platform_label", return_value="Linux"):
            with mock.patch("timecapsulesmb.cli.bootstrap.find_command", return_value=None):
                with self.assertRaises(bootstrap.BootstrapError):
                    bootstrap.maybe_install_sshpass()

    def test_configure_writes_values_from_prompts(self) -> None:
        prompt_values = iter([
            "root@10.0.0.2",
            "pw",
            "bridge0",
                        "admin",
            "TimeCapsule",
            "samba4",
            "Time Capsule Samba 4",
            "timecapsulesamba4",
            "119",
        ])

        command_context = FakeCommandContext()
        result = self.run_configure_cli(
            prompt_side_effect=lambda _l, _d, _s: _d if _l == "mDNS device model hint" else next(prompt_values),
            probe_state=self.make_probe_state(self.make_probe_result_unreachable()),
            confirm=True,
            command_context=command_context,
            patch_telemetry=True,
        )
        fake_values = result.values
        self.assertEqual(result.rc, 0)
        self.assertEqual(fake_values["TC_SAMBA_USER"], "admin")
        self.assertEqual(fake_values["TC_INTERNAL_SHARE_USE_DISK_ROOT"], "false")
        uuid.UUID(fake_values["TC_CONFIGURE_ID"])
        telemetry_values = result.mocks.telemetry_factory.call_args.args[0].values
        self.assertEqual(telemetry_values["TC_CONFIGURE_ID"], fake_values["TC_CONFIGURE_ID"])
        self.assertEqual(result.mocks.command_context_factory.call_args.kwargs["configure_id"], fake_values["TC_CONFIGURE_ID"])
        command_context.finish.assert_called_once()
        self.assertEqual(command_context.finish.call_args.kwargs["configure_id"], fake_values["TC_CONFIGURE_ID"])
        self.assertEqual(command_context.finish.call_args.kwargs["device_syap"], fake_values["TC_AIRPORT_SYAP"])
        self.assertEqual(command_context.finish.call_args.kwargs["device_model"], fake_values["TC_MDNS_DEVICE_MODEL"])
        text = result.text
        self.assertIn("This writes a local .env configuration file", text)
        self.assertIn(f"Review the .env file configuration: wrote {configure.ENV_PATH}", text)
        self.assertNotIn("set-ssh", text)
        self.assertIn("- Deploy this configuration to your Time Capsule/Airport Extreme device, run:", text)
        self.assertIn("    .venv/bin/tcapsule deploy", text)

    def test_configure_config_arg_reads_and_writes_selected_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / "custom.env"
            prompt_values = iter([
                "root@10.0.0.2",
                "pw",
                "bridge0",
                                "admin",
                "TimeCapsule",
                "samba4",
                "Time Capsule Samba 4",
                "timecapsulesamba4",
                "119",
            ])

            result = self.run_configure_cli(
                ["--config", str(env_path)],
                prompt_side_effect=lambda _l, _d, _s: next(prompt_values),
                probe_state=self.make_probe_state(self.make_probe_result_unreachable()),
                confirm=True,
                command_context=FakeCommandContext(),
            )

        self.assertEqual(result.rc, 0)
        result.mocks.parse_env_file.assert_called_once_with(env_path.resolve())
        self.assertEqual(result.mocks.write_env_file.call_args.args[0], env_path.resolve())
        self.assertIn(f"Writing {env_path.resolve()}", result.text)
        self.assertIn(f"Review the .env file configuration: wrote {env_path.resolve()}", result.text)

    def test_configure_hidden_share_use_disk_root_arg_writes_true(self) -> None:
        prompt_values = iter([
            "root@10.0.0.2",
            "pw",
            "bridge0",
                        "admin",
            "TimeCapsule",
            "samba4",
            "Time Capsule Samba 4",
            "timecapsulesamba4",
            "119",
        ])

        result = self.run_configure_cli(
            ["--share-use-disk-root"],
            prompt_side_effect=lambda label, default, _secret: default if label == "mDNS device model hint" else next(prompt_values),
            probe_state=self.make_probe_state(self.make_probe_result_unreachable()),
            confirm=True,
            command_context=FakeCommandContext(),
        )
        self.assertEqual(result.rc, 0)
        self.assertEqual(result.values["TC_INTERNAL_SHARE_USE_DISK_ROOT"], "true")

    def test_configure_airport_extreme_keeps_hidden_internal_share_root_default(self) -> None:
        def fake_prompt(label, default, _secret):
            if label == "Device root password":
                return "rootpw"
            if label == "Airport Utility syAP code":
                return "120"
            if label == "mDNS device model hint":
                raise AssertionError("mDNS device model should be derived from the final syAP")
            return default

        interface_probe = RemoteInterfaceCandidatesProbeResult(
            candidates=(
                RemoteInterfaceCandidate(name="bridge0", ipv4_addrs=("192.168.1.217",), up=True, active=True, loopback=False),
            ),
            preferred_iface="bridge0",
            detail="preferred interface bridge0",
        )

        result = self.run_configure_cli(
            prompt_side_effect=fake_prompt,
            probe_state=self.make_probe_state(self.make_probe_result_netbsd6_no_identity()),
            interface_probe=interface_probe,
        )

        self.assertEqual(result.rc, 0)
        self.assertEqual(result.values["TC_AIRPORT_SYAP"], "120")
        self.assertEqual(result.values["TC_MDNS_DEVICE_MODEL"], "AirPort7,120")
        self.assertEqual(result.values["TC_INTERNAL_SHARE_USE_DISK_ROOT"], "false")

    def test_configure_ensures_install_id_before_telemetry(self) -> None:
        prompt_values = iter([
            "root@10.0.0.2",
            "pw",
            "bridge0",
                        "admin",
            "TimeCapsule",
            "samba4",
            "Time Capsule Samba 4",
            "timecapsulesamba4",
            "119",
        ])
        result = self.run_configure_cli(
            prompt_side_effect=lambda label, default, _secret: default if label == "mDNS device model hint" else next(prompt_values),
            probe_state=self.make_probe_state(self.make_probe_result_unreachable()),
            confirm=True,
            command_context=FakeCommandContext(),
            ensure_install_id=True,
        )
        self.assertEqual(result.rc, 0)
        result.mocks.ensure_install_id.assert_called_once_with()

    def test_configure_exits_before_intro_when_required_python_module_is_missing(self) -> None:
        output = io.StringIO()
        missing_zeroconf = ("zeroconf", ModuleNotFoundError("No module named 'zeroconf'"))
        with mock.patch("timecapsulesmb.cli.configure.ensure_install_id"):
            with mock.patch("timecapsulesmb.cli.configure.parse_env_file", return_value={}):
                with mock.patch("timecapsulesmb.cli.configure.missing_required_python_module", return_value=missing_zeroconf):
                    with redirect_stdout(output):
                        rc = configure.main([])

        self.assertEqual(rc, 1)
        text = output.getvalue()
        expected = (
            "Failed to load zeroconf. Install the Python package zeroconf. "
            "Run `./tcapsule bootstrap` first to set up the required dependencies. "
            "ModuleNotFoundError: No module named 'zeroconf'"
        )
        self.assertIn(expected, text)
        self.assertNotIn("This writes a local .env configuration file", text)
        self.assertEqual(self.configure_finished_result(), "failure")
        error = self.configure_finished_error()
        self.assertIn(expected, error)
        self.assertIn("stage=dependency_check", error)

    def test_configure_dependency_preflight_reports_first_missing_module_name(self) -> None:
        output = io.StringIO()
        missing_pexpect = ("pexpect", ModuleNotFoundError("No module named 'pexpect'"))
        with mock.patch("timecapsulesmb.cli.configure.ensure_install_id"):
            with mock.patch("timecapsulesmb.cli.configure.parse_env_file", return_value={}):
                with mock.patch("timecapsulesmb.cli.configure.missing_required_python_module", return_value=missing_pexpect):
                    with redirect_stdout(output):
                        rc = configure.main([])

        self.assertEqual(rc, 1)
        expected = (
            "Failed to load pexpect. Install the Python package pexpect. "
            "Run `./tcapsule bootstrap` first to set up the required dependencies. "
            "ModuleNotFoundError: No module named 'pexpect'"
        )
        self.assertIn(expected, output.getvalue())
        self.assertIn(expected, self.configure_finished_error())

    def test_configure_does_not_persist_configure_id_before_final_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text("TC_HOST=root@10.0.0.2\n")
            with mock.patch("timecapsulesmb.cli.configure.parse_env_file", return_value={"TC_HOST": "root@10.0.0.2"}):
                with mock.patch("timecapsulesmb.cli.configure.discover_resolved_records", return_value=[]):
                    with mock.patch("timecapsulesmb.cli.configure.prompt", side_effect=KeyboardInterrupt):
                        with mock.patch("timecapsulesmb.cli.configure.TelemetryClient.from_config"):
                            with self.assertRaises(KeyboardInterrupt):
                                configure.main(["--config", str(env_path)])
            text = env_path.read_text()
            values = {}
            for line in text.splitlines():
                if "=" not in line or line.startswith("#"):
                    continue
                key, value = line.split("=", 1)
                values[key] = value
        self.assertIn("TC_HOST=root@10.0.0.2", text)
        self.assertNotIn("TC_CONFIGURE_ID=", text)

    def test_configure_falls_back_to_manual_entry_when_bonjour_permission_denied(self) -> None:
        rc, text, values = self.run_configure_after_bonjour_error(
            PermissionError(errno.EACCES, "Permission denied")
        )

        self.assertEqual(rc, 0)
        self.assertEqual(values["TC_HOST"], DEFAULTS["TC_HOST"])
        self.assertIn("Warning: mDNS discovery failed:", text)
        self.assertIn("PermissionError: [Errno 13] Permission denied", text)
        self.assertIn("This only affects automatic device discovery.", text)
        self.assertIn("Falling back to manual SSH target entry.", text)
        payload = self.telemetry_payload("configure_finished")
        self.assertEqual(payload["result"], "success")
        self.assertEqual(payload["bonjour_discovery_failed"], True)
        self.assertEqual(payload["bonjour_discovery_fallback"], True)
        self.assertEqual(payload["bonjour_discovery_fallback_reason"], "discovery_exception")
        self.assertEqual(payload["bonjour_discovery_error_type"], "PermissionError")
        self.assertIn("PermissionError: [Errno 13] Permission denied", payload["bonjour_discovery_error"])

    def test_configure_falls_back_to_manual_entry_when_bonjour_operation_not_permitted(self) -> None:
        rc, text, _values = self.run_configure_after_bonjour_error(
            OSError(errno.EPERM, "Operation not permitted")
        )

        self.assertEqual(rc, 0)
        self.assertIn("Operation not permitted", text)
        payload = self.telemetry_payload("configure_finished")
        self.assertEqual(payload["result"], "success")
        self.assertEqual(payload["bonjour_discovery_fallback"], True)
        self.assertEqual(payload["bonjour_discovery_error_type"], "PermissionError")
        self.assertIn("Operation not permitted", payload["bonjour_discovery_error"])

    def test_configure_falls_back_to_manual_entry_when_bonjour_network_is_down(self) -> None:
        rc, text, _values = self.run_configure_after_bonjour_error(
            OSError(errno.ENETDOWN, "Network is down")
        )

        self.assertEqual(rc, 0)
        self.assertIn("Network is down", text)
        payload = self.telemetry_payload("configure_finished")
        self.assertEqual(payload["result"], "success")
        self.assertEqual(payload["bonjour_discovery_fallback"], True)
        self.assertEqual(payload["bonjour_discovery_error_type"], "OSError")
        self.assertIn("Network is down", payload["bonjour_discovery_error"])

    def test_configure_falls_back_to_manual_entry_when_bonjour_runtime_error_occurs(self) -> None:
        rc, text, _values = self.run_configure_after_bonjour_error(
            RuntimeError("zeroconf broke")
        )

        self.assertEqual(rc, 0)
        self.assertIn("zeroconf broke", text)
        payload = self.telemetry_payload("configure_finished")
        self.assertEqual(payload["result"], "success")
        self.assertEqual(payload["bonjour_discovery_fallback"], True)
        self.assertEqual(payload["bonjour_discovery_error_type"], "RuntimeError")
        self.assertIn("RuntimeError: zeroconf broke", payload["bonjour_discovery_error"])

    def test_configure_does_not_fallback_for_keyboard_interrupt_during_bonjour(self) -> None:
        with mock.patch("timecapsulesmb.cli.configure.ensure_install_id"):
            with mock.patch("timecapsulesmb.cli.configure.parse_env_file", return_value={}):
                with mock.patch(
                    "timecapsulesmb.cli.configure.discover_resolved_records",
                    side_effect=KeyboardInterrupt,
                ):
                    with self.assertRaises(KeyboardInterrupt):
                        with redirect_stdout(io.StringIO()):
                            configure.main([])

        self.assertEqual(self.configure_finished_result(), "cancelled")
        self.assertIn("Cancelled by user", self.configure_finished_error())

    def test_configure_preserves_bonjour_permission_fallback_on_later_failure(self) -> None:
        def fake_prompt(label, default, _secret):
            if label == "Device SSH target":
                return default
            if label == "Device root password":
                return "pw"
            if label == "Airport Utility syAP code":
                return "119"
            if label == "mDNS device model hint":
                return default or "TimeCapsule8,119"
            return default

        result = self.run_configure_cli(
            ensure_install_id=True,
            discovery_side_effect=PermissionError(errno.EACCES, "Permission denied"),
            prompt_side_effect=fake_prompt,
            probe_state=self.make_probe_state(self.make_probe_result_netbsd6_no_identity()),
            write_side_effect=RuntimeError("disk full"),
            raises=RuntimeError,
        )

        self.assertIn("Falling back to manual SSH target entry.", result.text)
        error = self.configure_finished_error()
        self.assertIn("RuntimeError: disk full", error)
        self.assertIn("stage=write_env", error)
        self.assertIn("bonjour_discovery_failed=true", error)
        self.assertIn("bonjour_discovery_fallback=true", error)
        self.assertIn("bonjour_discovery_fallback_reason=discovery_exception", error)
        self.assertIn("bonjour_discovery_error_type=PermissionError", error)
        self.assertIn("bonjour_discovery_error=PermissionError: [Errno 13] Permission denied", error)

    def test_configure_telemetry_includes_bonjour_stage_when_discovery_fails(self) -> None:
        with mock.patch("timecapsulesmb.cli.configure.ensure_install_id"):
            with mock.patch("timecapsulesmb.cli.configure.parse_env_file", return_value={}):
                with mock.patch("timecapsulesmb.cli.configure.discover_default_record", side_effect=SystemExit("zeroconf missing")):
                    with self.assertRaises(SystemExit):
                        with redirect_stdout(io.StringIO()):
                            configure.main([])

        self.assertEqual(self.configure_finished_result(), "failure")
        error = self.configure_finished_error()
        self.assertIn("zeroconf missing", error)
        self.assertIn("Debug context:", error)
        self.assertIn("command=configure", error)
        self.assertIn("stage=bonjour_discovery", error)
        self.assertIn("TC_INTERNAL_SHARE_USE_DISK_ROOT=false", error)

    def test_configure_telemetry_records_acp_enable_branch_on_later_failure(self) -> None:
        self.run_configure_cli(
            ensure_install_id=True,
            prompt_side_effect=self.configure_prompt_defaults(),
            probe_state=self.make_probe_state(self.make_probe_result_unreachable()),
            confirm=True,
            write_side_effect=RuntimeError("disk full"),
            raises=RuntimeError,
        )

        error = self.configure_finished_error()
        self.assertIn("RuntimeError: disk full", error)
        self.assertIn("stage=write_env", error)
        self.assertIn("host=root@10.0.0.2", error)
        self.assertIn("ssh_opts=-o HostKeyAlgorithms=+ssh-rsa", error)
        self.assertIn("TC_HOST=root@10.0.0.2", error)
        self.assertIn("configure_acp_enable_attempted=true", error)
        self.assertIn("configure_acp_enable_succeeded=true", error)
        self.assertIn("ssh_initially_reachable=false", error)
        self.assertIn("ssh_final_reachable=true", error)
        self.assertIn("probe_ssh_port_reachable=true", error)
        self.assertIn("probe_ssh_authenticated=true", error)
        self.assertNotIn("TC_PASSWORD", error)
        self.assertNotIn("pw", error)

    def test_configure_telemetry_records_auth_failed_saved_branch_on_later_failure(self) -> None:
        self.run_configure_cli(
            ensure_install_id=True,
            prompt_side_effect=self.configure_prompt_defaults(password="badpw"),
            probe_state=self.make_probe_state(self.make_probe_result_auth_failed()),
            confirm=True,
            write_side_effect=RuntimeError("cannot write env"),
            raises=RuntimeError,
        )

        error = self.configure_finished_error()
        self.assertIn("RuntimeError: cannot write env", error)
        self.assertIn("configure_saved_without_ssh_authentication=true", error)
        self.assertIn("probe_ssh_port_reachable=true", error)
        self.assertIn("probe_ssh_authenticated=false", error)
        self.assertIn("probe_error=SSH authentication failed.", error)
        self.assertNotIn("badpw", error)

    def test_configure_telemetry_records_unsupported_device_reason(self) -> None:
        self.run_configure_cli(
            ensure_install_id=True,
            prompt_side_effect=self.configure_prompt_defaults(),
            probe_state=self.make_probe_state(self.make_probe_result_netbsd5()),
            raises=SystemExit,
        )

        error = self.configure_finished_error()
        self.assertIn("not supported", error)
        self.assertIn("stage=ssh_probe", error)
        self.assertIn("configure_failure_reason=unsupported_device", error)
        self.assertIn("probe_supported=false", error)
        self.assertIn("probe_reason_code=", error)

    def test_configure_telemetry_records_interface_candidates_and_exact_match_source(self) -> None:
        interface_probe = RemoteInterfaceCandidatesProbeResult(
            candidates=(
                RemoteInterfaceCandidate(name="bcmeth1", ipv4_addrs=("10.0.1.1",), up=True, active=True, loopback=False),
                RemoteInterfaceCandidate(name="bridge0", ipv4_addrs=("192.168.1.217",), up=True, active=True, loopback=False),
            ),
            preferred_iface="bridge0",
            detail="preferred interface bridge0",
        )

        self.run_configure_cli(
            ensure_install_id=True,
            prompt_side_effect=self.configure_prompt_defaults(host="root@10.0.1.1"),
            probe_state=self.make_probe_state(self.make_probe_result_netbsd6()),
            interface_probe=interface_probe,
            write_side_effect=RuntimeError("cannot write env"),
            raises=RuntimeError,
        )

        error = self.configure_finished_error()
        self.assertIn("RuntimeError: cannot write env", error)
        self.assertIn("interface_candidates=[", error)
        self.assertIn("name:bcmeth1", error)
        self.assertIn("ipv4:[10.0.1.1]", error)
        self.assertIn("name:bridge0", error)
        self.assertIn("selected_net_iface=bcmeth1", error)
        self.assertIn("selected_net_iface_source=target_ip_match", error)

    def test_configure_uses_discovered_host_when_available(self) -> None:
        record = Discovered(
            name="Time Capsule",
            hostname="capsule.local",
            service_type="_airport._tcp.local.",
            ipv4=["10.0.0.2"],
            ipv6=[],
        )

        prompt_values = iter([
            "pw",
            "bridge0",
                        "admin",
            "TimeCapsule",
            "samba4",
            "Time Capsule Samba 4",
            "timecapsulesamba4",
            "119",
        ])

        def fake_prompt(label, default, _secret):
            if label == "Device SSH target":
                return default
            if label == "mDNS device model hint":
                return default
            return next(prompt_values)

        result = self.run_configure_cli(
            discovered_records=[record],
            discovered_root_host="root@10.0.0.2",
            input_side_effect=["1"],
            prompt_side_effect=fake_prompt,
            probe_state=self.make_probe_state(self.make_probe_result_unreachable()),
            confirm=True,
        )
        self.assertEqual(result.rc, 0)
        self.assertEqual(result.values["TC_HOST"], "root@10.0.0.2")

    def test_configure_prefills_mdns_device_model_from_detected_device(self) -> None:
        prompt_values = iter([
            "rootpw",
            "bridge0",
                        "admin",
            "TimeCapsule",
            "samba4",
            "Time Capsule Samba 4",
            "timecapsulesamba4",
            "119",
        ])
        seen_defaults = {}

        def fake_prompt(label, default, _secret):
            seen_defaults[label] = default
            if label == "Device SSH target":
                return "root@10.0.0.2"
            if label == "mDNS device model hint":
                return default
            return next(prompt_values)

        result = self.run_configure_cli(
            prompt_side_effect=fake_prompt,
            probe_state=self.make_probe_state(self.make_probe_result_netbsd6()),
        )
        self.assertEqual(result.rc, 0)
        self.assertEqual(result.values["TC_AIRPORT_SYAP"], "119")
        self.assertNotIn("mDNS device model hint", seen_defaults)
        self.assertEqual(result.values["TC_MDNS_DEVICE_MODEL"], "TimeCapsule8,119")

    def test_configure_uses_target_ip_interface_default_instead_of_static_bridge0(self) -> None:
        seen_defaults = {}
        prompt_values = iter([
            "root@10.0.1.1",
            "rootpw",
            "Data",
            "admin",
            "TimeCapsule",
            "samba4",
            "Time Capsule Samba 4",
            "timecapsulesamba4",
        ])

        def fake_prompt(label, default, _secret):
            seen_defaults[label] = default
            if label == "Network interface on the device":
                return default
            if label in {"Airport Utility syAP code", "mDNS device model hint"}:
                raise AssertionError(f"{label} should be auto-filled for NetBSD 6 little-endian")
            return next(prompt_values)

        interface_probe = RemoteInterfaceCandidatesProbeResult(
            candidates=(
                RemoteInterfaceCandidate(name="bcmeth1", ipv4_addrs=("10.0.1.1",), up=True, active=True, loopback=False),
                RemoteInterfaceCandidate(name="bridge0", ipv4_addrs=(), up=True, active=False, loopback=False),
            ),
            preferred_iface="bcmeth1",
            detail="preferred interface bcmeth1",
        )

        result = self.run_configure_cli(
            prompt_side_effect=fake_prompt,
            probe_state=self.make_probe_state(self.make_probe_result_netbsd6()),
            interface_probe=interface_probe,
        )
        self.assertEqual(result.rc, 0)
        self.assertEqual(seen_defaults["Network interface on the device"], "bcmeth1")
        self.assertEqual(result.values["TC_NET_IFACE"], "bcmeth1")
        self.assertIn("Found network interfaces with non-link-local IPv4 on the device:", result.text)
        self.assertIn("bcmeth1: 10.0.1.1 (suggested)", result.text)
        self.assertIn("Using probed default for TC_NET_IFACE: bcmeth1", result.text)

    def test_configure_uses_discovered_ip_for_interface_default_when_host_is_name(self) -> None:
        seen_defaults = {}
        record = Discovered(
            name="AirPort Time Capsule",
            hostname="AirPort-Time-Capsule.local",
            ipv4=["10.0.1.1"],
            services={"_airport._tcp.local."},
            properties={"syAP": "119"},
        )
        prompt_values = iter([
            "rootpw",
            "Data",
            "admin",
            "TimeCapsule",
            "samba4",
            "Time Capsule Samba 4",
            "timecapsulesamba4",
        ])

        def fake_prompt(label, default, _secret):
            seen_defaults[label] = default
            if label in {"Device SSH target", "Network interface on the device"}:
                return default
            if label in {"Airport Utility syAP code", "mDNS device model hint"}:
                raise AssertionError(f"{label} should be auto-filled")
            return next(prompt_values)

        interface_probe = RemoteInterfaceCandidatesProbeResult(
            candidates=(
                RemoteInterfaceCandidate(name="bcmeth1", ipv4_addrs=("10.0.1.1",), up=True, active=True, loopback=False),
                RemoteInterfaceCandidate(name="bridge0", ipv4_addrs=("192.168.1.217",), up=True, active=True, loopback=False),
            ),
            preferred_iface="bridge0",
            detail="preferred interface bridge0",
        )

        result = self.run_configure_cli(
            discovered_records=[record],
            discovered_root_host="root@AirPort-Time-Capsule.local",
            input_side_effect=["1"],
            prompt_side_effect=fake_prompt,
            probe_state=self.make_probe_state(self.make_probe_result_netbsd6()),
            interface_probe=interface_probe,
        )
        self.assertEqual(result.rc, 0)
        self.assertEqual(seen_defaults["Network interface on the device"], "bcmeth1")
        self.assertEqual(result.values["TC_NET_IFACE"], "bcmeth1")
        self.assertIn("bcmeth1: 10.0.1.1 (suggested)", result.text)

    def test_configure_keeps_saved_interface_when_it_matches_probed_candidates(self) -> None:
        seen_defaults = {}
        existing = {"TC_NET_IFACE": "bcmeth1"}
        prompt_values = iter([
            "root@10.0.0.2",
            "rootpw",
            "Data",
            "admin",
            "TimeCapsule",
            "samba4",
            "Time Capsule Samba 4",
            "timecapsulesamba4",
        ])

        def fake_prompt(label, default, _secret):
            seen_defaults[label] = default
            if label == "Network interface on the device":
                return default
            if label in {"Airport Utility syAP code", "mDNS device model hint"}:
                raise AssertionError(f"{label} should be auto-filled for NetBSD 6 little-endian")
            return next(prompt_values)

        interface_probe = RemoteInterfaceCandidatesProbeResult(
            candidates=(
                RemoteInterfaceCandidate(name="bridge0", ipv4_addrs=("192.168.1.217",), up=True, active=True, loopback=False),
                RemoteInterfaceCandidate(name="bcmeth1", ipv4_addrs=("10.0.1.1",), up=True, active=True, loopback=False),
            ),
            preferred_iface="bridge0",
            detail="preferred interface bridge0",
        )

        result = self.run_configure_cli(
            existing_values=existing,
            prompt_side_effect=fake_prompt,
            probe_state=self.make_probe_state(self.make_probe_result_netbsd6()),
            interface_probe=interface_probe,
        )
        self.assertEqual(result.rc, 0)
        self.assertEqual(seen_defaults["Network interface on the device"], "bcmeth1")
        self.assertEqual(result.values["TC_NET_IFACE"], "bcmeth1")
        self.assertIn("Found saved value: bcmeth1", result.text)

    def test_configure_target_ip_match_overrides_conflicting_saved_interface(self) -> None:
        seen_defaults = {}
        existing = {"TC_NET_IFACE": "bridge0"}
        prompt_values = iter([
            "root@10.0.1.1",
            "rootpw",
            "Data",
            "admin",
            "TimeCapsule",
            "samba4",
            "Time Capsule Samba 4",
            "timecapsulesamba4",
        ])

        def fake_prompt(label, default, _secret):
            seen_defaults[label] = default
            if label == "Network interface on the device":
                return default
            if label in {"Airport Utility syAP code", "mDNS device model hint"}:
                raise AssertionError(f"{label} should be auto-filled for NetBSD 6 little-endian")
            return next(prompt_values)

        interface_probe = RemoteInterfaceCandidatesProbeResult(
            candidates=(
                RemoteInterfaceCandidate(name="bridge0", ipv4_addrs=("192.168.1.217",), up=True, active=True, loopback=False),
                RemoteInterfaceCandidate(name="bcmeth1", ipv4_addrs=("10.0.1.1",), up=True, active=True, loopback=False),
            ),
            preferred_iface="bridge0",
            detail="preferred interface bridge0",
        )

        result = self.run_configure_cli(
            existing_values=existing,
            prompt_side_effect=fake_prompt,
            probe_state=self.make_probe_state(self.make_probe_result_netbsd6()),
            interface_probe=interface_probe,
        )
        self.assertEqual(result.rc, 0)
        self.assertEqual(seen_defaults["Network interface on the device"], "bcmeth1")
        self.assertEqual(result.values["TC_NET_IFACE"], "bcmeth1")
        self.assertIn("bcmeth1: 10.0.1.1 (suggested)", result.text)
        self.assertIn("Found saved value: bridge0", result.text)
        self.assertIn("Probed target IP 10.0.1.1 is on bcmeth1, so bcmeth1 is suggested instead.", result.text)

    def test_configure_private_discovered_ip_beats_loopback_ssh_target(self) -> None:
        seen_defaults = {}
        record = Discovered(
            name="AirPort Time Capsule",
            hostname="AirPort-Time-Capsule.local",
            ipv4=["192.168.1.217"],
            services={"_airport._tcp.local."},
            properties={"syAP": "119"},
        )
        prompt_values = iter([
            "Data",
            "admin",
            "TimeCapsule",
            "samba4",
            "Time Capsule Samba 4",
            "timecapsulesamba4",
        ])

        def fake_prompt(label, default, _secret):
            seen_defaults[label] = default
            if label == "Device SSH target":
                return "root@127.0.0.1"
            if label == "Device root password":
                return "rootpw"
            if label == "Network interface on the device":
                return default
            if label in {"Airport Utility syAP code", "mDNS device model hint"}:
                raise AssertionError(f"{label} should be auto-filled")
            return next(prompt_values)

        interface_probe = RemoteInterfaceCandidatesProbeResult(
            candidates=(
                RemoteInterfaceCandidate(name="bcmeth1", ipv4_addrs=("169.254.44.9",), up=True, active=True, loopback=False),
                RemoteInterfaceCandidate(name="bridge0", ipv4_addrs=("192.168.1.217",), up=True, active=True, loopback=False),
            ),
            preferred_iface="bridge0",
            detail="preferred interface bridge0",
        )

        result = self.run_configure_cli(
            discovered_records=[record],
            discovered_root_host="root@AirPort-Time-Capsule.local",
            input_side_effect=["1"],
            prompt_side_effect=fake_prompt,
            probe_state=self.make_probe_state(self.make_probe_result_netbsd6()),
            interface_probe=interface_probe,
        )
        self.assertEqual(result.rc, 0)
        self.assertEqual(seen_defaults["Network interface on the device"], "bridge0")
        self.assertEqual(result.values["TC_NET_IFACE"], "bridge0")
        self.assertIn("bridge0: 192.168.1.217 (suggested)", result.text)

    def test_configure_loopback_target_ip_does_not_win_runtime_interface(self) -> None:
        seen_defaults = {}
        prompt_values = iter([
            "root@127.0.0.1",
            "rootpw",
            "Data",
            "admin",
            "TimeCapsule",
            "samba4",
            "Time Capsule Samba 4",
            "timecapsulesamba4",
        ])

        def fake_prompt(label, default, _secret):
            seen_defaults[label] = default
            if label == "Network interface on the device":
                return default
            if label in {"Airport Utility syAP code", "mDNS device model hint"}:
                raise AssertionError(f"{label} should be auto-filled")
            return next(prompt_values)

        interface_probe = RemoteInterfaceCandidatesProbeResult(
            candidates=(
                RemoteInterfaceCandidate(name="bcmeth1", ipv4_addrs=("169.254.44.9",), up=True, active=True, loopback=False),
                RemoteInterfaceCandidate(name="bridge0", ipv4_addrs=("192.168.1.217",), up=True, active=True, loopback=False),
            ),
            preferred_iface="bridge0",
            detail="preferred interface bridge0",
        )

        result = self.run_configure_cli(
            prompt_side_effect=fake_prompt,
            probe_state=self.make_probe_state(self.make_probe_result_netbsd6()),
            interface_probe=interface_probe,
        )
        self.assertEqual(result.rc, 0)
        self.assertEqual(seen_defaults["Network interface on the device"], "bridge0")
        self.assertEqual(result.values["TC_NET_IFACE"], "bridge0")
        self.assertIn("bridge0: 192.168.1.217 (suggested)", result.text)
        self.assertNotIn("bcmeth1: 169.254.44.9 (suggested)", result.text)

    def test_configure_reprompts_link_local_ssh_target(self) -> None:
        prompt_values = iter([
            "root@169.254.44.9",
            "root@10.0.0.2",
            "rootpw",
            "Data",
            "admin",
            "TimeCapsule",
            "samba4",
            "Time Capsule Samba 4",
            "timecapsulesamba4",
        ])

        def fake_prompt(label, default, _secret):
            if label == "Network interface on the device":
                return default
            if label in {"Airport Utility syAP code", "mDNS device model hint"}:
                raise AssertionError(f"{label} should be auto-filled")
            return next(prompt_values)

        interface_probe = RemoteInterfaceCandidatesProbeResult(
            candidates=(
                RemoteInterfaceCandidate(name="bridge0", ipv4_addrs=("10.0.0.2",), up=True, active=True, loopback=False),
            ),
            preferred_iface="bridge0",
            detail="preferred interface bridge0",
        )

        result = self.run_configure_cli(
            prompt_side_effect=fake_prompt,
            probe_state=self.make_probe_state(self.make_probe_result_netbsd6()),
            interface_probe=interface_probe,
        )
        self.assertEqual(result.rc, 0)
        self.assertEqual(result.values["TC_HOST"], "root@10.0.0.2")
        self.assertIn("Device SSH target host must not be a 169.254.x.x link-local address", result.text)

    def test_configure_reprompts_hostname_that_resolves_link_local(self) -> None:
        prompt_values = iter([
            "root@capsule.local",
            "root@10.0.0.2",
            "rootpw",
            "Data",
            "admin",
            "TimeCapsule",
            "samba4",
            "Time Capsule Samba 4",
            "timecapsulesamba4",
        ])

        def fake_prompt(label, default, _secret):
            if label == "Network interface on the device":
                return default
            if label in {"Airport Utility syAP code", "mDNS device model hint"}:
                raise AssertionError(f"{label} should be auto-filled")
            return next(prompt_values)

        interface_probe = RemoteInterfaceCandidatesProbeResult(
            candidates=(
                RemoteInterfaceCandidate(name="bridge0", ipv4_addrs=("10.0.0.2",), up=True, active=True, loopback=False),
            ),
            preferred_iface="bridge0",
            detail="preferred interface bridge0",
        )
        addrinfo = [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("169.254.44.9", 0))]

        result = self.run_configure_cli(
            prompt_side_effect=fake_prompt,
            probe_state=self.make_probe_state(self.make_probe_result_netbsd6()),
            interface_probe=interface_probe,
            extra_patches={"timecapsulesmb.core.net.socket.getaddrinfo": mock.Mock(return_value=addrinfo)},
        )
        self.assertEqual(result.rc, 0)
        self.assertEqual(result.values["TC_HOST"], "root@10.0.0.2")
        self.assertIn("capsule.local resolves to 169.254.x.x link-local IPv4 address 169.254.44.9", result.text)

    def test_configure_multiple_private_interfaces_without_exact_match_prints_candidates_and_prompts(self) -> None:
        seen_defaults = {}
        prompt_values = iter([
            "root@time-capsule.local",
            "rootpw",
            "Data",
            "admin",
            "TimeCapsule",
            "samba4",
            "Time Capsule Samba 4",
            "timecapsulesamba4",
        ])

        def fake_prompt(label, default, _secret):
            seen_defaults[label] = default
            if label == "Network interface on the device":
                return default
            if label in {"Airport Utility syAP code", "mDNS device model hint"}:
                raise AssertionError(f"{label} should be auto-filled")
            return next(prompt_values)

        interface_probe = RemoteInterfaceCandidatesProbeResult(
            candidates=(
                RemoteInterfaceCandidate(name="bcmeth1", ipv4_addrs=("10.0.1.1",), up=True, active=True, loopback=False),
                RemoteInterfaceCandidate(name="bridge0", ipv4_addrs=("192.168.1.217",), up=True, active=True, loopback=False),
            ),
            preferred_iface="bridge0",
            detail="preferred interface bridge0",
        )

        result = self.run_configure_cli(
            prompt_side_effect=fake_prompt,
            probe_state=self.make_probe_state(self.make_probe_result_netbsd6()),
            interface_probe=interface_probe,
        )
        self.assertEqual(result.rc, 0)
        self.assertEqual(seen_defaults["Network interface on the device"], "bridge0")
        self.assertEqual(result.values["TC_NET_IFACE"], "bridge0")
        text = result.text
        self.assertIn("Found network interfaces with non-link-local IPv4 on the device:", text)
        self.assertIn("bcmeth1: 10.0.1.1", text)
        self.assertIn("bridge0: 192.168.1.217 (suggested)", text)

    def test_configure_uses_ssh_target_ip_before_discovered_ip_for_interface_default(self) -> None:
        seen_defaults = {}
        record = Discovered(
            name="AirPort Time Capsule",
            hostname="AirPort-Time-Capsule.local",
            ipv4=["192.168.1.217"],
            services={"_airport._tcp.local."},
            properties={"syAP": "119"},
        )
        prompt_values = iter([
            "Data",
            "admin",
            "TimeCapsule",
            "samba4",
            "Time Capsule Samba 4",
            "timecapsulesamba4",
        ])

        def fake_prompt(label, default, _secret):
            seen_defaults[label] = default
            if label == "Device SSH target":
                return "root@10.0.1.1"
            if label == "Device root password":
                return "rootpw"
            if label == "Network interface on the device":
                return default
            if label in {"Airport Utility syAP code", "mDNS device model hint"}:
                raise AssertionError(f"{label} should be auto-filled")
            return next(prompt_values)

        interface_probe = RemoteInterfaceCandidatesProbeResult(
            candidates=(
                RemoteInterfaceCandidate(name="bcmeth1", ipv4_addrs=("10.0.1.1",), up=True, active=True, loopback=False),
                RemoteInterfaceCandidate(name="bridge0", ipv4_addrs=("192.168.1.217",), up=True, active=True, loopback=False),
            ),
            preferred_iface="bridge0",
            detail="preferred interface bridge0",
        )

        result = self.run_configure_cli(
            discovered_records=[record],
            discovered_root_host="root@AirPort-Time-Capsule.local",
            input_side_effect=["1"],
            prompt_side_effect=fake_prompt,
            probe_state=self.make_probe_state(self.make_probe_result_netbsd6()),
            interface_probe=interface_probe,
        )
        self.assertEqual(result.rc, 0)
        self.assertEqual(seen_defaults["Network interface on the device"], "bcmeth1")
        self.assertEqual(result.values["TC_NET_IFACE"], "bcmeth1")
        self.assertIn("bcmeth1: 10.0.1.1 (suggested)", result.text)

    def test_configure_fails_when_probe_has_no_runtime_usable_ipv4_candidates(self) -> None:
        seen_defaults = {}
        prompt_values = iter([
            "root@10.0.0.2",
            "rootpw",
        ])

        def fake_prompt(label, default, _secret):
            seen_defaults[label] = default
            if label == "Network interface on the device":
                return default
            if label in {"Airport Utility syAP code", "mDNS device model hint"}:
                raise AssertionError(f"{label} should be auto-filled")
            return next(prompt_values)

        interface_probe = RemoteInterfaceCandidatesProbeResult(
            candidates=(
                RemoteInterfaceCandidate(name="lo0", ipv4_addrs=("127.0.0.1",), up=True, active=True, loopback=True),
                RemoteInterfaceCandidate(
                    name="bridge0",
                    ipv4_addrs=("0.0.0.0", "169.254.44.9"),
                    up=True,
                    active=True,
                    loopback=False,
                ),
            ),
            preferred_iface=None,
            detail="no non-loopback IPv4 interface candidates found",
        )

        result = self.run_configure_cli(
            prompt_side_effect=fake_prompt,
            probe_state=self.make_probe_state(self.make_probe_result_netbsd6()),
            interface_probe=interface_probe,
        )
        self.assertEqual(result.rc, 1)
        self.assertNotIn("Network interface on the device", seen_defaults)
        self.assertEqual(result.values, {})
        self.assertIn("No usable runtime network interface was found", result.text)
        self.assertIn("169.254.x.x self-assigned addresses are only suitable for temporary SSH recovery", result.text)

    def test_configure_skipped_mdns_netbsd6_little_autofills_syap_and_model(self) -> None:
        record = Discovered(
            name="AirPort Time Capsule",
            hostname="AirPort-Time-Capsule.local",
            ipv4=["192.168.1.72"],
            services={"_airport._tcp.local."},
            properties={"syAP": "106"},
        )
        prompt_values = iter([
            "root@10.0.0.2",
            "rootpw",
            "bridge0",
                        "admin",
            "TimeCapsule",
            "samba4",
            "Time Capsule Samba 4",
            "timecapsulesamba4",
        ])

        def fake_prompt(label, default, _secret):
            if label == "Airport Utility syAP code":
                raise AssertionError("NetBSD6 little-endian should autofill syAP")
            if label == "mDNS device model hint":
                raise AssertionError("NetBSD6 little-endian should autofill mDNS model")
            return next(prompt_values)

        result = self.run_configure_cli(
            discovered_records=[record],
            input_side_effect=["q"],
            prompt_side_effect=fake_prompt,
            probe_state=self.make_probe_state(self.make_probe_result_netbsd6()),
        )
        self.assertEqual(result.rc, 0)
        self.assertEqual(result.values["TC_AIRPORT_SYAP"], "119")
        self.assertEqual(result.values["TC_MDNS_DEVICE_MODEL"], "TimeCapsule8,119")
        text = result.text
        self.assertIn("Discovery skipped.", text)
        self.assertIn("Using probed TC_AIRPORT_SYAP: 119", text)
        self.assertIn("Using probed TC_MDNS_DEVICE_MODEL: TimeCapsule8,119", text)

    def test_configure_fails_when_probe_returns_unsupported_device(self) -> None:
        prompt_values = iter([
            "rootpw",
        ])

        def fake_prompt(label, default, _secret):
            if label == "Device SSH target":
                return "root@10.0.0.2"
            return next(prompt_values)

        result = self.run_configure_cli(
            prompt_side_effect=fake_prompt,
            probe_state=self.make_probe_state(self.make_probe_result_netbsd6_unknown()),
            raises=SystemExit,
        )
        self.assertIn("unknown-endian", str(result.exception))
        self.assertNotIn("Using probed TC_AIRPORT_SYAP: 119", result.text)

    def test_configure_skipped_mdns_netbsd6_big_fails_fast(self) -> None:
        record = Discovered(
            name="AirPort Time Capsule",
            hostname="AirPort-Time-Capsule.local",
            ipv4=["192.168.1.72"],
            services={"_airport._tcp.local."},
            properties={"syAP": "106"},
        )
        prompt_values = iter([
            "root@10.0.0.2",
            "rootpw",
        ])

        def fake_prompt(_label, _default, _secret):
            return next(prompt_values)

        result = self.run_configure_cli(
            discovered_records=[record],
            input_side_effect=["q"],
            prompt_side_effect=fake_prompt,
            probe_state=self.make_probe_state(self.make_probe_result_netbsd6_big()),
            raises=SystemExit,
        )
        self.assertIn("big-endian", str(result.exception))
        self.assertNotIn("Using probed TC_AIRPORT_SYAP", result.text)

    def test_configure_skipped_mdns_netbsd6_unknown_fails_fast(self) -> None:
        record = Discovered(
            name="AirPort Time Capsule",
            hostname="AirPort-Time-Capsule.local",
            ipv4=["192.168.1.72"],
            services={"_airport._tcp.local."},
            properties={"syAP": "106"},
        )
        prompt_values = iter([
            "root@10.0.0.2",
            "rootpw",
        ])

        def fake_prompt(_label, _default, _secret):
            return next(prompt_values)

        result = self.run_configure_cli(
            discovered_records=[record],
            input_side_effect=["q"],
            prompt_side_effect=fake_prompt,
            probe_state=self.make_probe_state(self.make_probe_result_netbsd6_unknown()),
            raises=SystemExit,
        )
        self.assertIn("unknown-endian", str(result.exception))
        self.assertNotIn("Using probed TC_AIRPORT_SYAP", result.text)

    def test_configure_skipped_mdns_netbsd_other_fails_fast(self) -> None:
        record = Discovered(
            name="AirPort Time Capsule",
            hostname="AirPort-Time-Capsule.local",
            ipv4=["192.168.1.72"],
            services={"_airport._tcp.local."},
            properties={"syAP": "106"},
        )
        prompt_values = iter([
            "root@10.0.0.2",
            "rootpw",
        ])

        def fake_prompt(_label, _default, _secret):
            return next(prompt_values)

        result = self.run_configure_cli(
            discovered_records=[record],
            input_side_effect=["q"],
            prompt_side_effect=fake_prompt,
            probe_state=self.make_probe_state(self.make_probe_result_netbsd5()),
            raises=SystemExit,
        )
        self.assertIn("NetBSD 5.0", str(result.exception))
        self.assertNotIn("Using probed TC_AIRPORT_SYAP", result.text)

    def test_configure_skipped_mdns_netbsd4le_shows_syap_table_and_restricts_candidates(self) -> None:
        record = Discovered(
            name="AirPort Time Capsule",
            hostname="AirPort-Time-Capsule.local",
            ipv4=["192.168.1.72"],
            services={"_airport._tcp.local."},
            properties={"syAP": "106"},
        )
        prompt_values = iter([
            "root@10.0.0.2",
            "rootpw",
            "bridge0",
                        "admin",
            "TimeCapsule",
            "samba4",
            "Time Capsule Samba 4",
            "timecapsulesamba4",
            "119",
            "113",
        ])

        def fake_prompt(label, default, _secret):
            if label == "mDNS device model hint":
                raise AssertionError("mDNS device model should be derived from the final syAP")
            return next(prompt_values)

        result = self.run_configure_cli(
            discovered_records=[record],
            input_side_effect=["q"],
            prompt_side_effect=fake_prompt,
            probe_state=self.make_probe_state(self.make_probe_result_netbsd4le()),
        )
        self.assertEqual(result.rc, 0)
        self.assertEqual(result.values["TC_AIRPORT_SYAP"], "113")
        self.assertEqual(result.values["TC_MDNS_DEVICE_MODEL"], "TimeCapsule6,113")
        text = result.text
        self.assertIn("Device                           Model identifier    syAP", text)
        self.assertIn("AirPort Extreme 3rd generation   AirPort5,108        108", text)
        self.assertIn("Time Capsule 3rd generation      TimeCapsule6,113    113", text)
        self.assertIn("AirPort Extreme 4th generation   AirPort5,114        114", text)
        self.assertIn("Time Capsule 4th generation      TimeCapsule6,116    116", text)
        self.assertIn("AirPort Extreme 5th generation   AirPort5,117        117", text)
        self.assertIn("From detected connection, syAP code should be one of: 108, 113, 114, 116, 117", text)

    def test_configure_probed_netbsd4be_shows_syap_table_and_restricts_candidates(self) -> None:
        syap_defaults: list[str] = []
        record = Discovered(
            name="AirPort Time Capsule",
            hostname="AirPort-Time-Capsule.local",
            ipv4=["192.168.1.72"],
            services={"_airport._tcp.local."},
            properties={"syAP": "113"},
        )
        prompt_values = iter([
            "root@10.0.0.2",
            "rootpw",
            "bridge0",
            "admin",
            "samba4",
            "119",
            "106",
        ])

        def fake_prompt(label, default, _secret):
            if label == "Airport Utility syAP code":
                syap_defaults.append(default)
            if label == "mDNS device model hint":
                raise AssertionError("mDNS device model should be derived from the final syAP")
            return next(prompt_values)

        result = self.run_configure_cli(
            discovered_records=[record],
            input_side_effect=["q"],
            prompt_side_effect=fake_prompt,
            probe_state=self.make_probe_state(self.make_probe_result_netbsd4be()),
        )
        self.assertEqual(result.rc, 0)
        self.assertEqual(syap_defaults, ["", ""])
        self.assertEqual(result.values["TC_AIRPORT_SYAP"], "106")
        self.assertEqual(result.values["TC_MDNS_DEVICE_MODEL"], "TimeCapsule6,106")
        text = result.text
        self.assertIn("Device                           Model identifier    syAP", text)
        self.assertIn("AirPort Extreme 1st generation   AirPort5,104        104", text)
        self.assertIn("AirPort Extreme 2nd generation   AirPort5,105        105", text)
        self.assertIn("Time Capsule 1st generation      TimeCapsule6,106    106", text)
        self.assertIn("Time Capsule 2nd generation      TimeCapsule6,109    109", text)
        self.assertIn("From detected connection, syAP code should be one of: 104, 105, 106, 109", text)

    def test_configure_probed_netbsd4be_airport_identity_identity_autofills_generation(self) -> None:
        prompt_values = iter([
            "root@10.0.0.2",
            "rootpw",
            "bridge0",
                        "admin",
            "TimeCapsule",
            "samba4",
            "Time Capsule Samba 4",
            "timecapsulesamba4",
        ])

        def fake_prompt(label, _default, _secret):
            if label in {"Airport Utility syAP code", "mDNS device model hint"}:
                raise AssertionError(f"{label} should be autofilled from AirPort identity")
            return next(prompt_values)

        result = self.run_configure_cli(
            prompt_side_effect=fake_prompt,
            probe_state=self.make_probe_state(self.make_probe_result_netbsd4be_airport_identity_106()),
        )
        self.assertEqual(result.rc, 0)
        self.assertEqual(result.values["TC_AIRPORT_SYAP"], "106")
        self.assertEqual(result.values["TC_MDNS_DEVICE_MODEL"], "TimeCapsule6,106")
        self.assertIn("Using probed TC_AIRPORT_SYAP: 106", result.text)
        self.assertIn("Using probed TC_MDNS_DEVICE_MODEL: TimeCapsule6,106", result.text)

    def test_configure_probed_netbsd4le_airport_identity_identity_autofills_generation(self) -> None:
        prompt_values = iter([
            "root@10.0.0.2",
            "rootpw",
            "bridge0",
                        "admin",
            "TimeCapsule",
            "samba4",
            "Time Capsule Samba 4",
            "timecapsulesamba4",
        ])

        def fake_prompt(label, _default, _secret):
            if label in {"Airport Utility syAP code", "mDNS device model hint"}:
                raise AssertionError(f"{label} should be autofilled from AirPort identity")
            return next(prompt_values)

        result = self.run_configure_cli(
            prompt_side_effect=fake_prompt,
            probe_state=self.make_probe_state(self.make_probe_result_netbsd4le_airport_identity_113()),
        )
        self.assertEqual(result.rc, 0)
        self.assertEqual(result.values["TC_AIRPORT_SYAP"], "113")
        self.assertEqual(result.values["TC_MDNS_DEVICE_MODEL"], "TimeCapsule6,113")
        self.assertIn("Using probed TC_AIRPORT_SYAP: 113", result.text)
        self.assertIn("Using probed TC_MDNS_DEVICE_MODEL: TimeCapsule6,113", result.text)

    def test_configure_saves_airport_syap_from_discovery_without_prompting(self) -> None:
        record = Discovered(
            name="Time Capsule Samba 4",
            hostname="timecapsulesamba4.local",
            ipv4=["192.168.1.217"],
            services={"_airport._tcp.local.", "_smb._tcp.local."},
            properties={"syAP": "119"},
        )
        prompt_values = iter([
            "rootpw",
            "bridge0",
                        "admin",
            "TimeCapsule",
            "samba4",
            "Time Capsule Samba 4",
            "timecapsulesamba4",
        ])

        def fake_prompt(label, default, _secret):
            if label == "Device SSH target":
                return default
            if label == "mDNS device model hint":
                return default
            return next(prompt_values)

        result = self.run_configure_cli(
            discovered_records=[record],
            input_side_effect=["1"],
            prompt_side_effect=fake_prompt,
            probe_state=self.make_probe_state(self.make_probe_result_unreachable()),
            confirm=True,
        )
        self.assertEqual(result.rc, 0)
        self.assertEqual(result.values["TC_AIRPORT_SYAP"], "119")
        self.assertEqual(result.values["TC_MDNS_DEVICE_MODEL"], "TimeCapsule8,119")
        self.assertIn("Using discovered TC_AIRPORT_SYAP: 119", result.text)

    def test_configure_discovered_syap_beats_invalid_existing_syap(self) -> None:
        seen_labels: list[str] = []
        record = Discovered(
            name="Time Capsule Samba 4",
            hostname="timecapsulesamba4.local",
            ipv4=["192.168.1.217"],
            services={"_airport._tcp.local.", "_smb._tcp.local."},
            properties={"syAP": "119"},
        )
        prompt_values = iter([
            "rootpw",
            "bridge0",
                        "admin",
            "TimeCapsule",
            "samba4",
            "Time Capsule Samba 4",
            "timecapsulesamba4",
        ])

        def fake_prompt(label, default, _secret):
            seen_labels.append(label)
            if label == "Device SSH target":
                return default
            return next(prompt_values)

        result = self.run_configure_cli(
            existing_values={"TC_AIRPORT_SYAP": "999"},
            discovered_records=[record],
            input_side_effect=["1"],
            prompt_side_effect=fake_prompt,
            probe_state=self.make_probe_state(self.make_probe_result_unreachable()),
            confirm=True,
        )
        self.assertEqual(result.rc, 0)
        self.assertEqual(result.values["TC_AIRPORT_SYAP"], "119")
        self.assertEqual(result.values["TC_MDNS_DEVICE_MODEL"], "TimeCapsule8,119")
        self.assertNotIn("Airport Utility syAP code", seen_labels)
        self.assertNotIn("mDNS device model hint", seen_labels)

    def test_configure_discovered_missing_syap_uses_probed_syap_after_acp(self) -> None:
        seen_defaults = {}
        record = Discovered(
            name="Time Capsule Samba 4",
            hostname="timecapsulesamba4.local",
            ipv4=["192.168.1.217"],
            services={"_airport._tcp.local.", "_smb._tcp.local."},
            properties={},
        )
        prompt_values = iter([
            "rootpw",
            "bridge0",
                        "admin",
            "TimeCapsule",
            "samba4",
            "Time Capsule Samba 4",
            "timecapsulesamba4",
        ])

        def fake_prompt(label, default, _secret):
            seen_defaults[label] = default
            if label == "Device SSH target":
                return default
            if label == "Airport Utility syAP code":
                return default
            return next(prompt_values)

        result = self.run_configure_cli(
            existing_values={"TC_AIRPORT_SYAP": "116"},
            discovered_records=[record],
            input_side_effect=["1"],
            prompt_side_effect=fake_prompt,
            probe_state=self.make_probe_state(self.make_probe_result_unreachable()),
            confirm=True,
        )
        self.assertEqual(result.rc, 0)
        self.assertNotIn("Airport Utility syAP code", seen_defaults)
        self.assertEqual(result.values["TC_AIRPORT_SYAP"], "119")
        self.assertEqual(result.values["TC_MDNS_DEVICE_MODEL"], "TimeCapsule8,119")
        self.assertNotIn("mDNS device model hint", seen_defaults)
        self.assertIn("Using probed TC_AIRPORT_SYAP: 119", result.text)

    def test_configure_selected_smb_record_without_airport_syap_uses_probe_before_saved_syap(self) -> None:
        seen_labels: list[str] = []
        record = Discovered(
            name="Time Capsule Samba 4",
            hostname="timecapsulesamba4.local",
            ipv4=["192.168.1.217"],
            services={"_smb._tcp.local."},
            properties={},
        )
        prompt_values = iter([
            "rootpw",
            "bridge0",
                        "admin",
            "TimeCapsule",
            "samba4",
            "Time Capsule Samba 4",
            "timecapsulesamba4",
        ])

        def fake_prompt(label, default, _secret):
            seen_labels.append(label)
            if label == "Device SSH target":
                return default
            if label in {"Airport Utility syAP code", "mDNS device model hint"}:
                raise AssertionError(f"{label} should be auto-filled from probe")
            return next(prompt_values)

        result = self.run_configure_cli(
            existing_values={"TC_AIRPORT_SYAP": "113"},
            discovered_records=[record],
            input_side_effect=["1"],
            prompt_side_effect=fake_prompt,
            probe_state=self.make_probe_state(self.make_probe_result_netbsd6()),
        )
        self.assertEqual(result.rc, 0)
        self.assertEqual(result.values["TC_AIRPORT_SYAP"], "119")
        self.assertEqual(result.values["TC_MDNS_DEVICE_MODEL"], "TimeCapsule8,119")
        self.assertNotIn("Airport Utility syAP code", seen_labels)
        self.assertIn("Using probed TC_AIRPORT_SYAP: 119", result.text)

    def test_configure_rejects_saved_syap_outside_probed_candidates(self) -> None:
        syap_answers = iter(["113", "120"])

        def fake_prompt(label, default, _secret):
            if label == "Device root password":
                return "rootpw"
            if label == "Airport Utility syAP code":
                return next(syap_answers)
            if label == "mDNS device model hint":
                raise AssertionError("mDNS device model should be derived from the final syAP")
            return default

        interface_probe = RemoteInterfaceCandidatesProbeResult(
            candidates=(
                RemoteInterfaceCandidate(name="bridge0", ipv4_addrs=("192.168.1.217",), up=True, active=True, loopback=False),
            ),
            preferred_iface="bridge0",
            detail="preferred interface bridge0",
        )

        result = self.run_configure_cli(
            existing_values={"TC_AIRPORT_SYAP": "113"},
            prompt_side_effect=fake_prompt,
            probe_state=self.make_probe_state(self.make_probe_result_netbsd6_no_identity()),
            interface_probe=interface_probe,
        )

        self.assertEqual(result.rc, 0)
        self.assertEqual(result.values["TC_AIRPORT_SYAP"], "120")
        self.assertEqual(result.values["TC_MDNS_DEVICE_MODEL"], "AirPort7,120")
        text = result.text
        self.assertIn("Found saved value: 113", text)
        self.assertIn("From detected connection, syAP code should be one of: 119, 120", text)

    def test_configure_discovered_invalid_syap_uses_probed_syap_after_acp(self) -> None:
        seen_defaults = {}
        record = Discovered(
            name="Time Capsule Samba 4",
            hostname="timecapsulesamba4.local",
            ipv4=["192.168.1.217"],
            services={"_airport._tcp.local.", "_smb._tcp.local."},
            properties={"syAP": "999"},
        )
        prompt_values = iter([
            "rootpw",
            "bridge0",
                        "admin",
            "TimeCapsule",
            "samba4",
            "Time Capsule Samba 4",
            "timecapsulesamba4",
        ])

        def fake_prompt(label, default, _secret):
            seen_defaults[label] = default
            if label == "Device SSH target":
                return default
            if label == "Airport Utility syAP code":
                return default
            return next(prompt_values)

        result = self.run_configure_cli(
            existing_values={"TC_AIRPORT_SYAP": "109"},
            discovered_records=[record],
            input_side_effect=["1"],
            prompt_side_effect=fake_prompt,
            probe_state=self.make_probe_state(self.make_probe_result_unreachable()),
            confirm=True,
        )
        self.assertEqual(result.rc, 0)
        self.assertNotIn("Airport Utility syAP code", seen_defaults)
        self.assertEqual(result.values["TC_AIRPORT_SYAP"], "119")
        self.assertEqual(result.values["TC_MDNS_DEVICE_MODEL"], "TimeCapsule8,119")
        self.assertNotIn("mDNS device model hint", seen_defaults)
        self.assertIn("Using probed TC_AIRPORT_SYAP: 119", result.text)

    def test_configure_discovered_invalid_syap_reprompts_until_valid_when_existing_syap_invalid(self) -> None:
        syap_defaults: list[str] = []
        syap_attempts = iter(["999", "113"])
        record = Discovered(
            name="Time Capsule Samba 4",
            hostname="timecapsulesamba4.local",
            ipv4=["192.168.1.217"],
            services={"_airport._tcp.local.", "_smb._tcp.local."},
            properties={"syAP": "bad"},
        )
        prompt_values = iter([
            "rootpw",
            "bridge0",
                        "admin",
            "TimeCapsule",
            "samba4",
            "Time Capsule Samba 4",
            "timecapsulesamba4",
        ])

        def fake_prompt(label, default, _secret):
            if label == "Device SSH target":
                return default
            if label == "Airport Utility syAP code":
                syap_defaults.append(default)
                return next(syap_attempts)
            if label == "mDNS device model hint":
                raise AssertionError("mDNS device model should be derived from the final syAP")
            return next(prompt_values)

        self._configure_acp_probe_mock.side_effect = [self.make_probe_state(self.make_probe_result_netbsd4le())]
        result = self.run_configure_cli(
            existing_values={"TC_AIRPORT_SYAP": "998"},
            discovered_records=[record],
            input_side_effect=["1"],
            prompt_side_effect=fake_prompt,
            probe_state=self.make_probe_state(self.make_probe_result_unreachable()),
            confirm=True,
        )
        self.assertEqual(result.rc, 0)
        self.assertEqual(syap_defaults, ["", ""])
        self.assertEqual(result.values["TC_AIRPORT_SYAP"], "113")
        self.assertEqual(result.values["TC_MDNS_DEVICE_MODEL"], "TimeCapsule6,113")
        self.assertIn("The configured syAP is invalid.", result.text)

    def test_configure_can_skip_single_discovered_device(self) -> None:
        record = Discovered(
            name="Time Capsule Samba 4",
            hostname="timecapsulesamba4.local",
            ipv4=["192.168.1.217"],
            services={"_airport._tcp.local.", "_smb._tcp.local."},
            properties={"syAP": "119"},
        )
        prompt_values = iter([
            "rootpw",
            "bridge0",
                        "admin",
            "TimeCapsule",
            "samba4",
            "Time Capsule Samba 4",
            "timecapsulesamba4",
            "119",
        ])

        def fake_prompt(label, default, _secret):
            if label == "Device SSH target":
                return default
            if label == "mDNS device model hint":
                return default
            return next(prompt_values)

        result = self.run_configure_cli(
            discovered_records=[record],
            input_side_effect=["q"],
            prompt_side_effect=fake_prompt,
            probe_state=self.make_probe_state(self.make_probe_result_unreachable()),
            confirm=True,
        )
        self.assertEqual(result.rc, 0)
        self.assertEqual(result.values["TC_HOST"], DEFAULTS["TC_HOST"])
        self.assertEqual(result.values["TC_AIRPORT_SYAP"], "119")
        self.assertIn("Found devices:", result.text)
        self.assertIn(f"Discovery skipped. Falling back to {DEFAULTS['TC_HOST']}.", result.text)

    def test_configure_does_not_default_to_discovered_link_local_ipv4(self) -> None:
        seen_defaults = {}
        record = Discovered(
            name="Time Capsule Samba 4",
            hostname="timecapsulesamba4.local",
            ipv4=["169.254.44.9"],
            services={"_airport._tcp.local."},
            properties={"syAP": "119"},
        )
        prompt_values = iter([
            "root@10.0.0.2",
            "rootpw",
            "Data",
            "admin",
            "TimeCapsule",
            "samba4",
            "Time Capsule Samba 4",
            "timecapsulesamba4",
        ])

        def fake_prompt(label, default, _secret):
            seen_defaults[label] = default
            if label == "Network interface on the device":
                return default
            if label in {"Airport Utility syAP code", "mDNS device model hint"}:
                raise AssertionError(f"{label} should be auto-filled")
            return next(prompt_values)

        interface_probe = RemoteInterfaceCandidatesProbeResult(
            candidates=(
                RemoteInterfaceCandidate(name="bridge0", ipv4_addrs=("10.0.0.2",), up=True, active=True, loopback=False),
            ),
            preferred_iface="bridge0",
            detail="preferred interface bridge0",
        )

        result = self.run_configure_cli(
            discovered_records=[record],
            input_side_effect=["1"],
            prompt_side_effect=fake_prompt,
            probe_state=self.make_probe_state(self.make_probe_result_netbsd6()),
            interface_probe=interface_probe,
        )
        self.assertEqual(result.rc, 0)
        self.assertEqual(seen_defaults["Device SSH target"], DEFAULTS["TC_HOST"])
        self.assertEqual(result.values["TC_HOST"], "root@10.0.0.2")
        self.assertIn("Selected device only advertised 169.254.x.x link-local IPv4", result.text)
        self.assertNotIn("host: 169.254.44.9", result.text)

    def test_configure_ctrl_c_during_discovery_selection_cancels(self) -> None:
        record = Discovered(
            name="Time Capsule Samba 4",
            hostname="timecapsulesamba4.local",
            ipv4=["192.168.1.217"],
            services={"_airport._tcp.local."},
            properties={"syAP": "119"},
        )
        command_context = FakeCommandContext()

        result = self.run_configure_cli(
            discovered_records=[record],
            input_side_effect=KeyboardInterrupt,
            command_context=command_context,
            raises=KeyboardInterrupt,
        )
        self.assertIn("Found devices:", result.text)
        self.assertNotIn("Discovery skipped.", result.text)
        command_context.finish.assert_called_once()
        self.assertEqual(command_context.finish.call_args.kwargs["result"], "cancelled")
        self.assertEqual(command_context.finish.call_args.kwargs["error"], "Cancelled by user")

    def test_configure_skipped_discovery_reprompts_invalid_existing_syap(self) -> None:
        syap_defaults: list[str] = []
        syap_attempts = iter(["999", "116"])
        prompt_values = iter([
            "root@10.0.0.2",
            "goodpw",
            "bridge0",
                        "admin",
            "TimeCapsule",
            "samba4",
            "Time Capsule Samba 4",
            "timecapsulesamba4",
        ])

        def fake_prompt(label, default, _secret):
            if label == "Airport Utility syAP code":
                syap_defaults.append(default)
                return next(syap_attempts)
            if label == "mDNS device model hint":
                raise AssertionError("mDNS device model should be derived from the final syAP")
            return next(prompt_values)

        self.force_configure_acp_reprobe_auth_failed()
        result = self.run_configure_cli(
            existing_values={"TC_AIRPORT_SYAP": "999"},
            prompt_side_effect=fake_prompt,
            probe_state=self.make_probe_state(self.make_probe_result_unreachable()),
            confirm=True,
        )
        self.assertEqual(result.rc, 0)
        self.assertEqual(syap_defaults, ["", ""])
        self.assertEqual(result.values["TC_AIRPORT_SYAP"], "116")
        self.assertEqual(result.values["TC_MDNS_DEVICE_MODEL"], "TimeCapsule6,116")
        self.assertIn("The configured syAP is invalid.", result.text)

    def test_configure_skipped_discovery_prints_when_reusing_existing_syap(self) -> None:
        existing = {
            "TC_AIRPORT_SYAP": "116",
        }
        prompt_values = iter([
            "root@10.0.0.2",
            "goodpw",
            "bridge0",
                        "admin",
            "TimeCapsule",
            "samba4",
            "Time Capsule Samba 4",
            "timecapsulesamba4",
        ])

        def fake_prompt(_label, _default, _secret):
            return next(prompt_values)

        self.force_configure_acp_reprobe_auth_failed()
        result = self.run_configure_cli(
            existing_values=existing,
            prompt_side_effect=fake_prompt,
            probe_state=self.make_probe_state(self.make_probe_result_unreachable()),
            confirm=True,
        )
        self.assertEqual(result.rc, 0)
        self.assertEqual(result.values["TC_AIRPORT_SYAP"], "116")
        self.assertIn("Using TC_AIRPORT_SYAP from .env: 116", result.text)

    def test_configure_ignores_legacy_existing_share_name(self) -> None:
        existing = {
            "TC_SHARE_NAME": "Archive Data",
        }
        prompt_values = iter([
            "root@10.0.0.2",
            "goodpw",
            "bridge0",
            "admin",
            "TimeCapsule",
            "samba4",
            "Time Capsule Samba 4",
            "timecapsulesamba4",
            "119",
        ])

        def fake_prompt(_label, default, _secret):
            self.assertNotEqual(_label, "SMB share name")
            return next(prompt_values)

        self.force_configure_acp_reprobe_auth_failed()
        result = self.run_configure_cli(
            existing_values=existing,
            prompt_side_effect=fake_prompt,
            probe_state=self.make_probe_state(self.make_probe_result_unreachable()),
            confirm=True,
        )
        self.assertEqual(result.rc, 0)
        self.assertNotIn("TC_SHARE_NAME", result.values)
        self.assertNotIn("SMB share name", result.text)

    def test_configure_invalid_ssh_inferred_model_falls_back_to_existing_syap_model(self) -> None:
        existing = {
            "TC_AIRPORT_SYAP": "116",
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule8,119",
            "TC_SSH_OPTS": "-o foo",
        }
        prompt_values = iter([
            "rootpw",
            "bridge0",
                        "admin",
            "TimeCapsule",
            "samba4",
            "Time Capsule Samba 4",
            "timecapsulesamba4",
        ])
        seen_defaults = {}

        def fake_prompt(label, default, _secret):
            seen_defaults[label] = default
            if label == "Device SSH target":
                return "root@10.0.0.2"
            return next(prompt_values)

        self.force_configure_acp_reprobe_auth_failed()
        result = self.run_configure_cli(
            existing_values=existing,
            prompt_side_effect=fake_prompt,
            probe_state=self.make_probe_state(self.make_probe_result_netbsd6()),
        )
        self.assertEqual(result.rc, 0)
        self.assertEqual(result.values["TC_AIRPORT_SYAP"], "119")
        self.assertEqual(result.values["TC_MDNS_DEVICE_MODEL"], "TimeCapsule8,119")
        self.assertNotIn("mDNS device model hint", seen_defaults)
        self.assertIn("Using probed TC_AIRPORT_SYAP: 119", result.text)

    def test_configure_ssh_inferred_mdns_device_model_overrides_existing_model(self) -> None:
        existing = {
            "TC_AIRPORT_SYAP": "119",
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule6,113",
            "TC_SSH_OPTS": "-o foo",
        }
        prompt_values = iter([
            "rootpw",
            "bridge0",
                        "admin",
            "TimeCapsule",
            "samba4",
            "Time Capsule Samba 4",
            "timecapsulesamba4",
        ])
        seen_defaults = {}

        def fake_prompt(label, default, _secret):
            seen_defaults[label] = default
            if label == "Device SSH target":
                return "root@10.0.0.2"
            if label == "mDNS device model hint":
                return default
            return next(prompt_values)

        self.force_configure_acp_reprobe_auth_failed()
        result = self.run_configure_cli(
            existing_values=existing,
            prompt_side_effect=fake_prompt,
            probe_state=self.make_probe_state(self.make_probe_result_netbsd6()),
        )
        self.assertEqual(result.rc, 0)
        self.assertNotIn("mDNS device model hint", seen_defaults)
        self.assertEqual(result.values["TC_AIRPORT_SYAP"], "119")
        self.assertEqual(result.values["TC_MDNS_DEVICE_MODEL"], "TimeCapsule8,119")
        self.assertIn("Using probed TC_AIRPORT_SYAP: 119", result.text)
        self.assertIn("Using probed TC_MDNS_DEVICE_MODEL: TimeCapsule8,119", result.text)

    def test_configure_skipped_discovery_uses_generic_model_default_when_syap_has_no_model_mapping(self) -> None:
        existing = {
            "TC_AIRPORT_SYAP": "119",
            "TC_MDNS_DEVICE_MODEL": "NotATimeCapsule",
        }
        prompt_values = iter([
            "root@10.0.0.2",
            "goodpw",
            "bridge0",
                        "admin",
            "TimeCapsule",
            "samba4",
            "Time Capsule Samba 4",
            "timecapsulesamba4",
            "TimeCapsule",
        ])
        seen_defaults = {}

        def fake_prompt(label, default, _secret):
            seen_defaults[label] = default
            return next(prompt_values)

        self.force_configure_acp_reprobe_auth_failed()
        result = self.run_configure_cli(
            existing_values=existing,
            prompt_side_effect=fake_prompt,
            probe_state=self.make_probe_state(self.make_probe_result_unreachable()),
            confirm=True,
            extra_patches={"timecapsulesmb.cli.configure.infer_mdns_device_model_from_airport_syap": mock.Mock(return_value=None)},
        )
        self.assertEqual(result.rc, 0)
        self.assertEqual(result.values["TC_AIRPORT_SYAP"], "119")
        self.assertEqual(seen_defaults["mDNS device model hint"], "TimeCapsule")
        self.assertEqual(result.values["TC_MDNS_DEVICE_MODEL"], "TimeCapsule")
        self.assertIn("Using TC_AIRPORT_SYAP from .env: 119", result.text)

    def test_configure_existing_syap_autofills_mdns_device_model_when_undetected(self) -> None:
        existing = {
            "TC_AIRPORT_SYAP": "116",
        }
        prompt_values = iter([
            "root@10.0.0.2",
            "goodpw",
            "bridge0",
                        "admin",
            "TimeCapsule",
            "samba4",
            "Time Capsule Samba 4",
            "timecapsulesamba4",
        ])
        seen_defaults = {}

        def fake_prompt(label, default, _secret):
            seen_defaults[label] = default
            if label == "mDNS device model hint":
                return default
            return next(prompt_values)

        self.force_configure_acp_reprobe_auth_failed()
        result = self.run_configure_cli(
            existing_values=existing,
            prompt_side_effect=fake_prompt,
            probe_state=self.make_probe_state(self.make_probe_result_unreachable()),
            confirm=True,
        )
        self.assertEqual(result.rc, 0)
        self.assertEqual(result.values["TC_AIRPORT_SYAP"], "116")
        self.assertNotIn("mDNS device model hint", seen_defaults)
        self.assertEqual(result.values["TC_MDNS_DEVICE_MODEL"], "TimeCapsule6,116")
        self.assertIn("Using TC_AIRPORT_SYAP from .env: 116", result.text)
        self.assertIn("Using TC_MDNS_DEVICE_MODEL derived from TC_AIRPORT_SYAP: TimeCapsule6,116", result.text)

    def test_configure_prompted_syap_overrides_existing_mdns_device_model(self) -> None:
        existing = {
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule6,113",
        }
        prompt_values = iter([
            "root@10.0.0.2",
            "goodpw",
            "bridge0",
                        "admin",
            "TimeCapsule",
            "samba4",
            "Time Capsule Samba 4",
            "timecapsulesamba4",
            "116",
            "TimeCapsule",
        ])
        seen_defaults = {}

        def fake_prompt(label, default, _secret):
            seen_defaults[label] = default
            if label == "mDNS device model hint":
                return default
            return next(prompt_values)

        self.force_configure_acp_reprobe_auth_failed()
        result = self.run_configure_cli(
            existing_values=existing,
            prompt_side_effect=fake_prompt,
            probe_state=self.make_probe_state(self.make_probe_result_unreachable()),
            confirm=True,
        )
        self.assertEqual(result.rc, 0)
        self.assertEqual(result.values["TC_AIRPORT_SYAP"], "116")
        self.assertNotIn("mDNS device model hint", seen_defaults)
        self.assertEqual(result.values["TC_MDNS_DEVICE_MODEL"], "TimeCapsule6,116")
        self.assertIn("Using TC_MDNS_DEVICE_MODEL derived from TC_AIRPORT_SYAP: TimeCapsule6,116", result.text)

    def test_configure_skipped_discovery_prints_when_reusing_existing_mdns_device_model(self) -> None:
        existing = {
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule",
        }
        prompt_values = iter([
            "root@10.0.0.2",
            "goodpw",
            "bridge0",
                        "admin",
            "TimeCapsule",
            "samba4",
            "Time Capsule Samba 4",
            "timecapsulesamba4",
            "116",
            "TimeCapsule",
        ])

        def fake_prompt(_label, _default, _secret):
            return next(prompt_values)

        self.force_configure_acp_reprobe_auth_failed()
        result = self.run_configure_cli(
            existing_values=existing,
            prompt_side_effect=fake_prompt,
            probe_state=self.make_probe_state(self.make_probe_result_unreachable()),
            confirm=True,
            extra_patches={"timecapsulesmb.cli.configure.infer_mdns_device_model_from_airport_syap": mock.Mock(return_value=None)},
        )
        self.assertEqual(result.rc, 0)
        self.assertEqual(result.values["TC_MDNS_DEVICE_MODEL"], "TimeCapsule")
        self.assertIn("Using TC_MDNS_DEVICE_MODEL from .env: TimeCapsule", result.text)

    def test_configure_invalid_saved_mdns_device_model_stays_silent_when_prompted(self) -> None:
        existing = {
            "TC_MDNS_DEVICE_MODEL": "NotATimeCapsule",
        }
        prompt_values = iter([
            "root@10.0.0.2",
            "goodpw",
            "bridge0",
                        "admin",
            "TimeCapsule",
            "samba4",
            "Time Capsule Samba 4",
            "timecapsulesamba4",
            "116",
            "TimeCapsule",
        ])
        seen_defaults = {}

        def fake_prompt(label, default, _secret):
            seen_defaults[label] = default
            return next(prompt_values)

        self.force_configure_acp_reprobe_auth_failed()
        result = self.run_configure_cli(
            existing_values=existing,
            prompt_side_effect=fake_prompt,
            probe_state=self.make_probe_state(self.make_probe_result_unreachable()),
            confirm=True,
            extra_patches={"timecapsulesmb.cli.configure.infer_mdns_device_model_from_airport_syap": mock.Mock(return_value=None)},
        )
        self.assertEqual(result.rc, 0)
        self.assertEqual(seen_defaults["mDNS device model hint"], "TimeCapsule")
        self.assertEqual(result.values["TC_MDNS_DEVICE_MODEL"], "TimeCapsule")
        self.assertNotIn("Found saved value: NotATimeCapsule", result.text)
        self.assertNotIn("Using TC_MDNS_DEVICE_MODEL from .env: NotATimeCapsule", result.text)

    def test_configure_rejects_blank_password_when_no_existing_password(self) -> None:
        input_values = iter([
            "root@10.0.0.2",
            "bridge0",
                        "admin",
            "TimeCapsule",
            "samba4",
            "Time Capsule Samba 4",
            "timecapsulesamba4",
            "119",
            "",
        ])
        password_values = iter(["", "goodpw"])

        result = self.run_configure_cli(
            input_side_effect=lambda _prompt: next(input_values),
            probe_state=self.make_probe_state(self.make_probe_result_unreachable()),
            confirm=True,
            extra_patches={
                "timecapsulesmb.cli.configure.getpass.getpass": mock.Mock(side_effect=lambda _prompt: next(password_values))
            },
        )
        self.assertEqual(result.rc, 0)
        self.assertEqual(result.values["TC_PASSWORD"], "goodpw")
        self.assertIn("Device root password cannot be blank", result.text)

    def test_configure_does_not_print_found_saved_value_for_password(self) -> None:
        input_values = iter([
            "root@10.0.0.2",
            "bridge0",
                        "admin",
            "TimeCapsule",
            "samba4",
            "Time Capsule Samba 4",
            "timecapsulesamba4",
            "119",
        ])
        password_values = iter(["savedpw"])

        result = self.run_configure_cli(
            existing_values={"TC_PASSWORD": "savedpw"},
            input_side_effect=lambda _prompt: next(input_values),
            probe_state=self.make_probe_state(self.make_probe_result_unreachable()),
            confirm=True,
            extra_patches={
                "timecapsulesmb.cli.configure.getpass.getpass": mock.Mock(side_effect=lambda _prompt: next(password_values))
            },
        )
        self.assertEqual(result.rc, 0)
        self.assertEqual(result.values["TC_PASSWORD"], "savedpw")
        self.assertNotIn("Found saved value: savedpw", result.text)

    def test_configure_reprompts_host_and_password_when_validation_fails(self) -> None:
        prompt_values = iter([
            "root@10.0.0.2",
            "badpw",
            "root@10.0.0.3",
            "goodpw",
            "bridge0",
                        "admin",
            "TimeCapsule",
            "samba4",
            "Time Capsule Samba 4",
            "timecapsulesamba4",
            "119",
        ])

        def fake_prompt(_label, _default, _secret):
            label = _label
            default = _default
            if label == "mDNS device model hint":
                return default
            return next(prompt_values)

        result = self.run_configure_cli(
            prompt_side_effect=fake_prompt,
            confirm=False,
            extra_patches={
                "timecapsulesmb.cli.configure.probe_connection_state": mock.Mock(
                    side_effect=[
                        self.make_probe_state(self.make_probe_result_auth_failed()),
                        self.make_probe_state(self.make_probe_result_netbsd6()),
                    ]
                )
            },
        )
        self.assertEqual(result.rc, 0)
        self.assertEqual(result.values["TC_HOST"], "root@10.0.0.3")
        self.assertEqual(result.values["TC_PASSWORD"], "goodpw")
        self.assertIn("did not work", result.text)

    def test_configure_reprompts_bare_ssh_target_before_password(self) -> None:
        password_prompts = 0
        prompt_values = iter([
            "10.0.0.2",
            "root@10.0.0.2",
            "goodpw",
            "bridge0",
                        "admin",
            "TimeCapsule",
            "samba4",
            "Time Capsule Samba 4",
            "timecapsulesamba4",
        ])

        def fake_prompt(label, default, _secret):
            nonlocal password_prompts
            if label == "Device root password":
                password_prompts += 1
            if label == "mDNS device model hint":
                return default
            return next(prompt_values)

        result = self.run_configure_cli(
            prompt_side_effect=fake_prompt,
            probe_state=self.make_probe_state(self.make_probe_result_netbsd6()),
        )
        self.assertEqual(result.rc, 0)
        self.assertEqual(result.values["TC_HOST"], "root@10.0.0.2")
        self.assertEqual(password_prompts, 1)
        self.assertIn("Device SSH target must include a username", result.text)

    def test_configure_can_save_even_when_validation_fails(self) -> None:
        prompt_values = iter([
            "root@10.0.0.2",
            "badpw",
            "bridge0",
                        "admin",
            "TimeCapsule",
            "samba4",
            "Time Capsule Samba 4",
            "timecapsulesamba4",
            "119",
        ])

        def fake_prompt(_label, _default, _secret):
            label = _label
            default = _default
            if label == "mDNS device model hint":
                return default
            return next(prompt_values)

        result = self.run_configure_cli(
            prompt_side_effect=fake_prompt,
            probe_state=self.make_probe_state(self.make_probe_result_auth_failed()),
            confirm=True,
        )
        self.assertEqual(result.rc, 0)
        self.assertEqual(result.values["TC_HOST"], "root@10.0.0.2")
        self.assertEqual(result.values["TC_PASSWORD"], "badpw")
        self._configure_acp_probe_mock.assert_not_called()

    def test_configure_reprompts_when_acp_rejects_airport_password(self) -> None:
        prompt_values = iter([
            "root@10.0.0.2",
            "badpw",
            "root@10.0.0.3",
            "goodpw",
            "bridge0",
                        "admin",
            "TimeCapsule",
            "samba4",
            "Time Capsule Samba 4",
            "timecapsulesamba4",
            "119",
        ])

        def fake_prompt(_label, _default, _secret):
            label = _label
            default = _default
            if label == "mDNS device model hint":
                return default
            return next(prompt_values)

        self._configure_acp_probe_mock.side_effect = [
            ACPAuthError("ACP command failed with error_code -0x10 (likely wrong AirPort admin password)"),
            self.make_probe_state(self.make_probe_result_netbsd6_no_identity()),
        ]
        result = self.run_configure_cli(
            prompt_side_effect=fake_prompt,
            probe_state=self.make_probe_state(self.make_probe_result_unreachable()),
        )
        self.assertEqual(result.rc, 0)
        self.assertEqual(result.values["TC_HOST"], "root@10.0.0.3")
        self.assertEqual(result.values["TC_PASSWORD"], "goodpw")
        self.assertEqual(self._configure_acp_probe_mock.call_count, 2)
        self.assertIn("The AirPort admin password did not work", result.text)
        self.assertIn("Please enter the SSH target and password again", result.text)

    def test_configure_hard_fails_when_acp_enable_fails_non_auth(self) -> None:
        self._configure_acp_probe_mock.side_effect = ACPConnectionError("Could not connect to ACP on 10.0.0.2:5009")
        result = self.run_configure_cli(
            prompt_side_effect=self.configure_prompt_defaults(),
            probe_state=self.make_probe_state(self.make_probe_result_unreachable()),
        )

        self.assertEqual(result.rc, 1)
        result.mocks.write_env_file.assert_not_called()
        self.assertIn(f"{ANSI_RED}Failed to enable SSH via ACP:{ANSI_RESET}", result.text)
        self.assertIn("Could not connect to ACP on 10.0.0.2:5009", result.text)
        error = self.configure_finished_error()
        self.assertIn("Failed to enable SSH via ACP: Could not connect to ACP on 10.0.0.2:5009", error)
        self.assertIn("stage=ssh_probe", error)

    def test_configure_hard_fails_when_ssh_does_not_open_after_acp(self) -> None:
        self._configure_acp_probe_mock.side_effect = [None]
        result = self.run_configure_cli(
            prompt_side_effect=self.configure_prompt_defaults(),
            probe_state=self.make_probe_state(self.make_probe_result_unreachable()),
        )

        self.assertEqual(result.rc, 1)
        result.mocks.write_env_file.assert_not_called()
        self.assertIn("SSH did not open after enabling via ACP.", result.text)
        self.assertIn("SSH did not open after enabling via ACP.", self.configure_finished_error())

    def test_configure_ignores_legacy_name_values_and_does_not_prompt_for_them(self) -> None:
        prompted_labels: list[str] = []
        existing = {
            "TC_NETBIOS_NAME": "ABCDEFGHIJKLMNOP",
            "TC_MDNS_INSTANCE_NAME": "bad.name",
            "TC_MDNS_HOST_LABEL": "time capsule",
        }
        prompt_values = iter([
            "root@10.0.0.2",
            "goodpw",
            "bridge0",
            "admin",
            "samba4",
            "119",
        ])

        def fake_prompt(_label, default, _secret):
            prompted_labels.append(_label)
            if _label == "mDNS device model hint":
                return default
            return next(prompt_values)

        result = self.run_configure_cli(
            existing_values=existing,
            prompt_side_effect=fake_prompt,
            probe_state=self.make_probe_state(self.make_probe_result_unreachable()),
            confirm=True,
        )
        self.assertEqual(result.rc, 0)
        self.assertNotIn("Samba NetBIOS name", prompted_labels)
        self.assertNotIn("mDNS SMB instance name", prompted_labels)
        self.assertNotIn("mDNS host label", prompted_labels)
        self.assertNotIn("TC_NETBIOS_NAME", result.values)
        self.assertNotIn("TC_MDNS_INSTANCE_NAME", result.values)
        self.assertNotIn("TC_MDNS_HOST_LABEL", result.values)

    def test_configure_invalid_hidden_mdns_device_model_falls_back_to_inferred_value(self) -> None:
        existing = {
            "TC_MDNS_DEVICE_MODEL": "a" * 250,
        }
        prompt_values = iter([
            "root@10.0.0.2",
            "goodpw",
            "bridge0",
                        "admin",
            "TimeCapsule",
            "samba4",
            "Time Capsule Samba 4",
            "timecapsulesamba4",
            "119",
        ])

        def fake_prompt(_label, _default, _secret):
            label = _label
            default = _default
            if label == "mDNS device model hint":
                return default
            return next(prompt_values)

        result = self.run_configure_cli(
            existing_values=existing,
            prompt_side_effect=fake_prompt,
            probe_state=self.make_probe_state(self.make_probe_result_netbsd6()),
        )
        self.assertEqual(result.rc, 0)
        self.assertEqual(result.values["TC_MDNS_DEVICE_MODEL"], "TimeCapsule8,119")

    def test_configure_uses_prompted_syap_to_fill_hidden_mdns_device_model_when_undetected(self) -> None:
        prompt_values = iter([
            "root@10.0.0.2",
            "goodpw",
            "bridge0",
            "admin",
            "samba4",
            "116",
        ])

        def fake_prompt(_label, _default, _secret):
            label = _label
            default = _default
            if label == "mDNS device model hint":
                return default
            return next(prompt_values)

        self.force_configure_acp_reprobe_auth_failed()
        result = self.run_configure_cli(
            prompt_side_effect=fake_prompt,
            probe_state=self.make_probe_state(self.make_probe_result_unreachable()),
            confirm=True,
        )
        self.assertEqual(result.rc, 0)
        self.assertEqual(result.values["TC_AIRPORT_SYAP"], "116")
        self.assertEqual(result.values["TC_MDNS_DEVICE_MODEL"], "TimeCapsule6,116")

    def test_configure_prompted_syap_autofills_mdns_device_model_from_lookup(self) -> None:
        prompt_values = iter([
            "root@10.0.0.2",
            "goodpw",
            "bridge0",
            "admin",
            "samba4",
            "116",
        ])
        seen_defaults = {}

        def fake_prompt(label, default, _secret):
            seen_defaults[label] = default
            if label == "mDNS device model hint":
                return default
            return next(prompt_values)

        self.force_configure_acp_reprobe_auth_failed()
        result = self.run_configure_cli(
            prompt_side_effect=fake_prompt,
            probe_state=self.make_probe_state(self.make_probe_result_unreachable()),
            confirm=True,
        )
        self.assertEqual(result.rc, 0)
        self.assertEqual(result.values["TC_AIRPORT_SYAP"], "116")
        self.assertNotIn("mDNS device model hint", seen_defaults)
        self.assertEqual(result.values["TC_MDNS_DEVICE_MODEL"], "TimeCapsule6,116")

    def test_doctor_returns_failure_when_checks_fatal(self) -> None:
        output = io.StringIO()
        fake_result = doctor.CheckResult("FAIL", "broken")
        with mock.patch("timecapsulesmb.cli.doctor.load_env_config", return_value=self.make_app_config({})):
            with mock.patch("timecapsulesmb.cli.doctor.run_doctor_checks", return_value=([fake_result], True)):
                with redirect_stdout(output):
                    rc = doctor.main([])
        self.assertEqual(rc, 1)
        self.assertIn("doctor found one or more fatal problems", output.getvalue())
        self.assertIn("Doctor failures:", self._telemetry_client.emit.call_args_list[-1].kwargs["error"] if self._telemetry_client.emit.call_args_list else "")

    def test_doctor_failure_telemetry_includes_bonjour_candidate_context(self) -> None:
        output = io.StringIO()
        results = [
            doctor.CheckResult("FAIL", "no discovered _smb._tcp instance matched configured instance 'Home'"),
            doctor.CheckResult("INFO", "discovered _smb._tcp candidates: 'Kitchen' @ kitchen.local [10.0.1.99]"),
        ]
        with mock.patch("timecapsulesmb.cli.doctor.load_env_config", return_value=self.make_app_config({})):
            with mock.patch("timecapsulesmb.cli.doctor.run_doctor_checks", return_value=(results, True)):
                with redirect_stdout(output):
                    rc = doctor.main([])
        self.assertEqual(rc, 1)
        telemetry_error = self._telemetry_client.emit.call_args_list[-1].kwargs["error"]
        self.assertIn("Doctor context:", telemetry_error)
        self.assertIn("discovered _smb._tcp candidates: 'Kitchen' @ kitchen.local [10.0.1.99]", telemetry_error)

    def test_doctor_failure_telemetry_includes_debug_fields_from_checks(self) -> None:
        output = io.StringIO()
        results = [doctor.CheckResult("FAIL", "no discovered _smb._tcp instance matched configured instance 'Home'")]

        def fake_run_doctor_checks(*_args, **kwargs):
            kwargs["debug_fields"]["bonjour_zeroconf"] = {"instance_count": 0, "ip_version": "V4Only"}
            kwargs["debug_fields"]["remote_rc_local_log_tail"] = "rc line 1\nrc line 2"
            kwargs["debug_fields"]["remote_mdns_log_tail"] = "mdns line"
            return results, True

        with mock.patch("timecapsulesmb.cli.doctor.load_env_config", return_value=self.make_app_config({})):
            with mock.patch("timecapsulesmb.cli.doctor.run_doctor_checks", side_effect=fake_run_doctor_checks):
                with redirect_stdout(output):
                    rc = doctor.main([])
        self.assertEqual(rc, 1)
        telemetry_error = self._telemetry_client.emit.call_args_list[-1].kwargs["error"]
        self.assertIn("bonjour_zeroconf={instance_count:0,ip_version:V4Only}", telemetry_error)
        self.assertIn("remote_rc_local_log_tail=rc line 1\nrc line 2", telemetry_error)
        self.assertIn("remote_mdns_log_tail=mdns line", telemetry_error)

    def test_doctor_failure_telemetry_identifies_zeroconf_native_dns_sd_false_negative(self) -> None:
        output = io.StringIO()
        results = [doctor.CheckResult("FAIL", "no discovered _smb._tcp instance matched expected device instance 'Home'")]

        def fake_run_doctor_checks(*_args, **kwargs):
            kwargs["debug_fields"]["bonjour_expected"] = {
                "instance_name": "Home",
                "host_label": "home",
                "target_ip": "10.0.0.2",
            }
            kwargs["debug_fields"]["bonjour_zeroconf"] = {"instance_count": 0, "service_event_count": 0, "ptr_record_count": 0}
            kwargs["debug_fields"]["bonjour_native_dns_sd"] = {
                "status": "ok",
                "browses": [
                    {
                        "service_type": "_smb._tcp",
                        "events": [
                            {"service_type": "_smb._tcp", "action": "Add", "name": "Home"},
                        ],
                    }
                ],
            }
            return results, True

        with mock.patch("timecapsulesmb.cli.doctor.load_env_config", return_value=self.make_app_config({})):
            with mock.patch("timecapsulesmb.cli.doctor.run_doctor_checks", side_effect=fake_run_doctor_checks):
                with redirect_stdout(output):
                    rc = doctor.main([])
        self.assertEqual(rc, 1)
        telemetry_error = self._telemetry_client.emit.call_args_list[-1].kwargs["error"]
        self.assertIn("Discovery context:", telemetry_error)
        self.assertIn("INFO Python zeroconf discovered 0 Bonjour instances during doctor", telemetry_error)
        self.assertIn("INFO native dns-sd discovered expected _smb._tcp instance 'Home'", telemetry_error)
        self.assertIn("INFO likely doctor false negative", telemetry_error)

    def test_doctor_error_does_not_report_false_negative_when_native_dns_sd_only_saw_other_instances(self) -> None:
        results = [
            doctor.CheckResult(
                "FAIL",
                "no discovered _smb._tcp instance matched expected device instance 'Home'",
            )
        ]
        error = doctor.build_doctor_error(
            results,
            {
                "bonjour_expected": {"instance_name": "Home"},
                "bonjour_zeroconf": {"instance_count": 0},
                "bonjour_native_dns_sd": {
                    "browses": [
                        {
                            "service_type": "_smb._tcp",
                            "events": [
                                {"service_type": "_smb._tcp", "action": "Add", "name": "Kitchen"},
                            ],
                        }
                    ],
                },
            },
        )
        self.assertIsNotNone(error)
        assert error is not None
        self.assertNotIn("Discovery context:", error)
        self.assertNotIn("likely doctor false negative", error)

    def test_doctor_failure_telemetry_includes_derived_mdns_boot_context(self) -> None:
        output = io.StringIO()
        results = [
            doctor.CheckResult(
                "FAIL",
                "no discovered _smb._tcp instance matched expected device instance 'Home'",
            )
        ]

        def fake_run_doctor_checks(*_args, **kwargs):
            kwargs["debug_fields"]["remote_rc_local_log_tail"] = "\n".join(
                [
                    "mDNS snapshot capture did not produce trusted Apple snapshot; generating AirPort fallback",
                    "mDNS AirPort snapshot generated",
                    "trusted Apple mDNS snapshot was updated during this boot run: /mnt/Flash/applemdns.txt",
                ]
            )
            kwargs["debug_fields"]["remote_mdns_log_tail"] = "\n".join(
                [
                    "warning: could not identify local Apple mDNS records for snapshot file: /mnt/Flash/applemdns.txt",
                    "airport snapshot: wrote 1 record to /mnt/Flash/applemdns.txt",
                    "snapshot load: loaded 1 records, advertising 1 snapshot records",
                    "serving summary: source=snapshot",
                    "serving service: type=_smb._tcp.local. instance=Home port=445 host=home.local.",
                    "serving service: type=_adisk._tcp.local. instance=Home share=Data disk_key=dk2 uuid=1234",
                    "serving service: type=_device-info._tcp.local. instance=Home model=TimeCapsule6,116",
                    "mDNS takeover established after SIGTERM + 0ms using exclusive bind",
                ]
            )
            return results, True

        with mock.patch("timecapsulesmb.cli.doctor.load_env_config", return_value=self.make_app_config({})):
            with mock.patch("timecapsulesmb.cli.doctor.run_doctor_checks", side_effect=fake_run_doctor_checks):
                with redirect_stdout(output):
                    rc = doctor.main([])
        self.assertEqual(rc, 1)
        telemetry_error = self._telemetry_client.emit.call_args_list[-1].kwargs["error"]
        self.assertIn("mDNS boot context:", telemetry_error)
        self.assertIn("INFO trusted Apple mDNS snapshot capture failed; AirPort fallback snapshot was generated", telemetry_error)
        self.assertIn("INFO mDNS snapshot load: loaded 1 records, advertising 1 snapshot records", telemetry_error)
        self.assertIn(
            "INFO mdns-advertiser source=snapshot; generated services include _smb._tcp.local., _adisk._tcp.local., _device-info._tcp.local.",
            telemetry_error,
        )
        self.assertIn("INFO mDNS takeover established after SIGTERM + 0ms using exclusive bind", telemetry_error)

    def test_doctor_includes_soft_preinspection_error_in_failure_telemetry(self) -> None:
        output = io.StringIO()
        values = self.make_valid_env()
        fake_result = doctor.CheckResult("FAIL", "SSH command works failed")
        with tempfile.NamedTemporaryFile() as env_file:
            env_path = Path(env_file.name)
            with mock.patch("timecapsulesmb.cli.doctor.load_env_config", return_value=self.make_app_config(values, path=env_path)):
                with mock.patch(
                    "timecapsulesmb.cli.context.CommandContext.inspect_managed_connection",
                    side_effect=SshError("Connecting to the device failed, SSH error: bind failed"),
                ):
                    with mock.patch("timecapsulesmb.cli.doctor.run_doctor_checks", return_value=([fake_result], True)):
                        with redirect_stdout(output):
                            rc = doctor.main([])
        self.assertEqual(rc, 1)
        telemetry_error = self._telemetry_client.emit.call_args_list[-1].kwargs["error"]
        self.assertIn("Doctor failures:", telemetry_error)
        self.assertIn("preflight_error=doctor pre-inspection failed: Connecting to the device failed, SSH error: bind failed", telemetry_error)

    def test_doctor_passes_preinspection_state_to_checks(self) -> None:
        output = io.StringIO()
        values = self.make_valid_env()
        command_context = FakeCommandContext()
        probe_state = self.make_probe_state(self.make_probe_result_netbsd6())
        command_context.probe_state = probe_state
        original_inspect = command_context.inspect_managed_connection
        command_context.inspect_managed_connection = mock.Mock(side_effect=original_inspect)

        with tempfile.NamedTemporaryFile() as env_file:
            env_path = Path(env_file.name)
            with mock.patch("timecapsulesmb.cli.doctor.load_env_config", return_value=self.make_app_config(values, path=env_path)):
                with mock.patch("timecapsulesmb.cli.doctor.CommandContext", return_value=command_context):
                    with mock.patch("timecapsulesmb.cli.doctor.run_doctor_checks", return_value=([], False)) as checks_mock:
                        with redirect_stdout(output):
                            rc = doctor.main([])

        self.assertEqual(rc, 0)
        command_context.inspect_managed_connection.assert_called_once_with(
            iface=values["TC_NET_IFACE"],
            include_probe=True,
        )
        checks_kwargs = checks_mock.call_args.kwargs
        self.assertIs(checks_kwargs["connection"], command_context.connection)
        self.assertIs(checks_kwargs["precomputed_interface_probe"], command_context.interface_probe)
        self.assertIs(checks_kwargs["precomputed_probe_state"], probe_state)

    def test_doctor_streams_results_in_human_mode(self) -> None:
        output = io.StringIO()
        streamed_result = doctor.CheckResult("PASS", "streamed")

        def fake_run_doctor_checks(*_args, **kwargs):
            kwargs["on_result"](streamed_result)
            return ([streamed_result], False)

        with mock.patch("timecapsulesmb.cli.doctor.load_env_config", return_value=self.make_app_config({})):
            with mock.patch("timecapsulesmb.cli.doctor.run_doctor_checks", side_effect=fake_run_doctor_checks):
                with redirect_stdout(output):
                    rc = doctor.main([])
        self.assertEqual(rc, 0)
        self.assertIn("\033[32mPASS\033[0m streamed", output.getvalue())

    def test_doctor_streams_fail_results_in_red_in_human_mode(self) -> None:
        output = io.StringIO()
        streamed_result = doctor.CheckResult("FAIL", "broken")

        def fake_run_doctor_checks(*_args, **kwargs):
            kwargs["on_result"](streamed_result)
            return ([streamed_result], True)

        with mock.patch("timecapsulesmb.cli.doctor.load_env_config", return_value=self.make_app_config({})):
            with mock.patch("timecapsulesmb.cli.doctor.run_doctor_checks", side_effect=fake_run_doctor_checks):
                with redirect_stdout(output):
                    rc = doctor.main([])
        self.assertEqual(rc, 1)
        self.assertIn("\033[31mFAIL\033[0m broken", output.getvalue())

    def test_doctor_streams_info_results_in_human_mode(self) -> None:
        output = io.StringIO()
        streamed_result = doctor.CheckResult("INFO", "advertised Bonjour instance: Home-Samba")

        def fake_run_doctor_checks(*_args, **kwargs):
            kwargs["on_result"](streamed_result)
            return ([streamed_result], False)

        with mock.patch("timecapsulesmb.cli.doctor.load_env_config", return_value=self.make_app_config({})):
            with mock.patch("timecapsulesmb.cli.doctor.run_doctor_checks", side_effect=fake_run_doctor_checks):
                with redirect_stdout(output):
                    rc = doctor.main([])
        self.assertEqual(rc, 0)
        self.assertIn("INFO advertised Bonjour instance: Home-Samba", output.getvalue())

    def test_exact_device_display_name_uses_configured_identity(self) -> None:
        self.assertEqual(
            airport_exact_display_name_from_config(
                AppConfig.from_values({
                    "TC_AIRPORT_SYAP": "120",
                    "TC_MDNS_DEVICE_MODEL": "AirPort7,120",
                })
            ),
            "AirPort Extreme 6th generation",
        )

    def test_set_ssh_returns_error_when_env_missing(self) -> None:
        output = io.StringIO()
        with mock.patch("timecapsulesmb.cli.set_ssh.load_env_config", return_value=self.make_app_config({}, exists=False)):
            with redirect_stdout(output):
                rc = set_ssh.main([])
        self.assertEqual(rc, 1)
        self.assertIn("Please run the `configure` command before running `set-ssh`.", output.getvalue())
        started = self.telemetry_payload("set_ssh_started")
        finished = self.telemetry_payload("set_ssh_finished")
        self.assertEqual(started["command_id"], finished["command_id"])
        self.assertEqual(finished["result"], "failure")
        self.assertEqual(finished["set_ssh_action"], "missing_config")
        self.assertIn("stage=load_config", finished["error"])
        self.assertNotIn("TC_PASSWORD", finished["error"])

    def test_set_ssh_enable_flow_succeeds(self) -> None:
        output = io.StringIO()
        values = {"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"}
        with mock.patch("timecapsulesmb.cli.set_ssh.load_env_config", return_value=self.make_app_config(values)):
            with mock.patch("timecapsulesmb.cli.set_ssh.tcp_open", return_value=False):
                with mock.patch("timecapsulesmb.cli.set_ssh.enable_ssh") as enable_ssh_mock:
                    with mock.patch("timecapsulesmb.cli.set_ssh.wait_for_tcp_port_state", return_value=True):
                        with redirect_stdout(output):
                            rc = set_ssh.main([])
        self.assertEqual(rc, 0)
        enable_ssh_mock.assert_called_once()
        self.assertIn("SSH is configured", output.getvalue())
        finished = self.telemetry_payload("set_ssh_finished")
        self.assertEqual(finished["result"], "success")
        self.assertEqual(finished["set_ssh_action"], "enable_ssh")
        self.assertEqual(finished["ssh_initially_reachable"], False)
        self.assertEqual(finished["ssh_final_reachable"], True)

    def test_set_ssh_enable_exception_emits_failure_stage(self) -> None:
        output = io.StringIO()
        values = {"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"}
        with mock.patch("timecapsulesmb.cli.set_ssh.load_env_config", return_value=self.make_app_config(values)):
            with mock.patch("timecapsulesmb.cli.set_ssh.tcp_open", return_value=False):
                with mock.patch("timecapsulesmb.cli.set_ssh.enable_ssh", side_effect=RuntimeError("ACP failed")):
                    with redirect_stdout(output):
                        rc = set_ssh.main([])
        self.assertEqual(rc, 1)
        message = "Failed to enable SSH via ACP: ACP failed"
        self.assertIn(f"{ANSI_RED}Failed to enable SSH via ACP:{ANSI_RESET}", output.getvalue())
        self.assertIn("ACP failed", output.getvalue())
        finished = self.telemetry_payload("set_ssh_finished")
        self.assertEqual(finished["result"], "failure")
        self.assertEqual(finished["set_ssh_action"], "enable_ssh")
        self.assertIn("stage=enable_ssh", finished["error"])
        self.assertIn(message, finished["error"])
        self.assertNotIn(ANSI_RED, finished["error"])

    def test_set_ssh_enable_failure_reports_acp_error_without_bootstrap_guidance(self) -> None:
        output = io.StringIO()
        values = {"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"}
        error = "ACP command failed with error_code -0x1234 (likely wrong AirPort admin password)"
        with mock.patch("timecapsulesmb.cli.set_ssh.load_env_config", return_value=self.make_app_config(values)):
            with mock.patch("timecapsulesmb.cli.set_ssh.tcp_open", return_value=False):
                with mock.patch("timecapsulesmb.cli.set_ssh.enable_ssh", side_effect=RuntimeError(error)):
                    with redirect_stdout(output):
                        rc = set_ssh.main([])

        self.assertEqual(rc, 1)
        rendered = output.getvalue()
        self.assertIn(f"{ANSI_RED}Failed to enable SSH via ACP:{ANSI_RESET}", rendered)
        self.assertIn(error, rendered)
        self.assertNotIn("./tcapsule bootstrap", rendered)
        finished = self.telemetry_payload("set_ssh_finished")
        self.assertIn(f"Failed to enable SSH via ACP: {error}", finished["error"])
        self.assertNotIn(ANSI_RED, finished["error"])

    def test_set_ssh_disable_failure_is_reported_as_ssh_error(self) -> None:
        output = io.StringIO()
        values = {"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"}
        error = "on-device acp failed"
        with mock.patch("timecapsulesmb.cli.set_ssh.load_env_config", return_value=self.make_app_config(values)):
            with mock.patch("timecapsulesmb.cli.set_ssh.tcp_open", return_value=True):
                with mock.patch("builtins.input", return_value="y"):
                    with mock.patch("timecapsulesmb.cli.set_ssh.disable_ssh_over_ssh", side_effect=RuntimeError(error)):
                        with redirect_stdout(output):
                            rc = set_ssh.main([])

        self.assertEqual(rc, 1)
        rendered = output.getvalue()
        self.assertIn(f"{ANSI_RED}Failed to disable SSH over SSH:{ANSI_RESET}", rendered)
        self.assertIn(error, rendered)
        self.assertNotIn("AirPyrt", rendered)
        self.assertNotIn("./tcapsule bootstrap", rendered)
        finished = self.telemetry_payload("set_ssh_finished")
        self.assertIn(f"Failed to disable SSH over SSH: {error}", finished["error"])
        self.assertNotIn("AirPyrt", finished["error"])
        self.assertNotIn(ANSI_RED, finished["error"])

    def test_set_ssh_disable_fails_when_ssh_never_goes_down(self) -> None:
        output = io.StringIO()
        values = {"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"}
        with mock.patch("timecapsulesmb.cli.set_ssh.load_env_config", return_value=self.make_app_config(values)):
            with mock.patch("timecapsulesmb.cli.set_ssh.tcp_open", return_value=True):
                with mock.patch("builtins.input", return_value="y"):
                    with mock.patch("timecapsulesmb.cli.set_ssh.disable_ssh_over_ssh"):
                        with mock.patch("timecapsulesmb.cli.set_ssh.wait_for_tcp_port_state", return_value=False) as wait_port_mock:
                            with mock.patch("timecapsulesmb.cli.set_ssh.wait_for_device_up") as wait_up_mock:
                                with redirect_stdout(output):
                                    rc = set_ssh.main([])
        self.assertEqual(rc, 1)
        wait_port_mock.assert_called_once_with("10.0.0.2", 22, expected_state=False, service_name="SSH port")
        wait_up_mock.assert_not_called()
        self.assertIn("SSH did not close after disable/reboot request; disable could not be verified.", output.getvalue())
        finished = self.telemetry_payload("set_ssh_finished")
        self.assertEqual(finished["result"], "failure")
        self.assertEqual(finished["set_ssh_action"], "disable_ssh")
        self.assertEqual(finished["ssh_final_reachable"], True)
        self.assertEqual(finished["ssh_disable_persisted"], False)
        self.assertEqual(finished["ssh_reboot_observed_down"], False)
        self.assertIn("stage=wait_for_ssh_down", finished["error"])

    def test_set_ssh_disable_fails_when_device_does_not_come_back(self) -> None:
        output = io.StringIO()
        values = {"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"}
        with mock.patch("timecapsulesmb.cli.set_ssh.load_env_config", return_value=self.make_app_config(values)):
            with mock.patch("timecapsulesmb.cli.set_ssh.tcp_open", return_value=True):
                with mock.patch("builtins.input", return_value="y"):
                    with mock.patch("timecapsulesmb.cli.set_ssh.disable_ssh_over_ssh"):
                        with mock.patch("timecapsulesmb.cli.set_ssh.wait_for_tcp_port_state", return_value=True) as wait_port_mock:
                            with mock.patch("timecapsulesmb.cli.set_ssh.wait_for_device_up", return_value=False) as wait_up_mock:
                                with redirect_stdout(output):
                                    rc = set_ssh.main([])
        self.assertEqual(rc, 1)
        wait_port_mock.assert_called_once_with("10.0.0.2", 22, expected_state=False, service_name="SSH port")
        wait_up_mock.assert_called_once_with("10.0.0.2")
        self.assertIn("Device went down after disable request but did not come back within timeout.", output.getvalue())
        finished = self.telemetry_payload("set_ssh_finished")
        self.assertEqual(finished["result"], "failure")
        self.assertEqual(finished["set_ssh_action"], "disable_ssh")
        self.assertEqual(finished["ssh_reboot_observed_down"], True)
        self.assertEqual(finished["device_recovered"], False)
        self.assertIn("stage=wait_for_device_up", finished["error"])

    def test_set_ssh_disable_fails_when_ssh_reopens(self) -> None:
        output = io.StringIO()
        values = {"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw", "TC_SSH_OPTS": "-o ProxyJump=bastion"}
        with mock.patch("timecapsulesmb.cli.set_ssh.load_env_config", return_value=self.make_app_config(values)):
            with mock.patch("timecapsulesmb.cli.set_ssh.tcp_open", return_value=True):
                with mock.patch("builtins.input", return_value="y"):
                    with mock.patch("timecapsulesmb.cli.set_ssh.disable_ssh_over_ssh") as disable_ssh_mock:
                        with mock.patch("timecapsulesmb.cli.set_ssh.wait_for_tcp_port_state", side_effect=[True, False]):
                            with mock.patch("timecapsulesmb.cli.set_ssh.wait_for_device_up", return_value=True):
                                with redirect_stdout(output):
                                    rc = set_ssh.main([])
        self.assertEqual(rc, 1)
        disable_ssh_mock.assert_called_once_with(
            SshConnection("root@10.0.0.2", "pw", "-o ProxyJump=bastion"),
            reboot_device=True,
            log=print,
        )
        self.assertIn("SSH reopened after reboot. Disable did not persist.", output.getvalue())
        finished = self.telemetry_payload("set_ssh_finished")
        self.assertEqual(finished["result"], "failure")
        self.assertEqual(finished["set_ssh_action"], "disable_ssh")
        self.assertEqual(finished["ssh_initially_reachable"], True)
        self.assertEqual(finished["ssh_reboot_observed_down"], True)
        self.assertEqual(finished["device_recovered"], True)
        self.assertEqual(finished["ssh_final_reachable"], True)
        self.assertEqual(finished["ssh_disable_persisted"], False)
        self.assertIn("stage=verify_ssh_disabled", finished["error"])

    def test_set_ssh_disable_flow_confirms_ssh_disabled(self) -> None:
        output = io.StringIO()
        values = {"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"}
        with mock.patch("timecapsulesmb.cli.set_ssh.load_env_config", return_value=self.make_app_config(values)):
            with mock.patch("timecapsulesmb.cli.set_ssh.tcp_open", return_value=True):
                with mock.patch("builtins.input", return_value="y"):
                    with mock.patch("timecapsulesmb.cli.set_ssh.disable_ssh_over_ssh"):
                        with mock.patch("timecapsulesmb.cli.set_ssh.wait_for_tcp_port_state", side_effect=[True, True]):
                            with mock.patch("timecapsulesmb.cli.set_ssh.wait_for_device_up", return_value=True):
                                with redirect_stdout(output):
                                    rc = set_ssh.main([])
        self.assertEqual(rc, 0)
        self.assertIn("SSH disabled (remains closed after reboot)", output.getvalue())
        finished = self.telemetry_payload("set_ssh_finished")
        self.assertEqual(finished["result"], "success")
        self.assertEqual(finished["set_ssh_action"], "disable_ssh")
        self.assertEqual(finished["ssh_reboot_observed_down"], True)
        self.assertEqual(finished["device_recovered"], True)
        self.assertEqual(finished["ssh_final_reachable"], False)
        self.assertEqual(finished["ssh_disable_persisted"], True)

    def test_doctor_json_outputs_structured_results(self) -> None:
        output = io.StringIO()
        fake_result = doctor.CheckResult("PASS", "ok")
        with mock.patch("timecapsulesmb.cli.doctor.load_env_config", return_value=self.make_app_config({})):
            with mock.patch("timecapsulesmb.cli.doctor.run_doctor_checks", return_value=([fake_result], False)):
                with redirect_stdout(output):
                    rc = doctor.main(["--json"])
        self.assertEqual(rc, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["fatal"], False)
        self.assertEqual(payload["results"][0]["status"], "PASS")

    def test_doctor_ensures_install_id_before_telemetry(self) -> None:
        output = io.StringIO()
        fake_result = doctor.CheckResult("PASS", "ok")
        with mock.patch("timecapsulesmb.cli.doctor.ensure_install_id") as ensure_mock:
            with mock.patch("timecapsulesmb.cli.doctor.load_env_config", return_value=self.make_app_config({})):
                with mock.patch("timecapsulesmb.cli.doctor.CommandContext", return_value=FakeCommandContext()):
                    with mock.patch("timecapsulesmb.cli.doctor.run_doctor_checks", return_value=([fake_result], False)):
                        with redirect_stdout(output):
                            rc = doctor.main(["--json"])
        self.assertEqual(rc, 0)
        ensure_mock.assert_called_once_with()

    def test_deploy_dry_run_prints_mast_payload_placeholder(self) -> None:
        result = self.run_deploy_cli(
            ["--dry-run"],
            artifacts=[("smbd", True, "ok"), ("mdns", True, "ok")],
            patch_actions=True,
            patch_upload=True,
        )

        self.assertEqual(result.rc, 0)
        text = result.text
        self.assertIn("Dry run: deployment plan", text)
        self.assertIn("host: root@10.0.0.2", text)
        self.assertIn("volume root: resolved from MaSt at deploy time", text)
        self.assertIn("payload dir: resolved from MaSt at deploy time/.samba4", text)
        self.assertIn(f"diskd.useVolume wait: {DEFAULT_APPLE_MOUNT_WAIT_SECONDS}s", text)
        self.assertIn("generated flash runtime config", text)
        self.assertIn("generated smbpasswd", text)
        self.assertNotIn("rendered:smb.conf.template", text)
        self.assertNotIn("generated adisk", text)
        self.assertNotIn("generated nbns marker", text)
        result.mocks.run_remote_actions.assert_not_called()
        result.mocks.upload_deployment_payload.assert_not_called()
        result.mocks.wait_for_mast_volumes_conn.assert_not_called()
        result.mocks.select_payload_home_with_diagnostics_conn.assert_not_called()

    def test_deploy_dry_run_json_outputs_modern_multivolume_plan(self) -> None:
        values = self.make_valid_env()
        result = self.run_deploy_cli(["--dry-run", "--json"], values=values)

        self.assertEqual(result.rc, 0)
        payload = json.loads(result.text)
        self.assertEqual(payload["host"], "root@10.0.0.2")
        self.assertEqual(payload["volume_root"], "resolved from MaSt at deploy time")
        self.assertEqual(payload["device_path"], "resolved from MaSt at deploy time")
        self.assertEqual(payload["payload_dir"], "resolved from MaSt at deploy time/.samba4")
        self.assertEqual(payload["apple_mount_wait_seconds"], DEFAULT_APPLE_MOUNT_WAIT_SECONDS)
        self.assertEqual(payload["payload_targets"]["nbns-advertiser"], "resolved from MaSt at deploy time/.samba4/nbns-advertiser")
        self.assertIn(
            {
                "source_id": GENERATED_FLASH_CONFIG_SOURCE,
                "destination": "/mnt/Flash/tcapsulesmb.conf",
                "mode": "flash_atomic",
                "timeout_seconds": 120,
                "description": "generated flash runtime config",
            },
            payload["uploads"],
        )
        self.assertNotIn("rendered:smb.conf.template", {upload["source_id"] for upload in payload["uploads"]})
        self.assertNotIn("generated:adisk.uuid", {upload["source_id"] for upload in payload["uploads"]})
        self.assertNotIn("generated:nbns.enabled", {upload["source_id"] for upload in payload["uploads"]})
        self.assertNotIn("initialize_data_root", {action["kind"] for action in payload["pre_upload_actions"]})
        self.assertIn("ensure_volume_mounted", {action["kind"] for action in payload["pre_upload_actions"]})
        self.assertEqual(
            [check["id"] for check in payload["post_deploy_checks"]],
            [
                "ssh_goes_down_after_reboot",
                "ssh_returns_after_reboot",
                "managed_runtime_smb_conf_present",
                "managed_smbd_parent_process",
                "managed_smbd_bound_445",
                "managed_mdns_takeover_ready",
                "authenticated_smb_listing",
            ],
        )

    def test_deploy_mount_wait_dry_run_json_uses_custom_value(self) -> None:
        result = self.run_deploy_cli(["--dry-run", "--json", "--mount-wait", "123"], values=self.make_valid_env())
        self.assertEqual(result.rc, 0)
        self.assertEqual(json.loads(result.text)["apple_mount_wait_seconds"], 123)

    def test_deploy_mount_wait_rejects_negative_values(self) -> None:
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                deploy.main(["--dry-run", "--mount-wait", "-1"])
        self.assertEqual(raised.exception.code, 2)

    def test_deploy_selects_payload_home_from_mast_for_real_deploy(self) -> None:
        volumes = (
            self._mast_volume("dk3", disk_device="sd0", name="USB", builtin=False),
            self._mast_volume("dk2", disk_device="wd0", name="Data", builtin=True),
        )
        result = self.run_deploy_cli(
            ["--yes", "--no-reboot", "--mount-wait", "7"],
            mast_volumes=volumes,
            mount_root="/Volumes/dk2",
            patch_actions=True,
            patch_upload=True,
        )

        self.assertEqual(result.rc, 0)
        result.mocks.wait_for_mast_volumes_conn.assert_called_once()
        self.assertEqual(result.mocks.wait_for_mast_volumes_conn.call_args.kwargs["attempts"], 10)
        self.assertEqual(result.mocks.wait_for_mast_volumes_conn.call_args.kwargs["delay_seconds"], 3)
        result.mocks.select_payload_home_with_diagnostics_conn.assert_called_once_with(
            result.mocks.wait_for_mast_volumes_conn.call_args.args[0],
            volumes,
            ".samba4",
            wait_seconds=7,
        )
        self.assertEqual(result.mocks.run_remote_actions.call_count, 2)
        result.mocks.upload_deployment_payload.assert_called_once()
        payload_home = PayloadHome("/Volumes/dk2", "/dev/dk2", ".samba4")
        result.mocks.verify_payload_home_conn.assert_has_calls(
            [
                mock.call(result.mocks.wait_for_mast_volumes_conn.call_args.args[0], payload_home, wait_seconds=7),
                mock.call(result.mocks.wait_for_mast_volumes_conn.call_args.args[0], payload_home, wait_seconds=7),
            ]
        )
        self.assertEqual(result.mocks.verify_payload_home_conn.call_count, 2)
        result.mocks.flush_remote_filesystem_writes.assert_called_once_with(
            result.mocks.wait_for_mast_volumes_conn.call_args.args[0]
        )
        self.assertIn("Flushing deployed payload to disk...", result.text)
        self.assertIn("Deployed Samba payload to /Volumes/dk2/.samba4", result.text)
        self.assertIn("Updated /mnt/Flash boot files.", result.text)
        self.assertIn("Skipping reboot.", result.text)

    def test_deploy_upload_source_resolver_contains_flash_config_and_no_legacy_generated_files(self) -> None:
        captured: dict[str, object] = {}

        def fake_upload(_plan, *, connection, source_resolver):
            captured["host"] = connection.host
            captured["source_ids"] = set(source_resolver)
            captured["smbpasswd"] = source_resolver[GENERATED_SMBPASSWD_SOURCE].read_text()
            captured["username_map"] = source_resolver[GENERATED_USERNAME_MAP_SOURCE].read_text()
            captured["flash_config"] = source_resolver[GENERATED_FLASH_CONFIG_SOURCE].read_text()

        result = self.run_deploy_cli(
            ["--debug-logging", "--no-reboot"],
            values=self.make_valid_env(TC_SAMBA_USER="admin"),
            patch_actions=True,
            patch_upload=True,
            upload_side_effect=fake_upload,
        )

        self.assertEqual(result.rc, 0)
        self.assertEqual(captured["host"], "root@10.0.0.2")
        self.assertIn(GENERATED_SMBPASSWD_SOURCE, captured["source_ids"])
        self.assertIn(GENERATED_USERNAME_MAP_SOURCE, captured["source_ids"])
        self.assertIn(GENERATED_FLASH_CONFIG_SOURCE, captured["source_ids"])
        self.assertNotIn("rendered:smb.conf.template", captured["source_ids"])
        self.assertNotIn("generated:adisk.uuid", captured["source_ids"])
        self.assertNotIn("generated:nbns.enabled", captured["source_ids"])
        self.assertIn("root:0:XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX:", captured["smbpasswd"])
        self.assertEqual(captured["username_map"], "!root = root\nroot = *\n")
        flash_config = str(captured["flash_config"])
        self.assertIn("TC_CONFIG_VERSION=1\n", flash_config)
        self.assertIn("PAYLOAD_DIR_NAME=.samba4\n", flash_config)
        self.assertIn("NBNS_ENABLED=1\n", flash_config)
        self.assertIn("SMBD_DEBUG_LOGGING=1\n", flash_config)
        self.assertNotIn("PAYLOAD_VOLUME_HINT", flash_config)
        self.assertNotIn("PAYLOAD_DEVICE_HINT", flash_config)
        self.assertNotIn("PAYLOAD_INSTALL_ID", flash_config)
        self.assertNotIn("TC_SHARE_NAME", flash_config)

    def test_deploy_no_nbns_writes_disabled_flash_config(self) -> None:
        captured: dict[str, str] = {}

        def fake_upload(_plan, *, connection, source_resolver):
            captured["flash_config"] = source_resolver[GENERATED_FLASH_CONFIG_SOURCE].read_text()

        result = self.run_deploy_cli(
            ["--no-nbns", "--no-reboot"],
            patch_actions=True,
            patch_upload=True,
            upload_side_effect=fake_upload,
        )

        self.assertEqual(result.rc, 0)
        self.assertIn("NBNS_ENABLED=0\n", captured["flash_config"])
        finished = self.telemetry_payload("deploy_finished")
        self.assertFalse(finished["nbns_enabled"])

    def test_deploy_rejects_removed_install_nbns_flag(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as raised:
                deploy.main(["--install-nbns", "--dry-run"])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("unrecognized arguments: --install-nbns", stderr.getvalue())

    def test_deploy_exits_when_mast_volumes_are_not_writable(self) -> None:
        volumes = (self._mast_volume("dk2"),)
        result = self.run_deploy_cli(
            ["--yes"],
            mast_volumes=volumes,
            payload_home_selection=PayloadHomeSelection(None, (PayloadCandidateCheck(volumes[0], True, False),)),
            patch_actions=True,
            patch_upload=True,
            raises=SystemExit,
        )

        self.assertEqual(
            str(result.exception),
            "MaSt found 1 deployable HFS volume(s), but deploy could not write to any of them.",
        )
        result.mocks.run_remote_actions.assert_not_called()
        result.mocks.upload_deployment_payload.assert_not_called()
        telemetry_error = self.telemetry_payload("deploy_finished")["error"]
        self.assertIn("stage=select_payload_home", telemetry_error)
        self.assertIn("mast_volume_count=1", telemetry_error)
        self.assertIn("mast_candidates=[{disk:wd0,part:dk2", telemetry_error)
        self.assertIn("mast_candidate_checks=[{disk:wd0,part:dk2", telemetry_error)
        self.assertIn("mounted:true", telemetry_error)
        self.assertIn("writable:false", telemetry_error)

    def test_deploy_exits_when_mast_discovery_never_finds_disks(self) -> None:
        raw_mast_output = "MaSt=<plist><array/></plist>"
        result = self.run_deploy_cli(
            ["--yes"],
            mast_volumes=(),
            mast_discovery=MaStDiscoveryResult((), 10, raw_mast_output),
            patch_actions=True,
            patch_upload=True,
            raises=SystemExit,
        )

        self.assertEqual(
            str(result.exception),
            "No deployable HFS disk was found after 10 MaSt queries spaced 3 seconds apart.",
        )
        result.mocks.wait_for_mast_volumes_conn.assert_called_once()
        result.mocks.select_payload_home_with_diagnostics_conn.assert_not_called()
        result.mocks.run_remote_actions.assert_not_called()
        result.mocks.upload_deployment_payload.assert_not_called()
        telemetry_error = self.telemetry_payload("deploy_finished")["error"]
        self.assertIn("stage=read_mast", telemetry_error)
        self.assertIn("mast_read_attempts=10", telemetry_error)
        self.assertIn("mast_volume_count=0", telemetry_error)
        self.assertIn("mast_candidates=[]", telemetry_error)
        self.assertIn(f"mast_acp_output_chars={len(raw_mast_output)}", telemetry_error)
        self.assertIn(f"mast_acp_output={raw_mast_output}", telemetry_error)

    def test_deploy_no_reboot_stops_after_upload_phase(self) -> None:
        result = self.run_deploy_cli(
            ["--no-reboot"],
            artifacts=[("smbd", True, "ok"), ("mdns", True, "ok")],
            patch_actions=True,
            patch_upload=True,
            reboot_side_effect=AssertionError("deploy --no-reboot should not request a reboot"),
        )

        self.assertEqual(result.rc, 0)
        result.mocks.remote_request_reboot.assert_not_called()
        self.assertEqual(result.mocks.verify_payload_home_conn.call_count, 2)
        result.mocks.flush_remote_filesystem_writes.assert_called_once()
        self.assertIn("Skipping reboot.", result.text)

    def test_deploy_payload_verification_failure_aborts_before_reboot(self) -> None:
        result = self.run_deploy_cli(
            ["--yes"],
            patch_actions=True,
            patch_upload=True,
            payload_verification=PayloadVerificationResult(False, "missing smbd"),
            reboot_side_effect=AssertionError("deploy should not request reboot after payload verification failure"),
            raises=SystemExit,
        )

        self.assertEqual(str(result.exception), "managed payload verification failed at /Volumes/dk2/.samba4: missing smbd")
        result.mocks.remote_request_reboot.assert_not_called()
        result.mocks.verify_payload_home_conn.assert_called_once()
        result.mocks.flush_remote_filesystem_writes.assert_not_called()
        telemetry_error = self.telemetry_payload("deploy_finished")["error"]
        self.assertIn("stage=verify_payload_upload", telemetry_error)
        self.assertIn("managed payload verification failed", telemetry_error)

    def test_deploy_post_sync_payload_verification_failure_aborts_before_reboot(self) -> None:
        result = self.run_deploy_cli(
            ["--yes"],
            patch_actions=True,
            patch_upload=True,
            payload_verification_side_effect=[
                PayloadVerificationResult(True, "ok"),
                PayloadVerificationResult(False, "missing payload directory"),
            ],
            reboot_side_effect=AssertionError("deploy should not request reboot after post-sync verification failure"),
            raises=SystemExit,
        )

        self.assertEqual(
            str(result.exception),
            "managed payload verification failed at /Volumes/dk2/.samba4: missing payload directory",
        )
        result.mocks.flush_remote_filesystem_writes.assert_called_once()
        result.mocks.remote_request_reboot.assert_not_called()
        self.assertEqual(result.mocks.verify_payload_home_conn.call_count, 2)
        telemetry_error = self.telemetry_payload("deploy_finished")["error"]
        self.assertIn("stage=verify_payload_upload_after_sync", telemetry_error)
        self.assertIn("payload_post_sync_verification=missing payload directory", telemetry_error)

    def test_deploy_declined_reboot_returns_without_rebooting(self) -> None:
        result = self.run_deploy_cli(
            [],
            artifacts=[("smbd", True, "ok"), ("mdns", True, "ok")],
            patch_actions=True,
            patch_upload=True,
            reboot_side_effect=AssertionError("declined deploy should not request a reboot"),
            input_side_effect=["n"],
        )

        self.assertEqual(result.rc, 0)
        self.assertIn("Deployment complete without reboot.", result.text)
        result.mocks.remote_request_reboot.assert_not_called()
        self.assertEqual(result.mocks.verify_payload_home_conn.call_count, 2)
        result.mocks.flush_remote_filesystem_writes.assert_called_once()

    def test_deploy_reboot_timeout_returns_failure(self) -> None:
        result = self.run_deploy_cli(
            ["--yes"],
            artifacts=[("smbd", True, "ok"), ("mdns", True, "ok")],
            patch_actions=True,
            patch_upload=True,
            reboot_side_effect=SshCommandTimeout("reboot timed out"),
            wait_side_effect=[False],
            verify_runtime=self.managed_runtime_probe(True),
        )

        self.assertEqual(result.rc, 1)
        self.assertIn("SSH reboot request timed out; checking whether the device is rebooting...", result.text)
        self.assertIn(deploy.REBOOT_NO_DOWN_MESSAGE, result.text)
        result.mocks.verify_managed_runtime.assert_not_called()

    def test_deploy_failure_telemetry_includes_current_stage(self) -> None:
        result = self.run_deploy_cli(
            ["--yes"],
            patch_actions=True,
            patch_upload=True,
            upload_side_effect=RuntimeError("scp failed"),
            raises=RuntimeError,
        )

        self.assertEqual(str(result.exception), "scp failed")
        finished = self.telemetry_payload("deploy_finished")
        self.assertEqual(finished["result"], "failure")
        self.assertIn("stage=upload_payload", finished["error"])
        self.assertIn("RuntimeError: scp failed", finished["error"])

    def test_deploy_netbsd4_dry_run_json_outputs_activation_plan(self) -> None:
        result = self.run_deploy_cli(
            ["--dry-run", "--json"],
            artifacts=[("smbd-netbsd4le", True, "ok")],
            compatibility=self.make_supported_netbsd4_compatibility(),
        )
        self.assertEqual(result.rc, 0)
        payload = json.loads(result.text)
        self.assertFalse(payload["reboot_required"])
        self.assertEqual(
            [action["kind"] for action in payload["activation_actions"]],
            ["stop_watchdog", "stop_process", "stop_process", "stop_process", "stop_process", "run_script"],
        )
        self.assertEqual(
            [action["args"] for action in payload["activation_actions"]],
            [[], ["smbd"], ["mdns-advertiser"], ["nbns-advertiser"], ["wcifsfs"], ["/mnt/Flash/rc.local"]],
        )
        self.assertEqual(
            [check["id"] for check in payload["post_deploy_checks"]],
            [
                "netbsd4_runtime_smb_conf_present",
                "netbsd4_smbd_parent_process",
                "netbsd4_smbd_bound_445",
                "netbsd4_mdns_bound_5353",
            ],
        )

    def test_deploy_netbsd4_yes_runs_activation_and_skips_reboot(self) -> None:
        result = self.run_deploy_cli(
            ["--yes"],
            values=self.make_valid_env(TC_PAYLOAD_DIR_NAME="samba4"),
            artifacts=[("smbd-netbsd4le", True, "ok")],
            compatibility=self.make_supported_netbsd4_compatibility(),
            patch_actions=True,
            patch_upload=True,
            verify_runtime=self.managed_runtime_probe(True),
            reboot_side_effect=AssertionError("NetBSD4 activation should not request a reboot"),
        )

        self.assertEqual(result.rc, 0)
        self.assertEqual(result.mocks.run_remote_actions.call_count, 3)
        self.assertEqual(result.mocks.verify_payload_home_conn.call_count, 2)
        result.mocks.flush_remote_filesystem_writes.assert_called_once()
        result.mocks.remote_request_reboot.assert_not_called()
        self.assertIn("Activating NetBSD4 payload without reboot.", result.text)
        self.assertIn("NetBSD4 activation complete.", result.text)

    def test_deploy_rejects_unsupported_device(self) -> None:
        unsupported = DeviceCompatibility(
            os_name="Linux",
            os_release="6.8",
            arch="armv7",
            elf_endianness="unknown",
            payload_family=None,
            device_generation="unknown",
            supported=False,
            reason_code="unsupported_os",
        )
        result = self.run_deploy_cli(
            ["--dry-run"],
            values=self.make_valid_env(TC_PAYLOAD_DIR_NAME="samba4"),
            artifacts=[("smbd", True, "ok"), ("mdns", True, "ok")],
            compatibility=unsupported,
            raises=SystemExit,
        )

        self.assertIn("Linux", str(result.exception))

    def test_deploy_allow_unsupported_still_fails_without_payload_family(self) -> None:
        unsupported = DeviceCompatibility(
            os_name="Linux",
            os_release="6.8",
            arch="armv7",
            elf_endianness="unknown",
            payload_family=None,
            device_generation="unknown",
            supported=False,
            reason_code="unsupported_os",
        )
        result = self.run_deploy_cli(
            ["--dry-run", "--allow-unsupported"],
            values=self.make_valid_env(TC_PAYLOAD_DIR_NAME="samba4"),
            artifacts=[("smbd", True, "ok"), ("mdns", True, "ok")],
            compatibility=unsupported,
            raises=SystemExit,
        )

        text = str(result.exception)
        self.assertIn("Linux", text)
        self.assertIn("No deployable payload is available", text)

    def test_activate_dry_run_prints_netbsd4_activation_plan(self) -> None:
        output = io.StringIO()
        values = self.make_valid_env()
        with mock.patch("timecapsulesmb.cli.activate.load_env_config", return_value=self.make_app_config(values)):
            with mock.patch("timecapsulesmb.cli.context.CommandContext.require_compatibility", return_value=self.make_supported_netbsd4_compatibility()):
                with mock.patch("timecapsulesmb.cli.activate.run_remote_actions") as actions_mock:
                    with redirect_stdout(output):
                        rc = activate.main(["--dry-run"])
        self.assertEqual(rc, 0)
        actions_mock.assert_not_called()
        text = output.getvalue()
        self.assertIn("Dry run: NetBSD4 activation plan", text)
        self.assertIn("tc_kill_watchdog_pids TERM", text)
        self.assertNotIn("/usr/bin/pkill -f '[w]atchdog.sh'", text)
        self.assertIn("/usr/bin/pkill '^smbd$' >/dev/null 2>&1 || true", text)
        self.assertIn("/usr/bin/pkill '^mdns-advertiser$' >/dev/null 2>&1 || true", text)
        self.assertIn("/usr/bin/pkill '^nbns-advertiser$' >/dev/null 2>&1 || true", text)
        self.assertIn("/usr/bin/pkill '^wcifsfs$' >/dev/null 2>&1 || true", text)
        self.assertIn("/bin/sh /mnt/Flash/rc.local", text)
        self.assertIn("skip rc.local if NetBSD4 payload is already healthy", text)
        self.assertIn("managed runtime smb.conf is present", text)
        self.assertIn("managed smbd parent process is running", text)
        self.assertIn("smbd is bound to TCP 445", text)
        self.assertIn("mdns-advertiser is bound to UDP 5353", text)
        self.assertIn("This will start the deployed Samba payload on the Time Capsule 5th generation.", text)
        self.assertIn("NetBSD 4 devices cannot auto-run Samba after a reboot.", text)

    def test_activate_ensures_install_id_before_telemetry(self) -> None:
        output = io.StringIO()
        values = self.make_valid_env()
        with mock.patch("timecapsulesmb.cli.activate.ensure_install_id") as ensure_mock:
            with mock.patch("timecapsulesmb.cli.activate.load_env_config", return_value=self.make_app_config(values)):
                with mock.patch(
                    "timecapsulesmb.cli.activate.CommandContext",
                    return_value=FakeCommandContext(compatibility=self.make_supported_netbsd4_compatibility()),
                ):
                    with mock.patch("timecapsulesmb.cli.context.CommandContext.require_compatibility", return_value=self.make_supported_netbsd4_compatibility()):
                        with redirect_stdout(output):
                            rc = activate.main(["--dry-run"])
        self.assertEqual(rc, 0)
        ensure_mock.assert_called_once_with()

    def test_activate_rejects_non_netbsd4_device(self) -> None:
        values = self.make_valid_env()
        with mock.patch("timecapsulesmb.cli.activate.load_env_config", return_value=self.make_app_config(values)):
            with mock.patch("timecapsulesmb.cli.context.CommandContext.require_compatibility", return_value=self.make_supported_compatibility()):
                with self.assertRaises(SystemExit) as cm:
                    activate.main(["--dry-run"])
        self.assertIn("only supported for NetBSD4", str(cm.exception))

    def test_activate_rejects_missing_remote_interface(self) -> None:
        values = self.make_valid_env()
        candidates = RemoteInterfaceCandidatesProbeResult(
            candidates=(
                RemoteInterfaceCandidate(name="bcmeth1", ipv4_addrs=("10.0.0.2",), up=True, active=True, loopback=False),
            ),
            preferred_iface="bcmeth1",
            detail="preferred interface bcmeth1",
            target_ip_matches=(
                RemoteInterfaceCandidate(name="bcmeth1", ipv4_addrs=("10.0.0.2",), up=True, active=True, loopback=False),
            ),
        )
        with self.assertRaises(SystemExit) as ctx:
            with mock.patch("timecapsulesmb.cli.activate.load_env_config", return_value=self.make_app_config(values)):
                with mock.patch(
                    "timecapsulesmb.cli.runtime.probe_remote_interface_conn",
                    return_value=RemoteInterfaceProbeResult(
                        iface="bridge0",
                        exists=False,
                        detail="interface bridge0 was not found on the device",
                    ),
                ):
                    with mock.patch(
                        "timecapsulesmb.cli.runtime.probe_remote_interface_candidates_conn",
                        return_value=candidates,
                    ):
                        activate.main(["--dry-run"])
        self.assertIn("TC_NET_IFACE is invalid", str(ctx.exception))
        self.assertIn("bridge0 was not found", str(ctx.exception))
        self.assertIn("Found remote interfaces: bcmeth1=10.0.0.2.", str(ctx.exception))

    def test_managed_target_rejects_link_local_runtime_interface(self) -> None:
        config = self.make_app_config(self.make_valid_env())
        with mock.patch(
            "timecapsulesmb.cli.runtime.read_interface_ipv4_addrs_conn",
            return_value=("0.0.0.0", "169.254.44.9"),
        ):
            with self.assertRaises(ConfigError) as ctx:
                cli_runtime.resolve_validated_managed_target(
                    config,
                    command_name="deploy",
                    profile="deploy",
                    include_probe=False,
                )

        self.assertIn("TC_NET_IFACE is not usable", str(ctx.exception))
        self.assertIn("Reported IPv4 addresses on bridge0: 0.0.0.0, 169.254.44.9", str(ctx.exception))

    def test_managed_target_rejects_hostname_that_resolves_link_local(self) -> None:
        config = self.make_app_config(self.make_valid_env(TC_HOST="root@capsule.local"))
        addrinfo = [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("169.254.44.9", 0))]
        with mock.patch("timecapsulesmb.core.net.socket.getaddrinfo", return_value=addrinfo):
            with mock.patch(
                "timecapsulesmb.cli.runtime.probe_remote_interface_conn",
                side_effect=AssertionError("should fail before SSH probing"),
            ):
                with self.assertRaises(ConfigError) as ctx:
                    cli_runtime.resolve_validated_managed_target(
                        config,
                        command_name="deploy",
                        profile="deploy",
                        include_probe=False,
                    )

        self.assertIn("TC_HOST host capsule.local resolves to 169.254.x.x link-local IPv4 address 169.254.44.9", str(ctx.exception))

    def test_managed_target_allows_proxied_hostname_that_resolves_link_local(self) -> None:
        config = self.make_app_config(
            self.make_valid_env(
                TC_HOST="root@capsule.local",
                TC_SSH_OPTS="-o ProxyJump=bastion",
            )
        )
        with mock.patch("timecapsulesmb.core.net.socket.getaddrinfo", side_effect=AssertionError("should not resolve")):
            with mock.patch(
                "timecapsulesmb.cli.runtime.probe_remote_interface_conn",
                return_value=RemoteInterfaceProbeResult("bridge0", True, "interface bridge0 exists"),
            ):
                with mock.patch(
                    "timecapsulesmb.cli.runtime.read_interface_ipv4_addrs_conn",
                    return_value=("10.0.0.2",),
                ):
                    target = cli_runtime.resolve_validated_managed_target(
                        config,
                        command_name="deploy",
                        profile="deploy",
                        include_probe=False,
                    )

        self.assertEqual(target.connection.host, "root@capsule.local")

    def test_activate_prompt_decline_cancels_before_remote_actions(self) -> None:
        output = io.StringIO()
        command_context = FakeCommandContext(compatibility=self.make_supported_netbsd4_compatibility())
        values = self.make_valid_env()
        with mock.patch("timecapsulesmb.cli.activate.load_env_config", return_value=self.make_app_config(values)):
            with mock.patch("timecapsulesmb.cli.context.CommandContext.require_compatibility", return_value=self.make_supported_netbsd4_compatibility()):
                with mock.patch("builtins.input", return_value="n"):
                    with mock.patch("timecapsulesmb.cli.activate.run_remote_actions") as actions_mock:
                        with mock.patch("timecapsulesmb.cli.activate.CommandContext", return_value=command_context):
                            with redirect_stdout(output):
                                rc = activate.main([])
        self.assertEqual(rc, 0)
        actions_mock.assert_not_called()
        text = output.getvalue()
        self.assertIn("This will start the deployed Samba payload on the Time Capsule 5th generation.", text)
        self.assertIn("Activation cancelled.", text)
        command_context.finish.assert_called_once()
        self.assertEqual(command_context.finish.call_args.kwargs["result"], "cancelled")
        self.assertIn("Cancelled by user at NetBSD4 activation confirmation prompt.", command_context.finish.call_args.kwargs["error"])

    def test_activate_prompt_eof_reports_non_interactive_error(self) -> None:
        output = io.StringIO()
        command_context = FakeCommandContext(compatibility=self.make_supported_netbsd4_compatibility())
        values = self.make_valid_env()
        with mock.patch("timecapsulesmb.cli.activate.load_env_config", return_value=self.make_app_config(values)):
            with mock.patch("timecapsulesmb.cli.context.CommandContext.require_compatibility", return_value=self.make_supported_netbsd4_compatibility()):
                with mock.patch("builtins.input", side_effect=EOFError):
                    with mock.patch("timecapsulesmb.cli.activate.run_remote_actions") as actions_mock:
                        with mock.patch("timecapsulesmb.cli.activate.CommandContext", return_value=command_context):
                            with redirect_stdout(output):
                                rc = activate.main([])
        self.assertEqual(rc, 1)
        actions_mock.assert_not_called()
        message = "Running `activate` requires confirmation when stdin is not interactive. Use `activate --yes` in a non-interactive environment."
        self.assertIn(message, output.getvalue())
        command_context.finish.assert_called_once()
        self.assertEqual(command_context.finish.call_args.kwargs["result"], "failure")
        self.assertEqual(command_context.finish.call_args.kwargs["error"], message)

    def test_activate_yes_runs_idempotent_actions_and_verifies(self) -> None:
        output = io.StringIO()
        values = self.make_valid_env()
        with mock.patch("timecapsulesmb.cli.activate.load_env_config", return_value=self.make_app_config(values)):
            with mock.patch("timecapsulesmb.cli.context.CommandContext.require_compatibility", return_value=self.make_supported_netbsd4_compatibility()):
                with mock.patch("timecapsulesmb.cli.activate.probe_managed_runtime_conn", return_value=mock.Mock(ready=False)):
                    with mock.patch("timecapsulesmb.cli.activate.run_remote_actions") as actions_mock:
                        with mock.patch("timecapsulesmb.cli.flows.verify_managed_runtime", return_value=self.managed_runtime_probe(True)) as verify_mock:
                            with redirect_stdout(output):
                                rc = activate.main(["--yes"])
        self.assertEqual(rc, 0)
        actions_mock.assert_called_once()
        self.assertEqual(
            actions_mock.call_args.args[1],
            [
                StopWatchdogAction(),
                StopProcessAction("smbd"),
                StopProcessAction("mdns-advertiser"),
                StopProcessAction("nbns-advertiser"),
                StopProcessAction("wcifsfs"),
                RunScriptAction("/mnt/Flash/rc.local"),
            ],
        )
        self.assertEqual(actions_mock.call_args.kwargs, {})
        self.assertEqual(verify_mock.call_args.args[0].host, "root@10.0.0.2")
        self.assertEqual(verify_mock.call_args.kwargs["timeout_seconds"], 180)
        self.assertIn("without file transfer", output.getvalue())

    def test_main_registers_flash_command(self) -> None:
        self.assertIs(cli_main_module.COMMANDS["flash"], cli_flash.main)

    def test_flash_live_login_read_uses_binary_capture(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")
        payload = b"#!/bin/sh\n\xff"
        with mock.patch("timecapsulesmb.cli.flash.run_ssh_capture_bytes", return_value=payload) as capture_mock:
            self.assertEqual(cli_flash.read_live_login(connection), payload)
        capture_mock.assert_called_once_with(
            connection,
            "/bin/dd if=/etc/rc.d/LOGIN bs=4096 2>/dev/null",
            timeout=30,
        )

    def test_flash_target_resolution_uses_connection_only_config(self) -> None:
        config = self.make_app_config({
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
            "TC_NET_IFACE": "not a valid interface value",
            "TC_AIRPORT_SYAP": "not-a-syap",
            "TC_MDNS_DEVICE_MODEL": "not-a-model",
        })
        with mock.patch("timecapsulesmb.cli.runtime.probe_remote_interface_conn", side_effect=AssertionError("flash should not probe TC_NET_IFACE")):
            with mock.patch("timecapsulesmb.cli.runtime.probe_connection_state", side_effect=AssertionError("flash target resolution should not probe the device")):
                target = cli_runtime.resolve_validated_managed_target(
                    config,
                    command_name="flash",
                    profile="flash",
                    include_probe=True,
                )

        self.assertEqual(target.connection.host, "root@10.0.0.2")
        self.assertEqual(target.connection.password, "pw")
        self.assertEqual(target.connection.ssh_opts, "-o foo")
        self.assertIsNone(target.interface_probe)
        self.assertIsNone(target.probe_state)

    def test_flash_backup_dir_sanitizes_dot_only_path_parts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backup_dir = cli_flash.build_flash_backup_dir(base_dir=None, host="..", syap=".")

        self.assertEqual(backup_dir.parent, cli_flash.default_flash_backup_root())
        self.assertIn("-device-syAPdevice", backup_dir.name)
        self.assertNotIn("..", backup_dir.parts)
        self.assertNotIn(".", backup_dir.parts)

        explicit_dir = cli_flash.build_flash_backup_dir(base_dir=root / ".." / "chosen", host="..", syap=".")
        self.assertEqual(explicit_dir, (root / ".." / "chosen").resolve())

    def test_flash_read_only_saves_banks_and_manifest(self) -> None:
        output = io.StringIO()
        primary = self.make_flash_bank(release=b"NetBSD 4.0_STABLE #0: current")
        secondary = self.make_flash_bank(release=b"NetBSD 4.0_BETA2 #0: old")
        command_context = FakeCommandContext(compatibility=self.make_supported_netbsd4_stable_compatibility())
        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp) / "backup"
            with self.flash_zopfli_available():
                config = self.make_app_config(self.make_valid_env(
                    TC_NET_IFACE="bad iface from config",
                    TC_AIRPORT_SYAP="999",
                    TC_MDNS_DEVICE_MODEL="NotADevice",
                ))
                with mock.patch("timecapsulesmb.cli.flash.load_env_config", return_value=config):
                    with mock.patch("timecapsulesmb.cli.flash.CommandContext", return_value=command_context):
                        with mock.patch(
                            "timecapsulesmb.cli.flash.read_flash_inputs",
                            return_value=(
                                primary,
                                secondary,
                                self.flash_bank_checksum(primary),
                                self.flash_bank_checksum(secondary),
                                113,
                                STOCK_LOGIN_NETBSD4_DUMMY,
                            ),
                        ):
                            with redirect_stdout(output):
                                rc = cli_flash.main(["--read-only", "--backup-dir", str(backup_dir)])

            self.assertEqual(rc, 0)
            self.assertEqual((backup_dir / "primary.raw").read_bytes(), primary)
            self.assertEqual((backup_dir / "secondary.raw").read_bytes(), secondary)
            self.assertFalse((backup_dir / "primary.patched.raw").exists())
            self.assertFalse((backup_dir / "secondary.patched.raw").exists())
            manifest = json.loads((backup_dir / "manifest.json").read_text())

        self.assertEqual(manifest["active_bank"], "primary")
        self.assertEqual(manifest["write_policy"], "active_bank_only")
        self.assertEqual(manifest["syap"], "113")
        self.assertNotIn("primary_patched", manifest["files"])
        self.assertNotIn("secondary_patched", manifest["files"])
        self.assertFalse(manifest["banks"][0]["would_write"])
        self.assertFalse(manifest["banks"][1]["would_write"])
        self.assertEqual(manifest["banks"][0]["write_decision"], "backup only; no patch candidate built")
        self.assertEqual(manifest["banks"][1]["write_decision"], "backup only; no patch candidate built")
        self.assertEqual(manifest["live_login"]["sha256"], sha256_hex(STOCK_LOGIN_NETBSD4_DUMMY))
        self.assertIn("Backed up firmware banks to:", output.getvalue())
        self.assertNotIn("patch file=", output.getvalue())
        command_context.finish.assert_called_once()
        self.assertEqual(command_context.finish.call_args.kwargs["result"], "success")
        self.assertEqual(command_context.finish.call_args.kwargs["device_syap"], "113")
        self.assertEqual(command_context.finish.call_args.kwargs["device_model"], "TimeCapsule6,113")

    def test_flash_read_acp_error_is_reported_without_traceback(self) -> None:
        output = io.StringIO()
        primary = self.make_flash_bank(release=b"NetBSD 4.0_STABLE #0: current")
        secondary = self.make_flash_bank(release=b"NetBSD 4.0_BETA2 #0: old")
        command_context = FakeCommandContext(compatibility=self.make_supported_netbsd4_stable_compatibility())

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("timecapsulesmb.cli.flash.load_env_config", return_value=self.make_app_config(self.make_valid_env())):
                with mock.patch("timecapsulesmb.cli.flash.CommandContext", return_value=command_context):
                    with mock.patch("timecapsulesmb.cli.flash.dump_remote_bank", side_effect=[primary, secondary]) as dump_mock:
                        with mock.patch("timecapsulesmb.cli.flash.get_property_int", side_effect=ACPAuthError("ACP command failed with error_code -0x10")):
                            with mock.patch("timecapsulesmb.cli.flash.read_live_login", side_effect=AssertionError("LOGIN should not be read after ACP failure")) as login_mock:
                                with redirect_stdout(output):
                                    rc = cli_flash.main([
                                        "--read-only",
                                        "--backup-dir",
                                        str(Path(tmp) / "backup"),
                                    ])

        self.assertEqual(rc, 1)
        self.assertEqual(dump_mock.call_count, 2)
        login_mock.assert_not_called()
        self.assertIn("ACP property cks1 read failed", output.getvalue())
        finished = command_context.finish.call_args.kwargs
        self.assertEqual(finished["result"], "failure")
        self.assertIn("flash_error_stage=read_flash", finished["error"])
        self.assertIn("ACP command failed with error_code -0x10", finished["error"])
        self.assertNotIn("flash_error_stage", finished)
        self.assertNotIn("flash_error", finished)

    def test_flash_read_ssh_error_is_reported_without_traceback(self) -> None:
        output = io.StringIO()
        primary = self.make_flash_bank(release=b"NetBSD 4.0_STABLE #0: current")
        command_context = FakeCommandContext(compatibility=self.make_supported_netbsd4_stable_compatibility())

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("timecapsulesmb.cli.flash.load_env_config", return_value=self.make_app_config(self.make_valid_env())):
                with mock.patch("timecapsulesmb.cli.flash.CommandContext", return_value=command_context):
                    with mock.patch(
                        "timecapsulesmb.cli.flash.dump_remote_bank",
                        side_effect=[primary, SshError("ssh command failed with rc=255")],
                    ) as dump_mock:
                        with mock.patch("timecapsulesmb.cli.flash.get_property_int", side_effect=AssertionError("ACP should not be read after SSH failure")) as acp_mock:
                            with mock.patch("timecapsulesmb.cli.flash.read_live_login", side_effect=AssertionError("LOGIN should not be read after SSH failure")) as login_mock:
                                with redirect_stdout(output):
                                    rc = cli_flash.main([
                                        "--read-only",
                                        "--backup-dir",
                                        str(Path(tmp) / "backup"),
                                    ])

        self.assertEqual(rc, 1)
        self.assertEqual(dump_mock.call_count, 2)
        acp_mock.assert_not_called()
        login_mock.assert_not_called()
        self.assertIn("SSH flash read failed", output.getvalue())
        self.assertIn("ssh command failed with rc=255", output.getvalue())
        finished = command_context.finish.call_args.kwargs
        self.assertEqual(finished["result"], "failure")
        self.assertIn("flash_error_stage=read_flash", finished["error"])
        self.assertIn("SSH flash read failed", finished["error"])
        self.assertNotIn("flash_error_stage", finished)
        self.assertNotIn("flash_error", finished)

    def test_flash_analyze_error_is_reported_without_system_exit(self) -> None:
        output = io.StringIO()
        primary = self.make_flash_bank(release=b"NetBSD 4.0_STABLE #0: current")
        secondary = self.make_flash_bank(release=b"NetBSD 4.0_BETA2 #0: old")
        command_context = FakeCommandContext(compatibility=self.make_supported_netbsd4_stable_compatibility())

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("timecapsulesmb.cli.flash.load_env_config", return_value=self.make_app_config(self.make_valid_env())):
                with mock.patch("timecapsulesmb.cli.flash.CommandContext", return_value=command_context):
                    with mock.patch(
                        "timecapsulesmb.cli.flash.read_flash_inputs",
                        return_value=(
                            primary,
                            secondary,
                            self.flash_bank_checksum(primary),
                            self.flash_bank_checksum(secondary),
                            113,
                            STOCK_LOGIN_NETBSD4_DUMMY,
                        ),
                    ):
                        with mock.patch(
                            "timecapsulesmb.cli.flash.analyze_flash_banks",
                            side_effect=cli_flash.FlashAnalysisError("expected exactly one valid footer, found 0"),
                        ):
                            with redirect_stdout(output):
                                rc = cli_flash.main([
                                    "--read-only",
                                    "--backup-dir",
                                    str(Path(tmp) / "backup"),
                                ])

        self.assertEqual(rc, 1)
        self.assertIn("expected exactly one valid footer, found 0", output.getvalue())
        finished = command_context.finish.call_args.kwargs
        self.assertEqual(finished["result"], "failure")
        self.assertIn("flash_error_stage=analyze_flash", finished["error"])
        self.assertIn("expected exactly one valid footer, found 0", finished["error"])
        self.assertNotIn("flash_error_stage", finished)
        self.assertNotIn("flash_error", finished)

    def test_flash_refuses_when_probed_syap_is_missing(self) -> None:
        output = io.StringIO()
        primary = self.make_flash_bank(release=b"NetBSD 4.0_STABLE #0: current")
        secondary = self.make_flash_bank(release=b"NetBSD 4.0_BETA2 #0: old")
        command_context = FakeCommandContext(compatibility=self.make_supported_netbsd4_stable_compatibility())
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("timecapsulesmb.cli.flash.load_env_config", return_value=self.make_app_config(self.make_valid_env(TC_AIRPORT_SYAP="113"))):
                with mock.patch("timecapsulesmb.cli.flash.CommandContext", return_value=command_context):
                    with mock.patch(
                        "timecapsulesmb.cli.flash.read_flash_inputs",
                        return_value=(
                            primary,
                            secondary,
                            self.flash_bank_checksum(primary),
                            self.flash_bank_checksum(secondary),
                            None,
                            STOCK_LOGIN_NETBSD4_DUMMY,
                        ),
                    ):
                        with redirect_stdout(output):
                            rc = cli_flash.main(["--read-only", "--backup-dir", str(Path(tmp) / "backup")])

        self.assertEqual(rc, 1)
        self.assertIn("syAP is missing", output.getvalue())
        self.assertEqual(command_context.finish.call_args.kwargs["result"], "failure")
        self.assertIn("flash_error_stage=read_flash", command_context.finish.call_args.kwargs["error"])
        self.assertNotIn("flash_error_stage", command_context.finish.call_args.kwargs)

    def test_flash_uses_probed_zero_syap_without_falling_back_to_config(self) -> None:
        output = io.StringIO()
        primary = self.make_flash_bank(release=b"NetBSD 4.0_STABLE #0: current")
        secondary = self.make_flash_bank(release=b"NetBSD 4.0_BETA2 #0: old")
        command_context = FakeCommandContext(compatibility=self.make_supported_netbsd4_stable_compatibility())

        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp) / "backup"
            with self.flash_zopfli_available():
                with mock.patch(
                    "timecapsulesmb.cli.flash.load_env_config",
                    return_value=self.make_app_config(self.make_valid_env(TC_AIRPORT_SYAP="113")),
                ):
                    with mock.patch("timecapsulesmb.cli.flash.CommandContext", return_value=command_context):
                        with mock.patch(
                            "timecapsulesmb.cli.flash.read_flash_inputs",
                            return_value=(
                                primary,
                                secondary,
                                self.flash_bank_checksum(primary),
                                self.flash_bank_checksum(secondary),
                                0,
                                STOCK_LOGIN_NETBSD4_DUMMY,
                            ),
                        ):
                            with redirect_stdout(output):
                                rc = cli_flash.main(["--read-only", "--backup-dir", str(backup_dir)])
            manifest = json.loads((backup_dir / "manifest.json").read_text())

        self.assertEqual(rc, 0)
        self.assertEqual(manifest["syap"], "0")
        self.assertEqual(command_context.finish.call_args.kwargs["device_syap"], "0")
        self.assertNotIn("device_model", command_context.finish.call_args.kwargs)
        self.assertIn("Backed up firmware banks to:", output.getvalue())

    def test_flash_read_only_leaves_inactive_secondary_unmodified_when_it_fits(self) -> None:
        output = io.StringIO()
        primary = self.make_flash_bank(release=b"NetBSD 4.0_STABLE #0: current")
        secondary = self.make_flash_bank(release=b"NetBSD 4.0_BETA2 #0: old")
        command_context = FakeCommandContext(compatibility=self.make_supported_netbsd4_stable_compatibility())
        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp) / "backup"
            with self.flash_zopfli_available():
                with mock.patch("timecapsulesmb.cli.flash.load_env_config", return_value=self.make_app_config(self.make_valid_env())):
                    with mock.patch("timecapsulesmb.cli.flash.CommandContext", return_value=command_context):
                        with mock.patch(
                            "timecapsulesmb.cli.flash.read_flash_inputs",
                            return_value=(
                                primary,
                                secondary,
                                self.flash_bank_checksum(primary),
                                self.flash_bank_checksum(secondary),
                                113,
                                STOCK_LOGIN_NETBSD4_DUMMY,
                            ),
                        ):
                            with redirect_stdout(output):
                                rc = cli_flash.main(["--read-only", "--backup-dir", str(backup_dir)])

            secondary_patched_exists = (backup_dir / "secondary.patched.raw").is_file()
            manifest = json.loads((backup_dir / "manifest.json").read_text())

        self.assertEqual(rc, 0)
        self.assertFalse(secondary_patched_exists)
        self.assertNotIn("secondary_patched", manifest["files"])
        self.assertFalse(manifest["banks"][1]["would_write"])
        self.assertEqual(manifest["banks"][1]["write_decision"], "backup only; no patch candidate built")
        self.assertIsNone(manifest["banks"][1]["patch"])
        self.assertNotIn("secondary: patch", output.getvalue())

    def test_flash_read_only_saves_no_patch_when_secondary_is_active(self) -> None:
        output = io.StringIO()
        primary = self.make_flash_bank(release=b"NetBSD 4.0_BETA2 #0: old")
        secondary = self.make_flash_bank(release=b"NetBSD 4.0_STABLE #0: current")
        command_context = FakeCommandContext(compatibility=self.make_supported_netbsd4_stable_compatibility())
        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp) / "backup"
            with self.flash_zopfli_available():
                with mock.patch("timecapsulesmb.cli.flash.load_env_config", return_value=self.make_app_config(self.make_valid_env())):
                    with mock.patch("timecapsulesmb.cli.flash.CommandContext", return_value=command_context):
                        with mock.patch(
                            "timecapsulesmb.cli.flash.read_flash_inputs",
                            return_value=(
                                primary,
                                secondary,
                                self.flash_bank_checksum(primary),
                                self.flash_bank_checksum(secondary),
                                113,
                                STOCK_LOGIN_NETBSD4_DUMMY,
                            ),
                        ):
                            with redirect_stdout(output):
                                rc = cli_flash.main(["--read-only", "--backup-dir", str(backup_dir)])

            manifest = json.loads((backup_dir / "manifest.json").read_text())
            primary_patched_exists = (backup_dir / "primary.patched.raw").exists()
            secondary_patched_exists = (backup_dir / "secondary.patched.raw").is_file()

        self.assertEqual(rc, 0)
        self.assertEqual(manifest["active_bank"], "secondary")
        self.assertNotIn("primary_patched", manifest["files"])
        self.assertFalse(primary_patched_exists)
        self.assertFalse(secondary_patched_exists)
        self.assertNotIn("secondary_patched", manifest["files"])
        self.assertFalse(manifest["banks"][0]["would_write"])
        self.assertFalse(manifest["banks"][1]["would_write"])
        self.assertEqual(manifest["banks"][0]["write_decision"], "backup only; no patch candidate built")
        self.assertEqual(manifest["banks"][1]["write_decision"], "backup only; no patch candidate built")
        self.assertIn("secondary: size=", output.getvalue())
        self.assertNotIn("patch file=", output.getvalue())

    def test_flash_read_only_saves_no_patch_when_active_bank_is_unknown(self) -> None:
        output = io.StringIO()
        primary = self.make_flash_bank(release=b"NetBSD 4.0_STABLE #0: current")
        secondary = self.make_flash_bank(release=b"NetBSD 4.0_STABLE #0: current")
        command_context = FakeCommandContext(compatibility=self.make_supported_netbsd4_stable_compatibility())
        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp) / "backup"
            with self.flash_zopfli_available():
                with mock.patch("timecapsulesmb.cli.flash.load_env_config", return_value=self.make_app_config(self.make_valid_env())):
                    with mock.patch("timecapsulesmb.cli.flash.CommandContext", return_value=command_context):
                        with mock.patch(
                            "timecapsulesmb.cli.flash.read_flash_inputs",
                            return_value=(
                                primary,
                                secondary,
                                self.flash_bank_checksum(primary),
                                self.flash_bank_checksum(secondary),
                                113,
                                STOCK_LOGIN_NETBSD4_DUMMY,
                            ),
                        ):
                            with redirect_stdout(output):
                                rc = cli_flash.main(["--read-only", "--backup-dir", str(backup_dir)])

            manifest = json.loads((backup_dir / "manifest.json").read_text())
            primary_patched_exists = (backup_dir / "primary.patched.raw").exists()
            secondary_patched_exists = (backup_dir / "secondary.patched.raw").exists()

        self.assertEqual(rc, 0)
        self.assertIsNone(manifest["active_bank"])
        self.assertNotIn("primary_patched", manifest["files"])
        self.assertNotIn("secondary_patched", manifest["files"])
        self.assertFalse(primary_patched_exists)
        self.assertFalse(secondary_patched_exists)
        self.assertFalse(manifest["banks"][0]["would_write"])
        self.assertFalse(manifest["banks"][1]["would_write"])
        self.assertEqual(manifest["banks"][0]["write_decision"], "backup only; no patch candidate built")
        self.assertEqual(manifest["banks"][1]["write_decision"], "backup only; no patch candidate built")
        self.assertIsNone(manifest["banks"][0]["patch"])
        self.assertIsNone(manifest["banks"][1]["patch"])
        self.assertNotIn("patch file=", output.getvalue())

    def test_flash_patch_refuses_multiple_active_candidates_without_override(self) -> None:
        output = io.StringIO()
        primary = self.make_flash_bank(release=b"NetBSD 4.0_STABLE #0: current")
        secondary = self.make_flash_bank(release=b"NetBSD 4.0_STABLE #0: current")
        command_context = FakeCommandContext(compatibility=self.make_supported_netbsd4_stable_compatibility())
        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp) / "backup"
            with self.flash_zopfli_available():
                with mock.patch("timecapsulesmb.cli.flash.load_env_config", return_value=self.make_app_config(self.make_valid_env())):
                    with mock.patch("timecapsulesmb.cli.flash.CommandContext", return_value=command_context):
                        with mock.patch(
                            "timecapsulesmb.cli.flash.read_flash_inputs",
                            return_value=(
                                primary,
                                secondary,
                                self.flash_bank_checksum(primary),
                                self.flash_bank_checksum(secondary),
                                113,
                                STOCK_LOGIN_NETBSD4_DUMMY,
                            ),
                        ):
                            with mock.patch("timecapsulesmb.cli.flash.flash_firmware_bank") as flash_mock:
                                with redirect_stdout(output):
                                    rc = cli_flash.main(["--patch", "--yes", "--backup-dir", str(backup_dir)])
            manifest = json.loads((backup_dir / "manifest.json").read_text())

        self.assertEqual(rc, 1)
        flash_mock.assert_not_called()
        self.assertEqual(manifest["active_selection"]["status"], "multiple_candidates")
        self.assertEqual(manifest["active_selection"]["candidates"], ["primary", "secondary"])
        self.assertIn("multiple firmware banks passed active selection checks: primary, secondary", output.getvalue())
        self.assertIn("multiple firmware banks passed active selection checks", manifest["flash_plan_error"]["message"])

    def test_flash_patch_refuses_when_no_active_candidates_pass(self) -> None:
        output = io.StringIO()
        primary = self.make_flash_bank(release=b"NetBSD 4.0_BETA2 #0: old")
        secondary = self.make_flash_bank(release=b"NetBSD 4.0_BETA2 #0: old")
        command_context = FakeCommandContext(compatibility=self.make_supported_netbsd4_stable_compatibility())
        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp) / "backup"
            with self.flash_zopfli_available():
                with mock.patch("timecapsulesmb.cli.flash.load_env_config", return_value=self.make_app_config(self.make_valid_env())):
                    with mock.patch("timecapsulesmb.cli.flash.CommandContext", return_value=command_context):
                        with mock.patch(
                            "timecapsulesmb.cli.flash.read_flash_inputs",
                            return_value=(
                                primary,
                                secondary,
                                self.flash_bank_checksum(primary),
                                self.flash_bank_checksum(secondary),
                                113,
                                STOCK_LOGIN_NETBSD4_DUMMY,
                            ),
                        ):
                            with mock.patch("timecapsulesmb.cli.flash.flash_firmware_bank") as flash_mock:
                                with redirect_stdout(output):
                                    rc = cli_flash.main(["--patch", "--yes", "--backup-dir", str(backup_dir)])
            manifest = json.loads((backup_dir / "manifest.json").read_text())

        self.assertEqual(rc, 1)
        flash_mock.assert_not_called()
        self.assertEqual(manifest["active_selection"]["status"], "no_candidates")
        self.assertEqual(manifest["active_selection"]["candidates"], [])
        self.assertIn("no firmware bank passed active selection checks", output.getvalue())
        self.assertIn("primary rejected: running OS release", output.getvalue())
        self.assertIn("secondary rejected: running OS release", output.getvalue())

    def test_flash_read_only_json_outputs_manifest(self) -> None:
        output = io.StringIO()
        primary = self.make_flash_bank(release=b"NetBSD 4.0_STABLE #0: current")
        secondary = self.make_flash_bank(release=b"NetBSD 4.0_BETA2 #0: old")
        command_context = FakeCommandContext(compatibility=self.make_supported_netbsd4_stable_compatibility())
        with tempfile.TemporaryDirectory() as tmp:
            with self.flash_zopfli_available():
                with mock.patch("timecapsulesmb.cli.flash.load_env_config", return_value=self.make_app_config(self.make_valid_env())):
                    with mock.patch("timecapsulesmb.cli.flash.CommandContext", return_value=command_context):
                        with mock.patch(
                            "timecapsulesmb.cli.flash.read_flash_inputs",
                            return_value=(
                                primary,
                                secondary,
                                self.flash_bank_checksum(primary),
                                self.flash_bank_checksum(secondary),
                                113,
                                STOCK_LOGIN_NETBSD4_DUMMY,
                            ),
                        ):
                            with redirect_stdout(output):
                                rc = cli_flash.main(["--read-only", "--json", "--backup-dir", str(Path(tmp) / "backup")])

        self.assertEqual(rc, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["active_bank"], "primary")
        self.assertEqual(payload["write_policy"], "active_bank_only")
        self.assertEqual(payload["banks"][0]["login"]["classification"], "stock")
        self.assertFalse(payload["banks"][0]["would_write"])
        self.assertFalse(payload["banks"][1]["would_write"])
        self.assertEqual(payload["banks"][0]["write_decision"], "backup only; no patch candidate built")
        self.assertEqual(payload["banks"][1]["write_decision"], "backup only; no patch candidate built")
        self.assertNotIn("primary_patched", payload["files"])
        self.assertNotIn("secondary_patched", payload["files"])

    def test_flash_read_only_rejects_yes(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as raised:
                cli_flash.main(["--yes"])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("--yes is only valid with --patch or --restore", stderr.getvalue())

    def test_flash_patch_missing_zopfli_fails_before_config_or_device_reads(self) -> None:
        output = io.StringIO()
        missing_zopfli = RuntimeError(
            "Python package zopfli is required for flash patch compression. "
            "Run `./tcapsule bootstrap` to install it, then rerun `.venv/bin/tcapsule flash`."
        )
        with mock.patch("timecapsulesmb.flash.require_python_module", side_effect=missing_zopfli):
            with mock.patch("timecapsulesmb.cli.flash.ensure_install_id") as ensure_mock:
                with mock.patch("timecapsulesmb.cli.flash.load_env_config") as load_mock:
                    with mock.patch("timecapsulesmb.cli.flash.CommandContext") as context_mock:
                        with mock.patch("timecapsulesmb.cli.flash.read_flash_inputs") as read_mock:
                            with redirect_stdout(output):
                                with self.assertRaises(SystemExit) as raised:
                                    cli_flash.main(["--patch"])

        self.assertIn("Python package zopfli is required", str(raised.exception))
        self.assertIn("./tcapsule bootstrap", str(raised.exception))
        self.assertIn(".venv/bin/tcapsule flash", str(raised.exception))
        self.assertEqual(output.getvalue(), "")
        ensure_mock.assert_not_called()
        load_mock.assert_not_called()
        context_mock.assert_not_called()
        read_mock.assert_not_called()

    def test_flash_read_only_does_not_require_zopfli(self) -> None:
        output = io.StringIO()
        primary = self.make_flash_bank(release=b"NetBSD 4.0_STABLE #0: current")
        secondary = self.make_flash_bank(release=b"NetBSD 4.0_BETA2 #0: old")
        command_context = FakeCommandContext(compatibility=self.make_supported_netbsd4_stable_compatibility())
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("timecapsulesmb.flash.require_python_module", side_effect=AssertionError("zopfli not needed")):
                with mock.patch("timecapsulesmb.cli.flash.load_env_config", return_value=self.make_app_config(self.make_valid_env())):
                    with mock.patch("timecapsulesmb.cli.flash.CommandContext", return_value=command_context):
                        with mock.patch(
                            "timecapsulesmb.cli.flash.read_flash_inputs",
                            return_value=(
                                primary,
                                secondary,
                                self.flash_bank_checksum(primary),
                                self.flash_bank_checksum(secondary),
                                113,
                                STOCK_LOGIN_NETBSD4_DUMMY,
                            ),
                        ):
                            with redirect_stdout(output):
                                rc = cli_flash.main(["--read-only", "--backup-dir", str(Path(tmp) / "backup")])

        self.assertEqual(rc, 0)
        self.assertIn("Backed up firmware banks to:", output.getvalue())

    def test_flash_write_prompt_decline_cancels_without_write(self) -> None:
        output = io.StringIO()
        primary = self.make_flash_bank(release=b"NetBSD 4.0_STABLE #0: current")
        secondary = self.make_flash_bank(release=b"NetBSD 4.0_BETA2 #0: old")
        command_context = FakeCommandContext(compatibility=self.make_supported_netbsd4_stable_compatibility())
        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp) / "backup"
            template_path = Path(tmp) / "7.8.1.basebinary"
            template_path.write_bytes(self.make_firmware_template(primary, product_id=113))
            with self.flash_zopfli_available():
                with mock.patch("timecapsulesmb.cli.flash.load_env_config", return_value=self.make_app_config(self.make_valid_env())):
                    with mock.patch("timecapsulesmb.cli.flash.CommandContext", return_value=command_context):
                        with mock.patch(
                            "timecapsulesmb.cli.flash.read_flash_inputs",
                            return_value=(
                                primary,
                                secondary,
                                self.flash_bank_checksum(primary),
                                self.flash_bank_checksum(secondary),
                                113,
                                STOCK_LOGIN_NETBSD4_DUMMY,
                            ),
                        ):
                            with mock.patch("builtins.input", return_value="n"):
                                with redirect_stdout(output):
                                        rc = cli_flash.main([
                                            "--patch",
                                            "--firmware-template",
                                            str(template_path),
                                            "--backup-dir",
                                            str(backup_dir),
                                        ])
            manifest = json.loads((backup_dir / "manifest.json").read_text())

        self.assertEqual(rc, 0)
        self.assertIn("Flash write cancelled.", output.getvalue())
        self.assertNotIn("secondary: patch", output.getvalue())
        self.assertEqual(manifest["write_outcome"]["status"], "cancelled")
        self.assertFalse(manifest["write_outcome"]["write_may_have_modified_device"])
        self.assertEqual(command_context.finish.call_args.kwargs["result"], "cancelled")

    def test_flash_yes_without_write_mode_rejects(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as raised:
                cli_flash.main(["--yes"])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("--yes is only valid with --patch or --restore", stderr.getvalue())

    def test_build_acp_flash_payload_for_active_bank_uses_matching_template(self) -> None:
        primary = self.make_flash_bank(release=b"NetBSD 4.0_STABLE #0: current")
        secondary = self.make_flash_bank(release=b"NetBSD 4.0_BETA2 #0: old")
        with tempfile.TemporaryDirectory() as tmp:
            template_path = Path(tmp) / "7.8.1.basebinary"
            template_path.write_bytes(self.make_firmware_template(primary, product_id=113))
            with self.flash_zopfli_available():
                analysis = cli_flash.analyze_flash_banks(
                    primary_data=primary,
                    secondary_data=secondary,
                    cks1=self.flash_bank_checksum(primary),
                    cks2=self.flash_bank_checksum(secondary),
                    os_release="4.0_STABLE",
                )
            active = cli_flash.require_write_ready(analysis)

            payload = cli_flash.build_acp_flash_payload_for_active_bank(
                active,
                syap="113",
                firmware_template=template_path,
                cache_dir=Path(tmp) / "cache",
            )

        assert active.patch is not None
        reparsed = parse_nested_basebinary(payload.data)
        self.assertEqual(payload.key_id, "observed-k30a-78100")
        self.assertEqual(payload.inner_model, 113)
        self.assertEqual(reparsed.inner.payload, active.patch.target_bank[: active.footer.end_offset])
        self.assertEqual(payload.template_sha256, sha256_hex(self.make_firmware_template(primary, product_id=113)))

    def test_build_acp_flash_payload_auto_downloads_matching_template_by_syap(self) -> None:
        primary = self.make_flash_bank(release=b"NetBSD 4.0_STABLE #0: current")
        secondary = self.make_flash_bank(release=b"NetBSD 4.0_BETA2 #0: old")
        template = self.make_firmware_template(primary, product_id=113)
        catalog = plistlib.dumps({
            "firmwareUpdates": [
                {
                    "productID": "113",
                    "version": "7.8.1",
                    "location": "http://example.invalid/113/7.8.1.basebinary",
                    "sizeInBytes": len(template),
                    "newest": True,
                }
            ]
        })

        def fake_download(url: str, **_kwargs: object) -> bytes:
            if url == cli_flash.APPLE_FIRMWARE_CATALOG_URL:
                return catalog
            self.assertEqual(url, "http://example.invalid/113/7.8.1.basebinary")
            return template

        with tempfile.TemporaryDirectory() as tmp:
            with self.flash_zopfli_available():
                analysis = cli_flash.analyze_flash_banks(
                    primary_data=primary,
                    secondary_data=secondary,
                    cks1=self.flash_bank_checksum(primary),
                    cks2=self.flash_bank_checksum(secondary),
                    os_release="4.0_STABLE",
                )
            active = cli_flash.require_write_ready(analysis)

            with mock.patch("timecapsulesmb.apple_firmware.download_url", side_effect=fake_download) as download_mock:
                payload = cli_flash.build_acp_flash_payload_for_active_bank(
                    active,
                    syap="113",
                    firmware_template=None,
                    cache_dir=Path(tmp) / "cache",
                )

            cached_templates = list((Path(tmp) / "cache" / "113").glob("*.basebinary"))

        self.assertEqual(download_mock.call_count, 2)
        self.assertEqual(len(cached_templates), 1)
        self.assertEqual(payload.template_source, "http://example.invalid/113/7.8.1.basebinary")
        self.assertEqual(payload.template_product_id, "113")
        self.assertEqual(payload.template_version, "7.8.1")

    def test_build_acp_flash_payload_redownloads_corrupt_cached_template(self) -> None:
        primary = self.make_flash_bank(release=b"NetBSD 4.0_STABLE #0: current")
        secondary = self.make_flash_bank(release=b"NetBSD 4.0_BETA2 #0: old")
        template = self.make_firmware_template(primary, product_id=113)
        template_url = "http://example.invalid/113/7.8.1.basebinary"
        catalog = plistlib.dumps({
            "firmwareUpdates": [
                {
                    "productID": "113",
                    "version": "7.8.1",
                    "location": template_url,
                    "sizeInBytes": len(template),
                    "newest": True,
                }
            ]
        })
        calls: list[str] = []

        def fake_download(url: str, **_kwargs: object) -> bytes:
            calls.append(url)
            if url == cli_flash.APPLE_FIRMWARE_CATALOG_URL:
                return catalog
            self.assertEqual(url, template_url)
            return template

        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "cache"
            cached_path = apple_firmware.firmware_template_cache_path(
                cache_dir=cache_dir,
                product_id="113",
                version="7.8.1",
                url=template_url,
            )
            cached_path.parent.mkdir(parents=True)
            cached_path.write_bytes(b"\x00" * len(template))
            with self.flash_zopfli_available():
                analysis = cli_flash.analyze_flash_banks(
                    primary_data=primary,
                    secondary_data=secondary,
                    cks1=self.flash_bank_checksum(primary),
                    cks2=self.flash_bank_checksum(secondary),
                    os_release="4.0_STABLE",
                )
            active = cli_flash.require_write_ready(analysis)

            with mock.patch("timecapsulesmb.apple_firmware.download_url", side_effect=fake_download):
                payload = cli_flash.build_acp_flash_payload_for_active_bank(
                    active,
                    syap="113",
                    firmware_template=None,
                    cache_dir=cache_dir,
                )
            refreshed_cache = cached_path.read_bytes()

        self.assertEqual(calls, [cli_flash.APPLE_FIRMWARE_CATALOG_URL, template_url])
        self.assertEqual(refreshed_cache, template)
        self.assertEqual(payload.template_sha256, sha256_hex(template))

    def test_check_apple_redownloads_corrupt_cached_template(self) -> None:
        primary = self.make_flash_bank(release=b"NetBSD 4.0_STABLE #0: current")
        secondary = self.make_flash_bank(release=b"NetBSD 4.0_BETA2 #0: old")
        template = self.make_firmware_template(primary, product_id=113)
        template_url = "http://example.invalid/113/7.8.1.basebinary"
        catalog = plistlib.dumps({
            "firmwareUpdates": [
                {
                    "productID": "113",
                    "version": "7.8.1",
                    "location": template_url,
                    "sizeInBytes": len(template),
                    "newest": True,
                }
            ]
        })
        calls: list[str] = []

        def fake_download(url: str, **_kwargs: object) -> bytes:
            calls.append(url)
            if url == cli_flash.APPLE_FIRMWARE_CATALOG_URL:
                return catalog
            self.assertEqual(url, template_url)
            return template

        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "cache"
            cached_path = apple_firmware.firmware_template_cache_path(
                cache_dir=cache_dir,
                product_id="113",
                version="7.8.1",
                url=template_url,
            )
            cached_path.parent.mkdir(parents=True)
            cached_path.write_bytes(b"\x00" * len(template))
            with self.flash_zopfli_available():
                analysis = cli_flash.analyze_flash_banks(
                    primary_data=primary,
                    secondary_data=secondary,
                    cks1=self.flash_bank_checksum(primary),
                    cks2=self.flash_bank_checksum(secondary),
                    os_release="4.0_STABLE",
                )
            active = cli_flash.require_write_ready(analysis)

            with mock.patch("timecapsulesmb.apple_firmware.download_url", side_effect=fake_download):
                match = find_apple_firmware_match(
                    active,
                    syap="113",
                    firmware_template=None,
                    cache_dir=cache_dir,
                )
            refreshed_cache = cached_path.read_bytes()

        self.assertEqual(calls, [cli_flash.APPLE_FIRMWARE_CATALOG_URL, template_url])
        self.assertEqual(refreshed_cache, template)
        self.assertTrue(match.matched)
        self.assertEqual(match.template_sha256, sha256_hex(template))

    def test_build_acp_flash_payload_refuses_template_that_does_not_match_live_bank(self) -> None:
        primary = self.make_flash_bank(release=b"NetBSD 4.0_STABLE #0: current")
        secondary = self.make_flash_bank(release=b"NetBSD 4.0_BETA2 #0: old")
        with tempfile.TemporaryDirectory() as tmp:
            template_path = Path(tmp) / "7.8.1.basebinary"
            template = parse_nested_basebinary(self.make_firmware_template(primary, product_id=113))
            modified_payload = bytes([template.inner.payload[0] ^ 0x01]) + template.inner.payload[1:]
            modified_inner = compose_basebinary(template.inner.header, modified_payload, key=template.inner.key)
            template_path.write_bytes(compose_basebinary(template.outer.header, modified_inner, key=template.outer.key))
            with self.flash_zopfli_available():
                analysis = cli_flash.analyze_flash_banks(
                    primary_data=primary,
                    secondary_data=secondary,
                    cks1=self.flash_bank_checksum(primary),
                    cks2=self.flash_bank_checksum(secondary),
                    os_release="4.0_STABLE",
                )
            active = cli_flash.require_write_ready(analysis)

            with self.assertRaises(cli_flash.FlashAnalysisError) as raised:
                cli_flash.build_acp_flash_payload_for_active_bank(
                    active,
                    syap="113",
                    firmware_template=template_path,
                    cache_dir=Path(tmp) / "cache",
                )

        self.assertIn("does not match the live active bank", str(raised.exception))

    def test_build_acp_flash_payload_refuses_unknown_key_with_issue_url(self) -> None:
        primary = self.make_flash_bank(release=b"NetBSD 4.0_STABLE #0: current")
        secondary = self.make_flash_bank(release=b"NetBSD 4.0_BETA2 #0: old")
        unknown_key = BasebinaryKey.from_hex("unknown-test", "00112233445566778899aabbccddeeff")
        with tempfile.TemporaryDirectory() as tmp:
            template_path = Path(tmp) / "7.8.1.basebinary"
            template_path.write_bytes(self.make_firmware_template(primary, product_id=113, key=unknown_key))
            with self.flash_zopfli_available():
                analysis = cli_flash.analyze_flash_banks(
                    primary_data=primary,
                    secondary_data=secondary,
                    cks1=self.flash_bank_checksum(primary),
                    cks2=self.flash_bank_checksum(secondary),
                    os_release="4.0_STABLE",
                )
            active = cli_flash.require_write_ready(analysis)

            with self.assertRaises(cli_flash.FlashAnalysisError) as raised:
                cli_flash.build_acp_flash_payload_for_active_bank(
                    active,
                    syap="113",
                    firmware_template=template_path,
                    cache_dir=Path(tmp) / "cache",
                )

        self.assertIn("do not have firmware encryption keys", str(raised.exception))
        self.assertIn("https://github.com/jamesyc/TimeCapsuleSMB/issues", str(raised.exception))

    def test_flash_write_refuses_unsupported_firmware_key_before_acp(self) -> None:
        output = io.StringIO()
        primary = self.make_flash_bank(release=b"NetBSD 4.0_STABLE #0: current")
        secondary = self.make_flash_bank(release=b"NetBSD 4.0_BETA2 #0: old")
        command_context = FakeCommandContext(compatibility=self.make_supported_netbsd4_stable_compatibility())
        unknown_key = BasebinaryKey.from_hex("unknown-test", "00112233445566778899aabbccddeeff")
        unsupported_template = cli_flash.FirmwareTemplateCandidate(
            data=self.make_firmware_template(primary, product_id=113, key=unknown_key),
            source="test-unsupported.basebinary",
            path=None,
            product_id="113",
            version="7.8.1",
        )

        with tempfile.TemporaryDirectory() as tmp:
            with self.flash_zopfli_available():
                with mock.patch("timecapsulesmb.cli.flash.load_env_config", return_value=self.make_app_config(self.make_valid_env())):
                    with mock.patch("timecapsulesmb.cli.flash.CommandContext", return_value=command_context):
                        with mock.patch(
                            "timecapsulesmb.cli.flash.read_flash_inputs",
                            return_value=(
                                primary,
                                secondary,
                                self.flash_bank_checksum(primary),
                                self.flash_bank_checksum(secondary),
                                113,
                                STOCK_LOGIN_NETBSD4_DUMMY,
                            ),
                        ):
                            with mock.patch("timecapsulesmb.flash_payloads.resolve_firmware_template_candidates", return_value=[unsupported_template]):
                                with mock.patch("timecapsulesmb.cli.flash.flash_firmware_bank") as flash_mock:
                                    with redirect_stdout(output):
                                        rc = cli_flash.main([
                                            "--patch",
                                            "--yes",
                                            "--backup-dir",
                                            str(Path(tmp) / "backup"),
                                        ])

        self.assertEqual(rc, 1)
        flash_mock.assert_not_called()
        self.assertIn("do not have firmware encryption keys", output.getvalue())
        self.assertIn("https://github.com/jamesyc/TimeCapsuleSMB/issues", output.getvalue())
        finished = command_context.finish.call_args.kwargs
        self.assertEqual(finished["result"], "failure")
        self.assertIn("flash_error_stage=plan_flash", finished["error"])
        self.assertIn("do not have firmware encryption keys", finished["error"])
        self.assertNotIn("flash_error_stage", finished)
        self.assertNotIn("flash_error", finished)

    def test_flash_write_validates_active_bank_readback_and_stops_before_reboot(self) -> None:
        output = io.StringIO()
        primary = self.make_flash_bank(release=b"NetBSD 4.0_STABLE #0: current")
        secondary = self.make_flash_bank(release=b"NetBSD 4.0_BETA2 #0: old")
        command_context = FakeCommandContext(compatibility=self.make_supported_netbsd4_stable_compatibility())
        written: dict[str, bytes] = {}

        def fake_flash(_host: str, _password: str, bank_name: str, payload: bytes, **_kwargs: object) -> SimpleNamespace:
            written["bank_name"] = bank_name.encode()
            written["payload"] = payload
            return SimpleNamespace(command=0x03, reply_body=b"")

        def fake_get_property(_host: str, _password: str, name: str, **_kwargs: object) -> int:
            self.assertEqual(name, "cks1")
            return self.flash_bank_checksum(fake_readback(None, ""))

        def fake_readback(_conn: object, _dev: str) -> bytes:
            reparsed = parse_nested_basebinary(written["payload"])
            end_offset = self.flash_bank_end_offset(primary)
            rebuilt = bytearray(primary)
            rebuilt[:end_offset] = reparsed.inner.payload
            checksum = zlib.adler32(bytes(rebuilt[:end_offset])) & 0xFFFFFFFF
            for offset in range(max(0, len(primary) - 4096), len(primary) - 7):
                _old_checksum, candidate_end = struct.unpack(">II", primary[offset : offset + 8])
                if candidate_end == end_offset:
                    rebuilt[offset : offset + 4] = struct.pack(">I", checksum)
                    return bytes(rebuilt)
            self.fail("synthetic flash bank footer not found")

        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp) / "backup"
            template_path = Path(tmp) / "7.8.1.basebinary"
            template_path.write_bytes(self.make_firmware_template(primary, product_id=113))
            with self.flash_zopfli_available():
                with mock.patch("timecapsulesmb.cli.flash.load_env_config", return_value=self.make_app_config(self.make_valid_env())):
                    with mock.patch("timecapsulesmb.cli.flash.CommandContext", return_value=command_context):
                        with mock.patch(
                            "timecapsulesmb.cli.flash.read_flash_inputs",
                            return_value=(
                                primary,
                                secondary,
                                self.flash_bank_checksum(primary),
                                self.flash_bank_checksum(secondary),
                                113,
                                STOCK_LOGIN_NETBSD4_DUMMY,
                            ),
                        ):
                            with mock.patch("timecapsulesmb.cli.flash.flash_firmware_bank", side_effect=fake_flash) as flash_mock:
                                with mock.patch("timecapsulesmb.cli.flash.dump_remote_bank", side_effect=fake_readback):
                                    with mock.patch("timecapsulesmb.cli.flash.get_property_int", side_effect=fake_get_property):
                                        with mock.patch("timecapsulesmb.cli.flows.remote_request_reboot") as reboot_mock:
                                            with redirect_stdout(output):
                                                rc = cli_flash.main([
                                                    "--patch",
                                                    "--yes",
                                                    "--firmware-template",
                                                    str(template_path),
                                                    "--backup-dir",
                                                    str(backup_dir),
                                                ])
            manifest = json.loads((backup_dir / "manifest.json").read_text())
            payload_file_exists = (backup_dir / "primary.patched.basebinary").is_file()

        self.assertEqual(rc, 0)
        flash_mock.assert_called_once()
        self.assertEqual(written["bank_name"], b"primary")
        reparsed_payload = parse_nested_basebinary(written["payload"])
        self.assertEqual(reparsed_payload.inner.payload, fake_readback(None, "")[: self.flash_bank_end_offset(primary)])
        self.assertTrue(payload_file_exists)
        reboot_mock.assert_not_called()
        self.assertEqual(manifest["write_outcome"]["status"], "validated")
        self.assertTrue(manifest["write_outcome"]["write_may_have_modified_device"])
        self.assertEqual(manifest["write_result"]["bank"], "primary")
        self.assertEqual(manifest["write_result"]["login_classification"], "already_patched")
        self.assertEqual(manifest["write_result"]["expected_bank_sha256"], manifest["write_result"]["readback_sha256"])
        self.assertEqual(manifest["flash_plan"]["payload"]["key_id"], "observed-k30a-78100")
        self.assertIn("POWER-CYCLE REQUIRED", output.getvalue())
        self.assertIn("Patch write successful.\x1b[0m The device needs to be manually rebooted.", output.getvalue())
        self.assertEqual(command_context.finish.call_args.kwargs["result"], "success")

    def test_flash_active_bank_override_writes_requested_primary_candidate(self) -> None:
        output = io.StringIO()
        primary = self.make_flash_bank(release=b"NetBSD 4.0_STABLE #0: current")
        secondary = self.make_flash_bank(release=b"NetBSD 4.0_STABLE #0: current")
        command_context = FakeCommandContext(compatibility=self.make_supported_netbsd4_stable_compatibility())
        written: dict[str, bytes] = {}

        def fake_flash(_host: str, _password: str, bank_name: str, payload: bytes, **_kwargs: object) -> SimpleNamespace:
            written["bank_name"] = bank_name.encode()
            written["payload"] = payload
            return SimpleNamespace(command=0x03, reply_body=b"")

        def fake_get_property(_host: str, _password: str, name: str, **_kwargs: object) -> int:
            self.assertEqual(name, "cks1")
            return self.flash_bank_checksum(fake_readback(None, ""))

        def fake_readback(_conn: object, _dev: str) -> bytes:
            reparsed = parse_nested_basebinary(written["payload"])
            end_offset = self.flash_bank_end_offset(primary)
            rebuilt = bytearray(primary)
            rebuilt[:end_offset] = reparsed.inner.payload
            checksum = zlib.adler32(bytes(rebuilt[:end_offset])) & 0xFFFFFFFF
            for offset in range(max(0, len(primary) - 4096), len(primary) - 7):
                _old_checksum, candidate_end = struct.unpack(">II", primary[offset : offset + 8])
                if candidate_end == end_offset:
                    rebuilt[offset : offset + 4] = struct.pack(">I", checksum)
                    return bytes(rebuilt)
            self.fail("synthetic flash bank footer not found")

        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp) / "backup"
            template_path = Path(tmp) / "7.8.1.basebinary"
            template_path.write_bytes(self.make_firmware_template(primary, product_id=113))
            with self.flash_zopfli_available():
                with mock.patch("timecapsulesmb.cli.flash.load_env_config", return_value=self.make_app_config(self.make_valid_env())):
                    with mock.patch("timecapsulesmb.cli.flash.CommandContext", return_value=command_context):
                        with mock.patch(
                            "timecapsulesmb.cli.flash.read_flash_inputs",
                            return_value=(
                                primary,
                                secondary,
                                self.flash_bank_checksum(primary),
                                self.flash_bank_checksum(secondary),
                                113,
                                STOCK_LOGIN_NETBSD4_DUMMY,
                            ),
                        ):
                            with mock.patch("timecapsulesmb.cli.flash.flash_firmware_bank", side_effect=fake_flash):
                                with mock.patch("timecapsulesmb.cli.flash.dump_remote_bank", side_effect=fake_readback):
                                    with mock.patch("timecapsulesmb.cli.flash.get_property_int", side_effect=fake_get_property):
                                        with redirect_stdout(output):
                                            rc = cli_flash.main([
                                                "--patch",
                                                "--yes",
                                                "--active-bank",
                                                "primary",
                                                "--firmware-template",
                                                str(template_path),
                                                "--backup-dir",
                                                str(backup_dir),
                                            ])
            manifest = json.loads((backup_dir / "manifest.json").read_text())

        self.assertEqual(rc, 0)
        self.assertEqual(written["bank_name"], b"primary")
        self.assertEqual(manifest["active_bank"], "primary")
        self.assertEqual(manifest["active_selection"]["status"], "selected")
        self.assertEqual(manifest["active_selection"]["selected_by"], "user_override")
        self.assertEqual(manifest["active_selection"]["requested_bank"], "primary")
        self.assertEqual(manifest["active_selection"]["candidates"], ["primary", "secondary"])
        self.assertEqual(manifest["flash_plan"]["target_bank"], "primary")
        self.assertEqual(
            manifest["flash_plan"]["warnings"],
            ["active bank selected by --active-bank primary; automatic candidates were: primary, secondary"],
        )

    def test_flash_patch_noops_when_active_bank_is_already_patched(self) -> None:
        output = io.StringIO()
        stock_primary = self.make_flash_bank(release=b"NetBSD 4.0_STABLE #0: current")
        secondary = self.make_flash_bank(release=b"NetBSD 4.0_BETA2 #0: old")
        command_context = FakeCommandContext(compatibility=self.make_supported_netbsd4_stable_compatibility())

        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp) / "backup"
            with self.flash_zopfli_available():
                patched_primary = self.make_patched_flash_bank(stock_primary, secondary)
                with mock.patch("timecapsulesmb.cli.flash.load_env_config", return_value=self.make_app_config(self.make_valid_env())):
                    with mock.patch("timecapsulesmb.cli.flash.CommandContext", return_value=command_context):
                        with mock.patch(
                            "timecapsulesmb.cli.flash.read_flash_inputs",
                            return_value=(
                                patched_primary,
                                secondary,
                                self.flash_bank_checksum(patched_primary),
                                self.flash_bank_checksum(secondary),
                                113,
                                PATCHED_LOGIN_SCRIPT,
                            ),
                        ):
                            with mock.patch("timecapsulesmb.flash_payloads.resolve_firmware_template_candidates", side_effect=AssertionError("no template needed")):
                                with mock.patch("timecapsulesmb.cli.flash.flash_firmware_bank") as flash_mock:
                                    with redirect_stdout(output):
                                        rc = cli_flash.main([
                                            "--patch",
                                            "--yes",
                                            "--backup-dir",
                                            str(backup_dir),
                                        ])
            manifest = json.loads((backup_dir / "manifest.json").read_text())

        self.assertEqual(rc, 0)
        flash_mock.assert_not_called()
        self.assertTrue(manifest["flash_plan"]["already_satisfied"])
        self.assertFalse(manifest["flash_plan"]["write_requested"])
        self.assertIsNone(manifest["flash_plan"]["payload"])
        self.assertEqual(manifest["flash_plan"]["target_bank"], "primary")
        self.assertEqual(manifest["write_outcome"]["status"], "not_needed")
        self.assertFalse(manifest["write_outcome"]["write_may_have_modified_device"])
        self.assertIn("Active firmware bank is already patched; no write needed.", output.getvalue())
        self.assertEqual(command_context.finish.call_args.kwargs["result"], "success")

    def test_flash_patch_rejects_poweroff(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as raised:
                cli_flash.main(["--patch", "--poweroff"])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("--poweroff is not supported", stderr.getvalue())

    def test_flash_patch_rejects_reboot(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as raised:
                cli_flash.main(["--patch", "--reboot"])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("flash --patch cannot use --reboot", stderr.getvalue())

    def test_flash_restore_writes_apple_basebinary_and_validates_readback(self) -> None:
        output = io.StringIO()
        stock_primary = self.make_flash_bank(release=b"NetBSD 4.0_STABLE #0: current")
        secondary = self.make_flash_bank(release=b"NetBSD 4.0_BETA2 #0: old")
        patched_primary = self.make_patched_flash_bank(stock_primary, secondary)
        template = self.make_firmware_template(stock_primary, product_id=113)
        command_context = FakeCommandContext(compatibility=self.make_supported_netbsd4_stable_compatibility())
        written: dict[str, bytes] = {}

        def fake_flash(_host: str, _password: str, bank_name: str, payload: bytes, **_kwargs: object) -> SimpleNamespace:
            written["bank_name"] = bank_name.encode()
            written["payload"] = payload
            return SimpleNamespace(command=0x03, reply_body=b"")

        def fake_readback(_conn: object, _dev: str) -> bytes:
            reparsed = parse_nested_basebinary(written["payload"])
            end_offset = self.flash_bank_end_offset(patched_primary)
            rebuilt = bytearray(patched_primary)
            rebuilt[:end_offset] = reparsed.inner.payload
            checksum = zlib.adler32(bytes(rebuilt[:end_offset])) & 0xFFFFFFFF
            for offset in range(max(0, len(patched_primary) - 4096), len(patched_primary) - 7):
                _old_checksum, candidate_end = struct.unpack(">II", patched_primary[offset : offset + 8])
                if candidate_end == end_offset:
                    rebuilt[offset : offset + 4] = struct.pack(">I", checksum)
                    return bytes(rebuilt)
            self.fail("synthetic flash bank footer not found")

        def fake_get_property(_host: str, _password: str, name: str, **_kwargs: object) -> int:
            self.assertEqual(name, "cks1")
            return self.flash_bank_checksum(fake_readback(None, ""))

        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp) / "backup"
            template_path = Path(tmp) / "7.8.1.basebinary"
            template_path.write_bytes(template)
            with mock.patch("timecapsulesmb.cli.flash.load_env_config", return_value=self.make_app_config(self.make_valid_env())):
                with mock.patch("timecapsulesmb.cli.flash.CommandContext", return_value=command_context):
                    with mock.patch(
                        "timecapsulesmb.cli.flash.read_flash_inputs",
                        return_value=(
                            patched_primary,
                            secondary,
                            self.flash_bank_checksum(patched_primary),
                            self.flash_bank_checksum(secondary),
                            113,
                            PATCHED_LOGIN_SCRIPT,
                        ),
                    ):
                        with mock.patch("timecapsulesmb.cli.flash.flash_firmware_bank", side_effect=fake_flash) as flash_mock:
                            with mock.patch("timecapsulesmb.cli.flash.dump_remote_bank", side_effect=fake_readback):
                                with mock.patch("timecapsulesmb.cli.flash.get_property_int", side_effect=fake_get_property):
                                    with mock.patch("timecapsulesmb.cli.flows.remote_request_reboot") as reboot_mock:
                                        with redirect_stdout(output):
                                            rc = cli_flash.main([
                                                "--restore",
                                                "--yes",
                                                "--firmware-template",
                                                str(template_path),
                                                "--backup-dir",
                                                str(backup_dir),
                                            ])
            manifest = json.loads((backup_dir / "manifest.json").read_text())
            payload_file_exists = (backup_dir / "primary.restore.basebinary").is_file()

        self.assertEqual(rc, 0)
        flash_mock.assert_called_once()
        self.assertEqual(written["bank_name"], b"primary")
        self.assertEqual(written["payload"], template)
        self.assertTrue(payload_file_exists)
        self.assertFalse(reboot_mock.called)
        self.assertEqual(manifest["operation"], "restore")
        self.assertEqual(manifest["flash_plan"]["mode"], "restore")
        self.assertFalse(manifest["flash_plan"]["already_satisfied"])
        self.assertTrue(manifest["banks"][0]["would_write"])
        self.assertEqual(manifest["banks"][0]["write_decision"], "active bank restore from Apple firmware planned")
        self.assertFalse(manifest["banks"][1]["would_write"])
        self.assertEqual(manifest["banks"][1]["write_decision"], "inactive bank left unmodified")
        self.assertEqual(manifest["write_outcome"]["status"], "validated")
        self.assertTrue(manifest["write_outcome"]["write_validated"])
        self.assertEqual(manifest["write_result"]["login_classification"], "stock")
        self.assertEqual(manifest["write_result"]["expected_bank_sha256"], manifest["write_result"]["readback_sha256"])
        self.assertIn("Restore write successful.\x1b[0m The device needs to be manually rebooted.", output.getvalue())
        self.assertNotIn("Reboot not requested", output.getvalue())

    def test_flash_restore_reboot_uses_ssh_reboot_not_acp(self) -> None:
        output = io.StringIO()
        stock_primary = self.make_flash_bank(release=b"NetBSD 4.0_STABLE #0: current")
        secondary = self.make_flash_bank(release=b"NetBSD 4.0_BETA2 #0: old")
        patched_primary = self.make_patched_flash_bank(stock_primary, secondary)
        template = self.make_firmware_template(stock_primary, product_id=113)
        command_context = FakeCommandContext(compatibility=self.make_supported_netbsd4_stable_compatibility())
        written: dict[str, bytes] = {}

        def fake_flash(_host: str, _password: str, bank_name: str, payload: bytes, **_kwargs: object) -> SimpleNamespace:
            written["bank_name"] = bank_name.encode()
            written["payload"] = payload
            return SimpleNamespace(command=0x03, reply_body=b"")

        def fake_readback(_conn: object, _dev: str) -> bytes:
            reparsed = parse_nested_basebinary(written["payload"])
            end_offset = self.flash_bank_end_offset(patched_primary)
            rebuilt = bytearray(patched_primary)
            rebuilt[:end_offset] = reparsed.inner.payload
            checksum = zlib.adler32(bytes(rebuilt[:end_offset])) & 0xFFFFFFFF
            for offset in range(max(0, len(patched_primary) - 4096), len(patched_primary) - 7):
                _old_checksum, candidate_end = struct.unpack(">II", patched_primary[offset : offset + 8])
                if candidate_end == end_offset:
                    rebuilt[offset : offset + 4] = struct.pack(">I", checksum)
                    return bytes(rebuilt)
            self.fail("synthetic flash bank footer not found")

        def fake_get_property(_host: str, _password: str, name: str, **_kwargs: object) -> int:
            self.assertEqual(name, "cks1")
            return self.flash_bank_checksum(fake_readback(None, ""))

        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp) / "backup"
            template_path = Path(tmp) / "7.8.1.basebinary"
            template_path.write_bytes(template)
            with mock.patch("timecapsulesmb.cli.flash.load_env_config", return_value=self.make_app_config(self.make_valid_env())):
                with mock.patch("timecapsulesmb.cli.flash.CommandContext", return_value=command_context):
                    with mock.patch(
                        "timecapsulesmb.cli.flash.read_flash_inputs",
                        return_value=(
                            patched_primary,
                            secondary,
                            self.flash_bank_checksum(patched_primary),
                            self.flash_bank_checksum(secondary),
                            113,
                            PATCHED_LOGIN_SCRIPT,
                        ),
                    ):
                        with mock.patch("timecapsulesmb.cli.flash.flash_firmware_bank", side_effect=fake_flash):
                            with mock.patch("timecapsulesmb.cli.flash.dump_remote_bank", side_effect=fake_readback):
                                with mock.patch("timecapsulesmb.cli.flash.get_property_int", side_effect=fake_get_property):
                                    with mock.patch("timecapsulesmb.cli.flows.remote_request_reboot") as ssh_reboot_mock:
                                        with mock.patch("timecapsulesmb.cli.flows.acp_reboot", side_effect=AssertionError("flash should not request ACP reboot")) as acp_reboot_mock:
                                            with mock.patch("timecapsulesmb.cli.flows.wait_for_ssh_state_conn", side_effect=[True, True]) as wait_mock:
                                                with redirect_stdout(output):
                                                    rc = cli_flash.main([
                                                        "--restore",
                                                        "--yes",
                                                        "--reboot",
                                                        "--firmware-template",
                                                        str(template_path),
                                                        "--backup-dir",
                                                        str(backup_dir),
                                                    ])

        self.assertEqual(rc, 0)
        self.assertEqual(written["bank_name"], b"primary")
        ssh_reboot_mock.assert_called_once()
        acp_reboot_mock.assert_not_called()
        self.assertEqual(wait_mock.call_args_list[0].kwargs, {"expected_up": False, "timeout_seconds": 60})
        self.assertEqual(wait_mock.call_args_list[1].kwargs, {"expected_up": True, "timeout_seconds": 240})
        text = output.getvalue()
        self.assertIn("SSH reboot requested.", text)
        self.assertIn("Device is back online.", text)
        self.assertIn("Run `tcapsule flash --check-apple` to verify Apple stock firmware.", text)
        self.assertNotIn("verify Samba startup", text)
        finished = command_context.finish.call_args.kwargs
        self.assertEqual(finished["result"], "success")
        self.assertEqual(finished["reboot_was_attempted"], True)
        self.assertEqual(finished["device_came_back_after_reboot"], True)

    def test_flash_restore_noops_when_active_bank_already_matches_apple(self) -> None:
        output = io.StringIO()
        primary = self.make_flash_bank(release=b"NetBSD 4.0_STABLE #0: current")
        secondary = self.make_flash_bank(release=b"NetBSD 4.0_BETA2 #0: old")
        command_context = FakeCommandContext(compatibility=self.make_supported_netbsd4_stable_compatibility())

        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp) / "backup"
            template_path = Path(tmp) / "7.8.1.basebinary"
            template_path.write_bytes(self.make_firmware_template(primary, product_id=113))
            with mock.patch("timecapsulesmb.cli.flash.load_env_config", return_value=self.make_app_config(self.make_valid_env())):
                with mock.patch("timecapsulesmb.cli.flash.CommandContext", return_value=command_context):
                    with mock.patch(
                        "timecapsulesmb.cli.flash.read_flash_inputs",
                        return_value=(
                            primary,
                            secondary,
                            self.flash_bank_checksum(primary),
                            self.flash_bank_checksum(secondary),
                            113,
                            STOCK_LOGIN_NETBSD4_DUMMY,
                        ),
                    ):
                        with mock.patch("timecapsulesmb.cli.flash.flash_firmware_bank") as flash_mock:
                            with redirect_stdout(output):
                                rc = cli_flash.main([
                                    "--restore",
                                    "--yes",
                                    "--firmware-template",
                                    str(template_path),
                                    "--backup-dir",
                                    str(backup_dir),
                                ])
            manifest = json.loads((backup_dir / "manifest.json").read_text())

        self.assertEqual(rc, 0)
        flash_mock.assert_not_called()
        self.assertTrue(manifest["flash_plan"]["already_satisfied"])
        self.assertTrue(manifest["flash_plan"]["apple_match"]["matched"])
        self.assertFalse(manifest["banks"][0]["would_write"])
        self.assertEqual(manifest["banks"][0]["write_decision"], "active bank already matches requested Apple stock firmware; no write needed")
        self.assertEqual(manifest["write_outcome"]["status"], "not_needed")
        self.assertFalse(manifest["write_outcome"]["write_may_have_modified_device"])
        self.assertIn("already matches the requested Apple stock firmware", output.getvalue())

    def test_flash_check_apple_reports_match_without_zopfli_or_write(self) -> None:
        output = io.StringIO()
        primary = self.make_flash_bank(release=b"NetBSD 4.0_STABLE #0: current")
        secondary = self.make_flash_bank(release=b"NetBSD 4.0_BETA2 #0: old")
        command_context = FakeCommandContext(compatibility=self.make_supported_netbsd4_stable_compatibility())

        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp) / "backup"
            template_path = Path(tmp) / "7.8.1.basebinary"
            template_path.write_bytes(self.make_firmware_template(primary, product_id=113))
            with mock.patch("timecapsulesmb.flash.require_python_module", side_effect=AssertionError("zopfli not needed")):
                with mock.patch("timecapsulesmb.cli.flash.load_env_config", return_value=self.make_app_config(self.make_valid_env())):
                    with mock.patch("timecapsulesmb.cli.flash.CommandContext", return_value=command_context):
                        with mock.patch(
                            "timecapsulesmb.cli.flash.read_flash_inputs",
                            return_value=(
                                primary,
                                secondary,
                                self.flash_bank_checksum(primary),
                                self.flash_bank_checksum(secondary),
                                113,
                                STOCK_LOGIN_NETBSD4_DUMMY,
                            ),
                        ):
                            with mock.patch("timecapsulesmb.cli.flash.flash_firmware_bank") as flash_mock:
                                with redirect_stdout(output):
                                    rc = cli_flash.main([
                                        "--check-apple",
                                        "--firmware-template",
                                        str(template_path),
                                        "--backup-dir",
                                        str(backup_dir),
                                    ])
            manifest = json.loads((backup_dir / "manifest.json").read_text())

        self.assertEqual(rc, 0)
        flash_mock.assert_not_called()
        self.assertEqual(manifest["operation"], "check_apple")
        self.assertTrue(manifest["flash_plan"]["apple_match"]["matched"])
        self.assertIn("Apple firmware match: matched=True", output.getvalue())

    def test_flash_download_only_validates_firmware_without_write(self) -> None:
        output = io.StringIO()
        primary = self.make_flash_bank(release=b"NetBSD 4.0_STABLE #0: current")
        secondary = self.make_flash_bank(release=b"NetBSD 4.0_BETA2 #0: old")
        command_context = FakeCommandContext(compatibility=self.make_supported_netbsd4_stable_compatibility())

        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp) / "backup"
            template_path = Path(tmp) / "7.8.1.basebinary"
            template_path.write_bytes(self.make_firmware_template(primary, product_id=113))
            with mock.patch("timecapsulesmb.cli.flash.load_env_config", return_value=self.make_app_config(self.make_valid_env())):
                with mock.patch("timecapsulesmb.cli.flash.CommandContext", return_value=command_context):
                    with mock.patch(
                        "timecapsulesmb.cli.flash.read_flash_inputs",
                        return_value=(
                            primary,
                            secondary,
                            self.flash_bank_checksum(primary),
                            self.flash_bank_checksum(secondary),
                            113,
                            STOCK_LOGIN_NETBSD4_DUMMY,
                        ),
                    ):
                        with mock.patch("timecapsulesmb.cli.flash.flash_firmware_bank") as flash_mock:
                            with redirect_stdout(output):
                                rc = cli_flash.main([
                                    "--download-only",
                                    "--firmware-template",
                                    str(template_path),
                                    "--backup-dir",
                                    str(backup_dir),
                                ])
            manifest = json.loads((backup_dir / "manifest.json").read_text())

        self.assertEqual(rc, 0)
        flash_mock.assert_not_called()
        self.assertEqual(manifest["operation"], "download_only")
        self.assertEqual(manifest["flash_plan"]["payload"]["key_id"], "observed-k30a-78100")
        self.assertTrue(manifest["flash_plan"]["already_satisfied"])
        self.assertTrue(manifest["flash_plan"]["apple_match"]["matched"])

    def test_flash_download_only_reports_mismatch_without_write(self) -> None:
        output = io.StringIO()
        stock_primary = self.make_flash_bank(release=b"NetBSD 4.0_STABLE #0: current")
        secondary = self.make_flash_bank(release=b"NetBSD 4.0_BETA2 #0: old")
        patched_primary = self.make_patched_flash_bank(stock_primary, secondary)
        command_context = FakeCommandContext(compatibility=self.make_supported_netbsd4_stable_compatibility())

        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp) / "backup"
            template_path = Path(tmp) / "7.8.1.basebinary"
            template_path.write_bytes(self.make_firmware_template(stock_primary, product_id=113))
            with mock.patch("timecapsulesmb.cli.flash.load_env_config", return_value=self.make_app_config(self.make_valid_env())):
                with mock.patch("timecapsulesmb.cli.flash.CommandContext", return_value=command_context):
                    with mock.patch(
                        "timecapsulesmb.cli.flash.read_flash_inputs",
                        return_value=(
                            patched_primary,
                            secondary,
                            self.flash_bank_checksum(patched_primary),
                            self.flash_bank_checksum(secondary),
                            113,
                            PATCHED_LOGIN_SCRIPT,
                        ),
                    ):
                        with mock.patch("timecapsulesmb.cli.flash.flash_firmware_bank") as flash_mock:
                            with redirect_stdout(output):
                                rc = cli_flash.main([
                                    "--download-only",
                                    "--firmware-template",
                                    str(template_path),
                                    "--backup-dir",
                                    str(backup_dir),
                                ])
            manifest = json.loads((backup_dir / "manifest.json").read_text())

        self.assertEqual(rc, 0)
        flash_mock.assert_not_called()
        self.assertFalse(manifest["flash_plan"]["already_satisfied"])
        self.assertFalse(manifest["flash_plan"]["apple_match"]["matched"])
        self.assertFalse(manifest["flash_plan"]["write_requested"])
        self.assertFalse(manifest["banks"][0]["would_write"])
        self.assertEqual(manifest["banks"][0]["write_decision"], "download only; no firmware write planned")

    def test_flash_restore_refuses_wrong_product_template_before_acp(self) -> None:
        output = io.StringIO()
        primary = self.make_flash_bank(release=b"NetBSD 4.0_STABLE #0: current")
        secondary = self.make_flash_bank(release=b"NetBSD 4.0_BETA2 #0: old")
        command_context = FakeCommandContext(compatibility=self.make_supported_netbsd4_stable_compatibility())

        with tempfile.TemporaryDirectory() as tmp:
            template_path = Path(tmp) / "wrong-product.basebinary"
            template_path.write_bytes(self.make_firmware_template(primary, product_id=106))
            with mock.patch("timecapsulesmb.cli.flash.load_env_config", return_value=self.make_app_config(self.make_valid_env())):
                with mock.patch("timecapsulesmb.cli.flash.CommandContext", return_value=command_context):
                    with mock.patch(
                        "timecapsulesmb.cli.flash.read_flash_inputs",
                        return_value=(
                            primary,
                            secondary,
                            self.flash_bank_checksum(primary),
                            self.flash_bank_checksum(secondary),
                            113,
                            STOCK_LOGIN_NETBSD4_DUMMY,
                        ),
                    ):
                        with mock.patch("timecapsulesmb.cli.flash.flash_firmware_bank") as flash_mock:
                            with redirect_stdout(output):
                                rc = cli_flash.main([
                                    "--restore",
                                    "--yes",
                                    "--firmware-template",
                                    str(template_path),
                                    "--backup-dir",
                                    str(Path(tmp) / "backup"),
                                ])

        self.assertEqual(rc, 1)
        flash_mock.assert_not_called()
        self.assertIn("does not match device syAP", output.getvalue())
        self.assertIn("flash_error_stage=plan_flash", command_context.finish.call_args.kwargs["error"])
        self.assertNotIn("flash_error_stage", command_context.finish.call_args.kwargs)

    def test_flash_restore_refuses_non_stock_template_before_acp(self) -> None:
        output = io.StringIO()
        stock_primary = self.make_flash_bank(release=b"NetBSD 4.0_STABLE #0: current")
        secondary = self.make_flash_bank(release=b"NetBSD 4.0_BETA2 #0: old")
        patched_primary = self.make_patched_flash_bank(stock_primary, secondary)
        command_context = FakeCommandContext(compatibility=self.make_supported_netbsd4_stable_compatibility())

        with tempfile.TemporaryDirectory() as tmp:
            template_path = Path(tmp) / "patched-template.basebinary"
            template_path.write_bytes(self.make_firmware_template(patched_primary, product_id=113))
            with mock.patch("timecapsulesmb.cli.flash.load_env_config", return_value=self.make_app_config(self.make_valid_env())):
                with mock.patch("timecapsulesmb.cli.flash.CommandContext", return_value=command_context):
                    with mock.patch(
                        "timecapsulesmb.cli.flash.read_flash_inputs",
                        return_value=(
                            stock_primary,
                            secondary,
                            self.flash_bank_checksum(stock_primary),
                            self.flash_bank_checksum(secondary),
                            113,
                            STOCK_LOGIN_NETBSD4_DUMMY,
                        ),
                    ):
                        with mock.patch("timecapsulesmb.cli.flash.flash_firmware_bank") as flash_mock:
                            with redirect_stdout(output):
                                rc = cli_flash.main([
                                    "--restore",
                                    "--yes",
                                    "--firmware-template",
                                    str(template_path),
                                    "--backup-dir",
                                    str(Path(tmp) / "backup"),
                                ])

        self.assertEqual(rc, 1)
        flash_mock.assert_not_called()
        self.assertIn("Apple firmware template LOGIN classification is already_patched", output.getvalue())
        self.assertIn("flash_error_stage=plan_flash", command_context.finish.call_args.kwargs["error"])

    def test_flash_patch_and_restore_are_mutually_exclusive(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as raised:
                cli_flash.main(["--patch", "--restore"])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("not allowed with argument", stderr.getvalue())

    def test_flash_write_readback_sha_mismatch_fails_before_reboot(self) -> None:
        output = io.StringIO()
        primary = self.make_flash_bank(release=b"NetBSD 4.0_STABLE #0: current")
        secondary = self.make_flash_bank(release=b"NetBSD 4.0_BETA2 #0: old")
        command_context = FakeCommandContext(compatibility=self.make_supported_netbsd4_stable_compatibility())

        with tempfile.TemporaryDirectory() as tmp:
            template_path = Path(tmp) / "7.8.1.basebinary"
            template_path.write_bytes(self.make_firmware_template(primary, product_id=113))
            with self.flash_zopfli_available():
                with mock.patch("timecapsulesmb.cli.flash.load_env_config", return_value=self.make_app_config(self.make_valid_env())):
                    with mock.patch("timecapsulesmb.cli.flash.CommandContext", return_value=command_context):
                        with mock.patch(
                            "timecapsulesmb.cli.flash.read_flash_inputs",
                            return_value=(
                                primary,
                                secondary,
                                self.flash_bank_checksum(primary),
                                self.flash_bank_checksum(secondary),
                                113,
                                STOCK_LOGIN_NETBSD4_DUMMY,
                            ),
                        ):
                            with mock.patch("timecapsulesmb.cli.flash.flash_firmware_bank", return_value=SimpleNamespace(command=0x03, reply_body=b"")):
                                with mock.patch("timecapsulesmb.cli.flash.dump_remote_bank", return_value=primary):
                                    with mock.patch("timecapsulesmb.cli.flows.remote_request_reboot") as reboot_mock:
                                        with redirect_stdout(output):
                                            rc = cli_flash.main([
                                                "--patch",
                                                "--yes",
                                                "--firmware-template",
                                                str(template_path),
                                                "--backup-dir",
                                                str(Path(tmp) / "backup"),
                                            ])

        self.assertEqual(rc, 1)
        reboot_mock.assert_not_called()
        self.assertIn("read-back firmware bank prefix SHA-256 mismatch", output.getvalue())
        self.assertEqual(command_context.finish.call_args.kwargs["result"], "failure")
        self.assertIn("flash_error_stage=post_write_validation", command_context.finish.call_args.kwargs["error"])
        self.assertIn("read-back firmware bank prefix SHA-256 mismatch", command_context.finish.call_args.kwargs["error"])
        self.assertNotIn("flash_error_stage", command_context.finish.call_args.kwargs)
        self.assertNotIn("flash_error", command_context.finish.call_args.kwargs)

    def test_flash_write_full_bank_mismatch_fails_even_when_prefix_matches(self) -> None:
        output = io.StringIO()
        primary = self.make_flash_bank(release=b"NetBSD 4.0_STABLE #0: current")
        secondary = self.make_flash_bank(release=b"NetBSD 4.0_BETA2 #0: old")
        command_context = FakeCommandContext(compatibility=self.make_supported_netbsd4_stable_compatibility())
        written: dict[str, bytes] = {}

        def fake_flash(_host: str, _password: str, bank_name: str, payload: bytes, **_kwargs: object) -> SimpleNamespace:
            written["bank_name"] = bank_name.encode()
            written["payload"] = payload
            return SimpleNamespace(command=0x03, reply_body=b"")

        def fake_readback(_conn: object, _dev: str) -> bytes:
            reparsed = parse_nested_basebinary(written["payload"])
            end_offset = self.flash_bank_end_offset(primary)
            rebuilt = bytearray(primary)
            rebuilt[:end_offset] = reparsed.inner.payload
            checksum = zlib.adler32(bytes(rebuilt[:end_offset])) & 0xFFFFFFFF
            for offset in range(max(0, len(primary) - 4096), len(primary) - 7):
                _old_checksum, candidate_end = struct.unpack(">II", primary[offset : offset + 8])
                if candidate_end == end_offset:
                    rebuilt[offset : offset + 4] = struct.pack(">I", checksum)
                    rebuilt[-1] ^= 0x01
                    return bytes(rebuilt)
            self.fail("synthetic flash bank footer not found")

        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp) / "backup"
            template_path = Path(tmp) / "7.8.1.basebinary"
            template_path.write_bytes(self.make_firmware_template(primary, product_id=113))
            with self.flash_zopfli_available():
                with mock.patch("timecapsulesmb.cli.flash.load_env_config", return_value=self.make_app_config(self.make_valid_env())):
                    with mock.patch("timecapsulesmb.cli.flash.CommandContext", return_value=command_context):
                        with mock.patch(
                            "timecapsulesmb.cli.flash.read_flash_inputs",
                            return_value=(
                                primary,
                                secondary,
                                self.flash_bank_checksum(primary),
                                self.flash_bank_checksum(secondary),
                                113,
                                STOCK_LOGIN_NETBSD4_DUMMY,
                            ),
                        ):
                            with mock.patch("timecapsulesmb.cli.flash.flash_firmware_bank", side_effect=fake_flash) as flash_mock:
                                with mock.patch("timecapsulesmb.cli.flash.dump_remote_bank", side_effect=fake_readback):
                                    with mock.patch("timecapsulesmb.cli.flash.get_property_int", side_effect=AssertionError("ACP checksum should not be read after full-bank mismatch")) as acp_mock:
                                        with mock.patch("timecapsulesmb.cli.flows.remote_request_reboot") as reboot_mock:
                                            with redirect_stdout(output):
                                                rc = cli_flash.main([
                                                    "--patch",
                                                    "--yes",
                                                    "--firmware-template",
                                                    str(template_path),
                                                    "--backup-dir",
                                                    str(backup_dir),
                                                ])
            manifest = json.loads((backup_dir / "manifest.json").read_text())

        self.assertEqual(rc, 1)
        flash_mock.assert_called_once()
        acp_mock.assert_not_called()
        reboot_mock.assert_not_called()
        self.assertIn("read-back firmware bank SHA-256 mismatch", output.getvalue())
        self.assertEqual(manifest["write_outcome"]["status"], "failed")
        self.assertTrue(manifest["write_outcome"]["write_may_have_modified_device"])
        self.assertIn("read-back firmware bank SHA-256 mismatch", manifest["write_outcome"]["message"])

    def test_flash_write_readback_ssh_error_is_reported_without_traceback(self) -> None:
        output = io.StringIO()
        primary = self.make_flash_bank(release=b"NetBSD 4.0_STABLE #0: current")
        secondary = self.make_flash_bank(release=b"NetBSD 4.0_BETA2 #0: old")
        command_context = FakeCommandContext(compatibility=self.make_supported_netbsd4_stable_compatibility())

        with tempfile.TemporaryDirectory() as tmp:
            template_path = Path(tmp) / "7.8.1.basebinary"
            template_path.write_bytes(self.make_firmware_template(primary, product_id=113))
            with self.flash_zopfli_available():
                with mock.patch("timecapsulesmb.cli.flash.load_env_config", return_value=self.make_app_config(self.make_valid_env())):
                    with mock.patch("timecapsulesmb.cli.flash.CommandContext", return_value=command_context):
                        with mock.patch(
                            "timecapsulesmb.cli.flash.read_flash_inputs",
                            return_value=(
                                primary,
                                secondary,
                                self.flash_bank_checksum(primary),
                                self.flash_bank_checksum(secondary),
                                113,
                                STOCK_LOGIN_NETBSD4_DUMMY,
                            ),
                        ):
                            with mock.patch("timecapsulesmb.cli.flash.flash_firmware_bank", return_value=SimpleNamespace(command=0x03, reply_body=b"")) as flash_mock:
                                with mock.patch("timecapsulesmb.cli.flash.dump_remote_bank", side_effect=SshError("ssh command failed with rc=255")):
                                    with mock.patch("timecapsulesmb.cli.flash.get_property_int", side_effect=AssertionError("ACP checksum should not be read after read-back failure")) as acp_mock:
                                        with mock.patch("timecapsulesmb.cli.flows.remote_request_reboot") as reboot_mock:
                                            with redirect_stdout(output):
                                                rc = cli_flash.main([
                                                    "--patch",
                                                    "--yes",
                                                    "--firmware-template",
                                                    str(template_path),
                                                    "--backup-dir",
                                                    str(Path(tmp) / "backup"),
                                                ])

        self.assertEqual(rc, 1)
        flash_mock.assert_called_once()
        acp_mock.assert_not_called()
        reboot_mock.assert_not_called()
        self.assertIn("SSH post-write validation failed", output.getvalue())
        self.assertIn("ssh command failed with rc=255", output.getvalue())
        finished = command_context.finish.call_args.kwargs
        self.assertEqual(finished["result"], "failure")
        self.assertIn("flash_error_stage=post_write_validation", finished["error"])
        self.assertIn("SSH post-write validation failed", finished["error"])
        self.assertNotIn("flash_error_stage", finished)
        self.assertNotIn("flash_error", finished)

    def test_flash_write_acp_error_is_reported_to_telemetry_without_traceback(self) -> None:
        output = io.StringIO()
        primary = self.make_flash_bank(release=b"NetBSD 4.0_STABLE #0: current")
        secondary = self.make_flash_bank(release=b"NetBSD 4.0_BETA2 #0: old")
        command_context = FakeCommandContext(compatibility=self.make_supported_netbsd4_stable_compatibility())

        with tempfile.TemporaryDirectory() as tmp:
            template_path = Path(tmp) / "7.8.1.basebinary"
            template_path.write_bytes(self.make_firmware_template(primary, product_id=113))
            with self.flash_zopfli_available():
                with mock.patch("timecapsulesmb.cli.flash.load_env_config", return_value=self.make_app_config(self.make_valid_env())):
                    with mock.patch("timecapsulesmb.cli.flash.CommandContext", return_value=command_context):
                        with mock.patch(
                            "timecapsulesmb.cli.flash.read_flash_inputs",
                            return_value=(
                                primary,
                                secondary,
                                self.flash_bank_checksum(primary),
                                self.flash_bank_checksum(secondary),
                                113,
                                STOCK_LOGIN_NETBSD4_DUMMY,
                            ),
                        ):
                            with mock.patch("timecapsulesmb.cli.flash.flash_firmware_bank", side_effect=ACPAuthError("ACP command failed with error_code -0x14")):
                                with redirect_stdout(output):
                                    rc = cli_flash.main([
                                        "--patch",
                                        "--yes",
                                        "--firmware-template",
                                        str(template_path),
                                        "--backup-dir",
                                        str(Path(tmp) / "backup"),
                                    ])

        self.assertEqual(rc, 1)
        self.assertIn("ACP flash command failed", output.getvalue())
        finished = command_context.finish.call_args.kwargs
        self.assertEqual(finished["result"], "failure")
        self.assertIn("flash_error_stage=post_write_validation", finished["error"])
        self.assertIn("ACP flash command failed", finished["error"])
        self.assertNotIn("flash_error_stage", finished)
        self.assertNotIn("flash_error", finished)

    def test_flash_write_postwrite_acp_checksum_error_is_reported_without_traceback(self) -> None:
        output = io.StringIO()
        primary = self.make_flash_bank(release=b"NetBSD 4.0_STABLE #0: current")
        secondary = self.make_flash_bank(release=b"NetBSD 4.0_BETA2 #0: old")
        command_context = FakeCommandContext(compatibility=self.make_supported_netbsd4_stable_compatibility())
        written: dict[str, bytes] = {}

        def fake_flash(_host: str, _password: str, bank_name: str, payload: bytes, **_kwargs: object) -> SimpleNamespace:
            written["bank_name"] = bank_name.encode()
            written["payload"] = payload
            return SimpleNamespace(command=0x03, reply_body=b"")

        def fake_readback(_conn: object, _dev: str) -> bytes:
            reparsed = parse_nested_basebinary(written["payload"])
            end_offset = self.flash_bank_end_offset(primary)
            rebuilt = bytearray(primary)
            rebuilt[:end_offset] = reparsed.inner.payload
            checksum = zlib.adler32(bytes(rebuilt[:end_offset])) & 0xFFFFFFFF
            for offset in range(max(0, len(primary) - 4096), len(primary) - 7):
                _old_checksum, candidate_end = struct.unpack(">II", primary[offset : offset + 8])
                if candidate_end == end_offset:
                    rebuilt[offset : offset + 4] = struct.pack(">I", checksum)
                    return bytes(rebuilt)
            self.fail("synthetic flash bank footer not found")

        def fake_get_property(_host: str, _password: str, name: str, **_kwargs: object) -> int:
            self.assertEqual(name, "cks1")
            raise ACPConnectionError("ACP service unavailable")

        with tempfile.TemporaryDirectory() as tmp:
            template_path = Path(tmp) / "7.8.1.basebinary"
            template_path.write_bytes(self.make_firmware_template(primary, product_id=113))
            with self.flash_zopfli_available():
                with mock.patch("timecapsulesmb.cli.flash.load_env_config", return_value=self.make_app_config(self.make_valid_env())):
                    with mock.patch("timecapsulesmb.cli.flash.CommandContext", return_value=command_context):
                        with mock.patch(
                            "timecapsulesmb.cli.flash.read_flash_inputs",
                            return_value=(
                                primary,
                                secondary,
                                self.flash_bank_checksum(primary),
                                self.flash_bank_checksum(secondary),
                                113,
                                STOCK_LOGIN_NETBSD4_DUMMY,
                            ),
                        ):
                            with mock.patch("timecapsulesmb.cli.flash.flash_firmware_bank", side_effect=fake_flash) as flash_mock:
                                with mock.patch("timecapsulesmb.cli.flash.dump_remote_bank", side_effect=fake_readback):
                                    with mock.patch("timecapsulesmb.cli.flash.get_property_int", side_effect=fake_get_property):
                                        with mock.patch("timecapsulesmb.cli.flows.remote_request_reboot") as reboot_mock:
                                            with redirect_stdout(output):
                                                rc = cli_flash.main([
                                                    "--patch",
                                                    "--yes",
                                                    "--firmware-template",
                                                    str(template_path),
                                                    "--backup-dir",
                                                    str(Path(tmp) / "backup"),
                                                ])

        self.assertEqual(rc, 1)
        flash_mock.assert_called_once()
        reboot_mock.assert_not_called()
        self.assertIn("ACP checksum property cks1 read failed after write", output.getvalue())
        finished = command_context.finish.call_args.kwargs
        self.assertEqual(finished["result"], "failure")
        self.assertIn("flash_error_stage=post_write_validation", finished["error"])
        self.assertIn("ACP service unavailable", finished["error"])
        self.assertNotIn("flash_error_stage", finished)
        self.assertNotIn("flash_error", finished)

    def test_flash_write_unknown_login_includes_live_login_in_error(self) -> None:
        output = io.StringIO()
        unknown_login = b"#!/bin/sh\n# PROVIDE: LOGIN\nexit 0\n"
        primary = self.make_flash_bank(release=b"NetBSD 4.0_STABLE #0: current", login=unknown_login)
        secondary = self.make_flash_bank(release=b"NetBSD 4.0_BETA2 #0: old")
        command_context = FakeCommandContext(compatibility=self.make_supported_netbsd4_stable_compatibility())

        with tempfile.TemporaryDirectory() as tmp:
            with self.flash_zopfli_available():
                with mock.patch("timecapsulesmb.cli.flash.load_env_config", return_value=self.make_app_config(self.make_valid_env())):
                    with mock.patch("timecapsulesmb.cli.flash.CommandContext", return_value=command_context):
                        with mock.patch(
                            "timecapsulesmb.cli.flash.read_flash_inputs",
                            return_value=(
                                primary,
                                secondary,
                                self.flash_bank_checksum(primary),
                                self.flash_bank_checksum(secondary),
                                113,
                                unknown_login,
                            ),
                        ):
                            with mock.patch("timecapsulesmb.cli.flash.flash_firmware_bank") as flash_mock:
                                with redirect_stdout(output):
                                    rc = cli_flash.main([
                                        "--patch",
                                        "--yes",
                                        "--backup-dir",
                                        str(Path(tmp) / "backup"),
                                    ])

        self.assertEqual(rc, 1)
        flash_mock.assert_not_called()
        finished = command_context.finish.call_args.kwargs
        self.assertEqual(finished["result"], "failure")
        error = finished["error"]
        self.assertIn("flash_error_stage=plan_flash", error)
        self.assertIn("LOGIN classification unknown", error)
        self.assertIn("flash_login_mismatch_file=/etc/rc.d/LOGIN", error)
        self.assertIn(f"flash_login_mismatch_size={len(unknown_login)}", error)
        self.assertIn(f"flash_login_mismatch_sha256={sha256_hex(unknown_login)}", error)
        self.assertIn("flash_login_mismatch_truncated=False", error)
        self.assertIn("flash_login_mismatch_base64=IyEvYmluL3NoCiMgUFJPVklERTogTE9HSU4KZXhpdCAwCg==", error)
        self.assertNotIn("flash_error_stage", finished)
        self.assertNotIn("flash_error", finished)
        self.assertNotIn("flash_login_mismatch_file", finished)

    def test_flash_patch_write_readiness_fails_before_prompt(self) -> None:
        output = io.StringIO()
        unknown_login = b"#!/bin/sh\n# PROVIDE: LOGIN\nexit 0\n"
        primary = self.make_flash_bank(release=b"NetBSD 4.0_STABLE #0: current", login=unknown_login)
        secondary = self.make_flash_bank(release=b"NetBSD 4.0_BETA2 #0: old")
        command_context = FakeCommandContext(compatibility=self.make_supported_netbsd4_stable_compatibility())

        with tempfile.TemporaryDirectory() as tmp:
            with self.flash_zopfli_available():
                with mock.patch("timecapsulesmb.cli.flash.load_env_config", return_value=self.make_app_config(self.make_valid_env())):
                    with mock.patch("timecapsulesmb.cli.flash.CommandContext", return_value=command_context):
                        with mock.patch(
                            "timecapsulesmb.cli.flash.read_flash_inputs",
                            return_value=(
                                primary,
                                secondary,
                                self.flash_bank_checksum(primary),
                                self.flash_bank_checksum(secondary),
                                113,
                                unknown_login,
                            ),
                        ):
                            with mock.patch("timecapsulesmb.cli.context.runtime.confirm", side_effect=AssertionError("confirm should not be called")) as confirm_mock:
                                with mock.patch("timecapsulesmb.cli.flash.flash_firmware_bank") as flash_mock:
                                    with redirect_stdout(output):
                                        rc = cli_flash.main([
                                            "--patch",
                                            "--backup-dir",
                                            str(Path(tmp) / "backup"),
                                        ])

        self.assertEqual(rc, 1)
        confirm_mock.assert_not_called()
        flash_mock.assert_not_called()
        self.assertIn("plan_flash", command_context.stages)
        self.assertNotIn("confirm_write", command_context.stages)
        self.assertIn("LOGIN classification unknown", output.getvalue())
        finished = command_context.finish.call_args.kwargs
        self.assertEqual(finished["result"], "failure")
        self.assertIn("flash_error_stage=plan_flash", finished["error"])

    def test_flash_rejects_non_netbsd4_before_dumping_banks(self) -> None:
        command_context = FakeCommandContext(compatibility=self.make_supported_compatibility())
        with self.flash_zopfli_available():
            with mock.patch("timecapsulesmb.cli.flash.load_env_config", return_value=self.make_app_config(self.make_valid_env())):
                with mock.patch("timecapsulesmb.cli.flash.CommandContext", return_value=command_context):
                    with mock.patch("timecapsulesmb.cli.flash.read_flash_inputs") as read_mock:
                        with self.assertRaises(SystemExit) as raised:
                            cli_flash.main(["--read-only"])

        self.assertIn("flash is only supported for NetBSD4", str(raised.exception))
        read_mock.assert_not_called()

    def test_activate_skips_rc_local_when_payload_is_already_healthy(self) -> None:
        output = io.StringIO()
        values = self.make_valid_env()
        with mock.patch("timecapsulesmb.cli.activate.load_env_config", return_value=self.make_app_config(values)):
            with mock.patch("timecapsulesmb.cli.context.CommandContext.require_compatibility", return_value=self.make_supported_netbsd4_compatibility()):
                with mock.patch("timecapsulesmb.cli.activate.probe_managed_runtime_conn", return_value=mock.Mock(ready=True)):
                    with mock.patch("timecapsulesmb.cli.activate.run_remote_actions") as actions_mock:
                        with mock.patch("timecapsulesmb.cli.flows.verify_managed_runtime") as verify_mock:
                            with redirect_stdout(output):
                                rc = activate.main(["--yes"])
        self.assertEqual(rc, 0)
        actions_mock.assert_not_called()
        verify_mock.assert_not_called()
        self.assertIn("already active; skipping rc.local", output.getvalue())

    def test_activate_returns_nonzero_when_verification_fails(self) -> None:
        output = io.StringIO()
        values = self.make_valid_env()
        with mock.patch("timecapsulesmb.cli.activate.load_env_config", return_value=self.make_app_config(values)):
            with mock.patch("timecapsulesmb.cli.context.CommandContext.require_compatibility", return_value=self.make_supported_netbsd4_compatibility()):
                with mock.patch("timecapsulesmb.cli.activate.probe_managed_runtime_conn", return_value=mock.Mock(ready=False)):
                    with mock.patch("timecapsulesmb.cli.activate.run_remote_actions"):
                        with mock.patch("timecapsulesmb.cli.flows.verify_managed_runtime", return_value=self.managed_runtime_probe(False)):
                            with redirect_stdout(output):
                                rc = activate.main(["--yes"])
        self.assertEqual(rc, 1)
        self.assertIn("NetBSD4 activation failed.", output.getvalue())

    def test_uninstall_dry_run_prints_target_host(self) -> None:
        output = io.StringIO()
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
            "TC_PAYLOAD_DIR_NAME": "samba4",
        }
        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.uninstall.load_env_config", return_value=self.make_app_config(values)))
            mast_mocks = self._patch_mast_volume_flow(stack, "uninstall")
            with redirect_stdout(output):
                rc = uninstall.main(["--dry-run"])
        self.assertEqual(rc, 0)
        text = output.getvalue()
        self.assertIn("Dry run: uninstall plan", text)
        self.assertIn("host: root@10.0.0.2", text)
        self.assertIn("volume roots:\n    resolved from MaSt at uninstall time", text)
        self.assertIn(f"payload dirs:\n    resolved from MaSt at uninstall time/{values['TC_PAYLOAD_DIR_NAME']}", text)
        self.assertIn("request: attempt device reboot", text)
        self.assertIn("follow-up: wait for SSH down, then SSH up", text)
        started = self.telemetry_payload("uninstall_started")
        finished = self.telemetry_payload("uninstall_finished")
        self.assertEqual(started["command_id"], finished["command_id"])
        self.assertEqual(finished["result"], "success")
        self.assertEqual(finished["volume_roots"], ["resolved from MaSt at uninstall time"])
        self.assertEqual(finished["payload_dirs"], [f"resolved from MaSt at uninstall time/{values['TC_PAYLOAD_DIR_NAME']}"])
        self.assertEqual(finished["reboot_was_attempted"], False)
        mast_mocks.read_mast_volumes_conn.assert_not_called()
        mast_mocks.mounted_mast_volumes_conn.assert_not_called()

    def test_uninstall_dry_run_no_reboot_matches_no_reboot_execution_path(self) -> None:
        output = io.StringIO()
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
            "TC_PAYLOAD_DIR_NAME": "samba4",
        }
        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.uninstall.load_env_config", return_value=self.make_app_config(values)))
            self._patch_mast_volume_flow(stack, "uninstall")
            with redirect_stdout(output):
                rc = uninstall.main(["--dry-run", "--no-reboot"])
        self.assertEqual(rc, 0)
        text = output.getvalue()
        self.assertIn("Reboot:\n  no", text)
        self.assertIn("Post-uninstall checks:\n  none", text)
        self.assertNotIn("SSH returns after reboot", text)

    def test_uninstall_validates_only_host_and_payload_dir(self) -> None:
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "",
            "TC_SSH_OPTS": "-o foo",
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_MDNS_HOST_LABEL": "bad host label",
        }
        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.uninstall.load_env_config", return_value=self.make_app_config(values)))
            self._patch_mast_volume_flow(stack, "uninstall")
            with redirect_stdout(io.StringIO()):
                rc = uninstall.main(["--dry-run"])
        self.assertEqual(rc, 0)

    def test_uninstall_rejects_unsafe_payload_dir(self) -> None:
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "",
            "TC_SSH_OPTS": "-o foo",
            "TC_PAYLOAD_DIR_NAME": "../samba4",
        }
        with self.assertRaises(SystemExit) as ctx:
            with mock.patch("timecapsulesmb.cli.uninstall.load_env_config", return_value=self.make_app_config(values)):
                uninstall.main(["--dry-run"])
        self.assertIn("TC_PAYLOAD_DIR_NAME is invalid", str(ctx.exception))

    def test_uninstall_json_outputs_plan(self) -> None:
        output = io.StringIO()
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
            "TC_PAYLOAD_DIR_NAME": "samba4",
        }
        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.uninstall.load_env_config", return_value=self.make_app_config(values)))
            self._patch_mast_volume_flow(stack, "uninstall")
            with redirect_stdout(output):
                rc = uninstall.main(["--dry-run", "--json"])
        self.assertEqual(rc, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["host"], "root@10.0.0.2")
        self.assertEqual(payload["volume_roots"], ["resolved from MaSt at uninstall time"])
        self.assertEqual(payload["payload_dirs"], ["resolved from MaSt at uninstall time/samba4"])
        self.assertEqual(
            payload["reboot_request"],
            {
                "mode": "device_reboot",
                "strategy": "acp_then_ssh",
                "follow_up": ["wait_for_ssh_down", "wait_for_ssh_up"],
            },
        )
        self.assertEqual(
            [check["id"] for check in payload["post_uninstall_checks"]],
            [
                "ssh_goes_down_after_reboot",
                "ssh_returns_after_reboot",
                "managed_files_absent",
            ],
        )

    def test_uninstall_yes_reboots_and_verifies(self) -> None:
        output = io.StringIO()
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
            "TC_PAYLOAD_DIR_NAME": "samba4",
        }
        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.uninstall.load_env_config", return_value=self.make_app_config(values)))
            self._patch_mast_volume_flow(stack, "uninstall")
            uninstall_mock = stack.enter_context(mock.patch("timecapsulesmb.cli.uninstall.remote_uninstall_payload"))
            run_ssh_mock = stack.enter_context(mock.patch("timecapsulesmb.cli.flows.remote_request_reboot"))
            wait_mock = stack.enter_context(mock.patch("timecapsulesmb.cli.flows.wait_for_ssh_state_conn", side_effect=[True, True]))
            verify_mock = stack.enter_context(mock.patch("timecapsulesmb.cli.uninstall.verify_post_uninstall", return_value=VerificationResult(True, ())))
            with redirect_stdout(output):
                rc = uninstall.main(["--yes"])
        self.assertEqual(rc, 0)
        uninstall_mock.assert_called_once()
        run_ssh_mock.assert_called_once()
        self.assertEqual(wait_mock.call_args_list[0].args[0].host, "root@10.0.0.2")
        self.assertEqual(wait_mock.call_args_list[0].kwargs, {"expected_up": False, "timeout_seconds": 60})
        self.assertEqual(wait_mock.call_args_list[1].args[0].host, "root@10.0.0.2")
        self.assertEqual(wait_mock.call_args_list[1].kwargs, {"expected_up": True, "timeout_seconds": 240})
        verify_mock.assert_called_once()
        self.assertIn("Device is back online.", output.getvalue())
        finished = self.telemetry_payload("uninstall_finished")
        self.assertEqual(finished["result"], "success")
        self.assertEqual(finished["reboot_was_attempted"], True)
        self.assertEqual(finished["device_came_back_after_reboot"], True)
        self.assertEqual(finished["post_uninstall_verified"], True)

    def test_uninstall_reboot_request_timeout_continues_when_device_reboots(self) -> None:
        output = io.StringIO()
        values = self.make_valid_env()
        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.uninstall.load_env_config", return_value=self.make_app_config(values)))
            self._patch_mast_volume_flow(stack, "uninstall")
            stack.enter_context(mock.patch("timecapsulesmb.cli.uninstall.remote_uninstall_payload"))
            stack.enter_context(
                mock.patch(
                    "timecapsulesmb.cli.flows.remote_request_reboot",
                    side_effect=SshCommandTimeout("Timed out waiting for ssh command to finish: reboot"),
                )
            )
            wait_mock = stack.enter_context(mock.patch("timecapsulesmb.cli.flows.wait_for_ssh_state_conn", side_effect=[True, True]))
            verify_mock = stack.enter_context(mock.patch("timecapsulesmb.cli.uninstall.verify_post_uninstall", return_value=VerificationResult(True, ())))
            with redirect_stdout(output):
                rc = uninstall.main(["--yes"])

        self.assertEqual(rc, 0)
        self.assertEqual(wait_mock.call_args_list[0].kwargs, {"expected_up": False, "timeout_seconds": 60})
        self.assertEqual(wait_mock.call_args_list[1].kwargs, {"expected_up": True, "timeout_seconds": 240})
        verify_mock.assert_called_once()
        text = output.getvalue()
        self.assertIn("SSH reboot request timed out; checking whether the device is rebooting...", text)
        self.assertIn("Device is back online.", text)
        finished = self.telemetry_payload("uninstall_finished")
        self.assertEqual(finished["result"], "success")
        self.assertEqual(finished["reboot_was_attempted"], True)
        self.assertEqual(finished["device_came_back_after_reboot"], True)
        self.assertEqual(finished["post_uninstall_verified"], True)

    def test_uninstall_reboot_request_timeout_fails_when_device_never_goes_down(self) -> None:
        output = io.StringIO()
        values = self.make_valid_env()
        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.uninstall.load_env_config", return_value=self.make_app_config(values)))
            self._patch_mast_volume_flow(stack, "uninstall")
            stack.enter_context(mock.patch("timecapsulesmb.cli.uninstall.remote_uninstall_payload"))
            stack.enter_context(
                mock.patch(
                    "timecapsulesmb.cli.flows.remote_request_reboot",
                    side_effect=SshCommandTimeout("Timed out waiting for ssh command to finish: reboot"),
                )
            )
            wait_mock = stack.enter_context(mock.patch("timecapsulesmb.cli.flows.wait_for_ssh_state_conn", return_value=False))
            verify_mock = stack.enter_context(mock.patch("timecapsulesmb.cli.uninstall.verify_post_uninstall"))
            with redirect_stdout(output):
                rc = uninstall.main(["--yes"])

        self.assertEqual(rc, 1)
        wait_mock.assert_called_once()
        verify_mock.assert_not_called()
        text = output.getvalue()
        self.assertIn("Reboot was requested but the device did not go down.", text)
        self.assertIn("The uninstall removed managed TimeCapsuleSMB files before reboot; power-cycle or rerun uninstall.", text)
        finished = self.telemetry_payload("uninstall_finished")
        self.assertEqual(finished["result"], "failure")
        self.assertEqual(finished["reboot_was_attempted"], True)
        self.assertEqual(finished["device_came_back_after_reboot"], False)
        self.assertEqual(finished["post_uninstall_verified"], False)
        self.assertIn("stage=wait_for_reboot_down", finished["error"])
        self.assertIn("ssh_reboot_timed_out=true", finished["error"])

    def test_uninstall_no_reboot_skips_reboot_and_returns_success(self) -> None:
        output = io.StringIO()
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
            "TC_PAYLOAD_DIR_NAME": "samba4",
        }
        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.uninstall.load_env_config", return_value=self.make_app_config(values)))
            self._patch_mast_volume_flow(stack, "uninstall")
            uninstall_mock = stack.enter_context(mock.patch("timecapsulesmb.cli.uninstall.remote_uninstall_payload"))
            run_ssh_mock = stack.enter_context(mock.patch("timecapsulesmb.cli.flows.remote_request_reboot"))
            verify_mock = stack.enter_context(mock.patch("timecapsulesmb.cli.uninstall.verify_post_uninstall"))
            with redirect_stdout(output):
                rc = uninstall.main(["--no-reboot"])
        self.assertEqual(rc, 0)
        uninstall_mock.assert_called_once()
        run_ssh_mock.assert_not_called()
        verify_mock.assert_not_called()
        self.assertIn("Skipping reboot.", output.getvalue())

    def test_uninstall_without_mounted_hfs_volumes_removes_flash_and_runtime_only(self) -> None:
        output = io.StringIO()
        values = self.make_valid_env(TC_PAYLOAD_DIR_NAME="samba4")

        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.uninstall.load_env_config", return_value=self.make_app_config(values)))
            self._patch_mast_volume_flow(stack, "uninstall", mounted_volumes=(), read_volumes=(self._mast_volume("dk5", builtin=False),))
            uninstall_mock = stack.enter_context(mock.patch("timecapsulesmb.cli.uninstall.remote_uninstall_payload"))
            with redirect_stdout(output):
                rc = uninstall.main(["--no-reboot"])

        self.assertEqual(rc, 0)
        plan = uninstall_mock.call_args.args[1]
        self.assertEqual(plan.volume_roots, [])
        self.assertEqual(plan.payload_dirs, [])
        self.assertIn("No mounted HFS volumes found; removing flash hooks and runtime state only.", output.getvalue())

    def test_uninstall_declined_reboot_skips_reboot_and_returns_success(self) -> None:
        output = io.StringIO()
        prompt_text = []
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_AIRPORT_SYAP": "120",
            "TC_MDNS_DEVICE_MODEL": "AirPort7,120",
        }

        def fake_input(prompt: str) -> str:
            prompt_text.append(prompt)
            return "n"

        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.uninstall.load_env_config", return_value=self.make_app_config(values)))
            self._patch_mast_volume_flow(stack, "uninstall")
            stack.enter_context(mock.patch("timecapsulesmb.cli.uninstall.remote_uninstall_payload"))
            stack.enter_context(mock.patch("builtins.input", side_effect=fake_input))
            run_ssh_mock = stack.enter_context(mock.patch("timecapsulesmb.cli.flows.remote_request_reboot"))
            with redirect_stdout(output):
                rc = uninstall.main([])
        self.assertEqual(rc, 0)
        run_ssh_mock.assert_not_called()
        self.assertEqual(prompt_text, ["This will reboot the AirPort Extreme 6th generation now. Continue? [Y/n]: "])
        self.assertIn("Skipped reboot. The AirPort Extreme 6th generation may need a manual reboot", output.getvalue())
        finished = self.telemetry_payload("uninstall_finished")
        self.assertEqual(finished["result"], "success")
        self.assertEqual(finished["reboot_was_attempted"], False)

    def test_uninstall_verify_failure_emits_failure_stage(self) -> None:
        output = io.StringIO()
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
            "TC_PAYLOAD_DIR_NAME": "samba4",
        }
        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.uninstall.load_env_config", return_value=self.make_app_config(values)))
            self._patch_mast_volume_flow(stack, "uninstall")
            stack.enter_context(mock.patch("timecapsulesmb.cli.uninstall.remote_uninstall_payload"))
            stack.enter_context(mock.patch("timecapsulesmb.cli.flows.remote_request_reboot"))
            stack.enter_context(mock.patch("timecapsulesmb.cli.flows.wait_for_ssh_state_conn", side_effect=[True, True]))
            stack.enter_context(mock.patch("timecapsulesmb.cli.uninstall.verify_post_uninstall", return_value=VerificationResult(False, ())))
            with redirect_stdout(output):
                rc = uninstall.main(["--yes"])
        self.assertEqual(rc, 1)
        self.assertIn("Managed TimeCapsuleSMB files are still present after reboot.", output.getvalue())
        finished = self.telemetry_payload("uninstall_finished")
        self.assertEqual(finished["result"], "failure")
        self.assertEqual(finished["reboot_was_attempted"], True)
        self.assertEqual(finished["device_came_back_after_reboot"], True)
        self.assertEqual(finished["post_uninstall_verified"], False)
        self.assertIn("stage=verify_post_uninstall", finished["error"])

    def test_fsck_yes_reboots_and_waits_by_default(self) -> None:
        output = io.StringIO()
        values = self.make_valid_env()
        run_result = mock.Mock(stdout="--- fsck_hfs /dev/dk2 ---\nOK\n--- reboot ---\n", returncode=255)
        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.fsck.load_env_config", return_value=self.make_app_config(values)))
            self._patch_mast_volume_flow(stack, "fsck", mounted_volumes=(self._mast_volume("dk2"),))
            run_ssh_mock = stack.enter_context(mock.patch("timecapsulesmb.cli.fsck.run_ssh", return_value=run_result))
            wait_mock = stack.enter_context(mock.patch("timecapsulesmb.cli.flows.wait_for_ssh_state_conn", side_effect=[True, True]))
            with redirect_stdout(output):
                rc = fsck.main(["--yes"])
        self.assertEqual(rc, 0)
        run_ssh_mock.assert_called_once()
        self.assertEqual(run_ssh_mock.call_args.kwargs["timeout"], fsck.FSCK_REMOTE_COMMAND_TIMEOUT_SECONDS)
        self.assertEqual(fsck.FSCK_REMOTE_COMMAND_TIMEOUT_SECONDS, 10800)
        remote_cmd = run_ssh_mock.call_args.args[1]
        self.assertIn("tc_kill_watchdog_pids KILL", remote_cmd)
        self.assertIn("/mnt/Flash/watchdog.sh", remote_cmd)
        self.assertNotIn("pkill -9 -f", remote_cmd)
        self.assertIn("^smbd$", remote_cmd)
        self.assertIn("^afpserver$", remote_cmd)
        self.assertIn("^wcifsnd$", remote_cmd)
        self.assertIn("^wcifsfs$", remote_cmd)
        self.assertIn("umount -f /Volumes/dk2", remote_cmd)
        self.assertIn("fsck_hfs -fy /dev/dk2", remote_cmd)
        self.assertIn("/sbin/reboot", remote_cmd)
        self.assertEqual(wait_mock.call_args_list[0].kwargs, {"expected_up": False, "timeout_seconds": 90})
        self.assertEqual(wait_mock.call_args_list[1].kwargs, {"expected_up": True, "timeout_seconds": 420})
        text = output.getvalue()
        self.assertIn("Mounted HFS volume: /dev/dk2 on /Volumes/dk2", text)
        self.assertIn("--- fsck_hfs /dev/dk2 ---", text)
        self.assertIn("Device is back online.", text)
        started = self.telemetry_payload("fsck_started")
        finished = self.telemetry_payload("fsck_finished")
        self.assertEqual(started["command_id"], finished["command_id"])
        self.assertEqual(finished["result"], "success")
        self.assertEqual(finished["reboot_was_attempted"], True)
        self.assertEqual(finished["device_came_back_after_reboot"], True)
        self.assertEqual(finished["fsck_device"], "/dev/dk2")
        self.assertEqual(finished["fsck_mountpoint"], "/Volumes/dk2")

    def test_fsck_validates_only_host(self) -> None:
        output = io.StringIO()
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "",
            "TC_SSH_OPTS": "-o foo",
            "TC_PAYLOAD_DIR_NAME": "../bad",
        }
        run_result = mock.Mock(stdout="--- fsck_hfs /dev/dk2 ---\nOK\n", returncode=0)
        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.fsck.load_env_config", return_value=self.make_app_config(values)))
            self._patch_mast_volume_flow(stack, "fsck", mounted_volumes=(self._mast_volume("dk2"),))
            stack.enter_context(mock.patch("timecapsulesmb.cli.fsck.run_ssh", return_value=run_result))
            with redirect_stdout(output):
                rc = fsck.main(["--yes", "--no-reboot"])
        self.assertEqual(rc, 0)

    def test_fsck_no_wait_skips_ssh_waits(self) -> None:
        output = io.StringIO()
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
        }
        run_result = mock.Mock(stdout="--- fsck_hfs /dev/dk2 ---\nOK\n--- reboot ---\n", returncode=255)
        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.fsck.load_env_config", return_value=self.make_app_config(values)))
            self._patch_mast_volume_flow(stack, "fsck", mounted_volumes=(self._mast_volume("dk2"),))
            stack.enter_context(mock.patch("timecapsulesmb.cli.fsck.run_ssh", return_value=run_result))
            observe_mock = stack.enter_context(mock.patch("timecapsulesmb.cli.fsck.observe_reboot_cycle"))
            with redirect_stdout(output):
                rc = fsck.main(["--yes", "--no-wait"])
        self.assertEqual(rc, 0)
        observe_mock.assert_not_called()

    def test_fsck_no_reboot_omits_reboot_and_waits(self) -> None:
        output = io.StringIO()
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
        }
        run_result = mock.Mock(stdout="--- fsck_hfs /dev/dk2 ---\nOK\n", returncode=0)
        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.fsck.load_env_config", return_value=self.make_app_config(values)))
            self._patch_mast_volume_flow(stack, "fsck", mounted_volumes=(self._mast_volume("dk2"),))
            run_ssh_mock = stack.enter_context(mock.patch("timecapsulesmb.cli.fsck.run_ssh", return_value=run_result))
            observe_mock = stack.enter_context(mock.patch("timecapsulesmb.cli.fsck.observe_reboot_cycle"))
            with redirect_stdout(output):
                rc = fsck.main(["--yes", "--no-reboot"])
        self.assertEqual(rc, 0)
        observe_mock.assert_not_called()
        self.assertEqual(run_ssh_mock.call_args.kwargs["timeout"], fsck.FSCK_REMOTE_COMMAND_TIMEOUT_SECONDS)
        self.assertNotIn("/sbin/reboot", run_ssh_mock.call_args.args[1])

    def test_fsck_prompt_decline_cancels_before_remote_actions(self) -> None:
        output = io.StringIO()
        prompt_text = []
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
            "TC_AIRPORT_SYAP": "120",
            "TC_MDNS_DEVICE_MODEL": "AirPort7,120",
        }
        def fake_input(prompt: str) -> str:
            prompt_text.append(prompt)
            return "n"

        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.fsck.load_env_config", return_value=self.make_app_config(values)))
            self._patch_mast_volume_flow(stack, "fsck", mounted_volumes=(self._mast_volume("dk2"),))
            stack.enter_context(mock.patch("builtins.input", side_effect=fake_input))
            run_ssh_mock = stack.enter_context(mock.patch("timecapsulesmb.cli.fsck.run_ssh"))
            with redirect_stdout(output):
                rc = fsck.main([])
        self.assertEqual(rc, 0)
        run_ssh_mock.assert_not_called()
        self.assertEqual(
            prompt_text,
            ["This will stop file sharing, unmount the disk, run fsck_hfs, and reboot the AirPort Extreme 6th generation. Continue? [Y/n]: "],
        )
        self.assertIn("fsck cancelled.", output.getvalue())
        finished = self.telemetry_payload("fsck_finished")
        self.assertEqual(finished["result"], "cancelled")
        self.assertIn("Cancelled by user at fsck confirmation prompt.", finished["error"])

    def test_fsck_no_mounted_hfs_volumes_exits_with_clear_message(self) -> None:
        output = io.StringIO()
        values = self.make_valid_env()
        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.fsck.load_env_config", return_value=self.make_app_config(values)))
            self._patch_mast_volume_flow(stack, "fsck", mounted_volumes=())
            run_ssh_mock = stack.enter_context(mock.patch("timecapsulesmb.cli.fsck.run_ssh"))
            with self.assertRaises(SystemExit) as ctx:
                with redirect_stdout(output):
                    fsck.main(["--yes"])

        self.assertEqual(str(ctx.exception), "no mounted HFS volumes found")
        run_ssh_mock.assert_not_called()
        self.assertNotIn("MaSt", str(ctx.exception))

    def test_fsck_prompts_for_volume_when_multiple_hfs_volumes_are_mounted(self) -> None:
        output = io.StringIO()
        values = self.make_valid_env()
        internal = self._mast_volume("dk2", name="Internal", builtin=True)
        external = self._mast_volume("dk5", disk_device="sd0", name="External", builtin=False)
        run_result = mock.Mock(stdout="--- fsck_hfs /dev/dk5 ---\nOK\n", returncode=0)

        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.fsck.load_env_config", return_value=self.make_app_config(values)))
            self._patch_mast_volume_flow(stack, "fsck", mounted_volumes=(internal, external))
            stack.enter_context(mock.patch("builtins.input", side_effect=["2", "y"]))
            run_ssh_mock = stack.enter_context(mock.patch("timecapsulesmb.cli.fsck.run_ssh", return_value=run_result))
            with redirect_stdout(output):
                rc = fsck.main(["--no-reboot"])

        self.assertEqual(rc, 0)
        remote_cmd = run_ssh_mock.call_args.args[1]
        self.assertIn("umount -f /Volumes/dk5", remote_cmd)
        self.assertIn("fsck_hfs -fy /dev/dk5", remote_cmd)
        text = output.getvalue()
        self.assertIn("Mounted HFS volumes:", text)
        self.assertIn("2. /dev/dk5 on /Volumes/dk5 (External, external)", text)
        self.assertIn("Mounted HFS volume: /dev/dk5 on /Volumes/dk5", text)

    def test_fsck_yes_with_multiple_hfs_volumes_requires_selector(self) -> None:
        output = io.StringIO()
        values = self.make_valid_env()
        internal = self._mast_volume("dk2", name="Internal", builtin=True)
        external = self._mast_volume("dk5", disk_device="sd0", name="External", builtin=False)

        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.fsck.load_env_config", return_value=self.make_app_config(values)))
            self._patch_mast_volume_flow(stack, "fsck", mounted_volumes=(internal, external))
            input_mock = stack.enter_context(mock.patch("builtins.input", side_effect=AssertionError("fsck --yes should not prompt")))
            run_ssh_mock = stack.enter_context(mock.patch("timecapsulesmb.cli.fsck.run_ssh"))
            with self.assertRaises(SystemExit) as ctx:
                with redirect_stdout(output):
                    fsck.main(["--yes", "--no-reboot"])

        self.assertEqual(str(ctx.exception), "multiple mounted HFS volumes found; specify --volume to select one")
        input_mock.assert_not_called()
        run_ssh_mock.assert_not_called()
        self.assertNotIn("Mounted HFS volumes:", output.getvalue())

    def test_fsck_volume_selector_skips_multiple_volume_prompt(self) -> None:
        output = io.StringIO()
        values = self.make_valid_env()
        internal = self._mast_volume("dk2", name="Internal", builtin=True)
        external = self._mast_volume("dk5", disk_device="sd0", name="External", builtin=False)
        run_result = mock.Mock(stdout="--- fsck_hfs /dev/dk5 ---\nOK\n", returncode=0)

        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.fsck.load_env_config", return_value=self.make_app_config(values)))
            self._patch_mast_volume_flow(stack, "fsck", mounted_volumes=(internal, external))
            stack.enter_context(mock.patch("builtins.input", side_effect=AssertionError("volume prompt should not run")))
            run_ssh_mock = stack.enter_context(mock.patch("timecapsulesmb.cli.fsck.run_ssh", return_value=run_result))
            with redirect_stdout(output):
                rc = fsck.main(["--yes", "--no-reboot", "--volume", "dk5"])

        self.assertEqual(rc, 0)
        self.assertIn("fsck_hfs -fy /dev/dk5", run_ssh_mock.call_args.args[1])
        self.assertNotIn("Mounted HFS volumes:", output.getvalue())

    def test_fsck_reboot_no_down_emits_failure_stage(self) -> None:
        output = io.StringIO()
        values = self.make_valid_env()
        run_result = mock.Mock(stdout="--- fsck_hfs /dev/dk2 ---\nOK\n--- reboot ---\n", returncode=255)
        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.fsck.load_env_config", return_value=self.make_app_config(values)))
            self._patch_mast_volume_flow(stack, "fsck", mounted_volumes=(self._mast_volume("dk2"),))
            stack.enter_context(mock.patch("timecapsulesmb.cli.fsck.run_ssh", return_value=run_result))
            wait_mock = stack.enter_context(mock.patch("timecapsulesmb.cli.flows.wait_for_ssh_state_conn", return_value=False))
            with redirect_stdout(output):
                rc = fsck.main(["--yes"])
        self.assertEqual(rc, 1)
        wait_mock.assert_called_once()
        self.assertIn("fsck requested reboot from the device, but SSH did not go down.", output.getvalue())
        finished = self.telemetry_payload("fsck_finished")
        self.assertEqual(finished["result"], "failure")
        self.assertEqual(finished["reboot_was_attempted"], True)
        self.assertEqual(finished["device_came_back_after_reboot"], False)
        self.assertIn("stage=wait_for_reboot_down", finished["error"])

    def test_fsck_reboot_timeout_emits_failure_stage(self) -> None:
        output = io.StringIO()
        values = self.make_valid_env()
        run_result = mock.Mock(stdout="--- fsck_hfs /dev/dk2 ---\nOK\n--- reboot ---\n", returncode=255)
        with ExitStack() as stack:
            stack.enter_context(mock.patch("timecapsulesmb.cli.fsck.load_env_config", return_value=self.make_app_config(values)))
            self._patch_mast_volume_flow(stack, "fsck", mounted_volumes=(self._mast_volume("dk2"),))
            stack.enter_context(mock.patch("timecapsulesmb.cli.fsck.run_ssh", return_value=run_result))
            stack.enter_context(mock.patch("timecapsulesmb.cli.flows.wait_for_ssh_state_conn", side_effect=[True, False]))
            with redirect_stdout(output):
                rc = fsck.main(["--yes"])
        self.assertEqual(rc, 1)
        self.assertIn("Timed out waiting for SSH after reboot.", output.getvalue())
        finished = self.telemetry_payload("fsck_finished")
        self.assertEqual(finished["result"], "failure")
        self.assertEqual(finished["reboot_was_attempted"], True)
        self.assertEqual(finished["device_came_back_after_reboot"], False)
        self.assertIn("stage=wait_for_reboot_up", finished["error"])

    def test_discover_json_outputs_records(self) -> None:
        output = io.StringIO()
        record = Discovered(
            name="Time Capsule",
            hostname="capsule.local",
            ipv4=["10.0.0.2"],
            ipv6=[],
            services={"_airport._tcp.local."},
            properties={"model": "AirPort Time Capsule"},
        )
        snapshot = BonjourDiscoverySnapshot(
            instances=[
                BonjourServiceInstance("_airport._tcp.local.", "Time Capsule", "Time Capsule._airport._tcp.local."),
            ],
            resolved=[record],
        )
        with mock.patch("timecapsulesmb.cli.discover.ensure_install_id"):
            with mock.patch("timecapsulesmb.cli.discover.discover_snapshot", return_value=snapshot):
                with redirect_stdout(output):
                    rc = discover.main(["--json"])
        self.assertEqual(rc, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["instances"][0]["name"], "Time Capsule")
        self.assertEqual(payload["resolved"][0]["name"], "Time Capsule")
        started = self.telemetry_payload("discover_started")
        finished = self.telemetry_payload("discover_finished")
        self.assertTrue(started["json_output"])
        self.assertEqual(finished["result"], "success")
        self.assertEqual(finished["bonjour_instance_count"], 1)
        self.assertEqual(finished["bonjour_resolved_count"], 1)





if __name__ == "__main__":
    unittest.main()
