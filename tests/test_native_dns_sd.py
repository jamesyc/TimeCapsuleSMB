from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from timecapsulesmb.discovery.native_dns_sd import (
    browse_native_dns_sd,
    discover_native_dns_sd_snapshot_detailed,
    resolve_native_dns_sd_service_instance,
)


class FakeDnsSdProc:
    def __init__(self, stdout: str, stderr: str = "") -> None:
        self.stdout_text = stdout
        self.stderr_text = stderr
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def communicate(self, timeout: float | None = None) -> tuple[str, str]:
        return self.stdout_text, self.stderr_text


class NativeDnsSdTests(unittest.TestCase):
    def test_browse_native_dns_sd_omits_diagnostic_on_non_macos(self) -> None:
        with mock.patch("timecapsulesmb.discovery.native_dns_sd.command_exists") as command_exists:
            self.assertIsNone(browse_native_dns_sd(["_smb._tcp"], platform_name="Linux"))
        command_exists.assert_not_called()

    def test_browse_native_dns_sd_omits_diagnostic_when_dns_sd_is_missing(self) -> None:
        with mock.patch("timecapsulesmb.discovery.native_dns_sd.command_exists", return_value=False):
            self.assertIsNone(browse_native_dns_sd(["_smb._tcp"], platform_name="Darwin"))

    def test_browse_native_dns_sd_parses_macos_browse_output_and_terminates_browser(self) -> None:
        proc = FakeDnsSdProc(
            """
Browsing for _smb._tcp.local
DATE: ---Thu 30 Apr 2026---
10:20:07.123  ...STARTING...
Timestamp     A/R    Flags  if Domain               Service Type         Instance Name
10:20:07.456  Add        3  14 local.               _smb._tcp.           Time Capsule
"""
        )

        with mock.patch("timecapsulesmb.discovery.native_dns_sd.command_exists", return_value=True):
            with mock.patch("timecapsulesmb.discovery.native_dns_sd.subprocess.Popen", return_value=proc) as popen_mock:
                with mock.patch("timecapsulesmb.discovery.native_dns_sd.time.monotonic", side_effect=[0.0, 0.0, 0.25]):
                    diagnostics = browse_native_dns_sd(["_smb._tcp.local."], timeout_sec=0, platform_name="Darwin")

        self.assertIsNotNone(diagnostics)
        assert diagnostics is not None
        popen_mock.assert_called_once_with(
            ["dns-sd", "-B", "_smb._tcp", "local"],
            stdout=mock.ANY,
            stderr=mock.ANY,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        self.assertTrue(proc.terminated)
        self.assertEqual(diagnostics.status, "ok")
        self.assertEqual(diagnostics.elapsed_sec, 0.25)
        browse = diagnostics.browses[0]
        self.assertEqual(browse.service_type, "_smb._tcp")
        self.assertEqual(browse.parse_error_count, 0)
        self.assertEqual(len(browse.events), 1)
        self.assertEqual(browse.events[0].action, "Add")
        self.assertEqual(browse.events[0].interface_index, 14)
        self.assertEqual(browse.events[0].name, "Time Capsule")

    def test_resolve_native_dns_sd_service_instance_parses_target_and_addresses(self) -> None:
        lookup_proc = FakeDnsSdProc(
            """
Lookup Time Capsule._smb._tcp.local
DATE: ---Thu 30 Apr 2026---
10:20:07.456  Time Capsule._smb._tcp.local. can be reached at time-capsule.local.:445 (interface 14)
"""
        )
        address_proc = FakeDnsSdProc(
            """
DATE: ---Thu 30 Apr 2026---
Timestamp     A/R    Flags if Hostname                               Address                                      TTL
10:20:07.789  Add        2 14 time-capsule.local.                    10.0.0.2                                     120
"""
        )

        with mock.patch("timecapsulesmb.discovery.native_dns_sd.subprocess.Popen", side_effect=[lookup_proc, address_proc]) as popen_mock:
            record, diagnostics = resolve_native_dns_sd_service_instance(
                "_smb._tcp.local.",
                "Time Capsule",
                timeout_sec=0,
                family="ipv4",
            )

        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.name, "Time Capsule")
        self.assertEqual(record.hostname, "time-capsule.local")
        self.assertEqual(record.service_type, "_smb._tcp.local.")
        self.assertEqual(record.port, 445)
        self.assertEqual(record.ipv4, ["10.0.0.2"])
        self.assertEqual(record.fullname, "Time Capsule._smb._tcp.local.")
        self.assertEqual(diagnostics.interface_index, 14)
        self.assertEqual(diagnostics.addresses[0].addresses, ["10.0.0.2"])
        self.assertEqual(
            [call.args[0] for call in popen_mock.call_args_list],
            [
                ["dns-sd", "-L", "Time Capsule", "_smb._tcp", "local"],
                ["dns-sd", "-G", "v4", "time-capsule.local"],
            ],
        )

    def test_resolve_native_dns_sd_service_instance_parses_txt_properties(self) -> None:
        lookup_proc = FakeDnsSdProc(
            r"""
10:20:07.456  Time Capsule._adisk._tcp.local. can be reached at time-capsule.local.:9 (interface 14)
 sys=waMA=80:EA:96:E6:58:68,adVF=0x1010
 dk2=adVF=0x83,adVN=Time\ Machine,adVU=117b94b1-3cf3-5600-b192-cc0dd671b852
"""
        )
        address_proc = FakeDnsSdProc("10:20:07.789  Add 2 14 time-capsule.local. 10.0.0.2 120\n")

        with mock.patch(
            "timecapsulesmb.discovery.native_dns_sd.subprocess.Popen",
            side_effect=[lookup_proc, address_proc],
        ):
            record, _diagnostics = resolve_native_dns_sd_service_instance(
                "_adisk._tcp.local.",
                "Time Capsule",
                timeout_sec=0,
                family="ipv4",
            )

        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.properties["sys"], "waMA=80:EA:96:E6:58:68,adVF=0x1010")
        self.assertEqual(
            record.properties["dk2"],
            "adVF=0x83,adVN=Time Machine,adVU=117b94b1-3cf3-5600-b192-cc0dd671b852",
        )

    def test_resolve_native_dns_sd_service_instance_reports_lookup_miss_without_address_lookup(self) -> None:
        lookup_proc = FakeDnsSdProc(
            """
Lookup Time Capsule._smb._tcp.local
DATE: ---Thu 30 Apr 2026---
"""
        )

        with mock.patch("timecapsulesmb.discovery.native_dns_sd.subprocess.Popen", return_value=lookup_proc) as popen_mock:
            record, diagnostics = resolve_native_dns_sd_service_instance(
                "_smb._tcp.local.",
                "Time Capsule",
                timeout_sec=0,
                family="ipv4",
            )

        self.assertIsNone(record)
        self.assertEqual(diagnostics.error, "dns-sd lookup did not resolve a service target")
        self.assertEqual(diagnostics.hostname, "")
        self.assertEqual(diagnostics.port, 0)
        self.assertEqual(len(diagnostics.addresses), 0)
        popen_mock.assert_called_once()

    def test_resolve_native_dns_sd_service_instance_keeps_service_record_when_address_lookup_fails(self) -> None:
        lookup_proc = FakeDnsSdProc(
            """
10:20:07.456  Time Capsule._smb._tcp.local. can be reached at time-capsule.local.:445 (interface 14)
"""
        )

        with mock.patch(
            "timecapsulesmb.discovery.native_dns_sd.subprocess.Popen",
            side_effect=[lookup_proc, OSError("dns-sd unavailable")],
        ):
            record, diagnostics = resolve_native_dns_sd_service_instance(
                "_smb._tcp.local.",
                "Time Capsule",
                timeout_sec=0,
                family="ipv4",
            )

        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.hostname, "time-capsule.local")
        self.assertEqual(record.port, 445)
        self.assertEqual(record.ipv4, [])
        self.assertEqual(diagnostics.addresses[0].family, "v4")
        self.assertIn("OSError", diagnostics.addresses[0].error)

    def test_discover_native_dns_sd_snapshot_builds_bonjour_records_from_browse_and_lookup(self) -> None:
        browse_proc = FakeDnsSdProc(
            """
Browsing for _smb._tcp.local
10:20:07.456  Add        3  14 local.               _smb._tcp.           Time Capsule
"""
        )
        lookup_proc = FakeDnsSdProc(
            """
10:20:07.457  Time Capsule._smb._tcp.local. can be reached at time-capsule.local.:445 (interface 14)
"""
        )
        address_proc = FakeDnsSdProc("10:20:07.789  Add        2 14 time-capsule.local. 10.0.0.2 120\n")

        with mock.patch("timecapsulesmb.discovery.native_dns_sd.command_exists", return_value=True):
            with mock.patch("timecapsulesmb.discovery.native_dns_sd.subprocess.Popen", side_effect=[browse_proc, lookup_proc, address_proc]):
                result = discover_native_dns_sd_snapshot_detailed(
                    "_smb",
                    timeout_sec=0,
                    family="ipv4",
                    platform_name="Darwin",
                )

        self.assertIsNotNone(result)
        assert result is not None
        snapshot, diagnostics = result
        self.assertEqual(len(snapshot.instances), 1)
        self.assertEqual(snapshot.instances[0].fullname, "Time Capsule._smb._tcp.local.")
        self.assertEqual(len(snapshot.resolved), 1)
        self.assertEqual(snapshot.resolved[0].hostname, "time-capsule.local")
        self.assertEqual(snapshot.resolved[0].ipv4, ["10.0.0.2"])
        self.assertEqual(diagnostics.instance_count, 1)
        self.assertEqual(diagnostics.resolved_count, 1)
        self.assertEqual(len(diagnostics.resolves), 1)

    def test_discover_native_dns_sd_snapshot_deduplicates_duplicate_browse_events(self) -> None:
        browse_proc = FakeDnsSdProc(
            """
Browsing for _smb._tcp.local
10:20:07.456  Add        3  14 local.               _smb._tcp.           Time Capsule
10:20:07.457  Add        2  15 local.               _smb._tcp.           Time Capsule
"""
        )
        lookup_proc = FakeDnsSdProc(
            """
10:20:07.458  Time Capsule._smb._tcp.local. can be reached at time-capsule.local.:445 (interface 14)
"""
        )
        address_proc = FakeDnsSdProc("10:20:07.789  Add        2 14 time-capsule.local. 10.0.0.2 120\n")

        with mock.patch("timecapsulesmb.discovery.native_dns_sd.command_exists", return_value=True):
            with mock.patch("timecapsulesmb.discovery.native_dns_sd.subprocess.Popen", side_effect=[browse_proc, lookup_proc, address_proc]) as popen_mock:
                result = discover_native_dns_sd_snapshot_detailed(
                    "_smb",
                    timeout_sec=0,
                    family="ipv4",
                    platform_name="Darwin",
                )

        self.assertIsNotNone(result)
        assert result is not None
        snapshot, diagnostics = result
        self.assertEqual(len(snapshot.instances), 1)
        self.assertEqual(len(snapshot.resolved), 1)
        self.assertEqual(len(diagnostics.resolves), 1)
        self.assertEqual(len(popen_mock.call_args_list), 3)


if __name__ == "__main__":
    unittest.main()
