from __future__ import annotations

import shlex
import subprocess
import sys
import tempfile
import os
import plistlib
import unittest
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from tests.test_xattr_migration import fake_inventory
from timecapsulesmb.app.recovery import cli_steps
from timecapsulesmb.device.migration_jobs import MigrationActivity, MigrationProgress, RunningMigration
from timecapsulesmb.core.messages import NETBSD4_ACTIVATION_COMPLETED, netbsd4_activation_summary
from timecapsulesmb.core.summaries import Summary
from timecapsulesmb.deploy.commands import (
    EnsureVolumeMountedAction,
    InstallPermissionsAction,
    PrepareDirsAction,
    RemotePermission,
    RemoteSymlink,
    RemovePathsAction,
    RunScriptAction,
    StopServiceRuntimeAction,
    StopProcessAction,
    remote_action_to_jsonable,
    render_remote_action,
)
from timecapsulesmb.deploy.dry_run import format_deployment_plan
from timecapsulesmb.deploy.executor import (
    FLUSH_REMOTE_FILESYSTEMS_COMMAND,
    FLUSH_REMOTE_FILESYSTEMS_TIMEOUT_SECONDS,
    XattrMigrationResult,
    flush_remote_filesystem_writes,
    migrate_xattr_tdb_to_hfs,
    run_remote_actions,
    remote_uninstall_payload,
    upload_deployment_payload,
)
from timecapsulesmb.deploy.migration import (
    MigrationExitError,
    MigrationInventory,
    MigrationStalledError,
    OversizedSummary,
    OversizedValue,
    RAM_HELPER,
    SKIP_REASONS,
    SkippedEntry,
    SkippedSummary,
)
from timecapsulesmb.deploy.planner import (
    BINARY_SERVICE_SOURCE,
    BINARY_RSYNC_SOURCE,
    BINARY_SMBD_SOURCE,
    BINARY_XATTR_MIGRATOR_SOURCE,
    DEFAULT_APPLE_MOUNT_WAIT_SECONDS,
    DEPLOY_STARTUP_REBOOT_THEN_ACTIVATE,
    DEPLOY_STARTUP_REBOOT_THEN_VERIFY,
    GENERATED_FLASH_CONFIG_SOURCE,
    GENERATED_RSYNC_CONFIG_SOURCE,
    PACKAGED_RC_LOCAL_SOURCE,
    PAYLOAD_BINARY_UPLOAD_TIMEOUT_SECONDS,
    build_deployment_plan,
    build_uninstall_plan,
)
from timecapsulesmb.deploy.boot_assets import (
    boot_asset_path,
    load_boot_asset_text,
)
from timecapsulesmb.deploy.verify import (
    VerificationResult,
    render_managed_runtime_verification,
    render_post_uninstall_verification,
    verify_post_uninstall,
)
from timecapsulesmb.core.config import AppConfig
from tests.device_ps import FakePs, process
from timecapsulesmb.device.processes import (
    PS_CAPTURE_COMMAND,
    PS_LISTING_END,
    STUCK_PROCESS_PS_COMMAND,
    UNREADABLE_COMMAND,
    render_process_present_by_ucomm,
)
from timecapsulesmb.device.probe import (
    RuntimeNamingIdentityProbeResult,
    DISCOVERY_UNREADABLE,
    DISKD_UNREADABLE,
    DISK_PAYLOAD_PROBLEMS,
    RSYNC_NOT_RUNNING_STEP,
    ACPD_QUIT_STEP,
    MANAGER_UNREADABLE,
    runtime_payload_problem,
    FOREIGN_WCIFSND_RUNNING,
    NATIVE_NBNS_OFF_DETAIL,
    MDNS_BINARY_PROBE_TIMEOUT_SECONDS,
    MDNS_FSTAT_PROBE_TIMEOUT_SECONDS,
    MDNS_PROCESS_TABLE_PROBE_TIMEOUT_SECONDS,
    MDNS_SOCKET_FAMILIES_PROBE_TIMEOUT_SECONDS,
    ManagedRuntimeProbeResult,
    ManagerState,
    ProbeStepResult,
    ReadinessProbeResult,
    SMBD_STATUS_HELPERS,
    _parse_link_plan,
    apple_bonjour_host_label,
    derive_runtime_naming_identity,
    extract_airport_identity_from_acp_output,
    extract_airport_identity_from_text,
    probe_remote_runtime_naming_identity_conn,
    probe_device_conn,
    probe_managed_runtime_conn,
    probe_managed_runtime_once_conn,
    probe_managed_mdns_conn,
    probe_managed_rsync_conn,
    probe_managed_smbd_conn,
    probe_remote_airport_identity_conn,
)
from timecapsulesmb.device.storage import (
    MaStDiscoveryResult,
    MaStVolume,
    PayloadHome,
    PayloadVerificationResult,
    mounted_mast_volumes_conn,
)
from timecapsulesmb.services.activation import activate_runtime, decide_manual_activation
from timecapsulesmb.device.disk_errors import DiskFailingError
from timecapsulesmb.device.errors import AcpdQuitError, DeviceError, DiskPayloadError, RsyncNotRunningError
from timecapsulesmb.device.compat import DeviceCompatibility
from timecapsulesmb.services.callbacks import OperationCallbacks
from timecapsulesmb.services.deploy import (
    DeployArtifactPaths,
    DeployCompletionMessages,
    DeployDeviceError,
    DeployPayloadContext,
    DeployRuntimeConfig,
    INSTALL_INTERRUPTED_MESSAGE,
    PreparedDeployPlan,
    _oversized_summaries,
    _share_path,
    _skipped_summaries,
    complete_deployment_after_upload,
    device_has_no_internal_disk,
    mast_no_volume_error,
    upload_and_verify_deployment_payload,
)
from timecapsulesmb.services.runtime_verification import (
    recheck_cached_adisk,
    verify_managed_runtime_ready,
)
from timecapsulesmb.transport.errors import SshUploadTimeout
from timecapsulesmb.transport.ssh import SshCommandTimeout, SshConnection, SshError, upload_timeout_seconds


def manager_state(problems, *, acpd_running: bool | None = True) -> ManagerState | None:
    """read_manager_state's reading of a listing with these title verdicts;
    None (no reading) for None."""
    return None if problems is None else ManagerState(tuple(problems), acpd_running)


def readiness_result(ready: bool, detail: str, lines: tuple[str, ...]) -> ReadinessProbeResult:
    steps = []
    for index, line in enumerate(lines):
        if line.startswith("PASS:"):
            steps.append(ProbeStepResult(f"test_{index}", "pass", line.removeprefix("PASS:")))
        elif line.startswith("FAIL:"):
            steps.append(ProbeStepResult(f"test_{index}", "fail", line.removeprefix("FAIL:")))
        else:
            steps.append(ProbeStepResult(f"test_{index}", "fail", line))
    return ReadinessProbeResult(ready=ready, detail=detail, steps=tuple(steps))



def removed_paths(actions) -> set[str]:
    return {path for action in actions if isinstance(action, RemovePathsAction) for path in action.paths}

