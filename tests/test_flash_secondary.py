"""Restore rewriting an invalid secondary firmware bank.

The banks are full 7 MiB banks laid out as Apple lays them out, and the device
is tests.flash_fixtures.FakeFlashDevice, which runs the write command against
whichever devices it names and computes cks1/cks2 as ACPd does.
"""
from __future__ import annotations

import functools
import io
import struct
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.cli_support import CliTestCase, FakeCommandContext
from tests.flash_fixtures import (
    FULL_BANK_OS_RELEASE,
    FakeFlashDevice,
    acpd_checksum,
    full_broken_secondary,
    full_old_secondary,
    full_primary,
    inspect_full_banks,
    make_full_bank,
    newest_firmware_payload,
    newest_firmware_prefix,
    plan_full_restore,
    with_flipped_byte,
)
from timecapsulesmb.cli import flash as cli_flash
from timecapsulesmb.device.compat import DeviceCompatibility
from timecapsulesmb.flash import (
    PATCHED_LOGIN_SCRIPT,
    STOCK_LOGIN_NETBSD4_DUMMY,
    FlashAnalysisError,
    find_footer,
    inspect_flash_banks,
)
from timecapsulesmb.flash_workflow import (
    FIRMWARE_BANK_FOOTER_OFFSET,
    FIRMWARE_BANK_SIZE,
    SECONDARY_BANK_WRITE_COMMAND,
    SecondaryBankInvalidError,
    SecondaryBankReadMismatchError,
    build_secondary_bank_image,
    require_primary_patch_ready,
    write_and_validate_secondary_refresh,
)
from timecapsulesmb.integrations.acp import ACPError
from timecapsulesmb.services import flash as flash_service
from timecapsulesmb.services.callbacks import OperationCallbacks
from timecapsulesmb.services.flash import FlashInputs
from timecapsulesmb.transport.ssh import SshConnection, SshError

CONNECTION = SshConnection("root@10.0.0.2", "pw", "-o test")


@functools.cache
def damaged_secondary() -> bytes:
    """A secondary whose footer survived but whose data does not match it."""
    return with_flipped_byte(full_old_secondary())


def damaged_secondary_footer() -> int:
    """What ACPd's cks2 is when its own read of damaged_secondary() is fine: the checksum its footer holds."""
    return struct.unpack(">I", damaged_secondary()[-32:-28])[0]


def device(secondary: bytes | None = None, *, primary: bytes | None = None) -> FakeFlashDevice:
    return FakeFlashDevice(
        primary=full_primary() if primary is None else primary,
        secondary=full_broken_secondary() if secondary is None else secondary,
    )


def write_refresh(flash: FakeFlashDevice, plan) -> dict[str, object]:
    return write_and_validate_secondary_refresh(
        connection=CONNECTION,
        acp_host="10.0.0.2",
        plan=plan,
        run_write_func=flash.run_write,
        dump_remote_bank_func=flash.dump,
        get_property_int_func=flash.get_property,
        timeout=600,
    )


def refresh_plan():
    plan, _download, _restore = plan_full_restore(inspect_full_banks())
    return plan


