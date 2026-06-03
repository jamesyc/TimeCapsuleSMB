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
    flash_runtime_config_present_conn,
    preferred_interface_name,
    read_deployed_version_conn,
    probe_remote_interface_conn,
    read_remote_network_diagnostics_conn,
    read_runtime_payload_dir_conn,
    read_runtime_log_tails_conn,
    runtime_ram_root_present_conn,
    runtime_startup_failure_debug_fields,
)
from timecapsulesmb.transport.ssh import SshConnection


class ProbeTests(unittest.TestCase):
    def test_read_runtime_payload_dir_conn_derives_payload_from_active_smb_conf_log_file(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "-o StrictHostKeyChecking=no")
        proc = subprocess.CompletedProcess(
            args=["ssh"],
            returncode=0,
            stdout="[global]\n    log file = /Volumes/dk2/.samba4/logs/log.smbd\n[Data]\n    path = /Volumes/dk2/ShareRoot\n",
        )

        with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=proc) as run_ssh_mock:
            result = read_runtime_payload_dir_conn(connection)

        self.assertEqual(result, "/Volumes/dk2/.samba4")
        run_ssh_mock.assert_called_once()
        args, kwargs = run_ssh_mock.call_args
        self.assertEqual(args[0], connection)
        self.assertIn(probe.RUNTIME_SMB_CONF, args[1])
        self.assertFalse(kwargs["check"])
        self.assertEqual(kwargs["timeout"], probe.REMOTE_STATE_PROBE_TIMEOUT_SECONDS)

    def test_read_deployed_version_conn_sources_flash_runtime_config(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "-o StrictHostKeyChecking=no")
        proc = subprocess.CompletedProcess(
            args=["ssh"],
            returncode=0,
            stdout="release_tag=v2.1.0-rc4b\ncli_version_code=20118\n",
        )

        with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=proc) as run_ssh_mock:
            result = read_deployed_version_conn(connection)

        self.assertEqual(result.release_tag, "v2.1.0-rc4b")
        self.assertEqual(result.cli_version_code, 20118)
        self.assertEqual(result.detail, "ok")
        args, kwargs = run_ssh_mock.call_args
        self.assertEqual(args[0], connection)
        self.assertIn(probe.FLASH_RUNTIME_CONFIG, args[1])
        self.assertIn("TC_DEPLOY_RELEASE_TAG", args[1])
        self.assertFalse(kwargs["check"])
        self.assertEqual(kwargs["timeout"], probe.REMOTE_STATE_PROBE_TIMEOUT_SECONDS)

    def test_flash_runtime_config_present_conn_returns_true_when_file_exists(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "-o StrictHostKeyChecking=no")
        proc = subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="")

        with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=proc) as run_ssh_mock:
            result = flash_runtime_config_present_conn(connection)

        self.assertTrue(result)
        args, kwargs = run_ssh_mock.call_args
        self.assertEqual(args[0], connection)
        self.assertIn(probe.FLASH_RUNTIME_CONFIG, args[1])
        self.assertIn("[ -f", args[1])
        self.assertFalse(kwargs["check"])
        self.assertEqual(kwargs["timeout"], probe.REMOTE_STATE_PROBE_TIMEOUT_SECONDS)

    def test_flash_runtime_config_present_conn_returns_false_when_file_is_missing(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "-o StrictHostKeyChecking=no")
        proc = subprocess.CompletedProcess(args=["ssh"], returncode=1, stdout="")

        with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=proc):
            result = flash_runtime_config_present_conn(connection)

        self.assertFalse(result)

    def test_read_deployed_version_conn_reports_missing_metadata(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "-o StrictHostKeyChecking=no")
        proc = subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="release_tag=\ncli_version_code=\n")

        with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=proc):
            result = read_deployed_version_conn(connection)

        self.assertIsNone(result.release_tag)
        self.assertIsNone(result.cli_version_code)
        self.assertEqual(result.detail, "missing version metadata")

    def test_runtime_ram_root_present_conn_returns_true_when_directory_exists(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "-o StrictHostKeyChecking=no")
        proc = subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="")

        with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=proc) as run_ssh_mock:
            result = runtime_ram_root_present_conn(connection)

        self.assertTrue(result)
        args, kwargs = run_ssh_mock.call_args
        self.assertEqual(args[0], connection)
        self.assertIn(probe.RUNTIME_RAM_ROOT, args[1])
        self.assertFalse(kwargs["check"])
        self.assertEqual(kwargs["timeout"], probe.REMOTE_STATE_PROBE_TIMEOUT_SECONDS)

    def test_runtime_ram_root_present_conn_returns_false_when_directory_is_missing(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "-o StrictHostKeyChecking=no")
        proc = subprocess.CompletedProcess(args=["ssh"], returncode=1, stdout="")

        with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=proc):
            result = runtime_ram_root_present_conn(connection)

        self.assertFalse(result)

    def test_read_runtime_log_tails_conn_fetches_ram_and_payload_logs_with_short_timeout(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "-o StrictHostKeyChecking=no")

        def fake_run_ssh(
            _connection: SshConnection,
            remote_cmd: str,
            **_kwargs: object,
        ) -> subprocess.CompletedProcess[str]:
            if "rc.local.log" in remote_cmd:
                return subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="rc log\n", stderr="")
            if "manager.log" in remote_cmd:
                return subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="manager log\n", stderr="")
            if probe.RUNTIME_SMB_CONF in remote_cmd:
                return subprocess.CompletedProcess(
                    args=["ssh"],
                    returncode=0,
                    stdout="[global]\n    log file = /Volumes/dk2/.samba4/logs/log.smbd\n[Data]\n    path = /Volumes/dk2/ShareRoot\n",
                    stderr="",
                )
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
        self.assertEqual(logs["remote_manager_log_tail"], "manager log")
        self.assertEqual(logs["remote_mdns_log_tail"], "mdns log")
        self.assertEqual(logs["remote_nbns_log_tail"], "nbns log")
        self.assertEqual(logs["remote_smbd_log_tail"], "smbd log")
        self.assertEqual(run_ssh_mock.call_count, 6)
        commands = [call.args[1] for call in run_ssh_mock.call_args_list]
        self.assertTrue(any("/Volumes/dk2/.samba4/logs/mdns.log" in command for command in commands))
        self.assertTrue(any("/Volumes/dk2/.samba4/logs/nbns.log" in command for command in commands))
        self.assertFalse(any("/mnt/Memory/samba4/var/mdns.log" in command for command in commands))
        self.assertFalse(any("/mnt/Memory/samba4/var/nbns.log" in command for command in commands))
        for call in run_ssh_mock.call_args_list:
            args, kwargs = call
            self.assertEqual(args[0], connection)
            self.assertFalse(kwargs["check"])
            self.assertEqual(kwargs["timeout"], probe.REMOTE_LOG_TAIL_TIMEOUT_SECONDS)

    def test_read_runtime_log_tails_conn_falls_back_to_ram_advertiser_logs_without_payload_state(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "-o StrictHostKeyChecking=no")

        def fake_run_ssh(
            _connection: SshConnection,
            remote_cmd: str,
            **_kwargs: object,
        ) -> subprocess.CompletedProcess[str]:
            if "rc.local.log" in remote_cmd:
                return subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="rc log\n", stderr="")
            if "manager.log" in remote_cmd:
                return subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="manager log\n", stderr="")
            if probe.RUNTIME_SMB_CONF in remote_cmd:
                return subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="[global]\n[Data]\n    path = /Volumes/dk2/ShareRoot\n", stderr="")
            if "/mnt/Memory/samba4/var/mdns.log" in remote_cmd:
                return subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="ram mdns log\n", stderr="")
            if "/mnt/Memory/samba4/var/nbns.log" in remote_cmd:
                return subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="ram nbns log\n", stderr="")
            self.fail(f"unexpected remote command: {remote_cmd}")

        with mock.patch("timecapsulesmb.device.probe.run_ssh", side_effect=fake_run_ssh) as run_ssh_mock:
            logs = read_runtime_log_tails_conn(connection)

        self.assertEqual(logs["remote_payload_log_dir"], f"(unavailable from active {probe.RUNTIME_SMB_CONF})")
        self.assertEqual(logs["remote_manager_log_tail"], "manager log")
        self.assertEqual(logs["remote_mdns_log_tail"], "ram mdns log")
        self.assertEqual(logs["remote_nbns_log_tail"], "ram nbns log")
        self.assertEqual(run_ssh_mock.call_count, 5)

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

    def test_runtime_startup_failure_debug_fields_classifies_auto_ip_unavailable(self) -> None:
        fields = runtime_startup_failure_debug_fields(
            {
                "remote_manager_log_tail": (
                    "manager: mDNS auto-ip check: no usable address yet\n"
                    "manager: mDNS startup deferred; no usable address has appeared yet\n"
                )
            }
        )

        self.assertEqual(
            fields,
            {
                "runtime_startup_failure": "network_auto_ip_unavailable",
                "runtime_startup_waiting_for_auto_ip": True,
            },
        )

    def test_runtime_startup_failure_debug_fields_classifies_probe_auto_ip_waiting_detail(self) -> None:
        fields = runtime_startup_failure_debug_fields(
            {},
            verification_detail="runtime verification timed out; mdns-advertiser is waiting for auto-IP",
        )

        self.assertEqual(fields["runtime_startup_failure"], "network_auto_ip_unavailable")

    def test_read_remote_network_diagnostics_conn_summarizes_live_candidates(self) -> None:
        connection = SshConnection("root@169.254.44.9", "pw", "-o StrictHostKeyChecking=no")
        stdout = """\
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

        self.assertEqual(diagnostics["remote_network_config"], {"ssh_target_host": "169.254.44.9"})
        self.assertEqual(diagnostics["remote_network_probe_rc"], 0)
        self.assertEqual(diagnostics["remote_network_target_ip_matches"], ["bcmeth1"])
        self.assertIsNone(diagnostics["remote_network_preferred_iface"])
        self.assertNotIn("remote_network_failure_hint", diagnostics)
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
        self.assertNotIn("/mnt/Flash/tcapsulesmb.conf", args[1])
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

    def test_preferred_interface_name_prefers_bridge0_with_private_ipv4(self) -> None:
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
        candidates = probe._parse_ifconfig_candidates(ifconfig_output)

        self.assertEqual(preferred_interface_name(candidates), "bridge0")
        self.assertEqual([candidate.name for candidate in candidates], ["gec0", "bridge0", "lo0"])

    def test_parse_ifconfig_candidates_parses_netbsd_inet_alias(self) -> None:
        ifconfig_output = """
