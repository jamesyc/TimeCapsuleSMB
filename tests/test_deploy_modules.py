from __future__ import annotations

import shutil
import shlex
import subprocess
import sys
import tempfile
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
    StopProcessAction,
    StopWatchdogAction,
    ensure_volume_mounted_action,
    install_permissions_action,
    prepare_dirs_action,
    remote_action_to_jsonable,
    render_remote_action,
)
from timecapsulesmb.deploy.dry_run import format_deployment_plan
from timecapsulesmb.deploy.executor import (
    DETACHED_REBOOT_COMMAND,
    REBOOT_REQUEST_TIMEOUT_SECONDS,
    remote_request_reboot,
    remote_uninstall_payload,
    upload_deployment_payload,
    upload_flash_file,
)
from timecapsulesmb.deploy.planner import (
    BINARY_MDNS_SOURCE,
    BINARY_NBNS_SOURCE,
    BINARY_SMBD_SOURCE,
    DEFAULT_APPLE_MOUNT_WAIT_SECONDS,
    FLASH_TEXT_UPLOAD_TIMEOUT_SECONDS,
    GENERATED_FLASH_CONFIG_SOURCE,
    GENERATED_SMBPASSWD_SOURCE,
    GENERATED_USERNAME_MAP_SOURCE,
    PACKAGED_COMMON_SH_SOURCE,
    PACKAGED_DFREE_SH_SOURCE,
    PACKAGED_RC_LOCAL_SOURCE,
    PACKAGED_START_SAMBA_SOURCE,
    PACKAGED_WATCHDOG_SOURCE,
    PAYLOAD_BINARY_UPLOAD_TIMEOUT_SECONDS,
    build_deployment_plan,
    build_uninstall_plan,
)
from timecapsulesmb.deploy.boot_assets import (
    load_boot_asset_text,
)
from timecapsulesmb.deploy.verify import (
    VerificationResult,
    managed_runtime_ready,
    render_managed_runtime_verification,
    render_post_uninstall_verification,
    verify_managed_runtime,
    verify_post_uninstall,
)
from timecapsulesmb.core.config import AppConfig
from timecapsulesmb.device.processes import render_process_present
from timecapsulesmb.device.probe import (
    ManagedMdnsTakeoverProbeResult,
    ManagedRuntimeProbeResult,
    ManagedSmbdProbeResult,
    SMBD_STATUS_HELPERS,
    derive_runtime_naming_identity,
    extract_airport_identity_from_acp_output,
    extract_airport_identity_from_text,
    probe_remote_runtime_naming_identity_conn,
    probe_device_conn,
    probe_managed_runtime_conn,
    probe_managed_mdns_takeover_conn,
    probe_managed_smbd_conn,
    probe_remote_airport_identity_conn,
    wait_for_ssh_state_conn,
)
from timecapsulesmb.device.storage import MaStVolume, PayloadHome, mounted_mast_volumes_conn
from timecapsulesmb.transport.ssh import SshCommandTimeout, SshConnection, SshError


