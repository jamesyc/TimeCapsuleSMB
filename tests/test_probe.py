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
from timecapsulesmb.integrations import acp
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
from timecapsulesmb.transport.errors import (
    SshAlgorithmNegotiationError,
    SshAuthenticationError,
    SshLocalNetworkFilteredError,
    SshNetworkError,
)
from timecapsulesmb.transport.ssh import SshCommandTimeout, SshConnection


DEVICE_MAC = "02:00:00:00:00:01"
# The real read: conftest replaces probe.read_airport_acp for every test.
REAL_READ_AIRPORT_ACP = probe.read_airport_acp


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

    @staticmethod
    def _manager_age_output(*rows: str, now_ms: str = "60000") -> str:
        # The probe's output: each ps row (pid ppid stat etime ucomm command,
        # as NetBSD 4 and 6 print them) behind ps=, then the clock reading.
        return "".join(f"ps={row}\n" for row in rows) + f"now_ms={now_ms}\n"

    _MANAGER_PEERS = (
        " 4390  9751 S       2:47:30 service       service: role=telemetry --daemon",
        " 9070  9751 Ss      2:47:29 smbd          /mnt/Memory/samba4/sbin/smbd -F --no-process-group",
        " 9807  9751 S       2:47:27 service       service: role=discovery nbns=ready mode=payload",
    )

    def test_probe_manager_startup_age_conn_subtracts_the_title_start_from_the_monotonic_clock(self) -> None:
        result, run_ssh_mock = self._probe_manager_age(self._manager_age_output(
            *self._MANAGER_PEERS, "  250     1 Ss         0:41 service       service: role=manager started=19", now_ms="60999"
        ))

        self.assertEqual(result.manager_started_seconds_ago, 41.0)
        self.assertEqual(result.detail, "manager started 41s ago")
        self.assertEqual((result.started_monotonic_s, result.now_monotonic_ms), (19, 60999))
        args, kwargs = run_ssh_mock.call_args
        self.assertEqual(args[1], probe.MANAGER_STARTUP_AGE_COMMAND)
        self.assertTrue(args[1].startswith(probe.MANAGER_PS_COMMAND))
        self.assertIn('"${RUNTIME_SERVICE_BIN:-/mnt/Flash/service}" --print-monotonic-ms', args[1])
        self.assertFalse(kwargs["check"])
        self.assertEqual(kwargs["timeout"], probe.REMOTE_STATE_PROBE_TIMEOUT_SECONDS)

    def test_probe_manager_startup_age_conn_ignores_a_wall_clock_step_in_the_elapsed_time(self) -> None:
        # Field case (v3.3.0 telemetry): the device booted with its clock a day
        # behind and sntpd stepped it, so ps said 1-00:02:57 (86577 s) for a
        # manager that started 55 s earlier.
        result, _ = self._probe_manager_age(self._manager_age_output(
            " 142 1 S 1-00:02:57 service service: role=manager started=5", now_ms="60000"
        ))

        self.assertEqual(result.manager_started_seconds_ago, 55.0)

    def test_probe_manager_startup_age_conn_measures_a_manager_started_after_boot(self) -> None:
        # NetBSD 4 deploys reboot first and activate starts the manager later:
        # its age is not the device's uptime.
        result, _ = self._probe_manager_age(self._manager_age_output(
            " 758 1 S 0:20 service service: role=manager started=600", now_ms="620400"
        ))

        self.assertEqual(result.manager_started_seconds_ago, 20.0)

    def test_device_hostname_probe_parses_name_hosts_title_and_boot_wait(self) -> None:
        result = probe.parse_device_hostname_probe(
            "hostname=capsule\n"
            "ps=  250     1 Ss 0:41 service       service: role=manager waiting=hostname\n"
            "ps=  251     1 S  0:41 service       service: role=discovery nbns=ready\n"
            "hosts=127.0.0.1\tlocalhost localhost.\n"
            "hosts=127.0.0.1\tcapsule capsule.local\n"
            "found=4330\n"
            "found=12\n"
        )

        self.assertEqual(result.hostname, "capsule")
        self.assertTrue(result.manager_waiting)
        self.assertTrue(result.mapped)
        self.assertEqual(result.stale_names, ())
        # A manager restarted during the boot logs again; its line is the last.
        self.assertEqual(result.boot_wait_ms, 12)
        self.assertIsNone(result.error)

    def test_device_hostname_probe_ignores_lookalike_and_dead_managers(self) -> None:
        result = probe.parse_device_hostname_probe(
            "hostname=capsule\n"
            "ps=  250 1 Z 0:05 service service: role=manager waiting=hostname\n"
            "ps=  251 1 S 0:05 service service: role=manager-helper waiting=hostname\n"
            "ps=  252 1 S 0:05 sh sh -c echo service: role=manager waiting=hostname\n"
            "ps=  253 1 S 0:05 service service: role=manager\n"
        )

        self.assertFalse(result.manager_waiting)
        self.assertIsNone(result.boot_wait_ms)

    def test_device_hostname_probe_decides_what_counts_as_a_mapping(self) -> None:
        cases = {
            ("192.0.2.1 capsule",): True,
            ("::1 capsule.local # Apple",): True,
            ("127.0.0.1 localhost # capsule",): False,  # only a comment
            ("capsule 127.0.0.1",): False,  # the address field is not a name
            ("127.0.0.1\tcapsule2 capsule2.local",): False,
            (): False,
        }
        for lines, mapped in cases.items():
            with self.subTest(lines=lines):
                self.assertEqual(probe.DeviceHostnameProbeResult("capsule", lines).mapped, mapped)
        self.assertFalse(probe.DeviceHostnameProbeResult("", ("127.0.0.1\t capsule",)).mapped)

    def test_device_hostname_probe_lists_our_lines_for_other_names_only(self) -> None:
        result = probe.DeviceHostnameProbeResult("new", (
            "127.0.0.1\told old.local",
            "127.0.0.1\tnew new.local",
            "127.0.0.1 other other.local",   # not our exact form
            "127.0.0.1\tlocalhost old",       # Apple's line
            "",
            "192.168.1.170 tcsmb-192-168-1-170",  # an SSH client's line
        ))

        self.assertEqual(result.stale_names, ("old",))
        # While the name is unset (ACPd cleared it) no mapping counts as earlier.
        self.assertEqual(probe.DeviceHostnameProbeResult("", ("127.0.0.1\tnew new.local",)).stale_names, ())

    def test_device_hostname_probe_splits_lines_only_at_lf_like_the_manager(self) -> None:
        # A CR or a vertical tab stays inside its /etc/hosts line, as it does
        # for the manager, which splits only at LF and uses the kernel name as
        # it is.
        self.assertEqual(probe.parse_device_hostname_probe("hostname= new \n").hostname, " new ")
        result = probe.parse_device_hostname_probe(
            "hostname=new\n"
            "hosts=127.0.0.1\told old.local\r\n"
            "hosts=127.0.0.1\x0bnew\n"
        )

        self.assertEqual(result.hosts_lines, ("127.0.0.1\told old.local\r", "127.0.0.1\x0bnew"))
        self.assertEqual(result.stale_names, ())
        self.assertFalse(result.mapped)

    def test_device_hostname_probe_conn_reports_timeouts_and_failures(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "")
        with mock.patch("timecapsulesmb.device.probe.run_ssh", side_effect=SshCommandTimeout("slow")):
            self.assertEqual(probe.probe_device_hostname_conn(connection).error, "device hostname probe timed out")
        failed = subprocess.CompletedProcess([], 255, stdout="", stderr="")
        with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=failed):
            self.assertEqual(probe.probe_device_hostname_conn(connection).error, "device hostname probe failed (rc=255)")
        ok = subprocess.CompletedProcess([], 0, stdout="hostname=capsule\nhosts=127.0.0.1\tcapsule capsule.local\n", stderr="")
        with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=ok) as run_ssh:
            result = probe.probe_device_hostname_conn(connection)
        self.assertTrue(result.mapped)
        self.assertEqual(run_ssh.call_args.args[1], probe.DEVICE_HOSTNAME_PROBE_COMMAND)

    def test_probe_manager_startup_age_conn_reads_the_start_among_other_title_words(self) -> None:
        for title in (
            "service: role=manager started=19 waiting=hostname",
            "service: role=manager started=19 stuck=3166:smbd:needbuf:600,+5",
            "service: role=manager waiting=hostname started=19",
        ):
            with self.subTest(title=title):
                result, _ = self._probe_manager_age(self._manager_age_output(
                    *self._MANAGER_PEERS, f"  250     1 Ss 0:41 service       {title}", now_ms="60000"
                ))
                self.assertEqual(result.manager_started_seconds_ago, 41.0)

    def test_probe_manager_startup_age_conn_counts_the_second_the_manager_started_in(self) -> None:
        # started= is whole seconds; the age is too, so a reading in the same
        # second is 0, never negative.
        for now_ms, age in (("19000", 0.0), ("19999", 0.0), ("20000", 1.0)):
            with self.subTest(now_ms=now_ms):
                result, _ = self._probe_manager_age(self._manager_age_output(
                    " 250 1 Ss 0:00 service service: role=manager started=19", now_ms=now_ms
                ))
                self.assertEqual(result.manager_started_seconds_ago, age)

    def test_probe_manager_startup_age_conn_returns_none_for_a_title_without_a_start(self) -> None:
        # A manager from an older release, or one whose clock read failed.
        for title in (
            "service: role=manager",
            "service: role=manager waiting=hostname",
            "service: role=manager started=",
            "service: role=manager started=-5",
            "service: role=manager started=1x",
            "service: role=manager started=5 started=6",
            "service: role=manager started=\u00b2",
        ):
            with self.subTest(title=title):
                result, _ = self._probe_manager_age(self._manager_age_output(f" 250 1 Ss 0:41 service {title}"))
                self.assertIsNone(result.manager_started_seconds_ago)
                self.assertEqual(result.detail, "manager title has no start time (an older release, or its clock read failed)")

    def test_probe_manager_startup_age_conn_returns_none_without_a_monotonic_reading(self) -> None:
        # An older service binary rejects --print-monotonic-ms and prints
        # nothing; a missing line is the same.
        row = " 250 1 Ss 0:41 service service: role=manager started=19"
        for output in (
            self._manager_age_output(row, now_ms=""),
            self._manager_age_output(row, now_ms="soon"),
            self._manager_age_output(row, now_ms="-1"),
            self._manager_age_output(row, now_ms="\u00b2"),
            f"ps={row}\n",
        ):
            with self.subTest(output=output):
                result, _ = self._probe_manager_age(output)
                self.assertIsNone(result.manager_started_seconds_ago)
                self.assertEqual(result.started_monotonic_s, 19)
                self.assertIn("--print-monotonic-ms failed", result.detail)

    def test_probe_manager_startup_age_conn_returns_none_for_a_start_after_the_clock_reading(self) -> None:
        result, _ = self._probe_manager_age(self._manager_age_output(
            " 250 1 Ss 0:41 service service: role=manager started=61", now_ms="60000"
        ))

        self.assertIsNone(result.manager_started_seconds_ago)
        self.assertEqual((result.started_monotonic_s, result.now_monotonic_ms), (61, 60000))
        self.assertIn("after the device clock reading", result.detail)

    def test_probe_manager_startup_age_conn_reports_an_empty_process_listing(self) -> None:
        # ps's status is lost in the pipe, and a listing always has the probe's
        # own shell: no ps= line means ps failed, not that no manager runs. A
        # row without the ps= prefix is not a process.
        for output in ("now_ms=60000\n", " 250 1 Ss 0:41 service service: role=manager started=19\nnow_ms=60000\n"):
            with self.subTest(output=output):
                result, _ = self._probe_manager_age(output)
                self.assertIsNone(result.manager_started_seconds_ago)
                self.assertEqual(result.detail, "process listing failed")

    def test_probe_manager_startup_age_conn_ignores_a_manager_before_its_first_title(self) -> None:
        # Between exec and its first setproctitle, ps shows the manager's argv,
        # which has no start yet: like no manager, it gets no startup grace.
        result, _ = self._probe_manager_age(self._manager_age_output(
            " 250 1 Ss 0:00 service /mnt/Flash/service manager"
        ))

        self.assertIsNone(result.manager_started_seconds_ago)
        self.assertEqual(result.detail, "manager is not running")

    def test_manager_startup_age_command_runs_ps_then_the_service_clock(self) -> None:
        # The probe's own shell, with a stand-in service binary: every ps row
        # comes back behind ps=, then one clock line from the binary.
        with tempfile.TemporaryDirectory() as directory:
            service = Path(directory) / "service"
            service.write_text('#!/bin/sh\n[ "$1" = --print-monotonic-ms ] && echo 4242000\n')
            service.chmod(0o755)
            run = subprocess.run(
                ["/bin/sh", "-c", probe.MANAGER_STARTUP_AGE_COMMAND],
                capture_output=True, text=True, timeout=30, env={"PATH": "/usr/bin:/bin", "RUNTIME_SERVICE_BIN": str(service)},
            )
        lines = run.stdout.splitlines()
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(lines[-1], "now_ms=4242000")
        self.assertGreater(len(lines), 1)
        self.assertTrue(all(line.startswith("ps=") for line in lines[:-1]))

    def test_probe_manager_startup_age_conn_returns_none_when_manager_is_not_running(self) -> None:
        result, _ = self._probe_manager_age(self._manager_age_output(*self._MANAGER_PEERS))

        self.assertIsNone(result.manager_started_seconds_ago)
        self.assertEqual(result.detail, "manager is not running")

    def test_probe_manager_startup_age_conn_ignores_zombie_and_lookalike_managers(self) -> None:
        result, _ = self._probe_manager_age(self._manager_age_output(
            " 250 1 Z 0:05 service service: role=manager started=1",
            " 251 1 S 0:05 service service: role=manager-helper started=1",
            " 252 1 S 0:05 sh sh -c echo service: role=manager started=1",
            " 253 1 S 0:05 service service: role=job manager started=1",
        ))

        self.assertIsNone(result.manager_started_seconds_ago)
        self.assertEqual(result.detail, "manager is not running")

    def test_probe_manager_startup_age_conn_returns_none_when_several_managers_run(self) -> None:
        result, _ = self._probe_manager_age(self._manager_age_output(
            " 250 1 Ss 5:00 service service: role=manager started=1",
            " 900 1 Ss 0:03 service service: role=manager started=50",
        ))

        self.assertIsNone(result.manager_started_seconds_ago)
        self.assertEqual(result.detail, "2 manager processes are running")

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

    def test_read_runtime_log_tails_conn_skips_data_disk_logs_when_asked(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "-o StrictHostKeyChecking=no")
        commands: list[str] = []

        def fake_run_ssh(
            _connection: SshConnection,
            remote_cmd: str,
            **_kwargs: object,
        ) -> subprocess.CompletedProcess[str]:
            commands.append(remote_cmd)
            if "/Volumes/" in remote_cmd or probe.RUNTIME_SMB_CONF in remote_cmd:
                self.fail(f"read from the data disk: {remote_cmd}")
            return subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="ram log\n", stderr="")

        with mock.patch("timecapsulesmb.device.probe.run_ssh", side_effect=fake_run_ssh):
            logs = read_runtime_log_tails_conn(connection, skip_data_disk="the device's process list timed out")

        self.assertEqual(len(commands), len(probe.REMOTE_RUNTIME_RAM_LOG_PATHS))
        for key in probe.REMOTE_RUNTIME_RAM_LOG_PATHS:
            self.assertEqual(logs[key], "ram log")
        skipped = "(skipped: the device's process list timed out)"
        self.assertEqual(logs["remote_payload_log_dir"], skipped)
        for key in probe.REMOTE_PAYLOAD_LOG_FILENAMES:
            self.assertEqual(logs[key], skipped)

    def test_read_process_snapshot_conn_runs_ps_with_short_timeout(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "-o StrictHostKeyChecking=no")
        listing = "  457   146   457 I       20 select   smbd     /mnt/Memory/samba4/sbin/smbd -F\n"

        with mock.patch(
            "timecapsulesmb.device.probe.run_ssh",
            return_value=subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout=listing, stderr=""),
        ) as run_ssh_mock:
            snapshot = probe.read_process_snapshot_conn(connection)

        self.assertEqual(snapshot, listing)
        args, kwargs = run_ssh_mock.call_args
        self.assertEqual(args, (connection, probe.PROCESS_SNAPSHOT_COMMAND))
        self.assertFalse(kwargs["check"])
        self.assertEqual(kwargs["timeout"], probe.PROCESS_SNAPSHOT_TIMEOUT_SECONDS)

    def test_read_process_snapshot_conn_returns_empty_text_without_output(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "-o StrictHostKeyChecking=no")

        with mock.patch(
            "timecapsulesmb.device.probe.run_ssh",
            return_value=subprocess.CompletedProcess(args=["ssh"], returncode=1, stdout=None, stderr="ps: error"),
        ):
            self.assertEqual(probe.read_process_snapshot_conn(connection), "")

    def test_read_process_snapshot_conn_raises_when_ps_does_not_answer(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "-o StrictHostKeyChecking=no")

        with mock.patch(
            "timecapsulesmb.device.probe.run_ssh",
            side_effect=probe.SshCommandTimeout("Timed out waiting for ssh command to finish: ps"),
        ):
            with self.assertRaises(SshCommandTimeout):
                probe.read_process_snapshot_conn(connection)

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
                return subprocess.CompletedProcess(
                    args=["ssh"], returncode=0, stdout="NetBSD\n6.0\nearmv4\nno-rc.local-autostart\n")
            if "bs=1 skip=5" in remote_cmd:
                return subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="little\n")
            if "/usr/bin/acp -q syAP" in remote_cmd:
                return subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="syAP=0x00000077\nsyAM=TimeCapsule8,119\nwaMA = 02-AA-BB-CC-DD-EE\n")
            self.fail(f"unexpected remote command: {remote_cmd}")

        with mock.patch("timecapsulesmb.device.probe.tcp_open", return_value=True):
            with mock.patch("timecapsulesmb.device.probe.run_ssh", side_effect=fake_run_ssh) as run_ssh_mock:
                result = probe.probe_device_conn(connection)

        self.assertTrue(result.ssh_authenticated)
        self.assertEqual(result.os_name, "NetBSD")
        self.assertEqual(result.elf_endianness, "little")
        self.assertEqual(result.airport_model, "TimeCapsule8,119")
        self.assertEqual(result.airport_mac, "02:aa:bb:cc:dd:ee")
        self.assertFalse(result.rc_local_autostart)
        self.assertEqual(run_ssh_mock.call_count, 3)
        for call in run_ssh_mock.call_args_list:
            args, _kwargs = call
            self.assertEqual(args[0], connection)
            self.assertEqual(len(args), 2)

    def test_identity_probe_keeps_missing_or_invalid_mac_optional(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "")
        for output, expected in (("02-AA-BB-CC-DD-EE\n", "02:aa:bb:cc:dd:ee"),
                                 ("waMA = 02:aa:bb:cc:dd:ee\n", "02:aa:bb:cc:dd:ee"),
                                 ("", None), ("unavailable\n", None),
                                 ("waMA = 00:00:00:00:00:00\n", None)):
            with self.subTest(output=output), mock.patch.object(probe, "run_ssh", return_value=subprocess.CompletedProcess(
                [], 0, "syAP=0x00000077\nsyAM=TimeCapsule8,119\n" + output)) as query:
                result = probe.probe_remote_airport_identity_conn(connection)
                self.assertEqual(result.airport_mac, expected)
                self.assertEqual(result.syap, "119")
                self.assertEqual(result.model, "TimeCapsule8,119")
                self.assertEqual(query.call_count, 1)

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
        self.assertIsNone(result.mac_network_filters)

    def test_probe_device_conn_reports_a_connection_this_mac_dropped_with_its_filters(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "-o StrictHostKeyChecking=no")
        dropped = SshLocalNetworkFilteredError("This Mac dropped the connection ... (ssh: ...)")
        filters = {"mac_network_extensions": ["com.objective-see.lulu.extension [activated enabled]"]}

        with mock.patch("timecapsulesmb.device.probe.tcp_open", return_value=True):
            with mock.patch("timecapsulesmb.device.probe.run_ssh", side_effect=dropped):
                with mock.patch("timecapsulesmb.device.probe.mac_network_filters", return_value=filters) as collect:
                    result = probe.probe_device_conn(connection)

        self.assertEqual(result.ssh_status, SshAccessStatus.LOCAL_NETWORK_FILTERED)
        # The app's own connection to port 22 worked; only ssh's was dropped.
        self.assertTrue(result.ssh_port_reachable)
        self.assertFalse(result.ssh_authenticated)
        self.assertEqual(result.error, str(dropped))
        self.assertEqual(result.mac_network_filters, filters)
        collect.assert_called_once_with()
        # Probe results stay hashable with the filter fields attached.
        hash(result)

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
                "rc.local-autostart\n"
            ),
        )

        with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=proc):
            result = probe._probe_remote_os_info_conn(connection)

        self.assertEqual(result, ("NetBSD", "4.0_STABLE", "earmv4", True))

    def test_probe_remote_os_info_conn_reads_the_boot_hook_from_login_in_the_same_command(self) -> None:
        # Run the probe's own shell command here, against a stand-in LOGIN:
        # deploy and fsck learn the NetBSD4 boot hook from it before rebooting.
        connection = SshConnection("root@10.0.0.2", "pw", "-o StrictHostKeyChecking=no")
        captured: list[str] = []

        def capture(_connection, remote_cmd, **_kwargs):
            captured.append(remote_cmd)
            return subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="NetBSD\n4.0\nevbarm\nno-rc.local-autostart\n")

        with mock.patch("timecapsulesmb.device.probe.run_ssh", side_effect=capture):
            probe._probe_remote_os_info_conn(connection)
        command = captured[0]
        # The device has no grep, awk, wc or head.
        for tool in ("grep", "awk", "wc ", "head", "tr "):
            self.assertNotIn(tool, command)

        cases = {
            "patched": ("#!/bin/sh\nif [ -x /mnt/Flash/rc.local ]; then\n    /mnt/Flash/rc.local\nfi\n", "rc.local-autostart"),
            "stock": ("#!/bin/sh\n# LOGIN\nexit 0\n", "no-rc.local-autostart"),
            "missing": (None, "no-rc.local-autostart"),
        }
        for name, (content, expected) in cases.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                login = Path(tmp) / "LOGIN"
                if content is not None:
                    login.write_text(content)
                local = command.replace(probe.NETBSD4_LOGIN_PATH, str(login))
                out = subprocess.run(local, shell=True, capture_output=True, text=True, check=True).stdout
                self.assertEqual(out.splitlines()[-1], expected)
                self.assertEqual(len(out.splitlines()), 4)

    def test_probe_remote_os_info_conn_rejects_output_without_the_boot_hook_line(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "-o StrictHostKeyChecking=no")
        proc = subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="NetBSD\n4.0\nevbarm\n")

        with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=proc):
            with self.assertRaises(DeviceError):
                probe._probe_remote_os_info_conn(connection)

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


