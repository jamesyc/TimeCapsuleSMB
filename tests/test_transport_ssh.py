from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import sys
import time
import unittest
from tempfile import NamedTemporaryFile, TemporaryDirectory
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from timecapsulesmb.transport import errors as transport_errors
from timecapsulesmb.transport import ssh as ssh_transport
from timecapsulesmb.transport.local import find_free_local_port


AUTHENTICATED_LOG = 'Authenticated to device ([192.0.2.1]:22) using "password".\n'


class DecodeTrapBytes(bytes):
    def decode(self, *args: object, **kwargs: object) -> str:
        raise AssertionError("stdout should not be decoded")


# Patch the transport's time namespace, not time.sleep on the shared stdlib
# module: subprocess.wait(timeout=...) also sleeps while reaping local probes.
class SSHTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        ssh_transport._ssh_option_supported.cache_clear()
        ssh_transport._local_ssh_macs.cache_clear()
        self._local_macs_patch = mock.patch("timecapsulesmb.transport.ssh._local_ssh_macs", return_value=())
        self._local_macs_patch.start()
        self.addCleanup(self._local_macs_patch.stop)

    def tearDown(self) -> None:
        ssh_transport._ssh_option_supported.cache_clear()
        if hasattr(ssh_transport._local_ssh_macs, "cache_clear"):
            ssh_transport._local_ssh_macs.cache_clear()

    def test_is_ssh_timeout_error_matches_direct_timeout(self) -> None:
        error = ssh_transport.SshCommandTimeout("Timed out waiting for ssh command to finish: sync")

        self.assertTrue(transport_errors.is_ssh_timeout_error(error))

    def test_is_ssh_timeout_error_matches_wrapped_transport_timeout(self) -> None:
        try:
            try:
                raise ssh_transport.SshCommandTimeout("Timed out copying manager.sh")
            except ssh_transport.SshCommandTimeout as exc:
                raise ssh_transport.SshError(str(exc)) from exc
        except ssh_transport.SshError as error:
            self.assertTrue(transport_errors.is_ssh_timeout_error(error))

    def test_is_ssh_timeout_error_ignores_other_transport_errors(self) -> None:
        self.assertFalse(transport_errors.is_ssh_timeout_error(ssh_transport.SshError("permission denied")))

    @staticmethod
    def ssh_runs(*runs):
        """Fake ssh runs through run_process: each (returncode, output,
        client_log) writes the log ssh would write and returns the output."""
        runs = iter(runs)

        def run(command, **kwargs):
            if "-E" not in command:  # a local ssh option probe
                return subprocess.CompletedProcess(command, 0, "", "")
            returncode, output, diagnostics = next(runs)
            Path(command[command.index("-E") + 1]).write_text(diagnostics)
            stdout = output.encode("utf-8") if isinstance(output, str) else output
            stderr = None if kwargs.get("stderr") == subprocess.STDOUT else b""
            return subprocess.CompletedProcess(command, returncode, stdout, stderr)

        return run

    @classmethod
    def authenticated(cls, returncode: int = 0, output: str | bytes = "ok\n"):
        return cls.ssh_runs((returncode, output, AUTHENTICATED_LOG))

    @staticmethod
    def completed_run(process: subprocess.CompletedProcess[bytes], diagnostics: str | None = None):
        def run(command, **_kwargs):
            if "-E" not in command:  # a local ssh option probe
                return subprocess.CompletedProcess(command, 0, "", "")
            Path(command[command.index("-E") + 1]).write_text(
                AUTHENTICATED_LOG if diagnostics is None else diagnostics
            )
            return process

        return run

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
            with mock.patch("timecapsulesmb.transport.ssh.run_process", side_effect=self.authenticated()) as run:
                proc = ssh_transport.run_ssh(
                    ssh_transport.SshConnection("root@192.168.1.67", "pw", "-o PubkeyAcceptedAlgorithms=+ssh-rsa"),
                    "/bin/echo ok",
                    check=False,
                    timeout=10,
                )
        self.assertEqual(proc.returncode, 0)
        cmd = run.call_args.args[0]
        self.assertIn("PubkeyAuthentication=no", cmd)
        self.assertIn("PubkeyAcceptedKeyTypes=+ssh-rsa", cmd)
        self.assertIn("NumberOfPasswordPrompts=1", cmd)
        self.assertEqual(cmd[-2:], ["root@192.168.1.67", ssh_transport.CLIENT_HOSTS_LINE_COMMAND + "/bin/echo ok"])

    def test_normalize_ssh_tokens_adds_supported_legacy_airport_macs_when_missing(self) -> None:
        with mock.patch("timecapsulesmb.transport.ssh._local_ssh_macs", return_value=("hmac-sha1", "hmac-md5-96")):
            tokens = ssh_transport._normalize_ssh_tokens("-o HostKeyAlgorithms=+ssh-rsa")

        self.assertEqual(
            tokens,
            [
                "-o",
                "HostKeyAlgorithms=+ssh-rsa",
                "-o",
                "MACs=+hmac-sha1,hmac-md5-96",
            ],
        )

    def test_normalize_ssh_tokens_preserves_explicit_mac_option(self) -> None:
        with mock.patch("timecapsulesmb.transport.ssh._local_ssh_macs", return_value=("hmac-sha1", "hmac-md5-96")):
            tokens = ssh_transport._normalize_ssh_tokens("-m hmac-md5-96 -o HostKeyAlgorithms=+ssh-rsa")

        self.assertEqual(
            tokens,
            [
                "-m",
                "hmac-md5-96",
                "-o",
                "HostKeyAlgorithms=+ssh-rsa",
            ],
        )

    def test_client_diagnostics_detect_no_matching_mac_offer(self) -> None:
        line = (
            "Unable to negotiate with 192.168.200.214 port 22: no matching MAC found. "
            "Their offer: hmac-md5,hmac-sha1,hmac-ripemd160,hmac-ripemd160@openssh.com,hmac-sha1-96,hmac-md5-96"
        )

        error = ssh_transport.parse_ssh_client_diagnostics(line).error

        self.assertIsInstance(error, ssh_transport.SshAlgorithmNegotiationError)
        assert isinstance(error, ssh_transport.SshAlgorithmNegotiationError)
        self.assertEqual(error.algorithm, "mac")
        self.assertEqual(error.offered[0:2], ("hmac-md5", "hmac-sha1"))
        self.assertEqual(str(error), line)

    def test_client_diagnostics_detect_a_connection_this_computer_dropped(self) -> None:
        for line in (
            "ssh: connect to host 192.168.1.22 port 22: Bad file descriptor",
            "ssh: connect to host fe80::dea4:caff:feed:c031%en0 port 22: Bad file descriptor",
        ):
            with self.subTest(line=line):
                error = ssh_transport.parse_ssh_client_diagnostics(f"{line}\n").error

                self.assertIsInstance(error, transport_errors.SshLocalNetworkFilteredError)
                # Callers that only know transport failures still catch it.
                self.assertIsInstance(error, transport_errors.SshNetworkError)
                self.assertEqual(str(error), f"{transport_errors.local_network_filtered_message()} ({line})")

    def test_client_diagnostics_keep_other_failures_out_of_the_dropped_connection_error(self) -> None:
        cases = (
            ("ssh: connect to host 192.168.1.22 port 22: Connection refused", transport_errors.SshNetworkError),
            ("ssh: connect to host 192.168.1.22 port 22: No route to host", transport_errors.SshNetworkError),
            # The same errno from ssh-agent is not about the device connection.
            ("Error connecting to agent: Bad file descriptor", type(None)),
        )
        for line, expected in cases:
            with self.subTest(line=line):
                error = ssh_transport.parse_ssh_client_diagnostics(f"{line}\n").error

                self.assertIs(type(error), expected)

    def test_dropped_connection_message_names_a_mac_only_on_macos(self) -> None:
        self.assertTrue(transport_errors.local_network_filtered_message(platform="darwin").startswith("This Mac dropped"))
        self.assertTrue(transport_errors.local_network_filtered_message(platform="linux").startswith("This computer dropped"))
        self.assertEqual(
            transport_errors.LOCAL_NETWORK_FILTERED_MESSAGE,
            transport_errors.local_network_filtered_message(platform="darwin"),
        )

    def test_client_diagnostics_detect_auth_rejection(self) -> None:
        error = ssh_transport.parse_ssh_client_diagnostics("Permission denied, please try again.\n").error

        self.assertIsInstance(error, ssh_transport.SshAuthenticationError)

    def test_run_ssh_retries_transient_permission_denied(self) -> None:
        run = self.ssh_runs(
            (255, "", "Permission denied, please try again.\n"),
            (0, "ok\n", AUTHENTICATED_LOG),
        )
        with mock.patch("timecapsulesmb.transport.ssh._ssh_option_supported", return_value=True), \
             mock.patch("timecapsulesmb.transport.ssh.run_process", side_effect=run) as run_mock, \
             mock.patch("timecapsulesmb.transport.ssh.time") as time_mock:
            proc = ssh_transport.run_ssh(
                ssh_transport.SshConnection("root@192.168.1.118", "pw", "-o StrictHostKeyChecking=no"),
                "/bin/echo ok",
                check=False,
                timeout=10,
            )
        self.assertEqual((proc.returncode, proc.stdout), (0, "ok\n"))
        self.assertEqual(run_mock.call_count, 2)
        time_mock.sleep.assert_called_once_with(1)

    def test_run_ssh_does_not_retry_passwordless_auth_rejection(self) -> None:
        run = self.ssh_runs((255, "", "root@device: Permission denied (publickey).\n"))
        with mock.patch("timecapsulesmb.transport.ssh._ssh_option_supported", return_value=True), \
             mock.patch("timecapsulesmb.transport.ssh.run_process", side_effect=run) as run_mock, \
             mock.patch("timecapsulesmb.transport.ssh.time") as time_mock:
            with self.assertRaises(ssh_transport.SshAuthenticationError):
                ssh_transport.run_ssh(
                    ssh_transport.SshConnection("root@192.168.1.118", "", "-o StrictHostKeyChecking=no"),
                    "/bin/echo ok",
                    check=False,
                    timeout=10,
                )

        run_mock.assert_called_once()
        time_mock.sleep.assert_not_called()

    def test_run_ssh_does_not_retry_remote_permission_denied(self) -> None:
        with mock.patch("timecapsulesmb.transport.ssh._ssh_option_supported", return_value=True), \
             mock.patch("timecapsulesmb.transport.ssh.run_process",
                        side_effect=self.authenticated(1, "mutation complete\nPermission denied\n")) as run, \
             mock.patch("timecapsulesmb.transport.ssh.time") as time_mock:
            process = ssh_transport.run_ssh(
                ssh_transport.SshConnection("device", "pw", ""),
                "mutating-command",
                check=False,
                timeout=10,
            )
        self.assertEqual(process.returncode, 1)
        self.assertEqual(process.stdout, "mutation complete\nPermission denied\n")
        run.assert_called_once()
        time_mock.sleep.assert_not_called()

    def test_run_ssh_check_false_returns_nonzero_process(self) -> None:
        with mock.patch("timecapsulesmb.transport.ssh._ssh_option_supported", return_value=True):
            with mock.patch(
                "timecapsulesmb.transport.ssh.run_process",
                side_effect=self.authenticated(7, "remote command failed\n"),
            ):
                proc = ssh_transport.run_ssh(
                    ssh_transport.SshConnection("root@192.168.1.118", "pw", "-o StrictHostKeyChecking=no"),
                    "/bin/false",
                    check=False,
                    timeout=10,
                )
        self.assertEqual(proc.returncode, 7)
        self.assertEqual(proc.stdout, "remote command failed\n")

    def test_run_ssh_check_true_raises_on_nonzero_process(self) -> None:
        with mock.patch("timecapsulesmb.transport.ssh._ssh_option_supported", return_value=True):
            with mock.patch(
                "timecapsulesmb.transport.ssh.run_process",
                side_effect=self.authenticated(7, "remote command failed\n"),
            ):
                with self.assertRaises(ssh_transport.SshError) as exc:
                    ssh_transport.run_ssh(
                        ssh_transport.SshConnection("root@192.168.1.118", "pw", "-o StrictHostKeyChecking=no"),
                        "/bin/false",
                        check=True,
                        timeout=10,
                    )
        self.assertNotIsInstance(exc.exception, SystemExit)
        self.assertEqual(str(exc.exception), "remote command failed")

    def test_run_ssh_check_true_uses_rc_fallback_when_remote_output_is_empty(self) -> None:
        run = self.ssh_runs((
            7,
            "",
            "Warning: Permanently added device to the list of known hosts.\n" + AUTHENTICATED_LOG,
        ))
        with mock.patch("timecapsulesmb.transport.ssh._ssh_option_supported", return_value=True):
            with mock.patch("timecapsulesmb.transport.ssh.run_process", side_effect=run):
                with self.assertRaises(ssh_transport.SshError) as exc:
                    ssh_transport.run_ssh(
                        ssh_transport.SshConnection("root@192.168.1.118", "pw", "-o StrictHostKeyChecking=no"),
                        "/bin/false",
                        check=True,
                        timeout=10,
                    )
        self.assertNotIsInstance(exc.exception, SystemExit)
        self.assertEqual(str(exc.exception), "ssh command failed with rc=7")

    def test_run_ssh_timeout_error_includes_remote_command_summary(self) -> None:
        remote_cmd = "/bin/sh -c 'echo one\necho two'"
        with mock.patch("timecapsulesmb.transport.ssh._ssh_option_supported", return_value=True):
            with mock.patch(
                "timecapsulesmb.transport.ssh.run_process",
                side_effect=subprocess.TimeoutExpired("ssh", 10),
            ):
                with self.assertRaises(ssh_transport.SshCommandTimeout) as exc:
                    ssh_transport.run_ssh(
                        ssh_transport.SshConnection("root@192.168.1.118", "secret-password", "-o StrictHostKeyChecking=no"),
                        remote_cmd,
                        check=False,
                        timeout=10,
                    )
        self.assertNotIsInstance(exc.exception, SystemExit)
        self.assertEqual(
            str(exc.exception),
            "Timed out waiting for ssh command to finish: /bin/sh -c 'echo one echo two'",
        )

    def test_summarize_remote_command_truncates_long_commands(self) -> None:
        summary = ssh_transport._summarize_remote_command("x" * (ssh_transport.REMOTE_COMMAND_SUMMARY_LIMIT + 20))
        self.assertEqual(len(summary), ssh_transport.REMOTE_COMMAND_SUMMARY_LIMIT)
        self.assertTrue(summary.endswith("..."))

    def test_client_diagnostics_detect_forward_bind_failure(self) -> None:
        output = (
            "bind [127.0.0.1]:108: Permission denied\n"
            "channel_setup_fwd_listener_tcpip: cannot listen to port: 108\n"
            "NetBSD\n"
        )
        error = ssh_transport.parse_ssh_client_diagnostics(output).error
        self.assertEqual(str(error), "Connecting to the device failed, SSH error: bind [127.0.0.1]:108: Permission denied")

    def test_run_ssh_raises_on_ssh_transport_warning_even_with_zero_exit(self) -> None:
        run = self.ssh_runs((
            0,
            "NetBSD\n6.0\nevbarm\n",
            "bind [127.0.0.1]:108: Permission denied\n"
            "channel_setup_fwd_listener_tcpip: cannot listen to port: 108\n",
        ))
        with mock.patch("timecapsulesmb.transport.ssh._ssh_option_supported", return_value=True), \
             mock.patch("timecapsulesmb.transport.ssh.run_process", side_effect=run):
            with self.assertRaises(ssh_transport.SshError) as exc:
                ssh_transport.run_ssh(
                    ssh_transport.SshConnection("root@192.168.1.67", "pw", "-o LocalForward=127.0.0.1:108:127.0.0.1:108"),
                    "/bin/echo ok",
                    check=False,
                    timeout=10,
                )
        self.assertNotIsInstance(exc.exception, SystemExit)
        self.assertEqual(
            str(exc.exception),
            "Connecting to the device failed, SSH error: bind [127.0.0.1]:108: Permission denied",
        )

    def test_run_ssh_keeps_client_warnings_out_of_remote_output(self) -> None:
        run = self.ssh_runs((
            0,
            "NetBSD\n4.0_STABLE\nearmv4\n",
            "Warning: Permanently added device to the list of known hosts.\n"
            "** WARNING: connection is not using a post-quantum key exchange algorithm.\n"
            + AUTHENTICATED_LOG,
        ))
        with mock.patch("timecapsulesmb.transport.ssh._ssh_option_supported", return_value=True), \
             mock.patch("timecapsulesmb.transport.ssh.run_process", side_effect=run):
            proc = ssh_transport.run_ssh(
                ssh_transport.SshConnection("root@192.168.1.118", "pw", "-o StrictHostKeyChecking=no"),
                "uname -s",
                check=False,
                timeout=10,
            )
        self.assertEqual(proc.stdout, "NetBSD\n4.0_STABLE\nearmv4\n")

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
        with mock.patch("timecapsulesmb.transport.ssh._ssh_option_supported", return_value=True):
            with mock.patch("timecapsulesmb.transport.ssh.run_process", side_effect=self.authenticated()) as run:
                ssh_transport.run_ssh(
                    ssh_transport.SshConnection("root@192.168.1.118", "pw", "-J jamesyc@ig1wx38mgh6to6vo.myfritz.net:22123 -o HostKeyAlgorithms=+ssh-rsa"),
                    "/bin/echo ok",
                    check=False,
                    timeout=10,
                )
        cmd = run.call_args.args[0]
        self.assertIn("-J", cmd)
        self.assertIn("jamesyc@ig1wx38mgh6to6vo.myfritz.net:22123", cmd)
        self.assertEqual(cmd[-2:], ["root@192.168.1.118", ssh_transport.CLIENT_HOSTS_LINE_COMMAND + "/bin/echo ok"])

    def test_run_ssh_respects_explicit_identity_without_restricting_agent(self) -> None:
        with mock.patch("timecapsulesmb.transport.ssh._ssh_option_supported", return_value=True):
            with mock.patch("timecapsulesmb.transport.ssh.run_process", side_effect=self.authenticated()) as run:
                ssh_transport.run_ssh(
                    ssh_transport.SshConnection(
                        "root@192.168.1.67",
                        "pw",
                        "-i /home/tc/.ssh/id_tc -o HostKeyAlgorithms=+ssh-rsa",
                    ),
                    "/bin/echo ok",
                    check=False,
                    timeout=10,
                )
        cmd = run.call_args.args[0]
        # Explicit key configuration keeps the caller's existing OpenSSH
        # behavior, including agent fallback.
        self.assertNotIn("IdentitiesOnly=yes", cmd)
        self.assertNotIn("PubkeyAuthentication=no", cmd)
        self.assertNotIn("PreferredAuthentications=password", cmd)
        self.assertIn("-i", cmd)
        self.assertIn("/home/tc/.ssh/id_tc", cmd)

    def test_connection_ssh_args_preserve_explicit_key_authentication_intent(self) -> None:
        for opts in (
            "-i ~/.ssh/id_tc",
            "-i~/.ssh/id_tc",
            "-i none -i ~/.ssh/id_tc",
            "-I /usr/local/lib/pkcs11.so",
            "-I/usr/local/lib/pkcs11.so",
            "-o IdentityFile=/home/tc/id_tc",
            "-oIdentityFile=/home/tc/id_tc",
            "-o 'IdentityFile /home/tc/id_tc'",
            "-o IdentityAgent=/tmp/ssh-agent.sock",
            "-o CertificateFile=/home/tc/id_tc-cert.pub",
            "-o PKCS11Provider=/usr/local/lib/pkcs11.so",
            "-o SecurityKeyProvider=internal",
            "-o PubkeyAuthentication=yes",
            "-o 'PubkeyAuthentication yes'",
            "-o PubkeyAuthentication=unbound",
            "-o PubkeyAuthentication=host-bound",
            "-o PreferredAuthentications=publickey,password",
            "-o 'PreferredAuthentications publickey,password'",
            "-o 'PreferredAuthentications password, publickey'",
            "-o BatchMode=yes",
        ):
            with self.subTest(opts=opts):
                with mock.patch("timecapsulesmb.transport.ssh._ssh_option_supported", return_value=True):
                    args = ssh_transport._connection_ssh_args(
                        ssh_transport.SshConnection("root@192.168.1.118", "pw", opts),
                        client_log=Path("/tmp/client.log"),
                        stdin_null=True,
                    )
                self.assertEqual(args[:2], ["-F", "/dev/null"])
                self.assertNotIn("IdentitiesOnly=yes", args)
                self.assertNotIn("PubkeyAuthentication=no", args)
                self.assertNotIn("PreferredAuthentications=password", args)

    def test_connection_ssh_args_force_password_without_explicit_key_intent(self) -> None:
        for opts in (
            "-o HostKeyAlgorithms=+ssh-rsa -o PubkeyAcceptedAlgorithms=+ssh-rsa",
            "-o IdentityFile=none",
            "-o IdentityAgent=none",
            "-I none",
            "-o PubkeyAuthentication=no",
            "-o PreferredAuthentications=keyboard-interactive,password",
            "-o BatchMode=no",
        ):
            with self.subTest(opts=opts):
                with mock.patch("timecapsulesmb.transport.ssh._ssh_option_supported", return_value=True):
                    args = ssh_transport._connection_ssh_args(
                        ssh_transport.SshConnection("root@192.168.1.118", "pw", opts),
                        client_log=Path("/tmp/client.log"),
                        stdin_null=True,
                    )
                self.assertEqual(args[:2], ["-F", "/dev/null"])
                self.assertIn("PubkeyAuthentication=no", args)
                self.assertNotIn("PreferredAuthentications=password", args)
                self.assertNotIn("IdentitiesOnly=yes", args)

    def test_connection_ssh_args_use_batch_mode_without_password(self) -> None:
        with mock.patch("timecapsulesmb.transport.ssh._ssh_option_supported", return_value=True):
            args = ssh_transport._connection_ssh_args(
                ssh_transport.SshConnection("root@192.168.1.118", "", "-o HostKeyAlgorithms=+ssh-rsa"),
                client_log=Path("/tmp/client.log"),
                stdin_null=True,
            )

        self.assertEqual(args[:2], ["-F", "/dev/null"])
        self.assertIn("BatchMode=yes", args)
        self.assertNotIn("PubkeyAuthentication=no", args)

    def test_connection_ssh_args_ignore_identity_inside_proxycommand(self) -> None:
        opts = "-o 'ProxyCommand=ssh -i ~/.ssh/jump -W %h:%p jump.example'"
        with mock.patch("timecapsulesmb.transport.ssh._ssh_option_supported", return_value=True):
            args = ssh_transport._connection_ssh_args(
                ssh_transport.SshConnection("root@192.168.1.118", "pw", opts),
                client_log=Path("/tmp/client.log"),
                stdin_null=True,
            )

        self.assertIn("PubkeyAuthentication=no", args)

    def test_connection_ssh_args_enforce_transport_owned_session_options(self) -> None:
        opts = (
            "-q -vv -t -A -X -E /tmp/user.log -F /tmp/user.conf -S /tmp/master "
            "-o LogLevel=DEBUG -o NumberOfPasswordPrompts=9 -o ExitOnForwardFailure=no "
            "-o RequestTTY=force -o StdinNull=no -o ForwardAgent=yes -o ForwardX11=yes "
            "-o ControlPath=/tmp/other -J jump.example"
        )
        with mock.patch("timecapsulesmb.transport.ssh._ssh_option_supported", return_value=True):
            args = ssh_transport._connection_ssh_args(
                ssh_transport.SshConnection("device", "pw", opts),
                client_log=Path("/tmp/internal.log"),
                stdin_null=True,
            )

        self.assertEqual(args[0:2], ["-F", "/dev/null"])
        self.assertIn("LogLevel=VERBOSE", args)
        self.assertIn("NumberOfPasswordPrompts=1", args)
        self.assertIn("ExitOnForwardFailure=yes", args)
        self.assertIn("-J", args)
        self.assertIn("jump.example", args)
        self.assertEqual(args[args.index("-E") + 1], "/tmp/internal.log")
        self.assertEqual(args[args.index("-S") + 1], "none")
        for flag in ("-T", "-n", "-a", "-x"):
            self.assertIn(flag, args)
        self.assertNotIn("/tmp/user.log", args)
        self.assertNotIn("/tmp/user.conf", args)
        self.assertNotIn("/tmp/master", args)
        self.assertNotIn("LogLevel=DEBUG", args)
        self.assertNotIn("ControlPath=/tmp/other", args)

    def test_connection_ssh_args_leave_stdin_open_for_piped_requests(self) -> None:
        with mock.patch("timecapsulesmb.transport.ssh._ssh_option_supported", return_value=True):
            args = ssh_transport._connection_ssh_args(
                ssh_transport.SshConnection("device", "pw", "-n -o StdinNull=yes"),
                client_log=Path("/tmp/internal.log"),
                stdin_null=False,
            )
        self.assertNotIn("-n", args)
        self.assertNotIn("StdinNull=yes", args)

    def test_client_log_path_is_unique_and_removed(self) -> None:
        with ssh_transport._ssh_client_log_path() as first:
            first.parent.mkdir(parents=True, exist_ok=True)
            first.write_text("first")
            first_parent = first.parent
        with ssh_transport._ssh_client_log_path() as second:
            second_parent = second.parent
            self.assertNotEqual(first, second)
            self.assertFalse(second.exists())
        self.assertFalse(first_parent.exists())
        self.assertFalse(second_parent.exists())

    def test_authenticated_client_log_ignores_earlier_password_rejection(self) -> None:
        diagnostics = ssh_transport.parse_ssh_client_diagnostics(
            "Permission denied, please try again.\n"
            'Authenticated to device ([192.0.2.1]:22) using "password".\n'
        )
        self.assertTrue(diagnostics.authenticated)
        self.assertIsNone(diagnostics.error)

    def test_debug_command_text_cannot_be_classified_as_auth_failure(self) -> None:
        diagnostics = ssh_transport.parse_ssh_client_diagnostics(
            "debug1: Sending command: echo Permission denied, please try again.\n"
        )
        self.assertIsNone(diagnostics.error)

    def test_ssh_option_supported_returns_false_for_bad_configuration_option(self) -> None:
        with mock.patch(
            "timecapsulesmb.transport.ssh.run_process",
            return_value=subprocess.CompletedProcess(
                ["ssh"],
                255,
                stdout="",
                stderr="command-line: line 0: Bad configuration option: pubkeyacceptedalgorithms\n",
            ),
        ):
            self.assertFalse(ssh_transport._ssh_option_supported("PubkeyAcceptedAlgorithms"))

    def test_ssh_option_supported_returns_false_when_ssh_binary_is_missing(self) -> None:
        with mock.patch("timecapsulesmb.transport.ssh.run_process", side_effect=FileNotFoundError("missing ssh")):
            self.assertFalse(ssh_transport._ssh_option_supported("PubkeyAcceptedAlgorithms"))

    def upload(self, stdout: bytes, content: bytes = b"hello") -> tuple[mock.Mock, Path]:
        process = subprocess.CompletedProcess(["ssh"], 0, stdout, b"5+0 records in\n")
        with NamedTemporaryFile() as tmp:
            src = Path(tmp.name)
            src.write_bytes(content)
            with mock.patch("timecapsulesmb.transport.ssh._run_ssh", return_value=process) as run:
                ssh_transport.upload_file(
                    ssh_transport.SshConnection("device", "pw", ""),
                    src,
                    "/Volumes/dk2/.samba4/smbd",
                    timeout=180,
                )
        return run, src

    def test_upload_file_writes_large_blocks_and_checks_size_in_one_command(self) -> None:
        run, _src = self.upload(b"5\n")
        run.assert_called_once()
        self.assertEqual(run.call_args.kwargs["input_bytes"], b"hello")
        self.assertEqual(run.call_args.kwargs["timeout"], 180)
        script = shlex.split(run.call_args.args[1])[2]
        self.assertTrue(script.startswith("dd of=/Volumes/dk2/.samba4/smbd ibs=65536 obs=1048576 && "))
        self.assertIn("ls -l /Volumes/dk2/.samba4/smbd", script)

    def test_upload_file_rejects_a_short_file(self) -> None:
        with self.assertRaises(ssh_transport.SshError) as exc:
            self.upload(b"3\n")
        self.assertRegex(
            str(exc.exception),
            r"^upload verification failed for \S+ -> /Volumes/dk2/.samba4/smbd: expected 5 bytes, got 3 bytes$",
        )

    def test_upload_file_without_a_size_reports_unknown(self) -> None:
        with self.assertRaisesRegex(ssh_transport.SshError, "expected 5 bytes, got unknown bytes"):
            self.upload(b"")

    def test_upload_command_writes_the_file_and_prints_its_size_in_a_shell(self) -> None:
        with TemporaryDirectory() as tmp:
            src = Path(tmp) / "src"
            src.write_bytes(bytes(range(256)) * 9000)
            dest = Path(tmp) / "dest dir" / "smbd"
            dest.parent.mkdir()

            def run(_connection, remote_cmd, *, input_bytes, **_kwargs):
                return subprocess.run(remote_cmd, shell=True, input=input_bytes, capture_output=True)

            with mock.patch("timecapsulesmb.transport.ssh._run_ssh", side_effect=run):
                ssh_transport.upload_file(ssh_transport.SshConnection("device", "pw", ""), src, str(dest))
            self.assertEqual(dest.read_bytes(), src.read_bytes())

    def test_upload_file_reports_remote_failure(self) -> None:
        process = subprocess.CompletedProcess(["ssh"], 1, b"", b"disk full\n")
        with NamedTemporaryFile() as tmp:
            src = Path(tmp.name)
            src.write_bytes(b"hello")
            with mock.patch("timecapsulesmb.transport.ssh._run_ssh", return_value=process):
                with self.assertRaisesRegex(ssh_transport.SshError, "disk full"):
                    ssh_transport.upload_file(
                        ssh_transport.SshConnection("device", "pw", ""),
                        src,
                        "/tmp/test-upload",
                    )

    def test_run_ssh_capture_bytes_returns_binary_stdout(self) -> None:
        payload = b"\x00firmware\xff\n"
        process = subprocess.CompletedProcess(["ssh"], 0, payload, b"")
        connection = ssh_transport.SshConnection("device", "pw", "")
        with mock.patch("timecapsulesmb.transport.ssh.run_process", side_effect=self.completed_run(process)) as run:
            self.assertEqual(ssh_transport.run_ssh_capture_bytes(connection, "/bin/dd if=/dev/rflash0.raw"), payload)
        command = run.call_args.args[0]
        self.assertEqual(command[0], "ssh")
        self.assertIn("-n", command)
        self.assertIn("-T", command)
        self.assertIn("ControlMaster=auto", command)
        # Binary stdout stays apart from ssh's own and the remote stderr.
        self.assertEqual(run.call_args.kwargs["stderr"], subprocess.PIPE)
        self.assertEqual(run.call_args.kwargs["stdin"], subprocess.DEVNULL)

    def test_run_ssh_capture_bytes_without_password_uses_plain_ssh(self) -> None:
        payload = b"bank"
        process = subprocess.CompletedProcess(["ssh"], 0, payload, b"")
        connection = ssh_transport.SshConnection("device", "", "")
        with mock.patch("timecapsulesmb.transport.ssh.run_process", side_effect=self.completed_run(process)) as run:
            self.assertEqual(ssh_transport.run_ssh_capture_bytes(connection, "dd"), payload)
        command = run.call_args.args[0]
        self.assertEqual(command[0], "ssh")
        self.assertIn("BatchMode=yes", command)
        # A key login needs no password helper: ssh inherits our environment.
        self.assertIsNone(run.call_args.kwargs["env"])

    def test_run_ssh_capture_bytes_does_not_decode_successful_binary_stdout(self) -> None:
        payload = DecodeTrapBytes(b"\x00firmware\xff" * 4096)
        process = subprocess.CompletedProcess(["ssh"], 0, payload, b"")
        with mock.patch("timecapsulesmb.transport.ssh.run_process", side_effect=self.completed_run(process)):
            result = ssh_transport.run_ssh_capture_bytes(
                ssh_transport.SshConnection("device", "pw", ""),
                "dd",
            )
        self.assertEqual(result, payload)

    def test_run_ssh_capture_bytes_does_not_decode_failed_binary_stdout(self) -> None:
        payload = DecodeTrapBytes(b"x" * (ssh_transport.SSH_ERROR_STDOUT_PREFIX_BYTES + 100))
        process = subprocess.CompletedProcess(["ssh"], 1, payload, b"")
        with mock.patch("timecapsulesmb.transport.ssh.run_process", side_effect=self.completed_run(process)):
            with self.assertRaisesRegex(ssh_transport.SshError, "ssh command failed with rc=1"):
                ssh_transport.run_ssh_capture_bytes(
                    ssh_transport.SshConnection("device", "pw", ""),
                    "dd",
                )

    def test_piped_ssh_retries_auth_failure_from_client_log(self) -> None:
        run = self.ssh_runs(
            (255, b"", "root@device: Permission denied (password).\n"),
            (0, b"ok", AUTHENTICATED_LOG),
        )
        with mock.patch("timecapsulesmb.transport.ssh.run_process", side_effect=run) as run_mock, \
             mock.patch("timecapsulesmb.transport.ssh.time") as time_mock:
            result = ssh_transport.run_ssh_capture_bytes(
                ssh_transport.SshConnection("device", "pw", ""),
                "dd",
            )
        self.assertEqual(result, b"ok")
        self.assertEqual(sum("-E" in call.args[0] for call in run_mock.call_args_list), 2)
        time_mock.sleep.assert_called_once_with(1)

    def test_remote_permission_denied_is_not_classified_or_retried(self) -> None:
        process = subprocess.CompletedProcess(["ssh"], 1, b"", b"cat: protected: Permission denied\n")
        with mock.patch("timecapsulesmb.transport.ssh.run_process", side_effect=self.completed_run(process)) as run:
            with self.assertRaisesRegex(ssh_transport.SshError, "Permission denied"):
                ssh_transport.run_ssh_capture_bytes(
                    ssh_transport.SshConnection("device", "pw", ""),
                    "cat protected",
                )
        self.assertEqual(sum("-E" in call.args[0] for call in run.call_args_list), 1)

    def test_passwordless_auth_rejection_uses_client_log(self) -> None:
        process = subprocess.CompletedProcess(["ssh"], 255, b"", b"")
        diagnostics = "root@device: Permission denied (publickey).\n"
        with mock.patch("timecapsulesmb.transport.ssh.run_process", side_effect=self.completed_run(process, diagnostics)) as run:
            with self.assertRaises(ssh_transport.SshAuthenticationError):
                ssh_transport.run_ssh_capture_bytes(
                    ssh_transport.SshConnection("device", "", ""),
                    "dd",
                )
        self.assertEqual(sum("-E" in call.args[0] for call in run.call_args_list), 1)

    def test_piped_ssh_sends_client_hosts_line_before_the_command(self) -> None:
        process = subprocess.CompletedProcess(["ssh"], 0, b"done\n", b"")
        with mock.patch("timecapsulesmb.transport.ssh.run_process", side_effect=self.completed_run(process)) as run:
            proc = ssh_transport.run_ssh_input(
                ssh_transport.SshConnection("root@device", "pw", ""),
                "/bin/sh -c 'cat > /tmp/x'",
                input_bytes=b"payload",
            )
        command = run.call_args.args[0]
        self.assertEqual(command[-2:], ["root@device", ssh_transport.CLIENT_HOSTS_LINE_COMMAND + "/bin/sh -c 'cat > /tmp/x'"])
        self.assertEqual(run.call_args.kwargs["input"], b"payload")
        self.assertNotIn("stdin", run.call_args.kwargs)
        self.assertNotIn("-n", command)
        self.assertEqual(proc.stdout, b"done\n")

    def test_piped_ssh_timeout_names_only_the_callers_command(self) -> None:
        def run(command, **_kwargs):
            if "-E" not in command:  # a local ssh option probe
                return subprocess.CompletedProcess(command, 0, "", "")
            raise subprocess.TimeoutExpired(command, 5)

        with mock.patch("timecapsulesmb.transport.ssh.run_process", side_effect=run):
            with self.assertRaises(ssh_transport.SshCommandTimeout) as exc:
                ssh_transport.run_ssh_input(ssh_transport.SshConnection("root@device", "pw", ""), "helper --flag", timeout=5)
        self.assertEqual(str(exc.exception), "Timed out waiting for ssh command to finish: helper --flag")

    def test_run_ssh_failure_reports_the_device_output_not_the_prefix(self) -> None:
        with mock.patch("timecapsulesmb.transport.ssh._ssh_option_supported", return_value=True), \
             mock.patch("timecapsulesmb.transport.ssh.run_process",
                        side_effect=self.authenticated(1, "no such file\n")) as run:
            with self.assertRaises(ssh_transport.SshError) as exc:
                ssh_transport.run_ssh(ssh_transport.SshConnection("root@device", "pw", ""), "ls /missing", timeout=10)
        self.assertEqual(run.call_args.args[0][-1], ssh_transport.CLIENT_HOSTS_LINE_COMMAND + "ls /missing")
        self.assertEqual(str(exc.exception), "no such file")

    def test_run_ssh_joins_stderr_and_gives_ssh_the_password_through_the_helper(self) -> None:
        with mock.patch("timecapsulesmb.transport.ssh._ssh_option_supported", return_value=True), \
             mock.patch("timecapsulesmb.transport.ssh.run_process", side_effect=self.authenticated()) as run:
            ssh_transport.run_ssh(ssh_transport.SshConnection("root@device", "secret", ""), "true", timeout=10)
        kwargs = run.call_args.kwargs
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stderr"], subprocess.STDOUT)
        env = kwargs["env"]
        self.assertEqual(env["SSH_ASKPASS"], str(ssh_transport.SSH_ASKPASS_PATH))
        self.assertEqual(env["SSH_ASKPASS_REQUIRE"], "force")
        self.assertEqual(env[ssh_transport.SSH_PASSWORD_ENV], "secret")
        # Only the child's copy holds the password.
        self.assertNotIn(ssh_transport.SSH_PASSWORD_ENV, os.environ)

    def test_a_crashed_ssh_is_reported_as_a_local_failure_and_not_retried(self) -> None:
        def crashed(command, **_kwargs):
            Path(command[command.index("-E") + 1]).write_text("")
            return subprocess.CompletedProcess(command, -11, b"", b"")

        with mock.patch("timecapsulesmb.transport.ssh._ssh_option_supported", return_value=True), \
             mock.patch("timecapsulesmb.transport.ssh.run_process", side_effect=crashed) as run, \
             mock.patch("timecapsulesmb.transport.ssh.time") as time_mock:
            with self.assertRaises(ssh_transport.SshClientCrashedError) as raised:
                ssh_transport.run_ssh(ssh_transport.SshConnection("root@device", "pw", ""), "true", timeout=10)
        run.assert_called_once()
        time_mock.sleep.assert_not_called()
        self.assertEqual(raised.exception.signal_number, 11)
        self.assertIn("crashed (SIGSEGV)", str(raised.exception))
        self.assertIn("VPN, firewall or security app", str(raised.exception))
        self.assertNotIn("rc=", str(raised.exception))
        # It is guided like a connection this Mac dropped: same apps, same code.
        self.assertIsInstance(raised.exception, transport_errors.SshLocalNetworkFilteredError)

    def test_an_ssh_killed_from_outside_is_reported_as_stopped_not_crashed(self) -> None:
        for number, name in ((signal.SIGKILL, "SIGKILL"), (signal.SIGTERM, "SIGTERM"), (99, "signal 99")):
            with self.subTest(name=name):
                error = ssh_transport.SshClientCrashedError(number, platform="darwin")
                self.assertEqual(str(error), f"The ssh program on this Mac was stopped by {name} before it finished.")
                self.assertEqual(error.signal_number, number)
        self.assertIn("this computer crashed (SIGBUS)", str(ssh_transport.SshClientCrashedError(signal.SIGBUS, platform="linux")))

    def test_a_password_helper_that_cannot_run_is_a_damaged_install_not_a_wrong_password(self) -> None:
        log = (
            f"ssh_askpass: exec({ssh_transport.SSH_ASKPASS_PATH}): No such file or directory\n"
            "root@device: Permission denied (publickey,password,keyboard-interactive).\n"
        )
        with mock.patch("timecapsulesmb.transport.ssh._ssh_option_supported", return_value=True), \
             mock.patch("timecapsulesmb.transport.ssh.run_process", side_effect=self.ssh_runs((255, "", log))) as run, \
             mock.patch("timecapsulesmb.transport.ssh.time") as time_mock:
            with self.assertRaises(ssh_transport.SshClientConfigError) as raised:
                ssh_transport.run_ssh(ssh_transport.SshConnection("root@device", "pw", ""), "true", timeout=10)
        run.assert_called_once()
        time_mock.sleep.assert_not_called()
        self.assertIn("reinstall TimeCapsuleSMB", str(raised.exception))


