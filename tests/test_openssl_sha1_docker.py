"""The real ssh transport on Linux against a server limited to the Time Capsule's algorithms.

Opt-in (TCAPSULE_DOCKER_TESTS=1, Docker running): it pulls alpine:3.20 and one
image per distro and takes several minutes. Red Hat's OpenSSL refuses SHA-1
signatures by default, and the server signs only with ssh-rsa, as the Time
Capsule does, so on Fedora and RHEL this is the field failure and its fix end
to end. Debian's OpenSSL has no such setting: nothing may change there.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import unittest
import uuid
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
ENABLED = os.environ.get("TCAPSULE_DOCKER_TESTS") == "1" and shutil.which("docker") is not None
SSHD_START_TIMEOUT_SEC = 180

# sshd with only what Apple's SSH server offers.
SERVER_SCRIPT = r"""
apk add --no-cache openssh-server
ssh-keygen -q -t rsa -b 2048 -N '' -f /etc/ssh/ssh_host_rsa_key
echo 'root:testpw' | chpasswd
cat > /etc/ssh/sshd_config <<'C'
HostKey /etc/ssh/ssh_host_rsa_key
HostKeyAlgorithms ssh-rsa
KexAlgorithms diffie-hellman-group14-sha1
MACs hmac-sha1
PermitRootLogin yes
PasswordAuthentication yes
C
exec /usr/sbin/sshd -D -e
"""

CLIENT_PROGRAM = r"""
import os
from unittest import mock
from timecapsulesmb.core.config import DEFAULTS
from timecapsulesmb.transport import ssh
from timecapsulesmb.transport.openssl_sha1 import sha1_signature_policy

print("policy:", sha1_signature_policy(os.environ))
connection = ssh.SshConnection("root@tcsshd", "testpw", DEFAULTS["TC_SSH_OPTS"])
print("transport:", ssh.run_ssh(connection, "echo ok").stdout.strip())
ssh.close_ssh_masters()
with mock.patch("timecapsulesmb.transport.ssh.sha1_signature_env", return_value={}):
    try:
        ssh.run_ssh(connection, "echo ok")
        print("without override: connected")
    except ssh.SshAlgorithmNegotiationError as exc:
        print("without override:", exc.algorithm)
"""

DNF_INSTALL = "dnf -y -q install python3 openssh-clients"
APT_INSTALL = "apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 openssh-client"
# (image, install command, policy state, result without the override)
DISTROS = (
    # The policy says no.
    ("fedora:44", DNF_INSTALL, "override", "signature"),
    # No key in the policy; Red Hat's OpenSSL refuses SHA-1 without one.
    ("rockylinux:9", DNF_INSTALL, "override", "signature"),
    ("quay.io/centos/centos:stream10", DNF_INSTALL, "override", "signature"),
    # No key, and its OpenSSL allows SHA-1: the override is harmless.
    ("amazonlinux:2023", DNF_INSTALL, "override", "connected"),
    # Not Red Hat's OpenSSL: nothing changes.
    ("debian:13", APT_INSTALL, "None", "connected"),
)


@unittest.skipUnless(ENABLED, "set TCAPSULE_DOCKER_TESTS=1 with Docker running")
class LinuxSshSha1DockerTests(unittest.TestCase):
    def docker(self, *args: str, **kwargs) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["docker", *args], capture_output=True, text=True, **kwargs)

    def start_server(self) -> str:
        tag = uuid.uuid4().hex[:8]
        network, server = f"tc-sha1-{tag}", f"tcsshd-{tag}"
        self.assertEqual(self.docker("network", "create", network).returncode, 0)
        self.addCleanup(self.docker, "network", "rm", network)
        started = self.docker(
            "run", "-d", "--name", server, "--network", network, "--network-alias", "tcsshd",
            "alpine:3.20", "sh", "-c", SERVER_SCRIPT,
        )
        self.assertEqual(started.returncode, 0, started.stderr)
        self.addCleanup(self.docker, "rm", "-f", server)
        # sshd -e logs this once it accepts connections; the apk install
        # before it takes as long as the mirror does.
        deadline = time.monotonic() + SSHD_START_TIMEOUT_SEC
        while True:
            logs = self.docker("logs", server)
            if "Server listening on" in logs.stdout + logs.stderr:
                return network
            running = self.docker("inspect", "-f", "{{.State.Running}}", server).stdout.strip()
            if running != "true" or time.monotonic() > deadline:
                self.fail(f"sshd did not start (running={running}):\n{logs.stdout}{logs.stderr}")
            time.sleep(1)

    def test_the_transport_logs_in_under_each_distros_default_policy(self) -> None:
        network = self.start_server()
        for image, install, policy, without_override in DISTROS:
            with self.subTest(image):
                client = self.docker(
                    "run", "--rm", "--network", network, "-v", f"{REPO_ROOT}:/repo:ro",
                    "-e", f"CLIENT_PROGRAM={CLIENT_PROGRAM}",
                    image, "sh", "-c", f'{install} && cd /repo && PYTHONPATH=/repo/src python3 -c "$CLIENT_PROGRAM"',
                    timeout=600,
                )
                output = client.stdout + client.stderr
                self.assertIn(f"policy: {policy}", client.stdout, output)
                self.assertIn("transport: ok", client.stdout, output)
                # Where the policy refuses SHA-1, the error without the config
                # names an SSH compatibility failure, not a network one.
                self.assertIn(f"without override: {without_override}", client.stdout, output)


if __name__ == "__main__":
    unittest.main()
