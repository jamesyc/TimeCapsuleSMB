from __future__ import annotations

import unittest
from unittest import mock

from timecapsulesmb.discovery.bonjour import BonjourResolvedService
from timecapsulesmb.services.configure_target import (
    bonjour_record_from_selected_record,
    resolve_configure_target,
)


class ConfigureTargetTests(unittest.TestCase):
    def test_explicit_host_wins_over_selected_record_and_existing_config(self) -> None:
        record = BonjourResolvedService("Office", "office.local.", "_airport._tcp.local.", ipv4=["10.0.0.5"])

        target = resolve_configure_target(
            explicit_host="root@10.0.0.9",
            selected_record=record,
            existing={"TC_HOST": "root@10.0.0.2"},
            ssh_opts="",
        )

        self.assertEqual(target.host, "root@10.0.0.9")
        self.assertEqual(target.source, "explicit_host")
        self.assertIs(target.selected_record, record)

    def test_explicit_bare_host_is_canonicalized_before_validation(self) -> None:
        target = resolve_configure_target(
            explicit_host="10.0.0.9",
            selected_record=None,
            existing={},
            ssh_opts="",
        )

        self.assertEqual(target.host, "root@10.0.0.9")
        self.assertEqual(target.source, "explicit_host")

    def test_proxy_ssh_opts_are_rejected_before_any_name_lookup(self) -> None:
        with mock.patch("timecapsulesmb.core.net.socket.getaddrinfo", side_effect=AssertionError("must not resolve")):
            with self.assertRaises(ValueError) as raised:
                resolve_configure_target(
                    explicit_host="root@capsule.local",
                    selected_record=None,
                    existing={},
                    ssh_opts="-o ProxyJump=bastion",
                )

        self.assertIn("TC_SSH_OPTS must not use ProxyJump", str(raised.exception))

    def test_explicit_link_local_host_is_rejected(self) -> None:
        with self.assertRaises(ValueError) as raised:
            resolve_configure_target(
                explicit_host="root@169.254.44.9",
                selected_record=None,
                existing={},
                ssh_opts="",
            )

        self.assertIn("Device SSH target host must not be a link-local address", str(raised.exception))

    def test_explicit_hostname_is_kept_without_a_name_lookup(self) -> None:
        # An AirPort's .local name also resolves to its 169.254 and fe80
        # addresses; the name is saved as typed and resolved at connect time.
        with mock.patch("timecapsulesmb.core.net.socket.getaddrinfo") as getaddrinfo:
            resolution = resolve_configure_target(
                explicit_host="root@AirPort-Time-Capsule.local",
                selected_record=None,
                existing={},
                ssh_opts="",
            )

        self.assertEqual(resolution.host, "root@AirPort-Time-Capsule.local")
        self.assertEqual(resolution.source, "explicit_host")
        getaddrinfo.assert_not_called()

    def test_explicit_scoped_link_local_host_is_kept_with_its_zone(self) -> None:
        resolution = resolve_configure_target(
            explicit_host="FE80:0000:0000:0000:82EA:96FF:FEE6:5868%en0",
            selected_record=None,
            existing={},
            ssh_opts="",
        )

        self.assertEqual(resolution.host, "root@fe80::82ea:96ff:fee6:5868%en0")

    def test_explicit_unscoped_link_local_ipv6_host_is_rejected(self) -> None:
        with self.assertRaises(ValueError) as raised:
            resolve_configure_target(explicit_host="root@fe80::1", selected_record=None, existing={}, ssh_opts="")

        self.assertIn("fe80::1 is a link-local IPv6 address without its interface", str(raised.exception))

    def test_existing_link_local_host_is_rejected(self) -> None:
        with self.assertRaises(ValueError) as raised:
            resolve_configure_target(
                explicit_host="",
                selected_record=None,
                existing={"TC_HOST": "root@169.254.44.9"},
                ssh_opts="",
            )

        self.assertIn("Device SSH target host must not be a link-local address", str(raised.exception))

    def test_selected_record_refreshes_stale_existing_ip(self) -> None:
        record = BonjourResolvedService(
            "Office",
            "office.local.",
            "_airport._tcp.local.",
            ipv4=["10.0.0.80"],
            properties={"syAP": "119"},
            fullname="Office._airport._tcp.local.",
        )

        target = resolve_configure_target(
            explicit_host="",
            selected_record=record,
            existing={"TC_HOST": "root@10.0.0.2"},
            ssh_opts="",
        )

        self.assertEqual(target.host, "root@10.0.0.80")
        self.assertEqual(target.source, "selected_record")
        self.assertEqual(target.discovered_airport_syap, "119")

    def test_existing_config_is_used_when_no_explicit_or_selected_host_exists(self) -> None:
        target = resolve_configure_target(
            explicit_host="",
            selected_record=None,
            existing={"TC_HOST": "root@10.0.0.2"},
            ssh_opts="",
        )

        self.assertEqual(target.host, "root@10.0.0.2")
        self.assertEqual(target.source, "existing_config")

    def test_link_local_only_selected_record_is_rejected_without_using_existing_config(self) -> None:
        record = BonjourResolvedService(
            "Office",
            "office.local.",
            "_airport._tcp.local.",
            ipv4=["169.254.108.120"],
            ipv6=["fe80::9272:40ff:fe07:36a2%7"],
        )

        for existing in ({}, {"TC_HOST": "root@10.0.0.2"}):
            with self.subTest(existing=existing):
                with self.assertRaises(ValueError) as raised:
                    resolve_configure_target(
                        explicit_host="",
                        selected_record=record,
                        existing=existing,
                        ssh_opts="",
                    )

                self.assertIn("only advertised link-local addresses", str(raised.exception))

    def test_explicit_host_is_used_for_link_local_only_selected_record(self) -> None:
        record = BonjourResolvedService("Office", "office.local.", "_airport._tcp.local.", ipv4=["169.254.108.120"])

        target = resolve_configure_target(
            explicit_host="root@10.0.0.9",
            selected_record=record,
            existing={},
            ssh_opts="",
        )

        self.assertEqual(target.host, "root@10.0.0.9")
        self.assertEqual(target.source, "explicit_host")

    def test_selected_record_syap_describes_target_only_when_host_came_from_record(self) -> None:
        record = BonjourResolvedService(
            "Express",
            "express.local.",
            "_airport._tcp.local.",
            ipv4=["10.0.0.40"],
            properties={"syAP": "115"},
        )

        from_record = resolve_configure_target(explicit_host="", selected_record=record, existing={}, ssh_opts="")
        typed = resolve_configure_target(explicit_host="root@10.0.0.9", selected_record=record, existing={}, ssh_opts="")
        saved = resolve_configure_target(explicit_host="", selected_record=None, existing={"TC_HOST": "root@10.0.0.2"}, ssh_opts="")

        self.assertEqual(from_record.source, "selected_record")
        self.assertEqual(from_record.selected_record_airport_syap, "115")
        self.assertEqual(typed.source, "explicit_host")
        self.assertEqual(typed.discovered_airport_syap, "115")
        self.assertIsNone(typed.selected_record_airport_syap)
        self.assertEqual(saved.source, "existing_config")
        self.assertIsNone(saved.selected_record_airport_syap)
        # ACP probe telemetry gets the record only when it describes the target.
        self.assertIs(from_record.target_record, from_record.selected_record)
        self.assertIsNotNone(from_record.target_record)
        self.assertIsNone(typed.target_record)
        self.assertIsNone(saved.target_record)

    def test_jsonable_selected_record_is_parsed_for_resolution(self) -> None:
        record = bonjour_record_from_selected_record({
            "name": "Office",
            "hostname": "office.local.",
            "service_type": "_airport._tcp.local.",
            "port": 5009,
            "ipv4": ["10.0.0.80"],
            "properties": {"syAP": "119"},
            "fullname": "Office._airport._tcp.local.",
        })

        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.fullname, "Office._airport._tcp.local.")
        self.assertEqual(record.properties["syAP"], "119")


if __name__ == "__main__":
    unittest.main()