class SecondaryBankImageTests(unittest.TestCase):
    def test_image_is_a_whole_bank_with_apples_footer_written_last(self) -> None:
        image, checksum = build_secondary_bank_image(newest_firmware_prefix())

        self.assertEqual(len(image), FIRMWARE_BANK_SIZE)
        self.assertEqual(image[: len(newest_firmware_prefix())], newest_firmware_prefix())
        self.assertEqual(set(image[len(newest_firmware_prefix()) : FIRMWARE_BANK_FOOTER_OFFSET]), {0xFF})
        self.assertEqual(image[FIRMWARE_BANK_FOOTER_OFFSET:FIRMWARE_BANK_FOOTER_OFFSET + 8],
                         struct.pack(">II", checksum, len(newest_firmware_prefix())))
        self.assertEqual(set(image[FIRMWARE_BANK_FOOTER_OFFSET + 8 :]), {0xFF})
        # The footer is the last thing in the stream dd writes.
        self.assertEqual(find_footer(image).offset, FIRMWARE_BANK_FOOTER_OFFSET)
        self.assertEqual(acpd_checksum(image), checksum)

    def test_image_refuses_payloads_that_do_not_fit_a_bank(self) -> None:
        for size in (3 * 1024 * 1024 - 1, FIRMWARE_BANK_FOOTER_OFFSET + 1):
            with self.subTest(size=size):
                with self.assertRaisesRegex(FlashAnalysisError, "does not fit a firmware bank"):
                    build_secondary_bank_image(b"\x00" * size)
        image, _checksum = build_secondary_bank_image(b"\x00" * FIRMWARE_BANK_FOOTER_OFFSET)
        self.assertEqual(len(image), FIRMWARE_BANK_SIZE)


