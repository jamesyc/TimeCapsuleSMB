"""The flash command and its firmware payload checks."""
from __future__ import annotations

import argparse
from collections.abc import Iterator
import io
import json
import struct
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import zlib
import timecapsulesmb.cli.main as cli_main_module
from timecapsulesmb.apple_firmware import FirmwareTemplateCandidate
from timecapsulesmb.basebinary import (
    BasebinaryKey,
    parse_nested_basebinary,
)
from timecapsulesmb.cli import flash as cli_flash
from timecapsulesmb.cli.runtime import NonInteractivePromptError
from timecapsulesmb.services import flash as flash_service
from timecapsulesmb.device.compat import DeviceCompatibility
from timecapsulesmb.flash import (
    FlashAnalysisError,
    PATCHED_LOGIN_SCRIPT,
    STOCK_LOGIN_NETBSD4_DUMMY,
    sha256_hex,
)
from timecapsulesmb.transport.ssh import SshConnection, SshError
from timecapsulesmb.integrations.acp import ACPAuthError, ACPConnectionError
from timecapsulesmb.core.config import AppConfig

from tests.cli_support import CliTestCase, FakeCommandContext
from tests.reboot_support import FakeAcpDevice
from timecapsulesmb.services.reboot import reboot_device
from tests.flash_fixtures import (
    bank_checksum,
    bank_end_offset,
    firmware_template,
    flash_inputs,
    make_bank,
    patched_bank,
    zopfli_available,
)


