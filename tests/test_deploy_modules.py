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


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from timecapsulesmb.deploy.auth import nt_hash_hex, render_smbpasswd
from timecapsulesmb.deploy.commands import (
    EnsureVolumeMountedAction,
    InstallPermissionsAction,
    PrepareDirsAction,
    RemotePermission,
    RemoteSymlink,
    RemovePathAction,
    RunScriptAction,
    StopManagerAction,
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
    flush_remote_filesystem_writes,
    remote_request_reboot,
    run_remote_actions,
    remote_uninstall_payload,
    upload_deployment_payload,
    upload_flash_file,
)
from timecapsulesmb.deploy.planner import (
    BINARY_MDNS_SOURCE,
    BINARY_NBNS_SOURCE,
    BINARY_SMBD_SOURCE,
    DEFAULT_APPLE_MOUNT_WAIT_SECONDS,
    DEPLOY_STARTUP_ACTIVATE_NOW,
    DEPLOY_STARTUP_REBOOT_THEN_ACTIVATE,
    DEPLOY_STARTUP_REBOOT_THEN_VERIFY,
    FLASH_TEXT_UPLOAD_TIMEOUT_SECONDS,
    GENERATED_FLASH_CONFIG_SOURCE,
    GENERATED_SMBPASSWD_SOURCE,
    GENERATED_USERNAME_MAP_SOURCE,
    PACKAGED_BOOT_SOURCE,
    PACKAGED_COMMON_SH_SOURCE,
    PACKAGED_DFREE_SH_SOURCE,
    PACKAGED_MANAGER_SOURCE,
    PACKAGED_RC_LOCAL_SOURCE,
    PAYLOAD_BINARY_UPLOAD_TIMEOUT_SECONDS,
    build_deployment_plan,
    build_uninstall_plan,
)
from timecapsulesmb.deploy.boot_assets import (
    COMMON_SH_FRAGMENTS,
    assemble_common_sh_text,
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
    probe_managed_mdns_takeover_conn,
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
    DeployPayloadContext,
    DeployRuntimeConfig,
    PreparedDeployPlan,
    complete_deployment_after_upload,
    upload_and_verify_deployment_payload,
)
from timecapsulesmb.transport.ssh import SshCommandTimeout, SshConnection, SshError


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
    _mdns_binary_tmpdir: tempfile.TemporaryDirectory[str] | None = None
    _mdns_binary_path: Path | None = None
    _nbns_binary_tmpdir: tempfile.TemporaryDirectory[str] | None = None
    _nbns_binary_path: Path | None = None

    @classmethod
    def tearDownClass(cls) -> None:
        if cls._mdns_binary_tmpdir is not None:
            cls._mdns_binary_tmpdir.cleanup()
        if cls._nbns_binary_tmpdir is not None:
            cls._nbns_binary_tmpdir.cleanup()
        cls._mdns_binary_tmpdir = None
        cls._mdns_binary_path = None
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
        startup_mode=DEPLOY_STARTUP_ACTIVATE_NOW,
        payload_family: str = "netbsd6_samba4",
        is_netbsd4: bool = False,
        wait_after_reboot: bool = True,
    ) -> PreparedDeployPlan:
        payload_home = self._payload_home()
        plan = build_deployment_plan(
            "root@10.0.0.2",
            payload_home,
            Path("bin/smbd"),
            Path("bin/mdns"),
            Path("bin/nbns"),
            startup_mode=startup_mode,
            wait_after_reboot=wait_after_reboot,
        )
        return PreparedDeployPlan(
            payload_context=DeployPayloadContext(
                compatibility=mock.Mock(),
                payload_family=payload_family,
                is_netbsd4=is_netbsd4,
                startup_mode=startup_mode,
            ),
            artifacts=DeployArtifactPaths(
                smbd=Path("bin/smbd"),
                mdns_advertiser=Path("bin/mdns"),
                nbns_advertiser=Path("bin/nbns"),
            ),
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
        if shutil.which("cc") is None:
            self.skipTest("cc not available")

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            c_path = tmp / f"{bin_name}.c"
            bin_path = tmp / bin_name
            c_path.write_text(source)
            proc = subprocess.run(
                ["cc", "-Wall", "-Wextra", "-Werror", str(c_path), "-o", str(bin_path)],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            return subprocess.run(
                [str(bin_path), *(args or [])],
                capture_output=True,
                text=True,
                check=False,
            )

    def _compile_mdns_advertiser_binary(self, tmp: Path) -> Path:
        if shutil.which("cc") is None:
            self.skipTest("cc not available")
        if self.__class__._mdns_binary_path is not None:
            return self.__class__._mdns_binary_path

        self.__class__._mdns_binary_tmpdir = tempfile.TemporaryDirectory()
        bin_path = Path(self.__class__._mdns_binary_tmpdir.name) / "mdns-advertiser"
        proc = subprocess.run(
            [
                "cc",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-DSNAPSHOT_CAPTURE_TIMEOUT_SECONDS=0",
                "-DSNAPSHOT_CAPTURE_RETRY_INTERVAL_SECONDS=0",
                "-DSNAPSHOT_CAPTURE_STEP_SECONDS=0",
                str(REPO_ROOT / "build" / "mdns-advertiser.c"),
                "-o",
                str(bin_path),
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.__class__._mdns_binary_path = bin_path
        return bin_path

    def _compile_nbns_advertiser_binary(self, tmp: Path) -> Path:
        if shutil.which("cc") is None:
            self.skipTest("cc not available")
        if self.__class__._nbns_binary_path is not None:
            return self.__class__._nbns_binary_path

        self.__class__._nbns_binary_tmpdir = tempfile.TemporaryDirectory()
        bin_path = Path(self.__class__._nbns_binary_tmpdir.name) / "nbns-advertiser"
        proc = subprocess.run(
            [
                "cc",
                "-Wall",
                "-Wextra",
                "-Werror",
                str(REPO_ROOT / "build" / "nbns-advertiser.c"),
                "-o",
                str(bin_path),
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.__class__._nbns_binary_path = bin_path
        return bin_path

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

    def _write_matching_airport_snapshot(self, path: Path) -> None:
        path.write_text(
            "BEGIN\n"
            "TYPE=_airport._tcp.local.\n"
            "INSTANCE=Home\n"
            "HOST=Home.local.\n"
            "PORT=5009\n"
            "TXT=waMA=80-EA-96-E6-58-68,syAP=119\n"
            "END\n"
        )

    def test_nt_hash_hex_is_stable(self) -> None:
        self.assertEqual(nt_hash_hex("password"), "8846F7EAEE8FB117AD06BDD830B7586C")

    def test_render_smbpasswd_maps_any_username_to_root(self) -> None:
        smbpasswd_text, username_map = render_smbpasswd("password")
        self.assertTrue(smbpasswd_text.startswith("root:0:"))
        self.assertEqual(username_map, "!root = root\nroot = *\n")

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
        self.assertIn("/bin/sleep 5", FLUSH_REMOTE_FILESYSTEMS_COMMAND)
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
        content = load_boot_asset_text("rc.local")
        self.assertIn("/mnt/Flash/boot.sh", content)
        common = load_boot_asset_text("common.sh")
        self.assertEqual(common, assemble_common_sh_text())
        self.assertIn("get_airport_syvs()", common)
        self.assertIn("ether[[:space:]]", common)
        self.assertIn("address:*[[:space:]]", common)
        self.assertNotIn("tr '[:lower:]' '[:upper:]'", common)
        self.assertNotIn("/usr/bin/wc", common)
        self.assertNotIn("/usr/bin/tr", common)

    def test_common_sh_is_assembled_from_ordered_source_fragments(self) -> None:
        asset_root = REPO_ROOT / "src/timecapsulesmb/assets/boot/samba4"
        assembled = "".join((asset_root / fragment).read_text().rstrip("\n") + "\n" for fragment in COMMON_SH_FRAGMENTS)
        self.assertEqual(load_boot_asset_text("common.sh"), assembled)
        self.assertGreaterEqual(len(COMMON_SH_FRAGMENTS), 5)

        with tempfile.TemporaryDirectory() as tmp:
            assembled_path = Path(tmp) / "common.sh"
            progressive = ""
            for fragment in COMMON_SH_FRAGMENTS:
                fragment_path = asset_root / fragment
                self.assertTrue(fragment_path.is_file(), fragment)
                progressive += fragment_path.read_text().rstrip("\n") + "\n"
                assembled_path.write_text(progressive)
                subprocess.run(["/bin/sh", "-n", str(assembled_path)], check=True, text=True, capture_output=True)

            with boot_asset_path("common.sh") as common_path:
                self.assertEqual(common_path.read_text(), assembled)
                self.assertNotEqual(common_path, asset_root / "common.sh")

    def test_common_sh_contains_shared_network_and_airport_helpers(self) -> None:
        content = load_boot_asset_text("common.sh")
        self.assertIn("RAM_ROOT=/mnt/Memory/samba4", content)
        self.assertIn('RAM_SBIN="$RAM_ROOT/sbin"', content)
        self.assertIn('RAM_ETC="$RAM_ROOT/etc"', content)
        self.assertIn('RAM_VAR="$RAM_ROOT/var"', content)
        self.assertIn('RAM_PRIVATE="$RAM_ROOT/private"', content)
        self.assertIn("LOCKS_ROOT=/mnt/Locks", content)
        self.assertIn("MDNS_PROC_NAME=mdns-advertiser", content)
        self.assertIn("NBNS_PROC_NAME=nbns-advertiser", content)
        self.assertNotIn("ALL_MDNS_SNAPSHOT=/mnt/Flash/allmdns.txt", content)
        self.assertNotIn("APPLE_MDNS_SNAPSHOT=/mnt/Flash/applemdns.txt", content)
        self.assertIn("tc_select_advertise_mac()", content)
        self.assertIn("tc_select_live_iface_mac()", content)
        self.assertIn("get_airport_prni_raw()", content)
        self.assertNotIn("get_iface_mac()", content)
        self.assertNotIn("tc_select_advertise_network()", content)
        self.assertNotIn("tc_find_iface_for_ipv4()", content)
        self.assertIn("get_radio_mac()", content)
        self.assertIn("get_airport_srcv()", content)
        self.assertIn("get_airport_syvs()", content)
        self.assertIn("wait_for_process()", content)
        self.assertIn("tc_ensure_parent_dir()", content)
        self.assertNotIn("wait_for_smbd_ready()", content)
        self.assertNotIn("daemon_ready", content)
        self.assertIn("derive_airport_fields()", content)
        self.assertIn("get_airport_syvs()", content)
        self.assertIn("sed -n 's/^\\([0-9]\\)\\([0-9]\\)\\([0-9]\\).*/\\1.\\2.\\3/p'", content)

    def test_common_process_helpers_ignore_zombies(self) -> None:
        common = load_boot_asset_text("common.sh").replace(
            "/bin/ps axww -o pid= -o stat= -o ucomm= -o command= 2>/dev/null",
            'cat "$PS_FIXTURE"',
        )
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Path(tmp) / "ps.txt"
            script = Path(tmp) / "check.sh"
            fixture.write_text(
                "\n".join(
                    [
                        "101 Z    wcifsnd         (wcifsnd)",
                        "102 Z    wcifsfs         (wcifsfs)",
                        "103 S    nbns-advertiser /mnt/Memory/samba4/sbin/nbns-advertiser --name TimeCapsule",
                        "106 S    sh              /bin/sh /mnt/Flash/manager.sh",
                    ]
                )
                + "\n"
            )
            script.write_text(
                common
                + f"\nPS_FIXTURE={shlex.quote(str(fixture))}\n"
                + """
runtime_process_present_by_ucomm wcifsnd; echo "zombie-name=$?"
runtime_process_present_by_ucomm nbns-advertiser; echo "live-name=$?"
runtime_manager_present; echo "manager-full=$?"
echo "manager-pids=$(runtime_manager_pids)"
runtime_process_present_by_ucomm wcifsfs; echo "zombie-full=$?"
wait_for_process nbns-advertiser 1; echo "live-wait=$?"
wait_for_process wcifsnd 1; echo "zombie-wait=$?"
"""
            )

            result = subprocess.run(["/bin/sh", str(script)], check=False, text=True, capture_output=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("zombie-name=1", result.stdout)
        self.assertIn("live-name=0", result.stdout)
        self.assertIn("manager-full=0", result.stdout)
        self.assertIn("manager-pids=106", result.stdout)
        self.assertIn("zombie-full=1", result.stdout)
        self.assertIn("live-wait=0", result.stdout)
        self.assertIn("zombie-wait=1", result.stdout)

    def test_common_size_helpers_do_not_require_netbsd_missing_tools(self) -> None:
        common = load_boot_asset_text("common.sh")
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            sample = tmp_path / "sample.txt"
            sample.write_text("AirPort Disk")
            script = tmp_path / "check.sh"
            script.write_text(
                common
                + f"\nSAMPLE={shlex.quote(str(sample))}\n"
                + """
echo "byte-len=$(tc_byte_len 'AirPort Disk')"
echo "utf8-byte-len=$(tc_byte_len 'éé')"
echo "file-size=$(tc_log_file_size "$SAMPLE")"
"""
            )

            result = subprocess.run(["/bin/sh", str(script)], check=False, text=True, capture_output=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("byte-len=12", result.stdout)
        self.assertIn("utf8-byte-len=4", result.stdout)
        self.assertIn("file-size=12", result.stdout)

    def test_common_binary_selection_logs_only_when_debug_logging_enabled(self) -> None:
        common = load_boot_asset_text("common.sh")
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            payload = tmp_path / "payload"
            payload.mkdir()
            for name in ("smbd", "nbns-advertiser"):
                binary = payload / name
                binary.write_text("#!/bin/sh\n")
                binary.chmod(0o755)
            log = tmp_path / "runtime.log"
            script = tmp_path / "check.sh"
            script.write_text(
                common
                + f"\nPAYLOAD={shlex.quote(str(payload))}\n"
                + f"TC_LOG_FILE={shlex.quote(str(log))}\n"
                + """
TC_LOG_PREFIX=manager
TC_LOG_MAX_BYTES=65536
SMBD_DEBUG_LOGGING=0
echo "smbd-normal=$(tc_find_payload_smbd "$PAYLOAD")"
echo "nbns-normal=$(tc_find_payload_nbns "$PAYLOAD")"
normal_log=$(cat "$TC_LOG_FILE" 2>/dev/null || true)
SMBD_DEBUG_LOGGING=1
echo "smbd-debug=$(tc_find_payload_smbd "$PAYLOAD")"
echo "nbns-debug=$(tc_find_payload_nbns "$PAYLOAD")"
printf '%s\n' "$normal_log" >"$PAYLOAD/normal.log"
"""
            )

            result = subprocess.run(["/bin/sh", str(script)], check=False, text=True, capture_output=True)
            normal_log = (payload / "normal.log").read_text()
            debug_log = log.read_text()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"smbd-normal={payload}/smbd", result.stdout)
        self.assertIn(f"nbns-normal={payload}/nbns-advertiser", result.stdout)
        self.assertIn(f"smbd-debug={payload}/smbd", result.stdout)
        self.assertIn(f"nbns-debug={payload}/nbns-advertiser", result.stdout)
        self.assertNotIn("selected smbd binary", normal_log)
        self.assertNotIn("selected nbns binary", normal_log)
        self.assertIn(f"selected smbd binary {payload}/smbd", debug_log)
        self.assertIn(f"selected nbns binary {payload}/nbns-advertiser", debug_log)

    def test_common_select_advertise_mac_falls_back_to_ifconfig_mac(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake_ifconfig = tmp_path / "ifconfig"
            fake_ifconfig.write_text(
                "#!/bin/sh\n"
                "cat <<'OUT'\n"
                "bcmeth1: flags=8843<UP,BROADCAST,RUNNING,SIMPLEX,MULTICAST>\n"
                "        address: 80:ea:96:e6:58:70\n"
                "bridge0: flags=8843<UP,BROADCAST,RUNNING,SIMPLEX,MULTICAST>\n"
                "        address: 80:ea:96:e6:58:71\n"
                "OUT\n"
            )
            fake_ifconfig.chmod(0o755)
            common = load_boot_asset_text("common.sh").replace("/sbin/ifconfig", shlex.quote(str(fake_ifconfig)))
            script = tmp_path / "check.sh"
            script.write_text(
                common
                + """
	tc_log() { :; }
	get_airport_acp_value() { return 1; }
	printf 'mac=%s\n' "$(tc_select_advertise_mac)"
	"""
            )

            result = subprocess.run(["/bin/sh", str(script)], check=False, text=True, capture_output=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("mac=80:ea:96:e6:58:70\n", result.stdout)

    def test_common_log_trim_preserves_existing_log_when_readers_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fail_tail = tmp_path / "tail"
            fail_cat = tmp_path / "cat"
            fail_tail.write_text("#!/bin/sh\nexit 1\n")
            fail_cat.write_text("#!/bin/sh\nexit 1\n")
            fail_tail.chmod(0o755)
            fail_cat.chmod(0o755)
            common = (
                load_boot_asset_text("common.sh")
                .replace("/usr/bin/tail", shlex.quote(str(fail_tail)))
                .replace("/bin/cat", shlex.quote(str(fail_cat)))
            )
            bounded_log = tmp_path / "bounded.log"
            legacy_log = tmp_path / "legacy.log"
            script = tmp_path / "check.sh"
            script.write_text(
                common
                + f"\nBOUNDED_LOG={shlex.quote(str(bounded_log))}\n"
                + f"LEGACY_LOG={shlex.quote(str(legacy_log))}\n"
                + """
printf '%s\n' 'abcdefghijklmnopqrstuvwxyz' >"$BOUNDED_LOG"
printf '%s\n' '0123456789abcdef' >"$LEGACY_LOG"
tc_trim_log_file_if_needed "$BOUNDED_LOG" 5
tc_prepare_log_file "$LEGACY_LOG" 5
"""
            )

            result = subprocess.run(["/bin/sh", str(script)], check=False, text=True, capture_output=True)
            trim_temps = list(tmp_path.glob("*.tmp.*"))

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(bounded_log.read_text(), "abcdefghijklmnopqrstuvwxyz\n")
            self.assertEqual(legacy_log.read_text(), "0123456789abcdef\n")
            self.assertEqual(trim_temps, [])

    def test_common_hostname_resolution_update_is_idempotent(self) -> None:
        common = (
            load_boot_asset_text("common.sh")
            .replace("/etc/hosts", '"$HOSTS_FIXTURE"')
            .replace("$(/bin/hostname 2>/dev/null || true)", "airport-base")
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            hosts = tmp_path / "hosts"
            log = tmp_path / "runtime.log"
            script = tmp_path / "check.sh"
            hosts.write_text("127.0.0.1\tlocalhost airport-base-old\n")
            script.write_text(
                common
                + f"\nHOSTS_FIXTURE={shlex.quote(str(hosts))}\n"
                + f"TC_LOG_FILE={shlex.quote(str(log))}\n"
                + """
tc_prepare_local_hostname_resolution
SMBD_DEBUG_LOGGING=1
tc_prepare_local_hostname_resolution
"""
            )

            result = subprocess.run(["/bin/sh", str(script)], check=False, text=True, capture_output=True)

            hosts_text = hosts.read_text()
            log_text = log.read_text()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(hosts_text.count("127.0.0.1\tairport-base airport-base.local\n"), 1)
        self.assertIn("127.0.0.1\tlocalhost airport-base-old\n", hosts_text)
        self.assertIn("local hostname resolution prepared for airport-base", log_text)
        self.assertIn("local hostname resolution already present for airport-base", log_text)

    def test_common_script_process_helpers_do_not_self_match_literal(self) -> None:
        common = load_boot_asset_text("common.sh").replace(
            "/bin/ps axww -o pid= -o stat= -o ucomm= -o command= 2>/dev/null",
            'cat "$PS_FIXTURE"',
        )
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Path(tmp) / "ps.txt"
            script = Path(tmp) / "check.sh"
            fixture.write_text(
                "\n".join(
                    [
                        "103 S    sh              /bin/sh -c probe=/mnt/Flash/manager.sh",
                        "104 S    sh              sh -c /bin/sh -c 'probe=/mnt/Flash/manager.sh'",
                    ]
                )
                + "\n"
            )
            script.write_text(
                common
                + f"\nPS_FIXTURE={shlex.quote(str(fixture))}\n"
                + """
runtime_manager_present; echo "manager=$?"
echo "manager-pids=$(runtime_manager_pids)"
"""
            )

            result = subprocess.run(["/bin/sh", str(script)], check=False, text=True, capture_output=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("manager=1", result.stdout)
        self.assertIn("manager-pids=", result.stdout)

    def test_common_manager_kill_helper_targets_only_detected_pids(self) -> None:
        common = load_boot_asset_text("common.sh").replace("/bin/kill", "record_kill")
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "check.sh"
            kill_log = Path(tmp) / "kill.log"
            script.write_text(
                common
                + f"\nKILL_LOG={shlex.quote(str(kill_log))}\n"
                + """
record_kill() { echo "kill:$*" >> "$KILL_LOG"; }
runtime_manager_pids() { printf '%s\\n' 333; }
kill_manager_pids TERM
kill_manager_pids KILL
"""
            )

            result = subprocess.run(["/bin/sh", str(script)], check=False, text=True, capture_output=True)
            kill_lines = kill_log.read_text().splitlines()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            kill_lines,
            ["kill:333", "kill:-9 333"],
        )

    def test_common_script_kill_helper_allows_no_detected_pids_under_nounset(self) -> None:
        common = load_boot_asset_text("common.sh").replace("/bin/kill", "record_kill")
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "check.sh"
            kill_log = Path(tmp) / "kill.log"
            script.write_text(
                common
                + f"\nKILL_LOG={shlex.quote(str(kill_log))}\n"
                + """
set -eu
record_kill() { echo "unexpected kill:$*" >> "$KILL_LOG"; }
runtime_manager_pids() { :; }
kill_manager_pids TERM
echo ok
"""
            )

            result = subprocess.run(["/bin/sh", str(script)], check=False, text=True, capture_output=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "ok\n")
        self.assertFalse(kill_log.exists())

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
        result = derive_runtime_naming_identity("James's AirPort.Time Capsule", "Time Capsule.local")

        self.assertEqual(result.system_name, "James's AirPort.Time Capsule")
        self.assertEqual(result.hostname, "Time Capsule.local")
        self.assertEqual(result.mdns_instance_name, "James's AirPort-Time Capsule")
        self.assertEqual(result.mdns_host_label, "time-capsule")
        self.assertEqual(result.netbios_name, "TimeCapsule")

    def test_runtime_naming_identity_rejects_netbios_without_alnum(self) -> None:
        result = derive_runtime_naming_identity("极端 时间胶囊", "---.local")

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

    def test_common_sh_mac_helpers_use_live_scan_and_radio_argument(self) -> None:
        content = load_boot_asset_text("common.sh")
        self.assertIn("tc_select_live_iface_mac()", content)
        self.assertIn("ifconfig -a", content)
        self.assertIn("radio_iface=$1", content)
        self.assertIn('ifconfig "$radio_iface"', content)

    def test_common_sh_allows_partial_airport_field_derivation(self) -> None:
        content = load_boot_asset_text("common.sh")
        self.assertIn('if [ -n "$AIRPORT_WAMA" ] || [ -n "$AIRPORT_RAMA" ] || [ -n "$AIRPORT_RAM2" ] || [ -n "$AIRPORT_SRCV" ] || [ -n "$AIRPORT_SYVS" ]; then', content)

    def test_runtime_scripts_source_common_sh(self) -> None:
        boot = load_boot_asset_text("boot.sh")
        manager = load_boot_asset_text("manager.sh")
        self.assertIn(". /mnt/Flash/common.sh", boot)
        self.assertIn(". /mnt/Flash/common.sh", manager)
        self.assertNotIn("RAM_SAMBA_LIBEXEC", boot)
        self.assertNotIn("RAM_SAMBA_LIBEXEC", manager)

    def test_rc_local_leaves_service_launch_to_boot_script(self) -> None:
        content = load_boot_asset_text("rc.local")
        self.assertIn("/mnt/Flash/boot.sh </dev/null >/dev/null 2>&1 &", content)
        self.assertNotIn("/mnt/Flash/start-samba.sh", content)
        self.assertNotIn("/mnt/Flash/manager.sh", content)
        self.assertNotIn("/mnt/Flash/watchdog.sh", content)
        self.assertNotIn("pkill -0 -f /mnt/Flash/watchdog.sh", content)

    def test_rc_local_detaches_background_jobs_from_stdin(self) -> None:
        content = load_boot_asset_text("rc.local")
        self.assertIn("/mnt/Flash/boot.sh </dev/null >/dev/null 2>&1 &", content)

    def test_common_script_has_no_smbd_daemon_ready_helpers(self) -> None:
        common = load_boot_asset_text("common.sh")
        self.assertNotIn("get_smbd_log_path_from_config()", common)
        self.assertNotIn("wait_for_smbd_ready()", common)
        self.assertNotIn("daemon_ready", common)

    def test_mdns_advertiser_accepts_lowercase_wama_and_normalizes_output(self) -> None:
        if shutil.which("cc") is None:
            self.skipTest("cc not available")

        mdns_source = (REPO_ROOT / "build" / "mdns-advertiser.c").as_posix()
        source = '''
#include <stdio.h>
#define main mdns_advertiser_main
#include "{mdns_source}"
#undef main

int main(void) {{
    char out[256];
    if (build_adisk_system_txt(out, sizeof(out), "80:ea:96:e6:58:68") != 0) {{
        return 1;
    }}
    puts(out);
    return 0;
}}
'''.format(mdns_source=mdns_source)
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            c_path = tmp / "mdns_test.c"
            bin_path = tmp / "mdns_test"
            c_path.write_text(source)
            proc = subprocess.run(
                ["cc", "-Wall", "-Wextra", "-Werror", str(c_path), "-o", str(bin_path)],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            run = subprocess.run([str(bin_path)], capture_output=True, text=True, check=False)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(run.stdout.strip(), "sys=waMA=80:EA:96:E6:58:68,adVF=0x1010")

    def test_mdns_advertiser_adisk_disk_txt_defaults_to_cloned_advf(self) -> None:
        mdns_source = (REPO_ROOT / "build" / "mdns-advertiser.c").as_posix()
        source = '''
#include <stdio.h>
#define main mdns_advertiser_main
#include "{mdns_source}"
#undef main

int main(void) {{
    char out[256];
    if (build_adisk_disk_txt(out, sizeof(out), "dk2", "Data", "12345678-1234-1234-1234-123456789012", ADISK_DEFAULT_DISK_ADVF) != 0) {{
        return 1;
    }}
    puts(out);
    return 0;
}}
'''.format(mdns_source=mdns_source)
        run = self._compile_and_run_c_helper(source, "mdns_adisk_disk_txt_default_advf")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(
            run.stdout.strip(),
            "dk2=adVF=0x1093,adVN=Data,adVU=12345678-1234-1234-1234-123456789012",
        )

    def test_mdns_advertiser_adisk_disk_txt_accepts_time_machine_smb_advf(self) -> None:
        mdns_source = (REPO_ROOT / "build" / "mdns-advertiser.c").as_posix()
        source = '''
#include <stdio.h>
#define main mdns_advertiser_main
#include "{mdns_source}"
#undef main

int main(void) {{
    char out[256];
    if (build_adisk_disk_txt(out, sizeof(out), "dk2", "Data", "12345678-1234-1234-1234-123456789012", "0x82") != 0) {{
        return 1;
    }}
    puts(out);
    return 0;
}}
'''.format(mdns_source=mdns_source)
        run = self._compile_and_run_c_helper(source, "mdns_adisk_disk_txt_time_machine_advf")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(
            run.stdout.strip(),
            "dk2=adVF=0x82,adVN=Data,adVU=12345678-1234-1234-1234-123456789012",
        )

    def test_mdns_advertiser_rejects_extra_adisk_share_fields(self) -> None:
        mdns_source = (REPO_ROOT / "build" / "mdns-advertiser.c").as_posix()
        source = '''
#include <stdio.h>
#include <string.h>
#define main mdns_advertiser_main
#include "{mdns_source}"
#undef main

int main(void) {{
    struct config cfg;
    char path[] = "/tmp/tcapsulesmb-adisk-extra-XXXXXX";
    int fd;
    FILE *fp;
    int rc;

    memset(&cfg, 0, sizeof(cfg));
    fd = mkstemp(path);
    if (fd < 0) {{
        return 1;
    }}
    fp = fdopen(fd, "w");
    if (fp == NULL) {{
        close(fd);
        unlink(path);
        return 2;
    }}
    fputs("Data\\tdk2\\t12345678-1234-1234-1234-123456789012\\t0x1093\\textra\\n", fp);
    fclose(fp);

    rc = parse_adisk_shares_file(&cfg, path);
    unlink(path);
    return rc == 0 ? 3 : 0;
}}
'''.format(mdns_source=mdns_source)
        run = self._compile_and_run_c_helper(source, "mdns_adisk_extra_fields")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("has extra fields", run.stderr)

    def test_mdns_advertiser_adisk_argument_validation_respects_diskless_mode(self) -> None:
        mdns_source = (REPO_ROOT / "build" / "mdns-advertiser.c").as_posix()
        source = r'''
#define main mdns_advertiser_main
#include "@MDNS_SOURCE@"
#undef main

int main(int argc, char **argv) {
    return mdns_advertiser_main(argc, argv);
}
'''.replace("@MDNS_SOURCE@", mdns_source)
        adisk_uuid = "12345678-1234-1234-1234-123456789012"

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            shares_file = tmp / "adisk.tsv"
            shares_file.write_text(f"Data\tdk2\t{adisk_uuid}\t0x82\n")
            bad_shares_file = tmp / "bad-adisk.tsv"
            bad_shares_file.write_text("Data\tdk2\tbad\t0x82\n")

            def base_args(snapshot_name: str) -> list[str]:
                return [
                    "--save-airport-snapshot",
                    str(tmp / snapshot_name),
                    "--instance",
                    "Capsule",
                    "--host",
                    "capsule",
                    "--airport-wama",
                    "80:EA:96:E6:58:68",
                ]

            cases = [
                (
                    "no_adisk_config_does_not_require_adisk_sys_wama",
                    [],
                    0,
                    "",
                ),
                (
                    "diskful_adisk_shares_file_requires_adisk_sys_wama",
                    ["--adisk-shares-file", str(shares_file)],
                    7,
                    "",
                ),
                (
                    "diskful_adisk_shares_file_rejects_invalid_adisk_sys_wama",
                    ["--adisk-shares-file", str(shares_file), "--adisk-sys-wama", "not-a-mac"],
                    7,
                    "adisk sys waMA must be a MAC address",
                ),
                (
                    "diskful_adisk_shares_file_accepts_valid_adisk_sys_wama",
                    ["--adisk-shares-file", str(shares_file), "--adisk-sys-wama", "80:EA:96:E6:58:68"],
                    0,
                    "",
                ),
                (
                    "diskless_adisk_shares_file_suppresses_missing_adisk_sys_wama",
                    ["--diskless", "--adisk-shares-file", str(shares_file)],
                    0,
                    "",
                ),
                (
                    "diskless_adisk_shares_file_suppresses_invalid_adisk_sys_wama",
                    ["--diskless", "--adisk-shares-file", str(shares_file), "--adisk-sys-wama", "not-a-mac"],
                    0,
                    "",
                ),
                (
                    "diskless_still_validates_configured_adisk_disk_fields",
                    ["--diskless", "--adisk-shares-file", str(bad_shares_file)],
                    8,
                    "adisk uuid must be 36 characters",
                ),
            ]

            for label, extra_args, expected_rc, expected_stderr in cases:
                with self.subTest(label=label):
                    snapshot_name = f"{label}.txt"
                    run = self._compile_and_run_c_helper(
                        source,
                        f"mdns_adisk_args_{label}",
                        [*base_args(snapshot_name), *extra_args],
                    )
                    self.assertEqual(run.returncode, expected_rc, run.stderr)
                    if expected_stderr:
                        self.assertIn(expected_stderr, run.stderr)
                    if expected_rc == 0:
                        self.assertTrue((tmp / snapshot_name).exists())

    def test_mdns_advertiser_normalizes_airport_mac_fields_to_apple_style(self) -> None:
        mdns_source = (REPO_ROOT / "build" / "mdns-advertiser.c").as_posix()
        source = '''
#include <stdio.h>
#include <string.h>
#define main mdns_advertiser_main
#include "{mdns_source}"
#undef main

int main(void) {{
    struct config cfg;
    char out[256];

    memset(&cfg, 0, sizeof(cfg));
    snprintf(cfg.airport_wama, sizeof(cfg.airport_wama), "%s", "80:ea:96:e6:58:68");
    snprintf(cfg.airport_rama, sizeof(cfg.airport_rama), "%s", "80-ea-96-eb-2e-7d");
    snprintf(cfg.airport_ram2, sizeof(cfg.airport_ram2), "%s", "80:EA:96:EB:2E:7C");
    snprintf(cfg.airport_syap, sizeof(cfg.airport_syap), "%s", "119");

    if (build_airport_txt(out, sizeof(out), &cfg) != 0) {{
        return 1;
    }}
    puts(out);
    return 0;
}}
'''.format(mdns_source=mdns_source)
        run = self._compile_and_run_c_helper(source, "mdns_airport_txt_normalization")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(
            run.stdout.strip(),
            "waMA=80-EA-96-E6-58-68,raMA=80-EA-96-EB-2E-7D,raM2=80-EA-96-EB-2E-7C,syAP=119",
        )

    def test_mdns_advertiser_rejects_invalid_airport_mac_field(self) -> None:
        mdns_source = (REPO_ROOT / "build" / "mdns-advertiser.c").as_posix()
        source = '''
#include <stdio.h>
#include <string.h>
#define main mdns_advertiser_main
#include "{mdns_source}"
#undef main

int main(void) {{
    struct config cfg;
    char out[256];

    memset(&cfg, 0, sizeof(cfg));
    snprintf(cfg.airport_wama, sizeof(cfg.airport_wama), "%s", "80:ea:96:e6:58");
    snprintf(cfg.airport_syap, sizeof(cfg.airport_syap), "%s", "119");

    if (build_airport_txt(out, sizeof(out), &cfg) == 0) {{
        return 1;
    }}
    return 0;
}}
'''.format(mdns_source=mdns_source)
        run = self._compile_and_run_c_helper(source, "mdns_airport_txt_invalid_mac")
        self.assertEqual(run.returncode, 0, run.stderr)

    def test_mdns_advertiser_sets_cache_flush_for_unique_records_only(self) -> None:
        mdns_source = (REPO_ROOT / "build" / "mdns-advertiser.c").as_posix()
        source = '''
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/types.h>

ssize_t fake_sendto(int sockfd, const void *buf, size_t len, int flags,
                    const struct sockaddr *dest, socklen_t dest_len);

#define sendto fake_sendto
#define main mdns_advertiser_main
#include "{mdns_source}"
#undef main
#undef sendto

static unsigned char captured_packet[BUF_SIZE];
static size_t captured_len = 0;

ssize_t fake_sendto(int sockfd, const void *buf, size_t len, int flags,
                    const struct sockaddr *dest, socklen_t dest_len) {{
    (void)sockfd;
    (void)flags;
    (void)dest;
    (void)dest_len;
    memcpy(captured_packet, buf, len);
    captured_len = len;
    return (ssize_t)len;
}}

static int read_first_rr_class(const unsigned char *packet, size_t packet_len, unsigned short *out_class) {{
    char name[MAX_NAME];
    size_t cursor = 0;
    unsigned short rrtype;
    unsigned short rrclass;

    if (decode_name(packet, packet_len, &cursor, name, sizeof(name)) != 0 || cursor + 10 > packet_len) {{
        return -1;
    }}
    memcpy(&rrtype, packet + cursor, 2);
    memcpy(&rrclass, packet + cursor + 2, 2);
    (void)rrtype;
    *out_class = ntohs(rrclass);
    return 0;
}}

static int read_first_question_class(const unsigned char *packet, size_t packet_len, unsigned short *out_class) {{
    struct dns_header hdr;
    char name[MAX_NAME];
    size_t cursor = sizeof(hdr);
    unsigned short qtype;
    unsigned short qclass;

    if (packet_len < sizeof(hdr)) {{
        return -1;
    }}
    if (decode_name(packet, packet_len, &cursor, name, sizeof(name)) != 0 || cursor + 4 > packet_len) {{
        return -1;
    }}
    memcpy(&qtype, packet + cursor, 2);
    memcpy(&qclass, packet + cursor + 2, 2);
    (void)qtype;
    *out_class = ntohs(qclass);
    return 0;
}}

int main(void) {{
    uint8_t buf[BUF_SIZE];
    size_t off;
    unsigned short rrclass;
    uint32_t ipv4;
    const char *txts[1] = {{"k=v"}};
    struct sockaddr_in dest;

    off = 0;
    if (add_rr_ptr(buf, &off, sizeof(buf), "_smb._tcp.local.", "Home._smb._tcp.local.", 120) != 0 ||
        read_first_rr_class(buf, off, &rrclass) != 0 ||
        rrclass != DNS_CLASS_IN) {{
        return 1;
    }}

    off = 0;
    if (add_rr_srv(buf, &off, sizeof(buf), "Home._smb._tcp.local.", "home.local.", 445, 120) != 0 ||
        read_first_rr_class(buf, off, &rrclass) != 0 ||
        rrclass != DNS_CLASS_IN_UNIQUE) {{
        return 2;
    }}

    off = 0;
    if (add_rr_txt_empty(buf, &off, sizeof(buf), "Home._smb._tcp.local.", 120) != 0 ||
        read_first_rr_class(buf, off, &rrclass) != 0 ||
        rrclass != DNS_CLASS_IN_UNIQUE) {{
        return 3;
    }}

    off = 0;
    if (add_rr_txt_items(buf, &off, sizeof(buf), "Home._adisk._tcp.local.", 120, txts, NULL, 1) != 0 ||
        read_first_rr_class(buf, off, &rrclass) != 0 ||
        rrclass != DNS_CLASS_IN_UNIQUE) {{
        return 4;
    }}

    if (inet_pton(AF_INET, "10.0.1.1", &ipv4) != 1) {{
        return 5;
    }}
    off = 0;
    if (add_rr_a(buf, &off, sizeof(buf), "home.local.", ipv4, 120) != 0 ||
        read_first_rr_class(buf, off, &rrclass) != 0 ||
        rrclass != DNS_CLASS_IN_UNIQUE) {{
        return 6;
    }}

    memset(&dest, 0, sizeof(dest));
    dest.sin_family = AF_INET;
    dest.sin_port = htons(5353);
    dest.sin_addr.s_addr = inet_addr("224.0.0.251");
    if (send_query_question_any(1, (const struct sockaddr *)&dest, sizeof(dest), "home.local.", DNS_TYPE_A) != 0 ||
        read_first_question_class(captured_packet, captured_len, &rrclass) != 0 ||
        rrclass != DNS_CLASS_IN) {{
        return 7;
    }}

    return 0;
}}
'''.format(mdns_source=mdns_source)
        run = self._compile_and_run_c_helper(source, "mdns_cache_flush_classes")
        self.assertEqual(run.returncode, 0, run.stderr)

    def test_mdns_advertiser_no_args_returns_usage_without_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            bin_path = self._compile_mdns_advertiser_binary(Path(tmpdir))
            run = subprocess.run([str(bin_path)], capture_output=True, text=True, check=False)
        self.assertEqual(run.returncode, 4)
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
        self.assertEqual(run.stdout, "2106\n")
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
        self.assertEqual(run.stdout, "2106\n")
        self.assertEqual(run.stderr, "")

    def test_mdns_advertiser_traffic_summary_counters_are_debug_only(self) -> None:
        mdns_source = (REPO_ROOT / "build" / "mdns-advertiser.c").as_posix()
        source = f'''
#include <string.h>
#define main mdns_advertiser_main
#include "{mdns_source}"
#undef main

int main(void) {{
    memset(&g_mdns_counters, 0, sizeof(g_mdns_counters));
    memset(&g_mdns_counter_log_state, 0, sizeof(g_mdns_counter_log_state));
    g_debug_logging = 0;
    g_mdns_counters.ipv4_packets_received = 1;
    maybe_log_mdns_counters("traffic_summary", 1000);
    if (g_mdns_counter_log_state.last_log_ms != 0) {{
        return 1;
    }}

    g_debug_logging = 1;
    maybe_log_mdns_counters("traffic_summary", 1000);
    if (g_mdns_counter_log_state.last_log_ms != 1000) {{
        return 2;
    }}

    maybe_log_mdns_counters("traffic_summary", 2000);
    if (g_mdns_counter_log_state.last_log_ms != 1000) {{
        return 3;
    }}

    g_mdns_counters.ipv4_packets_received = 2;
    maybe_log_mdns_counters("traffic_summary", 32000);
    return g_mdns_counter_log_state.last_log_ms == 32000 ? 0 : 4;
}}
'''
        run = self._compile_and_run_c_helper(source, "mdns_debug_counter_logging")
        self.assertEqual(run.returncode, 0, run.stderr)

    def test_mdns_timestamped_logging_truncates_long_lines_without_heap(self) -> None:
        mdns_source = (REPO_ROOT / "build" / "mdns-advertiser.c").as_posix()
        source = f'''
#include <string.h>
#define main mdns_advertiser_main
#include "{mdns_source}"
#undef main

int main(void) {{
    char message[5001];
    memset(message, 'A', sizeof(message) - 1);
    message[sizeof(message) - 1] = '\\0';
    timestamped_fprintf(stderr, "%s\\n", message);
    return 0;
}}
'''
        run = self._compile_and_run_c_helper(source, "mdns_long_timestamped_log")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertNotIn("A" * 5000, run.stderr)
        self.assertGreaterEqual(run.stderr.count("A"), 4000)
        self.assertLess(run.stderr.count("A"), 5000)
        self.assertTrue(run.stderr.endswith("\n"))

    def test_mdns_advertiser_can_skip_capture_when_snapshot_is_newer_than_boot(self) -> None:
        if sys.platform != "darwin" and not sys.platform.startswith("netbsd"):
            self.skipTest("snapshot freshness check requires BSD KERN_BOOTTIME")

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            bin_path = self._compile_mdns_advertiser_binary(tmp)
            all_snapshot = tmp / "allmdns.txt"
            apple_snapshot = tmp / "applemdns.txt"
            apple_snapshot.write_text("trusted\n")
            run = subprocess.run(
                [
                    str(bin_path),
                    "--auto-ip",
                    "--save-all-snapshot",
                    str(all_snapshot),
                    "--save-snapshot",
                    str(apple_snapshot),
                    "--skip-capture-if-snapshot-newer-than-boot",
                    str(apple_snapshot),
                    "--airport-wama",
                    "80:EA:96:E6:58:68",
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=2,
            )

            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertIn("mDNS snapshot capture skipped;", run.stderr)
            self.assertIn("is newer than current boot", run.stderr)
            self.assertIn("exiting without UDP 5353 takeover or advertisement", run.stderr)
            self.assertFalse(all_snapshot.exists())
            self.assertEqual(apple_snapshot.read_text(), "trusted\n")

    def test_mdns_advertiser_reports_snapshot_newer_than_boot(self) -> None:
        if sys.platform != "darwin" and not sys.platform.startswith("netbsd"):
            self.skipTest("snapshot freshness check requires BSD KERN_BOOTTIME")

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            bin_path = self._compile_mdns_advertiser_binary(tmp)
            apple_snapshot = tmp / "applemdns.txt"
            apple_snapshot.write_text("trusted\n")
            run = subprocess.run(
                [
                    str(bin_path),
                    "--snapshot-newer-than-boot",
                    str(apple_snapshot),
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=2,
            )

            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertIn("mDNS snapshot is newer than current boot:", run.stderr)
            self.assertEqual(run.stdout, "")

    def test_mdns_advertiser_reports_missing_snapshot_as_not_fresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            bin_path = self._compile_mdns_advertiser_binary(tmp)
            run = subprocess.run(
                [
                    str(bin_path),
                    "--snapshot-newer-than-boot",
                    str(tmp / "missing-applemdns.txt"),
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=2,
            )

            self.assertEqual(run.returncode, 14, run.stderr)
            self.assertIn("mDNS snapshot is missing, empty, or not newer than current boot:", run.stderr)
            self.assertEqual(run.stdout, "")

    def test_mdns_advertiser_reports_empty_snapshot_as_not_fresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            bin_path = self._compile_mdns_advertiser_binary(tmp)
            apple_snapshot = tmp / "applemdns.txt"
            apple_snapshot.touch()
            run = subprocess.run(
                [
                    str(bin_path),
                    "--snapshot-newer-than-boot",
                    str(apple_snapshot),
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=2,
            )

            self.assertEqual(run.returncode, 14, run.stderr)
            self.assertIn("mDNS snapshot is missing, empty, or not newer than current boot:", run.stderr)
            self.assertEqual(run.stdout, "")

    def test_mdns_advertiser_reports_stale_snapshot_as_not_fresh(self) -> None:
        if sys.platform != "darwin" and not sys.platform.startswith("netbsd"):
            self.skipTest("snapshot freshness check requires BSD KERN_BOOTTIME")

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            bin_path = self._compile_mdns_advertiser_binary(tmp)
            apple_snapshot = tmp / "applemdns.txt"
            apple_snapshot.write_text("trusted\n")
            os.utime(apple_snapshot, (1, 1))
            run = subprocess.run(
                [
                    str(bin_path),
                    "--snapshot-newer-than-boot",
                    str(apple_snapshot),
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=2,
            )

            self.assertEqual(run.returncode, 14, run.stderr)
            self.assertIn("mDNS snapshot is missing, empty, or not newer than current boot:", run.stderr)
            self.assertEqual(run.stdout, "")

    def test_mdns_advertiser_save_airport_snapshot_generates_one_record_without_takeover(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            bin_path = self._compile_mdns_advertiser_binary(tmp)
            apple_snapshot = tmp / "applemdns.txt"
            run = subprocess.run(
                [
                    str(bin_path),
                    "--save-airport-snapshot",
                    str(apple_snapshot),
                    "--instance",
                    "James's AirPort Time Capsule",
                    "--host",
                    "jamess-airport-time-capsule",
                    "--airport-wama",
                    "80:EA:96:E6:58:68",
                    "--airport-rama",
                    "80:EA:96:EB:2E:7D",
                    "--airport-ram2",
                    "80:EA:96:EB:2E:7C",
                    "--airport-rast",
                    "3",
                    "--airport-rana",
                    "0",
                    "--airport-syfl",
                    "0xA0C",
                    "--airport-syap",
                    "119",
                    "--airport-syvs",
                    "7.9.1",
                    "--airport-srcv",
                    "79100.2",
                    "--airport-bjsd",
                    "16",
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=2,
            )
            content = apple_snapshot.read_text()

        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("airport snapshot: wrote 1 record", run.stderr)
        self.assertIn("mdns capture-only:", run.stderr)
        self.assertNotIn("mDNS takeover", run.stderr)
        self.assertEqual(content.count("BEGIN\n"), 1)
        self.assertIn("TYPE=_airport._tcp.local.\n", content)
        self.assertIn("INSTANCE=James's AirPort Time Capsule\n", content)
        self.assertIn(f"HOST_HEX={'jamess-airport-time-capsule.local.'.encode().hex()}\n", content)
        self.assertIn("PORT=5009\n", content)
        self.assertIn(
            "TXT=waMA=80-EA-96-E6-58-68,raMA=80-EA-96-EB-2E-7D,raM2=80-EA-96-EB-2E-7C,"
            "raSt=3,raNA=0,syFl=0xA0C,syAP=119,syVs=7.9.1,srcv=79100.2,bjSd=16\n",
            content,
        )

    def test_mdns_advertiser_load_arg_requires_advertising_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            bin_path = self._compile_mdns_advertiser_binary(tmp)
            snapshot = tmp / "applemdns.txt"
            self._write_matching_airport_snapshot(snapshot)
            run = subprocess.run(
                [str(bin_path), "--load-snapshot", str(snapshot)],
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(run.returncode, 4)
        self.assertIn("Usage:", run.stderr)
        self.assertNotIn("snapshot load:", run.stderr)

    def test_mdns_advertiser_capture_only_validates_optional_dns_labels(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            bin_path = self._compile_mdns_advertiser_binary(tmp)
            run = subprocess.run(
                [
                    str(bin_path),
                    "--save-snapshot",
                    str(tmp / "applemdns.txt"),
                    "--host",
                    "bad.host",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(run.returncode, 5)
        self.assertIn("host label must not contain dots", run.stderr)
        self.assertNotIn("mdns capture-only:", run.stderr)

    def test_mdns_advertiser_snapshot_capture_requires_auto_ip(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            bin_path = self._compile_mdns_advertiser_binary(tmp)
            run = subprocess.run(
                [
                    str(bin_path),
                    "--save-snapshot",
                    str(tmp / "applemdns.txt"),
                    "--host",
                    "timecapsule",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(run.returncode, 4)
        self.assertIn("mDNS snapshot capture requires --auto-ip", run.stderr)
        self.assertNotIn("mdns capture-only:", run.stderr)

    def test_mdns_advertiser_auto_ip_airport_snapshot_does_not_require_ipv4(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            bin_path = self._compile_mdns_advertiser_binary(tmp)
            snapshot = tmp / "airport.txt"
            run = subprocess.run(
                [
                    str(bin_path),
                    "--auto-ip",
                    "--save-airport-snapshot",
                    str(snapshot),
                    "--instance",
                    "TimeCapsule",
                    "--host",
                    "timecapsule",
                    "--airport-wama",
                    "80:EA:96:E6:58:68",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            snapshot_exists = snapshot.exists()
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("airport snapshot: wrote 1 record", run.stderr)
        self.assertTrue(snapshot_exists)

    def test_mdns_auto_ip_helpers_filter_and_detect_interface_changes(self) -> None:
        mdns_source = (REPO_ROOT / "build" / "mdns-advertiser.c").as_posix()
        source = '''
#include <arpa/inet.h>
#include <string.h>
#define main mdns_advertiser_main
#include "{mdns_source}"
#undef main

int main(void) {{
    struct iface_context_set a;
    struct iface_context_set b;
    char synthetic_name[IFNAMSIZ];

    if (runtime_ipv4_is_usable(inet_addr("0.1.2.3")) ||
        runtime_ipv4_is_usable(inet_addr("127.0.0.1")) ||
        runtime_ipv4_is_usable(inet_addr("169.254.1.9")) ||
        runtime_ipv4_is_usable(inet_addr("224.0.0.1")) ||
        runtime_ipv4_is_usable(inet_addr("240.0.0.1")) ||
        runtime_ipv4_is_usable(inet_addr("255.255.255.255")) ||
        runtime_ipv4_is_usable(0) ||
        !runtime_ipv4_is_usable(inet_addr("10.0.1.1"))) {{
        return 1;
    }}
    if (!iface_flags_are_usable(IFF_UP | IFF_RUNNING, 1) ||
        iface_flags_are_usable(IFF_UP, 1) ||
        !iface_flags_are_usable(IFF_UP, 0) ||
        iface_flags_are_usable(IFF_UP | IFF_LOOPBACK, 0) ||
        iface_flags_are_usable(IFF_RUNNING, 0)) {{
        return 2;
    }}
    synthetic_ipv4_ifaddrs_name(synthetic_name, sizeof(synthetic_name), inet_addr("192.168.100.100"));
    if (strcmp(synthetic_name, "ip4-c0a86464") != 0) {{
        return 7;
    }}
    synthetic_ipv4_ifaddrs_name(synthetic_name, sizeof(synthetic_name), inet_addr("255.255.255.255"));
    if (strcmp(synthetic_name, "ip4-ffffffff") != 0 || strlen(synthetic_name) >= IFNAMSIZ) {{
        return 8;
    }}

    memset(&a, 0, sizeof(a));
    memset(&b, 0, sizeof(b));
    append_iface_context(&a, "bridge0", inet_addr("10.0.1.1"), inet_addr("255.255.255.0"), IFF_UP | IFF_RUNNING);
    append_iface_context(&b, "bridge0", inet_addr("10.0.1.1"), inet_addr("255.255.255.0"), IFF_UP | IFF_RUNNING);
    if (!iface_context_sets_equal(&a, &b)) {{
        return 3;
    }}
    append_iface_context(&b, "bcmeth0", inet_addr("192.168.1.217"), inet_addr("255.255.255.0"), IFF_UP | IFF_RUNNING);
    if (iface_context_sets_equal(&a, &b)) {{
        return 4;
    }}
    b = a;
    b.contexts[0].netmask = inet_addr("255.255.0.0");
    if (iface_context_sets_equal(&a, &b)) {{
        return 5;
    }}
    b = a;
    b.count = 0;
    if (iface_context_sets_equal(&a, &b)) {{
        return 6;
    }}

    memset(&a, 0, sizeof(a));
    memset(&b, 0, sizeof(b));
    append_iface_context(&a, "bridge0", inet_addr("10.0.1.1"), inet_addr("255.255.255.0"), IFF_UP | IFF_RUNNING);
    append_iface_context(&a, "bcmeth0", inet_addr("192.168.1.217"), inet_addr("255.255.255.0"), IFF_UP | IFF_RUNNING);
    append_iface_context(&b, "bcmeth0", inet_addr("192.168.1.217"), inet_addr("255.255.255.0"), IFF_UP | IFF_RUNNING);
    append_iface_context(&b, "bridge0", inet_addr("10.0.1.1"), inet_addr("255.255.255.0"), IFF_UP | IFF_RUNNING);
    if (!iface_context_sets_equal(&a, &b)) {{
        return 10;
    }}

    memset(&a, 0, sizeof(a));
    append_iface_context(&a, "ppp0", inet_addr("10.0.1.2"), inet_addr("255.255.255.0"), IFF_UP | IFF_RUNNING);
    append_iface_context(&a, "bridge0", inet_addr("203.0.113.5"), inet_addr("255.255.255.0"), IFF_UP | IFF_RUNNING);
    append_iface_context(&a, "en0", inet_addr("192.168.1.2"), inet_addr("255.255.255.0"), IFF_UP | IFF_RUNNING);
    append_iface_context(&a, "br1", inet_addr("192.168.1.3"), inet_addr("255.255.255.0"), IFF_UP | IFF_RUNNING);
    append_iface_context(&a, "br0", inet_addr("192.168.1.4"), inet_addr("255.255.255.0"), IFF_UP | IFF_RUNNING);
    sort_iface_contexts(&a);
    if (strcmp(a.contexts[0].name, "br0") != 0 ||
        strcmp(a.contexts[1].name, "br1") != 0 ||
        strcmp(a.contexts[2].name, "en0") != 0 ||
        strcmp(a.contexts[3].name, "bridge0") != 0 ||
        strcmp(a.contexts[4].name, "ppp0") != 0) {{
        return 11;
    }}
    return 0;
}}
'''.format(mdns_source=mdns_source)
        run = self._compile_and_run_c_helper(source, "mdns_auto_ip_helpers")
        self.assertEqual(run.returncode, 0, run.stderr)

    def test_mdns_auto_ip_cidr_helpers_format_valid_bind_output(self) -> None:
        mdns_source = (REPO_ROOT / "build" / "mdns-advertiser.c").as_posix()
        source = '''
#include <arpa/inet.h>
#include <string.h>
#define main mdns_advertiser_main
#include "{mdns_source}"
#undef main

int main(void) {{
    struct iface_context_set set;
    char cidr[INET_ADDRSTRLEN + 4];
    struct in6_addr mask6;

    if (netmask_prefix_length(inet_addr("255.255.255.0")) != 24 ||
        netmask_prefix_length(inet_addr("255.255.0.0")) != 16 ||
        netmask_prefix_length(0) != 24 ||
        netmask_prefix_length(inet_addr("255.0.255.0")) != 24) {{
        return 1;
    }}
    if (netmask_prefix_length(ipv4_link_local_netmask()) != 16) {{
        return 6;
    }}
    if (inet_pton(AF_INET6, "ffff:ffff:ffff:ffff::", &mask6) != 1 ||
        ipv6_prefix_length_from_mask(&mask6) != 64) {{
        return 7;
    }}
    memset(&mask6, 0, sizeof(mask6));
    if (ipv6_prefix_length_from_mask(&mask6) != -1) {{
        return 8;
    }}
    if (inet_pton(AF_INET6, "ffff:ffff::ffff", &mask6) != 1 ||
        ipv6_prefix_length_from_mask(&mask6) != -1) {{
        return 9;
    }}

    memset(&set, 0, sizeof(set));
    if (print_iface_context_cidrs(stdout, &set) == 0) {{
        return 5;
    }}
    append_iface_context(&set, "bridge0", inet_addr("10.0.1.1"), inet_addr("255.255.255.0"), IFF_UP | IFF_RUNNING);
    append_iface_context(&set, "bcmeth0", inet_addr("192.168.1.40"), 0, IFF_UP | IFF_RUNNING);
    append_iface_context(&set, "lo0", inet_addr("127.0.0.1"), inet_addr("255.0.0.0"), IFF_UP | IFF_RUNNING);
    append_iface_context(&set, "ll0", inet_addr("169.254.1.9"), inet_addr("255.255.0.0"), IFF_UP | IFF_RUNNING);
    append_iface_context(&set, "zero0", inet_addr("0.1.2.3"), inet_addr("255.0.0.0"), IFF_UP | IFF_RUNNING);
    append_iface_context(&set, "mcast0", inet_addr("224.0.0.1"), inet_addr("255.255.255.0"), IFF_UP | IFF_RUNNING);
    append_iface_context(&set, "reserved0", inet_addr("240.0.0.1"), inet_addr("255.255.255.0"), IFF_UP | IFF_RUNNING);
    append_iface_context(&set, "broadcast0", inet_addr("255.255.255.255"), inet_addr("255.255.255.255"), IFF_UP | IFF_RUNNING);
    if (set.count != 2) {{
        return 2;
    }}
    if (iface_context_cidr(cidr, sizeof(cidr), &set.contexts[1]) != 0 || strcmp(cidr, "192.168.1.40/24") != 0) {{
        return 3;
    }}
    if (print_iface_context_cidrs(stdout, &set) != 0) {{
        return 4;
    }}
    return 0;
}}
'''.format(mdns_source=mdns_source)
        run = self._compile_and_run_c_helper(source, "mdns_auto_ip_cidrs")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout, "10.0.1.1/24 192.168.1.40/24\n")

    def test_auto_ip_context_collection_uses_getifaddrs_netmasks(self) -> None:
        mdns_source = (REPO_ROOT / "build" / "mdns-advertiser.c").as_posix()
        source = '''
#include <arpa/inet.h>
#include <ifaddrs.h>
#include <string.h>

static int fake_getifaddrs(struct ifaddrs **out);
static void fake_freeifaddrs(struct ifaddrs *list);

#define getifaddrs fake_getifaddrs
#define freeifaddrs fake_freeifaddrs
#define main mdns_advertiser_main
#include "{mdns_source}"
#undef main
#undef getifaddrs
#undef freeifaddrs

static struct ifaddrs fake_ifas[4];
static struct sockaddr_in fake_addrs[3];
static struct sockaddr_in fake_masks[3];

static void set_ipv4_sockaddr(struct sockaddr_in *sin, const char *addr) {{
    memset(sin, 0, sizeof(*sin));
#if defined(__NetBSD__) || defined(__APPLE__) || defined(__FreeBSD__) || defined(__OpenBSD__) || defined(__DragonFly__)
    sin->sin_len = sizeof(*sin);
#endif
    sin->sin_family = AF_INET;
    sin->sin_addr.s_addr = inet_addr(addr);
}}

static int fake_getifaddrs(struct ifaddrs **out) {{
    memset(fake_ifas, 0, sizeof(fake_ifas));
    memset(fake_addrs, 0, sizeof(fake_addrs));
    memset(fake_masks, 0, sizeof(fake_masks));

    set_ipv4_sockaddr(&fake_addrs[0], "10.0.1.1");
    set_ipv4_sockaddr(&fake_masks[0], "255.0.0.0");
    fake_ifas[0].ifa_next = &fake_ifas[1];
    fake_ifas[0].ifa_name = "bridge0";
    fake_ifas[0].ifa_flags = IFF_UP | IFF_RUNNING;
    fake_ifas[0].ifa_addr = (struct sockaddr *)(void *)&fake_addrs[0];
    fake_ifas[0].ifa_netmask = (struct sockaddr *)(void *)&fake_masks[0];

    set_ipv4_sockaddr(&fake_addrs[1], "192.168.1.217");
    set_ipv4_sockaddr(&fake_masks[1], "255.255.255.0");
    fake_ifas[1].ifa_next = &fake_ifas[2];
    fake_ifas[1].ifa_name = "bcmeth1";
    fake_ifas[1].ifa_flags = IFF_UP | IFF_RUNNING;
    fake_ifas[1].ifa_addr = (struct sockaddr *)(void *)&fake_addrs[1];
    fake_ifas[1].ifa_netmask = (struct sockaddr *)(void *)&fake_masks[1];

    set_ipv4_sockaddr(&fake_addrs[2], "10.2.3.4");
    set_ipv4_sockaddr(&fake_masks[2], "255.0.0.0");
    fake_ifas[2].ifa_next = NULL;
    fake_ifas[2].ifa_name = "down0";
    fake_ifas[2].ifa_flags = IFF_UP;
    fake_ifas[2].ifa_addr = (struct sockaddr *)(void *)&fake_addrs[2];
    fake_ifas[2].ifa_netmask = (struct sockaddr *)(void *)&fake_masks[2];

    *out = &fake_ifas[0];
    return 0;
}}

static void fake_freeifaddrs(struct ifaddrs *list) {{
    (void)list;
}}

int main(void) {{
    struct iface_context_set iface_contexts;
    struct link_context_set link_contexts;
    char cidr[INET_ADDRSTRLEN + 4];

    if (collect_usable_iface_contexts(&iface_contexts) != 0 || iface_contexts.count != 2) {{
        return 1;
    }}
    if (strcmp(iface_contexts.contexts[0].name, "bridge0") != 0 ||
        iface_contexts.contexts[0].ipv4_addr != inet_addr("10.0.1.1") ||
        iface_contexts.contexts[0].netmask != inet_addr("255.0.0.0")) {{
        return 2;
    }}
    if (source_matches_context_subnet(inet_addr("10.44.55.66"), &iface_contexts.contexts[0]) != 1) {{
        return 3;
    }}
    if (source_matches_context_subnet(inet_addr("11.0.1.3"), &iface_contexts.contexts[0]) != 0) {{
        return 4;
    }}
    if (collect_usable_link_contexts(&link_contexts) != 0 || link_contexts.count != 2) {{
        return 5;
    }}
    if (link_context_ipv4_cidr(cidr, sizeof(cidr), &link_contexts.links[0].ipv4[0]) != 0 ||
        strcmp(cidr, "10.0.1.1/8") != 0) {{
        return 6;
    }}
    return 0;
}}
'''.format(mdns_source=mdns_source)
        run = self._compile_and_run_c_helper(source, "auto_ip_getifaddrs_netmasks")
        self.assertEqual(run.returncode, 0, run.stderr)

    def test_auto_ip_getifaddrs_handles_unnamed_netbsd4_address_entries(self) -> None:
        mdns_source = (REPO_ROOT / "build" / "mdns-advertiser.c").as_posix()
        source = '''
#include <arpa/inet.h>
#include <ifaddrs.h>
#include <string.h>

static int fake_getifaddrs(struct ifaddrs **out);
static void fake_freeifaddrs(struct ifaddrs *list);

#define getifaddrs fake_getifaddrs
#define freeifaddrs fake_freeifaddrs
#define main mdns_advertiser_main
#include "{mdns_source}"
#undef main
#undef getifaddrs
#undef freeifaddrs

static struct ifaddrs fake_ifas[3];
static struct sockaddr_in fake_addrs[3];
static struct sockaddr_in fake_masks[3];

static void set_ipv4_sockaddr(struct sockaddr_in *sin, const char *addr, int family) {{
    memset(sin, 0, sizeof(*sin));
#if defined(__NetBSD__) || defined(__APPLE__) || defined(__FreeBSD__) || defined(__OpenBSD__) || defined(__DragonFly__)
    sin->sin_len = sizeof(*sin);
#endif
    sin->sin_family = family;
    sin->sin_addr.s_addr = inet_addr(addr);
}}

static int fake_getifaddrs(struct ifaddrs **out) {{
    memset(fake_ifas, 0, sizeof(fake_ifas));
    memset(fake_addrs, 0, sizeof(fake_addrs));
    memset(fake_masks, 0, sizeof(fake_masks));

    set_ipv4_sockaddr(&fake_addrs[0], "192.168.1.217", AF_INET);
    set_ipv4_sockaddr(&fake_masks[0], "255.255.255.0", 0);
    fake_ifas[0].ifa_next = &fake_ifas[1];
    fake_ifas[0].ifa_name = "";
    fake_ifas[0].ifa_flags = IFF_UP | IFF_RUNNING;
    fake_ifas[0].ifa_addr = (struct sockaddr *)(void *)&fake_addrs[0];
    fake_ifas[0].ifa_netmask = (struct sockaddr *)(void *)&fake_masks[0];

    set_ipv4_sockaddr(&fake_addrs[1], "10.0.1.1", AF_INET);
    set_ipv4_sockaddr(&fake_masks[1], "255.0.0.0", 0);
    fake_ifas[1].ifa_next = &fake_ifas[2];
    fake_ifas[1].ifa_name = "";
    fake_ifas[1].ifa_flags = IFF_UP | IFF_RUNNING;
    fake_ifas[1].ifa_addr = (struct sockaddr *)(void *)&fake_addrs[1];
    fake_ifas[1].ifa_netmask = (struct sockaddr *)(void *)&fake_masks[1];

    set_ipv4_sockaddr(&fake_addrs[2], "169.254.155.207", AF_INET);
    set_ipv4_sockaddr(&fake_masks[2], "255.255.0.0", 0);
    fake_ifas[2].ifa_next = NULL;
    fake_ifas[2].ifa_name = "";
    fake_ifas[2].ifa_flags = IFF_UP | IFF_RUNNING;
    fake_ifas[2].ifa_addr = (struct sockaddr *)(void *)&fake_addrs[2];
    fake_ifas[2].ifa_netmask = (struct sockaddr *)(void *)&fake_masks[2];

    *out = &fake_ifas[0];
    return 0;
}}

static void fake_freeifaddrs(struct ifaddrs *list) {{
    (void)list;
}}

int main(void) {{
    struct iface_context_set iface_contexts;
    struct link_context_set link_contexts;
    const struct iface_context *ten_iface = NULL;
    const struct link_context *ten_link = NULL;
    char cidr[INET_ADDRSTRLEN + 4];
    size_t i;

    if (collect_usable_iface_contexts(&iface_contexts) != 0 || iface_contexts.count != 2) {{
        return 1;
    }}
    for (i = 0; i < iface_contexts.count; i++) {{
        if (iface_contexts.contexts[i].ipv4_addr == inet_addr("10.0.1.1")) {{
            ten_iface = &iface_contexts.contexts[i];
        }}
    }}
    if (ten_iface == NULL ||
        strcmp(ten_iface->name, "") == 0 ||
        ten_iface->netmask != inet_addr("255.0.0.0")) {{
        return 2;
    }}
    if (collect_usable_link_contexts(&link_contexts) != 0 || link_contexts.count != 3) {{
        return 3;
    }}
    for (i = 0; i < link_contexts.count; i++) {{
        if (link_contexts.links[i].ipv4_count > 0 &&
            link_contexts.links[i].ipv4[0].addr == inet_addr("10.0.1.1")) {{
            ten_link = &link_contexts.links[i];
        }}
    }}
    if (ten_link == NULL) {{
        return 4;
    }}
    if (source_matches_link_ipv4_subnet(inet_addr("10.44.55.66"), ten_link) != 1) {{
        return 5;
    }}
    if (link_ipv4_source_for_peer(ten_link, inet_addr("10.44.55.66")) != inet_addr("10.0.1.1")) {{
        return 6;
    }}
    if (link_context_ipv4_cidr(cidr, sizeof(cidr), &ten_link->ipv4[0]) != 0 ||
        strcmp(cidr, "10.0.1.1/8") != 0) {{
        return 7;
    }}
    return 0;
}}
'''.format(mdns_source=mdns_source)
        run = self._compile_and_run_c_helper(source, "auto_ip_getifaddrs_unnamed_entries")
        self.assertEqual(run.returncode, 0, run.stderr)

    def test_mdns_smb_bind_tokens_and_host_records_are_link_scoped_dual_stack(self) -> None:
        mdns_source = (REPO_ROOT / "build" / "mdns-advertiser.c").as_posix()
        source = '''
#include <arpa/inet.h>
#include <string.h>
#define main mdns_advertiser_main
#include "{mdns_source}"
#undef main

static int buffer_contains(const uint8_t *buf, size_t len, const void *needle, size_t needle_len) {{
    size_t i;
    for (i = 0; i + needle_len <= len; i++) {{
        if (memcmp(buf + i, needle, needle_len) == 0) {{
            return 1;
        }}
    }}
    return 0;
}}

int main(void) {{
    struct link_context_set set;
    struct in6_addr ula;
    struct in6_addr unknown_prefix;
    struct in6_addr ll;
    uint8_t packet[512];
    size_t off;
    int answers;

    memset(&set, 0, sizeof(set));
    if (inet_pton(AF_INET6, "fdbb:1111:2222:3333::40", &ula) != 1 ||
        inet_pton(AF_INET6, "fdbb:1111:2222:3333::41", &unknown_prefix) != 1 ||
        inet_pton(AF_INET6, "fe80::40", &ll) != 1) {{
        return 1;
    }}
    append_link_ipv4(&set, "bridge0", inet_addr("10.0.1.1"), inet_addr("255.255.255.0"), IFF_UP | IFF_RUNNING);
    append_link_ipv4(&set, "bridge0", inet_addr("169.254.1.9"), inet_addr("255.255.0.0"), IFF_UP | IFF_RUNNING);
    append_link_ipv4(&set, "lo0", inet_addr("127.0.0.1"), inet_addr("255.0.0.0"), IFF_UP | IFF_RUNNING);
    append_link_ipv6(&set, "bridge0", &ula, 64, 7, IFF_UP | IFF_RUNNING);
    append_link_ipv6(&set, "bridge0", &unknown_prefix, -1, 7, IFF_UP | IFF_RUNNING);
    append_link_ipv6(&set, "bridge0", &ll, 64, 7, IFF_UP | IFF_RUNNING);

    if (set.count != 1 || set.links[0].ipv4_count != 2 || set.links[0].ipv6_count != 3) {{
        return 2;
    }}
    if (print_smb_link_bind_tokens(stdout, &set) != 0) {{
        return 3;
    }}

    memset(packet, 0, sizeof(packet));
    off = 0;
    answers = 0;
    if (append_host_address_records(packet, &off, sizeof(packet), "timecapsule.local.", &set.links[0], 1, 1, 120, &answers) != 0) {{
        return 4;
    }}
    if (answers != 3 ||
        !buffer_contains(packet, off, &set.links[0].ipv4[0].addr, sizeof(set.links[0].ipv4[0].addr)) ||
        !buffer_contains(packet, off, &set.links[0].ipv4[1].addr, sizeof(set.links[0].ipv4[1].addr)) ||
        !buffer_contains(packet, off, &ula, sizeof(ula)) ||
        buffer_contains(packet, off, &unknown_prefix, sizeof(unknown_prefix)) ||
        buffer_contains(packet, off, &ll, sizeof(ll))) {{
        return 5;
    }}
    return 0;
}}
'''.format(mdns_source=mdns_source)
        run = self._compile_and_run_c_helper(source, "mdns_dual_stack_bind_records")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout, "10.0.1.1/24 169.254.1.9/16 fdbb:1111:2222:3333::40/64\n")

    def test_mdns_advertise_links_exclude_fe80_only_links_but_keep_ipv4_fe80_transport(self) -> None:
        mdns_source = (REPO_ROOT / "build" / "mdns-advertiser.c").as_posix()
        source = '''
#include <arpa/inet.h>
#include <string.h>
#define main mdns_advertiser_main
#include "{mdns_source}"
#undef main

int main(void) {{
    struct link_context_set all_links;
    struct link_context_set advertise_links;
    struct in6_addr ll1;
    struct in6_addr ll2;

    memset(&all_links, 0, sizeof(all_links));
    if (inet_pton(AF_INET6, "fe80::1", &ll1) != 1 ||
        inet_pton(AF_INET6, "fe80::2", &ll2) != 1) {{
        return 1;
    }}

    append_link_ipv6(&all_links, "bridge0", &ll1, 64, 7, IFF_UP | IFF_RUNNING);
    append_link_ipv4(&all_links, "bridge1", inet_addr("192.168.1.40"), inet_addr("255.255.255.0"), IFF_UP | IFF_RUNNING);
    append_link_ipv6(&all_links, "bridge1", &ll2, 64, 8, IFF_UP | IFF_RUNNING);
    filter_advertise_link_contexts(&advertise_links, &all_links);

    if (all_links.count != 2 || advertise_links.count != 1) {{
        return 2;
    }}
    if (strcmp(advertise_links.links[0].name, "bridge1") != 0) {{
        return 3;
    }}
    if (!link_contexts_need_ipv4_socket(&advertise_links) ||
        !link_contexts_need_ipv6_socket(&advertise_links)) {{
        return 4;
    }}
    return 0;
}}
'''.format(mdns_source=mdns_source)
        run = self._compile_and_run_c_helper(source, "mdns_advertise_link_filter")
        self.assertEqual(run.returncode, 0, run.stderr)

    def test_mdns_print_auto_ip_cidrs_returns_distinct_probe_failure_status(self) -> None:
        mdns_source = (REPO_ROOT / "build" / "mdns-advertiser.c").as_posix()
        source = '''
#include <arpa/inet.h>
#include <string.h>
#define main mdns_advertiser_main
#include "{mdns_source}"
#undef main

struct fake_auto_ip_plan {{
    int mode;
}};

static int fake_collect_contexts(struct link_context_set *out, void *userdata) {{
    struct fake_auto_ip_plan *plan = (struct fake_auto_ip_plan *)userdata;
    memset(out, 0, sizeof(*out));
    if (plan->mode == 1) {{
        return -1;
    }}
    if (plan->mode == 2) {{
        append_link_ipv4(out, "bridge0", inet_addr("10.0.1.1"), inet_addr("255.255.255.0"), IFF_UP | IFF_RUNNING);
    }}
    if (plan->mode == 3) {{
        append_link_ipv4(out, "bridge0", inet_addr("10.0.1.1"), inet_addr("255.255.255.0"), IFF_UP | IFF_RUNNING);
        out->truncated = 1;
    }}
    if (plan->mode == 4) {{
        struct in6_addr addr6;
        if (inet_pton(AF_INET6, "fdbb:1111:2222:3333::40", &addr6) != 1) {{
            return -1;
        }}
        append_link_ipv6(out, "bridge0", &addr6, 64, 7, IFF_UP | IFF_RUNNING);
    }}
    return 0;
}}

int main(void) {{
    struct fake_auto_ip_plan plan;

    memset(&plan, 0, sizeof(plan));
    plan.mode = 2;
    if (print_auto_ip_cidrs_with_provider(stdout, fake_collect_contexts, &plan) != EXIT_OK) {{
        return 1;
    }}
    plan.mode = 0;
    if (print_auto_ip_cidrs_with_provider(stdout, fake_collect_contexts, &plan) != EXIT_AUTO_IP_UNAVAILABLE) {{
        return 2;
    }}
    plan.mode = 1;
    if (print_auto_ip_cidrs_with_provider(stdout, fake_collect_contexts, &plan) != EXIT_AUTO_IP_PROBE_FAILED) {{
        return 3;
    }}
    if (print_auto_ip_cidrs_with_provider(stdout, NULL, &plan) != EXIT_AUTO_IP_PROBE_FAILED) {{
        return 4;
    }}
    plan.mode = 3;
    if (print_auto_ip_cidrs_with_provider(stdout, fake_collect_contexts, &plan) != EXIT_AUTO_IP_PROBE_FAILED) {{
        return 5;
    }}
    plan.mode = 4;
    if (print_auto_ip_cidrs_with_provider(stdout, fake_collect_contexts, &plan) != EXIT_AUTO_IP_UNAVAILABLE) {{
        return 6;
    }}
    return 0;
}}
'''.format(mdns_source=mdns_source)
        run = self._compile_and_run_c_helper(source, "mdns_print_auto_ip_cidrs_status")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout, "10.0.1.1/24\n")

    def test_mdns_print_smb_bind_interfaces_returns_dual_stack_probe_status(self) -> None:
        mdns_source = (REPO_ROOT / "build" / "mdns-advertiser.c").as_posix()
        source = '''
#include <arpa/inet.h>
#include <string.h>
#define main mdns_advertiser_main
#include "{mdns_source}"
#undef main

struct fake_bind_plan {{
    int mode;
}};

static int fake_collect_links(struct link_context_set *out, void *userdata) {{
    struct fake_bind_plan *plan = (struct fake_bind_plan *)userdata;
    struct in6_addr ula;
    struct in6_addr ll;

    memset(out, 0, sizeof(*out));
    if (plan->mode == 1) {{
        return -1;
    }}
    if (plan->mode == 2) {{
        inet_pton(AF_INET6, "fdbb:1111:2222:3333::40", &ula);
        inet_pton(AF_INET6, "fe80::40", &ll);
        append_link_ipv4(out, "bridge0", inet_addr("169.254.1.9"), inet_addr("255.255.0.0"), IFF_UP | IFF_RUNNING);
        append_link_ipv6(out, "bridge0", &ula, 64, 7, IFF_UP | IFF_RUNNING);
        append_link_ipv6(out, "bridge0", &ll, 64, 7, IFF_UP | IFF_RUNNING);
    }}
    if (plan->mode == 3) {{
        inet_pton(AF_INET6, "fe80::40", &ll);
        append_link_ipv6(out, "bridge0", &ll, 64, 7, IFF_UP | IFF_RUNNING);
    }}
    if (plan->mode == 4) {{
        append_link_ipv4(out, "bridge0", inet_addr("169.254.1.9"), inet_addr("255.255.0.0"), IFF_UP | IFF_RUNNING);
        out->truncated = 1;
    }}
    return 0;
}}

int main(void) {{
    struct fake_bind_plan plan;

    memset(&plan, 0, sizeof(plan));
    plan.mode = 2;
    if (print_smb_bind_interfaces_with_provider(stdout, fake_collect_links, &plan) != EXIT_OK) {{
        return 1;
    }}
    plan.mode = 0;
    if (print_smb_bind_interfaces_with_provider(stdout, fake_collect_links, &plan) != EXIT_AUTO_IP_UNAVAILABLE) {{
        return 2;
    }}
    plan.mode = 1;
    if (print_smb_bind_interfaces_with_provider(stdout, fake_collect_links, &plan) != EXIT_AUTO_IP_PROBE_FAILED) {{
        return 3;
    }}
    plan.mode = 3;
    if (print_smb_bind_interfaces_with_provider(stdout, fake_collect_links, &plan) != EXIT_AUTO_IP_UNAVAILABLE) {{
        return 4;
    }}
    if (print_smb_bind_interfaces_with_provider(stdout, NULL, &plan) != EXIT_AUTO_IP_PROBE_FAILED) {{
        return 5;
    }}
    plan.mode = 4;
    if (print_smb_bind_interfaces_with_provider(stdout, fake_collect_links, &plan) != EXIT_AUTO_IP_PROBE_FAILED) {{
        return 6;
    }}
    return 0;
}}
'''.format(mdns_source=mdns_source)
        run = self._compile_and_run_c_helper(source, "mdns_print_smb_bind_status")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout, "169.254.1.9/16 fdbb:1111:2222:3333::40/64\n")

    def test_mdns_print_socket_families_uses_advertise_links_not_samba_tokens(self) -> None:
        mdns_source = (REPO_ROOT / "build" / "mdns-advertiser.c").as_posix()
        source = '''
#include <arpa/inet.h>
#include <string.h>
#define main mdns_advertiser_main
#include "{mdns_source}"
#undef main

struct fake_family_plan {{
    int mode;
}};

static int fake_collect_advertise_links(struct link_context_set *out, void *userdata) {{
    struct fake_family_plan *plan = (struct fake_family_plan *)userdata;
    struct in6_addr ll;
    struct in6_addr ula;

    memset(out, 0, sizeof(*out));
    inet_pton(AF_INET6, "fe80::40", &ll);
    inet_pton(AF_INET6, "fdbb:1111:2222:3333::40", &ula);
    if (plan->mode == 1) {{
        return -1;
    }}
    if (plan->mode == 2) {{
        append_link_ipv4(out, "bridge0", inet_addr("192.168.1.40"), inet_addr("255.255.255.0"), IFF_UP | IFF_RUNNING);
        append_link_ipv6(out, "bridge0", &ll, 64, 7, IFF_UP | IFF_RUNNING);
    }}
    if (plan->mode == 3) {{
        append_link_ipv6(out, "bridge0", &ula, 64, 7, IFF_UP | IFF_RUNNING);
    }}
    if (plan->mode == 4) {{
        append_link_ipv6(out, "bridge0", &ll, 64, 7, IFF_UP | IFF_RUNNING);
    }}
    if (plan->mode == 5) {{
        append_link_ipv4(out, "bridge0", inet_addr("192.168.1.40"), inet_addr("255.255.255.0"), IFF_UP | IFF_RUNNING);
        append_link_ipv6_with_transport(out, "bridge0", &ll, 64, 7, IFF_UP | IFF_RUNNING, 0);
    }}
    return 0;
}}

int main(void) {{
    struct fake_family_plan plan;

    memset(&plan, 0, sizeof(plan));
    plan.mode = 2;
    if (print_mdns_socket_families_with_provider(stdout, fake_collect_advertise_links, &plan) != EXIT_OK) {{
        return 1;
    }}
    plan.mode = 3;
    if (print_mdns_socket_families_with_provider(stdout, fake_collect_advertise_links, &plan) != EXIT_OK) {{
        return 2;
    }}
    plan.mode = 0;
    if (print_mdns_socket_families_with_provider(stdout, fake_collect_advertise_links, &plan) != EXIT_AUTO_IP_UNAVAILABLE) {{
        return 3;
    }}
    plan.mode = 4;
    if (print_mdns_socket_families_with_provider(stdout, fake_collect_advertise_links, &plan) != EXIT_AUTO_IP_UNAVAILABLE) {{
        return 5;
    }}
    plan.mode = 5;
    if (print_mdns_socket_families_with_provider(stdout, fake_collect_advertise_links, &plan) != EXIT_OK) {{
        return 6;
    }}
    plan.mode = 1;
    if (print_mdns_socket_families_with_provider(stdout, fake_collect_advertise_links, &plan) != EXIT_AUTO_IP_PROBE_FAILED) {{
        return 4;
    }}
    return 0;
}}
'''.format(mdns_source=mdns_source)
        run = self._compile_and_run_c_helper(source, "mdns_print_socket_families")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout, "ipv4 ipv6\nipv6\nipv4\n")

    def test_mdns_scoped_ipv6_multicast_destination_uses_link_ifindex(self) -> None:
        mdns_source = (REPO_ROOT / "build" / "mdns-advertiser.c").as_posix()
        source = '''
#include <arpa/inet.h>
#include <string.h>
#define main mdns_advertiser_main
#include "{mdns_source}"
#undef main

int main(void) {{
    struct sockaddr_in6 base;
    struct sockaddr_in6 scoped;
    struct link_context link;

    memset(&base, 0, sizeof(base));
    memset(&scoped, 0, sizeof(scoped));
    memset(&link, 0, sizeof(link));
    base.sin6_family = AF_INET6;
    base.sin6_port = htons(5353);
    if (inet_pton(AF_INET6, "ff02::fb", &base.sin6_addr) != 1) {{
        return 1;
    }}
    link.ifindex = 17;
    scoped_mdns_dest6_for_link(&scoped, &base, &link);
    if (scoped.sin6_family != AF_INET6 ||
        scoped.sin6_port != htons(5353) ||
        scoped.sin6_scope_id != 17 ||
        memcmp(&scoped.sin6_addr, &base.sin6_addr, sizeof(base.sin6_addr)) != 0) {{
        return 2;
    }}
    return 0;
}}
'''.format(mdns_source=mdns_source)
        run = self._compile_and_run_c_helper(source, "mdns_scoped_ipv6_dest")
        self.assertEqual(run.returncode, 0, run.stderr)

    def test_mdns_advertiser_builds_riousbprint_txt_from_printer_identity(self) -> None:
        mdns_source = (REPO_ROOT / "build" / "mdns-advertiser.c").as_posix()
        source = r'''
#include <stdio.h>
#include <string.h>
#define main mdns_advertiser_main
#include "@MDNS_SOURCE@"
#undef main

static int has_txt(const char *txts[], size_t count, const char *want) {
    size_t i;
    for (i = 0; i < count; i++) {
        if (strcmp(txts[i], want) == 0) {
            return 1;
        }
    }
    return 0;
}

int main(void) {
    struct config cfg;
    char storage[RIOUSBPRINT_MAX_TXT_ITEMS][MAX_TXT_STRING + 1];
    const char *txts[RIOUSBPRINT_MAX_TXT_ITEMS];
    size_t txt_count = 0;

    memset(&cfg, 0, sizeof(cfg));
    snprintf(cfg.instance_name, sizeof(cfg.instance_name), "%s", "James's AirPort Time Capsule");
    snprintf(cfg.riousbprint_instance_name, sizeof(cfg.riousbprint_instance_name), "%s", "Canon MP490 series");
    snprintf(cfg.riousbprint_note, sizeof(cfg.riousbprint_note), "%s", "James's AirPort Time Capsule");
    snprintf(cfg.riousbprint_mfg, sizeof(cfg.riousbprint_mfg), "%s", "Canon");
    snprintf(cfg.riousbprint_mdl, sizeof(cfg.riousbprint_mdl), "%s", "MP490 series");
    snprintf(cfg.riousbprint_serial, sizeof(cfg.riousbprint_serial), "%s", "C0958C");
    snprintf(cfg.riousbprint_cmd, sizeof(cfg.riousbprint_cmd), "%s", "BJL,BJRaster3,BSCCe,IVEC,IVECPLI");

    if (build_riousbprint_txt_items(&cfg, storage, txts, &txt_count) != 0) {
        return 1;
    }
    if (txt_count != 12) {
        return 2;
    }
    if (!has_txt(txts, txt_count, "txtvers=1") ||
        !has_txt(txts, txt_count, "qtotal=1") ||
        !has_txt(txts, txt_count, "note=James's AirPort Time Capsule") ||
        !has_txt(txts, txt_count, "product=(Canon MP490 series)") ||
        !has_txt(txts, txt_count, "rp=Canon MP490 series C0958C") ||
        !has_txt(txts, txt_count, "pdl=application/BJL,application/BJRaster3,application/BSCCe,application/IVEC,application/IVECPLI") ||
        !has_txt(txts, txt_count, "priority=1") ||
        !has_txt(txts, txt_count, "usb_MFG=Canon") ||
        !has_txt(txts, txt_count, "usb_CMD=BJL,BJRaster3,BSCCe,IVEC,IVECPLI") ||
        !has_txt(txts, txt_count, "usb_MDL=MP490 series") ||
        !has_txt(txts, txt_count, "usb_CLS=PRINTER") ||
        !has_txt(txts, txt_count, "usb_DES=Canon MP490 series")) {
        return 3;
    }
    printf("ok\n");
    return 0;
}
'''.replace("@MDNS_SOURCE@", mdns_source)
        run = self._compile_and_run_c_helper(source, "mdns_riousbprint_txt")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout.strip(), "ok")

    def test_mdns_advertiser_builds_pdl_datastream_txt_from_printer_identity(self) -> None:
        mdns_source = (REPO_ROOT / "build" / "mdns-advertiser.c").as_posix()
        source = r'''
#include <stdio.h>
#include <string.h>
#define main mdns_advertiser_main
#include "@MDNS_SOURCE@"
#undef main

static int has_txt(const char *txts[], size_t count, const char *want) {
    size_t i;
    for (i = 0; i < count; i++) {
        if (strcmp(txts[i], want) == 0) {
            return 1;
        }
    }
    return 0;
}

int main(void) {
    struct config cfg;
    char storage[PDL_DATASTREAM_MAX_TXT_ITEMS][MAX_TXT_STRING + 1];
    const char *txts[PDL_DATASTREAM_MAX_TXT_ITEMS];
    size_t txt_count = 0;

    memset(&cfg, 0, sizeof(cfg));
    snprintf(cfg.instance_name, sizeof(cfg.instance_name), "%s", "James's AirPort Time Capsule");
    snprintf(cfg.riousbprint_instance_name, sizeof(cfg.riousbprint_instance_name), "%s", "Canon MP490 series");
    snprintf(cfg.riousbprint_note, sizeof(cfg.riousbprint_note), "%s", "James's AirPort Time Capsule");
    snprintf(cfg.riousbprint_mfg, sizeof(cfg.riousbprint_mfg), "%s", "Canon");
    snprintf(cfg.riousbprint_mdl, sizeof(cfg.riousbprint_mdl), "%s", "MP490 series");
    snprintf(cfg.riousbprint_serial, sizeof(cfg.riousbprint_serial), "%s", "C0958C");
    snprintf(cfg.riousbprint_cmd, sizeof(cfg.riousbprint_cmd), "%s", "BJL,BJRaster3,BSCCe,IVEC,IVECPLI");

    if (build_pdl_datastream_txt_items(&cfg, storage, txts, &txt_count) != 0) {
        return 1;
    }
    if (txt_count != 12) {
        return 2;
    }
    if (!has_txt(txts, txt_count, "txtvers=1") ||
        !has_txt(txts, txt_count, "qtotal=1") ||
        !has_txt(txts, txt_count, "note=James's AirPort Time Capsule") ||
        !has_txt(txts, txt_count, "product=(Canon MP490 series)") ||
        !has_txt(txts, txt_count, "pdl=U") ||
        !has_txt(txts, txt_count, "priority=5") ||
        !has_txt(txts, txt_count, "usb_MFG=Canon") ||
        !has_txt(txts, txt_count, "usb_CMD=BJL,BJRaster3,BSCCe,IVEC,IVECPLI") ||
        !has_txt(txts, txt_count, "usb_MDL=MP490 series") ||
        !has_txt(txts, txt_count, "usb_CLS=PRINTER") ||
        !has_txt(txts, txt_count, "usb_DES=Canon MP490 series") ||
        !has_txt(txts, txt_count, "ty=Canon MP490 series")) {
        return 3;
    }
    if (has_txt(txts, txt_count, "rp=Canon MP490 series C0958C")) {
        return 4;
    }
    printf("ok\n");
    return 0;
}
'''.replace("@MDNS_SOURCE@", mdns_source)
        run = self._compile_and_run_c_helper(source, "mdns_pdl_datastream_txt")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout.strip(), "ok")

    def test_mdns_advertiser_extracts_riousbprint_cmd_from_ieee1284_device_id(self) -> None:
        mdns_source = (REPO_ROOT / "build" / "mdns-advertiser.c").as_posix()
        source = r'''
#include <stdio.h>
#include <string.h>
#define main mdns_advertiser_main
#include "@MDNS_SOURCE@"
#undef main

int main(void) {
    const char *device_id = "MFG:Canon;MDL:MP490 series;CMD:BJL,BJRaster3,BSCCe,IVEC,IVECPLI;";
    unsigned char buf[256];
    char cmd[MAX_TXT_STRING + 1];
    size_t len = strlen(device_id) + 2;

    memset(buf, 0, sizeof(buf));
    buf[0] = (unsigned char)((len >> 8) & 0xff);
    buf[1] = (unsigned char)(len & 0xff);
    memcpy(buf + 2, device_id, strlen(device_id));

    if (extract_cmd_from_ieee1284_device_id(cmd, sizeof(cmd), buf, len) != 0) {
        return 1;
    }
    if (strcmp(cmd, "BJL,BJRaster3,BSCCe,IVEC,IVECPLI") != 0) {
        return 2;
    }
    printf("%s\n", cmd);
    return 0;
}
'''.replace("@MDNS_SOURCE@", mdns_source)
        run = self._compile_and_run_c_helper(source, "mdns_riousbprint_ieee1284")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout.strip(), "BJL,BJRaster3,BSCCe,IVEC,IVECPLI")

    def test_mdns_advertiser_rejects_null_usb_printer_helper_args(self) -> None:
        mdns_source = (REPO_ROOT / "build" / "mdns-advertiser.c").as_posix()
        source = r'''
#include <stdio.h>
#include <string.h>
#define main mdns_advertiser_main
#include "@MDNS_SOURCE@"
#undef main

int main(void) {
    char storage[RIOUSBPRINT_MAX_TXT_ITEMS][MAX_TXT_STRING + 1];
    const char *txts[RIOUSBPRINT_MAX_TXT_ITEMS];
    size_t txt_count = 0;
    char out[MAX_TXT_STRING + 1];
    unsigned char device_id[] = {0, 10, 'C', 'M', 'D', ':', 'P', 'W', 'G', ';'};

    if (append_txt_itemf(NULL, txts, &txt_count, RIOUSBPRINT_MAX_TXT_ITEMS, "txtvers=1") == 0) {
        return 1;
    }
    if (append_txt_itemf(storage, NULL, &txt_count, RIOUSBPRINT_MAX_TXT_ITEMS, "txtvers=1") == 0) {
        return 2;
    }
    if (append_txt_itemf(storage, txts, NULL, RIOUSBPRINT_MAX_TXT_ITEMS, "txtvers=1") == 0) {
        return 3;
    }
    if (append_txt_itemf(storage, txts, &txt_count, RIOUSBPRINT_MAX_TXT_ITEMS, NULL) == 0) {
        return 4;
    }
    txt_count = RIOUSBPRINT_MAX_TXT_ITEMS;
    if (append_txt_itemf(storage, txts, &txt_count, RIOUSBPRINT_MAX_TXT_ITEMS, "txtvers=1") == 0) {
        return 5;
    }

    if (build_riousbprint_pdl(NULL, sizeof(out), "PWG") == 0) {
        return 6;
    }
    if (build_riousbprint_pdl(out, 0, "PWG") == 0) {
        return 7;
    }
    if (build_riousbprint_pdl(out, sizeof(out), NULL) == 0) {
        return 8;
    }

    if (ieee1284_lookup_field(NULL, sizeof(out), device_id + 2, sizeof(device_id) - 2, "CMD") == 0) {
        return 9;
    }
    if (extract_cmd_from_ieee1284_device_id(NULL, sizeof(out), device_id, sizeof(device_id)) == 0) {
        return 10;
    }
    if (extract_cmd_from_ieee1284_device_id(out, 0, device_id, sizeof(device_id)) == 0) {
        return 11;
    }
    if (extract_cmd_from_ieee1284_device_id(out, sizeof(out), NULL, sizeof(device_id)) == 0) {
        return 12;
    }

    printf("ok\n");
    return 0;
}
'''.replace("@MDNS_SOURCE@", mdns_source)
        run = self._compile_and_run_c_helper(source, "mdns_usb_printer_helper_null_args")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout.strip(), "ok")

    def test_mdns_advertiser_rejects_short_usb_device_id_transfer(self) -> None:
        mdns_source = (REPO_ROOT / "build" / "mdns-advertiser.c").as_posix()
        source = r'''
#include <stdio.h>
#include <string.h>
#define main mdns_advertiser_main
#include "@MDNS_SOURCE@"
#undef main

int main(void) {
    unsigned char buf[64];
    int actual_len = 99;

    memset(buf, 'X', sizeof(buf));
    buf[0] = 0;
    buf[1] = 32;
    memcpy(buf + 2, "CMD:LEAK;", 9);

    if (sanitize_usb_printer_device_id_transfer(buf, sizeof(buf), 2, &actual_len) == 0) {
        return 1;
    }
    if (actual_len != 0) {
        return 2;
    }
    if (buf[0] != 0 || buf[1] != 32 || buf[2] != 0 || buf[10] != 0 || buf[63] != 0) {
        return 3;
    }

    memset(buf, 'Y', sizeof(buf));
    if (sanitize_usb_printer_device_id_transfer(buf, sizeof(buf), 8, &actual_len) != 0) {
        return 4;
    }
    if (actual_len != 8 || buf[7] != 'Y' || buf[8] != 0 || buf[63] != 0) {
        return 5;
    }

    memset(buf, 'Z', sizeof(buf));
    if (sanitize_usb_printer_device_id_transfer(buf, sizeof(buf), 65, &actual_len) == 0) {
        return 6;
    }
    if (buf[0] != 0 || buf[63] != 0) {
        return 7;
    }

    printf("ok\n");
    return 0;
}
'''.replace("@MDNS_SOURCE@", mdns_source)
        run = self._compile_and_run_c_helper(source, "mdns_usb_device_id_short_transfer")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout.strip(), "ok")

    def test_mdns_advertiser_extracts_service_type_from_arbitrary_instance_fqdn(self) -> None:
        if shutil.which("cc") is None:
            self.skipTest("cc not available")

        mdns_source = (REPO_ROOT / "build" / "mdns-advertiser.c").as_posix()
        source = '''
#include <stdio.h>
#define main mdns_advertiser_main
#include "{mdns_source}"
#undef main

int main(void) {{
    char out[256];
    if (find_service_type_for_instance_fqdn(out, sizeof(out), "HP Printer._pdl-datastream._tcp.local.") != 0) {{
        return 1;
    }}
    puts(out);
    return 0;
}}
'''.format(mdns_source=mdns_source)
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            c_path = tmp / "mdns_service_type_test.c"
            bin_path = tmp / "mdns_service_type_test"
            c_path.write_text(source)
            proc = subprocess.run(
                ["cc", "-Wall", "-Wextra", "-Werror", str(c_path), "-o", str(bin_path)],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            run = subprocess.run([str(bin_path)], capture_output=True, text=True, check=False)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(run.stdout.strip(), "_pdl-datastream._tcp.local.")

    def test_mdns_advertiser_extracts_non_hardcoded_udp_service_type(self) -> None:
        if shutil.which("cc") is None:
            self.skipTest("cc not available")

        mdns_source = (REPO_ROOT / "build" / "mdns-advertiser.c").as_posix()
        source = '''
#include <stdio.h>
#define main mdns_advertiser_main
#include "{mdns_source}"
#undef main

int main(void) {{
    char out[256];
    if (find_service_type_for_instance_fqdn(out, sizeof(out), "Custom Thing._example-service._udp.local.") != 0) {{
        return 1;
    }}
    puts(out);
    return 0;
}}
'''.format(mdns_source=mdns_source)
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            c_path = tmp / "mdns_udp_service_type_test.c"
            bin_path = tmp / "mdns_udp_service_type_test"
            c_path.write_text(source)
            proc = subprocess.run(
                ["cc", "-Wall", "-Wextra", "-Werror", str(c_path), "-o", str(bin_path)],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            run = subprocess.run([str(bin_path)], capture_output=True, text=True, check=False)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(run.stdout.strip(), "_example-service._udp.local.")

    def test_mdns_dualstack_takeover_keeps_desired_ipv4_after_bind_race(self) -> None:
        mdns_source = (REPO_ROOT / "build" / "mdns-advertiser.c").as_posix()
        source = r'''
#include <arpa/inet.h>
#include <errno.h>
#include <netinet/in.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <unistd.h>

static int fake_socket(int domain, int type, int protocol);
static int fake_setsockopt(int sockfd, int level, int optname, const void *optval, socklen_t optlen);
static int fake_bind(int sockfd, const struct sockaddr *addr, socklen_t addrlen);
static int fake_close(int fd);
static int fake_system(const char *cmd);
static int fake_usleep(useconds_t usec);
static FILE *fake_popen(const char *cmd, const char *mode);
static char *fake_fgets(char *s, int size, FILE *stream);
static int fake_pclose(FILE *fp);

#define socket fake_socket
#define setsockopt fake_setsockopt
#define bind fake_bind
#define close fake_close
#define system fake_system
#define usleep fake_usleep
#define popen fake_popen
#define fgets fake_fgets
#define pclose fake_pclose
#define main mdns_advertiser_main
#include "@MDNS_SOURCE@"
#undef main
#undef pclose
#undef fgets
#undef popen
#undef usleep
#undef system
#undef close
#undef bind
#undef setsockopt
#undef socket

static int next_fd = 100;
static int ipv4_bind_failures_remaining;
static int ipv4_bind_attempts;
static int ipv6_bind_attempts;

static int fake_socket(int domain, int type, int protocol) {
    (void)domain;
    (void)type;
    (void)protocol;
    return next_fd++;
}

static int fake_setsockopt(int sockfd, int level, int optname, const void *optval, socklen_t optlen) {
    (void)sockfd;
    (void)level;
    (void)optname;
    (void)optval;
    (void)optlen;
    return 0;
}

static int fake_bind(int sockfd, const struct sockaddr *addr, socklen_t addrlen) {
    (void)sockfd;
    if (addrlen >= sizeof(struct sockaddr_in) && addr != NULL && addr->sa_family == AF_INET) {
        ipv4_bind_attempts++;
        if (ipv4_bind_failures_remaining > 0) {
            ipv4_bind_failures_remaining--;
            errno = EADDRINUSE;
            return -1;
        }
    } else if (addrlen >= sizeof(struct sockaddr_in6) && addr != NULL && addr->sa_family == AF_INET6) {
        ipv6_bind_attempts++;
    }
    return 0;
}

static int fake_close(int fd) {
    (void)fd;
    return 0;
}

static int fake_system(const char *cmd) {
    (void)cmd;
    return 0;
}

static int fake_usleep(useconds_t usec) {
    (void)usec;
    return 0;
}

static FILE *fake_popen(const char *cmd, const char *mode) {
    (void)cmd;
    (void)mode;
    return NULL;
}

static char *fake_fgets(char *s, int size, FILE *stream) {
    (void)s;
    (void)size;
    (void)stream;
    return NULL;
}

static int fake_pclose(FILE *fp) {
    (void)fp;
    return 0;
}

static void make_desired_links(struct link_context_set *links) {
    struct in6_addr ula;
    memset(links, 0, sizeof(*links));
    append_link_ipv4(links, "bridge0", inet_addr("10.0.1.40"), inet_addr("255.255.255.0"), IFF_UP | IFF_RUNNING);
    if (inet_pton(AF_INET6, "fdbb:1111:2222:3333::40", &ula) != 1) {
        return;
    }
    append_link_ipv6(links, "bridge0", &ula, 64, 7, IFF_UP | IFF_RUNNING);
}

int main(void) {
    struct link_context_set desired;
    struct link_context_set active;
    struct mdns_socket_pair sockets;
    struct mdns_transport_status status;
    int rc;

    make_desired_links(&desired);
    memset(&active, 0, sizeof(active));
    sockets.ipv4_fd = -1;
    sockets.ipv6_fd = -1;
    ipv4_bind_failures_remaining = 2;
    rc = acquire_dualstack_mdns_sockets(0, &desired, &active, &sockets, &status);
    if (rc != 0) {
        return 1;
    }
    if (!link_contexts_need_ipv4_socket(&desired) || !link_contexts_need_ipv6_socket(&desired)) {
        return 2;
    }
    if (!status.active_ipv4 || !status.active_ipv6 || status.missing_required_ipv4) {
        return 3;
    }
    if (ipv4_bind_attempts < 3 || ipv6_bind_attempts < 1) {
        return 4;
    }
    close_mdns_socket_pair(&sockets);

    make_desired_links(&desired);
    memset(&active, 0, sizeof(active));
    sockets.ipv4_fd = -1;
    sockets.ipv6_fd = -1;
    ipv4_bind_attempts = 0;
    ipv6_bind_attempts = 0;
    ipv4_bind_failures_remaining = 100;
    rc = acquire_dualstack_mdns_sockets(0, &desired, &active, &sockets, &status);
    if (rc != 1) {
        return 5;
    }
    if (!link_contexts_need_ipv4_socket(&desired) || !status.missing_required_ipv4) {
        return 6;
    }
    if (status.active_ipv4 || !status.active_ipv6) {
        return 7;
    }
    if (ipv4_bind_attempts <= 1) {
        return 8;
    }
    close_mdns_socket_pair(&sockets);
    return 0;
}
'''.replace("@MDNS_SOURCE@", mdns_source)
        run = self._compile_and_run_c_helper(source, "mdns_dualstack_takeover_desired_ipv4")
        self.assertEqual(run.returncode, 0, run.stderr)

    def test_mdns_runtime_socket_updates_roll_back_partial_memberships_and_fallback_to_ipv4(self) -> None:
        mdns_source = (REPO_ROOT / "build" / "mdns-advertiser.c").as_posix()
        source = r'''
#include <arpa/inet.h>
#include <errno.h>
#include <netinet/in.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/types.h>

static int fake_socket(int domain, int type, int protocol);
static int fake_setsockopt(int sockfd, int level, int optname, const void *optval, socklen_t optlen);
static int fake_bind(int sockfd, const struct sockaddr *addr, socklen_t addrlen);
static int fake_close(int fd);

#define socket fake_socket
#define setsockopt fake_setsockopt
#define bind fake_bind
#define close fake_close
#define main mdns_advertiser_main
#include "@MDNS_SOURCE@"
#undef main
#undef close
#undef bind
#undef setsockopt
#undef socket

static int socket_calls;
static int bind_calls;
static int close_calls;
static int membership_sets;
static int drop_membership_sets;
static int outbound_sets;
static int fail_ipv6_socket;
static int fail_second_membership;
static int next_fd = 100;

static void reset_fakes(void) {
    socket_calls = 0;
    bind_calls = 0;
    close_calls = 0;
    membership_sets = 0;
    drop_membership_sets = 0;
    outbound_sets = 0;
    fail_ipv6_socket = 0;
    fail_second_membership = 0;
    next_fd = 100;
}

static int fake_socket(int domain, int type, int protocol) {
    (void)type;
    (void)protocol;
    socket_calls++;
    if (fail_ipv6_socket && domain == AF_INET6) {
        errno = EAFNOSUPPORT;
        return -1;
    }
    return next_fd++;
}

static int fake_setsockopt(int sockfd, int level, int optname, const void *optval, socklen_t optlen) {
    (void)sockfd;
    (void)optval;
    (void)optlen;
    if (level == IPPROTO_IP && optname == IP_ADD_MEMBERSHIP) {
        membership_sets++;
        if (fail_second_membership && membership_sets >= 2) {
            errno = EADDRINUSE;
            return -1;
        }
    }
#ifdef IP_DROP_MEMBERSHIP
    if (level == IPPROTO_IP && optname == IP_DROP_MEMBERSHIP) {
        drop_membership_sets++;
    }
#endif
    if (level == IPPROTO_IP && optname == IP_MULTICAST_IF) {
        outbound_sets++;
    }
    return 0;
}

static int fake_bind(int sockfd, const struct sockaddr *addr, socklen_t addrlen) {
    (void)sockfd;
    (void)addr;
    (void)addrlen;
    bind_calls++;
    return 0;
}

static int fake_close(int fd) {
    (void)fd;
    close_calls++;
    return 0;
}

static void add_ipv4_link(struct link_context_set *set, const char *name, const char *addr) {
    append_link_ipv4(set, name, inet_addr(addr), inet_addr("255.255.255.0"), IFF_UP | IFF_RUNNING);
}

int main(void) {
    struct link_context_set old_links;
    struct link_context_set new_links;
    struct mdns_socket_pair sockets;
    struct in6_addr ula;

    reset_fakes();
    memset(&old_links, 0, sizeof(old_links));
    memset(&new_links, 0, sizeof(new_links));
    add_ipv4_link(&old_links, "bridge0", "10.0.1.1");
    add_ipv4_link(&new_links, "bridge0", "10.0.1.1");
    add_ipv4_link(&new_links, "en1", "192.168.50.2");
    add_ipv4_link(&new_links, "en2", "192.168.60.2");
    sockets.ipv4_fd = 55;
    sockets.ipv6_fd = -1;
    fail_second_membership = 1;
    if (prepare_runtime_mdns_sockets_for_links(0, &sockets, &old_links, &new_links) != 0) {
        return 1;
    }
#ifdef IP_DROP_MEMBERSHIP
    if (drop_membership_sets != 0) {
        return 2;
    }
#endif
    if (sockets.ipv4_fd != 55 || close_calls != 0 || membership_sets != 3 || outbound_sets != 1) {
        return 3;
    }
    if (new_links.count != 2 ||
        strcmp(new_links.links[0].name, "bridge0") != 0 ||
        strcmp(new_links.links[1].name, "en1") != 0) {
        return 16;
    }

    reset_fakes();
    memset(&new_links, 0, sizeof(new_links));
    add_ipv4_link(&new_links, "bridge0", "10.0.1.1");
    if (inet_pton(AF_INET6, "fdbb:1111:2222:3333::40", &ula) != 1) {
        return 4;
    }
    append_link_ipv6(&new_links, "bridge0", &ula, 64, 7, IFF_UP | IFF_RUNNING);
    fail_ipv6_socket = 1;
    sockets.ipv4_fd = -1;
    sockets.ipv6_fd = -1;
    if (open_dualstack_mdns_sockets(0, &new_links, 0, &sockets) != 0) {
        return 5;
    }
    if (sockets.ipv4_fd < 0 || sockets.ipv6_fd >= 0 || link_contexts_need_ipv6_socket(&new_links)) {
        return 6;
    }
    close_mdns_socket_pair(&sockets);
    printf("ok\n");
    return 0;
}
'''.replace("@MDNS_SOURCE@", mdns_source)
        run = self._compile_and_run_c_helper(source, "mdns_runtime_membership_rollback")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout.strip(), "ok")

    def test_mdns_advertiser_load_snapshot_accepts_host_hex_and_smb_adisk_records(self) -> None:
        if shutil.which("cc") is None:
            self.skipTest("cc not available")

        mdns_source = (REPO_ROOT / "build" / "mdns-advertiser.c").as_posix()
        source = '''
#include <stdio.h>
#define main mdns_advertiser_main
#include "{mdns_source}"
#undef main

int main(int argc, char **argv) {{
    struct service_record_set set;
    if (argc != 2) {{
        return 2;
    }}
    if (load_snapshot_file(argv[1], &set) != 0) {{
        return 1;
    }}
    printf("%zu\\n", set.count);
    printf("%s\\n", set.records[0].service_type);
    printf("%s\\n", set.records[0].host_fqdn);
    printf("%s\\n", set.records[1].service_type);
    printf("%s\\n", set.records[1].host_fqdn);
    return 0;
}}
'''.format(mdns_source=mdns_source)
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            c_path = tmp / "mdns_snapshot_load_test.c"
            bin_path = tmp / "mdns_snapshot_load_test"
            snapshot_path = tmp / "applemdns.txt"
            snapshot_path.write_text(
                "BEGIN\n"
                "TYPE=_smb._tcp.local.\n"
                "INSTANCE=James's AirPort Time Capsule\n"
                "HOST_HEX=4a616d6573732d416972506f72742d54696d652d43617073756c652e6c6f63616c2e\n"
                "PORT=445\n"
                "TXT=netbios=test\n"
                "END\n"
                "BEGIN\n"
                "TYPE=_adisk._tcp.local.\n"
                "INSTANCE=James's AirPort Time Capsule\n"
                "HOST_HEX=4a616d6573732d416972506f72742d54696d652d43617073756c652e6c6f63616c2e\n"
                "PORT=9\n"
                "TXT=sys=waMA=80:EA:96:E6:58:68,adVF=0x1010\n"
                "END\n"
            )
            c_path.write_text(source)
            proc = subprocess.run(
                ["cc", "-Wall", "-Wextra", "-Werror", str(c_path), "-o", str(bin_path)],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            run = subprocess.run([str(bin_path), str(snapshot_path)], capture_output=True, text=True, check=False)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(
                run.stdout.splitlines(),
                [
                    "2",
                    "_smb._tcp.local.",
                    "Jamess-AirPort-Time-Capsule.local.",
                    "_adisk._tcp.local.",
                    "Jamess-AirPort-Time-Capsule.local.",
                ],
            )

    def test_mdns_advertiser_filters_loaded_snapshot_by_local_airport_identity(self) -> None:
        if shutil.which("cc") is None:
            self.skipTest("cc not available")

        mdns_source = (REPO_ROOT / "build" / "mdns-advertiser.c").as_posix()
        source = '''
#include <stdio.h>
#include <string.h>
#define main mdns_advertiser_main
#include "{mdns_source}"
#undef main

static void add_record(struct service_record_set *set, const char *type, const char *instance,
                       const char *host, const char *txt) {{
    struct service_record *record = &set->records[set->count++];
    memset(record, 0, sizeof(*record));
    strncpy(record->service_type, type, sizeof(record->service_type) - 1);
    strncpy(record->instance_name, instance, sizeof(record->instance_name) - 1);
    strncpy(record->host_label, host, sizeof(record->host_label) - 1);
    snprintf(record->host_fqdn, sizeof(record->host_fqdn), "%s.local.", host);
    build_instance_fqdn(record->instance_fqdn, sizeof(record->instance_fqdn), instance, type);
    record->port = 5009;
    if (txt != NULL) {{
        strncpy(record->txt[record->txt_count++], txt, MAX_TXT_STRING);
    }}
}}

int main(void) {{
    struct config cfg;
    struct service_record_set loaded;
    struct service_record_set filtered;

    memset(&cfg, 0, sizeof(cfg));
    memset(&loaded, 0, sizeof(loaded));
    snprintf(cfg.airport_wama, sizeof(cfg.airport_wama), "%s", "80:EA:96:E6:58:68");

    add_record(&loaded, "_airport._tcp.local.", "Kitchen", "Kitchen", "waMA=AA:BB:CC:DD:EE:FF");
    add_record(&loaded, "_riousbprint._tcp.local.", "Kitchen Printer", "Kitchen", "rp=usb");
    add_record(&loaded, "_airport._tcp.local.", "Home", "Home", "raMA=80:ea:96:e6:58:68");
    add_record(&loaded, "_riousbprint._tcp.local.", "Home Printer", "Home", "rp=usb");

    if (prepare_loaded_snapshot_for_advertising(&cfg, &loaded, &filtered) != 0) {{
        return 1;
    }}
    printf("%zu\\n", filtered.count);
    printf("%s\\n", filtered.records[0].host_label);
    printf("%s\\n", filtered.records[1].host_label);

    memset(&cfg, 0, sizeof(cfg));
    if (prepare_loaded_snapshot_for_advertising(&cfg, &loaded, &filtered) == 0) {{
        return 2;
    }}
    snprintf(cfg.airport_wama, sizeof(cfg.airport_wama), "%s", "00:11:22:33:44:55");
    if (prepare_loaded_snapshot_for_advertising(&cfg, &loaded, &filtered) == 0) {{
        return 3;
    }}
    return 0;
}}
'''.format(mdns_source=mdns_source)
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            c_path = tmp / "mdns_snapshot_identity_test.c"
            bin_path = tmp / "mdns_snapshot_identity_test"
            c_path.write_text(source)
            proc = subprocess.run(
                ["cc", "-Wall", "-Wextra", "-Werror", str(c_path), "-o", str(bin_path)],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            run = subprocess.run([str(bin_path)], capture_output=True, text=True, check=False)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(run.stdout.splitlines(), ["2", "Home", "Home"])

    def test_mdns_advertiser_matches_comma_delimited_airport_txt(self) -> None:
        if shutil.which("cc") is None:
            self.skipTest("cc not available")

        mdns_source = (REPO_ROOT / "build" / "mdns-advertiser.c").as_posix()
        source = '''
#include <stdio.h>
#include <string.h>
#define main mdns_advertiser_main
#include "{mdns_source}"
#undef main

static void add_record(struct service_record_set *set, const char *type, const char *instance,
                       const char *host, const char *txt) {{
    struct service_record *record = &set->records[set->count++];
    memset(record, 0, sizeof(*record));
    strncpy(record->service_type, type, sizeof(record->service_type) - 1);
    strncpy(record->instance_name, instance, sizeof(record->instance_name) - 1);
    strncpy(record->host_label, host, sizeof(record->host_label) - 1);
    snprintf(record->host_fqdn, sizeof(record->host_fqdn), "%s.local.", host);
    build_instance_fqdn(record->instance_fqdn, sizeof(record->instance_fqdn), instance, type);
    record->port = 5009;
    if (txt != NULL) {{
        strncpy(record->txt[record->txt_count++], txt, MAX_TXT_STRING);
    }}
}}

int main(void) {{
    struct config cfg;
    struct service_record_set loaded;
    struct service_record_set filtered;

    memset(&cfg, 0, sizeof(cfg));
    memset(&loaded, 0, sizeof(loaded));
    snprintf(cfg.airport_wama, sizeof(cfg.airport_wama), "%s", "80:EA:96:E6:58:68");

    add_record(&loaded, "_airport._tcp.local.", "Home", "Home",
               "waMA=80-EA-96-E6-58-68,raMA=80-EA-96-EB-2E-7D,raM2=80-EA-96-EB-2E-7C");
    add_record(&loaded, "_riousbprint._tcp.local.", "Home Printer", "Home", "rp=usb");

    if (prepare_loaded_snapshot_for_advertising(&cfg, &loaded, &filtered) != 0) {{
        return 1;
    }}
    printf("%zu\\n", filtered.count);
    printf("%s\\n", filtered.records[0].host_label);
    return 0;
}}
'''.format(mdns_source=mdns_source)
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            c_path = tmp / "mdns_snapshot_composite_txt_test.c"
            bin_path = tmp / "mdns_snapshot_composite_txt_test"
            c_path.write_text(source)
            proc = subprocess.run(
                ["cc", "-Wall", "-Wextra", "-Werror", str(c_path), "-o", str(bin_path)],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            run = subprocess.run([str(bin_path)], capture_output=True, text=True, check=False)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(run.stdout.splitlines(), ["2", "Home"])

    def test_mdns_advertiser_snapshot_txt_round_trips_text_and_binary(self) -> None:
        mdns_source = (REPO_ROOT / "build" / "mdns-advertiser.c").as_posix()
        source = '''
#include <stdio.h>
#include <string.h>
#define main mdns_advertiser_main
#include "{mdns_source}"
#undef main

int main(int argc, char **argv) {{
    struct service_record_set set;
    struct service_record_set loaded;
    struct service_record *record;
    unsigned char binary_txt[] = {{'n','e','t','b','i','o','s','=','\\xff','\\x80','r'}};

    if (argc != 2) {{
        return 2;
    }}
    memset(&set, 0, sizeof(set));
    record = &set.records[set.count++];
    snprintf(record->service_type, sizeof(record->service_type), "%s", "_smb._tcp.local.");
    snprintf(record->instance_name, sizeof(record->instance_name), "%s", "Disk");
    snprintf(record->instance_fqdn, sizeof(record->instance_fqdn), "%s", "Disk._smb._tcp.local.");
    snprintf(record->host_label, sizeof(record->host_label), "%s", "DiskHost");
    snprintf(record->host_fqdn, sizeof(record->host_fqdn), "%s", "DiskHost.local.");
    record->port = 445;
    snprintf(record->txt[0], sizeof(record->txt[0]), "%s", "sys=waMA=80:EA:96:E6:58:68,adVF=0x1010");
    record->txt_len[0] = (uint8_t)strlen(record->txt[0]);
    record->txt_count = 1;

    memcpy(record->txt[1], binary_txt, sizeof(binary_txt));
    record->txt[1][sizeof(binary_txt)] = '\\0';
    record->txt_len[1] = (uint8_t)sizeof(binary_txt);
    record->txt_count = 2;

    if (write_snapshot_file_atomic(argv[1], &set) != 0) {{
        return 3;
    }}
    memset(&loaded, 0, sizeof(loaded));
    if (load_snapshot_file(argv[1], &loaded) != 0) {{
        return 4;
    }}
    if (loaded.count != 1 || loaded.records[0].txt_count != 2) {{
        return 5;
    }}
    if (loaded.records[0].txt_len[0] != strlen("sys=waMA=80:EA:96:E6:58:68,adVF=0x1010")) {{
        return 6;
    }}
    if (memcmp(loaded.records[0].txt[1], binary_txt, sizeof(binary_txt)) != 0) {{
        return 7;
    }}
    printf("%u\\n%u\\n", (unsigned)loaded.records[0].txt_len[0], (unsigned)loaded.records[0].txt_len[1]);
    return 0;
}}
'''.format(mdns_source=mdns_source)
        with tempfile.TemporaryDirectory() as tmpdir:
            snapshot_path = Path(tmpdir) / "applemdns.txt"
            run = self._compile_and_run_c_helper(source, "mdns_snapshot_txt_roundtrip", [str(snapshot_path)])
            self.assertEqual(run.returncode, 0, run.stderr)
            content = snapshot_path.read_text()
            self.assertIn("TXT=sys=waMA=80:EA:96:E6:58:68,adVF=0x1010", content)
            self.assertIn("TXT_HEX=", content)
            self.assertNotIn("TXT_HEX=7379733d77614d41", content)

    def test_mdns_advertiser_snapshot_suppression_rules_behave(self) -> None:
        mdns_source = (REPO_ROOT / "build" / "mdns-advertiser.c").as_posix()
        source = '''
#include <stdio.h>
#define main mdns_advertiser_main
#include "{mdns_source}"
#undef main

int main(void) {{
    printf("%d\\n", is_suppressed_snapshot_service_type("_smb._tcp.local."));
    printf("%d\\n", is_suppressed_snapshot_service_type("_adisk._tcp.local."));
    printf("%d\\n", is_suppressed_snapshot_service_type("_device-info._tcp.local."));
    printf("%d\\n", is_suppressed_snapshot_service_type("_afpovertcp._tcp.local."));
    printf("%d\\n", is_suppressed_snapshot_service_type("_airport._tcp.local."));
    printf("%d\\n", is_suppressed_snapshot_service_type("_sleep-proxy._udp.local."));
    return 0;
}}
'''.format(mdns_source=mdns_source)
        run = self._compile_and_run_c_helper(source, "mdns_snapshot_suppression")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout.splitlines(), ["1", "1", "1", "1", "0", "0"])

    def test_mdns_advertiser_routes_qu_qm_and_mixed_query_responses(self) -> None:
        mdns_source = (REPO_ROOT / "build" / "mdns-advertiser.c").as_posix()
        source = r'''
#include <arpa/inet.h>
#include <netinet/in.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/types.h>

ssize_t fake_sendto(int sockfd, const void *buf, size_t len, int flags,
                    const struct sockaddr *dest, socklen_t dest_len);

#define sendto fake_sendto
#define main mdns_advertiser_main
#include "@MDNS_SOURCE@"
#undef main
#undef sendto

static unsigned char captured_packets[8][BUF_SIZE];
static size_t captured_lengths[8];
static struct sockaddr_in captured_dests[8];
static size_t captured_count = 0;

ssize_t fake_sendto(int sockfd, const void *buf, size_t len, int flags,
                    const struct sockaddr *dest, socklen_t dest_len) {
    (void)sockfd;
    (void)flags;
    if (dest_len != sizeof(struct sockaddr_in)) {
        return -1;
    }
    if (captured_count < 8) {
        memcpy(captured_packets[captured_count], buf, len);
        captured_lengths[captured_count] = len;
        memcpy(&captured_dests[captured_count], dest, sizeof(struct sockaddr_in));
        captured_count++;
    }
    return (ssize_t)len;
}

static void reset_captures(void) {
    memset(captured_packets, 0, sizeof(captured_packets));
    memset(captured_lengths, 0, sizeof(captured_lengths));
    memset(captured_dests, 0, sizeof(captured_dests));
    captured_count = 0;
}

static void configure_base(struct config *cfg) {
    memset(cfg, 0, sizeof(*cfg));
    snprintf(cfg->instance_name, sizeof(cfg->instance_name), "%s", "Alton Time Capsule");
    snprintf(cfg->host_label, sizeof(cfg->host_label), "%s", "alton-time-capsule");
    snprintf(cfg->host_fqdn, sizeof(cfg->host_fqdn), "%s", "alton-time-capsule.local.");
    snprintf(cfg->service_type, sizeof(cfg->service_type), "%s", "_smb._tcp.local.");
    snprintf(cfg->adisk_service_type, sizeof(cfg->adisk_service_type), "%s", "_adisk._tcp.local.");
    snprintf(cfg->device_info_service_type, sizeof(cfg->device_info_service_type), "%s", "_device-info._tcp.local.");
    snprintf(cfg->airport_service_type, sizeof(cfg->airport_service_type), "%s", "_airport._tcp.local.");
    cfg->port = 445;
    cfg->adisk_port = 9;
    cfg->airport_port = 5009;
    cfg->ttl = 120;
}

static void configure_addrs(struct sockaddr_in *mdns_dest, struct sockaddr_in *source) {
    memset(mdns_dest, 0, sizeof(*mdns_dest));
    mdns_dest->sin_family = AF_INET;
    mdns_dest->sin_port = htons(MDNS_PORT);
    mdns_dest->sin_addr.s_addr = inet_addr(MDNS_GROUP);

    memset(source, 0, sizeof(*source));
    source->sin_family = AF_INET;
    source->sin_port = htons(62001);
    source->sin_addr.s_addr = inet_addr("10.0.1.42");
}

static int append_question(unsigned char *packet, size_t *off, const char *qname,
                           unsigned short qtype, unsigned short qclass) {
    return encode_name(packet, off, BUF_SIZE, qname) != 0 ||
           append_u16(packet, off, BUF_SIZE, qtype) != 0 ||
           append_u16(packet, off, BUF_SIZE, qclass) != 0;
}

static size_t make_query(unsigned char *packet, const char *qname, unsigned short qtype, unsigned short qclass) {
    struct dns_header hdr;
    size_t off = sizeof(hdr);
    memset(&hdr, 0, sizeof(hdr));
    hdr.qdcount = htons(1);
    memcpy(packet, &hdr, sizeof(hdr));
    if (append_question(packet, &off, qname, qtype, qclass) != 0) {
        return 0;
    }
    return off;
}

static size_t make_mixed_query(unsigned char *packet, const char *qu_name, const char *qm_name) {
    struct dns_header hdr;
    size_t off = sizeof(hdr);
    memset(&hdr, 0, sizeof(hdr));
    hdr.id = htons(0x1234);
    hdr.qdcount = htons(2);
    memcpy(packet, &hdr, sizeof(hdr));
    if (append_question(packet, &off, qu_name, DNS_TYPE_PTR, DNS_CLASS_IN | DNS_CLASS_CACHE_FLUSH) != 0 ||
        append_question(packet, &off, qm_name, DNS_TYPE_A, DNS_CLASS_IN) != 0) {
        return 0;
    }
    return off;
}

static int skip_response_questions(const unsigned char *packet, size_t packet_len, size_t *cursor) {
    struct dns_header hdr;
    unsigned short i;
    unsigned short qdcount;

    if (packet_len < sizeof(hdr)) {
        return -1;
    }
    memcpy(&hdr, packet, sizeof(hdr));
    qdcount = ntohs(hdr.qdcount);
    *cursor = sizeof(hdr);
    for (i = 0; i < qdcount; i++) {
        char name[MAX_NAME];
        if (decode_name(packet, packet_len, cursor, name, sizeof(name)) != 0 || *cursor + 4 > packet_len) {
            return -1;
        }
        *cursor += 4;
    }
    return 0;
}

static int count_rr_type(const unsigned char *packet, size_t packet_len, unsigned short want_type) {
    struct dns_header hdr;
    size_t cursor;
    unsigned short total_answers;
    int matches = 0;
    unsigned short i;

    memcpy(&hdr, packet, sizeof(hdr));
    total_answers = ntohs(hdr.ancount);
    if (skip_response_questions(packet, packet_len, &cursor) != 0) {
        return -1;
    }
    for (i = 0; i < total_answers; i++) {
        char name[MAX_NAME];
        unsigned short rrtype;
        unsigned short rdlength;

        if (decode_name(packet, packet_len, &cursor, name, sizeof(name)) != 0 || cursor + 10 > packet_len) {
            return -1;
        }
        memcpy(&rrtype, packet + cursor, 2);
        memcpy(&rdlength, packet + cursor + 8, 2);
        cursor += 10;
        rrtype = ntohs(rrtype);
        rdlength = ntohs(rdlength);
        if (cursor + rdlength > packet_len) {
            return -1;
        }
        if (rrtype == want_type) {
            matches++;
        }
        cursor += rdlength;
    }
    return matches;
}

static int packet_has_smb_browse_additionals(const unsigned char *packet, size_t packet_len) {
    return count_rr_type(packet, packet_len, DNS_TYPE_PTR) == 1 &&
           count_rr_type(packet, packet_len, DNS_TYPE_SRV) == 1 &&
           count_rr_type(packet, packet_len, DNS_TYPE_TXT) == 1 &&
           count_rr_type(packet, packet_len, DNS_TYPE_A) == 1;
}

static int legacy_unicast_ttls_and_classes_are_capped(const unsigned char *packet, size_t packet_len) {
    struct dns_header hdr;
    size_t cursor;
    unsigned short total_answers;
    unsigned short i;

    memcpy(&hdr, packet, sizeof(hdr));
    total_answers = ntohs(hdr.ancount);
    if (skip_response_questions(packet, packet_len, &cursor) != 0) {
        return 0;
    }
    for (i = 0; i < total_answers; i++) {
        char name[MAX_NAME];
        unsigned short rrclass;
        unsigned int ttl;
        unsigned short rdlength;

        if (decode_name(packet, packet_len, &cursor, name, sizeof(name)) != 0 || cursor + 10 > packet_len) {
            return 0;
        }
        memcpy(&rrclass, packet + cursor + 2, 2);
        memcpy(&ttl, packet + cursor + 4, 4);
        memcpy(&rdlength, packet + cursor + 8, 2);
        cursor += 10;
        rrclass = ntohs(rrclass);
        ttl = ntohl(ttl);
        rdlength = ntohs(rdlength);
        if ((rrclass & DNS_CLASS_CACHE_FLUSH) != 0 || ttl > LEGACY_UNICAST_TTL_MAX || cursor + rdlength > packet_len) {
            return 0;
        }
        cursor += rdlength;
    }
    return 1;
}

static int legacy_response_repeats_question(const unsigned char *response, size_t response_len,
                                            const unsigned char *query, size_t query_len) {
    struct dns_header hdr;
    size_t question_len = query_len - sizeof(hdr);

    if (response_len < query_len || query_len < sizeof(hdr)) {
        return 0;
    }
    memcpy(&hdr, response, sizeof(hdr));
    if (ntohs(hdr.qdcount) != 1) {
        return 0;
    }
    return memcmp(response + sizeof(hdr), query + sizeof(hdr), question_len) == 0;
}

static int run_route_cases(void) {
    struct config cfg;
    struct link_context response_link;
    struct service_record_set snapshot;
    struct sockaddr_in mdns_dest;
    struct sockaddr_in source;
    unsigned char query[BUF_SIZE];
    size_t query_len;

    configure_base(&cfg);
    memset(&response_link, 0, sizeof(response_link));
    snprintf(response_link.name, sizeof(response_link.name), "%s", "bridge0");
    response_link.flags = IFF_UP | IFF_RUNNING;
    response_link.ipv4[0].addr = inet_addr("10.0.1.77");
    response_link.ipv4[0].netmask = inet_addr("255.255.255.0");
    response_link.ipv4_count = 1;
    response_link.mdns_ipv4_transport = 1;
    response_link.mdns_ipv4_transport_addr = response_link.ipv4[0].addr;
    memset(&snapshot, 0, sizeof(snapshot));
    configure_addrs(&mdns_dest, &source);

    reset_captures();
    query_len = make_query(query, cfg.service_type, DNS_TYPE_PTR, DNS_CLASS_IN | DNS_CLASS_CACHE_FLUSH);
    if (query_len == 0 ||
        handle_query(1, query, query_len, &mdns_dest, &source, &cfg, &response_link, &snapshot, 0) != 0) {
        return 1;
    }
    if (captured_count != 1 ||
        captured_dests[0].sin_addr.s_addr != source.sin_addr.s_addr ||
        captured_dests[0].sin_port != source.sin_port ||
        !packet_has_smb_browse_additionals(captured_packets[0], captured_lengths[0]) ||
        !legacy_response_repeats_question(captured_packets[0], captured_lengths[0], query, query_len) ||
        !legacy_unicast_ttls_and_classes_are_capped(captured_packets[0], captured_lengths[0])) {
        return 2;
    }

    source.sin_port = htons(MDNS_PORT);
    reset_captures();
    query_len = make_query(query, cfg.service_type, DNS_TYPE_PTR, DNS_CLASS_IN);
    if (query_len == 0 ||
        handle_query(1, query, query_len, &mdns_dest, &source, &cfg, &response_link, &snapshot, 0) != 0) {
        return 3;
    }
    if (captured_count != 1 ||
        captured_dests[0].sin_addr.s_addr != mdns_dest.sin_addr.s_addr ||
        captured_dests[0].sin_port != mdns_dest.sin_port ||
        !packet_has_smb_browse_additionals(captured_packets[0], captured_lengths[0])) {
        return 4;
    }

    reset_captures();
    query_len = make_query(query, cfg.service_type, DNS_TYPE_PTR, DNS_CLASS_IN | DNS_CLASS_CACHE_FLUSH);
    if (query_len == 0 ||
        handle_query(1, query, query_len, &mdns_dest, &source, &cfg, &response_link, &snapshot, 0) != 0) {
        return 13;
    }
    if (captured_count != 2 ||
        captured_dests[0].sin_addr.s_addr != source.sin_addr.s_addr ||
        captured_dests[0].sin_port != source.sin_port ||
        captured_dests[1].sin_addr.s_addr != mdns_dest.sin_addr.s_addr ||
        captured_dests[1].sin_port != mdns_dest.sin_port ||
        !packet_has_smb_browse_additionals(captured_packets[0], captured_lengths[0]) ||
        !packet_has_smb_browse_additionals(captured_packets[1], captured_lengths[1])) {
        return 14;
    }

    source.sin_port = htons(MDNS_PORT);
    reset_captures();
    query_len = make_mixed_query(query, cfg.service_type, cfg.host_fqdn);
    if (query_len == 0 ||
        handle_query(1, query, query_len, &mdns_dest, &source, &cfg, &response_link, &snapshot, 0) != 0) {
        return 5;
    }
    if (captured_count != 2 ||
        captured_dests[0].sin_addr.s_addr != source.sin_addr.s_addr ||
        captured_dests[0].sin_port != source.sin_port ||
        captured_dests[1].sin_addr.s_addr != mdns_dest.sin_addr.s_addr ||
        captured_dests[1].sin_port != mdns_dest.sin_port ||
        !packet_has_smb_browse_additionals(captured_packets[0], captured_lengths[0]) ||
        count_rr_type(captured_packets[1], captured_lengths[1], DNS_TYPE_A) != 1) {
        return 6;
    }

    reset_captures();
    query_len = make_query(query, cfg.service_type, DNS_TYPE_PTR, DNS_CLASS_ANY);
    if (query_len == 0 ||
        handle_query(1, query, query_len, &mdns_dest, &source, &cfg, &response_link, &snapshot, 0) != 0) {
        return 7;
    }
    if (captured_count != 1 ||
        captured_dests[0].sin_addr.s_addr != mdns_dest.sin_addr.s_addr ||
        !packet_has_smb_browse_additionals(captured_packets[0], captured_lengths[0])) {
        return 8;
    }

    reset_captures();
    query_len = make_query(query, cfg.service_type, DNS_TYPE_ANY, DNS_CLASS_IN);
    if (query_len == 0 ||
        handle_query(1, query, query_len, &mdns_dest, &source, &cfg, &response_link, &snapshot, 0) != 0) {
        return 9;
    }
    if (captured_count != 1 ||
        captured_dests[0].sin_addr.s_addr != mdns_dest.sin_addr.s_addr ||
        !packet_has_smb_browse_additionals(captured_packets[0], captured_lengths[0])) {
        return 10;
    }

    reset_captures();
    query_len = make_query(query, cfg.service_type, DNS_TYPE_PTR, DNS_CLASS_CACHE_FLUSH | 2);
    if (query_len == 0 ||
        handle_query(1, query, query_len, &mdns_dest, &source, &cfg, &response_link, &snapshot, 0) != 0) {
        return 11;
    }
    if (captured_count != 0) {
        return 12;
    }

    return 0;
}

int main(void) {
    int result = run_route_cases();
    if (result != 0) {
        return result;
    }
    printf("ok\n");
    return 0;
}
'''.replace("@MDNS_SOURCE@", mdns_source)
        run = self._compile_and_run_c_helper(source, "mdns_qu_qm_query_routes")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout.strip(), "ok")

    def test_mdns_advertiser_enumerates_dns_sd_service_types(self) -> None:
        mdns_source = (REPO_ROOT / "build" / "mdns-advertiser.c").as_posix()
        source = r'''
#include <arpa/inet.h>
#include <netinet/in.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/types.h>

ssize_t fake_sendto(int sockfd, const void *buf, size_t len, int flags,
                    const struct sockaddr *dest, socklen_t dest_len);

#define sendto fake_sendto
#define main mdns_advertiser_main
#include "@MDNS_SOURCE@"
#undef main
#undef sendto

static unsigned char captured[BUF_SIZE];
static size_t captured_len = 0;
static struct sockaddr_in captured_dest;
static size_t captured_count = 0;

ssize_t fake_sendto(int sockfd, const void *buf, size_t len, int flags,
                    const struct sockaddr *dest, socklen_t dest_len) {
    (void)sockfd;
    (void)flags;
    if (dest_len != sizeof(struct sockaddr_in) || len > sizeof(captured)) {
        return -1;
    }
    memcpy(captured, buf, len);
    captured_len = len;
    memcpy(&captured_dest, dest, sizeof(captured_dest));
    captured_count++;
    return (ssize_t)len;
}

static void reset_capture(void) {
    memset(captured, 0, sizeof(captured));
    captured_len = 0;
    memset(&captured_dest, 0, sizeof(captured_dest));
    captured_count = 0;
}

static void configure_base(struct config *cfg) {
    memset(cfg, 0, sizeof(*cfg));
    snprintf(cfg->instance_name, sizeof(cfg->instance_name), "%s", "Alton Time Capsule");
    snprintf(cfg->host_label, sizeof(cfg->host_label), "%s", "alton-time-capsule");
    snprintf(cfg->host_fqdn, sizeof(cfg->host_fqdn), "%s", "alton-time-capsule.local.");
    snprintf(cfg->service_type, sizeof(cfg->service_type), "%s", "_smb._tcp.local.");
    snprintf(cfg->adisk_service_type, sizeof(cfg->adisk_service_type), "%s", "_adisk._tcp.local.");
    snprintf(cfg->device_info_service_type, sizeof(cfg->device_info_service_type), "%s", "_device-info._tcp.local.");
    snprintf(cfg->airport_service_type, sizeof(cfg->airport_service_type), "%s", "_airport._tcp.local.");
    snprintf(cfg->device_model, sizeof(cfg->device_model), "%s", "TimeCapsule8,119");
    snprintf(cfg->airport_wama, sizeof(cfg->airport_wama), "%s", "80:EA:96:E6:58:68");
    cfg->adisk_disks.count = 1;
    cfg->port = 445;
    cfg->adisk_port = 9;
    cfg->airport_port = 5009;
    cfg->ttl = 120;
}

static void configure_link(struct link_context *link, uint32_t ipv4_addr) {
    memset(link, 0, sizeof(*link));
    snprintf(link->name, sizeof(link->name), "%s", "bridge0");
    link->flags = IFF_UP | IFF_RUNNING;
    link->ipv4[0].addr = ipv4_addr;
    link->ipv4[0].netmask = inet_addr("255.255.255.0");
    link->ipv4_count = 1;
    link->mdns_ipv4_transport = 1;
    link->mdns_ipv4_transport_addr = ipv4_addr;
}

static void configure_addrs(struct sockaddr_in *mdns_dest, struct sockaddr_in *source) {
    memset(mdns_dest, 0, sizeof(*mdns_dest));
    mdns_dest->sin_family = AF_INET;
    mdns_dest->sin_port = htons(MDNS_PORT);
    mdns_dest->sin_addr.s_addr = inet_addr(MDNS_GROUP);

    memset(source, 0, sizeof(*source));
    source->sin_family = AF_INET;
    source->sin_port = htons(62001);
    source->sin_addr.s_addr = inet_addr("10.0.1.42");
}

static size_t make_query(unsigned char *packet, unsigned short qtype) {
    struct dns_header hdr;
    size_t off = sizeof(hdr);

    memset(&hdr, 0, sizeof(hdr));
    hdr.id = htons(0x4444);
    hdr.qdcount = htons(1);
    memcpy(packet, &hdr, sizeof(hdr));
    if (encode_name(packet, &off, BUF_SIZE, DNS_SD_SERVICE_ENUMERATION_NAME) != 0 ||
        append_u16(packet, &off, BUF_SIZE, qtype) != 0 ||
        append_u16(packet, &off, BUF_SIZE, DNS_CLASS_IN) != 0) {
        return 0;
    }
    return off;
}

static void add_snapshot_service(struct service_record_set *snapshot, const char *service_type) {
    struct service_record *record = &snapshot->records[snapshot->count++];
    memset(record, 0, sizeof(*record));
    snprintf(record->service_type, sizeof(record->service_type), "%s", service_type);
    snprintf(record->instance_name, sizeof(record->instance_name), "%s", "Snapshot");
    snprintf(record->instance_fqdn, sizeof(record->instance_fqdn), "%s%s", "Snapshot.", service_type);
    snprintf(record->host_fqdn, sizeof(record->host_fqdn), "%s", "snapshot-host.local.");
    record->port = 1;
}

static int skip_questions(size_t *cursor) {
    struct dns_header hdr;
    unsigned short i;

    if (captured_len < sizeof(hdr)) {
        return -1;
    }
    memcpy(&hdr, captured, sizeof(hdr));
    *cursor = sizeof(hdr);
    for (i = 0; i < ntohs(hdr.qdcount); i++) {
        char qname[MAX_NAME];
        if (decode_name(captured, captured_len, cursor, qname, sizeof(qname)) != 0 ||
            *cursor + 4 > captured_len) {
            return -1;
        }
        *cursor += 4;
    }
    return 0;
}

static int count_ptr_target(const char *target) {
    struct dns_header hdr;
    size_t cursor;
    unsigned short i;
    int matches = 0;

    memcpy(&hdr, captured, sizeof(hdr));
    if (skip_questions(&cursor) != 0) {
        return -1;
    }
    for (i = 0; i < ntohs(hdr.ancount); i++) {
        char owner[MAX_NAME];
        char ptr_target[MAX_NAME];
        unsigned short rrtype;
        unsigned short rdlength;
        size_t rdata_cursor;
        size_t rdata_end;

        if (decode_name(captured, captured_len, &cursor, owner, sizeof(owner)) != 0 ||
            cursor + 10 > captured_len) {
            return -1;
        }
        memcpy(&rrtype, captured + cursor, 2);
        memcpy(&rdlength, captured + cursor + 8, 2);
        cursor += 10;
        rrtype = ntohs(rrtype);
        rdlength = ntohs(rdlength);
        if (cursor + rdlength > captured_len) {
            return -1;
        }
        rdata_cursor = cursor;
        rdata_end = cursor + rdlength;
        if (rrtype == DNS_TYPE_PTR &&
            decode_name(captured, captured_len, &rdata_cursor, ptr_target, sizeof(ptr_target)) == 0 &&
            rdata_cursor == rdata_end &&
            name_equals(ptr_target, target)) {
            matches++;
        }
        cursor += rdlength;
    }
    return matches;
}

static int expect_generated_types(void) {
    if (captured_count != 1 ||
        captured_dest.sin_addr.s_addr != inet_addr("10.0.1.42") ||
        count_ptr_target("_smb._tcp.local.") != 1 ||
        count_ptr_target("_adisk._tcp.local.") != 1 ||
        count_ptr_target("_device-info._tcp.local.") != 1 ||
        count_ptr_target("_airport._tcp.local.") != 1 ||
        count_ptr_target("_ipp._tcp.local.") != 0 ||
        count_ptr_target("_afpovertcp._tcp.local.") != 0) {
        return 1;
    }
    return 0;
}

static int expect_snapshot_types(void) {
    if (captured_count != 1 ||
        count_ptr_target("_smb._tcp.local.") != 1 ||
        count_ptr_target("_adisk._tcp.local.") != 1 ||
        count_ptr_target("_device-info._tcp.local.") != 1 ||
        count_ptr_target("_airport._tcp.local.") != 1 ||
        count_ptr_target("_ipp._tcp.local.") != 1 ||
        count_ptr_target("_afpovertcp._tcp.local.") != 0) {
        return 1;
    }
    return 0;
}

int main(void) {
    struct config cfg;
    struct link_context link;
    struct service_record_set snapshot;
    struct sockaddr_in mdns_dest;
    struct sockaddr_in source;
    unsigned char query[BUF_SIZE];
    size_t query_len;

    configure_base(&cfg);
    configure_link(&link, inet_addr("10.0.1.77"));
    configure_addrs(&mdns_dest, &source);
    memset(&snapshot, 0, sizeof(snapshot));

    query_len = make_query(query, DNS_TYPE_PTR);
    reset_capture();
    if (query_len == 0 ||
        handle_query(1, query, query_len, &mdns_dest, &source, &cfg, &link, &snapshot, 0) != 0 ||
        expect_generated_types() != 0) {
        return 1;
    }

    add_snapshot_service(&snapshot, "_airport._tcp.local.");
    add_snapshot_service(&snapshot, "_ipp._tcp.local.");
    add_snapshot_service(&snapshot, "_smb._tcp.local.");
    add_snapshot_service(&snapshot, "_afpovertcp._tcp.local.");

    query_len = make_query(query, DNS_TYPE_ANY);
    reset_capture();
    if (query_len == 0 ||
        handle_query(1, query, query_len, &mdns_dest, &source, &cfg, &link, &snapshot, 1) != 0 ||
        expect_snapshot_types() != 0) {
        return 2;
    }

    printf("ok\n");
    return 0;
}
'''.replace("@MDNS_SOURCE@", mdns_source)
        run = self._compile_and_run_c_helper(source, "mdns_service_type_enumeration")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout.strip(), "ok")

    def test_mdns_advertiser_multicast_delay_and_unicast_hop_limits(self) -> None:
        mdns_source = (REPO_ROOT / "build" / "mdns-advertiser.c").as_posix()
        source = r'''
#include <arpa/inet.h>
#include <netinet/in.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <unistd.h>

ssize_t fake_sendto(int sockfd, const void *buf, size_t len, int flags,
                    const struct sockaddr *dest, socklen_t dest_len);
int fake_setsockopt(int sockfd, int level, int optname, const void *optval, socklen_t optlen);
int fake_usleep(useconds_t usec);
int fake_rand(void);
void fake_srand(unsigned int seed);

#define sendto fake_sendto
#define setsockopt fake_setsockopt
#define usleep fake_usleep
#define rand fake_rand
#define srand fake_srand
#define main mdns_advertiser_main
#include "@MDNS_SOURCE@"
#undef main
#undef srand
#undef rand
#undef usleep
#undef setsockopt
#undef sendto

struct opt_call {
    int level;
    int optname;
    unsigned int value;
};

static unsigned char captured_packets[4][BUF_SIZE];
static size_t captured_lengths[4];
static size_t captured_count = 0;
static useconds_t captured_usleeps[8];
static size_t captured_usleep_count = 0;
static struct opt_call captured_opts[32];
static size_t captured_opt_count = 0;

ssize_t fake_sendto(int sockfd, const void *buf, size_t len, int flags,
                    const struct sockaddr *dest, socklen_t dest_len) {
    (void)sockfd;
    (void)flags;
    (void)dest;
    (void)dest_len;
    if (captured_count < 4 && len <= sizeof(captured_packets[0])) {
        memcpy(captured_packets[captured_count], buf, len);
        captured_lengths[captured_count] = len;
        captured_count++;
    }
    return (ssize_t)len;
}

int fake_setsockopt(int sockfd, int level, int optname, const void *optval, socklen_t optlen) {
    (void)sockfd;
    if (captured_opt_count < 32) {
        captured_opts[captured_opt_count].level = level;
        captured_opts[captured_opt_count].optname = optname;
        captured_opts[captured_opt_count].value = 0;
        if (optval != NULL) {
            if (optlen == sizeof(int)) {
                int value;
                memcpy(&value, optval, sizeof(value));
                captured_opts[captured_opt_count].value = (unsigned int)value;
            } else if (optlen == sizeof(unsigned int)) {
                unsigned int value;
                memcpy(&value, optval, sizeof(value));
                captured_opts[captured_opt_count].value = value;
            }
        }
        captured_opt_count++;
    }
    return 0;
}

int fake_usleep(useconds_t usec) {
    if (captured_usleep_count < 8) {
        captured_usleeps[captured_usleep_count++] = usec;
    }
    return 0;
}

int fake_rand(void) {
    return 0;
}

void fake_srand(unsigned int seed) {
    (void)seed;
}

static void reset_packet_capture(void) {
    memset(captured_packets, 0, sizeof(captured_packets));
    memset(captured_lengths, 0, sizeof(captured_lengths));
    captured_count = 0;
    captured_usleep_count = 0;
}

static void reset_option_capture(void) {
    memset(captured_opts, 0, sizeof(captured_opts));
    captured_opt_count = 0;
}

static int saw_opt(int level, int optname, unsigned int value) {
    size_t i;

    for (i = 0; i < captured_opt_count; i++) {
        if (captured_opts[i].level == level &&
            captured_opts[i].optname == optname &&
            captured_opts[i].value == value) {
            return 1;
        }
    }
    return 0;
}

static void configure_base(struct config *cfg) {
    memset(cfg, 0, sizeof(*cfg));
    snprintf(cfg->instance_name, sizeof(cfg->instance_name), "%s", "Alton Time Capsule");
    snprintf(cfg->host_label, sizeof(cfg->host_label), "%s", "alton-time-capsule");
    snprintf(cfg->host_fqdn, sizeof(cfg->host_fqdn), "%s", "alton-time-capsule.local.");
    snprintf(cfg->service_type, sizeof(cfg->service_type), "%s", "_smb._tcp.local.");
    cfg->port = 445;
    cfg->ttl = 120;
}

static void configure_link(struct link_context *link, uint32_t ipv4_addr) {
    memset(link, 0, sizeof(*link));
    snprintf(link->name, sizeof(link->name), "%s", "bridge0");
    link->flags = IFF_UP | IFF_RUNNING;
    link->ipv4[0].addr = ipv4_addr;
    link->ipv4[0].netmask = inet_addr("255.255.255.0");
    link->ipv4_count = 1;
    link->mdns_ipv4_transport = 1;
    link->mdns_ipv4_transport_addr = ipv4_addr;
    link->ifindex = 5;
}

static size_t make_query(unsigned char *packet, const char *qname) {
    struct dns_header hdr;
    size_t off = sizeof(hdr);

    memset(&hdr, 0, sizeof(hdr));
    hdr.qdcount = htons(1);
    memcpy(packet, &hdr, sizeof(hdr));
    if (encode_name(packet, &off, BUF_SIZE, qname) != 0 ||
        append_u16(packet, &off, BUF_SIZE, DNS_TYPE_PTR) != 0 ||
        append_u16(packet, &off, BUF_SIZE, DNS_CLASS_IN) != 0) {
        return 0;
    }
    return off;
}

static int handle_query_from_port(uint16_t port) {
    struct config cfg;
    struct link_context link;
    struct service_record_set snapshot;
    struct sockaddr_in mdns_dest;
    struct sockaddr_in source;
    unsigned char query[BUF_SIZE];
    size_t query_len;

    configure_base(&cfg);
    configure_link(&link, inet_addr("10.0.1.77"));
    memset(&snapshot, 0, sizeof(snapshot));
    memset(&mdns_dest, 0, sizeof(mdns_dest));
    mdns_dest.sin_family = AF_INET;
    mdns_dest.sin_port = htons(MDNS_PORT);
    mdns_dest.sin_addr.s_addr = inet_addr(MDNS_GROUP);
    memset(&source, 0, sizeof(source));
    source.sin_family = AF_INET;
    source.sin_port = htons(port);
    source.sin_addr.s_addr = inet_addr("10.0.1.42");

    query_len = make_query(query, cfg.service_type);
    return query_len == 0 ? -1 : handle_query(1, query, query_len, &mdns_dest, &source, &cfg, &link, &snapshot, 0);
}

int main(void) {
    reset_packet_capture();
    if (handle_query_from_port(MDNS_PORT) != 0 ||
        captured_count != 1 ||
        captured_usleep_count != 1 ||
        captured_usleeps[0] != 20000) {
        return 1;
    }

    reset_packet_capture();
    if (handle_query_from_port(62001) != 0 ||
        captured_count != 1 ||
        captured_usleep_count != 0) {
        return 2;
    }

    reset_option_capture();
    if (configure_multicast_socket_options(77) != 0 ||
        !saw_opt(IPPROTO_IP, IP_MULTICAST_TTL, 255) ||
        !saw_opt(IPPROTO_IP, IP_MULTICAST_LOOP, 1)) {
        return 3;
    }
#ifdef IP_TTL
    if (!saw_opt(IPPROTO_IP, IP_TTL, 255)) {
        return 4;
    }
#endif

#ifdef IPV6_MULTICAST_IF
    reset_option_capture();
    if (set_outbound_multicast_interface6(77, 5, "test", 0, 0) != 0 ||
        !saw_opt(IPPROTO_IPV6, IPV6_MULTICAST_IF, 5)) {
        return 5;
    }
#ifdef IPV6_MULTICAST_HOPS
    if (!saw_opt(IPPROTO_IPV6, IPV6_MULTICAST_HOPS, 255)) {
        return 6;
    }
#endif
#ifdef IPV6_MULTICAST_LOOP
    if (!saw_opt(IPPROTO_IPV6, IPV6_MULTICAST_LOOP, 1)) {
        return 7;
    }
#endif
#ifdef IPV6_UNICAST_HOPS
    if (!saw_opt(IPPROTO_IPV6, IPV6_UNICAST_HOPS, 255)) {
        return 8;
    }
#endif
#endif

    printf("ok\n");
    return 0;
}
'''.replace("@MDNS_SOURCE@", mdns_source)
        run = self._compile_and_run_c_helper(source, "mdns_multicast_delay_and_hops")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout.strip(), "ok")

    def test_mdns_advertiser_startup_burst_schedule_is_apple_compatible(self) -> None:
        mdns_source = (REPO_ROOT / "build" / "mdns-advertiser.c").as_posix()
        source = r'''
#include <stdio.h>

#define main mdns_advertiser_main
#include "@MDNS_SOURCE@"
#undef main

int main(void) {
    static const unsigned int expected[STARTUP_BURST_COUNT] = {0, 1000, 3000, 7000};
    size_t i;

    if (STARTUP_BURST_COUNT != 4) {
        return 1;
    }
    for (i = 0; i < STARTUP_BURST_COUNT; i++) {
        if (g_startup_burst_offsets_ms[i] != expected[i]) {
            return 2;
        }
    }
    printf("ok\n");
    return 0;
}
'''.replace("@MDNS_SOURCE@", mdns_source)
        run = self._compile_and_run_c_helper(source, "mdns_startup_schedule")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout.strip(), "ok")

    def test_mdns_advertiser_diskless_answers_host_a_but_not_smb(self) -> None:
        mdns_source = (REPO_ROOT / "build" / "mdns-advertiser.c").as_posix()
        source = r'''
#include <arpa/inet.h>
#include <netinet/in.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/types.h>

ssize_t fake_sendto(int sockfd, const void *buf, size_t len, int flags,
                    const struct sockaddr *dest, socklen_t dest_len);

#define sendto fake_sendto
#define main mdns_advertiser_main
#include "@MDNS_SOURCE@"
#undef main
#undef sendto

static unsigned char captured_packets[4][BUF_SIZE];
static size_t captured_lengths[4];
static size_t captured_count = 0;

ssize_t fake_sendto(int sockfd, const void *buf, size_t len, int flags,
                    const struct sockaddr *dest, socklen_t dest_len) {
    (void)sockfd;
    (void)flags;
    (void)dest;
    (void)dest_len;
    if (captured_count < 4) {
        memcpy(captured_packets[captured_count], buf, len);
        captured_lengths[captured_count] = len;
        captured_count++;
    }
    return (ssize_t)len;
}

static void reset_captures(void) {
    memset(captured_packets, 0, sizeof(captured_packets));
    memset(captured_lengths, 0, sizeof(captured_lengths));
    captured_count = 0;
}

static void configure_base(struct config *cfg) {
    memset(cfg, 0, sizeof(*cfg));
    snprintf(cfg->instance_name, sizeof(cfg->instance_name), "%s", "Alton Time Capsule");
    snprintf(cfg->host_label, sizeof(cfg->host_label), "%s", "alton-time-capsule");
    snprintf(cfg->host_fqdn, sizeof(cfg->host_fqdn), "%s", "alton-time-capsule.local.");
    snprintf(cfg->service_type, sizeof(cfg->service_type), "%s", "_smb._tcp.local.");
    snprintf(cfg->adisk_service_type, sizeof(cfg->adisk_service_type), "%s", "_adisk._tcp.local.");
    snprintf(cfg->device_info_service_type, sizeof(cfg->device_info_service_type), "%s", "_device-info._tcp.local.");
    snprintf(cfg->airport_service_type, sizeof(cfg->airport_service_type), "%s", "_airport._tcp.local.");
    cfg->port = 445;
    cfg->adisk_port = 9;
    cfg->airport_port = 5009;
    cfg->ttl = 120;
    cfg->diskless = 1;
}

static size_t make_query(unsigned char *packet, const char *qname, unsigned short qtype) {
    struct dns_header hdr;
    size_t off = sizeof(hdr);
    memset(&hdr, 0, sizeof(hdr));
    hdr.qdcount = htons(1);
    memcpy(packet, &hdr, sizeof(hdr));
    if (encode_name(packet, &off, BUF_SIZE, qname) != 0 ||
        append_u16(packet, &off, BUF_SIZE, qtype) != 0 ||
        append_u16(packet, &off, BUF_SIZE, DNS_CLASS_IN) != 0) {
        return 0;
    }
    return off;
}

static int count_rr_type(const unsigned char *packet, size_t packet_len, unsigned short want_type) {
    struct dns_header hdr;
    size_t cursor = sizeof(hdr);
    unsigned short total_answers;
    int matches = 0;
    unsigned short i;

    memcpy(&hdr, packet, sizeof(hdr));
    total_answers = ntohs(hdr.ancount);
    for (i = 0; i < ntohs(hdr.qdcount); i++) {
        char qname[MAX_NAME];
        if (decode_name(packet, packet_len, &cursor, qname, sizeof(qname)) != 0 || cursor + 4 > packet_len) {
            return -1;
        }
        cursor += 4;
    }
    for (i = 0; i < total_answers; i++) {
        char name[MAX_NAME];
        unsigned short rrtype;
        unsigned short rdlength;

        if (decode_name(packet, packet_len, &cursor, name, sizeof(name)) != 0 || cursor + 10 > packet_len) {
            return -1;
        }
        memcpy(&rrtype, packet + cursor, 2);
        memcpy(&rdlength, packet + cursor + 8, 2);
        cursor += 10;
        rrtype = ntohs(rrtype);
        rdlength = ntohs(rdlength);
        if (cursor + rdlength > packet_len) {
            return -1;
        }
        if (rrtype == want_type) {
            matches++;
        }
        cursor += rdlength;
    }
    return matches;
}

int main(void) {
    struct config cfg;
    struct link_context response_link;
    struct service_record_set snapshot;
    struct sockaddr_in mdns_dest;
    struct sockaddr_in source;
    unsigned char query[BUF_SIZE];
    size_t query_len;

    configure_base(&cfg);
    memset(&response_link, 0, sizeof(response_link));
    snprintf(response_link.name, sizeof(response_link.name), "%s", "bridge0");
    response_link.flags = IFF_UP | IFF_RUNNING;
    response_link.ipv4[0].addr = inet_addr("10.0.1.77");
    response_link.ipv4[0].netmask = inet_addr("255.255.255.0");
    response_link.ipv4_count = 1;
    response_link.mdns_ipv4_transport = 1;
    response_link.mdns_ipv4_transport_addr = response_link.ipv4[0].addr;
    memset(&snapshot, 0, sizeof(snapshot));
    memset(&mdns_dest, 0, sizeof(mdns_dest));
    mdns_dest.sin_family = AF_INET;
    mdns_dest.sin_port = htons(MDNS_PORT);
    mdns_dest.sin_addr.s_addr = inet_addr(MDNS_GROUP);
    memset(&source, 0, sizeof(source));
    source.sin_family = AF_INET;
    source.sin_port = htons(62001);
    source.sin_addr.s_addr = inet_addr("10.0.1.42");

    reset_captures();
    query_len = make_query(query, cfg.service_type, DNS_TYPE_PTR);
    if (query_len == 0 ||
        handle_query(1, query, query_len, &mdns_dest, &source, &cfg, &response_link, &snapshot, 0) != 0) {
        return 1;
    }
    if (captured_count != 0) {
        return 2;
    }

    reset_captures();
    query_len = make_query(query, cfg.host_fqdn, DNS_TYPE_A);
    if (query_len == 0 ||
        handle_query(1, query, query_len, &mdns_dest, &source, &cfg, &response_link, &snapshot, 0) != 0) {
        return 3;
    }
    if (captured_count != 1 ||
        count_rr_type(captured_packets[0], captured_lengths[0], DNS_TYPE_A) != 1 ||
        count_rr_type(captured_packets[0], captured_lengths[0], DNS_TYPE_PTR) != 0) {
        return 4;
    }

    printf("ok\n");
    return 0;
}
'''.replace("@MDNS_SOURCE@", mdns_source)
        run = self._compile_and_run_c_helper(source, "mdns_diskless_host_a_no_smb")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout.strip(), "ok")

    def test_mdns_advertiser_suppresses_fresh_known_answer_a_records(self) -> None:
        mdns_source = (REPO_ROOT / "build" / "mdns-advertiser.c").as_posix()
        source = r'''
#include <arpa/inet.h>
#include <netinet/in.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/types.h>

ssize_t fake_sendto(int sockfd, const void *buf, size_t len, int flags,
                    const struct sockaddr *dest, socklen_t dest_len);

#define sendto fake_sendto
#define main mdns_advertiser_main
#include "@MDNS_SOURCE@"
#undef main
#undef sendto

static unsigned char captured_packet[BUF_SIZE];
static size_t captured_len = 0;
static size_t captured_count = 0;

ssize_t fake_sendto(int sockfd, const void *buf, size_t len, int flags,
                    const struct sockaddr *dest, socklen_t dest_len) {
    (void)sockfd;
    (void)flags;
    (void)dest;
    (void)dest_len;
    memcpy(captured_packet, buf, len);
    captured_len = len;
    captured_count++;
    return (ssize_t)len;
}

static void reset_captures(void) {
    memset(captured_packet, 0, sizeof(captured_packet));
    captured_len = 0;
    captured_count = 0;
}

static void configure_base(struct config *cfg) {
    memset(cfg, 0, sizeof(*cfg));
    snprintf(cfg->instance_name, sizeof(cfg->instance_name), "%s", "Alton Time Capsule");
    snprintf(cfg->host_label, sizeof(cfg->host_label), "%s", "alton-time-capsule");
    snprintf(cfg->host_fqdn, sizeof(cfg->host_fqdn), "%s", "alton-time-capsule.local.");
    snprintf(cfg->service_type, sizeof(cfg->service_type), "%s", "_smb._tcp.local.");
    snprintf(cfg->adisk_service_type, sizeof(cfg->adisk_service_type), "%s", "_adisk._tcp.local.");
    snprintf(cfg->device_info_service_type, sizeof(cfg->device_info_service_type), "%s", "_device-info._tcp.local.");
    snprintf(cfg->airport_service_type, sizeof(cfg->airport_service_type), "%s", "_airport._tcp.local.");
    cfg->port = 445;
    cfg->ttl = 120;
}

static size_t make_query_with_known_a_pair(unsigned char *packet, const struct config *cfg,
                                           uint32_t ttl, uint32_t first_addr, int include_second,
                                           uint32_t second_addr) {
    struct dns_header hdr;
    size_t off = sizeof(hdr);
    memset(&hdr, 0, sizeof(hdr));
    hdr.qdcount = htons(1);
    hdr.ancount = htons(include_second ? 2 : 1);
    memcpy(packet, &hdr, sizeof(hdr));
    if (encode_name(packet, &off, BUF_SIZE, cfg->host_fqdn) != 0 ||
        append_u16(packet, &off, BUF_SIZE, DNS_TYPE_A) != 0 ||
        append_u16(packet, &off, BUF_SIZE, DNS_CLASS_IN) != 0 ||
        encode_name(packet, &off, BUF_SIZE, cfg->host_fqdn) != 0 ||
        append_u16(packet, &off, BUF_SIZE, DNS_TYPE_A) != 0 ||
        append_u16(packet, &off, BUF_SIZE, DNS_CLASS_IN_UNIQUE) != 0 ||
        append_u32(packet, &off, BUF_SIZE, ttl) != 0 ||
        append_u16(packet, &off, BUF_SIZE, 4) != 0 ||
        append_bytes(packet, &off, BUF_SIZE, &first_addr, 4) != 0) {
        return 0;
    }
    if (include_second &&
        (encode_name(packet, &off, BUF_SIZE, cfg->host_fqdn) != 0 ||
         append_u16(packet, &off, BUF_SIZE, DNS_TYPE_A) != 0 ||
         append_u16(packet, &off, BUF_SIZE, DNS_CLASS_IN_UNIQUE) != 0 ||
         append_u32(packet, &off, BUF_SIZE, ttl) != 0 ||
         append_u16(packet, &off, BUF_SIZE, 4) != 0 ||
         append_bytes(packet, &off, BUF_SIZE, &second_addr, 4) != 0)) {
        return 0;
    }
    return off;
}

static size_t make_query_with_known_a(unsigned char *packet, const struct config *cfg,
                                      uint32_t ttl, uint32_t known_addr) {
    return make_query_with_known_a_pair(packet, cfg, ttl, known_addr, 0, 0);
}

static int count_rr_type(const unsigned char *packet, size_t packet_len, unsigned short want_type) {
    struct dns_header hdr;
    size_t cursor = sizeof(hdr);
    unsigned short total_answers;
    int matches = 0;
    unsigned short i;

    memcpy(&hdr, packet, sizeof(hdr));
    total_answers = ntohs(hdr.ancount);
    for (i = 0; i < ntohs(hdr.qdcount); i++) {
        char qname[MAX_NAME];
        if (decode_name(packet, packet_len, &cursor, qname, sizeof(qname)) != 0 || cursor + 4 > packet_len) {
            return -1;
        }
        cursor += 4;
    }
    for (i = 0; i < total_answers; i++) {
        char name[MAX_NAME];
        unsigned short rrtype;
        unsigned short rdlength;

        if (decode_name(packet, packet_len, &cursor, name, sizeof(name)) != 0 || cursor + 10 > packet_len) {
            return -1;
        }
        memcpy(&rrtype, packet + cursor, 2);
        memcpy(&rdlength, packet + cursor + 8, 2);
        cursor += 10;
        rrtype = ntohs(rrtype);
        rdlength = ntohs(rdlength);
        if (cursor + rdlength > packet_len) {
            return -1;
        }
        if (rrtype == want_type) {
            matches++;
        }
        cursor += rdlength;
    }
    return matches;
}

int main(void) {
    struct config cfg;
    struct link_context response_link;
    struct service_record_set snapshot;
    struct sockaddr_in mdns_dest;
    struct sockaddr_in source;
    unsigned char query[BUF_SIZE];
    size_t query_len;
    uint32_t primary_addr;
    uint32_t link_local_addr;

    configure_base(&cfg);
    primary_addr = inet_addr("10.0.1.77");
    memset(&response_link, 0, sizeof(response_link));
    snprintf(response_link.name, sizeof(response_link.name), "%s", "bridge0");
    response_link.flags = IFF_UP | IFF_RUNNING;
    response_link.ipv4[0].addr = primary_addr;
    response_link.ipv4[0].netmask = inet_addr("255.255.255.0");
    response_link.ipv4_count = 1;
    response_link.mdns_ipv4_transport = 1;
    response_link.mdns_ipv4_transport_addr = primary_addr;
    memset(&snapshot, 0, sizeof(snapshot));
    memset(&mdns_dest, 0, sizeof(mdns_dest));
    mdns_dest.sin_family = AF_INET;
    mdns_dest.sin_port = htons(MDNS_PORT);
    mdns_dest.sin_addr.s_addr = inet_addr(MDNS_GROUP);
    memset(&source, 0, sizeof(source));
    source.sin_family = AF_INET;
    source.sin_port = htons(62001);
    source.sin_addr.s_addr = inet_addr("10.0.1.42");

    reset_captures();
    query_len = make_query_with_known_a(query, &cfg, 100, primary_addr);
    if (query_len == 0 ||
        handle_query(1, query, query_len, &mdns_dest, &source, &cfg, &response_link, &snapshot, 0) != 0 ||
        captured_count != 0) {
        return 1;
    }

    link_local_addr = inet_addr("169.254.44.55");
    if (response_link.ipv4_count >= MAX_LINK_IPV4_ADDRS) {
        return 2;
    }
    response_link.ipv4[response_link.ipv4_count].addr = link_local_addr;
    response_link.ipv4[response_link.ipv4_count].netmask = ipv4_link_local_netmask();
    response_link.ipv4_count++;

    reset_captures();
    query_len = make_query_with_known_a(query, &cfg, 100, primary_addr);
    if (query_len == 0 ||
        handle_query(1, query, query_len, &mdns_dest, &source, &cfg, &response_link, &snapshot, 0) != 0 ||
        captured_count != 1 ||
        count_rr_type(captured_packet, captured_len, DNS_TYPE_A) != 1) {
        return 3;
    }

    reset_captures();
    query_len = make_query_with_known_a_pair(query, &cfg, 100, primary_addr, 1, link_local_addr);
    if (query_len == 0 ||
        handle_query(1, query, query_len, &mdns_dest, &source, &cfg, &response_link, &snapshot, 0) != 0 ||
        captured_count != 0) {
        return 4;
    }

    reset_captures();
    query_len = make_query_with_known_a(query, &cfg, 10, primary_addr);
    if (query_len == 0 ||
        handle_query(1, query, query_len, &mdns_dest, &source, &cfg, &response_link, &snapshot, 0) != 0 ||
        captured_count != 1 ||
        count_rr_type(captured_packet, captured_len, DNS_TYPE_A) != 2) {
        return 5;
    }

    reset_captures();
    query_len = make_query_with_known_a(query, &cfg, 100, inet_addr("10.0.1.88"));
    if (query_len == 0 ||
        handle_query(1, query, query_len, &mdns_dest, &source, &cfg, &response_link, &snapshot, 0) != 0 ||
        captured_count != 1 ||
        count_rr_type(captured_packet, captured_len, DNS_TYPE_A) != 2) {
        return 6;
    }

    printf("ok\n");
    return 0;
}
'''.replace("@MDNS_SOURCE@", mdns_source)
        run = self._compile_and_run_c_helper(source, "mdns_known_answer_suppression")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout.strip(), "ok")

    def test_mdns_advertiser_defers_tc_and_matches_structured_known_answers(self) -> None:
        mdns_source = (REPO_ROOT / "build" / "mdns-advertiser.c").as_posix()
        source = r'''
#include <arpa/inet.h>
#include <netinet/in.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/types.h>

ssize_t fake_sendto(int sockfd, const void *buf, size_t len, int flags,
                    const struct sockaddr *dest, socklen_t dest_len);

#define sendto fake_sendto
#define main mdns_advertiser_main
#include "@MDNS_SOURCE@"
#undef main
#undef sendto

static unsigned char captured_packet[BUF_SIZE];
static size_t captured_len = 0;
static size_t captured_count = 0;

ssize_t fake_sendto(int sockfd, const void *buf, size_t len, int flags,
                    const struct sockaddr *dest, socklen_t dest_len) {
    (void)sockfd;
    (void)flags;
    (void)dest;
    (void)dest_len;
    memcpy(captured_packet, buf, len);
    captured_len = len;
    captured_count++;
    return (ssize_t)len;
}

static void reset_captures(void) {
    memset(captured_packet, 0, sizeof(captured_packet));
    captured_len = 0;
    captured_count = 0;
}

static void configure_base(struct config *cfg) {
    memset(cfg, 0, sizeof(*cfg));
    snprintf(cfg->instance_name, sizeof(cfg->instance_name), "%s", "Alton Time Capsule");
    snprintf(cfg->host_label, sizeof(cfg->host_label), "%s", "alton-time-capsule");
    snprintf(cfg->host_fqdn, sizeof(cfg->host_fqdn), "%s", "alton-time-capsule.local.");
    snprintf(cfg->service_type, sizeof(cfg->service_type), "%s", "_smb._tcp.local.");
    snprintf(cfg->adisk_service_type, sizeof(cfg->adisk_service_type), "%s", "_adisk._tcp.local.");
    snprintf(cfg->device_info_service_type, sizeof(cfg->device_info_service_type), "%s", "_device-info._tcp.local.");
    snprintf(cfg->airport_service_type, sizeof(cfg->airport_service_type), "%s", "_airport._tcp.local.");
    cfg->port = 445;
    cfg->ttl = 120;
}

static int count_rr_type(const unsigned char *packet, size_t packet_len, unsigned short want_type) {
    struct dns_header hdr;
    size_t cursor = sizeof(hdr);
    unsigned short total_answers;
    int matches = 0;
    unsigned short i;

    memcpy(&hdr, packet, sizeof(hdr));
    total_answers = ntohs(hdr.ancount);
    for (i = 0; i < ntohs(hdr.qdcount); i++) {
        char qname[MAX_NAME];
        if (decode_name(packet, packet_len, &cursor, qname, sizeof(qname)) != 0 || cursor + 4 > packet_len) {
            return -1;
        }
        cursor += 4;
    }
    for (i = 0; i < total_answers; i++) {
        char name[MAX_NAME];
        unsigned short rrtype;
        unsigned short rdlength;

        if (decode_name(packet, packet_len, &cursor, name, sizeof(name)) != 0 || cursor + 10 > packet_len) {
            return -1;
        }
        memcpy(&rrtype, packet + cursor, 2);
        memcpy(&rdlength, packet + cursor + 8, 2);
        cursor += 10;
        rrtype = ntohs(rrtype);
        rdlength = ntohs(rdlength);
        if (cursor + rdlength > packet_len) {
            return -1;
        }
        if (rrtype == want_type) {
            matches++;
        }
        cursor += rdlength;
    }
    return matches;
}

static int append_question(unsigned char *packet, size_t *off, const char *qname,
                           unsigned short qtype) {
    return encode_name(packet, off, BUF_SIZE, qname) != 0 ||
           append_u16(packet, off, BUF_SIZE, qtype) != 0 ||
           append_u16(packet, off, BUF_SIZE, DNS_CLASS_IN) != 0;
}

static size_t make_tc_host_a_query(unsigned char *packet, const struct config *cfg) {
    struct dns_header hdr;
    size_t off = sizeof(hdr);
    memset(&hdr, 0, sizeof(hdr));
    hdr.flags = htons(DNS_FLAG_TC);
    hdr.qdcount = htons(1);
    memcpy(packet, &hdr, sizeof(hdr));
    if (append_question(packet, &off, cfg->host_fqdn, DNS_TYPE_A) != 0) {
        return 0;
    }
    return off;
}

static size_t make_known_a_only(unsigned char *packet, const struct config *cfg, uint32_t known_addr) {
    struct dns_header hdr;
    size_t off = sizeof(hdr);
    memset(&hdr, 0, sizeof(hdr));
    hdr.ancount = htons(1);
    memcpy(packet, &hdr, sizeof(hdr));
    if (encode_name(packet, &off, BUF_SIZE, cfg->host_fqdn) != 0 ||
        append_u16(packet, &off, BUF_SIZE, DNS_TYPE_A) != 0 ||
        append_u16(packet, &off, BUF_SIZE, DNS_CLASS_IN_UNIQUE) != 0 ||
        append_u32(packet, &off, BUF_SIZE, 100) != 0 ||
        append_u16(packet, &off, BUF_SIZE, 4) != 0 ||
        append_bytes(packet, &off, BUF_SIZE, &known_addr, 4) != 0) {
        return 0;
    }
    return off;
}

static size_t make_txt_query_with_known(unsigned char *packet, const char *instance_fqdn,
                                        const char *known_txt) {
    struct dns_header hdr;
    size_t off = sizeof(hdr);
    unsigned char txt_len = (unsigned char)strlen(known_txt);
    memset(&hdr, 0, sizeof(hdr));
    hdr.qdcount = htons(1);
    hdr.ancount = htons(1);
    memcpy(packet, &hdr, sizeof(hdr));
    if (append_question(packet, &off, instance_fqdn, DNS_TYPE_TXT) != 0 ||
        encode_name(packet, &off, BUF_SIZE, instance_fqdn) != 0 ||
        append_u16(packet, &off, BUF_SIZE, DNS_TYPE_TXT) != 0 ||
        append_u16(packet, &off, BUF_SIZE, DNS_CLASS_IN_UNIQUE) != 0 ||
        append_u32(packet, &off, BUF_SIZE, 100) != 0 ||
        append_u16(packet, &off, BUF_SIZE, (uint16_t)(1 + txt_len)) != 0 ||
        append_bytes(packet, &off, BUF_SIZE, &txt_len, 1) != 0 ||
        append_bytes(packet, &off, BUF_SIZE, known_txt, txt_len) != 0) {
        return 0;
    }
    return off;
}

static size_t make_srv_query_with_known(unsigned char *packet, const struct config *cfg,
                                        const char *instance_fqdn, unsigned short port) {
    struct dns_header hdr;
    size_t off = sizeof(hdr);
    size_t rdlength_offset;
    size_t rdata_start;
    uint16_t rdlength;
    memset(&hdr, 0, sizeof(hdr));
    hdr.qdcount = htons(1);
    hdr.ancount = htons(1);
    memcpy(packet, &hdr, sizeof(hdr));
    if (append_question(packet, &off, instance_fqdn, DNS_TYPE_SRV) != 0 ||
        encode_name(packet, &off, BUF_SIZE, instance_fqdn) != 0 ||
        append_u16(packet, &off, BUF_SIZE, DNS_TYPE_SRV) != 0 ||
        append_u16(packet, &off, BUF_SIZE, DNS_CLASS_IN_UNIQUE) != 0 ||
        append_u32(packet, &off, BUF_SIZE, 100) != 0) {
        return 0;
    }
    rdlength_offset = off;
    if (append_u16(packet, &off, BUF_SIZE, 0) != 0) {
        return 0;
    }
    rdata_start = off;
    if (append_u16(packet, &off, BUF_SIZE, 0) != 0 ||
        append_u16(packet, &off, BUF_SIZE, 0) != 0 ||
        append_u16(packet, &off, BUF_SIZE, port) != 0 ||
        encode_name(packet, &off, BUF_SIZE, cfg->host_fqdn) != 0) {
        return 0;
    }
    rdlength = htons((uint16_t)(off - rdata_start));
    memcpy(packet + rdlength_offset, &rdlength, 2);
    return off;
}

int main(void) {
    struct config cfg;
    struct link_context response_link;
    struct service_record_set snapshot;
    struct sockaddr_in mdns_dest;
    struct sockaddr_in source;
    unsigned char query[BUF_SIZE];
    size_t query_len;
    char instance_fqdn[MAX_NAME];
    uint32_t primary_addr;
    uint32_t link_local_addr;

    configure_base(&cfg);
    primary_addr = inet_addr("10.0.1.77");
    if (build_instance_fqdn(instance_fqdn, sizeof(instance_fqdn), cfg.instance_name, cfg.service_type) != 0) {
        return 1;
    }
    memset(&response_link, 0, sizeof(response_link));
    snprintf(response_link.name, sizeof(response_link.name), "%s", "bridge0");
    response_link.flags = IFF_UP | IFF_RUNNING;
    response_link.ipv4[0].addr = primary_addr;
    response_link.ipv4[0].netmask = inet_addr("255.255.255.0");
    response_link.ipv4_count = 1;
    response_link.mdns_ipv4_transport = 1;
    response_link.mdns_ipv4_transport_addr = primary_addr;
    link_local_addr = inet_addr("169.254.44.55");
    response_link.ipv4[response_link.ipv4_count].addr = link_local_addr;
    response_link.ipv4[response_link.ipv4_count].netmask = ipv4_link_local_netmask();
    response_link.ipv4_count++;

    memset(&snapshot, 0, sizeof(snapshot));
    memset(&mdns_dest, 0, sizeof(mdns_dest));
    mdns_dest.sin_family = AF_INET;
    mdns_dest.sin_port = htons(MDNS_PORT);
    mdns_dest.sin_addr.s_addr = inet_addr(MDNS_GROUP);
    memset(&source, 0, sizeof(source));
    source.sin_family = AF_INET;
    source.sin_port = htons(MDNS_PORT);
    source.sin_addr.s_addr = inet_addr("10.0.1.42");

    reset_captures();
    query_len = make_tc_host_a_query(query, &cfg);
    if (query_len == 0 ||
        handle_query(1, query, query_len, &mdns_dest, &source, &cfg, &response_link, &snapshot, 0) != 0 ||
        captured_count != 0 ||
        !g_deferred_response.active) {
        return 2;
    }
    query_len = make_known_a_only(query, &cfg, primary_addr);
    if (query_len == 0 ||
        handle_query(1, query, query_len, &mdns_dest, &source, &cfg, &response_link, &snapshot, 0) != 0 ||
        captured_count != 1 ||
        count_rr_type(captured_packet, captured_len, DNS_TYPE_A) != 1 ||
        g_deferred_response.active) {
        return 3;
    }

    reset_captures();
    query_len = make_txt_query_with_known(query, instance_fqdn, "");
    if (query_len == 0 ||
        handle_query(1, query, query_len, &mdns_dest, &source, &cfg, &response_link, &snapshot, 0) != 0 ||
        captured_count != 0) {
        return 4;
    }
    query_len = make_txt_query_with_known(query, instance_fqdn, "x");
    if (query_len == 0 ||
        handle_query(1, query, query_len, &mdns_dest, &source, &cfg, &response_link, &snapshot, 0) != 0 ||
        captured_count != 1 ||
        count_rr_type(captured_packet, captured_len, DNS_TYPE_TXT) != 1) {
        return 5;
    }

    reset_captures();
    query_len = make_srv_query_with_known(query, &cfg, instance_fqdn, cfg.port);
    if (query_len == 0 ||
        handle_query(1, query, query_len, &mdns_dest, &source, &cfg, &response_link, &snapshot, 0) != 0 ||
        captured_count != 1 ||
        count_rr_type(captured_packet, captured_len, DNS_TYPE_SRV) != 0 ||
        count_rr_type(captured_packet, captured_len, DNS_TYPE_A) != 2) {
        return 6;
    }
    reset_captures();
    query_len = make_srv_query_with_known(query, &cfg, instance_fqdn, (unsigned short)(cfg.port + 1));
    if (query_len == 0 ||
        handle_query(1, query, query_len, &mdns_dest, &source, &cfg, &response_link, &snapshot, 0) != 0 ||
        captured_count != 1 ||
        count_rr_type(captured_packet, captured_len, DNS_TYPE_SRV) != 1) {
        return 7;
    }

    printf("ok\n");
    return 0;
}
'''.replace("@MDNS_SOURCE@", mdns_source)
        run = self._compile_and_run_c_helper(source, "mdns_tc_and_structured_known_answers")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout.strip(), "ok")

    def test_mdns_advertiser_query_response_preserves_snapshot_suppression(self) -> None:
        mdns_source = (REPO_ROOT / "build" / "mdns-advertiser.c").as_posix()
        source = r'''
#include <arpa/inet.h>
#include <netinet/in.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/types.h>

ssize_t fake_sendto(int sockfd, const void *buf, size_t len, int flags,
                    const struct sockaddr *dest, socklen_t dest_len);

#define sendto fake_sendto
#define main mdns_advertiser_main
#include "@MDNS_SOURCE@"
#undef main
#undef sendto

static unsigned char captured_packets[8][BUF_SIZE];
static size_t captured_lengths[8];
static size_t captured_count = 0;

ssize_t fake_sendto(int sockfd, const void *buf, size_t len, int flags,
                    const struct sockaddr *dest, socklen_t dest_len) {
    (void)sockfd;
    (void)flags;
    (void)dest;
    (void)dest_len;
    if (captured_count < 8) {
        memcpy(captured_packets[captured_count], buf, len);
        captured_lengths[captured_count] = len;
        captured_count++;
    }
    return (ssize_t)len;
}

static void reset_captures(void) {
    memset(captured_packets, 0, sizeof(captured_packets));
    memset(captured_lengths, 0, sizeof(captured_lengths));
    captured_count = 0;
}

static void configure_base(struct config *cfg) {
    memset(cfg, 0, sizeof(*cfg));
    snprintf(cfg->instance_name, sizeof(cfg->instance_name), "%s", "Alton Time Capsule");
    snprintf(cfg->host_label, sizeof(cfg->host_label), "%s", "alton-time-capsule");
    snprintf(cfg->host_fqdn, sizeof(cfg->host_fqdn), "%s", "alton-time-capsule.local.");
    snprintf(cfg->service_type, sizeof(cfg->service_type), "%s", "_smb._tcp.local.");
    snprintf(cfg->afp_service_type, sizeof(cfg->afp_service_type), "%s", AFP_SERVICE_TYPE);
    snprintf(cfg->adisk_service_type, sizeof(cfg->adisk_service_type), "%s", "_adisk._tcp.local.");
    snprintf(cfg->device_info_service_type, sizeof(cfg->device_info_service_type), "%s", "_device-info._tcp.local.");
    snprintf(cfg->airport_service_type, sizeof(cfg->airport_service_type), "%s", "_airport._tcp.local.");
    snprintf(cfg->airport_syap, sizeof(cfg->airport_syap), "%s", "116");
    cfg->port = 445;
    cfg->afp_port = AFP_DEFAULT_PORT;
    cfg->adisk_port = 9;
    cfg->airport_port = 5009;
    cfg->ttl = 120;
}

static void configure_addrs(struct sockaddr_in *mdns_dest, struct sockaddr_in *source) {
    memset(mdns_dest, 0, sizeof(*mdns_dest));
    mdns_dest->sin_family = AF_INET;
    mdns_dest->sin_port = htons(MDNS_PORT);
    mdns_dest->sin_addr.s_addr = inet_addr(MDNS_GROUP);

    memset(source, 0, sizeof(*source));
    source->sin_family = AF_INET;
    source->sin_port = htons(62001);
    source->sin_addr.s_addr = inet_addr("10.0.1.42");
}

static void add_snapshot_record(struct service_record_set *set, const char *type, const char *instance,
                                const char *host, unsigned short port, const char *txt) {
    struct service_record *record = &set->records[set->count++];
    memset(record, 0, sizeof(*record));
    snprintf(record->service_type, sizeof(record->service_type), "%s", type);
    snprintf(record->instance_name, sizeof(record->instance_name), "%s", instance);
    build_instance_fqdn(record->instance_fqdn, sizeof(record->instance_fqdn), instance, type);
    snprintf(record->host_label, sizeof(record->host_label), "%s", host);
    snprintf(record->host_fqdn, sizeof(record->host_fqdn), "%s.local.", host);
    record->port = port;
    if (txt != NULL) {
        snprintf(record->txt[0], sizeof(record->txt[0]), "%s", txt);
        record->txt_len[0] = (uint8_t)strlen(record->txt[0]);
        record->txt_count = 1;
    }
}

static size_t make_query(unsigned char *packet, const char *qname, unsigned short qtype) {
    struct dns_header hdr;
    size_t off = sizeof(hdr);
    memset(&hdr, 0, sizeof(hdr));
    hdr.qdcount = htons(1);
    memcpy(packet, &hdr, sizeof(hdr));
    if (encode_name(packet, &off, BUF_SIZE, qname) != 0 ||
        append_u16(packet, &off, BUF_SIZE, qtype) != 0 ||
        append_u16(packet, &off, BUF_SIZE, DNS_CLASS_IN) != 0) {
        return 0;
    }
    return off;
}

static int count_rr_type(const unsigned char *packet, size_t packet_len, unsigned short want_type) {
    struct dns_header hdr;
    size_t cursor = sizeof(hdr);
    unsigned short total_answers;
    int matches = 0;
    unsigned short i;

    memcpy(&hdr, packet, sizeof(hdr));
    total_answers = ntohs(hdr.ancount);
    for (i = 0; i < ntohs(hdr.qdcount); i++) {
        char qname[MAX_NAME];
        if (decode_name(packet, packet_len, &cursor, qname, sizeof(qname)) != 0 || cursor + 4 > packet_len) {
            return -1;
        }
        cursor += 4;
    }
    for (i = 0; i < total_answers; i++) {
        char name[MAX_NAME];
        unsigned short rrtype;
        unsigned short rdlength;

        if (decode_name(packet, packet_len, &cursor, name, sizeof(name)) != 0 || cursor + 10 > packet_len) {
            return -1;
        }
        memcpy(&rrtype, packet + cursor, 2);
        memcpy(&rdlength, packet + cursor + 8, 2);
        cursor += 10;
        rrtype = ntohs(rrtype);
        rdlength = ntohs(rdlength);
        if (cursor + rdlength > packet_len) {
            return -1;
        }
        if (rrtype == want_type) {
            matches++;
        }
        cursor += rdlength;
    }
    return matches;
}

static int count_ptr_target(const unsigned char *packet, size_t packet_len, const char *target) {
    struct dns_header hdr;
    size_t cursor = sizeof(hdr);
    unsigned short total_answers;
    int matches = 0;
    unsigned short i;

    if (packet_len < sizeof(hdr)) {
        return -1;
    }
    memcpy(&hdr, packet, sizeof(hdr));
    total_answers = ntohs(hdr.ancount);
    for (i = 0; i < ntohs(hdr.qdcount); i++) {
        char qname[MAX_NAME];
        if (decode_name(packet, packet_len, &cursor, qname, sizeof(qname)) != 0 || cursor + 4 > packet_len) {
            return -1;
        }
        cursor += 4;
    }
    for (i = 0; i < total_answers; i++) {
        char name[MAX_NAME];
        char ptr_target[MAX_NAME];
        size_t rdata_cursor;
        size_t rdata_end;
        unsigned short rrtype;
        unsigned short rdlength;

        if (decode_name(packet, packet_len, &cursor, name, sizeof(name)) != 0 || cursor + 10 > packet_len) {
            return -1;
        }
        memcpy(&rrtype, packet + cursor, 2);
        memcpy(&rdlength, packet + cursor + 8, 2);
        cursor += 10;
        rrtype = ntohs(rrtype);
        rdlength = ntohs(rdlength);
        if (cursor + rdlength > packet_len) {
            return -1;
        }
        rdata_cursor = cursor;
        rdata_end = cursor + rdlength;
        if (rrtype == DNS_TYPE_PTR &&
            decode_name(packet, packet_len, &rdata_cursor, ptr_target, sizeof(ptr_target)) == 0 &&
            rdata_cursor == rdata_end &&
            name_equals(ptr_target, target)) {
            matches++;
        }
        cursor += rdlength;
    }
    return matches;
}

static int packet_has_srv_port(const unsigned char *packet, size_t packet_len, unsigned short want_port) {
    struct dns_header hdr;
    size_t cursor = sizeof(hdr);
    unsigned short total_answers;
    unsigned short i;

    if (packet_len < sizeof(hdr)) {
        return 0;
    }
    memcpy(&hdr, packet, sizeof(hdr));
    total_answers = ntohs(hdr.ancount);
    for (i = 0; i < ntohs(hdr.qdcount); i++) {
        char qname[MAX_NAME];
        if (decode_name(packet, packet_len, &cursor, qname, sizeof(qname)) != 0 || cursor + 4 > packet_len) {
            return 0;
        }
        cursor += 4;
    }
    for (i = 0; i < total_answers; i++) {
        char name[MAX_NAME];
        unsigned short rrtype;
        unsigned short rdlength;

        if (decode_name(packet, packet_len, &cursor, name, sizeof(name)) != 0 || cursor + 10 > packet_len) {
            return 0;
        }
        memcpy(&rrtype, packet + cursor, 2);
        memcpy(&rdlength, packet + cursor + 8, 2);
        cursor += 10;
        rrtype = ntohs(rrtype);
        rdlength = ntohs(rdlength);
        if (cursor + rdlength > packet_len) {
            return 0;
        }
        if (rrtype == DNS_TYPE_SRV && rdlength >= 6) {
            unsigned short port;
            memcpy(&port, packet + cursor + 4, 2);
            if (ntohs(port) == want_port) {
                return 1;
            }
        }
        cursor += rdlength;
    }
    return 0;
}

static int packet_has_browse_additionals(const unsigned char *packet, size_t packet_len) {
    return count_rr_type(packet, packet_len, DNS_TYPE_PTR) == 1 &&
           count_rr_type(packet, packet_len, DNS_TYPE_SRV) == 1 &&
           count_rr_type(packet, packet_len, DNS_TYPE_TXT) == 1 &&
           count_rr_type(packet, packet_len, DNS_TYPE_A) == 1;
}

int main(void) {
    struct config cfg;
    struct link_context response_link;
    struct service_record_set snapshot;
    struct sockaddr_in mdns_dest;
    struct sockaddr_in source;
    unsigned char query[BUF_SIZE];
    char afp_instance_fqdn[MAX_NAME];
    size_t query_len;

    configure_base(&cfg);
    memset(&response_link, 0, sizeof(response_link));
    snprintf(response_link.name, sizeof(response_link.name), "%s", "bridge0");
    response_link.flags = IFF_UP | IFF_RUNNING;
    response_link.ipv4[0].addr = inet_addr("10.0.1.77");
    response_link.ipv4[0].netmask = inet_addr("255.255.255.0");
    response_link.ipv4_count = 1;
    response_link.mdns_ipv4_transport = 1;
    response_link.mdns_ipv4_transport_addr = response_link.ipv4[0].addr;
    memset(&snapshot, 0, sizeof(snapshot));
    configure_addrs(&mdns_dest, &source);
    add_snapshot_record(&snapshot, "_airport._tcp.local.", "Alton Time Capsule", "Alton-Time-Capsule", 5009, "syAP=116");
    add_snapshot_record(&snapshot, "_afpovertcp._tcp.local.", "Snapshot AFP", "Stale-Host", 1548, NULL);
    if (build_instance_fqdn(afp_instance_fqdn, sizeof(afp_instance_fqdn), cfg.instance_name, cfg.afp_service_type) != 0) {
        return 1;
    }

    reset_captures();
    query_len = make_query(query, cfg.afp_service_type, DNS_TYPE_PTR);
    if (query_len == 0 ||
        handle_query(1, query, query_len, &mdns_dest, &source, &cfg, &response_link, &snapshot, 1) != 0 ||
        captured_count != 0) {
        return 1;
    }

    cfg.advertise_afp = 1;
    reset_captures();
    query_len = make_query(query, cfg.afp_service_type, DNS_TYPE_PTR);
    if (query_len == 0 ||
        handle_query(1, query, query_len, &mdns_dest, &source, &cfg, &response_link, &snapshot, 1) != 0 ||
        captured_count != 1 ||
        !packet_has_browse_additionals(captured_packets[0], captured_lengths[0]) ||
        count_ptr_target(captured_packets[0], captured_lengths[0], afp_instance_fqdn) != 1 ||
        count_ptr_target(captured_packets[0], captured_lengths[0], "Snapshot AFP._afpovertcp._tcp.local.") != 0 ||
        !packet_has_srv_port(captured_packets[0], captured_lengths[0], AFP_DEFAULT_PORT)) {
        return 2;
    }

    reset_captures();
    query_len = make_query(query, afp_instance_fqdn, DNS_TYPE_ANY);
    if (query_len == 0 ||
        handle_query(1, query, query_len, &mdns_dest, &source, &cfg, &response_link, &snapshot, 1) != 0 ||
        captured_count != 1 ||
        count_rr_type(captured_packets[0], captured_lengths[0], DNS_TYPE_PTR) != 0 ||
        count_rr_type(captured_packets[0], captured_lengths[0], DNS_TYPE_SRV) != 1 ||
        count_rr_type(captured_packets[0], captured_lengths[0], DNS_TYPE_TXT) != 1 ||
        count_rr_type(captured_packets[0], captured_lengths[0], DNS_TYPE_A) != 1 ||
        !packet_has_srv_port(captured_packets[0], captured_lengths[0], AFP_DEFAULT_PORT)) {
        return 3;
    }

    reset_captures();
    query_len = make_query(query, DNS_SD_SERVICE_ENUMERATION_NAME, DNS_TYPE_PTR);
    if (query_len == 0 ||
        handle_query(1, query, query_len, &mdns_dest, &source, &cfg, &response_link, &snapshot, 1) != 0 ||
        captured_count != 1 ||
        count_ptr_target(captured_packets[0], captured_lengths[0], cfg.afp_service_type) != 1) {
        return 4;
    }

    reset_captures();
    query_len = make_query(query, "_airport._tcp.local.", DNS_TYPE_PTR);
    if (query_len == 0 ||
        handle_query(1, query, query_len, &mdns_dest, &source, &cfg, &response_link, &snapshot, 1) != 0 ||
        captured_count != 1 ||
        !packet_has_browse_additionals(captured_packets[0], captured_lengths[0])) {
        return 5;
    }

    reset_captures();
    query_len = make_query(query, cfg.service_type, DNS_TYPE_PTR);
    if (query_len == 0 ||
        handle_query(1, query, query_len, &mdns_dest, &source, &cfg, &response_link, &snapshot, 1) != 0 ||
        captured_count != 1 ||
        !packet_has_browse_additionals(captured_packets[0], captured_lengths[0])) {
        return 6;
    }

    printf("ok\n");
    return 0;
}
'''.replace("@MDNS_SOURCE@", mdns_source)
        run = self._compile_and_run_c_helper(source, "mdns_query_snapshot_suppression")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout.strip(), "ok")

    def test_mdns_advertiser_generated_printer_records_overlay_snapshot_records(self) -> None:
        mdns_source = (REPO_ROOT / "build" / "mdns-advertiser.c").as_posix()
        source = r'''
#include <arpa/inet.h>
#include <netinet/in.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/types.h>

ssize_t fake_sendto(int sockfd, const void *buf, size_t len, int flags,
                    const struct sockaddr *dest, socklen_t dest_len);

#define sendto fake_sendto
#define main mdns_advertiser_main
#include "@MDNS_SOURCE@"
#undef main
#undef sendto

static unsigned char captured_packets[16][BUF_SIZE];
static size_t captured_lengths[16];
static size_t captured_count = 0;

ssize_t fake_sendto(int sockfd, const void *buf, size_t len, int flags,
                    const struct sockaddr *dest, socklen_t dest_len) {
    (void)sockfd;
    (void)flags;
    (void)dest;
    (void)dest_len;
    if (captured_count < 16) {
        memcpy(captured_packets[captured_count], buf, len);
        captured_lengths[captured_count] = len;
        captured_count++;
    }
    return (ssize_t)len;
}

static void reset_captures(void) {
    memset(captured_packets, 0, sizeof(captured_packets));
    memset(captured_lengths, 0, sizeof(captured_lengths));
    captured_count = 0;
}

static void configure_base(struct config *cfg) {
    memset(cfg, 0, sizeof(*cfg));
    snprintf(cfg->instance_name, sizeof(cfg->instance_name), "%s", "Alton Time Capsule");
    snprintf(cfg->host_label, sizeof(cfg->host_label), "%s", "alton-time-capsule");
    snprintf(cfg->host_fqdn, sizeof(cfg->host_fqdn), "%s", "alton-time-capsule.local.");
    snprintf(cfg->service_type, sizeof(cfg->service_type), "%s", "_smb._tcp.local.");
    snprintf(cfg->afp_service_type, sizeof(cfg->afp_service_type), "%s", AFP_SERVICE_TYPE);
    snprintf(cfg->adisk_service_type, sizeof(cfg->adisk_service_type), "%s", "_adisk._tcp.local.");
    snprintf(cfg->device_info_service_type, sizeof(cfg->device_info_service_type), "%s", "_device-info._tcp.local.");
    snprintf(cfg->airport_service_type, sizeof(cfg->airport_service_type), "%s", "_airport._tcp.local.");
    snprintf(cfg->airport_wama, sizeof(cfg->airport_wama), "%s", "80:EA:96:E6:58:68");
    snprintf(cfg->riousbprint_instance_name, sizeof(cfg->riousbprint_instance_name), "%s", "Canon MP490 series");
    snprintf(cfg->riousbprint_mfg, sizeof(cfg->riousbprint_mfg), "%s", "Canon");
    snprintf(cfg->riousbprint_mdl, sizeof(cfg->riousbprint_mdl), "%s", "MP490 series");
    snprintf(cfg->riousbprint_cmd, sizeof(cfg->riousbprint_cmd), "%s", "BJL,BJRaster3");
    cfg->port = 445;
    cfg->afp_port = AFP_DEFAULT_PORT;
    cfg->adisk_port = 9;
    cfg->airport_port = 5009;
    cfg->riousbprint_port = 10000;
    cfg->pdl_datastream_port = 9100;
    cfg->ttl = 120;
}

static void configure_link(struct link_context *link) {
    memset(link, 0, sizeof(*link));
    snprintf(link->name, sizeof(link->name), "%s", "bridge0");
    link->flags = IFF_UP | IFF_RUNNING;
    link->ipv4[0].addr = inet_addr("10.0.1.77");
    link->ipv4[0].netmask = inet_addr("255.255.255.0");
    link->ipv4_count = 1;
    link->mdns_ipv4_transport = 1;
    link->mdns_ipv4_transport_addr = link->ipv4[0].addr;
}

static void add_snapshot_record(struct service_record_set *set, const char *type, const char *instance,
                                const char *host, unsigned short port, const char *txt) {
    struct service_record *record = &set->records[set->count++];
    memset(record, 0, sizeof(*record));
    snprintf(record->service_type, sizeof(record->service_type), "%s", type);
    snprintf(record->instance_name, sizeof(record->instance_name), "%s", instance);
    build_instance_fqdn(record->instance_fqdn, sizeof(record->instance_fqdn), instance, type);
    snprintf(record->host_label, sizeof(record->host_label), "%s", host);
    snprintf(record->host_fqdn, sizeof(record->host_fqdn), "%s.local.", host);
    record->port = port;
    if (txt != NULL) {
        snprintf(record->txt[0], sizeof(record->txt[0]), "%s", txt);
        record->txt_len[0] = (uint8_t)strlen(record->txt[0]);
        record->txt_count = 1;
    }
}

static int planned_count_ptr_target(const struct planned_rr_set *planned, const char *target) {
    size_t i;
    int matches = 0;

    for (i = 0; i < planned->count; i++) {
        char ptr_target[MAX_NAME];
        size_t cursor = 0;
        if (planned->records[i].type == DNS_TYPE_PTR &&
            decode_name(planned->records[i].rdata, planned->records[i].rdlength, &cursor, ptr_target, sizeof(ptr_target)) == 0 &&
            cursor == planned->records[i].rdlength &&
            name_equals(ptr_target, target)) {
            matches++;
        }
    }
    return matches;
}

static int planned_has_srv_port(const struct planned_rr_set *planned, unsigned short want_port) {
    size_t i;

    for (i = 0; i < planned->count; i++) {
        if (planned->records[i].type == DNS_TYPE_SRV && planned->records[i].rdlength >= 6) {
            unsigned short port;
            memcpy(&port, planned->records[i].rdata + 4, 2);
            if (ntohs(port) == want_port) {
                return 1;
            }
        }
    }
    return 0;
}

static int packet_has_srv_port(const unsigned char *packet, size_t packet_len, unsigned short want_port) {
    struct dns_header hdr;
    size_t cursor = sizeof(hdr);
    unsigned short total_answers;
    unsigned short i;

    if (packet_len < sizeof(hdr)) {
        return 0;
    }
    memcpy(&hdr, packet, sizeof(hdr));
    total_answers = ntohs(hdr.ancount);
    for (i = 0; i < ntohs(hdr.qdcount); i++) {
        char qname[MAX_NAME];
        if (decode_name(packet, packet_len, &cursor, qname, sizeof(qname)) != 0 || cursor + 4 > packet_len) {
            return 0;
        }
        cursor += 4;
    }
    for (i = 0; i < total_answers; i++) {
        char name[MAX_NAME];
        unsigned short rrtype;
        unsigned short rdlength;

        if (decode_name(packet, packet_len, &cursor, name, sizeof(name)) != 0 || cursor + 10 > packet_len) {
            return 0;
        }
        memcpy(&rrtype, packet + cursor, 2);
        memcpy(&rdlength, packet + cursor + 8, 2);
        cursor += 10;
        rrtype = ntohs(rrtype);
        rdlength = ntohs(rdlength);
        if (cursor + rdlength > packet_len) {
            return 0;
        }
        if (rrtype == DNS_TYPE_SRV && rdlength >= 6) {
            unsigned short port;
            memcpy(&port, packet + cursor + 4, 2);
            if (ntohs(port) == want_port) {
                return 1;
            }
        }
        cursor += rdlength;
    }
    return 0;
}

static int captured_has_srv_port(unsigned short want_port) {
    size_t i;

    for (i = 0; i < captured_count; i++) {
        if (packet_has_srv_port(captured_packets[i], captured_lengths[i], want_port)) {
            return 1;
        }
    }
    return 0;
}

static int plan_printer_type(const struct config *cfg,
                             const struct link_context *link,
                             const struct service_record_set *snapshot,
                             const char *service_type,
                             const char *riousbprint_instance_fqdn,
                             const char *pdl_datastream_instance_fqdn,
                             struct planned_rr_set *planned) {
    memset(planned, 0, sizeof(*planned));
    return plan_question_answers(planned,
                                 MDNS_REPLY_MULTICAST,
                                 service_type,
                                 DNS_TYPE_PTR,
                                 cfg,
                                 link,
                                 snapshot,
                                 1,
                                 "",
                                 "",
                                 "",
                                 "",
                                 "",
                                 riousbprint_instance_fqdn,
                                 pdl_datastream_instance_fqdn);
}

int main(void) {
    struct config cfg;
    struct link_context response_link;
    struct service_record_set snapshot;
    struct planned_rr_set planned;
    struct sockaddr_in announcement_dest;
    char riousbprint_instance_fqdn[MAX_NAME];
    char pdl_datastream_instance_fqdn[MAX_NAME];

    configure_base(&cfg);
    configure_link(&response_link);
    memset(&snapshot, 0, sizeof(snapshot));
    add_snapshot_record(&snapshot, RIOUSBPRINT_SERVICE_TYPE, "Canon MP490 series", "stale-printer", 10001, "rp=stale");
    add_snapshot_record(&snapshot, RIOUSBPRINT_SERVICE_TYPE, "Other Printer", "other-printer", 10002, "rp=other");
    add_snapshot_record(&snapshot, PDL_DATASTREAM_SERVICE_TYPE, "Canon MP490 series", "stale-printer", 9101, "rp=stale");
    add_snapshot_record(&snapshot, PDL_DATASTREAM_SERVICE_TYPE, "Other Printer", "other-printer", 9102, "rp=other");

    if (build_instance_fqdn(riousbprint_instance_fqdn,
                            sizeof(riousbprint_instance_fqdn),
                            cfg.riousbprint_instance_name,
                            RIOUSBPRINT_SERVICE_TYPE) != 0 ||
        build_instance_fqdn(pdl_datastream_instance_fqdn,
                            sizeof(pdl_datastream_instance_fqdn),
                            cfg.riousbprint_instance_name,
                            PDL_DATASTREAM_SERVICE_TYPE) != 0) {
        return 1;
    }

    if (plan_printer_type(&cfg, &response_link, &snapshot, RIOUSBPRINT_SERVICE_TYPE,
                          riousbprint_instance_fqdn, pdl_datastream_instance_fqdn, &planned) != 0 ||
        planned_count_ptr_target(&planned, "Canon MP490 series._riousbprint._tcp.local.") != 1 ||
        planned_count_ptr_target(&planned, "Other Printer._riousbprint._tcp.local.") != 1 ||
        !planned_has_srv_port(&planned, 10000) ||
        planned_has_srv_port(&planned, 10001) ||
        !planned_has_srv_port(&planned, 10002)) {
        return 2;
    }

    if (plan_printer_type(&cfg, &response_link, &snapshot, PDL_DATASTREAM_SERVICE_TYPE,
                          riousbprint_instance_fqdn, pdl_datastream_instance_fqdn, &planned) != 0 ||
        planned_count_ptr_target(&planned, "Canon MP490 series._pdl-datastream._tcp.local.") != 1 ||
        planned_count_ptr_target(&planned, "Other Printer._pdl-datastream._tcp.local.") != 1 ||
        !planned_has_srv_port(&planned, 9100) ||
        planned_has_srv_port(&planned, 9101) ||
        !planned_has_srv_port(&planned, 9102)) {
        return 3;
    }

    memset(&announcement_dest, 0, sizeof(announcement_dest));
    announcement_dest.sin_family = AF_INET;
    announcement_dest.sin_port = htons(MDNS_PORT);
    announcement_dest.sin_addr.s_addr = inet_addr(MDNS_GROUP);
    reset_captures();
    if (send_announcement(1, &announcement_dest, &cfg, &response_link, cfg.ttl, &snapshot, 1) != 0 ||
        !captured_has_srv_port(10000) ||
        captured_has_srv_port(10001) ||
        !captured_has_srv_port(10002) ||
        !captured_has_srv_port(9100) ||
        captured_has_srv_port(9101) ||
        !captured_has_srv_port(9102)) {
        return 4;
    }

    printf("ok\n");
    return 0;
}
'''.replace("@MDNS_SOURCE@", mdns_source)
        run = self._compile_and_run_c_helper(source, "mdns_generated_printer_snapshot_overlay")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout.strip(), "ok")

    def test_mdns_advertiser_retries_interrupted_sendto(self) -> None:
        mdns_source = (REPO_ROOT / "build" / "mdns-advertiser.c").as_posix()
        source = '''
#include <errno.h>
#include <netinet/in.h>
#include <stdio.h>
#include <sys/socket.h>
#include <sys/types.h>

ssize_t fake_sendto(int sockfd, const void *buf, size_t len, int flags,
                    const struct sockaddr *dest, socklen_t dest_len);

#define sendto fake_sendto
#define main mdns_advertiser_main
#include "{mdns_source}"
#undef main
#undef sendto

static int sendto_call_count = 0;

ssize_t fake_sendto(int sockfd, const void *buf, size_t len, int flags,
                    const struct sockaddr *dest, socklen_t dest_len) {{
    (void)sockfd;
    (void)buf;
    (void)flags;
    (void)dest;
    (void)dest_len;

    sendto_call_count++;
    if (sendto_call_count == 1) {{
        errno = EINTR;
        return -1;
    }}
    return (ssize_t)len;
}}

int main(void) {{
    struct sockaddr_in dest;
    unsigned char packet[4] = {{1, 2, 3, 4}};
    ssize_t sent;

    memset(&dest, 0, sizeof(dest));
    sent = sendto_retry(1, packet, sizeof(packet), 0, (const struct sockaddr *)&dest, sizeof(dest));
    if (sent != (ssize_t)sizeof(packet)) {{
        return 1;
    }}
    if (sendto_call_count != 2) {{
        return 2;
    }}
    return 0;
}}
'''.format(mdns_source=mdns_source)
        run = self._compile_and_run_c_helper(source, "mdns_sendto_eintr")
        self.assertEqual(run.returncode, 0, run.stderr)

    def test_mdns_advertiser_splits_snapshot_announcements_and_keeps_managed_device_info(self) -> None:
        mdns_source = (REPO_ROOT / "build" / "mdns-advertiser.c").as_posix()
        source = '''
#include <stdio.h>
#include <string.h>
#include <sys/types.h>
#include <sys/socket.h>

ssize_t fake_sendto(int sockfd, const void *buf, size_t len, int flags,
                    const struct sockaddr *dest, socklen_t dest_len);

#define sendto fake_sendto
#define main mdns_advertiser_main
#include "{mdns_source}"
#undef main
#undef sendto

static unsigned char captured_packets[16][BUF_SIZE];
static size_t captured_lengths[16];
static size_t captured_count = 0;

ssize_t fake_sendto(int sockfd, const void *buf, size_t len, int flags,
                    const struct sockaddr *dest, socklen_t dest_len) {{
    (void)sockfd;
    (void)flags;
    (void)dest;
    (void)dest_len;
    if (captured_count < 16) {{
        memcpy(captured_packets[captured_count], buf, len);
        captured_lengths[captured_count] = len;
        captured_count++;
    }}
    return (ssize_t)len;
}}

static int count_rr_type(const unsigned char *packet, size_t packet_len, unsigned short want_type) {{
    struct dns_header hdr;
    size_t cursor = sizeof(hdr);
    unsigned short total_answers;
    int matches = 0;
    unsigned short i;

    memcpy(&hdr, packet, sizeof(hdr));
    total_answers = ntohs(hdr.ancount);
    for (i = 0; i < ntohs(hdr.qdcount); i++) {{
        char qname[MAX_NAME];
        if (decode_name(packet, packet_len, &cursor, qname, sizeof(qname)) != 0 || cursor + 4 > packet_len) {{
            return -1;
        }}
        cursor += 4;
    }}
    for (i = 0; i < total_answers; i++) {{
        char name[MAX_NAME];
        unsigned short rrtype;
        unsigned short rrclass;
        unsigned int ttl;
        unsigned short rdlength;

        if (decode_name(packet, packet_len, &cursor, name, sizeof(name)) != 0 || cursor + 10 > packet_len) {{
            return -1;
        }}
        memcpy(&rrtype, packet + cursor, 2);
        memcpy(&rrclass, packet + cursor + 2, 2);
        memcpy(&ttl, packet + cursor + 4, 4);
        memcpy(&rdlength, packet + cursor + 8, 2);
        (void)rrclass;
        (void)ttl;
        cursor += 10;
        rrtype = ntohs(rrtype);
        rdlength = ntohs(rdlength);
        if (cursor + rdlength > packet_len) {{
            return -1;
        }}
        if (rrtype == want_type) {{
            matches++;
        }}
        cursor += rdlength;
    }}
    return matches;
}}

int main(void) {{
    struct config cfg;
    struct link_context response_link;
    struct service_record_set snapshot;
    struct sockaddr_in dest;
    struct service_record *record;
    struct service_record_set parsed;
    struct service_type_set types;
    int total_a = 0;
    int saw_device_info = 0;
    int saw_afp = 0;
    size_t i;

    memset(&cfg, 0, sizeof(cfg));
    snprintf(cfg.instance_name, sizeof(cfg.instance_name), "%s", "Time Capsule Samba 4");
    snprintf(cfg.host_label, sizeof(cfg.host_label), "%s", "timecapsulesamba4");
    snprintf(cfg.host_fqdn, sizeof(cfg.host_fqdn), "%s", "timecapsulesamba4.local.");
    snprintf(cfg.service_type, sizeof(cfg.service_type), "%s", "_smb._tcp.local.");
    snprintf(cfg.device_info_service_type, sizeof(cfg.device_info_service_type), "%s", "_device-info._tcp.local.");
    snprintf(cfg.adisk_service_type, sizeof(cfg.adisk_service_type), "%s", "_adisk._tcp.local.");
    snprintf(cfg.airport_service_type, sizeof(cfg.airport_service_type), "%s", "_airport._tcp.local.");
    snprintf(cfg.device_model, sizeof(cfg.device_model), "%s", "TimeCapsule8,119");
    snprintf(cfg.adisk_sys_wama, sizeof(cfg.adisk_sys_wama), "%s", "80:EA:96:E6:58:68");
    cfg.port = 445;
    cfg.adisk_port = 9;
    cfg.ttl = 120;
    if (add_adisk_disk_config(&cfg, "Data", "dk2", "c4f673b8-c422-4da7-92a1-54bffe406af2", "0x82") != 0) {{
        return 1;
    }}
    memset(&response_link, 0, sizeof(response_link));
    snprintf(response_link.name, sizeof(response_link.name), "%s", "bridge0");
    response_link.flags = IFF_UP | IFF_RUNNING;
    response_link.ipv4[0].addr = inet_addr("192.168.1.217");
    response_link.ipv4[0].netmask = inet_addr("255.255.255.0");
    response_link.ipv4_count = 1;
    response_link.mdns_ipv4_transport = 1;
    response_link.mdns_ipv4_transport_addr = response_link.ipv4[0].addr;

    memset(&snapshot, 0, sizeof(snapshot));

    record = &snapshot.records[snapshot.count++];
    snprintf(record->service_type, sizeof(record->service_type), "%s", "_sleep-proxy._udp.local.");
    snprintf(record->instance_name, sizeof(record->instance_name), "%s", "Sleep");
    snprintf(record->instance_fqdn, sizeof(record->instance_fqdn), "%s", "Sleep._sleep-proxy._udp.local.");
    snprintf(record->host_label, sizeof(record->host_label), "%s", "Jamess-AirPort-Time-Capsule");
    snprintf(record->host_fqdn, sizeof(record->host_fqdn), "%s", "Jamess-AirPort-Time-Capsule.local.");
    record->port = 60459;

    record = &snapshot.records[snapshot.count++];
    snprintf(record->service_type, sizeof(record->service_type), "%s", "_airport._tcp.local.");
    snprintf(record->instance_name, sizeof(record->instance_name), "%s", "James's AirPort Time Capsule");
    snprintf(record->instance_fqdn, sizeof(record->instance_fqdn), "%s", "James's AirPort Time Capsule._airport._tcp.local.");
    snprintf(record->host_label, sizeof(record->host_label), "%s", "Jamess-AirPort-Time-Capsule");
    snprintf(record->host_fqdn, sizeof(record->host_fqdn), "%s", "Jamess-AirPort-Time-Capsule.local.");
    record->port = 5009;
    snprintf(record->txt[0], sizeof(record->txt[0]), "%s",
             "waMA=80-EA-96-E6-58-68,raMA=80-EA-96-EB-2E-7D,raM2=80-EA-96-EB-2E-7C,raSt=3,raNA=0,syFl=0xA0C,syAP=119,syVs=7.9.1,srcv=79100.2,bjSd=99");
    record->txt_len[0] = (uint8_t)strlen(record->txt[0]);
    record->txt_count = 1;

    record = &snapshot.records[snapshot.count++];
    snprintf(record->service_type, sizeof(record->service_type), "%s", "_afpovertcp._tcp.local.");
    snprintf(record->instance_name, sizeof(record->instance_name), "%s", "AFP");
    snprintf(record->instance_fqdn, sizeof(record->instance_fqdn), "%s", "AFP._afpovertcp._tcp.local.");
    snprintf(record->host_label, sizeof(record->host_label), "%s", "Jamess-AirPort-Time-Capsule");
    snprintf(record->host_fqdn, sizeof(record->host_fqdn), "%s", "Jamess-AirPort-Time-Capsule.local.");
    record->port = 548;

    record = &snapshot.records[snapshot.count++];
    snprintf(record->service_type, sizeof(record->service_type), "%s", "_device-info._tcp.local.");
    snprintf(record->instance_name, sizeof(record->instance_name), "%s", "Snapshot Device");
    snprintf(record->instance_fqdn, sizeof(record->instance_fqdn), "%s", "Snapshot Device._device-info._tcp.local.");
    snprintf(record->host_label, sizeof(record->host_label), "%s", "Jamess-AirPort-Time-Capsule");
    snprintf(record->host_fqdn, sizeof(record->host_fqdn), "%s", "Jamess-AirPort-Time-Capsule.local.");
    record->port = 0;
    snprintf(record->txt[0], sizeof(record->txt[0]), "%s", "model=Wrong");
    record->txt_len[0] = (uint8_t)strlen(record->txt[0]);
    record->txt_count = 1;

    memset(&dest, 0, sizeof(dest));
    dest.sin_family = AF_INET;
    dest.sin_port = htons(5353);
    dest.sin_addr.s_addr = inet_addr("224.0.0.251");

    if (send_announcement(1, &dest, &cfg, &response_link, cfg.ttl, &snapshot, 1) != 0) {{
        return 10;
    }}
    if (captured_count < 3) {{
        return 11;
    }}
    for (i = 0; i < captured_count; i++) {{
        int count_a;
        memset(&parsed, 0, sizeof(parsed));
        memset(&types, 0, sizeof(types));
        if (parse_snapshot_rrs(captured_packets[i], captured_lengths[i], &parsed, &types) != 0) {{
            return 12;
        }}
        if (service_type_set_contains(&types, "_device-info._tcp.local.")) {{
            saw_device_info = 1;
        }}
        if (service_type_set_contains(&types, "_afpovertcp._tcp.local.")) {{
            saw_afp = 1;
        }}
        count_a = count_rr_type(captured_packets[i], captured_lengths[i], DNS_TYPE_A);
        if (count_a < 0) {{
            return 13;
        }}
        total_a += count_a;
    }}
    if (!saw_device_info) {{
        return 14;
    }}
    if (saw_afp) {{
        return 15;
    }}
    if (total_a != 2) {{
        return 16;
    }}
    printf("%lu\\n%d\\n%d\\n", (unsigned long)captured_count, saw_device_info, total_a);
    return 0;
}}
'''.format(mdns_source=mdns_source)
        run = self._compile_and_run_c_helper(source, "mdns_announcement_split_test")
        self.assertEqual(run.returncode, 0, run.stderr)
        lines = run.stdout.splitlines()
        self.assertGreaterEqual(int(lines[0]), 3)
        self.assertEqual(lines[1:], ["1", "2"])

    def test_mdns_advertiser_diskless_replays_unsuppressed_snapshot_records(self) -> None:
        mdns_source = (REPO_ROOT / "build" / "mdns-advertiser.c").as_posix()
        source = r'''
#include <arpa/inet.h>
#include <netinet/in.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/types.h>

ssize_t fake_sendto(int sockfd, const void *buf, size_t len, int flags,
                    const struct sockaddr *dest, socklen_t dest_len);

#define sendto fake_sendto
#define main mdns_advertiser_main
#include "@MDNS_SOURCE@"
#undef main
#undef sendto

static unsigned char captured_packets[16][BUF_SIZE];
static size_t captured_lengths[16];
static size_t captured_count = 0;

ssize_t fake_sendto(int sockfd, const void *buf, size_t len, int flags,
                    const struct sockaddr *dest, socklen_t dest_len) {
    (void)sockfd;
    (void)flags;
    (void)dest;
    (void)dest_len;
    if (captured_count < 16) {
        memcpy(captured_packets[captured_count], buf, len);
        captured_lengths[captured_count] = len;
        captured_count++;
    }
    return (ssize_t)len;
}

static int count_rr_type(const unsigned char *packet, size_t packet_len, unsigned short want_type) {
    struct dns_header hdr;
    size_t cursor = sizeof(hdr);
    unsigned short total_answers;
    int matches = 0;
    unsigned short i;

    memcpy(&hdr, packet, sizeof(hdr));
    total_answers = ntohs(hdr.ancount);
    for (i = 0; i < ntohs(hdr.qdcount); i++) {
        char qname[MAX_NAME];
        if (decode_name(packet, packet_len, &cursor, qname, sizeof(qname)) != 0 || cursor + 4 > packet_len) {
            return -1;
        }
        cursor += 4;
    }
    for (i = 0; i < total_answers; i++) {
        char name[MAX_NAME];
        unsigned short rrtype;
        unsigned short rdlength;

        if (decode_name(packet, packet_len, &cursor, name, sizeof(name)) != 0 || cursor + 10 > packet_len) {
            return -1;
        }
        memcpy(&rrtype, packet + cursor, 2);
        memcpy(&rdlength, packet + cursor + 8, 2);
        cursor += 10;
        rrtype = ntohs(rrtype);
        rdlength = ntohs(rdlength);
        if (cursor + rdlength > packet_len) {
            return -1;
        }
        if (rrtype == want_type) {
            matches++;
        }
        cursor += rdlength;
    }
    return matches;
}

static void add_snapshot_record(struct service_record_set *snapshot,
                                const char *type,
                                const char *instance,
                                const char *host,
                                unsigned short port) {
    struct service_record *record = &snapshot->records[snapshot->count++];
    snprintf(record->service_type, sizeof(record->service_type), "%s", type);
    snprintf(record->instance_name, sizeof(record->instance_name), "%s", instance);
    build_instance_fqdn(record->instance_fqdn, sizeof(record->instance_fqdn), instance, type);
    snprintf(record->host_label, sizeof(record->host_label), "%s", host);
    snprintf(record->host_fqdn, sizeof(record->host_fqdn), "%s.local.", host);
    record->port = port;
}

int main(void) {
    struct config cfg;
    struct link_context response_link;
    struct service_record_set snapshot;
    struct sockaddr_in dest;
    struct service_record_set parsed;
    struct service_type_set types;
    int saw_airport = 0;
    int saw_device_info = 0;
    int saw_printer = 0;
    int saw_smb = 0;
    int saw_adisk = 0;
    int saw_afp = 0;
    int total_a = 0;
    size_t i;

    memset(&cfg, 0, sizeof(cfg));
    snprintf(cfg.instance_name, sizeof(cfg.instance_name), "%s", "Alton Time Capsule");
    snprintf(cfg.host_label, sizeof(cfg.host_label), "%s", "alton-time-capsule");
    snprintf(cfg.host_fqdn, sizeof(cfg.host_fqdn), "%s", "alton-time-capsule.local.");
    snprintf(cfg.service_type, sizeof(cfg.service_type), "%s", "_smb._tcp.local.");
    snprintf(cfg.adisk_service_type, sizeof(cfg.adisk_service_type), "%s", "_adisk._tcp.local.");
    snprintf(cfg.device_info_service_type, sizeof(cfg.device_info_service_type), "%s", "_device-info._tcp.local.");
    snprintf(cfg.airport_service_type, sizeof(cfg.airport_service_type), "%s", "_airport._tcp.local.");
    snprintf(cfg.device_model, sizeof(cfg.device_model), "%s", "TimeCapsule8,119");
    snprintf(cfg.adisk_sys_wama, sizeof(cfg.adisk_sys_wama), "%s", "80:EA:96:E6:58:68");
    cfg.port = 445;
    cfg.adisk_port = 9;
    cfg.airport_port = 5009;
    cfg.ttl = 120;
    cfg.diskless = 1;
    memset(&response_link, 0, sizeof(response_link));
    snprintf(response_link.name, sizeof(response_link.name), "%s", "bridge0");
    response_link.flags = IFF_UP | IFF_RUNNING;
    response_link.ipv4[0].addr = inet_addr("10.0.1.77");
    response_link.ipv4[0].netmask = inet_addr("255.255.255.0");
    response_link.ipv4_count = 1;
    response_link.mdns_ipv4_transport = 1;
    response_link.mdns_ipv4_transport_addr = response_link.ipv4[0].addr;
    if (add_adisk_disk_config(&cfg, "Data", "dk2", "12345678-1234-1234-1234-123456789012", "0x82") != 0) {
        return 1;
    }

    memset(&snapshot, 0, sizeof(snapshot));
    add_snapshot_record(&snapshot, "_airport._tcp.local.", "Alton Time Capsule", "alton-time-capsule", 5009);
    add_snapshot_record(&snapshot, "_ipp._tcp.local.", "Printer", "printer-host", 631);
    add_snapshot_record(&snapshot, "_smb._tcp.local.", "Stale SMB", "alton-time-capsule", 445);
    add_snapshot_record(&snapshot, "_adisk._tcp.local.", "Stale Disk", "alton-time-capsule", 9);
    add_snapshot_record(&snapshot, "_afpovertcp._tcp.local.", "Stale AFP", "alton-time-capsule", 548);
    add_snapshot_record(&snapshot, "_device-info._tcp.local.", "Snapshot Device", "alton-time-capsule", 0);

    memset(&dest, 0, sizeof(dest));
    dest.sin_family = AF_INET;
    dest.sin_port = htons(5353);
    dest.sin_addr.s_addr = inet_addr("224.0.0.251");

    if (send_announcement(1, &dest, &cfg, &response_link, cfg.ttl, &snapshot, 1) != 0) {
        return 2;
    }
    if (captured_count < 3) {
        return 3;
    }
    for (i = 0; i < captured_count; i++) {
        int count_a;
        memset(&parsed, 0, sizeof(parsed));
        memset(&types, 0, sizeof(types));
        if (parse_snapshot_rrs(captured_packets[i], captured_lengths[i], &parsed, &types) != 0) {
            return 4;
        }
        if (service_type_set_contains(&types, "_airport._tcp.local.")) {
            saw_airport = 1;
        }
        if (service_type_set_contains(&types, "_device-info._tcp.local.")) {
            saw_device_info = 1;
        }
        if (service_type_set_contains(&types, "_ipp._tcp.local.")) {
            saw_printer = 1;
        }
        if (service_type_set_contains(&types, "_smb._tcp.local.")) {
            saw_smb = 1;
        }
        if (service_type_set_contains(&types, "_adisk._tcp.local.")) {
            saw_adisk = 1;
        }
        if (service_type_set_contains(&types, "_afpovertcp._tcp.local.")) {
            saw_afp = 1;
        }
        count_a = count_rr_type(captured_packets[i], captured_lengths[i], DNS_TYPE_A);
        if (count_a < 0) {
            return 5;
        }
        total_a += count_a;
    }
    if (!saw_airport || !saw_device_info || !saw_printer) {
        return 6;
    }
    if (saw_smb || saw_adisk || saw_afp) {
        return 7;
    }
    if (total_a < 1) {
        return 8;
    }
    printf("ok\n");
    return 0;
}
'''.replace("@MDNS_SOURCE@", mdns_source)
        run = self._compile_and_run_c_helper(source, "mdns_diskless_snapshot_records")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout.strip(), "ok")

    def test_nbns_advertiser_retries_interrupted_sendto(self) -> None:
        nbns_source = (REPO_ROOT / "build" / "nbns-advertiser.c").as_posix()
        source = '''
#include <errno.h>
#include <netinet/in.h>
#include <stdio.h>
#include <sys/socket.h>
#include <sys/types.h>

ssize_t fake_sendto(int sockfd, const void *buf, size_t len, int flags,
                    const struct sockaddr *dest, socklen_t dest_len);

#define sendto fake_sendto
#define main nbns_advertiser_main
#include "{nbns_source}"
#undef main
#undef sendto

static int sendto_call_count = 0;

ssize_t fake_sendto(int sockfd, const void *buf, size_t len, int flags,
                    const struct sockaddr *dest, socklen_t dest_len) {{
    (void)sockfd;
    (void)buf;
    (void)flags;
    (void)dest;
    (void)dest_len;

    sendto_call_count++;
    if (sendto_call_count == 1) {{
        errno = EINTR;
        return -1;
    }}
    return (ssize_t)len;
}}

int main(void) {{
    struct sockaddr_in dest;
    unsigned char packet[4] = {{1, 2, 3, 4}};
    ssize_t sent;

    memset(&dest, 0, sizeof(dest));
    sent = sendto_retry(1, packet, sizeof(packet), 0, (const struct sockaddr *)&dest, sizeof(dest));
    if (sent != (ssize_t)sizeof(packet)) {{
        return 1;
    }}
    if (sendto_call_count != 2) {{
        return 2;
    }}
    return 0;
}}
'''.format(nbns_source=nbns_source)
        run = self._compile_and_run_c_helper(source, "nbns_sendto_eintr")
        self.assertEqual(run.returncode, 0, run.stderr)

    def test_nbns_advertiser_rejects_removed_legacy_cli_modes(self) -> None:
        if shutil.which("cc") is None:
            self.skipTest("cc not available")

        with tempfile.TemporaryDirectory() as tmpdir:
            bin_path = self._compile_nbns_advertiser_binary(Path(tmpdir))
            runs = [
                subprocess.run(
                    [str(bin_path), "--name", "TimeCapsule", "--ipv4", "192.168.1.217"],
                    capture_output=True,
                    text=True,
                    check=False,
                ),
                subprocess.run(
                    [str(bin_path), "--name", "TimeCapsule", "--auto-ip", "--ttl", "30"],
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
            self.assertEqual(run.returncode, 2)
            self.assertIn("Usage:", run.stderr)

    def test_nbns_advertiser_version_prints_version_code(self) -> None:
        if shutil.which("cc") is None:
            self.skipTest("cc not available")

        with tempfile.TemporaryDirectory() as tmpdir:
            bin_path = self._compile_nbns_advertiser_binary(Path(tmpdir))
            run = subprocess.run([str(bin_path), "--version"], capture_output=True, text=True, check=False)
        self.assertEqual(run.returncode, 0)
        self.assertEqual(run.stdout, "2104\n")
        self.assertEqual(run.stderr, "")

    def test_nbns_advertiser_usage_reports_auto_ip_only(self) -> None:
        if shutil.which("cc") is None:
            self.skipTest("cc not available")

        with tempfile.TemporaryDirectory() as tmpdir:
            bin_path = self._compile_nbns_advertiser_binary(Path(tmpdir))
            run = subprocess.run([str(bin_path), "--help"], capture_output=True, text=True, check=False)

        self.assertEqual(run.returncode, 0)
        self.assertIn("Usage:", run.stderr)
        self.assertIn("--auto-ip", run.stderr)
        self.assertNotIn("--ipv4", run.stderr)
        self.assertNotIn("--ttl", run.stderr)
        self.assertNotIn("--check-auto-ip", run.stderr)

    def test_nbns_advertiser_builds_rfc_query_and_status_responses(self) -> None:
        nbns_source = (REPO_ROOT / "build" / "nbns-advertiser.c").as_posix()
        source = r'''
#include <arpa/inet.h>
#include <errno.h>
#include <netinet/in.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/types.h>

ssize_t fake_sendto(int sockfd, const void *buf, size_t len, int flags,
                    const struct sockaddr *dest, socklen_t dest_len);

#define sendto fake_sendto
#define main nbns_advertiser_main
#include "@NBNS_SOURCE@"
#undef main
#undef sendto

static uint8_t captured[BUF_SIZE];
static size_t captured_len = 0;
static int sendto_call_count = 0;

ssize_t fake_sendto(int sockfd, const void *buf, size_t len, int flags,
                    const struct sockaddr *dest, socklen_t dest_len) {
    (void)sockfd;
    (void)flags;
    (void)dest;
    (void)dest_len;

    if (len > sizeof(captured)) {
        errno = EMSGSIZE;
        return -1;
    }
    memcpy(captured, buf, len);
    captured_len = len;
    sendto_call_count++;
    return (ssize_t)len;
}

static void reset_capture(void) {
    memset(captured, 0, sizeof(captured));
    captured_len = 0;
    sendto_call_count = 0;
}

static void put_u16(uint8_t *out, uint16_t value) {
    uint16_t net = htons(value);
    memcpy(out, &net, sizeof(net));
}

static uint16_t get_u16(const uint8_t *buf, size_t off) {
    uint16_t value;
    memcpy(&value, buf + off, sizeof(value));
    return ntohs(value);
}

static uint32_t get_u32(const uint8_t *buf, size_t off) {
    uint32_t value;
    memcpy(&value, buf + off, sizeof(value));
    return ntohl(value);
}

static size_t append_query_name(uint8_t *out, const char *name, uint8_t suffix, const char *scope) {
    char raw[16];
    size_t i;
    size_t len;
    size_t off = 0;

    memset(raw, ' ', sizeof(raw));
    len = strlen(name);
    if (len > 15) {
        len = 15;
    }
    for (i = 0; i < len; i++) {
        raw[i] = (char)toupper((unsigned char)name[i]);
    }
    raw[15] = (char)suffix;

    out[off++] = 32;
    for (i = 0; i < 16; i++) {
        unsigned char value = (unsigned char)raw[i];
        out[off++] = (uint8_t)('A' + ((value >> 4) & 0x0f));
        out[off++] = (uint8_t)('A' + (value & 0x0f));
    }

    if (scope != NULL && scope[0] != '\0') {
        const char *cursor = scope;
        while (*cursor != '\0') {
            const char *dot = strchr(cursor, '.');
            size_t label_len = dot == NULL ? strlen(cursor) : (size_t)(dot - cursor);
            out[off++] = (uint8_t)label_len;
            memcpy(out + off, cursor, label_len);
            off += label_len;
            if (dot == NULL) {
                break;
            }
            cursor = dot + 1;
        }
    }

    out[off++] = 0;
    return off;
}

static size_t build_query(uint8_t *out,
                          const char *name,
                          uint8_t suffix,
                          uint16_t qtype,
                          uint16_t flags,
                          const char *scope) {
    size_t off;

    memset(out, 0, 256);
    put_u16(out, 0x1337);
    put_u16(out + 2, flags);
    put_u16(out + 4, 1);
    off = 12;
    off += append_query_name(out + off, name, suffix, scope);
    put_u16(out + off, qtype);
    off += 2;
    put_u16(out + off, DNS_CLASS_IN);
    off += 2;
    return off;
}

static int invoke_query(const uint8_t *query, size_t query_len) {
    struct config cfg;
    struct sockaddr_in peer;

    memset(&cfg, 0, sizeof(cfg));
    memcpy(cfg.netbios_name, "TimeCapsule", sizeof("TimeCapsule"));
    cfg.ipv4_addr = inet_addr("192.168.1.217");
    cfg.ttl = 123;

    memset(&peer, 0, sizeof(peer));
    peer.sin_family = AF_INET;
    peer.sin_port = htons(40000);
    peer.sin_addr.s_addr = inet_addr("192.168.1.50");

    return maybe_respond_to_query_addr(
        1,
        &cfg,
        query,
        query_len,
        (const struct sockaddr *)(const void *)&peer,
        sizeof(peer));
}

static int expect_positive_nb_response(const uint8_t *query, size_t qname_len) {
    size_t off = 12;
    uint32_t expected_ip = ntohl(inet_addr("192.168.1.217"));

    if (captured_len != 12 + qname_len + 10 + 6) return 10;
    if (get_u16(captured, 0) != 0x1337) return 11;
    if (get_u16(captured, 2) != 0x8480) return 12;
    if (get_u16(captured, 4) != 0) return 13;
    if (get_u16(captured, 6) != 1) return 14;
    if (get_u16(captured, 8) != 0 || get_u16(captured, 10) != 0) return 15;
    if (memcmp(captured + off, query + 12, qname_len) != 0) return 16;
    off += qname_len;
    if (get_u16(captured, off) != NB_TYPE_NB) return 17;
    if (get_u16(captured, off + 2) != DNS_CLASS_IN) return 18;
    if (get_u32(captured, off + 4) != 123) return 19;
    if (get_u16(captured, off + 8) != 6) return 20;
    if (get_u16(captured, off + 10) != 0) return 21;
    if (get_u32(captured, off + 12) != expected_ip) return 22;
    return 0;
}

static int expect_nbstat_response(const uint8_t *query, size_t qname_len) {
    size_t off = 12;
    size_t rdata_off;
    size_t stats_off;
    size_t i;

    if (captured_len != 12 + qname_len + 10 + 83) return 30;
    if (get_u16(captured, 0) != 0x1337) return 31;
    if (get_u16(captured, 2) != 0x8400) return 32;
    if (get_u16(captured, 4) != 0 || get_u16(captured, 6) != 1) return 33;
    if (get_u16(captured, 8) != 0 || get_u16(captured, 10) != 0) return 34;
    if (memcmp(captured + off, query + 12, qname_len) != 0) return 35;
    off += qname_len;
    if (get_u16(captured, off) != NB_TYPE_NBSTAT) return 36;
    if (get_u16(captured, off + 2) != DNS_CLASS_IN) return 37;
    if (get_u32(captured, off + 4) != 0) return 38;
    if (get_u16(captured, off + 8) != 83) return 39;
    rdata_off = off + 10;
    if (captured[rdata_off] != 2) return 40;
    if (memcmp(captured + rdata_off + 1, "TIMECAPSULE    ", 15) != 0) return 41;
    if (captured[rdata_off + 16] != NBNS_SUFFIX_WORKSTATION) return 42;
    if (get_u16(captured, rdata_off + 17) != NBNS_NAME_FLAGS_ACTIVE) return 43;
    if (memcmp(captured + rdata_off + 19, "TIMECAPSULE    ", 15) != 0) return 44;
    if (captured[rdata_off + 34] != NBNS_SUFFIX_SERVER) return 45;
    if (get_u16(captured, rdata_off + 35) != NBNS_NAME_FLAGS_ACTIVE) return 46;
    stats_off = rdata_off + 1 + (NBNS_NODE_STATUS_NAME_COUNT * 18);
    for (i = 0; i < NBNS_NODE_STATUS_STATS_LEN; i++) {
        if (captured[stats_off + i] != 0) return 47;
    }
    return 0;
}

int main(void) {
    uint8_t query[256];
    size_t query_len;
    size_t qname_len;
    int rc;

    query_len = build_query(query, "TimeCapsule", NBNS_SUFFIX_SERVER, NB_TYPE_NB, 0x0110, NULL);
    qname_len = query_len - 12 - 4;
    reset_capture();
    if (invoke_query(query, query_len) != 1 || sendto_call_count != 1) return 1;
    rc = expect_positive_nb_response(query, qname_len);
    if (rc != 0) return rc;

    query_len = build_query(query, "TimeCapsule", NBNS_SUFFIX_WORKSTATION, NB_TYPE_NB, 0, "office.local");
    qname_len = query_len - 12 - 4;
    reset_capture();
    if (invoke_query(query, query_len) != 1 || sendto_call_count != 1) return 2;
    rc = expect_positive_nb_response(query, qname_len);
    if (rc != 0) return 100 + rc;

    query_len = build_query(query, "*", NBNS_SUFFIX_WORKSTATION, NB_TYPE_NBSTAT, 0, NULL);
    qname_len = query_len - 12 - 4;
    reset_capture();
    if (invoke_query(query, query_len) != 1 || sendto_call_count != 1) return 3;
    rc = expect_nbstat_response(query, qname_len);
    if (rc != 0) return 200 + rc;

    query_len = build_query(query, "*", NBNS_SUFFIX_WORKSTATION, NB_TYPE_NBSTAT, 0, "office.local");
    qname_len = query_len - 12 - 4;
    reset_capture();
    if (invoke_query(query, query_len) != 1 || sendto_call_count != 1) return 4;
    rc = expect_nbstat_response(query, qname_len);
    if (rc != 0) return 300 + rc;

    return 0;
}
'''.replace("@NBNS_SOURCE@", nbns_source)
        run = self._compile_and_run_c_helper(source, "nbns_response_packets")
        self.assertEqual(run.returncode, 0, run.stderr)

    def test_nbns_advertiser_handles_query_edge_cases(self) -> None:
        nbns_source = (REPO_ROOT / "build" / "nbns-advertiser.c").as_posix()
        source = r'''
#include <arpa/inet.h>
#include <errno.h>
#include <netinet/in.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/types.h>

ssize_t fake_sendto(int sockfd, const void *buf, size_t len, int flags,
                    const struct sockaddr *dest, socklen_t dest_len);

#define sendto fake_sendto
#define main nbns_advertiser_main
#include "@NBNS_SOURCE@"
#undef main
#undef sendto

static uint8_t captured[BUF_SIZE];
static size_t captured_len = 0;
static int sendto_call_count = 0;

ssize_t fake_sendto(int sockfd, const void *buf, size_t len, int flags,
                    const struct sockaddr *dest, socklen_t dest_len) {
    (void)sockfd;
    (void)flags;
    (void)dest;
    (void)dest_len;

    if (len > sizeof(captured)) {
        errno = EMSGSIZE;
        return -1;
    }
    memcpy(captured, buf, len);
    captured_len = len;
    sendto_call_count++;
    return (ssize_t)len;
}

static void reset_capture(void) {
    memset(captured, 0, sizeof(captured));
    captured_len = 0;
    sendto_call_count = 0;
}

static void put_u16(uint8_t *out, uint16_t value) {
    uint16_t net = htons(value);
    memcpy(out, &net, sizeof(net));
}

static uint16_t get_u16(const uint8_t *buf, size_t off) {
    uint16_t value;
    memcpy(&value, buf + off, sizeof(value));
    return ntohs(value);
}

static size_t append_query_name(uint8_t *out, const char *name, uint8_t suffix) {
    char raw[16];
    size_t i;
    size_t len;
    size_t off = 0;

    memset(raw, ' ', sizeof(raw));
    len = strlen(name);
    if (len > 15) {
        len = 15;
    }
    for (i = 0; i < len; i++) {
        raw[i] = (char)toupper((unsigned char)name[i]);
    }
    raw[15] = (char)suffix;

    out[off++] = 32;
    for (i = 0; i < 16; i++) {
        unsigned char value = (unsigned char)raw[i];
        out[off++] = (uint8_t)('A' + ((value >> 4) & 0x0f));
        out[off++] = (uint8_t)('A' + (value & 0x0f));
    }
    out[off++] = 0;
    return off;
}

static size_t build_query(uint8_t *out,
                          const char *name,
                          uint8_t suffix,
                          uint16_t qtype,
                          uint16_t flags) {
    size_t off;

    memset(out, 0, 256);
    put_u16(out, 0x1337);
    put_u16(out + 2, flags);
    put_u16(out + 4, 1);
    off = 12;
    off += append_query_name(out + off, name, suffix);
    put_u16(out + off, qtype);
    off += 2;
    put_u16(out + off, DNS_CLASS_IN);
    off += 2;
    return off;
}

static int invoke_query(const uint8_t *query, size_t query_len) {
    struct config cfg;
    struct sockaddr_in peer;

    memset(&cfg, 0, sizeof(cfg));
    memcpy(cfg.netbios_name, "TimeCapsule", sizeof("TimeCapsule"));
    cfg.ipv4_addr = inet_addr("192.168.1.217");
    cfg.ttl = 300;

    memset(&peer, 0, sizeof(peer));
    peer.sin_family = AF_INET;
    peer.sin_port = htons(40000);
    peer.sin_addr.s_addr = inet_addr("192.168.1.50");

    return maybe_respond_to_query_addr(
        1,
        &cfg,
        query,
        query_len,
        (const struct sockaddr *)(const void *)&peer,
        sizeof(peer));
}

static int expect_no_response(const uint8_t *query, size_t query_len) {
    reset_capture();
    if (invoke_query(query, query_len) != 0) return 1;
    if (sendto_call_count != 0 || captured_len != 0) return 2;
    return 0;
}

static int expect_negative_response(const uint8_t *query, size_t query_len) {
    size_t qname_len = query_len - 12 - 4;
    size_t off = 12 + qname_len;

    reset_capture();
    if (invoke_query(query, query_len) != 1) return 10;
    if (sendto_call_count != 1) return 11;
    if (captured_len != 12 + qname_len + 10) return 12;
    if (get_u16(captured, 2) != 0x8483) return 13;
    if (get_u16(captured, 4) != 0 || get_u16(captured, 6) != 0) return 14;
    if (memcmp(captured + 12, query + 12, qname_len) != 0) return 15;
    if (get_u16(captured, off) != NB_TYPE_NULL) return 16;
    if (get_u16(captured, off + 2) != DNS_CLASS_IN) return 17;
    if (get_u16(captured, off + 8) != 0) return 18;
    return 0;
}

int main(void) {
    uint8_t query[256];
    size_t query_len;
    int rc;

    query_len = build_query(query, "OtherName", NBNS_SUFFIX_SERVER, NB_TYPE_NB, NBNS_FLAG_BROADCAST);
    rc = expect_no_response(query, query_len);
    if (rc != 0) return rc;

    query_len = build_query(query, "OtherName", NBNS_SUFFIX_SERVER, NB_TYPE_NB, 0);
    rc = expect_negative_response(query, query_len);
    if (rc != 0) return 20 + rc;

    query_len = build_query(query, "TimeCapsule", 0x03, NB_TYPE_NB, 0);
    rc = expect_negative_response(query, query_len);
    if (rc != 0) return 50 + rc;

    query_len = build_query(query, "TimeCapsule", NBNS_SUFFIX_SERVER, 0x0001, 0);
    rc = expect_no_response(query, query_len);
    if (rc != 0) return 80 + rc;

    query_len = build_query(query, "OtherName", NBNS_SUFFIX_SERVER, NB_TYPE_NBSTAT, 0);
    rc = expect_no_response(query, query_len);
    if (rc != 0) return 90 + rc;

    query_len = build_query(query, "TimeCapsule", NBNS_SUFFIX_SERVER, NB_TYPE_NB, 0);
    query[45] = 0xc0;
    query[46] = 0x0c;
    rc = expect_no_response(query, query_len);
    if (rc != 0) return 100 + rc;

    return 0;
}
'''.replace("@NBNS_SOURCE@", nbns_source)
        run = self._compile_and_run_c_helper(source, "nbns_query_edge_cases")
        self.assertEqual(run.returncode, 0, run.stderr)

    def test_nbns_auto_ip_helpers_filter_and_choose_subnet_response(self) -> None:
        nbns_source = (REPO_ROOT / "build" / "nbns-advertiser.c").as_posix()
        source = '''
#include <arpa/inet.h>
#include <string.h>
#define main nbns_advertiser_main
#include "{nbns_source}"
#undef main

int main(void) {{
    struct link_context_set links;
    struct link_context_set single_link;
    struct link_context_set v6_only_links;
    struct link_context_set links_a;
    struct link_context_set links_b;
    struct in6_addr v6_addr;

    if (AUTO_IP_STARTUP_POLL_SECONDS != 2 || AUTO_IP_STABLE_POLL_SECONDS != 30) {{
        return 10;
    }}

    if (runtime_ipv4_is_usable(inet_addr("0.1.2.3")) ||
        runtime_ipv4_is_usable(inet_addr("127.0.0.1")) ||
        runtime_ipv4_is_usable(inet_addr("169.254.1.9")) ||
        runtime_ipv4_is_usable(inet_addr("224.0.0.1")) ||
        runtime_ipv4_is_usable(inet_addr("240.0.0.1")) ||
        runtime_ipv4_is_usable(inet_addr("255.255.255.255")) ||
        runtime_ipv4_is_usable(0) ||
        !runtime_ipv4_is_usable(inet_addr("10.0.1.1"))) {{
        return 1;
    }}
    if (!iface_flags_are_usable(IFF_UP | IFF_RUNNING, 1) ||
        iface_flags_are_usable(IFF_UP, 1) ||
        !iface_flags_are_usable(IFF_UP, 0) ||
        iface_flags_are_usable(IFF_UP | IFF_LOOPBACK, 0) ||
        iface_flags_are_usable(IFF_RUNNING, 0)) {{
        return 2;
    }}

    memset(&links, 0, sizeof(links));
    append_link_ipv4(&links, "bridge0", inet_addr("10.0.1.1"), inet_addr("255.255.255.0"), IFF_UP | IFF_RUNNING);
    append_link_ipv4(&links, "bcmeth0", inet_addr("192.168.50.2"), inet_addr("255.255.255.0"), IFF_UP | IFF_RUNNING);
    if (choose_response_ipv4_from_links(&links, inet_addr("192.168.50.99")) != inet_addr("192.168.50.2")) {{
        return 3;
    }}
    if (choose_response_ipv4_from_links(&links, inet_addr("172.16.1.5")) != 0) {{
        return 4;
    }}

    memset(&links, 0, sizeof(links));
    append_link_ipv4(&links, "bridge0", inet_addr("10.0.1.1"), 0, IFF_UP | IFF_RUNNING);
    append_link_ipv4(&links, "bcmeth0", inet_addr("192.168.1.217"), 0, IFF_UP | IFF_RUNNING);
    if (choose_response_ipv4_from_links(&links, inet_addr("10.0.1.3")) != inet_addr("10.0.1.1")) {{
        return 14;
    }}
    if (choose_response_ipv4_from_links(&links, inet_addr("10.44.55.66")) != inet_addr("10.0.1.1")) {{
        return 17;
    }}
    if (choose_response_ipv4_from_links(&links, inet_addr("192.168.1.40")) != inet_addr("192.168.1.217")) {{
        return 15;
    }}
    if (choose_response_ipv4_from_links(&links, inet_addr("172.16.1.5")) != 0) {{
        return 16;
    }}

    memset(&single_link, 0, sizeof(single_link));
    append_link_ipv4(&single_link, "bridge0", inet_addr("10.0.1.1"), inet_addr("255.255.255.0"), IFF_UP | IFF_RUNNING);
    if (choose_response_ipv4_from_links(&single_link, inet_addr("172.16.1.5")) != inet_addr("10.0.1.1")) {{
        return 13;
    }}

    memset(&links, 0, sizeof(links));
    append_link_ipv4(&links, "bridge0", inet_addr("10.0.1.1"), 0, IFF_UP | IFF_RUNNING);
    if (inet_pton(AF_INET6, "fd00::1", &v6_addr) != 1) {{
        return 20;
    }}
    append_link_ipv6(&links, "bridge0", &v6_addr, 64, 0, IFF_UP | IFF_RUNNING);
    keep_only_nbns_ipv4_link_contexts(&links);
    if (!link_contexts_need_nbns_ipv4_socket(&links)) {{
        return 21;
    }}
    if (links.count != 1 || links.links[0].ipv6_count != 0 || links.links[0].mdns_ipv6_transport != 0) {{
        return 22;
    }}
    if (choose_response_ipv4_from_links(&links, inet_addr("172.16.1.5")) != inet_addr("10.0.1.1")) {{
        return 23;
    }}

    memset(&v6_only_links, 0, sizeof(v6_only_links));
    append_link_ipv6(&v6_only_links, "bridge0", &v6_addr, 64, 0, IFF_UP | IFF_RUNNING);
    keep_only_nbns_ipv4_link_contexts(&v6_only_links);
    if (v6_only_links.count != 0) {{
        return 24;
    }}
    if (link_contexts_need_nbns_ipv4_socket(&v6_only_links)) {{
        return 25;
    }}

    memset(&links_a, 0, sizeof(links_a));
    memset(&links_b, 0, sizeof(links_b));
    append_link_ipv4(&links_a, "bridge0", inet_addr("10.0.1.1"), inet_addr("255.255.255.0"), IFF_UP | IFF_RUNNING);
    append_link_ipv6(&links_a, "bridge0", &v6_addr, 64, 0, IFF_UP | IFF_RUNNING);
    append_link_ipv6(&links_b, "bridge0", &v6_addr, 64, 0, IFF_UP | IFF_RUNNING);
    append_link_ipv4(&links_b, "bridge0", inet_addr("10.0.1.1"), inet_addr("255.255.255.0"), IFF_UP | IFF_RUNNING);
    if (!link_context_sets_equal(&links_a, &links_b)) {{
        return 27;
    }}
    links_b.links[0].ipv6[0].prefix_len = 48;
    if (link_context_sets_equal(&links_a, &links_b)) {{
        return 28;
    }}
    return 0;
}}
'''.format(nbns_source=nbns_source)
        run = self._compile_and_run_c_helper(source, "nbns_auto_ip_helpers")
        self.assertEqual(run.returncode, 0, run.stderr)

    def test_nbns_advertiser_rejects_overlong_name_before_truncation(self) -> None:
        if shutil.which("cc") is None:
            self.skipTest("cc not available")

        with tempfile.TemporaryDirectory() as tmpdir:
            bin_path = self._compile_nbns_advertiser_binary(Path(tmpdir))
            run = subprocess.run(
                [str(bin_path), "--name", "ABCDEFGHIJKLMNOP", "--auto-ip"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(run.returncode, 2)
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

    def test_upload_deployment_payload_uploads_all_expected_files(self) -> None:
        paths = self._payload_home("/Volumes/dk2", "samba4")
        plan = build_deployment_plan("host", paths, Path("bin/smbd"), Path("bin/mdns"), Path("bin/nbns"))
        connection = SshConnection("host", "pw", "-o foo")
        source_resolver = {
            BINARY_SMBD_SOURCE: Path("/tmp/smbd"),
            BINARY_MDNS_SOURCE: Path("/tmp/mdns-advertiser"),
            BINARY_NBNS_SOURCE: Path("/tmp/nbns-advertiser"),
            GENERATED_SMBPASSWD_SOURCE: Path("/tmp/smbpasswd"),
            GENERATED_USERNAME_MAP_SOURCE: Path("/tmp/username.map"),
            GENERATED_FLASH_CONFIG_SOURCE: Path("/tmp/tcapsulesmb.conf"),
            PACKAGED_RC_LOCAL_SOURCE: Path("/tmp/rc.local"),
            PACKAGED_COMMON_SH_SOURCE: Path("/tmp/common.sh"),
            PACKAGED_BOOT_SOURCE: Path("/tmp/boot.sh"),
            PACKAGED_MANAGER_SOURCE: Path("/tmp/manager.sh"),
            PACKAGED_DFREE_SH_SOURCE: Path("/tmp/dfree.sh"),
        }
        with mock.patch("timecapsulesmb.deploy.executor.run_scp") as scp_mock:
            with mock.patch("timecapsulesmb.deploy.executor.run_ssh") as ssh_mock:
                with mock.patch("timecapsulesmb.deploy.executor.ensure_volume_root_mounted_conn", return_value=True) as mount_mock:
                    uploading = []
                    uploaded = []
                    upload_deployment_payload(
                        plan,
                        connection=connection,
                        source_resolver=source_resolver,
                        on_uploading=uploading.append,
                        on_uploaded=uploaded.append,
                    )
        self.assertEqual(scp_mock.call_count, 12)
        self.assertEqual(mount_mock.call_count, 5)
        self.assertTrue(all(call.args[:3] == (connection, "/Volumes/dk2", "/dev/dk2") for call in mount_mock.call_args_list))
        self.assertTrue(all(call.kwargs == {"wait_seconds": DEFAULT_APPLE_MOUNT_WAIT_SECONDS} for call in mount_mock.call_args_list))
        sources = [call.args[1] for call in scp_mock.call_args_list]
        self.assertEqual(
            sources,
            [
                Path("/tmp/smbd"),
                Path("/tmp/mdns-advertiser"),
                Path("/tmp/mdns-advertiser"),
                Path("/tmp/nbns-advertiser"),
                Path("/tmp/rc.local"),
                Path("/tmp/common.sh"),
                Path("/tmp/boot.sh"),
                Path("/tmp/manager.sh"),
                Path("/tmp/dfree.sh"),
                Path("/tmp/tcapsulesmb.conf"),
                Path("/tmp/smbpasswd"),
                Path("/tmp/username.map"),
            ],
        )
        destinations = [call.args[2] for call in scp_mock.call_args_list]
        self.assertEqual(
            destinations,
            [
                "/Volumes/dk2/samba4/smbd",
                "/Volumes/dk2/samba4/mdns-advertiser",
                "/mnt/Flash/.mdns-advertiser.tmp",
                "/Volumes/dk2/samba4/nbns-advertiser",
                "/mnt/Flash/.rc.local.tmp",
                "/mnt/Flash/.common.sh.tmp",
                "/mnt/Flash/.boot.sh.tmp",
                "/mnt/Flash/.manager.sh.tmp",
                "/mnt/Flash/.dfree.sh.tmp",
                "/mnt/Flash/.tcapsulesmb.conf.tmp",
                "/Volumes/dk2/samba4/private/smbpasswd",
                "/Volumes/dk2/samba4/private/username.map",
            ],
        )
        binary_upload_timeouts = [call.kwargs.get("timeout") for call in scp_mock.call_args_list[:4]]
        self.assertEqual(binary_upload_timeouts, [PAYLOAD_BINARY_UPLOAD_TIMEOUT_SECONDS] * 4)
        text_upload_timeouts = [call.kwargs.get("timeout") for call in scp_mock.call_args_list[4:]]
        self.assertEqual(text_upload_timeouts, [FLASH_TEXT_UPLOAD_TIMEOUT_SECONDS] * 6 + [None] * 2)
        self.assertEqual(ssh_mock.call_count, 14)
        self.assertEqual(uploading, plan.uploads)
        self.assertEqual(uploaded, plan.uploads)

    def test_upload_deployment_payload_consumes_plan_uploads_directly(self) -> None:
        paths = self._payload_home("/Volumes/dk2", "samba4")
        plan = build_deployment_plan("host", paths, Path("bin/smbd"), Path("bin/mdns"), Path("bin/nbns"))
        custom_plan = replace(plan, uploads=[plan.uploads[8], plan.uploads[9]])
        connection = SshConnection("host", "pw", "-o foo")
        source_resolver = {
            PACKAGED_DFREE_SH_SOURCE: Path("/tmp/dfree.sh"),
            GENERATED_FLASH_CONFIG_SOURCE: Path("/tmp/tcapsulesmb.conf"),
        }
        with mock.patch("timecapsulesmb.deploy.executor.run_scp") as scp_mock:
            with mock.patch("timecapsulesmb.deploy.executor.run_ssh") as ssh_mock:
                with mock.patch("timecapsulesmb.deploy.executor.ensure_volume_root_mounted_conn") as mount_mock:
                    upload_deployment_payload(custom_plan, connection=connection, source_resolver=source_resolver)

        self.assertEqual([call.args[1] for call in scp_mock.call_args_list], [Path("/tmp/dfree.sh"), Path("/tmp/tcapsulesmb.conf")])
        self.assertEqual([call.args[2] for call in scp_mock.call_args_list], ["/mnt/Flash/.dfree.sh.tmp", "/mnt/Flash/.tcapsulesmb.conf.tmp"])
        self.assertEqual(ssh_mock.call_count, 4)
        mount_mock.assert_not_called()

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
            flush_remote_writes=mock.Mock(),
            verify_payload_home=mock.Mock(return_value=PayloadVerificationResult(True, "ok")),
        )

        upload_measurements = [fields for kind, fields in measurements if kind == "upload"]
        batch_measurements = [fields for kind, fields in measurements if kind == "upload_batch"]
        self.assertEqual([fields["source_id"] for fields in upload_measurements], [BINARY_SMBD_SOURCE, BINARY_MDNS_SOURCE])
        self.assertEqual(upload_measurements[0]["destination_kind"], "payload")
        self.assertEqual(upload_measurements[0]["result"], "success")
        self.assertEqual(batch_measurements[0]["file_count"], len(prepared_plan.plan.uploads))
        self.assertEqual(batch_measurements[0]["result"], "success")

    def test_upload_deployment_payload_stops_when_payload_volume_guard_fails(self) -> None:
        paths = self._payload_home("/Volumes/dk2", "samba4")
        plan = build_deployment_plan("host", paths, Path("bin/smbd"), Path("bin/mdns"), Path("bin/nbns"))
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
        plan = build_deployment_plan("host", paths, Path("bin/smbd"), Path("bin/mdns"), Path("bin/nbns"))
        connection = SshConnection("host", "pw", "-o foo")
        with self.assertRaisesRegex(KeyError, "No local source for planned transfer 'binary:smbd'"):
            upload_deployment_payload(plan, connection=connection, source_resolver={})

    def test_upload_flash_file_uploads_tmp_then_installs_with_rename_and_cleanup(self) -> None:
        connection = SshConnection("host", "pw", "-o foo")
        with mock.patch("timecapsulesmb.deploy.executor.run_scp") as scp_mock:
            with mock.patch("timecapsulesmb.deploy.executor.run_ssh") as ssh_mock:
                upload_flash_file(connection, Path("/tmp/mdns-advertiser"), "/mnt/Flash/mdns-advertiser", timeout=180)

        scp_mock.assert_called_once_with(connection, Path("/tmp/mdns-advertiser"), "/mnt/Flash/.mdns-advertiser.tmp", timeout=180)
        ssh_commands = [call.args[1] for call in ssh_mock.call_args_list]
        self.assertEqual(len(ssh_commands), 2)
        self.assertIn("rm -f /mnt/Flash/.mdns-advertiser.tmp", ssh_commands[0])
        self.assertIn("chmod 755 /mnt/Flash/.mdns-advertiser.tmp", ssh_commands[1])
        self.assertIn("mv -f /mnt/Flash/.mdns-advertiser.tmp /mnt/Flash/mdns-advertiser", ssh_commands[1])
        self.assertIn("rm -f /mnt/Flash/.mdns-advertiser.tmp", ssh_commands[1])

    def test_render_managed_runtime_verification_passes_when_runtime_probe_succeeds(self) -> None:
        verification = ManagedRuntimeProbeResult(
            ready=True,
            detail="managed runtime is ready",
            smbd=readiness_result(True, "managed smbd ready", ("PASS:managed smbd ready",)),
            mdns=readiness_result(True, "managed mDNS takeover active", ("PASS:managed mDNS takeover active",)),
        )

        self.assertTrue(verification.ready)
        self.assertEqual(
            render_managed_runtime_verification(verification, heading="NetBSD4 activation verification:"),
            [
                "NetBSD4 activation verification:",
                "  ok: managed smbd ready",
                "  ok: managed mDNS takeover active",
            ],
        )

    def test_render_managed_runtime_verification_fails_when_runtime_probe_fails(self) -> None:
        verification = ManagedRuntimeProbeResult(
            ready=False,
            detail="managed runtime is not ready",
            smbd=readiness_result(False, "managed smbd is not ready", ("FAIL:managed smbd is not ready",)),
            mdns=readiness_result(True, "managed mDNS takeover active", ("PASS:managed mDNS takeover active",)),
        )

        self.assertFalse(verification.ready)
        self.assertEqual(
            render_managed_runtime_verification(verification, heading="NetBSD4 activation verification:"),
            [
                "NetBSD4 activation verification:",
                "  failed: managed smbd is not ready",
                "  ok: managed mDNS takeover active",
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
        self.assertNotIn("nbns-advertiser", remote_command)

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
zombie_mdns="200 1 Z 0:00.00 mdns-advertiser /mnt/Flash/mdns-advertiser"
live_mdns="201 1 S 0:00.00 mdns-advertiser /mnt/Flash/mdns-advertiser"
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
            (ram_root / "sbin" / "smbd").write_text("smbd")
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

    def test_mdns_status_helper_reports_missing_binary_instead_of_network_defer(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            missing_mdns = Path(tmpdir) / "missing-mdns-advertiser"
            script = f"""
RUNTIME_MDNS_BIN={shlex.quote(str(missing_mdns))}
{SMBD_STATUS_HELPERS}
ps_out=''
fstat_out=''
if describe_managed_mdns_status "$ps_out" "$fstat_out"; then
    echo status=0
else
    echo status=$?
fi
"""

            result = subprocess.run(["/bin/sh", "-c", script], check=False, text=True, capture_output=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"FAIL:mdns-advertiser binary missing at {missing_mdns}", result.stdout)
        self.assertIn("FAIL:mdns-advertiser process is not running", result.stdout)
        self.assertIn("FAIL:mdns-advertiser is not bound to required UDP 5353 listener", result.stdout)
        self.assertIn("PASS:Apple mDNSResponder is stopped", result.stdout)
        self.assertIn("status=1", result.stdout)
        self.assertNotIn("mDNS startup deferred; no usable address has appeared yet", result.stdout)

    def test_mdns_status_helper_requires_auto_ip_when_process_is_bound(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            mdns_bin = Path(tmpdir) / "mdns-advertiser"
            mdns_bin.write_text("#!/bin/sh\nexit 11\n")
            mdns_bin.chmod(0o755)
            ps_out = "201 1 S 0:00.00 mdns-advertiser /mnt/Flash/mdns-advertiser"
            fstat_out = "root mdns-advertiser 201 10 internet dgram udp 0x0 *:5353"
            script = f"""
RUNTIME_MDNS_BIN={shlex.quote(str(mdns_bin))}
{SMBD_STATUS_HELPERS}
ps_out={shlex.quote(ps_out)}
fstat_out={shlex.quote(fstat_out)}
if describe_managed_mdns_status "$ps_out" "$fstat_out"; then
    echo status=0
else
    echo status=$?
fi
"""

            result = subprocess.run(["/bin/sh", "-c", script], check=False, text=True, capture_output=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PASS:mdns-advertiser process is running", result.stdout)
        self.assertIn("FAIL:mdns-advertiser is waiting for a usable address", result.stdout)
        self.assertIn("status=1", result.stdout)
        self.assertNotIn("PASS:mdns-advertiser bind address active", result.stdout)

    def test_mdns_status_helper_reports_unexpected_auto_ip_check_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            mdns_bin = Path(tmpdir) / "mdns-advertiser"
            mdns_bin.write_text("#!/bin/sh\nexit 3\n")
            mdns_bin.chmod(0o755)
            ps_out = "201 1 S 0:00.00 mdns-advertiser /mnt/Flash/mdns-advertiser"
            fstat_out = "root mdns-advertiser 201 10 internet dgram udp 0x0 *:5353"
            script = f"""
RUNTIME_MDNS_BIN={shlex.quote(str(mdns_bin))}
{SMBD_STATUS_HELPERS}
ps_out={shlex.quote(ps_out)}
fstat_out={shlex.quote(fstat_out)}
if describe_managed_mdns_status "$ps_out" "$fstat_out"; then
    echo status=0
else
    echo status=$?
fi
"""

            result = subprocess.run(["/bin/sh", "-c", script], check=False, text=True, capture_output=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("FAIL:mdns-advertiser mDNS socket family probe failed with exit code 3", result.stdout)
        self.assertIn("PASS:mdns-advertiser process is running", result.stdout)
        self.assertIn("FAIL:mdns-advertiser is not bound to required UDP 5353 listener", result.stdout)
        self.assertIn("status=1", result.stdout)

    def test_mdns_status_helper_passes_only_when_bound_and_auto_ip_active(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            mdns_bin = Path(tmpdir) / "mdns-advertiser"
            mdns_bin.write_text("#!/bin/sh\necho ipv4\n")
            mdns_bin.chmod(0o755)
            ps_out = "201 1 S 0:00.00 mdns-advertiser /mnt/Flash/mdns-advertiser"
            fstat_out = "root mdns-advertiser 201 10 internet dgram udp 0x0 *:5353"
            script = f"""
RUNTIME_MDNS_BIN={shlex.quote(str(mdns_bin))}
{SMBD_STATUS_HELPERS}
ps_out={shlex.quote(ps_out)}
fstat_out={shlex.quote(fstat_out)}
if describe_managed_mdns_status "$ps_out" "$fstat_out"; then
    echo status=0
else
    echo status=$?
fi
"""

            result = subprocess.run(["/bin/sh", "-c", script], check=False, text=True, capture_output=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PASS:mdns-advertiser process is running", result.stdout)
        self.assertIn("PASS:mdns-advertiser bound to required UDP 5353 listeners", result.stdout)
        self.assertIn("PASS:mdns-advertiser bind address active", result.stdout)
        self.assertIn("PASS:Apple mDNSResponder is stopped", result.stdout)
        self.assertIn("status=0", result.stdout)

    def test_mdns_status_helper_requires_both_udp_5353_listeners_when_advertiser_is_dual_stack(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            mdns_bin = Path(tmpdir) / "mdns-advertiser"
            mdns_bin.write_text("#!/bin/sh\necho 'ipv4 ipv6'\n")
            mdns_bin.chmod(0o755)
            ps_out = "201 1 S 0:00.00 mdns-advertiser /mnt/Flash/mdns-advertiser"
            fstat_out = "\n".join(
                [
                    "root mdns-advertiser 201 10 internet dgram udp 0x0 *:5353",
                    "root mdns-advertiser 201 11 internet6 dgram udp 0x0 [*]:5353",
                ]
            )
            script = f"""
RUNTIME_MDNS_BIN={shlex.quote(str(mdns_bin))}
{SMBD_STATUS_HELPERS}
ps_out={shlex.quote(ps_out)}
fstat_out={shlex.quote(fstat_out)}
if describe_managed_mdns_status "$ps_out" "$fstat_out"; then
    echo status=0
else
    echo status=$?
fi
"""

            result = subprocess.run(["/bin/sh", "-c", script], check=False, text=True, capture_output=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PASS:mdns-advertiser process is running", result.stdout)
        self.assertIn("PASS:mdns-advertiser bound to required UDP 5353 listeners", result.stdout)
        self.assertIn("status=0", result.stdout)

    def test_mdns_status_helper_accepts_ipv6_udp_5353_when_advertiser_is_ipv6_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            mdns_bin = Path(tmpdir) / "mdns-advertiser"
            mdns_bin.write_text("#!/bin/sh\necho ipv6\n")
            mdns_bin.chmod(0o755)
            ps_out = "201 1 S 0:00.00 mdns-advertiser /mnt/Flash/mdns-advertiser"
            fstat_out = "root mdns-advertiser 201 10 internet6 dgram udp 0x0 [*]:5353"
            script = f"""
RUNTIME_MDNS_BIN={shlex.quote(str(mdns_bin))}
{SMBD_STATUS_HELPERS}
ps_out={shlex.quote(ps_out)}
fstat_out={shlex.quote(fstat_out)}
if describe_managed_mdns_status "$ps_out" "$fstat_out"; then
    echo status=0
else
    echo status=$?
fi
"""

            result = subprocess.run(["/bin/sh", "-c", script], check=False, text=True, capture_output=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PASS:mdns-advertiser process is running", result.stdout)
        self.assertIn("PASS:mdns-advertiser bound to required UDP 5353 listeners", result.stdout)
        self.assertIn("PASS:mdns-advertiser bind address active", result.stdout)
        self.assertIn("status=0", result.stdout)

    def test_mdns_status_helper_rejects_ipv4_udp_5353_when_advertiser_is_ipv6_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            mdns_bin = Path(tmpdir) / "mdns-advertiser"
            mdns_bin.write_text("#!/bin/sh\necho ipv6\n")
            mdns_bin.chmod(0o755)
            ps_out = "201 1 S 0:00.00 mdns-advertiser /mnt/Flash/mdns-advertiser"
            fstat_out = "root mdns-advertiser 201 10 internet dgram udp 0x0 *:5353"
            script = f"""
RUNTIME_MDNS_BIN={shlex.quote(str(mdns_bin))}
{SMBD_STATUS_HELPERS}
ps_out={shlex.quote(ps_out)}
fstat_out={shlex.quote(fstat_out)}
if describe_managed_mdns_status "$ps_out" "$fstat_out"; then
    echo status=0
else
    echo status=$?
fi
"""

            result = subprocess.run(["/bin/sh", "-c", script], check=False, text=True, capture_output=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PASS:mdns-advertiser process is running", result.stdout)
        self.assertIn("FAIL:mdns-advertiser is not bound to required UDP 5353 listener", result.stdout)
        self.assertIn("status=1", result.stdout)

    def test_probe_managed_smbd_reports_runtime_invariant_failures(self) -> None:
        stdout = "\n".join(
            [
                "FAIL:managed runtime smbd binary missing",
                "FAIL:active smb.conf passdb backend is not staged in RAM",
                "FAIL:one or more managed share volumes are not mounted",
            ]
        )
        with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=mock.Mock(returncode=1, stdout=stdout)):
            result = probe_managed_smbd_conn(SshConnection("host", "pw", "-o foo"), timeout_seconds=12)

        self.assertFalse(result.ready)
        self.assertEqual(
            result.detail,
            "managed runtime smbd binary missing; active smb.conf passdb backend is not staged in RAM; one or more managed share volumes are not mounted",
        )
        self.assertEqual(
            result.lines,
            (
                "FAIL:managed runtime smbd binary missing",
                "FAIL:active smb.conf passdb backend is not staged in RAM",
                "FAIL:one or more managed share volumes are not mounted",
            ),
        )

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

    def test_probe_managed_mdns_takeover_uses_timed_subprobes(self) -> None:
        ps_out = "123 1 S 0:00 mdns-advertiser /mnt/Flash/mdns-advertiser\n"
        fstat_out = "root mdns-advertiser 123 4* internet dgram udp *:5353\n"
        with mock.patch(
            "timecapsulesmb.device.probe.run_ssh",
            side_effect=[
                mock.Mock(returncode=0, stdout="/mnt/Flash/mdns-advertiser\n", stderr=""),
                mock.Mock(returncode=0, stdout=ps_out, stderr=""),
                mock.Mock(returncode=0, stdout="ipv4\n", stderr=""),
                mock.Mock(returncode=0, stdout=fstat_out, stderr=""),
            ],
        ) as run_ssh_mock:
            result = probe_managed_mdns_takeover_conn(SshConnection("host", "pw", "-o foo"), timeout_seconds=60)

        self.assertTrue(result.ready)
        self.assertEqual([call.kwargs["timeout"] for call in run_ssh_mock.call_args_list], [8, 12, 24, 16])
        remote_commands = [call.args[1] for call in run_ssh_mock.call_args_list]
        self.assertIn("[ ! -e \"$RUNTIME_MDNS_BIN\" ]", remote_commands[0])
        self.assertIn("ps axww", remote_commands[1])
        self.assertIn("--print-mdns-socket-families", remote_commands[2])
        self.assertIn("/usr/bin/fstat -p 123", remote_commands[3])
        self.assertIn("PASS:mdns-advertiser bound to required UDP 5353 listeners", result.lines)
        self.assertIn("PASS:Apple mDNSResponder is stopped", result.lines)

    def test_probe_managed_mdns_takeover_retries_binary_probe_timeout_with_min_timeout(self) -> None:
        ps_out = "123 1 S 0:00 mdns-advertiser /mnt/Flash/mdns-advertiser\n"
        fstat_out = "root mdns-advertiser 123 4* internet dgram udp *:5353\n"
        with mock.patch(
            "timecapsulesmb.device.probe.run_ssh",
            side_effect=[
                SshCommandTimeout("Timed out waiting for ssh command to finish: binary"),
                mock.Mock(returncode=0, stdout="/mnt/Flash/mdns-advertiser\n", stderr=""),
                mock.Mock(returncode=0, stdout=ps_out, stderr=""),
                mock.Mock(returncode=0, stdout="ipv4\n", stderr=""),
                mock.Mock(returncode=0, stdout=fstat_out, stderr=""),
            ],
        ) as run_ssh_mock:
            result = probe_managed_mdns_takeover_conn(SshConnection("host", "pw", "-o foo"), timeout_seconds=1)

        self.assertTrue(result.ready)
        self.assertEqual([call.kwargs["timeout"] for call in run_ssh_mock.call_args_list[:2]], [5, 5])
        self.assertNotIn("FAIL:mdns-advertiser binary probe timed out after 5s", result.lines)

    def test_probe_managed_mdns_takeover_reports_binary_timeout_after_retry(self) -> None:
        with mock.patch(
            "timecapsulesmb.device.probe.run_ssh",
            side_effect=[
                SshCommandTimeout("Timed out waiting for ssh command to finish: binary"),
                SshCommandTimeout("Timed out waiting for ssh command to finish: binary"),
            ],
        ) as run_ssh_mock:
            result = probe_managed_mdns_takeover_conn(SshConnection("host", "pw", "-o foo"), timeout_seconds=1)

        self.assertFalse(result.ready)
        self.assertEqual(result.detail, "mdns-advertiser binary probe timed out after 5s")
        self.assertEqual([call.kwargs["timeout"] for call in run_ssh_mock.call_args_list], [5, 5])
        self.assertIn("FAIL:mdns-advertiser binary probe timed out after 5s", result.lines)

    def test_probe_managed_mdns_takeover_reports_apple_responder_conflict(self) -> None:
        ps_out = (
            "123 1 S 0:00 mdns-advertiser /mnt/Flash/mdns-advertiser\n"
            "124 1 S 0:00 mDNSResponder /usr/sbin/mDNSResponder\n"
        )
        fstat_out = "root mdns-advertiser 123 4* internet dgram udp *:5353\n"
        with mock.patch(
            "timecapsulesmb.device.probe.run_ssh",
            side_effect=[
                mock.Mock(returncode=0, stdout="/mnt/Flash/mdns-advertiser\n", stderr=""),
                mock.Mock(returncode=0, stdout=ps_out, stderr=""),
                mock.Mock(returncode=0, stdout="ipv4\n", stderr=""),
                mock.Mock(returncode=0, stdout=fstat_out, stderr=""),
            ],
        ):
            result = probe_managed_mdns_takeover_conn(SshConnection("host", "pw", "-o foo"), timeout_seconds=60)
        self.assertFalse(result.ready)
        self.assertEqual(result.detail, "Apple mDNSResponder is still running")

    def test_probe_managed_mdns_takeover_reports_socket_family_timeout(self) -> None:
        ps_out = "123 1 S 0:00 mdns-advertiser /mnt/Flash/mdns-advertiser\n"
        with mock.patch(
            "timecapsulesmb.device.probe.run_ssh",
            side_effect=[
                mock.Mock(returncode=0, stdout="/mnt/Flash/mdns-advertiser\n", stderr=""),
                mock.Mock(returncode=0, stdout=ps_out, stderr=""),
                SshCommandTimeout("Timed out waiting for ssh command to finish: socket families"),
            ],
        ):
            result = probe_managed_mdns_takeover_conn(SshConnection("host", "pw", "-o foo"), timeout_seconds=60)
        self.assertFalse(result.ready)
        self.assertEqual(result.detail, "mdns-advertiser socket family probe timed out after 24s")
        self.assertIn("FAIL:mdns-advertiser socket family probe timed out after 24s", result.lines)

    def test_probe_managed_mdns_takeover_reports_process_table_timeout(self) -> None:
        with mock.patch(
            "timecapsulesmb.device.probe.run_ssh",
            side_effect=[
                mock.Mock(returncode=0, stdout="/mnt/Flash/mdns-advertiser\n", stderr=""),
                SshCommandTimeout("Timed out waiting for ssh command to finish: ps"),
            ],
        ):
            result = probe_managed_mdns_takeover_conn(SshConnection("host", "pw", "-o foo"), timeout_seconds=60)
        self.assertFalse(result.ready)
        self.assertEqual(result.detail, "mDNS process table probe timed out after 12s")
        self.assertIn("FAIL:mDNS process table probe timed out after 12s", result.lines)

    def test_probe_managed_mdns_takeover_reports_fstat_timeout(self) -> None:
        ps_out = "123 1 S 0:00 mdns-advertiser /mnt/Flash/mdns-advertiser\n"
        with mock.patch(
            "timecapsulesmb.device.probe.run_ssh",
            side_effect=[
                mock.Mock(returncode=0, stdout="/mnt/Flash/mdns-advertiser\n", stderr=""),
                mock.Mock(returncode=0, stdout=ps_out, stderr=""),
                mock.Mock(returncode=0, stdout="ipv4\n", stderr=""),
                SshCommandTimeout("Timed out waiting for ssh command to finish: fstat"),
            ],
        ):
            result = probe_managed_mdns_takeover_conn(SshConnection("host", "pw", "-o foo"), timeout_seconds=60)
        self.assertFalse(result.ready)
        self.assertEqual(result.detail, "mdns-advertiser fstat probe timed out after 16s")
        self.assertIn("FAIL:mdns-advertiser fstat probe timed out after 16s", result.lines)

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
            mdns=readiness_result(True, "managed mDNS takeover active", ("PASS:managed mDNS takeover active",)),
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

    def test_complete_deployment_activate_now_runs_actions_and_verifies_runtime(self) -> None:
        prepared_plan = self._prepared_deploy_plan(startup_mode=DEPLOY_STARTUP_ACTIVATE_NOW)
        callbacks, stages, logs, _debug_fields, _finish_fields = self._operation_callbacks()
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")
        run_actions = mock.Mock()
        verify_runtime = mock.Mock()
        request_reboot_func = mock.Mock()
        request_reboot_and_wait_func = mock.Mock()

        result = complete_deployment_after_upload(
            connection,
            prepared_plan,
            no_wait=False,
            callbacks=callbacks,
            run_remote_actions_func=run_actions,
            request_reboot_func=request_reboot_func,
            request_reboot_and_wait_func=request_reboot_and_wait_func,
            verify_runtime_func=verify_runtime,
        )

        run_actions.assert_called_once_with(connection, prepared_plan.plan.activation_actions)
        request_reboot_func.assert_not_called()
        request_reboot_and_wait_func.assert_not_called()
        verify_runtime.assert_called_once()
        self.assertEqual(verify_runtime.call_args.kwargs["stage"], "verify_runtime_activation")
        self.assertEqual(stages, ["activate_runtime"])
        self.assertIn("Starting deployed runtime without reboot.", logs)
        self.assertFalse(result.reboot_requested)
        self.assertFalse(result.rebooted)
        self.assertTrue(result.verified)

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

        result = complete_deployment_after_upload(
            connection,
            prepared_plan,
            no_wait=False,
            callbacks=callbacks,
            run_remote_actions_func=run_actions,
            request_reboot_and_wait_func=request_reboot_and_wait_func,
            decide_post_reboot_activation=mock.Mock(return_value=activation_decision),
            verify_runtime_func=verify_runtime,
        )

        request_reboot_and_wait_func.assert_called_once()
        run_actions.assert_called_once_with(connection, prepared_plan.plan.activation_actions)
        verify_runtime.assert_called_once()
        self.assertEqual(stages, ["probe_runtime", "post_reboot_activation"])
        self.assertEqual(debug_fields["activation_decision"], "firmware_autostart_missing")
        self.assertTrue(debug_fields["manual_activation_required"])
        self.assertIn("Activating deployed runtime after reboot.", logs)
        self.assertTrue(result.rebooted)
        self.assertTrue(result.verified)

    def test_complete_deployment_netbsd6_reboot_waits_for_runtime(self) -> None:
        prepared_plan = self._prepared_deploy_plan(startup_mode=DEPLOY_STARTUP_REBOOT_THEN_VERIFY)
        callbacks, stages, logs, _debug_fields, _finish_fields = self._operation_callbacks()
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")
        verify_runtime = mock.Mock()

        result = complete_deployment_after_upload(
            connection,
            prepared_plan,
            no_wait=False,
            callbacks=callbacks,
            messages=DeployCompletionMessages(reboot_runtime_wait_message="Waiting for managed runtime..."),
            request_reboot_and_wait_func=mock.Mock(),
            verify_runtime_func=verify_runtime,
        )

        verify_runtime.assert_called_once()
        self.assertEqual(verify_runtime.call_args.kwargs["stage"], "verify_runtime_reboot")
        self.assertIn("Waiting for managed runtime...", logs)
        self.assertEqual(stages, [])
        self.assertTrue(result.verified)

    def test_probe_managed_runtime_polls_both_probes_and_rechecks_mdns_after_settle(self) -> None:
        smbd_ready = readiness_result(True, "managed smbd ready", ("PASS:managed smbd ready",))
        mdns_not_ready = readiness_result(False, "managed mDNS takeover not active", ("FAIL:managed mDNS takeover not active",))
        mdns_ready = readiness_result(True, "managed mDNS takeover active", ("PASS:managed mDNS takeover active",))
        connection = SshConnection("host", "pw", "-o foo")
        monotonic_values = iter([0.0, 0.0, 0.2, 1.3, 1.4, 5.1, 5.2, 5.3, 10.5, 10.6, 10.7])
        with mock.patch("timecapsulesmb.device.probe.probe_managed_smbd_conn", side_effect=[smbd_ready, smbd_ready]) as smbd_mock:
            with mock.patch("timecapsulesmb.device.probe.probe_managed_mdns_takeover_conn", side_effect=[mdns_not_ready, mdns_ready, mdns_ready]) as mdns_mock:
                with mock.patch("timecapsulesmb.device.probe.time.monotonic", side_effect=lambda: next(monotonic_values)):
                    with mock.patch("timecapsulesmb.device.probe.time.sleep") as sleep_mock:
                        result = probe_managed_runtime_conn(connection, timeout_seconds=20, poll_interval_seconds=5.0, smbd_mdns_stagger_seconds=1.0)
        self.assertTrue(result.ready)
        self.assertEqual(smbd_mock.call_count, 1)
        self.assertEqual(mdns_mock.call_count, 3)
        self.assertNotIn(mock.call(1.0), sleep_mock.call_args_list)
        self.assertIn(mock.call(3.0), sleep_mock.call_args_list)

    def test_probe_managed_runtime_continues_polling_after_single_probe_timeout(self) -> None:
        smbd_timeout = readiness_result(False, "managed smbd readiness probe timed out", ("FAIL:managed smbd readiness probe timed out",))
        smbd_ready = readiness_result(True, "managed smbd ready", ("PASS:managed smbd ready",))
        mdns_ready = readiness_result(True, "managed mDNS takeover active", ("PASS:managed mDNS takeover active",))
        connection = SshConnection("host", "pw", "-o foo")
        monotonic_values = iter([0.0, 0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6])
        with mock.patch("timecapsulesmb.device.probe.probe_managed_smbd_conn", side_effect=[smbd_timeout, smbd_ready]) as smbd_mock:
            with mock.patch("timecapsulesmb.device.probe.probe_managed_mdns_takeover_conn", side_effect=[mdns_ready, mdns_ready]) as mdns_mock:
                with mock.patch("timecapsulesmb.device.probe.time.monotonic", side_effect=lambda: next(monotonic_values)):
                    with mock.patch("timecapsulesmb.device.probe.time.sleep"):
                        result = probe_managed_runtime_conn(
                            connection,
                            timeout_seconds=10,
                            poll_interval_seconds=0.1,
                            smbd_mdns_stagger_seconds=0.0,
                            mdns_settle_seconds=0.0,
                        )
        self.assertTrue(result.ready)
        self.assertEqual(smbd_mock.call_count, 2)
        self.assertEqual(mdns_mock.call_count, 2)

    def test_probe_managed_runtime_reports_readable_timeout(self) -> None:
        smbd_timeout = readiness_result(False, "managed smbd readiness probe timed out", ("FAIL:managed smbd readiness probe timed out",))
        mdns_timeout = readiness_result(False, "managed mDNS takeover probe timed out", ("FAIL:managed mDNS takeover probe timed out",))
        connection = SshConnection("host", "pw", "-o foo")
        monotonic_values = iter([0.0, 0.0, 0.1, 0.2, 0.3, 1.1])
        with mock.patch("timecapsulesmb.device.probe.probe_managed_smbd_conn", return_value=smbd_timeout):
            with mock.patch("timecapsulesmb.device.probe.probe_managed_mdns_takeover_conn", return_value=mdns_timeout):
                with mock.patch("timecapsulesmb.device.probe.time.monotonic", side_effect=lambda: next(monotonic_values)):
                    with mock.patch("timecapsulesmb.device.probe.time.sleep"):
                        result = probe_managed_runtime_conn(
                            connection,
                            timeout_seconds=1,
                            poll_interval_seconds=0.1,
                            smbd_mdns_stagger_seconds=0.0,
                            mdns_settle_seconds=0.0,
                        )
        self.assertFalse(result.ready)
        self.assertIn("runtime verification timed out after 1s", result.detail)
        self.assertIn("FAIL:runtime verification timed out after 1s", result.lines)

    def test_format_deployment_plan_contains_concrete_actions(self) -> None:
        payload_dir_name = "samba4"
        payload_dir = f"/Volumes/dk2/{payload_dir_name}"
        paths = self._payload_home("/Volumes/dk2", payload_dir_name)
        plan = build_deployment_plan("root@10.0.0.2", paths, Path("bin/smbd"), Path("bin/mdns"), Path("bin/nbns"))
        text = format_deployment_plan(plan)
        self.assertIn("volume root: /Volumes/dk2", text)
        self.assertEqual(plan.device_path, "/dev/dk2")
        self.assertIn(f"diskd.useVolume wait: {DEFAULT_APPLE_MOUNT_WAIT_SECONDS}s per attempt", text)
        self.assertIn("tc_kill_manager_pids TERM", text)
        self.assertIn("tc_kill_watchdog_pids TERM", text)
        self.assertNotIn("/usr/bin/pkill -f '[m]anager.sh'", text)
        self.assertNotIn("/usr/bin/pkill -f '[w]atchdog.sh'", text)
        self.assertIn("/usr/bin/pkill '^mdns-advertiser$' >/dev/null 2>&1 || true", text)
        self.assertIn("/usr/bin/acp rpc diskd.useVolume path:s:/Volumes/dk2", text)
        self.assertIn(f"mkdir -p {payload_dir} {payload_dir}/private {payload_dir}/cache /mnt/Flash", text)
        self.assertIn(f"rm -rf {payload_dir}/smb.conf.template", text)
        self.assertIn(f"rm -rf {payload_dir}/private/adisk.uuid", text)
        self.assertIn(f"rm -rf {payload_dir}/private/nbns.enabled", text)
        self.assertIn(f"generated smbpasswd (generated:smbpasswd, generated) -> {payload_dir}/private/smbpasswd", text)
        self.assertIn("generated flash runtime config (generated:tcapsulesmb.conf, flash_atomic, timeout 120s) -> /mnt/Flash/tcapsulesmb.conf", text)
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
            Path("bin/mdns"),
            Path("bin/nbns"),
            startup_mode=DEPLOY_STARTUP_REBOOT_THEN_ACTIVATE,
        )
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
        self.assertIn("managed mDNS takeover becomes ready", text)

    def test_activate_now_plan_has_runtime_checks(self) -> None:
        paths = self._payload_home("/Volumes/dk2", "samba4")
        plan = build_deployment_plan(
            "root@10.0.0.2",
            paths,
            Path("bin/smbd"),
            Path("bin/mdns"),
            Path("bin/nbns"),
            startup_mode=DEPLOY_STARTUP_ACTIVATE_NOW,
        )
        self.assertFalse(plan.reboot_required)
        self.assertEqual(plan.startup_mode, DEPLOY_STARTUP_ACTIVATE_NOW)
        self.assertEqual(
            plan.activation_actions,
            [
                StopManagerAction(),
                StopWatchdogAction(),
                StopProcessAction("wcifsfs"),
                RunScriptAction("/mnt/Flash/rc.local"),
            ],
        )
        self.assertEqual([check.id for check in plan.post_deploy_checks], [
            "managed_runtime_smbd_binary_present",
            "managed_runtime_smb_conf_present",
            "active_smb_conf_passdb_ram",
            "active_smb_conf_username_map_ram",
            "active_smb_conf_xattr_tdb_persistent",
            "managed_share_volumes_mounted",
            "managed_runtime_manager_process",
            "managed_smbd_parent_process",
            "managed_smbd_bound_445",
            "managed_mdns_takeover_ready",
            "managed_mdns_settle_healthy",
        ])
        text = format_deployment_plan(plan)
        self.assertIn("mode: activate_now", text)
        self.assertIn("Reboot:\n  no", text)
        self.assertIn("follow-up: run /mnt/Flash/rc.local without rebooting", text)
        self.assertIn("managed runtime smb.conf is present", text)

    def test_reboot_then_activate_no_wait_plan_skips_post_reboot_activation_and_checks(self) -> None:
        paths = self._payload_home("/Volumes/dk2", "samba4")
        plan = build_deployment_plan(
            "root@10.0.0.2",
            paths,
            Path("bin/smbd"),
            Path("bin/mdns"),
            Path("bin/nbns"),
            startup_mode=DEPLOY_STARTUP_REBOOT_THEN_ACTIVATE,
            wait_after_reboot=False,
        )

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
        self.assertTrue(any(command.startswith("/usr/bin/pkill '^nbns-advertiser$' >/dev/null 2>&1 || true;") for command in rendered))

    def test_build_uninstall_plan_stops_supervisors_first(self) -> None:
        plan = build_uninstall_plan("root@10.0.0.2", ["/Volumes/dk2"], ["/Volumes/dk2/samba4"])
        rendered = [render_remote_action(action) for action in plan.remote_actions]
        self.assertTrue(rendered[0].startswith("tc_manager_pids() { "))
        self.assertIn("tc_kill_manager_pids TERM", rendered[0])
        self.assertTrue(rendered[1].startswith("tc_watchdog_pids() { "))
        self.assertIn("tc_kill_watchdog_pids TERM", rendered[1])
        self.assertNotIn("/usr/bin/pkill -f '[m]anager.sh'", rendered[0])
        self.assertNotIn("/usr/bin/pkill -f '[w]atchdog.sh'", rendered[1])

    def test_build_uninstall_plan_removes_flash_configuration(self) -> None:
        plan = build_uninstall_plan("root@10.0.0.2", ["/Volumes/dk2"], ["/Volumes/dk2/samba4"])

        self.assertNotIn("allmdns.txt", plan.flash_targets)
        self.assertNotIn("applemdns.txt", plan.flash_targets)
        self.assertEqual(plan.flash_targets["tcapsulesmb.conf"], "/mnt/Flash/tcapsulesmb.conf")
        self.assertNotIn("/mnt/Flash/allmdns.txt", plan.verify_absent_targets)
        self.assertNotIn("/mnt/Flash/applemdns.txt", plan.verify_absent_targets)
        self.assertIn("/mnt/Flash/tcapsulesmb.conf", plan.verify_absent_targets)
        self.assertNotIn(RemovePathAction("/mnt/Flash/allmdns.txt"), plan.remote_actions)
        self.assertNotIn(RemovePathAction("/mnt/Flash/applemdns.txt"), plan.remote_actions)
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
                    RemotePermission(f"{payload_dir}/private/smbpasswd", "600"),
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
        self.assertIn("'/Volumes/dk2/Time Capsule Samba 4/private/smbpasswd'", permissions_cmd)
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
        plan = build_deployment_plan("host", paths, Path("bin/smbd"), Path("bin/mdns"), Path("bin/nbns"))
        self.assertEqual(plan.post_upload_actions[0], EnsureVolumeMountedAction("/Volumes/dk2", "/dev/dk2", DEFAULT_APPLE_MOUNT_WAIT_SECONDS))
        self.assertIn(InstallPermissionsAction(tuple(plan.permissions)), plan.post_upload_actions)

    def test_deployment_plan_guards_each_payload_write_action(self) -> None:
        paths = self._payload_home("/Volumes/dk2", "samba4")
        plan = build_deployment_plan("host", paths, Path("bin/smbd"), Path("bin/mdns"), Path("bin/nbns"))
        expected_guard = EnsureVolumeMountedAction("/Volumes/dk2", "/dev/dk2", DEFAULT_APPLE_MOUNT_WAIT_SECONDS)

        self.assertEqual(plan.pre_upload_actions[7], expected_guard)
        self.assertEqual(plan.pre_upload_actions[9], expected_guard)
        self.assertEqual(plan.pre_upload_actions[11], expected_guard)
        self.assertEqual(plan.pre_upload_actions[13], expected_guard)
        self.assertEqual(plan.post_upload_actions[0], expected_guard)

    def test_deployment_plan_marks_uploaded_payload_binaries_executable(self) -> None:
        paths = self._payload_home("/Volumes/dk2", "samba4")
        plan = build_deployment_plan("host", paths, Path("bin/smbd"), Path("bin/mdns"), Path("bin/nbns"))
        executable_permissions = {permission.path for permission in plan.permissions if permission.mode == "755"}

        self.assertIn("/Volumes/dk2/samba4/smbd", executable_permissions)
        self.assertIn("/Volumes/dk2/samba4/mdns-advertiser", executable_permissions)
        self.assertIn("/Volumes/dk2/samba4/nbns-advertiser", executable_permissions)

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
        command = render_remote_action(StopProcessAction("mdns-advertiser"))
        self.assertIn("/usr/bin/pkill '^mdns-advertiser$' >/dev/null 2>&1 || true;", command)
        self.assertIn("while /bin/sh -c 'found=1; if ps axww -o stat= -o ucomm= -o command= >/tmp/tcapsule-ps.", command)
        self.assertIn('case \"$1\" in Z*) continue ;; esac;', command)
        self.assertIn('if [ \"$2\" = mdns-advertiser ]; then found=1; break; fi;', command)
        self.assertIn('if [ "$attempt" -ge 5 ]; then break; fi;', command)
        self.assertIn("/usr/bin/pkill -9 '^mdns-advertiser$' >/dev/null 2>&1 || true;", command)

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
        run_ssh_mock.assert_called_once_with(connection, "/bin/echo ok", check=False, timeout=10)

    def test_wait_for_ssh_state_treats_probe_failure_as_down(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "-o ProxyCommand=jump")
        with mock.patch("timecapsulesmb.device.probe.run_ssh", side_effect=SshError("timeout")) as run_ssh_mock:
            self.assertTrue(wait_for_ssh_state_conn(connection, expected_up=False, timeout_seconds=1))
        run_ssh_mock.assert_called_once_with(connection, "/bin/echo ok", check=False, timeout=10)

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