class SecondaryRestoreTargetTests(unittest.TestCase):
    def test_restore_rewrites_an_invalid_secondary_with_the_newest_firmware(self) -> None:
        inspection = inspect_full_banks()
        plan, download, restore = plan_full_restore(inspection)

        assert plan.secondary_refresh is not None
        self.assertEqual(plan.mode, "restore")
        self.assertIsNone(plan.target_bank)
        self.assertEqual(plan.target_name, "secondary")
        self.assertTrue(plan.write_requested)
        download.assert_called_once_with(syap="116", firmware_template=None, firmware_version=None, cache_dir=None)
        restore.assert_not_called()
        image, checksum = build_secondary_bank_image(newest_firmware_prefix())
        self.assertEqual(plan.secondary_refresh.image, image)
        self.assertEqual(plan.secondary_refresh.footer_checksum, checksum)
        self.assertEqual(plan.secondary_refresh.primary_sha256, inspection.primary.sha256)
        self.assertEqual(plan.secondary_refresh.primary_login, "stock")
        jsonable = plan.to_jsonable()
        self.assertEqual(jsonable["target_bank"], "secondary")
        self.assertEqual(jsonable["secondary_refresh"]["device"], "/dev/rflash1.raw")

    def test_firmware_version_choice_reaches_the_download(self) -> None:
        _plan, download, _restore = plan_full_restore(inspect_full_banks(), firmware_version="7.6.9")

        self.assertEqual(download.call_args.kwargs["firmware_version"], "7.6.9")

    def test_every_checked_model_rewrites_and_others_keep_the_refusal(self) -> None:
        for syap in ("106", "109", "113", "116"):
            with self.subTest(syap=syap):
                plan, _download, _restore = plan_full_restore(inspect_full_banks(), syap=syap)
                self.assertIsNotNone(plan.secondary_refresh)
        for syap in ("105", "114", "117", "119"):
            with self.subTest(syap=syap):
                with self.assertRaisesRegex(FlashAnalysisError, f"not supported on syAP {syap} yet"):
                    plan_full_restore(inspect_full_banks(), syap=syap)

    def test_a_secondary_both_reads_find_damaged_is_rewritten(self) -> None:
        # ACPd's cks2 is what it computes over its own read: over the damaged
        # data when there is a footer, 0 when it finds none.
        cases = {
            "data does not match its footer": inspect_full_banks(secondary=damaged_secondary()),
            "footer slot zeroed, ACPd finds no footer": inspect_full_banks(secondary=full_broken_secondary()),
            "data does not match, ACPd finds no footer": inspect_full_banks(secondary=damaged_secondary(), cks2=0),
            "erased bank": inspect_full_banks(secondary=b"\xff" * FIRMWARE_BANK_SIZE),
        }
        for name, inspection in cases.items():
            with self.subTest(name):
                plan, _download, _restore = plan_full_restore(inspection)
                self.assertIsNotNone(plan.secondary_refresh)
                with self.assertRaises(SecondaryBankInvalidError):
                    require_primary_patch_ready(inspection)

    def test_a_secondary_the_two_reads_disagree_about_is_left_alone(self) -> None:
        # When either read passes the footer, the chip may hold a good
        # fallback and the other read was unlucky: neither restore nor patch
        # proceeds, and both say which read disagreed.
        cases = {
            "only ACPd's read matches the footer": (
                inspect_full_banks(secondary=damaged_secondary(), cks2=damaged_secondary_footer()),
                "this backup's read does not match the footer checksum "
                f"0x{damaged_secondary_footer():08x}, but ACPd's own read does",
            ),
            "only this backup's read matches the footer": (
                inspect_full_banks(secondary=full_old_secondary(), cks2=0x12345678),
                "this backup's read matches the footer checksum "
                f"0x{find_footer(full_old_secondary()).checksum:08x}, but ACPd's own read does not (cks2 0x12345678)",
            ),
            "only ACPd's read finds a footer": (
                inspect_full_banks(secondary=full_broken_secondary(), cks2=0x12345678),
                "this backup's read found no footer, but ACPd's own read found one (cks2 0x12345678)",
            ),
            "no cks2 in the backup": (
                inspect_flash_banks(
                    primary_data=full_primary(), secondary_data=full_broken_secondary(),
                    cks1=acpd_checksum(full_primary()), cks2=None, os_release=FULL_BANK_OS_RELEASE,
                ),
                "this backup has no ACP checksum (cks2) for the secondary bank",
            ),
        }
        for name, (inspection, detail) in cases.items():
            with self.subTest(name):
                self.assertFalse(inspection.secondary.backup_valid)
                with self.assertRaises(SecondaryBankReadMismatchError) as restore_refusal:
                    plan_full_restore(inspection)
                restore_text = str(restore_refusal.exception)
                self.assertIn("refusing to rewrite the secondary bank because", restore_text)
                self.assertIn(detail, restore_text)
                self.assertIn("Back up and inspect again.", restore_text)
                self.assertNotIn("--force", restore_text)
                with self.assertRaises(SecondaryBankReadMismatchError) as patch_refusal:
                    require_primary_patch_ready(inspection)
                patch_text = str(patch_refusal.exception)
                self.assertIn("refusing to patch primary because", patch_text)
                self.assertIn(detail, patch_text)
                self.assertIn("`tcapsule flash --patch --force`", patch_text)

    def test_a_secondary_both_reads_find_intact_keeps_the_existing_refusals(self) -> None:
        # Its footer checks out in both reads, but it holds nothing we
        # recognize as firmware: nothing says rewriting it is safe.
        unrecognized, checksum = build_secondary_bank_image(b"\x00" * (3 * 1024 * 1024))
        inspection = inspect_full_banks(secondary=unrecognized)
        self.assertEqual(inspection.secondary.acp_checksum, checksum)
        self.assertFalse(inspection.secondary.backup_valid)

        with self.assertRaisesRegex(FlashAnalysisError, "both firmware banks must be valid backups") as restore_refusal:
            plan_full_restore(inspection)
        self.assertNotIsInstance(restore_refusal.exception, SecondaryBankReadMismatchError)
        with self.assertRaisesRegex(FlashAnalysisError, "both firmware banks must be valid backups") as patch_refusal:
            require_primary_patch_ready(inspection)
        self.assertNotIsInstance(patch_refusal.exception, (SecondaryBankInvalidError, SecondaryBankReadMismatchError))

    def test_force_patches_past_a_secondary_the_reads_disagree_about(self) -> None:
        # An already patched primary needs no patch candidate, so the refusal
        # under test is the only one in the way.
        inspection = inspect_full_banks(
            primary=make_full_bank(login=PATCHED_LOGIN_SCRIPT),
            secondary=damaged_secondary(),
            cks2=damaged_secondary_footer(),
        )

        with self.assertRaises(SecondaryBankReadMismatchError):
            require_primary_patch_ready(inspection)
        self.assertEqual(require_primary_patch_ready(inspection, force=True).name, "primary")

    def test_the_footer_slot_is_read_where_acpd_reads_it_whether_or_not_it_matches(self) -> None:
        cases = {
            "valid bank": (full_old_secondary(), find_footer(full_old_secondary()).checksum, True),
            "data no longer matches the footer": (damaged_secondary(), damaged_secondary_footer(), False),
            "footer slot zeroed": (full_broken_secondary(), None, False),
            "erased bank": (b"\xff" * FIRMWARE_BANK_SIZE, None, False),
        }
        for name, (bank, checksum, matches) in cases.items():
            with self.subTest(name):
                secondary = inspect_full_banks(secondary=bank).secondary
                self.assertEqual(secondary.stored_footer_checksum, checksum)
                self.assertEqual(secondary.stored_footer_matches_data, matches)
        self.assertNotEqual(damaged_secondary_footer(), acpd_checksum(damaged_secondary()))

    def test_a_valid_secondary_keeps_the_existing_primary_restore(self) -> None:
        plan, download, restore = plan_full_restore(inspect_full_banks(secondary=full_old_secondary()))

        self.assertIsNone(plan.secondary_refresh)
        self.assertEqual(plan.target_name, "primary")
        restore.assert_called_once()
        download.assert_not_called()

    def test_no_rewrite_unless_the_primary_is_a_valid_running_bank(self) -> None:
        cases = {
            "primary invalid, secondary valid": inspect_full_banks(primary=full_broken_secondary(), secondary=full_old_secondary()),
            "both invalid": inspect_full_banks(primary=full_broken_secondary()),
            "primary cks1 disagrees": inspect_full_banks(cks1=0x12345678),
            "primary not the running kernel": inspect_full_banks(primary=make_full_bank(b"NetBSD 3.0 #0: other")),
        }
        for name, inspection in cases.items():
            with self.subTest(name):
                with self.assertRaises(FlashAnalysisError) as raised:
                    plan_full_restore(inspection)
                self.assertNotIsInstance(raised.exception, SecondaryBankInvalidError)

    def test_banks_of_another_size_are_refused(self) -> None:
        # A read that came back short, or a model whose banks are another size.
        small = FIRMWARE_BANK_SIZE - 0x10000
        cases = {
            "primary": inspect_full_banks(primary=make_full_bank(size=small)),
            "secondary": inspect_full_banks(secondary=full_broken_secondary()[:small]),
        }
        for bank, inspection in cases.items():
            with self.subTest(bank=bank):
                self.assertTrue(inspection.primary.backup_valid)
                with self.assertRaisesRegex(FlashAnalysisError, f"the {bank} bank read {small} bytes, expected 7340032"):
                    plan_full_restore(inspection)

    def test_patch_refusal_points_at_restore_when_only_the_secondary_is_bad(self) -> None:
        with self.assertRaises(SecondaryBankInvalidError) as raised:
            require_primary_patch_ready(inspect_full_banks())

        self.assertIn("secondary (backup) firmware bank is not a valid backup", str(raised.exception))
        self.assertIn("tcapsule flash --restore", str(raised.exception))
        self.assertNotIn("--force", str(raised.exception))

    def test_patch_refusal_for_a_bad_primary_is_unchanged(self) -> None:
        with self.assertRaises(FlashAnalysisError) as raised:
            require_primary_patch_ready(inspect_full_banks(primary=full_broken_secondary(), secondary=full_old_secondary()))

        self.assertNotIsInstance(raised.exception, SecondaryBankInvalidError)
        self.assertIn("primary firmware bank could not be analyzed", str(raised.exception))


