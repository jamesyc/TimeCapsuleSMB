from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
import io
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock
import uuid


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from timecapsulesmb.deploy.auth import nt_hash_hex, render_smbpasswd
from timecapsulesmb.deploy.commands import (
    enable_nbns_action,
    initialize_data_root_action,
    install_permissions_action,
    prepare_dirs_action,
    remove_path_action,
    render_remote_action,
    run_script_action,
    stop_process_action,
    stop_process_full_action,
)
from timecapsulesmb.deploy.dry_run import format_deployment_plan
from timecapsulesmb.deploy.executor import (
    remote_enable_nbns,
    remote_ensure_adisk_uuid,
    remote_initialize_data_root,
    remote_install_permissions,
    remote_prepare_dirs,
    remote_uninstall_payload,
    upload_deployment_payload,
)
from timecapsulesmb.deploy.planner import build_deployment_plan, build_uninstall_plan
from timecapsulesmb.deploy.templates import (
    DEFAULT_APPLE_MOUNT_WAIT_SECONDS,
    build_template_bundle,
    cache_directory_replacements,
    load_boot_asset_text,
    render_template,
    render_template_text,
)
from timecapsulesmb.deploy.verify import (
    verify_managed_runtime,
)
from timecapsulesmb.device.probe import (
    ManagedMdnsTakeoverProbeResult,
    ManagedRuntimeProbeResult,
    ManagedSmbdProbeResult,
    build_device_paths,
    discover_mounted_volume_conn,
    discover_volume_root_conn,
    extract_airport_identity_from_acp_output,
    extract_airport_identity_from_text,
    probe_device_conn,
    probe_managed_runtime_conn,
    probe_managed_mdns_takeover_conn,
    probe_managed_smbd_conn,
    probe_remote_airport_identity_conn,
    wait_for_ssh_state_conn,
)
from timecapsulesmb.transport.ssh import SshConnection


