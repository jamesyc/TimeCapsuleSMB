"""build/samba4-cross-exec.sh runs a cross-built binary on the device over ssh.

A fake ssh on PATH records each remote command and plays the device's side, so
these tests check what the wrapper asks the device to run, not how it is written.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from tests.build_wrapper_harness import REPO_ROOT

FAKE_SSH = """#!/bin/sh
for last; do :; done
printf '%s\\n' "$last" >> "$FAKE_SSH_LOG"
case "$last" in
    *"df -k"*) [ -n "${FAKE_DF_LINE:-}" ] && printf '%s\\n' "$FAKE_DF_LINE" ;;
    *"cat > "*) cat > /dev/null ;;
    *"chmod +x"*) exit "${FAKE_RUN_STATUS:-0}" ;;
esac
exit 0
"""


def run_wrapper(tmp_path: Path, *args: str, **env: str) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    ssh = bin_dir / "ssh"
    ssh.write_text(FAKE_SSH)
    ssh.chmod(0o755)
    probe = tmp_path / "probe"
    probe.write_bytes(b"\x7fELF")
    log = tmp_path / "ssh.log"
    log.unlink(missing_ok=True)
    base = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "FAKE_SSH_LOG": str(log),
        "FAKE_DF_LINE": "/dev/dk2 1000 10 990 1% /Volumes/dk2",
        "TC_ENV_FILE": "/dev/null",
        "SDK_FAMILY": "netbsd7",
        "TC_NETBSD7_HOST": "root@device",
    }
    base.update(env)
    result = subprocess.run(["sh", str(REPO_ROOT / "build/samba4-cross-exec.sh"), str(probe), *args],
                            env=base, capture_output=True, text=True, timeout=30)
    return result, log.read_text().splitlines() if log.exists() else []


def test_binary_runs_with_tmpdir_in_the_volumes_scratch_directory(tmp_path):
    result, commands = run_wrapper(tmp_path, "full_buffer", "it's",
                                   CROSS_EXEC_REMOTE_DIR="/Volumes/dk2/tc-test")

    assert result.returncode == 0, result.stderr
    run = commands[-1]
    remote_bin = run.split('"')[1]
    assert remote_bin.startswith("/Volumes/dk2/tc-test/probe.")
    # Scratch files (8 MiB for tc_aio_fork_test's full_buffer case) must land on
    # the data disk, not in the login directory on the device's RAM root.
    assert f"TMPDIR='/Volumes/dk2/tc-test' '{remote_bin}' 'full_buffer' 'it'\\''s'" in run
    assert run.endswith(f"rm -f '{remote_bin}'; exit $rc")
    assert commands[:2] == [
        "df -k '/Volumes/dk2/tc-test' 2>/dev/null | sed -n '2p' | sed -n '/[[:space:]]\\/Volumes\\//p'",
        "mkdir -p '/Volumes/dk2/tc-test'",
    ]


def test_lane_default_probe_directory_is_also_the_binary_tmpdir(tmp_path):
    # Without CROSS_EXEC_REMOTE_DIR, env.sh picks the lane's /tmp probe
    # directory (configure probes); no /Volumes mount check applies there.
    result, commands = run_wrapper(tmp_path, "probe-arg")

    assert result.returncode == 0, result.stderr
    assert not any(command.startswith("df -k") for command in commands)
    assert ("TMPDIR='/tmp/tc-samba4x-probes-netbsd7' '/tmp/tc-samba4x-probes-netbsd7/probe."
            in commands[-1])


def test_unmounted_volumes_directory_is_refused_before_upload(tmp_path):
    result, commands = run_wrapper(tmp_path, "full_buffer",
                                   CROSS_EXEC_REMOTE_DIR="/Volumes/dk2/tc-test", FAKE_DF_LINE="")

    assert result.returncode == 1
    assert "not a mounted /Volumes filesystem" in result.stderr
    assert len(commands) == 1 and commands[0].startswith("df -k")


def test_binary_failure_status_reaches_the_caller(tmp_path):
    result, commands = run_wrapper(tmp_path, "full_buffer",
                                   CROSS_EXEC_REMOTE_DIR="/Volumes/dk2/tc-test", FAKE_RUN_STATUS="90")

    assert result.returncode == 90
    assert "TMPDIR='/Volumes/dk2/tc-test'" in commands[-1]
