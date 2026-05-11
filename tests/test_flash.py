from __future__ import annotations

import struct
import sys
import unittest
from unittest import mock
from pathlib import Path
import zlib


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from timecapsulesmb.flash import (
    PATCHED_LOGIN_SCRIPT,
    STOCK_LOGIN_NETBSD4_DUMMY,
    FlashAnalysisError,
    analyze_bank,
    analyze_flash_banks,
    build_patch,
    classify_login,
    find_gzip_member,
    find_footer,
)


def make_gzip_member(data: bytes) -> bytes:
    compressor = zlib.compressobj(level=1, wbits=16 + zlib.MAX_WBITS)
    return compressor.compress(data) + compressor.flush()


def make_bank(
    *,
    login: bytes = STOCK_LOGIN_NETBSD4_DUMMY,
    release: bytes = b"NetBSD 4.0 #0: test",
    extra_gzip_magic: bytes = b"",
) -> bytes:
    decompressed = b"kernel " + release + b"\n" + (b"A" * 128) + login + (b"\x00" * 64)
    gz = make_gzip_member(decompressed)
    prefix = b"BOOT" + extra_gzip_magic + (b"\x00" * 16)
    body = prefix + gz + b"\x00\x00"
    end_offset = len(body)
    checksum = zlib.adler32(body) & 0xFFFFFFFF
    return body + (b"\xff" * 16) + struct.pack(">II", checksum, end_offset) + (b"\xff" * 24)


def bank_checksum(bank: bytes) -> int:
    return find_footer(bank).checksum


class FastFakeZopfliGzip:
    @staticmethod
    def compress(data: bytes, **_kwargs) -> bytes:
        return make_gzip_member(data)


class FlashAnalysisTests(unittest.TestCase):
    def setUp(self) -> None:
        self._zopfli_patch = mock.patch("timecapsulesmb.flash._load_zopfli_gzip", return_value=FastFakeZopfliGzip)
        self._zopfli_patch.start()

    def tearDown(self) -> None:
        self._zopfli_patch.stop()

    def test_find_footer_and_gzip_member_ignore_false_gzip_signature(self) -> None:
        bank = make_bank(extra_gzip_magic=b"\x1f\x8b\x08bad")
        footer = find_footer(bank)
        member = find_gzip_member(bank, footer)

        self.assertGreater(member.offset, 0)
        self.assertIn(STOCK_LOGIN_NETBSD4_DUMMY, member.decompressed)

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

    def test_zopfli_gzip_patches_primary_and_secondary_when_both_fit(self) -> None:
        primary = make_bank(release=b"NetBSD 4.0_STABLE #0: current")
        secondary = make_bank(release=b"NetBSD 4.0_BETA2 #0: old")

        analysis = analyze_flash_banks(
            primary_data=primary,
            secondary_data=secondary,
            cks1=bank_checksum(primary),
            cks2=bank_checksum(secondary),
            os_release="4.0_STABLE",
        )

        self.assertEqual(analysis.active_bank, "primary")
        self.assertIsNotNone(analysis.primary.patch)
        self.assertIsNotNone(analysis.secondary.patch)
        assert analysis.primary.patch is not None
        assert analysis.secondary.patch is not None
        self.assertEqual(analysis.primary.patch.compression_method, "zopfli-gzip")
        self.assertEqual(analysis.secondary.patch.compression_method, "zopfli-gzip")
        self.assertEqual(len(analysis.primary.patch.target_bank), len(primary))
        self.assertEqual(len(analysis.secondary.patch.target_bank), len(secondary))

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

    def test_patch_errors_are_reported_for_primary_and_secondary_when_zopfli_gzip_is_too_large(self) -> None:
        primary = make_bank(release=b"NetBSD 4.0_STABLE #0: current")
        secondary = make_bank(release=b"NetBSD 4.0_BETA2 #0: old")

        class TooLargeZopfliGzip:
            @staticmethod
            def compress(_data: bytes, **_kwargs) -> bytes:
                return b"z" * 100000

        with mock.patch("timecapsulesmb.flash._load_zopfli_gzip", return_value=TooLargeZopfliGzip):
            analysis = analyze_flash_banks(
                primary_data=primary,
                secondary_data=secondary,
                cks1=bank_checksum(primary),
                cks2=bank_checksum(secondary),
                os_release="4.0_STABLE",
            )

        self.assertIsNone(analysis.primary.patch)
        self.assertIsNone(analysis.secondary.patch)
        self.assertIn("zopfli-gzip=100000", analysis.primary.patch_error or "")
        self.assertIn("zopfli-gzip=100000", analysis.secondary.patch_error or "")

    def test_analyze_already_patched_bank_does_not_build_patch(self) -> None:
        bank = make_bank(login=PATCHED_LOGIN_SCRIPT)
        checksum = find_footer(bank).checksum
        analysis = analyze_bank(name="primary", device="/dev/rflash0.raw", data=bank, acp_checksum=checksum, os_release="4.0")

        self.assertEqual(analysis.login.classification, "already_patched")
        self.assertIsNone(analysis.patch)
        self.assertIsNone(analysis.patch_error)

    def test_analyze_bank_marks_acp_checksum_mismatch(self) -> None:
        bank = make_bank()
        analysis = analyze_bank(name="primary", device="/dev/rflash0.raw", data=bank, acp_checksum=0, os_release="4.0")

        self.assertFalse(analysis.acp_checksum_matches)
        self.assertFalse(analysis.valid_for_active_selection)

    def test_acp_checksum_mismatch_prevents_active_bank_selection(self) -> None:
        primary = make_bank(release=b"NetBSD 4.0_STABLE #0: current")
        secondary = make_bank(release=b"NetBSD 4.0_BETA2 #0: old")

        analysis = analyze_flash_banks(
            primary_data=primary,
            secondary_data=secondary,
            cks1=0,
            cks2=bank_checksum(secondary),
            os_release="4.0_STABLE",
        )

        self.assertIsNone(analysis.active_bank)
        self.assertFalse(analysis.primary.valid_for_active_selection)

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

        analysis = analyze_flash_banks(
            primary_data=primary,
            secondary_data=secondary,
            cks1=cks1,
            cks2=cks2,
            os_release="4.0_STABLE",
        )

        self.assertEqual(analysis.active_bank, "primary")

    def test_ambiguous_active_bank_is_unknown(self) -> None:
        primary = make_bank()
        secondary = make_bank()

        analysis = analyze_flash_banks(
            primary_data=primary,
            secondary_data=secondary,
            cks1=find_footer(primary).checksum,
            cks2=find_footer(secondary).checksum,
            os_release="4.0",
        )

        self.assertIsNone(analysis.active_bank)


if __name__ == "__main__":
    unittest.main()
