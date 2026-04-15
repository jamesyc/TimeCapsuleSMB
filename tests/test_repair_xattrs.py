from __future__ import annotations

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from timecapsulesmb.cli import repair_xattrs


class RepairXattrsTests(unittest.TestCase):
    def test_finds_arch_file_when_xattr_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "broken.txt"
            target.write_text("data")

            def fake_run(args: list[str]):
                if args[0] == "xattr":
                    return mock.Mock(returncode=1, stdout="", stderr="Invalid argument")
                if args[0] == "stat":
                    return mock.Mock(returncode=0, stdout="arch\n", stderr="")
                raise AssertionError(args)

            summary = repair_xattrs.RepairSummary()
            with mock.patch("timecapsulesmb.cli.repair_xattrs.run_capture", side_effect=fake_run):
                candidates = repair_xattrs.find_candidates(
                    root,
                    recursive=True,
                    max_depth=None,
                    include_hidden=False,
                    include_time_machine=False,
                    summary=summary,
                )

        self.assertEqual([candidate.path.name for candidate in candidates], ["broken.txt"])
        self.assertEqual(summary.scanned, 1)
        self.assertEqual(summary.repairable, 1)

    def test_does_not_repair_when_xattr_is_readable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "ok.txt").write_text("data")

            with mock.patch("timecapsulesmb.cli.repair_xattrs.run_capture", return_value=mock.Mock(returncode=0, stdout="", stderr="")):
                summary = repair_xattrs.RepairSummary()
                candidates = repair_xattrs.find_candidates(
                    root,
                    recursive=True,
                    max_depth=None,
                    include_hidden=False,
                    include_time_machine=False,
                    summary=summary,
                )

        self.assertEqual(candidates, [])
        self.assertEqual(summary.scanned, 1)

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
            with mock.patch("timecapsulesmb.cli.repair_xattrs.run_capture", side_effect=fake_run):
                candidates = repair_xattrs.find_candidates(
                    root,
                    recursive=True,
                    max_depth=None,
                    include_hidden=False,
                    include_time_machine=False,
                    summary=summary,
                )

        self.assertEqual(candidates, [])
        self.assertEqual(summary.scanned, 1)

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
            with mock.patch("timecapsulesmb.cli.repair_xattrs.run_capture", side_effect=fake_run):
                candidates = repair_xattrs.find_candidates(
                    root,
                    recursive=True,
                    max_depth=None,
                    include_hidden=False,
                    include_time_machine=False,
                    summary=summary,
                )

        self.assertEqual([candidate.path.name for candidate in candidates], ["broken.txt"])
        self.assertEqual(candidates[0].flags, "arch,nodump")

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
            with mock.patch("timecapsulesmb.cli.repair_xattrs.run_capture", side_effect=fake_run):
                candidates = repair_xattrs.find_candidates(
                    root,
                    recursive=True,
                    max_depth=None,
                    include_hidden=False,
                    include_time_machine=False,
                    summary=summary,
                )

        self.assertEqual(candidates, [])
        self.assertEqual(summary.scanned, 1)

    def test_dry_run_does_not_call_chflags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "broken.txt").write_text("data")

            def fake_run(args: list[str]):
                if args[0] == "xattr":
                    return mock.Mock(returncode=1, stdout="", stderr="Invalid argument")
                if args[0] == "stat":
                    return mock.Mock(returncode=0, stdout="arch\n", stderr="")
                if args[0] == "chflags":
                    raise AssertionError("dry-run should not call chflags")
                raise AssertionError(args)

            output = io.StringIO()
            with mock.patch("timecapsulesmb.cli.repair_xattrs.sys.platform", "darwin"):
                with mock.patch("timecapsulesmb.cli.repair_xattrs.run_capture", side_effect=fake_run):
                    with redirect_stdout(output):
                        rc = repair_xattrs.main(["--path", str(root), "--dry-run"])

        self.assertEqual(rc, 0)
        self.assertIn("Would repair:", output.getvalue())
        self.assertIn("No changes made.", output.getvalue())

    def test_apply_repairs_after_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "broken.txt"
            target.write_text("data")
            repaired = False

            def fake_run(args: list[str]):
                nonlocal repaired
                if args[0] == "xattr":
                    return mock.Mock(returncode=0 if repaired else 1, stdout="", stderr="")
                if args[0] == "stat":
                    return mock.Mock(returncode=0, stdout="arch\n", stderr="")
                if args[0] == "chflags":
                    repaired = True
                    return mock.Mock(returncode=0, stdout="", stderr="")
                raise AssertionError(args)

            output = io.StringIO()
            with mock.patch("timecapsulesmb.cli.repair_xattrs.sys.platform", "darwin"):
                with mock.patch("timecapsulesmb.cli.repair_xattrs.run_capture", side_effect=fake_run):
                    with mock.patch("builtins.input", return_value="y"):
                        with redirect_stdout(output):
                            rc = repair_xattrs.main(["--path", str(root)])

        self.assertEqual(rc, 0)
        self.assertTrue(repaired)
        self.assertIn("PASS xattr now readable", output.getvalue())

    def test_apply_yes_skips_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "broken.txt").write_text("data")
            repaired = False

            def fake_run(args: list[str]):
                nonlocal repaired
                if args[0] == "xattr":
                    return mock.Mock(returncode=0 if repaired else 1, stdout="", stderr="")
                if args[0] == "stat":
                    return mock.Mock(returncode=0, stdout="arch\n", stderr="")
                if args[0] == "chflags":
                    repaired = True
                    return mock.Mock(returncode=0, stdout="", stderr="")
                raise AssertionError(args)

            with mock.patch("timecapsulesmb.cli.repair_xattrs.sys.platform", "darwin"):
                with mock.patch("timecapsulesmb.cli.repair_xattrs.run_capture", side_effect=fake_run):
                    with mock.patch("builtins.input") as input_mock:
                        with redirect_stdout(io.StringIO()):
                            rc = repair_xattrs.main(["--path", str(root), "--yes"])

        self.assertEqual(rc, 0)
        input_mock.assert_not_called()

    def test_prompt_decline_does_not_repair(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "broken.txt").write_text("data")

            def fake_run(args: list[str]):
                if args[0] == "xattr":
                    return mock.Mock(returncode=1, stdout="", stderr="")
                if args[0] == "stat":
                    return mock.Mock(returncode=0, stdout="arch\n", stderr="")
                if args[0] == "chflags":
                    raise AssertionError("declining prompt should not repair")
                raise AssertionError(args)

            output = io.StringIO()
            with mock.patch("timecapsulesmb.cli.repair_xattrs.sys.platform", "darwin"):
                with mock.patch("timecapsulesmb.cli.repair_xattrs.run_capture", side_effect=fake_run):
                    with mock.patch("builtins.input", return_value="n"):
                        with redirect_stdout(output):
                            rc = repair_xattrs.main(["--path", str(root)])

        self.assertEqual(rc, 0)
        self.assertIn("No changes made.", output.getvalue())

    def test_no_candidates_does_not_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "ok.txt").write_text("data")

            with mock.patch("timecapsulesmb.cli.repair_xattrs.sys.platform", "darwin"):
                with mock.patch("timecapsulesmb.cli.repair_xattrs.run_capture", return_value=mock.Mock(returncode=0, stdout="", stderr="")):
                    with mock.patch("builtins.input") as input_mock:
                        output = io.StringIO()
                        with redirect_stdout(output):
                            rc = repair_xattrs.main(["--path", str(root)])

        self.assertEqual(rc, 0)
        input_mock.assert_not_called()
        self.assertIn("No repairable files found.", output.getvalue())

    def test_repair_failure_returns_nonzero_when_xattr_still_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "broken.txt").write_text("data")

            def fake_run(args: list[str]):
                if args[0] == "xattr":
                    return mock.Mock(returncode=1, stdout="", stderr="")
                if args[0] == "stat":
                    return mock.Mock(returncode=0, stdout="arch\n", stderr="")
                if args[0] == "chflags":
                    return mock.Mock(returncode=0, stdout="", stderr="")
                raise AssertionError(args)

            output = io.StringIO()
            with mock.patch("timecapsulesmb.cli.repair_xattrs.sys.platform", "darwin"):
                with mock.patch("timecapsulesmb.cli.repair_xattrs.run_capture", side_effect=fake_run):
                    with redirect_stdout(output):
                        rc = repair_xattrs.main(["--path", str(root), "--yes"])

        self.assertEqual(rc, 1)
        self.assertIn("FAIL repair did not make xattr readable", output.getvalue())

    def test_repair_failure_returns_nonzero_when_chflags_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "broken.txt").write_text("data")

            def fake_run(args: list[str]):
                if args[0] == "xattr":
                    return mock.Mock(returncode=1, stdout="", stderr="")
                if args[0] == "stat":
                    return mock.Mock(returncode=0, stdout="arch\n", stderr="")
                if args[0] == "chflags":
                    return mock.Mock(returncode=1, stdout="", stderr="nope")
                raise AssertionError(args)

            with mock.patch("timecapsulesmb.cli.repair_xattrs.sys.platform", "darwin"):
                with mock.patch("timecapsulesmb.cli.repair_xattrs.run_capture", side_effect=fake_run):
                    with redirect_stdout(io.StringIO()):
                        rc = repair_xattrs.main(["--path", str(root), "--yes"])

        self.assertEqual(rc, 1)

    def test_repair_failure_when_size_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "broken.txt"
            target.write_text("data")

            def fake_run(args: list[str]):
                if args[0] == "xattr":
                    return mock.Mock(returncode=1, stdout="", stderr="")
                if args[0] == "stat":
                    return mock.Mock(returncode=0, stdout="arch\n", stderr="")
                if args[0] == "chflags":
                    target.write_text("changed")
                    return mock.Mock(returncode=0, stdout="", stderr="")
                raise AssertionError(args)

            with mock.patch("timecapsulesmb.cli.repair_xattrs.sys.platform", "darwin"):
                with mock.patch("timecapsulesmb.cli.repair_xattrs.run_capture", side_effect=fake_run):
                    with redirect_stdout(io.StringIO()):
                        rc = repair_xattrs.main(["--path", str(root), "--yes"])

        self.assertEqual(rc, 1)

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
            with mock.patch("timecapsulesmb.cli.repair_xattrs.run_capture", side_effect=fake_run):
                candidates = repair_xattrs.find_candidates(
                    root,
                    recursive=True,
                    max_depth=None,
                    include_hidden=False,
                    include_time_machine=False,
                    summary=summary,
                )

        self.assertEqual([candidate.path.name for candidate in candidates], ["visible.txt"])
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
            with mock.patch("timecapsulesmb.cli.repair_xattrs.run_capture", side_effect=fake_run):
                candidates = repair_xattrs.find_candidates(
                    root,
                    recursive=True,
                    max_depth=None,
                    include_hidden=True,
                    include_time_machine=True,
                    summary=summary,
                )

        self.assertEqual(sorted(candidate.path.name for candidate in candidates), [".hidden.txt", "backup.txt"])
        self.assertEqual(summary.skipped, 0)

    def test_skips_top_level_hidden_file_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / ".broken.txt"
            target.write_text("data")

            summary = repair_xattrs.RepairSummary()
            with mock.patch("timecapsulesmb.cli.repair_xattrs.run_capture") as run_mock:
                candidates = repair_xattrs.find_candidates(
                    target,
                    recursive=True,
                    max_depth=None,
                    include_hidden=False,
                    include_time_machine=False,
                    summary=summary,
                )

        self.assertEqual(candidates, [])
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
            with mock.patch("timecapsulesmb.cli.repair_xattrs.run_capture", side_effect=fake_run):
                candidates = repair_xattrs.find_candidates(
                    target,
                    recursive=True,
                    max_depth=None,
                    include_hidden=True,
                    include_time_machine=False,
                    summary=summary,
                )

        self.assertEqual([candidate.path for candidate in candidates], [target.resolve()])

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
            with mock.patch("timecapsulesmb.cli.repair_xattrs.run_capture", side_effect=fake_run):
                candidates = repair_xattrs.find_candidates(
                    root,
                    recursive=True,
                    max_depth=None,
                    include_hidden=False,
                    include_time_machine=False,
                    summary=summary,
                )

        self.assertEqual([candidate.path.name for candidate in candidates], ["outside.txt"])
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
            with mock.patch("timecapsulesmb.cli.repair_xattrs.run_capture", side_effect=fake_run):
                candidates = repair_xattrs.find_candidates(
                    root,
                    recursive=True,
                    max_depth=None,
                    include_hidden=False,
                    include_time_machine=False,
                    summary=summary,
                )

        self.assertEqual([candidate.path.name for candidate in candidates], ["target.txt"])
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
            with mock.patch("timecapsulesmb.cli.repair_xattrs.run_capture", side_effect=fake_run):
                candidates = repair_xattrs.find_candidates(
                    root,
                    recursive=False,
                    max_depth=None,
                    include_hidden=False,
                    include_time_machine=False,
                    summary=summary,
                )

        self.assertEqual([candidate.path.name for candidate in candidates], ["top.txt"])
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
            with mock.patch("timecapsulesmb.cli.repair_xattrs.run_capture", side_effect=fake_run):
                candidates = repair_xattrs.find_candidates(
                    root,
                    recursive=True,
                    max_depth=1,
                    include_hidden=False,
                    include_time_machine=False,
                    summary=summary,
                )

        self.assertEqual([candidate.path.name for candidate in candidates], ["one.txt"])
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
            with mock.patch("timecapsulesmb.cli.repair_xattrs.run_capture", side_effect=fake_run):
                candidates = repair_xattrs.find_candidates(
                    target,
                    recursive=True,
                    max_depth=None,
                    include_hidden=False,
                    include_time_machine=False,
                    summary=summary,
                )

        self.assertEqual([candidate.path for candidate in candidates], [target.resolve()])

    def test_default_share_path_uses_env_share_name_when_mounted(self) -> None:
        with mock.patch("timecapsulesmb.cli.repair_xattrs.parse_env_values", return_value={"TC_SHARE_NAME": "Data"}):
            with mock.patch("pathlib.Path.exists", return_value=True):
                self.assertEqual(repair_xattrs.default_share_path(), Path("/Volumes/Data"))

    def test_default_share_path_returns_none_when_share_missing(self) -> None:
        with mock.patch("timecapsulesmb.cli.repair_xattrs.parse_env_values", return_value={"TC_SHARE_NAME": "Data"}):
            with mock.patch("pathlib.Path.exists", return_value=False):
                self.assertIsNone(repair_xattrs.default_share_path())

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
            with mock.patch("timecapsulesmb.cli.repair_xattrs.default_share_path", return_value=None):
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
