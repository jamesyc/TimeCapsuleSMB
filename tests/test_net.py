from __future__ import annotations

import socket
import sys
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from timecapsulesmb.core.net import (  # noqa: E402
    canonical_ssh_target,
    endpoint_host,
    ipv4_literal,
    ipv6_literal,
    is_link_local_ip,
    is_link_local_ipv4,
    is_link_local_ipv6,
    is_loopback_ipv4,
    resolve_host_ips,
    resolve_host_ipv4s,
    resolve_host_ipv6s,
)


class NetTests(unittest.TestCase):
    def test_endpoint_host_strips_supported_wrappers_users_paths_and_ports(self) -> None:
        cases = {
            "root@192.168.1.1:22": "192.168.1.1",
            "airport.local:445": "airport.local",
            "root@airport.local:22": "airport.local",
            "smb://admin@airport.local:445/share": "airport.local",
            "root@[fd00::2]:22": "fd00::2",
            "[fd00::2]:445": "fd00::2",
            "fd00::2": "fd00::2",
            " capsule.local. ": "capsule.local",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(endpoint_host(raw), expected)

    def test_canonical_ssh_target_adds_root_and_strips_default_port(self) -> None:
        self.assertEqual(canonical_ssh_target("10.0.0.2:22"), "root@10.0.0.2")
        self.assertEqual(canonical_ssh_target("admin@capsule.local:22"), "admin@capsule.local")
        self.assertEqual(canonical_ssh_target("root@[fd00::2]:22"), "root@fd00::2")

    def test_canonical_ssh_target_rejects_non_default_or_invalid_ports(self) -> None:
        with self.assertRaises(ValueError):
            canonical_ssh_target("root@10.0.0.2:2222")
        with self.assertRaises(ValueError):
            canonical_ssh_target("root@capsule.local:ssh")

    def test_ipv4_literal_accepts_zero_padded_ipv4_and_rejects_non_ipv4(self) -> None:
        self.assertEqual(ipv4_literal("010.000.001.007"), "10.0.1.7")
        self.assertIsNone(ipv4_literal("fe80::1"))
        self.assertIsNone(ipv4_literal("10.0.1.999"))
        self.assertIsNone(ipv4_literal("capsule.local"))

    def test_ipv6_literal_accepts_ipv6_and_rejects_non_ipv6(self) -> None:
        self.assertEqual(ipv6_literal("FD00::1"), "fd00::1")
        self.assertEqual(ipv6_literal("fe80::1%en0"), "fe80::1")
        self.assertIsNone(ipv6_literal("10.0.1.7"))
        self.assertIsNone(ipv6_literal("capsule.local"))

    def test_link_local_and_loopback_detection(self) -> None:
        self.assertTrue(is_link_local_ipv4("169.254.44.9"))
        self.assertFalse(is_link_local_ipv4("10.0.0.2"))
        self.assertTrue(is_link_local_ipv6("fe80::1"))
        self.assertTrue(is_link_local_ipv6("fe80::1%en0"))
        self.assertFalse(is_link_local_ipv6("fd00::1"))
        self.assertTrue(is_link_local_ip("169.254.44.9"))
        self.assertTrue(is_link_local_ip("fe80::1"))
        self.assertFalse(is_link_local_ip("10.0.0.2"))
        self.assertTrue(is_loopback_ipv4("127.0.0.1"))
        self.assertFalse(is_loopback_ipv4("169.254.44.9"))

    def test_resolve_host_ipv4s_deduplicates_and_normalizes(self) -> None:
        addrinfo = [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("010.000.000.002", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("10.0.0.2", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("169.254.44.9", 0)),
        ]
        with mock.patch("timecapsulesmb.core.net.socket.getaddrinfo", return_value=addrinfo):
            self.assertEqual(resolve_host_ipv4s("capsule.local"), ("10.0.0.2", "169.254.44.9"))

    def test_resolve_host_ipv4s_returns_empty_on_resolution_failure(self) -> None:
        with mock.patch("timecapsulesmb.core.net.socket.getaddrinfo", side_effect=OSError("no dns")):
            self.assertEqual(resolve_host_ipv4s("capsule.local"), ())

    def test_resolve_host_ipv6s_deduplicates_and_normalizes(self) -> None:
        addrinfo = [
            (socket.AF_INET6, socket.SOCK_STREAM, 0, "", ("FD00::2", 0, 0, 0)),
            (socket.AF_INET6, socket.SOCK_STREAM, 0, "", ("fd00::2", 0, 0, 0)),
            (socket.AF_INET6, socket.SOCK_STREAM, 0, "", ("fe80::1%en0", 0, 0, 4)),
        ]
        with mock.patch("timecapsulesmb.core.net.socket.getaddrinfo", return_value=addrinfo):
            self.assertEqual(resolve_host_ipv6s("capsule.local"), ("fd00::2", "fe80::1"))

    def test_resolve_host_ips_preserves_ipv4_then_ipv6_order(self) -> None:
        with mock.patch("timecapsulesmb.core.net.resolve_host_ipv4s", return_value=("10.0.0.2",)):
            with mock.patch("timecapsulesmb.core.net.resolve_host_ipv6s", return_value=("fd00::2",)):
                self.assertEqual(resolve_host_ips("capsule.local"), ("10.0.0.2", "fd00::2"))


if __name__ == "__main__":
    unittest.main()
