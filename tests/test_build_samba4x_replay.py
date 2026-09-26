"""The Samba patch-series replay tool, on a tiny fake Samba and series.

The fixture upstream has what the real tree has and the tool must survive: a
.gitignore that matches a file a patch creates, a file that is not UTF-8, and a
CRLF file. The fixture patches are written by hand, as git would write them
without index lines, so an init/export round trip must give them back byte for
byte.
"""
from __future__ import annotations

import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("samba4x_replay", ROOT / "build/samba4x_replay.py")
replay_tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(replay_tool)

REF = "samba-test"
A_C = b"".join(b"\ta%d;\n" % i for i in range(1, 9))
LATIN = b"one\n\xe9t\xe9\ntwo\n"
CRLF = b"l1\r\nl2\r\nl3\r\n"

PATCH_1 = (b"diff --git a/lib/a.c b/lib/a.c\n"
           b"--- a/lib/a.c\n"
           b"+++ b/lib/a.c\n"
           b"@@ -1,4 +1,5 @@\n"
           b" \ta1;\n"
           b"+#include \"tc_ours.c\"\n"
           b" \ta2;\n"
           b" \ta3;\n"
           b" \ta4;\n")
# Depends on 0001 for its line numbers, and creates a file .gitignore matches.
PATCH_2 = (b"diff --git a/lib/a.c b/lib/a.c\n"
           b"--- a/lib/a.c\n"
           b"+++ b/lib/a.c\n"
           b"@@ -5,5 +5,5 @@\n"
           b" \ta4;\n"
           b" \ta5;\n"
           b" \ta6;\n"
           b"-\ta7;\n"
           b"+\ta7 = 7;\n"
           b" \ta8;\n"
           b"diff --git a/out/gen.o b/out/gen.o\n"
           b"new file mode 100644\n"
           b"--- /dev/null\n"
           b"+++ b/out/gen.o\n"
           b"@@ -0,0 +1 @@\n"
           b"+generated\n")
# Its context line is Latin-1, not UTF-8.
PATCH_3 = (b"diff --git a/lib/latin.c b/lib/latin.c\n"
           b"--- a/lib/latin.c\n"
           b"+++ b/lib/latin.c\n"
           b"@@ -1,3 +1,3 @@\n"
           b" one\n"
           b" \xe9t\xe9\n"
           b"-two\n"
           b"+three\n")
PATCHES = {"0001-hook.patch": PATCH_1, "0002-tail.patch": PATCH_2, "0003-latin.patch": PATCH_3}


def sh(*args: str, cwd: Path) -> str:
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout


def git(repo: Path, *args: str) -> str:
    return sh("git", *args, cwd=repo).strip()


class Fixture:
    def __init__(self, tmp: Path) -> None:
        self.upstream = tmp / "upstream"
        self.root = tmp / "root"
        self.replay = tmp / "replay"
        self.pdir = self.root / "build/patches/samba4x"
        files = {"lib/a.c": A_C, "lib/latin.c": LATIN, "win/crlf.txt": CRLF,
                 ".gitignore": b"*.o\n/bin/\n"}
        for name, data in files.items():
            (self.upstream / name).parent.mkdir(parents=True, exist_ok=True)
            (self.upstream / name).write_bytes(data)
        git(self.upstream, "init", "-q")
        git(self.upstream, "add", "-A")
        git(self.upstream, "-c", "user.email=u@u", "-c", "user.name=u", "commit", "-qm", "upstream")
        git(self.upstream, "tag", REF)

        (self.root / "build").mkdir(parents=True)
        shutil.copy(ROOT / "build/_patch_helpers.sh", self.root / "build/_patch_helpers.sh")
        self.set_ref(REF)
        (self.pdir / "overlay/lib").mkdir(parents=True)
        (self.pdir / "overlay/lib/tc_ours.c").write_bytes(b"int ours;\n")
        (self.pdir / "overlay/lib/tc_old.h").write_bytes(b"#define OLD 1\n")
        (self.pdir / "overlay/.DS_Store").write_bytes(b"finder\n")
        for name, data in PATCHES.items():
            (self.pdir / name).write_bytes(data)
        self.write_series(list(PATCHES))

    def set_ref(self, ref: str) -> None:
        (self.root / "build/env.sh").write_text(
            f'SAMBA4X_GIT_URL="file://{self.upstream}"\nSAMBA4X_GIT_REF="{ref}"\n')

    def write_series(self, names: list[str]) -> None:
        (self.pdir / "series").write_text(
            "# Fixture series\n" + "".join(f"{n}|{n[5:-6]}\n" for n in names))

    def run(self, *args: str) -> int:
        return replay_tool.main(["--root", str(self.root), *args])

    def init(self) -> None:
        assert self.run("init", str(self.replay)) == 0

    def amend(self, subject: str, script: str) -> int:
        path = self.replay.parent / "edit.py"
        path.write_text(script)
        return self.run("amend", str(self.replay), subject, str(path))

    def subjects(self) -> list[str]:
        return git(self.replay, "log", "--reverse", "--format=%s").splitlines()

    def head(self) -> str:
        return git(self.replay, "rev-parse", "HEAD")

    def rebasing(self) -> bool:
        return replay_tool.rebase_in_progress(self.replay)

    def repo_inputs(self) -> dict[str, bytes]:
        return {str(p.relative_to(self.pdir)): p.read_bytes()
                for p in sorted(self.pdir.rglob("*")) if p.is_file()}