MUX_SESSION_LOG = (
    "debug1: auto-mux: Trying existing master at '/tmp/tcsmb-ssh-x/0123456789abcdef'\n"
    "debug1: mux_client_request_session: master session id: 2\n"
)


class SharedConnectionTests(unittest.TestCase):
    """Commands share one authenticated connection per device and process."""

    def setUp(self) -> None:
        directory = TemporaryDirectory(prefix="tcsmb-test-", dir="/tmp")
        self.addCleanup(directory.cleanup)
        self.control_dir = directory.name
        for name, value in (("_control_dir", self.control_dir), ("_control_hosts", {})):
            patcher = mock.patch.object(ssh_transport, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch("timecapsulesmb.transport.ssh._ssh_option_supported", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch("timecapsulesmb.transport.ssh._local_ssh_macs", return_value=())
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def option(args: list[str], name: str) -> str | None:
        values = [arg.split("=", 1)[1] for arg in args if arg.startswith(f"{name}=")]
        return values[0] if values else None

    def shared_args(self, connection: ssh_transport.SshConnection) -> list[str]:
        return ssh_transport._connection_ssh_args(
            connection, client_log=Path("/tmp/client.log"), stdin_null=True, shared=True,
        )

    def test_shared_args_reuse_a_private_master_and_mark_shared_sessions(self) -> None:
        args = self.shared_args(ssh_transport.SshConnection("root@192.168.1.118", "pw", ""))

        self.assertEqual(self.option(args, "ControlMaster"), "auto")
        self.assertEqual(self.option(args, "ControlPersist"), "180")
        path = self.option(args, "ControlPath")
        self.assertEqual(os.path.dirname(path), self.control_dir)
        # The DEBUG1 log carries the shared-session marker.
        self.assertEqual(self.option(args, "LogLevel"), "DEBUG1")
        self.assertEqual(self.option(args, "ServerAliveInterval"), "15")
        self.assertEqual(self.option(args, "ServerAliveCountMax"), "3")
        self.assertNotIn("-S", args)

    def test_unshared_args_keep_their_own_connection(self) -> None:
        args = ssh_transport._connection_ssh_args(
            ssh_transport.SshConnection("root@192.168.1.118", "pw", ""),
            client_log=Path("/tmp/client.log"),
            stdin_null=True,
        )
        self.assertEqual(args[args.index("-S") + 1], "none")
        self.assertEqual(self.option(args, "LogLevel"), "VERBOSE")
        self.assertIsNone(self.option(args, "ControlMaster"))
        self.assertIsNone(self.option(args, "ServerAliveInterval"))
        self.assertEqual(ssh_transport._control_hosts, {})

    def test_callers_cannot_take_over_the_master_settings(self) -> None:
        args = self.shared_args(ssh_transport.SshConnection(
            "device", "pw",
            "-S /tmp/theirs -o ControlMaster=yes -o ControlPersist=yes -oControlPath=/tmp/other "
            "-o ServerAliveInterval=30",
        ))

        self.assertEqual([a for a in args if a.startswith("ControlMaster=")], ["ControlMaster=auto"])
        self.assertEqual([a for a in args if a.startswith("ControlPersist=")], ["ControlPersist=180"])
        self.assertEqual(os.path.dirname(self.option(args, "ControlPath")), self.control_dir)
        self.assertNotIn("/tmp/theirs", args)
        self.assertNotIn("-oControlPath=/tmp/other", args)
        # ssh keeps an option's first value: the caller's own keepalive wins.
        self.assertEqual(self.option(args, "ServerAliveInterval"), "30")

    def test_one_master_per_host_password_and_options(self) -> None:
        def path(host: str, password: str, opts: str) -> str:
            return self.option(self.shared_args(ssh_transport.SshConnection(host, password, opts)), "ControlPath")

        base = path("root@10.0.0.2", "pw", "-o HostKeyAlgorithms=+ssh-rsa")
        self.assertEqual(base, path("root@10.0.0.2", "pw", "-o HostKeyAlgorithms=+ssh-rsa"))
        # A different password must log in itself, never ride on this master.
        self.assertNotEqual(base, path("root@10.0.0.2", "other", "-o HostKeyAlgorithms=+ssh-rsa"))
        self.assertNotEqual(base, path("root@10.0.0.3", "pw", "-o HostKeyAlgorithms=+ssh-rsa"))
        self.assertNotEqual(base, path("root@10.0.0.2", "pw", "-p 2222"))
        self.assertNotIn("pw", base)

    def test_first_shared_command_creates_a_short_private_directory(self) -> None:
        with mock.patch.object(ssh_transport, "_control_dir", None), \
             mock.patch("timecapsulesmb.transport.ssh.atexit.register") as register:
            first = self.option(self.shared_args(ssh_transport.SshConnection("root@10.0.0.2", "pw", "")), "ControlPath")
            second = self.option(self.shared_args(ssh_transport.SshConnection("root@10.0.0.3", "pw", "")), "ControlPath")
            directory = ssh_transport._control_dir
        self.addCleanup(lambda: os.path.isdir(directory) and os.rmdir(directory))

        self.assertEqual(os.path.dirname(first), directory)
        self.assertEqual(os.path.dirname(second), directory)
        self.assertTrue(os.path.basename(directory).startswith(ssh_transport.SSH_CONTROL_DIR_PREFIX))
        self.assertEqual(os.stat(directory).st_mode & 0o777, 0o700)
        # ssh binds a temporary name 17 bytes longer; macOS allows 104 bytes.
        self.assertLess(len(first) + 17, 104)
        register.assert_called_once_with(ssh_transport.close_ssh_masters)

    def test_shared_session_counts_as_authenticated(self) -> None:
        diagnostics = ssh_transport.parse_ssh_client_diagnostics(MUX_SESSION_LOG)
        self.assertTrue(diagnostics.authenticated)
        self.assertIsNone(diagnostics.error)

    def test_marker_outside_a_debug_line_is_not_a_shared_session(self) -> None:
        diagnostics = ssh_transport.parse_ssh_client_diagnostics(
            "mux_client_request_session: master session id: 2\n"
        )
        self.assertFalse(diagnostics.authenticated)

    def test_shared_session_failure_after_falling_back_still_reports_the_login_error(self) -> None:
        # The master was gone, so ssh logged in itself and was refused.
        diagnostics = ssh_transport.parse_ssh_client_diagnostics(
            "debug1: auto-mux: Trying existing master at '/tmp/tcsmb-ssh-x/0123456789abcdef'\n"
            "debug1: Control socket \"/tmp/tcsmb-ssh-x/0123456789abcdef\" does not exist\n"
            "root@device: Permission denied (password).\n"
        )
        self.assertFalse(diagnostics.authenticated)
        self.assertIsInstance(diagnostics.error, ssh_transport.SshAuthenticationError)

    def test_remote_exit_255_over_a_shared_session_is_the_commands_status(self) -> None:
        run = SSHTransportTests.ssh_runs((255, "remote said no\n", MUX_SESSION_LOG))
        with mock.patch("timecapsulesmb.transport.ssh.run_process", side_effect=run) as ran:
            proc = ssh_transport.run_ssh(
                ssh_transport.SshConnection("root@device", "pw", ""), "exit 255", check=False,
            )
        self.assertEqual(proc.returncode, 255)
        self.assertEqual(proc.stdout, "remote said no\n")
        self.assertEqual(ran.call_count, 1)

    def test_rc_255_with_neither_login_nor_shared_session_is_a_client_failure(self) -> None:
        run = SSHTransportTests.ssh_runs(
            (255, "", "debug1: auto-mux: Trying existing master\nConnection closed by 10.0.0.2 port 22\n"),
        )
        with mock.patch("timecapsulesmb.transport.ssh.run_process", side_effect=run):
            with self.assertRaises(ssh_transport.SshError):
                ssh_transport.run_ssh(ssh_transport.SshConnection("root@device", "pw", ""), "true", check=False)

    def test_piped_commands_use_the_shared_connection(self) -> None:
        # The empty source file arrives as an empty file on the device.
        process = subprocess.CompletedProcess(["ssh"], 0, b"0\n", b"")
        with mock.patch("timecapsulesmb.transport.ssh.run_process",
                        side_effect=SSHTransportTests.completed_run(process)) as run:
            with NamedTemporaryFile() as source:
                ssh_transport.upload_file(ssh_transport.SshConnection("root@device", "pw", ""), Path(source.name), "/tmp/x")
        command = run.call_args.args[0]
        self.assertEqual(self.option(command, "ControlMaster"), "auto")
        self.assertEqual(os.path.dirname(self.option(command, "ControlPath")), self.control_dir)

    def test_every_command_offers_ssh_the_shared_connection_and_the_password(self) -> None:
        # ssh uses a live master, or logs in and becomes it: one invocation
        # either way, so a command never runs twice.
        run = SSHTransportTests.ssh_runs((3, "out\nerr\n", MUX_SESSION_LOG), (0, "made\n", AUTHENTICATED_LOG))
        connection = ssh_transport.SshConnection("root@device", "pw", "")
        with mock.patch("timecapsulesmb.transport.ssh.run_process", side_effect=run) as ran:
            over_master = ssh_transport.run_ssh(connection, "probe", check=False, timeout=7)
            logged_in = ssh_transport.run_ssh(connection, "mkdir /x")

        self.assertEqual((over_master.returncode, over_master.stdout), (3, "out\nerr\n"))
        self.assertEqual(logged_in.stdout, "made\n")
        self.assertEqual(ran.call_count, 2)
        for call in ran.call_args_list:
            command = call.args[0]
            self.assertEqual(self.option(command, "ControlMaster"), "auto")
            self.assertNotIn("BatchMode=yes", command)
            self.assertEqual(call.kwargs["env"][ssh_transport.SSH_PASSWORD_ENV], "pw")
        self.assertEqual(ran.call_args_list[0].args[0][-1], ssh_transport.CLIENT_HOSTS_LINE_COMMAND + "probe")

    def test_shared_session_failure_raises_with_the_device_output(self) -> None:
        run = SSHTransportTests.ssh_runs((1, "no such file\n", MUX_SESSION_LOG))
        with mock.patch("timecapsulesmb.transport.ssh.run_process", side_effect=run):
            with self.assertRaisesRegex(ssh_transport.SshError, "^no such file$"):
                ssh_transport.run_ssh(ssh_transport.SshConnection("root@device", "pw", ""), "ls /missing")

    def test_a_command_that_hangs_times_out_naming_the_command(self) -> None:
        with mock.patch("timecapsulesmb.transport.ssh.run_process",
                        side_effect=subprocess.TimeoutExpired("ssh", 7)) as run:
            with self.assertRaises(ssh_transport.SshCommandTimeout) as raised:
                ssh_transport.run_ssh(ssh_transport.SshConnection("root@device", "pw", ""), "sleep 99", timeout=7)
        run.assert_called_once()
        self.assertEqual(run.call_args.kwargs["timeout"], 7)
        self.assertEqual(str(raised.exception), "Timed out waiting for ssh command to finish: sleep 99")

    def make_master(self, host: str, *, opts: str = "", live: bool = True) -> str:
        path = self.option(self.shared_args(ssh_transport.SshConnection(host, "pw", opts)), "ControlPath")
        if live:
            Path(path).touch()
        return path

    @staticmethod
    def exited(run: mock.Mock) -> list[str]:
        paths = []
        for call in run.call_args_list:
            command = call.args[0]
            assert command[-3:-1] == ["-O", "exit"], command
            paths.append(command[command.index("-o") + 1].removeprefix("ControlPath="))
        return paths

    def test_closing_a_device_exits_only_its_masters(self) -> None:
        device = self.make_master("root@10.0.0.2")
        other_login = self.option(
            self.shared_args(ssh_transport.SshConnection("admin@10.0.0.2", "pw2", "")), "ControlPath",
        )
        Path(other_login).touch()
        other_device = self.make_master("root@10.0.0.3")

        with mock.patch("timecapsulesmb.transport.ssh.run_process") as run:
            ssh_transport.close_ssh_masters("root@10.0.0.2")

        self.assertCountEqual(self.exited(run), [device, other_login])
        self.assertEqual(list(ssh_transport._control_hosts), [other_device])
        self.assertTrue(os.path.isdir(self.control_dir))

    def test_closing_skips_masters_that_already_ended(self) -> None:
        self.make_master("root@10.0.0.2", live=False)
        with mock.patch("timecapsulesmb.transport.ssh.run_process") as run:
            ssh_transport.close_ssh_masters("10.0.0.2")
        run.assert_not_called()
        self.assertEqual(ssh_transport._control_hosts, {})

    def test_closing_everything_exits_every_master_and_removes_the_directory(self) -> None:
        first = self.make_master("root@10.0.0.2")
        second = self.make_master("root@10.0.0.3")
        with mock.patch("timecapsulesmb.transport.ssh.run_process") as run:
            ssh_transport.close_ssh_masters()
        self.assertCountEqual(self.exited(run), [first, second])
        self.assertFalse(os.path.exists(self.control_dir))
        self.assertEqual(ssh_transport._control_hosts, {})

        # A later shared command gets a new directory, not the removed one.
        with mock.patch("timecapsulesmb.transport.ssh.atexit.register"):
            later = self.make_master("root@10.0.0.2", live=False)
        directory = os.path.dirname(later)
        self.addCleanup(lambda: os.path.isdir(directory) and os.rmdir(directory))
        self.assertNotEqual(directory, self.control_dir)
        self.assertTrue(os.path.isdir(directory))

    def test_a_master_that_will_not_exit_does_not_stop_the_others(self) -> None:
        first = self.make_master("root@10.0.0.2")
        second = self.make_master("root@10.0.0.3")
        with mock.patch(
            "timecapsulesmb.transport.ssh.run_process",
            side_effect=[subprocess.TimeoutExpired("ssh", 5), OSError("no ssh")],
        ) as run:
            ssh_transport.close_ssh_masters()
        self.assertCountEqual(self.exited(run), [first, second])
        for call in run.call_args_list:
            self.assertEqual(call.kwargs["timeout"], ssh_transport.SSH_CONTROL_EXIT_TIMEOUT_SECONDS)


class MigrationInputTransportTests(unittest.TestCase):
    # The local ssh capability checks are cached for the process. Unpatched,
    # they run through each test's patched run_process only when no earlier
    # test filled the cache, so call counts would depend on test order.
    def setUp(self) -> None:
        ssh_transport._ssh_option_supported.cache_clear()
        ssh_transport._local_ssh_macs.cache_clear()
        self.addCleanup(ssh_transport._local_ssh_macs.cache_clear)
        self.addCleanup(ssh_transport._ssh_option_supported.cache_clear)
        for name, value in (("_ssh_option_supported", True), ("_local_ssh_macs", ())):
            patcher = mock.patch.object(ssh_transport, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_request_bytes_and_separate_output_use_existing_transport(self):
        connection = ssh_transport.SshConnection("device", "", "")
        process = subprocess.CompletedProcess(["ssh"], 0, b'{"version":1}\n', b'diagnostic\n')
        with mock.patch.object(ssh_transport, "_run_ssh", return_value=process) as run:
            result = ssh_transport.run_ssh_input(
                connection,
                "helper multi copy",
                input_bytes=b"TCMIGRATE1\nE\n",
                timeout=900,
                raw_remote_status=True,
                extra_ssh_args=("-o", "ConnectTimeout=20"),
            )
        self.assertIs(result, process)
        self.assertEqual(run.call_args.kwargs["input_bytes"], b"TCMIGRATE1\nE\n")
        self.assertEqual(run.call_args.kwargs["timeout"], 900)
        self.assertEqual(run.call_args.kwargs["extra_ssh_args"], ("-o", "ConnectTimeout=20"))
        self.assertFalse(run.call_args.kwargs.get("merge_stderr", False))

    def test_raw_remote_status_is_single_attempt_and_keeps_native_stderr(self):
        connection = ssh_transport.SshConnection("device", "pw", "")
        process = subprocess.CompletedProcess(["ssh"], 4, b'{"version":1}', b"Permission denied in TDB")
        with mock.patch(
            "timecapsulesmb.transport.ssh.run_process",
            side_effect=SSHTransportTests.completed_run(process),
        ) as run:
            with mock.patch("timecapsulesmb.transport.ssh.time.sleep") as sleep:
                result = ssh_transport.run_ssh_input(
                    connection,
                    "helper multi copy",
                    raw_remote_status=True,
                    extra_ssh_args=("-o", "ConnectTimeout=20"),
                )
        self.assertIs(result, process)
        run.assert_called_once()
        sleep.assert_not_called()
        command = run.call_args.args[0]
        self.assertLess(command.index("ConnectTimeout=20"), command.index("device"))

    def test_raw_remote_status_does_not_retry_a_host_key_failure(self):
        # The command may or may not be safe to rerun; only a rejected login
        # (retried below) proves it never started.
        connection = ssh_transport.SshConnection("device", "pw", "")
        process = subprocess.CompletedProcess(["ssh"], 255, b"", b"")
        with mock.patch(
            "timecapsulesmb.transport.ssh.run_process",
            side_effect=SSHTransportTests.completed_run(process, "Host key verification failed.\n"),
        ) as run:
            with mock.patch("timecapsulesmb.transport.ssh.time.sleep") as sleep:
                with self.assertRaisesRegex(ssh_transport.SshError, "Host key verification failed"):
                    ssh_transport.run_ssh_input(connection, "helper multi copy", raw_remote_status=True)
        run.assert_called_once()
        sleep.assert_not_called()

    @staticmethod
    def login_sequence(outcomes):
        """Fake ssh runs: each outcome is ("rejected", None) for a refused
        password, or ("ran", CompletedProcess) for a login that reached the
        remote command. Records the stdin each run was given."""
        outcomes = iter(outcomes)
        inputs = []

        def run(command, **kwargs):
            inputs.append(kwargs.get("input"))
            kind, process = next(outcomes)
            log = Path(command[command.index("-E") + 1])
            if kind == "rejected":
                log.write_text("root@device: Permission denied (publickey,password,keyboard-interactive).\n")
                return subprocess.CompletedProcess(command, 255, b"", b"")
            log.write_text(AUTHENTICATED_LOG)
            return process

        return run, inputs

    def test_raw_remote_status_retries_a_rejected_login_with_the_same_input(self):
        # A rejected login never started the migrator, so the request can be
        # sent again; v3.1.x deploys failed their migration here instead.
        connection = ssh_transport.SshConnection("device", "pw", "")
        ran = subprocess.CompletedProcess(["ssh"], 0, b'{"version":1}', b"")
        run, inputs = self.login_sequence([("rejected", None), ("ran", ran)])
        with mock.patch("timecapsulesmb.transport.ssh.run_process", side_effect=run):
            with mock.patch("timecapsulesmb.transport.ssh.time.sleep") as sleep:
                result = ssh_transport.run_ssh_input(
                    connection,
                    "helper multi cleanup",
                    input_bytes=b"TCMIGRATE1\nE\n",
                    raw_remote_status=True,
                )
        self.assertIs(result, ran)
        self.assertEqual(inputs, [b"TCMIGRATE1\nE\n", b"TCMIGRATE1\nE\n"])
        sleep.assert_called_once_with(1)

    def test_raw_remote_status_gives_up_after_three_rejected_logins(self):
        connection = ssh_transport.SshConnection("device", "pw", "")
        run, inputs = self.login_sequence([("rejected", None)] * 3)
        with mock.patch("timecapsulesmb.transport.ssh.run_process", side_effect=run):
            with mock.patch("timecapsulesmb.transport.ssh.time.sleep") as sleep:
                with self.assertRaises(ssh_transport.SshAuthenticationError):
                    ssh_transport.run_ssh_input(connection, "helper multi copy", raw_remote_status=True)
        self.assertEqual(len(inputs), 3)
        self.assertEqual(sleep.call_count, 2)

    def test_raw_remote_status_never_reruns_a_command_that_started(self):
        # After a login the remote status is the migrator's own, even one that
        # mentions a denied permission; it is returned, never retried.
        connection = ssh_transport.SshConnection("device", "pw", "")
        ran = subprocess.CompletedProcess(["ssh"], 4, b"", b"lstat failed error=Permission denied\n")
        run, inputs = self.login_sequence([("ran", ran), ("ran", ran)])
        with mock.patch("timecapsulesmb.transport.ssh.run_process", side_effect=run):
            with mock.patch("timecapsulesmb.transport.ssh.time.sleep") as sleep:
                result = ssh_transport.run_ssh_input(connection, "helper multi copy", raw_remote_status=True)
        self.assertIs(result, ran)
        self.assertEqual(len(inputs), 1)
        sleep.assert_not_called()

    def test_explicit_unlimited_piped_timeout_preserves_ordinary_defaults(self):
        connection = ssh_transport.SshConnection("device", "", "")
        process = subprocess.CompletedProcess(["ssh"], 0, b"ok", b"")
        with mock.patch(
            "timecapsulesmb.transport.ssh.run_process",
            side_effect=SSHTransportTests.completed_run(process),
        ) as run:
            ssh_transport.run_ssh_input(connection, "helper", timeout=None)
        self.assertIsNone(run.call_args.kwargs["timeout"])
        with mock.patch(
            "timecapsulesmb.transport.ssh.run_process",
            side_effect=SSHTransportTests.completed_run(process),
        ) as run:
            ssh_transport.run_ssh_input(connection, "helper")
        self.assertEqual(run.call_args.kwargs["timeout"], 120)

    def test_failed_remote_helper_cannot_look_like_a_json_success(self):
        process = subprocess.CompletedProcess(["ssh"], 4, b'{"version":1}', b'corrupt TDB')
        with mock.patch.object(ssh_transport, "_run_ssh", return_value=process):
            with self.assertRaisesRegex(ssh_transport.SshError, "corrupt TDB"):
                ssh_transport.run_ssh_input(ssh_transport.SshConnection("device", "", ""), "helper")


class ClientHostsLineTests(unittest.TestCase):
    """Run the real prefix in /bin/sh against a temporary hosts file."""

    APPLE_HOSTS = (
        "#\t$NetBSD: hosts,v 1.8 2009/07/03 22:32:55 hubertf Exp $\n"
        "::1\t\t\tlocalhost localhost.\n"
        "127.0.0.1\t\tlocalhost localhost.\n"
        "127.0.0.1\tjamess-airport-time-capsule jamess-airport-time-capsule.local\n"
    )

    def setUp(self) -> None:
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.hosts = self.dir / "hosts"
        self.hosts.write_text(self.APPLE_HOSTS)

    def run_prefixed(
        self,
        client: str | None,
        command: str = "echo out; exit 3",
        *,
        hosts: Path | None = None,
        stdin: bytes = b"",
    ) -> subprocess.CompletedProcess[bytes]:
        env = {"PATH": "/usr/bin:/bin"}
        if client is not None:
            env["SSH_CLIENT"] = client
        prefix = ssh_transport.client_hosts_line_command(str(hosts or self.hosts))
        return subprocess.run(["/bin/sh", "-c", prefix + command], input=stdin, capture_output=True, env=env, timeout=10)

    def test_first_command_appends_a_line_named_after_the_address(self) -> None:
        proc = self.run_prefixed("192.168.1.170 50065 22")

        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (3, b"out\n", b""))
        self.assertEqual(self.hosts.read_text(), self.APPLE_HOSTS + "\n192.168.1.170 tcsmb-192-168-1-170\n")

    def test_later_commands_from_the_same_address_change_nothing(self) -> None:
        self.run_prefixed("192.168.1.170 50065 22")
        after_first = self.hosts.read_bytes()

        proc = self.run_prefixed("192.168.1.170 50070 22")

        self.assertEqual(proc.returncode, 3)
        self.assertEqual(self.hosts.read_bytes(), after_first)

    def test_each_client_appends_its_own_line(self) -> None:
        # .17 is a prefix of .170: an existing line for one must not satisfy the other.
        for client in ("192.168.1.170 1 22", "192.168.1.17 2 22", "10.0.1.5 3 22", "192.168.1.170 4 22"):
            self.run_prefixed(client)

        self.assertEqual(
            self.hosts.read_text(),
            self.APPLE_HOSTS
            + "\n192.168.1.170 tcsmb-192-168-1-170\n"
            + "\n192.168.1.17 tcsmb-192-168-1-17\n"
            + "\n10.0.1.5 tcsmb-10-0-1-5\n",
        )

    def test_global_ipv6_address_gets_a_line(self) -> None:
        self.run_prefixed("2600:1700:83b7:20f::5 50065 22")

        self.assertEqual(self.hosts.read_text(), self.APPLE_HOSTS + "\n2600:1700:83b7:20f::5 tcsmb-2600-1700-83b7-20f--5\n")

    def test_link_local_or_missing_address_changes_nothing(self) -> None:
        for client in ("fe80::1%bridge0 50065 22", "fe80::1 50065 22", "FE80::1 50065 22", "", None):
            with self.subTest(client=client):
                proc = self.run_prefixed(client)
                self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (3, b"out\n", b""))
                self.assertEqual(self.hosts.read_text(), self.APPLE_HOSTS)

    def test_last_line_without_a_newline_is_kept_intact(self) -> None:
        self.hosts.write_text(self.APPLE_HOSTS.rstrip("\n"))

        self.run_prefixed("192.168.1.170 50065 22")

        self.assertEqual(self.hosts.read_text(), self.APPLE_HOSTS + "192.168.1.170 tcsmb-192-168-1-170\n")

    def test_unwritable_hosts_file_is_silent_and_the_command_still_runs(self) -> None:
        proc = self.run_prefixed("192.168.1.170 50065 22", "echo out; exit 5", hosts=self.dir / "missing-dir" / "hosts")

        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (5, b"out\n", b""))

    def test_callers_stdin_and_binary_stdout_pass_through(self) -> None:
        payload = b"\x00binary\xff\npayload"
        target = self.dir / "uploaded"

        upload = self.run_prefixed("192.168.1.170 50065 22", f"cat > '{target}'", stdin=payload)
        echo = self.run_prefixed("192.168.1.170 50065 22", f"cat '{target}'")

        self.assertEqual(upload.returncode, 0)
        self.assertEqual(target.read_bytes(), payload)
        self.assertEqual(echo.stdout, payload)

    def test_callers_positional_parameters_are_untouched(self) -> None:
        proc = self.run_prefixed("192.168.1.170 50065 22", 'set -- a b; echo "$#:$1:${_tc-unset}"')

        self.assertEqual(proc.stdout, b"2:a:unset\n")

    def test_hosts_path_is_quoted_for_the_shell(self) -> None:
        hosts = self.dir / "dir with space" / "hosts"
        hosts.parent.mkdir()
        hosts.write_text(self.APPLE_HOSTS)

        self.run_prefixed("192.168.1.170 50065 22", "true", hosts=hosts)

        self.assertEqual(hosts.read_text(), self.APPLE_HOSTS + "\n192.168.1.170 tcsmb-192-168-1-170\n")


