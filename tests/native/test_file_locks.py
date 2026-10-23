"""`service --print-file-locks` names the processes holding fcntl locks on
Samba's TDBs, the locks an smbd asleep in "lockf" waits for."""
import fcntl
import os
import subprocess

import pytest

from tests.native.build import compile_service


@pytest.fixture(scope="module")
def service(tmp_path_factory):
    return compile_service(tmp_path_factory.mktemp("locks") / "service", flags=[
        "-UTC_NATIVE_TEST",
        '-DTC_ACP_PATH="/nonexistent/tc-test-acp"',
    ])


def run(binary, *files):
    return subprocess.run([str(binary), "--print-file-locks", *map(str, files)], capture_output=True, text=True,
                          timeout=10)


def test_lists_each_lock_another_process_holds_in_order(service, tmp_path):
    tdb = tmp_path / "locking.tdb"
    tdb.write_bytes(b"\0" * 4096)
    fd = os.open(tdb, os.O_RDWR)
    try:
        # A record chain lock, a reader (the TDB "active" slot) and a lock
        # to the end of the file (an all-record lock).
        fcntl.lockf(fd, fcntl.LOCK_EX, 1, 168)
        fcntl.lockf(fd, fcntl.LOCK_SH, 1, 4)
        fcntl.lockf(fd, fcntl.LOCK_EX, 0, 1000)
        result = run(service, tdb)
    finally:
        os.close(fd)
    pid = os.getpid()
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        f"{tdb} pid {pid} read 4+1 (TDB open marker)",
        f"{tdb} pid {pid} write 168+1",
        f"{tdb} pid {pid} write 1000+0",
    ]


def test_marks_only_a_tdbs_read_lock_on_byte_four_as_its_open_marker(service, tmp_path):
    # TDB's open marker is a read lock on exactly byte 4. A writer there (a
    # clearing open) or a reader of a wider range is a lock someone may wait
    # for, and byte 4 of a file that is not a TDB means nothing.
    tdb = tmp_path / "brlock.tdb"
    other = tmp_path / "brlock.tdb.bak"
    for path in (tdb, other):
        path.write_bytes(b"\0" * 64)
    tdb_fd = os.open(tdb, os.O_RDWR)
    other_fd = os.open(other, os.O_RDWR)
    try:
        fcntl.lockf(other_fd, fcntl.LOCK_SH, 1, 4)
        result = run(service, tdb, other)
        assert result.stdout.splitlines() == [f"{other} pid {os.getpid()} read 4+1"]
        fcntl.lockf(tdb_fd, fcntl.LOCK_EX, 1, 4)
        assert run(service, tdb).stdout == f"{tdb} pid {os.getpid()} write 4+1\n"
        fcntl.lockf(tdb_fd, fcntl.LOCK_SH, 2, 4)
        assert run(service, tdb).stdout == f"{tdb} pid {os.getpid()} read 4+2\n"
    finally:
        os.close(tdb_fd)
        os.close(other_fd)


def test_an_unlocked_file_lists_nothing_and_the_scan_releases_nothing(service, tmp_path):
    tdb = tmp_path / "brlock.tdb"
    tdb.write_bytes(b"\0" * 64)
    assert run(service, tdb).stdout == ""
    fd = os.open(tdb, os.O_RDWR)
    try:
        fcntl.lockf(fd, fcntl.LOCK_EX, 8, 0)
        assert run(service, tdb).stdout == f"{tdb} pid {os.getpid()} write 0+8\n"
        # The helper's open and close of the file left the holder's lock alone.
        child = subprocess.run(
            ["python3", "-c", "import fcntl, os, sys\n"
             "fd = os.open(sys.argv[1], os.O_RDWR)\n"
             "try:\n    fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB, 8, 0)\nexcept OSError:\n    sys.exit(7)\n",
             str(tdb)], timeout=10)
        assert child.returncode == 7
    finally:
        os.close(fd)


def test_an_unreadable_file_fails_but_the_others_are_still_listed(service, tmp_path):
    tdb = tmp_path / "g_lock.tdb"
    tdb.write_bytes(b"\0" * 64)
    fd = os.open(tdb, os.O_RDWR)
    try:
        fcntl.lockf(fd, fcntl.LOCK_EX, 1, 48)
        result = run(service, tmp_path / "missing.tdb", tdb)
    finally:
        os.close(fd)
    assert result.returncode == 1
    assert result.stdout.splitlines() == [
        f"{tmp_path / 'missing.tdb'}: No such file or directory",
        f"{tdb} pid {os.getpid()} write 48+1",
    ]

