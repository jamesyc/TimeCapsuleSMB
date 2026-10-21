"""Stage and publish real RAM files, retaining applied config on preparation failures."""
import errno
import os
import plistlib
import shutil
import subprocess
import zlib

import pytest
from tests.native.build import ROOT, compile_modules, header_define
from timecapsulesmb.services.deploy import render_payload_checksums


# The read budget (worker.h) and a simulated read that takes longer than it:
# the first read goes through, the next is past the budget.
BUDGET_MS = header_define('common/worker.h', 'TC_PAYLOAD_READ_BUDGET_MS')
STALL_MS = BUDGET_MS + 1
# The host libc's words for a stall: macOS and Linux differ.
TIMED_OUT = os.strerror(errno.ETIMEDOUT)


@pytest.fixture(scope="module")
def stage_tools(tmp_path_factory):
    root = tmp_path_factory.mktemp("stage-native")
    binary = root / "stage"
    modules = ["native/samba/staging.c", "native/samba/config.c", "native/storage/runtime.c",
               "native/storage/mast.c", "native/storage/shares.c", "native/common/config.c",
               "native/common/worker.c", "native/common/process.c", "native/common/parent.c",
               "native/common/acp.c"]
    compile_modules(binary, modules,
                    flags=(f'-DTC_FLASH_CONFIG_PATH="{root}/runtime.conf"',
                           f'-DTC_RAM_ROOT="{root}/ram"', f'-DTC_LOCKS_ROOT="{root}/locks"',
                           f'-DTC_VOLUMES_ROOT="{root}"', "-I", str(ROOT / "build/native")),
                    extra_sources=(ROOT / "tests/native/unit/test_staging.c",))
    return root, binary


@pytest.fixture
def stage(stage_tools):
    root, binary = stage_tools
    for name in ("dk2", "ram", "locks"):
        shutil.rmtree(root / name, ignore_errors=True)
    payload = root / "dk2/.samba4"
    payload.mkdir(parents=True)
    (payload / "smbd").write_bytes(b"samba-image")
    (payload / "rsync").write_bytes(b"rsync-image")
    (payload / "rsyncd.conf").write_text("[Data]\npath = /Volumes/dk9/ShareRoot\nread only = no\n")
    (root / "runtime.conf").write_text("RSYNC_ENABLED=1\n")
    mounts = root / "mounts"
    mounts.write_text(f"{root}/dk2 dk2 1\n")
    disk = [dict(deviceName="sd0", builtin=True, partitions=[dict(deviceName="dk2", name="Data", format="hfs",
            uuid="11111111-1111-1111-1111-111111111111")])]

    def run(command="copy", **env):
        return subprocess.run([str(binary), command], input=plistlib.dumps(disk), capture_output=True,
                              timeout=10, env={**os.environ, "TC_TEST_MOUNTS": str(mounts), **env})
    return root, payload, run


def test_stage_copies_then_publishes_config_and_private_auth(stage):
    root, payload, run = stage
    result = run()
    assert result.returncode == 0, result.stderr
    assert (root / "ram/sbin/smbd").read_bytes() == b"samba-image"
    # Samba's run never touches rsync's files: rsync stages on its own.
    assert not (root / "ram/sbin/rsync").exists() and not (root / "ram/etc/rsyncd.conf").exists()
    result = run("rsync-copy")
    assert result.returncode == 0, result.stderr
    assert (root / "ram/sbin/rsync").read_bytes() == b"rsync-image"
    assert f"path = {root}/dk2/ShareRoot" in (root / "ram/etc/rsyncd.conf").read_text()
    password = root / "ram/private/smbpasswd"
    assert password.stat().st_mode & 0o777 == 0o600
    assert ":0123456789ABCDEF0123456789ABCDEF:" in password.read_text()
    assert (root / "ram/private/username.map").read_text() == "!root = root\nroot = *\n"
    assert (payload / "logs/cores/smbd").is_dir()
    assert not list((root / "ram").rglob("*.next"))