class SecondaryRefreshWriteTests(unittest.TestCase):
    def test_success_writes_only_the_secondary_and_proves_it_three_ways(self) -> None:
        flash = device()
        plan = refresh_plan()

        result = write_refresh(flash, plan)

        self.assertEqual(flash.commands, [(SECONDARY_BANK_WRITE_COMMAND, 600)])
        self.assertEqual(flash.erased, ["/dev/rflash1.raw"])
        self.assertFalse(flash.unlocked)
        self.assertEqual(flash.primary, full_primary())
        self.assertEqual(flash.secondary, plan.secondary_refresh.image)
        self.assertEqual(result["bank"], "secondary")
        self.assertEqual(result["firmware_version"], "7.8.1")
        self.assertEqual(result["acp_checksum"], f"0x{plan.secondary_refresh.footer_checksum:08x}")
        self.assertEqual(result["cks1_before"], result["cks1_after"])
        self.assertEqual(flash.reads, 1)

    def test_one_flaky_read_is_retried_before_judging_the_write(self) -> None:
        flash = device()
        flash.flaky_reads = 1

        result = write_refresh(flash, refresh_plan())

        self.assertEqual(flash.reads, 2)
        self.assertEqual(result["bank"], "secondary")

    def test_a_bank_that_kept_its_old_contents_may_be_write_protected(self) -> None:
        flash = device()
        flash.write_protected = True

        with self.assertRaisesRegex(FlashAnalysisError, "may be write-protected") as raised:
            write_refresh(flash, refresh_plan())
        self.assertIn("primary firmware is unchanged", str(raised.exception))

    def test_a_readback_that_matches_neither_image_is_unverified(self) -> None:
        for name, setup in (
            ("corrupt write", lambda flash: setattr(flash, "corrupt_write", True)),
            ("two flaky reads", lambda flash: setattr(flash, "flaky_reads", 2)),
        ):
            with self.subTest(name):
                flash = device()
                setup(flash)
                with self.assertRaisesRegex(FlashAnalysisError, "was written but could not be verified \\(read-back SHA-256"):
                    write_refresh(flash, refresh_plan())

    def test_a_failed_write_command_says_the_bank_was_not_written(self) -> None:
        flash = device()
        flash.write_error = SshError("ssh command failed with rc=1")

        with self.assertRaisesRegex(FlashAnalysisError, "could not be written \\(ssh command failed with rc=1\\)") as raised:
            write_refresh(flash, refresh_plan())
        self.assertNotIn("was written", str(raised.exception))
        self.assertIn("you can retry", str(raised.exception))
        self.assertEqual(flash.reads, 0)

    def test_cks2_must_equal_the_footer(self) -> None:
        flash = device()
        real = flash.get_property
        flash.get_property = lambda host, password, name: 0xDEADBEEF if name == "cks2" else real(host, password, name)  # type: ignore[method-assign]

        with self.assertRaisesRegex(FlashAnalysisError, "ACP cks2 0xdeadbeef, footer"):
            write_refresh(flash, refresh_plan())

    def test_a_primary_that_changed_fails_the_write(self) -> None:
        flash = device()
        flash.change_primary_on_write = True

        with self.assertRaisesRegex(FlashAnalysisError, "ACP cks1 changed during the secondary bank write"):
            write_refresh(flash, refresh_plan())

    def test_no_write_without_a_cks1_baseline(self) -> None:
        flash = device()
        flash.cks_error = ACPError("timed out")

        with self.assertRaisesRegex(FlashAnalysisError, "cks1 read failed before the secondary bank write"):
            write_refresh(flash, refresh_plan())
        self.assertEqual(flash.commands, [])

    def test_a_plan_without_an_image_is_refused(self) -> None:
        plan = SimpleNamespace(secondary_refresh=None, payload=newest_firmware_payload(), mode="restore")

        with self.assertRaisesRegex(FlashAnalysisError, "no secondary bank image"):
            write_refresh(device(), plan)


