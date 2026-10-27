"""The wipe-disk command: choosing the disk, the confirmation, the dry run,
and how its errors read."""
from __future__ import annotations

import io
from contextlib import ExitStack, redirect_stdout
from unittest import mock

from timecapsulesmb.cli import wipe_disk as cli
from timecapsulesmb.cli.main import main
from timecapsulesmb.core.error_codes import ErrorCode
from timecapsulesmb.device import wipe
from timecapsulesmb.services import wipe_disk

from tests.cli_support import CliTestCase
from tests.test_wipe_disk import TWO_TB, disk


def plan(**changes: object) -> wipe_disk.WipePlan:
    from dataclasses import replace
    base = wipe_disk.WipePlan(disk(), TWO_TB, wipe.wipe_offsets(TWO_TB), 2, "Data", wipe.GptVerdict("apple_hfs"), True)
    return replace(base, **changes)


class CliWipeDiskTests(CliTestCase):
    def run_cli(self, argv: list[str], *, answers: list[str] | None = None, outcome=None, error=None):
        output = io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(cli, "load_env_config", return_value=self.make_app_config(self.make_valid_env())))
            self.prepare = stack.enter_context(mock.patch.object(cli, "prepare_wipe", return_value=plan()))
            self.run_wipe = stack.enter_context(mock.patch.object(
                cli, "run_wipe",
                side_effect=error, return_value=outcome or wipe_disk.WipeOutcome("uuid", "Data", True),
            ))
            self.failing_preflight = stack.enter_context(
                mock.patch.object(cli, "confirm_despite_failing_disk", return_value=None))
            stack.enter_context(mock.patch.object(cli, "require_acpd_ready"))
            self.input = stack.enter_context(mock.patch("builtins.input", side_effect=answers or AssertionError("no prompt expected")))
            with redirect_stdout(output):
                try:
                    rc = cli.main(argv)
                except SystemExit as exc:
                    rc = exc
        return rc, output.getvalue()

    def test_wipe_internal_disk_command_is_registered(self) -> None:
        from timecapsulesmb.cli.main import COMMANDS
        self.assertIs(COMMANDS["wipe-internal-disk"], cli.main)
        self.assertNotIn("wipe-disk", COMMANDS)
        with mock.patch("timecapsulesmb.cli.main.COMMANDS", {"wipe-internal-disk": mock.Mock(return_value=0)}) as commands:
            self.assertEqual(main(["wipe-internal-disk", "--dry-run"]), 0)
        commands["wipe-internal-disk"].assert_called_once_with(["--dry-run"])

    def test_there_is_no_disk_choice(self) -> None:
        # The internal disk is the only target, and prepare_wipe finds it.
        rc, _text = self.run_cli(["--yes", "--disk", "sd1"])
        self.assertEqual(rc.code, 2)
        self.prepare.assert_not_called()
        self.run_cli(["--yes"])
        self.assertEqual(len(self.prepare.call_args.args), 1)
        self.assertEqual(set(self.prepare.call_args.kwargs), {"callbacks"})

    def test_no_input_needs_yes(self) -> None:
        rc, text = self.run_cli(["--no-input"])
        self.assertEqual(rc, 1)
        self.assertIn("requires `--yes`", text)
        self.prepare.assert_not_called()
        self.run_wipe.assert_not_called()
        rc, _text = self.run_cli(["--no-input", "--yes"])
        self.assertEqual(rc, 0)
        self.run_wipe.assert_called_once()

    def test_a_dry_run_prints_the_plan_and_changes_nothing(self) -> None:
        rc, text = self.run_cli(["--dry-run"])
        self.assertEqual(rc, 0)
        self.assertIn("Dry run: Wipe Internal Disk plan", text)
        self.assertIn("Disk: Disk 1 (sd0, internal, 2.0 TB, WDC WD20EARS-42S0XB0): Data (9 GB used)", text)
        # The raw whole-disk device, both ends in blocks that divide the disk,
        # and the exact request diskd gets.
        self.assertIn("zero 1280 x 8192 bytes at the start of /dev/rsd0c", text)
        self.assertIn("zero 1280 x 8192 bytes from block 244188043", text)
        self.assertIn(
            "erase (Quick): diskd.eraseDisk uuid:h:8320beddbee156fc92fbcc47aa532c1f deviceName:s:sd0 "
            "volumeName:s:Data method:i:0", text,
        )
        self.run_wipe.assert_not_called()

    def test_it_asks_about_the_internal_disk_and_defaults_to_no(self) -> None:
        rc, text = self.run_cli([], answers=[""])
        self.assertEqual(rc, 0)
        self.prepare.assert_called_once()
        self.assertIn("Wipe Internal Disk cancelled.", text)
        self.run_wipe.assert_not_called()
        # The question names what is lost and defaults to No.
        question = self.input.call_args_list[-1].args[0]
        self.assertIn("deletes everything on Disk 1 (sd0, internal, 2.0 TB", question)
        self.assertIn("Data (9 GB used)", question)
        self.assertIn("Any backup in progress will be interrupted", question)
        self.assertTrue(question.endswith("[y/N]: "))

    def test_a_refusal_before_the_reboot_records_no_reboot(self) -> None:
        error = wipe_disk.WipeDiskError("busy", code=ErrorCode.WIPE_DISK_BUSY, reason="erase_busy")
        self.run_cli(["--yes"], error=error)
        self.assertFalse(self.telemetry_payload("wipe_disk_finished")["reboot_was_attempted"])

    def test_yes_wipes_and_says_install_comes_next(self) -> None:
        rc, text = self.run_cli(["--yes"])
        self.assertEqual(rc, 0)
        self.run_wipe.assert_called_once()
        self.assertIn('The disk is formatted as "Data".', text)
        self.assertIn("Run `tcapsule deploy` to install Samba on it.", text)

    def test_a_wipe_error_prints_its_next_steps(self) -> None:
        error = wipe_disk.WipeDiskError("The device could not start.", code=ErrorCode.WIPE_DISK_RESTART_NEEDED,
                                        reason="rpc_missing")
        rc, _text = self.run_cli(["--yes"], error=error)
        self.assertIsInstance(rc, SystemExit)
        message = str(rc.code)
        self.assertIn("The device could not start.", message)
        self.assertIn("1. Restart the device.", message)
        self.assertIn("2. Then run `tcapsule wipe-internal-disk` again.", message)
        self.assertEqual(self.telemetry_payload("wipe_disk_finished")["wipe_reason"], "rpc_missing")

    def test_a_disk_without_volumes_is_described_by_its_table_in_words(self) -> None:
        cases = {
            "foreign": (wipe.GptVerdict("foreign", ("efi", "basic_data")), "This disk is formatted as exFAT or NTFS."),
            "bogus map": (wipe.GptVerdict("bogus_map"), "AirPort Utility cannot replace this disk's partition table."),
            "blank": (wipe.GptVerdict("blank"), "This disk has no partitions."),
        }
        for name, (verdict, sentence) in cases.items():
            with self.subTest(name):
                text = cli.confirmation_text(plan(disk=disk(partitions=()), gpt_verdict=verdict), "Time Capsule")
                self.assertIn(f"(sd0, internal, 2.0 TB, WDC WD20EARS-42S0XB0). {sentence} Any backup", text)
                self.assertNotIn("basic_data", text)
                self.assertNotIn("efi", text)

    def test_the_failing_disk_warning_reads_only_the_wiped_disk(self) -> None:
        self.run_cli(["--yes"])
        read_evidence = self.failing_preflight.call_args.kwargs["read_evidence"]
        with mock.patch.object(cli, "wipe_failing_evidence", return_value=None) as scoped:
            read_evidence("connection")
        scoped.assert_called_once_with("connection", self.prepare.return_value)
