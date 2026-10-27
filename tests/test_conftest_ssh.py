"""The ssh guard tests get (tests/conftest.py): a real ssh to a device raises
at once and fails the test, while the local queries the code makes of ssh
itself, and a test's own fake ssh, still run."""
from __future__ import annotations

import shutil
import subprocess
import sys

import pytest

from timecapsulesmb.transport.ssh import SshConnection, run_ssh

REAL_SSH = shutil.which("ssh")
needs_ssh = pytest.mark.skipif(REAL_SSH is None, reason="no ssh on this computer")


@needs_ssh
def test_an_unmocked_ssh_to_a_test_address_raises_and_is_recorded(block_real_ssh_connections) -> None:
    with pytest.raises(AssertionError, match="tests must not run ssh to a device"):
        run_ssh(SshConnection("root@10.0.0.2", "pw", ""), "/bin/echo ok", check=False, timeout=30)
    # The record is what fails a test that swallows the error.
    assert len(block_real_ssh_connections) == 1
    assert "root@10.0.0.2" in block_real_ssh_connections[0]
    block_real_ssh_connections.clear()


@needs_ssh
def test_local_ssh_queries_still_reach_the_real_ssh(block_real_ssh_connections) -> None:
    # The version check (require_local_ssh) and option queries never leave
    # this computer.
    version = subprocess.run(["ssh", "-V"], capture_output=True, text=True, check=False)
    assert version.returncode == 0
    assert "OpenSSH" in version.stderr
    query = subprocess.run(["ssh", "-Q", "mac"], capture_output=True, text=True, check=False)
    assert query.returncode == 0
    assert query.stdout.strip()
    assert block_real_ssh_connections == []


def test_a_tests_own_fake_ssh_runs(tmp_path, monkeypatch, block_real_ssh_connections) -> None:
    fake = tmp_path / "ssh"
    fake.write_text(f"#!{sys.executable}\nprint('fake')\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    result = subprocess.run(["ssh", "root@10.0.0.2", "true"], capture_output=True, text=True, check=False)
    assert result.stdout == "fake\n"
    assert block_real_ssh_connections == []


@needs_ssh
@pytest.mark.real_ssh
def test_a_test_marked_real_ssh_is_not_guarded() -> None:
    # The real ssh starts; its argument error shows it ran.
    result = subprocess.run(["ssh", "-Z"], capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert "illegal option" in result.stderr or "unknown option" in result.stderr