@pytest.fixture
def fx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Fixture:
    # Keep the user's own git config out of the tests; one test sets a hostile one.
    empty = tmp_path / "gitconfig"
    empty.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    return Fixture(tmp_path)


def test_init_replays_one_commit_per_series_entry(fx: Fixture) -> None:
    fx.init()

    assert fx.subjects() == ["base", "overlay", *PATCHES]
    assert git(fx.replay, "config", "--get", "tc-replay.ref") == REF
    assert (fx.replay / "lib/a.c").read_bytes().startswith(b"\ta1;\n#include \"tc_ours.c\"\n")
    assert (fx.replay / "lib/latin.c").read_bytes() == b"one\n\xe9t\xe9\nthree\n"
    tracked = git(fx.replay, "ls-files").splitlines()
    # The patch-created file is committed although .gitignore matches it.
    assert "out/gen.o" in tracked
    # Overlay dotfiles are skipped, as patch_copy_overlay skips them.
    assert "lib/tc_ours.c" in tracked and ".DS_Store" not in tracked
    assert git(fx.replay, "status", "--porcelain") == ""


def test_init_refuses_an_existing_directory(fx: Fixture, capsys) -> None:
    fx.replay.mkdir()

    assert fx.run("init", str(fx.replay)) == 1
    assert "already exists" in capsys.readouterr().err


def test_init_refuses_an_overlay_file_that_samba_has_and_removes_the_replay(fx: Fixture, capsys) -> None:
    (fx.pdir / "overlay/lib/a.c").write_bytes(b"shadow\n")

    assert fx.run("init", str(fx.replay)) == 1
    assert "lib/a.c already exists in Samba" in capsys.readouterr().err
    assert not fx.replay.exists()


def test_init_reports_a_missing_samba_ref(fx: Fixture, capsys) -> None:
    fx.set_ref("samba-missing")

    assert fx.run("init", str(fx.replay)) == 1
    assert "cannot clone Samba samba-missing" in capsys.readouterr().err
    assert not fx.replay.exists()


def test_export_after_init_gives_back_the_same_files(fx: Fixture) -> None:
    before = fx.repo_inputs()
    fx.init()

    assert fx.run("export", str(fx.replay)) == 0
    assert fx.repo_inputs() == before


def test_verify_matches_and_catches_a_repo_that_differs(fx: Fixture, capsys) -> None:
    fx.init()
    assert fx.run("verify", str(fx.replay)) == 0
    assert "MATCH" in capsys.readouterr().out

    (fx.pdir / "overlay/lib/tc_ours.c").write_bytes(b"int changed;\n")

    assert fx.run("verify", str(fx.replay)) == 1
    err = capsys.readouterr().err
    assert "MISMATCH" in err and "lib/tc_ours.c" in err


def test_verify_reports_a_series_that_does_not_apply(fx: Fixture, capsys) -> None:
    fx.init()
    (fx.pdir / "0002-tail.patch").write_bytes(PATCH_2.replace(b"-\ta7;", b"-\ta9;"))

    assert fx.run("verify", str(fx.replay)) == 1
    assert "does not apply" in capsys.readouterr().err


