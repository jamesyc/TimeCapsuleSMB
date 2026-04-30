from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from timecapsulesmb.discovery.native_dns_sd import browse_native_dns_sd


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


if __name__ == "__main__":
    unittest.main()
