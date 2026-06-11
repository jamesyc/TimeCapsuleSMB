from __future__ import annotations

import unittest

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
