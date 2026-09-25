from __future__ import annotations

import subprocess
import sys
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


REAL_IMPORT = __import__
MISSING_PEXPECT_PREFIX = "Failed to load pexpect. Install the Python package pexpect."
MISSING_PEXPECT_ERROR = "ModuleNotFoundError: No module named 'pexpect'"


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

    def missing_pexpect_import(self, name: str, *args: object, **kwargs: object) -> object:
        if name == "pexpect":
            raise ModuleNotFoundError("No module named 'pexpect'")
        return REAL_IMPORT(name, *args, **kwargs)

    @staticmethod
    def authenticated_spawn(returncode: int = 0, output: str = "ok\n"):
        def spawn(_cmd, _password, *, client_log, timeout, timeout_message):
            Path(client_log).write_text('Authenticated to device ([192.0.2.1]:22) using "password".\n')
            return returncode, output

        return spawn

    @staticmethod
    def completed_run(process: subprocess.CompletedProcess[bytes], diagnostics: str | None = None):
        client_text = diagnostics if diagnostics is not None else 'Authenticated to device ([192.0.2.1]:22) using "password".\n'

        def run(command, **_kwargs):
            if "-E" not in command:
                return subprocess.CompletedProcess(command, 0, b"", b"")
            Path(command[command.index("-E") + 1]).write_text(client_text)
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
            with mock.patch(
                "timecapsulesmb.transport.ssh._spawn_with_password",
                side_effect=self.authenticated_spawn(),
            ) as spawn_mock:
                proc = ssh_transport.run_ssh(
                    ssh_transport.SshConnection("root@192.168.1.67", "pw", "-o PubkeyAcceptedAlgorithms=+ssh-rsa"),
                    "/bin/echo ok",
                    check=False,
                    timeout=10,
                )
        self.assertEqual(proc.returncode, 0)
        cmd = spawn_mock.call_args.args[0]
        self.assertIn("PubkeyAuthentication=no", cmd)
        self.assertIn("PubkeyAcceptedKeyTypes=+ssh-rsa", cmd)
        self.assertIn("NumberOfPasswordPrompts=1", cmd)
        self.assertEqual(cmd[-2:], ["root@192.168.1.67", "/bin/echo ok"])

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

    def test_classify_ssh_client_error_detects_no_matching_mac_offer(self) -> None:
        line = (
            "Unable to negotiate with 192.168.200.214 port 22: no matching MAC found. "
            "Their offer: hmac-md5,hmac-sha1,hmac-ripemd160,hmac-ripemd160@openssh.com,hmac-sha1-96,hmac-md5-96"
        )

        error = ssh_transport.classify_ssh_client_error(line)

        self.assertIsInstance(error, ssh_transport.SshAlgorithmNegotiationError)
        assert isinstance(error, ssh_transport.SshAlgorithmNegotiationError)
        self.assertEqual(error.algorithm, "mac")
        self.assertEqual(error.offered[0:2], ("hmac-md5", "hmac-sha1"))
        self.assertEqual(str(error), line)

    def test_classify_ssh_client_error_detects_auth_rejection(self) -> None:
        error = ssh_transport.classify_ssh_client_error("Permission denied, please try again.\n")

        self.assertIsInstance(error, ssh_transport.SshAuthenticationError)

    def test_spawn_with_password_replaces_invalid_utf8_output(self) -> None:
        try:
            import pexpect  # noqa: F401
        except Exception:
            self.skipTest("pexpect not available")
        fake_child = mock.Mock()
        fake_child.expect.side_effect = [2]
        fake_child.before = "TimeCapsule�\n"
        fake_child.exitstatus = 0
        fake_child.signalstatus = None
        with TemporaryDirectory() as directory:
            client_log = Path(directory) / "client.log"
            with mock.patch("pexpect.spawn", return_value=fake_child) as spawn_mock:
                rc, output = ssh_transport._spawn_with_password(
                    ["ssh", "host", "cmd"],
                    "pw",
                    client_log=client_log,
                    timeout=10,
                    timeout_message="timeout",
                )
        self.assertEqual(rc, 0)
        self.assertEqual(output, "TimeCapsule�\n")
        self.assertEqual(spawn_mock.call_args.kwargs["codec_errors"], "replace")

    def test_spawn_with_password_accepts_first_connection_authenticity_prompt(self) -> None:
        try:
            import pexpect  # noqa: F401
        except Exception:
            self.skipTest("pexpect not available")
        fake_child = mock.Mock()
        fake_child.expect.side_effect = [0, 1, 2]
        fake_child.before = "NetBSD\n"
        fake_child.exitstatus = 0
        fake_child.signalstatus = None
        with TemporaryDirectory() as directory:
            client_log = Path(directory) / "client.log"
            with mock.patch("pexpect.spawn", return_value=fake_child):
                rc, output = ssh_transport._spawn_with_password(
                    ["ssh", "host", "cmd"],
                    "pw",
                    client_log=client_log,
                    timeout=10,
                    timeout_message="timeout",
                )
        self.assertEqual(rc, 0)
        self.assertEqual(output, "NetBSD\n")
        self.assertEqual(fake_child.sendline.call_args_list, [mock.call("yes"), mock.call("pw")])

    def test_spawn_with_password_timeout_raises_timeout_subtype(self) -> None:
        try:
            import pexpect  # noqa: F401
        except Exception:
            self.skipTest("pexpect not available")
        fake_child = mock.Mock()
        fake_child.expect.side_effect = [3]
        fake_child.before = "partial output"
        with TemporaryDirectory() as directory:
            client_log = Path(directory) / "client.log"
            with mock.patch("pexpect.spawn", return_value=fake_child):
                with self.assertRaises(ssh_transport.SshCommandTimeout) as exc:
                    ssh_transport._spawn_with_password(
                        ["ssh", "host", "cmd"],
                        "pw",
                        client_log=client_log,
                        timeout=10,
                        timeout_message="timeout",
                    )
        self.assertNotIsInstance(exc.exception, SystemExit)
        self.assertEqual(str(exc.exception), "timeout")
        fake_child.close.assert_called_once_with(force=False)

    def test_spawn_with_password_does_not_answer_remote_password_text_after_auth(self) -> None:
        try:
            import pexpect  # noqa: F401
        except Exception:
            self.skipTest("pexpect not available")
        fake_child = mock.Mock()
        events = iter([
            (1, "remote says ", "Password:"),
            (2, "\ndone\n", ""),
        ])

        def expect(*_args, **_kwargs):
            index, before, after = next(events)
            fake_child.before = before
            fake_child.after = after
            return index

        fake_child.expect.side_effect = expect
        fake_child.exitstatus = 0
        fake_child.signalstatus = None
        with TemporaryDirectory() as directory:
            client_log = Path(directory) / "client.log"
            client_log.write_text('Authenticated to device ([192.0.2.1]:22) using "password".\n')
            with mock.patch("pexpect.spawn", return_value=fake_child):
                rc, output = ssh_transport._spawn_with_password(
                    ["ssh", "device", "command"],
                    "secret",
                    client_log=client_log,
                    timeout=10,
                    timeout_message="timeout",
                )
        self.assertEqual(rc, 0)
        self.assertEqual(output, "remote says Password:\ndone\n")
        fake_child.sendline.assert_not_called()

    def test_run_ssh_retries_transient_permission_denied(self) -> None:
        attempts = iter([
            (255, "", "Permission denied, please try again.\n"),
            (0, "ok\n", 'Authenticated to device ([192.0.2.1]:22) using "password".\n'),
        ])

        def spawn(_cmd, _password, *, client_log, timeout, timeout_message):
            rc, output, diagnostics = next(attempts)
            Path(client_log).write_text(diagnostics)
            return rc, output

        with mock.patch("timecapsulesmb.transport.ssh._ssh_option_supported", return_value=True), \
             mock.patch("timecapsulesmb.transport.ssh._spawn_with_password", side_effect=spawn) as spawn_mock, \
             mock.patch("timecapsulesmb.transport.ssh.time") as time_mock:
            sleep_mock = time_mock.sleep
            proc = ssh_transport.run_ssh(
                ssh_transport.SshConnection("root@192.168.1.118", "pw", "-o StrictHostKeyChecking=no"),
                "/bin/echo ok",
                check=False,
                timeout=10,
            )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(spawn_mock.call_count, 2)
        sleep_mock.assert_called_once_with(1)

    def test_run_ssh_does_not_retry_passwordless_auth_rejection(self) -> None:
        def spawn(_cmd, _password, *, client_log, timeout, timeout_message):
            Path(client_log).write_text("root@device: Permission denied (publickey).\n")
            return 255, ""

        with mock.patch("timecapsulesmb.transport.ssh._ssh_option_supported", return_value=True), \
             mock.patch("timecapsulesmb.transport.ssh._spawn_with_password", side_effect=spawn) as spawn_mock, \
             mock.patch("timecapsulesmb.transport.ssh.time") as time_mock:
            sleep_mock = time_mock.sleep
            with self.assertRaises(ssh_transport.SshAuthenticationError):
                ssh_transport.run_ssh(
                    ssh_transport.SshConnection("root@192.168.1.118", "", "-o StrictHostKeyChecking=no"),
                    "/bin/echo ok",
                    check=False,
                    timeout=10,
                )

        spawn_mock.assert_called_once()
        sleep_mock.assert_not_called()

    def test_run_ssh_does_not_retry_remote_permission_denied(self) -> None:
        spawn = self.authenticated_spawn(1, "mutation complete\nPermission denied\n")
        with mock.patch("timecapsulesmb.transport.ssh._ssh_option_supported", return_value=True), \
             mock.patch("timecapsulesmb.transport.ssh._spawn_with_password", side_effect=spawn) as run, \
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
        def spawn(_cmd, _password, *, client_log, timeout, timeout_message):
            Path(client_log).write_text('Authenticated to device ([192.0.2.1]:22) using "password".\n')
            return 7, "remote command failed\n"

        with mock.patch("timecapsulesmb.transport.ssh._ssh_option_supported", return_value=True):
            with mock.patch(
                "timecapsulesmb.transport.ssh._spawn_with_password",
                side_effect=spawn,
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
        def spawn(_cmd, _password, *, client_log, timeout, timeout_message):
            Path(client_log).write_text('Authenticated to device ([192.0.2.1]:22) using "password".\n')
            return 7, "remote command failed\n"

        with mock.patch("timecapsulesmb.transport.ssh._ssh_option_supported", return_value=True):
            with mock.patch(
                "timecapsulesmb.transport.ssh._spawn_with_password",
                side_effect=spawn,
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
        def spawn(_cmd, _password, *, client_log, timeout, timeout_message):
            Path(client_log).write_text(
                "Warning: Permanently added device to the list of known hosts.\n"
                'Authenticated to device ([192.0.2.1]:22) using "password".\n'
            )
            return 7, ""

        with mock.patch("timecapsulesmb.transport.ssh._ssh_option_supported", return_value=True):
            with mock.patch(
                "timecapsulesmb.transport.ssh._spawn_with_password",
                side_effect=spawn,
            ):
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
        def fake_spawn(_cmd, _password, *, client_log, timeout, timeout_message):
            raise ssh_transport.SshCommandTimeout(timeout_message)

        remote_cmd = "/bin/sh -c 'echo one\necho two'"
        with mock.patch("timecapsulesmb.transport.ssh._ssh_option_supported", return_value=True):
            with mock.patch("timecapsulesmb.transport.ssh._spawn_with_password", side_effect=fake_spawn):
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

    def test_classify_ssh_client_error_detects_forward_bind_failure(self) -> None:
        output = (
            "bind [127.0.0.1]:108: Permission denied\n"
            "channel_setup_fwd_listener_tcpip: cannot listen to port: 108\n"
            "NetBSD\n"
        )
        error = ssh_transport.classify_ssh_client_error(output)
        self.assertEqual(str(error), "Connecting to the device failed, SSH error: bind [127.0.0.1]:108: Permission denied")

    def test_run_ssh_raises_on_ssh_transport_warning_even_with_zero_exit(self) -> None:
        def spawn(_cmd, _password, *, client_log, timeout, timeout_message):
            Path(client_log).write_text(
                "bind [127.0.0.1]:108: Permission denied\n"
                "channel_setup_fwd_listener_tcpip: cannot listen to port: 108\n"
            )
            return 0, "NetBSD\n6.0\nevbarm\n"

        with mock.patch("timecapsulesmb.transport.ssh._ssh_option_supported", return_value=True), \
             mock.patch("timecapsulesmb.transport.ssh._spawn_with_password", side_effect=spawn):
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
        def spawn(_cmd, _password, *, client_log, timeout, timeout_message):
            Path(client_log).write_text(
                "Warning: Permanently added device to the list of known hosts.\n"
                "** WARNING: connection is not using a post-quantum key exchange algorithm.\n"
                'Authenticated to device ([192.0.2.1]:22) using "password".\n'
            )
            return 0, "NetBSD\n4.0_STABLE\nearmv4\n"

        with mock.patch("timecapsulesmb.transport.ssh._ssh_option_supported", return_value=True), \
             mock.patch("timecapsulesmb.transport.ssh._spawn_with_password", side_effect=spawn):
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

    def test_ssh_opts_use_proxy_falls_back_for_unbalanced_quotes(self) -> None:
        self.assertTrue(ssh_transport.ssh_opts_use_proxy("-J jump.example 'unterminated"))
        self.assertTrue(ssh_transport.ssh_opts_use_proxy("-oProxyCommand='unterminated"))

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
                    ssh_transport.SshConnection("root@192.168.1.118", "pw", "-J jamesyc@ig1wx38mgh6to6vo.myfritz.net:22123 -o HostKeyAlgorithms=+ssh-rsa"),
                    "/bin/echo ok",
                    check=False,
                    timeout=10,
                )
        cmd = spawn_mock.call_args.args[0]
        self.assertIn("-J", cmd)
        self.assertIn("jamesyc@ig1wx38mgh6to6vo.myfritz.net:22123", cmd)
        self.assertEqual(cmd[-2:], ["root@192.168.1.118", "/bin/echo ok"])

    def test_run_ssh_respects_explicit_identity_without_restricting_agent(self) -> None:
        with mock.patch(
            "timecapsulesmb.transport.ssh._ssh_option_supported",
            return_value=True,
        ):
            with mock.patch(
                "timecapsulesmb.transport.ssh._spawn_with_password",
                side_effect=self.authenticated_spawn(),
            ) as spawn_mock:
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
        cmd = spawn_mock.call_args.args[0]
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
            "timecapsulesmb.transport.ssh.subprocess.run",
            return_value=subprocess.CompletedProcess(
                ["ssh"],
                255,
                stdout="",
                stderr="command-line: line 0: Bad configuration option: pubkeyacceptedalgorithms\n",
            ),
        ):
            self.assertFalse(ssh_transport._ssh_option_supported("PubkeyAcceptedAlgorithms"))

    def test_ssh_option_supported_returns_false_when_ssh_binary_is_missing(self) -> None:
        with mock.patch("timecapsulesmb.transport.ssh.subprocess.run", side_effect=OSError("missing ssh")):
            self.assertFalse(ssh_transport._ssh_option_supported("PubkeyAcceptedAlgorithms"))

    def test_spawn_with_password_reports_missing_pexpect(self) -> None:
        with mock.patch("builtins.__import__", side_effect=self.missing_pexpect_import):
            with self.assertRaises(ssh_transport.SshError) as exc:
                ssh_transport._spawn_with_password(
                    ["ssh", "host", "cmd"],
                    "pw",
                    client_log=Path("/tmp/client.log"),
                    timeout=10,
                    timeout_message="timeout",
                )
        self.assertNotIsInstance(exc.exception, SystemExit)
        self.assertIn(MISSING_PEXPECT_PREFIX, str(exc.exception))
        self.assertIn(MISSING_PEXPECT_ERROR, str(exc.exception))

    def test_ssh_local_forward_reports_missing_pexpect(self) -> None:
        with mock.patch("builtins.__import__", side_effect=self.missing_pexpect_import):
            with self.assertRaises(ssh_transport.SshError) as exc:
                with ssh_transport.ssh_local_forward(
                    ssh_transport.SshConnection("root@192.168.1.118", "pw", ""),
                    local_port=10445,
                    remote_host="127.0.0.1",
                    remote_port=445,
                    ready_timeout=5,
                ):
                    pass
        self.assertNotIsInstance(exc.exception, SystemExit)
        self.assertIn(MISSING_PEXPECT_PREFIX, str(exc.exception))
        self.assertIn(MISSING_PEXPECT_ERROR, str(exc.exception))

    def test_ssh_local_forward_waits_for_port_and_closes_child(self) -> None:
        try:
            import pexpect  # noqa: F401
        except Exception:
            self.skipTest("pexpect not available")
        fake_child = mock.Mock()
        fake_child.expect.side_effect = [0, 1, 3]
        fake_child.before = ""
        fake_child.isalive.return_value = True
        with mock.patch("pexpect.spawn", return_value=fake_child) as spawn_mock:
            with mock.patch("timecapsulesmb.transport.ssh._ssh_option_supported", return_value=True):
                with mock.patch("timecapsulesmb.transport.ssh.tcp_open", return_value=True) as tcp_open_mock:
                    with ssh_transport.ssh_local_forward(
                        ssh_transport.SshConnection("root@192.168.1.118", "pw", "-J jump.example"),
                        local_port=10445,
                        remote_host="127.0.0.1",
                        remote_port=445,
                        ready_timeout=5,
                    ):
                        pass
        cmd = spawn_mock.call_args.args[0:2]
        self.assertEqual(cmd[0], "ssh")
        self.assertIn("-F", cmd[1])
        self.assertIn("/dev/null", cmd[1])
        self.assertIn("-J", cmd[1])
        self.assertIn("jump.example", cmd[1])
        self.assertEqual(fake_child.sendline.call_args_list, [mock.call("yes"), mock.call("pw")])
        tcp_open_mock.assert_called_once_with("127.0.0.1", 10445, timeout=0.2)
        fake_child.close.assert_called_once_with(force=True)

    def test_ssh_local_forward_reports_transport_error_before_ready(self) -> None:
        try:
            import pexpect  # noqa: F401
        except Exception:
            self.skipTest("pexpect not available")
        fake_child = mock.Mock()
        fake_child.expect.side_effect = [2]
        fake_child.before = "bind [127.0.0.1]:10445: Permission denied\n"
        with mock.patch("pexpect.spawn", return_value=fake_child):
            with mock.patch("timecapsulesmb.transport.ssh._ssh_option_supported", return_value=True):
                with self.assertRaises(ssh_transport.SshError) as exc:
                    with ssh_transport.ssh_local_forward(
                        ssh_transport.SshConnection("root@192.168.1.118", "pw", ""),
                        local_port=10445,
                        remote_host="127.0.0.1",
                        remote_port=445,
                        ready_timeout=5,
                    ):
                        pass
        self.assertNotIsInstance(exc.exception, SystemExit)
        self.assertIn("bind [127.0.0.1]:10445: Permission denied", str(exc.exception))
        fake_child.close.assert_called_once_with(force=True)

    def test_ssh_local_forward_timeout_reports_tunnel_target(self) -> None:
        try:
            import pexpect  # noqa: F401
        except Exception:
            self.skipTest("pexpect not available")
        fake_child = mock.Mock()
        fake_child.expect.side_effect = [3]
        fake_child.before = ""
        fake_child.isalive.return_value = False
        with mock.patch("pexpect.spawn", return_value=fake_child):
            with mock.patch("timecapsulesmb.transport.ssh._ssh_option_supported", return_value=True):
                with mock.patch("timecapsulesmb.transport.ssh.tcp_open", return_value=False):
                    with mock.patch("timecapsulesmb.transport.ssh.time.time", side_effect=[100.0, 106.0]):
                        with self.assertRaises(ssh_transport.SshError) as exc:
                            with ssh_transport.ssh_local_forward(
                                ssh_transport.SshConnection("root@192.168.1.118", "pw", ""),
                                local_port=10445,
                                remote_host="10.0.1.1",
                                remote_port=445,
                                ready_timeout=5,
                            ):
                                pass
        self.assertNotIsInstance(exc.exception, SystemExit)
        self.assertEqual(
            str(exc.exception),
            "Timed out waiting for ssh tunnel to become ready: 127.0.0.1:10445 -> 10.0.1.1:445 via root@192.168.1.118",
        )
        fake_child.close.assert_called_once_with(force=True)

    def test_verify_uploaded_size_retries_transient_failure(self) -> None:
        with NamedTemporaryFile() as tmp:
            src = Path(tmp.name)
            src.write_bytes(b"hello")
            responses = [
                subprocess.CompletedProcess(["ssh"], 1, stdout="not ready\n", stderr=""),
                subprocess.CompletedProcess(["ssh"], 0, stdout="5\n", stderr=""),
            ]
            with mock.patch("timecapsulesmb.transport.ssh.run_ssh", side_effect=responses) as run, \
                 mock.patch("timecapsulesmb.transport.ssh.time") as time_mock:
                ssh_transport._verify_uploaded_size(
                    ssh_transport.SshConnection("device", "pw", ""),
                    src,
                    "/tmp/test-upload",
                    timeout=30,
                )
        self.assertEqual(run.call_count, 2)
        time_mock.sleep.assert_called_once_with(1)

    def test_verify_uploaded_size_failure_reports_source_and_destination(self) -> None:
        with NamedTemporaryFile() as tmp:
            src = Path(tmp.name)
            src.write_bytes(b"hello")
            process = subprocess.CompletedProcess(["ssh"], 0, stdout="3\n", stderr="")
            with mock.patch("timecapsulesmb.transport.ssh.run_ssh", return_value=process), \
                 mock.patch("timecapsulesmb.transport.ssh.time"):
                with self.assertRaises(ssh_transport.SshError) as exc:
                    ssh_transport._verify_uploaded_size(
                        ssh_transport.SshConnection("device", "pw", ""),
                        src,
                        "/tmp/test-upload",
                        timeout=30,
                    )
        self.assertEqual(
            str(exc.exception),
            f"upload verification failed for {src.name} -> /tmp/test-upload: expected 5 bytes, got 3 bytes",
        )

    def test_upload_file_streams_bytes_and_verifies_size(self) -> None:
        process = subprocess.CompletedProcess(["ssh"], 0, b"", b"")
        with NamedTemporaryFile() as tmp:
            src = Path(tmp.name)
            src.write_bytes(b"hello")
            with mock.patch("timecapsulesmb.transport.ssh._run_piped_ssh", return_value=process) as run, \
                 mock.patch("timecapsulesmb.transport.ssh._verify_uploaded_size") as verify:
                ssh_transport.upload_file(
                    ssh_transport.SshConnection("device", "pw", ""),
                    src,
                    "/Volumes/dk2/.samba4/smbd",
                    timeout=180,
                )
        self.assertEqual(run.call_args.kwargs["input_bytes"], b"hello")
        self.assertEqual(run.call_args.kwargs["timeout"], 180)
        self.assertIn("cat > ", run.call_args.args[1])
        verify.assert_called_once_with(run.call_args.args[0], src, "/Volumes/dk2/.samba4/smbd", timeout=30)

    def test_upload_file_reports_remote_failure(self) -> None:
        process = subprocess.CompletedProcess(["ssh"], 1, b"", b"disk full\n")
        with NamedTemporaryFile() as tmp:
            src = Path(tmp.name)
            src.write_bytes(b"hello")
            with mock.patch("timecapsulesmb.transport.ssh._run_piped_ssh", return_value=process):
                with self.assertRaisesRegex(ssh_transport.SshError, "disk full"):
                    ssh_transport.upload_file(
                        ssh_transport.SshConnection("device", "pw", ""),
                        src,
                        "/tmp/test-upload",
                    )

    def test_upload_file_explains_missing_sshpass(self) -> None:
        with NamedTemporaryFile() as tmp:
            src = Path(tmp.name)
            src.write_bytes(b"hello")
            with mock.patch("timecapsulesmb.transport.ssh.find_command", return_value=None):
                with self.assertRaises(ssh_transport.SshError) as exc:
                    ssh_transport.upload_file(
                        ssh_transport.SshConnection("device", "pw", ""),
                        src,
                        "/tmp/test-upload",
                    )
        self.assertIn("password require local sshpass", str(exc.exception))
        self.assertIn("tcapsule bootstrap", str(exc.exception))

    def test_run_ssh_capture_bytes_returns_binary_stdout(self) -> None:
        payload = b"\x00firmware\xff\n"
        process = subprocess.CompletedProcess(["sshpass"], 0, payload, b"")
        connection = ssh_transport.SshConnection("device", "pw", "")
        with mock.patch("timecapsulesmb.transport.ssh.find_command", return_value="/usr/bin/sshpass"), \
             mock.patch("timecapsulesmb.transport.ssh.subprocess.run", side_effect=self.completed_run(process)) as run:
            self.assertEqual(ssh_transport.run_ssh_capture_bytes(connection, "/bin/dd if=/dev/rflash0.raw"), payload)
        command = run.call_args.args[0]
        self.assertEqual(command[:3], ["sshpass", "-e", "ssh"])
        self.assertIn("-n", command)
        self.assertIn("-T", command)
        self.assertIn("-S", command)

    def test_run_ssh_capture_bytes_without_password_uses_plain_ssh(self) -> None:
        payload = b"bank"
        process = subprocess.CompletedProcess(["ssh"], 0, payload, b"")
        connection = ssh_transport.SshConnection("device", "", "")
        with mock.patch("timecapsulesmb.transport.ssh.find_command", side_effect=AssertionError("must not need sshpass")), \
             mock.patch("timecapsulesmb.transport.ssh.subprocess.run", side_effect=self.completed_run(process)) as run:
            self.assertEqual(ssh_transport.run_ssh_capture_bytes(connection, "dd"), payload)
        command = run.call_args.args[0]
        self.assertEqual(command[0], "ssh")
        self.assertIn("BatchMode=yes", command)

    def test_run_ssh_capture_bytes_does_not_decode_successful_binary_stdout(self) -> None:
        payload = DecodeTrapBytes(b"\x00firmware\xff" * 4096)
        process = subprocess.CompletedProcess(["sshpass"], 0, payload, b"")
        with mock.patch("timecapsulesmb.transport.ssh.find_command", return_value="/usr/bin/sshpass"), \
             mock.patch("timecapsulesmb.transport.ssh.subprocess.run", side_effect=self.completed_run(process)):
            result = ssh_transport.run_ssh_capture_bytes(
                ssh_transport.SshConnection("device", "pw", ""),
                "dd",
            )
        self.assertEqual(result, payload)

    def test_run_ssh_capture_bytes_does_not_decode_failed_binary_stdout(self) -> None:
        payload = DecodeTrapBytes(b"x" * (ssh_transport.SSH_ERROR_STDOUT_PREFIX_BYTES + 100))
        process = subprocess.CompletedProcess(["sshpass"], 1, payload, b"")
        with mock.patch("timecapsulesmb.transport.ssh.find_command", return_value="/usr/bin/sshpass"), \
             mock.patch("timecapsulesmb.transport.ssh.subprocess.run", side_effect=self.completed_run(process)):
            with self.assertRaisesRegex(ssh_transport.SshError, "ssh command failed with rc=1"):
                ssh_transport.run_ssh_capture_bytes(
                    ssh_transport.SshConnection("device", "pw", ""),
                    "dd",
                )

    def test_piped_ssh_retries_auth_failure_from_client_log(self) -> None:
        processes = iter([
            subprocess.CompletedProcess(["sshpass"], 255, b"", b""),
            subprocess.CompletedProcess(["sshpass"], 0, b"ok", b""),
        ])
        logs = iter([
            "root@device: Permission denied (password).\n",
            'Authenticated to device ([192.0.2.1]:22) using "password".\n',
        ])

        def run(command, **_kwargs):
            if "-E" not in command:
                return subprocess.CompletedProcess(command, 0, b"", b"")
            Path(command[command.index("-E") + 1]).write_text(next(logs))
            return next(processes)

        with mock.patch("timecapsulesmb.transport.ssh.find_command", return_value="/usr/bin/sshpass"), \
             mock.patch("timecapsulesmb.transport.ssh.subprocess.run", side_effect=run) as subprocess_run, \
             mock.patch("timecapsulesmb.transport.ssh.time") as time_mock:
            result = ssh_transport.run_ssh_capture_bytes(
                ssh_transport.SshConnection("device", "pw", ""),
                "dd",
            )
        self.assertEqual(result, b"ok")
        self.assertEqual(sum("-E" in call.args[0] for call in subprocess_run.call_args_list), 2)
        time_mock.sleep.assert_called_once_with(1)

    def test_remote_permission_denied_is_not_classified_or_retried(self) -> None:
        process = subprocess.CompletedProcess(["sshpass"], 1, b"", b"cat: protected: Permission denied\n")
        with mock.patch("timecapsulesmb.transport.ssh.find_command", return_value="/usr/bin/sshpass"), \
             mock.patch("timecapsulesmb.transport.ssh.subprocess.run", side_effect=self.completed_run(process)) as run:
            with self.assertRaisesRegex(ssh_transport.SshError, "Permission denied"):
                ssh_transport.run_ssh_capture_bytes(
                    ssh_transport.SshConnection("device", "pw", ""),
                    "cat protected",
                )
        self.assertEqual(sum("-E" in call.args[0] for call in run.call_args_list), 1)

    def test_passwordless_auth_rejection_uses_client_log(self) -> None:
        process = subprocess.CompletedProcess(["ssh"], 255, b"", b"")
        diagnostics = "root@device: Permission denied (publickey).\n"
        with mock.patch("timecapsulesmb.transport.ssh.subprocess.run", side_effect=self.completed_run(process, diagnostics)) as run:
            with self.assertRaises(ssh_transport.SshAuthenticationError):
                ssh_transport.run_ssh_capture_bytes(
                    ssh_transport.SshConnection("device", "", ""),
                    "dd",
                )
        self.assertEqual(sum("-E" in call.args[0] for call in run.call_args_list), 1)