bridge0: flags=e043<UP,BROADCAST,RUNNING,LINK1,LINK2,MULTICAST> metric 0 mtu 1500
\tinet alias 10.0.1.13 netmask 0xffffff00 broadcast 10.0.1.255
\tstatus: active
"""
        candidates = probe._parse_ifconfig_candidates(ifconfig_output)

        self.assertEqual(preferred_interface_name(candidates), "bridge0")
        self.assertEqual(candidates[0].ipv4_addrs, ("10.0.1.13",))

    def test_preferred_interface_name_prefers_bcmeth1_when_bridge0_has_no_ipv4(self) -> None:
        ifconfig_output = """
bcmeth1: flags=ffffe843<UP,BROADCAST,RUNNING,SIMPLEX,LINK1,LINK2,MULTICAST> metric 0 mtu 1500
\tinet 10.0.1.1 netmask 0xffffff00 broadcast 10.0.1.255
\tstatus: active
bcmeth0: flags=ffffe843<UP,BROADCAST,RUNNING,SIMPLEX,LINK1,LINK2,MULTICAST> metric 0 mtu 1500
\tstatus: active
bridge0: flags=ffffe043<UP,BROADCAST,RUNNING,LINK1,LINK2,MULTICAST> metric 0 mtu 1500
\tstatus: active
"""
        candidates = probe._parse_ifconfig_candidates(ifconfig_output)

        self.assertEqual(preferred_interface_name(candidates), "bcmeth1")

    def test_preferred_interface_name_ignores_loopback_only_output(self) -> None:
        ifconfig_output = """
lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> metric 0 mtu 33172
\tinet 127.0.0.1 netmask 0xff000000
"""
        candidates = probe._parse_ifconfig_candidates(ifconfig_output)

        self.assertIsNone(preferred_interface_name(candidates))

    def test_preferred_interface_name_uses_target_ip_before_generic_ranking(self) -> None:
        ifconfig_output = """
bcmeth1: flags=ffffe843<UP,BROADCAST,RUNNING,SIMPLEX,LINK1,LINK2,MULTICAST> metric 0 mtu 1500
\tinet 10.0.1.1 netmask 0xffffff00 broadcast 10.0.1.255
\tstatus: active
bridge0: flags=ffffe043<UP,BROADCAST,RUNNING,LINK1,LINK2,MULTICAST> metric 0 mtu 1500
\tinet 192.168.1.217 netmask 0xffffff00 broadcast 192.168.1.255
\tstatus: active
"""
        candidates = probe._parse_ifconfig_candidates(ifconfig_output)

        self.assertEqual(preferred_interface_name(candidates), "bridge0")
        self.assertEqual(preferred_interface_name(candidates, target_ips=("10.0.1.1",)), "bcmeth1")

    def test_parse_ifconfig_candidates_keeps_target_matchable_addresses(self) -> None:
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
        candidates = probe._parse_ifconfig_candidates(ifconfig_output)

        self.assertEqual(preferred_interface_name(candidates, target_ips=("192.168.168.111",)), "bcmeth1")
        self.assertEqual(
            [
                candidate.name
                for candidate in candidates
                if "192.168.168.111" in candidate.ipv4_addrs
            ],
            ["bcmeth1"],
        )

    def test_preferred_interface_name_private_ipv4_beats_link_local_without_target_ip(self) -> None:
        ifconfig_output = """