@pytest.mark.parametrize("command", ["export", "verify"])
def test_a_replay_of_another_samba_ref_is_refused(fx: Fixture, capsys, command: str) -> None:
    fx.init()
    before = fx.repo_inputs()
    fx.set_ref("samba-next")

    assert fx.run(command, str(fx.replay)) == 1
    assert "made from Samba samba-test" in capsys.readouterr().err
    assert fx.repo_inputs() == before


def test_amend_replays_later_patches_and_exports_their_new_offsets(fx: Fixture) -> None:
    fx.init()
    script = (
        'FILES = ["lib/a.c"]\n'
        "def edit(path, text):\n"
        '    return text.replace("#include \\"tc_ours.c\\"\\n",'
        ' "#include \\"tc_ours.c\\"\\n#include \\"tc_more.c\\"\\n")\n')

    assert fx.amend("0001-hook.patch", script) == 0
    assert fx.subjects() == ["base", "overlay", *PATCHES]
    assert fx.run("export", str(fx.replay)) == 0
    assert (fx.pdir / "0001-hook.patch").read_bytes() == PATCH_1.replace(
        b"@@ -1,4 +1,5 @@", b"@@ -1,4 +1,6 @@").replace(
        b"+#include \"tc_ours.c\"\n", b"+#include \"tc_ours.c\"\n+#include \"tc_more.c\"\n")
    assert (fx.pdir / "0002-tail.patch").read_bytes() == PATCH_2.replace(
        b"@@ -5,5 +5,5 @@", b"@@ -6,5 +6,5 @@")
    assert (fx.pdir / "0003-latin.patch").read_bytes() == PATCH_3
    assert fx.run("verify", str(fx.replay)) == 0


def test_amend_overlay_exports_added_changed_and_removed_files(fx: Fixture) -> None:
    fx.init()
    script = (
        'FILES = ["lib/tc_ours.c", "lib/tc_new.h", "lib/tc_old.h"]\n'
        "def edit(path, text):\n"
        '    if path == "lib/tc_ours.c":\n'
        '        return "int ours = 1;\\n"\n'
        '    if path == "lib/tc_new.h":\n'
        '        assert text == ""\n'
        '        return "#define NEW 1\\n"\n'
        "    return None\n")

    assert fx.amend("overlay", script) == 0
    assert fx.run("export", str(fx.replay)) == 0
    overlay = fx.pdir / "overlay"
    assert (overlay / "lib/tc_ours.c").read_bytes() == b"int ours = 1;\n"
    assert (overlay / "lib/tc_new.h").read_bytes() == b"#define NEW 1\n"
    assert not (overlay / "lib/tc_old.h").exists()
    # A dotfile the build never copies is not the tool's to remove.
    assert (overlay / ".DS_Store").exists()
    assert {p.name for p in fx.pdir.glob("*.patch")} == set(PATCHES)
    assert fx.run("verify", str(fx.replay)) == 0


def test_amend_keeps_crlf_and_bytes_that_are_not_utf8(fx: Fixture) -> None:
    fx.init()
    script = (
        'FILES = ["lib/latin.c", "win/crlf.txt"]\n'
        "def edit(path, text):\n"
        '    return text.replace("three", "four").replace("l2", "L2")\n')

    assert fx.amend("0003-latin.patch", script) == 0
    assert (fx.replay / "lib/latin.c").read_bytes() == b"one\n\xe9t\xe9\nfour\n"
    assert (fx.replay / "win/crlf.txt").read_bytes() == b"l1\r\nL2\r\nl3\r\n"
    assert fx.run("export", str(fx.replay)) == 0
    patch = (fx.pdir / "0003-latin.patch").read_bytes()
    assert b" \xe9t\xe9\n-two\n+four\n" in patch and b"-l2\r\n+L2\r\n" in patch
    assert fx.run("verify", str(fx.replay)) == 0


def test_amend_saves_the_previous_history_each_time(fx: Fixture) -> None:
    fx.init()
    first = fx.head()
    assert fx.amend("0003-latin.patch", 'FILES = ["lib/latin.c"]\n'
                    'def edit(path, text):\n    return text + "x\\n"\n') == 0
    second = fx.head()
    assert fx.amend("0003-latin.patch", 'FILES = ["lib/latin.c"]\n'
                    'def edit(path, text):\n    return text + "y\\n"\n') == 0

    assert git(fx.replay, "rev-parse", "refs/replay-backups/1") == first
    assert git(fx.replay, "rev-parse", "refs/replay-backups/2") == second
    assert fx.head() not in (first, second)


