from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

import timecapsulesmb.device.probe as probe
from timecapsulesmb.device.probe import (
    preferred_interface_name,
    probe_remote_interface_candidates_conn,
    probe_remote_interface_conn,
    read_remote_network_diagnostics_conn,
    read_runtime_share_names_conn,
    read_runtime_log_tails_conn,
    runtime_startup_failure_debug_fields,
)
from timecapsulesmb.transport.ssh import SshConnection


class ProbeTests(unittest.TestCase):
    def test_read_runtime_share_names_conn_parses_shares_tsv(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "-o StrictHostKeyChecking=no")
        proc = subprocess.CompletedProcess(
            args=["ssh"],
            returncode=0,
            stdout="AirPort Disk\t/Volumes/dk2/ShareRoot\nBackup (dk3)\t/Volumes/dk3\n",
        )

        with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=proc) as run_ssh_mock:
            result = read_runtime_share_names_conn(connection)

        self.assertEqual(result, ["AirPort Disk", "Backup (dk3)"])
        run_ssh_mock.assert_called_once()
        args, kwargs = run_ssh_mock.call_args
        self.assertEqual(args[0], connection)
        self.assertIn(probe.RUNTIME_SHARES_TSV, args[1])
        self.assertFalse(kwargs["check"])
        self.assertEqual(kwargs["timeout"], probe.REMOTE_STATE_PROBE_TIMEOUT_SECONDS)

    def test_read_runtime_share_names_conn_ignores_non_tsv_output(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "-o StrictHostKeyChecking=no")
        proc = subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="enabled\n\n")

        with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=proc):
            result = read_runtime_share_names_conn(connection)

        self.assertEqual(result, [])

    def test_read_runtime_log_tails_conn_fetches_ram_and_payload_logs_with_short_timeout(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "-o StrictHostKeyChecking=no")

        def fake_run_ssh(
            _connection: SshConnection,
            remote_cmd: str,
            **_kwargs: object,
        ) -> subprocess.CompletedProcess[str]:
            if "payload.tsv" in remote_cmd:
                return subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="/Volumes/dk2/.samba4\n", stderr="")
            if "rc.local.log" in remote_cmd:
                return subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="rc log\n", stderr="")
            if "watchdog.log" in remote_cmd:
                return subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="watchdog log\n", stderr="")
            if "mdns.log" in remote_cmd:
                return subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="mdns log\n", stderr="")
            if "nbns.log" in remote_cmd:
                return subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="nbns log\n", stderr="")
            if "log.smbd" in remote_cmd:
                return subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="smbd log\n", stderr="")
            self.fail(f"unexpected remote command: {remote_cmd}")

        with mock.patch("timecapsulesmb.device.probe.run_ssh", side_effect=fake_run_ssh) as run_ssh_mock:
            logs = read_runtime_log_tails_conn(connection)

        self.assertEqual(logs["remote_rc_local_log_tail"], "rc log")
        self.assertEqual(logs["remote_payload_log_dir"], "/Volumes/dk2/.samba4")
        self.assertEqual(logs["remote_watchdog_log_tail"], "watchdog log")
        self.assertEqual(logs["remote_mdns_log_tail"], "mdns log")
        self.assertEqual(logs["remote_nbns_log_tail"], "nbns log")
        self.assertEqual(logs["remote_smbd_log_tail"], "smbd log")
        self.assertEqual(run_ssh_mock.call_count, 6)
        for call in run_ssh_mock.call_args_list:
            args, kwargs = call
            self.assertEqual(args[0], connection)
            self.assertFalse(kwargs["check"])
            self.assertEqual(kwargs["timeout"], probe.REMOTE_LOG_TAIL_TIMEOUT_SECONDS)

    def test_read_remote_service_socket_diagnostics_conn_scopes_fstat_to_service_processes(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "-o StrictHostKeyChecking=no")
        stdout = "smbd:\nroot smbd 101 10 internet stream tcp 0x0 *:445\nnbns-advertiser:\n(no internet sockets reported)\n"
        proc = subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout=stdout)

        with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=proc) as run_ssh_mock:
            result = probe.read_remote_service_socket_diagnostics_conn(connection)

        self.assertEqual(result, stdout.strip())
        args, kwargs = run_ssh_mock.call_args
        self.assertEqual(args[0], connection)
        self.assertIn('capture_fstat_for_ucomm "$ps_out" "$proc_name"', args[1])
        self.assertIn("for proc_name in smbd nbns-advertiser", args[1])
        self.assertIn("/internet/p", args[1])
        self.assertFalse(kwargs["check"])
        self.assertEqual(kwargs["timeout"], probe.REMOTE_STATE_PROBE_TIMEOUT_SECONDS)

    def test_runtime_startup_failure_debug_fields_classifies_network_ipv4_timeout(self) -> None:
        fields = runtime_startup_failure_debug_fields(
            {
                "remote_rc_local_log_tail": (
                    "rc.local: managed Samba boot startup beginning\n"
                    "rc.local: timed out waiting for IPv4 on bridge0\n"
                    "rc.local: network startup failed: could not determine bridge0 IPv4 address\n"
                )
            }
        )

        self.assertEqual(
            fields,
            {
                "runtime_startup_failure": "network_ipv4_timeout",
                "runtime_startup_failed_iface": "bridge0",
            },
        )

    def test_read_remote_network_diagnostics_conn_summarizes_configured_iface_and_candidates(self) -> None:
        connection = SshConnection("root@169.254.44.9", "pw", "-o StrictHostKeyChecking=no")
        stdout = """\
NET_IFACE=bridge0
TC_DIAG_BEGIN target_ifconfig
bridge0: flags=ffffe043<UP,BROADCAST,RUNNING,LINK1,LINK2,MULTICAST> metric 0 mtu 1500
\tstatus: active
TC_DIAG_END target_ifconfig
TC_DIAG_BEGIN ifconfig_a
bcmeth1: flags=ffffe843<UP,BROADCAST,RUNNING,SIMPLEX,LINK1,LINK2,MULTICAST> metric 0 mtu 1500
\tinet 169.254.44.9 netmask 0xffff0000 broadcast 169.254.255.255
\tstatus: active
bridge0: flags=ffffe043<UP,BROADCAST,RUNNING,LINK1,LINK2,MULTICAST> metric 0 mtu 1500
\tstatus: active
TC_DIAG_END ifconfig_a
TC_DIAG_BEGIN routes
default 169.254.0.1
TC_DIAG_END routes
"""
        proc = subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout=stdout, stderr="")

        with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=proc) as run_ssh_mock:
            diagnostics = read_remote_network_diagnostics_conn(connection)

        self.assertEqual(diagnostics["remote_network_config"], {"NET_IFACE": "bridge0", "ssh_target_host": "169.254.44.9"})
        self.assertEqual(diagnostics["remote_network_probe_rc"], 0)
        self.assertIn("bridge0: flags=", str(diagnostics["remote_network_target_ifconfig"]))
        self.assertEqual(diagnostics["remote_network_target_ip_matches"], ["bcmeth1"])
        self.assertEqual(diagnostics["remote_network_preferred_iface"], "bcmeth1")
        self.assertEqual(diagnostics["remote_network_failure_hint"], "configured interface bridge0 has no IPv4 address")
        self.assertEqual(
            diagnostics["remote_network_ipv4_interfaces"],
            [
                {"name": "bcmeth1", "ipv4": ["169.254.44.9"], "up": True, "active": True, "loopback": False},
                {"name": "bridge0", "ipv4": [], "up": True, "active": True, "loopback": False},
            ],
        )
        self.assertIn("default 169.254.0.1", str(diagnostics["remote_network_routes"]))
        args, kwargs = run_ssh_mock.call_args
        self.assertEqual(args[0], connection)
        self.assertIn("/sbin/ifconfig -a", args[1])
        self.assertIn("/mnt/Flash/tcapsulesmb.conf", args[1])
        self.assertFalse(kwargs["check"])
        self.assertEqual(kwargs["timeout"], probe.REMOTE_NETWORK_DIAGNOSTICS_TIMEOUT_SECONDS)

    def test_probe_remote_interface_conn_uses_connection_wrapper_without_old_positional_shape(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "-o StrictHostKeyChecking=no")
        proc = subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="bridge0\n")

        with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=proc) as run_ssh_mock:
            result = probe_remote_interface_conn(connection, "bridge0")

        self.assertTrue(result.exists)
        run_ssh_mock.assert_called_once()
        args, kwargs = run_ssh_mock.call_args
        self.assertEqual(args[0], connection)
        self.assertEqual(len(args), 2)
        self.assertFalse(kwargs["check"])

    def test_probe_device_conn_uses_connection_wrapper_for_remote_probe_sequence(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "-o StrictHostKeyChecking=no")

        def fake_run_ssh(_connection: SshConnection, remote_cmd: str, **_kwargs: object) -> subprocess.CompletedProcess[str]:
            if "uname -s" in remote_cmd:
                return subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="NetBSD\n6.0\nearmv4\n")
            if "bs=1 skip=5" in remote_cmd:
                return subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="little\n")
            if "/usr/bin/acp syAP syAM" in remote_cmd:
                return subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="syAP=0x00000077\nsyAM=TimeCapsule8,119\n")
            self.fail(f"unexpected remote command: {remote_cmd}")

        with mock.patch("timecapsulesmb.device.probe.tcp_open", return_value=True):
            with mock.patch("timecapsulesmb.device.probe.run_ssh", side_effect=fake_run_ssh) as run_ssh_mock:
                result = probe.probe_device_conn(connection)

        self.assertTrue(result.ssh_authenticated)
        self.assertEqual(result.os_name, "NetBSD")
        self.assertEqual(result.elf_endianness, "little")
        self.assertEqual(result.airport_model, "TimeCapsule8,119")
        self.assertEqual(run_ssh_mock.call_count, 3)
        for call in run_ssh_mock.call_args_list:
            args, _kwargs = call
            self.assertEqual(args[0], connection)
            self.assertEqual(len(args), 2)

    def test_probe_remote_os_info_conn_ignores_ssh_client_preamble(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "-o StrictHostKeyChecking=no")
        proc = subprocess.CompletedProcess(
            args=["ssh"],
            returncode=0,
            stdout=(
                "Warning: No xauth data; using fake authentication data for X11 forwarding.\n"
                "X11 forwarding request failed on channel 0.\n"
                "NetBSD\n"
                "4.0_STABLE\n"
                "earmv4\n"
            ),
        )

        with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=proc):
            result = probe._probe_remote_os_info_conn(connection)

        self.assertEqual(result, ("NetBSD", "4.0_STABLE", "earmv4"))

    def test_probe_remote_elf_endianness_uses_dd_and_sed_only(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "-o StrictHostKeyChecking=no")
        proc = subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="\\001$\nlittle\n")

        with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=proc) as run_ssh_mock:
            result = probe._probe_remote_elf_endianness_conn(connection)

        self.assertEqual(result, "little")
        remote_cmd = run_ssh_mock.call_args.args[1]
        self.assertIn("/bin/dd", remote_cmd)
        self.assertIn("/usr/bin/sed -n l", remote_cmd)
        self.assertNotIn("/usr/bin/tr", remote_cmd)
        self.assertNotIn("/usr/bin/od", remote_cmd)

    def test_extract_airport_identity_from_text_finds_airport_extreme_model(self) -> None:
        result = probe.extract_airport_identity_from_text("prefix\x00psyAM\x00pAirPort7,120\x00suffix")
        self.assertEqual(result.model, "AirPort7,120")
        self.assertEqual(result.syap, "120")
        self.assertIn("AirPort7,120", result.detail)

    def test_probe_remote_interface_candidates_prefers_bridge0_with_private_ipv4(self) -> None:
        ifconfig_output = """
gec0: flags=eb43<UP,BROADCAST,RUNNING,PROMISC,ALLMULTI,SIMPLEX,LINK1,LINK2,MULTICAST> metric 0 mtu 1500
\tstatus: active
bridge0: flags=e043<UP,BROADCAST,RUNNING,LINK1,LINK2,MULTICAST> metric 0 mtu 1500
\tinet 192.168.1.72 netmask 0xffffff00 broadcast 192.168.1.255
\tinet 169.254.117.175 netmask 0xffff0000 broadcast 169.254.255.255
\tstatus: active
lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> metric 0 mtu 33172
\tinet 127.0.0.1 netmask 0xff000000
"""
        connection = SshConnection("root@10.0.0.2", "pw", "")
        proc = subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout=ifconfig_output)
        with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=proc):
            result = probe_remote_interface_candidates_conn(connection)
        self.assertEqual(result.preferred_iface, "bridge0")
        self.assertEqual([candidate.name for candidate in result.candidates], ["gec0", "bridge0", "lo0"])

    def test_probe_remote_interface_candidates_prefers_bcmeth1_when_bridge0_has_no_ipv4(self) -> None:
        ifconfig_output = """
bcmeth1: flags=ffffe843<UP,BROADCAST,RUNNING,SIMPLEX,LINK1,LINK2,MULTICAST> metric 0 mtu 1500
\tinet 10.0.1.1 netmask 0xffffff00 broadcast 10.0.1.255
\tstatus: active
bcmeth0: flags=ffffe843<UP,BROADCAST,RUNNING,SIMPLEX,LINK1,LINK2,MULTICAST> metric 0 mtu 1500
\tstatus: active
bridge0: flags=ffffe043<UP,BROADCAST,RUNNING,LINK1,LINK2,MULTICAST> metric 0 mtu 1500
\tstatus: active
"""
        connection = SshConnection("root@10.0.0.2", "pw", "")
        proc = subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout=ifconfig_output)
        with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=proc):
            result = probe_remote_interface_candidates_conn(connection)
        self.assertEqual(result.preferred_iface, "bcmeth1")

    def test_probe_remote_interface_candidates_ignores_loopback_only_output(self) -> None:
        ifconfig_output = """
lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> metric 0 mtu 33172
\tinet 127.0.0.1 netmask 0xff000000
"""
        connection = SshConnection("root@10.0.0.2", "pw", "")
        proc = subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout=ifconfig_output)
        with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=proc):
            result = probe_remote_interface_candidates_conn(connection)
        self.assertIsNone(result.preferred_iface)
        self.assertIn("no non-loopback IPv4 interface candidates found", result.detail)

    def test_preferred_interface_name_uses_target_ip_before_generic_ranking(self) -> None:
        ifconfig_output = """
bcmeth1: flags=ffffe843<UP,BROADCAST,RUNNING,SIMPLEX,LINK1,LINK2,MULTICAST> metric 0 mtu 1500
\tinet 10.0.1.1 netmask 0xffffff00 broadcast 10.0.1.255
\tstatus: active
bridge0: flags=ffffe043<UP,BROADCAST,RUNNING,LINK1,LINK2,MULTICAST> metric 0 mtu 1500
\tinet 192.168.1.217 netmask 0xffffff00 broadcast 192.168.1.255
\tstatus: active
"""
        connection = SshConnection("root@10.0.0.2", "pw", "")
        proc = subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout=ifconfig_output)
        with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=proc):
            result = probe_remote_interface_candidates_conn(connection)
        self.assertEqual(result.preferred_iface, "bridge0")
        self.assertEqual(preferred_interface_name(result.candidates, target_ips=("10.0.1.1",)), "bcmeth1")

    def test_probe_remote_interface_candidates_reports_target_ip_matches(self) -> None:
        ifconfig_output = """
bcmeth1: flags=ffffe843<UP,BROADCAST,RUNNING,SIMPLEX,LINK1,LINK2,MULTICAST> metric 0 mtu 1500
\tinet 192.168.168.111 netmask 0xffffff00 broadcast 192.168.168.255
\tstatus: active
bridge0: flags=ffffe043<UP,BROADCAST,RUNNING,LINK1,LINK2,MULTICAST> metric 0 mtu 1500
\tinet 169.254.117.175 netmask 0xffff0000 broadcast 169.254.255.255
\tstatus: active
lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> metric 0 mtu 33172
\tinet 127.0.0.1 netmask 0xff000000
"""
        connection = SshConnection("root@10.0.0.2", "pw", "")
        proc = subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout=ifconfig_output)
        with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=proc):
            result = probe_remote_interface_candidates_conn(connection, target_ips=("192.168.168.111",))
        self.assertEqual(result.preferred_iface, "bcmeth1")
        self.assertEqual([candidate.name for candidate in result.target_ip_matches], ["bcmeth1"])

    def test_preferred_interface_name_private_ipv4_beats_link_local_without_target_ip(self) -> None:
        ifconfig_output = """
bcmeth1: flags=ffffe843<UP,BROADCAST,RUNNING,SIMPLEX,LINK1,LINK2,MULTICAST> metric 0 mtu 1500
\tinet 169.254.44.9 netmask 0xffff0000 broadcast 169.254.255.255
\tstatus: active
bridge0: flags=ffffe043<UP,BROADCAST,RUNNING,LINK1,LINK2,MULTICAST> metric 0 mtu 1500
\tinet 192.168.1.217 netmask 0xffffff00 broadcast 192.168.1.255
\tstatus: active
"""
        connection = SshConnection("root@10.0.0.2", "pw", "")
        proc = subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout=ifconfig_output)
        with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=proc):
            result = probe_remote_interface_candidates_conn(connection)
        self.assertEqual(result.preferred_iface, "bridge0")

    def test_preferred_interface_name_exact_link_local_target_can_win(self) -> None:
        ifconfig_output = """
bcmeth1: flags=ffffe843<UP,BROADCAST,RUNNING,SIMPLEX,LINK1,LINK2,MULTICAST> metric 0 mtu 1500
\tinet 169.254.44.9 netmask 0xffff0000 broadcast 169.254.255.255
\tstatus: active
bridge0: flags=ffffe043<UP,BROADCAST,RUNNING,LINK1,LINK2,MULTICAST> metric 0 mtu 1500
\tinet 192.168.1.217 netmask 0xffffff00 broadcast 192.168.1.255
\tstatus: active
"""
        connection = SshConnection("root@10.0.0.2", "pw", "")
        proc = subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout=ifconfig_output)
        with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=proc):
            result = probe_remote_interface_candidates_conn(connection)
        self.assertEqual(preferred_interface_name(result.candidates, target_ips=("169.254.44.9",)), "bcmeth1")

    def test_probe_remote_interface_candidates_preserves_multiple_private_candidates(self) -> None:
        ifconfig_output = """
bcmeth1: flags=ffffe843<UP,BROADCAST,RUNNING,SIMPLEX,LINK1,LINK2,MULTICAST> metric 0 mtu 1500
\tinet 10.0.1.1 netmask 0xffffff00 broadcast 10.0.1.255
\tstatus: active
bridge0: flags=ffffe043<UP,BROADCAST,RUNNING,LINK1,LINK2,MULTICAST> metric 0 mtu 1500
\tinet 192.168.1.217 netmask 0xffffff00 broadcast 192.168.1.255
\tstatus: active
"""
        connection = SshConnection("root@10.0.0.2", "pw", "")
        proc = subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout=ifconfig_output)
        with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=proc):
            result = probe_remote_interface_candidates_conn(connection)
        self.assertEqual(
            [(candidate.name, candidate.ipv4_addrs) for candidate in result.candidates],
            [("bcmeth1", ("10.0.1.1",)), ("bridge0", ("192.168.1.217",))],
        )
        self.assertEqual(preferred_interface_name(result.candidates), "bridge0")
        self.assertEqual(preferred_interface_name(result.candidates, target_ips=("10.0.1.1",)), "bcmeth1")
