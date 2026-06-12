from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

from timecapsulesmb.flash import (
    ActiveSelectionInfo,
    BankAnalysis,
    BankInspection,
    FlashAnalysis,
    FlashAnalysisError,
    FlashInspection,
    FooterInfo,
    GzipMemberInfo,
    LoginInfo,
    PatchBuildInfo,
    sha256_hex,
)
from timecapsulesmb.flash_payloads import AcpFlashPayload, AppleFirmwareMatch
from timecapsulesmb.flash_workflow import (
    FlashPlan,
    expected_bank_after_write,
    plan_check_apple,
    plan_patch_primary,
    plan_restore_apple,
    require_active_and_inactive_valid,
    require_patch_ready,
    write_and_validate_plan,
)
from timecapsulesmb.integrations.acp import ACPError, ACPFlashResult
from timecapsulesmb.transport.ssh import SshConnection


def make_bank(
    name: str = "primary",
    *,
    classification: str = "stock",
    footer_valid: bool = True,
    acp_checksum_matches: bool | None = True,
    patch: PatchBuildInfo | None = None,
    patch_error: str | None = None,
    data: bytes | None = None,
) -> BankAnalysis:
    data = data or b"abcdefghABCDEFGH"
    return BankAnalysis(
        name=name,
        device=f"/dev/{name}",
        data=data,
        sha256=sha256_hex(data),
        size=len(data),
        footer=FooterInfo(offset=12, checksum=0x12345678, end_offset=8),
        footer_valid=footer_valid,
        acp_checksum=0x12345678,
        acp_checksum_matches=acp_checksum_matches,
        gzip_member=GzipMemberInfo(offset=0, consumed_length=8, decompressed=b"login"),
        decompressed_sha256=sha256_hex(b"login"),
        login=LoginInfo(classification=classification, offset=0, length=5, sha256=sha256_hex(b"login"), match_count=1),
        kernel_identity_match=True,
        kernel_identity_detail="matched",
        active_selection_failures=(),
        patch=patch,
        patch_error=patch_error,
    )


def make_patch(bank: BankAnalysis) -> PatchBuildInfo:
    return PatchBuildInfo(
        target_bank_sha256=bank.sha256,
        patched_image_sha256=sha256_hex(b"patched"),
        patched_gzip_length=7,
        compression_method="test",
        changed_range_start=0,
        changed_range_end=7,
        footer_checksum=0x11111111,
        target_bank=b"patched",
    )


def make_analysis(
    *,
    active_bank: str | None = "primary",
    primary: BankAnalysis | None = None,
    secondary: BankAnalysis | None = None,
) -> FlashAnalysis:
    primary = primary or make_bank("primary")
    secondary = secondary or make_bank("secondary")
    candidates = () if active_bank is None else (active_bank,)
    return FlashAnalysis(
        primary=primary,
        secondary=secondary,
        active_bank=active_bank,
        active_selection=ActiveSelectionInfo("single_candidate" if active_bank else "no_candidates", candidates, "test"),
    )


def make_inspection(
    *,
    primary: BankAnalysis | None = None,
    secondary: BankAnalysis | None = None,
    primary_backup_valid: bool = True,
    secondary_backup_valid: bool = True,
    primary_active_candidate: bool = True,
) -> FlashInspection:
    primary = primary or make_bank("primary", patch=make_patch(make_bank("primary")))
    secondary = secondary or make_bank("secondary")
    return FlashInspection(
        primary=BankInspection(
            "primary",
            "/dev/primary",
            primary.size,
            primary.sha256,
            primary.acp_checksum,
            primary,
            primary_backup_valid,
            () if primary_backup_valid else ("bad primary",),
            primary_active_candidate,
            () if primary_active_candidate else ("not active",),
            None,
            None,
        ),
        secondary=BankInspection(
            "secondary",
            "/dev/secondary",
            secondary.size,
            secondary.sha256,
            secondary.acp_checksum,
            secondary,
            secondary_backup_valid,
            () if secondary_backup_valid else ("bad secondary",),
            False,
            ("inactive",),
            None,
            None,
        ),
        active_selection=ActiveSelectionInfo("single_candidate", ("primary",), "test"),
    )


def make_payload(*, expected_prefix: bytes = b"PATCHED!") -> AcpFlashPayload:
    return AcpFlashPayload(
        data=b"payload",
        expected_prefix=expected_prefix,
        expected_login_classification="already_patched",
        template_source="template",
        template_path=Path("/tmp/template.basebinary"),
        template_product_id="116",
        template_version="7.8.1",
        template_sha256=sha256_hex(b"template"),
        payload_sha256=sha256_hex(b"payload"),
        key_id="test-key",
        inner_model=116,
        inner_version=0x00070801,
        inner_payload_size=len(expected_prefix),
    )


