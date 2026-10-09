"""The configure command."""
from __future__ import annotations

import errno
import io
import json
import os
import socket
import tempfile
import unittest
import uuid
from contextlib import ExitStack
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from timecapsulesmb.cli import configure
from timecapsulesmb.core.config import DEFAULTS, default_env_path, render_env_text
from timecapsulesmb.device.probe import ProbeResult, ProbedDeviceState, SshAccessStatus
from timecapsulesmb.discovery.bonjour import (
    BonjourDiscoverySnapshot,
    BonjourQueryDiagnostics,
    BonjourResolvedService,
)
from timecapsulesmb.cli.util import ANSI_RED, ANSI_RESET
from timecapsulesmb.integrations.acp import ACPAuthError, ACPConnectionError

from timecapsulesmb.services.configure import enable_ssh_and_reprobe as real_enable_ssh_and_reprobe
from tests.cli_support import CliTestCase, FakeCommandContext
from tests.reboot_support import acp_reading


class CliConfigureTests(CliTestCase):
    def make_probe_result_unreachable(self) -> ProbeResult:
        return ProbeResult(
            ssh_status=SshAccessStatus.CLOSED,
            error="SSH is not reachable yet.",
            os_name="",
            os_release="",
            arch="",
            elf_endianness="unknown",
        )

    def make_probe_result_auth_failed(self) -> ProbeResult:
        return ProbeResult(
            ssh_status=SshAccessStatus.AUTH_REJECTED,
            error="SSH authentication failed.",
            os_name="",
            os_release="",
            arch="",
            elf_endianness="unknown",
        )

    def make_probe_result_netbsd6_no_identity(self) -> ProbeResult:
        return ProbeResult(
            ssh_status=SshAccessStatus.OPEN_AUTHENTICATED,
            error=None,
            os_name="NetBSD",
            os_release="6.0",
            arch="earmv4",
            elf_endianness="little",
        )

    def make_probe_result_netbsd6_unknown(self) -> ProbeResult:
        return ProbeResult(
            ssh_status=SshAccessStatus.OPEN_AUTHENTICATED,
            error=None,
            os_name="NetBSD",
            os_release="6.0",
            arch="earmv4",
            elf_endianness="unknown",
        )

    def make_probe_result_netbsd6_big(self) -> ProbeResult:
        return ProbeResult(
            ssh_status=SshAccessStatus.OPEN_AUTHENTICATED,
            error=None,
            os_name="NetBSD",
            os_release="6.0",
            arch="earmv4",
            elf_endianness="big",
        )

    def make_probe_result_netbsd4le(self) -> ProbeResult:
        return ProbeResult(
            ssh_status=SshAccessStatus.OPEN_AUTHENTICATED,
            error=None,
            os_name="NetBSD",
            os_release="4.0",
            arch="earmv4",
            elf_endianness="little",
        )

    def make_probe_result_netbsd4le_airport_identity_113(self) -> ProbeResult:
        return ProbeResult(
            ssh_status=SshAccessStatus.OPEN_AUTHENTICATED,
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
            ssh_status=SshAccessStatus.OPEN_AUTHENTICATED,
            error=None,
            os_name="NetBSD",
            os_release="4.0",
            arch="earmv4",
            elf_endianness="big",
        )

    def make_probe_result_netbsd4be_airport_identity_106(self) -> ProbeResult:
        return ProbeResult(
            ssh_status=SshAccessStatus.OPEN_AUTHENTICATED,
            error=None,
            os_name="NetBSD",
            os_release="4.0",
            arch="earmv4",
            elf_endianness="big",
            airport_model="TimeCapsule6,106",
            airport_syap="106",
        )

    def make_probe_result_netbsd5(self) -> ProbeResult:
        return ProbeResult(
            ssh_status=SshAccessStatus.OPEN_AUTHENTICATED,
            error=None,
            os_name="NetBSD",
            os_release="5.0",
            arch="earmv4",
            elf_endianness="little",
        )

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
        discovered_records: list[BonjourResolvedService] | None = None,
        discovery_side_effect=None,
        discovered_root_host: str | None = None,
        input_side_effect=None,
        prompt_side_effect=None,
        probe_state: ProbedDeviceState | None = None,
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

        def capture_write_env(_path, values, **_kwargs):
            written_values.update(values)

        with ExitStack() as stack:
            if ensure_install_id:
                mocks.ensure_install_id = stack.enter_context(mock.patch("timecapsulesmb.cli.configure.ensure_install_id"))
            mocks.parse_env_file = stack.enter_context(
                mock.patch("timecapsulesmb.cli.configure.parse_env_file", return_value=dict(existing_values or {}))
            )
            if discovery_side_effect is not None:
                mocks.discover_snapshot_detailed = stack.enter_context(
                    mock.patch("timecapsulesmb.cli.configure.discover_snapshot_detailed", side_effect=discovery_side_effect)
                )
            else:
                discovery_records = list(discovered_records or [])
                discovery_snapshot = BonjourDiscoverySnapshot(instances=[], resolved=discovery_records)
                discovery_diagnostics = BonjourQueryDiagnostics(
                    provider="zeroconf",
                    service_types=["_airport._tcp.local."],
                    timeout_sec=6.0,
                    elapsed_sec=0.0,
                    instance_count=0,
                    resolved_count=len(discovery_records),
                )
                mocks.discover_snapshot_detailed = stack.enter_context(
                    mock.patch(
                        "timecapsulesmb.cli.configure.discover_snapshot_detailed",
                        return_value=(discovery_snapshot, discovery_diagnostics),
                    )
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
            if confirm is not None:
                mocks.confirm = stack.enter_context(mock.patch("timecapsulesmb.cli.configure.confirm", return_value=confirm))
            mocks.write_env_file = stack.enter_context(
                mock.patch(
                    "timecapsulesmb.cli.configure.write_configure_env_file",
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
                return "root@10.0.0.2"
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

    def force_configure_acp_reprobe_auth_failed(self) -> None:
        self._configure_acp_probe_mock.side_effect = [self.make_probe_state(self.make_probe_result_auth_failed())]

    def test_configure_writes_values_from_prompts(self) -> None:
        prompt_values = iter([
            "root@10.0.0.2",
            "pw",
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
        self.assertNotIn("TC_SAMBA_USER", fake_values)
        self.assertNotIn("TC_PAYLOAD_DIR_NAME", fake_values)
        self.assertEqual(fake_values["TC_AIRPORT_SYAP"], "119")
        self.assertNotIn("TC_MDNS_DEVICE_MODEL", fake_values)
        rendered_env = render_env_text(fake_values)
        self.assertNotIn("TC_AIRPORT_SYAP", rendered_env)
        self.assertNotIn("TC_MDNS_DEVICE_MODEL", rendered_env)
        self.assertNotIn("TC_NET_IFACE", fake_values)
        self.assertEqual(fake_values["TC_INTERNAL_SHARE_USE_DISK_ROOT"], "false")
        self.assertNotIn("TC_SMB_BIND_LAN_ONLY", fake_values)
        self.assertEqual(fake_values["TC_SMB_BROWSE_COMPATIBILITY"], "false")
        self.assertEqual(fake_values["TC_MDNS_ADVERTISE_AFP"], "false")
        self.assertEqual(fake_values["TC_ANY_PROTOCOL"], "false")
        self.assertEqual(fake_values["TC_REQUIRE_SMB_ENCRYPTION"], "false")
        self.assertEqual(fake_values["TC_FORCE_DISABLE_SMB_SIGNING_AND_ENCRYPTION"], "false")
        self.assertEqual(fake_values["TC_FRUIT_METADATA_NETATALK"], "true")
        self.assertEqual(fake_values["TC_ATA_IDLE_SECONDS"], "300")
        self.assertEqual(fake_values["TC_ATA_STANDBY"], "")
        uuid.UUID(fake_values["TC_CONFIGURE_ID"])
        telemetry_values = result.mocks.telemetry_factory.call_args.args[0].values
        self.assertEqual(telemetry_values["TC_CONFIGURE_ID"], fake_values["TC_CONFIGURE_ID"])
        self.assertEqual(result.mocks.command_context_factory.call_args.kwargs["configure_id"], fake_values["TC_CONFIGURE_ID"])
        command_context.finish.assert_called_once()
        self.assertEqual(command_context.finish.call_args.kwargs["configure_id"], fake_values["TC_CONFIGURE_ID"])
        self.assertEqual(command_context.finish.call_args.kwargs["device_syap"], "119")
        self.assertEqual(command_context.finish.call_args.kwargs["device_model"], "TimeCapsule8,119")
        text = result.text
        self.assertIn("This writes a local .env configuration file", text)
        self.assertIn(f"Review the .env file configuration: wrote {default_env_path()}", text)
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

    def test_configure_hidden_internal_share_use_disk_root_arg_writes_true(self) -> None:
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
            ["--internal-share-use-disk-root"],
            prompt_side_effect=lambda label, default, _secret: default if label == "mDNS device model hint" else next(prompt_values),
            probe_state=self.make_probe_state(self.make_probe_result_unreachable()),
            confirm=True,
            command_context=FakeCommandContext(),
        )
        self.assertEqual(result.rc, 0)
        self.assertEqual(result.values["TC_INTERNAL_SHARE_USE_DISK_ROOT"], "true")

    def test_configure_hidden_smb_browse_compatibility_arg_writes_true(self) -> None:
        result = self.run_configure_cli(
            ["--smb-browse-compatibility"],
            prompt_side_effect=self.configure_prompt_defaults(),
            probe_state=self.make_probe_state(self.make_probe_result_unreachable()),
            confirm=True,
            command_context=FakeCommandContext(),
        )
        self.assertEqual(result.rc, 0)
        self.assertEqual(result.values["TC_SMB_BROWSE_COMPATIBILITY"], "true")

    def test_configure_hidden_no_mdns_advertise_afp_arg_writes_false(self) -> None:
        result = self.run_configure_cli(
            ["--no-mdns-advertise-afp"],
            prompt_side_effect=self.configure_prompt_defaults(),
            probe_state=self.make_probe_state(self.make_probe_result_unreachable()),
            confirm=True,
            command_context=FakeCommandContext(),
        )
        self.assertEqual(result.rc, 0)
        self.assertEqual(result.values["TC_MDNS_ADVERTISE_AFP"], "false")

    def test_configure_hidden_mdns_advertise_afp_arg_writes_true(self) -> None:
        result = self.run_configure_cli(
            ["--mdns-advertise-afp"],
            prompt_side_effect=self.configure_prompt_defaults(),
            probe_state=self.make_probe_state(self.make_probe_result_unreachable()),
            confirm=True,
            command_context=FakeCommandContext(),
        )
        self.assertEqual(result.rc, 0)
        self.assertEqual(result.values["TC_MDNS_ADVERTISE_AFP"], "true")

    def test_configure_hidden_any_protocol_arg_writes_true(self) -> None:
        result = self.run_configure_cli(
            ["--any-protocol"],
            prompt_side_effect=self.configure_prompt_defaults(),
            probe_state=self.make_probe_state(self.make_probe_result_unreachable()),
            confirm=True,
            command_context=FakeCommandContext(),
        )
        self.assertEqual(result.rc, 0)
        self.assertEqual(result.values["TC_ANY_PROTOCOL"], "true")

    def test_configure_require_smb_encryption_arg_writes_true(self) -> None:
        result = self.run_configure_cli(
            ["--require-smb-encryption"],
            prompt_side_effect=self.configure_prompt_defaults(),
            probe_state=self.make_probe_state(self.make_probe_result_unreachable()),
            confirm=True,
            command_context=FakeCommandContext(),
        )
        self.assertEqual(result.rc, 0)
        self.assertEqual(result.values["TC_REQUIRE_SMB_ENCRYPTION"], "true")

    def test_configure_force_disable_smb_signing_and_encryption_arg_writes_true(self) -> None:
        result = self.run_configure_cli(
            ["--disable-smb-security"],
            prompt_side_effect=self.configure_prompt_defaults(),
            probe_state=self.make_probe_state(self.make_probe_result_unreachable()),
            confirm=True,
            command_context=FakeCommandContext(),
        )
        self.assertEqual(result.rc, 0)
        self.assertEqual(result.values["TC_FORCE_DISABLE_SMB_SIGNING_AND_ENCRYPTION"], "true")

    def test_configure_rejects_required_and_disabled_smb_encryption(self) -> None:
        with redirect_stderr(io.StringIO()):
            result = self.run_configure_cli(
                ["--require-smb-encryption", "--disable-smb-security"],
                raises=SystemExit,
            )
        self.assertEqual(result.exception.code, 2)

    def test_configure_force_disable_smb_security_args_are_mutually_exclusive(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            result = self.run_configure_cli(
                [
                    "--disable-smb-security",
                    "--no-disable-smb-security",
                ],
                raises=SystemExit,
            )
        self.assertEqual(result.exception.code, 2)
        self.assertIn("not allowed with argument", stderr.getvalue())

    def test_configure_rejects_any_protocol_with_smb_encryption(self) -> None:
        with redirect_stderr(io.StringIO()):
            result = self.run_configure_cli(
                ["--any-protocol", "--require-smb-encryption"],
                raises=SystemExit,
            )
        self.assertEqual(result.exception.code, 2)

    def test_configure_can_disable_any_protocol_when_requiring_smb_encryption(self) -> None:
        result = self.run_configure_cli(
            ["--no-any-protocol", "--require-smb-encryption"],
            existing_values=self.make_valid_env(TC_ANY_PROTOCOL="true"),
            prompt_side_effect=self.configure_prompt_defaults(),
            probe_state=self.make_probe_state(self.make_probe_result_unreachable()),
            confirm=True,
            command_context=FakeCommandContext(),
        )
        self.assertEqual(result.rc, 0)
        self.assertEqual(result.values["TC_ANY_PROTOCOL"], "false")
        self.assertEqual(result.values["TC_REQUIRE_SMB_ENCRYPTION"], "true")

    def test_configure_netatalk_arg_writes_true(self) -> None:
        result = self.run_configure_cli(
            ["--netatalk"],
            prompt_side_effect=self.configure_prompt_defaults(),
            probe_state=self.make_probe_state(self.make_probe_result_unreachable()),
            confirm=True,
            command_context=FakeCommandContext(),
        )
        self.assertEqual(result.rc, 0)
        self.assertEqual(result.values["TC_FRUIT_METADATA_NETATALK"], "true")

    def test_configure_vfs_aio_fork_args_enable_and_disable_saved_value(self) -> None:
        enabled = self.run_configure_cli(
            ["--enable-vfs-aio-fork"],
            prompt_side_effect=self.configure_prompt_defaults(),
            probe_state=self.make_probe_state(self.make_probe_result_unreachable()),
            confirm=True,
            command_context=FakeCommandContext(),
        )
        disabled = self.run_configure_cli(
            ["--disable-vfs-aio-fork"],
            existing_values=self.make_valid_env(TC_VFS_AIO_FORK_ENABLED="true"),
            prompt_side_effect=self.configure_prompt_defaults(),
            probe_state=self.make_probe_state(self.make_probe_result_unreachable()),
            confirm=True,
            command_context=FakeCommandContext(),
        )

        self.assertEqual(enabled.rc, 0)
        self.assertEqual(enabled.values["TC_VFS_AIO_FORK_ENABLED"], "true")
        self.assertEqual(disabled.rc, 0)
        self.assertEqual(disabled.values["TC_VFS_AIO_FORK_ENABLED"], "false")

    def test_configure_boolean_override_pairs_can_enable_and_disable_saved_values(self) -> None:
        cases = (
            (
                "internal_share_use_disk_root",
                "--internal-share-use-disk-root",
                "--no-internal-share-use-disk-root",
                "TC_INTERNAL_SHARE_USE_DISK_ROOT",
            ),
            (
                "smb_browse_compatibility",
                "--smb-browse-compatibility",
                "--no-smb-browse-compatibility",
                "TC_SMB_BROWSE_COMPATIBILITY",
            ),
            ("fruit_metadata_netatalk", "--netatalk", "--no-netatalk", "TC_FRUIT_METADATA_NETATALK"),
            ("debug_logging", "--debug-logging", "--no-debug-logging", "TC_DEBUG_LOGGING"),
        )
        for name, enable_flag, disable_flag, config_key in cases:
            with self.subTest(name=name, value=True):
                enabled = self.run_configure_cli(
                    [enable_flag],
                    existing_values=self.make_valid_env(**{config_key: "false"}),
                    prompt_side_effect=self.configure_prompt_defaults(),
                    probe_state=self.make_probe_state(self.make_probe_result_unreachable()),
                    confirm=True,
                    command_context=FakeCommandContext(),
                )
                self.assertEqual(enabled.rc, 0)
                self.assertEqual(enabled.values[config_key], "true")
            with self.subTest(name=name, value=False):
                disabled = self.run_configure_cli(
                    [disable_flag],
                    existing_values=self.make_valid_env(**{config_key: "true"}),
                    prompt_side_effect=self.configure_prompt_defaults(),
                    probe_state=self.make_probe_state(self.make_probe_result_unreachable()),
                    confirm=True,
                    command_context=FakeCommandContext(),
                )
                self.assertEqual(disabled.rc, 0)
                self.assertEqual(disabled.values[config_key], "false")

    def test_configure_canonical_force_disable_smb_security_arg_is_supported(self) -> None:
        result = self.run_configure_cli(
            ["--force-disable-smb-signing-and-encryption"],
            prompt_side_effect=self.configure_prompt_defaults(),
            probe_state=self.make_probe_state(self.make_probe_result_unreachable()),
            confirm=True,
            command_context=FakeCommandContext(),
        )

        self.assertEqual(result.rc, 0)
        self.assertEqual(result.values["TC_FORCE_DISABLE_SMB_SIGNING_AND_ENCRYPTION"], "true")

    def test_configure_hidden_ata_args_write_drive_settings(self) -> None:
        result = self.run_configure_cli(
            ["--ata-idle-seconds", "0", "--ata-standby", "0"],
            prompt_side_effect=self.configure_prompt_defaults(),
            probe_state=self.make_probe_state(self.make_probe_result_unreachable()),
            confirm=True,
            command_context=FakeCommandContext(),
        )
        self.assertEqual(result.rc, 0)
        self.assertEqual(result.values["TC_ATA_IDLE_SECONDS"], "0")
        self.assertEqual(result.values["TC_ATA_STANDBY"], "0")

    def test_configure_no_input_uses_explicit_host_and_password_env_without_prompts(self) -> None:
        with mock.patch.dict(os.environ, {"TCAPSULE_TEST_PASSWORD": "pw"}):
            result = self.run_configure_cli(
                [
                    "--no-input",
                    "--host",
                    "root@10.0.0.2",
                    "--password-env",
                    "TCAPSULE_TEST_PASSWORD",
                ],
                probe_state=self.make_probe_state(self.make_probe_result_netbsd6()),
                extra_patches={
                    "timecapsulesmb.cli.configure.prompt": mock.Mock(side_effect=AssertionError("configure --no-input should not prompt")),
                    "builtins.input": mock.Mock(side_effect=AssertionError("configure --no-input should not call input")),
                    "timecapsulesmb.cli.runtime.getpass.getpass": mock.Mock(side_effect=AssertionError("configure --no-input should not call getpass")),
                },
            )

        self.assertEqual(result.rc, 0)
        self.assertEqual(result.values["TC_HOST"], "root@10.0.0.2")
        self.assertEqual(result.values["TC_PASSWORD"], "pw")
        result.mocks.discover_snapshot_detailed.assert_not_called()

    def test_configure_no_input_requires_password_before_probe_or_write(self) -> None:
        result = self.run_configure_cli(
            ["--no-input", "--host", "root@10.0.0.2"],
            probe_state=self.make_probe_state(self.make_probe_result_netbsd6()),
            extra_patches={
                "timecapsulesmb.cli.configure.prompt": mock.Mock(side_effect=AssertionError("configure --no-input should not prompt")),
                "timecapsulesmb.cli.runtime.getpass.getpass": mock.Mock(side_effect=AssertionError("configure --no-input should not call getpass")),
            },
        )

        self.assertEqual(result.rc, 1)
        self.assertIn("configure --no-input requires a device password", result.text)
        result.mocks.probe_connection_state.assert_not_called()
        result.mocks.write_env_file.assert_not_called()

    def test_configure_no_input_requires_explicit_ssh_enable_when_ssh_is_closed(self) -> None:
        with mock.patch.dict(os.environ, {"TCAPSULE_TEST_PASSWORD": "pw"}):
            result = self.run_configure_cli(
                ["--no-input", "--host", "root@10.0.0.2", "--password-env", "TCAPSULE_TEST_PASSWORD"],
                probe_state=self.make_probe_state(self.make_probe_result_unreachable()),
                extra_patches={
                    "timecapsulesmb.cli.configure.prompt": mock.Mock(side_effect=AssertionError("configure --no-input should not prompt")),
                },
            )

        self.assertEqual(result.rc, 1)
        self.assertIn("use --enable-ssh --yes to enable SSH via ACP", result.text)
        self._configure_acp_probe_mock.assert_not_called()
        result.mocks.write_env_file.assert_not_called()

    def test_configure_no_input_json_outputs_machine_readable_summary(self) -> None:
        password = "super-secret-configure-password"
        with mock.patch.dict(os.environ, {"TCAPSULE_TEST_PASSWORD": password}):
            result = self.run_configure_cli(
                [
                    "--no-input",
                    "--json",
                    "--host",
                    "root@10.0.0.2",
                    "--password-env",
                    "TCAPSULE_TEST_PASSWORD",
                ],
                probe_state=self.make_probe_state(self.make_probe_result_netbsd6()),
            )

        payload = json.loads(result.text)
        self.assertEqual(result.rc, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["host"], "root@10.0.0.2")
        self.assertEqual(payload["device_syap"], "119")
        self.assertNotIn("TC_PASSWORD", payload)
        self.assertNotIn(password, result.text)

    def test_configure_no_input_json_failure_does_not_print_password(self) -> None:
        password = "super-secret-configure-password"
        with mock.patch.dict(os.environ, {"TCAPSULE_TEST_PASSWORD": password}):
            result = self.run_configure_cli(
                [
                    "--no-input",
                    "--json",
                    "--host",
                    "root@10.0.0.2",
                    "--password-env",
                    "TCAPSULE_TEST_PASSWORD",
                ],
                probe_state=self.make_probe_state(self.make_probe_result_auth_failed()),
            )

        payload = json.loads(result.text)
        self.assertEqual(result.rc, 1)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["code"], "auth_failed")
        self.assertEqual(payload["error"], "The AirPort admin password did not work.")
        telemetry_error = self.configure_finished_error()
        self.assertTrue(telemetry_error.startswith("The AirPort admin password did not work."))
        self.assertIn("configure_error_code=auth_failed", telemetry_error)
        self.assertIn("configure_error_debug=SSH authentication failed.", telemetry_error)
        self.assertNotIn(password, result.text)
        self.assertNotIn(password, telemetry_error)

    def test_configure_no_input_json_reports_acp_auth_failure_with_shared_contract(self) -> None:
        password = "super-secret-configure-password"
        acp_error = "ACP command failed with error_code -0x10 (likely wrong AirPort admin password)"
        self._configure_acp_probe_mock.side_effect = ACPAuthError(acp_error)

        with mock.patch.dict(os.environ, {"TCAPSULE_TEST_PASSWORD": password}):
            result = self.run_configure_cli(
                [
                    "--no-input",
                    "--json",
                    "--host",
                    "root@10.0.0.2",
                    "--password-env",
                    "TCAPSULE_TEST_PASSWORD",
                    "--enable-ssh",
                    "--yes",
                ],
                probe_state=self.make_probe_state(self.make_probe_result_unreachable()),
            )

        payload = json.loads(result.text)
        self.assertEqual(result.rc, 1)
        self.assertEqual(payload["code"], "auth_failed")
        self.assertEqual(payload["error"], "The AirPort admin password did not work.")
        telemetry_error = self.configure_finished_error()
        self.assertTrue(telemetry_error.startswith("The AirPort admin password did not work."))
        self.assertIn("configure_error_code=auth_failed", telemetry_error)
        self.assertIn(f"configure_error_debug={acp_error}", telemetry_error)
        self.assertNotIn(password, result.text)
        self.assertNotIn(password, telemetry_error)
        result.mocks.write_env_file.assert_not_called()

    def test_configure_preserves_existing_ata_settings(self) -> None:
        result = self.run_configure_cli(
            [],
            existing_values={
                "TC_ATA_IDLE_SECONDS": "42",
                "TC_ATA_STANDBY": "0",
            },
            prompt_side_effect=self.configure_prompt_defaults(),
            probe_state=self.make_probe_state(self.make_probe_result_unreachable()),
            confirm=True,
            command_context=FakeCommandContext(),
        )
        self.assertEqual(result.rc, 0)
        self.assertEqual(result.values["TC_ATA_IDLE_SECONDS"], "42")
        self.assertEqual(result.values["TC_ATA_STANDBY"], "0")

    def test_configure_saves_default_ata_settings_when_existing_env_lacks_them(self) -> None:
        result = self.run_configure_cli(
            [],
            existing_values={
                "TC_HOST": "root@10.0.0.2",
                "TC_PASSWORD": "pw",
            },
            prompt_side_effect=self.configure_prompt_defaults(),
            probe_state=self.make_probe_state(self.make_probe_result_unreachable()),
            confirm=True,
            command_context=FakeCommandContext(),
        )
        rendered = render_env_text(result.values)

        self.assertEqual(result.rc, 0)
        self.assertEqual(result.values["TC_ATA_IDLE_SECONDS"], DEFAULTS["TC_ATA_IDLE_SECONDS"])
        self.assertEqual(result.values["TC_ATA_STANDBY"], DEFAULTS["TC_ATA_STANDBY"])
        self.assertIn("TC_ATA_IDLE_SECONDS=300", rendered)
        self.assertIn("TC_ATA_STANDBY=''", rendered)

    def test_configure_airport_extreme_keeps_hidden_internal_share_root_default(self) -> None:
        def fake_prompt(label, default, _secret):
            if label == "Device SSH target":
                return "root@10.0.0.2"
            if label == "Device root password":
                return "rootpw"
            if label == "Airport Utility syAP code":
                return "120"
            if label == "mDNS device model hint":
                raise AssertionError("mDNS device model should be derived from the final syAP")
            return default

        result = self.run_configure_cli(
            prompt_side_effect=fake_prompt,
            probe_state=self.make_probe_state(self.make_probe_result_netbsd6_no_identity()),
        )

        self.assertEqual(result.rc, 0)
        self.assertNotIn("TC_AIRPORT_SYAP", result.values)
        self.assertNotIn("TC_MDNS_DEVICE_MODEL", result.values)
        self.assertEqual(result.values["TC_INTERNAL_SHARE_USE_DISK_ROOT"], "false")
        self.assertEqual(result.values["TC_ANY_PROTOCOL"], "false")

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
        expected_prefix = "Failed to load zeroconf. Install the Python package zeroconf."
        expected_error = "ModuleNotFoundError: No module named 'zeroconf'"
        self.assertIn(expected_prefix, text)
        self.assertIn(expected_error, text)
        self.assertNotIn("This writes a local .env configuration file", text)
        self.assertEqual(self.configure_finished_result(), "failure")
        error = self.configure_finished_error()
        self.assertIn(expected_prefix, error)
        self.assertIn(expected_error, error)
        self.assertIn("stage=dependency_check", error)

    def test_configure_dependency_preflight_reports_first_missing_module_name(self) -> None:
        output = io.StringIO()
        missing_zeroconf = ("zeroconf", ModuleNotFoundError("No module named 'zeroconf'"))
        with mock.patch("timecapsulesmb.cli.configure.ensure_install_id"):
            with mock.patch("timecapsulesmb.cli.configure.parse_env_file", return_value={}):
                with mock.patch("timecapsulesmb.cli.configure.missing_required_python_module", return_value=missing_zeroconf):
                    with redirect_stdout(output):
                        rc = configure.main([])

        self.assertEqual(rc, 1)
        expected_prefix = "Failed to load zeroconf. Install the Python package zeroconf."
        expected_error = "ModuleNotFoundError: No module named 'zeroconf'"
        self.assertIn(expected_prefix, output.getvalue())
        self.assertIn(expected_error, output.getvalue())
        self.assertIn(expected_prefix, self.configure_finished_error())
        self.assertIn(expected_error, self.configure_finished_error())

    def test_configure_does_not_persist_configure_id_before_final_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text("TC_HOST=root@10.0.0.2\n")
            with mock.patch("timecapsulesmb.cli.configure.parse_env_file", return_value={"TC_HOST": "root@10.0.0.2"}):
                empty_snapshot = BonjourDiscoverySnapshot(instances=[], resolved=[])
                empty_diagnostics = BonjourQueryDiagnostics(
                    provider="zeroconf",
                    service_types=["_airport._tcp.local."],
                    timeout_sec=6.0,
                    elapsed_sec=0.0,
                    instance_count=0,
                    resolved_count=0,
                )
                with mock.patch("timecapsulesmb.cli.configure.discover_snapshot_detailed", return_value=(empty_snapshot, empty_diagnostics)):
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
        self.assertEqual(values["TC_HOST"], "root@10.0.0.2")
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
                    "timecapsulesmb.cli.configure.discover_snapshot_detailed",
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
                return "root@10.0.0.2"
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
        # The password "pw" as a value, not inside a field name such as sypw_check.
        self.assertNotRegex(error, r"\bpw\b")

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

    def airport_express_record(self) -> BonjourResolvedService:
        return BonjourResolvedService(
            name="Living Room Express",
            hostname="Living-Room-Express.local",
            ipv4=["192.168.1.40"],
            services={"_airport._tcp.local."},
            properties={"syAP": "115"},
        )

    def capsule_record(self) -> BonjourResolvedService:
        return BonjourResolvedService(
            name="Office Capsule",
            hostname="Office-Capsule.local",
            ipv4=["192.168.1.50"],
            services={"_airport._tcp.local."},
            properties={"syAP": "119"},
        )

    def assert_configure_stopped_for_closed_input(self, result) -> None:
        self.assertEqual(result.exception.code, configure.CONFIGURE_NONINTERACTIVE_MESSAGE)
        result.mocks.write_env_file.assert_not_called()
        result.mocks.probe_connection_state.assert_not_called()
        self.assertEqual(self.configure_finished_result(), "failure")
        error = self.configure_finished_error()
        self.assertTrue(error.startswith(configure.CONFIGURE_NONINTERACTIVE_MESSAGE + "\nCaused by: EOF when reading a line"))
        self.assertIn("stage=prompt_host_password", error)

    def test_configure_without_input_names_the_scripted_options_instead_of_a_traceback(self) -> None:
        # v3.3.1-1 telemetry: configure run by an agent whose shell has no stdin.
        result = self.run_configure_cli(
            discovered_records=[self.capsule_record()],
            input_side_effect=EOFError("EOF when reading a line"),
            probe_state=self.make_probe_state(self.make_probe_result_netbsd6()),
            raises=SystemExit,
        )

        self.assert_configure_stopped_for_closed_input(result)
        self.assertIn("Found devices:", result.text)
        self.assertNotIn("mDNS discovery failed", result.text)

    def test_configure_without_input_at_the_password_prompt_stops_before_probing(self) -> None:
        # The SSH target is answered (Enter accepts --host), then the password
        # prompt reads end of input: Ctrl-D, or a pipe that ran dry.
        getpass_mock = mock.Mock(side_effect=EOFError("EOF when reading a line"))
        result = self.run_configure_cli(
            ["--host", "root@10.0.1.20"],
            input_side_effect=[""],
            probe_state=self.make_probe_state(self.make_probe_result_netbsd6()),
            extra_patches={"timecapsulesmb.cli.runtime.getpass.getpass": getpass_mock},
            raises=SystemExit,
        )

        self.assert_configure_stopped_for_closed_input(result)
        self.assertEqual(result.mocks.input.call_count, 1)
        getpass_mock.assert_called_once()

    def test_configure_lists_unsupported_model_and_asks_again_when_it_is_chosen(self) -> None:
        result = self.run_configure_cli(
            discovered_records=[self.airport_express_record(), self.capsule_record()],
            input_side_effect=["1", "2"],
            prompt_side_effect=self.configure_prompt_defaults(host="root@192.168.1.50"),
            probe_state=self.make_probe_state(self.make_probe_result_netbsd6()),
        )

        self.assertEqual(result.rc, 0)
        listing = [line for line in result.text.splitlines() if line.startswith("  1. ") or line.startswith("  2. ")]
        self.assertIn("not supported (not a Time Capsule or AirPort Extreme)", listing[0])
        self.assertNotIn("not supported", listing[1])
        self.assertIn("syAP 115", result.text)
        self.assertIn("Choose another device, or q to skip discovery.", result.text)
        self.assertIn("Selected: Office Capsule", result.text)
        self.assertEqual(result.mocks.input.call_count, 2)
        self.assertEqual(result.values["TC_HOST"], "root@192.168.1.50")
        probed_hosts = [call.args[0].host for call in result.mocks.probe_connection_state.call_args_list]
        self.assertEqual(probed_hosts, ["root@192.168.1.50"])
        payload = self.telemetry_payload("configure_finished")
        self.assertEqual(payload["result"], "success")
        self.assertEqual(payload["discovery_unsupported_syaps"], ["115"])

    def test_configure_unsupported_model_choice_can_fall_back_to_manual_entry(self) -> None:
        result = self.run_configure_cli(
            discovered_records=[self.airport_express_record()],
            input_side_effect=["1", "q"],
            prompt_side_effect=self.configure_prompt_defaults(host="root@10.0.0.2"),
            probe_state=self.make_probe_state(self.make_probe_result_netbsd6()),
        )

        self.assertEqual(result.rc, 0)
        self.assertIn("syAP 115", result.text)
        self.assertNotIn("Selected: Living Room Express", result.text)
        self.assertEqual(result.values["TC_HOST"], "root@10.0.0.2")
        self._configure_acp_probe_mock.assert_not_called()
        self.assertEqual(self.telemetry_payload("configure_finished")["discovery_unsupported_syaps"], ["115"])

    def test_configure_selected_supported_or_unusable_syap_record_reaches_prompts(self) -> None:
        for syap in ("116", "bad"):
            with self.subTest(syap=syap):
                record = BonjourResolvedService(
                    name="Office Capsule",
                    hostname="Office-Capsule.local",
                    ipv4=["192.168.1.40"],
                    services={"_airport._tcp.local."},
                    properties={"syAP": syap},
                )
                result = self.run_configure_cli(
                    discovered_records=[record],
                    input_side_effect=["1"],
                    prompt_side_effect=self.configure_prompt_defaults(host="root@192.168.1.40"),
                    probe_state=self.make_probe_state(self.make_probe_result_netbsd6()),
                )
                self.assertEqual(result.rc, 0)
                result.mocks.prompt.assert_called()
                result.mocks.probe_connection_state.assert_called()
                self.assertNotIn("discovery_unsupported_syaps", self.telemetry_payload("configure_finished"))

    def test_configure_rejects_airport_express_found_by_ssh_probe(self) -> None:
        # A record without syAP (or a typed IP) reaches SSH; the CPU check stops it.
        record = BonjourResolvedService(
            name="Living Room Express",
            hostname="Living-Room-Express.local",
            ipv4=["192.168.1.40"],
            services={"_smb._tcp.local."},
            properties={},
        )
        probe_result = ProbeResult(
            ssh_status=SshAccessStatus.OPEN_AUTHENTICATED,
            error=None,
            os_name="NetBSD",
            os_release="4.0_STABLE",
            arch="ar7240",
            elf_endianness="big",
        )

        result = self.run_configure_cli(
            ensure_install_id=True,
            discovered_records=[record],
            input_side_effect=["1"],
            prompt_side_effect=self.configure_prompt_defaults(host="root@192.168.1.40"),
            probe_state=self.make_probe_state(probe_result),
            raises=SystemExit,
        )

        self.assertIn("ar7240 processor", str(result.exception))
        result.mocks.write_env_file.assert_not_called()
        error = self.configure_finished_error()
        self.assertIn("stage=ssh_probe", error)
        self.assertIn("probe_reason_code=unsupported_arch", error)

    def test_configure_telemetry_records_elf_endianness_probe_detail(self) -> None:
        probe_result = ProbeResult(
            ssh_status=SshAccessStatus.OPEN_AUTHENTICATED,
            error=None,
            os_name="NetBSD",
            os_release="6.0",
            arch="evbarm",
            elf_endianness="unknown",
            elf_endianness_detail="sed=unknown,rc=0,stdout=sed_b5=; raw=unknown,rc=0,stdout=raw_compare=nomatch",
        )

        self.run_configure_cli(
            ensure_install_id=True,
            prompt_side_effect=self.configure_prompt_defaults(),
            probe_state=self.make_probe_state(probe_result),
            raises=SystemExit,
        )

        error = self.configure_finished_error()
        self.assertIn("probe_reason_code=unsupported_netbsd6_endianness", error)
        self.assertIn("probe_elf_endianness=unknown", error)
        self.assertIn("probe_elf_endianness_detail=sed=unknown", error)
        self.assertIn("raw_compare=nomatch", error)

    def test_configure_telemetry_does_not_record_runtime_interface_selection(self) -> None:
        self.run_configure_cli(
            ensure_install_id=True,
            prompt_side_effect=self.configure_prompt_defaults(host="root@10.0.1.1"),
            probe_state=self.make_probe_state(self.make_probe_result_netbsd6()),
            write_side_effect=RuntimeError("cannot write env"),
            raises=RuntimeError,
        )

        error = self.configure_finished_error()
        self.assertIn("RuntimeError: cannot write env", error)
        self.assertNotIn("interface_candidates=[", error)
        self.assertNotIn("selected_net_iface=", error)

    def test_configure_uses_discovered_host_when_available(self) -> None:
        record = BonjourResolvedService(
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
                return "root@10.0.0.2"
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

    def test_configure_saves_fe80_when_the_discovered_lan_address_is_off_this_network(self) -> None:
        # Discussion #368 from the CLI: accepting the discovered default runs
        # the same selection as the app, which reaches the device over fe80.
        record = BonjourResolvedService(
            name="AirPort Time Capsule",
            hostname="AirPort-Time-Capsule.local",
            service_type="_airport._tcp.local.",
            ipv4=["192.168.1.83", "169.254.205.45"],
            ipv6=["FE80:0000:0000:0000:66A5:C3FF:FE60:FC22%en0"],
        )
        self._record_acp_probe.side_effect = lambda address, port: None if address.lower().startswith("fe80") else "timed out"
        prompt_values = iter(["rootpw", "Data", "admin", "TimeCapsule", "samba4", "Time Capsule Samba 4", "timecapsulesamba4"])
        seen_defaults: dict[str, str] = {}

        def fake_prompt(label, default, _secret):
            seen_defaults[label] = default
            if label in {"Device SSH target", "Network interface on the device"}:
                return default
            if label in {"Airport Utility syAP code", "mDNS device model hint"}:
                raise AssertionError(f"{label} should be auto-filled")
            return next(prompt_values)

        result = self.run_configure_cli(
            discovered_records=[record],
            input_side_effect=["1"],
            prompt_side_effect=fake_prompt,
            probe_state=self.make_probe_state(self.make_probe_result_netbsd6()),
        )

        self.assertEqual(result.rc, 0)
        # The prompt offers the advertised LAN address; the saved target is
        # the address that answered.
        self.assertEqual(seen_defaults["Device SSH target"], "root@192.168.1.83")
        self.assertEqual(result.mocks.probe_connection_state.call_args.args[0].host, "root@fe80::66a5:c3ff:fe60:fc22%en0")
        self.assertEqual(result.values["TC_HOST"], "root@fe80::66a5:c3ff:fe60:fc22%en0")

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
        self.assertNotIn("TC_MDNS_DEVICE_MODEL", result.values)

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

        result = self.run_configure_cli(
            prompt_side_effect=fake_prompt,
            probe_state=self.make_probe_state(self.make_probe_result_netbsd6()),
        )
        self.assertEqual(result.rc, 0)
        self.assertEqual(result.values["TC_HOST"], "root@10.0.0.2")
        self.assertIn("Device SSH target host must not be a link-local address", result.text)

    def test_configure_accepts_hostname_that_also_resolves_link_local(self) -> None:
        prompt_values = iter([
            "root@capsule.local",
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
        addrinfo = [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("10.0.0.2", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("169.254.44.9", 0)),
        ]

        result = self.run_configure_cli(
            prompt_side_effect=fake_prompt,
            probe_state=self.make_probe_state(self.make_probe_result_netbsd6()),
            extra_patches={"timecapsulesmb.core.net.socket.getaddrinfo": mock.Mock(return_value=addrinfo)},
        )
        self.assertEqual(result.rc, 0)
        self.assertEqual(result.values["TC_HOST"], "root@capsule.local")
        self.assertNotIn("link-local", result.text)

    def test_configure_skipped_mdns_netbsd6_little_autofills_syap_and_model(self) -> None:
        record = BonjourResolvedService(
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
        self.assertNotIn("TC_MDNS_DEVICE_MODEL", result.values)
        text = result.text
        self.assertIn("Discovery skipped.", text)
        self.assertIn("Using probed TC_AIRPORT_SYAP: 119", text)
        self.assertNotIn("Using probed TC_MDNS_DEVICE_MODEL", text)

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
        record = BonjourResolvedService(
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
        record = BonjourResolvedService(
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
        record = BonjourResolvedService(
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
        record = BonjourResolvedService(
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
        self.assertNotIn("TC_AIRPORT_SYAP", result.values)
        self.assertNotIn("TC_MDNS_DEVICE_MODEL", result.values)
        text = result.text
        self.assertNotIn("Device                           Model identifier    syAP", text)
        self.assertNotIn("Airport Utility syAP code", text)

    def test_configure_probed_netbsd4be_shows_syap_table_and_restricts_candidates(self) -> None:
        syap_defaults: list[str] = []
        record = BonjourResolvedService(
            name="AirPort Time Capsule",
            hostname="AirPort-Time-Capsule.local",
            ipv4=["192.168.1.72"],
            services={"_airport._tcp.local."},
            properties={"syAP": "113"},
        )
        prompt_values = iter([
            "root@10.0.0.2",
            "rootpw",
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
        self.assertEqual(syap_defaults, [])
        self.assertNotIn("TC_AIRPORT_SYAP", result.values)
        self.assertNotIn("TC_MDNS_DEVICE_MODEL", result.values)
        text = result.text
        self.assertNotIn("Device                           Model identifier    syAP", text)
        self.assertNotIn("Airport Utility syAP code", text)

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
        self.assertNotIn("TC_MDNS_DEVICE_MODEL", result.values)
        self.assertIn("Using probed TC_AIRPORT_SYAP: 106", result.text)
        self.assertNotIn("Using probed TC_MDNS_DEVICE_MODEL", result.text)

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
        self.assertNotIn("TC_MDNS_DEVICE_MODEL", result.values)
        self.assertIn("Using probed TC_AIRPORT_SYAP: 113", result.text)
        self.assertNotIn("Using probed TC_MDNS_DEVICE_MODEL", result.text)

    def test_configure_uses_discovered_airport_syap_without_prompting(self) -> None:
        record = BonjourResolvedService(
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
                return "root@10.0.0.2"
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
        self.assertNotIn("TC_MDNS_DEVICE_MODEL", result.values)
        self.assertIn("Using probed TC_AIRPORT_SYAP: 119", result.text)

    def test_configure_discovered_syap_beats_invalid_existing_syap(self) -> None:
        seen_labels: list[str] = []
        record = BonjourResolvedService(
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
        self.assertNotIn("TC_MDNS_DEVICE_MODEL", result.values)
        self.assertNotIn("Airport Utility syAP code", seen_labels)
        self.assertNotIn("mDNS device model hint", seen_labels)

    def test_configure_discovered_missing_syap_uses_probed_syap_after_acp(self) -> None:
        seen_defaults = {}
        record = BonjourResolvedService(
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
        self.assertNotIn("TC_MDNS_DEVICE_MODEL", result.values)
        self.assertNotIn("mDNS device model hint", seen_defaults)
        self.assertIn("Using probed TC_AIRPORT_SYAP: 119", result.text)

    def test_configure_selected_smb_record_without_airport_syap_uses_probe_before_saved_syap(self) -> None:
        seen_labels: list[str] = []
        record = BonjourResolvedService(
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
        self.assertNotIn("TC_MDNS_DEVICE_MODEL", result.values)
        self.assertNotIn("Airport Utility syAP code", seen_labels)
        self.assertIn("Using probed TC_AIRPORT_SYAP: 119", result.text)

    def test_configure_ignores_legacy_saved_syap_when_identity_is_unobserved(self) -> None:
        syap_answers = iter(["113", "120"])

        def fake_prompt(label, default, _secret):
            if label == "Device SSH target":
                return "root@10.0.0.2"
            if label == "Device root password":
                return "rootpw"
            if label == "Airport Utility syAP code":
                return next(syap_answers)
            if label == "mDNS device model hint":
                raise AssertionError("mDNS device model should be derived from the final syAP")
            return default

        result = self.run_configure_cli(
            existing_values={"TC_AIRPORT_SYAP": "113"},
            prompt_side_effect=fake_prompt,
            probe_state=self.make_probe_state(self.make_probe_result_netbsd6_no_identity()),
        )

        self.assertEqual(result.rc, 0)
        self.assertNotIn("TC_AIRPORT_SYAP", result.values)
        self.assertNotIn("TC_MDNS_DEVICE_MODEL", result.values)
        text = result.text
        self.assertNotIn("Found saved value: 113", text)
        self.assertNotIn("Airport Utility syAP code", text)

    def test_configure_discovered_invalid_syap_uses_probed_syap_after_acp(self) -> None:
        # A syAP that is not a model code is ignored and the probed one wins. A
        # well-formed code outside the model table instead names another AirPort
        # model and stops configure (see the unsupported-syAP tests below).
        seen_defaults = {}
        record = BonjourResolvedService(
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
        self.assertNotIn("TC_MDNS_DEVICE_MODEL", result.values)
        self.assertNotIn("mDNS device model hint", seen_defaults)
        self.assertIn("Using probed TC_AIRPORT_SYAP: 119", result.text)

    def test_configure_discovered_invalid_syap_reprompts_until_valid_when_existing_syap_invalid(self) -> None:
        syap_defaults: list[str] = []
        syap_attempts = iter(["999", "113"])
        record = BonjourResolvedService(
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
        self.assertEqual(syap_defaults, [])
        self.assertNotIn("TC_AIRPORT_SYAP", result.values)
        self.assertNotIn("TC_MDNS_DEVICE_MODEL", result.values)
        self.assertNotIn("The configured syAP is invalid.", result.text)

    def test_configure_can_skip_single_discovered_device(self) -> None:
        record = BonjourResolvedService(
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
                return "root@10.0.0.2"
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
        self.assertEqual(result.values["TC_HOST"], "root@10.0.0.2")
        self.assertEqual(result.values["TC_AIRPORT_SYAP"], "119")
        self.assertIn("Found devices:", result.text)
        self.assertIn(f"Discovery skipped. Falling back to {DEFAULTS['TC_HOST']}.", result.text)

    def test_configure_does_not_default_to_discovered_link_local_ipv4(self) -> None:
        seen_defaults = {}
        record = BonjourResolvedService(
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

        result = self.run_configure_cli(
            discovered_records=[record],
            input_side_effect=["1"],
            prompt_side_effect=fake_prompt,
            probe_state=self.make_probe_state(self.make_probe_result_netbsd6()),
        )
        self.assertEqual(result.rc, 0)
        self.assertEqual(seen_defaults["Device SSH target"], DEFAULTS["TC_HOST"])
        self.assertEqual(result.values["TC_HOST"], "root@10.0.0.2")
        self.assertIn("Selected device only advertised link-local addresses", result.text)
        self.assertNotIn("host: 169.254.44.9", result.text)

    def test_configure_does_not_default_to_discovered_link_local_ipv4_or_ipv6(self) -> None:
        seen_defaults = {}
        record = BonjourResolvedService(
            name="Time Capsule Samba 4",
            hostname="timecapsulesamba4.local",
            ipv4=["169.254.44.9"],
            ipv6=["fe80::82ea:96ff:fee6:c7e5"],
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

        result = self.run_configure_cli(
            discovered_records=[record],
            input_side_effect=["1"],
            prompt_side_effect=fake_prompt,
            probe_state=self.make_probe_state(self.make_probe_result_netbsd6()),
        )

        self.assertEqual(result.rc, 0)
        self.assertEqual(seen_defaults["Device SSH target"], DEFAULTS["TC_HOST"])
        self.assertEqual(result.values["TC_HOST"], "root@10.0.0.2")
        self.assertIn("Selected device only advertised link-local addresses", result.text)
        self.assertIn("IPv6: fe80::82ea:96ff:fee6:c7e5", result.text)

    def link_local_only_cli_run(self, *, fe80_answers: bool, typed_host: str | None):
        seen_defaults: dict[str, str] = {}
        record = BonjourResolvedService(
            name="Time Capsule Samba 4",
            hostname="timecapsulesamba4.local",
            ipv4=["169.254.44.9"],
            ipv6=["fe80::82ea:96ff:fee6:c7e5%en0"],
            services={"_airport._tcp.local."},
            properties={"syAP": "119"},
        )
        self._record_acp_probe.return_value = None if fe80_answers else "timed out"
        prompt_values = iter(["rootpw", "Data", "admin", "TimeCapsule", "samba4", "Time Capsule Samba 4", "timecapsulesamba4"])

        def fake_prompt(label, default, _secret):
            seen_defaults[label] = default
            if label == "Network interface on the device":
                return default
            if label == "Device SSH target":
                # Asked again means the first answer was refused.
                if label in asked:
                    raise AssertionError(f"reprompted for the SSH target after {default!r}")
                asked.add(label)
                return typed_host or default
            if label in {"Airport Utility syAP code", "mDNS device model hint"}:
                raise AssertionError(f"{label} should be auto-filled")
            return next(prompt_values)

        asked: set[str] = set()
        result = self.run_configure_cli(
            discovered_records=[record],
            input_side_effect=["1"],
            prompt_side_effect=fake_prompt,
            probe_state=self.make_probe_state(self.make_probe_result_netbsd6()),
        )
        return result, seen_defaults

    def test_configure_offers_the_fe80_address_of_a_link_local_only_record_that_answers(self) -> None:
        result, seen_defaults = self.link_local_only_cli_run(fe80_answers=True, typed_host=None)

        self.assertEqual(result.rc, 0)
        self.assertEqual(seen_defaults["Device SSH target"], "root@fe80::82ea:96ff:fee6:c7e5%en0")
        self.assertEqual(result.mocks.probe_connection_state.call_args.args[0].host, "root@fe80::82ea:96ff:fee6:c7e5%en0")
        self.assertEqual(result.values["TC_HOST"], "root@fe80::82ea:96ff:fee6:c7e5%en0")
        self.assertNotIn("Enter the device's LAN IP", result.text)
        # 169.254 is never tried.
        self.assertNotIn(mock.call("169.254.44.9", 5009), self._record_acp_probe.call_args_list)

    def test_configure_asks_for_a_lan_address_when_a_link_local_only_record_does_not_answer(self) -> None:
        result, seen_defaults = self.link_local_only_cli_run(fe80_answers=False, typed_host="root@10.0.0.2")

        self.assertEqual(result.rc, 0)
        self.assertEqual(seen_defaults["Device SSH target"], DEFAULTS["TC_HOST"])
        self.assertIn("only advertised link-local addresses, and none answered from this computer", result.text)
        self.assertEqual(result.values["TC_HOST"], "root@10.0.0.2")

    def test_configure_defaults_to_ipv6_when_discovery_has_no_routable_ipv4(self) -> None:
        seen_defaults = {}
        ipv6 = "fdbb:5737:6e53:9bf7:82ea:96ff:fee6:5868"
        record = BonjourResolvedService(
            name="Time Capsule Samba 4",
            hostname="timecapsulesamba4.local",
            ipv4=["169.254.44.9"],
            ipv6=[ipv6],
            services={"_airport._tcp.local."},
            properties={"syAP": "119"},
        )

        def fake_prompt(label, default, _secret):
            seen_defaults[label] = default
            if label == "Device root password":
                return "rootpw"
            return default

        result = self.run_configure_cli(
            discovered_records=[record],
            input_side_effect=["1"],
            prompt_side_effect=fake_prompt,
            probe_state=self.make_probe_state(self.make_probe_result_netbsd6()),
        )

        self.assertEqual(result.rc, 0)
        self.assertEqual(seen_defaults["Device SSH target"], f"root@{ipv6}")
        self.assertEqual(result.values["TC_HOST"], f"root@{ipv6}")
        self.assertIn(f"host: {ipv6}", result.text)
        self.assertIn(f"IPv6: {ipv6}", result.text)
        self.assertNotIn("Selected device only advertised link-local addresses", result.text)

    def test_configure_ctrl_c_during_discovery_selection_cancels(self) -> None:
        record = BonjourResolvedService(
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
        self.assertEqual(syap_defaults, [])
        self.assertNotIn("TC_AIRPORT_SYAP", result.values)
        self.assertNotIn("TC_MDNS_DEVICE_MODEL", result.values)
        self.assertNotIn("The configured syAP is invalid.", result.text)

    def test_configure_skipped_discovery_ignores_legacy_existing_syap(self) -> None:
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
        self.assertNotIn("TC_AIRPORT_SYAP", result.values)
        self.assertNotIn("Using TC_AIRPORT_SYAP from .env: 116", result.text)

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
        self.assertNotIn("TC_MDNS_DEVICE_MODEL", result.values)
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
        self.assertNotIn("TC_MDNS_DEVICE_MODEL", result.values)
        self.assertIn("Using probed TC_AIRPORT_SYAP: 119", result.text)
        self.assertNotIn("Using probed TC_MDNS_DEVICE_MODEL", result.text)

    def test_configure_skipped_discovery_ignores_legacy_syap_model_when_unusable(self) -> None:
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
        )
        self.assertEqual(result.rc, 0)
        self.assertNotIn("TC_AIRPORT_SYAP", result.values)
        self.assertNotIn("mDNS device model hint", seen_defaults)
        self.assertNotIn("TC_MDNS_DEVICE_MODEL", result.values)
        self.assertNotIn("Using TC_AIRPORT_SYAP from .env: 119", result.text)

    def test_configure_ignores_legacy_existing_syap_when_identity_is_undetected(self) -> None:
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
        self.assertNotIn("TC_AIRPORT_SYAP", result.values)
        self.assertNotIn("mDNS device model hint", seen_defaults)
        self.assertNotIn("TC_MDNS_DEVICE_MODEL", result.values)
        self.assertNotIn("Using TC_AIRPORT_SYAP from .env: 116", result.text)
        self.assertNotIn("Using TC_MDNS_DEVICE_MODEL derived from TC_AIRPORT_SYAP", result.text)

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
        self.assertNotIn("TC_AIRPORT_SYAP", result.values)
        self.assertNotIn("mDNS device model hint", seen_defaults)
        self.assertNotIn("TC_MDNS_DEVICE_MODEL", result.values)
        self.assertNotIn("Using TC_MDNS_DEVICE_MODEL derived from TC_AIRPORT_SYAP", result.text)

    def test_configure_skipped_discovery_ignores_legacy_existing_mdns_device_model(self) -> None:
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
        )
        self.assertEqual(result.rc, 0)
        self.assertNotIn("TC_MDNS_DEVICE_MODEL", result.values)
        self.assertNotIn("Using TC_MDNS_DEVICE_MODEL from .env: TimeCapsule", result.text)

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
        )
        self.assertEqual(result.rc, 0)
        self.assertNotIn("mDNS device model hint", seen_defaults)
        self.assertNotIn("TC_MDNS_DEVICE_MODEL", result.values)
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
                "timecapsulesmb.cli.runtime.getpass.getpass": mock.Mock(side_effect=lambda _prompt: next(password_values))
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
                "timecapsulesmb.cli.runtime.getpass.getpass": mock.Mock(side_effect=lambda _prompt: next(password_values))
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

    def test_configure_reprompts_placeholder_ssh_target_before_password(self) -> None:
        password_prompts = 0
        host_values = iter([DEFAULTS["TC_HOST"], "root@10.0.0.2"])

        def fake_prompt(label, default, _secret):
            nonlocal password_prompts
            if label == "Device SSH target":
                return next(host_values)
            if label == "Device root password":
                password_prompts += 1
                return "goodpw"
            if label == "mDNS device model hint":
                return default
            return default

        result = self.run_configure_cli(
            prompt_side_effect=fake_prompt,
            probe_state=self.make_probe_state(self.make_probe_result_netbsd6()),
        )
        self.assertEqual(result.rc, 0)
        self.assertEqual(result.values["TC_HOST"], "root@10.0.0.2")
        self.assertEqual(password_prompts, 1)
        self.assertIn("Device SSH target IP address is invalid", result.text)
        probed_connection = result.mocks.probe_connection_state.call_args.args[0]
        self.assertEqual(probed_connection.host, "root@10.0.0.2")

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

    def test_configure_reprompts_when_ssh_accepts_a_password_the_device_rejects(self) -> None:
        # SSH checks only 8 characters: "pw-secretzz" logs in where the AirPort
        # admin password is "pw-secret". The device's syPW comparison refuses it,
        # the user declines to save it anyway, and the retyped password is saved.
        prompt_values = iter(["root@10.0.0.2", "pw-secretzz", "root@10.0.0.2", "pw-secret"])

        def fake_prompt(label, default, _secret):
            if label == "mDNS device model hint":
                return default
            return next(prompt_values, default)

        compare = mock.Mock(side_effect=[
            acp_reading(False),
            acp_reading(True),
        ])
        with mock.patch("timecapsulesmb.device.probe.read_airport_acp", compare):
            result = self.run_configure_cli(
                prompt_side_effect=fake_prompt,
                probe_state=self.make_probe_state(self.make_probe_result_netbsd6_no_identity()),
                confirm=False,
            )

        self.assertEqual(result.rc, 0)
        self.assertEqual(result.values["TC_PASSWORD"], "pw-secret")
        self.assertEqual([call.args[1] for call in compare.call_args_list], ["pw-secretzz", "pw-secret"])
        self.assertIn("SSH accepted the password, but it is not the AirPort admin password.", result.text)
        self.assertNotIn("The provided AirPort SSH target and password did not work", result.text)
        self.assertIn("Please enter the SSH target and password again", result.text)

    def test_enter_at_save_anyway_does_not_keep_a_password_the_device_rejects(self) -> None:
        # The prompt defaults to No: pressing Enter after "SSH accepted the
        # password, but it is not the AirPort admin password" asks again.
        prompt_values = iter(["root@10.0.0.2", "pw-secretzz", "root@10.0.0.2", "pw-secret"])

        def fake_prompt(label, default, _secret):
            if label == "mDNS device model hint":
                return default
            return next(prompt_values, default)

        compare = mock.Mock(side_effect=[
            acp_reading(False),
            acp_reading(True),
        ])
        with mock.patch("timecapsulesmb.device.probe.read_airport_acp", compare):
            result = self.run_configure_cli(
                prompt_side_effect=fake_prompt,
                probe_state=self.make_probe_state(self.make_probe_result_netbsd6_no_identity()),
                input_side_effect=[""],
            )

        self.assertEqual(result.rc, 0)
        self.assertEqual(result.values["TC_PASSWORD"], "pw-secret")
        self.assertIn("Save this information still? [y/N]", "".join(str(call.args[0]) for call in result.mocks.input.call_args_list))

    def test_configure_save_anyway_prompt_says_ssh_rejected_the_password(self) -> None:
        result = self.run_configure_cli(
            prompt_side_effect=self.configure_prompt_defaults(password="badpw"),
            probe_state=self.make_probe_state(self.make_probe_result_auth_failed()),
            confirm=True,
        )

        self.assertEqual(result.rc, 0)
        self.assertEqual(result.values["TC_PASSWORD"], "badpw")
        self.assertIn("The provided AirPort SSH target and password did not work", result.text)

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
        self.assertIn(
            "SSH did not open after enabling via ACP. Reboot the device, wait 5 minutes, and try configure again.",
            result.text,
        )
        self.assertIn(
            "SSH did not open after enabling via ACP. Reboot the device, wait 5 minutes, and try configure again.",
            self.configure_finished_error(),
        )

    def test_configure_records_the_ssh_enable_reboot_cycle_in_telemetry(self) -> None:
        # The real enable-and-reboot runs against the simulated device; only the
        # ACP write is stubbed. SSH never opens on the new boot.
        self._configure_acp_probe_mock.side_effect = lambda connection, **kwargs: real_enable_ssh_and_reprobe(connection, **kwargs)[1]
        self.device.ssh_up_after_boot = None
        with mock.patch("timecapsulesmb.services.configure.enable_ssh_with_port_preflight", side_effect=lambda host, *_args, **_kwargs: host):
            result = self.run_configure_cli(
                prompt_side_effect=self.configure_prompt_defaults(),
                probe_state=self.make_probe_state(self.make_probe_result_unreachable()),
                confirm=True,
            )

        self.assertEqual(result.rc, 1)
        self.assertIn("SSH did not open after enabling via ACP.", self.configure_finished_error())
        execution = self.telemetry_payload("configure_finished")["execution"]
        cycle = execution["measurements"]["reboot_cycle"][0]
        self.assertEqual(cycle["result"], "ssh_not_open")
        self.assertIn("reset_seen_after_sec", cycle)
        self.assertEqual(execution["measurements"]["reboot_request"][0]["strategy"], "network_acp")

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
        self.assertNotIn("TC_MDNS_DEVICE_MODEL", result.values)

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
        self.assertNotIn("TC_AIRPORT_SYAP", result.values)
        self.assertNotIn("TC_MDNS_DEVICE_MODEL", result.values)

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
        self.assertNotIn("TC_AIRPORT_SYAP", result.values)
        self.assertNotIn("mDNS device model hint", seen_defaults)
        self.assertNotIn("TC_MDNS_DEVICE_MODEL", result.values)



class ConfigurePromptEncodingTests(unittest.TestCase):
    """A non-UTF-8 terminal under a UTF-8 locale sends bytes input() cannot
    decode; configure must ask again instead of dying with a traceback."""

    def bad_decode(self) -> UnicodeDecodeError:
        return UnicodeDecodeError("utf-8", b"\xd0a", 0, 1, "invalid continuation byte")

    def test_host_prompt_asks_again_after_undecodable_input(self) -> None:
        with mock.patch("builtins.input", side_effect=[self.bad_decode(), "root@10.0.1.20"]):
            with redirect_stdout(io.StringIO()) as output:
                value = configure.prompt("SSH target", "", False)

        self.assertEqual(value, "root@10.0.1.20")
        self.assertIn("could not be read as", output.getvalue())

    def test_device_choice_asks_again_after_undecodable_input(self) -> None:
        # Stands in for BonjourResolvedService, whose properties default to {}.
        records = [SimpleNamespace(name="Capsule", display_host=lambda: "capsule.local", ipv4=["10.0.1.2"], ipv6=[], properties={})]
        with mock.patch("builtins.input", side_effect=[self.bad_decode(), "1"]):
            with redirect_stdout(io.StringIO()):
                chosen = configure.choose_device(records)

        self.assertIs(chosen, records[0])

    def test_device_choice_skips_discovery_at_end_of_input(self) -> None:
        # Ctrl-D (or a closed stdin) at the device list falls back to the SSH
        # target prompt rather than escaping as a discovery failure.
        records = [SimpleNamespace(name="Capsule", display_host=lambda: "capsule.local", ipv4=["10.0.1.2"], ipv6=[], properties={})]
        with mock.patch("builtins.input", side_effect=EOFError):
            with redirect_stdout(io.StringIO()) as output:
                chosen = configure.choose_device(records)

        self.assertIsNone(chosen)
        # One newline ends the unanswered prompt; the fallback message follows on the next line.
        self.assertEqual(output.getvalue(), "\n")


if __name__ == "__main__":
    unittest.main()