class DeployModuleTests(unittest.TestCase):
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

        bin_path = tmp / "mdns-advertiser"
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
        return bin_path

    def _run_mdns_advertiser_until_ready_or_exit(self, bin_path: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
        proc = subprocess.Popen(
            [str(bin_path), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            stdout, stderr = proc.communicate(timeout=2)
            return subprocess.CompletedProcess([str(bin_path), *args], proc.returncode, stdout, stderr)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                stdout, stderr = proc.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                stdout, stderr = proc.communicate(timeout=2)
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
        smbpasswd_text, username_map = render_smbpasswd("admin", "password")
        self.assertTrue(smbpasswd_text.startswith("root:0:"))
        self.assertEqual(username_map, "!root = root\nroot = *\n")

    def test_remote_request_reboot_uses_explicit_reboot_timeout(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")
        with mock.patch("timecapsulesmb.deploy.executor.run_ssh") as run_ssh_mock:
            remote_request_reboot(connection)
        run_ssh_mock.assert_called_once_with(
            connection,
            DETACHED_REBOOT_COMMAND,
            check=False,
            timeout=REBOOT_REQUEST_TIMEOUT_SECONDS,
        )

    def test_load_boot_asset_text_reads_packaged_asset(self) -> None:
        content = load_boot_asset_text("rc.local")
        self.assertIn("/mnt/Flash/start-samba.sh", content)
        common = load_boot_asset_text("common.sh")
        self.assertIn("get_airport_syvs()", common)
        self.assertIn("ether[[:space:]]", common)
        self.assertIn("address[[:space:]]", common)
        self.assertNotIn("tr '[:lower:]' '[:upper:]'", common)
        self.assertNotIn("/usr/bin/wc", common)
        self.assertNotIn("/usr/bin/tr", common)

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
        self.assertIn("ALL_MDNS_SNAPSHOT=/mnt/Flash/allmdns.txt", content)
        self.assertIn("APPLE_MDNS_SNAPSHOT=/mnt/Flash/applemdns.txt", content)
        self.assertIn("get_iface_ipv4()", content)
        self.assertIn("get_iface_mac()", content)
        self.assertIn("get_radio_mac()", content)
        self.assertIn("get_airport_srcv()", content)
        self.assertIn("get_airport_syvs()", content)
        self.assertIn("wait_for_process()", content)
        self.assertIn("ensure_parent_dir()", content)
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
                        "104 S    sh              /bin/sh /mnt/Flash/watchdog.sh",
                        "105 S    sh              /bin/sh -c probe=/mnt/Flash/watchdog.sh",
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
runtime_watchdog_present; echo "live-full=$?"
runtime_watchdog_present < /dev/null; echo "live-full-repeat=$?"
echo "watchdog-pids=$(runtime_watchdog_pids)"
runtime_process_present_by_ucomm wcifsfs; echo "zombie-full=$?"
wait_for_process nbns-advertiser 1; echo "live-wait=$?"
wait_for_process wcifsnd 1; echo "zombie-wait=$?"
"""
            )

            result = subprocess.run(["/bin/sh", str(script)], check=False, text=True, capture_output=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("zombie-name=1", result.stdout)
        self.assertIn("live-name=0", result.stdout)
        self.assertIn("live-full=0", result.stdout)
        self.assertIn("live-full-repeat=0", result.stdout)
        self.assertIn("watchdog-pids=104", result.stdout)
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

    def test_common_watchdog_process_helper_does_not_self_match_literal(self) -> None:
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
                        "101 S    sh              /bin/sh -c probe=/mnt/Flash/watchdog.sh",
                        "102 S    sh              sh -c /bin/sh -c 'probe=/mnt/Flash/watchdog.sh'",
                    ]
                )
                + "\n"
            )
            script.write_text(
                common
                + f"\nPS_FIXTURE={shlex.quote(str(fixture))}\n"
                + """
runtime_watchdog_present; echo "watchdog=$?"
echo "watchdog-pids=$(runtime_watchdog_pids)"
"""
            )

            result = subprocess.run(["/bin/sh", str(script)], check=False, text=True, capture_output=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("watchdog=1", result.stdout)
        self.assertIn("watchdog-pids=", result.stdout)

    def test_common_watchdog_kill_helper_targets_only_detected_pids(self) -> None:
        common = load_boot_asset_text("common.sh").replace("/bin/kill", "record_kill")
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "check.sh"
            kill_log = Path(tmp) / "kill.log"
            script.write_text(
                common
                + f"\nKILL_LOG={shlex.quote(str(kill_log))}\n"
                + """
record_kill() { echo "kill:$*" >> "$KILL_LOG"; }
runtime_watchdog_pids() { printf '%s\\n' 111 222; }
kill_watchdog_pids TERM
kill_watchdog_pids KILL
"""
            )

            result = subprocess.run(["/bin/sh", str(script)], check=False, text=True, capture_output=True)
            kill_lines = kill_log.read_text().splitlines()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            kill_lines,
            ["kill:111", "kill:222", "kill:-9 111", "kill:-9 222"],
        )

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

    def test_common_sh_helpers_take_iface_argument(self) -> None:
        content = load_boot_asset_text("common.sh")
        self.assertIn("iface=$1", content)
        self.assertIn('ifconfig "$iface"', content)
        self.assertIn("radio_iface=$1", content)
        self.assertIn('ifconfig "$radio_iface"', content)

    def test_common_sh_allows_partial_airport_field_derivation(self) -> None:
        content = load_boot_asset_text("common.sh")
        self.assertIn('if [ -n "$AIRPORT_WAMA" ] || [ -n "$AIRPORT_RAMA" ] || [ -n "$AIRPORT_RAM2" ] || [ -n "$AIRPORT_SRCV" ] || [ -n "$AIRPORT_SYVS" ]; then', content)

    def test_start_and_watchdog_source_common_sh(self) -> None:
        start = load_boot_asset_text("start-samba.sh")
        watchdog = load_boot_asset_text("watchdog.sh")
        self.assertIn(". /mnt/Flash/common.sh", start)
        self.assertIn(". /mnt/Flash/common.sh", watchdog)
        self.assertNotIn("RAM_SAMBA_LIBEXEC", start)
        self.assertNotIn("stage_runtime_helper", start)
        self.assertNotIn("get_radio_mac()", start)
        self.assertNotIn("get_airport_srcv()", start)
        self.assertNotIn("get_airport_syvs()", start)
        self.assertNotIn("wait_for_process()", start)
        self.assertNotIn("wait_for_smbd_ready()", start)
        self.assertNotIn("get_radio_mac()", watchdog)
        self.assertNotIn("get_airport_srcv()", watchdog)
        self.assertNotIn("get_airport_syvs()", watchdog)

    def test_rc_local_leaves_watchdog_launch_to_start_samba(self) -> None:
        content = load_boot_asset_text("rc.local")
        self.assertIn("/mnt/Flash/start-samba.sh </dev/null >/dev/null 2>&1 &", content)
        self.assertNotIn("/mnt/Flash/watchdog.sh", content)
        self.assertNotIn("pkill -0 -f /mnt/Flash/watchdog.sh", content)

    def test_rc_local_detaches_background_jobs_from_stdin(self) -> None:
        content = load_boot_asset_text("rc.local")
        self.assertIn("/mnt/Flash/start-samba.sh </dev/null >/dev/null 2>&1 &", content)

    def test_common_script_has_no_smbd_daemon_ready_helpers(self) -> None:
        common = (REPO_ROOT / "src/timecapsulesmb/assets/boot/samba4/common.sh").read_text()
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
    if (send_query_question(1, &dest, "home.local.", DNS_TYPE_A) != 0 ||
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
        self.assertNotIn("serving summary", run.stderr)

    def test_mdns_advertiser_save_args_capture_and_exit_without_takeover(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            bin_path = self._compile_mdns_advertiser_binary(tmp)
            all_snapshot = tmp / "allmdns.txt"
            apple_snapshot = tmp / "applemdns.txt"
            run = subprocess.run(
                [
                    str(bin_path),
                    "--save-all-snapshot",
                    str(all_snapshot),
                    "--save-snapshot",
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
        self.assertIn("mdns capture-only:", run.stderr)
        self.assertIn("exiting without UDP 5353 takeover or advertisement", run.stderr)
        self.assertNotIn("serving summary", run.stderr)
        self.assertNotIn("mDNS takeover", run.stderr)

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

    def test_mdns_advertiser_load_arg_reaches_takeover_after_loading_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            bin_path = self._compile_mdns_advertiser_binary(tmp)
            snapshot = tmp / "applemdns.txt"
            self._write_matching_airport_snapshot(snapshot)
            run = self._run_mdns_advertiser_until_ready_or_exit(
                bin_path,
                [
                    "--load-snapshot",
                    str(snapshot),
                    "--instance",
                    "TimeCapsule",
                    "--host",
                    "timecapsulesamba",
                    "--ipv4",
                    "127.0.0.1",
                    "--airport-wama",
                    "80:EA:96:E6:58:68",
                ],
            )
        self.assertIn("snapshot load: loaded 1 records, advertising 1 snapshot records", run.stderr)
        self.assertIn("serving summary:", run.stderr)
        self.assertNotIn("mdns capture-only:", run.stderr)

    def test_mdns_advertiser_save_and_load_args_preserve_combined_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            bin_path = self._compile_mdns_advertiser_binary(tmp)
            all_snapshot = tmp / "allmdns.txt"
            apple_snapshot = tmp / "applemdns.txt"
            self._write_matching_airport_snapshot(apple_snapshot)
            run = self._run_mdns_advertiser_until_ready_or_exit(
                bin_path,
                [
                    "--save-all-snapshot",
                    str(all_snapshot),
                    "--save-snapshot",
                    str(apple_snapshot),
                    "--load-snapshot",
                    str(apple_snapshot),
                    "--instance",
                    "TimeCapsule",
                    "--host",
                    "timecapsulesamba",
                    "--ipv4",
                    "127.0.0.1",
                    "--airport-wama",
                    "80:EA:96:E6:58:68",
                ],
            )
        self.assertIn("warning: could not capture Apple mDNS snapshot", run.stderr)
        self.assertIn("snapshot load: loaded 1 records, advertising 1 snapshot records", run.stderr)
        self.assertIn("serving summary:", run.stderr)
        self.assertNotIn("mdns capture-only:", run.stderr)

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
    snprintf(cfg.adisk_share_name, sizeof(cfg.adisk_share_name), "%s", "Data");
    snprintf(cfg.adisk_disk_key, sizeof(cfg.adisk_disk_key), "%s", "dk2");
    snprintf(cfg.adisk_uuid, sizeof(cfg.adisk_uuid), "%s", "c4f673b8-c422-4da7-92a1-54bffe406af2");
    snprintf(cfg.adisk_disk_advf, sizeof(cfg.adisk_disk_advf), "%s", "0x82");
    snprintf(cfg.adisk_sys_wama, sizeof(cfg.adisk_sys_wama), "%s", "80:EA:96:E6:58:68");
    cfg.port = 445;
    cfg.adisk_port = 9;
    cfg.ttl = 120;
    cfg.ipv4_addr = inet_addr("192.168.1.217");

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

    if (send_announcement(1, &dest, &cfg, &snapshot, 1) != 0) {{
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

    def test_nbns_advertiser_rejects_overlong_name_before_truncation(self) -> None:
        if shutil.which("cc") is None:
            self.skipTest("cc not available")

        source_path = REPO_ROOT / "build" / "nbns-advertiser.c"
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            bin_path = tmp / "nbns-advertiser"
            proc = subprocess.run(
                ["cc", "-Wall", "-Wextra", "-Werror", str(source_path), "-o", str(bin_path)],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            run = subprocess.run(
                [str(bin_path), "--name", "ABCDEFGHIJKLMNOP", "--ipv4", "192.168.1.217"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(run.returncode, 2)
            self.assertIn("15 bytes or fewer", run.stderr)

    def test_mounted_mast_volumes_mounts_each_volume_and_returns_successes(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")
        internal = self._mast_volume("dk2", name="Internal", builtin=True)
        external = self._mast_volume("dk5", disk_device="sd0", name="External", builtin=False)

        with mock.patch("timecapsulesmb.device.storage.ensure_mast_volume_mounted_conn", side_effect=[True, False]) as mount_mock:
            mounted = mounted_mast_volumes_conn(connection, (internal, external), wait_seconds=17)

        self.assertEqual(mounted, (internal,))
        self.assertEqual(
            mount_mock.call_args_list,
            [
                mock.call(connection, internal, wait_seconds=17),
                mock.call(connection, external, wait_seconds=17),
            ],
        )

    def test_mounted_mast_volumes_returns_empty_when_no_volume_mounts(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")
        internal = self._mast_volume("dk2", name="Internal", builtin=True)
        external = self._mast_volume("dk5", disk_device="sd0", name="External", builtin=False)

        with mock.patch("timecapsulesmb.device.storage.ensure_mast_volume_mounted_conn", return_value=False):
            mounted = mounted_mast_volumes_conn(connection, (internal, external), wait_seconds=30)

        self.assertEqual(mounted, ())

    def test_probe_device_skips_direct_tcp_check_for_proxy_ssh_options(self) -> None:
        with mock.patch("timecapsulesmb.device.probe.tcp_open", side_effect=AssertionError("direct TCP probe should be skipped")):
            with mock.patch("timecapsulesmb.device.probe._probe_remote_os_info_conn", return_value=("NetBSD", "4.0", "earmv4")):
                with mock.patch("timecapsulesmb.device.probe._probe_remote_elf_endianness_conn", return_value="big"):
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
            PACKAGED_DFREE_SH_SOURCE: Path("/tmp/dfree.sh"),
            PACKAGED_START_SAMBA_SOURCE: Path("/tmp/start-samba.sh"),
            PACKAGED_WATCHDOG_SOURCE: Path("/tmp/watchdog.sh"),
        }
        with mock.patch("timecapsulesmb.deploy.executor.run_scp") as scp_mock:
            with mock.patch("timecapsulesmb.deploy.executor.run_ssh") as ssh_mock:
                with mock.patch("timecapsulesmb.deploy.executor.ensure_volume_root_mounted_conn", return_value=True) as mount_mock:
                    upload_deployment_payload(
                        plan,
                        connection=connection,
                        source_resolver=source_resolver,
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
                Path("/tmp/start-samba.sh"),
                Path("/tmp/watchdog.sh"),
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
                "/mnt/Flash/.start-samba.sh.tmp",
                "/mnt/Flash/.watchdog.sh.tmp",
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

    def test_verify_managed_runtime_passes_when_runtime_probe_succeeds(self) -> None:
        result = ManagedRuntimeProbeResult(
            ready=True,
            detail="managed runtime is ready",
            smbd=ManagedSmbdProbeResult(True, "managed smbd ready", ("PASS:managed smbd ready",)),
            mdns=ManagedMdnsTakeoverProbeResult(True, "managed mDNS takeover active", ("PASS:managed mDNS takeover active",)),
            lines=("PASS:managed smbd ready", "PASS:managed mDNS takeover active"),
        )
        with mock.patch("timecapsulesmb.deploy.verify.probe_managed_runtime_conn", return_value=result):
            verification = verify_managed_runtime(SshConnection("host", "pw", "-o foo"))

        self.assertIs(verification, result)
        self.assertTrue(managed_runtime_ready(verification))
        self.assertEqual(
            render_managed_runtime_verification(verification, heading="NetBSD4 activation verification:"),
            [
                "NetBSD4 activation verification:",
                "  ok: managed smbd ready",
                "  ok: managed mDNS takeover active",
            ],
        )

    def test_verify_managed_runtime_fails_when_runtime_probe_fails(self) -> None:
        result = ManagedRuntimeProbeResult(
            ready=False,
            detail="managed runtime is not ready",
            smbd=ManagedSmbdProbeResult(False, "managed smbd is not ready", ("FAIL:managed smbd is not ready",)),
            mdns=ManagedMdnsTakeoverProbeResult(True, "managed mDNS takeover active", ("PASS:managed mDNS takeover active",)),
            lines=("FAIL:managed smbd is not ready", "PASS:managed mDNS takeover active"),
        )
        with mock.patch("timecapsulesmb.deploy.verify.probe_managed_runtime_conn", return_value=result):
            verification = verify_managed_runtime(SshConnection("host", "pw", "-o foo"))

        self.assertIs(verification, result)
        self.assertFalse(managed_runtime_ready(verification))
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

    def test_probe_status_helpers_do_not_count_probe_shell_body_as_watchdog(self) -> None:
        script = (
            SMBD_STATUS_HELPERS
            + r'''
real_watchdog="202 1 S 0:00.00 sh /bin/sh /mnt/Flash/watchdog.sh"
self_match_watchdog=$(cat <<'EOF'
3308 11745 S 0:00.01 sh /bin/sh -c probe=/mnt/Flash/watchdog.sh
11745 11677 Ss 0:00.01 sh sh -c /bin/sh -c 'probe=/mnt/Flash/watchdog.sh'
EOF
)
watchdog_process_present_for_volume "$real_watchdog"; echo "real=$?"
watchdog_process_present_for_volume "$self_match_watchdog"; echo "self=$?"
'''
        )

        result = subprocess.run(["/bin/sh", "-c", script], check=False, text=True, capture_output=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("real=0", result.stdout)
        self.assertIn("self=1", result.stdout)

    def test_smbd_status_helpers_pass_only_with_live_ram_auth_mount_and_watchdog(self) -> None:
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
""",
                encoding="utf-8",
            )
            shares_tsv = ram_root / "var" / "shares.tsv"
            shares_tsv.write_text(
                f"Data\t{data_root}\tdk2\t1\taaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa\n"
                f"USB\t{external_data_root}\tdk3\t0\tbbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb\n",
                encoding="utf-8",
            )
            ps_out = (
                "101 1 S 0:00.00 smbd /mnt/Memory/samba4/sbin/smbd -D -s /mnt/Memory/samba4/etc/smb.conf\n"
                "202 1 S 0:00.00 sh /bin/sh /mnt/Flash/watchdog.sh\n"
            )
            script = f"""
RUNTIME_RAM_ROOT={shlex.quote(str(ram_root))}
RUNTIME_SMB_CONF_PATH={shlex.quote(str(smb_conf))}
RUNTIME_SHARES_TSV_PATH={shlex.quote(str(shares_tsv))}
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
        self.assertIn("PASS:watchdog is running for managed runtime", result.stdout)
        self.assertIn("PASS:smbd bound to TCP 445", result.stdout)
        self.assertIn("status=0", result.stdout)

    def test_smbd_status_helpers_fail_for_disk_auth_unmounted_volume_and_missing_watchdog(self) -> None:
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
        self.assertIn("FAIL:watchdog is not running for managed runtime", result.stdout)
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

    def test_probe_managed_mdns_takeover_single_shot_checks_process_binding_and_apple_responder(self) -> None:
        with mock.patch(
            "timecapsulesmb.device.probe.run_ssh",
            return_value=mock.Mock(returncode=0, stdout=""),
        ) as run_ssh_mock:
            self.assertTrue(probe_managed_mdns_takeover_conn(SshConnection("host", "pw", "-o foo"), timeout_seconds=45).ready)
        remote_command = run_ssh_mock.call_args.args[1]
        self.assertIn("capture_ps_out()", remote_command)
        self.assertIn("mdns_process_present()", remote_command)
        self.assertIn("apple_mdns_present()", remote_command)
        self.assertIn('capture_fstat_for_ucomm "$ps_out" mdns-advertiser', remote_command)
        self.assertIn('/usr/bin/fstat -p "$1"', remote_command)
        self.assertIn("mdns_bound_5353()", remote_command)
        self.assertNotIn('out="$(fstat 2>&1)"', remote_command)
        self.assertNotIn("max_attempts", remote_command)
        self.assertNotIn("sleep 5", remote_command)

    def test_probe_managed_mdns_takeover_returns_detail_when_not_ready(self) -> None:
        with mock.patch(
            "timecapsulesmb.device.probe.run_ssh",
            return_value=mock.Mock(returncode=1, stdout="FAIL:Apple mDNSResponder is still running\n"),
        ):
            result = probe_managed_mdns_takeover_conn(SshConnection("host", "pw", "-o foo"), timeout_seconds=12)
        self.assertFalse(result.ready)
        self.assertEqual(result.detail, "Apple mDNSResponder is still running")

    def test_probe_managed_mdns_takeover_returns_detail_when_probe_times_out(self) -> None:
        with mock.patch(
            "timecapsulesmb.device.probe.run_ssh",
            side_effect=SshCommandTimeout("Timed out waiting for ssh command to finish: runtime probe"),
        ):
            result = probe_managed_mdns_takeover_conn(SshConnection("host", "pw", "-o foo"), timeout_seconds=12)
        self.assertFalse(result.ready)
        self.assertEqual(result.detail, "managed mDNS takeover probe timed out")
        self.assertEqual(result.lines, ("FAIL:managed mDNS takeover probe timed out",))

    def test_probe_managed_runtime_polls_both_probes_and_rechecks_mdns_after_settle(self) -> None:
        smbd_ready = ManagedSmbdProbeResult(True, "managed smbd ready", ("PASS:managed smbd ready",))
        mdns_not_ready = ManagedMdnsTakeoverProbeResult(False, "managed mDNS takeover not active", ("FAIL:managed mDNS takeover not active",))
        mdns_ready = ManagedMdnsTakeoverProbeResult(True, "managed mDNS takeover active", ("PASS:managed mDNS takeover active",))
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
        smbd_timeout = ManagedSmbdProbeResult(False, "managed smbd readiness probe timed out", ("FAIL:managed smbd readiness probe timed out",))
        smbd_ready = ManagedSmbdProbeResult(True, "managed smbd ready", ("PASS:managed smbd ready",))
        mdns_ready = ManagedMdnsTakeoverProbeResult(True, "managed mDNS takeover active", ("PASS:managed mDNS takeover active",))
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
        smbd_timeout = ManagedSmbdProbeResult(False, "managed smbd readiness probe timed out", ("FAIL:managed smbd readiness probe timed out",))
        mdns_timeout = ManagedMdnsTakeoverProbeResult(False, "managed mDNS takeover probe timed out", ("FAIL:managed mDNS takeover probe timed out",))
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
        self.assertIn(f"diskd.useVolume wait: {DEFAULT_APPLE_MOUNT_WAIT_SECONDS}s", text)
        self.assertIn("tc_kill_watchdog_pids TERM", text)
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

    def test_netbsd4_activation_plan_contains_no_reboot_actions(self) -> None:
        paths = self._payload_home("/Volumes/dk2", "samba4")
        plan = build_deployment_plan(
            "root@10.0.0.2",
            paths,
            Path("bin/smbd"),
            Path("bin/mdns"),
            Path("bin/nbns"),
            activate_netbsd4=True,
        )
        self.assertFalse(plan.reboot_required)
        self.assertEqual(
            plan.activation_actions,
            [
                StopWatchdogAction(),
                StopProcessAction("smbd"),
                StopProcessAction("mdns-advertiser"),
                StopProcessAction("nbns-advertiser"),
                StopProcessAction("wcifsfs"),
                RunScriptAction("/mnt/Flash/rc.local"),
            ],
        )

        text = format_deployment_plan(plan)
        self.assertIn("Remote actions (NetBSD4 activation):", text)
        self.assertIn("tc_kill_watchdog_pids TERM", text)
        self.assertNotIn("/usr/bin/pkill -f '[w]atchdog.sh'", text)
        self.assertIn("/usr/bin/pkill '^smbd$' >/dev/null 2>&1 || true", text)
        self.assertIn("/usr/bin/pkill '^mdns-advertiser$' >/dev/null 2>&1 || true", text)
        self.assertIn("/usr/bin/pkill '^nbns-advertiser$' >/dev/null 2>&1 || true", text)
        self.assertIn("/usr/bin/pkill '^wcifsfs$' >/dev/null 2>&1 || true", text)
        self.assertIn("/bin/sh /mnt/Flash/rc.local", text)
        self.assertIn("Deploy will activate Samba immediately without rebooting.", text)
        self.assertIn("NetBSD 4 devices cannot auto-run Samba after a reboot.", text)
        self.assertIn("Run `activate` after a reboot if the device did not auto-start Samba.", text)
        self.assertIn("managed runtime smb.conf is present", text)
        self.assertIn("smbd is bound to TCP 445", text)
        self.assertIn("mdns-advertiser is bound to UDP 5353", text)

    def test_netbsd6_no_reboot_plan_has_no_reboot_checks(self) -> None:
        paths = self._payload_home("/Volumes/dk2", "samba4")
        plan = build_deployment_plan(
            "root@10.0.0.2",
            paths,
            Path("bin/smbd"),
            Path("bin/mdns"),
            Path("bin/nbns"),
            reboot_after_deploy=False,
        )
        self.assertFalse(plan.reboot_required)
        self.assertEqual(plan.post_deploy_checks, [])
        text = format_deployment_plan(plan)
        self.assertIn("Reboot:\n  no", text)
        self.assertIn("Post-deploy checks:\n  none", text)

    def test_build_uninstall_plan_stops_nbns_process(self) -> None:
        plan = build_uninstall_plan("root@10.0.0.2", ["/Volumes/dk2"], ["/Volumes/dk2/samba4"])
        rendered = [render_remote_action(action) for action in plan.remote_actions]
        self.assertTrue(any(command.startswith("/usr/bin/pkill '^nbns-advertiser$' >/dev/null 2>&1 || true;") for command in rendered))

    def test_build_uninstall_plan_stops_watchdog_first(self) -> None:
        plan = build_uninstall_plan("root@10.0.0.2", ["/Volumes/dk2"], ["/Volumes/dk2/samba4"])
        rendered = [render_remote_action(action) for action in plan.remote_actions]
        self.assertTrue(rendered[0].startswith("tc_watchdog_pids() { "))
        self.assertIn("tc_kill_watchdog_pids TERM", rendered[0])
        self.assertNotIn("/usr/bin/pkill -f '[w]atchdog.sh'", rendered[0])

    def test_build_uninstall_plan_removes_mdns_snapshots(self) -> None:
        plan = build_uninstall_plan("root@10.0.0.2", ["/Volumes/dk2"], ["/Volumes/dk2/samba4"])

        self.assertEqual(plan.flash_targets["allmdns.txt"], "/mnt/Flash/allmdns.txt")
        self.assertEqual(plan.flash_targets["applemdns.txt"], "/mnt/Flash/applemdns.txt")
        self.assertEqual(plan.flash_targets["tcapsulesmb.conf"], "/mnt/Flash/tcapsulesmb.conf")
        self.assertIn("/mnt/Flash/allmdns.txt", plan.verify_absent_targets)
        self.assertIn("/mnt/Flash/applemdns.txt", plan.verify_absent_targets)
        self.assertIn("/mnt/Flash/tcapsulesmb.conf", plan.verify_absent_targets)
        self.assertIn(RemovePathAction("/mnt/Flash/allmdns.txt"), plan.remote_actions)
        self.assertIn(RemovePathAction("/mnt/Flash/applemdns.txt"), plan.remote_actions)
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
            prepare_dirs_action(
                [payload_dir, f"{payload_dir}/private", f"{payload_dir}/cache"],
                [RemoteSymlink("/root/tc netbsd4", "/mnt/Memory/samba4")],
            )
        )
        permissions_cmd = render_remote_action(
            install_permissions_action(
                [
                    RemotePermission(f"{payload_dir}/cache", "755"),
                    RemotePermission(f"{payload_dir}/nbns-advertiser", "755"),
                    RemotePermission(f"{payload_dir}/private/smbpasswd", "600"),
                ]
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

    def test_collection_action_factories_normalize_to_tuples(self) -> None:
        self.assertEqual(
            prepare_dirs_action(["/payload"], [RemoteSymlink("/root/tc-netbsd7", "/mnt/Memory/samba4")]),
            PrepareDirsAction(("/payload",), (RemoteSymlink("/root/tc-netbsd7", "/mnt/Memory/samba4"),)),
        )
        self.assertEqual(
            install_permissions_action([RemotePermission("/payload/private", "700")]),
            InstallPermissionsAction((RemotePermission("/payload/private", "700"),)),
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
        self.assertEqual(plan.post_upload_actions[0], ensure_volume_mounted_action("/Volumes/dk2", "/dev/dk2", DEFAULT_APPLE_MOUNT_WAIT_SECONDS))
        self.assertIn(install_permissions_action(plan.permissions), plan.post_upload_actions)

    def test_deployment_plan_guards_each_payload_write_action(self) -> None:
        paths = self._payload_home("/Volumes/dk2", "samba4")
        plan = build_deployment_plan("host", paths, Path("bin/smbd"), Path("bin/mdns"), Path("bin/nbns"))
        expected_guard = EnsureVolumeMountedAction("/Volumes/dk2", "/dev/dk2", DEFAULT_APPLE_MOUNT_WAIT_SECONDS)

        self.assertEqual(plan.pre_upload_actions[4], expected_guard)
        self.assertEqual(plan.pre_upload_actions[6], expected_guard)
        self.assertEqual(plan.pre_upload_actions[8], expected_guard)
        self.assertEqual(plan.pre_upload_actions[10], expected_guard)
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
        def process_present(pattern: str, *, full: bool, ps_lines: list[str]) -> bool:
            command = render_process_present(pattern, full=full)
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

        self.assertFalse(process_present("wcifsnd", full=False, ps_lines=["Z    wcifsnd         (wcifsnd)"]))
        self.assertTrue(process_present("wcifsnd", full=False, ps_lines=["S    wcifsnd         wcifsnd"]))
        self.assertFalse(process_present("/mnt/Flash/watchdog.sh", full=True, ps_lines=["Z    sh              /bin/sh /mnt/Flash/watchdog.sh"]))
        self.assertTrue(process_present("/mnt/Flash/watchdog.sh", full=True, ps_lines=["S    sh              /bin/sh /mnt/Flash/watchdog.sh"]))
        self.assertFalse(
            process_present(
                "/mnt/Flash/watchdog.sh",
                full=True,
                ps_lines=[
                    "S    sh              /bin/sh -c probe=/mnt/Flash/watchdog.sh",
                    "S    sh              sh -c /bin/sh -c 'probe=/mnt/Flash/watchdog.sh'",
                ],
            )
        )

    def test_render_process_present_rejects_generic_full_substring_matches(self) -> None:
        with self.assertRaises(ValueError):
            render_process_present("smbd", full=True)
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