class DeployModuleTests(unittest.TestCase):
    def setUp(self):
        from tests.test_xattr_migration import fake_inventory
        for target, kwargs in (
            ("inventory_metadata", {"side_effect": lambda *_a: fake_inventory()}),
            ("probe_migration_activity", {"return_value": MigrationActivity(())}),
            ("inspect_sources", {}),
            # Reads the migration log over SSH after a failed walk.
            ("unreadable_folder", {"return_value": None}),
            ("flush_remote_filesystem_writes", {}),
            # No flash file holds its new bytes yet: every file is written.
            ("_flash_files_holding_new_bytes", {"return_value": set()}),
        ):
            patcher = mock.patch("timecapsulesmb.services.deploy." + target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)

    _nbns_binary_tmpdir: tempfile.TemporaryDirectory[str] | None = None
    _nbns_binary_path: Path | None = None

    @classmethod
    def tearDownClass(cls) -> None:
        if cls._nbns_binary_tmpdir is not None:
            cls._nbns_binary_tmpdir.cleanup()
        cls._nbns_binary_tmpdir = None
        cls._nbns_binary_path = None

    def _payload_home(self, volume_root: str = "/Volumes/dk2", payload_dir_name: str = "samba4") -> PayloadHome:
        disk_key = volume_root.rstrip("/").rsplit("/", 1)[-1]
        return PayloadHome(volume_root, f"/dev/{disk_key}", payload_dir_name)

    def _mast_volume(
        self,
        partition_device: str = "dk2",
        *,
        disk_device: str = "wd0",
        name: str = "Data",
        builtin: bool = True,
    ) -> MaStVolume:
        return MaStVolume(
            disk_device,
            partition_device,
            f"/Volumes/{partition_device}",
            name,
            "12345678-1234-1234-1234-123456789012",
            builtin,
            "hfs",
        )

    def _prepared_deploy_plan(
        self,
        *,
        startup_mode=DEPLOY_STARTUP_REBOOT_THEN_VERIFY,
        payload_family: str = "netbsd6_samba4",
        is_netbsd4: bool = False,
        wait_after_reboot: bool = True,
        rc_local_autostart: bool = False,
    ) -> PreparedDeployPlan:
        payload_home = self._payload_home()
        # Deploy records the CRC-32 of the binaries the device stages to RAM,
        # so these two must exist.
        binaries = tempfile.TemporaryDirectory()
        self.addCleanup(binaries.cleanup)
        smbd, rsync = Path(binaries.name) / "smbd", Path(binaries.name) / "rsync"
        smbd.write_bytes(b"smbd")
        rsync.write_bytes(b"rsync")
        plan = build_deployment_plan(
            "root@10.0.0.2",
            payload_home,
            smbd,

            xattr_migrator_path=Path("bin/xattr-hfs-migrate"),
            rsync_path=rsync,
            startup_mode=startup_mode,
            wait_after_reboot=wait_after_reboot,
         service_path=Path("bin/service"))
        return PreparedDeployPlan(
            payload_context=DeployPayloadContext(
                compatibility=mock.Mock(),
                payload_family=payload_family,
                is_netbsd4=is_netbsd4,
                startup_mode=startup_mode,
                rc_local_autostart=rc_local_autostart,
            ),
            artifacts=DeployArtifactPaths(
                smbd=Path("bin/smbd"),
                xattr_migrator=Path("bin/xattr-hfs-migrate"),

                rsync=Path("bin/rsync"),
             service=Path("bin/service")),
            payload_home=payload_home,
            plan=plan,
        )

    def _operation_callbacks(self):
        stages: list[str] = []
        logs: list[str] = []
        debug_fields: dict[str, object] = {}
        finish_fields: dict[str, object] = {}
        return (
            OperationCallbacks(
                set_stage=stages.append,
                log=logs.append,
                add_debug_fields=debug_fields.update,
                update_fields=finish_fields.update,
            ),
            stages,
            logs,
            debug_fields,
            finish_fields,
        )

    def test_flush_remote_filesystem_writes_syncs_and_waits(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")
        with mock.patch("timecapsulesmb.deploy.executor.run_ssh") as run_ssh_mock:
            flush_remote_filesystem_writes(connection)
        run_ssh_mock.assert_called_once_with(
            connection,
            FLUSH_REMOTE_FILESYSTEMS_COMMAND,
            timeout=FLUSH_REMOTE_FILESYSTEMS_TIMEOUT_SECONDS,
        )
        self.assertIn("/bin/sync", FLUSH_REMOTE_FILESYSTEMS_COMMAND)
        self.assertIn("/bin/sleep 10", FLUSH_REMOTE_FILESYSTEMS_COMMAND)
        self.assertGreaterEqual(FLUSH_REMOTE_FILESYSTEMS_TIMEOUT_SECONDS, 300)

    def test_run_remote_actions_reports_completed_actions(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")
        actions = [StopServiceRuntimeAction(), RemovePathsAction(("/tmp/tc-old",))]
        completed = []
        with mock.patch("timecapsulesmb.deploy.executor.run_ssh") as run_ssh_mock:
            run_remote_actions(
                connection,
                actions,
                on_action_done=lambda action, index, total: completed.append((action, index, total)),
            )

        self.assertEqual(run_ssh_mock.call_count, 2)
        self.assertEqual(completed, [(actions[0], 1, 2), (actions[1], 2, 2)])
        # One rm removes every listed path, so it gets the longer bound.
        self.assertEqual([call.kwargs["timeout"] for call in run_ssh_mock.call_args_list], [120, 300])

    def test_load_boot_asset_text_reads_packaged_asset(self) -> None:
        with boot_asset_path("boot.sh") as path:
            self.assertEqual(load_boot_asset_text("boot.sh"), path.read_text())

    def test_extract_airport_identity_from_text_finds_time_capsule_model(self) -> None:
        result = extract_airport_identity_from_text("prefix\x00psyAM\x00pTimeCapsule6,113\x00suffix")
        self.assertEqual(result.model, "TimeCapsule6,113")
        self.assertEqual(result.syap, "113")
        self.assertIn("TimeCapsule6,113", result.detail)

    def test_extract_airport_identity_from_text_ignores_garbage(self) -> None:
        result = extract_airport_identity_from_text("prefix\x00not a model\x00suffix")
        self.assertIsNone(result.model)
        self.assertIsNone(result.syap)
        self.assertIn("no supported AirPort model", result.detail)

    def test_extract_airport_identity_from_text_finds_airport_extreme_model(self) -> None:
        result = extract_airport_identity_from_text("prefix\x00psyAM\x00pAirPort7,120\x00suffix")
        self.assertEqual(result.model, "AirPort7,120")
        self.assertEqual(result.syap, "120")
        self.assertIn("AirPort7,120", result.detail)

    def test_extract_airport_identity_from_acp_output_parses_labeled_hex_syap_and_model(self) -> None:
        result = extract_airport_identity_from_acp_output("syAP=0x00000077\nsyAM=TimeCapsule8,119\n")
        self.assertEqual(result.model, "TimeCapsule8,119")
        self.assertEqual(result.syap, "119")

    def test_extract_airport_identity_from_acp_output_parses_airport_extreme_model(self) -> None:
        result = extract_airport_identity_from_acp_output("syAP=0x00000078\nsyAM=AirPort7,120\n")
        self.assertEqual(result.model, "AirPort7,120")
        self.assertEqual(result.syap, "120")

    def test_extract_airport_identity_from_acp_output_parses_decimal_syap(self) -> None:
        result = extract_airport_identity_from_acp_output("syAP=113\n")
        self.assertEqual(result.model, "TimeCapsule6,113")
        self.assertEqual(result.syap, "113")

    def test_extract_airport_identity_from_acp_output_parses_unlabeled_numeric_syap(self) -> None:
        result = extract_airport_identity_from_acp_output("noise\n0x00000077\n")
        self.assertEqual(result.model, "TimeCapsule8,119")
        self.assertEqual(result.syap, "119")

    def test_extract_airport_identity_from_acp_output_ignores_punctuated_unlabeled_numeric_like_lines(self) -> None:
        result = extract_airport_identity_from_acp_output("119:\n113/extra\n")
        self.assertIsNone(result.model)
        self.assertIsNone(result.syap)
        self.assertIn("no supported AirPort identity found", result.detail)

    def test_extract_airport_identity_from_acp_output_derives_syap_from_model_without_syap(self) -> None:
        result = extract_airport_identity_from_acp_output("syAM=TimeCapsule6,106\n")
        self.assertEqual(result.model, "TimeCapsule6,106")
        self.assertEqual(result.syap, "106")

    def test_extract_airport_identity_from_acp_output_reports_model_syap_mismatch(self) -> None:
        result = extract_airport_identity_from_acp_output("syAP=0x00000078\nsyAM=TimeCapsule8,119\n")
        self.assertIsNone(result.model)
        self.assertIsNone(result.syap)
        self.assertIn("expects syAP 119, got 120", result.detail)

    def test_extract_airport_identity_from_acp_output_reports_malformed_syap_without_model(self) -> None:
        result = extract_airport_identity_from_acp_output("syAP=not-a-number\n")
        self.assertIsNone(result.model)
        self.assertIsNone(result.syap)
        self.assertIn("not parseable", result.detail)

    def test_probe_remote_airport_identity_reads_optional_mac_in_one_session(self) -> None:
        import shlex
        from timecapsulesmb.device import probe
        connection = SshConnection("host", "pw", "-o foo")
        with tempfile.TemporaryDirectory() as tmp:
            acp = Path(tmp) / "acp"
            for status, expected in ((0, "02:aa:bb:cc:dd:ee"), (1, None)):
                with self.subTest(mac_status=status):
                    # Execute the real remote shell with a stand-in ACP that accepts
                    # only read-only queries. Failed waMA output must not be trusted.
                    acp.write_text("#!/bin/sh\ncase \"$1:$2\" in\n"
                        "-q:syAP) echo 0x00000071;;\n"
                        "-q:syAM) echo TimeCapsule6,113;;\n"
                        f"-q:waMA) echo 02-AA-BB-CC-DD-EE; exit {status};;\n"
                        "*) exit 99;;\nesac\n")
                    acp.chmod(0o755)
                    def execute(_connection, command, **kwargs):
                        return subprocess.run(shlex.split(command), capture_output=True, text=True, **kwargs)
                    with mock.patch.object(probe, "DEVICE_ACP_PATH", str(acp)), mock.patch.object(
                        probe, "run_ssh", side_effect=execute
                    ) as ssh:
                        result = probe_remote_airport_identity_conn(connection)
                    self.assertEqual(result.model, "TimeCapsule6,113")
                    self.assertEqual(result.syap, "113")
                    self.assertEqual(result.airport_mac, expected)
                    self.assertEqual(ssh.call_count, 1)

    def test_runtime_naming_identity_derives_effective_names(self) -> None:
        result = derive_runtime_naming_identity("A.B.'s AirPort Time Capsule", "Time Capsule.local")

        self.assertEqual(result.system_name, "A.B.'s AirPort Time Capsule")
        self.assertEqual(result.hostname, "Time Capsule.local")
        self.assertEqual(result.mdns_instance_name, "A.B.'s AirPort Time Capsule")
        self.assertEqual(result.mdns_host_label, "a-b-s-airport-time-capsule")
        self.assertEqual(result.netbios_name, "TimeCapsule")

    def test_runtime_naming_identity_rejects_netbios_without_alnum(self) -> None:
        result = derive_runtime_naming_identity("极端 时间胶囊", "---.local")

        self.assertEqual(result.mdns_instance_name, "极端 时间胶囊")
        self.assertEqual(result.mdns_host_label, "timecapsule")
        self.assertEqual(result.netbios_name, "TimeCapsule")

    def test_probe_remote_runtime_naming_identity_reads_acp_and_hostname(self) -> None:
        proc = mock.Mock(stdout="system_name=Time Capsule\nhostname=time-capsule.local\n", returncode=0)
        connection = SshConnection("host", "pw", "-o foo")
        with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=proc) as run_ssh_mock:
            result = probe_remote_runtime_naming_identity_conn(connection)

        self.assertEqual(result.system_name, "Time Capsule")
        self.assertEqual(result.hostname, "time-capsule.local")
        self.assertEqual(result.mdns_instance_name, "Time Capsule")
        self.assertEqual(result.mdns_host_label, "time-capsule")
        self.assertEqual(result.netbios_name, "time-capsule")
        command = run_ssh_mock.call_args.args[1]
        self.assertIn("/usr/bin/acp -q syNm", command)
        self.assertIn("/usr/bin/acp -q syDN", command)
        self.assertIn("/bin/hostname", command)

    def test_probe_remote_runtime_naming_identity_fails_on_remote_error(self) -> None:
        proc = mock.Mock(stdout="", returncode=1)
        connection = SshConnection("host", "pw", "-o foo")
        with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=proc):
            with self.assertRaisesRegex(RuntimeError, "could not read runtime naming identity: rc=1"):
                probe_remote_runtime_naming_identity_conn(connection)

    def test_runtime_naming_identity_keeps_apples_label_whitespace(self) -> None:
        # Apple advertises syNm byte for byte as its default Bonjour name,
        # surrounding spaces and NBSPs included; only host names are normalized.
        expected_hosts = {" AirPort Time\u00a0Capsule ": "airport-time-capsule", "\u00a0Capsule\u00a0": "capsule",
                          "Capsule  Office": "capsule-office", "   ": "base-station-619b7d"}
        for name, host in expected_hosts.items():
            with self.subTest(name=name):
                result = derive_runtime_naming_identity(name, "airport-time-capsule", radio_mac="E8:8D:28:61:9B:7D")
                self.assertEqual(result.system_name, name)
                self.assertEqual(result.mdns_instance_name, name)
                self.assertEqual(result.mdns_host_label, host)
                self.assertEqual(result.netbios_name, "airport-time-ca")

    def test_apple_bonjour_host_label_matches_acpd(self) -> None:
        # Expected labels follow ACPd's conversion routine byte for byte.
        cases = {
            "AirPort Time Capsule": "AirPort-Time-Capsule",
            "James's AirPort Time Capsule": "Jamess-AirPort-Time-Capsule",
            "James\u2019s AirPort Time Capsule": "Jamess-AirPort-Time-Capsule",
            "A.B.'s AirPort Time Capsule": "A-B-s-AirPort-Time-Capsule",
            "Time.Capsule": "Time-Capsule",
            "Time\\Capsule": "Time-Capsule",
            " AirPort Time\u00a0Capsule ": "AirPort-Time-Capsule",
            "a--b": "a--b",
            "a  b": "a-b",
            "-a-": "a",
            "Caf\u00e9 Office": "Caf-Office",
            "x" * 70: "x" * 63,
            "x" * 62 + " y": "x" * 62,
            "   ": "",
            "!!!": "",
            "\u6781\u7aef \u65f6\u95f4\u80f6\u56ca": "",
            "": "",
        }
        for name, label in cases.items():
            with self.subTest(name=name):
                self.assertEqual(apple_bonjour_host_label(name), label)

    def test_runtime_naming_identity_host_label_falls_back_like_acpd(self) -> None:
        # With no letters or digits in syNm, Apple's Bonjour host is
        # Base-Station-<last three raMA bytes>, else waMA. /bin/hostname is
        # only a last resort: ACPd fills it from unchecked stack bytes.
        cases = [
            ({"radio_mac": "E8:8D:28:61:9B:7D", "airport_mac": "E8:8D:28:58:F1:5C"}, "base-station-619b7d"),
            ({"radio_mac": None, "airport_mac": "E8-8D-28-58-F1-5C"}, "base-station-58f15c"),
            ({"radio_mac": None, "airport_mac": None}, "base-station-edffbf"),
        ]
        for macs, host in cases:
            with self.subTest(**macs):
                result = derive_runtime_naming_identity("   ", "base-station-edffbf", **macs)
                self.assertEqual(result.mdns_host_label, host)
                self.assertEqual(result.mdns_instance_name, "   ")
        self.assertEqual(derive_runtime_naming_identity("!!!", None).mdns_host_label, "timecapsule")
        self.assertEqual(derive_runtime_naming_identity("Time.Capsule", "other", radio_mac="E8:8D:28:61:9B:7D").mdns_host_label,
                         "time-capsule")

    def test_runtime_naming_identity_host_label_prefers_syDN_like_acpd(self) -> None:
        # Rows observed on NetBSD 4 LE after acp syDN=... and a reboot: ACPd's
        # Bonjour host follows syDN's converted label, falls back to syNm when
        # that label is empty, and the instance name stays syNm throughout.
        cases = [
            ("Dn Test.Name\u2019s", "dn-test-names", "dn-test-names"),
            ("!!!", "base-station-edffbf", "airport-time-capsule"),
            ("", "airport-time-capsule", "airport-time-capsule"),
            (None, "airport-time-capsule", "airport-time-capsule"),
        ]
        for system_dns_name, hostname, host in cases:
            with self.subTest(system_dns_name=system_dns_name):
                result = derive_runtime_naming_identity("AirPort Time Capsule", hostname, system_dns_name=system_dns_name,
                                                        radio_mac="E8:8D:28:61:9B:7D")
                self.assertEqual(result.mdns_host_label, host)
                self.assertEqual(result.mdns_instance_name, "AirPort Time Capsule")
                self.assertEqual(result.system_dns_name, system_dns_name or None)
        # With syDN and syNm both unnamed, the Bonjour host is Base-Station-<raMA>.
        self.assertEqual(derive_runtime_naming_identity("   ", "base-station-edffbf", system_dns_name="!!!",
                                                        radio_mac="E8:8D:28:61:9B:7D").mdns_host_label,
                         "base-station-619b7d")

    def test_runtime_naming_identity_without_a_name_leaves_the_instance_unknown(self) -> None:
        # Without syNm mDNSResponder advertises its own "<model>-<MAC>" name:
        # the host label still falls back, the instance name is not guessed.
        for name in (None, ""):
            with self.subTest(name=name):
                result = derive_runtime_naming_identity(name, "base-station-edffbf")
                self.assertIsNone(result.system_name)
                self.assertIsNone(result.mdns_instance_name)
                self.assertEqual(result.mdns_host_label, "base-station-edffbf")
        named = derive_runtime_naming_identity("Office", "base-station-edffbf")
        self.assertEqual((named.mdns_instance_name, named.mdns_host_label), ("Office", "office"))

    def test_probe_remote_runtime_naming_identity_keeps_syNm_bytes(self) -> None:
        import shlex
        from timecapsulesmb.device import probe
        connection = SshConnection("host", "pw", "-o foo")
        with tempfile.TemporaryDirectory() as tmp:
            acp, value, radio, system_dns = Path(tmp) / "acp", Path(tmp) / "syNm", Path(tmp) / "raMA", Path(tmp) / "syDN"
            # Execute the real remote shell; command substitution drops only the
            # trailing newline, and the parser must not trim what is left. A
            # failed query prints its error on stderr and still exits zero, as
            # both NetBSD 4 and NetBSD 6 do for an unset syDN.
            acp.write_text("#!/bin/sh\nshow() {\n  case \"$(cat \"$1\")\" in\n"
                           "    \"### get \"*) cat \"$1\" >&2;;\n    *) cat \"$1\";;\n  esac\n}\n"
                           "case \"$1:$2\" in\n"
                           f"-q:syNm) show {shlex.quote(str(value))};;\n"
                           f"-q:syDN) show {shlex.quote(str(system_dns))};;\n"
                           f"-q:raMA) show {shlex.quote(str(radio))};;\n"
                           "-q:waMA) echo E8:8D:28:58:F1:5C;;\n*) exit 99;;\nesac\n")
            acp.chmod(0o755)
            def execute(_connection, command, **kwargs):
                return subprocess.run(shlex.split(command), capture_output=True, text=True, encoding="utf-8", **kwargs)
            unset = "### get 'syDN' failed: <<UNKNOWN FORMAT CONVERSION CODE %m>>"
            cases = [
                (" AirPort Time\u00a0Capsule ", unset, "E8:8D:28:61:9B:7D", "airport-time-capsule"),
                ("   ", unset, "E8:8D:28:61:9B:7D", "base-station-619b7d"),
                ("   ", unset, "### get 'raMA' failed: <<UNKNOWN FORMAT CONVERSION CODE %m>>", "base-station-58f15c"),
                ("Capsule\u2028Office ", unset, "E8:8D:28:61:9B:7D", "capsule-office"),
                ("AirPort Time Capsule", "Dn Test.Name\u2019s", "E8:8D:28:61:9B:7D", "dn-test-names"),
                ("AirPort Time Capsule", "!!!", "E8:8D:28:61:9B:7D", "airport-time-capsule"),
                ("AirPort Time Capsule", "", "E8:8D:28:61:9B:7D", "airport-time-capsule"),  # cleared
            ]
            for name, system_dns_name, radio_mac, host in cases:
                with self.subTest(name=name, system_dns_name=system_dns_name, radio_mac=radio_mac):
                    value.write_text(name + "\n", encoding="utf-8")
                    system_dns.write_text(system_dns_name + "\n", encoding="utf-8")
                    radio.write_text(radio_mac + "\n", encoding="utf-8")
                    with mock.patch.object(probe, "DEVICE_ACP_PATH", str(acp)), mock.patch.object(
                        probe, "run_ssh", side_effect=execute
                    ):
                        result = probe_remote_runtime_naming_identity_conn(connection)
                    self.assertEqual(result.system_name, name)
                    self.assertEqual(result.system_dns_name, None if system_dns_name == unset else system_dns_name or None)
                    self.assertEqual(result.mdns_instance_name, name)
                    self.assertEqual(result.mdns_host_label, host)

    def test_probe_remote_runtime_naming_identity_strips_only_line_endings(self) -> None:
        proc = mock.Mock(stdout="system_name= Capsule \r\nhostname= time-capsule.local \r\n", returncode=0)
        with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=proc):
            result = probe_remote_runtime_naming_identity_conn(SshConnection("host", "pw", "-o foo"))
        self.assertEqual(result.mdns_instance_name, " Capsule ")
        self.assertEqual(result.hostname, "time-capsule.local")
        self.assertEqual(result.mdns_host_label, "capsule")

    def test_deployment_uses_one_native_runtime_and_small_boot_scripts(self) -> None:
        plan = self._prepared_deploy_plan().plan
        transfers = [*plan.uploads, plan.config_upload, plan.boot_upload]
        services = [transfer for transfer in transfers if transfer.source_id == BINARY_SERVICE_SOURCE]
        self.assertEqual([transfer.destination for transfer in services], ["/mnt/Flash/service"])
        flash_names = {Path(transfer.destination).name for transfer in transfers
                       if transfer.destination.startswith("/mnt/Flash/")}
        self.assertEqual(flash_names, {"service", "boot.sh", "rc.local", "dfree.sh", "tcapsulesmb.conf"})


    def test_mounted_mast_volumes_mounts_each_volume_and_returns_successes(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")
        internal = self._mast_volume("dk2", name="Internal", builtin=True)
        external = self._mast_volume("dk5", disk_device="sd0", name="External", builtin=False)

        with mock.patch("timecapsulesmb.device.storage.ensure_volume_root_mounted_conn", side_effect=[True, False]) as mount_mock:
            mounted = mounted_mast_volumes_conn(connection, (internal, external), wait_seconds=17)

        self.assertEqual(mounted, (internal,))
        self.assertEqual(
            mount_mock.call_args_list,
            [
                mock.call(connection, internal.volume_root, internal.device_path, wait_seconds=17),
                mock.call(connection, external.volume_root, external.device_path, wait_seconds=17),
            ],
        )

    def test_mounted_mast_volumes_returns_empty_when_no_volume_mounts(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")
        internal = self._mast_volume("dk2", name="Internal", builtin=True)
        external = self._mast_volume("dk5", disk_device="sd0", name="External", builtin=False)

        with mock.patch("timecapsulesmb.device.storage.ensure_volume_root_mounted_conn", return_value=False):
            mounted = mounted_mast_volumes_conn(connection, (internal, external), wait_seconds=30)

        self.assertEqual(mounted, ())

    def test_probe_device_direct_target_fails_before_ssh_when_port_closed(self) -> None:
        with mock.patch("timecapsulesmb.device.probe.tcp_open", return_value=False) as tcp_open_mock:
            with mock.patch("timecapsulesmb.device.probe._probe_remote_os_info_conn", side_effect=AssertionError("should not ssh")):
                result = probe_device_conn(SshConnection("root@10.0.0.2", "pw", "-o HostKeyAlgorithms=+ssh-rsa"))
        tcp_open_mock.assert_called_once_with("10.0.0.2", 22)
        self.assertFalse(result.ssh_port_reachable)
        self.assertFalse(result.ssh_authenticated)
        self.assertEqual(result.error, "SSH is not reachable yet.")

    def test_upload_xattr_migrator_is_separate_from_runtime_payload(self) -> None:
        plan = build_deployment_plan(
            "host",
            self._payload_home(),
            Path("bin/smbd"),

            xattr_migrator_path=Path("bin/xattr-migrate/xattr-hfs-migrate"),
            rsync_path=Path("bin/rsync"),
            service_path=Path("bin/service"),

        )
        connection = SshConnection("host", "pw", "-o foo")

        self.assertNotIn(BINARY_XATTR_MIGRATOR_SOURCE, [item.source_id for item in plan.uploads])
        self.assertEqual(plan.migration_upload.source_id, BINARY_XATTR_MIGRATOR_SOURCE)
        with mock.patch("timecapsulesmb.deploy.executor.run_ssh"), mock.patch("timecapsulesmb.deploy.executor.upload_file") as upload_mock:
            with mock.patch(
                "timecapsulesmb.deploy.executor.ensure_volume_root_mounted_conn",
                return_value=True,
            ) as mount_mock:
                upload_deployment_payload(
                    replace(plan, uploads=[plan.migration_upload]),
                    connection=connection,
                    source_resolver={
                        BINARY_XATTR_MIGRATOR_SOURCE: Path("bin/xattr-migrate/xattr-hfs-migrate")
                    },
                )

        mount_mock.assert_not_called()  # The helper executes directly from RAM.
        upload_mock.assert_called_once_with(
            connection,
            Path("bin/xattr-migrate/xattr-hfs-migrate"),
            "/mnt/Memory/tc-xattr-hfs-migrate",
            timeout=180,
        )

    def test_xattr_migration_rejects_invalid_phase_and_metadata(self) -> None:
        plan = self._prepared_deploy_plan().plan
        connection = SshConnection("host", "pw", "-o foo")

        with self.assertRaisesRegex(ValueError, "migration phase"):
            migrate_xattr_tdb_to_hfs(
                connection, plan, phase="remove", inventory=None
            )

    def test_upload_and_verify_deployment_payload_records_upload_measurements(self) -> None:
        prepared_plan = self._prepared_deploy_plan()
        connection = SshConnection("host", "pw", "-o foo")
        # Sparse stand-ins at the shipped sizes, so the timeouts are real.
        binaries = tempfile.TemporaryDirectory()
        self.addCleanup(binaries.cleanup)
        smbd, rsync = Path(binaries.name) / "smbd", Path(binaries.name) / "rsync"
        for path, size in ((smbd, 10_296_644), (rsync, 1_037_780)):
            with path.open("wb") as file:
                file.truncate(size)
        prepared_plan = replace(prepared_plan, plan=replace(prepared_plan.plan, smbd_path=smbd, rsync_path=rsync))
        measurements: list[tuple[str, dict[str, object]]] = []

        def fake_upload(plan, *, connection, source_resolver, on_uploading=None, on_uploaded=None):
            for transfer in plan.uploads[:2]:
                if on_uploading is not None:
                    on_uploading(transfer)
                if on_uploaded is not None:
                    on_uploaded(transfer)

        upload_and_verify_deployment_payload(
            AppConfig.from_values({}),
            connection,
            prepared_plan,
            DeployRuntimeConfig(),
            callbacks=OperationCallbacks(record_execution_measurement=lambda kind, **fields: measurements.append((kind, fields))),
            run_remote_actions_func=mock.Mock(),
            upload_payload_func=fake_upload,
            migrate_xattrs_func=mock.Mock(return_value=XattrMigrationResult("migration=complete", ())),
            probe_flash_capacity_func=mock.Mock(return_value=(1_000_000, 100_000)),
            flush_remote_writes=mock.Mock(),
            verify_payload_home=mock.Mock(return_value=PayloadVerificationResult(True, "ok")),
        )

        upload_measurements = [fields for kind, fields in measurements if kind == "upload"]
        batch_measurements = [fields for kind, fields in measurements if kind == "upload_batch"]
        self.assertEqual(
            [fields["source_id"] for fields in upload_measurements],
            [BINARY_XATTR_MIGRATOR_SOURCE, BINARY_SMBD_SOURCE, BINARY_RSYNC_SOURCE, GENERATED_FLASH_CONFIG_SOURCE, PACKAGED_RC_LOCAL_SOURCE],
        )
        self.assertEqual([fields["destination_kind"] for fields in upload_measurements], ["memory", "payload", "payload", "flash", "flash"])
        self.assertTrue(all(fields["result"] == "success" for fields in upload_measurements))
        # Each measurement keeps the timeout upload_file applied, which grows
        # with the file: the 10 MB smbd past its planned 180 s, rsync not.
        by_source = {fields["source_id"]: fields for fields in upload_measurements}
        smbd = by_source[BINARY_SMBD_SOURCE]
        self.assertEqual(smbd["timeout_sec"], upload_timeout_seconds(smbd["bytes"], PAYLOAD_BINARY_UPLOAD_TIMEOUT_SECONDS))
        self.assertGreater(smbd["timeout_sec"], PAYLOAD_BINARY_UPLOAD_TIMEOUT_SECONDS)
        self.assertEqual(by_source[BINARY_RSYNC_SOURCE]["timeout_sec"], PAYLOAD_BINARY_UPLOAD_TIMEOUT_SECONDS)
        self.assertEqual(batch_measurements[0]["file_count"], len(prepared_plan.plan.uploads))
        self.assertEqual(batch_measurements[0]["result"], "success")

    def test_each_migration_phase_reports_progress_from_its_own_log(self) -> None:
        prepared_plan = self._prepared_deploy_plan()
        progress: list[tuple[str, dict[str, object]]] = []
        polled_logs: list[str] = []

        class FakePoller:
            def __init__(self, _connection, log, report):
                polled_logs.append(log)
                self.report = report

            def __enter__(self):
                # The migrator logged a position while the phase ran.
                self.report(MigrationProgress("copy", "u1", 4000))
                self.report(MigrationProgress("copy", "u1", None))
                return self

            def __exit__(self, *_exc):
                return None

        with mock.patch("timecapsulesmb.services.deploy.MigrationProgressPoller", FakePoller):
            upload_and_verify_deployment_payload(
                AppConfig.from_values({}),
                SshConnection("host", "pw", "-o foo"),
                prepared_plan,
                DeployRuntimeConfig(),
                callbacks=OperationCallbacks(report_progress=lambda stage, **fields: progress.append((stage, fields))),
                run_remote_actions_func=mock.Mock(),
                upload_payload_func=mock.Mock(),
                migrate_xattrs_func=mock.Mock(return_value=XattrMigrationResult("migration=complete", ())),
                probe_flash_capacity_func=mock.Mock(return_value=(1_000_000, 100_000)),
                flush_remote_writes=mock.Mock(),
                verify_payload_home=mock.Mock(return_value=PayloadVerificationResult(True, "ok")),
            )

        payload_dir = prepared_plan.plan.payload_dir
        self.assertEqual(polled_logs, [f"{payload_dir}/logs/xattr-migration-copy.log",
                                       f"{payload_dir}/logs/xattr-migration-cleanup.log"])
        fields = {"entries": 4000}
        # A log without a counter yet reports nothing.
        self.assertEqual(progress, [("migrate_xattrs_copy", fields), ("migrate_xattrs_cleanup", fields)])

    def test_new_metadata_preference_never_overrides_old_migration_inventory(self) -> None:
        for saved, override, expected in (("true", None, "netatalk"), ("false", None, "stream"),
                                          ("true", False, "stream"), ("false", True, "netatalk")):
            with self.subTest(saved=saved, override=override):
                migrate = mock.Mock(return_value=XattrMigrationResult("migration=complete", ()))
                upload_and_verify_deployment_payload(
                    AppConfig.from_values({"TC_FRUIT_METADATA_NETATALK": saved}),
                    SshConnection("host", "pw", ""),
                    self._prepared_deploy_plan(),
                    DeployRuntimeConfig(fruit_metadata_netatalk=override),
                    run_remote_actions_func=mock.Mock(),
                    upload_payload_func=mock.Mock(),
                    migrate_xattrs_func=migrate,
                    probe_flash_capacity_func=mock.Mock(return_value=(1_000_000, 100_000)),
                    flush_remote_writes=mock.Mock(),
                    verify_payload_home=mock.Mock(return_value=PayloadVerificationResult(True, "ok")),
                )
                self.assertEqual([c.kwargs["phase"] for c in migrate.call_args_list], ["copy", "cleanup"])
                self.assertIs(migrate.call_args_list[0].kwargs["inventory"], migrate.call_args_list[1].kwargs["inventory"])
                self.assertNotIn("legacy_metadata", migrate.call_args_list[0].kwargs)

    def _run_migration_reporting(self, summaries: dict[str, OversizedSummary | None], dropped: int = 0,
                                 skipped: dict[str, SkippedSummary | None] | None = None):
        messages: list[str] = []
        measurements: list[tuple[str, dict[str, object]]] = []
        root = self._mast_volume()

        def migrate(_connection, _plan, *, phase, inventory):
            # Only cleanup's retirement can drop verified rows.
            rows = dropped if phase == "cleanup" else 0
            return XattrMigrationResult(f"phase={phase}", (root,), (), summaries[phase], rows, bool(rows),
                                        (skipped or {}).get(phase))

        upload_and_verify_deployment_payload(
            AppConfig.from_values({}),
            SshConnection("host", "pw", ""),
            self._prepared_deploy_plan(),
            DeployRuntimeConfig(),
            callbacks=OperationCallbacks(
                log=messages.append,
                record_execution_measurement=lambda kind, **fields: measurements.append((kind, fields)),
            ),
            run_remote_actions_func=mock.Mock(),
            upload_payload_func=mock.Mock(),
            migrate_xattrs_func=migrate,
            probe_flash_capacity_func=mock.Mock(return_value=(1_000_000, 100_000)),
            flush_remote_writes=mock.Mock(),
            verify_payload_home=mock.Mock(return_value=PayloadVerificationResult(True, "ok")),
        )
        return messages, [fields for kind, fields in measurements if kind == "xattr_migration"]

    def test_each_migration_phase_records_its_output_and_unavailable_roots(self) -> None:
        debug: dict[str, object] = {}
        unavailable = {"copy": ("/Volumes/dk3",), "cleanup": ()}

        def migrate(_connection, _plan, *, phase, inventory):
            return XattrMigrationResult(f"\nmigration={phase}\n", (self._mast_volume(),), unavailable[phase])

        upload_and_verify_deployment_payload(
            AppConfig.from_values({}),
            SshConnection("host", "pw", ""),
            self._prepared_deploy_plan(),
            DeployRuntimeConfig(),
            callbacks=OperationCallbacks(add_debug_fields=lambda **fields: debug.update(fields)),
            run_remote_actions_func=mock.Mock(),
            upload_payload_func=mock.Mock(),
            migrate_xattrs_func=migrate,
            probe_flash_capacity_func=mock.Mock(return_value=(1_000_000, 100_000)),
            flush_remote_writes=mock.Mock(),
            verify_payload_home=mock.Mock(return_value=PayloadVerificationResult(True, "ok")),
        )

        self.assertEqual(debug["xattr_migration_copy"], "migration=copy")
        self.assertEqual(debug["xattr_migration_copy_unavailable_roots"], ["/Volumes/dk3"])
        self.assertEqual(debug["xattr_migration_cleanup"], "migration=cleanup")
        self.assertEqual(debug["xattr_migration_cleanup_unavailable_roots"], [])

    def _kept_lines(self, messages: list[str]) -> list[str]:
        """The kept-values report: its lines are consecutive log messages."""
        markers = ("larger than a native HFS attribute", "resource fork, which an HFS folder", "legacy storage and",
                   " of these values ")

        def in_report(message: str) -> bool:
            return message.startswith("  ") or any(marker in message for marker in markers)

        first = next(index for index, message in enumerate(messages) if in_report(message))
        last = first
        while last + 1 < len(messages) and in_report(messages[last + 1]):
            last += 1
        return messages[first:last + 1]

    def test_kept_oversized_values_are_shown_once_after_cleanup_and_recorded_without_paths(self) -> None:
        """Issue 345: values too large for a native HFS attribute are reported, not fatal."""
        values = [OversizedValue("tdb", f"/Volumes/dk2/Users/me/Library/Containers/app{i}",
                                 "com.apple.data-container-personality", 8090 + i) for i in range(11)]
        values.append(OversizedValue("appledouble", "/Volumes/dk2/Music/song", "user.private-name", 41409))
        copy_summary = OversizedSummary(tdb=131, appledouble=1, values=values)
        cleanup_summary = OversizedSummary(tdb=131, appledouble=1, values=values, database_outcome="quarantined")
        messages, migrations = self._run_migration_reporting({"copy": copy_summary, "cleanup": cleanup_summary})

        kept = [index for index, message in enumerate(messages) if "larger than a native HFS attribute" in message]
        self.assertEqual(len(kept), 1)  # copy and cleanup find the same values
        cleanup_start = next(index for index, message in enumerate(messages) if message.startswith("Verifying native HFS metadata"))
        self.assertGreater(kept[0], cleanup_start)  # the database outcome is known only after cleanup
        lines = self._kept_lines(messages)
        # One whole sentence per line, so the app can translate each by its key.
        self.assertEqual(lines[:4], [
            "132 Mac metadata values are larger than a native HFS attribute can hold (3,802 bytes).",
            "Apple's firmware cannot store them either, so they stay in legacy storage and are not visible "
            "over SMB; everything else was migrated.",
            "131 of these values are in the legacy Samba database, which was kept as xattr.tdb.orphaned.N "
            "instead of being deleted.",
            "1 of these values is in a ._ file, which was kept.",
        ])
        self.assertEqual(lines[4], "  /Volumes/dk2/Users/me/Library/Containers/app0 (com.apple.data-container-personality, 8090 bytes)")
        self.assertEqual(len(lines), 4 + 10 + 1)
        self.assertEqual(lines[-1], "  ...and 122 more")

        self.assertEqual([fields["phase"] for fields in migrations], ["copy", "cleanup"])
        for fields in migrations:
            self.assertEqual(fields["oversized_tdb"], 131)
            self.assertEqual(fields["oversized_appledouble"], 1)
            self.assertEqual(fields["oversized_max_size"], 41409)
            # Apple names verbatim, any other name generalized; never a path.
            self.assertEqual(fields["oversized_names"], ["com.apple.data-container-personality", "other"])
            self.assertFalse(any(value.path in repr(fields) or value.name == "user.private-name" and value.name in repr(fields)
                                 for value in values))
        self.assertNotIn("oversized_database", migrations[0])
        self.assertEqual(migrations[1]["oversized_database"], "quarantined")

    def test_kept_values_report_uses_the_sentences_and_keys_that_apply(self) -> None:
        value = OversizedValue("tdb", "/Volumes/dk2/folder", "com.apple.big", 3803)
        cases = (
            # A deferred retirement keeps the live database: never claim it was set aside.
            (OversizedSummary(tdb=1, values=[value], database_outcome="in_place"),
             ["migration.kept_too_large", "migration.kept_not_visible", "migration.kept_in_database_in_place",
              "migration.kept_value"],
             ["1 Mac metadata value is larger than a native HFS attribute can hold (3,802 bytes).",
              "Apple's firmware cannot store it either, so it stays in legacy storage and is not visible over SMB; "
              "everything else was migrated.",
              "1 of these values is in the legacy Samba database, which stays in place until migration finishes "
              "on every disk.",
              "  /Volumes/dk2/folder (com.apple.big, 3803 bytes)"]),
            (OversizedSummary(tdb=2, values=[value], database_outcome="quarantined"),
             ["migration.kept_too_large", "migration.kept_not_visible", "migration.kept_in_database_quarantined",
              "migration.kept_value", "migration.kept_more"],
             ["2 Mac metadata values are larger than a native HFS attribute can hold (3,802 bytes).",
              "Apple's firmware cannot store them either, so they stay in legacy storage and are not visible over "
              "SMB; everything else was migrated.",
              "2 of these values are in the legacy Samba database, which was kept as xattr.tdb.orphaned.N instead "
              "of being deleted.",
              "  /Volumes/dk2/folder (com.apple.big, 3803 bytes)",
              "  ...and 1 more"]),
            (OversizedSummary(appledouble=2, values=[replace(value, kind="appledouble")]),
             ["migration.kept_too_large", "migration.kept_not_visible", "migration.kept_in_appledouble",
              "migration.kept_value", "migration.kept_more"],
             ["2 Mac metadata values are larger than a native HFS attribute can hold (3,802 bytes).",
              "Apple's firmware cannot store them either, so they stay in legacy storage and are not visible over "
              "SMB; everything else was migrated.",
              "2 of these values are in ._ files, which were kept.",
              "  /Volumes/dk2/folder (com.apple.big, 3803 bytes)",
              "  ...and 1 more"]),
        )
        for summary, keys, texts in cases:
            with self.subTest(keys=keys):
                lines = _oversized_summaries(summary)
                self.assertEqual([line.key for line in lines], keys)
                self.assertEqual([line.text for line in lines], texts)
                messages, _ = self._run_migration_reporting({"copy": summary, "cleanup": summary})
                self.assertEqual(self._kept_lines(messages), texts)

    def test_kept_values_report_explains_folder_resource_forks(self) -> None:
        # An HFS folder holds no resource fork at any size; "larger than a
        # native attribute" would misstate why a 64-byte fork was kept.
        folder = OversizedValue("appledouble", "/Volumes/dk2/pass.txt.rtfd", "com.apple.ResourceFork", 64, "folder_fork")
        tiny = replace(folder, path="/Volumes/dk2/b.rtfd", size=1)
        big = OversizedValue("tdb", "/Volumes/dk2/folder", "com.apple.big", 3803)
        cases = (
            (OversizedSummary(appledouble=1, values=[tiny], folder_forks=1),
             ["1 folder has a resource fork, which an HFS folder cannot hold.",
              "Apple's firmware cannot store it either, so it stays in legacy storage and is not visible over SMB; "
              "everything else was migrated.",
              "1 of these values is in a ._ file, which was kept.",
              "  /Volumes/dk2/b.rtfd (folder resource fork, 1 byte)"]),
            (OversizedSummary(tdb=1, appledouble=2, values=[big, folder, tiny], folder_forks=2,
                              database_outcome="quarantined"),
             ["1 Mac metadata value is larger than a native HFS attribute can hold (3,802 bytes).",
              "2 folders have a resource fork, which an HFS folder cannot hold.",
              "Apple's firmware cannot store them either, so they stay in legacy storage and are not visible over "
              "SMB; everything else was migrated.",
              "1 of these values is in the legacy Samba database, which was kept as xattr.tdb.orphaned.N instead "
              "of being deleted.",
              "2 of these values are in ._ files, which were kept.",
              "  /Volumes/dk2/folder (com.apple.big, 3803 bytes)",
              "  /Volumes/dk2/pass.txt.rtfd (folder resource fork, 64 bytes)",
              "  /Volumes/dk2/b.rtfd (folder resource fork, 1 byte)"]),
        )
        for summary, expected in cases:
            with self.subTest(expected=expected[0]):
                lines = _oversized_summaries(summary)
                self.assertEqual([line.text for line in lines], expected)
                folder_lines = [line for line in lines if line.key == "migration.kept_folder_fork"]
                self.assertTrue(folder_lines)
                self.assertTrue(all(len(line.args) == 2 for line in folder_lines))  # path and size, no attribute name
                messages, migrations = self._run_migration_reporting({"copy": summary, "cleanup": summary})
                self.assertEqual(self._kept_lines(messages), expected)
                self.assertEqual([fields["oversized_folder_forks"] for fields in migrations], [summary.folder_forks] * 2)

    def test_skipped_entries_are_shown_once_after_cleanup_and_recorded_without_paths(self) -> None:
        # The native report lists 10 paths per reason.
        long_names = [SkippedEntry("name_too_long", f"/Volumes/dk2/Music/Song {i}#7A45{i:X}.tmp") for i in range(10)]
        damaged = SkippedEntry("malformed", "/Volumes/dk2/Photos/._private.jpg")
        summary = SkippedSummary({"name_too_long": 12, "not_found": 0, "malformed": 1, "unsupported": 0,
                                  "not_a_file": 0}, [*long_names, damaged])
        messages, migrations = self._run_migration_reporting({"copy": None, "cleanup": None},
                                                             skipped={"copy": summary, "cleanup": summary})

        # Copy and cleanup walk the same tree: the user hears it once, after cleanup.
        reported = [index for index, message in enumerate(messages) if "names too long for Samba" in message]
        self.assertEqual(len(reported), 1)
        cleanup_start = next(index for index, message in enumerate(messages)
                             if message.startswith("Verifying native HFS metadata"))
        self.assertGreater(reported[0], cleanup_start)
        lines = messages[reported[0]:reported[0] + 14]
        self.assertEqual(lines[0], "12 files have names too long for Samba on this device to open, so they are not "
                                   "visible over SMB. To reach them, connect the disk to a Mac.")
        self.assertEqual(lines[1:11], [f"  {entry.path}" for entry in long_names])
        self.assertEqual(lines[11], "  ...and 2 more")
        self.assertEqual(lines[12:], ["1 ._ file is damaged and was kept unchanged.", f"  {damaged.path}"])

        # Telemetry gets each reason's count in both phases, never a path.
        self.assertEqual([fields["phase"] for fields in migrations], ["copy", "cleanup"])
        for fields in migrations:
            self.assertEqual({key: value for key, value in fields.items() if key.startswith("skipped_")}, {
                "skipped_name_too_long": 12, "skipped_not_found": 0, "skipped_malformed": 1,
                "skipped_unsupported": 0, "skipped_not_a_file": 0})
            self.assertFalse(any(entry.path in repr(fields) for entry in summary.entries))

    def test_paths_are_shown_inside_their_shares(self) -> None:
        internal = self._mast_volume("dk2", name="AirPort Disk")
        external = self._mast_volume("dk4", disk_device="sd0", name="4TB", builtin=False)
        volumes = (internal, external)
        cases = (
            # The internal disk's share is its ShareRoot folder by default.
            ("/Volumes/dk2/ShareRoot/Photos/._x.jpg", False, "AirPort Disk/Photos/._x.jpg"),
            ("/Volumes/dk2/ShareRoot/Photos/._x.jpg", True, "AirPort Disk/ShareRoot/Photos/._x.jpg"),
            ("/Volumes/dk4/Music/song", False, "4TB/Music/song"),
            ("/Volumes/dk4/Music/song", True, "4TB/Music/song"),
            # Outside every share, or on a disk that only shares a prefix: as is.
            ("/Volumes/dk2/Backups.backupdb/x", False, "/Volumes/dk2/Backups.backupdb/x"),
            ("/Volumes/dk2/Backups.backupdb/x", True, "AirPort Disk/Backups.backupdb/x"),
            ("/Volumes/dk40/x", False, "/Volumes/dk40/x"),
            ("/Volumes/dk2/ShareRoot", False, "/Volumes/dk2/ShareRoot"),
        )
        for path, internal_root, expected in cases:
            with self.subTest(path=path, internal_root=internal_root):
                self.assertEqual(_share_path(path, volumes, internal_root), expected)

        # Deploy shows the kept values and skipped entries that way, and
        # sends the same paths as the message arguments the app translates.
        inventory = fake_inventory()
        inventory.volumes = volumes
        skipped = SkippedSummary({**dict.fromkeys(SKIP_REASONS, 0), "malformed": 1},
                                 [SkippedEntry("malformed", "/Volumes/dk2/ShareRoot/Photos/._x.jpg")])
        kept = OversizedSummary(appledouble=1, values=[
            OversizedValue("appledouble", "/Volumes/dk4/Music/song", "com.apple.big", 5000)])
        with mock.patch("timecapsulesmb.services.deploy.inventory_metadata", side_effect=lambda *_a: inventory):
            messages, _ = self._run_migration_reporting({"copy": kept, "cleanup": kept},
                                                        skipped={"copy": skipped, "cleanup": skipped})
        self.assertIn("  AirPort Disk/Photos/._x.jpg", messages)
        self.assertIn("  4TB/Music/song (com.apple.big, 5000 bytes)", messages)
        self.assertFalse(any("/Volumes/" in message for message in messages if message.startswith("  ")))

    def test_skipped_report_says_one_sentence_per_reason_in_order(self) -> None:
        entry = {reason: SkippedEntry(reason, f"/Volumes/dk2/{reason}")
                 for reason in ("name_too_long", "not_found", "malformed", "unsupported", "not_a_file")}
        one = SkippedSummary({reason: 1 for reason in entry}, list(entry.values())[::-1])
        lines = _skipped_summaries(one)
        # A file that vanished mid-walk is routine while afpserver and Time
        # Machine keep working, and leaves nothing to do: it is never shown.
        self.assertEqual([line.key for line in lines], [
            "migration.skipped_name_too_long", "migration.skipped_path",
            "migration.skipped_malformed", "migration.skipped_path",
            "migration.skipped_unsupported", "migration.skipped_path",
            "migration.skipped_not_a_file", "migration.skipped_path"])
        self.assertEqual([line.text for line in lines], [
            "1 file has a name too long for Samba on this device to open, so it is not visible over SMB. To reach "
            "it, connect the disk to a Mac.",
            "  /Volumes/dk2/name_too_long",
            "1 ._ file is damaged and was kept unchanged.",
            "  /Volumes/dk2/malformed",
            "1 ._ file holds entries TimeCapsuleSMB does not convert. Its supported metadata was migrated, and the "
            "file was kept.",
            "  /Volumes/dk2/unsupported",
            "1 ._ entry is not a regular file and was kept.",
            "  /Volumes/dk2/not_a_file"])
        self.assertEqual(lines[1].args, ("/Volumes/dk2/name_too_long",))

        # A reason whose entries the capped list left out still gets its
        # sentence, and the paths it lacks are counted, not invented.
        two = SkippedSummary({"name_too_long": 0, "not_found": 2, "malformed": 0, "unsupported": 2,
                              "not_a_file": 2}, [entry["unsupported"]])
        lines = _skipped_summaries(two)
        self.assertEqual([line.key for line in lines][-1], "migration.skipped_more")
        self.assertEqual([line.text for line in lines], [
            "2 ._ files hold entries TimeCapsuleSMB does not convert. Their supported metadata was migrated, and "
            "the files were kept.",
            "  /Volumes/dk2/unsupported",
            "  ...and 1 more",
            "2 ._ entries are not regular files and were kept.",
            "  ...and 2 more"])

    def test_no_skipped_message_when_nothing_was_skipped_or_files_only_vanished(self) -> None:
        vanished = SkippedSummary({**dict.fromkeys(SKIP_REASONS, 0), "not_found": 3},
                                  [SkippedEntry("not_found", "/Volumes/dk2/Backups.backupdb/band.tmp")])
        for skipped in ({"copy": SkippedSummary(), "cleanup": SkippedSummary()}, {"copy": None, "cleanup": None},
                        {"copy": vanished, "cleanup": vanished}):
            with self.subTest(skipped=skipped):
                messages, migrations = self._run_migration_reporting({"copy": None, "cleanup": None},
                                                                     skipped=skipped)
                self.assertFalse(any("skipped" in message or "._ " in message for message in messages))
                self.assertFalse(any("band.tmp" in message or "disappeared" in message for message in messages))
                if skipped["copy"] is None:
                    self.assertNotIn("skipped_not_found", migrations[0])
                else:
                    # Telemetry still counts them.
                    self.assertEqual(migrations[1]["skipped_not_found"], skipped["copy"].counts["not_found"])

    def test_skips_of_volumes_finished_before_a_cleanup_failure_are_still_shown(self) -> None:
        """A volume whose cleanup finished is saved as complete and never
        walked again, so a failure on a later volume must not swallow its
        skipped entries."""
        finished = SkippedSummary({**dict.fromkeys(SKIP_REASONS, 0), "malformed": 1},
                                  [SkippedEntry("malformed", "/Volumes/dk2/Photos/._x.jpg")])
        for phase in ("copy", "cleanup"):
            with self.subTest(phase=phase):
                messages, measurements = [], []

                def migrate(_connection, _plan, *, phase: str, inventory, failing=phase):
                    inventory.skipped[phase] = finished
                    if phase == failing:
                        raise RuntimeError("opendir failed path=/Volumes/dk3/x")
                    return XattrMigrationResult(f"phase={phase}", (self._mast_volume(),), (), None, 0, False,
                                                finished)

                with self.assertRaises(DeployDeviceError):
                    upload_and_verify_deployment_payload(
                        AppConfig.from_values({}), SshConnection("host", "pw", ""),
                        self._prepared_deploy_plan(), DeployRuntimeConfig(),
                        callbacks=OperationCallbacks(
                            log=messages.append,
                            record_execution_measurement=lambda kind, **fields: measurements.append((kind, fields))),
                        run_remote_actions_func=mock.Mock(), upload_payload_func=mock.Mock(),
                        migrate_xattrs_func=migrate,
                        probe_flash_capacity_func=mock.Mock(return_value=(1_000_000, 100_000)),
                        flush_remote_writes=mock.Mock(),
                        verify_payload_home=mock.Mock(return_value=PayloadVerificationResult(True, "ok")),
                    )
                failure = [fields for kind, fields in measurements
                           if kind == "xattr_migration" and fields["result"] == "failure"][0]
                self.assertEqual(failure["skipped_malformed"], 1)
                # A failed copy leaves every volume to be walked again: nothing
                # is final yet, so nothing is shown.
                shown = "1 ._ file is damaged and was kept unchanged." in messages
                self.assertEqual(shown, phase == "cleanup")
                self.assertEqual("  /Volumes/dk2/Photos/._x.jpg" in messages, phase == "cleanup")

    def test_dropped_verified_rows_are_recorded_for_cleanup_without_the_copy_path(self) -> None:
        for dropped in (0, 3):
            with self.subTest(dropped=dropped):
                _messages, migrations = self._run_migration_reporting({"copy": None, "cleanup": None}, dropped)
                copy_fields, cleanup_fields = migrations
                self.assertNotIn("dropped_rows", copy_fields)
                self.assertNotIn("backup", copy_fields)
                self.assertEqual((cleanup_fields["dropped_rows"], cleanup_fields["backup"]), (dropped, dropped > 0))
                self.assertFalse(any("orphaned" in str(value) for value in cleanup_fields.values()))

    def test_no_oversized_message_when_nothing_was_kept(self) -> None:
        for summaries in ({"copy": OversizedSummary(), "cleanup": OversizedSummary()},
                          {"copy": None, "cleanup": None}):
            with self.subTest(summaries=summaries):
                messages, migrations = self._run_migration_reporting(summaries)
                self.assertFalse(any("native HFS attribute" in message for message in messages))
                if summaries["copy"] is None:
                    self.assertNotIn("oversized_tdb", migrations[0])
                else:
                    self.assertEqual((migrations[0]["oversized_tdb"], migrations[0]["oversized_names"],
                                      migrations[0]["oversized_max_size"]), (0, [], 0))

    def test_migration_failure_diagnostics_distinguish_stall_timeout_and_scan_error(self) -> None:
        from timecapsulesmb.transport.errors import SshCommandTimeout

        cases = (
            (RuntimeError("opendir failed path=/Volumes/dk2/problem; errors=1"), False, False, "xattr_migration_failed"),
            (SshCommandTimeout("migration emergency deadline exceeded"), False, True, "xattr_migration_timeout"),
            (MigrationStalledError("no progress"), True, False, "xattr_migration_stalled"),
        )
        for error, stalled, timed_out, code in cases:
            with self.subTest(stalled=stalled, timed_out=timed_out):
                measurements = []
                remote_actions = mock.Mock()
                with self.assertRaises(DeployDeviceError) as caught:
                    upload_and_verify_deployment_payload(
                        AppConfig.from_values({}), SshConnection("host", "pw", ""),
                        self._prepared_deploy_plan(), DeployRuntimeConfig(),
                        callbacks=OperationCallbacks(record_execution_measurement=lambda kind, **fields: measurements.append((kind, fields))),
                        run_remote_actions_func=remote_actions, upload_payload_func=mock.Mock(),
                        migrate_xattrs_func=mock.Mock(side_effect=error),
                        probe_flash_capacity_func=mock.Mock(return_value=(1_000_000, 100_000)),
                    )
                message = str(caught.exception)
                self.assertIn("phase=copy elapsed_seconds=", message)
                self.assertIn("stall_seconds=300", message)
                self.assertNotIn("emergency_timeout_seconds", message)
                self.assertIn(f"stalled={str(stalled).lower()}", message)
                self.assertIn(f"timed_out={str(timed_out).lower()}", message)
                self.assertIn("xattr-migration-copy.log", message)
                self.assertIn(str(error), message)
                migration = next(fields for kind, fields in measurements if kind == "xattr_migration")
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(migration["stalled"], stalled)
                self.assertEqual(migration["timed_out"], timed_out)
                self.assertEqual(migration["result"], "failure")
                self.assertFalse(any(
                    RemovePathsAction((RAM_HELPER,)) in call.args[1]
                    for call in remote_actions.call_args_list
                ))

    def test_migrator_ended_by_a_signal_is_reported_as_stopped_or_crashed(self) -> None:
        cases = (
            (137, 9, "xattr_migration_stopped", "The device stopped the Samba metadata migration (signal 9)."),
            (143, 15, "xattr_migration_stopped", "The device stopped the Samba metadata migration (signal 15)."),
            (139, 11, "xattr_migration_crashed", "The Samba metadata migration crashed on the device (signal 11)."),
            (138, 10, "xattr_migration_crashed", "The Samba metadata migration crashed on the device (signal 10)."),
            (134, 6, "xattr_migration_crashed", "The Samba metadata migration crashed on the device (signal 6)."),
            (4, None, "xattr_migration_failed", "Native HFS metadata migration (copy) failed."),
            # Only ssh reports 255, after its login: the connection dropped.
            (255, None, "xattr_migration_connection_lost",
             "The connection to the device was lost during the Samba metadata migration. The migration may still "
             "be running on the device; the next deploy waits for a running migration to finish first."),
        )
        for status, signal_number, code, first_line in cases:
            with self.subTest(status=status):
                measurements = []
                error = MigrationExitError(f"Native metadata migration failed with exit status {status}.", status)
                with self.assertRaises(DeployDeviceError) as caught:
                    upload_and_verify_deployment_payload(
                        AppConfig.from_values({}), SshConnection("host", "pw", ""),
                        self._prepared_deploy_plan(), DeployRuntimeConfig(),
                        callbacks=OperationCallbacks(
                            record_execution_measurement=lambda kind, **fields: measurements.append((kind, fields))),
                        run_remote_actions_func=mock.Mock(), upload_payload_func=mock.Mock(),
                        migrate_xattrs_func=mock.Mock(side_effect=error),
                        probe_flash_capacity_func=mock.Mock(return_value=(1_000_000, 100_000)),
                    )
                self.assertEqual(caught.exception.code, code)
                message = str(caught.exception)
                self.assertEqual(message.splitlines()[0].split(" Verified")[0], first_line)
                # Only a dropped connection leaves a migration that may still run.
                self.assertEqual("may still be running" in message, status == 255)
                # The cause of a stop is unknown; the message must not guess one.
                self.assertNotIn("memory", message)
                self.assertIn(f"exit status {status}", message)
                migration = next(fields for kind, fields in measurements if kind == "xattr_migration")
                self.assertEqual((migration["exit_status"], migration["exit_signal"]), (status, signal_number))

        # Failures that are not an exit status record none.
        measurements = []
        with self.assertRaises(DeployDeviceError):
            upload_and_verify_deployment_payload(
                AppConfig.from_values({}), SshConnection("host", "pw", ""),
                self._prepared_deploy_plan(), DeployRuntimeConfig(),
                callbacks=OperationCallbacks(
                    record_execution_measurement=lambda kind, **fields: measurements.append((kind, fields))),
                run_remote_actions_func=mock.Mock(), upload_payload_func=mock.Mock(),
                migrate_xattrs_func=mock.Mock(side_effect=MigrationStalledError("no progress")),
                probe_flash_capacity_func=mock.Mock(return_value=(1_000_000, 100_000)),
            )
        migration = next(fields for kind, fields in measurements if kind == "xattr_migration")
        self.assertEqual((migration["exit_status"], migration["exit_signal"]), (None, None))

    def _fail_migration(self, error: Exception, *, phase: str = "copy", source_dir: str = ".samba4", aliases=(),
                        unavailable=(), folder=None):
        """Run a deploy whose migration phase fails with error, the walk's log
        naming the folder that failed it. Returns the error, the summaries
        shown, the unreadable_folder mock and the payload folder."""
        volume = self._mast_volume()
        path = f"/Volumes/dk2/{source_dir}/private/xattr.tdb"
        inventory = MigrationInventory((volume,), [{"path": path}], [], list(unavailable), "")
        inventory.sources = [{"uuid": volume.adisk_uuid, "path": path, "aliases": list(aliases)}]
        summaries: list[Summary] = []
        prepared = self._prepared_deploy_plan()

        def migrate(_connection, _plan, *, phase: str, inventory, failing=phase):
            if phase == failing:
                raise error
            return XattrMigrationResult(f"phase={phase}", (volume,), (), None, 0, False, None)

        with mock.patch("timecapsulesmb.services.deploy.inventory_metadata", return_value=inventory), \
                mock.patch("timecapsulesmb.services.deploy.unreadable_folder", return_value=folder) as unreadable:
            with self.assertRaises(DeployDeviceError) as caught:
                upload_and_verify_deployment_payload(
                    AppConfig.from_values({}), SshConnection("host", "pw", ""), prepared, DeployRuntimeConfig(),
                    callbacks=OperationCallbacks(log_summary=summaries.append),
                    run_remote_actions_func=mock.Mock(), upload_payload_func=mock.Mock(),
                    migrate_xattrs_func=migrate,
                    probe_flash_capacity_func=mock.Mock(return_value=(1_000_000, 100_000)),
                    flush_remote_writes=mock.Mock(),
                    verify_payload_home=mock.Mock(return_value=PayloadVerificationResult(True, "ok")),
                )
        return caught.exception, summaries, unreadable, prepared.plan.payload_dir

    def _assert_names_folder(self, summaries: list[Summary]) -> None:
        # The app's error line is fixed text, so its log names the folder.
        self.assertEqual([(s.key, s.args, s.text) for s in summaries if s.key == "migration.unreadable_folder"],
                         [("migration.unreadable_folder", ("Data/Mac.incomplete/bands",),
                           "The device could not read this folder: Data/Mac.incomplete/bands")])

    def test_a_folder_the_device_cannot_list_points_to_disk_repair_then_uninstall(self) -> None:
        bands = "/Volumes/dk2/ShareRoot/Mac.incomplete/bands"
        error = MigrationExitError("Native metadata migration failed with exit status 4.", 4)
        for phase in ("copy", "cleanup"):
            with self.subTest(phase=phase):
                caught, summaries, unreadable, payload_dir = self._fail_migration(error, phase=phase, folder=bands)
                self.assertEqual(caught.code, "xattr_migration_unreadable_folder")
                message = str(caught)
                # The CLI names the folder as the Mac sees it, inside its share.
                self.assertTrue(message.startswith(
                    'The device could not read the folder "Data/Mac.incomplete/bands", '
                    "so the Samba metadata migration failed."))
                self.assertIn("exit status 4", caught.detail)
                # The code's next steps: Disk Repair, then Uninstall, which keeps
                # the files and drops only metadata not migrated yet.
                steps = cli_steps("deploy", caught.code)
                self.assertEqual(steps[0], "Run `tcapsule fsck` on the disk, then run `tcapsule deploy` again.")
                self.assertIn("run `tcapsule uninstall`, then `tcapsule deploy`", steps[1])
                self.assertIn("metadata from an earlier version that is not migrated yet", steps[1])
                self.assertIn("your files stay on the disk", steps[1])
                self.assertNotIn("report it", " ".join(steps))
                self._assert_names_folder(summaries)
                # The log read is the failed phase's own.
                unreadable.assert_called_once_with(mock.ANY, f"{payload_dir}/logs/xattr-migration-{phase}.log")

    def test_an_unreadable_folder_points_only_to_disk_repair_when_uninstall_would_keep_a_database(self) -> None:
        bands = "/Volumes/dk2/ShareRoot/Mac.incomplete/bands"
        error = MigrationExitError("Native metadata migration failed with exit status 4.", 4)
        cases = (
            # Uninstall deletes only .samba4, so the next deploy would find this database.
            {"source_dir": "tc-netbsd4le"},
            {"aliases": ("/Volumes/dk2/samba4/private/xattr.tdb",)},
            # A disk that did not mount was never searched for another database.
            {"unavailable": ("/Volumes/dk3",)},
        )
        for case in cases:
            with self.subTest(**case):
                caught, summaries, _unreadable, _dir = self._fail_migration(error, folder=bands, **case)
                self.assertEqual(caught.code, "xattr_migration_unreadable_folder_repair_only")
                message = str(caught)
                self.assertTrue(message.startswith('The device could not read the folder "Data/Mac.incomplete/bands", '))
                self.assertIn("exit status 4", caught.detail)
                steps = cli_steps("deploy", caught.code)
                self.assertEqual(steps[0], "Run `tcapsule fsck` on the disk, then run `tcapsule deploy` again.")
                # Uninstall would leave the database, and the next deploy would fail the same way.
                self.assertNotIn("uninstall", " ".join(steps))
                # The next step past a folder Disk Repair cannot fix is a report.
                self.assertIn("the disk may be failing. Report it, with this command's output, at "
                              "https://github.com/jamesyc/TimeCapsuleSMB/issues", steps[1])
                self.assertIn("Reports there are public, so remove any names", steps[1])
                self._assert_names_folder(summaries)

    def test_a_walk_that_failed_on_no_folder_keeps_the_generic_failure(self) -> None:
        error = MigrationExitError("Native metadata migration failed with exit status 4.", 4)
        caught, summaries, unreadable, payload_dir = self._fail_migration(error, folder=None)
        unreadable.assert_called_once_with(mock.ANY, f"{payload_dir}/logs/xattr-migration-copy.log")
        self.assertEqual(caught.code, "xattr_migration_failed")
        self.assertTrue(str(caught).startswith("Native HFS metadata migration (copy) failed."))
        self.assertFalse(any(s.key == "migration.unreadable_folder" for s in summaries))

    def test_a_migrator_that_did_not_report_a_walk_failure_never_reads_the_log_for_folders(self) -> None:
        from timecapsulesmb.transport.errors import SshCommandTimeout

        bands = "/Volumes/dk2/ShareRoot/Mac.incomplete/bands"
        cases = (
            (MigrationStalledError("no progress"), "xattr_migration_stalled"),
            (SshCommandTimeout("migration emergency deadline exceeded"), "xattr_migration_timeout"),
            (MigrationExitError("Native metadata migration failed with exit status 255.", 255),
             "xattr_migration_connection_lost"),
            (MigrationExitError("Native metadata migration failed with exit status 139.", 139),
             "xattr_migration_crashed"),
            # The migrator exited but its response was not a failed walk's.
            (RuntimeError("Invalid migration response"), "xattr_migration_failed"),
        )
        for error, code in cases:
            with self.subTest(code=code, error=type(error).__name__):
                caught, _summaries, unreadable, _dir = self._fail_migration(error, folder=bands)
                self.assertEqual(caught.code, code)
                unreadable.assert_not_called()

    def test_source_inspection_stall_uses_migration_classification(self) -> None:
        remote_actions = mock.Mock()
        migrate = mock.Mock()
        with mock.patch(
            "timecapsulesmb.services.deploy.inspect_sources",
            side_effect=MigrationStalledError("inspection made no progress"),
        ):
            with self.assertRaises(DeployDeviceError) as caught:
                upload_and_verify_deployment_payload(
                    AppConfig.from_values({}),
                    SshConnection("host", "pw", ""),
                    self._prepared_deploy_plan(),
                    DeployRuntimeConfig(),
                    run_remote_actions_func=remote_actions,
                    upload_payload_func=mock.Mock(),
                    migrate_xattrs_func=migrate,
                    probe_flash_capacity_func=mock.Mock(return_value=(1_000_000, 100_000)),
                )
        self.assertEqual(caught.exception.code, "xattr_migration_stalled")
        self.assertIn("phase=inspect", str(caught.exception))
        migrate.assert_not_called()
        self.assertFalse(any(
            RemovePathsAction((RAM_HELPER,)) in call.args[1]
            for call in remote_actions.call_args_list
        ))

    def test_xattr_copy_precedes_payload_and_cleanup_follows_verification(self) -> None:
        prepared_plan = self._prepared_deploy_plan()
        connection = SshConnection("host", "pw", "-o foo")
        events: list[str] = []
        migrated_root = self._mast_volume()
        remote_actions = mock.Mock()

        def migrate(_connection, _plan, *, phase, inventory):
            events.append(f"migrate:{phase}")
            return XattrMigrationResult(f"phase={phase}", (migrated_root,))

        def verify(*_args, **_kwargs):
            events.append("verify")
            return PayloadVerificationResult(True, "ok")

        def upload(plan, *_args, **_kwargs):
            events.append(
                "upload:migrator"
                if plan.uploads == [plan.migration_upload]
                else "upload:boot" if plan.uploads == [plan.boot_upload] else "upload:config" if plan.uploads == [plan.config_upload] else "upload:payload"
            )

        upload_and_verify_deployment_payload(
            AppConfig.from_values({}),
            connection,
            prepared_plan,
            DeployRuntimeConfig(fruit_metadata_netatalk=False),
            callbacks=OperationCallbacks(),
            run_remote_actions_func=remote_actions,
            migrate_xattrs_func=migrate,
            probe_flash_capacity_func=mock.Mock(return_value=(1_000_000, 100_000)),
            upload_payload_func=upload,
            flush_remote_writes=mock.Mock(),
            verify_payload_home=verify,
        )

        self.assertEqual(
            events,
            [
                "upload:migrator",
                "migrate:copy",
                "upload:payload",
                "verify",
                "verify",
                "migrate:cleanup",
                "upload:config",
                "upload:boot",
            ],
        )
        self.assertTrue(any(
            call.args[1] == [RemovePathsAction((RAM_HELPER,))]
            for call in remote_actions.call_args_list
        ))

    def test_xattr_migration_keeps_afp_offline_through_cleanup(self) -> None:
        prepared_plan = self._prepared_deploy_plan()
        connection = SshConnection("host", "pw", "-o foo")
        afp_running = True
        migration_states: list[tuple[str, bool]] = []

        def run_actions(_connection, actions, **_kwargs):
            nonlocal afp_running
            if StopProcessAction("afpserver") in actions:
                afp_running = False

        def migrate(_connection, _plan, *, phase, inventory):
            migration_states.append((phase, afp_running))
            return XattrMigrationResult("migration=complete", ())

        upload_and_verify_deployment_payload(
            AppConfig.from_values({}),
            connection,
            prepared_plan,
            DeployRuntimeConfig(),
            run_remote_actions_func=run_actions,
            upload_payload_func=mock.Mock(),
            migrate_xattrs_func=migrate,
            probe_flash_capacity_func=mock.Mock(return_value=(1_000_000, 100_000)),
            flush_remote_writes=mock.Mock(),
            verify_payload_home=mock.Mock(return_value=PayloadVerificationResult(True, "ok")),
        )

        self.assertEqual(migration_states, [("copy", False), ("cleanup", False)])

    def test_afp_stop_failure_aborts_before_migration(self) -> None:
        prepared_plan = self._prepared_deploy_plan()
        upload = mock.Mock()
        migrate = mock.Mock()

        def fail_afp_stop(_connection, actions, **_kwargs):
            if StopProcessAction("afpserver") in actions:
                raise SshError("process afpserver did not stop")

        with self.assertRaisesRegex(SshError, "afpserver did not stop"):
            upload_and_verify_deployment_payload(
                AppConfig.from_values({}),
                SshConnection("host", "pw", "-o foo"),
                prepared_plan,
                DeployRuntimeConfig(),
                run_remote_actions_func=fail_afp_stop,
                upload_payload_func=upload,
                migrate_xattrs_func=migrate,
                probe_flash_capacity_func=mock.Mock(return_value=(1_000_000, 100_000)),
            )

        upload.assert_not_called()
        migrate.assert_not_called()

    def _deploy_failing(self, *, master_alive: bool, error: BaseException | None = None, step: str | None = None,
                        stage: str | None = None, migrate=None, wait_for_previous=None):
        """Run a deploy whose remote actions, upload or flush (step) raise error
        in the given stage, with the shared SSH connection up or gone at the
        failure. Returns the exception raised, the master check and the stage
        the deploy was in when it failed."""
        connection = SshConnection("host", "pw", "-o foo")
        alive = mock.Mock(return_value=master_alive)
        stages: list[str] = []

        def fails(name):
            def call(*_args, **_kwargs):
                if step == name and stages[-1] == stage:
                    raise error
            return call

        with ExitStack() as patches, self.assertRaises(Exception) as caught:
            patches.enter_context(mock.patch("timecapsulesmb.services.deploy.ssh_master_alive", alive))
            patches.enter_context(
                mock.patch("timecapsulesmb.services.deploy.flush_remote_filesystem_writes", fails("flush")))
            if wait_for_previous is not None:
                patches.enter_context(
                    mock.patch("timecapsulesmb.services.deploy.wait_for_previous_migration", wait_for_previous))
            upload_and_verify_deployment_payload(
                AppConfig.from_values({}), connection, self._prepared_deploy_plan(), DeployRuntimeConfig(),
                callbacks=OperationCallbacks(set_stage=stages.append),
                run_remote_actions_func=fails("actions"),
                upload_payload_func=fails("upload"),
                migrate_xattrs_func=migrate or mock.Mock(return_value=XattrMigrationResult("migration=complete", ())),
                probe_flash_capacity_func=mock.Mock(return_value=(1_000_000, 100_000)),
                verify_payload_home=mock.Mock(return_value=PayloadVerificationResult(True, "ok")),
            )
        return caught.exception, alive, stages[-1] if stages else None

    def test_a_connection_lost_mid_install_says_to_run_the_install_again(self) -> None:
        dropped = SshError("ssh command failed with rc=255")
        lost_migration = MigrationExitError("exit status 255", 255)
        # Every step from stopping the old runtime to flushing the boot hook,
        # in the stage the deploy must be in when it fails.
        points = (
            {"step": "actions", "stage": "pre_upload_actions"},
            {"step": "flush", "stage": "pre_upload_actions"},
            {"step": "upload", "stage": "upload_xattr_migrator"},
            {"migrate": mock.Mock(side_effect=lost_migration), "stage": "migrate_xattrs_copy"},
            {"step": "actions", "stage": "replace_software"},
            {"step": "upload", "stage": "upload_payload"},
            {"step": "flush", "stage": "flush_payload_upload"},
            {"step": "upload", "stage": "enable_boot"},
            {"step": "flush", "stage": "flush_boot_hook"},
        )
        for point in points:
            migration = "migrate" in point
            for master_alive in (False, True):
                with self.subTest(step=point.get("step", "migrate"), stage=point["stage"], master_alive=master_alive):
                    error, alive, failed_in = self._deploy_failing(
                        master_alive=master_alive, error=dropped, **point)
                    self.assertEqual(failed_in, point["stage"])
                    alive.assert_called_once()
                    if master_alive:
                        # Our own command killing its shell also makes ssh exit
                        # 255, but the shared connection survives: the failure
                        # keeps its own message.
                        if migration:
                            self.assertEqual(error.code, "xattr_migration_connection_lost")
                        else:
                            self.assertIs(error, dropped)
                        continue
                    self.assertIsInstance(error, DeployDeviceError)
                    self.assertEqual(error.code, "install_interrupted")
                    self.assertEqual(str(error), INSTALL_INTERRUPTED_MESSAGE)
                    # The cause stays attached for diagnostics.
                    if migration:
                        self.assertEqual(error.__cause__.code, "xattr_migration_connection_lost")
                    else:
                        self.assertIs(error.__cause__, dropped)

    def test_a_timeout_mid_install_keeps_its_own_message_with_the_connection_gone(self) -> None:
        # A failing disk shows up as a timeout; it is not an interrupted install.
        timeout = SshCommandTimeout("Timed out waiting for ssh")
        error, alive, failed_in = self._deploy_failing(
            master_alive=False, error=timeout, step="flush", stage="flush_payload_upload")
        self.assertIs(error, timeout)
        self.assertEqual(failed_in, "flush_payload_upload")
        alive.assert_not_called()

    def test_a_connection_lost_before_the_install_starts_keeps_its_own_message(self) -> None:
        # Nothing on the device has changed before the old runtime stops.
        dropped = SshError("ssh command failed with rc=255")
        error, alive, failed_in = self._deploy_failing(
            master_alive=False, wait_for_previous=mock.Mock(side_effect=dropped))
        self.assertIs(error, dropped)
        self.assertIsNone(failed_in)
        alive.assert_not_called()

    def test_upload_and_verify_waits_for_a_previous_migration_before_inventory(self) -> None:
        prepared_plan = self._prepared_deploy_plan()
        events: list[str] = []
        stages: list[str] = []
        busy = MigrationActivity(
            (RunningMigration(412, "copy", "/Volumes/dk2/.samba4/logs/xattr-migration-copy.log", "0:01.00"),),
            10, "Oct 5 00:20:15 2026", MigrationProgress("copy"))
        answers = iter([busy, MigrationActivity(())])

        def probe(_connection):
            events.append("probe")
            return next(answers)

        def inventory(*_args):
            events.append("inventory")
            return fake_inventory()

        def first_device_change(*_args, **_kwargs):
            events.append("actions")
            raise SshError("stop here")

        with mock.patch("timecapsulesmb.services.deploy.probe_migration_activity", side_effect=probe), \
                mock.patch("timecapsulesmb.services.deploy.inventory_metadata", side_effect=inventory), \
                mock.patch("time.sleep"), self.assertRaises(SshError):
            upload_and_verify_deployment_payload(
                AppConfig.from_values({}),
                SshConnection("host", "pw", "-o foo"),
                prepared_plan,
                DeployRuntimeConfig(),
                callbacks=OperationCallbacks(set_stage=stages.append),
                run_remote_actions_func=first_device_change,
                upload_payload_func=mock.Mock(),
                migrate_xattrs_func=mock.Mock(return_value=XattrMigrationResult("migration=complete", ())),
                probe_flash_capacity_func=mock.Mock(return_value=(1_000_000, 100_000)),
            )

        # The inventory and every device change come after the earlier
        # migration finished, so they see the volumes it completed.
        self.assertEqual(events, ["probe", "probe", "inventory", "actions"])
        self.assertEqual(stages[:2], ["wait_for_previous_migration", "inventory_legacy_metadata"])

    def test_upload_and_verify_deployment_payload_codes_manager_stop_timeout(self) -> None:
        prepared_plan = self._prepared_deploy_plan()
        connection = SshConnection("host", "pw", "-o foo")

        with self.assertRaises(DeployDeviceError) as raised:
            upload_and_verify_deployment_payload(
                AppConfig.from_values({}),
                connection,
                prepared_plan,
                DeployRuntimeConfig(),
                callbacks=OperationCallbacks(),
                run_remote_actions_func=mock.Mock(side_effect=SshError("process manager did not stop")),
                upload_payload_func=mock.Mock(),
                migrate_xattrs_func=mock.Mock(return_value=XattrMigrationResult("migration=complete", ())),
                probe_flash_capacity_func=mock.Mock(return_value=(1_000_000, 100_000)),
            )

        self.assertEqual(raised.exception.code, "manager_stop_timeout")
        self.assertIn("A service on the device is stuck", str(raised.exception))
        self.assertIsInstance(raised.exception.__cause__, SshError)

    def test_upload_and_verify_deployment_payload_codes_payload_upload_timeout(self) -> None:
        # An upload's own timeout says how much reached the device, and the
        # upload measurement keeps it; any other SSH timeout has no such field.
        message = "Timed out copying smbd to remote path /Volumes/dk2/.samba4/smbd over SSH"
        cases = (
            (SshUploadTimeout(message, bytes_on_device=4_194_304), {"bytes_on_device": 4_194_304}),
            (SshUploadTimeout(message, bytes_on_device=None), {"bytes_on_device": None}),
            (SshCommandTimeout(message), {}),
        )
        for error, expected_fields in cases:
            with self.subTest(error=type(error).__name__, fields=expected_fields):
                prepared_plan = self._prepared_deploy_plan()
                measurements: list[tuple[str, dict[str, object]]] = []

                def timeout_upload(plan, *, connection, source_resolver, on_uploading=None, on_uploaded=None):
                    if plan.uploads == [plan.migration_upload]:
                        return
                    if on_uploading is not None:
                        on_uploading(plan.uploads[0])
                    raise error

                with self.assertRaises(DeployDeviceError) as raised:
                    upload_and_verify_deployment_payload(
                        AppConfig.from_values({}),
                        SshConnection("host", "pw", "-o foo"),
                        prepared_plan,
                        DeployRuntimeConfig(),
                        callbacks=OperationCallbacks(
                            record_execution_measurement=lambda kind, **fields: measurements.append((kind, fields))
                        ),
                        run_remote_actions_func=mock.Mock(),
                        upload_payload_func=timeout_upload,
                        migrate_xattrs_func=mock.Mock(return_value=XattrMigrationResult("migration=complete", ())),
                        probe_flash_capacity_func=mock.Mock(return_value=(1_000_000, 100_000)),
                    )

                self.assertEqual(raised.exception.code, "payload_upload_timeout")
                self.assertIn("Copying the SMB payload timed out.", str(raised.exception))
                # The code's next steps say what to do about a slow disk or link.
                steps = cli_steps("deploy", raised.exception.code)
                self.assertIn("Connect this computer or the device by Ethernet, then run `tcapsule deploy` again.", steps)
                self.assertIn("If this keeps happening, the disk may need replacing.", steps)
                self.assertIs(raised.exception.__cause__, error)
                [upload] = [fields for kind, fields in measurements if kind == "upload" and fields["result"] == "failure"]
                self.assertEqual(upload["error_type"], type(error).__name__)
                self.assertEqual({key: upload[key] for key in upload if key == "bytes_on_device"}, expected_fields)

    def test_upload_deployment_payload_stops_when_payload_volume_guard_fails(self) -> None:
        paths = self._payload_home("/Volumes/dk2", "samba4")
        plan = build_deployment_plan("host", paths, Path("bin/smbd"),  xattr_migrator_path=Path("bin/xattr-hfs-migrate"), rsync_path=Path("bin/rsync"), service_path=Path("bin/service"))
        connection = SshConnection("host", "pw", "-o foo")
        source_resolver = {
            BINARY_SMBD_SOURCE: Path("/tmp/smbd"),
        }
        with mock.patch("timecapsulesmb.deploy.executor.ensure_volume_root_mounted_conn", return_value=False) as mount_mock:
            with mock.patch("timecapsulesmb.deploy.executor.upload_file") as upload_mock:
                with self.assertRaisesRegex(RuntimeError, "payload volume /Volumes/dk2 is not mounted before upload"):
                    upload_deployment_payload(plan, connection=connection, source_resolver=source_resolver)

        mount_mock.assert_called_once_with(connection, "/Volumes/dk2", "/dev/dk2", wait_seconds=DEFAULT_APPLE_MOUNT_WAIT_SECONDS)
        upload_mock.assert_not_called()

    def test_upload_deployment_payload_fails_for_missing_planned_source(self) -> None:
        paths = self._payload_home("/Volumes/dk2", "samba4")
        plan = build_deployment_plan("host", paths, Path("bin/smbd"),  xattr_migrator_path=Path("bin/xattr-hfs-migrate"), rsync_path=Path("bin/rsync"), service_path=Path("bin/service"))
        connection = SshConnection("host", "pw", "-o foo")
        with self.assertRaisesRegex(KeyError, "No local source for planned transfer 'binary:smbd'"):
            upload_deployment_payload(plan, connection=connection, source_resolver={})

    def test_render_managed_runtime_verification_passes_when_runtime_probe_succeeds(self) -> None:
        verification = ManagedRuntimeProbeResult(
            ready=True,
            detail="managed runtime is ready",
            smbd=readiness_result(True, "managed smbd ready", ("PASS:managed smbd ready",)),
            mdns=readiness_result(True, "managed mDNS registrant active", ("PASS:managed mDNS registrant active",)),
        )

        self.assertTrue(verification.ready)
        self.assertEqual(
            render_managed_runtime_verification(verification, heading="NetBSD4 activation verification:"),
            [
                "NetBSD4 activation verification:",
                "  ok: managed smbd ready",
                "  ok: managed mDNS registrant active",
            ],
        )

    def test_render_managed_runtime_verification_notes_advisory_findings(self) -> None:
        detail = "discovery native NBNS is not ready; NetBIOS name lookups may fail, SMB and Bonjour are unaffected"
        verification = ManagedRuntimeProbeResult(
            ready=True,
            detail="managed runtime is ready",
            smbd=readiness_result(True, "managed smbd ready", ("PASS:managed smbd ready",)),
            mdns=ReadinessProbeResult(ready=True, detail="managed mDNS registrant active", steps=(
                ProbeStepResult("mdns_process", "pass", "discovery process is running"),
                ProbeStepResult("native_nbns", "info", detail),
            )),
        )

        self.assertEqual(
            render_managed_runtime_verification(verification, heading="Runtime verification:"),
            [
                "Runtime verification:",
                "  ok: managed smbd ready",
                "  ok: discovery process is running",
                f"  note: {detail}",
            ],
        )

    def test_runtime_verification_measurement_keeps_advisory_native_nbns_result(self) -> None:
        detail = "discovery native NBNS is not ready; NetBIOS name lookups may fail, SMB and Bonjour are unaffected"
        smbd = readiness_result(True, "managed smbd ready", ("PASS:managed smbd ready",))
        bonjour = "Apple mDNSResponder accepted all 2 Bonjour registrations"
        cases = (
            ((ProbeStepResult("native_nbns", "info", detail),), {"native_nbns_status": "info", "native_nbns_detail": detail, "info_step_count": 1}),
            ((ProbeStepResult("native_nbns", "skip", "native NBNS is still starting; deploy does not wait for it"),), {"native_nbns_status": "skip", "info_step_count": 0}),
            ((), {"native_nbns_status": "none", "native_mdns_status": "none", "info_step_count": 0}),
            # Bonjour registration rides along the same way.
            ((ProbeStepResult("mdns_registered", "pass", bonjour),),
             {"native_nbns_status": "none", "native_mdns_status": "pass", "native_mdns_detail": bonjour}),
        )
        for nbns_steps, expected in cases:
            with self.subTest(expected["native_nbns_status"]):
                verification = ManagedRuntimeProbeResult(
                    ready=True,
                    detail="managed runtime is ready",
                    smbd=smbd,
                    mdns=ReadinessProbeResult(ready=True, detail="managed mDNS registrant active", steps=(
                        ProbeStepResult("mdns_process", "pass", "discovery process is running"),
                    ) + nbns_steps),
                )
                measurements: list[tuple[str, dict[str, object]]] = []
                result = verify_managed_runtime_ready(
                    SshConnection("host", "pw", ""),
                    callbacks=OperationCallbacks(record_execution_measurement=lambda kind, **fields: measurements.append((kind, fields))),
                    stage="verify_runtime_activation",
                    timeout_seconds=200,
                    heading="Runtime verification:",
                    failure_message="not ready",
                    probe_runtime=mock.Mock(return_value=verification),
                )
                self.assertIs(result, verification)
                fields = next(fields for kind, fields in measurements if kind == "runtime_verification")
                self.assertTrue(fields["ready"])
                self.assertNotIn("final_blocker_step", fields)
                self.assertEqual({key: fields.get(key) for key in expected}, expected)
                if not nbns_steps:
                    self.assertNotIn("native_nbns_detail", fields)

    def test_runtime_verification_measurement_tells_rsync_alone_from_a_runtime_failure(self) -> None:
        cases = (
            (self._runtime_not_ready(samba_ready=True), "rsync_not_ready", True),
            (self._runtime_not_ready(), "not_ready", False),
        )
        for verification, result, samba_ready in cases:
            with self.subTest(result):
                measurements: list[tuple[str, dict[str, object]]] = []
                with mock.patch("timecapsulesmb.services.runtime_verification.recheck_cached_adisk"):
                    with self.assertRaises(DeviceError):
                        verify_managed_runtime_ready(
                            SshConnection("host", "pw", ""),
                            callbacks=OperationCallbacks(
                                record_execution_measurement=lambda kind, **fields: measurements.append((kind, fields))),
                            stage="verify_runtime_activation", timeout_seconds=200, heading="Runtime verification:",
                            failure_message="not ready", probe_runtime=mock.Mock(return_value=verification),
                            read_runtime_logs=mock.Mock(return_value={}),
                        )
                fields = next(fields for kind, fields in measurements if kind == "runtime_verification")
                self.assertEqual((fields["result"], fields["samba_ready"]), (result, samba_ready))

    def test_runtime_verification_measurement_reports_what_a_timed_out_listing_waited_for(self) -> None:
        # A timed-out process listing carries the table read just after it and
        # the processes asleep in the kernel; other failures carry neither.
        table = "  812 700 812 D 127 vmmapbsy 0:00.00 ps\n"
        timed_out = ProbeStepResult(
            "mdns_process_table_probe", "timeout", "mDNS process table probe timed out after 30s",
            stdout=table, asleep_in_kernel="ps (pid 812) on vmmapbsy",
        )
        failed = ProbeStepResult("mdns_link_plan_probe", "fail", "mdns link plan probe failed with exit code 1",
                                 stdout="plan: garbage\n")
        for step, expected in (
            (timed_out, {"final_blocker_process_table": table.rstrip(),
                         "final_blocker_asleep_in_kernel": "ps (pid 812) on vmmapbsy"}),
            (failed, {"final_blocker_process_table": None, "final_blocker_asleep_in_kernel": None}),
        ):
            with self.subTest(step=step.id):
                verification = ManagedRuntimeProbeResult(
                    ready=False,
                    detail=step.detail,
                    smbd=readiness_result(True, "managed smbd ready", ("PASS:managed smbd ready",)),
                    mdns=ReadinessProbeResult(ready=False, detail=step.detail, steps=(step,)),
                )
                measurements: list[tuple[str, dict[str, object]]] = []
                with self.assertRaises(DeviceError):
                    verify_managed_runtime_ready(
                        SshConnection("host", "pw", ""),
                        callbacks=OperationCallbacks(
                            record_execution_measurement=lambda kind, **fields: measurements.append((kind, fields))
                        ),
                        stage="verify_runtime_activation",
                        timeout_seconds=200,
                        heading="Runtime verification:",
                        failure_message="not ready",
                        probe_runtime=mock.Mock(return_value=verification),
                        read_runtime_logs=mock.Mock(return_value={}),
                    )
                fields = next(fields for kind, fields in measurements if kind == "runtime_verification")
                self.assertEqual(fields["final_blocker_step"], step.id)
                self.assertEqual(fields["final_blocker_detail"], step.detail)
                self.assertEqual({key: fields.get(key) for key in expected}, expected)

    def test_render_managed_runtime_verification_fails_when_runtime_probe_fails(self) -> None:
        verification = ManagedRuntimeProbeResult(
            ready=False,
            detail="managed runtime is not ready",
            smbd=readiness_result(False, "managed smbd is not ready", ("FAIL:managed smbd is not ready",)),
            mdns=readiness_result(True, "managed mDNS registrant active", ("PASS:managed mDNS registrant active",)),
        )

        self.assertFalse(verification.ready)
        self.assertEqual(
            render_managed_runtime_verification(verification, heading="NetBSD4 activation verification:"),
            [
                "NetBSD4 activation verification:",
                "  failed: managed smbd is not ready",
                "  ok: managed mDNS registrant active",
            ],
        )

    def test_verify_post_uninstall_returns_structured_result_and_rendered_lines(self) -> None:
        plan = mock.Mock(verify_absent_targets=("/Volumes/dk2/samba4", "/mnt/Flash/rc.local"))
        probe_result = mock.Mock(returncode=1, stdout="ABSENT:/Volumes/dk2/samba4\nPRESENT:/mnt/Flash/rc.local\n")

        with mock.patch("timecapsulesmb.deploy.verify.probe_paths_absent_conn", return_value=probe_result):
            verification = verify_post_uninstall(SshConnection("host", "pw", "-o foo"), plan)

        self.assertIsInstance(verification, VerificationResult)
        self.assertFalse(verification)
        self.assertEqual(
            render_post_uninstall_verification(verification),
            [
                "Post-uninstall verification:",
                "  ok: removed /Volumes/dk2/samba4",
                "  failed: still present /mnt/Flash/rc.local",
            ],
        )

    def test_probe_managed_smbd_single_shot_checks_runtime_conf_parent_and_port_binding(self) -> None:
        with mock.patch(
            "timecapsulesmb.device.probe.run_ssh",
            return_value=mock.Mock(returncode=0, stdout=""),
        ) as run_ssh_mock:
            self.assertTrue(probe_managed_smbd_conn(SshConnection("host", "pw", "-o foo"), timeout_seconds=45).ready)
        remote_command = run_ssh_mock.call_args.args[1]
        self.assertIn("capture_ps_out()", remote_command)
        self.assertIn("smbd_parent_process_present()", remote_command)
        self.assertIn('capture_fstat_for_ucomm "$ps_out" smbd', remote_command)
        self.assertIn('/usr/bin/fstat -p "$1"', remote_command)
        self.assertIn("smbd_bound_445()", remote_command)
        self.assertNotIn('out="$(fstat 2>&1)"', remote_command)
        self.assertNotIn("smbd_ready_marker_matches_parent()", remote_command)
        self.assertNotIn("/mnt/Memory/samba4/var/smbd.ready", remote_command)
        self.assertNotIn("capture_ps_lstart_out()", remote_command)
        self.assertNotIn("normalize_lstart_fields()", remote_command)
        self.assertNotIn("smbd_log_has_fresh_daemon_ready()", remote_command)
        self.assertNotIn("max_attempts", remote_command)
        self.assertNotIn("sleep 5", remote_command)
        self.assertNotIn("nbns", remote_command)

    def test_probe_status_helpers_ignore_zombie_processes(self) -> None:
        helpers = SMBD_STATUS_HELPERS.replace(
            '/usr/bin/fstat -p "$1" 2>/dev/null || true',
            'echo "fstat:$1"',
        )
        script = (
            helpers
            + r'''
zombie_smbd="100 1 100 Z 0 - 0:00.00 smbd"
live_smbd="101 1 101 S 0 - 0:00.00 smbd"
zombie_discovery="200 1 200 Z 0 - 0:00.00 service service: role=discovery nbns=ready"
live_discovery="201 1 201 S 0 - 0:00.00 service service: role=discovery nbns=ready"
mixed_smbd=$(cat <<'EOF'
100 1 100 Z 0 - 0:00.00 smbd
101 1 101 S 0 - 0:00.00 smbd
EOF
)

smbd_parent_process_present "$zombie_smbd"; echo "zombie-smbd=$?"
smbd_parent_process_present "$live_smbd"; echo "live-smbd=$?"
native_service_role_present "$zombie_discovery" discovery; echo "zombie-discovery=$?"
native_service_role_present "$live_discovery" discovery; echo "live-discovery=$?"
capture_fstat_for_ucomm "$mixed_smbd" smbd
'''
        )

        result = subprocess.run(["/bin/sh", "-c", script], check=False, text=True, capture_output=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("zombie-smbd=1", result.stdout)
        self.assertIn("live-smbd=0", result.stdout)
        self.assertIn("zombie-discovery=1", result.stdout)
        self.assertIn("live-discovery=0", result.stdout)
        self.assertNotIn("fstat:100", result.stdout)
        self.assertIn("fstat:101", result.stdout)

    def test_probe_status_helpers_do_not_count_probe_shell_body_as_manager(self) -> None:
        # capture_ps_out never reads a shell's command line; these rows give
        # them one anyway, holding the manager's title, to show that only a
        # service process's title counts.
        script = (
            SMBD_STATUS_HELPERS
            + r'''
real_manager="203 1 203 S 0 - 0:00.00 service service: role=manager"
waiting_manager="205 1 205 S 0 - 0:00.00 service service: role=manager waiting=hostname"
started_manager="206 1 206 S 0 - 0:00.00 service service: role=manager started=12 waiting=hostname"
retired_shell_manager="204 1 204 S 0 - 0:00.00 sh /bin/sh /mnt/Flash/manager.sh"
self_match_manager=$(cat <<'EOF'
3308 11745 3308 S 0 - 0:00.01 sh /bin/sh -c probe='service: role=manager'
11745 11677 11745 Ss 0 - 0:00.01 sh sh -c /bin/sh -c 'probe=service: role=manager'
EOF
)
manager_process_present_for_volume "$real_manager"; echo "manager=$?"
manager_process_present_for_volume "$waiting_manager"; echo "waiting=$?"
manager_process_present_for_volume "$started_manager"; echo "started=$?"
manager_process_present_for_volume "$retired_shell_manager"; echo "retired=$?"
manager_process_present_for_volume "$self_match_manager"; echo "self=$?"
'''
        )

        result = subprocess.run(["/bin/sh", "-c", script], check=False, text=True, capture_output=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("manager=0", result.stdout)
        self.assertIn("waiting=0", result.stdout)
        self.assertIn("started=0", result.stdout)
        self.assertIn("retired=1", result.stdout)
        self.assertIn("self=1", result.stdout)

    def test_smbd_status_helpers_pass_only_with_live_ram_auth_mount_and_manager(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            ram_root = tmp / "mnt" / "Memory" / "samba4"
            persistent_prefix = tmp / "Volumes"
            volume_root = persistent_prefix / "dk2"
            external_volume_root = persistent_prefix / "dk3"
            data_root = volume_root / "ShareRoot"
            external_data_root = external_volume_root
            payload_private = volume_root / ".samba4" / "private"
            for path in (ram_root / "sbin", ram_root / "private", ram_root / "etc", ram_root / "var", data_root, external_data_root, payload_private):
                path.mkdir(parents=True, exist_ok=True)
            (ram_root / "sbin" / "smbd").write_text("#!/bin/sh\necho 'Version 4.24.3'\n")
            (ram_root / "sbin" / "smbd").chmod(0o755)
            (ram_root / "private" / "smbpasswd").write_text("smbpasswd")
            (ram_root / "private" / "username.map").write_text("username map")
            smb_conf = ram_root / "etc" / "smb.conf"
            smb_conf.write_text(
                f"""[global]
    passdb backend = smbpasswd:{ram_root}/private/smbpasswd
    username map = {ram_root}/private/username.map
    xattr_tdb:file = {payload_private}/xattr.tdb
[Data]
    path = {data_root}
[USB]
    path = {external_data_root}
""",
                encoding="utf-8",
            )
            ps_out = (
                "101 1 101 S 0 - 0:00.00 smbd\n"
                "202 1 202 S 0 - 0:00.00 service service: role=manager\n"
            )
            script = f"""
RUNTIME_RAM_ROOT={shlex.quote(str(ram_root))}
RUNTIME_SMB_CONF_PATH={shlex.quote(str(smb_conf))}
RUNTIME_PERSISTENT_ROOT_PREFIX={shlex.quote(str(persistent_prefix) + "/")}
{SMBD_STATUS_HELPERS}
capture_df_for_volume_root() {{ echo "/dev/dk2 100 10 90 10% $1"; }}
ps_out={shlex.quote(ps_out)}
fstat_out='root smbd 101 10 internet stream tcp 0x0 *:445
root smbd 101 11 internet6 stream tcp 0x0 *:445'
describe_managed_smbd_status "$ps_out" "$fstat_out"
printf 'status=%s\\n' "$?"
"""

            result = subprocess.run(["/bin/sh", "-c", script], check=False, text=True, capture_output=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PASS:managed runtime smbd binary present", result.stdout)
        self.assertIn("PASS:active smb.conf passdb backend uses RAM smbpasswd", result.stdout)
        self.assertIn("PASS:active smb.conf username map uses RAM username.map", result.stdout)
        self.assertIn("PASS:active smb.conf xattr_tdb:file is persistent", result.stdout)
        self.assertIn("PASS:all managed share volumes are mounted", result.stdout)
        self.assertIn("PASS:manager is running for managed runtime", result.stdout)
        self.assertIn("PASS:smbd owns IPv4 and IPv6 wildcard TCP 445 listeners", result.stdout)
        self.assertIn("PASS:device Samba version: 4.24.3", result.stdout)
        self.assertIn("status=0", result.stdout)

    def test_smbd_status_helper_requires_both_wildcard_tcp_445_families(self) -> None:
        script = (
            SMBD_STATUS_HELPERS
            + r'''
ipv4='root smbd 101 10 internet stream tcp 0x0 *:445'
ipv6='root smbd 101 10 internet6 stream tcp 0x0 [*]:445'
both=$(cat <<'EOF'
root smbd 101 10 internet6 stream tcp 0x0 [*]:445
root smbd 101 11 internet stream tcp 0x0 *:445
EOF
)
smbd_bound_445 "$ipv4"; echo "ipv4_only=$?"
smbd_bound_445 "$ipv6"; echo "ipv6_only=$?"
smbd_bound_445 "$both"; echo "both_wildcard=$?"
smbd_bound_445 'root smbd 101 10 internet stream tcp 0x0 192.168.1.40:445
root smbd 101 11 internet6 stream tcp 0x0 [fd00::40]:445'; echo "specific_only=$?"
'''
        )

        result = subprocess.run(["/bin/sh", "-c", script], check=False, text=True, capture_output=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ipv4_only=1", result.stdout)
        self.assertIn("ipv6_only=1", result.stdout)
        self.assertIn("both_wildcard=0", result.stdout)
        self.assertIn("specific_only=1", result.stdout)

    def test_smbd_status_helpers_fail_for_disk_auth_unmounted_volume_and_missing_manager(self) -> None:
        result = self._run_smbd_status_helpers(
            "101 1 101 S 0 - 0:00.00 smbd"
        )
        self.assertIn("FAIL:active smb.conf passdb backend is not staged in RAM", result.stdout)
        self.assertIn("FAIL:active smb.conf username map is not staged in RAM", result.stdout)
        self.assertIn("FAIL:active smb.conf xattr_tdb:file is not persistent disk storage", result.stdout)
        self.assertIn("FAIL:one or more managed share volumes are not mounted", result.stdout)
        self.assertIn("FAIL:manager is not running for managed runtime", result.stdout)
        self.assertIn("status=1", result.stdout)

    def test_smbd_status_helpers_report_a_manager_they_could_not_identify(self) -> None:
        # A service process whose command line was not read may be the manager.
        rows = [
            "101 1 101 S 0 - 0:00.00 smbd",
            f"102 1 102 D 0 - 0:00.00 service {UNREADABLE_COMMAND}",
            "103 1 103 S 0 - 0:00.00 service service: role=discovery nbns=ready",
        ]
        result = self._run_smbd_status_helpers("\n".join(rows))
        self.assertIn(f"FAIL:{MANAGER_UNREADABLE}", result.stdout)
        self.assertNotIn("FAIL:manager is not running for managed runtime", result.stdout)

        rows.append("104 1 104 S 0 - 0:00.00 service service: role=manager started=12")
        result = self._run_smbd_status_helpers("\n".join(rows))
        self.assertIn("PASS:manager is running for managed runtime", result.stdout)

    def _run_smbd_status_helpers(self, ps_out: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            ram_root = tmp / "mnt" / "Memory" / "samba4"
            persistent_prefix = tmp / "Volumes"
            volume_root = persistent_prefix / "dk2"
            data_root = volume_root / "ShareRoot"
            payload_private = volume_root / ".samba4" / "private"
            for path in (ram_root / "sbin", ram_root / "private", ram_root / "etc", data_root, payload_private):
                path.mkdir(parents=True, exist_ok=True)
            (ram_root / "sbin" / "smbd").write_text("smbd")
            (ram_root / "sbin" / "smbd").chmod(0o755)
            smb_conf = ram_root / "etc" / "smb.conf"
            smb_conf.write_text(
                f"""[global]
    passdb backend = smbpasswd:{payload_private}/smbpasswd
    username map = {payload_private}/username.map
    xattr_tdb:file = {ram_root}/private/xattr.tdb
[Data]
    path = {data_root}
""",
                encoding="utf-8",
            )
            script = f"""
RUNTIME_RAM_ROOT={shlex.quote(str(ram_root))}
RUNTIME_SMB_CONF_PATH={shlex.quote(str(smb_conf))}
RUNTIME_PERSISTENT_ROOT_PREFIX={shlex.quote(str(persistent_prefix) + "/")}
{SMBD_STATUS_HELPERS}
capture_df_for_volume_root() {{ echo "/dev/md0a 100 10 90 10% /"; }}
ps_out={shlex.quote(ps_out)}
fstat_out='root smbd 101 10 internet stream tcp 0x0 *:445'
if describe_managed_smbd_status "$ps_out" "$fstat_out"; then
    echo status=0
else
    echo status=$?
fi
"""

            result = subprocess.run(["/bin/sh", "-c", script], check=False, text=True, capture_output=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def test_smbd_status_helper_reports_device_samba_version_from_runtime_binary(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir)
            smbd_bin = runtime_root / "sbin" / "smbd"
            smbd_bin.parent.mkdir()
            smbd_bin.write_text("#!/bin/sh\necho 'Version 4.24.3'\n")
            smbd_bin.chmod(0o755)
            script = f"""
RUNTIME_RAM_ROOT={shlex.quote(str(runtime_root))}
{SMBD_STATUS_HELPERS}
describe_runtime_smbd_version
"""

            result = subprocess.run(["/bin/sh", "-c", script], check=False, text=True, capture_output=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "PASS:device Samba version: 4.24.3")

    def test_smbd_status_helper_fails_when_runtime_samba_version_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            script = f"""
RUNTIME_RAM_ROOT={shlex.quote(tmpdir)}
{SMBD_STATUS_HELPERS}
describe_runtime_smbd_version
"""

            result = subprocess.run(["/bin/sh", "-c", script], check=False, text=True, capture_output=True)

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(
            result.stdout.strip(),
            "FAIL:device Samba version unavailable (managed runtime smbd binary missing)",
        )

    def test_smbd_status_helper_reports_device_samba_version_after_smbd_checks(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            script = f"""
RUNTIME_RAM_ROOT={shlex.quote(tmpdir)}
{SMBD_STATUS_HELPERS}
describe_managed_smbd_status "" ""
"""

            result = subprocess.run(["/bin/sh", "-c", script], check=False, text=True, capture_output=True)

        lines = result.stdout.strip().splitlines()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("FAIL:smbd is missing an IPv4 or IPv6 wildcard TCP 445 listener", lines)
        self.assertEqual(lines[-1], "FAIL:device Samba version unavailable (managed runtime smbd binary missing)")
        self.assertLess(
            lines.index("FAIL:smbd is missing an IPv4 or IPv6 wildcard TCP 445 listener"),
            lines.index("FAIL:device Samba version unavailable (managed runtime smbd binary missing)"),
        )

    def test_probe_managed_smbd_reports_runtime_invariant_failures(self) -> None:
        stdout = "\n".join(
            [
                "FAIL:managed runtime smbd binary missing",
                "FAIL:active smb.conf passdb backend is not staged in RAM",
                "FAIL:one or more managed share volumes are not mounted",
                "FAIL:device Samba version unavailable (managed runtime smbd binary missing)",
            ]
        )
        with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=mock.Mock(returncode=1, stdout=stdout)):
            result = probe_managed_smbd_conn(SshConnection("host", "pw", "-o foo"), timeout_seconds=12)

        self.assertFalse(result.ready)
        self.assertEqual(
            result.detail,
            "managed runtime smbd binary missing; active smb.conf passdb backend is not staged in RAM; "
            "one or more managed share volumes are not mounted; "
            "device Samba version unavailable (managed runtime smbd binary missing)",
        )
        self.assertEqual(
            result.lines,
            (
                "FAIL:managed runtime smbd binary missing",
                "FAIL:active smb.conf passdb backend is not staged in RAM",
                "FAIL:one or more managed share volumes are not mounted",
                "FAIL:device Samba version unavailable (managed runtime smbd binary missing)",
            ),
        )

    def test_probe_managed_smbd_reports_device_samba_version_pass(self) -> None:
        stdout = "\n".join(
            [
                "PASS:managed runtime smbd binary present",
                "PASS:managed runtime smb.conf present",
                "PASS:device Samba version: 4.24.3",
            ]
        )
        with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=mock.Mock(returncode=0, stdout=stdout)) as run_ssh_mock:
            result = probe_managed_smbd_conn(SshConnection("host", "pw", "-o foo"), timeout_seconds=12)

        self.assertTrue(result.ready)
        self.assertIn("PASS:device Samba version: 4.24.3", result.lines)
        remote_cmd = run_ssh_mock.call_args.args[1]
        self.assertIn('"$RUNTIME_RAM_SBIN/smbd" --version', remote_cmd)
        self.assertIn("/usr/bin/sed -n", remote_cmd)
        self.assertIn("s/^Version[[:space:]][[:space:]]*//p", remote_cmd)
        self.assertNotIn("/usr/bin/awk", remote_cmd)
        self.assertNotIn("/usr/bin/cut", remote_cmd)
        self.assertNotIn("/usr/bin/grep", remote_cmd)

    def test_probe_managed_smbd_fails_when_device_samba_version_fails(self) -> None:
        stdout = "\n".join(
            [
                "PASS:managed runtime smbd binary present",
                "PASS:managed runtime smb.conf present",
                "FAIL:device Samba version unavailable (exit code 1)",
            ]
        )
        with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=mock.Mock(returncode=1, stdout=stdout)):
            result = probe_managed_smbd_conn(SshConnection("host", "pw", "-o foo"), timeout_seconds=12)

        self.assertFalse(result.ready)
        self.assertEqual(result.detail, "device Samba version unavailable (exit code 1)")
        self.assertIn("FAIL:device Samba version unavailable (exit code 1)", result.lines)

    def test_probe_managed_smbd_returns_detail_when_not_ready(self) -> None:
        with mock.patch(
            "timecapsulesmb.device.probe.run_ssh",
            return_value=mock.Mock(returncode=1, stdout="FAIL:managed smbd parent process is not running\n"),
        ):
            result = probe_managed_smbd_conn(SshConnection("host", "pw", "-o foo"), timeout_seconds=12)
        self.assertFalse(result.ready)
        self.assertEqual(result.detail, "managed smbd parent process is not running")

    def test_probe_managed_smbd_returns_detail_when_probe_times_out(self) -> None:
        with mock.patch(
            "timecapsulesmb.device.probe.run_ssh",
            side_effect=SshCommandTimeout("Timed out waiting for ssh command to finish: runtime probe"),
        ):
            result = probe_managed_smbd_conn(SshConnection("host", "pw", "-o foo"), timeout_seconds=12)
        self.assertFalse(result.ready)
        self.assertEqual(result.detail, "managed smbd readiness probe timed out")
        self.assertEqual(result.lines, ("FAIL:managed smbd readiness probe timed out",))

    def test_probe_managed_rsync_accepts_disabled_daemon_with_persistent_payload(self) -> None:
        stdout = "\n".join(
            (
                "PASS:persistent rsync binary is executable",
                "PASS:persistent rsync config is present",
                "SKIP:rsync daemon is disabled and not running",
            )
        )
        with mock.patch("timecapsulesmb.device.probe.read_runtime_payload_dir_conn", return_value="/Volumes/dk2/.samba4"):
            with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=mock.Mock(returncode=0, stdout=stdout)) as run_ssh_mock:
                result = probe_managed_rsync_conn(SshConnection("host", "pw", "-o foo"), timeout_seconds=12)

        self.assertTrue(result.ready)
        self.assertIn("SKIP:rsync daemon is disabled and not running", result.lines)
        remote_cmd = run_ssh_mock.call_args.args[1]
        self.assertIn('RUNTIME_PAYLOAD_DIR=/Volumes/dk2/.samba4', remote_cmd)
        self.assertNotIn("pid file", remote_cmd.lower())

    def test_probe_managed_rsync_requires_ram_process_and_tcp_873_when_enabled(self) -> None:
        stdout = "\n".join(
            (
                "PASS:persistent rsync binary is executable",
                "PASS:persistent rsync config is present",
                "PASS:managed rsync binary is executable in RAM",
                "PASS:managed rsync config is present in RAM",
                "PASS:managed rsync process is running",
                "PASS:managed rsync is bound to TCP 873",
            )
        )
        with mock.patch("timecapsulesmb.device.probe.read_runtime_payload_dir_conn", return_value="/Volumes/dk2/.samba4"):
            with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=mock.Mock(returncode=0, stdout=stdout)):
                result = probe_managed_rsync_conn(SshConnection("host", "pw", "-o foo"))

        self.assertTrue(result.ready)
        self.assertIn("PASS:managed rsync is bound to TCP 873", result.lines)

    def test_probe_managed_rsync_fails_when_disabled_but_process_is_running(self) -> None:
        stdout = "\n".join(
            (
                "PASS:persistent rsync binary is executable",
                "PASS:persistent rsync config is present",
                "FAIL:rsync daemon is disabled but an rsync process is running",
            )
        )
        with mock.patch("timecapsulesmb.device.probe.read_runtime_payload_dir_conn", return_value="/Volumes/dk2/.samba4"):
            with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=mock.Mock(returncode=1, stdout=stdout)):
                result = probe_managed_rsync_conn(SshConnection("host", "pw", "-o foo"))

        self.assertFalse(result.ready)
        self.assertEqual(result.detail, "rsync daemon is disabled but an rsync process is running")

    # The service binary's path, then `ls -i` of ACPd and boot.sh's guards.
    BINARY_V31 = (
        "/mnt/Flash/service\n"
        "19 /sbin/ACPd\n"
        "1239 /sbin/diskd\n"
        "1231 /sbin/wcifsnd\n"
    )
    PS_V31 = (
        "371 1 371 Sa 0 - 0:00 mDNSResponder\n"
        "559 1 559 S 0 - 0:00 diskd /sbin/diskd -i lo0 -d local.\n"
        "916 408 916 S 0 - 0:00 service service: role=discovery nbns=ready mdns=ready:2/2 mode=payload --netbios-name TimeCapsule --adisk-share Data dk2 12345678-1234-1234-1234-123456789012 0x82\n"
        "917 916 917 S 0 - 0:00 wcifsnd\n"
    )
    FSTAT_V31 = (
        "root     mDNSResponder  371    5* internet dgram udp *:5353\n"
        "root     mDNSResponder  371    6* internet6 dgram udp *:5353\n"
        "root     wcifsnd  917    5* internet dgram udp *:137\n"
        "root     wcifsnd  917    6* internet dgram udp *:138\n"
    )
    PLAN_V31 = (
        "config: advertise_afp=0\n"
        "plan: status=validated mode=bridge stale_seconds=0 diskless=0\n"
        "acp: raNA=0 raDS=0 waNM=1 usbF=0x450 laIP=192.168.1.10 waIP=192.168.1.10 waLL=unavailable gnRo=unavailable\n"
        'identity: instance="AirPort Time Capsule" netbios=airport-time-ca wama=E8:8D:28:58:F1:5C\n'
        "link: name=bridge0 index=9 role=lan mask=smb,adisk\n"
        "addr: link=9 family=inet addr=192.168.1.10 prefix=24\n"
        "link: name=bridge1 index=10 role=isolated mask=none\n"
    )

    def _run_mdns_probe(self, responses: list[object], native_nbns: str = "required",
                        bonjour_registrations: str = "required") -> tuple[object, mock.Mock]:
        with mock.patch("timecapsulesmb.device.probe.run_ssh", side_effect=responses) as run_ssh_mock:
            result = probe_managed_mdns_conn(
                SshConnection("host", "pw", "-o foo"),
                native_nbns=native_nbns,
                bonjour_registrations=bonjour_registrations,
            )
        return result, run_ssh_mock

    def _mdns_probe_with(self, ps_out: str, fstat_out: str | None = None, plan: str | None = None,
                         native_nbns: str = "required", bonjour_registrations: str = "required",
                         binary: str | None = None) -> object:
        result, _ = self._run_mdns_probe([
            mock.Mock(returncode=0, stdout=self.BINARY_V31 if binary is None else binary, stderr=""),
            mock.Mock(returncode=0, stdout=ps_out, stderr=""),
            mock.Mock(returncode=0, stdout=self.FSTAT_V31 if fstat_out is None else fstat_out, stderr=""),
            mock.Mock(returncode=0, stdout=self.PLAN_V31 if plan is None else plan, stderr=""),
        ], native_nbns=native_nbns, bonjour_registrations=bonjour_registrations)
        return result

    def test_probe_managed_mdns_checks_boot_sh_guards(self) -> None:
        unguarded_wcifsnd = self.BINARY_V31.replace("1231 /sbin/wcifsnd", "19 /sbin/wcifsnd")
        unguarded_diskd = self.BINARY_V31.replace("1239 /sbin/diskd", "19 /sbin/diskd")
        guarded = self._mdns_probe_with(self.PS_V31)
        self.assertTrue(guarded.ready, guarded.lines)
        self.assertIn("PASS:boot.sh guards /sbin/diskd against ACPd's starts", guarded.lines)
        self.assertIn("PASS:boot.sh guards /sbin/wcifsnd against ACPd's starts", guarded.lines)
        rc_log = "(see /mnt/Memory/samba4/var/rc.local.log)"
        wcifsnd_missing = ("boot.sh's /sbin/wcifsnd guard is missing: ACPd's wcifsnd starts take the NBNS ports "
                           f"until the manager stops them {rc_log}")
        diskd_missing = ("boot.sh's /sbin/diskd guard is missing: an ACPd start of diskd can delete "
                         f"diskd.useVolume until reboot {rc_log}")
        # Doctor fails either; deploy, which only reports NetBIOS, still
        # fails on diskd's, whose loss breaks disk activation.
        cases = (
            ("wcifsnd", unguarded_wcifsnd, "required", False, "FAIL:" + wcifsnd_missing),
            ("wcifsnd advisory", unguarded_wcifsnd, "advisory", True, "INFO:" + wcifsnd_missing),
            ("diskd", unguarded_diskd, "required", False, "FAIL:" + diskd_missing),
            ("diskd advisory", unguarded_diskd, "advisory", False, "FAIL:" + diskd_missing),
            # ls printed nothing for a name: no judgement either way.
            ("unreadable", "/mnt/Flash/service\n19 /sbin/ACPd\n1239 /sbin/diskd\n", "required", False,
             "FAIL:could not read /sbin/wcifsnd and /sbin/ACPd to check boot.sh's wcifsnd guard"),
            ("no listing", "/mnt/Flash/service\n", "advisory", False,
             "FAIL:could not read /sbin/diskd and /sbin/ACPd to check boot.sh's diskd guard"),
        )
        for name, binary, native_nbns, ready, line in cases:
            with self.subTest(name):
                result = self._mdns_probe_with(self.PS_V31, binary=binary, native_nbns=native_nbns)
                self.assertEqual(result.ready, ready, result.lines)
                self.assertIn(line, result.lines)

    def test_probe_managed_mdns_guard_listing_comes_from_the_real_script(self) -> None:
        # Run the probe's own script against a /sbin of hard links, as on the
        # device: ACPd's multi-call binary, a guarded diskd, an unguarded wcifsnd.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sbin = root / "sbin"
            sbin.mkdir()
            (sbin / "ACPd").write_bytes(b"apple")
            (sbin / "diskd").write_text("#!/bin/sh\nexit 0\n")
            os.link(sbin / "ACPd", sbin / "wcifsnd")  # Path.hardlink_to needs Python 3.10
            service = root / "service"
            service.write_text("#!/bin/sh\n")
            service.chmod(0o755)
            later = iter([mock.Mock(returncode=0, stdout=out, stderr="")
                          for out in (self.PS_V31, self.FSTAT_V31, self.PLAN_V31)])
            binary_runs = []

            def run_ssh(connection, command, **kwargs):
                if binary_runs:
                    return next(later)
                script = shlex.split(command)[2].replace("/sbin/", f"{sbin}/")
                proc = subprocess.run(["/bin/sh", "-c", script], capture_output=True, text=True,
                                      env={"RUNTIME_SERVICE_BIN": str(service), "PATH": "/usr/bin:/bin"})
                binary_runs.append(proc)
                return proc

            with mock.patch("timecapsulesmb.device.probe.run_ssh", side_effect=run_ssh):
                result = probe_managed_mdns_conn(SshConnection("host", "pw", "-o foo"))
        self.assertIn("PASS:boot.sh guards /sbin/diskd against ACPd's starts", result.lines)
        self.assertTrue(any(line.startswith("FAIL:boot.sh's /sbin/wcifsnd guard is missing") for line in result.lines),
                        result.lines)

    def test_probe_managed_mdns_reads_bonjour_registration_from_the_discovery_title(self) -> None:
        def title(mdns: str) -> str:
            return self.PS_V31.replace("mdns=ready:2/2", mdns)

        cases = [
            ("ready", title("mdns=ready:2/2"), None, "PASS:Apple mDNSResponder accepted all 2 Bonjour registrations"),
            ("starting", title("mdns=starting:1/2"), None, "FAIL:discovery Bonjour registrations are still starting"),
            # Just after the plan granted SMB, before the first registration.
            ("waiting with SMB", title("mdns=waiting:0/0"), None, "FAIL:discovery Bonjour registrations are still starting"),
            ("waiting without SMB", title("mdns=waiting:0/0"),
             self.PLAN_V31.replace("mask=smb,adisk", "mask=none").replace("status=validated", "status=incomplete reason=mode"),
             "PASS:no Bonjour registrations are due (diskless, or no link grants SMB)"),
            ("degraded", title("mdns=degraded:1/2"), None,
             "FAIL:Apple mDNSResponder has not accepted every Bonjour registration; discovery retries them "
             "(a name conflict or registration error, see discovery.log)"),
            ("unreachable", title("mdns=unreachable:0/2"), None,
             "FAIL:Apple mDNSResponder is not answering discovery's Bonjour registrations; nothing restarts it, reboot the device"),
            # A runtime from before the field existed.
            ("no state", title("").replace("  ", " "), None, "FAIL:discovery Bonjour registration state is not available yet"),
        ]
        for name, ps_out, plan, line in cases:
            with self.subTest(name):
                result = self._mdns_probe_with(ps_out, plan=plan)
                self.assertIn(line, result.lines)
                step = next(step for step in result.steps if step.id == "mdns_registered")
                self.assertEqual(step.status, line.split(":", 1)[0].lower())

    def test_probe_managed_mdns_advisory_reports_bonjour_registration_without_blocking_deploy(self) -> None:
        cases = [
            ("starting", "mdns=starting:1/2", "SKIP:discovery Bonjour registrations are still starting"),
            ("degraded", "mdns=degraded:1/2", "INFO:Apple mDNSResponder has not accepted every Bonjour registration; "
                                             "discovery retries them (a name conflict or registration error, see discovery.log)"),
            ("ready", "mdns=ready:2/2", "PASS:Apple mDNSResponder accepted all 2 Bonjour registrations"),
        ]
        for name, mdns, line in cases:
            with self.subTest(name):
                result = self._mdns_probe_with(
                    self.PS_V31.replace("mdns=ready:2/2", mdns), native_nbns="advisory", bonjour_registrations="advisory",
                )
                self.assertTrue(result.ready, result.lines)
                self.assertIn(line, result.lines)

    def test_probe_managed_mdns_hands_on_the_link_plan_it_read(self) -> None:
        # Doctor uses it to tell whether this computer is on a network where
        # the device shares its disks.
        result = self._mdns_probe_with(self.PS_V31)
        self.assertEqual(result.link_plan["status"], "validated")
        self.assertEqual(result.link_plan["mode"], "bridge")
        self.assertEqual(
            [(link["name"], link["role"], link["mask"]) for link in result.link_plan["links"]],
            [("bridge0", "lan", "smb,adisk"), ("bridge1", "isolated", "none")],
        )

    def test_probe_managed_mdns_has_no_link_plan_when_it_could_not_read_one(self) -> None:
        stopped_early, _ = self._run_mdns_probe([
            mock.Mock(returncode=0, stdout=self.BINARY_V31, stderr=""),
            SshCommandTimeout("Timed out waiting for ssh command to finish: ps"),
            mock.Mock(returncode=0, stdout="", stderr=""),
        ])
        self.assertIsNone(stopped_early.link_plan)
        failed, _ = self._run_mdns_probe([
            mock.Mock(returncode=0, stdout=self.BINARY_V31, stderr=""),
            mock.Mock(returncode=0, stdout=self.PS_V31, stderr=""),
            mock.Mock(returncode=0, stdout=self.FSTAT_V31, stderr=""),
            mock.Mock(returncode=1, stdout="", stderr="service: unknown option"),
        ])
        self.assertIsNone(failed.link_plan)

    def test_probe_managed_mdns_requires_native_nbns_to_finish_starting(self) -> None:
        result = self._mdns_probe_with(self.PS_V31.replace("nbns=ready", "nbns=starting"))
        self.assertFalse(result.ready)
        self.assertIn("FAIL:discovery native NBNS is still starting", result.lines)

    def test_probe_managed_mdns_advisory_reports_native_nbns_without_blocking_deploy(self) -> None:
        no_wcifsnd_ps = self.PS_V31.replace("917 916 917 S 0 - 0:00 wcifsnd\n", "")
        no_wcifsnd_fstat = "\n".join(line for line in self.FSTAT_V31.splitlines() if "wcifsnd" not in line) + "\n"
        unlisted = "; NetBIOS name lookups may fail, SMB and Bonjour are unaffected"
        cases = [
            ("ready", self.PS_V31, None, None,
             "PASS:Apple wcifsnd is ready on UDP 137 and 138"),
            ("starting", self.PS_V31.replace("nbns=ready", "nbns=starting"), None, None,
             "SKIP:native NBNS is still starting; deploy does not wait for it"),
            # Between retries, as after every reboot while ACPd's own wcifsnd
            # still holds the ports.
            ("retrying", no_wcifsnd_ps.replace("nbns=ready", "nbns=waiting"), no_wcifsnd_fstat, None,
             "SKIP:native NBNS is still starting; deploy does not wait for it"),
            ("ready without its ports", self.PS_V31, no_wcifsnd_fstat, None,
             "INFO:discovery native NBNS is not ready" + unlisted),
            ("no state", self.PS_V31.replace("nbns=ready ", ""), None, None,
             "INFO:discovery NBNS state is not available yet" + unlisted),
            ("mismatch", self.PS_V31.replace("nbns=ready", "nbns=waiting"), None,
             self.PLAN_V31.replace("addr=192.168.1.10", "addr=239.1.2.3"),
             "INFO:discovery native NBNS state does not match the active plan" + unlisted),
        ]
        for name, ps_out, fstat_out, plan, line in cases:
            with self.subTest(name):
                result = self._mdns_probe_with(ps_out, fstat_out, plan, native_nbns="advisory")
                self.assertTrue(result.ready, result.lines)
                self.assertIn(line, result.lines)
                # Advisory findings never become the probe's failure detail.
                self.assertNotIn("NetBIOS name lookups", result.detail)

    def test_probe_managed_mdns_passes_native_nbns_off_when_wcifsnd_has_no_address(self) -> None:
        # No DHCP server: bridge0 holds 0.0.0.0 beside its 169.254 address,
        # discovery runs no wcifsnd and says nbns=none.
        no_wcifsnd_ps = self.PS_V31.replace("917 916 917 S 0 - 0:00 wcifsnd\n", "")
        no_wcifsnd_fstat = "\n".join(line for line in self.FSTAT_V31.splitlines() if "wcifsnd" not in line) + "\n"
        no_lease_plan = self.PLAN_V31.replace(
            "addr: link=9 family=inet addr=192.168.1.10 prefix=24\n",
            "addr: link=9 family=inet addr=0.0.0.0 prefix=0\naddr: link=9 family=inet addr=169.254.147.85 prefix=16\n",
        ).replace("laIP=192.168.1.10 waIP=192.168.1.10", "laIP=169.254.147.85 waIP=unavailable")
        none_ps = no_wcifsnd_ps.replace("nbns=ready", "nbns=none")
        for native_nbns in ("required", "advisory"):
            with self.subTest(native_nbns=native_nbns):
                result = self._mdns_probe_with(none_ps, no_wcifsnd_fstat, no_lease_plan, native_nbns=native_nbns)
                self.assertTrue(result.ready, result.lines)
                self.assertIn("PASS:" + NATIVE_NBNS_OFF_DETAIL, result.lines)
                self.assertEqual(result.nbns_state, "none")
        mismatch = "FAIL:discovery native NBNS state does not match the active plan"
        # A wcifsnd still running beside "none" is not what discovery reports.
        running = self._mdns_probe_with(self.PS_V31.replace("nbns=ready", "nbns=none"), None, no_lease_plan)
        self.assertFalse(running.ready)
        self.assertIn(mismatch, running.lines)
        # Nor is "none" without a validated plan to decide it from.
        incomplete = self._mdns_probe_with(
            none_ps, no_wcifsnd_fstat, no_lease_plan.replace("status=validated", "status=incomplete reason=iflist"))
        self.assertIn(mismatch, incomplete.lines)
        self.assertEqual(self._mdns_probe_with(self.PS_V31).nbns_state, "ready")

    def test_probe_managed_mdns_names_a_wcifsnd_that_discovery_did_not_start(self) -> None:
        # An orphan, or ACPd's where boot.sh could not guard /sbin/wcifsnd;
        # the manager stops it within about a second.
        foreign_line = "4206 190 4206 S 0 - 0:00 wcifsnd\n"
        owned_line = "917 916 917 S 0 - 0:00 wcifsnd\n"
        no_owned = self.PS_V31.replace(owned_line, "")
        no_lease_plan = self.PLAN_V31.replace(
            "addr: link=9 family=inet addr=192.168.1.10 prefix=24\n",
            "addr: link=9 family=inet addr=0.0.0.0 prefix=0\naddr: link=9 family=inet addr=169.254.147.85 prefix=16\n",
        )
        cases = (
            ("none", no_owned.replace("nbns=ready", "nbns=none") + foreign_line, no_lease_plan),
            ("waiting", no_owned.replace("nbns=ready", "nbns=waiting") + foreign_line, no_lease_plan),
            ("ready beside its own child", self.PS_V31 + foreign_line, None),
        )
        for name, ps_out, plan in cases:
            with self.subTest(name):
                result = self._mdns_probe_with(ps_out, None, plan)
                self.assertFalse(result.ready)
                self.assertIn("FAIL:" + FOREIGN_WCIFSND_RUNNING, result.lines)
                advisory = self._mdns_probe_with(ps_out, None, plan, native_nbns="advisory")
                self.assertTrue(advisory.ready, advisory.lines)
                self.assertIn(f"SKIP:{FOREIGN_WCIFSND_RUNNING}; deploy does not wait for it", advisory.lines)
        # A zombie ACPd has not reaped is not a running daemon.
        zombie = self._mdns_probe_with(
            no_owned.replace("nbns=ready", "nbns=none") + "4206 190 4206 Z 0 - 0:00 wcifsnd\n", None, no_lease_plan)
        self.assertIn("PASS:" + NATIVE_NBNS_OFF_DETAIL, zombie.lines)
        # Discovery's own child beside "none" contradicts it: no retry fixes that.
        own = self._mdns_probe_with(self.PS_V31.replace("nbns=ready", "nbns=none") + foreign_line, None, no_lease_plan)
        self.assertIn("FAIL:discovery native NBNS state does not match the active plan", own.lines)

    def test_probe_managed_mdns_advisory_still_requires_bonjour(self) -> None:
        ps_out = "371 1 371 Sa 0 - 0:00 mDNSResponder\n559 1 559 S 0 - 0:00 diskd /sbin/diskd -i lo0 -d local.\n"
        result = self._mdns_probe_with(ps_out, native_nbns="advisory")
        self.assertFalse(result.ready)
        self.assertIn("FAIL:discovery process is not running", result.lines)

    def test_probe_managed_mdns_passes_when_apple_daemon_owns_5353_and_plan_grants_smb(self) -> None:
        result, run_ssh_mock = self._run_mdns_probe([
            mock.Mock(returncode=0, stdout=self.BINARY_V31, stderr=""),
            mock.Mock(returncode=0, stdout=self.PS_V31, stderr=""),
            mock.Mock(returncode=0, stdout=self.FSTAT_V31, stderr=""),
            mock.Mock(returncode=0, stdout=self.PLAN_V31, stderr=""),
        ])
        self.assertTrue(result.ready, result.lines)
        self.assertEqual(
            [call.kwargs["timeout"] for call in run_ssh_mock.call_args_list],
            [MDNS_BINARY_PROBE_TIMEOUT_SECONDS, MDNS_PROCESS_TABLE_PROBE_TIMEOUT_SECONDS, MDNS_FSTAT_PROBE_TIMEOUT_SECONDS, MDNS_SOCKET_FAMILIES_PROBE_TIMEOUT_SECONDS],
        )
        remote_commands = [call.args[1] for call in run_ssh_mock.call_args_list]
        self.assertEqual(remote_commands[1], f"/bin/sh -c {shlex.quote(PS_CAPTURE_COMMAND)}")
        self.assertIn("/internet/p", remote_commands[2])
        self.assertIn('"$RUNTIME_SERVICE_BIN" --print-link-plan', remote_commands[3])
        self.assertNotIn("discovery --print-link-plan", remote_commands[3])
        self.assertIn("PASS:Apple mDNSResponder is running", result.lines)
        self.assertIn("PASS:Apple diskd runs on loopback (-i lo0)", result.lines)
        self.assertIn("PASS:Apple mDNSResponder listens on UDP 5353 for IPv4 and IPv6", result.lines)
        self.assertIn("PASS:no other process holds UDP 5353", result.lines)
        self.assertIn("PASS:mdns link plan validated mode=bridge; SMB on bridge0(lan)", result.lines)

    def test_probe_managed_mdns_link_plan_reports_the_address_the_session_reached(self) -> None:
        """The plan step runs as written under a real shell: sshd's
        SSH_CONNECTION gives the session line, and the plan's own status
        still decides the step."""
        _, run_ssh_mock = self._run_mdns_probe([
            mock.Mock(returncode=0, stdout=self.BINARY_V31, stderr=""),
            mock.Mock(returncode=0, stdout=self.PS_V31, stderr=""),
            mock.Mock(returncode=0, stdout=self.FSTAT_V31, stderr=""),
            mock.Mock(returncode=0, stdout=self.PLAN_V31, stderr=""),
        ])
        command = run_ssh_mock.call_args_list[3].args[1]

        def run_plan_step(ssh_connection: str | None, status: int) -> subprocess.CompletedProcess[str]:
            with tempfile.TemporaryDirectory() as directory:
                service = Path(directory) / "service"
                service.write_text(f"#!/bin/sh\nprintf '%s' {shlex.quote(self.PLAN_V31)}\nexit {status}\n")
                service.chmod(0o755)
                env = {"PATH": "/bin:/usr/bin", "RUNTIME_SERVICE_BIN": str(service)}
                if ssh_connection is not None:
                    env["SSH_CONNECTION"] = ssh_connection
                return subprocess.run(command, shell=True, env=env, capture_output=True, text=True, check=False)

        reached = run_plan_step("169.254.24.209 50000 169.254.247.225 22", 0)
        self.assertEqual(reached.returncode, 0)
        self.assertEqual(reached.stdout.splitlines()[0], "session: server=169.254.247.225")
        result, _ = self._run_mdns_probe([
            mock.Mock(returncode=0, stdout=self.BINARY_V31, stderr=""),
            mock.Mock(returncode=0, stdout=self.PS_V31, stderr=""),
            mock.Mock(returncode=0, stdout=self.FSTAT_V31, stderr=""),
            mock.Mock(returncode=0, stdout=reached.stdout, stderr=""),
        ])
        self.assertTrue(result.ready, result.lines)
        self.assertEqual(result.link_plan["session_server"], "169.254.247.225")
        self.assertIn("PASS:mdns link plan validated mode=bridge; SMB on bridge0(lan)", result.lines)

        # Without SSH_CONNECTION the session field is empty, never an error.
        unset = run_plan_step(None, 0)
        self.assertEqual(unset.returncode, 0)
        self.assertEqual(_parse_link_plan(unset.stdout)["session_server"], "")
        # The plan's own failure stays the step's status.
        self.assertEqual(run_plan_step("169.254.24.209 50000 169.254.247.225 22", 13).returncode, 13)

    def test_probe_managed_mdns_fails_when_apple_daemon_is_dead_or_diskd_is_on_the_lan(self) -> None:
        ps_out = (
            "878 1 878 ZWa 0 - 0:00 (mDNSResponder)\n"
            "232 1 232 S 0 - 0:00 diskd /sbin/diskd -i  -d local.\n"
            "916 408 916 S 0 - 0:00 service service: role=discovery nbns=ready mdns=ready:2/2 mode=payload\n"
        )
        result, run_ssh_mock = self._run_mdns_probe([
            mock.Mock(returncode=0, stdout=self.BINARY_V31, stderr=""),
            mock.Mock(returncode=0, stdout=ps_out, stderr=""),
            mock.Mock(returncode=0, stdout=self.PLAN_V31, stderr=""),
        ])
        self.assertFalse(result.ready)
        # No fstat step when the daemon is gone: there is nothing to inspect, and the fix is a reboot.
        self.assertEqual(len(run_ssh_mock.call_args_list), 3)
        self.assertIn("FAIL:Apple mDNSResponder is not running (reboot the device; it cannot be restarted by hand)", result.lines)
        self.assertIn("FAIL:Apple diskd pid(s) 232 are not on loopback; their _smb/_adisk/_afpovertcp names may be visible", result.lines)
        self.assertIn("PASS:discovery process is running", result.lines)

    def test_probe_managed_mdns_fails_when_another_process_holds_5353_or_plan_grants_nothing(self) -> None:
        fstat_out = self.FSTAT_V31 + "root     avahi-dae 916    4* internet dgram udp *:5353\n"
        plan = self.PLAN_V31.replace("role=lan mask=smb,adisk", "role=isolated mask=none").replace("status=validated", "status=incomplete reason=mode")
        result, _ = self._run_mdns_probe([
            mock.Mock(returncode=0, stdout=self.BINARY_V31, stderr=""),
            mock.Mock(returncode=0, stdout=self.PS_V31, stderr=""),
            mock.Mock(returncode=0, stdout=fstat_out, stderr=""),
            mock.Mock(returncode=0, stdout=plan, stderr=""),
        ])
        self.assertFalse(result.ready)
        self.assertIn("FAIL:other processes hold UDP 5353: avahi-dae", result.lines)
        self.assertIn("FAIL:sharing facts are incomplete (mode); the registrant waits or retains its previous validated policy", result.lines)

    def test_probe_managed_mdns_accepts_diskless_registrant_without_smb_links(self) -> None:
        ps_out = self.PS_V31.replace(
            "nbns=ready mdns=ready:2/2 mode=payload --netbios-name TimeCapsule --adisk-share Data dk2 12345678-1234-1234-1234-123456789012 0x82\n917 916 917 S 0 - 0:00 wcifsnd",
            "nbns=waiting mdns=waiting:0/0 mode=diskless --diskless",
        )
        plan = self.PLAN_V31.replace("mask=smb,adisk", "mask=none").replace("diskless=0", "diskless=1")
        result, _ = self._run_mdns_probe([
            mock.Mock(returncode=0, stdout=self.BINARY_V31, stderr=""),
            mock.Mock(returncode=0, stdout=ps_out, stderr=""),
            mock.Mock(returncode=0, stdout=self.FSTAT_V31, stderr=""),
            mock.Mock(returncode=0, stdout=plan, stderr=""),
        ])
        self.assertTrue(result.ready, result.lines)
        self.assertIn("PASS:mdns link plan validated (diskless; nothing advertised)", result.lines)

    def test_probe_managed_mdns_reports_missing_registrant_and_unparsable_plan(self) -> None:
        ps_out = "371 1 371 Sa 0 - 0:00 mDNSResponder\n559 1 559 S 0 - 0:00 diskd /sbin/diskd -i lo0 -d local.\n"
        result, _ = self._run_mdns_probe([
            mock.Mock(returncode=0, stdout=self.BINARY_V31, stderr=""),
            mock.Mock(returncode=0, stdout=ps_out, stderr=""),
            mock.Mock(returncode=0, stdout=self.FSTAT_V31, stderr=""),
            mock.Mock(returncode=0, stdout="garbage\n", stderr=""),
        ])
        self.assertFalse(result.ready)
        self.assertIn("FAIL:discovery process is not running", result.lines)
        self.assertIn("FAIL:mdns link plan output could not be parsed", result.lines)

    def test_probe_managed_mdns_rejects_controller_without_native_nbns_state(self) -> None:
        ps_out = self.PS_V31.replace("nbns=ready ", "")
        result, _ = self._run_mdns_probe([
            mock.Mock(returncode=0, stdout=self.BINARY_V31, stderr=""),
            mock.Mock(returncode=0, stdout=ps_out, stderr=""),
            mock.Mock(returncode=0, stdout=self.FSTAT_V31, stderr=""),
            mock.Mock(returncode=0, stdout=self.PLAN_V31, stderr=""),
        ])

        self.assertFalse(result.ready)
        self.assertIn("FAIL:discovery NBNS state is not available yet", result.lines)

    def test_probe_managed_mdns_rejects_wcifsnd_sockets_owned_by_wrong_pid(self) -> None:
        wrong_pid_fstat = self.FSTAT_V31.replace("wcifsnd  917", "wcifsnd  999")
        result, _ = self._run_mdns_probe([
            mock.Mock(returncode=0, stdout=self.BINARY_V31, stderr=""),
            mock.Mock(returncode=0, stdout=self.PS_V31, stderr=""),
            mock.Mock(returncode=0, stdout=wrong_pid_fstat, stderr=""),
            mock.Mock(returncode=0, stdout=self.PLAN_V31, stderr=""),
        ])

        self.assertFalse(result.ready)
        self.assertIn("FAIL:discovery native NBNS is not ready", result.lines)

    def test_probe_managed_mdns_reports_nbns_retry_as_not_ready(self) -> None:
        ps_out = self.PS_V31.replace("nbns=ready", "nbns=waiting").replace(
            "917 916 917 S 0 - 0:00 wcifsnd\n", ""
        )
        fstat_out = "\n".join(line for line in self.FSTAT_V31.splitlines() if "wcifsnd" not in line) + "\n"
        result, _ = self._run_mdns_probe([
            mock.Mock(returncode=0, stdout=self.BINARY_V31, stderr=""),
            mock.Mock(returncode=0, stdout=ps_out, stderr=""),
            mock.Mock(returncode=0, stdout=fstat_out, stderr=""),
            mock.Mock(returncode=0, stdout=self.PLAN_V31, stderr=""),
        ])
        self.assertFalse(result.ready)
        self.assertIn("FAIL:discovery native NBNS is not ready", result.lines)

    def test_probe_managed_mdns_accepts_waiting_without_service_eligible_ipv4(self) -> None:
        ps_out = self.PS_V31.replace("nbns=ready", "nbns=waiting").replace(
            "917 916 917 S 0 - 0:00 wcifsnd\n", ""
        )
        fstat_out = "\n".join(line for line in self.FSTAT_V31.splitlines() if "wcifsnd" not in line) + "\n"
        plan = self.PLAN_V31.replace("addr=192.168.1.10", "addr=239.1.2.3")
        result, _ = self._run_mdns_probe([
            mock.Mock(returncode=0, stdout=self.BINARY_V31, stderr=""),
            mock.Mock(returncode=0, stdout=ps_out, stderr=""),
            mock.Mock(returncode=0, stdout=fstat_out, stderr=""),
            mock.Mock(returncode=0, stdout=plan, stderr=""),
        ])

        self.assertTrue(result.ready, result.lines)
        self.assertIn("PASS:native NBNS is waiting", result.lines)
        result, _ = self._run_mdns_probe([
            mock.Mock(returncode=0, stdout=self.BINARY_V31, stderr=""),
            mock.Mock(returncode=0, stdout=ps_out, stderr=""),
            mock.Mock(returncode=0, stdout=self.FSTAT_V31, stderr=""),
            mock.Mock(returncode=13, stdout="", stderr=""),
        ])
        self.assertIn("FAIL:mdns link plan probe failed with exit code 13", result.lines)

    def test_probe_managed_mdns_fails_the_diskd_gate_when_a_stray_diskd_runs_beside_ours(self) -> None:
        """Review 2 R8: `-i lo0` present somewhere is not enough; ACPd's diskd
        next to ours still advertises on the LAN (the runtime calls that acpd)."""
        ps_out = self.PS_V31 + "640 1 640 S 0 - 0:00 diskd /sbin/diskd -i  -d local.\n" + "735 1 735 ZW 0 - 0:00 diskd (diskd)\n"
        result, _ = self._run_mdns_probe([
            mock.Mock(returncode=0, stdout=self.BINARY_V31, stderr=""),
            mock.Mock(returncode=0, stdout=ps_out, stderr=""),
            mock.Mock(returncode=0, stdout=self.FSTAT_V31, stderr=""),
            mock.Mock(returncode=0, stdout=self.PLAN_V31, stderr=""),
        ])
        self.assertFalse(result.ready)
        self.assertIn(
            "FAIL:Apple diskd pid(s) 640 are not on loopback; their _smb/_adisk/_afpovertcp names may be visible"
            " (a loopback diskd also runs; the manager retries the cleanup every disk pass)",
            result.lines,
        )
        # Token matching: `-i lo0` must be the argv pair, not a substring elsewhere.
        ps_out = self.PS_V31.replace("/sbin/diskd -i lo0 -d local.", "/sbin/diskd -d local.-i lo0")
        result, _ = self._run_mdns_probe([
            mock.Mock(returncode=0, stdout=self.BINARY_V31, stderr=""),
            mock.Mock(returncode=0, stdout=ps_out, stderr=""),
            mock.Mock(returncode=0, stdout=self.FSTAT_V31, stderr=""),
            mock.Mock(returncode=0, stdout=self.PLAN_V31, stderr=""),
        ])
        self.assertIn("FAIL:Apple diskd pid(s) 559 are not on loopback; their _smb/_adisk/_afpovertcp names may be visible", result.lines)

    def test_probe_managed_mdns_parses_escaped_instance_names_and_survives_malformed_lines(self) -> None:
        """Review 2 R9: the plan line escapes quotes/backslashes; a line that
        still does not parse becomes a diagnostic, not an exception."""
        plan = self.PLAN_V31.replace('instance="AirPort Time Capsule"', 'instance="Capsule \\"A\\" \\\\ B"')
        result, _ = self._run_mdns_probe([
            mock.Mock(returncode=0, stdout=self.BINARY_V31, stderr=""),
            mock.Mock(returncode=0, stdout=self.PS_V31, stderr=""),
            mock.Mock(returncode=0, stdout=self.FSTAT_V31, stderr=""),
            mock.Mock(returncode=0, stdout=plan, stderr=""),
        ])
        self.assertTrue(result.ready, result.lines)
        broken = self.PLAN_V31.replace('instance="AirPort Time Capsule"', 'instance="Capsule "A"')
        result, _ = self._run_mdns_probe([
            mock.Mock(returncode=0, stdout=self.BINARY_V31, stderr=""),
            mock.Mock(returncode=0, stdout=self.PS_V31, stderr=""),
            mock.Mock(returncode=0, stdout=self.FSTAT_V31, stderr=""),
            mock.Mock(returncode=0, stdout=broken, stderr=""),
        ])
        # The identity line is malformed; the plan/link lines still parse.
        self.assertTrue(result.ready, result.lines)

    def test_probe_managed_mdns_retries_binary_probe_timeout_and_reports_fstat_timeout(self) -> None:
        result, run_ssh_mock = self._run_mdns_probe([
            SshCommandTimeout("Timed out waiting for ssh command to finish: binary"),
            mock.Mock(returncode=0, stdout=self.BINARY_V31, stderr=""),
            mock.Mock(returncode=0, stdout=self.PS_V31, stderr=""),
            mock.Mock(returncode=0, stdout=self.FSTAT_V31, stderr=""),
            mock.Mock(returncode=0, stdout=self.PLAN_V31, stderr=""),
        ])
        self.assertTrue(result.ready, result.lines)
        self.assertEqual([call.kwargs["timeout"] for call in run_ssh_mock.call_args_list[:2]],
                         [MDNS_BINARY_PROBE_TIMEOUT_SECONDS, MDNS_BINARY_PROBE_TIMEOUT_SECONDS])
        result, _ = self._run_mdns_probe([
            mock.Mock(returncode=0, stdout=self.BINARY_V31, stderr=""),
            mock.Mock(returncode=0, stdout=self.PS_V31, stderr=""),
            SshCommandTimeout("Timed out waiting for ssh command to finish: fstat"),
        ])
        self.assertFalse(result.ready)
        self.assertEqual(result.detail, f"mdns fstat probe timed out after {MDNS_FSTAT_PROBE_TIMEOUT_SECONDS}s")

    def test_probe_managed_mdns_reports_binary_timeout_after_retry(self) -> None:
        with mock.patch(
            "timecapsulesmb.device.probe.run_ssh",
            side_effect=[
                SshCommandTimeout("Timed out waiting for ssh command to finish: binary"),
                SshCommandTimeout("Timed out waiting for ssh command to finish: binary"),
            ],
        ) as run_ssh_mock:
            result = probe_managed_mdns_conn(SshConnection("host", "pw", "-o foo"))

        self.assertFalse(result.ready)
        self.assertEqual(
            result.detail,
            f"mdns binary probe timed out after {MDNS_BINARY_PROBE_TIMEOUT_SECONDS}s",
        )
        self.assertEqual(
            [call.kwargs["timeout"] for call in run_ssh_mock.call_args_list],
            [MDNS_BINARY_PROBE_TIMEOUT_SECONDS, MDNS_BINARY_PROBE_TIMEOUT_SECONDS],
        )
        self.assertIn(
            f"FAIL:mdns binary probe timed out after {MDNS_BINARY_PROBE_TIMEOUT_SECONDS}s",
            result.lines,
        )

    def test_probe_managed_mdns_takeover_reports_process_table_timeout(self) -> None:
        # The table alone never waits; it names what the listing waited for.
        # The detail stays one text for every such timeout.
        listing = "  812 700 812 D 127 vmmapbsy 0:00.00 ps\n  700 1 700 D 127 biowait 0:00.00 service\n  371 1 371 S 127 select 0:00.00 mDNSResponder\n"
        detail = f"mDNS process table probe timed out after {MDNS_PROCESS_TABLE_PROBE_TIMEOUT_SECONDS}s"
        for stuck, table, asleep in (
            (mock.Mock(returncode=0, stdout=listing, stderr=""), listing,
             "ps (pid 812) on vmmapbsy, service (pid 700) on biowait"),
            (mock.Mock(returncode=1, stdout=listing, stderr=""), "", ""),
            (SshCommandTimeout("Timed out waiting for ssh command to finish: ps"), "", ""),
        ):
            with self.subTest(stuck=stuck), mock.patch(
                "timecapsulesmb.device.probe.run_ssh",
                side_effect=[
                    mock.Mock(returncode=0, stdout=self.BINARY_V31, stderr=""),
                    SshCommandTimeout("Timed out waiting for ssh command to finish: ps"),
                    stuck,
                ],
            ) as run_ssh_mock:
                result = probe_managed_mdns_conn(SshConnection("host", "pw", "-o foo"))
            self.assertFalse(result.ready)
            self.assertEqual(result.detail, detail)
            self.assertIn(f"FAIL:{detail}", result.lines)
            step = next(step for step in result.steps if step.id == "mdns_process_table_probe")
            self.assertEqual((step.status, step.stdout, step.asleep_in_kernel), ("timeout", table, asleep))
            self.assertEqual(run_ssh_mock.call_args_list[2].args[1], STUCK_PROCESS_PS_COMMAND)

    def test_probe_managed_mdns_reports_a_discovery_process_it_could_not_identify(self) -> None:
        # A service process whose command line was not read may be discovery:
        # it is neither found nor missing, and doctor waits for it.
        ps_out = self.PS_V31.replace(
            "service service: role=discovery nbns=ready mdns=ready:2/2 mode=payload --netbios-name TimeCapsule "
            "--adisk-share Data dk2 12345678-1234-1234-1234-123456789012 0x82",
            f"service {UNREADABLE_COMMAND}",
        )
        result = self._mdns_probe_with(ps_out)
        self.assertFalse(result.ready)
        self.assertIn(f"FAIL:{DISCOVERY_UNREADABLE}", result.lines)
        self.assertNotIn("FAIL:discovery process is not running", result.lines)

        # Beside a discovery process that was read, it changes nothing.
        result = self._mdns_probe_with(self.PS_V31 + f"920 408 920 D 0 - 0:00 service {UNREADABLE_COMMAND}\n")
        self.assertIn("PASS:discovery process is running", result.lines)

    def test_probe_managed_mdns_skips_the_loopback_check_for_an_unreadable_diskd(self) -> None:
        unreadable = self.PS_V31.replace("diskd /sbin/diskd -i lo0 -d local.", f"diskd {UNREADABLE_COMMAND}")
        result = self._mdns_probe_with(unreadable)
        self.assertTrue(result.ready, result.lines)
        self.assertIn(f"SKIP:{DISKD_UNREADABLE}", result.lines)

        # A diskd known to be on the LAN still fails.
        stray = unreadable + "560 1 560 S 0 - 0:00 diskd /sbin/diskd\n"
        result = self._mdns_probe_with(stray)
        self.assertFalse(result.ready)
        self.assertTrue(any(line.startswith("FAIL:Apple diskd pid(s) 560 are not on loopback") for line in result.lines))

    def test_decide_manual_activation_skips_ready_runtime(self) -> None:
        runtime_ready = ManagedRuntimeProbeResult(
            ready=True,
            detail="managed runtime is ready",
            smbd=readiness_result(True, "managed smbd ready", ("PASS:managed smbd ready",)),
            mdns=readiness_result(True, "managed mDNS registrant active", ("PASS:managed mDNS registrant active",)),
        )
        with mock.patch("timecapsulesmb.services.activation.probe_managed_runtime_conn", return_value=runtime_ready) as runtime_mock:
            decision = decide_manual_activation(SshConnection("host", "pw", "-o foo"), runtime_probe_timeout_seconds=9)

        self.assertFalse(decision.run_actions)
        self.assertFalse(decision.verify_runtime)
        self.assertEqual(decision.reason, "runtime_already_ready")
        self.assertIs(decision.runtime, runtime_ready)
        runtime_mock.assert_called_once_with(SshConnection("host", "pw", "-o foo"), timeout_seconds=9)

    def test_complete_deployment_no_wait_requests_reboot_without_verifying_runtime(self) -> None:
        prepared_plan = self._prepared_deploy_plan(
            startup_mode=DEPLOY_STARTUP_REBOOT_THEN_ACTIVATE,
            payload_family="netbsd4be_samba4",
            is_netbsd4=True,
            wait_after_reboot=False,
        )
        callbacks, _stages, logs, _debug_fields, _finish_fields = self._operation_callbacks()
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")
        verify_runtime = mock.Mock()

        with mock.patch("timecapsulesmb.services.deploy.reboot_device", return_value=None) as reboot:
            result = complete_deployment_after_upload(
                connection,
                prepared_plan,
                no_wait=True,
                callbacks=callbacks,
                messages=DeployCompletionMessages(reboot_request_message="Requesting reboot..."),
                verify_runtime_func=verify_runtime,
            )

        # The marker still goes on the device, so the next command can tell
        # a restart that never happened.
        reboot.assert_called_once_with("root@10.0.0.2", "pw", wait=False, callbacks=callbacks, ssh=connection)
        verify_runtime.assert_not_called()
        self.assertIn("Requesting reboot...", logs)
        self.assertTrue(result.reboot_requested)
        self.assertFalse(result.waited)
        self.assertFalse(result.verified)

    def test_complete_deployment_activates_where_the_device_came_back(self) -> None:
        prepared_plan = self._prepared_deploy_plan(
            startup_mode=DEPLOY_STARTUP_REBOOT_THEN_ACTIVATE,
            payload_family="netbsd4be_samba4",
            is_netbsd4=True,
        )
        callbacks, _stages, _logs, _debug_fields, _finish_fields = self._operation_callbacks()
        run_actions = mock.Mock()
        verify_runtime = mock.Mock()
        with mock.patch("timecapsulesmb.services.deploy.reboot_device", return_value="root@10.0.0.9"):
            complete_deployment_after_upload(
                SshConnection("root@10.0.0.2", "pw", "-o foo"),
                prepared_plan,
                no_wait=False,
                callbacks=callbacks,
                run_remote_actions_func=run_actions,
                verify_runtime_func=verify_runtime,
            )

        moved = SshConnection("root@10.0.0.9", "pw", "-o foo")
        run_actions.assert_called_once_with(moved, prepared_plan.plan.activation_actions)
        self.assertEqual(verify_runtime.call_args.args[0], moved)

    def test_complete_deployment_netbsd4_runs_activation_after_reboot_when_autostart_missing(self) -> None:
        prepared_plan = self._prepared_deploy_plan(
            startup_mode=DEPLOY_STARTUP_REBOOT_THEN_ACTIVATE,
            payload_family="netbsd4be_samba4",
            is_netbsd4=True,
        )
        callbacks, stages, logs, debug_fields, _finish_fields = self._operation_callbacks()
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")
        run_actions = mock.Mock()
        verify_runtime = mock.Mock()
        order = mock.Mock()
        with mock.patch("time.sleep") as sleep_mock, mock.patch("timecapsulesmb.services.deploy.reboot_device", return_value=None) as reboot:
            order.attach_mock(reboot, "reboot")
            order.attach_mock(run_actions, "run_actions")
            order.attach_mock(verify_runtime, "verify_runtime")
            result = complete_deployment_after_upload(
                connection,
                prepared_plan,
                no_wait=False,
                callbacks=callbacks,
                run_remote_actions_func=run_actions,
                verify_runtime_func=verify_runtime,
            )

        self.assertTrue(reboot.call_args.kwargs["wait"])
        # The reboot wait returns once SSH is open, and verification polls on
        # its own, so nothing waits a fixed time in between.
        sleep_mock.assert_not_called()
        self.assertEqual([call[0] for call in order.mock_calls], ["reboot", "run_actions", "verify_runtime"])
        run_actions.assert_called_once_with(connection, prepared_plan.plan.activation_actions)
        # The boot hook answer came from the probe before the deploy: nothing
        # is read between the reboot and the start.
        self.assertEqual(stages, ["post_reboot_activation"])
        self.assertEqual(debug_fields["activation_decision"], "firmware_autostart_missing")
        self.assertTrue(debug_fields["manual_activation_required"])
        self.assertIn("Activating deployed runtime after reboot.", logs)
        self.assertTrue(result.rebooted)
        self.assertTrue(result.verified)
        # The app translates the follow-up through this key, so the service owns it.
        self.assertEqual(result.summary, netbsd4_activation_summary())
        self.assertEqual(result.message, NETBSD4_ACTIVATION_COMPLETED)

    def test_complete_deployment_netbsd4_with_the_boot_hook_only_waits_after_reboot(self) -> None:
        prepared_plan = self._prepared_deploy_plan(
            startup_mode=DEPLOY_STARTUP_REBOOT_THEN_ACTIVATE,
            payload_family="netbsd4le_samba4",
            is_netbsd4=True,
            rc_local_autostart=True,
        )
        callbacks, stages, logs, debug_fields, _finish_fields = self._operation_callbacks()
        run_actions = mock.Mock()
        verify_runtime = mock.Mock()
        with mock.patch("timecapsulesmb.services.deploy.reboot_device", return_value=None):
            result = complete_deployment_after_upload(
                SshConnection("root@10.0.0.2", "pw", "-o foo"),
                prepared_plan,
                no_wait=False,
                callbacks=callbacks,
                run_remote_actions_func=run_actions,
                verify_runtime_func=verify_runtime,
            )

        run_actions.assert_not_called()
        verify_runtime.assert_called_once()
        self.assertEqual(verify_runtime.call_args.kwargs["stage"], "verify_runtime_activation")
        self.assertEqual(stages, [])
        self.assertEqual(debug_fields["activation_decision"], "firmware_autostart_enabled")
        self.assertFalse(debug_fields["manual_activation_required"])
        self.assertIn("NetBSD4 firmware autostart is enabled; waiting for managed runtime.", logs)
        self.assertTrue(result.verified)

    def test_complete_deployment_netbsd6_reboot_waits_for_runtime(self) -> None:
        prepared_plan = self._prepared_deploy_plan(startup_mode=DEPLOY_STARTUP_REBOOT_THEN_VERIFY)
        callbacks, stages, logs, _debug_fields, _finish_fields = self._operation_callbacks()
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")
        verify_runtime = mock.Mock()

        with mock.patch("time.sleep") as sleep_mock, mock.patch("timecapsulesmb.services.deploy.reboot_device", return_value=None):
            result = complete_deployment_after_upload(
                connection,
                prepared_plan,
                no_wait=False,
                callbacks=callbacks,
                messages=DeployCompletionMessages(reboot_runtime_wait_message="Waiting for managed runtime..."),
                verify_runtime_func=verify_runtime,
            )

        sleep_mock.assert_not_called()
        verify_runtime.assert_called_once()
        self.assertEqual(verify_runtime.call_args.kwargs["stage"], "verify_runtime_reboot")
        self.assertIn("Waiting for managed runtime...", logs)
        self.assertEqual(stages, [])
        self.assertTrue(result.verified)
        self.assertIsNone(result.summary)
        self.assertIsNone(result.message)

    def test_deploy_completion_payload_forwards_the_service_summary(self) -> None:
        from timecapsulesmb.app.ops.deploy import _deploy_completion_payload
        from timecapsulesmb.services.deploy import DeployCompletionResult

        def completion(**fields: object) -> DeployCompletionResult:
            return DeployCompletionResult(
                payload_dir="/Volumes/dk2/.samba4", payload_family="netbsd4_samba4", is_netbsd4=True,
                rebooted=True, reboot_requested=True, waited=True, verified=True, **fields)

        netbsd4 = _deploy_completion_payload(completion(
            message=NETBSD4_ACTIVATION_COMPLETED, summary=netbsd4_activation_summary()))
        generic = _deploy_completion_payload(completion())

        self.assertEqual(netbsd4["summary_key"], "activation_completed_followup")
        self.assertEqual(netbsd4["summary"], NETBSD4_ACTIVATION_COMPLETED)
        self.assertEqual(netbsd4["message"], NETBSD4_ACTIVATION_COMPLETED)
        self.assertEqual(generic["summary_key"], "deploy_completed")
        self.assertEqual(generic["summary"], "Deployment completed.")
        self.assertNotIn("message", generic)

    def test_probe_managed_runtime_once_checks_both_probes_and_rechecks_mdns_after_settle(self) -> None:
        smbd_ready = readiness_result(True, "managed smbd ready", ("PASS:managed smbd ready",))
        mdns_ready = readiness_result(True, "managed mDNS registrant active", ("PASS:managed mDNS registrant active",))
        rsync_ready = readiness_result(True, "managed rsync disabled", ("SKIP:managed rsync disabled",))
        connection = SshConnection("host", "pw", "-o foo")
        with mock.patch("timecapsulesmb.device.probe.probe_managed_smbd_conn", return_value=smbd_ready) as smbd_mock:
            with mock.patch("timecapsulesmb.device.probe.probe_managed_mdns_conn", side_effect=[mdns_ready, mdns_ready]) as mdns_mock:
                with mock.patch("timecapsulesmb.device.probe.probe_managed_rsync_conn", return_value=rsync_ready) as rsync_mock:
                    with mock.patch("timecapsulesmb.device.probe.time.sleep") as sleep_mock:
                        result = probe_managed_runtime_once_conn(connection)
        self.assertTrue(result.ready)
        smbd_mock.assert_called_once()
        self.assertEqual(smbd_mock.call_args.kwargs["timeout_seconds"], 30)
        self.assertEqual(mdns_mock.call_count, 2)
        # Deploy never waits on or fails for native NBNS (first and settle probe).
        self.assertEqual([call.kwargs.get("native_nbns") for call in mdns_mock.call_args_list], ["advisory", "advisory"])
        rsync_mock.assert_called_once_with(connection)
        self.assertEqual(sleep_mock.call_args_list, [mock.call(3.0)])

    def test_probe_managed_runtime_continues_polling_after_single_probe_timeout(self) -> None:
        runtime_timeout = ManagedRuntimeProbeResult(
            ready=False,
            detail="managed smbd readiness probe timed out; managed mDNS registrant active",
            smbd=readiness_result(False, "managed smbd readiness probe timed out", ("FAIL:managed smbd readiness probe timed out",)),
            mdns=readiness_result(True, "managed mDNS registrant active", ("PASS:managed mDNS registrant active",)),
        )
        runtime_ready = ManagedRuntimeProbeResult(
            ready=True,
            detail="managed runtime is ready",
            smbd=readiness_result(True, "managed smbd ready", ("PASS:managed smbd ready",)),
            mdns=readiness_result(True, "managed mDNS registrant active", ("PASS:managed mDNS registrant active",)),
        )
        connection = SshConnection("host", "pw", "-o foo")
        with mock.patch("timecapsulesmb.device.probe.read_manager_state", return_value=manager_state(())), mock.patch("timecapsulesmb.device.probe.probe_managed_runtime_once_conn", side_effect=[runtime_timeout, runtime_ready]) as runtime_once:
            result = probe_managed_runtime_conn(
                connection,
                timeout_seconds=10,
                poll_interval_seconds=0.0,
                smbd_mdns_stagger_seconds=0.0,
                mdns_settle_seconds=0.0,
            )
        self.assertTrue(result.ready)
        self.assertEqual(runtime_once.call_count, 2)
        self.assertEqual([attempt.phase for attempt in result.attempts], ["soft_window", "soft_window"])
        self.assertEqual(result.soft_timeout_seconds, 10)
        self.assertEqual(result.final_attempts_allowed, 2)

    def test_probe_managed_runtime_runs_two_final_checks_after_soft_window_expires(self) -> None:
        runtime_not_ready = ManagedRuntimeProbeResult(
            ready=False,
            detail="managed smbd not ready; managed mDNS registrant not active",
            smbd=readiness_result(False, "managed smbd not ready", ("FAIL:managed smbd not ready",)),
            mdns=readiness_result(False, "managed mDNS registrant not active", ("FAIL:managed mDNS registrant not active",)),
        )
        connection = SshConnection("host", "pw", "-o foo")
        with mock.patch("timecapsulesmb.device.probe.read_manager_state", return_value=manager_state(())), mock.patch("timecapsulesmb.device.probe.probe_managed_runtime_once_conn", return_value=runtime_not_ready) as runtime_once:
            result = probe_managed_runtime_conn(
                connection,
                timeout_seconds=0,
                poll_interval_seconds=0.0,
                smbd_mdns_stagger_seconds=0.0,
                mdns_settle_seconds=0.0,
            )
        self.assertFalse(result.ready)
        self.assertEqual(runtime_once.call_count, 2)
        self.assertEqual([attempt.phase for attempt in result.attempts], ["final_check", "final_check"])
        self.assertIn("runtime verification timed out after 0s plus 2 final checks", result.detail)
        self.assertIn("FAIL:runtime verification timed out after 0s plus 2 final checks", result.lines)

    def test_probe_managed_runtime_finishes_soft_attempt_that_runs_past_deadline(self) -> None:
        runtime_not_ready = ManagedRuntimeProbeResult(
            ready=False,
            detail="managed smbd not ready; managed mDNS registrant not active",
            smbd=readiness_result(False, "managed smbd not ready", ("FAIL:managed smbd not ready",)),
            mdns=readiness_result(False, "managed mDNS registrant not active", ("FAIL:managed mDNS registrant not active",)),
        )
        connection = SshConnection("host", "pw", "-o foo")
        monotonic_values = iter([0.0, 0.0, 0.0, 5.0, 5.0, 5.0, 5.1, 5.2, 5.3, 5.4])
        with mock.patch("timecapsulesmb.device.probe.read_manager_state", return_value=manager_state(())), mock.patch("timecapsulesmb.device.probe.probe_managed_runtime_once_conn", return_value=runtime_not_ready) as runtime_once:
            with mock.patch("timecapsulesmb.device.probe.time.monotonic", side_effect=lambda: next(monotonic_values)):
                result = probe_managed_runtime_conn(
                    connection,
                    timeout_seconds=1,
                    poll_interval_seconds=0.0,
                    smbd_mdns_stagger_seconds=0.0,
                    mdns_settle_seconds=0.0,
                )
        self.assertFalse(result.ready)
        self.assertEqual(runtime_once.call_count, 3)
        self.assertEqual([attempt.phase for attempt in result.attempts], ["soft_window", "final_check", "final_check"])

    def test_probe_managed_runtime_reports_readable_timeout(self) -> None:
        runtime_not_ready = ManagedRuntimeProbeResult(
            ready=False,
            detail="managed smbd readiness probe timed out; managed mDNS takeover probe timed out",
            smbd=readiness_result(False, "managed smbd readiness probe timed out", ("FAIL:managed smbd readiness probe timed out",)),
            mdns=readiness_result(False, "managed mDNS takeover probe timed out", ("FAIL:managed mDNS takeover probe timed out",)),
        )
        connection = SshConnection("host", "pw", "-o foo")
        with mock.patch("timecapsulesmb.device.probe.read_manager_state", return_value=manager_state(())), mock.patch("timecapsulesmb.device.probe.probe_managed_runtime_once_conn", return_value=runtime_not_ready):
            result = probe_managed_runtime_conn(
                connection,
                timeout_seconds=0,
                poll_interval_seconds=0.0,
                smbd_mdns_stagger_seconds=0.0,
                mdns_settle_seconds=0.0,
            )
        self.assertFalse(result.ready)
        self.assertIn("runtime verification timed out after 0s plus 2 final checks", result.detail)
        self.assertIn("FAIL:runtime verification timed out after 0s plus 2 final checks", result.lines)

    @staticmethod
    def _runtime_not_ready(samba_ready: bool = False) -> ManagedRuntimeProbeResult:
        if samba_ready:
            # Samba serves; only rsync is not running.
            return ManagedRuntimeProbeResult(
                ready=False,
                detail="managed rsync process is not running",
                smbd=readiness_result(True, "managed smbd ready", ("PASS:managed smbd ready",)),
                mdns=readiness_result(True, "managed mDNS registrant active", ("PASS:managed mDNS registrant active",)),
                extra_steps=(ProbeStepResult(id="rsync", status="fail", detail="managed rsync process is not running"),),
                samba_ready=True,
            )
        return ManagedRuntimeProbeResult(
            ready=False,
            detail="managed smbd not ready; managed mDNS registrant not active",
            smbd=readiness_result(False, "managed smbd not ready", ("FAIL:managed smbd not ready",)),
            mdns=readiness_result(False, "managed mDNS registrant not active", ("FAIL:managed mDNS registrant not active",)),
        )

    def _payload_problem_runtime(self, problem) -> ManagedRuntimeProbeResult:
        # A wait whose window is already over: every verdict ends it.
        runtime = self._runtime_not_ready(samba_ready=not problem.samba_off)
        with mock.patch("timecapsulesmb.device.probe.read_manager_state", return_value=manager_state((problem,))):
            with mock.patch("timecapsulesmb.device.probe.probe_managed_runtime_once_conn", return_value=runtime):
                return probe_managed_runtime_conn(
                    SshConnection("host", "pw", "-o foo"), timeout_seconds=0, poll_interval_seconds=0.0,
                    smbd_mdns_stagger_seconds=0.0, mdns_settle_seconds=0.0,
                )

    def test_probe_managed_runtime_stops_waiting_at_any_verdict_in_the_title(self) -> None:
        # payload=missing, payload=damaged or rsync=damaged holds however long
        # the verification waits: it ends after the first attempt, not at
        # 200 s; rsync's once Samba is ready, so the result says how it is.
        connection = SshConnection("host", "pw", "-o foo")
        for problem in (problem for problem in DISK_PAYLOAD_PROBLEMS if not problem.replaces_disk):
            with self.subTest(problem.token):
                with mock.patch("timecapsulesmb.device.probe.read_manager_state", return_value=manager_state((problem,))) as asked:
                    with mock.patch("timecapsulesmb.device.probe.probe_managed_runtime_once_conn",
                                    return_value=self._runtime_not_ready(samba_ready=not problem.samba_off)) as runtime_once:
                        result = probe_managed_runtime_conn(
                            connection, timeout_seconds=200, poll_interval_seconds=0.0,
                            smbd_mdns_stagger_seconds=0.0, mdns_settle_seconds=0.0,
                        )
                self.assertFalse(result.ready)
                self.assertEqual(runtime_once.call_count, 1)
                asked.assert_called_once_with(connection)
                self.assertIs(runtime_payload_problem(result), problem)
                self.assertEqual(result.detail, problem.message)
                self.assertIn(f"FAIL:{problem.message}", result.lines)

    def _wait_with_listings(self, states, runtime, *, timeout_seconds: int = 200):
        """A wait whose attempts read these listings in turn (the last one
        again after them), each attempt not ready as runtime."""
        states = list(states)
        listings = iter(states)
        with mock.patch("timecapsulesmb.device.probe.read_manager_state",
                        side_effect=lambda _connection: next(listings, states[-1])) as asked, \
                mock.patch("timecapsulesmb.device.probe.probe_managed_runtime_once_conn", return_value=runtime) as once:
            result = probe_managed_runtime_conn(
                SshConnection("host", "pw", "-o foo"), timeout_seconds=timeout_seconds, poll_interval_seconds=0.0,
                smbd_mdns_stagger_seconds=0.0, mdns_settle_seconds=0.0, final_attempts=0,
            )
        return result, once.call_count, asked.call_count

    def test_the_wait_ends_once_two_listings_in_a_row_show_no_acpd(self) -> None:
        # ACPd quit: nothing the wait does brings it back, so it ends at the
        # second listing without it instead of after 200 s.
        gone = manager_state((), acpd_running=False)
        result, attempts, _reads = self._wait_with_listings(
            [manager_state(()), gone, gone], self._runtime_not_ready(),
        )
        self.assertFalse(result.ready)
        self.assertEqual(attempts, 3)
        self.assertIn(ACPD_QUIT_STEP, {step.id for step in result.steps})
        self.assertTrue(result.detail.startswith("Apple ACPd is not running; "))
        self.assertIn("managed smbd not ready", result.detail)  # what it would have waited for, as facts
        self.assertIsNone(runtime_payload_problem(result))

    def test_acpd_outranks_the_titles_verdicts(self) -> None:
        # Without ACPd the manager sees no disk, so a verdict it shows once
        # ACPd is gone says nothing.
        gone = manager_state((), acpd_running=False)
        gone_with_verdict = manager_state((DISK_PAYLOAD_PROBLEMS[0],), acpd_running=False)
        result, attempts, _reads = self._wait_with_listings([gone, gone_with_verdict], self._runtime_not_ready())
        self.assertIn(ACPD_QUIT_STEP, {step.id for step in result.steps})
        self.assertIsNone(runtime_payload_problem(result))
        self.assertEqual(attempts, 2)

    def test_a_verdict_seen_with_the_first_listing_without_acpd_waits_for_the_next(self) -> None:
        # The first listing without ACPd carries a verdict: the wait does not
        # end on it, and the next listing settles which comes first.
        gone_with_verdict = manager_state((DISK_PAYLOAD_PROBLEMS[0],), acpd_running=False)
        result, attempts, _reads = self._wait_with_listings(
            [manager_state(()), gone_with_verdict, gone_with_verdict], self._runtime_not_ready(),
        )
        self.assertIn(ACPD_QUIT_STEP, {step.id for step in result.steps})
        self.assertIsNone(runtime_payload_problem(result))
        self.assertEqual(attempts, 3)

    def test_a_verdict_stands_once_acpd_is_back(self) -> None:
        verdict = (DISK_PAYLOAD_PROBLEMS[0],)
        result, attempts, _reads = self._wait_with_listings(
            [manager_state(verdict, acpd_running=False), manager_state(verdict, acpd_running=True)],
            self._runtime_not_ready(),
        )
        self.assertIs(runtime_payload_problem(result), DISK_PAYLOAD_PROBLEMS[0])
        self.assertNotIn(ACPD_QUIT_STEP, {step.id for step in result.steps})
        self.assertEqual(attempts, 2)

    def test_a_verdict_with_acpd_running_ends_the_wait_at_once(self) -> None:
        result, attempts, _reads = self._wait_with_listings(
            [manager_state((DISK_PAYLOAD_PROBLEMS[0],), acpd_running=True)], self._runtime_not_ready(),
        )
        self.assertIs(runtime_payload_problem(result), DISK_PAYLOAD_PROBLEMS[0])
        self.assertEqual(attempts, 1)

    def test_an_acpd_missing_from_one_listing_only_does_not_end_the_wait(self) -> None:
        gone = manager_state((), acpd_running=False)
        clock = iter(range(0, 100000, 10))
        for between in (manager_state(()), manager_state((), acpd_running=None), None):
            with self.subTest(between=between), \
                    mock.patch("timecapsulesmb.device.probe.time.monotonic", side_effect=lambda: next(clock)), \
                    mock.patch("timecapsulesmb.device.probe.time.sleep"):
                result, attempts, _reads = self._wait_with_listings(
                    [gone, between, gone, between, gone, between], self._runtime_not_ready(), timeout_seconds=50,
                )
                self.assertNotIn(ACPD_QUIT_STEP, {step.id for step in result.steps})
                self.assertIn("runtime_timeout", {step.id for step in result.steps})

    def test_acpd_gone_while_samba_serves_waits_for_rsync_as_before(self) -> None:
        gone = manager_state((), acpd_running=False)
        result, _attempts, _reads = self._wait_with_listings([gone, gone, gone], self._runtime_not_ready(samba_ready=True),
                                                             timeout_seconds=1)
        self.assertNotIn(ACPD_QUIT_STEP, {step.id for step in result.steps})
        self.assertEqual(result.detail, "managed rsync process is not running")

    def test_files_the_disk_could_not_read_are_reported_only_at_the_end_of_the_wait(self) -> None:
        # payload=unreadable or rsync=unreadable: the manager reads the files
        # again within seconds, so the wait goes on through the soft window,
        # and the verdict, still there at the final checks, is the result.
        for problem in DISK_PAYLOAD_PROBLEMS:
            if not problem.replaces_disk:
                continue
            with self.subTest(problem.token):
                with mock.patch("timecapsulesmb.device.probe.read_manager_state", return_value=manager_state((problem,))):
                    with mock.patch("timecapsulesmb.device.probe.probe_managed_runtime_once_conn",
                                    return_value=self._runtime_not_ready(samba_ready=not problem.samba_off)) as runtime_once:
                        result = probe_managed_runtime_conn(
                            SshConnection("host", "pw", "-o foo"), timeout_seconds=1, poll_interval_seconds=0.0,
                            smbd_mdns_stagger_seconds=0.0, mdns_settle_seconds=0.0,
                        )
                self.assertFalse(result.ready)
                # Soft-window attempts, then the first final check, which ends it.
                self.assertGreater(runtime_once.call_count, 2)
                phases = [attempt.phase for attempt in result.attempts]
                self.assertEqual(phases[-1], "final_check")
                self.assertEqual(phases.count("final_check"), 1)
                self.assertIs(runtime_payload_problem(result), problem)
                self.assertEqual(result.detail, problem.message)

    def test_files_the_disk_reads_again_end_the_wait_ready(self) -> None:
        # One slow read at a cold start: the manager's next check reads smbd
        # whole, clears payload=unreadable and starts Samba, and the wait,
        # which went on through the verdict, ends ready.
        unreadable = next(p for p in DISK_PAYLOAD_PROBLEMS if p.token == "payload=unreadable")
        ready = ManagedRuntimeProbeResult(
            ready=True,
            detail="managed runtime is ready",
            smbd=readiness_result(True, "managed smbd ready", ("PASS:managed smbd ready",)),
            mdns=readiness_result(True, "managed mDNS registrant active", ("PASS:managed mDNS registrant active",)),
        )
        with mock.patch("timecapsulesmb.device.probe.read_manager_state", return_value=manager_state((unreadable,))):
            with mock.patch("timecapsulesmb.device.probe.probe_managed_runtime_once_conn",
                            side_effect=[self._runtime_not_ready(), self._runtime_not_ready(), ready]):
                result = probe_managed_runtime_conn(
                    SshConnection("host", "pw", "-o foo"), timeout_seconds=200, poll_interval_seconds=0.0,
                    smbd_mdns_stagger_seconds=0.0, mdns_settle_seconds=0.0,
                )
        self.assertTrue(result.ready)
        self.assertIsNone(runtime_payload_problem(result))

    def test_a_verdict_that_leaves_samba_on_waits_for_samba(self) -> None:
        # rsync=damaged while smbd is still starting (or failing): the wait
        # goes on, and a Samba that never comes up is what the result reports.
        problem = next(p for p in DISK_PAYLOAD_PROBLEMS if not p.samba_off)
        with mock.patch("timecapsulesmb.device.probe.read_manager_state", return_value=manager_state((problem,))):
            with mock.patch("timecapsulesmb.device.probe.probe_managed_runtime_once_conn",
                            return_value=self._runtime_not_ready()) as runtime_once:
                result = probe_managed_runtime_conn(
                    SshConnection("host", "pw", "-o foo"), timeout_seconds=1, poll_interval_seconds=0.0,
                    smbd_mdns_stagger_seconds=0.0, mdns_settle_seconds=0.0, final_attempts=0,
                )
        self.assertFalse(result.ready)
        self.assertGreater(runtime_once.call_count, 1)
        self.assertIsNone(runtime_payload_problem(result))

    def test_probe_managed_runtime_does_not_ask_about_the_payload_once_ready(self) -> None:
        runtime_ready = ManagedRuntimeProbeResult(
            ready=True,
            detail="managed runtime is ready",
            smbd=readiness_result(True, "managed smbd ready", ("PASS:managed smbd ready",)),
            mdns=readiness_result(True, "managed mDNS registrant active", ("PASS:managed mDNS registrant active",)),
        )
        with mock.patch("timecapsulesmb.device.probe.read_manager_state") as asked:
            with mock.patch("timecapsulesmb.device.probe.probe_managed_runtime_once_conn", return_value=runtime_ready):
                result = probe_managed_runtime_conn(SshConnection("host", "pw", "-o foo"), timeout_seconds=200)
        self.assertTrue(result.ready)
        asked.assert_not_called()
        self.assertIsNone(runtime_payload_problem(result))

    def test_verify_managed_runtime_ready_rechecks_this_macs_adisk_copy_only_once_samba_serves(self) -> None:
        # rsync alone failing leaves Samba serving, and Time Machine needs the
        # record as much as when everything is ready.
        connection = SshConnection("host", "pw", "")
        for ready, samba_ready in ((True, True), (False, True), (False, False)):
            with self.subTest(ready=ready, samba_ready=samba_ready):
                verification = ManagedRuntimeProbeResult(
                    ready=ready, detail="ready" if ready else "smbd missing",
                    smbd=readiness_result(samba_ready, "smbd", ()), mdns=readiness_result(samba_ready, "mdns", ()),
                    samba_ready=samba_ready,
                )
                callbacks = OperationCallbacks()
                with mock.patch("timecapsulesmb.services.runtime_verification.recheck_cached_adisk") as recheck:
                    try:
                        verify_managed_runtime_ready(
                            connection, callbacks=callbacks, stage="verify_runtime_reboot", timeout_seconds=240,
                            heading="Runtime verification:", failure_message="not ready",
                            probe_runtime=mock.Mock(return_value=verification), read_runtime_logs=mock.Mock(return_value={}),
                        )
                    except DeviceError:
                        self.assertFalse(ready)
                if samba_ready:
                    recheck.assert_called_once_with(connection, callbacks, stage="verify_runtime_reboot")
                else:
                    recheck.assert_not_called()

    def _recheck(self, *, system_name="Home", name="Home", answer=None, read_error=None, recheck_error=None):
        identity = RuntimeNamingIdentityProbeResult(system_name, "home", name, "home", "HOME", "ok")
        read_identity = mock.Mock(return_value=identity, side_effect=read_error)
        query = mock.Mock()
        query.return_value.recheck_cached_txt.return_value = answer
        query.return_value.recheck_cached_txt.side_effect = recheck_error
        measurements: list[tuple[str, dict[str, object]]] = []
        debug: dict[str, object] = {}
        callbacks = OperationCallbacks(record_execution_measurement=lambda kind, **f: measurements.append((kind, f)),
                                       add_debug_fields=lambda **f: debug.update(f))
        recheck_cached_adisk(SshConnection("host", "pw", ""), callbacks, stage="verify_runtime_activation",
                             read_identity=read_identity, query=query)
        (kind, fields), = measurements
        self.assertEqual(kind, "adisk_cache_recheck")
        self.assertEqual(fields["stage"], "verify_runtime_activation")
        self.assertIsInstance(fields["duration_sec"], float)
        # Everything rides on the measurement, which successful operations send too.
        self.assertEqual(debug, {})
        return fields, query

    def test_recheck_cached_adisk_asks_this_mac_to_recheck_the_devices_record(self) -> None:
        for answer, expected in (
            ((2, 2), {"result": "rechecked", "copies": 2, "rechecked": 2}),
            ((1, 0), {"result": "refused", "copies": 1, "rechecked": 0}),
            ((0, 0), {"result": "nothing_cached", "copies": 0, "rechecked": 0}),
            # No system cache this computer can re-check (Linux, or no libSystem call).
            (None, {"result": "unsupported"}),
        ):
            with self.subTest(answer=answer):
                fields, query = self._recheck(answer=answer)
                (instance,), _ = query.return_value.recheck_cached_txt.call_args
                self.assertEqual((instance.service_type, instance.name), ("_adisk._tcp.local.", "Home"))
                self.assertEqual({key: fields.get(key) for key in ("result", "copies", "rechecked")},
                                 {"copies": None, "rechecked": None, **expected})

    def test_recheck_cached_adisk_skips_a_name_apple_replaces_with_its_own(self) -> None:
        # A device name over 63 bytes gives no instance name to look up.
        fields, query = self._recheck(system_name="N" * 64, name=None)
        self.assertEqual(fields["result"], "unnamed")
        query.assert_not_called()

    def test_recheck_cached_adisk_tells_an_unread_device_name_from_a_replaced_one(self) -> None:
        # acp exits 0 when it cannot read syNm, so the probe sees no name at all.
        for system_name in (None, ""):
            with self.subTest(system_name=system_name):
                fields, query = self._recheck(system_name=system_name, name=None)
                self.assertEqual(fields["result"], "name_unreadable")
                query.assert_not_called()

    def test_recheck_cached_adisk_records_a_failure_and_never_fails_the_operation(self) -> None:
        for read_error, recheck_error, message in (
            (RuntimeError("could not read runtime naming identity: rc=255"), None, "rc=255"),
            (None, RuntimeError("Bonjour query error -65555"), "-65555"),
            (None, PermissionError("Bonjour query error -65570"), "-65570"),
        ):
            with self.subTest(message=message):
                fields, _query = self._recheck(read_error=read_error, recheck_error=recheck_error)
                self.assertEqual(fields["result"], "error")
                self.assertIn(message, fields["error"])

    def _verify_measuring(self, verification, read_start_state):
        """verify_managed_runtime_ready's acpd_after_start measurement, and its error."""
        measurements: list[tuple[str, dict[str, object]]] = []
        callbacks = OperationCallbacks(record_execution_measurement=lambda kind, **fields: measurements.append((kind, fields)))
        error = None
        try:
            verify_managed_runtime_ready(
                SshConnection("host", "pw", ""),
                callbacks=callbacks,
                stage="verify_runtime_activation",
                timeout_seconds=200,
                heading="Runtime verification:",
                failure_message="NetBSD4 activation failed.",
                probe_runtime=mock.Mock(return_value=verification),
                read_runtime_logs=mock.Mock(return_value={}),
                read_disk_errors=mock.Mock(return_value=()),
                read_start_state=read_start_state,
            )
        except DeviceError as exc:
            error = exc
        after = [fields for kind, fields in measurements if kind == "acpd_after_start"]
        self.assertEqual(len(after), 1)
        return after[0], error

    def _ready(self) -> ManagedRuntimeProbeResult:
        return ManagedRuntimeProbeResult(
            ready=True,
            detail="managed runtime ready",
            smbd=readiness_result(True, "managed smbd ready", ("PASS:managed smbd ready",)),
            mdns=readiness_result(True, "managed mDNS registrant active", ("PASS:managed mDNS registrant active",)),
        )

    def test_every_wait_records_whether_acpd_still_runs_after_the_start(self) -> None:
        # Successful starts too: telemetry counts how many starts of each kind
        # keep ACPd, at what uptime the manager started, and when it looked.
        from timecapsulesmb.device.probe import ManagerStartupAgeProbeResult

        age = ManagerStartupAgeProbeResult(30.0, "manager started 30s ago", 24, 54321, acpd_running=True,
                                           acpd_pids=(190,), clock_ms=54321)
        fields, error = self._verify_measuring(self._ready(), mock.Mock(return_value=age))
        self.assertIsNone(error)
        self.assertEqual(fields, {"stage": "verify_runtime_activation", "result": "running", "acpd_running": True,
                                  "acpd_pids": [190], "manager_started_sec": 24, "checked_at_sec": 54})

        quit_age = ManagerStartupAgeProbeResult(None, "manager started 900s ago", 24, 924000, acpd_running=False,
                                                clock_ms=924000)
        fields, error = self._verify_measuring(self._runtime_not_ready(), mock.Mock(return_value=quit_age))
        self.assertIsNotNone(error)
        self.assertEqual((fields["result"], fields["acpd_running"]), ("quit", False))

    def _verify_events(self, verification, read_start_state) -> list[tuple[str, str]]:
        """What verify_managed_runtime_ready shows and records, in order: its
        messages, and its measurements by kind."""
        events: list[tuple[str, str]] = []
        callbacks = OperationCallbacks(
            log=lambda message: events.append(("message", str(message))),
            record_execution_measurement=lambda kind, **_fields: events.append(("measurement", kind)),
        )
        verify_managed_runtime_ready(
            SshConnection("host", "pw", ""),
            callbacks=callbacks,
            stage="verify_runtime_activation",
            timeout_seconds=200,
            heading="Runtime verification:",
            failure_message="NetBSD4 activation failed.",
            probe_runtime=mock.Mock(return_value=verification),
            read_runtime_logs=mock.Mock(return_value={}),
            read_disk_errors=mock.Mock(return_value=()),
            read_start_state=read_start_state,
        )
        return events

    def test_the_acpd_read_comes_after_the_result_is_shown(self) -> None:
        # It adds an SSH command: the user sees how the start went first.
        events = self._verify_events(self._ready(), mock.Mock(return_value=self.acpd_age(True)))
        heading = events.index(("message", "Runtime verification:"))
        self.assertLess(heading, events.index(("measurement", "acpd_after_start")))

    @staticmethod
    def acpd_age(running: bool | None):
        from timecapsulesmb.device.probe import ManagerStartupAgeProbeResult

        return ManagerStartupAgeProbeResult(30.0, "manager started 30s ago", 24, 54321, acpd_running=running,
                                            acpd_pids=(190,) if running else (), clock_ms=54321)

    def test_a_start_that_serves_after_acpd_quit_says_what_no_longer_works(self) -> None:
        # Samba started before ACPd quit: the start succeeds, and the user
        # learns now what the next install's check of ACPd would refuse.
        from timecapsulesmb.device.acpd import ACPD_QUIT_SERVING_NOTE

        for running, warned in ((False, True), (True, False), (None, False)):
            with self.subTest(acpd_running=running):
                events = self._verify_events(self._ready(), mock.Mock(return_value=self.acpd_age(running)))
                warnings = [text for kind, text in events if kind == "message" and ACPD_QUIT_SERVING_NOTE.strip() in text]
                self.assertEqual(len(warnings), int(warned))
                if warned:
                    self.assertTrue(warnings[0].startswith(
                        "Apple ACPd is broken: the AirPort service on this device has quit"))
                    self.assertLess(events.index(("measurement", "acpd_after_start")),
                                    events.index(("message", warnings[0])))

    def test_one_reading_without_acpd_counts_only_when_a_second_one_agrees(self) -> None:
        # One listing can fall between two ACPds: the second, a pause later,
        # decides, and only a confirmed quit warns.
        from timecapsulesmb.device import acpd
        from timecapsulesmb.device.acpd import ACPD_QUIT_SERVING_NOTE

        gone, back, unread = self.acpd_age(False), self.acpd_age(True), self.acpd_age(None)
        cases = (
            ("still gone", [gone, gone], "quit", True),
            ("back", [gone, back], "running", False),
            ("second unreadable", [gone, unread], "unreadable", False),
        )
        for name, readings, result, warned in cases:
            with self.subTest(name), mock.patch("timecapsulesmb.device.acpd.pause") as pause:
                read = mock.Mock(side_effect=readings)
                fields, error = self._verify_measuring(self._ready(), read)
                events = self._verify_events(self._ready(), mock.Mock(side_effect=readings))
                self.assertIsNone(error)
                self.assertEqual(read.call_count, 2)
                self.assertEqual(pause.call_args_list[0], mock.call(acpd.ACPD_RECHECK_SECONDS))
                self.assertEqual(fields["result"], result)
                self.assertTrue(fields["rechecked"])
                warnings = [text for kind, text in events if kind == "message" and ACPD_QUIT_SERVING_NOTE.strip() in text]
                self.assertEqual(len(warnings), int(warned))

        # A second read that fails is unreadable; the start still succeeded.
        fields, error = self._verify_measuring(self._ready(), mock.Mock(side_effect=[gone, SshCommandTimeout("ps")]))
        self.assertIsNone(error)
        self.assertEqual(fields["result"], "unreadable")

        # A reading with ACPd is not read again.
        read = mock.Mock(return_value=back)
        fields, _error = self._verify_measuring(self._ready(), read)
        read.assert_called_once()
        self.assertNotIn("rechecked", fields)

    def test_a_reading_that_cannot_say_is_unreadable_and_changes_nothing(self) -> None:
        from timecapsulesmb.device.probe import ManagerStartupAgeProbeResult

        cut_short = ManagerStartupAgeProbeResult(None, "device monotonic clock unavailable", 24, acpd_running=None)
        fields, error = self._verify_measuring(self._ready(), mock.Mock(return_value=cut_short))
        self.assertIsNone(error)
        self.assertEqual(fields, {"stage": "verify_runtime_activation", "result": "unreadable", "acpd_running": None,
                                  "acpd_pids": [], "manager_started_sec": 24, "checked_at_sec": None})

        fields, error = self._verify_measuring(self._ready(), mock.Mock(side_effect=SshCommandTimeout("ps")))
        self.assertIsNone(error)  # the read is best effort: the start still succeeded
        self.assertEqual(fields["result"], "unreadable")
        self.assertIn("ps", fields["error"])

    def test_a_wait_ended_by_acpd_raises_acpd_quit_from_the_real_wait(self) -> None:
        # The whole path: the wait's listings show no ACPd twice, it ends, and
        # the verification's error is ACPd's, not a 200 s timeout.
        gone = manager_state((), acpd_running=False)
        with mock.patch("timecapsulesmb.device.probe.read_manager_state", return_value=gone), \
                mock.patch("timecapsulesmb.device.probe.probe_managed_runtime_once_conn",
                           return_value=self._runtime_not_ready()) as once, \
                mock.patch("timecapsulesmb.device.probe.time.sleep"):
            with self.assertRaises(AcpdQuitError) as raised:
                verify_managed_runtime_ready(
                    SshConnection("host", "pw", ""),
                    callbacks=OperationCallbacks(),
                    stage="verify_runtime_activation",
                    timeout_seconds=200,
                    heading="Runtime verification:",
                    failure_message="NetBSD4 activation failed.",
                    read_runtime_logs=mock.Mock(return_value={}),
                    read_disk_errors=mock.Mock(return_value=()),
                )
        self.assertEqual(once.call_count, 2)
        self.assertEqual(raised.exception.code, "acpd_quit")
        self.assertTrue(raised.exception.samba_off)
        self.assertEqual(
            raised.exception.message,
            "NetBSD4 activation failed. Apple ACPd is broken: the AirPort service on this device has quit, "
            "a bug in Apple's firmware. File sharing stays off until the device restarts.",
        )
        self.assertIn("managed smbd not ready", raised.exception.detail)

    def test_an_acpd_quit_error_lists_only_the_checks_that_failed(self) -> None:
        verification = replace(
            self._runtime_not_ready(),
            extra_steps=(
                ProbeStepResult(id="service_binary", status="pass", detail="native service binary is executable"),
                ProbeStepResult(id="smbd_parent", status="fail", detail="managed smbd parent process is not running"),
                ProbeStepResult(id=ACPD_QUIT_STEP, status="fail", detail="Apple ACPd is not running"),
            ),
        )
        with self.assertRaises(AcpdQuitError) as raised:
            verify_managed_runtime_ready(
                SshConnection("host", "pw", ""),
                callbacks=OperationCallbacks(),
                stage="verify_runtime_activation",
                timeout_seconds=200,
                heading="Runtime verification:",
                failure_message="NetBSD4 activation failed.",
                probe_runtime=mock.Mock(return_value=verification),
                read_runtime_logs=mock.Mock(return_value={}),
                read_disk_errors=mock.Mock(return_value=()),
            )
        detail = raised.exception.detail
        self.assertIn("managed smbd parent process is not running", detail)
        self.assertIn("Apple ACPd is not running", detail)
        self.assertNotIn("is executable", detail)

    def test_this_boots_disk_errors_come_before_an_acpd_that_quit(self) -> None:
        verification = replace(
            self._runtime_not_ready(),
            extra_steps=(ProbeStepResult(id=ACPD_QUIT_STEP, status="fail", detail="Apple ACPd is not running"),),
        )
        errors = ("sd0(mv_sata_integ0:0:0:0): error reading fsbn 2048",)
        for disk_errors, expected in ((errors, DiskFailingError), ((), AcpdQuitError), (None, AcpdQuitError)):
            with self.subTest(disk_errors=disk_errors):
                with self.assertRaises(expected):
                    verify_managed_runtime_ready(
                        SshConnection("host", "pw", ""),
                        callbacks=OperationCallbacks(),
                        stage="verify_runtime_activation",
                        timeout_seconds=200,
                        heading="Runtime verification:",
                        failure_message="NetBSD4 activation failed.",
                        probe_runtime=mock.Mock(return_value=verification),
                        read_runtime_logs=mock.Mock(return_value={}),
                        read_disk_errors=mock.Mock(return_value=disk_errors),
                    )

    def test_verify_managed_runtime_ready_raises_each_verdicts_own_code_and_no_other(self) -> None:
        def verify(verification: ManagedRuntimeProbeResult) -> None:
            verify_managed_runtime_ready(
                SshConnection("host", "pw", ""),
                callbacks=OperationCallbacks(),
                stage="verify_runtime_activation",
                timeout_seconds=200,
                heading="Runtime verification:",
                failure_message="NetBSD4 activation failed.",
                probe_runtime=mock.Mock(return_value=verification),
                read_runtime_logs=mock.Mock(return_value={}),
            )

        self.assertEqual([problem.code for problem in DISK_PAYLOAD_PROBLEMS],
                         ["payload_missing_from_disk", "payload_damaged_on_disk", "payload_unreadable_on_disk",
                          "rsync_missing_from_disk", "rsync_damaged_on_disk", "rsync_unreadable_on_disk"])
        for problem in DISK_PAYLOAD_PROBLEMS:
            with self.subTest(problem.token):
                with self.assertRaises(problem.error_type) as raised:
                    verify(self._payload_problem_runtime(problem))
                self.assertEqual(raised.exception.code, problem.code)
                # The caller's failure line says Samba did not start, which a
                # verdict that leaves Samba on would contradict.
                expected = f"NetBSD4 activation failed. {problem.error}" if problem.samba_off else problem.error
                self.assertEqual(str(raised.exception), expected)
                self.assertEqual(raised.exception.samba_off, problem.samba_off)

        rsync = next(p for p in DISK_PAYLOAD_PROBLEMS if not p.samba_off)
        self.assertTrue(rsync.error.startswith("File sharing and Time Machine are running, but the rsync file"))

        with self.assertRaises(DeviceError) as raised:
            verify(self._runtime_not_ready())
        self.assertNotIsInstance(raised.exception, (DiskPayloadError, RsyncNotRunningError))

        # Samba serves and rsync is off with no verdict saying why: an error
        # of its own, which does not say file sharing failed, and sends the
        # user to Checkup, which says why, rather than to a reinstall.
        with self.assertRaises(RsyncNotRunningError) as raised:
            verify(self._runtime_not_ready(samba_ready=True))
        self.assertEqual(raised.exception.code, "rsync_not_running")
        self.assertFalse(raised.exception.samba_off)
        self.assertEqual(raised.exception.message, "File sharing and Time Machine are running, but rsync is not.")
        self.assertEqual(raised.exception.detail, "managed rsync process is not running")
        self.assertEqual(cli_steps("deploy", raised.exception.code)[0], "Run `tcapsule doctor` for details.")

    def test_probe_managed_runtime_waits_for_rsync_as_long_as_for_samba(self) -> None:
        # No verdict says why rsync is off (a configuration without a path
        # line): rsync gets the whole window, final checks included, as smbd
        # would; then the result says only rsync failed, not a timeout.
        runtime = self._runtime_not_ready(samba_ready=True)
        clock = iter(range(0, 100000, 10))
        with mock.patch("timecapsulesmb.device.probe.read_manager_state", return_value=manager_state(())), \
                mock.patch("timecapsulesmb.device.probe.probe_managed_runtime_once_conn", return_value=runtime), \
                mock.patch("timecapsulesmb.device.probe.time.monotonic", side_effect=lambda: next(clock)), \
                mock.patch("timecapsulesmb.device.probe.time.sleep"):
            result = probe_managed_runtime_conn(
                SshConnection("host", "pw", "-o foo"), timeout_seconds=200, poll_interval_seconds=0.0,
                smbd_mdns_stagger_seconds=0.0, mdns_settle_seconds=0.0, final_attempts=2,
            )
        self.assertFalse(result.ready)
        self.assertTrue(result.samba_ready)
        self.assertEqual([a.phase for a in result.attempts][-3:], ["soft_window", "final_check", "final_check"])
        self.assertEqual((result.steps[-1].id, result.steps[-1].status), (RSYNC_NOT_RUNNING_STEP, "fail"))
        self.assertEqual(result.detail, "managed rsync process is not running")
        self.assertNotIn("runtime_timeout", {step.id for step in result.steps})

    def _final_check_runtime(self, runtimes, verdicts) -> ManagedRuntimeProbeResult:
        """A verification whose soft window has one attempt and whose final
        checks have the rest: the clock moves 100 s at each reading."""
        clock = iter(range(0, 100000, 100))
        with mock.patch("timecapsulesmb.device.probe.read_manager_state", side_effect=[manager_state(v) for v in verdicts]), \
                mock.patch("timecapsulesmb.device.probe.probe_managed_runtime_once_conn", side_effect=runtimes), \
                mock.patch("timecapsulesmb.device.probe.time.monotonic", side_effect=lambda: next(clock)), \
                mock.patch("timecapsulesmb.device.probe.time.sleep"):
            return probe_managed_runtime_conn(
                SshConnection("host", "pw", "-o foo"), timeout_seconds=200, poll_interval_seconds=0.0,
                smbd_mdns_stagger_seconds=0.0, mdns_settle_seconds=0.0, final_attempts=2,
            )

    def test_final_checks_end_at_verdicts_as_the_soft_window_does(self) -> None:
        # rsync=damaged while smbd still starts, then Samba serves in a final
        # check whose read of the title fails: still rsync's damage, not an
        # rsync that merely did not start.
        rsync = next(p for p in DISK_PAYLOAD_PROBLEMS if not p.samba_off)
        result = self._final_check_runtime(
            [self._runtime_not_ready(), self._runtime_not_ready(samba_ready=True)], [(rsync,), None])
        self.assertEqual([a.phase for a in result.attempts], ["soft_window", "final_check"])
        self.assertIs(runtime_payload_problem(result), rsync)
        self.assertNotIn(RSYNC_NOT_RUNNING_STEP, {step.id for step in result.steps})
        # Damage first seen in a final check ends the wait as in the soft
        # window, not as a timeout, so Activate does not start the runtime.
        damaged = next(p for p in DISK_PAYLOAD_PROBLEMS if p.token == "payload=damaged")
        result = self._final_check_runtime([self._runtime_not_ready()] * 2, [(), (damaged,)])
        self.assertIs(runtime_payload_problem(result), damaged)
        self.assertNotIn("runtime_timeout", {step.id for step in result.steps})

    def test_probe_managed_runtime_reports_rsync_alone_at_its_deadline(self) -> None:
        # A wait shorter than rsync's start (activation's first look): at its
        # deadline Samba serves, so the result is rsync's, not a timeout.
        with mock.patch("timecapsulesmb.device.probe.read_manager_state", return_value=manager_state(None)), \
                mock.patch("timecapsulesmb.device.probe.probe_managed_runtime_once_conn",
                           return_value=self._runtime_not_ready(samba_ready=True)):
            result = probe_managed_runtime_conn(
                SshConnection("host", "pw", "-o foo"), timeout_seconds=1, poll_interval_seconds=0.0,
                smbd_mdns_stagger_seconds=0.0, mdns_settle_seconds=0.0, final_attempts=0,
            )
        self.assertFalse(result.ready)
        self.assertEqual(result.steps[-1].id, RSYNC_NOT_RUNNING_STEP)
        self.assertNotIn("runtime_timeout", {step.id for step in result.steps})

    def test_activate_refuses_before_starting_when_the_manager_reports_a_payload_verdict(self) -> None:
        # The manager already runs and found no payload (the disk was erased
        # while it ran), a damaged one or a damaged rsync: starting it again
        # cannot help, only a reinstall.
        for problem in DISK_PAYLOAD_PROBLEMS:
            with self.subTest(problem.token):
                messages: list[str] = []
                callbacks = OperationCallbacks(log=messages.append)
                with mock.patch("timecapsulesmb.services.activation.require_compatible_install"):
                    with mock.patch("timecapsulesmb.services.activation.probe_managed_runtime_conn",
                                    return_value=self._payload_problem_runtime(problem)):
                        with mock.patch("timecapsulesmb.services.activation.run_activation_actions_and_verify") as run_actions:
                            with self.assertRaises(problem.error_type) as raised:
                                activate_runtime(SshConnection("host", "pw", ""), [], callbacks=callbacks)
                run_actions.assert_not_called()
                self.assertEqual(raised.exception.code, problem.code)
                # One sentence, said once: the progress line is not repeated
                # by the error.
                self.assertEqual(str(raised.exception), problem.error_after("NetBSD4 activation failed."))
                self.assertNotIn(problem.message, messages)

    def test_activate_runs_when_the_runtime_is_only_not_ready(self) -> None:
        with mock.patch("timecapsulesmb.services.activation.require_compatible_install"):
            with mock.patch("timecapsulesmb.services.activation.probe_managed_runtime_conn",
                            return_value=self._runtime_not_ready()):
                with mock.patch("timecapsulesmb.services.activation.run_activation_actions_and_verify") as run_actions:
                    decision = activate_runtime(SshConnection("host", "pw", ""), [], callbacks=OperationCallbacks())
        run_actions.assert_called_once()
        self.assertTrue(decision.run_actions)

    def _activate_with_acpd(self, *, never_happened=False, answers=True, listing="", runtime=None):
        """activate_runtime on a device whose runtime is not ready, with these
        ACPd readings (device.acpd); the start itself is a mock."""
        with mock.patch("timecapsulesmb.services.activation.require_compatible_install"), \
                mock.patch("timecapsulesmb.services.activation.probe_managed_runtime_conn",
                           return_value=runtime or self._runtime_not_ready()), \
                mock.patch("timecapsulesmb.device.acpd.restart_never_happened", return_value=never_happened), \
                mock.patch("timecapsulesmb.device.acpd.acpd_answers", return_value=answers), \
                mock.patch("timecapsulesmb.device.acpd.read_processes", return_value=listing), \
                mock.patch("timecapsulesmb.services.activation.run_activation_actions_and_verify") as run_actions:
            try:
                activate_runtime(SshConnection("host", "pw", ""), [], callbacks=OperationCallbacks())
            except DeviceError as exc:
                return exc, run_actions
        return None, run_actions

    def test_activate_refuses_at_once_when_acpd_quit(self) -> None:
        # Starting the runtime would only wait out its verification.
        listing = f"  107 1 15 S 0 select 0:00.32 sshd\n{PS_LISTING_END}"
        error, run_actions = self._activate_with_acpd(listing=listing)
        run_actions.assert_not_called()
        self.assertIsInstance(error, AcpdQuitError)
        self.assertEqual(error.code, "acpd_quit")
        self.assertEqual(
            str(error),
            "NetBSD4 activation failed. Apple ACPd is broken: the AirPort service on this device has quit, a bug "
            "in Apple's firmware. File sharing stays off until the device restarts. Nothing was started.",
        )

    def test_activate_refuses_an_acpd_that_cannot_serve_the_start(self) -> None:
        for name, readings, said in (
            ("restart never happened", {"never_happened": True}, "did not restart"),
            ("syUT silent", {"answers": False}, "is stuck"),
        ):
            with self.subTest(name):
                error, run_actions = self._activate_with_acpd(**readings)
                run_actions.assert_not_called()
                self.assertEqual(error.code, "acpd_broken")
                self.assertTrue(str(error).startswith("NetBSD4 activation failed. Apple ACPd is broken: "))
                self.assertIn(said, str(error))
                self.assertTrue(str(error).endswith("Nothing was started."))

    def test_activate_says_acpd_quit_before_a_verdict_the_manager_shows_without_it(self) -> None:
        # The manager's verdict is stale without ACPd: the refusal names ACPd.
        listing = f"  107 1 15 S 0 select 0:00.32 sshd\n{PS_LISTING_END}"
        error, run_actions = self._activate_with_acpd(
            listing=listing, runtime=self._payload_problem_runtime(DISK_PAYLOAD_PROBLEMS[0]),
        )
        run_actions.assert_not_called()
        self.assertIsInstance(error, AcpdQuitError)

    def test_activate_refuses_without_reading_again_when_its_probe_found_acpd_gone(self) -> None:
        # The decision's own wait already ended on two listings without ACPd:
        # no marker, syUT or listing read, and no pause, before the refusal.
        from dataclasses import replace

        probed = self._runtime_not_ready()
        probed = replace(probed, extra_steps=probed.extra_steps + (
            ProbeStepResult(id=ACPD_QUIT_STEP, status="fail", detail="Apple ACPd is not running"),))
        debug: dict[str, object] = {}
        with mock.patch("timecapsulesmb.services.activation.require_compatible_install"), \
                mock.patch("timecapsulesmb.services.activation.probe_managed_runtime_conn", return_value=probed), \
                mock.patch("timecapsulesmb.device.acpd.acpd_problems") as problems, \
                mock.patch("timecapsulesmb.services.activation.run_activation_actions_and_verify") as run_actions:
            with self.assertRaises(AcpdQuitError) as raised:
                activate_runtime(SshConnection("host", "pw", ""), [],
                                 callbacks=OperationCallbacks(add_debug_fields=debug.update))
        problems.assert_not_called()
        run_actions.assert_not_called()
        self.assertEqual(str(raised.exception), (
            "NetBSD4 activation failed. Apple ACPd is broken: the AirPort service on this device has quit, a bug "
            "in Apple's firmware. File sharing stays off until the device restarts. Nothing was started."))
        self.assertEqual(debug["acpd_problems"], ["quit"])

    def test_activate_starts_when_the_listing_cannot_say_and_acpd_answers(self) -> None:
        error, run_actions = self._activate_with_acpd(listing="")
        self.assertIsNone(error)
        run_actions.assert_called_once()

    def test_activate_starts_beside_a_stuck_fork_of_acpd(self) -> None:
        # The fork only hangs the next restart; a start needs ACPd's local answers.
        stuck = "\n".join((
            "  119 1 2 Sl 0 kqueue 0:00.00 ACPd",
            "  352 119 2 S 1 nanoslp 0:00.00 ACPd",
            PS_LISTING_END,
        ))
        error, run_actions = self._activate_with_acpd(listing=stuck)
        self.assertIsNone(error)
        run_actions.assert_called_once()

    def test_activate_asks_nothing_of_acpd_when_the_runtime_already_serves(self) -> None:
        ready = ManagedRuntimeProbeResult(
            ready=True,
            detail="managed runtime ready",
            smbd=readiness_result(True, "managed smbd ready", ("PASS:managed smbd ready",)),
            mdns=readiness_result(True, "managed mDNS registrant active", ("PASS:managed mDNS registrant active",)),
        )
        with mock.patch("timecapsulesmb.device.acpd.acpd_problems") as problems:
            error, run_actions = self._activate_with_acpd(runtime=ready)
        self.assertIsNone(error)
        problems.assert_not_called()
        run_actions.assert_not_called()

    def test_format_deployment_plan_contains_concrete_actions(self) -> None:
        payload_dir_name = "samba4"
        payload_dir = f"/Volumes/dk2/{payload_dir_name}"
        paths = self._payload_home("/Volumes/dk2", payload_dir_name)
        plan = build_deployment_plan("root@10.0.0.2", paths, Path("bin/smbd"),  xattr_migrator_path=Path("bin/xattr-hfs-migrate"), rsync_path=Path("bin/rsync"), service_path=Path("bin/service"))
        text = format_deployment_plan(plan)
        self.assertIn("volume root: /Volumes/dk2", text)
        self.assertEqual(plan.device_path, "/dev/dk2")
        self.assertIn(f"diskd.useVolume wait: {DEFAULT_APPLE_MOUNT_WAIT_SECONDS}s per attempt", text)
        self.assertIn(render_remote_action(plan.pre_upload_actions[0]), text)
        self.assertNotIn("/usr/bin/pkill -f '[m]anager.sh'", text)
        self.assertNotIn("/usr/bin/pkill -f '[w]atchdog.sh'", text)
        self.assertIn("/usr/bin/pkill '^mdns$' >/dev/null 2>&1 || true", text)
        self.assertIn("/usr/bin/acp rpc diskd.useVolume path:s:/Volumes/dk2", text)
        self.assertIn(f"mkdir -p {payload_dir} {payload_dir}/private {payload_dir}/cache /mnt/Flash", text)
        payload_rm = next(line.strip() for line in text.splitlines() if line.strip().startswith(f"rm -rf {payload_dir}/"))
        for name in ("smb.conf.template", "private/adisk.uuid", "private/nbns.enabled"):
            self.assertIn(f" {payload_dir}/{name}", payload_rm)
        self.assertNotIn("generated smbpasswd", text)
        self.assertNotIn("generated:username.map", text)
        self.assertIn("upload generated flash runtime config -> /mnt/Flash/tcapsulesmb.conf", text)
        self.assertIn(f"checked-in rsync ({BINARY_RSYNC_SOURCE}, timeout 180s or 16 KiB/s, whichever is longer) -> {payload_dir}/rsync", text)
        self.assertIn(f"generated rsync daemon config ({GENERATED_RSYNC_CONFIG_SOURCE}, timeout 120s or 16 KiB/s, whichever is longer) -> {payload_dir}/rsyncd.conf", text)
        self.assertIn("/usr/bin/pkill '^rsync$' >/dev/null 2>&1 || true", text)
        self.assertIn("ln -s /mnt/Memory/samba4 /root/tc-netbsd4", text)
        self.assertIn("ln -s /mnt/Memory/samba4 /root/tc-netbsd4le", text)
        self.assertIn("ln -s /mnt/Memory/samba4 /root/tc-netbsd4be", text)
        self.assertIn(f"chmod 755 {payload_dir}/cache", text)
        self.assertIn(f"chmod 700 {payload_dir}/private", text)

    def test_reboot_then_activate_plan_contains_activation_actions(self) -> None:
        paths = self._payload_home("/Volumes/dk2", "samba4")
        plan = build_deployment_plan(
            "root@10.0.0.2",
            paths,
            Path("bin/smbd"),

            xattr_migrator_path=Path("bin/xattr-hfs-migrate"),
            rsync_path=Path("bin/rsync"),
            startup_mode=DEPLOY_STARTUP_REBOOT_THEN_ACTIVATE,
         service_path=Path("bin/service"))
        self.assertTrue(plan.reboot_required)
        self.assertEqual(plan.startup_mode, DEPLOY_STARTUP_REBOOT_THEN_ACTIVATE)
        self.assertEqual(
            plan.activation_actions,
            [
                RunScriptAction("/mnt/Flash/rc.local"),
            ],
        )

        text = format_deployment_plan(plan)
        self.assertIn("Remote actions (post-reboot runtime start if firmware autostart is missing):", text)
        self.assertIn("/bin/sh /mnt/Flash/rc.local", text)
        self.assertIn("mode: reboot_then_activate", text)
        self.assertIn("the device probe read /etc/rc.d/LOGIN for /mnt/Flash/rc.local before the reboot", text)
        self.assertIn("if present: wait for managed runtime", text)
        self.assertIn("if missing: run /mnt/Flash/rc.local, then wait for managed runtime", text)
        self.assertIn("managed runtime smb.conf is present", text)
        self.assertIn("smbd owns IPv4 and IPv6 wildcard TCP 445 listeners", text)
        self.assertIn("managed mDNS registrant becomes ready", text)

    def test_enabled_rsync_plan_requires_daemon_readiness(self) -> None:
        paths = self._payload_home("/Volumes/dk2", "samba4")
        plan = build_deployment_plan(
            "root@10.0.0.2",
            paths,
            Path("bin/smbd"),

            xattr_migrator_path=Path("bin/xattr-hfs-migrate"),
            rsync_path=Path("bin/rsync"),
            rsync_enabled=True,
            startup_mode=DEPLOY_STARTUP_REBOOT_THEN_VERIFY,
         service_path=Path("bin/service"))

        self.assertTrue(plan.rsync_enabled)
        self.assertIn("managed_rsync_ready", [check.id for check in plan.post_deploy_checks])
        self.assertNotIn("managed_rsync_disabled", [check.id for check in plan.post_deploy_checks])

    def test_reboot_then_activate_no_wait_plan_skips_post_reboot_activation_and_checks(self) -> None:
        paths = self._payload_home("/Volumes/dk2", "samba4")
        plan = build_deployment_plan(
            "root@10.0.0.2",
            paths,
            Path("bin/smbd"),

            xattr_migrator_path=Path("bin/xattr-hfs-migrate"),
            rsync_path=Path("bin/rsync"),
            startup_mode=DEPLOY_STARTUP_REBOOT_THEN_ACTIVATE,
            wait_after_reboot=False,
         service_path=Path("bin/service"))

        self.assertTrue(plan.reboot_required)
        self.assertFalse(plan.wait_after_reboot)
        self.assertEqual(plan.activation_actions, [])
        self.assertEqual(plan.post_deploy_checks, [])
        text = format_deployment_plan(plan)
        self.assertNotIn("Remote actions (runtime activation):", text)
        self.assertIn("action: request reboot and return without post-reboot activation or verification", text)
        self.assertIn("follow-up: return immediately after reboot request", text)
        self.assertIn("Post-deploy checks:\n  none", text)

    def test_build_uninstall_plan_stops_nbns_process(self) -> None:
        plan = build_uninstall_plan("root@10.0.0.2", ["/Volumes/dk2"], ["/Volumes/dk2/samba4"])
        rendered = [render_remote_action(action) for action in plan.remote_actions]
        self.assertTrue(any(command.startswith("/usr/bin/pkill '^nbns$' >/dev/null 2>&1 || true;") for command in rendered))
        self.assertTrue(any(command.startswith("/usr/bin/pkill '^rsync$' >/dev/null 2>&1 || true;") for command in rendered))

    def test_build_uninstall_plan_stops_supervisors_first(self) -> None:
        plan = build_uninstall_plan("root@10.0.0.2", ["/Volumes/dk2"], ["/Volumes/dk2/samba4"])
        self.assertEqual(plan.remote_actions[0], StopServiceRuntimeAction())
        first_remove = next(i for i, action in enumerate(plan.remote_actions) if isinstance(action, RemovePathsAction))
        self.assertLess(plan.remote_actions.index(StopProcessAction("smbd")), first_remove)
        self.assertLess(plan.remote_actions.index(StopProcessAction("rsync")), first_remove)
        for native in ("afpserver", "mDNSResponder"):
            self.assertNotIn(StopProcessAction(native), plan.remote_actions)

    def test_build_uninstall_plan_removes_flash_configuration(self) -> None:
        plan = build_uninstall_plan("root@10.0.0.2", ["/Volumes/dk2"], ["/Volumes/dk2/samba4"])

        self.assertEqual(plan.flash_targets["tcapsulesmb.conf"], "/mnt/Flash/tcapsulesmb.conf")
        self.assertIn("/mnt/Flash/tcapsulesmb.conf", plan.verify_absent_targets)
        self.assertIn("/mnt/Flash/tcapsulesmb.conf", removed_paths(plan.remote_actions))

    def test_build_uninstall_plan_removes_each_payload_home_once(self) -> None:
        plan = build_uninstall_plan(
            "root@10.0.0.2",
            ["/Volumes/dk2", "/Volumes/dk5", "/Volumes/dk2"],
            ["/Volumes/dk2/samba4", "/Volumes/dk5/samba4", "/Volumes/dk2/samba4"],
        )

        self.assertEqual(plan.volume_roots, ["/Volumes/dk2", "/Volumes/dk5"])
        self.assertEqual(plan.payload_dirs, ["/Volumes/dk2/samba4", "/Volumes/dk5/samba4"])
        removed = [path for action in plan.remote_actions if isinstance(action, RemovePathsAction) for path in action.paths]
        self.assertEqual(removed.count("/Volumes/dk2/samba4"), 1)
        self.assertEqual(removed.count("/Volumes/dk5/samba4"), 1)

    def test_render_remove_path_refuses_flash_root(self) -> None:
        unsafe_paths = [
            "/mnt/Flash",
            "/mnt/Flash/",
            "/mnt/Flash//",
            "/mnt/Flash stale",
            "/mnt/Flash\tstale",
        ]
        for unsafe_path in unsafe_paths:
            with self.subTest(path=unsafe_path):
                # One unsafe path refuses the whole command.
                with self.assertRaisesRegex(ValueError, "Refusing to remove flash root path"):
                    render_remote_action(RemovePathsAction(("/mnt/Flash/rc.local", unsafe_path)))

        self.assertEqual(
            render_remote_action(RemovePathsAction(("/mnt/Flash/rc.local", "/Volumes/dk2/Time Capsule/smbd"))),
            "rm -rf /mnt/Flash/rc.local '/Volumes/dk2/Time Capsule/smbd'",
        )

    def test_remote_action_rendering_quotes_payload_paths_with_spaces(self) -> None:
        payload_dir = "/Volumes/dk2/Time Capsule Samba 4"
        prepare_cmd = render_remote_action(
            PrepareDirsAction(
                (payload_dir, f"{payload_dir}/private", f"{payload_dir}/cache"),
                (RemoteSymlink("/root/tc netbsd4", "/mnt/Memory/samba4"),),
            )
        )
        permissions_cmd = render_remote_action(
            InstallPermissionsAction(
                (
                    RemotePermission(f"{payload_dir}/cache", "755"),
                    RemotePermission(f"{payload_dir}/nbns-advertiser", "755"),
                    RemotePermission(f"{payload_dir}/private", "700"),
                )
            )
        )
        self.assertIn("'/Volumes/dk2/Time Capsule Samba 4'", prepare_cmd)
        self.assertIn("'/Volumes/dk2/Time Capsule Samba 4/private'", prepare_cmd)
        self.assertIn("'/Volumes/dk2/Time Capsule Samba 4/cache'", prepare_cmd)
        self.assertIn("'/root/tc netbsd4'", prepare_cmd)
        self.assertNotIn("'/Volumes/dk2/Time Capsule Samba 4/libexec'", prepare_cmd)
        self.assertNotIn("'/Volumes/dk2/Time Capsule Samba 4/libexec", permissions_cmd)
        self.assertIn("'/Volumes/dk2/Time Capsule Samba 4/cache'", permissions_cmd)
        self.assertIn("'/Volumes/dk2/Time Capsule Samba 4/nbns-advertiser'", permissions_cmd)
        self.assertIn("'/Volumes/dk2/Time Capsule Samba 4/private'", permissions_cmd)
        self.assertNotIn("'/Volumes/dk2/Time Capsule Samba 4/private/smbpasswd'", permissions_cmd)
        self.assertNotIn("if [ -e ", permissions_cmd)
        self.assertNotIn("|| chmod 600", permissions_cmd)
        self.assertNotIn("|| true", permissions_cmd)
        self.assertEqual(render_remote_action(RunScriptAction("/mnt/Flash/rc.local")), "/bin/sh /mnt/Flash/rc.local")
        self.assertEqual(
            render_remote_action(RunScriptAction("/mnt/Flash/Time Capsule SMB/rc.local")),
            "/bin/sh '/mnt/Flash/Time Capsule SMB/rc.local'",
        )

    def test_remote_action_json_preserves_dry_run_shape(self) -> None:
        self.assertEqual(
            remote_action_to_jsonable(StopProcessAction("smbd")),
            {"kind": "stop_process", "args": ["smbd"]},
        )
        self.assertEqual(
            remote_action_to_jsonable(StopServiceRuntimeAction()),
            {"kind": "stop_service_runtime", "args": []},
        )
        self.assertEqual(
            remote_action_to_jsonable(EnsureVolumeMountedAction("/Volumes/dk2", "/dev/dk2", 30)),
            {
                "kind": "ensure_volume_mounted",
                "volume_root": "/Volumes/dk2",
                "device_path": "/dev/dk2",
                "wait_seconds": 30,
            },
        )

    def test_render_remote_action_rejects_unknown_action_object(self) -> None:
        with self.assertRaises(TypeError):
            render_remote_action(object())  # type: ignore[arg-type]

    def test_deployment_plan_uses_install_permissions_action(self) -> None:
        paths = self._payload_home("/Volumes/dk2", "Time Capsule Samba 4")
        plan = build_deployment_plan("host", paths, Path("bin/smbd"),  xattr_migrator_path=Path("bin/xattr-hfs-migrate"), rsync_path=Path("bin/rsync"), service_path=Path("bin/service"))
        self.assertEqual(plan.post_upload_actions[0], EnsureVolumeMountedAction("/Volumes/dk2", "/dev/dk2", DEFAULT_APPLE_MOUNT_WAIT_SECONDS))
        self.assertIn(InstallPermissionsAction(tuple(plan.permissions)), plan.post_upload_actions)

    def test_deployment_plan_guards_each_payload_write_action(self) -> None:
        paths = self._payload_home("/Volumes/dk2", "samba4")
        plan = build_deployment_plan("host", paths, Path("bin/smbd"),  xattr_migrator_path=Path("bin/xattr-hfs-migrate"), rsync_path=Path("bin/rsync"), service_path=Path("bin/service"))
        expected_guard = EnsureVolumeMountedAction("/Volumes/dk2", "/dev/dk2", DEFAULT_APPLE_MOUNT_WAIT_SECONDS)

        for index, action in enumerate(plan.replace_software_actions):
            if isinstance(action, RemovePathsAction) and any(path.startswith("/Volumes/") for path in action.paths):
                self.assertEqual(plan.replace_software_actions[index - 1], expected_guard)
                # The guard covers one volume: no flash path shares its command.
                self.assertTrue(all(path.startswith(plan.payload_dir + "/") for path in action.paths))
        prepare = next(index for index, action in enumerate(plan.pre_upload_actions)
                       if isinstance(action, PrepareDirsAction))
        self.assertEqual(plan.pre_upload_actions[prepare - 1], expected_guard)
        self.assertEqual(plan.post_upload_actions[0], expected_guard)
        for protocol in ("mdns", "nbns"):
            self.assertIn(f"{plan.payload_dir}/{protocol}", removed_paths(plan.replace_software_actions))
            self.assertIn(StopProcessAction(protocol), plan.pre_upload_actions)
            self.assertIn(StopProcessAction(protocol + "-advertiser"), plan.pre_upload_actions)
        self.assertIn(f"{plan.payload_dir}/discoveryd", removed_paths(plan.replace_software_actions))
        self.assertIn(StopProcessAction("discoveryd"), plan.pre_upload_actions)
        self.assertIn(StopProcessAction("wcifsnd"), plan.pre_upload_actions)
        self.assertIn("/mnt/Flash/mdns", removed_paths(plan.replace_software_actions))
        self.assertIn("/mnt/Flash/mdns-advertiser", removed_paths(plan.replace_software_actions))

    def test_deployment_plan_marks_uploaded_payload_binaries_executable(self) -> None:
        paths = self._payload_home("/Volumes/dk2", "samba4")
        plan = build_deployment_plan("host", paths, Path("bin/smbd"),  xattr_migrator_path=Path("bin/xattr-hfs-migrate"), rsync_path=Path("bin/rsync"), service_path=Path("bin/service"))
        executable_permissions = {permission.path for permission in plan.permissions if permission.mode == "755"}

        self.assertIn("/Volumes/dk2/samba4/smbd", executable_permissions)
        self.assertIn("/mnt/Flash/service", executable_permissions)
        self.assertNotIn("/Volumes/dk2/samba4/service", executable_permissions)

    def test_remote_uninstall_payload_runs_actions_sequentially(self) -> None:
        plan = build_uninstall_plan("root@10.0.0.2", ["/Volumes/dk2"], ["/Volumes/dk2/samba4"])
        expected = [render_remote_action(action) for action in plan.remote_actions]
        connection = SshConnection("host", "pw", "-o foo")
        with mock.patch("timecapsulesmb.deploy.executor.run_ssh") as run_ssh_mock:
            remote_uninstall_payload(connection, plan)
        self.assertEqual([call.args[1] for call in run_ssh_mock.call_args_list], expected)

    def test_render_process_present_ignores_zombies_for_name_and_full_matches(self) -> None:
        def process_present(command: str, *, rows: list[str]) -> bool:
            with tempfile.TemporaryDirectory() as tmp:
                fake = FakePs(Path(tmp), [process(row) for row in rows])
                result = subprocess.run(["/bin/sh", "-c", fake.install(command)], check=False, text=True, capture_output=True)
            self.assertEqual(result.stderr, "")
            return result.returncode == 0

        self.assertFalse(process_present(render_process_present_by_ucomm("wcifsnd"), rows=["9 Z wcifsnd (wcifsnd)"]))
        self.assertTrue(process_present(render_process_present_by_ucomm("wcifsnd"), rows=["9 S wcifsnd wcifsnd"]))

    def test_render_process_present_rejects_generic_full_substring_matches(self) -> None:
        with self.assertRaises(ValueError):
            render_remote_action(StopProcessAction("smbd;rm"))

    def test_render_stop_process_action_waits_for_exit(self) -> None:
        # TERM first, KILL only for a process that outlives it, and a failure
        # only for one that outlives both. Zombies and other names are gone.
        for survives, signals, stopped in (
            ((), [["-TERM", "9"]], True),
            (("TERM",), [["-TERM", "9"], ["-KILL", "9"]], True),
            (("TERM", "KILL"), [["-TERM", "9"], ["-KILL", "9"]], False),
        ):
            with self.subTest(survives=survives), tempfile.TemporaryDirectory() as tmp:
                fake = FakePs(Path(tmp), [
                    process("9 S mdns /mnt/Flash/mdns", survives=survives),
                    process("10 Z mdns (mdns)", survives=("TERM", "KILL")),
                    process("11 S mdnsx /x", survives=("TERM", "KILL")),
                ])
                command = fake.install(render_remote_action(StopProcessAction("mdns"))).replace("sleep 1", ":")
                result = subprocess.run(["/bin/sh", "-c", command], check=False, text=True, capture_output=True)
                self.assertEqual(result.returncode == 0, stopped, result.stderr)
                self.assertEqual([signal for signal in fake.signals if signal[1] == "9"], signals)
                self.assertEqual(fake.command_reads(), [])
                if not stopped:
                    self.assertEqual(result.stderr, "process mdns did not stop\n")

    def test_render_stop_process_action_kills_and_fails_if_still_running(self) -> None:
        command = render_remote_action(StopProcessAction("smbd"))
        self.assertIn("/usr/bin/pkill '^smbd$' >/dev/null 2>&1 || true;", command)
        self.assertIn('if [ "$attempt" -ge 5 ]; then break; fi;', command)
        self.assertIn("/usr/bin/pkill -9 '^smbd$' >/dev/null 2>&1 || true;", command)
        self.assertIn("echo 'process smbd did not stop' >&2; exit 1", command)

class NoDiskErrorTests(unittest.TestCase):
    """An empty MaSt means different things on a Time Capsule and an Extreme."""

    EMPTY_MAST = MaStDiscoveryResult((), 10, "MaSt=<plist><array/></plist>")

    def compatibility(self, syaps: tuple[str, ...] = (), models: tuple[str, ...] = ()) -> DeviceCompatibility:
        return DeviceCompatibility(
            os_name="NetBSD", os_release="6.0", arch="earmv4", elf_endianness="little",
            payload_family="netbsd6_samba4", device_generation="gen5", supported=True,
            reason_code="supported_netbsd6", syap_candidates=syaps, model_candidates=models,
        )

    def test_extreme_models_have_no_internal_disk(self) -> None:
        self.assertTrue(device_has_no_internal_disk(self.compatibility(("120",))))
        self.assertTrue(device_has_no_internal_disk(self.compatibility(("114", "117"))))
        self.assertTrue(device_has_no_internal_disk(self.compatibility(models=("AirPort5,117",))))

    def test_time_capsules_mixed_and_unknown_models_keep_the_internal_disk_message(self) -> None:
        self.assertFalse(device_has_no_internal_disk(self.compatibility(("119",))))
        # A NetBSD 4 LE probe that cannot tell a Time Capsule from an Extreme.
        self.assertFalse(device_has_no_internal_disk(self.compatibility(("116", "117"))))
        self.assertFalse(device_has_no_internal_disk(self.compatibility(("999",))))
        self.assertFalse(device_has_no_internal_disk(self.compatibility()))
        self.assertFalse(device_has_no_internal_disk(None))

    def test_empty_mast_on_an_extreme_asks_for_a_usb_disk(self) -> None:
        error = mast_no_volume_error(self.EMPTY_MAST, no_internal_disk=True)

        self.assertEqual(error.code, "deploy_no_usb_disk_detected")
        self.assertTrue(str(error).startswith("No USB disk was detected."))
        self.assertEqual(error.detail, "After 10 MaSt queries spaced 3 seconds apart.")

    def test_empty_mast_on_a_time_capsule_still_reports_the_internal_disk(self) -> None:
        error = mast_no_volume_error(self.EMPTY_MAST)

        self.assertEqual(error.code, "deploy_no_disk_detected")
        self.assertIn("No internal disk was detected", str(error))

    def test_an_unformatted_usb_disk_on_an_extreme_is_a_partition_problem(self) -> None:
        raw = plistlib.dumps([{"deviceName": "sd0", "name": "USB HDD", "builtin": False, "partitions": []}]).decode()
        error = mast_no_volume_error(MaStDiscoveryResult((), 1, raw), no_internal_disk=True)

        self.assertEqual(error.code, "deploy_no_hfs_partition")



if __name__ == "__main__":
    unittest.main()