def test_amend_puts_the_history_back_when_the_edit_fails(fx: Fixture) -> None:
    fx.init()
    head = fx.head()

    with pytest.raises(RuntimeError):
        fx.amend("0001-hook.patch", 'FILES = ["lib/a.c"]\n'
                 'def edit(path, text):\n    raise RuntimeError("boom")\n')
    assert fx.head() == head and not fx.rebasing()
    assert git(fx.replay, "status", "--porcelain") == ""


def test_amend_that_changes_nothing_is_refused(fx: Fixture, capsys) -> None:
    fx.init()
    head = fx.head()

    assert fx.amend("0001-hook.patch", 'FILES = ["lib/a.c"]\n'
                    "def edit(path, text):\n    return text\n") == 1
    assert "changed nothing" in capsys.readouterr().err
    assert fx.head() == head and not fx.rebasing()


def test_amend_stops_at_a_conflicting_later_patch(fx: Fixture, capsys) -> None:
    fx.init()
    head = fx.head()

    assert fx.amend("0001-hook.patch", 'FILES = ["lib/a.c"]\n'
                    'def edit(path, text):\n    return text.replace("\\ta7;", "\\ta7 = 8;")\n') == 1
    err = capsys.readouterr().err
    assert "stopped the replay" in err and "refs/replay-backups/1" in err
    assert fx.rebasing()
    git(fx.replay, "rebase", "--abort")
    assert fx.head() == head


@pytest.mark.parametrize(("subject", "message"), [
    ("0009-none.patch", "0 commits have subject"),
    ("base", "cannot be amended"),
])
def test_amend_refuses_a_subject_it_cannot_edit(fx: Fixture, capsys, subject: str, message: str) -> None:
    fx.init()

    assert fx.amend(subject, 'FILES = ["lib/a.c"]\ndef edit(path, text):\n    return text + "x"\n') == 1
    assert message in capsys.readouterr().err


def test_amend_refuses_a_dirty_replay_or_one_mid_rebase(fx: Fixture, capsys) -> None:
    fx.init()
    script = 'FILES = ["lib/a.c"]\ndef edit(path, text):\n    return text + "x"\n'
    (fx.replay / "lib/a.c").write_bytes(b"dirty\n")
    assert fx.amend("0001-hook.patch", script) == 1
    assert "uncommitted changes" in capsys.readouterr().err

    git(fx.replay, "checkout", "--", "lib/a.c")
    (replay_tool.git_dir(fx.replay) / "rebase-merge").mkdir()
    assert fx.amend("0001-hook.patch", script) == 1
    assert "already in progress" in capsys.readouterr().err


def test_amend_requires_files_and_an_edit_function(fx: Fixture, capsys) -> None:
    fx.init()

    assert fx.amend("0001-hook.patch", "FILES = []\n") == 1
    assert "must define" in capsys.readouterr().err


def test_export_refuses_when_the_repo_changed_since_the_replay_read_it(fx: Fixture, capsys) -> None:
    fx.init()
    assert fx.amend("0003-latin.patch", 'FILES = ["lib/latin.c"]\n'
                    'def edit(path, text):\n    return text + "x\\n"\n') == 0
    # Another session edits a patch after this replay was made.
    (fx.pdir / "0001-hook.patch").write_bytes(PATCH_1 + b" \ta5;\n")
    before = fx.repo_inputs()

    assert fx.run("export", str(fx.replay)) == 1
    assert "0001-hook.patch" in capsys.readouterr().err
    assert fx.repo_inputs() == before


def test_export_can_run_again_after_its_own_export(fx: Fixture) -> None:
    fx.init()
    for extra in ("x", "y"):
        assert fx.amend("0003-latin.patch", 'FILES = ["lib/latin.c"]\n'
                        f'def edit(path, text):\n    return text + "{extra}\\n"\n') == 0
        assert fx.run("export", str(fx.replay)) == 0
    assert (fx.pdir / "0003-latin.patch").read_bytes().endswith(b"+three\n+x\n+y\n")


def test_export_refuses_a_series_that_differs_from_the_replay(fx: Fixture, capsys) -> None:
    fx.init()
    fx.write_series(["0002-tail.patch", "0001-hook.patch", "0003-latin.patch"])
    before = fx.repo_inputs()

    assert fx.run("export", str(fx.replay)) == 1
    assert "differ from series" in capsys.readouterr().err
    assert fx.repo_inputs() == before