def test_reload_does_not_replace_running_images_and_discard_keeps_applied(stage):
    root, payload, run = stage
    assert run().returncode == 0
    image = root / "ram/sbin/smbd"
    before = image.stat()
    applied = (root / "ram/etc/smb.conf").read_bytes()
    (payload / "smbd").write_bytes(b"new-generation")
    (root / "runtime.conf").write_text("SMBD_DEBUG_LOGGING=1\n")
    assert run("discard").returncode == 0
    assert (root / "ram/etc/smb.conf").read_bytes() == applied
    assert run("reload").returncode == 0
    assert image.stat().st_ino == before.st_ino and image.read_bytes() == b"samba-image"
    assert "log level = 10" in (root / "ram/etc/smb.conf").read_text()


@pytest.mark.parametrize("fault", ["unmounted", "readonly", "no_binary", "bad_auth", "symlink"])
def test_failed_stage_keeps_applied_configuration_and_retries(stage, fault):
    root, payload, run = stage
    assert run().returncode == 0
    applied = (root / "ram/etc/smb.conf").read_bytes()
    env = {}
    command = "reload"
    if fault == "unmounted": (root / "mounts").write_text("")
    elif fault == "readonly": (root / "mounts").write_text(f"{root}/dk2 dk2 0\n")
    elif fault == "no_binary":
        (payload / "smbd").unlink()
        command = "copy"
    elif fault == "bad_auth": env["FAIL_HASH"] = "1"
    else:
        shutil.rmtree(payload / "logs/cores/smbd")
        (payload / "logs/cores/smbd").symlink_to(root)
    assert run(command, **env).returncode != 0
    assert (root / "ram/etc/smb.conf").read_bytes() == applied
    assert not list((root / "ram").rglob("*.next"))
    (root / "mounts").write_text(f"{root}/dk2 dk2 1\n")
    (payload / "smbd").write_bytes(b"retry-image")
    (payload / "rsyncd.conf").write_text("[Data]\npath = /old\n")
    if fault == "symlink": (payload / "logs/cores/smbd").unlink()
    assert run().returncode == 0
    assert (root / "ram/sbin/smbd").read_bytes() == b"retry-image"


@pytest.mark.parametrize("fault", ["bad_config", "no_binary"])
def test_a_failed_rsync_run_touches_nothing_of_samba_and_removes_its_own_files(stage, fault):
    # rsync is optional and stages on its own: its failure leaves Samba's
    # published files alone and no copy or prepared file of its own behind.
    root, payload, run = stage
    assert run().returncode == 0 and run("rsync-copy").returncode == 0
    applied = {name: (root / name).read_bytes() for name in ("ram/etc/smb.conf", "ram/etc/rsyncd.conf")}
    (root / "ram/sbin/rsync").unlink()
    if fault == "bad_config": (payload / "rsyncd.conf").write_text("[Data]\n")
    else: (payload / "rsync").unlink()
    result = run("rsync-copy")
    assert result.returncode == 3
    # A bad configuration costs no copy; a missing binary is the payload's
    # verdict, logged unless the last run found it missing too.
    if fault == "bad_config":
        assert result.stdout.decode().strip() == "copy=-1 crc=-1 read=-1 missing=0"
        assert f"stage: {payload}/rsyncd.conf has no path line".encode() in result.stderr
    else:
        assert result.stdout.decode().strip() == "copy=-1 crc=-1 read=-1 missing=2"
        assert f"stage: {payload}/rsync is missing".encode() in result.stderr
        assert b"is missing" not in run("rsync-copy", MISSING="2").stderr
    assert {name: (root / name).read_bytes() for name in applied} == applied
    assert not (root / "ram/sbin/rsync").exists()
    assert not list((root / "ram").rglob("*.next"))


