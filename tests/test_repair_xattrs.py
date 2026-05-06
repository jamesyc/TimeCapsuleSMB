from __future__ import annotations

import io
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from timecapsulesmb.cli import repair_xattrs
from timecapsulesmb import repair_xattrs as repair_xattrs_domain
from timecapsulesmb.core.config import AppConfig


class RecordingCommandContext:
    instances: list["RecordingCommandContext"] = []

    def __init__(self, *_args, **_kwargs) -> None:
        self.result = "failure"
        self.error: str | None = None
        self.finished = False
        RecordingCommandContext.instances.append(self)

    def __enter__(self) -> "RecordingCommandContext":
        return self

    def __exit__(self, exc_type, exc, _tb) -> bool:
        if exc_type is SystemExit:
            self.result = "failure"
            self.error = str(exc)
        self.finished = True
        return False

    def succeed(self) -> None:
        self.result = "success"
        self.error = None

    def fail_with_error(self, message: str) -> None:
        self.result = "failure"
        self.error = message


UNSET = object()


class FakeXattrCommands:
    def __init__(
        self,
        *,
        xattr_returncode: int = 1,
        xattr_stdout: str = "",
        xattr_stderr: str = "Invalid argument",
        stat_returncode: int = 0,
        stat_stdout: str = "arch\n",
        stat_stderr: str = "",
        chflags_returncode: int = 0,
        chflags_stderr: str = "",
        readable_after_chflags: bool = True,
        forbid_chflags: str | None = None,
        on_chflags=None,
    ) -> None:
        self.xattr_returncode = xattr_returncode
        self.xattr_stdout = xattr_stdout
        self.xattr_stderr = xattr_stderr
        self.stat_returncode = stat_returncode
        self.stat_stdout = stat_stdout
        self.stat_stderr = stat_stderr
        self.chflags_returncode = chflags_returncode
        self.chflags_stderr = chflags_stderr
        self.readable_after_chflags = readable_after_chflags
        self.forbid_chflags = forbid_chflags
        self.on_chflags = on_chflags
        self.repaired = False
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]):
        self.calls.append(args)
        if args[0] == "xattr":
            return mock.Mock(
                returncode=0 if self.repaired and self.readable_after_chflags else self.xattr_returncode,
                stdout=self.xattr_stdout,
                stderr=self.xattr_stderr,
            )
        if args[0] == "stat":
            return mock.Mock(returncode=self.stat_returncode, stdout=self.stat_stdout, stderr=self.stat_stderr)
        if args[0] == "chflags":
            if self.forbid_chflags:
                raise AssertionError(self.forbid_chflags)
            if self.on_chflags is not None:
                self.on_chflags(args)
            self.repaired = True
            return mock.Mock(returncode=self.chflags_returncode, stdout="", stderr=self.chflags_stderr)
        raise AssertionError(args)