class FlashWorkflowTests(unittest.TestCase):
    def test_require_active_and_inactive_valid_rejects_missing_active(self) -> None:
        with self.assertRaisesRegex(FlashAnalysisError, "no firmware bank passed active selection"):
            require_active_and_inactive_valid(make_analysis(active_bank=None))

    def test_require_active_and_inactive_valid_rejects_bad_inactive_backup(self) -> None:
        secondary = make_bank("secondary", acp_checksum_matches=False)

        with self.assertRaisesRegex(FlashAnalysisError, "inactive firmware bank backup did not validate"):
            require_active_and_inactive_valid(make_analysis(secondary=secondary))

    def test_require_patch_ready_covers_login_and_patch_states(self) -> None:
        patched = make_bank("primary", classification="already_patched")
        self.assertIs(require_patch_ready(make_analysis(primary=patched)), patched)

        with self.assertRaisesRegex(FlashAnalysisError, "LOGIN classification unknown"):
            require_patch_ready(make_analysis(primary=make_bank("primary", classification="unknown")))

        with self.assertRaisesRegex(FlashAnalysisError, "no patch candidate: too large"):
            require_patch_ready(make_analysis(primary=make_bank("primary", patch=None, patch_error="too large")))

    def test_plan_patch_primary_respects_force_warnings_and_noop(self) -> None:
        patched = make_bank("primary", classification="already_patched")
        plan = plan_patch_primary(
            make_inspection(primary=patched, secondary_backup_valid=False, primary_active_candidate=False),
            force=True,
            syap="116",
            firmware_template=None,
        )

        self.assertTrue(plan.already_satisfied)
        self.assertFalse(plan.write_requested)
        self.assertEqual(plan.warnings, (
            "patch forced despite one or more invalid backup banks",
            "patch forced even though the primary bank did not pass active-candidate checks",
        ))

    def test_plan_patch_primary_builds_payload_for_stock_primary(self) -> None:
        base = make_bank("primary")
        primary = make_bank("primary", patch=make_patch(base))
        payload = make_payload()

        with mock.patch("timecapsulesmb.flash_workflow.build_patch_payload_for_bank", return_value=payload) as build:
            plan = plan_patch_primary(make_inspection(primary=primary), syap="116", firmware_template=None)

        build.assert_called_once_with(primary, syap="116", firmware_template=None, firmware_version=None, cache_dir=None)
        self.assertTrue(plan.write_requested)
        self.assertIs(plan.payload, payload)

    def test_restore_and_check_plans_cover_already_satisfied_states(self) -> None:
        payload = make_payload(expected_prefix=b"abcdefgh")
        match = AppleFirmwareMatch(True, "template", None, "116", "7.8.1", "sha", "inner", 8, "key", 116, 0x70801)
        analysis = make_analysis()

        with mock.patch("timecapsulesmb.flash_workflow.build_restore_payload_for_active_bank", return_value=payload):
            restore_plan = plan_restore_apple(analysis, syap="116", firmware_template=None)
        with mock.patch("timecapsulesmb.flash_workflow.find_apple_firmware_match", return_value=match):
            check_plan = plan_check_apple(make_inspection(), syap="116", firmware_template=None)

        self.assertTrue(restore_plan.already_satisfied)
        self.assertFalse(restore_plan.write_requested)
        self.assertTrue(check_plan.already_satisfied)
        self.assertFalse(check_plan.write_requested)

    def write_plan(self, *, payload: AcpFlashPayload | None = None, readback: bytes | None = None) -> tuple[FlashPlan, bytes]:
        active = make_bank("primary")
        payload = payload or make_payload()
        expected, _checksum = expected_bank_after_write(active, payload)
        plan = FlashPlan("patch", active, payload, None, False)
        return plan, readback if readback is not None else expected

    def test_write_and_validate_plan_rejects_missing_payload(self) -> None:
        with self.assertRaisesRegex(FlashAnalysisError, "no write payload"):
            write_and_validate_plan(
                connection=SshConnection("root@10.0.0.2", "pw", "-o test"),
                acp_host="10.0.0.2",
                plan=FlashPlan("patch", make_bank("primary"), None, None, False),
                os_release="4.0",
                flash_firmware_bank_func=mock.Mock(),
                dump_remote_bank_func=mock.Mock(),
                get_property_int_func=mock.Mock(),
                timeout=30,
            )

    def test_write_and_validate_plan_reports_write_and_readback_failures(self) -> None:
        plan, readback = self.write_plan()
        connection = SshConnection("root@10.0.0.2", "pw", "-o test")

        with self.assertRaisesRegex(FlashAnalysisError, "ACP flash command failed"):
            write_and_validate_plan(
                connection=connection,
                acp_host="10.0.0.2",
                plan=plan,
                os_release="4.0",
                flash_firmware_bank_func=mock.Mock(side_effect=ACPError("bad write")),
                dump_remote_bank_func=mock.Mock(),
                get_property_int_func=mock.Mock(),
                timeout=30,
            )

        with self.assertRaisesRegex(FlashAnalysisError, "prefix SHA-256 mismatch"):
            write_and_validate_plan(
                connection=connection,
                acp_host="10.0.0.2",
                plan=plan,
                os_release="4.0",
                flash_firmware_bank_func=mock.Mock(return_value=ACPFlashResult(0x26, b"ok")),
                dump_remote_bank_func=mock.Mock(return_value=b"WRONG!!!" + readback[8:]),
                get_property_int_func=mock.Mock(),
                timeout=30,
            )

        with self.assertRaisesRegex(FlashAnalysisError, "firmware bank SHA-256 mismatch"):
            write_and_validate_plan(
                connection=connection,
                acp_host="10.0.0.2",
                plan=plan,
                os_release="4.0",
                flash_firmware_bank_func=mock.Mock(return_value=ACPFlashResult(0x26, b"ok")),
                dump_remote_bank_func=mock.Mock(return_value=readback + b"extra"),
                get_property_int_func=mock.Mock(),
                timeout=30,
            )

    def test_write_and_validate_plan_checks_acp_checksum_footer_and_login(self) -> None:
        plan, readback = self.write_plan()
        connection = SshConnection("root@10.0.0.2", "pw", "-o test")

        cases = [
            (mock.Mock(side_effect=ACPError("no cks")), mock.Mock(), "ACP checksum property cks1 read failed"),
            (mock.Mock(return_value=0x11111111), mock.Mock(return_value=SimpleNamespace(footer_valid=False)), "footer checksum is invalid"),
            (
                mock.Mock(return_value=0x11111111),
                mock.Mock(return_value=SimpleNamespace(footer_valid=True, acp_checksum_matches=False)),
                "ACP cks1 does not match",
            ),
            (
                mock.Mock(return_value=0x11111111),
                mock.Mock(return_value=SimpleNamespace(
                    footer_valid=True,
                    acp_checksum_matches=True,
                    login=SimpleNamespace(classification="stock"),
                    footer=SimpleNamespace(checksum=0x11111111),
                )),
                "LOGIN classification is stock",
            ),
        ]
        for get_property, analyze, expected_error in cases:
            with self.subTest(expected_error=expected_error):
                with mock.patch("timecapsulesmb.flash_workflow.analyze_bank", analyze):
                    with self.assertRaisesRegex(FlashAnalysisError, expected_error):
                        write_and_validate_plan(
                            connection=connection,
                            acp_host="10.0.0.2",
                            plan=plan,
                            os_release="4.0",
                            flash_firmware_bank_func=mock.Mock(return_value=ACPFlashResult(0x26, b"ok")),
                            dump_remote_bank_func=mock.Mock(return_value=readback),
                            get_property_int_func=get_property,
                            timeout=30,
                        )

    def test_write_and_validate_plan_returns_summary_on_success(self) -> None:
        plan, readback = self.write_plan()
        readback_analysis = SimpleNamespace(
            footer_valid=True,
            acp_checksum_matches=True,
            login=SimpleNamespace(classification="already_patched"),
            footer=SimpleNamespace(checksum=0x11111111),
        )

        with mock.patch("timecapsulesmb.flash_workflow.analyze_bank", return_value=readback_analysis):
            result = write_and_validate_plan(
                connection=SshConnection("root@10.0.0.2", "pw", "-o test"),
                acp_host="10.0.0.2",
                plan=plan,
                os_release="4.0",
                flash_firmware_bank_func=mock.Mock(return_value=ACPFlashResult(0x26, b"reply")),
                dump_remote_bank_func=mock.Mock(return_value=readback),
                get_property_int_func=mock.Mock(return_value=0x11111111),
                timeout=30,
            )

        self.assertEqual(result["mode"], "patch")
        self.assertEqual(result["bank"], "primary")
        self.assertEqual(result["login_classification"], "already_patched")


if __name__ == "__main__":
    unittest.main()
