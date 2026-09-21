"""Stage and publish real RAM files, retaining applied config on preparation failures."""
import os
import plistlib
import shutil
import subprocess

import pytest
from tests.native.build import ROOT, compile_modules


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


@pytest.mark.parametrize("fault", ["unmounted", "readonly", "no_binary", "bad_rsync", "bad_auth", "symlink"])
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
    elif fault == "bad_rsync": (payload / "rsyncd.conf").write_text("[Data]\n")
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
