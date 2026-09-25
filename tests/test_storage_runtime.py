from __future__ import annotations

import subprocess
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from timecapsulesmb.core.config import AppConfig
from timecapsulesmb.core.release import CLI_VERSION_CODE, RELEASE_TAG
from timecapsulesmb.services.deploy import render_flash_runtime_config, render_rsync_daemon_config
from timecapsulesmb.services.deploy import render_flash_runtime_config as render_gui_flash_runtime_config
from timecapsulesmb.deploy.planner import (
    GENERATED_FLASH_CONFIG_SOURCE,
    build_deployment_plan,
)
from timecapsulesmb.device.storage import (
    MAST_PROBE_COMMAND,
    PayloadVerificationResult,
    StorageDeviceError,
    MaStProbeDiagnostics,
    MaStReadResult,
    MaStVolume,
    PayloadHome,
    ensure_volume_root_mounted_conn,
    mast_probe_debug_summary,
    mast_volumes_debug_summary,
    ordered_payload_candidate_volumes,
    payload_candidate_checks_debug_summary,
    parse_mast_inventory,
    parse_mast_plist,
    probe_mast_diagnostics_conn,
    render_ensure_volume_root_mounted_script,
    select_payload_home_with_diagnostics_conn,
    verify_payload_home_conn,
    volume_root_is_writable_conn,
    wait_for_mast_volumes_conn,
)
from timecapsulesmb.transport.ssh import SshCommandTimeout, SshConnection
from tests.storage_fixtures import MAST_FIXTURES, SHELL_MAST_FIXTURES


