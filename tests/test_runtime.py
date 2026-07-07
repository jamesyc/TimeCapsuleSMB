from __future__ import annotations

import io
import socket
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from timecapsulesmb.cli.runtime import (
    json_text,
    print_json,
    write_json_file,
)
from timecapsulesmb.core.config import AppConfig, ConfigError, DEFAULTS
from timecapsulesmb.services.runtime import resolve_env_connection, ssh_target_link_local_resolution_error


class RuntimeTests(unittest.TestCase):
    def test_resolve_env_connection_defaults_ssh_opts_when_missing(self) -> None:
        config = AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"})
        connection = resolve_env_connection(config)

        self.assertEqual(connection.host, "root@10.0.0.2")
        self.assertEqual(connection.password, "pw")
        self.assertEqual(connection.ssh_opts, DEFAULTS["TC_SSH_OPTS"])

    def test_resolve_env_connection_preserves_configured_ssh_opts(self) -> None:
        config = AppConfig.from_values({
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o ProxyJump=bastion",
        })
        connection = resolve_env_connection(config)

        self.assertEqual(connection.ssh_opts, "-o ProxyJump=bastion")

    def test_resolve_env_connection_uses_password_provider_when_password_missing(self) -> None:
        config = AppConfig.from_values({"TC_HOST": "root@10.0.0.2"})
        provider = mock.Mock(return_value="prompted-pw")

        connection = resolve_env_connection(config, password_provider=provider)

        provider.assert_called_once_with("Device root password: ")
        self.assertEqual(connection.password, "prompted-pw")

    def test_resolve_env_connection_does_not_prompt_without_provider(self) -> None:
        config = AppConfig.from_values({"TC_HOST": "root@10.0.0.2"})

        with self.assertRaises(ConfigError) as ctx:
            resolve_env_connection(config)

        self.assertIn("TC_PASSWORD is required", str(ctx.exception))

    def test_ssh_target_link_local_resolution_error_rejects_resolved_hostname(self) -> None:
        addrinfo = [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("169.254.44.9", 0))]

        with mock.patch("timecapsulesmb.core.net.socket.getaddrinfo", return_value=addrinfo):
            error = ssh_target_link_local_resolution_error(
                "root@capsule.local",
                DEFAULTS["TC_SSH_OPTS"],
            )

        self.assertIsNotNone(error)
        assert error is not None
        self.assertIn("capsule.local resolves to link-local address 169.254.44.9", error)

    def test_ssh_target_link_local_resolution_error_rejects_resolved_ipv6_hostname(self) -> None:
        addrinfo = [(socket.AF_INET6, socket.SOCK_STREAM, 0, "", ("fe80::1%en0", 0, 0, 4))]

        with mock.patch("timecapsulesmb.core.net.socket.getaddrinfo", return_value=addrinfo):
            error = ssh_target_link_local_resolution_error(
                "root@capsule.local",
                DEFAULTS["TC_SSH_OPTS"],
            )

        self.assertIsNotNone(error)
        assert error is not None
        self.assertIn("capsule.local resolves to link-local address fe80::1", error)

    def test_ssh_target_link_local_resolution_error_allows_loopback_hostname(self) -> None:
        addrinfo = [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 0))]

        with mock.patch("timecapsulesmb.core.net.socket.getaddrinfo", return_value=addrinfo):
            error = ssh_target_link_local_resolution_error(
                "root@localhost",
                DEFAULTS["TC_SSH_OPTS"],
            )

        self.assertIsNone(error)

    def test_ssh_target_link_local_resolution_error_skips_proxied_ssh(self) -> None:
        with mock.patch("timecapsulesmb.core.net.socket.getaddrinfo", side_effect=AssertionError("should not resolve")):
            error = ssh_target_link_local_resolution_error(
                "root@capsule.local",
                "-o ProxyJump=bastion",
            )

        self.assertIsNone(error)

    def test_json_text_uses_stable_pretty_format(self) -> None:
        self.assertEqual(json_text({"b": 1, "a": {"c": 2}}), '{\n  "a": {\n    "c": 2\n  },\n  "b": 1\n}')

    def test_json_text_preserves_sensitive_values_for_file_serialization(self) -> None:
        self.assertEqual(json_text({"TC_PASSWORD": "secret"}), '{\n  "TC_PASSWORD": "secret"\n}')

    def test_print_json_redacts_nested_sensitive_values(self) -> None:
        output = io.StringIO()

        with redirect_stdout(output):
            print_json({
                "host": "root@10.0.0.2",
                "TC_PASSWORD": "secret",
                "nested": {
                    "credentials": {"password": "nested-secret"},
                    "key_id": "observed-k30a-78100",
                    "tokens": ["token-secret"],
                    "ok": True,
                },
                "items": [
                    {"session_secret": "session-secret-value"},
                    {"name": "public"},
                ],
                "localization_key": "configure.auth_failed",
            })

        self.assertEqual(
            output.getvalue(),
            (
                '{\n'
                '  "TC_PASSWORD": "<redacted>",\n'
                '  "host": "root@10.0.0.2",\n'
                '  "items": [\n'
                '    {\n'
                '      "session_secret": "<redacted>"\n'
                '    },\n'
                '    {\n'
                '      "name": "public"\n'
                '    }\n'
                '  ],\n'
                '  "localization_key": "configure.auth_failed",\n'
                '  "nested": {\n'
                '    "credentials": "<redacted>",\n'
                '    "key_id": "observed-k30a-78100",\n'
                '    "ok": true,\n'
                '    "tokens": "<redacted>"\n'
                '  }\n'
                '}\n'
            ),
        )
        self.assertNotIn("nested-secret", output.getvalue())
        self.assertNotIn("token-secret", output.getvalue())
        self.assertNotIn("session-secret-value", output.getvalue())

    def test_write_json_file_adds_trailing_newline(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifest.json"
            write_json_file(path, {"b": 1, "a": 2})
            self.assertEqual(path.read_text(), '{\n  "a": 2,\n  "b": 1\n}\n')


if __name__ == "__main__":
    unittest.main()
