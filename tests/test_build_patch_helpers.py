from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HELPERS = ROOT / "build/_patch_helpers.sh"
SAMBA_SERIES = ROOT / "build/patches/samba4x/series"


def run(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True)


def apply_series(series: Path, workdir: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", "-c", '. "$1"; patch_apply_series Test "$2" "$3"', "sh", str(HELPERS), str(series), str(workdir)],
        capture_output=True,
        text=True,
    )


def series_patches(series: Path) -> list[Path]:
    names = []
    for line in series.read_text().splitlines():
        if line and not line.startswith("#"):
            names.append(series.parent / line.split("|", 1)[0])
    return names


class PatchSeriesOverlayTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.source = base / "source"
        self.patches = base / "patches"
        self.source.mkdir()
        self.patches.mkdir()
        (self.source / "lib").mkdir()
        (self.source / "lib/upstream.c").write_text("int upstream;\n")
        run("git", "init", "-q", cwd=self.source)
        run("git", "add", "-A", cwd=self.source)
        run("git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base", cwd=self.source)
        # One patch that hooks the overlay code into an upstream file.
        (self.patches / "0001-hook.patch").write_text(
            "diff --git a/lib/upstream.c b/lib/upstream.c\n"
            "--- a/lib/upstream.c\n"
            "+++ b/lib/upstream.c\n"
            "@@ -1 +1,2 @@\n"
            " int upstream;\n"
            '+#include "tc_ours.c"\n'
        )
        (self.patches / "series").write_text("# comment\n0001-hook.patch|hook\n")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write_overlay(self, relative: str, text: str) -> None:
        path = self.patches / "overlay" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def test_overlay_files_are_copied_into_new_directories_and_patches_applied(self) -> None:
        self.write_overlay("lib/tc_ours.c", "int ours;\n")
        self.write_overlay("new/dir/tc_other.h", "#define OTHER 1\n")

        result = apply_series(self.patches / "series", self.source)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.source / "lib/tc_ours.c").read_text(), "int ours;\n")
        self.assertEqual((self.source / "new/dir/tc_other.h").read_text(), "#define OTHER 1\n")
        self.assertEqual((self.source / "lib/upstream.c").read_text(), 'int upstream;\n#include "tc_ours.c"\n')

    def test_overlay_skips_dotfiles_and_keeps_paths_with_spaces_whole(self) -> None:
        self.write_overlay("lib/tc_ours.c", "int ours;\n")
        self.write_overlay(".DS_Store", "finder\n")
        self.write_overlay("lib/.DS_Store", "finder\n")
        self.write_overlay("docs/two words.txt", "spaced\n")

        result = apply_series(self.patches / "series", self.source)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.source / ".DS_Store").exists())
        self.assertFalse((self.source / "lib/.DS_Store").exists())
        self.assertEqual((self.source / "docs/two words.txt").read_text(), "spaced\n")
        self.assertFalse((self.source / "docs/two").exists())

    def test_overlay_never_overwrites_an_existing_file_and_leaves_the_tree_untouched(self) -> None:
        # Sorted before the colliding path, so a copy-as-you-check loop would
        # already have written it.
        self.write_overlay("a/tc_first.c", "int first;\n")
        self.write_overlay("lib/upstream.c", "int replaced;\n")

        result = apply_series(self.patches / "series", self.source)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("overlay file lib/upstream.c already exists", result.stderr)
        self.assertEqual((self.source / "lib/upstream.c").read_text(), "int upstream;\n")
        self.assertFalse((self.source / "a").exists())
        self.assertEqual(run("git", "status", "--porcelain", cwd=self.source).stdout, "")

    def test_rerun_on_a_patched_tree_is_refused_until_the_checkout_is_cleaned(self) -> None:
        self.write_overlay("lib/tc_ours.c", "int ours;\n")
        self.assertEqual(apply_series(self.patches / "series", self.source).returncode, 0)

        rerun = apply_series(self.patches / "series", self.source)
        self.assertNotEqual(rerun.returncode, 0)
        self.assertIn("overlay file lib/tc_ours.c already exists", rerun.stderr)

        # The downloader's reset --hard and clean -fd remove the untracked
        # overlay copies, after which the series applies again.
        run("git", "reset", "-q", "--hard", cwd=self.source)
        run("git", "clean", "-qfd", cwd=self.source)
        again = apply_series(self.patches / "series", self.source)
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual((self.source / "lib/tc_ours.c").read_text(), "int ours;\n")

    def test_series_without_overlay_directory_applies_patches_only(self) -> None:
        (self.patches / "0001-hook.patch").write_text(
            "diff --git a/lib/upstream.c b/lib/upstream.c\n"
            "--- a/lib/upstream.c\n"
            "+++ b/lib/upstream.c\n"
            "@@ -1 +1 @@\n"
            "-int upstream;\n"
            "+int patched;\n"
        )

        result = apply_series(self.patches / "series", self.source)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.source / "lib/upstream.c").read_text(), "int patched;\n")
        self.assertEqual(sorted(p.name for p in self.source.iterdir() if p.name != ".git"), ["lib"])


class SambaSeriesLayoutTests(unittest.TestCase):
    def test_every_listed_patch_exists_and_every_patch_is_listed(self) -> None:
        listed = series_patches(SAMBA_SERIES)
        for patch in listed:
            self.assertTrue(patch.is_file(), patch)
        on_disk = sorted(SAMBA_SERIES.parent.glob("*.patch"))
        self.assertEqual(sorted(listed), on_disk)

    def test_no_patch_adds_or_edits_an_overlay_file(self) -> None:
        # Overlay files are kept in their final form; a patch touching one
        # would bring back patches to patches (or collide with the copy).
        overlay = SAMBA_SERIES.parent / "overlay"
        overlay_paths = {str(p.relative_to(overlay)) for p in overlay.rglob("*") if p.is_file()}
        self.assertTrue(overlay_paths)
        for patch in series_patches(SAMBA_SERIES):
            numstat = subprocess.run(
                ["git", "apply", "--numstat", str(patch)], cwd=ROOT, capture_output=True, text=True, check=True
            ).stdout
            touched = {line.split("\t")[2] for line in numstat.splitlines()}
            self.assertFalse(touched & overlay_paths, patch.name)


if __name__ == "__main__":
    unittest.main()
