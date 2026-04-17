from __future__ import annotations

import subprocess
import sys
import unittest
from tempfile import NamedTemporaryFile
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from timecapsulesmb.transport import ssh as ssh_transport


class SSHTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        ssh_transport._ssh_option_supported.cache_clear()

    def tearDown(self) -> None:
        ssh_transport._ssh_option_supported.cache_clear()

    def test_normalize_ssh_tokens_rewrites_pubkeyacceptedalgorithms_for_older_ssh(self) -> None:
        with mock.patch(
            "timecapsulesmb.transport.ssh._ssh_option_supported",
            side_effect=lambda name: name == "PubkeyAcceptedKeyTypes",
        ):
            tokens = ssh_transport._normalize_ssh_tokens(
                "-o HostKeyAlgorithms=+ssh-rsa -o PubkeyAcceptedAlgorithms=+ssh-rsa -o KexAlgorithms=+ssh-rsa"
            )
        self.assertEqual(
            tokens,
            [
                "-o",
                "HostKeyAlgorithms=+ssh-rsa",
                "-o",
                "PubkeyAcceptedKeyTypes=+ssh-rsa",
                "-o",
                "KexAlgorithms=+ssh-rsa",
            ],
        )

    def test_run_ssh_uses_normalized_legacy_pubkey_option(self) -> None:
        with mock.patch(
            "timecapsulesmb.transport.ssh._ssh_option_supported",
            side_effect=lambda name: name == "PubkeyAcceptedKeyTypes",
        ):
            with mock.patch(
                "timecapsulesmb.transport.ssh._spawn_with_password",
                return_value=(0, "ok\n"),
            ) as spawn_mock:
                proc = ssh_transport.run_ssh(
                    "root@192.168.1.67",
                    "pw",
                    "-o PubkeyAcceptedAlgorithms=+ssh-rsa",
                    "/bin/echo ok",
                    check=False,
                    timeout=10,
                )
        self.assertEqual(proc.returncode, 0)
        cmd = spawn_mock.call_args.args[0]
        self.assertEqual(
            cmd,
            [
                "ssh",
                "-o",
                "PubkeyAcceptedKeyTypes=+ssh-rsa",
                "root@192.168.1.67",
                "/bin/echo ok",
            ],
        )

    def test_run_ssh_retries_transient_permission_denied(self) -> None:
        with mock.patch(
            "timecapsulesmb.transport.ssh._ssh_option_supported",
            return_value=True,
        ):
            with mock.patch(
                "timecapsulesmb.transport.ssh._spawn_with_password",
                side_effect=[
                    (255, "Permission denied, please try again.\n"),
                    (0, "ok\n"),
                ],
            ) as spawn_mock:
                with mock.patch("timecapsulesmb.transport.ssh.time.sleep") as sleep_mock:
                    proc = ssh_transport.run_ssh(
                        "root@192.168.1.118",
                        "pw",
                        "-o StrictHostKeyChecking=no",
                        "/bin/echo ok",
                        check=False,
                        timeout=10,
                    )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(spawn_mock.call_count, 2)
        sleep_mock.assert_called_once_with(1)

    def test_normalize_ssh_tokens_expands_identity_and_preserves_proxyjump(self) -> None:
        with mock.patch(
            "timecapsulesmb.transport.ssh._ssh_option_supported",
            return_value=True,
        ):
            tokens = ssh_transport._normalize_ssh_tokens(
                "-J jamesyc@ig1wx38mgh6to6vo.myfritz.net:22123 -i ~/.ssh/id_ed25519 -o IdentitiesOnly=yes"
            )
        self.assertEqual(
            tokens,
            [
                "-J",
                "jamesyc@ig1wx38mgh6to6vo.myfritz.net:22123",
                "-i",
                str(Path("~/.ssh/id_ed25519").expanduser()),
                "-o",
                "IdentitiesOnly=yes",
            ],
        )

    def test_normalize_ssh_tokens_preserves_proxycommand_payload(self) -> None:
        with mock.patch(
            "timecapsulesmb.transport.ssh._ssh_option_supported",
            return_value=True,
        ):
            tokens = ssh_transport._normalize_ssh_tokens(
                "-o ProxyCommand=ssh -4 -i ~/.ssh/id_ed25519 -o IdentitiesOnly=yes -W %h:%p -p 22123 jamesyc@ig1wx38mgh6to6vo.myfritz.net"
            )
        self.assertEqual(
            tokens,
            [
                "-o",
                "ProxyCommand=ssh",
                "-4",
                "-i",
                str(Path("~/.ssh/id_ed25519").expanduser()),
                "-o",
                "IdentitiesOnly=yes",
                "-W",
                "%h:%p",
                "-p",
                "22123",
                "jamesyc@ig1wx38mgh6to6vo.myfritz.net",
            ],
        )

    def test_run_ssh_preserves_proxyjump_options(self) -> None:
        with mock.patch(
            "timecapsulesmb.transport.ssh._ssh_option_supported",
            return_value=True,
        ):
            with mock.patch(
                "timecapsulesmb.transport.ssh._spawn_with_password",
                return_value=(0, "ok\n"),
            ) as spawn_mock:
                ssh_transport.run_ssh(
                    "root@192.168.1.118",
                    "pw",
                    "-J jamesyc@ig1wx38mgh6to6vo.myfritz.net:22123 -o HostKeyAlgorithms=+ssh-rsa",
                    "/bin/echo ok",
                    check=False,
                    timeout=10,
                )
        cmd = spawn_mock.call_args.args[0]
        self.assertEqual(
            cmd,
            [
                "ssh",
                "-J",
                "jamesyc@ig1wx38mgh6to6vo.myfritz.net:22123",
                "-o",
                "HostKeyAlgorithms=+ssh-rsa",
                "root@192.168.1.118",
                "/bin/echo ok",
            ],
        )

    def test_ssh_option_supported_returns_false_for_bad_configuration_option(self) -> None:
        with mock.patch(
            "timecapsulesmb.transport.ssh.subprocess.run",
            return_value=subprocess.CompletedProcess(
                ["ssh"],
                255,
                stdout="",
                stderr="command-line: line 0: Bad configuration option: pubkeyacceptedalgorithms\n",
            ),
        ):
            self.assertFalse(ssh_transport._ssh_option_supported("PubkeyAcceptedAlgorithms"))

    def test_verify_remote_size_retries_transient_failure(self) -> None:
        with NamedTemporaryFile() as tmp:
            src = Path(tmp.name)
            src.write_bytes(b"hello")
            expected_size = src.stat().st_size
            responses = [
                subprocess.CompletedProcess(["ssh"], 1, stdout="Permission denied, please try again.\n", stderr=""),
                subprocess.CompletedProcess(["ssh"], 0, stdout=f"{expected_size}\n", stderr=""),
            ]
            with mock.patch(
                "timecapsulesmb.transport.ssh.run_ssh",
                side_effect=responses,
            ) as run_ssh_mock:
                with mock.patch("timecapsulesmb.transport.ssh.time.sleep") as sleep_mock:
                    ssh_transport._verify_remote_size(
                        "root@192.168.1.118",
                        "pw",
                        "-o StrictHostKeyChecking=no",
                        src,
                        "/tmp/test-upload",
                        timeout=30,
                    )
        self.assertEqual(run_ssh_mock.call_count, 2)
        sleep_mock.assert_called_once_with(1)

    def test_run_scp_cat_fallback_retries_transient_permission_denied(self) -> None:
        with NamedTemporaryFile() as tmp:
            src = Path(tmp.name)
            src.write_bytes(b"hello")
            with mock.patch("timecapsulesmb.transport.ssh._ssh_option_supported", return_value=True):
                with mock.patch(
                    "timecapsulesmb.transport.ssh.run_ssh",
                    side_effect=[subprocess.CompletedProcess(["ssh"], 1, stdout="", stderr=""), subprocess.CompletedProcess(["ssh"], 0, stdout="5\n", stderr="")],
                ) as run_ssh_mock:
                    with mock.patch("timecapsulesmb.transport.ssh.shutil.which", return_value="/opt/homebrew/bin/sshpass"):
                        with mock.patch(
                            "timecapsulesmb.transport.ssh.subprocess.run",
                            side_effect=[
                                subprocess.CompletedProcess(["sshpass"], 255, stdout=b"Permission denied, please try again.\n", stderr=b""),
                                subprocess.CompletedProcess(["sshpass"], 0, stdout=b"", stderr=b""),
                            ],
                        ) as subprocess_run_mock:
                            with mock.patch("timecapsulesmb.transport.ssh.time.sleep") as sleep_mock:
                                ssh_transport.run_scp(
                                    "root@192.168.1.118",
                                    "pw",
                                    "-o StrictHostKeyChecking=no",
                                    src,
                                    "/tmp/test-upload",
                                    timeout=10,
                                )
        self.assertEqual(subprocess_run_mock.call_count, 2)
        sleep_mock.assert_called_once_with(1)
        self.assertEqual(run_ssh_mock.call_count, 2)


if __name__ == "__main__":
    unittest.main()
