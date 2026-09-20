from __future__ import annotations

import shutil
import shlex
import os
import subprocess
import sys
import selectors
import tempfile
import textwrap
import time
import unittest
import io
from dataclasses import replace
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


from tests.native.cases import native_case_source, compile_case
from tests.native.build import compile_native

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from timecapsulesmb.deploy.commands import (
    EnsureVolumeMountedAction,
    InstallPermissionsAction,
    PrepareDirsAction,
    RemotePermission,
    RemoteSymlink,
    RemovePathAction,
    RunScriptAction,
    StopManagerAction,
    StopServiceRuntimeAction,
    StopProcessAction,
    StopWatchdogAction,
    remote_action_to_jsonable,
    render_remote_action,
)
from timecapsulesmb.deploy.dry_run import format_deployment_plan
from timecapsulesmb.deploy.executor import (
    DETACHED_SHUTDOWN_REBOOT_COMMAND,
    FLUSH_REMOTE_FILESYSTEMS_COMMAND,
    FLUSH_REMOTE_FILESYSTEMS_TIMEOUT_SECONDS,
    REBOOT_REQUEST_TIMEOUT_SECONDS,
    XattrMigrationResult,
    flush_remote_filesystem_writes,
    migrate_xattr_tdb_to_hfs,
    remote_request_reboot,
    run_remote_actions,
    remote_uninstall_payload,
    upload_deployment_payload,
)
from timecapsulesmb.deploy.planner import (
    BINARY_SERVICE_SOURCE,
    BINARY_RSYNC_SOURCE,
    BINARY_SMBD_SOURCE,
    BINARY_XATTR_MIGRATOR_SOURCE,
    DEFAULT_APPLE_MOUNT_WAIT_SECONDS,
    DEPLOY_STARTUP_REBOOT_THEN_ACTIVATE,
    DEPLOY_STARTUP_REBOOT_THEN_VERIFY,
    FLASH_TEXT_UPLOAD_TIMEOUT_SECONDS,
    GENERATED_FLASH_CONFIG_SOURCE,
    GENERATED_RSYNC_CONFIG_SOURCE,
    PACKAGED_BOOT_SOURCE,
    PACKAGED_DFREE_SH_SOURCE,
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
from timecapsulesmb.device.processes import (
    render_manager_process_present,
    render_process_present_by_ucomm,
    render_watchdog_process_present,
)
from timecapsulesmb.device.probe import (
    ElfEndiannessProbeResult,
    MDNS_BINARY_PROBE_TIMEOUT_SECONDS,
    MDNS_FSTAT_PROBE_TIMEOUT_SECONDS,
    MDNS_PROCESS_TABLE_PROBE_TIMEOUT_SECONDS,
    MDNS_SOCKET_FAMILIES_PROBE_TIMEOUT_SECONDS,
    ManagedRuntimeProbeResult,
    ProbeStepResult,
    ReadinessProbeResult,
    SMBD_STATUS_HELPERS,
    RcLocalAutostartProbeResult,
    derive_runtime_naming_identity,
    extract_airport_identity_from_acp_output,
    extract_airport_identity_from_text,
    probe_remote_runtime_naming_identity_conn,
    probe_device_conn,
    probe_netbsd4_rc_local_autostart_conn,
    probe_managed_runtime_conn,
    probe_managed_runtime_once_conn,
    probe_managed_mdns_conn,
    probe_managed_rsync_conn,
    probe_managed_smbd_conn,
    probe_remote_airport_identity_conn,
    wait_for_ssh_state_conn,
)
from timecapsulesmb.device.storage import MaStVolume, PayloadHome, PayloadVerificationResult, mounted_mast_volumes_conn
from timecapsulesmb.services.activation import ActivationDecision, decide_manual_activation, decide_netbsd4_post_reboot_activation
from timecapsulesmb.services.callbacks import OperationCallbacks
from timecapsulesmb.services.deploy import (
    DeployArtifactPaths,
    DeployCompletionMessages,
    DeployDeviceError,
    DeployPayloadContext,
    DeployRuntimeConfig,
    PreparedDeployPlan,
    complete_deployment_after_upload,
    upload_and_verify_deployment_payload,
)
from timecapsulesmb.services.runtime_verification import (
    ACTIVATION_SETTLE_MESSAGE,
    ACTIVATION_SETTLE_SECONDS,
    BOOT_SETTLE_MESSAGE,
    BOOT_SETTLE_SECONDS,
)
from timecapsulesmb.transport.ssh import ScpError, SshCommandTimeout, SshConnection, SshError


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


class DeployModuleTests(unittest.TestCase):
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
    ) -> PreparedDeployPlan:
        payload_home = self._payload_home()
        plan = build_deployment_plan(
            "root@10.0.0.2",
            payload_home,
            Path("bin/smbd"),

            xattr_migrator_path=Path("bin/xattr-hfs-migrate"),
            rsync_path=Path("bin/rsync"),
            startup_mode=startup_mode,
            wait_after_reboot=wait_after_reboot,
         service_path=Path("bin/service"))
        return PreparedDeployPlan(
            payload_context=DeployPayloadContext(
                compatibility=mock.Mock(),
                payload_family=payload_family,
                is_netbsd4=is_netbsd4,
                startup_mode=startup_mode,
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

    def _extract_shell_function(self, source: str, name: str) -> str:
        marker = f"{name}()"
        start = source.index(marker)
        brace_start = source.index("{", start)
        depth = 0
        for offset, char in enumerate(source[brace_start:], start=brace_start):
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return source[start : offset + 1]
        self.fail(f"function {name} did not terminate")

    def _compile_and_run_c_helper(self, source: str, bin_name: str, args: list[str] | None = None) -> subprocess.CompletedProcess[str]:
        binary = compile_case(source)
        return subprocess.run([str(binary), *(args or [])], capture_output=True, text=True, timeout=10)

    def _compile_mdns_advertiser_binary(self, tmp: Path) -> Path:
        return compile_native("discovery", tmp / "discoveryd")

    def _run_mdns_nt_hash(self, password: bytes) -> subprocess.CompletedProcess[bytes]:
        with tempfile.TemporaryDirectory() as tmpdir:
            bin_path = compile_native("service", Path(tmpdir) / "service")
            return subprocess.run(
                [str(bin_path), "--print-nt-hash-from-stdin"],
                input=password,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )


    def _run_mdns_advertiser_until_ready_or_exit(self, bin_path: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
        proc = subprocess.Popen(
            [str(bin_path), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        stderr_chunks: list[str] = []
        deadline = time.monotonic() + 2
        selector = selectors.DefaultSelector()
        assert proc.stderr is not None
        selector.register(proc.stderr, selectors.EVENT_READ)
        try:
            while proc.poll() is None and time.monotonic() < deadline:
                events = selector.select(max(0.0, min(0.05, deadline - time.monotonic())))
                if not events:
                    continue
                assert proc.stderr is not None
                line = proc.stderr.readline()
                if line:
                    stderr_chunks.append(line)
                    if "serving summary:" in line:
                        break
        finally:
            selector.close()
        proc.terminate()
        try:
            stdout, stderr = proc.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate(timeout=2)
        stderr = "".join(stderr_chunks) + stderr
        return subprocess.CompletedProcess([str(bin_path), *args], proc.returncode, stdout, stderr)


    def test_mdns_print_nt_hash_hashes_utf8_passwords(self) -> None:
        cases = [
            (b"password", b"8846F7EAEE8FB117AD06BDD830B7586C\n"),
            ("pässwörd".encode(), b"0553152250AC01ADB4213CB9938663E4\n"),
            ("🔐password".encode(), b"CD08E0CEDBB719A7387D2F9DAE50FFA0\n"),
        ]
        for password, expected in cases:
            with self.subTest(password=password):
                result = self._run_mdns_nt_hash(password)
                self.assertEqual(result.returncode, 0, result.stderr.decode())
                self.assertEqual(result.stdout, expected)

    def test_mdns_print_nt_hash_strips_single_acp_newline(self) -> None:
        self.assertEqual(self._run_mdns_nt_hash(b"password\n").stdout, b"8846F7EAEE8FB117AD06BDD830B7586C\n")
        self.assertEqual(self._run_mdns_nt_hash(b"password\r\n").stdout, b"8846F7EAEE8FB117AD06BDD830B7586C\n")

    def test_mdns_print_nt_hash_rejects_invalid_or_empty_input(self) -> None:
        for password in (b"", b"\xff"):
            with self.subTest(password=password):
                result = self._run_mdns_nt_hash(password)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, b"")

    def test_remote_request_reboot_uses_explicit_reboot_timeout(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")
        with mock.patch("timecapsulesmb.deploy.executor.run_ssh") as run_ssh_mock:
            remote_request_reboot(connection)
        run_ssh_mock.assert_called_once_with(
            connection,
            DETACHED_SHUTDOWN_REBOOT_COMMAND,
            check=False,
            timeout=REBOOT_REQUEST_TIMEOUT_SECONDS,
        )
        self.assertIn("exec </dev/null >/dev/null 2>&1", DETACHED_SHUTDOWN_REBOOT_COMMAND)
        self.assertIn("/bin/sync; /bin/sleep 1;", DETACHED_SHUTDOWN_REBOOT_COMMAND)
        self.assertIn("/sbin/shutdown -r now", DETACHED_SHUTDOWN_REBOOT_COMMAND)
        self.assertIn("|| /sbin/reboot", DETACHED_SHUTDOWN_REBOOT_COMMAND)
        self.assertNotIn("[ -x /sbin/shutdown ]", DETACHED_SHUTDOWN_REBOOT_COMMAND)
        self.assertIn(") & exit 0", DETACHED_SHUTDOWN_REBOOT_COMMAND)

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
        actions = [StopManagerAction(), RemovePathAction("/tmp/tc-old")]
        completed = []
        with mock.patch("timecapsulesmb.deploy.executor.run_ssh") as run_ssh_mock:
            run_remote_actions(
                connection,
                actions,
                on_action_done=lambda action, index, total: completed.append((action, index, total)),
            )

        self.assertEqual(run_ssh_mock.call_count, 2)
        self.assertEqual(completed, [(actions[0], 1, 2), (actions[1], 2, 2)])

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

    def test_probe_remote_airport_identity_reads_acp_identity_on_device(self) -> None:
        proc = mock.Mock(stdout="syAP=0x00000071\nsyAM=TimeCapsule6,113\n", returncode=0)
        connection = SshConnection("host", "pw", "-o foo")
        with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=proc) as run_ssh_mock:
            result = probe_remote_airport_identity_conn(connection)
        self.assertEqual(result.model, "TimeCapsule6,113")
        self.assertEqual(result.syap, "113")
        command = run_ssh_mock.call_args.args[1]
        self.assertIn("/usr/bin/acp syAP syAM", command)
        self.assertNotIn("ACPData.bin", command)

    def test_runtime_naming_identity_derives_effective_names(self) -> None:
        result = derive_runtime_naming_identity("A.B.'s AirPort Time Capsule", "Time Capsule.local")

        self.assertEqual(result.system_name, "A.B.'s AirPort Time Capsule")
        self.assertEqual(result.hostname, "Time Capsule.local")
        self.assertEqual(result.mdns_instance_name, "A.B.'s AirPort Time Capsule")
        self.assertEqual(result.mdns_host_label, "time-capsule")
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
        self.assertIn("/bin/hostname", command)

    def test_probe_remote_runtime_naming_identity_fails_on_remote_error(self) -> None:
        proc = mock.Mock(stdout="", returncode=1)
        connection = SshConnection("host", "pw", "-o foo")
        with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=proc):
            with self.assertRaisesRegex(RuntimeError, "could not read runtime naming identity: rc=1"):
                probe_remote_runtime_naming_identity_conn(connection)

    def test_deployment_uses_one_native_runtime_and_small_boot_scripts(self) -> None:
        plan = self._prepared_deploy_plan().plan
        transfers = [*plan.uploads, plan.boot_upload]
        services = [transfer for transfer in transfers if transfer.source_id == BINARY_SERVICE_SOURCE]
        self.assertEqual([transfer.destination for transfer in services], ["/mnt/Flash/service"])
        flash_names = {Path(transfer.destination).name for transfer in transfers
                       if transfer.destination.startswith("/mnt/Flash/")}
        self.assertEqual(flash_names, {"service", "boot.sh", "rc.local", "dfree.sh", "tcapsulesmb.conf"})


    def test_mdns_advertiser_accepts_lowercase_wama_and_normalizes_output(self) -> None:
        if shutil.which("cc") is None:
            self.skipTest("cc not available")

        source = native_case_source("mdns_advertiser_accepts_lowercase_wama_and_normalizes_output")
        run = self._compile_and_run_c_helper(source, "mdns_adisk_system")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout.strip(), "sys=waMA=80:EA:96:E6:58:68,adVF=0x1010")

    def test_mdns_advertiser_adisk_disk_txt_defaults_to_cloned_advf(self) -> None:
        source = native_case_source("mdns_advertiser_adisk_disk_txt_defaults_to_cloned_advf")
        run = self._compile_and_run_c_helper(source, "mdns_adisk_disk_txt_default_advf")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(
            run.stdout.strip(),
            "dk2=adVF=0x1093,adVN=Data,adVU=12345678-1234-1234-1234-123456789012",
        )

    def test_mdns_advertiser_adisk_disk_txt_accepts_time_machine_smb_advf(self) -> None:
        source = native_case_source("mdns_advertiser_adisk_disk_txt_accepts_time_machine_smb_advf")
        run = self._compile_and_run_c_helper(source, "mdns_adisk_disk_txt_time_machine_advf")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(
            run.stdout.strip(),
            "dk2=adVF=0x82,adVN=Data,adVU=12345678-1234-1234-1234-123456789012",
        )

    def test_mdns_advertiser_rejects_extra_adisk_share_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            binary = self._compile_mdns_advertiser_binary(Path(tmpdir))
            run = subprocess.run(
                [str(binary), "--adisk-share", "Data", "dk2",
                 "12345678-1234-1234-1234-123456789012", "0x82", "extra"],
                capture_output=True, text=True, timeout=10,
            )
        self.assertEqual(run.returncode, 3, run.stderr)
        self.assertIn("Usage:", run.stderr)

    def test_mdns_advertiser_adisk_argument_validation_respects_diskless_mode(self) -> None:
        source = native_case_source("mdns_advertiser_adisk_argument_validation_respects_diskless_mode")
        adisk_uuid = "12345678-1234-1234-1234-123456789012"

        cases = [
            (
                "no_adisk_config_does_not_require_adisk_sys_wama",
                ["diskful", "-", ""],
                0,
                "",
            ),
            (
                "diskful_adisk_share_requires_adisk_sys_wama",
                ["diskful", adisk_uuid, ""],
                7,
                "",
            ),
            (
                "diskful_adisk_share_rejects_invalid_adisk_sys_wama",
                ["diskful", adisk_uuid, "not-a-mac"],
                7,
                "adisk sys waMA must be a MAC address",
            ),
            (
                "diskful_adisk_share_accepts_valid_adisk_sys_wama",
                ["diskful", adisk_uuid, "80:EA:96:E6:58:68"],
                0,
                "",
            ),
            (
                "diskless_adisk_share_suppresses_missing_adisk_sys_wama",
                ["diskless", adisk_uuid, ""],
                0,
                "",
            ),
            (
                "diskless_adisk_share_suppresses_invalid_adisk_sys_wama",
                ["diskless", adisk_uuid, "not-a-mac"],
                0,
                "",
            ),
            (
                "diskless_still_validates_configured_adisk_disk_fields",
                ["diskless", "bad", ""],
                8,
                "adisk uuid must be 36 characters",
            ),
        ]

        for label, extra_args, expected_rc, expected_stderr in cases:
            with self.subTest(label=label):
                run = self._compile_and_run_c_helper(
                    source,
                    f"mdns_adisk_args_{label}",
                    extra_args,
                )
                self.assertEqual(run.returncode, expected_rc, run.stderr)
                if expected_stderr:
                    self.assertIn(expected_stderr, run.stderr)

    def test_mdns_advertiser_unknown_option_returns_usage_without_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            bin_path = self._compile_mdns_advertiser_binary(Path(tmpdir))
            run = subprocess.run([str(bin_path), "--auto-ip"], capture_output=True, text=True, check=False)
        self.assertEqual(run.returncode, 3)
        self.assertIn("Usage:", run.stderr)
        self.assertTrue(run.stderr.splitlines())
        for line in run.stderr.splitlines():
            self.assertRegex(line, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} ")
        self.assertNotIn("serving summary", run.stderr)

    def test_mdns_advertiser_version_prints_version_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            bin_path = self._compile_mdns_advertiser_binary(Path(tmpdir))
            run = subprocess.run([str(bin_path), "--version"], capture_output=True, text=True, check=False)
        self.assertEqual(run.returncode, 0)
        self.assertEqual(run.stdout, "30100\n")
        self.assertEqual(run.stderr, "")

    def test_mdns_advertiser_accepts_debug_logging_before_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            bin_path = self._compile_mdns_advertiser_binary(Path(tmpdir))
            run = subprocess.run(
                [str(bin_path), "--debug-logging", "--version"],
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(run.returncode, 0)
        self.assertEqual(run.stdout, "30100\n")
        self.assertEqual(run.stderr, "")

    def test_mdns_timestamped_logging_truncates_long_lines_without_heap(self) -> None:
        source = native_case_source("mdns_timestamped_logging_truncates_long_lines_without_heap")
        run = self._compile_and_run_c_helper(source, "mdns_long_timestamped_log")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertNotIn("A" * 5000, run.stderr)
        self.assertGreaterEqual(run.stderr.count("A"), 4000)
        self.assertLess(run.stderr.count("A"), 5000)
        self.assertTrue(run.stderr.endswith("\n"))


    def test_discovery_rejects_removed_nbns_cli_modes(self) -> None:
        if shutil.which("cc") is None:
            self.skipTest("cc not available")

        with tempfile.TemporaryDirectory() as tmpdir:
            bin_path = self._compile_mdns_advertiser_binary(Path(tmpdir))
            runs = [
                subprocess.run(
                    [str(bin_path), "--name", "TimeCapsule", "--ipv4", "192.168.1.217"],
                    capture_output=True,
                    text=True,
                    check=False,
                ),
                subprocess.run(
                    [str(bin_path), "--name", "TimeCapsule", "--ttl", "30"],
                    capture_output=True,
                    text=True,
                    check=False,
                ),
                subprocess.run(
                    [str(bin_path), "--name", "TimeCapsule", "--auto-ip"],
                    capture_output=True,
                    text=True,
                    check=False,
                ),
                subprocess.run(
                    [str(bin_path), "--check-auto-ip"],
                    capture_output=True,
                    text=True,
                    check=False,
                ),
            ]
        for run in runs:
            self.assertEqual(run.returncode, 3)
            self.assertIn("Usage:", run.stderr)


    def test_discovery_usage_reports_native_interface(self) -> None:
        if shutil.which("cc") is None:
            self.skipTest("cc not available")

        with tempfile.TemporaryDirectory() as tmpdir:
            bin_path = self._compile_mdns_advertiser_binary(Path(tmpdir))
            run = subprocess.run([str(bin_path), "--help"], capture_output=True, text=True, check=False)

        self.assertEqual(run.returncode, 0)
        self.assertIn("Usage:", run.stderr)
        self.assertNotIn("--auto-ip", run.stderr)
        self.assertNotIn("--ipv4", run.stderr)
        self.assertNotIn("--ttl", run.stderr)
        self.assertNotIn("--check-auto-ip", run.stderr)


    def test_discovery_rejects_overlong_name_before_truncation(self) -> None:
        if shutil.which("cc") is None:
            self.skipTest("cc not available")

        with tempfile.TemporaryDirectory() as tmpdir:
            bin_path = self._compile_mdns_advertiser_binary(Path(tmpdir))
            run = subprocess.run(
                [str(bin_path), "--netbios-name", "ABCDEFGHIJKLMNOP"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(run.returncode, 3)
            self.assertIn("15 bytes or fewer", run.stderr)
            self.assertTrue(run.stderr.splitlines())
            for line in run.stderr.splitlines():
                self.assertRegex(line, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} ")

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

    def test_probe_device_skips_direct_tcp_check_for_proxy_ssh_options(self) -> None:
        with mock.patch("timecapsulesmb.device.probe.tcp_open", side_effect=AssertionError("direct TCP probe should be skipped")):
            with mock.patch("timecapsulesmb.device.probe._probe_remote_os_info_conn", return_value=("NetBSD", "4.0", "earmv4")):
                with mock.patch(
                    "timecapsulesmb.device.probe._probe_remote_elf_endianness_result_conn",
                    return_value=ElfEndiannessProbeResult("big"),
                ):
                    with mock.patch("timecapsulesmb.device.probe.probe_remote_airport_identity_conn", return_value=mock.Mock(model=None, syap=None)):
                        result = probe_device_conn(
                            SshConnection("root@192.168.1.118", "pw", "-o proxycommand=ssh\\ -W\\ %h:%p\\ bastion")
                        )
        self.assertTrue(result.ssh_port_reachable)
        self.assertTrue(result.ssh_authenticated)
        self.assertEqual(result.os_release, "4.0")
        self.assertEqual(result.elf_endianness, "big")

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
        with mock.patch("timecapsulesmb.deploy.executor.run_ssh"), mock.patch("timecapsulesmb.deploy.executor.run_scp") as scp_mock:
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

        mount_mock.assert_called_once_with(
            connection,
            "/Volumes/dk2",
            "/dev/dk2",
            wait_seconds=DEFAULT_APPLE_MOUNT_WAIT_SECONDS,
        )
        scp_mock.assert_called_once_with(
            connection,
            Path("bin/xattr-migrate/xattr-hfs-migrate"),
            "/Volumes/dk2/samba4/xattr-hfs-migrate",
            timeout=180,
        )

    def test_xattr_migration_rejects_invalid_phase_and_metadata(self) -> None:
        plan = self._prepared_deploy_plan().plan
        connection = SshConnection("host", "pw", "-o foo")

        with self.assertRaisesRegex(ValueError, "migration phase"):
            migrate_xattr_tdb_to_hfs(
                connection, plan, phase="remove", legacy_metadata="stream"
            )
        with self.assertRaisesRegex(ValueError, "metadata backend"):
            migrate_xattr_tdb_to_hfs(
                connection, plan, phase="copy", legacy_metadata="hfs"
            )

    def test_upload_and_verify_deployment_payload_records_upload_measurements(self) -> None:
        prepared_plan = self._prepared_deploy_plan()
        connection = SshConnection("host", "pw", "-o foo")
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
            DeployRuntimeConfig(nbns_enabled=True),
            callbacks=OperationCallbacks(record_execution_measurement=lambda kind, **fields: measurements.append((kind, fields))),
            run_remote_actions_func=mock.Mock(),
            upload_payload_func=fake_upload,
            migrate_xattrs_func=mock.Mock(return_value="migration=complete"),
            probe_flash_capacity_func=mock.Mock(return_value=(1_000_000, 100_000)),
            flush_remote_writes=mock.Mock(),
            verify_payload_home=mock.Mock(return_value=PayloadVerificationResult(True, "ok")),
        )

        upload_measurements = [fields for kind, fields in measurements if kind == "upload"]
        batch_measurements = [fields for kind, fields in measurements if kind == "upload_batch"]
        self.assertEqual(
            [fields["source_id"] for fields in upload_measurements],
            [BINARY_XATTR_MIGRATOR_SOURCE, BINARY_SMBD_SOURCE, BINARY_RSYNC_SOURCE, PACKAGED_RC_LOCAL_SOURCE],
        )
        self.assertTrue(all(fields["destination_kind"] == "payload" for fields in upload_measurements[:-1]))
        self.assertTrue(all(fields["result"] == "success" for fields in upload_measurements))
        self.assertEqual(batch_measurements[0]["file_count"], len(prepared_plan.plan.uploads))
        self.assertEqual(batch_measurements[0]["result"], "success")

    def test_migration_uses_saved_metadata_choice_unless_explicitly_overridden(self) -> None:
        for saved, override, expected in (("true", None, "netatalk"), ("false", None, "stream"),
                                          ("true", False, "stream"), ("false", True, "netatalk")):
            with self.subTest(saved=saved, override=override):
                migrate = mock.Mock(return_value="migration=complete")
                upload_and_verify_deployment_payload(
                    AppConfig.from_values({"TC_FRUIT_METADATA_NETATALK": saved}),
                    SshConnection("host", "pw", ""),
                    self._prepared_deploy_plan(),
                    DeployRuntimeConfig(nbns_enabled=True, fruit_metadata_netatalk=override),
                    run_remote_actions_func=mock.Mock(),
                    upload_payload_func=mock.Mock(),
                    migrate_xattrs_func=migrate,
                    probe_flash_capacity_func=mock.Mock(return_value=(1_000_000, 100_000)),
                    flush_remote_writes=mock.Mock(),
                    verify_payload_home=mock.Mock(return_value=PayloadVerificationResult(True, "ok")),
                )
                self.assertEqual([(c.kwargs["phase"], c.kwargs["legacy_metadata"])
                                  for c in migrate.call_args_list], [("copy", expected), ("cleanup", expected)])

    def test_migration_failure_diagnostics_distinguish_timeout_from_scan_error(self) -> None:
        from timecapsulesmb.transport.errors import SshCommandTimeout

        for error, timed_out in ((RuntimeError("opendir failed path=/Volumes/dk2/problem; errors=1"), False),
                                 (SshCommandTimeout("migration deadline exceeded"), True)):
            with self.subTest(timed_out=timed_out):
                measurements = []
                with self.assertRaises(DeployDeviceError) as caught:
                    upload_and_verify_deployment_payload(
                        AppConfig.from_values({}), SshConnection("host", "pw", ""),
                        self._prepared_deploy_plan(), DeployRuntimeConfig(nbns_enabled=True),
                        callbacks=OperationCallbacks(record_execution_measurement=lambda kind, **fields: measurements.append((kind, fields))),
                        run_remote_actions_func=mock.Mock(), upload_payload_func=mock.Mock(),
                        migrate_xattrs_func=mock.Mock(side_effect=error),
                        probe_flash_capacity_func=mock.Mock(return_value=(1_000_000, 100_000)),
                    )
                message = str(caught.exception)
                self.assertIn("phase=copy elapsed_seconds=", message)
                self.assertIn("timeout_seconds=21600", message)
                self.assertIn(f"timed_out={str(timed_out).lower()}", message)
                self.assertIn("xattr-migration-copy.log", message)
                self.assertIn(str(error), message)
                migration = next(fields for kind, fields in measurements if kind == "xattr_migration")
                self.assertEqual(migration["timed_out"], timed_out)
                self.assertEqual(migration["result"], "failure")

    def test_xattr_copy_precedes_payload_and_cleanup_follows_verification(self) -> None:
        prepared_plan = self._prepared_deploy_plan()
        connection = SshConnection("host", "pw", "-o foo")
        events: list[str] = []
        migrated_root = self._mast_volume()

        def migrate(_connection, _plan, *, phase, legacy_metadata, roots=None):
            events.append(
                f"migrate:{phase}:{legacy_metadata}:"
                f"{'selected' if roots == (migrated_root,) else 'discover'}"
            )
            return XattrMigrationResult(f"phase={phase}", (migrated_root,))

        def verify(*_args, **_kwargs):
            events.append("verify")
            return PayloadVerificationResult(True, "ok")

        def upload(plan, *_args, **_kwargs):
            events.append(
                "upload:migrator"
                if plan.uploads == [plan.migration_upload]
                else "upload:boot" if plan.uploads == [plan.boot_upload] else "upload:payload"
            )

        upload_and_verify_deployment_payload(
            AppConfig.from_values({}),
            connection,
            prepared_plan,
            DeployRuntimeConfig(nbns_enabled=True, fruit_metadata_netatalk=False),
            callbacks=OperationCallbacks(),
            run_remote_actions_func=mock.Mock(),
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
                "migrate:copy:stream:discover",
                "upload:payload",
                "verify",
                "verify",
                "migrate:cleanup:stream:selected",
                "upload:boot",
            ],
        )

    def test_upload_and_verify_deployment_payload_codes_manager_stop_timeout(self) -> None:
        prepared_plan = self._prepared_deploy_plan()
        connection = SshConnection("host", "pw", "-o foo")

        with self.assertRaises(DeployDeviceError) as raised:
            upload_and_verify_deployment_payload(
                AppConfig.from_values({}),
                connection,
                prepared_plan,
                DeployRuntimeConfig(nbns_enabled=True),
                callbacks=OperationCallbacks(),
                run_remote_actions_func=mock.Mock(side_effect=SshError("process manager did not stop")),
                upload_payload_func=mock.Mock(),
                migrate_xattrs_func=mock.Mock(return_value="migration=complete"),
                probe_flash_capacity_func=mock.Mock(return_value=(1_000_000, 100_000)),
            )

        self.assertEqual(raised.exception.code, "manager_stop_timeout")
        self.assertIn("A service on the device is stuck", str(raised.exception))
        self.assertIsInstance(raised.exception.__cause__, SshError)

    def test_upload_and_verify_deployment_payload_codes_payload_upload_timeout(self) -> None:
        prepared_plan = self._prepared_deploy_plan()
        connection = SshConnection("host", "pw", "-o foo")

        def timeout_upload(plan, *, connection, source_resolver, on_uploading=None, on_uploaded=None):
            if on_uploading is not None:
                on_uploading(plan.uploads[0])
            raise SshCommandTimeout("Timed out copying smbd to remote path /Volumes/dk2/.samba4/smbd via scp")

        with self.assertRaises(DeployDeviceError) as raised:
            upload_and_verify_deployment_payload(
                AppConfig.from_values({}),
                connection,
                prepared_plan,
                DeployRuntimeConfig(nbns_enabled=True),
                callbacks=OperationCallbacks(),
                run_remote_actions_func=mock.Mock(),
                upload_payload_func=timeout_upload,
                migrate_xattrs_func=mock.Mock(return_value="migration=complete"),
                probe_flash_capacity_func=mock.Mock(return_value=(1_000_000, 100_000)),
            )

        self.assertEqual(raised.exception.code, "payload_upload_timeout")
        self.assertIn("The disk did not respond while copying the SMB payload.", str(raised.exception))
        self.assertIsInstance(raised.exception.__cause__, SshCommandTimeout)

    def test_upload_deployment_payload_stops_when_payload_volume_guard_fails(self) -> None:
        paths = self._payload_home("/Volumes/dk2", "samba4")
        plan = build_deployment_plan("host", paths, Path("bin/smbd"),  xattr_migrator_path=Path("bin/xattr-hfs-migrate"), rsync_path=Path("bin/rsync"), service_path=Path("bin/service"))
        connection = SshConnection("host", "pw", "-o foo")
        source_resolver = {
            BINARY_SMBD_SOURCE: Path("/tmp/smbd"),
        }
        with mock.patch("timecapsulesmb.deploy.executor.ensure_volume_root_mounted_conn", return_value=False) as mount_mock:
            with mock.patch("timecapsulesmb.deploy.executor.run_scp") as scp_mock:
                with self.assertRaisesRegex(RuntimeError, "payload volume /Volumes/dk2 is not mounted before upload"):
                    upload_deployment_payload(plan, connection=connection, source_resolver=source_resolver)

        mount_mock.assert_called_once_with(connection, "/Volumes/dk2", "/dev/dk2", wait_seconds=DEFAULT_APPLE_MOUNT_WAIT_SECONDS)
        scp_mock.assert_not_called()

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
zombie_smbd="100 1 Z 0:00.00 smbd /mnt/Memory/samba4/sbin/smbd"
live_smbd="101 1 S 0:00.00 smbd /mnt/Memory/samba4/sbin/smbd"
zombie_mdns="200 1 Z 0:00.00 discoveryd /mnt/Flash/discoveryd"
live_mdns="201 1 S 0:00.00 discoveryd /mnt/Flash/discoveryd"
zombie_apple="300 1 Z 0:00.00 mDNSResponder /usr/sbin/mDNSResponder"
live_apple="301 1 S 0:00.00 mDNSResponder /usr/sbin/mDNSResponder"
mixed_smbd=$(cat <<'EOF'
100 1 Z 0:00.00 smbd /mnt/Memory/samba4/sbin/smbd
101 1 S 0:00.00 smbd /mnt/Memory/samba4/sbin/smbd
EOF
)

smbd_parent_process_present "$zombie_smbd"; echo "zombie-smbd=$?"
smbd_parent_process_present "$live_smbd"; echo "live-smbd=$?"
mdns_process_present "$zombie_mdns"; echo "zombie-mdns=$?"
mdns_process_present "$live_mdns"; echo "live-mdns=$?"
apple_mdns_present "$zombie_apple"; echo "zombie-apple=$?"
apple_mdns_present "$live_apple"; echo "live-apple=$?"
capture_fstat_for_ucomm "$mixed_smbd" smbd
'''
        )

        result = subprocess.run(["/bin/sh", "-c", script], check=False, text=True, capture_output=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("zombie-smbd=1", result.stdout)
        self.assertIn("live-smbd=0", result.stdout)
        self.assertIn("zombie-mdns=1", result.stdout)
        self.assertIn("live-mdns=0", result.stdout)
        self.assertIn("zombie-apple=1", result.stdout)
        self.assertIn("live-apple=0", result.stdout)
        self.assertNotIn("fstat:100", result.stdout)
        self.assertIn("fstat:101", result.stdout)

    def test_probe_status_helpers_do_not_count_probe_shell_body_as_manager(self) -> None:
        script = (
            SMBD_STATUS_HELPERS
            + r'''
real_manager="203 1 S 0:00.00 sh /bin/sh /mnt/Flash/manager.sh"
self_match_manager=$(cat <<'EOF'
3308 11745 S 0:00.01 sh /bin/sh -c probe=/mnt/Flash/manager.sh
11745 11677 Ss 0:00.01 sh sh -c /bin/sh -c 'probe=/mnt/Flash/manager.sh'
EOF
)
manager_process_present_for_volume "$real_manager"; echo "manager=$?"
manager_process_present_for_volume "$self_match_manager"; echo "self=$?"
'''
        )

        result = subprocess.run(["/bin/sh", "-c", script], check=False, text=True, capture_output=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("manager=0", result.stdout)
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
                "101 1 S 0:00.00 smbd /mnt/Memory/samba4/sbin/smbd -D -s /mnt/Memory/samba4/etc/smb.conf\n"
                "202 1 S 0:00.00 sh /bin/sh /mnt/Flash/manager.sh\n"
            )
            script = f"""
RUNTIME_RAM_ROOT={shlex.quote(str(ram_root))}
RUNTIME_SMB_CONF_PATH={shlex.quote(str(smb_conf))}
RUNTIME_PERSISTENT_ROOT_PREFIX={shlex.quote(str(persistent_prefix) + "/")}
{SMBD_STATUS_HELPERS}
capture_df_for_volume_root() {{ echo "/dev/dk2 100 10 90 10% $1"; }}
ps_out={shlex.quote(ps_out)}
fstat_out='root smbd 101 10 internet stream tcp 0x0 *:445'
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
        self.assertIn("PASS:smbd bound to required TCP 445 sockets", result.stdout)
        self.assertIn("PASS:device Samba version: 4.24.3", result.stdout)
        self.assertIn("status=0", result.stdout)

    def test_smbd_status_helper_requires_configured_tcp_445_families(self) -> None:
        script = (
            SMBD_STATUS_HELPERS
            + r'''
ipv4='root smbd 101 10 internet stream tcp 0x0 *:445'
ipv6='root smbd 101 10 internet6 stream tcp 0x0 *:445'
both=$(cat <<'EOF'
root smbd 101 10 internet6 stream tcp 0x0 *:445
root smbd 101 11 internet stream tcp 0x0 *:445
EOF
)
smbd_bound_445 "$ipv4" ""; echo "ipv4_default=$?"
smbd_bound_445 "$ipv6" ""; echo "ipv6_default=$?"
smbd_bound_445 "$ipv6" "127.0.0.1/8 ::1/128 fdbb:1111:2222:3333::40/64"; echo "ipv6_required=$?"
smbd_bound_445 "$ipv4" "127.0.0.1/8 ::1/128 192.168.1.40/24 fdbb:1111:2222:3333::40/64"; echo "ipv4_missing_v6=$?"
smbd_bound_445 "$both" "127.0.0.1/8 ::1/128 192.168.1.40/24 fdbb:1111:2222:3333::40/64"; echo "both_required=$?"
'''
        )

        result = subprocess.run(["/bin/sh", "-c", script], check=False, text=True, capture_output=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ipv4_default=0", result.stdout)
        self.assertIn("ipv6_default=1", result.stdout)
        self.assertIn("ipv6_required=0", result.stdout)
        self.assertIn("ipv4_missing_v6=1", result.stdout)
        self.assertIn("both_required=0", result.stdout)

    def test_smbd_status_helpers_fail_for_disk_auth_unmounted_volume_and_missing_manager(self) -> None:
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
ps_out='101 1 S 0:00.00 smbd /mnt/Memory/samba4/sbin/smbd -D -s /mnt/Memory/samba4/etc/smb.conf'
fstat_out='root smbd 101 10 internet stream tcp 0x0 *:445'
if describe_managed_smbd_status "$ps_out" "$fstat_out"; then
    echo status=0
else
    echo status=$?
fi
"""

            result = subprocess.run(["/bin/sh", "-c", script], check=False, text=True, capture_output=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("FAIL:active smb.conf passdb backend is not staged in RAM", result.stdout)
        self.assertIn("FAIL:active smb.conf username map is not staged in RAM", result.stdout)
        self.assertIn("FAIL:active smb.conf xattr_tdb:file is not persistent disk storage", result.stdout)
        self.assertIn("FAIL:one or more managed share volumes are not mounted", result.stdout)
        self.assertIn("FAIL:manager is not running for managed runtime", result.stdout)
        self.assertIn("status=1", result.stdout)

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
        self.assertIn("FAIL:smbd is not bound to required TCP 445 sockets", lines)
        self.assertEqual(lines[-1], "FAIL:device Samba version unavailable (managed runtime smbd binary missing)")
        self.assertLess(
            lines.index("FAIL:smbd is not bound to required TCP 445 sockets"),
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
        self.assertIn('[ "$3" = rsync ]', remote_cmd)
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

    PS_V31 = (
        "371 1 Sa 0:00 mDNSResponder /sbin/mDNSResponder -d\n"
        "559 1 S 0:00 diskd /sbin/diskd -i lo0 -d local.\n"
        "916 408 S 0:00 service service: role=discovery nbns=ready mode=payload --netbios-name TimeCapsule --adisk-share Data dk2 12345678-1234-1234-1234-123456789012 0x82\n"
        "917 916 S 0:00 wcifsnd /sbin/wcifsnd\n"
    )
    FSTAT_V31 = (
        "root     mDNSResponder  371    5* internet dgram udp *:5353\n"
        "root     mDNSResponder  371    6* internet6 dgram udp *:5353\n"
        "root     wcifsnd  917    5* internet dgram udp *:137\n"
        "root     wcifsnd  917    6* internet dgram udp *:138\n"
    )
    PLAN_V31 = (
        "config: nbns_enabled=1 advertise_afp=0\n"
        "plan: status=validated mode=bridge stale_seconds=0 diskless=0\n"
        "acp: raNA=0 raDS=0 waNM=1 usbF=0x450 laIP=192.168.1.10 waIP=192.168.1.10 waLL=unavailable gnRo=unavailable\n"
        'identity: instance="AirPort Time Capsule" netbios=airport-time-ca wama=E8:8D:28:58:F1:5C\n'
        "link: name=bridge0 index=9 role=lan mask=smb,adisk\n"
        "addr: link=9 family=inet addr=192.168.1.10 prefix=24\n"
        "link: name=bridge1 index=10 role=isolated mask=none\n"
        "bind: 127.0.0.1/8 ::1/128 192.168.1.10/24\n"
    )

    def _run_mdns_probe(self, responses: list[object]) -> tuple[object, mock.Mock]:
        with mock.patch("timecapsulesmb.device.probe.run_ssh", side_effect=responses) as run_ssh_mock:
            result = probe_managed_mdns_conn(SshConnection("host", "pw", "-o foo"))
        return result, run_ssh_mock

    def test_probe_managed_mdns_passes_when_apple_daemon_owns_5353_and_plan_grants_smb(self) -> None:
        result, run_ssh_mock = self._run_mdns_probe([
            mock.Mock(returncode=0, stdout="/mnt/Flash/discoveryd\n", stderr=""),
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
        self.assertIn("ps axww", remote_commands[1])
        self.assertIn("/internet/p", remote_commands[2])
        self.assertIn("--print-link-plan", remote_commands[3])
        self.assertIn("PASS:Apple mDNSResponder is running", result.lines)
        self.assertIn("PASS:Apple diskd runs on loopback (-i lo0)", result.lines)
        self.assertIn("PASS:Apple mDNSResponder listens on UDP 5353 for IPv4 and IPv6", result.lines)
        self.assertIn("PASS:no other process holds UDP 5353", result.lines)
        self.assertIn("PASS:mdns link plan validated mode=bridge; SMB on bridge0(lan)", result.lines)

    def test_probe_managed_mdns_fails_when_apple_daemon_is_dead_or_diskd_is_on_the_lan(self) -> None:
        ps_out = (
            "878 1 ZWa 0:00 (mDNSResponder) (mDNSResponder)\n"
            "232 1 S 0:00 diskd /sbin/diskd -i  -d local.\n"
            "916 408 S 0:00 discoveryd /mnt/Flash/discoveryd\n"
        )
        result, run_ssh_mock = self._run_mdns_probe([
            mock.Mock(returncode=0, stdout="/mnt/Flash/discoveryd\n", stderr=""),
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
        fstat_out = self.FSTAT_V31 + "root     discoveryd 916    4* internet dgram udp *:5353\n"
        plan = self.PLAN_V31.replace("role=lan mask=smb,adisk", "role=isolated mask=none").replace("status=validated", "status=incomplete reason=mode")
        result, _ = self._run_mdns_probe([
            mock.Mock(returncode=0, stdout="/mnt/Flash/discoveryd\n", stderr=""),
            mock.Mock(returncode=0, stdout=self.PS_V31, stderr=""),
            mock.Mock(returncode=0, stdout=fstat_out, stderr=""),
            mock.Mock(returncode=0, stdout=plan, stderr=""),
        ])
        self.assertFalse(result.ready)
        self.assertIn("FAIL:other processes hold UDP 5353: discoveryd", result.lines)
        self.assertIn("FAIL:sharing facts are incomplete (mode); the registrant waits or retains its previous validated policy", result.lines)

    def test_probe_managed_mdns_accepts_diskless_registrant_without_smb_links(self) -> None:
        ps_out = self.PS_V31.replace(
            "nbns=ready mode=payload --netbios-name TimeCapsule --adisk-share Data dk2 12345678-1234-1234-1234-123456789012 0x82\n917 916 S 0:00 wcifsnd /sbin/wcifsnd",
            "nbns=waiting mode=diskless --diskless",
        )
        plan = self.PLAN_V31.replace("mask=smb,adisk", "mask=none").replace("diskless=0", "diskless=1")
        result, _ = self._run_mdns_probe([
            mock.Mock(returncode=0, stdout="/mnt/Flash/discoveryd\n", stderr=""),
            mock.Mock(returncode=0, stdout=ps_out, stderr=""),
            mock.Mock(returncode=0, stdout=self.FSTAT_V31, stderr=""),
            mock.Mock(returncode=0, stdout=plan, stderr=""),
        ])
        self.assertTrue(result.ready, result.lines)
        self.assertIn("PASS:mdns link plan validated (diskless; nothing advertised)", result.lines)

    def test_probe_managed_mdns_reports_missing_registrant_and_unparsable_plan(self) -> None:
        ps_out = "371 1 Sa 0:00 mDNSResponder /sbin/mDNSResponder -d\n559 1 S 0:00 diskd /sbin/diskd -i lo0 -d local.\n"
        result, _ = self._run_mdns_probe([
            mock.Mock(returncode=0, stdout="/mnt/Flash/discoveryd\n", stderr=""),
            mock.Mock(returncode=0, stdout=ps_out, stderr=""),
            mock.Mock(returncode=0, stdout=self.FSTAT_V31, stderr=""),
            mock.Mock(returncode=0, stdout="garbage\n", stderr=""),
        ])
        self.assertFalse(result.ready)
        self.assertIn("FAIL:discovery process is not running", result.lines)
        self.assertIn("FAIL:mdns link plan output could not be parsed", result.lines)

    def test_probe_managed_mdns_rejects_controller_without_native_nbns_state(self) -> None:
        ps_out = self.PS_V31.replace("nbns=ready mode=payload ", "")
        result, _ = self._run_mdns_probe([
            mock.Mock(returncode=0, stdout="/mnt/Flash/discoveryd\n", stderr=""),
            mock.Mock(returncode=0, stdout=ps_out, stderr=""),
            mock.Mock(returncode=0, stdout=self.FSTAT_V31, stderr=""),
            mock.Mock(returncode=0, stdout=self.PLAN_V31, stderr=""),
        ])

        self.assertFalse(result.ready)
        self.assertIn("FAIL:discovery NBNS state is not available yet", result.lines)

    def test_probe_managed_mdns_rejects_wcifsnd_sockets_owned_by_wrong_pid(self) -> None:
        wrong_pid_fstat = self.FSTAT_V31.replace("wcifsnd  917", "wcifsnd  999")
        result, _ = self._run_mdns_probe([
            mock.Mock(returncode=0, stdout="/mnt/Flash/discoveryd\n", stderr=""),
            mock.Mock(returncode=0, stdout=self.PS_V31, stderr=""),
            mock.Mock(returncode=0, stdout=wrong_pid_fstat, stderr=""),
            mock.Mock(returncode=0, stdout=self.PLAN_V31, stderr=""),
        ])

        self.assertFalse(result.ready)
        self.assertIn("FAIL:discovery native NBNS is not ready", result.lines)

    def test_probe_managed_mdns_accepts_waiting_without_service_eligible_ipv4(self) -> None:
        ps_out = self.PS_V31.replace("nbns=ready", "nbns=waiting").replace(
            "917 916 S 0:00 wcifsnd /sbin/wcifsnd\n", ""
        )
        fstat_out = "\n".join(line for line in self.FSTAT_V31.splitlines() if "wcifsnd" not in line) + "\n"
        plan = self.PLAN_V31.replace("addr=192.168.1.10", "addr=239.1.2.3")
        result, _ = self._run_mdns_probe([
            mock.Mock(returncode=0, stdout="/mnt/Flash/discoveryd\n", stderr=""),
            mock.Mock(returncode=0, stdout=ps_out, stderr=""),
            mock.Mock(returncode=0, stdout=fstat_out, stderr=""),
            mock.Mock(returncode=0, stdout=plan, stderr=""),
        ])

        self.assertTrue(result.ready, result.lines)
        self.assertIn("PASS:native NBNS is waiting", result.lines)
        result, _ = self._run_mdns_probe([
            mock.Mock(returncode=0, stdout="/mnt/Flash/discoveryd\n", stderr=""),
            mock.Mock(returncode=0, stdout=ps_out, stderr=""),
            mock.Mock(returncode=0, stdout=self.FSTAT_V31, stderr=""),
            mock.Mock(returncode=13, stdout="", stderr=""),
        ])
        self.assertIn("FAIL:mdns link plan probe failed with exit code 13", result.lines)

    def test_probe_managed_mdns_fails_the_diskd_gate_when_a_stray_diskd_runs_beside_ours(self) -> None:
        """Review 2 R8: `-i lo0` present somewhere is not enough; ACPd's diskd
        next to ours still advertises on the LAN (the runtime calls that acpd)."""
        ps_out = self.PS_V31 + "640 1 S 0:00 diskd /sbin/diskd -i  -d local.\n" + "735 1 ZW 0:00 diskd (diskd)\n"
        result, _ = self._run_mdns_probe([
            mock.Mock(returncode=0, stdout="/mnt/Flash/discoveryd\n", stderr=""),
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
            mock.Mock(returncode=0, stdout="/mnt/Flash/discoveryd\n", stderr=""),
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
            mock.Mock(returncode=0, stdout="/mnt/Flash/discoveryd\n", stderr=""),
            mock.Mock(returncode=0, stdout=self.PS_V31, stderr=""),
            mock.Mock(returncode=0, stdout=self.FSTAT_V31, stderr=""),
            mock.Mock(returncode=0, stdout=plan, stderr=""),
        ])
        self.assertTrue(result.ready, result.lines)
        broken = self.PLAN_V31.replace('instance="AirPort Time Capsule"', 'instance="Capsule "A"')
        result, _ = self._run_mdns_probe([
            mock.Mock(returncode=0, stdout="/mnt/Flash/discoveryd\n", stderr=""),
            mock.Mock(returncode=0, stdout=self.PS_V31, stderr=""),
            mock.Mock(returncode=0, stdout=self.FSTAT_V31, stderr=""),
            mock.Mock(returncode=0, stdout=broken, stderr=""),
        ])
        # The identity line is malformed; the plan/link lines still parse.
        self.assertTrue(result.ready, result.lines)

    def test_probe_managed_mdns_retries_binary_probe_timeout_and_reports_fstat_timeout(self) -> None:
        result, run_ssh_mock = self._run_mdns_probe([
            SshCommandTimeout("Timed out waiting for ssh command to finish: binary"),
            mock.Mock(returncode=0, stdout="/mnt/Flash/discoveryd\n", stderr=""),
            mock.Mock(returncode=0, stdout=self.PS_V31, stderr=""),
            mock.Mock(returncode=0, stdout=self.FSTAT_V31, stderr=""),
            mock.Mock(returncode=0, stdout=self.PLAN_V31, stderr=""),
        ])
        self.assertTrue(result.ready, result.lines)
        self.assertEqual([call.kwargs["timeout"] for call in run_ssh_mock.call_args_list[:2]],
                         [MDNS_BINARY_PROBE_TIMEOUT_SECONDS, MDNS_BINARY_PROBE_TIMEOUT_SECONDS])
        result, _ = self._run_mdns_probe([
            mock.Mock(returncode=0, stdout="/mnt/Flash/discoveryd\n", stderr=""),
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
        with mock.patch(
            "timecapsulesmb.device.probe.run_ssh",
            side_effect=[
                mock.Mock(returncode=0, stdout="/mnt/Flash/discoveryd\n", stderr=""),
                SshCommandTimeout("Timed out waiting for ssh command to finish: ps"),
            ],
        ):
            result = probe_managed_mdns_conn(SshConnection("host", "pw", "-o foo"))
        self.assertFalse(result.ready)
        self.assertEqual(result.detail, f"mDNS process table probe timed out after {MDNS_PROCESS_TABLE_PROBE_TIMEOUT_SECONDS}s")
        self.assertIn(f"FAIL:mDNS process table probe timed out after {MDNS_PROCESS_TABLE_PROBE_TIMEOUT_SECONDS}s", result.lines)

    def test_probe_netbsd4_rc_local_autostart_detects_login_marker(self) -> None:
        connection = SshConnection("host", "pw", "-o foo")
        login = b"#!/bin/sh\nif [ -x /mnt/Flash/rc.local ]; then /mnt/Flash/rc.local; fi\n"
        with mock.patch("timecapsulesmb.device.probe.run_ssh_capture_bytes", return_value=login) as run_mock:
            result = probe_netbsd4_rc_local_autostart_conn(connection, timeout_seconds=7)

        self.assertTrue(result.enabled)
        self.assertEqual(result.login_size, len(login))
        self.assertEqual(result.detail, "/etc/rc.d/LOGIN invokes /mnt/Flash/rc.local")
        run_mock.assert_called_once()
        self.assertEqual(run_mock.call_args.args[:2], (connection, "/bin/dd if=/etc/rc.d/LOGIN bs=4096 2>/dev/null"))
        self.assertEqual(run_mock.call_args.kwargs["timeout"], 7)

    def test_probe_netbsd4_rc_local_autostart_reports_missing_marker(self) -> None:
        with mock.patch("timecapsulesmb.device.probe.run_ssh_capture_bytes", return_value=b"#!/bin/sh\nexit 0\n"):
            result = probe_netbsd4_rc_local_autostart_conn(SshConnection("host", "pw", "-o foo"))

        self.assertFalse(result.enabled)
        self.assertEqual(result.detail, "/etc/rc.d/LOGIN does not invoke /mnt/Flash/rc.local")

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

    def test_decide_netbsd4_post_reboot_activation_uses_live_login_autostart(self) -> None:
        autostart = RcLocalAutostartProbeResult(
            enabled=True,
            detail="/etc/rc.d/LOGIN invokes /mnt/Flash/rc.local",
            login_size=128,
        )
        with mock.patch("timecapsulesmb.services.activation.probe_netbsd4_rc_local_autostart_conn", return_value=autostart):
            decision = decide_netbsd4_post_reboot_activation(SshConnection("host", "pw", "-o foo"))

        self.assertFalse(decision.run_actions)
        self.assertTrue(decision.verify_runtime)
        self.assertEqual(decision.reason, "firmware_autostart_enabled")
        self.assertIs(decision.autostart, autostart)

    def test_complete_deployment_no_wait_requests_reboot_without_verifying_runtime(self) -> None:
        prepared_plan = self._prepared_deploy_plan(
            startup_mode=DEPLOY_STARTUP_REBOOT_THEN_ACTIVATE,
            payload_family="netbsd4be_samba4",
            is_netbsd4=True,
            wait_after_reboot=False,
        )
        callbacks, _stages, logs, _debug_fields, _finish_fields = self._operation_callbacks()
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")
        request_reboot_func = mock.Mock()
        request_reboot_and_wait_func = mock.Mock()
        verify_runtime = mock.Mock()

        result = complete_deployment_after_upload(
            connection,
            prepared_plan,
            no_wait=True,
            callbacks=callbacks,
            messages=DeployCompletionMessages(reboot_request_message="Requesting reboot..."),
            request_reboot_func=request_reboot_func,
            request_reboot_and_wait_func=request_reboot_and_wait_func,
            verify_runtime_func=verify_runtime,
        )

        request_reboot_func.assert_called_once_with(
            connection,
            strategy="ssh_shutdown_then_reboot",
            callbacks=callbacks,
            raise_on_request_error=True,
        )
        request_reboot_and_wait_func.assert_not_called()
        verify_runtime.assert_not_called()
        self.assertIn("Requesting reboot...", logs)
        self.assertTrue(result.reboot_requested)
        self.assertFalse(result.waited)
        self.assertFalse(result.verified)

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
        request_reboot_and_wait_func = mock.Mock()
        activation_decision = ActivationDecision(
            run_actions=True,
            verify_runtime=True,
            reason="firmware_autostart_missing",
            detail="/etc/rc.d/LOGIN does not invoke /mnt/Flash/rc.local",
        )
        settle_calls: list[int] = []

        def decide_after_settle(_connection: SshConnection) -> ActivationDecision:
            self.assertEqual(settle_calls, [BOOT_SETTLE_SECONDS])
            return activation_decision

        with mock.patch(
            "timecapsulesmb.services.runtime_verification.sleep",
            side_effect=lambda seconds: settle_calls.append(seconds),
        ) as sleep_mock:
            result = complete_deployment_after_upload(
                connection,
                prepared_plan,
                no_wait=False,
                callbacks=callbacks,
                run_remote_actions_func=run_actions,
                request_reboot_and_wait_func=request_reboot_and_wait_func,
                decide_post_reboot_activation=mock.Mock(side_effect=decide_after_settle),
                verify_runtime_func=verify_runtime,
            )

        request_reboot_and_wait_func.assert_called_once()
        self.assertEqual(sleep_mock.call_args_list, [mock.call(BOOT_SETTLE_SECONDS), mock.call(ACTIVATION_SETTLE_SECONDS)])
        run_actions.assert_called_once_with(connection, prepared_plan.plan.activation_actions)
        verify_runtime.assert_called_once()
        self.assertEqual(stages, ["post_reboot_boot_settle", "probe_runtime", "post_reboot_activation", "post_activation_settle"])
        self.assertEqual(debug_fields["activation_decision"], "firmware_autostart_missing")
        self.assertTrue(debug_fields["manual_activation_required"])
        self.assertIn(BOOT_SETTLE_MESSAGE, logs)
        self.assertIn("Activating deployed runtime after reboot.", logs)
        self.assertIn(ACTIVATION_SETTLE_MESSAGE, logs)
        self.assertTrue(result.rebooted)
        self.assertTrue(result.verified)

    def test_complete_deployment_netbsd6_reboot_waits_for_runtime(self) -> None:
        prepared_plan = self._prepared_deploy_plan(startup_mode=DEPLOY_STARTUP_REBOOT_THEN_VERIFY)
        callbacks, stages, logs, _debug_fields, _finish_fields = self._operation_callbacks()
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")
        verify_runtime = mock.Mock()
        settle_calls: list[int] = []

        def verify_after_settle(*args, **kwargs) -> None:
            self.assertEqual(settle_calls, [BOOT_SETTLE_SECONDS])

        verify_runtime.side_effect = verify_after_settle

        with mock.patch(
            "timecapsulesmb.services.runtime_verification.sleep",
            side_effect=lambda seconds: settle_calls.append(seconds),
        ) as sleep_mock:
            result = complete_deployment_after_upload(
                connection,
                prepared_plan,
                no_wait=False,
                callbacks=callbacks,
                messages=DeployCompletionMessages(reboot_runtime_wait_message="Waiting for managed runtime..."),
                request_reboot_and_wait_func=mock.Mock(),
                verify_runtime_func=verify_runtime,
            )

        sleep_mock.assert_called_once_with(BOOT_SETTLE_SECONDS)
        verify_runtime.assert_called_once()
        self.assertEqual(verify_runtime.call_args.kwargs["stage"], "verify_runtime_reboot")
        self.assertIn(BOOT_SETTLE_MESSAGE, logs)
        self.assertIn("Waiting for managed runtime...", logs)
        self.assertEqual(stages, ["post_reboot_boot_settle"])
        self.assertTrue(result.verified)

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
        with mock.patch("timecapsulesmb.device.probe.probe_managed_runtime_once_conn", side_effect=[runtime_timeout, runtime_ready]) as runtime_once:
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
        with mock.patch("timecapsulesmb.device.probe.probe_managed_runtime_once_conn", return_value=runtime_not_ready) as runtime_once:
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
        with mock.patch("timecapsulesmb.device.probe.probe_managed_runtime_once_conn", return_value=runtime_not_ready) as runtime_once:
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
        with mock.patch("timecapsulesmb.device.probe.probe_managed_runtime_once_conn", return_value=runtime_not_ready):
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
        self.assertIn(f"rm -rf {payload_dir}/smb.conf.template", text)
        self.assertIn(f"rm -rf {payload_dir}/private/adisk.uuid", text)
        self.assertIn(f"rm -rf {payload_dir}/private/nbns.enabled", text)
        self.assertNotIn("generated smbpasswd", text)
        self.assertNotIn("generated:username.map", text)
        self.assertIn("generated flash runtime config (generated:tcapsulesmb.conf, scp, timeout 120s) -> /mnt/Flash/tcapsulesmb.conf", text)
        self.assertIn(f"checked-in rsync ({BINARY_RSYNC_SOURCE}, scp, timeout 180s) -> {payload_dir}/rsync", text)
        self.assertIn(f"generated rsync daemon config ({GENERATED_RSYNC_CONFIG_SOURCE}, generated, timeout 120s) -> {payload_dir}/rsyncd.conf", text)
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
        self.assertIn("probe /etc/rc.d/LOGIN for /mnt/Flash/rc.local", text)
        self.assertIn("if present: wait for managed runtime", text)
        self.assertIn("if missing: run /mnt/Flash/rc.local, then wait for managed runtime", text)
        self.assertIn("managed runtime smb.conf is present", text)
        self.assertIn("smbd is bound to required TCP 445 sockets", text)
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
        self.assertEqual(plan.remote_actions[:2], [StopServiceRuntimeAction(), StopWatchdogAction()])
        first_remove = next(i for i, action in enumerate(plan.remote_actions) if isinstance(action, RemovePathAction))
        self.assertLess(plan.remote_actions.index(StopProcessAction("smbd")), first_remove)
        self.assertLess(plan.remote_actions.index(StopProcessAction("rsync")), first_remove)
        for native in ("afpserver", "mDNSResponder"):
            self.assertNotIn(StopProcessAction(native), plan.remote_actions)

    def test_build_uninstall_plan_removes_flash_configuration(self) -> None:
        plan = build_uninstall_plan("root@10.0.0.2", ["/Volumes/dk2"], ["/Volumes/dk2/samba4"])

        self.assertEqual(plan.flash_targets["tcapsulesmb.conf"], "/mnt/Flash/tcapsulesmb.conf")
        self.assertIn("/mnt/Flash/tcapsulesmb.conf", plan.verify_absent_targets)
        self.assertIn(RemovePathAction("/mnt/Flash/tcapsulesmb.conf"), plan.remote_actions)

    def test_build_uninstall_plan_removes_each_payload_home_once(self) -> None:
        plan = build_uninstall_plan(
            "root@10.0.0.2",
            ["/Volumes/dk2", "/Volumes/dk5", "/Volumes/dk2"],
            ["/Volumes/dk2/samba4", "/Volumes/dk5/samba4", "/Volumes/dk2/samba4"],
        )

        self.assertEqual(plan.volume_roots, ["/Volumes/dk2", "/Volumes/dk5"])
        self.assertEqual(plan.payload_dirs, ["/Volumes/dk2/samba4", "/Volumes/dk5/samba4"])
        self.assertEqual(
            [action for action in plan.remote_actions if action == RemovePathAction("/Volumes/dk2/samba4")],
            [RemovePathAction("/Volumes/dk2/samba4")],
        )
        self.assertIn(RemovePathAction("/Volumes/dk5/samba4"), plan.remote_actions)

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
                with self.assertRaisesRegex(ValueError, "Refusing to remove flash root path"):
                    render_remote_action(RemovePathAction(unsafe_path))

        self.assertEqual(
            render_remote_action(RemovePathAction("/mnt/Flash/rc.local")),
            "rm -rf /mnt/Flash/rc.local",
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
            remote_action_to_jsonable(StopWatchdogAction()),
            {"kind": "stop_watchdog", "args": []},
        )
        self.assertEqual(
            remote_action_to_jsonable(StopManagerAction()),
            {"kind": "stop_manager", "args": []},
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

        for index, action in enumerate(plan.pre_upload_actions):
            if isinstance(action, RemovePathAction) and action.path.startswith("/Volumes/"):
                self.assertEqual(plan.pre_upload_actions[index - 1], expected_guard)
        prepare = next(index for index, action in enumerate(plan.pre_upload_actions)
                       if isinstance(action, PrepareDirsAction))
        self.assertEqual(plan.pre_upload_actions[prepare - 1], expected_guard)
        self.assertEqual(plan.post_upload_actions[0], expected_guard)
        for protocol in ("mdns", "nbns"):
            self.assertIn(RemovePathAction(f"{plan.payload_dir}/{protocol}"), plan.pre_upload_actions)
            self.assertIn(StopProcessAction(protocol), plan.pre_upload_actions)
            self.assertIn(StopProcessAction(protocol + "-advertiser"), plan.pre_upload_actions)
        self.assertIn(RemovePathAction(f"{plan.payload_dir}/discoveryd"), plan.pre_upload_actions)
        self.assertIn(StopProcessAction("discoveryd"), plan.pre_upload_actions)
        self.assertIn(StopProcessAction("wcifsnd"), plan.pre_upload_actions)
        self.assertIn(RemovePathAction("/mnt/Flash/mdns"), plan.pre_upload_actions)
        self.assertIn(RemovePathAction("/mnt/Flash/mdns-advertiser"), plan.pre_upload_actions)

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
        def process_present(command: str, *, ps_lines: list[str]) -> bool:
            with tempfile.TemporaryDirectory() as tmp:
                fixture = Path(tmp) / "ps.txt"
                fixture.write_text("\n".join(ps_lines) + "\n")
                command = command.replace(
                    "ps axww -o stat= -o ucomm= -o command= >/tmp/tcapsule-ps.$$ 2>/dev/null",
                    f"cat {shlex.quote(str(fixture))} >/tmp/tcapsule-ps.$$",
                )
                result = subprocess.run(["/bin/sh", "-c", command], check=False, text=True, capture_output=True)
            self.assertEqual(result.stderr, "")
            return result.returncode == 0

        self.assertFalse(process_present(render_process_present_by_ucomm("wcifsnd"), ps_lines=["Z    wcifsnd         (wcifsnd)"]))
        self.assertTrue(process_present(render_process_present_by_ucomm("wcifsnd"), ps_lines=["S    wcifsnd         wcifsnd"]))
        self.assertFalse(process_present(render_watchdog_process_present(), ps_lines=["Z    sh              /bin/sh /mnt/Flash/watchdog.sh"]))
        self.assertTrue(process_present(render_watchdog_process_present(), ps_lines=["S    sh              /bin/sh /mnt/Flash/watchdog.sh"]))
        self.assertFalse(process_present(render_manager_process_present(), ps_lines=["Z    sh              /bin/sh /mnt/Flash/manager.sh"]))
        self.assertTrue(process_present(render_manager_process_present(), ps_lines=["S    sh              /bin/sh /mnt/Flash/manager.sh"]))
        self.assertFalse(
            process_present(
                render_watchdog_process_present(),
                ps_lines=[
                    "S    sh              /bin/sh -c probe=/mnt/Flash/watchdog.sh",
                    "S    sh              sh -c /bin/sh -c 'probe=/mnt/Flash/watchdog.sh'",
                ],
            )
        )
        self.assertFalse(
            process_present(
                render_manager_process_present(),
                ps_lines=[
                    "S    sh              /bin/sh -c probe=/mnt/Flash/manager.sh",
                    "S    sh              sh -c /bin/sh -c 'probe=/mnt/Flash/manager.sh'",
                ],
            )
        )

    def test_render_process_present_rejects_generic_full_substring_matches(self) -> None:
        with self.assertRaises(ValueError):
            render_remote_action(StopProcessAction("smbd;rm"))

    def test_render_stop_process_action_waits_for_exit(self) -> None:
        command = render_remote_action(StopProcessAction("mdns"))
        self.assertIn("/usr/bin/pkill '^mdns$' >/dev/null 2>&1 || true;", command)
        self.assertIn("while /bin/sh -c 'found=1; if ps axww -o stat= -o ucomm= -o command= >/tmp/tcapsule-ps.", command)
        self.assertIn('case \"$1\" in Z*) continue ;; esac;', command)
        self.assertIn('if [ \"$2\" = mdns ]; then found=1; break; fi;', command)
        self.assertIn('if [ "$attempt" -ge 5 ]; then break; fi;', command)
        self.assertIn("/usr/bin/pkill -9 '^mdns$' >/dev/null 2>&1 || true;", command)

    def test_render_stop_process_action_kills_and_fails_if_still_running(self) -> None:
        command = render_remote_action(StopProcessAction("smbd"))
        self.assertIn("/usr/bin/pkill '^smbd$' >/dev/null 2>&1 || true;", command)
        self.assertIn('if [ "$attempt" -ge 5 ]; then break; fi;', command)
        self.assertIn("/usr/bin/pkill -9 '^smbd$' >/dev/null 2>&1 || true;", command)
        self.assertIn("echo 'process smbd did not stop' >&2; exit 1", command)

    def test_render_stop_watchdog_action_waits_for_exit(self) -> None:
        command = render_remote_action(StopWatchdogAction())
        self.assertIn("tc_watchdog_pids() {", command)
        self.assertIn("tc_kill_watchdog_pids TERM;", command)
        self.assertIn("while /bin/sh -c 'found=1; if ps axww -o stat= -o ucomm= -o command= >/tmp/tcapsule-ps.", command)
        self.assertIn('case \"$1\" in Z*) continue ;; esac;', command)
        self.assertIn('[ "$2" = sh ] || continue;', command)
        self.assertIn("tc_kill_watchdog_pids KILL;", command)
        self.assertNotIn("/usr/bin/pkill -f '[w]atchdog.sh'", command)
        self.assertNotIn("/usr/bin/pkill -9 -f", command)

    def test_render_stop_watchdog_action_kills_by_full_match(self) -> None:
        command = render_remote_action(StopWatchdogAction())
        self.assertIn('if [ "${1:-}" = /bin/sh ] || [ "${1:-}" = sh ]; then', command)
        self.assertIn('/bin/kill -9 "$tc_watchdog_pid" >/dev/null 2>&1 || true', command)
        self.assertIn("echo 'process watchdog did not stop' >&2; exit 1", command)

    def test_render_stop_manager_action_kills_by_full_match(self) -> None:
        command = render_remote_action(StopManagerAction())
        self.assertIn("tc_manager_pids() {", command)
        self.assertIn("tc_kill_manager_pids TERM;", command)
        self.assertIn('if [ "${1:-}" = /bin/sh ] || [ "${1:-}" = sh ]; then', command)
        self.assertIn('/bin/kill -9 "$tc_manager_pid" >/dev/null 2>&1 || true', command)
        self.assertIn("echo 'process manager did not stop' >&2; exit 1", command)
        self.assertNotIn("/usr/bin/pkill -f '[m]anager.sh'", command)

    def test_wait_for_ssh_state_uses_real_ssh_probe_for_expected_up(self) -> None:
        proc = mock.Mock(returncode=0, stdout="ok\n")
        connection = SshConnection("root@10.0.0.2", "pw", "-o ProxyCommand=jump")
        with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=proc) as run_ssh_mock:
            self.assertTrue(wait_for_ssh_state_conn(connection, expected_up=True, timeout_seconds=1))
        run_ssh_mock.assert_called_once_with(connection, "/bin/echo ok", check=False, timeout=30)

    def test_wait_for_ssh_state_treats_probe_failure_as_down(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "-o ProxyCommand=jump")
        with mock.patch("timecapsulesmb.device.probe.run_ssh", side_effect=SshError("timeout")) as run_ssh_mock:
            self.assertTrue(wait_for_ssh_state_conn(connection, expected_up=False, timeout_seconds=1))
        run_ssh_mock.assert_called_once_with(connection, "/bin/echo ok", check=False, timeout=30)

    def test_wait_for_ssh_state_retries_until_up(self) -> None:
        fail = mock.Mock(returncode=255, stdout="")
        ok = mock.Mock(returncode=0, stdout="ok\n")
        with mock.patch("timecapsulesmb.device.probe.run_ssh", side_effect=[fail, ok]) as run_ssh_mock:
            with mock.patch("timecapsulesmb.device.probe.time.sleep") as sleep_mock:
                self.assertTrue(wait_for_ssh_state_conn(SshConnection("root@10.0.0.2", "pw", "-o ProxyCommand=jump"), expected_up=True, timeout_seconds=6))
        self.assertEqual(run_ssh_mock.call_count, 2)
        sleep_mock.assert_called_once_with(5)

    def test_wait_for_ssh_state_retries_until_down(self) -> None:
        ok = mock.Mock(returncode=0, stdout="ok\n")
        with mock.patch("timecapsulesmb.device.probe.run_ssh", side_effect=[ok, SshError("down")]) as run_ssh_mock:
            with mock.patch("timecapsulesmb.device.probe.time.sleep") as sleep_mock:
                self.assertTrue(wait_for_ssh_state_conn(SshConnection("root@10.0.0.2", "pw", "-o ProxyCommand=jump"), expected_up=False, timeout_seconds=6))
        self.assertEqual(run_ssh_mock.call_count, 2)
        sleep_mock.assert_called_once_with(5)

    def test_wait_for_ssh_state_times_out_when_state_never_matches(self) -> None:
        ok = mock.Mock(returncode=0, stdout="ok\n")
        with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=ok) as run_ssh_mock:
            with mock.patch("timecapsulesmb.device.probe.time.time", side_effect=[0.0, 0.0, 2.0]):
                with mock.patch("timecapsulesmb.device.probe.time.sleep") as sleep_mock:
                    self.assertFalse(wait_for_ssh_state_conn(SshConnection("root@10.0.0.2", "pw", "-o ProxyCommand=jump"), expected_up=False, timeout_seconds=1))
        run_ssh_mock.assert_called_once()
        sleep_mock.assert_called_once_with(5)


if __name__ == "__main__":
    unittest.main()