if __name__ == "__main__":
    unittest.main()


# A stand-in for ssh: it asks $SSH_ASKPASS as ssh does under
# SSH_ASKPASS_REQUIRE=force, writes ssh's log lines to -E, then runs the
# remote command in /bin/sh with the caller's stdin. A JSON scenario file
# sets the device password and what goes wrong; every run is recorded.
FAKE_SSH = r'''
import json, os, signal, socket, subprocess, sys, time
args = sys.argv[1:]
scenario = json.load(open(os.environ["FAKE_SSH_SCENARIO"]))
if "-E" not in args:  # a local option probe
    sys.exit(0)
runs = scenario["runs"]
with open(runs, "a") as record:
    record.write(json.dumps({
        "args": args,
        "pid": os.getpid(),
        "askpass": os.environ.get("SSH_ASKPASS"),
        "require": os.environ.get("SSH_ASKPASS_REQUIRE"),
    }) + "\n")
attempt = sum(1 for _ in open(runs))
log = args[args.index("-E") + 1]
def say(line):
    with open(log, "a") as out:
        out.write(line + "\n")
if scenario.get("crash"):
    os.kill(os.getpid(), signal.SIGSEGV)
if "BatchMode=yes" in args:
    reply, method = "", "publickey"
    authenticated = scenario.get("key_login", False)
else:
    askpass = os.environ["SSH_ASKPASS"]
    try:
        reply = subprocess.run([askpass, "root@device's password: "], capture_output=True, text=True).stdout
    except OSError as exc:
        say(f"ssh_askpass: exec({askpass}): {exc.strerror}")
        reply = ""
    method = "password"
    authenticated = reply == scenario["password"] + "\n" and attempt > scenario.get("rejections", 0)
if not authenticated:
    say("root@device: Permission denied (publickey,password,keyboard-interactive).")
    sys.exit(255)
say(f'Authenticated to device ([192.0.2.1]:22) using "{method}".')
if "-N" in args:
    port = int(args[args.index("-L") + 1].split(":")[0])
    tunnel = scenario.get("tunnel", "ready")
    if tunnel == "bind_error":
        say(f"bind [127.0.0.1]:{port}: Permission denied")
        sys.exit(255)
    if tunnel == "crash":
        os.kill(os.getpid(), signal.SIGSEGV)
    if tunnel == "ready":
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", port))
        listener.listen()
    time.sleep(60)
    sys.exit(0)
sys.exit(subprocess.run(["/bin/sh", "-c", args[-1]]).returncode)
'''