class RepairXattrsTests(unittest.TestCase):
    def app_config(self, values: dict[str, str], *, exists: bool = True) -> AppConfig:
        return AppConfig.from_values(
            values,
            path=REPO_ROOT / ".env",
            exists=exists,
            file_values=values if exists else {},
        )

    def setUp(self) -> None:
        RecordingCommandContext.instances = []
        self.telemetry_patch = mock.patch("timecapsulesmb.cli.repair_xattrs.TelemetryClient.from_config", return_value=mock.Mock())
        self.telemetry_patch.start()
        self.addCleanup(self.telemetry_patch.stop)
        self.path_guard_patch = mock.patch(
            "timecapsulesmb.cli.repair_xattrs.validate_repair_root_under_volumes",
            side_effect=lambda path: path.expanduser(),
        )
        self.path_guard_mock = self.path_guard_patch.start()
        self.addCleanup(self.path_guard_patch.stop)

    def find_findings_with_commands(
        self,
        root: Path,
        commands: FakeXattrCommands,
        *,
        recursive: bool = True,
        max_depth: int | None = None,
        include_hidden: bool = False,
        include_time_machine: bool = False,
        include_directories: bool = False,
        summary: repair_xattrs.RepairSummary | None = None,
    ):
        summary = summary or repair_xattrs.RepairSummary()
        with mock.patch("timecapsulesmb.repair_xattrs.run_capture", side_effect=commands):
            findings = repair_xattrs.find_findings(
                root,
                recursive=recursive,
                max_depth=max_depth,
                include_hidden=include_hidden,
                include_time_machine=include_time_machine,
                include_directories=include_directories,
                summary=summary,
            )
        return SimpleNamespace(findings=findings, summary=summary, commands=commands)

    def run_repair_cli(
        self,
        root: Path,
        argv: list[str] | None = None,
        *,
        commands: FakeXattrCommands | None = None,
        input_return_value=UNSET,
        input_side_effect=UNSET,
        recording_context: bool = False,
    ):
        output = io.StringIO()
        commands = commands or FakeXattrCommands()
        mocks = SimpleNamespace()
        with ExitStack() as stack:
            mocks.platform = stack.enter_context(mock.patch("timecapsulesmb.cli.repair_xattrs.sys.platform", "darwin"))
            mocks.run_capture = stack.enter_context(mock.patch("timecapsulesmb.repair_xattrs.run_capture", side_effect=commands))
            if input_side_effect is not UNSET:
                mocks.input = stack.enter_context(mock.patch("builtins.input", side_effect=input_side_effect))
            elif input_return_value is not UNSET:
                mocks.input = stack.enter_context(mock.patch("builtins.input", return_value=input_return_value))
            if recording_context:
                mocks.command_context = stack.enter_context(mock.patch("timecapsulesmb.cli.repair_xattrs.CommandContext", RecordingCommandContext))
            with redirect_stdout(output):
                rc = repair_xattrs.main(["--path", str(root), *(argv or [])])
        return SimpleNamespace(rc=rc, output=output, text=output.getvalue(), commands=commands, mocks=mocks)

    def test_finds_arch_file_when_xattr_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "broken.txt"
            target.write_text("data")

            result = self.find_findings_with_commands(root, FakeXattrCommands())

        findings = result.findings
        summary = result.summary
        self.assertEqual([finding.path.name for finding in findings], ["broken.txt"])
        self.assertEqual(findings[0].kind, "repairable_arch_flag")
        self.assertEqual(findings[0].actions, (repair_xattrs.ACTION_CLEAR_ARCH_FLAG,))
        self.assertEqual(summary.scanned, 1)
        self.assertEqual(summary.repairable, 1)

    def test_find_findings_can_scan_repairable_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "broken-dir"
            target.mkdir()

            result = self.find_findings_with_commands(root, FakeXattrCommands(), include_directories=True)

        findings = result.findings
        summary = result.summary
        self.assertEqual([finding.path.name for finding in findings], ["broken-dir"])
        self.assertEqual(findings[0].kind, "repairable_arch_flag")
        self.assertEqual(findings[0].path_type, "directory")
        self.assertEqual(findings[0].actions, (repair_xattrs.ACTION_CLEAR_ARCH_FLAG,))
        self.assertEqual(summary.scanned_dirs, 1)

    def test_does_not_repair_when_xattr_is_readable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "ok.txt").write_text("data")

            with mock.patch("timecapsulesmb.repair_xattrs.run_capture", return_value=mock.Mock(returncode=0, stdout="", stderr="")):
                summary = repair_xattrs.RepairSummary()
                findings = repair_xattrs.find_findings(
                    root,
                    recursive=True,
                    max_depth=None,
                    include_hidden=False,
                    include_time_machine=False,
                    summary=summary,
                )

        self.assertEqual(findings, [])
        self.assertEqual(summary.scanned, 1)

    def test_iter_scan_paths_streams_directory_entries(self) -> None:
        class ExplodingAfterFirstIterator:
            def __init__(self, first: Path) -> None:
                self.first = first
                self.count = 0

            def __iter__(self):
                return self

            def __next__(self) -> Path:
                if self.count == 0:
                    self.count += 1
                    return self.first
                raise AssertionError("iterdir was materialized before yielding")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "first.txt"
            first.write_text("data")
            summary = repair_xattrs.RepairSummary()

            with mock.patch.object(Path, "iterdir", return_value=ExplodingAfterFirstIterator(first)):
                scanner = repair_xattrs.iter_scan_paths(
                    root,
                    recursive=True,
                    max_depth=None,
                    include_hidden=False,
                    include_time_machine=False,
                    summary=summary,
                )
                try:
                    self.assertEqual(next(scanner), (first, "file"))
                finally:
                    scanner.close()

    def test_does_not_repair_without_arch_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "bad-but-not-arch.txt").write_text("data")

            def fake_run(args: list[str]):
                if args[0] == "xattr":
                    return mock.Mock(returncode=1, stdout="", stderr="Invalid argument")
                if args[0] == "stat":
                    return mock.Mock(returncode=0, stdout="-\n", stderr="")
                raise AssertionError(args)

            summary = repair_xattrs.RepairSummary()
            with mock.patch("timecapsulesmb.repair_xattrs.run_capture", side_effect=fake_run):
                findings = repair_xattrs.find_findings(
                    root,
                    recursive=True,
                    max_depth=None,
                    include_hidden=False,
                    include_time_machine=False,
                    summary=summary,
                )

        self.assertEqual([finding.path.name for finding in findings], ["bad-but-not-arch.txt"])
        self.assertEqual(findings[0].kind, "unreadable_no_arch_flag")
        self.assertEqual(findings[0].actions, ())
        self.assertEqual(summary.scanned, 1)
        self.assertEqual(summary.not_repairable, 1)

    def test_unreadable_without_arch_is_reported_not_repairable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "bad-but-not-arch.txt").write_text("data")

            result = self.run_repair_cli(
                root,
                ["--dry-run", "--verbose"],
                commands=FakeXattrCommands(stat_stdout="-\n"),
                recording_context=True,
            )

        self.assertEqual(result.rc, 0)
        self.assertIn("WARN unreadable_no_arch_flag", result.text)
        self.assertEqual(RecordingCommandContext.instances[-1].result, "failure")
        self.assertIn("unreadable_no_arch_flag", RecordingCommandContext.instances[-1].error or "")

    def test_repairs_when_arch_is_one_of_multiple_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "broken.txt"
            target.write_text("data")

            def fake_run(args: list[str]):
                if args[0] == "xattr":
                    return mock.Mock(returncode=1, stdout="", stderr="Invalid argument")
                if args[0] == "stat":
                    return mock.Mock(returncode=0, stdout="arch,nodump\n", stderr="")
                raise AssertionError(args)

            summary = repair_xattrs.RepairSummary()
            with mock.patch("timecapsulesmb.repair_xattrs.run_capture", side_effect=fake_run):
                findings = repair_xattrs.find_findings(
                    root,
                    recursive=True,
                    max_depth=None,
                    include_hidden=False,
                    include_time_machine=False,
                    summary=summary,
                )

        self.assertEqual([finding.path.name for finding in findings], ["broken.txt"])
        self.assertEqual(findings[0].flags, "arch,nodump")
        self.assertEqual(findings[0].actions, (repair_xattrs.ACTION_CLEAR_ARCH_FLAG,))

    def test_does_not_repair_when_stat_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "bad-stat.txt").write_text("data")

            def fake_run(args: list[str]):
                if args[0] == "xattr":
                    return mock.Mock(returncode=1, stdout="", stderr="Invalid argument")
                if args[0] == "stat":
                    return mock.Mock(returncode=1, stdout="", stderr="stat failed")
                raise AssertionError(args)

            summary = repair_xattrs.RepairSummary()
            with mock.patch("timecapsulesmb.repair_xattrs.run_capture", side_effect=fake_run):
                findings = repair_xattrs.find_findings(
                    root,
                    recursive=True,
                    max_depth=None,
                    include_hidden=False,
                    include_time_machine=False,
                    summary=summary,
                )

        self.assertEqual([finding.path.name for finding in findings], ["bad-stat.txt"])
        self.assertEqual(findings[0].kind, "xattr_failed_stat_failed")
        self.assertEqual(findings[0].actions, ())
        self.assertEqual(summary.scanned, 1)
        self.assertEqual(summary.not_repairable, 1)

    def test_dry_run_does_not_call_chflags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "broken.txt").write_text("data")

            result = self.run_repair_cli(
                root,
                ["--dry-run"],
                commands=FakeXattrCommands(forbid_chflags="dry-run should not call chflags"),
            )

        self.assertEqual(result.rc, 0)
        self.assertIn("Would repair:", result.text)
        self.assertIn("No changes made.", result.text)

    def test_dry_run_with_detected_issues_records_failure_telemetry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "broken.txt").write_text("data")

            result = self.run_repair_cli(
                root,
                ["--dry-run"],
                commands=FakeXattrCommands(forbid_chflags="dry-run should not call chflags"),
                recording_context=True,
            )

        self.assertEqual(result.rc, 0)
        self.assertEqual(RecordingCommandContext.instances[-1].result, "failure")
        self.assertIn("repair-xattrs detected issues", RecordingCommandContext.instances[-1].error or "")

    def test_apply_repairs_after_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "broken.txt"
            target.write_text("data")
            commands = FakeXattrCommands(xattr_stderr="")
            result = self.run_repair_cli(root, commands=commands, input_return_value="y")

        self.assertEqual(result.rc, 0)
        self.assertTrue(commands.repaired)
        self.assertIn("PASS xattr now readable", result.text)

    def test_apply_yes_skips_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "broken.txt").write_text("data")

            result = self.run_repair_cli(
                root,
                ["--yes"],
                commands=FakeXattrCommands(xattr_stderr=""),
                input_side_effect=AssertionError("prompt should be skipped"),
            )

        self.assertEqual(result.rc, 0)
        result.mocks.input.assert_not_called()

    def test_successful_repairs_record_success_telemetry_without_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "broken.txt").write_text("data")
            result = self.run_repair_cli(root, ["--yes"], recording_context=True)

        self.assertEqual(result.rc, 0)
        self.assertEqual(RecordingCommandContext.instances[-1].result, "success")
        self.assertIsNone(RecordingCommandContext.instances[-1].error)

    def test_fix_permissions_repairs_files_and_directories_without_chflags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            file_path = root / "file.txt"
            file_path.write_text("data")
            dir_path = root / "dir"
            dir_path.mkdir()
            chmod_calls: list[list[str]] = []

            def fake_run(args: list[str]):
                if args[0] == "xattr":
                    return mock.Mock(returncode=0, stdout="", stderr="")
                if args[0] == "chmod":
                    chmod_calls.append(args)
                    return mock.Mock(returncode=0, stdout="", stderr="")
                if args[0] == "chflags":
                    raise AssertionError("permission repair should not call chflags")
                raise AssertionError(args)

            with mock.patch("timecapsulesmb.cli.repair_xattrs.sys.platform", "darwin"):
                with mock.patch("timecapsulesmb.repair_xattrs.run_capture", side_effect=fake_run):
                    with redirect_stdout(io.StringIO()):
                        rc = repair_xattrs.main(["--path", str(root), "--fix-permissions", "--yes"])

        self.assertEqual(rc, 0)
        self.assertIn(["chmod", "ugo+rw", str(file_path.resolve())], chmod_calls)
        self.assertIn(["chmod", "ugo+rwx", str(dir_path.resolve())], chmod_calls)

    def test_fix_permissions_excludes_samba4_even_when_hidden_included(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hidden_payload = root / ".samba4"
            hidden_payload.mkdir()
            (hidden_payload / "private-file").write_text("secret")
            visible = root / "visible.txt"
            visible.write_text("data")

            with mock.patch("timecapsulesmb.repair_xattrs.run_capture", return_value=mock.Mock(returncode=0, stdout="", stderr="")):
                summary = repair_xattrs.RepairSummary()
                findings = repair_xattrs.find_findings(
                    root,
                    recursive=True,
                    max_depth=None,
                    include_hidden=True,
                    include_time_machine=True,
                    include_directories=True,
                    fix_permissions=True,
                    summary=summary,
                )

        self.assertTrue(all(".samba4" not in finding.path.parts for finding in findings))

    def test_prompt_decline_does_not_repair(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "broken.txt").write_text("data")

            result = self.run_repair_cli(
                root,
                commands=FakeXattrCommands(xattr_stderr="", forbid_chflags="declining prompt should not repair"),
                input_return_value="n",
            )

        self.assertEqual(result.rc, 0)
        self.assertIn("No changes made.", result.text)

    def test_prompt_eof_declines_without_repairing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "broken.txt").write_text("data")

            result = self.run_repair_cli(
                root,
                commands=FakeXattrCommands(xattr_stderr="", forbid_chflags="EOF at prompt should not repair"),
                input_side_effect=EOFError,
            )

        self.assertEqual(result.rc, 0)
        self.assertIn("No changes made.", result.text)

    def test_prompt_keyboard_interrupt_declines_without_repairing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "broken.txt").write_text("data")

            result = self.run_repair_cli(
                root,
                commands=FakeXattrCommands(xattr_stderr="", forbid_chflags="KeyboardInterrupt at prompt should not repair"),
                input_side_effect=KeyboardInterrupt,
            )

        self.assertEqual(result.rc, 0)
        self.assertIn("No changes made.", result.text)

    def test_no_candidates_does_not_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "ok.txt").write_text("data")

            result = self.run_repair_cli(
                root,
                commands=FakeXattrCommands(xattr_returncode=0, xattr_stderr=""),
                input_side_effect=AssertionError("prompt should not be called"),
            )

        self.assertEqual(result.rc, 0)
        result.mocks.input.assert_not_called()
        self.assertIn("No repairable files found.", result.text)

    def test_repair_failure_returns_nonzero_when_xattr_still_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "broken.txt").write_text("data")

            result = self.run_repair_cli(
                root,
                ["--yes"],
                commands=FakeXattrCommands(xattr_stderr="", readable_after_chflags=False),
            )

        self.assertEqual(result.rc, 1)
        self.assertIn("FAIL repair did not make xattr readable", result.text)

    def test_repair_failure_returns_nonzero_when_chflags_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "broken.txt").write_text("data")

            result = self.run_repair_cli(
                root,
                ["--yes"],
                commands=FakeXattrCommands(xattr_stderr="", chflags_returncode=1, chflags_stderr="nope"),
            )

        self.assertEqual(result.rc, 1)

    def test_repair_failure_when_size_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "broken.txt"
            target.write_text("data")

            result = self.run_repair_cli(
                root,
                ["--yes"],
                commands=FakeXattrCommands(
                    xattr_stderr="",
                    readable_after_chflags=False,
                    on_chflags=lambda _args: target.write_text("changed"),
                ),
            )

        self.assertEqual(result.rc, 1)

    def test_skips_hidden_and_time_machine_paths_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".hidden.txt").write_text("hidden")
            tm = root / "Backups.backupdb"
            tm.mkdir()
            (tm / "backup.txt").write_text("backup")
            visible = root / "visible.txt"
            visible.write_text("visible")

            def fake_run(args: list[str]):
                if args[0] == "xattr":
                    return mock.Mock(returncode=1, stdout="", stderr="")
                if args[0] == "stat":
                    return mock.Mock(returncode=0, stdout="arch\n", stderr="")
                raise AssertionError(args)

            summary = repair_xattrs.RepairSummary()
            with mock.patch("timecapsulesmb.repair_xattrs.run_capture", side_effect=fake_run):
                findings = repair_xattrs.find_findings(
                    root,
                    recursive=True,
                    max_depth=None,
                    include_hidden=False,
                    include_time_machine=False,
                    summary=summary,
                )

        self.assertEqual([finding.path.name for finding in findings], ["visible.txt"])
        self.assertEqual(summary.skipped, 2)

    def test_include_flags_scan_hidden_and_time_machine_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".hidden.txt").write_text("hidden")
            tm = root / "Backups.backupdb"
            tm.mkdir()
            (tm / "backup.txt").write_text("backup")

            def fake_run(args: list[str]):
                if args[0] == "xattr":
                    return mock.Mock(returncode=1, stdout="", stderr="")
                if args[0] == "stat":
                    return mock.Mock(returncode=0, stdout="arch\n", stderr="")
                raise AssertionError(args)

            summary = repair_xattrs.RepairSummary()
            with mock.patch("timecapsulesmb.repair_xattrs.run_capture", side_effect=fake_run):
                findings = repair_xattrs.find_findings(
                    root,
                    recursive=True,
                    max_depth=None,
                    include_hidden=True,
                    include_time_machine=True,
                    summary=summary,
                )

        self.assertEqual(sorted(finding.path.name for finding in findings), [".hidden.txt", "backup.txt"])
        self.assertEqual(summary.skipped, 0)

    def test_skips_top_level_hidden_file_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / ".broken.txt"
            target.write_text("data")

            summary = repair_xattrs.RepairSummary()
            with mock.patch("timecapsulesmb.repair_xattrs.run_capture") as run_mock:
                findings = repair_xattrs.find_findings(
                    target,
                    recursive=True,
                    max_depth=None,
                    include_hidden=False,
                    include_time_machine=False,
                    summary=summary,
                )

        self.assertEqual(findings, [])
        self.assertEqual(summary.skipped, 1)
        run_mock.assert_not_called()

    def test_include_hidden_scans_top_level_hidden_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / ".broken.txt"
            target.write_text("data")

            def fake_run(args: list[str]):
                if args[0] == "xattr":
                    return mock.Mock(returncode=1, stdout="", stderr="Invalid argument")
                if args[0] == "stat":
                    return mock.Mock(returncode=0, stdout="arch\n", stderr="")
                raise AssertionError(args)

            summary = repair_xattrs.RepairSummary()
            with mock.patch("timecapsulesmb.repair_xattrs.run_capture", side_effect=fake_run):
                findings = repair_xattrs.find_findings(
                    target,
                    recursive=True,
                    max_depth=None,
                    include_hidden=True,
                    include_time_machine=False,
                    summary=summary,
                )

        self.assertEqual([finding.path for finding in findings], [target.resolve()])

    def test_skips_bundle_like_directories_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = root / "Library.photoslibrary"
            bundle.mkdir()
            (bundle / "inside.txt").write_text("data")
            (root / "outside.txt").write_text("data")

            def fake_run(args: list[str]):
                if args[0] == "xattr":
                    return mock.Mock(returncode=1, stdout="", stderr="Invalid argument")
                if args[0] == "stat":
                    return mock.Mock(returncode=0, stdout="arch\n", stderr="")
                raise AssertionError(args)

            summary = repair_xattrs.RepairSummary()
            with mock.patch("timecapsulesmb.repair_xattrs.run_capture", side_effect=fake_run):
                findings = repair_xattrs.find_findings(
                    root,
                    recursive=True,
                    max_depth=None,
                    include_hidden=False,
                    include_time_machine=False,
                    summary=summary,
                )

        self.assertEqual([finding.path.name for finding in findings], ["outside.txt"])
        self.assertEqual(summary.skipped, 1)

    def test_skips_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target.txt"
            target.write_text("data")
            link = root / "link.txt"
            link.symlink_to(target)

            def fake_run(args: list[str]):
                if args[0] == "xattr":
                    return mock.Mock(returncode=1, stdout="", stderr="Invalid argument")
                if args[0] == "stat":
                    return mock.Mock(returncode=0, stdout="arch\n", stderr="")
                raise AssertionError(args)

            summary = repair_xattrs.RepairSummary()
            with mock.patch("timecapsulesmb.repair_xattrs.run_capture", side_effect=fake_run):
                findings = repair_xattrs.find_findings(
                    root,
                    recursive=True,
                    max_depth=None,
                    include_hidden=False,
                    include_time_machine=False,
                    summary=summary,
                )

        self.assertEqual([finding.path.name for finding in findings], ["target.txt"])
        self.assertEqual(summary.skipped, 1)

    def test_no_recursive_skips_nested_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "top.txt").write_text("top")
            nested = root / "nested"
            nested.mkdir()
            (nested / "deep.txt").write_text("deep")

            def fake_run(args: list[str]):
                if args[0] == "xattr":
                    return mock.Mock(returncode=1, stdout="", stderr="")
                if args[0] == "stat":
                    return mock.Mock(returncode=0, stdout="arch\n", stderr="")
                raise AssertionError(args)

            summary = repair_xattrs.RepairSummary()
            with mock.patch("timecapsulesmb.repair_xattrs.run_capture", side_effect=fake_run):
                findings = repair_xattrs.find_findings(
                    root,
                    recursive=False,
                    max_depth=None,
                    include_hidden=False,
                    include_time_machine=False,
                    summary=summary,
                )

        self.assertEqual([finding.path.name for finding in findings], ["top.txt"])
        self.assertEqual(summary.skipped, 1)

    def test_max_depth_limits_nested_scan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            level1 = root / "level1"
            level1.mkdir()
            (level1 / "one.txt").write_text("one")
            level2 = level1 / "level2"
            level2.mkdir()
            (level2 / "two.txt").write_text("two")

            def fake_run(args: list[str]):
                if args[0] == "xattr":
                    return mock.Mock(returncode=1, stdout="", stderr="")
                if args[0] == "stat":
                    return mock.Mock(returncode=0, stdout="arch\n", stderr="")
                raise AssertionError(args)

            summary = repair_xattrs.RepairSummary()
            with mock.patch("timecapsulesmb.repair_xattrs.run_capture", side_effect=fake_run):
                findings = repair_xattrs.find_findings(
                    root,
                    recursive=True,
                    max_depth=1,
                    include_hidden=False,
                    include_time_machine=False,
                    summary=summary,
                )

        self.assertEqual([finding.path.name for finding in findings], ["one.txt"])
        self.assertEqual(summary.skipped, 1)

    def test_single_file_path_scans_that_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "broken.txt"
            target.write_text("data")

            def fake_run(args: list[str]):
                if args[0] == "xattr":
                    return mock.Mock(returncode=1, stdout="", stderr="")
                if args[0] == "stat":
                    return mock.Mock(returncode=0, stdout="arch\n", stderr="")
                raise AssertionError(args)

            summary = repair_xattrs.RepairSummary()
            with mock.patch("timecapsulesmb.repair_xattrs.run_capture", side_effect=fake_run):
                findings = repair_xattrs.find_findings(
                    target,
                    recursive=True,
                    max_depth=None,
                    include_hidden=False,
                    include_time_machine=False,
                    summary=summary,
                )

        self.assertEqual([finding.path for finding in findings], [target.resolve()])

    def test_parse_mounted_smb_shares_decodes_mount_output(self) -> None:
        shares = repair_xattrs.parse_mounted_smb_shares(
            "//James%20Chang@timecapsulesamba4.local/Data on /Volumes/Data (smbfs, nodev)\n"
            "//James%20Chang@AirPort._afpovertcp._tcp.local/Data on /Volumes/AfpData (afpfs, nodev)\n"
        )

        self.assertEqual(shares, [repair_xattrs.MountedSmbShare("timecapsulesamba4.local", "Data", Path("/Volumes/Data"))])

    def test_default_share_path_uses_env_host_and_share_name_when_smb_mounted(self) -> None:
        env = {"TC_HOST": "root@192.168.1.217", "TC_SHARE_NAME": "Data"}
        shares = [
            repair_xattrs.MountedSmbShare("10.0.0.2", "Data", Path("/Volumes/WrongData")),
            repair_xattrs.MountedSmbShare("192.168.1.217", "Data", Path("/Volumes/Data")),
        ]
        self.assertEqual(
            repair_xattrs_domain.default_share_path_from_config(
                self.app_config(env),
                shares=shares,
                path_exists_func=lambda _path: True,
            ),
            Path("/Volumes/Data"),
        )

    def test_default_share_path_uses_unique_matching_smb_share_when_host_label_differs(self) -> None:
        env = {"TC_HOST": "root@192.168.1.217", "TC_SHARE_NAME": "Data"}
        shares = [repair_xattrs.MountedSmbShare("timecapsulesamba4.local", "Data", Path("/Volumes/Data-1"))]
        self.assertEqual(
            repair_xattrs_domain.default_share_path_from_config(
                self.app_config(env),
                shares=shares,
                path_exists_func=lambda _path: True,
            ),
            Path("/Volumes/Data-1"),
        )

    def test_default_share_path_ignores_afp_mount_with_matching_volume_name(self) -> None:
        env = {"TC_HOST": "root@192.168.1.217", "TC_SHARE_NAME": "Data"}
        mount_output = "//James%20Chang@AirPort._afpovertcp._tcp.local/Data on /Volumes/Data (afpfs, nodev)\n"
        with mock.patch("timecapsulesmb.repair_xattrs.run_capture", return_value=mock.Mock(returncode=0, stdout=mount_output)):
            self.assertIsNone(repair_xattrs_domain.default_share_path_from_config(self.app_config(env)))

    def test_default_share_path_ignores_inaccessible_smb_mountpoints(self) -> None:
        env = {"TC_HOST": "root@192.168.1.217", "TC_SHARE_NAME": "Data"}
        shares = [
            repair_xattrs.MountedSmbShare("Time Capsule Samba 4._smb._tcp.local", "Data", Path("/Volumes/.timemachine/Data")),
            repair_xattrs.MountedSmbShare("192.168.1.217", "Data", Path("/Volumes/Data")),
        ]

        def fake_path_exists(path: Path) -> bool:
            if str(path).startswith("/Volumes/.timemachine"):
                return False
            return True

        self.assertEqual(
            repair_xattrs_domain.default_share_path_from_config(
                self.app_config(env),
                shares=shares,
                path_exists_func=fake_path_exists,
            ),
            Path("/Volumes/Data"),
        )

    def test_default_share_path_rejects_ambiguous_matching_smb_shares(self) -> None:
        env = {"TC_HOST": "root@192.168.1.217", "TC_SHARE_NAME": "Data"}
        shares = [
            repair_xattrs.MountedSmbShare("timecapsule-a.local", "Data", Path("/Volumes/Data")),
            repair_xattrs.MountedSmbShare("timecapsule-b.local", "Data", Path("/Volumes/Data-1")),
        ]
        with self.assertRaises(RuntimeError) as cm:
            repair_xattrs_domain.default_share_path_from_config(
                self.app_config(env),
                shares=shares,
                path_exists_func=lambda _path: True,
            )
        self.assertIn("multiple mounted SMB shares", str(cm.exception))

    def test_default_share_path_returns_none_when_share_missing(self) -> None:
        self.assertIsNone(
            repair_xattrs_domain.default_share_path_from_config(
                self.app_config({"TC_HOST": "root@192.168.1.217", "TC_SHARE_NAME": "Data"}),
                shares=[],
            )
        )

    def test_default_share_path_rejects_invalid_env_share_name(self) -> None:
        with self.assertRaises(RuntimeError) as cm:
            repair_xattrs_domain.default_share_path_from_config(
                self.app_config({"TC_HOST": "root@192.168.1.217", "TC_SHARE_NAME": "Bad/Share"}),
                shares=[],
            )
        self.assertIn("TC_SHARE_NAME is invalid", str(cm.exception))

    def test_explicit_repair_path_does_not_require_valid_env_share_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "file.txt"
            target.write_text("data")
            with mock.patch("timecapsulesmb.cli.repair_xattrs.sys.platform", "darwin"):
                with mock.patch("timecapsulesmb.cli.repair_xattrs.find_findings", return_value=[]):
                    with redirect_stdout(io.StringIO()):
                        rc = repair_xattrs.main(["--path", str(target)])
        self.assertEqual(rc, 0)
        self.path_guard_mock.assert_called_with(target)

    def test_validate_repair_root_accepts_path_under_volumes(self) -> None:
        self.assertEqual(
            repair_xattrs_domain.validate_repair_root_under_volumes(Path("/Volumes/Data/Subdir")),
            Path("/Volumes/Data/Subdir"),
        )

    def test_validate_repair_root_rejects_root_directory(self) -> None:
        with self.assertRaises(RuntimeError) as cm:
            repair_xattrs_domain.validate_repair_root_under_volumes(Path("/"))
        self.assertIn("can only scan mounted volumes under /Volumes", str(cm.exception))
        self.assertIn("Refusing to scan: /", str(cm.exception))

    def test_validate_repair_root_rejects_home_directory(self) -> None:
        with self.assertRaises(RuntimeError) as cm:
            repair_xattrs_domain.validate_repair_root_under_volumes(Path("/Users/example"))
        self.assertIn("can only scan mounted volumes under /Volumes", str(cm.exception))

    def test_validate_repair_root_rejects_volumes_root(self) -> None:
        with self.assertRaises(RuntimeError) as cm:
            repair_xattrs_domain.validate_repair_root_under_volumes(Path("/Volumes"))
        self.assertIn("requires a mounted volume below /Volumes", str(cm.exception))

    def test_explicit_inaccessible_repair_path_reports_clean_error(self) -> None:
        target = Path("/Volumes/.timemachine/Data")
        summary = repair_xattrs.RepairSummary()

        with mock.patch("pathlib.Path.resolve", return_value=target):
            with mock.patch("pathlib.Path.is_file", side_effect=PermissionError("permission denied")):
                with self.assertRaises(RuntimeError) as cm:
                    list(
                        repair_xattrs.iter_scan_paths(
                            target,
                            recursive=True,
                            max_depth=None,
                            include_hidden=False,
                            include_time_machine=False,
                            include_directories=True,
                            summary=summary,
                        )
                    )

        self.assertIn("Cannot access path", str(cm.exception))
        self.assertIn("permission denied", str(cm.exception))

    def test_dry_run_and_yes_are_mutually_exclusive(self) -> None:
        with mock.patch("timecapsulesmb.cli.repair_xattrs.sys.platform", "darwin"):
            with redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    repair_xattrs.main(["--dry-run", "--yes", "--path", "/tmp"])

    def test_negative_max_depth_is_rejected(self) -> None:
        with mock.patch("timecapsulesmb.cli.repair_xattrs.sys.platform", "darwin"):
            with redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    repair_xattrs.main(["--max-depth", "-1", "--path", "/tmp"])

    def test_non_macos_is_rejected(self) -> None:
        with mock.patch("timecapsulesmb.cli.repair_xattrs.sys.platform", "linux"):
            with self.assertRaises(SystemExit) as cm:
                repair_xattrs.main(["--path", "/tmp"])
        self.assertIn("must be run on macOS", str(cm.exception))

    def test_missing_default_path_is_rejected(self) -> None:
        with mock.patch("timecapsulesmb.cli.repair_xattrs.sys.platform", "darwin"):
            with mock.patch(
                "timecapsulesmb.cli.repair_xattrs.load_env_config",
                return_value=self.app_config({"TC_HOST": "root@192.168.1.217", "TC_SHARE_NAME": "Data"}),
            ):
                with mock.patch("timecapsulesmb.cli.repair_xattrs.mounted_smb_shares", return_value=[]):
                    with self.assertRaises(SystemExit) as cm:
                        repair_xattrs.main([])
        self.assertIn("Pass --path explicitly", str(cm.exception))

    def test_subprocess_output_decodes_invalid_xattr_bytes(self) -> None:
        proc = repair_xattrs.run_capture([
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write(b'bad\\\\xffbytes')",
        ])
        self.assertEqual(proc.returncode, 0)
        self.assertIn("bad", proc.stdout)


if __name__ == "__main__":
    unittest.main()
