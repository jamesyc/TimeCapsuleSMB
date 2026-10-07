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
        self.assertEqual(device.addresses, ("169.254.44.9",))

    def test_distinct_observations_sharing_hostname_keep_stable_ids(self) -> None:
        first = self.record("First", "_airport._tcp.local.", ["192.0.2.10"], hostname="SHARED.local.")
        second = self.record("Second", "_airport._tcp.local.", ["192.0.2.20"], hostname="shared.local")
        first.interface_index, second.interface_index = 14, 15
        ordinary_ids = {r.name: device_candidates_from_records([r])[0].id for r in (first, second)}
        devices = device_candidates_from_records([first, second])
        self.assertEqual(len(devices), 2)
        self.assertEqual({d.name: d.id for d in devices}, ordinary_ids)
        self.assertTrue(all(device_candidate_to_jsonable(d)["airport_mac"] is None for d in devices))

    def test_duplicate_announcements_and_empty_hostnames_do_not_create_hostname_collisions(self) -> None:
        first = self.record("First", "_airport._tcp.local.", ["192.0.2.10"], hostname="shared.local.")
        duplicate = self.record("First", "_airport._tcp.local.", ["192.0.2.10"], hostname="SHARED.local")
        devices = device_candidates_from_records([first, duplicate])
        self.assertEqual(len(devices), 1)
        self.assertIsNone(devices[0].airport_mac)
        second = self.record("Second", "_airport._tcp.local.", ["192.0.2.20"])
        first.hostname = second.hostname = ""
        devices = device_candidates_from_records([first, second])
        self.assertEqual(len(devices), 2)
        self.assertTrue(all(d.airport_mac is None for d in devices))

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

    def test_json_payload_marks_whether_the_advertised_model_is_supported(self) -> None:
        for syap, expected in (("119", True), ("106", True), ("115", False), ("", None), ("bad", None)):
            with self.subTest(syap=syap):
                record = self.record("Office", "_airport._tcp.local.", ["10.0.0.2"], syap=syap)
                payload = device_candidate_to_jsonable(device_candidates_from_records([record])[0])
                self.assertIs(payload["supported_model"], expected)

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


class ApplianceIdentityTests(unittest.TestCase):
    def record(self, mac, *, scope=14, address="192.0.2.10", port=5009):
        return BonjourResolvedService("Office", "office.local", "_airport._tcp.local.", port=port,
            fullname="Office._airport._tcp.local.", interface_index=scope,
            ipv4=[address], properties={"waMA": mac} if mac is not None else {})

    def test_mac_normalization_and_invalid_values(self):
        from timecapsulesmb.discovery.models import normalize_airport_mac
        for value in (None, "", "00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff", "01:11:22:33:44:55", "00:11:22:33:44", "001122334455", "+2:aa:bb:cc:dd:ee"):
            with self.subTest(value=value):
                self.assertIsNone(normalize_airport_mac(value))
        self.assertEqual(normalize_airport_mac(" 02-AA-bb-CC-dd-EE "), "02:aa:bb:cc:dd:ee")

    def test_same_appliance_on_two_interfaces_is_one_row_with_real_selected_record(self):
        records = [self.record("02:11:22:33:44:55"), self.record("02-11-22-33-44-55", scope=15, address="192.0.2.20")]
        devices = device_candidates_from_records(records)
        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0].addresses, ("192.0.2.10", "192.0.2.20"))
        self.assertIn(devices[0].selected_record, records)
        self.assertEqual(devices[0].host, devices[0].selected_record.preferred_connection_host())
        self.assertEqual(device_candidates_from_records(reversed(records)), devices)

    def test_hardware_id_survives_dhcp_and_peer_appearance(self):
        original = self.record("02:11:22:33:44:55")
        moved = self.record("02:11:22:33:44:55", address="192.0.2.30")
        peer = self.record("02:11:22:33:44:66", address="192.0.2.40")
        identifier = device_candidates_from_records([original])[0].id
        self.assertEqual(device_candidates_from_records([moved])[0].id, identifier)
        self.assertIn(identifier, [d.id for d in device_candidates_from_records([moved, peer])])
        self.assertEqual(len(device_candidates_from_records([original, peer])), 2)

    def test_unknown_identity_does_not_join_names_across_interfaces(self):
        first, second = self.record(None), self.record(None, scope=15, address="192.0.2.20")
        before = device_candidates_from_records([first])[0].id
        devices = device_candidates_from_records([first, second])
        self.assertEqual(len(devices), 2)
        self.assertIn(before, [d.id for d in devices])

    def test_conflicting_service_observations_survive_appliance_grouping(self):
        from timecapsulesmb.discovery.models import BonjourDiscoverySnapshot, _merge_snapshots
        records = [self.record("02:11:22:33:44:55", port=p) for p in (5009, 5010)]
        snapshot = _merge_snapshots([BonjourDiscoverySnapshot([], records)])
        self.assertEqual(len(snapshot.resolved), 2)
        self.assertEqual(len(device_candidates_from_records(snapshot.resolved)), 1)

    def test_snapshot_never_overwrites_conflicting_hardware_or_txt(self):
        from dataclasses import replace
        from timecapsulesmb.discovery.models import BonjourDiscoverySnapshot, _merge_snapshots
        first = self.record("02:11:22:33:44:55")
        other = self.record("02:11:22:33:44:66", address="192.0.2.20")
        changed = replace(first, properties={**first.properties, "syAP": "116"})
        conflicted = replace(first, properties={**first.properties, "syAP": "119"})
        snapshot = _merge_snapshots([BonjourDiscoverySnapshot([], [changed]), BonjourDiscoverySnapshot([], [other, conflicted])])
        self.assertEqual(len(snapshot.resolved), 3)
        self.assertEqual({r.properties.get("syAP") for r in snapshot.resolved}, {None, "116", "119"})
        self.assertTrue(all(len(r.ipv4) == 1 for r in snapshot.resolved))
