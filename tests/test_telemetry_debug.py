from __future__ import annotations

import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from timecapsulesmb.device.compat import DeviceCompatibility
from timecapsulesmb.device.probe import (
    ProbeResult,
    ProbedDeviceState,
)
from timecapsulesmb.discovery.bonjour import (
    BonjourDiscoveryDiagnostics,
    BonjourDiscoverySnapshot,
    BonjourPtrRecordObservation,
    BonjourServiceEvent,
    BonjourServiceInstance,
    BonjourResolvedService,
)
from timecapsulesmb.discovery.native_dns_sd import (
    NativeDnsSdAddressResult,
    NativeDnsSdBrowseResult,
    NativeDnsSdDiscoveryDiagnostics,
    NativeDnsSdDiagnostics,
    NativeDnsSdResolveResult,
    NativeDnsSdServiceEvent,
)
from timecapsulesmb.telemetry.debug import (
    debug_summary,
    render_debug_mapping,
    render_debug_value,
)


class TelemetryDebugTests(unittest.TestCase):
    def test_debug_summary_for_discovered_record_keeps_relevant_bonjour_fields(self) -> None:
        record = BonjourResolvedService(
            name="James's AirPort Time Capsule",
            hostname="Jamess-AirPort-Time-Capsule.local",
            service_type="_airport._tcp.local.",
            ipv4=["192.168.1.217"],
            ipv6=["fe80::1"],
            properties={"syAP": "119", "syVs": "7.9.1", "model": "TimeCapsule8,119"},
        )

        self.assertEqual(
            debug_summary(record),
            {
                "service_type": "_airport._tcp.local.",
                "name": "James's AirPort Time Capsule",
                "hostname": "Jamess-AirPort-Time-Capsule.local",
                "ipv4": ["192.168.1.217"],
                "ipv6": ["fe80::1"],
                "syAP": "119",
                "model": "TimeCapsule8,119",
            },
        )

    def test_debug_summary_for_bonjour_snapshot_is_compact_but_keeps_many_candidates(self) -> None:
        instances = [
            BonjourServiceInstance("_smb._tcp.local.", f"Home {idx}", f"Home {idx}._smb._tcp.local.")
            for idx in range(55)
        ]
        resolved = [
            BonjourResolvedService(
                name=f"Home {idx}",
                hostname=f"home-{idx}.local",
                service_type="_smb._tcp.local.",
                port=445,
                ipv4=[f"10.0.0.{idx}"],
                fullname=f"Home {idx}._smb._tcp.local.",
            )
            for idx in range(55)
        ]
        summary = debug_summary(BonjourDiscoverySnapshot(instances=instances, resolved=resolved))

        self.assertEqual(summary["instance_count"], 55)
        self.assertEqual(summary["resolved_count"], 55)
        self.assertEqual(len(summary["instances"]), 50)
        self.assertEqual(len(summary["resolved"]), 50)
        self.assertEqual(summary["instances"][0]["name"], "Home 0")
        self.assertEqual(summary["resolved"][0]["hostname"], "home-0.local")

    def test_debug_summary_for_bonjour_diagnostics_keeps_counts_and_samples(self) -> None:
        instance = BonjourServiceInstance("_smb._tcp.local.", "Home", "Home._smb._tcp.local.")
        diagnostics = BonjourDiscoveryDiagnostics(
            service="_smb",
            service_types=["_smb._tcp.local."],
            timeout_sec=6.0,
            elapsed_sec=6.125,
            ip_version="V4Only",
            instance_count=1,
            resolved_count=0,
            pending_count=1,
            service_added_count=1,
            service_updated_count=0,
            resolve_attempt_count=2,
            resolve_success_count=0,
            resolve_error_count=1,
            instances=[instance],
            resolved=[],
        )

        summary = debug_summary(diagnostics)

        self.assertEqual(summary["service"], "_smb")
        self.assertEqual(summary["ip_version"], "V4Only")
        self.assertEqual(summary["pending_count"], 1)
        self.assertEqual(summary["resolve_error_count"], 1)
        self.assertEqual(summary["instances"][0]["fullname"], "Home._smb._tcp.local.")

    def test_debug_summary_for_bonjour_diagnostics_keeps_bounded_event_telemetry(self) -> None:
        service_events = [
            BonjourServiceEvent(
                service_type="_smb._tcp.local.",
                state="Added",
                name=f"Home {idx}",
                fullname=f"Home {idx}._smb._tcp.local.",
                elapsed_sec=float(idx),
            )
            for idx in range(55)
        ]
        ptr_records = [
            BonjourPtrRecordObservation(
                service_type="_smb._tcp.local.",
                alias=f"Home {idx}._smb._tcp.local.",
                alias_name=f"Home {idx}",
                ttl=120,
                expired=False,
                old_record_present=False,
                elapsed_sec=float(idx),
            )
            for idx in range(55)
        ]
        diagnostics = BonjourDiscoveryDiagnostics(
            service="_smb",
            service_types=["_smb._tcp.local."],
            timeout_sec=6.0,
            elapsed_sec=6.125,
            ip_version="V4Only",
            instance_count=0,
            resolved_count=0,
            pending_count=0,
            service_added_count=0,
            service_updated_count=0,
            resolve_attempt_count=0,
            resolve_success_count=0,
            resolve_error_count=0,
            zeroconf_version="0.148.0",
            service_events=service_events,
            ptr_records=ptr_records,
            ptr_record_error="listener setup failed",
        )

        summary = debug_summary(diagnostics)

        self.assertEqual(summary["zeroconf_version"], "0.148.0")
        self.assertEqual(summary["zeroconf_interfaces"], "system-default")
        self.assertEqual(summary["service_event_count"], 55)
        self.assertEqual(summary["ptr_record_count"], 55)
        self.assertEqual(len(summary["service_events"]), 50)
        self.assertEqual(len(summary["ptr_records"]), 50)
        self.assertEqual(summary["service_events"][0]["name"], "Home 0")
        self.assertEqual(summary["ptr_records"][0]["alias_name"], "Home 0")
        self.assertEqual(summary["ptr_record_error"], "listener setup failed")

    def test_debug_summary_for_native_dns_sd_diagnostics_is_bounded(self) -> None:
        events = [
            NativeDnsSdServiceEvent(
                service_type="_smb._tcp",
                action="Add",
                interface_index=idx,
                flags="3",
                domain="local.",
                name=f"Device {idx}",
            )
            for idx in range(55)
        ]
        diagnostics = NativeDnsSdDiagnostics(
            timeout_sec=6.0,
            elapsed_sec=6.1,
            status="ok",
            browses=[
                NativeDnsSdBrowseResult(
                    service_type="_smb._tcp",
                    events=events,
                    parse_error_count=2,
                    stderr="",
                    exit_code=-15,
                    terminated_after_timeout=True,
                )
            ],
        )

        summary = debug_summary(diagnostics)

        self.assertEqual(summary["status"], "ok")
        self.assertEqual(summary["browses"][0]["event_count"], 55)
        self.assertEqual(summary["browses"][0]["parse_error_count"], 2)
        self.assertEqual(len(summary["browses"][0]["events"]), 50)
        self.assertEqual(summary["browses"][0]["events"][0]["name"], "Device 0")

    def test_debug_summary_for_native_dns_sd_discovery_diagnostics_keeps_resolution_details(self) -> None:
        diagnostics = NativeDnsSdDiscoveryDiagnostics(
            timeout_sec=6.0,
            elapsed_sec=1.5,
            status="ok",
            service_types=["_smb._tcp.local."],
            ip_version="V4Only",
            instance_count=1,
            resolved_count=1,
            browses=[NativeDnsSdBrowseResult(service_type="_smb._tcp")],
            resolves=[
                NativeDnsSdResolveResult(
                    service_type="_smb._tcp",
                    name="Home",
                    fullname="Home._smb._tcp.local.",
                    hostname="home.local",
                    port=445,
                    interface_index=14,
                    addresses=[NativeDnsSdAddressResult("home.local", "v4", ["10.0.0.2"])],
                )
            ],
        )

        summary = debug_summary(diagnostics)

        self.assertEqual(summary["status"], "ok")
        self.assertEqual(summary["ip_version"], "V4Only")
        self.assertEqual(summary["instance_count"], 1)
        self.assertEqual(summary["resolved_count"], 1)
        self.assertEqual(summary["resolves"][0]["hostname"], "home.local")
        self.assertEqual(summary["resolves"][0]["addresses"][0]["addresses"], ["10.0.0.2"])

    def test_probe_debug_summary_suppresses_first_class_telemetry_fields(self) -> None:
        state = ProbedDeviceState(
            probe_result=ProbeResult(
                ssh_port_reachable=True,
                ssh_authenticated=True,
                error=None,
                os_name="NetBSD",
                os_release="6.0",
                arch="earmv4",
                elf_endianness="little",
            ),
            compatibility=DeviceCompatibility(
                os_name="NetBSD",
                os_release="6.0",
                arch="earmv4",
                elf_endianness="little",
                payload_family="netbsd6_samba4",
                device_generation="gen5",
                supported=True,
                reason_code="supported_netbsd6",
            ),
        )

        self.assertEqual(
            debug_summary(state),
            {
                "probe_ssh_port_reachable": True,
                "probe_ssh_authenticated": True,
            },
        )

    def test_unsupported_probe_debug_summary_includes_reason(self) -> None:
        state = ProbedDeviceState(
            probe_result=ProbeResult(
                ssh_port_reachable=True,
                ssh_authenticated=True,
                error=None,
                os_name="Linux",
                os_release="6.8",
                arch="arm64",
                elf_endianness="little",
            ),
            compatibility=DeviceCompatibility(
                os_name="Linux",
                os_release="6.8",
                arch="arm64",
                elf_endianness="little",
                payload_family=None,
                device_generation="unknown",
                supported=False,
                reason_code="unsupported_os",
            ),
        )

        self.assertEqual(
            debug_summary(state),
            {
                "probe_ssh_port_reachable": True,
                "probe_ssh_authenticated": True,
                "probe_supported": False,
                "probe_reason_code": "unsupported_os",
            },
        )

    def test_render_debug_mapping_applies_supplied_blacklist(self) -> None:
        lines = render_debug_mapping(
            {
                "TC_HOST": "root@192.168.1.217",
                "TC_PASSWORD": "secret",
                "TC_CONFIGURE_ID": "config-id",
                "TC_INTERNAL_SHARE_USE_DISK_ROOT": "true",
            },
            blacklist={"TC_PASSWORD", "TC_CONFIGURE_ID"},
        )

        self.assertEqual(lines, ["TC_HOST=root@192.168.1.217", "TC_INTERNAL_SHARE_USE_DISK_ROOT=true"])

    def test_render_debug_value_summarizes_registered_objects(self) -> None:
        record = BonjourResolvedService(
            name="Time Capsule Samba",
            hostname="timecapsulesamba.local",
            service_type="_smb._tcp.local.",
            ipv4=["192.168.1.72"],
            properties={"model": "TimeCapsule6,116"},
        )

        rendered = render_debug_value(record)

        self.assertIn("service_type:_smb._tcp.local.", rendered)
        self.assertIn("hostname:timecapsulesamba.local", rendered)
        self.assertIn("model:TimeCapsule6,116", rendered)


if __name__ == "__main__":
    unittest.main()
