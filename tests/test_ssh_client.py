"""The SSH version preflight is shared by setup, doctor and transport."""
from __future__ import annotations

import os
import signal
import subprocess
from unittest import mock

import pytest

from timecapsulesmb.checks.local_tools import check_required_local_tools
from timecapsulesmb.cli import bootstrap
from timecapsulesmb.transport import ssh, ssh_client
from timecapsulesmb.transport.errors import SshClientConfigError, SshClientStoppedError


@pytest.fixture(autouse=True)
def clear_version_cache():
    ssh_client._validate_ssh.cache_clear()
    yield
    ssh_client._validate_ssh.cache_clear()


@pytest.mark.parametrize("version,accepted", [
    ("OpenSSH_8.0p1, OpenSSL 1.1.1", False),
    ("OpenSSH_8.3p1 Ubuntu-1", False),
    ("OpenSSH_8.4p1 Debian-5", True),
    ("OpenSSH_9.9p2, LibreSSL 3.3.6", True),
    ("OpenSSH_10.0p1", True),
    ("OtherSSH 10.0", False),
    ("", False),
])
@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_version_requirement(version, accepted, stream):
    result = subprocess.CompletedProcess([], 0, "", "")
    setattr(result, stream, version)
    with mock.patch.object(ssh_client.shutil, "which", return_value="/tools/ssh"), \
         mock.patch.object(ssh_client, "run_process", return_value=result) as run:
        if accepted:
            assert ssh_client.require_local_ssh() == "/tools/ssh"
        else:
            with pytest.raises(SshClientConfigError, match="OpenSSH 8.4 or newer"):
                ssh_client.require_local_ssh()
        assert run.call_args.args[0] == ["/tools/ssh", "-V"]
        assert run.call_args.kwargs["timeout"] == 5


def test_missing_ssh_does_not_spawn():
    with mock.patch.object(ssh_client.shutil, "which", return_value=None), \
         mock.patch.object(ssh_client, "run_process") as run:
        with pytest.raises(SshClientConfigError, match="missing"):
            ssh_client.require_local_ssh()
    run.assert_not_called()


@pytest.mark.parametrize("failure", [PermissionError("denied"), subprocess.TimeoutExpired(["ssh", "-V"], 5)])
def test_probe_failure_is_a_local_client_error(failure):
    with mock.patch.object(ssh_client.shutil, "which", return_value="/tools/ssh"), \
         mock.patch.object(ssh_client, "run_process", side_effect=failure):
        with pytest.raises(SshClientConfigError, match="Could not check local SSH client"):
            ssh_client.require_local_ssh()


def test_success_is_cached_by_executable_and_failures_are_not_cached():
    old = subprocess.CompletedProcess([], 0, "", "OpenSSH_8.0p1")
    new = subprocess.CompletedProcess([], 0, "", "OpenSSH_9.9p1")
    with mock.patch.object(ssh_client.shutil, "which", return_value="/tools/ssh") as which, \
         mock.patch.object(ssh_client, "run_process", side_effect=[old, new, new]) as run:
        with pytest.raises(SshClientConfigError):
            ssh_client.require_local_ssh()
        assert ssh_client.require_local_ssh() == "/tools/ssh"
        assert ssh_client.require_local_ssh() == "/tools/ssh"
        assert run.call_count == 2
        which.return_value = "/other/ssh"
        assert ssh_client.require_local_ssh() == "/other/ssh"
        assert run.call_count == 3


@pytest.mark.parametrize("returncode,error", [(1, SshClientConfigError), (-signal.SIGTERM, SshClientStoppedError)])
def test_unsuccessful_probe_is_not_accepted_even_with_version_output(returncode, error):
    with mock.patch.object(ssh_client.shutil, "which", return_value="/tools/ssh"), \
         mock.patch.object(ssh_client, "run_process", return_value=subprocess.CompletedProcess([], returncode, "", "OpenSSH_9.9")):
        with pytest.raises(error):
            ssh_client.require_local_ssh()


def test_doctor_reports_an_old_client_as_failure():
    with mock.patch("timecapsulesmb.checks.local_tools.command_exists", return_value=True), \
         mock.patch("timecapsulesmb.checks.local_tools.require_local_ssh", side_effect=SshClientConfigError("OpenSSH 8.4 or newer required")):
        results = check_required_local_tools()
    assert results[0].status == "FAIL"
    assert "8.4" in results[0].message
    assert results[1].status == "PASS"


@pytest.mark.parametrize("missing,intel,expected_checks", [([], False, 1), (["smbclient"], True, 1), (["ssh"], False, 1)])
def test_bootstrap_checks_existing_skipped_and_newly_installed_clients(missing, intel, expected_checks):
    with mock.patch.object(bootstrap, "_missing_required_host_tools", side_effect=[missing, []]), \
         mock.patch.object(bootstrap, "current_platform_label", return_value="macOS" if intel else "Linux"), \
         mock.patch.object(bootstrap, "_macos_intel_host", return_value=intel), \
         mock.patch.object(bootstrap, "_linux_install_plan", return_value=([["apt-get", "install", "openssh-client"]], "install ssh")), \
         mock.patch.object(bootstrap, "run") as install, \
         mock.patch.object(bootstrap, "require_local_ssh", side_effect=SshClientConfigError("Upgrade OpenSSH")) as check:
        with pytest.raises(bootstrap.BootstrapError, match="Upgrade OpenSSH"):
            bootstrap.install_required_host_tools()
    assert check.call_count == expected_checks
    assert install.call_count == (1 if "ssh" in missing else 0)


def test_transport_rejects_old_ssh_before_command_or_tunnel_start(tmp_path):
    # Exercise real -V output through the same executable that would authenticate.
    binary = tmp_path / "ssh"
    binary.write_text('#!/bin/sh\n[ "$1" = -V ] || exit 99\necho OpenSSH_8.0p1 >&2\n')
    binary.chmod(0o755)
    with mock.patch.dict(os.environ, {"PATH": str(tmp_path)}), \
         mock.patch.object(ssh, "run_process") as command, \
         mock.patch.object(ssh, "popen_process") as tunnel:
        conn = ssh.SshConnection("root@device", "pw", "")
        with pytest.raises(SshClientConfigError, match="OpenSSH_8.0"):
            ssh.run_ssh(conn, "true")
        with pytest.raises(SshClientConfigError, match="OpenSSH_8.0"):
            with ssh.ssh_local_forward(conn, local_port=12345, remote_host="127.0.0.1", remote_port=445):
                pytest.fail("started tunnel")
        command.assert_not_called()
        tunnel.assert_not_called()