class StorageRuntimeTests(unittest.TestCase):


    def test_parse_mast_plist_matches_golden_fixtures(self) -> None:
        for fixture in MAST_FIXTURES:
            with self.subTest(fixture=fixture.name):
                self.assertEqual(parse_mast_plist(fixture.raw), fixture.expected)

    def test_parse_mast_openstep_fallback_handles_current_acp_line_format(self) -> None:
        raw = """\
MaSt = (
    {
        deviceName = "wd0";
        builtin = true;
        partitions = (
            {
                deviceName = "dk2";
                name = "Data; Main";
                format = "hfs";
                uuid = <f42bdb83 c2655522 a0872560 6a4d0abf>;
            },
            {
                deviceName = "dk1";
                name = "APconfig";
                format = "msdos";
                uuid = <00000000 00000000 00000000 00000000>;
            }
        );
    },
    {
        deviceName = "sd0";
        builtin = false;
        partitions = (
            {
                deviceName = "dk3";
                name = "uuid = fake";
                format = "hfs";
                uuid = <51f93e6f dc69524d 986dcee4 d7cb3573>;
            }
        );
    }
);
"""

        self.assertEqual(
            parse_mast_plist(raw),
            (
                MaStVolume("wd0", "dk2", "/Volumes/dk2", "Data; Main", "f42bdb83-c265-5522-a087-25606a4d0abf", True, "hfs"),
                MaStVolume("sd0", "dk3", "/Volumes/dk3", "uuid = fake", "51f93e6f-dc69-524d-986d-cee4d7cb3573", False, "hfs"),
            ),
        )

    def test_wait_for_mast_volumes_retries_until_available(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "")
        volume = MaStVolume("wd0", "dk2", "/Volumes/dk2", "Data", "f42bdb83-c265-5522-a087-25606a4d0abf", True, "hfs")

        with mock.patch(
            "timecapsulesmb.device.storage.read_mast_volumes_with_output_conn",
            side_effect=[
                MaStReadResult((), "MaSt=first"),
                MaStReadResult((), "MaSt=second"),
                MaStReadResult((volume,), "MaSt=third"),
            ],
        ) as read_mock:
            with mock.patch("timecapsulesmb.device.storage.time.sleep") as sleep_mock:
                result = wait_for_mast_volumes_conn(connection, attempts=10, delay_seconds=3)

        self.assertEqual(result.volumes, (volume,))
        self.assertEqual(result.attempts, 3)
        self.assertEqual(result.raw_output, "MaSt=third")
        self.assertEqual(read_mock.call_count, 3)
        self.assertEqual(sleep_mock.call_args_list, [mock.call(3), mock.call(3)])

    def test_wait_for_mast_volumes_returns_empty_after_exhaustion(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "")

        with mock.patch(
            "timecapsulesmb.device.storage.read_mast_volumes_with_output_conn",
            return_value=MaStReadResult((), "MaSt=[]"),
        ) as read_mock:
            with mock.patch("timecapsulesmb.device.storage.time.sleep") as sleep_mock:
                result = wait_for_mast_volumes_conn(connection, attempts=3, delay_seconds=3)

        self.assertEqual(result.volumes, ())
        self.assertEqual(result.attempts, 3)
        self.assertEqual(result.raw_output, "MaSt=[]")
        self.assertEqual(read_mock.call_count, 3)
        self.assertEqual(sleep_mock.call_args_list, [mock.call(3), mock.call(3)])

    def test_wait_for_mast_volumes_stops_when_disk_has_no_hfs_partitions(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "")
        raw_output = """
MaSt = (
    {
        deviceName = "wd0";
        name = "Seagate Expansion HDD";
        builtin = true;
        partitions = (
            {
                deviceName = "dk2";
                name = "PS3FAT";
                format = "msdos";
            }
        );
    }
);
"""

        with mock.patch(
            "timecapsulesmb.device.storage.read_mast_volumes_with_output_conn",
            return_value=MaStReadResult((), raw_output),
        ) as read_mock:
            with mock.patch("timecapsulesmb.device.storage.time.sleep") as sleep_mock:
                result = wait_for_mast_volumes_conn(connection, attempts=10, delay_seconds=3)

        self.assertEqual(result.volumes, ())
        self.assertEqual(result.attempts, 1)
        self.assertEqual(result.raw_output, raw_output)
        read_mock.assert_called_once_with(connection)
        sleep_mock.assert_not_called()

    def test_wait_for_mast_volumes_stops_when_disk_has_empty_partition_table(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "")
        raw_output = """
MaSt = (
    {
        deviceName = "wd0";
        model = "Seagate Expansion HDD";
        size = 8000000000000;
        builtin = true;
        partitions = (
        );
    }
);
"""

        with mock.patch(
            "timecapsulesmb.device.storage.read_mast_volumes_with_output_conn",
            return_value=MaStReadResult((), raw_output),
        ) as read_mock:
            with mock.patch("timecapsulesmb.device.storage.time.sleep") as sleep_mock:
                result = wait_for_mast_volumes_conn(connection, attempts=10, delay_seconds=3)

        inventory = parse_mast_inventory(raw_output)
        self.assertEqual(result.volumes, ())
        self.assertEqual(result.attempts, 1)
        self.assertEqual(result.raw_output, raw_output)
        self.assertEqual(inventory[0].name, "Seagate Expansion HDD")
        self.assertEqual(inventory[0].size, "8000000000000")
        self.assertEqual(inventory[0].partitions, ())
        read_mock.assert_called_once_with(connection)
        sleep_mock.assert_not_called()

    def test_probe_mast_diagnostics_records_empty_success(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "")
        raw_output = "MaSt = (\n);\n"
        proc = subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout=raw_output, stderr="")

        with mock.patch("timecapsulesmb.device.storage.run_ssh", return_value=proc) as run_mock:
            diagnostics = probe_mast_diagnostics_conn(connection)

        self.assertEqual(diagnostics.command, MAST_PROBE_COMMAND)
        self.assertEqual(diagnostics.returncode, 0)
        self.assertEqual(diagnostics.volumes, ())
        self.assertEqual(diagnostics.stdout, raw_output)
        self.assertEqual(diagnostics.stderr, "")
        run_mock.assert_called_once()
        self.assertEqual(run_mock.call_args.args[:2], (connection, MAST_PROBE_COMMAND))
        self.assertFalse(run_mock.call_args.kwargs["check"])
        summary = mast_probe_debug_summary(diagnostics)
        self.assertEqual(summary["mast_probe_volume_count"], 0)
        self.assertEqual(summary["mast_probe_stdout_chars"], len(raw_output))
        self.assertEqual(summary["mast_probe_stdout"], raw_output)
        self.assertEqual(summary["mast_probe_stderr"], "<empty>")

    def test_probe_mast_diagnostics_records_parsed_volume(self) -> None:
        fixture = SHELL_MAST_FIXTURES[0]
        raw_output = fixture.raw.decode("utf-8", errors="replace") if isinstance(fixture.raw, bytes) else fixture.raw
        connection = SshConnection("root@10.0.0.2", "pw", "")
        proc = subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout=raw_output, stderr="")

        with mock.patch("timecapsulesmb.device.storage.run_ssh", return_value=proc):
            diagnostics = probe_mast_diagnostics_conn(connection)

        self.assertEqual(diagnostics.returncode, 0)
        self.assertEqual(diagnostics.volumes, fixture.expected)
        summary = mast_probe_debug_summary(diagnostics)
        self.assertEqual(summary["mast_probe_volume_count"], len(fixture.expected))
        self.assertEqual(summary["mast_probe_candidates"], mast_volumes_debug_summary(fixture.expected))

    def test_probe_mast_diagnostics_captures_failure_stderr(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "")
        proc = subprocess.CompletedProcess(args=["ssh"], returncode=7, stdout="", stderr="acp failed\n")

        with mock.patch("timecapsulesmb.device.storage.run_ssh", return_value=proc):
            diagnostics = probe_mast_diagnostics_conn(connection)

        self.assertEqual(diagnostics.returncode, 7)
        self.assertEqual(diagnostics.volumes, ())
        self.assertEqual(diagnostics.stderr, "acp failed\n")
        summary = mast_probe_debug_summary(diagnostics)
        self.assertEqual(summary["mast_probe_stderr_chars"], len("acp failed\n"))
        self.assertEqual(summary["mast_probe_stderr"], "acp failed\n")

    def test_mast_probe_debug_summary_bounds_long_output(self) -> None:
        diagnostics = MaStProbeDiagnostics(
            command=MAST_PROBE_COMMAND,
            returncode=0,
            volumes=(),
            stdout="a" * 10000,
            stderr="b" * 10001,
        )

        summary = mast_probe_debug_summary(diagnostics)

        self.assertEqual(summary["mast_probe_stdout_chars"], 10000)
        self.assertEqual(summary["mast_probe_stderr_chars"], 10001)
        self.assertIn("<truncated", str(summary["mast_probe_stdout"]))
        self.assertIn("<truncated", str(summary["mast_probe_stderr"]))
        self.assertLess(len(str(summary["mast_probe_stdout"])), 10000)
        self.assertLess(len(str(summary["mast_probe_stderr"])), 10001)

    def test_payload_candidate_order_is_internal_first_then_external_mast_order_in_python(self) -> None:
        external_a = MaStVolume("sd0", "dk3", "/Volumes/dk3", "USB A", "51f93e6f-dc69-524d-986d-cee4d7cb3573", False, "hfs")
        internal = MaStVolume("wd0", "dk2", "/Volumes/dk2", "Data", "f42bdb83-c265-5522-a087-25606a4d0abf", True, "hfs")
        external_b = MaStVolume("sd1", "dk4", "/Volumes/dk4", "USB B", "7d40eaac-182b-562b-a7b8-49bb5ed69c0f", False, "hfs")

        self.assertEqual(
            ordered_payload_candidate_volumes((external_a, internal, external_b)),
            (internal, external_a, external_b),
        )

    def test_select_payload_home_prefers_writable_internal_volume(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "")
        internal = MaStVolume("wd0", "dk2", "/Volumes/dk2", "Data", "f42bdb83-c265-5522-a087-25606a4d0abf", True, "hfs")
        external = MaStVolume("sd0", "dk3", "/Volumes/dk3", "USB", "51f93e6f-dc69-524d-986d-cee4d7cb3573", False, "hfs")

        with mock.patch("timecapsulesmb.device.storage.ensure_volume_root_mounted_conn", return_value=True) as mount_mock:
            with mock.patch("timecapsulesmb.device.storage.volume_root_is_writable_conn", side_effect=[True]) as writable_mock:
                selection = select_payload_home_with_diagnostics_conn(connection, (external, internal), ".samba4", wait_seconds=30)

        self.assertEqual(selection.payload_home, PayloadHome("/Volumes/dk2", "/dev/dk2", ".samba4"))
        mount_mock.assert_called_once_with(connection, "/Volumes/dk2", "/dev/dk2", wait_seconds=30)
        writable_mock.assert_called_once_with(connection, "/Volumes/dk2")

    def test_ensure_volume_root_mounted_conn_claims_diskd_without_mount_hfs_fallback(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "")
        with mock.patch("timecapsulesmb.device.storage.run_ssh", return_value=mock.Mock(returncode=0)) as run_ssh_mock:
            self.assertTrue(ensure_volume_root_mounted_conn(connection, "/Volumes/dk2", "/dev/dk2", wait_seconds=12))

        run_ssh_mock.assert_called_once()
        remote_command = run_ssh_mock.call_args.args[1]
        self.assertIn("/bin/df -k /Volumes/dk2", remote_command)
        self.assertIn("/usr/bin/tail -n +2", remote_command)
        self.assertIn("/usr/bin/acp rpc diskd.useVolume", remote_command)
        self.assertLess(remote_command.index("/usr/bin/acp rpc diskd.useVolume"), remote_command.index("/bin/df -k /Volumes/dk2"))
        self.assertIn('while [ "$diskd_attempt" -le 2 ]', remote_command)
        self.assertNotIn("mount_hfs", remote_command)
        self.assertNotIn("grep", remote_command)
        self.assertNotIn("awk", remote_command)
        self.assertNotIn("cut", remote_command)
        self.assertEqual(run_ssh_mock.call_args.kwargs["timeout"], 69)

    def test_ensure_volume_root_mounted_conn_reports_failure(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "")
        with mock.patch("timecapsulesmb.device.storage.run_ssh", return_value=mock.Mock(returncode=1)):
            self.assertFalse(ensure_volume_root_mounted_conn(connection, "/Volumes/dk2", "/dev/dk2", wait_seconds=0))

    def test_render_ensure_volume_root_mounted_script_quotes_paths(self) -> None:
        script = render_ensure_volume_root_mounted_script("/Volumes/dk 2", "/dev/dk2", 1)
        self.assertIn("mkdir -p '/Volumes/dk 2'", script)
        self.assertIn("diskd.useVolume path:s:'/Volumes/dk 2'", script)

    def test_verify_payload_home_conn_passes_for_boot_compatible_payload(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "")
        payload_home = PayloadHome("/Volumes/dk2", "/dev/dk2", ".samba4")
        with mock.patch("timecapsulesmb.device.storage.ensure_volume_root_mounted_conn", return_value=True) as mount_mock:
            with mock.patch("timecapsulesmb.device.storage.run_ssh", return_value=mock.Mock(returncode=0, stdout="ok\n")) as run_ssh_mock:
                result = verify_payload_home_conn(connection, payload_home, wait_seconds=5)

        self.assertEqual(result, PayloadVerificationResult(True, "ok"))
        mount_mock.assert_called_once_with(connection, "/Volumes/dk2", "/dev/dk2", wait_seconds=5)
        remote_command = run_ssh_mock.call_args.args[1]
        self.assertIn("[ -d /Volumes/dk2/.samba4 ]", remote_command)
        self.assertIn("[ -x /Volumes/dk2/.samba4/smbd ]", remote_command)
        self.assertIn("[ -x /Volumes/dk2/.samba4/sbin/smbd ]", remote_command)
        self.assertIn("[ -d /Volumes/dk2/.samba4/private ]", remote_command)

    def test_verify_payload_home_conn_reports_mount_and_payload_failures(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "")
        payload_home = PayloadHome("/Volumes/dk2", "/dev/dk2", ".samba4")
        with mock.patch("timecapsulesmb.device.storage.ensure_volume_root_mounted_conn", return_value=False):
            result = verify_payload_home_conn(connection, payload_home, wait_seconds=5)
        self.assertEqual(result, PayloadVerificationResult(False, "volume /Volumes/dk2 is not mounted"))

        with mock.patch("timecapsulesmb.device.storage.ensure_volume_root_mounted_conn", return_value=True):
            with mock.patch(
                "timecapsulesmb.device.storage.run_ssh",
                return_value=mock.Mock(returncode=1, stdout="missing smbd; missing private directory\n"),
            ):
                result = verify_payload_home_conn(connection, payload_home, wait_seconds=5)
        self.assertEqual(result, PayloadVerificationResult(False, "missing smbd; missing private directory"))

    def test_select_payload_home_skips_unmountable_internal_before_external(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "")
        internal = MaStVolume("wd0", "dk2", "/Volumes/dk2", "Data", "f42bdb83-c265-5522-a087-25606a4d0abf", True, "hfs")
        external = MaStVolume("sd0", "dk3", "/Volumes/dk3", "USB", "51f93e6f-dc69-524d-986d-cee4d7cb3573", False, "hfs")

        with mock.patch("timecapsulesmb.device.storage.ensure_volume_root_mounted_conn", side_effect=[False, True]) as mount_mock:
            with mock.patch("timecapsulesmb.device.storage.volume_root_is_writable_conn", return_value=True) as writable_mock:
                selection = select_payload_home_with_diagnostics_conn(connection, (external, internal), ".samba4", wait_seconds=9)

        self.assertEqual(selection.payload_home, PayloadHome("/Volumes/dk3", "/dev/dk3", ".samba4"))
        self.assertEqual(
            mount_mock.call_args_list,
            [
                mock.call(connection, "/Volumes/dk2", "/dev/dk2", wait_seconds=9),
                mock.call(connection, "/Volumes/dk3", "/dev/dk3", wait_seconds=9),
            ],
        )
        writable_mock.assert_called_once_with(connection, "/Volumes/dk3")

    def test_select_payload_home_with_diagnostics_records_mount_and_write_results(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "")
        internal = MaStVolume("wd0", "dk2", "/Volumes/dk2", "Data", "f42bdb83-c265-5522-a087-25606a4d0abf", True, "hfs")
        external = MaStVolume("sd0", "dk3", "/Volumes/dk3", "USB", "51f93e6f-dc69-524d-986d-cee4d7cb3573", False, "hfs")

        with mock.patch("timecapsulesmb.device.storage.ensure_volume_root_mounted_conn", side_effect=[False, True]):
            with mock.patch("timecapsulesmb.device.storage.volume_root_is_writable_conn", return_value=True):
                selection = select_payload_home_with_diagnostics_conn(
                    connection,
                    (external, internal),
                    ".samba4",
                    wait_seconds=9,
                )

        self.assertEqual(selection.payload_home, PayloadHome("/Volumes/dk3", "/dev/dk3", ".samba4"))
        self.assertEqual(selection.checks[0].volume, internal)
        self.assertFalse(selection.checks[0].mounted)
        self.assertIsNone(selection.checks[0].writable)
        self.assertEqual(selection.checks[1].volume, external)
        self.assertTrue(selection.checks[1].mounted)
        self.assertTrue(selection.checks[1].writable)
        self.assertEqual(
            payload_candidate_checks_debug_summary(selection.checks),
            [
                {
                    "disk": "wd0",
                    "part": "dk2",
                    "root": "/Volumes/dk2",
                    "name": "Data",
                    "format": "hfs",
                    "builtin": True,
                    "uuid": "f42bdb83-c265-5522-a087-25606a4d0abf",
                    "mounted": False,
                    "writable": None,
                },
                {
                    "disk": "sd0",
                    "part": "dk3",
                    "root": "/Volumes/dk3",
                    "name": "USB",
                    "format": "hfs",
                    "builtin": False,
                    "uuid": "51f93e6f-dc69-524d-986d-cee4d7cb3573",
                    "mounted": True,
                    "writable": True,
                },
            ],
        )

    def test_select_payload_home_with_diagnostics_returns_no_home_when_all_unwritable(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "")
        internal = MaStVolume("wd0", "dk2", "/Volumes/dk2", "Data", "f42bdb83-c265-5522-a087-25606a4d0abf", True, "hfs")
        external = MaStVolume("sd0", "dk3", "/Volumes/dk3", "USB", "51f93e6f-dc69-524d-986d-cee4d7cb3573", False, "hfs")

        with mock.patch("timecapsulesmb.device.storage.ensure_volume_root_mounted_conn", return_value=True):
            with mock.patch("timecapsulesmb.device.storage.volume_root_is_writable_conn", return_value=False):
                selection = select_payload_home_with_diagnostics_conn(
                    connection,
                    (internal, external),
                    ".samba4",
                    wait_seconds=30,
                )

        self.assertIsNone(selection.payload_home)
        self.assertEqual([check.mounted for check in selection.checks], [True, True])
        self.assertEqual([check.writable for check in selection.checks], [False, False])

    def test_volume_root_writable_timeout_raises_coded_disk_write_error(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "")
        with mock.patch(
            "timecapsulesmb.device.storage.run_ssh",
            side_effect=SshCommandTimeout("Timed out waiting for ssh command to finish: mkdir write test"),
        ):
            with self.assertRaises(StorageDeviceError) as raised:
                volume_root_is_writable_conn(connection, "/Volumes/dk2")

        self.assertEqual(raised.exception.code, "disk_write_test_unresponsive")
        self.assertIn("The disk did not respond when tested.", str(raised.exception))
        self.assertIsInstance(raised.exception.__cause__, SshCommandTimeout)

    def test_select_payload_home_records_unmountable_candidates(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "")
        internal = MaStVolume("wd0", "dk2", "/Volumes/dk2", "Data", "f42bdb83-c265-5522-a087-25606a4d0abf", True, "hfs")
        external = MaStVolume("sd0", "dk3", "/Volumes/dk3", "USB", "51f93e6f-dc69-524d-986d-cee4d7cb3573", False, "hfs")

        with mock.patch("timecapsulesmb.device.storage.ensure_volume_root_mounted_conn", return_value=False):
            with mock.patch("timecapsulesmb.device.storage.volume_root_is_writable_conn") as writable_mock:
                selection = select_payload_home_with_diagnostics_conn(connection, (internal, external), ".samba4", wait_seconds=30)

        self.assertIsNone(selection.payload_home)
        self.assertEqual([check.mounted for check in selection.checks], [False, False])
        writable_mock.assert_not_called()

    def test_select_payload_home_falls_back_to_external_and_records_none_writable(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "")
        internal = MaStVolume("wd0", "dk2", "/Volumes/dk2", "Data", "f42bdb83-c265-5522-a087-25606a4d0abf", True, "hfs")
        external = MaStVolume("sd0", "dk3", "/Volumes/dk3", "USB", "51f93e6f-dc69-524d-986d-cee4d7cb3573", False, "hfs")

        with mock.patch("timecapsulesmb.device.storage.ensure_volume_root_mounted_conn", return_value=True):
            with mock.patch("timecapsulesmb.device.storage.volume_root_is_writable_conn", side_effect=[False, True]):
                selection = select_payload_home_with_diagnostics_conn(connection, (internal, external), ".samba4", wait_seconds=30)
        self.assertEqual(selection.payload_home, PayloadHome("/Volumes/dk3", "/dev/dk3", ".samba4"))

        with mock.patch("timecapsulesmb.device.storage.ensure_volume_root_mounted_conn", return_value=True):
            with mock.patch("timecapsulesmb.device.storage.volume_root_is_writable_conn", return_value=False):
                selection = select_payload_home_with_diagnostics_conn(connection, (internal, external), ".samba4", wait_seconds=30)
        self.assertIsNone(selection.payload_home)
        self.assertEqual([check.writable for check in selection.checks], [False, False])

    def test_flash_runtime_config_contains_runtime_settings_and_no_share_name(self) -> None:
        config = AppConfig.from_values(
            {
                "TC_SAMBA_USER": "admin",
                "TC_MDNS_DEVICE_MODEL": "TimeCapsule6,106",
                "TC_AIRPORT_SYAP": "106",
                "TC_INTERNAL_SHARE_USE_DISK_ROOT": "true",
                "TC_SMB_BROWSE_COMPATIBILITY": "true",
                "TC_ANY_PROTOCOL": "true",
                "TC_FRUIT_METADATA_NETATALK": "true",
                "TC_VFS_AIO_FORK_ENABLED": "true",
            }
        )

        rendered = render_flash_runtime_config(
            config,
            PayloadHome("/Volumes/dk2", "/dev/dk2", ".samba4"),
            debug_logging=True,
        )

        self.assertNotIn("PAYLOAD_DIR_NAME", rendered)
        self.assertNotIn("SMB_SAMBA_USER", rendered)
        self.assertNotIn("MDNS_DEVICE_MODEL", rendered)
        self.assertNotIn("AIRPORT_SYAP", rendered)
        self.assertNotIn("NET_IFACE", rendered)
        self.assertNotIn("NET_IPV4_HINT", rendered)
        self.assertNotIn("PAYLOAD_VOLUME_HINT", rendered)
        self.assertNotIn("PAYLOAD_DEVICE_HINT", rendered)
        self.assertNotIn("PAYLOAD_INSTALL_ID", rendered)
        self.assertIn(f"TC_DEPLOY_RELEASE_TAG={RELEASE_TAG}\n", rendered)
        self.assertIn(f"TC_DEPLOY_CLI_VERSION_CODE={CLI_VERSION_CODE}\n", rendered)
        self.assertIn("TELEMETRY=true\n", rendered)
        self.assertIn("INTERNAL_SHARE_USE_DISK_ROOT=1\n", rendered)
        self.assertNotIn("SMB_BIND_LAN_ONLY", rendered)
        self.assertIn("SMB_BROWSE_COMPATIBILITY=1\n", rendered)
        self.assertIn("MDNS_ADVERTISE_AFP=0\n", rendered)
        self.assertIn("ANY_PROTOCOL=1\n", rendered)
        self.assertIn("REQUIRE_SMB_ENCRYPTION=0\n", rendered)
        self.assertIn("FORCE_DISABLE_SMB_SIGNING_AND_ENCRYPTION=0\n", rendered)
        self.assertIn("FRUIT_METADATA_NETATALK=1\n", rendered)
        self.assertIn("VFS_AIO_FORK_ENABLED=1\n", rendered)
        self.assertIn("DISKD_USE_VOLUME_ATTEMPTS=2\n", rendered)
        self.assertIn("ATA_IDLE_SECONDS=300\n", rendered)
        self.assertIn("ATA_STANDBY=''\n", rendered)
        self.assertNotIn("NBNS_ENABLED=", rendered)
        self.assertIn("RSYNC_ENABLED=0\n", rendered)
        self.assertIn("SMBD_DEBUG_LOGGING=1\n", rendered)
        self.assertNotIn("SMB_NETBIOS_NAME", rendered)
        self.assertNotIn("TC_CONFIG_VERSION", rendered)
        # Name overrides travel only when the user set them (v3.1.0: the native
        # helpers read them from this file).
        self.assertNotIn("TC_MDNS_INSTANCE_NAME", rendered)
        self.assertNotIn("TC_NETBIOS_NAME", rendered)
        self.assertNotIn("MDNS_HOST_LABEL", rendered)

    def test_flash_runtime_config_ignores_deprecated_name_overrides(self) -> None:
        config = AppConfig.from_values(
            {
                "TC_MDNS_INSTANCE_NAME": "James's Capsule",
                "TC_NETBIOS_NAME": "JAMESCAP",
                "TC_MDNS_DEVICE_MODEL": "TimeCapsule6,106",
            }
        )
        rendered = render_flash_runtime_config(
            config,
            PayloadHome("/Volumes/dk2", "/dev/dk2", ".samba4"),
            debug_logging=False,
        )
        # Old configs remain loadable, but neither shell nor native runtime
        # receives an override. Exercise the rendered shell environment too.
        proc = subprocess.run(["/bin/sh", "-c", rendered + "printf '%s|%s\\n' \"${TC_MDNS_INSTANCE_NAME-unset}\" \"${TC_NETBIOS_NAME-unset}\""],
                              capture_output=True, text=True, check=True)
        self.assertEqual(proc.stdout, "unset|unset\n")
        self.assertNotIn("TC_SHARE_NAME", rendered)

    def test_flash_runtime_config_can_disable_telemetry(self) -> None:
        rendered = render_flash_runtime_config(
            AppConfig.from_values({}),
            PayloadHome("/Volumes/dk2", "/dev/dk2", ".samba4"),
            telemetry_enabled=False,
        )

        self.assertIn("TELEMETRY=false\n", rendered)

    def test_flash_runtime_config_can_enable_rsync(self) -> None:
        rendered = render_flash_runtime_config(
            AppConfig.from_values({}),
            PayloadHome("/Volumes/dk2", "/dev/dk2", ".samba4"),
            rsync_enabled=True,
        )

        self.assertIn("RSYNC_ENABLED=1\n", rendered)

    def test_rsync_daemon_config_chroots_payload_volume_share_root_without_pid_file(self) -> None:
        rendered = render_rsync_daemon_config(PayloadHome("/Volumes/dk5", "/dev/dk5", ".samba4"))

        self.assertEqual(
            rendered,
            textwrap.dedent(
                """\
                port = 873
                log file = /mnt/Memory/samba4/var/rsync.log
                uid = root
                gid = wheel
                use chroot = yes
                read only = false
                list = true

                [shareroot]
                path = /Volumes/dk5/ShareRoot
                """
            ),
        )
        self.assertNotIn("pid file", rendered.lower())

    def test_flash_runtime_config_uses_saved_debug_logging(self) -> None:
        config = AppConfig.from_values({"TC_DEBUG_LOGGING": "true"})

        rendered = render_gui_flash_runtime_config(
            config,
            PayloadHome("/Volumes/dk2", "/dev/dk2", ".samba4"),
            debug_logging=None,
        )

        self.assertIn("SMBD_DEBUG_LOGGING=1\n", rendered)
        self.assertIn("MDNS_DEBUG_LOGGING=1\n", rendered)

    def test_flash_runtime_config_deploy_time_debug_override_can_disable_saved_value(self) -> None:
        config = AppConfig.from_values({"TC_DEBUG_LOGGING": "true"})

        rendered = render_flash_runtime_config(
            config,
            PayloadHome("/Volumes/dk2", "/dev/dk2", ".samba4"),
            debug_logging=False,
        )

        self.assertIn("SMBD_DEBUG_LOGGING=0\n", rendered)
        self.assertIn("MDNS_DEBUG_LOGGING=0\n", rendered)

    def test_flash_runtime_config_accepts_deploy_time_advanced_overrides(self) -> None:
        config = AppConfig.from_values(
            {
                "TC_INTERNAL_SHARE_USE_DISK_ROOT": "false",
                "TC_SMB_BROWSE_COMPATIBILITY": "false",
                "TC_ANY_PROTOCOL": "false",
                "TC_FRUIT_METADATA_NETATALK": "false",
                "TC_VFS_AIO_FORK_ENABLED": "false",
            }
        )

        rendered = render_gui_flash_runtime_config(
            config,
            PayloadHome("/Volumes/dk2", "/dev/dk2", ".samba4"),
            debug_logging=False,
            internal_share_use_disk_root=True,
            smb_browse_compatibility=True,
            mdns_advertise_afp=True,
            any_protocol=True,
            fruit_metadata_netatalk=True,
            vfs_aio_fork_enabled=True,
        )

        self.assertIn("INTERNAL_SHARE_USE_DISK_ROOT=1\n", rendered)
        self.assertNotIn("SMB_BIND_LAN_ONLY", rendered)
        self.assertIn("SMB_BROWSE_COMPATIBILITY=1\n", rendered)
        self.assertIn("MDNS_ADVERTISE_AFP=1\n", rendered)
        self.assertIn("ANY_PROTOCOL=1\n", rendered)
        self.assertIn("REQUIRE_SMB_ENCRYPTION=0\n", rendered)
        self.assertIn("FRUIT_METADATA_NETATALK=1\n", rendered)
        self.assertIn("VFS_AIO_FORK_ENABLED=1\n", rendered)

    def test_flash_runtime_config_requires_smb_encryption(self) -> None:
        config = AppConfig.from_values(
            {
                "TC_ANY_PROTOCOL": "false",
                "TC_REQUIRE_SMB_ENCRYPTION": "true",
            }
        )

        rendered = render_flash_runtime_config(
            config,
            PayloadHome("/Volumes/dk2", "/dev/dk2", ".samba4"),
        )

        self.assertIn("ANY_PROTOCOL=0\n", rendered)
        self.assertIn("REQUIRE_SMB_ENCRYPTION=1\n", rendered)

    def test_flash_runtime_config_forces_smb_signing_and_encryption_off(self) -> None:
        config = AppConfig.from_values(
            {"TC_FORCE_DISABLE_SMB_SIGNING_AND_ENCRYPTION": "true"}
        )

        rendered = render_flash_runtime_config(
            config,
            PayloadHome("/Volumes/dk2", "/dev/dk2", ".samba4"),
        )

        self.assertIn("REQUIRE_SMB_ENCRYPTION=0\n", rendered)
        self.assertIn("FORCE_DISABLE_SMB_SIGNING_AND_ENCRYPTION=1\n", rendered)

    def test_flash_runtime_config_rejects_required_and_disabled_smb_encryption(self) -> None:
        config = AppConfig.from_values(
            {
                "TC_REQUIRE_SMB_ENCRYPTION": "true",
                "TC_FORCE_DISABLE_SMB_SIGNING_AND_ENCRYPTION": "true",
            }
        )

        with self.assertRaisesRegex(ValueError, "cannot be used with Force Disable"):
            render_flash_runtime_config(
                config,
                PayloadHome("/Volumes/dk2", "/dev/dk2", ".samba4"),
            )

    def test_flash_runtime_config_rejects_any_protocol_with_smb_encryption(self) -> None:
        config = AppConfig.from_values(
            {
                "TC_ANY_PROTOCOL": "true",
                "TC_REQUIRE_SMB_ENCRYPTION": "true",
            }
        )

        with self.assertRaisesRegex(ValueError, "SMB encryption requires SMB3-only"):
            render_flash_runtime_config(
                config,
                PayloadHome("/Volumes/dk2", "/dev/dk2", ".samba4"),
            )

    def test_flash_runtime_config_deploy_time_overrides_can_disable_saved_values(self) -> None:
        config = AppConfig.from_values(
            {
                "TC_INTERNAL_SHARE_USE_DISK_ROOT": "true",
                "TC_SMB_BROWSE_COMPATIBILITY": "true",
                "TC_MDNS_ADVERTISE_AFP": "true",
                "TC_ANY_PROTOCOL": "true",
                "TC_FRUIT_METADATA_NETATALK": "true",
                "TC_VFS_AIO_FORK_ENABLED": "true",
            }
        )

        rendered = render_gui_flash_runtime_config(
            config,
            PayloadHome("/Volumes/dk2", "/dev/dk2", ".samba4"),
            debug_logging=False,
            internal_share_use_disk_root=False,
            smb_browse_compatibility=False,
            mdns_advertise_afp=False,
            any_protocol=False,
            fruit_metadata_netatalk=False,
            vfs_aio_fork_enabled=False,
        )

        self.assertIn("INTERNAL_SHARE_USE_DISK_ROOT=0\n", rendered)
        self.assertNotIn("SMB_BIND_LAN_ONLY", rendered)
        self.assertIn("SMB_BROWSE_COMPATIBILITY=0\n", rendered)
        self.assertIn("MDNS_ADVERTISE_AFP=0\n", rendered)
        self.assertIn("ANY_PROTOCOL=0\n", rendered)
        self.assertIn("FRUIT_METADATA_NETATALK=0\n", rendered)
        self.assertIn("VFS_AIO_FORK_ENABLED=0\n", rendered)


    def test_flash_runtime_config_uses_drive_settings_from_config(self) -> None:
        config = AppConfig.from_values(
            {
                "TC_ATA_IDLE_SECONDS": "0",
                "TC_ATA_STANDBY": "0",
            }
        )

        rendered = render_flash_runtime_config(
            config,
            PayloadHome("/Volumes/dk2", "/dev/dk2", ".samba4"),
            debug_logging=False,
        )

        self.assertIn("ATA_IDLE_SECONDS=0\n", rendered)
        self.assertIn("ATA_STANDBY=0\n", rendered)


    def test_deployment_plan_uses_flash_pointer_and_single_private_payload(self) -> None:
        plan = build_deployment_plan(
            "root@10.0.0.2",
            PayloadHome("/Volumes/dk2", "/dev/dk2", ".samba4"),
            Path("/tmp/smbd"),

            xattr_migrator_path=Path("/tmp/xattr-hfs-migrate"),
            rsync_path=Path("/tmp/rsync"),
         service_path=Path("bin/service"))
        source_ids = {upload.source_id for upload in plan.uploads}

        self.assertNotIn(GENERATED_FLASH_CONFIG_SOURCE, source_ids)
        self.assertEqual(plan.config_upload.source_id, GENERATED_FLASH_CONFIG_SOURCE)
        self.assertNotIn("rendered:smb.conf.template", source_ids)
        self.assertNotIn("generated:adisk.uuid", source_ids)
        self.assertNotIn("generated:nbns.enabled", source_ids)
        self.assertNotIn("generated:install.id", source_ids)
        self.assertEqual(plan.private_dir, "/Volumes/dk2/.samba4/private")
        self.assertEqual(plan.flash_targets["tcapsulesmb.conf"], "/mnt/Flash/tcapsulesmb.conf")
        self.assertIn("/Volumes/dk2/.samba4/smb.conf.template", {action.path for action in plan.replace_software_actions if hasattr(action, "path")})
        self.assertIn("/Volumes/dk2/.samba4/private/adisk.uuid", {action.path for action in plan.replace_software_actions if hasattr(action, "path")})
        self.assertIn("/Volumes/dk2/.samba4/private/nbns.enabled", {action.path for action in plan.replace_software_actions if hasattr(action, "path")})
        # The installer applies config permissions after verified migration.
        self.assertNotIn(
            ("/mnt/Flash/tcapsulesmb.conf", "600"),
            {(permission.path, permission.mode) for permission in plan.permissions},
        )


    # ---- v3.1.0 boot: Apple mDNSResponder stays, diskd moves to loopback ----


    # ---- v3.1.0 discovery policy and registrant restarts ----


if __name__ == "__main__":
    unittest.main()
