"""Stage and publish real RAM files, retaining applied config on preparation failures."""
import os
import plistlib
import shutil
import subprocess

import pytest
from tests.native.build import ROOT, instrumentation_flags


@pytest.fixture(scope="module")
def stage_tools(tmp_path_factory):
    root = tmp_path_factory.mktemp("stage-native")
    binary = root / "stage"
    modules = ["samba/staging.c", "samba/config.c", "storage/runtime.c", "storage/mast.c",
               "storage/shares.c", "common/config.c", "common/worker.c", "common/process.c",
               "common/parent.c", "common/acp.c"]
    subprocess.run(["cc", "-D_GNU_SOURCE", "-DTC_NATIVE_TEST", "-Wall", "-Wextra", "-Werror",
                    *instrumentation_flags(), f'-DTC_FLASH_CONFIG_PATH="{root}/runtime.conf"',
                    f'-DTC_RAM_ROOT="{root}/ram"', f'-DTC_VOLUMES_ROOT="{root}"',
                    "-I", str(ROOT / "build/native"),
                    *(str(ROOT / "build/native" / module) for module in modules),
                    str(ROOT / "tests/native/unit/test_staging.c"), "-o", str(binary)],
                   check=True, capture_output=True)
    return root, binary


@pytest.fixture
def stage(stage_tools):
    root, binary = stage_tools
    for name in ("dk2", "ram"):
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
