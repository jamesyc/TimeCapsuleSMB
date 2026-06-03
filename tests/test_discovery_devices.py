from __future__ import annotations

import unittest

from timecapsulesmb.discovery.bonjour import BonjourResolvedService
from timecapsulesmb.discovery.devices import device_candidate_to_jsonable, device_candidates_from_records


class DiscoveryDeviceCandidateTests(unittest.TestCase):
    def test_builds_selectable_devices_from_airport_records_and_prefers_lan_ipv4(self) -> None:
        records = [
            self.record("James", "_adisk._tcp.local.", ["169.254.155.207", "192.168.1.217"]),
            self.record("James", "_airport._tcp.local.", ["169.254.155.207", "192.168.1.217"]),
            self.record("James", "_device-info._tcp.local.", ["169.254.155.207", "192.168.1.217"]),
            self.record("James", "_smb._tcp.local.", ["169.254.155.207", "192.168.1.217"]),
            self.record("Office", "_adisk._tcp.local.", ["10.0.0.9"]),
            self.record("Office", "_airport._tcp.local.", ["10.0.0.9"]),
            self.record("Office", "_device-info._tcp.local.", ["10.0.0.9"]),
            self.record("Office", "_smb._tcp.local.", ["10.0.0.9"]),
        ]

        devices = device_candidates_from_records(records)

        self.assertEqual([device.name for device in devices], ["James", "Office"])
        self.assertEqual(devices[0].host, "192.168.1.217")
        self.assertEqual(devices[0].ssh_host, "root@192.168.1.217")
        self.assertEqual(devices[0].preferred_ipv4, "192.168.1.217")
        self.assertFalse(devices[0].link_local_only)
        self.assertEqual(devices[0].selected_record.service_type, "_airport._tcp.local.")

    def test_ignores_non_airport_records_even_when_they_have_time_capsule_metadata(self) -> None:
        records = [
            self.record("SMB Only", "_smb._tcp.local.", ["10.0.0.2"], syap="119"),
            self.record("Device Info", "_device-info._tcp.local.", ["10.0.0.2"], syap="119"),
        ]

        self.assertEqual(device_candidates_from_records(records), [])

    def test_cli_can_build_candidates_from_already_filtered_mock_records(self) -> None:
        records = [
            self.record("SMB Only", "_smb._tcp.local.", ["10.0.0.2"], syap="", model=""),
        ]

        devices = device_candidates_from_records(records, airport_only=False)

        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0].host, "10.0.0.2")
        self.assertEqual(devices[0].selected_record.service_type, "_smb._tcp.local.")

    def test_dedupes_repeated_airport_records_and_keeps_best_address_candidate(self) -> None:
        records = [
            self.record("Office", "_airport._tcp.local.", ["169.254.44.9"], hostname="office.local."),
            self.record("Office", "_airport._tcp.local.", ["169.254.44.9", "10.0.0.2"], hostname="office.local."),
        ]

        devices = device_candidates_from_records(records)

        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0].host, "10.0.0.2")
        self.assertEqual(devices[0].addresses, ("169.254.44.9", "10.0.0.2"))

    def test_link_local_only_candidate_is_explicit_and_does_not_produce_ssh_host(self) -> None:
        devices = device_candidates_from_records([
            self.record("Office", "_airport._tcp.local.", ["169.254.44.9"], hostname="office.local.")
        ])

        device = devices[0]
        self.assertEqual(device.host, "office.local.")
        self.assertIsNone(device.ssh_host)
        self.assertIsNone(device.preferred_ipv4)
        self.assertTrue(device.link_local_only)

    def test_json_payload_keeps_raw_selected_record_for_configure(self) -> None:
        record = self.record("Office", "_airport._tcp.local.", ["10.0.0.2"], syap="119", model="TimeCapsule8,119")
        device = device_candidates_from_records([record])[0]

        payload = device_candidate_to_jsonable(device)

        self.assertEqual(payload["host"], "10.0.0.2")
        self.assertEqual(payload["ssh_host"], "root@10.0.0.2")
        self.assertEqual(payload["syap"], "119")
        self.assertEqual(payload["model"], "TimeCapsule8,119")
        self.assertEqual(payload["selected_record"]["fullname"], "Office._airport._tcp.local.")
        self.assertEqual(payload["selected_record"]["ipv4"], ["10.0.0.2"])

    def test_derives_full_model_identifier_from_syap_when_model_is_missing(self) -> None:
        record = self.record("Office", "_airport._tcp.local.", ["10.0.0.2"], syap="116", model="")

        device = device_candidates_from_records([record])[0]

        self.assertEqual(device.syap, "116")
        self.assertEqual(device.model, "TimeCapsule6,116")

    def test_derives_full_model_identifier_from_syap_when_model_is_generic(self) -> None:
        record = self.record("Office", "_airport._tcp.local.", ["10.0.0.2"], syap="119", model="TimeCapsule")

        device = device_candidates_from_records([record])[0]

        self.assertEqual(device.model, "TimeCapsule8,119")

    def test_keeps_explicit_model_when_syap_is_unknown(self) -> None:
        record = self.record("Office", "_airport._tcp.local.", ["10.0.0.2"], syap="999", model="MysteryModel")

        device = device_candidates_from_records([record])[0]

        self.assertEqual(device.syap, "999")
        self.assertEqual(device.model, "MysteryModel")

    def record(
        self,
        name: str,
        service_type: str,
        ipv4: list[str],
        *,
        hostname: str | None = None,
        syap: str = "119",
        model: str = "TimeCapsule8,119",
    ) -> BonjourResolvedService:
        return BonjourResolvedService(
            name=name,
            hostname=hostname or f"{name.lower()}.local.",
            service_type=service_type,
            port=5009,
            ipv4=ipv4,
            properties={"syAP": syap, "model": model},
            fullname=f"{name}.{service_type}",
        )