class AirportAcpReadingTests(unittest.TestCase):
    """The admin password and AirPort MAC, read over network ACP.

    SSH checks only the first 8 characters of the root password; ACPd checks
    all of syPW on every request, so a read that ACPd accepts proves the
    password every reboot needs.
    """

    def read(self, *answers: object) -> tuple[probe.AirportAcpReading, mock.Mock, mock.Mock]:
        get = mock.Mock(side_effect=list(answers))
        with mock.patch.object(probe.acp, "get_properties", get), mock.patch.object(probe.time, "sleep") as sleep:
            reading = REAL_READ_AIRPORT_ACP("root@10.0.0.2", "s3cret-pw")
        return reading, get, sleep

    def test_an_accepted_read_matches_the_password_and_gives_the_mac(self) -> None:
        reading, get, sleep = self.read({"waMA": bytes.fromhex("02000000000A")})

        self.assertEqual(reading, probe.AirportAcpReading(password_matches=True, airport_mac="02:00:00:00:00:0a"))
        get.assert_called_once_with("10.0.0.2", "s3cret-pw", ("waMA",), timeout=probe.ACP_IDENTITY_READ_TIMEOUT_SECONDS)
        sleep.assert_not_called()

    def test_a_rejected_password_is_a_mismatch_without_a_retry(self) -> None:
        reading, get, sleep = self.read(acp.ACPAuthError("ACP command failed with error_code -0x10"))

        self.assertIs(reading.password_matches, False)
        self.assertIsNone(reading.airport_mac)
        get.assert_called_once()
        sleep.assert_not_called()

    def test_a_failed_read_is_retried_once_five_seconds_later(self) -> None:
        reading, get, sleep = self.read(
            acp.ACPConnectionError("Could not connect to ACP on 10.0.0.2:5009: timed out"),
            {"waMA": bytes.fromhex("020000000001")},
        )

        self.assertIs(reading.password_matches, True)
        self.assertEqual(reading.airport_mac, "02:00:00:00:00:01")
        self.assertEqual(get.call_count, 2)
        sleep.assert_called_once_with(5.0)

    def test_a_single_attempt_read_is_not_retried(self) -> None:
        get = mock.Mock(side_effect=[acp.ACPConnectionError("ACP receive failed: timed out")])
        with mock.patch.object(probe.acp, "get_properties", get), mock.patch.object(probe.time, "sleep") as sleep:
            reading = REAL_READ_AIRPORT_ACP("root@10.0.0.2", "pw", attempts=1)

        self.assertIsNone(reading.password_matches)
        get.assert_called_once()
        sleep.assert_not_called()

    def test_two_failed_reads_are_unknown_not_a_wrong_password(self) -> None:
        reading, get, sleep = self.read(
            acp.ACPConnectionError("ACP receive failed: timed out"),
            acp.ACPProtocolError("ACP response had invalid magic"),
        )

        self.assertIsNone(reading.password_matches)
        self.assertEqual(reading.error, "ACP response had invalid magic")
        self.assertEqual(get.call_count, 2)
        sleep.assert_called_once_with(5.0)

    def test_an_unreadable_or_invalid_mac_still_proves_the_password(self) -> None:
        for answer in (
            {"waMA": acp.ACPPropertyError("ACP property waMA failed with error_code -0x1a37")},
            {"waMA": b"\x00" * 6},
            {},
        ):
            with self.subTest(answer=answer):
                reading, _get, _sleep = self.read(answer)
                self.assertEqual(reading, probe.AirportAcpReading(password_matches=True, airport_mac=None))

    def test_the_admin_password_is_read_only_with_a_password(self) -> None:
        answer = mock.Mock(return_value=probe.AirportAcpReading(password_matches=False))
        with mock.patch.object(probe, "read_airport_acp", answer):
            self.assertEqual(
                probe.read_admin_password(SshConnection("root@10.0.0.2", "", "")),
                probe.AirportAcpReading(password_matches=None),
            )
            answer.assert_not_called()
            self.assertIs(probe.read_admin_password(SshConnection("root@10.0.0.2", "pw", "")).password_matches, False)
        answer.assert_called_once_with("root@10.0.0.2", "pw")

    def test_an_unknown_password_check_says_why(self) -> None:
        cases = (
            (probe.AirportAcpReading(password_matches=True, airport_mac=DEVICE_MAC), {"sypw_check": "match"}),
            (probe.AirportAcpReading(password_matches=False), {"sypw_check": "mismatch"}),
            (probe.AirportAcpReading(password_matches=None), {"sypw_check": "unknown"}),
            (
                probe.AirportAcpReading(password_matches=None, error="Could not connect to ACP on 10.0.0.2:5009: timed out"),
                {"sypw_check": "unknown", "acp_read_error": "Could not connect to ACP on 10.0.0.2:5009: timed out"},
            ),
        )
        for reading, fields in cases:
            with self.subTest(reading=reading):
                self.assertEqual(probe.password_check_fields(reading), fields)