class CliFlashTests(CliTestCase):
    @contextmanager
    def flash_cli(self, command_context: FakeCommandContext, config: AppConfig | None = None) -> Iterator[None]:
        """Run cli_flash.main with this command context and config (default: a valid .env)."""
        if config is None:
            config = self.make_app_config(self.make_valid_env())
        with mock.patch("timecapsulesmb.cli.flash.load_env_config", return_value=config):
            with mock.patch("timecapsulesmb.cli.flash.CommandContext", return_value=command_context):
                yield

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

    def test_main_registers_flash_command(self) -> None:
        self.assertIs(cli_main_module.COMMANDS["flash"], cli_flash.main)

    def test_flash_read_only_saves_banks_and_manifest(self) -> None:
        output = io.StringIO()
        primary = make_bank(release=b"NetBSD 4.0_STABLE #0: current")
        secondary = make_bank(release=b"NetBSD 4.0_BETA2 #0: old")
        command_context = FakeCommandContext(compatibility=self.make_supported_netbsd4_stable_compatibility())
        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp) / "backup"
            with zopfli_available():
                config = self.make_app_config(self.make_valid_env(
                    TC_NET_IFACE="bad iface from config",
                    TC_AIRPORT_SYAP="999",
                    TC_MDNS_DEVICE_MODEL="NotADevice",
                ))
                with self.flash_cli(command_context, config):
                    with mock.patch(
                        "timecapsulesmb.services.flash.read_flash_inputs",
                        return_value=flash_inputs(primary, secondary),
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
        primary = make_bank(release=b"NetBSD 4.0_STABLE #0: current")
        secondary = make_bank(release=b"NetBSD 4.0_BETA2 #0: old")
        command_context = FakeCommandContext(compatibility=self.make_supported_netbsd4_stable_compatibility())

        with tempfile.TemporaryDirectory() as tmp:
            with self.flash_cli(command_context):
                with mock.patch("timecapsulesmb.services.flash.dump_remote_bank", side_effect=[primary, secondary]) as dump_mock:
                    with mock.patch("timecapsulesmb.services.flash.get_property_int", side_effect=ACPAuthError("ACP command failed with error_code -0x10")):
                        with mock.patch("timecapsulesmb.services.flash.read_live_login", side_effect=AssertionError("LOGIN should not be read after ACP failure")) as login_mock:
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
        primary = make_bank(release=b"NetBSD 4.0_STABLE #0: current")
        command_context = FakeCommandContext(compatibility=self.make_supported_netbsd4_stable_compatibility())

        with tempfile.TemporaryDirectory() as tmp:
            with self.flash_cli(command_context):
                with mock.patch(
                    "timecapsulesmb.services.flash.dump_remote_bank",
                    side_effect=[primary, SshError("ssh command failed with rc=255")],
                ) as dump_mock:
                    with mock.patch("timecapsulesmb.services.flash.get_property_int", side_effect=AssertionError("ACP should not be read after SSH failure")) as acp_mock:
                        with mock.patch("timecapsulesmb.services.flash.read_live_login", side_effect=AssertionError("LOGIN should not be read after SSH failure")) as login_mock:
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

    def test_flash_restore_inspection_error_is_reported_without_system_exit(self) -> None:
        output = io.StringIO()
        primary = make_bank(release=b"NetBSD 4.0_STABLE #0: current")
        secondary = make_bank(release=b"NetBSD 4.0_BETA2 #0: old")
        corrupt_secondary = b"not a valid firmware bank"
        command_context = FakeCommandContext(compatibility=self.make_supported_netbsd4_stable_compatibility())

        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp) / "backup"
            with self.flash_cli(command_context):
                with mock.patch(
                    "timecapsulesmb.services.flash.read_flash_inputs",
                    return_value=flash_inputs(
                        primary,
                        corrupt_secondary,
                        cks2=bank_checksum(secondary),
                    ),
                ):
                    with redirect_stdout(output):
                        rc = cli_flash.main([
                            "--restore",
                            "--yes",
                            "--backup-dir",
                            str(backup_dir),
                        ])
            manifest = json.loads((backup_dir / "manifest.json").read_text())

        self.assertEqual(rc, 1)
        self.assertEqual(manifest["banks"][1]["backup_valid"], False)
        self.assertIn("expected exactly one valid footer, found 0", output.getvalue())
        finished = command_context.finish.call_args.kwargs
        self.assertEqual(finished["result"], "failure")
        self.assertIn("flash_error_stage=plan_flash", finished["error"])
        self.assertIn("expected exactly one valid footer, found 0", finished["error"])
        self.assertNotIn("flash_error_stage", finished)
        self.assertNotIn("flash_error", finished)

    def test_flash_refuses_when_probed_syap_is_missing(self) -> None:
        output = io.StringIO()
        command_context = FakeCommandContext(compatibility=self.make_supported_netbsd4_stable_compatibility())
        with tempfile.TemporaryDirectory() as tmp:
            with self.flash_cli(command_context, self.make_app_config(self.make_valid_env(TC_AIRPORT_SYAP="113"))):
                with mock.patch(
                    "timecapsulesmb.services.flash.read_flash_inputs",
                    side_effect=FlashAnalysisError("syAP is missing"),
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
        primary = make_bank(release=b"NetBSD 4.0_STABLE #0: current")
        secondary = make_bank(release=b"NetBSD 4.0_BETA2 #0: old")
        command_context = FakeCommandContext(compatibility=self.make_supported_netbsd4_stable_compatibility())

        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp) / "backup"
            with zopfli_available():
                with self.flash_cli(command_context, self.make_app_config(self.make_valid_env(TC_AIRPORT_SYAP="113"))):
                    with mock.patch(
                        "timecapsulesmb.services.flash.read_flash_inputs",
                        return_value=flash_inputs(primary, secondary, syap="0"),
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
        primary = make_bank(release=b"NetBSD 4.0_STABLE #0: current")
        secondary = make_bank(release=b"NetBSD 4.0_BETA2 #0: old")
        command_context = FakeCommandContext(compatibility=self.make_supported_netbsd4_stable_compatibility())
        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp) / "backup"
            with zopfli_available():
                with self.flash_cli(command_context):
                    with mock.patch(
                        "timecapsulesmb.services.flash.read_flash_inputs",
                        return_value=flash_inputs(primary, secondary),
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
        primary = make_bank(release=b"NetBSD 4.0_BETA2 #0: old")
        secondary = make_bank(release=b"NetBSD 4.0_STABLE #0: current")
        command_context = FakeCommandContext(compatibility=self.make_supported_netbsd4_stable_compatibility())
        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp) / "backup"
            with zopfli_available():
                with self.flash_cli(command_context):
                    with mock.patch(
                        "timecapsulesmb.services.flash.read_flash_inputs",
                        return_value=flash_inputs(primary, secondary),
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
        primary = make_bank(release=b"NetBSD 4.0_STABLE #0: current")
        secondary = make_bank(release=b"NetBSD 4.0_STABLE #0: current")
        command_context = FakeCommandContext(compatibility=self.make_supported_netbsd4_stable_compatibility())
        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp) / "backup"
            with zopfli_available():
                with self.flash_cli(command_context):
                    with mock.patch(
                        "timecapsulesmb.services.flash.read_flash_inputs",
                        return_value=flash_inputs(primary, secondary),
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

    def test_flash_read_only_uses_live_login_to_select_between_active_candidates(self) -> None:
        output = io.StringIO()
        stock_primary = make_bank(release=b"NetBSD 4.0_STABLE #0: current")
        secondary = make_bank(release=b"NetBSD 4.0_STABLE #0: current")
        command_context = FakeCommandContext(compatibility=self.make_supported_netbsd4_stable_compatibility())
        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp) / "backup"
            with zopfli_available():
                patched_primary = patched_bank(stock_primary)
                with self.flash_cli(command_context):
                    with mock.patch(
                        "timecapsulesmb.services.flash.read_flash_inputs",
                        return_value=flash_inputs(
                            patched_primary,
                            secondary,
                            live_login=PATCHED_LOGIN_SCRIPT,
                        ),
                    ):
                        with redirect_stdout(output):
                            rc = cli_flash.main(["--read-only", "--backup-dir", str(backup_dir)])

            manifest = json.loads((backup_dir / "manifest.json").read_text())

        self.assertEqual(rc, 0)
        self.assertEqual(manifest["active_bank"], "primary")
        self.assertEqual(manifest["active_selection"]["status"], "selected")
        self.assertEqual(manifest["active_selection"]["selected_by"], "live_login")
        self.assertEqual(manifest["active_selection"]["candidates"], ["primary"])
        self.assertEqual(manifest["banks"][0]["live_login_match"], True)
        self.assertEqual(manifest["banks"][1]["live_login_match"], False)

    def test_flash_patch_targets_primary_when_both_banks_are_active_candidates(self) -> None:
        output = io.StringIO()
        primary = make_bank(release=b"NetBSD 4.0_STABLE #0: current")
        secondary = make_bank(release=b"NetBSD 4.0_STABLE #0: current")
        command_context = FakeCommandContext(compatibility=self.make_supported_netbsd4_stable_compatibility())
        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp) / "backup"
            template_path = Path(tmp) / "7.8.1.basebinary"
            template_path.write_bytes(firmware_template(primary, product_id=113))
            with zopfli_available():
                with self.flash_cli(command_context):
                    with mock.patch(
                        "timecapsulesmb.services.flash.read_flash_inputs",
                        return_value=flash_inputs(primary, secondary),
                    ):
                        with mock.patch("timecapsulesmb.services.flash.flash_firmware_bank") as flash_mock:
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
        flash_mock.assert_not_called()
        self.assertEqual(manifest["active_selection"]["status"], "multiple_candidates")
        self.assertEqual(manifest["active_selection"]["candidates"], ["primary", "secondary"])
        self.assertEqual(manifest["write_policy"], "primary_bank_patch")
        self.assertEqual(manifest["flash_plan"]["target_bank"], "primary")
        self.assertTrue(manifest["banks"][0]["would_write"])
        self.assertFalse(manifest["banks"][1]["would_write"])
        self.assertEqual(manifest["banks"][0]["write_decision"], "primary bank patch planned")
        self.assertEqual(manifest["banks"][1]["write_decision"], "secondary backup left unmodified")
        self.assertEqual(manifest["write_outcome"]["status"], "cancelled")
        self.assertEqual(command_context.finish.call_args.kwargs["result"], "cancelled")

    def test_flash_patch_refuses_when_no_active_candidates_pass(self) -> None:
        output = io.StringIO()
        primary = make_bank(release=b"NetBSD 4.0_BETA2 #0: old")
        secondary = make_bank(release=b"NetBSD 4.0_BETA2 #0: old")
        command_context = FakeCommandContext(compatibility=self.make_supported_netbsd4_stable_compatibility())
        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp) / "backup"
            with zopfli_available():
                with self.flash_cli(command_context):
                    with mock.patch(
                        "timecapsulesmb.services.flash.read_flash_inputs",
                        return_value=flash_inputs(primary, secondary),
                    ):
                        with mock.patch("timecapsulesmb.services.flash.flash_firmware_bank") as flash_mock:
                            with redirect_stdout(output):
                                rc = cli_flash.main(["--patch", "--yes", "--backup-dir", str(backup_dir)])
            manifest = json.loads((backup_dir / "manifest.json").read_text())

        self.assertEqual(rc, 1)
        flash_mock.assert_not_called()
        self.assertEqual(manifest["active_selection"]["status"], "no_candidates")
        self.assertEqual(manifest["active_selection"]["candidates"], [])
        self.assertIn("refusing to patch primary because primary is not an active firmware candidate", output.getvalue())
        self.assertIn("primary: backup=valid; active=not_candidate", output.getvalue())
        self.assertIn("secondary: backup=valid; active=not_candidate", output.getvalue())
        self.assertIn("`tcapsule flash --patch --force` patches the primary bank anyway", output.getvalue())
        self.assertEqual(manifest["flash_plan_error"]["stage"], "plan_flash")
        self.assertIn("refusing to patch primary", manifest["flash_plan_error"]["message"])
        self.assertNotIn("flash_plan", manifest)
        self.assertEqual(manifest["operation"], "patch")

    def test_flash_patch_refuses_when_only_secondary_is_active_candidate(self) -> None:
        output = io.StringIO()
        primary = make_bank(release=b"NetBSD 4.0_BETA2 #0: old")
        secondary = make_bank(release=b"NetBSD 4.0_STABLE #0: current")
        command_context = FakeCommandContext(compatibility=self.make_supported_netbsd4_stable_compatibility())
        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp) / "backup"
            with zopfli_available():
                with self.flash_cli(command_context):
                    with mock.patch(
                        "timecapsulesmb.services.flash.read_flash_inputs",
                        return_value=flash_inputs(primary, secondary),
                    ):
                        with mock.patch("timecapsulesmb.services.flash.flash_firmware_bank") as flash_mock:
                            with redirect_stdout(output):
                                rc = cli_flash.main(["--patch", "--yes", "--backup-dir", str(backup_dir)])
            manifest = json.loads((backup_dir / "manifest.json").read_text())

        self.assertEqual(rc, 1)
        flash_mock.assert_not_called()
        self.assertEqual(manifest["active_selection"]["status"], "selected")
        self.assertEqual(manifest["active_selection"]["candidates"], ["secondary"])
        self.assertIn("refusing to patch primary because primary is not an active firmware candidate", output.getvalue())
        self.assertIn("primary: backup=valid; active=not_candidate", output.getvalue())
        self.assertIn("secondary: backup=valid; active=candidate", output.getvalue())

    def test_flash_patch_force_bypasses_invalid_secondary_backup_and_targets_primary(self) -> None:
        output = io.StringIO()
        primary = make_bank(release=b"NetBSD 4.0_STABLE #0: current")
        secondary = make_bank(release=b"NetBSD 4.0_BETA2 #0: old")
        corrupt_secondary = b"not a valid firmware bank"
        command_context = FakeCommandContext(compatibility=self.make_supported_netbsd4_stable_compatibility())
        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp) / "backup"
            template_path = Path(tmp) / "7.8.1.basebinary"
            template_path.write_bytes(firmware_template(primary, product_id=113))
            with zopfli_available():
                with self.flash_cli(command_context):
                    with mock.patch(
                        "timecapsulesmb.services.flash.read_flash_inputs",
                        return_value=flash_inputs(
                            primary,
                            corrupt_secondary,
                            cks2=bank_checksum(secondary),
                        ),
                    ):
                        with mock.patch("timecapsulesmb.services.flash.flash_firmware_bank") as flash_mock:
                            with mock.patch("builtins.input", return_value="n"):
                                with redirect_stdout(output):
                                    rc = cli_flash.main([
                                        "--patch",
                                        "--force",
                                        "--firmware-template",
                                        str(template_path),
                                        "--backup-dir",
                                        str(backup_dir),
                                    ])
            manifest = json.loads((backup_dir / "manifest.json").read_text())

        self.assertEqual(rc, 0)
        flash_mock.assert_not_called()
        self.assertFalse(manifest["banks"][1]["backup_valid"])
        self.assertEqual(manifest["flash_plan"]["target_bank"], "primary")
        self.assertEqual(manifest["flash_plan"]["warnings"], ["patch forced despite one or more invalid backup banks"])
        self.assertTrue(manifest["banks"][0]["would_write"])
        self.assertEqual(manifest["write_outcome"]["status"], "cancelled")

    def test_flash_patch_force_bypasses_secondary_only_candidate_and_targets_primary(self) -> None:
        output = io.StringIO()
        primary = make_bank(release=b"NetBSD 4.0_BETA2 #0: old")
        secondary = make_bank(release=b"NetBSD 4.0_STABLE #0: current")
        command_context = FakeCommandContext(compatibility=self.make_supported_netbsd4_stable_compatibility())
        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp) / "backup"
            template_path = Path(tmp) / "7.8.1.basebinary"
            template_path.write_bytes(firmware_template(primary, product_id=113))
            with zopfli_available():
                with self.flash_cli(command_context):
                    with mock.patch(
                        "timecapsulesmb.services.flash.read_flash_inputs",
                        return_value=flash_inputs(primary, secondary),
                    ):
                        with mock.patch("timecapsulesmb.services.flash.flash_firmware_bank") as flash_mock:
                            with mock.patch("builtins.input", return_value="n"):
                                with redirect_stdout(output):
                                    rc = cli_flash.main([
                                        "--patch",
                                        "--force",
                                        "--firmware-template",
                                        str(template_path),
                                        "--backup-dir",
                                        str(backup_dir),
                                    ])
            manifest = json.loads((backup_dir / "manifest.json").read_text())

        self.assertEqual(rc, 0)
        flash_mock.assert_not_called()
        self.assertEqual(manifest["active_selection"]["candidates"], ["secondary"])
        self.assertEqual(manifest["flash_plan"]["target_bank"], "primary")
        self.assertEqual(
            manifest["flash_plan"]["warnings"],
            ["patch forced even though the primary bank did not pass active-candidate checks"],
        )
        self.assertTrue(manifest["banks"][0]["would_write"])
        self.assertEqual(manifest["write_outcome"]["status"], "cancelled")

    def test_flash_read_only_json_outputs_manifest(self) -> None:
        output = io.StringIO()
        primary = make_bank(release=b"NetBSD 4.0_STABLE #0: current")
        secondary = make_bank(release=b"NetBSD 4.0_BETA2 #0: old")
        command_context = FakeCommandContext(compatibility=self.make_supported_netbsd4_stable_compatibility())
        with tempfile.TemporaryDirectory() as tmp:
            with zopfli_available():
                with self.flash_cli(command_context):
                    with mock.patch(
                        "timecapsulesmb.services.flash.read_flash_inputs",
                        return_value=flash_inputs(primary, secondary),
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

    def test_flash_force_requires_patch_mode(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as raised:
                cli_flash.main(["--read-only", "--force"])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("--force is only valid with --patch", stderr.getvalue())

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
                        with mock.patch("timecapsulesmb.services.flash.read_flash_inputs") as read_mock:
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
        primary = make_bank(release=b"NetBSD 4.0_STABLE #0: current")
        secondary = make_bank(release=b"NetBSD 4.0_BETA2 #0: old")
        command_context = FakeCommandContext(compatibility=self.make_supported_netbsd4_stable_compatibility())
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("timecapsulesmb.flash.require_python_module", side_effect=AssertionError("zopfli not needed")):
                with self.flash_cli(command_context):
                    with mock.patch(
                        "timecapsulesmb.services.flash.read_flash_inputs",
                        return_value=flash_inputs(primary, secondary),
                    ):
                        with redirect_stdout(output):
                            rc = cli_flash.main(["--read-only", "--backup-dir", str(Path(tmp) / "backup")])

        self.assertEqual(rc, 0)
        self.assertIn("Backed up firmware banks to:", output.getvalue())

    def test_flash_write_prompt_decline_cancels_without_write(self) -> None:
        output = io.StringIO()
        primary = make_bank(release=b"NetBSD 4.0_STABLE #0: current")
        secondary = make_bank(release=b"NetBSD 4.0_BETA2 #0: old")
        command_context = FakeCommandContext(compatibility=self.make_supported_netbsd4_stable_compatibility())
        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp) / "backup"
            template_path = Path(tmp) / "7.8.1.basebinary"
            template_path.write_bytes(firmware_template(primary, product_id=113))
            with zopfli_available():
                with self.flash_cli(command_context):
                    with mock.patch(
                        "timecapsulesmb.services.flash.read_flash_inputs",
                        return_value=flash_inputs(primary, secondary),
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

    def run_patch_until_prompt(self, *, answer):
        output = io.StringIO()
        primary = make_bank(release=b"NetBSD 4.0_STABLE #0: current")
        secondary = make_bank(release=b"NetBSD 4.0_BETA2 #0: old")
        command_context = FakeCommandContext(compatibility=self.make_supported_netbsd4_stable_compatibility())
        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp) / "backup"
            template_path = Path(tmp) / "7.8.1.basebinary"
            template_path.write_bytes(firmware_template(primary, product_id=113))
            with ExitStack() as stack:
                stack.enter_context(zopfli_available())
                stack.enter_context(mock.patch("timecapsulesmb.cli.flash.load_env_config", return_value=self.make_app_config(self.make_valid_env())))
                stack.enter_context(mock.patch("timecapsulesmb.cli.flash.CommandContext", return_value=command_context))
                stack.enter_context(mock.patch(
                    "timecapsulesmb.services.flash.read_flash_inputs",
                    return_value=flash_inputs(primary, secondary),
                ))
                inspect = stack.enter_context(mock.patch(
                    "timecapsulesmb.services.flash.inspect_flash_banks", wraps=flash_service.inspect_flash_banks,
                ))
                flash_mock = stack.enter_context(mock.patch("timecapsulesmb.services.flash.flash_firmware_bank"))
                stack.enter_context(mock.patch("builtins.input", side_effect=answer))
                stack.enter_context(redirect_stdout(output))
                try:
                    rc = cli_flash.main([
                        "--patch",
                        "--firmware-template",
                        str(template_path),
                        "--backup-dir",
                        str(backup_dir),
                    ])
                except NonInteractivePromptError as exc:
                    # The fake context lets it escape; the real one exits with its message.
                    rc = exc
            manifest = json.loads((backup_dir / "manifest.json").read_text())
        return rc, output.getvalue(), manifest, command_context, inspect, flash_mock

    def test_flash_patch_plans_from_the_saved_backup_and_builds_the_candidate_once(self) -> None:
        rc, text, manifest, command_context, inspect, flash_mock = self.run_patch_until_prompt(answer=["n"])

        self.assertEqual(rc, 0)
        flash_mock.assert_not_called()
        builds = [call.kwargs.get("build_primary_patch_candidate", False) for call in inspect.call_args_list]
        self.assertEqual(builds, [False, True])
        # The first pass sees the live LOGIN; planning reads its saved evidence back.
        self.assertIn("live_login", inspect.call_args_list[0].kwargs)
        self.assertIn("saved_live_login_matches", inspect.call_args_list[1].kwargs)
        self.assertIn("[flash] Building patched gzip candidate...", text)
        self.assertEqual(manifest["flash_plan"]["target_bank"], "primary")
        self.assertEqual(manifest["flash_plan_params"]["operation"], "patch")
        self.assertIn("primary_patched", manifest["files"])
        finished = command_context.finish.call_args.kwargs
        self.assertEqual(finished["flash_plan_mode"], "patch")
        self.assertEqual([key for key in finished if key.endswith("_path")], [])

    def test_flash_patch_without_interactive_input_refuses_before_writing(self) -> None:
        rc, _text, manifest, command_context, _inspect, flash_mock = self.run_patch_until_prompt(answer=EOFError)

        self.assertIsInstance(rc, NonInteractivePromptError)
        self.assertEqual(
            str(rc),
            "No answer was read for the flash write confirmation. Use `flash --patch --yes` to skip the prompt.",
        )
        flash_mock.assert_not_called()
        self.assertEqual(manifest["flash_plan"]["target_bank"], "primary")
        self.assertNotIn("write_outcome", manifest)
        self.assertEqual(command_context.finish.call_args.kwargs["result"], "failure")

    def test_flash_check_apple_uses_the_live_login_bank_choice_from_the_saved_backup(self) -> None:
        output = io.StringIO()
        stock_primary = make_bank(release=b"NetBSD 4.0_STABLE #0: current")
        secondary = make_bank(release=b"NetBSD 4.0_STABLE #0: current")
        command_context = FakeCommandContext(compatibility=self.make_supported_netbsd4_stable_compatibility())
        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp) / "backup"
            template_path = Path(tmp) / "7.8.1.basebinary"
            with zopfli_available():
                patched_primary = patched_bank(stock_primary)
            template_path.write_bytes(firmware_template(stock_primary, product_id=113))
            with self.flash_cli(command_context):
                with mock.patch(
                    "timecapsulesmb.services.flash.read_flash_inputs",
                    return_value=flash_inputs(patched_primary, secondary, live_login=PATCHED_LOGIN_SCRIPT),
                ):
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
        self.assertEqual(manifest["active_bank"], "primary")
        self.assertEqual(manifest["active_selection"]["selected_by"], "live_login")
        self.assertEqual(manifest["flash_plan"]["mode"], "check_apple")
        # Every candidate is still compared; the live LOGIN picks which one is the target.
        self.assertEqual(manifest["flash_plan"]["target_bank"], "primary")
        self.assertEqual([result["bank"] for result in manifest["flash_plan"]["apple_matches"]], ["primary", "secondary"])
        self.assertEqual(command_context.finish.call_args.kwargs["active_selection_selected_by"], "live_login")

    def test_flash_yes_without_write_mode_rejects(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as raised:
                cli_flash.main(["--yes"])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("--yes is only valid with --patch or --restore", stderr.getvalue())

    def test_flash_write_refuses_unsupported_firmware_key_before_acp(self) -> None:
        output = io.StringIO()
        primary = make_bank(release=b"NetBSD 4.0_STABLE #0: current")
        secondary = make_bank(release=b"NetBSD 4.0_BETA2 #0: old")
        command_context = FakeCommandContext(compatibility=self.make_supported_netbsd4_stable_compatibility())
        unknown_key = BasebinaryKey.from_hex("unknown-test", "00112233445566778899aabbccddeeff")
        unsupported_template = FirmwareTemplateCandidate(
            data=firmware_template(primary, product_id=113, key=unknown_key),
            source="test-unsupported.basebinary",
            path=None,
            product_id="113",
            version="7.8.1",
        )

        with tempfile.TemporaryDirectory() as tmp:
            with zopfli_available():
                with self.flash_cli(command_context):
                    with mock.patch(
                        "timecapsulesmb.services.flash.read_flash_inputs",
                        return_value=flash_inputs(primary, secondary),
                    ):
                        with mock.patch("timecapsulesmb.flash_payloads.resolve_firmware_template_candidates", return_value=[unsupported_template]):
                            with mock.patch("timecapsulesmb.services.flash.flash_firmware_bank") as flash_mock:
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
        primary = make_bank(release=b"NetBSD 4.0_STABLE #0: current")
        secondary = make_bank(release=b"NetBSD 4.0_BETA2 #0: old")
        command_context = FakeCommandContext(compatibility=self.make_supported_netbsd4_stable_compatibility())
        written: dict[str, bytes] = {}

        def fake_flash(_host: str, _password: str, bank_name: str, payload: bytes, **_kwargs: object) -> SimpleNamespace:
            written["bank_name"] = bank_name.encode()
            written["payload"] = payload
            return SimpleNamespace(command=0x03, reply_body=b"")

        def fake_get_property(_host: str, _password: str, name: str, **_kwargs: object) -> int:
            self.assertEqual(name, "cks1")
            return bank_checksum(fake_readback(None, ""))

        def fake_readback(_conn: object, _dev: str, **_kwargs: object) -> bytes:
            reparsed = parse_nested_basebinary(written["payload"])
            end_offset = bank_end_offset(primary)
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
            template_path.write_bytes(firmware_template(primary, product_id=113))
            with zopfli_available():
                with self.flash_cli(command_context):
                    with mock.patch(
                        "timecapsulesmb.services.flash.read_flash_inputs",
                        return_value=flash_inputs(primary, secondary),
                    ):
                        with mock.patch("timecapsulesmb.services.flash.flash_firmware_bank", side_effect=fake_flash) as flash_mock:
                            with mock.patch("timecapsulesmb.services.flash.dump_remote_bank", side_effect=fake_readback):
                                with mock.patch("timecapsulesmb.services.flash.get_property_int", side_effect=fake_get_property):
                                    with FakeAcpDevice().patched() as reboot_mock:
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
        self.assertEqual(reparsed_payload.inner.payload, fake_readback(None, "")[: bank_end_offset(primary)])
        self.assertTrue(payload_file_exists)
        self.assertEqual(reboot_mock.calls, [])
        self.assertEqual(manifest["write_outcome"]["status"], "validated")
        self.assertTrue(manifest["write_outcome"]["write_may_have_modified_device"])
        self.assertEqual(manifest["write_result"]["bank"], "primary")
        self.assertEqual(manifest["write_result"]["login_classification"], "already_patched")
        self.assertEqual(manifest["write_result"]["expected_bank_sha256"], manifest["write_result"]["readback_sha256"])
        self.assertEqual(manifest["flash_plan"]["payload"]["key_id"], "observed-k30a-78100")
        self.assertIn("POWER-CYCLE REQUIRED", output.getvalue())
        self.assertIn("Patch write successful.\x1b[0m The device needs to be manually rebooted.", output.getvalue())
        self.assertEqual(command_context.finish.call_args.kwargs["result"], "success")

    def test_flash_patch_writes_primary_when_both_banks_are_active_candidates(self) -> None:
        output = io.StringIO()
        primary = make_bank(release=b"NetBSD 4.0_STABLE #0: current")
        secondary = make_bank(release=b"NetBSD 4.0_STABLE #0: current")
        command_context = FakeCommandContext(compatibility=self.make_supported_netbsd4_stable_compatibility())
        written: dict[str, bytes] = {}

        def fake_flash(_host: str, _password: str, bank_name: str, payload: bytes, **_kwargs: object) -> SimpleNamespace:
            written["bank_name"] = bank_name.encode()
            written["payload"] = payload
            return SimpleNamespace(command=0x03, reply_body=b"")

        def fake_get_property(_host: str, _password: str, name: str, **_kwargs: object) -> int:
            self.assertEqual(name, "cks1")
            return bank_checksum(fake_readback(None, ""))

        def fake_readback(_conn: object, _dev: str, **_kwargs: object) -> bytes:
            reparsed = parse_nested_basebinary(written["payload"])
            end_offset = bank_end_offset(primary)
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
            template_path.write_bytes(firmware_template(primary, product_id=113))
            with zopfli_available():
                with self.flash_cli(command_context):
                    with mock.patch(
                        "timecapsulesmb.services.flash.read_flash_inputs",
                        return_value=flash_inputs(primary, secondary),
                    ):
                        with mock.patch("timecapsulesmb.services.flash.flash_firmware_bank", side_effect=fake_flash):
                            with mock.patch("timecapsulesmb.services.flash.dump_remote_bank", side_effect=fake_readback):
                                with mock.patch("timecapsulesmb.services.flash.get_property_int", side_effect=fake_get_property):
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

        self.assertEqual(rc, 0)
        self.assertEqual(written["bank_name"], b"primary")
        self.assertIsNone(manifest["active_bank"])
        self.assertEqual(manifest["active_selection"]["status"], "multiple_candidates")
        self.assertIsNone(manifest["active_selection"]["selected_by"])
        self.assertEqual(manifest["active_selection"]["candidates"], ["primary", "secondary"])
        self.assertEqual(manifest["flash_plan"]["target_bank"], "primary")
        self.assertEqual(manifest["flash_plan"]["warnings"], [])

    def test_flash_patch_noops_when_active_bank_is_already_patched(self) -> None:
        output = io.StringIO()
        stock_primary = make_bank(release=b"NetBSD 4.0_STABLE #0: current")
        secondary = make_bank(release=b"NetBSD 4.0_BETA2 #0: old")
        command_context = FakeCommandContext(compatibility=self.make_supported_netbsd4_stable_compatibility())

        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp) / "backup"
            with zopfli_available():
                patched_primary = patched_bank(stock_primary, secondary)
                with self.flash_cli(command_context):
                    with mock.patch(
                        "timecapsulesmb.services.flash.read_flash_inputs",
                        return_value=flash_inputs(patched_primary, secondary, live_login=PATCHED_LOGIN_SCRIPT),
                    ):
                        with mock.patch("timecapsulesmb.flash_payloads.resolve_firmware_template_candidates", side_effect=AssertionError("no template needed")):
                            with mock.patch("timecapsulesmb.services.flash.flash_firmware_bank") as flash_mock:
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
        self.assertIn("Primary firmware bank is already patched; no write needed.", output.getvalue())
        self.assertEqual(command_context.finish.call_args.kwargs["result"], "success")

    def test_flash_restore_targets_primary_when_both_banks_are_active_candidates(self) -> None:
        output = io.StringIO()
        stock_primary = make_bank(release=b"NetBSD 4.0_STABLE #0: current")
        stock_secondary = make_bank(release=b"NetBSD 4.0_STABLE #0: current")
        command_context = FakeCommandContext(compatibility=self.make_supported_netbsd4_stable_compatibility())

        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp) / "backup"
            template_path = Path(tmp) / "7.8.1.basebinary"
            patched_primary = patched_bank(stock_primary)
            patched_secondary = patched_bank(stock_secondary)
            template_path.write_bytes(firmware_template(stock_primary, product_id=113))
            with self.flash_cli(command_context):
                with mock.patch(
                    "timecapsulesmb.services.flash.read_flash_inputs",
                    return_value=flash_inputs(
                        patched_primary,
                        patched_secondary,
                        live_login=PATCHED_LOGIN_SCRIPT,
                    ),
                ):
                    with mock.patch("timecapsulesmb.services.flash.flash_firmware_bank") as flash_mock:
                        with mock.patch("builtins.input", return_value="n"):
                            with redirect_stdout(output):
                                rc = cli_flash.main([
                                    "--restore",
                                    "--firmware-template",
                                    str(template_path),
                                    "--backup-dir",
                                    str(backup_dir),
                                ])
            manifest = json.loads((backup_dir / "manifest.json").read_text())

        self.assertEqual(rc, 0)
        flash_mock.assert_not_called()
        self.assertIsNone(manifest["active_bank"])
        self.assertEqual(manifest["active_selection"]["status"], "multiple_candidates")
        self.assertEqual(manifest["active_selection"]["candidates"], ["primary", "secondary"])
        self.assertEqual(manifest["write_policy"], "target_bank_restore")
        self.assertEqual(manifest["flash_plan"]["target_bank"], "primary")
        self.assertEqual(
            manifest["flash_plan"]["warnings"],
            ["restore targets primary because multiple firmware banks passed active selection checks"],
        )
        self.assertTrue(manifest["banks"][0]["would_write"])
        self.assertEqual(manifest["banks"][0]["write_decision"], "primary bank restore from Apple firmware planned")
        self.assertFalse(manifest["banks"][1]["would_write"])
        self.assertEqual(manifest["banks"][1]["write_decision"], "secondary bank left unmodified")
        self.assertEqual(manifest["write_outcome"]["status"], "cancelled")
        self.assertIn("Warning: restore targets primary because multiple firmware banks passed active selection checks", output.getvalue())

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
        stock_primary = make_bank(release=b"NetBSD 4.0_STABLE #0: current")
        secondary = make_bank(release=b"NetBSD 4.0_BETA2 #0: old")
        patched_primary = patched_bank(stock_primary, secondary)
        template = firmware_template(stock_primary, product_id=113)
        command_context = FakeCommandContext(compatibility=self.make_supported_netbsd4_stable_compatibility())
        written: dict[str, bytes] = {}

        def fake_flash(_host: str, _password: str, bank_name: str, payload: bytes, **_kwargs: object) -> SimpleNamespace:
            written["bank_name"] = bank_name.encode()
            written["payload"] = payload
            return SimpleNamespace(command=0x03, reply_body=b"")

        def fake_readback(_conn: object, _dev: str, **_kwargs: object) -> bytes:
            reparsed = parse_nested_basebinary(written["payload"])
            end_offset = bank_end_offset(patched_primary)
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
            return bank_checksum(fake_readback(None, ""))

        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp) / "backup"
            template_path = Path(tmp) / "7.8.1.basebinary"
            template_path.write_bytes(template)
            with self.flash_cli(command_context):
                with mock.patch(
                    "timecapsulesmb.services.flash.read_flash_inputs",
                    return_value=flash_inputs(patched_primary, secondary, live_login=PATCHED_LOGIN_SCRIPT),
                ):
                    with mock.patch("timecapsulesmb.services.flash.flash_firmware_bank", side_effect=fake_flash) as flash_mock:
                        with mock.patch("timecapsulesmb.services.flash.dump_remote_bank", side_effect=fake_readback):
                            with mock.patch("timecapsulesmb.services.flash.get_property_int", side_effect=fake_get_property):
                                with FakeAcpDevice().patched() as reboot_mock:
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
        self.assertEqual(reboot_mock.calls, [])
        self.assertEqual(manifest["operation"], "restore")
        self.assertEqual(manifest["flash_plan"]["mode"], "restore")
        self.assertFalse(manifest["flash_plan"]["already_satisfied"])
        self.assertEqual(manifest["write_policy"], "target_bank_restore")
        self.assertTrue(manifest["banks"][0]["would_write"])
        self.assertEqual(manifest["banks"][0]["write_decision"], "primary bank restore from Apple firmware planned")
        self.assertFalse(manifest["banks"][1]["would_write"])
        self.assertEqual(manifest["banks"][1]["write_decision"], "secondary bank left unmodified")
        self.assertEqual(manifest["write_outcome"]["status"], "validated")
        self.assertTrue(manifest["write_outcome"]["write_validated"])
        self.assertEqual(manifest["write_result"]["login_classification"], "stock")
        self.assertEqual(manifest["write_result"]["expected_bank_sha256"], manifest["write_result"]["readback_sha256"])
        self.assertIn("Restore write successful.\x1b[0m The device needs to be manually rebooted.", output.getvalue())
        self.assertNotIn("Reboot not requested", output.getvalue())

    def test_flash_restore_reboot_asks_acpd_over_ssh_not_network_acp(self) -> None:
        output = io.StringIO()
        stock_primary = make_bank(release=b"NetBSD 4.0_STABLE #0: current")
        secondary = make_bank(release=b"NetBSD 4.0_BETA2 #0: old")
        patched_primary = patched_bank(stock_primary, secondary)
        template = firmware_template(stock_primary, product_id=113)
        command_context = FakeCommandContext(compatibility=self.make_supported_netbsd4_stable_compatibility())
        written: dict[str, bytes] = {}

        def fake_flash(_host: str, _password: str, bank_name: str, payload: bytes, **_kwargs: object) -> SimpleNamespace:
            written["bank_name"] = bank_name.encode()
            written["payload"] = payload
            return SimpleNamespace(command=0x03, reply_body=b"")

        def fake_readback(_conn: object, _dev: str, **_kwargs: object) -> bytes:
            reparsed = parse_nested_basebinary(written["payload"])
            end_offset = bank_end_offset(patched_primary)
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
            return bank_checksum(fake_readback(None, ""))

        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp) / "backup"
            template_path = Path(tmp) / "7.8.1.basebinary"
            template_path.write_bytes(template)
            with self.flash_cli(command_context):
                with mock.patch(
                    "timecapsulesmb.services.flash.read_flash_inputs",
                    return_value=flash_inputs(patched_primary, secondary, live_login=PATCHED_LOGIN_SCRIPT),
                ):
                    with mock.patch("timecapsulesmb.services.flash.flash_firmware_bank", side_effect=fake_flash):
                        with mock.patch("timecapsulesmb.services.flash.dump_remote_bank", side_effect=fake_readback):
                            with mock.patch("timecapsulesmb.services.flash.get_property_int", side_effect=fake_get_property):
                                with FakeAcpDevice().patched() as device:
                                    with mock.patch("timecapsulesmb.integrations.acp.set_property_int", side_effect=AssertionError("flash should not set other ACP properties")) as network_acp_set_mock:
                                        with mock.patch("timecapsulesmb.services.flash.reboot_device", wraps=reboot_device) as reboot_spy:
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
            write_outcome = json.loads((backup_dir / "manifest.json").read_text())["write_outcome"]

        self.assertEqual(rc, 0)
        self.assertEqual(write_outcome["status"], "validated")
        self.assertEqual(write_outcome["post_write_action"], "ssh_reboot")
        self.assertTrue(write_outcome["rebooted"])
        self.assertTrue(write_outcome["waited_after_reboot"])
        self.assertEqual(written["bank_name"], b"primary")
        self.assertEqual(device.calls[:3], ["read", "sleep 1", "request"])
        self.assertTrue(device.served_new_boot)
        network_acp_set_mock.assert_not_called()
        self.assertTrue(reboot_spy.call_args.kwargs["wait"])
        # The default limits: 90 s to start the reboot, 600 s to come back.
        self.assertNotIn("start_timeout_seconds", reboot_spy.call_args.kwargs)
        self.assertNotIn("up_timeout_seconds", reboot_spy.call_args.kwargs)
        text = output.getvalue()
        self.assertIn("ACP reboot requested.", text)
        self.assertIn("Device is back online.", text)
        self.assertIn("Run `tcapsule flash --check-apple` to verify Apple stock firmware.", text)
        self.assertNotIn("verify Samba startup", text)
        finished = command_context.finish.call_args.kwargs
        self.assertEqual(finished["result"], "success")

    def run_finish_write(
        self,
        *,
        operation: str,
        reboot: bool,
        no_wait: bool,
        device: FakeAcpDevice | None = None,
        secondary_refresh: bool = False,
    ) -> tuple[int, str, dict[str, object], FakeCommandContext, FakeAcpDevice]:
        output = io.StringIO()
        command_context = FakeCommandContext()
        debug_fields: dict[str, object] = {}
        command_context.add_debug_fields = debug_fields.update  # type: ignore[method-assign]
        command_context.debug_fields = debug_fields  # type: ignore[attr-defined]
        target = SimpleNamespace(connection=SshConnection("root@10.0.0.2", "pw", "-o foo"))
        args = argparse.Namespace(reboot=reboot, no_wait=no_wait)
        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp)
            bundle = SimpleNamespace(
                manifest={"write_outcome": {"status": "validated", "mode": operation}},
                backup_dir=backup_dir,
            )
            with (device or FakeAcpDevice()).patched() as device:
                with mock.patch("timecapsulesmb.integrations.acp.set_property_int", side_effect=AssertionError("flash must not set other ACP properties")):
                    with redirect_stdout(output):
                        rc = cli_flash._finish_write(
                            command_context,
                            args=args,
                            operation=operation,
                            target=target,
                            bundle=bundle,
                            plan=SimpleNamespace(
                                mode=operation,
                                secondary_refresh=SimpleNamespace(primary_login="stock") if secondary_refresh else None,
                            ),
                        )
            saved = json.loads((backup_dir / "manifest.json").read_text())
        return rc, output.getvalue(), saved["write_outcome"], command_context, device

    def test_flash_patch_write_records_manual_power_cycle_without_rebooting(self) -> None:
        rc, text, outcome, command_context, device = self.run_finish_write(
            operation="patch", reboot=False, no_wait=False,
        )

        self.assertEqual(rc, 0)
        self.assertEqual(device.calls, [])
        self.assertEqual(outcome["post_write_action"], "manual_power_cycle")
        self.assertFalse(outcome["reboot_requested"])
        self.assertEqual(outcome["status"], "validated")
        self.assertIn("POWER-CYCLE REQUIRED", text)
        self.assertIn("Patch write successful.", text)
        self.assertEqual(command_context.result, "success")

    def test_flash_secondary_restore_needs_no_reboot_even_when_asked(self) -> None:
        # The device keeps running the primary it booted; restarting adds nothing.
        rc, text, outcome, command_context, device = self.run_finish_write(
            operation="restore", reboot=True, no_wait=False, secondary_refresh=True,
        )

        self.assertEqual(rc, 0)
        self.assertEqual(device.calls, [])
        self.assertEqual(outcome["post_write_action"], "none")
        self.assertFalse(outcome["reboot_requested"])
        self.assertIn("Backup firmware bank restored and verified.", text)
        self.assertIn("Run `tcapsule flash --patch` to install the boot hook.", text)
        self.assertNotIn("POWER-CYCLE REQUIRED", text)
        self.assertNotIn("manually rebooted", text)
        self.assertEqual(command_context.result, "success")

    def test_flash_restore_write_without_reboot_records_manual_reboot(self) -> None:
        rc, text, outcome, _context, device = self.run_finish_write(
            operation="restore", reboot=False, no_wait=False,
        )

        self.assertEqual(rc, 0)
        self.assertEqual(device.calls, [])
        self.assertEqual(outcome["post_write_action"], "manual_reboot")
        self.assertFalse(outcome["reboot_requested"])
        self.assertIn("Restore write successful.", text)
        self.assertNotIn("POWER-CYCLE REQUIRED", text)

    def test_flash_restore_reboot_and_wait_records_reboot(self) -> None:
        rc, text, outcome, command_context, device = self.run_finish_write(
            operation="restore", reboot=True, no_wait=False,
        )

        self.assertEqual(rc, 0)
        self.assertEqual(device.calls.count("request"), 1)
        self.assertTrue(device.served_new_boot)
        self.assertEqual(outcome["post_write_action"], "ssh_reboot")
        self.assertTrue(outcome["reboot_requested"])
        self.assertTrue(outcome["rebooted"])
        self.assertTrue(outcome["waited_after_reboot"])
        self.assertIn("Device returned after reboot.", text)
        self.assertEqual(command_context.debug_fields["reboot_request_strategy"], "network_acp")

    def test_flash_restore_reboot_that_never_goes_down_asks_for_power_cycle(self) -> None:
        rc, text, outcome, command_context, device = self.run_finish_write(
            operation="restore", reboot=True, no_wait=False, device=FakeAcpDevice(reboots=False),
        )

        self.assertEqual(rc, 1)
        self.assertEqual(device.calls.count("request"), 1)
        self.assertEqual(outcome["post_write_action"], "ssh_reboot")
        self.assertTrue(outcome["reboot_requested"])
        self.assertFalse(outcome["rebooted"])
        self.assertIn("Firmware restore write validated, but the device did not restart after the reboot request.", text)
        self.assertIn("POWER-CYCLE REQUIRED", text)
        self.assertNotIn("Device returned after reboot.", text)
        self.assertEqual(command_context.result, "failure")

    def test_flash_restore_reboot_that_never_comes_back_asks_for_power_cycle(self) -> None:
        rc, text, outcome, _context, _device = self.run_finish_write(
            operation="restore", reboot=True, no_wait=False, device=FakeAcpDevice(kernel_after=10_000),
        )

        self.assertEqual(rc, 1)
        self.assertFalse(outcome["rebooted"])
        self.assertIn("Timed out waiting for SSH after reboot.", text)
        self.assertIn("POWER-CYCLE REQUIRED", text)

    def test_flash_restore_reboot_no_wait_skips_reboot_observation(self) -> None:
        rc, text, outcome, _context, device = self.run_finish_write(
            operation="restore", reboot=True, no_wait=True,
        )

        self.assertEqual(rc, 0)
        self.assertEqual(device.calls, ["request"])
        self.assertEqual(outcome["post_write_action"], "ssh_reboot")
        self.assertTrue(outcome["reboot_requested"])
        self.assertFalse(outcome["rebooted"])
        self.assertFalse(outcome["waited_after_reboot"])
        self.assertIn("not waiting for the device", text)

    def test_flash_restore_reboot_no_wait_fails_when_reboot_request_fails(self) -> None:
        rc, text, outcome, command_context, device = self.run_finish_write(
            operation="restore",
            reboot=True,
            no_wait=True,
            device=FakeAcpDevice(request_error=ACPConnectionError("Could not connect to ACP on 10.0.0.2:5009: refused")),
        )

        self.assertEqual(rc, 1)
        self.assertEqual(device.calls, ["request"])
        self.assertTrue(outcome["reboot_requested"])
        self.assertFalse(outcome["rebooted"])
        self.assertIn("ACP reboot request failed: Could not connect to ACP on 10.0.0.2:5009: refused", text)
        self.assertNotIn("not waiting for the device", text)
        self.assertNotIn("POWER-CYCLE REQUIRED", text)
        self.assertEqual(command_context.result, "failure")

    def test_flash_restore_noops_when_active_bank_already_matches_apple(self) -> None:
        output = io.StringIO()
        primary = make_bank(release=b"NetBSD 4.0_STABLE #0: current")
        secondary = make_bank(release=b"NetBSD 4.0_BETA2 #0: old")
        command_context = FakeCommandContext(compatibility=self.make_supported_netbsd4_stable_compatibility())

        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp) / "backup"
            template_path = Path(tmp) / "7.8.1.basebinary"
            template_path.write_bytes(firmware_template(primary, product_id=113))
            with self.flash_cli(command_context):
                with mock.patch(
                    "timecapsulesmb.services.flash.read_flash_inputs",
                    return_value=flash_inputs(primary, secondary),
                ):
                    with mock.patch("timecapsulesmb.services.flash.flash_firmware_bank") as flash_mock:
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
        self.assertEqual(manifest["banks"][0]["write_decision"], "primary bank already matches requested Apple stock firmware; no write needed")
        self.assertEqual(manifest["write_outcome"]["status"], "not_needed")
        self.assertFalse(manifest["write_outcome"]["write_may_have_modified_device"])
        self.assertIn("already matches the requested Apple stock firmware", output.getvalue())

    def test_flash_check_apple_reports_match_without_zopfli_or_write(self) -> None:
        output = io.StringIO()
        primary = make_bank(release=b"NetBSD 4.0_STABLE #0: current")
        secondary = make_bank(release=b"NetBSD 4.0_BETA2 #0: old")
        command_context = FakeCommandContext(compatibility=self.make_supported_netbsd4_stable_compatibility())

        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp) / "backup"
            template_path = Path(tmp) / "7.8.1.basebinary"
            template_path.write_bytes(firmware_template(primary, product_id=113))
            with mock.patch("timecapsulesmb.flash.require_python_module", side_effect=AssertionError("zopfli not needed")):
                with self.flash_cli(command_context):
                    with mock.patch(
                        "timecapsulesmb.services.flash.read_flash_inputs",
                        return_value=flash_inputs(primary, secondary),
                    ):
                        with mock.patch("timecapsulesmb.services.flash.flash_firmware_bank") as flash_mock:
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

    def test_flash_check_apple_checks_all_ambiguous_active_candidates(self) -> None:
        output = io.StringIO()
        primary = make_bank(release=b"NetBSD 4.0_STABLE #0: current")
        secondary = make_bank(release=b"NetBSD 4.0_STABLE #0: current")
        command_context = FakeCommandContext(compatibility=self.make_supported_netbsd4_stable_compatibility())

        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp) / "backup"
            template_path = Path(tmp) / "7.8.1.basebinary"
            template_path.write_bytes(firmware_template(primary, product_id=113))
            with mock.patch("timecapsulesmb.flash.require_python_module", side_effect=AssertionError("zopfli not needed")):
                with self.flash_cli(command_context):
                    with mock.patch(
                        "timecapsulesmb.services.flash.read_flash_inputs",
                        return_value=flash_inputs(primary, secondary),
                    ):
                        with mock.patch("timecapsulesmb.services.flash.flash_firmware_bank") as flash_mock:
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
        self.assertIsNone(manifest["active_bank"])
        self.assertEqual(manifest["active_selection"]["status"], "multiple_candidates")
        self.assertEqual(manifest["flash_plan"]["target_bank"], None)
        self.assertEqual(manifest["flash_plan"]["apple_match_status"], "all_candidates_match")
        self.assertTrue(manifest["flash_plan"]["apple_match"]["matched"])
        self.assertEqual(
            [(result["bank"], result["match"]["matched"]) for result in manifest["flash_plan"]["apple_matches"]],
            [("primary", True), ("secondary", True)],
        )
        self.assertIn("Apple firmware match (primary): matched=True", output.getvalue())
        self.assertIn("Apple firmware match (secondary): matched=True", output.getvalue())

    def test_flash_download_only_validates_firmware_without_write(self) -> None:
        output = io.StringIO()
        primary = make_bank(release=b"NetBSD 4.0_STABLE #0: current")
        secondary = make_bank(release=b"NetBSD 4.0_BETA2 #0: old")
        command_context = FakeCommandContext(compatibility=self.make_supported_netbsd4_stable_compatibility())

        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp) / "backup"
            template_path = Path(tmp) / "7.8.1.basebinary"
            template_path.write_bytes(firmware_template(primary, product_id=113))
            with self.flash_cli(command_context):
                with mock.patch(
                    "timecapsulesmb.services.flash.read_flash_inputs",
                    return_value=flash_inputs(primary, secondary),
                ):
                    with mock.patch("timecapsulesmb.services.flash.flash_firmware_bank") as flash_mock:
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

    def test_flash_download_only_validates_template_without_selected_active_bank(self) -> None:
        output = io.StringIO()
        primary = make_bank(release=b"NetBSD 4.0_STABLE #0: current")
        secondary = make_bank(release=b"NetBSD 4.0_STABLE #0: current")
        command_context = FakeCommandContext(compatibility=self.make_supported_netbsd4_stable_compatibility())

        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp) / "backup"
            template_path = Path(tmp) / "7.8.1.basebinary"
            template_path.write_bytes(firmware_template(primary, product_id=113))
            with self.flash_cli(command_context):
                with mock.patch(
                    "timecapsulesmb.services.flash.read_flash_inputs",
                    return_value=flash_inputs(primary, secondary),
                ):
                    with mock.patch("timecapsulesmb.services.flash.flash_firmware_bank") as flash_mock:
                        with redirect_stdout(output):
                            rc = cli_flash.main([
                                "--download-only",
                                "--firmware-template",
                                str(template_path),
                                "--backup-dir",
                                str(backup_dir),
                            ])
            manifest = json.loads((backup_dir / "manifest.json").read_text())
            payload_exists = Path(manifest["files"]["download_only_basebinary_payload"]).exists()

        self.assertEqual(rc, 0)
        flash_mock.assert_not_called()
        self.assertIsNone(manifest["active_bank"])
        self.assertEqual(manifest["flash_plan"]["target_bank"], None)
        self.assertEqual(manifest["flash_plan"]["apple_match_status"], "all_candidates_match")
        self.assertEqual(manifest["files"]["download_only_basebinary_payload"], str((backup_dir / "download_only.basebinary").resolve()))
        self.assertEqual(manifest["flash_plan"]["payload"]["key_id"], "observed-k30a-78100")
        self.assertTrue(payload_exists)
        self.assertIn("Firmware payload: source=", output.getvalue())

    def test_flash_download_only_reports_mismatch_without_write(self) -> None:
        output = io.StringIO()
        stock_primary = make_bank(release=b"NetBSD 4.0_STABLE #0: current")
        secondary = make_bank(release=b"NetBSD 4.0_BETA2 #0: old")
        patched_primary = patched_bank(stock_primary, secondary)
        command_context = FakeCommandContext(compatibility=self.make_supported_netbsd4_stable_compatibility())

        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp) / "backup"
            template_path = Path(tmp) / "7.8.1.basebinary"
            template_path.write_bytes(firmware_template(stock_primary, product_id=113))
            with self.flash_cli(command_context):
                with mock.patch(
                    "timecapsulesmb.services.flash.read_flash_inputs",
                    return_value=flash_inputs(patched_primary, secondary, live_login=PATCHED_LOGIN_SCRIPT),
                ):
                    with mock.patch("timecapsulesmb.services.flash.flash_firmware_bank") as flash_mock:
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
        primary = make_bank(release=b"NetBSD 4.0_STABLE #0: current")
        secondary = make_bank(release=b"NetBSD 4.0_BETA2 #0: old")
        command_context = FakeCommandContext(compatibility=self.make_supported_netbsd4_stable_compatibility())

        with tempfile.TemporaryDirectory() as tmp:
            template_path = Path(tmp) / "wrong-product.basebinary"
            template_path.write_bytes(firmware_template(primary, product_id=106))
            with self.flash_cli(command_context):
                with mock.patch(
                    "timecapsulesmb.services.flash.read_flash_inputs",
                    return_value=flash_inputs(primary, secondary),
                ):
                    with mock.patch("timecapsulesmb.services.flash.flash_firmware_bank") as flash_mock:
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
        stock_primary = make_bank(release=b"NetBSD 4.0_STABLE #0: current")
        secondary = make_bank(release=b"NetBSD 4.0_BETA2 #0: old")
        patched_primary = patched_bank(stock_primary, secondary)
        command_context = FakeCommandContext(compatibility=self.make_supported_netbsd4_stable_compatibility())

        with tempfile.TemporaryDirectory() as tmp:
            template_path = Path(tmp) / "patched-template.basebinary"
            template_path.write_bytes(firmware_template(patched_primary, product_id=113))
            with self.flash_cli(command_context):
                with mock.patch(
                    "timecapsulesmb.services.flash.read_flash_inputs",
                    return_value=flash_inputs(stock_primary, secondary),
                ):
                    with mock.patch("timecapsulesmb.services.flash.flash_firmware_bank") as flash_mock:
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
        primary = make_bank(release=b"NetBSD 4.0_STABLE #0: current")
        secondary = make_bank(release=b"NetBSD 4.0_BETA2 #0: old")
        command_context = FakeCommandContext(compatibility=self.make_supported_netbsd4_stable_compatibility())

        with tempfile.TemporaryDirectory() as tmp:
            template_path = Path(tmp) / "7.8.1.basebinary"
            template_path.write_bytes(firmware_template(primary, product_id=113))
            with zopfli_available():
                with self.flash_cli(command_context):
                    with mock.patch(
                        "timecapsulesmb.services.flash.read_flash_inputs",
                        return_value=flash_inputs(primary, secondary),
                    ):
                        with mock.patch("timecapsulesmb.services.flash.flash_firmware_bank", return_value=SimpleNamespace(command=0x03, reply_body=b"")):
                            with mock.patch("timecapsulesmb.services.flash.dump_remote_bank", return_value=primary):
                                with FakeAcpDevice().patched() as reboot_mock:
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
        self.assertEqual(reboot_mock.calls, [])
        self.assertIn("read-back firmware bank prefix SHA-256 mismatch", output.getvalue())
        self.assertEqual(command_context.finish.call_args.kwargs["result"], "failure")
        self.assertIn("flash_error_stage=post_write_validation", command_context.finish.call_args.kwargs["error"])
        self.assertIn("read-back firmware bank prefix SHA-256 mismatch", command_context.finish.call_args.kwargs["error"])
        self.assertNotIn("flash_error_stage", command_context.finish.call_args.kwargs)
        self.assertNotIn("flash_error", command_context.finish.call_args.kwargs)

    def test_flash_write_full_bank_mismatch_fails_even_when_prefix_matches(self) -> None:
        output = io.StringIO()
        primary = make_bank(release=b"NetBSD 4.0_STABLE #0: current")
        secondary = make_bank(release=b"NetBSD 4.0_BETA2 #0: old")
        command_context = FakeCommandContext(compatibility=self.make_supported_netbsd4_stable_compatibility())
        written: dict[str, bytes] = {}

        def fake_flash(_host: str, _password: str, bank_name: str, payload: bytes, **_kwargs: object) -> SimpleNamespace:
            written["bank_name"] = bank_name.encode()
            written["payload"] = payload
            return SimpleNamespace(command=0x03, reply_body=b"")

        def fake_readback(_conn: object, _dev: str, **_kwargs: object) -> bytes:
            reparsed = parse_nested_basebinary(written["payload"])
            end_offset = bank_end_offset(primary)
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
            template_path.write_bytes(firmware_template(primary, product_id=113))
            with zopfli_available():
                with self.flash_cli(command_context):
                    with mock.patch(
                        "timecapsulesmb.services.flash.read_flash_inputs",
                        return_value=flash_inputs(primary, secondary),
                    ):
                        with mock.patch("timecapsulesmb.services.flash.flash_firmware_bank", side_effect=fake_flash) as flash_mock:
                            with mock.patch("timecapsulesmb.services.flash.dump_remote_bank", side_effect=fake_readback):
                                with mock.patch("timecapsulesmb.services.flash.get_property_int", side_effect=AssertionError("ACP checksum should not be read after full-bank mismatch")) as acp_mock:
                                    with FakeAcpDevice().patched() as reboot_mock:
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
        self.assertEqual(reboot_mock.calls, [])
        self.assertIn("read-back firmware bank SHA-256 mismatch", output.getvalue())
        self.assertEqual(manifest["write_outcome"]["status"], "failed")
        self.assertTrue(manifest["write_outcome"]["write_may_have_modified_device"])
        self.assertIn("read-back firmware bank SHA-256 mismatch", manifest["write_outcome"]["message"])

    def test_flash_write_readback_ssh_error_is_reported_without_traceback(self) -> None:
        output = io.StringIO()
        primary = make_bank(release=b"NetBSD 4.0_STABLE #0: current")
        secondary = make_bank(release=b"NetBSD 4.0_BETA2 #0: old")
        command_context = FakeCommandContext(compatibility=self.make_supported_netbsd4_stable_compatibility())

        with tempfile.TemporaryDirectory() as tmp:
            template_path = Path(tmp) / "7.8.1.basebinary"
            template_path.write_bytes(firmware_template(primary, product_id=113))
            with zopfli_available():
                with self.flash_cli(command_context):
                    with mock.patch(
                        "timecapsulesmb.services.flash.read_flash_inputs",
                        return_value=flash_inputs(primary, secondary),
                    ):
                        with mock.patch("timecapsulesmb.services.flash.flash_firmware_bank", return_value=SimpleNamespace(command=0x03, reply_body=b"")) as flash_mock:
                            with mock.patch("timecapsulesmb.services.flash.dump_remote_bank", side_effect=SshError("ssh command failed with rc=255")):
                                with mock.patch("timecapsulesmb.services.flash.get_property_int", side_effect=AssertionError("ACP checksum should not be read after read-back failure")) as acp_mock:
                                    with FakeAcpDevice().patched() as reboot_mock:
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
        self.assertEqual(reboot_mock.calls, [])
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
        primary = make_bank(release=b"NetBSD 4.0_STABLE #0: current")
        secondary = make_bank(release=b"NetBSD 4.0_BETA2 #0: old")
        command_context = FakeCommandContext(compatibility=self.make_supported_netbsd4_stable_compatibility())

        with tempfile.TemporaryDirectory() as tmp:
            template_path = Path(tmp) / "7.8.1.basebinary"
            template_path.write_bytes(firmware_template(primary, product_id=113))
            with zopfli_available():
                with self.flash_cli(command_context):
                    with mock.patch(
                        "timecapsulesmb.services.flash.read_flash_inputs",
                        return_value=flash_inputs(primary, secondary),
                    ):
                        with mock.patch("timecapsulesmb.services.flash.flash_firmware_bank", side_effect=ACPAuthError("ACP command failed with error_code -0x14")):
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
        primary = make_bank(release=b"NetBSD 4.0_STABLE #0: current")
        secondary = make_bank(release=b"NetBSD 4.0_BETA2 #0: old")
        command_context = FakeCommandContext(compatibility=self.make_supported_netbsd4_stable_compatibility())
        written: dict[str, bytes] = {}

        def fake_flash(_host: str, _password: str, bank_name: str, payload: bytes, **_kwargs: object) -> SimpleNamespace:
            written["bank_name"] = bank_name.encode()
            written["payload"] = payload
            return SimpleNamespace(command=0x03, reply_body=b"")

        def fake_readback(_conn: object, _dev: str, **_kwargs: object) -> bytes:
            reparsed = parse_nested_basebinary(written["payload"])
            end_offset = bank_end_offset(primary)
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
            template_path.write_bytes(firmware_template(primary, product_id=113))
            with zopfli_available():
                with self.flash_cli(command_context):
                    with mock.patch(
                        "timecapsulesmb.services.flash.read_flash_inputs",
                        return_value=flash_inputs(primary, secondary),
                    ):
                        with mock.patch("timecapsulesmb.services.flash.flash_firmware_bank", side_effect=fake_flash) as flash_mock:
                            with mock.patch("timecapsulesmb.services.flash.dump_remote_bank", side_effect=fake_readback):
                                with mock.patch("timecapsulesmb.services.flash.get_property_int", side_effect=fake_get_property):
                                    with FakeAcpDevice().patched() as reboot_mock:
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
        self.assertEqual(reboot_mock.calls, [])
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
        primary = make_bank(release=b"NetBSD 4.0_STABLE #0: current", login=unknown_login)
        secondary = make_bank(release=b"NetBSD 4.0_BETA2 #0: old")
        command_context = FakeCommandContext(compatibility=self.make_supported_netbsd4_stable_compatibility())

        with tempfile.TemporaryDirectory() as tmp:
            with zopfli_available():
                with self.flash_cli(command_context):
                    with mock.patch(
                        "timecapsulesmb.services.flash.read_flash_inputs",
                        return_value=flash_inputs(primary, secondary, live_login=unknown_login),
                    ):
                        with mock.patch("timecapsulesmb.services.flash.flash_firmware_bank") as flash_mock:
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
        primary = make_bank(release=b"NetBSD 4.0_STABLE #0: current", login=unknown_login)
        secondary = make_bank(release=b"NetBSD 4.0_BETA2 #0: old")
        command_context = FakeCommandContext(compatibility=self.make_supported_netbsd4_stable_compatibility())

        with tempfile.TemporaryDirectory() as tmp:
            with zopfli_available():
                with self.flash_cli(command_context):
                    with mock.patch(
                        "timecapsulesmb.services.flash.read_flash_inputs",
                        return_value=flash_inputs(primary, secondary, live_login=unknown_login),
                    ):
                        with mock.patch("timecapsulesmb.cli.flash.confirm", side_effect=AssertionError("confirm should not be called")) as confirm_mock:
                            with mock.patch("timecapsulesmb.services.flash.flash_firmware_bank") as flash_mock:
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
        with zopfli_available():
            with self.flash_cli(command_context):
                with mock.patch("timecapsulesmb.services.flash.read_flash_inputs") as read_mock:
                    with self.assertRaises(SystemExit) as raised:
                        cli_flash.main(["--read-only"])

        message = str(raised.exception)
        self.assertIn("flash is only supported for NetBSD4", message)
        self.assertIn("https://github.com/jamesyc/TimeCapsuleSMB/issues/160", message)
        read_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