class SecondaryRefreshServiceTests(unittest.TestCase):
    """From a saved backup through the write and what follows it, as the CLI and app run it."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.backup_dir = Path(self.tmp.name) / "backup"
        self.target = flash_service.FlashTarget(
            connection=CONNECTION,
            acp_host="10.0.0.2",
            compatibility=SimpleNamespace(os_release=FULL_BANK_OS_RELEASE),
        )
        inputs = FlashInputs(
            primary=full_primary(),
            secondary=full_broken_secondary(),
            cks1=acpd_checksum(full_primary()),
            cks2=acpd_checksum(full_broken_secondary()),
            syap="116",
            live_login=STOCK_LOGIN_NETBSD4_DUMMY,
        )
        flash_service.save_flash_backup(target=self.target, inputs=inputs, backup_dir=self.backup_dir)

    def plan(self):
        with mock.patch("timecapsulesmb.flash_workflow.build_download_payload_for_syap", return_value=newest_firmware_payload()):
            return flash_service.plan_flash_from_backup(
                backup_dir=self.backup_dir,
                operation="restore",
                force=False,
                firmware_template=None,
                firmware_version=None,
            )

    def patched_device(self, flash: FakeFlashDevice):
        return mock.patch.multiple(
            "timecapsulesmb.services.flash",
            run_ssh_input=flash.run_write,
            dump_remote_bank=flash.dump,
            get_property_int=flash.get_property,
        )

    def test_plan_records_the_secondary_rewrite_in_the_manifest(self) -> None:
        _bundle, plan = self.plan()
        saved = flash_service.load_flash_manifest(self.backup_dir)

        self.assertIsNotNone(plan.secondary_refresh)
        self.assertEqual(saved["flash_plan"]["target_bank"], "secondary")
        self.assertEqual(saved["flash_plan"]["secondary_refresh"]["image_size"], FIRMWARE_BANK_SIZE)
        decisions = {bank["name"]: bank["write_decision"] for bank in saved["banks"]}
        self.assertEqual(decisions["secondary"], "invalid secondary bank rewrite with the newest Apple firmware planned")
        self.assertEqual(decisions["primary"], "primary bank left unmodified")
        self.assertTrue((self.backup_dir / "secondary.restore.basebinary").exists())
        self.assertEqual(flash_service.write_stage_for_plan(plan), "write_secondary_bank")

    def test_write_then_finish_leaves_a_stale_backup_and_no_reboot(self) -> None:
        bundle, plan = self.plan()
        flash = device()
        with self.patched_device(flash):
            flash_service.validate_live_target_matches_backup(connection=CONNECTION, plan=plan)
            result = flash_service.write_flash_plan(target=self.target, bundle=bundle, plan=plan)
            flash_service.finish_validated_write(
                target=self.target, bundle=bundle, plan=plan, reboot=True, wait=True, callbacks=OperationCallbacks(),
            )
        saved = flash_service.load_flash_manifest(self.backup_dir)

        self.assertEqual(flash.erased, ["/dev/rflash1.raw"])
        self.assertEqual(flash.primary, full_primary())
        self.assertEqual(result["bank"], "secondary")
        outcome = saved["write_outcome"]
        self.assertEqual((outcome["status"], outcome["bank"], outcome["device"]), ("validated", "secondary", "/dev/rflash1.raw"))
        self.assertEqual(outcome["post_write_action"], "none")
        self.assertFalse(outcome["reboot_requested"])
        self.assertEqual(saved["write_result"]["readback_sha256"], saved["write_result"]["image_sha256"])
        with self.assertRaisesRegex(FlashAnalysisError, "used for a firmware write"):
            flash_service.require_backup_fresh_for_plan(saved)

    def test_pre_write_check_refuses_when_the_live_primary_moved(self) -> None:
        _bundle, plan = self.plan()
        flash = device(primary=make_full_bank(b"NetBSD 4.0_STABLE #0: changed"))
        with self.patched_device(flash):
            with self.assertRaisesRegex(FlashAnalysisError, "live primary firmware bank changed"):
                flash_service.validate_live_target_matches_backup(connection=CONNECTION, plan=plan)
        self.assertEqual(flash.commands, [])

    def test_a_failed_write_is_recorded_as_possibly_modifying_the_device(self) -> None:
        bundle, plan = self.plan()
        flash = device()
        flash.write_protected = True
        with self.patched_device(flash):
            with self.assertRaisesRegex(FlashAnalysisError, "write-protected"):
                flash_service.write_flash_plan(target=self.target, bundle=bundle, plan=plan)
        saved = flash_service.load_flash_manifest(self.backup_dir)

        self.assertEqual(saved["write_outcome"]["status"], "attempting")
        self.assertTrue(saved["write_outcome"]["write_may_have_modified_device"])
        self.assertEqual(saved["write_outcome"]["stage"], "write_secondary_bank")


class SecondaryRestoreCliTests(CliTestCase):
    """`tcapsule flash --restore` on a device whose secondary bank is invalid."""

    def run_cli(self, argv: list[str], flash: FakeFlashDevice, *, confirm: object = None, cks2: int | None = None):
        command_context = FakeCommandContext(compatibility=DeviceCompatibility(
            os_name="NetBSD", os_release=FULL_BANK_OS_RELEASE, arch="earmv4", elf_endianness="little",
            payload_family="netbsd4le_samba4", device_generation="gen1-4", supported=True,
            reason_code="supported_netbsd4",
        ))
        inputs = FlashInputs(
            primary=flash.primary, secondary=flash.secondary, cks1=acpd_checksum(flash.primary),
            cks2=acpd_checksum(flash.secondary) if cks2 is None else cks2,
            syap="116", live_login=STOCK_LOGIN_NETBSD4_DUMMY,
        )
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            for patch in (
                mock.patch("timecapsulesmb.cli.flash.load_env_config", return_value=self.make_app_config(self.make_valid_env())),
                mock.patch("timecapsulesmb.cli.flash.CommandContext", return_value=command_context),
                mock.patch("timecapsulesmb.services.flash.read_flash_inputs", return_value=inputs),
                mock.patch("timecapsulesmb.flash_workflow.build_download_payload_for_syap",
                           return_value=newest_firmware_payload()),
                mock.patch.multiple(
                    "timecapsulesmb.services.flash",
                    run_ssh_input=flash.run_write,
                    dump_remote_bank=flash.dump,
                    get_property_int=flash.get_property,
                ),
                mock.patch("timecapsulesmb.flash.require_python_module", return_value=None),
            ):
                stack.enter_context(patch)
            prompt = stack.enter_context(mock.patch("timecapsulesmb.cli.runtime.confirm", side_effect=confirm))
            with redirect_stdout(output):
                rc = cli_flash.main([*argv, "--backup-dir", str(Path(tmp) / "backup")])
        return rc, output.getvalue(), command_context, prompt

    def test_restore_rewrites_the_secondary_and_then_suggests_patching_a_stock_primary(self) -> None:
        flash = device()

        rc, text, command_context, _prompt = self.run_cli(["--restore", "--yes"], flash)

        self.assertEqual(rc, 0)
        self.assertEqual(flash.erased, ["/dev/rflash1.raw"])
        self.assertEqual(flash.secondary, build_secondary_bank_image(newest_firmware_prefix())[0])
        self.assertIn("Backup firmware bank restored and verified.", text)
        self.assertIn("Run `tcapsule flash --patch` to install the boot hook.", text)
        self.assertNotIn("POWER-CYCLE REQUIRED", text)
        self.assertEqual(command_context.result, "success")
        self.assertEqual(command_context.finish_fields["flash_plan_target_bank"], "secondary")
        self.assertEqual(command_context.finish_fields["wrote_bank"], "secondary")

    def test_restore_on_a_patched_primary_says_the_hook_stays_and_how_to_remove_it(self) -> None:
        # The user may have come from patch's refusal (nothing left to do) or
        # be restoring to undo the patch (restore again): say both.
        flash = device(primary=make_full_bank(login=PATCHED_LOGIN_SCRIPT))

        rc, text, _command_context, _prompt = self.run_cli(["--restore", "--yes"], flash)

        self.assertEqual(rc, 0)
        self.assertEqual(flash.primary, make_full_bank(login=PATCHED_LOGIN_SCRIPT))
        self.assertIn("The primary bank keeps the boot hook. To put Apple stock firmware back on it, "
                      "run `tcapsule flash --restore` again.", text)
        self.assertNotIn("flash --patch", text)

    def test_restore_on_an_unrecognized_primary_suggests_no_next_step(self) -> None:
        # Patch refuses a LOGIN it does not recognize, and restore would
        # replace whatever it is: neither is ours to suggest.
        flash = device(primary=make_full_bank(login=b"#!/bin/sh\n# someone else's LOGIN\n"))

        rc, text, _command_context, _prompt = self.run_cli(["--restore", "--yes"], flash)

        self.assertEqual(rc, 0)
        self.assertIn("Backup firmware bank restored and verified.", text)
        self.assertNotIn("flash --patch", text)
        self.assertNotIn("flash --restore` again", text)

    def test_the_write_is_logged_once(self) -> None:
        rc, text, _command_context, _prompt = self.run_cli(["--restore", "--yes"], device())

        self.assertEqual(rc, 0)
        self.assertEqual(text.count("Writing the secondary"), 1)
        self.assertIn(f"Writing the secondary firmware bank with {SECONDARY_BANK_WRITE_COMMAND}", text)

    def test_the_prompt_names_the_secondary_bank_and_a_no_writes_nothing(self) -> None:
        flash = device()

        rc, text, command_context, prompt = self.run_cli(["--restore"], flash, confirm=[False])

        self.assertEqual(rc, 0)
        self.assertEqual(flash.commands, [])
        prompt_text = prompt.call_args.args[0]
        self.assertIn("The secondary (backup) firmware bank is invalid.", prompt_text)
        self.assertIn("Apple firmware 7.8.1 for product 116 to the secondary bank", prompt_text)
        self.assertIn("The primary bank is not changed and no reboot is needed.", prompt_text)
        self.assertIn("Flash write cancelled.", text)
        self.assertEqual(command_context.result, "cancelled")

    def test_reboot_is_refused_before_anything_is_written(self) -> None:
        flash = device()

        rc, text, command_context, _prompt = self.run_cli(["--restore", "--yes", "--reboot"], flash)

        self.assertEqual(rc, 1)
        self.assertEqual(flash.commands, [])
        self.assertIn("--reboot does not apply", text)
        self.assertEqual(command_context.result, "failure")

    def test_patch_on_the_same_device_points_at_restore(self) -> None:
        flash = device()

        rc, text, command_context, _prompt = self.run_cli(["--patch", "--yes"], flash)

        self.assertEqual(rc, 1)
        self.assertEqual(flash.commands, [])
        self.assertIn("Run restore (`tcapsule flash --restore`)", text)
        self.assertIn("flash_error_stage=plan_flash", command_context.finish.call_args.kwargs["error"])

    def test_restore_leaves_a_secondary_only_acpd_reads_as_valid(self) -> None:
        flash = device(damaged_secondary())

        rc, text, _command_context, _prompt = self.run_cli(["--restore", "--yes"], flash, cks2=damaged_secondary_footer())

        self.assertEqual(rc, 1)
        self.assertEqual(flash.commands, [])
        self.assertIn("but ACPd's own read does", text)
        self.assertIn("Back up and inspect again.", text)


if __name__ == "__main__":
    unittest.main()
