"""tests/executables.py: one checked file per content, safe to share and to prune."""
import os
import stat
import subprocess

import pytest

from tests import executables


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setattr(executables, "CACHE", tmp_path / "cache")
    return tmp_path / "cache"


def test_same_content_is_one_read_only_file_under_every_name(cache, tmp_path):
    first = executables.write_executable(tmp_path / "a/mount", "#!/bin/sh\necho \"$(basename \"$0\")\"\n")
    second = executables.write_executable(tmp_path / "b/uname", "#!/bin/sh\necho \"$(basename \"$0\")\"\n")
    assert first.stat().st_ino == second.stat().st_ino
    assert stat.S_IMODE(first.stat().st_mode) == 0o555
    # Each name still runs as itself.
    assert subprocess.run([str(second)], capture_output=True, text=True).stdout == "uname\n"
    with pytest.raises(PermissionError):
        first.write_text("changed for every other test")


def test_different_content_or_bytes_get_their_own_file(cache, tmp_path):
    script = executables.write_executable(tmp_path / "one", "#!/bin/sh\nexit 0\n")
    other = executables.write_executable(tmp_path / "two", b"#!/bin/sh\nexit 1\n")
    assert script.stat().st_ino != other.stat().st_ino
    assert subprocess.run([str(other)]).returncode == 1


def test_rewriting_a_path_replaces_the_link_not_the_shared_file(cache, tmp_path):
    path = executables.write_executable(tmp_path / "tool", "#!/bin/sh\nexit 0\n")
    keep = executables.write_executable(tmp_path / "keep", "#!/bin/sh\nexit 0\n")
    executables.write_executable(path, "#!/bin/sh\nexit 3\n")
    assert subprocess.run([str(path)]).returncode == 3
    assert subprocess.run([str(keep)]).returncode == 0


def test_prune_removes_unused_copies_but_never_a_linked_file(cache, tmp_path):
    used = executables.write_executable(tmp_path / "used", "#!/bin/sh\nexit 0\n")
    old = executables.write_executable(tmp_path / "old", "#!/bin/sh\nexit 4\n")
    copies = {path.name: path for path in cache.iterdir()}
    stale = next(p for p in copies.values() if p.stat().st_ino == old.stat().st_ino)
    day_ago = stale.stat().st_mtime - executables.UNUSED_SECONDS - 60
    os.utime(stale, (day_ago, day_ago))
    executables.prune()
    assert not stale.exists()
    # The test's own link keeps the file, and the used copy stays.
    assert subprocess.run([str(old)]).returncode == 4
    assert len(list(cache.iterdir())) == 2  # the used copy and the prune marker
    assert used.exists()


def test_prune_runs_at_most_hourly(cache, tmp_path):
    executables.write_executable(tmp_path / "tool", "#!/bin/sh\nexit 0\n")
    copy = next(path for path in cache.iterdir())
    executables.prune()
    day_ago = copy.stat().st_mtime - executables.UNUSED_SECONDS - 60
    os.utime(copy, (day_ago, day_ago))
    executables.prune()
    assert copy.exists()
    executables.prune(now=(cache / ".pruned").stat().st_mtime + 3601)
    assert not copy.exists()


def test_a_use_keeps_a_copy_from_being_pruned(cache, tmp_path):
    executables.write_executable(tmp_path / "tool", "#!/bin/sh\nexit 0\n")
    copy = next(path for path in cache.iterdir())
    day_ago = copy.stat().st_mtime - executables.UNUSED_SECONDS - 60
    os.utime(copy, (day_ago, day_ago))
    executables.write_executable(tmp_path / "again", "#!/bin/sh\nexit 0\n")
    executables.prune()
    assert copy.exists()


def vanishing_copies(monkeypatch, times):
    """Delete the shared copy the first `times` uses return, as another run's
    prune does when it read the copy's time before the use refreshed it."""
    real_shared_copy = executables.shared_copy
    deleted = []

    def shared_copy(data):
        copy = real_shared_copy(data)
        if len(deleted) < times:
            copy.unlink()
            deleted.append(copy)
        return copy

    monkeypatch.setattr(executables, "shared_copy", shared_copy)
    return deleted


def test_a_copy_pruned_before_its_link_is_written_again(cache, tmp_path, monkeypatch):
    deleted = vanishing_copies(monkeypatch, times=1)
    path = executables.write_executable(tmp_path / "tool", "#!/bin/sh\nexit 5\n")
    assert len(deleted) == 1
    assert subprocess.run([str(path)]).returncode == 5
    # The new copy is shared again, not a private file.
    assert path.stat().st_ino == deleted[0].stat().st_ino
    assert executables.write_executable(tmp_path / "again", "#!/bin/sh\nexit 5\n").stat().st_ino == path.stat().st_ino


def test_a_copy_that_keeps_vanishing_fails_instead_of_retrying_forever(cache, tmp_path, monkeypatch):
    deleted = vanishing_copies(monkeypatch, times=3)
    with pytest.raises(FileNotFoundError):
        executables.write_executable(tmp_path / "tool", "#!/bin/sh\nexit 0\n")
    assert len(deleted) == 2
    assert not (tmp_path / "tool").exists()