bcmeth1: flags=ffffe843<UP,BROADCAST,RUNNING,SIMPLEX,LINK1,LINK2,MULTICAST> metric 0 mtu 1500
\tinet 169.254.44.9 netmask 0xffff0000 broadcast 169.254.255.255
\tstatus: active
bridge0: flags=ffffe043<UP,BROADCAST,RUNNING,LINK1,LINK2,MULTICAST> metric 0 mtu 1500
\tinet 192.168.1.217 netmask 0xffffff00 broadcast 192.168.1.255
\tstatus: active
"""
        candidates = probe._parse_ifconfig_candidates(ifconfig_output)

        self.assertEqual(preferred_interface_name(candidates), "bridge0")

    def test_preferred_interface_name_link_local_target_does_not_win(self) -> None:
        ifconfig_output = """
bcmeth1: flags=ffffe843<UP,BROADCAST,RUNNING,SIMPLEX,LINK1,LINK2,MULTICAST> metric 0 mtu 1500
\tinet 169.254.44.9 netmask 0xffff0000 broadcast 169.254.255.255
\tstatus: active
bridge0: flags=ffffe043<UP,BROADCAST,RUNNING,LINK1,LINK2,MULTICAST> metric 0 mtu 1500
\tinet 192.168.1.217 netmask 0xffffff00 broadcast 192.168.1.255
\tstatus: active
"""
        candidates = probe._parse_ifconfig_candidates(ifconfig_output)

        self.assertEqual(preferred_interface_name(candidates, target_ips=("169.254.44.9",)), "bridge0")

    def test_probe_remote_interface_candidates_preserves_multiple_private_candidates(self) -> None:
        ifconfig_output = """
bcmeth1: flags=ffffe843<UP,BROADCAST,RUNNING,SIMPLEX,LINK1,LINK2,MULTICAST> metric 0 mtu 1500
\tinet 10.0.1.1 netmask 0xffffff00 broadcast 10.0.1.255
\tstatus: active
bridge0: flags=ffffe043<UP,BROADCAST,RUNNING,LINK1,LINK2,MULTICAST> metric 0 mtu 1500
\tinet 192.168.1.217 netmask 0xffffff00 broadcast 192.168.1.255
\tstatus: active
"""
        candidates = probe._parse_ifconfig_candidates(ifconfig_output)

        self.assertEqual(
            [(candidate.name, candidate.ipv4_addrs) for candidate in candidates],
            [("bcmeth1", ("10.0.1.1",)), ("bridge0", ("192.168.1.217",))],
        )
        self.assertEqual(preferred_interface_name(candidates), "bridge0")
        self.assertEqual(preferred_interface_name(candidates, target_ips=("10.0.1.1",)), "bcmeth1")