class RealSshProcessTests(unittest.TestCase):
    """The transport with a real ssh-like program, askpass helper and spawn."""

    def setUp(self) -> None:
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        fake = bin_dir / "ssh"
        fake.write_text(f"#!{sys.executable}\n{FAKE_SSH}")
        fake.chmod(0o755)
        self.runs = self.tmp / "runs.jsonl"
        self.scenario_path = self.tmp / "scenario.json"
        self.scenario()
        env = mock.patch.dict(os.environ, {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            "FAKE_SSH_SCENARIO": str(self.scenario_path),
        })
        env.start()
        self.addCleanup(env.stop)
        for name, value in (("_ssh_option_supported", True), ("_local_ssh_macs", ())):
            patcher = mock.patch.object(ssh_transport, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        # Skip the 1 s pause between login attempts, through the transport's
        # own time reference: time.sleep itself is also what subprocess's
        # wait(timeout=...) calls. The tunnel's short polls still sleep.
        transport_time = mock.Mock(wraps=time)
        transport_time.sleep = self.sleep = mock.Mock(side_effect=lambda seconds: seconds < 1 and time.sleep(seconds))
        patcher = mock.patch.object(ssh_transport, "time", transport_time)
        patcher.start()
        self.addCleanup(patcher.stop)

    def scenario(self, **settings: object) -> None:
        self.scenario_path.write_text(json.dumps({"runs": str(self.runs), "password": "pa ss$1", **settings}))

    def recorded(self) -> list[dict[str, object]]:
        if not self.runs.exists():
            return []
        return [json.loads(line) for line in self.runs.read_text().splitlines()]

    def test_password_login_answers_through_the_helper_and_returns_the_output(self) -> None:
        spawned = []
        real_spawn = os.posix_spawn

        def spy(path, *args, **kwargs):
            spawned.append(path)
            return real_spawn(path, *args, **kwargs)

        with mock.patch.object(os, "posix_spawn", spy):
            proc = ssh_transport.run_ssh(
                ssh_transport.SshConnection("root@device", "pa ss$1", ""), "echo out; echo err >&2; exit 3",
                check=False,
            )
        self.assertEqual(proc.returncode, 3)
        self.assertEqual(sorted(proc.stdout.splitlines()), ["err", "out"])
        [run] = self.recorded()
        self.assertEqual(run["askpass"], str(ssh_transport.SSH_ASKPASS_PATH))
        self.assertEqual(run["require"], "force")
        # ssh started through posix_spawn, never fork (issue #371).
        self.assertIn(str(self.tmp / "bin" / "ssh"), spawned)

    def test_request_bytes_reach_the_remote_command_with_separate_output(self) -> None:
        proc = ssh_transport.run_ssh_input(
            ssh_transport.SshConnection("root@device", "pa ss$1", ""),
            "tr a-z A-Z; echo note >&2",
            input_bytes=b"request\n",
        )
        self.assertEqual((proc.stdout, proc.stderr), (b"REQUEST\n", b"note\n"))

    def test_wrong_password_is_refused_three_times(self) -> None:
        with self.assertRaises(ssh_transport.SshAuthenticationError):
            ssh_transport.run_ssh(ssh_transport.SshConnection("root@device", "wrong", ""), "true")
        self.assertEqual(len(self.recorded()), 3)
        self.assertEqual(self.sleep.call_count, 2)

    def test_a_login_the_device_refused_once_succeeds_on_the_next_try(self) -> None:
        self.scenario(rejections=1)
        proc = ssh_transport.run_ssh(ssh_transport.SshConnection("root@device", "pa ss$1", ""), "echo ran")
        self.assertEqual(proc.stdout, "ran\n")
        self.assertEqual(len(self.recorded()), 2)

    def test_key_login_never_offers_the_password_helper(self) -> None:
        self.scenario(key_login=True)
        with mock.patch.dict(os.environ):
            os.environ.pop("SSH_ASKPASS", None)
            proc = ssh_transport.run_ssh(ssh_transport.SshConnection("root@device", "", ""), "echo key")
        self.assertEqual(proc.stdout, "key\n")
        [run] = self.recorded()
        self.assertIsNone(run["askpass"])
        self.assertIn("BatchMode=yes", run["args"])

    def test_a_crashed_ssh_raises_the_local_crash_error_once(self) -> None:
        self.scenario(crash=True)
        with self.assertRaises(ssh_transport.SshClientCrashedError) as raised:
            ssh_transport.run_ssh(ssh_transport.SshConnection("root@device", "pa ss$1", ""), "true")
        self.assertEqual(raised.exception.signal_number, signal.SIGSEGV)
        self.assertEqual(len(self.recorded()), 1)

    def test_a_missing_password_helper_is_reported_as_a_damaged_install(self) -> None:
        missing = self.tmp / "gone" / "ssh-askpass"
        with mock.patch.object(ssh_transport, "SSH_ASKPASS_PATH", missing):
            with self.assertRaisesRegex(ssh_transport.SshClientConfigError, "reinstall TimeCapsuleSMB"):
                ssh_transport.run_ssh(ssh_transport.SshConnection("root@device", "pa ss$1", ""), "true")
        self.assertEqual(len(self.recorded()), 1)

    def forward(self, *, ready_timeout: int = 20):
        return ssh_transport.ssh_local_forward(
            ssh_transport.SshConnection("root@fe80::1%en0", "pa ss$1", "-J jump.example"),
            local_port=self.free_port(),
            remote_host="127.0.0.1",
            remote_port=445,
            ready_timeout=ready_timeout,
        )

    @staticmethod
    def free_port() -> int:
        return find_free_local_port()

    def test_tunnel_is_ready_once_its_port_listens_and_its_ssh_ends_with_the_block(self) -> None:
        with self.forward():
            [run] = self.recorded()
            os.kill(run["pid"], 0)
        with self.assertRaises(ProcessLookupError):
            os.kill(run["pid"], 0)
        args = run["args"]
        self.assertEqual(args[args.index("-S") + 1], "none")
        self.assertFalse(any(arg.startswith("Control") for arg in args))
        self.assertEqual(args[args.index("-L") + 1].split(":", 1)[1], "127.0.0.1:445")
        self.assertEqual(args[-1], "root@fe80::1%en0")
        self.assertIn("jump.example", args)

    def test_tunnel_that_cannot_bind_reports_ssh_error(self) -> None:
        self.scenario(tunnel="bind_error")
        with self.assertRaisesRegex(ssh_transport.SshError, r"bind \[127.0.0.1\]:\d+: Permission denied"):
            with self.forward():
                pass

    def test_tunnel_whose_ssh_crashes_raises_the_local_crash_error(self) -> None:
        self.scenario(tunnel="crash")
        with self.assertRaises(ssh_transport.SshClientCrashedError):
            with self.forward():
                pass

    def test_tunnel_that_never_listens_times_out_naming_its_target(self) -> None:
        self.scenario(tunnel="silent")
        children = []
        real_popen = ssh_transport.popen_process

        def recording_popen(*args, **kwargs):
            children.append(real_popen(*args, **kwargs))
            return children[-1]

        with mock.patch.object(ssh_transport, "popen_process", recording_popen):
            with self.assertRaisesRegex(
                ssh_transport.SshError,
                r"^Timed out waiting for ssh tunnel to become ready: 127\.0\.0\.1:\d+ -> 127\.0\.0\.1:445 via root@fe80::1%en0$",
            ):
                with self.forward(ready_timeout=1):
                    pass
        # Its ssh was stopped and reaped, whether or not it had started yet.
        [child] = children
        self.assertIsNotNone(child.returncode)
