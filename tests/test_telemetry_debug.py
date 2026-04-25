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
    RemoteInterfaceCandidate,
    RemoteInterfaceCandidatesProbeResult,
)
from timecapsulesmb.discovery.bonjour import Discovered
from timecapsulesmb.telemetry.debug import (
    DEBUG_FIELD_BLACKLIST,
    DEBUG_VALUE_BLACKLIST,
    debug_summary,
    render_command_debug_lines,
    render_debug_mapping,
    render_debug_value,
)
from timecapsulesmb.transport.ssh import SshConnection


class TelemetryDebugTests(unittest.TestCase):
    def test_debug_summary_for_discovered_record_keeps_relevant_bonjour_fields(self) -> None:
        record = Discovered(
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
                "syAP": "119",
                "model": "TimeCapsule8,119",
            },
        )

    def test_debug_summary_for_interface_candidates_is_compact(self) -> None:
        result = RemoteInterfaceCandidatesProbeResult(
            candidates=(
                RemoteInterfaceCandidate("bridge0", ("192.168.1.217",), up=True, active=True, loopback=False),
                RemoteInterfaceCandidate("lo0", ("127.0.0.1",), up=True, active=True, loopback=True),
            ),
            preferred_iface="bridge0",
            detail="ok",
        )

        self.assertEqual(
            debug_summary(result),
            [
                {"name": "bridge0", "ipv4": ["192.168.1.217"], "loopback": False},
                {"name": "lo0", "ipv4": ["127.0.0.1"], "loopback": True},
            ],
        )

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

    def test_render_debug_mapping_applies_password_and_duplicate_blacklists(self) -> None:
        lines = render_debug_mapping(
            {
                "TC_HOST": "root@192.168.1.217",
                "TC_PASSWORD": "secret",
                "TC_CONFIGURE_ID": "config-id",
                "TC_SHARE_USE_DISK_ROOT": "true",
            },
            blacklist=DEBUG_VALUE_BLACKLIST,
        )

        self.assertEqual(lines, ["TC_HOST=root@192.168.1.217", "TC_SHARE_USE_DISK_ROOT=true"])

        lines = render_debug_mapping(
            {
                "device_os_version": "NetBSD 6.0 (earmv4)",
                "device_family": "netbsd6_samba4",
                "selected_net_iface": "bridge0",
            },
            blacklist=DEBUG_FIELD_BLACKLIST,
        )

        self.assertEqual(lines, ["selected_net_iface=bridge0"])

    def test_render_debug_value_summarizes_registered_objects(self) -> None:
        record = Discovered(
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

    def test_render_command_debug_lines_combines_context_sources(self) -> None:
        state = ProbedDeviceState(
            probe_result=ProbeResult(
                ssh_port_reachable=True,
                ssh_authenticated=False,
                error="SSH authentication failed.",
                os_name="",
                os_release="",
                arch="",
                elf_endianness="unknown",
            ),
            compatibility=None,
        )
        lines = render_command_debug_lines(
            command_name="configure",
            stage="ssh_probe",
            connection=SshConnection("root@192.168.1.217", "secret", "-o ProxyJump=bastion"),
            values={
                "TC_HOST": "root@192.168.1.101",
                "TC_PASSWORD": "secret",
                "TC_SSH_OPTS": "-o ProxyJump=old",
                "TC_SHARE_USE_DISK_ROOT": "true",
                "TC_MDNS_DEVICE_MODEL": "TimeCapsule8,119",
            },
            preflight_error="preflight failed",
            finish_fields={
                "device_family": "netbsd6_samba4",
                "reboot_was_attempted": True,
                "custom_finish": "kept",
            },
            probe_state=state,
            debug_fields={
                "device_model": "TimeCapsule8,119",
                "selected_net_iface": "bridge0",
            },
        )

        self.assertEqual(lines[0:3], ["Debug context:", "command=configure", "stage=ssh_probe"])
        self.assertIn("host=root@192.168.1.217", lines)
        self.assertIn("ssh_opts=-o ProxyJump=bastion", lines)
        self.assertIn("TC_HOST=root@192.168.1.101", lines)
        self.assertIn("TC_SHARE_USE_DISK_ROOT=true", lines)
        self.assertIn("preflight_error=preflight failed", lines)
        self.assertIn("custom_finish=kept", lines)
        self.assertIn("probe_ssh_port_reachable=true", lines)
        self.assertIn("probe_ssh_authenticated=false", lines)
        self.assertIn("probe_error=SSH authentication failed.", lines)
        self.assertIn("selected_net_iface=bridge0", lines)
        self.assertNotIn("TC_PASSWORD=secret", lines)
        self.assertNotIn("TC_MDNS_DEVICE_MODEL=TimeCapsule8,119", lines)
        self.assertNotIn("device_family=netbsd6_samba4", lines)
        self.assertNotIn("reboot_was_attempted=true", lines)
        self.assertNotIn("device_model=TimeCapsule8,119", lines)

    def test_render_command_debug_lines_uses_values_when_connection_is_missing(self) -> None:
        lines = render_command_debug_lines(
            command_name="doctor",
            stage=None,
            connection=None,
            values={"TC_HOST": "root@10.0.0.1", "TC_SSH_OPTS": "-o ConnectTimeout=5"},
            preflight_error=None,
            finish_fields={},
            probe_state=None,
            debug_fields={},
        )

        self.assertIn("host=root@10.0.0.1", lines)
        self.assertIn("ssh_opts=-o ConnectTimeout=5", lines)


if __name__ == "__main__":
    unittest.main()
