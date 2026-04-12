from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

import subprocess

from timecapsulesmb.checks.bonjour import parse_browse_instance, parse_lookup_target, run_bonjour_checks
from timecapsulesmb.checks.doctor import run_doctor_checks
from timecapsulesmb.checks.local_tools import check_required_local_tools
from timecapsulesmb.checks.nbns import check_nbns_name_resolution, extract_nbns_response_ip
from timecapsulesmb.checks.smb import check_authenticated_smb_file_ops, exercise_mounted_share_file_ops, try_authenticated_smb_listing


class CheckTests(unittest.TestCase):
    def test_parse_browse_instance_extracts_service_name(self) -> None:
        output = "x x Add 3 4 local. _smb._tcp. Time Capsule Samba 4\n"
        self.assertEqual(parse_browse_instance(output), "Time Capsule Samba 4")

    def test_parse_lookup_target_extracts_target(self) -> None:
        output = "Time Capsule Samba 4._smb._tcp.local. can be reached at timecapsulesamba4.local.:445\n"
        self.assertEqual(parse_lookup_target(output), "timecapsulesamba4.local.:445")

    def test_run_doctor_checks_marks_missing_env_as_fatal(self) -> None:
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_NET_IFACE": "bridge0",
            "TC_SHARE_NAME": "Data",
            "TC_SAMBA_USER": "admin",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule",
        }
        with mock.patch("timecapsulesmb.checks.doctor.check_required_local_tools", return_value=[]):
            with mock.patch("timecapsulesmb.checks.doctor.check_required_artifacts", return_value=[]):
                with mock.patch("timecapsulesmb.checks.doctor.check_ssh_reachability"):
                    with mock.patch("timecapsulesmb.checks.doctor.check_smb_port"):
                        with mock.patch("timecapsulesmb.checks.doctor.run_bonjour_checks", return_value=([], None, None)):
                            with mock.patch("timecapsulesmb.checks.doctor.check_authenticated_smb_listing"):
                                with mock.patch("timecapsulesmb.checks.doctor.check_authenticated_smb_file_ops"):
                                    with mock.patch("timecapsulesmb.checks.doctor.discover_volume_root", return_value="/Volumes/dk2"):
                                        with mock.patch("timecapsulesmb.checks.doctor.run_ssh", return_value=mock.Mock(stdout="")):
                                            results, fatal = run_doctor_checks(values, env_exists=False, repo_root=REPO_ROOT)
        self.assertTrue(fatal)
        self.assertEqual(results[0].status, "FAIL")

    def test_check_required_local_tools_marks_dns_sd_missing_as_fail(self) -> None:
        def fake_exists(name: str) -> bool:
            return name == "ssh"

        with mock.patch("timecapsulesmb.checks.local_tools.command_exists", side_effect=fake_exists):
            results = check_required_local_tools()
        self.assertEqual([r.status for r in results], ["FAIL", "FAIL", "PASS"])

    def test_run_bonjour_checks_returns_fail_when_dns_sd_missing(self) -> None:
        with mock.patch("timecapsulesmb.checks.bonjour.command_exists", return_value=False):
            results, instance, target = run_bonjour_checks("Time Capsule Samba 4")
        self.assertEqual(results[0].status, "FAIL")
        self.assertIsNone(instance)
        self.assertIsNone(target)

    def test_try_authenticated_smb_listing_handles_timeout(self) -> None:
        with mock.patch("timecapsulesmb.checks.smb.command_exists", return_value=True):
            with mock.patch(
                "timecapsulesmb.checks.smb.run_local_capture",
                side_effect=subprocess.TimeoutExpired(cmd=["smbutil"], timeout=12),
            ):
                result = try_authenticated_smb_listing("admin", "pw", ["server.local"])
        self.assertEqual(result.status, "FAIL")
        self.assertIn("timed out", result.message)

    def test_run_doctor_checks_respects_skip_flags(self) -> None:
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
            "TC_NET_IFACE": "bridge0",
            "TC_SHARE_NAME": "Data",
            "TC_SAMBA_USER": "admin",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule",
        }
        with mock.patch("timecapsulesmb.checks.doctor.check_required_local_tools", return_value=[]):
            with mock.patch("timecapsulesmb.checks.doctor.check_required_artifacts", return_value=[]):
                with mock.patch("timecapsulesmb.checks.doctor.check_smb_port") as smb_port_mock:
                    with mock.patch("timecapsulesmb.checks.doctor.discover_volume_root", return_value="/Volumes/dk2"):
                        with mock.patch("timecapsulesmb.checks.doctor.run_ssh", return_value=mock.Mock(stdout="")):
                            results, fatal = run_doctor_checks(
                                values,
                                env_exists=True,
                                repo_root=REPO_ROOT,
                                skip_ssh=True,
                        skip_bonjour=True,
                        skip_smb=True,
                    )
        smb_port_mock.assert_called_once()
        self.assertFalse(fatal)
        self.assertEqual(results[0].status, "PASS")

    def test_run_doctor_checks_skip_ssh_does_not_probe_nbns_marker(self) -> None:
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
            "TC_NET_IFACE": "bridge0",
            "TC_SHARE_NAME": "Data",
            "TC_SAMBA_USER": "admin",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule",
        }
        with mock.patch("timecapsulesmb.checks.doctor.check_required_local_tools", return_value=[]):
            with mock.patch("timecapsulesmb.checks.doctor.check_required_artifacts", return_value=[]):
                with mock.patch("timecapsulesmb.checks.doctor.check_smb_port", return_value=mock.Mock(status="PASS", message="445 ok")):
                    with mock.patch("timecapsulesmb.checks.doctor.discover_volume_root") as discover_mock:
                        with mock.patch("timecapsulesmb.checks.doctor.run_ssh") as run_ssh_mock:
                            results, fatal = run_doctor_checks(
                                values,
                                env_exists=True,
                                repo_root=REPO_ROOT,
                                skip_ssh=True,
                                skip_bonjour=True,
                                skip_smb=True,
                            )
        self.assertFalse(fatal)
        discover_mock.assert_not_called()
        run_ssh_mock.assert_not_called()

    def test_run_doctor_checks_reports_results_as_they_complete(self) -> None:
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_NET_IFACE": "bridge0",
            "TC_SHARE_NAME": "Data",
            "TC_SAMBA_USER": "admin",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule",
        }
        emitted: list[str] = []
        with mock.patch("timecapsulesmb.checks.doctor.check_required_local_tools", return_value=[]):
            with mock.patch("timecapsulesmb.checks.doctor.check_required_artifacts", return_value=[]):
                with mock.patch("timecapsulesmb.checks.doctor.check_ssh_reachability", return_value=mock.Mock(status="PASS", message="ssh ok")):
                    with mock.patch("timecapsulesmb.checks.doctor.check_smb_port", return_value=mock.Mock(status="PASS", message="445 ok")):
                        with mock.patch("timecapsulesmb.checks.doctor.run_bonjour_checks", return_value=([mock.Mock(status="PASS", message="bonjour ok")], None, None)):
                            with mock.patch("timecapsulesmb.checks.doctor.check_authenticated_smb_listing", return_value=mock.Mock(status="PASS", message="listing ok")):
                                with mock.patch("timecapsulesmb.checks.doctor.check_authenticated_smb_file_ops", return_value=mock.Mock(status="PASS", message="file ops ok")):
                                    with mock.patch("timecapsulesmb.checks.doctor.discover_volume_root", return_value="/Volumes/dk2"):
                                        with mock.patch("timecapsulesmb.checks.doctor.run_ssh", return_value=mock.Mock(stdout="")):
                                            results, fatal = run_doctor_checks(
                                                values,
                                                env_exists=True,
                                                repo_root=REPO_ROOT,
                                                on_result=lambda result: emitted.append(result.message),
                                            )
        self.assertFalse(fatal)
        self.assertEqual([result.message for result in results], emitted)

    def test_exercise_mounted_share_file_ops_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            exercise_mounted_share_file_ops(root, prefix="unit")
            self.assertEqual(list(root.iterdir()), [])

    def test_check_authenticated_smb_file_ops_warns_without_mount_smbfs(self) -> None:
        with mock.patch("timecapsulesmb.checks.smb.command_exists", return_value=False):
            result = check_authenticated_smb_file_ops("admin", "pw", "server.local", "Data")
        self.assertEqual(result.status, "WARN")
        self.assertIn("mount_smbfs not found", result.message)

    def test_check_authenticated_smb_file_ops_handles_mount_failure(self) -> None:
        proc = subprocess.CompletedProcess(["mount_smbfs"], 1, "", "mount failed")
        with mock.patch("timecapsulesmb.checks.smb.command_exists", return_value=True):
            with mock.patch("timecapsulesmb.checks.smb._mount_smb_share", return_value=proc):
                result = check_authenticated_smb_file_ops("admin", "pw", "server.local", "Data")
        self.assertEqual(result.status, "FAIL")
        self.assertIn("failed to mount share", result.message)

    def test_check_authenticated_smb_file_ops_reuses_existing_mount_on_file_exists(self) -> None:
        proc = subprocess.CompletedProcess(["mount_smbfs"], 64, "", "mount_smbfs: mount error: //admin:pw@server.local/Data: File exists")
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            existing_mount = root / "mounted"
            existing_mount.mkdir()
            with mock.patch("timecapsulesmb.checks.smb.command_exists", return_value=True):
                with mock.patch("timecapsulesmb.checks.smb._mount_smb_share", return_value=proc):
                    with mock.patch("timecapsulesmb.checks.smb._find_existing_smb_mount", return_value=existing_mount):
                        result = check_authenticated_smb_file_ops("admin", "pw", "server.local", "Data")
        self.assertEqual(result.status, "PASS")
        self.assertIn("via existing mount", result.message)

    def test_extract_nbns_response_ip_reads_first_answer_ipv4(self) -> None:
        packet = (
            b"\x13\x37\x85\x00\x00\x01\x00\x01\x00\x00\x00\x00"
            + b"\x20" + b"FEEFFDFECACACACACACACACACACACAAA" + b"\x00"
            + b"\x00\x20\x00\x01"
            + b"\xc0\x0c\x00\x20\x00\x01\x00\x00\x01,\x00\x06\x00\x00"
            + b"\xc0\xa8\x01\xd9"
        )
        self.assertEqual(extract_nbns_response_ip(packet), "192.168.1.217")

    def test_check_nbns_name_resolution_reports_timeout(self) -> None:
        fake_sock = mock.Mock()
        fake_sock.recvfrom.side_effect = TimeoutError()
        with mock.patch("timecapsulesmb.checks.nbns.socket.socket", return_value=fake_sock):
            result = check_nbns_name_resolution("TimeCapsule", "192.168.1.217", "192.168.1.217")
        self.assertEqual(result.status, "FAIL")
        self.assertIn("timed out", result.message)

    def test_run_doctor_checks_skips_nbns_when_marker_absent(self) -> None:
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
            "TC_NET_IFACE": "bridge0",
            "TC_SHARE_NAME": "Data",
            "TC_SAMBA_USER": "admin",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule",
        }
        with mock.patch("timecapsulesmb.checks.doctor.check_required_local_tools", return_value=[]):
            with mock.patch("timecapsulesmb.checks.doctor.check_required_artifacts", return_value=[]):
                with mock.patch("timecapsulesmb.checks.doctor.check_ssh_reachability", return_value=mock.Mock(status="PASS", message="ssh ok")):
                    with mock.patch("timecapsulesmb.checks.doctor.check_smb_port", return_value=mock.Mock(status="PASS", message="445 ok")):
                        with mock.patch("timecapsulesmb.checks.doctor.run_bonjour_checks", return_value=([], None, None)):
                            with mock.patch("timecapsulesmb.checks.doctor.check_authenticated_smb_listing", return_value=mock.Mock(status="PASS", message="listing ok")):
                                with mock.patch("timecapsulesmb.checks.doctor.check_authenticated_smb_file_ops", return_value=mock.Mock(status="PASS", message="file ops ok")):
                                    with mock.patch("timecapsulesmb.checks.doctor.discover_volume_root", return_value="/Volumes/dk2"):
                                        with mock.patch("timecapsulesmb.checks.doctor.run_ssh", return_value=mock.Mock(stdout="")):
                                            results, fatal = run_doctor_checks(values, env_exists=True, repo_root=REPO_ROOT)
        self.assertFalse(fatal)
        nbns_result = next(result for result in results if "NBNS responder not enabled" in result.message)
        self.assertEqual(nbns_result.status, "SKIP")
        nbns_index = results.index(nbns_result)
        listing_index = next(i for i, result in enumerate(results) if result.message == "listing ok")
        self.assertLess(nbns_index, listing_index)

    def test_run_doctor_checks_checks_nbns_when_marker_present(self) -> None:
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
            "TC_NET_IFACE": "bridge0",
            "TC_SHARE_NAME": "Data",
            "TC_SAMBA_USER": "admin",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule",
        }
        with mock.patch("timecapsulesmb.checks.doctor.check_required_local_tools", return_value=[]):
            with mock.patch("timecapsulesmb.checks.doctor.check_required_artifacts", return_value=[]):
                with mock.patch("timecapsulesmb.checks.doctor.check_ssh_reachability", return_value=mock.Mock(status="PASS", message="ssh ok")):
                    with mock.patch("timecapsulesmb.checks.doctor.check_smb_port", return_value=mock.Mock(status="PASS", message="445 ok")):
                        with mock.patch("timecapsulesmb.checks.doctor.run_bonjour_checks", return_value=([], None, None)):
                            with mock.patch("timecapsulesmb.checks.doctor.check_authenticated_smb_listing", return_value=mock.Mock(status="PASS", message="listing ok")):
                                with mock.patch("timecapsulesmb.checks.doctor.check_authenticated_smb_file_ops", return_value=mock.Mock(status="PASS", message="file ops ok")):
                                    with mock.patch("timecapsulesmb.checks.doctor.discover_volume_root", return_value="/Volumes/dk2"):
                                        with mock.patch("timecapsulesmb.checks.doctor.run_ssh", return_value=mock.Mock(stdout="enabled\n")):
                                            with mock.patch("timecapsulesmb.checks.doctor.check_nbns_name_resolution", return_value=mock.Mock(status="PASS", message="nbns ok")):
                                                results, fatal = run_doctor_checks(values, env_exists=True, repo_root=REPO_ROOT)
        self.assertFalse(fatal)
        nbns_result = next(result for result in results if result.message == "nbns ok")
        self.assertEqual(nbns_result.status, "PASS")
        nbns_index = results.index(nbns_result)
        listing_index = next(i for i, result in enumerate(results) if result.message == "listing ok")
        self.assertLess(nbns_index, listing_index)


if __name__ == "__main__":
    unittest.main()
