from __future__ import annotations

import plistlib
import struct
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock
from pathlib import Path
import zlib


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

import timecapsulesmb.flash as flash_module
from timecapsulesmb import apple_firmware
from timecapsulesmb.apple_firmware import APPLE_FIRMWARE_CATALOG_URL
from timecapsulesmb.basebinary import BasebinaryKey, compose_basebinary, parse_nested_basebinary
from timecapsulesmb.flash_payloads import build_patch_payload_for_bank, find_apple_firmware_match
from timecapsulesmb.services import flash as flash_service
from timecapsulesmb.services.flash import default_flash_backup_root
from timecapsulesmb.flash import (
    PATCHED_LOGIN_SCRIPT,
    STOCK_LOGIN_NETBSD4_DUMMY,
    FlashAnalysisError,
    analyze_bank,
    build_patch,
    classify_login,
    find_gzip_member,
    find_footer,
    sha256_hex,
    inspect_flash_banks,
    inspection_to_jsonable,
    write_decision_for_bank,
)
from timecapsulesmb.integrations.acp import ACPAuthError
from timecapsulesmb.flash_payloads import AcpFlashPayload
from timecapsulesmb.flash_workflow import RESTORE_PRIMARY_AMBIGUOUS_WARNING, require_primary_patch_ready
from timecapsulesmb.transport.ssh import SshConnection, SshError

from tests.flash_fixtures import FastFakeZopfliGzip, bank_checksum, firmware_template, make_bank, zopfli_available