def test_each_missing_file_is_logged_once(stage):
    # Each file rsync needs has its own place in the set: one the last run
    # found missing is not logged again, one newly missing is.
    root, payload, run = stage
    (payload / "rsync").unlink()
    (payload / "rsyncd.conf").unlink()
    result = run("rsync-copy")
    assert result.stdout.decode().strip() == "copy=-1 crc=-1 read=-1 missing=3"
    for name in ("rsyncd.conf", "rsync"):
        assert f"stage: {payload}/{name} is missing".encode() in result.stderr
    result = run("rsync-copy", MISSING="1")
    assert f"stage: {payload}/rsync is missing".encode() in result.stderr
    assert b"rsyncd.conf is missing" not in result.stderr
    # A run that does not copy the binary needs only the configuration.
    assert run("rsync", MISSING="3").stdout.decode().strip() == "copy=-1 crc=-1 read=-1 missing=1"


@pytest.mark.parametrize("finding", ["missing", "damaged", "stalled"])
def test_a_disk_that_goes_away_during_a_run_reports_nothing_about_the_payload(stage, finding):
    # A disk being detached reads as files missing, as other bytes, or as a
    # read that never finishes: only one that stayed the payload's disk until
    # the run ended says any of them.
    root, payload, run = stage
    checksums(root, payload)
    fault = {}
    if finding == "missing":
        (payload / "rsync").unlink()
    elif finding == "damaged":
        (payload / "rsync").write_bytes(b"rsync-imagf")
    else:
        # Bytes left after the first read, which the budget then refuses.
        (payload / "rsync").write_bytes(b"rsync-image" * 10000)
        checksums(root, payload)
        fault = {"TC_TEST_SLOW_READ": f"{STALL_MS}:{payload / 'rsync'}"}
    expected = {"missing": "missing=2", "damaged": "copy=-2 ", "stalled": "read=-4 "}[finding]
    assert expected in run("rsync-copy", **fault).stdout.decode()
    result = run("rsync-copy", TC_TEST_UNMOUNT_AT=str(payload / "rsync"), **fault)
    assert result.returncode == 3
    copy, crc, read, missing = result.stdout.decode().split()
    assert (copy, read, missing) == ("copy=-1", "read=-1", "missing=-1")


def test_lock_cleanup_preserves_mount_root_and_never_follows_symlinks(stage):
    root, _, run = stage
    locks = root / 'locks'
    (locks / 'nested').mkdir(parents=True)
    (locks / 'nested/locking.tdb').write_bytes(b'old')
    protected = root / 'protected'
    protected.mkdir(exist_ok=True)
    (protected / 'preserve').write_text('user data')
    (locks / 'outside').symlink_to(protected)
    inode = locks.stat().st_ino
    assert run('clear-locks').returncode == 0
    assert locks.stat().st_ino == inode and list(locks.iterdir()) == []
    assert (protected / 'preserve').read_text() == 'user data'


def checksums(root, payload):
    """The checksums file deploy writes into the payload with its binaries."""
    (payload / "checksums").write_text(render_payload_checksums(smbd_path=payload / "smbd", rsync_path=payload / "rsync"))
    return {name: f"{zlib.crc32((payload / name).read_bytes()):08x}" for name in ("smbd", "rsync")}