def test_export_removes_a_patch_dropped_from_replay_and_series(fx: Fixture) -> None:
    fx.init()
    # Drop the last patch commit by hand, as a maintainer would.
    git(fx.replay, "reset", "-q", "--hard", "HEAD^")
    fx.write_series(["0001-hook.patch", "0002-tail.patch"])

    assert fx.run("export", str(fx.replay)) == 0
    assert not (fx.pdir / "0003-latin.patch").exists()
    assert fx.run("verify", str(fx.replay)) == 0


def add_commit(fx: Fixture, subject: str, files: dict[str, bytes]) -> None:
    for name, data in files.items():
        (fx.replay / name).write_bytes(data)
    git(fx.replay, "add", "-f", "--", *files)
    git(fx.replay, "commit", "-qm", subject)
    fx.write_series([*PATCHES, subject])


@pytest.mark.parametrize(("subject", "files", "message"), [
    ("0004-bad.patch", {"lib/tc_ours.c": b"int edited;\n"}, "edits overlay files"),
    ("0004-bin.patch", {"lib/blob.bin": b"\x00\x01\x02"}, "binary file"),
    ("notes.txt", {"lib/notes.txt": b"n\n"}, "not a patch file name"),
])
def test_export_refuses_what_the_series_cannot_carry(fx: Fixture, capsys, subject, files, message) -> None:
    fx.init()
    add_commit(fx, subject, files)
    before = fx.repo_inputs()

    assert fx.run("export", str(fx.replay)) == 1
    assert message in capsys.readouterr().err
    assert fx.repo_inputs() == before


def test_a_hostile_global_git_config_changes_nothing(fx: Fixture, tmp_path: Path, monkeypatch) -> None:
    hostile = tmp_path / "hostile-gitconfig"
    hostile.write_text(
        "[diff]\n\tnoprefix = true\n\tmnemonicPrefix = true\n\talgorithm = histogram\n"
        "[color]\n\tui = always\n[core]\n\tautocrlf = true\n"
        "[commit]\n\tgpgsign = true\n[gpg]\n\tprogram = false\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(hostile))
    before = fx.repo_inputs()

    fx.init()
    assert (fx.replay / "win/crlf.txt").read_bytes() == CRLF
    assert fx.run("export", str(fx.replay)) == 0
    assert fx.repo_inputs() == before
    assert fx.amend("0003-latin.patch", 'FILES = ["lib/latin.c"]\n'
                    'def edit(path, text):\n    return text + "x\\n"\n') == 0
    assert fx.run("export", str(fx.replay)) == 0
    assert (fx.pdir / "0003-latin.patch").read_bytes() == PATCH_3.replace(
        b"@@ -1,3 +1,3 @@", b"@@ -1,3 +1,4 @@") + b"+x\n"
    assert fx.run("verify", str(fx.replay)) == 0


def test_the_real_series_uses_patch_file_names_the_tool_accepts() -> None:
    names = replay_tool.series_names(ROOT)

    assert names and all(replay_tool.PATCH_NAME.match(n) for n in names)
    assert all((ROOT / replay_tool.PATCH_DIR / n).is_file() for n in names)


def test_amend_of_a_file_gitignore_matches_is_exported(fx: Fixture) -> None:
    fx.init()

    assert fx.amend("0002-tail.patch", 'FILES = ["out/gen.o", "out/new.o"]\n'
                    'def edit(path, text):\n    return "regenerated\\n"\n') == 0
    assert fx.run("export", str(fx.replay)) == 0
    patch = (fx.pdir / "0002-tail.patch").read_bytes()
    assert b"+++ b/out/gen.o\n@@ -0,0 +1 @@\n+regenerated\n" in patch
    assert patch.endswith(b"+++ b/out/new.o\n@@ -0,0 +1 @@\n+regenerated\n")
    assert fx.run("verify", str(fx.replay)) == 0


def test_amend_stops_where_a_later_patch_becomes_empty(fx: Fixture, capsys) -> None:
    fx.init()

    # 0002 now makes 0003's change too, so 0003 would carry nothing.
    assert fx.amend("0002-tail.patch", 'FILES = ["lib/latin.c"]\n'
                    'def edit(path, text):\n    return text.replace("two", "three")\n') == 1
    assert "stopped the replay" in capsys.readouterr().err
    assert fx.rebasing()