class FlashAnalysisTests(unittest.TestCase):
    def setUp(self) -> None:
        self._zopfli_patch = mock.patch("timecapsulesmb.flash._load_zopfli_gzip", return_value=FastFakeZopfliGzip)
        self._zopfli_patch.start()

    def tearDown(self) -> None:
        self._zopfli_patch.stop()

    def test_service_read_flash_inputs_normalizes_syap_and_reads_login_last(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")
        primary = b"primary"
        secondary = b"secondary"
        login = b"login"
        logs: list[str] = []
        with mock.patch("timecapsulesmb.services.flash.run_ssh_capture_bytes", side_effect=[primary, secondary, login]) as capture:
            with mock.patch("timecapsulesmb.services.flash.get_property_int", side_effect=[11, 22, 113]) as get_property:
                inputs = flash_service.read_flash_inputs(connection, acp_host="10.0.0.2", password="pw", log=logs.append)

        self.assertEqual(inputs.primary, primary)
        self.assertEqual(inputs.secondary, secondary)
        self.assertEqual(inputs.cks1, 11)
        self.assertEqual(inputs.cks2, 22)
        self.assertEqual(inputs.syap, "113")
        self.assertEqual(inputs.live_login, login)
        self.assertEqual(capture.call_count, 3)
        self.assertEqual([call.args[2] for call in get_property.mock_calls], ["cks1", "cks2", "syAP"])
        self.assertIn("Reading live /etc/rc.d/LOGIN...", logs)

    def test_service_read_flash_inputs_stops_before_login_when_acp_fails(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")
        with mock.patch("timecapsulesmb.services.flash.run_ssh_capture_bytes", side_effect=[b"primary", b"secondary"]) as capture:
            with mock.patch("timecapsulesmb.services.flash.get_property_int", side_effect=ACPAuthError("bad password")):
                with self.assertRaises(FlashAnalysisError) as raised:
                    flash_service.read_flash_inputs(connection, acp_host="10.0.0.2", password="pw")

        self.assertEqual(capture.call_count, 2)
        self.assertIn("ACP property cks1 read failed", str(raised.exception))

    def test_service_read_flash_inputs_stops_before_acp_when_secondary_read_fails(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")
        with mock.patch("timecapsulesmb.services.flash.run_ssh_capture_bytes", side_effect=[b"primary", SshError("rc=255")]):
            with mock.patch("timecapsulesmb.services.flash.get_property_int") as get_property:
                with self.assertRaises(SshError):
                    flash_service.read_flash_inputs(connection, acp_host="10.0.0.2", password="pw")

        get_property.assert_not_called()

    def test_service_validation_dump_preserves_validation_and_ssh_read_logs(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")
        logs: list[str] = []
        with mock.patch("timecapsulesmb.services.flash.run_ssh_capture_bytes", return_value=b"bank") as capture:
            payload = flash_service.dump_remote_bank_for_validation(connection, "/dev/rflash0.raw", log=logs.append)

        self.assertEqual(payload, b"bank")
        capture.assert_called_once_with(connection, "/bin/dd if=/dev/rflash0.raw bs=65536 2>/dev/null", timeout=180)
        self.assertEqual(
            logs,
            [
                "Reading back written firmware bank from /dev/rflash0.raw...",
                "SSH: /bin/dd if=/dev/rflash0.raw bs=65536 2>/dev/null",
            ],
        )

    def test_find_footer_and_gzip_member_ignore_false_gzip_signature(self) -> None:
        bank = make_bank(extra_gzip_magic=b"\x1f\x8b\x08bad")
        footer = find_footer(bank)
        member = find_gzip_member(bank, footer)

        self.assertGreater(member.offset, 0)
        self.assertIn(STOCK_LOGIN_NETBSD4_DUMMY, member.decompressed)

    def test_find_footer_rejects_short_buffers(self) -> None:
        with self.assertRaises(FlashAnalysisError) as raised:
            find_footer(b"short")

        self.assertIn("expected exactly one valid footer, found 0", str(raised.exception))

    def test_find_gzip_member_skips_reserved_flag_candidates_before_decompressing(self) -> None:
        bank = make_bank(extra_gzip_magic=b"\x1f\x8b\x08\xe0bad")
        footer = find_footer(bank)

        with mock.patch("timecapsulesmb.flash._decompress_gzip_member", wraps=flash_module._decompress_gzip_member) as decompress_mock:
            member = find_gzip_member(bank, footer)

        self.assertIn(STOCK_LOGIN_NETBSD4_DUMMY, member.decompressed)
        self.assertEqual(decompress_mock.call_count, 1)

    def test_find_footer_ignores_empty_prefix_padding_false_positive(self) -> None:
        bank = bytearray(make_bank())
        expected = find_footer(bytes(bank))
        bank[expected.offset - 12 : expected.offset - 4] = b"\x00\x00\x00\x01\x00\x00\x00\x00"

        footer = find_footer(bytes(bank))

        self.assertEqual(footer, expected)

    def test_find_footer_caches_adler32_by_candidate_end_offset(self) -> None:
        bank = bytearray(make_bank())
        footer = find_footer(bytes(bank))
        false_candidate = struct.pack(">II", 0, footer.end_offset)
        bank[footer.offset - 16 : footer.offset - 8] = false_candidate
        bank[footer.offset - 8 : footer.offset] = false_candidate
        original_adler32 = zlib.adler32
        calls_by_length: dict[int, int] = {}

        def counting_adler32(data: bytes | memoryview) -> int:
            calls_by_length[len(data)] = calls_by_length.get(len(data), 0) + 1
            return original_adler32(data)

        with mock.patch("timecapsulesmb.flash.zlib.adler32", side_effect=counting_adler32):
            found = find_footer(bytes(bank))

        self.assertEqual(found.offset, footer.offset)
        self.assertEqual(calls_by_length[footer.end_offset], 1)

    def test_classify_stock_login_as_patchable(self) -> None:
        login = classify_login(b"prefix" + STOCK_LOGIN_NETBSD4_DUMMY + b"\x00")

        self.assertEqual(login.classification, "stock")
        self.assertTrue(login.patchable)
        self.assertEqual(login.length, len(STOCK_LOGIN_NETBSD4_DUMMY))

    def test_classify_already_patched_login(self) -> None:
        login = classify_login(b"prefix" + PATCHED_LOGIN_SCRIPT + b"\x00")

        self.assertEqual(login.classification, "already_patched")
        self.assertFalse(login.patchable)

    def test_classify_mixed_stock_and_patched_login_as_unknown(self) -> None:
        login = classify_login(STOCK_LOGIN_NETBSD4_DUMMY + b"\x00" + PATCHED_LOGIN_SCRIPT)

        self.assertEqual(login.classification, "unknown")
        self.assertFalse(login.patchable)
        self.assertEqual(login.match_count, 2)

    def test_classify_duplicate_stock_login_as_unknown(self) -> None:
        login = classify_login(STOCK_LOGIN_NETBSD4_DUMMY + b"\x00" + STOCK_LOGIN_NETBSD4_DUMMY)

        self.assertEqual(login.classification, "unknown")
        self.assertFalse(login.patchable)
        self.assertEqual(login.match_count, 2)

    def test_unknown_login_refuses_patch(self) -> None:
        login = classify_login(b"#!/bin/sh\n# PROVIDE: LOGIN\nexit 0\n")

        self.assertEqual(login.classification, "unknown")
        self.assertFalse(login.patchable)

    def test_analyze_bank_builds_deterministic_patch_hash(self) -> None:
        bank = make_bank()
        checksum = find_footer(bank).checksum
        first = analyze_bank(name="primary", device="/dev/rflash0.raw", data=bank, acp_checksum=checksum, os_release="4.0")
        second = analyze_bank(name="primary", device="/dev/rflash0.raw", data=bank, acp_checksum=checksum, os_release="4.0")

        self.assertEqual(first.login.classification, "stock")
        self.assertIsNotNone(first.patch)
        self.assertIsNotNone(second.patch)
        assert first.patch is not None
        assert second.patch is not None
        self.assertEqual(first.patch.target_bank_sha256, second.patch.target_bank_sha256)
        self.assertEqual(first.patch.compression_method, "zopfli-gzip")
        self.assertGreaterEqual(first.patch.changed_range_start, first.login.offset or 0)
        self.assertLessEqual(first.patch.changed_range_end, (first.login.offset or 0) + (first.login.length or 0))

    def test_zopfli_gzip_patches_requested_primary_bank_only_when_both_fit(self) -> None:
        primary = make_bank(release=b"NetBSD 4.0_STABLE #0: current")
        secondary = make_bank(release=b"NetBSD 4.0_BETA2 #0: old")

        inspection = inspect_flash_banks(
            primary_data=primary,
            secondary_data=secondary,
            cks1=bank_checksum(primary),
            cks2=bank_checksum(secondary),
            os_release="4.0_STABLE",
            build_primary_patch_candidate=True,
        )
        analysis = inspection.strict_analysis
        assert analysis is not None

        self.assertEqual(analysis.active_bank, "primary")
        self.assertIsNotNone(analysis.primary.patch)
        self.assertIsNone(analysis.secondary.patch)
        assert analysis.primary.patch is not None
        self.assertEqual(analysis.primary.patch.compression_method, "zopfli-gzip")
        self.assertEqual(len(analysis.primary.patch.target_bank), len(primary))

    def test_inspect_flash_banks_reuses_primary_bank_metadata_for_patch(self) -> None:
        primary = make_bank(release=b"NetBSD 4.0_STABLE #0: current")
        secondary = make_bank(release=b"NetBSD 4.0_BETA2 #0: old")
        cks1 = bank_checksum(primary)
        cks2 = bank_checksum(secondary)

        with mock.patch("timecapsulesmb.flash.find_footer", wraps=find_footer) as footer_mock:
            with mock.patch("timecapsulesmb.flash.find_gzip_member", wraps=find_gzip_member) as gzip_mock:
                inspection = inspect_flash_banks(
                    primary_data=primary,
                    secondary_data=secondary,
                    cks1=cks1,
                    cks2=cks2,
                    os_release="4.0_STABLE",
                    build_primary_patch_candidate=True,
                )
                analysis = inspection.strict_analysis
                assert analysis is not None

        self.assertEqual(analysis.active_bank, "primary")
        self.assertIsNotNone(analysis.primary.patch)
        self.assertEqual(footer_mock.call_count, 2)
        self.assertEqual(gzip_mock.call_count, 2)

    def test_missing_zopfli_reports_bootstrap_message(self) -> None:
        bank = make_bank()
        footer = find_footer(bank)
        member = find_gzip_member(bank, footer)
        login = classify_login(member.decompressed)

        with mock.patch("timecapsulesmb.flash._load_zopfli_gzip", side_effect=ModuleNotFoundError("zopfli")):
            with self.assertRaises(FlashAnalysisError) as raised:
                build_patch(bank, footer, member, login)

        self.assertIn("Python package zopfli is required", str(raised.exception))
        self.assertIn("bootstrap", str(raised.exception))

    def test_patch_errors_are_reported_for_primary_bank_only_when_zopfli_gzip_is_too_large(self) -> None:
        primary = make_bank(release=b"NetBSD 4.0_STABLE #0: current")
        secondary = make_bank(release=b"NetBSD 4.0_BETA2 #0: old")

        class TooLargeZopfliGzip:
            @staticmethod
            def compress(_data: bytes, **_kwargs) -> bytes:
                return b"z" * 100000

        with mock.patch("timecapsulesmb.flash._load_zopfli_gzip", return_value=TooLargeZopfliGzip):
            inspection = inspect_flash_banks(
                primary_data=primary,
                secondary_data=secondary,
                cks1=bank_checksum(primary),
                cks2=bank_checksum(secondary),
                os_release="4.0_STABLE",
                build_primary_patch_candidate=True,
            )
            analysis = inspection.strict_analysis
            assert analysis is not None

        self.assertIsNone(analysis.primary.patch)
        self.assertIsNone(analysis.secondary.patch)
        self.assertIn("zopfli-gzip=100000", analysis.primary.patch_error or "")
        self.assertIsNone(analysis.secondary.patch_error)

    def test_analyze_already_patched_bank_does_not_build_patch(self) -> None:
        bank = make_bank(login=PATCHED_LOGIN_SCRIPT)
        checksum = find_footer(bank).checksum
        analysis = analyze_bank(name="primary", device="/dev/rflash0.raw", data=bank, acp_checksum=checksum, os_release="4.0")

        self.assertEqual(analysis.login.classification, "already_patched")
        self.assertIsNone(analysis.patch)
        self.assertIsNone(analysis.patch_error)

    def test_inspect_flash_banks_reports_active_already_patched_as_noop(self) -> None:
        primary = make_bank(login=PATCHED_LOGIN_SCRIPT, release=b"NetBSD 4.0_STABLE #0: current")
        secondary = make_bank(release=b"NetBSD 4.0_BETA2 #0: old")

        inspection = inspect_flash_banks(
            primary_data=primary,
            secondary_data=secondary,
            cks1=bank_checksum(primary),
            cks2=bank_checksum(secondary),
            os_release="4.0_STABLE",
            build_primary_patch_candidate=True,
        )
        analysis = inspection.strict_analysis
        assert analysis is not None

        self.assertEqual(analysis.active_bank, "primary")
        self.assertEqual(analysis.primary.login.classification, "already_patched")
        self.assertIsNone(analysis.primary.patch)
        self.assertEqual(write_decision_for_bank(analysis, analysis.primary), "active bank already patched; no patched output written")
        self.assertEqual(write_decision_for_bank(analysis, analysis.secondary), "inactive bank left unmodified")

    def test_inspect_flash_banks_refuses_active_unknown_login(self) -> None:
        primary = make_bank(login=b"#!/bin/sh\n# PROVIDE: LOGIN\nexit 0\n", release=b"NetBSD 4.0_STABLE #0: current")
        secondary = make_bank(release=b"NetBSD 4.0_BETA2 #0: old")

        inspection = inspect_flash_banks(
            primary_data=primary,
            secondary_data=secondary,
            cks1=bank_checksum(primary),
            cks2=bank_checksum(secondary),
            os_release="4.0_STABLE",
            build_primary_patch_candidate=True,
        )
        analysis = inspection.strict_analysis
        assert analysis is not None

        self.assertEqual(analysis.active_bank, "primary")
        self.assertEqual(analysis.primary.login.classification, "unknown")
        self.assertIsNone(analysis.primary.patch)
        self.assertEqual(write_decision_for_bank(analysis, analysis.primary), "active bank patch refused: LOGIN classification unknown")

    def test_analyze_bank_marks_acp_checksum_mismatch(self) -> None:
        bank = make_bank()
        analysis = analyze_bank(name="primary", device="/dev/rflash0.raw", data=bank, acp_checksum=0, os_release="4.0")

        self.assertFalse(analysis.acp_checksum_matches)
        self.assertFalse(analysis.valid_for_active_selection)

    def test_acp_checksum_mismatch_prevents_active_bank_selection(self) -> None:
        primary = make_bank(release=b"NetBSD 4.0_STABLE #0: current")
        secondary = make_bank(release=b"NetBSD 4.0_BETA2 #0: old")

        inspection = inspect_flash_banks(
            primary_data=primary,
            secondary_data=secondary,
            cks1=0,
            cks2=bank_checksum(secondary),
            os_release="4.0_STABLE",
        )
        analysis = inspection.strict_analysis
        assert analysis is not None

        self.assertIsNone(analysis.active_bank)
        self.assertIsNone(analysis.primary.patch)
        self.assertIsNone(analysis.secondary.patch)
        self.assertFalse(analysis.primary.valid_for_active_selection)
        self.assertEqual(analysis.active_selection.status, "no_candidates")
        self.assertIn("cks1 mismatch", analysis.primary.active_selection_failures[0])
        # The patch preflight refuses a bank whose ACP checksum does not validate.
        with self.assertRaises(FlashAnalysisError):
            require_primary_patch_ready(inspection)

    def test_patch_build_failure_is_reported_per_bank(self) -> None:
        bank = make_bank()
        checksum = find_footer(bank).checksum
        class TooLargeZopfliGzip:
            @staticmethod
            def compress(_data: bytes, **_kwargs) -> bytes:
                return b"z" * len(bank)

        with mock.patch("timecapsulesmb.flash._load_zopfli_gzip", return_value=TooLargeZopfliGzip):
            analysis = analyze_bank(name="primary", device="/dev/rflash0.raw", data=bank, acp_checksum=checksum, os_release="4.0")

        self.assertIsNone(analysis.patch)
        self.assertIn("zopfli-gzip", analysis.patch_error or "")

    def test_bad_footer_raises(self) -> None:
        bank = bytearray(make_bank())
        footer = find_footer(bytes(bank))
        bank[footer.offset] ^= 0x01

        with self.assertRaises(FlashAnalysisError):
            find_footer(bytes(bank))

    def test_active_bank_requires_single_matching_running_identity(self) -> None:
        primary = make_bank(release=b"NetBSD 4.0_STABLE #0: current")
        secondary = make_bank(release=b"NetBSD 4.0_BETA2 #0: old")
        cks1 = find_footer(primary).checksum
        cks2 = find_footer(secondary).checksum

        inspection = inspect_flash_banks(
            primary_data=primary,
            secondary_data=secondary,
            cks1=cks1,
            cks2=cks2,
            os_release="4.0_STABLE",
        )
        analysis = inspection.strict_analysis
        assert analysis is not None

        self.assertEqual(analysis.active_bank, "primary")

    def test_ambiguous_active_bank_is_unknown(self) -> None:
        primary = make_bank()
        secondary = make_bank()

        inspection = inspect_flash_banks(
            primary_data=primary,
            secondary_data=secondary,
            cks1=find_footer(primary).checksum,
            cks2=find_footer(secondary).checksum,
            os_release="4.0",
        )
        analysis = inspection.strict_analysis
        assert analysis is not None

        self.assertIsNone(analysis.active_bank)
        self.assertEqual(analysis.active_selection.status, "multiple_candidates")
        self.assertEqual(analysis.active_selection.candidates, ("primary", "secondary"))

    def test_inspect_flash_banks_marks_both_active_candidates_without_selecting_one(self) -> None:
        primary = make_bank()
        secondary = make_bank()

        inspection = inspect_flash_banks(
            primary_data=primary,
            secondary_data=secondary,
            cks1=find_footer(primary).checksum,
            cks2=find_footer(secondary).checksum,
            os_release="4.0",
            build_primary_patch_candidate=True,
        )

        self.assertIsNone(inspection.active_bank)
        self.assertEqual(inspection.active_selection.status, "multiple_candidates")
        self.assertEqual(inspection.active_selection.candidates, ("primary", "secondary"))
        self.assertTrue(inspection.primary.backup_valid)
        self.assertTrue(inspection.secondary.backup_valid)
        self.assertTrue(inspection.primary.active_candidate)
        self.assertTrue(inspection.secondary.active_candidate)
        self.assertIsNotNone(inspection.primary.analysis)
        assert inspection.primary.analysis is not None
        self.assertIsNotNone(inspection.primary.analysis.patch)

    def test_live_login_disambiguates_multiple_active_candidates(self) -> None:
        primary = make_bank(login=PATCHED_LOGIN_SCRIPT, release=b"NetBSD 4.0_STABLE #0: current")
        secondary = make_bank(login=STOCK_LOGIN_NETBSD4_DUMMY, release=b"NetBSD 4.0_STABLE #0: current")

        inspection = inspect_flash_banks(
            primary_data=primary,
            secondary_data=secondary,
            cks1=find_footer(primary).checksum,
            cks2=find_footer(secondary).checksum,
            os_release="4.0_STABLE",
            live_login=PATCHED_LOGIN_SCRIPT,
        )

        self.assertEqual(inspection.active_bank, "primary")
        self.assertEqual(inspection.active_selection.status, "selected")
        self.assertEqual(inspection.active_selection.candidates, ("primary",))
        self.assertEqual(inspection.active_selection.selected_by, "live_login")
        self.assertTrue(inspection.primary.active_candidate)
        self.assertTrue(inspection.secondary.active_candidate)
        self.assertTrue(inspection.primary.live_login_match)
        self.assertFalse(inspection.secondary.live_login_match)
        payload = inspection_to_jsonable(inspection)
        self.assertEqual(payload["banks"][0]["live_login_match"], True)
        self.assertEqual(payload["banks"][1]["live_login_match"], False)

    def test_live_login_keeps_ambiguous_when_multiple_candidates_match(self) -> None:
        primary = make_bank(release=b"NetBSD 4.0_STABLE #0: current")
        secondary = make_bank(release=b"NetBSD 4.0_STABLE #0: current")

        inspection = inspect_flash_banks(
            primary_data=primary,
            secondary_data=secondary,
            cks1=find_footer(primary).checksum,
            cks2=find_footer(secondary).checksum,
            os_release="4.0_STABLE",
            live_login=STOCK_LOGIN_NETBSD4_DUMMY,
        )

        self.assertIsNone(inspection.active_bank)
        self.assertEqual(inspection.active_selection.status, "multiple_candidates")
        self.assertEqual(inspection.active_selection.candidates, ("primary", "secondary"))
        self.assertTrue(inspection.primary.live_login_match)
        self.assertTrue(inspection.secondary.live_login_match)

    def test_plan_from_backup_reuses_saved_live_login_match_evidence(self) -> None:
        primary = make_bank(login=PATCHED_LOGIN_SCRIPT, release=b"NetBSD 4.0_STABLE #0: current")
        secondary = make_bank(login=STOCK_LOGIN_NETBSD4_DUMMY, release=b"NetBSD 4.0_STABLE #0: current")
        primary_footer = find_footer(primary)
        secondary_footer = find_footer(secondary)
        payload = AcpFlashPayload(
            data=b"payload",
            expected_prefix=primary[: primary_footer.end_offset],
            expected_login_classification="stock",
            template_source="test",
            template_path=Path("/tmp/template.basebinary"),
            template_product_id="116",
            template_version="7.8.1",
            template_sha256="template-sha",
            payload_sha256="payload-sha",
            key_id="test-key",
            inner_model=116,
            inner_version=0x00070801,
            inner_payload_size=primary_footer.end_offset,
        )

        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp)
            flash_service.save_flash_banks(backup_dir=backup_dir, primary=primary, secondary=secondary)
            flash_service.save_flash_manifest(backup_dir=backup_dir, manifest={
                "operation": "read_only",
                "backup_dir": str(backup_dir),
                "syap": "116",
                "os_release": "4.0_STABLE",
                "files": {
                    "primary": str(backup_dir / "primary.raw"),
                    "secondary": str(backup_dir / "secondary.raw"),
                    "manifest": str(backup_dir / "manifest.json"),
                },
                "banks": [
                    {
                        "name": "primary",
                        "sha256": flash_module.sha256_hex(primary),
                        "acp_checksum": f"0x{primary_footer.checksum:08x}",
                        "live_login_match": True,
                    },
                    {
                        "name": "secondary",
                        "sha256": flash_module.sha256_hex(secondary),
                        "acp_checksum": f"0x{secondary_footer.checksum:08x}",
                        "live_login_match": False,
                    },
                ],
            })

            with mock.patch("timecapsulesmb.flash_workflow.build_restore_payload_for_bank", return_value=payload):
                bundle, plan = flash_service.plan_flash_from_backup(
                    backup_dir=backup_dir,
                    operation="restore",
                    force=False,
                    firmware_template=None,
                    firmware_version=None,
                )

        self.assertIsNotNone(plan)
        assert plan is not None
        assert plan.target_bank is not None
        self.assertEqual(plan.target_bank.name, "primary")
        self.assertEqual(bundle.manifest["active_bank"], "primary")
        self.assertEqual(bundle.manifest["active_selection"]["selected_by"], "live_login")
        self.assertEqual(bundle.inspection.active_selection.selected_by, "live_login")

    def test_plan_from_backup_ignores_saved_live_login_match_when_raw_bank_changed(self) -> None:
        primary = make_bank(login=PATCHED_LOGIN_SCRIPT, release=b"NetBSD 4.0_STABLE #0: current")
        changed_primary = make_bank(login=PATCHED_LOGIN_SCRIPT, release=b"NetBSD 4.0_STABLE #0: changed")
        secondary = make_bank(login=STOCK_LOGIN_NETBSD4_DUMMY, release=b"NetBSD 4.0_STABLE #0: current")
        changed_primary_footer = find_footer(changed_primary)
        secondary_footer = find_footer(secondary)
        payload = AcpFlashPayload(
            data=b"payload",
            expected_prefix=changed_primary[: changed_primary_footer.end_offset],
            expected_login_classification="stock",
            template_source="test",
            template_path=Path("/tmp/template.basebinary"),
            template_product_id="116",
            template_version="7.8.1",
            template_sha256="template-sha",
            payload_sha256="payload-sha",
            key_id="test-key",
            inner_model=116,
            inner_version=0x00070801,
            inner_payload_size=changed_primary_footer.end_offset,
        )

        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp)
            flash_service.save_flash_banks(backup_dir=backup_dir, primary=changed_primary, secondary=secondary)
            flash_service.save_flash_manifest(backup_dir=backup_dir, manifest={
                "operation": "read_only",
                "backup_dir": str(backup_dir),
                "syap": "116",
                "os_release": "4.0_STABLE",
                "files": {
                    "primary": str(backup_dir / "primary.raw"),
                    "secondary": str(backup_dir / "secondary.raw"),
                    "manifest": str(backup_dir / "manifest.json"),
                },
                "banks": [
                    {
                        "name": "primary",
                        "sha256": flash_module.sha256_hex(primary),
                        "acp_checksum": f"0x{changed_primary_footer.checksum:08x}",
                        "live_login_match": True,
                    },
                    {
                        "name": "secondary",
                        "sha256": flash_module.sha256_hex(secondary),
                        "acp_checksum": f"0x{secondary_footer.checksum:08x}",
                        "live_login_match": False,
                    },
                ],
            })

            with mock.patch("timecapsulesmb.flash_workflow.build_restore_payload_for_bank", return_value=payload):
                bundle, plan = flash_service.plan_flash_from_backup(
                    backup_dir=backup_dir,
                    operation="restore",
                    force=False,
                    firmware_template=None,
                    firmware_version=None,
                )

        self.assertIsNotNone(plan)
        assert plan is not None
        assert plan.target_bank is not None
        self.assertEqual(plan.target_bank.name, "primary")
        self.assertEqual(plan.warnings, (RESTORE_PRIMARY_AMBIGUOUS_WARNING,))
        self.assertIsNone(bundle.manifest["active_bank"])
        self.assertEqual(bundle.manifest["active_selection"]["status"], "multiple_candidates")
        self.assertIsNone(bundle.inspection.active_selection.selected_by)

    def test_inspect_flash_banks_keeps_invalid_secondary_status_without_raising(self) -> None:
        primary = make_bank(release=b"NetBSD 4.0_STABLE #0: current")
        secondary = make_bank(release=b"NetBSD 4.0_STABLE #0: current")
        corrupt_secondary = b"not a valid firmware bank"

        inspection = inspect_flash_banks(
            primary_data=primary,
            secondary_data=corrupt_secondary,
            cks1=find_footer(primary).checksum,
            cks2=find_footer(secondary).checksum,
            os_release="4.0_STABLE",
        )
        payload = inspection_to_jsonable(inspection)

        self.assertEqual(inspection.active_bank, "primary")
        self.assertTrue(inspection.primary.backup_valid)
        self.assertFalse(inspection.secondary.backup_valid)
        self.assertIn("expected exactly one valid footer", inspection.secondary.error or "")
        self.assertEqual(payload["banks"][1]["footer"], None)
        self.assertIn("expected exactly one valid footer", payload["banks"][1]["analysis_error"])


LIVE_LOGIN_PRIMARY = make_bank(login=PATCHED_LOGIN_SCRIPT, release=b"NetBSD 4.0_STABLE #0: current")
LIVE_LOGIN_SECONDARY = make_bank(login=STOCK_LOGIN_NETBSD4_DUMMY, release=b"NetBSD 4.0_STABLE #0: current")


def save_live_login_backup(backup_dir: Path, *, log=None, stage=None):
    """Save a backup whose two active banks are told apart only by the live LOGIN."""
    inputs = flash_service.FlashInputs(
        primary=LIVE_LOGIN_PRIMARY,
        secondary=LIVE_LOGIN_SECONDARY,
        cks1=bank_checksum(LIVE_LOGIN_PRIMARY),
        cks2=bank_checksum(LIVE_LOGIN_SECONDARY),
        syap="116",
        live_login=PATCHED_LOGIN_SCRIPT,
    )
    target = SimpleNamespace(acp_host="10.0.0.2", compatibility=SimpleNamespace(os_release="4.0_STABLE"))
    return flash_service.save_flash_backup(target=target, inputs=inputs, backup_dir=backup_dir, log=log, stage=stage)


class FlashBackupServiceTests(unittest.TestCase):
    """The backup a run saves is what planning reads back, for the CLI and the app."""

    PRIMARY = LIVE_LOGIN_PRIMARY

    def save_backup(self, backup_dir: Path):
        stages: list[str] = []
        logs: list[str] = []
        with mock.patch("timecapsulesmb.services.flash.inspect_flash_banks", wraps=inspect_flash_banks) as inspect:
            bundle = save_live_login_backup(backup_dir, log=logs.append, stage=stages.append)
        return bundle, stages, logs, inspect

    def restore_payload(self) -> AcpFlashPayload:
        footer = find_footer(self.PRIMARY)
        return AcpFlashPayload(
            data=b"payload",
            expected_prefix=self.PRIMARY[: footer.end_offset],
            expected_login_classification="stock",
            template_source="test",
            template_path=Path("/tmp/template.basebinary"),
            template_product_id="116",
            template_version="7.8.1",
            template_sha256="template-sha",
            payload_sha256="payload-sha",
            key_id="test-key",
            inner_model=116,
            inner_version=0x00070801,
            inner_payload_size=footer.end_offset,
        )

    def plan_restore(self, backup_dir: Path):
        with mock.patch("timecapsulesmb.flash_workflow.build_restore_payload_for_bank", return_value=self.restore_payload()):
            return flash_service.plan_flash_from_backup(
                backup_dir=backup_dir,
                operation="restore",
                force=False,
                firmware_template=None,
                firmware_version=None,
            )

    def test_save_writes_a_read_only_manifest_with_live_login_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp) / "backup"
            bundle, stages, logs, inspect = self.save_backup(backup_dir)
            saved = flash_service.load_flash_manifest(backup_dir)
            raw_primary = (backup_dir / "primary.raw").read_bytes()

        self.assertEqual(stages, ["save_raw_backup", "analyze_flash", "save_backup"])
        self.assertEqual(logs, [
            f"Saving raw flash backup to {backup_dir.resolve()}...",
            "Analyzing flash banks...",
            "Writing flash manifest...",
        ])
        self.assertEqual(raw_primary, self.PRIMARY)
        self.assertEqual(saved, bundle.manifest)
        self.assertEqual(saved["operation"], "read_only")
        self.assertEqual(saved["syap"], "116")
        self.assertEqual([bank["live_login_match"] for bank in saved["banks"]], [True, False])
        self.assertEqual(saved["active_selection"]["selected_by"], "live_login")
        self.assertEqual([bank["patch"] for bank in saved["banks"]], [None, None])
        self.assertEqual({bank["write_decision"] for bank in saved["banks"]}, {"backup only; no patch candidate built"})
        self.assertFalse(inspect.call_args.kwargs.get("build_primary_patch_candidate", False))

    def test_planning_from_the_saved_backup_keeps_the_live_login_bank_choice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp) / "backup"
            self.save_backup(backup_dir)
            bundle, plan = self.plan_restore(backup_dir)

        assert plan is not None and plan.target_bank is not None
        self.assertEqual(plan.target_bank.name, "primary")
        self.assertEqual(bundle.inspection.active_selection.selected_by, "live_login")
        self.assertEqual(bundle.manifest["operation"], "restore")
        self.assertNotIn("flash_plan_error", bundle.manifest)

    def test_plan_failure_is_recorded_and_replaces_an_earlier_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp) / "backup"
            self.save_backup(backup_dir)
            self.plan_restore(backup_dir)
            self.assertIn("flash_plan", flash_service.load_flash_manifest(backup_dir))
            with mock.patch(
                "timecapsulesmb.services.flash.plan_from_operation",
                side_effect=FlashAnalysisError("refusing to restore"),
            ):
                with self.assertRaisesRegex(FlashAnalysisError, "refusing to restore"):
                    flash_service.plan_flash_from_backup(
                        backup_dir=backup_dir,
                        operation="check_apple",
                        force=False,
                        firmware_template=None,
                        firmware_version="7.8.1",
                    )
            saved = flash_service.load_flash_manifest(backup_dir)

        self.assertEqual(saved["flash_plan_error"], {"stage": "plan_flash", "message": "refusing to restore"})
        self.assertNotIn("flash_plan", saved)
        self.assertEqual(saved["operation"], "check_apple")
        self.assertEqual(saved["flash_plan_params"]["firmware_version"], "7.8.1")

    def test_a_later_successful_plan_clears_the_recorded_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp) / "backup"
            self.save_backup(backup_dir)
            with mock.patch("timecapsulesmb.services.flash.plan_from_operation", side_effect=FlashAnalysisError("no")):
                with self.assertRaises(FlashAnalysisError):
                    self.plan_restore(backup_dir)
            self.plan_restore(backup_dir)
            saved = flash_service.load_flash_manifest(backup_dir)

        self.assertNotIn("flash_plan_error", saved)
        self.assertEqual(saved["flash_plan"]["mode"], "restore")

    def test_failure_before_planning_leaves_the_manifest_alone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp) / "backup"
            bundle, _stages, _logs, _inspect = self.save_backup(backup_dir)
            (backup_dir / "manifest.json").write_text(
                (backup_dir / "manifest.json").read_text().replace('"syap": "116"', '"syap": ""'),
            )
            with self.assertRaisesRegex(FlashAnalysisError, "missing syAP"):
                self.plan_restore(backup_dir)
            saved = flash_service.load_flash_manifest(backup_dir)

        self.assertNotIn("flash_plan_error", saved)
        self.assertEqual(saved["operation"], "read_only")

    def test_flash_write_waits_600_seconds_for_acpd_to_finish_the_bank(self) -> None:
        # ACPd replies only after it has erased, written and verified the bank;
        # field writes took up to 200 s and a 300 s wait cut some replies off.
        target = SimpleNamespace(
            connection=SshConnection("root@10.0.0.2", "pw", "-o foo"),
            acp_host="10.0.0.2",
            compatibility=SimpleNamespace(os_release="4.0_STABLE"),
        )
        plan = SimpleNamespace(target_bank=SimpleNamespace(name="primary"), payload=object())
        with mock.patch("timecapsulesmb.services.flash.record_write_outcome") as record:
            with mock.patch("timecapsulesmb.services.flash.write_and_validate_plan", return_value={"bank": "primary"}) as write:
                result = flash_service.write_flash_plan(target=target, bundle=object(), plan=plan)

        self.assertEqual(result, {"bank": "primary"})
        self.assertEqual(write.call_args.kwargs["timeout"], 600)
        self.assertEqual(
            [call.kwargs["status"] for call in record.call_args_list],
            ["attempting", "validated"],
        )

    def test_flash_live_login_read_uses_binary_capture(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")
        payload = b"#!/bin/sh\n\xff"
        with mock.patch("timecapsulesmb.services.flash.run_ssh_capture_bytes", return_value=payload) as capture_mock:
            self.assertEqual(flash_service.read_live_login(connection), payload)
        capture_mock.assert_called_once_with(
            connection,
            "/bin/dd if=/etc/rc.d/LOGIN bs=4096 2>/dev/null",
            timeout=30,
        )

    def test_flash_backup_dir_sanitizes_dot_only_path_parts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backup_dir = flash_service.build_flash_backup_dir(base_dir=None, host="..", syap=".")

        self.assertEqual(backup_dir.parent, default_flash_backup_root())
        self.assertIn("-device-syAPdevice", backup_dir.name)
        self.assertNotIn("..", backup_dir.parts)
        self.assertNotIn(".", backup_dir.parts)

        explicit_dir = flash_service.build_flash_backup_dir(base_dir=root / ".." / "chosen", host="..", syap=".")
        self.assertEqual(explicit_dir, (root / ".." / "chosen").resolve())



class FlashPayloadTests(unittest.TestCase):
    """ACP flash payloads built from Apple firmware templates that match the live bank."""

    def test_build_acp_flash_payload_for_primary_bank_uses_matching_template(self) -> None:
        primary = make_bank(release=b"NetBSD 4.0_STABLE #0: current")
        secondary = make_bank(release=b"NetBSD 4.0_BETA2 #0: old")
        with tempfile.TemporaryDirectory() as tmp:
            template_path = Path(tmp) / "7.8.1.basebinary"
            template_path.write_bytes(firmware_template(primary, product_id=113))
            with zopfli_available():
                inspection = inspect_flash_banks(
                    primary_data=primary,
                    secondary_data=secondary,
                    cks1=bank_checksum(primary),
                    cks2=bank_checksum(secondary),
                    os_release="4.0_STABLE",
                    build_primary_patch_candidate=True,
                )
            active = require_primary_patch_ready(inspection)

            payload = build_patch_payload_for_bank(
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
        self.assertEqual(payload.template_sha256, sha256_hex(firmware_template(primary, product_id=113)))

    def test_build_acp_flash_payload_auto_downloads_matching_template_by_syap(self) -> None:
        primary = make_bank(release=b"NetBSD 4.0_STABLE #0: current")
        secondary = make_bank(release=b"NetBSD 4.0_BETA2 #0: old")
        template = firmware_template(primary, product_id=113)
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
            if url == APPLE_FIRMWARE_CATALOG_URL:
                return catalog
            self.assertEqual(url, "http://example.invalid/113/7.8.1.basebinary")
            return template

        with tempfile.TemporaryDirectory() as tmp:
            with zopfli_available():
                inspection = inspect_flash_banks(
                    primary_data=primary,
                    secondary_data=secondary,
                    cks1=bank_checksum(primary),
                    cks2=bank_checksum(secondary),
                    os_release="4.0_STABLE",
                    build_primary_patch_candidate=True,
                )
            active = require_primary_patch_ready(inspection)

            with mock.patch("timecapsulesmb.apple_firmware.download_url", side_effect=fake_download) as download_mock:
                payload = build_patch_payload_for_bank(
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
        primary = make_bank(release=b"NetBSD 4.0_STABLE #0: current")
        secondary = make_bank(release=b"NetBSD 4.0_BETA2 #0: old")
        template = firmware_template(primary, product_id=113)
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
            if url == APPLE_FIRMWARE_CATALOG_URL:
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
            with zopfli_available():
                inspection = inspect_flash_banks(
                    primary_data=primary,
                    secondary_data=secondary,
                    cks1=bank_checksum(primary),
                    cks2=bank_checksum(secondary),
                    os_release="4.0_STABLE",
                    build_primary_patch_candidate=True,
                )
            active = require_primary_patch_ready(inspection)

            with mock.patch("timecapsulesmb.apple_firmware.download_url", side_effect=fake_download):
                payload = build_patch_payload_for_bank(
                    active,
                    syap="113",
                    firmware_template=None,
                    cache_dir=cache_dir,
                )
            refreshed_cache = cached_path.read_bytes()

        self.assertEqual(calls, [APPLE_FIRMWARE_CATALOG_URL, template_url])
        self.assertEqual(refreshed_cache, template)
        self.assertEqual(payload.template_sha256, sha256_hex(template))

    def test_check_apple_redownloads_corrupt_cached_template(self) -> None:
        primary = make_bank(release=b"NetBSD 4.0_STABLE #0: current")
        secondary = make_bank(release=b"NetBSD 4.0_BETA2 #0: old")
        template = firmware_template(primary, product_id=113)
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
            if url == APPLE_FIRMWARE_CATALOG_URL:
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
            with zopfli_available():
                inspection = inspect_flash_banks(
                    primary_data=primary,
                    secondary_data=secondary,
                    cks1=bank_checksum(primary),
                    cks2=bank_checksum(secondary),
                    os_release="4.0_STABLE",
                    build_primary_patch_candidate=True,
                )
            active = require_primary_patch_ready(inspection)

            with mock.patch("timecapsulesmb.apple_firmware.download_url", side_effect=fake_download):
                match = find_apple_firmware_match(
                    active,
                    syap="113",
                    firmware_template=None,
                    cache_dir=cache_dir,
                )
            refreshed_cache = cached_path.read_bytes()

        self.assertEqual(calls, [APPLE_FIRMWARE_CATALOG_URL, template_url])
        self.assertEqual(refreshed_cache, template)
        self.assertTrue(match.matched)
        self.assertEqual(match.template_sha256, sha256_hex(template))

    def test_build_acp_flash_payload_refuses_template_that_does_not_match_live_bank(self) -> None:
        primary = make_bank(release=b"NetBSD 4.0_STABLE #0: current")
        secondary = make_bank(release=b"NetBSD 4.0_BETA2 #0: old")
        with tempfile.TemporaryDirectory() as tmp:
            template_path = Path(tmp) / "7.8.1.basebinary"
            template = parse_nested_basebinary(firmware_template(primary, product_id=113))
            modified_payload = bytes([template.inner.payload[0] ^ 0x01]) + template.inner.payload[1:]
            modified_inner = compose_basebinary(template.inner.header, modified_payload, key=template.inner.key)
            template_path.write_bytes(compose_basebinary(template.outer.header, modified_inner, key=template.outer.key))
            with zopfli_available():
                inspection = inspect_flash_banks(
                    primary_data=primary,
                    secondary_data=secondary,
                    cks1=bank_checksum(primary),
                    cks2=bank_checksum(secondary),
                    os_release="4.0_STABLE",
                    build_primary_patch_candidate=True,
                )
            active = require_primary_patch_ready(inspection)

            with self.assertRaises(FlashAnalysisError) as raised:
                build_patch_payload_for_bank(
                    active,
                    syap="113",
                    firmware_template=template_path,
                    cache_dir=Path(tmp) / "cache",
                )

        self.assertIn("does not match the live target bank", str(raised.exception))

    def test_build_acp_flash_payload_refuses_unknown_key_with_issue_url(self) -> None:
        primary = make_bank(release=b"NetBSD 4.0_STABLE #0: current")
        secondary = make_bank(release=b"NetBSD 4.0_BETA2 #0: old")
        unknown_key = BasebinaryKey.from_hex("unknown-test", "00112233445566778899aabbccddeeff")
        with tempfile.TemporaryDirectory() as tmp:
            template_path = Path(tmp) / "7.8.1.basebinary"
            template_path.write_bytes(firmware_template(primary, product_id=113, key=unknown_key))
            with zopfli_available():
                inspection = inspect_flash_banks(
                    primary_data=primary,
                    secondary_data=secondary,
                    cks1=bank_checksum(primary),
                    cks2=bank_checksum(secondary),
                    os_release="4.0_STABLE",
                    build_primary_patch_candidate=True,
                )
            active = require_primary_patch_ready(inspection)

            with self.assertRaises(FlashAnalysisError) as raised:
                build_patch_payload_for_bank(
                    active,
                    syap="113",
                    firmware_template=template_path,
                    cache_dir=Path(tmp) / "cache",
                )

        self.assertIn("do not have firmware encryption keys", str(raised.exception))
        self.assertIn("https://github.com/jamesyc/TimeCapsuleSMB/issues", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
