from __future__ import annotations

import socket
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from timecapsulesmb.cli.runtime import (
    json_text,
    resolve_env_connection,
    ssh_target_link_local_resolution_error,
    write_json_file,
)
from timecapsulesmb.core.config import AppConfig, DEFAULTS


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

    def test_ssh_target_link_local_resolution_error_rejects_resolved_hostname(self) -> None:
        addrinfo = [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("169.254.44.9", 0))]

        with mock.patch("timecapsulesmb.core.net.socket.getaddrinfo", return_value=addrinfo):
            error = ssh_target_link_local_resolution_error(
                "root@capsule.local",
                DEFAULTS["TC_SSH_OPTS"],
            )

        self.assertIsNotNone(error)
        assert error is not None
        self.assertIn("capsule.local resolves to 169.254.x.x link-local IPv4 address 169.254.44.9", error)

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

    def test_write_json_file_adds_trailing_newline(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifest.json"
            write_json_file(path, {"b": 1, "a": 2})
            self.assertEqual(path.read_text(), '{\n  "a": 2,\n  "b": 1\n}\n')


if __name__ == "__main__":
    unittest.main()