class DeployModuleTests(unittest.TestCase):
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

    def test_build_template_bundle_contains_expected_keys(self) -> None:
        values = {
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_SHARE_NAME": "Data",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_NET_IFACE": "bridge0",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule",
            "TC_AIRPORT_SYAP": "119",
            "TC_SAMBA_USER": "admin",
        }
        bundle = build_template_bundle(values)
        self.assertIn("__SMB_SHARE_NAME__", bundle.start_script_replacements)
        self.assertIn("__SMB_SAMBA_USER__", bundle.smbconf_replacements)
        self.assertIn("__CACHE_DIRECTORY__", bundle.start_script_replacements)
        self.assertIn("__CACHE_DIRECTORY__", bundle.smbconf_replacements)
        self.assertIn("__SMBD_LOG_FILE__", bundle.smbconf_replacements)
        self.assertIn("__SMBD_MAX_LOG_SIZE__", bundle.smbconf_replacements)
        self.assertIn("__SMBD_LOG_LEVEL_LINE__", bundle.smbconf_replacements)
        self.assertIn("__MDNS_DEVICE_MODEL__", bundle.smbconf_replacements)
        self.assertEqual(bundle.start_script_replacements["__MDNS_DEVICE_MODEL__"], "TimeCapsule")
        self.assertEqual(bundle.smbconf_replacements["__MDNS_DEVICE_MODEL__"], "TimeCapsule")
        self.assertEqual(bundle.start_script_replacements["__AIRPORT_SYAP__"], "119")
        self.assertEqual(bundle.start_script_replacements["__ADISK_DISK_KEY__"], "dk0")
        self.assertEqual(bundle.start_script_replacements["__ADISK_UUID__"], "''")
        self.assertEqual(bundle.start_script_replacements["__SMBD_DISK_LOGGING_ENABLED__"], "0")

    def test_build_template_bundle_defaults_mdns_device_model(self) -> None:
        values = {
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_SHARE_NAME": "Data",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_NET_IFACE": "bridge0",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_SAMBA_USER": "admin",
        }
        bundle = build_template_bundle(values)
        self.assertEqual(bundle.start_script_replacements["__MDNS_DEVICE_MODEL__"], "TimeCapsule")
        self.assertEqual(bundle.start_script_replacements["__AIRPORT_SYAP__"], "''")

    def test_cache_directory_replacements_default_unknown_family_to_ram_cache(self) -> None:
        self.assertEqual(
            cache_directory_replacements("unknown_future_family", "samba4"),
            ("/mnt/Memory/samba4/var", "/mnt/Memory/samba4/var"),
        )

    def test_cache_directory_replacements_keep_netbsd4_start_expression_unquoted(self) -> None:
        start_cache, smbconf_cache = cache_directory_replacements("netbsd4le_samba4", "samba4")
        self.assertEqual(start_cache, "$PAYLOAD_DIR/cache")
        self.assertEqual(smbconf_cache, "__PAYLOAD_DIR__/cache")
        self.assertFalse(start_cache.startswith("'"))
        self.assertFalse(start_cache.endswith("'"))

    def test_cache_directory_replacements_use_custom_payload_dir_for_netbsd4_smbconf(self) -> None:
        start_cache, smbconf_cache = cache_directory_replacements("netbsd4le_samba4", "samba4-test")
        self.assertEqual(start_cache, "$PAYLOAD_DIR/cache")
        self.assertEqual(smbconf_cache, "__PAYLOAD_DIR__/cache")

    def test_build_deployment_plan_uses_device_paths(self) -> None:
        payload_dir_name = "samba4"
        paths = build_device_paths("/Volumes/dk2", payload_dir_name)
        plan = build_deployment_plan("root@10.0.0.2", paths, Path("bin/smbd"), Path("bin/mdns"), Path("bin/nbns"))
        payload_dir = f"/Volumes/dk2/{payload_dir_name}"
        self.assertEqual(plan.payload_dir, payload_dir)
        self.assertEqual(plan.private_dir, f"{payload_dir}/private")
        self.assertEqual(plan.volume_root, "/Volumes/dk2")
        self.assertEqual(plan.disk_key, "dk2")
        self.assertEqual(paths.data_root, "/Volumes/dk2/ShareRoot")
        self.assertEqual(paths.data_root_marker, "/Volumes/dk2/ShareRoot/.com.apple.timemachine.supported")
        self.assertEqual(plan.remote_directories[0], payload_dir)
        self.assertIn(f"{payload_dir}/cache", plan.remote_directories)
        self.assertEqual(plan.payload_targets["nbns-advertiser"], f"{payload_dir}/nbns-advertiser")
        self.assertEqual(
            plan.pre_upload_actions,
            [
                stop_process_full_action("[w]atchdog.sh"),
                stop_process_action("smbd"),
                stop_process_action("mdns-advertiser"),
                stop_process_action("nbns-advertiser"),
                initialize_data_root_action("/Volumes/dk2/ShareRoot", "/Volumes/dk2/ShareRoot/.com.apple.timemachine.supported"),
                prepare_dirs_action(payload_dir),
            ],
        )
        self.assertEqual(plan.post_auth_actions, [install_permissions_action(payload_dir)])

    def test_build_device_paths_uses_shareroot_when_disk_root_false(self) -> None:
        paths = build_device_paths("/Volumes/dk2", ".samba4", share_use_disk_root=False)
        self.assertEqual(paths.volume_root, "/Volumes/dk2")
        self.assertEqual(paths.payload_dir, "/Volumes/dk2/.samba4")
        self.assertEqual(paths.data_root, "/Volumes/dk2/ShareRoot")
        self.assertEqual(paths.data_root_marker, "/Volumes/dk2/ShareRoot/.com.apple.timemachine.supported")

    def test_build_device_paths_can_use_disk_root_as_share_path(self) -> None:
        paths = build_device_paths("/Volumes/dk2", ".samba4", share_use_disk_root=True)
        self.assertEqual(paths.volume_root, "/Volumes/dk2")
        self.assertEqual(paths.payload_dir, "/Volumes/dk2/.samba4")
        self.assertEqual(paths.data_root, "/Volumes/dk2")
        self.assertEqual(paths.data_root_marker, "/Volumes/dk2/.com.apple.timemachine.supported")

    def test_shareroot_deployment_plan_initializes_shareroot_marker_when_disk_root_false(self) -> None:
        paths = build_device_paths("/Volumes/dk2", ".samba4", share_use_disk_root=False)
        plan = build_deployment_plan("root@10.0.0.2", paths, Path("bin/smbd"), Path("bin/mdns"), Path("bin/nbns"))
        self.assertIn(
            initialize_data_root_action("/Volumes/dk2/ShareRoot", "/Volumes/dk2/ShareRoot/.com.apple.timemachine.supported"),
            plan.pre_upload_actions,
        )
        self.assertNotIn(
            initialize_data_root_action("/Volumes/dk2", "/Volumes/dk2/.com.apple.timemachine.supported"),
            plan.pre_upload_actions,
        )

    def test_disk_root_deployment_plan_initializes_volume_root_marker(self) -> None:
        paths = build_device_paths("/Volumes/dk2", ".samba4", share_use_disk_root=True)
        plan = build_deployment_plan("root@10.0.0.2", paths, Path("bin/smbd"), Path("bin/mdns"), Path("bin/nbns"))
        self.assertEqual(plan.payload_dir, "/Volumes/dk2/.samba4")
        self.assertIn(
            initialize_data_root_action("/Volumes/dk2", "/Volumes/dk2/.com.apple.timemachine.supported"),
            plan.pre_upload_actions,
        )
        self.assertNotIn(
            initialize_data_root_action("/Volumes/dk2/ShareRoot", "/Volumes/dk2/ShareRoot/.com.apple.timemachine.supported"),
            plan.pre_upload_actions,
        )

    def test_render_template_text_replaces_tokens(self) -> None:
        self.assertEqual(render_template_text("hello __TOKEN__", {"__TOKEN__": "world"}), "hello world")

    def test_load_boot_asset_text_reads_packaged_asset(self) -> None:
        content = load_boot_asset_text("rc.local")
        self.assertIn("/mnt/Flash/start-samba.sh", content)
        common = load_boot_asset_text("common.sh")
        self.assertIn("get_airport_syvs()", common)
        self.assertIn("ether[[:space:]]", common)
        self.assertIn("address[[:space:]]", common)
        self.assertNotIn("tr '[:lower:]' '[:upper:]'", common)

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
        self.assertNotIn("get_radio_mac()", start)
        self.assertNotIn("get_airport_srcv()", start)
        self.assertNotIn("get_airport_syvs()", start)
        self.assertNotIn("wait_for_process()", start)
        self.assertNotIn("wait_for_smbd_ready()", start)
        self.assertNotIn("get_radio_mac()", watchdog)
        self.assertNotIn("get_airport_srcv()", watchdog)
        self.assertNotIn("get_airport_syvs()", watchdog)

    def test_rc_local_scopes_watchdog_errexit_workaround_around_probe_block(self) -> None:
        content = load_boot_asset_text("rc.local")
        self.assertIn("set +e\nif /usr/bin/pkill -0 -f /mnt/Flash/watchdog.sh", content)
        watchdog_probe_index = content.index("/usr/bin/pkill -0 -f /mnt/Flash/watchdog.sh")
        self.assertLess(content.index("set +e"), watchdog_probe_index)
        self.assertIn("set -e", content[watchdog_probe_index:])

    def test_rc_local_detaches_background_jobs_from_stdin(self) -> None:
        content = load_boot_asset_text("rc.local")
        self.assertIn("/mnt/Flash/start-samba.sh </dev/null >/dev/null 2>&1 &", content)
        self.assertIn("/mnt/Flash/watchdog.sh 1200 </dev/null >/dev/null 2>&1 &", content)

    def test_watchdog_accepts_initial_sleep_argument(self) -> None:
        content = load_boot_asset_text("watchdog.sh")
        self.assertIn("INITIAL_STARTUP_DELAY_SECONDS=${1:-30}", content)
        self.assertIn('log "watchdog startup beginning; initial recovery delay ${INITIAL_STARTUP_DELAY_SECONDS}s"', content)

    def test_render_start_script_includes_device_model_flag(self) -> None:
        values = {
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_SHARE_NAME": "Data",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_NET_IFACE": "bridge0",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "AirPortTimeCapsule",
            "TC_AIRPORT_SYAP": "119",
            "TC_SAMBA_USER": "admin",
        }
        bundle = build_template_bundle(values)
        rendered = render_template("start-samba.sh", bundle.start_script_replacements)
        self.assertIn('MDNS_DEVICE_MODEL=AirPortTimeCapsule', rendered)
        self.assertIn("AIRPORT_SYAP=119", rendered)
        self.assertIn('--device-model "$MDNS_DEVICE_MODEL"', rendered)
        self.assertIn('. /mnt/Flash/common.sh', rendered)
        self.assertIn('--airport-wama "$AIRPORT_WAMA"', rendered)
        self.assertIn('--airport-rama "$AIRPORT_RAMA"', rendered)
        self.assertIn('--airport-ram2 "$AIRPORT_RAM2"', rendered)
        self.assertIn('--airport-syap "$AIRPORT_SYAP"', rendered)
        self.assertIn('--airport-syvs "$AIRPORT_SYVS"', rendered)
        self.assertIn('--airport-srcv "$AIRPORT_SRCV"', rendered)
        self.assertIn('airport clone fields incomplete; skipping _airport._tcp advertisement', rendered)
        self.assertIn('ADISK_DISK_KEY=dk0', rendered)
        self.assertIn("ADISK_UUID=''", rendered)
        self.assertIn('--adisk-disk-key "$ADISK_DISK_KEY"', rendered)
        self.assertIn('--adisk-uuid "$ADISK_UUID"', rendered)
        self.assertIn('--adisk-sys-wama "$iface_mac"', rendered)
        self.assertIn('MDNS_CAPTURE_PID=', rendered)
        self.assertNotIn('IFACE_MAC=', rendered)
        self.assertIn('--save-all-snapshot "$ALL_MDNS_SNAPSHOT"', rendered)
        self.assertIn('--save-snapshot "$APPLE_MDNS_SNAPSHOT"', rendered)
        self.assertIn('--load-snapshot "$APPLE_MDNS_SNAPSHOT"', rendered)
        self.assertIn('/usr/bin/pkill -f /mnt/Flash/watchdog.sh >/dev/null 2>&1 || true', rendered)
        self.assertIn('/usr/bin/pkill "$MDNS_PROC_NAME" >/dev/null 2>&1 || true', rendered)
        self.assertIn('/usr/bin/pkill "$NBNS_PROC_NAME" >/dev/null 2>&1 || true', rendered)
        self.assertIn('if [ -f "$payload_dir/private/nbns.enabled" ]', rendered)
        self.assertIn('cp "$nbns_src" "$RAM_SBIN/nbns-advertiser"', rendered)
        self.assertIn('"$RAM_SBIN/nbns-advertiser" \\', rendered)
        self.assertIn("CACHE_DIRECTORY=/mnt/Memory/samba4/var", rendered)
        self.assertIn("cache directory = $CACHE_DIRECTORY", rendered)
        self.assertLess(rendered.rindex("start_mdns_capture"), rendered.index('waiting up to ${APPLE_MOUNT_WAIT_SECONDS}s for Apple-mounted data volume'))
        self.assertLess(rendered.rindex("start_smbd ||"), rendered.rindex("start_mdns_advertiser"))
        self.assertLess(rendered.rindex("start_mdns_advertiser"), rendered.rindex("start_nbns"))

    def test_render_start_script_splits_mdns_capture_from_advertising(self) -> None:
        values = {
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_SHARE_NAME": "Data",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_NET_IFACE": "bridge0",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "AirPortTimeCapsule",
            "TC_AIRPORT_SYAP": "119",
            "TC_SAMBA_USER": "admin",
        }
        bundle = build_template_bundle(values)
        rendered = render_template("start-samba.sh", bundle.start_script_replacements)
        capture_start = rendered.index("start_mdns_capture()")
        capture_end = rendered.index("wait_for_mdns_capture()")
        capture_section = rendered[capture_start:capture_end]
        advertise_start = rendered.index("start_mdns_advertiser()")
        advertise_end = rendered.index("start_nbns()")
        advertise_section = rendered[advertise_start:advertise_end]
        self.assertIn('--save-all-snapshot "$ALL_MDNS_SNAPSHOT"', capture_section)
        self.assertIn('--save-snapshot "$APPLE_MDNS_SNAPSHOT"', capture_section)
        self.assertNotIn('--load-snapshot "$APPLE_MDNS_SNAPSHOT"', capture_section)
        self.assertNotIn('--instance "$MDNS_INSTANCE_NAME"', capture_section)
        self.assertNotIn('--host "$MDNS_HOST_LABEL"', capture_section)
        self.assertNotIn('--ipv4 "$BRIDGE0_IP"', capture_section)
        self.assertIn('--load-snapshot "$APPLE_MDNS_SNAPSHOT"', advertise_section)
        self.assertIn('--instance "$MDNS_INSTANCE_NAME"', advertise_section)
        self.assertIn('--host "$MDNS_HOST_LABEL"', advertise_section)
        self.assertIn('--ipv4 "$BRIDGE0_IP"', advertise_section)
        self.assertIn("wait_for_mdns_capture", advertise_section)
        wait_start = rendered.index("wait_for_mdns_capture()")
        wait_end = rendered.index("start_mdns_advertiser()")
        wait_section = rendered[wait_start:wait_end]
        self.assertIn('if ! kill -0 "$MDNS_CAPTURE_PID" >/dev/null 2>&1; then', wait_section)
        self.assertIn('if [ -f "$APPLE_MDNS_SNAPSHOT" ]; then', wait_section)
        self.assertIn('log "mDNS snapshot capture already finished; trusted snapshot is available"', wait_section)
        self.assertIn('log "mDNS snapshot capture already finished before wait; no trusted snapshot is available"', wait_section)
        self.assertNotIn("log_mdns_snapshot_age", wait_section)
        self.assertIn("log_mdns_snapshot_age()", rendered)
        self.assertIn('APPLE_MDNS_SNAPSHOT_START=$(/bin/ls -lnT "$APPLE_MDNS_SNAPSHOT" 2>/dev/null || true)', rendered)
        self.assertIn('snapshot_current=$(/bin/ls -lnT "$snapshot_path" 2>/dev/null || true)', rendered)
        self.assertIn('if [ -z "$APPLE_MDNS_SNAPSHOT_START" ]; then', rendered)
        self.assertIn('elif [ "$snapshot_current" != "$APPLE_MDNS_SNAPSHOT_START" ]; then', rendered)
        self.assertIn('trusted Apple mDNS snapshot was created during this boot run: $snapshot_path', rendered)
        self.assertIn('trusted Apple mDNS snapshot was updated during this boot run: $snapshot_path', rendered)
        self.assertIn('trusted Apple mDNS snapshot predates this boot run; accepting stale snapshot: $snapshot_path', rendered)
        self.assertNotIn("file_mtime_key()", rendered)
        self.assertNotIn("START_KEY", rendered)
        self.assertNotIn("START_MARKER", rendered)
        self.assertNotIn("trap remove_start_marker", rendered)
        self.assertIn("lock directory = $LOCKS_ROOT", rendered)
        self.assertIn("state directory = $RAM_VAR", rendered)
        self.assertIn("prepare_locks_ramdisk()", rendered)
        self.assertIn('if locks_root_is_mounted; then', rendered)
        self.assertIn('rm -rf "$LOCKS_ROOT"/* >/dev/null 2>&1 || true', rendered)
        self.assertIn('/sbin/mount_tmpfs -s 6m tmpfs "$LOCKS_ROOT" >/dev/null 2>&1', rendered)
        self.assertIn('/sbin/mount_mfs -s 12288 swap "$LOCKS_ROOT" >/dev/null 2>&1', rendered)
        self.assertIn('log "mounted $LOCKS_ROOT tmpfs for Samba lock directory"', rendered)
        self.assertIn('log "failed to mount $LOCKS_ROOT tmpfs; using plain directory fallback"', rendered)
        self.assertIn('log "mounted $LOCKS_ROOT mfs for Samba lock directory"', rendered)
        self.assertIn('log "failed to mount $LOCKS_ROOT mfs; refusing rootfs fallback"', rendered)
        self.assertIn('log "aborting startup because $LOCKS_ROOT is unavailable"', rendered)
        self.assertNotIn('cp "$nbns_src" "$RAM_SBIN/nbns-advertiser"\n    chmod 755 "$RAM_SBIN/nbns-advertiser"', rendered)
        self.assertIn("discover_preexisting_data_root()", rendered)
        self.assertIn("mount_fallback_volume()", rendered)
        self.assertIn("resolve_data_root_on_mounted_volume()", rendered)
        self.assertIn('if DATA_ROOT=$(discover_preexisting_data_root); then', rendered)
        self.assertIn('VOLUME_ROOT=$(mount_fallback_volume) || {', rendered)
        self.assertIn('DATA_ROOT=$(resolve_data_root_on_mounted_volume "$VOLUME_ROOT") || {', rendered)
        self.assertIn("disk_name_candidates()", rendered)
        self.assertIn("volume_root_candidates()", rendered)
        self.assertIn("mount_candidates()", rendered)
        self.assertIn("APdata", rendered)
        self.assertIn("APconfig", rendered)
        self.assertIn("APswap", rendered)
        self.assertIn("/sbin/sysctl -n hw.disknames", rendered)
        self.assertIn("log_disk_discovery_state()", rendered)
        self.assertIn('log "disk discovery: hw.disknames=$disk_names"', rendered)
        self.assertIn('log "disk discovery: dmesg: $disk_line"', rendered)
        self.assertIn('log "disk discovery: disk candidates:${disk_candidates:- none}"', rendered)
        self.assertIn('log "disk discovery: volume root candidates:${volume_candidates:- none}"', rendered)
        self.assertIn('log "disk discovery: mount candidates:${mount_candidate_list:- none}"', rendered)
        self.assertIn('for mount_candidate in $(mount_candidates); do', rendered)
        self.assertIn('try_mount_candidate "$dev_path" "$volume_root"', rendered)
        self.assertIn('resolve_data_root_on_mounted_volume "$VOLUME_ROOT"', rendered)
        self.assertIn('/bin/df -k "$volume_root"', rendered)
        self.assertIn('df_line=$(/bin/df -k "$volume_root" 2>/dev/null | /usr/bin/tail -n +2 || true)', rendered)
        self.assertIn('case "$df_line" in', rendered)
        self.assertIn('*" $volume_root")', rendered)
        self.assertIn('if is_volume_root_mounted "$volume_root"; then', rendered)
        self.assertIn('initialize_data_root_under_volume "$volume_root"', rendered)
        self.assertIn(': >"$marker"', rendered)
        self.assertIn('log "disk discovery: waiting up to ${APPLE_MOUNT_WAIT_SECONDS}s for Apple-mounted data volume before manual mount fallback"', rendered)
        self.assertIn('log "no Apple-mounted usable volume found; falling back to manual mount"', rendered)
        self.assertIn('log "data root resolved after manual mount: $DATA_ROOT"', rendered)
        self.assertIn('log "starting nbns responder for $SMB_NETBIOS_NAME at $BRIDGE0_IP"', rendered)
        self.assertNotIn("wait_for_process()", rendered)
        self.assertNotIn("wait_for_smbd_ready()", rendered)

    def test_render_start_script_waits_for_existing_mounts_before_manual_mount(self) -> None:
        values = {
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_SHARE_NAME": "Data",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_NET_IFACE": "bridge0",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "AirPortTimeCapsule",
            "TC_SAMBA_USER": "admin",
        }
        bundle = build_template_bundle(values)
        rendered = render_template("start-samba.sh", bundle.start_script_replacements)
        main_start = rendered.index("cleanup_old_runtime")
        main_section = rendered[main_start:]
        self.assertLess(main_section.index("prepare_locks_ramdisk"), main_section.index("prepare_ram_root"))
        self.assertLess(main_section.index("log_disk_discovery_state"), main_section.index("start_mdns_capture"))
        self.assertLess(main_section.index('start_mdns'), main_section.index('waiting up to ${APPLE_MOUNT_WAIT_SECONDS}s for Apple-mounted data volume'))
        self.assertLess(main_section.index('waiting up to ${APPLE_MOUNT_WAIT_SECONDS}s for Apple-mounted data volume'), main_section.index('if DATA_ROOT=$(discover_preexisting_data_root); then'))
        self.assertLess(main_section.index('if DATA_ROOT=$(discover_preexisting_data_root); then'), main_section.index('VOLUME_ROOT=$(mount_fallback_volume) || {'))
        self.assertLess(main_section.index('log "smbd startup complete: process observed"'), main_section.rindex('start_mdns_advertiser'))
        self.assertLess(main_section.rindex('start_mdns_advertiser'), main_section.rindex('start_nbns'))
        discover_body = rendered[rendered.index("discover_preexisting_data_root()"):rendered.index("resolve_data_root_on_mounted_volume()")]
        self.assertIn('wait_for_existing_mount_target "data root" find_existing_data_root', discover_body)
        wait_section = rendered[rendered.index("wait_for_existing_mount_target()"):rendered.index("try_mount_candidate()")]
        self.assertIn(f"APPLE_MOUNT_WAIT_SECONDS={DEFAULT_APPLE_MOUNT_WAIT_SECONDS}", rendered)
        self.assertIn('while [ "$attempt" -lt "$APPLE_MOUNT_WAIT_SECONDS" ]; do', wait_section)
        self.assertIn('if target=$($finder); then', wait_section)
        self.assertIn('log "$target_name was mounted after ${attempt}s"', wait_section)
        self.assertIn('log "$target_name was not mounted after ${attempt}s"', wait_section)
        self.assertIn('wait_for_existing_mount_target "data root" find_existing_data_root', rendered)
        self.assertIn('wait_for_existing_mount_target "disk root" find_existing_volume_root', rendered)
        self.assertNotIn("wait_for_existing_data_root()", rendered)
        self.assertNotIn("wait_for_existing_volume_root()", rendered)

    def test_render_start_script_custom_disk_delay_extends_apple_mount_wait(self) -> None:
        values = {
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_SHARE_NAME": "Data",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_NET_IFACE": "bridge0",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "AirPortTimeCapsule",
            "TC_SAMBA_USER": "admin",
        }
        bundle = build_template_bundle(values, apple_mount_wait_seconds=123)
        rendered = render_template("start-samba.sh", bundle.start_script_replacements)
        self.assertIn("APPLE_MOUNT_WAIT_SECONDS=123", rendered)

    def test_render_start_script_starts_final_mdns_after_smbd_process_observed(self) -> None:
        values = {
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_SHARE_NAME": "Data",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_NET_IFACE": "bridge0",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "AirPortTimeCapsule",
            "TC_SAMBA_USER": "admin",
        }
        bundle = build_template_bundle(values)
        rendered = render_template("start-samba.sh", bundle.start_script_replacements)
        main_section = rendered[rendered.index("\ncleanup_old_runtime\nif ! prepare_locks_ramdisk; then"):]
        smbd_start = main_section.index("start_smbd || {")
        smbd_ready = main_section.index('log "smbd startup complete: process observed"')
        final_mdns = main_section.index("start_mdns_advertiser")
        self.assertLess(smbd_start, smbd_ready)
        self.assertLess(smbd_ready, final_mdns)

    def test_render_start_script_starts_nbns_after_smbd_process_observed(self) -> None:
        values = {
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_SHARE_NAME": "Data",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_NET_IFACE": "bridge0",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "AirPortTimeCapsule",
            "TC_SAMBA_USER": "admin",
        }
        bundle = build_template_bundle(values)
        rendered = render_template("start-samba.sh", bundle.start_script_replacements)
        main_section = rendered[rendered.index("\ncleanup_old_runtime\nif ! prepare_locks_ramdisk; then"):]
        smbd_ready = main_section.index('log "smbd startup complete: process observed"')
        nbns_start = main_section.index("start_nbns")
        self.assertLess(smbd_ready, nbns_start)

    def test_render_start_script_separates_mount_fallback_from_data_root_recovery(self) -> None:
        values = {
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_SHARE_NAME": "Data",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_NET_IFACE": "bridge0",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "AirPortTimeCapsule",
            "TC_SAMBA_USER": "admin",
        }
        bundle = build_template_bundle(values)
        rendered = render_template("start-samba.sh", bundle.start_script_replacements)
        self.assertIn("mount_fallback_volume()", rendered)
        self.assertIn("resolve_data_root_on_mounted_volume()", rendered)
        main_start = rendered.index("cleanup_old_runtime")
        main_section = rendered[main_start:]
        self.assertLess(main_section.index('VOLUME_ROOT=$(mount_fallback_volume) || {'), main_section.index('DATA_ROOT=$(resolve_data_root_on_mounted_volume "$VOLUME_ROOT") || {'))

    def test_render_start_script_does_not_initialize_share_root_in_pre_mount_phase(self) -> None:
        values = {
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_SHARE_NAME": "Data",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_NET_IFACE": "bridge0",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "AirPortTimeCapsule",
            "TC_SAMBA_USER": "admin",
        }
        bundle = build_template_bundle(values)
        rendered = render_template("start-samba.sh", bundle.start_script_replacements)
        pre_mount_start = rendered.index("discover_preexisting_data_root()")
        pre_mount_end = rendered.index("resolve_data_root_on_mounted_volume()")
        pre_mount_section = rendered[pre_mount_start:pre_mount_end]
        self.assertNotIn("initialize_data_root_under_volume", pre_mount_section)

    def test_render_start_script_only_accepts_existing_data_root_from_mounted_volumes(self) -> None:
        values = {
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_SHARE_NAME": "Data",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_NET_IFACE": "bridge0",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "AirPortTimeCapsule",
            "TC_SAMBA_USER": "admin",
        }
        bundle = build_template_bundle(values)
        rendered = render_template("start-samba.sh", bundle.start_script_replacements)
        start = rendered.index("find_existing_data_root() {")
        end = rendered.index("\nfind_existing_volume_root() {")
        section = rendered[start:end]
        self.assertIn('for volume_root in $(volume_root_candidates); do', section)
        self.assertIn('is_volume_root_mounted "$volume_root"', section)
        self.assertIn('data_root=$(find_data_root_under_volume "$volume_root")', section)
        self.assertNotIn("VOLUME_ROOT_CANDIDATES=", rendered)
        self.assertNotIn("mount_hfs", section)

    def test_render_start_script_initializes_share_root_only_after_confirmed_mount(self) -> None:
        values = {
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_SHARE_NAME": "Data",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_NET_IFACE": "bridge0",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "AirPortTimeCapsule",
            "TC_SAMBA_USER": "admin",
        }
        bundle = build_template_bundle(values)
        rendered = render_template("start-samba.sh", bundle.start_script_replacements)
        mounted_check = rendered.index('if is_volume_root_mounted "$volume_root"; then')
        initialize = rendered.index('initialize_data_root_under_volume "$volume_root"')
        self.assertLess(mounted_check, initialize)

    def test_render_start_script_prefers_existing_share_root_marker_before_plain_dir(self) -> None:
        values = {
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_SHARE_NAME": "Data",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_NET_IFACE": "bridge0",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "AirPortTimeCapsule",
            "TC_SAMBA_USER": "admin",
        }
        bundle = build_template_bundle(values)
        rendered = render_template("start-samba.sh", bundle.start_script_replacements)
        share_marker = rendered.index('if [ -f "$volume_root/ShareRoot/.com.apple.timemachine.supported" ]; then')
        shared_marker = rendered.index('if [ -f "$volume_root/Shared/.com.apple.timemachine.supported" ]; then')
        share_dir = rendered.index('if [ -d "$volume_root/ShareRoot" ]; then')
        shared_dir = rendered.index('if [ -d "$volume_root/Shared" ]; then')
        self.assertLess(share_marker, shared_marker)
        self.assertLess(shared_marker, share_dir)
        self.assertLess(share_dir, shared_dir)

    def test_render_start_script_discovers_mount_candidates_from_remote_disk_names(self) -> None:
        values = {
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_SHARE_NAME": "Data",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_NET_IFACE": "bridge0",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "AirPortTimeCapsule",
            "TC_SAMBA_USER": "admin",
        }
        bundle = build_template_bundle(values)
        rendered = render_template("start-samba.sh", bundle.start_script_replacements)
        mount_start = rendered.index("mount_fallback_volume()")
        mount_section = rendered[mount_start:rendered.index("\nfind_payload_dir()")]
        self.assertIn('for mount_candidate in $(mount_candidates); do', mount_section)
        self.assertIn('dev_path=${mount_candidate%%:*}', mount_section)
        self.assertIn('volume_root=${mount_candidate#*:}', mount_section)
        self.assertIn('try_mount_candidate "$dev_path" "$volume_root"', mount_section)

    def test_render_start_script_logs_mount_fallback_transition(self) -> None:
        values = {
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_SHARE_NAME": "Data",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_NET_IFACE": "bridge0",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "AirPortTimeCapsule",
            "TC_SAMBA_USER": "admin",
        }
        bundle = build_template_bundle(values)
        rendered = render_template("start-samba.sh", bundle.start_script_replacements)
        self.assertIn('log "no Apple-mounted usable volume found; falling back to manual mount"', rendered)

    def test_render_start_script_logs_discovered_data_root_after_mount(self) -> None:
        values = {
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_SHARE_NAME": "Data",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_NET_IFACE": "bridge0",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "AirPortTimeCapsule",
            "TC_SAMBA_USER": "admin",
        }
        bundle = build_template_bundle(values)
        rendered = render_template("start-samba.sh", bundle.start_script_replacements)
        self.assertIn('log "data root resolved after manual mount: $DATA_ROOT"', rendered)

    def test_render_start_script_logs_share_root_initialization(self) -> None:
        values = {
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_SHARE_NAME": "Data",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_NET_IFACE": "bridge0",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "AirPortTimeCapsule",
            "TC_SAMBA_USER": "admin",
        }
        bundle = build_template_bundle(values)
        rendered = render_template("start-samba.sh", bundle.start_script_replacements)
        self.assertIn('log "initialized ShareRoot under $volume_root"', rendered)

    def test_render_start_script_logs_found_apple_mounted_data_root(self) -> None:
        values = {
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_SHARE_NAME": "Data",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_NET_IFACE": "bridge0",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "AirPortTimeCapsule",
            "TC_SAMBA_USER": "admin",
        }
        bundle = build_template_bundle(values)
        rendered = render_template("start-samba.sh", bundle.start_script_replacements)
        self.assertIn('log "found Apple-mounted data root: $data_root"', rendered)

    def test_render_start_script_waits_longer_for_bind_interface(self) -> None:
        values = {
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_SHARE_NAME": "Data",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_NET_IFACE": "bridge0",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "AirPortTimeCapsule",
            "TC_SAMBA_USER": "admin",
        }
        bundle = build_template_bundle(values)
        rendered = render_template("start-samba.sh", bundle.start_script_replacements)
        self.assertIn('while [ "$attempt" -lt 60 ]; do', rendered)

    def test_render_start_script_logs_mdns_skip_when_mac_missing(self) -> None:
        values = {
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_SHARE_NAME": "Data",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_NET_IFACE": "bridge0",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "AirPortTimeCapsule",
            "TC_SAMBA_USER": "admin",
        }
        bundle = build_template_bundle(values)
        rendered = render_template("start-samba.sh", bundle.start_script_replacements)
        self.assertIn('log "mdns skipped; missing $NET_IFACE MAC address"', rendered)
        self.assertIn('log "mdns advertiser failed to stay running"', rendered)

    def test_render_start_script_logs_nbns_skip_when_marker_missing(self) -> None:
        values = {
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_SHARE_NAME": "Data",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_NET_IFACE": "bridge0",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "AirPortTimeCapsule",
            "TC_SAMBA_USER": "admin",
        }
        bundle = build_template_bundle(values)
        rendered = render_template("start-samba.sh", bundle.start_script_replacements)
        self.assertIn('log "nbns responder skipped; marker missing"', rendered)

    def test_render_start_script_logs_nbns_skip_when_runtime_binary_missing(self) -> None:
        values = {
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_SHARE_NAME": "Data",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_NET_IFACE": "bridge0",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "AirPortTimeCapsule",
            "TC_SAMBA_USER": "admin",
        }
        bundle = build_template_bundle(values)
        rendered = render_template("start-samba.sh", bundle.start_script_replacements)
        self.assertIn('log "nbns responder launch skipped; missing runtime binary"', rendered)
        self.assertIn('log "nbns responder failed to stay running"', rendered)

    def test_render_start_script_stops_apple_cifs_before_nbns_launch(self) -> None:
        values = {
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_SHARE_NAME": "Data",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_NET_IFACE": "bridge0",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "AirPortTimeCapsule",
            "TC_SAMBA_USER": "admin",
        }
        bundle = build_template_bundle(values)
        rendered = render_template("start-samba.sh", bundle.start_script_replacements)
        nbns_start = rendered.index("start_nbns()")
        nbns_end = rendered.index("\ncleanup_old_runtime\nif ! prepare_locks_ramdisk; then")
        nbns_section = rendered[nbns_start:nbns_end]
        self.assertIn('/usr/bin/pkill wcifsnd >/dev/null 2>&1 || true', nbns_section)
        self.assertIn('/usr/bin/pkill wcifsfs >/dev/null 2>&1 || true', nbns_section)
        self.assertLess(nbns_section.index('/usr/bin/pkill wcifsnd >/dev/null 2>&1 || true'), nbns_section.index('"$RAM_SBIN/nbns-advertiser" \\'))
        self.assertLess(nbns_section.index('/usr/bin/pkill wcifsfs >/dev/null 2>&1 || true'), nbns_section.index('"$RAM_SBIN/nbns-advertiser" \\'))

    def test_render_start_script_logs_payload_and_smbd_failures(self) -> None:
        values = {
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_SHARE_NAME": "Data",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_NET_IFACE": "bridge0",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "AirPortTimeCapsule",
            "TC_SAMBA_USER": "admin",
        }
        bundle = build_template_bundle(values)
        rendered = render_template("start-samba.sh", bundle.start_script_replacements)
        self.assertIn('log "payload discovery failed: missing payload directory under mounted volume"', rendered)
        self.assertIn('log "payload discovery failed: missing smbd binary in $PAYLOAD_DIR"', rendered)
        self.assertIn('log "smbd startup failed: process was not observed"', rendered)

    def test_render_start_script_prefers_root_level_payload_dir_with_share_root_fallback(self) -> None:
        values = {
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_SHARE_NAME": "Data",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_NET_IFACE": "bridge0",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "AirPortTimeCapsule",
            "TC_SAMBA_USER": "admin",
        }
        bundle = build_template_bundle(values)
        rendered = render_template("start-samba.sh", bundle.start_script_replacements)
        payload_start = rendered.index("find_payload_dir()")
        payload_end = rendered.index("find_payload_smbd()")
        payload_section = rendered[payload_start:payload_end]
        self.assertIn('volume_root=${data_root%/*}', payload_section)
        self.assertIn('payload_dir="$volume_root/$PAYLOAD_DIR_NAME"', payload_section)
        self.assertIn('payload_dir="$data_root/$PAYLOAD_DIR_NAME"', payload_section)
        self.assertLess(
            payload_section.index('payload_dir="$volume_root/$PAYLOAD_DIR_NAME"'),
            payload_section.index('payload_dir="$data_root/$PAYLOAD_DIR_NAME"'),
        )

    def test_render_start_script_fallback_smb_conf_uses_root_level_payload_private_paths(self) -> None:
        values = {
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_SHARE_NAME": "Data",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_NET_IFACE": "bridge0",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "AirPortTimeCapsule",
            "TC_SAMBA_USER": "admin",
        }
        bundle = build_template_bundle(values)
        rendered = render_template("start-samba.sh", bundle.start_script_replacements)
        self.assertIn("passdb backend = smbpasswd:$PAYLOAD_DIR/private/smbpasswd", rendered)
        self.assertIn("username map = $PAYLOAD_DIR/private/username.map", rendered)
        self.assertIn("xattr_tdb:file = $PAYLOAD_DIR/private/xattr.tdb", rendered)
        self.assertIn("create mask = 0666", rendered)
        self.assertIn("directory mask = 0777", rendered)
        self.assertIn("force create mode = 0666", rendered)
        self.assertIn("force directory mode = 0777", rendered)

    def test_render_start_script_logs_mount_and_recovery_failures(self) -> None:
        values = {
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_SHARE_NAME": "Data",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_NET_IFACE": "bridge0",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "AirPortTimeCapsule",
            "TC_SAMBA_USER": "admin",
        }
        bundle = build_template_bundle(values)
        rendered = render_template("start-samba.sh", bundle.start_script_replacements)
        self.assertIn('log "disk discovery failed: no fallback data volume mounted"', rendered)
        self.assertIn('log "data root resolution failed on mounted volume $VOLUME_ROOT"', rendered)

    def test_render_start_script_mount_phase_does_not_initialize_share_root(self) -> None:
        values = {
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_SHARE_NAME": "Data",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_NET_IFACE": "bridge0",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "AirPortTimeCapsule",
            "TC_SAMBA_USER": "admin",
        }
        bundle = build_template_bundle(values)
        rendered = render_template("start-samba.sh", bundle.start_script_replacements)
        mount_start = rendered.index("mount_fallback_volume()")
        mount_end = rendered.index("find_payload_dir()")
        mount_section = rendered[mount_start:mount_end]
        self.assertNotIn("initialize_data_root_under_volume", mount_section)

    def test_render_start_script_recovery_phase_does_not_call_mount_hfs(self) -> None:
        values = {
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_SHARE_NAME": "Data",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_NET_IFACE": "bridge0",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "AirPortTimeCapsule",
            "TC_SAMBA_USER": "admin",
        }
        bundle = build_template_bundle(values)
        rendered = render_template("start-samba.sh", bundle.start_script_replacements)
        recovery_start = rendered.index("resolve_data_root_on_mounted_volume()")
        recovery_end = rendered.index("wait_for_existing_mount_target()")
        recovery_section = rendered[recovery_start:recovery_end]
        self.assertNotIn("mount_hfs", recovery_section)

    def test_render_start_script_try_mount_candidate_confirms_mount_after_attempt(self) -> None:
        values = {
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_SHARE_NAME": "Data",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_NET_IFACE": "bridge0",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "AirPortTimeCapsule",
            "TC_SAMBA_USER": "admin",
        }
        bundle = build_template_bundle(values)
        rendered = render_template("start-samba.sh", bundle.start_script_replacements)
        candidate_start = rendered.index("try_mount_candidate()")
        candidate_end = rendered.index("mount_fallback_volume()")
        candidate_section = rendered[candidate_start:candidate_end]
        self.assertIn('if is_volume_root_mounted "$volume_root"; then', candidate_section)
        self.assertIn('mount_device_if_possible "$dev_path" "$volume_root" || true', candidate_section)
        self.assertLess(candidate_section.index('if is_volume_root_mounted "$volume_root"; then'), candidate_section.index('mount_device_if_possible "$dev_path" "$volume_root" || true'))

    def test_render_start_script_bounds_mount_hfs_with_timeout(self) -> None:
        values = {
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_SHARE_NAME": "Data",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_NET_IFACE": "bridge0",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "AirPortTimeCapsule",
            "TC_SAMBA_USER": "admin",
        }
        bundle = build_template_bundle(values)
        rendered = render_template("start-samba.sh", bundle.start_script_replacements)
        mount_start = rendered.index("mount_device_if_possible()")
        mount_end = rendered.index("discover_preexisting_data_root()")
        mount_section = rendered[mount_start:mount_end]
        self.assertIn('/sbin/mount_hfs "$dev_path" "$volume_root" >/dev/null 2>&1 &', mount_section)
        self.assertIn("mount_pid=$!", mount_section)
        self.assertIn('while kill -0 "$mount_pid" >/dev/null 2>&1; do', mount_section)
        self.assertIn('if [ "$attempt" -ge 30 ]; then', mount_section)
        self.assertIn('kill "$mount_pid" >/dev/null 2>&1 || true', mount_section)
        self.assertIn('kill -9 "$mount_pid" >/dev/null 2>&1 || true', mount_section)
        self.assertIn('created_mountpoint=0', mount_section)
        self.assertIn('created_mountpoint=1', mount_section)
        self.assertIn('/bin/rmdir "$volume_root" >/dev/null 2>&1 || true', mount_section)
        self.assertIn('log "mount_hfs command did not exit promptly for $dev_path at $volume_root; re-checking mount state"', mount_section)
        self.assertIn('log "mount_hfs command timed out, but volume is mounted"', mount_section)
        self.assertIn('log "mount_hfs timed out for $dev_path at $volume_root and volume was not mounted at the immediate re-check, will try manual mount"', mount_section)

    def test_render_start_script_waits_for_smbd_process_after_launch(self) -> None:
        values = {
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_SHARE_NAME": "Data",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_NET_IFACE": "bridge0",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "AirPortTimeCapsule",
            "TC_SAMBA_USER": "admin",
        }
        bundle = build_template_bundle(values)
        rendered = render_template("start-samba.sh", bundle.start_script_replacements)
        self.assertIn('"$RAM_SBIN/smbd" -D -s "$RAM_ETC/smb.conf"', rendered)
        self.assertIn('if wait_for_process smbd 15; then', rendered)
        self.assertNotIn('get_smbd_log_path_from_config', rendered)
        self.assertNotIn('wait_for_smbd_ready', rendered)
        self.assertNotIn('daemon_ready', rendered)
        self.assertNotIn("SMBD_READY_MARKER", rendered)
        self.assertIn('if wait_for_process "$MDNS_PROC_NAME" 100; then', rendered)
        self.assertIn('log "smbd startup complete: process observed"', rendered)

    def test_render_start_script_prepares_local_hostname_resolution_after_network_detection(self) -> None:
        values = {
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_SHARE_NAME": "Data",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_NET_IFACE": "bridge0",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "AirPortTimeCapsule",
            "TC_SAMBA_USER": "admin",
        }
        bundle = build_template_bundle(values)
        rendered = render_template("start-samba.sh", bundle.start_script_replacements)
        helper_start = rendered.index("prepare_local_hostname_resolution()")
        helper_end = rendered.index("start_smbd()")
        helper_section = rendered[helper_start:helper_end]
        self.assertIn('device_hostname=$(/bin/hostname 2>/dev/null || true)', helper_section)
        self.assertIn("printf '127.0.0.1\\t%s %s.local\\n'", helper_section)
        self.assertNotIn("grep", helper_section)
        self.assertNotIn("awk", helper_section)

        network_detection = rendered.index("BRIDGE0_IP=${BRIDGE0_IP%%/*}")
        hostname_call = rendered.index("\nprepare_local_hostname_resolution\n", network_detection)
        mdns_capture = rendered.index("\nstart_mdns_capture\n", network_detection)
        smbd_start = rendered.index("\nstart_smbd ||", network_detection)
        self.assertLess(network_detection, hostname_call)
        self.assertLess(hostname_call, mdns_capture)
        self.assertLess(hostname_call, smbd_start)

    def test_common_script_has_no_smbd_daemon_ready_helpers(self) -> None:
        common = (REPO_ROOT / "src/timecapsulesmb/assets/boot/samba4/common.sh").read_text()
        self.assertNotIn("get_smbd_log_path_from_config()", common)
        self.assertNotIn("wait_for_smbd_ready()", common)
        self.assertNotIn("daemon_ready", common)

    def test_render_smb_conf_uses_ram_cache_directory_by_default(self) -> None:
        values = {
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_SHARE_NAME": "Data",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_NET_IFACE": "bridge0",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "AirPortTimeCapsule",
            "TC_SAMBA_USER": "admin",
        }
        bundle = build_template_bundle(values)
        rendered = render_template("smb.conf.template", bundle.smbconf_replacements)
        self.assertIn("cache directory = /mnt/Memory/samba4/var", rendered)
        self.assertIn("lock directory = /mnt/Locks", rendered)
        self.assertIn("state directory = /mnt/Memory/samba4/var", rendered)
        self.assertIn("private dir = /mnt/Memory/samba4/private", rendered)
        self.assertIn("log file = /mnt/Memory/samba4/var/log.smbd", rendered)
        self.assertIn("max log size = 256", rendered)
        self.assertIn("max log size = 256\n    smb ports = 445", rendered)
        self.assertNotIn("log level =", rendered)
        self.assertIn("reset on zero vc = yes", rendered)
        self.assertIn("create mask = 0666", rendered)
        self.assertIn("directory mask = 0777", rendered)
        self.assertIn("force create mode = 0666", rendered)
        self.assertIn("force directory mode = 0777", rendered)

    def test_render_smb_conf_uses_disk_logging_when_debug_logging_enabled(self) -> None:
        values = {
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_SHARE_NAME": "Data",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_NET_IFACE": "bridge0",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "AirPortTimeCapsule",
            "TC_SAMBA_USER": "admin",
        }
        bundle = build_template_bundle(values, debug_logging=True)
        rendered = render_template("smb.conf.template", bundle.smbconf_replacements)
        self.assertIn("log file = __DATA_ROOT__/samba4-logs/log.smbd", rendered)
        self.assertIn("max log size = 1048576", rendered)
        self.assertIn("log level = 5 vfs:8 fruit:8", rendered)
        self.assertIn("max log size = 1048576\n    log level = 5 vfs:8 fruit:8\n    smb ports = 445", rendered)

    def test_render_start_script_prepares_smbd_disk_logging_only_when_debug_logging_enabled(self) -> None:
        values = {
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_SHARE_NAME": "Data",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_NET_IFACE": "bridge0",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "AirPortTimeCapsule",
            "TC_SAMBA_USER": "admin",
        }
        normal_bundle = build_template_bundle(values)
        normal_rendered = render_template("start-samba.sh", normal_bundle.start_script_replacements)
        self.assertIn("SMBD_DISK_LOGGING_ENABLED=0", normal_rendered)
        debug_bundle = build_template_bundle(values, debug_logging=True, data_root="/Volumes/dk2/ShareRoot")
        debug_rendered = render_template("start-samba.sh", debug_bundle.start_script_replacements)

        self.assertIn("SMBD_DISK_LOGGING_ENABLED=1", debug_rendered)
        self.assertIn('if [ "$SMBD_DISK_LOGGING_ENABLED" != "1" ]; then', debug_rendered)
        self.assertIn('log_dir="$DATA_ROOT/samba4-logs"', debug_rendered)
        self.assertIn('mkdir -p "$log_dir"', debug_rendered)
        self.assertIn('chmod 777 "$log_dir" >/dev/null 2>&1 || true', debug_rendered)
        self.assertIn('log "smbd debug logging directory ready: $log_dir"', debug_rendered)

    def test_render_start_script_prepares_smbd_disk_logging_after_data_root_before_runtime_staging(self) -> None:
        values = {
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_SHARE_NAME": "Data",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_NET_IFACE": "bridge0",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "AirPortTimeCapsule",
            "TC_SAMBA_USER": "admin",
        }
        bundle = build_template_bundle(values, debug_logging=True, data_root="/Volumes/dk2/ShareRoot")
        rendered = render_template("start-samba.sh", bundle.start_script_replacements)
        main_section = rendered[rendered.index("cleanup_old_runtime"):]

        self.assertLess(main_section.index('log "data root selected: $DATA_ROOT"'), main_section.index("prepare_smbd_disk_logging || true"))
        self.assertLess(main_section.index("prepare_smbd_disk_logging || true"), main_section.index('stage_runtime "$PAYLOAD_DIR" "$SMBD_SRC" "$NBNS_SRC"'))

    def test_render_smb_conf_uses_persistent_cache_directory_for_netbsd4(self) -> None:
        values = {
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_SHARE_NAME": "Data",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_NET_IFACE": "bridge0",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "AirPortTimeCapsule",
            "TC_SAMBA_USER": "admin",
        }
        bundle = build_template_bundle(values, payload_family="netbsd4le_samba4")
        rendered = render_template("smb.conf.template", bundle.smbconf_replacements)
        self.assertIn("cache directory = __PAYLOAD_DIR__/cache", rendered)
        self.assertIn("lock directory = /mnt/Locks", rendered)
        self.assertIn("state directory = /mnt/Memory/samba4/var", rendered)
        self.assertIn("private dir = /mnt/Memory/samba4/private", rendered)
        self.assertIn("reset on zero vc = yes", rendered)

    def test_render_start_script_uses_persistent_cache_directory_for_netbsd4_fallback(self) -> None:
        values = {
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_SHARE_NAME": "Data",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_NET_IFACE": "bridge0",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "AirPortTimeCapsule",
            "TC_SAMBA_USER": "admin",
        }
        bundle = build_template_bundle(values, payload_family="netbsd4le_samba4")
        rendered = render_template("start-samba.sh", bundle.start_script_replacements)
        self.assertIn("CACHE_DIRECTORY=$PAYLOAD_DIR/cache", rendered)
        self.assertIn("cache directory = $CACHE_DIRECTORY", rendered)
        self.assertIn("reset on zero vc = yes", rendered)

    def test_render_start_script_defaults_to_shareroot_mode(self) -> None:
        values = {
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_SHARE_NAME": "Data",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_NET_IFACE": "bridge0",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "AirPortTimeCapsule",
            "TC_SAMBA_USER": "admin",
        }
        bundle = build_template_bundle(values)
        rendered = render_template("start-samba.sh", bundle.start_script_replacements)
        self.assertIn("SHARE_USE_DISK_ROOT=false", rendered)
        discover_start = rendered.index("discover_preexisting_data_root()")
        discover_end = rendered.index("\nresolve_data_root_on_mounted_volume()")
        discover_section = rendered[discover_start:discover_end]
        self.assertIn('if [ "$SHARE_USE_DISK_ROOT" = "true" ]; then', discover_section)
        self.assertLess(
            discover_section.index('if [ "$SHARE_USE_DISK_ROOT" = "true" ]; then'),
            discover_section.index('if data_root=$(wait_for_existing_mount_target "data root" find_existing_data_root); then'),
        )
        resolve_start = rendered.index("resolve_data_root_on_mounted_volume()")
        resolve_end = rendered.index("\nwait_for_existing_mount_target()")
        resolve_section = rendered[resolve_start:resolve_end]
        self.assertIn('if data_root=$(find_data_root_under_volume "$volume_root"); then', resolve_section)
        self.assertLess(
            resolve_section.index('if [ "$SHARE_USE_DISK_ROOT" = "true" ]; then'),
            resolve_section.index('if data_root=$(find_data_root_under_volume "$volume_root"); then'),
        )

    def test_render_start_script_disk_root_mode_polls_for_apple_mounted_volume_root(self) -> None:
        values = {
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_SHARE_NAME": "Data",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_NET_IFACE": "bridge0",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "AirPortTimeCapsule",
            "TC_SAMBA_USER": "admin",
        }
        bundle = build_template_bundle(values, share_use_disk_root=True)
        rendered = render_template("start-samba.sh", bundle.start_script_replacements)
        self.assertIn("SHARE_USE_DISK_ROOT=true", rendered)
        discover_start = rendered.index("discover_preexisting_data_root()")
        discover_end = rendered.index("\nresolve_data_root_on_mounted_volume()")
        discover_section = rendered[discover_start:discover_end]
        self.assertIn('if [ "$SHARE_USE_DISK_ROOT" = "true" ]; then', discover_section)
        self.assertIn('if volume_root=$(wait_for_existing_mount_target "disk root" find_existing_volume_root); then', discover_section)
        self.assertIn('log "found Apple-mounted disk root: $volume_root"', discover_section)
        self.assertIn('echo "$volume_root"', discover_section)
        self.assertNotIn('sleep "$APPLE_MOUNT_WAIT_SECONDS"', discover_section)
        self.assertLess(
            discover_section.index('if [ "$SHARE_USE_DISK_ROOT" = "true" ]; then'),
            discover_section.index('if data_root=$(wait_for_existing_mount_target "data root" find_existing_data_root); then'),
        )
        resolve_start = rendered.index("resolve_data_root_on_mounted_volume()")
        resolve_end = rendered.index("\nwait_for_existing_mount_target()")
        resolve_section = rendered[resolve_start:resolve_end]
        self.assertIn('log "disk-root share mode: using mounted volume root $volume_root"', resolve_section)
        self.assertIn('echo "$volume_root"', resolve_section)
        self.assertLess(
            resolve_section.index('if [ "$SHARE_USE_DISK_ROOT" = "true" ]; then'),
            resolve_section.index('if data_root=$(find_data_root_under_volume "$volume_root"); then'),
        )

    def test_render_start_script_defers_netbsd4_cache_assignment_until_data_root_exists(self) -> None:
        values = {
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_SHARE_NAME": "Data",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_NET_IFACE": "bridge0",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "AirPortTimeCapsule",
            "TC_SAMBA_USER": "admin",
        }
        bundle = build_template_bundle(values, payload_family="netbsd4le_samba4")
        rendered = render_template("start-samba.sh", bundle.start_script_replacements)
        data_root_index = rendered.index('if DATA_ROOT=$(discover_preexisting_data_root); then')
        payload_index = rendered.index('PAYLOAD_DIR=$(find_payload_dir "$DATA_ROOT") || {')
        cache_index = rendered.index("CACHE_DIRECTORY=$PAYLOAD_DIR/cache")
        self.assertGreater(payload_index, data_root_index)
        self.assertGreater(cache_index, payload_index)

    def test_render_smb_conf_uses_custom_persistent_cache_directory_for_netbsd4(self) -> None:
        values = {
            "TC_PAYLOAD_DIR_NAME": "samba4-test",
            "TC_SHARE_NAME": "Data",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_NET_IFACE": "bridge0",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "AirPortTimeCapsule",
            "TC_SAMBA_USER": "admin",
        }
        bundle = build_template_bundle(values, payload_family="netbsd4le_samba4")
        rendered = render_template("smb.conf.template", bundle.smbconf_replacements)
        self.assertIn("cache directory = __PAYLOAD_DIR__/cache", rendered)
        self.assertNotIn("__CACHE_DIRECTORY__", rendered)
        self.assertNotIn("__PAYLOAD_DIR_NAME__", rendered)

    def test_render_watchdog_script_includes_device_model_flag(self) -> None:
        values = {
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_SHARE_NAME": "Data",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_NET_IFACE": "bridge0",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "AirPortTimeCapsule",
            "TC_AIRPORT_SYAP": "119",
            "TC_SAMBA_USER": "admin",
        }
        bundle = build_template_bundle(values)
        rendered = render_template("watchdog.sh", bundle.watchdog_replacements)
        self.assertIn('MDNS_DEVICE_MODEL=AirPortTimeCapsule', rendered)
        self.assertIn('AIRPORT_SYAP=119', rendered)
        self.assertIn('--device-model "$MDNS_DEVICE_MODEL"', rendered)
        self.assertIn('. /mnt/Flash/common.sh', rendered)
        self.assertIn('--airport-wama "$AIRPORT_WAMA"', rendered)
        self.assertIn('--airport-rama "$AIRPORT_RAMA"', rendered)
        self.assertIn('--airport-ram2 "$AIRPORT_RAM2"', rendered)
        self.assertIn('--airport-syap "$AIRPORT_SYAP"', rendered)
        self.assertIn('--airport-syvs "$AIRPORT_SYVS"', rendered)
        self.assertIn('--airport-srcv "$AIRPORT_SRCV"', rendered)
        self.assertIn('airport syAP missing; advertising _airport._tcp without syAP', rendered)
        self.assertIn('airport clone fields incomplete; skipping _airport._tcp advertisement', rendered)
        self.assertIn('ADISK_DISK_KEY=dk0', rendered)
        self.assertIn("ADISK_UUID=''", rendered)
        self.assertIn('--adisk-disk-key "$ADISK_DISK_KEY"', rendered)
        self.assertIn('--load-snapshot "$APPLE_MDNS_SNAPSHOT"', rendered)
        self.assertIn('SNAPSHOT_BOOTSTRAP_GRACE_SECONDS=120', rendered)
        self.assertIn('watchdog recovery: mdns restart deferred while startup snapshot capture may still be running', rendered)
        self.assertIn('[ ! -f "$APPLE_MDNS_SNAPSHOT" ] && [ "$elapsed" -lt "$SNAPSHOT_BOOTSTRAP_GRACE_SECONDS" ]', rendered)
        self.assertIn('if [ -f "$APPLE_MDNS_SNAPSHOT" ]; then', rendered)
        self.assertIn('--save-all-snapshot "$ALL_MDNS_SNAPSHOT"', rendered)
        self.assertIn('--save-snapshot "$APPLE_MDNS_SNAPSHOT"', rendered)
        self.assertIn('--load-snapshot "$APPLE_MDNS_SNAPSHOT"', rendered)
        self.assertIn('--adisk-uuid "$ADISK_UUID"', rendered)
        self.assertIn('--adisk-sys-wama "$iface_mac"', rendered)
        self.assertIn('if [ ! -f "$RAM_PRIVATE/nbns.enabled" ]; then', rendered)
        self.assertIn('"$NBNS_BIN" \\', rendered)
        self.assertIn('--name "$SMB_NETBIOS_NAME"', rendered)
        self.assertIn('rm -rf "$LOCKS_ROOT"/* >/dev/null 2>&1 || true', rendered)
        self.assertNotIn('if [ ! -f "$RAM_PRIVATE/nbns.enabled" ]; then\n        return 0\n    fi\n\n    if [ ! -x "$NBNS_BIN" ]; then\n        log_msg "NBNS restart skipped; missing $NBNS_BIN"\n        return 0\n    fi\n\n    iface_ip="$(get_iface_ipv4 "$NET_IFACE")"\n    if [ -z "$iface_ip" ]; then\n        log_msg "NBNS restart skipped; missing IPv4 for $NET_IFACE"\n        return 0\n    fi\n\n    pkill "$NBNS_PROC_NAME" >/dev/null 2>&1 || true\n    "$NBNS_BIN"', rendered)

    def test_render_watchdog_script_uses_fast_recovery_poll_before_steady_state(self) -> None:
        values = {
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_SHARE_NAME": "Data",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_NET_IFACE": "bridge0",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "AirPortTimeCapsule",
            "TC_SAMBA_USER": "admin",
        }
        bundle = build_template_bundle(values)
        rendered = render_template("watchdog.sh", bundle.watchdog_replacements)
        self.assertIn("RECOVERY_POLL_SECONDS=10", rendered)
        self.assertIn("STEADY_POLL_SECONDS=300", rendered)
        self.assertIn("INITIAL_STARTUP_DELAY_SECONDS=${1:-30}", rendered)
        self.assertIn("WATCHDOG_START_TS=$(/bin/date +%s)", rendered)
        self.assertIn('sleep "$INITIAL_STARTUP_DELAY_SECONDS"', rendered)
        self.assertIn("all_managed_services_healthy()", rendered)
        self.assertIn('elapsed=$((now_ts - WATCHDOG_START_TS))', rendered)
        self.assertIn('sleep "$RECOVERY_POLL_SECONDS"', rendered)
        self.assertIn('sleep "$STEADY_POLL_SECONDS"', rendered)

    def test_render_watchdog_script_requires_nbns_only_when_enabled(self) -> None:
        values = {
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_SHARE_NAME": "Data",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_NET_IFACE": "bridge0",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "AirPortTimeCapsule",
            "TC_SAMBA_USER": "admin",
        }
        bundle = build_template_bundle(values)
        rendered = render_template("watchdog.sh", bundle.watchdog_replacements)
        self.assertIn("nbns_enabled()", rendered)
        self.assertIn('if nbns_enabled; then', rendered)
        self.assertIn('if ! /usr/bin/pkill -0 "$NBNS_PROC_NAME" >/dev/null 2>&1; then', rendered)

    def test_render_watchdog_script_stops_apple_cifs_before_nbns_restart(self) -> None:
        values = {
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_SHARE_NAME": "Data",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_NET_IFACE": "bridge0",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "AirPortTimeCapsule",
            "TC_SAMBA_USER": "admin",
        }
        bundle = build_template_bundle(values)
        rendered = render_template("watchdog.sh", bundle.watchdog_replacements)
        restart_start = rendered.index("restart_nbns()")
        restart_end = rendered.index("nbns_enabled()")
        restart_section = rendered[restart_start:restart_end]
        self.assertIn('/usr/bin/pkill wcifsnd >/dev/null 2>&1 || true', restart_section)
        self.assertIn('/usr/bin/pkill wcifsfs >/dev/null 2>&1 || true', restart_section)
        self.assertLess(restart_section.index('/usr/bin/pkill wcifsnd >/dev/null 2>&1 || true'), restart_section.index('"$NBNS_BIN" \\'))
        self.assertLess(restart_section.index('/usr/bin/pkill wcifsfs >/dev/null 2>&1 || true'), restart_section.index('"$NBNS_BIN" \\'))

    def test_build_template_bundle_accepts_adisk_values(self) -> None:
        values = {
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_SHARE_NAME": "Data",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_NET_IFACE": "bridge0",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "AirPortTimeCapsule",
            "TC_SAMBA_USER": "admin",
        }
        bundle = build_template_bundle(values, adisk_disk_key="dk3", adisk_uuid="12345678-1234-1234-1234-123456789012")
        self.assertEqual(bundle.start_script_replacements["__ADISK_DISK_KEY__"], "dk3")
        self.assertEqual(bundle.start_script_replacements["__ADISK_UUID__"], "12345678-1234-1234-1234-123456789012")

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

    def test_discover_volume_root_raises_when_no_output(self) -> None:
        proc = mock.Mock(stdout="\n")
        with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=proc):
            with self.assertRaises(SystemExit):
                discover_volume_root_conn(SshConnection("root@10.0.0.2", "pw", "-o foo"))

    def test_discover_volume_root_prefers_existing_share_root(self) -> None:
        proc = mock.Mock(stdout="/Volumes/dk3\n")
        with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=proc):
            self.assertEqual(discover_volume_root_conn(SshConnection("root@10.0.0.2", "pw", "-o foo")), "/Volumes/dk3")

    def test_discover_volume_root_uses_discover_mounted_volume_first(self) -> None:
        mounted = mock.Mock(mountpoint="/Volumes/dk2")
        with mock.patch("timecapsulesmb.device.probe.discover_mounted_volume_conn", return_value=mounted) as mounted_mock:
            with mock.patch("timecapsulesmb.device.probe.run_ssh") as run_ssh_mock:
                volume = discover_volume_root_conn(SshConnection("root@10.0.0.2", "pw", "-o foo"))
        self.assertEqual(volume, "/Volumes/dk2")
        self.assertEqual(mounted_mock.call_args.args[0], SshConnection("root@10.0.0.2", "pw", "-o foo"))
        run_ssh_mock.assert_not_called()

    def test_discover_volume_root_falls_back_when_no_volume_is_mounted(self) -> None:
        proc = mock.Mock(stdout="/Volumes/dk3\n")
        with mock.patch("timecapsulesmb.device.probe.discover_mounted_volume_conn", side_effect=SystemExit("no mounted volume")):
            with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=proc) as run_ssh_mock:
                volume = discover_volume_root_conn(SshConnection("root@10.0.0.2", "pw", "-o foo"))
        self.assertEqual(volume, "/Volumes/dk3")
        run_ssh_mock.assert_called_once()

    def test_discover_volume_root_checks_existing_mounts_before_mounting_candidates(self) -> None:
        proc = mock.Mock(stdout="/Volumes/dk2\n")
        with mock.patch("timecapsulesmb.device.probe.discover_mounted_volume_conn", side_effect=SystemExit("no mounted volume")):
            with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=proc) as run_ssh_mock:
                discover_volume_root_conn(SshConnection("root@10.0.0.2", "pw", "-o foo"))
        cmd = run_ssh_mock.call_args.args[1]
        self.assertIn('volume="/Volumes/$dev"', cmd)
        self.assertIn('if [ ! -d "$volume" ]; then\n    mkdir -p "$volume"\n    created_mountpoint=1\n  fi', cmd)
        self.assertIn('df_line=$(/bin/df -k "$volume" 2>/dev/null | /usr/bin/tail -n +2 || true)', cmd)
        self.assertIn("disk_name_candidates()", cmd)
        self.assertIn("APdata", cmd)
        self.assertIn("APconfig", cmd)
        self.assertIn("APswap", cmd)
        self.assertIn("/sbin/sysctl -n hw.disknames", cmd)
        self.assertIn("for dev in $(disk_name_candidates); do", cmd)

    def test_discover_volume_root_cleans_up_unused_mountpoint_after_failed_fallback_mount(self) -> None:
        proc = mock.Mock(stdout="/Volumes/dk2\n")
        with mock.patch("timecapsulesmb.device.probe.discover_mounted_volume_conn", side_effect=SystemExit("no mounted volume")):
            with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=proc) as run_ssh_mock:
                discover_volume_root_conn(SshConnection("root@10.0.0.2", "pw", "-o foo"))
        cmd = run_ssh_mock.call_args.args[1]
        self.assertIn('created_mountpoint=0', cmd)
        self.assertIn('created_mountpoint=1', cmd)
        self.assertIn('/bin/rmdir "$volume" >/dev/null 2>&1 || true', cmd)

    def test_discover_mounted_volume_returns_active_device_and_mountpoint(self) -> None:
        proc = mock.Mock(stdout="/dev/dk2 /Volumes/dk2\n", returncode=0)
        with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=proc):
            mounted = discover_mounted_volume_conn(SshConnection("root@10.0.0.2", "pw", "-o foo"))
        self.assertEqual(mounted.device, "/dev/dk2")
        self.assertEqual(mounted.mountpoint, "/Volumes/dk2")

    def test_discover_mounted_volume_raises_when_no_candidate_is_mounted(self) -> None:
        proc = mock.Mock(stdout="", returncode=1)
        with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=proc):
            with self.assertRaises(SystemExit):
                discover_mounted_volume_conn(SshConnection("root@10.0.0.2", "pw", "-o foo"))

    def test_probe_device_skips_direct_tcp_check_for_proxy_ssh_options(self) -> None:
        with mock.patch("timecapsulesmb.device.probe.tcp_open", side_effect=AssertionError("direct TCP probe should be skipped")):
            with mock.patch("timecapsulesmb.device.probe._probe_remote_os_info_conn", return_value=("NetBSD", "4.0", "earmv4")):
                with mock.patch("timecapsulesmb.device.probe._probe_remote_elf_endianness_conn", return_value="big"):
                    with mock.patch("timecapsulesmb.device.probe.probe_remote_airport_identity_conn", return_value=mock.Mock(model=None, syap=None)):
                        result = probe_device_conn(
                            SshConnection("root@192.168.1.118", "pw", "-o ProxyCommand=ssh\\ -W\\ %h:%p\\ bastion")
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

    def test_remote_prepare_dirs_builds_expected_command(self) -> None:
        connection = SshConnection("host", "pw", "-o foo")
        with mock.patch("timecapsulesmb.deploy.executor.run_ssh") as run_ssh_mock:
            remote_prepare_dirs(connection, "/Volumes/dk2/samba4")
        command = run_ssh_mock.call_args.args[1]
        self.assertEqual(command, render_remote_action(prepare_dirs_action("/Volumes/dk2/samba4")))

    def test_remote_initialize_data_root_builds_expected_command(self) -> None:
        connection = SshConnection("host", "pw", "-o foo")
        with mock.patch("timecapsulesmb.deploy.executor.run_ssh") as run_ssh_mock:
            remote_initialize_data_root(
                connection,
                "/Volumes/dk2/ShareRoot",
                "/Volumes/dk2/ShareRoot/.com.apple.timemachine.supported",
            )
        command = run_ssh_mock.call_args.args[1]
        self.assertEqual(
            command,
            render_remote_action(
                initialize_data_root_action(
                    "/Volumes/dk2/ShareRoot",
                    "/Volumes/dk2/ShareRoot/.com.apple.timemachine.supported",
                )
            ),
        )

    def test_remote_install_permissions_builds_expected_command(self) -> None:
        connection = SshConnection("host", "pw", "-o foo")
        with mock.patch("timecapsulesmb.deploy.executor.run_ssh") as run_ssh_mock:
            remote_install_permissions(connection, "/Volumes/dk2/samba4")
        command = run_ssh_mock.call_args.args[1]
        self.assertEqual(command, render_remote_action(install_permissions_action("/Volumes/dk2/samba4")))

    def test_remote_ensure_adisk_uuid_reuses_existing_file(self) -> None:
        connection = SshConnection("host", "pw", "-o foo")
        with mock.patch("timecapsulesmb.deploy.executor.run_ssh", return_value=mock.Mock(stdout="12345678-1234-1234-1234-123456789012\n")):
            with mock.patch("timecapsulesmb.deploy.executor.run_scp") as scp_mock:
                result = remote_ensure_adisk_uuid(connection, "/Volumes/dk2/samba4/private")
        self.assertEqual(result, "12345678-1234-1234-1234-123456789012")
        scp_mock.assert_not_called()

    def test_remote_ensure_adisk_uuid_creates_new_file_when_missing(self) -> None:
        fixed_uuid = uuid.UUID("12345678-1234-1234-1234-123456789012")
        connection = SshConnection("host", "pw", "-o foo")
        with mock.patch("timecapsulesmb.deploy.executor.run_ssh", return_value=mock.Mock(stdout="\n")):
            with mock.patch("timecapsulesmb.deploy.executor.uuid.uuid4", return_value=fixed_uuid):
                with mock.patch("timecapsulesmb.deploy.executor.run_scp") as scp_mock:
                    result = remote_ensure_adisk_uuid(connection, "/Volumes/dk2/samba4/private")
        self.assertEqual(result, str(fixed_uuid))
        self.assertEqual(scp_mock.call_count, 1)

    def test_remote_enable_nbns_creates_marker_without_touch(self) -> None:
        connection = SshConnection("host", "pw", "-o foo")
        with mock.patch("timecapsulesmb.deploy.executor.run_ssh") as run_ssh_mock:
            remote_enable_nbns(connection, "/Volumes/dk2/samba4/private")
        self.assertEqual(run_ssh_mock.call_args.args[1], render_remote_action(enable_nbns_action("/Volumes/dk2/samba4/private")))

    def test_upload_deployment_payload_uploads_all_expected_files(self) -> None:
        paths = build_device_paths("/Volumes/dk2", "samba4")
        plan = build_deployment_plan("host", paths, Path("bin/smbd"), Path("bin/mdns"), Path("bin/nbns"))
        connection = SshConnection("host", "pw", "-o foo")
        with mock.patch("timecapsulesmb.deploy.executor.run_scp") as scp_mock:
            upload_deployment_payload(
                plan,
                connection=connection,
                rc_local=Path("/tmp/rc.local"),
                common_sh=Path("/tmp/common.sh"),
                rendered_start=Path("/tmp/start-samba.sh"),
                rendered_dfree=Path("/tmp/dfree.sh"),
                rendered_watchdog=Path("/tmp/watchdog.sh"),
                rendered_smbconf=Path("/tmp/smb.conf.template"),
            )
        self.assertEqual(scp_mock.call_count, 10)
        destinations = [call.args[2] for call in scp_mock.call_args_list]
        self.assertEqual(
            destinations,
            [
                "/Volumes/dk2/samba4/smbd",
                "/Volumes/dk2/samba4/mdns-advertiser",
                "/mnt/Flash/mdns-advertiser",
                "/Volumes/dk2/samba4/nbns-advertiser",
                "/mnt/Flash/rc.local",
                "/mnt/Flash/common.sh",
                "/mnt/Flash/start-samba.sh",
                "/mnt/Flash/watchdog.sh",
                "/mnt/Flash/dfree.sh",
                "/Volumes/dk2/samba4/smb.conf.template",
            ],
        )

    def test_verify_managed_runtime_passes_when_runtime_probe_succeeds(self) -> None:
        result = ManagedRuntimeProbeResult(
            ready=True,
            detail="managed runtime is ready",
            smbd=ManagedSmbdProbeResult(True, "managed smbd ready", ("PASS:managed smbd ready",)),
            mdns=ManagedMdnsTakeoverProbeResult(True, "managed mDNS takeover active", ("PASS:managed mDNS takeover active",)),
            lines=("PASS:managed smbd ready", "PASS:managed mDNS takeover active"),
        )
        with mock.patch("timecapsulesmb.deploy.verify.probe_managed_runtime_conn", return_value=result):
            with redirect_stdout(io.StringIO()):
                self.assertTrue(verify_managed_runtime(SshConnection("host", "pw", "-o foo"), heading="NetBSD4 activation verification:"))

    def test_verify_managed_runtime_fails_when_runtime_probe_fails(self) -> None:
        result = ManagedRuntimeProbeResult(
            ready=False,
            detail="managed runtime is not ready",
            smbd=ManagedSmbdProbeResult(False, "managed smbd is not ready", ("FAIL:managed smbd is not ready",)),
            mdns=ManagedMdnsTakeoverProbeResult(True, "managed mDNS takeover active", ("PASS:managed mDNS takeover active",)),
            lines=("FAIL:managed smbd is not ready", "PASS:managed mDNS takeover active"),
        )
        with mock.patch("timecapsulesmb.deploy.verify.probe_managed_runtime_conn", return_value=result):
            with redirect_stdout(io.StringIO()):
                self.assertFalse(verify_managed_runtime(SshConnection("host", "pw", "-o foo"), heading="NetBSD4 activation verification:"))

    def test_probe_managed_smbd_single_shot_checks_runtime_conf_parent_and_port_binding(self) -> None:
        with mock.patch(
            "timecapsulesmb.device.probe.run_ssh",
            return_value=mock.Mock(returncode=0, stdout=""),
        ) as run_ssh_mock:
            self.assertTrue(probe_managed_smbd_conn(SshConnection("host", "pw", "-o foo"), timeout_seconds=45).ready)
        remote_command = run_ssh_mock.call_args.args[1]
        self.assertIn("capture_ps_out()", remote_command)
        self.assertIn("smbd_parent_process_present()", remote_command)
        self.assertIn("smbd_bound_445()", remote_command)
        self.assertNotIn("smbd_ready_marker_matches_parent()", remote_command)
        self.assertNotIn("/mnt/Memory/samba4/var/smbd.ready", remote_command)
        self.assertNotIn("capture_ps_lstart_out()", remote_command)
        self.assertNotIn("normalize_lstart_fields()", remote_command)
        self.assertNotIn("smbd_log_has_fresh_daemon_ready()", remote_command)
        self.assertNotIn("/usr/bin/tail", remote_command)
        self.assertNotIn("max_attempts", remote_command)
        self.assertNotIn("sleep 5", remote_command)
        self.assertNotIn("nbns-advertiser", remote_command)

    def test_probe_managed_smbd_returns_detail_when_not_ready(self) -> None:
        with mock.patch(
            "timecapsulesmb.device.probe.run_ssh",
            return_value=mock.Mock(returncode=1, stdout="FAIL:managed smbd parent process is not running\n"),
        ):
            result = probe_managed_smbd_conn(SshConnection("host", "pw", "-o foo"), timeout_seconds=12)
        self.assertFalse(result.ready)
        self.assertEqual(result.detail, "managed smbd parent process is not running")

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
        self.assertIn("mdns_bound_5353()", remote_command)
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

    def test_format_deployment_plan_contains_concrete_actions(self) -> None:
        payload_dir_name = "samba4"
        payload_dir = f"/Volumes/dk2/{payload_dir_name}"
        paths = build_device_paths("/Volumes/dk2", payload_dir_name)
        plan = build_deployment_plan("root@10.0.0.2", paths, Path("bin/smbd"), Path("bin/mdns"), Path("bin/nbns"), install_nbns=True)
        text = format_deployment_plan(plan)
        self.assertIn("volume root: /Volumes/dk2", text)
        self.assertIn(f"Apple mount wait: {DEFAULT_APPLE_MOUNT_WAIT_SECONDS}s", text)
        self.assertIn("pkill -f '[w]atchdog.sh' >/dev/null 2>&1 || true", text)
        self.assertIn("pkill mdns-advertiser >/dev/null 2>&1 || true", text)
        self.assertIn("mkdir -p /Volumes/dk2/ShareRoot", text)
        self.assertIn("/bin/sh -c ': > /Volumes/dk2/ShareRoot/.com.apple.timemachine.supported'", text)
        self.assertIn(f"mkdir -p {payload_dir} {payload_dir}/private {payload_dir}/cache /mnt/Flash", text)
        self.assertIn(f"/bin/sh -c ': > {payload_dir}/private/nbns.enabled'", text)
        self.assertIn(f"generated smbpasswd -> {payload_dir}/private/smbpasswd", text)
        self.assertIn(f"generated adisk UUID -> {payload_dir}/private/adisk.uuid", text)
        self.assertIn(f"generated nbns marker -> {payload_dir}/private/nbns.enabled", text)
        self.assertIn("ln -s /mnt/Memory/samba4 /root/tc-netbsd4", text)
        self.assertIn("ln -s /mnt/Memory/samba4 /root/tc-netbsd4le", text)
        self.assertIn("ln -s /mnt/Memory/samba4 /root/tc-netbsd4be", text)
        self.assertIn(f"chmod 755 {payload_dir}/cache", text)
        self.assertIn(f"chmod 700 {payload_dir}/private", text)

    def test_netbsd4_activation_plan_contains_no_reboot_actions(self) -> None:
        paths = build_device_paths("/Volumes/dk2", "samba4")
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
            [action.kind for action in plan.activation_actions],
            ["stop_process_full", "stop_process", "stop_process", "stop_process", "stop_process", "run_script"],
        )
        self.assertEqual(
            [action.args[0] for action in plan.activation_actions],
            ["[w]atchdog.sh", "smbd", "mdns-advertiser", "nbns-advertiser", "wcifsfs", "/mnt/Flash/rc.local"],
        )

        text = format_deployment_plan(plan)
        self.assertIn("Remote actions (NetBSD4 activation):", text)
        self.assertIn("pkill -f '[w]atchdog.sh' >/dev/null 2>&1 || true", text)
        self.assertIn("pkill smbd >/dev/null 2>&1 || true", text)
        self.assertIn("pkill mdns-advertiser >/dev/null 2>&1 || true", text)
        self.assertIn("pkill nbns-advertiser >/dev/null 2>&1 || true", text)
        self.assertIn("pkill wcifsfs >/dev/null 2>&1 || true", text)
        self.assertIn("/bin/sh /mnt/Flash/rc.local", text)
        self.assertIn("Deploy will activate Samba immediately without rebooting.", text)
        self.assertIn("NetBSD 4 devices cannot auto-run Samba after a reboot.", text)
        self.assertIn("Run `activate` after a reboot if the device did not auto-start Samba.", text)
        self.assertIn("managed runtime smb.conf is present", text)
        self.assertIn("smbd is bound to TCP 445", text)
        self.assertIn("mdns-advertiser is bound to UDP 5353", text)

    def test_netbsd6_no_reboot_plan_has_no_reboot_checks(self) -> None:
        paths = build_device_paths("/Volumes/dk2", "samba4")
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
        paths = build_device_paths("/Volumes/dk2", "samba4")
        plan = build_uninstall_plan("root@10.0.0.2", paths)
        rendered = [render_remote_action(action) for action in plan.remote_actions]
        self.assertTrue(any(command.startswith("pkill nbns-advertiser >/dev/null 2>&1 || true;") for command in rendered))

    def test_build_uninstall_plan_stops_watchdog_first(self) -> None:
        paths = build_device_paths("/Volumes/dk2", "samba4")
        plan = build_uninstall_plan("root@10.0.0.2", paths)
        rendered = [render_remote_action(action) for action in plan.remote_actions]
        self.assertTrue(rendered[0].startswith("pkill -f '[w]atchdog.sh' >/dev/null 2>&1 || true;"))

    def test_build_uninstall_plan_removes_mdns_snapshots(self) -> None:
        paths = build_device_paths("/Volumes/dk2", "samba4")
        plan = build_uninstall_plan("root@10.0.0.2", paths)

        self.assertEqual(plan.flash_targets["allmdns.txt"], "/mnt/Flash/allmdns.txt")
        self.assertEqual(plan.flash_targets["applemdns.txt"], "/mnt/Flash/applemdns.txt")
        self.assertIn("/mnt/Flash/allmdns.txt", plan.verify_absent_targets)
        self.assertIn("/mnt/Flash/applemdns.txt", plan.verify_absent_targets)
        self.assertIn(remove_path_action("/mnt/Flash/allmdns.txt"), plan.remote_actions)
        self.assertIn(remove_path_action("/mnt/Flash/applemdns.txt"), plan.remote_actions)

    def test_remote_action_rendering_quotes_payload_paths_with_spaces(self) -> None:
        payload_dir = "/Volumes/dk2/Time Capsule Samba 4"
        prepare_cmd = render_remote_action(prepare_dirs_action(payload_dir))
        permissions_cmd = render_remote_action(install_permissions_action(payload_dir))
        enable_cmd = render_remote_action(enable_nbns_action(payload_dir + "/private"))
        self.assertIn("'/Volumes/dk2/Time Capsule Samba 4'", prepare_cmd)
        self.assertIn("'/Volumes/dk2/Time Capsule Samba 4/private'", prepare_cmd)
        self.assertIn("'/Volumes/dk2/Time Capsule Samba 4/cache'", prepare_cmd)
        self.assertIn("'/Volumes/dk2/Time Capsule Samba 4/cache'", permissions_cmd)
        self.assertIn("'/Volumes/dk2/Time Capsule Samba 4/nbns-advertiser'", permissions_cmd)
        self.assertIn("'/Volumes/dk2/Time Capsule Samba 4/private/nbns.enabled'", permissions_cmd)
        self.assertIn("if [ -f '/Volumes/dk2/Time Capsule Samba 4/private/nbns.enabled' ]; then", permissions_cmd)
        self.assertNotIn("|| chmod 600", permissions_cmd)
        self.assertNotIn("|| true", permissions_cmd)
        self.assertIn("'/Volumes/dk2/Time Capsule Samba 4/private/nbns.enabled'", enable_cmd)
        self.assertEqual(render_remote_action(run_script_action("/mnt/Flash/rc.local")), "/bin/sh /mnt/Flash/rc.local")
        self.assertEqual(
            render_remote_action(run_script_action("/mnt/Flash/Time Capsule SMB/rc.local")),
            "/bin/sh '/mnt/Flash/Time Capsule SMB/rc.local'",
        )
        initialize_cmd = render_remote_action(
            initialize_data_root_action(
                "/Volumes/dk2/Time Capsule ShareRoot",
                "/Volumes/dk2/Time Capsule ShareRoot/.com.apple.timemachine.supported",
            )
        )
        self.assertIn("'/Volumes/dk2/Time Capsule ShareRoot'", initialize_cmd)
        self.assertIn("'/Volumes/dk2/Time Capsule ShareRoot/.com.apple.timemachine.supported'", initialize_cmd)

    def test_deployment_plan_and_executor_share_permission_command_generation(self) -> None:
        payload_dir = "/Volumes/dk2/Time Capsule Samba 4"
        connection = SshConnection("host", "pw", "-o foo")
        with mock.patch("timecapsulesmb.deploy.executor.run_ssh") as run_ssh_mock:
            remote_install_permissions(connection, payload_dir)
        self.assertEqual(run_ssh_mock.call_args.args[1], render_remote_action(install_permissions_action(payload_dir)))

    def test_remote_uninstall_payload_runs_actions_sequentially(self) -> None:
        paths = build_device_paths("/Volumes/dk2", "samba4")
        plan = build_uninstall_plan("root@10.0.0.2", paths)
        expected = [render_remote_action(action) for action in plan.remote_actions]
        connection = SshConnection("host", "pw", "-o foo")
        with mock.patch("timecapsulesmb.deploy.executor.run_ssh") as run_ssh_mock:
            remote_uninstall_payload(connection, plan)
        self.assertEqual([call.args[1] for call in run_ssh_mock.call_args_list], expected)

    def test_render_stop_process_action_waits_for_exit(self) -> None:
        command = render_remote_action(stop_process_action("mdns-advertiser"))
        self.assertIn("pkill mdns-advertiser >/dev/null 2>&1 || true;", command)
        self.assertIn("while /bin/sh -c 'found=1; if ps ax -o ucomm= >/tmp/tcapsule-ps.", command)
        self.assertIn('case \"$line\" in mdns-advertiser) found=1; break ;; esac;', command)
        self.assertIn('if [ "$attempt" -ge 10 ]; then break; fi;', command)

    def test_render_stop_process_full_action_waits_for_exit(self) -> None:
        command = render_remote_action(stop_process_full_action("[w]atchdog.sh"))
        self.assertIn("pkill -f '[w]atchdog.sh' >/dev/null 2>&1 || true;", command)
        self.assertIn("while /bin/sh -c 'found=1; if ps ax -o command= >/tmp/tcapsule-ps.", command)
        self.assertIn('case "$line" in *[w]atchdog.sh*) found=1; break ;; esac;', command)

    def test_wait_for_ssh_state_uses_real_ssh_probe_for_expected_up(self) -> None:
        proc = mock.Mock(returncode=0, stdout="ok\n")
        connection = SshConnection("root@10.0.0.2", "pw", "-o ProxyCommand=jump")
        with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=proc) as run_ssh_mock:
            self.assertTrue(wait_for_ssh_state_conn(connection, expected_up=True, timeout_seconds=1))
        run_ssh_mock.assert_called_once_with(connection, "/bin/echo ok", check=False, timeout=10)

    def test_wait_for_ssh_state_treats_probe_failure_as_down(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "-o ProxyCommand=jump")
        with mock.patch("timecapsulesmb.device.probe.run_ssh", side_effect=SystemExit("timeout")) as run_ssh_mock:
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
        with mock.patch("timecapsulesmb.device.probe.run_ssh", side_effect=[ok, SystemExit("down")]) as run_ssh_mock:
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