class MigrationInputTransportTests(unittest.TestCase):
    def test_request_bytes_and_separate_output_use_existing_transport(self):
        connection = ssh_transport.SshConnection("device", "", "")
        process = subprocess.CompletedProcess(["ssh"], 0, b'{"version":1}\n', b'diagnostic\n')
        with mock.patch.object(ssh_transport, "_run_piped_ssh", return_value=process) as run:
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
        self.assertTrue(run.call_args.kwargs["raw_remote_status"])
        self.assertEqual(run.call_args.kwargs["extra_ssh_args"], ("-o", "ConnectTimeout=20"))

    def test_raw_remote_status_is_single_attempt_and_keeps_native_stderr(self):
        connection = ssh_transport.SshConnection("device", "pw", "")
        process = subprocess.CompletedProcess(["ssh"], 4, b'{"version":1}', b"Permission denied in TDB")
        with mock.patch.object(ssh_transport, "find_command", return_value="/usr/bin/sshpass"):
            with mock.patch(
                "timecapsulesmb.transport.ssh.subprocess.run",
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

    def test_raw_remote_status_rejects_sshpass_failures_without_retry(self):
        connection = ssh_transport.SshConnection("device", "pw", "")
        cases = (
            (5, b"", "root@device: Permission denied (password).\n", ssh_transport.SshAuthenticationError),
            (6, b"Host public key is unknown.\n", "", ssh_transport.SshError),
            (7, b"IP public key changed.\n", "", ssh_transport.SshError),
        )
        for status, stderr, diagnostics, error in cases:
            with self.subTest(status=status):
                process = subprocess.CompletedProcess(["sshpass"], status, b"", stderr)
                with mock.patch.object(ssh_transport, "find_command", return_value="/usr/bin/sshpass"):
                    with mock.patch(
                        "timecapsulesmb.transport.ssh.subprocess.run",
                        side_effect=SSHTransportTests.completed_run(process, diagnostics),
                    ) as run:
                        with mock.patch("timecapsulesmb.transport.ssh.time.sleep") as sleep:
                            with self.assertRaises(error):
                                ssh_transport.run_ssh_input(
                                    connection,
                                    "helper multi copy",
                                    raw_remote_status=True,
                                )
                run.assert_called_once()
                sleep.assert_not_called()

    def test_explicit_unlimited_piped_timeout_preserves_ordinary_defaults(self):
        connection = ssh_transport.SshConnection("device", "", "")
        process = subprocess.CompletedProcess(["ssh"], 0, b"ok", b"")
        with mock.patch(
            "timecapsulesmb.transport.ssh.subprocess.run",
            side_effect=SSHTransportTests.completed_run(process),
        ) as run:
            ssh_transport.run_ssh_input(connection, "helper", timeout=None)
        self.assertIsNone(run.call_args.kwargs["timeout"])
        with mock.patch(
            "timecapsulesmb.transport.ssh.subprocess.run",
            side_effect=SSHTransportTests.completed_run(process),
        ) as run:
            ssh_transport.run_ssh_input(connection, "helper")
        self.assertEqual(run.call_args.kwargs["timeout"], 120)

    def test_failed_remote_helper_cannot_look_like_a_json_success(self):
        process = subprocess.CompletedProcess(["ssh"], 4, b'{"version":1}', b'corrupt TDB')
        with mock.patch.object(ssh_transport, "_run_piped_ssh", return_value=process):
            with self.assertRaisesRegex(ssh_transport.SshError, "corrupt TDB"):
                ssh_transport.run_ssh_input(ssh_transport.SshConnection("device", "", ""), "helper")


if __name__ == "__main__":
    unittest.main()
