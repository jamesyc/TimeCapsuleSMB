"""Finding an AirPort by its AirPort MAC after it moved to another address."""
from __future__ import annotations

import unittest
from unittest import mock

from timecapsulesmb.device.probe import AirportAcpReading
from timecapsulesmb.discovery.bonjour import AIRPORT_SERVICE
from timecapsulesmb.discovery.models import BonjourDiscoverySnapshot, BonjourResolvedService
from timecapsulesmb.services.locate import LOCATE_BROWSE_SECONDS, LocateResult, locate_airport

from tests.reboot_support import DEVICE_AIRPORT_MAC, RecordingCallbacks, acp_reading

OTHER_MAC = "02:00:00:00:00:02"


def airport_record(*addresses: str, mac: str = DEVICE_AIRPORT_MAC, name: str = "Office Capsule") -> BonjourResolvedService:
    return BonjourResolvedService(
        name=name,
        hostname=f"{name.replace(' ', '-')}.local",
        service_type="_airport._tcp.local.",
        port=5009,
        ipv4=[address for address in addresses if ":" not in address],
        ipv6=[address for address in addresses if ":" in address],
        properties={"syAP": "119", "waMA": mac.upper().replace(":", "-")},
        fullname=f"{name}._airport._tcp.local.",
    )


class LocateAirportTests(unittest.TestCase):
    def locate(self, records, readings=None, *, current_host="root@192.168.1.218", **options):
        """Run locate_airport over a browse that finds `records`; `readings`
        maps each address to its network ACP answer (default: this device)."""
        readings = readings or {}
        browse = mock.Mock(return_value=(BonjourDiscoverySnapshot([], list(records)), None))
        read = mock.Mock(side_effect=lambda address, _password, *, attempts: readings.get(address, acp_reading(True)))
        recorder = RecordingCallbacks()
        with mock.patch("timecapsulesmb.device.probe.read_airport_acp", read):
            result = locate_airport(
                DEVICE_AIRPORT_MAC.upper(),
                "pw",
                current_host=current_host,
                trigger="acp_unreachable",
                callbacks=recorder.callbacks(),
                browse=browse,
                **options,
            )
        browse.assert_called_once_with(AIRPORT_SERVICE, timeout=LOCATE_BROWSE_SECONDS)
        self.read_attempts = [call.kwargs["attempts"] for call in read.call_args_list]
        return result, [call.args[0] for call in read.call_args_list], recorder.measurement("host_follow")

    def test_a_device_at_a_new_lan_address_is_found_there(self) -> None:
        result, reads, follow = self.locate([airport_record("192.168.1.40")])

        self.assertEqual(result, LocateResult("found", host="root@192.168.1.40", address="192.168.1.40"))
        self.assertEqual(reads, ["192.168.1.40"])
        self.assertEqual(
            {key: value for key, value in follow.items() if key != "browse_sec"},
            {
                "trigger": "acp_unreachable", "result": "found", "candidates": 1, "airports_seen": 1,
                "from_scope": "private", "to_scope": "private",
            },
        )

    def test_another_airport_is_never_read(self) -> None:
        result, reads, follow = self.locate([airport_record("192.168.1.10", mac=OTHER_MAC, name="Other")])

        self.assertEqual(result, LocateResult("not_found"))
        self.assertEqual(reads, [])
        # The browse worked: it saw another AirPort, just not this one.
        self.assertEqual((follow["candidates"], follow["airports_seen"]), (0, 1))

    def test_a_device_still_listing_the_current_address_has_not_moved(self) -> None:
        # ACPd is busy or still booting: its fe80 address may answer, but the
        # device is where it was.
        result, reads, follow = self.locate([airport_record("192.168.1.218", "fe80::82ea:96ff:fee6:5868%en0")])

        self.assertEqual(result, LocateResult("not_moved"))
        self.assertEqual(reads, [])
        self.assertEqual(follow["result"], "not_moved")

    def test_a_cache_holding_the_old_address_beside_the_new_one_does_not_block_the_follow(self) -> None:
        # Two observations of the device merged into one record: the cache on
        # one interface kept .218, the new boot announced .40 on another.
        result, reads, follow = self.locate([airport_record("192.168.1.218", "192.168.1.40", "fe80::82ea:96ff:fee6:5868%en0")])

        self.assertEqual(result, LocateResult("found", host="root@192.168.1.40", address="192.168.1.40"))
        # Neither the current address nor the device's link-local one is read.
        self.assertEqual(reads, ["192.168.1.40"])
        self.assertEqual(follow["result"], "found")

    def test_a_listed_current_address_whose_other_addresses_do_not_answer_has_not_moved(self) -> None:
        result, reads, _follow = self.locate(
            [airport_record("192.168.1.218", "192.168.1.40")], {"192.168.1.40": acp_reading(None)},
        )

        self.assertEqual(result, LocateResult("not_moved"))
        self.assertEqual(reads, ["192.168.1.40"])

    def test_a_saved_hostname_is_taken_as_current_without_a_browse(self) -> None:
        recorder = RecordingCallbacks()
        browse = mock.Mock()
        read = mock.Mock()
        with mock.patch("timecapsulesmb.device.probe.read_airport_acp", read):
            result = locate_airport(
                DEVICE_AIRPORT_MAC,
                "pw",
                current_host="root@Office-Capsule.local",
                trigger="reboot_wait",
                callbacks=recorder.callbacks(),
                browse=browse,
            )

        self.assertEqual(result, LocateResult("not_moved"))
        browse.assert_not_called()
        read.assert_not_called()
        self.assertEqual(
            recorder.measurement("host_follow"),
            {"trigger": "reboot_wait", "result": "not_moved", "saved_hostname": True},
        )

    def test_scoped_link_local_and_ipv4_saved_addresses_are_compared(self) -> None:
        for current in ("root@192.168.1.218", "root@fe80::82ea:96ff:fee6:5868%en0"):
            with self.subTest(current=current):
                _result, _reads, follow = self.locate([airport_record("192.168.1.40")], current_host=current)
                self.assertNotIn("saved_hostname", follow)

    def test_a_cached_address_another_device_now_answers_at_is_not_followed(self) -> None:
        # The Bonjour cache still has the device at .40; another AirPort with
        # the same admin password took that address.
        result, reads, _follow = self.locate(
            [airport_record("192.168.1.40")],
            {"192.168.1.40": AirportAcpReading(password_matches=True, airport_mac=OTHER_MAC)},
        )

        self.assertEqual(result, LocateResult("not_found"))
        self.assertEqual(reads, ["192.168.1.40"])

    def test_the_lan_address_is_tried_before_link_local_ipv6(self) -> None:
        result, reads, follow = self.locate(
            [airport_record("192.168.2.40", "fe80::82ea:96ff:fee6:5868%en0", "169.254.155.207")],
            {"192.168.2.40": acp_reading(None)},
        )

        self.assertEqual(result.host, "root@fe80::82ea:96ff:fee6:5868%en0")
        self.assertEqual(reads, ["192.168.2.40", "fe80::82ea:96ff:fee6:5868%en0"])
        self.assertEqual(follow["to_scope"], "link_local")

    def test_a_candidate_gets_a_second_read_unless_the_caller_asks_for_one(self) -> None:
        # A device that is up can fail one read and answer the next; the reboot
        # wait reads once, as it looks again 30 s later.
        for options, attempts in (({}, 2), ({"attempts": 1}, 1)):
            with self.subTest(options=options):
                self.locate([airport_record("192.168.1.40", "192.168.1.41")], {"192.168.1.40": acp_reading(None)}, **options)
                self.assertEqual(self.read_attempts, [attempts, attempts])

    def test_a_device_that_rejects_the_password_is_reported_not_followed(self) -> None:
        # A device reset to factory settings comes back with another password.
        result, _reads, follow = self.locate([airport_record("10.0.1.1")], {"10.0.1.1": acp_reading(False)})

        self.assertEqual(result, LocateResult("password_rejected", address="10.0.1.1"))
        self.assertEqual(follow["result"], "password_rejected")

    def test_no_device_on_the_network_is_not_found(self) -> None:
        result, reads, follow = self.locate([])

        self.assertEqual(result, LocateResult("not_found"))
        self.assertEqual(reads, [])
        self.assertNotIn("to_scope", follow)
        self.assertEqual(follow["airports_seen"], 0)


if __name__ == "__main__":
    unittest.main()
