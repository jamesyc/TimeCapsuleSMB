"""Write test executables as hard links to content-addressed copies.

macOS checks every newly written executable on its first exec, one file at a
time across the whole host: about 0.15 s for a two-line shell script and 0.45 s
for a linked binary, and seconds once parallel workers queue behind each other.
The check is remembered per file (inode), so a hard link to a file that already
ran starts at once. Tests that write the same fake tool again and again (a fresh
temporary toolchain per build-wrapper run) therefore link one shared copy per
content instead of writing a new file each time.

The shared copies are read-only, so a test or script that tries to rewrite a
linked tool in place fails instead of changing it for every other test. A
chmod cannot be stopped that way and would reach every link: a test that
changes a tool's mode must write its own file.

Builds that embed a run's temporary paths are new every run, so copies unused
for a day are deleted; links made from them keep their files.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import time
from pathlib import Path

CACHE = Path(tempfile.gettempdir()) / f"tc-test-executables-{os.getuid()}"
UNUSED_SECONDS = 86400


def shared_copy(data: bytes) -> Path:
    """The read-only shared file holding data, created on first use."""
    cached = CACHE / hashlib.sha256(data).hexdigest()
    try:
        # A use keeps the copy from being pruned.
        os.utime(cached)
    except FileNotFoundError:
        CACHE.mkdir(parents=True, exist_ok=True)
        # Concurrent writers each publish a complete file; the last rename wins.
        fd, temp = tempfile.mkstemp(dir=CACHE, prefix=".partial-")
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
            os.chmod(temp, 0o555)
            os.replace(temp, cached)
        except BaseException:
            Path(temp).unlink(missing_ok=True)
            raise
    return cached


def link_shared(source: Path, path: Path) -> Path:
    """Put a hard link to source at path, or a copy across file systems."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    try:
        os.link(source, path)
    except OSError:
        shutil.copy2(source, path)
    return path


def write_executable(path: Path, content: str | bytes) -> Path:
    """Make path an executable holding content."""
    data = content.encode() if isinstance(content, str) else content
    try:
        return link_shared(shared_copy(data), path)
    except FileNotFoundError:
        # Another run's prune can read a copy's old time before this use
        # refreshes it and delete it before the link; this writes it again.
        return link_shared(shared_copy(data), path)


def prune(now: float | None = None) -> None:
    """Delete copies unused for UNUSED_SECONDS, checking at most hourly."""
    now = time.time() if now is None else now
    marker = CACHE / ".pruned"
    try:
        if now - marker.stat().st_mtime < 3600:
            return
    except FileNotFoundError:
        pass
    CACHE.mkdir(parents=True, exist_ok=True)
    marker.touch()
    for entry in CACHE.iterdir():
        try:
            if entry != marker and now - entry.stat().st_mtime > UNUSED_SECONDS:
                entry.unlink()
        except OSError:
            pass


prune()
