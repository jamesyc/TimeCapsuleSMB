"""Generate reviewed provider-to-Swift discovery payloads from actual wire decoders."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest import mock

from tests.fixtures.bonjour import info, records, native_txt_output
from timecapsulesmb.app.contracts import discover_payload
from timecapsulesmb.app.ops.discovery import snapshot_payload
from timecapsulesmb.discovery import native_dns_sd, zeroconf_backend
from timecapsulesmb.discovery.models import BonjourDiscoverySnapshot, BonjourServiceInstance

FIXTURE_PATH = Path(__file__).resolve().parents[2] / "macos/TimeCapsuleSMB/Tests/TimeCapsuleSMBAppTests/Fixtures/bonjour_payloads.json"


def scenarios():
    base = records()[0]
    return [
        ("dual_stack", [base], ["192.0.2.10"]),
        ("ipv6_only", [{**base, "ipv4": [], "ipv6": ["fd00::20"]}], ["fd00::20"]),
        ("hostname_only", [{**base, "hostname": "hostname-only.local", "ipv4": [], "ipv6": []}], ["hostname-only.local"]),
        ("unsupported", [{**base, "name": "Express", "properties": {"syAP": "115"}}], ["192.0.2.10"]),
        ("unknown_model", [{**base, "name": "Unknown", "properties": {}}], ["192.0.2.10"]),
        ("escaped_txt", [{**base, "name": r"Bob's.Café\123", "properties": {
            "syAP": "116", "label": "Café Bob's \"Capsule\" & \\123", "empty": "",
        }}], ["192.0.2.10"]),
        ("equal_fullnames", [
            {**base, "hostname": "first.local", "interface_index": 14},
            {**base, "hostname": "second.local", "ipv4": ["192.0.2.20"], "ipv6": ["fd00::20"], "interface_index": 15, "properties": {"waMA": "00:11:22:33:44:66,syAP=116"}},
        ], ["192.0.2.10", "192.0.2.20"]),
        ("equal_hostnames", [
            {**base, "interface_index": 14},
            {**base, "ipv4": ["192.0.2.20"], "ipv6": ["fd00::20"], "interface_index": 15, "properties": {"waMA": "00:11:22:33:44:66,syAP=116"}},
        ], ["192.0.2.10", "192.0.2.20"]),
        ("distinct_fullnames_shared_hostname", [
            {**base, "name": "First", "interface_index": 14},
            {**base, "name": "Second", "ipv4": ["192.0.2.20"], "ipv6": ["fd00::20"], "interface_index": 15, "properties": {"waMA": "00:11:22:33:44:66,syAP=116"}},
        ], ["192.0.2.10", "192.0.2.20"]),
        ("multiple_interfaces", [base, {**base, "interface_index": 15}], ["192.0.2.10"]),
        ("no_mac_equal_names", [
            {**base, "properties": {"syAP": "116"}},
            {**base, "properties": {"syAP": "116"}, "interface_index": 15, "ipv4": ["192.0.2.20"], "ipv6": []},
        ], ["192.0.2.10", "192.0.2.20"]),
        ("empty", [], []),
    ]


def wire_record(record, provider):
    if provider == "zeroconf":
        return zeroconf_backend.resolved_service_from_info(record["service_type"], info(record))
    name, stype, host, index = (record[k] for k in ("name", "service_type", "hostname", "interface_index"))
    lookup = f"10:20:00 {name}.{stype} can be reached at {host}.:{record['port']} (interface {index})\n"
    lookup += native_txt_output(record["properties"]) + "\n"
    addresses = "".join(f"10:20:00 Add 2 {index} {host}. {address} 120\n" for address in record["ipv4"] + record["ipv6"])
    instance = BonjourServiceInstance(stype, name, f"{name}.{stype}", index)
    with mock.patch.object(native_dns_sd, "_run_dns_sd_command", side_effect=[
        (lookup, "", 0, False, ""), (addresses, "", -15, True, ""),
    ]):
        return native_dns_sd.resolve_service_instance_detailed(instance, 3000)[0]


def build():
    output = []
    for name, observations, expected_hosts in scenarios():
        paired = []
        for provider in ("dns-sd", "zeroconf"):
            decoded = [wire_record(r, provider) for r in observations]
            assert all(r is not None for r in decoded)
            instances = [BonjourServiceInstance(r.service_type, r.name, r.fullname, r.interface_index) for r in decoded]
            payload = discover_payload(snapshot_payload(BonjourDiscoverySnapshot(instances, decoded)))
            # Expectations are reviewed domain behavior, not copied from freshly generated output.
            assert payload["counts"]["devices"] == len(expected_hosts)
            assert [d["host"] for d in payload["devices"]] == expected_hosts
            if name == "dual_stack":
                assert payload["devices"][0]["model"] == "TimeCapsule6,116"
                assert payload["devices"][0]["ipv6"] == ["fd00::10"]
            if name == "unsupported": assert payload["devices"][0]["supported_model"] is False
            if name == "unknown_model": assert payload["devices"][0]["supported_model"] is None
            if name in {"equal_fullnames", "equal_hostnames", "distinct_fullnames_shared_hostname"}:
                assert len({d["id"] for d in payload["devices"]}) == 2
                assert len({d["airport_mac"] for d in payload["devices"]}) == 2
            paired.append(payload)
        assert paired[0] == paired[1], name
        output.append(dict(case=name, expected_hosts=expected_hosts,
                           expected_device_count=len(expected_hosts), payload=paired[0]))
    return output


def render():
    return json.dumps(build(), indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    text = render()
    if args.write:
        FIXTURE_PATH.write_text(text)
    else:
        assert FIXTURE_PATH.read_text() == text, "Regenerate reviewed Bonjour fixtures with --write"


if __name__ == "__main__": main()
