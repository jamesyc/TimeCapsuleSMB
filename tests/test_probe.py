from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

import timecapsulesmb.device.probe as probe
from timecapsulesmb.device.errors import DeviceError
from timecapsulesmb.device.probe import (
    SshAccessStatus,
    flash_runtime_config_present_conn,
    probe_manager_startup_age_conn,
    read_deployed_version_conn,
    read_runtime_ram_diagnostics_conn,
    read_runtime_payload_dir_conn,
    read_runtime_log_tails_conn,
    runtime_ram_root_present_conn,
)
from timecapsulesmb.transport.errors import SshAlgorithmNegotiationError, SshAuthenticationError, SshNetworkError
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

    def _probe_manager_age(self, stdout: str, returncode: int = 0) -> tuple[probe.ManagerStartupAgeProbeResult, mock.Mock]:
        connection = SshConnection("root@10.0.0.2", "pw", "-o StrictHostKeyChecking=no")
        proc = subprocess.CompletedProcess(args=["ssh"], returncode=returncode, stdout=stdout)
        with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=proc) as run_ssh_mock:
            return probe_manager_startup_age_conn(connection), run_ssh_mock

    # Rows as NetBSD 4 and 6 print them: pid ppid stat etime ucomm command.
    _MANAGER_PEERS = (
        " 4390  9751 S       2:47:30 service       service: role=telemetry --daemon\n"
        " 9070  9751 Ss      2:47:29 smbd          /mnt/Memory/samba4/sbin/smbd -F --no-process-group\n"
        " 9807  9751 S       2:47:27 service       service: role=discovery nbns=ready mode=payload\n"
    )

    def test_probe_manager_startup_age_conn_reads_manager_elapsed_time(self) -> None:
        result, run_ssh_mock = self._probe_manager_age(
            self._MANAGER_PEERS + "  250     1 Ss         0:41 service       service: role=manager\n"
        )

        self.assertEqual(result.manager_started_seconds_ago, 41.0)
        self.assertEqual(result.detail, "manager started 41s ago")
        args, kwargs = run_ssh_mock.call_args
        self.assertEqual(args[1], probe.MANAGER_ELAPSED_PS_COMMAND)
        self.assertIn("etime=", args[1])
        self.assertFalse(kwargs["check"])
        self.assertEqual(kwargs["timeout"], probe.REMOTE_STATE_PROBE_TIMEOUT_SECONDS)

    def test_probe_manager_startup_age_conn_parses_every_elapsed_format(self) -> None:
        cases = {
            "0:00": 0,
            "2:59": 179,
            "3:00": 180,
            "2:47:30": 10050,
            "1-02:03:04": 93784,
            "12-00:00:00": 1036800,
        }
        for elapsed, seconds in cases.items():
            with self.subTest(elapsed=elapsed):
                result, _ = self._probe_manager_age(f" 250 1 Ss {elapsed} service service: role=manager\n")
                self.assertEqual(result.manager_started_seconds_ago, float(seconds))

    def test_probe_manager_startup_age_conn_returns_none_when_manager_is_not_running(self) -> None:
        result, _ = self._probe_manager_age(self._MANAGER_PEERS)

        self.assertIsNone(result.manager_started_seconds_ago)
        self.assertEqual(result.detail, "manager is not running")

    def test_probe_manager_startup_age_conn_ignores_zombie_and_lookalike_managers(self) -> None:
        result, _ = self._probe_manager_age(
            " 250 1 Z 0:05 service service: role=manager\n"
            " 251 1 S 0:05 service service: role=manager-helper\n"
            " 252 1 S 0:05 sh sh -c echo service: role=manager\n"
            " 253 1 S 0:05 service service: role=job manager\n"
        )

        self.assertIsNone(result.manager_started_seconds_ago)
        self.assertEqual(result.detail, "manager is not running")

    def test_probe_manager_startup_age_conn_returns_none_when_several_managers_run(self) -> None:
        result, _ = self._probe_manager_age(
            " 250 1 Ss 5:00 service service: role=manager\n"
            " 900 1 Ss 0:03 service service: role=manager\n"
        )

        self.assertIsNone(result.manager_started_seconds_ago)
        self.assertEqual(result.detail, "2 manager processes are running")

    def test_probe_manager_startup_age_conn_returns_none_for_unparseable_elapsed_time(self) -> None:
        for elapsed in ("41", "a:bc", "1:2:3:4", "x-01:00:00", "-01:00"):
            with self.subTest(elapsed=elapsed):
                result, _ = self._probe_manager_age(f" 250 1 Ss {elapsed} service service: role=manager\n")
                self.assertIsNone(result.manager_started_seconds_ago)
                self.assertIn("unparseable", result.detail)

    def test_probe_manager_startup_age_conn_returns_none_on_probe_failure(self) -> None:
        result, _ = self._probe_manager_age("", returncode=1)

        self.assertIsNone(result.manager_started_seconds_ago)
        self.assertIn("rc=1", result.detail)

    def test_probe_manager_startup_age_conn_returns_none_on_ssh_timeout(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "-o StrictHostKeyChecking=no")

        with mock.patch(
            "timecapsulesmb.device.probe.run_ssh",
            side_effect=probe.SshCommandTimeout("Timed out waiting for ssh command to finish: probe"),
        ):
            result = probe_manager_startup_age_conn(connection)

        self.assertIsNone(result.manager_started_seconds_ago)
        self.assertEqual(result.detail, "manager startup age probe timed out")

    def test_read_runtime_log_tails_conn_fetches_ram_and_payload_logs_with_short_timeout(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "-o StrictHostKeyChecking=no")

        def fake_run_ssh(
            _connection: SshConnection,
            remote_cmd: str,
            **_kwargs: object,
        ) -> subprocess.CompletedProcess[str]:
            if "rc.local.log" in remote_cmd:
                return subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="rc log\n", stderr="")
            if "runtime.log" in remote_cmd:
                return subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="manager log\n", stderr="")
            if "rsync.log" in remote_cmd:
                return subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="rsync log\n", stderr="")
            if "telemetry.log" in remote_cmd:
                return subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="telemetry log\n", stderr="")
            if probe.RUNTIME_SMB_CONF in remote_cmd:
                return subprocess.CompletedProcess(
                    args=["ssh"],
                    returncode=0,
                    stdout="[global]\n    log file = /Volumes/dk2/.samba4/logs/log.smbd\n[Data]\n    path = /Volumes/dk2/ShareRoot\n",
                    stderr="",
                )
            if "/mnt/Memory/samba4/var/discovery.log" in remote_cmd:
                return subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="ram discovery log\n", stderr="")
            if "/Volumes/dk2/.samba4/logs/discovery.log" in remote_cmd:
                return subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="discovery log\n", stderr="")
            if "smbd-console.log" in remote_cmd:
                return subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="smbd console\n", stderr="")
            if "log.smbd" in remote_cmd:
                return subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="smbd log\n", stderr="")
            self.fail(f"unexpected remote command: {remote_cmd}")

        with mock.patch("timecapsulesmb.device.probe.run_ssh", side_effect=fake_run_ssh) as run_ssh_mock:
            logs = read_runtime_log_tails_conn(connection)

        self.assertEqual(logs["remote_rc_local_log_tail"], "rc log")
        self.assertEqual(logs["remote_payload_log_dir"], "/Volumes/dk2/.samba4")
        self.assertEqual(logs["remote_manager_log_tail"], "manager log")
        self.assertEqual(logs["remote_rsync_log_tail"], "rsync log")
        self.assertEqual(logs["remote_telemetry_log_tail"], "telemetry log")
        # Both discovery logs: the payload one, and the RAM one it writes
        # whenever smbd is not ready (the case a failure report cares about).
        self.assertEqual(logs["remote_discovery_log_tail"], "discovery log")
        self.assertEqual(logs["remote_diskless_discovery_log_tail"], "ram discovery log")
        self.assertEqual(logs["remote_smbd_log_tail"], "smbd log")
        self.assertEqual(logs["remote_smbd_console_log_tail"], "smbd console")
        self.assertEqual(run_ssh_mock.call_count, 9)
        for call in run_ssh_mock.call_args_list:
            args, kwargs = call
            self.assertEqual(args[0], connection)
            self.assertFalse(kwargs["check"])
            self.assertEqual(kwargs["timeout"], probe.REMOTE_LOG_TAIL_TIMEOUT_SECONDS)

    def test_read_runtime_log_tails_conn_reads_ram_logs_without_payload_state(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "-o StrictHostKeyChecking=no")

        def fake_run_ssh(
            _connection: SshConnection,
            remote_cmd: str,
            **_kwargs: object,
        ) -> subprocess.CompletedProcess[str]:
            if "rc.local.log" in remote_cmd:
                return subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="rc log\n", stderr="")
            if "runtime.log" in remote_cmd:
                return subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="manager log\n", stderr="")
            if "rsync.log" in remote_cmd:
                return subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="rsync log\n", stderr="")
            if "telemetry.log" in remote_cmd:
                return subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="telemetry log\n", stderr="")
            if probe.RUNTIME_SMB_CONF in remote_cmd:
                return subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="[global]\n[Data]\n    path = /Volumes/dk2/ShareRoot\n", stderr="")
            if "/mnt/Memory/samba4/var/discovery.log" in remote_cmd:
                return subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="ram discovery log\n", stderr="")
            self.fail(f"unexpected remote command: {remote_cmd}")

        with mock.patch("timecapsulesmb.device.probe.run_ssh", side_effect=fake_run_ssh) as run_ssh_mock:
            logs = read_runtime_log_tails_conn(connection)

        self.assertEqual(logs["remote_payload_log_dir"], f"(unavailable from active {probe.RUNTIME_SMB_CONF})")
        self.assertEqual(logs["remote_manager_log_tail"], "manager log")
        self.assertEqual(logs["remote_rsync_log_tail"], "rsync log")
        self.assertEqual(logs["remote_diskless_discovery_log_tail"], "ram discovery log")
        self.assertNotIn("remote_discovery_log_tail", logs)
        self.assertNotIn("remote_smbd_log_tail", logs)
        self.assertEqual(run_ssh_mock.call_count, 6)

    def test_read_remote_service_socket_diagnostics_conn_scopes_fstat_to_service_processes(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "-o StrictHostKeyChecking=no")
        stdout = (
            "smbd:\nroot smbd 101 10 internet stream tcp 0x0 *:445\n"
            "wcifsnd:\n(no internet sockets reported)\n"
            "rsync:\nroot rsync 103 10 internet stream tcp 0x0 *:873\n"
        )
        proc = subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout=stdout)

        with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=proc) as run_ssh_mock:
            result = probe.read_remote_service_socket_diagnostics_conn(connection)

        self.assertEqual(result, stdout.strip())
        args, kwargs = run_ssh_mock.call_args
        self.assertEqual(args[0], connection)
        self.assertIn('capture_fstat_for_ucomm "$ps_out" "$proc_name"', args[1])
        self.assertIn("for proc_name in smbd wcifsnd rsync", args[1])
        self.assertIn("/internet/p", args[1])
        self.assertFalse(kwargs["check"])
        self.assertEqual(kwargs["timeout"], probe.REMOTE_STATE_PROBE_TIMEOUT_SECONDS)

    def test_read_runtime_ram_diagnostics_conn_lists_present_and_missing_staged_files(self) -> None:
        # Run the real script against a fake RAM tree: staged files are listed,
        # missing ones are named, and nothing aborts on a partial stage.
        connection = SshConnection("root@10.0.0.2", "pw", "-o StrictHostKeyChecking=no")
        with tempfile.TemporaryDirectory() as tmp:
            ram = Path(tmp) / "samba4"
            for sub in ("sbin", "etc", "private", "var"):
                (ram / sub).mkdir(parents=True)
            (ram / "sbin" / "smbd").write_text("smbd")
            (ram / "etc" / "smb.conf").write_text("[global]\n")

            def run_locally(_connection, command, **kwargs):
                command = command.replace("/mnt/Memory/samba4", str(ram))
                return subprocess.run(command, shell=True, executable="/bin/sh", text=True, capture_output=True)

            with mock.patch("timecapsulesmb.device.probe.run_ssh", side_effect=run_locally) as run_ssh_mock:
                result = read_runtime_ram_diagnostics_conn(connection)

        lines = result.splitlines()
        self.assertIn("runtime paths:", lines)
        self.assertTrue(any(line.endswith(f"{ram}/sbin/smbd") and not line.startswith("missing") for line in lines))
        self.assertTrue(any(line.endswith(f"{ram}/etc/smb.conf") and not line.startswith("missing") for line in lines))
        for missing in ("sbin/rsync", "private/smbpasswd", "private/username.map", "etc/rsyncd.conf", "var/rsync.log"):
            self.assertIn(f"missing {ram}/{missing}", lines)
        self.assertFalse(any(".tmp." in line for line in lines))
        _args, kwargs = run_ssh_mock.call_args
        self.assertFalse(kwargs["check"])
        self.assertEqual(kwargs["timeout"], probe.REMOTE_STATE_PROBE_TIMEOUT_SECONDS)

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

    def test_probe_device_conn_reports_closed_ssh_port_without_remote_probe(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "-o StrictHostKeyChecking=no")

        with mock.patch("timecapsulesmb.device.probe.tcp_open", return_value=False) as tcp_open_mock:
            with mock.patch("timecapsulesmb.device.probe.run_ssh") as run_ssh_mock:
                result = probe.probe_device_conn(connection)

        self.assertEqual(result.ssh_status, SshAccessStatus.CLOSED)
        self.assertFalse(result.ssh_port_reachable)
        self.assertFalse(result.ssh_authenticated)
        self.assertEqual(result.error, "SSH is not reachable yet.")
        tcp_open_mock.assert_called_once_with("10.0.0.2", 22)
        run_ssh_mock.assert_not_called()

    def test_probe_device_conn_reports_auth_rejection_separately(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "-o StrictHostKeyChecking=no")

        with mock.patch("timecapsulesmb.device.probe.tcp_open", return_value=True):
            with mock.patch("timecapsulesmb.device.probe.run_ssh", side_effect=SshAuthenticationError("Permission denied")):
                result = probe.probe_device_conn(connection)

        self.assertEqual(result.ssh_status, SshAccessStatus.AUTH_REJECTED)
        self.assertTrue(result.ssh_port_reachable)
        self.assertFalse(result.ssh_authenticated)
        self.assertEqual(result.error, "Permission denied")

    def test_probe_device_conn_reports_algorithm_negotiation_separately(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "-o StrictHostKeyChecking=no")
        error = SshAlgorithmNegotiationError(
            "Unable to negotiate: no matching MAC found. Their offer: hmac-md5,hmac-sha1",
            algorithm="mac",
            offered=("hmac-md5", "hmac-sha1"),
        )

        with mock.patch("timecapsulesmb.device.probe.tcp_open", return_value=True):
            with mock.patch("timecapsulesmb.device.probe.run_ssh", side_effect=error):
                result = probe.probe_device_conn(connection)

        self.assertEqual(result.ssh_status, SshAccessStatus.ALGORITHM_NEGOTIATION_FAILED)
        self.assertTrue(result.ssh_port_reachable)
        self.assertFalse(result.ssh_authenticated)
        self.assertIn("no matching MAC found", result.error or "")

    def test_probe_device_conn_reports_transport_failure_without_auth_rejection(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "-o StrictHostKeyChecking=no")

        with mock.patch("timecapsulesmb.device.probe.tcp_open", return_value=True):
            with mock.patch("timecapsulesmb.device.probe.run_ssh", side_effect=SshNetworkError("Connection timed out")):
                result = probe.probe_device_conn(connection)

        self.assertEqual(result.ssh_status, SshAccessStatus.TRANSPORT_FAILED)
        self.assertTrue(result.ssh_port_reachable)
        self.assertFalse(result.ssh_authenticated)
        self.assertEqual(result.error, "Connection timed out")

    def test_probe_device_conn_reports_device_probe_failure_after_ssh_auth(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "-o StrictHostKeyChecking=no")

        with mock.patch("timecapsulesmb.device.probe.tcp_open", return_value=True):
            with mock.patch("timecapsulesmb.device.probe._probe_remote_os_info_conn", side_effect=DeviceError("bad uname")):
                result = probe.probe_device_conn(connection)

        self.assertEqual(result.ssh_status, SshAccessStatus.DEVICE_PROBE_FAILED)
        self.assertTrue(result.ssh_port_reachable)
        self.assertFalse(result.ssh_authenticated)
        self.assertEqual(result.error, "bad uname")

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
            result = probe._probe_remote_elf_endianness_result_conn(connection)

        self.assertEqual(result.endianness, "little")
        self.assertIsNone(result.detail)
        remote_cmd = run_ssh_mock.call_args.args[1]
        self.assertIn("/bin/dd", remote_cmd)
        self.assertIn("/usr/bin/sed -n l", remote_cmd)
        self.assertNotIn("/usr/bin/tr", remote_cmd)
        self.assertNotIn("/usr/bin/od", remote_cmd)

    def test_probe_remote_elf_endianness_retries_raw_compare_when_sed_unknown(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "-o StrictHostKeyChecking=no")
        sed_proc = subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="sed_b5=\nunknown\n")
        raw_proc = subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="little\n")

        with mock.patch("timecapsulesmb.device.probe.run_ssh", side_effect=[sed_proc, raw_proc]) as run_ssh_mock:
            result = probe._probe_remote_elf_endianness_result_conn(connection)

        self.assertEqual(result.endianness, "little")
        self.assertIsNotNone(result.detail)
        self.assertIn("sed=unknown", result.detail or "")
        self.assertIn("raw=little", result.detail or "")
        self.assertEqual(run_ssh_mock.call_count, 2)
        sed_cmd = run_ssh_mock.call_args_list[0].args[1]
        raw_cmd = run_ssh_mock.call_args_list[1].args[1]
        self.assertIn("/usr/bin/sed -n l", sed_cmd)
        self.assertIn("printf", raw_cmd)
        self.assertNotIn("/usr/bin/sed -n l", raw_cmd)

    def test_probe_remote_elf_endianness_keeps_detail_when_both_methods_unknown(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "-o StrictHostKeyChecking=no")
        sed_proc = subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="sed_b5=unexpected\nunknown\n")
        raw_proc = subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="raw_compare=nomatch\nunknown\n")

        with mock.patch("timecapsulesmb.device.probe.run_ssh", side_effect=[sed_proc, raw_proc]):
            result = probe._probe_remote_elf_endianness_result_conn(connection)

        self.assertEqual(result.endianness, "unknown")
        self.assertIsNotNone(result.detail)
        self.assertIn("sed_b5=unexpected", result.detail or "")
        self.assertIn("raw_compare=nomatch", result.detail or "")

    def test_extract_airport_identity_from_text_finds_airport_extreme_model(self) -> None:
        result = probe.extract_airport_identity_from_text("prefix\x00psyAM\x00pAirPort7,120\x00suffix")
        self.assertEqual(result.model, "AirPort7,120")
        self.assertEqual(result.syap, "120")
        self.assertIn("AirPort7,120", result.detail)