@pytest.mark.parametrize("image", ["smbd", "rsync"])
def test_stage_refuses_a_copy_that_differs_from_its_payloads_checksum(stage, image):
    root, payload, run = stage
    command = {"smbd": "copy", "rsync": "rsync-copy"}[image]
    crc = checksums(root, payload)
    assert run().returncode == 0 and run("rsync-copy").returncode == 0
    applied = (root / "ram/etc/smb.conf").read_bytes()
    good = (payload / image).read_bytes()
    # Same size, other bytes: what a disk returning stale blocks would give.
    (payload / image).write_bytes(good[:-1] + b"!")
    result = run(command)
    assert result.returncode == 3
    read = zlib.crc32(good[:-1] + b"!")
    assert result.stdout.decode().strip() == f"copy=-2 crc={int(crc[image], 16)} read={read} missing=0"
    assert f"copy verify failed: {payload}/{image} -> {root}/ram/sbin/{image}: ".encode() in result.stderr
    assert f"CRC-32 read {read:08x}, deployed {crc[image]} (copied {len(good)} of {len(good)} bytes".encode() in result.stderr
    assert not (root / f"ram/sbin/{image}").exists()
    assert (root / "ram/etc/smb.conf").read_bytes() == applied
    assert not list((root / "ram").rglob("*.next"))
    (payload / image).write_bytes(good)
    result = run(command)
    assert result.returncode == 0 and result.stdout.decode().strip() == f"copy=0 crc={int(crc[image], 16)} read=-1 missing=0"
    assert (root / f"ram/sbin/{image}").read_bytes() == good


def test_a_damaged_image_is_checked_in_place_and_logged_once_per_reading(stage):
    # After damage the manager asks for a probe: a disk copy that still
    # fails is neither copied into RAM nor logged again for the same reading.
    root, payload, run = stage
    crc = checksums(root, payload)
    good = (payload / "smbd").read_bytes()
    (payload / "smbd").write_bytes(good[:-1] + b"!")
    read = zlib.crc32(good[:-1] + b"!")
    result = run("copy", PROBE="1")
    assert result.returncode == 3 and result.stdout.decode().strip() == f"copy=-2 crc={int(crc['smbd'], 16)} read={read} missing=0"
    assert f"copy verify failed: {payload}/smbd: CRC-32 read {read:08x}, deployed {crc['smbd']}".encode() in result.stderr
    assert not (root / "ram/sbin/smbd").exists()
    result = run("copy", PROBE="1", SEEN=str(read))
    assert result.returncode == 3 and b"copy verify failed" not in result.stderr
    # A disk that cannot return the bytes is damage the same way.
    result = run("copy", PROBE="1", TC_TEST_EIO=str(payload / "smbd"))
    assert result.returncode == 3 and result.stdout.decode().strip() == f"copy=-2 crc={int(crc['smbd'], 16)} read=-2 missing=0"
    result = run("copy", PROBE="1", SEEN="-2", TC_TEST_EIO=str(payload / "smbd"))
    assert result.returncode == 3 and b"copy verify failed" not in result.stderr
    (payload / "smbd").write_bytes(good)
    result = run("copy", PROBE="1", SEEN=str(read))
    assert result.returncode == 0 and (root / "ram/sbin/smbd").read_bytes() == good


@pytest.mark.parametrize("image", ["smbd", "rsync"])
@pytest.mark.parametrize("fault, read, logged", [("TC_TEST_EIO", -2, os.strerror(errno.EIO)),
                                                 ("TC_TEST_SLOW_READ", -4, TIMED_OUT)])
def test_a_disk_that_cannot_return_the_bytes_reports_what_it_read(stage, image, fault, read, logged):
    # EIO, or a disk so slow that the read budget ran out: damage the disk
    # did, which the manager tells apart from other bytes (read=) and which
    # leaves nothing in RAM.
    root, payload, run = stage
    command = {"smbd": "copy", "rsync": "rsync-copy"}[image]
    # Big enough that the budget runs out with bytes left to read.
    for name in ("smbd", "rsync"):
        (payload / name).write_bytes(name.encode() * 40000)
    crc = checksums(root, payload)
    setting = str(payload / image) if fault == "TC_TEST_EIO" else f"{STALL_MS}:{payload / image}"
    result = run(command, **{fault: setting})
    assert result.returncode == 3
    assert result.stdout.decode().strip() == f"copy=-2 crc={int(crc[image], 16)} read={read} missing=0"
    assert f"copy read failed: {payload}/{image} -> {root}/ram/sbin/{image}: {logged}".encode() in result.stderr
    assert not (root / f"ram/sbin/{image}").exists()
    # The probe after it reads the disk copy in place: the same reading is
    # damage again, not logged a second time.
    result = run(command, PROBE="1", **{fault: setting})
    assert result.stdout.decode().strip() == f"copy=-2 crc={int(crc[image], 16)} read={read} missing=0"
    assert f"copy verify failed: {payload}/{image}: {logged}".encode() in result.stderr
    result = run(command, PROBE="1", SEEN=str(read), **{fault: setting})
    assert result.returncode == 3 and b"copy verify failed" not in result.stderr
    # Once the disk returns the bytes, the copy goes through.
    result = run(command, PROBE="1", SEEN=str(read))
    assert result.returncode == 0 and result.stdout.decode().strip() == f"copy=0 crc={int(crc[image], 16)} read=-1 missing=0"


