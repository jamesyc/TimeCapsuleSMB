from __future__ import annotations

import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from timecapsulesmb.telemetry.operation import (
    telemetry_details_from_payload,
    telemetry_options_from_params,
)


LAN_IP = "192.168.1.101"
SSH_TARGET = f"root@{LAN_IP}"
LOCAL_PATH = "/Volumes/Data/ShareRoot"


class TelemetryExposureTests(unittest.TestCase):
    """Characterize what a telemetry payload is allowed to contain.

    These tests are the guardrail for the privacy hardening: raw LAN addresses,
    SSH/SMB host targets, and local filesystem paths must never survive into an
    emitted payload's options or details.
    """

    def _assert_no_network_or_path_leak(self, values: dict[str, object]) -> None:
        serialized = repr(values)
        self.assertNotIn(LAN_IP, serialized)
        self.assertNotIn(SSH_TARGET, serialized)
        self.assertNotIn(LOCAL_PATH, serialized)

    def test_configure_details_do_not_leak_host(self) -> None:
        payload = {
            "configure_id": "cfg-1",
            "host": SSH_TARGET,
            "ssh_authenticated": True,
            "device_model": "TimeCapsule8,119",
            "device_syap": "119",
            "summary": f"Configured {SSH_TARGET}",
            "compatibility": {
                "payload_family": "samba4",
                "os_name": "NetBSD",
                "os_release": "6.0",
                "arch": "evbarm",
            },
        }
        details = telemetry_details_from_payload("configure", {"enable_ssh": True}, payload)
        self._assert_no_network_or_path_leak(details)
        self.assertEqual(details.get("device_model"), "TimeCapsule8,119")

    def test_reachability_details_do_not_leak_hosts(self) -> None:
        payload = {
            "status": "ok",
            "ssh_host": SSH_TARGET,
            "smb_host": LAN_IP,
            "summary": f"SSH reachable at {SSH_TARGET}",
        }
        details = telemetry_details_from_payload("reachability", {}, payload)
        self._assert_no_network_or_path_leak(details)
        self.assertEqual(details.get("status"), "ok")

    def test_fsck_details_do_not_leak_mount_paths(self) -> None:
        payload = {
            "target": {"name": "Data", "device": "/dev/dk2", "mountpoint": LOCAL_PATH},
            "returncode": 0,
            "summary": f"Checked {LOCAL_PATH}",
        }
        details = telemetry_details_from_payload("fsck", {"volume": "dk2"}, payload)
        self._assert_no_network_or_path_leak(details)

    def test_repair_xattrs_details_do_not_leak_root_path(self) -> None:
        payload = {
            "root": LOCAL_PATH,
            "finding_count": 2,
            "repairable_count": 2,
            "summary_text": f"Scanned {LOCAL_PATH}",
        }
        details = telemetry_details_from_payload("repair-xattrs", {}, payload)
        self._assert_no_network_or_path_leak(details)
        self.assertEqual(details.get("finding_count"), 2)

    def test_options_do_not_leak_free_text_hosts(self) -> None:
        options = telemetry_options_from_params({"yes": True, "dry_run": False, "mount_wait": 30})
        self._assert_no_network_or_path_leak(options)
        self.assertTrue(options.get("yes"))


if __name__ == "__main__":
    unittest.main()
