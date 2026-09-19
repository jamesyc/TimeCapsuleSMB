from pathlib import Path
import subprocess

from tests.native.build import ROOT


def test_canonical_shares_are_utf8_safe_unique_and_availability_filtered(tmp_path):
    output = tmp_path / "shares"
    native = ROOT / "build/native"
    result = subprocess.run(
        ["cc", "-D_GNU_SOURCE", "-Wall", "-Wextra", "-Werror",
         "-I", str(native / "storage"), str(native / "storage/shares.c"),
         str(ROOT / "tests/native/unit/test_shares.c"), "-o", str(output)],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    subprocess.run([str(output)], check=True, timeout=5)


def test_samba_generation_replaces_ram_binary_and_writes_runtime_files(tmp_path):
    output = tmp_path / "runtime"
    ram = tmp_path / "ram"
    payload = tmp_path / "payload"
    payload.mkdir()
    native = ROOT / "build/native"
    result = subprocess.run(
        ["cc", "-D_GNU_SOURCE", "-Wall", "-Wextra", "-Werror",
         f'-DTC_SAMBA_RAM_ROOT="{ram}"',
         "-I", str(native / "samba"), "-I", str(native / "common"), "-I", str(native / "storage"),
         str(native / "samba/runtime.c"), str(native / "common/config.c"),
         str(ROOT / "tests/native/unit/test_samba_runtime.c"), "-o", str(output)],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    run = subprocess.run([str(output), str(payload)], capture_output=True, text=True, timeout=5)
    assert run.returncode == 0, run.stderr
    config = (ram / "private/smb.conf").read_text()
    assert "interfaces = 127.0.0.1/8 ::1/128 10.0.0.2/24" in config
    assert "server smb encrypt = required" in config
    assert f"ncalrpc dir = {ram}/var/run/ncalrpc" in config
    assert "[Data]\n    path = /Volumes/dk2/ShareRoot" in config
    assert f"xattr_tdb:file = {payload}/private/xattr.tdb" in config
    assert (ram / "sbin/smbd").read_text() == "fake-smbd"
    assert "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA" in (ram / "private/smbpasswd").read_text()
    assert (ram / "var/run/ncalrpc").is_dir()
