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
from timecapsulesmb.checks.doctor import _configured_smb_server, check_xattr_tdb_persistence, run_doctor_checks
from timecapsulesmb.checks.local_tools import check_required_local_tools
from timecapsulesmb.checks.network import check_ssh_login, ssh_opts_use_proxy
from timecapsulesmb.checks.nbns import build_nbns_query, check_nbns_name_resolution, extract_nbns_response_ip
from timecapsulesmb.checks.smb import (
    check_authenticated_smb_file_ops,
    check_authenticated_smb_file_ops_detailed,
    check_authenticated_smb_listing,
    exercise_mounted_share_file_ops,
    try_authenticated_smb_listing,
)


class CheckTests(unittest.TestCase):
    def test_parse_browse_instance_extracts_service_name(self) -> None:
        output = "x x Add 3 4 local. _smb._tcp. Time Capsule Samba 4\n"
        self.assertEqual(parse_browse_instance(output), "Time Capsule Samba 4")

    def test_parse_lookup_target_extracts_target(self) -> None:
        output = "Time Capsule Samba 4._smb._tcp.local. can be reached at timecapsulesamba4.local.:445\n"
        self.assertEqual(parse_lookup_target(output), "timecapsulesamba4.local.:445")

    def test_configured_smb_server_appends_local_only_for_single_label_hostname(self) -> None:
        self.assertEqual(_configured_smb_server("timecapsulesamba4"), "timecapsulesamba4.local")
        self.assertEqual(_configured_smb_server("timecapsulesamba4.local"), "timecapsulesamba4.local")
        self.assertEqual(_configured_smb_server("10.0.1.99"), "10.0.1.99")

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
            "TC_AIRPORT_SYAP": "119",
        }
        with mock.patch("timecapsulesmb.checks.doctor.check_required_local_tools", return_value=[]):
            with mock.patch("timecapsulesmb.checks.doctor.check_required_artifacts", return_value=[]):
                with mock.patch("timecapsulesmb.checks.doctor.check_ssh_login"):
                    with mock.patch("timecapsulesmb.checks.doctor.check_smb_port"):
                        with mock.patch("timecapsulesmb.checks.doctor.run_bonjour_checks", return_value=([], None, None)):
                            with mock.patch("timecapsulesmb.checks.doctor.check_authenticated_smb_listing"):
                                with mock.patch("timecapsulesmb.checks.doctor.check_authenticated_smb_file_ops_detailed", return_value=[]):
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
        self.assertEqual([r.status for r in results], ["FAIL", "PASS"])
        self.assertEqual([r.message for r in results], ["missing local tool smbclient", "found local tool ssh"])

    def test_run_bonjour_checks_returns_fail_when_discovery_backend_exits(self) -> None:
        with mock.patch("timecapsulesmb.checks.bonjour.discover", side_effect=SystemExit("zeroconf missing")):
            results, instance, target = run_bonjour_checks("Time Capsule Samba 4")
        self.assertEqual(results[0].status, "FAIL")
        self.assertIn("zeroconf missing", results[0].message)
        self.assertIsNone(instance)
        self.assertIsNone(target)

    def test_run_bonjour_checks_discovers_expected_instance_via_python_backend(self) -> None:
        record = mock.Mock()
        record.name = "Time Capsule Samba 4"
        record.hostname = "timecapsulesamba4.local"
        record.ipv4 = ["10.0.0.2"]
        record.ipv6 = []
        record.services = {"_smb._tcp.local."}
        with mock.patch("timecapsulesmb.checks.bonjour.discover", return_value=[record]):
            results, instance, target = run_bonjour_checks("Time Capsule Samba 4")
        self.assertEqual([result.status for result in results], ["PASS", "PASS"])
        self.assertEqual(instance, "Time Capsule Samba 4")
        self.assertEqual(target, "timecapsulesamba4.local:445")

    def test_try_authenticated_smb_listing_handles_timeout(self) -> None:
        with mock.patch("timecapsulesmb.checks.smb.command_exists", return_value=True):
            with mock.patch(
                "timecapsulesmb.checks.smb.run_local_capture",
                side_effect=subprocess.TimeoutExpired(cmd=["smbclient"], timeout=12),
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
            "TC_AIRPORT_SYAP": "119",
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

    def test_run_doctor_checks_reports_invalid_env_values(self) -> None:
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
            "TC_MDNS_HOST_LABEL": "bad host label",
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule",
            "TC_AIRPORT_SYAP": "119",
        }
        with mock.patch("timecapsulesmb.checks.doctor.check_required_local_tools", return_value=[]):
            with mock.patch("timecapsulesmb.checks.doctor.check_required_artifacts", return_value=[]):
                results, fatal = run_doctor_checks(values, env_exists=True, repo_root=REPO_ROOT)
        self.assertTrue(fatal)
        self.assertIn("TC_MDNS_HOST_LABEL is invalid", results[0].message)

    def test_run_doctor_checks_reports_missing_remote_interface(self) -> None:
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
            "TC_NET_IFACE": "bridge9",
            "TC_SHARE_NAME": "Data",
            "TC_SAMBA_USER": "admin",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule",
            "TC_AIRPORT_SYAP": "119",
        }
        with mock.patch("timecapsulesmb.checks.doctor.check_required_local_tools", return_value=[]):
            with mock.patch("timecapsulesmb.checks.doctor.check_required_artifacts", return_value=[]):
                with mock.patch("timecapsulesmb.checks.doctor.check_ssh_login", return_value=mock.Mock(status="PASS", message="ssh ok")):
                    with mock.patch("timecapsulesmb.checks.doctor.check_smb_port", return_value=mock.Mock(status="PASS", message="445 ok")):
                        with mock.patch("timecapsulesmb.checks.doctor.run_bonjour_checks", return_value=([], None, None)):
                            with mock.patch("timecapsulesmb.checks.doctor.check_authenticated_smb_listing", return_value=mock.Mock(status="PASS", message="listing ok")):
                                with mock.patch("timecapsulesmb.checks.doctor.check_authenticated_smb_file_ops_detailed", return_value=[]):
                                    with mock.patch("timecapsulesmb.checks.doctor.discover_volume_root", return_value="/Volumes/dk2"):
                                        with mock.patch("timecapsulesmb.checks.doctor.run_ssh", return_value=mock.Mock(returncode=1, stdout="")):
                                            results, fatal = run_doctor_checks(values, env_exists=True, repo_root=REPO_ROOT)
        self.assertTrue(fatal)
        self.assertTrue(any("TC_NET_IFACE is invalid" in result.message for result in results))

    def test_run_doctor_checks_reports_managed_mdns_takeover_state(self) -> None:
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
            "TC_AIRPORT_SYAP": "119",
        }
        with mock.patch("timecapsulesmb.checks.doctor.check_required_local_tools", return_value=[]):
            with mock.patch("timecapsulesmb.checks.doctor.check_required_artifacts", return_value=[]):
                with mock.patch("timecapsulesmb.checks.doctor.check_ssh_login", return_value=mock.Mock(status="PASS", message="ssh ok")):
                    with mock.patch("timecapsulesmb.checks.doctor.check_smb_port", return_value=mock.Mock(status="PASS", message="445 ok")):
                        with mock.patch("timecapsulesmb.checks.doctor.run_bonjour_checks", return_value=([], None, None)):
                            with mock.patch("timecapsulesmb.checks.doctor.check_authenticated_smb_listing", return_value=mock.Mock(status="PASS", message="listing ok")):
                                with mock.patch("timecapsulesmb.checks.doctor.check_authenticated_smb_file_ops_detailed", return_value=[]):
                                    with mock.patch("timecapsulesmb.checks.doctor.discover_volume_root", return_value="/Volumes/dk2"):
                                        with mock.patch("timecapsulesmb.checks.doctor._managed_mdns_takeover_ready", return_value=False):
                                            with mock.patch("timecapsulesmb.checks.doctor.run_ssh", return_value=mock.Mock(returncode=0, stdout="[global]\n xattr_tdb:file = /Volumes/dk2/samba4/private/xattr.tdb\n[Data]\n")):
                                                results, fatal = run_doctor_checks(values, env_exists=True, repo_root=REPO_ROOT)
        self.assertTrue(fatal)
        self.assertTrue(any("managed mDNS takeover is not active" in result.message for result in results))

    def test_ssh_opts_use_proxy_detects_proxycommand_and_proxyjump(self) -> None:
        self.assertTrue(ssh_opts_use_proxy("-o ProxyCommand=ssh\\ -W\\ %h:%p\\ bastion"))
        self.assertTrue(ssh_opts_use_proxy("-J bastion.example.com"))
        self.assertTrue(ssh_opts_use_proxy("-Jbastion.example.com"))
        self.assertTrue(ssh_opts_use_proxy("-o ProxyJump=bastion.example.com"))
        self.assertTrue(ssh_opts_use_proxy("-oProxyCommand=ssh\\ -W\\ %h:%p\\ bastion"))
        self.assertFalse(ssh_opts_use_proxy("-o HostKeyAlgorithms=+ssh-rsa"))

    def test_check_ssh_login_uses_configured_ssh_transport(self) -> None:
        with mock.patch(
            "timecapsulesmb.checks.network.run_ssh",
            return_value=mock.Mock(returncode=0, stdout="ok\n"),
        ) as run_ssh_mock:
            result = check_ssh_login("root@192.168.1.118", "pw", "-o ProxyCommand=jump")
        self.assertEqual(result.status, "PASS")
        run_ssh_mock.assert_called_once_with(
            "root@192.168.1.118",
            "pw",
            "-o ProxyCommand=jump",
            "/bin/echo ok",
            check=False,
            timeout=30,
        )

    def test_check_ssh_login_reports_friendlier_ssh_transport_error(self) -> None:
        with mock.patch(
            "timecapsulesmb.checks.network.run_ssh",
            side_effect=SystemExit("Connecting to the device failed, SSH error: bind [127.0.0.1]:108: Permission denied"),
        ):
            result = check_ssh_login("root@192.168.1.118", "pw", "-o LocalForward=127.0.0.1:108:127.0.0.1:108")
        self.assertEqual(result.status, "FAIL")
        self.assertEqual(
            result.message,
            "Connecting to the device failed, SSH error: bind [127.0.0.1]:108: Permission denied",
        )

    def test_run_doctor_checks_proxy_target_skips_local_network_checks(self) -> None:
        values = {
            "TC_HOST": "root@192.168.1.118",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o ProxyCommand=ssh\\ -W\\ %h:%p\\ bastion",
            "TC_NET_IFACE": "bridge0",
            "TC_SHARE_NAME": "Data",
            "TC_SAMBA_USER": "admin",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule",
            "TC_AIRPORT_SYAP": "119",
        }
        with mock.patch("timecapsulesmb.checks.doctor.check_required_local_tools", return_value=[]):
            with mock.patch("timecapsulesmb.checks.doctor.check_required_artifacts", return_value=[]):
                with mock.patch("timecapsulesmb.checks.doctor.check_ssh_login", return_value=mock.Mock(status="PASS", message="ssh ok")) as ssh_mock:
                    with mock.patch("timecapsulesmb.checks.doctor.check_smb_port") as smb_port_mock:
                        with mock.patch("timecapsulesmb.checks.doctor.run_bonjour_checks") as bonjour_mock:
                            with mock.patch("timecapsulesmb.checks.doctor.find_free_local_port", return_value=1445):
                                with mock.patch("timecapsulesmb.checks.doctor.ssh_local_forward") as tunnel_mock:
                                    tunnel_mock.return_value.__enter__.return_value = None
                                    tunnel_mock.return_value.__exit__.return_value = None
                                    with mock.patch("timecapsulesmb.checks.doctor.check_authenticated_smb_listing", return_value=mock.Mock(status="PASS", message="listing ok")) as smb_listing_mock:
                                        with mock.patch("timecapsulesmb.checks.doctor.check_authenticated_smb_file_ops_detailed", return_value=[mock.Mock(status="PASS", message="file ops ok")]) as smb_file_ops_mock:
                                            with mock.patch("timecapsulesmb.checks.doctor.discover_volume_root", return_value="/Volumes/dk2"):
                                                with mock.patch("timecapsulesmb.checks.doctor.run_ssh", return_value=mock.Mock(stdout="enabled\n")):
                                                    with mock.patch("timecapsulesmb.checks.doctor.check_nbns_name_resolution") as nbns_mock:
                                                        results, fatal = run_doctor_checks(values, env_exists=True, repo_root=REPO_ROOT)
        self.assertFalse(fatal)
        ssh_mock.assert_called_once_with("root@192.168.1.118", "pw", values["TC_SSH_OPTS"])
        smb_port_mock.assert_not_called()
        bonjour_mock.assert_not_called()
        nbns_mock.assert_not_called()
        tunnel_mock.assert_called_once_with(
            "root@192.168.1.118",
            "pw",
            values["TC_SSH_OPTS"],
            local_port=1445,
            remote_host="192.168.1.118",
            remote_port=445,
        )
        smb_listing_mock.assert_called_once_with(
            "admin",
            "pw",
            "127.0.0.1",
            expected_share_name="Data",
            port=1445,
        )
        smb_file_ops_mock.assert_called_once_with(
            "admin",
            "pw",
            "127.0.0.1",
            "Data",
            port=1445,
        )
        messages = [result.message for result in results if result.status == "SKIP"]
        self.assertTrue(any("direct SMB port check skipped" in message for message in messages))
        self.assertTrue(any("Bonjour check skipped" in message for message in messages))
        self.assertTrue(any("NBNS check skipped" in message for message in messages))
        self.assertFalse(any("authenticated SMB checks skipped" in message for message in messages))

    def test_run_doctor_checks_compact_jump_option_skips_local_network_checks(self) -> None:
        values = {
            "TC_HOST": "root@192.168.1.118",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-Jbastion.example.com -o HostKeyAlgorithms=+ssh-rsa",
            "TC_NET_IFACE": "bridge0",
            "TC_SHARE_NAME": "Data",
            "TC_SAMBA_USER": "admin",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule",
            "TC_AIRPORT_SYAP": "119",
        }
        with mock.patch("timecapsulesmb.checks.doctor.check_required_local_tools", return_value=[]):
            with mock.patch("timecapsulesmb.checks.doctor.check_required_artifacts", return_value=[]):
                with mock.patch("timecapsulesmb.checks.doctor.check_ssh_login", return_value=mock.Mock(status="PASS", message="ssh ok")):
                    with mock.patch("timecapsulesmb.checks.doctor.check_smb_port") as smb_port_mock:
                        with mock.patch("timecapsulesmb.checks.doctor.run_bonjour_checks") as bonjour_mock:
                            with mock.patch("timecapsulesmb.checks.doctor.find_free_local_port", return_value=1446):
                                with mock.patch("timecapsulesmb.checks.doctor.ssh_local_forward") as tunnel_mock:
                                    tunnel_mock.return_value.__enter__.return_value = None
                                    tunnel_mock.return_value.__exit__.return_value = None
                                    with mock.patch("timecapsulesmb.checks.doctor.check_authenticated_smb_listing", return_value=mock.Mock(status="PASS", message="listing ok")) as smb_listing_mock:
                                        with mock.patch("timecapsulesmb.checks.doctor.check_authenticated_smb_file_ops_detailed", return_value=[mock.Mock(status="PASS", message="file ops ok")]) as smb_file_ops_mock:
                                            with mock.patch("timecapsulesmb.checks.doctor.discover_volume_root", return_value="/Volumes/dk2"):
                                                with mock.patch("timecapsulesmb.checks.doctor.run_ssh", return_value=mock.Mock(stdout="enabled\n")):
                                                    with mock.patch("timecapsulesmb.checks.doctor.check_nbns_name_resolution") as nbns_mock:
                                                        results, fatal = run_doctor_checks(values, env_exists=True, repo_root=REPO_ROOT)
        self.assertFalse(fatal)
        smb_port_mock.assert_not_called()
        bonjour_mock.assert_not_called()
        nbns_mock.assert_not_called()
        tunnel_mock.assert_called_once()
        smb_listing_mock.assert_called_once()
        smb_file_ops_mock.assert_called_once()
        messages = [result.message for result in results if result.status == "SKIP"]
        self.assertTrue(any("direct SMB port check skipped" in message for message in messages))
        self.assertTrue(any("Bonjour check skipped" in message for message in messages))
        self.assertTrue(any("NBNS check skipped" in message for message in messages))
        self.assertFalse(any("authenticated SMB checks skipped" in message for message in messages))

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
            "TC_AIRPORT_SYAP": "119",
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

    def test_check_xattr_tdb_persistence_passes_for_disk_path(self) -> None:
        smb_conf = "    xattr_tdb:file = /Volumes/dk2/samba4/private/xattr.tdb\n"
        with mock.patch("timecapsulesmb.checks.doctor.run_ssh", return_value=mock.Mock(stdout=smb_conf)):
            result = check_xattr_tdb_persistence("root@tc", "pw", "-o foo")
        self.assertEqual(result.status, "PASS")
        self.assertIn("/Volumes/dk2/samba4/private/xattr.tdb", result.message)

    def test_check_xattr_tdb_persistence_fails_for_ramdisk_path(self) -> None:
        smb_conf = "    xattr_tdb:file = /mnt/Memory/samba4/private/xattr.tdb\n"
        with mock.patch("timecapsulesmb.checks.doctor.run_ssh", return_value=mock.Mock(stdout=smb_conf)):
            result = check_xattr_tdb_persistence("root@tc", "pw", "-o foo")
        self.assertEqual(result.status, "FAIL")
        self.assertIn("non-persistent ramdisk", result.message)

    def test_check_xattr_tdb_persistence_warns_when_missing(self) -> None:
        with mock.patch("timecapsulesmb.checks.doctor.run_ssh", return_value=mock.Mock(stdout="[global]\n")):
            result = check_xattr_tdb_persistence("root@tc", "pw", "-o foo")
        self.assertEqual(result.status, "WARN")
        self.assertIn("does not contain xattr_tdb:file", result.message)

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
            "TC_AIRPORT_SYAP": "119",
        }
        emitted: list[str] = []
        with mock.patch("timecapsulesmb.checks.doctor.check_required_local_tools", return_value=[]):
            with mock.patch("timecapsulesmb.checks.doctor.check_required_artifacts", return_value=[]):
                with mock.patch("timecapsulesmb.checks.doctor.check_ssh_login", return_value=mock.Mock(status="PASS", message="ssh ok")):
                    with mock.patch("timecapsulesmb.checks.doctor.check_smb_port", return_value=mock.Mock(status="PASS", message="445 ok")):
                        with mock.patch("timecapsulesmb.checks.doctor.run_bonjour_checks", return_value=([mock.Mock(status="PASS", message="bonjour ok")], None, None)):
                            with mock.patch("timecapsulesmb.checks.doctor.check_authenticated_smb_listing", return_value=mock.Mock(status="PASS", message="listing ok")):
                                with mock.patch("timecapsulesmb.checks.doctor.check_authenticated_smb_file_ops_detailed", return_value=[mock.Mock(status="PASS", message="file ops ok")]):
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

    def test_run_doctor_checks_emits_detailed_smb_operation_results(self) -> None:
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
            "TC_AIRPORT_SYAP": "119",
        }
        smb_results = [
            mock.Mock(status="PASS", message="SMB directory create works"),
            mock.Mock(status="PASS", message="SMB file create works"),
            mock.Mock(status="PASS", message="SMB file overwrite/edit works"),
            mock.Mock(status="PASS", message="SMB file read works"),
            mock.Mock(status="PASS", message="SMB file rename works"),
            mock.Mock(status="PASS", message="SMB file copy works"),
            mock.Mock(status="PASS", message="SMB file delete works"),
            mock.Mock(status="PASS", message="SMB directory ls list works"),
            mock.Mock(status="PASS", message="SMB directory delete works"),
            mock.Mock(status="PASS", message="SMB final cleanup check passed"),
        ]
        with mock.patch("timecapsulesmb.checks.doctor.check_required_local_tools", return_value=[]):
            with mock.patch("timecapsulesmb.checks.doctor.check_required_artifacts", return_value=[]):
                with mock.patch("timecapsulesmb.checks.doctor.check_ssh_login", return_value=mock.Mock(status="PASS", message="ssh ok")):
                    with mock.patch("timecapsulesmb.checks.doctor.check_smb_port", return_value=mock.Mock(status="PASS", message="445 ok")):
                        with mock.patch("timecapsulesmb.checks.doctor.run_bonjour_checks", return_value=([], None, None)):
                            with mock.patch("timecapsulesmb.checks.doctor.check_authenticated_smb_listing", return_value=mock.Mock(status="PASS", message="listing ok")):
                                with mock.patch("timecapsulesmb.checks.doctor.check_authenticated_smb_file_ops_detailed", return_value=smb_results):
                                    with mock.patch("timecapsulesmb.checks.doctor.discover_volume_root", return_value="/Volumes/dk2"):
                                        with mock.patch("timecapsulesmb.checks.doctor.run_ssh", return_value=mock.Mock(stdout="")):
                                            results, fatal = run_doctor_checks(values, env_exists=True, repo_root=REPO_ROOT)
        self.assertFalse(fatal)
        self.assertEqual([result.message for result in results[-10:]], [result.message for result in smb_results])

    def test_run_doctor_checks_emits_naming_diagnostics(self) -> None:
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_NET_IFACE": "bridge0",
            "TC_SHARE_NAME": "Data",
            "TC_SAMBA_USER": "admin",
            "TC_NETBIOS_NAME": "HomeSamba",
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_MDNS_INSTANCE_NAME": "Home-Samba",
            "TC_MDNS_HOST_LABEL": "home-samba",
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule",
            "TC_AIRPORT_SYAP": "119",
        }
        active_smb_conf = """
[global]
    netbios name = HomeSamba

[Data]
    path = /Volumes/dk2/ShareRoot

[Data_Kitchen]
    path = /Volumes/dk2/Other
"""
        with mock.patch("timecapsulesmb.checks.doctor.check_required_local_tools", return_value=[]):
            with mock.patch("timecapsulesmb.checks.doctor.check_required_artifacts", return_value=[]):
                with mock.patch("timecapsulesmb.checks.doctor.check_ssh_login", return_value=mock.Mock(status="PASS", message="ssh ok")):
                    with mock.patch("timecapsulesmb.checks.doctor.check_smb_port", return_value=mock.Mock(status="PASS", message="445 ok")):
                        with mock.patch(
                            "timecapsulesmb.checks.doctor.run_bonjour_checks",
                            return_value=([mock.Mock(status="PASS", message="bonjour ok")], "Home-Samba", "home-samba.local:445"),
                        ):
                            with mock.patch("timecapsulesmb.checks.doctor.check_authenticated_smb_listing", return_value=mock.Mock(status="PASS", message="listing ok")):
                                with mock.patch("timecapsulesmb.checks.doctor.check_authenticated_smb_file_ops_detailed", return_value=[mock.Mock(status="PASS", message="file ops ok")]):
                                    with mock.patch("timecapsulesmb.checks.doctor.discover_volume_root", return_value="/Volumes/dk2"):
                                        with mock.patch("timecapsulesmb.checks.doctor.run_ssh", return_value=mock.Mock(stdout=active_smb_conf)):
                                            results, fatal = run_doctor_checks(values, env_exists=True, repo_root=REPO_ROOT)
        self.assertFalse(fatal)
        info_messages = [result.message for result in results if result.status == "INFO"]
        self.assertIn("advertised Bonjour instance: Home-Samba", info_messages)
        self.assertIn("advertised Bonjour host label: home-samba", info_messages)
        self.assertIn("active Samba NetBIOS name: HomeSamba", info_messages)
        self.assertIn("active Samba share names: Data, Data_Kitchen", info_messages)

    def test_run_doctor_checks_passes_expected_share_to_listing(self) -> None:
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
            "TC_AIRPORT_SYAP": "119",
        }
        with mock.patch("timecapsulesmb.checks.doctor.check_required_local_tools", return_value=[]):
            with mock.patch("timecapsulesmb.checks.doctor.check_required_artifacts", return_value=[]):
                with mock.patch("timecapsulesmb.checks.doctor.check_ssh_login", return_value=mock.Mock(status="PASS", message="ssh ok")):
                    with mock.patch("timecapsulesmb.checks.doctor.check_smb_port", return_value=mock.Mock(status="PASS", message="445 ok")):
                        with mock.patch("timecapsulesmb.checks.doctor.run_bonjour_checks", return_value=([], None, None)):
                            with mock.patch(
                                "timecapsulesmb.checks.doctor.check_authenticated_smb_listing",
                                return_value=mock.Mock(status="PASS", message="listing ok"),
                            ) as listing_mock:
                                with mock.patch(
                                    "timecapsulesmb.checks.doctor.check_authenticated_smb_file_ops_detailed",
                                    return_value=[mock.Mock(status="PASS", message="file ops ok")],
                                ):
                                    with mock.patch("timecapsulesmb.checks.doctor.discover_volume_root", return_value="/Volumes/dk2"):
                                        with mock.patch("timecapsulesmb.checks.doctor.run_ssh", return_value=mock.Mock(stdout="")):
                                            run_doctor_checks(values, env_exists=True, repo_root=REPO_ROOT)
        listing_mock.assert_called_once_with(
            "admin",
            "pw",
            "timecapsulesamba4.local",
            expected_share_name="Data",
        )

    def test_run_doctor_checks_uses_ip_mdns_host_label_without_appending_local(self) -> None:
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_NET_IFACE": "bridge0",
            "TC_SHARE_NAME": "Data",
            "TC_SAMBA_USER": "admin",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "10.0.1.99",
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule",
            "TC_AIRPORT_SYAP": "119",
        }
        with mock.patch("timecapsulesmb.checks.doctor.check_required_local_tools", return_value=[]):
            with mock.patch("timecapsulesmb.checks.doctor.check_required_artifacts", return_value=[]):
                with mock.patch("timecapsulesmb.checks.doctor.check_ssh_login", return_value=mock.Mock(status="PASS", message="ssh ok")):
                    with mock.patch("timecapsulesmb.checks.doctor.check_smb_port", return_value=mock.Mock(status="PASS", message="445 ok")):
                        with mock.patch("timecapsulesmb.checks.doctor.run_bonjour_checks", return_value=([], None, None)):
                            with mock.patch(
                                "timecapsulesmb.checks.doctor.check_authenticated_smb_listing",
                                return_value=mock.Mock(status="PASS", message="listing ok"),
                            ) as listing_mock:
                                with mock.patch(
                                    "timecapsulesmb.checks.doctor.check_authenticated_smb_file_ops_detailed",
                                    return_value=[mock.Mock(status="PASS", message="file ops ok")],
                                ):
                                    with mock.patch("timecapsulesmb.checks.doctor.discover_volume_root", return_value="/Volumes/dk2"):
                                        with mock.patch("timecapsulesmb.checks.doctor.run_ssh", return_value=mock.Mock(stdout="")):
                                            run_doctor_checks(values, env_exists=True, repo_root=REPO_ROOT)
        listing_mock.assert_called_once_with(
            "admin",
            "pw",
            "10.0.1.99",
            expected_share_name="Data",
        )

    def test_check_authenticated_smb_listing_requires_expected_share(self) -> None:
        proc = subprocess.CompletedProcess(["smbclient"], 0, "Public\n", "")
        with mock.patch("timecapsulesmb.checks.smb.command_exists", return_value=True):
            with mock.patch("timecapsulesmb.checks.smb.run_local_capture", return_value=proc):
                result = check_authenticated_smb_listing(
                    "admin",
                    "pw",
                    "server.local",
                    expected_share_name="Data",
                )
        self.assertEqual(result.status, "FAIL")
        self.assertIn("did not include expected share", result.message)

    def test_check_authenticated_smb_listing_passes_when_expected_share_present(self) -> None:
        proc = subprocess.CompletedProcess(["smbclient"], 0, "Data\nPublic\n", "")
        with mock.patch("timecapsulesmb.checks.smb.command_exists", return_value=True):
            with mock.patch("timecapsulesmb.checks.smb.run_local_capture", return_value=proc):
                result = check_authenticated_smb_listing(
                    "admin",
                    "pw",
                    "server.local",
                    expected_share_name="Data",
                )
        self.assertEqual(result.status, "PASS")
        self.assertIn("listing works", result.message)

    def test_exercise_mounted_share_file_ops_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            exercise_mounted_share_file_ops(root, prefix="unit")
            self.assertEqual(list(root.iterdir()), [])

    def test_check_authenticated_smb_file_ops_warns_without_smbclient(self) -> None:
        with mock.patch("timecapsulesmb.checks.smb.command_exists", return_value=False):
            result = check_authenticated_smb_file_ops("admin", "pw", "server.local", "Data")
        self.assertEqual(result.status, "WARN")
        self.assertIn("smbclient not found", result.message)

    def test_check_authenticated_smb_file_ops_handles_smbclient_failure(self) -> None:
        proc = subprocess.CompletedProcess(["smbclient"], 1, "", "NT_STATUS_LOGON_FAILURE")
        with mock.patch("timecapsulesmb.checks.smb.command_exists", return_value=True):
            with mock.patch("timecapsulesmb.checks.smb.run_local_capture", return_value=proc):
                result = check_authenticated_smb_file_ops("admin", "pw", "server.local", "Data")
        self.assertEqual(result.status, "FAIL")
        self.assertIn("NT_STATUS_LOGON_FAILURE", result.message)

    def test_check_authenticated_smb_file_ops_detailed_reports_each_step(self) -> None:
        def fake_run_local_capture(args, timeout=15):
            self.assertEqual(args[0], "smbclient")
            self.assertEqual(args[1:3], ["-s", "/dev/null"])
            command_text = args[-1]
            if 'get "sample.txt"' in command_text:
                download_target = Path(command_text.split('get "sample.txt" "', 1)[1].split('"', 1)[0])
                download_target.write_text("line1\nline2\nline3\nline4-updated\n", encoding="utf-8")
                return subprocess.CompletedProcess(args, 0, "", "")
            if 'get "sample-renamed.txt"' in command_text and 'get "sample-copy.txt"' in command_text:
                renamed_target = Path(command_text.split('get "sample-renamed.txt" "', 1)[1].split('"', 1)[0])
                copy_target = Path(command_text.split('get "sample-copy.txt" "', 1)[1].split('"', 1)[0])
                renamed_target.write_text("line1\nline2\nline3\nline4-updated\n", encoding="utf-8")
                copy_target.write_text("line1\nline2\nline3\nline4-updated\n", encoding="utf-8")
                return subprocess.CompletedProcess(args, 0, "", "")
            if 'get "sample-renamed.txt"' in command_text:
                download_target = Path(command_text.split('get "sample-renamed.txt" "', 1)[1].split('"', 1)[0])
                download_target.write_text("line1\nline2\nline3\nline4-updated\n", encoding="utf-8")
                return subprocess.CompletedProcess(args, 0, "", "")
            if 'get "sample-copy.txt"' in command_text:
                download_target = Path(command_text.split('get "sample-copy.txt" "', 1)[1].split('"', 1)[0])
                download_target.write_text("line1\nline2\nline3\nline4-updated\n", encoding="utf-8")
                return subprocess.CompletedProcess(args, 0, "", "")
            if 'del "sample-copy.txt"; ls' in command_text:
                return subprocess.CompletedProcess(args, 0, "sample-renamed.txt\n", "")
            if command_text == "ls":
                return subprocess.CompletedProcess(args, 0, "Public\n", "")
            return subprocess.CompletedProcess(args, 0, "", "")

        with mock.patch("timecapsulesmb.checks.smb.command_exists", return_value=True):
            with mock.patch("timecapsulesmb.checks.smb.run_local_capture", side_effect=fake_run_local_capture):
                results = check_authenticated_smb_file_ops_detailed("admin", "pw", "server.local", "Data")
        self.assertEqual([result.status for result in results], ["PASS"] * 10)
        self.assertEqual(
            [result.message for result in results],
            [
                "SMB directory create works for admin@server.local/Data",
                "SMB file create works for admin@server.local/Data",
                "SMB file overwrite/edit works for admin@server.local/Data",
                "SMB file read works for admin@server.local/Data",
                "SMB file rename works for admin@server.local/Data",
                "SMB file copy works for admin@server.local/Data",
                "SMB file delete works for admin@server.local/Data",
                "SMB directory ls list works for admin@server.local/Data",
                "SMB directory delete works for admin@server.local/Data",
                "SMB final cleanup check passed for admin@server.local/Data",
            ],
        )

    def test_check_authenticated_smb_file_ops_returns_last_pass_result(self) -> None:
        with mock.patch(
            "timecapsulesmb.checks.smb.check_authenticated_smb_file_ops_detailed",
            return_value=[
                mock.Mock(status="PASS", message="step1"),
                mock.Mock(status="PASS", message="step2"),
            ],
        ):
            result = check_authenticated_smb_file_ops("admin", "pw", "server.local", "Data")
        self.assertEqual(result.status, "PASS")
        self.assertEqual(result.message, "step2")

    def test_check_authenticated_smb_listing_uses_neutral_smbclient_config(self) -> None:
        captured_args = None

        def fake_run_local_capture(args, timeout=20):
            nonlocal captured_args
            captured_args = args
            return subprocess.CompletedProcess(args, 0, "Data\n", "")

        with mock.patch("timecapsulesmb.checks.smb.command_exists", return_value=True):
            with mock.patch("timecapsulesmb.checks.smb.run_local_capture", side_effect=fake_run_local_capture):
                result = check_authenticated_smb_listing("admin", "pw", "server.local", expected_share_name="Data")
        self.assertEqual(result.status, "PASS")
        self.assertIsNotNone(captured_args)
        self.assertEqual(captured_args[:3], ["smbclient", "-s", "/dev/null"])

    def test_check_authenticated_smb_listing_places_custom_port_before_dash_l_target(self) -> None:
        captured_args = None

        def fake_run_local_capture(args, timeout=20):
            nonlocal captured_args
            captured_args = args
            return subprocess.CompletedProcess(args, 0, "Data\n", "")

        with mock.patch("timecapsulesmb.checks.smb.command_exists", return_value=True):
            with mock.patch("timecapsulesmb.checks.smb.run_local_capture", side_effect=fake_run_local_capture):
                result = check_authenticated_smb_listing(
                    "admin",
                    "pw",
                    "127.0.0.1",
                    expected_share_name="Data",
                    port=1445,
                )
        self.assertEqual(result.status, "PASS")
        self.assertEqual(
            captured_args,
            ["smbclient", "-s", "/dev/null", "-g", "-p", "1445", "-L", "//127.0.0.1", "-U", "admin%pw"],
        )

    def test_try_authenticated_smb_listing_forwards_custom_port(self) -> None:
        captured_args = None

        def fake_run_local_capture(args, timeout=12):
            nonlocal captured_args
            captured_args = args
            return subprocess.CompletedProcess(args, 0, "Data\n", "")

        with mock.patch("timecapsulesmb.checks.smb.command_exists", return_value=True):
            with mock.patch("timecapsulesmb.checks.smb.run_local_capture", side_effect=fake_run_local_capture):
                result = try_authenticated_smb_listing("admin", "pw", ["127.0.0.1"], port=2445)
        self.assertEqual(result.status, "PASS")
        self.assertEqual(captured_args[3:6], ["-g", "-p", "2445"])

    def test_extract_nbns_response_ip_reads_first_answer_ipv4(self) -> None:
        packet = (
            b"\x13\x37\x85\x00\x00\x01\x00\x01\x00\x00\x00\x00"
            + b"\x20" + b"FEEFFDFECACACACACACACACACACACAAA" + b"\x00"
            + b"\x00\x20\x00\x01"
            + b"\xc0\x0c\x00\x20\x00\x01\x00\x00\x01,\x00\x06\x00\x00"
            + b"\xc0\xa8\x01\xd9"
        )
        self.assertEqual(extract_nbns_response_ip(packet), "192.168.1.217")

    def test_extract_nbns_response_ip_returns_none_for_truncated_name(self) -> None:
        packet = (
            b"\x13\x37\x85\x00\x00\x01\x00\x01\x00\x00\x00\x00"
            + b"\x20" + b"FEEFFDFECACACACACACACACACACACAAA"
        )
        self.assertIsNone(extract_nbns_response_ip(packet))

    def test_extract_nbns_response_ip_returns_none_for_truncated_answer_header(self) -> None:
        packet = (
            b"\x13\x37\x85\x00\x00\x01\x00\x01\x00\x00\x00\x00"
            + b"\x20" + b"FEEFFDFECACACACACACACACACACACAAA" + b"\x00"
            + b"\x00\x20\x00\x01"
            + b"\xc0\x0c\x00\x20\x00\x01\x00\x00"
        )
        self.assertIsNone(extract_nbns_response_ip(packet))

    def test_build_nbns_query_has_expected_header_and_question(self) -> None:
        packet = build_nbns_query("TimeCapsule", transaction_id=0x1337)
        self.assertEqual(packet[:2], b"\x13\x37")
        self.assertEqual(packet[2:4], b"\x00\x00")
        self.assertEqual(packet[4:6], b"\x00\x01")
        self.assertEqual(packet[-4:], b"\x00\x20\x00\x01")

    def test_check_nbns_name_resolution_reports_timeout(self) -> None:
        fake_sock = mock.Mock()
        fake_sock.recvfrom.side_effect = TimeoutError()
        with mock.patch("timecapsulesmb.checks.nbns.socket.socket", return_value=fake_sock):
            result = check_nbns_name_resolution("TimeCapsule", "192.168.1.217", "192.168.1.217")
        self.assertEqual(result.status, "FAIL")
        self.assertIn("timed out", result.message)

    def test_check_nbns_name_resolution_reports_success(self) -> None:
        fake_sock = mock.Mock()
        fake_sock.recvfrom.return_value = (
            b"\x13\x37\x85\x00\x00\x01\x00\x01\x00\x00\x00\x00"
            + b"\x20" + b"FEEFFDFECACACACACACACACACACACAAA" + b"\x00"
            + b"\x00\x20\x00\x01"
            + b"\xc0\x0c\x00\x20\x00\x01\x00\x00\x01,\x00\x06\x00\x00"
            + b"\xc0\xa8\x01\xd9",
            ("192.168.1.217", 137),
        )
        with mock.patch("timecapsulesmb.checks.nbns.socket.socket", return_value=fake_sock):
            result = check_nbns_name_resolution("TimeCapsule", "192.168.1.217", "192.168.1.217")
        self.assertEqual(result.status, "PASS")
        self.assertIn("192.168.1.217", result.message)
        fake_sock.sendto.assert_called_once()

    def test_check_nbns_name_resolution_reports_wrong_ip(self) -> None:
        fake_sock = mock.Mock()
        fake_sock.recvfrom.return_value = (
            b"\x13\x37\x85\x00\x00\x01\x00\x01\x00\x00\x00\x00"
            + b"\x20" + b"FEEFFDFECACACACACACACACACACACAAA" + b"\x00"
            + b"\x00\x20\x00\x01"
            + b"\xc0\x0c\x00\x20\x00\x01\x00\x00\x01,\x00\x06\x00\x00"
            + b"\xc0\xa8\x01\x10",
            ("192.168.1.217", 137),
        )
        with mock.patch("timecapsulesmb.checks.nbns.socket.socket", return_value=fake_sock):
            result = check_nbns_name_resolution("TimeCapsule", "192.168.1.217", "192.168.1.217")
        self.assertEqual(result.status, "FAIL")
        self.assertIn("resolved to 192.168.1.16", result.message)

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
            "TC_AIRPORT_SYAP": "119",
        }
        with mock.patch("timecapsulesmb.checks.doctor.check_required_local_tools", return_value=[]):
            with mock.patch("timecapsulesmb.checks.doctor.check_required_artifacts", return_value=[]):
                with mock.patch("timecapsulesmb.checks.doctor.check_ssh_login", return_value=mock.Mock(status="PASS", message="ssh ok")):
                    with mock.patch("timecapsulesmb.checks.doctor.check_smb_port", return_value=mock.Mock(status="PASS", message="445 ok")):
                        with mock.patch("timecapsulesmb.checks.doctor.run_bonjour_checks", return_value=([], None, None)):
                            with mock.patch("timecapsulesmb.checks.doctor.check_authenticated_smb_listing", return_value=mock.Mock(status="PASS", message="listing ok")):
                                with mock.patch("timecapsulesmb.checks.doctor.check_authenticated_smb_file_ops_detailed", return_value=[mock.Mock(status="PASS", message="file ops ok")]):
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
            "TC_AIRPORT_SYAP": "119",
        }
        with mock.patch("timecapsulesmb.checks.doctor.check_required_local_tools", return_value=[]):
            with mock.patch("timecapsulesmb.checks.doctor.check_required_artifacts", return_value=[]):
                with mock.patch("timecapsulesmb.checks.doctor.check_ssh_login", return_value=mock.Mock(status="PASS", message="ssh ok")):
                    with mock.patch("timecapsulesmb.checks.doctor.check_smb_port", return_value=mock.Mock(status="PASS", message="445 ok")):
                        with mock.patch("timecapsulesmb.checks.doctor.run_bonjour_checks", return_value=([], None, None)):
                            with mock.patch("timecapsulesmb.checks.doctor.check_authenticated_smb_listing", return_value=mock.Mock(status="PASS", message="listing ok")):
                                with mock.patch("timecapsulesmb.checks.doctor.check_authenticated_smb_file_ops_detailed", return_value=[mock.Mock(status="PASS", message="file ops ok")]):
                                    with mock.patch("timecapsulesmb.checks.doctor.discover_volume_root", return_value="/Volumes/dk2"):
                                        with mock.patch("timecapsulesmb.checks.doctor._managed_mdns_takeover_ready", return_value=True):
                                            with mock.patch(
                                                "timecapsulesmb.checks.doctor.run_ssh",
                                                side_effect=[
                                                    mock.Mock(returncode=0, stdout=""),
                                                    mock.Mock(stdout="[global]\n    netbios name = TimeCapsule\nxattr_tdb:file = /Volumes/dk2/samba4/private/xattr.tdb\n[Data]\n"),
                                                    mock.Mock(stdout="xattr_tdb:file = /Volumes/dk2/samba4/private/xattr.tdb\n"),
                                                    mock.Mock(stdout="enabled\n"),
                                                    mock.Mock(stdout="192.168.1.217\n"),
                                                ],
                                            ) as run_ssh_mock:
                                                with mock.patch("timecapsulesmb.checks.doctor.check_nbns_name_resolution", return_value=mock.Mock(status="PASS", message="nbns ok")) as nbns_mock:
                                                    results, fatal = run_doctor_checks(values, env_exists=True, repo_root=REPO_ROOT)
        self.assertFalse(fatal)
        nbns_result = next(result for result in results if result.message == "nbns ok")
        self.assertEqual(nbns_result.status, "PASS")
        nbns_index = results.index(nbns_result)
        listing_index = next(i for i, result in enumerate(results) if result.message == "listing ok")
        self.assertLess(nbns_index, listing_index)
        self.assertEqual(run_ssh_mock.call_count, 5)
        nbns_mock.assert_called_once_with("TimeCapsule", "10.0.0.2", "192.168.1.217")

    def test_run_doctor_checks_resolves_nbns_expected_ip_from_hostname(self) -> None:
        values = {
            "TC_HOST": "root@timecapsule.local",
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
            "TC_AIRPORT_SYAP": "119",
        }
        with mock.patch("timecapsulesmb.checks.doctor.check_required_local_tools", return_value=[]):
            with mock.patch("timecapsulesmb.checks.doctor.check_required_artifacts", return_value=[]):
                with mock.patch("timecapsulesmb.checks.doctor.check_ssh_login", return_value=mock.Mock(status="PASS", message="ssh ok")):
                    with mock.patch("timecapsulesmb.checks.doctor.check_smb_port", return_value=mock.Mock(status="PASS", message="445 ok")):
                        with mock.patch("timecapsulesmb.checks.doctor.run_bonjour_checks", return_value=([], None, None)):
                            with mock.patch("timecapsulesmb.checks.doctor.check_authenticated_smb_listing", return_value=mock.Mock(status="PASS", message="listing ok")):
                                with mock.patch("timecapsulesmb.checks.doctor.check_authenticated_smb_file_ops_detailed", return_value=[mock.Mock(status="PASS", message="file ops ok")]):
                                    with mock.patch("timecapsulesmb.checks.doctor.discover_volume_root", return_value="/Volumes/dk2"):
                                        with mock.patch("timecapsulesmb.checks.doctor._managed_mdns_takeover_ready", return_value=True):
                                            with mock.patch(
                                                "timecapsulesmb.checks.doctor.run_ssh",
                                                side_effect=[
                                                    mock.Mock(returncode=0, stdout=""),
                                                    mock.Mock(stdout="[global]\n    netbios name = TimeCapsule\nxattr_tdb:file = /Volumes/dk2/samba4/private/xattr.tdb\n[Data]\n"),
                                                    mock.Mock(stdout="xattr_tdb:file = /Volumes/dk2/samba4/private/xattr.tdb\n"),
                                                    mock.Mock(stdout="enabled\n"),
                                                    mock.Mock(stdout="192.168.1.217\n"),
                                                ],
                                            ):
                                                with mock.patch("timecapsulesmb.checks.doctor.check_nbns_name_resolution", return_value=mock.Mock(status="PASS", message="nbns ok")) as nbns_mock:
                                                    results, fatal = run_doctor_checks(values, env_exists=True, repo_root=REPO_ROOT)
        self.assertFalse(fatal)
        self.assertEqual(next(result for result in results if result.message == "nbns ok").status, "PASS")
        nbns_mock.assert_called_once_with("TimeCapsule", "timecapsule.local", "192.168.1.217")

    def test_run_doctor_checks_uses_interface_ip_for_nbns_expected_ip(self) -> None:
        values = {
            "TC_HOST": "root@wan.example.com",
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
            "TC_AIRPORT_SYAP": "119",
        }
        with mock.patch("timecapsulesmb.checks.doctor.check_required_local_tools", return_value=[]):
            with mock.patch("timecapsulesmb.checks.doctor.check_required_artifacts", return_value=[]):
                with mock.patch("timecapsulesmb.checks.doctor.check_ssh_login", return_value=mock.Mock(status="PASS", message="ssh ok")):
                    with mock.patch("timecapsulesmb.checks.doctor.check_smb_port", return_value=mock.Mock(status="PASS", message="445 ok")):
                        with mock.patch("timecapsulesmb.checks.doctor.run_bonjour_checks", return_value=([], None, None)):
                            with mock.patch("timecapsulesmb.checks.doctor.check_authenticated_smb_listing", return_value=mock.Mock(status="PASS", message="listing ok")):
                                with mock.patch("timecapsulesmb.checks.doctor.check_authenticated_smb_file_ops_detailed", return_value=[mock.Mock(status="PASS", message="file ops ok")]):
                                    with mock.patch("timecapsulesmb.checks.doctor.discover_volume_root", return_value="/Volumes/dk2"):
                                        with mock.patch("timecapsulesmb.checks.doctor._managed_mdns_takeover_ready", return_value=True):
                                            with mock.patch(
                                                "timecapsulesmb.checks.doctor.run_ssh",
                                                side_effect=[
                                                    mock.Mock(returncode=0, stdout=""),
                                                    mock.Mock(stdout="[global]\n    netbios name = TimeCapsule\nxattr_tdb:file = /Volumes/dk2/samba4/private/xattr.tdb\n[Data]\n"),
                                                    mock.Mock(stdout="xattr_tdb:file = /Volumes/dk2/samba4/private/xattr.tdb\n"),
                                                    mock.Mock(stdout="enabled\n"),
                                                    mock.Mock(stdout="192.168.1.217\n"),
                                                ],
                                            ):
                                                with mock.patch("timecapsulesmb.checks.doctor.check_nbns_name_resolution", return_value=mock.Mock(status="PASS", message="nbns ok")) as nbns_mock:
                                                    results, fatal = run_doctor_checks(values, env_exists=True, repo_root=REPO_ROOT)
        self.assertFalse(fatal)
        self.assertEqual(next(result for result in results if result.message == "nbns ok").status, "PASS")
        nbns_mock.assert_called_once_with("TimeCapsule", "wan.example.com", "192.168.1.217")

    def test_run_doctor_checks_warns_when_nbns_marker_probe_fails(self) -> None:
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
            "TC_AIRPORT_SYAP": "119",
        }
        with mock.patch("timecapsulesmb.checks.doctor.check_required_local_tools", return_value=[]):
            with mock.patch("timecapsulesmb.checks.doctor.check_required_artifacts", return_value=[]):
                with mock.patch("timecapsulesmb.checks.doctor.check_ssh_login", return_value=mock.Mock(status="PASS", message="ssh ok")):
                    with mock.patch("timecapsulesmb.checks.doctor.check_smb_port", return_value=mock.Mock(status="PASS", message="445 ok")):
                        with mock.patch("timecapsulesmb.checks.doctor.run_bonjour_checks", return_value=([], None, None)):
                            with mock.patch("timecapsulesmb.checks.doctor.check_authenticated_smb_listing", return_value=mock.Mock(status="PASS", message="listing ok")):
                                with mock.patch("timecapsulesmb.checks.doctor.check_authenticated_smb_file_ops_detailed", return_value=[mock.Mock(status="PASS", message="file ops ok")]):
                                    with mock.patch("timecapsulesmb.checks.doctor._remote_interface_exists", return_value=True):
                                        with mock.patch("timecapsulesmb.checks.doctor._managed_mdns_takeover_ready", return_value=True):
                                            with mock.patch("timecapsulesmb.checks.doctor.discover_volume_root", side_effect=RuntimeError("volume probe failed")):
                                                results, fatal = run_doctor_checks(values, env_exists=True, repo_root=REPO_ROOT)
        self.assertFalse(fatal)
        nbns_result = next(result for result in results if result.status == "WARN" and result.message.startswith("NBNS check skipped:"))
        self.assertIn("volume probe failed", nbns_result.message)

    def test_run_doctor_checks_warns_when_nbns_marker_probe_raises_system_exit(self) -> None:
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
            "TC_AIRPORT_SYAP": "119",
        }
        with mock.patch("timecapsulesmb.checks.doctor.check_required_local_tools", return_value=[]):
            with mock.patch("timecapsulesmb.checks.doctor.check_required_artifacts", return_value=[]):
                with mock.patch("timecapsulesmb.checks.doctor.check_ssh_login", return_value=mock.Mock(status="PASS", message="ssh ok")):
                    with mock.patch("timecapsulesmb.checks.doctor.check_smb_port", return_value=mock.Mock(status="PASS", message="445 ok")):
                        with mock.patch("timecapsulesmb.checks.doctor.run_bonjour_checks", return_value=([], None, None)):
                            with mock.patch("timecapsulesmb.checks.doctor.check_authenticated_smb_listing", return_value=mock.Mock(status="PASS", message="listing ok")):
                                with mock.patch("timecapsulesmb.checks.doctor.check_authenticated_smb_file_ops_detailed", return_value=[mock.Mock(status="PASS", message="file ops ok")]):
                                    with mock.patch("timecapsulesmb.checks.doctor._remote_interface_exists", return_value=True):
                                        with mock.patch("timecapsulesmb.checks.doctor._managed_mdns_takeover_ready", return_value=True):
                                            with mock.patch("timecapsulesmb.checks.doctor.discover_volume_root", side_effect=SystemExit("ssh failed")):
                                                results, fatal = run_doctor_checks(values, env_exists=True, repo_root=REPO_ROOT)
        self.assertFalse(fatal)
        nbns_result = next(result for result in results if result.status == "WARN" and result.message.startswith("NBNS check skipped:"))
        self.assertIn("ssh failed", nbns_result.message)

    def test_check_authenticated_smb_file_ops_detailed_passes_custom_port_to_smbclient(self) -> None:
        captured_args: list[list[str]] = []

        def fake_run_local_capture(args, timeout=15):
            captured_args.append(args)
            command_text = args[-1]
            if 'get "sample.txt"' in command_text:
                download_target = Path(command_text.split('get "sample.txt" "', 1)[1].split('"', 1)[0])
                download_target.write_text("line1\nline2\nline3\nline4-updated\n", encoding="utf-8")
                return subprocess.CompletedProcess(args, 0, "", "")
            if 'get "sample-renamed.txt"' in command_text and 'get "sample-copy.txt"' in command_text:
                renamed_target = Path(command_text.split('get "sample-renamed.txt" "', 1)[1].split('"', 1)[0])
                copy_target = Path(command_text.split('get "sample-copy.txt" "', 1)[1].split('"', 1)[0])
                renamed_target.write_text("line1\nline2\nline3\nline4-updated\n", encoding="utf-8")
                copy_target.write_text("line1\nline2\nline3\nline4-updated\n", encoding="utf-8")
                return subprocess.CompletedProcess(args, 0, "", "")
            if 'del "sample-copy.txt"; ls' in command_text:
                return subprocess.CompletedProcess(args, 0, "sample-renamed.txt\n", "")
            if command_text == "ls":
                return subprocess.CompletedProcess(args, 0, "Public\n", "")
            return subprocess.CompletedProcess(args, 0, "", "")

        with mock.patch("timecapsulesmb.checks.smb.command_exists", return_value=True):
            with mock.patch("timecapsulesmb.checks.smb.run_local_capture", side_effect=fake_run_local_capture):
                results = check_authenticated_smb_file_ops_detailed("admin", "pw", "127.0.0.1", "Data", port=3445)
        self.assertEqual(len(results), 10)
        self.assertTrue(all(args[:5] == ["smbclient", "-s", "/dev/null", "-p", "3445"] for args in captured_args))

    def test_run_doctor_checks_proxy_target_reports_tunnel_failure_as_fatal(self) -> None:
        values = {
            "TC_HOST": "root@192.168.1.118",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o ProxyCommand=ssh\\ -W\\ %h:%p\\ bastion",
            "TC_NET_IFACE": "bridge0",
            "TC_SHARE_NAME": "Data",
            "TC_SAMBA_USER": "admin",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule",
            "TC_AIRPORT_SYAP": "119",
        }
        with mock.patch("timecapsulesmb.checks.doctor.check_required_local_tools", return_value=[]):
            with mock.patch("timecapsulesmb.checks.doctor.check_required_artifacts", return_value=[]):
                with mock.patch("timecapsulesmb.checks.doctor.check_ssh_login", return_value=mock.Mock(status="PASS", message="ssh ok")):
                    with mock.patch("timecapsulesmb.checks.doctor.find_free_local_port", return_value=1445):
                        with mock.patch("timecapsulesmb.checks.doctor.ssh_local_forward", side_effect=SystemExit("tunnel failed")):
                            with mock.patch("timecapsulesmb.checks.doctor.discover_volume_root", return_value="/Volumes/dk2"):
                                with mock.patch("timecapsulesmb.checks.doctor.run_ssh", return_value=mock.Mock(stdout="")):
                                    results, fatal = run_doctor_checks(values, env_exists=True, repo_root=REPO_ROOT, skip_bonjour=True)
        self.assertTrue(fatal)
        smb_result = next(result for result in results if result.message.startswith("authenticated SMB checks failed through SSH tunnel:"))
        self.assertEqual(smb_result.status, "FAIL")
        self.assertIn("tunnel failed", smb_result.message)


if __name__ == "__main__":
    unittest.main()