def test_each_copy_logs_how_long_its_reads_took(stage):
    # How far healthy disks are from the read budget, in the field. On the
    # simulated clock each read takes 1000 ms: the bytes, then the end.
    root, payload, run = stage
    checksums(root, payload)
    result = run(TC_TEST_SLOW_READ=f"1000:{payload / 'smbd'}")
    assert result.returncode == 0
    assert b"stage: copied smbd in 2000 ms" in result.stderr


def test_a_stall_on_another_file_leaves_this_copy_alone(stage):
    # The budget is per file: rsync's stall says nothing about smbd.
    root, payload, run = stage
    crc = checksums(root, payload)
    result = run(TC_TEST_SLOW_READ=f"{STALL_MS}:{payload / 'rsync'}")
    assert result.returncode == 0 and result.stdout.decode().strip() == f"copy=0 crc={int(crc['smbd'], 16)} read=-1 missing=0"


@pytest.mark.parametrize("read_ms, stalled", [(BUDGET_MS // 3 - 1, False), (BUDGET_MS // 2, True)])
def test_the_read_budget_counts_only_while_bytes_remain(stage, read_ms, stalled):
    # On the simulated clock each read takes read_ms. 100000 bytes are four
    # 32 KiB reads and a fifth that finds the end: reads that start before
    # the budget runs out go through, and the one that finds the end is never
    # refused, so a disk that returned everything in time is not a stall.
    root, payload, run = stage
    (payload / "smbd").write_bytes(bytes(range(256)) * 390 + b"x" * 160)
    crc = checksums(root, payload)
    result = run(TC_TEST_SLOW_READ=f"{read_ms}:{payload / 'smbd'}")
    if stalled:
        # Reads start at 0, B/2 and B: the third is refused, two chunks in.
        assert result.returncode == 3 and result.stdout.decode().strip() == f"copy=-2 crc={int(crc['smbd'], 16)} read=-4 missing=0"
        assert f"{TIMED_OUT} (copied 65536 of 100000 bytes".encode() in result.stderr
        assert not (root / "ram/sbin/smbd").exists()
    else:
        assert result.returncode == 0 and result.stdout.decode().strip() == f"copy=0 crc={int(crc['smbd'], 16)} read=-1 missing=0"
        assert (root / "ram/sbin/smbd").read_bytes() == (payload / "smbd").read_bytes()


def test_a_checksums_file_the_disk_cannot_read_is_the_disks_damage(stage):
    # EIO on the checksums file is the disk's damage, as an unreadable binary
    # is, not a payload without the line (read=-3): reinstalling would not
    # help. It has its own reading (read=-5), so it is logged even right
    # after an unreadable binary (read=-2), and not again after itself.
    root, payload, run = stage
    checksums(root, payload)
    result = run(TC_TEST_EIO_OPEN=str(payload / "checksums"))
    assert result.returncode == 3 and result.stdout.decode().strip() == "copy=-2 crc=-1 read=-5 missing=0"
    assert f"stage: {payload}/checksums unreadable: Input/output error".encode() in result.stderr
    result = run(PROBE="1", SEEN="-2", TC_TEST_EIO_OPEN=str(payload / "checksums"))
    assert f"stage: {payload}/checksums unreadable: Input/output error".encode() in result.stderr
    result = run(PROBE="1", SEEN="-5", TC_TEST_EIO_OPEN=str(payload / "checksums"))
    assert result.returncode == 3 and b"checksums unreadable" not in result.stderr


def test_a_fault_named_through_a_file_lasts_while_the_file_does(stage, tmp_path):
    # "@FILE": the fault applies while FILE names the path, so a test can
    # clear it while the manager runs.
    root, payload, run = stage
    crc = checksums(root, payload)
    control = tmp_path / "eio"
    control.write_text(f"{payload / 'smbd'}\n")
    assert run(TC_TEST_EIO=f"@{control}").stdout.decode().strip() == f"copy=-2 crc={int(crc['smbd'], 16)} read=-2 missing=0"
    control.unlink()
    result = run(TC_TEST_EIO=f"@{control}")
    assert result.returncode == 0 and (root / "ram/sbin/smbd").exists()


@pytest.mark.parametrize("content", ["smbd 1234567\nrsync 89abcdef\n", "smbd 1234567G\n", "rsync 89abcdef\n",
                                     "x" * 300])
def test_a_damaged_checksums_file_is_damage_too(stage, content):
    root, payload, run = stage
    (payload / "checksums").write_text(content)
    result = run()
    assert result.returncode == 3 and result.stdout.decode().strip() == "copy=-2 crc=-1 read=-3 missing=0"
    assert f"stage: {payload}/checksums is damaged: no checksum for smbd".encode() in result.stderr
    assert not (root / "ram/sbin/smbd").exists()
    # The manager passes back what the last run read: the same damage is not
    # logged again.
    result = run(PROBE="1", SEEN="-3")
    assert result.returncode == 3 and result.stdout.decode().strip() == "copy=-2 crc=-1 read=-3 missing=0"
    assert b"checksums is damaged" not in result.stderr


def test_a_run_that_fails_at_once_still_reports_its_result(stage):
    root, payload, run = stage
    result = run(BAD_HASH="1")
    assert result.returncode == 3 and result.stdout.decode().strip() == "copy=-1 crc=-1 read=-1 missing=-1"


def test_a_checksums_file_that_cannot_be_read_says_so(stage):
    # Damage all the same, but the log names the read error, not a missing
    # entry: the disk is what to look at, not the deploy.
    root, payload, run = stage
    (payload / "checksums").mkdir()
    result = run()
    assert result.returncode == 3 and result.stdout.decode().strip() == "copy=-2 crc=-1 read=-3 missing=0"
    assert f"stage: {payload}/checksums unreadable: Is a directory".encode() in result.stderr
    assert b"no checksum" not in result.stderr


def test_a_checksums_file_that_cannot_be_opened_is_a_plain_failure(stage):
    # Not the disk's damage (no EIO): the run fails and retries as any other.
    root, payload, run = stage
    checksums(root, payload)
    (payload / "checksums").chmod(0)
    try:
        result = run()
    finally:
        (payload / "checksums").chmod(0o600)
    assert result.returncode != 0 and result.stdout.decode().strip() == "copy=-1 crc=-1 read=-1 missing=0"
    assert f"stage: {payload}/checksums unreadable: Permission denied".encode() in result.stderr
    assert not (root / "ram/sbin/smbd").exists()


def test_a_payload_without_a_checksums_file_is_copied_unchecked(stage):
    # A payload from before checksums, or one another disk kept: nothing to
    # check it against, so it is copied as it was.
    root, payload, run = stage
    result = run()
    assert result.returncode == 0 and result.stdout.decode().strip() == "copy=0 crc=-1 read=-1 missing=0"
