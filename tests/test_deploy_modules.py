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
    build_template_bundle,
    cache_directory_replacements,
    load_boot_asset_text,
    render_template,
    render_template_text,
)
from timecapsulesmb.deploy.verify import (
    verify_netbsd4_activation,
    verify_post_deploy,
    wait_for_post_reboot_mdns_ready,
    wait_for_post_reboot_mdns_takeover,
    wait_for_post_reboot_bonjour,
    wait_for_post_reboot_smbd,
)
from timecapsulesmb.device.probe import build_device_paths, discover_mounted_volume, discover_volume_root, wait_for_ssh_state


class DeployModuleTests(unittest.TestCase):
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
        self.assertEqual(bundle.start_script_replacements["__MDNS_DEVICE_MODEL__"], "TimeCapsule")
        self.assertEqual(bundle.start_script_replacements["__AIRPORT_SYAP__"], "119")
        self.assertEqual(bundle.start_script_replacements["__ADISK_DISK_KEY__"], "dk0")
        self.assertEqual(bundle.start_script_replacements["__ADISK_UUID__"], "''")

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
        start_cache, smbconf_cache = cache_directory_replacements("netbsd4_samba4", "samba4")
        self.assertEqual(start_cache, "$PAYLOAD_DIR/cache")
        self.assertEqual(smbconf_cache, "__PAYLOAD_DIR__/cache")
        self.assertFalse(start_cache.startswith("'"))
        self.assertFalse(start_cache.endswith("'"))

    def test_cache_directory_replacements_use_custom_payload_dir_for_netbsd4_smbconf(self) -> None:
        start_cache, smbconf_cache = cache_directory_replacements("netbsd4_samba4", "samba4-test")
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
        self.assertIn("wait_for_smbd_ready()", content)
        self.assertIn("derive_airport_fields()", content)
        self.assertIn("get_airport_syvs()", content)
        self.assertIn("sed -n 's/^\\([0-9]\\)\\([0-9]\\)\\([0-9]\\).*/\\1.\\2.\\3/p'", content)

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
        self.assertIn("/mnt/Flash/watchdog.sh </dev/null >/dev/null 2>&1 &", content)

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
        self.assertIn('MDNS_STARTUP_DELAY_SECONDS=30', rendered)
        self.assertIn('SCRIPT_START_TS=$(/bin/date +%s)', rendered)
        self.assertIn('if [ "$elapsed" -lt "$MDNS_STARTUP_DELAY_SECONDS" ]; then', rendered)
        self.assertIn('sleep $((MDNS_STARTUP_DELAY_SECONDS - elapsed))', rendered)
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
        self.assertIn('try_mount_candidate /dev/dk2 /Volumes/dk2', rendered)
        self.assertIn('try_mount_candidate /dev/dk3 /Volumes/dk3', rendered)
        self.assertIn('resolve_data_root_on_mounted_volume "$VOLUME_ROOT"', rendered)
        self.assertIn('/bin/df -k "$volume_root"', rendered)
        self.assertIn('df_line=$(/bin/df -k "$volume_root" 2>/dev/null | /usr/bin/tail -n +2 || true)', rendered)
        self.assertIn('case "$df_line" in', rendered)
        self.assertIn('*" $volume_root")', rendered)
        self.assertIn('if is_volume_root_mounted "$volume_root"; then', rendered)
        self.assertIn('initialize_data_root_under_volume "$volume_root"', rendered)
        self.assertIn(': >"$marker"', rendered)
        self.assertIn('log "waiting for Apple-mounted data volume before manual mount fallback"', rendered)
        self.assertIn('log "no Apple-mounted data root found; falling back to manual mount"', rendered)
        self.assertIn('log "found data root after manual mount: $DATA_ROOT"', rendered)
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
        self.assertLess(main_section.index('start_mdns'), main_section.index('log "waiting for Apple-mounted data volume before manual mount fallback"'))
        self.assertLess(main_section.index('log "waiting for Apple-mounted data volume before manual mount fallback"'), main_section.index('if DATA_ROOT=$(discover_preexisting_data_root); then'))
        self.assertLess(main_section.index('if DATA_ROOT=$(discover_preexisting_data_root); then'), main_section.index('VOLUME_ROOT=$(mount_fallback_volume) || {'))
        self.assertLess(main_section.rindex('start_nbns'), main_section.index('log "smbd ready"'))
        self.assertLess(main_section.rindex('start_nbns'), main_section.index('log "smbd ready"'))
        discover_body = rendered[rendered.index("discover_preexisting_data_root()"):rendered.index("resolve_data_root_on_mounted_volume()")]
        self.assertIn("wait_for_existing_data_root", discover_body)
        self.assertIn('while [ "$attempt" -lt 30 ]; do', rendered[rendered.index("wait_for_existing_data_root()"):rendered.index("try_mount_candidate()")])

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
        end = rendered.index("\nis_volume_root_mounted() {")
        section = rendered[start:end]
        self.assertIn('if is_volume_root_mounted /Volumes/dk2 && data_root=$(find_data_root_under_volume /Volumes/dk2); then', section)
        self.assertIn('if is_volume_root_mounted /Volumes/dk3 && data_root=$(find_data_root_under_volume /Volumes/dk3); then', section)
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

    def test_render_start_script_checks_dk2_before_dk3_in_fallback_mount(self) -> None:
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
        self.assertLess(rendered.index("try_mount_candidate /dev/dk2 /Volumes/dk2"), rendered.index("try_mount_candidate /dev/dk3 /Volumes/dk3"))

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
        self.assertIn('log "no Apple-mounted data root found; falling back to manual mount"', rendered)

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
        self.assertIn('log "found data root after manual mount: $DATA_ROOT"', rendered)

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
        self.assertIn('log "mdns advertiser launch skipped; missing $NET_IFACE MAC address"', rendered)
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
        self.assertIn('log "missing payload directory under mounted volume"', rendered)
        self.assertIn('log "missing smbd in payload directory"', rendered)
        self.assertIn('log "smbd did not become ready"', rendered)

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
        self.assertIn('log "failed to mount fallback data volume"', rendered)
        self.assertIn('log "failed to discover or initialize data root on mounted volume"', rendered)

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
        recovery_end = rendered.index("wait_for_existing_data_root()")
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
        self.assertIn('if [ "$attempt" -ge 10 ]; then', mount_section)
        self.assertIn('kill "$mount_pid" >/dev/null 2>&1 || true', mount_section)
        self.assertIn('kill -9 "$mount_pid" >/dev/null 2>&1 || true', mount_section)
        self.assertIn('created_mountpoint=0', mount_section)
        self.assertIn('created_mountpoint=1', mount_section)
        self.assertIn('/bin/rmdir "$volume_root" >/dev/null 2>&1 || true', mount_section)
        self.assertIn('log "mount_hfs command did not exit promptly for $dev_path at $volume_root; re-checking mount state"', mount_section)
        self.assertIn('log "mount_hfs command timed out, but volume is mounted"', mount_section)
        self.assertIn('log "mount_hfs timed out for $dev_path at $volume_root and volume was not mounted at the immediate re-check"', mount_section)

    def test_render_start_script_waits_for_smbd_ready_after_launch(self) -> None:
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
        self.assertIn('if configured_smbd_log=$(get_smbd_log_path_from_config "$RAM_ETC/smb.conf" || true); then', rendered)
        self.assertIn('wait_for_smbd_ready "$smbd_ready_log"', rendered)
        self.assertIn('if wait_for_process "$MDNS_PROC_NAME" 90; then', rendered)
        self.assertIn('log "smbd ready"', rendered)

    def test_common_script_extracts_smbd_log_path_from_config(self) -> None:
        common = (REPO_ROOT / "src/timecapsulesmb/assets/boot/samba4/common.sh").read_text()
        self.assertIn("get_smbd_log_path_from_config()", common)
        self.assertIn("log file", common)

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
        self.assertIn("reset on zero vc = yes", rendered)

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
        bundle = build_template_bundle(values, payload_family="netbsd4_samba4")
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
        bundle = build_template_bundle(values, payload_family="netbsd4_samba4")
        rendered = render_template("start-samba.sh", bundle.start_script_replacements)
        self.assertIn("CACHE_DIRECTORY=$PAYLOAD_DIR/cache", rendered)
        self.assertIn("cache directory = $CACHE_DIRECTORY", rendered)
        self.assertIn("reset on zero vc = yes", rendered)

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
        bundle = build_template_bundle(values, payload_family="netbsd4_samba4")
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
        bundle = build_template_bundle(values, payload_family="netbsd4_samba4")
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
        self.assertIn('mdns restart deferred; waiting for startup snapshot bootstrap', rendered)
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
        self.assertIn("INITIAL_STARTUP_DELAY_SECONDS=30", rendered)
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

    def test_mdns_advertiser_save_snapshot_falls_back_to_raw_capture(self) -> None:
        content = (REPO_ROOT / "build" / "mdns-advertiser.c").read_text()
        self.assertIn(
            "Keep a raw LAN-wide dump in allmdns.txt for diagnostics, but\n"
            "             * only refresh applemdns.txt when the capture can be tied back to\n"
            "             * this unit's _airport identity.",
            content,
        )
        self.assertIn(
            'if (cfg.save_all_snapshot_path[0] != \'\\0\' &&\n'
            '                write_snapshot_file_atomic(cfg.save_all_snapshot_path, &captured_records) != 0) {',
            content,
        )

    def test_mdns_advertiser_suppresses_snapshot_device_info_and_afp(self) -> None:
        content = (REPO_ROOT / "build" / "mdns-advertiser.c").read_text()
        self.assertIn('name_equals(service_type, "_smb._tcp.local.")', content)
        self.assertIn('name_equals(service_type, "_adisk._tcp.local.")', content)
        self.assertIn('name_equals(service_type, "_device-info._tcp.local.")', content)
        self.assertIn('name_equals(service_type, "_afpovertcp._tcp.local.")', content)

    def test_mdns_advertiser_keeps_managed_device_info_when_snapshot_mode_is_enabled(self) -> None:
        content = (REPO_ROOT / "build" / "mdns-advertiser.c").read_text()
        self.assertIn("if (add_device_info_records(buf, &off, sizeof(buf), cfg, &answers) != 0) {", content)
        self.assertIn("if (cfg->device_model[0] != '\\0' &&\n        build_instance_fqdn(device_info_instance_fqdn", content)
        self.assertIn("} else if (cfg->device_model[0] != '\\0' &&\n                   name_equals(qname, cfg->device_info_service_type)", content)
        self.assertIn("} else if (cfg->device_model[0] != '\\0' &&\n                   name_equals(qname, device_info_instance_fqdn))", content)
        self.assertIn("if (want_device_info_ptr || want_device_info_srv || want_device_info_txt) {", content)

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
                discover_volume_root("root@10.0.0.2", "pw", "-o foo")

    def test_discover_volume_root_prefers_existing_share_root(self) -> None:
        proc = mock.Mock(stdout="/Volumes/dk3\n")
        with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=proc):
            self.assertEqual(discover_volume_root("root@10.0.0.2", "pw", "-o foo"), "/Volumes/dk3")

    def test_discover_volume_root_uses_discover_mounted_volume_first(self) -> None:
        mounted = mock.Mock(mountpoint="/Volumes/dk2")
        with mock.patch("timecapsulesmb.device.probe.discover_mounted_volume", return_value=mounted) as mounted_mock:
            with mock.patch("timecapsulesmb.device.probe.run_ssh") as run_ssh_mock:
                volume = discover_volume_root("root@10.0.0.2", "pw", "-o foo")
        self.assertEqual(volume, "/Volumes/dk2")
        mounted_mock.assert_called_once_with("root@10.0.0.2", "pw", "-o foo")
        run_ssh_mock.assert_not_called()

    def test_discover_volume_root_falls_back_when_no_volume_is_mounted(self) -> None:
        proc = mock.Mock(stdout="/Volumes/dk3\n")
        with mock.patch("timecapsulesmb.device.probe.discover_mounted_volume", side_effect=SystemExit("no mounted volume")):
            with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=proc) as run_ssh_mock:
                volume = discover_volume_root("root@10.0.0.2", "pw", "-o foo")
        self.assertEqual(volume, "/Volumes/dk3")
        run_ssh_mock.assert_called_once()

    def test_discover_volume_root_checks_existing_mounts_before_mounting_candidates(self) -> None:
        proc = mock.Mock(stdout="/Volumes/dk2\n")
        with mock.patch("timecapsulesmb.device.probe.discover_mounted_volume", side_effect=SystemExit("no mounted volume")):
            with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=proc) as run_ssh_mock:
                discover_volume_root("root@10.0.0.2", "pw", "-o foo")
        cmd = run_ssh_mock.call_args.args[3]
        self.assertIn('volume="/Volumes/$dev"', cmd)
        self.assertIn('if [ ! -d "$volume" ]; then\n    mkdir -p "$volume"\n    created_mountpoint=1\n  fi', cmd)
        self.assertIn('df_line=$(/bin/df -k "$volume" 2>/dev/null | /usr/bin/tail -n +2 || true)', cmd)
        self.assertEqual(cmd.count('for dev in dk2 dk3; do'), 1)

    def test_discover_volume_root_cleans_up_unused_mountpoint_after_failed_fallback_mount(self) -> None:
        proc = mock.Mock(stdout="/Volumes/dk2\n")
        with mock.patch("timecapsulesmb.device.probe.discover_mounted_volume", side_effect=SystemExit("no mounted volume")):
            with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=proc) as run_ssh_mock:
                discover_volume_root("root@10.0.0.2", "pw", "-o foo")
        cmd = run_ssh_mock.call_args.args[3]
        self.assertIn('created_mountpoint=0', cmd)
        self.assertIn('created_mountpoint=1', cmd)
        self.assertIn('/bin/rmdir "$volume" >/dev/null 2>&1 || true', cmd)

    def test_discover_mounted_volume_returns_active_device_and_mountpoint(self) -> None:
        proc = mock.Mock(stdout="/dev/dk2 /Volumes/dk2\n", returncode=0)
        with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=proc):
            mounted = discover_mounted_volume("root@10.0.0.2", "pw", "-o foo")
        self.assertEqual(mounted.device, "/dev/dk2")
        self.assertEqual(mounted.mountpoint, "/Volumes/dk2")

    def test_discover_mounted_volume_raises_when_no_candidate_is_mounted(self) -> None:
        proc = mock.Mock(stdout="", returncode=1)
        with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=proc):
            with self.assertRaises(SystemExit):
                discover_mounted_volume("root@10.0.0.2", "pw", "-o foo")

    def test_remote_prepare_dirs_builds_expected_command(self) -> None:
        with mock.patch("timecapsulesmb.deploy.executor.run_ssh") as run_ssh_mock:
            remote_prepare_dirs("host", "pw", "-o foo", "/Volumes/dk2/samba4")
        command = run_ssh_mock.call_args.args[3]
        self.assertEqual(command, render_remote_action(prepare_dirs_action("/Volumes/dk2/samba4")))

    def test_remote_initialize_data_root_builds_expected_command(self) -> None:
        with mock.patch("timecapsulesmb.deploy.executor.run_ssh") as run_ssh_mock:
            remote_initialize_data_root(
                "host",
                "pw",
                "-o foo",
                "/Volumes/dk2/ShareRoot",
                "/Volumes/dk2/ShareRoot/.com.apple.timemachine.supported",
            )
        command = run_ssh_mock.call_args.args[3]
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
        with mock.patch("timecapsulesmb.deploy.executor.run_ssh") as run_ssh_mock:
            remote_install_permissions("host", "pw", "-o foo", "/Volumes/dk2/samba4")
        command = run_ssh_mock.call_args.args[3]
        self.assertEqual(command, render_remote_action(install_permissions_action("/Volumes/dk2/samba4")))

    def test_remote_ensure_adisk_uuid_reuses_existing_file(self) -> None:
        with mock.patch("timecapsulesmb.deploy.executor.run_ssh", return_value=mock.Mock(stdout="12345678-1234-1234-1234-123456789012\n")):
            with mock.patch("timecapsulesmb.deploy.executor.run_scp") as scp_mock:
                result = remote_ensure_adisk_uuid("host", "pw", "-o foo", "/Volumes/dk2/samba4/private")
        self.assertEqual(result, "12345678-1234-1234-1234-123456789012")
        scp_mock.assert_not_called()

    def test_remote_ensure_adisk_uuid_creates_new_file_when_missing(self) -> None:
        fixed_uuid = uuid.UUID("12345678-1234-1234-1234-123456789012")
        with mock.patch("timecapsulesmb.deploy.executor.run_ssh", return_value=mock.Mock(stdout="\n")):
            with mock.patch("timecapsulesmb.deploy.executor.uuid.uuid4", return_value=fixed_uuid):
                with mock.patch("timecapsulesmb.deploy.executor.run_scp") as scp_mock:
                    result = remote_ensure_adisk_uuid("host", "pw", "-o foo", "/Volumes/dk2/samba4/private")
        self.assertEqual(result, str(fixed_uuid))
        self.assertEqual(scp_mock.call_count, 1)

    def test_remote_enable_nbns_creates_marker_without_touch(self) -> None:
        with mock.patch("timecapsulesmb.deploy.executor.run_ssh") as run_ssh_mock:
            remote_enable_nbns("host", "pw", "-o foo", "/Volumes/dk2/samba4/private")
        self.assertEqual(run_ssh_mock.call_args.args[3], render_remote_action(enable_nbns_action("/Volumes/dk2/samba4/private")))

    def test_upload_deployment_payload_uploads_all_expected_files(self) -> None:
        paths = build_device_paths("/Volumes/dk2", "samba4")
        plan = build_deployment_plan("host", paths, Path("bin/smbd"), Path("bin/mdns"), Path("bin/nbns"))
        with mock.patch("timecapsulesmb.deploy.executor.run_scp") as scp_mock:
            upload_deployment_payload(
                plan,
                host="host",
                password="pw",
                ssh_opts="-o foo",
                rc_local=Path("/tmp/rc.local"),
                common_sh=Path("/tmp/common.sh"),
                rendered_start=Path("/tmp/start-samba.sh"),
                rendered_dfree=Path("/tmp/dfree.sh"),
                rendered_watchdog=Path("/tmp/watchdog.sh"),
                rendered_smbconf=Path("/tmp/smb.conf.template"),
            )
        self.assertEqual(scp_mock.call_count, 10)
        destinations = [call.args[4] for call in scp_mock.call_args_list]
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

    def test_verify_netbsd4_activation_passes_when_fstat_has_smb_and_mdns_ports(self) -> None:
        fstat_output = """
PASS:managed runtime smb.conf present
PASS:managed smbd reported daemon_ready
root     smbd        2846   28* internet stream tcp c2b3a310 192.168.1.118:445
root     mdns-advertiser  3056    3* internet dgram udp c2ad757c *:5353
PASS:smbd bound to TCP 445
PASS:mdns-advertiser bound to UDP 5353
"""
        with mock.patch(
            "timecapsulesmb.deploy.verify.run_ssh",
            return_value=mock.Mock(returncode=0, stdout=fstat_output),
        ):
            with redirect_stdout(io.StringIO()):
                self.assertTrue(verify_netbsd4_activation("host", "pw", "-o foo"))

    def test_verify_netbsd4_activation_fails_when_fstat_check_fails(self) -> None:
        fstat_output = """
PASS:managed runtime smb.conf present
PASS:managed smbd reported daemon_ready
FAIL:smbd is not bound to TCP 445
PASS:mdns-advertiser bound to UDP 5353
"""
        with mock.patch(
            "timecapsulesmb.deploy.verify.run_ssh",
            return_value=mock.Mock(returncode=1, stdout=fstat_output),
        ):
            with redirect_stdout(io.StringIO()):
                self.assertFalse(verify_netbsd4_activation("host", "pw", "-o foo"))

    def test_verify_netbsd4_activation_fails_when_fstat_is_missing(self) -> None:
        fstat_output = """
FAIL:fstat missing
"""
        with mock.patch(
            "timecapsulesmb.deploy.verify.run_ssh",
            return_value=mock.Mock(returncode=1, stdout=fstat_output),
        ):
            with redirect_stdout(io.StringIO()):
                self.assertFalse(verify_netbsd4_activation("host", "pw", "-o foo"))

    def test_verify_netbsd4_activation_polls_for_background_launcher(self) -> None:
        with mock.patch(
            "timecapsulesmb.deploy.verify.run_ssh",
            return_value=mock.Mock(returncode=1, stdout="FAIL:smbd is not bound to TCP 445\n"),
        ) as run_ssh_mock:
            with redirect_stdout(io.StringIO()):
                verify_netbsd4_activation("host", "pw", "-o foo")
        remote_command = run_ssh_mock.call_args.args[3]
        self.assertIn('max_attempts=$(((180 + 4) / 5))', remote_command)
        self.assertIn('while [ "$attempt" -lt "$max_attempts" ]; do', remote_command)
        self.assertIn("sleep 5", remote_command)
        self.assertIn("/mnt/Memory/samba4/etc/smb.conf", remote_command)
        self.assertIn("daemon_ready", remote_command)

    def test_verify_netbsd4_activation_requires_smbd_process_name_for_445(self) -> None:
        fstat_output = """
PASS:managed runtime smb.conf present
PASS:managed smbd reported daemon_ready
root     otherd      1111   28* internet stream tcp c2b3a310 192.168.1.118:445
root     mdns-advertiser  3056    3* internet dgram udp c2ad757c *:5353
FAIL:smbd is not bound to TCP 445
PASS:mdns-advertiser bound to UDP 5353
"""
        with mock.patch(
            "timecapsulesmb.deploy.verify.run_ssh",
            return_value=mock.Mock(returncode=1, stdout=fstat_output),
        ):
            with redirect_stdout(io.StringIO()):
                self.assertFalse(verify_netbsd4_activation("host", "pw", "-o foo"))

    def test_verify_netbsd4_activation_fails_when_managed_runtime_missing(self) -> None:
        fstat_output = """
FAIL:managed runtime smb.conf missing
FAIL:managed smbd did not report daemon_ready
root     smbd        2846   28* internet stream tcp c2b3a310 192.168.1.118:445
root     mdns-advertiser  3056    3* internet dgram udp c2ad757c *:5353
PASS:smbd bound to TCP 445
PASS:mdns-advertiser bound to UDP 5353
"""
        with mock.patch(
            "timecapsulesmb.deploy.verify.run_ssh",
            return_value=mock.Mock(returncode=1, stdout=fstat_output),
        ):
            with redirect_stdout(io.StringIO()):
                self.assertFalse(verify_netbsd4_activation("host", "pw", "-o foo"))

    def test_verify_post_deploy_uses_ip_host_label_without_appending_local(self) -> None:
        values = {
            "TC_SAMBA_USER": "admin",
            "TC_PASSWORD": "pw",
            "TC_MDNS_HOST_LABEL": "10.0.1.99",
            "TC_MDNS_INSTANCE_NAME": "Home-Samba",
            "TC_HOST": "root@10.0.0.2",
        }
        with mock.patch("timecapsulesmb.deploy.verify.wait_for_post_reboot_bonjour", return_value=([], None, None)):
            with mock.patch("timecapsulesmb.deploy.verify.command_exists", return_value=True):
                with mock.patch(
                    "timecapsulesmb.deploy.verify.try_authenticated_smb_listing",
                    return_value=mock.Mock(status="PASS", message="authenticated SMB listing works for admin@10.0.1.99"),
                ) as listing_mock:
                    with redirect_stdout(io.StringIO()):
                        verify_post_deploy(values)
        listing_mock.assert_called_once_with("admin", "pw", ["10.0.1.99", "10.0.0.2"])

    def test_wait_for_post_reboot_bonjour_returns_early_when_instance_and_target_appear(self) -> None:
        with mock.patch(
            "timecapsulesmb.deploy.verify.run_bonjour_checks",
            side_effect=[
                ([], None, None),
                ([mock.Mock()], "Time Capsule Samba 4", "timecapsulesamba4.local:445"),
            ],
        ) as bonjour_mock:
            with mock.patch("timecapsulesmb.deploy.verify.time.sleep") as sleep_mock:
                results, instance, target = wait_for_post_reboot_bonjour("Time Capsule Samba 4", timeout_seconds=30.0)
        self.assertEqual(instance, "Time Capsule Samba 4")
        self.assertEqual(target, "timecapsulesamba4.local:445")
        self.assertEqual(bonjour_mock.call_count, 2)
        sleep_mock.assert_called_once()

    def test_wait_for_post_reboot_bonjour_returns_last_result_on_timeout(self) -> None:
        monotonic_values = iter([0.0, 1.0, 3.0, 4.0, 6.1])
        with mock.patch(
            "timecapsulesmb.deploy.verify.run_bonjour_checks",
            side_effect=[
                ([mock.Mock(status="FAIL")], None, None),
                ([mock.Mock(status="WARN")], "Other Samba", None),
            ],
        ):
            with mock.patch("timecapsulesmb.deploy.verify.time.monotonic", side_effect=lambda: next(monotonic_values)):
                with mock.patch("timecapsulesmb.deploy.verify.time.sleep") as sleep_mock:
                    results, instance, target = wait_for_post_reboot_bonjour("Time Capsule Samba 4", timeout_seconds=5.0, poll_interval_seconds=2.0)
        self.assertEqual(instance, "Other Samba")
        self.assertIsNone(target)
        self.assertEqual(len(results), 1)
        sleep_mock.assert_called()

    def test_wait_for_post_reboot_smbd_passes_when_managed_smbd_is_ready(self) -> None:
        with mock.patch(
            "timecapsulesmb.deploy.verify.run_ssh",
            return_value=mock.Mock(returncode=0),
        ) as run_ssh_mock:
            self.assertTrue(wait_for_post_reboot_smbd("host", "pw", "-o foo", timeout_seconds=45))
        remote_command = run_ssh_mock.call_args.args[3]
        self.assertIn('if /usr/bin/pkill -0 smbd >/dev/null 2>&1; then', remote_command)
        self.assertIn("*daemon_ready*", remote_command)
        self.assertIn('max_attempts=$(((45 + 4) / 5))', remote_command)
        self.assertIn('while [ "$attempt" -lt "$max_attempts" ]; do', remote_command)
        self.assertIn("sleep 5", remote_command)
        self.assertNotIn("mdns-advertiser", remote_command)
        self.assertNotIn("nbns-advertiser", remote_command)

    def test_wait_for_post_reboot_smbd_fails_when_remote_probe_times_out(self) -> None:
        with mock.patch(
            "timecapsulesmb.deploy.verify.run_ssh",
            return_value=mock.Mock(returncode=1),
        ):
            self.assertFalse(wait_for_post_reboot_smbd("host", "pw", "-o foo", timeout_seconds=12))

    def test_wait_for_post_reboot_mdns_takeover_passes_when_managed_mdns_is_ready(self) -> None:
        with mock.patch(
            "timecapsulesmb.deploy.verify.run_ssh",
            return_value=mock.Mock(returncode=0),
        ) as run_ssh_mock:
            self.assertTrue(wait_for_post_reboot_mdns_takeover("host", "pw", "-o foo", timeout_seconds=45))
        remote_command = run_ssh_mock.call_args.args[3]
        self.assertIn('/usr/bin/pkill -0 mdns-advertiser >/dev/null 2>&1', remote_command)
        self.assertIn('ucomm_field" = "mDNSResponder"', remote_command)
        self.assertNotIn("max_attempts", remote_command)

    def test_wait_for_post_reboot_mdns_takeover_fails_when_remote_probe_times_out(self) -> None:
        with mock.patch(
            "timecapsulesmb.deploy.verify.run_ssh",
            return_value=mock.Mock(returncode=1),
        ):
            self.assertFalse(wait_for_post_reboot_mdns_takeover("host", "pw", "-o foo", timeout_seconds=12))

    def test_wait_for_post_reboot_mdns_ready_requires_takeover_and_bonjour(self) -> None:
        monotonic_values = iter([0.0, 1.0, 1.0, 6.0, 6.0, 7.0, 7.0, 8.0, 8.0])
        with mock.patch(
            "timecapsulesmb.deploy.verify._post_reboot_mdns_takeover_probe",
            side_effect=[False, True, True],
        ) as takeover_mock:
            with mock.patch(
                "timecapsulesmb.deploy.verify.run_bonjour_checks",
                side_effect=[
                    ([mock.Mock(status="WARN")], None, None),
                    ([mock.Mock(status="PASS")], "Time Capsule Samba 4", "timecapsulesamba4.local:445"),
                ],
            ) as bonjour_mock:
                with mock.patch("timecapsulesmb.deploy.verify.time.monotonic", side_effect=lambda: next(monotonic_values)):
                    with mock.patch("timecapsulesmb.deploy.verify.time.sleep") as sleep_mock:
                        ready = wait_for_post_reboot_mdns_ready(
                            "host",
                            "pw",
                            "-o foo",
                            "Time Capsule Samba 4",
                            timeout_seconds=15.0,
                            poll_interval_seconds=5.0,
                        )
        self.assertTrue(ready)
        self.assertEqual(takeover_mock.call_count, 3)
        self.assertEqual(bonjour_mock.call_count, 2)
        sleep_mock.assert_called()

    def test_wait_for_post_reboot_mdns_ready_times_out_when_bonjour_never_appears(self) -> None:
        monotonic_values = iter([0.0, 1.0, 1.0, 6.0, 6.0, 11.0, 11.0, 12.1])
        with mock.patch(
            "timecapsulesmb.deploy.verify._post_reboot_mdns_takeover_probe",
            return_value=True,
        ):
            with mock.patch(
                "timecapsulesmb.deploy.verify.run_bonjour_checks",
                return_value=([mock.Mock(status="WARN")], None, None),
            ):
                with mock.patch("timecapsulesmb.deploy.verify.time.monotonic", side_effect=lambda: next(monotonic_values)):
                    with mock.patch("timecapsulesmb.deploy.verify.time.sleep"):
                        ready = wait_for_post_reboot_mdns_ready(
                            "host",
                            "pw",
                            "-o foo",
                            "Time Capsule Samba 4",
                            timeout_seconds=12.0,
                            poll_interval_seconds=5.0,
                        )
        self.assertFalse(ready)

    def test_format_deployment_plan_contains_concrete_actions(self) -> None:
        payload_dir_name = "samba4"
        payload_dir = f"/Volumes/dk2/{payload_dir_name}"
        paths = build_device_paths("/Volumes/dk2", payload_dir_name)
        plan = build_deployment_plan("root@10.0.0.2", paths, Path("bin/smbd"), Path("bin/mdns"), Path("bin/nbns"), install_nbns=True)
        text = format_deployment_plan(plan)
        self.assertIn("volume root: /Volumes/dk2", text)
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
        self.assertIn("Tested NetBSD4 devices cannot auto-run Samba after a reboot;", text)
        self.assertIn("other NetBSD4 generations may auto-start Samba if their firmware runs /mnt/Flash/rc.local after a reboot.", text)
        self.assertIn("Run `activate` after a reboot if the device did not auto-start Samba.", text)
        self.assertIn("fstat shows smbd bound to TCP 445", text)

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
        with mock.patch("timecapsulesmb.deploy.executor.run_ssh") as run_ssh_mock:
            remote_install_permissions("host", "pw", "-o foo", payload_dir)
        self.assertEqual(run_ssh_mock.call_args.args[3], render_remote_action(install_permissions_action(payload_dir)))

    def test_remote_uninstall_payload_runs_actions_sequentially(self) -> None:
        paths = build_device_paths("/Volumes/dk2", "samba4")
        plan = build_uninstall_plan("root@10.0.0.2", paths)
        expected = [render_remote_action(action) for action in plan.remote_actions]
        with mock.patch("timecapsulesmb.deploy.executor.run_ssh") as run_ssh_mock:
            remote_uninstall_payload("host", "pw", "-o foo", plan)
        self.assertEqual([call.args[3] for call in run_ssh_mock.call_args_list], expected)

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
        with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=proc) as run_ssh_mock:
            self.assertTrue(wait_for_ssh_state("root@10.0.0.2", "pw", "-o ProxyCommand=jump", expected_up=True, timeout_seconds=1))
        run_ssh_mock.assert_called_once_with("root@10.0.0.2", "pw", "-o ProxyCommand=jump", "/bin/echo ok", check=False, timeout=10)

    def test_wait_for_ssh_state_treats_probe_failure_as_down(self) -> None:
        with mock.patch("timecapsulesmb.device.probe.run_ssh", side_effect=SystemExit("timeout")) as run_ssh_mock:
            self.assertTrue(wait_for_ssh_state("root@10.0.0.2", "pw", "-o ProxyCommand=jump", expected_up=False, timeout_seconds=1))
        run_ssh_mock.assert_called_once_with("root@10.0.0.2", "pw", "-o ProxyCommand=jump", "/bin/echo ok", check=False, timeout=10)

    def test_wait_for_ssh_state_retries_until_up(self) -> None:
        fail = mock.Mock(returncode=255, stdout="")
        ok = mock.Mock(returncode=0, stdout="ok\n")
        with mock.patch("timecapsulesmb.device.probe.run_ssh", side_effect=[fail, ok]) as run_ssh_mock:
            with mock.patch("timecapsulesmb.device.probe.time.sleep") as sleep_mock:
                self.assertTrue(wait_for_ssh_state("root@10.0.0.2", "pw", "-o ProxyCommand=jump", expected_up=True, timeout_seconds=6))
        self.assertEqual(run_ssh_mock.call_count, 2)
        sleep_mock.assert_called_once_with(5)

    def test_wait_for_ssh_state_retries_until_down(self) -> None:
        ok = mock.Mock(returncode=0, stdout="ok\n")
        with mock.patch("timecapsulesmb.device.probe.run_ssh", side_effect=[ok, SystemExit("down")]) as run_ssh_mock:
            with mock.patch("timecapsulesmb.device.probe.time.sleep") as sleep_mock:
                self.assertTrue(wait_for_ssh_state("root@10.0.0.2", "pw", "-o ProxyCommand=jump", expected_up=False, timeout_seconds=6))
        self.assertEqual(run_ssh_mock.call_count, 2)
        sleep_mock.assert_called_once_with(5)

    def test_wait_for_ssh_state_times_out_when_state_never_matches(self) -> None:
        ok = mock.Mock(returncode=0, stdout="ok\n")
        with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=ok) as run_ssh_mock:
            with mock.patch("timecapsulesmb.device.probe.time.time", side_effect=[0.0, 0.0, 2.0]):
                with mock.patch("timecapsulesmb.device.probe.time.sleep") as sleep_mock:
                    self.assertFalse(wait_for_ssh_state("root@10.0.0.2", "pw", "-o ProxyCommand=jump", expected_up=False, timeout_seconds=1))
        run_ssh_mock.assert_called_once()
        sleep_mock.assert_called_once_with(5)


if __name__ == "__main__":
    unittest.main()
