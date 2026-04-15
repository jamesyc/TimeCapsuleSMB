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
from timecapsulesmb.deploy.verify import verify_netbsd4_activation
from timecapsulesmb.device.probe import build_device_paths, discover_volume_root, wait_for_ssh_state


class DeployModuleTests(unittest.TestCase):
    def test_nt_hash_hex_is_stable(self) -> None:
        self.assertEqual(nt_hash_hex("password"), "8846F7EAEE8FB117AD06BDD830B7586C")

    def test_render_smbpasswd_contains_root_mapping(self) -> None:
        smbpasswd_text, username_map = render_smbpasswd("admin", "password")
        self.assertTrue(smbpasswd_text.startswith("root:0:"))
        self.assertEqual(username_map, "root = admin\n")

    def test_build_template_bundle_contains_expected_keys(self) -> None:
        values = {
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_SHARE_NAME": "Data",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_NET_IFACE": "bridge0",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule",
            "TC_SAMBA_USER": "admin",
        }
        bundle = build_template_bundle(values)
        self.assertIn("__SMB_SHARE_NAME__", bundle.start_script_replacements)
        self.assertIn("__SMB_SAMBA_USER__", bundle.smbconf_replacements)
        self.assertIn("__CACHE_DIRECTORY__", bundle.start_script_replacements)
        self.assertIn("__CACHE_DIRECTORY__", bundle.smbconf_replacements)
        self.assertEqual(bundle.start_script_replacements["__MDNS_DEVICE_MODEL__"], "TimeCapsule")
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

    def test_cache_directory_replacements_default_unknown_family_to_ram_cache(self) -> None:
        self.assertEqual(
            cache_directory_replacements("unknown_future_family", "samba4"),
            ("/mnt/Memory/samba4/var", "/mnt/Memory/samba4/var"),
        )

    def test_cache_directory_replacements_keep_netbsd4_start_expression_unquoted(self) -> None:
        start_cache, smbconf_cache = cache_directory_replacements("netbsd4_samba4", "samba4")
        self.assertEqual(start_cache, "$DATA_ROOT/../$PAYLOAD_DIR_NAME/cache")
        self.assertEqual(smbconf_cache, "__DATA_ROOT__/../samba4/cache")
        self.assertFalse(start_cache.startswith("'"))
        self.assertFalse(start_cache.endswith("'"))

    def test_cache_directory_replacements_use_custom_payload_dir_for_netbsd4_smbconf(self) -> None:
        start_cache, smbconf_cache = cache_directory_replacements("netbsd4_samba4", "samba4-test")
        self.assertEqual(start_cache, "$DATA_ROOT/../$PAYLOAD_DIR_NAME/cache")
        self.assertEqual(smbconf_cache, "__DATA_ROOT__/../samba4-test/cache")

    def test_build_deployment_plan_uses_device_paths(self) -> None:
        paths = build_device_paths("/Volumes/dk2", "samba4")
        plan = build_deployment_plan("root@10.0.0.2", paths, Path("bin/smbd"), Path("bin/mdns"), Path("bin/nbns"))
        self.assertEqual(plan.payload_dir, "/Volumes/dk2/samba4")
        self.assertEqual(plan.private_dir, "/Volumes/dk2/samba4/private")
        self.assertEqual(plan.volume_root, "/Volumes/dk2")
        self.assertEqual(plan.disk_key, "dk2")
        self.assertEqual(plan.remote_directories[0], "/Volumes/dk2/samba4")
        self.assertIn("/Volumes/dk2/samba4/cache", plan.remote_directories)
        self.assertEqual(plan.payload_targets["nbns-advertiser"], "/Volumes/dk2/samba4/nbns-advertiser")
        self.assertEqual(
            plan.pre_upload_actions,
            [
                stop_process_full_action("[w]atchdog.sh"),
                stop_process_action("smbd"),
                stop_process_action("mdns-advertiser"),
                stop_process_action("nbns-advertiser"),
                prepare_dirs_action("/Volumes/dk2/samba4"),
            ],
        )
        self.assertEqual(plan.post_auth_actions, [install_permissions_action("/Volumes/dk2/samba4")])

    def test_render_template_text_replaces_tokens(self) -> None:
        self.assertEqual(render_template_text("hello __TOKEN__", {"__TOKEN__": "world"}), "hello world")

    def test_load_boot_asset_text_reads_packaged_asset(self) -> None:
        content = load_boot_asset_text("rc.local")
        self.assertIn("/mnt/Flash/start-samba.sh", content)

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
            "TC_SAMBA_USER": "admin",
        }
        bundle = build_template_bundle(values)
        rendered = render_template("start-samba.sh", bundle.start_script_replacements)
        self.assertIn('MDNS_DEVICE_MODEL=AirPortTimeCapsule', rendered)
        self.assertIn('--device-model "$MDNS_DEVICE_MODEL"', rendered)
        self.assertIn('ADISK_DISK_KEY=dk0', rendered)
        self.assertIn("ADISK_UUID=''", rendered)
        self.assertIn('--adisk-disk-key "$ADISK_DISK_KEY"', rendered)
        self.assertIn('--adisk-uuid "$ADISK_UUID"', rendered)
        self.assertIn('--adisk-sys-wama "$iface_mac"', rendered)
        self.assertIn("ether[[:space:]]", rendered)
        self.assertIn("address[[:space:]]", rendered)
        self.assertNotIn("tr '[:lower:]' '[:upper:]'", rendered)
        self.assertIn('if [ -f "$payload_dir/private/nbns.enabled" ]', rendered)
        self.assertIn('cp "$nbns_src" "$RAM_SBIN/nbns-advertiser"', rendered)
        self.assertIn('"$RAM_SBIN/nbns-advertiser" \\', rendered)
        self.assertIn("CACHE_DIRECTORY=/mnt/Memory/samba4/var", rendered)
        self.assertIn("cache directory = $CACHE_DIRECTORY", rendered)
        self.assertIn("lock directory = $RAM_LOCKS", rendered)
        self.assertIn("state directory = $RAM_VAR", rendered)
        self.assertNotIn('cp "$nbns_src" "$RAM_SBIN/nbns-advertiser"\n    chmod 755 "$RAM_SBIN/nbns-advertiser"', rendered)

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
        self.assertIn("lock directory = /mnt/Memory/samba4/locks", rendered)
        self.assertIn("state directory = /mnt/Memory/samba4/var", rendered)
        self.assertIn("private dir = /mnt/Memory/samba4/private", rendered)

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
        self.assertIn("cache directory = __DATA_ROOT__/../samba4/cache", rendered)
        self.assertIn("lock directory = /mnt/Memory/samba4/locks", rendered)
        self.assertIn("state directory = /mnt/Memory/samba4/var", rendered)
        self.assertIn("private dir = /mnt/Memory/samba4/private", rendered)

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
        self.assertIn("CACHE_DIRECTORY=$DATA_ROOT/../$PAYLOAD_DIR_NAME/cache", rendered)
        self.assertIn("cache directory = $CACHE_DIRECTORY", rendered)

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
        data_root_index = rendered.index("DATA_ROOT=$(ensure_data_root)")
        cache_index = rendered.index("CACHE_DIRECTORY=$DATA_ROOT/../$PAYLOAD_DIR_NAME/cache")
        self.assertGreater(cache_index, data_root_index)

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
        self.assertIn("cache directory = __DATA_ROOT__/../samba4-test/cache", rendered)
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
            "TC_SAMBA_USER": "admin",
        }
        bundle = build_template_bundle(values)
        rendered = render_template("watchdog.sh", bundle.watchdog_replacements)
        self.assertIn('MDNS_DEVICE_MODEL=AirPortTimeCapsule', rendered)
        self.assertIn('--device-model "$MDNS_DEVICE_MODEL"', rendered)
        self.assertIn('ADISK_DISK_KEY=dk0', rendered)
        self.assertIn("ADISK_UUID=''", rendered)
        self.assertIn('--adisk-disk-key "$ADISK_DISK_KEY"', rendered)
        self.assertIn('--adisk-uuid "$ADISK_UUID"', rendered)
        self.assertIn('--adisk-sys-wama "$iface_mac"', rendered)
        self.assertIn("ether[[:space:]]", rendered)
        self.assertIn("address[[:space:]]", rendered)
        self.assertNotIn("tr '[:lower:]' '[:upper:]'", rendered)
        self.assertIn('if [ ! -f "$RAM_PRIVATE/nbns.enabled" ]; then', rendered)
        self.assertIn('"$NBNS_BIN" \\', rendered)
        self.assertIn('--name "$SMB_NETBIOS_NAME"', rendered)
        self.assertNotIn('if [ ! -f "$RAM_PRIVATE/nbns.enabled" ]; then\n        return 0\n    fi\n\n    if [ ! -x "$NBNS_BIN" ]; then\n        log_msg "NBNS restart skipped; missing $NBNS_BIN"\n        return 0\n    fi\n\n    iface_ip="$(get_iface_ipv4 "$NET_IFACE")"\n    if [ -z "$iface_ip" ]; then\n        log_msg "NBNS restart skipped; missing IPv4 for $NET_IFACE"\n        return 0\n    fi\n\n    pkill "$NBNS_PROC_NAME" >/dev/null 2>&1 || true\n    "$NBNS_BIN"', rendered)

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

    def test_remote_prepare_dirs_builds_expected_command(self) -> None:
        with mock.patch("timecapsulesmb.deploy.executor.run_ssh") as run_ssh_mock:
            remote_prepare_dirs("host", "pw", "-o foo", "/Volumes/dk2/samba4")
        command = run_ssh_mock.call_args.args[3]
        self.assertEqual(command, render_remote_action(prepare_dirs_action("/Volumes/dk2/samba4")))

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
                rendered_start=Path("/tmp/start-samba.sh"),
                rendered_dfree=Path("/tmp/dfree.sh"),
                rendered_watchdog=Path("/tmp/watchdog.sh"),
                rendered_smbconf=Path("/tmp/smb.conf.template"),
            )
        self.assertEqual(scp_mock.call_count, 9)
        destinations = [call.args[4] for call in scp_mock.call_args_list]
        self.assertEqual(
            destinations,
            [
                "/Volumes/dk2/samba4/smbd",
                "/Volumes/dk2/samba4/mdns-advertiser",
                "/mnt/Flash/mdns-advertiser",
                "/Volumes/dk2/samba4/nbns-advertiser",
                "/mnt/Flash/rc.local",
                "/mnt/Flash/start-samba.sh",
                "/mnt/Flash/watchdog.sh",
                "/mnt/Flash/dfree.sh",
                "/Volumes/dk2/samba4/smb.conf.template",
            ],
        )

    def test_verify_netbsd4_activation_passes_when_fstat_has_smb_and_mdns_ports(self) -> None:
        fstat_output = """
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
        self.assertIn('while [ "$attempt" -lt 30 ]; do', remote_command)
        self.assertIn("sleep 1", remote_command)

    def test_verify_netbsd4_activation_requires_smbd_process_name_for_445(self) -> None:
        fstat_output = """
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

    def test_format_deployment_plan_contains_concrete_actions(self) -> None:
        paths = build_device_paths("/Volumes/dk2", "samba4")
        plan = build_deployment_plan("root@10.0.0.2", paths, Path("bin/smbd"), Path("bin/mdns"), Path("bin/nbns"), install_nbns=True)
        text = format_deployment_plan(plan)
        self.assertIn("volume root: /Volumes/dk2", text)
        self.assertIn("pkill -f '[w]atchdog.sh' >/dev/null 2>&1 || true", text)
        self.assertIn("pkill mdns-advertiser >/dev/null 2>&1 || true", text)
        self.assertIn("mkdir -p /Volumes/dk2/samba4 /Volumes/dk2/samba4/private /Volumes/dk2/samba4/cache /mnt/Flash", text)
        self.assertIn("/bin/sh -c ': > /Volumes/dk2/samba4/private/nbns.enabled'", text)
        self.assertIn("generated smbpasswd -> /Volumes/dk2/samba4/private/smbpasswd", text)
        self.assertIn("generated adisk UUID -> /Volumes/dk2/samba4/private/adisk.uuid", text)
        self.assertIn("generated nbns marker -> /Volumes/dk2/samba4/private/nbns.enabled", text)
        self.assertIn("ln -s /mnt/Memory/samba4 /root/tc-netbsd4", text)
        self.assertIn("chmod 755 /Volumes/dk2/samba4/cache", text)
        self.assertIn("chmod 700 /Volumes/dk2/samba4/private", text)

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
        self.assertEqual([action.kind for action in plan.activation_actions], ["stop_process_full", "stop_process", "stop_process", "run_script"])
        self.assertEqual([action.args[0] for action in plan.activation_actions], ["[w]atchdog.sh", "mDNSResponder", "wcifsfs", "/mnt/Flash/rc.local"])

        text = format_deployment_plan(plan)
        self.assertIn("Remote actions (NetBSD4 activation):", text)
        self.assertIn("pkill -f '[w]atchdog.sh' >/dev/null 2>&1 || true", text)
        self.assertIn("pkill mDNSResponder >/dev/null 2>&1 || true", text)
        self.assertIn("pkill wcifsfs >/dev/null 2>&1 || true", text)
        self.assertIn("/bin/sh /mnt/Flash/rc.local", text)
        self.assertIn("NetBSD4 activation is immediate.", text)
        self.assertIn("other generations may auto-start rc.local", text)
        self.assertIn("fstat shows smbd bound to TCP 445", text)

    def test_build_uninstall_plan_stops_nbns_process(self) -> None:
        paths = build_device_paths("/Volumes/dk2", "samba4")
        plan = build_uninstall_plan("root@10.0.0.2", paths)
        self.assertIn("pkill nbns-advertiser >/dev/null 2>&1 || true", [render_remote_action(action) for action in plan.remote_actions])

    def test_build_uninstall_plan_stops_watchdog_first(self) -> None:
        paths = build_device_paths("/Volumes/dk2", "samba4")
        plan = build_uninstall_plan("root@10.0.0.2", paths)
        rendered = [render_remote_action(action) for action in plan.remote_actions]
        self.assertEqual(rendered[0], "pkill -f '[w]atchdog.sh' >/dev/null 2>&1 || true")

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
